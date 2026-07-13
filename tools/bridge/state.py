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
    artifact_namespace_blocked as load_artifact_namespace_blocked,
    env_truthy,
    load_runtime_state_document_status,
)

# ── ACP client pool ────────────────────────────────────────────────
acp_client = None           # Global ACP client instance (most-recently-used)
acp_copilot_path = "copilot"
acp_cwd = os.getcwd()
acp_model = None
mcp_github_pat = ""
acp_pool = {}               # model_key -> ACPClient
acp_pool_order = []         # model_key list, LRU first
acp_pool_lock = threading.RLock()

# ── Kusto auth ──────────────────────────────────────────────────────
kusto_token_cache = None    # Cached Kusto access token
kusto_credential = None     # Cached credential object for token refresh
kusto_table_columns_cache = {}  # (cluster, db, table) -> [columns]
kusto_database_locked = env_truthy("KUSTO_DATABASE_LOCKED") or env_truthy("EVA_KUSTO_LOCKED")
active_kusto_db = os.environ.get("KUSTO_DATABASE", "").strip()
active_kusto_cluster = os.environ.get("KUSTO_CLUSTER_URL", "").strip()

# ── Cognition ───────────────────────────────────────────────────────
cognition_enabled = False
cognition_initialization_lock = threading.RLock()
cognition_launch_iso = None
cognition_launch_id = None
session_exchange_count = 0
session_conversation_buffer = []  # (user, assistant) pairs
cognition_candidate_counts = {}   # lowercased entity -> mention count
candidate_history_cache = {}      # entity_lower -> (ts, mentions, max_conf)
last_interaction_date = None

# ── Memory backend ──────────────────────────────────────────────────
memory_backend = os.environ.get("EVA_MEMORY_BACKEND", "").strip().lower() or None
memory_backend_lock = threading.RLock()
sqlite_mem = None           # SqliteMemory instance (lazy)
sqlite_mem_lock = threading.Lock()
openai_api_key_cache = ""
embedding_cache = None      # lazy dict: sha1(text) -> [floats]
embedding_cache_lock = threading.Lock()
embedding_disabled_logged = False

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

# ── One-shot camera capture authority ──────────────────────────────
camera_capture_lock = threading.RLock()
camera_captures = {}

# ── Immutable artifacts ────────────────────────────────────────────
artifact_lock = threading.RLock()
artifact_generation = "0"
artifact_turn_counts = {}
artifact_namespace_blocked = load_artifact_namespace_blocked()

# ── Local MCP (no-cloud mode) ──────────────────────────────────────
local_mcp_manager = None    # LocalMCPManager instance (lazy)
mode_mcp_transition_lock = threading.RLock()
mode_mcp_generation = 0
provider_leases = {}
# Restore mode only from the same exact document that owns the MCP selection.
_runtime_state_status, _runtime_state = load_runtime_state_document_status()
runtime_state_invalid = _runtime_state_status == "invalid"
_saved_mode = _runtime_state["mode"] if _runtime_state is not None else ""
local_mode = (_saved_mode == "local") or runtime_state_invalid
local_mode_state = (
    "invalid" if runtime_state_invalid
    else "selected" if local_mode
    else "inactive"
)

# ── Per-launch auth ────────────────────────────────────────────────
bridge_auth_token = ""      # Set at startup; empty = auth disabled
launch_capability_secret = ""  # Separate Electron-only launch authority

# ── Proposal review lock ───────────────────────────────────────────
proposal_review_lock = threading.Lock()
proposal_transition_lock = threading.Lock()
proposal_last_transition_at = None

# ── Egress mode ────────────────────────────────────────────────────
_egress_mode_raw = os.environ.get("EVA_EGRESS_MODE", "").strip().lower()
egress_mode_invalid = bool(_egress_mode_raw and _egress_mode_raw not in ("offline", "local-network", "cloud"))
egress_mode = _egress_mode_raw or "cloud"
if egress_mode_invalid:
    egress_mode = "cloud"

