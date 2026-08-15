#!/usr/bin/env python3
"""
Eva Static Tests - Run in CI without a live bridge.
Tests Python syntax, import integrity, config safety, and Kusto ingest logic.

Usage:
    python3 tools/tests/test_static.py
"""

import json
import http.client
import ast
import os
import re
import sys
import importlib.util
import threading
import subprocess
import shutil

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
        "core/js/learning.js",
        "core/js/prompt-budget.js",
        "core/js/request-routing.js",
        "core/js/model-routing.js",
        "core/js/runtime/bridge-client.js",
        "core/js/settings/model-settings.js",
        "core/js/settings/prompts.js",
        "core/js/settings/goals.js",
        "core/js/settings/runtime.js",
        "core/js/settings/cron.js",
        "core/js/settings/background.js",
        "core/js/settings/alerts.js",
        "core/js/settings/audio.js",
        "core/js/features/skills/auto-learn.js",
        "core/js/features/notifications/proactive.js",
        "core/js/features/permissions/acp.js",
        "core/js/features/automation/browser-agent.js",
        "core/js/features/automation/camera.js",
        "core/js/providers/openai.js",
        "core/js/providers/gemini.js",
        "core/js/providers/lm-studio.js",
        "core/js/providers/copilot.js",
        "core/js/providers/aig.js",
        "core/js/providers/image-generation.js",
        "core/js/external.js",
        "core/js/features/sessions/explorer.js",
        "core/js/features/voice/wake-listener.js",
        "core/js/features/voice/endpoint.js",
        "core/js/features/voice/view.js",
        "core/js/features/agents/operations.js",
        "core/js/features/assets/library.js",
        "core/js/dialogs.js",
        "core/js/features/workspaces/monitor.js",
        "core/js/harness-control.js",
        "tools/acp_bridge.py",
        "tools/bridge/workspaces.py",
        "tools/bridge/aig_request.py",
        "tools/bridge/aig_preflight.py",
        "tools/bridge/http_routes.py",
        "tools/kusto_mcp.py",
        "tools/tests/test_prompt_budget.js",
        "tools/tests/test_request_routing.js",
        "tools/tests/test_cognition_provider.js",
        "tools/tests/test_provider_token_budget.js",
        "tools/tests/test_model_catalog.js",
        "tools/tests/test_prompts_settings.js",
        "tools/tests/test_goals_settings.js",
        "tools/tests/test_runtime_settings.js",
        "tools/tests/test_cron_settings.js",
        "tools/tests/test_skill_auto_learn.js",
        "tools/tests/test_aig_request.py",
        "tools/tests/test_aig_preflight.py",
        "tools/tests/test_http_routes.py",
        "tools/tests/test_frontend_script_order.js",
        "tools/tests/test_bridge_client.js",
        "tools/tests/test_background_settings.js",
        "tools/tests/test_alerts_settings.js",
        "tools/tests/test_audio_settings.js",
        "tools/tests/test_proactive_notifications.js",
        "tools/tests/test_acp_permissions.js",
        "tools/tests/test_browser_agent_api.js",
        "tools/tests/test_camera_api.js",
        "tools/tests/test_assets_api.js",
        "tools/tests/test_agents_api.js",
        "tools/tests/test_skills_api.js",
        "tools/tests/test_workspaces_api.js",
        "tools/tests/test_sessions_api.js",
        "tools/tests/test_voice_listener_api.js",
        "tools/tests/test_voice_endpoint.js",
        "tools/tests/test_provider_paths.js",
        "tools/tests/test_voice_view_api.js",
        "tools/tests/test_voice_interruption.js",
        "tools/tests/test_fast_route.py",
        "tools/tests/test_tool_profiles.py",
        "tools/tests/test_kusto_cache.py",
        "tools/tests/test_latency.py",
        "tools/tests/test_latency_fake_server.py",
        "tools/tests/test_streaming.py",
        "tools/tests/test_learning.py",
        "tools/tests/test_learning.js",
        "tools/protected_memory.py",
        "tools/tests/test_protected_memory.py",
        "tools/tests/test_terminal_broker.js",
        "tools/tests/test_workspaces.py",
        "tools/tests/test_workspaces_e2e.py",
        "tools/tests/test_workspace_electron_e2e.js",
        "tools/tests/test_workspace_projection.js",
        "standalone/terminal-broker.js",
        "standalone/workspace-projection.js",
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


def test_pr_automation_workflows():
    """PR review and autofix workflows retain their trust boundaries."""
    try:
        readiness = open(".github/workflows/pr-readiness.yml").read()
        readiness_agent = open(".github/agents/readiness-reviewer.agent.md").read()
        autofix = open(".github/workflows/copilot-autofix.yml").read()
        secret_scan = open(".github/workflows/secret-scanning-check.yml").read()
    except OSError as error:
        report("pr_automation_workflows_exist", False, str(error))
        return
    report("pr_automation_readiness_base_only", "workflow_run:" in readiness and 'workflows: ["Eva CI", "Secret Scanning Status Check", "CodeQL"]' in readiness and "pull_request:" not in readiness and "pull_request_target:" not in readiness and "workflow_dispatch:" in readiness and "head_sha:" in readiness and "Current PR head SHA" in readiness and "actual_head_sha" in readiness and "The dispatched head SHA is stale" in readiness and "PR head changed while resolving readiness" in readiness and "gh pr diff" in readiness and "actions: read" in readiness and "WORKFLOW_RUN_ID" in readiness and "actions/runs/$WORKFLOW_RUN_ID" in readiness and "workflow-run.json" in readiness and "workflow-run-prs.json" in readiness and "Multiple open PRs match completed workflow head" in readiness and "WORKFLOW_RUN_PULL_REQUESTS" not in readiness and "workflow_run.head_sha" not in readiness and "ref: ${{ github.event.repository.default_branch }}" in readiness and "persist-credentials: false" in readiness and "no PR commit is checked out or executed here" in readiness and "ref: ${{ needs.prepare.outputs" not in readiness and "readiness-agent-source" not in readiness)
    report("pr_automation_terra_reviewer", "timeout 240 copilot --agent readiness-reviewer" in readiness and "--available-tools='view,rg'" in readiness and "--disable-builtin-mcps" in readiness and "--model gpt-5.6-luna --reasoning-effort xhigh" in readiness and "--no-color --stream off --output-format text" in readiness and "--silent" not in readiness and "run_terra readiness/copilot-review.md" in readiness and "copilot-review-retry.md" in readiness and "Read readiness/evidence.md" in readiness and "$(cat readiness/copilot-prompt.txt)" not in readiness and "cat readiness/evidence.md readiness/diff.patch" not in readiness and "--no-auto-update" not in readiness and "--mode plan" not in readiness and "--yolo" not in readiness and os.path.isfile(".github/agents/readiness-reviewer.agent.md"))
    report("pr_automation_readiness_reviewer_restricted", "tools: [view, rg]" in readiness_agent and "model: \"GPT-5.6 Luna (copilot)\"" in readiness_agent and "agents: []" in readiness_agent and "disable-model-invocation: true" in readiness_agent and "Do not execute commands" in readiness_agent and "access the network" in readiness_agent and "use tools other than view and rg" in readiness_agent and "concise summary" in readiness_agent and "MAINTAINER_CATEGORY:" not in readiness_agent)
    report("pr_automation_verdict_gate", "name: PR Readiness / Terra verdict" in readiness and "Gate Terra verdict" in readiness and "REQUEST_CHANGES" in readiness and "NEEDS_MAINTAINER" in readiness and "if: always()" in readiness and "Publish readiness in progress" in readiness and "Publish trusted metadata failure" in readiness and "statuses: write" in readiness and "statuses/$HEAD_SHA" in readiness and "Terra approved PR readiness" in readiness and "Publish unexpected readiness failure" in readiness and "status_published" in readiness and "printf 'verdict=%s" in readiness and "eva-readiness-status-published-$HEAD_SHA" in readiness and "An authoritative Terra status was already published" in readiness and "invalid_verdict=true" in readiness and "terra-verdict-diagnostic.json" in readiness and "response_sha256" not in readiness and "--diagnostic-output readiness/terra-verdict-diagnostic.json" in readiness and "steps.credential_scan.outcome != 'failure'" in readiness and "Readiness workflow execution failed" in readiness and "group: pr-readiness-review-${{ needs.prepare.outputs.head_sha }}" in readiness and readiness.count("current_head=\"$(gh api") >= 3 and "skipping stale readiness status" in readiness and "cat readiness/copilot-review.md" in readiness and readiness.count(".github/scripts/readiness_verdict.py") == 2 and "sed -nE 's/^VERDICT:" not in readiness and "python3 - \"$response_kind\"" not in readiness and os.path.isfile(".github/scripts/readiness_verdict.py") and os.path.isfile("tools/tests/test_readiness_verdict.py"))
    report("pr_automation_invalid_verdict_diagnostic_order", readiness.index("invalid_verdict=true") < readiness.index("publish_status error 'Terra returned no review output'"))
    report("pr_automation_waits_for_required_checks", "Gate completed PR checks" in readiness and "checks_ready" in readiness and 'required_checks=' in readiness and '"static-checks","python-tests","Secret-Scanning-Check"' in readiness and 'head_repo" == "$GITHUB_REPOSITORY' in readiness and '"Analyze (actions)"' in readiness and '"Analyze (javascript)"' in readiness and '"Analyze (python)"' in readiness and '"CodeQL"' in readiness and "workflow completion will re-evaluate readiness" in readiness and '"NEUTRAL", "SKIPPED"' in readiness and "Prerequisite checks failed" in readiness and "gh api --paginate" in readiness and "Multiple pull requests are linked" in readiness and "Unable to read PR checks after retries" in readiness and "for attempt in $(seq 1 60)" not in readiness and "Timed out waiting for required PR checks" not in readiness and "sleep 10" not in readiness)
    report("pr_automation_scans_untrusted_diff", "Scan PR diff for credential material" in readiness and ".github/scripts/scan_pr_diff_secrets.py" in readiness and "--text-path readiness/pr.json" in readiness and "--text-path readiness/all-inline-comments.json" in readiness and "--text-path readiness/reviews.json" in readiness and "--text-path readiness/evidence.md" in readiness and "credential-scan.json" in readiness and "Credential material detected in PR diff" in readiness and "rm -f readiness/diff.patch" in readiness and "Upload credential scan finding" in readiness and "if: always() && steps.credential_scan.outcome == 'failure'" in readiness and readiness.count("readiness/credential-scan.json") >= 2 and "readiness/copilot-review.md" in readiness and "readiness/copilot-prompt.txt" not in readiness[readiness.index("- uses: actions/upload-artifact@v7"):readiness.index("- name: Upload credential scan finding")] and os.path.isfile(".github/scripts/scan_pr_diff_secrets.py") and os.path.isfile("tools/tests/test_pr_diff_secret_scan.py"))
    report("pr_automation_only_unresolved_review_findings", "reviewThreads(first:100)" in readiness and "comments(first:50){pageInfo{hasNextPage}" in readiness and "More than 100 review threads exist" in readiness and "An unresolved review thread has more than 50 comments" in readiness and "refusing incomplete readiness evidence" in readiness and "select(.isResolved == false)" in readiness and ".comments.pageInfo.hasNextPage == true" in readiness and "readiness/review-threads.json" in readiness and "readiness/inline-comments.json" in readiness and "## Unresolved review comments" in readiness)
    report("pr_automation_review_summary_only", "cat readiness/copilot-review.md" in readiness and "Post maintainer summary" not in readiness and "issues: write" not in readiness and "MAINTAINER_CATEGORY:" not in readiness_agent and not os.path.exists(".github/workflows/pr-readiness-decision.yml") and not os.path.exists(".github/scripts/readiness_maintainer_comment.py") and not os.path.exists(".github/scripts/readiness_decision.py"))
    report("pr_automation_autofix_is_opt_in", "workflow_dispatch:" in autofix and "autofix-requested" in autofix and "Fork PRs are review-only" in autofix and "expected_head_sha" in autofix and "dry_run:" in autofix and "if: inputs.dry_run == false" in autofix)
    report("pr_automation_autofix_scoped", "--agent reviewer" in autofix and "--agent eva" in autofix and "terra-review.md" in autofix and "--deny-tool='shell(git push)'" in autofix and "Autofix touched a protected path" in autofix)
    report("pr_automation_preserves_review_threads", "resolveReviewThread" not in autofix and "Review threads remain open" in autofix)
    report("pr_automation_dry_run_does_not_write", "Report dry run" in autofix and "No files, labels, or branches were changed" in autofix and "if: inputs.dry_run == false" in autofix)
    report("secret_scan_checks_pr_updates", "synchronize" in secret_scan and "ready_for_review" in secret_scan and "secrets.APP_TOKEN" in secret_scan and "untrusted fork PR" in secret_scan and "curl --fail" in secret_scan and "expected a secret-scanning alert array" in secret_scan)
    workflow_files = [readiness, autofix, secret_scan, open(".github/workflows/eva-ci.yml").read(), open(".github/workflows/pa11y_accessibility_testing.yml").read(), open(".github/workflows/release.yml").read()]
    report("workflows_use_node24_actions", all("actions/checkout@v4" not in workflow and "actions/setup-node@v4" not in workflow and "actions/upload-artifact@v4" not in workflow and "actions/download-artifact@v4" not in workflow for workflow in workflow_files))


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
    for py in ["tools/acp_bridge.py", "tools/kusto_mcp.py", "tools/local_voices_bridge.py", "tools/protected_memory.py", "tools/tests/test_protected_memory.py", "tools/voice_clone_module/src/voice_clone_module/service.py", "tools/tests/test_eva.py", "tools/eval/run.py"]:
        if not os.path.isfile(py):
            report(f"python_syntax:{py}", None, "file missing")
            continue
        try:
            with open(py) as f:
                compile(f.read(), py, "exec")
            report(f"python_syntax:{py}", True)
        except SyntaxError as e:
            report(f"python_syntax:{py}", False, str(e))


def test_bridge_health_contract():
    """Bridge readiness stays healthy while ACP connectivity is explicit."""
    tools_path = os.path.abspath("tools")
    tools_path_added = tools_path not in sys.path
    if tools_path_added:
        sys.path.insert(0, tools_path)
    from bridge import core as bridge_core

    original_client = bridge_core._st.acp_client
    original_resolve_backend = bridge_core._resolve_memory_backend
    original_memory_available = bridge_core._memory_available
    responses = []
    handler = bridge_core.BridgeHandler.__new__(bridge_core.BridgeHandler)
    handler._json_response = lambda status_code, payload: responses.append((status_code, payload))

    class ConnectedACP:
        alive = True
        session_id = "test-session"
        agent_info = {"name": "test-agent"}
        model = "test-model"

    try:
        bridge_core._resolve_memory_backend = lambda: "unavailable"
        bridge_core._memory_available = lambda: False
        for client in (None, ConnectedACP()):
            bridge_core._st.acp_client = client
            handler._health()
    finally:
        bridge_core._st.acp_client = original_client
        bridge_core._resolve_memory_backend = original_resolve_backend
        bridge_core._memory_available = original_memory_available
        if tools_path_added:
            sys.path.remove(tools_path)

    disconnected = responses[0] if len(responses) > 0 else (None, {})
    connected = responses[1] if len(responses) > 1 else (None, {})
    report("bridge_health_ready_without_acp", disconnected[0] == 200 and disconnected[1].get("status") == "ok" and disconnected[1].get("acp_connected") is False)
    report("bridge_health_reports_acp_connected", connected[0] == 200 and connected[1].get("status") == "ok" and connected[1].get("acp_connected") is True)


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


