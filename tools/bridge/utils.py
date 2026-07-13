"""Bridge domain: utils."""

import copy
import datetime
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
from bridge.sensitive import redact_credentials

_HTTP_CONTENT_TYPE_RE = _cfg.HTTP_CONTENT_TYPE_RE
_LMSTUDIO_ALLOWED_PORTS = _cfg.LMSTUDIO_ALLOWED_PORTS
_MCP_CONFIG_CACHE_PATH = _cfg.MCP_CONFIG_CACHE_PATH
_RUNTIME_STATE_PATH = _cfg.RUNTIME_STATE_PATH

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
    """Return True only for localhost, loopback, RFC1918, or IPv6 ULA."""
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
        if isinstance(addr, ipaddress.IPv6Address) and (
            addr.ipv4_mapped is not None
            or addr.sixtofour is not None
            or addr.teredo is not None
        ):
            return False
        if addr.is_loopback:
            return True
        if addr.is_unspecified or addr.is_multicast or addr.is_reserved:
            return False
        if isinstance(addr, ipaddress.IPv4Address):
            return any(addr in network for network in (
                ipaddress.ip_network("10.0.0.0/8"),
                ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"),
            ))
        return addr in ipaddress.ip_network("fc00::/7")
    except ValueError:
        return False



def _validate_lmstudio_base_url(raw):
    if not isinstance(raw, str):
        return "", "lmstudio_base_url must be a string"
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
    if parsed.params or parsed.query or parsed.fragment:
        return "", "lmstudio_base_url must not include params, query, or fragment"

    host = (parsed.hostname or "").lower()
    if not _is_local_or_private(host):
        return "", "lmstudio_base_url must point at localhost or a private network address"
    if _st.egress_mode == "offline" and host not in ("localhost", "127.0.0.1", "::1"):
        return "", "offline mode requires a loopback LM Studio address"

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
    return f"{parsed.scheme}://{host_for_url}:{port}/v1", ""



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
            had_github_pat = "GITHUB_PERSONAL_ACCESS_TOKEN" in env
            for k, v in env.items():
                if not isinstance(k, str):
                    continue
                if k == "_useGitHubPAT":
                    if v is True:
                        cleaned[k] = True
                    continue
                if k.startswith("_"):
                    continue
                if _cfg.is_sensitive_env_name(k):
                    continue  # drop tokens/keys/secrets
                if not isinstance(v, (str, int, float, bool)) or v is None:
                    continue
                if redact_credentials(k) != k or redact_credentials(v) != v:
                    continue
                if (
                    isinstance(v, str)
                    and len(v.strip()) >= 32
                    and re.fullmatch(r"[A-Za-z0-9_+/=.-]+", v.strip())
                ):
                    continue
                cleaned[k] = v
            if srv_name == "github-mcp-server" and had_github_pat:
                cleaned["_useGitHubPAT"] = True
            safe_srv["env"] = cleaned
        safe_name = redact_credentials(str(srv_name))
        redacted_srv = redact_credentials(safe_srv)
        if (
            isinstance(redacted_srv, dict)
            and isinstance(redacted_srv.get("env"), dict)
            and isinstance(env, dict)
            and env.get("_useGitHubPAT") is True
        ):
            redacted_srv["env"]["_useGitHubPAT"] = True
        safe[safe_name] = redacted_srv
    return safe



def _load_runtime_state():
    status, data = _cfg.load_runtime_state_document_status(
        _RUNTIME_STATE_PATH
    )
    if status == "invalid":
        try:
            with _cfg.open_private_file(
                _RUNTIME_STATE_PATH, "w", encoding="utf-8"
            ) as revoked:
                revoked.write("{}")
        except (OSError, _cfg.PrivateStorageError):
            pass
    if status != "valid" or data is None:
        return None
    safe = _sanitize_mcp_for_persist(data["mcp_servers"])
    safe, _rejected = _cfg.mcp_config_for_egress(safe, "cloud")
    return {"version": 1, "mode": data["mode"], "mcp_servers": safe}


def _persist_runtime_state(mode, mcp_servers):
    if mode not in ("local", "cloud"):
        return False
    try:
        state = {
            "version": 1, "mode": mode,
            "mcp_servers": _sanitize_mcp_for_persist(mcp_servers),
        }
        with _cfg.open_private_file(
            _RUNTIME_STATE_PATH, "w", encoding="utf-8"
        ) as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
        return True
    except (OSError, TypeError, _cfg.PrivateStorageError):
        print("[Bridge] Could not persist runtime state", file=sys.stderr)
        return False


def _persist_mcp_config(mcp_servers):
    """Persist the front-end MCP server selection so it survives bridge restarts
    even when the Electron file:// localStorage is cleared. Secrets are stripped
    before writing."""
    state = _load_runtime_state()
    mode = state["mode"] if state else ("local" if _st.local_mode else "cloud")
    return _persist_runtime_state(mode, mcp_servers)



