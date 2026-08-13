#!/usr/bin/env python3
"""Focused startup-briefing state contract tests."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from bridge import briefing, state


def main():
    state.startup_briefing = briefing._new_state()
    state.startup_briefing_thread = None
    with (patch.object(briefing, "_memory_source", return_value=("Local context", "ready")),
          patch.object(briefing, "_wait_for_live_tools", return_value=True),
          patch.object(briefing, "_live_source", side_effect=[("News", "ready"), ("No location", "failed"), ("Markets", "ready")]),
          patch.object(briefing, "audit_event")):
        briefing._prepare_worker()
    result = briefing.briefing_status()
    assert result["status"] == "ready"
    assert result["sources"]["weather"]["status"] == "failed"
    assert result["sources"]["news"]["status"] == "ready"
    assert "Morning briefing prepared" in result["summary"]
    assert "News: News" in briefing.briefing_prompt_context()
    assert "Markets: Markets" in briefing.briefing_prompt_context()
    assert "Weather: unavailable" in briefing.briefing_prompt_context()
    assert briefing.briefing_unavailable_sources(result) == []

    state.startup_briefing = briefing._new_state()
    state.startup_briefing.update({
      "status": "preparing",
      "sources": {"memory": {"status": "ready", "summary": "Early local context"}},
    })
    assert briefing.briefing_prompt_context() == ""
    assert "Memory: Early local context" in briefing.briefing_prompt_context(allow_partial=True)

    state.startup_briefing = briefing._new_state()
    with (patch.object(briefing, "_memory_source", return_value=("Local context", "ready")),
          patch.object(briefing, "_wait_for_live_tools", return_value=True),
          patch.object(briefing, "_live_source", return_value=("Live context", "ready")),
          patch.object(briefing, "audit_event")):
        briefing._prepare_worker()
    prepared = briefing.briefing_prompt_context()
    assert "Memory: Local context" in prepared
    assert "News: Live context" in prepared

    state.startup_briefing = briefing._new_state()
    with (patch.object(briefing, "_memory_source", return_value=("Local context", "ready")),
          patch.object(briefing, "_wait_for_live_tools", return_value=False),
          patch.object(briefing, "_live_source") as live_source,
          patch.object(briefing, "audit_event")):
        briefing._prepare_worker()
    result = briefing.briefing_status()
    assert result["status"] == "partial"
    assert result["sources"]["news"]["status"] == "failed"
    assert not live_source.called
    assert briefing.briefing_unavailable_sources(result) == ["news", "markets"]

    state.startup_briefing = briefing._new_state()
    with (patch.object(briefing, "_memory_source", return_value=("", "cancelled")),
          patch.object(briefing, "_wait_for_live_tools", return_value=True),
          patch.object(briefing, "_live_source", return_value=("", "failed")),
          patch.object(briefing, "audit_event")):
        briefing._prepare_worker()
    result = briefing.briefing_status()
    assert result["status"] == "partial"
    assert result["sources"]["memory"]["status"] == "cancelled"

    class LocalManager:
      alive = True
      tool_count = 1

      def __init__(self):
        self.calls = []

      def call_tool(self, tool_name, arguments, timeout=0):
        self.calls.append((tool_name, arguments, timeout))
        return {"text": "Local facts"}

    previous_mode = state.local_mode
    previous_manager = state.local_mcp_manager
    state.local_mode = True
    manager = LocalManager()
    state.local_mcp_manager = manager
    try:
      text, status = briefing._live_source("news", "Current facts", 12)
      assert text == "Local facts"
      assert status == "ready"
      assert manager.calls == [("web_search_news", {"query": "top news headlines today", "max_results": 6}, 12)]
      _, weather_status = briefing._live_source("weather", "Current weather", 12)
      assert weather_status == "failed"
    finally:
      state.local_mode = previous_mode
      state.local_mcp_manager = previous_manager
    print("startup briefing tests: PASS")


if __name__ == "__main__":
    main()