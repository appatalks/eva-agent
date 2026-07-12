#!/usr/bin/env python3
"""
Phase 1 Event-Sourced Memory Kernel Tests — deterministic, no network,
no providers, no real databases.  Uses temp HOME / SQLite and mocked ADX.

Behavioral tests that verify:
  * Migration failures are fatal
  * Immutable event store (UPDATE/DELETE trigger enforcement)
  * Atomic transactions with proper rollback
  * Deterministic IDs and idempotency collision detection
  * Validation rejects oversized/invalid fields (never truncates)
  * Canonical JSON rejects NaN/Infinity, normalizes Unicode NFC
  * Exactly-once projections via receipts
  * Credential redaction before persistence
  * Outbox ordering, lease/claim, retry, dead-letter, dedup
  * Cursor-based pagination with JournalSequence
  * Connection cleanup with ResourceWarning detection
  * Read-only enforcement for query()/query_strict()
  * finalize_turn exactly-once semantics on duplicate/retry

Usage:
    python3 tools/test_phase1.py
"""

import concurrent.futures
import contextlib
import datetime
import gc
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
import warnings
from unittest import mock

# ── Make ResourceWarning fatal in Phase 1 tests ─────────────────────
warnings.filterwarnings("error", category=ResourceWarning)

# ── Ensure bridge package is importable ─────────────────────────────
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TOOLS_DIR)
sys.path.insert(0, TOOLS_DIR)

# Temp HOME so nothing touches real config/data
_TMP_HOME = tempfile.mkdtemp(prefix="eva_test_phase1_")
os.environ["HOME"] = _TMP_HOME
os.environ.setdefault("EVA_MEMORY_BACKEND", "sqlite")
os.environ.setdefault("EVA_MEMORY_DB", os.path.join(_TMP_HOME, "test.db"))
os.environ.pop("KUSTO_CLUSTER_URL", None)
os.environ.pop("KUSTO_DATABASE", None)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("EVA_ADX_PROJECTION", None)


def tearDownModule():
    from sqlite_memory import SqliteMemory
    for instance in list(SqliteMemory._instances.values()):
        try:
            instance.close()
        except Exception:
            pass
    gc.collect()
os.environ["EVA_MEMORY_READ_MODE"] = "legacy"


def _fresh_mem(name=None):
    """Create a fresh SqliteMemory in a unique temp DB."""
    # Clear singleton cache to allow fresh instances in tests
    from sqlite_memory import SqliteMemory
    path = os.path.join(_TMP_HOME, f"test_{name or uuid.uuid4().hex[:8]}.db")
    SqliteMemory._instances.pop(path, None)
    return SqliteMemory(path)


# ═══════════════════════════════════════════════════════════════════
#  A. Versioned SQLite Migrations — Fatal Failures
# ═══════════════════════════════════════════════════════════════════
class TestMigrations(unittest.TestCase):

    def _make_conn(self):
        path = os.path.join(_TMP_HOME, f"mig_{uuid.uuid4().hex[:8]}.db")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn, path

    def test_fresh_schema(self):
        """Fresh DB: migrations create metadata + event tables."""
        from bridge.migrations import run_migrations, current_schema_version
        conn, _ = self._make_conn()
        applied = run_migrations(conn)
        self.assertGreater(applied, 0)
        ver = current_schema_version(conn)
        self.assertGreaterEqual(ver, 1)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='MemoryEvents'"
        ).fetchone()
        self.assertIsNotNone(row)
        conn.close()

    def test_idempotent_migrations(self):
        """Running migrations twice is a no-op the second time."""
        from bridge.migrations import run_migrations, current_schema_version
        conn, _ = self._make_conn()
        first = run_migrations(conn)
        self.assertGreater(first, 0)
        ver1 = current_schema_version(conn)
        second = run_migrations(conn)
        self.assertEqual(second, 0)
        self.assertEqual(current_schema_version(conn), ver1)
        conn.close()

    def test_standalone_migrations_enable_foreign_keys(self):
        from bridge.migrations import run_migrations
        path = os.path.join(_TMP_HOME, f"fk_{uuid.uuid4().hex[:8]}.db")
        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 0)
        run_migrations(conn)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO MemoryProjectionReceipts(EventId,Destination) "
                "VALUES ('missing-event','test')"
            )
        conn.close()

    def test_malformed_preexisting_event_table_fails(self):
        """A malformed preexisting MemoryEvents table causes migration to fail fatally."""
        from bridge.migrations import run_migrations, MigrationError, SchemaVerificationError
        conn, _ = self._make_conn()
        # Create a malformed MemoryEvents table (missing required columns)
        conn.execute("CREATE TABLE MemoryEvents (BadCol TEXT)")
        conn.execute("CREATE TABLE _schema_migrations (version INTEGER PRIMARY KEY, description TEXT, applied_at TEXT, checksum TEXT)")
        conn.execute("INSERT INTO _schema_migrations VALUES (1, 'fake', '2026-01-01', 'x')")
        conn.commit()
        # Verification should fail because required columns are missing
        from bridge.migrations import verify_schema, SchemaVerificationError
        with self.assertRaises(SchemaVerificationError):
            verify_schema(conn)
        conn.close()

    def test_interrupted_migration_partial_db(self):
        """Partial migration (table exists without triggers) detected by verification."""
        from bridge.migrations import _verify_v1_schema, SchemaVerificationError
        conn, _ = self._make_conn()
        # Create table without triggers
        conn.execute("""
            CREATE TABLE MemoryEvents (
                JournalSequence INTEGER PRIMARY KEY AUTOINCREMENT,
                EventId TEXT NOT NULL, StreamId TEXT NOT NULL,
                StreamVersion INTEGER NOT NULL, EventType TEXT NOT NULL,
                SchemaVersion INTEGER, ActorType TEXT, ActorId TEXT,
                Origin TEXT, OccurredAt TEXT, RecordedAt TEXT,
                CorrelationId TEXT, CausationId TEXT, SessionId TEXT,
                TurnId TEXT, SourceMessageId TEXT, Trust REAL,
                Sensitivity TEXT, ConsentScope TEXT, Payload TEXT,
                PayloadHash TEXT, IdempotencyKey TEXT
            )
        """)
        conn.execute("CREATE TABLE MemoryOutbox (OutboxId TEXT, EventId TEXT, Destination TEXT, Status TEXT, Attempts INTEGER, MaxAttempts INTEGER, NextAttemptAt TEXT, CreatedAt TEXT, UpdatedAt TEXT, LastError TEXT)")
        conn.execute("CREATE TABLE LegacyProjectionReceipts (EventId TEXT, ProjectionName TEXT, ProjectedAt TEXT, RowCount INTEGER, PRIMARY KEY(EventId, ProjectionName))")
        conn.execute("CREATE UNIQUE INDEX uq_events_id ON MemoryEvents(EventId)")
        conn.execute("CREATE UNIQUE INDEX uq_events_stream_ver ON MemoryEvents(StreamId, StreamVersion)")
        conn.execute("CREATE UNIQUE INDEX uq_events_idempotency ON MemoryEvents(IdempotencyKey)")
        conn.execute("CREATE INDEX idx_events_stream ON MemoryEvents(StreamId)")
        conn.execute("CREATE INDEX idx_events_type ON MemoryEvents(EventType)")
        conn.execute("CREATE INDEX idx_events_recorded ON MemoryEvents(RecordedAt)")
        conn.execute("CREATE INDEX idx_events_session ON MemoryEvents(SessionId)")
        conn.execute("CREATE UNIQUE INDEX uq_outbox_id ON MemoryOutbox(OutboxId)")
        conn.execute("CREATE UNIQUE INDEX uq_outbox_event_dest ON MemoryOutbox(EventId, Destination)")
        conn.execute("CREATE INDEX idx_outbox_status ON MemoryOutbox(Status)")
        conn.execute("CREATE INDEX idx_outbox_next ON MemoryOutbox(NextAttemptAt)")
        conn.commit()
        # Missing triggers should cause verification failure
        with self.assertRaises(SchemaVerificationError) as ctx:
            _verify_v1_schema(conn)
        self.assertIn("trg_events_no_update", str(ctx.exception))
        conn.close()

    def test_migration_failure_is_fatal_for_sqlite_memory(self):
        """SqliteMemory construction raises if migration fails."""
        from bridge.migrations import MigrationError
        from sqlite_memory import SqliteMemory
        # Create a DB with a conflicting MemoryEvents that will block migration
        path = os.path.join(_TMP_HOME, f"fatal_{uuid.uuid4().hex[:8]}.db")
        conn = sqlite3.connect(path)
        # Create a table that conflicts with the migration's CHECK constraints
        conn.execute("CREATE TABLE MemoryEvents (x TEXT)")
        # Add schema_migrations claiming v0 is done but not v1
        conn.execute("CREATE TABLE _schema_migrations (version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL, checksum TEXT NOT NULL DEFAULT '')")
        conn.execute("INSERT INTO _schema_migrations VALUES (0, 'baseline', '2026-01-01', '')")
        conn.commit()
        conn.close()
        # SqliteMemory construction should raise
        SqliteMemory._instances.pop(path, None)
        with self.assertRaises(MigrationError):
            SqliteMemory(path)

    def test_seed_does_not_duplicate_on_repeat(self):
        """Creating SqliteMemory twice on same path does not duplicate seed rows."""
        from sqlite_memory import SqliteMemory
        path = os.path.join(_TMP_HOME, f"seed_{uuid.uuid4().hex[:8]}.db")
        SqliteMemory._instances.pop(path, None)
        mem1 = SqliteMemory(path)
        count1 = mem1.count("Knowledge")
        mem1.close()
        # Re-open same DB
        SqliteMemory._instances.pop(path, None)
        mem2 = SqliteMemory(path)
        count2 = mem2.count("Knowledge")
        self.assertEqual(count1, count2, "Seed rows must not duplicate")
        mem2.close()

    def test_creating_one_missing_table_does_not_reseed_all(self):
        """If only one table is newly created, only it gets seeded."""
        from sqlite_memory import SqliteMemory
        path = os.path.join(_TMP_HOME, f"partial_{uuid.uuid4().hex[:8]}.db")
        SqliteMemory._instances.pop(path, None)
        mem = SqliteMemory(path)
        initial_knowledge = mem.count("Knowledge")
        # Drop a non-seeded table and recreate
        conn = mem._conn()
        conn.execute("DROP TABLE IF EXISTS BackgroundActivity")
        conn.commit()
        mem.close()
        # Reopen — should recreate BackgroundActivity but NOT reseed Knowledge
        SqliteMemory._instances.pop(path, None)
        mem2 = SqliteMemory(path)
        self.assertEqual(mem2.count("Knowledge"), initial_knowledge)
        self.assertTrue(mem2.table_exists("BackgroundActivity"))
        mem2.close()


