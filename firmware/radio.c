/*
 * radio.c — IEEE 802.15.4 RX/TX through the TI RF driver.
 *
 * Earlier iterations of this file drove the RF core bare-metal via the
 * doorbell directly (`RFCDoorbellSendTo`). That approach succeeded at the
 * command-acceptance level (CPE booted, CMD_RADIO_SETUP returned DONE_OK,
 * CMD_IEEE_RX went ACTIVE) but the receiver never demodulated anything —
 * RSSI stayed at noise floor across all 16 channels, counters all zero.
 * The TI RF driver does more than command submission: it negotiates Power
 * domains, drives the antenna switch via a `globalCallback`, handles the
 * RFC interrupt for buffer/end events, and times calibrations. Replicating
 * all of that bare-metal turned out to be more work than just using the
 * driver, especially since SysConfig 1.21.1 emits
 * the matching driver/board config for us.
 *
 * This file drives RX via the TI RF driver: `RF_open()` for setup,
 * `RF_postCmd()` to start continuous RX, and an RX callback that fires on
 * `RF_EventRxEntryDone` per received frame.
 */

#include "radio.h"
#include "ti_radio_config.h"
#include "uart_bridge.h"

#include <stddef.h>
#include <string.h>

#include <ti/drivers/rf/RF.h>

#include <ti/devices/DeviceFamily.h>
#include DeviceFamily_constructPath(driverlib/rf_mailbox.h)
#include DeviceFamily_constructPath(driverlib/rf_common_cmd.h)
#include DeviceFamily_constructPath(driverlib/rf_ieee_cmd.h)
#include DeviceFamily_constructPath(driverlib/rf_ieee_mailbox.h)
#include DeviceFamily_constructPath(driverlib/rf_data_entry.h)

/* RX data queue — 4 circular entries, 152 bytes each. Plenty for any
 * 127-byte 802.15.4 frame plus the 8-byte trailer the RX command appends. */
#define RX_BUF_CNT    4
#define RX_BUF_SIZE   152
#define RX_TRAILER    8   /* FCS(2)+RSSI(1)+corr/status(1)+timestamp(4) */

static radio_rx_cb_t rx_callback;
static uint8_t       cur_channel;
static int           rx_running;

static RF_Handle  rfHandle;
static RF_Object  rfObject;
static RF_CmdHandle rxCmdHandle = -1;

static rfc_ieeeRxOutput_t rx_stats;

typedef union {
    rfc_dataEntryGeneral_t entry;
    uint8_t                raw[RX_BUF_SIZE];
} rx_buf_t;

static rx_buf_t  rx_bufs[RX_BUF_CNT] __attribute__((aligned(4)));
static dataQueue_t rx_queue;
static rfc_dataEntryGeneral_t *rx_read_entry;

static uint8_t tx_buf[160] __attribute__((aligned(4)));

/* ----- helpers ------------------------------------------------------- */

static void rx_queue_init(void)
{
    int i;
    for (i = 0; i < RX_BUF_CNT; i++) {
        rfc_dataEntryGeneral_t *e = &rx_bufs[i].entry;
        e->status       = DATA_ENTRY_PENDING;
        e->config.type  = DATA_ENTRY_TYPE_GEN;
        e->config.lenSz = 1;
        e->length       = RX_BUF_SIZE - sizeof(rfc_dataEntryGeneral_t);
        e->pNextEntry   = (uint8_t *)&rx_bufs[(i + 1) % RX_BUF_CNT].entry;
    }
    rx_queue.pCurrEntry = (uint8_t *)&rx_bufs[0].entry;
    rx_queue.pLastEntry = NULL;   /* circular */
    rx_read_entry = &rx_bufs[0].entry;
}

/* RF driver RX callback. Runs in SwiP context — keep it short; defer the
 * actual frame-out-to-host work to radio_service() in the main loop. */
static void rx_cb(RF_Handle h, RF_CmdHandle ch, RF_EventMask events)
{
    (void)h; (void)ch; (void)events;
    /* No-op: radio_service() polls the data queue directly. The callback
     * is here mostly so the driver knows the app cares about RX events. */
}

/* ----- API ----------------------------------------------------------- */

