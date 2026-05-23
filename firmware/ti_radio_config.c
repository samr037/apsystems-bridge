/*
 * ti_radio_config.c — IEEE 802.15.4 RF core configuration for the CC2652P.
 *
 * Ported from Contiki-NG's open cc26x2 RF settings
 * (arch/cpu/simplelink-cc13xx-cc26xx/rf-settings/cc26x2/ieee-settings.c,
 * Copyright (c) Texas Instruments / Contiki-NG, BSD-3-Clause). Replaces the
 * SysConfig-generated RF config.
 *
 * Parameter summary: 2.4 GHz O-QPSK 250 kbps IEEE 802.15.4. Several fields
 * (frequency, channel, txPower, frameFiltOpt, pRxQ, pPayload...) are
 * placeholders here and are filled in at runtime by radio.c.
 */

#include "ti_radio_config.h"

#include <ti/devices/DeviceFamily.h>
#include DeviceFamily_constructPath(driverlib/rf_mailbox.h)
#include DeviceFamily_constructPath(driverlib/rf_common_cmd.h)
#include DeviceFamily_constructPath(driverlib/rf_ieee_cmd.h)
#include DeviceFamily_constructPath(rf_patches/rf_patch_cpe_ieee_802_15_4.h)
#include <ti/drivers/rf/RF.h>

/* TX power tables for 2.4 GHz, CC2652P. Two paths:
 *
 *   - txPowerTable_2400_pa5[]  : default-PA, -20..+5 dBm (SmartRF Studio).
 *   - txPowerTable_2400_pa20[] : high-PA,    +6..+20 dBm (SmartRF Studio,
 *                                IEEE 802.15.4 @ CC1352P-2 / CC2652P).
 *
 * The Sonoff ZBDongle-P PCB clones the CC1352P-2 LaunchPad antenna routing
 * (high-PA tied to DIO28/29/30 RF switch), so the high-PA path works without
 * board changes. EU SRD ceiling is +20 dBm EIRP — that's the legal max in
 * this band; staying at or below makes us compliant.
 *
 * Used at runtime by radio_set_tx_power() in radio.c: it searches the std
 * table first, then the high-PA table, and calls RF_setTxPower() with
 * whichever entry matched. The PA struct below exposes both override arrays
 * (pRegOverrideTxStd / pRegOverrideTx20) so the RF driver can hot-swap the
 * front-end routing when crossing the +5/+6 boundary. */
RF_TxPowerTable_Entry txPowerTable_2400_pa5[] = {
    { -20, RF_TxPowerTable_DEFAULT_PA_ENTRY( 0, 3, 0,  2) },
    { -15, RF_TxPowerTable_DEFAULT_PA_ENTRY( 1, 3, 0,  3) },
    { -10, RF_TxPowerTable_DEFAULT_PA_ENTRY( 2, 3, 0,  5) },
    {  -5, RF_TxPowerTable_DEFAULT_PA_ENTRY( 4, 3, 0,  5) },
    {   0, RF_TxPowerTable_DEFAULT_PA_ENTRY( 8, 3, 0, 10) },
    {   1, RF_TxPowerTable_DEFAULT_PA_ENTRY( 9, 3, 0, 11) },
    {   2, RF_TxPowerTable_DEFAULT_PA_ENTRY(10, 3, 0, 11) },
    {   3, RF_TxPowerTable_DEFAULT_PA_ENTRY(11, 3, 0, 11) },
    {   4, RF_TxPowerTable_DEFAULT_PA_ENTRY(13, 3, 0, 13) },
    {   5, RF_TxPowerTable_DEFAULT_PA_ENTRY(14, 3, 0, 14) },
    RF_TxPowerTable_TERMINATION_ENTRY
};

