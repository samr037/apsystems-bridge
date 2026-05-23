# APsystems YC600 / QS1 / DS3 micro-inverter protocol

Reverse-engineered from `No13/ApsYc600-Pythonlib`, `patience4711/ESP32-read-APS-inverters`
and `patience4711/read-APSystems-YC600-QS1-DS3`. The references run on a CC253x
with closed Z-Stack **Home 1.2** firmware; the APS *application* layer (C–G) is
firmware-independent, and the coordinator bring-up (A) is handled by this
project's custom CC2652P firmware (see `firmware/`).

## 0. MT serial transport

115200 8N1. Standard TI MT frame:

```
SOF=0xFE | LEN(1) | CMD0(1) | CMD1(1) | DATA[LEN] | FCS(1)
```

- `LEN` counts DATA bytes only.
- `CMD0/CMD1` = 16-bit MT command, big-endian as written (`0x2401` → CMD0=0x24).
- `FCS` = XOR of `LEN || CMD0 || CMD1 || DATA` (running XOR, *not* a CRC).
- Frames may arrive concatenated — resync on `0xFE`. The reference firmware
  occasionally emits stray `F8`/`F0F8` bytes between frames; treat non-`FE`
  bytes as noise (likely a custom-firmware artefact — verify on Z-Stack 3.x).

## A. Coordinator bring-up — "fake ECU"

`ECU_ID` = 12-hex controller id (default `D8A3000000DE`). `rev_ecu` = ECU_ID
byte-reversed. The reference sequence (Home 1.2 Simple API):

| # | Send (CMD+DATA hex) | Meaning |
|---|---|---|
| 0 | `2605 03 01 03` | ZB_WRITE_CONFIGURATION `ZCD_NV_STARTUP_OPTION`=0x03 (clear state+config) |
| 1 | `4100 00` | SYS_RESET_REQ (hardware) → SYS_RESET_IND `4180` |
| 2 | `2605 01 08 FFFF`+`rev_ecu` | `ZCD_NV_EXTADDR` = fake ECU IEEE addr |
| 3 | `2605 87 01 00` | `ZCD_NV_LOGICAL_TYPE`=0x00 (coordinator) |
| 4 | `2605 83 02`+`ECU_ID[0:4]` | `ZCD_NV_PANID` (e.g. PAN 0xA3D8) |
| 5 | `2605 84 04 00000100` | `ZCD_NV_CHANLIST` = channel 16 (bit 16, LE) |
| 6 | `2400 14050F00010100020000150000` | AF_REGISTER endpoint 0x14 (replay verbatim) |
| 7 | `2600` | ZB_START_REQUEST — start as coordinator |
| 8 | `2700` | ZB_GET_DEVICE_INFO — readback / health |
| 9 | the "NO" frame (E.0) | normal-op only; registers ECU into routing |

**Z-Stack 3.x mapping (our port):**
- `2605` ZB_WRITE_CONFIGURATION → `SYS_OSAL_NV_WRITE` (0x2109) on the same
  `ZCD_NV_*` item ids (STARTUP_OPTION=0x0003, EXTADDR=0x0001, LOGICAL_TYPE=0x0087,
  PANID=0x0083, CHANLIST=0x0084).
- `2600` ZB_START_REQUEST → `ZDO_STARTUP_FROM_APP` (0x2540).
- `2700` ZB_GET_DEVICE_INFO → `UTIL_GET_DEVICE_INFO` (0x2700, same id).
- `AF_REGISTER` (0x2400), `AF_DATA_REQUEST` (0x2401), `AF_DATA_REQUEST_EXT`
  (0x2402), `AF_INCOMING_MSG` (0x4481), `AF_DATA_CONFIRM` (0x4480) — unchanged.
- **Risk:** `ZCD_NV_EXTADDR` is often write-locked on Z-Stack 3.x. If so, use the
  chip's real MAC and pair the inverters fresh against it (we are not
  impersonating an existing ECU).

MT command ids: `2101` SYS/ZB ping · `2400/6400` AF_REGISTER · `2401/6401`
AF_DATA_REQUEST · `2402` AF_DATA_REQUEST_EXT · `4100/4180` SYS_RESET ·
`4481` AF_INCOMING_MSG · `4480` AF_DATA_CONFIRM (status `CD` = NoRoute).

## B. Addressing

`AF_REGISTER` payload `14 05 0F00 0101 0002 0000 15 0000` — endpoint **0x14**,
profile **0x000F**; does not split cleanly into the canonical layout → **replay
verbatim**.