int radio_init(radio_rx_cb_t rx_cb_in)
{
    RF_Params rfParams;

    rx_callback = rx_cb_in;
    cur_channel = 11;
    rx_running  = 0;

    rx_queue_init();

    /* Wire the RX command to our queue and stats output. Keep promiscuous
     * settings (frameFiltEn=0, autoAckEn=0). The data-entry-done event
     * mask is what fires our rxCallback. */
    rf_cmd_ieee_rx.pRxQ      = &rx_queue;
    rf_cmd_ieee_rx.pOutput   = &rx_stats;
    rf_cmd_ieee_rx.channel   = cur_channel;
    rf_cmd_ieee_rx.frameFiltOpt.frameFiltEn = 0;
    rf_cmd_ieee_rx.frameFiltOpt.autoAckEn   = 0;
    /* Continuous RX — only end on explicit cancel. */
    rf_cmd_ieee_rx.endTrigger.triggerType   = TRIG_NEVER;
    rf_cmd_ieee_rx.endTime                  = 0;

    /* Default channel frequency for CMD_FS — 2405 MHz (ch 11). */
    rf_cmd_ieee_fs.frequency = 2405;
    rf_cmd_ieee_fs.fractFreq = 0;

    RF_Params_init(&rfParams);
    /* nInactivityTimeout = BIOS_WAIT_FOREVER keeps the RFC powered between
     * commands. The driver still re-runs CMD_RADIO_SETUP on wake, but we
     * don't sleep, so this is mostly belt-and-braces. */
    rfParams.nInactivityTimeout = ~0U;

    uart_bridge_log("radio: RF_open...");
    rfHandle = RF_open(&rfObject, &rf_ieee_mode,
                       (RF_RadioSetup *)&rf_cmd_ieee_radio_setup, &rfParams);
    if (rfHandle == NULL) {
        uart_bridge_log("ERROR: RF_open returned NULL");
        return -1;
    }
    uart_bridge_log("radio: RF_open ok");

    return 0;
}

int radio_set_channel(uint8_t channel)
{
    RF_EventMask st;
    uint32_t mhz;

    if (channel < 11 || channel > 26 || rfHandle == NULL)
        return -1;

    /* CMD_FS reprograms the synth — RX must be quiesced first or it would
     * keep running on the old frequency. RF_flushCmd is non-blocking; the
     * earlier RF_cancelCmd + RF_pendCmd combo deadlocked under NoRTOS. */
    if (rx_running && rxCmdHandle >= 0) {
        RF_flushCmd(rfHandle, rxCmdHandle, 0);
        rx_running  = 0;
        rxCmdHandle = -1;
    }

    mhz = 2405u + 5u * (uint32_t)(channel - 11);
    rf_cmd_ieee_fs.frequency = (uint16_t)mhz;
    rf_cmd_ieee_fs.fractFreq = 0;
    rf_cmd_ieee_rx.channel   = channel;
    cur_channel = channel;

    /* Synchronously run CMD_FS. */
    st = RF_runCmd(rfHandle, (RF_Op *)&rf_cmd_ieee_fs, RF_PriorityNormal, NULL, 0);
    if ((st & RF_EventLastCmdDone) == 0) {
        uart_bridge_log_u32("radio: FS event mask", (uint32_t)st);
        return -1;
    }
    return 0;
}

void radio_start_rx(void)
{
    RF_ScheduleCmdParams rxSched;

    if (rfHandle == NULL || rx_running)
        return;

    /* IEEE 802.15.4 RX must sit in the RF core's BACKGROUND slot so that
     * subsequent CMD_IEEE_TX commands can slot into FOREGROUND alongside
     * it. RF_postCmd doesn't understand FG/BG and would let the infinite
     * RX block any TX behind it. RF_scheduleCmd is the FG/BG-aware API. */
    rf_cmd_ieee_rx.status = IDLE;

    RF_ScheduleCmdParams_init(&rxSched);
    rxSched.allowDelay = RF_AllowDelayAny;
    rxSched.endTime    = 0;
    rxSched.startTime  = 0;
    rxSched.duration   = 0;

    rxCmdHandle = RF_scheduleCmd(rfHandle, (RF_Op *)&rf_cmd_ieee_rx,
                                 &rxSched, rx_cb,
                                 RF_EventRxEntryDone | RF_EventRxOk | RF_EventRxNOk);
    if (rxCmdHandle < 0) {
        uart_bridge_log_u32("radio: RF_scheduleCmd RX failed", (uint32_t)rxCmdHandle);
        return;
    }
    rx_running = 1;
    uart_bridge_log_u32("radio: RX posted, handle", (uint32_t)rxCmdHandle);
}

