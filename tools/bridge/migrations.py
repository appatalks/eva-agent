"""Fatal, versioned SQLite migrations for Eva's memory kernel."""

import datetime
import hashlib
import json
import re
import sqlite3


class MigrationError(Exception):
    def __init__(self, version, description, cause):
        self.version = version
        self.description = description
        self.cause = cause
        super().__init__(f"Migration v{version} ({description}) FAILED fatally: {cause}")


class SchemaVerificationError(MigrationError):
    def __init__(self, version, description, detail):
        super().__init__(version, description, f"Schema verification: {detail}")


_META_DDL = (
    "CREATE TABLE IF NOT EXISTS _schema_migrations ("
    "version INTEGER PRIMARY KEY,description TEXT NOT NULL,"
    "applied_at TEXT NOT NULL,checksum TEXT NOT NULL)"
)


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _object_exists(conn, kind, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (kind, name)
    ).fetchone() is not None


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_xinfo({table})").fetchall()}


def _normalized_sql(conn, kind, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)
    ).fetchone()
    return re.sub(r"\s+", "", (row[0] if row and row[0] else "").lower())


_MIGRATION_MANIFESTS = {
    0: {"legacy_owner": "SqliteMemory", "contract": "no destructive baseline migration"},
    1: {
        "tables": {
            "MemoryEvents": {
                "pk": ["JournalSequence"], "autoincrement": True,
                "unique": [["EventId"], ["StreamId", "StreamVersion"], ["IdempotencyKey"]],
                "immutable": ["UPDATE", "DELETE"],
            },
            "MemoryOutbox": {
                "unique": [["OutboxId"], ["EventId", "Destination"]],
                "fk": ["EventId", "MemoryEvents", "EventId"],
                "lease": "LeaseUntil", "retry": ["Attempts", "MaxAttempts", "NextAttemptAt"],
                "checks": ["Destination length 1..512", "Attempts >= 0", "MaxAttempts >= 1"],
            },
            "LegacyProjectionReceipts": {"pk": ["EventId", "ProjectionName"]},
            "MemoryProjectionReceipts": {"pk": ["EventId", "Destination"]},
        },
        "indexes": {
            "uq_events_id": ["EventId"],
            "uq_events_stream_ver": ["StreamId", "StreamVersion"],
            "uq_events_idempotency": ["IdempotencyKey"],
            "idx_events_stream": ["StreamId"],
            "idx_events_type": ["EventType"],
            "idx_events_recorded": ["RecordedAt"],
            "idx_events_session": ["SessionId"],
            "idx_events_sequence": ["JournalSequence"],
            "uq_outbox_id": ["OutboxId"],
            "uq_outbox_event_dest": ["EventId", "Destination"],
            "idx_outbox_status": ["Status"],
            "idx_outbox_next": ["NextAttemptAt"],
            "idx_outbox_dest": ["Destination"],
        },
    },
    2: {"upgrade": ["EventHash", "LeaseUntil", "MaxAttempts", "MemoryProjectionReceipts"]},
    3: {
        "repair": "canonical event-support rebuild",
        "tables": [
            "MemoryEvents", "MemoryOutbox",
            "LegacyProjectionReceipts", "MemoryProjectionReceipts",
        ],
    },
    4: {
        "repair": "canonical logical event hashes",
        "algorithm": "bridge.events.canonical_event_hash",
    },
}


_SQL_NOW_DEFAULT = "strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z'"

