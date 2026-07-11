#!/usr/bin/env python3
"""
Eva Static Tests - Run in CI without a live bridge.
Tests Python syntax, import integrity, config safety, and Kusto ingest logic.

Usage:
    python3 tools/test_static.py
"""

import json
import os
import re
import sys
import importlib.util

PASS = 0
FAIL = 0
WARN = 0

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def report(name, ok, detail=""):
    global PASS, FAIL, WARN
    if ok is True:
        PASS += 1
        tag = f"{GREEN}PASS{RESET}"
    elif ok is None:
        WARN += 1
        tag = f"{YELLOW}WARN{RESET}"
    else:
        FAIL += 1
        tag = f"{RED}FAIL{RESET}"
    suffix = f"  ({detail})" if detail else ""
    print(f"  [{tag}] {name}{suffix}")


# ═══════════════════════════════════════════════════════════════════
#  Section 1: File Integrity
# ═══════════════════════════════════════════════════════════════════

def test_required_files():
    """All required project files exist."""
    required = [
        "index.html",
        "config.example.json",
        "config.local.example.js",
        "core/style.css",
        "core/js/options.js",
        "core/js/gpt-core.js",
        "core/js/gl-google.js",
        "core/js/lm-studio.js",
        "core/js/copilot.js",
        "core/js/aig.js",
        "core/js/dalle3.js",
        "core/js/external.js",
        "core/js/sessions.js",
        "core/js/voice.js",
        "tools/acp_bridge.py",
        "tools/kusto_mcp.py",
        ".gitignore",
    ]
    for f in required:
        report(f"file_exists:{f}", os.path.isfile(f), "missing" if not os.path.isfile(f) else "")


def test_no_secrets_committed():
    """Sensitive files are not in the repo."""
    forbidden = [
        "config.json",
        "config.local.js",
        ".env",
        ".env.local",
        "msal_token_cache.json",
    ]
    for f in forbidden:
        exists = os.path.isfile(f)
        report(f"not_committed:{f}", not exists,
               "COMMITTED - remove immediately!" if exists else "")


# ═══════════════════════════════════════════════════════════════════
#  Section 2: Config Safety
# ═══════════════════════════════════════════════════════════════════

def test_config_example_clean():
    """config.example.json has no real values."""
    with open("config.example.json") as f:
        cfg = json.load(f)
    for k, v in cfg.items():
        if isinstance(v, str) and v and not v.startswith("sk-FAKE") and not v.startswith("ghp_EXAMPLE"):
            report(f"config_example_clean:{k}", False, f"non-empty value: '{v[:20]}...'")
            return
    report("config_example_clean", True)


def test_no_hardcoded_keys():
    """No API keys/tokens hardcoded in source files."""
    patterns = [
        (r'sk-[a-zA-Z0-9]{20,}', "OpenAI API key"),
        (r'ghp_[a-zA-Z0-9]{36}', "GitHub PAT"),
        (r'AKIA[0-9A-Z]{16}', "AWS Access Key"),
        (r'AIza[0-9A-Za-z_-]{35}', "Google API Key"),
    ]
    scan_dirs = ["core/js", "tools"]
    scan_exts = {".js", ".py", ".html"}

    for d in scan_dirs:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for fname in files:
                ext = os.path.splitext(fname)[1]
                if ext not in scan_exts or fname.endswith(".min.js"):
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath) as f:
                    content = f.read()
                for pattern, label in patterns:
                    if re.search(pattern, content):
                        report(f"no_hardcoded_keys:{fpath}", False, f"found {label}")
                        return
    report("no_hardcoded_keys", True)


# ═══════════════════════════════════════════════════════════════════
#  Section 3: Python Module Integrity
# ═══════════════════════════════════════════════════════════════════

def test_python_syntax():
    """All Python files compile without errors."""
    py_files = ["tools/acp_bridge.py", "tools/kusto_mcp.py", "tools/test_eva.py", "tools/eval/run.py",
                 "tools/sqlite_memory.py", "tools/test_phase0.py", "tools/test_phase1.py",
                 "tools/test_phase2.py", "tools/test_static.py"]
    # Include all bridge package files
    bridge_dir = "tools/bridge"
    if os.path.isdir(bridge_dir):
        for name in sorted(os.listdir(bridge_dir)):
            if name.endswith(".py"):
                py_files.append(os.path.join(bridge_dir, name))
    for py in py_files:
        if not os.path.isfile(py):
            report(f"python_syntax:{py}", None, "file missing")
            continue
        try:
            with open(py) as f:
                compile(f.read(), py, "exec")
            report(f"python_syntax:{py}", True)
        except SyntaxError as e:
            report(f"python_syntax:{py}", False, str(e))


