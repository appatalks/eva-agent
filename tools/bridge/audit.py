"""Application-owned, privacy-safe audit records for Eva runtime events."""

import json
import os
import re
import threading

from bridge import config as _cfg

_AUDIT_PATH = _cfg.AUDIT_LOG_PATH
_AUDIT_MAX_BYTES = _cfg.AUDIT_LOG_MAX_BYTES
_AUDIT_TEXT_LIMIT = _cfg.AUDIT_LOG_TEXT_LIMIT
_SENSITIVE_KEY_RE = re.compile(r"(?:(?:api|private)[_-]?key|authorization|credential|cookie|password|secret|token)", re.I)
_AUTHORIZATION_HEADER_RE = re.compile(r"\bAuthorization\s*[:=]\s*[^\r\n]*", re.I)
_BEARER_RE = re.compile(r"\bBearer\s+[^\s,;]+", re.I)
_ASSIGNMENT_SECRET_RE = re.compile(
    r"\b(?:api[_-]?key|authorization|credential|cookie|password|secret|token)\s*[:=]\s*[^\s,;]+",
    re.I,
)
_PROVIDER_TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})\b")
_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")
_URL_SECRET_RE = re.compile(r"([?&](?:api[_-]?key|token|signature|sig|password|x-amz-signature|x-amz-credential|x-amz-security-token|x-goog-signature|x-goog-credential)=)[^&#\s]+", re.I)
_audit_lock = threading.Lock()

_RUNTIME_AUDIT_BASE_FIELDS = {
    "ts", "event", "outcome", "backend", "route", "status",
    "kind", "tool_kind", "decision",
    "error_type", "permission_basis", "recipients",
    "requested_backend", "selected_backend", "policy_mode", "reason",
    "requires_tools",
}
_RUNTIME_AUDIT_SUFFIXES = (
    "_count", "_chars", "_bytes", "_ms", "_rate", "_percent", "_attempts",
)


def _runtime_audit_record(record):
    """Project a sanitized audit record into privacy-safe operational metadata."""
    forwarded = {}
    for key, value in (record or {}).items():
        if key in _RUNTIME_AUDIT_BASE_FIELDS or key.endswith(_RUNTIME_AUDIT_SUFFIXES):
            forwarded[key] = value
    return forwarded


def _clip(value, limit=_AUDIT_TEXT_LIMIT):
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _redact_private_key_blocks(text):
    """Redact PEM private-key blocks with linear marker scanning, not regex."""
    marker = "-----BEGIN "
    output = []
    cursor = 0
    while True:
        start = text.find(marker, cursor)
        if start < 0:
            output.append(text[cursor:])
            return "".join(output)
        line_end = text.find("\n", start)
        line_end = len(text) if line_end < 0 else line_end
        header = text[start:line_end]
        if "PRIVATE KEY-----" not in header:
            output.append(text[cursor:start + len(marker)])
            cursor = start + len(marker)
            continue
        footer = header.replace("BEGIN", "END", 1)
        footer_start = text.find(footer, line_end)
        output.append(text[cursor:start])
        output.append("<redacted-private-key>")
        cursor = footer_start + len(footer) if footer_start >= 0 else len(text)


def _sanitize_text(value):
    text = _clip(value)
    text = _redact_private_key_blocks(text)
    text = _AUTHORIZATION_HEADER_RE.sub("Authorization: <redacted>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _ASSIGNMENT_SECRET_RE.sub("<redacted>", text)
    text = _PROVIDER_TOKEN_RE.sub("<redacted>", text)
    return _URL_SECRET_RE.sub(r"\1<redacted>", text)


def _sanitize(value, key=""):
    if _SENSITIVE_KEY_RE.search(str(key or "")):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(item_key)[:80]: _sanitize(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value[:32]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _sanitize_text(value)


def audit_event(event, correlation_id="", outcome="", **fields):
    """Append one sanitized application event without affecting user work."""
    try:
        raw_correlation_id = str(correlation_id or "")
        safe_correlation_id = _sanitize_text(raw_correlation_id)
        record = {
            "ts": _cfg.to_utc_iso(_cfg.utc_now()),
            "event": _sanitize_text(event)[:64],
            "correlation_id": _clip(raw_correlation_id, 120)
            if _CORRELATION_ID_RE.fullmatch(raw_correlation_id) and safe_correlation_id == raw_correlation_id else "invalid",
            "outcome": _sanitize_text(outcome)[:32],
        }
        for key, value in fields.items():
            record[str(key)[:80]] = _sanitize(value, key)
        with _audit_lock:
            os.makedirs(os.path.dirname(_AUDIT_PATH), exist_ok=True)
            if os.path.isfile(_AUDIT_PATH):
                try:
                    os.chmod(_AUDIT_PATH, 0o600)
                except OSError:
                    pass
            if os.path.isfile(_AUDIT_PATH) and os.path.getsize(_AUDIT_PATH) >= _AUDIT_MAX_BYTES:
                try:
                    os.replace(_AUDIT_PATH, _AUDIT_PATH + ".1")
                except OSError:
                    pass
            descriptor = os.open(_AUDIT_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
            try:
                os.chmod(_AUDIT_PATH, 0o600)
            except OSError:
                pass
        if os.environ.get("EVA_RUNTIME_AUDIT_STDOUT") == "1":
            print("[Audit] " + json.dumps(
                _runtime_audit_record(record), sort_keys=True, separators=(",", ":")
            ))
        return record
    except Exception:
        return None