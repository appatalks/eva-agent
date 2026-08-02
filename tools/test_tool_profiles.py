#!/usr/bin/env python3
"""Tests for route-scoped ACP MCP exposure and warm-pool isolation."""
import os
import sys
import unittest
from unittest.mock import patch

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from bridge import state as _st
from bridge.acp_client import (
    _acp_config_fingerprint,
    _acp_model_key,
    _acp_tool_profile_config,
    _ensure_acp_model,
)
from bridge.utils import _select_acp_tool_profile


class FakeClient:
    created = []

    def __init__(self, copilot_path="copilot", cwd=None, model=None, mcp_config=None,
                 reasoning_effort=None, tool_profile=None):
        self.copilot_path = copilot_path
        self.cwd = cwd or os.getcwd()
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.mcp_config = mcp_config or {}
        self.tool_profile = tool_profile
        self.config_fingerprint = "test"
        self.alive = True
        self.active_requests = 0
        self.created.append(self)

    def start(self):
        return None

    def stop(self):
        self.alive = False


class ToolProfileTests(unittest.TestCase):
    def setUp(self):
        self.old_client = _st.acp_client
        self.old_config = _st.configured_mcp_config
        self.old_pool = _st.acp_pool
        self.old_order = _st.acp_pool_order
        _st.acp_pool = {}
        _st.acp_pool_order = []
        _st.configured_mcp_config = {
            "eva-web-search": {"command": "python", "args": ["web_search_mcp.py"], "env": {}},
            "github-mcp-server": {"command": "docker", "args": ["run"], "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_SECRET_VALUE"}},
            "kusto-mcp-server": {"command": "python", "args": ["kusto_mcp.py"], "env": {"KUSTO_ACCESS_TOKEN": "kusto_SECRET_VALUE", "KUSTO_DATABASE": "Eva"}},
            "computer-use-linux": {"command": "computer-use-linux", "args": ["mcp"], "env": {}},
        }
        _st.acp_client = FakeClient(model="eva", mcp_config={}, tool_profile="none")
        FakeClient.created = []

    def tearDown(self):
        _st.acp_client = self.old_client
        _st.configured_mcp_config = self.old_config
        _st.acp_pool = self.old_pool
        _st.acp_pool_order = self.old_order

    def test_profile_filtering_and_route_mapping(self):
        self.assertEqual(_acp_tool_profile_config(_st.configured_mcp_config, "none"), {})
        self.assertEqual(set(_acp_tool_profile_config(_st.configured_mcp_config, "web")), {"eva-web-search"})
        self.assertEqual(set(_acp_tool_profile_config(_st.configured_mcp_config, "github")), {"github-mcp-server"})
        self.assertEqual(set(_acp_tool_profile_config(_st.configured_mcp_config, "kusto")), {"kusto-mcp-server"})
        self.assertEqual(set(_acp_tool_profile_config(_st.configured_mcp_config, "broad")), set(_st.configured_mcp_config))

        self.assertEqual(_select_acp_tool_profile("What is the weather today?", "weather-search"), "web")
        self.assertEqual(_select_acp_tool_profile("Search GitHub for issue 42", "general"), "github")
        self.assertEqual(_select_acp_tool_profile("Show my active goals", "general"), "kusto")
        self.assertEqual(_select_acp_tool_profile("Explain recursion", "general"), "none")
        self.assertEqual(_select_acp_tool_profile("Review this browser task", "general"), "broad")
        self.assertEqual(_select_acp_tool_profile("anything", "general", no_tools=True), "none")

    def test_fingerprint_and_pool_key_are_secret_safe(self):
        first = dict(_st.configured_mcp_config)
        first["github-mcp-server"] = dict(first["github-mcp-server"])
        first["github-mcp-server"]["env"] = {"GITHUB_PERSONAL_ACCESS_TOKEN": "one-secret"}
        second = dict(first)
        second["github-mcp-server"] = dict(first["github-mcp-server"])
        second["github-mcp-server"]["env"] = {"GITHUB_PERSONAL_ACCESS_TOKEN": "different-secret"}
        self.assertEqual(_acp_config_fingerprint(first), _acp_config_fingerprint(second))
        key = _acp_model_key("eva", "high", "github", first)
        self.assertNotIn("secret", key.lower())
        self.assertNotIn("ghp_", key.lower())
        self.assertNotEqual(key, _acp_model_key("eva", "high", "kusto", first))

    def test_pool_isolates_profiles_and_reuses_each_warm_client(self):
        with patch("bridge.acp_client.ACPClient", FakeClient):
            ok, _ = _ensure_acp_model("eva", tool_profile="web")
            self.assertTrue(ok)
            ok, _ = _ensure_acp_model("eva", tool_profile="github")
            self.assertTrue(ok)
            ok, _ = _ensure_acp_model("eva", tool_profile="web")
            self.assertTrue(ok)
        self.assertEqual(len(FakeClient.created), 2)
        self.assertEqual({client.tool_profile for client in FakeClient.created}, {"web", "github"})
        self.assertEqual(set(FakeClient.created[0].mcp_config) | set(FakeClient.created[1].mcp_config), {"eva-web-search", "github-mcp-server"})
        self.assertEqual(len(_st.acp_pool), 3)


if __name__ == "__main__":
    unittest.main()