def test_artifact_filename_validation():
    """Generated artifact filenames accept only safe local names."""
    spec = importlib.util.spec_from_file_location("acp_bridge", "tools/acp_bridge.py")
    if spec is None or spec.loader is None:
        report("artifact_name_validator_import", False, "could not load tools/acp_bridge.py")
        return
    acp_bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acp_bridge)

    cases = [
        ("out.pdf", True),
        ("a-b_c.1.txt", True),
        ("../etc/passwd", False),
        (".hidden", False),
        (".", False),
        ("..", False),
        ("a/b", False),
        ("", False),
        ("x" * 129, False),
        ("x" * 128, True),
    ]
    for name, expected in cases:
        label = name if name else "empty"
        report(f"artifact_name:{label}", acp_bridge._valid_artifact_name(name) is expected)


# ═══════════════════════════════════════════════════════════════════
#  Section 4: Kusto Ingest CSV Logic (Unit Tests)
# ═══════════════════════════════════════════════════════════════════

def test_csv_quoting_logic():
    """Verify CSV row generation handles commas, quotes, JSON correctly."""
    # Simulate the bridge's CSV row builder
    import json as _json

    def build_csv_row(columns, row_obj):
        vals = []
        for col in columns:
            v = row_obj.get(col, "")
            if v is None:
                vals.append("")
            elif isinstance(v, bool):
                vals.append("true" if v else "false")
            elif isinstance(v, (int, float)):
                vals.append(str(v))
            elif isinstance(v, (dict, list)):
                j = _json.dumps(v)
                vals.append('"' + j.replace('"', '""') + '"')
            else:
                s = str(v).replace("\n", "\\n").replace("\r", "")
                if ',' in s or '"' in s:
                    vals.append('"' + s.replace('"', '""') + '"')
                else:
                    vals.append(s)
        return ",".join(vals)

    # Test 1: Simple values (no commas)
    row = build_csv_row(["A", "B"], {"A": "hello", "B": "world"})
    report("csv_simple", row == "hello,world", f"got: {row}")

    # Test 2: Value with comma gets quoted
    row = build_csv_row(["A", "B"], {"A": "red, green, blue", "B": "ok"})
    expected = '"red, green, blue",ok'
    report("csv_comma_quoting", row == expected, f"got: {row}")

    # Test 3: JSON dict gets double-quote escaped
    row = build_csv_row(["A", "B"], {"A": "x", "B": {"key": "val"}})
    expected = 'x,"{""key"": ""val""}"'
    report("csv_json_dict", row == expected, f"got: {row}")

    # Test 4: JSON with commas (the original bug)
    row = build_csv_row(["T", "C", "S", "D"],
                        {"T": "2026-01-01", "C": "test", "S": "active",
                         "D": _json.dumps({"cluster": "https://example.com", "database": "Eva"})})
    # D is a pre-serialized string containing commas - must be quoted
    assert ',"' in row and 'https://example.com' in row, f"bad row: {row}"
    report("csv_json_commas", True)

    # Test 5: Boolean values
    row = build_csv_row(["A", "B"], {"A": True, "B": False})
    report("csv_booleans", row == "true,false", f"got: {row}")

    # Test 6: None/missing values
    row = build_csv_row(["A", "B", "C"], {"A": "x", "C": None})
    report("csv_none_handling", row == "x,,", f"got: {row}")

    # Test 7: Numeric values
    row = build_csv_row(["A", "B"], {"A": 42, "B": 3.14})
    report("csv_numeric", row == "42,3.14", f"got: {row}")

    # Test 8: Value with quotes
    row = build_csv_row(["A"], {"A": 'say "hello"'})
    expected = '"say ""hello"""'
    report("csv_quote_escaping", row == expected, f"got: {row}")

    # Test 9: Newlines get escaped
    row = build_csv_row(["A"], {"A": "line1\nline2"})
    report("csv_newline_escape", row == "line1\\nline2", f"got: {row}")


# ═══════════════════════════════════════════════════════════════════
#  Section 5: HTML Model Selector
# ═══════════════════════════════════════════════════════════════════