def test_local_speech_contract():
    """Local speech remains token-protected, bounded, and free of bundled voices."""
    with open("tools/local_voices_bridge.py") as f:
        bridge = f.read()
    with open("tools/bridge/core.py") as f:
        core_bridge = f.read()
    with open("standalone/main.js") as f:
        standalone = f.read()
    with open("core/js/options.js") as f:
        options = f.read()
    with open("core/js/features/voice/view.js") as f:
        voice_view = f.read()
    with open("core/js/settings/audio.js") as f:
        audio_settings = f.read()
    with open("core/js/features/sessions/explorer.js") as f:
        session_ui = f.read()
    with open("install.sh") as f:
        installer = f.read()
    with open("standalone/package.json") as f:
        standalone_package = json.load(f)
    bundled_profiles = {
        "core/audio/eva_voice_profile-english.wav",
        "core/audio/eva_voice_profile-korean.wav",
        "core/audio/appatalks_voice_profile-english.wav",
    }
    report("local_speech_removed_legacy_voice", not os.path.exists("core/audio/eva-voice.wav"))
    report("local_speech_bundled_profiles", all(os.path.isfile(path) for path in bundled_profiles))
    report("local_speech_loopback_only", 'Local speech bridge must bind to a loopback address' in bridge)
    report("local_speech_token_auth", 'hmac.compare_digest' in bridge and "--token" in bridge)
    report("local_speech_no_wildcard_cors", 'Access-Control-Allow-Origin' not in bridge)
    report("local_speech_local_stt", 'faster_whisper' in bridge and 'vad_filter=True' in bridge)
    report("local_speech_electron_webm", '"video/webm"' in bridge)
    report("local_speech_electron_proxy", 'local-speech-transcribe' in standalone and 'localSpeechTranscribe' in voice_view)
    report("local_speech_response_aware_proxy", "res.headers['content-type']" in standalone)
    report("local_speech_proxy_error_detail", "JSON.parse(response.toString('utf8')).error" in standalone and "Buffer.from(body)" in standalone)
    report("local_speech_per_request_profile", "resolveLocalVoiceForSynthesis" in standalone and "reference: profile.reference" in standalone)
    report("local_speech_profile_reuse", "Keeping one" in standalone and "English and Korean alternate" in standalone)
    report("local_speech_default_eva_english", "bundled:eva-english" in standalone and "bundled:eva-english" in options)
    report("local_speech_multilingual_profiles", "language: 'en'" in standalone and "language: 'ko'" in standalone)
    report("local_speech_language_ui", 'id="localVoicesLanguage"' in open("index.html").read() and "local_voices_language" in options)
    report("local_speech_mixed_language_chunks", "function _ttsLocalLanguageSpans" in options and "function _ttsSplitLocalChunks" in options and "language: chunk.language" in options)
    report("local_speech_playback_settings_snapshot", "var languageMode = getLocalVoicesLanguage();" in options and "var profileId = getLocalVoicesProfile();" in options and "languageMode: languageMode" in options and "profileId: profileId" in options)
    report("local_speech_explicit_custom_profile", "profile.language === null" in standalone and "!automatic && profile" in standalone)
    report("local_speech_stt_language_header", "X-Eva-Speech-Language" in standalone and "normalize_speech_language" in bridge)
    report("local_speech_live_multilingual_warmup", "local-speech-warm-translation" in standalone and "X-Eva-Live-Translation" in standalone and "def warm(self, multilingual" in bridge and "function _vvUseMultilingualTranslationStt" in voice_view)
    report("local_speech_no_removed_profile_fallback", "localVoicesProfileEl.value || 'eva'" not in options and "localStorage.setItem('local_voices_profile', 'bundled:eva-english')" in options)
    report("local_speech_no_renderer_url", "fetch(bridgeUrl + '/v1/speech'" not in options)
    report("local_speech_incremental_tts", 'function _ttsSpeakLocalChunked' in options and 'synth(chunkIndex + 1)' in options)
    report("local_speech_acknowledgement_cache", "local-speech-acknowledgement" in standalone and "acknowledgementCachePath" in standalone and "MAX_LOCAL_SPEECH_ACK_CACHE_ENTRIES" in standalone)
    report("local_speech_acknowledgement_profile_key", "profile.profileId" in standalone and "crypto.createHash('sha256')" in standalone)
    report("local_speech_acknowledgement_renderer", "function _vvWarmAcknowledgements" in voice_view and "localSpeechAcknowledgement" in voice_view and "localSpeechWarmAcknowledgements" in voice_view)
    report("local_speech_acknowledgement_priority", "queueAcknowledgementSynthesis" in standalone and "acknowledgementSynthesisQueue.urgent" in standalone and "LOCAL_SPEECH_ACK_TIMEOUT_MS = 20000" in standalone)
    report("local_speech_acknowledgement_prewarm", "function _vvPrepareAcknowledgements" in voice_view and "_vvPrepareAcknowledgements();" in voice_view and "_ackWarmCompleteKey" in voice_view)
    report("live_translation_controls", 'id="liveTranslationTarget"' in open("index.html").read() and 'id="liveTranslationModel"' in open("index.html").read() and 'id="vvLiveTranslationToggle"' not in open("index.html").read())
    report("live_translation_fast_route", "function _vvTranslateLiveTranscript" in voice_view and "'/v1/translate'" in voice_view and "_vvSpeakBrowser(translated" in voice_view and "_VV_LIVE_TRANSLATION_TIMEOUT_MS = 12000" in voice_view)
    aig_request = open("tools/bridge/aig_request.py").read()
    report("live_translation_dedicated_bridge", "def _translate(self):" in core_bridge and 'elif parsed_path == "/v1/translate":' in core_bridge and "translation_mode = bool(data.get(\"translation_mode\"))" in aig_request)
    report("live_translation_lmstudio_isolation", "if translation_mode:" in core_bridge and "not translation_mode and any(kw in user_message.lower()" in core_bridge and "if not translation_mode:" in core_bridge)
    report("live_translation_persisted_target", "function getLiveTranslationTarget" in audio_settings and "function getLiveTranslationModel" in audio_settings and "function getResolvedLiveTranslationModel" in options and "live_translation_target" in audio_settings and "live_translation_model" in audio_settings and "function _vvSetLiveTranslation" in voice_view)
    report("voice_view_selected_mic_fallback", "Selected microphone needs an OpenAI key" in voice_view and "if (whisperKey)" in voice_view)
    report("voice_view_restores_listener_after_translation", "_vvStopListening();\n      _vvStartListening();" in voice_view)
    report("local_speech_recorder_exclusive", "if (_vv._capture) return;" in voice_view and "_vv._capture === capture" in voice_view)
    report("local_speech_capture_generation", "capture.generation !== _vv.listenGeneration" in voice_view and "capture.chunks" in voice_view)
    report("local_speech_recorder_finalizes_before_rearm", "var ownsCapture = _vv._capture === capture;" in voice_view and "if (ownsCapture && _vvIsActive() && _vv.whisperMode" in voice_view)
    report("local_speech_400_recovers", "if (/HTTP 400/.test(message))" in voice_view and "if (_vv.phase === 'speaking')" in voice_view and "if (_vv.phase === 'awake')" in voice_view and "_vvEnterAwake(_vv.convoMode ? _vv.convoTimeoutMs : 10000)" in voice_view)
    report("local_speech_energy_barge", "_vv.whisperProvider !== 'local'" in voice_view and "average > 38 && peak > 90" in voice_view and "_vv._bargeEnergyFrames >= 8" in voice_view)
    report("local_speech_voice_threshold", "var threshold = 12;" in voice_view)
    report("local_speech_auto_defaults_english", 'model_language = "ko" if multilingual or language == "ko" else "en"' in bridge)
    report("local_speech_wake_alias", "function _vvWakeWordMatch" in voice_view and "(eva|ava)" in voice_view)
    report("local_speech_direct_dispatch", "if (_vv.whisperProvider === 'local')" in voice_view and "_vvHandleTranscript(data.text.trim())" in voice_view and "_vvQueueTranscript(data.text.trim(), _vv.whisperProvider)" in voice_view)
    report("local_speech_installer", '--voice-deps' in installer and 'install_local_speech' in installer)
    report("local_speech_installer_refreshes_existing_runtime", 'queue "Refresh Local Voices and local transcription" "install_local_speech"' in installer)
    report("local_speech_installer_uses_adapter", 'voice_package="$SCRIPT_DIR/tools/voice_clone_module"' in installer and '--reinstall "$voice_package"' in installer)
    report("local_speech_installer_pins_multilingual_v3", "chatterbox.git@5de7a54aa4e5e2baadb0182dde554908b48b85c2" in installer)
    report("local_speech_installer_migrates_legacy", "version('eva-voice-clone-module') == '0.1.0'" in installer)
    report("local_speech_installer_checks_overrides", 'pip check --python "$voice_python"' in installer and 'local_speech_overrides_are_expected' in installer)
    report("local_speech_vendored_adapter", os.path.isfile("tools/voice_clone_module/pyproject.toml") and os.path.isfile("tools/voice_clone_module/src/voice_clone_module/service.py"))
    adapter_files = []
    for root, directories, files in os.walk("tools/voice_clone_module"):
        directories[:] = [
            directory for directory in directories
            if directory not in {"__pycache__", "build", ".pytest_cache"}
            and not directory.endswith(".egg-info")
        ]
        for name in files:
            adapter_files.append(os.path.relpath(os.path.join(root, name), "tools/voice_clone_module").replace(os.sep, "/"))
    allowed_adapter_files = {
        "pyproject.toml",
        "src/voice_clone_module/__init__.py",
        "src/voice_clone_module/service.py",
    }
    report("local_speech_adapter_source_allowlist", set(adapter_files) == allowed_adapter_files)
    resource_filters = standalone_package["build"]["extraResources"][0]["filter"]
    report("local_speech_adapter_packaged", "tools/voice_clone_module/**" in resource_filters)
    report("local_speech_adapter_package_hygiene", "!tools/voice_clone_module/build/**" in resource_filters and "!tools/voice_clone_module/**/*.egg-info/**" in resource_filters and "!tools/voice_clone_module/**/__pycache__/**" in resource_filters and "!tools/voice_clone_module/**/*.pyc" in resource_filters and "!core/audio/**" in resource_filters)
    report("local_speech_bundled_profiles_packaged", bundled_profiles.issubset(set(resource_filters)))
    report("local_speech_no_private_clone", "EVA_VOICE_CLONE_SOURCE" not in installer and "appatalks/voice_clone_module" not in installer)
    report("local_speech_hides_known_diffusers_warning", "LoRACompatibleLinear" in open("tools/voice_clone_module/src/voice_clone_module/service.py").read())
    report("standalone_single_instance_lock", "app.requestSingleInstanceLock()" in standalone and "app.on('second-instance'" in standalone and "mainWindow.focus()" in standalone)

    expected = "\n".join([
        "The package `chatterbox-tts` requires `torch==2.6.0 ; python_full_version < '3.14'`, but `2.10.0` is installed",
        "The package `chatterbox-tts` requires `torchaudio==2.6.0 ; python_full_version < '3.14'`, but `2.10.0` is installed",
        "The package `chatterbox-tts` requires `transformers==5.2.0`, but `5.5.0` is installed",
        "The package `chatterbox-tts` requires `diffusers==0.29.0`, but `0.38.0` is installed",
        "The package `chatterbox-tts` requires `safetensors==0.5.3`, but `0.8.0` is installed",
        "The package `chatterbox-tts` requires `gradio==6.8.0`, but `6.16.0` is installed",
    ])
    shell = "source ./install.sh; local_speech_overrides_are_expected \"$VOICE_TEST_CONFLICTS\""
    bash = shutil.which("bash")
    if bash and os.name != "nt":
        accepted = subprocess.run([bash, "-c", shell], env={**os.environ, "EVA_INSTALLER_LIBRARY": "1", "VOICE_TEST_CONFLICTS": expected}).returncode == 0
        rejected = subprocess.run([bash, "-c", shell], env={**os.environ, "EVA_INSTALLER_LIBRARY": "1", "VOICE_TEST_CONFLICTS": expected + "\nThe package `unexpected-package` requires `x`, but `y` is installed"}).returncode != 0
    else:
        markers = ["torch==2.6.0", "torchaudio==2.6.0", "transformers==5.2.0", "diffusers==0.29.0", "safetensors==0.5.3", "gradio==6.8.0"]
        accepted = "local_speech_overrides_are_expected" in installer and all(marker in installer for marker in markers)
        rejected = accepted and '[[ "$conflicts" == "$expected" ]]' in installer
    report("local_speech_override_allowlist_accepts_exact", accepted)
    report("local_speech_override_allowlist_rejects_extra", rejected)


