#!/usr/bin/env python3
"""Capability registry consistency and runtime readiness contracts."""

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from bridge import capabilities, state
from bridge.core import BridgeHandler


def main():
    harness = (ROOT / "core/js/harness-control.js").read_text(encoding="utf-8")
    manifest = harness[harness.index("var actionManifest = ["):harness.index("function normalize", harness.index("var actionManifest = ["))]
    browser_actions = set(re.findall(r"\{ id: '([^']+)'", manifest))
    assert browser_actions == set(capabilities.NATIVE_HARNESS_ACTIONS)

    original_manager = state.local_mcp_manager
    original_acp = state.acp_client
    state.local_mcp_manager = SimpleNamespace(
        alive=True,
        servers={"fixture": SimpleNamespace(alive=True, tools=[{
            "name": "fixture_tool", "description": "Fixture local tool."
        }])},
    )
    state.acp_client = None
    try:
        with patch("bridge.cognition._active_skill_rows_for_decision", return_value=[{
            "SkillId": "skill-playlist", "Name": "Play my YouTube playlist",
            "Config": '{"validation":{"status":"passed"},"approved_url":"https://example.com/playlist"}',
            "Instructions": "Open the approved URL after explicit confirmation."
        }]):
            view = capabilities.runtime_capabilities()
        ids = {item["id"] for item in view["capabilities"]}
        assert "memory" in ids
        assert "native-harness" in ids
        assert "mcp:fixture_tool" in ids
        assert "skill:skill-playlist" in ids
        skill = next(item for item in view["capabilities"] if item["id"] == "skill:skill-playlist")
        assert skill["executor"] == "verified-skill"
        assert skill["validation"] == "receipt-required"
        assert skill["status"] == "available"
        assert next(item for item in view["capabilities"] if item["id"] == "acp-tools")["status"] == "unavailable"
        prompt = capabilities.runtime_capability_prompt_view()
        assert "mcp:fixture_tool via local-mcp:fixture" in prompt

        with patch("bridge.cognition._active_skill_rows_for_decision", return_value=[{
            "SkillId": "skill-unready", "Name": "Unvalidated Skill", "Instructions": "Use the configured URL."
        }]):
            unready = capabilities.runtime_capabilities()
        assert next(item for item in unready["capabilities"] if item["id"] == "skill:skill-unready")["status"] == "needs-validation"

        captured = {}
        handler = BridgeHandler.__new__(BridgeHandler)
        handler._json_response = lambda status, payload: captured.update(status=status, payload=payload)
        handler._runtime_capabilities()
        assert captured["status"] == 200
        assert captured["payload"]["version"] == 2
    finally:
        state.local_mcp_manager = original_manager
        state.acp_client = original_acp

    core = (ROOT / "tools/bridge/core.py").read_text(encoding="utf-8")
    assert 'parsed_path == "/v1/runtime/capabilities"' in core
    assert "runtime_capability_prompt_view()" in core
    cognition = (ROOT / "tools/bridge/cognition.py").read_text(encoding="utf-8")
    assert "[Capability Guidance]" in cognition
    assert "A verified playlist Skill opens its authorized URL through the native harness" in cognition
    assert "Persist it via kusto_ingest_inline" not in cognition
    print("runtime capability tests: PASS")


if __name__ == "__main__":
    main()