def test_model_selector():
    """All expected model values present in the selector."""
    with open("index.html") as f:
        html = f.read()

    # Extract model select content
    match = re.search(r'<select id="selModel"[^>]*>(.*?)</select>', html, re.DOTALL)
    if not match:
        report("model_selector_found", False)
        return
    report("model_selector_found", True)

    selector_html = match.group(1)
    values = re.findall(r'value="([^"]+)"', selector_html)

    required_models = ["gpt-4o", "copilot-acp", "aig", "gemini", "lm-studio", "dall-e-3"]
    for model in required_models:
        report(f"model_in_selector:{model}", model in values,
               "missing" if model not in values else "")

    # AIG should be labelled as Eva
    if 'Eva' in selector_html:
        report("model_eva_label", True)
    else:
        report("model_eva_label", False, "AIG option should reference 'Eva'")

    aig_match = re.search(r'<select id="selAIGBackend"[^>]*>(.*?)</select>', html, re.DOTALL)
    aig_values = re.findall(r'value="([^"]+)"', aig_match.group(1)) if aig_match else []
    report("aig_backend_lmstudio_option", "lmstudio" in aig_values,
           "missing" if "lmstudio" not in aig_values else "")


# ═══════════════════════════════════════════════════════════════════
#  Section 6: JavaScript Function Routing
# ═══════════════════════════════════════════════════════════════════

def test_js_routing_functions():
    """Required routing functions exist in JS files."""
    required = {
        "aigSend": "core/js/aig.js",
        "trboSend": "core/js/gpt-core.js",
        "geminiSend": "core/js/gl-google.js",
        "lmsSend": "core/js/lm-studio.js",
        "copilotSend": "core/js/copilot.js",
        "dalle3Send": "core/js/dalle3.js",
        "renderEvaResponse": "core/js/options.js",
        "getSystemPrompt": "core/js/options.js",
        "getLmStudioBaseUrl": "core/js/options.js",
        "getLmStudioModel": "core/js/options.js",
    }
    for fn, expected_file in required.items():
        if not os.path.isfile(expected_file):
            report(f"js_function:{fn}", None, f"{expected_file} missing")
            continue
        with open(expected_file) as f:
            content = f.read()
        found = re.search(rf'(?:async\s+)?function\s+{fn}\s*\(', content)
        report(f"js_function:{fn}", found is not None,
               f"not found in {expected_file}" if not found else "")


# ═══════════════════════════════════════════════════════════════════
#  Section 7: Seed File Validation
# ═══════════════════════════════════════════════════════════════════

def test_seed_file():
    """Kusto seed file exists and is valid."""
    seed_path = "tools/eva_seed.kql"
    if not os.path.isfile(seed_path):
        report("seed_file_exists", None, "tools/eva_seed.kql not found")
        return
    report("seed_file_exists", True)

    with open(seed_path) as f:
        content = f.read()

    # Must contain table creation commands
    required_tables = ["SelfState", "Knowledge", "Conversations", "EmotionState",
                       "HeuristicsIndex", "MemorySummaries", "Reflections", "Goals", "EmotionBaseline",
                       "BackgroundProposals", "BackgroundActivity", "MemoryEvents",
                       "MemoryProjectionReceipts"]
    for tbl in required_tables:
        if f".create-merge table {tbl}" in content or f".create table {tbl}" in content:
            report(f"seed_table:{tbl}", True)
        else:
            report(f"seed_table:{tbl}", False, "missing table creation")

    # Must NOT contain real data (no real names, cluster URLs, etc.)
    for pattern in [r'192\.168\.', r'sk-[a-zA-Z0-9]{20}', r'ghp_[a-zA-Z0-9]{36}']:
        if re.search(pattern, content):
            report("seed_no_secrets", False, f"pattern {pattern} found")
            return
    report("seed_no_secrets", True)


def test_goals_static_contract():
    """Goals schema and MCP read contract are wired."""
    seed_path = "tools/eva_seed.kql"
    mcp_path = "tools/kusto_mcp.py"
    if not os.path.isfile(seed_path):
        report("goals_seed_file", None, "tools/eva_seed.kql not found")
        return
    if not os.path.isfile(mcp_path):
        report("goals_mcp_file", None, "tools/kusto_mcp.py not found")
        return

    with open(seed_path) as f:
        seed = f.read()
    with open(mcp_path) as f:
        mcp = f.read()

    report("goals_seed_table", ".create-merge table Goals" in seed,
           "missing Goals table" if ".create-merge table Goals" not in seed else "")
    allowed_match = re.search(r"allowed_tables\s*=\s*\{[^}]*\"Goals\"", mcp, re.DOTALL)
    report("goals_allowed_tables", allowed_match is not None,
           "Goals missing from allowed_tables" if allowed_match is None else "")
    report("goals_active_tool_method", "def _tool_eva_get_active_goals" in mcp,
           "missing _tool_eva_get_active_goals" if "def _tool_eva_get_active_goals" not in mcp else "")


