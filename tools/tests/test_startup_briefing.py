#!/usr/bin/env python3
"""Focused startup-briefing state contract tests."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from bridge import briefing, state


def first_live_source(name, _prompt, _timeout):
  return {
    "news": ("News", "ready"),
    "weather": ("No location", "failed"),
    "markets": ("Markets", "ready"),
  }[name]


def main():

    state.startup_briefing = briefing._new_state()
    state.startup_briefing_thread = None
    with (patch.object(briefing, "_memory_source", return_value=("Local context", "ready")),
          patch.object(briefing, "_wait_for_live_tools", return_value=True),
      patch.object(briefing, "_live_source", side_effect=first_live_source),
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

    local_calls = []
    previous_mode = state.local_mode
    state.local_mode = True
    state.startup_briefing = briefing._new_state()
    try:
      with (patch.object(briefing, "_memory_source", return_value=("Local context", "ready")),
            patch.object(briefing, "_mail_source", return_value=("No unread mail.", "ready")),
            patch.object(briefing, "_wait_for_live_tools", return_value=True),
            patch.object(briefing, "_live_source", side_effect=lambda name, prompt, timeout: (local_calls.append(name) or (name.title(), "ready"))),
            patch.object(briefing, "audit_event")):
        briefing._prepare_worker()
    finally:
      state.local_mode = previous_mode
    assert local_calls == ["news", "weather", "markets"]

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
      with patch.object(briefing, "_briefing_weather_location", return_value="Example City"):
        weather_text, weather_status = briefing._live_source("weather", "Current weather", 12)
      assert weather_text == "Local facts"
      assert weather_status == "ready"
      market_text, market_status = briefing._live_source("markets", "Current markets", 12)
      assert market_text == "Local facts"
      assert market_status == "ready"
      assert manager.calls == [
        ("web_search_news", {"query": "top national and world news headlines today", "max_results": 6}, 12),
        ("weather_current", {"location": "Example City"}, 12),
        ("web_search_news", {"query": "S&P 500 Dow Nasdaq US stock market today", "max_results": 6}, 12),
      ]
      with patch.object(briefing, "_briefing_weather_location", return_value=""):
        missing_text, missing_status = briefing._live_source("weather", "Current weather", 12)
      assert missing_status == "failed"
      assert "not learned your weather location" in missing_text
    finally:
      state.local_mode = previous_mode
      state.local_mcp_manager = previous_manager
    print("startup briefing tests: PASS")


if __name__ == "__main__":
    main()