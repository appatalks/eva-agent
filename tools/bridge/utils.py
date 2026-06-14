"""Bridge domain: utils."""

import copy
import json
import os
import re
import sys
import threading
import time
import ipaddress
import urllib.parse
from bridge import config as _cfg
from bridge import state as _st
from bridge.cron import _push_notification

_HTTP_CONTENT_TYPE_RE = _cfg.HTTP_CONTENT_TYPE_RE
_LMSTUDIO_ALLOWED_PORTS = _cfg.LMSTUDIO_ALLOWED_PORTS
_MCP_CONFIG_CACHE_PATH = _cfg.MCP_CONFIG_CACHE_PATH
_MCP_SECRET_ENV_MARKERS = ("TOKEN", "KEY", "SECRET", "PAT", "PASSWORD", "CREDENTIAL")

def _env_truthy(name):
    """Return True when an environment flag uses the shared truthy form."""
    return _cfg.env_truthy(name)



def _is_loopback_bind():
    bind = (_st.bridge_bind_address or "").strip().lower()
    return bind in ("127.0.0.1", "localhost", "::1")



def _valid_artifact_name(name):
    return (
        bool(re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name or ""))
        and not name.startswith(".")
        and not all(char == "." for char in name)
    )



def _safe_content_type(value):
    if value and _HTTP_CONTENT_TYPE_RE.fullmatch(value):
        return value
    return "application/octet-stream"



def _is_local_or_private(host):
    """Return True if host is localhost or an RFC 1918 / link-local address."""
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        import ipaddress
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return False



def _validate_lmstudio_base_url(raw):
    value = (raw or "").strip().rstrip("/")
    if not value:
        return "", "lmstudio_base_url is required"

    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return "", "lmstudio_base_url is invalid"

    if parsed.scheme not in ("http", "https"):
        return "", "lmstudio_base_url must use http or https"
    if parsed.username or parsed.password:
        return "", "lmstudio_base_url must not include userinfo"
    if parsed.query or parsed.fragment:
        return "", "lmstudio_base_url must not include query or fragment"

    host = (parsed.hostname or "").lower()
    if not _is_local_or_private(host):
        return "", "lmstudio_base_url must point at localhost or a private network address"

    try:
        port = parsed.port
    except ValueError:
        return "", "lmstudio_base_url port must be numeric"
    if port not in _LMSTUDIO_ALLOWED_PORTS:
        return "", "lmstudio_base_url port is not allowed"

    if parsed.path not in ("", "/v1", "/v1/"):
        return "", "lmstudio_base_url path must be empty or /v1"

    host_for_url = host
    if ":" in host_for_url and not host_for_url.startswith("["):
        host_for_url = "[" + host_for_url + "]"
    normalized_path = "/v1" if parsed.path in ("/v1", "/v1/") else ""
    return f"{parsed.scheme}://{host_for_url}:{port}{normalized_path}", ""



def _sanitize_mcp_for_persist(mcp_servers):
    """Return a deep copy of an MCP server config with secret-looking env values
    removed, so the persisted file never holds tokens or keys. Internal flags
    such as _useGitHubPAT are kept (they tell the bridge to resolve the PAT from
    the process environment at apply time)."""
    safe = {}
    for srv_name, srv_cfg in (mcp_servers or {}).items():
        if not isinstance(srv_cfg, dict):
            continue
        safe_srv = copy.deepcopy(srv_cfg)
        env = safe_srv.get("env")
        if isinstance(env, dict):
            cleaned = {}
            for k, v in env.items():
                upper = str(k).upper()
                if k.startswith("_"):
                    cleaned[k] = v  # internal flag, not a secret value
                elif any(marker in upper for marker in _MCP_SECRET_ENV_MARKERS):
                    continue  # drop tokens/keys/secrets
                else:
                    cleaned[k] = v
            safe_srv["env"] = cleaned
        safe[srv_name] = safe_srv
    return safe



def _persist_mcp_config(mcp_servers):
    """Persist the front-end MCP server selection so it survives bridge restarts
    even when the Electron file:// localStorage is cleared. Secrets are stripped
    before writing."""
    try:
        os.makedirs(os.path.dirname(_MCP_CONFIG_CACHE_PATH), exist_ok=True)
        with open(_MCP_CONFIG_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_sanitize_mcp_for_persist(mcp_servers), f)
    except (OSError, TypeError) as exc:
        print(f"[Bridge] Could not persist MCP config: {exc}", file=sys.stderr)



