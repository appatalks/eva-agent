"""End-to-end test for the Skills importer against the real bridge HTTP server.

External services are stubbed: Kusto becomes an in-memory append-only store
(mimicking ingest + arg_max-by-id reads), and the ACP agent is a fake that
returns a normalized skill JSON for the Eva'rise step. The actual
ThreadingHTTPServer and request routing are exercised over real HTTP.

Run: python3 tools/test_skills_e2e.py
"""
import importlib.util
import json
import os
import sys
import threading
import tempfile
import time
import uuid
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "acp_bridge.py")
_TEMP_ROOT = tempfile.mkdtemp(prefix="eva-skills-e2e-")
os.environ["EVA_MEMORY_DB"] = os.path.join(_TEMP_ROOT, "memory.db")

spec = importlib.util.spec_from_file_location("acp_bridge", BRIDGE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
from bridge import core as bridge_core
from bridge import state as bridge_state
from bridge import cognition as bridge_cognition

# ── In-memory Kusto store ────────────────────────────────────────────────
_STORE = {"Skills": []}  # table -> append-only list of row dicts
_TABLE_COLS = {
    "Skills": list(m._SKILL_COLUMNS),
    "Goals": list(m._GOAL_COLUMNS),
}


def _latest_by(rows, key, time_col):
    latest = {}
    for r in rows:
        k = r.get(key)
        if k not in latest or str(r.get(time_col, "")) >= str(latest[k].get(time_col, "")):
            latest[k] = r
    return list(latest.values())


def fake_query(cluster, db, query, is_mgmt=False):
    """Tiny KQL-ish interpreter, only for the Skills queries the handlers emit."""
    if "Skills" not in query:
        return []
    if "todatetime(UpdatedAt)" in query:
        return []
    rows = _latest_by(_STORE["Skills"], "SkillId", "UpdatedAt")
    # Apply the filters that actually appear in our queries.
    import re as _re
    mid = _re.search(r"SkillId == '([^']+)'", query)
    if mid:
        rows = [r for r in rows if r.get("SkillId") == mid.group(1)]
    if "Status != 'deleted'" in query:
        rows = [r for r in rows if r.get("Status") != "deleted"]
    if "Status == 'active'" in query:
        rows = [r for r in rows if r.get("Status") == "active"]
    return rows


def fake_ingest(cluster, db, table, columns, rows_data):
    for row in rows_data:
        _STORE.setdefault(table, []).append({c: row.get(c, "") for c in columns})
    return True


def fake_table_columns(cluster, db, table):
    return _TABLE_COLS.get(table)


class FakeACP:
    alive = True
    model = "claude-sonnet-4.6"
    mcp_config = {"kusto-mcp-server": {"env": {"KUSTO_CLUSTER_URL": "https://x.kusto.windows.net", "KUSTO_DATABASE": "Eva"}}}

    def prompt(self, text, timeout=120):
        # Return a normalized skill as strict JSON, as the real agent would.
        return {"text": json.dumps({
            "name": "Summarize a webpage",
            "description": "Use when the user wants a concise summary of a web page or article.",
            "instructions": "1. Fetch the page.\n2. Extract the main text.\n3. Produce a 5 bullet summary.",
            "tools": ["browser"],
            "tags": ["summary", "web", "article"],
        })}


# ── Wire the stubs ───────────────────────────────────────────────────────
bridge_state.acp_client = FakeACP()
bridge_state.bridge_bind_address = "127.0.0.1"
bridge_state.bridge_auth_token = "skills-test-token"
bridge_state.kusto_token_cache = "faketoken"
bridge_state.cognition_enabled = True
bridge_state.active_kusto_cluster = "https://x.kusto.windows.net"
bridge_state.active_kusto_db = "Eva"
bridge_state.memory_backend = "kusto"
bridge_core._kusto_query_direct = fake_query
bridge_core._kusto_ingest_direct = fake_ingest
bridge_core._get_table_columns = fake_table_columns
bridge_core._ensure_kusto_token = lambda: (True, "")
bridge_cognition._kusto_query_direct = fake_query
bridge_cognition._kusto_ingest_direct = fake_ingest
bridge_cognition._get_table_columns = fake_table_columns

PORT = 8899
BASE = f"http://127.0.0.1:{PORT}"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={
                                   "Content-Type": "application/json",
                                   "Authorization": "Bearer skills-test-token",
                                   "X-Eva-Request-Id": str(uuid.uuid4()),
                               })
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main():
    server = m.ThreadingHTTPServer(("127.0.0.1", PORT), m.BridgeHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    failures = []

    def check(label, cond):
        print(("PASS" if cond else "FAIL") + ": " + label)
        if not cond:
            failures.append(label)

    try:
        # 1. Eva'rise an imported source.
        st, body = req("POST", "/v1/skills/evarise", {"source_type": "paste", "content": "A guide to summarizing web pages."})
        check("evarise returns 200", st == 200)
        draft = body.get("draft", {})
        check("evarise draft has name", draft.get("name") == "Summarize a webpage")
        check("evarise draft tools normalized to csv", draft.get("tools") == "browser")

        # 2. Save the skill.
        st, body = req("POST", "/v1/skills", draft)
        check("create returns 201", st == 201)
        sid = body.get("skill", {}).get("SkillId", "")
        check("create returns SkillId", sid.startswith("sk-"))

        # 3. List skills.
        st, body = req("GET", "/v1/skills")
        check("list returns 200", st == 200)
        skills = body.get("skills", [])
        check("list has 1 active skill", len(skills) == 1 and skills[0]["Status"] == "active")

        # 4. Disable via PATCH.
        st, body = req("PATCH", "/v1/skills/" + sid, {"status": "disabled"})
        check("patch disable returns 200", st == 200 and body.get("skill", {}).get("Status") == "disabled")

        # 5. Runtime injection: a matching message should surface the skill.
        #    Re-enable first, then check _build_memory_context (lexical fallback,
        #    no embedding key) injects an [Active Skill] block.
        req("PATCH", "/v1/skills/" + sid, {"status": "active"})
        ctx = bridge_core._build_memory_context("please summarize this web article for me")
        check("runtime injection includes the skill", "[Active Skill: Summarize a webpage]" in ctx)
        check("runtime injection includes instructions", "5 bullet summary" in ctx)

        # 6. An unrelated message should NOT inject it.
        ctx2 = bridge_core._build_memory_context("what is your favorite color")
        check("no injection for unrelated message", "[Active Skill:" not in ctx2)

        # 7. Delete (soft) and confirm it drops from the list.
        st, body = req("DELETE", "/v1/skills/" + sid)
        check("delete returns 200", st == 200 and body.get("status") == "deleted")
        st, body = req("GET", "/v1/skills")
        check("deleted skill removed from list", len(body.get("skills", [])) == 0)

        # 8. Validation: missing instructions is rejected.
        st, body = req("POST", "/v1/skills", {"name": "x"})
        check("create without instructions rejected (400)", st == 400)

    finally:
        server.shutdown()

    print("\n" + ("ALL SKILLS E2E TESTS PASSED" if not failures
                  else f"{len(failures)} FAILED: {failures}"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
