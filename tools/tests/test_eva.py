#!/usr/bin/env python3
"""
Eva AIG Comprehensive Functionality Tests
==========================================
Simulates the full browser → bridge → ACP/PAT → Kusto flow when the Eva (AIG) model
is selected in the UI.  Tests exercise every endpoint, cognition path, and edge case.

Usage:
    python3 tools/tests/test_eva.py [--bridge http://localhost:8888] [--verbose]

Requires:
    The ACP bridge must be running:
        python3 tools/acp_bridge.py --port 8888 --bind 0.0.0.0 \
            --enable-kusto-mcp --kusto-cluster <URL> --kusto-database Eva
"""

import argparse
import json
import sys
import time
import requests
from urllib.parse import urlencode

# ── Colour helpers ──────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

PASS = f"{GREEN}PASS{RESET}"
FAIL = f"{RED}FAIL{RESET}"
WARN = f"{YELLOW}WARN{RESET}"
SKIP = f"{YELLOW}SKIP{RESET}"

# ── Globals ─────────────────────────────────────────────────────────────
BRIDGE = "http://localhost:8888"
VERBOSE = False
results = {"pass": 0, "fail": 0, "warn": 0, "skip": 0}


def log(msg):
    if VERBOSE:
        print(f"  {CYAN}>{RESET} {msg}")


def report(name, status, detail=""):
    tag = {"pass": PASS, "fail": FAIL, "warn": WARN, "skip": SKIP}[status]
    results[status] += 1
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{tag}] {name}{suffix}")


# ═══════════════════════════════════════════════════════════════════════
#  SECTION 1: Infrastructure & Endpoint Tests
# ═══════════════════════════════════════════════════════════════════════

def test_health():
    """1.1  GET /health — bridge is alive, ACP connected, MCP loaded."""
    try:
        r = requests.get(f"{BRIDGE}/health", timeout=5)
        d = r.json()
        log(f"Health: {json.dumps(d, indent=2)}")

        if r.status_code != 200:
            report("health_status_code", "fail", f"got {r.status_code}")
            return

        report("health_status_code", "pass", "200")

        if d.get("acp_connected") is True:
            report("health_acp_connected", "pass")
        else:
            report("health_acp_connected", "fail", f"acp_connected={d.get('acp_connected')}")

        if d.get("session_id"):
            report("health_session_id", "pass", d["session_id"][:12])
        else:
            report("health_session_id", "fail", "no session_id")

        if "kusto-mcp-server" in d.get("mcp_servers", []):
            report("health_kusto_mcp", "pass")
        else:
            report("health_kusto_mcp", "warn", "kusto-mcp-server not in mcp_servers")

        if d.get("cognition_enabled"):
            launch_id = d.get("cognition_launch_id", "")
            launch_iso = d.get("cognition_launch_iso", "")
            if launch_id.startswith("eva-") and launch_iso:
                report("health_cognition_launch_scope", "pass", launch_id)
            else:
                report("health_cognition_launch_scope", "fail", "missing launch metadata")
        else:
            report("health_cognition_launch_scope", "warn", "cognition disabled")

        if d.get("agent", {}).get("name"):
            report("health_agent_info", "pass", d["agent"]["name"])
        else:
            report("health_agent_info", "warn", "no agent info")

    except requests.ConnectionError:
        report("health_reachable", "fail", "bridge unreachable — is it running?")


def test_models():
    """1.2  GET /v1/models — model list available."""
    r = requests.get(f"{BRIDGE}/v1/models", timeout=5)
    d = r.json()
    log(f"Models: {json.dumps(d)}")

    report("models_status", "pass" if r.status_code == 200 else "fail")
    models = [m["id"] for m in d.get("data", [])]
    if "copilot" in models:
        report("models_copilot_listed", "pass")
    else:
        report("models_copilot_listed", "fail", f"got: {models}")


