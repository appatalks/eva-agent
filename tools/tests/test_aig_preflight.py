#!/usr/bin/env python3
"""Focused AIG preflight planning contracts."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from bridge.aig_preflight import plan_aig_preflight


def needs_preflight(message, request_type):
    return request_type in {"web-search", "weather-search", "kusto-query"}


def select_profile(message, request_type, fast_route="", no_tools=False):
    if no_tools:
        return "none"
    return "kusto" if request_type == "kusto-query" else "web"


class AigPreflightTests(unittest.TestCase):
    def plan(self, message, request_type="general", **overrides):
        values = {
            "fast_route": "",
            "internal": False,
            "force_retrieve": False,
            "acp_available": True,
            "local_mode": False,
            "no_tools": False,
        }
        values.update(overrides)
        return plan_aig_preflight(
            message, request_type,
            needs_preflight=needs_preflight,
            select_tool_profile=select_profile,
            **values,
        )

    def test_fast_internal_and_unavailable_routes(self):
        self.assertEqual(self.plan("hello", fast_route="greeting")["acp_route"], "fast/greeting")
        self.assertEqual(self.plan("draft", internal=True)["acp_route"], "internal-cognition")
        self.assertEqual(self.plan("question", acp_available=False)["acp_route"], "acp-unavailable")
        forced = self.plan("weather", "weather-search", internal=True, force_retrieve=True)
        self.assertTrue(forced["needs_acp_tools"])

    def test_greeting_meta_and_general_skip_routes(self):
        self.assertEqual(self.plan("Hello!")["acp_route"], "greeting/trivial")
        self.assertEqual(self.plan("What can you do?")["acp_route"], "meta-question")
        self.assertEqual(self.plan("Explain dependency injection")["acp_route"], "direct/general")

    def test_live_data_uses_tool_profiles_and_escalates(self):
        web = self.plan("Search the web", "web-search")
        self.assertFalse(web["skip_acp"])
        self.assertTrue(web["needs_acp_tools"])
        self.assertEqual(web["tool_profile"], "web")
        self.assertEqual(web["escalation"], "acp-preflight")
        kusto = self.plan("Query the table", "kusto-query")
        self.assertEqual(kusto["tool_profile"], "kusto")

    def test_briefing_uses_cache_not_preflight(self):
        result = self.plan("Prepare my morning briefing", "web-search")
        self.assertTrue(result["briefing_request"])
        self.assertFalse(result["needs_acp_tools"])
        self.assertEqual(result["acp_route"], "direct/general")


if __name__ == "__main__":
    unittest.main(verbosity=2)