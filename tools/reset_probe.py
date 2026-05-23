#!/usr/bin/env python3
"""Brute-force the Sonoff ZBDongle-P reset-to-application DTR/RTS sequence.

The dongle's CP2102N DTR/RTS lines feed a logic gate driving the CC2652P
!RESET and the DIO15 BSL pin. The exact polarity is hard to derive, so this
tries every plausible (hold, release) DTR/RTS pair, resetting the chip each
time, and reads the UART after each. Whichever pair makes the application
firmware talk is the reset-to-app sequence.

Usage: reset_probe.py [port] [baud] [seconds-per-combo]
"""
import sys
import time

import serial

COMBOS = [
    ("hold(1,0) release(0,0)", (True, False), (False, False)),
    ("hold(0,1) release(0,0)", (False, True), (False, False)),
    ("hold(1,0) release(1,1)", (True, False), (True, True)),
    ("hold(0,1) release(1,1)", (False, True), (True, True)),
    ("hold(1,1) release(0,0)", (True, True), (False, False)),
    ("hold(0,0) release(1,0)", (False, False), (True, False)),
    ("hold(1,1) release(1,0)", (True, True), (True, False)),
    ("hold(0,0) release(0,1)", (False, False), (False, True)),
]


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
    secs = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0

    sp = serial.Serial(port, baud, timeout=0.2)
    hit = False
    for name, hold, release in COMBOS:
        sp.dtr, sp.rts = hold
        time.sleep(0.30)
        sp.dtr, sp.rts = release
        time.sleep(0.10)
        sp.reset_input_buffer()

        data = bytearray()
        deadline = time.time() + secs
        while time.time() < deadline:
            chunk = sp.read(256)
            if chunk:
                data += chunk

        mark = "  <<< DATA" if data else ""
        text = data.decode("ascii", "replace").replace("\r", "").replace("\n", "|")
        print(f"{name:28s} {len(data):4d} bytes  {text[:70]!r}{mark}")
        if data:
            hit = True
    sp.close()
    return 0 if hit else 1


if __name__ == "__main__":
    raise SystemExit(main())
