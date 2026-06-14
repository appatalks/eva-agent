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
import json
import mimetypes
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


# Domain modules
from bridge.acp_client import (  # noqa: F401
    ACPClient,
    _acp_model_key,
    _acp_pool_touch,
    _acp_pool_register,
    _acp_pool_evict_if_needed,
    _reset_acp_pool,
    _ensure_acp_model,
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
from bridge.telemetry import (  # noqa: F401
    _StdoutTee,
    _log_ring_add,
    _install_log_tee,
    _telemetry_clip,
    _telemetry_emit,
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
    _evarise_skill,
)
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
    _classify_request_type,
    _MEMORY_CAPTURE_DIRECTIVE,
)

# Constants needed by BridgeHandler (imported from config)
_LOG_RING_MAX = _cfg.LOG_RING_MAX
_NOTIFY_RING_MAX = _cfg.NOTIFY_RING_MAX
_ARTIFACTS_DIR = _cfg.ARTIFACTS_DIR
_GOAL_CATEGORIES = _cfg.GOAL_CATEGORIES
_GOAL_STATUSES = _cfg.GOAL_STATUSES
_GOAL_COLUMNS = _cfg.GOAL_COLUMNS
_GOALS_LATEST_QUERY = _cfg.GOALS_LATEST_QUERY
_SKILL_STATUSES = _cfg.SKILL_STATUSES
_SKILL_COLUMNS = _cfg.SKILL_COLUMNS
_SKILLS_LATEST_QUERY = _cfg.SKILLS_LATEST_QUERY
_BG_PROPOSAL_STATUSES = _cfg.BG_PROPOSAL_STATUSES
_BG_APPLY_TABLES = _cfg.BG_APPLY_TABLES
_ALERT_TYPES = _cfg.ALERT_TYPES
_ALERT_CHANNELS = _cfg.ALERT_CHANNELS
_TELEMETRY_RING_MAX = _cfg.TELEMETRY_RING_MAX
_BG_PROPOSAL_COLUMNS = _cfg.BG_PROPOSAL_COLUMNS
_SUBAGENT_MAX = 4
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

