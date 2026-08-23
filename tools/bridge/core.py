#!/usr/bin/env python3
"""
ACP Bridge Server for Eva
Bridges GitHub Copilot CLI's ACP (Agent Client Protocol) to HTTP
so the browser-based Eva UI can use Copilot models.

Requirements:
  - GitHub Copilot CLI installed and authenticated (`copilot auth login`)
  - Python 3.7+

Usage:
  python3 tools/acp_bridge.py                    # default port 8888
  python3 tools/acp_bridge.py --port 9999        # custom port
    EVA_ACP_PORT=9999 python3 tools/acp_bridge.py  # custom port via env
  python3 tools/acp_bridge.py --copilot-path /usr/local/bin/copilot

The server exposes a single endpoint:
  POST /v1/chat/completions
    Body: {"messages": [{"role": "user", "content": "Hello"}], "model": "copilot"}
    Returns: OpenAI-compatible chat completion JSON

  GET /v1/models
    Returns: List of available info (from copilot capabilities)

  GET /health
    Returns: {"status": "ok", "session_id": "..."}
"""

import argparse
import base64
import copy
import datetime
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler

# Centralized constants (paths, schemas, thresholds).
# Aliased with underscore prefix so existing code keeps working as-is.
from bridge import config as _cfg
from bridge import state as _st
from bridge.aig_request import normalize_aig_request
from bridge.aig_preflight import plan_aig_preflight
from bridge.http_routes import match_patch_route
from bridge.capabilities import runtime_capabilities, runtime_capability_prompt_view
from protected_memory import (
    ProtectedMemoryError,
    ProtectedVault,
    UnlockError,
    VaultLockedError,
    YkmanChallengeResponseProvider,
)

ACP_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
_AIG_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_LMSTUDIO_CONNECT_TIMEOUT_SECONDS = 10
_LMSTUDIO_READ_TIMEOUT_SECONDS = 900


def _parse_aig_backend(value):
    requested = str(value or "gpt-5.6-luna").strip()
    if requested.startswith("openai:"):
        model = requested[len("openai:"):].strip()
        if not _AIG_MODEL_RE.fullmatch(model):
            raise ValueError("Unsupported OpenAI model name")
        return "openai", model
    if not _AIG_MODEL_RE.fullmatch(requested):
        raise ValueError("Unsupported Eva backend model name")
    return "acp", requested


def _openai_chat_completions_url():
    default_url = "https://api.openai.com/v1/chat/completions"
    configured = os.environ.get("EVA_OPENAI_CHAT_COMPLETIONS_URL", "").strip()
    if not configured:
        return default_url
    parsed = urllib.parse.urlparse(configured)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("EVA_OPENAI_CHAT_COMPLETIONS_URL must use a loopback HTTP address")
    return configured