def _load_persisted_mcp_config():
    """Load the persisted MCP server selection (no secrets)."""
    try:
        if os.path.isfile(_MCP_CONFIG_CACHE_PATH):
            with open(_MCP_CONFIG_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[Bridge] Could not load persisted MCP config: {exc}", file=sys.stderr)
    return {}


# Small client preferences store (non-secret UI toggles) that survives the
# Electron file:// localStorage being wiped across app rebuilds. Used for things
# like the camera-presence auto-wake toggle so the user does not re-enable it
# every restart.
_CLIENT_PREFS_PATH = os.path.expanduser("~/.config/eva-standalone/client_prefs.json")



def _load_client_prefs():
    try:
        if os.path.isfile(_CLIENT_PREFS_PATH):
            with open(_CLIENT_PREFS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}



def _save_client_prefs(prefs):
    try:
        os.makedirs(os.path.dirname(_CLIENT_PREFS_PATH), exist_ok=True)
        cur = _load_client_prefs()
        # Only store small scalar values (booleans, strings, numbers) so this can
        # never become a secrets sink.
        for k, v in (prefs or {}).items():
            if isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 200):
                cur[str(k)[:64]] = v
        with open(_CLIENT_PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(cur, f)
        return cur
    except (OSError, TypeError) as exc:
        print(f"[Bridge] Could not persist client prefs: {exc}", file=sys.stderr)
        return _load_client_prefs()


# ---------------------------------------------------------------------------
# Telemetry — structured, privacy-safe event log for latency/behavior analysis
# ---------------------------------------------------------------------------
# Events are appended as JSONL to _TELEMETRY_PATH and mirrored to an in-memory
# ring buffer for the GET /v1/telemetry endpoint. We record durations, model
# names, routing/decision labels, and character COUNTS only — never the user
# message, the response text, tokens, keys, or any MCP env values.

_TELEMETRY_PATH = _cfg.TELEMETRY_PATH
_TELEMETRY_MAX_BYTES = _cfg.TELEMETRY_MAX_BYTES
_TELEMETRY_RING_MAX = _cfg.TELEMETRY_RING_MAX
_TELEMETRY_ENABLED = os.environ.get("EVA_TELEMETRY", "1") not in ("0", "false", "no")
_st.telemetry_lock = _st.telemetry_lock
_st.telemetry_ring = _st.telemetry_ring


# ── Log ring — recent stdout lines, for the voice-mode background feed ───────
# A tee on stdout mirrors every printed line both to the real terminal and to a
# small in-memory ring. The voice view polls GET /v1/logs and renders these as
# a faint scrolling console behind the orb. Lines are bridge status output
# (already free of secrets by the project's logging discipline); each is length-
# capped defensively.
_LOG_RING_MAX = _cfg.LOG_RING_MAX
_LOG_LINE_CAP = _cfg.LOG_LINE_CAP
_st.log_lock = _st.log_lock
_st.log_ring = _st.log_ring
# _log_seq -> _st.log_seq



def _subagent_worker(task_id, prompt, label):
    """Run a single subagent task in its own thread using the existing ACP pool."""
    with _st.subagent_lock:
        task = _st.subagent_tasks.get(task_id)
        if not task:
            return
    try:
        if not _st.acp_client or not _st.acp_client.alive:
            raise RuntimeError("ACP not available")
        messages = [{"role": "user", "content": f"[Subagent task: {label}] {prompt}"}]
        result = _st.acp_client.send_prompt(messages)
        with _st.subagent_lock:
            task["status"] = "done"
            task["result"] = str(result or "")[:4000]
            task["ended_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _push_notification(f"Subagent done: {label}", str(result or "")[:300], channel="chat")
    except Exception as e:
        with _st.subagent_lock:
            task["status"] = "error"
            task["result"] = str(e)[:500]
            task["ended_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _push_notification(f"Subagent failed: {label}", str(e)[:300], channel="chat")



def _classify_request_type(msg_lower):
    """Classify a message into a coarse request type for prompt tuning.

    Uses phrase patterns and guards ambiguous single words (open/close/share/
    market/result) that previously misrouted everyday messages. Defaults to
    'general', which lets the agentic ACP layer pick its own tools."""
    m = msg_lower or ""

    finance_strong = re.search(
        r'\b(stock price|share price|stock market|stock quote|market cap|ticker symbol|'
        r'nasdaq|s&p ?500|dow jones|earnings report)\b', m
    ) or re.search(r'(?:^|\s)\$[a-z]{1,5}\b', m)
    finance_noun = re.search(r'\b(stock|stocks|shares?|ticker|equit(?:y|ies)|crypto|bitcoin|etf)\b', m)
    finance_action = re.search(r'\b(price|prices|quote|quotes|market|trading|trade|buy|sell|invest|worth|value)\b', m)
    if finance_strong or (finance_noun and finance_action):
        return "financial-data"

    if re.search(r'\b(weather|forecast|temperature|raining|snowing|humidity|wind speed)\b', m):
        return "weather-search"

    if re.search(r'\b(news|headlines?|breaking news|current events?|morning briefing|daily briefing|briefing)\b', m) or \
       re.search(r'\blatest\b.*\b(update|report|story|stories|happening|developments?)\b', m):
        return "news-search"

    if re.search(r'\b(kql|kusto|run a query|execute a query|table schema|sample rows|show me data)\b', m):
        return "kusto-query"
    if re.search(r'\b(count|summarize|filter by|group by|\bjoin\b|distinct|top \d|take \d)\b', m):
        return "kusto-operator"

    if re.search(r'\b(search the web|web search|look up|google|what happened|who won|search for)\b', m):
        return "web-search"

    return "general"
_MEMORY_TABLES = _cfg.MEMORY_TABLES

# Injected into tool-active ACP prompts so Eva persists durable facts herself.
# The model decides salience (not regex), and writes via the kusto_ingest_inline MCP tool.
_MEMORY_CAPTURE_DIRECTIVE = (
    "\n\n[Memory Capture — act before answering]\n"
    "If this message states a durable fact about the user (preferences, plans, relationships, "
    "possessions, identity, or a list such as a music playlist), OR the user asks you to "
    "remember/save/note something, you MUST persist it first by calling the kusto_ingest_inline tool.\n"
    "Call it with table=\"Knowledge\" and one row object per fact, using exactly these columns:\n"
    "  Timestamp = current UTC time in ISO-8601 (e.g. 2026-06-02T12:00:00Z)\n"
    "  Entity = \"User\" for facts about the user; otherwise the proper-noun subject\n"
    "  Relation = a short snake_case key (e.g. youtube_music_playlist, favorite_song, upcoming_trip)\n"
    "  Value = the concrete content; for a list, a single comma-separated string of the items\n"
    "  Confidence = 0.85 when the user stated it directly\n"
    "  Source = \"learned\"\n"
    "  Decay = 0.01\n"
    "Split distinct facts into separate rows. Do NOT save greetings, one-off questions, or "
    "ephemeral chit-chat. If nothing durable was shared, do not call the tool. "
    "After saving, briefly confirm what you stored so the user knows it persisted."
)
_GOAL_CATEGORIES = _cfg.GOAL_CATEGORIES
_GOAL_STATUSES = _cfg.GOAL_STATUSES
_GOAL_COLUMNS = _cfg.GOAL_COLUMNS
_GOALS_LATEST_QUERY = _cfg.GOALS_LATEST_QUERY
# ── Skills (imported, normalized, semantically surfaced) ──────────────
# A skill is a flexible instruction document Eva can follow, from simple
# ("create a PDF") to multi-step ("review a PR and push fixes"). Imported from a
# variety of sources, normalized ("Eva'rised") into this schema by the agent,
# stored in ADX, and surfaced on demand by semantic match to the user's message.
_SKILL_STATUSES = _cfg.SKILL_STATUSES
_SKILL_COLUMNS = _cfg.SKILL_COLUMNS
_SKILLS_LATEST_QUERY = _cfg.SKILLS_LATEST_QUERY
_SKILL_SOURCE_MAX_BYTES = _cfg.SKILL_SOURCE_MAX_BYTES
_SKILL_INSTRUCTIONS_INJECT_CAP = _cfg.SKILL_INSTRUCTIONS_INJECT_CAP
_SKILL_INJECT_MAX = _cfg.SKILL_INJECT_MAX
_BG_JOB_TYPE = _cfg.BG_JOB_TYPE
_BG_TARGET_TABLE = _cfg.BG_TARGET_TABLE
_BG_JOB_GOAL_CHECKIN = _cfg.BG_JOB_GOAL_CHECKIN
_BG_JOB_DAILY_DIGEST = _cfg.BG_JOB_DAILY_DIGEST
_BG_JOB_KNOWLEDGE_HYGIENE = _cfg.BG_JOB_KNOWLEDGE_HYGIENE
_BG_JOB_REFLECTION_SYNTHESIS = _cfg.BG_JOB_REFLECTION_SYNTHESIS
_BG_JOB_EMOTION_DRIFT = _cfg.BG_JOB_EMOTION_DRIFT
_BG_JOB_TOKEN_TELEMETRY = _cfg.BG_JOB_TOKEN_TELEMETRY
_BG_JOB_PROACTIVE_BRIEFING = _cfg.BG_JOB_PROACTIVE_BRIEFING
_BG_JOB_MARKET_SNAPSHOT = _cfg.BG_JOB_MARKET_SNAPSHOT
_BG_JOB_SEC_FILINGS = _cfg.BG_JOB_SEC_FILINGS
_BG_JOB_SPACE_WEATHER = _cfg.BG_JOB_SPACE_WEATHER
_BG_JOB_RESEARCH_DEEPDIVE = _cfg.BG_JOB_RESEARCH_DEEPDIVE
_BG_JOB_ALERT_WATCH = _cfg.BG_JOB_ALERT_WATCH
# Per-job enable switches. All on by default; the loop still respects the
# global _bg_loop_enabled flag and the recent-activity pause.
_BG_JOBS_ENABLED = {
    _BG_JOB_TYPE: True,
    _BG_JOB_GOAL_CHECKIN: True,
    _BG_JOB_DAILY_DIGEST: True,
    _BG_JOB_KNOWLEDGE_HYGIENE: True,
    _BG_JOB_REFLECTION_SYNTHESIS: True,
    _BG_JOB_EMOTION_DRIFT: True,
    _BG_JOB_TOKEN_TELEMETRY: True,
    _BG_JOB_PROACTIVE_BRIEFING: True,
    _BG_JOB_MARKET_SNAPSHOT: True,
    _BG_JOB_SEC_FILINGS: True,
    _BG_JOB_SPACE_WEATHER: True,
    _BG_JOB_RESEARCH_DEEPDIVE: True,
    _BG_JOB_ALERT_WATCH: True,
}
_BG_APPLY_TABLES = _cfg.BG_APPLY_TABLES
_GOAL_STALE_DAYS = _cfg.GOAL_STALE_DAYS
_GOAL_CHECKIN_MAX = _cfg.GOAL_CHECKIN_MAX
_KNOWLEDGE_STALE_CONFIDENCE = _cfg.KNOWLEDGE_STALE_CONFIDENCE
_EMOTION_DRIFT_THRESHOLD = _cfg.EMOTION_DRIFT_THRESHOLD
_REFLECTION_SYNTH_MIN = _cfg.REFLECTION_SYNTH_MIN
_SEC_WATCH_SYMBOLS = _cfg.SEC_WATCH_SYMBOLS
# Uppercase tokens that look like tickers but are not, used to filter the
# heuristic ticker extraction from goal text.
_TICKER_STOPWORDS = {
    "SEC", "CEO", "CFO", "COO", "ETF", "USA", "USD", "API", "PLC", "LLC", "INC",
    "NYSE", "IPO", "EPS", "GDP", "FDA", "ESG", "AND", "THE", "FOR", "ESPP", "AI",
}
_BG_PROPOSAL_STATUSES = _cfg.BG_PROPOSAL_STATUSES
_BG_ACTIVITY_STATUSES = {"succeeded", "failed", "paused", "skipped"}
_BG_PROPOSAL_COLUMNS = _cfg.BG_PROPOSAL_COLUMNS
_BG_ACTIVITY_COLUMNS = _cfg.BG_ACTIVITY_COLUMNS
_BG_PROPOSALS_LATEST_QUERY = (
    "BackgroundProposals "
    "| extend _SortAt = coalesce(ReviewedAt, CreatedAt) "
    "| summarize arg_max(_SortAt, *) by ProposalId "
    "| project-away _SortAt"
)
# _bg_loop_thread -> _st.bg_loop_thread
_st.bg_loop_stop = _st.bg_loop_stop
# _bg_loop_enabled -> _st.bg_loop_enabled
# _bg_loop_interval_seconds -> _st.bg_loop_interval_seconds
# _bg_last_tick_iso -> _st.bg_last_tick_iso
# _bg_last_error -> _st.bg_last_error
# _bg_last_activity -> _st.bg_last_activity
# _last_user_activity_ts -> _st.last_user_activity_ts
_st.bg_tick_lock = _st.bg_tick_lock

# ---------------------------------------------------------------------------
# Cron scheduler — user-defined scheduled tasks
# ---------------------------------------------------------------------------
_CRON_TASKS_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "eva-standalone", "cron_tasks.json"
)
# _cron_tasks -> _st.cron_tasks
_st.cron_lock = _st.cron_lock


