"""Privacy-safe structured learning signals and standing consent."""

import datetime
import json
import os
import re
import threading
import uuid
from typing import TypedDict

from bridge import config as _cfg

SIGNAL_SOURCES = {"explicit-user", "action-result", "voice-inferred"}
SIGNAL_KINDS = {"feedback", "action-outcome", "voice-diagnostic"}
SIGNAL_STATUSES = {"helpful", "unhelpful", "misunderstood", "done", "error", "cancelled", "declined", "diagnostic"}
SIGNAL_SCOPES = {"session", "user", "global"}
PERMISSION_BASES = {"explicit-user", "standing-consent", "routine-outcome", "none"}
CONSENT_CATEGORIES = {"explicit_feedback", "action_outcomes", "voice_diagnostics", "routine_tools"}
DEFAULT_CONSENT = {
    "explicit_feedback": True,
    "action_outcomes": True,
    "voice_diagnostics": False,
    "routine_tools": False,
}
MAX_SIGNAL_BYTES = 5 * 1024 * 1024
MAX_SIGNAL_COUNT = 2000
MAX_DETAIL_KEYS = 8
MAX_DETAIL_VALUE = 120
MAX_SESSION_ID = 120
MAX_SCOPE = 40
VALUE_BY_KIND = {
    "feedback": {"helpful", "unhelpful", "misunderstood"},
    "action-outcome": {"done", "error", "cancelled", "declined"},
    "voice-diagnostic": {"buffered", "merged", "duplicate", "committed", "interrupted", "error", "denied", "unsupported", "diagnostic"},
}
DETAIL_BY_KIND = {
    "feedback": {"control"},
    "action-outcome": {"agent", "operation"},
    "voice-diagnostic": {"event", "provider", "reason", "chars", "fragments"},
}

_LOCK = threading.RLock()


class LearningSignal(TypedDict, total=False):
    """Provider-independent bounded signal record serialized to JSONL."""
    id: str
    timestamp: str
    source: str
    kind: str
    status: str
    value: object
    confidence: float
    scope: str
    session_id: str
    permission_basis: str
    retention_days: int
    expires_at: str
    applied: dict
    detail: dict


class ActionOutcome(LearningSignal):
    """Structured action result; its detail never contains the action payload."""


