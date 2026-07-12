"""Immutable configuration constants for the Eva ACP Bridge.

This module centralizes path definitions, tuning thresholds, column
schemas, and other values that do not change at runtime. Mutable
state (token caches, flags, buffers) remains in ``core.py`` until
a future phase extracts it into ``state.py``.
"""

import datetime
import os
import pwd
import re
import sys
import urllib.parse


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
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(BRIDGE_DIR)
PROJECT_ROOT = os.path.dirname(TOOLS_DIR)
EVA_CONFIG_DIR = os.path.expanduser("~/.config/eva-standalone")
ARTIFACTS_DIR = os.path.join(EVA_CONFIG_DIR, "artifacts")
KUSTO_CLUSTER_CACHE_PATH = os.path.join(EVA_CONFIG_DIR, "kusto_cluster.txt")
MCP_CONFIG_CACHE_PATH = os.path.join(EVA_CONFIG_DIR, "mcp_config.json")
ALERTS_CONFIG_PATH = os.path.join(EVA_CONFIG_DIR, "alerts.json")
NOTIFY_PATH = os.path.join(EVA_CONFIG_DIR, "notifications.jsonl")
EMBEDDING_CACHE_PATH = os.path.join(EVA_CONFIG_DIR, "embeddings_cache.json")
MEMORY_BACKEND_PREF_PATH = os.path.join(EVA_CONFIG_DIR, "memory_backend.txt")
MODE_PREF_PATH = os.path.join(EVA_CONFIG_DIR, "mode.txt")
TELEMETRY_PATH = os.path.join(EVA_CONFIG_DIR, "telemetry.jsonl")

# ── Networking / validation ─────────────────────────────────────────
LMSTUDIO_ALLOWED_PORTS = {1234, 8000, 8080, 11434}
HTTP_CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")

# ── Request limits ──────────────────────────────────────────────────
MAX_JSON_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB

# ── Egress mode ─────────────────────────────────────────────────────
EGRESS_MODE_VALUES = ("offline", "local-network", "cloud")
REQUEST_ENVELOPE_FIELDS = {
    "request_id", "correlation_id", "session_id", "turn_id",
    "actor", "origin", "installation_id", "user_id",
}
SENSITIVE_ENV_MARKERS = {
    "TOKEN", "TOKENS", "KEY", "KEYS", "SECRET", "SECRETS", "PAT",
    "PASSWORD", "PASSWORDS", "CREDENTIAL", "CREDENTIALS", "AUTH",
    "AUTHORIZATION",
}
SENSITIVE_ENV_SUFFIXES = (
    "APIKEY", "ACCESSKEY", "PRIVATEKEY", "TOKEN", "SECRET", "PASSWORD",
    "CREDENTIAL", "CREDENTIALS", "AUTH", "AUTHORIZATION", "PAT",
)
_MCP_CONFIG_FIELDS = frozenset({"command", "args", "env"})
_SQLITE_MCP_NAMES = frozenset({"sqlite", "sqlite-mcp-server", "eva-sqlite"})
_KUSTO_SUFFIXES = (".kusto.windows.net", ".kusto.data.microsoft.com")
_COMMON_CHILD_ENV = frozenset({
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LOGNAME", "TZ", "USER",
})
_GUI_CHILD_ENV = frozenset({
    "DBUS_SESSION_BUS_ADDRESS", "DESKTOP_SESSION", "DISPLAY", "WAYLAND_DISPLAY",
    "XAUTHORITY", "XDG_CURRENT_DESKTOP", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
})
_CHILD_ENV_PROFILES = {
    "base": _COMMON_CHILD_ENV,
    "acp": _COMMON_CHILD_ENV | frozenset({
        "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
    }),
    "camera": _COMMON_CHILD_ENV,
    "gui": _COMMON_CHILD_ENV | _GUI_CHILD_ENV,
    "mcp": _COMMON_CHILD_ENV,
    "notification": _COMMON_CHILD_ENV | _GUI_CHILD_ENV,
}
_BLOCKED_CHILD_ENV = frozenset({
    "BASH_ENV", "CDPATH", "COPILOT_ALLOW_ALL", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH", "ELECTRON_RUN_AS_NODE", "ENV", "GIT_CONFIG",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
    "LD_AUDIT", "LD_LIBRARY_PATH", "LD_PRELOAD", "NODE_EXTRA_CA_CERTS",
    "NODE_OPTIONS", "NODE_PATH", "PYTHONHOME", "PYTHONPATH", "RUBYOPT",
})
_BLOCKED_CHILD_ENV_PREFIXES = (
    "COPILOT_", "CURL_", "DYLD_", "GIT_CONFIG_", "HTTPS_PROXY", "HTTP_PROXY",
    "LD_", "NODE_", "NPM_CONFIG_", "OTEL_", "PIP_", "PYTHON", "REQUESTS_",
    "SSL_CERT_", "ALL_PROXY", "NO_PROXY",
)


