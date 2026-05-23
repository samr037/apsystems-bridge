/*
 * ti_radio_config.h — IEEE 802.15.4 RF core configuration for the CC2652P.
 *
 * The RF_Mode object and the RF core command structures (CMD_RADIO_SETUP,
 * CMD_FS, CMD_IEEE_TX, CMD_IEEE_RX) for 2.4 GHz O-QPSK 250 kbps IEEE
 * 802.15.4. Ported from Contiki-NG's open cc26x2 IEEE settings
 * (arch/cpu/simplelink-cc13xx-cc26xx/rf-settings/cc26x2/ieee-settings.c,
 * BSD-3-Clause) — this replaces the SysConfig-generated RF config.
 */
#ifndef TI_RADIO_CONFIG_H
#define TI_RADIO_CONFIG_H

#include <ti/devices/DeviceFamily.h>
#include DeviceFamily_constructPath(driverlib/rf_common_cmd.h)
#include DeviceFamily_constructPath(driverlib/rf_ieee_cmd.h)

#include <ti/drivers/rf/RF.h>

/* Note: CMD_RADIO_SETUP_PA (PA-aware variant of CMD_RADIO_SETUP) shares the
 * same opcode (0x0802) but has a larger struct with pRegOverrideTxStd /
 * pRegOverrideTx20 / bSynthNarrowBand fields. The CPE on PA-capable silicon
 * (CC2652P / CC1352P) expects this layout. SysConfig 1.21.1 generates the
 * PA variant for the CC1352P_2 LaunchPad target. */
extern RF_Mode                  rf_ieee_mode;
extern rfc_CMD_RADIO_SETUP_PA_t rf_cmd_ieee_radio_setup;
extern rfc_CMD_FS_t             rf_cmd_ieee_fs;
extern rfc_CMD_IEEE_TX_t        rf_cmd_ieee_tx;
extern rfc_CMD_IEEE_RX_t        rf_cmd_ieee_rx;

#endif /* TI_RADIO_CONFIG_H */
