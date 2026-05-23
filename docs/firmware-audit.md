# Firmware audit — 2026-05-20

Two independent reviews of the custom CC2652P raw 802.15.4 firmware
(`firmware/main.c`, `radio.c`, `uart_bridge.c`, `proto.h`, `ccfg.c`).
Findings consolidated below, in severity order. Each item has a clear
location, attack/failure scenario, and recommended fix. Nothing was
patched as part of this audit — fixes land in follow-up commits.

## Critical — fix before any release

### C-1  Unbounded `total` from the RF core enables OOB read

**File:** `firmware/radio.c:292-300`

```c
uint8_t  total = elem[0];
if (total >= RX_TRAILER && rx_callback != NULL) {
    uint16_t n    = (uint16_t)(total - RX_TRAILER);
    int8_t   rssi = (int8_t)elem[1 + n + 2];   // ← indexed by attacker-influenced n
    uint8_t  corr = elem[1 + n + 3] & 0x3F;
    rx_callback(&elem[1], n, rssi, corr);
}
```

`elem[0]` is the length byte the RF core writes into the rx queue entry.
It's only checked for the lower bound (`>= RX_TRAILER`), never the upper.
An anomalous frame (corrupt SRAM, attacker-relayed deliberately-malformed
frame, RF-core glitch under load) producing `total` in the 145–255 range
makes `n` reach 137–247. `elem[1 + n + 2]` then reads past the 144-byte
data area of `rfc_dataEntryGeneral_t` into adjacent static buffers.

Effect on this hardware: leak of adjacent static memory through the RSSI
byte to the host (no MPU enabled, no code-execution path). Still — a
straightforward OOB read driven by an unauthenticated over-the-air input.

**Fix:** before computing `n`, gate `total` at the data-area cap (=
`RX_BUF_SIZE - sizeof(rfc_dataEntryGeneral_t) - 1`, ≈ 135 bytes) and
discard otherwise. Also: independently check `1 + n + 3 < data_area_size`
in case the RSSI offset arithmetic is touched later.

### C-2  `RF_flushCmd` is non-blocking; command struct mutated mid-flight

**File:** `firmware/radio.c:148-157, 207-210`

`RF_flushCmd` posts a cancel request and returns immediately. The CM0+
RF coprocessor reads command structs (`rf_cmd_ieee_rx`, `rf_cmd_ieee_fs`)
directly from SRAM via the mailbox. The code then writes to those
structs without waiting for the cancellation to complete — genuine
TOCTOU between the ARM M4 and the CM0+. Same pattern in
`radio_set_filter()`.

**Fix:** after `RF_flushCmd`, poll the rx command's `status` field for a
terminal value (not `ACTIVE` / `PENDING`) with a small timeout before
mutating the struct. TI's reference pattern is `RF_cancelCmd` +
`RF_pendCmd(RF_WAIT_FOREVER)` with the RFC interrupt unmasked — make
sure the version that didn't deadlock under NoRTOS in earlier debug is
documented in the code with a comment, so it doesn't get "fixed" back.

## High — fix before declaring v1

### H-1  COBS decoder silently truncates on output overflow

**File:** `firmware/uart_bridge.c:60-81`

`cobs_decode()` returns the number of bytes written when the dst buffer
is full mid-group, indistinguishable from a successful decode. The
caller dispatches a partially-decoded buffer as a valid command (PKT
type taken from `dec[0]`, payload from `dec[1..di]`).

**Attack:** a host crafts an oversized COBS frame; the first 192 decoded
bytes are dispatched. If `dec[0]` is `PKT_TX_FRAME`, the radio
transmits attacker-chosen garbage. If `dec[0]` is `PKT_SET_MAC_FILTER`,
attacker-chosen filter values are accepted.

**Fix:** `cobs_decode` returns `UINT16_MAX` (or a separate `bool ok` out
param) on overflow; the caller drops the frame entirely.

### H-2  COBS RX overflow resets `rawlen` without resync

**File:** `firmware/uart_bridge.c:174-179`

```c
if (rawlen < sizeof(raw))
    raw[rawlen++] = (uint8_t)c;
else
    rawlen = 0;   /* overflow — drop the frame */
```

The overflow path resets `rawlen` immediately, so the next byte is
treated as the start of a fresh frame. Combined with H-1, this gives
an attacker reliable framing-slip injection: pad to overflow, then
emit a crafted COBS command starting at byte 0 with no preceding `0x00`
delimiter required.

**Fix:** on overflow, set a `framing_error` flag and consume all bytes
until the next `0x00` arrives. Only then resume accumulating.

### H-3  No 802.15.4 127-byte cap on TX

**File:** `firmware/radio.c:246-254`

`radio_tx()` allows `len` up to `sizeof(tx_buf)` (160). 802.15.4 PHY
caps PSDU at 127 bytes. Submitting 128–160 bytes to the RF core produces
illegal frames; the RF core's behaviour for over-spec lengths is
undefined.

**Fix:** add `if (len > 127) return -1;` at the top of `radio_tx`.

### H-4  `radio_start_rx()` called unconditionally after failed `radio_set_channel`

**File:** `firmware/main.c:60-64`

```c
case PKT_SET_CHANNEL:
    if (len >= 1) {
        radio_set_channel(data[0]);   // returns -1 on bad channel, ignored
        radio_start_rx();
    }
```

Bad channel (`<11` or `>26`) silently leaves the radio on the old
channel but the host now believes it's on the new one.

**Fix:**
```c
if (len >= 1 && radio_set_channel(data[0]) == 0)
    radio_start_rx();
// else: send PKT_TX_DONE/error code back to host
```

