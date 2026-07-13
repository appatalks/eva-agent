"""Sensitive content validation and redaction for Eva memory events.

Provides:
  * Validation of Trust, Sensitivity, ConsentScope enums
  * Recursive credential/secret pattern redaction before persistence
  * Tombstone/deletion event semantics
  * Default consent policy: conversation content is local_only unless
    explicit cloud opt-in
"""

import re

from bridge.config import is_sensitive_env_name

# ── Enum definitions ────────────────────────────────────────────────

VALID_SENSITIVITY = ("public", "normal", "private", "secret")
VALID_CONSENT_SCOPE = ("local_only", "session", "cloud_allowed", "deleted")
DEFAULT_SENSITIVITY = "normal"
DEFAULT_CONSENT_SCOPE = "local_only"
_SYNTHETIC_MEMORY_PREFIX_RE = re.compile(
    r"^(?:test|tmp|dummy|sample|foo|bar)"
    r"(?:[0-9]+|(?:[\s_\-]+(?:test|tmp|dummy|sample|foo|bar|user|person|place|value|data|name|entity|item|[0-9]+))*)$",
    re.IGNORECASE,
)
_SYNTHETIC_MEMORY_COMPACT_RE = re.compile(
    r"^(?:test|tmp|dummy|sample|foo|bar)"
    r"(?:user|person|place|value|data|name|entity|item|bar)[0-9]*$",
    re.IGNORECASE,
)


def is_synthetic_memory_value(value):
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return True
    return (
        _SYNTHETIC_MEMORY_PREFIX_RE.fullmatch(text) is not None
        or _SYNTHETIC_MEMORY_COMPACT_RE.fullmatch(text) is not None
    )

# ── Credential patterns (synthetic/obvious only) ────────────────────

_CREDENTIAL_PATTERNS = [
    # OpenAI-style keys, including segmented project keys. Lookarounds are
    # used instead of \b because underscores are word characters. The length
    # floor deliberately excludes Eva's short ``sk-<12 chars>`` skill IDs.
    re.compile(
        r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])",
        re.ASCII,
    ),
    re.compile(
        r"\b(?:openai|github|google|gemini|azure|aws)[_-]?"
        r"(?:api[_-]?)?(?:key|token|secret)\s*[=:]\s*"
        r"['\"]?[A-Za-z0-9._~+/=-]{16,}['\"]?",
        re.IGNORECASE,
    ),
    # GitHub PATs
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}", re.ASCII),
    re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}(?![A-Za-z0-9_])", re.ASCII),
    # Google API keys (Gemini / Vision)
    re.compile(r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-])", re.ASCII),
    # RFC 6750 b64token characters, including dotted JWTs.
    re.compile(
        r"\bBearer[ \t]+[A-Za-z0-9\-._~+/]{20,}=*",
        re.IGNORECASE | re.ASCII,
    ),
    re.compile(
        r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])",
        re.ASCII,
    ),
    # AWS keys
    re.compile(r"AKIA[0-9A-Z]{16}", re.ASCII),
    # Azure keys (base64 with == ending)
    re.compile(r"[A-Za-z0-9+/]{40,}==", re.ASCII),
    # Generic API key patterns
    re.compile(r"(?:api[_-]?key|token|secret|password)\s*[=:]\s*['\"]?[A-Za-z0-9+/=_\-]{20,}['\"]?", re.IGNORECASE),
]

_REDACTED = "[REDACTED]"


def validate_trust(value):
    """Validate Trust is a finite float in [0, 1]. Returns float or raises."""
    import math
    try:
        t = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Trust must be a float in [0,1], got {value!r}")
    if math.isnan(t) or math.isinf(t):
        raise ValueError(f"Trust must be finite, got {t}")
    if t < 0.0 or t > 1.0:
        raise ValueError(f"Trust must be in [0,1], got {t}")
    return t


def validate_sensitivity(value):
    """Validate sensitivity enum. Returns normalized value or raises."""
    v = str(value or DEFAULT_SENSITIVITY).strip().lower()
    if v not in VALID_SENSITIVITY:
        raise ValueError(f"Sensitivity must be one of {VALID_SENSITIVITY}, got '{value}'")
    return v


def validate_consent_scope(value):
    """Validate consent scope enum. Returns normalized value or raises."""
    v = str(value or DEFAULT_CONSENT_SCOPE).strip().lower()
    if v not in VALID_CONSENT_SCOPE:
        raise ValueError(f"ConsentScope must be one of {VALID_CONSENT_SCOPE}, got '{value}'")
    return v


def should_create_outbox(sensitivity, consent_scope):
    """Return True only when an event is eligible for cloud egress."""
    return consent_scope == "cloud_allowed" and sensitivity != "secret"


def redact_credentials(value):
    """Recursively redact synthetic credential patterns from a value.

    Handles str, dict, and list.  Returns the same type with credentials replaced.
    Never persists obvious bearer/API secrets.
    """
    if isinstance(value, str):
        result = value
        for pattern in _CREDENTIAL_PATTERNS:
            result = pattern.sub(_REDACTED, result)
        return result
    elif isinstance(value, dict):
        result = {}
        for key, item in value.items():
            safe_key = redact_credentials(key) if isinstance(key, str) else key
            if safe_key in result:
                # Multiple secret-shaped keys can collapse to the same marker.
                # Keep every value without retaining any original key material.
                suffix = 2
                candidate = f"{safe_key}#{suffix}"
                while candidate in result:
                    suffix += 1
                    candidate = f"{safe_key}#{suffix}"
                safe_key = candidate
            result[safe_key] = (
                _REDACTED if is_sensitive_env_name(key)
                else redact_credentials(item)
            )
        return result
    elif isinstance(value, (list, tuple)):
        return [redact_credentials(v) for v in value]
    return value


def default_conversation_consent():
    """Default consent for conversation content: local_only."""
    return "local_only"


def is_tombstone_event(event_type):
    """Check if an event type represents a tombstone/deletion."""
    return event_type.endswith(".deleted") or event_type.endswith(".tombstone")