RF_TxPowerTable_Entry txPowerTable_2400_pa20[] = {
    {  6, RF_TxPowerTable_HIGH_PA_ENTRY( 5, 0, 0,  4, 0) },
    {  7, RF_TxPowerTable_HIGH_PA_ENTRY( 8, 0, 0,  5, 0) },
    {  8, RF_TxPowerTable_HIGH_PA_ENTRY( 9, 0, 0,  6, 0) },
    {  9, RF_TxPowerTable_HIGH_PA_ENTRY( 9, 0, 0,  7, 0) },
    { 10, RF_TxPowerTable_HIGH_PA_ENTRY(10, 0, 0,  8, 0) },
    { 11, RF_TxPowerTable_HIGH_PA_ENTRY(13, 0, 0,  9, 0) },
    { 12, RF_TxPowerTable_HIGH_PA_ENTRY(15, 0, 0, 10, 0) },
    { 13, RF_TxPowerTable_HIGH_PA_ENTRY(18, 0, 0, 11, 0) },
    { 14, RF_TxPowerTable_HIGH_PA_ENTRY(20, 0, 0, 13, 0) },
    { 15, RF_TxPowerTable_HIGH_PA_ENTRY(22, 0, 0, 15, 0) },
    { 16, RF_TxPowerTable_HIGH_PA_ENTRY(25, 0, 0, 17, 0) },
    { 17, RF_TxPowerTable_HIGH_PA_ENTRY(29, 0, 0, 21, 0) },
    { 18, RF_TxPowerTable_HIGH_PA_ENTRY(34, 0, 0, 26, 0) },
    { 19, RF_TxPowerTable_HIGH_PA_ENTRY(41, 0, 0, 35, 0) },
    { 20, RF_TxPowerTable_HIGH_PA_ENTRY(57, 0, 0, 64, 0) },
    RF_TxPowerTable_TERMINATION_ENTRY
};

/* RF Mode object — IEEE-specific CPE patch (matches what TI's SmartRF
 * config ships for cc2652p_ieee_15_4_pg21). multi_protocol was tried first
 * but the bare-metal RX wouldn't pick up anything; switching to the
 * IEEE-specific patch is the recommended config for IEEE 802.15.4 on
 * CC2652P PG2.1. */
RF_Mode rf_ieee_mode = {
    .rfMode      = RF_MODE_AUTO,
    .cpePatchFxn = &rf_patch_cpe_ieee_802_15_4,
    .mcePatchFxn = 0,
    .rfePatchFxn = 0,
};

/* Register overrides for CMD_RADIO_SETUP (from TI's override_ieee_802_15_4).
 * The 0xFFFFFFFF terminator is mandatory — the RF core walks the table. */
static uint32_t rf_ieee_overrides[] = {
    /* DC/DC regulator: in Tx use DCDCCTL5[3:0]=0x3 (DITHER_EN=0, IPEAK=3). */
    (uint32_t)0x00F388D3,
    /* Rx: set LNA bias current offset to +15 (saturate trim to max). */
    (uint32_t)0x000F8883,
    (uint32_t)0xFFFFFFFF,
};

/* PA-path-specific overrides — applied by the RF driver depending on whether
 * the active TX power entry came from txPowerTable_2400_pa5[] (std PA) or
 * txPowerTable_2400_pa20[] (high PA). Values are SmartRF Studio output for
 * IEEE 802.15.4 @ CC1352P-2 LaunchPad, which is the reference design the
 * Sonoff ZBDongle-P PCB clones — antenna front-end on DIO28/29/30. */
static uint32_t rf_ieee_overrides_tx_std[] = {
    TX_STD_POWER_OVERRIDE(0x7217),
    /* ANADIV: loDivider=0, frontEndMode=diff, standard-PA path. */
    (uint32_t)0x82A86C2B,
    (uint32_t)0xFFFFFFFF,
};

static uint32_t rf_ieee_overrides_tx_20[] = {
    TX20_POWER_OVERRIDE(0x003F75F5),
    /* ANADIV: loDivider=0, frontEndMode=diff, high-PA path enabled. */
    (uint32_t)0x82A8E92B,
    (uint32_t)0xFFFFFFFF,
};