int radio_set_filter(int filtered, uint16_t pan_id,
                     uint16_t short_addr, uint64_t ext_addr)
{
    if (rfHandle == NULL)
        return -1;

    /* Stop RX so we can mutate the command struct. */
    if (rx_running && rxCmdHandle >= 0) {
        RF_flushCmd(rfHandle, rxCmdHandle, 0);
        rx_running  = 0;
        rxCmdHandle = -1;
    }

    if (filtered) {
        rf_cmd_ieee_rx.frameFiltOpt.frameFiltEn = 0x1;
        rf_cmd_ieee_rx.frameFiltOpt.autoAckEn   = 0x1;
        /* Reply to Data Requests with frame-pending bit based on per-device
         * indirect transaction queue. We don't have one, so leave default. */
        rf_cmd_ieee_rx.frameFiltOpt.autoPendEn  = 0x0;
        rf_cmd_ieee_rx.localPanID    = pan_id;
        rf_cmd_ieee_rx.localShortAddr = short_addr;
        rf_cmd_ieee_rx.localExtAddr  = ext_addr;
        uart_bridge_log_u32("filter: pan", pan_id);
        uart_bridge_log_u32("filter: short", short_addr);
    } else {
        rf_cmd_ieee_rx.frameFiltOpt.frameFiltEn = 0x0;
        rf_cmd_ieee_rx.frameFiltOpt.autoAckEn   = 0x0;
        uart_bridge_log("filter: promiscuous");
    }

    radio_start_rx();
    return 0;
}

void radio_log_stats(void)
{
    uint32_t nRxOk = (uint32_t)rx_stats.nRxBeacon + rx_stats.nRxData
                   + rx_stats.nRxAck + rx_stats.nRxMacCmd + rx_stats.nRxReserved;

    uart_bridge_log_u32("rx.status",  rf_cmd_ieee_rx.status);
    uart_bridge_log_u32("ch",         (uint32_t)cur_channel);
    uart_bridge_log_u32("nRxOk",      nRxOk);
    uart_bridge_log_u32("nRxNok",     (uint32_t)rx_stats.nRxNok);
    uart_bridge_log_u32("nRxIgnored", (uint32_t)rx_stats.nRxIgnored);
    uart_bridge_log_u32("nRxBufFull", (uint32_t)rx_stats.nRxBufFull);
}

/* Set the RF driver's current TX power. Searches the std-PA table first
 * (-20..+5 dBm), then the high-PA table (+6..+20 dBm); RF_setTxPower() then
 * routes the front-end via pRegOverrideTxStd / pRegOverrideTx20 as needed.
 * Returns 0 on success, -1 if `dbm` isn't in either table or the driver
 * rejected the value. */
int radio_set_tx_power(int8_t dbm)
{
    extern RF_TxPowerTable_Entry txPowerTable_2400_pa5[];
    extern RF_TxPowerTable_Entry txPowerTable_2400_pa20[];
    RF_TxPowerTable_Value v;
    RF_Stat st;

    if (rfHandle == NULL)
        return -1;

    v = RF_TxPowerTable_findValue(txPowerTable_2400_pa5, dbm);
    if (v.rawValue == RF_TxPowerTable_INVALID_VALUE)
        v = RF_TxPowerTable_findValue(txPowerTable_2400_pa20, dbm);
    if (v.rawValue == RF_TxPowerTable_INVALID_VALUE) {
        uart_bridge_log_u32("tx_power reject (dbm out of table)", (uint32_t)dbm);
        return -1;
    }
    st = RF_setTxPower(rfHandle, v);
    if (st != RF_StatSuccess) {
        uart_bridge_log_u32("tx_power RF_setTxPower failed st", (uint32_t)st);
        return -1;
    }
    uart_bridge_log_u32("tx_power set (dbm)", (uint32_t)dbm);
    return 0;
}