def _fixed_child_path():
    candidates = (
        "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
        "/usr/local/sbin", "/usr/bin", "/usr/sbin", "/bin", "/sbin",
    )
    return os.pathsep.join(path for path in candidates if os.path.isdir(path))


def _account_home():
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (AttributeError, KeyError, OSError):
        return os.path.expanduser("~")


def is_sensitive_env_name(name):
    upper = str(name or "").upper()
    if upper == "EVA_BRIDGE_TOKEN":
        return True
    parts = {part for part in re.split(r"[^A-Z0-9]+", upper) if part}
    if parts.intersection(SENSITIVE_ENV_MARKERS):
        return True
    compact = re.sub(r"[^A-Z0-9]", "", upper)
    return compact != "PATH" and any(compact.endswith(suffix) for suffix in SENSITIVE_ENV_SUFFIXES)


def child_process_env(explicit=None, *, profile="base"):
    """Build a minimal, profile-specific environment for one child process."""
    allowed = _CHILD_ENV_PROFILES.get(profile)
    if allowed is None:
        raise ValueError(f"unknown child environment profile: {profile}")
    result = {
        "HOME": _account_home(),
        "PATH": _fixed_child_path(),
    }
    for name in allowed:
        value = os.environ.get(name)
        if isinstance(value, str) and "\x00" not in value and len(value) <= 4096:
            result[name] = value
    if explicit and profile != "mcp":
        raise ValueError("explicit child environment is restricted to MCP children")
    for name, value in (explicit or {}).items():
        key = str(name)
        upper = key.upper()
        if (
            key == "EVA_BRIDGE_TOKEN"
            or upper in _BLOCKED_CHILD_ENV
            or any(upper.startswith(prefix) for prefix in _BLOCKED_CHILD_ENV_PREFIXES)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        ):
            raise ValueError(f"unsafe child environment variable: {key}")
        text = str(value)
        if "\x00" in text or len(text) > 16384:
            raise ValueError(f"invalid child environment value: {key}")
        result[key] = text
    return result


