#!/usr/bin/env python3
"""APS local web UI — zero-dependency stdlib HTTP server.

Reads decoded telemetry from /opt/aps/logs/telemetry-decoded-YYYY-MM-DD.jsonl
and serves a small JSON API plus the index.html SPA. Designed to run on the
Pi alongside aps-unified-daemon. No Flask, no Node, no build step.

Persistent config (MQTT broker etc.) lives at /opt/aps/etc/mqtt.json so the
daemon and UI agree.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import secrets
import socketserver
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LOG_DIR    = Path("/opt/aps/logs")
ETC_DIR    = Path("/opt/aps/etc")
WEB_DIR    = Path(__file__).parent
PORT       = int(os.environ.get("APS_WEB_PORT", "8088"))

# Daemon source whose hardcoded INVERTERS list is the seed if no override.
DAEMON_SRC = Path("/opt/aps/host/aps_unified_daemon.py")
MQTT_CFG   = ETC_DIR / "mqtt.json"
INV_CFG    = ETC_DIR / "inverters.json"     # user-editable runtime override

# In-memory cache — append-only logs, so trust mtime.
_cache_lock = threading.Lock()
_decoded_cache: dict[str, list[dict]] = {}
_decoded_mtime: dict[str, float] = {}


def _decoded_path(d: date) -> Path:
    return LOG_DIR / f"telemetry-decoded-{d.isoformat()}.jsonl"


def _load_decoded(d: date) -> list[dict]:
    path = _decoded_path(d)
    if not path.exists():
        return []
    mtime = path.stat().st_mtime
    key = d.isoformat()
    with _cache_lock:
        if _decoded_mtime.get(key) == mtime and key in _decoded_cache:
            return _decoded_cache[key]
    records: list[dict] = []
    with path.open() as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    with _cache_lock:
        _decoded_cache[key] = records
        _decoded_mtime[key] = mtime
    return records


def _local_iso(dt: datetime) -> str:
    """ISO string with the Pi's local TZ offset so the browser parses it
    correctly into local time on the chart x-axis."""
    return dt.astimezone().isoformat(timespec="seconds")


# ─── /api/snapshot ─────────────────────────────────────────────────────────
def api_snapshot() -> dict:
    """Latest decoded reading per inverter + total + last-poll age.

    Pull both yesterday + today, then take the newest record per inverter so
    nothing disappears at midnight rollover.
    """
    pool = _load_decoded(date.today() - timedelta(days=1)) + _load_decoded(date.today())
    latest: dict[str, dict] = {}
    for rec in pool:
        latest[rec["inv"]] = rec
    now = datetime.now()

    # Stable order: yc600-1, yc600-2, ds3 (add fallback for any extras)
    pref = ["yc600-1", "yc600-2", "ds3"]
    all_names = pref + [k for k in latest if k not in pref]

    # Pull labels from the inverter config file once per snapshot. The label
    # is the user-facing display name (e.g. "Garage East"); `name` stays as
    # the slug used for topic/state-dict keys.
    inv_cfg = _read_inverters_file() or []
    labels = {x.get("name"): (x.get("label") or "").strip()
              for x in inv_cfg if x.get("name")}
    # Per-panel labels: {inv_name: [label, label, ...]} preserving index.
    # Panel serial stays out of /api/snapshot (same soft-credential rule as
    # the inverter serial); full per-panel config is in /api/inverters.
    panels_meta = {
        x.get("name"): [(p.get("label") or "") for p in (x.get("panels") or [])]
        for x in inv_cfg if x.get("name")
    }

    inverters = []
    total_power_W = 0.0
    total_energy_today_Wh = 0.0
    live_count = 0
    for name in all_names:
        rec = latest.get(name)
        if not rec:
            inverters.append({"name": name, "label": labels.get(name) or name,
                              "status": "no_data",
                              "reason": "awaiting first telemetry"})
            continue
        ts = datetime.fromisoformat(rec["ts"])
        age_s = (now - ts).total_seconds()
        panels = []
        meta = panels_meta.get(name, [])
        for i, p in enumerate(rec.get("panels", [])):
            instant = p.get("instant_power_W")
            if instant is None:
                instant = round(p["dc_voltage_V"] * p["dc_current_A"], 1)
            panels.append({
                "label": meta[i] if i < len(meta) else "",
                "dc_voltage_V":   p["dc_voltage_V"],
                "dc_current_A":   p["dc_current_A"],
                "instant_power_W": instant,
                "energy_today_Wh": p["energy_today_Wh"],
            })
        snap_power = sum(p["instant_power_W"] for p in panels)
        total_power_W += snap_power
        total_energy_today_Wh += rec.get("total_energy_today_Wh", 0)
        if age_s < 120:
            live_count += 1
        inverters.append({
            "name": name,
            "label": labels.get(name) or name,
            "status": "live" if age_s < 600 else "stale",
            # Serial is omitted from the public snapshot on purpose — it
            # functions as a soft credential (PAN + serial observed off-air
            # let any nearby coordinator talk to the inverter). Management
            # surfaces (/api/inverters, edit modal) still expose it.
            "type": rec.get("inverter_type"),
            "signal_quality_pct": rec.get("signal_quality_pct"),
            "ac_voltage_V": rec.get("ac_voltage_V"),
            "ac_freq_Hz": rec.get("ac_freq_Hz"),
            "temperature_C": rec.get("temperature_C"),
            "instant_power_W": round(snap_power, 1),
            "total_energy_today_Wh": rec.get("total_energy_today_Wh"),
            "panels": panels,
            "last_poll_age_s": round(age_s, 1),
        })

    return {
        "now": _local_iso(now),
        "total_power_W": round(total_power_W, 1),
        "total_energy_today_Wh": round(total_energy_today_Wh, 2),
        "live_count": live_count,
        "inverters": inverters,
    }


# ─── /api/today ────────────────────────────────────────────────────────────
def _inverter_labels() -> dict:
    """Return {name: label} for every configured inverter. Label falls back
    to the slug name when unset, so callers always get a non-empty string."""
    out: dict[str, str] = {}
    for x in (_read_inverters_file() or []):
        n = x.get("name")
        if not n:
            continue
        out[n] = (x.get("label") or "").strip() or n
    return out


def api_today() -> dict:
    """Today's decoded record stream, down-sampled to 1 entry/minute/inverter."""
    records = _load_decoded(date.today())
    seen: dict[tuple[str, str], dict] = {}
    for r in records:
        seen[(r["inv"], r["ts"][:16])] = r
    series: dict[str, list[dict]] = {}
    for (inv, _ts), r in sorted(seen.items(), key=lambda kv: kv[0][1]):
        # Local-tz ISO so Chart.js time scale plots correctly.
        ts_local = _local_iso(datetime.fromisoformat(r["ts"]))
        series.setdefault(inv, []).append({
            "ts": ts_local,
            "power_W": round(sum(p.get("instant_power_W",
                                       p["dc_voltage_V"] * p["dc_current_A"])
                                 for p in r.get("panels", [])), 1),
            "total_Wh": r.get("total_energy_today_Wh", 0),
        })
    return {"series": series, "labels": _inverter_labels()}


