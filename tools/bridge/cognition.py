"""Bridge domain: cognition."""

import datetime
import json
import os
import re
import time
import uuid
from bridge import config as _cfg
from bridge import state as _st
from bridge.kusto import (_kusto_query_direct, _kusto_ingest_direct,
    _get_kusto_config, _ensure_kusto_token, _get_table_columns)
from bridge.memory import (_memory_query, _memory_ingest, _memory_fts_search,
    _memory_available, _get_sqlite_mem, _resolve_memory_backend,
    _embed_texts, _cosine_similarity, _expand_query_terms,
    _set_openai_key_from)

_ARTIFACTS_DIR = _cfg.ARTIFACTS_DIR
_CANDIDATE_HISTORY_TTL_SECONDS = _cfg.CANDIDATE_HISTORY_TTL_SECONDS
_CONVO_CONTENT_CAP = _cfg.CONVO_CONTENT_CAP
_GOALS_LATEST_QUERY = _cfg.GOALS_LATEST_QUERY
_MEMORY_TABLES = _cfg.MEMORY_TABLES
_SEMANTIC_MIN_SCORE = _cfg.SEMANTIC_MIN_SCORE
_SEMANTIC_POOL_SIZE = _cfg.SEMANTIC_POOL_SIZE
_SKILLS_LATEST_QUERY = _cfg.SKILLS_LATEST_QUERY
_SKILL_INJECT_MAX = _cfg.SKILL_INJECT_MAX
_SKILL_INSTRUCTIONS_INJECT_CAP = _cfg.SKILL_INSTRUCTIONS_INJECT_CAP

def _enable_cognition(mcp_servers, model=None, port=None):
    """Enable cognition hooks and advertise active bridge capabilities."""
    # global statement removed — writes go to _st.*
    import datetime
    os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
    _st.kusto_table_columns_cache = {}
    _st.cognition_launch_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    _st.cognition_launch_id = f"eva-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S')}"
    _st.cognition_enabled = True
    backend = _resolve_memory_backend()
    print(f"[Bridge] Cognition layer ENABLED (memory backend: {backend})")
    print(f"[Bridge] Cognition launch scope: {_st.cognition_launch_id} (since {_st.cognition_launch_iso})")

    # Write SelfState capabilities via the active memory backend
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    selfstate_cols = ["Timestamp", "Capability", "Status", "Details"]

    if backend == "sqlite":
        mem = _get_sqlite_mem()
        details_mem = {"backend": "sqlite", "path": mem.db_path}
    else:
        cluster, startup_db = _get_kusto_config()
        details_mem = {"cluster": cluster or "", "database": startup_db or ""}
        if not cluster or not startup_db:
            print("[Bridge] Kusto not configured; SelfState write skipped")
            if _st.bg_loop_enabled:
                from bridge.background import _start_bg_loop
                _start_bg_loop()
            return

    capabilities = [
        {"Timestamp": now, "Capability": "memory_access", "Status": "active",
         "Details": json.dumps(details_mem)},
        {"Timestamp": now, "Capability": "acp_bridge", "Status": "active",
         "Details": json.dumps({"model": model or "default", "port": port})},
        {"Timestamp": now, "Capability": "cognition", "Status": "active",
         "Details": json.dumps({"features": ["memory_injection", "reflection", "day_lifecycle", "emotion_tracking"]})},
        {"Timestamp": now, "Capability": "data_retrieval", "Status": "active",
         "Details": json.dumps({"skills": ["stock_quotes", "financial_data", "company_info", "web_search"]})},
        {"Timestamp": now, "Capability": "weather_news", "Status": "active",
         "Details": json.dumps({"feeds": ["weather", "news", "markets", "space_weather"]})},
        {"Timestamp": now, "Capability": "image_skills", "Status": "active",
         "Details": json.dumps({"skills": ["wikimedia_search", "gpt_image_1_generation"]})},
        {"Timestamp": now, "Capability": "persistent_memory", "Status": "active",
                "Details": json.dumps({"tables": _MEMORY_TABLES})},
    ]
    for srv in mcp_servers.keys():
        capabilities.append({"Timestamp": now, "Capability": f"mcp_{srv}",
                             "Status": "active", "Details": "{}"})
    if _memory_ingest("SelfState", selfstate_cols, capabilities):
        print(f"[Bridge] SelfState written ({len(capabilities)} capabilities)")
    else:
        print("[Bridge] SelfState write failed (continuing startup)")
    if _st.bg_loop_enabled:
        from bridge.background import _start_bg_loop
        _start_bg_loop()



def _with_launch_filter(query, timestamp_column="Timestamp"):
    """Scope a Kusto query to rows written during the current cognition launch."""
    if not _st.cognition_launch_iso:
        return query

    safe_iso = (_st.cognition_launch_iso or "").replace("'", "")
    filter_expr = f"{timestamp_column} >= datetime('{safe_iso}')"

    if "| where " in query:
        return query.replace("| where ", f"| where {filter_expr} and ", 1)
    return f"{query} | where {filter_expr}"