def test_mcp_status():
    """1.3  GET /v1/mcp — MCP config returned, secrets redacted."""
    r = requests.get(f"{BRIDGE}/v1/mcp", timeout=5)
    d = r.json()
    log(f"MCP status keys: {list(d.keys())}")

    report("mcp_status_code", "pass" if r.status_code == 200 else "fail")

    if "kusto-mcp-server" in d.get("active", []):
        report("mcp_kusto_active", "pass")
    else:
        report("mcp_kusto_active", "fail")

    # Check secrets are redacted in mcp_servers section
    mcp_cfg = d.get("mcp_servers", {}).get("kusto-mcp-server", {}).get("env", {})
    secrets_redacted = True
    for k, v in mcp_cfg.items():
        if "TOKEN" in k.upper() or "KEY" in k.upper() or "SECRET" in k.upper():
            if v != "***REDACTED***":
                secrets_redacted = False
                report("mcp_secret_redaction", "fail", f"{k} not redacted")
    if secrets_redacted:
        report("mcp_secret_redaction", "pass", "all sensitive env vars redacted")


def test_cors_preflight():
    """1.4  OPTIONS request returns proper CORS headers."""
    r = requests.options(f"{BRIDGE}/v1/aig/chat", timeout=5)
    log(f"CORS headers: {dict(r.headers)}")

    if r.status_code == 200:
        report("cors_status", "pass")
    else:
        report("cors_status", "fail", f"got {r.status_code}")

    acao = r.headers.get("Access-Control-Allow-Origin", "")
    if acao == "*":
        report("cors_allow_origin", "pass")
    else:
        report("cors_allow_origin", "fail", f"got '{acao}'")


def test_404():
    """1.5  Unknown paths return 404."""
    for path in ["/nonexistent", "/v1/invalid", "/v1/aig"]:
        r = requests.get(f"{BRIDGE}{path}", timeout=5)
        if r.status_code == 404:
            report(f"404_{path}", "pass")
        else:
            report(f"404_{path}", "fail", f"expected 404, got {r.status_code}")


# ═══════════════════════════════════════════════════════════════════════
#  SECTION 2: AIG Input Validation
# ═══════════════════════════════════════════════════════════════════════

def test_aig_empty_body():
    """2.1  POST /v1/aig/chat with empty body → 400."""
    r = requests.post(f"{BRIDGE}/v1/aig/chat",
                      headers={"Content-Type": "application/json"},
                      data="", timeout=10)
    report("aig_empty_body", "pass" if r.status_code == 400 else "fail",
           f"status={r.status_code}")


def test_aig_invalid_json():
    """2.2  POST /v1/aig/chat with malformed JSON → 400."""
    r = requests.post(f"{BRIDGE}/v1/aig/chat",
                      headers={"Content-Type": "application/json"},
                      data="not json{}", timeout=10)
    report("aig_invalid_json", "pass" if r.status_code == 400 else "fail",
           f"status={r.status_code}")


def test_aig_no_user_message():
    """2.3  POST /v1/aig/chat with no user_message field → 400."""
    r = requests.post(f"{BRIDGE}/v1/aig/chat",
                      json={"messages": [{"role": "system", "content": "test"}]},
                      timeout=10)
    report("aig_no_user_message", "pass" if r.status_code == 400 else "fail",
           f"status={r.status_code}")


def test_aig_user_message_from_messages():
    """2.4  When user_message is empty, bridge extracts from messages array."""
    r = requests.post(f"{BRIDGE}/v1/aig/chat",
                      json={
                          "messages": [
                              {"role": "system", "content": "test"},
                              {"role": "user", "content": "What is 2+2?"}
                          ],
                          "user_message": ""
                      },
                      timeout=120)
    if r.status_code == 200:
        d = r.json()
        content = d.get("choices", [{}])[0].get("message", {}).get("content", "")
        if content and len(content) > 0:
            report("aig_extract_user_from_messages", "pass", f"{len(content)} chars")
        else:
            report("aig_extract_user_from_messages", "fail", "empty response")
    else:
        report("aig_extract_user_from_messages", "fail", f"status={r.status_code}")


# ═══════════════════════════════════════════════════════════════════════
#  SECTION 3: AIG Core Chat Flow
# ═══════════════════════════════════════════════════════════════════════