# ─── /api/history?days=N ───────────────────────────────────────────────────
def api_history(days: int = 7) -> dict:
    """Last N days, latest-of-day per inverter. Aligns all inverters to the
    same date axis so bars stack correctly in the chart."""
    today = date.today()
    dates = [today - timedelta(days=d) for d in range(days - 1, -1, -1)]
    date_strs = [d.isoformat() for d in dates]

    # Find every inverter that ever appeared in this window
    all_invs: set[str] = set()
    by_inv_by_date: dict[str, dict[str, float]] = {}
    for d in dates:
        recs = _load_decoded(d)
        latest_for_day: dict[str, dict] = {}
        for r in recs:
            latest_for_day[r["inv"]] = r
        for inv, r in latest_for_day.items():
            all_invs.add(inv)
            by_inv_by_date.setdefault(inv, {})[d.isoformat()] = \
                r.get("total_energy_today_Wh", 0)

    series: dict[str, list[dict]] = {}
    for inv in sorted(all_invs):
        series[inv] = [
            {"date": ds, "total_Wh": by_inv_by_date.get(inv, {}).get(ds, 0)}
            for ds in date_strs
        ]
    return {"days": days, "series": series, "labels": _inverter_labels()}


def _seeded_inverters() -> list[dict]:
    """Parse the hardcoded INVERTERS tuple from the daemon source — used only
    as the seed for the first run of /api/inverters."""
    out: list[dict] = []
    if not DAEMON_SRC.exists():
        return out
    src = DAEMON_SRC.read_text()
    for m in re.finditer(
        r'\("([^"]+)",\s*0x([0-9A-Fa-f]+),\s*"(\d+)",\s*"(\w+)"\)', src
    ):
        name, short_hex, serial, family = m.groups()
        out.append({
            "name": name,
            "serial": serial,
            "family": family,
            "short_addr_on_wire": short_hex.upper(),
        })
    return out


def _read_inverters_file() -> list[dict] | None:
    if not INV_CFG.exists():
        return None
    try:
        return json.loads(INV_CFG.read_text())
    except Exception:
        return None


def _ecu_id_and_channel() -> tuple[str, int]:
    ecu_id = "D8A3000000DE"; channel = 16
    if DAEMON_SRC.exists():
        src = DAEMON_SRC.read_text()
        m = re.search(r'ECU_ID_LE\s*=\s*bytes\.fromhex\("([0-9A-Fa-f]+)"\)', src)
        if m:
            le = m.group(1)
            ecu_id = "".join(le[i:i+2] for i in range(len(le)-2, -2, -2))
        m = re.search(r'CHANNEL\s*=\s*(\d+)', src)
        if m:
            channel = int(m.group(1))
    return ecu_id, channel


