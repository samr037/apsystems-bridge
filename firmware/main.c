/*
 * APsystems open bridge — CC2652P raw-IEEE-802.15.4 firmware.
 *
 * A deliberately dumb USB-CDC <-> raw-802.15.4 bridge: promiscuous receive
 * and arbitrary-frame transmit. All higher-layer protocol (the unencrypted
 * Zigbee MAC/NWK/APS frames, the APsystems pairing/poll handshake) lives on
 * the host. This firmware just moves frames.
 *
 * Board: Sonoff ZBDongle-P (TI CC2652P). UART0 TX=DIO13 / RX=DIO12 — the
 * CP2102N bridge, so the host talks to it over /dev/ttyUSB0.
 *
 * Host link: COBS-framed packets — see proto.h / PROTOCOL.md.
 * RF: TI RF driver in IEEE 802.15.4 mode, configured from SysConfig 1.21.1
 * output for the CC1352P_2 LaunchPad (the reference board the Sonoff PCB
 * clones). See radio.c / ti_radio_config.c / ti_drivers_config.c.
 *
 * See BUILD.md for the build recipe.
 */

#include <stdint.h>
#include <string.h>

#include <NoRTOS.h>
#include <ti/drivers/dpl/ClockP.h>

#include "proto.h"
#include "radio.h"
#include "ti_drivers_config.h"
#include "uart_bridge.h"

/* IEEE channel the bridge starts on; the host changes it with PKT_SET_CHANNEL. */
#define DEFAULT_CHANNEL   11

/* A received 802.15.4 frame -> PKT_RX_FRAME packet to the host. */
static void on_rx_frame(const uint8_t *frame, uint16_t len,
                        int8_t rssi, uint8_t corr)
{
    static uint8_t pkt[160];

    if ((uint32_t)len + 3 > sizeof(pkt))
        return;

    pkt[0] = (uint8_t)rssi;
    pkt[1] = corr;
    pkt[2] = radio_channel();
    memcpy(&pkt[3], frame, len);

    uart_bridge_send(PKT_RX_FRAME, pkt, (uint16_t)(len + 3));
}

/* A command packet from the host. */
static void on_command(uint8_t type, const uint8_t *data, uint16_t len)
{
    switch (type) {
    case PKT_TX_FRAME: {
        uint8_t status = (radio_tx(data, len) == 0) ? 0u : 1u;
        uart_bridge_send(PKT_TX_DONE, &status, 1);
        break;
    }
    case PKT_SET_CHANNEL:
        /* H-4: only restart RX if the channel was actually accepted. A bad
         * channel byte (<11 or >26) used to silently leave the radio on
         * the previous channel while the host thought it had moved.
         * Report via PKT_LOG so the host log captures it; don't reuse
         * PKT_TX_DONE (that has its own meaning for the TX command). */
        if (len >= 1) {
            if (radio_set_channel(data[0]) == 0) {
                radio_start_rx();
            } else {
                uart_bridge_log_u32("set_channel reject", (uint32_t)data[0]);
            }
        }
        break;
    case PKT_PING: {
        /* Reply carries RX-core health so the host watchdog can tell a
         * genuinely-listening dongle from one whose MCU answers but whose
         * RF core RX has died. rx-status is u16 little-endian. */
        uint16_t rxst = radio_rx_status();
        uint8_t info[4] = { radio_channel(), APS_FW_VERSION,
                            (uint8_t)(rxst & 0xFF),
                            (uint8_t)((rxst >> 8) & 0xFF) };
        uart_bridge_send(PKT_INFO, info, 4);
        break;
    }
    case PKT_SET_MAC_FILTER: {
        /* [mode:u8][pan:u16-LE][short:u16-LE][ieee:u64-LE] */
        if (len < 13) break;
        uint8_t  mode  = data[0];
        uint16_t pan   = (uint16_t)(data[1] | (data[2] << 8));
        uint16_t sh    = (uint16_t)(data[3] | (data[4] << 8));
        uint64_t ext   = 0;
        for (int i = 0; i < 8; i++)
            ext |= ((uint64_t)data[5 + i]) << (8 * i);
        radio_set_filter(mode != 0, pan, sh, ext);
        break;
    }
    case PKT_SET_TX_POWER:
        /* [dbm:i8] — signed dBm. Out-of-table values are rejected by the
         * radio layer; rejection is logged via PKT_LOG so the host log
         * captures it. No PKT_TX_DONE reply (that opcode has its own
         * meaning for PKT_TX_FRAME). */
        if (len >= 1)
            radio_set_tx_power((int8_t)data[0]);
        break;
    default:
        break;
    }
}

int main(void)
{
    /* NoRTOS DPL first — Power / GPIO / RF drivers all depend on it. */
    NoRTOS_start();

    /* Board_init() calls Power_init() and GPIO_init(). Both must run
     * before RF_open(); the RF driver's setup callback uses the GPIO
     * pinmux helpers to configure the antenna-switch DIOs. */
    Board_init();

    uart_bridge_init(on_command);
    uart_bridge_log("APsystems bridge - CC2652P raw-802.15.4 firmware");
    uart_bridge_log("boot: radio_init...");
    if (radio_init(on_rx_frame) != 0) {
        uart_bridge_log("ERROR: radio_init failed");
        for (;;)
            uart_bridge_service();
    }
    uart_bridge_log("boot: radio_init ok; set_channel...");
    radio_set_channel(DEFAULT_CHANNEL);
    uart_bridge_log("boot: set_channel ok; start_rx...");
    radio_start_rx();
    uart_bridge_log("radio up - promiscuous RX running");

    /* Steady-state: forward RX frames to the host, take commands from it.
     * No auto-sweep — the host chooses the channel via PKT_SET_CHANNEL. */
    for (;;) {
        radio_service();         /* drain RX queue -> host */
        uart_bridge_service();   /* host commands -> radio */
    }
}
