"""Immutable event journal and repository for Eva's event-sourced memory.

The MemoryEvents table is the append-only source of truth for all memory
mutations.  Legacy tables remain read-authoritative in Phase 1; events are
shadow-written alongside legacy writes.

Immutability guarantees:
  * SQLite BEFORE UPDATE/DELETE triggers ABORT any mutation on MemoryEvents.
  * EventRepository exposes NO update/delete methods for events.
  * Delivery state is tracked ONLY in MemoryOutbox/receipts, never on events.

Validation:
  * Rejects (never truncates) oversized stream/event/idempotency fields.
  * Rejects empty event_type/stream, invalid expected_version.
  * Canonical JSON rejects NaN/Infinity, normalizes Unicode NFC.
  * Deterministic EventId/OutboxId via UUIDv5 from installation_id + key.
  * Idempotency collision detected by canonical hash comparison.

Transaction safety:
  * append_event uses BEGIN IMMEDIATE for write serialization.
  * ConcurrentStreamError raised after bounded retry on lock/unique race.
  * Never commits caller transactions; rollback-safe via savepoints.
"""

import datetime
import hashlib
import json
import math
import re
import sqlite3
import threading
import unicodedata
import uuid

# ── Constants ───────────────────────────────────────────────────────
MAX_PAYLOAD_BYTES = 64 * 1024  # 64 KiB
MAX_STRING_LEN = 512
_APPEND_MAX_RETRIES = 3
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)
# UUIDv5 namespace for Eva event IDs (stable, deterministic)
_EVA_EVENT_NS = uuid.UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")


# ── Exceptions ──────────────────────────────────────────────────────

class EventStoreError(Exception):
    """Base exception for event store operations."""


class ConcurrentStreamError(EventStoreError):
    """Raised on expected-version conflict or lock race."""

    def __init__(self, stream_id, expected, actual):
        self.stream_id = stream_id
        self.expected_version = expected
        self.actual_version = actual
        super().__init__(
            f"Stream '{stream_id}': expected version {expected}, "
            f"but current version is {actual}"
        )


class PayloadTooLargeError(EventStoreError):
    """Raised when the event payload exceeds MAX_PAYLOAD_BYTES."""

    def __init__(self, size, max_size=MAX_PAYLOAD_BYTES):
        self.size = size
        self.max_size = max_size
        super().__init__(f"Payload size {size} exceeds maximum {max_size} bytes")


class ValidationError(EventStoreError):
    """Raised when input validation fails (field too long, empty, invalid)."""

    def __init__(self, field, reason):
        self.field = field
        self.reason = reason
        super().__init__(f"Validation failed for '{field}': {reason}")


class IdempotencyCollisionError(EventStoreError):
    """Raised when idempotency key matches but payload/metadata differs."""

    def __init__(self, key, detail=""):
        self.key = key
        self.detail = detail
        super().__init__(f"Idempotency collision on key '{key}': {detail}")


class MemoryQueryError(EventStoreError):
    """Typed/observable query error for repository APIs."""

    def __init__(self, query, error):
        self.query = query
        self.error = error
        super().__init__(f"Query failed: {error}")


class ReadOnlyViolationError(EventStoreError):
    """Raised when a read API is used to execute a write statement."""

    def __init__(self, statement_start):
        super().__init__(
            f"Read-only violation: statement starts with '{statement_start}'"
        )


# ── Canonical JSON ──────────────────────────────────────────────────

