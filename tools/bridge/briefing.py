"""Non-blocking startup preparation for Eva's morning briefing."""

import concurrent.futures
import json
import os
import threading
import time
from datetime import timedelta, timezone
from email.utils import parsedate_to_datetime

from bridge import config as _cfg
from bridge import state as _st
from bridge.audit import audit_event

_briefing_lock = threading.RLock()
_LIVE_TOOL_READY_TIMEOUT_SECONDS = 10
_LIVE_SEARCH_MAX_AGE = timedelta(hours=36)


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
        state = _st.startup_briefing
        snapshot = dict(state)
        snapshot["sources"] = {
            name: dict(source) for name, source in (state.get("sources") or {}).items()
            if isinstance(source, dict)
        }
        return snapshot


def briefing_unavailable_sources(status=None):
    """Name required live sources that cannot support a complete briefing."""
    state = status if isinstance(status, dict) else briefing_status()
    sources = state.get("sources") or {}
    return [name for name in ("weather", "news", "markets") if (sources.get(name) or {}).get("status") != "ready"]


def briefing_prompt_context(allow_partial=False):
    """Return ready data, optionally including sources completed during preparation."""
    with _briefing_lock:
        state = _st.startup_briefing
        if state.get("status") != "ready" and not allow_partial:
            return ""
        sources = dict(state.get("sources") or {})
    lines = []
    for name in ("memory", "mail", "news", "weather", "markets"):
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


def _mail_source():
    """Summarize unread mail for accounts opted into the morning routine.

    Mail is never a required source: a locked or unreachable mailbox degrades the
    briefing rather than failing it.
    """
    try:
        from bridge.email_service import morning_mail_summary
        summary, unavailable = morning_mail_summary()
    except Exception as error:
        return "Mail could not be read: " + type(error).__name__, "failed"
    if summary:
        note = ""
        if unavailable:
            note = "\n(Not read: " + ", ".join(str(label)[:40] for label in unavailable[:5]) + ")"
        return summary + note, "ready"
    if unavailable:
        return "Locked or unreachable: " + ", ".join(str(label)[:40] for label in unavailable[:5]), "partial"
    return "No unread mail.", "ready"


def _briefing_weather_location():
    from bridge.cognition import _weather_user_profile_rows
    from bridge.skills import resolve_weather_location
    decision = resolve_weather_location(
        "",
        user_profile=_weather_user_profile_rows(),
        approved_approximate_location=str(
            os.environ.get("EVA_APPROVED_APPROXIMATE_LOCATION", "") or ""
        )[:120],
    )
    return str(decision.get("location") or "")[:120]