In data frames the recurring `14 14 06 0001 00 0F` is **DstEP=0x14, SrcEP=0x14,
ClusterID=0x0006, TransID, Options, Radius=0x0F**. The community shorthand
"cluster 0x1414" is wrong — `14 14` is the endpoint pair; the real cluster is
`0x0006`.

- ECU short address `0x0000` (it is the coordinator).
- Inverter destination = 16-bit `inv_id`, transmitted **byte-reversed** (LE).
- PAN `0xA3D8`, channel 16.

## C. APS application frame (`FBFB … FEFE`)

Carried as the AF data payload (outbound) / inside AF_INCOMING_MSG (inbound).

```
FBFB | LL | OP | args… | VV | FEFE
```

`LL` = app length, `OP` = opcode, `VV` = validation byte.

- Poll:   `FBFB 06 BB 000000000000 C1 FEFE`
- Query:  `FBFB 06 DE 000000000000 E4 FEFE`  (reads back power setpoint)
- Reboot: `FBFB 06 C1 000000000000 A6 FEFE`

**Validation byte:** there is **no single CRC**. Poll/query/reboot use hard-coded
constants (`C1/E4/A6`) — replay them. Only the variable set-power command
computes a checksum (YC600: additive-16 sum; DS3: `(msb+lsb-0x29)&0xFF`). The
2-byte trailer after `FEFE` in inbound frames is never validated → opaque.

## D. Pairing

The `inv_id` is **discovered**, not computed — the printed serial (YC600
`806000XXXXXX`) is only a search key. Four `AF_DATA_REQUEST_EXT` (0x2402) frames,
each broadcast (dest `FF…FF`), sent after coordinator bring-up steps 0–8
(no "NO" frame). `serial` = printed serial; `ecu_short` = `ECU_ID[2:4]+ECU_ID[0:2]`.

| # | CMD `2402` payload |
|---|---|
| 0 | `0F FFFFFFFFFFFFFFFF 14 FFFF 14 0D 02 0000 0F 11 00`+`serial`+`FFFF10FFFF`+`rev_ecu` |
| 1 | `0F FFFFFFFFFFFFFFFF 14 FFFF 14 0C 02 0100 0F 06 00`+`serial` |
| 2 | `0F FFFFFFFFFFFFFFFF 14 FFFF 14 0F 01 0200 0F 11 00`+`serial`+`ecu_short`+`10FFFF`+`rev_ecu` |
| 3 | `0F FFFFFFFFFFFFFFFF 14 FFFF 14 01 01 0300 0F 06 00`+`rev_ecu` |

Replies to frames 1 & 2 carry the answer. The `inv_id` = **4 hex chars right
after the last occurrence of the serial** in a reply. Reject `0000`, `FFFF`, or
`rev_ecu[-4:]`. Valid reply length ≈ 60–222 hex chars. ~1.1 s listen + ~1.5 s
settle between frames. Pairing must be redone on a fresh coordinator.

`DstAddrMode 0x0F` in these frames is non-standard (canonical 0x03/0x02) —
replay verbatim, verify on 3.x.

## E. Polling

**E.0 "NO" frame** — once after bring-up, before polling:
`2401 FFFF 1414 06 0001 00 0F 1E `+`rev_ecu`+` FBFB 11 00000D6030FBD3 000000000000 0004010281 FEFE`
— broadcast; exact length matters → replay verbatim.

**E.1 Poll** (identical YC600/QS1/DS3):
`2401 `+`rev(inv_id)`+` 1414 06 0001 00 0F 13 `+`rev_ecu`+` FBFB06BB000000000000C1FEFE`

**E.2 Cadence** 30–60 s. After sending, wait ~1 s then read.

**E.3 Response chain:** `FE01640100` (AF_DATA_REQUEST SRSP ok) → `FE03448000`
(AF_DATA_CONFIRM ok; `CD`=NoRoute) → optional `45C4` route ind → `4481`
AF_INCOMING_MSG with data. Valid telemetry response ≥ 223 hex chars.

**E.4 Reboot** (soft-reboot the inverter application). Same envelope as poll:
`… FBFB06C1000000000000A6FEFE`. Sourced from patience4711's MIT-licensed
ESP32 firmware (`ZIGBEE_HELPERS.ino::inverterReboot()`).

**E.5 Set max power.** Throttles each of the inverter's MPPT channels to
≤ N W — so total AC output ≈ N × panel count (the value is applied
*per-panel*; see below) — until the inverter loses DC (overnight).

