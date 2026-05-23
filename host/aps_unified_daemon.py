#!/usr/bin/env python3
"""APS unified daemon — single Sonoff CC2652P handles ALL inverters.

One dongle running this project's custom, fully-open raw-802.15.4
firmware, hand-building 802.15.4 frames. Polls every configured
inverter each cycle via inter-PAN unicast addressed to its short
address.

Output: /opt/aps/logs/telemetry-decoded-YYYY-MM-DD.jsonl  (web UI reads this)
        /opt/aps/logs/STATUS_UNIFIED.txt  (operator snapshot)
"""
from __future__ import annotations

import json, os, signal, sys, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/opt/aps/host")
from aps_bridge.bridge import Bridge, PKT_RX_FRAME

# Optional MQTT — only import if user enables it via the web UI.
try:
    import paho.mqtt.client as _mqtt
    _MQTT_AVAILABLE = True
except ImportError:
    _MQTT_AVAILABLE = False

# ─── Config ────────────────────────────────────────────────────────────────
PORT       = os.environ.get("APS_SERIAL", "/dev/ttyUSB0")  # override for the HA add-on
MQTT_CFG_PATH = Path("/opt/aps/etc/mqtt.json")
INV_CFG_PATH  = Path("/opt/aps/etc/inverters.json")   # optional UI-managed list
RADIO_CFG_PATH = Path("/opt/aps/etc/radio.json")      # UI-managed TX power etc.
RETENTION_CFG_PATH = Path("/opt/aps/etc/retention.json")  # UI-managed log retention
LOGGING_CFG_PATH = Path("/opt/aps/etc/logging.json")  # UI-managed verbosity toggle
POLLING_CFG_PATH = Path("/opt/aps/etc/polling.json")  # UI-managed poll interval
ACTIONS_DIR = Path("/opt/aps/etc/actions")            # one-shot commands from UI
ACTIONS_LOG_DIR = Path("/opt/aps/logs/actions")       # per-action result records
FEATURES_CFG_PATH = Path("/opt/aps/etc/features.json")  # UI-managed feature flags

def commands_enabled() -> bool:
    """Whether the experimental command queue (set_power / reboot) is
    armed. Mirrors the UI Config toggle; default false until inverters
    semantically accept our throttle frames (see docs/aps-protocol.md §E.5)."""
    if not FEATURES_CFG_PATH.exists():
        return False
    try:
        return bool(json.loads(FEATURES_CFG_PATH.read_text()).get("commands_enabled"))
    except Exception:
        return False

# Per-family lower bound for set_max_power, in watts. Defends against a
# UI typo or rogue caller cutting too deep into the array (we still want
# enough output for the operator to see telemetry, and to avoid bricking
# the inverter into a never-export state that needs an ECU to reset).
MIN_MAX_POWER_W = {"YC600": 50, "QS1": 50, "DS3": 100}
CHANNEL    = 16
# ECU IEEE address — the coordinator your inverters are paired to. Set
# it per-install in radio.json as "ecu_id" (the forward/printed hex,
# MSB-first); the daemon byte-reverses it for on-air use. The
# placeholder below is NOT a real ECU and will poll nothing — every
# install must set its own (read it off the ECU label, or discover it
# while pairing). See load_ecu_id_le() below.
DEFAULT_ECU_ID = "D8A3000000DE"
POLL_INTERVAL_S = 30                               # default; overridable via polling.json
PER_POLL_WAIT_S = 2.5                              # listen window per inverter
# Poll-interval bounds for polling.json. The floor protects the radio +
# inverters: a full cycle already spends ~PER_POLL_WAIT_S per inverter
# (~8-10 s for 3) plus the watchdog probe, so anything under ~15 s would
# leave no idle gap and just hammer the 2.4 GHz band. Ceiling is a sanity
# cap (1 h) — beyond that telemetry is too sparse to be useful.
MIN_POLL_INTERVAL_S = 15
MAX_POLL_INTERVAL_S = 3600

# Default retention if no /opt/aps/etc/retention.json exists. 90 days of
# JSONL telemetry on a Pi-Zero SD card is roughly 50-200 MB — well within
# any reasonable budget. Users with constrained storage can lower; users
# with TB-grade NAS can raise or disable cleanup (0 = keep forever).
DEFAULT_RETENTION_DAYS = 90
# How often the daemon checks whether it's time to run the retention sweep.
# Once per cycle is sufficient; the sweep itself only RUNS once per UTC day.
# Set in-memory in main(); not configurable.

# Default TX power if no /opt/aps/etc/radio.json exists. Matches the
# original firmware default — conservative, well within all ISM-band
# limits and proven to reach the inverters at typical home distances.
DEFAULT_TX_POWER_DBM = 5
# Supported TX power values — std-PA path (-20..+5) plus high-PA path
# (+6..+20) from firmware/ti_radio_config.c. Used to validate radio.json
# input and clamp out-of-range UI values. EU SRD ceiling is +20 dBm EIRP;
# +20 is legal but leaves no headroom for antenna gain.
SUPPORTED_TX_POWERS_DBM = (
    -20, -15, -10, -5, 0, 1, 2, 3, 4, 5,
    6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
)

# ─── Watchdog tuning ──────────────────────────────────────────────────────
# We probe the *dongle* (not the inverters) for liveness. After every poll
# cycle the daemon sends PKT_PING and expects a PKT_INFO reply. The dongle
# answers within ~50 ms when healthy. If we miss this many consecutive
# pings, we consider the bridge stuck (kernel USB-reset half-state etc.)
# and bounce it. This is independent of whether inverters are producing —
# silent inverters at night still leave the dongle pinging happily.
WATCHDOG_PING_TIMEOUT_S = 0.3                       # per-ping wait window
WATCHDOG_MAX_MISSED_PINGS = 3                       # 3 missed = stuck
# After this many consecutive bounce attempts without recovery, exit
# non-zero so systemd's `Restart=always` takes over (full process restart
# + USB device renegotiation).
WATCHDOG_MAX_RECOVERIES = 3

# Runtime RF self-heal. The startup RF check only runs once on bridge
# open; without this, a redeploy at night settles into "likely night,
# proceeding" and the daemon never re-tests RF at dawn. If a whole poll
# cycle gets 0 inverter replies *despite* the dongle still answering
# PKT_PING (so the watchdog above can't fire), bounce the bridge
# in-place every MAX_NO_REPLY_CYCLES_BEFORE_HEAL cycles. During daytime
# escalate to container exit after a few unsuccessful bounces so Docker
# brings up a fresh USB session — the only thing that reliably resolves
# a deep RF-deaf state that an in-process bounce can't fix.
MAX_NO_REPLY_CYCLES_BEFORE_HEAL = 10   # ~5 min at the 30 s default cadence
MAX_RF_HEAL_ATTEMPTS = 3               # daytime-only escalation threshold
DAY_HOUR_START, DAY_HOUR_END = 6, 22   # local-time daytime window [6:00, 22:00)

INVERTERS = [
    # (name, on-wire short, serial, family, enabled) — built-in seed of
    # placeholder values, overridden entirely by the real config in
    # /opt/aps/etc/inverters.json (managed via the web UI). Used only as
    # a fallback when that file is missing or invalid.
    ("yc600-1", 0xAAAA, "408000AAAAAA", "YC600", True),
    ("yc600-2", 0xBBBB, "408000BBBBBB", "YC600", True),
    ("ds3",     0xCCCC, "704000CCCCCC", "DS3",   True),
]