def _paths():
    base = _cfg.EVA_CONFIG_DIR
    return os.path.join(base, "learning_signals.jsonl"), os.path.join(base, "learning_consent.json")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(value=None):
    value = value or _now()
    return value.astimezone(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(datetime.timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _clip(value, limit=MAX_DETAIL_VALUE):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return str(value or "").strip()[:limit]


def sanitize_detail(detail):
    """Allow a small scalar metadata vocabulary; never persist arbitrary content."""
    if detail is None:
        return {}
    if not isinstance(detail, dict):
        raise ValueError("detail must be an object")
    blocked = {"prompt", "content", "transcript", "audio", "secret", "token", "authorization", "auth", "key", "password", "body", "response"}
    result = {}
    for key, value in list(detail.items())[:MAX_DETAIL_KEYS]:
        key = str(key).strip().lower().replace("-", "_")
        if not key or key in blocked or any(part in key for part in blocked):
            raise ValueError("detail contains a restricted field")
        if not isinstance(value, (str, int, float, bool)) or isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) > 1000000:
            raise ValueError("detail values must be bounded scalars")
        result[key[:40]] = _clip(value)
    return result


def _category_for(signal):
    if signal["kind"] == "feedback":
        return "explicit_feedback"
    if signal["kind"] == "action-outcome":
        return "action_outcomes"
    return "voice_diagnostics"


def _load_consent_unlocked():
    _, path = _paths()
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        data = {}
    profile = dict(DEFAULT_CONSENT)
    if isinstance(data, dict):
        for category in CONSENT_CATEGORIES:
            if isinstance(data.get(category), bool):
                profile[category] = data[category]
        retention = data.get("retention_days")
        if isinstance(retention, int) and not isinstance(retention, bool):
            profile["retention_days"] = max(1, min(retention, 3650))
        profile["updated_at"] = str(data.get("updated_at") or "")[:40]
    profile.setdefault("retention_days", 30)
    return profile


def get_consent():
    with _LOCK:
        return _load_consent_unlocked()


def update_consent(changes):
    if not isinstance(changes, dict):
        raise ValueError("consent must be an object")
    unknown = set(changes) - CONSENT_CATEGORIES - {"retention_days"}
    if unknown:
        raise ValueError("unknown consent category")
    profile = get_consent()
    for category in CONSENT_CATEGORIES:
        if category in changes and not isinstance(changes[category], bool):
            raise ValueError("consent categories must be boolean")
        if category in changes:
            profile[category] = changes[category]
    if "retention_days" in changes:
        retention = changes["retention_days"]
        if not isinstance(retention, int) or isinstance(retention, bool) or not 1 <= retention <= 3650:
            raise ValueError("retention_days must be an integer from 1 to 3650")
        profile["retention_days"] = retention
    profile["updated_at"] = _iso()
    _, path = _paths()
    with _LOCK:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(profile, handle, separators=(",", ":"))
        os.replace(temp, path)
    return profile


def _consent_allows(signal):
    return bool(get_consent().get(_category_for(signal), False))


def _validate_signal(data):
    if not isinstance(data, dict):
        raise ValueError("signal must be an object")
    required = {"source", "kind", "status", "confidence", "scope", "permission_basis"}
    missing = required - set(data)
    if missing:
        raise ValueError("missing signal fields")
    if data["source"] not in SIGNAL_SOURCES or data["kind"] not in SIGNAL_KINDS or data["status"] not in SIGNAL_STATUSES:
        raise ValueError("unsupported signal enum")
    if data["scope"] not in SIGNAL_SCOPES or data["permission_basis"] not in PERMISSION_BASES:
        raise ValueError("unsupported scope or permission basis")
    confidence = data["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    session_id = str(data.get("session_id") or "")[:MAX_SESSION_ID]
    if data["scope"] == "session" and not session_id:
        raise ValueError("session scope requires session_id")
    if data.get("value") is not None and not isinstance(data["value"], (str, int, float, bool)):
        raise ValueError("value must be a scalar")
    detail = sanitize_detail(data.get("detail"))
    if set(detail) - DETAIL_BY_KIND[data["kind"]]:
        raise ValueError("detail contains unsupported metadata")
    value = data.get("value")
    if value is not None and str(value) not in VALUE_BY_KIND[data["kind"]]:
        raise ValueError("value is not allowed for signal kind")
    supplied_id = data.get("id")
    if supplied_id is not None and not re.fullmatch(r"[0-9a-fA-F-]{8,80}", str(supplied_id)):
        raise ValueError("id must be a bounded identifier")
    now = _now()
    retention_days = get_consent().get("retention_days", 30)
    # Retention is a server-side policy. Client timestamps and expiration dates
    # are deliberately ignored so a renderer cannot extend storage lifetime.
    timestamp = now
    expires_at = now + datetime.timedelta(days=retention_days)
    signal = {
        "id": str(data.get("id") or uuid.uuid4()),
        "timestamp": _iso(timestamp),
        "source": data["source"],
        "kind": data["kind"],
        "status": data["status"],
        "value": _clip(value),
        "confidence": round(float(confidence), 4),
        "scope": data["scope"],
        "session_id": session_id,
        "permission_basis": data["permission_basis"],
        "retention_days": retention_days,
        "expires_at": _iso(expires_at),
        "applied": {"status": "pending", "effect": ""},
        "detail": detail,
    }
    if len(json.dumps(signal, ensure_ascii=True)) > 8192:
        raise ValueError("signal is too large")
    return signal


def _read_unlocked():
    path, _ = _paths()
    rows = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                except ValueError:
                    continue
    except OSError:
        pass
    return rows[-MAX_SIGNAL_COUNT:]


def _write_unlocked(rows):
    path, _ = _paths()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temp = path + ".tmp"
    bounded = []
    total_bytes = 0
    for row in reversed(rows[-MAX_SIGNAL_COUNT:]):
        encoded = (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        if bounded and total_bytes + len(encoded) > MAX_SIGNAL_BYTES:
            break
        if not bounded and len(encoded) > MAX_SIGNAL_BYTES:
            continue
        bounded.append((row, encoded))
        total_bytes += len(encoded)
    bounded.reverse()
    with open(temp, "w", encoding="utf-8") as handle:
        for _, encoded in bounded:
            handle.buffer.write(encoded)
    os.replace(temp, path)


def _purge_unlocked(rows):
    now = _now()
    return [row for row in rows if _parse_time(row.get("expires_at")) and _parse_time(row.get("expires_at")) > now]


def create_signal(data):
    signal = _validate_signal(data)
    if not _consent_allows(signal):
        return None, "consent_denied"
    with _LOCK:
        rows = _purge_unlocked(_read_unlocked())
        rows.append(signal)
        _write_unlocked(rows)
    return signal, ""


def list_signals(scope=None, session_id=None, limit=100):
    if scope and scope not in SIGNAL_SCOPES:
        raise ValueError("unsupported scope")
    limit = max(1, min(int(limit or 100), 200))
    with _LOCK:
        rows = _purge_unlocked(_read_unlocked())
        _write_unlocked(rows)
    result = [row for row in rows if (not scope or row.get("scope") == scope) and (not session_id or row.get("session_id") == str(session_id)[:MAX_SESSION_ID])]
    return result[-limit:]


def delete_signals(signal_id=None, scope=None, session_id=None, delete_all=False):
    if scope and scope not in SIGNAL_SCOPES:
        raise ValueError("unsupported scope")
    if scope == "session" and not session_id:
        raise ValueError("session scope requires session_id")
    if delete_all and (signal_id or scope or session_id):
        raise ValueError("delete_all cannot be combined with selectors")
    if not signal_id and not scope and not delete_all:
        raise ValueError("delete requires an id, scope, or explicit delete_all")

    def matches(row):
        if delete_all:
            return True
        if signal_id and row.get("id") != signal_id:
            return False
        if scope and row.get("scope") != scope:
            return False
        if session_id and row.get("session_id") != session_id:
            return False
        return bool(signal_id or scope or session_id)

    with _LOCK:
        rows = _purge_unlocked(_read_unlocked())
        kept = [row for row in rows if not matches(row)]
        deleted = len(rows) - len(kept)
        _write_unlocked(kept)
    return deleted


def mark_applied(signal_id, effect):
    effect = str(effect or "")[:MAX_DETAIL_VALUE]
    with _LOCK:
        rows = _purge_unlocked(_read_unlocked())
        for row in rows:
            if row.get("id") == signal_id:
                row["applied"] = {"status": "applied", "effect": effect}
        _write_unlocked(rows)
    return next((row for row in rows if row.get("id") == signal_id), None)


def reset_for_tests():
    with _LOCK:
        path, consent = _paths()
        for item in (path, consent, path + ".tmp", consent + ".tmp"):
            try:
                os.remove(item)
            except OSError:
                pass