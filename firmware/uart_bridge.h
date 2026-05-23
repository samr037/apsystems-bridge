/*
 * uart_bridge.h — COBS-framed host <-> firmware link over UART0.
 *
 * Owns UART0 (DIO13 TX / DIO12 RX). Sends and receives COBS-framed packets
 * (see proto.h / PROTOCOL.md).
 */
#ifndef UART_BRIDGE_H
#define UART_BRIDGE_H

#include <stdint.h>

/* Called from uart_bridge_service() for each decoded host->firmware packet. */
typedef void (*bridge_cmd_cb_t)(uint8_t type, const uint8_t *data, uint16_t len);

/* Bring up UART0 and the framing layer. */
void uart_bridge_init(bridge_cmd_cb_t cmd_cb);

/* COBS-frame and transmit a packet [type][data...] to the host. */
void uart_bridge_send(uint8_t type, const uint8_t *data, uint16_t len);

/* Send a NUL-terminated string as a PKT_LOG packet. */
void uart_bridge_log(const char *s);

/* Send "<label>=0x........" as a PKT_LOG packet (debug instrumentation). */
void uart_bridge_log_u32(const char *label, uint32_t value);

/* Poll UART RX, decode COBS frames, dispatch to the command callback. */
void uart_bridge_service(void);

#endif /* UART_BRIDGE_H */
