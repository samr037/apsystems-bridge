# Host ↔ firmware protocol

The CC2652P bridge firmware and the host (`host/aps_bridge/bridge.py`) talk
over the USB-CDC serial link (`/dev/ttyUSB0`, 115200 8N1) with a small
COBS-framed packet protocol. Constants live in `firmware/proto.h` (firmware)
and at the top of `bridge.py` (host) — keep them in sync.

## Framing

Every packet is **COBS-encoded** and terminated by a single `0x00` byte.
[Consistent Overhead Byte Stuffing](https://en.wikipedia.org/wiki/Consistent_Overhead_Byte_Stuffing)
removes all `0x00` bytes from the encoded payload, so the `0x00` delimiter
unambiguously frames packets even though the 802.15.4 frames carried are
arbitrary binary. A decoded packet is:

```
[ type : 1 byte ] [ data : 0..N bytes ]
```

No length field (the COBS delimiter gives the boundary) and no checksum
(the USB-CDC link is reliable; a CRC can be added later if needed).

## Packets — firmware → host

| Type | Name | Data |
|------|------|------|
| `0x01` | `RX_FRAME` | `[rssi:i8][corr:u8][channel:u8][802.15.4 MAC frame, no FCS]` |
| `0x04` | `LOG` | ASCII text — banners, errors, debug |
| `0x05` | `TX_DONE` | `[status:u8]` — 0 = TX ok, non-zero = failed |
| `0x07` | `INFO` | `[channel:u8][fw_version:u8]` — reply to `PING` |

`RX_FRAME` carries the received 802.15.4 MAC frame **without** the 2-byte
FCS (the radio validates and strips it). `rssi` is signed dBm; `corr` is the
6-bit correlation/LQI value.

## Packets — host → firmware

| Type | Name | Data |
|------|------|------|
| `0x02` | `TX_FRAME` | `[802.15.4 MAC frame to transmit, no FCS]` |
| `0x03` | `SET_CHANNEL` | `[channel:u8]` — IEEE channel 11..26 |
| `0x06` | `PING` | (none) — firmware replies `INFO` |

`TX_FRAME` carries only the MAC frame (MAC header … MAC payload); the radio
generates the PHY header and appends the FCS on air. The frame must already
be a complete, correctly-formed 802.15.4 MAC frame — the firmware does no
protocol processing.

## Notes

- Promiscuous RX: the firmware receives **all** 802.15.4 traffic on the
  current channel — no address/PAN filtering, no auto-ACK. It is a raw
  receiver and transmitter; all Zigbee/APS logic is on the host.
- A `TX_FRAME` briefly suspends RX while the frame is sent, then RX resumes.
- The dongle must be reset into the application after flashing — see
  `tools/reset_probe.py` / `bridge.py`'s `open()`.