/* CMD_RADIO_SETUP_PA — the PA-aware variant the CPE expects on CC2652P /
 * CC1352P silicon (same opcode 0x0802, larger struct). Values below are
 * exactly what SysConfig 1.21.1 generates for the CC1352P-2 LaunchPad
 * (which is the reference design the Sonoff ZBDongle-P PCB clones).
 *
 * Two material differences vs our earlier hand-rolled CMD_RADIO_SETUP_t:
 *   - struct type: PA variant has pRegOverrideTxStd / pRegOverrideTx20 /
 *     bSynthNarrowBand fields after pRegOverride
 *   - config.biasMode = 0x1 (external bias) — required on the LaunchPad /
 *     Sonoff PCB; our previous 0x0 (internal bias) was the bug behind the
 *     silent receiver.  */
rfc_CMD_RADIO_SETUP_PA_t rf_cmd_ieee_radio_setup = {
    .commandNo                = 0x0802,      /* CMD_RADIO_SETUP[_PA] */
    .status                   = IDLE,
    .pNextOp                  = 0,
    .startTime                = 0x00000000,
    .startTrigger.triggerType = TRIG_NOW,
    .startTrigger.bEnaCmd     = 0x0,
    .startTrigger.triggerNo   = 0x0,
    .startTrigger.pastTrig    = 0x0,
    .condition.rule           = COND_NEVER,
    .condition.nSkip          = 0x0,
    .mode                     = 0x01,        /* 0x01 = IEEE 802.15.4 */
    .loDivider                = 0x00,
    .config.frontEndMode      = 0x0,         /* differential */
    .config.biasMode          = 0x1,         /* external bias (LaunchPad/Sonoff) */
    .config.analogCfgMode     = 0x0,
    .config.bNoFsPowerUp      = 0x0,
    .config.bSynthNarrowBand  = 0x0,
    .txPower                  = 0x7217,      /* 5 dBm default; overridden by CMD on TX */
    .pRegOverride             = rf_ieee_overrides,
    .pRegOverrideTxStd        = rf_ieee_overrides_tx_std,
    .pRegOverrideTx20         = rf_ieee_overrides_tx_20,
};

/* CMD_FS — synthesizer programming. frequency/fractFreq set per channel. */
rfc_CMD_FS_t rf_cmd_ieee_fs = {
    .commandNo                = CMD_FS,
    .status                   = IDLE,
    .pNextOp                  = 0,
    .startTime                = 0x00000000,
    .startTrigger.triggerType = TRIG_NOW,
    .startTrigger.bEnaCmd     = 0x0,
    .startTrigger.triggerNo   = 0x0,
    .startTrigger.pastTrig    = 0x0,
    .condition.rule           = COND_NEVER,
    .condition.nSkip          = 0x0,
    .frequency                = 0x0965,     /* placeholder (2405 MHz) */
    .fractFreq                = 0x0000,
    .synthConf.bTxMode        = 0x1,
    .synthConf.refFreq        = 0x0,
};

/* CMD_IEEE_TX — transmit. payloadLen/pPayload set per transmission.
 * bIncludeCrc = 0: the RF core computes and appends the 2-byte FCS.
 * bIncludePhyHdr = 0: the RF core generates the PHY header. */
rfc_CMD_IEEE_TX_t rf_cmd_ieee_tx = {
    .commandNo                = CMD_IEEE_TX,
    .status                   = IDLE,
    .pNextOp                  = 0,
    .startTime                = 0x00000000,
    .startTrigger.triggerType = TRIG_NOW,
    .startTrigger.bEnaCmd     = 0x0,
    .startTrigger.triggerNo   = 0x0,
    .startTrigger.pastTrig    = 0x0,
    .condition.rule           = COND_NEVER,
    .condition.nSkip          = 0x0,
    .txOpt.bIncludePhyHdr     = 0x0,
    .txOpt.bIncludeCrc        = 0x0,
    .txOpt.payloadLenMsb      = 0x0,
    .payloadLen               = 0x0,        /* set per TX */
    .pPayload                 = 0,          /* set per TX */
    .timeStamp                = 0x00000000,
};

/* CMD_IEEE_RX — continuous promiscuous receive.
 * frameFiltOpt.frameFiltEn / autoAckEn / pRxQ / pOutput / channel are set
 * by radio_init(); endTrigger TRIG_NEVER keeps RX running until cancelled.
 * rxConfig appends, per element: FCS(2) + RSSI(1) + corr/status(1) + TS(4). */