## Medium

### M-1  Missing `volatile` / DMB on rx entry status

**File:** `firmware/radio.c:292,303`

`rx_read_entry->status` is read and written without `volatile` and
without a memory barrier between the read and the `PENDING` write. The
RF driver SwiP updates the field; the compiler is free to cache the
load or reorder the store.

**Fix:** cast through `volatile *` on read, `__DMB()` between read and
write, declare `rx_running` and `rxCmdHandle` `volatile`.

### M-2  RX queue can starve host UART under flood

**Files:** `firmware/main.c:114-117`, `radio.c:287-306`

`radio_service()` drains all finished rx entries per main-loop tick,
each sending up to ~160 bytes over UART at 115200 baud (~14 ms each).
With `RX_BUF_CNT = 4` and a 250 frame/s flood, host commands are
effectively blocked for the duration of the attack.

**Fix:** cap frames-per-tick in `radio_service` at 1 or 2 so
`uart_bridge_service()` gets CPU. Also: encourage host to call
`PKT_SET_MAC_FILTER` once the PAN/addresses are known, so the RF core
filters most flood frames in hardware.

### M-3  `dec[192]` in hot-path stack frame

**File:** `firmware/uart_bridge.c:168`

The 192-byte `dec[]` decode buffer is allocated on every
`uart_bridge_service()` call. Inside that path the TI RF driver is
re-entered (`radio_tx` → `RF_runScheduleCmd`), which TI documents as
needing ~1 KB headroom of its own. Custom stack is 4 KB.

**Fix:** make `dec[]` `static` (no reentrancy concern under NoRTOS;
costs 192 bytes of BSS, gains 192 bytes of stack margin).

### M-4  `RF_runCmd` for CMD_FS only checks event mask, not status

**File:** `firmware/radio.c:161-164`

`rf_cmd_ieee_fs.status == DONE_OK` is never verified — a PLL lock
failure returns `RF_EventLastCmdDone` with a non-OK status and the
function reports success.

**Fix:** add `if (rf_cmd_ieee_fs.status != DONE_OK) return -1;`.

## Low / hardening

### L-1  Bootloader backdoor permanently enabled

**File:** `firmware/ccfg.c`

Production builds expose the ROM bootloader backdoor on DIO15. For a
field deployment, physical access plus a UART pin lets anyone reflash
the firmware without authentication.

**Fix:** make backdoor a build flag (`-DDEV_CCFG` for dev builds,
disabled in the default/release CCFG).

### L-2  `RF_EventRxBufFull` is subscribed but ignored

**Files:** `firmware/radio.c:86,190`

The rx callback is a no-op. Buffer-full events leave `nRxBufFull`
incrementing in stats, but `radio_log_stats()` is never called from
the main loop, so silent frame loss is invisible to the host.

**Fix:** in `rx_cb`, set a `volatile uint8_t rx_overrun` flag on
`RF_EventRxBufFull`; surface it from `radio_service` as a counter or
a PKT_LOG line.

### L-3  Magic offsets

**Files:** `firmware/main.c:38-40`, `firmware/uart_bridge.c:141`

`pkt[160]` in `on_rx_frame` is not derived from `MAX_PKT`. The label
loop bound `32` in `uart_bridge_log_u32` is not derived from
`sizeof(buf) - 13`. Both are correct today; both will become bugs the
first time someone adjusts a constant.

**Fix:** named constants, `static_assert` where applicable.

### L-4  `tx_buf` not reentrant-safe

**File:** `firmware/radio.c:62`

Static `tx_buf[160]` is fine under NoRTOS but a footgun if anyone ever
adds an ISR-driven TX path.

**Fix:** add a comment to the declaration documenting the constraint,
or wrap mutations in `HwiP_disable`/`restore`.

## Summary

| ID | Sev | File | Lines | Type | Fix difficulty |
|---|---|---|---|---|---|
| C-1 | Critical | radio.c | 292-300 | OOB read | Trivial (add `if`) |
| C-2 | Critical | radio.c | 148, 207 | TOCTOU vs RF core | Moderate (status poll loop) |
| H-1 | High | uart_bridge.c | 60-81 | Silent truncation | Trivial (sentinel) |
| H-2 | High | uart_bridge.c | 174-179 | Framing-slip injection | Trivial (flag + skip) |
| H-3 | High | radio.c | 246-254 | TX len > 127 | Trivial |
| H-4 | High | main.c | 60-64 | Failed set_channel ignored | Trivial |
| M-1 | Medium | radio.c | 292,303 | Memory ordering | Trivial (volatile + DMB) |
| M-2 | Medium | main.c, radio.c | several | RX flood DoS | Easy (cap per tick) |
| M-3 | Medium | uart_bridge.c | 168 | Stack pressure | Trivial (`static`) |
| M-4 | Medium | radio.c | 161-164 | FS status ignored | Trivial |
| L-1 | Low | ccfg.c | — | Bootloader backdoor | Easy (build flag) |
| L-2 | Low | radio.c | 86,190 | Silent frame loss | Easy (counter +log) |
| L-3 | Low | several | several | Magic constants | Trivial |
| L-4 | Low | radio.c | 62 | Reentrancy footgun | Comment-only |

**Verdict:** the firmware works correctly under the current load profile
(daemon polls 3 inverters every 30 s, no hostile RF environment). It is
*not* ready for a hostile network or for users running unattended
deployments without auth — the two Critical findings plus the four High
findings are exploitable from over-the-air or from a compromised host
process. Recommend tracking them as GitHub issues and gating any v1.0
tag on C-1 / C-2 fixes minimum.