# ─── /api/config — ECU + channel + current inverters list ─────────────────
def _firmware_version() -> str:
    """Live firmware version as the daemon's status banner reports it
    (the daemon learns it from the dongle's PKT_INFO reply and writes
    'fw vN' into STATUS_UNIFIED.txt). Empty string if not yet known."""
    for p in (Path("/opt/aps/logs/STATUS_UNIFIED.txt"),
              Path("/opt/aps/STATUS_UNIFIED.txt")):
        if p.exists():
            m = re.search(r"fw v(\d+)", p.read_text())
            if m:
                return f"v{m.group(1)}"
    return ""


def api_config() -> dict:
    inverters = _read_inverters_file() or _seeded_inverters()
    ecu_id, channel = _ecu_id_and_channel()
    # PAN ID is the first 2 bytes of ECU_ID byte-swapped (LE on wire).
    # ECU "D8A3000000DE" → bytes D8 A3 → PAN 0xA3D8.
    pan_id = "0x" + (ecu_id[2:4] + ecu_id[0:2]).upper() if len(ecu_id) >= 4 else "0x?"
    return {"inverters": inverters, "ecu_id": ecu_id,
            "pan_id": pan_id, "channel": channel,
            "firmware_version": _firmware_version()}


# ─── /api/inverters — read + write the inverter list ──────────────────────
INV_FAMILIES = {"YC600", "QS1", "DS3"}
FAMILY_BY_PREFIX = {"4080": "YC600", "7040": "DS3", "5010": "QS1"}

def api_inverters_get() -> dict:
    invs = _read_inverters_file()
    if invs is None:
        invs = _seeded_inverters()
        # Persist the seed so the daemon and UI share one source of truth.
        ETC_DIR.mkdir(parents=True, exist_ok=True)
        INV_CFG.write_text(json.dumps(invs, indent=2))
    return {"inverters": invs}


def _validate_inverter(d: dict) -> tuple[bool, str]:
    # `name` is the machine-safe slug — used as MQTT topic component,
    # HA unique_id base, JSON state key. Strict ASCII slug rules.
    name = (d.get("name") or "").strip()
    if not name or len(name) > 32:
        return False, "name: 1-32 chars required"
    if not re.fullmatch(r"[a-z0-9_\-]+", name):
        return False, "name: lowercase letters / digits / _ / - only"
    # `label` is the human-facing display name shown in the UI and used as
    # the HA friendly_name. Optional; allows accented letters, spaces,
    # punctuation common in installation labels (e.g. "Garage East", "Toit-Sud").
    label = (d.get("label") or "").strip()
    if label:
        if len(label) > 40:
            return False, "label: max 40 chars"
        # Letters incl. Latin-1 accented, digits, spaces, hyphens, underscores,
        # apostrophes, dots, slashes. No control chars, no quotes, no pipes.
        if not re.fullmatch(r"[A-Za-zÀ-ÿ0-9 ._\-'/]+", label):
            return False, "label: letters / digits / spaces / . _ - ' / only"
    serial = (d.get("serial") or "").upper().strip()
    if not re.fullmatch(r"[0-9A-F]{12}", serial):
        return False, "serial: exactly 12 hex chars"
    family = (d.get("family") or "").upper().strip()
    if family not in INV_FAMILIES:
        return False, f"family: one of {sorted(INV_FAMILIES)}"
    short = (d.get("short_addr_on_wire") or "").upper().strip()
    if short and not re.fullmatch(r"[0-9A-F]{4}", short):
        return False, "short_addr_on_wire: 4 hex chars or empty"
    # `enabled` is optional; missing / non-bool treated as True for
    # backwards compat with config files written before #24.
    # `panels` is optional — per-panel metadata (label + opt. serial) for
    # the Now-tab card + HA discovery. Validate each entry shallowly so
    # an old config without the field is accepted as-is.
    panels = d.get("panels", [])
    if panels and not isinstance(panels, list):
        return False, "panels: must be an array"
    if len(panels) > 8:
        return False, "panels: at most 8 entries (covers up to HMS-2500-8T)"
    for i, p in enumerate(panels):
        if not isinstance(p, dict):
            return False, f"panels[{i}]: must be an object"
        plbl = (p.get("label") or "").strip()
        if plbl and (len(plbl) > 40 or
                     not re.fullmatch(r"[A-Za-zÀ-ÿ0-9 ._\-'/]+", plbl)):
            return False, f"panels[{i}].label: max 40 chars / letters / digits / spaces / . _ - ' /"
        psn = (p.get("serial") or "").strip()
        if psn and (len(psn) > 32 or
                    not re.fullmatch(r"[A-Za-z0-9._\- ]+", psn)):
            return False, f"panels[{i}].serial: max 32 chars / letters / digits / . _ - space"
    # `calib` is an optional signed watt offset applied to set-power
    # commands (effective = requested + calib) to trim the inverter's
    # throttle drift. Bounded to ±300 W — a sane manual-trim range.
    calib = d.get("calib", 0)
    try:
        calib = int(calib)
    except (TypeError, ValueError):
        return False, "calib: must be an integer (watts)"
    if not -300 <= calib <= 300:
        return False, "calib: must be between -300 and 300 W"
    return True, ""