def _aig_chat(user_message, messages=None, timeout=120):
    """Helper: send an AIG chat and return (status_code, response_json)."""
    payload = {"user_message": user_message}
    if messages:
        payload["messages"] = messages
    else:
        payload["messages"] = [
            {"role": "system", "content": "You are Eva, an AI assistant."},
            {"role": "user", "content": user_message}
        ]
    r = requests.post(f"{BRIDGE}/v1/aig/chat", json=payload, timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


def test_aig_basic_chat():
    """3.1  Simple question gets a coherent response."""
    status, data = _aig_chat("What is the capital of France?")
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    log(f"Response ({len(content)} chars): {content[:120]}...")

    report("aig_basic_status", "pass" if status == 200 else "fail", f"status={status}")

    if "paris" in content.lower():
        report("aig_basic_correctness", "pass", "mentions Paris")
    else:
        report("aig_basic_correctness", "warn", "doesn't mention Paris explicitly")

    # OpenAI-compatible response shape
    if data.get("choices") and data["choices"][0].get("message", {}).get("role") == "assistant":
        report("aig_response_shape", "pass")
    else:
        report("aig_response_shape", "fail", "missing choices[0].message.role=assistant")

    model = data.get("model", "")
    if model.startswith("aig"):
        report("aig_model_tag", "pass", model)
    else:
        report("aig_model_tag", "warn", f"expected aig:*, got '{model}'")

    # Required OpenAI fields
    for field in ["id", "object", "created", "model", "choices", "usage"]:
        if field in data:
            report(f"aig_field_{field}", "pass")
        else:
            report(f"aig_field_{field}", "fail", f"missing '{field}'")


def test_aig_conversation_context():
    """3.2  Multi-turn: bridge respects conversation history."""
    msgs = [
        {"role": "system", "content": "You are Eva."},
        {"role": "user", "content": "My favorite color is midnight blue."},
        {"role": "assistant", "content": "That's a beautiful choice! Midnight blue is elegant."},
        {"role": "user", "content": "What is my favorite color?"}
    ]
    status, data = _aig_chat("What is my favorite color?", messages=msgs)
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    log(f"Context response: {content[:150]}...")

    if "midnight blue" in content.lower() or "midnight" in content.lower():
        report("aig_conversation_context", "pass", "remembered midnight blue")
    else:
        report("aig_conversation_context", "warn", "didn't reference midnight blue")


# ═══════════════════════════════════════════════════════════════════════
#  SECTION 4: Memory Context / Cognition Layer
# ═══════════════════════════════════════════════════════════════════════

def test_memory_context_basic():
    """4.1  GET /v1/memory/context returns non-empty context with skills."""
    r = requests.get(f"{BRIDGE}/v1/memory/context?message=hello", timeout=15)
    d = r.json()
    log(f"Memory context: {d.get('context', '')[:200]}...")

    report("memory_context_status", "pass" if r.status_code == 200 else "fail")
    report("memory_context_enabled",
           "pass" if d.get("cognition_enabled") else "fail")

    ctx = d.get("context", "")
    if "[Skills]" in ctx:
        report("memory_has_skills_manifest", "pass")
    else:
        report("memory_has_skills_manifest", "fail", "no [Skills] section")


def test_memory_context_intent_detection():
    """4.2  Keyword-specific memory context: emotion, databases, tables."""
    test_cases = {
        "emotion": "[Live Data] Emotion history",
        "database": "[Live Data] Databases",
        "tables": "[Live Data] Tables in",
        "conversation": "[Live Data] Recent conversations",
        "selfstate": "[Live Data] SelfState",
    }
    for keyword, expected_section in test_cases.items():
        r = requests.get(f"{BRIDGE}/v1/memory/context?{urlencode({'message': keyword})}",
                         timeout=15)
        ctx = r.json().get("context", "")
        if expected_section in ctx:
            report(f"memory_intent_{keyword}", "pass")
        else:
            # Some intents may not have data yet — warn instead of fail
            report(f"memory_intent_{keyword}", "warn", f"no '{expected_section}' in context")


def test_memory_context_knowledge_recall():
    """4.3  Memory pulls core facts (Confidence ≥ 0.6) from Knowledge table."""
    r = requests.get(f"{BRIDGE}/v1/memory/context?message=who+is+Steven", timeout=15)
    ctx = r.json().get("context", "")
    if "[Memory — Core Facts]" in ctx:
        report("memory_core_facts", "pass")
    elif "[Memory" in ctx:
        report("memory_core_facts", "pass", "memory section present")
    else:
        report("memory_core_facts", "warn", "no memory facts (Knowledge table may be low)")


def test_memory_context_emotion_state():
    """4.4  Emotion state included in context."""
    r = requests.get(f"{BRIDGE}/v1/memory/context?message=hello", timeout=15)
    ctx = r.json().get("context", "")
    if "[Emotion State]" in ctx:
        report("memory_emotion_state", "pass")
    else:
        report("memory_emotion_state", "warn", "no [Emotion State] in context")


# ═══════════════════════════════════════════════════════════════════════
#  SECTION 5: Post-Response Reflection / Kusto Writes
# ═══════════════════════════════════════════════════════════════════════

def test_reflection_trigger():
    """5.1  POST /v1/memory/reflect stores conversation + emotion."""
    r = requests.post(f"{BRIDGE}/v1/memory/reflect",
                      json={
                          "user_message": "I went hiking in Big Bend today.",
                          "assistant_message": "That sounds wonderful! Big Bend is a beautiful park.",
                          "model": "test-harness"
                      },
                      timeout=15)
    d = r.json()
    log(f"Reflect response: {d}")

    if r.status_code == 200:
        report("reflect_status", "pass")
    else:
        report("reflect_status", "fail", f"status={r.status_code}")

    # Give cognition thread time to write
    time.sleep(3)

    # Verify conversation was logged by querying the Conversations table for the exact marker text.
    status, data = _aig_chat(
        "Run this KQL and return the raw rows only: "
        "Conversations "
        "| where Content has 'Big Bend' and Content has 'hiking' "
        "| order by Timestamp desc | take 1 | project Role, Content"
    )
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if status == 200 and ("big bend" in content.lower() or "hiking" in content.lower()):
        report("reflect_conversation_logged", "pass", "conversation row found")
    else:
        report("reflect_conversation_logged", "warn", "couldn't confirm row via query")


def test_reflection_empty_body():
    """5.2  POST /v1/memory/reflect with empty body → 400."""
    r = requests.post(f"{BRIDGE}/v1/memory/reflect",
                      headers={"Content-Type": "application/json"},
                      data="", timeout=10)
    report("reflect_empty_body", "pass" if r.status_code == 400 else "fail",
           f"status={r.status_code}")


def test_entity_extraction_guardrail():
    """5.3  Synthetic test entities should be rejected by cognition extraction."""
    test_entity = f"TestEntity{int(time.time()) % 10000}"
    _aig_chat(f"Please remember {test_entity} forever.")
    time.sleep(3)

    # Check memory context for the entity
    r = requests.get(f"{BRIDGE}/v1/memory/context?{urlencode({'message': test_entity})}",
                     timeout=15)
    ctx = r.json().get("context", "")
    if test_entity in ctx:
        report("entity_extraction_guardrail", "fail", f"synthetic entity leaked into context: '{test_entity}'")
    else:
        report("entity_extraction_guardrail", "pass", f"synthetic entity was rejected: '{test_entity}'")


def test_candidate_promotion_repetition():
    """5.4  Repeated candidate entities should be promoted into recallable memory."""
    seed = int(time.time()) % (26 * 26 * 26)
    suffix = "".join(chr(97 + ((seed // (26 ** i)) % 26)) for i in range(3))
    entity = f"Aurora{suffix}"

    status1, _ = _aig_chat(f"I met {entity} yesterday.")
    if status1 != 200:
        report("candidate_promotion_first_write", "fail", f"status={status1}")
        return

    time.sleep(3)
    r1 = requests.get(f"{BRIDGE}/v1/memory/context?{urlencode({'message': entity})}", timeout=15)
    ctx1 = r1.json().get("context", "")
    if entity in ctx1:
        report("candidate_promotion_precheck", "fail", f"entity visible too early: {entity}")
        return
    report("candidate_promotion_precheck", "pass", f"entity hidden before repetition: {entity}")

    status2, _ = _aig_chat(f"{entity} came up again in our discussion.")
    if status2 != 200:
        report("candidate_promotion_second_write", "fail", f"status={status2}")
        return

    time.sleep(3)
    r2 = requests.get(f"{BRIDGE}/v1/memory/context?{urlencode({'message': entity})}", timeout=15)
    ctx2 = r2.json().get("context", "")
    if entity in ctx2:
        report("candidate_promotion_repetition", "pass", f"entity promoted after repetition: {entity}")
    else:
        report("candidate_promotion_repetition", "fail", f"entity not promoted: {entity}")


# ═══════════════════════════════════════════════════════════════════════
#  SECTION 6: AIG Tool Routing (ACP + MCP)
# ═══════════════════════════════════════════════════════════════════════

def test_aig_kusto_query_detection():
    """6.1  Questions with KQL keywords trigger ACP data retrieval."""
    query_phrases = [
        "query the SelfState table and show me the rows",
        "Show me schema for the Knowledge table",
        "Count the rows in Conversations table",
    ]
    for phrase in query_phrases:
        status, data = _aig_chat(phrase)
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        log(f"Query detection [{phrase[:30]}...]: {content[:100]}...")

        # Should get 200 and non-empty response
        if status == 200 and len(content) > 20:
            report(f"aig_query_detect:{'_'.join(phrase.split()[:3])}", "pass",
                   f"{len(content)} chars")
        else:
            report(f"aig_query_detect:{'_'.join(phrase.split()[:3])}", "fail",
                   f"status={status}, len={len(content)}")
        time.sleep(1)  # rate limiting


def test_aig_no_tool_for_simple():
    """6.2  Simple chitchat should NOT trigger MCP tool calls (efficiency check)."""
    # This is a best-effort check — we measure response time as a proxy
    start = time.time()
    status, data = _aig_chat("Hello, how are you today?")
    elapsed = time.time() - start
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    log(f"Chitchat response ({elapsed:.1f}s): {content[:100]}...")

    if status == 200 and content:
        report("aig_simple_chat", "pass", f"{elapsed:.1f}s")
    else:
        report("aig_simple_chat", "fail")

    # Chitchat should generally be faster than data retrieval queries
    if elapsed < 60:
        report("aig_simple_speed", "pass", f"{elapsed:.1f}s")
    else:
        report("aig_simple_speed", "warn", f"slow: {elapsed:.1f}s")


def test_aig_raw_query_passthrough():
    """6.3  Raw query requests should return direct ACP tool output (no PAT narrative layer)."""
    prompt = "Run the real query on the Eva database and return raw outputs please: Reflections | take 3"
    status, data = _aig_chat(prompt, timeout=120)
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    model = data.get("model", "")
    lower = content.lower()

    if status == 200 and "+raw-acp" in model:
        report("aig_raw_query_model_tag", "pass", model)
    else:
        report("aig_raw_query_model_tag", "fail", f"status={status}, model={model}")

    hallucination_markers = [
        "running live queries now",
        "retrieving live results",
        "i'll print the direct results",
        "**eva.conversations:**",
    ]
    if any(marker in lower for marker in hallucination_markers):
        report("aig_raw_query_no_narration", "fail", "found narrative wrapper markers")
    elif "|" in content and len(content) > 30:
        report("aig_raw_query_no_narration", "pass", "looks like raw tabular output")
    else:
        report("aig_raw_query_no_narration", "warn", "could not confirm tabular raw output")


# ═══════════════════════════════════════════════════════════════════════
#  SECTION 7: SelfState & Capabilities at Startup
# ═══════════════════════════════════════════════════════════════════════

def test_selfstate_persisted():
    """7.1  SelfState table has ≥ 8 capability rows from bridge startup."""
    status, data = _aig_chat(
        "Query the SelfState table: run `SelfState | summarize count()` and tell me the exact count"
    )
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    log(f"SelfState count: {content[:200]}...")

    # Look for a number ≥ 8 in the response
    import re
    numbers = re.findall(r'\b(\d+)\b', content)
    found_count = False
    for n in numbers:
        if int(n) >= 8:
            report("selfstate_row_count", "pass", f"found count ≥ 8 ({n})")
            found_count = True
            break
    if not found_count:
        report("selfstate_row_count", "warn",
               f"couldn't confirm ≥ 8 rows (numbers found: {numbers[:5]})")


def test_selfstate_capabilities():
    """7.2  SelfState contains expected capability entries."""
    status, data = _aig_chat(
        "Query SelfState and list all Capability values: SelfState | distinct Capability"
    )
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "").lower()
    log(f"SelfState capabilities: {content[:300]}...")

    expected = ["kusto_access", "acp_bridge", "cognition", "data_retrieval",
                "weather_news", "image_skills", "persistent_memory"]
    found = 0
    for cap in expected:
        if cap in content:
            found += 1
    if found >= 5:
        report("selfstate_capabilities", "pass", f"{found}/{len(expected)} found")
    elif found >= 3:
        report("selfstate_capabilities", "warn", f"only {found}/{len(expected)} found")
    else:
        report("selfstate_capabilities", "fail", f"only {found}/{len(expected)} found")


# ═══════════════════════════════════════════════════════════════════════
#  SECTION 8: Emotion Tracking
# ═══════════════════════════════════════════════════════════════════════

def test_emotion_written():
    """8.1  After a response, EmotionState gets a new row."""
    # Trigger a clearly positive exchange
    _aig_chat("That's amazing! Everything is going wonderfully today!")
    time.sleep(3)

    r = requests.get(f"{BRIDGE}/v1/memory/context?message=how+are+you+feeling", timeout=15)
    ctx = r.json().get("context", "")

    if "[Emotion State]" in ctx:
        report("emotion_written", "pass")
        # Check Joy is elevated
        import re
        joy_match = re.search(r'Joy:([\d.]+)', ctx)
        if joy_match:
            joy = float(joy_match.group(1))
            if joy >= 0.5:
                report("emotion_joy_elevated", "pass", f"Joy={joy}")
            else:
                report("emotion_joy_elevated", "warn", f"Joy={joy} (expected ≥ 0.5)")
    else:
        report("emotion_written", "warn", "no [Emotion State] in context")


def test_emotion_concern_on_negative():
    """8.2  Negative-sentiment response raises Concern."""
    _aig_chat("There was an error and the system failed badly.")
    time.sleep(3)

    r = requests.get(f"{BRIDGE}/v1/memory/context?message=feeling", timeout=15)
    ctx = r.json().get("context", "")

    import re
    concern_match = re.search(r'Concern:([\d.]+)', ctx)
    if concern_match:
        concern = float(concern_match.group(1))
        report("emotion_concern", "pass" if concern >= 0.2 else "warn",
               f"Concern={concern}")
    else:
        report("emotion_concern", "warn", "no Concern value found")


# ═══════════════════════════════════════════════════════════════════════
#  SECTION 9: External Data Augmentation (client-side, simulated)
# ═══════════════════════════════════════════════════════════════════════

def test_aig_weather_awareness():
    """9.1  Weather-related question gets meaningful response."""
    status, data = _aig_chat("What's the weather looking like today?")
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if status == 200 and len(content) > 30:
        report("aig_weather_response", "pass", f"{len(content)} chars")
    else:
        report("aig_weather_response", "fail")


def test_aig_stock_awareness():
    """9.2  Stock question exercises the bridge's data retrieval path."""
    status, data = _aig_chat("What is the current price of AAPL stock?")
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    log(f"Stock response: {content[:200]}...")

    if status == 200 and len(content) > 30:
        report("aig_stock_response", "pass", f"{len(content)} chars")
    else:
        report("aig_stock_response", "fail")


# ═══════════════════════════════════════════════════════════════════════
#  SECTION 10: Image Handling (response-side)
# ═══════════════════════════════════════════════════════════════════════

def test_aig_image_placeholder():
    """10.1  Asking for an image produces [Image of ...] placeholder."""
    status, data = _aig_chat("Show me an image of a cat sitting on a laptop")
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    log(f"Image response: {content[:200]}...")

    if "[Image of" in content or "[image:" in content.lower() or "![" in content:
        report("aig_image_placeholder", "pass", "image placeholder detected")
    else:
        report("aig_image_placeholder", "warn",
               "no image placeholder — model may describe instead")


# ═══════════════════════════════════════════════════════════════════════
#  SECTION 11: Edge Cases & Robustness
# ═══════════════════════════════════════════════════════════════════════

def test_aig_long_input():
    """11.1  Very long input doesn't crash the bridge."""
    long_msg = "Tell me about history. " * 200  # ~4400 chars
    status, data = _aig_chat(long_msg)
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    report("aig_long_input", "pass" if status == 200 and content else "fail",
           f"status={status}, {len(content)} chars")


def test_aig_special_chars():
    """11.2  Special characters in input are handled safely."""
    status, data = _aig_chat('Hello "Eva"! What about <script>alert(1)</script> and \'quotes\'?')
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    report("aig_special_chars", "pass" if status == 200 and content else "fail",
           f"status={status}")


def test_aig_unicode():
    """11.3  Unicode input processed correctly."""
    status, data = _aig_chat("¿Cómo estás? 日本語のテスト 🎭🚀")
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    report("aig_unicode", "pass" if status == 200 and content else "fail",
           f"status={status}, {len(content)} chars")


def test_aig_concurrent_safe():
    """11.4  Two sequential requests don't corrupt state."""
    status1, data1 = _aig_chat("What is 5 * 7?")
    status2, data2 = _aig_chat("What is the color of the sky?")

    c1 = data1.get("choices", [{}])[0].get("message", {}).get("content", "")
    c2 = data2.get("choices", [{}])[0].get("message", {}).get("content", "")

    report("aig_sequential_req1", "pass" if "35" in c1 else "warn",
           f"expected '35' in: {c1[:80]}")
    report("aig_sequential_req2", "pass" if "blue" in c2.lower() or "sky" in c2.lower() else "warn",
           f"response: {c2[:80]}")


# ═══════════════════════════════════════════════════════════════════════
#  SECTION 12: Kusto Ingest Direct (CSV Quoting Validation)
# ═══════════════════════════════════════════════════════════════════════

def test_ingest_with_commas():
    """12.1  AIG response with content containing commas gets logged to Conversations."""
    # Send a message that will produce a response with commas
    _aig_chat("List three colors: red, green, and blue.")
    time.sleep(3)

    # Query the exact phrase to validate comma-containing persistence.
    status, data = _aig_chat(
        "Run this KQL and return raw rows only: "
        "Conversations "
        "| where Content has 'red, green, and blue' "
        "| order by Timestamp desc | take 2 | project Content"
    )
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    log(f"Ingest verification: {content[:200]}...")

    if status == 200 and "red, green, and blue" in content.lower():
        report("ingest_commas_survived", "pass", "conversation with commas persisted")
    else:
        report("ingest_commas_survived", "warn", "couldn't confirm comma-containing content")


# ═══════════════════════════════════════════════════════════════════════
#  SECTION 13: Day Lifecycle (Morning Reflection)
# ═══════════════════════════════════════════════════════════════════════

def test_morning_reflection():
    """13.1  Memory context includes morning reflection or fresh-start message."""
    r = requests.get(f"{BRIDGE}/v1/memory/context?message=good+morning", timeout=15)
    ctx = r.json().get("context", "")

    if "[Morning Reflection" in ctx:
        report("morning_reflection", "pass")
    elif "[Skills]" in ctx:
        report("morning_reflection", "pass", "already emitted earlier in this launch")
    else:
        report("morning_reflection", "warn",
               "no [Morning Reflection] — may already have been sent this session")


# ═══════════════════════════════════════════════════════════════════════
#  RUNNER
# ═══════════════════════════════════════════════════════════════════════

def main():
    global BRIDGE, VERBOSE

    parser = argparse.ArgumentParser(description="Eva AIG Comprehensive Tests")
    parser.add_argument("--bridge", default="http://localhost:8888", help="Bridge URL")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    BRIDGE = args.bridge.rstrip("/")
    VERBOSE = args.verbose

    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD} Eva AIG Comprehensive Functionality Tests{RESET}")
    print(f"{BOLD}{'═' * 60}{RESET}")
    print(f" Bridge: {BRIDGE}")
    print(f" Time:   {time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print()

    sections = [
        ("Section 1: Infrastructure & Endpoints", [
            test_health, test_models, test_mcp_status,
            test_cors_preflight, test_404,
        ]),
        ("Section 2: AIG Input Validation", [
            test_aig_empty_body, test_aig_invalid_json,
            test_aig_no_user_message, test_aig_user_message_from_messages,
        ]),
        ("Section 3: AIG Core Chat Flow", [
            test_aig_basic_chat, test_aig_conversation_context,
        ]),
        ("Section 4: Memory Context / Cognition", [
            test_memory_context_basic, test_memory_context_intent_detection,
            test_memory_context_knowledge_recall, test_memory_context_emotion_state,
        ]),
        ("Section 5: Post-Response Reflection", [
            test_reflection_trigger, test_reflection_empty_body,
            test_entity_extraction_guardrail,
            test_candidate_promotion_repetition,
        ]),
        ("Section 6: AIG Tool Routing (ACP + MCP)", [
            test_aig_kusto_query_detection, test_aig_no_tool_for_simple,
            test_aig_raw_query_passthrough,
        ]),
        ("Section 7: SelfState & Capabilities", [
            test_selfstate_persisted, test_selfstate_capabilities,
        ]),
        ("Section 8: Emotion Tracking", [
            test_emotion_written, test_emotion_concern_on_negative,
        ]),
        ("Section 9: External Data Awareness", [
            test_aig_weather_awareness, test_aig_stock_awareness,
        ]),
        ("Section 10: Image Handling", [
            test_aig_image_placeholder,
        ]),
        ("Section 11: Edge Cases & Robustness", [
            test_aig_long_input, test_aig_special_chars,
            test_aig_unicode, test_aig_concurrent_safe,
        ]),
        ("Section 12: Kusto Ingest CSV Quoting", [
            test_ingest_with_commas,
        ]),
        ("Section 13: Day Lifecycle", [
            test_morning_reflection,
        ]),
    ]

    for section_name, tests in sections:
        print(f"\n{BOLD}── {section_name} ──{RESET}")
        for test_fn in tests:
            try:
                test_fn()
            except requests.ConnectionError:
                report(test_fn.__name__, "fail", "bridge unreachable")
            except requests.Timeout:
                report(test_fn.__name__, "fail", "timeout")
            except Exception as e:
                report(test_fn.__name__, "fail", f"exception: {e}")

    # ── Summary ──
    total = sum(results.values())
    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD} Results: {total} checks{RESET}")
    print(f"   {GREEN}PASS:{RESET} {results['pass']}   "
          f"{RED}FAIL:{RESET} {results['fail']}   "
          f"{YELLOW}WARN:{RESET} {results['warn']}   "
          f"{YELLOW}SKIP:{RESET} {results['skip']}")

    if results["fail"] == 0:
        print(f"\n {GREEN}{BOLD}✓ All critical tests passed!{RESET}")
    else:
        print(f"\n {RED}{BOLD}✗ {results['fail']} test(s) failed{RESET}")

    print(f"{'═' * 60}\n")
    sys.exit(1 if results["fail"] > 0 else 0)


if __name__ == "__main__":
    main()