**It is a 3-frame sequence, not a single frame** — this is the key point.
The throttle frame alone does *not* take effect; the inverter only latches
the new ceiling after the trailing nonsense + query frames. Sequence (each
frame its own seq/aps pair, reply discarded except the last):

1. **throttle** —
   - YC600 / QS1: `… FBFB061C8C02 <sc_hi><sc_lo>00 <cks_hi><cks_lo> FEFE`
     where `scaled = trunc(W × 28.89)`, big-endian; checksum =
     `sum(bytes [06,1C,8C,02,sc_hi,sc_lo,00])` (16-bit, big-endian).
   - DS3: `… FBFB06AA270000 <msb><lsb>01 <vv> FEFE`
     where `scaled = trunc(W × 16.59)`, big-endian; `vv = ((msb+lsb) − 0x29) & 0xFF`.
2. **nonsense** — `… FBFB06DE00000000000000FEFE`
3. **query** — `… FBFB06DE000000000000E4FEFE`

`scaled` uses C-style truncation (`int Scaled = W * 28.89;`), not rounding.

**Reading back the programmed ceiling.** The query reply's FBFB…FEFE block
ends `…<value:2B><marker:2B><00 00 00 00> FEFE`. The `marker` is *not*
constant — patience4711's `decodeQueryAnswer()` hardcodes `3B66` but that
was just his one capture (ours reads `0366`). Strip the trailing `FEFE`
and the programmed value is hex chars `[−16:−12]`; watts = value / 28.89.

Sourced from patience4711's MIT-licensed ESP32 firmware (`SETPOWER.ino`,
`ZIGBEE_QUERYING.ino`) and its project wiki ("14 Inverter throttling").

**Verification status (2026-05-22).**

*Frame format — VERIFIED.* YC600/QS1 + DS3 builders diffed against the
`SETPOWER.ino` algorithm for every watt value in range: **0 mismatches**.
A live frame was also confirmed on air, byte-identical to the reference.

*Command accepted + stored — VERIFIED.* With the full 3-frame sequence the
inverter accepts a throttle value and the query read-back returns exactly
what was commanded: yc600-1 500→500 and 420→420 (`0x386D`, `0x2F65`); ds3
600→600 (`0x26E2`). An out-of-range value is rejected — ds3 set to 800 W
read back 440 W (a prior value), so the DS3 appears to clamp/reject above
its nameplate. The earlier "no effect" result is now understood: frame 1
alone is never accepted — the trailing nonsense + query frames are
required. (Not ECU-pairing rejection, as previously hypothesised; no pair
handshake is needed.)

*Output enforcement — CONFIRMED on DS3, but per-MPPT-channel.* A binding
test settled it. ds3 capped at 300 W pinned its AC output at **662–670 W,
dead-flat for 11 minutes** while the sky was clear and the unthrottled
yc600s climbed — unambiguously a hard cap, not a cloud. A second point at
500 W left output at ~793 W (≈ uncapped). The DS3 is dual-MPPT (~805 W
baseline ≈ 2 × ~400 W/channel) and the data fits **single-channel
throttling**:

| command | enforced | interpretation |
|---|---|---|
| 300 W | ~667 W | ch1 → 300 + ch2 free ~367 |
| 500 W | ~793 W | 500 > ch1's ~400 natural → cap inactive |

