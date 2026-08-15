#!/usr/bin/env python3
"""Pure Phase 3/4 skill execution and Weather routing contracts."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from bridge.skills import (
    build_weather_retrieval_prompt,
    resolve_weather_location,
    skill_execution_decision,
    skill_live_capabilities,
)
from sqlite_memory import _load_default_skill_rows


class SkillExecutionTests(unittest.TestCase):
    def setUp(self):
        self.skills = _load_default_skill_rows()

    def test_explicit_and_continuation_requests_precede_ordinary_matching(self):
        for request in ("use my Weather Report skill", "check the weather skill and use it"):
            decision = skill_execution_decision(
                request, self.skills,
                skill_live_capabilities(configured_data_paths={"weather-news": True}),
            )
            self.assertEqual(decision["selected_skill_id"], "skill-weather")
            self.assertEqual(decision["selection_reason"], "explicit-name")
        ordinary = skill_execution_decision(
            "What is the forecast?", self.skills,
            skill_live_capabilities(configured_data_paths={"weather-news": True}),
        )
        self.assertEqual(ordinary["selected_skill_id"], "skill-weather")
        self.assertEqual(ordinary["selection_reason"], "lexical")

    def test_whitespace_normalization_preserves_skill_and_location_routing(self):
        request = "check" + (" " * 5000) + "the Weather Report skill and use it"
        decision = skill_execution_decision(
            request, self.skills,
            skill_live_capabilities(configured_data_paths={"weather-news": True}),
        )
        self.assertEqual(decision["selected_skill_id"], "skill-weather")
        weather = next(row for row in self.skills if row["SkillId"] == "skill-weather")
        location = resolve_weather_location("weather" + (" " * 5000) + "in Seattle", weather)
        self.assertEqual(location["location"], "Seattle")

    def test_skill_management_does_not_execute(self):
        decision = skill_execution_decision("list my saved skills", self.skills)
        self.assertEqual(decision["status"], "skill-management")
        self.assertFalse(decision["selected_skill_id"])
        self.assertEqual(skill_execution_decision("check the weather skill", self.skills)["status"], "skill-management")
        continuation = skill_execution_decision("check the weather skill and use it", self.skills)
        self.assertNotEqual(continuation["status"], "skill-management")

    def test_ambiguous_explicit_name_is_honest(self):
        rows = [
            {"SkillId": "a", "Name": "Research", "Description": "", "Tools": "web-search", "Status": "active"},
            {"SkillId": "b", "Name": "Research", "Description": "", "Tools": "web-search", "Status": "active"},
        ]
        decision = skill_execution_decision("use the Research skill", rows)
        self.assertEqual(decision["status"], "ambiguous")
        self.assertEqual(decision["selection_reason"], "explicit-name")

    def test_precedence_and_allowed_fallback(self):
        weather = next(row for row in self.skills if row["SkillId"] == "skill-weather")
        mcp = skill_live_capabilities(
            configured_data_paths={"weather-news": True},
            local_mcp_tools=[{"name": "get_weather", "description": "weather forecast", "server": "weather"}],
        )
        self.assertEqual(skill_execution_decision("weather", [weather], mcp)["selected_tool"], "weather-news")
        fallback = skill_execution_decision(
            "weather", [weather], skill_live_capabilities(configured_data_paths={"web-search": True})
        )
        self.assertEqual(fallback["selected_tool"], "web-search")
        self.assertIn("fallback", fallback["fallback_reason"])
        unavailable = skill_execution_decision("weather", [weather], skill_live_capabilities())
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertNotIn("browser", unavailable["selected_tool"])
        self.assertNotIn("desktop", unavailable["selected_tool"])

    def test_weather_location_precedence_and_prompt(self):
        skill = next(row for row in self.skills if row["SkillId"] == "skill-weather")
        skill["Config"] = '{"defaults":{"default_location":"Austin"},"allowed_fallbacks":[]}'
        self.assertEqual(resolve_weather_location("weather in Seattle", skill, {"user_location": "London"})["location"], "Seattle")
        self.assertEqual(resolve_weather_location("weather", skill, {"user_location": "London"})["location"], "Austin")
        skill["Config"] = '{"defaults":{"default_location":""},"allowed_fallbacks":[]}'
        self.assertEqual(resolve_weather_location("weather", skill, {"user_location": "London"})["location"], "London")
        self.assertEqual(resolve_weather_location("weather", skill, [], "Denver")["location"], "Denver")
        unresolved = resolve_weather_location("weather", skill, [], "")
        self.assertEqual(unresolved["source"], "unresolved")
        prompt = build_weather_retrieval_prompt("weather", "Austin", "web-search")
        self.assertIn("Resolved location: Austin", prompt)
        self.assertIn("never use browser or desktop", prompt.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)