# name -> (SQLite affinity, NOT NULL, default SQL, primary-key position)
# PRAGMA reports INTEGER PRIMARY KEY as nullable even though SQLite enforces
# row identity, so JournalSequence intentionally records ``False`` here.
_COLUMN_MANIFESTS = {
    "MemoryEvents": {
        "JournalSequence": ("INTEGER", False, None, 1),
        "EventId": ("TEXT", True, None, 0),
        "StreamId": ("TEXT", True, None, 0),
        "StreamVersion": ("INTEGER", True, None, 0),
        "EventType": ("TEXT", True, None, 0),
        "SchemaVersion": ("INTEGER", True, "1", 0),
        "ActorType": ("TEXT", True, None, 0),
        "ActorId": ("TEXT", True, "''", 0),
        "Origin": ("TEXT", True, None, 0),
        "OccurredAt": ("TEXT", True, None, 0),
        "RecordedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
        "CorrelationId": ("TEXT", True, "''", 0),
        "CausationId": ("TEXT", True, "''", 0),
        "SessionId": ("TEXT", True, "''", 0),
        "TurnId": ("TEXT", True, "''", 0),
        "SourceMessageId": ("TEXT", True, "''", 0),
        "Trust": ("REAL", True, None, 0),
        "Sensitivity": ("TEXT", True, None, 0),
        "ConsentScope": ("TEXT", True, None, 0),
        "Payload": ("TEXT", True, None, 0),
        "PayloadHash": ("TEXT", True, None, 0),
        "EventHash": ("TEXT", True, None, 0),
        "IdempotencyKey": ("TEXT", True, None, 0),
    },
    "MemoryOutbox": {
        "OutboxId": ("TEXT", True, None, 0),
        "EventId": ("TEXT", True, None, 0),
        "Destination": ("TEXT", True, "'adx'", 0),
        "Status": ("TEXT", True, "'pending'", 0),
        "Attempts": ("INTEGER", True, "0", 0),
        "MaxAttempts": ("INTEGER", True, "10", 0),
        "NextAttemptAt": ("TEXT", False, "''", 0),
        "LeaseUntil": ("TEXT", False, "''", 0),
        "CreatedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
        "UpdatedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
        "LastError": ("TEXT", False, "''", 0),
    },
    "LegacyProjectionReceipts": {
        "EventId": ("TEXT", True, None, 1),
        "ProjectionName": ("TEXT", True, None, 2),
        "ProjectedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
        "RowCount": ("INTEGER", True, "0", 0),
    },
    "MemoryProjectionReceipts": {
        "EventId": ("TEXT", True, None, 1),
        "Destination": ("TEXT", True, None, 2),
        "ProjectedAt": ("TEXT", True, _SQL_NOW_DEFAULT, 0),
    },
}