def test_background_static_contract():
    """Background proposal schema and bridge routes are wired."""
    seed_path = "tools/eva_seed.kql"
    bridge_path = "tools/acp_bridge.py"
    bridge_core_path = "tools/bridge/core.py"
    if not os.path.isfile(seed_path):
        report("background_seed_file", None, "tools/eva_seed.kql not found")
        return
    if not os.path.isfile(bridge_path) and not os.path.isfile(bridge_core_path):
        report("background_bridge_file", None, "tools/acp_bridge.py not found")
        return

    with open(seed_path) as f:
        seed = f.read()
    # Read from the core module if the bridge has been modularized
    active_bridge = bridge_core_path if os.path.isfile(bridge_core_path) else bridge_path
    with open(active_bridge) as f:
        bridge = f.read()

    for table_name in ("BackgroundProposals", "BackgroundActivity"):
        report(f"background_seed_table:{table_name}", f".create-merge table {table_name}" in seed,
               f"missing {table_name} table" if f".create-merge table {table_name}" not in seed else "")
    for endpoint in ("/v1/background/status", "/v1/background/proposals", "/v1/background/activity"):
        report(f"background_bridge_endpoint:{endpoint}", endpoint in bridge,
               f"missing {endpoint}" if endpoint not in bridge else "")
    proposals_loopback = re.search(r"def _background_proposals\(self\):.*?_is_loopback_bind\(\)", bridge, re.DOTALL)
    activity_loopback = re.search(r"def _background_activity\(self\):.*?_is_loopback_bind\(\)", bridge, re.DOTALL)
    report("background_proposals_loopback_read", proposals_loopback is not None,
           "background proposals read endpoint must check loopback bind" if proposals_loopback is None else "")
    report("background_activity_loopback_read", activity_loopback is not None,
           "background activity read endpoint must check loopback bind" if activity_loopback is None else "")


def test_mcp_config():
    """mcp.json is valid and contains well-formed server entries (local-only, skipped in CI)."""
    mcp_path = "mcp.json"
    exists = os.path.isfile(mcp_path)
    if not exists:
        # mcp.json is local-only (like config.json); skip when absent
        return
    try:
        with open(mcp_path) as f:
            data = json.load(f)
        report("mcp_json_valid", True)
    except Exception as e:
        report("mcp_json_valid", False, str(e))
        return
    servers = data.get("mcpServers", {})
    report("mcp_json_has_servers", isinstance(servers, dict) and bool(servers),
           "mcpServers must be a non-empty object" if not (isinstance(servers, dict) and bool(servers)) else "")
    secret_pattern = re.compile(r"(sk-[a-zA-Z0-9]{10}|ghp_[a-zA-Z0-9]{10}|Bearer\s+\S{10})", re.IGNORECASE)
    for name, cfg in servers.items():
        has_command = isinstance(cfg.get("command"), str) and bool(cfg["command"])
        report(f"mcp_server_command:{name}", has_command,
               "missing or empty 'command'" if not has_command else "")
        has_args = isinstance(cfg.get("args"), list)
        report(f"mcp_server_args:{name}", has_args,
               "'args' must be a list" if not has_args else "")
        env = cfg.get("env", {})
        for key, val in env.items():
            leaked = secret_pattern.search(str(val))
            report(f"mcp_server_env_clean:{name}:{key}", not leaked,
                   "env value looks like a real secret" if leaked else "")