def api_inverters_put(body: dict) -> dict:
    """Accepts either a single inverter object or {'inverters': [...]} for
    a full replacement. Validates each entry. Returns the new list."""
    if "inverters" in body:
        candidates = body["inverters"]
    else:
        candidates = [body]
    if not isinstance(candidates, list):
        return {"error": "expected an array under 'inverters'"}
    cleaned: list[dict] = []
    for d in candidates:
        ok, msg = _validate_inverter(d)
        if not ok:
            return {"error": msg, "rejected": d}
        # `enabled` defaults to True; explicit False disables polling
        # (daemon-side: skip in main loop, mark "[DISABLED]" in status).
        en = d.get("enabled", True)
        if isinstance(en, str):
            en = en.lower() not in ("false", "0", "no", "off")
        # Normalize panel entries — trim, drop empty {} (no label, no serial)
        # so the config file stays compact; preserve order.
        raw_panels = d.get("panels") or []
        clean_panels = []
        for p in raw_panels:
            lbl = (p.get("label") or "").strip()
            psn = (p.get("serial") or "").strip()
            if not lbl and not psn:
                clean_panels.append({})  # placeholder to preserve index
            else:
                clean_panels.append({"label": lbl, "serial": psn})
        # Trim trailing all-empty entries
        while clean_panels and not clean_panels[-1]:
            clean_panels.pop()
        cleaned.append({
            "name": d["name"].strip(),
            "label": (d.get("label") or "").strip(),
            "serial": d["serial"].upper().strip(),
            "family": d["family"].upper().strip(),
            "short_addr_on_wire":
                (d.get("short_addr_on_wire") or "").upper().strip(),
            "enabled": bool(en),
            "calib": int(d.get("calib", 0) or 0),
            "panels": clean_panels,
        })
    # No duplicate names or serials
    names = [c["name"] for c in cleaned]
    if len(names) != len(set(names)):
        return {"error": "duplicate inverter name in list"}
    serials = [c["serial"] for c in cleaned]
    if len(serials) != len(set(serials)):
        return {"error": "duplicate inverter serial in list"}
    ETC_DIR.mkdir(parents=True, exist_ok=True)
    INV_CFG.write_text(json.dumps(cleaned, indent=2))
    return {"saved": True, "inverters": cleaned}


# ─── /api/mqtt — read + write broker config ────────────────────────────────
# Two prefixes:
#   topic_prefix       → HA discovery root, must match the broker's HA
#                        discovery_prefix (default "homeassistant").
#   state_topic_prefix → top-level root for our actual state/value topics
#                        (default "apsbridge"). Decoupled from the HA root
#                        so state topics land at e.g. apsbridge/yc600-1/...
#                        instead of homeassistant/aps/yc600-1/... — clearer
#                        broker namespace, doesn't masquerade as the vendor.
DEFAULT_MQTT = {
    "enabled": False, "host": "", "port": 1883,
    "username": "", "password": "",
    "topic_prefix": "homeassistant",
    "state_topic_prefix": "apsbridge",
    "retain": True, "ha_discovery": True,
}

def _validate_mqtt_prefix(p: str) -> str | None:
    """Return None if valid, else an error string. MQTT topics allow
    a-z A-Z 0-9 / _ -. Disallow leading/trailing slash, MQTT wildcards (+ #),
    nulls, and whitespace. Max length kept sane for broker filters."""
    if not isinstance(p, str) or not p:
        return "must be a non-empty string"
    if len(p) > 64:
        return "max 64 chars"
    if p.startswith("/") or p.endswith("/"):
        return "no leading/trailing slash"
    if not re.fullmatch(r"[A-Za-z0-9_\-/]+", p):
        return "letters / digits / _ - / only (no + # wildcards, no spaces)"
    return None

def api_mqtt_get() -> dict:
    """Merge stored config over DEFAULT_MQTT so the UI always sees every
    canonical field (including new ones added in releases after the file
    was first written). Password is masked."""
    cfg: dict = {}
    if MQTT_CFG.exists():
        try:
            cfg = json.loads(MQTT_CFG.read_text())
        except Exception:
            cfg = {}
    merged = {**DEFAULT_MQTT, **cfg}
    return {**merged, "password": "***" if merged.get("password") else ""}

def api_mqtt_put(body: dict) -> dict:
    ETC_DIR.mkdir(parents=True, exist_ok=True)
    # If incoming password is masked, keep previously stored one.
    if MQTT_CFG.exists():
        try:
            existing = json.loads(MQTT_CFG.read_text())
        except Exception:
            existing = {}
    else:
        existing = {}
    if body.get("password") in ("", "***"):
        body["password"] = existing.get("password", "")
    # validate ints
    try:
        body["port"] = int(body.get("port", 1883))
    except Exception:
        body["port"] = 1883
    # validate the two topic prefixes if the caller sent them — empty/missing
    # values fall back to the defaults via the {**DEFAULT_MQTT, **body} merge.
    for fld in ("topic_prefix", "state_topic_prefix"):
        if fld in body and body[fld]:
            err = _validate_mqtt_prefix(body[fld])
            if err:
                return {"error": f"{fld}: {err}", "rejected": body[fld]}
    merged = {**DEFAULT_MQTT, **existing, **body}
    MQTT_CFG.write_text(json.dumps(merged, indent=2))
    return {"saved": True, "config": {**merged, "password": "***" if merged["password"] else ""}}


