#!/usr/bin/env python3
"""Focused contract checks for fixed bridge route tables."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from bridge.http_routes import PATCH_ROUTES, match_patch_route


class PatchRouteTests(unittest.TestCase):
    def test_matches_all_known_routes(self):
        expected = {
            "/v1/goals/goal%2F1": ("_goals_patch", "goal/1"),
            "/v1/memory/atoms/atom%201": ("_memory_atom_patch", "atom 1"),
            "/v1/skills/skill-1": ("_skills_patch", "skill-1"),
            "/v1/cron/task%2F1": ("_cron_update", "task/1"),
        }
        for path, route in expected.items():
            with self.subTest(path=path):
                self.assertEqual(match_patch_route(path), route)

    def test_unknown_route_is_not_matched(self):
        self.assertIsNone(match_patch_route("/v1/alerts/rule-1"))
        self.assertIsNone(match_patch_route("/health"))

    def test_table_contains_only_private_handler_names(self):
        self.assertEqual(len(PATCH_ROUTES), 4)
        for prefix, handler_name in PATCH_ROUTES:
            self.assertTrue(prefix.startswith("/v1/"))
            self.assertTrue(handler_name.startswith("_"))


if __name__ == "__main__":
    unittest.main(verbosity=2)