#!/usr/bin/env python3
"""Host driver for the CC2652P raw-802.15.4 bridge firmware.

Speaks the COBS-framed host<->firmware protocol (see firmware/proto.h and
firmware/PROTOCOL.md): promiscuous 802.15.4 receive, arbitrary-frame
transmit, channel selection. All Zigbee/APS protocol logic lives above this.

CLI (run on the Pi the dongle is attached to):
    bridge.py scan                 sweep channels 11-26, count 802.15.4 traffic
    bridge.py ping                 query firmware channel + version
"""
import argparse
import sys
import time

import serial

# --- protocol constants (mirror firmware/proto.h) -------------------------
PKT_RX_FRAME       = 0x01
PKT_TX_FRAME       = 0x02
PKT_SET_CHANNEL    = 0x03
PKT_LOG            = 0x04
PKT_TX_DONE        = 0x05
PKT_PING           = 0x06
PKT_INFO           = 0x07
PKT_SET_MAC_FILTER = 0x08
PKT_SET_TX_POWER   = 0x09     # v3+ firmware


# --- COBS -----------------------------------------------------------------
def cobs_encode(data: bytes) -> bytes:
    out = bytearray([0])
    code_idx = 0
    code = 1
    for b in data:
        if b == 0:
            out[code_idx] = code
            code_idx = len(out)
            out.append(0)
            code = 1
        else:
            out.append(b)
            code += 1
            if code == 0xFF:
                out[code_idx] = code
                code_idx = len(out)
                out.append(0)
                code = 1
    out[code_idx] = code
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        code = data[i]
        i += 1
        if code == 0:
            return b""
        for _ in range(code - 1):
            if i >= n:
                return bytes(out)
            out.append(data[i])
            i += 1
        if code != 0xFF and i < n:
            out.append(0)
    return bytes(out)


# --- bridge ---------------------------------------------------------------
class Bridge:
    """Connection to the CC2652P bridge firmware over a serial port."""

    def __init__(self, port: str = "/dev/ttyUSB0", baud: int = 115200):
        self.port = port
        self.baud = baud
        self.sp = None
        self._rx = bytearray()

    def open(self, reset: bool = True) -> None:
        self.sp = serial.Serial(self.port, self.baud, timeout=0.05)
        if reset:
            # Reset the dongle into the application (see tools/reset_probe.py).
            self.sp.dtr = False
            self.sp.rts = True
            time.sleep(0.30)
            self.sp.dtr = False
            self.sp.rts = False
            time.sleep(0.20)
        self.sp.reset_input_buffer()
        self._rx.clear()

    def close(self) -> None:
        if self.sp:
            self.sp.close()
            self.sp = None

    # -- framing --
    def send(self, ptype: int, data: bytes = b"") -> None:
        self.sp.write(cobs_encode(bytes([ptype]) + data) + b"\x00")

    def poll(self):
        """Return a list of (ptype, data) packets received since last call.

        If ``ping_with_reply`` re-queued any packets drained during its wait
        for a PKT_INFO reply (because it had to read past them), surface
        those first so they aren't lost to higher layers.
        """
        packets = []
        if hasattr(self, "_pending") and self._pending:
            packets.extend(self._pending)
            self._pending = []
        chunk = self.sp.read(4096)
        if chunk:
            self._rx += chunk
        while b"\x00" in self._rx:
            frame, _, rest = self._rx.partition(b"\x00")
            self._rx = bytearray(rest)
            if not frame:
                continue
            dec = cobs_decode(bytes(frame))
            if dec:
                packets.append((dec[0], dec[1:]))
        return packets

    # -- commands --
    def set_channel(self, channel: int) -> None:
        self.send(PKT_SET_CHANNEL, bytes([channel]))

    def tx_frame(self, frame: bytes) -> None:
        self.send(PKT_TX_FRAME, frame)

    def ping(self) -> None:
        self.send(PKT_PING)

    def ping_with_reply(self, timeout_s: float = 0.3) -> dict | None:
        """Send PKT_PING and wait up to ``timeout_s`` for the firmware's
        PKT_INFO reply. Returns ``{"channel": int, "version": int,
        "rx_status": int|None, "rx_active": bool}`` on success, or ``None``
        on timeout.

        ``rx_status`` is the RF core's CMD_IEEE_RX command status (firmware
        v5+); ``rx_active`` is True iff that status is ACTIVE (0x0002) — i.e.
        the receiver is genuinely armed, not just the MCU answering. On
        firmware v4 and earlier the reply has no rx-status field, so
        ``rx_status`` is None and ``rx_active`` defaults True (can't probe
        it — don't false-trigger the watchdog on old firmware).

        A timeout means the dongle is serially stuck (USB reset half-state);
        a reply with ``rx_active`` False means the dongle is "RF-deaf" —
        alive on USB but not receiving. Both warrant a bridge bounce.

        Frames received while we wait (including any unsolicited PKT_LOG)
        are not lost — they remain queued in the internal RX buffer and
        will be returned by the next ``poll()`` call. Only PKT_INFO is
        consumed here.
        """
        self.send(PKT_PING)
        deadline = time.time() + timeout_s
        leftover = []
        try:
            while time.time() < deadline:
                for ptype, data in self.poll():
                    if ptype == PKT_INFO and len(data) >= 2:
                        # Re-queue anything we collected before the INFO
                        # reply so the daemon's main poll-loop still sees
                        # them (e.g. PKT_RX_FRAME inverter replies that
                        # arrived during the ping window).
                        for p in leftover:
                            self._pending.append(p) if hasattr(self, "_pending") else None
                        # rx-status is the v5+ 2-byte LE tail. Absent on
                        # older firmware → rx_status None, rx_active True
                        # (no probe available, so don't false-trigger).
                        rx_status = None
                        rx_active = True
                        if len(data) >= 4:
                            rx_status = data[2] | (data[3] << 8)
                            rx_active = (rx_status == 0x0002)  # RF core ACTIVE
                        return {"channel": data[0], "version": data[1],
                                "rx_status": rx_status, "rx_active": rx_active}
                    leftover.append((ptype, data))
                time.sleep(0.01)
        finally:
            # If we didn't find INFO, anything we drained must be
            # re-injected so poll() in the daemon's main loop still
            # delivers RX frames captured during the ping window.
            if leftover:
                if not hasattr(self, "_pending"):
                    self._pending = []
                self._pending.extend(leftover)
        return None

    def set_mac_filter(self, pan_id: int, short_addr: int,
                       ieee_le: bytes) -> None:
        """Enable filtered + auto-ACK mode. ``ieee_le`` is the 8-byte IEEE
        address as it appears on the wire (LE byte order). Pass pan_id=0xFFFF
        to accept any PAN, or 0 with the dummy default to return to
        promiscuous (use ``set_promiscuous()`` for that)."""
        assert len(ieee_le) == 8
        data = (bytes([0x01])                                      # mode = 1 (filtered)
                + bytes([pan_id & 0xFF, (pan_id >> 8) & 0xFF])
                + bytes([short_addr & 0xFF, (short_addr >> 8) & 0xFF])
                + ieee_le)
        self.send(PKT_SET_MAC_FILTER, data)

    def set_promiscuous(self) -> None:
        """Return RX to promiscuous (no filter, no auto-ACK)."""
        self.send(PKT_SET_MAC_FILTER,
                  bytes([0]) + bytes(2) + bytes(2) + bytes(8))

    def set_tx_power(self, dbm: int) -> None:
        """Set the dongle's conducted TX power. ``dbm`` is a signed integer in
        the range supported by the firmware's TX power table (default-PA path:
        -20..+5 dBm). Out-of-table values are silently ignored by the
        firmware (a rejection is logged via PKT_LOG). v3+ firmware only;
        older firmware ignores the unknown packet type — no error raised."""
        d = int(dbm) & 0xFF                       # encode as signed-8 byte
        self.send(PKT_SET_TX_POWER, bytes([d]))


