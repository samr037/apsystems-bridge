# APsystems Open Bridge — Web Flasher

A zero-install, browser-based flasher that programs the **APsystems Open Bridge** firmware onto a **Sonoff ZBDongle-P** (TI CC2652P). Plug, click, done.

This tool is **not** a general-purpose CC2652 flasher. It only knows how to download the binary listed in `manifest.json`. If you need to flash arbitrary images, use [`cc2538-bsl`](https://github.com/JelmerT/cc2538-bsl) or TI Uniflash.

## Browser support

Web Serial is **Chromium-only**. You need:

- Chrome (desktop, version 89+)
- Edge (desktop, version 89+)
- Opera (desktop, version 75+)

Firefox and Safari don't expose Web Serial and probably never will. The flasher detects this and shows a friendly notice.

The page also has to be served from a **secure context** — either `https://` or `http://localhost`. GitHub Pages hosts it as HTTPS so this is automatic for end users.

## How to use it

1. Plug the Sonoff ZBDongle-P into a USB port. (No need to press the boot button — the flasher pulses DTR/RTS to enter the ROM bootloader automatically.)
2. Open the flasher page.
3. Click **Connect**. Pick the dongle's serial device from the browser prompt (it usually shows up as a `CP2102N` / `Silicon Labs` USB-to-UART bridge).
4. The selected firmware is already populated from the manifest. Click **Flash**.
5. Watch the log: bootloader entry → sync → chip ID → erase → program → CRC32 verify → reset. Takes about 20-40 seconds for a typical image.
6. When the log says **"Flashing complete"**, the chip is already running the new firmware. Unplug / replug the dongle if your host software needs a fresh USB enumeration.

If anything goes wrong mid-flash, **don't unplug the dongle**. Click Connect → Disconnect, then try again — the chip stays in ROM bootloader after a failed flash as long as `!RESET` hasn't been pulsed.

## Publishing a new firmware build

The manifest's `url` points at the **`latest`** GitHub Release's `aps-bridge.bin` asset:

```
https://github.com/samr037/apsystems-bridge/releases/latest/download/aps-bridge.bin
```

To roll out a new firmware version:

1. Build the `.bin` from `firmware/` (see `firmware/BUILD.md`).
2. Cut a new GitHub Release in this repo.
3. Attach the file as `aps-bridge.bin`.
4. That's it — the flasher picks it up immediately, no rebuild needed.

Optionally bump `version` in `manifest.json` and set `crc32` to the hex digest of the published binary (e.g. `"0xDEADBEEF"`). When set, the flasher pre-verifies the downloaded image against the manifest CRC before touching the chip.

### CORS

The firmware URL has to be fetchable from the flasher's origin. GitHub Release downloads serve `Access-Control-Allow-Origin: *`, so this works out of the box for the URL pattern above. If you host firmware elsewhere, make sure the server emits a permissive CORS header.

## Local development

Web Serial requires a secure context — `localhost` counts as one. Serve the folder over HTTP from the project root:

```sh
cd flasher
python3 -m http.server 8080
# then open http://localhost:8080/
```

No build step. Edit `app.js` / `cc26xx-bsl.js` and reload.

## How it works (short version)

The CC2652P boots into a small ROM serial bootloader when its `IO15` pin is low at reset. The Sonoff ZBDongle-P wires the CP2102N's DTR/RTS lines through two NPN gates to the chip's `!RESET` and `IO15` pins, so the flasher can drive the chip into the bootloader by sequencing those lines (see `cc26xx-bsl.js` → `invokeBootloader`). Once in the bootloader, the flasher speaks a tiny packet protocol (TI SWRA466) at 500000 8N1:

| opcode | meaning |
|-------:|---------|
| `0x20` | PING |
| `0x21` | DOWNLOAD (set start addr + length) |
| `0x22` | RUN |
| `0x23` | GET_STATUS |
| `0x24` | SEND_DATA (chunk, ≤252 bytes) |
| `0x25` | RESET |
| `0x27` | CRC32 |
| `0x28` | GET_CHIP_ID |
| `0x2C` | BANK_ERASE |

The flow is `sync → ping → get chip ID → bank-erase → download header → loop send-data → CRC32 verify → reset`. The on-device CRC32 uses the standard IEEE-802.3 polynomial (`0xEDB88320`, reflected), same as ZIP/Ethernet, and the host computes the same one in pure JS to compare.

## ⚠️ Bootloader backdoor must stay enabled

The CC2652P's serial ROM bootloader is only reachable from the firmware if the application's CCFG (Customer Configuration) keeps the backdoor enabled:

```c
SET_CCFG_BL_CONFIG_BL_ENABLE = 0xC5;
```

This project's firmware **does** keep it enabled — see `firmware/ccfg.c`. If you ever flash an image that disables it, you lose serial re-flashability forever — recovery requires JTAG via the unpopulated pads inside the dongle. So: if you build a custom firmware, do **not** touch `BL_ENABLE` unless you have JTAG hardware and know what you're doing.

## License

Same license as the parent project (MIT, see repo root `LICENSE`).