# ─── /api/radio — TX power + future radio knobs ───────────────────────────
RADIO_CFG = ETC_DIR / "radio.json"

# Supported TX power values — std-PA (-20..+5) plus high-PA (+6..+20). Keep
# in sync with SUPPORTED_TX_POWERS_DBM in aps_unified_daemon.py and the two
# tables in firmware/ti_radio_config.c.
SUPPORTED_TX_POWERS_DBM = [
    -20, -15, -10, -5, 0, 1, 2, 3, 4, 5,
    6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
]
DEFAULT_RADIO = {"tx_power_dbm": 5}

def api_radio_get() -> dict:
    cfg = DEFAULT_RADIO.copy()
    if RADIO_CFG.exists():
        try:
            cfg.update(json.loads(RADIO_CFG.read_text()))
        except Exception:
            pass
    # Always report the supported values so the UI can render the slider
    # without hardcoding them — single source of truth lives server-side.
    cfg["supported_dbm"] = SUPPORTED_TX_POWERS_DBM
    return cfg

# ─── /api/retention — UI-managed log retention ────────────────────────────
RETENTION_CFG = ETC_DIR / "retention.json"
DEFAULT_RETENTION = {"telemetry_days": 90}

def api_retention_get() -> dict:
    cfg = DEFAULT_RETENTION.copy()
    if RETENTION_CFG.exists():
        try:
            cfg.update(json.loads(RETENTION_CFG.read_text()))
        except Exception:
            pass
    # Surface current disk usage of /opt/aps/logs so the UI can show
    # how much retention is buying users.
    log_dir = Path("/opt/aps/logs")
    if log_dir.exists():
        try:
            total = sum(p.stat().st_size for p in log_dir.iterdir() if p.is_file())
            cfg["current_log_bytes"] = total
        except Exception:
            cfg["current_log_bytes"] = 0
    else:
        cfg["current_log_bytes"] = 0
    return cfg

def api_retention_put(body: dict) -> dict:
    try:
        d = int(body.get("telemetry_days", DEFAULT_RETENTION["telemetry_days"]))
    except Exception:
        return {"error": "telemetry_days must be an integer"}
    if d < 0 or d > 3650:
        return {"error": "telemetry_days must be between 0 and 3650 (0 = keep forever)"}
    ETC_DIR.mkdir(parents=True, exist_ok=True)
    new_cfg = {"telemetry_days": d}
    RETENTION_CFG.write_text(json.dumps(new_cfg, indent=2))
    return {"saved": True, "config": new_cfg,
            "note": "Cleanup runs on next daemon midnight rollover; "
                    "restart aps-unified to sweep now."}


# ─── /api/features — UI-managed feature flags ─────────────────────────────
# Currently houses commands_enabled: gates the per-inverter set-power /
# reboot action queue. Defaults to false because those commands TX cleanly
# but their semantic effect (does the inverter actually accept set-power?)
# is not yet verified — see docs/aps-protocol.md §E.4-E.5. Users who want
# to experiment opt in via the Config tab.
FEATURES_CFG = ETC_DIR / "features.json"
DEFAULT_FEATURES = {"commands_enabled": False}

def api_features_get() -> dict:
    cfg = DEFAULT_FEATURES.copy()
    if FEATURES_CFG.exists():
        try:
            cfg.update(json.loads(FEATURES_CFG.read_text()))
        except Exception:
            pass
    return cfg

def api_features_put(body: dict) -> dict:
    cfg = {"commands_enabled": bool(body.get("commands_enabled", False))}
    ETC_DIR.mkdir(parents=True, exist_ok=True)
    FEATURES_CFG.write_text(json.dumps(cfg, indent=2))
    return {"saved": True, "config": cfg,
            "note": "Daemon picks up the change on the next action-queue tick (≤30s)."}


# ─── /api/logging — UI-managed verbose toggle ─────────────────────────────
LOGGING_CFG = ETC_DIR / "logging.json"
DEFAULT_LOGGING = {"verbose": False}

def api_logging_get() -> dict:
    cfg = DEFAULT_LOGGING.copy()
    if LOGGING_CFG.exists():
        try:
            cfg.update(json.loads(LOGGING_CFG.read_text()))
        except Exception:
            pass
    return cfg