The DS3 throttle frame's `01` byte (`…06AA2700 00 <msb><lsb> 01 <vv>`) is
the **MPPT-channel selector** — **confirmed**. Throttling only channel 1
(as we, and patience4711 who hardcodes `01`, originally did) leaves
channel 2 free. To cap a DS3's *total* output, send the throttle for
**both** channels — a `channel=1` frame and a `channel=2` frame (the `vv`
validation byte is unchanged; patience's formula sums only `06,AA,27,
msb,lsb`). The daemon now does this automatically for DS3.

**Dual-channel verified:** ds3 was capped at 150 W on both channels — AC
output dropped from ~805 W and held at **324–342 W (mean ~334)** for 10
minutes while the unthrottled yc600-1 free-wandered 295–331 W. ~334 ≈
2×~167 — each channel settles slightly above the commanded 150 W (~11 %
high). An earlier 600 W single-channel test showed no drop — consistent:
600 > a channel's ~400 W natural output, so that cap never engaged.

**Calibration offset.** Every set-power command transmits
`effective = requested + calib`, where `calib` is a signed per-inverter
watt offset stored in `inverters.json` (bounded ±300 W, default 0). It
trims the throttle drift: if a 150 W cap settles at ~167 W, set
`calib = -17`. Verified — ds3 with `calib=-20`, commanded 300 W,
transmitted + read back exactly 280 W. The offset is applied equally to
both DS3 MPPT-channel frames.

Stay careful with the DS3 result — it is **ours, not community-
corroborated**. patience4711's firmware sends a single DS3 frame with
`01` hardcoded, and the community has published no DS3 *total-output*
binding test, so upstream it is unconfirmed whether one frame caps one
channel or both. The test above is the only evidence that a single
`01` frame caps just its own channel — which is why the daemon sends
both. If a future capture shows one frame already caps the whole DS3,
revisit the dual-frame send.

*YC600 / QS1 — per-panel, like the DS3.* The `1C8C` throttle frame has
**no channel byte**: one frame covers the inverter. But the value is
**applied per MPPT-channel** — patience4711's throttling wiki states it
plainly ("set max power to 20 → each channel produces max 20 W → total
inverter 40 W"), and user `kiss81` (issue #42) independently confirmed
"20 watts per panel". So a request of *W* watts caps **each panel** at
~W; total AC output ≈ W × panel count.

**Binding test, 2026-05-22 — CONFIRMED.** yc600-1 (2 panels, ~378 W
natural) capped at 50 W: AC output collapsed to **~101 W, dead-flat for
7 minutes** while the yc600-2 and ds3 controls tracked their natural
output — unambiguously the cap, not a cloud. ~101 ≈ 2 × 50, a textbook
match to the per-panel model. Note the floor: `MIN_MAX_POWER_W` (50 W
for YC600) is a *per-panel* floor — the whole-inverter minimum for a
2-panel YC600 is ~2× that. Unlike the DS3, no per-channel frame is
needed: the single `1C8C` frame already covers both panels.

## F. Telemetry decode

`extractValue(start,len,slope,off)` = `slope * int(hex[start:start+len],16) + off`.
Offsets are **hex-char** offsets. ESP buffer = whole frame split on `44810000`
then `[30:]`; Python buffer = `whole_hex[38:]`. **Decode constants below are per
the ESP `s_d` buffer.**

### YC600 / QS1 (`invType` 0 / 1)

| Quantity | Formula (ESP `s_d`) |
|---|---|
| AC voltage | `extractValue(56,4) * (1/1.3277) / 4` V |
| AC frequency | `50000000 / extractValue(24,6)` Hz |
| Temperature | `extractValue(20,4,0.2752,-258.7)` °C |
| DC voltage ch1 | `(extractValue(48,2,16) + extractValue(46,1)) * 82.5/4096` V |
| DC voltage ch2 | `(extractValue(54,2,16) + extractValue(52,1)) * 82.5/4096` V |
| DC current ch1 | `(extractValue(47,1,256) + extractValue(44,2)) * 27.5/4096` A |
| DC current ch2 | `(extractValue(53,1,256) + extractValue(50,2)) * 27.5/4096` A |
| Energy | offset 74, +10/ch, width 6, `*8.311/3600` Wh — **ch0/ch1 swapped** |

DC channels use a 12-bit nibble-interleaved packing across 3 bytes — implement
as the two reads added, do not simplify. Per-panel watts = `V_dc * I_dc`.

### DS3 (`invType` 2) — dual-MPPT, plain big-endian

| Quantity | Formula (ESP `s_d`) |
|---|---|
| AC voltage | `extractValue(68,4) / 3.8` V |
| AC frequency | `extractValue(72,4) / 100` Hz |
| Temperature | `extractValue(96,4) * 0.0198 - 23.84` °C |
| DC voltage ch1/ch2 | `extractValue(52,4)/48`, `extractValue(56,4)/48` V |
| DC current ch1/ch2 | `extractValue(60,4)*0.0125`, `extractValue(64,4)*0.0125` A |
| Energy per ch | offset 100, +8/ch, width 8, `(raw/1000/100)*1.66` Wh |

Signal quality: `extractValue(14,2)*100/255` % (offset into post-`44810000` tail).

Inverter type is **not** auto-detected — it is configured per inverter; decode
branches on the stored `invType`.

## Open risks (verify on hardware)

1. App checksum algorithm unknown beyond set-power → replay constants.
2. `AF_REGISTER` payload + `2402` `DstAddrMode 0x0F` are non-canonical → replay.
3. `ZCD_NV_EXTADDR` may be locked on Z-Stack 3.x → fall back to real MAC.
4. Inverter-id endianness differs between references but cancels out — store
   as-seen-after-serial, transmit byte-reversed.
5. Whole bring-up depends on porting Home-1.2 Simple API to 3.x MT.