def load_poll_interval() -> int:
    """Poll-cycle interval (seconds) from /opt/aps/etc/polling.json,
    clamped to [MIN_POLL_INTERVAL_S, MAX_POLL_INTERVAL_S]. Read fresh each
    cycle (the file is tiny) so a UI change applies on the next cycle.
    Falls back to POLL_INTERVAL_S when the file is absent or invalid."""
    if not POLLING_CFG_PATH.exists():
        return POLL_INTERVAL_S
    try:
        v = int(json.loads(POLLING_CFG_PATH.read_text()).get(
            "interval_s", POLL_INTERVAL_S))
        return max(MIN_POLL_INTERVAL_S, min(MAX_POLL_INTERVAL_S, v))
    except Exception:
        return POLL_INTERVAL_S


def load_tx_power_dbm() -> int:
    """Return the TX power (dBm) to apply on bridge open. Honors
    /opt/aps/etc/radio.json (UI-managed) if present and valid, otherwise
    DEFAULT_TX_POWER_DBM. Clamps to supported values silently — the UI
    enforces the same set so this only triggers if the file was edited
    by hand to a bad value."""
    if not RADIO_CFG_PATH.exists():
        return DEFAULT_TX_POWER_DBM
    try:
        with RADIO_CFG_PATH.open() as f:
            cfg = json.load(f)
        v = int(cfg.get("tx_power_dbm", DEFAULT_TX_POWER_DBM))
        if v in SUPPORTED_TX_POWERS_DBM:
            return v
        # clamp to nearest supported
        return min(SUPPORTED_TX_POWERS_DBM, key=lambda s: abs(s - v))
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] radio.json invalid: {e}; "
              f"defaulting to {DEFAULT_TX_POWER_DBM} dBm", flush=True)
        return DEFAULT_TX_POWER_DBM


def load_ecu_id_le() -> bytes:
    """ECU IEEE address as on-air little-endian bytes. Reads "ecu_id"
    (the forward/printed hex, MSB-first) from /opt/aps/etc/radio.json
    and byte-reverses it; falls back to DEFAULT_ECU_ID on any problem.
    Loaded once at import — the ECU id is fixed for an install."""
    raw = DEFAULT_ECU_ID
    if RADIO_CFG_PATH.exists():
        try:
            v = (json.loads(RADIO_CFG_PATH.read_text()).get("ecu_id")
                 or "").strip()
            if v:
                bytes.fromhex(v)          # validate before accepting
                raw = v
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] radio.json ecu_id "
                  f"invalid: {e}; using placeholder", flush=True)
    return bytes.fromhex(raw)[::-1]


ECU_ID_LE = load_ecu_id_le()


_inv_mtime = 0.0
_inv_cache: list[tuple] | None = None


def inverter_calib(name: str) -> int:
    """Per-inverter set-power calibration offset, in watts, from
    inverters.json. set_max_power sends ``requested + calib`` so the user
    can trim the inverter's throttle drift (the DS3 settles ~11% above the
    commanded value — see docs/aps-protocol.md §E.5). Read fresh on each
    set-power action (rare); defaults to 0 if missing/unreadable."""
    if not INV_CFG_PATH.exists():
        return 0
    try:
        for d in json.loads(INV_CFG_PATH.read_text()):
            if d.get("name") == name:
                return int(d.get("calib", 0) or 0)
    except Exception:
        pass
    return 0


def load_inverters() -> list[tuple]:
    """Return the current inverter list as ``(name, short, serial, family,
    enabled, label)`` tuples. ``label`` is the human-facing display name
    (UI / HA friendly_name); ``name`` is the slug used as MQTT topic
    component and state-dict key. ``label`` defaults to ``name`` if not set
    so callers always have a non-empty value to render. Reads
    /opt/aps/etc/inverters.json with hot-reload on file mtime change.
    Entries with no ``short_addr_on_wire`` are skipped silently — they
    can't be polled until the user fills in the short addr."""
    global _inv_mtime, _inv_cache
    if not INV_CFG_PATH.exists():
        return [(n, s, ser, fam, en, n) for n, s, ser, fam, en in INVERTERS]
    try:
        mt = INV_CFG_PATH.stat().st_mtime
        if mt == _inv_mtime and _inv_cache is not None:
            return _inv_cache
        with INV_CFG_PATH.open() as f:
            data = json.load(f)
        out: list[tuple] = []
        for d in data:
            short_hex = (d.get("short_addr_on_wire") or "").strip()
            if not short_hex:
                continue  # entry has no short addr yet — skip (can't poll)
            enabled = d.get("enabled", True)
            if isinstance(enabled, str):
                enabled = enabled.lower() not in ("false", "0", "no", "off")
            else:
                enabled = bool(enabled)
            label = (d.get("label") or "").strip() or d["name"]
            out.append((
                d["name"], int(short_hex, 16),
                d["serial"], d["family"], enabled, label,
            ))
        _inv_cache = out
        _inv_mtime = mt
        return out
    except Exception as e:
        # Fall back to seed but log loudly
        print(f"[{datetime.now().isoformat()}] inverters file invalid: {e}",
              flush=True)
        return [(n, s, ser, fam, en, n) for n, s, ser, fam, en in INVERTERS]

# ─── Frame builders ────────────────────────────────────────────────────────
def u16(v): return bytes([v & 0xFF, (v >> 8) & 0xFF])

def _wrap_app(target_short: int, seq: int, aps_ctr: int, app: bytes) -> bytes:
    """Wrap an ``FBFB…FEFE`` application blob in a unicast inter-PAN MAC+NWK+APS
    frame addressed to ``target_short``. Identical envelope for poll / reboot
    / set-power — only the application bytes vary."""
    mac = u16(0x8861) + bytes([seq]) + u16(0xFFFF) + u16(target_short) + u16(0x0000)
    nwk = u16(0x0008) + u16(target_short) + u16(0x0000) + bytes([0x0F, seq])
    aps = bytes([0x00, 0x14]) + u16(0x0006) + u16(0x0F05) + bytes([0x14, aps_ctr])
    return mac + nwk + aps + ECU_ID_LE + app


def build_poll(target_short: int, seq: int, aps_ctr: int) -> bytes:
    """Inter-PAN unicast poll. Works for YC600 *and* DS3 alike."""
    return _wrap_app(target_short, seq, aps_ctr,
                     bytes.fromhex("FBFB06BB000000000000C1FEFE"))


def build_reboot(target_short: int, seq: int, aps_ctr: int) -> bytes:
    """Soft-reboot an inverter. The application byte sequence is identical to
    poll except the command nibble (BB→C1) and the trailing constant
    (C1→A6). Sourced from patience4711's MIT-licensed ESP32 firmware
    (ZIGBEE_HELPERS.ino, inverterReboot())."""
    return _wrap_app(target_short, seq, aps_ctr,
                     bytes.fromhex("FBFB06C1000000000000A6FEFE"))


def build_setpower_yc600(target_short: int, seq: int, aps_ctr: int,
                         watts: int) -> bytes:
    """YC600 / QS1 throttle command. ``Scaled = watts * 28.89`` (16-bit BE),
    followed by a 16-bit checksum that's the byte-wise sum of
    ``[06, 1C, 8C, 02, sc_hi, sc_lo, 00]``. Sourced from patience4711's
    MIT-licensed ESP32 firmware (SETPOWER.ino).

    ``int()`` truncation (not round) deliberately mirrors the C
    ``int Scaled = calibrated * 28.89;`` in SETPOWER.ino — keeps our frame
    byte-identical to the proven implementation for every watt value."""
    scaled = int(watts * 28.89)
    sc_hi, sc_lo = (scaled >> 8) & 0xFF, scaled & 0xFF
    cks = (0x06 + 0x1C + 0x8C + 0x02 + sc_hi + sc_lo + 0x00) & 0xFFFF
    app = (bytes.fromhex("FBFB061C8C02")
           + bytes([sc_hi, sc_lo, 0x00])
           + bytes([(cks >> 8) & 0xFF, cks & 0xFF])
           + bytes.fromhex("FEFE"))
    return _wrap_app(target_short, seq, aps_ctr, app)


