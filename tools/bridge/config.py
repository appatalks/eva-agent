"""Immutable configuration constants for the Eva ACP Bridge.

This module centralizes path definitions, tuning thresholds, column
schemas, and other values that do not change at runtime. Mutable
state (token caches, flags, buffers) remains in ``core.py`` until
a future phase extracts it into ``state.py``.
"""

import datetime
import os
import re


def env_truthy(name):
    """Return True when an environment flag uses the shared truthy form."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def utc_now():
    """Current UTC datetime (timezone-aware)."""
    return datetime.datetime.now(datetime.timezone.utc)


def to_utc_iso(value):
    """Convert a datetime (or None) to a UTC ISO-8601 string."""
    if isinstance(value, datetime.datetime):
        active_value = value
    else:
        active_value = utc_now()
    if active_value.tzinfo is None:
        active_value = active_value.replace(tzinfo=datetime.timezone.utc)
    return active_value.astimezone(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Filesystem paths ────────────────────────────────────────────────
EVA_CONFIG_DIR = os.path.expanduser("~/.config/eva-standalone")
ARTIFACTS_DIR = os.path.join(EVA_CONFIG_DIR, "artifacts")
KUSTO_CLUSTER_CACHE_PATH = os.path.join(EVA_CONFIG_DIR, "kusto_cluster.txt")
MCP_CONFIG_CACHE_PATH = os.path.join(EVA_CONFIG_DIR, "mcp_config.json")
ALERTS_CONFIG_PATH = os.path.join(EVA_CONFIG_DIR, "alerts.json")
NOTIFY_PATH = os.path.join(EVA_CONFIG_DIR, "notifications.jsonl")
EMBEDDING_CACHE_PATH = os.path.join(EVA_CONFIG_DIR, "embeddings_cache.json")
MEMORY_BACKEND_PREF_PATH = os.path.join(EVA_CONFIG_DIR, "memory_backend.txt")
TELEMETRY_PATH = os.path.join(EVA_CONFIG_DIR, "telemetry.jsonl")

# ── Networking / validation ─────────────────────────────────────────
LMSTUDIO_ALLOWED_PORTS = {1234, 8000, 8080, 11434}
HTTP_CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")

# ── ACP pool ────────────────────────────────────────────────────────
ACP_POOL_MAX = 4

# ── Cognition tuning ───────────────────────────────────────────────
CANDIDATE_HISTORY_TTL_SECONDS = 60
CONVO_CONTENT_CAP = 8000
EMBEDDING_MODEL = "text-embedding-3-small"
SEMANTIC_MIN_SCORE = 0.30
SEMANTIC_POOL_SIZE = 150

# ── Memory tables ───────────────────────────────────────────────────
MEMORY_TABLES = [
    "Knowledge", "Conversations", "EmotionState", "MemorySummaries",
    "Reflections", "Goals", "SelfState", "HeuristicsIndex",
    "EmotionBaseline", "BackgroundProposals", "BackgroundActivity", "Skills",
]

# ── Goals ───────────────────────────────────────────────────────────
GOAL_CATEGORIES = {"self_improvement", "knowledge_curation", "relational"}
GOAL_STATUSES = {"active", "paused", "done", "dropped"}
GOAL_COLUMNS = [
    "GoalId", "Title", "Description", "Category", "Status",
    "Priority", "RelatedTopics", "CreatedAt", "UpdatedAt",
]
GOALS_LATEST_QUERY = (
    "Goals | summarize arg_max(UpdatedAt, *) by GoalId "
    "| project GoalId, Title, Description, Category, Status, Priority, "
    "RelatedTopics, CreatedAt, UpdatedAt"
)

# ── Skills ──────────────────────────────────────────────────────────
SKILL_STATUSES = {"active", "disabled", "deleted"}
SKILL_COLUMNS = [
    "SkillId", "Name", "Description", "Instructions", "Tools",
    "Tags", "Source", "Status", "CreatedAt", "UpdatedAt",
]
SKILLS_LATEST_QUERY = (
    "Skills | summarize arg_max(UpdatedAt, *) by SkillId "
    "| project SkillId, Name, Description, Instructions, Tools, Tags, "
    "Source, Status, CreatedAt, UpdatedAt"
)
SKILL_SOURCE_MAX_BYTES = 200 * 1024
SKILL_INSTRUCTIONS_INJECT_CAP = 1500
SKILL_INJECT_MAX = 2

# ── Background jobs ─────────────────────────────────────────────────
BG_JOB_TYPE = "memory_consolidation"
BG_TARGET_TABLE = "MemorySummaries"
BG_JOB_GOAL_CHECKIN = "goal_checkin"
BG_JOB_DAILY_DIGEST = "daily_digest"
BG_JOB_KNOWLEDGE_HYGIENE = "knowledge_hygiene"
BG_JOB_REFLECTION_SYNTHESIS = "reflection_synthesis"
BG_JOB_EMOTION_DRIFT = "emotion_drift"
BG_JOB_TOKEN_TELEMETRY = "token_telemetry"
BG_JOB_PROACTIVE_BRIEFING = "proactive_briefing"
BG_JOB_MARKET_SNAPSHOT = "market_snapshot"
BG_JOB_SEC_FILINGS = "sec_filing_watch"
BG_JOB_SPACE_WEATHER = "space_weather_alert"
BG_JOB_RESEARCH_DEEPDIVE = "research_deepdive"
BG_JOB_ALERT_WATCH = "alert_watch"
BG_APPLY_TABLES = {"MemorySummaries", "Reflections"}
GOAL_STALE_DAYS = 3
GOAL_CHECKIN_MAX = 2
KNOWLEDGE_STALE_CONFIDENCE = 0.3
EMOTION_DRIFT_THRESHOLD = 0.15
REFLECTION_SYNTH_MIN = 3
SEC_WATCH_SYMBOLS = ["PLG", "PKX"]

# ── Background proposals ───────────────────────────────────────────
BG_PROPOSAL_STATUSES = {"pending", "approved", "rejected", "applying", "applied", "failed"}
BG_PROPOSAL_COLUMNS = [
    "ProposalId", "CreatedAt", "JobType", "TargetTable", "Payload",
    "Status", "SourceWindowStart", "SourceWindowEnd", "Notes",
    "ReviewedAt", "ReviewedBy",
]
BG_ACTIVITY_COLUMNS = [
    "TickId", "StartedAt", "EndedAt", "JobType", "Status",
    "ProposalCount", "TokenEstimate", "Notes",
]

# ── Telemetry ───────────────────────────────────────────────────────
TELEMETRY_MAX_BYTES = 5 * 1024 * 1024
TELEMETRY_RING_MAX = 300
LOG_RING_MAX = 200
LOG_LINE_CAP = 240

# ── Alerts / notifications ─────────────────────────────────────────
ALERT_TYPES = ("sec_filing", "weather", "space_weather", "keyword_watch", "research_question")
ALERT_CHANNELS = ("chat", "voice", "signal")
NOTIFY_RING_MAX = 100
NOTIFY_MAX_BYTES = 2 * 1024 * 1024
NOTIFY_CRITICAL_SALIENCE = 0.9
DEFAULT_ALERT_SETTINGS = {
    "rate_limit_per_hour": 8,
    "quiet_hours_start": None,
    "quiet_hours_end": None,
}

# ── Signal (send-only) ─────────────────────────────────────────────
SIGNAL_CLI_PATH = os.environ.get("EVA_SIGNAL_CLI", "signal-cli")
SIGNAL_SENDER = os.environ.get("EVA_SIGNAL_SENDER", "").strip()
SIGNAL_RECIPIENT = os.environ.get("EVA_SIGNAL_RECIPIENT", "").strip()
SIGNAL_SEND_TIMEOUT = 15

# ── Entity extraction ──────────────────────────────────────────────
ENTITY_IGNORE_WORDS = {
    "the", "this", "that", "what", "when", "where", "how", "why", "who", "can", "could",
    "would", "should", "hello", "please", "thanks", "hey", "eva", "image", "tell", "today",
    "tomorrow", "yesterday", "time", "date", "reply", "respond", "answer", "exactly",
    "its", "whats", "have", "has", "had", "does", "did", "was", "were", "are", "been",
    "being", "will", "shall", "may", "might", "must", "let", "lets", "also", "just",
    "here", "there", "some", "any", "all", "each", "every", "many", "much", "very",
    "yes", "not", "but", "and", "for", "with", "from", "about", "into", "over",
    "your", "you", "they", "them", "their", "then", "than", "our", "his", "her",
    "great", "good", "like", "sure", "okay", "right", "know", "think", "want",
    "need", "make", "get", "see", "say", "said", "new", "use", "try", "give",
    "look", "help", "come", "take", "back", "well", "too", "now",
    "fetching", "searching", "getting", "running", "checking",
}

ENTITY_RESERVED_TERMS = {
    "run", "show", "query", "timestamp", "schema", "table", "tables", "database", "databases",
    "count", "sum", "average", "filter", "where", "join", "project", "distinct", "take", "top",
    "execute", "save", "remember", "store", "write", "reply", "respond", "answer",
    "kusto", "adx", "conversation", "conversations", "knowledge", "emotionstate", "reflections", "goals",
    "memorysummaries", "selfstate", "heuristicsindex", "emotionbaseline", "backgroundproposals",
    "backgroundactivity",
}
