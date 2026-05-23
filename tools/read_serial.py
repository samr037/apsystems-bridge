#!/usr/bin/env python3
"""Reset the Sonoff ZBDongle-P into application mode and dump its UART.

The dongle's CP2102N DTR/RTS lines feed a logic gate driving the CC2652P
reset and the DIO15 BSL pin. The gate is stateful and its polarity is not
obvious; the reset-to-app sequence below was found empirically with
tools/reset_probe.py (DTR=0,RTS=1 held, then DTR=0,RTS=0 released — boots
the application; note that DTR=1,RTS=0 -> DTR=0,RTS=0 instead drops into the
ROM bootloader). Pass --no-reset to just open and read.

Usage: read_serial.py [port] [baud] [seconds] [--no-reset]
Output is shown both as decoded text and as a hex dump.
"""
import sys
import time

import serial


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}

    port = args[0] if len(args) > 0 else "/dev/ttyUSB0"
    baud = int(args[1]) if len(args) > 1 else 115200
    secs = float(args[2]) if len(args) > 2 else 6.0

    sp = serial.Serial(port, baud, timeout=0.25)

    if "--no-reset" not in flags:
        # Reset into the application (empirically found, see module docstring).
        sp.dtr = False
        sp.rts = True
        time.sleep(0.30)
        sp.dtr = False
        sp.rts = False
        time.sleep(0.10)

    buf = bytearray()
    deadline = time.time() + secs
    while time.time() < deadline:
        data = sp.read(256)
        if data:
            buf += data
    sp.close()

    if not buf:
        sys.stderr.write("[read_serial] no data received "
                         f"(port={port} baud={baud})\n")
        return 1

    sys.stdout.write(f"[read_serial] {len(buf)} bytes @ {baud} baud\n")
    sys.stdout.write("--- text ---\n")
    sys.stdout.write(buf.decode("ascii", "replace"))
    sys.stdout.write("\n--- hex (first 128) ---\n")
    sys.stdout.write(buf[:128].hex(" "))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