def build_setpower_ds3(target_short: int, seq: int, aps_ctr: int,
                       watts: int, channel: int = 1) -> bytes:
    """DS3 throttle command for one MPPT channel. ``Scaled = watts * 16.59``
    (16-bit BE) with a 1-byte validation ``vv = ((msb + lsb) - 0x29) & 0xFF``.
    Sourced from patience4711's MIT-licensed ESP32 firmware (SETPOWER.ino).

    ``channel`` is the byte patience hardcodes to ``01``. The DS3 is
    dual-MPPT and a throttle frame caps only the addressed channel — see
    docs/aps-protocol.md §E.5. Pass channel=2 for the second MPPT input;
    both must be throttled to cap the inverter's total output. ``vv``
    excludes the channel byte (patience's formula sums only 06,AA,27,
    msb,lsb), so it is identical for either channel.

    ``int()`` truncation mirrors the C ``int Scaled = calibrated * 16.59;``
    in SETPOWER.ino."""
    scaled = int(watts * 16.59)
    msb, lsb = (scaled >> 8) & 0xFF, scaled & 0xFF
    vv = (msb + lsb - 0x29) & 0xFF
    app = (bytes.fromhex("FBFB06AA270000")
           + bytes([msb, lsb, channel & 0xFF, vv])
           + bytes.fromhex("FEFE"))
    return _wrap_app(target_short, seq, aps_ctr, app)


SETPOWER_BUILDERS = {
    "YC600": build_setpower_yc600,
    "QS1":   build_setpower_yc600,
    "DS3":   build_setpower_ds3,
}


def build_query(target_short: int, seq: int, aps_ctr: int) -> bytes:
    """Inverter status query (``FBFB06DE…E4FEFE``). The reply echoes the
    currently-programmed throttle ceiling — used to confirm a set-power
    actually landed. Sourced from patience4711's MIT-licensed ESP32
    firmware (ZIGBEE_QUERYING.ino)."""
    return _wrap_app(target_short, seq, aps_ctr,
                     bytes.fromhex("FBFB06DE000000000000E4FEFE"))


def build_setpower_nonsense(target_short: int, seq: int, aps_ctr: int) -> bytes:
    """The 'nonsense' frame patience4711's setMaxPower() sends *between* the
    throttle frame and the verification query — the same 06DE command as
    the query but with a 00 trailer instead of E4. Empirically part of the
    sequence the inverter needs to latch a new throttle: sending the
    throttle frame alone does not take effect. SETPOWER.ino."""
    return _wrap_app(target_short, seq, aps_ctr,
                     bytes.fromhex("FBFB06DE00000000000000FEFE"))


def decode_setpower_query(payload: bytes, family: str) -> float | None:
    """Extract the programmed throttle ceiling (watts) from a query reply.

    The reply's FBFB…FEFE block ends with a fixed structure:
    ``…<value:2B><marker:2B><00 00 00 00> FEFE``. patience4711's
    decodeQueryAnswer() searched for a literal ``3B66`` marker, but that
    'marker' is not constant — it was just whatever sat in his single test
    capture (ours reads ``0366``). The robust anchor is the trailing
    ``FEFE``: with it stripped, the block ends ``<value:4hex><marker:4hex>
    <00000000>`` so the programmed value is hex chars [−16:−12].
    watts = value / 28.89 (YC600/QS1). Verified: our 500 W set-power read
    back 0x386D=14445 → 500.0; patience's 300 W sample 0x21DB → 300.0.

    DS3 keeps patience's from-start offset (value at post-FBFB hex 10..14,
    /16.59) — unverified on hardware, flagged for a future DS3 test.

    Returns None if the reply can't be parsed."""
    if not payload:
        return None
    h = payload.hex().upper()
    if h.find("FBFB") < 0:
        return None
    try:
        if family == "DS3":
            body = h[h.find("FBFB") + 4:]
            if len(body) < 14:
                return None
            return int(body[10:14], 16) / 16.59
        # YC600 / QS1 — strip the closing FEFE, then the value is the 4
        # hex chars before <marker:4><00000000>.
        e = h.rfind("FEFE")
        seg = h[:e] if e >= 16 else h
        if len(seg) < 16:
            return None
        return int(seg[-16:-12], 16) / 28.89
    except ValueError:
        return None


# ─── Raw frame → APS application payload extraction ───────────────────────
def extract_payload(wire: bytes) -> tuple[int | None, bytes | None]:
    """Returns (src_short, payload) given the bytes our firmware delivered
    (1-byte len prefix + raw 802.15.4 frame). payload starts with the
    inverter serial echo (6 bytes) followed by FBFB header."""
    if len(wire) < 25: return None, None
    frame = wire[1:]
    # MAC src short at bytes 7-8 (after FCF[2] + seq[1] + dstPAN[2] + dst[2])
    src_short = (frame[8] << 8) | frame[7]
    i = frame.find(b"\xFB\xFB", 18)
    if i < 6: return src_short, None
    start = i - 6                                # 6-byte serial echo before FBFB
    end = frame.rfind(b"\xFE\xFE")
    if end < i:
        end = len(frame)
    return src_short, frame[start:end + 2]


# ─── Decoders (patience4711 formulas, verified) ────────────────────────────
def _x(h: str, start: int, length: int) -> int:
    return int(h[start:start+length], 16) if h[start:start+length] else 0

def decode_yc600(payload: bytes) -> dict | None:
    if len(payload) < 80 or payload[6:8] != b"\xFB\xFB":
        return None
    sd = payload.hex().upper()
    sigQ_raw = 0    # would be from MAC LinkQual byte upstream; placeholder
    acv  = round((_x(sd, 56, 4) * (1 / 1.3277)) / 4, 1)
    freq_raw = _x(sd, 24, 6)
    freq = round(50_000_000 / freq_raw, 1) if freq_raw else 0.0
    temp = round(_x(sd, 20, 4) * 0.2752 - 258.7, 1)
    dcV0 = round((_x(sd, 54, 2) * 16 + _x(sd, 52, 1)) * 82.5 / 4096, 1)
    dcV1 = round((_x(sd, 48, 2) * 16 + _x(sd, 46, 1)) * 82.5 / 4096, 1)
    dcI0 = round((_x(sd, 53, 1) * 256 + _x(sd, 50, 2)) * 27.5 / 4096, 2)
    dcI1 = round((_x(sd, 47, 1) * 256 + _x(sd, 44, 2)) * 27.5 / 4096, 2)
    t_new = _x(sd, 34, 4)
    raw_p0 = _x(sd, 74 + 10, 6); raw_p1 = _x(sd, 74, 6)
    p0_wh = round(raw_p0 * 8.311 / 3600, 2)
    p1_wh = round(raw_p1 * 8.311 / 3600, 2)
    return {
        "inverter_type": "YC600",
        "serial": sd[0:12],
        "signal_quality_pct": 0,
        "ac_voltage_V": acv, "ac_freq_Hz": freq, "temperature_C": temp,
        "timestamp_raw": t_new,
        "panels": [
            {"dc_voltage_V": dcV0, "dc_current_A": dcI0,
             "instant_power_W": round(dcV0 * dcI0, 1),
             "energy_today_Wh": p0_wh},
            {"dc_voltage_V": dcV1, "dc_current_A": dcI1,
             "instant_power_W": round(dcV1 * dcI1, 1),
             "energy_today_Wh": p1_wh},
        ],
        "total_energy_today_Wh": round(p0_wh + p1_wh, 2),
    }

