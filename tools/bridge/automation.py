"""Small, shared safety helpers for the browser and desktop agents."""

import json
import re

from bridge.audit import audit_event


_CANCEL_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never)\s+(?:\w+\s+){0,2}(?:stop|cancel)\b",
    re.I,
)
_CANCEL_CLAUSE_RE = re.compile(
    r"^(?:eva\s*[,;:]?\s*)?(?:please\s+|go\s+ahead\s+and\s+)?"
    r"(?:stop(?:\s+(?:clicking|typing|the\s+(?:task|run|automation)|this|that|it))?|"
    r"cancel(?:\s+(?:the\s+)?(?:task|run|automation|agent))?)\s*[.!?]*$",
    re.I,
)


def is_explicit_cancel(text):
    """Recognize a direct stop request without treating quoted/domain text as one."""
    value = " ".join(str(text or "").replace("’", "'").split())
    if not value or _CANCEL_NEGATION_RE.search(value):
        return False
    for clause in re.split(r"(?:[.!?;]|\bthen\b)", value):
        clause = clause.strip()
        if _CANCEL_CLAUSE_RE.fullmatch(clause):
            return True
    return False


def _json_text(raw):
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    return text


def parse_json_object(raw):
    """Return a model JSON object or None; never return a scalar/list."""
    text = _json_text(raw)
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _valid_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def parse_action(raw, action_kinds, required=None):
    """Parse and minimally validate an action without allowing caller crashes."""
    action = parse_json_object(raw)
    if action is None:
        return {"action": "ask", "question": "Model returned malformed action JSON."}
    kind = action.get("action")
    if not isinstance(kind, str) or kind not in action_kinds:
        return {"action": "ask", "question": "Model returned an unsupported action."}
    required = required or {}
    for field, field_type in required.get(kind, {}).items():
        value = action.get(field)
        valid = isinstance(value, field_type) if field_type is not int else _valid_int(value)
        if field_type is str:
            valid = isinstance(value, str) and bool(value.strip())
        if field_type is list:
            valid = isinstance(value, list)
        if not valid:
            return {"action": "ask", "question": "Model returned malformed action JSON."}
    return action


def action_signature(action):
    """Return a physical-action signature; narration/reason are excluded."""
    if not isinstance(action, dict):
        return ""
    kind = action.get("action")
    fields = {
        "click": ("x", "y"),
        "double_click": ("x", "y"),
        "right_click": ("x", "y"),
        "move": ("x", "y"),
        "click_ref": ("ref",),
        "type_ref": ("ref", "text"),
        "type": ("text",),
        "press": ("key",),
        "hotkey": ("keys",),
        "scroll": ("dy",),
        "wait": ("ms",),
        "navigate": ("url",),
        "launch_app": ("app", "args"),
        "focus_window": ("match",),
        "crop": ("x", "y", "width", "height"),
    }.get(kind)
    if not fields:
        return ""
    return json.dumps(
        [kind] + [action.get(field) for field in fields],
        sort_keys=True,
        separators=(",", ":"),
    )


def recent_signature_count(history, signature, window=6):
    if not signature:
        return 0
    count = 0
    for entry in (history or [])[-window:]:
        if action_signature(entry.get("action", {})) == signature:
            count += 1
    return count


def automation_audit(run_id, event, backend, kind, outcome):
    """Write only operational metadata; goals, text, and pixels stay local."""
    return audit_event(
        "automation." + str(event or "event")[:32],
        correlation_id=str(run_id or ""),
        outcome=str(outcome or "")[:32],
        backend=str(backend or "unknown")[:64],
        kind=str(kind or "run")[:32],
    )