# --- 802.15.4 helper ------------------------------------------------------
# --- CLI ------------------------------------------------------------------
def _drain_logs(br: Bridge, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        for ptype, data in br.poll():
            if ptype == PKT_LOG:
                print(f"  [fw] {data.decode('ascii', 'replace')}")
            elif ptype == PKT_INFO and len(data) >= 2:
                print(f"  [fw] channel={data[0]} version={data[1]}")
        time.sleep(0.02)


def cmd_scan(br: Bridge, args) -> int:
    print(f"Scanning channels 11-26, {args.seconds}s each ...")
    _drain_logs(br, 1.0)
    total = 0
    for ch in range(11, 27):
        br.set_channel(ch)
        time.sleep(0.15)
        for _ in br.poll():
            pass  # discard frames from the previous channel
        count = 0
        deadline = time.time() + args.seconds
        while time.time() < deadline:
            for ptype, data in br.poll():
                if ptype == PKT_RX_FRAME:
                    count += 1
            time.sleep(0.01)
        total += count
        bar = "#" * min(count, 50)
        print(f"  ch {ch:2d}: {count:4d} frames  {bar}")
    print(f"total: {total} frames")
    print("RX VERIFIED" if total else "no traffic seen on any channel")
    return 0 if total else 1


def cmd_ping(br: Bridge, args) -> int:
    br.ping()
    _drain_logs(br, 1.5)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-p", "--port", default="/dev/ttyUSB0")
    ap.add_argument("-b", "--baud", type=int, default=115200)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="sweep channels 11-26")
    p.add_argument("-s", "--seconds", type=float, default=2.5)

    sub.add_parser("ping", help="query firmware")

    args = ap.parse_args()

    br = Bridge(args.port, args.baud)
    br.open()
    try:
        return {"scan": cmd_scan,
                "ping": cmd_ping}[args.cmd](br, args)
    finally:
        br.close()


if __name__ == "__main__":
    sys.exit(main())
