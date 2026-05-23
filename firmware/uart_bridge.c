/*
 * uart_bridge.c — COBS-framed host <-> firmware link over UART0.
 *
 * UART0 is driven straight from driverlib (polled). Packets [type][data...]
 * are COBS-encoded and 0x00-delimited; COBS keeps the payload free of 0x00
 * so the delimiter unambiguously frames arbitrary binary 802.15.4 frames.
 */

#include "uart_bridge.h"
#include "proto.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <ti/devices/DeviceFamily.h>
#include DeviceFamily_constructPath(inc/hw_memmap.h)
#include DeviceFamily_constructPath(driverlib/prcm.h)
#include DeviceFamily_constructPath(driverlib/ioc.h)
#include DeviceFamily_constructPath(driverlib/uart.h)

#define UART_RX_DIO   IOID_12
#define UART_TX_DIO   IOID_13
#define CPU_CLK_HZ    48000000U
#define UART_BAUD     115200U

/* Largest decoded packet: type + a full 802.15.4 frame and metadata. */
#define MAX_PKT       192
#define MAX_ENC       (MAX_PKT + MAX_PKT / 254 + 2)

static bridge_cmd_cb_t cmd_callback;

/* --- COBS ----------------------------------------------------------------
 * Packets here are always < 254 bytes, so the 0xFF-group path is never hit. */

static uint16_t cobs_encode(const uint8_t *src, uint16_t len, uint8_t *dst)
{
    uint16_t code_idx = 0;
    uint16_t di = 1;
    uint8_t  code = 1;
    uint16_t si;

    for (si = 0; si < len; si++) {
        if (src[si] == 0) {
            dst[code_idx] = code;
            code_idx = di++;
            code = 1;
        } else {
            dst[di++] = src[si];
            if (++code == 0xFF) {
                dst[code_idx] = code;
                code_idx = di++;
                code = 1;
            }
        }
    }
    dst[code_idx] = code;
    return di;
}

/* COBS decode result. The caller MUST check `ok` before using `len` —
 * a partial decode on output-cap overflow is otherwise indistinguishable
 * from a successful one (H-1). */
#define COBS_OVERFLOW  UINT16_MAX

static uint16_t cobs_decode(const uint8_t *src, uint16_t len, uint8_t *dst,
                            uint16_t dst_cap)
{
    uint16_t si = 0, di = 0;

    while (si < len) {
        uint8_t code = src[si++];
        uint8_t i;
        if (code == 0)
            return 0;
        for (i = 1; i < code; i++) {
            if (si >= len)
                return di;                    /* end of input — clean */
            if (di >= dst_cap)
                return COBS_OVERFLOW;         /* H-1: signal truncation */
            dst[di++] = src[si++];
        }
        if (code != 0xFF && si < len) {
            if (di >= dst_cap)
                return COBS_OVERFLOW;
            dst[di++] = 0;
        }
    }
    return di;
}

/* --- UART0 ---------------------------------------------------------------- */

static void uart0_init(void)
{
    PRCMPowerDomainOn(PRCM_DOMAIN_SERIAL);
    while (PRCMPowerDomainsAllOn(PRCM_DOMAIN_SERIAL) != PRCM_DOMAIN_POWER_ON)
        ;
    PRCMPeripheralRunEnable(PRCM_PERIPH_UART0);
    PRCMLoadSet();
    while (!PRCMLoadGet())
        ;

    IOCPinTypeUart(UART0_BASE, UART_RX_DIO, UART_TX_DIO,
                   IOID_UNUSED, IOID_UNUSED);
    UARTConfigSetExpClk(UART0_BASE, CPU_CLK_HZ, UART_BAUD,
                        UART_CONFIG_WLEN_8 | UART_CONFIG_STOP_ONE
                        | UART_CONFIG_PAR_NONE);
    UARTEnable(UART0_BASE);
}

void uart_bridge_init(bridge_cmd_cb_t cmd_cb)
{
    cmd_callback = cmd_cb;
    uart0_init();
}

void uart_bridge_send(uint8_t type, const uint8_t *data, uint16_t len)
{
    static uint8_t pkt[MAX_PKT];
    static uint8_t enc[MAX_ENC];
    uint16_t plen, elen, i;

    if (len + 1 > MAX_PKT)
        return;

    pkt[0] = type;
    if (len)
        memcpy(&pkt[1], data, len);
    plen = (uint16_t)(len + 1);

    elen = cobs_encode(pkt, plen, enc);
    for (i = 0; i < elen; i++)
        UARTCharPut(UART0_BASE, enc[i]);
    UARTCharPut(UART0_BASE, 0x00);   /* frame delimiter */
}

void uart_bridge_log(const char *s)
{
    uart_bridge_send(PKT_LOG, (const uint8_t *)s, (uint16_t)strlen(s));
}

void uart_bridge_log_u32(const char *label, uint32_t value)
{
    char buf[48];
    int  i = 0;
    int  s;

    while (label[i] != '\0' && i < 32) {
        buf[i] = label[i];
        i++;
    }
    buf[i++] = '=';
    buf[i++] = '0';
    buf[i++] = 'x';
    for (s = 28; s >= 0; s -= 4) {
        uint8_t nib = (uint8_t)((value >> s) & 0xF);
        buf[i++] = (char)(nib < 10 ? '0' + nib : 'a' + nib - 10);
    }
    buf[i] = '\0';
    uart_bridge_log(buf);
}

void uart_bridge_service(void)
{
    static uint8_t  raw[MAX_ENC];
    static uint16_t rawlen;
    static bool     framing_error;   /* H-2: discard until next 0x00 on overflow */
    static uint8_t  dec[MAX_PKT];    /* M-3: hoist 192B out of stack hot-path */

    while (UARTCharsAvail(UART0_BASE)) {
        int32_t c = UARTCharGetNonBlocking(UART0_BASE);
        if (c < 0)
            break;

        if (c == 0x00) {
            /* Frame delimiter. If we were discarding due to a prior overflow,
             * THIS delimiter closes the bad frame — resume clean from here. */
            if (framing_error) {
                framing_error = false;
                rawlen = 0;
                continue;
            }
            if (rawlen > 0) {
                uint16_t dlen = cobs_decode(raw, rawlen, dec, sizeof(dec));
                /* H-1: COBS_OVERFLOW means partial decode; drop the frame. */
                if (dlen != COBS_OVERFLOW && dlen >= 1 && cmd_callback != NULL)
                    cmd_callback(dec[0], &dec[1], (uint16_t)(dlen - 1));
            }
            rawlen = 0;
        } else if (framing_error) {
            /* Still flushing a previous oversized frame; consume silently. */
            continue;
        } else {
            if (rawlen < sizeof(raw)) {
                raw[rawlen++] = (uint8_t)c;
            } else {
                /* H-2: overflow — set framing_error and consume all bytes
                 * until the next 0x00 delimiter, so we don't accept the
                 * tail of an oversized frame as a fresh one. */
                framing_error = true;
                rawlen = 0;
            }
        }
    }
}