def decode_ds3(payload: bytes) -> dict | None:
    if len(payload) < 60 or payload[6:8] != b"\xFB\xFB":
        return None
    sd = payload.hex().upper()
    dcV0 = round(_x(sd, 52, 4) / 48, 1)
    dcV1 = round(_x(sd, 56, 4) / 48, 1)
    dcI1 = round(_x(sd, 60, 4) * 0.0125, 2)
    dcI0 = round(_x(sd, 64, 4) * 0.0125, 2)
    acv  = round(_x(sd, 68, 4) / 3.8, 1)
    freq = round(_x(sd, 72, 4) / 100, 1)
    temp = round(_x(sd, 96, 4) * 0.0198 - 23.84, 1)
    t_new= _x(sd, 76, 4)
    en0_Wh = round((_x(sd, 100, 8) / 1000 / 100) * 1.66, 2)
    en1_Wh = round((_x(sd, 108, 8) / 1000 / 100) * 1.66, 2)
    return {
        "inverter_type": "DS3",
        "serial": sd[0:12],
        "signal_quality_pct": 0,
        "ac_voltage_V": acv, "ac_freq_Hz": freq, "temperature_C": temp,
        "timestamp_raw": t_new,
        "panels": [
            {"dc_voltage_V": dcV0, "dc_current_A": dcI0,
             "instant_power_W": round(dcV0 * dcI0, 1),
             "energy_today_Wh": en0_Wh},
            {"dc_voltage_V": dcV1, "dc_current_A": dcI1,
             "instant_power_W": round(dcV1 * dcI1, 1),
             "energy_today_Wh": en1_Wh},
        ],
        "total_energy_today_Wh": round(en0_Wh + en1_Wh, 2),
    }

DECODERS = {"YC600": decode_yc600, "DS3": decode_ds3}


# ─── Persistence ───────────────────────────────────────────────────────────
LOG_DIR     = Path("/opt/aps/logs")
STATUS_PATH = Path("/opt/aps/logs/STATUS_UNIFIED.txt")

def stamp(): return datetime.now().isoformat(timespec="seconds")
def today_str(): return datetime.now().strftime("%Y-%m-%d")

def log_event(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{stamp()}] {msg}"
    with (LOG_DIR / "unified-daemon.log").open("a") as f:
        f.write(line + "\n")
    print(line, flush=True)


# Hot-reloadable verbose toggle. When True, log_debug() is a regular
# log_event(). When False (default), it's a no-op. Updated each cycle
# from /opt/aps/etc/logging.json via _refresh_logging_cfg().
_verbose_logging = False
_logging_cfg_mtime = 0.0

def _refresh_logging_cfg() -> None:
    """Cheap mtime-keyed reload. Called once per poll cycle."""
    global _verbose_logging, _logging_cfg_mtime
    if not LOGGING_CFG_PATH.exists():
        _verbose_logging = False
        return
    try:
        mt = LOGGING_CFG_PATH.stat().st_mtime
        if mt == _logging_cfg_mtime:
            return
        with LOGGING_CFG_PATH.open() as f:
            cfg = json.load(f)
        _verbose_logging = bool(cfg.get("verbose", False))
        _logging_cfg_mtime = mt
    except Exception:
        pass

def log_debug(msg: str) -> None:
    """Verbose-mode log helper. No-op when /opt/aps/etc/logging.json has
    ``verbose: false`` (or the file is absent). Prefix the line with
    [DEBUG] so it's grep-able in journalctl."""
    if _verbose_logging:
        log_event("[DEBUG] " + msg)

