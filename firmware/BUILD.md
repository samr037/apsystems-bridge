# Firmware build setup — CC2652P raw 802.15.4

Headless build recipe for the custom raw-IEEE-802.15.4 firmware on the
CC2652P (Sonoff ZBDongle-P). Build host: your Linux build host.
**No TI.com account, no Code Composer Studio, no SysConfig required.**

The scaffold below is **verified end to end** (2026-05-19): it builds with
GCC, flashes with cc2538-bsl, and runs on the dongle (UART banner observed
on `/dev/ttyUSB0`).

## SDK — GitHub mirror

TI's `.run` SDK installers are no longer anonymously fetchable (302→404). The
GitHub mirror is sufficient — it carries the RF + UART2 drivers, the RF-core
IEEE command headers, CC2652 driverlib / linker files / RF patches, and the
NoRTOS kernel (only `examples/` and `docs/` are stripped, which we don't need).

```sh
git clone --depth 1 --branch lpf2-8.30.01.01 \
  https://github.com/TexasInstruments/simplelink-lowpower-f2-sdk.git \
  simplelink_cc13xx_cc26xx_sdk_8_30_01_01
```

Keep that exact directory name — TI makefiles expect the path shape.

## Toolchain

- **arm-gnu-toolchain** — xpack `arm-none-eabi-gcc` **14.2.1** (not Debian
  `apt`; apt's lacks the newlib headers). Installed at
  `/opt/aps-firmware/xpack-arm-none-eabi-gcc-14.2.1-1.1`.
- **CMake ≥ 3.21** — the SDK's top-level `CMakeLists.txt` requires it.
  Debian 11's CMake (3.18) is too old; a Kitware binary tarball is unpacked
  at `/opt/aps-firmware/cmake/`.
- **No SysConfig.** TI gates the SysConfig installer behind a TI.com login.
  The firmware is hand-assembled instead: `ccfg.c` wraps the SDK CCFG with
  bootloader-backdoor overrides; the IEEE-802.15.4 RF config will be lifted
  from Contiki-NG's open `cc13xx-cc26xx` driver. The build needs no SysConfig.
- **No XDCtools, no CCS** — NoRTOS + raw-RF builds purely with GCC + make.

## Step 1 — build the SDK libraries (once)

The GitHub mirror ships *sources*, not prebuilt libraries (except the RF
multi-mode driver). Build them with the SDK's CMake build.

Edit `<SDK>/imports.mak`: set `GCC_ARMCOMPILER` to the xpack GCC path and
`CMAKE` to the CMake ≥ 3.21 binary. Then:

```sh
cd <SDK>
export GCC_ARMCOMPILER=/opt/aps-firmware/xpack-arm-none-eabi-gcc-14.2.1-1.1
cmake -G "Unix Makefiles" . -B build/gcc
cmake --build build/gcc --target \
    driverlib_cc13x2_cc26x2 drivers_cc26x2 nortos_cc26x2
```

(`make build-gcc` from the SDK root builds *everything* — slow on a 1-core
box; the targeted build above produces just what the firmware links.)

This yields, under `<SDK>/build/gcc/`:

| Library | Path |
|---|---|
| `driverlib.lib` | `source/ti/devices/cc13x2_cc26x2/lib/gcc/m4f/driverlib/` |
| `drivers_cc26x2.a` | `source/ti/drivers/lib/gcc/m4f/drivers_cc26x2/` |
| `nortos_cc26x2.a` | `kernel/nortos/lib/gcc/m4f/nortos_cc26x2/` |

The RF multi-mode driver `rf_multiMode_cc26x2.a` ships prebuilt for GCC at
`<SDK>/source/ti/drivers/rf/lib/gcc/m4f/`. `driverlib` is built soft-float
(it contains no FP code) and links cleanly into the hard-float (`m4f`) image.

## Step 2 — build the firmware

```sh
cd firmware/
make            # -> firmware.hex
```

The `makefile` overrides `SDK` / `GCC` if the install paths differ. Build
flow: GCC compiles `main.c` + `ccfg.c`, links against the four SDK libraries
with `cc13x2_cc26x2_nortos.lds`, `objcopy` → `firmware.hex`.

Key build details (already wired into the makefile):

- `-DDeviceFamily_CC13X2_CC26X2`, `-mcpu=cortex-m4 -mfloat-abi=hard
  -mfpu=fpv4-sp-d16`.
- Linker script **`cc13x2_cc26x2_nortos.lds`** (SDK `source/ti/boards/...`).
  This is the script that matches the NoRTOS startup object — it provides
  `_stack_end`, `__data_*`, `__bss_*`, `__init_array_*`. The other SDK
  script, `cc26x2r1f.lds`, uses different symbol names and does **not** work
  with this startup.
- `-Wl,-u,resetISR` forces the linker to pull the startup object (reset
  handler + vector table) from `nortos_cc26x2.a` — `ENTRY()` alone does not
  extract an archive member. Note the symbol is `resetISR`, lower-case.
- `ccfg.c` is compiled as a plain object; it wraps the SDK `ccfg.c`.

## CCFG — keep the dongle flashable

**The SDK's default CCFG disables the ROM bootloader and its backdoor.**
Flashing that would leave the dongle un-reflashable over USB (only JTAG
recovery). `firmware/ccfg.c` overrides the bootloader fields to re-enable
the ROM bootloader and the backdoor on **DIO15, active-low** — the pin and
polarity the ZBDongle-P's reset gate uses. Verify the built image before
flashing: the `.ccfg` section's `BL_CONFIG` word (flash 0x57FD8) must read
`0xC5FE0FC5`. The CC2652 is *not* unconditionally unbrickable — a bad CCFG
is the way to brick it; this override is what keeps it safe.

## Flashing

```sh
python3 cc2538_bsl.py -p /dev/ttyUSB0 --bootloader-sonoff-usb -e -w -v firmware.hex
```

cc2538-bsl leaves the chip in the ROM bootloader. To run the application,
reset the dongle into app mode — the ZBDongle-P's DTR/RTS reset gate is
stateful; `tools/read_serial.py` (DTR=0/RTS=1 held, then DTR=0/RTS=0) does
this and dumps the UART. `tools/reset_probe.py` brute-forces the sequence if
needed.

## Radio — RF core in IEEE 802.15.4 mode (next: radio.c)

The CC13x2/CC26x2 RF core has a dedicated IEEE 802.15.4 PHY (2.4 GHz O-QPSK
250 kbps). Commands (from `driverlib/rf_ieee_cmd.h`):

- `CMD_RADIO_SETUP` — one-time PHY config (`mode = RF_MODE_IEEE_15_4`).
- `CMD_FS` — synthesizer frequency; `freq = 2405 + 5·(ch-11)` MHz, ch 11–26.
- `CMD_IEEE_RX` — receiver; **promiscuous** = `frameFiltOpt.frameFiltEn = 0`,
  `autoAckEn = 0`.
- `CMD_IEEE_TX` — arbitrary-frame TX; **hardware appends the 2-byte FCS**.
- `CMD_IEEE_CSMA` — optional CSMA-CA before TX; skip for "transmit now".

NoRTOS init: `NoRTOS_start()` → `RF_open(…, CMD_RADIO_SETUP)` → `CMD_FS` →
post `CMD_IEEE_RX` with an `RF_EventRxEntryDone` callback; TX via
`RF_runCmd(CMD_IEEE_TX)`. The RF driver needs a small hand-written driver
config (`ti_drivers_config.c`) since SysConfig is not used.

## Project layout (`firmware/`)

```
firmware/
├── makefile                 # headless GCC build
├── main.c                   # NoRTOS bring-up + UART (verified scaffold)
├── ccfg.c                   # Customer Config Area + bootloader-backdoor override
├── cc13x2_cc26x2_nortos.lds  # linker script (copy of the SDK NoRTOS script)
│  --- still to come (raw 802.15.4 bridge) ---
├── radio.{c,h}              # RF_open, CMD_FS, CMD_IEEE_RX (promisc), CMD_IEEE_TX
├── uart_bridge.{c,h}        # framed host <-> firmware serial protocol
├── ti_radio_config.{c,h}    # IEEE 802.15.4 RF config (from Contiki-NG)
└── ti_drivers_config.{c,h}  # hand-written RF driver config
```