def _check_no_special_floats(obj):
    """Recursively check that no NaN/Infinity values exist."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ValidationError("payload", f"NaN/Infinity not allowed in canonical JSON, got {obj}")
    elif isinstance(obj, dict):
        for v in obj.values():
            _check_no_special_floats(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _check_no_special_floats(v)


def _normalize_nfc_recursive(obj):
    """Recursively normalize all strings in obj to NFC Unicode."""
    if isinstance(obj, str):
        return unicodedata.normalize("NFC", obj)
    elif isinstance(obj, dict):
        return {unicodedata.normalize("NFC", k) if isinstance(k, str) else k:
                _normalize_nfc_recursive(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_normalize_nfc_recursive(v) for v in obj]
    return obj


def canonical_json(obj):
    """Deterministic JSON: sorted keys, compact separators, ASCII, NFC Unicode.

    Normalizes all string values to NFC before serialization.
    Raises ValidationError on NaN/Infinity.
    """
    _check_no_special_floats(obj)
    normalized = _normalize_nfc_recursive(obj)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_hash(canonical_bytes):
    """SHA-256 hex digest of canonical JSON bytes."""
    if isinstance(canonical_bytes, str):
        canonical_bytes = canonical_bytes.encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()


def canonical_event_hash(
    *, stream_id, event_type, schema_version, actor_type, actor_id, origin,
    correlation_id, causation_id, session_id, turn_id, source_message_id,
    trust, sensitivity, consent_scope, payload,
):
    """Hash the immutable logical event metadata used for replay comparison."""
    logical = canonical_json({
        "stream_id": str(stream_id),
        "event_type": str(event_type),
        "schema_version": int(schema_version),
        "actor_type": str(actor_type),
        "actor_id": str(actor_id),
        "origin": str(origin),
        "correlation_id": str(correlation_id),
        "causation_id": str(causation_id),
        "session_id": str(session_id),
        "turn_id": str(turn_id),
        "source_message_id": str(source_message_id),
        "trust": float(trust),
        "sensitivity": str(sensitivity),
        "consent_scope": str(consent_scope),
        "payload": payload,
    })
    return payload_hash(logical)


# ── Deterministic ID generation ─────────────────────────────────────

def deterministic_event_id(installation_id, idempotency_key):
    """UUIDv5 from installation namespace + idempotency key."""
    ns = uuid.uuid5(_EVA_EVENT_NS, str(installation_id))
    return str(uuid.uuid5(ns, str(idempotency_key)))


def deterministic_outbox_id(event_id, destination):
    """UUIDv5 from event_id + destination."""
    return str(uuid.uuid5(_EVA_EVENT_NS, f"{event_id}:{destination}"))


def deterministic_source_message_id(turn_id, role, index=0):
    """Stable source message ID derived from turn_id + role + index."""
    return str(uuid.uuid5(_EVA_EVENT_NS, f"{turn_id}:{role}:{index}"))


# ── Validation ──────────────────────────────────────────────────────

def _validate_string(value, field_name, max_len=MAX_STRING_LEN, allow_empty=True):
    """Validate and return string.  REJECTS (never truncates) oversized values."""
    if value is None:
        if not allow_empty:
            raise ValidationError(field_name, "must not be empty")
        return ""
    s = str(value)
    if not allow_empty and not s.strip():
        raise ValidationError(field_name, "must not be empty")
    if len(s) > max_len:
        raise ValidationError(field_name, f"length {len(s)} exceeds max {max_len}")
    return s


def _validate_uuid_shape(value, field_name):
    """Validate UUID-shaped string; returns lowercased or raises."""
    s = str(value or "").strip().lower()
    if not s:
        return ""
    if not _UUID_RE.match(s):
        raise ValidationError(field_name, f"invalid UUID format: '{value}'")
    return s


def _validate_expected_version(value):
    """Validate expected_version is integer >= -1 or None."""
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ValidationError("expected_version", f"must be integer >= -1, got {value!r}")
    if v < -1:
        raise ValidationError("expected_version", f"must be >= -1, got {v}")
    return v


# ── Helpers ─────────────────────────────────────────────────────────

_WRITE_STATEMENT_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|REINDEX|VACUUM|PRAGMA)",
    re.IGNORECASE
)
_READ_STATEMENT_RE = re.compile(
    r"^\s*(SELECT|WITH|EXPLAIN|PRAGMA\s+table_info|PRAGMA\s+index_list)",
    re.IGNORECASE
)


def guard_read_only(sql):
    """Raise ReadOnlyViolationError if sql is not a SELECT/CTE/read PRAGMA."""
    stripped = sql.strip()
    if _WRITE_STATEMENT_RE.match(stripped) or re.search(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|REINDEX|VACUUM)\b",
        stripped,
        re.IGNORECASE,
    ):
        raise ReadOnlyViolationError(stripped[:40])
    if not _READ_STATEMENT_RE.match(stripped):
        # Allow only if it looks like a bare table name (legacy shortcut)
        if " " in stripped or stripped.startswith("."):
            raise ReadOnlyViolationError(stripped[:40])


def _utc_iso_now():
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _row_to_dict(cursor, row):
    """Convert a sqlite3.Row-compatible tuple to a dict."""
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


# ── EventRepository ────────────────────────────────────────────────

class EventRepository:
    """Thread-safe, immutable event store backed by SQLite.

    Requires MemoryEvents, MemoryOutbox, and LegacyProjectionReceipts
    tables (created by migration v1).

    Parameters
    ----------
    get_conn : callable
        Returns a ``sqlite3.Connection`` for the current thread.
    installation_id : str
        Stable per-install UUID used for deterministic ID generation.
    """

    def __init__(self, get_conn, installation_id=""):
        self._get_conn = get_conn
        self._installation_id = installation_id or ""
        self._closed = False
        self._lock = threading.Lock()

    def close(self):
        """Mark repository as closed."""
        self._closed = True

    def _check_open(self):
        if self._closed:
            raise EventStoreError("EventRepository is closed")

    # ── append ──────────────────────────────────────────────────────

    def append_event(
        self,
        *,
        stream_id,
        event_type,
        payload,
        expected_version=None,
        actor_type="system",
        actor_id="",
        origin="bridge",
        occurred_at=None,
        correlation_id="",
        causation_id="",
        session_id="",
        turn_id="",
        source_message_id="",
        trust=0.5,
        sensitivity="normal",
        consent_scope="local_only",
        schema_version=1,
        idempotency_key=None,
        outbox_destination="adx",
    ):
        """Append an immutable event with an outbox entry atomically.

        Returns dict of the event row.  On duplicate IdempotencyKey with
        matching content, returns existing row.  On collision (same key,
        different content), raises IdempotencyCollisionError.

        Outbox entry is only created when consent_scope == 'cloud_allowed'
        and sensitivity != 'secret'.
        """
        self._check_open()

        # ── Validate inputs (reject, never truncate) ────────────────
        stream_id = _validate_string(stream_id, "stream_id", allow_empty=False)
        event_type = _validate_string(event_type, "event_type", allow_empty=False)
        actor_type = _validate_string(actor_type, "actor_type")
        actor_id = _validate_string(actor_id, "actor_id")
        origin = _validate_string(origin, "origin")
        correlation_id = _validate_string(correlation_id, "correlation_id")
        causation_id = _validate_string(causation_id, "causation_id")
        session_id = _validate_string(session_id, "session_id")
        turn_id = _validate_string(turn_id, "turn_id")
        source_message_id = _validate_string(source_message_id, "source_message_id")

        expected_version = _validate_expected_version(expected_version)

        # Trust validation
        try:
            trust = float(trust)
        except (TypeError, ValueError):
            raise ValidationError("trust", f"must be float in [0,1], got {trust!r}")
        if not (0.0 <= trust <= 1.0) or math.isnan(trust) or math.isinf(trust):
            raise ValidationError("trust", f"must be finite float in [0,1], got {trust}")

        # Sensitivity/consent enum validation
        valid_sensitivity = ("public", "normal", "private", "secret")
        if sensitivity not in valid_sensitivity:
            raise ValidationError("sensitivity", f"must be one of {valid_sensitivity}")
        valid_consent = ("local_only", "session", "cloud_allowed", "deleted")
        if consent_scope not in valid_consent:
            raise ValidationError("consent_scope", f"must be one of {valid_consent}")

        # Canonical JSON (rejects NaN/Infinity, normalizes NFC)
        canon = canonical_json(payload)
        canon_bytes = canon.encode("utf-8")
        if len(canon_bytes) > MAX_PAYLOAD_BYTES:
            raise PayloadTooLargeError(len(canon_bytes))

        p_hash = payload_hash(canon_bytes)

        # Deterministic IDs
        if idempotency_key is None:
            idempotency_key = f"{stream_id}:{event_type}:{p_hash}"
        _validate_string(idempotency_key, "idempotency_key", allow_empty=False)

        event_id = deterministic_event_id(self._installation_id, idempotency_key)

        # Determine if outbox entry should be created
        create_outbox = (consent_scope == "cloud_allowed" and sensitivity != "secret")
        outbox_id = deterministic_outbox_id(event_id, outbox_destination) if create_outbox else None

        if occurred_at is None:
            now_iso = _utc_iso_now()
        elif isinstance(occurred_at, datetime.datetime):
            now_iso = occurred_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        else:
            now_iso = str(occurred_at)

        conn = self._get_conn()

        # Retry loop for lock contention
        for attempt in range(_APPEND_MAX_RETRIES):
            try:
                return self._do_append(
                    conn, event_id=event_id, stream_id=stream_id,
                    event_type=event_type, canon=canon, p_hash=p_hash,
                    expected_version=expected_version, actor_type=actor_type,
                    actor_id=actor_id, origin=origin, occurred_at=now_iso,
                    correlation_id=correlation_id, causation_id=causation_id,
                    session_id=session_id, turn_id=turn_id,
                    source_message_id=source_message_id, trust=trust,
                    sensitivity=sensitivity, consent_scope=consent_scope,
                    schema_version=schema_version, idempotency_key=idempotency_key,
                    outbox_id=outbox_id, outbox_destination=outbox_destination,
                    create_outbox=create_outbox,
                )
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < _APPEND_MAX_RETRIES - 1:
                    import time
                    time.sleep(0.01 * (attempt + 1))
                    continue
                raise ConcurrentStreamError(stream_id, expected_version, -1) from e

        raise ConcurrentStreamError(stream_id, expected_version, -1)

    def _do_append(self, conn, *, event_id, stream_id, event_type, canon, p_hash,
                   expected_version, actor_type, actor_id, origin, occurred_at,
                   correlation_id, causation_id, session_id, turn_id,
                   source_message_id, trust, sensitivity, consent_scope,
                   schema_version, idempotency_key, outbox_id, outbox_destination,
                   create_outbox):
        """Internal atomic append with idempotency check."""

        # Check idempotency outside savepoint first (fast path)
        cur = conn.execute(
            "SELECT * FROM MemoryEvents WHERE IdempotencyKey = ?",
            (idempotency_key,),
        )
        existing = cur.fetchone()
        if existing:
            existing_dict = _row_to_dict(cur, existing)
            # Collision check: same key must have same content
            if existing_dict.get("PayloadHash") != p_hash:
                raise IdempotencyCollisionError(
                    idempotency_key,
                    f"hash mismatch: existing={existing_dict.get('PayloadHash')}, new={p_hash}"
                )
            if existing_dict.get("StreamId") != stream_id:
                raise IdempotencyCollisionError(
                    idempotency_key,
                    f"stream mismatch: existing={existing_dict.get('StreamId')}, new={stream_id}"
                )
            if existing_dict.get("EventType") != event_type:
                raise IdempotencyCollisionError(
                    idempotency_key,
                    f"type mismatch: existing={existing_dict.get('EventType')}, new={event_type}"
                )
            return existing_dict

        # Atomic append via SAVEPOINT
        conn.execute("SAVEPOINT event_append")
        try:
            # Determine stream version under write lock
            row = conn.execute(
                "SELECT MAX(StreamVersion) FROM MemoryEvents WHERE StreamId = ?",
                (stream_id,),
            ).fetchone()
            current_max = row[0] if row and row[0] is not None else -1

            if expected_version is not None and expected_version != current_max:
                raise ConcurrentStreamError(stream_id, expected_version, current_max)

            next_version = current_max + 1

            # Re-check idempotency under write lock
            cur2 = conn.execute(
                "SELECT * FROM MemoryEvents WHERE IdempotencyKey = ?",
                (idempotency_key,),
            )
            existing2 = cur2.fetchone()
            if existing2:
                conn.execute("RELEASE event_append")
                return _row_to_dict(cur2, existing2)

            conn.execute(
                "INSERT INTO MemoryEvents ("
                "  EventId, StreamId, StreamVersion, EventType, SchemaVersion,"
                "  ActorType, ActorId, Origin, OccurredAt,"
                "  CorrelationId, CausationId, SessionId, TurnId, SourceMessageId,"
                "  Trust, Sensitivity, ConsentScope,"
                "  Payload, PayloadHash, IdempotencyKey"
                ") VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?,?, ?,?,?)",
                (
                    event_id, stream_id, next_version, event_type, schema_version,
                    actor_type, actor_id, origin, occurred_at,
                    correlation_id, causation_id, session_id, turn_id, source_message_id,
                    trust, sensitivity, consent_scope,
                    canon, p_hash, idempotency_key,
                ),
            )

            if create_outbox and outbox_id:
                conn.execute(
                    "INSERT INTO MemoryOutbox "
                    "(OutboxId, EventId, Destination, Status, Attempts) "
                    "VALUES (?, ?, ?, 'pending', 0)",
                    (outbox_id, event_id, outbox_destination),
                )

            conn.execute("RELEASE event_append")
        except ConcurrentStreamError:
            conn.execute("ROLLBACK TO event_append")
            conn.execute("RELEASE event_append")
            raise
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK TO event_append")
            conn.execute("RELEASE event_append")
            exc_str = str(exc)
            if "IdempotencyKey" in exc_str or "uq_events_idempotency" in exc_str:
                cur3 = conn.execute(
                    "SELECT * FROM MemoryEvents WHERE IdempotencyKey = ?",
                    (idempotency_key,),
                )
                row3 = cur3.fetchone()
                if row3:
                    return _row_to_dict(cur3, row3)
            if "StreamVersion" in exc_str or "uq_events_stream_ver" in exc_str:
                actual_row = conn.execute(
                    "SELECT MAX(StreamVersion) FROM MemoryEvents WHERE StreamId = ?",
                    (stream_id,),
                ).fetchone()
                actual = actual_row[0] if actual_row and actual_row[0] is not None else -1
                raise ConcurrentStreamError(stream_id, expected_version, actual) from exc
            raise EventStoreError(f"Append failed: {exc}") from exc
        except Exception:
            conn.execute("ROLLBACK TO event_append")
            conn.execute("RELEASE event_append")
            raise

        conn.commit()

        return {
            "EventId": event_id,
            "JournalSequence": None,  # filled by autoincrement
            "StreamId": stream_id,
            "StreamVersion": next_version,
            "EventType": event_type,
            "SchemaVersion": schema_version,
            "ActorType": actor_type,
            "ActorId": actor_id,
            "Origin": origin,
            "OccurredAt": occurred_at,
            "RecordedAt": _utc_iso_now(),
            "CorrelationId": correlation_id,
            "CausationId": causation_id,
            "SessionId": session_id,
            "TurnId": turn_id,
            "SourceMessageId": source_message_id,
            "Trust": trust,
            "Sensitivity": sensitivity,
            "ConsentScope": consent_scope,
            "Payload": canon,
            "PayloadHash": p_hash,
            "IdempotencyKey": idempotency_key,
        }

    # ── reads ───────────────────────────────────────────────────────

    def get_event(self, event_id):
        """Single event by EventId.  Returns dict or None."""
        self._check_open()
        conn = self._get_conn()
        cur = conn.execute("SELECT * FROM MemoryEvents WHERE EventId = ?", (event_id,))
        row = cur.fetchone()
        return _row_to_dict(cur, row)

    def list_stream(self, stream_id, from_version=0, limit=1000):
        """Events in a stream ordered by StreamVersion."""
        self._check_open()
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM MemoryEvents WHERE StreamId = ? AND StreamVersion >= ? "
            "ORDER BY StreamVersion LIMIT ?",
            (stream_id, from_version, limit),
        )
        return [_row_to_dict(cur, r) for r in cur.fetchall()]

    def events_since(self, cursor_sequence=0, limit=1000):
        """Events with JournalSequence > cursor_sequence, ordered monotonically.

        Uses JournalSequence (autoincrement PK) for gap-free cursor pagination.
        Returns actual DB rows including RecordedAt and JournalSequence.
        """
        self._check_open()
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM MemoryEvents WHERE JournalSequence > ? "
            "ORDER BY JournalSequence LIMIT ?",
            (cursor_sequence, limit),
        )
        return [_row_to_dict(cur, r) for r in cur.fetchall()]

    def events_since_timestamp(self, recorded_after, limit=1000):
        """Events recorded after the given ISO timestamp (legacy compat)."""
        self._check_open()
        conn = self._get_conn()
        cur = conn.execute(
            "SELECT * FROM MemoryEvents WHERE RecordedAt > ? ORDER BY JournalSequence LIMIT ?",
            (recorded_after, limit),
        )
        return [_row_to_dict(cur, r) for r in cur.fetchall()]

    # ── outbox ──────────────────────────────────────────────────────

    def claim_outbox(self, limit=50, destination=None):
        """Atomically claim pending outbox entries for processing.

        Filters by NextAttemptAt <= now OR empty.  Marks claimed as 'processing'.
        Returns list of dicts with joined event data.
        """
        self._check_open()
        conn = self._get_conn()
        now = _utc_iso_now()

        if destination:
            cur = conn.execute(
                "SELECT o.*, e.Payload, e.EventType, e.StreamId, e.OccurredAt, "
                "e.Sensitivity, e.ConsentScope "
                "FROM MemoryOutbox o "
                "JOIN MemoryEvents e ON o.EventId = e.EventId "
                "WHERE o.Status IN ('pending', 'retry') "
                "AND (o.NextAttemptAt <= ? OR o.NextAttemptAt = '' OR o.NextAttemptAt IS NULL) "
                "AND o.Attempts < o.MaxAttempts "
                "AND o.Destination = ? "
                "ORDER BY o.CreatedAt LIMIT ?",
                (now, destination, limit),
            )
        else:
            cur = conn.execute(
                "SELECT o.*, e.Payload, e.EventType, e.StreamId, e.OccurredAt, "
                "e.Sensitivity, e.ConsentScope "
                "FROM MemoryOutbox o "
                "JOIN MemoryEvents e ON o.EventId = e.EventId "
                "WHERE o.Status IN ('pending', 'retry') "
                "AND (o.NextAttemptAt <= ? OR o.NextAttemptAt = '' OR o.NextAttemptAt IS NULL) "
                "AND o.Attempts < o.MaxAttempts "
                "ORDER BY o.CreatedAt LIMIT ?",
                (now, limit),
            )
        entries = [_row_to_dict(cur, r) for r in cur.fetchall()]

        # Mark as processing
        for entry in entries:
            conn.execute(
                "UPDATE MemoryOutbox SET Status = 'processing', UpdatedAt = ? WHERE OutboxId = ?",
                (now, entry["OutboxId"]),
            )
        conn.commit()
        return entries

    def complete_outbox(self, event_id, destination="adx"):
        """Mark outbox entry as projected and record receipt."""
        self._check_open()
        conn = self._get_conn()
        now = _utc_iso_now()
        conn.execute(
            "UPDATE MemoryOutbox SET Status = 'projected', UpdatedAt = ? "
            "WHERE EventId = ? AND Destination = ?",
            (now, event_id, destination),
        )
        conn.execute(
            "INSERT OR IGNORE INTO MemoryProjectionReceipts (EventId, Destination) VALUES (?, ?)",
            (event_id, destination),
        )
        conn.commit()

    def fail_outbox(self, event_id, error="", destination="adx", next_attempt_at=""):
        """Mark outbox entry as retry/failed with error and backoff."""
        self._check_open()
        conn = self._get_conn()
        now = _utc_iso_now()
        # Get current attempts
        row = conn.execute(
            "SELECT Attempts, MaxAttempts FROM MemoryOutbox WHERE EventId = ? AND Destination = ?",
            (event_id, destination),
        ).fetchone()
        if not row:
            return
        attempts = (row[0] or 0) + 1
        max_attempts = row[1] or 10
        status = "dead_letter" if attempts >= max_attempts else "retry"
        conn.execute(
            "UPDATE MemoryOutbox SET Status = ?, Attempts = ?, LastError = ?, "
            "NextAttemptAt = ?, UpdatedAt = ? WHERE EventId = ? AND Destination = ?",
            (status, attempts, str(error)[:1000], next_attempt_at, now, event_id, destination),
        )
        conn.commit()

    def pending_outbox(self, limit=50, destination=None):
        """Outbox entries awaiting projection (non-claiming read)."""
        self._check_open()
        conn = self._get_conn()
        now = _utc_iso_now()

        if destination:
            cur = conn.execute(
                "SELECT o.*, e.Payload, e.EventType, e.StreamId, e.OccurredAt "
                "FROM MemoryOutbox o "
                "JOIN MemoryEvents e ON o.EventId = e.EventId "
                "WHERE o.Status IN ('pending', 'retry') "
                "AND (o.NextAttemptAt <= ? OR o.NextAttemptAt = '' OR o.NextAttemptAt IS NULL) "
                "AND o.Destination = ? "
                "ORDER BY o.CreatedAt LIMIT ?",
                (now, destination, limit),
            )
        else:
            cur = conn.execute(
                "SELECT o.*, e.Payload, e.EventType, e.StreamId, e.OccurredAt "
                "FROM MemoryOutbox o "
                "JOIN MemoryEvents e ON o.EventId = e.EventId "
                "WHERE o.Status IN ('pending', 'retry') "
                "AND (o.NextAttemptAt <= ? OR o.NextAttemptAt = '' OR o.NextAttemptAt IS NULL) "
                "ORDER BY o.CreatedAt LIMIT ?",
                (now, limit),
            )
        return [_row_to_dict(cur, r) for r in cur.fetchall()]

    def outbox_status(self):
        """Return dict with outbox counts by status and destination."""
        self._check_open()
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT Destination, Status, COUNT(*) as cnt FROM MemoryOutbox GROUP BY Destination, Status"
        ).fetchall()
        result = {}
        for row in rows:
            dest = row[0]
            if dest not in result:
                result[dest] = {}
            result[dest][row[1]] = row[2]
        return result

    # ── legacy projection receipts ──────────────────────────────────

    def has_legacy_receipt(self, event_id, projection_name):
        """True if a legacy projection receipt exists for this event."""
        self._check_open()
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM LegacyProjectionReceipts "
            "WHERE EventId = ? AND ProjectionName = ?",
            (event_id, projection_name),
        ).fetchone()
        return row is not None

    def record_legacy_receipt(self, event_id, projection_name, row_count=0):
        """Record that a legacy projection has been applied."""
        self._check_open()
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO LegacyProjectionReceipts "
            "(EventId, ProjectionName, RowCount) VALUES (?, ?, ?)",
            (event_id, projection_name, row_count),
        )
        conn.commit()

    def has_projection_receipt(self, event_id, destination):
        """True if a projection receipt exists for this event+destination."""
        self._check_open()
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM MemoryProjectionReceipts WHERE EventId = ? AND Destination = ?",
            (event_id, destination),
        ).fetchone()
        return row is not None


# The V2 implementation owns transaction boundaries safely and is the public
# repository used by production. The legacy class above remains only to keep
# old serialized references/import traces readable during this migration.
from bridge.event_store import EventRepositoryV2 as EventRepository  # noqa: E402,F811