def _manifest_hash(version):
    canonical = json.dumps(
        _MIGRATION_MANIFESTS[version], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _index_columns(conn, table, name):
    indexes = {row[1]: bool(row[2]) for row in conn.execute(f"PRAGMA index_list({table})")}
    columns = [row[2] for row in conn.execute(f"PRAGMA index_info({name})")]
    return indexes.get(name, False), columns


def _verify_index(conn, table, name, columns, unique, version):
    matches = [
        row for row in conn.execute(f"PRAGMA index_list({table})").fetchall()
        if row[1] == name
    ]
    if len(matches) != 1:
        raise SchemaVerificationError(version, "event kernel", f"index {name} missing")
    row = matches[0]
    if bool(row[2]) != unique or row[3] != "c" or bool(row[4]):
        raise SchemaVerificationError(
            version, "event kernel",
            f"index semantics drift: {name} unique={row[2]} origin={row[3]} partial={row[4]}",
        )
    key_rows = [
        entry for entry in conn.execute(f"PRAGMA index_xinfo({name})").fetchall()
        if entry[5]
    ]
    actual = [entry[2] for entry in key_rows]
    if (
        actual != columns
        or any(entry[2] is None or bool(entry[3]) or entry[4] != "BINARY" for entry in key_rows)
    ):
        raise SchemaVerificationError(
            version, "event kernel", f"index key drift: {name} {actual}",
        )


def _verify_user_index_set(conn, table, expected, version):
    actual = {
        row[1] for row in conn.execute(f"PRAGMA index_list({table})").fetchall()
        if row[3] == "c"
    }
    if actual != set(expected):
        raise SchemaVerificationError(
            version, "event kernel",
            f"{table} index manifest drift: {sorted(actual)} != {sorted(expected)}",
        )


def _verify_constraint_indexes(conn, table, expected, version):
    actual = []
    for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if row[3] == "c":
            continue
        key_rows = [
            entry for entry in conn.execute(
                f"PRAGMA index_xinfo({row[1]})"
            ).fetchall() if entry[5]
        ]
        signature = (
            row[3], bool(row[2]), bool(row[4]),
            tuple((entry[2], bool(entry[3]), entry[4]) for entry in key_rows),
        )
        actual.append(signature)
    if sorted(actual, key=repr) != sorted(expected, key=repr):
        raise SchemaVerificationError(
            version, "event kernel",
            f"{table} constraint-index manifest drift",
        )


def _sqlite_affinity(declared_type):
    declared = str(declared_type or "").upper()
    if "INT" in declared:
        return "INTEGER"
    if any(marker in declared for marker in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if not declared or "BLOB" in declared:
        return "BLOB"
    if any(marker in declared for marker in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _normalized_default(value):
    if value is None:
        return None
    return re.sub(r"\s+", "", str(value)).lower()


def _verify_column_manifest(conn, table, version):
    rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
    actual = {row[1]: row for row in rows}
    expected = _COLUMN_MANIFESTS[table]
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise SchemaVerificationError(
            version, "event kernel",
            f"{table} column manifest drift (missing={missing}, extra={extra})",
        )
    for name, (affinity, not_null, default, pk_position) in expected.items():
        row = actual[name]
        observed = (
            _sqlite_affinity(row[2]), bool(row[3]),
            _normalized_default(row[4]), int(row[5]),
        )
        wanted = (
            affinity, not_null, _normalized_default(default), pk_position,
        )
        if observed != wanted:
            raise SchemaVerificationError(
                version, "event kernel",
                f"{table}.{name} manifest drift: {observed} != {wanted}",
            )
        if len(row) < 7 or int(row[6]) != 0:
            raise SchemaVerificationError(
                version, "event kernel",
                f"{table}.{name} hidden/generated-column drift: {row[6] if len(row) >= 7 else 'unknown'}",
            )


def _primary_key_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
    return [row[1] for row in sorted((row for row in rows if row[5]), key=lambda row: row[5])]


def _has_event_fk(conn, table):
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    return len(rows) == 1 and tuple(rows[0][2:8]) == (
        "MemoryEvents", "EventId", "EventId",
        "NO ACTION", "NO ACTION", "NONE",
    )


def _verify_immutable_behavior(conn):
    savepoint = "verify_event_immutability"
    marker = hashlib.sha256(
        f"{id(conn)}:{datetime.datetime.now(datetime.timezone.utc).isoformat()}".encode("utf-8")
    ).hexdigest()
    event_id = f"verify-{marker}"
    stream_id = f"verify:immutable:{marker}"
    idempotency_key = f"verify:immutable:{marker}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        conn.execute(
            "INSERT INTO MemoryEvents ("
            "EventId,StreamId,StreamVersion,EventType,SchemaVersion,ActorType,ActorId,Origin,"
            "OccurredAt,CorrelationId,CausationId,SessionId,TurnId,SourceMessageId,Trust,"
            "Sensitivity,ConsentScope,Payload,PayloadHash,EventHash,IdempotencyKey"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, stream_id, 0,
                "verify.created", 1, "system", "verifier", "test",
                "2000-01-01T00:00:00Z", "", "", "", "", "", 1.0,
                "normal", "local_only", "{}", "verify", "verify", idempotency_key,
            ),
        )
        sequence = conn.execute(
            "SELECT JournalSequence FROM MemoryEvents WHERE EventId=?", (event_id,)
        ).fetchone()[0]
        if not isinstance(sequence, int) or sequence <= 0:
            raise SchemaVerificationError(2, "event kernel", "JournalSequence autoincrement drift")
        for statement in (
            "UPDATE MemoryEvents SET EventType='verify.changed' WHERE EventId=?",
            "DELETE FROM MemoryEvents WHERE EventId=?",
        ):
            try:
                conn.execute(statement, (event_id,))
            except sqlite3.IntegrityError:
                continue
            raise SchemaVerificationError(2, "event kernel", "immutability trigger behavior drift")
    finally:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")


def _current_version(conn):
    if not _table_exists(conn, "_schema_migrations"):
        return -1
    row = conn.execute("SELECT MAX(version) FROM _schema_migrations").fetchone()
    return row[0] if row and row[0] is not None else -1


def _m0_baseline(conn):
    # SqliteMemory owns legacy table creation. The event repository is also
    # usable standalone (tests, migration tools), so v0 records compatibility
    # without requiring those projections to exist.
    return None


def _create_event_support(conn):
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS MemoryEvents (
            JournalSequence INTEGER PRIMARY KEY AUTOINCREMENT,
            EventId TEXT NOT NULL UNIQUE,
            StreamId TEXT NOT NULL CHECK(length(StreamId)>0 AND length(StreamId)<=512),
            StreamVersion INTEGER NOT NULL CHECK(StreamVersion>=0),
            EventType TEXT NOT NULL CHECK(length(EventType)>0 AND length(EventType)<=512),
            SchemaVersion INTEGER NOT NULL DEFAULT 1 CHECK(SchemaVersion>=1),
            ActorType TEXT NOT NULL CHECK(ActorType IN ('system','user','background','admin')),
            ActorId TEXT NOT NULL DEFAULT '',
            Origin TEXT NOT NULL CHECK(Origin IN ('bridge','browser','api','background','test')),
            OccurredAt TEXT NOT NULL,
            RecordedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z'),
            CorrelationId TEXT NOT NULL DEFAULT '',
            CausationId TEXT NOT NULL DEFAULT '',
            SessionId TEXT NOT NULL DEFAULT '',
            TurnId TEXT NOT NULL DEFAULT '',
            SourceMessageId TEXT NOT NULL DEFAULT '',
            Trust REAL NOT NULL CHECK(Trust>=0.0 AND Trust<=1.0),
            Sensitivity TEXT NOT NULL CHECK(Sensitivity IN ('public','normal','private','secret')),
            ConsentScope TEXT NOT NULL CHECK(ConsentScope IN ('local_only','session','cloud_allowed','deleted')),
            Payload TEXT NOT NULL,
            PayloadHash TEXT NOT NULL,
            EventHash TEXT NOT NULL,
            IdempotencyKey TEXT NOT NULL UNIQUE,
            UNIQUE(StreamId,StreamVersion)
        )
    """)
    for sql in (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_events_id ON MemoryEvents(EventId)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_events_stream_ver ON MemoryEvents(StreamId,StreamVersion)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_events_idempotency ON MemoryEvents(IdempotencyKey)",
        "CREATE INDEX IF NOT EXISTS idx_events_stream ON MemoryEvents(StreamId)",
        "CREATE INDEX IF NOT EXISTS idx_events_type ON MemoryEvents(EventType)",
        "CREATE INDEX IF NOT EXISTS idx_events_recorded ON MemoryEvents(RecordedAt)",
        "CREATE INDEX IF NOT EXISTS idx_events_session ON MemoryEvents(SessionId)",
        "CREATE INDEX IF NOT EXISTS idx_events_sequence ON MemoryEvents(JournalSequence)",
    ):
        conn.execute(sql)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_events_no_update BEFORE UPDATE ON MemoryEvents
        BEGIN SELECT RAISE(ABORT,'MemoryEvents is immutable: UPDATE not allowed'); END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_events_no_delete BEFORE DELETE ON MemoryEvents
        BEGIN SELECT RAISE(ABORT,'MemoryEvents is immutable: DELETE not allowed'); END
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS MemoryOutbox (
            OutboxId TEXT NOT NULL UNIQUE,
            EventId TEXT NOT NULL,
            Destination TEXT NOT NULL DEFAULT 'adx'
                CHECK(length(Destination)>0 AND length(Destination)<=512),
            Status TEXT NOT NULL DEFAULT 'pending'
                CHECK(Status IN ('pending','processing','retry','projected','failed','dead_letter')),
            Attempts INTEGER NOT NULL DEFAULT 0 CHECK(Attempts>=0),
            MaxAttempts INTEGER NOT NULL DEFAULT 10 CHECK(MaxAttempts>=1),
            NextAttemptAt TEXT DEFAULT '',
            LeaseUntil TEXT DEFAULT '',
            CreatedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z'),
            UpdatedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z'),
            LastError TEXT DEFAULT '',
            UNIQUE(EventId,Destination),
            FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId)
        )
    """)
    for sql in (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_id ON MemoryOutbox(OutboxId)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_event_dest ON MemoryOutbox(EventId,Destination)",
        "CREATE INDEX IF NOT EXISTS idx_outbox_status ON MemoryOutbox(Status)",
        "CREATE INDEX IF NOT EXISTS idx_outbox_next ON MemoryOutbox(NextAttemptAt)",
        "CREATE INDEX IF NOT EXISTS idx_outbox_dest ON MemoryOutbox(Destination)",
    ):
        conn.execute(sql)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS LegacyProjectionReceipts (
            EventId TEXT NOT NULL,ProjectionName TEXT NOT NULL,
            ProjectedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z'),
            RowCount INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(EventId,ProjectionName),
            FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS MemoryProjectionReceipts (
            EventId TEXT NOT NULL,Destination TEXT NOT NULL,
            ProjectedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z'),
            PRIMARY KEY(EventId,Destination),
            FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId)
        )
    """)


def _m1_event_kernel(conn):
    _create_event_support(conn)


def _draft_select_list(
    columns, expected, fallbacks=None, *, version=2,
    description="phase1 draft upgrade",
):
    fallbacks = fallbacks or {}
    expressions = []
    for name in expected:
        if name in columns:
            expression = f'"{name}"'
            if name == "EventHash":
                expression = "COALESCE(NULLIF(EventHash,''),PayloadHash)"
        elif name in fallbacks:
            expression = fallbacks[name]
        else:
            raise SchemaVerificationError(
                version, description, f"required source column missing: {name}"
            )
        expressions.append(f'{expression} AS "{name}"')
    return ",".join(expressions)


def _rehash_temp_events(conn, *, version, description):
    from bridge.events import canonical_event_hash, canonical_json, payload_hash

    cursor = conn.execute("SELECT rowid AS _TempRowId,* FROM _eva_m2_events")
    names = [column[0] for column in cursor.description]
    for raw in cursor.fetchall():
        row = dict(zip(names, raw))
        try:
            payload = json.loads(row["Payload"])
            payload_json = canonical_json(payload)
            payload_digest = payload_hash(payload_json)
            event_hash = canonical_event_hash(
                stream_id=row["StreamId"], event_type=row["EventType"],
                schema_version=row["SchemaVersion"], actor_type=row["ActorType"],
                actor_id=row["ActorId"], origin=row["Origin"],
                correlation_id=row["CorrelationId"], causation_id=row["CausationId"],
                session_id=row["SessionId"], turn_id=row["TurnId"],
                source_message_id=row["SourceMessageId"], trust=row["Trust"],
                sensitivity=row["Sensitivity"], consent_scope=row["ConsentScope"],
                payload=payload,
            )
        except Exception as exc:
            raise SchemaVerificationError(
                version, description,
                f"cannot canonicalize event {row.get('EventId', '')}: {exc}",
            ) from exc
        conn.execute(
            "UPDATE _eva_m2_events SET Payload=?,PayloadHash=?,EventHash=? WHERE rowid=?",
            (payload_json, payload_digest, event_hash, row["_TempRowId"]),
        )


def _rebuild_draft_event_support(
    conn, *, version=2, description="phase1 draft upgrade"
):
    """Rebuild the draft journal into the exact canonical schema atomically."""
    event_names = list(_COLUMN_MANIFESTS["MemoryEvents"])
    outbox_names = list(_COLUMN_MANIFESTS["MemoryOutbox"])
    legacy_names = list(_COLUMN_MANIFESTS["LegacyProjectionReceipts"])
    projection_names = list(_COLUMN_MANIFESTS["MemoryProjectionReceipts"])
    temp_tables = (
        "_eva_m2_events", "_eva_m2_outbox",
        "_eva_m2_legacy_receipts", "_eva_m2_projection_receipts",
    )
    for table in temp_tables:
        conn.execute(f"DROP TABLE IF EXISTS temp.{table}")

    event_columns = _columns(conn, "MemoryEvents")
    event_select = _draft_select_list(
        event_columns, event_names, {"EventHash": "PayloadHash"},
        version=version, description=description,
    )
    conn.execute(
        f"CREATE TEMP TABLE _eva_m2_events AS "
        f"SELECT {event_select} FROM MemoryEvents"
    )
    _rehash_temp_events(conn, version=version, description=description)

    outbox_columns = _columns(conn, "MemoryOutbox")
    outbox_select = _draft_select_list(
        outbox_columns, outbox_names,
        {
            "Destination": "'adx'", "Status": "'pending'",
            "Attempts": "0", "MaxAttempts": "10",
            "NextAttemptAt": "''", "LeaseUntil": "''",
            "CreatedAt": _SQL_NOW_DEFAULT, "UpdatedAt": _SQL_NOW_DEFAULT,
            "LastError": "''",
        },
        version=version, description=description,
    )
    conn.execute(
        f"CREATE TEMP TABLE _eva_m2_outbox AS "
        f"SELECT {outbox_select} FROM MemoryOutbox"
    )

    has_legacy = _table_exists(conn, "LegacyProjectionReceipts")
    if has_legacy:
        legacy_select = _draft_select_list(
            _columns(conn, "LegacyProjectionReceipts"), legacy_names,
            {"ProjectedAt": _SQL_NOW_DEFAULT, "RowCount": "0"},
            version=version, description=description,
        )
        conn.execute(
            f"CREATE TEMP TABLE _eva_m2_legacy_receipts AS "
            f"SELECT {legacy_select} FROM LegacyProjectionReceipts"
        )
    has_projection = _table_exists(conn, "MemoryProjectionReceipts")
    if has_projection:
        projection_select = _draft_select_list(
            _columns(conn, "MemoryProjectionReceipts"), projection_names,
            {"ProjectedAt": _SQL_NOW_DEFAULT},
            version=version, description=description,
        )
        conn.execute(
            f"CREATE TEMP TABLE _eva_m2_projection_receipts AS "
            f"SELECT {projection_select} FROM MemoryProjectionReceipts"
        )

    conn.execute("DROP TABLE IF EXISTS MemoryProjectionReceipts")
    conn.execute("DROP TABLE IF EXISTS LegacyProjectionReceipts")
    conn.execute("DROP TABLE MemoryOutbox")
    conn.execute("DROP TABLE MemoryEvents")
    _create_event_support(conn)

    def restore(table, names, source):
        columns = ",".join(f'"{name}"' for name in names)
        conn.execute(
            f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {source}"
        )

    restore("MemoryEvents", event_names, "_eva_m2_events")
    restore("MemoryOutbox", outbox_names, "_eva_m2_outbox")
    if has_legacy:
        restore(
            "LegacyProjectionReceipts", legacy_names,
            "_eva_m2_legacy_receipts",
        )
    if has_projection:
        restore(
            "MemoryProjectionReceipts", projection_names,
            "_eva_m2_projection_receipts",
        )
    for table in temp_tables:
        conn.execute(f"DROP TABLE IF EXISTS temp.{table}")


def _m2_draft_upgrade(conn):
    if not _table_exists(conn, "MemoryEvents"):
        _create_event_support(conn)
        return
    try:
        verify_schema(conn)
        return
    except SchemaVerificationError:
        pass
    # JournalSequence cannot be safely retrofitted as a primary key. A malformed
    # development table must be exported/recreated rather than silently accepted.
    if "JournalSequence" not in _columns(conn, "MemoryEvents"):
        raise SchemaVerificationError(2, "phase1 draft upgrade", "MemoryEvents missing JournalSequence")
    if not _table_exists(conn, "MemoryOutbox"):
        raise SchemaVerificationError(2, "phase1 draft upgrade", "MemoryOutbox missing")
    _rebuild_draft_event_support(conn)


def _m3_canonical_repair(conn):
    try:
        verify_schema(conn)
        return
    except SchemaVerificationError:
        pass
    if not _table_exists(conn, "MemoryEvents") or not _table_exists(conn, "MemoryOutbox"):
        raise SchemaVerificationError(3, "canonical repair", "event support tables missing")
    _rebuild_draft_event_support(
        conn, version=3, description="canonical repair"
    )


def _m4_canonical_event_hashes(conn):
    if not _table_exists(conn, "MemoryEvents") or not _table_exists(conn, "MemoryOutbox"):
        raise SchemaVerificationError(4, "event hash repair", "event support tables missing")
    _rebuild_draft_event_support(
        conn, version=4, description="event hash repair"
    )


_MIGRATIONS = [
    (0, "baseline legacy schema", _manifest_hash(0), _m0_baseline),
    (1, "immutable event kernel", _manifest_hash(1), _m1_event_kernel),
    (2, "phase1 draft upgrade", _manifest_hash(2), _m2_draft_upgrade),
    (3, "canonical event schema repair", _manifest_hash(3), _m3_canonical_repair),
    (4, "canonical event hash repair", _manifest_hash(4), _m4_canonical_event_hashes),
]


def verify_schema(conn):
    version = _current_version(conn)
    if version < 1:
        return version
    for trigger in ("trg_events_no_update", "trg_events_no_delete"):
        if not _object_exists(conn, "trigger", trigger):
            raise SchemaVerificationError(version, "event kernel", f"trigger {trigger} missing")
    update_sql = _normalized_sql(conn, "trigger", "trg_events_no_update")
    delete_sql = _normalized_sql(conn, "trigger", "trg_events_no_delete")
    if "beforeupdateonmemoryevents" not in update_sql or "raise(abort" not in update_sql:
        raise SchemaVerificationError(version, "event kernel", "immutable UPDATE trigger body drift")
    if "beforedeleteonmemoryevents" not in delete_sql or "raise(abort" not in delete_sql:
        raise SchemaVerificationError(version, "event kernel", "immutable DELETE trigger body drift")
    _verify_column_manifest(conn, "MemoryEvents", version)
    events_sql = _normalized_sql(conn, "table", "MemoryEvents")
    if _primary_key_columns(conn, "MemoryEvents") != ["JournalSequence"]:
        raise SchemaVerificationError(version, "event kernel", "JournalSequence primary-key drift")
    if "journalsequenceintegerprimarykeyautoincrement" not in events_sql:
        raise SchemaVerificationError(version, "event kernel", "JournalSequence autoincrement DDL drift")
    for fragment in (
        "unique(streamid,streamversion)",
        "check(length(streamid)>0andlength(streamid)<=512)",
        "check(streamversion>=0)",
        "check(length(eventtype)>0andlength(eventtype)<=512)",
        "check(schemaversion>=1)",
        "check(actortypein('system','user','background','admin'))",
        "check(originin('bridge','browser','api','background','test'))",
        "check(trust>=0.0andtrust<=1.0)",
        "check(sensitivityin('public','normal','private','secret'))",
        "check(consentscopein('local_only','session','cloud_allowed','deleted'))",
    ):
        if fragment not in events_sql:
            raise SchemaVerificationError(version, "event kernel", f"MemoryEvents constraint drift: {fragment}")
    for table in ("MemoryOutbox", "LegacyProjectionReceipts", "MemoryProjectionReceipts"):
        if not _table_exists(conn, table):
            raise SchemaVerificationError(version, "event kernel", f"{table} missing")
        _verify_column_manifest(conn, table, version)
    _verify_immutable_behavior(conn)
    event_indexes = _MIGRATION_MANIFESTS[1]["indexes"]
    _verify_user_index_set(
        conn, "MemoryEvents",
        [name for name in event_indexes if "outbox" not in name], version,
    )
    _verify_user_index_set(
        conn, "MemoryOutbox",
        [name for name in event_indexes if "outbox" in name], version,
    )
    _verify_user_index_set(conn, "LegacyProjectionReceipts", [], version)
    _verify_user_index_set(conn, "MemoryProjectionReceipts", [], version)
    def binary(*columns):
        return tuple((column, False, "BINARY") for column in columns)
    _verify_constraint_indexes(conn, "MemoryEvents", [
        ("u", True, False, binary("EventId")),
        ("u", True, False, binary("IdempotencyKey")),
        ("u", True, False, binary("StreamId", "StreamVersion")),
    ], version)
    _verify_constraint_indexes(conn, "MemoryOutbox", [
        ("u", True, False, binary("OutboxId")),
        ("u", True, False, binary("EventId", "Destination")),
    ], version)
    _verify_constraint_indexes(conn, "LegacyProjectionReceipts", [
        ("pk", True, False, binary("EventId", "ProjectionName")),
    ], version)
    _verify_constraint_indexes(conn, "MemoryProjectionReceipts", [
        ("pk", True, False, binary("EventId", "Destination")),
    ], version)
    for index, columns, expected_unique in (
        ("uq_events_id", ["EventId"], True),
        ("uq_events_stream_ver", ["StreamId", "StreamVersion"], True),
        ("uq_events_idempotency", ["IdempotencyKey"], True),
        ("idx_events_stream", ["StreamId"], False),
        ("idx_events_type", ["EventType"], False),
        ("idx_events_recorded", ["RecordedAt"], False),
        ("idx_events_session", ["SessionId"], False),
        ("idx_events_sequence", ["JournalSequence"], False),
    ):
        _verify_index(
            conn, "MemoryEvents", index, columns, expected_unique, version
        )
    outbox_sql = _normalized_sql(conn, "table", "MemoryOutbox")
    for fragment in (
        "check(length(destination)>0andlength(destination)<=512)",
        "check(statusin('pending','processing','retry','projected','failed','dead_letter'))",
        "check(attempts>=0)",
        "check(maxattempts>=1)",
        "unique(eventid,destination)",
    ):
        if fragment not in outbox_sql:
            raise SchemaVerificationError(version, "event kernel", f"MemoryOutbox constraint drift: {fragment}")
    for index, expected, expected_unique in (
        ("uq_outbox_event_dest", ["EventId", "Destination"], True),
        ("uq_outbox_id", ["OutboxId"], True),
        ("idx_outbox_status", ["Status"], False),
        ("idx_outbox_next", ["NextAttemptAt"], False),
        ("idx_outbox_dest", ["Destination"], False),
    ):
        _verify_index(
            conn, "MemoryOutbox", index, expected, expected_unique, version
        )
    if not _has_event_fk(conn, "MemoryOutbox"):
        raise SchemaVerificationError(version, "event kernel", "MemoryOutbox EventId foreign key missing")
    for table, expected_pk in (
        ("LegacyProjectionReceipts", ["EventId", "ProjectionName"]),
        ("MemoryProjectionReceipts", ["EventId", "Destination"]),
    ):
        if _primary_key_columns(conn, table) != expected_pk:
            raise SchemaVerificationError(version, "event kernel", f"{table} primary-key drift")
        if not _has_event_fk(conn, table):
            raise SchemaVerificationError(version, "event kernel", f"{table} EventId foreign key missing")
    return version


def _verify_v1_schema(conn):
    """Compatibility verifier that inspects a partial event schema directly."""
    if not _table_exists(conn, "MemoryEvents"):
        raise SchemaVerificationError(1, "event kernel", "MemoryEvents missing")
    for trigger in ("trg_events_no_update", "trg_events_no_delete"):
        if not _object_exists(conn, "trigger", trigger):
            raise SchemaVerificationError(1, "event kernel", f"trigger {trigger} missing")
    required = {"JournalSequence", "EventId", "EventHash", "IdempotencyKey"}
    missing = required - _columns(conn, "MemoryEvents")
    if missing:
        raise SchemaVerificationError(1, "event kernel", f"MemoryEvents missing {sorted(missing)}")
    return 1


def run_migrations(conn):
    # PRAGMA foreign_keys is ignored inside an active transaction. Establish
    # and verify it before any migration savepoint so standalone callers get
    # the same referential guarantees as SqliteMemory-managed connections.
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise MigrationError(-1, "foreign-key enforcement", "PRAGMA foreign_keys could not be enabled")
    conn.execute(_META_DDL)
    conn.commit()
    known = {version: (description, checksum) for version, description, checksum, _ in _MIGRATIONS}
    for version, description, checksum in conn.execute(
        "SELECT version,description,checksum FROM _schema_migrations"
    ).fetchall():
        if known.get(version) != (description, checksum):
            raise MigrationError(version, description, "migration metadata checksum drift")
    current = _current_version(conn)
    applied = 0
    for version, description, checksum, up in _MIGRATIONS:
        if version <= current:
            continue
        savepoint = f"migration_{version}"
        try:
            conn.execute(f"SAVEPOINT {savepoint}")
            up(conn)
            conn.execute(
                "INSERT INTO _schema_migrations(version,description,applied_at,checksum) VALUES (?,?,?,?)",
                (
                    version, description,
                    datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    checksum,
                ),
            )
            conn.execute(f"RELEASE {savepoint}")
            applied += 1
            print(f"[Migrations] Applied v{version}: {description}")
        except Exception as exc:
            try:
                conn.execute(f"ROLLBACK TO {savepoint}")
                conn.execute(f"RELEASE {savepoint}")
            except Exception:
                pass
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError(version, description, str(exc)) from exc
    conn.commit()
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise MigrationError(
            _current_version(conn), "foreign-key enforcement",
            "PRAGMA foreign_keys became disabled",
        )
    verify_schema(conn)
    if applied:
        print(f"[Migrations] {applied} migration(s) applied (now at v{_current_version(conn)})")
    return applied


def current_schema_version(conn):
    conn.execute(_META_DDL)
    return _current_version(conn)