rfc_CMD_IEEE_RX_t rf_cmd_ieee_rx = {
    .commandNo                  = CMD_IEEE_RX,
    .status                     = IDLE,
    .pNextOp                    = 0,
    .startTime                  = 0x00000000,
    .startTrigger.triggerType   = TRIG_NOW,
    .startTrigger.bEnaCmd       = 0x0,
    .startTrigger.triggerNo     = 0x0,
    .startTrigger.pastTrig      = 0x0,
    .condition.rule             = COND_NEVER,
    .condition.nSkip            = 0x0,
    .channel                    = 0x00,     /* set by radio_set_channel() */
    .rxConfig.bAutoFlushCrc     = 0x1,
    .rxConfig.bAutoFlushIgn     = 0x1,
    .rxConfig.bIncludePhyHdr    = 0x0,
    .rxConfig.bIncludeCrc       = 0x1,
    .rxConfig.bAppendRssi       = 0x1,
    .rxConfig.bAppendCorrCrc    = 0x1,
    .rxConfig.bAppendSrcInd     = 0x0,
    .rxConfig.bAppendTimestamp  = 0x1,
    .pRxQ                       = 0,        /* set by radio_init() */
    .pOutput                    = 0,        /* set by radio_init() */
    .frameFiltOpt.frameFiltEn   = 0x0,      /* set by radio_init(): 0 = promiscuous */
    .frameFiltOpt.frameFiltStop = 0x1,
    .frameFiltOpt.autoAckEn     = 0x0,
    .frameFiltOpt.slottedAckEn  = 0x0,
    .frameFiltOpt.autoPendEn    = 0x0,
    .frameFiltOpt.defaultPend   = 0x0,
    .frameFiltOpt.bPendDataReqOnly = 0x0,
    .frameFiltOpt.bPanCoord     = 0x0,
    .frameFiltOpt.maxFrameVersion = 0x3,    /* SysConfig default — accept all */
    .frameFiltOpt.fcfReservedMask = 0x0,
    .frameFiltOpt.modifyFtFilter  = 0x0,
    .frameFiltOpt.bStrictLenFilter = 0x0,
    .frameTypes.bAcceptFt0Beacon  = 0x1,
    .frameTypes.bAcceptFt1Data    = 0x1,
    .frameTypes.bAcceptFt2Ack     = 0x1,
    .frameTypes.bAcceptFt3MacCmd  = 0x1,
    .frameTypes.bAcceptFt4Reserved = 0x1,
    .frameTypes.bAcceptFt5Reserved = 0x1,
    .frameTypes.bAcceptFt6Reserved = 0x1,
    .frameTypes.bAcceptFt7Reserved = 0x1,
    /* CCA settings: canonical SysConfig values for IEEE 802.15.4 — all
     * CCA enables off (we're promiscuous-receive; we don't gate RX on CCA),
     * and ccaRssiThr=0x64 matches TI's generated config. */
    .ccaOpt.ccaEnEnergy         = 0x0,
    .ccaOpt.ccaEnCorr           = 0x0,
    .ccaOpt.ccaEnSync           = 0x0,
    .ccaOpt.ccaCorrOp           = 0x1,
    .ccaOpt.ccaSyncOp           = 0x1,
    .ccaOpt.ccaCorrThr          = 0x0,
    .ccaRssiThr                 = 0x64,
    .numExtEntries              = 0x00,
    .numShortEntries            = 0x00,
    .pExtEntryList              = 0,
    .pShortEntryList            = 0,
    /* Non-zero placeholder addresses (canonical SysConfig default) — needed
     * even in promiscuous mode; some CPE patch versions still look at these. */
    .localExtAddr               = 0x12345678,
    .localShortAddr             = 0xABBA,
    .localPanID                 = 0x0000,
    .endTrigger.triggerType     = TRIG_NEVER,
    .endTrigger.bEnaCmd         = 0x0,
    .endTrigger.triggerNo       = 0x0,
    .endTrigger.pastTrig        = 0x0,
    .endTime                    = 0x00000000,
};