def normalize_kusto_origin(value):
    """Return one exact Microsoft Kusto HTTPS origin or raise ``ValueError``."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Kusto cluster must be a non-empty HTTPS origin")
    text = value if "://" in value else "https://" + value
    try:
        parsed = urllib.parse.urlsplit(text)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Kusto cluster URL is invalid") from exc
    hostname = (parsed.hostname or "").lower()
    if hostname.endswith("."):
        hostname = hostname[:-1]
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Kusto cluster host must be ASCII") from exc
    labels = hostname.split(".")
    valid_labels = bool(labels) and all(
        label
        and len(label) <= 63
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    )
    allowed_host = any(
        hostname.endswith(suffix) and hostname != suffix[1:]
        for suffix in _KUSTO_SUFFIXES
    )
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
        or not valid_labels
        or not allowed_host
    ):
        raise ValueError("Kusto cluster must be an exact Microsoft HTTPS origin")
    return "https://" + hostname


def _mcp_env(raw, allowed, *, strip_unknown=False):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return None
    safe = {}
    for key, value in raw.items():
        if key not in allowed:
            if strip_unknown:
                continue
            return None
        if key == "_useGitHubPAT":
            if value is not True:
                return None
            safe[key] = True
            continue
        if not isinstance(value, str) or "\x00" in value or len(value) > 16384:
            return None
        safe[str(key)] = value
    return safe


def _trusted_mcp_script(value, basename):
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    candidate = os.path.expanduser(value)
    if not os.path.isabs(candidate):
        candidate = os.path.join(PROJECT_ROOT, candidate)
    real = os.path.realpath(candidate)
    expected = os.path.realpath(os.path.join(TOOLS_DIR, basename))
    return expected if real == expected else None


def _trusted_python_command(value):
    if value == "python3":
        return True
    return (
        isinstance(value, str)
        and os.path.realpath(os.path.expanduser(value))
        == os.path.realpath(sys.executable)
    )


def _canonical_mcp_server(name, raw, mode):
    """Return one exact release-approved MCP process shape, else ``None``."""
    if (
        not isinstance(name, str)
        or not isinstance(raw, dict)
        or set(raw) - _MCP_CONFIG_FIELDS
        or not isinstance(raw.get("command"), str)
        or not isinstance(raw.get("args"), list)
        or not all(isinstance(arg, str) for arg in raw["args"])
    ):
        return None
    command = raw["command"]
    args = raw["args"]

    if name in _SQLITE_MCP_NAMES and _trusted_python_command(command):
        script = _trusted_mcp_script(args[0], "sqlite_mcp.py") if len(args) == 1 else None
        env = _mcp_env(
            raw.get("env"), {"EVA_MEMORY_DB"}, strip_unknown=True
        )
        if script and env is not None:
            if env.get("KUSTO_CLUSTER_URL"):
                try:
                    env["KUSTO_CLUSTER_URL"] = normalize_kusto_origin(
                        env["KUSTO_CLUSTER_URL"]
                    )
                except ValueError:
                    return None
            return {"command": sys.executable, "args": [script], "env": env}

    if mode != "cloud":
        return None

    if name == "kusto-mcp-server" and _trusted_python_command(command):
        script = _trusted_mcp_script(args[0], "kusto_mcp.py") if len(args) == 1 else None
        env = _mcp_env(
            raw.get("env"), {
                "KUSTO_ACCESS_TOKEN", "KUSTO_CLUSTER_URL", "KUSTO_DATABASE",
                "KUSTO_DATABASE_LOCKED",
            },
            strip_unknown=True,
        )
        if script and env is not None:
            return {"command": sys.executable, "args": [script], "env": env}

    if name == "eva-web-search" and _trusted_python_command(command):
        script = _trusted_mcp_script(args[0], "web_search_mcp.py") if len(args) == 1 else None
        env = _mcp_env(raw.get("env"), set(), strip_unknown=True)
        if script and env is not None:
            return {"command": sys.executable, "args": [script], "env": env}

    if (
        name == "azure-mcp-server"
        and command == "npx"
        and args == ["-y", "@azure/mcp@latest", "server", "start"]
    ):
        env = _mcp_env(raw.get("env"), {"AZURE_MCP_COLLECT_TELEMETRY"})
        if env is not None and env.get("AZURE_MCP_COLLECT_TELEMETRY", "false") == "false":
            return {
                "command": "npx", "args": list(args),
                "env": {"AZURE_MCP_COLLECT_TELEMETRY": "false"},
            }

    if (
        name == "github-mcp-server"
        and command == "docker"
        and args == [
            "run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
            "ghcr.io/github/github-mcp-server",
        ]
    ):
        env = _mcp_env(
            raw.get("env"), {"_useGitHubPAT", "GITHUB_PERSONAL_ACCESS_TOKEN"}
        )
        if env is not None:
            return {"command": "docker", "args": list(args), "env": env}
    return None


def _release_disabled_mcp(name, raw):
    """True unless the server is an exact release-approved cloud shape."""
    return _canonical_mcp_server(name, raw, "cloud") is None


def mcp_config_for_egress(mcp_config, mode):
    """Return the MCP subset permitted by an egress policy.

    Every mode is fail-closed over exact process shapes. Cloud permits only the
    release presets plus trusted bundled servers. Offline and local-network
    permit only Eva's bundled SQLite MCP. Unknown, aliased, wrapped, or
    pointer/keyboard-capable servers are rejected before process startup.
    """
    source = dict(mcp_config or {}) if isinstance(mcp_config, dict) else {}
    allowed = {}
    rejected = []
    for name, raw in source.items():
        canonical = _canonical_mcp_server(name, raw, mode)
        if canonical is None:
            rejected.append(str(name))
        else:
            allowed[name] = canonical
    return allowed, rejected

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
BG_JOB_ADX_PROJECTION = "adx_projection"
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

# ═══════════════════════════════════════════════════════════════════════
#  Phase 2 – Startup-immutable, fail-closed feature flags
#
#  These are frozen at import time. Invalid enum values produce a sentinel
#  (the string "INVALID") so downstream code can detect misconfiguration
#  deterministically without crashing on import.
# ═══════════════════════════════════════════════════════════════════════

_PHASE2_RECALL_MODES = frozenset({"legacy", "shadow", "hybrid"})
_PHASE2_SEMANTIC_MODES = frozenset({"off", "cache", "openai"})
_PHASE2_CONSOLIDATION_VALUES = frozenset({"off", "proposals"})
_PHASE2_ANALYTICS_VALUES = frozenset({"off", "local"})
_PHASE2_BOOL_TRUTHY = frozenset({"1", "true", "yes"})
_PHASE2_BOOL_FALSY = frozenset({"0", "false", "no"})


def _phase2_enum(env_name, valid_set, default):
    """Read an env var as a constrained enum. Returns 'INVALID' on bad value."""
    raw = os.environ.get(env_name, "").strip().lower()
    if not raw:
        return default
    if raw in valid_set:
        return raw
    return "INVALID"


def _phase2_bool(env_name, default=False):
    """Read env var as strict boolean. Returns (value, is_valid).

    Valid values: '1','true','yes' (True); '0','false','no' (False); '' (default).
    Invalid values (e.g. 'maybe','2','on') return (default, False) — the
    sentinel records the flag name as invalid rather than silently defaulting.
    """
    raw = os.environ.get(env_name, "").strip().lower()
    if not raw:
        return (default, True)
    if raw in _PHASE2_BOOL_TRUTHY:
        return (True, True)
    if raw in _PHASE2_BOOL_FALSY:
        return (False, True)
    # Invalid: not silently false, records invalidity
    return (default, False)


# ── Frozen flag values ──────────────────────────────────────────────

# Master kill switch (default OFF)
_EVA_PHASE2_MEMORY_RESULT = _phase2_bool("EVA_PHASE2_MEMORY", False)
EVA_PHASE2_MEMORY = _EVA_PHASE2_MEMORY_RESULT[0]

# Recall mode
EVA_MEMORY_RECALL_MODE = _phase2_enum("EVA_MEMORY_RECALL_MODE", _PHASE2_RECALL_MODES, "legacy")

# Semantic mode
EVA_MEMORY_SEMANTIC_MODE = _phase2_enum("EVA_MEMORY_SEMANTIC_MODE", _PHASE2_SEMANTIC_MODES, "off")

# Explicit consent for semantic queries
_EVA_MEMORY_SEMANTIC_QUERY_CONSENT_RESULT = _phase2_bool("EVA_MEMORY_SEMANTIC_QUERY_CONSENT", False)
EVA_MEMORY_SEMANTIC_QUERY_CONSENT = _EVA_MEMORY_SEMANTIC_QUERY_CONSENT_RESULT[0]

# Consolidation engine
EVA_MEMORY_CONSOLIDATION = _phase2_enum("EVA_MEMORY_CONSOLIDATION", _PHASE2_CONSOLIDATION_VALUES, "off")

# Analytics collection
EVA_MEMORY_ANALYTICS = _phase2_enum("EVA_MEMORY_ANALYTICS", _PHASE2_ANALYTICS_VALUES, "off")

# ── Invalid flag tracking ───────────────────────────────────────────

def _collect_invalid_flags():
    """Collect a tuple of flag names with invalid values. No values stored."""
    invalid = []
    # Bool flags
    _bool_flags = [
        ("EVA_PHASE2_MEMORY", _EVA_PHASE2_MEMORY_RESULT),
        ("EVA_MEMORY_SEMANTIC_QUERY_CONSENT", _EVA_MEMORY_SEMANTIC_QUERY_CONSENT_RESULT),
    ]
    for name, (_, valid) in _bool_flags:
        if not valid:
            invalid.append(name)
    # Enum flags
    _enum_flags = [
        ("EVA_MEMORY_RECALL_MODE", EVA_MEMORY_RECALL_MODE),
        ("EVA_MEMORY_SEMANTIC_MODE", EVA_MEMORY_SEMANTIC_MODE),
        ("EVA_MEMORY_CONSOLIDATION", EVA_MEMORY_CONSOLIDATION),
        ("EVA_MEMORY_ANALYTICS", EVA_MEMORY_ANALYTICS),
    ]
    for name, value in _enum_flags:
        if value == "INVALID":
            invalid.append(name)
    return tuple(invalid)


PHASE2_INVALID_FLAGS = _collect_invalid_flags()


def phase2_config_valid():
    """Return True if all Phase 2 flags are in valid states."""
    return len(PHASE2_INVALID_FLAGS) == 0


def phase2_effective_enabled():
    """Return True only if master flag is on AND config is valid."""
    return EVA_PHASE2_MEMORY and phase2_config_valid()


def phase2_effective_modes():
    """Return effective modes when master is off or invalid.

    If master is off or config invalid, returns all-legacy/off/no-consent
    defaults regardless of what was configured.
    """
    if not phase2_effective_enabled():
        return {
            "recall_mode": "legacy",
            "semantic_mode": "off",
            "query_consent": False,
            "consolidation": "off",
            "analytics": "off",
        }
    return {
        "recall_mode": EVA_MEMORY_RECALL_MODE,
        "semantic_mode": EVA_MEMORY_SEMANTIC_MODE,
        "query_consent": EVA_MEMORY_SEMANTIC_QUERY_CONSENT,
        "consolidation": EVA_MEMORY_CONSOLIDATION,
        "analytics": EVA_MEMORY_ANALYTICS,
    }


def validate_phase2_startup():
    """Validate Phase 2 configuration at startup. Returns (ok, message).

    - If invalid flags exist AND master requested enabled => (False, error_msg)
      Caller should print redacted error and exit(2).
    - If invalid flags exist AND master off => (True, warning_msg)
      Caller should print warning; effective disabled.
    - If all valid => (True, None)
    """
    if not PHASE2_INVALID_FLAGS:
        return (True, None)

    flag_list = ", ".join(PHASE2_INVALID_FLAGS)

    if EVA_PHASE2_MEMORY or "EVA_PHASE2_MEMORY" in PHASE2_INVALID_FLAGS:
        return (
            False,
            f"Phase2 startup FATAL: invalid configuration for flags: {flag_list}. "
            f"Master enabled but config invalid. Fix environment or disable EVA_PHASE2_MEMORY.",
        )
    else:
        return (
            True,
            f"Phase2 startup WARNING: invalid configuration for flags: {flag_list}. "
            f"Master is off so Phase2 remains disabled.",
        )


def phase2_startup_summary():
    """Return a fixed, credential-free summary of effective Phase 2 modes."""
    modes = phase2_effective_modes()
    return (
        "Phase2 memory=" + ("enabled" if phase2_effective_enabled() else "disabled")
        + ", recall=" + modes["recall_mode"]
        + ", semantic=" + modes["semantic_mode"]
        + ", query_consent=" + ("enabled" if modes["query_consent"] else "disabled")
        + ", consolidation=" + modes["consolidation"]
        + ", analytics=" + modes["analytics"]
    )


# ═══════════════════════════════════════════════════════════════════════
#  Phase 3 – Safe continual-learning shadow mode
# ═══════════════════════════════════════════════════════════════════════

_PHASE3_LEARNING_VALUES = frozenset({"off", "shadow"})
EVA_PHASE3_LEARNING = _phase2_enum(
    "EVA_PHASE3_LEARNING", _PHASE3_LEARNING_VALUES, "off"
)
_EVA_LEGACY_SKILL_AUTO_LEARN_RESULT = _phase2_bool(
    "EVA_LEGACY_SKILL_AUTO_LEARN", False
)
EVA_LEGACY_SKILL_AUTO_LEARN = _EVA_LEGACY_SKILL_AUTO_LEARN_RESULT[0]
PHASE3_INVALID_FLAGS = tuple(
    name for name, valid in (
        ("EVA_PHASE3_LEARNING", EVA_PHASE3_LEARNING != "INVALID"),
        ("EVA_LEGACY_SKILL_AUTO_LEARN", _EVA_LEGACY_SKILL_AUTO_LEARN_RESULT[1]),
    ) if not valid
)


def phase3_config_valid():
    return not PHASE3_INVALID_FLAGS


def phase3_effective_enabled():
    return phase3_config_valid() and EVA_PHASE3_LEARNING == "shadow"


def validate_phase3_startup():
    if PHASE3_INVALID_FLAGS:
        return (
            False,
            "Phase3 startup FATAL: invalid configuration for flags: "
            + ", ".join(PHASE3_INVALID_FLAGS),
        )
    return (True, None)


def phase3_startup_summary():
    return (
        "Phase3 learning="
        + ("shadow" if phase3_effective_enabled() else "disabled")
        + ", legacy_skill_auto_learn="
        + ("enabled" if EVA_LEGACY_SKILL_AUTO_LEARN else "disabled")
    )