def api_logging_put(body: dict) -> dict:
    v = bool(body.get("verbose", False))
    ETC_DIR.mkdir(parents=True, exist_ok=True)
    new_cfg = {"verbose": v}
    LOGGING_CFG.write_text(json.dumps(new_cfg, indent=2))
    return {"saved": True, "config": new_cfg,
            "note": "Daemon hot-reloads this on the next poll cycle (~30s)."}


# ─── /api/polling — UI-managed inverter poll interval ─────────────────────
# The interval is how often the daemon polls every inverter. The minimum
# is a safety floor: a full poll cycle already spends ~2.5 s per inverter
# plus a watchdog probe (~8-10 s for 3 inverters), so a shorter interval
# would leave no idle gap and just saturate the 2.4 GHz band. Keep these
# bounds in sync with MIN/MAX_POLL_INTERVAL_S in aps_unified_daemon.py.
POLLING_CFG = ETC_DIR / "polling.json"
DEFAULT_POLL_INTERVAL_S = 30
MIN_POLL_INTERVAL_S = 15
MAX_POLL_INTERVAL_S = 3600

def api_polling_get() -> dict:
    cfg = {"interval_s": DEFAULT_POLL_INTERVAL_S}
    if POLLING_CFG.exists():
        try:
            cfg.update(json.loads(POLLING_CFG.read_text()))
        except Exception:
            pass
    # Surface the bounds so the UI can render + enforce them client-side.
    cfg["min_interval_s"] = MIN_POLL_INTERVAL_S
    cfg["max_interval_s"] = MAX_POLL_INTERVAL_S
    return cfg

def api_polling_put(body: dict) -> dict:
    try:
        v = int(body.get("interval_s", DEFAULT_POLL_INTERVAL_S))
    except (TypeError, ValueError):
        return {"error": "interval_s must be an integer (seconds)"}
    if not MIN_POLL_INTERVAL_S <= v <= MAX_POLL_INTERVAL_S:
        return {"error": f"interval_s must be {MIN_POLL_INTERVAL_S}-"
                          f"{MAX_POLL_INTERVAL_S} seconds", "rejected": v}
    ETC_DIR.mkdir(parents=True, exist_ok=True)
    POLLING_CFG.write_text(json.dumps({"interval_s": v}, indent=2))
    return {"saved": True, "config": {"interval_s": v},
            "note": "Daemon applies this on the next poll cycle."}


def api_radio_put(body: dict) -> dict:
    try:
        v = int(body.get("tx_power_dbm", DEFAULT_RADIO["tx_power_dbm"]))
    except Exception:
        return {"error": "tx_power_dbm must be an integer"}
    if v not in SUPPORTED_TX_POWERS_DBM:
        return {"error": f"tx_power_dbm must be one of {SUPPORTED_TX_POWERS_DBM}",
                "rejected": v}
    ETC_DIR.mkdir(parents=True, exist_ok=True)
    new_cfg = {"tx_power_dbm": v}
    RADIO_CFG.write_text(json.dumps(new_cfg, indent=2))
    # NOTE: the daemon reads radio.json on each Bridge.open(), so a value
    # change takes effect at the next bridge bounce. To force immediate
    # apply, restart aps-unified.service. Documented in the UI hint.
    return {"saved": True, "config": new_cfg,
            "note": "Restart aps-unified.service to apply immediately, "
                    "or the value will take effect on the next bridge reconnect."}


# ─── /api/action — one-shot inverter commands (set_max_power, reboot) ─────
# We don't talk to /dev/ttyUSB0 from here (the daemon owns it). We drop a
# JSON file in /opt/aps/etc/actions/; the daemon picks it up at the top of
# each poll cycle (≤30s), executes, and writes the outcome to
# /opt/aps/logs/actions/.  Per-family floors mirror MIN_MAX_POWER_W in
# aps_unified_daemon.py — a misuse here can't push the inverter below the
# floor even if the file is hand-edited.
ACTIONS_DIR = ETC_DIR / "actions"
ACTION_MIN_W = {"YC600": 50, "QS1": 50, "DS3": 100}
# Per-family soft ceiling — protects against fat-fingered values exceeding
# the inverter's nameplate. Same numbers shown in the UI hint copy.
ACTION_MAX_W = {"YC600": 700, "QS1": 400, "DS3": 800}

def _queue_action(cmd: dict) -> dict:
    """Persist a one-shot command for the daemon to pick up."""
    ACTIONS_DIR.mkdir(parents=True, exist_ok=True)
    inv = cmd.get("inverter") or "global"
    name = f"{int(time.time())}-{cmd['action']}-{inv}.json"
    (ACTIONS_DIR / name).write_text(json.dumps(cmd, indent=2))
    return {"queued": True, "request_file": name,
            "note": "Daemon picks up at next poll cycle (≤30s). "
                    "Result lands in /opt/aps/logs/actions/."}

