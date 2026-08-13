#!/usr/bin/env python3
"""Focused deterministic model-policy contract tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from bridge.model_policy import select_model_policy


def decision(mode="auto-balanced", requested="gpt-5.6-luna", request_type="general", requires_tools=False, **facts):
    candidates = {
        "acp_available": True,
        "acp_model": "gpt-5.6-luna",
        "openai_available": True,
        "openai_model": "gpt-5.6-luna",
        "lmstudio_available": True,
        "lmstudio_model": "test-local-model",
    }
    candidates.update(facts.pop("candidates", {}))
    return select_model_policy(mode, requested, request_type, requires_tools, candidates, **facts)


def main():
    assert decision(mode="pinned") == {"provider": "pinned", "backend": "gpt-5.6-luna", "reason": "pinned"}
    assert decision(requires_tools=True)["provider"] == "acp"
    assert decision(requires_tools=True, candidates={"acp_available": False})["reason"] == "tool-route-unavailable"
    assert decision(local_only=True)["provider"] == "lmstudio"
    assert decision(local_only=True, candidates={"lmstudio_available": False})["reason"] == "local-unavailable"
    assert decision(mode="auto-fast")["reason"] == "fast-local"
    assert decision(mode="auto-fast", candidates={"lmstudio_available": False})["reason"] == "fast-direct"
    assert decision(candidates={"openai_available": False})["reason"] == "balanced-acp"
    assert decision(candidates={"openai_available": False, "acp_available": False})["reason"] == "balanced-local"
    assert decision(candidates={"openai_available": False, "acp_available": False, "lmstudio_available": False})["reason"] == "no-auto-candidate"
    print("model policy tests: PASS")


if __name__ == "__main__":
    main()