# ═══════════════════════════════════════════════════════════════════
#  A2. Production integration contracts
# ═══════════════════════════════════════════════════════════════════
class TestProductionContracts(unittest.TestCase):
    def test_public_repository_and_outer_rollback(self):
        from bridge.events import EventRepository
        self.assertEqual(EventRepository.__module__, "bridge.event_store")
        mem = _fresh_mem("outer_contract")
        repo = mem.event_repository()
        conn = mem._conn()
        conn.execute("INSERT INTO Knowledge(Entity,Relation,Value) VALUES ('Outer','x','rollback')")
        event = repo.append_event(
            stream_id="outer:contract", event_type="outer.created", payload={"x": 1},
            idempotency_key="outer-contract",
        )
        conn.rollback()
        self.assertIsNone(repo.get_event(event["EventId"]))
        self.assertEqual(mem.query("SELECT * FROM Knowledge WHERE Entity='Outer'"), [])
        mem.close()

    def test_finalize_all_projections_is_exactly_once(self):
        from bridge.cognition import _extract_entity_candidates, _extract_explicit_user_facts
        from bridge.finalize import finalize_turn
        mem = _fresh_mem("projection_contract")
        repo = mem.event_repository()
        kwargs = {
            "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
            "user_message": "My name is Alice. Aurora Project and Orion Initiative matter.",
            "assistant_message": "Thanks Alice. " + "Detailed response. " * 60,
            "model": "test", "correlation_id": str(uuid.uuid4()),
            "actor_id": str(uuid.uuid4()),
            "extract_facts_fn": _extract_explicit_user_facts,
            "extract_candidates_fn": _extract_entity_candidates,
        }
        first = finalize_turn(mem, repo, **kwargs)
        tables = ("Conversations", "Knowledge", "HeuristicsIndex", "EmotionState", "Reflections", "MemorySummaries")
        before = {table: mem.count(table) for table in tables}
        second = finalize_turn(mem, repo, **kwargs)
        self.assertEqual(first["event_ids"], second["event_ids"])
        self.assertEqual(before, {table: mem.count(table) for table in tables})
        event_ids = {row["EventId"] for row in repo.events_since(0, 100)}
        receipts = {row["EventId"] for row in mem.query("SELECT EventId FROM LegacyProjectionReceipts")}
        self.assertTrue(event_ids.issubset(receipts))
        mem.close()

    def test_kusto_read_selection_still_uses_local_journal(self):
        from bridge import state as st
        from bridge.cognition import _post_response_reflection
        mem = _fresh_mem("kusto_read_contract")
        saved = (st.memory_backend, st.sqlite_mem, st.cognition_enabled)
        try:
            st.memory_backend, st.sqlite_mem, st.cognition_enabled = "kusto", mem, False
            envelope = {
                "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
                "user_id": str(uuid.uuid4()), "origin": "api",
            }
            result = _post_response_reflection("hello", "world", "test", envelope=envelope)
            self.assertEqual(mem.count("MemoryEvents"), len(result["event_ids"]))
        finally:
            st.memory_backend, st.sqlite_mem, st.cognition_enabled = saved
            mem.close()

    def test_frontend_provider_envelope_matrix(self):
        with open(os.path.join(PROJECT_ROOT, "core/js/options.js")) as handle:
            self.assertIn("newEnvelopeTurn()", handle.read())
        with open(os.path.join(PROJECT_ROOT, "core/js/sessions.js")) as handle:
            self.assertIn("finalizeDirectProviderTurn", handle.read())
        for filename in ("gpt-core.js", "gl-google.js", "lm-studio.js", "copilot.js"):
            with open(os.path.join(PROJECT_ROOT, "core/js", filename)) as handle:
                self.assertIn("finalizeDirectProviderTurn", handle.read(), filename)

    def test_outbox_failure_rolls_back_event_with_owned_and_supplied_transaction(self):
        from bridge.events import EventStoreError
        for supplied in (False, True):
            mem = _fresh_mem("outbox_atomic_" + str(supplied))
            repo = mem.event_repository()
            conn = mem._conn()
            conn.execute("""
                CREATE TRIGGER fail_test_outbox BEFORE INSERT ON MemoryOutbox
                BEGIN SELECT RAISE(ABORT,'forced outbox failure'); END
            """)
            if supplied:
                conn.execute("BEGIN IMMEDIATE")
            with self.assertRaises(EventStoreError):
                repo.append_event(
                    connection=conn if supplied else None,
                    stream_id="atomic:outbox", event_type="atomic.created",
                    payload={"x": 1}, consent_scope="cloud_allowed",
                    idempotency_key="atomic-outbox-" + str(supplied),
                )
            if supplied:
                conn.rollback()
            self.assertEqual(mem.count("MemoryEvents"), 0)
            self.assertEqual(mem.count("MemoryOutbox"), 0)
            mem.close()

    def test_schema_verifier_rejects_noop_trigger_and_missing_fk(self):
        from bridge.migrations import SchemaVerificationError, verify_schema
        mem = _fresh_mem("schema_manifest")
        conn = mem._conn()
        conn.execute("DROP TRIGGER trg_events_no_update")
        conn.execute("CREATE TRIGGER trg_events_no_update BEFORE UPDATE ON MemoryEvents BEGIN SELECT 1; END")
        with self.assertRaises(SchemaVerificationError):
            verify_schema(conn)
        conn.execute("DROP TRIGGER trg_events_no_update")
        conn.execute("""
            CREATE TRIGGER trg_events_no_update BEFORE UPDATE ON MemoryEvents
            BEGIN SELECT RAISE(ABORT,'MemoryEvents is immutable: UPDATE not allowed'); END
        """)
        conn.execute("DROP TABLE MemoryOutbox")
        conn.execute("""
            CREATE TABLE MemoryOutbox (
                OutboxId TEXT UNIQUE,EventId TEXT,Destination TEXT,Status TEXT,
                Attempts INTEGER,MaxAttempts INTEGER,NextAttemptAt TEXT,LeaseUntil TEXT,
                CreatedAt TEXT,UpdatedAt TEXT,LastError TEXT,UNIQUE(EventId,Destination)
            )
        """)
        for sql in (
            "CREATE UNIQUE INDEX uq_outbox_id ON MemoryOutbox(OutboxId)",
            "CREATE UNIQUE INDEX uq_outbox_event_dest ON MemoryOutbox(EventId,Destination)",
            "CREATE INDEX idx_outbox_status ON MemoryOutbox(Status)",
            "CREATE INDEX idx_outbox_next ON MemoryOutbox(NextAttemptAt)",
        ):
            conn.execute(sql)
        with self.assertRaises(SchemaVerificationError):
            verify_schema(conn)
        mem.close()

    def test_schema_verifier_rejects_wrong_index_and_receipt_keys(self):
        from bridge.migrations import SchemaVerificationError, verify_schema
        mem = _fresh_mem("schema_keys")
        conn = mem._conn()
        conn.execute("DROP INDEX uq_events_id")
        conn.execute("CREATE UNIQUE INDEX uq_events_id ON MemoryEvents(StreamId,StreamVersion)")
        with self.assertRaises(SchemaVerificationError):
            verify_schema(conn)
        conn.execute("DROP INDEX uq_events_id")
        conn.execute("CREATE UNIQUE INDEX uq_events_id ON MemoryEvents(EventId)")
        conn.execute("DROP TABLE MemoryProjectionReceipts")
        conn.execute("""
            CREATE TABLE MemoryProjectionReceipts (
                EventId TEXT, Destination TEXT, ProjectedAt TEXT
            )
        """)
        with self.assertRaises(SchemaVerificationError):
            verify_schema(conn)
        mem.close()

    def test_schema_verifier_rejects_outbox_index_drift(self):
        from bridge.migrations import SchemaVerificationError, verify_schema
        mem = _fresh_mem("schema_outbox_index")
        conn = mem._conn()
        conn.execute("DROP INDEX idx_outbox_status")
        conn.execute("CREATE INDEX idx_outbox_status ON MemoryOutbox(NextAttemptAt)")
        with self.assertRaises(SchemaVerificationError):
            verify_schema(conn)
        mem.close()

    def test_schema_verifier_rejects_generated_columns_and_partial_indexes(self):
        from bridge.migrations import SchemaVerificationError, verify_schema
        mem = _fresh_mem("schema_hidden_partial")
        conn = mem._conn()
        conn.execute(
            "ALTER TABLE MemoryOutbox ADD COLUMN ExtraGenerated TEXT "
            "GENERATED ALWAYS AS (Destination) VIRTUAL"
        )
        with self.assertRaises(SchemaVerificationError):
            verify_schema(conn)
        mem.close()

        mem = _fresh_mem("schema_partial_index")
        conn = mem._conn()
        conn.execute("DROP INDEX idx_outbox_status")
        conn.execute(
            "CREATE INDEX idx_outbox_status ON MemoryOutbox(Status) "
            "WHERE Status='pending'"
        )
        with self.assertRaises(SchemaVerificationError) as ctx:
            verify_schema(conn)
        self.assertIn("partial", str(ctx.exception))
        mem.close()

    def test_draft_upgrade_rebuilds_exact_schema_and_preserves_rows(self):
        from bridge.migrations import (
            _META_DDL, _MIGRATIONS, run_migrations, verify_schema,
        )
        path = os.path.join(_TMP_HOME, f"draft_{uuid.uuid4().hex[:8]}.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(_META_DDL)
        for version, description, checksum, _ in _MIGRATIONS[:2]:
            conn.execute(
                "INSERT INTO _schema_migrations(version,description,applied_at,checksum) "
                "VALUES (?,?,?,?)",
                (version, description, "2026-01-01T00:00:00Z", checksum),
            )
        conn.execute("""
            CREATE TABLE MemoryEvents (
                JournalSequence INTEGER PRIMARY KEY AUTOINCREMENT,
                EventId TEXT NOT NULL UNIQUE, StreamId TEXT NOT NULL,
                StreamVersion INTEGER NOT NULL, EventType TEXT NOT NULL,
                SchemaVersion INTEGER NOT NULL DEFAULT 1,
                ActorType TEXT NOT NULL, ActorId TEXT NOT NULL DEFAULT '',
                Origin TEXT NOT NULL, OccurredAt TEXT NOT NULL,
                RecordedAt TEXT NOT NULL, CorrelationId TEXT NOT NULL DEFAULT '',
                CausationId TEXT NOT NULL DEFAULT '', SessionId TEXT NOT NULL DEFAULT '',
                TurnId TEXT NOT NULL DEFAULT '', SourceMessageId TEXT NOT NULL DEFAULT '',
                Trust REAL NOT NULL, Sensitivity TEXT NOT NULL,
                ConsentScope TEXT NOT NULL, Payload TEXT NOT NULL,
                PayloadHash TEXT NOT NULL, IdempotencyKey TEXT NOT NULL UNIQUE,
                UNIQUE(StreamId,StreamVersion)
            )
        """)
        conn.execute("""
            CREATE TABLE MemoryOutbox (
                OutboxId TEXT NOT NULL UNIQUE, EventId TEXT NOT NULL,
                Destination TEXT NOT NULL DEFAULT 'adx',
                Status TEXT NOT NULL DEFAULT 'pending', Attempts INTEGER NOT NULL DEFAULT 0,
                NextAttemptAt TEXT DEFAULT '', CreatedAt TEXT NOT NULL,
                UpdatedAt TEXT NOT NULL, LastError TEXT DEFAULT '',
                UNIQUE(EventId,Destination),
                FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId)
            )
        """)
        conn.execute("""
            CREATE TABLE LegacyProjectionReceipts (
                EventId TEXT NOT NULL, ProjectionName TEXT NOT NULL,
                ProjectedAt TEXT NOT NULL, RowCount INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(EventId,ProjectionName),
                FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId)
            )
        """)
        event_values = (
            "event-draft", "stream:draft", 0, "draft.created", 1,
            "system", "", "bridge", "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z", "", "", "", "", "", 0.5,
            "normal", "local_only", "{}", "payload-hash", "draft-key",
        )
        conn.execute(
            "INSERT INTO MemoryEvents (EventId,StreamId,StreamVersion,EventType,"
            "SchemaVersion,ActorType,ActorId,Origin,OccurredAt,RecordedAt,CorrelationId,"
            "CausationId,SessionId,TurnId,SourceMessageId,Trust,Sensitivity,ConsentScope,"
            "Payload,PayloadHash,IdempotencyKey) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            event_values,
        )
        conn.execute(
            "INSERT INTO MemoryOutbox (OutboxId,EventId,Destination,Status,Attempts,"
            "NextAttemptAt,CreatedAt,UpdatedAt,LastError) VALUES (?,?,?,?,?,?,?,?,?)",
            ("outbox-draft", "event-draft", "adx", "retry", 2, "",
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "retry"),
        )
        conn.execute(
            "INSERT INTO LegacyProjectionReceipts VALUES (?,?,?,?)",
            ("event-draft", "draft", "2026-01-01T00:00:00Z", 1),
        )
        conn.commit()

        self.assertEqual(run_migrations(conn), 3)
        self.assertEqual(verify_schema(conn), 4)
        event = conn.execute(
            "SELECT EventHash FROM MemoryEvents WHERE EventId='event-draft'"
        ).fetchone()
        self.assertEqual(len(event[0]), 64)
        self.assertNotEqual(event[0], "payload-hash")
        from bridge.event_store import EventRepositoryV2
        from bridge.events import IdempotencyCollisionError
        repo = EventRepositoryV2(lambda: conn, installation_id="migration-replay-test")
        replayed = repo.append_event(
            stream_id="stream:draft", event_type="draft.created", payload={},
            schema_version=1, actor_type="system", actor_id="", origin="bridge",
            correlation_id="", causation_id="", session_id="", turn_id="",
            source_message_id="", trust=0.5, sensitivity="normal",
            consent_scope="local_only", idempotency_key="draft-key",
        )
        self.assertEqual(replayed["EventId"], "event-draft")
        with self.assertRaises(IdempotencyCollisionError):
            repo.append_event(
                stream_id="stream:draft", event_type="draft.created",
                payload={"changed": True}, actor_type="system", actor_id="",
                origin="bridge", trust=0.5, sensitivity="normal",
                consent_scope="local_only", idempotency_key="draft-key",
            )
        outbox = conn.execute(
            "SELECT Attempts,MaxAttempts,LeaseUntil FROM MemoryOutbox "
            "WHERE OutboxId='outbox-draft'"
        ).fetchone()
        self.assertEqual(tuple(outbox), (2, 10, ""))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM LegacyProjectionReceipts").fetchone()[0], 1
        )
        event_hash_info = {
            row[1]: row for row in conn.execute("PRAGMA table_xinfo(MemoryEvents)")
        }["EventHash"]
        self.assertIsNone(event_hash_info[4])
        conn.close()

    def test_recorded_v2_sparse_fields_are_repaired_with_rows_preserved(self):
        from bridge.migrations import (
            _META_DDL, _MIGRATIONS, _create_event_support,
            run_migrations, verify_schema,
        )
        path = os.path.join(_TMP_HOME, f"v2_sparse_{uuid.uuid4().hex[:8]}.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(_META_DDL)
        _create_event_support(conn)
        for version, description, checksum, _ in _MIGRATIONS[:3]:
            conn.execute(
                "INSERT INTO _schema_migrations(version,description,applied_at,checksum) "
                "VALUES (?,?,?,?)",
                (version, description, "2026-01-01T00:00:00Z", checksum),
            )
        conn.execute(
            "INSERT INTO MemoryEvents (EventId,StreamId,StreamVersion,EventType,"
            "SchemaVersion,ActorType,ActorId,Origin,OccurredAt,RecordedAt,CorrelationId,"
            "CausationId,SessionId,TurnId,SourceMessageId,Trust,Sensitivity,ConsentScope,"
            "Payload,PayloadHash,EventHash,IdempotencyKey) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "event-v2-sparse", "stream:v2-sparse", 0, "sparse.created", 1,
                "system", "", "bridge", "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z", "", "", "", "", "", 0.5,
                "normal", "cloud_allowed", "{}", "payload-hash", "event-hash",
                "sparse-key",
            ),
        )
        conn.execute("DROP TABLE MemoryProjectionReceipts")
        conn.execute("DROP TABLE LegacyProjectionReceipts")
        conn.execute("DROP TABLE MemoryOutbox")
        conn.execute("""
            CREATE TABLE MemoryOutbox (
                OutboxId TEXT NOT NULL UNIQUE, EventId TEXT NOT NULL,
                Destination TEXT NOT NULL DEFAULT 'adx',
                Status TEXT NOT NULL DEFAULT 'pending',
                MaxAttempts INTEGER NOT NULL DEFAULT 10,
                NextAttemptAt TEXT DEFAULT '', LeaseUntil TEXT DEFAULT '',
                CreatedAt TEXT NOT NULL, UpdatedAt TEXT NOT NULL,
                LastError TEXT DEFAULT '', UNIQUE(EventId,Destination),
                FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId)
            )
        """)
        conn.execute("""
            CREATE TABLE LegacyProjectionReceipts (
                EventId TEXT NOT NULL, ProjectionName TEXT NOT NULL,
                ProjectedAt TEXT NOT NULL, RowCount INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(EventId,ProjectionName),
                FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId)
            )
        """)
        conn.execute("""
            CREATE TABLE MemoryProjectionReceipts (
                EventId TEXT NOT NULL, Destination TEXT NOT NULL,
                PRIMARY KEY(EventId,Destination),
                FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId)
            )
        """)
        conn.execute(
            "INSERT INTO MemoryOutbox (OutboxId,EventId,Destination,Status,"
            "MaxAttempts,NextAttemptAt,LeaseUntil,CreatedAt,UpdatedAt,LastError) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "outbox-v2-sparse", "event-v2-sparse", "adx", "retry", 10,
                "", "", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "retry",
            ),
        )
        conn.execute(
            "INSERT INTO LegacyProjectionReceipts VALUES (?,?,?,?)",
            ("event-v2-sparse", "sparse", "2026-01-01T00:00:00Z", 1),
        )
        conn.execute(
            "INSERT INTO MemoryProjectionReceipts(EventId,Destination) VALUES (?,?)",
            ("event-v2-sparse", "adx"),
        )
        conn.commit()

        self.assertEqual(run_migrations(conn), 2)
        self.assertEqual(verify_schema(conn), 4)
        outbox = conn.execute(
            "SELECT Attempts,Status FROM MemoryOutbox WHERE OutboxId='outbox-v2-sparse'"
        ).fetchone()
        self.assertEqual(tuple(outbox), (0, "retry"))
        receipt = conn.execute(
            "SELECT ProjectedAt FROM MemoryProjectionReceipts "
            "WHERE EventId='event-v2-sparse' AND Destination='adx'"
        ).fetchone()
        self.assertTrue(receipt[0])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM MemoryEvents").fetchone()[0], 1
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM LegacyProjectionReceipts").fetchone()[0], 1
        )
        conn.close()

    def test_recorded_v3_event_hash_is_repaired_for_repository_replay(self):
        from bridge.event_store import EventRepositoryV2
        from bridge.events import IdempotencyCollisionError
        from bridge.migrations import (
            _META_DDL, _MIGRATIONS, _create_event_support,
            run_migrations, verify_schema,
        )
        path = os.path.join(_TMP_HOME, f"v3_hash_{uuid.uuid4().hex[:8]}.db")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(_META_DDL)
        _create_event_support(conn)
        for version, description, checksum, _ in _MIGRATIONS[:4]:
            conn.execute(
                "INSERT INTO _schema_migrations(version,description,applied_at,checksum) "
                "VALUES (?,?,?,?)",
                (version, description, "2026-01-01T00:00:00Z", checksum),
            )
        conn.execute(
            "INSERT INTO MemoryEvents (EventId,StreamId,StreamVersion,EventType,"
            "SchemaVersion,ActorType,ActorId,Origin,OccurredAt,RecordedAt,CorrelationId,"
            "CausationId,SessionId,TurnId,SourceMessageId,Trust,Sensitivity,ConsentScope,"
            "Payload,PayloadHash,EventHash,IdempotencyKey) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "event-v3-hash", "stream:v3-hash", 0, "v3.created", 1,
                "system", "", "bridge", "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z", "", "", "", "", "", 0.5,
                "normal", "local_only", "{}", "legacy-payload-hash",
                "legacy-payload-hash", "v3-hash-key",
            ),
        )
        conn.commit()

        self.assertEqual(run_migrations(conn), 1)
        self.assertEqual(verify_schema(conn), 4)
        repaired = conn.execute(
            "SELECT EventHash FROM MemoryEvents WHERE EventId='event-v3-hash'"
        ).fetchone()[0]
        self.assertEqual(len(repaired), 64)
        self.assertNotEqual(repaired, "legacy-payload-hash")

        repo = EventRepositoryV2(lambda: conn, installation_id="v3-replay")
        replay = repo.append_event(
            stream_id="stream:v3-hash", event_type="v3.created", payload={},
            actor_type="system", actor_id="", origin="bridge", trust=0.5,
            sensitivity="normal", consent_scope="local_only",
            idempotency_key="v3-hash-key",
        )
        self.assertEqual(replay["EventId"], "event-v3-hash")
        with self.assertRaises(IdempotencyCollisionError):
            repo.append_event(
                stream_id="stream:v3-hash", event_type="v3.created",
                payload={"changed": True}, actor_type="system", actor_id="",
                origin="bridge", trust=0.5, sensitivity="normal",
                consent_scope="local_only", idempotency_key="v3-hash-key",
            )
        conn.close()

    def test_schema_verifier_rejects_outbox_without_attempts(self):
        from bridge.migrations import SchemaVerificationError, verify_schema
        mem = _fresh_mem("schema_outbox_attempts")
        conn = mem._conn()
        conn.execute("DROP TABLE MemoryOutbox")
        conn.execute("""
            CREATE TABLE MemoryOutbox (
                OutboxId TEXT NOT NULL UNIQUE,
                EventId TEXT NOT NULL,
                Destination TEXT NOT NULL DEFAULT 'adx',
                Status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(Status IN ('pending','processing','retry','projected','failed','dead_letter')),
                MaxAttempts INTEGER NOT NULL DEFAULT 10,
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
            "CREATE UNIQUE INDEX uq_outbox_id ON MemoryOutbox(OutboxId)",
            "CREATE UNIQUE INDEX uq_outbox_event_dest ON MemoryOutbox(EventId,Destination)",
            "CREATE INDEX idx_outbox_status ON MemoryOutbox(Status)",
            "CREATE INDEX idx_outbox_next ON MemoryOutbox(NextAttemptAt)",
            "CREATE INDEX idx_outbox_dest ON MemoryOutbox(Destination)",
        ):
            conn.execute(sql)
        with self.assertRaises(SchemaVerificationError) as ctx:
            verify_schema(conn)
        self.assertIn("Attempts", str(ctx.exception))
        mem.close()

    def test_schema_verifier_rejects_incomplete_receipt_columns(self):
        from bridge.migrations import SchemaVerificationError, verify_schema
        cases = (
            (
                "LegacyProjectionReceipts", "RowCount",
                """CREATE TABLE LegacyProjectionReceipts (
                    EventId TEXT NOT NULL, ProjectionName TEXT NOT NULL,
                    ProjectedAt TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now') || 'Z'),
                    PRIMARY KEY(EventId,ProjectionName),
                    FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId))""",
            ),
            (
                "MemoryProjectionReceipts", "ProjectedAt",
                """CREATE TABLE MemoryProjectionReceipts (
                    EventId TEXT NOT NULL, Destination TEXT NOT NULL,
                    PRIMARY KEY(EventId,Destination),
                    FOREIGN KEY(EventId) REFERENCES MemoryEvents(EventId))""",
            ),
        )
        for table, missing, ddl in cases:
            mem = _fresh_mem("schema_receipt_" + table)
            conn = mem._conn()
            conn.execute(f"DROP TABLE {table}")
            conn.execute(ddl)
            with self.assertRaises(SchemaVerificationError) as ctx:
                verify_schema(conn)
            self.assertIn(missing, str(ctx.exception))
            mem.close()

    def test_delayed_turn_retry_returns_original_events(self):
        from bridge.finalize import finalize_turn
        mem = _fresh_mem("delayed_retry")
        repo = mem.event_repository()
        session_id = str(uuid.uuid4())
        turn_a, turn_b = str(uuid.uuid4()), str(uuid.uuid4())
        common = {"mem": mem, "repo": repo, "session_id": session_id, "model": "test"}
        first = finalize_turn(
            **common, turn_id=turn_a, user_message="first", assistant_message="answer one"
        )
        finalize_turn(
            **common, turn_id=turn_b, user_message="second", assistant_message="answer two"
        )
        before = mem.count("MemoryEvents")
        replay = finalize_turn(
            **common, turn_id=turn_a, user_message="first", assistant_message="answer one"
        )
        self.assertEqual(first["event_ids"], replay["event_ids"])
        self.assertEqual(first["exchange_ordinal"], replay["exchange_ordinal"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(mem.count("MemoryEvents"), before)
        mem.close()

    def test_goal_and_skill_create_replay_by_request_id(self):
        from bridge import state as st
        from bridge.core import BridgeHandler
        mem = _fresh_mem("handler_replay")
        saved = (st.memory_backend, st.sqlite_mem, st.bridge_bind_address)
        envelope = {
            "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
        }
        try:
            st.memory_backend, st.sqlite_mem, st.bridge_bind_address = "sqlite", mem, "127.0.0.1"
            handler = object.__new__(BridgeHandler)
            responses = []
            handler._json_response = lambda status, data: responses.append((status, data))
            goal = {
                **envelope, "title": "Replay Goal", "description": "same",
                "category": "relational", "priority": 50, "relatedTopics": "replay",
            }
            handler._read_json_body = lambda: (dict(goal), "")
            handler._goals_create(); handler._goals_create()
            self.assertEqual([responses[0][0], responses[1][0]], [201, 200])
            self.assertTrue(responses[1][1]["idempotent"])
            self.assertEqual(mem.count("Goals", "Title=?", ("Replay Goal",)), 1)
            handler._read_json_body = lambda: ({**goal, "title": "Different"}, "")
            handler._goals_create()
            self.assertEqual(responses[-1][0], 409)

            responses.clear()
            skill = {
                **envelope, "request_id": str(uuid.uuid4()),
                "name": "Replay Skill", "description": "same",
                "instructions": "Do one safe thing.", "tools": "", "tags": "replay",
            }
            handler._read_json_body = lambda: (dict(skill), "")
            handler._skills_create(); handler._skills_create()
            self.assertEqual([responses[0][0], responses[1][0]], [201, 200])
            self.assertEqual(mem.count("Skills", "Name=?", ("Replay Skill",)), 1)
        finally:
            st.memory_backend, st.sqlite_mem, st.bridge_bind_address = saved
            mem.close()

    def test_delayed_goal_and_skill_patch_replays_original_result(self):
        from bridge import state as st
        from bridge.core import BridgeHandler
        mem = _fresh_mem("delayed_mutation_replay")
        saved = (st.memory_backend, st.sqlite_mem, st.bridge_bind_address)

        def envelope():
            return {
                "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
            }

        try:
            st.memory_backend, st.sqlite_mem, st.bridge_bind_address = "sqlite", mem, "127.0.0.1"
            handler = object.__new__(BridgeHandler)
            handler.headers = {}
            responses = []
            handler._json_response = lambda status, data: responses.append((status, data))

            goal_create = {
                **envelope(), "title": "Original Goal", "description": "base",
                "category": "relational", "priority": 50, "relatedTopics": "",
            }
            handler._read_json_body = lambda: (dict(goal_create), "")
            handler._goals_create()
            goal_id = responses[-1][1]["goal"]["GoalId"]

            goal_patch_a = {**envelope(), "title": "Goal Version A"}
            handler._read_json_body = lambda: (dict(goal_patch_a), "")
            handler._goals_patch(goal_id)
            original_goal_response = responses[-1][1]["goal"]
            goal_patch_b = {**envelope(), "title": "Goal Version B"}
            handler._read_json_body = lambda: (dict(goal_patch_b), "")
            handler._goals_patch(goal_id)
            handler._read_json_body = lambda: (dict(goal_patch_a), "")
            handler._goals_patch(goal_id)
            self.assertEqual(responses[-1][0], 200)
            self.assertTrue(responses[-1][1]["idempotent"])
            self.assertEqual(responses[-1][1]["goal"], original_goal_response)
            handler._read_json_body = lambda: ({**goal_patch_a, "title": "Changed Command"}, "")
            handler._goals_patch(goal_id)
            self.assertEqual(responses[-1][0], 409)

            skill_create = {
                **envelope(), "name": "Replay Skill", "description": "base",
                "instructions": "Perform one bounded action.", "tools": "", "tags": "",
            }
            handler._read_json_body = lambda: (dict(skill_create), "")
            handler._skills_create()
            skill_id = responses[-1][1]["skill"]["SkillId"]
            skill_patch_a = {**envelope(), "description": "Skill Version A"}
            handler._read_json_body = lambda: (dict(skill_patch_a), "")
            handler._skills_patch(skill_id)
            original_skill_response = responses[-1][1]["skill"]
            skill_patch_b = {**envelope(), "description": "Skill Version B"}
            handler._read_json_body = lambda: (dict(skill_patch_b), "")
            handler._skills_patch(skill_id)
            handler._read_json_body = lambda: (dict(skill_patch_a), "")
            handler._skills_patch(skill_id)
            self.assertEqual(responses[-1][0], 200)
            self.assertTrue(responses[-1][1]["idempotent"])
            self.assertEqual(responses[-1][1]["skill"], original_skill_response)
            handler._read_json_body = lambda: ({
                **skill_patch_a, "description": "Changed Command",
            }, "")
            handler._skills_patch(skill_id)
            self.assertEqual(responses[-1][0], 409)

            events = mem.query(
                "SELECT EventType,Payload FROM MemoryEvents "
                "WHERE EventType IN ('goal.updated','skill.updated')"
            )
            self.assertEqual(sum(e["EventType"] == "goal.updated" for e in events), 2)
            self.assertEqual(sum(e["EventType"] == "skill.updated" for e in events), 2)
            for event in events:
                receipt = json.loads(event["Payload"])
                self.assertEqual(len(receipt["command_hash"]), 64)
                self.assertIn("result", receipt)
        finally:
            st.memory_backend, st.sqlite_mem, st.bridge_bind_address = saved
            mem.close()

    def test_unicode_mutation_replay_matches_first_response_exactly(self):
        import unicodedata
        from bridge import state as st
        from bridge.core import BridgeHandler
        mem = _fresh_mem("unicode_mutation_replay")
        saved = (st.memory_backend, st.sqlite_mem, st.bridge_bind_address)

        def envelope():
            return {
                "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
            }

        try:
            st.memory_backend, st.sqlite_mem, st.bridge_bind_address = "sqlite", mem, "127.0.0.1"
            handler = object.__new__(BridgeHandler)
            handler.headers = {}
            responses = []
            handler._json_response = lambda status, data: responses.append((status, data))

            goal_create = {
                **envelope(), "title": "Unicode Goal", "description": "",
                "category": "relational", "priority": 50, "relatedTopics": "",
            }
            handler._read_json_body = lambda: (dict(goal_create), "")
            handler._goals_create()
            goal_id = responses[-1][1]["goal"]["GoalId"]
            nfd_title = "Cafe\u0301"
            goal_patch = {**envelope(), "title": nfd_title}
            handler._read_json_body = lambda: (dict(goal_patch), "")
            handler._goals_patch(goal_id)
            first_goal = responses[-1][1]["goal"]
            handler._goals_patch(goal_id)
            replay_goal = responses[-1][1]["goal"]
            self.assertEqual(first_goal, replay_goal)
            self.assertEqual(first_goal["Title"], unicodedata.normalize("NFC", nfd_title))
            self.assertEqual(
                mem.query(
                    "SELECT Title FROM Goals WHERE GoalId=? ORDER BY rowid DESC LIMIT 1",
                    (goal_id,),
                )[0]["Title"],
                first_goal["Title"],
            )

            skill_create = {
                **envelope(), "name": "Unicode Skill", "description": "",
                "instructions": "Act safely.", "tools": "", "tags": "",
            }
            handler._read_json_body = lambda: (dict(skill_create), "")
            handler._skills_create()
            skill_id = responses[-1][1]["skill"]["SkillId"]
            nfd_description = "Resume\u0301"
            skill_patch = {**envelope(), "description": nfd_description}
            handler._read_json_body = lambda: (dict(skill_patch), "")
            handler._skills_patch(skill_id)
            first_skill = responses[-1][1]["skill"]
            handler._skills_patch(skill_id)
            replay_skill = responses[-1][1]["skill"]
            self.assertEqual(first_skill, replay_skill)
            self.assertEqual(
                first_skill["Description"],
                unicodedata.normalize("NFC", nfd_description),
            )
        finally:
            st.memory_backend, st.sqlite_mem, st.bridge_bind_address = saved
            mem.close()

    def test_envelope_only_goal_and_skill_patch_are_rejected(self):
        from bridge import state as st
        from bridge.core import BridgeHandler
        mem = _fresh_mem("empty_mutation_reject")
        saved = (st.memory_backend, st.sqlite_mem, st.bridge_bind_address)

        def envelope():
            return {
                "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
            }

        try:
            st.memory_backend, st.sqlite_mem, st.bridge_bind_address = "sqlite", mem, "127.0.0.1"
            handler = object.__new__(BridgeHandler)
            handler.headers = {}
            responses = []
            handler._json_response = lambda status, data: responses.append((status, data))
            before = mem.count("MemoryEvents")
            with mock.patch.object(
                handler, "_memory_context_required",
                side_effect=AssertionError("backend must not be touched"),
            ) as backend_context:
                handler._read_json_body = lambda: (envelope(), "")
                handler._goals_patch("goal-001")
                self.assertEqual(responses[-1][0], 400)
                self.assertEqual(mem.count("MemoryEvents"), before)
                handler._read_json_body = lambda: (envelope(), "")
                handler._skills_patch("skill-morning-briefing")
                self.assertEqual(responses[-1][0], 400)
                self.assertEqual(mem.count("MemoryEvents"), before)
                backend_context.assert_not_called()
        finally:
            st.memory_backend, st.sqlite_mem, st.bridge_bind_address = saved
            mem.close()

    def test_invalid_goal_and_skill_create_never_touch_backend(self):
        from bridge import state as st
        from bridge.core import BridgeHandler
        mem = _fresh_mem("invalid_create_order")
        saved = (st.memory_backend, st.sqlite_mem, st.bridge_bind_address)
        valid_envelope = {
            "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
        }
        cases = (
            ("_goals_create", []),
            ("_goals_create", {
                **valid_envelope, "request_id": "invalid",
                "title": "Goal", "category": "relational", "priority": 50,
            }),
            ("_goals_create", dict(valid_envelope)),
            ("_skills_create", []),
            ("_skills_create", {
                **valid_envelope, "request_id": "invalid",
                "name": "Skill", "instructions": "Act safely.",
            }),
            ("_skills_create", {**valid_envelope, "name": "Missing instructions"}),
        )
        try:
            st.memory_backend, st.sqlite_mem, st.bridge_bind_address = "kusto", mem, "127.0.0.1"
            handler = object.__new__(BridgeHandler)
            handler.headers = {}
            responses = []
            handler._json_response = lambda status, data: responses.append((status, data))
            before = mem.count("MemoryEvents")
            with mock.patch.object(
                handler, "_memory_context_required",
                side_effect=AssertionError("backend must not be touched"),
            ) as backend_context:
                for method, payload in cases:
                    handler._read_json_body = lambda payload=payload: (payload, "")
                    getattr(handler, method)()
                    self.assertEqual(responses[-1][0], 400, (method, payload))
                backend_context.assert_not_called()
            self.assertEqual(mem.count("MemoryEvents"), before)
        finally:
            st.memory_backend, st.sqlite_mem, st.bridge_bind_address = saved
            mem.close()

    def test_confirmed_backend_switch_replay_returns_stored_success(self):
        from bridge import core
        from bridge import state as st
        from bridge.core import BridgeHandler
        mem = _fresh_mem("backend_switch_replay")
        saved = (st.sqlite_mem, st.egress_mode, st.memory_backend)
        envelope = {
            "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
        }
        state = {"backend": "sqlite"}
        responses = []

        def set_backend(value):
            state["backend"] = value
            return True

        unreconciled = {
            "reconciled": False, "event_count": 1, "outbox_pending": 0,
            "outbox_projected": 0, "receipt_count": 1, "unreceipted": 0,
            "adx_unprojected": 0, "local_only_events": 1,
            "target_backend": "kusto", "message": "confirmation required",
        }
        try:
            st.sqlite_mem, st.egress_mode, st.memory_backend = mem, "cloud", "sqlite"
            handler = object.__new__(BridgeHandler)
            handler.headers = {}
            handler._json_response = lambda status, data: responses.append((status, data))
            payload = {**envelope, "backend": "kusto", "confirm_unreconciled": False}
            with mock.patch.object(core, "_resolve_memory_backend", side_effect=lambda: state["backend"]), \
                    mock.patch.object(core, "_set_memory_backend", side_effect=set_backend), \
                    mock.patch.object(core, "reconciliation_status", return_value=unreconciled):
                handler._read_json_body = lambda: (dict(payload), "")
                handler._memory_backend_set()
                self.assertEqual(responses[-1][0], 409)
                payload["confirm_unreconciled"] = True
                handler._read_json_body = lambda: (dict(payload), "")
                handler._memory_backend_set()
                first_success = responses[-1]
                handler._memory_backend_set()
                replay_success = responses[-1]

            self.assertEqual(first_success[0], 200)
            self.assertEqual(replay_success[0], 200)
            self.assertTrue(replay_success[1]["idempotent"])
            self.assertEqual(
                {k: v for k, v in replay_success[1].items() if k != "idempotent"},
                first_success[1],
            )
            self.assertEqual(state["backend"], "kusto")
            self.assertEqual(
                mem.count(
                    "MemoryEvents", "EventType='memory.backend_switch_overridden'"
                ), 1,
            )
        finally:
            st.sqlite_mem, st.egress_mode, st.memory_backend = saved
            mem.close()

    def test_goal_and_skill_delete_replay_uses_header_identity(self):
        from bridge import state as st
        from bridge.core import BridgeHandler
        mem = _fresh_mem("delete_mutation_replay")
        saved = (st.memory_backend, st.sqlite_mem, st.bridge_bind_address)

        def envelope():
            return {
                "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
            }

        def headers(values):
            return {
                "X-Eva-Session-Id": values["session_id"],
                "X-Eva-Turn-Id": values["turn_id"],
                "X-Eva-Request-Id": values["request_id"],
                "X-Eva-Correlation-Id": values["correlation_id"],
            }

        try:
            st.memory_backend, st.sqlite_mem, st.bridge_bind_address = "sqlite", mem, "127.0.0.1"
            handler = object.__new__(BridgeHandler)
            responses = []
            handler._json_response = lambda status, data: responses.append((status, data))
            handler.headers = {}
            goal_create = {
                **envelope(), "title": "Delete Goal", "description": "",
                "category": "relational", "priority": 50, "relatedTopics": "",
            }
            handler._read_json_body = lambda: (dict(goal_create), "")
            handler._goals_create()
            goal_id = responses[-1][1]["goal"]["GoalId"]
            delete_goal_envelope = envelope()
            handler.headers = headers(delete_goal_envelope)
            handler._goals_delete(goal_id)
            first_goal = responses[-1]
            handler._goals_delete(goal_id)
            second_goal = responses[-1]
            self.assertEqual(first_goal[0], 200)
            self.assertFalse(first_goal[1]["idempotent"])
            self.assertTrue(second_goal[1]["idempotent"])
            self.assertEqual(first_goal[1]["goal"], second_goal[1]["goal"])
            self.assertEqual(mem.count("MemoryEvents", "EventType='goal.deleted'"), 1)
            handler.headers = {}
            handler._goals_delete(goal_id)
            self.assertEqual(responses[-1][0], 400)

            skill_create = {
                **envelope(), "name": "Delete Skill", "description": "",
                "instructions": "Delete only on request.", "tools": "", "tags": "",
            }
            handler._read_json_body = lambda: (dict(skill_create), "")
            handler._skills_create()
            skill_id = responses[-1][1]["skill"]["SkillId"]
            delete_skill_envelope = envelope()
            handler.headers = headers(delete_skill_envelope)
            handler._skills_delete(skill_id)
            first_skill = responses[-1]
            handler._skills_delete(skill_id)
            second_skill = responses[-1]
            self.assertEqual(first_skill[0], 200)
            self.assertFalse(first_skill[1]["idempotent"])
            self.assertTrue(second_skill[1]["idempotent"])
            self.assertEqual(first_skill[1]["skill"], second_skill[1]["skill"])
            self.assertEqual(mem.count("MemoryEvents", "EventType='skill.deleted'"), 1)
        finally:
            st.memory_backend, st.sqlite_mem, st.bridge_bind_address = saved
            mem.close()

    def test_kusto_goal_and_skill_projection_retries_until_receipted(self):
        from bridge import core
        from bridge import state as st
        from bridge.core import BridgeHandler
        from bridge.identity import RequestEnvelope
        mem = _fresh_mem("kusto_mutation_receipts")
        saved = (st.memory_backend, st.sqlite_mem, st.egress_mode)
        try:
            st.memory_backend, st.sqlite_mem, st.egress_mode = "kusto", mem, "cloud"
            handler = object.__new__(BridgeHandler)
            now = "2026-07-10T12:00:00.000000Z"
            goal_envelope = RequestEnvelope({
                "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
            })
            skill_envelope = RequestEnvelope({
                "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
            })
            goal_row = {
                "GoalId": "goal-kusto-retry", "Title": "Retry Goal", "Description": "",
                "Category": "relational", "Status": "active", "Priority": 50,
                "RelatedTopics": "", "CreatedAt": now, "UpdatedAt": now,
            }
            skill_row = {
                "SkillId": "skill-kusto-retry", "Name": "Retry Skill", "Description": "",
                "Instructions": "Retry safely.", "Tools": "", "Tags": "", "Source": "test",
                "Status": "active", "CreatedAt": now, "UpdatedAt": now,
            }
            goal_command = {"entity": "goal", "operation": "create", "fields": {"Title": "Retry Goal"}}
            skill_command = {"entity": "skill", "operation": "create", "fields": {"Name": "Retry Skill"}}
            with mock.patch.object(core, "_kusto_query_direct", return_value=[]), \
                    mock.patch.object(core, "_kusto_ingest_direct", side_effect=[False, True, False, True]) as ingest:
                goal_first = handler._write_goal_row(
                    "example-cluster", "example-db", goal_row, goal_envelope,
                    "goal.created", goal_command,
                )
                goal_replay = handler._write_goal_row(
                    "example-cluster", "example-db", goal_row, goal_envelope,
                    "goal.created", goal_command,
                )
                skill_first = handler._write_skill_row(
                    "example-cluster", "example-db", skill_row, skill_envelope,
                    "skill.created", skill_command,
                )
                skill_replay = handler._write_skill_row(
                    "example-cluster", "example-db", skill_row, skill_envelope,
                    "skill.created", skill_command,
                )
            self.assertFalse(goal_first[0])
            self.assertTrue(goal_replay[0])
            self.assertTrue(goal_replay[2])
            self.assertFalse(skill_first[0])
            self.assertTrue(skill_replay[0])
            self.assertTrue(skill_replay[2])
            self.assertEqual(ingest.call_count, 4)

            repo = mem.event_repository()
            goal_event = repo.get_by_idempotency_key(
                f"request:{goal_envelope.request_id}:goal.created"
            )
            skill_event = repo.get_by_idempotency_key(
                f"request:{skill_envelope.request_id}:skill.created"
            )
            for event, destination in (
                (goal_event, "kusto:Goals"), (skill_event, "kusto:Skills"),
            ):
                self.assertTrue(repo.has_projection_receipt(event["EventId"], destination))
                row = mem.query(
                    "SELECT Status,Attempts FROM MemoryOutbox "
                    "WHERE EventId=? AND Destination=?", (event["EventId"], destination),
                )[0]
                self.assertEqual(row["Status"], "projected")
                self.assertEqual(row["Attempts"], 2)
        finally:
            st.memory_backend, st.sqlite_mem, st.egress_mode = saved
            mem.close()

    def test_concurrent_kusto_goal_and_skill_delivery_is_single_claimed(self):
        from bridge import core
        from bridge import state as st
        from bridge.core import BridgeHandler
        from bridge.identity import RequestEnvelope

        for table in ("Goals", "Skills"):
            mem = _fresh_mem("kusto_concurrent_" + table.lower())
            saved = (st.memory_backend, st.sqlite_mem, st.egress_mode)
            try:
                st.memory_backend, st.sqlite_mem, st.egress_mode = "kusto", mem, "cloud"
                handler = object.__new__(BridgeHandler)
                envelope = RequestEnvelope({
                    "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
                    "request_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
                })
                now = "2026-07-10T12:00:00.000000Z"
                if table == "Goals":
                    row = {
                        "GoalId": "goal-concurrent", "Title": "Concurrent", "Description": "",
                        "Category": "relational", "Status": "active", "Priority": 50,
                        "RelatedTopics": "", "CreatedAt": now, "UpdatedAt": now,
                    }
                    command = {"entity": "goal", "operation": "create", "fields": {"Title": "Concurrent"}}
                    writer = lambda: handler._write_goal_row(
                        "example-cluster", "example-db", row, envelope,
                        "goal.created", command,
                    )
                    destination = "kusto:Goals"
                    event_type = "goal.created"
                else:
                    row = {
                        "SkillId": "skill-concurrent", "Name": "Concurrent", "Description": "",
                        "Instructions": "Act once.", "Tools": "", "Tags": "", "Source": "test",
                        "Status": "active", "CreatedAt": now, "UpdatedAt": now,
                    }
                    command = {"entity": "skill", "operation": "create", "fields": {"Name": "Concurrent"}}
                    writer = lambda: handler._write_skill_row(
                        "example-cluster", "example-db", row, envelope,
                        "skill.created", command,
                    )
                    destination = "kusto:Skills"
                    event_type = "skill.created"

                query_entered = threading.Event()
                release_query = threading.Event()

                def blocked_query(*_args, **_kwargs):
                    query_entered.set()
                    self.assertTrue(release_query.wait(timeout=5))
                    return []

                results = []
                with mock.patch.object(core, "_kusto_query_direct", side_effect=blocked_query), \
                        mock.patch.object(core, "_kusto_ingest_direct", return_value=True) as ingest:
                    first = threading.Thread(target=lambda: results.append(writer()))
                    first.start()
                    self.assertTrue(query_entered.wait(timeout=5))
                    second = threading.Thread(target=lambda: results.append(writer()))
                    second.start()
                    second.join(timeout=5)
                    self.assertFalse(second.is_alive())
                    release_query.set()
                    first.join(timeout=5)
                    self.assertFalse(first.is_alive())

                self.assertEqual(ingest.call_count, 1)
                self.assertEqual(sum(bool(result[0]) for result in results), 1)
                event = mem.event_repository().get_by_idempotency_key(
                    f"request:{envelope.request_id}:{event_type}"
                )
                self.assertTrue(
                    mem.event_repository().has_projection_receipt(
                        event["EventId"], destination
                    )
                )
            finally:
                st.memory_backend, st.sqlite_mem, st.egress_mode = saved
                mem.close()

    def test_generic_mcp_memory_writes_are_denied(self):
        from sqlite_mcp import SqliteMCPServer
        server = SqliteMCPServer()
        result = server._tool_ingest({
            "table": "Knowledge",
            "data": [{"Entity": "User", "Relation": "credential", "Value": "sk-" + "x" * 30}],
        })
        self.assertIn("disabled", result.lower())
        self.assertEqual(server._mem.query("SELECT * FROM Knowledge WHERE Relation='credential'"), [])
        server._mem.close()

    def test_frontend_envelope_fields_are_accepted_by_business_validators(self):
        from bridge.core import BridgeHandler
        handler = object.__new__(BridgeHandler)
        envelope = {
            "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
            "request_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
        }
        fields, error = handler._validate_goal_payload({
            **envelope, "title": "Goal", "category": "relational", "priority": 50,
            "description": "", "relatedTopics": "",
        }, creating=True)
        self.assertEqual(error, "")
        self.assertEqual(fields["Title"], "Goal")
        from bridge import state as st
        saved_bind, saved_enabled = st.bridge_bind_address, st.bg_loop_enabled
        responses = []
        try:
            st.bridge_bind_address = "127.0.0.1"
            st.bg_loop_enabled = False
            handler._read_json_body = lambda: ({**envelope, "enabled": False}, "")
            handler._json_response = lambda status, data: responses.append((status, data))
            handler._background_control()
            self.assertEqual(responses[-1][0], 200)
        finally:
            st.bridge_bind_address, st.bg_loop_enabled = saved_bind, saved_enabled

    def test_frontend_envelopes_are_distinct_and_browser_finalization_is_gated(self):
        sessions_path = os.path.join(PROJECT_ROOT, "core/js/sessions.js")
        script = r"""