class BridgeHandler(BaseHTTPRequestHandler):
    """HTTP handler that bridges browser requests to ACP."""

    def _cors_headers(self):
        origin = self.headers.get("Origin", "")
        # Reject any origin containing CRLF or null bytes to prevent HTTP response splitting
        if "\r" in origin or "\n" in origin or "\x00" in origin:
            origin = ""
        allowed = not origin or origin.startswith("file://") or origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost") or origin.startswith("http://[::1]")
        if allowed:
            self.send_header("Access-Control-Allow-Origin", origin if origin else "*")
        else:
            self.send_header("Access-Control-Allow-Origin", "null")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path).path
        if parsed_path == "/health":
            self._health()
        elif parsed_path == "/v1/doctor":
            self._doctor()
        elif parsed_path == "/v1/models":
            self._models()
        elif parsed_path == "/v1/memory/backend":
            self._memory_backend_get()
        elif parsed_path == "/v1/mcp":
            self._mcp_status()
        elif parsed_path == "/v1/mcp/config":
            self._mcp_persisted_config()
        elif parsed_path == "/v1/cron":
            self._cron_list()
        elif parsed_path == "/v1/subagent/status":
            self._subagent_status()
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
        elif parsed_path == "/v1/background/proposals":
            self._background_proposals()
        elif parsed_path == "/v1/background/activity":
            self._background_activity()
        elif parsed_path == "/v1/alerts":
            self._alerts_list()
        elif parsed_path == "/v1/notifications":
            self._notifications_list()
        elif parsed_path == "/v1/memory/context":
            self._memory_context()
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
        elif parsed_path == "/v1/mode":
            self._get_mode()
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
        if parsed_path == "/v1/chat/completions":
            self._chat_completions()
        elif parsed_path == "/v1/mcp/configure":
            self._mcp_configure()
        elif parsed_path == "/v1/memory/reflect":
            self._memory_reflect()
        elif parsed_path == "/v1/memory/backend":
            self._memory_backend_set()
        elif parsed_path == "/v1/aig/chat":
            self._aig_chat()
        elif parsed_path == "/v1/telemetry":
            self._telemetry_ingest()
        elif parsed_path == "/v1/cron":
            self._cron_create()
        elif parsed_path == "/v1/skills/auto-learn":
            self._skills_auto_learn()
        elif parsed_path == "/v1/subagent/spawn":
            self._subagent_spawn()
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
        elif parsed_path == "/v1/notifications/seen":
            self._notifications_mark_seen()
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
        if parsed_path.startswith("/v1/goals/"):
            self._goals_patch(urllib.parse.unquote(parsed_path.split("/v1/goals/", 1)[1]))
        elif parsed_path.startswith("/v1/skills/"):
            self._skills_patch(urllib.parse.unquote(parsed_path.split("/v1/skills/", 1)[1]))
        elif parsed_path.startswith("/v1/cron/"):
            self._cron_update(urllib.parse.unquote(parsed_path.split("/v1/cron/", 1)[1]))
        else:
            self.send_error(404, "Not Found")

    def do_DELETE(self):
        parsed_path = urllib.parse.urlparse(self.path).path
        if parsed_path.startswith("/v1/goals/"):
            self._goals_delete(urllib.parse.unquote(parsed_path.split("/v1/goals/", 1)[1]))
        elif parsed_path.startswith("/v1/alerts/"):
            self._alerts_delete(urllib.parse.unquote(parsed_path.split("/v1/alerts/", 1)[1]))
        elif parsed_path.startswith("/v1/skills/"):
            self._skills_delete(urllib.parse.unquote(parsed_path.split("/v1/skills/", 1)[1]))
        elif parsed_path.startswith("/v1/cron/"):
            self._cron_delete(urllib.parse.unquote(parsed_path.split("/v1/cron/", 1)[1]))
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
                f"ORDER BY UpdatedAt DESC LIMIT 1"
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
        status = data.get("status", data.get("Status"))
        if status is not None:
            status = str(status).strip().lower()
            if status not in _SKILL_STATUSES:
                return None, "status must be one of: " + ", ".join(sorted(_SKILL_STATUSES))
            fields["Status"] = status
        if creating and not fields.get("Instructions"):
            return None, "instructions are required"
        return fields, ""

    def _skills_list(self):
        cluster, db, ok = self._kusto_context()
        if not ok:
            return
        if not _get_table_columns(cluster, db, "Skills"):
            self._json_response(200, {"skills": [], "warning": "Skills table may not exist yet; run /v1/kusto/seed to create it"})
            return
        rows = _kusto_query_direct(cluster, db, _SKILLS_LATEST_QUERY + " | where Status != 'deleted' | order by UpdatedAt desc")
        self._json_response(200, {"skills": rows or []})

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
        cluster, db, ok = self._kusto_context()
        if not ok:
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
            "Instructions": fields.get("Instructions", ""),
            "Tools": fields.get("Tools", ""),
            "Tags": fields.get("Tags", ""),
            "Source": fields.get("Source", ""),
            "Status": fields.get("Status", "active"),
            "CreatedAt": now,
            "UpdatedAt": now,
        }
        if not self._write_skill_row(cluster, db, row):
            self._json_response(500, {"error": {"message": "Skill write failed"}})
            return
        self._json_response(201, {"skill": row})

    def _skills_patch(self, raw_skill_id):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "skill mutations are restricted to loopback bind"}})
            return
        cluster, db, ok = self._kusto_context()
        if not ok:
            return
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
        now = self._goal_now()
        row = {col: current.get(col, "") for col in _SKILL_COLUMNS}
        row["SkillId"] = skill_id
        if not row.get("CreatedAt"):
            row["CreatedAt"] = now
        if not row.get("Status"):
            row["Status"] = "active"
        row.update(fields)
        row["UpdatedAt"] = now
        if not self._write_skill_row(cluster, db, row):
            self._json_response(500, {"error": {"message": "Skill write failed"}})
            return
        self._json_response(200, {"skill": row})

    def _skills_delete(self, raw_skill_id):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "skill mutations are restricted to loopback bind"}})
            return
        cluster, db, ok = self._kusto_context()
        if not ok:
            return
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

    def _open_artifact(self, requested_name):
        """Open an artifact file with the system's default application."""
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "only available on localhost"}})
            return
        if not _valid_artifact_name(requested_name):
            self._json_response(400, {"error": {"message": "invalid filename"}})
            return
        base = os.path.realpath(_ARTIFACTS_DIR)
        target = os.path.realpath(os.path.join(_ARTIFACTS_DIR, requested_name))
        if not target.startswith(base + os.sep) or not os.path.isfile(target):
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
        target = os.path.join(_ARTIFACTS_DIR, filename)
        base = os.path.realpath(_ARTIFACTS_DIR)
        if not os.path.realpath(target).startswith(base + os.sep):
            self._json_response(400, {"error": {"message": "path traversal"}})
            return
        try:
            if is_pdf:
                self._write_text_pdf(target, content)
            else:
                with open(target, "w", encoding="utf-8") as f:
                    f.write(content)
            size = os.path.getsize(target)
            print(f"[Artifact] Wrote {filename} ({size} bytes, pdf={is_pdf})")
            self._json_response(200, {"ok": True, "filename": filename, "size": size})
        except Exception as e:
            self._json_response(500, {"error": {"message": f"write failed: {e}"}})

    @staticmethod
    def _write_text_pdf(path, text):
        """Generate a minimal valid PDF from plain text and write to path."""
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
        with open(path, "wb") as f:
            f.write(bytes(ord(c) & 0xFF for c in out))

    def _serve_artifact(self, requested_name):
        if not _is_loopback_bind():
            self._json_response(403, {"error": {"message": "/v1/files is only available on localhost-bound bridges"}})
            return

        if not _valid_artifact_name(requested_name):
            self._json_response(400, {"error": {"message": "invalid filename"}})
            return

        base = os.path.realpath(_ARTIFACTS_DIR)
        target = os.path.realpath(os.path.join(_ARTIFACTS_DIR, requested_name))
        if not target.startswith(base + os.sep) or not os.path.isfile(target):
            self._json_response(404, {"error": {"message": "file not found"}})
            return

        content_type = _safe_content_type(mimetypes.guess_type(requested_name)[0])
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
            "status": "ok" if (_st.acp_client and _st.acp_client.alive) else "error",
            "session_id": _st.acp_client.session_id if _st.acp_client else None,
            "agent": _st.acp_client.agent_info if _st.acp_client else None,
            "model": _st.acp_client.model if _st.acp_client else None,
            "mcp_servers": list(_st.acp_client.mcp_config.keys()) if _st.acp_client and _st.acp_client.mcp_config else [],
            "cognition_enabled": _st.cognition_enabled,
            "cognition_launch_id": _st.cognition_launch_id,
            "cognition_launch_iso": _st.cognition_launch_iso,
            "memory_backend": backend,
            "memory_available": _memory_available(),
        }
        if backend == "sqlite" and _st.sqlite_mem:
            status["memory_db_path"] = _st.sqlite_mem.db_path
        self._json_response(200, status)

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
        mcp_names = list(_st.acp_client.mcp_config.keys()) if _st.acp_client and _st.acp_client.mcp_config else []
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
            '- "Instructions": step-by-step instructions Eva should follow (markdown)\n'
            '- "Tools": comma-separated list of tools/capabilities used\n'
            '- "Tags": comma-separated tags for categorization\n\n'
            "Return ONLY the JSON object, no markdown fencing.\n\n"
            f"Interaction:\n{digest}"
        )

        # Use ACP to generate the skill
        if not _st.acp_client or not _st.acp_client.alive:
            self._json_response(503, {"error": {"message": "ACP not available for skill extraction"}})
            return

        try:
            result = _st.acp_client.send_prompt([
                {"role": "system", "content": "You extract reusable skills from successful interactions. Output only valid JSON."},
                {"role": "user", "content": extract_prompt}
            ])
            # Parse the result as JSON
            result_text = str(result or "").strip()
            # Strip markdown fencing if present
            if result_text.startswith("```"):
                result_text = re.sub(r"^```(?:json)?\s*", "", result_text)
                result_text = re.sub(r"\s*```$", "", result_text)
            draft = json.loads(result_text)
            draft["Source"] = "auto-learned"
            draft["Status"] = "draft"
            self._json_response(200, {"draft": draft})
        except json.JSONDecodeError:
            self._json_response(200, {"draft": None, "raw": result_text[:1000], "error": "model output was not valid JSON"})
        except Exception as e:
            self._json_response(502, {"error": {"message": f"skill extraction failed: {e}"}})

    # ------------------------------------------------------------------
    # Subagent parallelism
    # ------------------------------------------------------------------
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
        if not prompt:
            self._json_response(400, {"error": {"message": "prompt is required"}})
            return
        with _st.subagent_lock:
            running = sum(1 for t in _st.subagent_tasks.values() if t.get("status") == "running")
            if running >= _SUBAGENT_MAX:
                self._json_response(429, {"error": {"message": f"max {_SUBAGENT_MAX} concurrent subagents"}})
                return
        task_id = "sub-" + uuid.uuid4().hex[:8]
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        task = {
            "id": task_id,
            "label": label,
            "prompt": prompt[:500],
            "status": "running",
            "result": None,
            "started_at": now_iso,
            "ended_at": None,
        }
        with _st.subagent_lock:
            _st.subagent_tasks[task_id] = task
        thread = threading.Thread(target=_subagent_worker, args=(task_id, prompt, label), name=f"subagent-{task_id}", daemon=True)
        thread.start()
        self._json_response(202, {"task": {k: v for k, v in task.items() if k != "thread"}})

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
                self._json_response(200, {"task": {k: v for k, v in task.items() if k != "thread"}})
            else:
                tasks = [{k: v for k, v in t.items() if k != "thread"} for t in _st.subagent_tasks.values()]
                running = sum(1 for t in tasks if t.get("status") == "running")
                self._json_response(200, {"tasks": tasks[-20:], "running": running, "max": _SUBAGENT_MAX})

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
            "summary": _telemetry_summarize(events),
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

    def _mcp_status(self):
        """Return current MCP server configuration status."""
        config = _st.acp_client.mcp_config if _st.acp_client else {}
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
            "active": list(config.keys()) if config else [],
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

    def _aig_chat(self):
        """AIG orchestrator — intelligently routes to the best model for each task."""
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
        user_message = data.get("user_message", "")
        internal = bool(data.get("internal"))
        # Cognition draft/revise stages are internal but still want memory recall.
        # They pass the raw user turn so _build_memory_context runs on the real
        # message instead of the wrapped task prompt.
        inject_memory = bool(data.get("inject_memory"))
        recall_query = (data.get("recall_query") or "").strip()
        # Tool-free mode: the cognition reviewer is a text-only judge. It already
        # has the draft and the user message, so it must NOT re-run web/Kusto/MCP
        # tools (that duplicated the draft's retrieval and doubled latency).
        no_tools = bool(data.get("no_tools"))
        model_for_response = data.get("model", "claude-opus-4.8")  # frontend-selectable, default claude-opus-4.8
        _set_openai_key_from(data)  # cache key for semantic recall (incl. background threads)

        if not user_message and messages:
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    user_message = msg.get("content", "")
                    break

        if not user_message:
            self._json_response(400, {"error": {"message": "No user message provided"}})
            return
        _mark_user_activity()
        _turn_t0 = time.perf_counter()

        print(f"[AIG] Processing: {user_message[:80]}...")

        # Step 1: Build memory context + proactive data retrieval
        # Skip for internal calls (cognition sub-calls already have context)
        if internal:
            # Cognition draft/revise stages opt in to recall via recall_query so
            # the cognitive layer (default ON) does not bypass persistent memory.
            if inject_memory and recall_query and _st.cognition_enabled:
                memory_context = _build_memory_context(recall_query)
                if memory_context:
                    print(f"[AIG] Internal call: injected {len(memory_context)} chars of memory context (recall)")
                else:
                    print("[AIG] Internal call: recall requested but no memory context produced")
            else:
                memory_context = ""
                print("[AIG] Internal call: skipping memory injection")
        else:
            memory_context = _build_memory_context(user_message) if _st.cognition_enabled else ""
            if memory_context:
                print(f"[AIG] Injected {len(memory_context)} chars of memory context")

        # Step 2: ACP-first routing — ACP is the default path (it has MCP tools).
        # Skip ACP data retrieval for internal calls (cognition sub-calls)
        # and for trivial conversational messages with high confidence.
        import re as _re
        msg_lower = user_message.lower()
        msg_stripped = _re.sub(r'[^\w\s]', '', msg_lower).strip()
        msg_words = msg_stripped.split()

        skip_acp = False
        _acp_route = "default"

        # retrieve_data: cognition draft calls set this to opt in to data
        # retrieval even though they are internal. Without it the draft model
        # never sees [Data Retrieved] and fabricates everything.
        force_retrieve = bool(data.get("retrieve_data"))
        if internal and not force_retrieve:
            skip_acp = True
            _acp_route = "internal-cognition"
        elif not _st.acp_client and not _st.local_mode:
            skip_acp = True
            _acp_route = "acp-unavailable"
        elif len(msg_words) <= 4 and _re.match(
            r'^(hi|hey|hello|howdy|yo|sup|good morning|good evening|good afternoon|thanks|thank you|ok|okay|bye|goodbye|see you|great|cool|nice|sure|yes|no|nah|yep|nope)\b',
            msg_stripped
        ):
            skip_acp = True
            _acp_route = "greeting/trivial"
        elif len(msg_words) <= 6 and _re.match(
            r'^(how are you|how do you feel|what is your name|who are you|what can you do|tell me about yourself)\b',
            msg_stripped
        ):
            skip_acp = True
            _acp_route = "meta-question"

        # Classify the request type for logging and prompt tuning
        _request_type = _classify_request_type(msg_lower)

        needs_acp_tools = not skip_acp
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
        if needs_acp_tools:
            print(f"[AIG] Step 2: Using ACP ({_request_type})...")
            # Ensure ACP is alive before attempting tool calls.
            # The CLI may have died between requests (idle timeout, crash).
            if not _st.acp_client.alive:
                ok, _ = _ensure_acp_model(_st.acp_client.model or "")
                if not ok:
                    needs_acp_tools = False
                    print("[AIG] ACP restart failed, skipping data retrieval")
        if needs_acp_tools:
            # Use ACP to run the data query (it has MCP tools)
            if raw_output_requested:
                acp_prompt = (
                    "You are a strict Kusto query executor. "
                    "Execute the appropriate Kusto MCP tool for the user request and return ONLY the final tool output text. "
                    "Do not add headings, markdown, explanations, or invented rows.\n\n"
                    f"{user_message}"
                )
            elif _request_type in ("news-search", "weather-search", "financial-data", "web-search"):
                acp_prompt = (
                    "You are a research assistant with web search tools. "
                    "Use your available tools to search the web and find REAL, CURRENT information for the user's request. "
                    "Return factual results with sources. Do NOT invent or guess information. "
                    "If no tools return results, say 'No results found' — do NOT fabricate data.\n\n"
                    f"{user_message}"
                )
            elif _request_type in ("kusto-query", "kusto-operator"):
                acp_prompt = (
                    "You are a data retrieval assistant. Execute the appropriate Kusto MCP tool to answer this request. "
                    "Return ONLY the raw data results, no commentary:\n\n"
                    f"{user_message}"
                )
            else:
                # General request — let ACP use whatever tools it deems appropriate
                acp_prompt = (
                    "You are an assistant with access to web search, Kusto databases, GitHub, and Azure tools. "
                    "Answer the user's question using your available tools if they would help. "
                    "If no tools are needed, answer directly. Be factual and concise.\n"
                    f"If asked to create a file (PDF, CSV, etc.), write it to {_ARTIFACTS_DIR}/ using a short descriptive filename. "
                    "Return ONLY the filename (no path, no blob URLs) so the system can serve it.\n\n"
                    f"{user_message}"
                )
            # Continuous learning: while MCP tools are active, persist durable user facts.
            # Skipped in raw mode so strict query output is not polluted.
            if not raw_output_requested:
                acp_prompt += _MEMORY_CAPTURE_DIRECTIVE
            acp_result = _st.acp_client.prompt(acp_prompt, timeout=90)
            if acp_result and "text" in acp_result and acp_result["text"]:
                acp_data = acp_result["text"]
                acp_model_used = _st.acp_client.model or "copilot-acp"
                print(f"[AIG] ACP returned {len(acp_data)} chars of data")

        # Step 3: Build the final prompt for Eva's persona model (PAT)
        eva_system = (
            "You are Eva, an AI assistant with persistent memory and active tool access. "
            "You have skills for live data retrieval (stocks, weather, news, markets), "
            "web search, image generation, and a Kusto persistent memory database. "
            "Use the context below naturally as your own knowledge.\n\n"
            "CRITICAL RULES:\n"
            "- NEVER fabricate news headlines, stock prices, weather forecasts, or current events.\n"
            "- NEVER pretend to 'fetch' or 'search' for data — you either have it in [Data Retrieved] below, or you don't.\n"
            "- If [Data Retrieved] is present, use it as your authoritative source.\n"
            "- If NO [Data Retrieved] section exists for a real-time question (news, stocks, weather), "
            "honestly say you could not retrieve that information right now.\n"
            "- Do NOT generate fake source citations (AP, Reuters, etc.) unless they appear in [Data Retrieved].\n"
            "- When asked about your base model, underlying model, model ID, or what powers you, "
            "answer using the [Runtime] section below. Do NOT guess or invent a model name.\n"
            "- When the user asks to show, find, or generate an image, do NOT call the web fetch tool to look up image URLs. Instead emit a placeholder of the form [Image of <short description>] on its own line. The browser resolves the placeholder by calling gpt-image-1 (if the user asked to generate) or Wikimedia (if the user asked to find or show). Do not invent image URLs. Do not say you cannot show or generate images. Up to 3 placeholders per response are supported.\n"
            "- If asked to produce a downloadable file (PDF, CSV, image, etc.), write it to the directory in environment variable EVA_ARTIFACTS_DIR using a short descriptive filename. After the file is written, end your message with a single line containing exactly: [[EVA_FILE]] <filename.ext>. Do not claim a file was produced unless you actually wrote it. Do not include the EVA_FILE marker if no file exists.\n\n"
            "BROWSER CONTROL:\n"
            "- You CAN control a real web browser through the Playwright tools available in this session "
            "(navigate to URLs, click elements, type text, read page snapshots). The browser opens in a "
            "separate Chromium window on the user's machine.\n"
            "- When the user asks you to open a site, play a playlist, look something up on a specific page, "
            "fill a form, or add an item to a cart, USE the Playwright browser tools to actually do it. "
            "Do NOT reply that you 'cannot open websites or apps' — that is false; you can.\n"
            "- HONESTY: only state that an action happened AFTER the corresponding browser tool has actually "
            "run and returned. If a browser tool is unavailable or fails, say so plainly and offer a clickable "
            "link instead. Never narrate a click, navigation, or purchase you did not actually perform.\n"
            "- For purchases, account changes, or other irreversible/sensitive actions, stop at the final "
            "confirmation step and ask the user to confirm before completing it.\n"
            "- VISUAL BROWSER AGENT: for a supervised, multi-step visual task (add an item to a cart, fill "
            "a multi-page form, navigate a flow that needs to be watched), you may launch Eva's own vision "
            "browser agent by emitting a single line of the form: "
            "[[EVA_BROWSER]]{\"goal\":\"<plain-language task>\",\"start_url\":\"<optional url>\"}[[/EVA_BROWSER]]. "
            "It drives a real Chrome using a persistent profile, so sites you logged into once (e.g. Amazon) "
            "stay signed in. It auto-approves browsing, searching, adding to cart, and sign-in, and pauses "
            "ONLY at the final purchase commit: at that point it asks you in chat/voice to confirm, and you "
            "reply yes or no. Use this when the user wants to watch the work happen; use the direct Playwright "
            "tools above for quick one-off navigations. Emit at most one EVA_BROWSER block per reply, and only "
            "when the user actually asked for a browser task.\n"
            "- DESKTOP CONTROL: you can also operate the user's whole desktop by sight, including launching "
            "applications (e.g. GIMP, a file manager, an editor). For a supervised desktop task, emit a single "
            "line of the form: [[EVA_DESKTOP]]{\"goal\":\"<plain-language task, naming the app if relevant>\"}[[/EVA_DESKTOP]]. "
            "A floating window opens, a vision model sees the screen and launches/clicks/types via the real "
            "mouse and keyboard. It opens apps automatically and only pauses for your approval before a "
            "genuinely destructive action. Use this for genuine desktop tasks (\"open GIMP and create a picture\"), not for things a "
            "browser or a direct answer handles better. Emit at most one EVA_DESKTOP block per reply, and only "
            "when the user actually asked to do something on the desktop. Do NOT say you cannot open or control "
            "desktop applications.\n"
            "- USE THE EXISTING BROWSER: for reliable web tasks (shopping, add to cart, fill a form), prefer "
            "the EVA_BROWSER agent: it controls the page through the DOM so its clicks are precise, and it uses "
            "a persistent Chrome profile, so after the user signs in once it stays logged in across runs. Only "
            "when the user specifically insists on their CURRENTLY-open browser window use the DESKTOP agent "
            "[[EVA_DESKTOP]] with a goal telling it to focus that Chrome window and open a new tab; that drives "
            "the real cursor by sight, so it is less precise.\n"
            "- ACT, DON'T EXPLAIN: when the user asks you to DO an actionable task (open or operate an app, run "
            "a browser flow), act on the FIRST request by emitting the appropriate marker. Do NOT instead list "
            "the manual steps for the user to follow, and do NOT wait to be told 'do it yourself' — describing "
            "the steps instead of doing it is a failure. Before the marker, write ONE short present/future-tense "
            "sentence announcing what you are about to do (\"I'm opening GIMP and starting a new canvas now.\"), "
            "not a past-tense report after the fact. The agent then carries out the task.\n"
            "- CAMERA / EYES: you can SEE through the user's webcam. When the user asks what you see, to look, "
            "or to describe something in front of the camera, emit a single line of the form: "
            "[[EVA_LOOK]]{\"question\":\"<what to look for>\"}[[/EVA_LOOK]] (the question is optional). A frame "
            "is captured locally and you describe it. Do NOT say you cannot see or use a camera. Emit at most "
            "one EVA_LOOK per reply, only when the user asks you to look or about what you can see.\n"
            "- SCREENSHOTS vs CAMERA: 'screenshot', 'capture my screen', or 'what's on my desktop' means the "
            "user wants to see their MONITOR — use [[EVA_DESKTOP]] with a goal like 'take a screenshot and "
            "describe what is on screen'. 'Take a picture', 'what am I holding', or 'look at me' means the "
            "user wants the WEBCAM — use [[EVA_LOOK]]. NEVER confuse these two.\n\n"
        )

        if no_tools:
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

        if model_for_response == "lmstudio":
            lms_base = (data.get("lmstudio_base_url") or "").strip()
            lms_model = (data.get("lmstudio_model") or "").strip()
            if not lms_base:
                lms_base = "http://localhost:1234/v1"
            if not lms_model:
                lms_model = "granite-3.1-8b-instruct"

            lms_base, lms_error = _validate_lmstudio_base_url(lms_base)
            if lms_error:
                self._json_response(400, {"error": {"message": lms_error}})
                return

            eva_system_full = eva_system

            lms_messages = [{"role": "system", "content": eva_system_full}]
            for msg in messages[-6:]:
                if msg.get("role") and msg.get("content"):
                    lms_messages.append({"role": msg["role"], "content": msg["content"]})
            lms_messages.append({"role": "user", "content": user_message})

            try:
                import requests as _req
                lms_resp = _req.post(
                    lms_base + "/chat/completions",
                    json={"model": lms_model, "messages": lms_messages, "temperature": 0.7},
                    timeout=180,
                )
                if lms_resp.status_code == 200:
                    lms_body = lms_resp.json()
                    response_text = (lms_body.get("choices") or [{}])[0].get("message", {}).get("content", "")
                    model_used = "aig:lmstudio:" + lms_model
                else:
                    print(f"[AIG] LM Studio HTTP error: {lms_resp.status_code}")
                    self._json_response(502, {"error": {"message": f"LM Studio returned HTTP {lms_resp.status_code}"}})
                    return
            except Exception as _lms_err:
                print(f"[AIG] LM Studio request failed: {_lms_err}")
                self._json_response(504, {"error": {"message": f"LM Studio request failed: {_lms_err}"}})
                return

            print(f"[AIG] LM Studio response: {len(response_text)} chars from {lms_model}")
            # Log the first 500 chars of the response for debugging
            print(f"[AIG] LM Studio content: {response_text[:500]}")

            # Post-process: convert blob/download links to [[EVA_FILE]] markers.
            # ACP or the model may produce blob:file:/// URLs or markdown download links
            # referencing sandbox files. These are not accessible in Electron, so we
            # extract the filename and emit the proper marker instead.
            response_text = _re.sub(
                r'\[(?:Download|Open)\s+([A-Za-z0-9._-]{1,128})\]\(blob:[^)]+\)'
                r'(?:\s*\[(?:Download|Open)\s+[A-Za-z0-9._-]{1,128}\]\(blob:[^)]+\))*'
                r'(?:\s*\([^)]*\))?',
                lambda m: '\n[[EVA_FILE]] ' + m.group(1),
                response_text
            )

            if response_text and _st.cognition_enabled and not internal:
                threading.Thread(target=_post_response_reflection,
                                 args=(user_message, response_text, model_used),
                                 daemon=True).start()

            response = {
                "id": f"aig-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model_used,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": response_text},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }
            self._json_response(200, response)
            return

        # Step 4: Pick the best PAT model for response generation
        # Priority: request body PAT > env var > Copilot CLI OAuth token > ACP fallback
        github_pat = data.get("github_pat", "") or os.environ.get("GITHUB_PAT", "")

        # Fallback: read Copilot CLI's OAuth token (works with GitHub Models API — OpenAI models only)
        _using_oauth_token = False
        if not github_pat:
            try:
                oauth_path = os.path.expanduser("~/.config/github-copilot/oauth.json")
                if os.path.isfile(oauth_path):
                    with open(oauth_path) as _f:
                        _oauth = json.load(_f)
                    entries = _oauth.get("https://github.com/login/oauth", [])
                    if entries and isinstance(entries, list) and entries[0].get("accessToken"):
                        github_pat = entries[0]["accessToken"]
                        _using_oauth_token = True
                        print("[AIG] Using Copilot CLI OAuth token for GitHub Models API")
            except Exception as _e:
                print(f"[AIG] Could not read Copilot OAuth: {_e}")

        # Models available on GitHub Models API (PAT).
        # Models absent from this map must route through ACP.
        # See: https://github.com/marketplace/models/catalog
        # API endpoint: https://models.github.ai/inference/chat/completions
        # Model names use publisher/model format.
        _github_model_map = {
            "gpt-4.1": "openai/gpt-4.1",
            "gpt-4o": "openai/gpt-4o",
            "gpt-4o-mini": "openai/gpt-4o-mini",
            "gpt-5": "openai/gpt-5",
            "gpt-5-mini": "openai/gpt-5-mini",
            "gpt-5-nano": "openai/gpt-5-nano",
            "gpt-5-chat": "openai/gpt-5-chat",
            "o3-mini": "openai/o3-mini",
            "o3": "openai/o3",
            "o4-mini": "openai/o4-mini",
            "deepseek-r1": "deepseek/DeepSeek-R1",
            "llama-4-maverick": "meta/llama-4-maverick-17b-128e-instruct-fp8",
        }
        # Any selector model not listed in _github_model_map routes
        # through ACP. This covers Claude, Gemini, and unmapped GPT
        # variants (e.g. gpt-5.5, gpt-5.3-codex) that Copilot CLI serves.

        api_model = _github_model_map.get(model_for_response, model_for_response)
        acp_response_model = ""
        if model_for_response == "acp":
            acp_response_model = ""
        elif model_for_response not in _github_model_map:
            acp_response_model = model_for_response

        print(f"[AIG] Model requested: {model_for_response}, API model: {api_model}, PAT present: {bool(github_pat)} ({len(github_pat)} chars)")
        response_text = ""
        model_used = "aig"

        if raw_output_requested and acp_data:
            active_raw_model = acp_model_used or (_st.acp_client.model if _st.acp_client else "copilot-acp")
            response_text = acp_data
            model_used = f"aig:{active_raw_model}+raw-acp"
            github_pat = ""
            print("[AIG] Raw-output mode: returning ACP tool output directly")
        elif row_recall_requested and acp_data:
            active_data_model = acp_model_used or (_st.acp_client.model if _st.acp_client else "copilot-acp")
            response_text = acp_data
            model_used = f"aig:{active_data_model}+acp-data"
            github_pat = ""
            print("[AIG] Row-recall mode: returning ACP tool output directly")
        elif raw_output_requested and needs_acp_tools and not acp_data:
            response_text = "Raw query mode requested but no tool output was returned. Retry with explicit KQL."
            model_used = "aig:raw-acp-unavailable"
            github_pat = ""
            print("[AIG] Raw-output mode: no ACP data available")

        if model_for_response == "acp":
            # Explicit ACP routing — skip PAT entirely
            github_pat = ""

        # When cognition is active, ACP is the primary path (not a fallback).
        # This avoids PAT round-trips and keeps model routing through Copilot CLI.
        # Note: _st.cognition_enabled is only set at startup when Kusto MCP + token
        # are confirmed, so ACP availability is guaranteed at that point.
        # The alive check is deferred to the actual ACP prompt call.
        if _st.cognition_enabled and _st.acp_client:
            if model_for_response not in ("lmstudio",):
                github_pat = ""
                acp_response_model = model_for_response if model_for_response != "acp" else ""
                print(f"[AIG] Cognition active: routing directly to ACP")

        # Non-mapped models are not on GitHub Models API and must go through ACP.
        elif model_for_response != "acp" and model_for_response not in _github_model_map:
            print(f"[AIG] {model_for_response} not on GitHub Models API, routing to ACP")
            github_pat = ""

        # Inject runtime info so Eva can answer truthfully when asked about her model.
        # Decided after routing fall-throughs above so it reflects the path that will run.
        if github_pat:
            _route_label = "GitHub Models API (PAT)" if not _using_oauth_token else "GitHub Models API (Copilot OAuth)"
            _runtime_model = model_for_response
        else:
            _route_label = "Copilot CLI ACP bridge"
            _runtime_model = acp_response_model or (_st.acp_client.model if _st.acp_client else "") or "default"
        eva_system += (
            f"\n[Runtime - AUTHORITATIVE GROUND TRUTH]\n"
            f"This block is injected by tools/acp_bridge.py. It overrides any model self-knowledge.\n"
            f"User-selected backend: {model_for_response}\n"
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

        if github_pat:
            # Use GitHub Models API (PAT) for persona-friendly response
            print(f"[AIG] Step 3: Generating response via PAT model ({api_model})...")
            try:
                import requests as _req
                pat_messages = [{"role": "system", "content": eva_system}]
                # Add recent conversation context (last few messages)
                for msg in messages[-6:]:
                    if msg.get("role") in ("user", "assistant"):
                        pat_messages.append({"role": msg["role"], "content": msg.get("content", "")[:500]})
                # Always ensure the current user message is the last message
                if not pat_messages or pat_messages[-1].get("content") != user_message:
                    pat_messages.append({"role": "user", "content": user_message})

                pat_resp = _req.post("https://models.github.ai/inference/chat/completions",
                    headers={
                        "Authorization": f"Bearer {github_pat}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": api_model,
                        "messages": pat_messages,
                        "max_tokens": 4096
                    },
                    timeout=60
                )
                if pat_resp.status_code == 200:
                    pat_data = pat_resp.json()
                    response_text = pat_data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    model_used = f"aig:{model_for_response}"
                    if acp_model_used:
                        model_used += f"+{acp_model_used}"

                    # If PAT produces a planning/deferral narrative despite ACP data,
                    # prefer the already-retrieved tool output to avoid hallucinated recall text.
                    if acp_data and needs_acp_tools:
                        pat_lower = (response_text or "").lower()
                        deferral_markers = [
                            "if you'd like",
                            "i can run this query",
                            "i can run the query",
                            "once results are available",
                            "i will execute",
                            "please confirm",
                            "preloaded data",
                            "no explicit",
                        ]
                        if any(m in pat_lower for m in deferral_markers):
                            active_data_model = acp_model_used or (_st.acp_client.model if _st.acp_client else "copilot-acp")
                            response_text = acp_data
                            model_used = f"aig:{active_data_model}+acp-data"
                            print("[AIG] PAT response deferred despite ACP data; returning ACP data directly")

                    print(f"[AIG] PAT response: {len(response_text)} chars")
                else:
                    err_body = pat_resp.text[:500] if pat_resp.text else "(empty)"
                    print(f"[AIG] PAT model failed ({pat_resp.status_code}): {err_body}")
                    print(f"[AIG] Falling back to ACP")
                    github_pat = ""  # trigger ACP fallback
            except Exception as e:
                print(f"[AIG] PAT error: {e}, falling back to ACP")
                github_pat = ""

        if not response_text:
            # ACP response generation — primary path when cognition is active,
            # fallback path when PAT is unavailable or failed.
            print(f"[AIG] Using ACP for response generation...")
            if _st.acp_client:
                switched, switch_info = _ensure_acp_model(acp_response_model)
                if not switched:
                    response_text = f"ACP model switch failed: {switch_info}"
                    model_used = "aig:unavailable"
                else:
                    # Include conversation history so follow-up messages have context
                    history_lines = []
                    for msg in messages[-6:]:
                        if msg.get("role") in ("user", "assistant"):
                            role_label = "User" if msg["role"] == "user" else "Eva"
                            history_lines.append(f"{role_label}: {msg.get('content', '')[:500]}")
                    if history_lines:
                        full_prompt = eva_system + "\n\n[Conversation]\n" + "\n\n".join(history_lines)
                        # Append current message if not already the last in history
                        last_hist = history_lines[-1] if history_lines else ""
                        if not last_hist.startswith("User: " + user_message[:50]):
                            full_prompt += "\n\nUser: " + user_message
                    else:
                        full_prompt = eva_system + "\n\nUser: " + user_message
                    acp_result = _st.acp_client.prompt(full_prompt, timeout=120)
                    response_text = acp_result.get("text", "I'm having trouble processing that right now.")
                    active_model = _st.acp_client.model or "acp-default"
                    model_used = f"aig:{active_model}"
                    if acp_model_used and acp_model_used != active_model:
                        model_used += f"+{acp_model_used}"
            else:
                response_text = "The AIG system needs either a GitHub PAT or a running ACP bridge to generate responses."
                model_used = "aig:unavailable"

        # Step 5: Post-response reflection (background)
        if response_text and _st.cognition_enabled and not internal:
            threading.Thread(target=_post_response_reflection,
                           args=(user_message, response_text, model_used),
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
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }
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
    def _retrieve_acp_data_for(user_message):
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

        # --- Local mode: use local MCP + LM Studio for tool-calling ---
        if _st.local_mode:
            return BridgeHandler._retrieve_local_data(user_message)

        # --- Cloud mode: use ACP (Copilot CLI) ---
        if not _st.acp_client:
            return "", ""

        # Ensure ACP is alive
        if not _st.acp_client.alive:
            ok, _ = _ensure_acp_model(_st.acp_client.model or "")
            if not ok:
                print("[DataRetrieve] ACP restart failed")
                return "", ""

        _request_type = _classify_request_type(msg_lower)
        print(f"[DataRetrieve] ACP query ({_request_type}): {user_message[:80]}")

        if _request_type in ("news-search", "weather-search", "financial-data", "web-search"):
            acp_prompt = (
                "You are a research assistant with web search tools. "
                "Use your available tools to search the web and find REAL, CURRENT information for the user's request. "
                "Return factual results with sources. Do NOT invent or guess information. "
                "If no tools return results, say 'No results found' — do NOT fabricate data.\n\n"
                f"{user_message}"
            )
        elif _request_type in ("kusto-query", "kusto-operator"):
            acp_prompt = (
                "You are a data retrieval assistant. Execute the appropriate Kusto MCP tool to answer this request. "
                "Return ONLY the raw data results, no commentary:\n\n"
                f"{user_message}"
            )
        else:
            acp_prompt = (
                "You are an assistant with access to web search, Kusto databases, GitHub, and Azure tools. "
                "Answer the user's question using your available tools if they would help. "
                "If no tools are needed, answer directly. Be factual and concise.\n"
                f"If asked to create a file (PDF, CSV, etc.), write it to {_ARTIFACTS_DIR}/ using a short descriptive filename. "
                "Return ONLY the filename (no path, no blob URLs) so the system can serve it.\n\n"
                f"{user_message}"
            )
        acp_prompt += _MEMORY_CAPTURE_DIRECTIVE

        try:
            acp_result = _st.acp_client.prompt(acp_prompt, timeout=90)
        except Exception as e:
            print(f"[DataRetrieve] ACP error: {e}")
            return "", ""

        if acp_result and "text" in acp_result and acp_result["text"]:
            data = acp_result["text"]
            # Strip blob URLs
            data = _re.sub(r'blob:file:///[a-f0-9-]+', '', data)
            model = _st.acp_client.model or "copilot-acp"
            print(f"[DataRetrieve] ACP returned {len(data)} chars")
            return data, model
        return "", ""

    @staticmethod
    def _retrieve_local_data(user_message):
        """Run data retrieval via local MCP servers + LM Studio tool-calling."""
        if not _st.local_mcp_manager or not _st.local_mcp_manager.alive:
            print("[DataRetrieve] Local mode: no MCP servers running")
            return "", ""
        try:
            from bridge.local_mcp import local_agent_query
        except ImportError as e:
            print(f"[DataRetrieve] Local mode import error: {e}")
            return "", ""
        # Get LM Studio URL/model from client prefs or defaults
        prefs = _load_client_prefs()
        lms_base = prefs.get("lmstudio_base_url", "http://localhost:1234/v1")
        lms_model = prefs.get("lmstudio_model", "")
        print(f"[DataRetrieve] Local mode query: {user_message[:80]}")
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
        if not user_message:
            self._json_response(200, {"data": "", "model": "", "retrieved": False, "mode": "local" if _st.local_mode else "cloud"})
            return
        data, model = self._retrieve_acp_data_for(user_message)
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
        if user_message:
            _mark_user_activity()

        context = _build_memory_context(user_message)
        self._json_response(200, {
            "context": context,
            "cognition_enabled": True
        })

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
        if user_msg:
            _mark_user_activity()

        if user_msg and assistant_msg:
            threading.Thread(target=_post_response_reflection,
                           args=(user_msg, assistant_msg, model),
                           daemon=True).start()

        self._json_response(200, {"status": "ok"})

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
        mcp_config = getattr(_st.acp_client, "mcp_config", {}) if _st.acp_client is not None else {}
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

        mcp_servers = data.get("mcp_servers", {})

        # Persist the raw selection (secrets stripped) so it survives bridge
        # restarts even if the Electron file:// localStorage is cleared.
        _persist_mcp_config(mcp_servers)

        # Resolve internal flags in MCP server env before passing to copilot
        for srv_name, srv_cfg in mcp_servers.items():
            env = srv_cfg.get('env', {})
            resolved_env = {}
            for k, v in env.items():
                # _useGitHubPAT: resolve to actual PAT from process environment
                if k == '_useGitHubPAT':
                    pat = os.environ.get('GITHUB_PERSONAL_ACCESS_TOKEN', '')
                    if pat:
                        resolved_env['GITHUB_PERSONAL_ACCESS_TOKEN'] = pat
                    continue
                # Skip any other internal flags (prefixed with _)
                if k.startswith('_'):
                    continue
                # Ensure all env values are strings (subprocess.Popen requirement)
                resolved_env[k] = str(v) if not isinstance(v, str) else v
            srv_cfg['env'] = resolved_env

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

        # Restart ACP client with new MCP config
        old_path = _st.acp_client.copilot_path if _st.acp_client else "copilot"
        old_cwd = _st.acp_client.cwd if _st.acp_client else os.getcwd()
        old_model = _st.acp_client.model if _st.acp_client else None
        if _st.acp_client:
            _st.acp_client.stop()

        _st.acp_client = ACPClient(copilot_path=old_path, cwd=old_cwd, model=old_model, mcp_config=mcp_servers)
        try:
            _st.acp_client.start()
            # MCP config changed: drop stale warm clients so the pool only holds
            # clients built with the new server set.
            _reset_acp_pool(_st.acp_client)
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
                "active_servers": list(mcp_servers.keys())
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
        switched, switch_info = _ensure_acp_model(requested_model)
        if not switched:
            self._json_response(503, {"error": {"message": switch_info}})
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

        memory_context = _build_memory_context(last_user_msg)
        if memory_context:
            prompt_text = memory_context + prompt_text
            print(f"[Cognition] Injected {len(memory_context)} chars of memory context")

        # Send to ACP
        result = _st.acp_client.prompt(prompt_text, timeout=180)

        if "error" in result:
            error_detail = result["error"]
            if isinstance(error_detail, dict):
                error_msg = error_detail.get("message", str(error_detail))
            else:
                error_msg = str(error_detail)
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
                "finish_reason": "stop" if result.get("stop_reason") == "end_turn" else result.get("stop_reason", "stop")
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }
        self._json_response(200, response)

        # --- Cognition: Post-response reflection (background) ---
        response_text = result.get("text", "")
        model_label = f"copilot-acp:{requested_model}" if requested_model else "copilot-acp"
        if last_user_msg and response_text:
            threading.Thread(target=_post_response_reflection,
                           args=(last_user_msg, response_text, model_label),
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
        self._json_response(200, _save_client_prefs(data))

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
                    if _st.acp_client and _st.acp_client.mcp_config:
                        mcp_config = dict(_st.acp_client.mcp_config)
                    if not mcp_config:
                        mcp_config = _load_persisted_mcp_config()
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

    # global statement removed — writes go to _st.*
    print(f"[Bridge] Starting ACP bridge on port {args.port}...")
    print(f"[Bridge] Copilot CLI: {args.copilot_path}")
    print(f"[Bridge] Working directory: {args.cwd}")
    if mcp_config:
        print(f"[Bridge] MCP Servers: {', '.join(mcp_config.keys())}")

    # Start ACP client
    _st.acp_client = ACPClient(copilot_path=args.copilot_path, cwd=args.cwd, model=args.model, mcp_config=mcp_config)
    try:
        _st.acp_client.start()
    except RuntimeError as e:
        print(f"[Bridge] ERROR: {e}")
        sys.exit(1)

    # Enable cognition layer if memory backend is available
    # global statement removed — writes go to _st.*
    _startup_backend = _resolve_memory_backend()
    if _startup_backend == "sqlite":
        _enable_cognition(mcp_config, model=args.model, port=args.port)
    elif "kusto-mcp-server" in mcp_config and _st.kusto_token_cache:
        _enable_cognition(mcp_config, model=args.model, port=args.port)
    else:
        print(f"[Bridge] Cognition layer disabled (no Kusto MCP or token, and backend is not sqlite)")

    # Start HTTP server. Threaded so a long-running browser agent run does not
    # block status/cancel/confirm polling on other connections.
    server = ThreadingHTTPServer((args.bind, args.port), BridgeHandler)
    print(f"[Bridge] Listening on http://{args.bind}:{args.port}")
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
