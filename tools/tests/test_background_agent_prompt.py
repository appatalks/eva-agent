#!/usr/bin/env python3
"""Focused contract for background live-data tool-profile selection."""

import contextlib
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from bridge import background, state


class _Client:
    model = "test-model"
    alive = True


class _ProfiledClient:
    def prompt(self, prompt, timeout=0):
        assert prompt == "Current facts"
        assert timeout == 12
        return {"text": "Retrieved facts"}


def main():
    previous_client = state.acp_client
    previous_activity = state.last_user_activity_ts
    state.acp_client = _Client()
    state.last_user_activity_ts = 0
    captured = []

    @contextlib.contextmanager
    def acquire(model, tool_profile="none"):
        captured.append((model, tool_profile))
        yield _ProfiledClient(), ""

    try:
        with patch("bridge.acp_client._acquire_acp_client", acquire):
            text, error = background._bg_agent_prompt("Current facts", {"trigger": "startup"}, timeout=12)
        assert text == "Retrieved facts"
        assert error == ""
        assert captured == [("test-model", "web")]
    finally:
        state.acp_client = previous_client
        state.last_user_activity_ts = previous_activity
    print("background agent prompt tests: PASS")


if __name__ == "__main__":
    main()