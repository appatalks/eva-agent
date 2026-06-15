"""Bridge domain: alerts."""

import datetime
import json
import os
import re
import subprocess
import sys
import time
from bridge import config as _cfg
from bridge import state as _st
from bridge.telemetry import _telemetry_emit

_utc_now = _cfg.utc_now
_to_utc_iso = _cfg.to_utc_iso

_ALERTS_CONFIG_PATH = _cfg.ALERTS_CONFIG_PATH
_ALERT_CHANNELS = _cfg.ALERT_CHANNELS
_ALERT_TYPES = _cfg.ALERT_TYPES
_DEFAULT_ALERT_SETTINGS = _cfg.DEFAULT_ALERT_SETTINGS
_NOTIFY_CRITICAL_SALIENCE = _cfg.NOTIFY_CRITICAL_SALIENCE
_NOTIFY_MAX_BYTES = _cfg.NOTIFY_MAX_BYTES
_NOTIFY_PATH = _cfg.NOTIFY_PATH
_NOTIFY_RING_MAX = _cfg.NOTIFY_RING_MAX
_SIGNAL_CLI_PATH = _cfg.SIGNAL_CLI_PATH
_SIGNAL_SENDER = _cfg.SIGNAL_SENDER
_SIGNAL_RECIPIENT = _cfg.SIGNAL_RECIPIENT
_SIGNAL_SEND_TIMEOUT = _cfg.SIGNAL_SEND_TIMEOUT


