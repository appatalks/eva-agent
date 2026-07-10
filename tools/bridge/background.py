"""Bridge domain: background."""

import datetime
import hashlib
import json
import os
import re
import threading
import time
import uuid
from bridge import config as _cfg
from bridge import state as _st
from bridge.kusto import (_kusto_query_direct, _kusto_ingest_direct,
    _get_kusto_config, _ensure_kusto_token, _get_table_columns,
    _canonical_kusto_ingest_string)
from bridge.memory import (_memory_query, _memory_ingest,
    _get_sqlite_mem, _resolve_memory_backend)
from bridge.cognition import _enable_cognition
from bridge.normalization import latest_row_sql
from bridge.sensitive import redact_credentials
from bridge.cron import _load_cron_tasks, _cron_tick
from bridge.alerts import (_load_alerts, _save_alerts, _alert_cooldown_elapsed,
    _alert_build_prompt, _alert_salience, _alert_clip, _notify_enqueue)

_BG_ACTIVITY_COLUMNS = _cfg.BG_ACTIVITY_COLUMNS
_BG_JOB_ALERT_WATCH = _cfg.BG_JOB_ALERT_WATCH
_BG_JOB_ADX_PROJECTION = _cfg.BG_JOB_ADX_PROJECTION
_BG_JOB_DAILY_DIGEST = _cfg.BG_JOB_DAILY_DIGEST
_BG_JOB_EMOTION_DRIFT = _cfg.BG_JOB_EMOTION_DRIFT
_BG_JOB_GOAL_CHECKIN = _cfg.BG_JOB_GOAL_CHECKIN
_BG_JOB_KNOWLEDGE_HYGIENE = _cfg.BG_JOB_KNOWLEDGE_HYGIENE
_BG_JOB_MARKET_SNAPSHOT = _cfg.BG_JOB_MARKET_SNAPSHOT
_BG_JOB_PROACTIVE_BRIEFING = _cfg.BG_JOB_PROACTIVE_BRIEFING
_BG_JOB_REFLECTION_SYNTHESIS = _cfg.BG_JOB_REFLECTION_SYNTHESIS
_BG_JOB_RESEARCH_DEEPDIVE = _cfg.BG_JOB_RESEARCH_DEEPDIVE
_BG_JOB_SEC_FILINGS = _cfg.BG_JOB_SEC_FILINGS
_BG_JOB_SPACE_WEATHER = _cfg.BG_JOB_SPACE_WEATHER
_BG_JOB_TOKEN_TELEMETRY = _cfg.BG_JOB_TOKEN_TELEMETRY
_BG_JOB_TYPE = _cfg.BG_JOB_TYPE
_BG_JOBS_ENABLED = {
    _BG_JOB_TYPE: True, _BG_JOB_GOAL_CHECKIN: True, _BG_JOB_DAILY_DIGEST: True,
    _BG_JOB_KNOWLEDGE_HYGIENE: True, _BG_JOB_REFLECTION_SYNTHESIS: True,
    _BG_JOB_EMOTION_DRIFT: True, _BG_JOB_TOKEN_TELEMETRY: True,
    _BG_JOB_PROACTIVE_BRIEFING: True, _BG_JOB_MARKET_SNAPSHOT: True,
    _BG_JOB_SEC_FILINGS: True, _BG_JOB_SPACE_WEATHER: True,
    _BG_JOB_RESEARCH_DEEPDIVE: True, _BG_JOB_ALERT_WATCH: True,
    _BG_JOB_ADX_PROJECTION: _cfg.env_truthy("EVA_ADX_PROJECTION"),
}
_BG_PROPOSALS_LATEST_QUERY = (
    "BackgroundProposals "
    "| extend _SortAt = coalesce(ReviewedAt, CreatedAt) "
    "| summarize arg_max(_SortAt, *) by ProposalId "
    "| project-away _SortAt"
)
_BG_PROPOSAL_COLUMNS = _cfg.BG_PROPOSAL_COLUMNS
_DEFAULT_ALERT_SETTINGS = _cfg.DEFAULT_ALERT_SETTINGS
_EMOTION_DRIFT_THRESHOLD = _cfg.EMOTION_DRIFT_THRESHOLD
_ENTITY_IGNORE_WORDS = _cfg.ENTITY_IGNORE_WORDS
_ENTITY_RESERVED_TERMS = _cfg.ENTITY_RESERVED_TERMS
_GOALS_LATEST_QUERY = _cfg.GOALS_LATEST_QUERY
_GOAL_CHECKIN_MAX = _cfg.GOAL_CHECKIN_MAX
_GOAL_STALE_DAYS = _cfg.GOAL_STALE_DAYS
_KNOWLEDGE_STALE_CONFIDENCE = _cfg.KNOWLEDGE_STALE_CONFIDENCE
_REFLECTION_SYNTH_MIN = _cfg.REFLECTION_SYNTH_MIN
_SEC_WATCH_SYMBOLS = _cfg.SEC_WATCH_SYMBOLS
_TICKER_STOPWORDS = {
    "SEC", "CEO", "CFO", "COO", "ETF", "USA", "USD", "API", "PLC", "LLC", "INC",
    "NYSE", "IPO", "EPS", "GDP", "FDA", "ESG", "AND", "THE", "FOR", "ESPP", "AI",
}

def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)