def api_action_post(body: dict) -> dict:
    if not api_features_get().get("commands_enabled"):
        return {"error": "commands are disabled — enable 'experimental commands' in Config first",
                "code": "commands_disabled"}
    action = body.get("action")
    if action == "set_max_power":
        inv_cfg = api_inverters_get().get("inverters", [])
        by_name = {x["name"]: x for x in inv_cfg}
        name = body.get("inverter")
        if name not in by_name:
            return {"error": f"unknown inverter '{name}'"}
        family = by_name[name].get("family", "")
        try:
            watts = int(body.get("watts"))
        except Exception:
            return {"error": "watts must be an integer"}
        lo = ACTION_MIN_W.get(family, 100)
        hi = ACTION_MAX_W.get(family, 800)
        if not (lo <= watts <= hi):
            return {"error": f"watts={watts} outside [{lo}..{hi}] for {family}"}
        return _queue_action({"action": "set_max_power",
                              "inverter": name, "watts": watts})
    if action == "reboot":
        inv_cfg = api_inverters_get().get("inverters", [])
        if body.get("inverter") not in {x["name"] for x in inv_cfg}:
            return {"error": f"unknown inverter '{body.get('inverter')}'"}
        return _queue_action({"action": "reboot",
                              "inverter": body["inverter"]})
    return {"error": f"unknown action '{action}'"}


# ─── /api/status — raw STATUS file for debugging ───────────────────────────
def api_status_raw() -> dict:
    out = {}
    # Primary: status under /opt/aps/logs/ (shared volume in Docker; also
    # writable on bare-metal installs). Fall back to the legacy /opt/aps/
    # root locations so existing bare-metal installs keep working without
    # a forced migration step.
    for p in (Path("/opt/aps/logs/STATUS_UNIFIED.txt"),
              Path("/opt/aps/STATUS_UNIFIED.txt"),
              Path("/opt/aps/STATUS.txt"),
              Path("/opt/aps/STATUS_DS3.txt")):
        if p.exists():
            out[p.name] = p.read_text()
    return out


# ─── Auth (optional, env-driven) ──────────────────────────────────────────
# Defense-in-depth model: the dashboard's read-only surfaces (snapshot,
# today, history, status) stay public; everything that reveals credentials
# (full serials, MQTT password) or mutates state requires a session cookie
# obtained by POST /api/auth/login with the right password.
#
# Enabled by setting APS_WEBUI_PASSWORD in the daemon's environment. Unset
# → open mode (the prior behavior). Sessions live in-memory only; they're
# cleared on server restart, which is acceptable for a home-lab tool.
AUTH_PASSWORD = os.environ.get("APS_WEBUI_PASSWORD", "").strip()
AUTH_REQUIRED = bool(AUTH_PASSWORD)
AUTH_SESSION_TTL_S = 24 * 3600
AUTH_COOKIE = "aps_session"
_SESSIONS: dict[str, float] = {}  # token -> expiry epoch

# Endpoints that NEVER require auth, even when AUTH_REQUIRED is true:
# - read-only telemetry (snapshot/today/history/status)
# - the auth surface itself (status/login/logout)
PUBLIC_PATHS = {
    "/api/snapshot", "/api/today", "/api/history", "/api/status",
    "/api/auth/status", "/api/auth/login", "/api/auth/logout",
}

# Endpoints public to READ but gated for mutation: a GET passes without
# auth (the UI must fetch these to render the page before login), but a
# PUT/POST changes state and falls through to the session check.
# - /api/config   PAN/channel — non-sensitive, and GET-only anyway.
# - /api/features houses commands_enabled. The GET is a harmless flag, but
#   a PUT flips inverter set-power/reboot control on or off — that is the
#   safety gate itself, so mutating it must require auth.
PUBLIC_GET_PATHS = {"/api/config", "/api/features"}

def _gc_sessions() -> None:
    now = time.time()
    for tok in [t for t, exp in _SESSIONS.items() if exp <= now]:
        _SESSIONS.pop(tok, None)

def _issue_session() -> str:
    _gc_sessions()
    tok = secrets.token_hex(16)
    _SESSIONS[tok] = time.time() + AUTH_SESSION_TTL_S
    return tok

def _parse_cookie(header: str) -> dict:
    out: dict = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out

def _is_authenticated(cookie_header: str) -> bool:
    if not AUTH_REQUIRED:
        return True
    tok = _parse_cookie(cookie_header).get(AUTH_COOKIE, "")
    if not tok:
        return False
    exp = _SESSIONS.get(tok, 0)
    if exp <= time.time():
        _SESSIONS.pop(tok, None)
        return False
    return True