def _signal_send(message):
    """Send a text message via signal-cli. Returns True on success.
    Reads sender/recipient from alerts settings (UI), falling back to env vars."""
    settings = _load_alerts().get("settings", {})
    sender = settings.get("signal_sender") or _SIGNAL_SENDER
    recipient = settings.get("signal_recipient") or _SIGNAL_RECIPIENT
    if not sender or not recipient:
        print("[Signal] Skipped: no sender or recipient configured", file=sys.stderr)
        return False
    try:
        subprocess.run(
            [_SIGNAL_CLI_PATH, "-u", sender, "send",
             "-m", message, recipient],
            capture_output=True, text=True, timeout=_SIGNAL_SEND_TIMEOUT,
            check=True,
        )
        print(f"[Signal] Sent to {recipient[:4]}...")
        return True
    except FileNotFoundError:
        print(f"[Signal] signal-cli not found at: {_SIGNAL_CLI_PATH}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("[Signal] Send timed out", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as exc:
        print(f"[Signal] Send failed: {exc.stderr[:200] if exc.stderr else exc}", file=sys.stderr)
        return False


def _alerts_default_doc():
    return {"alerts": [], "settings": dict(_DEFAULT_ALERT_SETTINGS)}



def _load_alerts():
    """Load the alert rules + settings document. Returns a normalized dict with
    'alerts' (list) and 'settings' (dict). Never raises."""
    doc = _alerts_default_doc()
    try:
        if os.path.isfile(_ALERTS_CONFIG_PATH):
            with open(_ALERTS_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                rules = data.get("alerts")
                if isinstance(rules, list):
                    doc["alerts"] = [r for r in rules if isinstance(r, dict)]
                settings = data.get("settings")
                if isinstance(settings, dict):
                    for k in _DEFAULT_ALERT_SETTINGS:
                        if k in settings:
                            doc["settings"][k] = settings[k]
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[Bridge] Could not load alerts config: {exc}", file=sys.stderr)
    return doc



def _save_alerts(doc):
    """Persist the alert rules document atomically. Never raises."""
    try:
        os.makedirs(os.path.dirname(_ALERTS_CONFIG_PATH), exist_ok=True)
        tmp = _ALERTS_CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        os.replace(tmp, _ALERTS_CONFIG_PATH)
        return True
    except (OSError, TypeError) as exc:
        print(f"[Bridge] Could not save alerts config: {exc}", file=sys.stderr)
        return False



def _alert_clip(value, limit):
    s = "" if value is None else str(value)
    s = s.strip()
    return s if len(s) <= limit else s[:limit]



def _sanitize_alert_rule(raw, existing=None):
    """Validate and normalize a single rule dict from a client request.
    Returns (rule, error). Generates an id when absent. Preserves server-side
    bookkeeping (last_fired_iso, last_hash) from an existing rule when updating."""
    if not isinstance(raw, dict):
        return None, "rule must be an object"
    rtype = _alert_clip(raw.get("type"), 32)
    if rtype not in _ALERT_TYPES:
        return None, "type must be one of: " + ", ".join(_ALERT_TYPES)
    label = _alert_clip(raw.get("label"), 80)
    if not label:
        return None, "label is required"
    rid = _alert_clip(raw.get("id"), 64)
    if rid and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid):
        return None, "id is invalid"
    if not rid:
        rid = "alr-" + uuid.uuid4().hex[:10]

    params_in = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    params = {}
    if rtype == "sec_filing":
        symbols_raw = params_in.get("symbols")
        if isinstance(symbols_raw, str):
            symbols_raw = re.split(r"[,\s]+", symbols_raw)
        symbols = []
        for sym in (symbols_raw or []):
            s = _alert_clip(sym, 8).upper()
            if re.fullmatch(r"[A-Z.]{1,8}", s) and s not in symbols:
                symbols.append(s)
        if not symbols:
            return None, "sec_filing requires at least one ticker symbol"
        params["symbols"] = symbols[:12]
    elif rtype == "weather":
        location = _alert_clip(params_in.get("location"), 80)
        if not location:
            return None, "weather requires a location"
        params["location"] = location
        params["condition"] = _alert_clip(params_in.get("condition"), 160) or "severe weather, storms, or warnings"
    elif rtype == "space_weather":
        params["threshold"] = _alert_clip(params_in.get("threshold"), 40) or "Kp 5+, G1+, R1+, or S1+"
    elif rtype == "keyword_watch":
        topic = _alert_clip(params_in.get("topic"), 160)
        if not topic:
            return None, "keyword_watch requires a topic"
        params["topic"] = topic
    elif rtype == "research_question":
        question = _alert_clip(params_in.get("question"), 240)
        if not question:
            return None, "research_question requires a question"
        params["question"] = question

    channels = []
    for ch in (raw.get("channels") or ["chat", "voice"]):
        c = _alert_clip(ch, 12)
        if c in _ALERT_CHANNELS and c not in channels:
            channels.append(c)
    if not channels:
        channels = ["chat"]

    try:
        cooldown = int(raw.get("cooldown_min", 1440))
    except (TypeError, ValueError):
        cooldown = 1440
    cooldown = max(60, min(cooldown, 20160))  # 1 hour to 14 days

    rule = {
        "id": rid,
        "label": label,
        "type": rtype,
        "params": params,
        "cooldown_min": cooldown,
        "channels": channels,
        "enabled": bool(raw.get("enabled", True)),
        "last_fired_iso": (existing or {}).get("last_fired_iso", ""),
        "last_hash": (existing or {}).get("last_hash", ""),
    }
    return rule, ""



def _sanitize_alert_settings(raw):
    settings = dict(_DEFAULT_ALERT_SETTINGS)
    if not isinstance(raw, dict):
        return settings
    for key in ("quiet_hours_start", "quiet_hours_end"):
        try:
            val = int(raw.get(key, -1))
        except (TypeError, ValueError):
            val = -1
        settings[key] = val if -1 <= val <= 23 else -1
    try:
        settings["max_per_hour"] = max(1, min(int(raw.get("max_per_hour", 4)), 60))
    except (TypeError, ValueError):
        settings["max_per_hour"] = 4
    try:
        sal = float(raw.get("min_salience", 0.0))
    except (TypeError, ValueError):
        sal = 0.0
    settings["min_salience"] = max(0.0, min(sal, 1.0))
    # Signal phone numbers (E.164 format)
    _PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")
    for key in ("signal_sender", "signal_recipient"):
        val = str(raw.get(key, "")).strip()
        if val and _PHONE_RE.match(val):
            settings[key] = val
    return settings



def _alert_cooldown_elapsed(rule, now):
    """True if the rule has never fired or its cooldown window has passed."""
    last = rule.get("last_fired_iso", "")
    if not last:
        return True
    try:
        last_dt = datetime.datetime.fromisoformat(last.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=datetime.timezone.utc)
    elapsed_min = (now - last_dt).total_seconds() / 60.0
    return elapsed_min >= rule.get("cooldown_min", 1440)



def _alert_build_prompt(rule):
    """Compose the background agent prompt for a rule. The agent must answer with
    a leading ALERT: or QUIET: token so the tick can decide whether to surface."""
    rtype = rule.get("type")
    params = rule.get("params", {})
    head = ("Background watch task (no user is present). Reply with plain text only. "
            "Begin your reply with 'ALERT:' if there is something genuinely new and "
            "noteworthy to report right now, otherwise begin with 'QUIET:'. ")
    if rtype == "sec_filing":
        syms = ", ".join(params.get("symbols", []))
        return head + (
            f"Check the most recent SEC EDGAR filings for these companies by ticker: {syms}. "
            "Consider only filings from the last 7 days. If you find one, ALERT with the form "
            "type, date, and a one-line description for each. If none, reply QUIET.")
    if rtype == "weather":
        return head + (
            f"Check the weather forecast for {params.get('location', '')} for the next 24 to 48 hours. "
            f"ALERT only if any of these are expected: {params.get('condition', '')}. "
            "If ALERT, give one or two short lines with timing. Otherwise QUIET.")
    if rtype == "space_weather":
        return head + (
            "Report current space weather using NOAA SWPC: the latest planetary Kp index and any active "
            f"geomagnetic storm, solar flare, or radiation alerts. ALERT only if at or above {params.get('threshold', 'moderate')}. "
            "If ALERT, give one or two short lines. Otherwise QUIET.")
    if rtype == "keyword_watch":
        return head + (
            f"Search the web for genuinely new developments about: {params.get('topic', '')}. "
            "Consider only items from roughly the last day or two. If you find something notable, ALERT with a "
            "one to three sentence summary including the key fact and source name. Otherwise QUIET.")
    if rtype == "research_question":
        return head + (
            f"Investigate and determine the current answer to this standing question: {params.get('question', '')}. "
            "ALERT only if the answer has a notable update or newly significant development. If ALERT, give the "
            "answer in one to three sentences. Otherwise QUIET.")
    return None



def _alert_salience(rule, body):
    """Heuristic 0..1 importance score used by restraint gating."""
    base = {
        "sec_filing": 0.7,
        "weather": 0.65,
        "space_weather": 0.7,
        "keyword_watch": 0.55,
        "research_question": 0.6,
    }.get(rule.get("type"), 0.5)
    low = (body or "").lower()
    if re.search(r"\b(severe|urgent|critical|warning|emergency|immediately|major)\b", low):
        base = min(1.0, base + 0.2)
    return round(base, 2)



def _notify_count_last_hour(now):
    cutoff = now - datetime.timedelta(hours=1)
    n = 0
    for rec in _st.notify_ring:
        try:
            ts = datetime.datetime.fromisoformat(str(rec.get("ts", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        if ts >= cutoff:
            n += 1
    return n



def _notify_in_quiet_hours(settings, now):
    start = settings.get("quiet_hours_start", -1)
    end = settings.get("quiet_hours_end", -1)
    if start < 0 or end < 0 or start == end:
        return False
    hour = now.astimezone().hour
    if start < end:
        return start <= hour < end
    # window wraps past midnight, e.g. 22 -> 7
    return hour >= start or hour < end



def _notify_enqueue(title, body, source, salience, channels, settings=None):
    """Apply restraint, then append a notification to the ring + JSONL. Returns
    the stored record, or None when suppressed. Never raises."""
    try:
        if settings is None:
            settings = _load_alerts().get("settings", dict(_DEFAULT_ALERT_SETTINGS))
        now = _utc_now()
        critical = salience >= _NOTIFY_CRITICAL_SALIENCE
        if salience < settings.get("min_salience", 0.0):
            _telemetry_emit("notify", result="suppressed", reason="below_min_salience", source=source, salience=salience)
            return None
        if not critical and _notify_in_quiet_hours(settings, now):
            _telemetry_emit("notify", result="suppressed", reason="quiet_hours", source=source, salience=salience)
            return None
        with _st.notify_lock:
            if not critical and _notify_count_last_hour(now) >= settings.get("max_per_hour", 4):
                _telemetry_emit("notify", result="suppressed", reason="rate_cap", source=source, salience=salience)
                return None
            record = {
                "id": "ntf-" + uuid.uuid4().hex[:10],
                "ts": _to_utc_iso(now),
                "title": _alert_clip(title, 120) or "Eva",
                "body": _alert_clip(body, 1200),
                "source": _alert_clip(source, 64),
                "salience": salience,
                "channels": [c for c in (channels or ["chat"]) if c in _ALERT_CHANNELS] or ["chat"],
                "seen": False,
            }
            _st.notify_ring.append(record)
            if len(_st.notify_ring) > _NOTIFY_RING_MAX:
                del _st.notify_ring[:-_NOTIFY_RING_MAX]
            try:
                os.makedirs(os.path.dirname(_NOTIFY_PATH), exist_ok=True)
                if os.path.isfile(_NOTIFY_PATH) and os.path.getsize(_NOTIFY_PATH) >= _NOTIFY_MAX_BYTES:
                    try:
                        os.replace(_NOTIFY_PATH, _NOTIFY_PATH + ".1")
                    except OSError:
                        pass
                with open(_NOTIFY_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
            except OSError:
                pass
        _telemetry_emit("notify", result="emit", source=source, salience=salience,
                        channels=",".join(record["channels"]))
        print(f"[Notify] {record['title']} (salience {salience}, {source})")
        # Dispatch to Signal when requested
        if "signal" in record["channels"]:
            _signal_send(f"{record['title']}\n{record['body']}".strip())
        return record
    except Exception:
        return None



def _notify_mark_seen(ids):
    """Mark ring notifications seen by id. Returns count updated."""
    if not ids:
        return 0
    id_set = set(str(i) for i in ids)
    updated = 0
    with _st.notify_lock:
        for rec in _st.notify_ring:
            if rec.get("id") in id_set and not rec.get("seen"):
                rec["seen"] = True
                updated += 1
    return updated


# ---------------------------------------------------------------------------
# Skills — import, normalize ("Eva'rise"), and fetch external sources
# ---------------------------------------------------------------------------

