# APsystems Open Bridge

Home Assistant add-on for the [APsystems Open Bridge][repo] — it polls
APsystems **YC600 / DS3** micro-inverters over their proprietary 2.4 GHz
link and feeds Home Assistant via MQTT auto-discovery.

## Requirements

- A **Sonoff ZBDongle-P (CC2652P)** flashed with the APsystems Open
  Bridge firmware (see the project repo), plugged into the machine
  running Home Assistant.
- The Home Assistant host must be within **2.4 GHz radio range** of the
  inverters — the bridge talks to them directly, there is no repeater.
- An **MQTT broker** (e.g. the Mosquitto broker add-on). The add-on
  picks up the broker automatically from the Supervisor.

## Installation

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add:
   `https://github.com/samr037/apsystems-bridge`
2. Install **APsystems Open Bridge** from the store.
3. On the **Configuration** tab, set `serial_device` to the dongle's
   path — check the add-on's **Hardware** tab, or use a stable
   `/dev/serial/by-id/...` path. Default is `/dev/ttyUSB0`.
4. Start the add-on.

## Configuration

| Option | Description |
|--------|-------------|
| `serial_device` | Path to the CC2652P dongle — `/dev/ttyUSB0`, or a `/dev/serial/by-id/...` path (more stable across reboots). |

Inverters, MQTT topic prefixes, poll interval, TX power and per-inverter
calibration are managed in the **web UI**, not here — click **Open Web UI**
on the add-on's page (the UI is exposed via Home Assistant **ingress**,
so it appears in the sidebar with no host-port conflict and is gated
by your Home Assistant login).

## How it works

- MQTT is auto-configured from the Supervisor's broker service — no
  credentials to enter by hand.
- Decoded telemetry is published with Home Assistant MQTT discovery, so
  each inverter appears as a device with power, energy, AC and per-panel
  sensors.
- Configuration and telemetry history persist in the add-on's `/data`.

[repo]: https://github.com/samr037/apsystems-bridge