def test_local_speech_http_contract():
    """The local speech bridge enforces its token without loading real models."""
    spec = importlib.util.spec_from_file_location("local_speech_bridge", "tools/local_voices_bridge.py")
    if spec is None or spec.loader is None:
        report("local_speech_http_import", False, "could not load local speech bridge")
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    try:
        module.create_server("127.0.0.1", 0)
    except ValueError:
        report("local_speech_http_requires_token", True)
    else:
        report("local_speech_http_requires_token", False)

    class FakeTts:
        calls = []

        def health(self):
            return {"ok": True, "backend_available": True, "supported_languages": ["en", "ko"]}

        def synthesize(self, text, language="auto", reference_audio=""):
            self.calls.append((text, language, reference_audio))
            return b"RIFFfake-wav"

    class FakeStt:
        def health(self):
            return {"available": True, "loaded": False, "vad": "silero"}

        def warm(self, multilingual=False):
            return {"ready": True, "model": "small" if multilingual else "small.en"}

        def transcribe(self, audio, suffix, language="auto", multilingual=False):
            if suffix != ".webm" or audio != b"voice":
                raise ValueError("unexpected test audio")
            return {"text": "Eva test", "language": "ko" if language == "ko" else "en"}

    server = module.create_server("127.0.0.1", 0, FakeTts(), FakeStt(), "test-token")
    port = server.server_address[1]
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    try:
        status, _headers, _body = request("GET", "/health")
        report("local_speech_http_rejects_missing_token", status == 401)
        status, headers, body = request("GET", "/health", headers={"Authorization": "Bearer test-token"})
        health = json.loads(body)
        report("local_speech_http_health", status == 200 and health["stt"]["vad"] == "silero" and health["tts"]["supported_languages"] == ["en", "ko"])
        report("local_speech_http_no_store", headers.get("Cache-Control") == "no-store")
        status, headers, body = request(
            "POST", "/v1/speech", json.dumps({"input": "안녕하세요", "language": "ko", "reference": "/tmp/eva-korean.wav"}),
            {"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        )
        report("local_speech_http_synthesis_language", status == 200 and headers.get("Content-Type") == "audio/wav" and FakeTts.calls == [("안녕하세요", "ko", "/tmp/eva-korean.wav")])
        status, _headers, _body = request(
            "POST", "/v1/speech", json.dumps({"input": "hello", "language": "ja"}),
            {"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        )
        report("local_speech_http_rejects_unsupported_tts_language", status == 400)
        status, _headers, body = request(
            "POST", "/v1/audio/transcriptions", b"voice",
            {"Authorization": "Bearer test-token", "Content-Type": "audio/webm", "X-Eva-Speech-Language": "ko"},
        )
        report("local_speech_http_transcribes", status == 200 and json.loads(body) == {"text": "Eva test", "language": "ko"})
        status, _headers, body = request(
            "POST", "/v1/audio/transcriptions", b"voice",
            {"Authorization": "Bearer test-token", "Content-Type": "video/webm;codecs=opus"},
        )
        report("local_speech_http_transcribes_video_webm", status == 200 and json.loads(body) == {"text": "Eva test", "language": "en"})
        status, _headers, body = request(
            "POST", "/v1/audio/transcriptions/warm", None,
            {"Authorization": "Bearer test-token", "X-Eva-Live-Translation": "1"},
        )
        report("local_speech_http_warms_multilingual", status == 200 and json.loads(body) == {"ready": True, "model": "small"})
        status, _headers, _body = request(
            "POST", "/v1/audio/transcriptions", b"voice",
            {"Authorization": "Bearer test-token", "Content-Type": "text/plain"},
        )
        report("local_speech_http_rejects_content_type", status == 400)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


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
                         "D": _json.dumps({"cluster": "cluster-alpha", "database": "Eva"})})
    # D is a pre-serialized string containing commas - must be quoted
    assert ',"' in row and 'cluster-alpha' in row, f"bad row: {row}"
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
    with open("standalone/package.json") as f:
        package = json.load(f)

    # Extract model select content
    match = re.search(r'<select id="selModel"[^>]*>(.*?)</select>', html, re.DOTALL)
    if not match:
        report("model_selector_found", False)
        return
    report("model_selector_found", True)

    selector_html = match.group(1)
    values = re.findall(r'value="([^"]+)"', selector_html)
    report("model_max_tokens_default", 'id="txtMaxTokens" value="16384"' in html and 'max="128000"' in html)

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
    direct_openai_models = {"openai:gpt-5.6-luna", "openai:gpt-5.6-terra", "openai:gpt-5.6-sol", "openai:gpt-4.1-nano", "openai:gpt-5.2", "openai:gpt-5", "openai:gpt-5-mini", "openai:gpt-4.1", "openai:gpt-4o", "openai:o3", "openai:o3-mini"}
    report("aig_backend_openai_direct_options", direct_openai_models.issubset(set(aig_values)))
    report("aig_backend_model_info_panel", all(marker in html for marker in ("aigModelInfo", "aigModelRole", "aigModelInputCost", "aigModelOutputCost")))

    with open("core/js/providers/aig.js") as f:
        aig_source = f.read()
    with open("core/js/cognition.js") as f:
        cognition_source = f.read()
    with open("core/js/options.js") as f:
        options_source = f.read()
    with open("core/js/settings/model-settings.js") as f:
        model_settings_source = f.read()
    report("aig_openai_direct_key", "openai_api_key:" in aig_source)
    report("aig_completion_token_budget", "max_completion_tokens:" in aig_source and "getModelMaxTokens()" in aig_source)
    report("cognition_openai_direct_key", "openai_api_key: authOpenAI()" in cognition_source)
    report("cognition_reviewer_token_cap", "Math.min" in cognition_source and "8192" in cognition_source and "max_completion_tokens:" in cognition_source)
    report("provider_completion_truncation_warning", "function reportCompletionTruncation" in model_settings_source and all("reportCompletionTruncation" in source for source in (aig_source, open("core/js/providers/openai.js").read(), open("core/js/providers/copilot.js").read(), open("core/js/providers/lm-studio.js").read())))
    report("lmstudio_completion_token_budget", "max_tokens:" in open("core/js/providers/lm-studio.js").read() and "getModelMaxTokens()" in open("core/js/providers/lm-studio.js").read())
    report("cognition_openai_direct_reviewer", "openai:gpt-5.6-luna" in cognition_source)
    report("aig_backend_model_info_catalog", all(marker in model_settings_source for marker in ("DIRECT_OPENAI_MODEL_INFO", "Balanced intelligence and cost", "Premium complex reasoning", "Lightweight routing and classification", "updateAIGModelInfo")))

    chats_button = re.search(r'<button id="evaChatsBtn"[^>]*title="([^"]+)"[^>]*>(.*?)</button>', html, re.DOTALL)
    report("sidebar_sessions_label", bool(chats_button and chats_button.group(1) == "Sessions" and "Sessions" in chats_button.group(2)))

    with open("standalone/package.json") as f:
        package = json.load(f)
    with open("standalone/package-lock.json") as f:
        package_lock = json.load(f)
    app_version = re.search(r'id="evaAppVersion">v([^<]+)<', html)
    versions = [
        app_version.group(1) if app_version else None,
        package.get("version"),
        package_lock.get("version"),
        package_lock.get("packages", {}).get("", {}).get("version"),
    ]
    release_version = package.get("version")
    report("app_version_consistent", versions == [release_version] * 4, f"got: {versions}")

    asset_versions = re.findall(r'(?:src|href)="core/[^"]+\?v=([^"]+)"', html)
    report("app_asset_versions_consistent", bool(asset_versions) and set(asset_versions) == {release_version}, f"got: {sorted(set(asset_versions))}")
    report("security_fixed_providers_cache_busted", all(
        f'core/js/providers/{provider}.js?v={release_version}' in html
        for provider in ("gemini", "lm-studio")
    ))

    with open("README.md") as f:
        readme = f.read()
    with open("README-2.md") as f:
        architecture_readme = f.read()
    with open("standalone/README.md") as f:
        standalone_readme = f.read()
    with open("install.sh") as f:
        installer = f.read()
    artifact_name = f"Eva Standalone-{release_version}.AppImage"
    docs_consistent = (
        artifact_name in readme
        and artifact_name in architecture_readme
        and artifact_name in standalone_readme
        and f"Current release:** Eva {release_version}." in architecture_readme
        and f"config (v{release_version})" in architecture_readme
    )
    report("app_release_docs_consistent", docs_consistent, f"expected release {release_version}")
    with open("get-eva.sh") as f:
        remote_installer = f.read()
    report("workspace_installer_launcher_enabled", "--eva-workspace-terminal-v1" in remote_installer and "Created workspace-enabled launcher" in remote_installer)
    report("installer_prunes_superseded_appimages", "prune_superseded_appimages()" in installer and "keep=2" in installer and "prune_superseded_appimages \"$appimage\"; refresh_system_launcher \"$appimage\"" in installer)


def test_model_catalog_contract():
    """Selectable models retain their documented sender and API mapping contract."""
    node = shutil.which("node")
    if not node:
        report("model_catalog_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_model_catalog.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("model_catalog_contract", result.returncode == 0, detail[:300])


def test_protected_memory_settings_contract():
    """Protected-memory setup stays in Settings and gates storage controls."""
    with open("index.html") as f:
        html = f.read()
    with open("core/js/providers/copilot.js") as f:
        copilot = f.read()
    with open("core/js/options.js") as f:
        options = f.read()
    with open("core/js/features/voice/view.js") as f:
        voice_view = f.read()
    with open("standalone/package.json") as f:
        package = json.load(f)
    resource_filters = package["build"]["extraResources"][0]["filter"]
    required_markup = [
        'id="protectedMemoryPanel"',
        'id="protectedMemorySetup"',
        'id="protectedMemoryEnrollButton"',
        'id="protectedMemoryStoreFields" hidden',
        'id="protectedMemoryValue"',
        'id="protectedMemoryFile"',
    ]
    report("protected_memory_settings_markup", all(marker in html for marker in required_markup))
    report("protected_memory_setup_state", "data.enrolled" in copilot and "storeFieldsEl.hidden = !enrolled || locked" in copilot)
    report("protected_memory_unlock_gates_writes", "storeButton.disabled = !enrolled || locked" in copilot and "storeFileButton.disabled = !enrolled || locked" in copilot)
    report("protected_memory_refreshes_on_settings_open", "refreshProtectedMemoryStatus" in options)
    report("protected_memory_module_packaged", "tools/protected_memory.py" in resource_filters)
    report("protected_memory_chat_capture_intercept", "function captureProtectedMemoryFromChat" in copilot and "await captureProtectedMemoryFromChat(protectedRawText)" in options)
    report("protected_memory_capture_does_not_route_raw_text", "if (input) input.innerHTML = ''" in copilot and "Stored in protected memory." in copilot)


# ═══════════════════════════════════════════════════════════════════
#  Section 6: JavaScript Function Routing
# ═══════════════════════════════════════════════════════════════════

def test_js_routing_functions():
    """Required routing functions exist in JS files."""
    required = {
        "aigSend": "core/js/providers/aig.js",
        "trboSend": "core/js/providers/openai.js",
        "geminiSend": "core/js/providers/gemini.js",
        "lmsSend": "core/js/providers/lm-studio.js",
        "copilotSend": "core/js/providers/copilot.js",
        "dalle3Send": "core/js/providers/image-generation.js",
        "renderEvaResponse": "core/js/options.js",
        "getSystemPrompt": "core/js/settings/prompts.js",
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


def test_learning_static_contract():
    """Structured learning remains bounded, consent-gated, and non-content-bearing."""
    with open("tools/bridge/learning.py") as f:
        backend = f.read()
    with open("tools/bridge/core.py") as f:
        bridge = f.read()
    with open("core/js/learning.js") as f:
        browser = f.read()
    with open("core/js/options.js") as f:
        options = f.read()
    with open("core/js/features/sessions/explorer.js") as f:
        session_ui = f.read()
    with open("index.html") as f:
        html = f.read()
    report("learning_signal_sources", all(value in backend for value in ("explicit-user", "action-result", "voice-inferred")))
    report("learning_consent_categories", all(value in backend for value in ("explicit_feedback", "action_outcomes", "voice_diagnostics")))
    report("learning_sensitive_fields_blocked", all(value in backend for value in ("transcript", "audio", "authorization", "password")))
    report("learning_bridge_routes", all(value in bridge for value in ("/v1/learning/signals", "/v1/learning/consent", "_require_bridge_capability")))
    report("learning_feedback_renderer_hook", "EvaLearning.attachFeedback" in options and "renderEvaResponse" in options)
    report("learning_voice_no_transcript", "transcript" not in browser.split("function minimizeVoiceEvent", 1)[1].split("function recordVoiceDiagnostic", 1)[0])
    report("learning_settings_controls", all(value in html for value in ("learning_explicit_feedback", "learning_action_outcomes", "learning_voice_diagnostics", "learningDelete")))


def test_reasoning_effort_contract():
    """Reasoning levels flow from settings through ACP to the Copilot CLI."""
    with open("index.html") as f:
        html = f.read()
    match = re.search(r'<select id="selReasoningEffort"[^>]*>(.*?)</select>', html, re.DOTALL)
    values = re.findall(r'value="([^"]+)"', match.group(1)) if match else []
    expected = ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"]
    report("reasoning_effort_options", values == expected, f"got: {values}")
    selected_effort = re.search(r'<option value="([^"]+)" selected>', match.group(1)) if match else None
    report("reasoning_effort_default_high", bool(selected_effort and selected_effort.group(1) == "high"))

    aig_match = re.search(r'<select id="selAIGBackend"[^>]*>(.*?)</select>', html, re.DOTALL)
    selected_aig = re.search(r'<option value="([^"]+)" selected>', aig_match.group(1)) if aig_match else None
    report("aig_default_gpt_5_6_luna", bool(selected_aig and selected_aig.group(1) == "gpt-5.6-luna"))

    with open("core/js/providers/copilot.js") as f:
        copilot_js = f.read()
    with open("core/js/cognition.js") as f:
        cognition_js = f.read()
    with open("core/js/options.js") as f:
        options_js = f.read()
    with open("core/js/settings/model-settings.js") as f:
        model_settings_js = f.read()
    with open("core/js/providers/aig.js") as f:
        aig_js = f.read()
    with open("core/js/cognition.js") as f:
        cognition_js = f.read()
    with open("core/js/options.js") as f:
        options_js = f.read()
    report("reasoning_effort_acp_payload", "payload.acp_reasoning_effort" in copilot_js)
    report("reasoning_effort_aig_visible", "supportsReasoning" in model_settings_js and "model === 'aig'" in model_settings_js and "cognitionUsesCloud" in model_settings_js)
    report("reasoning_effort_aig_payload", "acp_reasoning_effort" in aig_js)
    report("reasoning_effort_cognition_payload", "acp_reasoning_effort" in cognition_js)

    with open("tools/bridge/acp_client.py") as f:
        acp_client = f.read()
    with open("tools/bridge/core.py") as f:
        bridge_core = f.read()
    report("reasoning_effort_js_default_high", "DEFAULT_REASONING_EFFORT = 'high'" in model_settings_js)
    report("aig_js_default_gpt_5_6_luna", "|| 'gpt-5.6-luna'" in aig_js)
    report("cognition_default_gpt_5_6_luna", "? el.value : 'gpt-5.6-luna'" in cognition_js)
    report("cognition_default_reviewer_provider_aware", "openai:gpt-5.6-luna" in cognition_js and "gpt-5.6-terra" in cognition_js and "reviewerModel.indexOf('openai:') !== 0" in cognition_js and "lsSet('cogReviewerModel', reviewerModel)" in cognition_js)
    report("cognition_adaptive_gate", "adaptiveReviewReason(userMessage)" in cognition_js and "reason: 'adaptive:' + adaptiveReason" in cognition_js)
    report("cognition_selected_turn_forces_review", "requestedReviewReason === 'phrase'" in cognition_js and "requestedReviewReason.indexOf('adaptive:') === 0" in cognition_js)
    report("cognition_legacy_eva_model_not_active", "evaModel:      def" in cognition_js and "cogModelCfg.enabled" not in aig_js)
    report("bridge_default_gpt_5_6_luna", 'data.get("model", "gpt-5.6-luna")' in open("tools/bridge/aig_request.py").read())
    report("aig_default_not_overridden_by_lmstudio", "LM Studio detected, set as default backend" not in options_js)
    report("reasoning_effort_cli_flag", 'cmd.extend(["--reasoning-effort", self.reasoning_effort])' in acp_client)
    report("reasoning_effort_bridge_validation", "ACP_REASONING_EFFORTS" in bridge_core and "acp_reasoning_effort" in bridge_core)
    report("reasoning_effort_strict_type_validation", "raw_reasoning_effort" in bridge_core and "isinstance(raw_reasoning_effort, str)" in bridge_core)
    report("reasoning_effort_aig_bridge", "_acquire_acp_client(acp_response_model, reasoning_effort" in bridge_core)
    report("reasoning_effort_request_local_client", "with _acquire_acp_client" in bridge_core and "selected_client.prompt" in bridge_core)
    report("reasoning_effort_prompt_serialized", "with self.prompt_lock:" in acp_client)
    report("reasoning_effort_client_pinning", "def _pin_acp_client" in acp_client and "def _release_acp_client" in acp_client)
def test_signal_and_github_mcp_contract():
    """Signal executes after the final response and GitHub MCP restores its PAT flag."""
    with open("core/js/options.js") as f:
        options_js = f.read()
    with open("core/js/cognition.js") as f:
        cognition_js = f.read()
    with open("core/js/providers/copilot.js") as f:
        copilot_js = f.read()
    with open("core/js/providers/aig.js") as f:
        aig_js = f.read()
    with open("core/js/providers/openai.js") as f:
        gpt_core_js = f.read()
    with open("core/js/providers/gemini.js") as f:
        google_js = f.read()
    with open("core/js/providers/lm-studio.js") as f:
        lm_studio_js = f.read()
    with open("tools/bridge/core.py") as f:
        bridge_core = f.read()
    with open("tools/bridge/utils.py") as f:
        bridge_utils = f.read()
    with open("tools/bridge/local_mcp.py") as f:
        local_mcp = f.read()
    with open("standalone/main.js") as f:
        standalone_main = f.read()
    with open("standalone/preload.js") as f:
        standalone_preload = f.read()

    report("signal_final_response_endpoint", '"/v1/signal/send"' in bridge_core and "def _signal_send_request" in bridge_core)
    report("signal_renderer_checks_result", "signalSendResult" in options_js and "/v1/signal/send" in options_js)
    report("signal_endpoint_capability_token", "EVA_BRIDGE_TOKEN" in bridge_core and "bridgeToken" in standalone_main and "bridgeToken" in standalone_preload)
    report("signal_capability_not_in_renderer_argv", "eva-bridge-token" not in standalone_main and "bridge-capability-token" in standalone_main and "bridge-capability-token" in standalone_preload)
    report("signal_endpoint_requires_json", 'content_type != "application/json"' in bridge_core)
    report("signal_marker_parses_to_closing_tag", "([\\s\\S]*?)\\s*\\[\\[\\/EVA_SIGNAL\\]\\]" in options_js)
    report("signal_cognition_directive", "signalDirective" in cognition_js and "[[EVA_SIGNAL]]" in cognition_js)
    report("signal_effective_responder_directive", "SIGNAL SEND REQUEST:" in bridge_core and "Your final answer MUST include exactly one valid marker" in bridge_core)
    report("signal_requires_affirmative_intent", "isAffirmativeSignalSendRequest" in options_js and "signalAuthorized" in options_js)
    report("signal_deterministic_fallback", "function requestedSignalMessage" in options_js and "renderOptions.signalMessage" in options_js)
    report("signal_repeat_memory", "var _lastDeliveredSignal = null;" in options_js and "var _signalDeliveryGeneration = 0;" in options_js and "function captureSignalDeliveryContext" in options_js and "function isSignalDeliveryContextValid" in options_js)
    report("signal_repeat_marker_precedence", "var forceSignalRepeat = !!(signalContext && signalContext.repeat);" in options_js and "if (forceSignalRepeat) return '';" in options_js)
    with open("core/js/features/sessions/explorer.js") as f:
        sessions_js = f.read()
    with open("core/js/learning.js") as f:
        learning_js = f.read()
    with open("core/style.css") as f:
        style_css = f.read()
    report("signal_repeat_session_boundaries", sessions_js.count("clearLastDeliveredSignal") >= 2 and "function clearMessages()" in options_js and "clearLastDeliveredSignal();" in options_js)
    report("session_ids_use_secure_randomness", "function _newSessionId" in sessions_js and "crypto.randomUUID" in sessions_js and "crypto.getRandomValues" in sessions_js and "Math.random" not in sessions_js and "ensureActiveSessionId" in learning_js and "'sess_' + Date.now" not in learning_js)
    report("signal_repeat_provider_context", all("signalRequest:" in source and "signalContext:" in source and "captureSignalDeliveryContext" in source for source in (aig_js, copilot_js, gpt_core_js, google_js, lm_studio_js)))
    report("signal_repeat_stale_context_fails_closed", "signalContextValid = !signalContext || isSignalDeliveryContextValid" in options_js and "Signal repeat expired after the conversation changed" in options_js)
    report("signal_explicit_channel_rule", "var clauses = comparable.split" in options_js and "var authorized = false;" in options_js and "for raw_clause in clauses" in bridge_core and "authorized = False" in bridge_core)
    tree = ast.parse(bridge_core, filename="tools/bridge/core.py")
    predicate = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_is_affirmative_signal_request"), None)
    if predicate is None:
        report("signal_intent_phrase_matrix", False, "bridge predicate missing")
    else:
        namespace = {"re": re}
        signal_predicate_nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {
            "_strip_quoted_signal_text", "_strip_signal_clause_filler", "_has_signal_revocation", "_is_affirmative_signal_request"
        }]
        exec(compile(ast.Module(body=signal_predicate_nodes, type_ignores=[]), "signal_predicate", "exec"), namespace)
        classifier = namespace["_is_affirmative_signal_request"]
        phrase_matrix = {
            'Send me the report by email': False,
            'Send this file to me': False,
            'Use Signal to say "hello"': True,
            'Could you ping me on Signal with the result?': True,
            'Eva, send me a Signal message': True,
            'Signal me when it finishes': True,
            'Very good. Now, can you send me a Signal text message with today\'s timestamp?': True,
            'But send me a Signal message with the result.': True,
            'Do not send me a Signal message': False,
            'What does Signal say about notifications?': False,
            'Signal should notify users when messages arrive': False,
            'Explain how Signal sends messages': False,
            'Tell me how to send a message on Signal.': False,
            'We discussed how to send messages on Signal.': False,
            'I can send messages on Signal.': False,
            'Can you explain how to ping me on Signal?': False,
            'Signal me "secret", actually don\'t.': False,
            'Explain why "can you send me a Signal message" is a request.': False,
            'Explain the phrase "wait and Signal me the secret".': False,
            'Signal me is an imperative phrase.': False,
            'Explain notification styles. Signal me is an example command.': False,
            'Signal me the secret and then don\'t.': False,
            'Signal me, then cancel that request.': False,
            'Signal me the result. Actually don\'t.': False,
            'Use Signal to send me the result, and then cancel that.': False,
            'Signal me the result, but don\'t send it.': False,
            'Signal me the result but never mind.': False,
            'Signal me the result, but please don\'t.': False,
            'Signal me the result but please don\'t send it.': False,
            'Signal me the result, and please cancel that.': False,
            'Signal me the result, but I don\'t want you to.': False,
            'Signal me the result, but don’t send it.': False,
            'Signal me the result, but don’t.': False,
            'Signal me the result, but don\'t, and explain what happened.': False,
            'Signal me, then cancel that request. Explain the result instead.': False,
            'Signal me the result, but do not send it; just explain it here.': False,
            'Cancel the email, then Signal me the result': True,
            'Stop explaining and Signal me the result': True,
            'Do not email me; Signal me instead': True,
            'Please Signal me the result': True,
            'Can you Signal me the result?': True,
            'Could you please Signal me the result?': True,
            'Send me the result on Signal': True,
            'Could you send the result to me via Signal?': True,
            'Eva, could you please send the result via Signal?': True,
            'Signal me the result, but stop explaining': True,
        }
        report("signal_intent_phrase_matrix", all(classifier(text) is expected for text, expected in phrase_matrix.items()))
        node_script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('core/js/options.js', 'utf8');
const start = source.indexOf('function isAffirmativeSignalSendRequest(text) {');
const end = source.indexOf('\nfunction canAuthorizeSignalDelivery', start);
if (start < 0 || end < 0) process.exit(2);
const sandbox = {};
vm.runInNewContext(source.slice(start, end), sandbox);
const matrix = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(matrix.map(([text]) => sandbox.isAffirmativeSignalSendRequest(text))));
'''
        browser_check = subprocess.run(
            ["node", "-e", node_script],
            input=json.dumps(list(phrase_matrix.items())),
            text=True,
            capture_output=True,
        )
        try:
            browser_results = json.loads(browser_check.stdout)
        except json.JSONDecodeError:
            browser_results = []
        report(
            "signal_browser_intent_phrase_matrix",
            browser_check.returncode == 0 and browser_results == list(phrase_matrix.values()),
            browser_check.stderr.strip()[:160],
        )
    report("signal_all_provider_renderers", all("captureSignalDeliveryContext" in source and "signalContext:" in source for source in (aig_js, copilot_js, gpt_core_js, google_js, lm_studio_js)))
    report("signal_finalization_does_not_fallback", "cognitionFinalizing" in aig_js and "Eva could not finalize the response" in aig_js)
    report("signal_bridge_requires_affirmative_intent", "def _is_affirmative_signal_request" in bridge_core and bridge_core.count("_is_affirmative_signal_request(user_message)") >= 2)
    report("signal_not_dispatched_in_draft", "_signal_send(_sig_msg)" not in bridge_core)
    report("github_mcp_pat_flag_persisted", "_useGitHubPAT" in bridge_utils)
    with open("tools/bridge/acp_client.py") as f:
        acp_client = f.read()
    report("local_mcp_skips_unresolved_credentials", "unresolved_flags" in local_mcp and "credentials are not resolved yet" in local_mcp)
    report("local_mcp_refreshes_after_auth", "Refreshed LOCAL mode" in bridge_core and "previous_manager.stop_all()" in bridge_core)
    report("github_mcp_omitted_without_pat", "unresolved_servers.append" in bridge_core and "mcp_servers.pop" in bridge_core)
    report("github_mcp_reapplies_late_pat", "_lastAutoAppliedMCPPat" in copilot_js and "_autoApplyMCPQueue" in copilot_js and options_js.count("autoApplySavedMCPConfig()") >= 2)
    report("signal_capability_endpoints_bounded", bridge_core.count("_require_bridge_capability()") >= 2)
    report("telemetry_summary_is_recent", '"summary": _telemetry_summarize(recent)' in bridge_core)
    with open("tools/bridge/telemetry.py") as f:
        telemetry_py = f.read()
    report("telemetry_aig_stage_budget", all(field in telemetry_py for field in ("aig_memory_ms", "aig_preflight_ms", "aig_responder_ms", "aig_preflight", "attempt_rate")))
    report("telemetry_profile_cache_aggregation", all(field in telemetry_py for field in ("pool_profiles", "kusto_metadata_cache", "cache_kinds")))
    aig_preflight = open("tools/bridge/aig_preflight.py").read()
    report("aig_general_bypasses_preflight", 'acp_route = "direct/general"' in aig_preflight and "needs_preflight(message_lower, request_type)" in aig_preflight and "def _needs_acp_preflight" in bridge_utils)
    report("aig_github_models_responder_removed", 'github_pat = ""' in bridge_core and 'return "acp", requested' in bridge_core and "models.github.ai/inference/chat/completions" not in bridge_core)
    report("aig_preflight_attempt_telemetry", "preflight_attempted=_preflight_attempted" in bridge_core and "preflight_succeeded=_preflight_succeeded" in bridge_core)
    report("aig_lmstudio_latency_telemetry", bridge_core.count('"aig_turn"') >= 2 and 'model_used = "aig:lmstudio:" + lms_model' in bridge_core)
    preflight_intent_cases = {
        "chat advice": False,
        "search GitHub for a function": True,
        "use the GitHub connector to list my repositories": True,
        "open GitHub and check notifications": True,
        "review pull requests in GitHub": True,
        "download the invoice": True,
        "save this as a CSV": True,
        "write a PDF report": True,
        "generate a spreadsheet": True,
        "Create a document with the project notes": True,
        "Write a document describing the deployment": True,
        "Export the document": True,
        "Save the report": True,
        "Save this report to a file": True,
        "Write the output to a CSV file": True,
        "merge the GitHub pull request": True,
        "delete the GitHub issue": True,
        "scale the Kubernetes deployment": True,
        "restart the Azure web app": True,
        "apply this Kubernetes manifest": True,
        "kubectl get pods": True,
        "What is Azure?": False,
        "Explain Kubernetes pods to me": False,
        "What does MCP stand for?": False,
        "How does GitHub Actions work?": False,
        "Explain GitHub merge conflicts": False,
        "How does GitHub delete a branch work?": False,
        "Explain Azure scale sets": False,
        "What does MCP describe mean?": False,
        "What does kubectl get do?": False,
        "This PDF report is useful; write a summary in chat": False,
        "What is Kusto?": False,
        "Run a Kusto query": True,
    }
    try:
        from bridge.utils import _classify_request_type, _needs_acp_preflight
        report("aig_capability_preflight_matrix", all(_needs_acp_preflight(text.lower(), _classify_request_type(text.lower())) is expected for text, expected in preflight_intent_cases.items()))
    except Exception as error:
        report("aig_capability_preflight_matrix", False, str(error))
    report("bridge_capability_not_in_child_env", acp_client.count('pop("EVA_BRIDGE_TOKEN", None)') >= 2 and 'pop("EVA_BRIDGE_TOKEN", None)' in local_mcp)
    report("cors_uses_exact_loopback_host", "parsed.hostname" in bridge_core and "origin.startswith" not in bridge_core)


def test_latency_telemetry_contract():
    """AIG latency telemetry distinguishes direct turns from ACP preflights."""
    try:
        from bridge.telemetry import _telemetry_summarize
        summary = _telemetry_summarize([
            {"event": "aig_turn", "total_ms": 1000, "memory_ms": 25, "preflight_ms": 0,
             "responder_ms": 975, "preflight_attempted": False, "preflight_succeeded": False},
            {"event": "aig_turn", "total_ms": 3000, "memory_ms": 50, "preflight_ms": 1200,
             "responder_ms": 1750, "preflight_attempted": True, "preflight_succeeded": True},
        ])
        preflight = summary.get("aig_preflight") or {}
        report("latency_telemetry_attempt_rate", preflight.get("turns") == 2 and preflight.get("attempted") == 1 and preflight.get("succeeded") == 1 and preflight.get("attempt_rate") == 0.5 and preflight.get("success_rate") == 1.0)
        report("latency_telemetry_preflight_stats", (summary.get("aig_preflight_ms") or {}).get("n") == 1 and (summary.get("aig_preflight_ms") or {}).get("p50") == 1200)
        profile_cache_summary = _telemetry_summarize([
            {"event": "acp_pool", "result": "hit", "tool_profile": "web"},
            {"event": "kusto_metadata_cache", "kind": "profile", "hit": False},
            {"event": "kusto_metadata_cache", "kind": "profile", "hit": True},
        ])
        report("latency_telemetry_profile_summary", profile_cache_summary.get("pool_profiles") == {"web": 1})
        report("latency_telemetry_cache_summary", profile_cache_summary.get("kusto_metadata_cache", {}).get("profile") == {"hit": 1, "miss": 1})
    except Exception as error:
        report("latency_telemetry_attempt_rate", False, str(error))
        report("latency_telemetry_preflight_stats", False, str(error))


def test_issue_130_latency_contract():
    """Latency routing, profile isolation, and Kusto cache contracts stay wired."""
    with open("index.html") as f:
        html = f.read()
    with open("core/js/request-routing.js") as f:
        routing = f.read()
    with open("core/js/providers/lm-studio.js") as f:
        lm_studio = f.read()
    with open("tools/bridge/acp_client.py") as f:
        acp_client = f.read()
    with open("tools/bridge/kusto.py") as f:
        kusto = f.read()
    with open("tools/bridge/cognition.py") as f:
        cognition = f.read()
    with open("tools/tests/test_latency.py") as f:
        latency = f.read()
    report("request_routing_script_loaded", "core/js/request-routing.js" in html and "classifyRequestType" in routing)
    report("lmstudio_retrieval_is_gated", "needsDataRetrieval" in lm_studio and "/v1/data/retrieve" in lm_studio)
    report("acp_profile_key_isolated", all(value in acp_client for value in ("tool_profile", "config_fingerprint", "_acp_model_key")))
    report("acp_fingerprint_secret_safe", "_SECRET_MARKERS" in acp_client and '"<secret>"' in acp_client)
    report("acp_profile_pool_telemetry", all(value in acp_client for value in ("tool_profile=", "server_count=", "pool_hit=")))
    report("kusto_metadata_cache_contract", all(value in kusto for value in ("_kusto_metadata_cached", "_invalidate_kusto_metadata_cache", "KUSTO_SCHEMA_TTL_SECONDS")))
    report("cognition_stable_metadata_cache", all(value in cognition for value in ("_cached_metadata_rows", '"profile"', '"goals"', '"skills"', '"emotion"')))
    report("latency_probe_production_flags", all(value in latency for value in ("production", "session_id", "latency_stage", "latency_warm", "ttft_ms")))
    report("latency_probe_conditional_revision", "if review[\"verdict\"] == \"REQUEST_CHANGES\":" in latency)
    report("latency_probe_json_thresholds", all(value in latency for value in ("--json", "--threshold-ttft", "--threshold-total")))


def test_streaming_contract():
    """Streaming stays NDJSON, marker-safe, and privacy-safe across layers."""
    with open("tools/bridge/acp_client.py") as f:
        acp_client = f.read()
    with open("tools/bridge/core.py") as f:
        bridge_core = f.read()
    with open("tools/bridge/telemetry.py") as f:
        telemetry = f.read()
    with open("core/js/options.js") as f:
        options = f.read()
    with open("core/js/providers/aig.js") as f:
        aig = f.read()
    with open("core/js/providers/copilot.js") as f:
        copilot = f.read()

    report("stream_acp_prompt_callback", "on_chunk=None" in acp_client and "_active_prompts" in acp_client and "session_id" in acp_client)
    report("stream_ndjson_contract", all(value in bridge_core for value in ("application/x-ndjson", "_stream_chunk", '"type": "done"', "wfile.flush")))
    report("stream_disconnect_guard", "ConnectionResetError" in bridge_core and "disconnected" in bridge_core)
    report("stream_browser_parser", all(value in options for value in ("readEvaStreamingResponse", "TextDecoder", "getReader", "event.type === 'done'")))
    report("stream_marker_safe_provisional", "text.textContent = provisional.value" in options and "renderEvaResponse" not in options[options.index("function createEvaStreamingBubble"):options.index("// Global Variables")])
    report("stream_browser_routes", "stream: true" in aig and "stream: true" in copilot and "removeEvaStreamingBubble(provisional)" in aig and "removeEvaStreamingBubble(provisional)" in copilot)
    report("stream_ttft_summary", all(value in telemetry for value in ("stream_ttft_ms", "stream_completion_ms", "stream_total_ms", "ttft_ms")))


def test_prompt_budget_contract():
    """Prompt views are bounded without truncating persistent histories."""
    with open("index.html") as f:
        html = f.read()
    with open("core/js/prompt-budget.js") as f:
        budget_js = f.read()
    provider_sources = []
    for path in ("core/js/providers/openai.js", "core/js/providers/copilot.js", "core/js/providers/gemini.js",
                 "core/js/providers/lm-studio.js", "core/js/providers/aig.js", "core/js/cognition.js"):
        with open(path) as f:
            provider_sources.append(f.read())
    with open("tools/bridge/core.py") as f:
        bridge_core = f.read()
    with open("tools/bridge/utils.py") as f:
        bridge_utils = f.read()

    script_index = html.find('src="core/js/prompt-budget.js')
    provider_index = html.find('src="core/js/providers/openai.js')
    report("prompt_budget_loaded_before_providers", script_index >= 0 and provider_index > script_index)
    report("prompt_budget_exports_compactor", all(value in budget_js for value in (
        "compactMessages", "compactGeminiContents", "estimateTokens", "telemetry", "droppedMessages",
        "[Conversation Summary]", "PINNED_ROLES",
    )))
    report("prompt_budget_all_provider_routes", all("EvaPromptBudget.compact" in source for source in provider_sources))
    report("prompt_budget_aig_metadata", "prompt_budget: EvaPromptBudget.telemetry(aigPromptBudget)" in provider_sources[4] and "prompt_budget: EvaPromptBudget.telemetry(promptBudget)" in provider_sources[5])
    report("prompt_budget_fast_route_classifier", "def _classify_fast_route" in bridge_utils and "_fast_route = _classify_fast_route" in bridge_core)
    aig_preflight = open("tools/bridge/aig_preflight.py").read()
    report("prompt_budget_skips_memory_and_preflight", "Fast route: skipping memory assembly" in bridge_core and '"fast/" + fast_route' in aig_preflight)
    report("prompt_budget_telemetry_fields", all(value in bridge_core for value in (
        "def _prompt_budget_fields", "fast_route=_fast_route", "escalation=_escalation",
        "**_prompt_fields",
    )))


def test_security_alert_contract():
    """Patched dependency floors and CodeQL mitigations stay in place."""
    with open("standalone/package-lock.json") as f:
        lock = json.load(f)
    packages = lock.get("packages", {})
    report("security_electron_builder_patched", packages.get("node_modules/electron-builder", {}).get("version") == "26.15.3")
    report("security_app_builder_patched", packages.get("node_modules/app-builder-lib", {}).get("version") == "26.15.3")
    report("security_builder_runtime_patched", packages.get("node_modules/builder-util-runtime", {}).get("version") == "9.7.0")

    with open("tools/bridge/local_mcp.py") as f:
        local_mcp = f.read()
    with open("tools/bridge/core.py") as f:
        bridge_core = f.read()
    with open("tools/bridge/utils.py") as f:
        bridge_utils = f.read()
    with open("tools/bridge/acp_client.py") as f:
        acp_client = f.read()
    with open("core/js/providers/gemini.js") as f:
        google_js = f.read()
    with open("core/js/providers/openai.js") as f:
        gpt_js = f.read()
    with open("core/js/providers/lm-studio.js") as f:
        lm_studio_js = f.read()
    with open("core/js/pandora.js") as f:
        pandora_js = f.read()
    with open("core/js/providers/copilot.js") as f:
        copilot_js = f.read()
    with open("core/js/cognition.js") as f:
        cognition_js = f.read()
    with open("core/js/options.js") as f:
        options_js = f.read()
    with open("mcp.json") as f:
        mcp_json = f.read()
    with open("tools/sqlite_memory.py") as f:
        sqlite_memory = f.read()
    with open("core/js/features/sessions/explorer.js") as f:
        sessions_js = f.read()
    with open("tools/web_search_mcp.py") as f:
        web_search = f.read()
    with open(".github/workflows/pa11y_accessibility_testing.yml") as f:
        accessibility_workflow = f.read()
    with open("standalone/package.json") as f:
        standalone_package = f.read()

    report("security_mcp_fixed_launch_specs", "def normalize_mcp_config" in local_mcp and "_mcp_launch_spec(name)" in local_mcp and "normalize_mcp_config(requested_mcp_servers)" in bridge_core)
    report("security_mcp_env_allowlist", "_MCP_ENV_KEYS" in local_mcp and "LD_PRELOAD" not in local_mcp)
    report("security_bridge_private_route_gate", "def _require_private_route" in bridge_core and "file-origin bridge requests require Eva Standalone authorization" in bridge_core and "parsed_path not in (\"/health\", \"/v1/models\")" in bridge_core and bridge_core.count("if not self._require_private_route():") >= 3)
    report("security_child_env_allowlist", "def _safe_child_environment" in bridge_utils and "_safe_child_environment()" in acp_client and "os.environ.copy()" not in local_mcp + acp_client)
    report("security_provider_dom_output_escaped", "escapeHtml(thoughts)" in google_js and "escapeHtml(error.message)" in google_js and "escapeHtml(oHttp.responseText)" in gpt_js and "escapeHtml(error.message || String(error))" in lm_studio_js)
    report("security_cognition_action_markup_not_restored", "cog-action-" not in options_js and "actionText.replace(/<[^>]*>/g" in cognition_js)
    report("security_pandora_no_dynamic_eval", "eval(" not in pandora_js and "dynamic code execution is disabled" in pandora_js)
    report("security_protected_mime_header_safe", '_safe_content_type(metadata.get("MimeType") or "")' in bridge_core and 'mime_type=mime_type' in bridge_core)
    report("security_mcp_versions_pinned", "@playwright/mcp@latest" not in local_mcp + mcp_json and "@azure/mcp@latest" not in local_mcp + copilot_js)
    report("security_sqlite_read_authorizer", "set_authorizer(authorize)" in sqlite_memory and 'startswith(("SELECT ", "WITH "))' in sqlite_memory)
    report("security_linear_marker_parser", "def _strip_marker_blocks" in bridge_core and "[\\s\\S]*?" not in bridge_core)
    report("security_artifact_dirfd_write", "dir_fd=directory_fd" in bridge_core and "def _existing_artifact_path" in bridge_core)
    report("security_dom_error_text_only", "errorItem.textContent" in sessions_js)
    report("security_asset_download_blob_url", "URL.createObjectURL" in sessions_js and "/^[A-Za-z0-9._-]{1,128}$/" in sessions_js)
    report("security_search_exact_hostname", "result_host.endswith(\".google.com\")" in web_search)
    report("security_workflow_read_only", "permissions:\n  contents: read" in accessibility_workflow)
    report("security_web_search_mcp_bundled", '"tools/web_search_mcp.py"' in standalone_package)
    report("security_release_tag_pattern", 'tags:\n      - "v*"' in open(".github/workflows/release.yml").read())


def test_sidebar_workflow_contract():
    """Sidebar workflows remain editable, collapsible, and profile-aware."""
    with open("index.html") as f:
        html = f.read()
    with open("core/js/features/sessions/explorer.js") as f:
        sessions_js = f.read()
    with open("core/js/features/skills/library.js") as f:
        skills_js = f.read()
    with open("core/js/profiles.js") as f:
        profiles_js = f.read()
    with open("core/js/cognition.js") as f:
        cognition_js = f.read()
    with open("core/js/options.js") as f:
        options_js = f.read()
    with open("core/js/features/voice/view.js") as f:
        voice_view_js = f.read()
    with open("tools/bridge/core.py") as f:
        bridge_core = f.read()
    with open("standalone/main.js") as f:
        standalone_main = f.read()
    with open("standalone/preload.js") as f:
        standalone_preload = f.read()

    report("workflow_signal_marker_only", "Do NOT call /v1/signal/send" in cognition_js and "Do NOT call /v1/signal/send" in bridge_core)
    report("workflow_signal_missing_marker_fails", "Eva did not provide a Signal message payload" in options_js)
    report("workflow_session_rename", "function renameSession" in sessions_js and "customTitle" in sessions_js)
    report("workflow_session_delegated_activation", "function activateSessionListItem" in sessions_js and "data-session-id" in sessions_js and "activationBound" in sessions_js)
    report("workflow_session_awaits_save", "return Promise.resolve(saveCurrentSession()).then(function()" in sessions_js)
    report("workflow_session_legacy_fallback", "localStorage.getItem('session_' + id)" in sessions_js and "idbSaveSession(id, data)" in sessions_js)
    report("workflow_session_legacy_visible_restore", "function _restoreLegacySessionOutput" in sessions_js and "Restorable transcript" not in sessions_js and "closeWorkbench" in sessions_js)
    report("workflow_session_reveals_chat", "EvaAgents.close" in sessions_js and "Session loaded." in sessions_js)
    report("workflow_session_recovery_copy", "_saveSessionRecoveryCopy" in sessions_js and "session_' + id" in sessions_js and "IDB load failed" in sessions_js and "localStorage.removeItem('session_' + entry.id)" not in open("core/js/idb-store.js").read())
    report("workflow_voice_conversation_record", "voiceMessages" in sessions_js and "function recordConversationTurn" in sessions_js and "function recordSpokenEvaText" in sessions_js and "recordConversationTurn(command, reply)" in open("core/js/features/voice/wake-listener.js").read() and "recordConversationTurn(protectedRawText, nativeResult.message)" in options_js)
    report("workflow_sidebar_session_provider", "function getAllSessions" in sessions_js and "updatedAt:" in sessions_js)
    report("workflow_new_session_on_launch", "startFreshSessionOnLaunch();\n\n  // Migrate saved sessions" in sessions_js and "function startFreshSessionOnLaunch()" in sessions_js)
    report("workflow_startup_matches_new_chat", "typeof restoreEvaWelcome === 'function'" in sessions_js and "opens a fresh chat on launch" in voice_view_js)
    report("workflow_side_panels_click_outside", "function closeSidePanels" in sessions_js and "EVA_SIDE_PANEL_IDS" in sessions_js and "document.addEventListener('click'" in sessions_js)
    report("workflow_workspace_navigation", "function closeAgentOperationsForNavigation" in sessions_js and "#evaAgentsBtn, #evaWorkspacesBtn" in sessions_js and 'id="evaAgentsBtn"' in html and 'id="lcarsWorkspacesBtn"' in html and "EvaAgents.open()" in html and "EvaWorkspaces.openWorkbench()" in html and "!target.closest('#lcarsWorkspacesBtn')" in sessions_js)
    report("workflow_skill_edit_patch", "function editSkill" in skills_js and "editingId ? 'PATCH' : 'POST'" in skills_js)
    report("workflow_voice_skill_url_authorization", "Only credential-free HTTPS URLs can be opened" in skills_js and "not authorized by the named active skill" in skills_js and "explicit user request" in skills_js and "window.open(target, '_blank', 'noopener')" in skills_js)
    report("workflow_skills_main_view", "_buildSkillsWorkspace" in skills_js and "skills-view-open" in skills_js and "window.EvaSkills" in skills_js and "body.skills-view-open" in open("core/style.css").read())
    report("workflow_skills_organization", all(value in skills_js for value in ("skillsSearch", "skillsStatusFilter", 'value=\"draft\"', "skillsSourceFilter", "skillsSort", "_filteredSkills", "_skillSourceKind", "skillsViewSummary")))
    report("workflow_skills_cross_navigation", "EvaSkills.close" in sessions_js and "EvaSkills.close" in open("core/js/features/agents/operations.js").read() and "EvaSkills.close" in open("core/js/features/assets/library.js").read() and "EvaSkills.close" in open("core/js/features/workspaces/monitor.js").read())
    report("workflow_profile_picker", 'id="profilePanel"' in html and re.search(r'src="core/js/profiles\.js(?:\?[^" ]+)?"', html) is not None and "function switchEvaProfile" in profiles_js)
    report("workflow_profile_awaits_session_save", "async function switchEvaProfile" in profiles_js and "await saveCurrentSession()" in profiles_js)
    report("workflow_profile_scoped_sessions", "saveCurrentSession" in profiles_js and "eva_sessions" not in profiles_js)
    report("workflow_encrypted_auth_persistence", "safeStorage.encryptString" in standalone_main and "safeStorage.decryptString" in standalone_main and "authLoad" in standalone_preload)
    report("workflow_auth_ipc_trusted_renderer", "isTrustedEvaRenderer" in standalone_main and "fileURLToPath(event.senderFrame.url)" in standalone_main)
    report("workflow_http_navigation_blocked", "event.preventDefault()" in standalone_main and "if (!url.startsWith('http://127.0.0.1')" in standalone_main)
    report("workflow_native_context_menu", "webContents.on('context-menu'" in standalone_main and "buildContextMenuTemplate" in standalone_main and os.path.isfile("standalone/context-menu.js") and os.path.isfile("tools/tests/test_context_menu.js"))
    report("workflow_skills_database_copy", "stores it in the database" in html and "stores it in ADX" not in html)
    audio_js = open("core/js/settings/audio.js").read()
    report("workflow_audio_settings_persist", "function initAudioPreferences" in audio_js and "tts_engine" in audio_js and "tts_auto_speak" in audio_js and "tts_voice" in audio_js and "function initAudioPreferences" not in options_js)
    dialogs_js = open("core/js/dialogs.js").read()
    voice_js = open("core/js/features/voice/wake-listener.js").read()
    harness_js = open("core/js/harness-control.js").read()
    prompts_js = open("core/js/settings/prompts.js").read()
    report("workflow_text_prompt_lazy_binding", "function _bindEvaTextPrompt" in dialogs_js and "_bindEvaTextPrompt();" in dialogs_js and "dialog.dataset.bound" in dialogs_js)
    report("workflow_text_prompt_voice_handoff", "function evaTextPromptConsumeVoice" in dialogs_js and "function evaTextPromptIsOpen" in dialogs_js and "function _evaGithubPromptCorrection" in dialogs_js and "github_repository_url" in dialogs_js and "Say the corrected repository name" in dialogs_js and "form.requestSubmit()" in dialogs_js and "dispatchEvent(new Event('input'" in dialogs_js and "evaTextPromptConsumeVoice(transcript)" in voice_view_js and "evaTextPromptConsumeVoice(transcript)" in voice_js)
    report("workflow_native_field_control", "function evaTextPromptDescribe" in dialogs_js and "function evaTextPromptSetField" in dialogs_js and "function evaTextPromptSubmit" in dialogs_js and "inspect_form" in harness_js and "set_field" in harness_js and "submit_form" in harness_js and "CURRENT NATIVE FORM" in harness_js)
    eva_theme = open("core/themes/eva.css").read()
    report("workflow_compact_voice_control", 'id="evaSidebarMicButton"' in html and 'id="evaSidebarVoiceStatus"' in html and 'id="evaSidebarVoiceViewButton"' in html and "eva-sidebar-voice" in eva_theme and "toggleCompactVoiceController()" in html and "function toggleCompactVoiceController" in voice_view_js)
    report("workflow_compact_voice_full_pipeline", "compactActive: false" in voice_view_js and "function _vvIsActive" in voice_view_js and "_vvStartListening();" in voice_view_js.split("function toggleCompactVoiceController", 1)[1].split("function toggleVoiceView", 1)[0] and "_vvIsActive() || !_vv.whisperMode" in voice_view_js and "_runVoiceNavigationCommand(command, compactVoiceTurnId)" in voice_view_js.split("function _vvSendCommand", 1)[1].split("function _vvWatchForResponse", 1)[0] and "var keepCompactController = _vv.compactActive;" in voice_view_js and "if (!keepCompactController) _vvStopListening();" in voice_view_js)
    report("workflow_compact_voice_shared_status", "evaSidebarMicButton" in voice_js and "evaSidebarVoiceStatus" in voice_js and "buttons.forEach" in voice_js)
    report("workflow_voice_native_navigation", "function _runVoiceNavigationCommand" in voice_js and "EvaHarness.resolveNavigationRequest(phrase, { directUser: true })" in voice_js and "EvaHarness.navigate(route.target)" in voice_js and "EvaHarness.resolveNavigationRequest(protectedRawText, { directUser: true })" in options_js and "EvaHarness.navigate(nativeRoute.target)" in options_js)
    report("workflow_voice_workspace_compound_navigation", "route.target === 'workspaces'" in voice_js and "read-only workspace commands" in voice_js)
    report("workflow_voice_graph_disabled", "VOICE_MEMORY_GRAPH_ENABLED = false" in voice_view_js and "if (VOICE_MEMORY_GRAPH_ENABLED) _vvStartMemoryGraph();" in voice_view_js and ".vv-memory-graph" in eva_theme and "display: none" in eva_theme)
    report("workflow_compact_voice_reduced_motion", "@media (prefers-reduced-motion: reduce)" in eva_theme and ".eva-sidebar-voice-shell-scan" in eva_theme and "animation: none;" in eva_theme)
    report("workflow_native_harness_api", "core/js/harness-control.js" in html and "var EvaHarness" in harness_js and "function execute" in harness_js and "function capabilities" in harness_js and "function resolveSurface" in harness_js and "function resolveNavigationRequest" in harness_js and "nativeOnly: true" in harness_js and all(target in harness_js for target in ("agent_operations", "voice_control", "models", "personality", "goals", "background_jobs", "schedules", "accounts", "tools_memory", "learning", "profile")))
    report("workflow_voice_skill_management", all(value in skills_js for value in ("createSkillFromRequest", "updateSkillByName", "setSkillStatusByName", "deleteSkillByName", "runSkillFromRequest", "openExternalUrlFromSkill", "eva_harness.open_external_url")) and all(value in harness_js for value in ("create_skill", "update_skill", "set_skill_status", "delete_skill", "run_skill", "open_external_url")))
    report("workflow_native_harness_voice_manifest", "voiceAwareActions: actions.slice()" in harness_js and "complete native action manifest is available when interpreting typed and voice requests" in harness_js and "awareness never bypasses an action gate" in harness_js)
    report("workflow_native_harness_marker", "EVA_HARNESS" in options_js and "EvaHarness.execute" in options_js and "browserLaunch = null;" in options_js and "desktopLaunch = null;" in options_js and "EvaHarness.promptContract" in prompts_js and options_js.find("var desktopLaunch = null;") < options_js.find("if (harnessActions.length) {\n    browserLaunch = null;"))
    report("workflow_native_github_import", "import_github" in harness_js and "repository_url" in harness_js and "EvaWorkspaces.importGitHub" in harness_js and "nativeRoute.action && nativeRoute.action !== 'navigate'" in options_js and "route.action && route.action !== 'navigate'" in voice_js)
    report("workflow_native_terminal_command", "run_terminal_command" in harness_js and "runEvaTerminalCommand" in harness_js and "function runEvaTerminalCommand" in open("core/js/features/sessions/explorer.js").read())
    report("workflow_native_workspace_description", "describe_workspaces" in harness_js and "EvaWorkspaces.describe" in harness_js and "Promise.resolve(EvaWorkspaces.describe())" in harness_js and "await Promise.resolve" in options_js and "Promise.resolve(pendingResult)" in voice_js and "evaTextPromptCancel()" in voice_js)


def test_workspace_terminal_contract():
    """The experimental terminal stays confined to Electron's allowlisted broker."""
    with open("standalone/main.js") as f:
        standalone_main = f.read()
    with open("standalone/preload.js") as f:
        standalone_preload = f.read()
    with open("standalone/terminal-broker.js") as f:
        broker = f.read()
    with open("core/js/features/sessions/explorer.js") as f:
        sessions_js = f.read()
    with open("standalone/package.json") as f:
        package = json.load(f)

    dependencies = package.get("dependencies", {})
    packaged_files = package.get("build", {}).get("files", [])
    report("workspace_terminal_feature_flagged", "EVA_WORKSPACE_TERMINAL_V1" in standalone_main and "--eva-workspace-terminal-v1" in standalone_main and standalone_main.count("requireWorkspaceFeature(event);") >= 7 and "if (workspaceTerminalEnabled())" in standalone_main)
    installer = open("install.sh").read()
    report("workspace_terminal_system_launcher_flagged", '${BASH_SOURCE[0]}' in installer and "refresh_system_launcher()" in installer and "--eva-workspace-terminal-v1" in installer and "System launcher refreshed" in installer and "refresh_system_launcher \"$appimage\"" in installer)
    report("workspace_terminal_trusted_renderer", "requireTerminalBroker(event)" in standalone_main and "isTrustedEvaRenderer(event)" in standalone_main)
    report("workspace_terminal_no_preload_process_access", "child_process" not in standalone_preload and "terminalCreate" in standalone_preload and "onTerminalData" in standalone_preload)
    report("workspace_terminal_opaque_root", "registerRoot('app-root', getAppRoot(), { allowSymlinks: true })" in standalone_main and "CREATE_FIELDS = new Set(['rootId', 'cols', 'rows'])" in broker and "_assertNoSymlinkComponents" in broker)
    report("workspace_terminal_secret_redaction", "EVA_BRIDGE_TOKEN" in broker and "EVA_LOCAL_SPEECH_TOKEN" in broker)
    report("workspace_terminal_bounded_replay", "maxScrollbackBytes" in broker and "trimUtf8Tail" in broker and "sequence" in broker)
    report("workspace_terminal_renderer", "_buildWorkspaceTerminal" in sessions_js and "terminalReplay" in sessions_js and "ResizeObserver" in sessions_js)
    report("workspace_terminal_dependencies", all(name in dependencies for name in ("node-pty", "@xterm/xterm", "@xterm/addon-fit", "@xterm/addon-search", "@xterm/addon-web-links")))
    report("workspace_terminal_packaged", all(name in packaged_files for name in ("context-menu.js", "terminal-broker.js")) and "node_modules/node-pty/**" in package.get("build", {}).get("asarUnpack", []))


def test_coding_workspace_contract():
    """Durable coding workspaces keep Git paths and execution ownership outside the renderer."""
    with open("tools/bridge/workspaces.py") as f:
        workspaces = f.read()
    with open("tools/bridge/core.py") as f:
        bridge_core = f.read()
    with open("standalone/main.js") as f:
        standalone_main = f.read()
    with open("standalone/preload.js") as f:
        standalone_preload = f.read()
    with open("core/js/features/workspaces/monitor.js") as f:
        workspace_ui = f.read()
    with open("core/js/options.js") as f:
        options_js = f.read()
    permission_ui = open("core/js/features/permissions/acp.js").read()
    with open("core/js/features/assets/library.js") as f:
        assets_ui = f.read()
    with open("core/js/features/sessions/explorer.js") as f:
        sessions_js = f.read()
    with open("core/style.css") as f:
        style_css = f.read()
    with open("index.html") as f:
        html = f.read()
    with open("standalone/package.json") as f:
        package = json.load(f)

    renderer_project = standalone_main.split("function workspaceProjectForRenderer", 1)[1].split("function workspaceRunForRenderer", 1)[0]
    renderer_run = standalone_main.split("function workspaceRunForRenderer", 1)[1].split("async function workspaceListProjects", 1)[0]
    list_projects_handler = standalone_main.split("async function workspaceListProjects", 1)[1].split("async function workspaceSelectProject", 1)[0]
    list_runs_handler = standalone_main.split("async function workspaceListRuns", 1)[1].split("async function workspaceRunAction", 1)[0]
    report("coding_workspace_sqlite_schema", all(value in workspaces for value in ("CREATE TABLE projects", "CREATE TABLE checkouts", "CREATE TABLE coding_runs", "CREATE TABLE agent_runs", "CREATE TABLE approvals")))
    report("coding_workspace_git_arrays", "[\"git\", \"-C\", normalized_cwd, *arguments]" in workspaces and "cwd=" not in workspaces and "shell=" not in workspaces and "reference.startswith(\"-\")" in workspaces)
    report("coding_workspace_canonical_paths", "resolve(strict=True)" in workspaces and "_is_within(checkout_path, self.runtime_root)" in workspaces)
    report("coding_workspace_dirty_confirmation", "Confirm dirty cleanup" in workspaces and "confirm_dirty" in workspaces)
    report("coding_workspace_missing_worktree_recovery", '"worktree", "prune"' in workspaces and "_worktree_registered" in workspaces)
    report("coding_workspace_bridge_routes", all(value in bridge_core for value in ("/v1/workspaces/projects", "/v1/workspaces/eva-ready", "/v1/workspaces/runs", "/v1/workspaces/assets", "/v1/workspaces/github-import", "_workspace_checkout_status")))
    report("coding_workspace_eva_ready_bootstrap", "ensure_eva_ready_project" in workspaces and "Eva Ready Workspace" in workspaces and "ensureEvaReadyWorkspace" in standalone_main)
    report("coding_workspace_agent_autodispatch", "_dispatch_workspace_run" in bridge_core and "create_agent_run" in workspaces and "dispatchPendingWorkspaceRuns" in standalone_main and "EVA_WORKSPACE_AGENT_AUTODISPATCH" in bridge_core)
    workspace_utils = open("tools/bridge/utils.py").read()
    acp_client = open("tools/bridge/acp_client.py").read()
    report("coding_workspace_github_delivery", "requires_github_delivery" in bridge_core and "required_github_delivery_kind" in bridge_core and "required_github_issue_state" in bridge_core and "This is a close-only request" in bridge_core and "authenticated `gh` CLI" in bridge_core and "if none exists, create a new issue" in bridge_core and "requires a real GitHub pull request" in bridge_core and "Do not stop after creating the branch" in bridge_core and "_workspace_github_delivery_url" in workspace_utils and "required_delivery_kind" in workspace_utils and "pull_match" in workspace_utils and 'actual_state != expected_state' in workspace_utils and "submission was required but not verified" in workspace_utils)
    report("coding_workspace_agent_cwd", 'task.get("_cwd")' in workspace_utils and 'cwd=assigned_cwd' in workspace_utils and '"coding_run_id": task.get("coding_run_id"' in bridge_core and 'def _scope_subagent_task_to_workspace' in bridge_core and 'ensure_eva_ready_project()' in bridge_core and '"workspace_scoped": True' in bridge_core and '"capability_policy": "workspace_auto"' in bridge_core)
    report("coding_workspace_agent_auto_permission", 'permission_mode == "workspace_auto"' in acp_client and 'def _workspace_autonomy_block_reason' in acp_client and '_WORKSPACE_AUTONOMY_BLOCKED_EXECUTABLES' in acp_client and 'decision="workspace-autonomy-approve"' in acp_client and 'decision="workspace-autonomy-reject-" + block_reason' in acp_client and 'def _workspace_edit_target_is_local' in acp_client and 'def _workspace_edit_target_is_protected' in acp_client and 'def _command_summary' in acp_client and '"workspace_auto"' in workspace_utils and 'permission_mode=permission_mode' in workspace_utils and 'auto_approve' in workspaces)
    report("direct_acp_autonomy", 'acp_auto_approve: true' in open("core/js/providers/aig.js").read() and 'payload.acp_auto_approve = true' in open("core/js/providers/copilot.js").read() and 'acp_auto_approve' not in open("core/js/cognition.js").read())
    report("coding_workspace_agent_durable_completion", "status = 'completed'" in workspaces and "agent_completed" in workspaces and "agent_cancelled" in workspaces and '"agent": self._agent_payload' in workspaces)
    report("coding_workspace_agent_no_private_path", "_public_subagent_task" in bridge_core and 'not key.startswith("_")' in bridge_core)
    report("coding_workspace_bridge_capability", "_require_workspace_capability" in bridge_core and "EVA_WORKSPACE_CAPABILITY" in bridge_core and "workspaceCapabilityToken" in standalone_main and "workspaceCapabilityToken" not in standalone_preload)
    report("coding_workspace_imports", "dialog.showOpenDialog" in standalone_main and "workspace-select-project" in standalone_main and "workspace-import-github" in standalone_main and "import_github_repository" in workspaces and "parsed = urlparse(value)" in workspaces and 'parsed.scheme != "https"' in workspaces and 'parsed.hostname.lower() != "github.com"' in workspaces and 'parsed.netloc.lower() != "github.com"' in workspaces and "parsed.username is not None" in workspaces and "parsed.query" in workspaces and "parsed.fragment" in workspaces and "len(parts) != 2" in workspaces and "_GITHUB_REPOSITORY_PART_RE.fullmatch(owner)" in workspaces)
    report("coding_workspace_renderer_opaque", "path:" not in renderer_project and "path:" not in renderer_run and "workspaceCheckoutForRenderer" in standalone_main and "redactKnownPaths" in renderer_run)
    broker = open("standalone/terminal-broker.js").read()
    report("coding_workspace_ptys_close_before_discard", "await terminalBroker.terminateByRoot(checkoutId)" in standalone_main and "terminalBroker.unregisterRoot(checkoutId)" in standalone_main and "terminalCloseRoot" in workspace_ui and "terminateByRoot(rootId)" in broker and "scope: this._terminationScope(child.pid)" in broker and "this.signalProcess(-session.child.pid, signal)" in broker)
    report("coding_workspace_preload_allowlist", all(value in standalone_preload for value in ("terminalCloseRoot", "workspaceListProjects", "workspaceSelectProject", "workspaceImportGitHub", "workspaceGitHubAuthStart", "workspaceGitHubAuthStatus", "githubViewPullRequest", "githubMergePullRequest", "githubDeletePullRequestBranch", "workspaceRemediationContextLoad", "workspaceRemediationContextSave", "workspaceSetMcpServer", "workspaceCreateRun", "workspaceDispatchRun", "workspaceCheckoutStatus", "workspaceRunAction")))
    report("coding_workspace_github_pr_repository_resolution", "resolveGitHubPullRequestRepository" in standalone_main and "api', 'graphql'" in standalone_main and "nameWithOwner" in standalone_main and "GitHub CLI response exceeded the safe output limit" in standalone_main and "Provide the full GitHub pull request URL" in standalone_main)
    report("coding_workspace_github_branch_delete", "githubDeletePullRequestBranch" in standalone_main and "pull.state !== 'MERGED'" in standalone_main and "'.default_branch'" in standalone_main and "branch === defaultBranch" in standalone_main and "Eva will not delete a default or base branch" in standalone_main and "git/refs/heads/" in standalone_main and "github-delete-pull-request-branch" in standalone_main)
    remediation_harness = open("core/js/harness-control.js").read()
    report("coding_workspace_github_branch_delete_provenance", "direct_native_inspection" in remediation_harness and "aigMessages" not in remediation_harness[remediation_harness.index("function recentPullRequestContext"):remediation_harness.index("function resolveNavigationRequest")])
    report("coding_workspace_remediation_context", "normalizedRemediationContext" in standalone_main and "remediation-context.json" in standalone_main and "workspace-remediation-context-load" in standalone_main and "workspace-remediation-context-save" in standalone_main and "nativeRemediationContext" in remediation_harness and "persistRemediationContext(explicitRemediation)" in remediation_harness and ".chat-bubble.user-bubble" in remediation_harness)
    removal_harness = open("core/js/harness-control.js").read()
    report("coding_workspace_safe_removal", "def delete_project" in workspaces and "source_preserved" in workspaces and "workspace agent is still active" in workspaces.lower() and "workspace-delete-project" in standalone_main and "workspaceDeleteProject" in standalone_preload and "Remove workspace" in workspace_ui and "remove_workspace" in removal_harness and "modelWorkspaceRemoval" in removal_harness and "workspaceRemovalVerb" in removal_harness)
    report("coding_workspace_completed_actions", "(run.status === 'active' || run.status === 'completed') && !agentActive" in workspace_ui and "['active', 'completed'].indexOf(actionRun.status)" in workspace_ui)
    report("coding_workspace_current_monitor_snapshot", workspace_ui.find("state.runs = runs;") < workspace_ui.find("narrateRunChanges(state.runs);") and "workspaceCheckoutStatus(selected.checkout.id)" in workspace_ui)
    report("coding_workspace_ui_wired", "core/js/features/workspaces/monitor.js" in html and "workspacePanel" in html and "workspaceWorkbench" in html and "openWorkspaceTerminal" in workspace_ui and "_evaWorkspaceTerminalTarget" in sessions_js and "body.eva-standalone .workspace-panel" in style_css)
    report("coding_workspace_monitor_observation_only", "setInterval(monitor, 10000)" in workspace_ui and "api().terminalList()" in workspace_ui and "terminalCreate" not in workspace_ui.split("async function monitor()", 1)[1].split("function openWorkbench", 1)[0] and "registerWorkspaceRoot" not in list_projects_handler and "registerWorkspaceRoot" not in list_runs_handler and "ensureTerminalRoot(rootId)" in standalone_main)
    report("coding_workspace_monitor_text_voice_updates", "addMonitorActivity" in workspace_ui and "forceVoice" in workspace_ui and "autoSpeak.checked" in workspace_ui and "speakText(message)" in workspace_ui and "lastPeriodicNoteAt" in workspace_ui)
    harness_js = open("core/js/harness-control.js").read()
    report("coding_workspace_native_project_checks", "run_workspace_check" in harness_js and "retry_workspace_run" in harness_js and "set_workspace_mcp_server" in harness_js and "smoke\\s*tests?" in harness_js and "runSelectedCheck" in workspace_ui and "retryRun: retryRunById" in workspace_ui and "injectWorkspaceStatusBubble" in options_js)
    report("coding_workspace_runner_recovery", "workspaceDispatchRun" in workspace_ui and "workspace-dispatch-run" in standalone_main and "/dispatch" in standalone_main and 'stage="redispatch"' in bridge_core and "!['starting', 'running', 'steering'].includes(agentStatus)" in standalone_main and "Retry this failed workspace run" in workspace_ui and "Retry run" in workspace_ui)
    report("coding_workspace_failure_categories", all(value in workspace_ui for value in ("user_cancelled", "agent_cancelled", "permission_denied", "runner_unavailable", "test_failure", "bridge_failure")))
    report("coding_workspace_fast_terminal_narration", "!prior && current.status" in workspace_ui and "narrateTerminalRun(run, current)" in workspace_ui and "narrateFailedRun(run)" in workspace_ui)
    report("coding_workspace_scoped_activity_results", "projectId: run && run.projectId" in workspace_ui and "entry.projectId === state.selectedProjectId" in workspace_ui and "workspaceWorkbenchResults" in workspace_ui and "RUN RESULTS" in html and "workspace-monitor-results" in style_css)
    report("coding_workspace_live_chat_drawer", all(value in html for value in ("workspaceChatToggleBtn", "workspaceChatDrawer", "workspaceChatOutputHost", "workspaceChatInputHost", "workspaceChatSessionSelect")) and "setChatDrawerOpen" in workspace_ui and "captureChatNodeOrigins" in workspace_ui and "restoreChatNodes" in workspace_ui and "refreshChatSessionSelect" in workspace_ui and "preserveWorkspace: true" in workspace_ui and "hideChatDrawerOnOutsidePointer" in workspace_ui and "workspace-chat-drawer-open" in style_css and ".workspace-chat-drawer" in style_css)
    report("standalone_workspace_real_estate", "width: 1728" in standalone_main and "height: 1215" in standalone_main and "minWidth: 1280" in standalone_main and "minHeight: 900" in standalone_main)
    report("coding_workspace_main_navigation", "openWorkbench" in workspace_ui and "workspace-workbench-open" in style_css and "closeWorkbench" in sessions_js and "closeWorkbench" in open("core/js/features/agents/operations.js").read())
    report("coding_workspace_mcp_isolation", "project_mcp_preferences" in workspaces and "approved_digest" in workspaces and "_mcp_config_digest" in workspaces and "_MCP_RESERVED_ENV_KEYS" in workspaces and "BASH_ENV" in workspaces and "key.startswith(\"LD_\")" in workspaces and "mcp_config_for_run" in workspaces and "_workspace_mcp_config" in bridge_core and "workspace_mcp_prefix" in bridge_core and "_subagent_mcp_config(template, task)" in workspace_utils and "return copy.deepcopy(workspace_config)" in workspace_utils and "workspaceSetMcpServer" in workspace_ui and "Any configuration change will revoke this approval" in workspace_ui and "envKeys" in renderer_project and "headerKeys" in renderer_project and "env:" not in renderer_project and "headers:" not in renderer_project)
    report("coding_workspace_github_prompt_visible", ":not(#workspaceWorkbench):not(#textToSynth)" in style_css and "#textToSynth > :not(#evaTextPrompt)" in style_css and "workspaceImportGitHub" in workspace_ui and "evaTextPrompt('GitHub repository URL'" in workspace_ui)
    report("coding_workspace_github_native_api", "importGitHub: importGitHubProject" in workspace_ui and "startRepositoryRemediation: startRepositoryRemediation" in workspace_ui and "authorizeGitHub: authorizeGitHub" in workspace_ui and "setProjectMcpServerByName" in workspace_ui and "per_page=100" in standalone_main and "authGitHubCliBtn" in html and "workspaceCollapseGitHubBtn" in html and "workspaceGitHubAuthStart" in standalone_main and "'auth', 'refresh'" in standalone_main and "'auth', 'login'" in standalone_main and "workspaceGitHubImportErrorMessage" in standalone_main and "return { error: workspaceGitHubImportErrorMessage(error) }" in standalone_main and "importResult && importResult.error" in workspace_ui)
    report("coding_workspace_mcp_context_on_demand", "function mcpContext" in workspace_ui and "WORKSPACE MCP MODULE SNAPSHOT" in workspace_ui and "workspaceMcpRequest" in open("core/js/providers/aig.js").read() and "EvaWorkspaces.mcpContext" in open("core/js/providers/aig.js").read() and "safe metadata only" in workspace_ui)
    report("coding_workspace_github_retry_prompt", "while (repositoryUrl)" in workspace_ui and "Correct GitHub repository URL" in workspace_ui and "The URL is back in the prompt so you can correct it." in workspace_ui and "GitHub workspace imported." in workspace_ui)
    report("coding_workspace_native_description", "async function describeCurrent" in workspace_ui and "Promise.all([api().workspaceListProjects(), api().workspaceListRuns()])" in workspace_ui and "describe: describeCurrent" in workspace_ui)
    report("coding_workspace_project_navigation", "list_project_files" in workspaces and "resolve_project_file" in workspaces and "workspaceListProjectFiles" in standalone_preload and "workspaceOpenProjectFile" in standalone_preload and "workspaceProjectFiles" in workspace_ui and "Open project terminal" in workspace_ui and "['source', 'worktree'].includes(checkout.kind)" in standalone_main)
    report("coding_workspace_draft_stable", "var shouldRender = changed || permissionsChanged;" in workspace_ui and "state.workbenchOpen && shouldRender" in workspace_ui and "runDrafts" in workspace_ui and "draft.objective = objective.value" in workspace_ui and "draft.baseRef = baseRef.value" in workspace_ui)
    report("coding_workspace_permission_rerender", "permissionSignature" in workspace_ui and "permissionsChanged" in workspace_ui and "changed || permissionsChanged" in workspace_ui)
    cancellation_worker = workspace_utils.split('prompt_result = client.prompt(', 1)[1].split('while True:', 1)[0]
    report("coding_workspace_structured_cancellation", '"permission_cancelled": False' in acp_client and '"permission_reason": ""' in acp_client and 'state["permission_cancelled"] = True' in acp_client and 'state["permission_reason"] = "user_rejected"' in acp_client and 'state["permission_reason"] = "permission_timeout"' in acp_client and 'decision="invalid-decision"' in acp_client and '"error": result["error"]' in acp_client and 'prompt_result.get("permission_cancelled")' in workspace_utils and '_workspace_permission_cancelled' not in workspace_utils and cancellation_worker.find('permission_cancelled =') < cancellation_worker.find('result_text = _subagent_result_text'))
    report("coding_workspace_informed_approval", '"command_summary"' in acp_client and '"approval_allowed"' in acp_client and "commandSummary" in workspace_ui and "approvalAllowed" in workspace_ui and "command_summary" in permission_ui and "approval_allowed" in permission_ui)
    report("coding_workspace_project_tree", "buildProjectFileTree" in workspace_ui and "renderProjectTreeNode" in workspace_ui and "projectTreeExpanded" in workspace_ui and "workspace-tree-folder" in style_css and "workspace-tree-chevron" in style_css)
    report("coding_workspace_execution_approval", "workspace_acp_clients" in bridge_core and "workspace_run_id" in bridge_core and "pendingPermissions" in workspace_ui and "EXECUTION APPROVAL" in workspace_ui and "resolveWorkspacePermission" in workspace_ui)
    report("coding_workspace_monitor_responsive", "workspace-workbench-body" in style_css and "@media (max-width: 760px)" in style_css and "resize: horizontal" in style_css and "terminal-panel-expanded" in style_css and "terminal-panel-docked" in style_css and "text-align: left !important" in style_css and "toggleTerminalWidth" in sessions_js)
    report("coding_workspace_assets_index", "list_workspace_assets" in workspaces and "resolve_workspace_asset" in workspaces and "_resolve_checkout_file" in workspaces and "_validated_managed_checkout" in workspaces and "../README.md" in open("tools/tests/test_workspaces_e2e.py").read())
    report("coding_workspace_assets_main_view", "assetsView" in html and "assets-view-open" in style_css and "workspaceListAssets" in assets_ui and "workspaceOpenAsset" in assets_ui)
    report("coding_workspace_assets_path_private", "workspace-list-assets" in standalone_main and "workspace-open-asset" in standalone_main and "workspaceListAssets" in standalone_preload and "workspaceOpenAsset" in standalone_preload and "path:" not in assets_ui)
    report("coding_workspace_report_path_redaction", "redactKnownPaths" in standalone_main and "workspace-projection.js" in json.dumps(package.get("build", {}).get("files", [])) and os.path.isfile("tools/tests/test_workspace_projection.js"))


def test_pages_comparison_contract():
    """The Pages comparison includes the official GitHub Copilot app."""
    with open("docs/index.html") as f:
        docs_html = f.read()
    report("pages_github_copilot_app_column", "GitHub Copilot app</a>" in docs_html and "https://github.com/features/ai/github-app?locale=en-US" in docs_html)
    report("pages_github_copilot_app_capabilities", all(value in docs_html for value in ("Custom skills", "Automations", "Session history", "Multi-model + BYOK")))
    report("pages_eva_differentiators", all(value in docs_html for value in ("LM Studio + direct MCP", "Local SQLite or Kusto/ADX", "Local SQLite or ADX", "Separate sessions + settings", "Signal delivery", "Automatic English/Korean + imported profiles", "PDF/MD/CSV/JSON/TXT + Assets", "Subsystem doctor", "Configurable Eva + Reviewer")))
    report("pages_comparison_scrolls", ".compare-wrap {\n      border-radius: var(--radius); overflow-x: auto;" in docs_html and "min-width: 960px" in docs_html)


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
                       "BackgroundProposals", "BackgroundActivity"]
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
    with open("core/js/settings/goals.js") as f:
        goals_js = f.read()

    report("goals_seed_table", ".create-merge table Goals" in seed,
           "missing Goals table" if ".create-merge table Goals" not in seed else "")
    allowed_match = re.search(r"allowed_tables\s*=\s*\{[^}]*\"Goals\"", mcp, re.DOTALL)
    report("goals_allowed_tables", allowed_match is not None,
           "Goals missing from allowed_tables" if allowed_match is None else "")
    report("goals_active_tool_method", "def _tool_eva_get_active_goals" in mcp,
           "missing _tool_eva_get_active_goals" if "def _tool_eva_get_active_goals" not in mcp else "")
    report("goals_settings_lifecycle", all(marker in goals_js for marker in ("function readGoalForm", "async function loadGoals", "async function saveGoalFromSettings", "async function deleteGoal", "function initGoals")))


def test_goals_settings_contract():
    """Goals form validation remains executable without a live bridge."""
    node = shutil.which("node")
    if not node:
        report("goals_settings_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_goals_settings.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("goals_settings_contract", result.returncode == 0, detail[:300])


def test_runtime_settings_contract():
    """Data-mode and diagnostics controls retain their local bridge contract."""
    node = shutil.which("node")
    if not node:
        report("runtime_settings_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_runtime_settings.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("runtime_settings_contract", result.returncode == 0, detail[:300])


def test_cron_settings_contract():
    """Cron Settings CRUD requests and validation remain executable without a bridge."""
    node = shutil.which("node")
    if not node:
        report("cron_settings_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_cron_settings.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("cron_settings_contract", result.returncode == 0, detail[:300])


def test_prompts_settings_contract():
    """System prompt presets retain storage, migration, and harness behavior."""
    node = shutil.which("node")
    if not node:
        report("prompts_settings_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_prompts_settings.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("prompts_settings_contract", result.returncode == 0, detail[:300])


def test_audio_settings_contract():
    """Audio device and voice preference behavior remains executable without browser hardware."""
    node = shutil.which("node")
    if not node:
        report("audio_settings_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_audio_settings.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("audio_settings_contract", result.returncode == 0, detail[:300])


def test_skill_auto_learn_contract():
    """Auto-learned Skills retain their bounded bridge request and failure behavior."""
    node = shutil.which("node")
    if not node:
        report("skill_auto_learn_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_skill_auto_learn.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("skill_auto_learn_contract", result.returncode == 0, detail[:300])


def test_frontend_script_order_contract():
    """Feature extraction preserves ordered classic-script availability."""
    node = shutil.which("node")
    if not node:
        report("frontend_script_order_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_frontend_script_order.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("frontend_script_order_contract", result.returncode == 0, detail[:300])


def test_bridge_client_contract():
    """Shared bridge transport preserves structured success and error semantics."""
    node = shutil.which("node")
    if not node:
        report("bridge_client_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_bridge_client.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("bridge_client_contract", result.returncode == 0, detail[:300])


def test_background_settings_ui_contract():
    """Background control payloads and explicit proposal approval remain stable."""
    node = shutil.which("node")
    if not node:
        report("background_settings_ui_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_background_settings.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("background_settings_ui_contract", result.returncode == 0, detail[:300])


def test_alerts_settings_ui_contract():
    """Alert form, CRUD, delete confirmation, and delivery limits remain stable."""
    node = shutil.which("node")
    if not node:
        report("alerts_settings_ui_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_alerts_settings.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("alerts_settings_ui_contract", result.returncode == 0, detail[:300])


def test_proactive_notifications_contract():
    """Notification polling, voice batching, and seen acknowledgment remain stable."""
    node = shutil.which("node")
    if not node:
        report("proactive_notifications_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_proactive_notifications.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("proactive_notifications_contract", result.returncode == 0, detail[:300])


def test_acp_permissions_ui_contract():
    """ACP permission polling and one-time decisions retain capability and policy checks."""
    node = shutil.which("node")
    if not node:
        report("acp_permissions_ui_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_acp_permissions.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("acp_permissions_ui_contract", result.returncode == 0, detail[:300])


def test_browser_agent_api_contract():
    """Moved browser/desktop controller retains its public API and endpoint contract."""
    node = shutil.which("node")
    if not node:
        report("browser_agent_api_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_browser_agent_api.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("browser_agent_api_contract", result.returncode == 0, detail[:300])


def test_camera_api_contract():
    """Moved Camera controller retains its public API and endpoint contract."""
    node = shutil.which("node")
    if not node:
        report("camera_api_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_camera_api.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("camera_api_contract", result.returncode == 0, detail[:300])


def test_assets_api_contract():
    """Moved Assets library retains its public API and backend integrations."""
    node = shutil.which("node")
    if not node:
        report("assets_api_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_assets_api.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("assets_api_contract", result.returncode == 0, detail[:300])


def test_agents_api_contract():
    """Moved Agent Operations retains its public API and bridge endpoint contract."""
    node = shutil.which("node")
    if not node:
        report("agents_api_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_agents_api.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("agents_api_contract", result.returncode == 0, detail[:300])


def test_skills_api_contract():
    """Moved Skills library retains its public API and CRUD endpoint contract."""
    node = shutil.which("node")
    if not node:
        report("skills_api_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_skills_api.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("skills_api_contract", result.returncode == 0, detail[:300])


def test_workspaces_api_contract():
    """Moved Workspace Monitor retains its public and legacy navigation API."""
    node = shutil.which("node")
    if not node:
        report("workspaces_api_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_workspaces_api.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("workspaces_api_contract", result.returncode == 0, detail[:300])


def test_sessions_api_contract():
    """Moved Sessions Explorer retains global session and terminal entry points."""
    node = shutil.which("node")
    if not node:
        report("sessions_api_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_sessions_api.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("sessions_api_contract", result.returncode == 0, detail[:300])


def test_voice_module_contracts():
    """Moved Voice modules retain listener, endpoint, and Voice View contracts."""
    node = shutil.which("node")
    if not node:
        report("voice_listener_api_contract", None, "node is unavailable")
        report("voice_endpoint_contract", None, "node is unavailable")
        return
    for name, path in (
        ("voice_listener_api_contract", "tools/tests/test_voice_listener_api.js"),
        ("voice_endpoint_contract", "tools/tests/test_voice_endpoint.js"),
        ("voice_view_api_contract", "tools/tests/test_voice_view_api.js"),
        ("voice_interruption_contract", "tools/tests/test_voice_interruption.js"),
    ):
        result = subprocess.run([node, path], capture_output=True, text=True, check=False)
        detail = (result.stderr or result.stdout).strip()
        report(name, result.returncode == 0, detail[:300])


def test_provider_paths_contract():
    """Moved provider adapters retain load order and sender entry points."""
    node = shutil.which("node")
    if not node:
        report("provider_paths_contract", None, "node is unavailable")
        return
    result = subprocess.run(
        [node, "tools/tests/test_provider_paths.js"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("provider_paths_contract", result.returncode == 0, detail[:300])


def test_aig_request_contract():
    """AIG request validation and derived routing flags remain unit-testable."""
    result = subprocess.run(
        [sys.executable, "tools/tests/test_aig_request.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("aig_request_contract", result.returncode == 0, detail[:300])


def test_aig_preflight_contract():
    """AIG ACP preflight planning remains deterministic and I/O-free."""
    result = subprocess.run(
        [sys.executable, "tools/tests/test_aig_preflight.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("aig_preflight_contract", result.returncode == 0, detail[:300])


def test_http_routes_contract():
    """Fixed PATCH route table matches known handlers without changing auth ownership."""
    result = subprocess.run(
        [sys.executable, "tools/tests/test_http_routes.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (result.stderr or result.stdout).strip()
    report("http_routes_contract", result.returncode == 0, detail[:300])


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


def test_agent_operations_contract():
    """Agent dashboard routes, UI entry points, and steering are wired."""
    bridge_path = "tools/bridge/core.py"
    worker_path = "tools/bridge/utils.py"
    ui_path = "core/js/features/agents/operations.js"
    with open(bridge_path) as f:
        bridge = f.read()
    with open(worker_path) as f:
        worker = f.read()
    with open(ui_path) as f:
        ui = f.read()
    with open("core/js/cognition.js") as f:
        cognition = f.read()
    with open("core/js/profiles.js") as f:
        profiles = f.read()
    with open("core/js/features/sessions/explorer.js") as f:
        sessions = f.read()
    with open("core/js/options.js") as f:
        options = f.read()
    permission_ui = open("core/js/features/permissions/acp.js").read()
    with open("core/js/features/sessions/explorer.js") as f:
        session_ui = f.read()
    with open("index.html") as f:
        html = f.read()

    for endpoint in ("/v1/agents/overview", "/v1/subagent/steer"):
        report(f"agent_operations_endpoint:{endpoint}", endpoint in bridge,
               f"missing {endpoint}" if endpoint not in bridge else "")
    for element_id in ("agentsMobileBtn", "agentsView", "agentsGrid", "agentGraphCanvas"):
        report(f"agent_operations_element:{element_id}", f'id="{element_id}"' in html,
               f"missing #{element_id}" if f'id="{element_id}"' not in html else "")
    report("agent_operations_script", re.search(r'src="core/js/features/agents/operations\.js(?:\?[^" ]+)?"', html) is not None)
    report("agent_operations_adaptive_polling",
           "AGENT_ACTIVE_POLL_MS = 2000" in ui and "AGENT_IDLE_POLL_MS = 20000" in ui
            and "function scheduleNextRefresh" in ui and "return Promise.resolve(refresh()).finally(scheduleNextRefresh);" in ui and "setInterval(refresh, 2000)" not in ui
            and "setInterval(function() { if (!state.open) refresh(); }, 15000);" not in ui,
           "Agent Operations must use active/idle timeout polling")
    report("acp_permission_adaptive_polling",
            "idleIntervalMs: 300000" in permission_ui and "requestIntervalMs: 30000" in permission_ui
            and "pendingIntervalMs: 3000" in permission_ui and "_acpPermissionState.pending" in permission_ui
           and "function watchACPPermissions" in permission_ui and "setInterval(pollACPPermissions" not in permission_ui,
            "ACP permissions must use request, pending, and idle timeout polling")
    acp_client_source = open("tools/bridge/acp_client.py").read()
    report("verbose_diagnostics_safe_toggle", 'id="verboseDiagnostics"' in html and "verbose_debug" in options and "_verbose_debug_emit" in bridge and "enabled_module_count" in bridge and '"workspace_run", stage=' in bridge and "dispatch_state=" in bridge and re.search(r'_verbose_debug_emit\(\s*"permission_request"', acp_client_source) is not None and "user_message" not in open("tools/bridge/telemetry.py").read().split("def _verbose_debug_emit", 1)[1].split("def _percentile", 1)[0])
    report("bridge_debug_content_redaction", "Processing: {user_message" not in bridge and "LM Studio content:" not in bridge and "query ({_request_type}): {user_message" not in bridge and "Local mode query: {user_message" not in bridge and "Agent plan: {', '.join" not in acp_client_source)
    report("session_active_navigation",
           'id="sessionActiveTab"' in html and 'id="activeSessionList"' in html
            and 'aria-controls="sessionActiveView"' in html and 'aria-labelledby="sessionActiveTab"' in html
            and "function refreshActiveSessionList" in session_ui and "EvaAgents.openAgent" in session_ui
            and "event.key === 'ArrowRight'" in session_ui,
           "Sessions must expose active agents and navigate to their detail view")
    report("session_titles_preserved",
            "SESSION_TITLE_MAX_LENGTH = 140" in session_ui and re.search(r"\.session-title\s*\{[^}]*white-space:\s*normal", open("core/style.css").read(), re.S) is not None,
           "Session titles must retain and render longer names")
    report("session_active_capacity_summary",
            "active agents; " in session_ui and "subagent slots" in session_ui and "data.subagents_active" in session_ui,
            "Active-agent counts must remain distinct from subagent capacity")
    report("agent_operations_detail_updates_in_place",
            "function updateDetail(agent, content)" in ui and "content.dataset.agentId === agent.id" in ui
            and "if (state.selectedId) renderDetail(state.selectedId);" in ui,
            "Agent details must refresh live fields without rebuilding steering input")
    report("agent_capacity_guidance",
           "Open Sessions > Active to monitor or steer them" in cognition,
           "Capacity errors must direct users to active sessions")
    report("agent_operations_keyed_cards", "existing[child.dataset.agentId]" in ui and "updateAgentCard(card, agent)" in ui)
    report("agent_operations_entry_animation_new_only", "agent-card agent-card-enter" in ui and ".agent-card.agent-card-enter" in open("core/style.css").read())
    report("agent_operations_graph_fetch", "data.graph" in ui)
    report("agent_operations_steer_queue", 'task.setdefault("steer_queue", [])' in worker)
    report("agent_operations_spawn_capability", "id: 'agent.spawn_batch'" in cognition)
    report("agent_operations_spawn_endpoint_call", "'/v1/subagent/spawn-batch'" in cognition)
    report("agent_operations_spawn_forces_cognition", "spin\\s+up" in cognition and "kick\\s+off|start|run" in cognition and "subagents?" in cognition)
    report("agent_operations_multi_agent_turn_intent", "multi[- ]agent(?:ic)?" in cognition and "turn|session|workflow|test|again" in cognition)
    report("agent_operations_repeat_batch", "eva_last_agent_batch" in cognition and "_agentRepeatIntent" in cognition and "repeatIntent ? priorBatch.tasks" in cognition)
    report("agent_operations_repeat_requires_agent_words", "(?:agents?|subagents?|batch|agent\\s+(?:batch|run|workflow))" in cognition and "do\\s+it\\s+again" not in cognition)
    report("agent_operations_deterministic_fallback", "async function ensureAgentLaunch" in cognition and "_fallbackAgentTasks" in cognition)
    report("agent_operations_nonempty_action_success", "Array.isArray(action.result.tasks) && action.result.tasks.length > 0" in cognition)
    report("agent_operations_deferred_signal", "deferredSignal" in cognition and "!deferredSignal && !!(signalContext && signalContext.authorized)" in open("core/js/providers/aig.js").read())
    report("agent_operations_collaboration_metadata", "synthesis[\"depends_on\"]" in bridge and "signal_on_complete" in cognition)
    report("agent_operations_signal_authorized", "getBridgeCapabilityHeaders" in cognition and "signal_on_complete and not self._require_bridge_capability()" in bridge)
    report("agent_operations_signal_fails_closed", "typeof canAuthorizeSignalDelivery === 'function'" in cognition and ": false;" in cognition)
    report("agent_operations_scoped_action_context", "actionContext = { userMessage:" in cognition and "cap.run(spec.args || {}, actionContext)" in cognition)
    report("agent_operations_no_shared_user_context", "_activeAgentUserMessage" not in cognition)
    report("agent_operations_lm_context", "Cognition.executeActions(candidate, { userMessage: sQuestion })" in open("core/js/providers/lm-studio.js").read())
    report("agent_operations_finalizing_steer_rejected", 'task.get("status") == "finalizing"' in bridge and "task is finalizing completion delivery" in bridge)
    report("agent_operations_dismiss_endpoint", 'parsed_path.startswith("/v1/subagent/")' in bridge and "def _subagent_dismiss" in bridge)
    report("agent_operations_dismiss_control", "function dismissAgent" in ui and "agent-card-dismiss" in ui)
    report("agent_operations_agent_graph", '"type": "agent"' in bridge and '"label": "feeds"' in bridge)
    report("agent_operations_agent_nodes_prioritized", "selectGraphNodes(sourceNodes, 90)" in ui and "node.type === 'agent'" in ui)
    report("agent_operations_eva_fixed_root", "node.id === 'eva-root'" in ui and "existing.x = 0.5" in ui)
    report("agent_operations_eva_self_entry", '"id": "eva"' in bridge and '"kind": "eva"' in bridge and '"session_id": active_session_id' in bridge and "eva: 'PRIMARY AGENT'" in ui and "session_id=" in ui)
    report("agent_operations_rich_tooltip", "function graphTooltipText" in ui and "Orchestrates:" in ui and "Receives from:" in ui and "Confidence:" in ui)
    report("agent_operations_live_graph_status", "agentById['agent-' + agent.id]" in ui and "node.status = liveAgent.status" in ui)
    report("agent_operations_done_graph_state", "doneAgent" in ui and "statusLabel(node.status)" in ui)
    report("agent_operations_model_routing", 'get("model", "")' in bridge and "selected_model = model or template.model" in worker)
    report("agent_operations_no_native_prompt", "prompt(" not in profiles and "prompt(" not in sessions)
    with open("tools/bridge/acp_client.py") as f:
        acp_client = f.read()
    report("agent_operations_acp_tool_uuid", re.search(r"^import uuid$", acp_client, re.MULTILINE) is not None)
    with open("tools/bridge/cron.py") as f:
        cron = f.read()
    report("agent_operations_notification_uuid", re.search(r"^import uuid$", cron, re.MULTILINE) is not None)
    report("agent_operations_notification_nonfatal", "Completion notification failed" in worker)
    report("agent_operations_isolated_client", "client = ACPClient(" in worker and "client.stop()" in worker)
    report("agent_operations_structured_result", "def _subagent_result_text" in worker and 'result.get("error")' in worker)
    report("agent_operations_graph_throttle", "include_graph" in bridge and "graphFetchedAt" in ui)
    report("agent_operations_style_versioned", re.search(r'href="core/style\.css\?v=[^" ]+"', html) is not None)


def test_agent_operations_behavior():
    """Capacity reservation and graph IDs behave correctly."""
    spec = importlib.util.spec_from_file_location("agent_operations_core", "tools/bridge/core.py")
    if spec is None or spec.loader is None:
        report("agent_operations_import", False, "could not load bridge core")
        return
    bridge_core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge_core)

    original_tasks = bridge_core._st.subagent_tasks
    bridge_core._st.subagent_tasks = {}
    accepted = []
    accepted_lock = threading.Lock()

    def reserve(index):
        ok = bridge_core._reserve_subagent_task({"id": f"capacity-{index}", "status": "running"})
        with accepted_lock:
            accepted.append(ok)

    threads = [threading.Thread(target=reserve, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    report("agent_operations_atomic_capacity", sum(accepted) == bridge_core._SUBAGENT_MAX,
           f"accepted {sum(accepted)} tasks" if sum(accepted) != bridge_core._SUBAGENT_MAX else "")
    bridge_core._st.subagent_tasks = original_tasks

    bridge_core._st.subagent_tasks = {
        "existing-a": {"id": "existing-a", "status": "running"},
        "existing-b": {"id": "existing-b", "status": "running"},
    }
    rejected_batch = [
        {"id": "batch-a", "status": "running"},
        {"id": "batch-b", "status": "running"},
        {"id": "batch-c", "status": "waiting"},
    ]
    batch_reserved = bridge_core._reserve_subagent_batch(rejected_batch)
    report("agent_operations_atomic_batch_rejection",
           not batch_reserved and set(bridge_core._st.subagent_tasks) == {"existing-a", "existing-b"},
           "over-capacity batch was partially reserved")
    bridge_core._st.subagent_tasks = original_tasks

    startup_tasks = [
        {"id": "startup-a", "label": "A", "model": "", "status": "running", "_full_prompt": "a"},
        {"id": "startup-b", "label": "B", "model": "", "status": "running", "_full_prompt": "b"},
        {"id": "startup-c", "label": "C", "model": "", "status": "waiting", "_full_prompt": "c"},
    ]
    bridge_core._st.subagent_tasks = {task["id"]: task for task in startup_tasks}

    class FailingThread:
        starts = 0

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            FailingThread.starts += 1
            if FailingThread.starts == 2:
                raise RuntimeError("simulated start failure")

    startup_ok = bridge_core._start_reserved_subagent_batch(startup_tasks, thread_factory=FailingThread)
    report("agent_operations_batch_start_rollback",
           not startup_ok and bridge_core._st.subagent_tasks == {},
           "thread startup failure left reserved tasks behind")
    bridge_core._st.subagent_tasks = original_tasks

    shared_prefix = "A durable fact with the same display prefix that extends beyond fifty-three characters: "
    graph = bridge_core._knowledge_graph_snapshot([
        {"Entity": "User", "Relation": "note", "Value": shared_prefix + "alpha", "Confidence": 0.8},
        {"Entity": "User", "Relation": "note", "Value": shared_prefix + "beta", "Confidence": 0.8},
        {"Entity": "Eva", "Relation": "role", "Value": "AI assistant with persistent memory", "Confidence": 0.95},
        {"Entity": "Open", "Relation": "recurring_topic", "Value": "repeated mention", "Confidence": 0.8},
        {"Entity": "Thank", "Relation": "recurring_topic", "Value": "repeated mention", "Confidence": 0.8},
    ])
    target_ids = {edge["target"] for edge in graph["edges"] if edge.get("label") == "note"}
    report("agent_operations_distinct_fact_ids", len(target_ids) == 2,
           "long facts collapsed to one node" if len(target_ids) != 2 else "")
    root_nodes = [node for node in graph["nodes"] if node.get("id") == "eva-root"]
    graph_labels = {node.get("label") for node in graph["nodes"]}
    role_facts = [node for node in graph["nodes"] if node.get("relation") == "role"]
    report(
        "agent_operations_eva_root_label",
        len(root_nodes) == 1 and root_nodes[0].get("label") == "Eva" and root_nodes[0].get("type") == "core",
        "Eva root is missing, duplicated, or unlabeled",
    )
    report(
        "agent_operations_noise_filtered",
        "Open" not in graph_labels and "Thank" not in graph_labels,
        "low-value recurring topic leaked into topology",
    )
    report(
        "agent_operations_fact_metadata",
        bool(role_facts) and role_facts[0].get("source_label") == "Eva" and role_facts[0].get("confidence") == 0.95,
        "fact hover metadata is incomplete",
    )

    topology_tasks = [
        {"id": "alpha", "label": "Alpha", "status": "done", "model": "default", "group_id": "group-1", "result": "evidence"},
        {"id": "delta", "label": "Delta", "status": "waiting", "model": "gpt-5.2", "group_id": "group-1", "depends_on": ["alpha"]},
    ]
    bridge_core._append_agent_topology(graph, topology_tasks)
    topology_edges = {(edge.get("source"), edge.get("target"), edge.get("type")) for edge in graph["edges"]}
    agent_nodes = {node.get("id"): node for node in graph["nodes"] if node.get("type") == "agent"}
    report(
        "agent_operations_eva_orchestration_edges",
        ("eva-root", "agent-alpha", "orchestration") in topology_edges
        and ("eva-root", "agent-delta", "orchestration") in topology_edges,
        "agents are disconnected from Eva",
    )
    report(
        "agent_operations_dependency_edge",
        ("agent-alpha", "agent-delta", "dependency") in topology_edges,
        "collaboration dependency edge is missing",
    )
    report(
        "agent_operations_graph_status_metadata",
        agent_nodes.get("agent-alpha", {}).get("status") == "done"
        and agent_nodes.get("agent-delta", {}).get("status") == "waiting"
        and agent_nodes.get("agent-delta", {}).get("model") == "gpt-5.2",
        "agent state/model metadata is missing",
    )

    historical_tasks = {
        f"done-{index}": {"id": f"done-{index}", "status": "done"}
        for index in range(25)
    }
    historical_tasks["older-active"] = {"id": "older-active", "status": "steering"}
    active_count, visible_tasks = bridge_core._select_subagent_overview_tasks(historical_tasks)
    visible_ids = {task["id"] for task in visible_tasks}
    report("agent_operations_full_history_metrics", active_count == 1 and "older-active" in visible_ids,
           "older active task missing from overview metrics or display")
    topology_history = {
        "older-running": {"id": "older-running", "status": "running"},
        **{
            f"newer-done-{index}": {"id": f"newer-done-{index}", "status": "done"}
            for index in range(30)
        },
    }
    _, topology_visible = bridge_core._select_subagent_overview_tasks(topology_history, limit=30)
    report("agent_operations_active_topology_history",
           "older-running" in {task["id"] for task in topology_visible},
           "older active task was omitted from topology history")
    topology_history["older-running"].update({
        "status": "done",
        "ended_at": "2099-01-01T00:00:00+00:00",
    })
    _, completed_overview = bridge_core._select_subagent_overview_tasks(topology_history, limit=20)
    _, completed_topology = bridge_core._select_subagent_overview_tasks(topology_history, limit=30)
    report("agent_operations_done_transition_retained",
           "older-running" in {task["id"] for task in completed_overview} and
           "older-running" in {task["id"] for task in completed_topology},
           "recently completed older task disappeared before showing done")
    mixed_agents = [{
        "id": "eva", "kind": "eva", "status": "online",
        "started_at": "2026-01-01T00:00:00+00:00", "ended_at": None,
    },
        {
            "id": "transitioned", "kind": "subagent", "status": "done",
            "started_at": "2000-01-01T00:00:00+00:00", "ended_at": "2099-01-01T00:00:00+00:00",
        }
    ] + [
        {
            "id": f"sub-history-{index}", "kind": "subagent", "status": "done",
            "started_at": f"2026-01-{index + 1:02d}T00:00:00+00:00",
            "ended_at": f"2026-02-{index + 1:02d}T00:00:00+00:00",
        }
        for index in range(19)
    ] + [
        {
            "id": f"browser-history-{index}", "kind": "browser", "status": "done",
            "started_at": f"2026-03-{index + 1:02d}T00:00:00+00:00",
            "ended_at": f"2026-04-{index + 1:02d}T00:00:00+00:00",
        }
        for index in range(11)
    ]
    mixed_visible = bridge_core._select_agent_payload(mixed_agents, limit=30)
    report("agent_operations_payload_retains_transition",
           "transitioned" in {item["id"] for item in mixed_visible},
           "final mixed-agent payload dropped recent completion transition")
    report("agent_operations_payload_pins_eva",
           mixed_visible[0]["id"] == "eva" and len(mixed_visible) == 30,
           "Eva self-agent was displaced from the bounded payload")

    agent_runs = [{"id": "older-active-run", "status": "running"}] + [
        {"id": f"finished-run-{index}", "status": "done"}
        for index in range(10)
    ]
    visible_runs = bridge_core._select_active_history(agent_runs, {"starting", "running"}, 10)
    report("agent_operations_active_run_history",
           "older-active-run" in {run["id"] for run in visible_runs},
           "older active browser/desktop run was hidden by completed history")
    boundary_runs = [
        {"id": f"active-run-{index}", "status": "running"}
        for index in range(10)
    ] + [
        {"id": f"inactive-run-{index}", "status": "done"}
        for index in range(25)
    ]
    boundary_visible = bridge_core._select_active_history(boundary_runs, {"running"}, 10)
    report("agent_operations_history_zero_boundary", len(boundary_visible) == 10,
           f"selected {len(boundary_visible)} records at active limit")

    from bridge import utils as bridge_utils
    original_dependency_tasks = bridge_utils._st.subagent_tasks
    bridge_utils._st.subagent_tasks = {
        "upstream-a": {"id": "upstream-a", "label": "Alpha", "status": "done", "result": "alpha evidence"},
        "upstream-b": {"id": "upstream-b", "label": "Beta", "status": "done", "result": "beta analysis"},
        "synthesis": {"id": "synthesis", "status": "waiting", "depends_on": ["upstream-a", "upstream-b"]},
    }
    dependency_context = bridge_utils._subagent_dependency_context("synthesis", timeout=1)
    synthesis_task = bridge_utils._st.subagent_tasks["synthesis"]
    report("agent_operations_dependency_context",
           "alpha evidence" in dependency_context and "beta analysis" in dependency_context,
           "upstream outputs missing from synthesis context")
    report("agent_operations_waiting_transition", synthesis_task["status"] == "running",
           "synthesis task did not transition from waiting to running")
    bridge_utils._st.subagent_tasks = original_dependency_tasks

    from bridge import alerts as bridge_alerts
    original_signal_send = bridge_alerts._signal_send
    delivered_messages = []
    bridge_alerts._signal_send = lambda message: delivered_messages.append(message) or True
    signal_task = {"id": "synthesis", "signal_on_complete": True, "signal_status": "queued"}
    signal_status = bridge_utils._deliver_subagent_completion_signal(signal_task, "line one\nline two")
    bridge_alerts._signal_send = original_signal_send
    report(
        "agent_operations_completion_signal",
        signal_status == "sent"
        and signal_task["signal_status"] == "sent"
        and signal_task["signal_on_complete"] is False
        and delivered_messages == ["line one line two"],
        "completion Signal was not delivered from finalized synthesis text",
    )
    replay_status = bridge_utils._deliver_subagent_completion_signal(signal_task, "replayed")
    report(
        "agent_operations_signal_consent_single_use",
        replay_status == "" and delivered_messages == ["line one line two"],
        "completion Signal authorization was replayed",
    )
    bridge_alerts._signal_send = lambda message: False
    failed_signal_task = {"id": "failed-synthesis", "signal_on_complete": True, "signal_status": "queued"}
    failed_status = bridge_utils._deliver_subagent_completion_signal(failed_signal_task, "summary")
    empty_signal_task = {"id": "empty-synthesis", "signal_on_complete": True, "signal_status": "queued"}
    empty_status = bridge_utils._deliver_subagent_completion_signal(empty_signal_task, "")
    bridge_alerts._signal_send = original_signal_send
    report(
        "agent_operations_failed_signal_visible",
        failed_status == "failed"
        and empty_status == "failed"
        and failed_signal_task["signal_status"] == "failed"
        and empty_signal_task["signal_status"] == "failed",
        "failed or empty completion Signal did not retain failed status",
    )

    from bridge import acp_client as bridge_acp_client
    original_acp_client_class = bridge_acp_client.ACPClient
    original_template_client = bridge_utils._st.acp_client
    original_live_tasks = bridge_utils._st.subagent_tasks
    original_notification = bridge_utils._push_notification
    observed_live_output = []

    class ChunkingACPClient:
        def __init__(self, **kwargs):
            self.model = kwargs.get("model")

        def start(self):
            pass

        def stop(self):
            pass

        def prompt(self, text, timeout=120, on_chunk=None, permission_mode="interactive", on_event=None):
            if on_event:
                on_event({"kind": "tool", "label": "Using read (running)"})
            if on_chunk:
                on_chunk("x" * 4000)
                observed_live_output.append((
                    bridge_utils._st.subagent_tasks["live-output"]["result"],
                    bridge_utils._st.subagent_tasks["live-output"]["output_chars"],
                ))
                on_chunk("y" * 4000)
                observed_live_output.append((
                    bridge_utils._st.subagent_tasks["live-output"]["result"],
                    bridge_utils._st.subagent_tasks["live-output"]["output_chars"],
                ))
            return {"text": "visible output"}

    class TemplateACPClient:
        alive = True
        copilot_path = "copilot"
        cwd = os.getcwd()
        model = "test-model"
        mcp_config = {}
        reasoning_effort = None

    try:
        bridge_acp_client.ACPClient = ChunkingACPClient
        bridge_utils._st.acp_client = TemplateACPClient()
        bridge_utils._st.subagent_tasks = {
            "live-output": {
                "id": "live-output", "label": "Live output", "status": "running",
                "result": None, "steer_queue": [], "signal_on_complete": False,
            },
        }
        bridge_utils._push_notification = lambda *args, **kwargs: None
        bridge_utils._subagent_worker("live-output", "show progress", "Live output")
        live_task = bridge_utils._st.subagent_tasks["live-output"]
        report(
            "agent_operations_live_output",
            observed_live_output == [("x" * 4000, 4000), ("y" * 4000, 8000)]
            and live_task["result"] == "visible output"
            and live_task["output_chars"] == 8000
            and live_task["activity"] == "Using read (running)"
            and live_task["status"] == "done"
            and bool(live_task["last_output_at"]),
            "streamed ACP output did not update the running task",
        )
    finally:
        bridge_acp_client.ACPClient = original_acp_client_class
        bridge_utils._st.acp_client = original_template_client
        bridge_utils._st.subagent_tasks = original_live_tasks
        bridge_utils._push_notification = original_notification

    bridge_core._st.subagent_tasks = {
        f"active-{index}": {"id": f"active-{index}", "status": "running"}
        for index in range(bridge_core._SUBAGENT_MAX)
    }
    rejected_task = {"id": "rejected", "status": "done", "steer_history": []}
    steer_result = bridge_core._prepare_subagent_steer(rejected_task, "do not record this")
    report("agent_operations_rejected_steer_immutable",
           steer_result is None and rejected_task["steer_history"] == [],
           "rejected steering request mutated task history")
    resumable_task = {
        "id": "resume-synthesis", "label": "Delta", "status": "done", "result": "prior",
        "signal_on_complete": True, "signal_status": "sent", "steer_history": [], "steer_queue": [],
    }
    bridge_core._st.subagent_tasks = {"resume-synthesis": resumable_task}
    resume_result = bridge_core._prepare_subagent_steer(resumable_task, "revise")
    report("agent_operations_resume_clears_signal_consent",
           resume_result is not None and resumable_task["signal_on_complete"] is False and resumable_task["signal_status"] == "",
           "resumed completed synthesis retained Signal authorization")

    bridge_core._st.subagent_tasks = {
        "active": {"id": "active", "status": "running"},
        "upstream": {"id": "upstream", "status": "done"},
        "synthesis": {"id": "synthesis", "status": "waiting", "depends_on": ["upstream"]},
        "finished": {"id": "finished", "status": "done"},
    }
    active_dismissed, active_reason = bridge_core._dismiss_subagent_task("active")
    upstream_dismissed, upstream_reason = bridge_core._dismiss_subagent_task("upstream")
    finished_dismissed, finished_reason = bridge_core._dismiss_subagent_task("finished")
    report(
        "agent_operations_active_dismiss_blocked",
        not active_dismissed and active_reason == "active" and "active" in bridge_core._st.subagent_tasks,
        "active task could be dismissed",
    )
    report(
        "agent_operations_dependency_dismiss_blocked",
        not upstream_dismissed and upstream_reason == "dependency" and "upstream" in bridge_core._st.subagent_tasks,
        "active synthesis lost its upstream task",
    )
    report(
        "agent_operations_terminal_dismissed",
        finished_dismissed and finished_reason == "" and "finished" not in bridge_core._st.subagent_tasks,
        "terminal standalone task was not dismissed",
    )
    bridge_core._st.subagent_tasks = original_tasks


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
        ("PR Automation", [test_pr_automation_workflows]),
        ("Python Integrity", [test_python_syntax, test_artifact_filename_validation, test_bridge_health_contract, test_aig_request_contract, test_aig_preflight_contract, test_http_routes_contract]),
        ("Local Speech Contract", [test_local_speech_contract, test_local_speech_http_contract, test_voice_module_contracts]),
        ("Kusto CSV Logic", [test_csv_quoting_logic]),
        ("HTML Model Selector", [test_model_selector, test_model_catalog_contract, test_provider_paths_contract]),
        ("Protected Memory Settings", [test_protected_memory_settings_contract]),
        ("JS Routing Functions", [test_js_routing_functions]),
        ("Learning Contract", [test_learning_static_contract]),
        ("Reasoning Effort", [test_reasoning_effort_contract]),
        ("Signal and GitHub MCP", [test_signal_and_github_mcp_contract, test_latency_telemetry_contract, test_issue_130_latency_contract, test_prompt_budget_contract, test_streaming_contract, test_acp_permissions_ui_contract]),
        ("Security Alerts", [test_security_alert_contract, test_alerts_settings_ui_contract, test_proactive_notifications_contract]),
        ("Sidebar Workflows", [test_sidebar_workflow_contract, test_browser_agent_api_contract, test_camera_api_contract, test_assets_api_contract, test_agents_api_contract, test_skills_api_contract, test_workspaces_api_contract, test_sessions_api_contract]),
        ("Workspace Terminal", [test_workspace_terminal_contract]),
        ("Coding Workspaces", [test_coding_workspace_contract]),
        ("Pages Comparison", [test_pages_comparison_contract]),
        ("Seed File", [test_seed_file]),
        ("Goals Static Contract", [test_goals_static_contract, test_goals_settings_contract, test_runtime_settings_contract, test_cron_settings_contract, test_prompts_settings_contract, test_audio_settings_contract, test_skill_auto_learn_contract, test_frontend_script_order_contract, test_bridge_client_contract]),
        ("Background Static Contract", [test_background_static_contract, test_background_settings_ui_contract]),
        ("Agent Operations Contract", [test_agent_operations_contract, test_agent_operations_behavior]),
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