const fs=require('fs'), vm=require('vm'), crypto=require('crypto').webcrypto;
global.crypto=crypto;
let active='11111111-1111-4111-8111-111111111111', fetches=[];
global._activeSessionId=()=>active;
global.isEvaStandalone=()=>false;
global.window={};
global.getACPBridgeUrl=()=> 'http://localhost:8888';
global.fetch=async (url,opts)=>{fetches.push(JSON.parse(opts.body));return {ok:true,json:async()=>({})}};
let source=fs.readFileSync(process.argv[1],'utf8');
source=source.slice(source.indexOf('var _envelopeState'));
vm.runInThisContext(source);
resetEnvelopeSession(active);
newEnvelopeTurn(); const first=captureRequestEnvelope();
newEnvelopeTurn(); const second=captureRequestEnvelope();
(async()=>{
 await finalizeDirectProviderTurn('u1','a1','test',first);
 const browserFetches=fetches.length;
 global.isEvaStandalone=()=>true;
 await finalizeDirectProviderTurn('u1','a1','test',first);
 console.log(JSON.stringify({first,second,browserFetches,body:fetches[0]}));
})().catch(e=>{console.error(e);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script, sessions_path], capture_output=True, text=True, check=True
        )
        data = json.loads(result.stdout)
        self.assertEqual(data["first"]["session_id"], data["second"]["session_id"])
        self.assertNotEqual(data["first"]["turn_id"], data["second"]["turn_id"])
        self.assertNotEqual(data["first"]["correlation_id"], data["second"]["correlation_id"])
        self.assertEqual(data["browserFetches"], 0)
        self.assertEqual(data["body"]["turn_id"], data["first"]["turn_id"])

    def test_frontend_mutation_envelopes_are_operation_scoped(self):
        sessions_path = os.path.join(PROJECT_ROOT, "core/js/sessions.js")
        options_path = os.path.join(PROJECT_ROOT, "core/js/options.js")
        script = r"""
const fs=require('fs'), vm=require('vm'), crypto=require('crypto').webcrypto;
global.crypto=crypto;
global._activeSessionId=()=> '11111111-1111-4111-8111-111111111111';
let sessions=fs.readFileSync(process.argv[1],'utf8');
sessions=sessions.slice(sessions.indexOf('var _envelopeState'));
vm.runInThisContext(sessions);
let options=fs.readFileSync(process.argv[2],'utf8');
options=options.slice(options.indexOf('function _withBridgeMutationEnvelope'),
    options.indexOf('async function backgroundBridgeRequest'));
vm.runInThisContext(options);
resetEnvelopeSession(_activeSessionId());
newEnvelopeTurn();
const chat=captureRequestEnvelope();
const mutation=captureMutationEnvelope();
const post=_withBridgeMutationEnvelope({
    method:'PATCH', body:JSON.stringify({status:'active'}),
    evaMutationEnvelope:mutation
});
const del=_withBridgeMutationEnvelope({
    method:'DELETE', evaMutationEnvelope:mutation
});
const chatAfter=captureRequestEnvelope();
console.log(JSON.stringify({chat,mutation,post:JSON.parse(post.body),headers:del.headers,chatAfter}));
"""
        result = subprocess.run(
            ["node", "-e", script, sessions_path, options_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertNotEqual(data["mutation"]["turn_id"], data["chat"]["turn_id"])
        self.assertEqual(data["chatAfter"]["turn_id"], data["chat"]["turn_id"])
        self.assertEqual(data["post"]["request_id"], data["mutation"]["request_id"])
        self.assertEqual(
            data["headers"]["X-Eva-Request-Id"], data["mutation"]["request_id"]
        )
        self.assertEqual(
            data["headers"]["X-Eva-Turn-Id"], data["mutation"]["turn_id"]
        )

    def test_memory_backend_switch_reuses_only_operation_envelope(self):
        sessions_path = os.path.join(PROJECT_ROOT, "core/js/sessions.js")
        copilot_path = os.path.join(PROJECT_ROOT, "core/js/copilot.js")
        script = r"""
const fs=require('fs'), vm=require('vm'), crypto=require('crypto').webcrypto;
global.crypto=crypto;
global._activeSessionId=()=> '11111111-1111-4111-8111-111111111111';
global.document={getElementById:()=>null};
const store={eva_memory_backend:'sqlite'};
global.localStorage={getItem:k=>store[k]||null,setItem:(k,v)=>{store[k]=v}};
global.getACPBridgeUrl=()=> 'http://localhost:8888';
global.AbortSignal={timeout:()=>({})};
global.window={confirm:()=>true};
let calls=[];
global.fetch=async (_url,opts)=>{
  calls.push(JSON.parse(opts.body));
  const first=calls.length===1;
  return {status:first?409:200,json:async()=>first
    ? {requires_confirmation:true,reconciliation:{message:'confirm'}}
    : {status:'ok',backend:'kusto'}};
};
let sessions=fs.readFileSync(process.argv[1],'utf8');
sessions=sessions.slice(sessions.indexOf('var _envelopeState'));
vm.runInThisContext(sessions);
let source=fs.readFileSync(process.argv[2],'utf8');
source=source.slice(source.indexOf('function _doSwitchMemoryBackend'));
vm.runInThisContext(source);
resetEnvelopeSession(_activeSessionId());
newEnvelopeTurn();
const chat=captureRequestEnvelope();
_doSwitchMemoryBackend('kusto');
setTimeout(()=>{
  const after=captureRequestEnvelope();
  console.log(JSON.stringify({chat,after,calls}));
},20);
"""
        result = subprocess.run(
            ["node", "-e", script, sessions_path, copilot_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertEqual(len(data["calls"]), 2)
        first, second = data["calls"]
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["turn_id"], second["turn_id"])
        self.assertEqual(first["correlation_id"], second["correlation_id"])
        self.assertNotEqual(first["turn_id"], data["chat"]["turn_id"])
        self.assertEqual(data["after"]["turn_id"], data["chat"]["turn_id"])
        self.assertEqual(
            data["after"]["correlation_id"], data["chat"]["correlation_id"]
        )

    def test_clear_memory_rotates_session_without_overwriting_snapshot(self):
        sessions_path = os.path.join(PROJECT_ROOT, "core/js/sessions.js")
        options_path = os.path.join(PROJECT_ROOT, "core/js/options.js")
        aig_path = os.path.join(PROJECT_ROOT, "core/js/aig.js")
        script = r"""
const fs=require('fs'), vm=require('vm'), crypto=require('crypto').webcrypto;
global.crypto=crypto;
const oldId='11111111-1111-4111-8111-111111111111';
const oldMarker='OLD_SESSION_MARKER_SHOULD_NOT_LEAK';
const store={
    eva_active_session:oldId,
    eva_sessions:JSON.stringify([{id:oldId,title:'Old',created:1,updated:1}]),
    messages:JSON.stringify([{role:'user',content:'old content'}])
};
global.localStorage={
    get length(){return Object.keys(store).length},
    key:i=>Object.keys(store)[i] || null,
    getItem:k=>Object.prototype.hasOwnProperty.call(store,k)?store[k]:null,
    setItem:(k,v)=>{store[k]=String(v)},
    removeItem:k=>{delete store[k]},
    clear:()=>{Object.keys(store).forEach(k=>delete store[k])}
};
const output={innerHTML:'',innerText:'',scrollTop:0,scrollHeight:0};
const input={innerHTML:'',focus:()=>{}};
global.document={getElementById:id=>{
    if(id==='txtOutput') return output;
    if(id==='txtMsg') return input;
    if(id==='selAIGBackend') return {value:'test-model'};
    if(id==='selModel') return {value:'aig'};
    return null;
}};
global.window={addEventListener:()=>{}};
global.setInterval=()=>0;
global.console=console;
global.alert=()=>{};
global.escapeHtml=s=>String(s);
global.setStatus=()=>{};
global.renderEvaResponse=async(content,out,envelope)=>{
    if(!isCurrentRequestEnvelope(envelope)) return false;
    out.innerText+=String(content);return true;
};
global.getSystemPrompt=()=>'';
global.getACPBridgeUrl=()=> 'http://localhost:8888';
global.getLmStudioBaseUrl=()=>'';
global.getLmStudioModel=()=>'';
global.getAuthKey=()=>'';
global.isEvaStandalone=()=>false;
global.dateContents='';
const snapshots={[oldId]:{marker:'durable-old'}};
global.idbSaveSession=async(id,data)=>{snapshots[id]=JSON.parse(JSON.stringify(data))};
global.idbLoadSession=async id=>snapshots[id] || null;
global.idbDeleteSession=async id=>{delete snapshots[id]};
global.idbMigrateFromLocalStorage=async()=>{};
let sessions=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(sessions);
let options=fs.readFileSync(process.argv[2],'utf8');
options=options.slice(options.indexOf('function resetTransientConversationState()'),
    options.indexOf('// Restore the Eva welcome'));
vm.runInThisContext(options);
let aig=fs.readFileSync(process.argv[3],'utf8');
vm.runInThisContext(aig);
resetEnvelopeSession(oldId);
const before=captureRequestEnvelope();
lastResponse=oldMarker;
masterOutput=oldMarker;
userMasterResponse=oldMarker;
aiMasterResponse=oldMarker;
storageAssistant=oldMarker;
retryCount=4;
window._evaSessionId=oldMarker;
window._evaTurnId=oldMarker;
localStorage.setItem('masterOutput',oldMarker);
let requests=[];
global.fetch=async(_url,opts)=>{
    requests.push(JSON.parse(opts.body));
    return {ok:true,json:async()=>({
        model:'test-model',choices:[{message:{content:'fresh response'}}]
    })};
};
(async()=>{
    clearMessages();
    const afterClear=captureRequestEnvelope();
    const clearedGlobals={
        lastResponse,masterOutput,userMasterResponse,aiMasterResponse,storageAssistant,
        retryCount,evaSessionId:window._evaSessionId,evaTurnId:window._evaTurnId
    };
    input.innerHTML='new prompt';
    await aigSend('test-model',afterClear);
    saveCurrentSession();
    await new Promise(resolve=>setTimeout(resolve,0));
    const index=JSON.parse(localStorage.getItem('eva_sessions'));
    console.log(JSON.stringify({
        before,afterClear,active:localStorage.getItem('eva_active_session'),
        index,snapshots,oldSnapshot:snapshots[oldId],requests,clearedGlobals,
        persistedMaster:localStorage.getItem('masterOutput')
    }));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script, sessions_path, options_path, aig_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        old_id = data["before"]["session_id"]
        new_id = data["afterClear"]["session_id"]
        self.assertNotEqual(new_id, old_id)
        self.assertEqual(data["active"], new_id)
        self.assertEqual(data["oldSnapshot"], {"marker": "durable-old"})
        self.assertIn(new_id, data["snapshots"])
        self.assertEqual([entry["id"] for entry in data["index"]].count(old_id), 1)
        self.assertEqual([entry["id"] for entry in data["index"]].count(new_id), 1)
        self.assertEqual(data["clearedGlobals"]["retryCount"], 0)
        for key in (
            "lastResponse", "masterOutput", "userMasterResponse",
            "aiMasterResponse", "storageAssistant", "evaSessionId", "evaTurnId",
        ):
            self.assertEqual(data["clearedGlobals"][key], "", key)
        self.assertEqual(len(data["requests"]), 1)
        request_json = json.dumps(data["requests"][0], sort_keys=True)
        new_snapshot_json = json.dumps(data["snapshots"][new_id], sort_keys=True)
        self.assertNotIn("OLD_SESSION_MARKER_SHOULD_NOT_LEAK", request_json)
        self.assertNotIn("OLD_SESSION_MARKER_SHOULD_NOT_LEAK", new_snapshot_json)
        self.assertNotIn("OLD_SESSION_MARKER_SHOULD_NOT_LEAK", data["persistedMaster"])

    def test_active_session_delete_cannot_resurrect_deleted_content(self):
        sessions_path = os.path.join(PROJECT_ROOT, "core/js/sessions.js")
        options_path = os.path.join(PROJECT_ROOT, "core/js/options.js")
        aig_path = os.path.join(PROJECT_ROOT, "core/js/aig.js")
        script = r"""
const fs=require('fs'), vm=require('vm'), crypto=require('crypto').webcrypto;
global.crypto=crypto;
const oldId='11111111-1111-4111-8111-111111111111';
const marker='DELETED_SESSION_MARKER_SHOULD_NOT_LEAK';
const store={
    eva_active_session:oldId,
    eva_sessions:JSON.stringify([{id:oldId,title:'Deleted',created:1,updated:1}]),
    messages:JSON.stringify([{role:'assistant',content:marker}]),
    copilotMessages:JSON.stringify([{role:'assistant',content:marker}]),
    copilotACPMessages:JSON.stringify([{role:'assistant',content:marker}]),
    geminiMessages:JSON.stringify([{role:'model',content:marker}]),
    openLLMessages:JSON.stringify([{role:'assistant',content:marker}]),
    aigMessages:JSON.stringify([{role:'assistant',content:marker}]),
    masterOutput:marker
};
global.localStorage={
    get length(){return Object.keys(store).length},
    key:i=>Object.keys(store)[i] || null,
    getItem:k=>Object.prototype.hasOwnProperty.call(store,k)?store[k]:null,
    setItem:(k,v)=>{store[k]=String(v)},
    removeItem:k=>{delete store[k]},
    clear:()=>{Object.keys(store).forEach(k=>delete store[k])}
};
const output={
    _html:marker,_text:marker,scrollTop:0,scrollHeight:0,
    get innerHTML(){return this._html},
    set innerHTML(value){this._html=String(value);this._text=String(value).replace(/<[^>]*>/g,'')},
    get innerText(){return this._text},
    set innerText(value){this._text=String(value)}
};
const input={innerHTML:'',focus:()=>{}};
global.document={getElementById:id=>{
    if(id==='txtOutput') return output;
    if(id==='txtMsg') return input;
    if(id==='selAIGBackend') return {value:'test-model'};
    if(id==='selModel') return {value:'aig'};
    return null;
}};
global.window={addEventListener:()=>{},_evaSessionId:marker,_evaTurnId:marker};
global.setInterval=()=>0;
global.console=console;
global.alert=()=>{};
global.escapeHtml=s=>String(s);
global.setStatus=()=>{};
global.renderEvaResponse=async(content,out,envelope)=>{
    if(!isCurrentRequestEnvelope(envelope)) return false;
    out.innerText+=String(content);return true;
};
global.getSystemPrompt=()=>'';
global.getACPBridgeUrl=()=> 'http://localhost:8888';
global.getLmStudioBaseUrl=()=>'';
global.getLmStudioModel=()=>'';
global.getAuthKey=()=>'';
global.isEvaStandalone=()=>false;
global.dateContents='';
const snapshots={[oldId]:{marker}};
global.idbSaveSession=async(id,data)=>{snapshots[id]=JSON.parse(JSON.stringify(data))};
global.idbLoadSession=async id=>snapshots[id] || null;
global.idbDeleteSession=async id=>{delete snapshots[id]};
global.idbMigrateFromLocalStorage=async()=>{};
let options=fs.readFileSync(process.argv[2],'utf8');
options=options.slice(options.indexOf('function resetTransientConversationState()'),
    options.indexOf('function clearMessages()'));
vm.runInThisContext(options);
let sessions=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(sessions);
let aig=fs.readFileSync(process.argv[3],'utf8');
vm.runInThisContext(aig);
resetEnvelopeSession(oldId);
lastResponse=marker;
masterOutput=marker;
userMasterResponse=marker;
aiMasterResponse=marker;
storageAssistant=marker;
retryCount=5;
let requests=[];
global.fetch=async(_url,opts)=>{
    requests.push(JSON.parse(opts.body));
    return {ok:true,json:async()=>({
        model:'test-model',choices:[{message:{content:'fresh response'}}]
    })};
};
(async()=>{
    deleteSession(oldId);
    const afterDeleteStorage=JSON.stringify(store);
    const afterDeleteDom=output.innerHTML+'|'+output.innerText;
    const newEnvelope=captureRequestEnvelope();
    input.innerHTML='new prompt';
    await aigSend('test-model',newEnvelope);
    saveCurrentSession();
    await new Promise(resolve=>setTimeout(resolve,0));
    console.log(JSON.stringify({
        oldId,newEnvelope,active:localStorage.getItem('eva_active_session'),
        index:JSON.parse(localStorage.getItem('eva_sessions')),snapshots,requests,
        afterDeleteStorage,afterDeleteDom,finalStorage:JSON.stringify(store),
        finalDom:output.innerHTML+'|'+output.innerText,
        globals:{lastResponse,masterOutput,userMasterResponse,aiMasterResponse,
            storageAssistant,retryCount,evaSessionId:window._evaSessionId,
            evaTurnId:window._evaTurnId}
    }));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script, sessions_path, options_path, aig_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        new_id = data["newEnvelope"]["session_id"]
        self.assertNotEqual(new_id, data["oldId"])
        self.assertEqual(data["active"], new_id)
        self.assertNotIn(data["oldId"], data["snapshots"])
        self.assertIn(new_id, data["snapshots"])
        index_ids = [entry["id"] for entry in data["index"]]
        self.assertNotIn(data["oldId"], index_ids)
        self.assertEqual(index_ids.count(new_id), 1)
        self.assertEqual(len(data["requests"]), 1)
        for content in (
            data["afterDeleteStorage"], data["afterDeleteDom"],
            json.dumps(data["requests"][0], sort_keys=True),
            json.dumps(data["snapshots"][new_id], sort_keys=True),
            data["finalStorage"], data["finalDom"],
        ):
            self.assertNotIn("DELETED_SESSION_MARKER_SHOULD_NOT_LEAK", content)

    def test_inflight_response_cannot_cross_deleted_session_generation(self):
        sessions_path = os.path.join(PROJECT_ROOT, "core/js/sessions.js")
        options_path = os.path.join(PROJECT_ROOT, "core/js/options.js")
        aig_path = os.path.join(PROJECT_ROOT, "core/js/aig.js")
        script = r"""
const fs=require('fs'), vm=require('vm'), crypto=require('crypto').webcrypto;
global.crypto=crypto;
const oldId='11111111-1111-4111-8111-111111111111';
const stale='STALE_INFLIGHT_RESPONSE_MUST_NOT_CROSS';
const store={
    eva_active_session:oldId,
    eva_sessions:JSON.stringify([{id:oldId,title:'Old',created:1,updated:1}]),
    aigMessages:JSON.stringify([{role:'system',content:'system'}])
};
global.localStorage={
    get length(){return Object.keys(store).length},
    key:i=>Object.keys(store)[i] || null,
    getItem:k=>Object.prototype.hasOwnProperty.call(store,k)?store[k]:null,
    setItem:(k,v)=>{store[k]=String(v)},
    removeItem:k=>{delete store[k]},
    clear:()=>{Object.keys(store).forEach(k=>delete store[k])}
};
const output={innerHTML:'',innerText:'',scrollTop:0,scrollHeight:0};
const input={innerHTML:'',focus:()=>{}};
global.document={getElementById:id=>{
    if(id==='txtOutput') return output;
    if(id==='txtMsg') return input;
    if(id==='selAIGBackend') return {value:'test-model'};
    if(id==='selModel') return {value:'aig'};
    return null;
}};
global.window={addEventListener:()=>{}};
global.setInterval=()=>0;
global.console=console;
global.alert=()=>{};
global.escapeHtml=s=>String(s);
global.setStatus=()=>{};
let renders=[];
global.renderEvaResponse=async(content,out,envelope)=>{
    if(!isCurrentRequestEnvelope(envelope)) return false;
    renders.push(content);out.innerText+=String(content);return true;
};
global.getSystemPrompt=()=>'';
global.getACPBridgeUrl=()=> 'http://localhost:8888';
global.getLmStudioBaseUrl=()=>'';
global.getLmStudioModel=()=>'';
global.getAuthKey=()=>'';
global.isEvaStandalone=()=>false;
global.dateContents='';
const snapshots={[oldId]:{marker:'old snapshot'}};
global.idbSaveSession=async(id,data)=>{snapshots[id]=JSON.parse(JSON.stringify(data))};
global.idbLoadSession=async id=>snapshots[id] || null;
global.idbDeleteSession=async id=>{delete snapshots[id]};
global.idbMigrateFromLocalStorage=async()=>{};
let options=fs.readFileSync(process.argv[2],'utf8');
options=options.slice(options.indexOf('function resetTransientConversationState()'),
    options.indexOf('function clearMessages()'));
vm.runInThisContext(options);
let sessions=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(sessions);
let aig=fs.readFileSync(process.argv[3],'utf8');
vm.runInThisContext(aig);
resetEnvelopeSession(oldId);
lastResponse='';
masterOutput='';
userMasterResponse='';
aiMasterResponse='';
storageAssistant='';
retryCount=0;
let requests=[];
let releaseOld;
global.fetch=async(_url,opts)=>{
    requests.push(JSON.parse(opts.body));
    if(requests.length===1){
        return new Promise(resolve=>{releaseOld=()=>resolve({
            ok:true,json:async()=>({model:'old-model',choices:[{message:{content:stale}}]})
        })});
    }
    return {ok:true,json:async()=>({
        model:'fresh-model',choices:[{message:{content:'fresh response'}}]
    })};
};
(async()=>{
    input.innerHTML='old prompt';
    newEnvelopeTurn();
    const oldEnvelope=captureRequestEnvelope();
    const oldSend=aigSend('test-model',oldEnvelope);
    if(typeof releaseOld!=='function') throw new Error('old request did not start');
    deleteSession(oldId);
    const newEnvelope=captureRequestEnvelope();
    releaseOld();
    await oldSend;
    const afterOld={
        lastResponse,masterOutput,dom:output.innerHTML+'|'+output.innerText,
        storage:JSON.stringify(store),renders:renders.slice()
    };
    input.innerHTML='fresh prompt';
    await aigSend('test-model',newEnvelope);
    saveCurrentSession();
    await new Promise(resolve=>setTimeout(resolve,0));
    console.log(JSON.stringify({
        oldId,oldEnvelope,newEnvelope,active:localStorage.getItem('eva_active_session'),
        requests,renders,afterOld,snapshots,
        index:JSON.parse(localStorage.getItem('eva_sessions')),
        finalState:JSON.stringify({store,dom:output.innerHTML+'|'+output.innerText,
            lastResponse,masterOutput})
    }));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script, sessions_path, options_path, aig_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        new_id = data["newEnvelope"]["session_id"]
        self.assertNotEqual(new_id, data["oldId"])
        self.assertEqual(data["active"], new_id)
        self.assertEqual(data["renders"], ["fresh response"])
        self.assertEqual(data["afterOld"]["lastResponse"], "")
        self.assertEqual(data["afterOld"]["masterOutput"], "")
        self.assertNotIn("STALE_INFLIGHT_RESPONSE_MUST_NOT_CROSS", json.dumps(data["afterOld"]))
        self.assertEqual(len(data["requests"]), 2)
        self.assertNotIn(
            "STALE_INFLIGHT_RESPONSE_MUST_NOT_CROSS",
            json.dumps(data["requests"][1], sort_keys=True),
        )
        self.assertNotIn(data["oldId"], data["snapshots"])
        self.assertIn(new_id, data["snapshots"])
        self.assertNotIn(
            "STALE_INFLIGHT_RESPONSE_MUST_NOT_CROSS",
            json.dumps(data["snapshots"][new_id], sort_keys=True),
        )
        self.assertNotIn("STALE_INFLIGHT_RESPONSE_MUST_NOT_CROSS", data["finalState"])
        index_ids = [entry["id"] for entry in data["index"]]
        self.assertNotIn(data["oldId"], index_ids)
        self.assertEqual(index_ids.count(new_id), 1)

    def test_all_provider_completions_use_generation_guards(self):
        expected = {
            "aig.js": ("requestIsCurrent", "renderEvaResponse(content, txtOutput, _envelope)"),
            "gpt-core.js": ("requestIsCurrent", "renderEvaResponse(s.content, txtOutput, capturedEnvelope)"),
            "gl-google.js": ("requestIsCurrent", "renderEvaResponse(mainResponse, out, capturedEnvelope)"),
            "lm-studio.js": ("requestIsCurrent", "renderEvaResponse(candidate, out, capturedEnvelope)"),
            "copilot.js": ("_copilotRequestIsCurrent", "renderEvaResponse(content, txtOutput, capturedEnvelope)"),
        }
        for filename, markers in expected.items():
            with open(os.path.join(PROJECT_ROOT, "core", "js", filename)) as handle:
                source = handle.read()
            self.assertIn("isCurrentRequestEnvelope", source, filename)
            for marker in markers:
                self.assertIn(marker, source, filename)
        with open(os.path.join(PROJECT_ROOT, "core", "js", "cognition.js")) as handle:
            cognition = handle.read()
        self.assertIn("requireCurrentEnvelope", cognition)
        self.assertIn("executeActions(text, capturedEnvelope)", cognition)

    def test_load_session_invalidates_old_generation_before_idb_resolves(self):
        sessions_path = os.path.join(PROJECT_ROOT, "core/js/sessions.js")
        script = r"""
const fs=require('fs'),vm=require('vm'),crypto=require('crypto').webcrypto;
global.crypto=crypto;
const oldId='11111111-1111-4111-8111-111111111111';
const targetOne='22222222-2222-4222-8222-222222222222';
const targetTwo='33333333-3333-4333-8333-333333333333';
const markerOne='ABANDONED_LOAD_MUST_NOT_RESTORE';
const markerTwo='DELETED_LOAD_MUST_NOT_RESTORE';
const store={
    eva_active_session:oldId,
    eva_sessions:JSON.stringify([
        {id:oldId,title:'Old'}, {id:targetOne,title:'One'}, {id:targetTwo,title:'Two'}
    ])
};
global.localStorage={
    getItem:k=>Object.prototype.hasOwnProperty.call(store,k)?store[k]:null,
    setItem:(k,v)=>{store[k]=String(v)},removeItem:k=>{delete store[k]}
};
global.document={getElementById:()=>null};
global.window={addEventListener:()=>{}};
global.setInterval=()=>0;
global.console=console;
let agentCancels=0;
global._resetAgentInteractionState=()=>{agentCancels+=1};
global.idbSaveSession=async()=>{};
const pending={};
global.idbLoadSession=id=>new Promise(resolve=>{pending[id]=resolve});
global.idbDeleteSession=async()=>{};
global.idbMigrateFromLocalStorage=async()=>{};
let source=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(source);
(async()=>{
    resetEnvelopeSession(oldId);
    newEnvelopeTurn();
    const oldEnvelope=captureRequestEnvelope();
    loadSession(targetOne);
    const cancelAtFirstLoad=agentCancels;
    const invalidImmediately=!isCurrentRequestEnvelope(oldEnvelope);
    newSession();
    const freshEnvelope=captureRequestEnvelope();
    pending[targetOne]({messages:JSON.stringify([{role:'user',content:markerOne}]),
        _messages:[{role:'user',text:markerOne}],_structuredSnapshot:true,_model:''});
    await new Promise(resolve=>setTimeout(resolve,0));
    const afterNew={active:localStorage.getItem('eva_active_session'),
        storage:JSON.stringify(store),freshCurrent:isCurrentRequestEnvelope(freshEnvelope)};

    loadSession(targetTwo);
    const cancelAtSecondLoad=agentCancels;
    deleteSession(targetTwo);
    pending[targetTwo]({messages:JSON.stringify([{role:'user',content:markerTwo}]),
        _messages:[{role:'user',text:markerTwo}],_structuredSnapshot:true,_model:''});
    await new Promise(resolve=>setTimeout(resolve,0));
    console.log(JSON.stringify({invalidImmediately,oldEnvelope,freshEnvelope,afterNew,
        active:localStorage.getItem('eva_active_session'),storage:JSON.stringify(store),
          index:JSON.parse(localStorage.getItem('eva_sessions')),
          cancelAtFirstLoad,cancelAtSecondLoad}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script, sessions_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertTrue(data["invalidImmediately"])
        self.assertNotEqual(data["oldEnvelope"]["session_id"], data["freshEnvelope"]["session_id"])
        self.assertTrue(data["afterNew"]["freshCurrent"])
        for serialized in (data["afterNew"]["storage"], data["storage"]):
            self.assertNotIn("ABANDONED_LOAD_MUST_NOT_RESTORE", serialized)
            self.assertNotIn("DELETED_LOAD_MUST_NOT_RESTORE", serialized)
        self.assertIsNone(data["active"])
        self.assertEqual(data["cancelAtFirstLoad"], 1)
        self.assertGreaterEqual(data["cancelAtSecondLoad"], 2)
        index_ids = [entry["id"] for entry in data["index"]]
        self.assertNotIn("33333333-3333-4333-8333-333333333333", index_ids)

    def test_agent_callbacks_cannot_cross_session_generation(self):
        sessions_path = os.path.join(PROJECT_ROOT, "core/js/sessions.js")
        options_path = os.path.join(PROJECT_ROOT, "core/js/options.js")
        markers_path = os.path.join(PROJECT_ROOT, "core/js/agent-markers.js")
        script = r"""
const fs=require('fs'),vm=require('vm'),crypto=require('crypto').webcrypto;
global.crypto=crypto;
const sid='11111111-1111-4111-8111-111111111111';
const store={eva_active_session:sid,eva_sessions:'[]'};
global.localStorage={
    getItem:k=>Object.prototype.hasOwnProperty.call(store,k)?store[k]:null,
    setItem:(k,v)=>{store[k]=String(v)},removeItem:k=>{delete store[k]}
};
let html='';
const output={
    get innerHTML(){return html},set innerHTML(v){html=String(v)},
    innerText:'',scrollTop:0,scrollHeight:0,querySelectorAll:()=>[]
};
global.document={getElementById:id=>id==='txtOutput'?output:null};
global.window={addEventListener:()=>{}};
global.setInterval=()=>0;
global.console=console;
global.escapeHtml=s=>String(s);
global.renderMarkdown=s=>String(s);
global.hideEvaWelcome=()=>{};
global.speakText=()=>{throw new Error('stale speech')};
global._lastUserAskedImage=false;
global._lastUserAskedGenerate=false;
global._lastUserImageSubject='';
let browserOpts;
global.EvaBrowser={launch:(_goal,opts)=>{browserOpts=opts},isActive:()=>false};
global.EvaDesktop={launch:()=>{throw new Error('unexpected desktop launch')},isActive:()=>false};
let sessions=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(sessions);
vm.runInThisContext(fs.readFileSync(process.argv[2],'utf8'));
let options=fs.readFileSync(process.argv[3],'utf8');
const feedback=options.slice(options.indexOf('function _agentEnvelopeCurrent'),
    options.indexOf('async function pollNotifications'));
vm.runInThisContext(feedback);
const reset=options.slice(options.indexOf('function resetTransientConversationState()'),
    options.indexOf('function clearMessages()'));
vm.runInThisContext(reset);
const renderer=options.slice(options.indexOf('async function renderEvaResponse'),
    options.indexOf('/**\n * Extract the key subject'));
vm.runInThisContext(renderer);
lastResponse='';masterOutput='';userMasterResponse='';aiMasterResponse='';
storageAssistant='';retryCount=0;
(async()=>{
    resetEnvelopeSession(sid);newEnvelopeTurn();
    const envelope=captureRequestEnvelope();
    const content='[[EVA_BROWSER]]{"goal":"old browser"}[[/EVA_BROWSER]]';
    const rendered=await renderEvaResponse(content,output,envelope);
    if(!rendered||!browserOpts) throw new Error('agent not launched');
    resetEnvelopeSession();
    resetTransientConversationState();
    output.innerHTML='';output.innerText='';
    const status={status:'done',goal:'old task',result:'STALE_AGENT_RESULT'};
    browserOpts.onProgress('STALE_AGENT_PROGRESS',status);
    browserOpts.onConfirm('STALE_AGENT_CONFIRM',false,status);
    browserOpts.onComplete(status,'/v1/browser','Browser Agent');
    await new Promise(resolve=>setTimeout(resolve,0));
    console.log(JSON.stringify({html:output.innerHTML,text:output.innerText,store,
        lastResponse,confirm:_agentConfirm,progress:_agentProgress}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script, sessions_path, markers_path, options_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        serialized = json.dumps(data, sort_keys=True)
        for marker in (
            "STALE_AGENT_RESULT", "STALE_AGENT_PROGRESS", "STALE_AGENT_CONFIRM",
            "STALE_DESKTOP_PROGRESS", "STALE_DESKTOP_CONFIRM",
        ):
            self.assertNotIn(marker, serialized)
        self.assertEqual(data["lastResponse"], "")
        self.assertFalse(data["confirm"]["pending"])
        self.assertEqual(data["progress"]["last"], 0)
        self.assertEqual(data["progress"]["lastText"], "")

    def test_deferred_cognition_action_rethrows_stale_generation(self):
        sessions_path = os.path.join(PROJECT_ROOT, "core/js/sessions.js")
        cognition_path = os.path.join(PROJECT_ROOT, "core/js/cognition.js")
        script = r"""
const fs=require('fs'),vm=require('vm'),crypto=require('crypto').webcrypto;
global.crypto=crypto;
const sid='11111111-1111-4111-8111-111111111111';
const store={eva_active_session:sid};
global.localStorage={getItem:k=>store[k]||null,setItem:(k,v)=>{store[k]=String(v)}};
global.document={getElementById:()=>null};
global.window={addEventListener:()=>{}};
global.setInterval=()=>0;
global.console=console;
let sessions=fs.readFileSync(process.argv[1],'utf8');
vm.runInThisContext(sessions);
let cognition=fs.readFileSync(process.argv[2],'utf8');
vm.runInThisContext(cognition);
const Cognition=window.Cognition;
let release;
Cognition.registerCapability({id:'test.deferred',description:'test',run:()=>
    new Promise(resolve=>{release=()=>resolve({html:'STALE_ACTION_RESULT'})})});
(async()=>{
    resetEnvelopeSession(sid);newEnvelopeTurn();
    const envelope=captureRequestEnvelope();
    const pending=Cognition.executeActions(
        '[[EVA_ACTION]]{"id":"test.deferred","args":{}}[[/EVA_ACTION]]',envelope
    ).then(value=>({resolved:true,value}),error=>({resolved:false,code:error.code,message:error.message}));
    resetEnvelopeSession();
    release();
    const result=await pending;
    console.log(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script, sessions_path, cognition_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertFalse(data["resolved"])
        self.assertEqual(data["code"], "EVA_STALE_ENVELOPE")
        self.assertNotIn("STALE_ACTION_RESULT", json.dumps(data))

    def test_update_button_uses_only_central_dispatch(self):
        with open(os.path.join(PROJECT_ROOT, "core/js/options.js")) as handle:
            source = handle.read()
        body = source[source.index("function updateButton()") : source.index("function sendData()")]
        self.assertIn("btnSend.onclick = sendData", body)
        for direct in ("aigSend(", "copilotSend(", "trboSend(", "geminiSend(", "lmsSend("):
            self.assertNotIn(direct, body)

    def test_disabled_mcp_write_tool_is_not_advertised(self):
        from sqlite_mcp import SqliteMCPServer
        from kusto_mcp import KustoMCPServer
        for tools in (SqliteMCPServer.TOOLS, KustoMCPServer.TOOLS):
            self.assertNotIn("kusto_ingest_inline", {tool["name"] for tool in tools})
        with open(os.path.join(TOOLS_DIR, "bridge", "cognition.py")) as handle:
            context_source = handle.read()
        self.assertNotIn("Persist it via kusto_ingest_inline", context_source)


class TestFrontendControllerRaces(unittest.TestCase):

    @staticmethod
    def _controller_dom_prelude():
        return r"""
const elements={};
class Element {
    constructor(tag){
        this.tagName=tag;this.children=[];this.style={};this.attributes={};
        this.classList={add:()=>{},remove:()=>{}};this.hidden=false;this.src='';
        this.textContent='';this.parentNode=null;this._id='';this._html='';
    }
    set id(value){this._id=String(value);elements[this._id]=this}
    get id(){return this._id}
    set innerHTML(value){
        this._html=String(value);
        const re=/id="([^"]+)"/g;let m;
        while((m=re.exec(this._html))!==null){if(!elements[m[1]]){const e=new Element('div');e.id=m[1];e.parentNode=this}}
    }
    get innerHTML(){return this._html}
    appendChild(child){child.parentNode=this;this.children.push(child);return child}
    remove(){if(this._id)delete elements[this._id]}
    addEventListener(){}
    setAttribute(k,v){this.attributes[k]=String(v)}
    removeAttribute(k){delete this.attributes[k];if(k==='src')this.src=''}
    getBoundingClientRect(){return {left:0,top:0}}
    querySelector(){return null}
}
const body=new Element('body');body.style={};
global.document={
    body,createElement:tag=>new Element(tag),getElementById:id=>elements[id]||null,
    querySelector:selector=>selector==='#evaBrowserPopup .ebp-title'?(elements.ebpTitlebar||null):null,
    addEventListener:()=>{}
};
global.window={innerWidth:1200,innerHeight:800,
    evaStandalone:{authorizeAgentLaunch:async(agent,specification)=>({
        authorized:true,capability:'synthetic.capability',specification
    })}};
global.setInterval=()=>1;global.clearInterval=()=>{};
global.setTimeout=setTimeout;global.clearTimeout=clearTimeout;
global.AbortSignal={timeout:()=>({})};
global.console=console;
global.getSafeBridgeBaseUrl=()=> 'http://localhost:8888';
global.getAuthKey=()=> 'synthetic-key';
global.setStatus=()=>{};
global.FileReader=class {
    readAsDataURL(blob){this.result=blob.data;Promise.resolve().then(()=>this.onload&&this.onload())}
};
"""

    def test_late_startup_restore_cannot_overwrite_live_turn(self):
        sessions_path = os.path.join(PROJECT_ROOT, "core/js/sessions.js")
        script = r"""
const fs=require('fs'),vm=require('vm'),crypto=require('crypto').webcrypto;
global.crypto=crypto;
const sid='11111111-1111-4111-8111-111111111111';
const stale='STALE_STARTUP_SNAPSHOT';
const live='LIVE_TURN_STARTED';
const store={eva_active_session:sid,eva_sessions:JSON.stringify([{id:sid,title:'Active'}])};
global.localStorage={
    getItem:k=>Object.prototype.hasOwnProperty.call(store,k)?store[k]:null,
    setItem:(k,v)=>{store[k]=String(v)},removeItem:k=>{delete store[k]}
};
const output={innerHTML:'',textContent:'',scrollTop:0,scrollHeight:0,appendChild:()=>{}};
global.document={getElementById:id=>id==='txtOutput'?output:null,createElement:()=>({
    appendChild:()=>{},style:{},className:'',textContent:''
})};
global.window={addEventListener:()=>{}};global.setInterval=()=>0;global.console=console;
global.resetTransientConversationState=()=>{};
let resolveMigration,resolveSnapshot;
global.idbMigrateFromLocalStorage=()=>new Promise(resolve=>{resolveMigration=resolve});
global.idbLoadSession=()=>new Promise(resolve=>{resolveSnapshot=resolve});
global.idbSaveSession=async()=>{};global.idbDeleteSession=async()=>{};
let source=fs.readFileSync(process.argv[1],'utf8');vm.runInThisContext(source);
(async()=>{
    initSessions();
    resolveMigration();
    await new Promise(resolve=>setTimeout(resolve,0));
    if(typeof resolveSnapshot!=='function')throw new Error('startup snapshot read did not begin');
    newEnvelopeTurn();const liveEnvelope=captureRequestEnvelope();
    localStorage.setItem('messages',JSON.stringify([{role:'user',content:live}]));
    output.innerHTML=live;output.textContent=live;
    resolveSnapshot({messages:JSON.stringify([{role:'user',content:stale}]),
        _messages:[{role:'user',text:stale}],_structuredSnapshot:true,_model:''});
    await new Promise(resolve=>setTimeout(resolve,0));
    console.log(JSON.stringify({storage:JSON.stringify(store),html:output.innerHTML,
        text:output.textContent,envelopeCurrent:isCurrentRequestEnvelope(liveEnvelope)}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script, sessions_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        serialized = json.dumps(data, sort_keys=True)
        self.assertNotIn("STALE_STARTUP_SNAPSHOT", serialized)
        self.assertIn("LIVE_TURN_STARTED", serialized)
        self.assertTrue(data["envelopeCurrent"])

    def test_pending_and_replaced_agent_launches_cancel_remote_runs(self):
        browser_path = os.path.join(PROJECT_ROOT, "core/js/browser-agent.js")
        script = self._controller_dom_prelude() + r"""
const fs=require('fs'),vm=require('vm');
let runResolvers=[],runNumber=0,cancelIds=[],cancelUrls=[];
let activeBase='http://bridge-one';
global.getSafeBridgeBaseUrl=()=>activeBase;
global.fetch=async(url,opts)=>{
    if(url.endsWith('/run')){
        runNumber+=1;const number=runNumber;
        return new Promise(resolve=>runResolvers[number]=resolve);
    }
    if(url.endsWith('/cancel')){
        cancelIds.push(JSON.parse(opts.body).run_id);cancelUrls.push(url);
        return {ok:true,json:async()=>({})}
    }
    if(url.includes('/status?')){
        const id=new URL(url).searchParams.get('run_id');
        return {ok:true,json:async()=>({id,status:cancelIds.includes(id)?'cancelled':'running',goal:id})};
    }
    return {ok:true,json:async()=>({})};
};
let source=fs.readFileSync(process.argv[1],'utf8');vm.runInThisContext(source);
const EvaBrowser=window.EvaBrowser,EvaDesktop=window.EvaDesktop;
async function waitResolver(number){
    for(let i=0;i<100&&typeof runResolvers[number]!=='function';i++){
        await new Promise(resolve=>setTimeout(resolve,0));
    }
    if(typeof runResolvers[number]!=='function')throw new Error('run resolver not reached '+number);
}
(async()=>{
    const first=EvaBrowser.launch('pending one',{});
    await waitResolver(1);
    activeBase='http://bridge-two';
    EvaBrowser.cancel();
    runResolvers[1]({ok:true,json:async()=>({id:'late-pending',status:'starting',goal:'pending one'})});
    await first;await new Promise(resolve=>setTimeout(resolve,0));

    activeBase='http://known-origin';
    const second=EvaBrowser.launch('replacement old',{});
    await waitResolver(2);
    runResolvers[2]({ok:true,json:async()=>({id:'known-old',status:'starting',goal:'replacement old'})});
    await second;
    activeBase='http://replacement-origin';
    const third=EvaDesktop.launch('replacement new',{});
    await waitResolver(3);
    runResolvers[3]({ok:true,json:async()=>({id:'known-new',status:'starting',goal:'replacement new'})});
    await third;await new Promise(resolve=>setTimeout(resolve,0));
    console.log(JSON.stringify({cancelIds,cancelUrls,active:EvaBrowser.isActive()}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script, browser_path],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn("late-pending", data["cancelIds"])
        self.assertIn("known-old", data["cancelIds"])
        self.assertNotIn("known-new", data["cancelIds"])
        cancel_by_id = dict(zip(data["cancelIds"], data["cancelUrls"]))
        self.assertTrue(cancel_by_id["late-pending"].startswith("http://bridge-one/"))
        self.assertTrue(cancel_by_id["known-old"].startswith("http://known-origin/"))
        self.assertTrue(data["active"])

    def test_stale_screenshot_cannot_overwrite_replacement_run(self):
        browser_path = os.path.join(PROJECT_ROOT, "core/js/browser-agent.js")
        script = self._controller_dom_prelude() + r"""
const fs=require('fs'),vm=require('vm');
let oldShotResolve,cancelIds=[];
global.fetch=async(url,opts)=>{
    if(url.endsWith('/browser/run'))return {ok:true,json:async()=>({id:'old-run',status:'starting',goal:'old'})};
    if(url.endsWith('/desktop/run'))return {ok:true,json:async()=>({id:'new-run',status:'starting',goal:'new'})};
    if(url.includes('/status?run_id=old-run'))return {ok:true,json:async()=>({id:'old-run',status:cancelIds.includes('old-run')?'cancelled':'running',step:1,goal:'old'})};
    if(url.includes('/status?run_id=new-run'))return {ok:true,json:async()=>({id:'new-run',status:'running',step:1,goal:'new'})};
    if(url.includes('/browser/screenshot'))return new Promise(resolve=>{oldShotResolve=()=>resolve({ok:true,blob:async()=>({data:'OLD_SCREENSHOT'})})});
    if(url.includes('/desktop/screenshot'))return {ok:true,blob:async()=>({data:'NEW_SCREENSHOT'})};
    if(url.endsWith('/cancel')){cancelIds.push(JSON.parse(opts.body).run_id);return {ok:true,json:async()=>({})}}
    return {ok:true,json:async()=>({})};
};
let source=fs.readFileSync(process.argv[1],'utf8');vm.runInThisContext(source);
const EvaBrowser=window.EvaBrowser,EvaDesktop=window.EvaDesktop;
(async()=>{
    await EvaBrowser.launch('old',{});await new Promise(resolve=>setTimeout(resolve,0));
    await EvaDesktop.launch('new',{});await new Promise(resolve=>setTimeout(resolve,0));
    const img=document.getElementById('ebpShot');
    const beforeOld=img&&img.src;
    oldShotResolve();await new Promise(resolve=>setTimeout(resolve,0));
    console.log(JSON.stringify({beforeOld,afterOld:img&&img.src,cancelIds}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script, browser_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertEqual(data["beforeOld"], "NEW_SCREENSHOT")
        self.assertEqual(data["afterOld"], "NEW_SCREENSHOT")
        self.assertIn("old-run", data["cancelIds"])


# ═══════════════════════════════════════════════════════════════════
#  B. Truly Immutable Event Store
# ═══════════════════════════════════════════════════════════════════
class TestImmutableEventStore(unittest.TestCase):

    def setUp(self):
        self.mem = _fresh_mem("immutable")
        self.repo = self.mem.event_repository()

    def tearDown(self):
        self.mem.close()

    def test_update_trigger_blocks_update(self):
        """UPDATE on MemoryEvents is blocked by BEFORE UPDATE trigger."""
        ev = self.repo.append_event(
            stream_id="imm1", event_type="test.created", payload={"k": "v"},
        )
        conn = self.mem._conn()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            conn.execute(
                "UPDATE MemoryEvents SET EventType = 'hacked' WHERE EventId = ?",
                (ev["EventId"],),
            )
        self.assertIn("immutable", str(ctx.exception).lower())

    def test_delete_trigger_blocks_delete(self):
        """DELETE on MemoryEvents is blocked by BEFORE DELETE trigger."""
        ev = self.repo.append_event(
            stream_id="imm2", event_type="test.del", payload={},
        )
        conn = self.mem._conn()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            conn.execute("DELETE FROM MemoryEvents WHERE EventId = ?", (ev["EventId"],))
        self.assertIn("immutable", str(ctx.exception).lower())

    def test_query_cannot_write_to_events(self):
        """query() rejects UPDATE/DELETE/INSERT on journal tables."""
        from bridge.events import ReadOnlyViolationError
        # query() returns empty list (backward compat) on write attempt
        result = self.mem.query("UPDATE MemoryEvents SET EventType='x'")
        self.assertEqual(result, [])
        result = self.mem.query("DELETE FROM MemoryEvents")
        self.assertEqual(result, [])
        result = self.mem.query("INSERT INTO MemoryEvents (EventId) VALUES ('x')")
        self.assertEqual(result, [])

    def test_query_strict_raises_on_write(self):
        """query_strict() raises MemoryQueryError on write statement."""
        from bridge.events import MemoryQueryError
        with self.assertRaises(MemoryQueryError):
            self.mem.query_strict("UPDATE MemoryEvents SET EventType='x'")
        with self.assertRaises(MemoryQueryError):
            self.mem.query_strict("DELETE FROM MemoryEvents WHERE 1=1")

    def test_no_projection_status_mutation(self):
        """Events never have ProjectionStatus/ProjectionError updated."""
        ev = self.repo.append_event(
            stream_id="nostat", event_type="test.status", payload={},
        )
        # Verify no ProjectionStatus column exists (removed from schema)
        conn = self.mem._conn()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(MemoryEvents)").fetchall()]
        self.assertNotIn("ProjectionStatus", cols)
        self.assertNotIn("ProjectionError", cols)


# ═══════════════════════════════════════════════════════════════════
#  C. Atomic Transactions + Concurrency
# ═══════════════════════════════════════════════════════════════════
class TestAtomicTransactions(unittest.TestCase):

    def setUp(self):
        self.mem = _fresh_mem("atomic")
        self.repo = self.mem.event_repository()

    def tearDown(self):
        self.mem.close()

    def test_append_and_outbox_atomic(self):
        """Event and outbox entry created in same transaction."""
        ev = self.repo.append_event(
            stream_id="atom1", event_type="test.atom", payload={"x": 1},
            consent_scope="cloud_allowed",
        )
        conn = self.mem._conn()
        outbox = conn.execute(
            "SELECT * FROM MemoryOutbox WHERE EventId = ?", (ev["EventId"],)
        ).fetchone()
        self.assertIsNotNone(outbox)
        self.assertEqual(outbox["Status"], "pending")

    def test_no_outbox_for_local_only(self):
        """No outbox entry created for local_only consent."""
        ev = self.repo.append_event(
            stream_id="local1", event_type="test.local", payload={},
            consent_scope="local_only",
        )
        conn = self.mem._conn()
        outbox = conn.execute(
            "SELECT * FROM MemoryOutbox WHERE EventId = ?", (ev["EventId"],)
        ).fetchone()
        self.assertIsNone(outbox)

    def test_no_outbox_for_secret(self):
        """No outbox entry for secret sensitivity."""
        ev = self.repo.append_event(
            stream_id="secret1", event_type="test.secret", payload={},
            consent_scope="cloud_allowed", sensitivity="secret",
        )
        conn = self.mem._conn()
        outbox = conn.execute(
            "SELECT * FROM MemoryOutbox WHERE EventId = ?", (ev["EventId"],)
        ).fetchone()
        self.assertIsNone(outbox)

    def test_memory_backed_mutations_use_transaction_before_repository(self):
        """Appends and both outbox claim paths use memory→repository order."""
        exact_event = self.repo.append_event(
            stream_id="lock-order:exact-outbox",
            event_type="test.lock_order",
            payload={"path": "exact-outbox"},
            consent_scope="cloud_allowed",
            idempotency_key="lock-order-exact-outbox",
            outbox_destination="lock-order",
        )
        self.repo.append_event(
            stream_id="lock-order:batch-outbox",
            event_type="test.lock_order",
            payload={"path": "batch-outbox"},
            consent_scope="cloud_allowed",
            idempotency_key="lock-order-batch-outbox",
            outbox_destination="lock-order",
        )
        observed = []
        original_transaction = self.repo._transaction
        original_lock = self.repo._lock

        class TrackingLock:
            def __enter__(inner_self):
                observed.append("repository")
                original_lock.acquire()
                return inner_self

            def __exit__(inner_self, exc_type, exc, traceback):
                original_lock.release()

        @contextlib.contextmanager
        def tracking_transaction(connection=None):
            with original_transaction(connection) as conn:
                observed.append("transaction")
                yield conn

        with mock.patch.object(self.repo, "_transaction", tracking_transaction), \
                mock.patch.object(self.repo, "_lock", TrackingLock()):
            self.repo.append_event(
                stream_id="lock-order:ordinary",
                event_type="test.lock_order",
                payload={"path": "ordinary"},
                idempotency_key="lock-order-ordinary",
            )
            self.assertEqual(observed[:2], ["transaction", "repository"])

            observed.clear()
            with self.mem.transaction() as conn:
                self.repo.append_event(
                    connection=conn,
                    stream_id="lock-order:caller",
                    event_type="test.lock_order",
                    payload={"path": "caller"},
                    idempotency_key="lock-order-caller",
                )
            self.assertEqual(observed[:2], ["transaction", "repository"])

            observed.clear()
            claimed = self.repo.claim_outbox_entry(
                exact_event["EventId"], "lock-order"
            )
            self.assertIsNotNone(claimed)
            self.assertEqual(observed[:2], ["transaction", "repository"])

            observed.clear()
            claimed_batch = self.repo.claim_outbox(
                limit=10, destination="lock-order"
            )
            self.assertEqual(len(claimed_batch), 1)
            self.assertEqual(observed[:2], ["transaction", "repository"])

    def test_factory_shared_connection_serializes_transaction_entry(self):
        """Concurrent factory appends never persist an event while reporting failure."""
        from bridge.event_store import EventRepositoryV2
        from bridge.migrations import run_migrations

        path = os.path.join(_TMP_HOME, f"factory_{uuid.uuid4().hex[:8]}.db")
        conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        run_migrations(conn)
        repo = EventRepositoryV2(lambda: conn, installation_id="factory-shared")
        barrier = threading.Barrier(8)
        results = []
        errors = []
        result_lock = threading.Lock()

        def worker(index):
            try:
                barrier.wait(timeout=5)
                event = repo.append_event(
                    stream_id=f"factory:{index}",
                    event_type="test.factory_shared",
                    payload={"index": index},
                    idempotency_key=f"factory-shared-{index}",
                )
                with result_lock:
                    results.append(event["EventId"])
            except Exception as exc:
                with result_lock:
                    errors.append(type(exc).__name__)

        threads = [
            threading.Thread(target=worker, args=(index,), daemon=True)
            for index in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        try:
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 8)
            durable = conn.execute(
                "SELECT EventId FROM MemoryEvents "
                "WHERE EventType='test.factory_shared' ORDER BY EventId"
            ).fetchall()
            self.assertEqual(
                [row[0] for row in durable], sorted(results)
            )
        finally:
            repo.close()
            conn.close()

    def test_memory_append_cannot_deadlock_with_outbox_claims(self):
        """Forced former ABBA interleavings complete for exact and batch claims."""
        for claim_kind in ("exact", "batch"):
            with self.subTest(claim_kind=claim_kind):
                mem = _fresh_mem(f"outbox_order_{claim_kind}")
                repo = mem.event_repository()
                destination = f"lock-{claim_kind}"
                claimed_event = repo.append_event(
                    stream_id=f"outbox-order:{claim_kind}:claimed",
                    event_type="test.outbox_order",
                    payload={"claim_kind": claim_kind},
                    consent_scope="cloud_allowed",
                    outbox_destination=destination,
                    idempotency_key=f"outbox-order-{claim_kind}-claimed",
                )
                original_transaction = repo._transaction
                append_holds_memory = threading.Event()
                claim_attempted_memory = threading.Event()
                release_append = threading.Event()
                results = []
                errors = []
                result_lock = threading.Lock()

                @contextlib.contextmanager
                def coordinated_transaction(connection=None):
                    name = threading.current_thread().name
                    if name == "claim-thread":
                        claim_attempted_memory.set()
                    with original_transaction(connection) as conn:
                        if name == "append-thread":
                            append_holds_memory.set()
                            if not release_append.wait(timeout=5):
                                raise RuntimeError("append release timeout")
                        yield conn

                def append_worker():
                    try:
                        event = repo.append_event(
                            stream_id=f"outbox-order:{claim_kind}:append",
                            event_type="test.outbox_order",
                            payload={"path": "append"},
                            idempotency_key=f"outbox-order-{claim_kind}-append",
                        )
                        with result_lock:
                            results.append(("append", event["EventId"]))
                    except Exception as exc:
                        with result_lock:
                            errors.append(("append", type(exc).__name__))

                def claim_worker():
                    try:
                        if claim_kind == "exact":
                            value = repo.claim_outbox_entry(
                                claimed_event["EventId"], destination
                            )
                            count = int(value is not None)
                        else:
                            count = len(repo.claim_outbox(
                                limit=10, destination=destination
                            ))
                        with result_lock:
                            results.append(("claim", count))
                    except Exception as exc:
                        with result_lock:
                            errors.append(("claim", type(exc).__name__))

                with mock.patch.object(repo, "_transaction", coordinated_transaction):
                    append_thread = threading.Thread(
                        target=append_worker, name="append-thread", daemon=True
                    )
                    append_thread.start()
                    self.assertTrue(append_holds_memory.wait(timeout=5))
                    claim_thread = threading.Thread(
                        target=claim_worker, name="claim-thread", daemon=True
                    )
                    claim_thread.start()
                    self.assertTrue(claim_attempted_memory.wait(timeout=5))
                    release_append.set()
                    append_thread.join(timeout=10)
                    claim_thread.join(timeout=10)
                try:
                    self.assertFalse(append_thread.is_alive())
                    self.assertFalse(claim_thread.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual({kind for kind, _ in results}, {"append", "claim"})
                    self.assertEqual(dict(results)["claim"], 1)
                finally:
                    release_append.set()
                    mem.close()

    def test_factory_idempotency_fallback_never_sees_uncommitted_duplicate(self):
        """A rolled-back competing row cannot produce a false successful return."""
        from bridge.event_store import EventRepositoryV2
        from bridge.events import EventStoreError
        from bridge.migrations import run_migrations

        path = os.path.join(_TMP_HOME, f"fallback_{uuid.uuid4().hex[:8]}.db")
        conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        run_migrations(conn)
        repo = EventRepositoryV2(lambda: conn, installation_id="fallback-shared")
        original_serialized = repo._serialized_transaction
        original_append = repo._append_in_transaction
        first_exited = threading.Event()
        second_inserted = threading.Event()
        allow_second_rollback = threading.Event()
        call_counts = {}
        counts_lock = threading.Lock()
        results = []
        errors = []

        @contextlib.contextmanager
        def coordinated_serialized(connection=None):
            name = threading.current_thread().name
            with counts_lock:
                call_counts[name] = call_counts.get(name, 0) + 1
                count = call_counts[name]
            if name == "fallback-first" and count == 2:
                allow_second_rollback.set()
            try:
                with original_serialized(connection) as active:
                    yield active
            finally:
                if name == "fallback-first" and count == 1:
                    first_exited.set()
                    if not second_inserted.wait(timeout=5):
                        allow_second_rollback.set()

        def coordinated_append(active, **values):
            name = threading.current_thread().name
            if name == "fallback-first":
                raise EventStoreError("injected first failure")
            value = original_append(active, **values)
            second_inserted.set()
            allow_second_rollback.wait(timeout=5)
            raise EventStoreError("injected competing rollback")

        def worker(name):
            try:
                if name == "fallback-second":
                    first_exited.wait(timeout=5)
                value = repo.append_event(
                    stream_id="fallback:shared",
                    event_type="test.fallback",
                    payload={"same": True},
                    idempotency_key="fallback-shared-key",
                )
                results.append(value["EventId"])
            except Exception as exc:
                errors.append(type(exc).__name__)

        with mock.patch.object(repo, "_serialized_transaction", coordinated_serialized), \
                mock.patch.object(repo, "_append_in_transaction", coordinated_append):
            threads = [
                threading.Thread(target=worker, args=("fallback-first",),
                                 name="fallback-first", daemon=True),
                threading.Thread(target=worker, args=("fallback-second",),
                                 name="fallback-second", daemon=True),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        try:
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(results, [])
            self.assertEqual(len(errors), 2)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM MemoryEvents "
                    "WHERE IdempotencyKey='fallback-shared-key'"
                ).fetchone()[0],
                0,
            )
        finally:
            allow_second_rollback.set()
            repo.close()
            conn.close()

    def test_factory_legacy_receipt_cannot_commit_inflight_append(self):
        """Receipt serialization cannot cross-commit an append transaction."""
        from bridge.event_store import EventRepositoryV2
        from bridge.events import deterministic_event_id
        from bridge.migrations import run_migrations

        path = os.path.join(_TMP_HOME, f"receipt_{uuid.uuid4().hex[:8]}.db")
        conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        run_migrations(conn)
        installation = "receipt-shared"
        key = "receipt-inflight-key"
        expected_event_id = deterministic_event_id(installation, key)
        repo = EventRepositoryV2(lambda: conn, installation_id=installation)
        original_append = repo._append_in_transaction
        original_lock = repo._factory_connection_lock(conn)
        append_inserted = threading.Event()
        receipt_lock_attempt = threading.Event()
        release_append = threading.Event()
        results = []
        errors = []

        class TrackingLock:
            def __enter__(inner_self):
                if threading.current_thread().name == "receipt-thread":
                    receipt_lock_attempt.set()
                original_lock.acquire()
                return inner_self

            def __exit__(inner_self, exc_type, exc, traceback):
                original_lock.release()

        def paused_append(active, **values):
            result = original_append(active, **values)
            append_inserted.set()
            if not release_append.wait(timeout=5):
                raise RuntimeError("append release timeout")
            return result

        def append_worker():
            try:
                event = repo.append_event(
                    stream_id="receipt:inflight", event_type="test.receipt",
                    payload={"value": 1}, idempotency_key=key,
                )
                results.append(("append", event["EventId"]))
            except Exception as exc:
                errors.append(("append", type(exc).__name__))

        def receipt_worker():
            try:
                repo.record_legacy_receipt(
                    expected_event_id, "test-receipt", row_count=1
                )
                results.append(("receipt", expected_event_id))
            except Exception as exc:
                errors.append(("receipt", type(exc).__name__))

        with mock.patch.object(repo, "_append_in_transaction", paused_append), \
            mock.patch.object(
                repo, "_factory_connection_lock",
                return_value=TrackingLock(),
            ):
            append_thread = threading.Thread(
                target=append_worker, name="append-thread", daemon=True
            )
            append_thread.start()
            self.assertTrue(append_inserted.wait(timeout=5))
            receipt_thread = threading.Thread(
                target=receipt_worker, name="receipt-thread", daemon=True
            )
            receipt_thread.start()
            attempted = receipt_lock_attempt.wait(timeout=5)
            release_append.set()
            append_thread.join(timeout=10)
            receipt_thread.join(timeout=10)
        try:
            self.assertTrue(attempted)
            self.assertFalse(append_thread.is_alive())
            self.assertFalse(receipt_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual({kind for kind, _ in results}, {"append", "receipt"})
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM MemoryEvents WHERE EventId=?",
                    (expected_event_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM LegacyProjectionReceipts "
                    "WHERE EventId=? AND ProjectionName='test-receipt'",
                    (expected_event_id,),
                ).fetchone()[0],
                1,
            )
        finally:
            release_append.set()
            repo.close()
            conn.close()

    def test_factory_implicit_operations_reject_unknown_caller_transaction(self):
        """Implicit factory operations cannot join or observe an external transaction."""
        from bridge.event_store import EventRepositoryV2
        from bridge.events import EventStoreError
        from bridge.migrations import run_migrations

        path = os.path.join(_TMP_HOME, f"external_{uuid.uuid4().hex[:8]}.db")
        conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        run_migrations(conn)
        repo = EventRepositoryV2(lambda: conn, installation_id="external-shared")
        conn.execute("BEGIN IMMEDIATE")
        event = repo.append_event(
            connection=conn,
            stream_id="external:caller",
            event_type="test.external_transaction",
            payload={"value": 1},
            turn_id="external-turn",
            consent_scope="cloud_allowed",
            idempotency_key="external-caller-event",
        )
        repo.record_legacy_receipt(
            event["EventId"], "external", 1, connection=conn
        )
        conn.execute(
            "INSERT INTO MemoryProjectionReceipts(EventId,Destination) "
            "VALUES (?,?)", (event["EventId"], "external"),
        )

        # Explicit connection reads are intentionally transaction-local.
        self.assertEqual(
            repo.get_event(event["EventId"], connection=conn)["EventId"],
            event["EventId"],
        )
        self.assertEqual(len(repo.events_since(connection=conn)), 1)
        self.assertEqual(len(repo.pending_outbox(connection=conn)), 1)
        self.assertTrue(repo.has_legacy_receipt(
            event["EventId"], "external", connection=conn
        ))
        self.assertTrue(repo.has_projection_receipt(
            event["EventId"], "external", connection=conn
        ))

        implicit_mutations = (
            lambda: repo.append_event(
                stream_id="external:implicit", event_type="test.external",
                payload={}, idempotency_key="external-implicit",
            ),
            lambda: repo.ensure_outbox(event["EventId"], "extra"),
            lambda: repo.claim_outbox_entry(event["EventId"], "adx"),
            lambda: repo.claim_outbox(limit=10),
            lambda: repo.complete_outbox(event["EventId"], "adx"),
            lambda: repo.fail_outbox(event["EventId"], "error", "adx"),
            lambda: repo.record_legacy_receipt(event["EventId"], "implicit", 1),
        )
        implicit_reads = (
            lambda: repo.get_event(event["EventId"]),
            lambda: repo.get_by_idempotency_key("external-caller-event"),
            lambda: repo.events_for_turn("external-turn"),
            lambda: repo.list_stream("external:caller"),
            lambda: repo.events_since(),
            lambda: repo.events_since_timestamp(""),
            lambda: repo.pending_outbox(),
            lambda: repo.outbox_status(),
            lambda: repo.has_legacy_receipt(event["EventId"], "external"),
            lambda: repo.has_projection_receipt(event["EventId"], "external"),
        )
        try:
            for index, operation in enumerate(implicit_mutations):
                with self.subTest(kind="mutation", index=index):
                    with self.assertRaises(EventStoreError):
                        operation()
            for index, operation in enumerate(implicit_reads):
                with self.subTest(kind="read", index=index):
                    with self.assertRaises(EventStoreError):
                        operation()
        finally:
            conn.rollback()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM MemoryEvents WHERE EventId=?",
                (event["EventId"],),
            ).fetchone()[0],
            0,
        )
        repo.close()
        conn.close()

    def test_factory_implicit_reads_never_observe_repository_rollback(self):
        """Every implicit factory read waits for rollback and returns durable state."""
        from bridge.event_store import EventRepositoryV2
        from bridge.events import EventStoreError, deterministic_event_id
        from bridge.migrations import run_migrations

        path = os.path.join(_TMP_HOME, f"reads_{uuid.uuid4().hex[:8]}.db")
        conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        run_migrations(conn)
        installation = "read-rollback"
        idempotency_key = "read-rollback-event"
        event_id = deterministic_event_id(installation, idempotency_key)
        repo = EventRepositoryV2(lambda: conn, installation_id=installation)
        original_append = repo._append_in_transaction
        original_lock = repo._factory_connection_lock(conn)
        inserted = threading.Event()
        all_readers_attempted = threading.Event()
        release_rollback = threading.Event()
        attempt_count = 0
        attempt_lock = threading.Lock()
        results = {}
        errors = []

        class TrackingLock:
            def __enter__(inner_self):
                nonlocal attempt_count
                if threading.current_thread().name.startswith("reader-"):
                    with attempt_lock:
                        attempt_count += 1
                        if attempt_count == 10:
                            all_readers_attempted.set()
                original_lock.acquire()
                return inner_self

            def __exit__(inner_self, exc_type, exc, traceback):
                original_lock.release()

        def append_then_rollback(active, **values):
            value = original_append(active, **values)
            active.execute(
                "INSERT INTO LegacyProjectionReceipts "
                "(EventId,ProjectionName,RowCount) VALUES (?,?,?)",
                (event_id, "rolled-back", 1),
            )
            active.execute(
                "INSERT INTO MemoryProjectionReceipts(EventId,Destination) "
                "VALUES (?,?)", (event_id, "rolled-back"),
            )
            inserted.set()
            if not release_rollback.wait(timeout=5):
                raise RuntimeError("rollback release timeout")
            raise EventStoreError("injected rollback")

        readers = {
            "get_event": lambda: repo.get_event(event_id),
            "idempotency": lambda: repo.get_by_idempotency_key(idempotency_key),
            "turn": lambda: repo.events_for_turn("read-rollback-turn"),
            "stream": lambda: repo.list_stream("read-rollback:stream"),
            "since": lambda: repo.events_since(),
            "timestamp": lambda: repo.events_since_timestamp(""),
            "pending": lambda: repo.pending_outbox(),
            "status": lambda: repo.outbox_status(),
            "legacy_receipt": lambda: repo.has_legacy_receipt(
                event_id, "rolled-back"
            ),
            "projection_receipt": lambda: repo.has_projection_receipt(
                event_id, "rolled-back"
            ),
        }

        def writer():
            try:
                repo.append_event(
                    stream_id="read-rollback:stream",
                    event_type="test.read_rollback",
                    payload={"value": 1},
                    turn_id="read-rollback-turn",
                    consent_scope="cloud_allowed",
                    idempotency_key=idempotency_key,
                )
            except Exception as exc:
                errors.append(("writer", type(exc).__name__))

        def reader(name, operation):
            try:
                results[name] = operation()
            except Exception as exc:
                errors.append((name, type(exc).__name__))

        with mock.patch.object(repo, "_append_in_transaction", append_then_rollback), \
            mock.patch.object(
                repo, "_factory_connection_lock",
                return_value=TrackingLock(),
            ):
            writer_thread = threading.Thread(
                target=writer, name="writer", daemon=True
            )
            writer_thread.start()
            self.assertTrue(inserted.wait(timeout=5))
            reader_threads = [
                threading.Thread(
                    target=reader, args=(name, operation),
                    name=f"reader-{name}", daemon=True,
                )
                for name, operation in readers.items()
            ]
            for thread in reader_threads:
                thread.start()
            self.assertTrue(all_readers_attempted.wait(timeout=5))
            release_rollback.set()
            writer_thread.join(timeout=10)
            for thread in reader_threads:
                thread.join(timeout=10)
        try:
            self.assertFalse(writer_thread.is_alive())
            self.assertFalse(any(thread.is_alive() for thread in reader_threads))
            self.assertEqual(errors, [("writer", "EventStoreError")])
            self.assertIsNone(results["get_event"])
            self.assertIsNone(results["idempotency"])
            for name in ("turn", "stream", "since", "timestamp", "pending"):
                self.assertEqual(results[name], [])
            self.assertEqual(results["status"], {})
            self.assertFalse(results["legacy_receipt"])
            self.assertFalse(results["projection_receipt"])
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM MemoryEvents WHERE EventId=?", (event_id,)
                ).fetchone()[0],
                0,
            )
        finally:
            release_rollback.set()
            repo.close()
            conn.close()

    def test_per_thread_factory_wait_does_not_block_caller_connection(self):
        """Distinct factory connections never form transaction↔repository ABBA."""
        from bridge.event_store import EventRepositoryV2
        from bridge.migrations import run_migrations

        path = os.path.join(_TMP_HOME, f"perthread_{uuid.uuid4().hex[:8]}.db")
        setup = sqlite3.connect(path)
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("PRAGMA foreign_keys=ON")
        setup.row_factory = sqlite3.Row
        run_migrations(setup)
        source_repo = EventRepositoryV2(lambda: setup, installation_id="per-thread")
        source = source_repo.append_event(
            stream_id="per-thread:source",
            event_type="test.per_thread",
            payload={"source": True},
            idempotency_key="per-thread-source",
        )
        source_repo.close()
        setup.close()

        caller_raw = sqlite3.connect(path, timeout=5, check_same_thread=False)
        worker_raw = sqlite3.connect(path, timeout=5, check_same_thread=False)
        for active in (caller_raw, worker_raw):
            active.execute("PRAGMA foreign_keys=ON")
            active.execute("PRAGMA busy_timeout=5000")
            active.row_factory = sqlite3.Row

        begin_attempted = threading.Event()

        class ConnectionProxy:
            def __init__(self, active, signal_begin=False):
                self.active = active
                self.signal_begin = signal_begin

            @property
            def in_transaction(self):
                return self.active.in_transaction

            def execute(self, sql, params=()):
                if self.signal_begin and sql == "BEGIN IMMEDIATE":
                    begin_attempted.set()
                return self.active.execute(sql, params)

            def commit(self):
                return self.active.commit()

            def rollback(self):
                return self.active.rollback()

        caller_conn = ConnectionProxy(caller_raw)
        worker_conn = ConnectionProxy(worker_raw, signal_begin=True)
        thread_connections = {"implicit-worker": worker_conn}

        def factory():
            return thread_connections.get(threading.current_thread().name, caller_conn)

        repo = EventRepositoryV2(factory, installation_id="per-thread")
        results = []
        errors = []

        def implicit_worker():
            try:
                repo.record_legacy_receipt(
                    source["EventId"], "implicit-worker", 1
                )
                results.append("implicit")
            except Exception as exc:
                errors.append(type(exc).__name__)

        caller_conn.execute("BEGIN IMMEDIATE")
        worker = threading.Thread(
            target=implicit_worker, name="implicit-worker", daemon=True
        )
        worker.start()
        self.assertTrue(begin_attempted.wait(timeout=5))
        started = datetime.datetime.now(datetime.timezone.utc)
        repo.record_legacy_receipt(
            source["EventId"], "caller-explicit", 1,
            connection=caller_conn,
        )
        elapsed = (
            datetime.datetime.now(datetime.timezone.utc) - started
        ).total_seconds()
        caller_conn.commit()
        worker.join(timeout=10)
        try:
            self.assertLess(elapsed, 0.5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results, ["implicit"])
            receipts = caller_raw.execute(
                "SELECT ProjectionName FROM LegacyProjectionReceipts "
                "WHERE EventId=? ORDER BY ProjectionName",
                (source["EventId"],),
            ).fetchall()
            self.assertEqual(
                [row[0] for row in receipts],
                ["caller-explicit", "implicit-worker"],
            )
        finally:
            caller_conn.rollback()
            repo.close()
            caller_raw.close()
            worker_raw.close()

    def test_expected_version_conflict(self):
        """Concurrent expected-version conflicts raise ConcurrentStreamError."""
        from bridge.events import ConcurrentStreamError
        self.repo.append_event(
            stream_id="sv", event_type="test.v0", payload={},
        )
        with self.assertRaises(ConcurrentStreamError) as ctx:
            self.repo.append_event(
                stream_id="sv", event_type="test.v1", payload={},
                expected_version=-1,
            )
        self.assertEqual(ctx.exception.expected_version, -1)
        self.assertEqual(ctx.exception.actual_version, 0)

    def test_parallel_append_version_conflict(self):
        """Multiple threads appending with same expected_version: at least one conflict."""
        from bridge.events import ConcurrentStreamError, EventRepository
        from bridge.identity import get_installation_id
        results = {"success": 0, "conflict": 0}
        lock = threading.Lock()
        db_path = self.mem.db_path

        def worker(worker_id):
            conn = sqlite3.connect(db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            repo = EventRepository(lambda c=conn: c, installation_id=get_installation_id())
            try:
                repo.append_event(
                    stream_id="race", event_type="test.race",
                    payload={"w": worker_id},
                    expected_version=-1,
                    idempotency_key=f"race-{worker_id}",
                )
                with lock:
                    results["success"] += 1
            except (ConcurrentStreamError, Exception):
                with lock:
                    results["conflict"] += 1
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(worker, i) for i in range(4)]
            concurrent.futures.wait(futures)

        total = results["success"] + results["conflict"]
        self.assertEqual(total, 4)
        self.assertGreaterEqual(results["success"], 1)
        self.assertGreaterEqual(results["conflict"], 1)

    def test_rollback_does_not_commit_caller_transaction(self):
        """ConcurrentStreamError leaves the connection in a usable state."""
        from bridge.events import ConcurrentStreamError
        self.repo.append_event(stream_id="rb", event_type="t1", payload={})
        try:
            self.repo.append_event(stream_id="rb", event_type="t2", payload={}, expected_version=-1)
        except ConcurrentStreamError:
            pass
        # Connection should still work
        ev = self.repo.append_event(stream_id="rb2", event_type="t3", payload={})
        self.assertIsNotNone(ev["EventId"])


# ═══════════════════════════════════════════════════════════════════
#  D. Canonical Deterministic Identity / Idempotency
# ═══════════════════════════════════════════════════════════════════
class TestDeterministicIdentity(unittest.TestCase):

    def setUp(self):
        self.mem = _fresh_mem("identity")
        self.repo = self.mem.event_repository()

    def tearDown(self):
        self.mem.close()

    def test_deterministic_event_id(self):
        """Same idempotency key produces same event ID (UUIDv5)."""
        from bridge.events import deterministic_event_id
        id1 = deterministic_event_id("install-1", "key-A")
        id2 = deterministic_event_id("install-1", "key-A")
        id3 = deterministic_event_id("install-1", "key-B")
        self.assertEqual(id1, id2)
        self.assertNotEqual(id1, id3)

    def test_deterministic_source_message_id(self):
        """Source message IDs are deterministic from turn+role+index."""
        from bridge.events import deterministic_source_message_id
        mid1 = deterministic_source_message_id("turn-1", "user", 0)
        mid2 = deterministic_source_message_id("turn-1", "user", 0)
        mid3 = deterministic_source_message_id("turn-1", "assistant", 0)
        self.assertEqual(mid1, mid2)
        self.assertNotEqual(mid1, mid3)

    def test_idempotent_retry_same_content(self):
        """Same idempotency key + same content returns existing event."""
        ev1 = self.repo.append_event(
            stream_id="idem", event_type="test.idem",
            payload={"v": 1}, idempotency_key="idem-001",
        )
        ev2 = self.repo.append_event(
            stream_id="idem", event_type="test.idem",
            payload={"v": 1}, idempotency_key="idem-001",
        )
        self.assertEqual(ev1["EventId"], ev2["EventId"])
        stream = self.repo.list_stream("idem")
        self.assertEqual(len(stream), 1)

    def test_idempotency_collision_different_payload(self):
        """Same key but different payload raises IdempotencyCollisionError."""
        from bridge.events import IdempotencyCollisionError
        self.repo.append_event(
            stream_id="coll", event_type="test.coll",
            payload={"v": 1}, idempotency_key="coll-001",
        )
        with self.assertRaises(IdempotencyCollisionError):
            self.repo.append_event(
                stream_id="coll", event_type="test.coll",
                payload={"v": 999}, idempotency_key="coll-001",
            )

    def test_idempotency_collision_different_stream(self):
        """Same key but different stream raises IdempotencyCollisionError."""
        from bridge.events import IdempotencyCollisionError
        self.repo.append_event(
            stream_id="s1", event_type="test.x", payload={},
            idempotency_key="stream-coll",
        )
        with self.assertRaises(IdempotencyCollisionError):
            self.repo.append_event(
                stream_id="s2", event_type="test.x", payload={},
                idempotency_key="stream-coll",
            )

    def test_rejects_oversized_stream_id(self):
        """Oversized stream_id is rejected, not truncated."""
        from bridge.events import ValidationError
        with self.assertRaises(ValidationError) as ctx:
            self.repo.append_event(
                stream_id="x" * 600, event_type="test.big", payload={},
            )
        self.assertEqual(ctx.exception.field, "stream_id")

    def test_rejects_empty_event_type(self):
        """Empty event_type is rejected."""
        from bridge.events import ValidationError
        with self.assertRaises(ValidationError) as ctx:
            self.repo.append_event(
                stream_id="s", event_type="", payload={},
            )
        self.assertEqual(ctx.exception.field, "event_type")

    def test_rejects_invalid_expected_version(self):
        """expected_version < -1 is rejected."""
        from bridge.events import ValidationError
        with self.assertRaises(ValidationError):
            self.repo.append_event(
                stream_id="s", event_type="t", payload={},
                expected_version=-2,
            )

    def test_canonical_json_rejects_nan(self):
        """NaN in payload raises ValidationError."""
        from bridge.events import ValidationError
        with self.assertRaises(ValidationError):
            self.repo.append_event(
                stream_id="nan", event_type="test.nan",
                payload={"val": float("nan")},
            )

    def test_canonical_json_rejects_infinity(self):
        """Infinity in payload raises ValidationError."""
        from bridge.events import ValidationError
        with self.assertRaises(ValidationError):
            self.repo.append_event(
                stream_id="inf", event_type="test.inf",
                payload={"val": float("inf")},
            )

    def test_canonical_json_nfc_unicode(self):
        """Canonical JSON normalizes Unicode to NFC."""
        import unicodedata
        from bridge.events import canonical_json
        # é as combining sequence vs precomposed
        combining = "e\u0301"  # e + combining acute
        precomposed = "\u00e9"  # é precomposed
        c1 = canonical_json({"name": combining})
        c2 = canonical_json({"name": precomposed})
        self.assertEqual(c1, c2)

    def test_canonical_json_key_order(self):
        """Canonical JSON is key-order deterministic."""
        from bridge.events import canonical_json
        a = canonical_json({"z": 1, "a": 2, "m": 3})
        b = canonical_json({"m": 3, "z": 1, "a": 2})
        self.assertEqual(a, b)
        self.assertEqual(a, '{"a":2,"m":3,"z":1}')

    def test_payload_too_large(self):
        """Payload exceeding 64 KiB raises PayloadTooLargeError."""
        from bridge.events import PayloadTooLargeError
        big = {"data": "x" * (65 * 1024)}
        with self.assertRaises(PayloadTooLargeError):
            self.repo.append_event(stream_id="big", event_type="test.big", payload=big)

    def test_trust_validation(self):
        """Trust must be finite float in [0,1]."""
        from bridge.events import ValidationError
        with self.assertRaises(ValidationError):
            self.repo.append_event(stream_id="t", event_type="t", payload={}, trust=1.5)
        with self.assertRaises(ValidationError):
            self.repo.append_event(stream_id="t", event_type="t", payload={}, trust=-0.1)
        with self.assertRaises(ValidationError):
            self.repo.append_event(stream_id="t", event_type="t", payload={}, trust=float("nan"))

    def test_sensitivity_enum_validation(self):
        """Invalid sensitivity value is rejected."""
        from bridge.events import ValidationError
        with self.assertRaises(ValidationError):
            self.repo.append_event(
                stream_id="s", event_type="t", payload={}, sensitivity="invalid",
            )

    def test_consent_scope_enum_validation(self):
        """Invalid consent_scope value is rejected."""
        from bridge.events import ValidationError
        with self.assertRaises(ValidationError):
            self.repo.append_event(
                stream_id="s", event_type="t", payload={}, consent_scope="invalid",
            )


# ═══════════════════════════════════════════════════════════════════
#  E. Stable Identity / Request Envelope
# ═══════════════════════════════════════════════════════════════════
class TestIdentityEnvelope(unittest.TestCase):

    def test_stable_installation_id(self):
        from bridge.identity import get_installation_id
        id1 = get_installation_id()
        id2 = get_installation_id()
        self.assertEqual(id1, id2)
        self.assertEqual(len(id1), 36)

    def test_stable_user_id(self):
        from bridge.identity import get_user_id
        id1 = get_user_id()
        id2 = get_user_id()
        self.assertEqual(id1, id2)

    def test_envelope_rejects_invalid_turn_id(self):
        """Supplied invalid UUID turn_id raises EnvelopeValidationError."""
        from bridge.identity import RequestEnvelope, EnvelopeValidationError
        with self.assertRaises(EnvelopeValidationError) as ctx:
            RequestEnvelope({"turn_id": "not-a-uuid"})
        self.assertEqual(ctx.exception.field, "turn_id")

    def test_envelope_rejects_invalid_request_id(self):
        """Supplied invalid request_id raises."""
        from bridge.identity import RequestEnvelope, EnvelopeValidationError
        with self.assertRaises(EnvelopeValidationError):
            RequestEnvelope({"request_id": "garbage"})

    def test_envelope_rejects_oversized_session_id(self):
        """Session ID > 512 chars is rejected."""
        from bridge.identity import RequestEnvelope, EnvelopeValidationError
        with self.assertRaises(EnvelopeValidationError):
            RequestEnvelope({"session_id": "x" * 600})

    def test_envelope_rejects_invalid_actor(self):
        """Invalid actor enum raises."""
        from bridge.identity import RequestEnvelope, EnvelopeValidationError
        with self.assertRaises(EnvelopeValidationError):
            RequestEnvelope({"actor": "hacker"})

    def test_envelope_rejects_invalid_origin(self):
        """Invalid origin enum raises."""
        from bridge.identity import RequestEnvelope, EnvelopeValidationError
        with self.assertRaises(EnvelopeValidationError):
            RequestEnvelope({"origin": "unknown_source"})

    def test_envelope_generates_missing_server_fields(self):
        """Absent request_id/correlation_id are generated server-side."""
        from bridge.identity import RequestEnvelope
        env = RequestEnvelope({})
        self.assertTrue(len(env.request_id) == 36)
        self.assertTrue(len(env.turn_id) == 36)
        self.assertEqual(env.correlation_id, env.request_id)

    def test_envelope_preserves_valid_uuids(self):
        """Valid supplied UUIDs are preserved."""
        from bridge.identity import RequestEnvelope
        tid = str(uuid.uuid4())
        rid = str(uuid.uuid4())
        env = RequestEnvelope({"turn_id": tid, "request_id": rid})
        self.assertEqual(env.turn_id, tid.lower())
        self.assertEqual(env.request_id, rid.lower())

    def test_envelope_server_owns_installation_id(self):
        """installation_id from request is overridden by server."""
        from bridge.identity import RequestEnvelope, get_installation_id
        env = RequestEnvelope({"installation_id": "fake-id"})
        self.assertEqual(env.installation_id, get_installation_id())

    def test_envelope_to_dict(self):
        """to_dict returns all expected keys."""
        from bridge.identity import RequestEnvelope
        env = RequestEnvelope({})
        d = env.to_dict()
        expected = {"request_id", "installation_id", "user_id", "session_id",
                    "turn_id", "correlation_id", "actor", "origin", "egress_mode"}
        self.assertEqual(set(d.keys()), expected)


# ═══════════════════════════════════════════════════════════════════
#  F. Finalize Turn — Exactly-Once Projections
# ═══════════════════════════════════════════════════════════════════
class TestFinalizeTurn(unittest.TestCase):

    def setUp(self):
        self.mem = _fresh_mem("finalize")
        self.repo = self.mem.event_repository()

    def tearDown(self):
        self.mem.close()

    def test_finalize_creates_events_and_legacy(self):
        """finalize_turn creates events and projects to legacy tables."""
        from bridge.finalize import finalize_turn
        turn_id = str(uuid.uuid4())
        result = finalize_turn(
            self.mem, self.repo,
            session_id="sess-1", turn_id=turn_id,
            user_message="Hello Eva", assistant_message="Hello!",
            model="test-model",
        )
        self.assertEqual(result["turn_id"], turn_id)
        self.assertEqual(result["session_id"], "sess-1")
        self.assertGreaterEqual(len(result["event_ids"]), 2)
        # Legacy projection
        rows = self.mem.query("SELECT * FROM Conversations WHERE SessionId = 'sess-1'")
        self.assertEqual(len(rows), 2)

    def test_finalize_retry_exactly_once(self):
        """Retrying finalize_turn with same turn_id produces no duplicates."""
        from bridge.finalize import finalize_turn
        turn_id = str(uuid.uuid4())
        kwargs = dict(
            session_id="once-sess", turn_id=turn_id,
            user_message="Test msg", assistant_message="Test resp",
            model="model",
        )
        r1 = finalize_turn(self.mem, self.repo, **kwargs)
        r2 = finalize_turn(self.mem, self.repo, **kwargs)
        # Same event IDs returned (idempotent)
        self.assertEqual(r1["event_ids"], r2["event_ids"])
        # No duplicate conversations
        rows = self.mem.query("SELECT COUNT(*) as cnt FROM Conversations WHERE SessionId = 'once-sess'")
        self.assertEqual(rows[0]["cnt"], 2)  # user + assistant, not 4

    def test_finalize_with_fact_extraction(self):
        """finalize_turn with fact extractor creates fact events."""
        from bridge.finalize import finalize_turn
        turn_id = str(uuid.uuid4())

        def fake_extract(msg):
            return [{"Entity": "User", "Relation": "name", "Value": "Alice", "Confidence": 0.9}]

        result = finalize_turn(
            self.mem, self.repo,
            session_id="fact-sess", turn_id=turn_id,
            user_message="My name is Alice",
            assistant_message="Nice to meet you!",
            model="m", extract_facts_fn=fake_extract,
        )
        self.assertGreater(len(result["event_ids"]), 2)
        # Fact projected to Knowledge
        rows = self.mem.query("SELECT * FROM Knowledge WHERE Entity='User' AND Relation='name' AND Value='Alice'")
        self.assertGreater(len(rows), 0)

    def test_finalize_concurrent_same_turn(self):
        """Concurrent finalize_turn calls with same turn_id: no duplicates."""
        from bridge.finalize import finalize_turn
        from bridge.events import EventRepository
        from bridge.identity import get_installation_id
        turn_id = str(uuid.uuid4())
        db_path = self.mem.db_path
        results = []
        errors = []

        def worker():
            from sqlite_memory import SqliteMemory
            SqliteMemory._instances.pop(db_path, None)
            mem = SqliteMemory(db_path)
            repo = mem.event_repository()
            try:
                r = finalize_turn(
                    mem, repo,
                    session_id="concurrent", turn_id=turn_id,
                    user_message="msg", assistant_message="resp",
                    model="m",
                )
                results.append(r)
            except Exception as e:
                errors.append(e)
            finally:
                mem.close()

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All should succeed (idempotent) or get handled gracefully
        self.assertGreater(len(results), 0)
        # No duplicate events
        from sqlite_memory import SqliteMemory
        SqliteMemory._instances.pop(db_path, None)
        mem_check = SqliteMemory(db_path)
        events = mem_check.event_repository().list_stream("conversation:concurrent")
        self.assertEqual(len(events), 2)  # user + assistant only
        mem_check.close()

    def test_exchange_ordinal_from_events(self):
        """Exchange ordinal is derived from persisted events, not global counter."""
        from bridge.finalize import finalize_turn
        r1 = finalize_turn(
            self.mem, self.repo,
            session_id="ord-sess", turn_id=str(uuid.uuid4()),
            user_message="msg1", assistant_message="resp1", model="m",
        )
        self.assertEqual(r1["exchange_ordinal"], 0)
        r2 = finalize_turn(
            self.mem, self.repo,
            session_id="ord-sess", turn_id=str(uuid.uuid4()),
            user_message="msg2", assistant_message="resp2", model="m",
        )
        self.assertEqual(r2["exchange_ordinal"], 1)


# ═══════════════════════════════════════════════════════════════════
#  G. Generic Mutation Service (Goals, Skills, Background)
# ═══════════════════════════════════════════════════════════════════
class TestMutationService(unittest.TestCase):

    def setUp(self):
        self.mem = _fresh_mem("mutation")
        self.repo = self.mem.event_repository()

    def tearDown(self):
        self.mem.close()

    def test_mutate_goal_event_first(self):
        """Goal mutation creates event then projects to legacy."""
        from bridge.finalize import mutate_event
        ev = mutate_event(
            self.mem, self.repo,
            stream_id="goal:g1", event_type="goal.created",
            payload={"GoalId": "g1", "Title": "Test Goal", "Status": "active"},
            legacy_table="Goals",
            legacy_columns=["GoalId", "Title", "Status", "CreatedAt", "UpdatedAt"],
            legacy_row={"GoalId": "g1", "Title": "Test Goal", "Status": "active",
                        "CreatedAt": "2026-01-01T00:00:00Z", "UpdatedAt": "2026-01-01T00:00:00Z"},
            idempotency_key="goal:create:g1",
        )
        self.assertIn("EventId", ev)
        goals = self.mem.query("SELECT * FROM Goals WHERE GoalId='g1'")
        self.assertGreater(len(goals), 0)

    def test_mutate_goal_retry_no_duplicate(self):
        """Retry of goal mutation doesn't duplicate legacy row."""
        from bridge.finalize import mutate_event
        kwargs = dict(
            stream_id="goal:g2", event_type="goal.created",
            payload={"GoalId": "g2", "Title": "G2"},
            legacy_table="Goals",
            legacy_columns=["GoalId", "Title", "Status"],
            legacy_row={"GoalId": "g2", "Title": "G2", "Status": "active"},
            idempotency_key="goal:create:g2",
        )
        mutate_event(self.mem, self.repo, **kwargs)
        mutate_event(self.mem, self.repo, **kwargs)
        goals = self.mem.query("SELECT * FROM Goals WHERE GoalId='g2'")
        # Should have at most 1 from mutation (seed may also have one)
        g2_rows = [g for g in goals if g["Title"] == "G2"]
        self.assertEqual(len(g2_rows), 1)


# ═══════════════════════════════════════════════════════════════════
#  H. Sensitive Content Validation + Redaction
# ═══════════════════════════════════════════════════════════════════
class TestSensitiveContent(unittest.TestCase):

    def test_validate_trust_range(self):
        from bridge.sensitive import validate_trust
        self.assertEqual(validate_trust(0.5), 0.5)
        self.assertEqual(validate_trust(0.0), 0.0)
        self.assertEqual(validate_trust(1.0), 1.0)
        with self.assertRaises(ValueError):
            validate_trust(1.5)
        with self.assertRaises(ValueError):
            validate_trust(-0.1)
        with self.assertRaises(ValueError):
            validate_trust(float("nan"))
        with self.assertRaises(ValueError):
            validate_trust(float("inf"))

    def test_validate_sensitivity_enum(self):
        from bridge.sensitive import validate_sensitivity
        self.assertEqual(validate_sensitivity("public"), "public")
        self.assertEqual(validate_sensitivity("normal"), "normal")
        self.assertEqual(validate_sensitivity("private"), "private")
        self.assertEqual(validate_sensitivity("secret"), "secret")
        with self.assertRaises(ValueError):
            validate_sensitivity("invalid")

    def test_validate_consent_scope_enum(self):
        from bridge.sensitive import validate_consent_scope
        self.assertEqual(validate_consent_scope("local_only"), "local_only")
        self.assertEqual(validate_consent_scope("cloud_allowed"), "cloud_allowed")
        with self.assertRaises(ValueError):
            validate_consent_scope("any")

    def test_redact_openai_key(self):
        from bridge.sensitive import redact_credentials
        # Use a variable to construct the fake key so static analysis doesn't flag it
        fake_prefix = "sk-"
        fake_key = fake_prefix + "a" * 30
        text = f"My key is {fake_key}"
        result = redact_credentials(text)
        self.assertNotIn(fake_key, result)
        self.assertIn("[REDACTED]", result)

    def test_redact_segmented_project_key_in_event_and_projection(self):
        from bridge.finalize import mutate_event
        from bridge.sensitive import redact_credentials
        segmented = "sk-" + "proj-" + "A" * 24 + "-" + "B" * 12
        self.assertEqual(redact_credentials(segmented), "[REDACTED]")
        self.assertEqual(redact_credentials("sk-123456789abc"), "sk-123456789abc")
        mem = _fresh_mem("redact_segmented")
        repo = mem.event_repository()
        event = mutate_event(
            mem, repo,
            stream_id="goal:redacted", event_type="goal.created",
            payload={
                "command": {"title": "Use " + segmented},
                "nested": {segmented: "credential-shaped key"},
            },
            legacy_table="Goals", legacy_columns=[
                "GoalId", "Title", "Status", "CreatedAt", "UpdatedAt",
            ],
            legacy_row={
                "GoalId": "redacted", "Title": "Use " + segmented,
                "Status": "active", "CreatedAt": "2026-07-10T00:00:00Z",
                "UpdatedAt": "2026-07-10T00:00:00Z",
            },
            idempotency_key="redact-segmented-project-key",
        )
        stored = repo.get_event(event["EventId"])["Payload"]
        projection = mem.query("SELECT Title FROM Goals WHERE GoalId='redacted'")[0]["Title"]
        self.assertNotIn(segmented, stored)
        self.assertNotIn(segmented, projection)
        self.assertIn("[REDACTED]", stored)
        self.assertIn("[REDACTED]", projection)
        self.assertIn("[REDACTED]", json.loads(stored)["nested"])
        mem.close()

    def test_redact_github_pat(self):
        from bridge.sensitive import redact_credentials
        fake_pat = "ghp_" + "A" * 36
        text = f"Token: {fake_pat}"
        result = redact_credentials(text)
        self.assertNotIn("ghp_", result)
        self.assertIn("[REDACTED]", result)

    def test_redact_current_provider_key_formats(self):
        from bridge.sensitive import redact_credentials
        synthetic_values = (
            "sk-" + "proj-" + "A" * 24 + "-" + "B" * 12,
            "github_" + "pat_" + "C" * 60,
            "AI" + "za" + "D" * 35,
        )
        for value in synthetic_values:
            redacted = redact_credentials("credential=" + value)
            self.assertNotIn(value, redacted)
            self.assertIn("[REDACTED]", redacted)

    def test_dotted_bearer_redacted_before_response_event_projection_and_egress(self):
        from bridge import core
        from bridge import state as st
        from bridge.core import BridgeHandler
        from bridge.identity import RequestEnvelope
        dotted = ".".join(("A" * 16, "B" * 24, "C" * 20))
        bearer = "Bearer " + dotted
        mem = _fresh_mem("redact_dotted_bearer")
        saved = (st.memory_backend, st.sqlite_mem, st.egress_mode)
        captured_rows = []
        try:
            st.memory_backend, st.sqlite_mem, st.egress_mode = "kusto", mem, "cloud"
            handler = object.__new__(BridgeHandler)
            envelope = RequestEnvelope({
                "session_id": str(uuid.uuid4()), "turn_id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()),
            })
            now = "2026-07-10T12:00:00.000000Z"
            row = {
                "GoalId": "goal-dotted-bearer", "Title": bearer,
                "Description": "JWT " + dotted, "Category": "relational",
                "Status": "active", "Priority": 50, "RelatedTopics": "",
                "CreatedAt": now, "UpdatedAt": now,
            }
            command = {
                "entity": "goal", "operation": "create",
                "fields": {"Title": bearer},
            }

            def ingest(_cluster, _db, _table, _columns, rows):
                captured_rows.extend(rows)
                return True

            with mock.patch.object(core, "_kusto_query_direct", return_value=[]), \
                    mock.patch.object(core, "_kusto_ingest_direct", side_effect=ingest):
                ok, persisted, _ = handler._write_goal_row(
                    "example-cluster", "example-db", row, envelope,
                    "goal.created", command,
                )
            self.assertTrue(ok)
            serialized_response = json.dumps(persisted)
            serialized_egress = json.dumps(captured_rows)
            event = mem.event_repository().get_by_idempotency_key(
                f"request:{envelope.request_id}:goal.created"
            )
            local = json.dumps(mem.query(
                "SELECT Title,Description FROM Goals WHERE GoalId='goal-dotted-bearer'"
            ))
            for content in (serialized_response, event["Payload"], local, serialized_egress):
                self.assertNotIn(dotted, content)
                self.assertIn("[REDACTED]", content)
        finally:
            st.memory_backend, st.sqlite_mem, st.egress_mode = saved
            mem.close()

    def test_redact_recursive_dict(self):
        from bridge.sensitive import redact_credentials
        fake_sk = "sk-" + "b" * 30
        fake_ghp = "ghp_" + "C" * 36
        data = {"key": fake_sk, "nested": {"token": fake_ghp}}
        result = redact_credentials(data)
        self.assertIn("[REDACTED]", result["key"])
        self.assertIn("[REDACTED]", result["nested"]["token"])

    def test_redact_in_finalize(self):
        """Credentials in messages are redacted before event persistence."""
        mem = _fresh_mem("redact")
        repo = mem.event_repository()
        from bridge.finalize import finalize_turn
        turn_id = str(uuid.uuid4())
        fake_key = "sk-" + "x" * 30
        finalize_turn(
            mem, repo,
            session_id="redact-sess", turn_id=turn_id,
            user_message=f"My key is {fake_key} please use it",
            assistant_message="I won't store that.",
            model="m",
        )
        events = repo.list_stream("conversation:redact-sess")
        for ev in events:
            payload = json.loads(ev["Payload"])
            if payload.get("role") == "user":
                self.assertNotIn(fake_key, payload["content"])
                self.assertIn("[REDACTED]", payload["content"])
        mem.close()

    def test_should_create_outbox_policy(self):
        from bridge.sensitive import should_create_outbox
        self.assertTrue(should_create_outbox("normal", "cloud_allowed"))
        self.assertFalse(should_create_outbox("secret", "cloud_allowed"))
        self.assertFalse(should_create_outbox("normal", "local_only"))
        self.assertFalse(should_create_outbox("normal", "session"))


# ═══════════════════════════════════════════════════════════════════
#  I. ADX Outbox — Ordering, Lease, Retry, Dead-Letter, Dedup
# ═══════════════════════════════════════════════════════════════════
class TestADXOutbox(unittest.TestCase):

    def setUp(self):
        self.mem = _fresh_mem("outbox")
        self.repo = self.mem.event_repository()

    def tearDown(self):
        self.mem.close()

    def test_projection_disabled_by_default(self):
        """ADX projection does nothing unless EVA_ADX_PROJECTION=1."""
        from bridge.adx_projection import project_pending_events
        os.environ.pop("EVA_ADX_PROJECTION", None)
        self.repo.append_event(
            stream_id="adx1", event_type="test.adx", payload={},
            consent_scope="cloud_allowed",
        )
        ok, fail = project_pending_events(self.repo, None, lambda: ("", ""))
        self.assertEqual(ok, 0)
        self.assertEqual(fail, 0)

    def test_claim_marks_processing(self):
        """claim_outbox atomically marks entries as processing."""
        self.repo.append_event(
            stream_id="claim1", event_type="test.claim", payload={},
            consent_scope="cloud_allowed",
        )
        claimed = self.repo.claim_outbox(limit=10)
        self.assertGreater(len(claimed), 0)
        # Status should be processing
        conn = self.mem._conn()
        row = conn.execute(
            "SELECT Status FROM MemoryOutbox WHERE EventId = ?",
            (claimed[0]["EventId"],),
        ).fetchone()
        self.assertEqual(row[0], "processing")

    def test_complete_outbox_records_receipt(self):
        """complete_outbox marks projected and records receipt."""
        ev = self.repo.append_event(
            stream_id="comp1", event_type="test.comp", payload={},
            consent_scope="cloud_allowed",
        )
        self.repo.complete_outbox(ev["EventId"], "adx")
        self.assertTrue(self.repo.has_projection_receipt(ev["EventId"], "adx"))
        conn = self.mem._conn()
        row = conn.execute(
            "SELECT Status FROM MemoryOutbox WHERE EventId = ?", (ev["EventId"],)
        ).fetchone()
        self.assertEqual(row[0], "projected")

    def test_fail_outbox_with_retry_and_dead_letter(self):
        """Exhausted retries move to dead_letter."""
        ev = self.repo.append_event(
            stream_id="fail1", event_type="test.fail", payload={},
            consent_scope="cloud_allowed",
        )
        # Set max attempts to 2
        conn = self.mem._conn()
        conn.execute("UPDATE MemoryOutbox SET MaxAttempts = 2 WHERE EventId = ?", (ev["EventId"],))
        conn.commit()
        # First failure: retry
        self.repo.fail_outbox(ev["EventId"], "err1", "adx", "2099-01-01T00:00:00Z")
        row = conn.execute("SELECT Status, Attempts FROM MemoryOutbox WHERE EventId = ?", (ev["EventId"],)).fetchone()
        self.assertEqual(row[0], "retry")
        self.assertEqual(row[1], 1)
        # Second failure: dead_letter
        self.repo.fail_outbox(ev["EventId"], "err2", "adx", "")
        row = conn.execute("SELECT Status, Attempts FROM MemoryOutbox WHERE EventId = ?", (ev["EventId"],)).fetchone()
        self.assertEqual(row[0], "dead_letter")
        self.assertEqual(row[1], 2)

    def test_pending_outbox_respects_next_attempt_at(self):
        """pending_outbox filters by NextAttemptAt <= now."""
        ev = self.repo.append_event(
            stream_id="future1", event_type="test.future", payload={},
            consent_scope="cloud_allowed",
        )
        conn = self.mem._conn()
        conn.execute(
            "UPDATE MemoryOutbox SET Status='retry', NextAttemptAt='2099-01-01T00:00:00Z' WHERE EventId=?",
            (ev["EventId"],),
        )
        conn.commit()
        pending = self.repo.pending_outbox()
        event_ids = [p["EventId"] for p in pending]
        self.assertNotIn(ev["EventId"], event_ids)

    def test_outbox_dedup_via_receipt(self):
        """Already-receipted events are skipped on re-projection."""
        from bridge.adx_projection import project_pending_events
        os.environ["EVA_ADX_PROJECTION"] = "1"
        ev = self.repo.append_event(
            stream_id="dedup1", event_type="test.dedup", payload={},
            consent_scope="cloud_allowed",
        )
        # Pre-record receipt
        self.repo.complete_outbox(ev["EventId"], "adx")
        # Reset outbox status to pending for test
        conn = self.mem._conn()
        conn.execute("UPDATE MemoryOutbox SET Status='pending' WHERE EventId=?", (ev["EventId"],))
        conn.commit()

        ingested = []
        def mock_ingest(cluster, db, table, columns, rows):
            ingested.extend(rows)
            return True

        ok, fail = project_pending_events(
            self.repo, mock_ingest, lambda: ("https://example.kusto.net", "Eva"),
        )
        os.environ.pop("EVA_ADX_PROJECTION", None)
        # Should not have re-ingested (receipt exists)
        self.assertEqual(len(ingested), 0)

    def test_outbox_status_summary(self):
        """outbox_status returns counts by destination and status."""
        self.repo.append_event(
            stream_id="stat1", event_type="test.stat", payload={},
            consent_scope="cloud_allowed",
        )
        status = self.repo.outbox_status()
        self.assertIn("adx", status)
        self.assertIn("pending", status["adx"])


# ═══════════════════════════════════════════════════════════════════
#  J. Pagination / JournalSequence
# ═══════════════════════════════════════════════════════════════════
class TestPagination(unittest.TestCase):

    def setUp(self):
        self.mem = _fresh_mem("pagination")
        self.repo = self.mem.event_repository()

    def tearDown(self):
        self.mem.close()

    def test_journal_sequence_monotonic(self):
        """JournalSequence is monotonically increasing."""
        ids = []
        for i in range(5):
            ev = self.repo.append_event(
                stream_id="seq", event_type="test.seq", payload={"i": i},
            )
            ids.append(ev["EventId"])
        # Read back with sequence
        events = self.repo.events_since(cursor_sequence=0)
        sequences = [e["JournalSequence"] for e in events if e["EventId"] in ids]
        self.assertEqual(len(sequences), 5)
        self.assertEqual(sequences, sorted(sequences))
        # Each is strictly greater than previous
        for i in range(1, len(sequences)):
            self.assertGreater(sequences[i], sequences[i - 1])

    def test_events_since_cursor(self):
        """events_since with cursor returns only events after cursor."""
        for i in range(5):
            self.repo.append_event(
                stream_id="cursor", event_type="test.cur", payload={"i": i},
            )
        all_events = self.repo.events_since(cursor_sequence=0)
        self.assertGreaterEqual(len(all_events), 5)
        mid_seq = all_events[2]["JournalSequence"]
        after = self.repo.events_since(cursor_sequence=mid_seq)
        for ev in after:
            self.assertGreater(ev["JournalSequence"], mid_seq)

    def test_events_since_no_timestamp_skipping(self):
        """Cursor-based pagination doesn't skip events with same timestamp."""
        # Insert multiple events (likely same millisecond)
        for i in range(10):
            self.repo.append_event(
                stream_id="noskip", event_type="test.fast", payload={"i": i},
            )
        events = self.repo.events_since(cursor_sequence=0)
        noskip = [e for e in events if e["StreamId"] == "noskip"]
        self.assertEqual(len(noskip), 10)


# ═══════════════════════════════════════════════════════════════════
#  K. Normalization / Backend Parity
# ═══════════════════════════════════════════════════════════════════
class TestNormalization(unittest.TestCase):

    def test_normalize_timestamp_utc(self):
        from bridge.normalization import normalize_timestamp
        self.assertEqual(normalize_timestamp("2026-07-10T12:00:00Z"), "2026-07-10T12:00:00Z")
        self.assertEqual(normalize_timestamp("2026-07-10T12:00:00+00:00"), "2026-07-10T12:00:00Z")
        self.assertIsNone(normalize_timestamp(""))
        self.assertIsNone(normalize_timestamp("not-a-date"))

    def test_normalize_timestamp_offset(self):
        from bridge.normalization import normalize_timestamp
        result = normalize_timestamp("2026-07-10T15:00:00+03:00")
        self.assertEqual(result, "2026-07-10T12:00:00Z")

    def test_latest_row_sql_with_tie_breaking(self):
        """latest_row_sql uses deterministic tie-breaking by rowid."""
        from bridge.normalization import latest_row_sql
        sql = latest_row_sql("Goals", "GoalId", "UpdatedAt")
        self.assertIn("rowid", sql)
        self.assertIn("NOT EXISTS", sql)

    def test_latest_row_hides_deleted(self):
        """latest_row_sql excludes rows with Status='deleted'."""
        from bridge.normalization import latest_row_sql
        mem = _fresh_mem("latest_del")
        mem.ingest("Goals", ["GoalId", "Title", "Status", "UpdatedAt"], [
            {"GoalId": "g1", "Title": "Active", "Status": "active", "UpdatedAt": "2026-01-01"},
        ])
        mem.ingest("Goals", ["GoalId", "Title", "Status", "UpdatedAt"], [
            {"GoalId": "g1", "Title": "Deleted", "Status": "deleted", "UpdatedAt": "2026-07-01"},
        ])
        sql = latest_row_sql("Goals", "GoalId", "UpdatedAt")
        rows = mem.query(sql)
        g1_rows = [r for r in rows if r["GoalId"] == "g1"]
        self.assertEqual(len(g1_rows), 0, "Deleted goal should be hidden")
        mem.close()

    def test_latest_row_tie_breaking_by_rowid(self):
        """When UpdatedAt is equal, higher rowid wins."""
        from bridge.normalization import latest_row_sql
        mem = _fresh_mem("tiebreak")
        mem.ingest("Goals", ["GoalId", "Title", "Status", "UpdatedAt"], [
            {"GoalId": "tie1", "Title": "First", "Status": "active", "UpdatedAt": "2026-01-01"},
        ])
        mem.ingest("Goals", ["GoalId", "Title", "Status", "UpdatedAt"], [
            {"GoalId": "tie1", "Title": "Second", "Status": "active", "UpdatedAt": "2026-01-01"},
        ])
        sql = latest_row_sql("Goals", "GoalId", "UpdatedAt")
        rows = mem.query(sql)
        tie_rows = [r for r in rows if r["GoalId"] == "tie1"]
        self.assertEqual(len(tie_rows), 1)
        self.assertEqual(tie_rows[0]["Title"], "Second")
        mem.close()

    def test_reconciliation_status_structure(self):
        """reconciliation_status returns complete structure."""
        from bridge.normalization import reconciliation_status
        mem = _fresh_mem("recon")
        repo = mem.event_repository()
        status = reconciliation_status(mem, repo)
        required_keys = {
            "reconciled", "event_count", "outbox_pending", "outbox_projected",
            "receipt_count", "unreceipted", "adx_unprojected",
            "local_only_events", "target_backend", "message",
        }
        self.assertEqual(set(status.keys()), required_keys)
        self.assertTrue(status["reconciled"])  # fresh DB
        mem.close()

    def test_reconciliation_with_unreceipted_events(self):
        """Unreceipted events make reconciliation report unreconciled."""
        from bridge.normalization import reconciliation_status
        mem = _fresh_mem("unrecon")
        repo = mem.event_repository()
        repo.append_event(stream_id="ur", event_type="test.ur", payload={})
        status = reconciliation_status(mem, repo)
        self.assertFalse(status["reconciled"])
        self.assertGreater(status["unreceipted"], 0)
        mem.close()


# ═══════════════════════════════════════════════════════════════════
#  L. Connection Cleanup / ResourceWarning
# ═══════════════════════════════════════════════════════════════════
class TestConnectionCleanup(unittest.TestCase):

    def test_close_clears_all_connections(self):
        """close() closes all tracked thread connections."""
        mem = _fresh_mem("cleanup")
        # Access connection
        conn = mem._conn()
        self.assertIsNotNone(conn)
        mem.close()
        # Connection should be closed (attempting execute should fail or be None)
        self.assertTrue(mem._closed)

    def test_no_resource_warning_on_close(self):
        """Properly closed SqliteMemory does not trigger ResourceWarning."""
        mem = _fresh_mem("nowarn")
        mem.event_repository()
        mem.close()
        gc.collect()  # Force GC to detect unclosed resources

    def test_closed_repo_raises(self):
        """Operations on closed repository raise EventStoreError."""
        from bridge.events import EventStoreError
        mem = _fresh_mem("closed_repo")
        repo = mem.event_repository()
        mem.close()
        with self.assertRaises(EventStoreError):
            repo.append_event(stream_id="x", event_type="t", payload={})


# ═══════════════════════════════════════════════════════════════════
#  M. Legacy Read Mode
# ═══════════════════════════════════════════════════════════════════
class TestLegacyReadMode(unittest.TestCase):

    def test_legacy_reads_still_authoritative(self):
        """With EVA_MEMORY_READ_MODE=legacy, query() returns legacy data."""
        mem = _fresh_mem("legacy_read")
        mem.ingest("Knowledge", ["Entity", "Relation", "Value"], [
            {"Entity": "Test", "Relation": "note", "Value": "legacy-data"},
        ])
        # Event write
        repo = mem.event_repository()
        repo.append_event(
            stream_id="k:Test", event_type="test.write", payload={"val": "event-data"},
        )
        # Legacy read still works
        rows = mem.query("SELECT * FROM Knowledge WHERE Entity = 'Test' AND Value = 'legacy-data'")
        self.assertGreater(len(rows), 0)
        self.assertEqual(rows[0]["Value"], "legacy-data")
        mem.close()


# ═══════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    os.chdir(PROJECT_ROOT)
    print(f"Phase 1 Tests — HOME={_TMP_HOME}")
    print(f"DB path: {os.environ.get('EVA_MEMORY_DB', 'default')}")
    print(f"Read mode: {os.environ.get('EVA_MEMORY_READ_MODE', 'legacy')}")
    print()
    try:
        unittest.main(verbosity=2)
    finally:
        # Cleanup temp dir
        try:
            shutil.rmtree(_TMP_HOME, ignore_errors=True)
        except Exception:
            pass
