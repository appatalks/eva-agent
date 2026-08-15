"""End-to-end test for the Skills importer against the real bridge HTTP server.

External services are stubbed: Kusto becomes an in-memory append-only store
(mimicking ingest + arg_max-by-id reads), and the ACP agent is a fake that
returns a normalized skill JSON for the Eva'rise step. The actual
ThreadingHTTPServer and request routing are exercised over real HTTP.

Run: python3 tools/tests/test_skills_e2e.py
"""
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from unittest.mock import patch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from bridge import core as m
from bridge import cognition as cognition
from bridge.skills import _safe_external_url

# ── In-memory Kusto store ────────────────────────────────────────────────
_STORE = {"Skills": [], "SkillVersions": []}  # table -> append-only list of row dicts
_TABLE_COLS = {
    "Skills": list(m._SKILL_COLUMNS),
    "SkillVersions": ["SkillVersionId", "SkillId", "Version", "RiskLevel", "TriggerSpec", "AllowedTools", "ValidationSpec", "Status", "ExpiresAt", "CreatedAt"],
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
    if "GovernedStatus" in query:
        rows = _latest_by(_STORE["Skills"], "SkillId", "UpdatedAt")
        return [row for row in rows if row.get("Status") == "active"]
    if "SkillVersions" in query:
        rows = _latest_by(_STORE["SkillVersions"], "SkillId", "CreatedAt")
        import re as _re
        mid = _re.search(r"SkillId == '([^']+)'", query)
        return [row for row in rows if not mid or row.get("SkillId") == mid.group(1)]
    if "Skills" not in query:
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
            "category": "Documents & Data",
            "instructions": "1. Fetch the page.\n2. Extract the main text.\n3. Produce a 5 bullet summary.",
            "tools": ["browser"],
            "tags": ["summary", "web", "article"],
        })}


# ── Wire the stubs ───────────────────────────────────────────────────────
m._st.acp_client = FakeACP()
m._st.bridge_bind_address = "127.0.0.1"
m._st.memory_backend = "kusto"
m._st.kusto_token_cache = "faketoken"
m._st.cognition_enabled = True
m._st.active_kusto_cluster = "https://x.kusto.windows.net"
m._st.active_kusto_db = "Eva"
m._resolve_memory_backend = lambda: "kusto"
m._get_kusto_config = lambda: ("https://x.kusto.windows.net", "Eva")
m._kusto_query_direct = fake_query
m._kusto_ingest_direct = fake_ingest
m._get_table_columns = fake_table_columns
m._ensure_kusto_token = lambda: (True, "")
cognition._resolve_memory_backend = lambda: "kusto"
cognition._get_kusto_config = lambda: ("https://x.kusto.windows.net", "Eva")
cognition._kusto_query_direct = fake_query
cognition._get_table_columns = fake_table_columns
cognition._embed_texts = lambda texts: {}
cognition._st.kusto_metadata_cache.clear()

PORT = 8899
BASE = f"http://127.0.0.1:{PORT}"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
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
        with patch("bridge.skills.socket.getaddrinfo", return_value=[(0, 0, 0, "", ("8.8.8.8", 443))]):
            safe, _error, pinned_ip = _safe_external_url("https://skills.example.invalid/import")
        check("external URL validation resolves a public target", safe and pinned_ip == "8.8.8.8")

        # 1. Eva'rise an imported source.
        st, body = req("POST", "/v1/skills/evarise", {"source_type": "paste", "content": "A guide to summarizing web pages."})
        check("evarise returns 200", st == 200)
        draft = body.get("draft", {})
        check("evarise draft has name", draft.get("name") == "Summarize a webpage")
        check("evarise draft has category", draft.get("category") == "Documents & Data")
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
        check("list has 1 draft skill", len(skills) == 1 and skills[0]["Status"] == "draft" and skills[0]["Category"] == "Documents & Data")

        # 4. Disable via PATCH.
        st, body = req("PATCH", "/v1/skills/" + sid, {"status": "disabled", "category": "Information & Research"})
        check("patch disable returns 200", st == 200 and body.get("skill", {}).get("Status") == "disabled")
        check("patch updates category", body.get("skill", {}).get("Category") == "Information & Research")

        # 5. Runtime injection: a matching message should surface the skill.
        #    Re-enable first, then check _build_memory_context (lexical fallback,
        #    no embedding key) injects an [Active Skill] block.
        req("PATCH", "/v1/skills/" + sid, {"status": "active"})
        ctx = m._build_memory_context("please summarize this web article for me")
        check("runtime injection includes the skill", "[Active Skill: Summarize a webpage]" in ctx)
        check("runtime injection includes instructions", "5 bullet summary" in ctx)

        # 6. An unrelated message should NOT inject it.
        ctx2 = m._build_memory_context("what is your favorite color")
        check("no injection for unrelated message", "[Active Skill:" not in ctx2)

        # 7. Delete (soft) and confirm it drops from the list.
        st, body = req("DELETE", "/v1/skills/" + sid)
        check("delete returns 200", st == 200 and body.get("status") == "deleted")
        st, body = req("GET", "/v1/skills")
        check("deleted skill removed from list", len(body.get("skills", [])) == 0)

        # 8. Editing a shipped skill creates an override instead of another
        # managed seed version that startup backfill may replace.
        _STORE["Skills"].append({
            "SkillId": "skill-weather", "Name": "Weather Report",
            "Description": "Provide current weather and forecast for a location",
            "Category": "Information & Research",
            "Instructions": "Use the requested location.", "Tools": "weather-news,data-retrieval",
            "Tags": "weather,forecast", "Source": "seed", "Status": "active",
            "CreatedAt": "2026-08-15T00:00:00Z", "UpdatedAt": "2026-08-15T00:00:00Z",
        })
        st, body = req("PATCH", "/v1/skills/skill-weather", {
            "instructions": "Use Seattle when the request has no location."
        })
        edited_weather = body.get("skill", {})
        check("editing a seed creates a user override", st == 200 and edited_weather.get("Source") == "user-override")
        st, body = req("PATCH", "/v1/skills/skill-weather", {
            "config": {"defaults": {"default_location": "Seattle"}, "allowed_fallbacks": ["Use web search"]}
        })
        config = json.loads(body.get("skill", {}).get("Config", "{}"))
        check("weather default persists as structured Config", st == 200 and config.get("defaults", {}).get("default_location") == "Seattle")

        # 9. Validation: missing instructions is rejected.
        st, body = req("POST", "/v1/skills", {"name": "x"})
        check("create without instructions rejected (400)", st == 400)

    finally:
        server.shutdown()

    print("\n" + ("ALL SKILLS E2E TESTS PASSED" if not failures
                  else f"{len(failures)} FAILED: {failures}"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
