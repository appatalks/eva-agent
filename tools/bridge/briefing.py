"""Non-blocking startup preparation for Eva's morning briefing."""

import threading
import time

from bridge import config as _cfg
from bridge import state as _st
from bridge.audit import audit_event

_briefing_lock = threading.RLock()
_LIVE_TOOL_READY_TIMEOUT_SECONDS = 20


def _new_state():
    return {
        "status": "idle",
        "started_at": "",
        "completed_at": "",
        "sources": {},
        "summary": "",
    }


def briefing_status():
    with _briefing_lock:
        return dict(_st.startup_briefing)


def briefing_unavailable_sources(status=None):
    """Name required live sources that cannot support a complete briefing."""
    state = status if isinstance(status, dict) else briefing_status()
    sources = state.get("sources") or {}
    return [name for name in ("news", "markets") if (sources.get(name) or {}).get("status") != "ready"]


def briefing_prompt_context(allow_partial=False):
    """Return ready data, optionally including sources completed during preparation."""
    with _briefing_lock:
        state = _st.startup_briefing
        if state.get("status") != "ready" and not allow_partial:
            return ""
        sources = dict(state.get("sources") or {})
    lines = []
    for name in ("memory", "news", "weather", "markets"):
        source = sources.get(name) or {}
        if source.get("status") == "ready" and source.get("summary"):
            lines.append(name.title() + ": " + str(source["summary"])[:1200])
        elif name == "weather" and source.get("status") in {"failed", "cancelled"}:
            lines.append("Weather: unavailable (" + str(source.get("summary") or "not configured")[:240] + ")")
    return "\n".join(lines)


def _set_source(name, status, summary=""):
    with _briefing_lock:
        state = _st.startup_briefing
        state["sources"][name] = {"status": status, "summary": str(summary or "")[:1200]}


def _memory_source():
    from bridge.background import _job_proactive_briefing
    from bridge.memory import _resolve_memory_backend
    from bridge.kusto import _get_kusto_config
    now = _cfg.utc_now()
    backend = _resolve_memory_backend()
    cluster, database = _get_kusto_config() if backend != "sqlite" else (None, None)
    proposals, note = _job_proactive_briefing({
        "cluster": cluster,
        "database": database,
        "backend": backend,
        "now_iso": _cfg.to_utc_iso(now),
    })
    if proposals:
        return proposals[0].get("payload", {}).get("Summary", ""), "ready"
    return note or "No local briefing summary is available.", "partial"


def _live_source(name, prompt, timeout):
    if _st.local_mode:
        manager = _st.local_mcp_manager
        if not manager or not manager.alive:
            return "Local live-data tools are unavailable.", "failed"
        if name == "weather":
            return "No configured location is available for a local weather briefing.", "failed"
        tool_name = "web_search_news" if name == "news" else "web_search"
        query = "top news headlines today" if name == "news" else "major market update today"
        result = manager.call_tool(tool_name, {"query": query, "max_results": 6}, timeout=timeout)
        text = str((result or {}).get("text") or "").strip()
        if text:
            return text, "ready"
        return str((result or {}).get("error") or "Local live-data retrieval returned no result."), "failed"
    from bridge.background import _bg_agent_prompt
    context = {"backend": "sqlite", "cluster": None, "database": None}
    text, error = _bg_agent_prompt(prompt, context, timeout=timeout)
    if text:
        return text, "ready"
    if error == "user active":
        return "Preparation stopped because the user became active.", "cancelled"
    return error or "No result returned.", "failed"


def _wait_for_live_tools():
    """Let asynchronous MCP restoration finish without delaying bridge readiness."""
    deadline = time.monotonic() + _LIVE_TOOL_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        local_manager = _st.local_mcp_manager
        if _st.local_mode:
            if local_manager and local_manager.alive and local_manager.tool_count:
                return True
        elif _st.configured_mcp_config:
            return True
        time.sleep(0.25)
    return False


def _prepare_worker():
    correlation_id = "startup-briefing-" + (_st.cognition_launch_id or "bridge")
    try:
        with _briefing_lock:
            _st.startup_briefing = _new_state()
            _st.startup_briefing["status"] = "preparing"
            _st.startup_briefing["started_at"] = _cfg.to_utc_iso(_cfg.utc_now())
        audit_event("briefing.prepare", correlation_id, "started")

        memory_text, memory_status = _memory_source()
        _set_source("memory", memory_status, memory_text)
        live_sources = (
            ("news", "Provide a concise current morning news briefing with sources. Do not invent facts.", 60),
            ("weather", "Provide the current weather and short forecast for the configured user location, if available. Do not invent a location or facts.", 45),
            ("markets", "Provide a concise current market snapshot for configured watched symbols, if available. Do not invent prices.", 60),
        )
        if _wait_for_live_tools():
            for name, prompt, timeout in live_sources:
                text, status = _live_source(name, prompt, timeout)
                _set_source(name, status, text)
        else:
            for name, _, _ in live_sources:
                _set_source(name, "failed", "Live data tools were not ready before preparation timed out.")

        with _briefing_lock:
            state = _st.startup_briefing
            required_sources = ("news", "markets")
            state["status"] = "ready" if all(
                (state["sources"].get(name) or {}).get("status") == "ready" for name in required_sources
            ) else "partial"
            state["completed_at"] = _cfg.to_utc_iso(_cfg.utc_now())
            state["summary"] = "Morning briefing prepared." if state["status"] == "ready" else "Morning briefing prepared with unavailable required sources."
            outcome = state["status"]
        audit_event("briefing.prepare", correlation_id, outcome, source_count=len(live_sources) + 1)
    except Exception as error:
        with _briefing_lock:
            _st.startup_briefing["status"] = "failed"
            _st.startup_briefing["completed_at"] = _cfg.to_utc_iso(_cfg.utc_now())
            _st.startup_briefing["summary"] = "Morning briefing preparation failed."
        audit_event("briefing.prepare", correlation_id, "failed", error_type=type(error).__name__)
    finally:
        _st.startup_briefing_thread = None


def start_startup_briefing():
    """Start one detached preparation pass after HTTP readiness is available."""
    with _briefing_lock:
        if _st.startup_briefing_thread and _st.startup_briefing_thread.is_alive():
            return False
        _st.startup_briefing_thread = threading.Thread(
            target=_prepare_worker, name="eva-startup-briefing", daemon=True
        )
        _st.startup_briefing_thread.start()
    return True