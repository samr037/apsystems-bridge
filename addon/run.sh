#!/usr/bin/env bash
# Home Assistant add-on entrypoint for the APsystems Open Bridge.
#
# Wires HA conventions onto the daemon: persistent /data storage, the
# operator-chosen serial device, and MQTT auto-config from the
# Supervisor-provided broker service. Then runs the web UI + poll daemon.
set -e

# --- Persistent storage --------------------------------------------------
# The daemon + web UI hardcode /opt/aps/{etc,logs}; point those at the
# add-on's /data volume so config and telemetry survive add-on updates.
mkdir -p /data/etc /data/logs /opt/aps
ln -sfn /data/etc  /opt/aps/etc
ln -sfn /data/logs /opt/aps/logs

# --- Serial device (add-on option) ---------------------------------------
SERIAL="$(python3 -c "import json; print((json.load(open('/data/options.json')).get('serial_device') or '/dev/ttyUSB0'))" 2>/dev/null || echo /dev/ttyUSB0)"
export APS_SERIAL="$SERIAL"
echo "[addon] serial device: ${APS_SERIAL}"

# --- MQTT: pull broker host + credentials from the Supervisor service ----
python3 - <<'PYEOF'
import json, os, pathlib, urllib.request

mqf = pathlib.Path("/opt/aps/etc/mqtt.json")
cfg = {}
if mqf.exists():
    try:
        cfg = json.loads(mqf.read_text())
    except Exception:
        cfg = {}

token = os.environ.get("SUPERVISOR_TOKEN", "")
try:
    req = urllib.request.Request(
        "http://supervisor/services/mqtt",
        headers={"Authorization": "Bearer " + token})
    svc = json.load(urllib.request.urlopen(req, timeout=10))["data"]
    # Broker connection fields are owned by the Supervisor — refresh them
    # every start. Topic prefixes / discovery flags are left as-is so
    # web-UI tweaks survive.
    cfg.update(enabled=True, host=svc["host"], port=int(svc["port"]),
               username=svc.get("username", ""),
               password=svc.get("password", ""))
    cfg.setdefault("topic_prefix", "homeassistant")
    cfg.setdefault("ha_discovery", True)
    cfg.setdefault("retain", True)
    mqf.write_text(json.dumps(cfg, indent=2))
    print(f"[addon] MQTT auto-configured from Supervisor: "
          f"{svc['host']}:{svc['port']}")
except Exception as e:
    print(f"[addon] no MQTT service available ({e}); telemetry still "
          f"writes to /data/logs and the web UI")
PYEOF

# --- Run: web UI in the background, poll daemon in the foreground --------
# If the daemon exits the container exits and the Supervisor restarts the
# add-on (with its built-in watchdog/restart policy).
python3 /opt/aps/host/webui/server.py &
exec python3 /opt/aps/host/aps_unified_daemon.py
