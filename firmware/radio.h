/*
 * radio.h — raw IEEE 802.15.4 RX/TX over the CC2652P RF core.
 *
 * Promiscuous receive (no address/PAN filtering, no auto-ACK) and
 * arbitrary-frame transmit. All protocol logic lives on the host; this is
 * just the radio.
 */
#ifndef RADIO_H
#define RADIO_H

#include <stdint.h>

/* Called from radio_service() for each received frame. `frame`/`len` are the
 * 802.15.4 MAC frame without the FCS; `rssi` in dBm; `corr` is the 6-bit
 * correlation/LQI value. */
typedef void (*radio_rx_cb_t)(const uint8_t *frame, uint16_t len,
                              int8_t rssi, uint8_t corr);

/* Power up the RF core, open the RF driver in IEEE 802.15.4 mode.
 * Returns 0 on success. */
int  radio_init(radio_rx_cb_t rx_cb);

/* Program the synthesizer to an IEEE channel (11..26). Returns 0 on success.
 * Stops RX if running — call radio_start_rx() afterwards. */
int  radio_set_channel(uint8_t channel);

/* (Re)start continuous promiscuous receive on the current channel. */
void radio_start_rx(void);

/* Transmit a raw 802.15.4 MAC frame (without FCS — the radio appends it).
 * Briefly suspends RX. Returns 0 on success. */
int  radio_tx(const uint8_t *frame, uint16_t len);

/* Drain the RX queue, invoking the rx_cb for each frame. Call from the
 * main loop. */
void radio_service(void);

/* The channel currently programmed. */
uint8_t radio_channel(void);

/* The RF core's CMD_IEEE_RX command status (raw 16-bit value). 0x0002
 * (ACTIVE) means the receiver is armed and listening; anything else means
 * RX is not running even if the MCU + USB link are fine. Surfaced in
 * PKT_INFO so the host watchdog can detect an "RF-deaf" dongle. */
uint16_t radio_rx_status(void);

/* Debug: dump RX status + counters as PKT_LOG packets. */
void radio_log_stats(void);

/* Switch RX to filtered + auto-ACK mode for a Zigbee join. Frames addressed to
 * `short_addr`, `ext_addr` (8-byte IEEE, LE bit order on wire), or any broadcast
 * on PAN `pan_id` (or any PAN if pan_id==0xFFFF) are received AND auto-ACKed
 * per IEEE 802.15.4. Pass pan_id=0 (and any other value) with filtered=0 to
 * return to promiscuous. Returns 0 on success.
 */
int  radio_set_filter(int filtered, uint16_t pan_id,
                      uint16_t short_addr, uint64_t ext_addr);

/* Set the radio's conducted TX power in dBm. Supported range: -20..+5 dBm
 * via the standard PA, +6..+20 dBm via the high PA (CC2652P front-end on
 * DIO28/29/30 — Sonoff ZBDongle-P wiring matches CC1352P-2 LaunchPad).
 * Returns 0 on success, -1 if the requested value isn't in either table. */
int  radio_set_tx_power(int8_t dbm);

#endif /* RADIO_H */