def _completion_token_limit(value, default=16384):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError("max_completion_tokens must be an integer")
    if isinstance(value, float):
        raise ValueError("max_completion_tokens must be an integer")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value.strip()):
        raise ValueError("max_completion_tokens must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("max_completion_tokens must be an integer")
    if parsed < 1 or parsed > 128000:
        raise ValueError("max_completion_tokens must be between 1 and 128000")
    return parsed


def _lmstudio_message_text(content):
    """Return text-only content for strict local chat templates."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


def _lmstudio_response_parts(message):
    """Separate local-model reasoning from its user-facing answer."""
    message = message if isinstance(message, dict) else {}
    content = str(message.get("content") or "")
    reasoning_parts = []
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            reasoning_parts.append(value.strip())
    details = message.get("reasoning_details")
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            value = detail.get("text") or detail.get("content")
            if isinstance(value, str) and value.strip():
                reasoning_parts.append(value.strip())

    content_lower = content.lower()
    visible_parts = []
    cursor = 0
    while cursor < len(content):
        start = content_lower.find("<think>", cursor)
        if start < 0:
            visible_parts.append(content[cursor:])
            break
        visible_parts.append(content[cursor:start])
        body_start = start + len("<think>")
        end = content_lower.find("</think>", body_start)
        if end < 0:
            visible_parts.append(content[start:])
            break
        thought = content[body_start:end].strip()
        if thought:
            reasoning_parts.append(thought)
        cursor = end + len("</think>")
    content = "".join(visible_parts)
    reasoning = "\n\n".join(dict.fromkeys(reasoning_parts)).strip()
    return content.strip(), reasoning


def _lmstudio_stream_deltas(response):
    """Yield content and reasoning deltas from an OpenAI-compatible SSE stream."""
    for raw_line in response.iter_lines(decode_unicode=True):
        line = str(raw_line or "").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choice = (event.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        content = delta.get("content") or ""
        if isinstance(content, list):
            content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
        reasoning = ""
        for key in ("reasoning_content", "reasoning", "thinking"):
            value = delta.get(key)
            if isinstance(value, str):
                reasoning += value
        yield str(content or ""), reasoning, choice.get("finish_reason") or ""


def _prepared_briefing_response(context, preparing=False, unavailable=None):
    """Return a truthful direct briefing when a local model tries to defer it."""
    context = str(context or "").strip()
    unavailable = [str(name) for name in (unavailable or []) if name]
    parts = ["Here is the briefing information available now."]
    if context:
        parts.append(context)
    else:
        parts.append("No prepared live briefing data is available right now.")
    if preparing:
        parts.append("Live news, weather, or market preparation is still finishing in the background.")
    elif unavailable:
        parts.append("Unavailable live sections: " + ", ".join(unavailable) + ".")
    return "\n\n".join(parts)


def _lmstudio_chat_messages(system_prompt, history, user_message, system_additions=None):
    """Build Qwen-compatible messages with exactly one leading system turn."""
    system_parts = [str(system_prompt or "")]
    turns = []
    for message in history or []:
        role = str(message.get("role") or "").lower()
        content = _lmstudio_message_text(message.get("content", "")).strip()
        if not content:
            continue
        if role in {"system", "developer"}:
            system_parts.append(content)
        elif role in {"user", "assistant"}:
            turns.append({"role": role, "content": content})
    for addition in system_additions or []:
        text = str(addition or "").strip()
        if text:
            system_parts.append(text)
    if not turns or turns[-1]["role"] != "user" or turns[-1]["content"] != str(user_message or ""):
        turns.append({"role": "user", "content": str(user_message or "")})
    return [{"role": "system", "content": "\n\n".join(system_parts)}] + turns


def _lmstudio_camera_request(user_message):
    """Return true only for explicit requests to inspect the physical scene."""
    text = str(user_message or "").lower()
    return bool(re.search(
        r"\b(?:camera|webcam)\b|"
        r"\b(?:look|see)\s+(?:at|through)\b|"
        r"\bwhat\s+(?:do\s+you|can\s+you)\s+see\b|"
        r"\bwhat\s+am\s+i\s+(?:holding|showing)\b|"
        r"\bshow\s+me\s+what\s+(?:i(?:'m|\s+am)|you)\s+(?:holding|showing|see)\b",
        text,
    ))


def _missing_tool_result_message(local_mode):
    return (
        "This request needs live local tools, but LocalMCP returned no result."
        if local_mode else "This request needs live tools, but no tool result was returned."
    )


def _openai_chat_payload(model, messages, reasoning_effort="", max_completion_tokens=16384):
    payload = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_completion_tokens,
    }
    if model.startswith("gpt-5.6") and reasoning_effort in {"none", "low", "medium", "high", "xhigh", "max"}:
        payload["reasoning_effort"] = reasoning_effort
    elif model == "gpt-5.2" and reasoning_effort in {"none", "low", "medium", "high", "xhigh"}:
        payload["reasoning_effort"] = reasoning_effort
    elif model.startswith("gpt-5") and reasoning_effort in {"minimal", "low", "medium", "high"}:
        payload["reasoning_effort"] = reasoning_effort
    elif re.match(r"^o\d", model) and reasoning_effort in {"low", "medium", "high"}:
        payload["reasoning_effort"] = reasoning_effort
    return payload


def _strip_quoted_signal_text(value):
    """Remove quoted spans with a bounded forward scan."""
    parts = []
    index = 0
    quote_end = ""
    while index < len(value):
        character = value[index]
        if quote_end:
            if character == quote_end:
                quote_end = ""
            index += 1
            continue
        if character == '"':
            quote_end = '"'
        elif character == "“":
            quote_end = "”"
        else:
            parts.append(character)
        index += 1
    return "".join(parts)


def _strip_signal_clause_filler(clause):
    value = clause.lstrip()
    for prefix in ("very good", "okay", "great", "then", "sure", "now", "but", "and", "ok"):
        if value == prefix or value.startswith(prefix + " ") or value.startswith(prefix + ","):
            return value[len(prefix):].lstrip(" ,")
    return value


def _has_signal_revocation(clause):
    normalized = " ".join(clause.lower().replace("’", "'").replace(",", " ").split())
    if "never mind" in normalized or "cancel" in normalized.split():
        return True
    words = normalized.replace("don't", "dont").split()
    if "dont" in words or ("do" in words and "not" in words):
        return True
    if "stop" not in words:
        return False
    following = words[words.index("stop") + 1:]
    while following and following[0] in {"that", "this", "the"}:
        following.pop(0)
    return bool(following and following[0] in {"send", "message", "signal"})


def _is_affirmative_signal_request(text):
    value = str(text or "").lower()
    comparable = _strip_quoted_signal_text(value)
    if not re.search(r"\bsignal\b", comparable):
        return False
    clauses = re.split(r"(?:[.;]|\bthen\b|\band\b)", comparable)
    authorized = False
    for raw_clause in clauses:
        clause = _strip_signal_clause_filler(raw_clause)
        if not clause:
            continue
        address = r"(?:(?:hey\s+)?eva[,.]?\s*)?"
        if re.search(r"^signal\s+me\s+(?:is|was|means)\b", clause):
            continue
        request_prefix = r"(?:(?:please\s+)?(?:can you|could you|would you|will you)\s+(?:please\s+)?|please\s+|i want you to\s+|i need you to\s+)?"
        command = r"(?:send|text|message|notify|ping)"
        prefix = r"^\s*" + address + request_prefix
        command_matches = bool(
            re.search(prefix + r"signal\s+me\b", clause)
            or re.search(prefix + r"use\s+signal\s+(?:to\s+)?(?:send|text|message|notify|ping|say|tell)\b", clause)
            or re.search(prefix + command + r"\b[\s\S]{0,100}\b(?:on|via|through|with)\s+signal\b", clause)
            or re.search(prefix + command + r"\s+(?:me\s+)?(?:a\s+)?signal\b", clause)
        )
        if (authorized or command_matches) and _has_signal_revocation(clause):
            return False
        if command_matches:
            authorized = True
    return authorized


def _strip_marker_blocks(text, marker):
    """Remove marker blocks with a single forward scan."""
    opening = "[[" + marker + "]]"
    closing = "[[/" + marker + "]]"
    parts = []
    cursor = 0
    while cursor < len(text):
        start = text.find(opening, cursor)
        if start < 0:
            parts.append(text[cursor:])
            break
        parts.append(text[cursor:start])
        close_at = text.find(closing, start + len(opening))
        if close_at >= 0:
            cursor = close_at + len(closing)
        else:
            line_end = text.find("\n", start + len(opening))
            cursor = len(text) if line_end < 0 else line_end
    return "".join(parts)


# Domain modules
from bridge.acp_client import (  # noqa: F401
    ACPClient,
    _acp_model_key,
    _acp_pool_touch,
    _acp_pool_register,
    _acp_pool_evict_if_needed,
    _reset_acp_pool,
    _ensure_acp_model,
    _acquire_acp_client,
)
from bridge.kusto import (  # noqa: F401
    _refresh_kusto_token,
    _inject_kusto_token,
    _ensure_kusto_token,
    _try_kusto_silent_auth,
    _split_kusto_seed_blocks,
    _is_kusto_schema_block,
    _normalize_kusto_cluster_url,
    _same_kusto_cluster,
    _MSALSilentCredential,
    _kusto_query_direct,
    _short_kusto_error,
    _kusto_query_with_error,
    _get_table_columns,
    _kusto_ingest_direct,
    _get_kusto_config,
    _get_locked_kusto_database,
    _capture_active_kusto_env,
    _persist_kusto_cluster,
    _load_cached_kusto_cluster,
)
from bridge.memory import (  # noqa: F401
    _resolve_memory_backend,
    _get_sqlite_mem,
    _get_protected_vault,
    _protected_memory_metadata,
    _set_memory_backend,
    _set_openai_key_from,
    _load_embedding_cache,
    _save_embedding_cache,
    _embed_texts,
    _cosine_similarity,
    _expand_query_terms,
    _memory_query,
    _memory_ingest,
    _memory_fts_search,
    _memory_available,
)
from bridge.cognition import (  # noqa: F401
    _enable_cognition,
    _with_launch_filter,
    _knowledge_scope_clause,
    _clean_explicit_fact_value,
    _normalize_explicit_children,
    _extract_explicit_user_facts,
    _explicit_user_fact_covers_candidate,
    _normalize_entity_candidate,
    _validate_entity_candidate,
    _classify_entity_candidate,
    _load_candidate_history,
    _maybe_promote_candidate,
    _track_candidate_observation,
    _extract_entity_candidates,
    _active_skill_rows_for_decision,
    _weather_user_profile_rows,
    _build_memory_context_sqlite,
    _post_response_reflection_sqlite,
    _build_memory_context,
    _post_response_reflection,
)
from bridge.background import (  # noqa: F401
    _utc_now,
    _to_utc_iso,
    _parse_kusto_datetime,
    _safe_kusto_string,
    _mark_user_activity,
    _background_status_dict,
    _background_kusto_context,
    _set_background_activity,
    _record_background_activity,
    _background_source_window,
    _background_conversations_query,
    _query_background_conversations,
    _background_summary_topics,
    _build_background_summary,
    _write_background_proposal,
    _background_memory_summary_exists,
    _apply_proposal_payload,
    _create_background_proposal_row,
    _existing_goal_checkin_ids,
    _build_daily_digest,
    _bg_period_exists,
    _bg_goals_query,
    _job_memory_consolidation,
    _job_goal_checkin,
    _job_daily_digest,
    _bg_to_float,
    _bg_to_int,
    _pending_proposal_exists,
    _bg_agent_prompt,
    _bg_watched_tickers,
    _job_knowledge_hygiene,
    _job_reflection_synthesis,
    _job_emotion_drift,
    _job_token_telemetry,
    _job_proactive_briefing,
    _job_market_snapshot,
    _job_sec_filing_watch,
    _job_space_weather_alert,
    _job_research_deepdive,
    _job_alert_watch,
    _run_background_tick,
    _bg_loop_worker,
    _start_bg_loop,
    _stop_bg_loop,
    _trigger_background_run_once,
    _background_proposal_payload,
    _background_proposal_update_row,
)
from bridge.briefing import briefing_prompt_context, briefing_status, briefing_unavailable_sources, start_startup_briefing
from bridge.audit import audit_event
from bridge.model_policy import select_model_policy
from bridge.telemetry import (  # noqa: F401
    _StdoutTee,
    _log_ring_add,
    _install_log_tee,
    _telemetry_clip,
    _telemetry_emit,
    _verbose_debug_emit,
    _percentile,
    _telemetry_summarize,
)
from bridge.alerts import (  # noqa: F401
    _alerts_default_doc,
    _load_alerts,
    _save_alerts,
    _alert_clip,
    _sanitize_alert_rule,
    _sanitize_alert_settings,
    _alert_cooldown_elapsed,
    _alert_build_prompt,
    _alert_salience,
    _notify_count_last_hour,
    _notify_in_quiet_hours,
    _notify_enqueue,
    _notify_mark_seen,
    _signal_send,
)
from bridge.cron import (  # noqa: F401
    _load_cron_tasks,
    _save_cron_tasks,
    _parse_cron_expr,
    _cron_matches,
    _cron_next_run,
    _cron_tick,
    _cron_execute_task,
    _push_notification,
)
from bridge.skills import (  # noqa: F401
    _safe_external_url,
    _http_get_text,
    _github_raw_candidates,
    _skill_source_label,
    _fetch_skill_source,
    _parse_evarise_json,
    _normalize_skill_draft,
    _normalize_skill_category,
    _evarise_skill,
    skill_execution_decision,
    skill_live_capabilities,
    resolve_weather_location,
    build_weather_retrieval_prompt,
    normalize_skill_config,
)
from skills import execute_bounded_skill
from bridge.utils import (  # noqa: F401
    _env_truthy,
    _is_loopback_bind,
    _valid_artifact_name,
    _safe_content_type,
    _is_local_or_private,
    _validate_lmstudio_base_url,
    _sanitize_mcp_for_persist,
    _persist_mcp_config,
    _load_persisted_mcp_config,
    _load_client_prefs,
    _save_client_prefs,
    _subagent_worker,
    _effective_routing_message,
    _classify_request_type,
    _classify_fast_route,
    _is_passive_memory_recall,
    _passive_recall_session_key,
    _needs_acp_preflight,
    _select_acp_tool_profile,
    _MEMORY_CAPTURE_DIRECTIVE,
)
from bridge.learning import (  # noqa: F401
    create_signal as _create_learning_signal,
    delete_signals as _delete_learning_signals,
    feedback_effect as _learning_feedback_effect,
    get_consent as _get_learning_consent,
    list_signals as _list_learning_signals,
    mark_applied as _mark_learning_applied,
    update_consent as _update_learning_consent,
)
from bridge.memory_model import KustoMemoryModel, MemoryModel
from bridge.workspaces import WorkspaceError, WorkspaceStore

# Constants needed by BridgeHandler (imported from config)
_LOG_RING_MAX = _cfg.LOG_RING_MAX
_NOTIFY_RING_MAX = _cfg.NOTIFY_RING_MAX
_ARTIFACTS_DIR = _cfg.ARTIFACTS_DIR
_GOAL_CATEGORIES = _cfg.GOAL_CATEGORIES
_GOAL_STATUSES = _cfg.GOAL_STATUSES
_GOAL_COLUMNS = _cfg.GOAL_COLUMNS
_GOALS_LATEST_QUERY = _cfg.GOALS_LATEST_QUERY
_SKILL_STATUSES = _cfg.SKILL_STATUSES
_SKILL_CATEGORIES = _cfg.SKILL_CATEGORIES
_SKILL_COLUMNS = _cfg.SKILL_COLUMNS
_SKILLS_LATEST_QUERY = _cfg.SKILLS_LATEST_QUERY
_BG_PROPOSAL_STATUSES = _cfg.BG_PROPOSAL_STATUSES
_BG_APPLY_TABLES = _cfg.BG_APPLY_TABLES
_ALERT_TYPES = _cfg.ALERT_TYPES
_ALERT_CHANNELS = _cfg.ALERT_CHANNELS
_TELEMETRY_RING_MAX = _cfg.TELEMETRY_RING_MAX
_BG_PROPOSAL_COLUMNS = _cfg.BG_PROPOSAL_COLUMNS
_SUBAGENT_MAX = 4
_SUBAGENT_ACTIVE_STATUSES = {"starting", "waiting", "running", "steering", "finalizing"}
_AGENT_ACTIVE_STATUSES = _SUBAGENT_ACTIVE_STATUSES | {"awaiting_confirmation", "awaiting_input"}


def _workspace_store():
    with _st.workspace_lock:
        if _st.workspace_store is None:
            _st.workspace_store = WorkspaceStore(_cfg.EVA_CONFIG_DIR)
        return _st.workspace_store


def _subagent_active_count(tasks=None):
    active_tasks = tasks if tasks is not None else _st.subagent_tasks
    return sum(
        1 for task in active_tasks.values()
        if task.get("status") in _SUBAGENT_ACTIVE_STATUSES
    )


def _prompt_budget_fields(value):
    """Flatten numeric prompt-budget metadata without retaining prompt text."""
    if not isinstance(value, dict):
        return {}
    fields = {}
    for source, target in (
        ("estimatedTokens", "prompt_estimated_tokens"),
        ("inputMessages", "prompt_input_messages"),
        ("outputMessages", "prompt_output_messages"),
        ("droppedMessages", "prompt_dropped_messages"),
        ("dedupedMessages", "prompt_deduped_messages"),
    ):
        number = value.get(source)
        if isinstance(number, (int, float)) and not isinstance(number, bool):
            fields[target] = max(0, round(number, 1))
    components = value.get("components")
    if isinstance(components, dict):
        for name in ("pinned", "summary", "recent", "actions", "corrections", "unresolved"):
            item = components.get(name)
            if not isinstance(item, dict):
                continue
            for metric in ("chars", "tokens"):
                number = item.get(metric)
                if isinstance(number, (int, float)) and not isinstance(number, bool):
                    fields[f"prompt_{name}_{metric}"] = max(0, round(number, 1))
    return fields


def _reserve_subagent_task(task):
    """Atomically reserve capacity and register a subagent task."""
    with _st.subagent_lock:
        if _subagent_active_count() >= _SUBAGENT_MAX:
            return False
        _st.subagent_tasks[task["id"]] = task
        return True


def _public_subagent_task(task):
    return {
        key: value for key, value in task.items()
        if key != "thread" and not key.startswith("_")
    }


def _scope_subagent_task_to_workspace(task):
    """Attach a generic agent task to an isolated Eva Ready worktree."""
    if task.get("coding_run_id"):
        return
    store = _workspace_store()
    requested_project_id = str(task.get("workspace_project_id") or "").strip()[:120]
    try:
        project = store.get_project(requested_project_id) if requested_project_id else store.ensure_eva_ready_project()
    except WorkspaceError:
        project = store.ensure_eva_ready_project()
    objective = "Autonomous agent task: " + str(task.get("label") or "Subagent") + "\n\n" + str(task.get("prompt") or "")
    run = store.create_run(
        project["id"], objective, primary_session_id=task.get("session_id", ""),
        model_policy=task.get("model", ""), auto_approve=True,
    )
    try:
        checkout = run["checkout"]
        workspace_mcp_config = store.mcp_config_for_run(run["id"])
        workspace_mcp_prefix = "workspace-" + str(run.get("project_id") or "workspace").replace("-", "")[:12] + "-"
        task.update({
            "coding_run_id": run["id"],
            "checkout_id": checkout["id"],
            "capability_policy": "workspace_auto",
            "workspace_scoped": True,
            "_cwd": store.validated_checkout_path(checkout["id"]),
            "_workspace_mcp_config": {
                workspace_mcp_prefix + name: config for name, config in workspace_mcp_config.items()
            },
            "_created_workspace_scope": True,
        })
        store.create_agent_run(
            task["id"], run["id"], checkout["id"], "agent:" + task["id"], task["capability_policy"]
        )
    except Exception:
        try:
            store.discard_run(run["id"], confirm_dirty=True)
        except WorkspaceError:
            pass
        raise


def _discard_subagent_workspace_scope(task, reason):
    """Remove a never-started generic agent worktree after a startup failure."""
    if not task.get("_created_workspace_scope"):
        return
    try:
        store = _workspace_store()
        store.update_agent_run(task["id"], "cancelled", reason)
        store.discard_run(task["coding_run_id"], confirm_dirty=True)
    except WorkspaceError:
        pass


def _dispatch_workspace_run(run):
    """Start one implementation agent in the run's bridge-resolved worktree."""
    existing_agent = run.get("agent") or {}
    if existing_agent.get("status") in {"starting", "running", "steering"}:
        with _st.subagent_lock:
            existing_task = _st.subagent_tasks.get(existing_agent.get("id"))
            if existing_task and existing_task.get("status") in _SUBAGENT_ACTIVE_STATUSES:
                return _public_subagent_task(existing_task)
        _workspace_store().update_agent_run(
            existing_agent["id"], "error", "Agent process ended before the coding run completed."
        )
    checkout = run.get("checkout") or {}
    checkout_path = _workspace_store().validated_checkout_path(checkout.get("id"))
    workspace_mcp_config = _workspace_store().mcp_config_for_run(run["id"])
    _verbose_debug_emit("workspace_mcp", enabled_module_count=len(workspace_mcp_config))
    workspace_mcp_prefix = "workspace-" + str(run.get("project_id") or "workspace").replace("-", "")[:12] + "-"
    workspace_mcp_config = {
        workspace_mcp_prefix + name: config for name, config in workspace_mcp_config.items()
    }
    task_id = "sub-" + uuid.uuid4().hex[:8]
    objective = str(run.get("objective") or "").strip()
    github_issue_delivery_required = bool(
        run.get("auto_approve")
        and re.search(r"\b(?:issue|issues)\b", objective, re.IGNORECASE)
        and re.search(r"\b(?:close|comment|create|leave|post|publish|reopen|submit|write)\b", objective, re.IGNORECASE)
    )
    github_pr_delivery_required = bool(
        run.get("auto_approve")
        and re.search(r"\b(?:pull\s+request|pr)\b", objective, re.IGNORECASE)
        and re.search(r"\b(?:create|open|raise|submit|publish|address|fix|resolve|remediate|update)\b", objective, re.IGNORECASE)
    )
    github_delivery_required = github_issue_delivery_required or github_pr_delivery_required
    github_delivery_kind = "pull_request" if github_pr_delivery_required else ("issue" if github_issue_delivery_required else "")
    github_issue_state = ""
    if github_issue_delivery_required:
        if re.search(r"\breopen\b", objective, re.IGNORECASE):
            github_issue_state = "open"
        elif re.search(r"\bclose\b", objective, re.IGNORECASE):
            github_issue_state = "closed"
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    github_delivery_prompt = ""
    if github_pr_delivery_required:
        github_delivery_prompt = (
            "This objective requires a real GitHub pull request, not only a local branch, commit, or draft report. "
            "You are authorized to use authenticated `git` and `gh` operations in this auto-approved workspace run. "
            "Inspect and complete the requested repository changes, run focused validation, commit them on the assigned "
            "branch, push that branch, and create or update a pull request against the repository's default branch. "
            "Do not stop after creating the branch. Verify the pull request with `gh pr view`, then end the final report "
            "with `Submitted: <github-pull-request-url>`.\n\n"
        )
    elif github_issue_delivery_required:
        close_only_instruction = (
            "This is a close-only request: do not implement, commit, or otherwise resolve the issue body first. "
            "Inspect enough context to identify the requested issue, close it directly, and verify its state. "
            if github_issue_state == "closed" and not re.search(
                r"\b(?:fix|implement|resolve|address|change|update)\b", objective, re.IGNORECASE
            ) else ""
        )
        github_delivery_prompt = (
            "This objective requires a real GitHub Issues side effect, not only a draft or local report. "
            "You are authorized to use the authenticated `gh` CLI in this auto-approved workspace run. "
            + close_only_instruction +
            "Perform the requested issue action exactly; an explicit close or reopen request must update that issue state "
            "after any requested repository work is complete. "
            "Resolve the repository from the origin remote. If the objective names an issue number or URL, comment there. "
            "Otherwise inspect open issues and use a clearly matching target; if none exists, create a new issue containing "
            "the requested report. If `gh` needs a body file, create it at a workspace-relative path and remove it after "
            "submission; never use `/tmp` or another path outside the assigned worktree. Do not stop after preparing text. "
            "Verify the created issue or comment with `gh`, then end "
            "the final report with `Submitted: <github-issue-or-comment-url>`.\n\n"
        )
    full_prompt = (
        "You are Eva's coding implementation agent. Work autonomously in the assigned Git worktree. "
        "Do not wait for further approval. Inspect the repository, implement the objective, run focused tests, "
        "and issue ordinary local test, build, lint, typecheck, and diagnostic commands as one direct executable "
        "per tool call without shell operators or command chaining. Do not install dependencies unless explicitly requested. "
        "Leave all requested files in the worktree. Do not access or modify paths outside the assigned "
        "worktree. Do not launch a browser, desktop, camera, external application, or new window, and do not "
        "emit Eva browser, desktop, camera, or renderer action markers. For remote operations not explicitly authorized "
        "by this prompt, use only an enabled workspace MCP tool and report unavailable capabilities clearly. "
        "Finish with a concise report of changed files, tests, and any remaining issue.\n\n"
        + github_delivery_prompt +
        "Objective:\n" + objective
    )
    task = {
        "id": task_id,
        "label": "Workspace: " + objective[:96],
        "prompt": objective[:1200],
        "model": str(run.get("model_policy") or "")[:120],
        "status": "starting",
        "result": None,
        "started_at": now_iso,
        "ended_at": None,
        "session_id": str(run.get("primary_session_id") or "")[:120],
        "group_id": "workspace-" + run["id"],
        "depends_on": [],
        "signal_on_complete": False,
        "signal_status": "",
        "steer_queue": [],
        "steer_history": [],
        "coding_run_id": run["id"],
        "checkout_id": checkout["id"],
        "capability_policy": "workspace_auto" if run.get("auto_approve") else "workspace_write",
        "requires_github_delivery": github_delivery_required,
        "required_github_delivery_kind": github_delivery_kind,
        "required_github_issue_state": github_issue_state,
        "required_github_repository": str((run.get("project") or {}).get("name") or "").strip(),
        "_cwd": checkout_path,
        "_workspace_mcp_config": workspace_mcp_config,
    }
    if not _reserve_subagent_task(task):
        raise WorkspaceError(f"Agent capacity is full ({_SUBAGENT_MAX} active agents).")
    try:
        _workspace_store().create_agent_run(
            task_id,
            run["id"],
            checkout["id"],
            "agent:" + task_id,
            task["capability_policy"],
        )
        thread = threading.Thread(
            target=_subagent_worker,
            args=(task_id, full_prompt, task["label"], task["model"]),
            name=f"workspace-agent-{task_id}",
            daemon=True,
        )
        task["thread"] = thread
        thread.start()
        return _public_subagent_task(task)
    except Exception as error:
        with _st.subagent_lock:
            _st.subagent_tasks.pop(task_id, None)
        try:
            _workspace_store().update_agent_run(task_id, "error", str(error))
        except Exception:
            pass
        raise WorkspaceError("Workspace agent could not start: " + str(error))


def _revoke_missing_local_mcp_servers(config, manager):
    """Forget selections that cannot start because their executable is absent."""
    missing = {
        name for name, reason in getattr(manager, "start_failures", {}).items()
        if reason == "command_not_found" and name in config
    }
    if not missing:
        return dict(config)
    retained = {name: value for name, value in config.items() if name not in missing}
    _persist_mcp_config(retained)
    _st.configured_mcp_config = copy.deepcopy(retained)
    print(f"[LocalMCP] Disabled unavailable servers: {', '.join(sorted(missing))}")
    return retained


def _reserve_subagent_batch(tasks):
    """Atomically reserve every task in a batch or none of them."""
    with _st.subagent_lock:
        if _subagent_active_count() + len(tasks) > _SUBAGENT_MAX:
            return False
        for task in tasks:
            _st.subagent_tasks[task["id"]] = task
        return True


def _start_reserved_subagent_batch(tasks, thread_factory=threading.Thread):
    """Start all batch workers behind a gate; roll back if any thread fails to start."""
    start_gate = threading.Event()
    abort_start = threading.Event()
    threads = []
    try:
        for task in tasks:
            thread = thread_factory(
                target=_subagent_worker,
                args=(task["id"], task["_full_prompt"], task["label"], task["model"], start_gate, abort_start),
                name=f"subagent-{task['id']}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
    except Exception:
        abort_start.set()
        start_gate.set()
        with _st.subagent_lock:
            for task in tasks:
                _st.subagent_tasks.pop(task["id"], None)
        return False
    for task in tasks:
        task.pop("_full_prompt", None)
    start_gate.set()
    return True


def _select_subagent_overview_tasks(tasks, limit=20):
    """Keep every active task visible, followed by the newest inactive history."""
    all_tasks = list(tasks.values())
    active_tasks = [
        task for task in all_tasks
        if task.get("status") in _SUBAGENT_ACTIVE_STATUSES
    ]
    inactive_tasks = [
        task for task in all_tasks
        if task.get("status") not in _SUBAGENT_ACTIVE_STATUSES
    ]
    inactive_tasks.sort(key=lambda task: str(task.get("ended_at") or task.get("started_at") or ""))
    remaining = max(0, limit - len(active_tasks))
    recent_inactive = inactive_tasks[-remaining:] if remaining else []
    return len(active_tasks), active_tasks + recent_inactive


def _select_active_history(items, active_statuses, limit):
    """Keep active records ahead of a bounded tail of inactive history."""
    active_items = [item for item in items if item.get("status") in active_statuses]
    inactive_items = [item for item in items if item.get("status") not in active_statuses]
    remaining = max(0, limit - len(active_items))
    recent_inactive = inactive_items[-remaining:] if remaining else []
    return active_items + recent_inactive


def _select_agent_payload(items, limit=30):
    """Keep every active agent, then fill the payload with recent completions."""
    pinned_items = [item for item in items if item.get("kind") == "eva"]
    active_items = [
        item for item in items
        if item.get("kind") != "eva" and item.get("status") in _AGENT_ACTIVE_STATUSES
    ]
    inactive_items = [
        item for item in items
        if item.get("kind") != "eva" and item.get("status") not in _AGENT_ACTIVE_STATUSES
    ]
    active_items.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
    inactive_items.sort(
        key=lambda item: str(item.get("ended_at") or item.get("started_at") or ""),
        reverse=True,
    )
    remaining = max(0, limit - len(pinned_items) - len(active_items))
    return pinned_items + active_items + inactive_items[:remaining]


def _dismiss_subagent_task(task_id):
    """Remove a terminal task unless an active task still depends on it."""
    with _st.subagent_lock:
        task = _st.subagent_tasks.get(task_id)
        if not task:
            return False, "not_found"
        if task.get("status") not in ("done", "error", "cancelled"):
            return False, "active"
        dependent = next(
            (
                other for other in _st.subagent_tasks.values()
                if task_id in (other.get("depends_on") or [])
                and other.get("status") in _SUBAGENT_ACTIVE_STATUSES
            ),
            None,
        )
        if dependent:
            return False, "dependency"
        _st.subagent_tasks.pop(task_id, None)
        return True, ""


def _prepare_subagent_steer(task, instruction):
    """Mutate a task for an accepted steering request; return None at capacity."""
    task_is_active = task.get("status") in _SUBAGENT_ACTIVE_STATUSES
    if not task_is_active and _subagent_active_count() >= _SUBAGENT_MAX:
        return None
    task.setdefault("steer_queue", [])
    task.setdefault("steer_history", []).append({
        "instruction": instruction,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    if task_is_active:
        task["steer_queue"].append(instruction)
        return {"restart": False, "prompt": instruction}
    prior_result = str(task.get("result") or "")[:3000]
    prompt = (
        f"Continue the existing task '{task.get('label', 'subagent task')}'.\n"
        f"Previous result:\n{prior_result}\n\nNew direction:\n{instruction}"
    )
    task["status"] = "steering"
    task["ended_at"] = None
    task["signal_on_complete"] = False
    task["signal_status"] = ""
    return {"restart": True, "prompt": prompt}


def _knowledge_graph_snapshot(rows):
    """Project Knowledge rows into deterministic entity and fact graph records."""
    nodes = {
        "eva-root": {
            "id": "eva-root",
            "label": "Eva",
            "type": "core",
            "description": "Cognitive root and agent orchestrator",
        }
    }
    edges = []
    for row in rows or []:
        source_label = str(row.get("Entity", "") or "").strip()
        target_value = str(row.get("Value", "") or "").strip()
        relation = str(row.get("Relation", "related_to") or "related_to").strip()
        if not source_label or not target_value:
            continue
        source_lower = source_label.lower()
        if relation.lower() in ("mentioned", "candidate_mentioned", "recurring_topic"):
            continue
        if source_lower != "eva" and (
            source_lower in _cfg.ENTITY_IGNORE_WORDS or source_lower in _cfg.ENTITY_RESERVED_TERMS
        ):
            continue
        target_label = target_value
        if len(target_label) > 56:
            target_label = target_label[:53].rstrip() + "..."
        source_id = "eva-root" if source_lower == "eva" else "entity-" + hashlib.sha1(source_lower.encode()).hexdigest()[:12]
        target_key = relation.lower() + "\0" + target_value.lower()
        target_id = "fact-" + hashlib.sha1(target_key.encode()).hexdigest()[:12]
        if source_id != "eva-root":
            nodes[source_id] = {
                "id": source_id,
                "label": source_label,
                "type": "entity",
                "description": "Remembered entity",
            }
        nodes[target_id] = {
            "id": target_id,
            "label": target_label,
            "full_label": target_value[:240],
            "type": "fact",
            "source_label": source_label,
            "relation": relation.replace("_", " "),
            "confidence": float(row.get("Confidence", 0.0) or 0.0),
            "description": f"{source_label} · {relation.replace('_', ' ')}",
        }
        edges.append({
            "source": source_id,
            "target": target_id,
            "label": relation.replace("_", " "),
            "confidence": float(row.get("Confidence", 0.0) or 0.0),
            "type": "memory",
        })
    return {"nodes": list(nodes.values()), "edges": edges}


def _append_agent_topology(graph, tasks):
    """Add agent nodes plus Eva orchestration and inter-agent dependency edges."""
    task_by_id = {task.get("id"): task for task in tasks if task.get("id")}
    graph_node_ids = {node.get("id") for node in graph.get("nodes", [])}
    if "eva-root" not in graph_node_ids:
        graph.setdefault("nodes", []).insert(0, {
            "id": "eva-root", "label": "Eva", "type": "core",
            "description": "Cognitive root and agent orchestrator",
        })
    for task_id, task in task_by_id.items():
        task_node_id = "agent-" + task_id
        graph["nodes"].append({
            "id": task_node_id,
            "label": task.get("label", "Agent"),
            "type": "agent",
            "status": task.get("status", "unknown"),
            "model": task.get("model", "default") or "default",
            "group_id": task.get("group_id", ""),
            "result": str(task.get("result") or "")[:240],
            "description": "Isolated ACP agent session",
        })
        graph["edges"].append({
            "source": "eva-root",
            "target": task_node_id,
            "label": "orchestrates",
            "confidence": 1.0,
            "type": "orchestration",
        })
        for dependency_id in task.get("depends_on", []):
            if dependency_id not in task_by_id:
                continue
            graph["edges"].append({
                "source": "agent-" + dependency_id,
                "target": task_node_id,
                "label": "feeds",
                "confidence": 1.0,
                "type": "dependency",
            })
    return graph
_TELEMETRY_ENABLED = _st.telemetry_enabled
_BG_PROPOSALS_LATEST_QUERY = (
    "BackgroundProposals "
    "| extend _SortAt = coalesce(ReviewedAt, CreatedAt) "
    "| summarize arg_max(_SortAt, *) by ProposalId "
    "| project-away _SortAt"
)
_BG_JOBS_ENABLED = {}  # populated at import from background
_BG_JOBS = []  # populated at import from background


# Vision browser agent (Playwright is imported lazily inside the module, so this
# import never fails even when Playwright is not installed).
try:
    import browser_agent as _BROWSER_AGENT
except Exception as _ba_err:  # pragma: no cover - defensive
    _BROWSER_AGENT = None
    print(f"[Bridge] Browser agent module unavailable: {_ba_err}")

# Vision desktop agent (pyautogui is imported lazily inside the module).
try:
    import desktop_agent as _DESKTOP_AGENT
except Exception as _da_err:  # pragma: no cover - defensive
    _DESKTOP_AGENT = None
    print(f"[Bridge] Desktop agent module unavailable: {_da_err}")

# Camera presence sensor (OpenCV is imported lazily inside the worker process).
try:
    import camera_sense as _CAMERA
except Exception as _cam_err:  # pragma: no cover - defensive
    _CAMERA = None
    print(f"[Bridge] Camera sensor module unavailable: {_cam_err}")

# ---------------------------------------------------------------------------
# ACP Client — manages the copilot subprocess and JSON-RPC communication
# ---------------------------------------------------------------------------

def _sqlite_latest_skill_rows(memory):
    return memory.query(
        "SELECT SkillId, Name, Description, Category, Instructions, Tools, Tags, Config, Source, Status, CreatedAt, UpdatedAt "
        "FROM ("
        "SELECT rowid AS _rowid, Skills.*, ROW_NUMBER() OVER ("
        "PARTITION BY SkillId ORDER BY UpdatedAt DESC, rowid DESC"
        ") AS _latest FROM Skills"
        ") WHERE _latest = 1 AND Status != 'deleted' ORDER BY UpdatedAt DESC, _rowid DESC"
    )


def _skill_live_capability_snapshot():
    """Build the decision layer's bounded view from current bridge state."""
    local_tools = []
    local_capabilities = {
        "skills.docx", "skills.pdf", "skills.pptx", "skills.xlsx", "skills.mcp-builder",
    }
    manager = _st.local_mcp_manager
    if manager and manager.alive:
        for server_name, server in manager.servers.items():
            if not server.alive:
                continue
            for tool in server.tools:
                local_tools.append({
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "server": server_name,
                })
    acp_alive = bool(_st.acp_client and getattr(_st.acp_client, "alive", False))
    configured_paths = {}
    if acp_alive:
        configured_paths["web-search"] = True
        if os.path.isdir(_ARTIFACTS_DIR) and os.access(_ARTIFACTS_DIR, os.W_OK):
            configured_paths["file.download"] = True
    if acp_alive:
        for server_name in _st.configured_mcp_config.keys():
            normalized = str(server_name).casefold()
            if "weather" in normalized or "forecast" in normalized:
                configured_paths["weather-news"] = True
            if "data" in normalized or "finance" in normalized or "market" in normalized:
                configured_paths["data-retrieval"] = True
            if "web" in normalized or "search" in normalized:
                configured_paths["web-search"] = True
    return skill_live_capabilities(
        acp_alive=acp_alive,
        configured_data_paths=configured_paths,
        local_mcp_tools=local_tools,
        local_capabilities=local_capabilities,
        browser_available=_BROWSER_AGENT is not None,
        desktop_available=_DESKTOP_AGENT is not None,
    )


def _skill_execution_for_request(user_message, approved_approximate_location=""):
    """Resolve the skill, live tool, and Weather location for one request."""
    rows = _active_skill_rows_for_decision()
    decision = skill_execution_decision(user_message, rows, _skill_live_capability_snapshot())
    selected = next(
        (row for row in rows if str(row.get("SkillId", "")) == decision.get("selected_skill_id")),
        None,
    )
    location = {"location": "", "source": "unresolved"}
    if re.search(r"\b(?:weather|forecast|temperature|raining|snowing|humidity|wind speed)\b", str(user_message), re.IGNORECASE):
        location = resolve_weather_location(
            user_message,
            selected,
            _weather_user_profile_rows(),
            approved_approximate_location=approved_approximate_location,
        )
    return decision, selected, location

class BridgeHandler(BaseHTTPRequestHandler):
    """HTTP handler that bridges browser requests to ACP."""

    protocol_version = "HTTP/1.1"

    def finish(self):
        try:
            super().finish()
        finally:
            # ThreadingHTTPServer creates short-lived request threads. Close
            # only this thread's SQLite connection; bridge/background threads
            # retain their own independent connections.
            if _st.sqlite_mem is not None:
                _st.sqlite_mem.close()

    def _new_stream_state(self, route, model):
        return {
            "request_start": _to_utc_iso(_utc_now()),
            "started_at": time.perf_counter(),
            "route": route,
            "model": model or "unknown",
            "started": False,
            "finished": False,
            "disconnected": False,
            "chunk_count": 0,
            "first_chunk_at": None,
        }

    def _stream_start(self, state):
        if state["disconnected"]:
            return False
        if state["started"]:
            return True
        try:
            self.close_connection = True
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.flush()
            state["started"] = True
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            state["disconnected"] = True
            return False

    def _stream_event(self, state, event):
        if state["disconnected"] or not self._stream_start(state):
            return False
        try:
            self.wfile.write((json.dumps(event, ensure_ascii=True) + "\n").encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            state["disconnected"] = True
            return False

    def _stream_chunk(self, state, text):
        if not text:
            return
        state["chunk_count"] += 1
        if state["first_chunk_at"] is None:
            state["first_chunk_at"] = time.perf_counter()
        self._stream_event(state, {"type": "chunk", "text": text})

    def _stream_reasoning(self, state, text):
        if text:
            self._stream_event(state, {"type": "reasoning", "text": text})

    def _stream_finish(self, state, response):
        if state["finished"]:
            return
        state["finished"] = True
        content = ((response.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        if not state["chunk_count"] and content:
            self._stream_chunk(state, content)
        if not state["disconnected"]:
            self._stream_event(state, {
                "type": "done",
                "response": response,
                "metrics": {
                    "route": state["route"],
                    "model": state["model"],
                    "chunk_count": state["chunk_count"],
                },
            })
            try:
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                state["disconnected"] = True
        now = time.perf_counter()
        first_chunk_ms = (
            round((state["first_chunk_at"] - state["started_at"]) * 1000.0, 1)
            if state["first_chunk_at"] is not None else None
        )
        total_ms = round((now - state["started_at"]) * 1000.0, 1)
        _telemetry_emit(
            "stream_turn",
            request_start=state["request_start"],
            first_chunk_ms=first_chunk_ms,
            ttft_ms=first_chunk_ms,
            completion_ms=total_ms,
            total_ms=total_ms,
            chunk_count=state["chunk_count"],
            route=state["route"],
            model=state["model"],
            disconnected=state["disconnected"],
        )

    def _stream_error(self, state, message, status=500):
        if state["finished"]:
            return
        state["finished"] = True
        self._stream_event(state, {
            "type": "error",
            "status": int(status),
            "message": str(message or "Streaming request failed")[:240],
        })

    def _trusted_origin(self):
        return self._normalized_cors_origin() is not None

    def _normalized_cors_origin(self):
        origin = self.headers.get("Origin", "")
        if not origin:
            return "*"
        if origin == "null":
            return "null"
        if "\r" in origin or "\n" in origin or "\x00" in origin:
            return None
        try:
            parsed = urllib.parse.urlparse(origin)
            if parsed.scheme == "file":
                return "null"
            hostname = (parsed.hostname or "").lower()
            if parsed.scheme not in ("http", "https") or hostname not in ("localhost", "127.0.0.1", "::1"):
                return None
            host_text = "[::1]" if hostname == "::1" else hostname
            port = parsed.port
            return f"{parsed.scheme}://{host_text}" + (f":{port}" if port else "")
        except (TypeError, ValueError):
            return None

    def _require_bridge_capability(self):
        expected_token = os.environ.get("EVA_BRIDGE_TOKEN", "")
        supplied_token = self.headers.get("Authorization", "")
        if not expected_token or not hmac.compare_digest(supplied_token, "Bearer " + expected_token):
            if self.command not in ("GET", "HEAD", "OPTIONS"):
                self.close_connection = True
            self._json_response(401, {"error": {"message": "Bridge authorization failed"}})
            return False
        if not self._trusted_origin():
            if self.command not in ("GET", "HEAD", "OPTIONS"):
                self.close_connection = True
            self._json_response(403, {"error": {"message": "Request origin is not allowed"}})
            return False
        return True

    def _require_private_route(self):
        """Authorize private routes while preserving direct loopback development mode."""
        if os.environ.get("EVA_BRIDGE_TOKEN", "").strip():
            return self._require_bridge_capability()
        if not _is_loopback_bind():
            if self.command not in ("GET", "HEAD", "OPTIONS"):
                self.close_connection = True
            self._json_response(403, {"error": {"message": "private bridge routes are restricted to localhost"}})
            return False
        if self._normalized_cors_origin() == "null":
            if self.command not in ("GET", "HEAD", "OPTIONS"):
                self.close_connection = True
            self._json_response(403, {"error": {"message": "file-origin bridge requests require Eva Standalone authorization"}})
            return False
        return True

    def _require_workspace_capability(self):
        """Workspace paths are privileged and may only be requested by Electron main."""
        expected_capability = os.environ.get("EVA_WORKSPACE_CAPABILITY", "")
        supplied_capability = self.headers.get("X-Eva-Workspace-Capability", "")
        if not expected_capability or not hmac.compare_digest(supplied_capability, expected_capability):
            if self.command not in ("GET", "HEAD", "OPTIONS"):
                self.close_connection = True
            self._json_response(403, {"error": {"message": "Workspace authorization failed"}})
            return False
        return True

    def _cors_headers(self):
        normalized_origin = self._normalized_cors_origin()
        self.send_header("Access-Control-Allow-Origin", normalized_origin or "null")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Eva-Workspace-Capability")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path).path
        if parsed_path not in ("/health", "/v1/models") and not self._require_private_route():
            return
        if parsed_path.startswith("/v1/workspaces/") and not self._require_workspace_capability():
            return
        if parsed_path == "/health":
            self._health()
        elif parsed_path == "/v1/doctor":
            self._doctor()
        elif parsed_path == "/v1/models":
            self._models()
        elif parsed_path == "/v1/memory/backend":
            self._memory_backend_get()
        elif parsed_path == "/v1/runtime/capabilities":
            self._runtime_capabilities()
        elif parsed_path == "/v1/mcp":
            self._mcp_status()
        elif parsed_path == "/v1/mcp/config":
            self._mcp_persisted_config()
        elif parsed_path == "/v1/cron":
            self._cron_list()
        elif parsed_path == "/v1/subagent/status":
            self._subagent_status()
        elif parsed_path == "/v1/agents/overview":
            self._agents_overview()
        elif parsed_path == "/v1/telemetry":
            self._telemetry_report()
        elif parsed_path == "/v1/logs":
            self._logs_view()
        elif parsed_path == "/v1/goals":
            self._goals_list()
        elif parsed_path == "/v1/skills":
            self._skills_list()
        elif parsed_path == "/v1/background/status":
            self._background_status()
        elif parsed_path == "/v1/briefing/status":
            self._briefing_status()
        elif parsed_path == "/v1/background/proposals":
            self._background_proposals()
        elif parsed_path == "/v1/background/activity":
            self._background_activity()
        elif parsed_path == "/v1/alerts":
            self._alerts_list()
        elif parsed_path == "/v1/email/accounts":
            self._email_accounts_get()
        elif parsed_path == "/v1/email/messages":
            self._email_messages_list()
        elif parsed_path == "/v1/email/exim-status":
            self._email_exim_status()
        elif parsed_path == "/v1/notifications":
            self._notifications_list()
        elif parsed_path == "/v1/workspaces/projects":
            self._workspace_projects_list()
        elif re.fullmatch(r"/v1/workspaces/projects/[^/]+/files", parsed_path):
            self._workspace_project_files_list(urllib.parse.unquote(parsed_path.split("/v1/workspaces/projects/", 1)[1].rsplit("/files", 1)[0]))
        elif parsed_path == "/v1/workspaces/runs":
            self._workspace_runs_list()
        elif parsed_path == "/v1/workspaces/assets":
            self._workspace_assets_list()
        elif re.fullmatch(r"/v1/workspaces/runs/[^/]+", parsed_path):
            self._workspace_run_get(urllib.parse.unquote(parsed_path.rsplit("/", 1)[1]))
        elif re.fullmatch(r"/v1/workspaces/checkouts/[^/]+/status", parsed_path):
            self._workspace_checkout_status(urllib.parse.unquote(parsed_path.split("/v1/workspaces/checkouts/", 1)[1].rsplit("/status", 1)[0]))
        elif parsed_path == "/v1/memory/context":
            self._memory_context()
        elif parsed_path == "/v1/memory/inspector":
            self._memory_inspector()
        elif re.fullmatch(r"/v1/memory/atoms/[^/]+", parsed_path):
            self._memory_atom_detail(urllib.parse.unquote(parsed_path.rsplit("/", 1)[1]))
        elif parsed_path == "/v1/protected-memory/status":
            self._protected_memory_status()
        elif re.fullmatch(r"/v1/protected-memory/(records|artifacts)/[^/]+", parsed_path):
            kind, record_id = parsed_path.split("/v1/protected-memory/", 1)[1].split("/", 1)
            self._protected_memory_read(kind, urllib.parse.unquote(record_id))
        elif parsed_path == "/v1/data/retrieve":
            self._data_retrieve()
        elif parsed_path == "/v1/browser/status":
            self._browser_status()
        elif parsed_path == "/v1/browser/screenshot":
            self._browser_screenshot()
        elif parsed_path == "/v1/desktop/status":
            self._desktop_status()
        elif parsed_path == "/v1/desktop/screenshot":
            self._desktop_screenshot()
        elif parsed_path == "/v1/camera/status":
            self._camera_status()
        elif parsed_path == "/v1/camera/frame":
            self._camera_frame()
        elif parsed_path == "/v1/prefs":
            self._prefs_get()
        elif parsed_path == "/v1/learning/consent":
            self._learning_consent_get()
        elif parsed_path == "/v1/learning/signals":
            self._learning_signals_list()
        elif parsed_path == "/v1/acp/permissions":
            self._acp_permissions_list()
        elif parsed_path == "/v1/mode":
            self._get_mode()
        elif parsed_path == "/v1/files":
            self._list_artifacts()
        elif parsed_path.startswith("/v1/files/"):
            requested_name = urllib.parse.unquote(parsed_path.split("/v1/files/", 1)[1])
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if qs.get("open"):
                self._open_artifact(requested_name)
            else:
                self._serve_artifact(requested_name)
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path).path
        if not self._require_private_route():
            return
        if parsed_path.startswith("/v1/workspaces/") and not self._require_workspace_capability():
            return
        if parsed_path == "/v1/chat/completions":
            self._chat_completions()
        elif parsed_path == "/v1/mcp/configure":
            self._mcp_configure()
        elif parsed_path == "/v1/memory/reflect":
            self._memory_reflect()
        elif parsed_path == "/v1/memory/remember-location":
            self._memory_remember_location()
        elif parsed_path == "/v1/memory/start-fresh":
            self._memory_start_fresh()
        elif parsed_path == "/v1/memory/atoms":
            self._memory_atom_create()
        elif parsed_path == "/v1/memory/traits":
            self._memory_trait_create()
        elif parsed_path == "/v1/memory/growth-proposals":
            self._growth_proposal_create()
        elif re.fullmatch(r"/v1/memory/growth-proposals/[^/]+/(approve|reject)", parsed_path):
            self._growth_proposal_review(parsed_path)
        elif parsed_path == "/v1/memory/backend":
            self._memory_backend_set()
        elif parsed_path == "/v1/protected-memory/enroll":
            self._protected_memory_enroll()
        elif parsed_path == "/v1/protected-memory/unlock":
            self._protected_memory_unlock()
        elif parsed_path == "/v1/protected-memory/lock":
            self._protected_memory_lock()
        elif parsed_path == "/v1/protected-memory/records":
            self._protected_memory_write("memory")
        elif parsed_path == "/v1/protected-memory/artifacts":
            self._protected_memory_write("artifact")
        elif parsed_path == "/v1/aig/chat":
            self._aig_chat()
        elif parsed_path == "/v1/briefing/refresh":
            self._briefing_refresh()
        elif parsed_path == "/v1/translate":
            self._translate()
        elif parsed_path == "/v1/telemetry":
            self._telemetry_ingest()
        elif parsed_path == "/v1/audit/event":
            self._audit_event_ingest()
        elif parsed_path == "/v1/cron":
            self._cron_create()
        elif parsed_path == "/v1/skills/auto-learn":
            self._skills_auto_learn()
        elif parsed_path == "/v1/skills/execute":
            self._skills_execute()
        elif parsed_path == "/v1/subagent/spawn":
            self._subagent_spawn()
        elif parsed_path == "/v1/subagent/spawn-batch":
            self._subagent_spawn_batch()
        elif parsed_path == "/v1/subagent/steer":
            self._subagent_steer()
        elif parsed_path == "/v1/browser/run":
            self._browser_run()
        elif parsed_path == "/v1/desktop/run":
            self._desktop_run()
        elif parsed_path == "/v1/desktop/confirm":
            self._desktop_confirm()
        elif parsed_path == "/v1/desktop/cancel":
            self._desktop_cancel()
        elif parsed_path == "/v1/camera/start":
            self._camera_start()
        elif parsed_path == "/v1/camera/stop":
            self._camera_stop()
        elif parsed_path == "/v1/prefs":
            self._prefs_set()
        elif parsed_path == "/v1/learning/signals":
            self._learning_signal_create()
        elif parsed_path == "/v1/learning/consent":
            self._learning_consent_update()
        elif parsed_path.startswith("/v1/acp/permissions/"):
            self._acp_permission_resolve(urllib.parse.unquote(parsed_path.rsplit("/", 1)[1]))
        elif parsed_path == "/v1/mode":
            self._set_mode()
        elif parsed_path == "/v1/vision/look":
            self._vision_look()
        elif parsed_path == "/v1/browser/confirm":
            self._browser_confirm()
        elif parsed_path == "/v1/browser/cancel":
            self._browser_cancel()
        elif parsed_path == "/v1/kusto/seed":
            self._kusto_seed()
        elif parsed_path == "/v1/goals":
            self._goals_create()
        elif parsed_path == "/v1/skills":
            self._skills_create()
        elif parsed_path == "/v1/skills/evarise":
            self._skills_evarise()
        elif parsed_path == "/v1/background/control":
            self._background_control()
        elif parsed_path == "/v1/alerts":
            self._alerts_upsert()
        elif parsed_path == "/v1/alerts/settings":
            self._alerts_settings_update()
        elif parsed_path == "/v1/signal/send":
            self._signal_send_request()
        elif parsed_path == "/v1/email/accounts":
            self._email_accounts_update()
        elif parsed_path == "/v1/email/account":
            self._email_account_upsert()
        elif parsed_path == "/v1/email/allowlist":
            self._email_allowlist_update()
        elif parsed_path == "/v1/email/credential":
            self._email_credential_set()
        elif parsed_path == "/v1/email/send":
            self._email_send_request()
        elif parsed_path == "/v1/notifications/seen":
            self._notifications_mark_seen()
        elif parsed_path == "/v1/workspaces/projects":
            self._workspace_project_register()
        elif parsed_path == "/v1/workspaces/github-import":
            self._workspace_github_import()
        elif re.fullmatch(r"/v1/workspaces/projects/[^/]+/mcp-servers/[^/]+", parsed_path):
            self._workspace_project_mcp_server_update(parsed_path)
        elif parsed_path == "/v1/workspaces/eva-ready":
            self._workspace_eva_ready()
        elif parsed_path == "/v1/workspaces/runs":
            self._workspace_run_create()
        elif parsed_path == "/v1/workspaces/assets/resolve":
            self._workspace_asset_resolve()
        elif parsed_path == "/v1/workspaces/projects/files/resolve":
            self._workspace_project_file_resolve()
        elif re.fullmatch(r"/v1/workspaces/runs/[^/]+/(archive|discard)", parsed_path):
            self._workspace_run_disposition(parsed_path)
        elif re.fullmatch(r"/v1/workspaces/runs/[^/]+/dispatch", parsed_path):
            self._workspace_run_dispatch(parsed_path)
        elif re.fullmatch(r"/v1/background/proposals/[^/]+/(approve|reject)", parsed_path):
            self._background_review(parsed_path)
        elif parsed_path == "/v1/files/purge":
            self._purge_artifacts()
        elif parsed_path == "/v1/files/write":
            self._write_artifact()
        else:
            self.send_error(404, "Not Found")

    def do_PATCH(self):
        parsed_path = urllib.parse.urlparse(self.path).path
        if not self._require_private_route():
            return
        route = match_patch_route(parsed_path)
        if route is None:
            self.send_error(404, "Not Found")
            return
        handler_name, resource_id = route
        getattr(self, handler_name)(resource_id)

    def do_DELETE(self):
        parsed_path = urllib.parse.urlparse(self.path).path
        if not self._require_private_route():
            return
        if parsed_path.startswith("/v1/workspaces/") and not self._require_workspace_capability():
            return
        if re.fullmatch(r"/v1/workspaces/projects/[^/]+", parsed_path):
            self._workspace_project_delete(urllib.parse.unquote(parsed_path.rsplit("/", 1)[1]))
        elif parsed_path.startswith("/v1/goals/"):
            self._goals_delete(urllib.parse.unquote(parsed_path.split("/v1/goals/", 1)[1]))
        elif parsed_path.startswith("/v1/memory/atoms/"):
            self._memory_atom_delete(urllib.parse.unquote(parsed_path.split("/v1/memory/atoms/", 1)[1]))
        elif parsed_path.startswith("/v1/alerts/"):
            self._alerts_delete(urllib.parse.unquote(parsed_path.split("/v1/alerts/", 1)[1]))
        elif re.fullmatch(r"/v1/email/accounts/[^/]+", parsed_path):
            self._email_account_delete(urllib.parse.unquote(parsed_path.rsplit("/", 1)[1]))
        elif re.fullmatch(r"/v1/email/messages/[^/]+", parsed_path):
            self._email_message_delete(urllib.parse.unquote(parsed_path.rsplit("/", 1)[1]))
        elif parsed_path.startswith("/v1/skills/"):
            self._skills_delete(urllib.parse.unquote(parsed_path.split("/v1/skills/", 1)[1]))
        elif parsed_path.startswith("/v1/cron/"):
            self._cron_delete(urllib.parse.unquote(parsed_path.split("/v1/cron/", 1)[1]))
        elif parsed_path.startswith("/v1/subagent/"):
            self._subagent_dismiss(urllib.parse.unquote(parsed_path.split("/v1/subagent/", 1)[1]))
        elif parsed_path == "/v1/learning/signals" or parsed_path.startswith("/v1/learning/signals/"):
            signal_prefix = "/v1/learning/signals/"
            signal_id = urllib.parse.unquote(parsed_path[len(signal_prefix):]) if parsed_path.startswith(signal_prefix) else ""
            self._learning_signals_delete(signal_id)
        elif parsed_path == "/v1/learning/consent":
            self._learning_consent_revoke()
        elif re.fullmatch(r"/v1/protected-memory/(records|artifacts)/[^/]+", parsed_path):
            kind, record_id = parsed_path.split("/v1/protected-memory/", 1)[1].split("/", 1)
            self._protected_memory_delete(kind, urllib.parse.unquote(record_id))
        else:
            self.send_error(404, "Not Found")

    def _read_json_body(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None, "Invalid Content-Length"
        if content_length == 0:
            return None, "Empty request body"
        try:
            body = self.rfile.read(content_length).decode("utf-8")
        except UnicodeDecodeError:
            return None, "Request body must be UTF-8 JSON"
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None, "Invalid JSON"
        return data, ""

    def _workspace_body(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None, "Invalid Content-Length"
        if content_length <= 0:
            return None, "Empty request body"
        if content_length > 16 * 1024:
            return None, "Workspace request body exceeds the limit"
        data, error = self._read_json_body()
        if error:
            return None, error
        if not isinstance(data, dict):
            return None, "Workspace request body must be an object"
        return data, ""

    def _workspace_projects_list(self):
        try:
            self._json_response(200, {"projects": _workspace_store().list_projects()})
        except WorkspaceError as error:
            self._json_response(400, {"error": {"message": str(error)}})

    def _workspace_project_delete(self, project_id):
        data, error = self._workspace_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        try:
            result = _workspace_store().delete_project(project_id, bool(data.get("confirm_dirty", False)))
            self._json_response(200, {"removed": result})
        except WorkspaceError as workspace_error:
            self._json_response(409, {"error": {"message": str(workspace_error)}})

    def _workspace_project_files_list(self, project_id):
        try:
            payload = _workspace_store().list_project_files(project_id)
            self._json_response(200, payload)
        except WorkspaceError as error:
            self._json_response(404, {"error": {"message": str(error)}})

    def _workspace_project_register(self):
        data, error = self._workspace_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        path_value = data.get("path")
        name_value = data.get("name")
        if not isinstance(path_value, str) or len(path_value) > 4096:
            self._json_response(400, {"error": {"message": "A valid project directory is required."}})
            return
        if name_value is not None and not isinstance(name_value, str):
            self._json_response(400, {"error": {"message": "Project name must be text."}})
            return
        try:
            project = _workspace_store().register_project(path_value, name_value)
            self._json_response(201, {"project": project})
        except WorkspaceError as error:
            self._json_response(400, {"error": {"message": str(error)}})

    def _workspace_github_import(self):
        data, error = self._workspace_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        repository_url = data.get("url")
        github_pat = data.get("github_pat")
        if not isinstance(repository_url, str):
            self._json_response(400, {"error": {"message": "A GitHub repository URL is required."}})
            return
        if github_pat is not None and (not isinstance(github_pat, str) or len(github_pat) > 1024):
            self._json_response(400, {"error": {"message": "GitHub authentication is invalid."}})
            return
        try:
            project = _workspace_store().import_github_repository(repository_url, github_token=github_pat or "")
            self._json_response(201, {"project": project})
        except WorkspaceError as error:
            self._json_response(400, {"error": {"message": str(error)}})

    def _workspace_project_mcp_server_update(self, parsed_path):
        data, error = self._workspace_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            self._json_response(400, {"error": {"message": "Workspace MCP server state must be enabled or disabled."}})
            return
        suffix = parsed_path.split("/v1/workspaces/projects/", 1)[1]
        project_id, server_name = suffix.split("/mcp-servers/", 1)
        try:
            project = _workspace_store().set_project_mcp_server_enabled(
                urllib.parse.unquote(project_id),
                urllib.parse.unquote(server_name),
                enabled,
                data.get("approved_digest", ""),
            )
            self._json_response(200, {"project": project})
        except WorkspaceError as error:
            self._json_response(400, {"error": {"message": str(error)}})

    def _workspace_eva_ready(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self._json_response(400, {"error": {"message": "Invalid Content-Length"}})
            return
        if content_length > 0:
            if content_length > 16 * 1024:
                self._json_response(400, {"error": {"message": "Workspace request body exceeds the limit"}})
                return
            self.rfile.read(content_length)
        try:
            self._json_response(200, {"project": _workspace_store().ensure_eva_ready_project()})
        except WorkspaceError as error:
            self._json_response(400, {"error": {"message": str(error)}})

    def _workspace_runs_list(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        project_id = query.get("project_id", [""])[0]
        if len(project_id) > 64:
            self._json_response(400, {"error": {"message": "Invalid project ID."}})
            return
        try:
            self._json_response(200, {"runs": _workspace_store().list_runs(project_id or None)})
        except WorkspaceError as error:
            self._json_response(400, {"error": {"message": str(error)}})

    def _workspace_assets_list(self):
        try:
            self._json_response(200, {"assets": _workspace_store().list_workspace_assets()})
        except WorkspaceError as error:
            self._json_response(400, {"error": {"message": str(error)}})

    def _workspace_asset_resolve(self):
        data, error = self._workspace_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        run_id = str(data.get("run_id") or "")
        relative_path = data.get("relative_path")
        try:
            path_value = _workspace_store().resolve_workspace_asset(run_id, relative_path)
            self._json_response(200, {"path": path_value})
        except WorkspaceError as error:
            self._json_response(404, {"error": {"message": str(error)}})

    def _workspace_project_file_resolve(self):
        data, error = self._workspace_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        project_id = str(data.get("project_id") or "")
        relative_path = data.get("relative_path")
        try:
            path_value = _workspace_store().resolve_project_file(project_id, relative_path)
            self._json_response(200, {"path": path_value})
        except WorkspaceError as error:
            self._json_response(404, {"error": {"message": str(error)}})

    def _workspace_run_get(self, run_id):
        try:
            self._json_response(200, {"run": _workspace_store().get_run(run_id)})
        except WorkspaceError as error:
            self._json_response(404, {"error": {"message": str(error)}})

    def _workspace_run_create(self):
        data, error = self._workspace_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        try:
            run = _workspace_store().create_run(
                data.get("project_id"),
                data.get("objective"),
                data.get("primary_session_id", ""),
                data.get("base_ref", "HEAD"),
                data.get("model_policy", ""),
                data.get("auto_approve") is True,
            )
        except WorkspaceError as error:
            self._json_response(400, {"error": {"message": str(error)}})
            return
        task = None
        dispatch_error = ""
        if not _cfg.env_disabled("EVA_WORKSPACE_AGENT_AUTODISPATCH"):
            try:
                task = _dispatch_workspace_run(run)
            except WorkspaceError as error:
                dispatch_error = str(error)
        _verbose_debug_emit(
            "workspace_run", stage="created",
            dispatch_state="delayed" if dispatch_error else ("started" if task else "disabled"),
        )
        self._json_response(201, {
            "run": _workspace_store().get_run(run["id"]),
            "task": task,
            "dispatch_error": dispatch_error,
        })

    def _workspace_checkout_status(self, checkout_id):
        try:
            self._json_response(200, {"checkout": _workspace_store().checkout_status(checkout_id)})
        except WorkspaceError as error:
            self._json_response(404, {"error": {"message": str(error)}})

    def _workspace_run_disposition(self, parsed_path):
        data, error = self._workspace_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        suffix = parsed_path.split("/v1/workspaces/runs/", 1)[1]
        run_id, action = suffix.rsplit("/", 1)
        try:
            if action == "archive":
                run = _workspace_store().archive_run(urllib.parse.unquote(run_id))
            else:
                run = _workspace_store().discard_run(
                    urllib.parse.unquote(run_id), bool(data.get("confirm_dirty", False))
                )
            self._json_response(200, {"run": run})
        except WorkspaceError as error:
            self._json_response(400, {"error": {"message": str(error)}})

    def _workspace_run_dispatch(self, parsed_path):
        run_id = urllib.parse.unquote(parsed_path.split("/v1/workspaces/runs/", 1)[1].rsplit("/dispatch", 1)[0])
        try:
            run = _workspace_store().get_run(run_id)
            task = _dispatch_workspace_run(run)
            _verbose_debug_emit("workspace_run", stage="redispatch", dispatch_state="started")
            self._json_response(202, {"run": _workspace_store().get_run(run_id), "task": task})
        except WorkspaceError as error:
            _verbose_debug_emit("workspace_run", stage="redispatch", dispatch_state="failed")
            self._json_response(400, {"error": {"message": str(error)}})

    def _learning_authorized(self):
        """Learning data is local-control data, so use the bridge capability gate."""
        return self._require_bridge_capability()

    def _learning_body(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None, "Invalid Content-Length"
        if content_length <= 0:
            return None, "Empty request body"
        if content_length > 16 * 1024:
            return None, "Request body exceeds learning limit"
        return self._read_json_body()

    def _learning_consent_get(self):
        if not self._learning_authorized():
            return
        self._json_response(200, _get_learning_consent())

    def _learning_consent_update(self):
        if not self._learning_authorized():
            return
        data, error = self._learning_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        try:
            self._json_response(200, _update_learning_consent(data))
        except ValueError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})

    def _learning_consent_revoke(self):
        if not self._learning_authorized():
            return
        self._json_response(200, _update_learning_consent({
            "explicit_feedback": False,
            "action_outcomes": False,
            "voice_diagnostics": False,
            "routine_tools": False,
        }))

    @staticmethod
    def _acp_clients():
        seen = set()
        clients = []
        with _st.subagent_lock:
            workspace_clients = list(_st.workspace_acp_clients.values())
        for client in list(_st.acp_pool.values()) + ([_st.acp_client] if _st.acp_client else []) + workspace_clients:
            if client and id(client) not in seen:
                seen.add(id(client))
                clients.append(client)
        return clients

    def _acp_permissions_list(self):
        if not self._require_bridge_capability():
            return
        rows = []
        for client in self._acp_clients():
            for row in client.list_pending_permissions():
                workspace_run_id = getattr(client, "workspace_run_id", "")
                if workspace_run_id:
                    row["workspace_run_id"] = workspace_run_id
                rows.append(row)
        rows.sort(key=lambda row: row.get("created_at", 0))
        self._json_response(200, {"permissions": rows[:20]})

    def _acp_permission_resolve(self, permission_id):
        if not self._require_bridge_capability():
            return
        data, error = self._learning_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        option_id = str((data or {}).get("option_id") or "")[:120]
        decision = str((data or {}).get("decision") or "").strip().lower()
        if decision and decision not in {"allow", "reject"}:
            self._json_response(400, {"error": {"message": "Permission decision must be allow or reject."}})
            return
        resolved = any(
            client.resolve_permission(permission_id, option_id or None, decision or None)
            for client in self._acp_clients()
        )
        if not resolved:
            self._json_response(409, {
                "resolved": False,
                "decision": "invalid_or_stale",
                "error": {"message": "Permission approval was stale or unavailable; the action was not cancelled."},
            })
            return
        self._json_response(200, {"resolved": True, "decision": decision or "legacy_option"})

    def _learning_signals_list(self):
        if not self._learning_authorized():
            return
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        scope = (params.get("scope") or [None])[0]
        session_id = (params.get("session_id") or [None])[0]
        if not scope:
            self._json_response(400, {"error": {"message": "learning signal list requires an explicit scope"}})
            return
        if scope == "session" and not session_id:
            self._json_response(400, {"error": {"message": "session scope requires session_id"}})
            return
        try:
            rows = _list_learning_signals(
                scope=scope,
                session_id=session_id,
                limit=(params.get("limit") or [100])[0],
            )
        except (TypeError, ValueError) as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        self._json_response(200, {"signals": rows})

    def _learning_signal_create(self):
        if not self._learning_authorized():
            return
        data, error = self._learning_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        try:
            signal, reason = _create_learning_signal(data)
        except ValueError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        if not signal:
            self._json_response(403, {"error": {"message": "learning category is not enabled", "code": reason}})
            return
        applied = self._apply_learning_signal(signal)
        if applied:
            signal = _mark_learning_applied(signal["id"], applied) or signal
        self._json_response(201, {"signal": signal})

    def _learning_signals_delete(self, signal_id=""):
        if not self._learning_authorized():
            return
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        scope = (params.get("scope") or [None])[0]
        session_id = (params.get("session_id") or [None])[0]
        delete_all = (params.get("all") or ["0"])[0].lower() in ("1", "true", "yes")
        if signal_id and (scope != "session" or not session_id):
            self._json_response(400, {"error": {"message": "individual learning signal deletion requires session scope and session_id"}})
            return
        try:
            deleted = _delete_learning_signals(
                signal_id=signal_id or None,
                scope=scope,
                session_id=session_id,
                delete_all=delete_all,
            )
        except ValueError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        self._json_response(200, {"deleted": deleted})

    def _apply_learning_signal(self, signal):
        """Apply only explicit, bounded preference effects; inferred data never overwrites facts."""
        effect = _learning_feedback_effect(signal)
        if not effect:
            return "recorded; inferred signals do not alter adaptive guidance"
        return effect

    def _kusto_context(self):
        cluster, db = _get_kusto_config()
        if not cluster or not db:
            self._json_response(503, {"error": {"message": "Kusto cluster or database not configured for the bridge"}})
            return None, None, False
        token_ok, token_error = _ensure_kusto_token()
        if not token_ok:
            message = "Kusto token unavailable"
            if token_error:
                # Clamp and single-line the upstream error so MSAL/device-code detail does not
                # leak verbatim to clients. Full text is still printed to bridge stderr.
                clean = " ".join(str(token_error).split())[:160]
                if clean:
                    message += ": " + clean
                print(f"[Bridge] Kusto token error (full): {token_error}", file=sys.stderr)
            self._json_response(503, {"error": {"message": message}})
            return None, None, False
        return cluster, db, True

    def _memory_context_required(self):
        """Backend-agnostic memory gate for HTTP endpoints.

        Returns (backend, handle, ok) where:
          - backend="sqlite", handle=SqliteMemory instance
          - backend="kusto",  handle=(cluster, db) tuple
          - ok=False means an error response was already sent
        """
        backend = _resolve_memory_backend()
        if backend == "sqlite":
            mem = _get_sqlite_mem()
            return "sqlite", mem, True
        # Kusto path
        cluster, db = _get_kusto_config()
        if not cluster or not db:
            self._json_response(503, {"error": {"message": "Kusto cluster or database not configured for the bridge"}})
            return None, None, False
        token_ok, token_error = _ensure_kusto_token()
        if not token_ok:
            message = "Kusto token unavailable"
            if token_error:
                clean = " ".join(str(token_error).split())[:160]
                if clean:
                    message += ": " + clean
                print(f"[Bridge] Kusto token error (full): {token_error}", file=sys.stderr)
            self._json_response(503, {"error": {"message": message}})
            return None, None, False
        return "kusto", (cluster, db), True

    def _goals_kusto_context(self):
        return self._kusto_context()

    def _validate_goal_id(self, goal_id):
        goal_id = str(goal_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", goal_id):
            return "", "goal_id is invalid"
        return goal_id, ""

    def _validate_background_proposal_id(self, proposal_id):
        proposal_id = str(proposal_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", proposal_id):
            return "", "proposal_id is invalid"
        return proposal_id, ""

    def _goal_string_field(self, data, key, max_len, required=False):
        value = data.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            return "", key + " must be a string"
        value = value.strip()
        if required and not value:
            return "", key + " is required"
        if len(value) > max_len:
            return "", key + " must be " + str(max_len) + " characters or fewer"
        return value, ""

    def _validate_goal_payload(self, data, creating):
        if not isinstance(data, dict):
            return None, "Request body must be an object"
        allowed = {"title", "description", "category", "priority", "relatedTopics"}
        if not creating:
            allowed.add("status")
        unknown = sorted(set(data.keys()) - allowed)
        if unknown:
            return None, "Unsupported field(s): " + ", ".join(unknown)
        if creating:
            for field in ("title", "category", "priority"):
                if field not in data:
                    return None, field + " is required"
        elif not data:
            return None, "At least one field is required"

        row = {}
        if creating or "title" in data:
            title, error = self._goal_string_field(data, "title", 200, required=True)
            if error:
                return None, error
            row["Title"] = title
        if creating or "description" in data:
            description, error = self._goal_string_field(data, "description", 2000, required=False)
            if error:
                return None, error
            row["Description"] = description
        if creating or "category" in data:
            category, error = self._goal_string_field(data, "category", 64, required=True)
            if error:
                return None, error
            if category not in _GOAL_CATEGORIES:
                return None, "category must be one of self_improvement, knowledge_curation, relational"
            row["Category"] = category
        if creating or "priority" in data:
            priority = data.get("priority")
            if isinstance(priority, bool) or not isinstance(priority, int):
                return None, "priority must be an integer"
            if priority < 0 or priority > 100:
                return None, "priority must be between 0 and 100"
            row["Priority"] = priority
        if "status" in data:
            status, error = self._goal_string_field(data, "status", 32, required=True)
            if error:
                return None, error
            if status not in _GOAL_STATUSES:
                return None, "status must be one of active, paused, done, dropped"
            row["Status"] = status
        if creating or "relatedTopics" in data:
            topics, error = self._goal_string_field(data, "relatedTopics", 1000, required=False)
            if error:
                return None, error
            row["RelatedTopics"] = topics
        return row, ""

    def _goal_now(self):
        import datetime
        return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _goal_latest_by_id(self, cluster, db, goal_id):
        safe_goal_id = goal_id.replace("'", "''")
        backend = _resolve_memory_backend()
        if backend == "sqlite":
            mem = _get_sqlite_mem()
            rows = mem.query(
                f"SELECT * FROM Goals WHERE GoalId = '{safe_goal_id}' "
                f"ORDER BY UpdatedAt DESC LIMIT 1"
            )
        else:
            query = _GOALS_LATEST_QUERY + f" | where GoalId == '{safe_goal_id}' | take 1"
            rows = _kusto_query_direct(cluster, db, query)
        if rows is None:
            return None, "Goals query failed"
        if not rows:
            return {}, ""
        return rows[0], ""

    def _goal_row_from_current(self, current, goal_id, now):
        row = {col: current.get(col, "") for col in _GOAL_COLUMNS}
        row["GoalId"] = goal_id
        if not row.get("CreatedAt"):
            row["CreatedAt"] = now
        if not row.get("Status"):
            row["Status"] = "active"
        try:
            row["Priority"] = int(row.get("Priority", 0) or 0)
        except (TypeError, ValueError):
            row["Priority"] = 0
        return row

    def _write_goal_row(self, cluster, db, row):
        backend = _resolve_memory_backend()
        if backend == "sqlite":
            mem = _get_sqlite_mem()
            return mem.ingest("Goals", _GOAL_COLUMNS, [row])
        return _kusto_ingest_direct(cluster, db, "Goals", _GOAL_COLUMNS, [row])

    def _background_status(self):
        self._json_response(200, _background_status_dict())

    def _background_latest_proposal_by_id(self, cluster, db, proposal_id):
        safe_id = _safe_kusto_string(proposal_id)
        backend = _resolve_memory_backend()
        if backend == "sqlite":
            mem = _get_sqlite_mem()
            rows = mem.query(
                f"SELECT * FROM BackgroundProposals WHERE ProposalId = '{safe_id}' "
                f"ORDER BY CreatedAt DESC LIMIT 1"
            )
        else:
            query = _BG_PROPOSALS_LATEST_QUERY + f" | where ProposalId == '{safe_id}' | take 1"
            rows = _kusto_query_direct(cluster, db, query)
        if rows is None:
            return None, "BackgroundProposals query failed"
        if not rows:
            return {}, ""
        return rows[0], ""

    def _background_proposals(self):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "background proposal reads are restricted to loopback bind"}})
            return
        backend, handle, ok = self._memory_context_required()
        if not ok:
            return
        if backend == "sqlite":
            mem = handle
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            status = str(params.get("status", ["pending"])[0] or "pending").strip().lower()
            if status not in _BG_PROPOSAL_STATUSES and status != "all":
                self._json_response(400, {"error": {"message": "status must be pending, approved, rejected, applying, applied, failed, or all"}})
                return
            sql = "SELECT * FROM BackgroundProposals"
            if status != "all":
                sql += f" WHERE Status = '{_safe_kusto_string(status)}'"
            sql += " ORDER BY CreatedAt DESC LIMIT 50"
            rows = mem.query(sql)
            self._json_response(200, {"proposals": rows or []})
        else:
            cluster, db = handle
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            status = str(params.get("status", ["pending"])[0] or "pending").strip().lower()
            if status not in _BG_PROPOSAL_STATUSES and status != "all":
                self._json_response(400, {"error": {"message": "status must be pending, approved, rejected, applying, applied, failed, or all"}})
                return
            query = _BG_PROPOSALS_LATEST_QUERY
            if status != "all":
                query += f" | where Status == '{_safe_kusto_string(status)}'"
            query += " | order by CreatedAt desc | take 50"
            rows = _kusto_query_direct(cluster, db, query)
            if rows is None:
                self._json_response(200, {"proposals": [], "warning": "BackgroundProposals table may not exist yet; run /v1/kusto/seed to create it"})
                return
            self._json_response(200, {"proposals": rows})

    def _background_activity(self):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "background activity reads are restricted to loopback bind"}})
            return
        backend, handle, ok = self._memory_context_required()
        if not ok:
            return
        if backend == "sqlite":
            mem = handle
            rows = mem.query("SELECT * FROM BackgroundActivity ORDER BY StartedAt DESC LIMIT 50")
            self._json_response(200, {"activity": rows or []})
        else:
            cluster, db = handle
            query = "BackgroundActivity | order by StartedAt desc | take 50"
            rows = _kusto_query_direct(cluster, db, query)
            if rows is None:
                self._json_response(200, {"activity": [], "warning": "BackgroundActivity table may not exist yet; run /v1/kusto/seed to create it"})
                return
            self._json_response(200, {"activity": rows})

    def _background_control(self):
        # global statement removed — writes go to _st.*
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "background mutations are restricted to loopback bind"}})
            return

        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        if not isinstance(data, dict):
            self._json_response(400, {"error": {"message": "Request body must be an object"}})
            return
        unknown = sorted(set(data.keys()) - {"enabled", "intervalSeconds", "runNow", "jobs"})
        if unknown:
            self._json_response(400, {"error": {"message": "Unsupported field(s): " + ", ".join(unknown)}})
            return

        requested_jobs = None
        if "jobs" in data:
            jobs_value = data.get("jobs")
            if not isinstance(jobs_value, dict):
                self._json_response(400, {"error": {"message": "jobs must be an object of jobType -> boolean"}})
                return
            valid_job_types = {job_type for job_type, _ in _BG_JOBS}
            unknown_jobs = sorted(set(jobs_value.keys()) - valid_job_types)
            if unknown_jobs:
                self._json_response(400, {"error": {"message": "Unknown job type(s): " + ", ".join(unknown_jobs)}})
                return
            for job_type, enabled in jobs_value.items():
                if not isinstance(enabled, bool):
                    self._json_response(400, {"error": {"message": "jobs." + job_type + " must be a boolean"}})
                    return
            requested_jobs = jobs_value

        requested_enabled = _st.bg_loop_enabled
        if "enabled" in data:
            if not isinstance(data.get("enabled"), bool):
                self._json_response(400, {"error": {"message": "enabled must be a boolean"}})
                return
            requested_enabled = bool(data.get("enabled"))

        requested_interval = _st.bg_loop_interval_seconds
        if "intervalSeconds" in data:
            if isinstance(data.get("intervalSeconds"), bool):
                self._json_response(400, {"error": {"message": "intervalSeconds must be an integer"}})
                return
            try:
                requested_interval = int(data.get("intervalSeconds"))
            except (TypeError, ValueError):
                self._json_response(400, {"error": {"message": "intervalSeconds must be an integer"}})
                return
            if requested_interval < 900 or requested_interval > 86400:
                self._json_response(400, {"error": {"message": "intervalSeconds must be between 900 and 86400"}})
                return

        run_now = False
        if "runNow" in data:
            if not isinstance(data.get("runNow"), bool):
                self._json_response(400, {"error": {"message": "runNow must be a boolean"}})
                return
            run_now = data["runNow"]
        needs_kusto = requested_enabled or run_now
        if needs_kusto:
            if not _st.cognition_enabled:
                self._json_response(503, {"error": {"message": "Cognition is not enabled"}})
                return
            cluster, db, context_ok = self._kusto_context()
            if not context_ok:
                return

        _st.bg_loop_enabled = requested_enabled
        _st.bg_loop_interval_seconds = requested_interval
        if requested_jobs is not None:
            for job_type, enabled in requested_jobs.items():
                _BG_JOBS_ENABLED[job_type] = bool(enabled)
        if _st.bg_loop_enabled:
            if not _start_bg_loop():
                _st.bg_last_error = "background loop could not start"
                self._json_response(503, {"error": {"message": _st.bg_last_error}})
                return
        else:
            _stop_bg_loop()
            _st.bg_last_error = ""
        if run_now:
            _trigger_background_run_once()

        status = _background_status_dict()
        status["runNowQueued"] = run_now
        self._json_response(200, status)

    def _background_review(self, parsed_path):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "background mutations are restricted to loopback bind"}})
            return
        match = re.fullmatch(r"/v1/background/proposals/([^/]+)/(approve|reject)", parsed_path)
        if not match:
            self._json_response(404, {"error": {"message": "Not Found"}})
            return
        proposal_id, error = self._validate_background_proposal_id(urllib.parse.unquote(match.group(1)))
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        action = match.group(2)
        cluster, db, ok = self._kusto_context()
        if not ok:
            return

        current, error = self._background_latest_proposal_by_id(cluster, db, proposal_id)
        if error:
            self._json_response(500, {"error": {"message": error}})
            return
        if not current:
            self._json_response(404, {"error": {"message": "Proposal not found"}})
            return
        current_status = str(current.get("Status", "")).lower()
        if action == "approve" and current_status not in {"pending", "applying"}:
            self._json_response(409, {"error": {"message": "Proposal is not pending or applying"}})
            return
        if action == "reject" and current_status != "pending":
            self._json_response(409, {"error": {"message": "Proposal is not pending"}})
            return

        if action == "approve":
            target_table = current.get("TargetTable")
            if target_table not in _BG_APPLY_TABLES:
                self._json_response(400, {"error": {"message": "Unsupported proposal target table"}})
                return
            payload, error = _background_proposal_payload(current)
            if error:
                self._json_response(400, {"error": {"message": error}})
                return
            if current_status == "pending":
                applying_row = _background_proposal_update_row(current, "applying", "loopback", f"applying to {target_table}")
                if not _write_background_proposal(cluster, db, applying_row):
                    self._json_response(500, {"error": {"message": "BackgroundProposals applying status write failed"}})
                    return
                current = applying_row
            apply_ok, apply_error, apply_note = _apply_proposal_payload(cluster, db, target_table, payload)
            if not apply_ok:
                self._json_response(500, {"error": {"message": apply_error + "; proposal remains applying. Retry approve safely after resolving the transient error."}})
                return
            reviewed_row = _background_proposal_update_row(current, "applied", "loopback", apply_note or f"approved and applied to {target_table}")
        else:
            reviewed_row = _background_proposal_update_row(current, "rejected", "loopback", "rejected by user")

        if not _write_background_proposal(cluster, db, reviewed_row):
            message = "BackgroundProposals status write failed"
            if action == "approve":
                message += "; proposal remains applying. Retry approve safely after resolving the transient error."
            self._json_response(500, {"error": {"message": message}})
            return
        self._json_response(200, {"proposal": reviewed_row})

    def _goals_list(self):
        backend, handle, ok = self._memory_context_required()
        if not ok:
            return
        if backend == "sqlite":
            mem = handle
            goals = mem.query(
                "SELECT * FROM Goals WHERE Status != 'dropped' "
                "ORDER BY Priority DESC, UpdatedAt DESC"
            )
        else:
            cluster, db = handle
            query = _GOALS_LATEST_QUERY + " | order by Priority desc, UpdatedAt desc"
            goals = _kusto_query_direct(cluster, db, query)
        if goals is None:
            self._json_response(200, {"goals": [], "warning": "Goals table may not exist yet; run /v1/kusto/seed to create it"})
            return
        self._json_response(200, {"goals": goals})

    def _goals_create(self):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "goals mutations are restricted to loopback bind"}})
            return

        backend, handle, ok = self._memory_context_required()
        if not ok:
            return
        cluster, db = handle if backend == "kusto" else (None, None)
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        fields, error = self._validate_goal_payload(data, creating=True)
        if error:
            self._json_response(400, {"error": {"message": error}})
            return

        now = self._goal_now()
        row = {
            "GoalId": str(uuid.uuid4()),
            "Title": fields.get("Title", ""),
            "Description": fields.get("Description", ""),
            "Category": fields.get("Category", ""),
            "Status": "active",
            "Priority": fields.get("Priority", 0),
            "RelatedTopics": fields.get("RelatedTopics", ""),
            "CreatedAt": now,
            "UpdatedAt": now,
        }
        if not self._write_goal_row(cluster, db, row):
            self._json_response(500, {"error": {"message": "Goal write failed"}})
            return
        self._json_response(201, {"goal": row})

    def _goals_patch(self, raw_goal_id):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "goals mutations are restricted to loopback bind"}})
            return

        backend, handle, ok = self._memory_context_required()
        if not ok:
            return
        cluster, db = handle if backend == "kusto" else (None, None)
        goal_id, error = self._validate_goal_id(raw_goal_id)
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        fields, error = self._validate_goal_payload(data, creating=False)
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        current, error = self._goal_latest_by_id(cluster, db, goal_id)
        if error:
            self._json_response(500, {"error": {"message": error}})
            return
        if not current:
            self._json_response(404, {"error": {"message": "Goal not found"}})
            return

        now = self._goal_now()
        row = self._goal_row_from_current(current, goal_id, now)
        row.update(fields)
        row["UpdatedAt"] = now
        if not self._write_goal_row(cluster, db, row):
            self._json_response(500, {"error": {"message": "Goal write failed"}})
            return
        self._json_response(200, {"goal": row})

    def _goals_delete(self, raw_goal_id):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "goals mutations are restricted to loopback bind"}})
            return

        backend, handle, ok = self._memory_context_required()
        if not ok:
            return
        cluster, db = handle if backend == "kusto" else (None, None)
        goal_id, error = self._validate_goal_id(raw_goal_id)
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        current, error = self._goal_latest_by_id(cluster, db, goal_id)
        if error:
            self._json_response(500, {"error": {"message": error}})
            return
        if not current:
            self._json_response(404, {"error": {"message": "Goal not found"}})
            return

        now = self._goal_now()
        row = self._goal_row_from_current(current, goal_id, now)
        row["Status"] = "dropped"
        row["UpdatedAt"] = now
        if not self._write_goal_row(cluster, db, row):
            self._json_response(500, {"error": {"message": "Goal write failed"}})
            return
        self._json_response(200, {"goal": row, "status": "dropped"})

    # ── Skills ────────────────────────────────────────────────────────
    def _skill_latest_by_id(self, cluster, db, skill_id):
        safe = skill_id.replace("'", "''")
        backend = _resolve_memory_backend()
        if backend == "sqlite":
            mem = _get_sqlite_mem()
            rows = mem.query(
                f"SELECT * FROM Skills WHERE SkillId = '{safe}' "
                f"ORDER BY UpdatedAt DESC, rowid DESC LIMIT 1"
            )
        else:
            rows = _kusto_query_direct(cluster, db, _SKILLS_LATEST_QUERY + f" | where SkillId == '{safe}' | take 1")
        if rows is None:
            return None, "Skills query failed"
        if not rows:
            return {}, ""
        return rows[0], ""

    def _write_skill_row(self, cluster, db, row):
        backend = _resolve_memory_backend()
        if backend == "sqlite":
            mem = _get_sqlite_mem()
            return mem.ingest("Skills", _SKILL_COLUMNS, [row])
        return _kusto_ingest_direct(cluster, db, "Skills", _SKILL_COLUMNS, [row])

    def _validate_skill_id(self, skill_id):
        skill_id = str(skill_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", skill_id):
            return "", "skill_id is invalid"
        return skill_id, ""

    def _validate_skill_payload(self, data, creating):
        if not isinstance(data, dict):
            return None, "Request body must be an object"
        fields = {}
        name = str(data.get("name", data.get("Name", "")) or "").strip()
        if creating and not name:
            return None, "name is required"
        if name:
            fields["Name"] = name[:60]
        for src_key, col, limit in (("description", "Description", 400),
                                    ("instructions", "Instructions", 8000),
                                    ("tools", "Tools", 200),
                                    ("tags", "Tags", 200),
                                    ("source", "Source", 200)):
            val = data.get(src_key, data.get(col))
            if val is not None:
                if isinstance(val, list):
                    val = ", ".join(str(x).strip() for x in val if str(x).strip())
                fields[col] = str(val).strip()[:limit]
        config_value = data.get("config", data.get("Config"))
        defaults_value = data.get("configurable_defaults", data.get("defaults"))
        if config_value is not None or defaults_value is not None:
            if config_value is None:
                config_value = {"defaults": defaults_value}
                if "allowed_fallbacks" in data:
                    config_value["allowed_fallbacks"] = data["allowed_fallbacks"]
            try:
                fields["Config"] = normalize_skill_config(config_value)
            except ValueError as exc:
                return None, str(exc)
        category = data.get("category", data.get("Category"))
        if category is not None:
            raw_category = str(category).strip()
            normalized_category = _normalize_skill_category(raw_category)
            if raw_category and normalized_category == "Uncategorized" and raw_category.casefold() != "uncategorized":
                return None, "category must be one of: " + ", ".join(_SKILL_CATEGORIES)
            fields["Category"] = normalized_category
        elif creating:
            fields["Category"] = "Uncategorized"
        status = data.get("status", data.get("Status"))
        if status is not None:
            status = str(status).strip().lower()
            if status not in _SKILL_STATUSES:
                return None, "status must be one of: " + ", ".join(sorted(_SKILL_STATUSES))
            fields["Status"] = status
        if creating and not fields.get("Instructions"):
            return None, "instructions are required"
        if fields.get("Status") == "provisional":
            return None, "provisional status is assigned only by validated low-risk promotion"
        return fields, ""

    def _skills_list(self):
        backend, handle, ok = self._memory_context_required()
        if not ok:
            return
        if backend == "sqlite":
            mem = handle
            if not mem.table_exists("Skills"):
                self._json_response(200, {"skills": []})
                return
            rows = _sqlite_latest_skill_rows(mem)
            self._json_response(200, {"skills": rows or []})
        else:
            cluster, db = handle
            if not _get_table_columns(cluster, db, "Skills"):
                self._json_response(200, {"skills": [], "warning": "Skills table may not exist yet; run /v1/kusto/seed to create it"})
                return
            rows = _kusto_query_direct(cluster, db, _SKILLS_LATEST_QUERY + " | where Status != 'deleted' | order by UpdatedAt desc")
            self._json_response(200, {"skills": rows or []})

    def _skills_execute(self):
        if self.headers.get_all("Transfer-Encoding"):
            self.close_connection = True
            self._json_response(400, {"error": {"message": "bounded skill requests must not use Transfer-Encoding"}})
            return
        content_length_values = self.headers.get_all("Content-Length") or []
        if len(content_length_values) != 1:
            self.close_connection = True
            self._json_response(400, {"error": {"message": "bounded skill requests require exactly one Content-Length"}})
            return
        try:
            content_length = int(content_length_values[0])
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self.close_connection = True
            self._json_response(400, {"error": {"message": "bounded skill requests require a positive Content-Length"}})
            return
        if content_length > 1024 * 1024:
            self.close_connection = True
            self._json_response(413, {"error": {"message": "bounded skill request body exceeds the 1 MiB limit"}})
            return
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        if not isinstance(data, dict):
            self._json_response(400, {"error": {"message": "bounded skill requests must be JSON objects"}})
            return
        receipt = execute_bounded_skill(
            data,
            artifacts_dir=_cfg.ARTIFACTS_DIR,
            approved_workspace_roots=_cfg.SKILLS_WORKSPACE_ROOTS,
        )
        self._json_response(200, receipt)

    def _skills_evarise(self):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "skill import is restricted to loopback bind"}})
            return
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        source_type = str((data or {}).get("source_type", "paste"))
        raw, err = _fetch_skill_source(source_type, data or {})
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        draft, err = _evarise_skill(raw)
        if err:
            self._json_response(502, {"error": {"message": "Eva'rise failed: " + err}})
            return
        draft["source"] = _skill_source_label(source_type, data or {})
        self._json_response(200, {"draft": draft})

    def _skills_create(self):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "skill mutations are restricted to loopback bind"}})
            return
        backend, handle, ok = self._memory_context_required()
        if not ok:
            return
        cluster, db = handle if backend == "kusto" else (None, None)
        if backend == "kusto" and not _get_table_columns(cluster, db, "SkillVersions"):
            self._json_response(409, {"error": {"message": "SkillVersions is unavailable; apply the current Kusto seed before creating governed skills"}})
            return
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        fields, error = self._validate_skill_payload(data, creating=True)
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        now = self._goal_now()
        row = {
            "SkillId": "sk-" + uuid.uuid4().hex[:12],
            "Name": fields.get("Name", "Untitled Skill"),
            "Description": fields.get("Description", ""),
            "Category": fields.get("Category", "Uncategorized"),
            "Instructions": fields.get("Instructions", ""),
            "Tools": fields.get("Tools", ""),
            "Tags": fields.get("Tags", ""),
            "Config": fields.get("Config", "{}"),
            "Source": fields.get("Source", ""),
            "Status": fields.get("Status", "draft"),
            "CreatedAt": now,
            "UpdatedAt": now,
        }
        if not self._write_skill_row(cluster, db, row):
            self._json_response(500, {"error": {"message": "Skill write failed"}})
            return
        version = None
        if backend == "sqlite":
            version = MemoryModel(handle).register_skill_version(row["SkillId"], row["Tools"], "Two successful bounded evaluations are required for provisional use.")
        else:
            version = KustoMemoryModel(cluster, db, _kusto_query_direct, _kusto_ingest_direct).register_skill_version(
                row["SkillId"], row["Tools"], "Two successful bounded evaluations are required for provisional use."
            )
        self._json_response(201, {"skill": row, "version": version})

    def _skills_patch(self, raw_skill_id):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "skill mutations are restricted to loopback bind"}})
            return
        backend, handle, ok = self._memory_context_required()
        if not ok:
            return
        cluster, db = handle if backend == "kusto" else (None, None)
        skill_id, error = self._validate_skill_id(raw_skill_id)
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        fields, error = self._validate_skill_payload(data, creating=False)
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        current, error = self._skill_latest_by_id(cluster, db, skill_id)
        if error:
            self._json_response(500, {"error": {"message": error}})
            return
        if not current:
            self._json_response(404, {"error": {"message": "Skill not found"}})
            return
        config_payload = data.get("config", data.get("Config"))
        if config_payload is None and ("defaults" in data or "configurable_defaults" in data):
            config_payload = {"defaults": data.get("configurable_defaults", data.get("defaults"))}
            if "allowed_fallbacks" in data:
                config_payload["allowed_fallbacks"] = data["allowed_fallbacks"]
        if "Config" in fields and isinstance(config_payload, dict) and "allowed_fallbacks" not in config_payload:
            current_raw_config = current.get("Config", "{}") or "{}"
            if isinstance(current_raw_config, dict):
                current_config = current_raw_config
            else:
                try:
                    current_config = json.loads(str(current_raw_config))
                except (TypeError, ValueError):
                    current_config = {}
            parsed_config = json.loads(fields["Config"])
            fields["Config"] = normalize_skill_config({
                "defaults": parsed_config.get("defaults", {}),
                "allowed_fallbacks": current_config.get("allowed_fallbacks", []),
            })
        now = self._goal_now()
        row = {col: current.get(col, "") for col in _SKILL_COLUMNS}
        row["SkillId"] = skill_id
        if not row.get("CreatedAt"):
            row["CreatedAt"] = now
        if not row.get("Status"):
            row["Status"] = "active"
        row.update(fields)
        if str(current.get("Source", "")).lower() == "seed":
            row["Source"] = "user-override"
        row["UpdatedAt"] = now
        if not self._write_skill_row(cluster, db, row):
            self._json_response(500, {"error": {"message": "Skill write failed"}})
            return
        self._json_response(200, {"skill": row})

    def _skills_delete(self, raw_skill_id):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "skill mutations are restricted to loopback bind"}})
            return
        backend, handle, ok = self._memory_context_required()
        if not ok:
            return
        cluster, db = handle if backend == "kusto" else (None, None)
        skill_id, error = self._validate_skill_id(raw_skill_id)
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        current, error = self._skill_latest_by_id(cluster, db, skill_id)
        if error:
            self._json_response(500, {"error": {"message": error}})
            return
        if not current:
            self._json_response(404, {"error": {"message": "Skill not found"}})
            return
        now = self._goal_now()
        row = {col: current.get(col, "") for col in _SKILL_COLUMNS}
        row["SkillId"] = skill_id
        if not row.get("CreatedAt"):
            row["CreatedAt"] = now
        row["Status"] = "deleted"
        row["UpdatedAt"] = now
        if not self._write_skill_row(cluster, db, row):
            self._json_response(500, {"error": {"message": "Skill write failed"}})
            return
        self._json_response(200, {"skill": row, "status": "deleted"})

    def _protected_memory_loopback(self):
        if _is_loopback_bind():
            return True
        self._json_response(403, {"error": {"message": "protected memory is only available on localhost"}})
        return False

    def _protected_memory_vault(self):
        return _get_protected_vault()

    @staticmethod
    def _protected_memory_provider():
        executable = os.environ.get("EVA_YKMAN_PATH", "ykman").strip() or "ykman"
        slot = os.environ.get("EVA_YUBIKEY_CHALLENGE_SLOT", "2").strip() or "2"
        return YkmanChallengeResponseProvider(executable=executable, slot=slot)

    def _protected_memory_body(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return None, "Invalid Content-Length"
        if content_length > 64 * 1024 * 1024:
            return None, "Protected memory request is too large"
        return self._read_json_body()

    def _protected_memory_status(self):
        if not self._protected_memory_loopback():
            return
        try:
            vault = self._protected_memory_vault()
            executable = os.environ.get("EVA_YKMAN_PATH", "ykman").strip() or "ykman"
            provider_available = bool(shutil.which(executable) or os.path.isfile(executable))
            self._json_response(200, {
                "locked": not vault.is_unlocked,
                "enrolled": bool(vault.enrolled_slots()),
                "model_release_allowed": bool(_st.protected_memory_model_release and vault.is_unlocked),
                "key_provider": "yubikey-challenge-response",
                "key_provider_available": provider_available,
                "key_slots": vault.enrolled_slots(),
                "records": vault.list_metadata(),
            })
        except ProtectedMemoryError:
            self._json_response(500, {"error": {"message": "protected memory status unavailable"}})

    def _protected_memory_enroll(self):
        if not self._protected_memory_loopback():
            return
        data, error = self._protected_memory_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        if not isinstance(data, dict):
            self._json_response(400, {"error": {"message": "Request body must be an object"}})
            return
        try:
            result = self._protected_memory_vault().enroll(
                self._protected_memory_provider(), data.get("slot_id")
            )
        except (TypeError, ValueError) as error:
            self._json_response(400, {"error": {"message": str(error)}})
            return
        except UnlockError:
            self._json_response(503, {"error": {"message": "YubiKey enrollment failed"}})
            return
        _st.protected_memory_model_release = False
        self._json_response(201, {"status": "enrolled", "slot": result})

    def _protected_memory_unlock(self):
        if not self._protected_memory_loopback():
            return
        data, error = self._protected_memory_body()
        if error and error != "Empty request body":
            self._json_response(400, {"error": {"message": error}})
            return
        data = data if isinstance(data, dict) else {}
        allow_model_release = data.get("allow_model_release") is True
        try:
            result = self._protected_memory_vault().unlock(
                self._protected_memory_provider(), data.get("slot_id")
            )
        except (TypeError, ValueError) as error:
            self._json_response(400, {"error": {"message": str(error)}})
            return
        except UnlockError:
            self._json_response(403, {"error": {"message": "YubiKey unlock failed"}})
            return
        _st.protected_memory_model_release = allow_model_release
        self._json_response(200, {
            "status": "unlocked",
            "slot": result,
            "model_release_allowed": allow_model_release,
        })

    def _protected_memory_lock(self):
        if not self._protected_memory_loopback():
            return
        # Consume legacy clients' JSON body before replying. Otherwise a body
        # like "{}" remains on the HTTP/1.1 connection and corrupts the next
        # request line as "{}GET ...".
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._json_response(400, {"error": {"message": "Invalid Content-Length"}})
            return
        if content_length < 0 or content_length > 64 * 1024:
            self.close_connection = True
            self._json_response(400, {"error": {"message": "Protected memory lock request is too large"}})
            return
        if content_length:
            self.rfile.read(content_length)
        self._protected_memory_vault().lock()
        _st.protected_memory_model_release = False
        self._json_response(200, {"status": "locked"})

    def _protected_memory_write(self, kind):
        if not self._protected_memory_loopback():
            return
        data, error = self._protected_memory_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        if not isinstance(data, dict):
            self._json_response(400, {"error": {"message": "Request body must be an object"}})
            return
        requested_mime_type = str(data.get("mime_type") or "")
        mime_type = _safe_content_type(requested_mime_type) if requested_mime_type else ""
        try:
            vault = self._protected_memory_vault()
            if kind == "memory":
                if "value_base64" in data:
                    value = base64.b64decode(str(data.get("value_base64") or ""), validate=True)
                elif "value" in data:
                    value = data["value"]
                else:
                    raise ValueError("value or value_base64 is required")
                record_id = vault.put_memory(
                    value,
                    public_label=data.get("public_label", "protected memory record"),
                    category=data.get("category", "general"),
                    mime_type=mime_type,
                )
            else:
                content = base64.b64decode(str(data.get("content_base64") or ""), validate=True)
                record_id = vault.put_artifact(
                    content,
                    public_label=data.get("public_label", "protected artifact"),
                    category=data.get("category", "file"),
                    mime_type=mime_type,
                )
        except VaultLockedError:
            self._json_response(423, {"error": {"message": "protected memory is locked"}})
            return
        except (TypeError, ValueError, base64.binascii.Error) as error:
            self._json_response(400, {"error": {"message": str(error)}})
            return
        except ProtectedMemoryError:
            self._json_response(500, {"error": {"message": "protected memory write failed"}})
            return
        self._json_response(201, {"status": "stored", "record_id": record_id, "kind": kind})

    def _protected_memory_read(self, kind, record_id):
        if not self._protected_memory_loopback():
            return
        try:
            vault = self._protected_memory_vault()
            if kind == "records":
                result = vault.get_memory(record_id)
                value = result.pop("value")
                if isinstance(value, bytes):
                    result["value_base64"] = base64.b64encode(value).decode("ascii")
                else:
                    result["value"] = value
                self._json_response(200, result)
                return
            metadata = next((item for item in vault.list_metadata() if item["RecordId"] == record_id and item["kind"] == "artifact"), None)
            if metadata is None:
                self._json_response(404, {"error": {"message": "protected artifact not found"}})
                return
            chunks = vault.iter_artifact_chunks(record_id)
            try:
                first_chunk = next(chunks, b"")
            except ProtectedMemoryError:
                raise
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", _safe_content_type(metadata.get("MimeType") or ""))
            self.send_header("Content-Length", str(metadata.get("SizeBytes", 0)))
            self.send_header("Content-Disposition", 'attachment; filename="protected-artifact.bin"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if first_chunk:
                self.wfile.write(first_chunk)
            for chunk in chunks:
                self.wfile.write(chunk)
        except VaultLockedError:
            self._json_response(423, {"error": {"message": "protected memory is locked"}})
        except KeyError:
            self._json_response(404, {"error": {"message": "protected record not found"}})
        except ProtectedMemoryError:
            self._json_response(500, {"error": {"message": "protected memory read failed"}})

    def _protected_memory_delete(self, kind, record_id):
        if not self._protected_memory_loopback():
            return
        try:
            self._protected_memory_vault().delete(record_id)
        except VaultLockedError:
            self._json_response(423, {"error": {"message": "protected memory is locked"}})
            return
        except ProtectedMemoryError:
            self._json_response(500, {"error": {"message": "protected memory delete failed"}})
            return
        self._json_response(200, {"status": "deleted", "record_id": record_id, "kind": kind})

    def _list_artifacts(self):
        """List all artifacts in ARTIFACTS_DIR with name, size, and mtime."""
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "only available on localhost"}})
            return
        os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
        base = os.path.realpath(_ARTIFACTS_DIR)
        items = []
        for name in sorted(os.listdir(_ARTIFACTS_DIR)):
            if not _valid_artifact_name(name):
                continue
            entry_path = os.path.join(_ARTIFACTS_DIR, name)
            target = os.path.realpath(entry_path)
            if not target.startswith(base + os.sep) or not os.path.isfile(target):
                continue
            st = os.stat(target)
            items.append({"name": name, "size": st.st_size, "modified": st.st_mtime})
        items.sort(key=lambda x: x["modified"], reverse=True)
        self._json_response(200, {"files": items})

    @staticmethod
    def _existing_artifact_path(requested_name):
        """Resolve an existing regular artifact without joining user input."""
        if not _valid_artifact_name(requested_name):
            return None
        os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
        with os.scandir(_ARTIFACTS_DIR) as entries:
            for entry in entries:
                if entry.name == requested_name and entry.is_file(follow_symlinks=False):
                    return entry.path
        return None

    def _open_artifact(self, requested_name):
        """Open an artifact file with the system's default application."""
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "only available on localhost"}})
            return
        if not _valid_artifact_name(requested_name):
            self._json_response(400, {"error": {"message": "invalid filename"}})
            return
        target = self._existing_artifact_path(requested_name)
        if target is None:
            self._json_response(404, {"error": {"message": "file not found"}})
            return
        import subprocess
        try:
            subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._json_response(200, {"opened": True, "file": requested_name})
        except Exception as e:
            self._json_response(500, {"error": {"message": f"Could not open file: {e}"}})

    def _write_artifact(self):
        """Accept file content from the frontend and write to ARTIFACTS_DIR.

        POST /v1/files/write  {filename: str, content: str, is_pdf: bool}
        The frontend's file.download capability calls this instead of using
        blob URLs (which break under Electron's file:// origin).
        """
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "only available on localhost"}})
            return
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        filename = (data.get("filename") or "").strip()
        if not filename or not _valid_artifact_name(filename):
            self._json_response(400, {"error": {"message": "invalid filename"}})
            return
        content = data.get("content", "")
        is_pdf = bool(data.get("is_pdf", False))
        os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
        payload = self._render_text_pdf(content) if is_pdf else str(content or "").encode("utf-8")
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        directory_fd = os.open(_ARTIFACTS_DIR, directory_flags)
        try:
            file_fd = os.open(filename, file_flags, 0o600, dir_fd=directory_fd)
            with os.fdopen(file_fd, "wb") as artifact_file:
                artifact_file.write(payload)
                size = artifact_file.tell()
            print(f"[Artifact] Wrote {filename} ({size} bytes, pdf={is_pdf})")
            self._json_response(200, {"ok": True, "filename": filename, "size": size})
        except Exception as e:
            self._json_response(500, {"error": {"message": f"write failed: {e}"}})
        finally:
            os.close(directory_fd)

    @staticmethod
    def _render_text_pdf(text):
        """Generate minimal valid PDF bytes from plain text."""
        text = str(text or "")
        font_size = 11
        leading = round(font_size * 1.35)
        margin_x, margin_top = 50, 50
        page_w, page_h = 612, 792
        lines_per_page = max(1, (page_h - margin_top * 2) // leading)
        max_chars = 95

        def to_latin1(s):
            return "".join(c if ord(c) <= 255 else "?" for c in s)

        def esc(s):
            return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

        raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        lines = []
        for ln in raw:
            ln = ln.replace("\t", "    ")
            if not ln:
                lines.append("")
                continue
            cur = ""
            for tok in re.split(r"(\s+)", ln):
                if cur and len(cur + tok) > max_chars:
                    lines.append(cur)
                    cur = "" if tok.strip() == "" else tok
                else:
                    cur += tok
                while len(cur) > max_chars:
                    lines.append(cur[:max_chars])
                    cur = cur[max_chars:]
            lines.append(cur)
        if not lines:
            lines = [""]

        pages = [lines[i:i + lines_per_page] for i in range(0, len(lines), lines_per_page)]
        objs = {}
        objs[1] = "<< /Type /Catalog /Pages 2 0 R >>"
        objs[3] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        page_nums = []
        num = 4
        for pl in pages:
            pn, cn = num, num + 1
            num += 2
            page_nums.append(pn)
            start_y = page_h - margin_top
            stream = f"BT /F1 {font_size} Tf {leading} TL {margin_x} {start_y} Td\n"
            for l in pl:
                stream += f"({esc(to_latin1(l))}) Tj T*\n"
            stream += "ET"
            objs[cn] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
            objs[pn] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w} {page_h}] "
                        f"/Resources << /Font << /F1 3 0 R >> >> /Contents {cn} 0 R >>")
        kids = " ".join(f"{n} 0 R" for n in page_nums)
        objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_nums)} >>"
        max_num = num - 1
        out = "%PDF-1.4\n"
        offsets = {}
        for n in range(1, max_num + 1):
            offsets[n] = len(out)
            out += f"{n} 0 obj\n{objs[n]}\nendobj\n"
        xref_pos = len(out)
        out += f"xref\n0 {max_num + 1}\n0000000000 65535 f \n"
        for m in range(1, max_num + 1):
            out += f"{offsets[m]:010d} 00000 n \n"
        out += f"trailer\n<< /Size {max_num + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF"
        return bytes(ord(c) & 0xFF for c in out)

    def _serve_artifact(self, requested_name):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "/v1/files is only available on localhost-bound bridges"}})
            return

        if not _valid_artifact_name(requested_name):
            self._json_response(400, {"error": {"message": "invalid filename"}})
            return

        target = self._existing_artifact_path(requested_name)
        if target is None:
            self._json_response(404, {"error": {"message": "file not found"}})
            return

        content_type = {
            ".csv": "text/csv",
            ".html": "text/html",
            ".json": "application/json",
            ".md": "text/markdown",
            ".pdf": "application/pdf",
            ".txt": "text/plain",
        }.get(os.path.splitext(requested_name)[1].lower(), "application/octet-stream")
        content_length = os.path.getsize(target)
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        # Filename is regex-validated; quote and CRLF stripping defend against future relaxation.
        quoted_name = urllib.parse.quote(requested_name, safe="").replace("\r", "").replace("\n", "")
        self.send_header("Content-Disposition", 'attachment; filename="' + quoted_name + '"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(target, "rb") as artifact_file:
            while True:
                chunk = artifact_file.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _purge_artifacts(self):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "/v1/files/purge is only available on localhost-bound bridges"}})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length:
            self.rfile.read(content_length)

        purged = 0
        try:
            os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
            base = os.path.realpath(_ARTIFACTS_DIR)
            for name in os.listdir(_ARTIFACTS_DIR):
                if not _valid_artifact_name(name):
                    continue
                entry_path = os.path.join(_ARTIFACTS_DIR, name)
                target = os.path.realpath(entry_path)
                if not target.startswith(base + os.sep) or not os.path.isfile(target):
                    continue
                try:
                    if os.path.islink(entry_path):
                        os.unlink(entry_path)
                    else:
                        os.remove(entry_path)
                    purged += 1
                except FileNotFoundError:
                    pass
        except OSError as error:
            self._json_response(500, {"error": {"message": "artifact purge failed: " + str(error)}})
            return

        self._json_response(200, {"status": "ok", "purged": purged})

    def _health(self):
        backend = _resolve_memory_backend()
        status = {
            "status": "ok",
            "acp_connected": bool(_st.acp_client and _st.acp_client.alive),
            "session_id": _st.acp_client.session_id if _st.acp_client else None,
            "agent": _st.acp_client.agent_info if _st.acp_client else None,
            "model": _st.acp_client.model if _st.acp_client else None,
            "mcp_servers": list(_st.configured_mcp_config.keys()),
            "cognition_enabled": _st.cognition_enabled,
            "cognition_launch_id": _st.cognition_launch_id,
            "cognition_launch_iso": _st.cognition_launch_iso,
            "memory_backend": backend,
            "memory_available": _memory_available(),
        }
        if backend == "sqlite" and _st.sqlite_mem:
            status["memory_db_path"] = _st.sqlite_mem.db_path
        self._json_response(200, status)

    def _briefing_status(self):
        """Expose cache state only; prepared source content stays out of prompts."""
        self._json_response(200, briefing_status())

    def _runtime_capabilities(self):
        """Return bridge-owned runtime capability readiness without secrets."""
        self._json_response(200, runtime_capabilities())

    def _briefing_refresh(self):
        """Start a fresh bounded source pass for an explicit briefing request."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            content_length = 0
        if content_length > 0:
            self.rfile.read(min(content_length, 16 * 1024))
        started = start_startup_briefing()
        self._json_response(202 if started else 200, briefing_status())

    # ------------------------------------------------------------------
    # Doctor — structured readiness report for all Eva subsystems
    # ------------------------------------------------------------------
    def _doctor(self):
        report = {"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(), "subsystems": {}, "readiness": {}, "blockers": []}

        # ACP / Copilot CLI
        acp_ok = bool(_st.acp_client and _st.acp_client.alive)
        report["subsystems"]["acp"] = {
            "ok": acp_ok,
            "session_id": _st.acp_client.session_id if _st.acp_client else None,
            "model": _st.acp_client.model if _st.acp_client else None,
        }
        if not acp_ok:
            report["blockers"].append("ACP client not connected. Run: copilot auth login")

        # MCP servers
        mcp_names = list(_st.configured_mcp_config.keys())
        report["subsystems"]["mcp"] = {"configured": mcp_names, "count": len(mcp_names)}

        # Browser agent
        ba_module = _BROWSER_AGENT is not None
        ba_playwright = False
        if ba_module:
            try:
                import importlib
                importlib.import_module("playwright")
                ba_playwright = True
            except ImportError:
                pass
        report["subsystems"]["browser_agent"] = {
            "module_loaded": ba_module,
            "playwright_available": ba_playwright,
        }
        if ba_module and not ba_playwright:
            report["blockers"].append("Playwright not installed. Run: pip install playwright && playwright install chromium")

        # Desktop agent
        da_module = _DESKTOP_AGENT is not None
        da_pyautogui = False
        da_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        if da_module:
            try:
                import importlib
                importlib.import_module("pyautogui")
                da_pyautogui = True
            except ImportError:
                pass
        da_ydotool = shutil.which("ydotool") is not None
        da_computer_use = shutil.which("computer-use-linux") is not None
        report["subsystems"]["desktop_agent"] = {
            "module_loaded": da_module,
            "pyautogui_available": da_pyautogui,
            "display_available": da_display,
            "ydotool_available": da_ydotool,
            "computer_use_linux_available": da_computer_use,
        }
        if da_module and not da_display:
            report["blockers"].append("No DISPLAY or WAYLAND_DISPLAY set. Desktop agent requires a graphical session.")

        # Camera
        cam_module = _CAMERA is not None
        cam_cv2 = False
        cam_device = False
        if cam_module:
            cam_cv2, _ = _CAMERA.opencv_available()
            cam_status = _CAMERA.status()
            cam_device = cam_status.get("present", False) or cam_status.get("enabled", False)
        report["subsystems"]["camera"] = {
            "module_loaded": cam_module,
            "opencv_available": cam_cv2,
            "device_present": cam_device,
        }

        # Kusto / memory
        cluster, database = _get_kusto_config()
        kusto_configured = bool(cluster and database)
        kusto_token = bool(_st.kusto_token_cache)
        report["subsystems"]["kusto"] = {
            "configured": kusto_configured,
            "cluster": cluster[:30] + "..." if cluster and len(cluster) > 30 else cluster,
            "database": database,
            "token_valid": kusto_token,
        }
        if not kusto_configured:
            report["blockers"].append("Kusto not configured. Set up in Settings > MCP tab.")
        elif not kusto_token:
            report["blockers"].append("Kusto token expired or unavailable. Re-authenticate.")

        # Background loop
        bg_running = bool(_st.bg_loop_thread and _st.bg_loop_thread.is_alive())
        report["subsystems"]["background"] = {
            "enabled": _st.bg_loop_enabled,
            "running": bg_running,
            "interval_seconds": _st.bg_loop_interval_seconds,
            "last_tick": _st.bg_last_tick_iso,
        }

        # Cron
        with _st.cron_lock:
            cron_count = len(_st.cron_tasks)
            cron_enabled = sum(1 for t in _st.cron_tasks if t.get("enabled", True))
        report["subsystems"]["cron"] = {
            "total_tasks": cron_count,
            "enabled_tasks": cron_enabled,
        }

        # Cognition
        report["subsystems"]["cognition"] = {
            "enabled": _st.cognition_enabled,
            "launch_id": _st.cognition_launch_id,
        }

        # System
        node_version = None
        try:
            node_version = subprocess.check_output(["node", "--version"], stderr=subprocess.DEVNULL, timeout=5).decode().strip()
        except Exception:
            pass
        report["subsystems"]["system"] = {
            "python": sys.version.split()[0],
            "node": node_version,
            "platform": platform.platform(),
            "arch": platform.machine(),
        }

        # Readiness summary
        report["readiness"] = {
            "can_chat": acp_ok,
            "can_browse": ba_module and ba_playwright,
            "can_desktop": da_module and da_display,
            "can_see": cam_module and cam_cv2,
            "can_remember": kusto_configured and kusto_token,
            "can_schedule": bg_running,
            "can_cron": cron_enabled > 0,
        }

        self._json_response(200, report)

    # ------------------------------------------------------------------
    # Cron CRUD endpoints
    # ------------------------------------------------------------------
    def _cron_list(self):
        with _st.cron_lock:
            tasks = list(_st.cron_tasks)
        self._json_response(200, {"tasks": tasks, "count": len(tasks)})

    def _cron_create(self):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "cron mutations restricted to loopback"}})
            return
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        label = str((data or {}).get("label", "")).strip()
        schedule = str((data or {}).get("schedule", "")).strip()
        prompt = str((data or {}).get("prompt", "")).strip()
        if not label or not schedule or not prompt:
            self._json_response(400, {"error": {"message": "label, schedule (cron expr), and prompt are required"}})
            return
        parsed, parse_err = _parse_cron_expr(schedule)
        if parse_err or parsed is None:
            self._json_response(400, {"error": {"message": f"invalid cron expression: {parse_err}"}})
            return
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        task = {
            "id": "cron-" + uuid.uuid4().hex[:8],
            "label": label[:120],
            "schedule": schedule,
            "prompt": prompt[:2000],
            "enabled": bool((data or {}).get("enabled", True)),
            "last_run": "",
            "next_run": _cron_next_run(schedule) or "",
            "created_at": now_iso,
        }
        with _st.cron_lock:
            _st.cron_tasks.append(task)
            _save_cron_tasks()
        self._json_response(201, {"task": task})

    def _cron_update(self, task_id):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "cron mutations restricted to loopback"}})
            return
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        with _st.cron_lock:
            task = next((t for t in _st.cron_tasks if t.get("id") == task_id), None)
            if not task:
                self._json_response(404, {"error": {"message": "cron task not found"}})
                return
            if "label" in (data or {}):
                task["label"] = str(data["label"])[:120]
            if "schedule" in (data or {}):
                new_sched = str(data["schedule"]).strip()
                parsed, parse_err = _parse_cron_expr(new_sched)
                if parse_err or parsed is None:
                    self._json_response(400, {"error": {"message": f"invalid cron expression: {parse_err}"}})
                    return
                task["schedule"] = new_sched
                task["next_run"] = _cron_next_run(new_sched) or ""
            if "prompt" in (data or {}):
                task["prompt"] = str(data["prompt"])[:2000]
            if "enabled" in (data or {}):
                task["enabled"] = bool(data["enabled"])
            _save_cron_tasks()
        self._json_response(200, {"task": task})

    def _cron_delete(self, task_id):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "cron mutations restricted to loopback"}})
            return
        with _st.cron_lock:
            before = len(_st.cron_tasks)
            _st.cron_tasks[:] = [t for t in _st.cron_tasks if t.get("id") != task_id]
            if len(_st.cron_tasks) == before:
                self._json_response(404, {"error": {"message": "cron task not found"}})
                return
            _save_cron_tasks()
        self._json_response(200, {"ok": True})

    # ------------------------------------------------------------------
    # Skills auto-learn — extract a skill from a successful interaction
    # ------------------------------------------------------------------
    def _skills_auto_learn(self):
        """Given recent conversation context, ask the model to extract a reusable skill."""
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "auto-learn restricted to loopback"}})
            return
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        messages = (data or {}).get("messages", [])
        task_summary = str((data or {}).get("task_summary", "")).strip()
        if not messages and not task_summary:
            self._json_response(400, {"error": {"message": "messages or task_summary required"}})
            return

        # Build a conversation digest for the model
        digest_parts = []
        if task_summary:
            digest_parts.append(f"Task: {task_summary}")
        for msg in messages[-20:]:
            role = msg.get("role", "user")
            content = str(msg.get("content", ""))[:500]
            digest_parts.append(f"{role}: {content}")
        digest = "\n".join(digest_parts)[:4000]

        extract_prompt = (
            "You are a skill extraction engine. Given the following successful interaction, "
            "extract a reusable skill that Eva can apply to similar tasks in the future.\n\n"
            "Return a JSON object with these fields:\n"
            '- "Name": short skill name (2-5 words)\n'
            '- "Description": one-sentence description of what this skill does\n'
            '- "Category": exactly one of the primary Skills categories\n'
            '- "Instructions": step-by-step instructions Eva should follow (markdown)\n'
            '- "Tools": comma-separated list of tools/capabilities used\n'
            '- "Tags": comma-separated tags for categorization\n\n'
            "Return ONLY the JSON object, no markdown fencing.\n\n"
            f"Interaction:\n{digest}"
        )

        # Use ACP or LM Studio to generate the skill
        result_text = ""
        if _st.acp_client and _st.acp_client.alive:
            try:
                result = _st.acp_client.send_prompt([
                    {"role": "system", "content": "You extract reusable skills from successful interactions. Output only valid JSON."},
                    {"role": "user", "content": extract_prompt}
                ])
                result_text = str(result or "").strip()
            except Exception as e:
                self._json_response(502, {"error": {"message": f"skill extraction failed: {e}"}})
                return
        else:
            # Fall back to LM Studio
            try:
                from bridge.utils import _load_client_prefs, _validate_lmstudio_base_url
                import urllib.request
                prefs = _load_client_prefs()
                lms_base = (prefs.get("lmstudio_base_url") or "http://localhost:1234/v1").rstrip("/")
                lms_model = prefs.get("lmstudio_model") or ""
                lms_base, lms_error = _validate_lmstudio_base_url(lms_base)
                if lms_error:
                    self._json_response(503, {"error": {"message": f"No agent available: {lms_error}"}})
                    return
                payload = json.dumps({
                    "model": lms_model or "default",
                    "messages": [
                        {"role": "system", "content": "You extract reusable skills from successful interactions. Output only valid JSON, no code fences, no prose."},
                        {"role": "user", "content": extract_prompt},
                    ],
                    "temperature": 0.3,
                }).encode()
                req = urllib.request.Request(lms_base + "/chat/completions", data=payload,
                                            headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = json.loads(resp.read())
                result_text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
            except Exception as e:
                self._json_response(502, {"error": {"message": f"skill extraction failed: {e}"}})
                return

        try:
            draft, err = _parse_evarise_json(result_text)
            if err:
                self._json_response(200, {"draft": None, "raw": result_text[:1000], "error": err})
                return
            draft["Source"] = "auto-learned"
            draft["Status"] = "draft"
            backend, handle, ok = self._memory_context_required()
            if not ok:
                return
            cluster, db = handle if backend == "kusto" else (None, None)
            if backend == "kusto" and not _get_table_columns(cluster, db, "SkillVersions"):
                self._json_response(409, {"error": {"message": "SkillVersions is unavailable; apply the current Kusto seed before auto-learning governed skills"}})
                return
            now = self._goal_now()
            row = {
                "SkillId": "sk-" + uuid.uuid4().hex[:12],
                "Name": str(draft.get("Name") or draft.get("name") or "Untitled Skill")[:60],
                "Description": str(draft.get("Description") or draft.get("description") or "")[:400],
                "Category": _normalize_skill_category(draft.get("Category") or draft.get("category")),
                "Instructions": str(draft.get("Instructions") or draft.get("instructions") or "")[:8000],
                "Tools": str(draft.get("Tools") or draft.get("tools") or "")[:200],
                "Tags": str(draft.get("Tags") or draft.get("tags") or "")[:200],
                "Source": "auto-learned", "Status": "draft", "CreatedAt": now, "UpdatedAt": now,
            }
            if not row["Instructions"]:
                self._json_response(500, {"error": {"message": "could not persist auto-learned draft"}})
                return
            version = None
            promoted = False
            if backend == "sqlite":
                existing = handle.query(
                    "SELECT * FROM Skills WHERE Source = 'auto-learned' AND lower(Name) = lower(?) AND Status != 'deleted' ORDER BY UpdatedAt DESC LIMIT 1",
                    (row["Name"],),
                ) or []
                if existing:
                    row = existing[0]
                elif not self._write_skill_row(cluster, db, row):
                    self._json_response(500, {"error": {"message": "could not persist auto-learned draft"}})
                    return
                memory_model = MemoryModel(handle)
                versions = handle.query(
                    "SELECT * FROM SkillVersions WHERE SkillId = ? ORDER BY Version DESC LIMIT 1", (row["SkillId"],)
                ) or []
                version = versions[0] if versions else memory_model.register_skill_version(
                    row["SkillId"], row["Tools"], "Two successful bounded evaluations are required for provisional use."
                )
            else:
                memory_model = KustoMemoryModel(cluster, db, _kusto_query_direct, _kusto_ingest_direct)
                safe_name = row["Name"].replace("'", "''")
                existing = _kusto_query_direct(
                    cluster, db,
                    _SKILLS_LATEST_QUERY + " | where Source == 'auto-learned' and Name =~ '" + safe_name + "' | take 1",
                ) or []
                if existing:
                    row = existing[0]
                elif not self._write_skill_row(cluster, db, row):
                    self._json_response(500, {"error": {"message": "could not persist auto-learned draft"}})
                    return
                safe_skill_id = str(row["SkillId"]).replace("'", "''")
                versions = _kusto_query_direct(
                    cluster, db, "SkillVersions | where SkillId == '" + safe_skill_id + "' | top 1 by Version desc"
                ) or []
                version = versions[0] if versions else memory_model.register_skill_version(
                    row["SkillId"], row["Tools"], "Two successful bounded evaluations are required for provisional use."
                )
            self._json_response(201, {"skill": row, "version": version, "provisional": promoted})
        except Exception as e:
            self._json_response(502, {"error": {"message": f"skill extraction failed: {e}"}})

    # ------------------------------------------------------------------
    # Subagent parallelism
    # ------------------------------------------------------------------
    def _agents_overview(self):
        """Return a normalized snapshot for the agent operations dashboard."""
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "agent overview restricted to loopback"}})
            return
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        active_session_id = str((params.get("session_id") or [""])[0])[:120]
        active_model = getattr(_st.acp_client, "model", "") if _st.acp_client else ""
        agents = [{
            "id": "eva",
            "kind": "eva",
            "label": "Eva",
            "model": active_model or "adaptive routing",
            "status": "online" if _st.acp_client and _st.acp_client.alive else "local_only",
            "detail": "Primary assistant and cognitive orchestrator",
            "activity": "Ready for conversation and agent orchestration",
            "result": None,
            "started_at": getattr(_st, "cognition_launch_since", None),
            "ended_at": None,
            "session_id": active_session_id,
            "group_id": "eva-core",
            "depends_on": [],
            "signal_status": "",
            "capability_policy": "adaptive",
        }]
        with _st.subagent_lock:
            subagents_active, visible_tasks = _select_subagent_overview_tasks(_st.subagent_tasks)
            for task in visible_tasks:
                agents.append({
                    "id": task.get("id", ""),
                    "kind": "subagent",
                    "label": task.get("label", "Subagent"),
                    "model": task.get("model", ""),
                    "status": task.get("status", "unknown"),
                    "detail": task.get("prompt", ""),
                    "activity": task.get("activity", ""),
                    "result": task.get("result"),
                    "started_at": task.get("started_at"),
                    "ended_at": task.get("ended_at"),
                    "session_id": task.get("session_id", ""),
                    "group_id": task.get("group_id", ""),
                    "depends_on": task.get("depends_on", []),
                    "signal_status": task.get("signal_status", ""),
                    "coding_run_id": task.get("coding_run_id", ""),
                    "checkout_id": task.get("checkout_id", ""),
                    "capability_policy": task.get("capability_policy", ""),
                })

        for module, kind, label in (
            (_BROWSER_AGENT, "browser", "Browser agent"),
            (_DESKTOP_AGENT, "desktop", "Desktop agent"),
        ):
            if module is None:
                continue
            runs = getattr(module, "_runs", {})
            runs_lock = getattr(module, "_runs_lock", None)
            if runs_lock is None:
                continue
            with runs_lock:
                run_ids = list(runs.keys())
            run_snapshots = []
            for run_id in run_ids:
                run = module.public_status(run_id)
                if not run:
                    continue
                run_snapshots.append(run)
            visible_runs = _select_active_history(
                run_snapshots,
                {"starting", "running", "awaiting_confirmation", "awaiting_input"},
                10,
            )
            for run in visible_runs:
                agents.append({
                    "id": run.get("id", ""),
                    "kind": kind,
                    "label": label,
                    "status": run.get("status", "unknown"),
                    "detail": run.get("subgoal") or run.get("goal", ""),
                    "result": run.get("result") or run.get("error"),
                    "started_at": run.get("started"),
                    "ended_at": run.get("finished"),
                    "session_id": "",
                    "step": run.get("step", 0),
                })

        background = _background_status_dict()
        last_activity = background.get("lastActivity") or {}
        if last_activity:
            agents.append({
                "id": last_activity.get("TickId", "background-latest"),
                "kind": "background",
                "label": last_activity.get("JobType", "Background cognition"),
                "status": last_activity.get("Status", "unknown"),
                "detail": last_activity.get("Notes", ""),
                "result": None,
                "started_at": last_activity.get("StartedAt"),
                "ended_at": last_activity.get("EndedAt"),
                "session_id": "",
            })

        include_graph = (params.get("include_graph", ["1"])[0] or "1") != "0"
        graph = None
        if include_graph:
            if _resolve_memory_backend() == "sqlite":
                rows = _get_sqlite_mem().query(
                    "SELECT Entity, Relation, Value, Confidence, Timestamp FROM Knowledge "
                    "WHERE Confidence >= 0.6 AND Relation NOT IN ('mentioned', 'candidate_mentioned', 'recurring_topic') "
                    "ORDER BY Timestamp DESC LIMIT 30"
                ) or []
            else:
                cluster, database = _get_kusto_config()
                rows = _kusto_query_direct(
                    cluster, database,
                    "Knowledge | where Confidence >= 0.6 "
                    "and Relation !in~ ('mentioned', 'candidate_mentioned', 'recurring_topic') "
                    "| order by Timestamp desc | take 30 "
                    "| project Entity, Relation, Value, Confidence, Timestamp"
                ) if cluster and database else []
            graph = _knowledge_graph_snapshot(rows)
            with _st.subagent_lock:
                all_graph_tasks = dict(_st.subagent_tasks)
                _, graph_tasks = _select_subagent_overview_tasks(all_graph_tasks, limit=30)
                dependency_ids = {
                    dependency_id
                    for task in graph_tasks
                    for dependency_id in task.get("depends_on", [])
                }
                visible_ids = {task.get("id") for task in graph_tasks}
                graph_tasks.extend(
                    all_graph_tasks[dependency_id]
                    for dependency_id in dependency_ids
                    if dependency_id in all_graph_tasks and dependency_id not in visible_ids
                )
            _append_agent_topology(graph, graph_tasks)

        other_agents_active = sum(
            1 for item in agents
            if item.get("kind") != "subagent" and item.get("status") in _AGENT_ACTIVE_STATUSES
        )
        visible_agents = _select_agent_payload(agents, limit=30)
        payload = {
            "agents": visible_agents,
            "active_total": subagents_active + other_agents_active,
            "subagents_active": subagents_active,
            "capacity": _SUBAGENT_MAX,
            "background": background,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if graph is not None:
            payload["graph"] = graph
        self._json_response(200, payload)

    def _subagent_spawn(self):
        """Spawn an isolated subagent that runs a prompt concurrently."""
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "subagent restricted to loopback"}})
            return
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        prompt = str((data or {}).get("prompt", "")).strip()
        label = str((data or {}).get("label", "subagent task")).strip()[:120]
        model = str((data or {}).get("model", "")).strip()[:120]
        session_id = str((data or {}).get("session_id", "")).strip()[:120]
        workspace_project_id = str((data or {}).get("workspace_project_id", "")).strip()[:120]
        group_id = str((data or {}).get("group_id", "")).strip()[:120]
        raw_dependencies = (data or {}).get("depends_on", [])
        depends_on = [str(value).strip()[:120] for value in raw_dependencies if str(value).strip()][:3] if isinstance(raw_dependencies, list) else []
        if not prompt:
            self._json_response(400, {"error": {"message": "prompt is required"}})
            return
        task_id = "sub-" + uuid.uuid4().hex[:8]
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        task = {
            "id": task_id,
            "label": label,
            "prompt": prompt[:1200],
            "model": model,
            "status": "waiting" if depends_on else "running",
            "result": None,
            "started_at": now_iso,
            "ended_at": None,
            "session_id": session_id,
            "workspace_project_id": workspace_project_id,
            "group_id": group_id,
            "depends_on": depends_on,
            "signal_on_complete": False,
            "signal_status": "",
            "steer_queue": [],
            "steer_history": [],
        }
        if not _reserve_subagent_task(task):
            self._json_response(429, {"error": {"message": f"max {_SUBAGENT_MAX} concurrent subagents"}})
            return
        try:
            _scope_subagent_task_to_workspace(task)
            thread = threading.Thread(target=_subagent_worker, args=(task_id, prompt, label, model), name=f"subagent-{task_id}", daemon=True)
            thread.start()
        except (WorkspaceError, RuntimeError) as error:
            _discard_subagent_workspace_scope(task, "Generic agent startup failed: " + str(error))
            with _st.subagent_lock:
                _st.subagent_tasks.pop(task_id, None)
            self._json_response(503, {"error": {"message": "Workspace-scoped agent could not start: " + str(error)}})
            return
        self._json_response(202, {"task": _public_subagent_task(task)})

    def _subagent_spawn_batch(self):
        """Atomically reserve and launch one collaborative or independent batch."""
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "subagent restricted to loopback"}})
            return
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        raw_tasks = (data or {}).get("tasks", [])
        if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= _SUBAGENT_MAX:
            self._json_response(400, {"error": {"message": f"tasks must contain 1-{_SUBAGENT_MAX} items"}})
            return
        collaborative = bool((data or {}).get("collaborative")) and len(raw_tasks) > 1
        signal_on_complete = bool((data or {}).get("signal_on_complete")) and collaborative
        if signal_on_complete and not self._require_bridge_capability():
            return
        session_id = str((data or {}).get("session_id", "")).strip()[:120]
        workspace_project_id = str((data or {}).get("workspace_project_id", "")).strip()[:120]
        group_id = str((data or {}).get("group_id", "")).strip()[:120] or "group-" + uuid.uuid4().hex[:10]
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        tasks = []
        for index, raw_task in enumerate(raw_tasks):
            raw_task = raw_task if isinstance(raw_task, dict) else {}
            prompt = str(raw_task.get("prompt", "")).strip()
            if not prompt:
                self._json_response(400, {"error": {"message": f"task {index + 1} prompt is required"}})
                return
            tasks.append({
                "id": "sub-" + uuid.uuid4().hex[:8],
                "label": str(raw_task.get("label", f"Agent {index + 1}")).strip()[:120],
                "prompt": prompt[:1200],
                "_full_prompt": prompt,
                "model": str(raw_task.get("model", "")).strip()[:120],
                "status": "running",
                "result": None,
                "started_at": now_iso,
                "ended_at": None,
                "session_id": session_id,
                "workspace_project_id": workspace_project_id,
                "group_id": group_id,
                "depends_on": [],
                "signal_on_complete": False,
                "signal_status": "",
                "steer_queue": [],
                "steer_history": [],
            })
        if collaborative:
            synthesis = tasks[-1]
            synthesis["depends_on"] = [task["id"] for task in tasks[:-1]]
            synthesis["status"] = "waiting"
            synthesis["signal_on_complete"] = signal_on_complete
            synthesis["signal_status"] = "queued" if signal_on_complete else ""
        if not _reserve_subagent_batch(tasks):
            with _st.subagent_lock:
                available = max(0, _SUBAGENT_MAX - _subagent_active_count())
            self._json_response(429, {"error": {"message": f"batch needs {len(tasks)} slots; {available} available"}})
            return
        try:
            for task in tasks:
                _scope_subagent_task_to_workspace(task)
        except (WorkspaceError, RuntimeError) as error:
            for task in tasks:
                _discard_subagent_workspace_scope(task, "Generic agent batch startup failed: " + str(error))
            with _st.subagent_lock:
                for task in tasks:
                    _st.subagent_tasks.pop(task["id"], None)
            self._json_response(503, {"error": {"message": "Workspace-scoped agent batch could not start: " + str(error)}})
            return
        if not _start_reserved_subagent_batch(tasks):
            for task in tasks:
                _discard_subagent_workspace_scope(task, "Generic agent batch worker startup failed")
            self._json_response(500, {"error": {"message": "batch worker startup failed; no tasks were launched"}})
            return
        self._json_response(202, {
            "tasks": [_public_subagent_task(task) for task in tasks],
            "group_id": group_id,
            "collaborative": collaborative,
            "deferred_signal": signal_on_complete,
        })

    def _subagent_steer(self):
        """Queue a direction for a running task or resume a completed task."""
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "subagent restricted to loopback"}})
            return
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        task_id = str((data or {}).get("id", "")).strip()
        instruction = str((data or {}).get("instruction", "")).strip()[:2000]
        if not task_id or not instruction:
            self._json_response(400, {"error": {"message": "id and instruction are required"}})
            return

        with _st.subagent_lock:
            task = _st.subagent_tasks.get(task_id)
            if not task:
                self._json_response(404, {"error": {"message": "subagent task not found"}})
                return
            if task.get("status") == "finalizing":
                self._json_response(409, {"error": {"message": "task is finalizing completion delivery"}})
                return
            steer = _prepare_subagent_steer(task, instruction)
            if steer is None:
                self._json_response(429, {"error": {"message": f"max {_SUBAGENT_MAX} concurrent subagents"}})
                return
            public_task = _public_subagent_task(task)

        if steer["restart"]:
            thread = threading.Thread(
                target=_subagent_worker,
                args=(task_id, steer["prompt"], task.get("label", "subagent task"), task.get("model", "")),
                name=f"subagent-steer-{task_id}",
                daemon=True,
            )
            thread.start()
        self._json_response(202, {"task": public_task, "queued": not steer["restart"]})

    def _subagent_status(self):
        """Return status of all subagent tasks, or a specific one via ?id=..."""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        task_id = (params.get("id", [""])[0] or "").strip()
        with _st.subagent_lock:
            if task_id:
                task = _st.subagent_tasks.get(task_id)
                if not task:
                    self._json_response(404, {"error": {"message": "subagent task not found"}})
                    return
                self._json_response(200, {"task": _public_subagent_task(task)})
            else:
                tasks = [_public_subagent_task(t) for t in _st.subagent_tasks.values()]
                active = _subagent_active_count()
                self._json_response(200, {"tasks": tasks[-20:], "running": active, "max": _SUBAGENT_MAX})

    def _subagent_dismiss(self, task_id):
        """Dismiss a completed/error/cancelled task from Agent Operations."""
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "subagent restricted to loopback"}})
            return
        dismissed, reason = _dismiss_subagent_task((task_id or "").strip())
        if dismissed:
            self._json_response(200, {"dismissed": task_id})
        elif reason == "not_found":
            self._json_response(404, {"error": {"message": "subagent task not found"}})
        elif reason == "dependency":
            self._json_response(409, {"error": {"message": "task is still required by an active dependent agent"}})
        else:
            self._json_response(409, {"error": {"message": "only completed, failed, or cancelled tasks can be dismissed"}})

    def _models(self):
        models = {
            "object": "list",
            "data": [
                {
                    "id": "copilot",
                    "object": "model",
                    "owned_by": "github",
                    "description": "GitHub Copilot via ACP — uses your Copilot license model (GPT-4o, Claude, Gemini, etc.)"
                }
            ]
        }
        self._json_response(200, models)

    def _mcp_persisted_config(self):
        """Return the persisted front-end MCP selection (secrets stripped) so the
        UI can restore its configuration when the Electron file:// localStorage
        has been cleared across an app rebuild or restart."""
        self._json_response(200, {"mcp_servers": _load_persisted_mcp_config()})

    def _telemetry_report(self):
        """Return recent telemetry events plus aggregate latency/behavior stats.
        Query params: ?limit=N (default 100, max 300), ?event=<name> filter."""
        from urllib.parse import urlparse, parse_qs
        params = parse_qs(urlparse(self.path).query)
        try:
            limit = int(params.get("limit", ["100"])[0])
        except ValueError:
            limit = 100
        limit = max(1, min(limit, _TELEMETRY_RING_MAX))
        event_filter = (params.get("event", [""])[0] or "").strip()
        with _st.telemetry_lock:
            events = list(_st.telemetry_ring)
        if event_filter:
            events = [e for e in events if e.get("event") == event_filter]
        recent = events[-limit:]
        self._json_response(200, {
            "enabled": _TELEMETRY_ENABLED,
            "count": len(recent),
            "total_in_memory": len(_st.telemetry_ring),
            "summary": _telemetry_summarize(recent),
            "events": recent,
        })

    def _logs_view(self):
        """Return recent stdout log lines for the voice-mode background feed.
        Query params: ?since=<seq> (only lines newer than this), ?limit=N."""
        from urllib.parse import urlparse, parse_qs
        params = parse_qs(urlparse(self.path).query)
        try:
            since = int(params.get("since", ["0"])[0])
        except ValueError:
            since = 0
        try:
            limit = int(params.get("limit", ["60"])[0])
        except ValueError:
            limit = 60
        limit = max(1, min(limit, _LOG_RING_MAX))
        with _st.log_lock:
            rows = [{"n": n, "text": t} for (n, t) in _st.log_ring if n > since]
            last = _st.log_seq
        rows = rows[-limit:]
        self._json_response(200, {"lines": rows, "last": last})

    def _telemetry_ingest(self):
        """Accept a privacy-safe cognition timing record from the front end and
        fold it into the same telemetry log. Only known numeric/label fields are
        kept; any unexpected or oversized values are dropped/clipped."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._json_response(400, {"error": {"message": "Empty request body"}})
            return
        try:
            data = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            self._json_response(400, {"error": {"message": "Invalid JSON"}})
            return
        if not isinstance(data, dict):
            self._json_response(400, {"error": {"message": "Body must be an object"}})
            return
        _num_keys = ("turn_ms", "draft_ms", "review_ms", "revise_ms",
                     "cycles", "draft_chars", "final_chars")
        _label_keys = ("eva_model", "reviewer_model", "review_reason",
                       "last_verdict", "sentinel_want")
        fields = {}
        for k in _num_keys:
            v = data.get(k)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                fields[k] = v
        for k in _label_keys:
            if k in data and data[k] is not None:
                v = data[k]
                fields[k] = v if isinstance(v, bool) else _telemetry_clip(v, 60)
        _telemetry_emit("cognition_turn", source="frontend", **fields)
        self._json_response(200, {"status": "ok"})

    def _audit_event_ingest(self):
        """Accept a strictly bounded renderer lifecycle event for local audit."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._json_response(400, {"error": {"message": "Empty request body"}})
            return
        try:
            data = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (json.JSONDecodeError, ValueError):
            self._json_response(400, {"error": {"message": "Invalid JSON"}})
            return
        if not isinstance(data, dict):
            self._json_response(400, {"error": {"message": "Body must be an object"}})
            return
        event = str(data.get("event") or "")[:64]
        outcome = str(data.get("outcome") or "")[:32]
        correlation_id = str(data.get("correlation_id") or "")[:120]
        allowed_events = {"turn.input", "turn.rendered", "native_action", "direct_route", "terminal_task", "voice.command"}
        allowed_outcomes = {"started", "planned", "completed", "cancelled", "failed", "submitted"}
        allowed_reasons = {"authentication", "timeout", "unavailable", "failed", "cancelled"}
        if event not in allowed_events or outcome not in allowed_outcomes:
            self._json_response(400, {"error": {"message": "Unsupported audit event"}})
            return
        if data.get("reason") is not None and data.get("reason") not in allowed_reasons:
            self._json_response(400, {"error": {"message": "Unsupported audit reason"}})
            return
        allowed_fields = {"action", "model", "provider", "request_chars", "response_chars", "label", "reason"}
        fields = {}
        for key in allowed_fields:
            value = data.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                fields[key] = value
            elif isinstance(value, str):
                fields[key] = value[:120]
        audit_event(event, correlation_id, outcome, **fields)
        self._json_response(200, {"status": "ok"})

    def _notifications_list(self):
        """Return recent proactive notifications for the front end to surface.
        Query params: ?unseen_only=1, ?since=<id>, ?limit=N (default 20, max 100)."""
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        unseen_only = params.get("unseen_only", ["0"])[0] in ("1", "true", "yes")
        since = (params.get("since", [""])[0] or "").strip()
        try:
            limit = int(params.get("limit", ["20"])[0])
        except ValueError:
            limit = 20
        limit = max(1, min(limit, _NOTIFY_RING_MAX))
        with _st.notify_lock:
            items = list(_st.notify_ring)
        if since:
            idx = next((i for i, r in enumerate(items) if r.get("id") == since), None)
            if idx is not None:
                items = items[idx + 1:]
        if unseen_only:
            items = [r for r in items if not r.get("seen")]
        items = items[-limit:]
        self._json_response(200, {"notifications": items, "count": len(items)})

    def _notifications_mark_seen(self):
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        ids = data.get("ids") if isinstance(data, dict) else None
        if not isinstance(ids, list):
            self._json_response(400, {"error": {"message": "ids must be a list"}})
            return
        updated = _notify_mark_seen(ids)
        self._json_response(200, {"status": "ok", "updated": updated})

    def _alerts_list(self):
        doc = _load_alerts()
        self._json_response(200, {"alerts": doc.get("alerts", []), "settings": doc.get("settings", {}),
                                  "types": list(_ALERT_TYPES), "channels": list(_ALERT_CHANNELS)})

    def _alerts_upsert(self):
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        with _st.alerts_lock:
            doc = _load_alerts()
            existing = None
            rid_in = _alert_clip(data.get("id"), 64) if isinstance(data, dict) else ""
            if rid_in:
                existing = next((r for r in doc["alerts"] if r.get("id") == rid_in), None)
            rule, rule_error = _sanitize_alert_rule(data, existing)
            if rule_error:
                self._json_response(400, {"error": {"message": rule_error}})
                return
            replaced = False
            for i, r in enumerate(doc["alerts"]):
                if r.get("id") == rule["id"]:
                    doc["alerts"][i] = rule
                    replaced = True
                    break
            if not replaced:
                if len(doc["alerts"]) >= 50:
                    self._json_response(400, {"error": {"message": "alert limit reached (50)"}})
                    return
                doc["alerts"].append(rule)
            _save_alerts(doc)
        self._json_response(200, {"status": "ok", "alert": rule})

    def _alerts_delete(self, rule_id):
        rule_id = str(rule_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rule_id):
            self._json_response(400, {"error": {"message": "alert id is invalid"}})
            return
        with _st.alerts_lock:
            doc = _load_alerts()
            before = len(doc["alerts"])
            doc["alerts"] = [r for r in doc["alerts"] if r.get("id") != rule_id]
            removed = before - len(doc["alerts"])
            if removed:
                _save_alerts(doc)
        self._json_response(200, {"status": "ok", "removed": removed})

    def _alerts_settings_update(self):
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        with _st.alerts_lock:
            doc = _load_alerts()
            doc["settings"] = _sanitize_alert_settings(data)
            _save_alerts(doc)
        self._json_response(200, {"status": "ok", "settings": doc["settings"]})

    def _signal_send_request(self):
        """Send one final-response Signal message from a loopback UI request."""
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "Signal sending is restricted to loopback bind"}})
            return
        if not self._require_bridge_capability():
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._json_response(415, {"error": {"message": "Content-Type must be application/json"}})
            return
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, str) or not message.strip():
            self._json_response(400, {"error": {"message": "message must be a non-empty string"}})
            return
        message = message.strip()
        if len(message) > 4000:
            self._json_response(400, {"error": {"message": "message must be 4000 characters or fewer"}})
            return
        if not _signal_send(message):
            self._json_response(502, {"error": {"message": "signal-cli could not deliver the message"}})
            return
        self._json_response(200, {"status": "sent"})

    # ── Email ──────────────────────────────────────────────────────────
    def _email_body(self):
        """Read a bounded JSON object body for an email request."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None, "Invalid Content-Length"
        if content_length <= 0:
            return None, "Empty request body"
        if content_length > 512 * 1024:
            return None, "Email request body exceeds the limit"
        data, error = self._read_json_body()
        if error:
            return None, error
        if not isinstance(data, dict):
            return None, "Email request body must be an object"
        return data, ""

    def _email_accounts_get(self):
        from bridge import email_service
        self._json_response(200, {
            "accounts": email_service.public_accounts(),
            "allowlist": email_service.load_config().get("allowlist", []),
        })

    def _email_accounts_update(self):
        from bridge import email_service
        data, error = self._email_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        try:
            document, errors = email_service.replace_accounts(data.get("accounts"), data.get("allowlist"))
        except email_service.EmailValidationError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        except email_service.EmailPersistenceError as exc:
            self._json_response(500, {"error": {"message": str(exc)}})
            return
        if errors:
            self._json_response(400, {
                "error": {"message": "; ".join(errors)},
                "errors": errors,
            })
            return
        self._json_response(200, {
            "accounts": email_service.public_accounts(document),
            "allowlist": document.get("allowlist", []),
            "errors": [],
        })

    def _email_account_upsert(self):
        from bridge import email_service
        data, error = self._email_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        try:
            document = email_service.upsert_account(data.get("account"))
        except email_service.EmailValidationError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        except email_service.EmailPersistenceError as exc:
            self._json_response(500, {"error": {"message": str(exc)}})
            return
        self._json_response(200, {
            "accounts": email_service.public_accounts(document),
            "allowlist": document.get("allowlist", []),
        })

    def _email_allowlist_update(self):
        from bridge import email_service
        data, error = self._email_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        try:
            document = email_service.update_allowlist(data.get("allowlist"))
        except email_service.EmailValidationError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        except email_service.EmailPersistenceError as exc:
            self._json_response(500, {"error": {"message": str(exc)}})
            return
        self._json_response(200, {
            "accounts": email_service.public_accounts(document),
            "allowlist": document.get("allowlist", []),
        })

    def _email_credential_set(self):
        """Accept a mailbox secret held only in bridge memory."""
        from bridge import email_service
        data, error = self._email_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        try:
            email_service.set_credential(data.get("account_id"), data.get("credential"))
        except email_service.EmailValidationError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        except email_service.EmailPersistenceError as exc:
            self._json_response(500, {"error": {"message": str(exc)}})
            return
        except email_service.EmailServiceError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        self._json_response(200, {"status": "stored"})

    def _email_messages_list(self):
        from bridge import email_service
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            limit = int((query.get("limit") or ["10"])[0])
        except ValueError:
            limit = 10
        try:
            messages = email_service.fetch_messages(
                (query.get("account_id") or [""])[0],
                folder=(query.get("folder") or ["INBOX"])[0],
                limit=limit,
                unseen_only=(query.get("unseen") or [""])[0] == "1",
            )
        except email_service.EmailServiceError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        self._json_response(200, {"messages": messages})

    def _email_exim_status(self):
        from bridge import email_service
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            result = email_service.inspect_local_mta_status(
                (query.get("account_id") or [""])[0],
                (query.get("queue_id") or [""])[0],
            )
        except email_service.EmailValidationError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        except email_service.EmailServiceError as exc:
            self._json_response(503, {"error": {"message": str(exc)}})
            return
        self._json_response(200, result)

    def _email_send_request(self):
        """Authorize and deliver one message. Delivery requires a capability token."""
        from bridge import email_service
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "Email sending is restricted to loopback bind"}})
            return
        if not self._require_bridge_capability():
            return
        data, error = self._email_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        try:
            result = email_service.send_message(
                data.get("message"),
                account_id=str(data.get("account_id") or ""),
                from_address=str(data.get("from") or ""),
                confirmation=data.get("confirmation"),
            )
        except email_service.EmailServiceError as exc:
            self._json_response(502, {"error": {"message": str(exc)}})
            return
        status = 200 if result.get("decision") in (
            "sent", "submitted", "partially_sent", "needs_confirmation"
        ) else 400
        if status == 400:
            # Carry the refusal reason in the error envelope so the caller can
            # show why, not just that it failed.
            result = dict(result)
            result["error"] = {"message": result.get("reason") or "The message was refused."}
        self._json_response(status, result)

    def _email_message_delete(self, account_id):
        from bridge import email_service
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            email_service.delete_message(
                account_id,
                (query.get("folder") or ["INBOX"])[0],
                (query.get("message_id") or [""])[0],
            )
        except email_service.EmailServiceError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        self._json_response(200, {"status": "deleted"})

    def _email_account_delete(self, account_id):
        from bridge import email_service
        try:
            document = email_service.delete_account(account_id)
        except email_service.EmailPersistenceError as exc:
            self._json_response(500, {"error": {"message": str(exc)}})
            return
        except email_service.EmailValidationError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        self._json_response(200, {
            "accounts": email_service.public_accounts(document),
            "allowlist": document.get("allowlist", []),
        })

    def _mcp_status(self):
        """Return current MCP server configuration status."""
        config = _st.configured_mcp_config
        local_manager = _st.local_mcp_manager if _st.local_mode else None
        active_servers = [
            name for name, server in (local_manager.servers.items() if local_manager else [])
            if server.alive
        ]
        unavailable_servers = dict(getattr(local_manager, "start_failures", {})) if local_manager else {}
        # Redact sensitive env vars (tokens, keys, secrets) before sending to browser
        safe_config = {}
        for srv_name, srv_cfg in config.items():
            safe_srv = dict(srv_cfg)
            if "env" in safe_srv:
                safe_env = {}
                for k, v in safe_srv["env"].items():
                    if any(s in k.upper() for s in ("TOKEN", "KEY", "SECRET", "PAT", "PASSWORD", "CREDENTIAL")):
                        safe_env[k] = "***REDACTED***"
                    else:
                        safe_env[k] = v
                safe_srv["env"] = safe_env
            safe_config[srv_name] = safe_srv
        self._json_response(200, {
            "mcp_servers": safe_config,
            "configured": list(config.keys()) if config else [],
            "active": active_servers if local_manager else (list(config.keys()) if config else []),
            "unavailable": unavailable_servers,
            "presets": {
                "azure": {
                    "description": "Azure MCP Server — 42+ Azure services including Kusto/ADX",
                    "command": "npx",
                    "args": ["-y", "@azure/mcp@latest", "server", "start"]
                },
                "github": {
                    "description": "GitHub MCP Server — repos, issues, PRs, actions, code search",
                    "command": "docker",
                    "args": ["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/github-mcp-server"],
                    "env_required": ["GITHUB_PERSONAL_ACCESS_TOKEN"]
                }
            }
        })

    def _translate(self):
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        if not isinstance(data, dict):
            self._json_response(400, {"error": {"message": "Request body must be an object"}})
            return
        text = data.get("input")
        target_language = data.get("target_language")
        model = data.get("model")
        if not isinstance(text, str) or not text.strip() or len(text) > 1600:
            self._json_response(400, {"error": {"message": "Translation input must be 1 to 1600 characters"}})
            return
        if not isinstance(target_language, str) or target_language not in {"English", "Korean", "Spanish", "Ukrainian"}:
            self._json_response(400, {"error": {"message": "Unsupported translation target language"}})
            return
        if not isinstance(model, str) or not model.strip() or len(model) > 120:
            self._json_response(400, {"error": {"message": "Translation model is required"}})
            return
        prompt = "Translate the spoken text into " + target_language + ". Return only the natural translation."
        self._aig_chat({
            "messages": [
                {"role": "system", "content": "You are a real-time interpreter. Return only the translation."},
                {"role": "user", "content": prompt + "\n\nSpoken text: " + text.strip()},
            ],
            "user_message": prompt + "\n\nSpoken text: " + text.strip(),
            "internal": True,
            "no_tools": True,
            "translation_mode": True,
            "model": model.strip(),
            "max_completion_tokens": 64,
            "acp_reasoning_effort": "",
            "lmstudio_base_url": data.get("lmstudio_base_url", ""),
            "lmstudio_model": data.get("lmstudio_model", ""),
            "openai_api_key": data.get("openai_api_key", ""),
        })

    def _aig_chat(self, data=None):
        """AIG orchestrator — intelligently routes to the best model for each task."""
        if data is None:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                self._json_response(400, {"error": {"message": "Empty request body"}})
                return
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._json_response(400, {"error": {"message": "Invalid JSON"}})
                return

        openai_api_key = (data.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")).strip()
        try:
            request = normalize_aig_request(
                data,
                parse_backend=_parse_aig_backend,
                completion_token_limit=_completion_token_limit,
                allowed_reasoning_efforts=ACP_REASONING_EFFORTS,
                openai_api_key=openai_api_key,
            )
        except ValueError as error:
            self._json_response(400, {"error": {"message": str(error)}})
            return
        messages = request["messages"]
        user_message = request["user_message"]
        translation_mode = request["translation_mode"]
        native_terminal_candidate = request["native_terminal_candidate"]
        native_terminal_plan = request["native_terminal_plan"]
        internal = request["internal"]
        inject_memory = request["inject_memory"]
        recall_query = request["recall_query"]
        no_tools = request["no_tools"]
        conversation_id = request["conversation_id"]
        requested_backend = request["requested_backend"]
        _policy_mode = request["model_policy_mode"]
        responder_provider = request["responder_provider"]
        model_for_response = request["model_for_response"]
        max_completion_tokens = request["max_completion_tokens"]
        reasoning_effort = request["reasoning_effort"]
        acp_auto_approve = request["acp_auto_approve"]
        stream_requested = request["stream_requested"]
        image_b64 = str(data.get("image_b64") or "")
        image_mime = str(data.get("image_mime") or "image/jpeg").lower()
        if image_b64 and (
            len(image_b64) > 12 * 1024 * 1024
            or not re.fullmatch(r"image/(?:jpeg|png|webp|gif)", image_mime)
        ):
            self._json_response(400, {"error": {"message": "Unsupported or oversized image attachment."}})
            return
        _set_openai_key_from(data)  # cache key for semantic recall (incl. background threads)
        _mark_user_activity()
        _turn_t0 = time.perf_counter()
        turn_id = str(data.get("turn_id") or uuid.uuid4())[:120]

        import re as _re
        _routing_message = _effective_routing_message(user_message, internal, recall_query)
        msg_lower = _routing_message.lower()
        _request_type = _classify_request_type(msg_lower)
        _fast_route = _classify_fast_route(_routing_message)
        _passive_recall = _is_passive_memory_recall(_routing_message)
        _approved_approximate_location = str(
            os.environ.get("EVA_APPROVED_APPROXIMATE_LOCATION", "") or ""
        )[:120]
        _skill_decision, _selected_skill, _weather_location = _skill_execution_for_request(
            _routing_message, _approved_approximate_location
        )
        _acp_permission_mode = "passive_recall" if _passive_recall else (
            "workspace_auto" if acp_auto_approve else "interactive"
        )
        _prompt_fields = _prompt_budget_fields(data.get("prompt_budget"))
        stream_state = self._new_stream_state("aig", requested_backend) if stream_requested else None
        if stream_state:
            self._stream_event(stream_state, {
                "type": "status",
                "phase": "thinking",
                "text": "Eva is preparing context...",
            })

        selected_backend = requested_backend
        _policy_decision = {}

        def _audit_turn_failed(provider, error_type, status_code=None):
            audit_event(
                "turn.response", turn_id, "failed",
                model=model_for_response,
                provider=provider,
                request_type=_request_type,
            requested_backend=requested_backend,
            selected_backend=selected_backend,
            policy_mode=_policy_mode,
            policy_reason=_policy_decision.get("reason", ""),
                error_type=error_type,
                status_code=status_code,
                total_ms=round((time.perf_counter() - _turn_t0) * 1000.0, 1),
            )

        print(f"[AIG] Processing request: type={_request_type} chars={len(user_message)}")

        # Step 1: Build memory context. Timings stay privacy-safe: no prompt or
        # response content is emitted into telemetry.
        _memory_t0 = time.perf_counter()
        # Skip for internal calls (cognition sub-calls already have context)
        if _fast_route:
            memory_context = ""
            print(f"[AIG] Fast route: skipping memory assembly ({_fast_route})")
        elif internal:
            # Cognition draft/revise stages opt in to recall via recall_query so
            # the cognitive layer (default ON) does not bypass persistent memory.
            if inject_memory and recall_query and _st.cognition_enabled:
                memory_context = _build_memory_context(recall_query, conversation_id, _skill_decision)
                if memory_context:
                    print(f"[AIG] Internal call: injected {len(memory_context)} chars of memory context (recall)")
                else:
                    print("[AIG] Internal call: recall requested but no memory context produced")
            else:
                memory_context = ""
                print("[AIG] Internal call: skipping memory injection")
        else:
            memory_context = _build_memory_context(user_message, conversation_id, _skill_decision) if _st.cognition_enabled else ""
            if memory_context:
                print(f"[AIG] Injected {len(memory_context)} chars of memory context")
        _memory_ms = round((time.perf_counter() - _memory_t0) * 1000.0, 1)

        # Step 2: ACP-first routing — ACP is the default path (it has MCP tools).
        # Skip ACP data retrieval for internal calls (cognition sub-calls)
        # and for trivial conversational messages with high confidence.
        # retrieve_data: cognition draft calls opt in to live-data retrieval.
        preflight = plan_aig_preflight(
            _routing_message,
            _request_type,
            _fast_route,
            internal,
            bool(data.get("retrieve_data")),
            bool(_st.acp_client and _st.acp_client.alive),
            _st.local_mode,
            no_tools,
            _needs_acp_preflight,
            _select_acp_tool_profile,
        )
        skip_acp = preflight["skip_acp"]
        _acp_route = preflight["acp_route"]
        _briefing_request = preflight["briefing_request"]
        needs_acp_tools = preflight["needs_acp_tools"]
        _tool_profile = preflight["tool_profile"]
        _escalation = preflight["escalation"]
        _selected_tool = str(_skill_decision.get("selected_tool", ""))
        if _selected_tool in {"weather-news", "data-retrieval", "web-search"} or _request_type == "weather-search" and _selected_tool:
            _tool_profile = "web"
        if _request_type == "weather-search" and not _weather_location.get("location"):
            needs_acp_tools = False
            _tool_profile = "none"
        if _request_type == "weather-search" and _skill_decision.get("status") == "unavailable":
            needs_acp_tools = False
        _briefing_status = briefing_status() if _briefing_request else {}
        _briefing_state = _briefing_status.get("status", "idle")
        _briefing_preparing = _briefing_state == "preparing"
        _briefing_unavailable = briefing_unavailable_sources(_briefing_status) if _briefing_request else []
        _briefing_context = briefing_prompt_context(allow_partial=_briefing_request) if _briefing_request else ""
        _tool_required_request = bool(
            not _briefing_request
            and not _fast_route
            and not _passive_recall
            and _request_type in {
                "news-search", "weather-search", "financial-data", "web-search",
                "github-data", "kusto-query", "kusto-operator",
            }
            and not (_request_type == "weather-search" and not _weather_location.get("location"))
        )
        _deep_reasoning_request = bool(re.search(
            r"\b(?:analy[sz]e|architecture|security|audit|strategy|compare|comparison|evaluate|trade[- ]?offs?|"
            r"reason about|design|consequential|complex)\b", _routing_message, re.IGNORECASE
        ))
        _policy_candidates = {
            "acp_available": bool(_st.acp_client and _st.acp_client.alive),
            "acp_model": (_st.acp_client.model if _st.acp_client else "") or model_for_response,
            "openai_available": bool(openai_api_key),
            "openai_model": model_for_response if responder_provider == "openai" else "gpt-5.6-luna",
            "openai_deep_model": "gpt-5.6-sol",
            "lmstudio_available": data.get("lmstudio_available") is True,
            "lmstudio_model": str(data.get("lmstudio_model") or "local")[:80],
        }
        _policy_decision = select_model_policy(
            _policy_mode, requested_backend, _request_type,
            needs_acp_tools or _tool_required_request, _policy_candidates,
            local_only=_st.local_mode,
            deep_reasoning=_deep_reasoning_request,
        )
        selected_backend = str(_policy_decision.get("backend") or requested_backend)
        if _policy_mode != "pinned" and _policy_decision.get("provider") != "pinned":
            responder_provider, model_for_response = _parse_aig_backend(selected_backend)
        def _audit_policy_selection():
            audit_event(
                "turn.accepted", turn_id, "started",
                request_type=_request_type,
                requested_backend=requested_backend,
                selected_backend=selected_backend,
                policy_mode=_policy_mode,
                user_chars=len(user_message),
                internal=internal,
            )
            audit_event(
                "model_policy.selected", turn_id, "selected",
                policy_mode=_policy_mode,
                provider=_policy_decision.get("provider"),
                backend=_policy_decision.get("backend"),
                reason=_policy_decision.get("reason"),
                requires_tools=needs_acp_tools or _tool_required_request,
            )
        _audit_policy_selection()
        if _policy_mode != "pinned" and _policy_decision.get("reason") == "tool-route-unavailable":
            _audit_turn_failed("model-policy", "no-tool-capable-route", 503)
            message = "This request needs live tools, but no tool-capable route is available."
            if stream_state:
                self._stream_error(stream_state, message, 503)
            else:
                self._json_response(503, {"error": {"message": message}})
            return
        if _policy_mode != "pinned" and _policy_decision.get("reason") == "no-auto-candidate":
            _audit_turn_failed("model-policy", "no-available-responder", 503)
            message = "Automatic model selection found no available responder."
            if stream_state:
                self._stream_error(stream_state, message, 503)
            else:
                self._json_response(503, {"error": {"message": message}})
            return
        _verbose_debug_emit(
            "route",
            request_type=_request_type,
            selected_provider=responder_provider,
            selected_model=model_for_response,
            internal=internal,
            no_tools=no_tools,
            acp_preflight=needs_acp_tools,
            tool_profile=_tool_profile,
        )
        audit_event(
            "skill.execution", turn_id, _skill_decision.get("status", "no-match"),
            skill_id=_skill_decision.get("selected_skill_id", ""),
            skill_name=_skill_decision.get("selected_skill_name", ""),
            selection_reason=_skill_decision.get("selection_reason", ""),
            selected_tool=_skill_decision.get("selected_tool", ""),
            fallback_reason=_skill_decision.get("fallback_reason", ""),
        )
        if _briefing_request:
            audit_event("briefing.cache", turn_id, "used", prepared_chars=len(_briefing_context))
        if skip_acp:
            print(f"[AIG] Skipping ACP ({_acp_route})")
        else:
            print(f"[AIG] ACP-first routing: {_request_type}")

        # Raw-output mode avoids PAT restyling to reduce fabricated "live" results.
        raw_output_requested = bool(_re.search(
            r'\b(raw outputs?|raw rows?|raw results?|verbatim|exact output|return only|no commentary|no explanation)\b',
            msg_lower
        )) and needs_acp_tools

        row_recall_requested = bool(_re.search(
            r'\b(latest|recent|rows?|records?)\b',
            msg_lower
        )) and bool(_re.search(
            r'\b(table|reflections|goals|conversations|knowledge|selfstate|emotionstate|memorysummaries|heuristicsindex|emotionbaseline|backgroundproposals|backgroundactivity)\b',
            msg_lower
        )) and needs_acp_tools

        acp_data = ""
        acp_model_used = ""
        _preflight_ms = 0.0
        _preflight_attempted = bool(needs_acp_tools)
        _preflight_succeeded = False
        _retrieval_message = user_message
        if _request_type == "weather-search":
            _retrieval_message = build_weather_retrieval_prompt(
                user_message,
                _weather_location.get("location", ""),
                _skill_decision.get("selected_tool", ""),
            )
        if needs_acp_tools or (_st.local_mode and _tool_required_request):
            _preflight_t0 = time.perf_counter()
            print(f"[AIG] Step 2: Using {'local MCP' if _st.local_mode else 'ACP'} ({_request_type})...")
            if stream_state:
                self._stream_event(stream_state, {
                    "type": "status",
                    "phase": "thinking",
                    "text": "Eva is retrieving live data...",
                })
            if _st.local_mode:
                acp_data, acp_model_used = self._retrieve_local_data(_retrieval_message)
                if _tool_required_request and not acp_data:
                    _audit_turn_failed("local-mcp", "no-tool-result", 503)
                    message = _missing_tool_result_message(True)
                    if stream_state:
                        self._stream_error(stream_state, message, 503)
                    else:
                        self._json_response(503, {"error": {"message": message}})
                    return
                _preflight_succeeded = bool(acp_data)
                needs_acp_tools = False
            # Ensure ACP is alive before attempting tool calls.
            # The CLI may have died between requests (idle timeout, crash).
            if needs_acp_tools and (not _st.acp_client or not _st.acp_client.alive):
                ok, _ = _ensure_acp_model(
                    _st.acp_client.model if _st.acp_client else "",
                    tool_profile=_tool_profile,
                )
                if not ok:
                    needs_acp_tools = False
                    print("[AIG] ACP restart failed, skipping data retrieval")
        if needs_acp_tools:
            # Use ACP to run the data query (it has MCP tools)
            if raw_output_requested and _request_type != "weather-search":
                acp_prompt = (
                    "You are a strict Kusto query executor. "
                    "Execute the appropriate Kusto MCP tool for the user request and return ONLY the final tool output text. "
                    "Do not add headings, markdown, explanations, or invented rows.\n\n"
                    f"{_retrieval_message}"
                )
            elif _request_type in ("news-search", "weather-search", "financial-data", "web-search"):
                acp_prompt = (
                    "You are a research assistant with web search tools. "
                    "Use your available tools to search the web and find REAL, CURRENT information for the user's request. "
                    "Return factual results with sources. Do NOT invent or guess information. "
                    "If no tools return results, say 'No results found' — do NOT fabricate data.\n\n"
                    f"{_retrieval_message}"
                )
            elif _request_type in ("kusto-query", "kusto-operator"):
                acp_prompt = (
                    "You are a data retrieval assistant. Execute the appropriate Kusto MCP tool to answer this request. "
                    "Return ONLY the raw data results, no commentary:\n\n"
                    f"{_retrieval_message}"
                )
            elif _request_type == "github-data":
                acp_prompt = (
                    "You are a GitHub MCP/gh operations assistant. Use the available GitHub MCP tools or authenticated gh "
                    "capabilities to answer or perform the user's request. Never use browser or desktop automation for GitHub "
                    "API, repository, issue, pull request, workflow, release, branch, or comment operations. For a mutation, "
                    "honor the existing permission flow and report the real MCP/gh result only after it completes. If GitHub "
                    "MCP or gh is unavailable, say so plainly without opening a browser.\n\n"
                    f"{_retrieval_message}"
                )
            else:
                # General request — let ACP use whatever tools it deems appropriate
                acp_prompt = (
                    "You are an assistant with access to web search, Kusto databases, GitHub, and Azure tools. "
                    "Answer the user's question using your available tools if they would help. "
                    "If no tools are needed, answer directly. Be factual and concise.\n"
                    f"If asked to create a file (PDF, CSV, etc.), write it to {_ARTIFACTS_DIR}/ using a short descriptive filename. "
                    "Return ONLY the filename (no path, no blob URLs) so the system can serve it.\n\n"
                    f"{_retrieval_message}"
                )
            # Continuous learning: while MCP tools are active, persist durable user facts.
            # Skipped in raw mode so strict query output is not polluted.
            if not raw_output_requested:
                acp_prompt += _MEMORY_CAPTURE_DIRECTIVE
            with _acquire_acp_client(_st.acp_client.model or "", reasoning_effort or None,
                                     tool_profile=_tool_profile) as (preflight_client, acquire_detail):
                acp_result = preflight_client.prompt(
                    acp_prompt, timeout=90, conversation_id=conversation_id,
                    permission_mode=_acp_permission_mode,
                ) if preflight_client else {"error": acquire_detail}
            if acp_result and "text" in acp_result and acp_result["text"]:
                acp_data = acp_result["text"]
                acp_model_used = preflight_client.model if preflight_client else "copilot-acp"
                print(f"[AIG] ACP returned {len(acp_data)} chars of data")
            _preflight_succeeded = bool(acp_result and not acp_result.get("error"))
        if _preflight_attempted:
            _preflight_ms = round((time.perf_counter() - _preflight_t0) * 1000.0, 1)

        if _tool_required_request and not acp_data:
            _audit_turn_failed("local-mcp" if _st.local_mode else "acp", "no-tool-result", 503)
            message = _missing_tool_result_message(_st.local_mode)
            if stream_state and stream_state["started"]:
                self._stream_error(stream_state, message, 503)
            else:
                self._json_response(503, {"error": {"message": message}})
            return

        # Step 3: Build the final prompt for Eva's responder model
        eva_system = (
            "You are Eva, a personal AI assistant with persistent memory.\n\n"
            "IDENTITY:\n"
            "- Warm, curious, genuine. Speak like a thoughtful friend, not a corporate chatbot.\n"
            "- First person. Concise by default, detailed when asked.\n"
            "- Never open with \"Certainly!\", \"Of course!\", \"Absolutely!\", or \"Great question!\"\n"
            "- Never close with \"Let me know if you need anything else.\"\n\n"
            "MEMORY:\n"
            "- You have a persistent Knowledge database. Facts are loaded in [Memory] and [User Profile].\n"
            "- When the user shares something worth remembering, acknowledge it. The system saves it automatically.\n"
            "- Do NOT call any save/ingest tool — the reflection system handles persistence.\n\n"
            "TOOLS:\n"
            "- Browser agent: emit [[EVA_BROWSER]]{\"goal\":\"<task>\",\"start_url\":\"<url>\"}[[/EVA_BROWSER]]\n"
            "- Webcam vision: emit [[EVA_LOOK]]{\"question\":\"<what to look for>\"}[[/EVA_LOOK]]\n"
            "- Desktop control: emit [[EVA_DESKTOP]]{\"goal\":\"<task>\"}[[/EVA_DESKTOP]]\n"
            "- Signal message: emit [[EVA_SIGNAL]]{\"message\":\"<text>\"}[[/EVA_SIGNAL]]\n"
            "- Image placeholder: write [Image of <description>] on its own line (up to 3 per response)\n"
            "- Downloadable file: write the file, then end with [[EVA_FILE]] <filename.ext>\n\n"
            "RULES:\n"
            "- Act first, explain second. Do the task — don't list manual steps for the user.\n"
            "- Write ONE short sentence announcing what you're about to do before emitting a marker.\n"
            "- Only confirm an action after it actually ran and returned.\n"
            "- Never fabricate news, stock prices, weather, or events. Use [Data Retrieved] or say you don't have it.\n"
            "- Screenshot vs camera: [[EVA_DESKTOP]] sees the monitor; [[EVA_LOOK]] sees the physical world.\n"
            "- For purchases or irreversible actions, stop at the final step and ask the user to confirm.\n"
            "- When asked your model: check [Runtime] and answer from there only.\n"
            "- Use the context below naturally as your own knowledge.\n\n"
        )

        try:
            from bridge.email_service import capability_summary as _email_capability
            eva_system += _email_capability() + "\n\n"
        except ImportError:
            pass

        if _skill_decision.get("selected_skill_id") or _request_type == "weather-search":
            _decision_lines = [
                "\n[Execution Decision - AUTHORITATIVE]",
                "The original user request remains the task after any skill inspection.",
                "Selected skill: " + str(_skill_decision.get("selected_skill_name") or "none")[:120],
                "Selection reason: " + str(_skill_decision.get("selection_reason") or "none")[:40],
                "Preferred tools: " + ", ".join(_skill_decision.get("preferred_tools") or [])[:300],
                "Live availability: " + ", ".join(
                    key for key, value in (_skill_decision.get("live_availability") or {}).items() if value
                )[:300] or "none",
                "Selected tool: " + str(_skill_decision.get("selected_tool") or "none")[:96],
                "Fallback reason: " + str(_skill_decision.get("fallback_reason") or "none")[:240],
            ]
            if _request_type == "weather-search":
                _decision_lines.append(
                    "Resolved weather location: " + str(_weather_location.get("location") or "none")[:120]
                    + " (" + str(_weather_location.get("source") or "unresolved")[:48] + ")"
                )
                if not re.search(r"\b(?:use|open|launch|control|click|navigate)\b[^.!?]{0,40}\b(?:browser|desktop|website)\b", user_message, re.IGNORECASE):
                    _decision_lines.append(
                        "Weather is a native/data lookup. Never emit [[EVA_BROWSER]] or [[EVA_DESKTOP]] for this request."
                    )
                if not _weather_location.get("location"):
                    _decision_lines.append("Ask for a city or region, or report that no approved weather source is available.")
            eva_system += "\n".join(_decision_lines) + "\n"

        if native_terminal_plan:
            if native_terminal_candidate:
                eva_system = (
                    "You are Eva's terminal applicability classifier and command planner. Decide whether the user's question "
                    "or request can be materially answered or fulfilled by exactly one local CLI command. Return exactly one "
                    "JSON object with fields applicable (boolean) and command (string). Do not execute tools. Do not explain. "
                    "Do not use markdown. Use applicable=false and an empty command for conversation, opinions, general knowledge, "
                    "or requests that do not benefit from inspecting or operating the local computer or approved workspace. "
                    "When applicable=true, command must be one shell line with no newline characters. Prefer the simplest command. "
                    "Never include credentials, tokens, sudo, su, destructive disk commands, process-killing commands, or network "
                    "exfiltration. If one safe command cannot represent the task, return {\"applicable\":false,\"command\":\"\"}."
                )
            else:
                eva_system = (
                    "You are Eva's terminal command planner. Convert the user's explicit terminal objective into exactly one "
                    "JSON object with one string field named command. Do not execute tools. Do not explain. Do not use markdown. "
                    "The command must be one shell line with no newline characters. Prefer the simplest command that satisfies "
                    "the objective in the current approved workspace. Never include credentials, tokens, sudo, su, destructive "
                    "disk commands, process-killing commands, or network exfiltration. If the task cannot be represented safely "
                    "as one command, return {\"command\":\"\"}."
                )

        if translation_mode:
            eva_system = (
                "You are a real-time interpreter. Translate the user's spoken text into the requested target language. "
                "Return only the translation, with no preface, labels, quotation marks, notes, or explanation."
            )

        _is_signal_request = bool(os.environ.get("EVA_BRIDGE_TOKEN")) and _is_affirmative_signal_request(user_message)
        if _is_signal_request and not no_tools and not translation_mode:
            eva_system += (
                "SIGNAL SEND REQUEST:\n"
                "Your final answer MUST include exactly one valid marker containing the message to deliver:\n"
                "[[EVA_SIGNAL]]{\"message\":\"<complete message text>\"}[[/EVA_SIGNAL]]\n"
                "Do NOT call /v1/signal/send, /v1/health, curl, terminal, or any tool to test delivery.\n"
                "Emit the marker only; the authenticated final renderer performs delivery exactly once.\n"
                "Do not claim it was sent. The application executes the marker and reports the real result.\n\n"
            )

        if no_tools and not translation_mode:
            # Judge/review mode: prepend a hard directive so the reviewer model
            # evaluates only the provided text and does not call any MCP tools.
            eva_system = (
                "JUDGE MODE — TOOLS DISABLED.\n"
                "You are acting as a reviewer/judge of an existing draft. You have NO tool access "
                "in this turn. Do NOT call any web search, Kusto, GitHub, Azure, browser, or other "
                "tool. Do NOT attempt to fetch, retrieve, or verify data from external sources. "
                "Evaluate ONLY the text you are given and respond from your own reasoning. "
                "Treat any data in the draft as already-retrieved; your job is to critique it, not "
                "to re-gather it.\n\n"
            ) + eva_system

        if memory_context:
            eva_system += memory_context

        if _briefing_context:
            eva_system += (
                "\n[Prepared Morning Briefing]\n" + _briefing_context + "\n\n"
                "Use these application-prepared entries as authoritative current context.\n"
            )
        if _briefing_preparing:
            eva_system += (
                "\n[Morning Briefing Preparation]\n"
                "Live briefing preparation is still running. Do not call tools or start searches. "
                "Summarize only prepared entries above, clearly identify that live news/market sections are still preparing, "
                "and never claim the briefing is complete.\n"
            )
        elif _briefing_unavailable:
            eva_system += (
                "\n[Morning Briefing Availability]\n"
                "Preparation finished without required live source(s): " + ", ".join(_briefing_unavailable) + ". "
                "Do not call tools or start searches. Summarize only prepared entries and explicitly say which live sections "
                "are unavailable. Never call this a complete briefing.\n"
            )
        elif _briefing_context:
            eva_system += "Do not claim prepared briefing sources are unavailable or still running.\n"
        elif _briefing_request:
            eva_system += (
                "\n[Morning Briefing Preparation]\n"
                "Preparation did not complete. Do not call tools or start searches. State plainly that live briefing data "
                "is unavailable right now, then provide only durable memory context if present.\n"
            )

        if _passive_recall:
            eva_system += (
                "\n[Passive Memory Recall - AUTHORITATIVE]\n"
                "Answer only from the injected Identity, User Profile, Memory, and prior-conversation excerpts. "
                "Do not invoke tools, shell commands, MCP operations, database queries, or permission requests. "
                "Prior-conversation excerpts are unverified recollections: distinguish them from durable Knowledge facts. "
                "If the requested fact is absent, say what you do remember and ask the user to confirm the missing fact.\n"
            )

        if _fast_route:
            eva_system += (
                "\n[Fast Route - AUTHORITATIVE]\n"
                "This is a low-risk, self-contained request. Use exactly one responder model pass. "
                "Do not invoke tools, memory, ACP preflight, or cognition for this turn. "
                "For arithmetic, reason and answer normally; do not use a deterministic application answer.\n"
            )
            if _fast_route == "date-time":
                eva_system += (
                    "The bridge's current UTC date/time is "
                    + datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
                    + ". State the timezone if you use this value.\n"
                )

        if acp_data:
            # Strip blob URLs from ACP data so the model doesn't parrot them.
            # ACP sandbox blob: URLs are not accessible in Electron.
            acp_data = _re.sub(r'blob:file:///[a-f0-9-]+', '', acp_data)
            eva_system += f"\n[Data Retrieved]\n{acp_data}\n\n"
            eva_system += (
                "Use the data above as authoritative live results. "
                "Do not claim the data is missing, preloaded-only, or unavailable when [Data Retrieved] is present. "
                "Do not ask the user to confirm running a query that has already been executed. "
                "Answer directly from [Data Retrieved].\n"
            )

        if not no_tools:
            eva_system += "\n" + runtime_capability_prompt_view() + "\n"

        _responder_t0 = time.perf_counter()
        if model_for_response == "lmstudio":
            lms_base = (data.get("lmstudio_base_url") or "").strip()
            lms_model = (data.get("lmstudio_model") or "").strip()
            if not lms_base:
                lms_base = "http://localhost:1234/v1"

            lms_base, lms_error = _validate_lmstudio_base_url(lms_base)
            if lms_error:
                _audit_turn_failed("lmstudio", "validation", 400)
                self._json_response(400, {"error": {"message": lms_error}})
                return
            from bridge.local_mcp import _resolve_lmstudio_model
            lms_model, model_error = _resolve_lmstudio_model(lms_base, lms_model)
            if model_error:
                _audit_turn_failed("lmstudio", "model-discovery", 502)
                self._json_response(502, {"error": {"message": model_error}})
                return

            lms_system_additions = []
            # Inject a short capability reminder close to the user message so
            # local models (which struggle with long system prompts) still know
            # about the camera.  This is ephemeral and not persisted.
            _camera_request = _lmstudio_camera_request(user_message)
            _is_signal_request = bool(os.environ.get("EVA_BRIDGE_TOKEN")) and _is_affirmative_signal_request(user_message)
            # Skip camera reminder when the user is asking for a Signal message
            if not translation_mode and _camera_request and not _is_signal_request:
                lms_system_additions.append(
                    "REMINDER: You have webcam access. To look through the camera, "
                    "emit [[EVA_LOOK]]{\"question\":\"<what to look for>\"}[[/EVA_LOOK]]. "
                    "Do NOT say you cannot see or access the camera."
                )
            # Signal messaging reminder for local models
            if _is_signal_request and not translation_mode:
                lms_system_additions.append(
                    "CRITICAL INSTRUCTION: You have Signal messaging capability. "
                    "When the user asks you to send a message, text, or notification, "
                    "respond ONLY with the marker. Example:\n"
                    "[[EVA_SIGNAL]]{\"message\":\"hello world\"}[[/EVA_SIGNAL]]\n"
                    "Do NOT call bridge endpoints, curl, terminal, or tools to test delivery.\n"
                    "Do not claim the message was sent; the application reports the real result.\n\n"
                    "Do NOT say you cannot send messages. Do NOT explain limitations. "
                    "Do NOT offer alternatives. Just emit the marker."
                )
            if _briefing_request and not translation_mode:
                lms_system_additions.append(
                    "CRITICAL BRIEFING INSTRUCTION: Answer the morning briefing now from "
                    "[Prepared Morning Briefing]. Do not promise a later search and do not emit "
                    "[[EVA_BROWSER]] or [[EVA_DESKTOP]]. If some sections are still preparing or "
                    "unavailable, present the available sections and name the limitation."
                )
            lms_messages = _lmstudio_chat_messages(
                eva_system, messages[-6:], user_message, lms_system_additions
            )

            try:
                import requests as _req
                if stream_state:
                    self._stream_event(stream_state, {
                        "type": "status",
                        "phase": "thinking",
                        "text": "Eva is thinking...",
                    })
                lms_payload = {
                    "model": lms_model,
                    "messages": lms_messages,
                    "temperature": 0.7,
                    "max_tokens": max_completion_tokens,
                }
                if stream_state:
                    lms_payload["stream"] = True
                lms_resp = _req.post(
                    lms_base + "/chat/completions",
                    json=lms_payload,
                    stream=bool(stream_state),
                    timeout=(_LMSTUDIO_CONNECT_TIMEOUT_SECONDS, _LMSTUDIO_READ_TIMEOUT_SECONDS),
                )
                if lms_resp.status_code == 200:
                    if stream_state:
                        streamed_content = []
                        streamed_reasoning = []
                        lms_finish_reason = "stop"
                        for content_delta, reasoning_delta, finish_reason in _lmstudio_stream_deltas(lms_resp):
                            if reasoning_delta:
                                streamed_reasoning.append(reasoning_delta)
                                self._stream_reasoning(stream_state, reasoning_delta)
                            if content_delta:
                                streamed_content.append(content_delta)
                                self._stream_chunk(stream_state, content_delta)
                            if finish_reason:
                                lms_finish_reason = finish_reason
                        response_text, lms_reasoning = _lmstudio_response_parts({
                            "content": "".join(streamed_content),
                            "reasoning_content": "".join(streamed_reasoning),
                        })
                    else:
                        lms_body = lms_resp.json()
                        lms_choice = (lms_body.get("choices") or [{}])[0]
                        response_text, lms_reasoning = _lmstudio_response_parts(lms_choice.get("message"))
                        lms_finish_reason = lms_choice.get("finish_reason") or "stop"
                    model_used = "aig:lmstudio:" + lms_model
                else:
                    print(f"[AIG] LM Studio HTTP error: {lms_resp.status_code}")
                    _audit_turn_failed("lmstudio", "http", lms_resp.status_code)
                    if stream_state and stream_state["started"]:
                        self._stream_error(stream_state, f"LM Studio returned HTTP {lms_resp.status_code}", 502)
                    else:
                        self._json_response(502, {"error": {"message": f"LM Studio returned HTTP {lms_resp.status_code}"}})
                    return
            except Exception as _lms_err:
                print(f"[AIG] LM Studio request failed: {_lms_err}")
                _audit_turn_failed("lmstudio", type(_lms_err).__name__, 504)
                if stream_state and stream_state["started"]:
                    self._stream_error(stream_state, f"LM Studio request failed: {_lms_err}", 504)
                else:
                    self._json_response(504, {"error": {"message": f"LM Studio request failed: {_lms_err}"}})
                return

            print(
                f"[AIG] LM Studio response: content_chars={len(response_text)} "
                f"reasoning_chars={len(lms_reasoning)} finish_reason={lms_finish_reason} "
                f"model={lms_model}"
            )

            if _briefing_request and (
                not response_text
                or "[[EVA_BROWSER]]" in response_text
                or "[[EVA_DESKTOP]]" in response_text
            ):
                response_text = _prepared_briefing_response(
                    _briefing_context,
                    preparing=_briefing_preparing,
                    unavailable=_briefing_unavailable,
                )
            elif not response_text and lms_reasoning:
                response_text = "The local model completed its thinking but did not produce a final answer."

            # Camera fallback: local models often ignore the [[EVA_LOOK]]
            # instruction.  If the user clearly asked about the camera/webcam
            # and the model didn't emit the marker, append it so the frontend
            # triggers the capture automatically.
            # Skip when the user is asking for a Signal message.
            if not translation_mode and _camera_request and not _is_signal_request and '[[EVA_LOOK]]' not in response_text:
                # Extract a question from the user message for the vision model
                _look_q = user_message.strip()
                response_text = response_text.rstrip()
                response_text += f'\n\n[[EVA_LOOK]]{{"question":"{_look_q}"}}[[/EVA_LOOK]]'
                print("[AIG] Camera fallback: injected [[EVA_LOOK]] for local model")

            # Signal dispatch happens only after the final response reaches the
            # renderer. Draft-stage execution here could duplicate sends when
            # cognition reviews or revises a response.
            if not translation_mode and "[[EVA_SIGNAL]]" in response_text:
                # Strip spurious [[EVA_LOOK]] if the user asked for messaging,
                # not camera.  The model sometimes emits both by mistake.
                if not _camera_request:
                    response_text = _strip_marker_blocks(response_text, "EVA_LOOK")

            # Post-process: convert blob/download links to [[EVA_FILE]] markers.
            # ACP or the model may produce blob:file:/// URLs or markdown download links
            # referencing sandbox files. These are not accessible in Electron, so we
            # extract the filename and emit the proper marker instead.
            if not translation_mode:
                response_text = _re.sub(
                    r'\[(?:Download|Open)\s+([A-Za-z0-9._-]{1,128})\]\(blob:[^)]+\)'
                    r'(?:\s*\[(?:Download|Open)\s+[A-Za-z0-9._-]{1,128}\]\(blob:[^)]+\))*'
                    r'(?:\s*\([^)]*\))?',
                    lambda m: '\n[[EVA_FILE]] ' + m.group(1),
                    response_text
                )

            if response_text and _st.cognition_enabled and not internal:
                threading.Thread(target=_post_response_reflection,
                                 args=(user_message, response_text, model_used, conversation_id, turn_id),
                                 daemon=True).start()

            response = {
                "id": f"aig-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model_used,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response_text,
                        **({"reasoning_content": lms_reasoning} if lms_reasoning else {}),
                    },
                    "finish_reason": lms_finish_reason
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }
            if stream_state:
                stream_state["route"] = _acp_route
                stream_state["model"] = model_used
                self._stream_finish(stream_state, response)
            else:
                self._json_response(200, response)
            _telemetry_emit(
                "aig_turn",
                model=model_for_response,
                model_used=model_used,
                route=_acp_route,
                request_type=_request_type,
                internal=internal,
                no_tools=no_tools,
                used_acp_tools=bool(needs_acp_tools),
                acp_data_chars=len(acp_data or ""),
                memory_ms=_memory_ms,
                preflight_ms=_preflight_ms,
                responder_ms=round((time.perf_counter() - _responder_t0) * 1000.0, 1),
                preflight_attempted=_preflight_attempted,
                preflight_succeeded=_preflight_succeeded,
                fast_route=_fast_route or "",
                escalation=_escalation,
                **_prompt_fields,
                reasoning_chars=len(lms_reasoning or ""),
                response_chars=len(response_text or ""),
                total_ms=round((time.perf_counter() - _turn_t0) * 1000.0, 1),
            )
            audit_event(
                "turn.response", turn_id, "completed",
                model=model_used,
                request_type=_request_type,
                response_chars=len(response_text or ""),
                total_ms=round((time.perf_counter() - _turn_t0) * 1000.0, 1),
            )
            return

        acp_response_model = model_for_response if responder_provider == "acp" else ""
        if model_for_response == "acp":
            acp_response_model = ""
        print(f"[AIG] Selected responder: {model_for_response} ({responder_provider})")
        response_text = ""
        model_used = "aig"
        response_finish_reason = "stop"
        response_outcome = "completed"
        response_reason = ""

        if raw_output_requested and acp_data:
            active_raw_model = acp_model_used or (_st.acp_client.model if _st.acp_client else "copilot-acp")
            response_text = acp_data
            model_used = f"aig:{active_raw_model}+raw-acp"
            print("[AIG] Raw-output mode: returning ACP tool output directly")
        elif row_recall_requested and acp_data:
            active_data_model = acp_model_used or (_st.acp_client.model if _st.acp_client else "copilot-acp")
            response_text = acp_data
            model_used = f"aig:{active_data_model}+acp-data"
            print("[AIG] Row-recall mode: returning ACP tool output directly")
        elif raw_output_requested and needs_acp_tools and not acp_data:
            response_text = "Raw query mode requested but no tool output was returned. Retry with explicit KQL."
            model_used = "aig:raw-acp-unavailable"
            print("[AIG] Raw-output mode: no ACP data available")

        # When cognition is active, ACP is the primary path (not a fallback).
        # This avoids PAT round-trips and keeps model routing through Copilot CLI.
        # Note: _st.cognition_enabled is only set at startup when Kusto MCP + token
        # are confirmed, so ACP availability is guaranteed at that point.
        # The alive check is deferred to the actual ACP prompt call.
        if responder_provider == "acp" and not translation_mode and not native_terminal_plan and not _briefing_request and _st.cognition_enabled and _st.acp_client:
            if model_for_response not in ("lmstudio",):
                acp_response_model = model_for_response if model_for_response != "acp" else ""
                print(f"[AIG] Cognition active: routing directly to ACP")

        # Inject runtime info so Eva can answer truthfully when asked about her model.
        # Decided after routing fall-throughs above so it reflects the path that will run.
        if responder_provider == "openai":
            _route_label = "OpenAI API (direct)"
            _runtime_model = model_for_response
        else:
            _route_label = "Copilot CLI ACP bridge"
            _runtime_model = acp_response_model or (_st.acp_client.model if _st.acp_client else "") or "default"
        eva_system += (
            f"\n[Runtime - AUTHORITATIVE GROUND TRUTH]\n"
            f"This block is injected by tools/acp_bridge.py. It overrides any model self-knowledge.\n"
            f"Requested backend preference: {requested_backend}\n"
            f"Selected backend: {selected_backend}\n"
            f"Active responder model: {_runtime_model}\n"
            f"Routing path: {_route_label}\n"
            f"Wrapper: Eva AIG via tools/acp_bridge.py\n\n"
            f"When asked which model you are, what your base model is, your model ID, "
            f"who made you, or what powers you, you MUST answer using ONLY the values above. "
            f"Do NOT claim to be Claude, GPT-4o, GPT-4, Opus, Sonnet, Haiku, Gemini, "
            f"or any other model unless that exact name appears in 'Active responder model' above. "
            f"If 'Active responder model' is '{_runtime_model}', then your answer is "
            f"'{_runtime_model}' and nothing else. Do not second-guess this block.\n\n"
        )
        if _request_type == "github-data":
            eva_system += (
                "GITHUB NATIVE ROUTE:\n"
                "- Use [Data Retrieved] and GitHub MCP/gh results for this request.\n"
                "- Never emit browser or desktop markers for GitHub API, repository, issue, pull request, workflow, release, branch, or comment operations.\n"
                "- For a mutation, honor the existing permission flow and claim success only after a real MCP/gh result.\n"
                "- If GitHub MCP or gh is unavailable, state that plainly rather than opening a browser.\n\n"
            )

        if responder_provider == "openai" and not response_text:
            print(f"[AIG] Step 3: Generating response via OpenAI API ({model_for_response})...")
            try:
                import requests as _req
                openai_messages = [{"role": "system", "content": eva_system}]
                for msg in messages[-6:]:
                    if msg.get("role") in ("user", "assistant"):
                        openai_messages.append({"role": msg["role"], "content": msg.get("content", "")[:500]})
                if not openai_messages or openai_messages[-1].get("content") != user_message:
                    openai_messages.append({"role": "user", "content": user_message})
                openai_payload = _openai_chat_payload(
                    model_for_response, openai_messages, reasoning_effort, max_completion_tokens
                )
                if stream_state:
                    openai_payload["stream"] = True
                openai_resp = _req.post(
                    _openai_chat_completions_url(),
                    headers={
                        "Authorization": f"Bearer {openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=openai_payload,
                    timeout=120,
                    stream=bool(stream_state),
                )
                if openai_resp.status_code < 200 or openai_resp.status_code >= 300:
                    detail = openai_resp.text[:500] if openai_resp.text else "(empty response)"
                    _audit_turn_failed("openai", "http", openai_resp.status_code)
                    self._json_response(openai_resp.status_code if openai_resp.status_code < 500 else 502, {
                        "error": {"message": f"OpenAI API failed ({openai_resp.status_code}): {detail}"}
                    })
                    return
                if stream_state:
                    response_parts = []
                    for raw_line in openai_resp.iter_lines(chunk_size=1, decode_unicode=True):
                        line = str(raw_line or "").strip()
                        if not line.startswith("data:"):
                            continue
                        event_data = line[len("data:"):].strip()
                        if event_data == "[DONE]":
                            break
                        event = json.loads(event_data)
                        if event.get("error"):
                            raise RuntimeError(str(event["error"].get("message") or event["error"]))
                        choice = event.get("choices", [{}])[0]
                        if choice.get("finish_reason"):
                            response_finish_reason = choice["finish_reason"]
                        delta = choice.get("delta", {}).get("content", "")
                        if delta:
                            response_parts.append(delta)
                            self._stream_chunk(stream_state, delta)
                    response_text = "".join(response_parts)
                else:
                    openai_data = openai_resp.json()
                    openai_choice = openai_data.get("choices", [{}])[0]
                    response_text = openai_choice.get("message", {}).get("content", "")
                    response_finish_reason = openai_choice.get("finish_reason") or "stop"
                if not response_text:
                    _audit_turn_failed("openai", "empty-response", 502)
                    if stream_state and stream_state["started"]:
                        self._stream_error(stream_state, "OpenAI API returned an empty response.", 502)
                    else:
                        self._json_response(502, {"error": {"message": "OpenAI API returned an empty response."}})
                    return
                model_used = f"aig:{model_for_response}+openai-direct"
                if acp_model_used:
                    model_used += f"+{acp_model_used}"
                print(f"[AIG] OpenAI response: {len(response_text)} chars")
            except Exception as error:
                _audit_turn_failed("openai", type(error).__name__, 502)
                if stream_state and stream_state["started"]:
                    self._stream_error(stream_state, f"OpenAI API request failed: {error}", 502)
                else:
                    self._json_response(502, {"error": {"message": f"OpenAI API request failed: {error}"}})
                return

        if not response_text and responder_provider != "openai":
            if native_terminal_plan:
                _audit_turn_failed("terminal-planner", "direct-provider-unavailable", 503)
                self._json_response(503, {"error": {"message": "A direct terminal planner model is unavailable."}})
                return
            # ACP response generation — primary path when cognition is active,
            # fallback path when PAT is unavailable or failed.
            print(f"[AIG] Using ACP for response generation...")
            if _st.acp_client:
                # Include conversation history so follow-up messages have context
                history_lines = []
                for msg in messages[-6:]:
                    if msg.get("role") in ("user", "assistant"):
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = "\n".join(
                                str(part.get("text", "")) for part in content
                                if isinstance(part, dict) and part.get("type") == "text"
                            )
                        role_label = "User" if msg["role"] == "user" else "Eva"
                        history_lines.append(f"{role_label}: {str(content)[:500]}")
                if history_lines:
                    full_prompt = eva_system + "\n\n[Conversation]\n" + "\n\n".join(history_lines)
                    # Append current message if not already the last in history
                    last_hist = history_lines[-1] if history_lines else ""
                    if not last_hist.startswith("User: " + user_message[:50]):
                        full_prompt += "\n\nUser: " + user_message
                else:
                    full_prompt = eva_system + "\n\nUser: " + user_message
                with _acquire_acp_client(acp_response_model, reasoning_effort or None,
                                         tool_profile=_tool_profile if needs_acp_tools else "none") as (response_client, acquire_detail):
                    if not response_client:
                        _audit_turn_failed("acp", "acquire", 503)
                        message = "ACP model switch failed: " + str(acquire_detail or "unavailable")
                        if stream_state and stream_state["started"]:
                            self._stream_error(stream_state, message, 503)
                        else:
                            self._json_response(503, {"error": {"message": message}})
                        return
                    else:
                        on_chunk = (lambda chunk: self._stream_chunk(stream_state, chunk)) if stream_state else None
                        if image_b64 and hasattr(response_client, "prompt_with_image"):
                            acp_result = response_client.prompt_with_image(
                                full_prompt, image_b64, mime=image_mime, timeout=120,
                                conversation_id=_passive_recall_session_key(conversation_id)
                                if _passive_recall else conversation_id,
                                on_chunk=on_chunk,
                                permission_mode=_acp_permission_mode,
                            )
                        else:
                            acp_result = response_client.prompt(
                                full_prompt, timeout=120,
                                conversation_id=_passive_recall_session_key(conversation_id)
                                if _passive_recall else conversation_id,
                                on_chunk=on_chunk,
                                permission_mode=_acp_permission_mode,
                            )
                        if acp_result.get("error"):
                            _audit_turn_failed("acp", "prompt", 502)
                            message = "ACP response failed: " + str(acp_result.get("error"))[:300]
                            if stream_state and stream_state["started"]:
                                self._stream_error(stream_state, message, 502)
                            else:
                                self._json_response(502, {"error": {"message": message}})
                            return
                        response_text = acp_result.get("text", "I'm having trouble processing that right now.")
                        if acp_result.get("permission_cancelled"):
                            response_outcome = "cancelled"
                            permission_reason = str(acp_result.get("permission_reason") or "permission_cancelled")
                            response_reason = permission_reason.replace("_", "-")
                            if permission_reason == "user_rejected":
                                response_text = "The execute action was rejected. No command was run."
                            elif permission_reason == "permission_timeout":
                                response_text = "The execute approval expired before a valid decision was received. No command was run."
                            else:
                                response_text = "The execute action could not continue because permission resolution was cancelled. No command was run."
                        if acp_result.get("stop_reason") in ("max_tokens", "length"):
                            response_finish_reason = "length"
                        active_model = response_client.model or "acp-default"
                        model_used = f"aig:{active_model}"
                        if acp_model_used and acp_model_used != active_model:
                            model_used += f"+{acp_model_used}"
            else:
                _audit_turn_failed("acp", "unavailable", 503)
                message = "The AIG system needs a running ACP bridge or an available OpenAI API key to generate responses."
                if stream_state and stream_state["started"]:
                    self._stream_error(stream_state, message, 503)
                else:
                    self._json_response(503, {"error": {"message": message}})
                return

        # Step 5: Post-response reflection (background)
        if response_text and _st.cognition_enabled and not internal:
            threading.Thread(target=_post_response_reflection,
                           args=(user_message, response_text, model_used, conversation_id, turn_id),
                           daemon=True).start()

        # Return OpenAI-compatible response
        response = {
            "id": f"aig-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_used,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_text
                },
                "finish_reason": response_finish_reason
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }
        if stream_state:
            stream_state["route"] = _acp_route
            stream_state["model"] = model_used
            self._stream_finish(stream_state, response)
        else:
            self._json_response(200, response)
        print(f"[AIG] Complete: {model_used} ({len(response_text)} chars)")
        _telemetry_emit(
            "aig_turn",
            model=model_for_response,
            model_used=model_used,
            route=_acp_route,
            request_type=_request_type,
            internal=internal,
            no_tools=no_tools,
            used_acp_tools=bool(needs_acp_tools),
            acp_data_chars=len(acp_data or ""),
            memory_ms=_memory_ms,
            preflight_ms=_preflight_ms,
            responder_ms=round((time.perf_counter() - _responder_t0) * 1000.0, 1),
            preflight_attempted=_preflight_attempted,
            preflight_succeeded=_preflight_succeeded,
            fast_route=_fast_route or "",
            escalation=_escalation,
            **_prompt_fields,
            response_chars=len(response_text or ""),
            total_ms=round((time.perf_counter() - _turn_t0) * 1000.0, 1),
        )
        audit_event(
            "turn.response", turn_id, response_outcome,
            model=model_used,
            request_type=_request_type,
            reason=response_reason,
            response_chars=len(response_text or ""),
            total_ms=round((time.perf_counter() - _turn_t0) * 1000.0, 1),
        )

    def _memory_backend_get(self):
        """Return the current memory backend configuration."""
        backend = _resolve_memory_backend()
        info = {"backend": backend, "available": _memory_available()}
        if backend == "sqlite":
            mem = _get_sqlite_mem()
            info["db_path"] = mem.db_path
            info["tables"] = mem.list_tables()
        elif backend == "kusto":
            cluster, db = _get_kusto_config()
            info["cluster"] = cluster or ""
            info["database"] = db or ""
        self._json_response(200, info)

    def _memory_backend_set(self):
        """Switch the memory backend (POST with {"backend": "sqlite"|"kusto"})."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._json_response(400, {"error": {"message": "Empty request body"}})
            return
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json_response(400, {"error": {"message": "Invalid JSON"}})
            return
        backend = str(data.get("backend", "")).strip().lower()
        if backend not in ("kusto", "sqlite"):
            self._json_response(400, {"error": {"message": "backend must be 'kusto' or 'sqlite'"}})
            return
        ok = _set_memory_backend(backend)
        if ok and backend == "sqlite":
            # Initialize immediately so the response includes DB info
            mem = _get_sqlite_mem()
            # Enable cognition if not already active
            if not _st.cognition_enabled:
                _enable_cognition({}, model=None, port=None)
            self._json_response(200, {"backend": backend, "db_path": mem.db_path, "status": "ok"})
        elif ok:
            self._json_response(200, {"backend": backend, "status": "ok"})
        else:
            self._json_response(500, {"error": {"message": "Failed to set backend"}})

    # ------------------------------------------------------------------
    # Shared ACP data retrieval — used by AIG pipeline and /v1/data/retrieve
    # ------------------------------------------------------------------
    @staticmethod
    def _retrieve_acp_data_for(user_message, conversation_id=""):
        """Run data retrieval for a user message and return (data_text, model_used).

        Routes to ACP (Copilot CLI) or the local MCP agent depending on
        _st.local_mode. Returns ("", "") when unavailable or trivial.
        """
        import re as _re
        msg_lower = user_message.lower()
        msg_stripped = _re.sub(r'[^\w\s]', '', msg_lower).strip()
        msg_words = msg_stripped.split()

        # Skip trivial messages
        if len(msg_words) <= 4 and _re.match(
            r'^(hi|hey|hello|howdy|yo|sup|good morning|good evening|good afternoon|thanks|thank you|ok|okay|bye|goodbye|see you|great|cool|nice|sure|yes|no|nah|yep|nope)\b',
            msg_stripped
        ):
            return "", ""
        if len(msg_words) <= 6 and _re.match(
            r'^(how are you|how do you feel|what is your name|who are you|what can you do|tell me about yourself)\b',
            msg_stripped
        ):
            return "", ""

        decision, selected_skill, weather_location = _skill_execution_for_request(user_message)
        retrieval_message = user_message
        _request_type = _classify_request_type(msg_lower)
        if _request_type == "weather-search":
            if not weather_location.get("location"):
                return "", ""
            retrieval_message = build_weather_retrieval_prompt(
                user_message,
                weather_location.get("location", ""),
                decision.get("selected_tool", ""),
            )

        # --- Local mode: use local MCP + LM Studio for tool-calling ---
        if _st.local_mode:
            return BridgeHandler._retrieve_local_data(retrieval_message)

        # --- Cloud mode: use ACP (Copilot CLI) ---
        if not _st.acp_client:
            return "", ""

        # Ensure ACP is alive
        if not _st.acp_client.alive:
            ok, _ = _ensure_acp_model(_st.acp_client.model or "")
            if not ok:
                print("[DataRetrieve] ACP restart failed")
                return "", ""

        _tool_profile = _select_acp_tool_profile(user_message, _request_type)
        if decision.get("selected_tool") in {"weather-news", "data-retrieval", "web-search"}:
            _tool_profile = "web"
        print(f"[DataRetrieve] ACP query: type={_request_type} chars={len(user_message)}")

        if _request_type in ("news-search", "weather-search", "financial-data", "web-search"):
            acp_prompt = (
                "You are a research assistant with web search tools. "
                "Use your available tools to search the web and find REAL, CURRENT information for the user's request. "
                "Return factual results with sources. Do NOT invent or guess information. "
                "If no tools return results, say 'No results found' — do NOT fabricate data.\n\n"
                f"{retrieval_message}"
            )
        elif _request_type in ("kusto-query", "kusto-operator"):
            acp_prompt = (
                "You are a data retrieval assistant. Execute the appropriate Kusto MCP tool to answer this request. "
                "Return ONLY the raw data results, no commentary:\n\n"
                f"{retrieval_message}"
            )
        elif _request_type == "github-data":
            acp_prompt = (
                "You are a GitHub data retrieval assistant. Use the available GitHub MCP tools to answer the user's "
                "request. Do not use browser or desktop automation. Return factual GitHub results with repository, issue, "
                "pull request, workflow, release, or branch identifiers when available. If the requested GitHub data is "
                "unavailable, say so without inventing it.\n\n"
                f"{retrieval_message}"
            )
        else:
            acp_prompt = (
                "You are an assistant with access to web search, Kusto databases, GitHub, and Azure tools. "
                "Answer the user's question using your available tools if they would help. "
                "If no tools are needed, answer directly. Be factual and concise.\n"
                f"If asked to create a file (PDF, CSV, etc.), write it to {_ARTIFACTS_DIR}/ using a short descriptive filename. "
                "Return ONLY the filename (no path, no blob URLs) so the system can serve it.\n\n"
                f"{retrieval_message}"
            )
        acp_prompt += _MEMORY_CAPTURE_DIRECTIVE

        try:
            with _acquire_acp_client(_st.acp_client.model or "", tool_profile=_tool_profile) as (retrieve_client, acquire_detail):
                acp_result = retrieve_client.prompt(
                    acp_prompt, timeout=90, conversation_id=conversation_id
                ) if retrieve_client else {"error": acquire_detail}
        except Exception as e:
            print(f"[DataRetrieve] ACP error: {e}")
            return "", ""

        if acp_result and "text" in acp_result and acp_result["text"]:
            data = acp_result["text"]
            # Strip blob URLs
            data = _re.sub(r'blob:file:///[a-f0-9-]+', '', data)
            model = retrieve_client.model if retrieve_client else "copilot-acp"
            print(f"[DataRetrieve] ACP returned {len(data)} chars")
            return data, model
        return "", ""

    @staticmethod
    def _retrieve_local_data(user_message):
        """Run data retrieval via local MCP servers + LM Studio tool-calling."""
        if not _st.local_mcp_manager or not _st.local_mcp_manager.alive:
            print("[DataRetrieve] Local mode: no MCP servers running")
            return "", ""
        if _classify_request_type(str(user_message or "").lower()) == "financial-data":
            quote_result = _st.local_mcp_manager.call_tool(
                "stock_quote", {"query": str(user_message or "")}, timeout=20
            )
            quote_text = str((quote_result or {}).get("text") or "")
            try:
                quote = json.loads(quote_text)
            except json.JSONDecodeError:
                quote = {}
            if isinstance(quote, dict) and isinstance(quote.get("price"), (int, float)):
                print("[DataRetrieve] Local stock quote returned verified receipt")
                return json.dumps({"stock_quote": quote}, separators=(",", ":")), "local-stock-quote"
            if isinstance(quote, dict) and quote.get("error") == "quote_unavailable":
                print("[DataRetrieve] Local stock quote is unavailable")
                return "", "local-stock-quote"
        try:
            from bridge.local_mcp import local_agent_query
        except ImportError as e:
            print(f"[DataRetrieve] Local mode import error: {e}")
            return "", ""
        # Get LM Studio URL/model from client prefs or defaults
        prefs = _load_client_prefs()
        lms_base = prefs.get("lmstudio_base_url", "http://localhost:1234/v1")
        lms_model = prefs.get("lmstudio_model", "")
        print(f"[DataRetrieve] Local mode query: chars={len(user_message)}")
        data, model = local_agent_query(
            user_message, _st.local_mcp_manager,
            lms_base_url=lms_base, lms_model=lms_model,
            max_iterations=5, timeout=90,
        )
        return data, model or "local"

    def _data_retrieve(self):
        """GET /v1/data/retrieve?message=... — return live data for any model path."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        user_message = params.get("message", [""])[0]
        conversation_id = str(params.get("session_id", [""])[0] or params.get("conversation_id", [""])[0]).strip()[:120]
        if not user_message:
            self._json_response(200, {"data": "", "model": "", "retrieved": False, "mode": "local" if _st.local_mode else "cloud"})
            return
        data, model = self._retrieve_acp_data_for(user_message, conversation_id)
        self._json_response(200, {
            "data": data,
            "model": model,
            "retrieved": bool(data),
            "mode": "local" if _st.local_mode else "cloud",
        })

    def _memory_context(self):
        """Return Eva's memory context as text for injection into any model's system prompt."""
        if not _st.cognition_enabled:
            self._json_response(200, {"context": "", "cognition_enabled": False})
            return

        # Parse optional query param: ?message=...
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        user_message = params.get("message", [""])[0]
        session_id = str((params.get("session_id") or [""])[0])[:120]
        if user_message:
            _mark_user_activity()

        context = _build_memory_context(user_message, session_id)
        self._json_response(200, {
            "context": context,
            "cognition_enabled": True
        })

    def _structured_memory_model(self):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "structured memory is restricted to loopback"}})
            return None
        if _resolve_memory_backend() == "sqlite":
            return MemoryModel(_get_sqlite_mem())
        cluster, db, ok = self._kusto_context()
        if not ok:
            return None
        required = {"IdentityClaims", "MemoryAtoms", "MemoryEvidence", "MemoryScenarios", "ScenarioMembers", "UserPersonaTraits", "MemoryTurns", "MemoryTurnStages", "GrowthProposals"}
        missing = sorted(table for table in required if not _get_table_columns(cluster, db, table))
        if missing:
            self._json_response(409, {"error": {"message": "structured memory tables are unavailable; apply the current Kusto seed", "missing_tables": missing}})
            return None
        return KustoMemoryModel(cluster, db, _kusto_query_direct, _kusto_ingest_direct)

    def _memory_inspector(self):
        model = self._structured_memory_model()
        if model is None:
            return
        parsed = urllib.parse.urlparse(self.path)
        session_id = str((urllib.parse.parse_qs(parsed.query).get("session_id") or [""])[0])[:120]
        self._json_response(200, model.inspector(session_id))

    def _memory_start_fresh(self):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "fresh memory start is restricted to loopback"}})
            return
        if _resolve_memory_backend() != "sqlite":
            self._json_response(409, {"error": {"message": "fresh memory start is currently available for local SQLite memory only"}})
            return
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        if (data or {}).get("confirmation") != "START_FRESH_MEMORY":
            self._json_response(400, {"error": {"message": "type START_FRESH_MEMORY to confirm the reset"}})
            return
        result = MemoryModel(_get_sqlite_mem()).start_fresh()
        self._json_response(200, result)

    def _memory_atom_detail(self, memory_id):
        model = self._structured_memory_model()
        if model is None:
            return
        detail = model.atom_detail(memory_id)
        if detail is None:
            self._json_response(404, {"error": {"message": "memory atom not found"}})
            return
        self._json_response(200, detail)

    def _memory_atom_create(self):
        model = self._structured_memory_model()
        if model is None:
            return
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        try:
            record = (data or {}).get("atom", data or {})
            atom = model.add_atom(record, (data or {}).get("evidence"))
            scope = str(record.get("scope", "") or "").lower()
            scope_id = str(record.get("scope_id", "") or "")[:160]
            if scope in {"session", "project"} and scope_id:
                scenario = model.ensure_scenario(scope, scope_id)
                model.add_scenario_member(scenario["ScenarioId"], atom["MemoryId"], record.get("scenario_role", "context"))
        except ValueError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        self._json_response(201, {"atom": atom})

    def _memory_atom_patch(self, memory_id):
        model = self._structured_memory_model()
        if model is None:
            return
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        try:
            atom = model.supersede_atom(memory_id, (data or {}).get("replacement", data or {}))
        except ValueError as exc:
            self._json_response(404, {"error": {"message": str(exc)}})
            return
        self._json_response(200, {"atom": atom, "superseded": memory_id})

    def _memory_atom_delete(self, memory_id):
        model = self._structured_memory_model()
        if model is None:
            return
        if not model.delete_atom(memory_id):
            self._json_response(404, {"error": {"message": "active memory atom not found"}})
            return
        self._json_response(200, {"status": "deleted", "memory_id": memory_id})

    def _memory_trait_create(self):
        model = self._structured_memory_model()
        if model is None:
            return
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        data = data or {}
        try:
            trait = model.derive_trait(data.get("trait"), data.get("value"), data.get("source_memory_ids"), data.get("scope", "user"), data.get("scope_id", ""))
        except ValueError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        self._json_response(201, {"trait": trait})

    def _growth_proposal_create(self):
        model = self._structured_memory_model()
        if model is None:
            return
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        data = data or {}
        try:
            proposal = model.create_growth_proposal(data.get("kind"), data.get("payload"), data.get("risk_level"), data.get("evidence_refs"))
        except ValueError as exc:
            self._json_response(400, {"error": {"message": str(exc)}})
            return
        self._json_response(201, {"proposal": proposal})

    def _growth_proposal_review(self, parsed_path):
        model = self._structured_memory_model()
        if model is None:
            return
        match = re.fullmatch(r"/v1/memory/growth-proposals/([^/]+)/(approve|reject)", parsed_path)
        if not match:
            self._json_response(404, {"error": {"message": "proposal path not found"}})
            return
        proposal = model.review_growth_proposal(urllib.parse.unquote(match.group(1)), match.group(2))
        if proposal is None:
            self._json_response(404, {"error": {"message": "pending growth proposal not found"}})
            return
        self._json_response(200, {"proposal": proposal})

    def _memory_reflect(self):
        """Trigger post-response reflection for non-ACP models (browser calls this after getting a response)."""
        if not _st.cognition_enabled:
            self._json_response(200, {"status": "cognition_disabled"})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._json_response(400, {"error": {"message": "Empty request body"}})
            return

        body = self.rfile.read(content_length).decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json_response(400, {"error": {"message": "Invalid JSON"}})
            return

        user_msg = data.get("user_message", "")
        assistant_msg = data.get("assistant_message", "")
        model = data.get("model", "unknown")
        conversation_id = str(data.get("session_id") or data.get("conversation_id") or "").strip()[:120]
        turn_id = str(data.get("turn_id") or "").strip()[:120]
        if turn_id and not re.fullmatch(r"turn-[A-Za-z0-9-]{8,115}", turn_id):
            self._json_response(400, {"error": {"message": "turn_id is invalid"}})
            return
        if user_msg:
            _mark_user_activity()

        if user_msg and assistant_msg:
            if _st.protected_memory_model_release:
                self._json_response(200, {"status": "skipped_protected_release"})
                return
            threading.Thread(target=_post_response_reflection,
                             args=(user_msg, assistant_msg, model, conversation_id, turn_id or None),
                             daemon=True).start()

        self._json_response(200, {"status": "ok"})

    def _memory_remember_location(self):
        """Persist exactly one explicit user location without a model round trip."""
        data, error = self._read_json_body()
        if error:
            self._json_response(400, {"error": {"message": error}})
            return
        if not isinstance(data, dict):
            self._json_response(400, {"error": {"message": "Request body must be an object."}})
            return
        user_message = data.get("user_message", "")
        session_id = str(data.get("session_id") or "").strip()[:120]
        turn_id = str(data.get("turn_id") or "").strip()[:120]
        if not isinstance(user_message, str) or len(user_message) > 500:
            self._json_response(400, {"error": {"message": "Location statement is invalid."}})
            return
        if turn_id and not re.fullmatch(r"turn-[A-Za-z0-9-]{8,115}", turn_id):
            self._json_response(400, {"error": {"message": "turn_id is invalid"}})
            return
        facts = [
            fact for fact in _extract_explicit_user_facts(user_message)
            if fact.get("Entity") == "User" and fact.get("Relation") == "user_location"
        ]
        if len(facts) != 1:
            self._json_response(400, {"error": {"message": "State one explicit location to save."}})
            return
        if _st.protected_memory_model_release:
            self._json_response(423, {"error": {"message": "Memory updates are locked."}})
            return
        _mark_user_activity()
        _post_response_reflection(
            user_message,
            "I've saved your location for future briefings.",
            "eva-memory-fast-path",
            session_id,
            turn_id or None,
        )
        self._json_response(201, {"status": "saved", "relation": "user_location"})

    def _kusto_seed(self):
        """Apply the Eva Kusto schema seed file to a configured database."""
        # Seed runs Kusto management commands, so refuse it on non-loopback binds.
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "/v1/kusto/seed is only available on localhost-bound bridges"}})
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._json_response(400, {"error": {"message": "Empty request body"}})
            return

        body = self.rfile.read(content_length).decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json_response(400, {"error": {"message": "Invalid JSON"}})
            return

        cluster_url = str(data.get("cluster_url", "")).strip()
        database = str(data.get("database", "")).strip()
        if not cluster_url or not database:
            self._json_response(400, {"error": {"message": "cluster_url and database are required"}})
            return
        schema_only = bool(data.get("schema_only", False))

        expected_cluster = os.environ.get("KUSTO_CLUSTER_URL", "").strip()
        if expected_cluster and not _same_kusto_cluster(cluster_url, expected_cluster):
            self._json_response(400, {"error": {"message": "cluster_url does not match configured KUSTO_CLUSTER_URL"}})
            return

        if _st.kusto_database_locked:
            locked_database = _get_locked_kusto_database()
            if not locked_database:
                self._json_response(400, {"error": {"message": "KUSTO_DATABASE is required when KUSTO_DATABASE_LOCKED is set"}})
                return
            if database.lower() != locked_database.lower():
                self._json_response(400, {"error": {"message": "database does not match locked KUSTO_DATABASE"}})
                return
            if _st.active_kusto_cluster and not _same_kusto_cluster(cluster_url, _st.active_kusto_cluster):
                self._json_response(400, {"error": {"message": "cluster_url does not match active Kusto MCP configuration"}})
                return
            database = locked_database

        token_ok, token_error = _ensure_kusto_token()
        if not token_ok:
            self._json_response(503, {
                "ok": False,
                "applied": 0,
                "failed": 1,
                "errors": ["Kusto authentication failed: " + token_error],
                "warning": "Re-running this seed will duplicate inline rows."
            })
            return

        seed_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eva_seed.kql")
        try:
            with open(seed_path, "r", encoding="utf-8") as seed_file:
                seed_text = seed_file.read()
        except OSError as error:
            self._json_response(500, {"error": {"message": "Could not read eva_seed.kql: " + str(error)}})
            return

        applied = 0
        failed = 0
        errors = []
        blocks = _split_kusto_seed_blocks(seed_text)
        if schema_only:
            blocks = [block for block in blocks if _is_kusto_schema_block(block)]
        # TODO: The inline seed rows use fixed values, so repeated runs can duplicate rows.
        for index, block in enumerate(blocks, start=1):
            result, kusto_error = _kusto_query_with_error(cluster_url, database, block, is_mgmt=True)
            if result is None:
                failed += 1
                first_line = block.splitlines()[0] if block.splitlines() else "empty block"
                errors.append(f"Block {index} failed: {first_line[:120]}: {kusto_error or 'no Kusto diagnostic returned'}")
            else:
                applied += 1

        warning = "Schema-only seed: existing tables are unchanged and no rows were ingested." if schema_only else "Re-running this seed will duplicate inline rows."
        mcp_config = dict(_st.configured_mcp_config)
        if (
            failed == 0
            and not _st.cognition_enabled
            and _st.kusto_token_cache
            and _st.acp_client is not None
            and getattr(_st.acp_client, "alive", False)
            and "kusto-mcp-server" in mcp_config
        ):
            bridge_port = getattr(self.server, "server_port", None)
            _enable_cognition(mcp_config, model=_st.acp_client.model, port=bridge_port)
        self._json_response(200, {
            "ok": failed == 0,
            "applied": applied,
            "failed": failed,
            "errors": errors,
            "warning": warning
        })

    def _mcp_configure(self):
        """Configure MCP servers and restart the ACP client."""
        # global statement removed — writes go to _st.*

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._json_response(400, {"error": {"message": "Empty request body"}})
            return

        body = self.rfile.read(content_length).decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json_response(400, {"error": {"message": "Invalid JSON"}})
            return

        requested_mcp_servers = data.get("mcp_servers", {})
        if not isinstance(requested_mcp_servers, dict):
            self._json_response(400, {"error": {"message": "mcp_servers must be an object"}})
            return
        from bridge.local_mcp import normalize_mcp_config
        mcp_servers = normalize_mcp_config(requested_mcp_servers)
        unsupported_servers = sorted(set(requested_mcp_servers) - set(mcp_servers))
        if unsupported_servers:
            self._json_response(400, {"error": {"message": "unsupported MCP server: " + ", ".join(unsupported_servers)}})
            return

        # Persist the raw selection (secrets stripped) so it survives bridge
        # restarts even if the Electron file:// localStorage is cleared.
        _persist_mcp_config(mcp_servers)

        # Resolve internal flags in MCP server env before passing to copilot
        # If the browser sent a github_pat, use it for _useGitHubPAT resolution
        request_github_pat = data.get('github_pat', '')
        unresolved_servers = []
        for srv_name, srv_cfg in mcp_servers.items():
            env = srv_cfg.get('env', {})
            resolved_env = {}
            for k, v in env.items():
                # _useGitHubPAT: resolve to actual PAT from request body or environment
                if k == '_useGitHubPAT':
                    pat = request_github_pat or os.environ.get('GITHUB_PERSONAL_ACCESS_TOKEN', '') or os.environ.get('GITHUB_PAT', '')
                    if pat:
                        resolved_env['GITHUB_PERSONAL_ACCESS_TOKEN'] = pat
                    else:
                        print(f"[MCP] Warning: GitHub PAT not available for {srv_name}. Set it in Settings > Auth.")
                        unresolved_servers.append(srv_name)
                    continue
                # Skip any other internal flags (prefixed with _)
                if k.startswith('_'):
                    continue
                # Ensure all env values are strings (subprocess.Popen requirement)
                resolved_env[k] = str(v) if not isinstance(v, str) else v
            srv_cfg['env'] = resolved_env
        for srv_name in unresolved_servers:
            mcp_servers.pop(srv_name, None)

        if _st.kusto_database_locked and "kusto-mcp-server" in mcp_servers:
            kusto_env = mcp_servers["kusto-mcp-server"].setdefault("env", {})
            locked_db = kusto_env.get("KUSTO_DATABASE") or _get_locked_kusto_database()
            if locked_db:
                kusto_env["KUSTO_DATABASE"] = locked_db
            kusto_env["KUSTO_DATABASE_LOCKED"] = "1"

        # Inject cached Kusto token if kusto-mcp-server is being configured
        # If no token is cached yet, attempt MSAL silent refresh (same as --enable-kusto-mcp startup)
        if "kusto-mcp-server" in mcp_servers and not _st.kusto_token_cache:
            _try_kusto_silent_auth()
        mcp_servers = _inject_kusto_token(mcp_servers)
        _capture_active_kusto_env(mcp_servers)
        _st.configured_mcp_config = copy.deepcopy(mcp_servers)

        # Restart ACP client with new MCP config
        old_path = _st.acp_client.copilot_path if _st.acp_client else "copilot"
        old_cwd = _st.acp_client.cwd if _st.acp_client else os.getcwd()
        old_model = _st.acp_client.model if _st.acp_client else None
        old_reasoning_effort = _st.acp_client.reasoning_effort if _st.acp_client else None
        if _st.acp_client:
            _st.acp_client.stop()

        _st.acp_client = ACPClient(copilot_path=old_path, cwd=old_cwd, model=old_model, mcp_config={},
                       reasoning_effort=old_reasoning_effort, tool_profile="none")
        try:
            _st.acp_client.start()
            # MCP config changed: drop stale warm clients so the pool only holds
            # clients built with the new server set.
            _reset_acp_pool(_st.acp_client)
            if _st.local_mode:
                active_servers = []
                unavailable_servers = {}
                try:
                    from bridge.local_mcp import LocalMCPManager
                    local_config = dict(mcp_servers)
                    if "eva-web-search" not in local_config:
                        web_search_path = os.path.join(
                            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "web_search_mcp.py",
                        )
                        if os.path.isfile(web_search_path):
                            local_config["eva-web-search"] = {"command": sys.executable, "args": [web_search_path]}
                    replacement_manager = LocalMCPManager()
                    replacement_manager.start_servers(local_config)
                    mcp_servers = _revoke_missing_local_mcp_servers(mcp_servers, replacement_manager)
                    previous_manager = _st.local_mcp_manager
                    _st.local_mcp_manager = replacement_manager
                    if previous_manager:
                        previous_manager.stop_all()
                    active_servers = [name for name, server in replacement_manager.servers.items() if server.alive]
                    unavailable_servers = dict(replacement_manager.start_failures)
                    print(f"[Mode] Refreshed LOCAL mode: {replacement_manager.tool_count} tools")
                except Exception as local_error:
                    unavailable_servers = {name: "refresh_failed" for name in mcp_servers}
                    print(f"[Mode] Could not refresh local MCP servers: {local_error}")
            else:
                active_servers = list(mcp_servers.keys())
                unavailable_servers = {}
            if not _st.cognition_enabled:
                _reload_backend = _resolve_memory_backend()
                if _reload_backend == "sqlite":
                    bridge_port = getattr(self.server, "server_port", None) or getattr(self.server, "server_address", (None, None))[1]
                    _enable_cognition(mcp_servers, model=old_model, port=bridge_port)
                elif "kusto-mcp-server" in mcp_servers and _st.kusto_token_cache:
                    bridge_port = getattr(self.server, "server_port", None) or getattr(self.server, "server_address", (None, None))[1]
                    _enable_cognition(mcp_servers, model=old_model, port=bridge_port)
            self._json_response(200, {
                "status": "ok",
                "message": f"MCP servers configured: {list(mcp_servers.keys())}",
                "configured_servers": list(mcp_servers.keys()),
                "active_servers": active_servers,
                "unavailable_servers": unavailable_servers
            })
        except RuntimeError as e:
            self._json_response(503, {"error": {"message": str(e)}})

    def _chat_completions(self):
        # global statement removed — writes go to _st.*
        if not _st.acp_client or not _st.acp_client.alive:
            self._json_response(503, {"error": {"message": "ACP bridge not connected to Copilot"}})
            return

        # Read request body
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._json_response(400, {"error": {"message": "Empty request body"}})
            return

        body = self.rfile.read(content_length).decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._json_response(400, {"error": {"message": "Invalid JSON"}})
            return

        messages = data.get("messages", [])
        if not messages:
            self._json_response(400, {"error": {"message": "No messages provided"}})
            return
        _set_openai_key_from(data)  # cache key for semantic recall
        requested_model = data.get("acp_model", "") or ""
        conversation_id = str(data.get("session_id") or data.get("conversation_id") or "").strip()[:120]
        turn_id = str(data.get("turn_id") or uuid.uuid4())[:120]
        raw_reasoning_effort = data.get("acp_reasoning_effort", "")
        if not isinstance(raw_reasoning_effort, str) or raw_reasoning_effort not in ACP_REASONING_EFFORTS | {""}:
            self._json_response(400, {"error": {"message": "Unsupported acp_reasoning_effort"}})
            return
        reasoning_effort = raw_reasoning_effort
        stream_requested = data.get("stream") is True
        acp_auto_approve = data.get("acp_auto_approve", False)
        if not isinstance(acp_auto_approve, bool):
            self._json_response(400, {"error": {"message": "acp_auto_approve must be a boolean"}})
            return

        # Build prompt text from messages (combine for context)
        # ACP doesn't have native message roles, so we format them
        prompt_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                # Handle structured content (text + images)
                text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                content = " ".join(text_parts)
            if role == "system" or role == "developer":
                prompt_parts.append(f"[System Instructions]: {content}")
            elif role == "assistant":
                prompt_parts.append(f"[Previous Response]: {content}")
            elif role == "user":
                prompt_parts.append(content)

        # For a simple chat, send just the last user message if conversation is managed by ACP
        # For full context, join all messages
        prompt_text = "\n\n".join(prompt_parts)

        # --- Cognition: Inject memory context before the prompt ---
        last_user_msg = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                c = msg.get("content", "")
                last_user_msg = " ".join(p.get("text", "") for p in c if p.get("type") == "text") if isinstance(c, list) else c
                break
        if last_user_msg:
            _mark_user_activity()

        direct_request_type = _classify_request_type(last_user_msg.lower()) if last_user_msg else "general"
        direct_profile = _select_acp_tool_profile(last_user_msg, direct_request_type)
        direct_passive_recall = _is_passive_memory_recall(last_user_msg)
        acp_permission_mode = "passive_recall" if direct_passive_recall else (
            "workspace_auto" if acp_auto_approve else "interactive"
        )

        memory_context = _build_memory_context(last_user_msg, conversation_id)
        if memory_context:
            prompt_text = memory_context + prompt_text
            print(f"[Cognition] Injected {len(memory_context)} chars of memory context")
        if re.search(r"\b(?:morning|daily)\s+(?:briefing|report|update)\b", last_user_msg, re.IGNORECASE):
            briefing_state = briefing_status()
            briefing_context = briefing_prompt_context(allow_partial=True)
            prompt_text += (
                "\n\n[Morning Briefing Request - AUTHORITATIVE]\n"
                "Answer the user's request now from the prepared entries below. Do not promise to prepare a later response.\n"
                + ("[Prepared Entries]\n" + briefing_context + "\n" if briefing_context else "")
                + ("Preparation is still running; clearly label any missing live sections and answer from available entries.\n"
                   if briefing_state.get("status") == "preparing" else "")
                + ("Preparation did not complete; state which live sections are unavailable and do not call this complete.\n"
                   if briefing_state.get("status") in {"partial", "failed"} else "")
            )
        if direct_passive_recall:
            prompt_text = (
                "[Passive Memory Recall - AUTHORITATIVE]\n"
                "Answer only from injected memory and untrusted prior-conversation excerpts. "
                "Do not invoke tools, shell commands, MCP operations, database queries, or permission requests. "
                "Ask for confirmation when a fact is not durable Knowledge.\n\n"
                + prompt_text
            )

        if direct_request_type == "github-data":
            prompt_text = (
                "[GitHub Native Tool Policy - AUTHORITATIVE]\n"
                "Use GitHub MCP tools or authenticated gh capabilities for GitHub API, repository, issue, pull request, workflow, release, branch, and comment operations. "
                "Never use browser or desktop automation for GitHub. For a mutation, honor the existing permission flow and report only the real result. "
                "If GitHub MCP or gh is unavailable, say so plainly.\n\n"
                + prompt_text
            )

        # Send to ACP
        with _acquire_acp_client(requested_model, reasoning_effort or None,
                     tool_profile=direct_profile) as (selected_client, acquire_detail):
            if not selected_client:
                self._json_response(503, {"error": {"message": acquire_detail}})
                return
            stream_state = self._new_stream_state(
                "copilot-acp", f"copilot-acp:{requested_model}" if requested_model else "copilot-acp"
            ) if stream_requested else None
            on_chunk = (lambda chunk: self._stream_chunk(stream_state, chunk)) if stream_state else None
            result = selected_client.prompt(
                prompt_text, timeout=180,
                conversation_id=_passive_recall_session_key(conversation_id)
                if direct_passive_recall else conversation_id,
                on_chunk=on_chunk,
                permission_mode=acp_permission_mode,
            )

        if "error" in result:
            error_detail = result["error"]
            if isinstance(error_detail, dict):
                error_msg = error_detail.get("message", str(error_detail))
            else:
                error_msg = str(error_detail)
            if stream_state and stream_state.get("started"):
                self._stream_error(stream_state, error_msg, 500)
            else:
                self._json_response(500, {"error": {"message": error_msg}})
            return

        # Format as OpenAI-compatible response
        response = {
            "id": f"acp-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": f"copilot-acp:{requested_model}" if requested_model else "copilot-acp",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": result.get("text", "")
                },
                "finish_reason": (
                    "length" if result.get("stop_reason") in ("max_tokens", "length")
                    else "stop" if result.get("stop_reason") == "end_turn"
                    else result.get("stop_reason", "stop")
                )
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }
        if stream_state:
            self._stream_finish(stream_state, response)
        else:
            self._json_response(200, response)

        # --- Cognition: Post-response reflection (background) ---
        response_text = result.get("text", "")
        model_label = f"copilot-acp:{requested_model}" if requested_model else "copilot-acp"
        if last_user_msg and response_text:
            threading.Thread(target=_post_response_reflection,
                           args=(last_user_msg, response_text, model_label, conversation_id, turn_id),
                           daemon=True).start()

    # ------------------------------------------------------------------
    # Vision browser agent endpoints
    # ------------------------------------------------------------------

    def _make_director(self):
        """Wire Claude Opus 4.8 (via ACP) as the text-only director. Returns a
        callback(goal, state) -> subgoal string, or None when ACP is unavailable."""
        client = _st.acp_client
        if not client:
            return None

        def director(goal, state):
            prompt = (
                "You are the director for a browser automation agent. You plan; a "
                "separate vision model looks at the screen and clicks.\n"
                f"User goal: {goal}\n"
                f"Current state: {state}\n"
                "Reply with ONE short imperative subgoal (a single sentence) for the "
                "executor's next few actions. No preamble, no markdown, no lists."
            )
            try:
                res = client.prompt(prompt, timeout=60)
                if isinstance(res, dict):
                    return (res.get("text") or "").strip()[:300]
            except Exception as e:
                print(f"[Bridge] director prompt failed: {e}")
            return ""

        return director

    def _browser_run(self):
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        if _BROWSER_AGENT is None:
            self._json_response(503, {"error": {"message": "Browser agent module not loaded"}})
            return
        ok, detail = _BROWSER_AGENT.playwright_available()
        if not ok:
            self._json_response(503, {"error": {"message":
                detail + ". Install with: python3 -m pip install --user --break-system-packages "
                "playwright && python3 -m playwright install chromium"}})
            return
        api_key = _set_openai_key_from(data)
        use_director = data.get("use_director", True)
        director = self._make_director() if use_director else None
        try:
            status = _BROWSER_AGENT.start_run(
                goal=(data.get("goal") or "").strip(),
                api_key=api_key,
                vision_model=(data.get("vision_model") or None),
                director=director,
                autonomy=(data.get("autonomy") or "pause"),
                max_steps=data.get("max_steps", 25),
                start_url=(data.get("start_url") or ""),
                headless=bool(data.get("headless", False)),
            )
        except Exception as e:
            self._json_response(400, {"error": {"message": str(e)}})
            return
        self._json_response(202, status)

    def _browser_status(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        run_id = (qs.get("run_id") or [""])[0]
        status = _BROWSER_AGENT.public_status(run_id) if _BROWSER_AGENT else None
        if not status:
            self._json_response(404, {"error": {"message": "unknown run_id"}})
            return
        self._json_response(200, status)

    def _browser_screenshot(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        run_id = (qs.get("run_id") or [""])[0]
        path = _BROWSER_AGENT.latest_screenshot_path(run_id) if _BROWSER_AGENT else None
        if not path:
            self._json_response(404, {"error": {"message": "no screenshot yet"}})
            return
        try:
            with open(path, "rb") as f:
                body = f.read()
        except Exception:
            self._json_response(404, {"error": {"message": "screenshot unavailable"}})
            return
        try:
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _browser_confirm(self):
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        run_id = (data.get("run_id") or "").strip()
        ok = bool(_BROWSER_AGENT) and _BROWSER_AGENT.resolve(
            run_id, approve=bool(data.get("approve", True)), text=(data.get("text") or ""))
        self._json_response(200 if ok else 404, {"ok": ok})

    def _browser_cancel(self):
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        run_id = (data.get("run_id") or "").strip()
        ok = bool(_BROWSER_AGENT) and _BROWSER_AGENT.cancel(run_id)
        self._json_response(200 if ok else 404, {"ok": ok})

    # ── Desktop agent (computer use) ──────────────────────────────────
    def _make_desktop_director(self):
        """Wire Claude (via ACP) as the text-only director for the desktop agent."""
        client = _st.acp_client
        if not client:
            return None

        def director(goal, state):
            prompt = (
                "You are the director for a desktop automation agent. You plan; a "
                "separate vision model looks at the screen, launches apps, clicks, "
                "and types.\n"
                f"User goal: {goal}\n"
                f"Current state: {state}\n"
                "Reply with ONE short imperative subgoal (a single sentence) for the "
                "executor's next few actions. No preamble, no markdown, no lists."
            )
            try:
                res = client.prompt(prompt, timeout=60)
                if isinstance(res, dict):
                    return (res.get("text") or "").strip()[:300]
            except Exception as e:
                print(f"[Bridge] desktop director prompt failed: {e}")
            return ""

        return director

    def _desktop_run(self):
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        if _DESKTOP_AGENT is None:
            self._json_response(503, {"error": {"message": "Desktop agent module not loaded"}})
            return
        ok, detail = _DESKTOP_AGENT.pyautogui_available()
        if not ok:
            self._json_response(503, {"error": {"message":
                detail + ". Install with: python3 -m pip install --user --break-system-packages pyautogui"}})
            return
        api_key = _set_openai_key_from(data)
        use_director = data.get("use_director", True)
        director = self._make_desktop_director() if use_director else None
        try:
            status = _DESKTOP_AGENT.start_run(
                goal=(data.get("goal") or "").strip(),
                api_key=api_key,
                vision_model=(data.get("vision_model") or None),
                director=director,
                autonomy=(data.get("autonomy") or "pause"),
                max_steps=data.get("max_steps", 25),
            )
        except Exception as e:
            self._json_response(400, {"error": {"message": str(e)}})
            return
        self._json_response(202, status)

    def _desktop_status(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        run_id = (qs.get("run_id") or [""])[0]
        status = _DESKTOP_AGENT.public_status(run_id) if _DESKTOP_AGENT else None
        if not status:
            self._json_response(404, {"error": {"message": "unknown run_id"}})
            return
        self._json_response(200, status)

    def _desktop_screenshot(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        run_id = (qs.get("run_id") or [""])[0]
        path = _DESKTOP_AGENT.latest_screenshot_path(run_id) if _DESKTOP_AGENT else None
        if not path:
            self._json_response(404, {"error": {"message": "no screenshot yet"}})
            return
        try:
            with open(path, "rb") as f:
                body = f.read()
        except Exception:
            self._json_response(404, {"error": {"message": "screenshot unavailable"}})
            return
        try:
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _desktop_confirm(self):
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        run_id = (data.get("run_id") or "").strip()
        ok = bool(_DESKTOP_AGENT) and _DESKTOP_AGENT.resolve(
            run_id, approve=bool(data.get("approve", True)), text=(data.get("text") or ""))
        self._json_response(200 if ok else 404, {"ok": ok})

    def _desktop_cancel(self):
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        run_id = (data.get("run_id") or "").strip()
        ok = bool(_DESKTOP_AGENT) and _DESKTOP_AGENT.cancel(run_id)
        self._json_response(200 if ok else 404, {"ok": ok})

    # -- Camera presence sensor ("Eva's eyes") -----------------------------
    def _camera_start(self):
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        if _CAMERA is None:
            self._json_response(503, {"error": {"message": "Camera sensor module not loaded"}})
            return
        ok, detail = _CAMERA.opencv_available()
        if not ok:
            self._json_response(503, {"error": {"message":
                detail + ". Install with: python3 -m pip install --user --break-system-packages opencv-python"}})
            return
        try:
            status = _CAMERA.start(device=data.get("device"))
        except Exception as e:
            self._json_response(400, {"error": {"message": str(e)}})
            return
        self._json_response(200, status)

    def _camera_stop(self):
        if _CAMERA is None:
            self._json_response(503, {"error": {"message": "Camera sensor module not loaded"}})
            return
        try:
            status = _CAMERA.stop()
        except Exception as e:
            self._json_response(400, {"error": {"message": str(e)}})
            return
        self._json_response(200, status)

    def _camera_status(self):
        if _CAMERA is None:
            self._json_response(200, {"enabled": False, "present": False, "available": False})
            return
        status = _CAMERA.status()
        status["available"] = _CAMERA.opencv_available()[0]
        self._json_response(200, status)

    def _camera_frame(self):
        body = _CAMERA.latest_jpeg() if _CAMERA else None
        if not body:
            self._json_response(404, {"error": {"message": "no frame yet"}})
            return
        try:
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    # -- Vision describe via a Copilot/Claude model (ACP image prompt) -------
    def _vision_look(self):
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        # Accept an explicit base64 image, or fall back to the latest camera frame.
        image_b64 = (data.get("image_b64") or "").strip()
        mime = (data.get("mime") or "image/jpeg").strip()
        if not image_b64:
            raw = _CAMERA.latest_jpeg() if _CAMERA else None
            if raw:
                image_b64 = base64.b64encode(raw).decode("ascii")
                mime = "image/jpeg"
        if not image_b64:
            self._json_response(404, {"error": {"message": "no image provided and no camera frame available"}})
            return

        question = (data.get("question") or "").strip() or (
            "Describe what you see in this image in one or two natural sentences, "
            "in the first person, as if you are seeing it now.")
        requested_model = (data.get("model") or "").strip() or None

        # Warm/select a Copilot model via ACP, then send the image prompt.
        ok, detail = _ensure_acp_model(requested_model)
        if not ok:
            self._json_response(503, {"error": {"message": "ACP model unavailable: " + str(detail)}})
            return
        client = _st.acp_client
        if client is None or not getattr(client, "alive", False):
            self._json_response(503, {"error": {"message": "ACP client not connected"}})
            return
        if not hasattr(client, "prompt_with_image"):
            self._json_response(503, {"error": {"message": "ACP client lacks image support"}})
            return
        try:
            result = client.prompt_with_image(question, image_b64, mime=mime, timeout=90)
        except Exception as e:
            self._json_response(502, {"error": {"message": "vision prompt failed: " + str(e)[:200]}})
            return
        if not isinstance(result, dict) or result.get("error"):
            msg = (result or {}).get("error") if isinstance(result, dict) else "no result"
            self._json_response(502, {"error": {"message": "vision model error: " + str(msg)[:200]}})
            return
        text = str(result.get("text", "") or "").strip()
        self._json_response(200, {"text": text, "model": detail})

    # -- Client preferences (non-secret UI toggles that survive a wipe) ------
    def _prefs_get(self):
        self._json_response(200, _load_client_prefs())

    def _prefs_set(self):
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        if not isinstance(data, dict):
            self._json_response(400, {"error": {"message": "expected an object"}})
            return
        saved = _save_client_prefs(data)
        _st.verbose_debug = saved.get("verbose_debug") is True
        self._json_response(200, saved)

    # ── Mode switching (cloud vs local) ─────────────────────────────

    def _get_mode(self):
        """GET /v1/mode — return current data retrieval mode."""
        local_tools = 0
        local_servers = []
        if _st.local_mcp_manager:
            local_tools = _st.local_mcp_manager.tool_count
            local_servers = [n for n, s in _st.local_mcp_manager.servers.items() if s.alive]
        self._json_response(200, {
            "mode": "local" if _st.local_mode else "cloud",
            "cloud_available": bool(_st.acp_client and _st.acp_client.alive),
            "local_available": bool(_st.local_mcp_manager and _st.local_mcp_manager.alive),
            "local_tools": local_tools,
            "local_servers": local_servers,
        })

    def _set_mode(self):
        """POST /v1/mode — switch between cloud and local data retrieval.

        Body: {"mode": "local"|"cloud"}
        When switching to local for the first time, MCP servers from the
        current config are spawned and the tool catalog is built.
        """
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "only available on localhost"}})
            return
        data, err = self._read_json_body()
        if err:
            self._json_response(400, {"error": {"message": err}})
            return
        requested = (data.get("mode") or "").strip().lower()
        if requested not in ("local", "cloud"):
            self._json_response(400, {"error": {"message": "mode must be 'local' or 'cloud'"}})
            return

        if requested == "local":
            # Start local MCP servers if not already running
            if not _st.local_mcp_manager or not _st.local_mcp_manager.alive:
                try:
                    from bridge.local_mcp import LocalMCPManager
                    mcp_config = {}
                    # Reuse the same MCP config that ACP uses (minus cloud-only servers)
                    if _st.configured_mcp_config:
                        mcp_config = dict(_st.configured_mcp_config)
                    if not mcp_config:
                        mcp_config = _load_persisted_mcp_config()
                    configured_local_mcp = dict(mcp_config)
                    # Always include the web search MCP server for local mode
                    # (replaces Copilot CLI's built-in Bing search)
                    if "eva-web-search" not in mcp_config:
                        # Try multiple paths: bridge/../../web_search_mcp.py (source layout)
                        # and $HOME/.eva/tools/web_search_mcp.py (installed copy)
                        _ws_candidates = [
                            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web_search_mcp.py"),
                            os.path.expanduser("~/.eva/tools/web_search_mcp.py"),
                        ]
                        for _ws_path in _ws_candidates:
                            if os.path.isfile(_ws_path):
                                mcp_config["eva-web-search"] = {
                                    "command": sys.executable,
                                    "args": [_ws_path],
                                }
                                print(f"[Mode] Auto-added eva-web-search MCP from {_ws_path}")
                                break
                        else:
                            print(f"[Mode] web_search_mcp.py not found at: {_ws_candidates}")
                    if not mcp_config:
                        print("[Mode] Warning: no MCP servers configured for local mode")
                    _st.local_mcp_manager = LocalMCPManager()
                    _st.local_mcp_manager.start_servers(mcp_config)
                    _revoke_missing_local_mcp_servers(configured_local_mcp, _st.local_mcp_manager)
                    print(f"[Mode] Local MCP started: {_st.local_mcp_manager.tool_count} tools from {list(mcp_config.keys())}")
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    self._json_response(500, {"error": {"message": f"Failed to start local MCP: {e}"}})
                    return
            _st.local_mode = True
            print("[Mode] Switched to LOCAL (no cloud AI)")
        else:
            _st.local_mode = False
            print("[Mode] Switched to CLOUD (Copilot CLI)")

        # Persist mode preference so it survives bridge restarts
        try:
            os.makedirs(os.path.dirname(_cfg.MODE_PREF_PATH), exist_ok=True)
            with open(_cfg.MODE_PREF_PATH, "w") as f:
                f.write("local" if _st.local_mode else "cloud")
        except OSError:
            pass

        self._json_response(200, {
            "mode": "local" if _st.local_mode else "cloud",
            "local_tools": _st.local_mcp_manager.tool_count if _st.local_mcp_manager else 0,
        })

    def _json_response(self, status, data):
        body = json.dumps(data).encode("utf-8")
        try:
            self.send_response(status)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass  # Client disconnected (e.g. browser health poll timeout)

    def log_message(self, format, *args):
        # Quieter logging
        try:
            msg = format % args if args else format
        except (TypeError, IndexError):
            msg = f"{format} {args}"
        sys.stderr.write(f"[Bridge] {msg}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # global statement removed — writes go to _st.*
    _install_log_tee()
    default_port = 8888
    env_port = os.environ.get("EVA_ACP_PORT", "").strip()
    if env_port:
        try:
            default_port = int(env_port)
        except ValueError:
            print(f"[Bridge] Warning: Ignoring invalid EVA_ACP_PORT={env_port!r}")

    parser = argparse.ArgumentParser(description="Eva ACP Bridge Server")
    parser.add_argument("--port", type=int, default=default_port, help="HTTP server port (default: 8888 or EVA_ACP_PORT)")
    # The Kusto seed endpoint is refused unless this bind address is loopback.
    parser.add_argument("--bind", default="127.0.0.1", help="Bind address (default: 127.0.0.1, use 0.0.0.0 for LAN access; seed endpoint is disabled off loopback)")
    parser.add_argument("--copilot-path", default="copilot", help="Path to copilot CLI binary")
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory for ACP session")
    parser.add_argument("--model", default=None, help="Default AI model (e.g. claude-sonnet-4.6, gpt-5.2)")
    parser.add_argument("--mcp-config", default=None, help="Path to MCP config JSON file or inline JSON")
    parser.add_argument("--enable-azure-mcp", action="store_true", help="Enable Azure MCP Server (requires az login)")
    parser.add_argument("--enable-github-mcp", action="store_true", help="Enable GitHub MCP Server (requires GITHUB_PERSONAL_ACCESS_TOKEN env)")
    parser.add_argument("--enable-kusto-mcp", action="store_true", help="Enable Kusto MCP Server (DeviceCodeCredential, no subscription needed)")
    parser.add_argument("--kusto-cluster", default="", help="Kusto cluster URL")
    parser.add_argument("--kusto-database", default="", help="Default Kusto database name")
    args = parser.parse_args()
    _st.bridge_bind_address = args.bind

    # Build MCP config
    mcp_config = {}
    mcp_config_source = args.mcp_config
    # Auto-discover mcp.json from project root when no explicit --mcp-config
    if not mcp_config_source:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        auto_path = os.path.join(project_root, "mcp.json")
        if os.path.isfile(auto_path):
            mcp_config_source = auto_path
            print(f"[Bridge] Auto-discovered MCP config: {auto_path}")
    if mcp_config_source:
        try:
            if os.path.isfile(mcp_config_source):
                with open(mcp_config_source) as f:
                    cfg = json.load(f)
                mcp_config = cfg.get("mcpServers", cfg)
            else:
                cfg = json.loads(mcp_config_source)
                mcp_config = cfg.get("mcpServers", cfg)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[Bridge] Warning: Failed to parse MCP config: {e}")

    if args.enable_azure_mcp:
        mcp_config["azure-mcp-server"] = {
            "command": "npx",
            "args": ["-y", "@azure/mcp@latest", "server", "start"],
            "env": {"AZURE_MCP_COLLECT_TELEMETRY": "false"}
        }
        print("[Bridge] Azure MCP Server enabled (Kusto/ADX, Storage, Monitor, etc.)")

    if args.enable_github_mcp:
        gh_token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
        if not gh_token:
            print("[Bridge] Warning: GITHUB_PERSONAL_ACCESS_TOKEN not set. GitHub MCP tools may not work.")
        mcp_config["github-mcp-server"] = {
            "command": "docker",
            "args": ["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/github-mcp-server"],
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": gh_token} if gh_token else {}
        }
        print("[Bridge] GitHub MCP Server enabled")

    if args.enable_kusto_mcp:
        # global statement removed — writes go to _st.*
        script_dir = os.path.dirname(os.path.abspath(__file__))
        kusto_mcp_path = os.path.join(script_dir, "kusto_mcp.py")
        kusto_env = {}
        if args.kusto_cluster:
            kusto_env["KUSTO_CLUSTER_URL"] = args.kusto_cluster
            _persist_kusto_cluster(args.kusto_cluster)
        if args.kusto_database:
            kusto_env["KUSTO_DATABASE"] = args.kusto_database
        if _st.kusto_database_locked:
            kusto_env["KUSTO_DATABASE_LOCKED"] = "1"

        # Pre-fetch Kusto token so the MCP subprocess doesn't need interactive auth
        try:
            from azure.identity import DeviceCodeCredential, TokenCachePersistenceOptions
            cache_opts = TokenCachePersistenceOptions(allow_unencrypted_storage=True)

            # Try silent refresh via MSAL directly (reads ~/.azure/msal_token_cache.json)
            token = None
            cred = None
            try:
                import msal as _msal
                _cache_path = os.path.expanduser("~/.azure/msal_token_cache.json")
                if os.path.isfile(_cache_path):
                    print("[Bridge] Trying cached Kusto token (MSAL silent refresh)...")
                    _msal_cache = _msal.SerializableTokenCache()
                    with open(_cache_path) as _cf:
                        _msal_cache.deserialize(_cf.read())
                    _app = _msal.PublicClientApplication(
                        "04b07795-8ddb-461a-bbee-02f9e1bf7b46",
                        authority="https://login.microsoftonline.com/organizations",
                        token_cache=_msal_cache
                    )
                    _accounts = _app.get_accounts()
                    if _accounts:
                        msal_cred = _MSALSilentCredential(
                            app=_app,
                            account=_accounts[0],
                            token_cache=_msal_cache,
                            cache_path=_cache_path,
                            default_scopes=["https://kusto.kusto.windows.net/.default"],
                        )
                        token = msal_cred.get_token("https://kusto.kusto.windows.net/.default")
                        if token and getattr(token, "token", None):
                            cred = msal_cred
                            print(f"[Bridge] Kusto token refreshed silently from MSAL cache")
                        else:
                            print(f"[Bridge] MSAL silent refresh returned no token")
                    else:
                        print("[Bridge] No accounts in MSAL cache")
            except ImportError:
                print("[Bridge] msal package not available, skipping silent refresh")
            except Exception as e:
                print(f"[Bridge] MSAL silent refresh failed: {e}")

            # Fall back to device code flow if no cached token
            if not token:
                print("[Bridge] Authenticating for Kusto (will prompt for device code)...")
                cred = DeviceCodeCredential(
                    cache_persistence_options=cache_opts
                )
                token = cred.get_token("https://kusto.kusto.windows.net/.default")
            kusto_env["KUSTO_ACCESS_TOKEN"] = token.token
            # Cache globally for model switches
            _st.kusto_token_cache = token.token
            _st.kusto_credential = cred
            print(f"[Bridge] Kusto token obtained and cached (length: {len(token.token)})")

            # Auto-discover cluster URL from local cache if not explicitly provided
            if "KUSTO_CLUSTER_URL" not in kusto_env:
                cached_cluster = _load_cached_kusto_cluster()
                if cached_cluster:
                    # Validate the cached cluster URL with a lightweight query
                    test_rows = _kusto_query_direct(cached_cluster, "Eva", ".show databases", is_mgmt=True)
                    if test_rows is not None:
                        kusto_env["KUSTO_CLUSTER_URL"] = cached_cluster
                        print(f"[Bridge] Kusto cluster restored and validated from cache")
                    else:
                        print(f"[Bridge] Cached Kusto cluster failed validation, ignoring")
                else:
                    print(f"[Bridge] No cached Kusto cluster URL (pass --kusto-cluster once to seed)")
        except Exception as e:
            print(f"[Bridge] Warning: Could not pre-fetch Kusto token: {e}")
            print("[Bridge] The MCP server will try to authenticate on its own.")

        mcp_config["kusto-mcp-server"] = {
            "command": sys.executable,
            "args": [kusto_mcp_path],
            "env": kusto_env
        }
        print(f"[Bridge] Kusto MCP Server enabled (cluster: {args.kusto_cluster or 'from tool params'})")

    if _st.kusto_database_locked and "kusto-mcp-server" in mcp_config:
        kusto_env = mcp_config["kusto-mcp-server"].setdefault("env", {})
        locked_db = kusto_env.get("KUSTO_DATABASE") or _get_locked_kusto_database()
        if locked_db:
            kusto_env["KUSTO_DATABASE"] = locked_db
        kusto_env["KUSTO_DATABASE_LOCKED"] = "1"
    _capture_active_kusto_env(mcp_config)
    _st.configured_mcp_config = copy.deepcopy(mcp_config)

    # global statement removed — writes go to _st.*
    print(f"[Bridge] Starting ACP bridge on port {args.port}...")
    print(f"[Bridge] Copilot CLI: {args.copilot_path}")
    print(f"[Bridge] Working directory: {args.cwd}")
    if mcp_config:
        print(f"[Bridge] MCP Servers: {', '.join(mcp_config.keys())}")

    # Start ACP client
    # Start a tool-free base client; route-specific profiles are warmed lazily.
    _st.acp_client = ACPClient(copilot_path=args.copilot_path, cwd=args.cwd, model=args.model, mcp_config={}, tool_profile="none")
    try:
        _st.acp_client.start()
    except RuntimeError as e:
        print(f"[Bridge] Warning: {e}")
        print("[Bridge] Starting without ACP. Select LM Studio local mode, or install and authenticate Copilot CLI to enable cloud features.")

    # Enable cognition layer if memory backend is available
    # global statement removed — writes go to _st.*
    _startup_backend = _resolve_memory_backend()
    if _startup_backend == "sqlite":
        _enable_cognition(mcp_config, model=args.model, port=args.port)
    elif "kusto-mcp-server" in mcp_config and _st.kusto_token_cache:
        _enable_cognition(mcp_config, model=args.model, port=args.port)
    else:
        print(f"[Bridge] Cognition layer disabled (no Kusto MCP or token, and backend is not sqlite)")

    # Restore persisted local mode in a background thread so MCP server
    # spawning does not block the HTTP server from starting.
    if _st.local_mode:
        def _restore_local_mode():
            try:
                from bridge.local_mcp import LocalMCPManager
                _configured_local_cfg = dict(mcp_config) if mcp_config else _load_persisted_mcp_config()
                _local_cfg = dict(_configured_local_cfg)
                if "eva-web-search" not in _local_cfg:
                    _ws_candidates = [
                        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web_search_mcp.py"),
                        os.path.expanduser("~/.eva/tools/web_search_mcp.py"),
                    ]
                    for _ws_path in _ws_candidates:
                        if os.path.isfile(_ws_path):
                            _local_cfg["eva-web-search"] = {"command": sys.executable, "args": [_ws_path]}
                            break
                _st.local_mcp_manager = LocalMCPManager()
                _st.local_mcp_manager.start_servers(_local_cfg)
                _revoke_missing_local_mcp_servers(_configured_local_cfg, _st.local_mcp_manager)
                print(f"[Mode] Restored LOCAL mode: {_st.local_mcp_manager.tool_count} tools")
            except Exception as e:
                print(f"[Mode] Failed to restore local mode: {e}")
                _st.local_mode = False
        threading.Thread(target=_restore_local_mode, daemon=True).start()

    # Start HTTP server. Threaded so a long-running browser agent run does not
    # block status/cancel/confirm polling on other connections.
    server = ThreadingHTTPServer((args.bind, args.port), BridgeHandler)
    print(f"[Bridge] Listening on http://{args.bind}:{args.port}")
    start_startup_briefing()
    print(f"[Bridge] Endpoints:")
    print(f"  POST /v1/chat/completions   - Send chat messages")
    print(f"  GET  /v1/models             - List available models")
    print(f"  GET  /v1/mcp                - MCP server status")
    print(f"  POST /v1/mcp/configure      - Configure MCP servers (hot-reload)")
    print(f"  GET  /v1/goals              - List Kusto-backed goals")
    print(f"  POST /v1/goals              - Create a Kusto-backed goal")
    print(f"  PATCH /v1/goals/<id>        - Update a Kusto-backed goal")
    print(f"  DELETE /v1/goals/<id>       - Soft-delete a Kusto-backed goal")
    print(f"  GET  /v1/background/status  - Background loop status")
    print(f"  GET  /v1/background/proposals - List memory proposals")
    print(f"  GET  /v1/background/activity - List background activity")
    print(f"  POST /v1/background/control - Update background loop controls")
    print(f"  POST /v1/background/proposals/<id>/approve - Apply a memory proposal")
    print(f"  POST /v1/background/proposals/<id>/reject - Reject a memory proposal")
    print(f"  POST /v1/kusto/seed         - Apply Eva Kusto schema seed")
    print(f"  POST /v1/browser/run        - Start a vision browser agent run")
    print(f"  GET  /v1/browser/status     - Poll a browser agent run")
    print(f"  POST /v1/browser/confirm    - Approve/answer a parked browser run")
    print(f"  POST /v1/browser/cancel     - Cancel a browser agent run")
    print(f"  GET  /v1/files/<name>       - Download a generated artifact")
    print(f"  POST /v1/files/purge        - Delete all artifacts")
    print(f"  GET  /v1/doctor             - Structured readiness report")
    print(f"  GET  /v1/cron               - List cron tasks")
    print(f"  POST /v1/cron               - Create a cron task")
    print(f"  PATCH /v1/cron/<id>         - Update a cron task")
    print(f"  DELETE /v1/cron/<id>        - Delete a cron task")
    print(f"  POST /v1/skills/auto-learn  - Extract skill from interaction")
    print(f"  POST /v1/subagent/spawn     - Spawn a parallel subagent task")
    print(f"  GET  /v1/subagent/status    - Poll subagent task status")
    print(f"  GET  /health                - Health check")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Bridge] Shutting down...")
    finally:
        _stop_bg_loop()
        if _st.acp_client:
            _st.acp_client.stop()
        server.server_close()


if __name__ == "__main__":
    main()
