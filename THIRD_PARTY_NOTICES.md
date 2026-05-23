# Third-party notices

This project is licensed under MIT (see [`LICENSE`](LICENSE)) but ports and
references code and information from several other projects. Their licenses
and the conditions they impose on this work are documented below.

---

## Contiki-NG — `firmware/ti_radio_config.c`

The IEEE 802.15.4 RF configuration in `firmware/ti_radio_config.c` and
`firmware/ti_radio_config.h` is ported from Contiki-NG's open CC13x2/CC26x2
RF settings (`arch/cpu/simplelink-cc13xx-cc26xx/rf-settings/cc26x2/ieee-settings.c`).
Contiki-NG is BSD-3-Clause licensed.

> Copyright (c) Texas Instruments Incorporated — http://www.ti.com/
> Copyright (c) The Contiki-NG developers
>
> All rights reserved.
>
> Redistribution and use in source and binary forms, with or without
> modification, are permitted provided that the following conditions are
> met:
>
> 1. Redistributions of source code must retain the above copyright
>    notice, this list of conditions and the following disclaimer.
>
> 2. Redistributions in binary form must reproduce the above copyright
>    notice, this list of conditions and the following disclaimer in the
>    documentation and/or other materials provided with the distribution.
>
> 3. Neither the name of the copyright holder nor the names of its
>    contributors may be used to endorse or promote products derived from
>    this software without specific prior written permission.
>
> THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
> "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
> LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
> A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
> HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
> SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
> LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
> DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
> THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
> (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
> OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

Upstream: https://github.com/contiki-ng/contiki-ng (file:
`arch/cpu/simplelink-cc13xx-cc26xx/rf-settings/cc26x2/ieee-settings.c`).

---

## Texas Instruments SimpleLink SDK 8.30.01.01 — build-time linkage

The CC2652P firmware in `firmware/` is built against the TI SimpleLink
CC13x2/CC26x2 SDK (driverlib, drivers, NoRTOS modules, RF driver). The SDK
is governed by the [TI Software Programs Agreement (TSPA)](https://www.ti.com/corp/docs/legal/swlicense/spnu489a.html).

- **We do not redistribute SDK source or headers** in this repository.
  The build instructions in `firmware/BUILD.md` direct builders to install
  the SDK themselves from TI's website.
- **Compiled binaries** of our firmware are linked against SDK libraries.
  Per TSPA section 2.1, redistribution of derivative binaries is permitted
  when they are used on TI hardware (the CC2652P qualifies). Binaries
  produced from this codebase will be attached to GitHub Releases with
  this notice file included.

---

## patience4711 — APsystems decoder formulas (MIT)

The decode formulas in `host/aps_unified_daemon.py` (YC600 / DS3) are
a Python port of formulas
published by patience4711 in
[`ESP32-read-APS-inverters/AAA_DECODE.ino`](https://github.com/patience4711/ESP32-read-APS-inverters/blob/main/AAA_DECODE.ino),
**MIT License**, Copyright (c) 2023 patience4711.

The same formulas (same magic constants, same frame offsets) also appear
in patience4711's earlier C++ implementation
([`RPI-APS-inverters/ecu/inverterPoll.cpp`](https://github.com/patience4711/RPI-APS-inverters))
and ESP8266 implementation
([`read-APSystems-YC600-QS1-DS3/AAA_DECODE.ino`](https://github.com/patience4711/read-APSystems-YC600-QS1-DS3)).
This project credits the MIT-licensed ESP32 codebase as the canonical
source because it carries an explicit license grant.

The Python port is our own (adapted for our raw-802.15.4 bridge
framing), but the underlying formulas, constants, and offsets are
patience4711's work.

---

## kadzsol — `CC2531ZNP-with-SBL.hex` / `CC2530ZNP-with-SBL.hex` (runtime dependency, not redistributed)

The legacy CC2531/CC2530 coordinator path in this project uses the
"kadzsol" modified Z-Stack Home 1.2 firmware (the build with NWK security
disabled, allowing APsystems' unencrypted inter-PAN frames). That firmware
is **Creative Commons BY-NC-SA 3.0** — non-commercial, share-alike,
attribution required. It is **not redistributed in this repository**.

- Canonical distribution: the kadzsol Discord and the
  [Koenkk/zigbee2mqtt#4221](https://github.com/Koenkk/zigbee2mqtt/issues/4221)
  reverse-engineering thread.
- `.gitignore` excludes `cc2531-fw/` and any committed `.hex` files to
  prevent accidental redistribution.
- Users following the legacy path are directed to download kadzsol's
  firmware themselves from those upstream channels.

The CC2652P path (in `firmware/`) is **fully open-source**, builds with
GCC + TI SDK, and has no runtime dependency on kadzsol's firmware. This is
the recommended path for new installs.

---

## Python runtime dependencies

| Package | License | Use |
|---|---|---|
| `pyserial` | BSD-3-Clause | USB serial transport (CC2652P bridge, CC2531 MT) |
| `paho-mqtt` | EPL-2.0 + EDL-1.0 (dual) | Optional MQTT publisher in `aps_unified_daemon.py` |

Both are permissively licensed and redistribution-compatible with MIT.

---

## Reverse-engineering corpus / informational references

These were consulted but no code was copied. Listed for credit:

- **kadzsol** — reverse-engineered the APsystems on-air protocol and built
  the modified Z-Stack Home 1.2 firmware that the CC2531/CC2530 path
  depends on. See above for license terms on their firmware.
- **No13 / `ApsYc600-Pythonlib`** — first public Python pair+poll for
  kadzsol coordinators (informational reference for the pairing dance).
- **Koenkk/zigbee2mqtt#4221** — the 823-comment reverse-engineering
  thread where most of the protocol was documented in 2020.
- **nccgroup/Sniffle** — headless CC2652 firmware build reference (BSD).
- **Texas Instruments E2E forum** — multiple posts on `RF_scheduleCmd`
  FG/BG slot routing that resolved our CC2652P TX bug.