def _to_utc_iso(value):
    if isinstance(value, datetime.datetime):
        active_value = value
    else:
        active_value = _utc_now()
    if active_value.tzinfo is None:
        active_value = active_value.replace(tzinfo=datetime.timezone.utc)
    return active_value.astimezone(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _proposal_transition_iso():
    """Return a strictly increasing UTC key for proposal append versions."""
    with _st.proposal_transition_lock:
        now = _utc_now()
        prior = _st.proposal_last_transition_at
        if prior is not None and now <= prior:
            now = prior + datetime.timedelta(microseconds=1)
        _st.proposal_last_transition_at = now
        return now.astimezone(datetime.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")



def _parse_kusto_datetime(value):
    if isinstance(value, datetime.datetime):
        parsed_value = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        text = re.sub(r"(\.\d{6})\d+", r"\1", text)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed_value = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed_value.tzinfo is None:
        parsed_value = parsed_value.replace(tzinfo=datetime.timezone.utc)
    return parsed_value.astimezone(datetime.timezone.utc)



def _safe_kusto_string(value):
    # Escape for embedding inside a single-quoted KQL string literal. Backslash
    # must be escaped first, then quotes and control characters, so free-form
    # text (agent summaries, observations) with newlines does not produce a
    # malformed query. KQL parses these escapes back to the original value, so
    # equality filters still match.
    s = str(value or "")
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "\\'")
    s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return s



def _mark_user_activity():
    # global statement removed — writes go to _st.*
    _st.last_user_activity_ts = time.time()



def _background_status_dict():
    running = bool(_st.bg_loop_thread and _st.bg_loop_thread.is_alive())
    return {
        "enabled": _st.bg_loop_enabled,
        "intervalSeconds": _st.bg_loop_interval_seconds,
        "lastTick": _st.bg_last_tick_iso,
        "lastError": _st.bg_last_error,
        "lastActivity": _st.bg_last_activity,
        "running": running,
        "jobs": {job_type: bool(_BG_JOBS_ENABLED.get(job_type, True)) for job_type, _ in _BG_JOBS},
    }



def _background_kusto_context():
    cluster, database = _get_kusto_config()
    if not cluster or not database:
        return None, None, "Kusto cluster or database not configured for the bridge"
    token_ok, token_error = _ensure_kusto_token()
    if not token_ok:
        message = "Kusto token unavailable"
        clean_error = " ".join(str(token_error or "").split())[:160]
        if clean_error:
            message += ": " + clean_error
        return None, None, message
    return cluster, database, ""



def _set_background_activity(row, error_text=""):
    # global statement removed — writes go to _st.*
    _st.bg_last_activity = dict(row or {})
    if row and row.get("StartedAt"):
        _st.bg_last_tick_iso = row.get("StartedAt")
    _st.bg_last_error = error_text or ""



def _record_background_activity(cluster, database, tick_id, started_at, ended_at, status, proposal_count, token_estimate, notes, job_type=_BG_JOB_TYPE):
    row = {
        "TickId": tick_id,
        "StartedAt": _to_utc_iso(started_at),
        "EndedAt": _to_utc_iso(ended_at),
        "JobType": job_type,
        "Status": status,
        "ProposalCount": int(proposal_count or 0),
        "TokenEstimate": int(token_estimate or 0),
        "Notes": str(notes or "")[:500],
    }
    wrote = False
    backend = _resolve_memory_backend()
    try:
        from bridge.finalize import mutate_event
        mem = _get_sqlite_mem()
        mutate_event(
            mem, mem.event_repository(), stream_id=f"background:activity:{tick_id}",
            event_type="background.activity_recorded", payload=row,
            actor_type="background", actor_id="eva-background", origin="background",
            trust=1.0, sensitivity="normal", consent_scope="local_only",
            idempotency_key=f"background-activity:{tick_id}:{status}:{row['EndedAt']}",
            legacy_table="BackgroundActivity", legacy_columns=_BG_ACTIVITY_COLUMNS,
            legacy_row=row, projection_name="background_activity",
        )
        wrote = True
    except Exception as exc:
        print(f"[Background] Activity event mutation failed: {exc}")
    if wrote and backend == "kusto" and cluster and database and _st.kusto_token_cache:
        wrote = _kusto_ingest_direct(cluster, database, "BackgroundActivity", _BG_ACTIVITY_COLUMNS, [row])
    error_text = row["Notes"] if status == "failed" else ""
    if status == "failed" and not wrote:
        error_text = (error_text + "; activity write failed").strip("; ")
    _set_background_activity(row, error_text)
    return wrote



def _background_source_window(cluster, database, window_end):
    fallback_start = window_end - datetime.timedelta(seconds=7200)
    latest_summary = None
    rows = _kusto_query_direct(cluster, database, "MemorySummaries | summarize LastTimestamp=max(Timestamp)")
    if rows:
        latest_summary = _parse_kusto_datetime(rows[0].get("LastTimestamp"))
    window_start = fallback_start
    if latest_summary and latest_summary > window_start:
        window_start = latest_summary
    if window_start > window_end:
        window_start = window_end
    return window_start, window_end



def _background_conversations_query(window_start, window_end):
    active_start = window_start
    launch_start = _parse_kusto_datetime(_st.cognition_launch_iso)
    if launch_start and launch_start > active_start:
        active_start = launch_start
    start_iso = _to_utc_iso(active_start)
    end_iso = _to_utc_iso(window_end)
    return (
        "Conversations\n"
        f"| where Timestamp >= datetime('{start_iso}') and Timestamp <= datetime('{end_iso}')\n"
        "| order by Timestamp asc\n"
        "| take 200\n"
        "| project Timestamp, Role, Provider, Model, Content, TokenEstimate"
    )



def _query_background_conversations(cluster, database, window_start, window_end):
    backend = _resolve_memory_backend()
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        start_iso = _to_utc_iso(window_start)
        end_iso = _to_utc_iso(window_end)
        return mem.query(
            f"SELECT SessionId, Timestamp, Role, Content, TokenEstimate FROM Conversations "
            f"WHERE Timestamp >= '{start_iso}' AND Timestamp <= '{end_iso}' "
            f"ORDER BY Timestamp ASC LIMIT 500"
        )
    return _kusto_query_direct(cluster, database, _background_conversations_query(window_start, window_end))



def _background_summary_topics(user_rows):
    stop_words = set(_ENTITY_IGNORE_WORDS) | set(_ENTITY_RESERVED_TERMS) | {"assistant", "user", "eva", "message", "messages"}
    topic_counts = {}
    topic_labels = {}
    for conversation_row in user_rows:
        content = str(conversation_row.get("Content", "") or "")
        for match_text in re.findall(r"\b[A-Za-z][A-Za-z_-]{2,}\b", content):
            key = match_text.lower().strip("_-")
            if len(key) < 4 or key in stop_words:
                continue
            topic_counts[key] = topic_counts.get(key, 0) + 1
            topic_labels.setdefault(key, match_text.strip("_-"))
    ranked_topics = sorted(topic_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
    return [topic_labels.get(topic_key, topic_key) for topic_key, _ in ranked_topics]



def _build_background_summary(conversation_rows):
    user_rows = [row for row in conversation_rows if str(row.get("Role", "")).lower() == "user"]
    assistant_rows = [row for row in conversation_rows if str(row.get("Role", "")).lower() == "assistant"]
    topics = _background_summary_topics(user_rows)
    topics_text = ", ".join(topics) if topics else "general conversation"
    if assistant_rows:
        assistant_text = f"Assistant actions: responded in {len(assistant_rows)} turn(s) with guidance, answers, or implementation detail."
    else:
        assistant_text = "Assistant actions: no assistant response rows were present in the source window."
    summary = (
        f"Background consolidation for {len(conversation_rows)} conversation row(s) "
        f"({len(user_rows)} user, {len(assistant_rows)} assistant). "
        f"User topics/entities: {topics_text}. {assistant_text}"
    )
    return summary[:800]



def _write_background_proposal(cluster, database, proposal_row):
    backend = _resolve_memory_backend()
    from bridge.finalize import mutate_event
    safe_row = redact_credentials(proposal_row)
    mem = _get_sqlite_mem()
    try:
        mutate_event(
            mem, mem.event_repository(),
            stream_id=f"background:proposal:{safe_row.get('ProposalId', '')}",
            event_type="background.proposal_recorded", payload=safe_row,
            actor_type="background", actor_id=str(safe_row.get("ReviewedBy") or "eva-background"),
            origin="background", trust=1.0, sensitivity="private",
            consent_scope="cloud_allowed" if backend == "kusto" and _st.egress_mode == "cloud" else "local_only",
            idempotency_key=(
                f"background-proposal:{safe_row.get('ProposalId','')}:"
                f"{safe_row.get('Status','')}:{safe_row.get('ReviewedAt') or safe_row.get('CreatedAt','')}"
            ),
            legacy_table="BackgroundProposals", legacy_columns=_BG_PROPOSAL_COLUMNS,
            legacy_row=safe_row, projection_name="background_proposals",
        )
    except Exception as exc:
        print(f"[Background] Proposal event mutation failed: {exc}")
        return False
    if backend == "kusto":
        return _kusto_ingest_direct(cluster, database, "BackgroundProposals", _BG_PROPOSAL_COLUMNS, [safe_row])
    return True



def _background_memory_summary_exists(cluster, database, summary_row):
    timestamp = _parse_kusto_datetime(summary_row.get("Timestamp"))
    if not timestamp:
        return False, "Proposal payload Timestamp must be a valid datetime"
    timestamp_iso = _to_utc_iso(timestamp)
    backend = _resolve_memory_backend()
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        rows = mem.query(
            "SELECT 1 FROM MemorySummaries WHERE Period = ? "
            "AND Timestamp = ? AND Summary = ? LIMIT 1",
            (str(summary_row.get("Period") or ""), timestamp_iso, str(summary_row.get("Summary") or "")),
        )
        if rows is None:
            return False, "MemorySummaries lookup failed"
        return bool(rows), ""
    query = (
        "MemorySummaries\n"
        f"| where Period == '{_safe_kusto_string(_canonical_kusto_ingest_string(summary_row.get('Period')))}'\n"
        f"| where Timestamp == datetime('{timestamp_iso}')\n"
        f"| where Summary == '{_safe_kusto_string(_canonical_kusto_ingest_string(summary_row.get('Summary')))}'\n"
        "| take 1"
    )
    rows = _kusto_query_direct(cluster, database, query)
    if rows is None:
        return False, "MemorySummaries lookup failed"
    return bool(rows), ""



def _apply_proposal_payload(cluster, database, target_table, payload, mutation_id=""):
    """Apply a background proposal payload to its target table.

    Returns (ok, error, note). Used by both the auto-apply path inside a tick
    and the manual approve endpoint, so the apply logic lives in one place.
    """
    if not isinstance(payload, dict):
        return False, "proposal payload missing or not an object", ""
    backend = _resolve_memory_backend()
    from bridge.events import canonical_json, payload_hash
    from bridge.finalize import mutate_event
    mutation_key = mutation_id or payload_hash(canonical_json(payload))
    local_mem = _get_sqlite_mem()
    local_repo = local_mem.event_repository()
    if target_table == "MemorySummaries":
        summary_row = {
            "Period": str(payload.get("Period", "") or ""),
            "Summary": str(payload.get("Summary", "") or ""),
            "Timestamp": str(payload.get("Timestamp", "") or ""),
        }
        if not summary_row["Period"] or not summary_row["Summary"] or not summary_row["Timestamp"]:
            return False, "Proposal payload must include Period, Summary, and Timestamp", ""
        parsed_timestamp = _parse_kusto_datetime(summary_row["Timestamp"])
        if not parsed_timestamp:
            return False, "Proposal payload Timestamp must be a valid datetime", ""
        summary_row["Timestamp"] = _to_utc_iso(parsed_timestamp)
        summary_row = redact_credentials(summary_row)
        try:
            mutate_event(
                local_mem, local_repo,
                stream_id=f"background:summary:{summary_row['Period']}",
                event_type="background.memory_summary_applied", payload=summary_row,
                actor_type="background", actor_id="eva-background", origin="background",
                trust=0.6, sensitivity="private",
                consent_scope="cloud_allowed" if backend == "kusto" and _st.egress_mode == "cloud" else "local_only",
                idempotency_key=f"background:{mutation_key}:MemorySummaries",
                legacy_table="MemorySummaries",
                legacy_columns=["Period", "Summary", "Timestamp"],
                legacy_row=summary_row, projection_name="memory_summaries",
            )
        except Exception as exc:
            return False, f"Memory summary event mutation failed: {exc}", ""
        if backend == "kusto":
            summary_exists, error = _background_memory_summary_exists(cluster, database, summary_row)
            if error:
                return False, error, ""
            if not summary_exists and not _kusto_ingest_direct(
                cluster, database, "MemorySummaries",
                ["Period", "Summary", "Timestamp"], [summary_row]
            ):
                return False, "MemorySummaries ADX projection failed", ""
        return True, "", "applied to MemorySummaries"
    if target_table == "Reflections":
        observation = str(payload.get("Observation", "") or "").strip()
        if not observation:
            return False, "Reflection payload must include Observation", ""
        parsed_timestamp = _parse_kusto_datetime(payload.get("Timestamp")) or _utc_now()
        try:
            effectiveness = float(payload.get("Effectiveness", 0.0) or 0.0)
        except (TypeError, ValueError):
            effectiveness = 0.0
        reflection_row = {
            "Timestamp": _to_utc_iso(parsed_timestamp),
            "Trigger": str(payload.get("Trigger", "") or "")[:200],
            "Observation": observation[:1000],
            "ActionTaken": str(payload.get("ActionTaken", "") or "")[:500],
            "Effectiveness": effectiveness,
        }
        reflection_row = redact_credentials(reflection_row)
        try:
            mutate_event(
                local_mem, local_repo,
                stream_id=f"background:reflection:{str(payload.get('Trigger', 'general'))[:100]}",
                event_type="background.reflection_applied", payload=reflection_row,
                actor_type="background", actor_id="eva-background", origin="background",
                trust=0.5, sensitivity="private",
                consent_scope="cloud_allowed" if backend == "kusto" and _st.egress_mode == "cloud" else "local_only",
                idempotency_key=f"background:{mutation_key}:Reflections",
                legacy_table="Reflections",
                legacy_columns=["Timestamp", "Trigger", "Observation", "ActionTaken", "Effectiveness"],
                legacy_row=reflection_row, projection_name="reflections",
            )
        except Exception as exc:
            return False, f"Reflection event mutation failed: {exc}", ""
        if backend == "kusto":
            canonical_trigger = _canonical_kusto_ingest_string(reflection_row["Trigger"])
            canonical_observation = _canonical_kusto_ingest_string(reflection_row["Observation"])
            canonical_action = _canonical_kusto_ingest_string(reflection_row["ActionTaken"])
            exists_query = (
                "Reflections\n"
                f"| where Timestamp == datetime('{reflection_row['Timestamp']}')\n"
                f"| where Trigger == '{_safe_kusto_string(canonical_trigger)}'\n"
                f"| where Observation == '{_safe_kusto_string(canonical_observation)}'\n"
                f"| where ActionTaken == '{_safe_kusto_string(canonical_action)}'\n"
                f"| where todouble(Effectiveness) == {reflection_row['Effectiveness']}\n"
                "| take 1"
            )
            existing = _kusto_query_direct(cluster, database, exists_query)
            if existing is None:
                return False, "Reflections idempotency lookup failed", ""
            if not existing:
                if not _kusto_ingest_direct(cluster, database, "Reflections", ["Timestamp", "Trigger", "Observation", "ActionTaken", "Effectiveness"], [reflection_row]):
                    return False, "Reflections ADX projection failed", ""
        return True, "", "applied to Reflections"
    return False, "Unsupported proposal target table", ""



def _create_background_proposal_row(job_type, target_table, payload, window_start, window_end, notes, status="pending"):
    now_iso = _proposal_transition_iso()
    return {
        "ProposalId": "bgp-" + str(uuid.uuid4()),
        "CreatedAt": now_iso,
        "JobType": job_type,
        "TargetTable": target_table,
        "Payload": payload,
        "Status": status,
        "SourceWindowStart": _to_utc_iso(window_start),
        "SourceWindowEnd": _to_utc_iso(window_end),
        "Notes": str(notes or "")[:500],
        "ReviewedAt": "",
        "ReviewedBy": "",
    }



def _existing_goal_checkin_ids(cluster, database):
    """GoalIds that already have a pending goal check-in proposal (dedup)."""
    backend = _resolve_memory_backend()
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        rows = mem.query(
            "SELECT bp.Payload FROM BackgroundProposals bp "
            "WHERE bp.rowid = (SELECT newer.rowid FROM BackgroundProposals newer "
            "WHERE newer.ProposalId = bp.ProposalId "
            "ORDER BY COALESCE(NULLIF(newer.ReviewedAt,''), newer.CreatedAt) DESC, newer.rowid DESC LIMIT 1) "
            "AND bp.Status IN ('pending', 'applying', 'approved') AND bp.JobType = ? LIMIT 100",
            (_BG_JOB_GOAL_CHECKIN,),
        ) or []
    else:
        query = _BG_PROPOSALS_LATEST_QUERY + f" | where Status in ('pending', 'applying', 'approved') | where JobType == '{_BG_JOB_GOAL_CHECKIN}' | take 100"
        rows = _kusto_query_direct(cluster, database, query) or []
    ids = set()
    for row in rows:
        payload, _ = _background_proposal_payload(row)
        if isinstance(payload, dict) and payload.get("GoalId"):
            ids.add(str(payload.get("GoalId")))
    return ids



def _build_daily_digest(conversation_rows, goal_rows, period):
    user_rows = [row for row in conversation_rows if str(row.get("Role", "")).lower() == "user"]
    assistant_rows = [row for row in conversation_rows if str(row.get("Role", "")).lower() == "assistant"]
    topics = _background_summary_topics(user_rows)
    topics_text = ", ".join(topics) if topics else "general conversation"
    active_goals = [g for g in (goal_rows or []) if str(g.get("Status", "")).lower() == "active"]
    goal_titles = [str(g.get("Title", "") or "untitled").strip() for g in active_goals[:3]]
    if active_goals:
        goals_text = f"{len(active_goals)} active goal(s)"
        if goal_titles:
            goals_text += " (" + "; ".join(goal_titles) + ")"
    else:
        goals_text = "no active goals"
    digest = (
        f"Daily digest for {period}: {len(user_rows)} user message(s) and "
        f"{len(assistant_rows)} assistant reply(ies). Top topics: {topics_text}. "
        f"Goals: {goals_text}."
    )
    return digest[:800]



def _bg_period_exists(ctx, period):
    """Check whether a MemorySummaries row with this Period already exists."""
    backend = ctx.get("backend", "kusto")
    safe = _safe_kusto_string(period)
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        return bool(mem.query(f"SELECT 1 FROM MemorySummaries WHERE Period = '{safe}' LIMIT 1"))
    return bool(_kusto_query_direct(ctx["cluster"], ctx["database"],
                                    f"MemorySummaries | where Period == '{safe}' | take 1"))



def _bg_goals_query(ctx):
    """Fetch goals rows, backend-aware."""
    backend = ctx.get("backend", "kusto")
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        return mem.query(
            latest_row_sql("Goals", "GoalId", "UpdatedAt")
            + " ORDER BY Priority DESC, UpdatedAt DESC"
        )
    return _kusto_query_direct(ctx["cluster"], ctx["database"], _GOALS_LATEST_QUERY)



def _job_memory_consolidation(ctx):
    conversation_rows = _query_background_conversations(ctx["cluster"], ctx["database"], ctx["window_start"], ctx["window_end"])
    if conversation_rows is None:
        return None, "Conversations query failed"
    if not conversation_rows:
        return [], "no conversations in source window"
    summary_text = _build_background_summary(conversation_rows)
    payload = {
        "Period": "background-" + ctx["now_iso"][:10],
        "Summary": summary_text,
        "Timestamp": ctx["now_iso"],
    }
    return [{
        "target_table": "MemorySummaries",
        "payload": payload,
        "auto_apply": False,
        "notes": f"from {len(conversation_rows)} conversation row(s)",
    }], f"proposal from {len(conversation_rows)} conversation row(s)"



def _job_goal_checkin(ctx):
    backend = ctx.get("backend", "kusto")
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        goal_rows = mem.query(
            latest_row_sql("Goals", "GoalId", "UpdatedAt")
            + " AND Status IN ('active','paused') ORDER BY Priority DESC, UpdatedAt DESC"
        )
    else:
        goal_rows = _kusto_query_direct(ctx["cluster"], ctx["database"], _GOALS_LATEST_QUERY)
    if goal_rows is None:
        return None, "Goals query failed"
    active_goals = [g for g in goal_rows if str(g.get("Status", "")).lower() == "active"]
    if not active_goals:
        return [], "no active goals"
    existing_ids = _existing_goal_checkin_ids(ctx["cluster"], ctx["database"])
    now = _utc_now()
    stalled = []
    for goal in active_goals:
        goal_id = str(goal.get("GoalId", "") or "")
        if not goal_id or goal_id in existing_ids:
            continue
        updated = _parse_kusto_datetime(goal.get("UpdatedAt"))
        age_days = (now - updated).days if updated else 999
        if age_days >= _GOAL_STALE_DAYS:
            stalled.append((age_days, goal))
    if not stalled:
        return [], "no stalled active goals"
    stalled.sort(key=lambda item: -item[0])
    proposals = []
    for age_days, goal in stalled[:_GOAL_CHECKIN_MAX]:
        title = str(goal.get("Title", "") or "untitled goal").strip()
        category = str(goal.get("Category", "") or "").strip()
        observation = (
            f"Goal '{title}'" + (f" ({category})" if category else "") +
            f" has had no updates for {age_days} day(s). Suggest a concrete next step "
            "or update its status so it keeps moving."
        )
        payload = {
            "Timestamp": ctx["now_iso"],
            "Trigger": "goal_checkin:" + str(goal.get("GoalId")),
            "Observation": observation,
            "ActionTaken": "",
            "Effectiveness": 0.0,
            "GoalId": str(goal.get("GoalId")),
        }
        proposals.append({
            "target_table": "Reflections",
            "payload": payload,
            "auto_apply": False,
            "notes": f"stalled goal '{title}' ({age_days}d)",
        })
    return proposals, f"{len(proposals)} goal check-in(s)"



def _job_daily_digest(ctx):
    cluster, database = ctx["cluster"], ctx["database"]
    backend = ctx.get("backend", "kusto")
    now = _utc_now()
    day_start = (now - datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    period = "digest-" + _to_utc_iso(day_start)[:10]
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        existing = mem.query(f"SELECT 1 FROM MemorySummaries WHERE Period = '{_safe_kusto_string(period)}' LIMIT 1")
    else:
        existing = _kusto_query_direct(cluster, database, f"MemorySummaries | where Period == '{_safe_kusto_string(period)}' | take 1")
    if existing:
        return [], "digest already exists for " + period
    conversation_rows = _query_background_conversations(cluster, database, day_start, day_end)
    if conversation_rows is None:
        return None, "Conversations query failed"
    if not conversation_rows:
        return [], "no conversations for " + period
    if backend == "sqlite":
        goal_rows = mem.query(
            latest_row_sql("Goals", "GoalId", "UpdatedAt")
            + " ORDER BY Priority DESC, UpdatedAt DESC"
        ) or []
    else:
        goal_rows = _kusto_query_direct(cluster, database, _GOALS_LATEST_QUERY) or []
    digest_text = _build_daily_digest(conversation_rows, goal_rows, period)
    payload = {
        "Period": period,
        "Summary": digest_text,
        "Timestamp": ctx["now_iso"],
    }
    return [{
        "target_table": "MemorySummaries",
        "payload": payload,
        "auto_apply": True,
        "notes": f"daily digest from {len(conversation_rows)} conversation row(s)",
    }], "daily digest for " + period



def _bg_to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default



def _bg_to_int(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default



def _pending_proposal_exists(cluster, database, job_type):
    """True when a pending proposal already exists for job_type (dedup guard)."""
    backend = _resolve_memory_backend()
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        rows = mem.query(
            "SELECT 1 FROM BackgroundProposals bp "
            "WHERE bp.rowid = (SELECT newer.rowid FROM BackgroundProposals newer "
            "WHERE newer.ProposalId = bp.ProposalId "
            "ORDER BY COALESCE(NULLIF(newer.ReviewedAt,''), newer.CreatedAt) DESC, newer.rowid DESC LIMIT 1) "
            "AND bp.Status IN ('pending', 'applying', 'approved') AND bp.JobType = ? LIMIT 1",
            (job_type,),
        )
        return bool(rows)
    query = _BG_PROPOSALS_LATEST_QUERY + f" | where Status in ('pending', 'applying', 'approved') | where JobType == '{job_type}' | take 1"
    rows = _kusto_query_direct(cluster, database, query)
    return bool(rows)



def _bg_agent_prompt(prompt_text, ctx, timeout=120):
    """Run a background-only prompt through the ACP agent.

    Returns (text, error). Bails out if the user became active mid-tick so a
    background research call never collides with a live chat turn. Scheduled
    ticks already pause on recent activity, but agent jobs run later in the
    tick, so this re-checks just before the (slow) model call.
    """
    if ctx.get("trigger") != "manual" and time.time() - _st.last_user_activity_ts < 120:
        return None, "user active"
    if _st.acp_client is None or not getattr(_st.acp_client, "alive", False):
        return None, "agent unavailable"
    try:
        result = _st.acp_client.prompt(prompt_text, timeout=timeout)
    except Exception as agent_error:
        return None, "agent error: " + str(agent_error)[:200]
    if isinstance(result, dict):
        if result.get("error"):
            return None, "agent error: " + str(result.get("error"))[:200]
        text = str(result.get("text", "") or "").strip()
        if not text:
            return None, "agent returned no text"
        return text, ""
    return None, "agent returned no result"



def _bg_watched_tickers(goal_rows):
    """Default watch symbols plus tickers mentioned in active finance goals."""
    tickers = []
    seen = set()
    for symbol in _SEC_WATCH_SYMBOLS:
        if symbol not in seen:
            seen.add(symbol)
            tickers.append(symbol)
    for goal in goal_rows or []:
        if str(goal.get("Status", "")).lower() != "active":
            continue
        text = " ".join([
            str(goal.get("Title", "") or ""),
            str(goal.get("RelatedTopics", "") or ""),
            str(goal.get("Description", "") or ""),
        ])
        if not re.search(r"\b(ticker|symbol|stock|shares?|equit)\b", text, re.IGNORECASE):
            continue
        for match_text in re.findall(r"\b([A-Z]{2,5})\b", text):
            if match_text in _TICKER_STOPWORDS or match_text in seen:
                continue
            seen.add(match_text)
            tickers.append(match_text)
    return tickers[:8]



def _job_knowledge_hygiene(ctx):
    cluster, database = ctx["cluster"], ctx["database"]
    backend = ctx.get("backend", "kusto")
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        if not mem.table_exists("Knowledge"):
            return [], "no Knowledge table"
    else:
        if not _get_table_columns(cluster, database, "Knowledge"):
            return [], "no Knowledge table"
    if _pending_proposal_exists(cluster, database, _BG_JOB_KNOWLEDGE_HYGIENE):
        return [], "hygiene proposal already pending"
    if backend == "sqlite":
        rows = mem.query("SELECT Entity, Relation, Value, Confidence FROM Knowledge LIMIT 2000")
    else:
        rows = _kusto_query_direct(cluster, database, "Knowledge | project Entity, Relation, Value, Confidence | take 2000")
    if rows is None:
        return None, "Knowledge query failed"
    if not rows:
        return [], "no knowledge rows"
    stale = [r for r in rows if _bg_to_float(r.get("Confidence"), 1.0) < _KNOWLEDGE_STALE_CONFIDENCE]
    seen = {}
    dups = []
    for r in rows:
        key = (str(r.get("Entity", "")).strip().lower(), str(r.get("Relation", "")).strip().lower())
        if not key[0]:
            continue
        if key in seen:
            dups.append(r)
        else:
            seen[key] = r
    if not stale and not dups:
        return [], "knowledge healthy"
    parts = []
    if stale:
        sample = ", ".join(f"{r.get('Entity')}/{r.get('Relation')}" for r in stale[:5])
        parts.append(f"{len(stale)} low-confidence fact(s) under {_KNOWLEDGE_STALE_CONFIDENCE} (e.g. {sample})")
    if dups:
        sample = ", ".join(f"{r.get('Entity')}/{r.get('Relation')}" for r in dups[:5])
        parts.append(f"{len(dups)} duplicate entity/relation pair(s) (e.g. {sample})")
    observation = (
        "Knowledge hygiene: " + "; ".join(parts) +
        ". Review whether to prune the stale facts or merge the duplicates so memory stays accurate."
    )
    payload = {
        "Timestamp": ctx["now_iso"],
        "Trigger": "knowledge_hygiene",
        "Observation": observation[:1000],
        "ActionTaken": "",
        "Effectiveness": 0.0,
    }
    return [{
        "target_table": "Reflections",
        "payload": payload,
        "auto_apply": False,
        "notes": f"{len(stale)} stale, {len(dups)} dup",
    }], "knowledge hygiene report"



def _job_reflection_synthesis(ctx):
    cluster, database = ctx["cluster"], ctx["database"]
    backend = ctx.get("backend", "kusto")
    start_iso = _to_utc_iso(ctx["window_start"])
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        if not mem.table_exists("Reflections"):
            return [], "no Reflections table"
        rows = mem.query(
            f"SELECT Timestamp, Trigger, Observation FROM Reflections "
            f"WHERE Timestamp >= '{start_iso}' AND Trigger != 'reflection_synthesis' "
            f"ORDER BY Timestamp ASC LIMIT 100"
        )
    else:
        if not _get_table_columns(cluster, database, "Reflections"):
            return [], "no Reflections table"
        query = (
            "Reflections "
            f"| where Timestamp >= datetime('{start_iso}') "
            "| where Trigger != 'reflection_synthesis' "
            "| order by Timestamp asc | take 100 | project Timestamp, Trigger, Observation"
        )
        rows = _kusto_query_direct(cluster, database, query)
    if rows is None:
        return None, "Reflections query failed"
    if len(rows) < _REFLECTION_SYNTH_MIN:
        return [], "not enough new reflections"
    trigger_counts = {}
    for r in rows:
        label = str(r.get("Trigger", "") or "general").split(":")[0]
        trigger_counts[label] = trigger_counts.get(label, 0) + 1
    top = sorted(trigger_counts.items(), key=lambda item: -item[1])[:4]
    theme = ", ".join(f"{label} ({count})" for label, count in top)
    observation = (
        f"Reflection synthesis over {len(rows)} recent reflection(s). Recurring themes: {theme}. "
        "Recording this pattern for self-awareness across sessions."
    )
    payload = {
        "Timestamp": ctx["now_iso"],
        "Trigger": "reflection_synthesis",
        "Observation": observation[:1000],
        "ActionTaken": "",
        "Effectiveness": 0.0,
    }
    return [{
        "target_table": "Reflections",
        "payload": payload,
        "auto_apply": True,
        "notes": f"synthesized {len(rows)} reflection(s)",
    }], "reflection synthesis"



def _job_emotion_drift(ctx):
    cluster, database = ctx["cluster"], ctx["database"]
    backend = ctx.get("backend", "kusto")
    dims = ["Joy", "Curiosity", "Concern", "Excitement", "Calm", "Empathy"]
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        if not mem.table_exists("EmotionState"):
            return [], "no EmotionState table"
        if _pending_proposal_exists(cluster, database, _BG_JOB_EMOTION_DRIFT):
            return [], "drift proposal already pending"
        agg_cols = ", ".join(f"AVG({d}) AS avg{d}" for d in dims)
        rows = mem.query(
            f"SELECT {agg_cols}, COUNT(*) AS N FROM EmotionState "
            f"WHERE Timestamp >= datetime('now', '-7 days')"
        )
        base_rows = mem.query("SELECT Dimension, Value FROM EmotionBaseline") or []
    else:
        if not _get_table_columns(cluster, database, "EmotionState"):
            return [], "no EmotionState table"
        if _pending_proposal_exists(cluster, database, _BG_JOB_EMOTION_DRIFT):
            return [], "drift proposal already pending"
        agg = (
            "EmotionState | where Timestamp >= ago(7d) | summarize "
            + ", ".join(f"avg{d}=avg({d})" for d in dims)
            + ", N=count()"
        )
        rows = _kusto_query_direct(cluster, database, agg)
        base_rows = _kusto_query_direct(cluster, database, "EmotionBaseline | project Dimension, Value") or []
    baseline = {str(b.get("Dimension")): _bg_to_float(b.get("Value")) for b in base_rows}
    drifts = []
    for d in dims:
        recent = _bg_to_float(rows[0].get("avg" + d))
        base = baseline.get(d)
        if base is None:
            continue
        delta = recent - base
        if abs(delta) >= _EMOTION_DRIFT_THRESHOLD:
            sign = "+" if delta >= 0 else ""
            drifts.append(f"{d} {sign}{round(delta, 2)} (now {round(recent, 2)} vs base {round(base, 2)})")
    if not drifts:
        return [], "emotion within baseline"
    observation = (
        "Emotion baseline drift over the last 7 day(s): " + "; ".join(drifts) +
        ". Consider whether the resting baseline should be recalibrated."
    )
    payload = {
        "Timestamp": ctx["now_iso"],
        "Trigger": "emotion_drift",
        "Observation": observation[:1000],
        "ActionTaken": "",
        "Effectiveness": 0.0,
    }
    return [{
        "target_table": "Reflections",
        "payload": payload,
        "auto_apply": False,
        "notes": f"{len(drifts)} dimension(s) drifted",
    }], "emotion drift"



def _job_token_telemetry(ctx):
    cluster, database = ctx["cluster"], ctx["database"]
    backend = ctx.get("backend", "kusto")
    period = "telemetry-" + ctx["now_iso"][:10]
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        existing = mem.query(f"SELECT 1 FROM MemorySummaries WHERE Period = '{_safe_kusto_string(period)}' LIMIT 1")
        if existing:
            return [], "telemetry already logged for " + period
        rows = mem.query(
            "SELECT SUM(TokenEstimate) AS Total, COUNT(*) AS N, "
            "SUM(CASE WHEN Role='user' THEN 1 ELSE 0 END) AS Users, "
            "SUM(CASE WHEN Role='assistant' THEN 1 ELSE 0 END) AS Assistants "
            "FROM Conversations WHERE Timestamp >= datetime('now', '-1 day')"
        )
    else:
        existing = _kusto_query_direct(cluster, database, f"MemorySummaries | where Period == '{_safe_kusto_string(period)}' | take 1")
        if existing:
            return [], "telemetry already logged for " + period
        query = (
            "Conversations | where Timestamp >= ago(1d) | summarize "
            "Total=sum(TokenEstimate), N=count(), "
            "Users=countif(Role=='user'), Assistants=countif(Role=='assistant')"
        )
        rows = _kusto_query_direct(cluster, database, query)
    if rows is None:
        return None, "Conversations query failed"
    row = rows[0] if rows else {}
    total = _bg_to_int(row.get("Total"))
    count = _bg_to_int(row.get("N"))
    if count == 0:
        return [], "no conversation activity to log"
    summary = (
        f"Token telemetry for {period[10:]}: ~{total} tokens across {count} message(s) "
        f"({_bg_to_int(row.get('Users'))} user, {_bg_to_int(row.get('Assistants'))} assistant)."
    )
    payload = {"Period": period, "Summary": summary[:800], "Timestamp": ctx["now_iso"]}
    return [{
        "target_table": "MemorySummaries",
        "payload": payload,
        "auto_apply": True,
        "notes": f"{total} tokens / {count} msgs",
    }], "token telemetry"



def _job_proactive_briefing(ctx):
    cluster, database = ctx["cluster"], ctx["database"]
    backend = ctx.get("backend", "kusto")
    period = "briefing-" + ctx["now_iso"][:10]
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        existing = mem.query(f"SELECT 1 FROM MemorySummaries WHERE Period = '{_safe_kusto_string(period)}' LIMIT 1")
        if existing:
            return [], "briefing already exists for " + period
        recent = mem.query(
            "SELECT Period, Summary FROM MemorySummaries "
            "WHERE Timestamp >= datetime('now', '-3 days') AND Period NOT LIKE 'briefing-%' "
            "ORDER BY Timestamp DESC LIMIT 8"
        ) or []
        goal_rows = mem.query(
            latest_row_sql("Goals", "GoalId", "UpdatedAt")
            + " ORDER BY Priority DESC, UpdatedAt DESC"
        ) or []
        active = [g for g in goal_rows if str(g.get("Status", "")).lower() == "active"]
        convo = mem.query("SELECT MAX(Timestamp) AS Last FROM Conversations") or []
    else:
        existing = _kusto_query_direct(cluster, database, f"MemorySummaries | where Period == '{_safe_kusto_string(period)}' | take 1")
        if existing:
            return [], "briefing already exists for " + period
        recent = _kusto_query_direct(
            cluster, database,
            "MemorySummaries | where Timestamp >= ago(3d) | where Period !startswith 'briefing-' "
            "| order by Timestamp desc | take 8 | project Period, Summary"
        ) or []
        goal_rows = _kusto_query_direct(cluster, database, _GOALS_LATEST_QUERY) or []
        active = [g for g in goal_rows if str(g.get("Status", "")).lower() == "active"]
        convo = _kusto_query_direct(cluster, database, "Conversations | summarize Last=max(Timestamp)") or []
    last_seen = _parse_kusto_datetime(convo[0].get("Last")) if convo else None
    lines = []
    if last_seen:
        gap_days = (_utc_now() - last_seen).days
        lines.append(f"About {gap_days} day(s) since our last exchange." if gap_days >= 1 else "We spoke recently.")
    if active:
        titles = "; ".join(str(g.get("Title", "") or "untitled") for g in active[:3])
        lines.append(f"{len(active)} active goal(s): {titles}.")
    if recent:
        notes = "; ".join(str(r.get("Summary", "") or "")[:80] for r in recent[:3])
        lines.append("Recent notes: " + notes)
    if not lines:
        return [], "nothing to brief"
    briefing = "Proactive briefing: " + " ".join(lines)
    payload = {"Period": period, "Summary": briefing[:800], "Timestamp": ctx["now_iso"]}
    return [{
        "target_table": "MemorySummaries",
        "payload": payload,
        "auto_apply": True,
        "notes": "proactive briefing",
    }], "proactive briefing"



def _job_market_snapshot(ctx):
    cluster, database = ctx["cluster"], ctx["database"]
    period = "market-" + ctx["now_iso"][:10]
    if _bg_period_exists(ctx, period):
        return [], "market snapshot already exists for " + period
    goal_rows = _bg_goals_query(ctx) or []
    tickers = _bg_watched_tickers(goal_rows)
    if not tickers:
        return [], "no watched tickers"
    prompt = (
        "Background task (no user is present). Provide a concise daily market snapshot for these ticker symbols: "
        + ", ".join(tickers) + ". For each symbol give the latest price, daily change percent, and volume if you can. "
        "Two short lines per symbol at most. If a symbol is unknown, say so. Plain text only."
    )
    text, error = _bg_agent_prompt(prompt, ctx, timeout=120)
    if text is None:
        return [], "agent: " + error
    summary = ("Market snapshot for " + ", ".join(tickers) + ":\n" + text)[:800]
    payload = {"Period": period, "Summary": summary, "Timestamp": ctx["now_iso"]}
    return [{
        "target_table": "MemorySummaries",
        "payload": payload,
        "auto_apply": True,
        "notes": "tickers " + ",".join(tickers),
    }], "market snapshot"



def _job_sec_filing_watch(ctx):
    cluster, database = ctx["cluster"], ctx["database"]
    if _pending_proposal_exists(cluster, database, _BG_JOB_SEC_FILINGS):
        return [], "sec proposal already pending"
    symbols = _SEC_WATCH_SYMBOLS
    prompt = (
        "Background task (no user is present). Check the most recent SEC EDGAR filings for these companies by ticker: "
        + ", ".join(symbols) + ". List only filings from the last 7 days: form type, date, and a one-line description. "
        "If a symbol has no filing in the last 7 days, write 'none recent' for it. Plain text only, be brief."
    )
    text, error = _bg_agent_prompt(prompt, ctx, timeout=120)
    if text is None:
        return [], "agent: " + error
    residual = text.lower().replace("none recent", "")
    if not re.search(r"\b(10-?k|10-?q|8-?k|6-?k|s-1|20-?f|form|filed)\b", residual):
        return [], "no recent filings"
    observation = ("SEC filing watch for " + ", ".join(symbols) + ":\n" + text)[:1000]
    payload = {
        "Timestamp": ctx["now_iso"],
        "Trigger": "sec_filing_watch",
        "Observation": observation,
        "ActionTaken": "",
        "Effectiveness": 0.0,
    }
    return [{
        "target_table": "Reflections",
        "payload": payload,
        "auto_apply": False,
        "notes": "sec " + ",".join(symbols),
    }], "sec filing watch"



def _job_space_weather_alert(ctx):
    cluster, database = ctx["cluster"], ctx["database"]
    period = "spaceweather-" + ctx["now_iso"][:10]
    if _bg_period_exists(ctx, period):
        return [], "space weather already logged for " + period
    prompt = (
        "Background task (no user is present). Report current space weather using NOAA SWPC: the latest planetary Kp "
        "index and any active geomagnetic storm (G-scale), solar flare (R-scale), or radiation (S-scale) alerts. "
        "Start your reply with 'ALERT:' if any level is at or above moderate (Kp 5+, G1+, R1+, or S1+); "
        "otherwise start with 'QUIET:'. Then one or two short lines. Plain text only."
    )
    text, error = _bg_agent_prompt(prompt, ctx, timeout=90)
    if text is None:
        return [], "agent: " + error
    if not text.strip().upper().startswith("ALERT"):
        return [], "space weather quiet"
    summary = ("Space weather alert:\n" + text)[:800]
    payload = {"Period": period, "Summary": summary, "Timestamp": ctx["now_iso"]}
    return [{
        "target_table": "MemorySummaries",
        "payload": payload,
        "auto_apply": True,
        "notes": "space weather alert",
    }], "space weather alert"



def _job_research_deepdive(ctx):
    cluster, database = ctx["cluster"], ctx["database"]
    backend = ctx.get("backend", "kusto")
    goal_rows = _bg_goals_query(ctx)
    if goal_rows is None:
        return None, "Goals query failed"
    active = [g for g in goal_rows if str(g.get("Status", "")).lower() == "active"]
    if not active:
        return [], "no active goals"
    active.sort(key=lambda g: (0 if str(g.get("Category", "")) == "knowledge_curation" else 1, -_bg_to_int(g.get("Priority"))))
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        recent = mem.query(
            "SELECT Trigger FROM Reflections "
            "WHERE Timestamp >= datetime('now', '-3 days') AND Trigger LIKE 'research_deepdive%'"
        ) or []
    else:
        recent = _kusto_query_direct(
            cluster, database,
            "Reflections | where Timestamp >= ago(3d) | where Trigger startswith 'research_deepdive' | project Trigger"
        ) or []
    done_ids = set()
    for r in recent:
        label = str(r.get("Trigger", ""))
        if ":" in label:
            done_ids.add(label.split(":", 1)[1])
    target = None
    for goal in active:
        if str(goal.get("GoalId", "")) not in done_ids:
            target = goal
            break
    if target is None:
        return [], "all active goals researched recently"
    title = str(target.get("Title", "") or "untitled").strip()
    description = str(target.get("Description", "") or "").strip()
    prompt = (
        "Background task (no user is present). Do a brief research deep-dive supporting this goal: '" + title + "'. "
        + (description[:300] + " " if description else "")
        + "Provide 3 to 5 concise factual bullet points or concrete next steps that move this goal forward. Plain text only."
    )
    text, error = _bg_agent_prompt(prompt, ctx, timeout=150)
    if text is None:
        return [], "agent: " + error
    observation = (f"Research deep-dive for goal '{title}':\n" + text)[:1000]
    payload = {
        "Timestamp": ctx["now_iso"],
        "Trigger": "research_deepdive:" + str(target.get("GoalId")),
        "Observation": observation,
        "ActionTaken": "",
        "Effectiveness": 0.0,
    }
    return [{
        "target_table": "Reflections",
        "payload": payload,
        "auto_apply": True,
        "notes": "deep-dive " + title[:40],
    }], "research deep-dive"



def _job_alert_watch(ctx):
    """Evaluate user-defined alert rules and surface notifications when they trip.
    Each rule is checked through the background agent with a leading ALERT:/QUIET:
    convention; firing is gated by per-rule cooldown and content-hash dedup."""
    # Snapshot the rules for evaluation. The slow agent calls below must NOT hold
    # _st.alerts_lock, so per-rule bookkeeping (last_fired_iso/last_hash) is collected
    # here and merged back under the lock at the end against a fresh read, which
    # preserves any concurrent edits an API request made during the tick.
    doc = _load_alerts()
    rules = doc.get("alerts", [])
    active = [r for r in rules if r.get("enabled")]
    if not active:
        return [], "no active alert rules"

    now = _utc_now()
    settings = doc.get("settings", dict(_DEFAULT_ALERT_SETTINGS))
    proposals = []
    fired = 0
    checked = 0
    notes = []
    pending_updates = {}  # rule_id -> {"last_fired_iso", "last_hash"}

    for rule in active:
        if not _alert_cooldown_elapsed(rule, now):
            continue
        # Re-check user presence before each (slow) agent call.
        if ctx.get("trigger") != "manual" and time.time() - _st.last_user_activity_ts < 120:
            notes.append("paused: user active")
            break
        prompt = _alert_build_prompt(rule)
        if not prompt:
            continue
        checked += 1
        text, error = _bg_agent_prompt(prompt, ctx, timeout=150)
        if text is None:
            notes.append(_alert_clip(rule.get("label"), 32) + ": " + error)
            continue
        if not text.strip().upper().startswith("ALERT"):
            continue
        body = text.strip()
        content_hash = hashlib.sha1(body[:500].encode("utf-8")).hexdigest()
        if content_hash == rule.get("last_hash"):
            continue  # same finding as last fire; do not repeat
        pending_updates[str(rule.get("id", ""))] = {
            "last_fired_iso": ctx["now_iso"],
            "last_hash": content_hash,
        }
        fired += 1
        salience = _alert_salience(rule, body)
        proposals.append({
            "target_table": "Reflections",
            "payload": {
                "Timestamp": ctx["now_iso"],
                "Trigger": "alert_watch:" + str(rule.get("id", "")),
                "Observation": (f"Alert '{rule.get('label', '')}':\n" + body)[:1000],
                "ActionTaken": "",
                "Effectiveness": 0.0,
            },
            "auto_apply": True,
            "notes": "alert " + _alert_clip(rule.get("label"), 40),
            "notify": {
                "title": _alert_clip(rule.get("label"), 80) or "Eva alert",
                "body": body[:1200],
                "source": "alert_watch:" + str(rule.get("id", "")),
                "salience": salience,
                "channels": rule.get("channels") or ["chat"],
                "settings": settings,
            },
        })

    if pending_updates:
        # Merge the fired-rule bookkeeping back under the lock against a fresh
        # read so a concurrent API edit during the tick is not clobbered.
        with _st.alerts_lock:
            fresh = _load_alerts()
            for r in fresh.get("alerts", []):
                upd = pending_updates.get(str(r.get("id", "")))
                if upd:
                    r["last_fired_iso"] = upd["last_fired_iso"]
                    r["last_hash"] = upd["last_hash"]
            _save_alerts(fresh)
    if not proposals:
        note = "; ".join(notes) if notes else (f"checked {checked}, none triggered" if checked else "no rules due")
        return [], note
    return proposals, f"{fired} alert(s) triggered"


def _job_adx_projection(ctx):
    if _st.egress_mode != "cloud":
        return [], "ADX projection unavailable for current egress mode"
    if not _cfg.env_truthy("EVA_ADX_PROJECTION"):
        return [], "ADX projection disabled"
    from bridge.adx_projection import project_pending_events
    mem = _get_sqlite_mem()
    succeeded, failed = project_pending_events(
        mem.event_repository(), _kusto_ingest_direct, _get_kusto_config,
        kusto_query_fn=_kusto_query_direct, limit=20,
    )
    return [], f"projected {succeeded}, failed {failed}"


# Ordered registry of automation jobs run on each tick. Fast Kusto-only jobs
# run first; the slower agent-prompt jobs (market, SEC, space weather, research)
# run last so a single tick stays responsive and can bail if the user returns.
_BG_JOBS = [
    (_BG_JOB_ADX_PROJECTION, _job_adx_projection),
    (_BG_JOB_TYPE, _job_memory_consolidation),
    (_BG_JOB_GOAL_CHECKIN, _job_goal_checkin),
    (_BG_JOB_DAILY_DIGEST, _job_daily_digest),
    (_BG_JOB_KNOWLEDGE_HYGIENE, _job_knowledge_hygiene),
    (_BG_JOB_REFLECTION_SYNTHESIS, _job_reflection_synthesis),
    (_BG_JOB_EMOTION_DRIFT, _job_emotion_drift),
    (_BG_JOB_TOKEN_TELEMETRY, _job_token_telemetry),
    (_BG_JOB_PROACTIVE_BRIEFING, _job_proactive_briefing),
    (_BG_JOB_MARKET_SNAPSHOT, _job_market_snapshot),
    (_BG_JOB_SEC_FILINGS, _job_sec_filing_watch),
    (_BG_JOB_SPACE_WEATHER, _job_space_weather_alert),
    (_BG_JOB_RESEARCH_DEEPDIVE, _job_research_deepdive),
    (_BG_JOB_ALERT_WATCH, _job_alert_watch),
]



def _run_background_tick(trigger="scheduled"):
    trigger = "manual" if trigger == "manual" else "scheduled"
    acquired = _st.bg_tick_lock.acquire(blocking=False)
    tick_id = "bg-" + str(uuid.uuid4())
    started_at = _utc_now()
    if not acquired:
        cluster, database = _get_kusto_config() if _resolve_memory_backend() != "sqlite" else (None, None)
        _record_background_activity(
            cluster, database, tick_id, started_at, _utc_now(), "skipped", 0, 0,
            f"{trigger} background tick already running"
        )
        return

    cluster = None
    database = None
    try:
        backend = _resolve_memory_backend()
        if backend == "sqlite":
            cluster, database = None, None
        else:
            cluster, database, context_error = _background_kusto_context()
            if context_error:
                _record_background_activity(cluster, database, tick_id, started_at, _utc_now(), "failed", 0, 0, f"{trigger} background tick: " + context_error)
                return

        if trigger != "manual" and time.time() - _st.last_user_activity_ts < 120:
            _record_background_activity(cluster, database, tick_id, started_at, _utc_now(), "paused", 0, 0, f"{trigger} background tick: recent user activity")
            return

        window_end = _utc_now()
        if backend == "sqlite":
            mem = _get_sqlite_mem()
            # Derive window start from last summary or 2-hour fallback
            fallback_start = window_end - datetime.timedelta(seconds=7200)
            rows = mem.query("SELECT MAX(Timestamp) AS LastTimestamp FROM MemorySummaries")
            latest_summary = None
            if rows and rows[0].get("LastTimestamp"):
                latest_summary = _parse_kusto_datetime(rows[0]["LastTimestamp"])
            window_start = fallback_start
            if latest_summary and latest_summary > window_start:
                window_start = latest_summary
            if window_start > window_end:
                window_start = window_end
        else:
            window_start, window_end = _background_source_window(cluster, database, window_end)

        ctx = {
            "cluster": cluster,
            "database": database,
            "backend": backend,
            "window_start": window_start,
            "window_end": window_end,
            "now_iso": _to_utc_iso(_utc_now()),
            "trigger": trigger,
        }

        for job_type, handler in _BG_JOBS:
            job_started = _utc_now()
            job_tick_id = tick_id + ":" + job_type
            note_prefix = f"{trigger} {job_type}: "
            if not _BG_JOBS_ENABLED.get(job_type, True):
                _record_background_activity(cluster, database, job_tick_id, job_started, _utc_now(), "skipped", 0, 0, note_prefix + "disabled", job_type=job_type)
                continue
            try:
                proposals, job_note = handler(ctx)
            except Exception as job_error:
                _record_background_activity(cluster, database, job_tick_id, job_started, _utc_now(), "failed", 0, 0, note_prefix + str(job_error)[:400], job_type=job_type)
                continue
            if proposals is None:
                _record_background_activity(cluster, database, job_tick_id, job_started, _utc_now(), "failed", 0, 0, note_prefix + (job_note or "job failed"), job_type=job_type)
                continue
            if not proposals:
                _record_background_activity(cluster, database, job_tick_id, job_started, _utc_now(), "skipped", 0, 0, note_prefix + (job_note or "nothing to do"), job_type=job_type)
                continue

            created = 0
            applied = 0
            failed_apply = 0
            pending_count = 0
            for proposal in proposals:
                target_table = proposal.get("target_table")
                payload = proposal.get("payload")
                notes = proposal.get("notes", "")
                should_auto_apply = proposal.get("auto_apply", False)
                if should_auto_apply:
                    row = _create_background_proposal_row(
                        job_type, target_table, payload, window_start, window_end,
                        notes, status="applying"
                    )
                    # Auto-apply: write the result immediately
                    apply_ok, apply_error, apply_note = _apply_proposal_payload(
                        cluster, database, target_table, payload,
                        mutation_id=row["ProposalId"],
                    )
                    if apply_ok:
                        applied += 1
                        row["Status"] = "applied"
                        row["Notes"] = (notes + "; auto-applied (" + apply_note + ")")[:500]
                        # Surface findings the job flagged for the user (alert rules,
                        # proactive briefings). Restraint + dedup live in _notify_enqueue.
                        notify = proposal.get("notify")
                        if isinstance(notify, dict):
                            try:
                                _salience = float(notify.get("salience", 0.5))
                            except (TypeError, ValueError):
                                _salience = 0.5
                            _notify_enqueue(
                                notify.get("title", ""),
                                notify.get("body", ""),
                                notify.get("source", job_type),
                                _salience,
                                notify.get("channels") or ["chat"],
                                settings=notify.get("settings"),
                            )
                    else:
                        failed_apply += 1
                        row["Status"] = "failed"
                        row["Notes"] = (notes + "; auto-apply failed: " + apply_error)[:500]
                    row["ReviewedAt"] = _proposal_transition_iso()
                    row["ReviewedBy"] = "auto"
                else:
                    # Pending: create proposal for human review, do NOT apply
                    pending_count += 1
                    row = _create_background_proposal_row(job_type, target_table, payload, window_start, window_end, notes, status="pending")
                if _write_background_proposal(cluster, database, row):
                    created += 1

            summary_note = note_prefix + (job_note or "done")
            if applied:
                summary_note += f"; auto-applied {applied}"
            if pending_count:
                summary_note += f"; {pending_count} pending review"
            if failed_apply:
                summary_note += f"; {failed_apply} auto-apply failure(s)"
            status = "succeeded" if created else "failed"
            _record_background_activity(cluster, database, job_tick_id, job_started, _utc_now(), status, created, 0, summary_note, job_type=job_type)
    except Exception as error:
        _record_background_activity(cluster, database, tick_id, started_at, _utc_now(), "failed", 0, 0, f"{trigger} background tick: " + str(error)[:500])
    finally:
        _st.bg_tick_lock.release()



def _bg_loop_worker():
    _load_cron_tasks()
    next_due = time.time() + max(1, int(_st.bg_loop_interval_seconds or 7200))
    _cron_last_minute = -1
    while not _st.bg_loop_stop.is_set():
        # Cron: check every ~30s regardless of background loop enabled state
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if now_utc.minute != _cron_last_minute:
            _cron_last_minute = now_utc.minute
            try:
                _cron_tick()
            except Exception as e:
                print(f"[Cron] tick error: {e}")

        if not _st.bg_loop_enabled:
            next_due = time.time() + max(1, int(_st.bg_loop_interval_seconds or 7200))
            _st.bg_loop_stop.wait(5)
            continue

        now_ts = time.time()
        if now_ts >= next_due:
            _run_background_tick("scheduled")
            next_due = time.time() + max(1, int(_st.bg_loop_interval_seconds or 7200))

        wait_seconds = min(5, max(0.1, next_due - time.time()))
        _st.bg_loop_stop.wait(wait_seconds)



def _start_bg_loop():
    # global statement removed — writes go to _st.*
    if not _st.cognition_enabled:
        return False
    backend = _resolve_memory_backend()
    if backend == "sqlite":
        pass  # SQLite needs no cluster/token
    else:
        cluster, database = _get_kusto_config()
        if not cluster or not database or not _st.kusto_token_cache:
            return False
    if _st.bg_loop_thread and _st.bg_loop_thread.is_alive():
        return True
    _st.bg_loop_stop.clear()
    _st.bg_loop_thread = threading.Thread(target=_bg_loop_worker, name="eva-background-loop", daemon=True)
    _st.bg_loop_thread.start()
    print(f"[Bridge] Background loop started ({_st.bg_loop_interval_seconds}s interval)")
    return True



def _stop_bg_loop():
    # global statement removed — writes go to _st.*
    _st.bg_loop_stop.set()
    active_thread = _st.bg_loop_thread
    if active_thread and active_thread.is_alive():
        active_thread.join(timeout=3)
    _st.bg_loop_thread = None



def _trigger_background_run_once():
    threading.Thread(target=_run_background_tick, args=("manual",), daemon=True).start()



def _background_proposal_payload(row):
    payload = row.get("Payload") if row else None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None, "proposal payload is not valid JSON"
    if not isinstance(payload, dict):
        return None, "proposal payload must be an object"
    return payload, ""



def _background_proposal_update_row(current, status, reviewed_by, notes):
    row = {column: current.get(column, "") for column in _BG_PROPOSAL_COLUMNS}
    payload, _ = _background_proposal_payload(row)
    if payload is not None:
        row["Payload"] = payload
    row["Status"] = status
    row["ReviewedAt"] = _proposal_transition_iso()
    row["ReviewedBy"] = reviewed_by
    row["Notes"] = notes or row.get("Notes", "")
    return row


