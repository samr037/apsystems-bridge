/*
 * ccfg.c — Customer Configuration Area for the APsystems bridge firmware.
 *
 * The CCFG is an 88-byte block at the top of flash that the CC2652P ROM
 * reads at boot. This file wraps the SDK's ccfg.c but overrides the
 * bootloader fields: the SDK default *disables* both the ROM serial
 * bootloader and its backdoor, which would leave the dongle un-reflashable
 * over USB (cc2538-bsl could no longer enter the bootloader — only JTAG
 * recovery would remain).
 *
 * We re-enable the ROM bootloader and the backdoor on DIO15, active-low —
 * the exact pin and polarity that the Sonoff ZBDongle-P's DTR/RTS reset
 * gate and `cc2538-bsl --bootloader-sonoff-usb` drive. With this CCFG the
 * dongle always stays recoverable: a normal cc2538-bsl flash works, and
 * holding the internal BOOT button (DIO15) forces the bootloader.
 *
 * The SET_CCFG_* macros below are #ifndef-guarded inside the SDK ccfg.c, so
 * defining them here takes precedence.
 */

/* 0xC5 = enable; any other value = disable. */
#define SET_CCFG_BL_CONFIG_BOOTLOADER_ENABLE   0xC5  /* enable ROM bootloader      */
#define SET_CCFG_BL_CONFIG_BL_ENABLE           0xC5  /* enable bootloader backdoor */
#define SET_CCFG_BL_CONFIG_BL_PIN_NUMBER       15    /* backdoor pin = DIO15       */
#define SET_CCFG_BL_CONFIG_BL_LEVEL            0x0   /* backdoor active-low        */

#include <ti/devices/cc13x2_cc26x2/startup_files/ccfg.c>