def log_decoded(name: str, t: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": stamp(), "inv": name, **t}
    with (LOG_DIR / f"telemetry-decoded-{today_str()}.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")

def log_raw(name: str, inv_id: str, raw_hex: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": stamp(), "inv": name, "inv_id": inv_id, "raw": raw_hex}
    with (LOG_DIR / f"telemetry-{today_str()}.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")


# ─── Retention ─────────────────────────────────────────────────────────────
def _load_retention_days() -> int:
    """Read /opt/aps/etc/retention.json. ``telemetry_days`` of 0 disables
    cleanup entirely (keep forever). Anything else is interpreted as a
    positive integer day count; defaults to DEFAULT_RETENTION_DAYS."""
    if not RETENTION_CFG_PATH.exists():
        return DEFAULT_RETENTION_DAYS
    try:
        with RETENTION_CFG_PATH.open() as f:
            cfg = json.load(f)
        v = int(cfg.get("telemetry_days", DEFAULT_RETENTION_DAYS))
        if v < 0: v = DEFAULT_RETENTION_DAYS
        return v
    except Exception:
        return DEFAULT_RETENTION_DAYS

def run_retention_sweep() -> None:
    """Delete telemetry-*.jsonl and telemetry-decoded-*.jsonl files older
    than the configured retention window. Idempotent and cheap to call
    daily (no-ops if cleanup_days=0 or no files older than threshold)."""
    days = _load_retention_days()
    if days <= 0:
        log_debug(f"retention: cleanup disabled (telemetry_days={days})")
        return
    if not LOG_DIR.exists():
        return
    cutoff = time.time() - days * 86400
    removed = 0
    for p in LOG_DIR.iterdir():
        # Only touch files we own; never touch unified-daemon.log itself
        # or anything we don't recognize.
        if not p.is_file(): continue
        if not p.name.startswith(("telemetry-", "telemetry-decoded-")):
            continue
        if not p.name.endswith(".jsonl"):
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError as e:
            log_event(f"retention: failed to remove {p}: {e}")
    if removed:
        log_event(f"retention: removed {removed} file(s) older than {days}d")
    else:
        log_debug(f"retention: nothing to remove (cutoff = {days}d ago)")

# ─── MQTT publisher (lazy + reloadable) ────────────────────────────────────
class MqttPublisher:
    """Connection-managed MQTT publisher. Reloads config from disk on demand;
    reconnects on parameter change. Publishes per-inverter decoded telemetry
    plus Home Assistant auto-discovery configs."""

    METRICS = [
        # (suffix, label, unit, ha_device_class, ha_state_class, ha_icon)
        ("ac_voltage_V",        "AC voltage",     "V",  "voltage",     "measurement", "mdi:sine-wave"),
        ("ac_freq_Hz",          "AC frequency",   "Hz", "frequency",   "measurement", "mdi:sine-wave"),
        ("temperature_C",       "Temperature",    "°C", "temperature", "measurement", "mdi:thermometer"),
        ("total_energy_today_Wh","Energy today",  "Wh", "energy",      "total_increasing", "mdi:lightning-bolt"),
        ("signal_quality_pct",  "Signal quality", "%",  None,          "measurement", "mdi:signal"),
    ]
    PANEL_METRICS = [
        # (suffix, label, unit, ha_device_class, ha_state_class, ha_icon)
        ("dc_voltage_V",   "DC voltage",   "V", "voltage", "measurement", "mdi:current-dc"),
        ("dc_current_A",   "DC current",   "A", "current", "measurement", "mdi:current-dc"),
        ("instant_power_W","Power",        "W", "power",   "measurement", "mdi:flash"),
        ("energy_today_Wh","Energy today", "Wh","energy",  "total_increasing", "mdi:lightning-bolt"),
    ]

    def __init__(self):
        self.client = None
        self.cfg = None
        self.cfg_mtime = 0.0
        self.discovered: set[str] = set()       # (inv, key) we've published config for

    def _load_cfg(self) -> dict | None:
        if not MQTT_CFG_PATH.exists():
            return None
        try:
            mt = MQTT_CFG_PATH.stat().st_mtime
            if mt == self.cfg_mtime and self.cfg is not None:
                return self.cfg
            with MQTT_CFG_PATH.open() as f:
                self.cfg = json.load(f)
            self.cfg_mtime = mt
            return self.cfg
        except Exception as e:
            log_event(f"mqtt: failed to read {MQTT_CFG_PATH}: {e}")
            return None

    def _connect(self, cfg: dict) -> bool:
        try:
            c = _mqtt.Client(_mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"aps-bridge-{os.getpid()}")
            if cfg.get("username"):
                c.username_pw_set(cfg["username"], cfg.get("password", "") or None)
            c.connect(cfg["host"], int(cfg.get("port", 1883)), keepalive=30)
            c.loop_start()
            self.client = c
            log_event(f"mqtt: connected to {cfg['host']}:{cfg['port']}")
            return True
        except Exception as e:
            log_event(f"mqtt: connect failed: {e}")
            self.client = None
            return False

    def _disconnect(self):
        if self.client:
            try: self.client.loop_stop()
            except Exception: pass
            try: self.client.disconnect()
            except Exception: pass
            self.client = None
            self.discovered.clear()
            log_event("mqtt: disconnected")

    def reconcile(self) -> bool:
        """Re-read config; (dis)connect / reconnect as needed.
        Returns True if a usable connection exists right now."""
        if not _MQTT_AVAILABLE:
            return False
        cfg = self._load_cfg()
        if not cfg or not cfg.get("enabled"):
            if self.client: self._disconnect()
            return False
        # Reconnect if any connection-affecting field changed
        if self.client is None:
            return self._connect(cfg)
        return True

    # Two namespaces:
    #   discovery_root → HA-required discovery root (must be `homeassistant`
    #     or whatever HA's `discovery_prefix` is set to). Used ONLY for the
    #     {root}/sensor/<uniq>/config retained config topics.
    #   state_root → top-level prefix for our actual state/value topics
    #     (default `apsbridge` — third-party tool, distinct from any vendor
    #     namespace). Used for state, label, panel telemetry, and is
    #     embedded as `state_topic` inside the discovery payload so HA
    #     finds the values where they actually live.
    def _topic_root(self, cfg: dict) -> str:
        return (cfg.get("topic_prefix") or "homeassistant").rstrip("/")

    def _state_root(self, cfg: dict) -> str:
        return (cfg.get("state_topic_prefix") or "apsbridge").rstrip("/")

    def publish_telemetry(self, name: str, t: dict, label: str = "") -> None:
        cfg = self._load_cfg() or {}
        if not self.reconcile(): return
        prefix = self._topic_root(cfg)
        state_root = self._state_root(cfg)
        retain = bool(cfg.get("retain", True))
        device_id = f"aps_{name}"
        display = label or name  # what humans see in HA

        # HA auto-discovery (one-shot per metric)
        if cfg.get("ha_discovery"):
            self._publish_discovery(prefix, state_root, device_id,
                                    name, display, t, retain)
        # Publish the friendly label on its own topic for non-HA consumers.
        if label:
            self.client.publish(f"{state_root}/{name}/label", label,
                                qos=0, retain=retain)

        # State topics — under state_root, not the HA discovery root.
        state = {
            "ac_voltage_V": t["ac_voltage_V"],
            "ac_freq_Hz":   t["ac_freq_Hz"],
            "temperature_C": t["temperature_C"],
            "total_energy_today_Wh": t["total_energy_today_Wh"],
            "signal_quality_pct":    t["signal_quality_pct"],
        }
        for k, v in state.items():
            self.client.publish(f"{state_root}/{name}/{k}", str(v),
                                qos=0, retain=retain)
        for i, p in enumerate(t["panels"]):
            for k, v in p.items():
                self.client.publish(f"{state_root}/{name}/panel{i}/{k}", str(v),
                                    qos=0, retain=retain)
        # Combined JSON for any HA template consumers
        self.client.publish(f"{state_root}/{name}/state", json.dumps(t),
                            qos=0, retain=retain)

    def _publish_discovery(self, prefix, state_root, device_id, inv, display, t, retain):
        device = {
            "identifiers": [device_id],
            "name": f"APSystems {display}",
            "model": t.get("inverter_type", "APsystems"),
            "manufacturer": "APsystems",
            "serial_number": t.get("serial"),
            "via_device": "aps_bridge",
        }
        def ensure(uniq, payload):
            if uniq in self.discovered: return
            self.client.publish(
                f"{prefix}/sensor/{uniq}/config",
                json.dumps(payload), qos=0, retain=True)
            self.discovered.add(uniq)

        for key, label, unit, dc, sc, icon in self.METRICS:
            uniq = f"{device_id}_{key}"
            p = {
                # has_entity_name lets HA compose "<device> <name>" itself,
                # so name is just the metric — no repeated inverter label.
                "name": label,
                "has_entity_name": True,
                "unique_id": uniq,
                "state_topic": f"{state_root}/{inv}/{key}",
                "unit_of_measurement": unit,
                "icon": icon,
                "device": device,
                "state_class": sc,
            }
            if dc: p["device_class"] = dc
            ensure(uniq, p)
        for i in range(len(t["panels"])):
            for key, label, unit, dc, sc, icon in self.PANEL_METRICS:
                uniq = f"{device_id}_panel{i}_{key}"
                p = {
                    "name": f"Panel {i + 1} {label}",
                    "has_entity_name": True,
                    "unique_id": uniq,
                    "state_topic": f"{state_root}/{inv}/panel{i}/{key}",
                    "unit_of_measurement": unit,
                    "icon": icon,
                    "device": device,
                    "state_class": sc,
                }
                if dc: p["device_class"] = dc
                ensure(uniq, p)


def _fmt_age(epoch: float | None) -> str:
    """Render seconds since `epoch` as a short human string, or '-' if None."""
    if epoch is None: return "-"
    s = int(time.time() - epoch)
    if s < 60:    return f"{s}s ago"
    if s < 3600:  return f"{s // 60}m ago"
    return f"{s // 3600}h{(s % 3600) // 60}m ago"


def write_status(state: dict) -> None:
    wd = state["watchdog"]
    fw = (f"fw v{wd['fw_version']} ch{wd['fw_channel']}"
          if wd['fw_version'] is not None else "fw ?")
    lines = [f"# APS unified daemon — last update {stamp()}",
             f"Coordinator: {PORT} (Sonoff CC2652P + raw 802.15.4 firmware, {fw})",
             f"Total polls: {state['polls']}  successful: {state['successful']}",
             (f"Watchdog: last pong {_fmt_age(wd['last_pong_ts'])}; "
              f"missed pings={wd['missed_pings']}; "
              f"RX {'ACTIVE' if wd.get('rx_status') == 0x0002 else ('0x%04X' % wd['rx_status'] if wd.get('rx_status') is not None else '?')}; "
              f"recoveries total={wd['total_recoveries']} "
              f"consecutive={wd['consecutive_recoveries']}; "
              f"last recovery {_fmt_age(wd['last_recovery_ts'])}"),
             f"Last telemetry from any inverter: {_fmt_age(wd['last_success_ts'])}",
             ""]
    for name, info in state["inverters"].items():
        suffix = "" if info.get("enabled", True) else "  [DISABLED]"
        line = f"  {name}: polls={info['polls']} telemetry={info['telemetry']}{suffix}"
        line += f"  last_success={_fmt_age(info.get('last_success_ts'))}"
        d = info.get("last_decoded")
        if d:
            line += (f"  AC={d['ac_voltage_V']}V/{d['ac_freq_Hz']}Hz "
                     f"temp={d['temperature_C']}°C totWh={d['total_energy_today_Wh']} "
                     f"Pinst={sum(p['instant_power_W'] for p in d['panels']):.1f}W")
        lines.append(line)
    STATUS_PATH.write_text("\n".join(lines) + "\n")


def _check_stalled(state: dict) -> bool:
    """Watchdog trigger: bridge is stuck if the dongle has missed too many
    consecutive ping replies. Decoupled from any RF / inverter activity —
    a silent dongle is a stuck dongle, regardless of whether the
    inverters happen to be producing right now."""
    return state["watchdog"]["missed_pings"] >= WATCHDOG_MAX_MISSED_PINGS


def _is_daytime() -> bool:
    """Rough day/night window for the runtime RF self-heal escalation.
    Wide margins so it's robust to TZ quirks and seasonal daylight shifts
    — not meant to be precise. Used only to decide whether to escalate a
    stuck-RF state to container restart (yes during the day, no at night
    where 0 replies is expected and a restart loop would be pure noise)."""
    h = datetime.now().hour
    return DAY_HOUR_START <= h < DAY_HOUR_END


# ─── Action queue (set_max_power / reboot / pair) ─────────────────────────
def _drain(bridge, seconds: float) -> None:
    """Discard whatever the bridge delivers over the next `seconds` — used
    between the frames of a multi-frame command sequence."""
    end = time.time() + seconds
    while time.time() < end:
        bridge.poll()
        time.sleep(0.04)


def _collect_reply(bridge, short_addr: int, seconds: float) -> bytes | None:
    """Listen up to `seconds` for a frame from `short_addr` and return its
    application payload (longest seen — full reply beats a stub). Used to
    capture the inverter's answer to a query frame."""
    end = time.time() + seconds
    best = None
    while time.time() < end:
        for ptype, data in bridge.poll():
            if ptype != PKT_RX_FRAME or len(data) < 8:
                continue
            src, payload = extract_payload(data[2:])
            if src == short_addr and payload:
                if best is None or len(payload) > len(best):
                    best = payload
        time.sleep(0.04)
    return best


def _write_action_result(req_path: Path, result: dict) -> None:
    """Persist the outcome of an action to /opt/aps/logs/actions/ and remove
    the request file. Result is the same dict the API returns to the caller."""
    try:
        ACTIONS_LOG_DIR.mkdir(parents=True, exist_ok=True)
        out = ACTIONS_LOG_DIR / f"{int(time.time())}-{req_path.stem}.json"
        out.write_text(json.dumps(
            {"request_file": req_path.name,
             "ts": datetime.now().isoformat(timespec="seconds"),
             "result": result}, indent=2))
    except Exception as e:
        log_event(f"action: failed to write result for {req_path.name}: {e}")
    try:
        req_path.unlink()
    except Exception:
        pass


def process_pending_actions(bridge, state: dict, current: list[tuple],
                            seq: int, aps_ctr: int) -> tuple[int, int]:
    """Drain /opt/aps/etc/actions/ at the top of each poll cycle. Each file
    is one action: ``{"action": "set_max_power"|"reboot"|"pair",
    "inverter": <name>|None, "watts": <int>|None, "serial": <hex>|None,
    "family": <"YC600"|"QS1"|"DS3">|None}``.

    Returns updated (seq, aps_ctr) — same counters used by polling, so each
    action consumes its own pair and we never collide with poll counters.
    Errors are logged + persisted to logs/actions/; bad files are deleted
    to avoid replay loops.
    """
    if not ACTIONS_DIR.exists():
        return seq, aps_ctr
    # Even if the UI somehow drops files here, refuse to act if the user
    # hasn't opted in. Belt-and-braces vs. the web layer's 403 gate.
    if not commands_enabled():
        for req in sorted(ACTIONS_DIR.glob("*.json")):
            _write_action_result(req, {"ok": False,
                "detail": "commands disabled (set commands_enabled in features.json)"})
        return seq, aps_ctr
    by_name = {n: (s, ser, fam) for n, s, ser, fam, _en, _lbl in current}
    for req in sorted(ACTIONS_DIR.glob("*.json")):
        try:
            cmd = json.loads(req.read_text())
            action = cmd.get("action")
            res: dict = {"ok": False, "action": action, "detail": ""}
            if action == "set_max_power":
                name = cmd.get("inverter")
                if name not in by_name:
                    res["detail"] = f"unknown inverter '{name}'"
                else:
                    short_addr, _ser, family = by_name[name]
                    builder = SETPOWER_BUILDERS.get(family)
                    if builder is None:
                        res["detail"] = f"family '{family}' has no set_power builder"
                    else:
                        watts = int(cmd.get("watts", 0))
                        floor = MIN_MAX_POWER_W.get(family, 100)
                        if watts < floor:
                            res["detail"] = f"refusing watts={watts}, floor for {family} is {floor}"
                        else:
                            # Apply the per-inverter calibration offset:
                            # the inverter's throttle drifts (DS3 settles
                            # ~11% high), so we transmit `watts + calib` and
                            # the operator tunes `calib` until actual output
                            # matches the requested watts. effective is
                            # floored at 10 W so a pathological calib can't
                            # produce a zero/negative scaled value.
                            calib = inverter_calib(name)
                            effective = max(10, watts + calib)
                            # Full patience4711 setMaxPower() sequence — the
                            # inverter does not latch a throttle from the
                            # set-power frame alone; it needs the trailing
                            # nonsense + query frames. Each frame consumes
                            # its own seq/aps pair.
                            def _bump():
                                nonlocal seq, aps_ctr
                                seq = (seq + 1) & 0xFF
                                aps_ctr = (aps_ctr + 1) & 0xFF
                                return seq, aps_ctr
                            # Throttle frame(s). The inverter applies the
                            # set-power value per MPPT-channel, so total
                            # output ~= value * panels. DS3 is dual-MPPT and
                            # one frame caps only the addressed channel (our
                            # finding -- patience4711 sends a single frame),
                            # so we send both. YC600/QS1's 1C8C frame has no
                            # channel byte: one frame, fanned to both panels.
                            if family == "DS3":
                                for ch in (1, 2):
                                    s, a = _bump()
                                    bridge.tx_frame(build_setpower_ds3(
                                        short_addr, s, a, effective, channel=ch))
                                    _drain(bridge, 1.0)
                            else:
                                s, a = _bump()
                                bridge.tx_frame(builder(short_addr, s, a, effective))
                                _drain(bridge, 1.0)
                            s, a = _bump()
                            bridge.tx_frame(build_setpower_nonsense(short_addr, s, a))
                            _drain(bridge, 1.0)
                            s, a = _bump()
                            bridge.tx_frame(build_query(short_addr, s, a))
                            reply = _collect_reply(bridge, short_addr, 2.5)
                            programmed = (decode_setpower_query(reply, family)
                                          if reply else None)
                            # Read-back reflects what we transmitted
                            # (effective), so confirm against that.
                            accepted = (programmed is not None
                                        and abs(programmed - effective) <= 10)
                            reply_hex = reply.hex().upper() if reply else None
                            res.update(ok=True, inverter=name, watts=watts,
                                       family=family, calib=calib,
                                       effective_W=effective,
                                       programmed_W=(round(programmed, 1)
                                                     if programmed is not None
                                                     else None),
                                       accepted=accepted,
                                       query_reply_hex=reply_hex)
                            if reply is None:
                                rb = "NO query reply"
                            elif programmed is None:
                                rb = f"undecodable reply ({len(reply)}B)"
                            else:
                                rb = f"{programmed:.0f}W"
                            cal = (f" (calib {calib:+d} → tx {effective}W)"
                                   if calib else "")
                            log_event(
                                f"action: set_max_power {name} = {watts}W{cal} — "
                                f"query read-back: {rb} → "
                                f"{'ACCEPTED' if accepted else 'NOT confirmed'}")
            elif action == "reboot":
                name = cmd.get("inverter")
                if name not in by_name:
                    res["detail"] = f"unknown inverter '{name}'"
                else:
                    short_addr, _ser, _fam = by_name[name]
                    seq = (seq + 1) & 0xFF
                    aps_ctr = (aps_ctr + 1) & 0xFF
                    bridge.tx_frame(build_reboot(short_addr, seq, aps_ctr))
                    log_event(f"action: reboot {name} tx ok")
                    res.update(ok=True, inverter=name)
            elif action == "pair":
                # Placeholder — pairing needs a multi-frame exchange with
                # reply parsing (docs/aps-protocol.md §D); not yet
                # implemented in this daemon.
                res["detail"] = "pair action not yet implemented"
            else:
                res["detail"] = f"unknown action '{action}'"
            _write_action_result(req, res)
        except Exception as e:
            log_event(f"action: failed to process {req.name}: {e}")
            _write_action_result(req, {"ok": False, "detail": str(e)})
    return seq, aps_ctr


# ─── Bridge open + startup RF self-heal ───────────────────────────────────
def _open_bridge():
    """Open the dongle, set channel + TX power, drain the initial chatter.
    Returns ``(bridge, tx_power_dbm)``."""
    b = Bridge(PORT, baud=115200)
    b.open(reset=True)
    b.set_channel(CHANNEL)
    tx_pwr = load_tx_power_dbm()
    b.set_tx_power(tx_pwr)
    time.sleep(0.3)
    for _ in range(10):
        b.poll()
        time.sleep(0.05)
    return b, tx_pwr


def _probe_rf(bridge, inverters) -> int:
    """Send one poll to each enabled inverter and count how many reply with
    a decodable telemetry frame. Used once at startup to detect the
    cold-start 'RF-deaf' state — where a USB re-enumeration leaves the
    CC2652P receiver demodulating nothing even though CMD_IEEE_RX still
    reports ACTIVE and the MCU answers PKT_PING. That state is invisible to
    every firmware-level health check; the only observable symptom is
    'polls go out, nothing comes back', and the only cure is re-opening
    the bridge."""
    seq, aps = 0xE0, 0x10
    replies = 0
    for name, short_addr, serial, family, enabled, _label in inverters:
        if not enabled:
            continue
        seq = (seq + 1) & 0xFF
        aps = (aps + 1) & 0xFF
        bridge.tx_frame(build_poll(short_addr, seq, aps))
        deadline = time.time() + PER_POLL_WAIT_S
        got = False
        while time.time() < deadline and not got:
            for ptype, data in bridge.poll():
                if ptype != PKT_RX_FRAME or len(data) < 8:
                    continue
                src, payload = extract_payload(data[2:])
                if src == short_addr and payload and DECODERS[family](payload):
                    got = True
                    break
            time.sleep(0.04)
        if got:
            replies += 1
    return replies


# ─── Main loop ─────────────────────────────────────────────────────────────
def main() -> int:
    stop = False
    def _sig(*_): nonlocal stop; stop = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    state = {
        "polls": 0, "successful": 0,
        "inverters": {},
        "watchdog": {
            "daemon_start_ts": time.time(),
            "last_success_ts": None,        # informational: last decode anywhere
            "last_pong_ts": None,            # primary trigger: dongle PKT_INFO
            "missed_pings": 0,               # consecutive ping timeouts
            "consecutive_recoveries": 0,
            "no_reply_cycles": 0,            # runtime RF-stall detector
            "rf_heal_attempts": 0,           # bounces since last good cycle
            "total_recoveries": 0,
            "last_recovery_ts": None,
            "fw_version": None,              # learned from first PKT_INFO
            "fw_channel": None,              # learned from PKT_INFO
            "rx_status": None,               # RF core CMD_IEEE_RX status (v5+)
        },
        "retention_last_day": None,         # YYYY-MM-DD of last sweep
    }
    # Initial retention sweep on startup — catches "daemon was offline a
    # while and there's a backlog of stale logs to clean".
    run_retention_sweep()
    state["retention_last_day"] = today_str()
    mqtt_pub = MqttPublisher()
    initial = load_inverters()
    log_event(f"daemon start — polling {len(initial)} inverters via {PORT}; "
              f"MQTT lib available: {_MQTT_AVAILABLE}")

    while not stop:
        try:
            b, tx_pwr = _open_bridge()
            log_event(f"bridge open, channel {CHANNEL} set, tx_power "
                      f"{tx_pwr:+d} dBm applied")

            # Startup RF self-heal. A cold start (USB re-enumeration on a
            # host reboot / container recreate) can leave the radio
            # 'RF-deaf' — see _probe_rf(). Probe once; if no inverter
            # answers, re-open the bridge and retry, up to 3 times. At
            # night, when inverters are genuinely silent, this performs a
            # few harmless re-opens then proceeds — the cost is bounded to
            # startup and the normal poll loop + watchdog take over after.
            for attempt in range(1, 4):
                replies = _probe_rf(b, load_inverters())
                if replies > 0:
                    log_event(f"startup RF check: {replies} inverter(s) "
                              f"replied — RF OK")
                    break
                log_event(f"startup RF check {attempt}/3: 0 replies — "
                          f"re-opening bridge")
                try:
                    b.close()
                except Exception:
                    pass
                time.sleep(0.5)
                b, _ = _open_bridge()
            else:
                log_event("startup RF check: 0 replies after 3 re-opens — "
                          "proceeding (likely night; watchdog continues)")

            seq, aps_ctr = 0xA0, 0x55
            cycle_start = 0.0
            while not stop:
                cycle_start = time.time()
                # Hot-reload verbose-logging setting (cheap mtime check).
                _refresh_logging_cfg()
                # Once-per-day retention sweep — cheap when the day hasn't
                # rolled over, and only walks the logs dir once it does.
                d = today_str()
                if state.get("retention_last_day") != d:
                    log_debug(f"retention: day rollover {state.get('retention_last_day')} → {d}; running sweep")
                    run_retention_sweep()
                    state["retention_last_day"] = d
                current = load_inverters()
                # Drain UI-issued one-shot actions (set_max_power / reboot /
                # pair) before polling. Counters advance so each action
                # consumes its own seq/aps pair.
                seq, aps_ctr = process_pending_actions(b, state, current, seq, aps_ctr)
                # Ensure state has an entry per inverter (newly added show up live)
                for n, s, ser, fam, _en, lbl in current:
                    if n not in state["inverters"]:
                        state["inverters"][n] = {
                            "polls": 0, "telemetry": 0, "last_decoded": None,
                            "last_success_ts": None, "enabled": True,
                            "short": s, "serial": ser, "family": fam,
                            "label": lbl,
                        }
                    else:
                        state["inverters"][n]["label"] = lbl  # hot-reload renames
                # Drop entries that no longer exist
                seen_names = {n for n, *_ in current}
                for stale in [n for n in state["inverters"] if n not in seen_names]:
                    del state["inverters"][stale]

                cycle_telemetry = 0  # successful decodes this cycle (for RF heal)
                for name, short_addr, serial, family, enabled, _label in current:
                    inv = state["inverters"][name]
                    inv["enabled"] = enabled
                    if not enabled:
                        log_debug(f"{name}: skipped (enabled=false)")
                        continue
                    state["polls"] += 1
                    inv["polls"] += 1
                    seq = (seq + 1) & 0xFF
                    aps_ctr = (aps_ctr + 1) & 0xFF
                    b.tx_frame(build_poll(short_addr, seq, aps_ctr))
                    log_debug(f"{name}: tx poll seq={seq:02X} aps={aps_ctr:02X}")

                    deadline = time.time() + PER_POLL_WAIT_S
                    best_payload = None
                    best_wire = None
                    while time.time() < deadline:
                        for ptype, data in b.poll():
                            if ptype != PKT_RX_FRAME or len(data) < 8: continue
                            wire = data[2:]
                            src, payload = extract_payload(wire)
                            if src == short_addr and payload:
                                # Prefer the longest payload (full telemetry > stub)
                                if best_payload is None or len(payload) > len(best_payload):
                                    best_payload = payload
                                    best_wire = wire
                        time.sleep(0.04)

                    if best_payload:
                        t = DECODERS[family](best_payload)
                        if t:
                            cycle_telemetry += 1
                            inv["telemetry"] += 1
                            state["successful"] += 1
                            now = time.time()
                            inv["last_success_ts"] = now
                            inv["last_decoded"] = t
                            # Watchdog: a successful decode proves the bridge
                            # is healthy. Reset consecutive-recovery counter
                            # and update global last-success timestamp.
                            state["watchdog"]["last_success_ts"] = now
                            state["watchdog"]["consecutive_recoveries"] = 0
                            log_decoded(name, t)
                            log_raw(name, f"{short_addr:04X}",
                                    best_wire.hex().upper())
                            log_event(f"{name}: AC={t['ac_voltage_V']}V "
                                      f"totWh={t['total_energy_today_Wh']} "
                                      f"Pinst={sum(p['instant_power_W'] for p in t['panels']):.0f}W")
                            try:
                                mqtt_pub.publish_telemetry(
                                    name, t,
                                    label=inv.get("label") or "")
                            except Exception as _me:
                                log_event(f"mqtt publish {name} failed: {_me}")

                # ── Watchdog heartbeat: probe the dongle directly.
                # PKT_PING → expect PKT_INFO within WATCHDOG_PING_TIMEOUT_S.
                # This is a serial-level probe, independent of RF / inverter
                # state. A silent dongle at any time of day means the bridge
                # is stuck (typical cause: kernel USB-reset half-state from
                # `xhci_hcd` resets we see in dmesg).
                wd = state["watchdog"]
                info = b.ping_with_reply(WATCHDOG_PING_TIMEOUT_S)
                if info is not None:
                    wd["fw_version"] = info["version"]
                    wd["fw_channel"] = info["channel"]
                    wd["rx_status"] = info.get("rx_status")
                if info is not None and info.get("rx_active", True):
                    # Dongle answered AND its RF core RX is ACTIVE — fully
                    # healthy. (rx_active defaults True on v4 firmware that
                    # can't report it, so old firmware behaves as before.)
                    wd["last_pong_ts"] = time.time()
                    wd["missed_pings"] = 0
                    wd["consecutive_recoveries"] = 0
                elif info is not None:
                    # "RF-deaf": the MCU answers on USB but the RF core RX
                    # command is not ACTIVE — the dongle hears nothing. The
                    # old serial-only ping couldn't see this. Count it like
                    # a missed ping so the stall threshold bounces the
                    # bridge (re-running radio_start_rx()).
                    wd["missed_pings"] += 1
                    log_event(f"watchdog: dongle RF-deaf — RX status "
                              f"0x{(info.get('rx_status') or 0):04X} "
                              f"(not ACTIVE); missed={wd['missed_pings']}")
                else:
                    wd["missed_pings"] += 1

                write_status(state)

                if _check_stalled(state):
                    wd["total_recoveries"] += 1
                    wd["consecutive_recoveries"] += 1
                    wd["last_recovery_ts"] = time.time()
                    log_event(
                        f"watchdog: dongle missed {wd['missed_pings']} "
                        f"consecutive PKT_PING replies "
                        f"(threshold {WATCHDOG_MAX_MISSED_PINGS}) — "
                        f"bouncing bridge (recovery #{wd['total_recoveries']}, "
                        f"{wd['consecutive_recoveries']} consecutive)")
                    if wd["consecutive_recoveries"] >= WATCHDOG_MAX_RECOVERIES:
                        log_event(
                            f"watchdog: {wd['consecutive_recoveries']} "
                            f"consecutive recoveries did not restore service "
                            f"— exiting non-zero so systemd full-restarts the "
                            f"process (and re-negotiates USB)")
                        write_status(state)   # snapshot before we exit
                        try: b.close()
                        except Exception: pass
                        return 2
                    write_status(state)        # surface the recovery attempt
                    break                       # exit inner loop, outer try/
                                                # except reconnects the bridge

                # ── Runtime RF self-heal: catch the case where every
                # inverter is silent but the dongle still answers
                # PKT_PING (so the watchdog above can't fire). This is
                # exactly what happens after a redeploy at night — the
                # startup RF check correctly proceeded with "likely
                # night", but if the radio is actually stuck in a deaf
                # state nothing else re-tests it at dawn.
                any_enabled = any(c[4] for c in current)
                if any_enabled and cycle_telemetry == 0:
                    wd["no_reply_cycles"] += 1
                    if wd["no_reply_cycles"] >= MAX_NO_REPLY_CYCLES_BEFORE_HEAL:
                        wd["rf_heal_attempts"] += 1
                        log_event(
                            f"runtime RF heal: {wd['no_reply_cycles']} "
                            f"consecutive cycles with 0 inverter replies — "
                            f"bouncing bridge (attempt #{wd['rf_heal_attempts']})")
                        try: b.close()
                        except Exception: pass
                        time.sleep(0.5)
                        b, _ = _open_bridge()
                        wd["no_reply_cycles"] = 0
                        if _is_daytime() and wd["rf_heal_attempts"] >= MAX_RF_HEAL_ATTEMPTS:
                            log_event(
                                f"runtime RF heal: {wd['rf_heal_attempts']} "
                                f"in-place bounces during daytime didn't "
                                f"restore replies — exiting so the container "
                                f"restarts with fresh USB enumeration")
                            wd["rf_heal_attempts"] = 0
                            write_status(state)
                            try: b.close()
                            except Exception: pass
                            return 2
                        if not _is_daytime():
                            # At night never escalate — keep bouncing
                            # every N cycles until inverters wake.
                            wd["rf_heal_attempts"] = 0
                elif cycle_telemetry > 0:
                    wd["no_reply_cycles"] = 0
                    wd["rf_heal_attempts"] = 0

                # Sleep the rest of the cycle (relative timing so cadence is
                # steady). Interval is re-read each cycle so a UI change to
                # polling.json takes effect on the very next cycle.
                elapsed = time.time() - cycle_start
                remain = max(0, load_poll_interval() - elapsed)
                while not stop and remain > 0:
                    chunk = min(1.0, remain)
                    time.sleep(chunk)
                    remain -= chunk
            b.close()
        except Exception as e:
            log_event(f"loop crash: {e!r}; reconnect in 8s")
            try: b.close()
            except Exception: pass
            time.sleep(8)
    log_event("daemon stop")
    return 0

if __name__ == "__main__":
    sys.exit(main())