def _fresh_search_results(text, now=None):
    """Return dated search results retrieved recently enough for a live briefing."""
    text = str(text or "").strip()
    try:
        results = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(results, list):
        return []
    current = now or _cfg.utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    oldest = current - _LIVE_SEARCH_MAX_AGE
    fresh = []
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            published = parsedate_to_datetime(str(item.get("date") or "").strip())
        except (TypeError, ValueError, IndexError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if oldest <= published.astimezone(timezone.utc) <= current:
            fresh.append(item)
    return fresh


def _current_weather_results(text):
    """Return only structured current-condition receipts from the weather tool."""
    try:
        results = json.loads(str(text or "").strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(results, list):
        return []
    trusted = [
        item for item in results
        if isinstance(item, dict)
        and str(item.get("source") or "").strip() in {
            "Google Weather", "AccuWeather", "The Weather Channel",
            "The Weather Network", "National Weather Service",
        }
        and str(item.get("kind") or "") in {"current", "forecast"}
        and str(item.get("snippet") or "").strip()
        and str(item.get("retrieved_at") or "").strip()
    ]
    kinds = {str(item.get("kind") or "") for item in trusted}
    return trusted if kinds == {"current", "forecast"} else []


def _format_search_receipt(text, results=None):
    text = str(text or "").strip()
    if results is None:
        try:
            results = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return text
    if not isinstance(results, list):
        return text
    lines = []
    for item in results[:6]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("info") or "Result").strip()[:240]
        metadata = " - ".join(
            value for value in (
                str(item.get("source") or "").strip()[:80],
                str(item.get("date") or "").strip()[:80],
            ) if value
        )
        snippet = str(item.get("snippet") or item.get("body") or "").strip()[:500]
        url = str(item.get("url") or "").strip()[:500]
        lines.append("- " + title + ((" (" + metadata + ")") if metadata else ""))
        if snippet:
            lines.append("  " + snippet)
        if url.startswith(("https://", "http://")):
            lines.append("  " + url)
    return "\n".join(lines).strip() or text


def _search_receipt_has_results(results):
    return any(
        isinstance(item, dict)
        and bool(str(item.get("title") or "").strip())
        and bool(str(item.get("url") or item.get("snippet") or "").strip())
        for item in results
    )


def _live_source(name, prompt, timeout):
    if _st.local_mode:
        manager = _st.local_mcp_manager
        if not manager or not manager.alive:
            return "Local live-data tools are unavailable.", "failed"
        if name == "weather":
            location = _briefing_weather_location()
            if not location:
                return (
                    "I have not learned your weather location yet. Tell me 'I live in <city or region>' "
                    "and I will remember it for future briefings.",
                    "failed",
                )
            tool_name = "weather_current"
            arguments = {"location": location}
        elif name == "news":
            tool_name = "web_search_news"
            arguments = {"query": "United States world news when:1d", "max_results": 6}
        else:
            tool_name = "web_search_news"
            arguments = {"query": "S&P 500 Dow Nasdaq US stock market today", "max_results": 6}
        result = manager.call_tool(tool_name, arguments, timeout=timeout)
        text = str((result or {}).get("text") or "").strip()
        if name == "weather":
            weather_results = _current_weather_results(text)
            if weather_results:
                return _format_search_receipt(text, weather_results), "ready"
            return "Current weather conditions were not returned.", "failed"
        fresh_results = _fresh_search_results(text)
        if _search_receipt_has_results(fresh_results):
            return _format_search_receipt(text, fresh_results), "ready"
        try:
            receipt = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            receipt = None
        if isinstance(receipt, list):
            label = "headlines" if name == "news" else "market items"
            return "No timely current " + label + " were returned.", "failed"
        detail = _format_search_receipt(text) if text else str((result or {}).get("error") or "")
        return detail or "Local live-data retrieval returned no timely result.", "failed"
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
        mail_text, mail_status = _mail_source()
        _set_source("mail", mail_status, mail_text)
        live_sources = (
            ("news", "Provide a concise current morning news briefing with sources. Do not invent facts.", 30),
            ("weather", "Provide the current weather and short forecast for the learned user location, if available. Do not invent a location or facts.", 25),
            ("markets", "Provide concise recent U.S. market news for the most recently completed trading session, with sources. Do not invent prices.", 30),
        )
        if _wait_for_live_tools():
            if _st.local_mode:
                for name, prompt, timeout in live_sources:
                    try:
                        text, status = _live_source(name, prompt, timeout)
                    except Exception as error:
                        text, status = "Live source failed: " + type(error).__name__, "failed"
                    _set_source(name, status, text)
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(live_sources)) as executor:
                    futures = {
                        executor.submit(_live_source, name, prompt, timeout): name
                        for name, prompt, timeout in live_sources
                    }
                    for future in concurrent.futures.as_completed(futures):
                        name = futures[future]
                        try:
                            text, status = future.result()
                        except Exception as error:
                            text, status = "Live source failed: " + type(error).__name__, "failed"
                        _set_source(name, status, text)
        else:
            for name, _, _ in live_sources:
                _set_source(name, "failed", "Live data tools were not ready before preparation timed out.")

        with _briefing_lock:
            state = _st.startup_briefing
            required_sources = ("weather", "news", "markets")
            state["status"] = "ready" if all(
                (state["sources"].get(name) or {}).get("status") == "ready" for name in required_sources
            ) else "partial"
            state["completed_at"] = _cfg.to_utc_iso(_cfg.utc_now())
            state["summary"] = "Morning briefing prepared." if state["status"] == "ready" else "Morning briefing prepared with unavailable required sources."
            outcome = state["status"]
        audit_event("briefing.prepare", correlation_id, outcome, source_count=len(live_sources) + 2)
    except Exception as error:
        with _briefing_lock:
            _st.startup_briefing["status"] = "failed"
            _st.startup_briefing["completed_at"] = _cfg.to_utc_iso(_cfg.utc_now())
            _st.startup_briefing["summary"] = "Morning briefing preparation failed."
        audit_event("briefing.prepare", correlation_id, "failed", error_type=type(error).__name__)
    finally:
        with _briefing_lock:
            _st.startup_briefing_thread = None


def start_startup_briefing():
    """Start one detached preparation pass after HTTP readiness is available."""
    with _briefing_lock:
        if _st.startup_briefing_thread and _st.startup_briefing_thread.is_alive():
            return False
        _st.startup_briefing = _new_state()
        _st.startup_briefing["status"] = "preparing"
        _st.startup_briefing["started_at"] = _cfg.to_utc_iso(_cfg.utc_now())
        _st.startup_briefing_thread = threading.Thread(
            target=_prepare_worker,
            name="eva-startup-briefing",
            daemon=True,
        )
        _st.startup_briefing_thread.start()
    return True