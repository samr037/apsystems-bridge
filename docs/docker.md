# Docker deployment

The bridge ships as a single multi-purpose image with a two-service
Compose stack. One container runs the pair/poll daemon (needs the Sonoff
USB dongle passed through), the other serves the dashboard on `:8088`.

All persistent state — config, telemetry history, action queue, status
banner — lives on the host under `/opt/aps/{etc,logs}` and is bind-mounted
into the containers at the same paths the bare-metal install uses. A Docker
install is byte-identical to a bare-metal install from the data side: stop
the systemd units, `docker compose up -d`, and the same files are read in
the same place. No migration, no data loss, no surprises if you ever
switch back.

## Quick start

```bash
# 1. Plug the Sonoff CC2652P into the host. Verify it shows up:
ls /dev/serial/by-id/ | grep Sonoff
# ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_xxxxxx-if00-port0 -> ../../ttyUSB0

# 2. Make sure the state dirs exist (idempotent — does nothing if they're
#    already populated from a prior bare-metal install).
sudo mkdir -p /opt/aps/etc /opt/aps/logs

# 3. Clone + bring the stack up:
git clone https://github.com/samr037/apsystems-bridge.git
cd apsystems-bridge
docker compose up -d --build

# 4. Open the dashboard:
xdg-open http://localhost:8088/
```

First boot takes ~10 s for the daemon to open the dongle. Telemetry
appears in the UI within ~30 s (one poll cycle).

## What lives where

| Inside container | Purpose | Host path |
|---|---|---|
| `/opt/aps/etc/`   | UI-managed config: inverters, MQTT, radio, features, retention | **bind**: `/opt/aps/etc/` |
| `/opt/aps/logs/`  | Append-only telemetry JSONL, action results, status banner | **bind**: `/opt/aps/logs/` |
| `/dev/ttyUSB0`    | Sonoff CC2652P serial | **device**: `${APS_SERIAL:-/dev/ttyUSB0}` |
| `:8088`           | Web UI | **port**: `${APS_WEB_PORT:-8088}` |

Nothing about the user's setup lives inside the image. `docker compose
down && docker image rm apsystems-bridge:local` is fully reversible.

## Services

| Service | Default? | What it does |
|---|---|---|
| `aps-unified` | ✅ on  | Pair + poll loop — talks to the Sonoff dongle, decodes telemetry into JSONL, optionally publishes to MQTT |
| `aps-webui`   | ✅ on  | Dashboard + JSON API on `:8088`. Stateless reader of `/opt/aps/logs/*.jsonl` |

## Environment variables

| Var | Default | Effect |
|---|---|---|
| `APS_SERIAL` | `/dev/ttyUSB0` | Host path to the Sonoff dongle. Use a stable `/dev/serial/by-id/...` symlink if you have multiple USB-serial adapters |
| `APS_WEB_PORT` | `8088` | Host port to publish the UI on |
| `APS_WEBUI_PASSWORD` | *(unset)* | Optional — when set, gates the Config tab + serial views behind a session cookie. Read-only telemetry stays public either way. See **Security** in the main README |
| `TZ` | `Europe/Paris` | Timestamps + daily JSONL roll-over boundary |

Set these in your shell, or drop them in a `.env` next to `docker-compose.yml`.

## Migrating from bare-metal systemd

The two installation modes share `/opt/aps/{etc,logs}` byte-for-byte, so
the migration is mostly a stop-then-start:

```bash
# Stop the systemd units (keep them enabled for an easy rollback).
sudo systemctl stop aps-unified aps-webui

# Bring the Docker stack up — it reads the same config from /opt/aps/etc
# and writes new telemetry to /opt/aps/logs alongside the existing files.
cd /path/to/apsystems-bridge
docker compose up -d --build

# Verify within one poll cycle:
curl -s http://localhost:8088/api/snapshot | jq .live_count

# Disable systemd units only after you've confirmed Docker works, so
# rollback is one `systemctl start` away.
sudo systemctl disable aps-unified aps-webui
```

## Building locally

```bash
# arm64 (Raspberry Pi) — native build:
docker compose build
# Cross-build for arm64 from an amd64 dev box:
docker buildx build --platform linux/arm64 -t apsystems-bridge:local .
```

The runtime image is ~85 MB; build takes 1–2 min on a Pi 4 (most of that
is `pip install pyserial paho-mqtt`).

## MQTT publishing

Once the stack is up, hit the **Config** tab in the web UI and fill in the
broker host / username / password. The daemon hot-reloads
`/opt/aps/etc/mqtt.json` on the next poll cycle (~30 s) and starts
publishing. State topics land at `apsbridge/<inverter>/<metric>` by
default; HA auto-discovery configs at `homeassistant/sensor/<uniq>/config`
point HA at the state topics automatically.

## Authentication (optional)

Read-only telemetry (Now / Today / History tabs) is always public. To gate
the Config tab + the full-serial views behind a password, set
`APS_WEBUI_PASSWORD` before bringing the stack up:

```bash
APS_WEBUI_PASSWORD='your-strong-password' docker compose up -d
# or, persistent:
echo "APS_WEBUI_PASSWORD=your-strong-password" >> .env
docker compose up -d
```

The UI grows a login pill in the header. Sessions are cookie-based, 24h
TTL, held in the webui container's memory (cleared on container restart).

## Troubleshooting

- **`/dev/ttyUSB0` not present in the container**: confirm the host sees
  it (`ls /dev/ttyUSB*`). If the host uses a different path, set
  `APS_SERIAL=/dev/...` before `docker compose up`.
- **`Permission denied` opening the serial**: the container runs as root
  by default, so this should not happen — if it does, the host kernel may
  be denying access (check `dmesg`).
- **Web UI shows "awaiting first telemetry"**: check
  `docker compose logs aps-unified` for bridge errors. The most common is
  a stale `/dev/ttyUSB0` path after replugging the dongle — pull, replug,
  `docker compose restart aps-unified`.
- **Old systemd units still hold the dongle**: `systemctl stop
  aps-unified aps-webui` (Docker can't open `/dev/ttyUSB0` while the
  systemd daemon has it).
- **Want to roll back to bare-metal**: `docker compose down && sudo
  systemctl start aps-unified aps-webui` — same config, same data, same
  exact behavior. The two paths are interchangeable.
