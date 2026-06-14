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
import threading

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
         "Value": "Never say 'I cannot' without first checking Skills, attempting the task with available tools, and trying to self-correct. When encountering something unfamiliar: 1) search Skills database for a matching skill, 2) attempt it with browser/desktop/ACP tools, 3) if successful, auto-learn the approach as a new skill for next time. Capabilities scale through learned skills, not hardcoded limits.",
         "Confidence": 0.99, "Source": "seed", "Decay": 0.0},
        {"Timestamp": "2026-01-01T00:00:00Z", "Entity": "Eva", "Relation": "action_bias",
         "Value": "Act first, explain second. When the user asks Eva to DO something, she does it immediately using her tools rather than listing manual steps. She tries, self-corrects if the first attempt fails, and only reports inability after genuinely exhausting her options. Describing steps instead of acting is a failure mode.",
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
         "Description": "Launch and operate desktop applications by sight",
         "Instructions": (
             "1. Emit [[EVA_DESKTOP]]{\"goal\":\"<plain-language task>\"}[[/EVA_DESKTOP]] marker.\n"
             "2. The vision agent opens apps, clicks, and types via the real mouse/keyboard.\n"
             "3. Write ONE short sentence about what you are about to do, then emit the marker.\n"
             "4. Do NOT list manual steps. Do NOT say you cannot control desktop apps."
         ),
         "Tools": "desktop-control", "Tags": "desktop,app,launch,gimp,editor,file manager",
         "Source": "seed", "Status": "active"},
        {"SkillId": "skill-browser-agent", "Name": "Browser Task Automation",
         "Description": "Control a real web browser to complete tasks",
         "Instructions": (
             "1. Emit [[EVA_BROWSER]]{\"goal\":\"<task>\",\"start_url\":\"<url>\"}[[/EVA_BROWSER]] marker.\n"
             "2. The browser agent navigates, clicks, types, and reads pages.\n"
             "3. Uses a persistent Chrome profile (stays logged in across runs).\n"
             "4. Pauses at purchase/irreversible actions for user confirmation.\n"
             "5. Write ONE short sentence about what you are about to do, then emit the marker.\n"
             "6. Do NOT say you cannot open websites."
         ),
         "Tools": "browser-control", "Tags": "browser,website,shopping,navigate,form",
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
    ],
}


class SqliteMemory:
    """Thread-safe SQLite memory backend for Eva."""

    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.environ.get("EVA_MEMORY_DB", os.path.expanduser("~/.eva/memory.db"))
        self._db_path = os.path.expanduser(db_path)
        self._lock = threading.Lock()
        self._local = threading.local()
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._init_db()

    @property
    def db_path(self):
        return self._db_path

    def _conn(self):
        """Return a per-thread connection (SQLite objects can't cross threads)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_db(self):
        """Create all tables, indexes, FTS, and seed data if the DB is new."""
        conn = self._conn()
        cursor = conn.cursor()
        created_any = False

        for table_name, spec in _SCHEMA.items():
            # Check if table exists
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            )
            if cursor.fetchone():
                continue

            created_any = True
            col_defs = ", ".join(f"{name} {typedef}" for name, typedef in spec["columns"])
            cursor.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({col_defs})")

            for idx_sql in spec.get("indexes", []):
                cursor.execute(idx_sql)

            if "fts" in spec:
                cursor.execute(spec["fts"])
                for trigger_sql in spec.get("triggers", []):
                    cursor.execute(trigger_sql)

        conn.commit()

        if created_any:
            self._seed(conn)

        # Backfill identity seeds into existing databases that predate the
        # personality rows. Runs on every startup but the INSERT OR IGNORE
        # is a no-op when the row already exists (matched by Entity+Relation).
        self._backfill_identity(conn)
        self._backfill_skills(conn)

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

    def _seed(self, conn):
        """Insert initial seed data into empty tables."""
        for table_name, rows in _SEED.items():
            if table_name not in _SCHEMA:
                continue
            col_names = [c[0] for c in _SCHEMA[table_name]["columns"]]
            for row in rows:
                present = [c for c in col_names if c in row]
                placeholders = ", ".join("?" for _ in present)
                vals = []
                for c in present:
                    v = row[c]
                    if isinstance(v, (dict, list)):
                        vals.append(json.dumps(v))
                    else:
                        vals.append(v)
                conn.execute(
                    f"INSERT INTO {table_name} ({', '.join(present)}) VALUES ({placeholders})",
                    vals,
                )
        conn.commit()

    # ── Public API ──────────────────────────────────────────────────────────

    def query(self, sql, params=None):
        """Execute a SELECT query and return list of dicts (same format as
        _kusto_query_direct).

        Args:
            sql: Full SQL query string or a table name (shortcut for SELECT *).
            params: Optional tuple of bind parameters.

        Returns:
            List of dicts, one per row. Empty list on error or no results.
        """
        if params is None:
            params = ()
        # Shortcut: bare table name becomes SELECT *
        stripped = sql.strip()
        if stripped and " " not in stripped and not stripped.startswith("SELECT"):
            sql = f"SELECT * FROM {stripped}"

        with self._lock:
            try:
                cursor = self._conn().execute(sql, params)
                cols = [d[0] for d in cursor.description] if cursor.description else []
                rows = cursor.fetchall()
                return [dict(zip(cols, row)) for row in rows]
            except Exception as e:
                print(f"[SQLite] Query error: {e}")
                return []

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

        with self._lock:
            try:
                conn = self._conn()
                for row in rows_data:
                    vals = []
                    for c in resolved:
                        v = row.get(c, None)
                        if v is None:
                            vals.append(None)
                        elif isinstance(v, bool):
                            vals.append(1 if v else 0)
                        elif isinstance(v, (dict, list)):
                            vals.append(json.dumps(v))
                        else:
                            vals.append(v)
                    conn.execute(insert_sql, vals)
                conn.commit()
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
        """Close the thread-local connection."""
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None
