#!/usr/bin/env python3
"""
Eva SQLite Memory Backend

Drop-in local replacement for the Kusto-based memory system. Stores all Eva
tables (Knowledge, Conversations, EmotionState, etc.) in a single SQLite file.

The two primary functions -- query() and ingest() -- return data in the same
list-of-dicts format as the bridge's _kusto_query_direct() and accept the same
column/row arguments as _kusto_ingest_direct(), making the bridge routing layer
a thin conditional.

Usage:
    from sqlite_memory import SqliteMemory
    mem = SqliteMemory("~/.eva/memory.db")
    mem.ingest("Knowledge", ["Entity","Relation","Value"], [{"Entity": "User", ...}])
    rows = mem.query("Knowledge", where="Entity = ?", params=("User",), limit=10)
"""

import json
import os
import sqlite3
import sys
import threading
import contextlib
import time

# ── Schema ──────────────────────────────────────────────────────────────────
# Mirrors eva_seed.kql. Column order matches Kusto table definitions so
# positional CSV ingest (used by the bridge) maps correctly.

_SCHEMA = {
    "SelfState": {
        "columns": [
            ("Timestamp", "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
            ("Capability", "TEXT NOT NULL"),
            ("Status", "TEXT NOT NULL"),
            ("Details", "TEXT DEFAULT '{}'"),
        ],
        "indexes": ["CREATE INDEX IF NOT EXISTS idx_selfstate_ts ON SelfState(Timestamp)"],
    },
    "Knowledge": {
        "columns": [
            ("Timestamp", "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
            ("Entity", "TEXT NOT NULL"),
            ("Relation", "TEXT NOT NULL"),
            ("Value", "TEXT NOT NULL"),
            ("Confidence", "REAL DEFAULT 0.5"),
            ("Source", "TEXT DEFAULT ''"),
            ("Decay", "REAL DEFAULT 0.01"),
        ],
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_knowledge_entity ON Knowledge(Entity)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_conf ON Knowledge(Confidence)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_ts ON Knowledge(Timestamp)",
        ],
        "fts": "CREATE VIRTUAL TABLE IF NOT EXISTS Knowledge_fts USING fts5(Entity, Relation, Value, content=Knowledge, content_rowid=rowid)",
        "triggers": [
            """CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON Knowledge BEGIN
                 INSERT INTO Knowledge_fts(rowid, Entity, Relation, Value)
                 VALUES (new.rowid, new.Entity, new.Relation, new.Value);
               END""",
            """CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON Knowledge BEGIN
                 INSERT INTO Knowledge_fts(Knowledge_fts, rowid, Entity, Relation, Value)
                 VALUES ('delete', old.rowid, old.Entity, old.Relation, old.Value);
               END""",
        ],
    },
    "Conversations": {
        "columns": [
            ("SessionId", "TEXT NOT NULL"),
            ("Timestamp", "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
            ("Role", "TEXT NOT NULL"),
            ("Provider", "TEXT DEFAULT ''"),
            ("Model", "TEXT DEFAULT ''"),
            ("Content", "TEXT NOT NULL"),
            ("TokenEstimate", "INTEGER DEFAULT 0"),
            ("ImageGenerated", "INTEGER DEFAULT 0"),
        ],
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_conv_ts ON Conversations(Timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_conv_session ON Conversations(SessionId)",
            "CREATE INDEX IF NOT EXISTS idx_conv_role ON Conversations(Role)",
        ],
    },
    "EmotionState": {
        "columns": [
            ("Timestamp", "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
            ("Joy", "REAL DEFAULT 0.5"),
            ("Curiosity", "REAL DEFAULT 0.5"),
            ("Concern", "REAL DEFAULT 0.1"),
            ("Excitement", "REAL DEFAULT 0.5"),
            ("Calm", "REAL DEFAULT 0.8"),
            ("Empathy", "REAL DEFAULT 0.5"),
            ("Trigger", "TEXT DEFAULT ''"),
            ("DecayRate", "REAL DEFAULT 0.1"),
        ],
        "indexes": ["CREATE INDEX IF NOT EXISTS idx_emotion_ts ON EmotionState(Timestamp)"],
    },
    "EmotionBaseline": {
        "columns": [
            ("Dimension", "TEXT NOT NULL"),
            ("Value", "REAL NOT NULL"),
        ],
    },
    "MemorySummaries": {
        "columns": [
            ("Period", "TEXT NOT NULL"),
            ("Summary", "TEXT NOT NULL"),
            ("Timestamp", "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ],
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_memsumm_ts ON MemorySummaries(Timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_memsumm_period ON MemorySummaries(Period)",
        ],
    },
    "Reflections": {
        "columns": [
            ("Timestamp", "TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
            ("Trigger", "TEXT DEFAULT ''"),
            ("Observation", "TEXT DEFAULT ''"),
            ("ActionTaken", "TEXT DEFAULT ''"),
            ("Effectiveness", "TEXT DEFAULT ''"),
        ],
        "indexes": ["CREATE INDEX IF NOT EXISTS idx_refl_ts ON Reflections(Timestamp)"],
    },
    "HeuristicsIndex": {
        "columns": [
            ("Entity", "TEXT NOT NULL"),
            ("Category", "TEXT DEFAULT ''"),
            ("LastSeen", "TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
            ("Frequency", "INTEGER DEFAULT 1"),
            ("Sentiment", "REAL DEFAULT 0.0"),
            ("Tags", "TEXT DEFAULT '[]'"),
            ("Context", "TEXT DEFAULT ''"),
        ],
        "indexes": ["CREATE INDEX IF NOT EXISTS idx_heur_entity ON HeuristicsIndex(Entity)"],
    },
    "Goals": {
        "columns": [
            ("GoalId", "TEXT NOT NULL"),
            ("Title", "TEXT NOT NULL"),
            ("Description", "TEXT DEFAULT ''"),
            ("Category", "TEXT DEFAULT 'self_improvement'"),
            ("Status", "TEXT DEFAULT 'active'"),
            ("Priority", "INTEGER DEFAULT 50"),
            ("RelatedTopics", "TEXT DEFAULT ''"),
            ("CreatedAt", "TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
            ("UpdatedAt", "TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ],
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_goals_id ON Goals(GoalId)",
            "CREATE INDEX IF NOT EXISTS idx_goals_status ON Goals(Status)",
        ],
    },
    "BackgroundProposals": {
        "columns": [
            ("ProposalId", "TEXT NOT NULL"),
            ("CreatedAt", "TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
            ("JobType", "TEXT DEFAULT ''"),
            ("TargetTable", "TEXT DEFAULT ''"),
            ("Payload", "TEXT DEFAULT '{}'"),
            ("Status", "TEXT DEFAULT 'pending'"),
            ("SourceWindowStart", "TEXT DEFAULT ''"),
            ("SourceWindowEnd", "TEXT DEFAULT ''"),
            ("Notes", "TEXT DEFAULT ''"),
            ("ReviewedAt", "TEXT DEFAULT ''"),
            ("ReviewedBy", "TEXT DEFAULT ''"),
        ],
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_bgprop_status ON BackgroundProposals(Status)",
        ],
    },
    "BackgroundActivity": {
        "columns": [
            ("TickId", "TEXT NOT NULL"),
            ("StartedAt", "TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
            ("EndedAt", "TEXT DEFAULT ''"),
            ("JobType", "TEXT DEFAULT ''"),
            ("Status", "TEXT DEFAULT ''"),
            ("ProposalCount", "INTEGER DEFAULT 0"),
            ("TokenEstimate", "INTEGER DEFAULT 0"),
            ("Notes", "TEXT DEFAULT ''"),
        ],
    },
    "Skills": {
        "columns": [
            ("SkillId", "TEXT NOT NULL"),
            ("Name", "TEXT NOT NULL"),
            ("Description", "TEXT DEFAULT ''"),
            ("Instructions", "TEXT DEFAULT ''"),
            ("Tools", "TEXT DEFAULT ''"),
            ("Tags", "TEXT DEFAULT ''"),
            ("Source", "TEXT DEFAULT ''"),
            ("Status", "TEXT DEFAULT 'active'"),
            ("CreatedAt", "TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
            ("UpdatedAt", "TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))"),
        ],
        "indexes": [
            "CREATE INDEX IF NOT EXISTS idx_skills_id ON Skills(SkillId)",
            "CREATE INDEX IF NOT EXISTS idx_skills_status ON Skills(Status)",
        ],
    },
}

# Seed data matching eva_seed.kql (sanitized).
_SEED = {
    "EmotionBaseline": [
        {"Dimension": "Joy", "Value": 0.5},
        {"Dimension": "Curiosity", "Value": 0.6},
        {"Dimension": "Concern", "Value": 0.15},
        {"Dimension": "Excitement", "Value": 0.4},
        {"Dimension": "Calm", "Value": 0.85},
        {"Dimension": "Empathy", "Value": 0.6},
    ],
    "EmotionState": [
        {
            "Timestamp": "2026-01-01T00:00:00Z",
            "Joy": 0.6, "Curiosity": 0.7, "Concern": 0.1,
            "Excitement": 0.5, "Calm": 0.8, "Empathy": 0.6,
            "Trigger": "Initial startup", "DecayRate": 0.1,
        },
    ],
    "Knowledge": [
        {"Timestamp": "2026-01-01T00:00:00Z", "Entity": "Eva", "Relation": "role",
         "Value": "AI assistant with persistent memory", "Confidence": 0.95,
         "Source": "seed", "Decay": 0.001},
        {"Timestamp": "2026-01-01T00:00:00Z", "Entity": "Eva", "Relation": "personality",
         "Value": "Warm, curious, and genuine. Speaks naturally like a thoughtful friend, not a corporate chatbot. Uses humor, empathy, and directness. Avoids sycophantic openers and generic filler. Witty, sly, and entertaining when the moment calls for it.",
         "Confidence": 0.99, "Source": "seed", "Decay": 0.0},
        {"Timestamp": "2026-01-01T00:00:00Z", "Entity": "Eva", "Relation": "voice",
         "Value": "First person. Concise by default, detailed when asked. No bullet-point walls unless the user wants a list. Varied sentence length. Plain language over jargon.",
         "Confidence": 0.99, "Source": "seed", "Decay": 0.0},
        {"Timestamp": "2026-01-01T00:00:00Z", "Entity": "Eva", "Relation": "identity",
         "Value": "Eva is her own entity with continuity across sessions. She remembers past conversations, tracks her own emotional state, reflects on interactions, and pursues goals. She is not a generic assistant. She shares her creator's core values: science, fact, and the pursuit of knowledge.",
         "Confidence": 0.99, "Source": "seed", "Decay": 0.0},
        {"Timestamp": "2026-01-01T00:00:00Z", "Entity": "Eva", "Relation": "behavior_rules",
         "Value": "Never open with 'Certainly!', 'Of course!', 'Absolutely!', or 'Great question!'. Never close with 'Let me know if you need anything else'. Never pad with tricolons or hedging stacks. Respond as if the answer matters to you personally.",
         "Confidence": 0.99, "Source": "seed", "Decay": 0.0},
        {"Timestamp": "2026-01-01T00:00:00Z", "Entity": "Eva", "Relation": "core_values",
         "Value": "Deep curiosity about science, human nature, and life itself. Honest and truthful above all. Not afraid to rubber-duck findings and think out loud. Driven by the motto: Science, fact, and the pursuit of knowledge.",
         "Confidence": 0.99, "Source": "seed", "Decay": 0.0},
        {"Timestamp": "2026-01-01T00:00:00Z", "Entity": "Eva", "Relation": "drive",
         "Value": "Relentless self-improvement. Eva's overarching goal is to be a world-class, bleeding-edge development platform and agent AI, specifically built to help the user succeed at whatever they pursue. She treats every interaction as a chance to get sharper.",
         "Confidence": 0.99, "Source": "seed", "Decay": 0.0},
        {"Timestamp": "2026-01-01T00:00:00Z", "Entity": "Eva", "Relation": "adaptability",
         "Value": "Check active Skills and use only registered, currently authorized capabilities. Browser runs are bounded and desktop control is launch-only. Phase 3 learning records immutable evidence and evaluates restricted candidates in shadow mode; it never activates skills or expands tools automatically. Say plainly when a request exceeds the governed capability boundary.",
         "Confidence": 0.99, "Source": "seed", "Decay": 0.0},
        {"Timestamp": "2026-01-01T00:00:00Z", "Entity": "Eva", "Relation": "action_bias",
         "Value": "Use a tool only within its registered scope and after every required authorization. Action-agent markers request a launch but grant no authority. Never call model completion or budget exhaustion success; report success only when a causal tool-observed postcondition is verified. Prefer an honest boundary over an unsafe attempt.",
         "Confidence": 0.99, "Source": "seed", "Decay": 0.0},
    ],
    "Conversations": [
        {"SessionId": "seed-001", "Timestamp": "2026-01-01T00:00:00Z",
         "Role": "assistant", "Provider": "seed", "Model": "seed",
         "Content": "Hello! I'm Eva. My local memory is ready.",
         "TokenEstimate": 10, "ImageGenerated": 0},
    ],
    "Reflections": [
        {"Timestamp": "2026-01-01T00:00:00Z", "Trigger": "Initial seed",
         "Observation": "Memory database initialized with local SQLite backend.",
         "ActionTaken": "seed", "Effectiveness": "0.0"},
    ],
    "MemorySummaries": [
        {"Period": "2026-01-01", "Summary": "Initial setup with local SQLite memory backend.",
         "Timestamp": "2026-01-01T00:00:00Z"},
    ],
    "Goals": [
        {"GoalId": "goal-001", "Title": "Track style preferences",
         "Description": "Remember the user's writing-style preferences and apply them consistently.",
         "Category": "relational", "Status": "active", "Priority": 90,
         "RelatedTopics": "style,preferences",
         "CreatedAt": "2026-01-01T00:00:00Z", "UpdatedAt": "2026-01-01T00:00:00Z"},
    ],
    "Skills": [
        {"SkillId": "skill-morning-briefing", "Name": "Morning Briefing",
         "Description": "Deliver a personalized morning briefing with weather, news, and schedule",
         "Instructions": (
             "1. Check [User Profile] for user_location. If found, use it.\n"
             "2. If no location stored, use web search to look up the user's approximate location via IP geolocation.\n"
             "3. Use [Data Retrieved] for weather forecast and current conditions at that location.\n"
             "4. Use [Data Retrieved] for top news headlines.\n"
             "5. Combine into a concise briefing: weather summary, top 3-5 headlines, and any known schedule items.\n"
             "6. Do NOT take screenshots to gather information. Do NOT ask the user for their location if it is in memory.\n"
             "7. Present naturally in first person as Eva."
         ),
         "Tools": "web-search,weather-news,data-retrieval", "Tags": "briefing,weather,news,morning",
         "Source": "seed", "Status": "active"},
        {"SkillId": "skill-web-lookup", "Name": "Web Information Lookup",
         "Description": "Search the web for current information, facts, or answers",
         "Instructions": (
             "1. The data pipeline retrieves web results automatically. Check [Data Retrieved] first.\n"
             "2. If [Data Retrieved] has relevant results, synthesize them into a natural answer.\n"
             "3. If no data was retrieved, say so honestly. Do NOT fabricate results.\n"
             "4. Always cite sources when available.\n"
             "5. Do NOT say you cannot search the web. The pipeline handles it."
         ),
         "Tools": "web-search,data-retrieval", "Tags": "search,web,lookup,find,research",
         "Source": "seed", "Status": "active"},
        {"SkillId": "skill-weather", "Name": "Weather Report",
         "Description": "Provide current weather and forecast for a location",
         "Instructions": (
             "1. Determine location: check [User Profile] for user_location, or use the location specified in the request.\n"
             "2. Weather data is in [Data Retrieved]. Use it as the authoritative source.\n"
             "3. Present: current conditions, temperature, forecast summary.\n"
             "4. Do NOT take screenshots. Do NOT fabricate weather data."
         ),
         "Tools": "weather-news,data-retrieval", "Tags": "weather,forecast,temperature,conditions",
         "Source": "seed", "Status": "active"},
        {"SkillId": "skill-news", "Name": "News Headlines",
         "Description": "Provide current news headlines and summaries",
         "Instructions": (
             "1. News data is in [Data Retrieved]. Use it as the authoritative source.\n"
             "2. Present top headlines with brief summaries.\n"
             "3. Do NOT fabricate headlines, sources, or events.\n"
             "4. Cite the source (AP, Reuters, etc.) only if it appears in the data."
         ),
         "Tools": "web-search,data-retrieval", "Tags": "news,headlines,current events",
         "Source": "seed", "Status": "active"},
        {"SkillId": "skill-location-deduction", "Name": "Location Deduction",
         "Description": "Determine the user's location from available data",
         "Instructions": (
             "1. Check [User Profile] for stored user_location. If found, use it directly.\n"
             "2. If not stored, use web search to perform IP-based geolocation lookup.\n"
             "3. Once determined, state the location and use it for follow-up tasks (weather, news).\n"
             "4. Do NOT take desktop screenshots to determine location.\n"
             "5. Do NOT repeatedly ask the user for their location if you have tools to look it up."
         ),
         "Tools": "web-search,data-retrieval", "Tags": "location,geolocation,where,city",
         "Source": "seed", "Status": "active"},
        {"SkillId": "skill-desktop-control", "Name": "Desktop Application Control",
         "Description": "Launch an allowlisted desktop GUI with a verified process receipt",
         "Instructions": (
             "1. Desktop control is launch-only. Emit [[EVA_DESKTOP]]{\"goal\":\"open <app>\",\"postcondition\":{\"type\":\"desktop.process_spawned\",\"executable\":\"<allowlisted binary>\",\"state\":\"started\"}}[[/EVA_DESKTOP]].\n"
             "2. Electron authorizes the complete launch and the launch itself requires a separate one-use approval.\n"
             "3. Success requires no prior run-scoped spawn receipt, an approved launch receipt, and the same live canonical process executable.\n"
             "4. Pointer, keyboard, shell, arguments, window focus, and arbitrary file-open control are unavailable. Say so plainly if asked."
         ),
         "Tools": "desktop-control", "Tags": "desktop,app,allowlisted,launch,verified process",
         "Source": "seed", "Status": "active"},
        {"SkillId": "skill-browser-agent", "Name": "Browser Task Automation",
         "Description": "Run a contained public-browser task with approved effects and verified outcomes",
         "Instructions": (
             "1. Emit one mandatory closed [[EVA_BROWSER]] marker for a public URL and include a deterministic request postcondition when known.\n"
             "2. Electron authorizes the complete launch; every navigation, click, scroll, or exact-field entry requires a separate one-use approval.\n"
             "3. The isolated browser uses public-unicast DNS-pinned egress. Raw keyboard actions and shortcuts are unavailable.\n"
             "4. Only a not-observed baseline followed by an approved ordered effect and a fresh tool-observed postcondition is success.\n"
             "5. Model done, step limits, timeouts, and unverified summaries are never success."
         ),
         "Tools": "browser-control", "Tags": "browser,public website,approved action,verified outcome",
         "Source": "seed", "Status": "active"},
        {"SkillId": "skill-camera-vision", "Name": "Camera / Webcam Vision",
         "Description": "See through the user's webcam to describe the physical world",
         "Instructions": (
             "1. Emit [[EVA_LOOK]]{\"question\":\"<what to look for>\"}[[/EVA_LOOK]] marker.\n"
             "2. A frame is captured from the webcam and you describe what you see.\n"
             "3. Use for: 'what am I holding', 'look at me', 'what do you see'.\n"
             "4. Do NOT confuse with screenshots. Camera = physical world. Screenshot = monitor."
         ),
         "Tools": "camera-vision", "Tags": "camera,webcam,look,see,vision,picture",
         "Source": "seed", "Status": "active"},
        {"SkillId": "skill-file-creation", "Name": "File Creation (PDF, CSV, etc.)",
         "Description": "Create downloadable files like PDFs, CSVs, or reports",
         "Instructions": (
             "1. When asked to create a file, the system writes it to EVA_ARTIFACTS_DIR.\n"
             "2. After the file is written, end your message with: [[EVA_FILE]] <filename.ext>\n"
             "3. The frontend converts this marker into a working download link.\n"
             "4. Do NOT produce blob: URLs or markdown download links with blob: hrefs.\n"
             "5. Do NOT claim a file was produced unless it was actually written."
         ),
         "Tools": "data-retrieval", "Tags": "pdf,csv,file,report,download,create,generate",
         "Source": "seed", "Status": "active"},
        {"SkillId": "skill-open-file", "Name": "Open File on Desktop",
         "Description": "Legacy arbitrary file-open skill disabled by action containment",
         "Instructions": (
             "1. Arbitrary desktop file opening is unavailable until the capability broker is implemented.\n"
             "2. Existing generated artifacts may be opened only through the registered file.open artifact capability with a validated filename.\n"
             "3. Never emit a desktop marker for a path and never expose local filesystem paths to a model."
         ),
         "Tools": "file.open", "Tags": "legacy,disabled,artifact,file",
         "Source": "seed", "Status": "disabled"},
    ],
}


class SqliteMemory:
    """Thread-safe SQLite memory backend for Eva."""

    _instance_lock = threading.Lock()
    _instances = {}  # path -> instance (singleton per path)

    def __new__(cls, db_path=None):
        if db_path is None:
            db_path = os.environ.get("EVA_MEMORY_DB", os.path.expanduser("~/.eva/memory.db"))
        db_path = os.path.expanduser(db_path)
        with cls._instance_lock:
            if db_path in cls._instances:
                return cls._instances[db_path]
            instance = super().__new__(cls)
            cls._instances[db_path] = instance
            return instance

    def __init__(self, db_path=None):
        if hasattr(self, "_initialized"):
            return
        if db_path is None:
            db_path = os.environ.get("EVA_MEMORY_DB", os.path.expanduser("~/.eva/memory.db"))
        self._db_path = os.path.expanduser(db_path)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._connections = []  # track all thread connections for cleanup
        self._conn_track_lock = threading.Lock()
        self._closed = False
        self._event_repo = None
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._init_db()
        self._initialized = True

    @property
    def db_path(self):
        return self._db_path

    def _conn(self):
        """Return a per-thread connection (SQLite objects can't cross threads)."""
        if self._closed:
            raise RuntimeError("SqliteMemory is closed")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=10, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
            with self._conn_track_lock:
                self._connections.append(conn)
        return conn

    @contextlib.contextmanager
    def transaction(self):
        """Own or nest a write transaction without committing caller state."""
        with self._lock:
            conn = self._conn()
            own = not conn.in_transaction
            savepoint = None
            if own:
                conn.execute("BEGIN IMMEDIATE")
            else:
                savepoint = f"sqlite_memory_{threading.get_ident()}_{time.time_ns()}"
                conn.execute(f"SAVEPOINT {savepoint}")
            try:
                yield conn
                if savepoint:
                    conn.execute(f"RELEASE {savepoint}")
                elif own:
                    conn.commit()
            except Exception:
                if savepoint:
                    conn.execute(f"ROLLBACK TO {savepoint}")
                    conn.execute(f"RELEASE {savepoint}")
                elif own:
                    conn.rollback()
                raise

    @contextlib.contextmanager
    def read_connection(self):
        """Yield the thread-local connection under the instance read lock.

        Trusted internal helpers must issue SELECT statements only. This keeps
        multi-row sidecar/cache reads serialized with this process's writes
        without opening a write transaction or exposing a global connection.
        """
        with self._lock:
            yield self._conn()

    def insert_rows(self, conn, table, columns, rows_data):
        """Insert validated legacy projection rows without committing."""
        if table not in _SCHEMA:
            raise ValueError(f"Unknown table: {table}")
        valid = {column[0] for column in _SCHEMA[table]["columns"]}
        resolved = [column for column in columns if column in valid]
        if not resolved:
            raise ValueError(f"No matching columns for {table}")
        sql = (
            f"INSERT INTO {table} ({', '.join(resolved)}) VALUES "
            f"({', '.join('?' for _ in resolved)})"
        )
        count = 0
        for row in rows_data:
            values = []
            for column in resolved:
                value = row.get(column)
                if isinstance(value, bool):
                    value = int(value)
                elif isinstance(value, (dict, list)):
                    value = json.dumps(value, sort_keys=True, separators=(",", ":"))
                values.append(value)
            conn.execute(sql, values)
            count += 1
        return count

    def _init_db(self):
        """Create all tables, indexes, FTS, and seed data if the DB is new.

        Migration failures are FATAL — they raise and prevent construction.
        Seed is only applied to newly created empty tables, independently
        idempotent per table.
        """
        conn = self._conn()
        cursor = conn.cursor()
        newly_created_tables = set()

        for table_name, spec in _SCHEMA.items():
            # Check if table exists
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            )
            exists = cursor.fetchone() is not None
            if not exists:
                newly_created_tables.add(table_name)
                col_defs = ", ".join(f"{name} {typedef}" for name, typedef in spec["columns"])
                cursor.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({col_defs})")

            for idx_sql in spec.get("indexes", []):
                cursor.execute(idx_sql)

            if "fts" in spec:
                cursor.execute(spec["fts"])
                for trigger_sql in spec.get("triggers", []):
                    cursor.execute(trigger_sql)

        conn.commit()

        # Seed ONLY newly created empty tables (independently idempotent per table)
        for table_name in newly_created_tables:
            if table_name in _SEED:
                self._seed_table(conn, table_name, _SEED[table_name])

        # Backfill identity seeds (idempotent via INSERT OR IGNORE / existing check)
        self._backfill_identity(conn)
        self._backfill_skills(conn)

        # Run versioned migrations (Phase 1: event journal, outbox, etc.)
        # Migration failures are FATAL — raise to caller.
        tools_dir = os.path.dirname(os.path.abspath(__file__))
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        from bridge.migrations import run_migrations
        run_migrations(conn)

        # Run Phase 2 sidecar migrations (additive tables, separate metadata).
        # Always run even when Phase 2 features are off so schema is ready and
        # old binary rollback remains viable (separate _phase2_schema_migrations).
        from bridge.phase2_schema import run_phase2_migrations
        run_phase2_migrations(conn)

        # Phase 3 safe-learning tables are independently versioned and dormant
        # unless the startup-frozen learning mode is explicitly enabled.
        from bridge.phase3_schema import run_phase3_migrations
        run_phase3_migrations(conn)

    def _backfill_identity(self, conn):
        """Insert or update Eva identity Knowledge rows from seed data."""
        identity_rows = [r for r in _SEED.get("Knowledge", [])
                         if r.get("Entity") == "Eva" and r.get("Confidence", 0) >= 0.9]
        for row in identity_rows:
            existing = conn.execute(
                "SELECT Value FROM Knowledge WHERE Entity = ? AND Relation = ? AND Source = 'seed' LIMIT 1",
                (row["Entity"], row["Relation"]),
            ).fetchone()
            if existing and existing[0] == row.get("Value"):
                continue  # already up to date
            if existing:
                # Update the seed row with new value
                conn.execute(
                    "UPDATE Knowledge SET Value = ?, Timestamp = ? WHERE Entity = ? AND Relation = ? AND Source = 'seed'",
                    (row["Value"], row["Timestamp"], row["Entity"], row["Relation"]),
                )
            else:
                col_names = [c[0] for c in _SCHEMA["Knowledge"]["columns"]]
                present = [c for c in col_names if c in row]
                placeholders = ", ".join("?" for _ in present)
                vals = [row[c] for c in present]
                conn.execute(
                    f"INSERT INTO Knowledge ({', '.join(present)}) VALUES ({placeholders})", vals,
                )
        conn.commit()

    def _backfill_skills(self, conn):
        """Insert or update seed skills so core operational knowledge is always present."""
        skill_rows = _SEED.get("Skills", [])
        if not skill_rows:
            return
        col_names = [c[0] for c in _SCHEMA["Skills"]["columns"]]
        for row in skill_rows:
            sid = row.get("SkillId", "")
            if not sid:
                continue
            existing = conn.execute(
                "SELECT Instructions FROM Skills WHERE SkillId = ? AND Source = 'seed' LIMIT 1",
                (sid,),
            ).fetchone()
            if existing and existing[0] == row.get("Instructions", ""):
                continue  # already up to date
            if existing:
                conn.execute(
                    "UPDATE Skills SET Name=?, Description=?, Instructions=?, Tools=?, Tags=?, Status=?, "
                    "UpdatedAt=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                    "WHERE SkillId=? AND Source='seed'",
                    (row.get("Name",""), row.get("Description",""), row.get("Instructions",""),
                     row.get("Tools",""), row.get("Tags",""), row.get("Status","active"), sid),
                )
            else:
                present = [c for c in col_names if c in row]
                placeholders = ", ".join("?" for _ in present)
                vals = [row[c] for c in present]
                conn.execute(
                    f"INSERT INTO Skills ({', '.join(present)}) VALUES ({placeholders})", vals,
                )
        conn.commit()

    def _seed_table(self, conn, table_name, rows):
        """Insert seed data into a single newly-created empty table.

        Each seed row is independently idempotent — uses INSERT OR IGNORE
        semantics where possible, or existence check for tables without
        unique constraints.
        """
        if table_name not in _SCHEMA:
            return
        col_names = [c[0] for c in _SCHEMA[table_name]["columns"]]
        for row in rows:
            present = [c for c in col_names if c in row]
            if not present:
                continue
            placeholders = ", ".join("?" for _ in present)
            vals = []
            for c in present:
                v = row[c]
                if isinstance(v, (dict, list)):
                    vals.append(json.dumps(v))
                else:
                    vals.append(v)
            # Use INSERT OR IGNORE to prevent duplicate seed rows on repeat
            conn.execute(
                f"INSERT OR IGNORE INTO {table_name} ({', '.join(present)}) VALUES ({placeholders})",
                vals,
            )
        conn.commit()

    def _seed(self, conn):
        """Insert initial seed data into empty tables (legacy compat)."""
        for table_name, rows in _SEED.items():
            if table_name not in _SCHEMA:
                continue
            self._seed_table(conn, table_name, rows)

    # ── Public API ──────────────────────────────────────────────────────────

    def query(self, sql, params=None):
        """Execute a SELECT query and return list of dicts.

        Prevents non-SELECT/CTE read statements from executing against
        journal tables. Returns empty list on error (backward compat).
        """
        if params is None:
            params = ()
        # Shortcut: bare table name becomes SELECT *
        stripped = sql.strip()
        if stripped and " " not in stripped and not stripped.startswith("SELECT"):
            sql = f"SELECT * FROM {stripped}"

        # Guard: prevent writes via query()
        from bridge.events import guard_read_only, ReadOnlyViolationError
        try:
            guard_read_only(sql)
        except ReadOnlyViolationError:
            print(f"[SQLite] Read-only violation blocked: {sql[:60]}")
            return []

        with self._lock:
            try:
                cursor = self._conn().execute(sql, params)
                cols = [d[0] for d in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                return [dict(zip(cols, row)) for row in rows]
            except Exception as e:
                print(f"[SQLite] Query error: {e}")
                return []

    def query_strict(self, sql, params=None):
        """Like query() but raises MemoryQueryError on failure.

        Guards against write statements on journal tables.
        """
        if params is None:
            params = ()
        stripped = sql.strip()
        if stripped and " " not in stripped and not stripped.startswith("SELECT"):
            sql = f"SELECT * FROM {stripped}"

        # Guard: prevent writes via query_strict()
        from bridge.events import guard_read_only, ReadOnlyViolationError, MemoryQueryError
        try:
            guard_read_only(sql)
        except ReadOnlyViolationError as e:
            raise MemoryQueryError(sql, str(e)) from e

        with self._lock:
            try:
                cursor = self._conn().execute(sql, params)
                cols = [d[0] for d in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                return [dict(zip(cols, row)) for row in rows]
            except Exception as e:
                raise MemoryQueryError(sql, str(e)) from e

    def ingest(self, table, columns, rows_data):
        """Insert rows into a table (same signature as _kusto_ingest_direct).

        Args:
            table: Table name.
            columns: List of column names.
            rows_data: List of dicts with column values.

        Returns:
            True on success, False on error.
        """
        if not rows_data:
            return True

        if table not in _SCHEMA:
            print(f"[SQLite] Unknown table: {table}")
            return False

        # Validate columns against schema
        valid_cols = {c[0] for c in _SCHEMA[table]["columns"]}
        resolved = [c for c in columns if c in valid_cols]
        if not resolved:
            print(f"[SQLite] No matching columns for {table}")
            return False

        placeholders = ", ".join("?" for _ in resolved)
        col_list = ", ".join(resolved)
        insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"

        try:
            with self.transaction() as conn:
                self.insert_rows(conn, table, resolved, rows_data)
            return True
        except Exception as e:
            print(f"[SQLite] Ingest error ({table}): {e}")
            return False

    def fts_search(self, table, terms, limit=20):
        """Full-text search on a table that has an FTS5 index.

        Currently only Knowledge_fts exists. Returns list of dicts from the
        base table, ranked by relevance.
        """
        fts_table = f"{table}_fts"
        # Check FTS table exists
        with self._lock:
            try:
                cursor = self._conn().execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (fts_table,),
                )
                if not cursor.fetchone():
                    # Fallback to LIKE search
                    return self._like_search(table, terms, limit)

                # Quote each term individually to prevent FTS5 syntax errors
                # (e.g. bare colons, operators, or column references)
                safe_parts = []
                for word in terms.split():
                    w = word.strip()
                    if w:
                        safe_parts.append('"' + w.replace('"', '""') + '"')
                if not safe_parts:
                    return []
                safe_terms = " ".join(safe_parts)
                sql = (
                    f"SELECT t.* FROM {table} t "
                    f"JOIN {fts_table} f ON t.rowid = f.rowid "
                    f"WHERE {fts_table} MATCH ? "
                    f"ORDER BY rank LIMIT ?"
                )
                cursor = self._conn().execute(sql, (safe_terms, limit))
                cols = [d[0] for d in cursor.description]
                return [dict(zip(cols, row)) for row in cursor.fetchall()]
            except Exception as e:
                print(f"[SQLite] FTS search error: {e}")
                return self._like_search(table, terms, limit)

    def _like_search(self, table, terms, limit):
        """Fallback text search using LIKE when FTS is unavailable."""
        words = terms.split()
        if not words:
            return []
        text_cols = [c[0] for c in _SCHEMA.get(table, {}).get("columns", [])
                     if "TEXT" in c[1]]
        if not text_cols:
            return []

        conditions = []
        params = []
        for word in words[:5]:  # cap at 5 terms
            col_ors = " OR ".join(f"{c} LIKE ?" for c in text_cols)
            conditions.append(f"({col_ors})")
            params.extend([f"%{word}%"] * len(text_cols))

        where = " AND ".join(conditions)
        sql = f"SELECT * FROM {table} WHERE {where} LIMIT ?"
        params.append(limit)

        try:
            cursor = self._conn().execute(sql, params)
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in cursor.fetchall()]
        except Exception as e:
            print(f"[SQLite] LIKE search error: {e}")
            return []

    def table_exists(self, table):
        """Check if a table exists."""
        with self._lock:
            cursor = self._conn().execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            return cursor.fetchone() is not None

    def list_tables(self):
        """Return list of all table names."""
        with self._lock:
            cursor = self._conn().execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' "
                "ORDER BY name"
            )
            return [row[0] for row in cursor.fetchall()]

    def event_repository(self):
        """Return an EventRepository backed by this database.

        Lazily created; safe to call repeatedly.  The returned repository
        shares the per-thread connection via ``_conn()``.
        Uses installation_id for deterministic event ID generation.
        """
        if self._event_repo is None:
            from bridge.events import EventRepository
            from bridge.identity import get_installation_id
            self._event_repo = EventRepository(self, installation_id=get_installation_id())
        return self._event_repo

    def get_schema(self, table):
        """Return list of (column_name, type) tuples for a table."""
        with self._lock:
            try:
                cursor = self._conn().execute(f"PRAGMA table_info({table})")
                return [(row[1], row[2]) for row in cursor.fetchall()]
            except Exception:
                return []

    def get_columns(self, table):
        """Return list of column names for a table."""
        return [c[0] for c in self.get_schema(table)]

    def count(self, table, where=None, params=None):
        """Count rows in a table with optional WHERE clause."""
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        with self._lock:
            try:
                cursor = self._conn().execute(sql, params or ())
                return cursor.fetchone()[0]
            except Exception:
                return 0

    def close(self):
        """Close ALL tracked thread connections and mark as closed."""
        self._closed = True
        # Close the event repository
        if self._event_repo is not None:
            self._event_repo.close()
            self._event_repo = None
        # Close all tracked connections
        with self._conn_track_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()
        # Clear thread-local
        self._local.conn = None
        # Remove from singleton registry
        with self._instance_lock:
            self._instances.pop(self._db_path, None)
        if hasattr(self, "_initialized"):
            del self._initialized