def test_eval_contract():
    """Behavioral eval files and fixture JSON are valid."""
    eval_dir = "tools/eval"
    fixtures_dir = os.path.join(eval_dir, "fixtures")
    skill_path = ".github/eva-eval.skill.md"
    allowed_checker_types = {
        "regex_must_match",
        "regex_must_not_match",
        "contains_any",
        "contains_all",
        "not_contains",
        "json_shape",
        "capability_invoked",
        "length_max_chars",
        "llm_judge",
    }
    seen_fixture_ids = {}
    report("eval_dir_exists", os.path.isdir(eval_dir), "missing tools/eval" if not os.path.isdir(eval_dir) else "")
    report("eval_fixtures_dir_exists", os.path.isdir(fixtures_dir), "missing tools/eval/fixtures" if not os.path.isdir(fixtures_dir) else "")
    report("eval_skill_exists", os.path.isfile(skill_path), "missing .github/eva-eval.skill.md" if not os.path.isfile(skill_path) else "")
    if not os.path.isdir(fixtures_dir):
        return

    fixture_files = sorted(
        os.path.join(fixtures_dir, name)
        for name in os.listdir(fixtures_dir)
        if name.endswith(".json")
    )
    report("eval_fixture_files_present", bool(fixture_files), "no fixture JSON files found" if not fixture_files else "")
    for path in fixture_files:
        try:
            with open(path) as f:
                data = json.load(f)
            report(f"eval_fixture_json:{path}", True)
        except Exception as error:
            report(f"eval_fixture_json:{path}", False, str(error))
            continue

        fixtures = data.get("fixtures", [])
        has_fixtures = isinstance(fixtures, list) and bool(fixtures)
        report(f"eval_fixture_list:{path}", has_fixtures,
               "fixtures must be a non-empty list" if not has_fixtures else "")
        for fixture in fixtures:
            fixture_id = fixture.get("id", "<missing-id>") if isinstance(fixture, dict) else "<invalid-fixture>"
            checkers = fixture.get("checkers", []) if isinstance(fixture, dict) else []
            fixture_location = f"{path}:{fixture_id}"
            previous_location = seen_fixture_ids.get(fixture_id)
            report(f"eval_fixture_id_unique:{fixture_location}", previous_location is None,
                   f"duplicate id previously seen in {previous_location}" if previous_location else "")
            if previous_location is None:
                seen_fixture_ids[fixture_id] = path
            has_checkers = isinstance(checkers, list) and bool(checkers)
            report(f"eval_fixture_has_checker:{fixture_id}", has_checkers,
                   "missing checkers" if not has_checkers else "")
            if not isinstance(checkers, list):
                continue
            for index, checker in enumerate(checkers):
                checker_type = checker.get("type") if isinstance(checker, dict) else None
                valid_type = checker_type in allowed_checker_types
                report(f"eval_checker_type:{fixture_id}:{index}", valid_type,
                       f"unknown checker type {checker_type!r}" if not valid_type else "")


# ═══════════════════════════════════════════════════════════════════
#  Runner
# ═══════════════════════════════════════════════════════════════════

def main():
    print(f"\n{BOLD}{'=' * 55}{RESET}")
    print(f"{BOLD} Eva Static Tests (CI-safe, no bridge needed){RESET}")
    print(f"{'=' * 55}\n")

    sections = [
        ("File Integrity", [test_required_files, test_no_secrets_committed]),
        ("Config Safety", [test_config_example_clean, test_no_hardcoded_keys]),
        ("Python Integrity", [test_python_syntax, test_artifact_filename_validation]),
        ("Kusto CSV Logic", [test_csv_quoting_logic]),
        ("HTML Model Selector", [test_model_selector]),
        ("JS Routing Functions", [test_js_routing_functions]),
        ("Seed File", [test_seed_file]),
        ("Goals Static Contract", [test_goals_static_contract]),
        ("Background Static Contract", [test_background_static_contract]),
        ("MCP Config", [test_mcp_config]),
        ("Behavioral Eval", [test_eval_contract]),
    ]

    for name, tests in sections:
        print(f"{BOLD}── {name} ──{RESET}")
        for t in tests:
            try:
                t()
            except Exception as e:
                report(t.__name__, False, f"exception: {e}")
        print()

    total = PASS + FAIL + WARN
    print(f"{'=' * 55}")
    print(f" Results: {total} checks")
    print(f"   {GREEN}PASS:{RESET} {PASS}   {RED}FAIL:{RESET} {FAIL}   {YELLOW}WARN:{RESET} {WARN}")

    if FAIL == 0:
        print(f"\n {GREEN}{BOLD}✓ All checks passed!{RESET}")
    else:
        print(f"\n {RED}{BOLD}✗ {FAIL} check(s) failed{RESET}")

    print(f"{'=' * 55}\n")
    sys.exit(1 if FAIL > 0 else 0)


if __name__ == "__main__":
    main()
