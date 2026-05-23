/*
 * proto.h — host <-> firmware framed protocol for the APsystems bridge.
 *
 * The link is COBS-framed: every packet is COBS-encoded and terminated by a
 * single 0x00 delimiter byte. A decoded packet is [type:1][data...]. COBS
 * guarantees the payload never contains 0x00, so 0x00 unambiguously frames
 * packets even though the 802.15.4 frames carried are arbitrary binary.
 *
 * See firmware/PROTOCOL.md for the full spec. Keep this in sync with the
 * host side (host/aps_bridge/bridge.py).
 */
#ifndef APS_PROTO_H
#define APS_PROTO_H

/* firmware -> host */
#define PKT_RX_FRAME     0x01  /* [rssi:i8][corr:u8][channel:u8][802.15.4 MAC frame, no FCS] */
#define PKT_LOG          0x04  /* ASCII text (banners, errors) */
#define PKT_TX_DONE      0x05  /* [status:u8]  0 = TX ok, non-zero = failed */
#define PKT_INFO         0x07  /* [channel:u8][fw-version:u8][rx-status:u16-LE]
                                * reply to PKT_PING. rx-status is the RF core's
                                * CMD_IEEE_RX command status: 0x0002 (ACTIVE)
                                * means the receiver is armed and listening;
                                * any other value means RX is dead even though
                                * the MCU is alive. v5+ — older firmware sends
                                * only the first 2 bytes. */

/* host -> firmware */
#define PKT_TX_FRAME     0x02  /* [802.15.4 MAC frame to transmit, no FCS — radio appends it] */
#define PKT_SET_CHANNEL  0x03  /* [channel:u8]  (11..26) */
#define PKT_PING         0x06  /* (no data) -> firmware replies PKT_INFO */
#define PKT_SET_MAC_FILTER 0x08
    /* [mode:u8][pan:u16-LE][short:u16-LE][ieee:u64-LE]
     * mode = 0: promiscuous (default) — all other fields ignored
     * mode = 1: filtered + auto-ACK enabled. Radio accepts frames addressed to
     *           `short`, `ieee`, or any broadcast, on PAN `pan` (or any PAN if
     *           pan==0xFFFF), and ACKs them automatically per IEEE 802.15.4.
     *           Required to complete a Zigbee association handshake.
     */
#define PKT_SET_TX_POWER 0x09  /* [dbm:i8]  signed; must match a SmartRF table
                                * entry. Std-PA: -20..+5 dBm (v3+). High-PA:
                                * +6..+20 dBm (v4+). Values outside both tables
                                * are rejected via PKT_LOG (no PKT_TX_DONE). */

#define APS_FW_VERSION   5

#endif /* APS_PROTO_H */