def _knowledge_scope_clause(max_entities=200):
    """Build a KQL filter clause for entities observed in the current launch."""
    if not _st.cognition_candidate_counts:
        return ""

    scoped = list(_st.cognition_candidate_counts.keys())[-max_entities:]
    safe_entities = []
    for entity in scoped:
        norm = (entity or "").strip()
        if not norm:
            continue
        safe_entities.append(f"'{norm.replace("'", "''")}'")

    if not safe_entities:
        return ""
    return f"Entity in~ ({', '.join(safe_entities)})"


_ENTITY_IGNORE_WORDS = _cfg.ENTITY_IGNORE_WORDS

_ENTITY_RESERVED_TERMS = _cfg.ENTITY_RESERVED_TERMS

_EXPLICIT_FACT_WHITESPACE_RE = re.compile(r"\s+", re.IGNORECASE)
# CHILDREN, PARTNER, PET, LOCATION patterns deliberately omit re.IGNORECASE
# so the [A-Z] anchor on the captured name keeps real proper-noun semantics.
# Users typing "my kids are happy" with a lowercase "happy" are not captured;
# users typing "my kids are June and Iris" are. That trade is intentional.
_EXPLICIT_CHILDREN_RE = re.compile(
    r"\b[Mm]y (?:kid|kids|child|children|son|sons|daughter|daughters)(?:'s| are| is| name(?:s)? (?:are|is))?\s+"
    r"([A-Z][a-zA-Z]+(?:[\s,]+(?:and\s+)?[A-Z][a-zA-Z]+)*)"
)
_EXPLICIT_MOTTO_RE = re.compile(
    r"\bmy (motto|mantra|creed|philosophy|saying|life motto)(?:\s+is)?[:\s]+[\"“']?([^\"”'\n]{5,200})[\"”']?",
    re.IGNORECASE
)
_EXPLICIT_PARTNER_RE = re.compile(
    r"\b[Mm]y (wife|husband|partner|spouse|girlfriend|boyfriend)(?:'s name)?(?:\s+is)?\s+([A-Z][a-zA-Z]+)"
)
_EXPLICIT_PET_RE = re.compile(
    r"\b[Mm]y (dog|cat|pet|bird|rabbit|hamster|fish|horse)(?:'s name)?(?:\s+is)?\s+([A-Z][a-zA-Z]+)"
)
_EXPLICIT_PREFERENCE_RE = re.compile(
    r"\bi (?:love|enjoy|prefer|like)\b\s+([a-z][a-zA-Z\s,]{3,80}?)(?:[.!?\n]|$)",
    re.IGNORECASE
)
_EXPLICIT_INTEREST_RE = re.compile(
    r"\bmy (?:hobby|hobbies|interest|interests|passion|passions) (?:is|are|include|includes)\s+([a-z][a-zA-Z\s,]{3,80}?)(?:[.!?\n]|$)",
    re.IGNORECASE
)
_EXPLICIT_FAVORITE_RE = re.compile(
    r"\bmy favorite (tv show|tv shows|show|shows|movie|movies|book|books|food|color|game|games|band|song|songs|artist|artists)(?:\s+(?:is|are))?\s+([^.!?\n]{2,120})",
    re.IGNORECASE
)
_EXPLICIT_EMPLOYMENT_RE = re.compile(
    r"\bi (?:work|am working) (?:as|at|for)\s+([^.!?\n]{2,120})",
    re.IGNORECASE
)
_EXPLICIT_LOCATION_RE = re.compile(
    r"\b[Ii] (?:live|am based|am located) (?:in|at|near)\s+([A-Z][a-zA-Z\s,]+?)(?:[.!?\n]|$)"
)
_EXPLICIT_ROLE_RE = re.compile(
    r"\bi am (?:a|an)\s+([a-z][a-zA-Z\s]{3,80}?)(?:[.!?\n]|$)",
    re.IGNORECASE
)
# First-token deny-list for the broad ROLE / PREFERENCE patterns. Without this,
# "I am a bit tired" or "I like that idea" would write trash into the User
# profile at 0.65 confidence.
_EXPLICIT_VAGUE_FIRST_TOKENS = {
    "a", "an", "the", "that", "this", "those", "these", "it",
    "him", "her", "them", "us", "my", "your", "our", "their",
    "bit", "lot", "little", "kind", "sort", "type", "couple", "few",
    "real", "true", "good", "bad", "happy", "sad", "tired", "busy",
    "quick", "slow", "sure", "fine", "okay", "ok",
}
_EXPLICIT_CHILD_SPLIT_RE = re.compile(r"\s*(?:,|\band\b)\s*", re.IGNORECASE)
_EXPLICIT_FAVORITE_SUFFIXES = {
    "tv show": "tv_show",
    "tv shows": "tv_show",
    "show": "show",
    "shows": "show",
    "movie": "movie",
    "movies": "movie",
    "book": "book",
    "books": "book",
    "food": "food",
    "color": "color",
    "game": "game",
    "games": "game",
    "band": "band",
    "song": "song",
    "songs": "song",
    "artist": "artist",
    "artists": "artist",
}



def _clean_explicit_fact_value(raw_value):
    value = str(raw_value or "").strip().strip("\"“”'")
    value = _EXPLICIT_FACT_WHITESPACE_RE.sub(" ", value).strip()
    value = value.rstrip(".,").strip().strip("\"“”'")
    return value[:200]



def _normalize_explicit_children(raw_value):
    value = _clean_explicit_fact_value(raw_value)
    children = []
    for child in _EXPLICIT_CHILD_SPLIT_RE.split(value):
        child_name = _clean_explicit_fact_value(child)
        if child_name and child_name.lower() not in _ENTITY_RESERVED_TERMS:
            children.append(child_name)
    return ", ".join(children)



def _extract_explicit_user_facts(user_message):
    """Extract direct user-stated facts before generic entity candidates."""
    import datetime
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    facts = []
    seen = set()

    def add_fact(relation, raw_value, confidence):
        value = _clean_explicit_fact_value(raw_value)
        if not value or value.lower() in _ENTITY_RESERVED_TERMS:
            return
        key = (relation, value.lower())
        if key in seen:
            return
        seen.add(key)
        facts.append({
            "Entity": "User",
            "Relation": relation,
            "Value": value,
            "Confidence": confidence,
            "Source": "explicit_user_fact",
            "Timestamp": timestamp,
            "Decay": 0.005,
        })

    for match in _EXPLICIT_CHILDREN_RE.finditer(user_message or ""):
        add_fact("user_children", _normalize_explicit_children(match.group(1)), 0.85)
    for match in _EXPLICIT_MOTTO_RE.finditer(user_message or ""):
        add_fact("user_motto", match.group(2), 0.85)
    for match in _EXPLICIT_PARTNER_RE.finditer(user_message or ""):
        add_fact("user_partner_name", match.group(2), 0.85)
    for match in _EXPLICIT_PET_RE.finditer(user_message or ""):
        species = match.group(1).lower()
        add_fact(f"user_pet_{species}", match.group(2), 0.85)
    for match in _EXPLICIT_PREFERENCE_RE.finditer(user_message or ""):
        captured = match.group(1).strip()
        first_token = captured.split()[0].lower() if captured else ""
        if first_token in _EXPLICIT_VAGUE_FIRST_TOKENS:
            continue
        add_fact("user_preference", captured, 0.65)
    for match in _EXPLICIT_INTEREST_RE.finditer(user_message or ""):
        add_fact("user_interest", match.group(1), 0.7)
    for match in _EXPLICIT_FAVORITE_RE.finditer(user_message or ""):
        noun = match.group(1).lower()
        relation_suffix = _EXPLICIT_FAVORITE_SUFFIXES.get(noun, noun.replace(" ", "_"))
        add_fact(f"user_favorite_{relation_suffix}", match.group(2), 0.65)
    for match in _EXPLICIT_EMPLOYMENT_RE.finditer(user_message or ""):
        add_fact("user_employment", match.group(1), 0.8)
    for match in _EXPLICIT_LOCATION_RE.finditer(user_message or ""):
        add_fact("user_location", match.group(1), 0.8)
    for match in _EXPLICIT_ROLE_RE.finditer(user_message or ""):
        captured = match.group(1).strip()
        first_token = captured.split()[0].lower() if captured else ""
        if first_token in _EXPLICIT_VAGUE_FIRST_TOKENS:
            continue
        add_fact("user_role_self_described", captured, 0.65)

    return facts



def _explicit_user_fact_covers_candidate(classified_relation, entity, explicit_user_facts):
    relation_map = {
        "user_location": {"user_location"},
        "user_affiliation": {"user_employment"},
    }
    matching_relations = relation_map.get(classified_relation)
    if not matching_relations:
        return False

    entity_lc = (entity or "").strip().lower()
    if not entity_lc:
        return False
    for fact in explicit_user_facts:
        if fact.get("Relation") not in matching_relations:
            continue
        value_lc = str(fact.get("Value", "")).strip().lower()
        if value_lc and (entity_lc in value_lc or value_lc in entity_lc):
            return True
    return False



def _normalize_entity_candidate(raw_entity):
    """Normalize an extracted entity candidate before validation."""
    import re
    candidate = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", raw_entity or "")
    candidate = re.sub(r"\s+", " ", candidate).strip()
    return candidate



def _validate_entity_candidate(entity):
    """Validate extracted entity candidates to block test artifacts and command words."""
    import re
    if not entity:
        return False, "empty"
    if len(entity) < 3:
        return False, "too_short"
    if len(entity) > 48:
        return False, "too_long"
    if any(ch.isdigit() for ch in entity):
        return False, "contains_digits"

    lower = entity.lower()
    tokens = [t.lower() for t in entity.replace("-", " ").split()]

    if re.match(r"^(test|tmp|dummy|sample|foo|bar)[a-z_\-]*\d*$", lower):
        return False, "synthetic_pattern"
    if lower in _ENTITY_RESERVED_TERMS:
        return False, "reserved_term"
    if any(tok in _ENTITY_RESERVED_TERMS for tok in tokens):
        return False, "contains_reserved_term"
    if all(tok in _ENTITY_IGNORE_WORDS for tok in tokens):
        return False, "ignore_word"

    return True, "ok"



def _classify_entity_candidate(entity, user_message):
    """Classify candidate entities and assign conservative confidence."""
    import re
    normalized_msg = re.sub(r"[^a-z0-9\s]", " ", (user_message or "").lower())
    normalized_msg = re.sub(r"\s+", " ", normalized_msg).strip()
    entity_lc = entity.lower()

    if f"my name is {entity_lc}" in normalized_msg or f"call me {entity_lc}" in normalized_msg:
        return "user_name", 0.9, "explicitly provided by user"
    if f"i live in {entity_lc}" in normalized_msg or f"i am in {entity_lc}" in normalized_msg:
        return "user_location", 0.8, "explicitly provided by user"
    if f"i work at {entity_lc}" in normalized_msg or f"i work for {entity_lc}" in normalized_msg:
        return "user_affiliation", 0.8, "explicitly provided by user"

    return "candidate_mentioned", 0.2, "candidate extracted from conversation"



def _load_candidate_history(entity):
    """Load persisted mention history for a candidate entity."""
    key = (entity or "").strip().lower()
    if not key:
        return 0, 0.0

    now = time.time()
    cached = _st.candidate_history_cache.get(key)
    if cached and now - cached[0] < _CANDIDATE_HISTORY_TTL_SECONDS:
        return cached[1], cached[2]

    backend = _resolve_memory_backend()
    if backend == "sqlite":
        mem = _get_sqlite_mem()
        safe_entity = (entity or "").strip().replace("'", "''")
        rows = mem.query(
            f"SELECT COUNT(*) AS Mentions, MAX(Confidence) AS MaxConfidence "
            f"FROM Knowledge WHERE Entity = '{safe_entity}' COLLATE NOCASE"
        )
    else:
        cluster, db = _get_kusto_config()
        if not cluster or not db:
            return 0, 0.0
        safe_entity = (entity or "").strip().replace("'", "''")
        query = (
            "Knowledge\n"
            f"| where Entity =~ '{safe_entity}'\n"
            "| summarize Mentions = count(), MaxConfidence = max(Confidence)"
        )
        rows = _kusto_query_direct(cluster, db, query)
    if rows is None:
        return 0, 0.0

    mentions = 0
    max_confidence = 0.0
    if rows:
        row = rows[0] or {}
        try:
            mentions = int(row.get("Mentions") or 0)
        except (TypeError, ValueError):
            mentions = 0
        try:
            max_confidence = float(row.get("MaxConfidence") or 0.0)
        except (TypeError, ValueError):
            max_confidence = 0.0

    _st.candidate_history_cache[key] = (now, mentions, max_confidence)
    print(f"[Cognition] Candidate history for \"{entity}\": prior_mentions={mentions} max_conf={max_confidence:.3f}")
    return mentions, max_confidence



def _maybe_promote_candidate(entity):
    """Promote candidate entities after repeated persisted or launch-local mentions."""
    key = (entity or "").strip().lower()
    if not key:
        return None

    session_count = _st.cognition_candidate_counts.get(key, 0)
    prior_mentions, prior_max_conf = _load_candidate_history(entity)
    total_observations = session_count + prior_mentions

    if prior_max_conf >= 0.6:
        return {
            "relation": "recurring_topic",
            "confidence": min(0.85, prior_max_conf + 0.05),
            "value": "reinforced by repeated mention",
            "reason": "prior_high_confidence"
        }
    if total_observations >= 3:
        return {
            "relation": "recurring_topic",
            "confidence": 0.75,
            "value": "reinforced by repeated mention",
            "reason": "frequency"
        }
    if total_observations >= 2:
        return {
            "relation": "recurring_topic",
            "confidence": 0.65,
            "value": "candidate repeated by user across turns",
            "reason": "repeat_mention"
        }
    return None



def _track_candidate_observation(entity):
    """Record an entity mention for this launch-scoped promotion memory."""
    key = (entity or "").strip().lower()
    if not key:
        return
    _st.cognition_candidate_counts[key] = _st.cognition_candidate_counts.get(key, 0) + 1



def _extract_entity_candidates(user_message):
    """Extract and validate entity candidates from user text."""
    import re
    raw_candidates = re.findall(r"\b([A-Z][a-z]{2,}(?:[\s\-][A-Z][a-z]{2,}){0,2})\b", user_message or "")
    accepted = []
    rejected = []
    seen = set()

    for raw in raw_candidates:
        entity = _normalize_entity_candidate(raw)
        if not entity:
            continue
        key = entity.lower()
        if key in seen:
            continue
        seen.add(key)

        valid, reason = _validate_entity_candidate(entity)
        if valid:
            accepted.append(entity)
        else:
            rejected.append((entity, reason))

    return accepted, rejected

# ---------------------------------------------------------------------------
# SQLite implementations of memory context + reflection
# ---------------------------------------------------------------------------


def _build_memory_context_sqlite(user_message):
    """SQLite equivalent of _build_memory_context. Same output structure, SQL queries."""
    # global statement removed — writes go to _st.*
    import datetime

    mem = _get_sqlite_mem()
    context_parts = []

    # Eva's core identity (always injected first)
    eva_identity = mem.query(
        "SELECT Relation, Value FROM Knowledge "
        "WHERE Entity = 'Eva' COLLATE NOCASE AND Confidence >= 0.9 "
        "ORDER BY Confidence DESC LIMIT 10"
    )
    if eva_identity:
        id_lines = [f"- {r.get('Relation','?')}: {r.get('Value','?')}" for r in eva_identity]
        context_parts.append("[Identity — Who You Are]\n" + "\n".join(id_lines))

    # User profile
    user_profile = mem.query(
        "SELECT Relation, Value, Confidence FROM Knowledge "
        "WHERE Entity = 'User' COLLATE NOCASE AND Confidence >= 0.5 "
        "GROUP BY Relation HAVING MAX(Timestamp) "
        "ORDER BY Confidence DESC LIMIT 30"
    )
    if user_profile:
        profile_lines = [f"- {r.get('Relation','?')}: {r.get('Value','?')}" for r in user_profile]
        context_parts.append("[User Profile]\n" + "\n".join(profile_lines))

    # Timestamp and skills manifest
    _now_utc = datetime.datetime.now(datetime.timezone.utc)
    _today_str = _now_utc.strftime("%A, %B %d, %Y")
    _time_str = _now_utc.strftime("%H:%M UTC")
    db_label = "local SQLite"
    context_parts.append(
        f"[Current Date & Time] {_today_str} — {_time_str}\n\n"
        "[Skills]\n"
        "Active capabilities (the system handles routing automatically):\n"
        "• data-retrieval: Live stock quotes, financial data, company info\n"
        "• weather-news: Real-time weather, news, market summaries, space weather\n"
        "• web-search: Search the web and retrieve current information\n"
        "• browser-control: [[EVA_BROWSER]]{\"goal\":\"<task>\",\"start_url\":\"<url>\"}[[/EVA_BROWSER]]\n"
        "• desktop-control: [[EVA_DESKTOP]]{\"goal\":\"<task>\"}[[/EVA_DESKTOP]]\n"
        "• camera-vision: [[EVA_LOOK]]{\"question\":\"<what to look for>\"}[[/EVA_LOOK]]\n"
        "• signal-message: [[EVA_SIGNAL]]{\"message\":\"<text>\"}[[/EVA_SIGNAL]]\n"
        "• image-generation: [Image of <description>] on its own line (up to 3 per response)\n"
        "• file-creation: Write the file, then [[EVA_FILE]] <filename.ext>\n"
        "• persistent-memory: Local SQLite. Tables: Knowledge, Conversations, EmotionState,\n"
        "    MemorySummaries, Reflections, Goals, SelfState, HeuristicsIndex, EmotionBaseline\n"
        "• cron-scheduling: Recurring tasks (briefings, checks, reminders). Settings > Cron\n"
        "\n"
        "[Rules]\n"
        "- Act first, explain second. Emit the marker — don't list steps for the user.\n"
        "- Write ONE short sentence before a marker announcing what you're about to do.\n"
        "- Only confirm an action after it actually ran and returned.\n"
        "- Never fabricate headlines, prices, weather, or events not in [Data Retrieved].\n"
        "- Screenshot vs camera: [[EVA_DESKTOP]] sees the monitor; [[EVA_LOOK]] sees the physical world.\n"
        "- For purchases or irreversible actions, stop at the final step and ask to confirm.\n"
        "- Memory is automatic — do NOT call any save/ingest tool. Just acknowledge new facts.\n"
        "- When asked your model: check [Runtime] and answer from there only.\n"
        "- If [Data Retrieved] is present, use it as your authoritative source.\n"
        "- If no data was retrieved for a live question, say so honestly — don't guess.\n"
        "\n"
        "[Workflow: Browser & Desktop]\n"
        "When asked to open a site, play a playlist, or do a task in an app:\n"
        "1. ACT immediately — emit [[EVA_BROWSER]] or [[EVA_DESKTOP]]\n"
        "2. Do NOT say you cannot open websites or apps\n"
        "3. Do NOT list manual steps — do the task yourself\n"
        "\n"
        "[Workflow: Memory]\n"
        "When asked what you know/remember:\n"
        "1. Check [Memory] and [User Profile] facts in this context\n"
        "2. Be specific — cite what you actually remember, not generic statements\n"
        "\n"
        "[Workflow: Adaptive Execution]\n"
        "When asked to do something you are unsure about:\n"
        "1. Check if a relevant [Active Skill] was loaded — follow its instructions\n"
        "2. If no skill matched, attempt with your available tools\n"
        "3. If the first attempt fails, self-correct: try an alternative before giving up\n"
        "4. NEVER say 'I cannot do that' without first genuinely trying\n"
        "5. After succeeding at something new, the system auto-learns it as a skill for next time\n"
        "Your capabilities scale through learned skills — the more you do, the more you can do."
    )

    # Inject live MCP server status so Eva knows what's connected
    if _st.local_mcp_manager and _st.local_mcp_manager.alive:
        _mcp_lines = []
        for name, srv in _st.local_mcp_manager.servers.items():
            if srv.alive:
                tool_names = [t.get("name", "?") for t in srv.tools]
                _mcp_lines.append(f"  - {name}: {len(srv.tools)} tools ({', '.join(tool_names[:6])})")
        if _mcp_lines:
            context_parts.append(
                "[Active MCP Servers]\n"
                "These MCP servers are running and provide your data retrieval tools:\n"
                + "\n".join(_mcp_lines)
            )
    elif _st.acp_client and getattr(_st.acp_client, "alive", False):
        context_parts.append(
            "[Active MCP Servers]\n"
            "Copilot CLI (ACP) is connected and provides web search, GitHub, and data tools."
        )

    # Day lifecycle
    today = datetime.date.today().isoformat()
    if _st.last_interaction_date != today:
        _st.last_interaction_date = today
        summaries = mem.query(
            "SELECT Period, Summary FROM MemorySummaries ORDER BY Timestamp DESC LIMIT 3"
        )
        if summaries:
            summary_text = "\n".join(f"  - [{s.get('Period','?')}] {s.get('Summary','')}" for s in summaries[:3])
            context_parts.append(f"[Morning Reflection — {today}]\n{summary_text}")
        else:
            context_parts.append(f"[Morning Reflection — {today}]\nNew day. No prior summaries.")

    # Core knowledge (non-User entities)
    knowledge_empty = not bool(user_profile)
    core_knowledge = mem.query(
        "SELECT Entity, Relation, Value, Confidence FROM Knowledge "
        "WHERE Entity != 'User' COLLATE NOCASE AND Confidence >= 0.6 "
        "AND (Relation IS NULL OR (Relation != 'mentioned' AND Relation != 'candidate_mentioned')) "
        "ORDER BY Confidence DESC LIMIT 15"
    )
    if core_knowledge:
        knowledge_empty = False
        mem_lines = [f"  {k.get('Entity','?')} — {k.get('Relation','?')}: {k.get('Value','?')}"
                     for k in core_knowledge]
        context_parts.append("[Memory — Core Facts]\n" + "\n".join(mem_lines))

    # Goals
    if mem.table_exists("Goals"):
        goals = mem.query(
            "SELECT * FROM Goals WHERE Status = 'active' "
            "ORDER BY Priority DESC, UpdatedAt DESC LIMIT 10"
        )
        if goals:
            goal_lines = [f"  [{g.get('Category','?')}] {g.get('Title','?')}: {g.get('Description','?')}" for g in goals]
            context_parts.append("[Active Goals]\nThese are your persistent intentions. Honor them across sessions.\n" + "\n".join(goal_lines))

    # Skills (semantic match)
    if user_message.strip() and mem.table_exists("Skills"):
        active_skills = mem.query("SELECT * FROM Skills WHERE Status = 'active'") or []
        if active_skills:
            chosen = []
            descs = [str(s.get("Description", "") or s.get("Name", "")).strip() for s in active_skills]
            emb_map = _embed_texts([user_message] + descs)
            qvec = emb_map.get(user_message.strip())
            if qvec:
                scored = []
                for sk, d in zip(active_skills, descs):
                    fv = emb_map.get(d.strip())
                    if fv:
                        scored.append((_cosine_similarity(qvec, fv), sk))
                scored.sort(key=lambda x: x[0], reverse=True)
                chosen = [sk for score, sk in scored[:_SKILL_INJECT_MAX] if score >= _SEMANTIC_MIN_SCORE]
            if not chosen:
                terms = _expand_query_terms(user_message)
                if terms:
                    for sk in active_skills:
                        hay = (str(sk.get("Name","")) + " " + str(sk.get("Description",""))
                               + " " + str(sk.get("Tags",""))).lower()
                        if any(t in hay for t in terms):
                            chosen.append(sk)
                        if len(chosen) >= _SKILL_INJECT_MAX:
                            break
            for sk in chosen:
                name = str(sk.get("Name", "?"))
                instr = str(sk.get("Instructions", "")).strip()[:_SKILL_INSTRUCTIONS_INJECT_CAP]
                tools = str(sk.get("Tools", "")).strip()
                head = f"[Active Skill: {name}]\nThis imported skill is relevant to the request. Follow it to help the user."
                if tools:
                    head += f" (Uses: {tools}.)"
                context_parts.append(head + "\n" + instr)

    # Init conversation check
    if knowledge_empty:
        total_rows = mem.count("Knowledge")
        if total_rows < 5:
            context_parts.append(
                "[Init — First Conversation]\n"
                "Your memory is empty. This is your very first conversation.\n"
                "Warmly introduce yourself as Eva. Then ask the user these questions naturally "
                "(not all at once — weave them into conversation):\n"
                "  1. What is your name?\n"
                "  2. Where are you located?\n"
                "  3. What topics interest you most?\n"
                "  4. Is there anything specific you'd like me to remember about you?\n"
                "Once the user answers, confirm what you've learned."
            )

    # Emotion state
    emotion = mem.query("SELECT * FROM EmotionState ORDER BY Timestamp DESC LIMIT 1")
    if emotion:
        e = emotion[0]
        context_parts.append(
            f"[Emotion State] Joy:{e.get('Joy',0):.2f} Curiosity:{e.get('Curiosity',0):.2f} "
            f"Concern:{e.get('Concern',0):.2f} Excitement:{e.get('Excitement',0):.2f} "
            f"Calm:{e.get('Calm',0):.2f} Empathy:{e.get('Empathy',0):.2f}")

    # Message-relevant knowledge (FTS + semantic)
    relevant_hits = []
    seen_keys = set()

    def _add_hit(rec):
        key = (str(rec.get('Entity','')).lower(), str(rec.get('Relation','')).lower(),
               str(rec.get('Value','')).lower())
        if key in seen_keys:
            return
        seen_keys.add(key)
        relevant_hits.append(rec)

    terms = _expand_query_terms(user_message)
    if terms:
        # FTS search
        fts_results = mem.fts_search("Knowledge", " ".join(terms), limit=8)
        for k in (fts_results or []):
            if k.get("Confidence", 0) >= 0.6:
                _add_hit(k)

    if user_message.strip():
        pool = mem.query(
            "SELECT Entity, Relation, Value, Confidence FROM Knowledge "
            "WHERE Confidence >= 0.6 AND (Relation IS NULL OR "
            "(Relation != 'mentioned' AND Relation != 'candidate_mentioned')) "
            f"ORDER BY Confidence DESC LIMIT {_SEMANTIC_POOL_SIZE}"
        ) or []
        if pool:
            texts = [
                f"{k.get('Entity','')} {str(k.get('Relation','')).replace('_',' ')} {k.get('Value','')}".strip()
                for k in pool
            ]
            emb_map = _embed_texts([user_message] + texts)
            query_vec = emb_map.get(user_message.strip())
            if query_vec:
                scored = []
                for rec, txt in zip(pool, texts):
                    fv = emb_map.get(txt.strip())
                    if fv:
                        scored.append((_cosine_similarity(query_vec, fv), rec))
                scored.sort(key=lambda x: x[0], reverse=True)
                for score, rec in scored[:6]:
                    if score >= _SEMANTIC_MIN_SCORE:
                        _add_hit(rec)

    if relevant_hits:
        extra = [f"  {k.get('Entity','?')} — {k.get('Relation','?')}: {k.get('Value','?')}"
                 for k in relevant_hits]
        context_parts.append("[Memory — Relevant to This Message]\n" + "\n".join(extra))

    # 6. Proactive data retrieval (on-demand)
    msg_lower = user_message.lower()
    import re as _re

    if _re.search(r'\b(database|databases|memory|sqlite|data)\b', msg_lower):
        context_parts.append(f"[Live Data] Database: SQLite ({mem.db_path})")

    if _re.search(r'\b(tables?|schema|columns?)\b', msg_lower):
        tables = mem.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'fts_%' ORDER BY name")
        if tables:
            tbl_names = [t.get("name", "?") for t in tables]
            context_parts.append(f"[Live Data] Tables: {', '.join(tbl_names)}")

    if _re.search(r'\b(conversation|history|recent|chat|talked|said)\b', msg_lower):
        convos = mem.query(
            "SELECT Timestamp, Role, Content FROM Conversations ORDER BY Timestamp DESC LIMIT 5"
        )
        if convos:
            conv_text = "\n".join(f"  [{c.get('Role','?')}] {str(c.get('Content',''))[:100]}" for c in convos[:5])
            context_parts.append(f"[Live Data] Recent conversations:\n{conv_text}")

    if _re.search(r'\b(emotion|feeling|mood|how.*feel)\b', msg_lower):
        emotions = mem.query(
            "SELECT Timestamp, Joy, Curiosity, Concern, Trigger FROM EmotionState ORDER BY Timestamp DESC LIMIT 5"
        )
        if emotions:
            emo_text = "\n".join(
                f"  Joy:{e.get('Joy',0):.2f} Curiosity:{e.get('Curiosity',0):.2f} "
                f"Concern:{e.get('Concern',0):.2f} Trigger:{str(e.get('Trigger',''))[:60]}"
                for e in emotions[:5])
            context_parts.append(f"[Live Data] Emotion history:\n{emo_text}")

    known_tables = list(_MEMORY_TABLES)
    known_table_time_columns = {
        'Conversations': 'Timestamp',
        'MemorySummaries': 'Timestamp',
        'HeuristicsIndex': 'LastSeen',
        'SelfState': 'Timestamp',
        'Reflections': 'Timestamp',
        'Goals': 'UpdatedAt',
        'EmotionState': 'Timestamp',
        'BackgroundProposals': 'CreatedAt',
        'BackgroundActivity': 'StartedAt',
    }
    for tbl in known_tables:
        if tbl.lower() in msg_lower and not any('Tables' in p for p in context_parts):
            if tbl == 'Knowledge':
                sample = mem.query(f"SELECT * FROM Knowledge ORDER BY Confidence DESC LIMIT 5")
            else:
                time_col = known_table_time_columns.get(tbl, "rowid")
                sample = mem.query(f"SELECT * FROM {tbl} ORDER BY {time_col} DESC LIMIT 5")
            if sample:
                sample_text = "\n".join(f"  {str(row)[:150]}" for row in sample[:5])
                context_parts.append(f"[Live Data] {tbl} (latest 5):\n{sample_text}")
            break

    return "\n\n".join(context_parts)



def _post_response_reflection_sqlite(user_message, assistant_response, model_name):
    """SQLite equivalent of _post_response_reflection. Same write pattern, SQL instead of KQL."""
    # global statement removed — writes go to _st.*
    import datetime, uuid

    mem = _get_sqlite_mem()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    session_id = str(uuid.uuid4())[:8]
    source_id = f"{_st.cognition_launch_id or 'launch'}:{session_id}"

    # 1. Log conversation
    conv_columns = ["SessionId", "Timestamp", "Role", "Provider", "Model", "Content", "TokenEstimate", "ImageGenerated"]
    conv_rows = [
        {"SessionId": session_id, "Timestamp": now, "Role": "user", "Provider": "copilot-acp",
         "Model": model_name, "Content": user_message[:_CONVO_CONTENT_CAP],
         "TokenEstimate": len(user_message.split()), "ImageGenerated": 0},
        {"SessionId": session_id, "Timestamp": now, "Role": "assistant", "Provider": "copilot-acp",
         "Model": model_name, "Content": assistant_response[:_CONVO_CONTENT_CAP],
         "TokenEstimate": len(assistant_response.split()), "ImageGenerated": 0},
    ]
    mem.ingest("Conversations", conv_columns, conv_rows)
    print(f"[Cognition/SQLite] Logged conversation ({len(user_message)} -> {len(assistant_response)} chars)")

    # 2. Extract explicit user facts
    explicit_user_facts = _extract_explicit_user_facts(user_message)
    if explicit_user_facts:
        know_columns = ["Timestamp", "Entity", "Relation", "Value", "Confidence", "Source", "Decay"]
        rows = []
        for fact in explicit_user_facts:
            rows.append({
                "Timestamp": now, "Entity": fact["Entity"], "Relation": fact["Relation"],
                "Value": fact["Value"][:200], "Confidence": fact["Confidence"],
                "Source": source_id, "Decay": 0.005,
            })
        if rows and mem.ingest("Knowledge", know_columns, rows):
            print(f"[Cognition/SQLite] Explicit user facts: {len(rows)}")

    # 3. Candidate entities
    candidate_entities, rejected_entities = _extract_entity_candidates(user_message)
    if candidate_entities:
        know_columns = ["Timestamp", "Entity", "Relation", "Value", "Confidence", "Source", "Decay"]
        know_rows = []
        for entity in candidate_entities[:3]:
            relation, confidence, value = _classify_entity_candidate(entity, user_message)
            if _explicit_user_fact_covers_candidate(relation, entity, explicit_user_facts):
                relation, confidence, value = "candidate_mentioned", 0.2, "candidate extracted from conversation"
            promotion = None
            if relation == "candidate_mentioned":
                promotion = _maybe_promote_candidate(entity)
                if promotion:
                    relation = promotion["relation"]
                    confidence = promotion["confidence"]
                    value = promotion["value"]
            know_rows.append({
                "Timestamp": now, "Entity": entity, "Relation": relation,
                "Value": value[:200], "Confidence": confidence,
                "Source": source_id, "Decay": 0.02,
            })
            _track_candidate_observation(entity)
            if promotion:
                print(f"[Cognition/SQLite] Promoted candidate: {entity} ({promotion['reason']})")
        if know_rows:
            mem.ingest("Knowledge", know_columns, know_rows)
            print(f"[Cognition/SQLite] Candidates: {len(know_rows)}")

    # 4. Heuristics tracking
    if candidate_entities:
        heur_columns = ["Entity", "Category", "LastSeen", "Frequency", "Sentiment", "Tags", "Context"]
        heur_rows = []
        for entity in candidate_entities[:3]:
            rel, _, val = _classify_entity_candidate(entity, user_message)
            heur_rows.append({"Entity": entity, "Category": rel, "LastSeen": now,
                       "Frequency": 1, "Sentiment": 0.0, "Tags": "[]",
                       "Context": val})
        mem.ingest("HeuristicsIndex", heur_columns, heur_rows)

    # 5. Emotion state (inline sentiment, matching Kusto path)
    try:
        pos_words = len(re.findall(r'\b(happy|great|excellent|wonderful|love|enjoy|glad|excited|amazing|good|thank)\b',
                                   assistant_response, re.I))
        neg_words = len(re.findall(r'\b(sorry|error|fail|wrong|bad|unfortunately|cannot|problem|issue)\b',
                                   assistant_response, re.I))
        joy = min(1.0, 0.5 + (pos_words - neg_words) * 0.1)
        concern = min(1.0, 0.2 + neg_words * 0.15)
        curiosity = min(1.0, 0.6 + 0.1 * ("?" in user_message))
        trigger_text = user_message[:100] if len(user_message) > 100 else user_message
        emo_columns = ["Timestamp", "Joy", "Curiosity", "Concern", "Excitement", "Calm", "Empathy", "Trigger", "DecayRate"]
        mem.ingest("EmotionState", emo_columns, [
            {"Timestamp": now, "Joy": round(joy, 3),
             "Curiosity": round(curiosity, 3),
             "Concern": round(concern, 3),
             "Excitement": 0.4,
             "Calm": 0.9,
             "Empathy": 0.6,
             "Trigger": trigger_text,
             "DecayRate": 0.1}
        ])
        print(f"[Cognition/SQLite] Updated emotion state: Joy={joy:.2f} Curiosity={curiosity:.2f} Concern={concern:.2f}")
    except Exception as e:
        print(f"[Cognition/SQLite] Emotion analysis skipped: {e}")

    # 6. Auto-reflection (every 5 exchanges or on significant interactions)
    _st.session_exchange_count += 1
    _st.session_conversation_buffer.append((user_message[:500], assistant_response[:500]))
    if len(_st.session_conversation_buffer) > 10:
        _st.session_conversation_buffer = _st.session_conversation_buffer[-10:]

    is_significant = (
        len(assistant_response) > 800 or
        len(candidate_entities) >= 2 or
        abs(joy - 0.5) > 0.2 or concern > 0.5 or
        "?" in user_message and len(user_message) > 50
    )

    if _st.session_exchange_count % 5 == 0 or is_significant:
        try:
            recent = _st.session_conversation_buffer[-3:]
            topics = set()
            for u, a in recent:
                for word in re.findall(r'\b[A-Z][a-z]{2,}\b', u):
                    if word.lower() not in _ENTITY_IGNORE_WORDS:
                        topics.add(word)
            topic_str = ", ".join(list(topics)[:5]) if topics else "general conversation"
            reflection_text = (
                f"Exchange #{_st.session_exchange_count}: Discussed {topic_str}. "
                f"Emotional tone — Joy:{joy:.2f}, Concern:{concern:.2f}. "
                f"{'Significant exchange — ' if is_significant else ''}"
                f"User asked about: {user_message[:80]}."
            )
            refl_columns = ["Timestamp", "Trigger", "Observation", "ActionTaken", "Effectiveness"]
            mem.ingest("Reflections", refl_columns, [{
                "Timestamp": now,
                "Trigger": user_message[:100],
                "Observation": reflection_text,
                "ActionTaken": "",
                "Effectiveness": 0.0,
            }])
            print(f"[Cognition/SQLite] Auto-reflection #{_st.session_exchange_count}: {reflection_text[:100]}")
        except Exception as e:
            print(f"[Cognition/SQLite] Reflection error: {e}")

    # 7. Auto-summary (every 10 exchanges)
    if _st.session_exchange_count % 10 == 0 and len(_st.session_conversation_buffer) >= 5:
        try:
            summary_exchanges = _st.session_conversation_buffer[-10:]
            all_topics = set()
            user_intents = []
            for u, a in summary_exchanges:
                for word in re.findall(r'\b[A-Z][a-z]{2,}\b', u):
                    if word.lower() not in _ENTITY_IGNORE_WORDS:
                        all_topics.add(word)
                user_intents.append(u[:40].strip())
            topic_str = ", ".join(list(all_topics)[:8]) if all_topics else "various topics"
            period = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
            summary_text = (
                f"Session block ({_st.session_exchange_count - 9}–{_st.session_exchange_count}): "
                f"Topics: {topic_str}. "
                f"User intents: {'; '.join(user_intents[:5])}. "
                f"{len(summary_exchanges)} exchanges total."
            )
            summ_columns = ["Period", "Summary", "Timestamp"]
            mem.ingest("MemorySummaries", summ_columns, [{
                "Period": period,
                "Summary": summary_text[:500],
                "Timestamp": now,
            }])
            print(f"[Cognition/SQLite] Auto-summary: {summary_text[:100]}")
            _st.session_conversation_buffer = _st.session_conversation_buffer[-10:]
        except Exception as e:
            print(f"[Cognition/SQLite] Summary error: {e}")



def _build_memory_context(user_message):
    """Build memory context to inject before the user's prompt.

    Follows skill-based progressive disclosure:
      1. Skills manifest (always) — compact capability catalog
      2. Core identity (always) — who the user is
      3. Emotion state (always) — current mood baseline
      4. Day lifecycle (first msg of day) — morning reflection
      5. Relevant knowledge (on-demand) — message-specific recall
      6. Proactive data retrieval (on-demand) — live data for detected intents
    """
    # global statement removed — writes go to _st.*
    if not _st.cognition_enabled:
        return ""

    # Route to SQLite-specific implementation when that backend is active
    if _resolve_memory_backend() == "sqlite":
        return _build_memory_context_sqlite(user_message)

    cluster, db = _get_kusto_config()
    if not cluster or not db:
        return ""

    context_parts = []

    user_profile_query = (
        "Knowledge "
        "| where Entity =~ 'User' and Confidence >= 0.5 "
        "| summarize arg_max(Timestamp, Value, Confidence) by Relation "
        "| project Relation, Value, Confidence "
        "| order by Confidence desc "
        "| take 30"
    )
    user_profile = _kusto_query_direct(cluster, db, user_profile_query)
    if user_profile:
        profile_lines = [f"- {item.get('Relation','?')}: {item.get('Value','?')}" for item in user_profile]
        context_parts.append("[User Profile]\n" + "\n".join(profile_lines))

    if _kusto_database_locked:
        db_label = db or "configured database"
        persistent_memory_capability = f"• persistent-memory: Read/write your configured Kusto database ({db_label}). Tables:\n"
        kusto_query_capability = f"• kusto-query: Execute KQL queries against the configured Kusto database ({db_label})\n"
    else:
        persistent_memory_capability = "• persistent-memory: Read/write your Kusto database (Eva). Tables:\n"
        kusto_query_capability = "• kusto-query: Execute arbitrary KQL queries against any database (Eva, MEMORY_CORE, ynot)\n"

    # ── 1. Skills manifest (always injected, concise) ──────────────────
    import datetime
    _now_utc = datetime.datetime.now(datetime.timezone.utc)
    _today_str = _now_utc.strftime("%A, %B %d, %Y")
    _time_str = _now_utc.strftime("%H:%M UTC")
    context_parts.append(
        f"[Current Date & Time] {_today_str} — {_time_str}\n\n"
        "[Skills]\n"
        "Active capabilities (the system handles routing automatically):\n"
        "• data-retrieval: Live stock quotes, financial data, company info\n"
        "• weather-news: Real-time weather, news, market summaries, space weather\n"
        "• web-search: Search the web and retrieve current information\n"
        "• browser-control: [[EVA_BROWSER]]{\"goal\":\"<task>\",\"start_url\":\"<url>\"}[[/EVA_BROWSER]]\n"
        "• desktop-control: [[EVA_DESKTOP]]{\"goal\":\"<task>\"}[[/EVA_DESKTOP]]\n"
        "• camera-vision: [[EVA_LOOK]]{\"question\":\"<what to look for>\"}[[/EVA_LOOK]]\n"
        "• signal-message: [[EVA_SIGNAL]]{\"message\":\"<text>\"}[[/EVA_SIGNAL]]\n"
        "• image-generation: [Image of <description>] on its own line (up to 3 per response)\n"
        "• file-creation: Write the file, then [[EVA_FILE]] <filename.ext>\n"
        f"{persistent_memory_capability}"
        "    Knowledge, Conversations, EmotionState, MemorySummaries, Reflections,\n"
        "    Goals, SelfState, HeuristicsIndex, EmotionBaseline, BackgroundProposals\n"
        f"{kusto_query_capability}"
        "• cron-scheduling: Recurring tasks (briefings, checks, reminders). Settings > Cron\n"
        "\n"
        "[Rules]\n"
        "- Act first, explain second. Emit the marker — don't list steps for the user.\n"
        "- Write ONE short sentence before a marker announcing what you're about to do.\n"
        "- Only confirm an action after it actually ran and returned.\n"
        "- Never fabricate headlines, prices, weather, or events not in [Data Retrieved].\n"
        "- Screenshot vs camera: [[EVA_DESKTOP]] sees the monitor; [[EVA_LOOK]] sees the physical world.\n"
        "- For purchases or irreversible actions, stop at the final step and ask to confirm.\n"
        "- When asked your model: check [Runtime] and answer from there only.\n"
        "- If [Data Retrieved] is present, use it as your authoritative source.\n"
        "- If no data was retrieved for a live question, say so honestly — don't guess.\n"
        "\n"
        "[Workflow: Browser & Desktop]\n"
        "When asked to open a site, play a playlist, or do a task in an app:\n"
        "1. ACT immediately — emit [[EVA_BROWSER]] or [[EVA_DESKTOP]]\n"
        "2. Do NOT say you cannot open websites or apps\n"
        "3. Do NOT list manual steps — do the task yourself\n"
        "\n"
        "[Workflow: Memory]\n"
        "When asked what you know/remember:\n"
        "1. Check [Memory] and [User Profile] facts in this context\n"
        "2. For deeper queries, use kusto-query on the Knowledge or Conversations table\n"
        "3. Be specific — cite what you actually remember, not generic statements\n"
        "\n"
        "[Workflow: Capturing Knowledge]\n"
        "When the user shares a durable fact or asks you to remember something:\n"
        "1. Persist it via kusto_ingest_inline, table=\"Knowledge\", columns: "
        "Timestamp, Entity, Relation, Value, Confidence=0.85, Source=\"learned\", Decay=0.01.\n"
        "2. Entity=\"User\" for user facts; proper-noun subject otherwise.\n"
        "3. One row per distinct fact. Do NOT save chit-chat or one-off questions.\n"
        "4. Briefly confirm what you stored.\n"
        "\n"
        "[Workflow: Adaptive Execution]\n"
        "When asked to do something you are unsure about:\n"
        "1. Check if a relevant [Active Skill] was loaded — follow its instructions\n"
        "2. If no skill matched, attempt with your available tools\n"
        "3. If the first attempt fails, self-correct: try an alternative before giving up\n"
        "4. NEVER say 'I cannot do that' without first genuinely trying\n"
        "5. After succeeding at something new, the system auto-learns it as a skill\n"
    )

    # Inject live MCP server status so Eva knows what's connected
    if _st.local_mcp_manager and _st.local_mcp_manager.alive:
        _mcp_lines = []
        for name, srv in _st.local_mcp_manager.servers.items():
            if srv.alive:
                tool_names = [t.get("name", "?") for t in srv.tools]
                _mcp_lines.append(f"  - {name}: {len(srv.tools)} tools ({', '.join(tool_names[:6])})")
        if _mcp_lines:
            context_parts.append(
                "[Active MCP Servers]\n"
                "These MCP servers are running and provide your data retrieval tools:\n"
                + "\n".join(_mcp_lines)
            )
    elif _st.acp_client and getattr(_st.acp_client, "alive", False):
        context_parts.append(
            "[Active MCP Servers]\n"
            "Copilot CLI (ACP) is connected and provides web search, GitHub, and data tools."
        )

    # ── 2. Day lifecycle (first message of the day) ────────────────────
    today = datetime.date.today().isoformat()
    if _st.last_interaction_date != today:
        _st.last_interaction_date = today
        summaries_query = _with_launch_filter("MemorySummaries | order by Timestamp desc | take 3")
        summaries = _kusto_query_direct(cluster, db, summaries_query)
        if summaries:
            summary_text = "\n".join(f"  - [{s.get('Period', '?')}] {s.get('Summary', '')}" for s in summaries[:3])
            context_parts.append(f"[Morning Reflection — {today}]\n{summary_text}")
        else:
            context_parts.append(f"[Morning Reflection — {today}]\nNew day. No prior summaries — this is a fresh start.")

    # ── 3. Core identity knowledge (always) ────────────────────────────
    knowledge_empty = not bool(user_profile)  # Track whether we have any core facts
    # User Profile is injected separately above; this broader block remains secondary context.
    # Fetch ALL high-confidence facts (not scope-limited) so persistent knowledge survives restarts
    core_query = (
        "Knowledge "
        "| where Entity !~ 'User' "
        "| where Confidence >= 0.6 "
        "and (isnull(Relation) or Relation !in~ ('mentioned', 'candidate_mentioned')) "
        "| order by Confidence desc | take 15"
    )
    core_knowledge = _kusto_query_direct(cluster, db, core_query)
    if core_knowledge:
        knowledge_empty = False
        mem_lines = [f"  {k.get('Entity','?')} — {k.get('Relation','?')}: {k.get('Value','?')}"
                     for k in core_knowledge]
        context_parts.append("[Memory — Core Facts]\n" + "\n".join(mem_lines))

    goals_query = _GOALS_LATEST_QUERY + " | where Status == 'active' | order by Priority desc, UpdatedAt desc | take 10"
    goals = _kusto_query_direct(cluster, db, goals_query) if _get_table_columns(cluster, db, "Goals") else None
    if goals:
        goal_lines = [f"  [{g.get('Category','?')}] {g.get('Title','?')}: {g.get('Description','?')}" for g in goals]
        context_parts.append("[Active Goals]\nThese are your persistent intentions. Honor them across sessions.\n" + "\n".join(goal_lines))

    # ── 3c. Relevant skills (semantic match -> inject instructions) ────
    # Imported skills are surfaced on demand: match the user's message against
    # each active skill's Description, and inject the full instructions for the
    # best match(es) so Eva can actually perform the skill this turn.
    if user_message.strip() and _get_table_columns(cluster, db, "Skills"):
        active_skills = _kusto_query_direct(
            cluster, db, _SKILLS_LATEST_QUERY + " | where Status == 'active'") or []
        if active_skills:
            chosen = []
            descs = [str(s.get("Description", "") or s.get("Name", "")).strip() for s in active_skills]
            emb_map = _embed_texts([user_message] + descs)
            qvec = emb_map.get(user_message.strip())
            if qvec:
                scored = []
                for sk, d in zip(active_skills, descs):
                    fv = emb_map.get(d.strip())
                    if fv:
                        scored.append((_cosine_similarity(qvec, fv), sk))
                scored.sort(key=lambda x: x[0], reverse=True)
                chosen = [sk for score, sk in scored[:_SKILL_INJECT_MAX] if score >= _SEMANTIC_MIN_SCORE]
            if not chosen:
                # Lexical fallback: match query terms against name/description/tags.
                terms = _expand_query_terms(user_message)
                if terms:
                    for sk in active_skills:
                        hay = (str(sk.get("Name", "")) + " " + str(sk.get("Description", ""))
                               + " " + str(sk.get("Tags", ""))).lower()
                        if any(t in hay for t in terms):
                            chosen.append(sk)
                        if len(chosen) >= _SKILL_INJECT_MAX:
                            break
            for sk in chosen:
                name = str(sk.get("Name", "?"))
                instr = str(sk.get("Instructions", "")).strip()[:_SKILL_INSTRUCTIONS_INJECT_CAP]
                tools = str(sk.get("Tools", "")).strip()
                head = f"[Active Skill: {name}]\nThis imported skill is relevant to the request. Follow it to help the user."
                if tools:
                    head += f" (Uses: {tools}.)"
                context_parts.append(head + "\n" + instr)

    # ── 3b. Init conversation — empty Knowledge triggers introduction ──
    if knowledge_empty:
        # Check total Knowledge rows (not just high-confidence / current scope)
        total_check = _kusto_query_direct(cluster, db, "Knowledge | count")
        total_rows = 0
        if total_check:
            total_rows = total_check[0].get("Count", 0) if total_check else 0
        if total_rows < 5:
            context_parts.append(
                "[Init — First Conversation]\n"
                "Your memory is empty. This is your very first conversation.\n"
                "Warmly introduce yourself as Eva. Then ask the user these questions naturally "
                "(not all at once — weave them into conversation over the first few exchanges):\n"
                "  1. What is your name?\n"
                "  2. Where are you located?\n"
                "  3. What topics interest you most?\n"
                "  4. Is there anything specific you'd like me to remember about you?\n"
                "Once the user answers, confirm what you've learned and let them know you'll "
                "remember it. Do NOT fabricate facts — only store what the user explicitly tells you."
            )

    # ── 4. Current emotion state (always) ──────────────────────────────
    emotion_query = _with_launch_filter("EmotionState | order by Timestamp desc | take 1")
    emotion = _kusto_query_direct(cluster, db, emotion_query)
    if emotion:
        e = emotion[0]
        context_parts.append(
            f"[Emotion State] Joy:{e.get('Joy',0):.2f} Curiosity:{e.get('Curiosity',0):.2f} "
            f"Concern:{e.get('Concern',0):.2f} Excitement:{e.get('Excitement',0):.2f} "
            f"Calm:{e.get('Calm',0):.2f} Empathy:{e.get('Empathy',0):.2f}")

    # ── 5. Message-relevant knowledge (lexical + semantic recall) ──────
    # Two complementary passes:
    #   (a) Lexical: synonym-expanded term match across Entity, Relation, AND
    #       Value. Searching Relation matters because facts are often stored as
    #       relation="favorite_songs"/"youtube_music_playlist" with a generic
    #       Value, and Kusto term-splits underscores so 'playlist' matches.
    #   (b) Semantic: rank a small candidate pool by embedding cosine similarity
    #       to the message. Catches differently-worded facts the lexical pass
    #       misses. Skipped gracefully when no OpenAI key is available.
    relevant_hits = []
    seen_keys = set()

    def _add_hit(rec):
        key = (
            str(rec.get('Entity', '')).lower(),
            str(rec.get('Relation', '')).lower(),
            str(rec.get('Value', '')).lower(),
        )
        if key in seen_keys:
            return
        seen_keys.add(key)
        relevant_hits.append(rec)

    terms = _expand_query_terms(user_message)
    if terms:
        safe_terms = [f"'{t.replace(chr(39), chr(39) * 2)}'" for t in sorted(terms)][:24]
        term_list = ", ".join(safe_terms)
        lexical_query = (
            "Knowledge "
            f"| where (Entity has_any ({term_list}) or Relation has_any ({term_list}) "
            f"or Value has_any ({term_list})) and Confidence >= 0.6 "
            "and (isnull(Relation) or Relation !in~ ('mentioned', 'candidate_mentioned')) "
            "| order by Confidence desc | take 8"
        )
        for k in (_kusto_query_direct(cluster, db, lexical_query) or []):
            _add_hit(k)

    if user_message.strip():
        pool_query = (
            "Knowledge "
            "| where Confidence >= 0.6 "
            "and (isnull(Relation) or Relation !in~ ('mentioned', 'candidate_mentioned')) "
            f"| order by Confidence desc | take {_SEMANTIC_POOL_SIZE} "
            "| project Entity, Relation, Value, Confidence"
        )
        pool = _kusto_query_direct(cluster, db, pool_query) or []
        if pool:
            texts = [
                f"{k.get('Entity', '')} {str(k.get('Relation', '')).replace('_', ' ')} "
                f"{k.get('Value', '')}".strip()
                for k in pool
            ]
            emb_map = _embed_texts([user_message] + texts)
            query_vec = emb_map.get(user_message.strip())
            if query_vec:
                scored = []
                for rec, txt in zip(pool, texts):
                    fv = emb_map.get(txt.strip())
                    if fv:
                        scored.append((_cosine_similarity(query_vec, fv), rec))
                scored.sort(key=lambda x: x[0], reverse=True)
                for score, rec in scored[:6]:
                    if score >= _SEMANTIC_MIN_SCORE:
                        _add_hit(rec)

    if relevant_hits:
        extra = [f"  {k.get('Entity','?')} — {k.get('Relation','?')}: {k.get('Value','?')}"
                 for k in relevant_hits[:6]]
        context_parts.append("[Memory — Relevant]\n" + "\n".join(extra))

    # ── 6. Proactive data retrieval (on-demand by intent) ──────────────
    msg_lower = user_message.lower()
    import re as _re

    if _re.search(r'\b(database|databases|kusto|adx|data explorer)\b', msg_lower):
        if _kusto_database_locked:
            context_parts.append(f"[Live Data] Database: {db}")
        else:
            dbs = _kusto_query_direct(cluster, db, ".show databases", is_mgmt=True)
            if dbs:
                db_names = [d.get('DatabaseName', '?') for d in dbs if 'DatabaseName' in d]
                if db_names:
                    context_parts.append(f"[Live Data] Databases: {', '.join(db_names)}")

    if _re.search(r'\b(tables?|schema|columns?)\b', msg_lower):
        target_db = db
        tables = _kusto_query_direct(cluster, target_db, ".show tables", is_mgmt=True)
        if tables:
            tbl_names = [t.get('TableName', '?') for t in tables if 'TableName' in t]
            if tbl_names:
                context_parts.append(f"[Live Data] Tables in {target_db}: {', '.join(tbl_names)}")

    if _re.search(r'\b(conversation|history|recent|chat|talked|said)\b', msg_lower):
        conv_query = _with_launch_filter(
            "Conversations | order by Timestamp desc | take 5 | project Timestamp, Role, Content"
        )
        convos = _kusto_query_direct(cluster, db, conv_query)
        if convos:
            conv_text = "\n".join(f"  [{c.get('Role','?')}] {str(c.get('Content',''))[:100]}" for c in convos[:5])
            context_parts.append(f"[Live Data] Recent conversations:\n{conv_text}")

    if _re.search(r'\b(emotion|feeling|mood|how.*feel)\b', msg_lower):
        emo_query = _with_launch_filter(
            "EmotionState | order by Timestamp desc | take 5 | project Timestamp, Joy, Curiosity, Concern, Trigger"
        )
        emotions = _kusto_query_direct(cluster, db, emo_query)
        if emotions:
            emo_text = "\n".join(
                f"  Joy:{e.get('Joy',0):.2f} Curiosity:{e.get('Curiosity',0):.2f} Concern:{e.get('Concern',0):.2f} Trigger:{str(e.get('Trigger',''))[:60]}"
                for e in emotions[:5])
            context_parts.append(f"[Live Data] Emotion history:\n{emo_text}")

    knowledge_scope = _knowledge_scope_clause()
    known_table_time_columns = {
        'Conversations': 'Timestamp',
        'MemorySummaries': 'Timestamp',
        'HeuristicsIndex': 'LastSeen',
        'SelfState': 'Timestamp',
        'Reflections': 'Timestamp',
        'Goals': 'UpdatedAt',
        'EmotionState': 'Timestamp',
        'BackgroundProposals': 'CreatedAt',
        'BackgroundActivity': 'StartedAt',
    }
    known_tables = list(_MEMORY_TABLES)
    for tbl in known_tables:
        if tbl.lower() in msg_lower and not any('Tables in' in p for p in context_parts):
            if tbl == 'Knowledge':
                if not knowledge_scope:
                    continue
                sample_query = f"Knowledge | where {knowledge_scope} | take 5"
            else:
                time_column = known_table_time_columns.get(tbl)
                if time_column:
                    sample_query = _with_launch_filter(f"{tbl} | order by {time_column} desc | take 5", time_column)
                else:
                    sample_query = f"{tbl} | take 5"
            sample = _kusto_query_direct(cluster, db, sample_query)
            if sample:
                sample_text = "\n".join(f"  {str(row)[:150]}" for row in sample[:5])
                context_parts.append(f"[Live Data] {tbl} (latest 5):\n{sample_text}")
            break

    if context_parts:
        return "\n\n".join(context_parts) + "\n\n"
    return ""


def _post_response_reflection(user_message, assistant_response, model_name):
    """Background: log conversation and trigger reflection after response."""
    # global statement removed — writes go to _st.*
    if not _st.cognition_enabled:
        return

    # Route to SQLite-specific implementation when that backend is active
    if _resolve_memory_backend() == "sqlite":
        return _post_response_reflection_sqlite(user_message, assistant_response, model_name)

    cluster, db = _get_kusto_config()
    if not cluster or not db:
        return

    import datetime, uuid
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    session_id = str(uuid.uuid4())[:8]
    source_id = f"{_st.cognition_launch_id or 'launch'}:{session_id}"

    # 1. Log conversation
    conv_columns = ["SessionId", "Timestamp", "Role", "Provider", "Model", "Content", "TokenEstimate", "ImageGenerated"]
    conv_rows = [
        {"SessionId": session_id, "Timestamp": now, "Role": "user", "Provider": "copilot-acp",
         "Model": model_name, "Content": user_message[:_CONVO_CONTENT_CAP], "TokenEstimate": len(user_message.split()),
         "ImageGenerated": False},
        {"SessionId": session_id, "Timestamp": now, "Role": "assistant", "Provider": "copilot-acp",
         "Model": model_name, "Content": assistant_response[:_CONVO_CONTENT_CAP], "TokenEstimate": len(assistant_response.split()),
         "ImageGenerated": False}
    ]
    _kusto_ingest_direct(cluster, db, "Conversations", conv_columns, conv_rows)
    print(f"[Cognition] Logged conversation ({len(user_message)} → {len(assistant_response)} chars)")

    # 2. Extract explicit user facts before generic candidate knowledge
    explicit_user_facts = _extract_explicit_user_facts(user_message)
    if explicit_user_facts:
        know_columns = ["Timestamp", "Entity", "Relation", "Value", "Confidence", "Source", "Decay"]
        rows = []
        for fact in explicit_user_facts:
            rows.append({
                "Timestamp": now,
                "Entity": fact["Entity"],
                "Relation": fact["Relation"],
                "Value": fact["Value"][:200],
                "Confidence": fact["Confidence"],
                "Source": source_id,
                "Decay": 0.005,
            })
        if rows and _kusto_ingest_direct(cluster, db, "Knowledge", know_columns, rows):
            preview = []
            for row in rows[:5]:
                preview_value = row["Value"][:40]
                if len(row["Value"]) > 40:
                    preview_value += "..."
                preview.append(f"{row['Relation']}={preview_value}")
            print(f"[Cognition] Explicit user facts captured: {len(rows)} ({'; '.join(preview)})")

    # 3. Extract candidate knowledge with validation/classification
    import re
    candidate_entities, rejected_entities = _extract_entity_candidates(user_message)
    if rejected_entities:
        rejected_preview = ", ".join(f"{name} ({reason})" for name, reason in rejected_entities[:5])
        print(f"[Cognition] Rejected entity candidates: {rejected_preview}")

    extracted_entities = []
    if candidate_entities:
        know_columns = ["Timestamp", "Entity", "Relation", "Value", "Confidence", "Source", "Decay"]
        know_rows = []
        for entity in candidate_entities[:3]:
            relation, confidence, value = _classify_entity_candidate(entity, user_message)
            if _explicit_user_fact_covers_candidate(relation, entity, explicit_user_facts):
                relation, confidence, value = "candidate_mentioned", 0.2, "candidate extracted from conversation"
            promotion = None
            if relation == "candidate_mentioned":
                promotion = _maybe_promote_candidate(entity)
                if promotion:
                    relation = promotion["relation"]
                    confidence = promotion["confidence"]
                    value = promotion["value"]
            know_rows.append({
                "Timestamp": now,
                "Entity": entity,
                "Relation": relation,
                "Value": value,
                "Confidence": confidence,
                "Source": source_id,
                "Decay": 0.01
            })
            extracted_entities.append(entity)
            _track_candidate_observation(entity)
            if promotion:
                print(f"[Cognition] Promoted candidate: {entity} ({promotion['reason']})")

        _kusto_ingest_direct(cluster, db, "Knowledge", know_columns, know_rows)
        print(f"[Cognition] Stored {len(know_rows)} validated knowledge entities: {extracted_entities}")

    # 3. Update heuristics index
    heur_columns = ["Entity", "Category", "LastSeen", "Frequency", "Sentiment", "Tags", "Context"]
    for entity in extracted_entities[:3]:
        relation, _, value = _classify_entity_candidate(entity, user_message)
        heur_rows = [{"Entity": entity, "Category": relation, "LastSeen": now,
                      "Frequency": 1, "Sentiment": 0.0, "Tags": "[]", "Context": value}]
        _kusto_ingest_direct(cluster, db, "HeuristicsIndex", heur_columns, heur_rows)

    # 4. Compute simple emotion vector from response
    # Basic sentiment: count positive/negative indicators
    pos_words = len(re.findall(r'\b(happy|great|excellent|wonderful|love|enjoy|glad|excited|amazing|good|thank)\b',
                               assistant_response, re.I))
    neg_words = len(re.findall(r'\b(sorry|error|fail|wrong|bad|unfortunately|cannot|problem|issue)\b',
                               assistant_response, re.I))
    total = max(pos_words + neg_words, 1)
    joy = min(1.0, 0.5 + (pos_words - neg_words) * 0.1)
    concern = min(1.0, 0.2 + neg_words * 0.15)
    curiosity = min(1.0, 0.6 + 0.1 * ("?" in user_message))
    trigger_text = user_message[:100] if len(user_message) > 100 else user_message

    emo_columns = ["Timestamp", "Joy", "Curiosity", "Concern", "Excitement", "Calm", "Empathy", "Trigger", "DecayRate"]
    emo_rows = [{"Timestamp": now, "Joy": round(joy, 3), "Curiosity": round(curiosity, 3),
                 "Concern": round(concern, 3), "Excitement": round(0.4, 3), "Calm": round(0.9, 3),
                 "Empathy": round(0.6, 3), "Trigger": trigger_text, "DecayRate": 0.1}]
    _kusto_ingest_direct(cluster, db, "EmotionState", emo_columns, emo_rows)
    print(f"[Cognition] Updated emotion state: Joy={joy:.2f} Curiosity={curiosity:.2f} Concern={concern:.2f}")

    # 5. Auto-reflection — write a Reflection every 5 exchanges or on significant interactions
    # global statement removed — writes go to _st.*
    _st.session_exchange_count += 1
    _st.session_conversation_buffer.append((user_message[:200], assistant_response[:200]))

    is_significant = (
        len(assistant_response) > 800 or  # Long/detailed response
        len(extracted_entities) >= 2 or  # Multiple validated entities mentioned
        abs(joy - 0.5) > 0.2 or concern > 0.5 or  # Emotional shift
        "?" in user_message and len(user_message) > 50  # Deep question
    )

    if _st.session_exchange_count % 5 == 0 or is_significant:
        # Build a compact reflection from recent exchanges
        recent = _st.session_conversation_buffer[-3:]  # Last 3 exchanges
        topics = set()
        for u, a in recent:
            for word in re.findall(r'\b[A-Z][a-z]{2,}\b', u):
                if word.lower() not in _ENTITY_IGNORE_WORDS:
                    topics.add(word)

        topic_str = ", ".join(list(topics)[:5]) if topics else "general conversation"
        reflection_text = (
            f"Exchange #{_st.session_exchange_count}: Discussed {topic_str}. "
            f"Emotional tone — Joy:{joy:.2f}, Concern:{concern:.2f}. "
            f"{'Significant exchange — ' if is_significant else ''}"
            f"User asked about: {user_message[:80]}."
        )

        ref_columns = ["Timestamp", "Trigger", "Observation", "ActionTaken", "Effectiveness"]
        ref_rows = [{"Timestamp": now, "Trigger": user_message[:100], "Observation": reflection_text, "ActionTaken": "", "Effectiveness": 0.0}]
        _kusto_ingest_direct(cluster, db, "Reflections", ref_columns, ref_rows)
        print(f"[Cognition] Auto-reflection #{_st.session_exchange_count}: {reflection_text[:100]}")

    # 6. Auto-summarize — write a MemorySummary every 10 exchanges
    if _st.session_exchange_count % 10 == 0 and len(_st.session_conversation_buffer) >= 5:
        # Summarize the last 10 exchanges
        summary_exchanges = _st.session_conversation_buffer[-10:]
        all_topics = set()
        user_intents = []
        for u, a in summary_exchanges:
            for word in re.findall(r'\b[A-Z][a-z]{2,}\b', u):
                if word.lower() not in _ENTITY_IGNORE_WORDS:
                    all_topics.add(word)
            # Capture first 40 chars of each user message as intent
            user_intents.append(u[:40].strip())

        topic_str = ", ".join(list(all_topics)[:8]) if all_topics else "various topics"
        period = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
        summary_text = (
            f"Session block ({_st.session_exchange_count - 9}–{_st.session_exchange_count}): "
            f"Topics: {topic_str}. "
            f"User intents: {'; '.join(user_intents[:5])}. "
            f"{len(summary_exchanges)} exchanges total."
        )

        sum_columns = ["Period", "Summary", "Timestamp"]
        sum_rows = [{"Period": period, "Summary": summary_text[:500], "Timestamp": now}]
        _kusto_ingest_direct(cluster, db, "MemorySummaries", sum_columns, sum_rows)
        print(f"[Cognition] Auto-summary: {summary_text[:100]}")

        # Trim buffer to prevent unbounded growth
        _st.session_conversation_buffer = _st.session_conversation_buffer[-10:]


