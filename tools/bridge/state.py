"""Mutable runtime state for the Eva ACP Bridge.

Every module in the bridge package reads and writes shared state through
this module. Import as ``from bridge import state`` and access attributes
directly: ``state.acp_client``, ``state.cognition_enabled = True``, etc.

Thread-safety: locks are defined here alongside the data they protect.
Callers must acquire the relevant lock before mutating guarded state.
"""

import os
import threading

from bridge.config import (
    DEFAULT_ALERT_SETTINGS,
    MODE_PREF_PATH,
    env_truthy,
)

# ── ACP client pool ────────────────────────────────────────────────
acp_client = None           # Global ACP client instance (most-recently-used)
acp_pool = {}               # model_key -> ACPClient
acp_pool_order = []         # model_key list, LRU first
acp_pool_lock = threading.RLock()
configured_mcp_config = {}  # full sanitized runtime selection; clients receive profiles

# ── Kusto auth ──────────────────────────────────────────────────────
kusto_token_cache = None    # Cached Kusto access token
kusto_credential = None     # Cached credential object for token refresh
kusto_table_columns_cache = {}  # (cluster, db, table) -> [columns]
kusto_metadata_cache = {}        # (cluster, db, kind, query) -> (expires, value)
kusto_metadata_cache_lock = threading.RLock()
kusto_database_locked = env_truthy("KUSTO_DATABASE_LOCKED") or env_truthy("EVA_KUSTO_LOCKED")
active_kusto_db = os.environ.get("KUSTO_DATABASE", "").strip()
active_kusto_cluster = os.environ.get("KUSTO_CLUSTER_URL", "").strip()

# ── Cognition ───────────────────────────────────────────────────────
cognition_enabled = False
cognition_launch_iso = None
cognition_launch_id = None
session_exchange_count = 0
session_conversation_buffer = []  # (user, assistant) pairs
cognition_candidate_counts = {}   # lowercased entity -> mention count
candidate_history_cache = {}      # entity_lower -> (ts, mentions, max_conf)
last_interaction_date = None

# ── Memory backend ──────────────────────────────────────────────────
memory_backend = os.environ.get("EVA_MEMORY_BACKEND", "").strip().lower() or None
sqlite_mem = None           # SqliteMemory instance (lazy)
openai_api_key_cache = ""
embedding_cache = None      # lazy dict: sha1(text) -> [floats]
embedding_cache_lock = threading.Lock()
embedding_disabled_logged = False

# ── Protected memory ──────────────────────────────────────────────
protected_vault = None
protected_vault_lock = threading.RLock()
protected_memory_model_release = False

# ── Background loop ────────────────────────────────────────────────
bg_loop_thread = None
bg_loop_stop = threading.Event()
bg_loop_enabled = True
bg_loop_interval_seconds = 7200
bg_last_tick_iso = ""
bg_last_error = ""
bg_last_activity = {}
last_user_activity_ts = 0.0
bg_tick_lock = threading.Lock()

# ── Bridge networking ───────────────────────────────────────────────
bridge_bind_address = "127.0.0.1"

# ── Cron ────────────────────────────────────────────────────────────
cron_tasks = []
cron_lock = threading.Lock()

# ── Subagent ────────────────────────────────────────────────────────
subagent_tasks = {}
subagent_lock = threading.Lock()

# ── Durable coding workspaces ───────────────────────────────────────
workspace_store = None
workspace_lock = threading.RLock()
workspace_acp_clients = {}  # task_id -> live workspace-scoped ACPClient

# ── Telemetry ───────────────────────────────────────────────────────
telemetry_enabled = os.environ.get("EVA_TELEMETRY", "1") not in ("0", "false", "no")
telemetry_lock = threading.Lock()
telemetry_ring = []

# ── Log ring ────────────────────────────────────────────────────────
log_lock = threading.Lock()
log_ring = []
log_seq = 0

# ── Alerts / notifications ─────────────────────────────────────────
alerts_lock = threading.RLock()
notify_lock = threading.Lock()
notify_ring = []

# ── Local MCP (no-cloud mode) ──────────────────────────────────────
local_mcp_manager = None    # LocalMCPManager instance (lazy)
# Restore persisted mode preference (local vs cloud).
_mode_pref_path = MODE_PREF_PATH
try:
    if os.path.isfile(_mode_pref_path):
        with open(_mode_pref_path, encoding="utf-8") as handle:
            _saved_mode = handle.read().strip().lower()
    else:
        _saved_mode = ""
except OSError:
    _saved_mode = ""
local_mode = (_saved_mode == "local")