int radio_tx(const uint8_t *frame, uint16_t len)
{
    RF_EventMask st;
    RF_ScheduleCmdParams txSched;

    /* H-3: IEEE 802.15.4 PHY caps PSDU at 127 bytes. Reject anything
     * larger; the RF core's behaviour on over-spec lengths is undefined. */
    if (rfHandle == NULL || len == 0 || len > 127 || len > sizeof(tx_buf))
        return -1;

    memcpy(tx_buf, frame, len);

    rf_cmd_ieee_tx.payloadLen      = (uint8_t)len;
    rf_cmd_ieee_tx.pPayload        = tx_buf;
    rf_cmd_ieee_tx.condition.rule  = COND_NEVER;
    rf_cmd_ieee_tx.pNextOp         = NULL;
    rf_cmd_ieee_tx.startTrigger.triggerType = TRIG_NOW;
    rf_cmd_ieee_tx.status          = IDLE;

    /* CMD_IEEE_TX is a FOREGROUND operation; the RF core runs it in the FG
     * slot alongside the continuous BG CMD_IEEE_RX. RF_scheduleCmd is the
     * FG/BG-aware API — RF_runCmd / RF_postCmd would queue this behind the
     * infinite RX and never run it (confirmed bug per TI E2E thread "CC1352P
     * RF_runCmd & RF_postCmd w/ RF_cmdIeeeTx lock up the radio"). Wait on
     * RF_EventLastFGCmdDone — not RF_EventLastCmdDone — because BG RX
     * deliberately never completes. */
    RF_ScheduleCmdParams_init(&txSched);
    txSched.allowDelay = RF_AllowDelayAny;
    txSched.endTime    = 0;
    txSched.startTime  = 0;
    txSched.duration   = 0;

    st = RF_runScheduleCmd(rfHandle, (RF_Op *)&rf_cmd_ieee_tx, &txSched,
                           NULL, RF_EventLastFGCmdDone);

    if (!(st & RF_EventLastFGCmdDone) || rf_cmd_ieee_tx.status != IEEE_DONE_OK) {
        uart_bridge_log_u32("tx fail evt", (uint32_t)st);
        uart_bridge_log_u32("tx fail st",  (uint32_t)rf_cmd_ieee_tx.status);
        return -1;
    }
    return 0;
}

void radio_service(void)
{
    if (rfHandle == NULL)
        return;

    while (rx_read_entry->status == DATA_ENTRY_FINISHED) {
        uint8_t *elem  = (uint8_t *)&rx_read_entry->data;
        uint8_t  total = elem[0];

        /* C-1: cap `total` at the entry data area before computing offsets.
         * The RF core writes `total` as the length byte; under load or a
         * corrupted entry, it can exceed the buffer. Anything above the
         * 802.15.4 PHY max (127) + RX_TRAILER is also impossible by spec. */
        #define RX_DATA_AREA  (RX_BUF_SIZE - (uint16_t)sizeof(rfc_dataEntryGeneral_t))
        #define RX_TOTAL_MAX  ((uint16_t)(127 + RX_TRAILER))
        if (total >= RX_TRAILER && total <= RX_TOTAL_MAX &&
            (uint16_t)(1 + (total - RX_TRAILER) + 3) < RX_DATA_AREA &&
            rx_callback != NULL) {
            uint16_t n    = (uint16_t)(total - RX_TRAILER);
            int8_t   rssi = (int8_t)elem[1 + n + 2];
            uint8_t  corr = elem[1 + n + 3] & 0x3F;
            rx_callback(&elem[1], n, rssi, corr);
        }

        rx_read_entry->status = DATA_ENTRY_PENDING;
        rx_read_entry = (rfc_dataEntryGeneral_t *)rx_read_entry->pNextEntry;
    }
}

uint8_t radio_channel(void)
{
    return cur_channel;
}

uint16_t radio_rx_status(void)
{
    /* The RF core updates rf_cmd_ieee_rx.status in place as the CMD_IEEE_RX
     * command runs. ACTIVE (0x0002) = receiver armed. After radio_start_rx()
     * succeeds it should stay ACTIVE indefinitely (endTrigger=TRIG_NEVER);
     * any other value means the RX command aborted or never started — the
     * "RF-deaf" state the host watchdog needs to catch. */
    return (uint16_t)rf_cmd_ieee_rx.status;
}
