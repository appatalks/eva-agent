"""Stable identity and request envelope for Eva.

Provides:
  * Persisted InstallationId / UserId under ~/.config/eva-standalone
  * RequestEnvelope with strict validation (rejects invalid, never silently generates
    replacements for supplied values except documented server-only fields)
  * Deterministic source message IDs from turn_id + role

IDs are not secrets but are not logged in full or injected into prompts.
"""

import os
import re
import uuid

from bridge import config as _cfg

_ID_DIR = _cfg.EVA_CONFIG_DIR
_INSTALL_ID_PATH = os.path.join(_ID_DIR, "installation_id")
_USER_ID_PATH = os.path.join(_ID_DIR, "user_id")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
_LEGACY_SESSION_RE = re.compile(r"^sess_[A-Za-z0-9_-]{1,120}$", re.I)
_MAX_STRING = 512
_VALID_ACTORS = ("user", "system", "background", "admin")
_VALID_ORIGINS = ("bridge", "browser", "api", "background", "test")


# ── Exceptions ─────────────────────────────────────────────────────

class EnvelopeValidationError(ValueError):
    """Raised when a supplied envelope field is invalid and cannot be accepted."""

    def __init__(self, field, reason):
        self.field = field
        self.reason = reason
        super().__init__(f"Envelope validation failed for '{field}': {reason}")


# ── Persisted IDs ───────────────────────────────────────────────────

def _read_or_create_id(path):
    """Read a UUID from *path*, or generate and persist one (mode 0600)."""
    try:
        if os.path.isfile(path):
            with open(path) as f:
                value = f.read().strip()
            if _UUID_RE.match(value):
                return value
    except OSError:
        pass
    new_id = str(uuid.uuid4())
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, new_id.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"[Identity] Could not persist {os.path.basename(path)}: {exc}")
    return new_id


def get_installation_id():
    """Return the stable per-install UUID (created once)."""
    return _read_or_create_id(_INSTALL_ID_PATH)


def get_user_id():
    """Return the stable per-user UUID (created once)."""
    return _read_or_create_id(_USER_ID_PATH)


# ── Request envelope ───────────────────────────────────────────────

class RequestEnvelope:
    """Validated carrier for per-request identity and correlation IDs.

    Validation policy:
    - Server-owned fields (installation_id, user_id): override from persistence,
      ignore untrusted client values.
    - Client-supplied UUIDs (session_id, turn_id): REJECT if supplied but invalid
      format, rather than silently replacing.
    - Server-generated when absent: request_id, correlation_id (documented
      server-only absent fields).
    - String fields: REJECT if oversized (> 512 chars), never truncate.
    - Enums (actor, origin): REJECT if not in allowed set.
    """

    __slots__ = (
        "request_id",
        "installation_id",
        "user_id",
        "session_id",
        "turn_id",
        "correlation_id",
        "actor",
        "origin",
        "egress_mode",
    )

    def __init__(
        self,
        data=None,
        *,
        installation_id=None,
        user_id=None,
        egress_mode="cloud",
    ):
        d = data if isinstance(data, dict) else {}

        # Server-owned IDs (override untrusted)
        self.installation_id = installation_id or get_installation_id()
        self.user_id = user_id or get_user_id()

        # Server-generated when absent; reject if supplied and invalid
        raw_request_id = d.get("request_id")
        if raw_request_id:
            self.request_id = self._require_uuid(raw_request_id, "request_id")
        else:
            self.request_id = str(uuid.uuid4())

        raw_correlation_id = d.get("correlation_id")
        if raw_correlation_id:
            self.correlation_id = self._require_uuid(raw_correlation_id, "correlation_id")
        else:
            self.correlation_id = self.request_id

        # Client-supplied: reject invalid, accept absent
        raw_session_id = d.get("session_id") or ""
        if raw_session_id:
            self.session_id = self._require_bounded_string(raw_session_id, "session_id")
            if not (_UUID_RE.fullmatch(self.session_id) or _LEGACY_SESSION_RE.fullmatch(self.session_id)):
                raise EnvelopeValidationError(
                    "session_id", "must be a UUID or legacy sess_ identifier"
                )
        else:
            self.session_id = ""

        raw_turn_id = d.get("turn_id")
        if raw_turn_id:
            self.turn_id = self._require_uuid(raw_turn_id, "turn_id")
        else:
            self.turn_id = str(uuid.uuid4())

        # Enums
        raw_actor = d.get("actor") or "user"
        if raw_actor not in _VALID_ACTORS:
            raise EnvelopeValidationError("actor", f"must be one of {_VALID_ACTORS}, got '{raw_actor}'")
        self.actor = raw_actor

        raw_origin = d.get("origin") or "browser"
        if raw_origin not in _VALID_ORIGINS:
            raise EnvelopeValidationError("origin", f"must be one of {_VALID_ORIGINS}, got '{raw_origin}'")
        self.origin = raw_origin

        if egress_mode not in ("offline", "local-network", "cloud"):
            raise EnvelopeValidationError("egress_mode", f"invalid: '{egress_mode}'")
        self.egress_mode = egress_mode

    # ── validation helpers ──────────────────────────────────────────

    @staticmethod
    def _require_uuid(value, field_name):
        """Validate as UUID-shaped string; REJECT if invalid (don't generate replacement)."""
        s = str(value).strip().lower()
        if not _UUID_RE.match(s):
            raise EnvelopeValidationError(field_name, f"invalid UUID format: '{value}'")
        return s

    @staticmethod
    def _require_bounded_string(value, field_name, limit=_MAX_STRING):
        """Validate string length; REJECT if oversized."""
        s = str(value) if value else ""
        if len(s) > limit:
            raise EnvelopeValidationError(field_name, f"length {len(s)} exceeds max {limit}")
        return s

    # ── serialisation ───────────────────────────────────────────────

    def to_dict(self):
        return {s: getattr(self, s) for s in self.__slots__}

    def __repr__(self):
        return (
            f"RequestEnvelope(request_id={self.request_id!r}, "
            f"session_id={self.session_id!r}, turn_id={self.turn_id!r})"
        )
