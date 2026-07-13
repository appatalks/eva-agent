"""Transactional implementation of Eva's immutable event repository."""

import contextlib
import datetime
import math
import sqlite3
import threading
import time

from bridge.events import (
    ConcurrentStreamError,
    EventStoreError,
    IdempotencyCollisionError,
    MAX_PAYLOAD_BYTES,
    PayloadTooLargeError,
    ValidationError,
    _row_to_dict,
    _validate_expected_version,
    _validate_string,
    canonical_event_hash,
    canonical_json,
    deterministic_event_id,
    deterministic_outbox_id,
    outbox_error_code,
    payload_hash,
)
from bridge.sensitive import redact_credentials, should_create_outbox


def _utc_iso_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


class EventRepositoryV2:
    """Immutable event repository with caller-safe transaction semantics.

    ``source`` may be a ``SqliteMemory`` instance or a connection factory. A
    supplied ``connection=`` participates in the caller's transaction and is
    never committed by this repository.
    """

    def __init__(self, source, installation_id=""):
        self._memory = source if hasattr(source, "transaction") else None
        self._get_conn = source._conn if self._memory is not None else source
        self._installation_id = str(installation_id or "")
        self._closed = False
        self._lock = threading.RLock()
        self._factory_locks_guard = threading.Lock()
        self._factory_connection_locks = {}

    def close(self):
        self._closed = True
        with self._factory_locks_guard:
            self._factory_connection_locks.clear()

    def _check_open(self):
        if self._closed:
            raise EventStoreError("EventRepository is closed")

    def _factory_connection_lock(self, conn):
        """Return one stable lock for the exact factory connection object."""
        key = id(conn)
        with self._factory_locks_guard:
            entry = self._factory_connection_locks.get(key)
            if entry is None or entry[0] is not conn:
                entry = (conn, threading.RLock())
                self._factory_connection_locks[key] = entry
            return entry[1]

    @contextlib.contextmanager
    def _transaction(self, connection=None):
        if connection is not None:
            yield connection
            return
        if self._memory is not None:
            with self._memory.transaction() as conn:
                yield conn
            return

        conn = self._get_conn()
        if conn.in_transaction:
            raise EventStoreError(
                "factory connection has an active transaction; pass connection= "
                "for explicit transaction-local participation"
            )
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @contextlib.contextmanager
    def _serialized_transaction(self, connection=None):
        """Serialize mutations without introducing a memory/repository ABBA edge.

        ``SqliteMemory`` already serializes transaction entry with its RLock,
        so memory-backed repositories always acquire memory/transaction first
        and the repository lock second. A bare connection factory has no such
        outer lock and may legally return one shared ``check_same_thread=False``
        connection, so repository serialization must cover transaction/savepoint
        setup there as well.
        """
        if self._memory is not None:
            with self._transaction(connection) as conn:
                with self._lock:
                    yield conn
            return
        conn = connection if connection is not None else self._get_conn()
        connection_lock = self._factory_connection_lock(conn)
        with connection_lock:
            if connection is not None:
                # Explicit callers own commit/rollback and intentionally see
                # transaction-local state on this exact connection.
                yield conn
                return
            if conn.in_transaction:
                raise EventStoreError(
                    "factory connection has an active transaction; pass connection= "
                    "for explicit transaction-local participation"
                )
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextlib.contextmanager
    def _serialized_read(self, connection=None):
        """Yield a read connection without exposing unknown uncommitted state."""
        if connection is not None:
            if self._memory is not None:
                yield connection
            else:
                with self._factory_connection_lock(connection):
                    yield connection
            return
        if self._memory is not None:
            with self._memory.read_connection() as conn:
                yield conn
            return
        conn = self._get_conn()
        with self._factory_connection_lock(conn):
            if conn.in_transaction:
                raise EventStoreError(
                    "factory connection has an active transaction; pass connection= "
                    "for explicit transaction-local reads"
                )
            yield conn

    @staticmethod
    def _validate_uuidish(value, field):
        return _validate_string(value, field, max_len=512, allow_empty=True)

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
        connection=None,
    ):
        self._check_open()
        stream_id = _validate_string(stream_id, "stream_id", allow_empty=False)
        event_type = _validate_string(event_type, "event_type", allow_empty=False)
        actor_type = _validate_string(actor_type, "actor_type", max_len=32, allow_empty=False)
        actor_id = _validate_string(actor_id, "actor_id")
        origin = _validate_string(origin, "origin", max_len=32, allow_empty=False)
        correlation_id = self._validate_uuidish(correlation_id, "correlation_id")
        causation_id = self._validate_uuidish(causation_id, "causation_id")
        session_id = self._validate_uuidish(session_id, "session_id")
        turn_id = self._validate_uuidish(turn_id, "turn_id")
        source_message_id = self._validate_uuidish(source_message_id, "source_message_id")
        expected_version = _validate_expected_version(expected_version)
        if not isinstance(schema_version, int) or schema_version < 1:
            raise ValidationError("schema_version", "must be an integer >= 1")
        try:
            trust = float(trust)
        except (TypeError, ValueError) as exc:
            raise ValidationError("trust", "must be a finite number in [0,1]") from exc
        if not math.isfinite(trust) or not 0 <= trust <= 1:
            raise ValidationError("trust", "must be a finite number in [0,1]")
        if actor_type not in ("system", "user", "background", "admin"):
            raise ValidationError("actor_type", "unsupported actor")
        if origin not in ("bridge", "browser", "api", "background", "test"):
            raise ValidationError("origin", "unsupported origin")
        if sensitivity not in ("public", "normal", "private", "secret"):
            raise ValidationError("sensitivity", "unsupported classification")
        if consent_scope not in ("local_only", "session", "cloud_allowed", "deleted"):
            raise ValidationError("consent_scope", "unsupported scope")

        safe_payload = redact_credentials(payload)
        payload_json = canonical_json(safe_payload)
        if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise PayloadTooLargeError(len(payload_json.encode("utf-8")))
        payload_digest = payload_hash(payload_json)
        if idempotency_key is None:
            idempotency_key = f"{stream_id}:{event_type}:{payload_digest}"
        idempotency_key = _validate_string(
            idempotency_key, "idempotency_key", allow_empty=False
        )
        event_id = deterministic_event_id(self._installation_id, idempotency_key)
        occurred = (
            occurred_at.astimezone(datetime.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            if isinstance(occurred_at, datetime.datetime)
            else str(occurred_at or _utc_iso_now())
        )
        event_hash = canonical_event_hash(
            stream_id=stream_id, event_type=event_type,
            schema_version=schema_version, actor_type=actor_type,
            actor_id=actor_id, origin=origin,
            correlation_id=correlation_id, causation_id=causation_id,
            session_id=session_id, turn_id=turn_id,
            source_message_id=source_message_id, trust=trust,
            sensitivity=sensitivity, consent_scope=consent_scope,
            payload=safe_payload,
        )
        create_outbox = should_create_outbox(sensitivity, consent_scope)
        outbox_id = deterministic_outbox_id(event_id, outbox_destination)

        last_lock_error = None
        for attempt in range(3):
            try:
                with self._serialized_transaction(connection) as conn:
                    savepoint = f"event_operation_{threading.get_ident()}_{time.time_ns()}"
                    conn.execute(f"SAVEPOINT {savepoint}")
                    try:
                        result = self._append_in_transaction(
                            conn,
                            event_id=event_id,
                            stream_id=stream_id,
                            event_type=event_type,
                            schema_version=schema_version,
                            actor_type=actor_type,
                            actor_id=actor_id,
                            origin=origin,
                            occurred_at=occurred,
                            correlation_id=correlation_id,
                            causation_id=causation_id,
                            session_id=session_id,
                            turn_id=turn_id,
                            source_message_id=source_message_id,
                            trust=trust,
                            sensitivity=sensitivity,
                            consent_scope=consent_scope,
                            payload_json=payload_json,
                            payload_digest=payload_digest,
                            event_hash=event_hash,
                            idempotency_key=idempotency_key,
                            expected_version=expected_version,
                            create_outbox=create_outbox,
                            outbox_id=outbox_id,
                            outbox_destination=outbox_destination,
                        )
                        conn.execute(f"RELEASE {savepoint}")
                        return result
                    except Exception:
                        conn.execute(f"ROLLBACK TO {savepoint}")
                        conn.execute(f"RELEASE {savepoint}")
                        raise
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise EventStoreError(str(exc)) from exc
                last_lock_error = exc
                if connection is not None or attempt == 2:
                    break
                time.sleep(0.01 * (attempt + 1))
            except EventStoreError as exc:
                # Operation savepoint and owned transaction have rolled back.
                # Only a matching event observed under the same mutation
                # serialization contract is idempotent. For a shared factory
                # connection this prevents reading another thread's uncommitted
                # savepoint and reporting success for a row that later rolls back.
                with self._serialized_transaction(connection) as replay_conn:
                    cur = replay_conn.execute(
                        "SELECT * FROM MemoryEvents WHERE IdempotencyKey=?",
                        (idempotency_key,),
                    )
                    duplicate = cur.fetchone()
                    existing = _row_to_dict(cur, duplicate) if duplicate else None
                if duplicate:
                    if existing.get("EventHash") != event_hash:
                        raise IdempotencyCollisionError(
                            idempotency_key, "canonical event metadata differs"
                        ) from exc
                    return existing
                raise
        actual = -1
        try:
            with self._serialized_read(connection) as read_conn:
                row = read_conn.execute(
                    "SELECT MAX(StreamVersion) FROM MemoryEvents WHERE StreamId=?",
                    (stream_id,),
                ).fetchone()
            actual = row[0] if row and row[0] is not None else -1
        except Exception:
            pass
        raise ConcurrentStreamError(stream_id, expected_version, actual) from last_lock_error

    def _append_in_transaction(self, conn, **values):
        key = values["idempotency_key"]
        cur = conn.execute("SELECT * FROM MemoryEvents WHERE IdempotencyKey=?", (key,))
        row = cur.fetchone()
        if row:
            existing = _row_to_dict(cur, row)
            if existing.get("EventHash") != values["event_hash"]:
                raise IdempotencyCollisionError(key, "canonical event metadata differs")
            return existing

        row = conn.execute(
            "SELECT MAX(StreamVersion) FROM MemoryEvents WHERE StreamId=?",
            (values["stream_id"],),
        ).fetchone()
        actual = row[0] if row and row[0] is not None else -1
        expected = values["expected_version"]
        if expected is not None and expected != actual:
            raise ConcurrentStreamError(values["stream_id"], expected, actual)
        next_version = actual + 1
        try:
            conn.execute(
                "INSERT INTO MemoryEvents ("
                "EventId,StreamId,StreamVersion,EventType,SchemaVersion,ActorType,ActorId,Origin,"
                "OccurredAt,CorrelationId,CausationId,SessionId,TurnId,SourceMessageId,Trust,"
                "Sensitivity,ConsentScope,Payload,PayloadHash,EventHash,IdempotencyKey"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    values["event_id"], values["stream_id"], next_version,
                    values["event_type"], values["schema_version"], values["actor_type"],
                    values["actor_id"], values["origin"], values["occurred_at"],
                    values["correlation_id"], values["causation_id"], values["session_id"],
                    values["turn_id"], values["source_message_id"], values["trust"],
                    values["sensitivity"], values["consent_scope"], values["payload_json"],
                    values["payload_digest"], values["event_hash"], values["idempotency_key"],
                ),
            )
            if values["create_outbox"]:
                conn.execute(
                    "INSERT INTO MemoryOutbox (OutboxId,EventId,Destination,Status,Attempts) "
                    "VALUES (?,?,?,'pending',0)",
                    (values["outbox_id"], values["event_id"], values["outbox_destination"]),
                )
        except sqlite3.IntegrityError as exc:
            # The caller's operation savepoint will roll back both event and
            # outbox before classifying the failure. Never mistake a just-
            # inserted event followed by an outbox failure for idempotency.
            raise EventStoreError(f"Atomic event append failed: {exc}") from exc
        cur = conn.execute("SELECT * FROM MemoryEvents WHERE EventId=?", (values["event_id"],))
        return _row_to_dict(cur, cur.fetchone())

    def get_event(self, event_id, connection=None):
        self._check_open()
        with self._serialized_read(connection) as conn:
            cur = conn.execute("SELECT * FROM MemoryEvents WHERE EventId=?", (event_id,))
            return _row_to_dict(cur, cur.fetchone())

    def get_by_idempotency_key(self, idempotency_key, connection=None):
        self._check_open()
        with self._serialized_read(connection) as conn:
            cur = conn.execute(
                "SELECT * FROM MemoryEvents WHERE IdempotencyKey=?",
                (idempotency_key,),
            )
            return _row_to_dict(cur, cur.fetchone())

    def events_for_turn(self, turn_id, connection=None):
        self._check_open()
        with self._serialized_read(connection) as conn:
            cur = conn.execute(
                "SELECT * FROM MemoryEvents WHERE TurnId=? ORDER BY JournalSequence",
                (turn_id,),
            )
            return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def list_stream(self, stream_id, from_version=0, limit=1000, connection=None):
        self._check_open()
        with self._serialized_read(connection) as conn:
            cur = conn.execute(
                "SELECT * FROM MemoryEvents WHERE StreamId=? AND StreamVersion>=? "
                "ORDER BY StreamVersion LIMIT ?",
                (stream_id, from_version, limit),
            )
            return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def events_since(self, cursor_sequence=0, limit=1000, connection=None):
        self._check_open()
        with self._serialized_read(connection) as conn:
            cur = conn.execute(
                "SELECT * FROM MemoryEvents WHERE JournalSequence>? "
                "ORDER BY JournalSequence LIMIT ?",
                (cursor_sequence, limit),
            )
            return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def events_since_timestamp(self, recorded_after, limit=1000, connection=None):
        self._check_open()
        with self._serialized_read(connection) as conn:
            cur = conn.execute(
                "SELECT * FROM MemoryEvents WHERE RecordedAt>? "
                "ORDER BY JournalSequence LIMIT ?",
                (recorded_after, limit),
            )
            return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def has_legacy_receipt(self, event_id, projection_name, connection=None):
        self._check_open()
        with self._serialized_read(connection) as conn:
            return conn.execute(
                "SELECT 1 FROM LegacyProjectionReceipts "
                "WHERE EventId=? AND ProjectionName=?",
                (event_id, projection_name),
            ).fetchone() is not None

    def record_legacy_receipt(self, event_id, projection_name, row_count=0, connection=None):
        self._check_open()
        with self._serialized_transaction(connection) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO LegacyProjectionReceipts "
                "(EventId,ProjectionName,RowCount) VALUES (?,?,?)",
                (event_id, projection_name, row_count),
            )

    def has_projection_receipt(self, event_id, destination, connection=None):
        self._check_open()
        with self._serialized_read(connection) as conn:
            return conn.execute(
                "SELECT 1 FROM MemoryProjectionReceipts "
                "WHERE EventId=? AND Destination=?",
                (event_id, destination),
            ).fetchone() is not None

    def record_projection_receipt(self, event_id, destination):
        self.complete_outbox(event_id, destination)

    def ensure_outbox(self, event_id, destination, connection=None):
        """Create a destination delivery record in the caller's transaction."""
        self._check_open()
        event_id = _validate_string(event_id, "event_id", allow_empty=False)
        destination = _validate_string(
            destination, "destination", max_len=512, allow_empty=False
        )
        outbox_id = deterministic_outbox_id(event_id, destination)
        with self._serialized_transaction(connection) as conn:
            if not conn.execute(
                "SELECT 1 FROM MemoryEvents WHERE EventId=?", (event_id,)
            ).fetchone():
                raise EventStoreError(f"Cannot create outbox for missing event {event_id}")
            conn.execute(
                "INSERT OR IGNORE INTO MemoryOutbox "
                "(OutboxId,EventId,Destination,Status,Attempts) "
                "VALUES (?,?,?,'pending',0)",
                (outbox_id, event_id, destination),
            )
            cur = conn.execute(
                "SELECT * FROM MemoryOutbox WHERE EventId=? AND Destination=?",
                (event_id, destination),
            )
            return _row_to_dict(cur, cur.fetchone())

    def claim_outbox_entry(self, event_id, destination, lease_seconds=120):
        """Atomically lease one exact event/destination delivery.

        A concurrent caller receives ``None`` while the winning lease is live.
        Expired processing leases are reclaimable for crash recovery.
        """
        self._check_open()
        event_id = _validate_string(event_id, "event_id", allow_empty=False)
        destination = _validate_string(
            destination, "destination", max_len=512, allow_empty=False
        )
        now = _utc_iso_now()
        lease = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=lease_seconds)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with self._serialized_transaction() as conn:
            updated = conn.execute(
                "UPDATE MemoryOutbox SET Status='processing',"
                "Attempts=Attempts+1,LeaseUntil=?,UpdatedAt=? "
                "WHERE EventId=? AND Destination=? AND Attempts<MaxAttempts AND ("
                "(Status IN ('pending','retry') AND "
                "(NextAttemptAt IS NULL OR NextAttemptAt='' OR NextAttemptAt<=?)) "
                "OR (Status='processing' AND (LeaseUntil IS NULL OR LeaseUntil='' OR LeaseUntil<=?))"
                ")",
                (lease, now, event_id, destination, now, now),
            )
            if updated.rowcount != 1:
                return None
            cur = conn.execute(
                "SELECT * FROM MemoryOutbox WHERE EventId=? AND Destination=?",
                (event_id, destination),
            )
            return _row_to_dict(cur, cur.fetchone())

    def pending_outbox(self, limit=50, destination=None, connection=None):
        self._check_open()
        now = _utc_iso_now()
        destination_sql = " AND o.Destination=?" if destination else ""
        params = [now]
        if destination:
            params.append(destination)
        params.append(limit)
        with self._serialized_read(connection) as conn:
            cur = conn.execute(
                "SELECT o.*,e.Payload,e.EventType,e.StreamId,e.OccurredAt,"
                "e.Sensitivity,e.ConsentScope "
                "FROM MemoryOutbox o JOIN MemoryEvents e ON e.EventId=o.EventId "
                "WHERE o.Status IN ('pending','retry') "
                "AND (o.NextAttemptAt IS NULL OR o.NextAttemptAt='' "
                "OR o.NextAttemptAt<=?)" + destination_sql
                + " ORDER BY o.CreatedAt,o.OutboxId LIMIT ?",
                tuple(params),
            )
            return [_row_to_dict(cur, row) for row in cur.fetchall()]

    def claim_outbox(self, limit=50, destination=None, lease_seconds=120):
        self._check_open()
        now = _utc_iso_now()
        lease = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=lease_seconds
        )).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with self._serialized_transaction() as conn:
            destination_sql = " AND o.Destination=?" if destination else ""
            params = [now, now]
            if destination:
                params.append(destination)
            params.append(limit)
            cur = conn.execute(
                "SELECT o.*,e.Payload,e.EventType,e.StreamId,e.OccurredAt,e.Sensitivity,e.ConsentScope "
                "FROM MemoryOutbox o JOIN MemoryEvents e ON e.EventId=o.EventId "
                "WHERE ((o.Status IN ('pending','retry') AND "
                "(o.NextAttemptAt IS NULL OR o.NextAttemptAt='' OR o.NextAttemptAt<=?)) "
                "OR (o.Status='processing' AND o.LeaseUntil<=?)) "
                "AND o.Attempts<o.MaxAttempts" + destination_sql
                + " ORDER BY o.CreatedAt,o.OutboxId LIMIT ?",
                tuple(params),
            )
            entries = [_row_to_dict(cur, row) for row in cur.fetchall()]
            for entry in entries:
                conn.execute(
                    "UPDATE MemoryOutbox SET Status='processing',Attempts=Attempts+1,"
                    "LeaseUntil=?,UpdatedAt=? WHERE OutboxId=?",
                    (lease, now, entry["OutboxId"]),
                )
                entry["Attempts"] = int(entry.get("Attempts", 0)) + 1
                entry["LeaseUntil"] = lease
            return entries

    def complete_outbox(self, event_id, destination="adx"):
        self._check_open()
        now = _utc_iso_now()
        with self._serialized_transaction() as conn:
            updated = conn.execute(
                "UPDATE MemoryOutbox SET Status='projected',LeaseUntil='',UpdatedAt=?,LastError='' "
                "WHERE EventId=? AND Destination=?",
                (now, event_id, destination),
            )
            if updated.rowcount != 1:
                raise EventStoreError(
                    f"Cannot receipt projection without outbox: {event_id}/{destination}"
                )
            conn.execute(
                "INSERT OR IGNORE INTO MemoryProjectionReceipts (EventId,Destination) VALUES (?,?)",
                (event_id, destination),
            )

    def fail_outbox(self, event_id, error="", destination="adx", next_attempt_at=""):
        self._check_open()
        now = _utc_iso_now()
        with self._serialized_transaction() as conn:
            row = conn.execute(
                "SELECT Attempts,MaxAttempts,Status FROM MemoryOutbox WHERE EventId=? AND Destination=?",
                (event_id, destination),
            ).fetchone()
            if not row:
                return
            attempts, max_attempts = int(row[0]), int(row[1])
            if row[2] != "processing":
                attempts += 1
            status = "dead_letter" if attempts >= max_attempts else "retry"
            conn.execute(
                "UPDATE MemoryOutbox SET Status=?,Attempts=?,LastError=?,NextAttemptAt=?,LeaseUntil='',UpdatedAt=? "
                "WHERE EventId=? AND Destination=?",
                (status, attempts, outbox_error_code(error), next_attempt_at, now, event_id, destination),
            )

    def outbox_status(self, connection=None):
        self._check_open()
        with self._serialized_read(connection) as conn:
            rows = conn.execute(
                "SELECT Destination,Status,COUNT(*) FROM MemoryOutbox "
                "GROUP BY Destination,Status"
            ).fetchall()
        result = {}
        for destination, status, count in rows:
            result.setdefault(destination, {})[status] = count
        return result


EventRepository = EventRepositoryV2