# ─── HTTP plumbing ─────────────────────────────────────────────────────────
class APSHandler(http.server.SimpleHTTPRequestHandler):

    GET_ROUTES = {
        "/api/snapshot":  api_snapshot,
        "/api/today":     api_today,
        "/api/history":   api_history,
        "/api/config":    api_config,
        "/api/inverters": api_inverters_get,
        "/api/mqtt":      api_mqtt_get,
        "/api/radio":     api_radio_get,
        "/api/retention": api_retention_get,
        "/api/logging":   api_logging_get,
        "/api/polling":   api_polling_get,
        "/api/features":  api_features_get,
        "/api/status":    api_status_raw,
    }

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB_DIR), **kw)

    def log_message(self, fmt, *args):
        if args and "/api/snapshot" in (args[0] if args else ""):
            return     # noisy poll, skip
        super().log_message(fmt, *args)

    def _json(self, code: int, body: dict, extra_headers: list | None = None) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)

    def _check_auth(self, path: str, method: str = "GET") -> bool:
        """Return True iff the request may proceed. Sends 401 if not."""
        if not AUTH_REQUIRED:
            return True
        if path in PUBLIC_PATHS:
            return True
        # Public to read, gated to mutate: a GET passes here; a PUT/POST
        # falls through to the session check below.
        if method == "GET" and path in PUBLIC_GET_PATHS:
            return True
        # Static assets (anything not under /api/) — always public.
        if not path.startswith("/api/"):
            return True
        if _is_authenticated(self.headers.get("Cookie", "")):
            return True
        self._json(401, {"error": "authentication required",
                         "code": "auth_required"})
        return False

    def do_GET(self):
        parsed = urlparse(self.path)
        # Auth-status is special: it doesn't 401 itself; it reports the
        # current state so the UI can decide whether to show a login modal.
        if parsed.path == "/api/auth/status":
            self._json(200, {"auth_required": AUTH_REQUIRED,
                             "authenticated": _is_authenticated(
                                 self.headers.get("Cookie", ""))})
            return
        if not self._check_auth(parsed.path, "GET"):
            return
        handler = self.GET_ROUTES.get(parsed.path)
        if handler is not None:
            qs = parse_qs(parsed.query)
            try:
                if parsed.path == "/api/history" and "days" in qs:
                    body = handler(int(qs["days"][0]))
                else:
                    body = handler()
                self._json(200, body)
            except Exception as e:
                self.send_error(500, f"API error: {e}")
            return
        if self.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_PUT(self):
        if not self._check_auth(self.path, "PUT"):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception as e:
            self.send_error(400, f"bad json: {e}")
            return
        if self.path == "/api/mqtt":
            self._json(200, api_mqtt_put(body))
            return
        if self.path == "/api/inverters":
            r = api_inverters_put(body)
            self._json(200 if "saved" in r else 400, r)
            return
        if self.path == "/api/radio":
            r = api_radio_put(body)
            self._json(200 if "saved" in r else 400, r)
            return
        if self.path == "/api/retention":
            r = api_retention_put(body)
            self._json(200 if "saved" in r else 400, r)
            return
        if self.path == "/api/logging":
            r = api_logging_put(body)
            self._json(200 if "saved" in r else 400, r)
            return
        if self.path == "/api/polling":
            r = api_polling_put(body)
            self._json(200 if "saved" in r else 400, r)
            return
        if self.path == "/api/features":
            r = api_features_put(body)
            self._json(200 if "saved" in r else 400, r)
            return
        self.send_error(405, "Method not allowed")

    def do_POST(self):
        # /api/auth/login is the one POST endpoint that's public — that's how
        # users transition from unauthenticated to authenticated. Logout is
        # also public (no point gating "destroy my session").
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception as e:
            self.send_error(400, f"bad json: {e}")
            return
        if self.path == "/api/auth/login":
            if not AUTH_REQUIRED:
                self._json(200, {"ok": True, "auth_required": False,
                                 "note": "open mode — no password configured"})
                return
            pw = (body.get("password") or "").strip()
            # Constant-time compare for the password check.
            if not pw or not secrets.compare_digest(pw, AUTH_PASSWORD):
                self._json(401, {"error": "bad password"})
                return
            tok = _issue_session()
            cookie = (f"{AUTH_COOKIE}={tok}; Path=/; HttpOnly; "
                      f"SameSite=Strict; Max-Age={AUTH_SESSION_TTL_S}")
            self._json(200, {"ok": True, "authenticated": True},
                       extra_headers=[("Set-Cookie", cookie)])
            return
        if self.path == "/api/auth/logout":
            tok = _parse_cookie(self.headers.get("Cookie", "")).get(AUTH_COOKIE, "")
            if tok:
                _SESSIONS.pop(tok, None)
            cookie = f"{AUTH_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
            self._json(200, {"ok": True}, extra_headers=[("Set-Cookie", cookie)])
            return
        if not self._check_auth(self.path, "POST"):
            return
        if self.path == "/api/action":
            r = api_action_post(body)
            self._json(200 if r.get("queued") else 400, r)
            return
        self.send_error(405, "Method not allowed")

    # CORS preflight (in case anyone calls from a different origin)
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ETC_DIR.mkdir(parents=True, exist_ok=True)
    if not MQTT_CFG.exists():
        MQTT_CFG.write_text(json.dumps(DEFAULT_MQTT, indent=2))
    addr = ("0.0.0.0", PORT)
    with ThreadedHTTPServer(addr, APSHandler) as srv:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] "
              f"APS web UI listening on http://{addr[0]}:{addr[1]}")
        srv.serve_forever()


if __name__ == "__main__":
    main()