def _load_persisted_mcp_config():
    """Load the persisted MCP server selection (no secrets)."""
    state = _load_runtime_state()
    if state is not None:
        return state["mcp_servers"]
    try:
        with _cfg.open_private_file(
            _MCP_CONFIG_CACHE_PATH, "r", encoding="utf-8"
        ):
            pass
        with _cfg.open_private_file(
            _MCP_CONFIG_CACHE_PATH, "w", encoding="utf-8"
        ) as revoked:
            revoked.write("{}")
    except FileNotFoundError:
        pass
    except (OSError, _cfg.PrivateStorageError):
        print("[Bridge] Could not revoke legacy MCP config", file=sys.stderr)
    return {}


def _load_persisted_mode():
    state = _load_runtime_state()
    if state is not None:
        return state["mode"]
    status, _data = _cfg.load_runtime_state_document_status(
        _RUNTIME_STATE_PATH
    )
    return "unknown" if status == "invalid" else "cloud"


# Small client preferences store (non-secret UI toggles) that survives the
# Electron file:// localStorage being wiped across app rebuilds. Used for things
# like the camera-presence auto-wake toggle so the user does not re-enable it
# every restart.
_CLIENT_PREFS_PATH = os.path.expanduser("~/.config/eva-standalone/client_prefs.json")



def _load_client_prefs():
    try:
        with _cfg.open_private_file(
            _CLIENT_PREFS_PATH, "r", encoding="utf-8"
        ) as f:
            data = json.load(f)
            if isinstance(data, dict):
                safe = {}
                if isinstance(data.get("cameraPresence"), bool):
                    safe["cameraPresence"] = data["cameraPresence"]
                base, error = _validate_lmstudio_base_url(
                    data.get("lmstudio_base_url")
                )
                if not error:
                    safe["lmstudio_base_url"] = base
                model = data.get("lmstudio_model")
                if (
                    isinstance(model, str) and 0 < len(model) <= 256
                    and re.search(r"[\x00-\x1f\x7f]", model) is None
                ):
                    safe["lmstudio_model"] = model
                if safe != data:
                    with _cfg.open_private_file(
                        _CLIENT_PREFS_PATH, "w", encoding="utf-8"
                    ) as rewritten:
                        json.dump(
                            safe, rewritten, sort_keys=True,
                            separators=(",", ":"),
                        )
                return safe
    except (OSError, json.JSONDecodeError, _cfg.PrivateStorageError):
        pass
    return {}



def _save_client_prefs(prefs):
    try:
        _cfg.ensure_private_directory(os.path.dirname(_CLIENT_PREFS_PATH))
        cur = _load_client_prefs()
        for k, v in (prefs or {}).items():
            if k == "cameraPresence" and isinstance(v, bool):
                cur[k] = v
            elif k == "lmstudio_base_url":
                base, error = _validate_lmstudio_base_url(v)
                if error:
                    raise ValueError(error)
                cur[k] = base
            elif (
                k == "lmstudio_model" and isinstance(v, str)
                and 0 < len(v) <= 256
                and re.search(r"[\x00-\x1f\x7f]", v) is None
            ):
                cur[k] = v
            else:
                raise ValueError("unsupported client preference")
        with _cfg.open_private_file(
            _CLIENT_PREFS_PATH, "w", encoding="utf-8"
        ) as f:
            json.dump(cur, f)
        return cur
    except (OSError, TypeError, ValueError, _cfg.PrivateStorageError) as exc:
        print(f"[Bridge] Could not persist client prefs: {exc}", file=sys.stderr)
        return None

# Telemetry — structured, privacy-safe event log for latency/behavior analysis
# ---------------------------------------------------------------------------
# Events are appended as JSONL to _TELEMETRY_PATH and mirrored to an in-memory
# ring buffer for the GET /v1/telemetry endpoint. Only numeric measures, closed
# enums, and hashed model identifiers are retained—never free-form labels,
# prompts, responses, tokens, keys, exceptions, or MCP environment values.

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
        prompt_text = f"[Subagent task: {label}] {prompt}"
        result = _st.acp_client.prompt(prompt_text)
        response_text = ""
        if isinstance(result, dict):
            if "error" in result:
                raise RuntimeError(str(result["error"]))
            response_text = result.get("text", "")
        else:
            response_text = str(result or "")
        with _st.subagent_lock:
            task["status"] = "done"
            task["result"] = response_text[:4000]
            task["ended_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _push_notification(f"Subagent done: {label}", response_text[:300], channel="chat")
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

_MEMORY_CAPTURE_DIRECTIVE = (
    "\n\n[Memory Capture]\n"
    "Do not call database ingest or management tools to save memory. The bridge's "
    "authenticated event-first turn finalizer extracts evidence-linked provisional facts "
    "and commits them after the response. Never claim a fact was stored before finalization."
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


