#!/usr/bin/env python3
"""Focused startup-briefing state contract tests."""

import sys
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from bridge import briefing, state
import web_search_mcp as web_search


def first_live_source(name, _prompt, _timeout):
  return {
    "news": ("News", "ready"),
    "weather": ("Weather", "ready"),
    "markets": ("Markets", "ready"),
  }[name]


def main():

    stale_receipt = json.dumps([{
      "title": "Yesterday's headline",
      "url": "https://example.com/yesterday",
      "date": "Thu, 03 Sep 2026 06:03:24 GMT",
    }])
    fresh_receipt = json.dumps([{
      "title": "Current headline",
      "url": "https://example.com/current",
      "date": "Fri, 04 Sep 2026 22:57:36 GMT",
    }])
    weather_receipt = json.dumps([{
      "kind": "current",
      "title": "Current weather for Example City",
      "snippet": "Clear; 72 F",
      "url": "https://example.com/weather",
      "source": "Google Weather",
      "retrieved_at": "2026-09-05T06:00:00Z",
    }, {
      "kind": "forecast",
      "title": "Today's forecast for Example City",
      "snippet": "High 82 F; Low 64 F",
      "url": "https://forecast.weather.gov/example",
      "source": "National Weather Service",
      "retrieved_at": "2026-09-05T06:00:00Z",
    }])
    briefing_now = datetime(2026, 9, 5, 6, tzinfo=timezone.utc)
    assert briefing._fresh_search_results(stale_receipt, briefing_now) == []
    current_results = briefing._fresh_search_results(fresh_receipt, briefing_now)
    assert [item["title"] for item in current_results] == ["Current headline"]
    assert briefing._search_receipt_has_results(current_results)

    class StaleManager:
      alive = True
      tool_count = 1

      def call_tool(self, _tool_name, _arguments, timeout=0):
        return {"text": stale_receipt}

    previous_mode = state.local_mode
    previous_manager = state.local_mcp_manager
    state.local_mode = True
    state.local_mcp_manager = StaleManager()
    try:
      with patch.object(briefing._cfg, "utc_now", return_value=briefing_now):
        stale_text, stale_status = briefing._live_source("news", "Current facts", 12)
      assert stale_status == "failed"
      assert stale_text == "No timely current headlines were returned."
      assert "Yesterday's headline" not in stale_text
    finally:
      state.local_mode = previous_mode
      state.local_mcp_manager = previous_manager

      with (patch.object(web_search, "_http_get", return_value=(200, "<html></html>")),
            patch.object(web_search, "ddg_search", return_value=[])):
        assert web_search.google_weather("Example City") == [{
          "info": "Current weather conditions unavailable for Example City"
        }]

      current_search = [{
        "title": "Example City Current Weather | AccuWeather",
        "url": "https://www.accuweather.com/en/us/example-city/current-weather/1",
        "snippet": "Example City is currently clear with a temperature of 72 degrees.",
      }]
      forecast_search = [{
        "title": "Example City - National Weather Service",
        "url": "https://forecast.weather.gov/MapClick.php?example=1",
        "snippet": "Updated today. High 82 F. Tonight Low 64 F.",
      }]
      with (patch.object(web_search, "_http_get", return_value=(200, "<html></html>")),
            patch.object(web_search, "ddg_search", side_effect=[current_search, forecast_search])):
        trusted_weather = web_search.google_weather("Example City")
      assert [item["kind"] for item in trusted_weather] == ["current", "forecast"]
      assert [item["source"] for item in trusted_weather] == ["AccuWeather", "National Weather Service"]

      nws_page = """
        <html><body>Exampleville TX
        <p class="myforecast-current-lrg">78&deg;F</p>
        <p class="myforecast-current">Mostly Cloudy</p>
        <li class="forecast-tombstone"><p class="period-name">Today</p><p class="temp temp-high">High: 97 &deg;F</p><p class="short-desc">Partly Sunny</p></li>
        <li class="forecast-tombstone"><p class="period-name">Tonight</p><p class="temp temp-low">Low: 78 &deg;F</p><p class="short-desc">Partly Cloudy</p></li>
        </body></html>
      """
      with patch.object(web_search, "_http_get", return_value=(200, nws_page)):
        nws_weather = web_search.google_weather("Exampleville Texas")
      assert "Mostly Cloudy" in nws_weather[0]["snippet"]
      assert "78" in nws_weather[0]["snippet"]
      assert "Today: Partly Sunny" in nws_weather[1]["snippet"]
      assert "High: 97" in nws_weather[1]["snippet"]
      assert "Tonight: Partly Cloudy" in nws_weather[1]["snippet"]
      assert "Low: 78" in nws_weather[1]["snippet"]

    state.startup_briefing = briefing._new_state()
    state.startup_briefing_thread = None
    with (patch.object(briefing, "_memory_source", return_value=("Local context", "ready")),
          patch.object(briefing, "_wait_for_live_tools", return_value=True),
      patch.object(briefing, "_live_source", side_effect=first_live_source),
          patch.object(briefing, "audit_event")):
        briefing._prepare_worker()
    result = briefing.briefing_status()
    assert result["status"] == "ready"
    assert result["sources"]["weather"]["status"] == "ready"
    assert result["sources"]["news"]["status"] == "ready"
    assert "Morning briefing prepared" in result["summary"]
    assert "News: News" in briefing.briefing_prompt_context()
    assert "Markets: Markets" in briefing.briefing_prompt_context()
    assert "Weather: Weather" in briefing.briefing_prompt_context()
    assert briefing.briefing_unavailable_sources(result) == []

    snapshot = briefing.briefing_status()
    state.startup_briefing["sources"]["news"]["summary"] = "Changed after snapshot"
    assert snapshot["sources"]["news"]["summary"] == "News"

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
    assert briefing.briefing_unavailable_sources(result) == ["weather", "news", "markets"]

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
        if tool_name == "weather_current":
          return {"text": weather_receipt}
        return {"text": fresh_receipt}

    previous_mode = state.local_mode
    previous_manager = state.local_mcp_manager
    state.local_mode = True
    manager = LocalManager()
    state.local_mcp_manager = manager
    try:
      with patch.object(briefing._cfg, "utc_now", return_value=briefing_now):
        text, status = briefing._live_source("news", "Current facts", 12)
        assert "Current headline" in text
        assert status == "ready"
        with patch.object(briefing, "_briefing_weather_location", return_value="Example City"):
          weather_text, weather_status = briefing._live_source("weather", "Current weather", 12)
        assert "Current weather for Example City" in weather_text
        assert weather_status == "ready"
        market_text, market_status = briefing._live_source("markets", "Current markets", 12)
        assert "Current headline" in market_text
        assert market_status == "ready"
      assert manager.calls == [
        ("web_search_news", {"query": "United States world news when:1d", "max_results": 6}, 12),
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