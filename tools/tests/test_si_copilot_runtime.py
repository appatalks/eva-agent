#!/usr/bin/env python3
"""SI routing and isolated ACP regressions without a CLI or network access."""
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import acp_client, core, state


BASE_URL = "http://localhost:1234/v1"
MODEL = "synthetic/qwen3:test"


class SIChatRoutingTests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        self.cloud = acp_client.ACPClient(model="synthetic-cloud")
        self.cloud.alive = True
        self.cloud.prompt = Mock(side_effect=AssertionError("Cloud fallback is forbidden"))
        stack.enter_context(patch.multiple(
            state, acp_client=self.cloud, acp_pool={}, acp_pool_order=[],
            configured_mcp_config={}, cognition_enabled=False, local_mode=False,
        ))
        for name in (
            "_set_openai_key_from", "_mark_user_activity", "_telemetry_emit",
            "_verbose_debug_emit", "audit_event",
        ):
            stack.enter_context(patch.object(core, name))
        stack.enter_context(patch.object(
            core, "_skill_execution_for_request", return_value=({}, {}, {})
        ))
        stack.enter_context(patch.object(core, "briefing_prompt_context", return_value=""))
        stack.enter_context(patch.object(core, "runtime_capability_prompt_view", return_value=""))
        stack.enter_context(patch(
            "bridge.email_service.capability_summary", return_value=""
        ))
        self.resolve = stack.enter_context(patch(
            "bridge.local_mcp._resolve_lmstudio_model", return_value=(MODEL, "")
        ))
        self.post = stack.enter_context(patch("requests.post"))
        self.post.return_value.status_code = 200
        self.post.return_value.json.return_value = {
            "choices": [{"message": {"content": "Synthetic direct response"}}]
        }
        self.client = Mock(model=MODEL)
        self.client.prompt.return_value = {"text": "Synthetic ACP response"}
        self.acquire = stack.enter_context(patch.object(core, "_acquire_acp_client"))
        self.acquire.return_value.__enter__.return_value = (self.client, MODEL)
        stack.enter_context(patch.object(acp_client, "_telemetry_emit"))
        self.handler = object.__new__(core.BridgeHandler)
        self.handler.headers = {}
        self.handler._json_response = Mock()
        self.handler._retrieve_local_data = Mock(
            side_effect=AssertionError("SI Copilot must own its unified tool loop")
        )

    def chat(self, **overrides):
        request = {
            "model": "lmstudio", "user_message": "Explain dependency injection.",
            "lmstudio_base_url": BASE_URL, "lmstudio_model": MODEL,
            "openai_api_key": "sk-FAKE-SI-REGRESSION",
        }
        request.update(overrides)
        self.handler._aig_chat(request)
        return self.handler._json_response.call_args.args

    def test_default_and_explicit_direct_use_only_the_configured_endpoint(self):
        for runtime in (None, "direct"):
            with self.subTest(runtime=runtime):
                status, response = self.chat(si_runtime=runtime)
                self.assertEqual(status, 200)
                self.assertEqual(response["model"], "aig:lmstudio:" + MODEL)
                self.assertEqual(self.post.call_args.args, (BASE_URL + "/chat/completions",))
                self.assertEqual(self.post.call_args.kwargs["json"]["model"], MODEL)
                self.acquire.assert_not_called()
                self.cloud.prompt.assert_not_called()

    def test_copilot_keeps_si_model_and_tools_under_pinned_and_automatic_policies(self):
        for policy in ("pinned", "auto-balanced", "auto-fast"):
            with self.subTest(policy=policy):
                self.client.prompt.reset_mock()
                status, response = self.chat(
                    si_runtime="copilot", model_policy_mode=policy,
                    user_message="What is the latest stock price of PKX?",
                )
                self.assertEqual(status, 200)
                self.assertEqual(response["model"], "aig:si-copilot:" + MODEL)
                self.client.prompt.assert_called_once()
                self.assertEqual(self.acquire.call_args.args[0], MODEL)
                options = self.acquire.call_args.kwargs
                self.assertEqual(options["provider_config"]["COPILOT_PROVIDER_BASE_URL"], BASE_URL)
                self.assertEqual(options["provider_config"]["COPILOT_MODEL"], MODEL)
                self.assertEqual(options["tool_profile"], "broad")
                self.assertFalse(options["no_tools"])
                self.post.assert_not_called()
                self.cloud.prompt.assert_not_called()
                self.handler._retrieve_local_data.assert_not_called()

    def test_tool_free_synthesis_stays_on_the_isolated_si_provider(self):
        status, _ = self.chat(si_runtime="copilot", no_tools=True, internal=True)
        self.assertEqual(status, 200)
        options = self.acquire.call_args.kwargs
        self.assertTrue(options["no_tools"])
        self.assertEqual(options["tool_profile"], "none")
        self.assertEqual(options["provider_config"]["COPILOT_MODEL"], MODEL)
        self.post.assert_not_called()

    def test_invalid_endpoint_model_or_runtime_fails_before_discovery_or_execution(self):
        cases = [
            {"lmstudio_base_url": "https://example.com:1234/v1", "lmstudio_model": ""},
            {"lmstudio_base_url": "file:///v1"},
            {"lmstudio_base_url": "http://example:fake@localhost:1234/v1"},
            {"lmstudio_base_url": BASE_URL + "?query=example"},
            {"lmstudio_base_url": BASE_URL + "#fragment"},
            {"lmstudio_base_url": "http://localhost:1/v1"},
            {"lmstudio_base_url": "http://localhost:1234/unexpected"},
            {"lmstudio_model": "synthetic\x00model"},
            {"lmstudio_model": "synthetic\nmodel"},
            {"si_runtime": "unsupported"},
        ]
        for case in cases:
            with self.subTest(case=case):
                status, response = self.chat(**dict({"si_runtime": "copilot"}, **case))
                self.assertEqual(status, 400)
                self.assertIn("message", response["error"])
                self.resolve.assert_not_called()
                self.acquire.assert_not_called()
                self.post.assert_not_called()
                self.cloud.prompt.assert_not_called()

    def test_missing_model_is_discovered_only_on_the_validated_endpoint(self):
        status, response = self.chat(si_runtime="copilot", lmstudio_model="")
        self.assertEqual(status, 200)
        self.resolve.assert_called_once_with(BASE_URL, "")
        self.assertEqual(self.acquire.call_args.args[0], MODEL)
        self.assertEqual(response["model"], "aig:si-copilot:" + MODEL)

    def test_discovery_failure_does_not_fall_back_to_available_cloud_providers(self):
        self.resolve.return_value = ("", "Synthetic model discovery failure")
        status, response = self.chat(
            si_runtime="copilot", lmstudio_model="", model_policy_mode="auto-balanced"
        )
        self.assertEqual(status, 400)
        self.assertIn("discovery failure", response["error"]["message"])
        self.acquire.assert_not_called()
        self.post.assert_not_called()
        self.cloud.prompt.assert_not_called()

    def test_cli_start_failure_does_not_use_the_live_cloud_singleton(self):
        self.acquire.side_effect = acp_client._acquire_acp_client
        with patch.object(
            acp_client.ACPClient, "start", side_effect=RuntimeError("Synthetic CLI failure")
        ):
            status, response = self.chat(
                si_runtime="copilot", model_policy_mode="auto-balanced"
            )
        self.assertEqual(status, 503)
        self.assertIn("Synthetic CLI failure", response["error"]["message"])
        self.assertIs(state.acp_client, self.cloud)
        self.post.assert_not_called()
        self.cloud.prompt.assert_not_called()

    def test_provider_error_is_returned_without_direct_or_cloud_retry(self):
        self.client.prompt.return_value = {"error": "Synthetic SI endpoint failure"}
        status, response = self.chat(
            si_runtime="copilot", model_policy_mode="auto-fast"
        )
        self.assertEqual(status, 502)
        self.assertIn("Synthetic SI endpoint failure", response["error"]["message"])
        self.client.prompt.assert_called_once()
        self.acquire.assert_called_once()
        self.post.assert_not_called()
        self.cloud.prompt.assert_not_called()


class SIClientIsolationTests(unittest.TestCase):
    def test_provider_validation_accepts_local_model_ids_but_rejects_invalid_ids(self):
        provider = core._si_provider_config(BASE_URL + "/", MODEL, 1024)
        self.assertEqual(provider["COPILOT_PROVIDER_BASE_URL"], BASE_URL)
        self.assertEqual(provider["COPILOT_PROVIDER_WIRE_MODEL"], MODEL)
        self.assertEqual(provider["COPILOT_PROVIDER_MAX_OUTPUT_TOKENS"], "1024")
        for model in ("", "   ", "a" * 201, "synthetic\x7fmodel"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                core._si_provider_config(BASE_URL, model)

    def test_client_start_isolates_provider_environment_profile_and_tool_free_mode(self):
        ambient = {
            "COPILOT_PROVIDER_BASE_URL": "https://example.com/v1",
            "COPILOT_PROVIDER_API_KEY": "sk-FAKE-AMBIENT",
            "COPILOT_PROVIDER_MAX_PROMPT_TOKENS": "1",
            "COPILOT_MODEL": "synthetic-cloud",
            "COPILOT_HOME": "/example/profile",
            "OPENAI_API_KEY": "sk-FAKE-OPENAI",
            "GH_TOKEN": "ghp_EXAMPLE",
            "EVA_BRIDGE_TOKEN": "FAKE-BRIDGE",
        }
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, ambient))
            stack.enter_context(patch.object(acp_client._cfg, "EVA_CONFIG_DIR", root))
            stack.enter_context(patch.object(acp_client, "_ARTIFACTS_DIR", root))
            stack.enter_context(patch.object(
                acp_client.ACPClient, "_validated_copilot_executable", return_value=sys.executable
            ))
            stack.enter_context(patch.object(acp_client.threading, "Thread"))
            stack.enter_context(patch.object(acp_client.ACPClient, "_send_request", side_effect=[
                {"agentInfo": {}}, {"sessionId": "synthetic-tools"},
                {"agentInfo": {}}, {"sessionId": "synthetic-no-tools"},
                {"agentInfo": {}}, {"sessionId": "synthetic-normal"},
            ]))
            spawn = stack.enter_context(patch.object(acp_client.subprocess, "Popen"))
            for no_tools in (False, True):
                with self.subTest(no_tools=no_tools):
                    client = acp_client.ACPClient(
                        model=MODEL, provider_config=core._si_provider_config(BASE_URL, MODEL),
                        no_tools=no_tools,
                    )
                    client.start()
                    command = spawn.call_args.args[0]
                    environment = spawn.call_args.kwargs["env"]
                    self.assertEqual(environment["COPILOT_PROVIDER_BASE_URL"], BASE_URL)
                    self.assertEqual(environment["COPILOT_MODEL"], MODEL)
                    self.assertEqual(environment["COPILOT_PROVIDER_WIRE_MODEL"], MODEL)
                    for key in ambient.keys() - {
                        "COPILOT_PROVIDER_BASE_URL", "COPILOT_MODEL", "COPILOT_HOME",
                    }:
                        self.assertNotIn(key, environment)
                    self.assertEqual(environment["COPILOT_HOME"], str(Path(root) / "copilot-si-runtime/home"))
                    self.assertEqual(spawn.call_args.kwargs["cwd"], str(Path(root) / "copilot-si-runtime/workspace"))
                    self.assertNotIn("--model", command)
                    self.assertIn("--no-custom-instructions", command)
                    self.assertIn("--disable-builtin-mcps", command)
                    self.assertEqual("--available-tools=__eva_no_tools__" in command, no_tools)
                    settings = json.loads((Path(environment["COPILOT_HOME"]) / "settings.json").read_text())
                    self.assertFalse(settings["autoUpdate"])
                    self.assertTrue(settings["disableAllHooks"])
                    self.assertTrue(settings["customAgents"]["defaultLocalOnly"])
                    self.assertEqual({key: os.environ[key] for key in ambient}, ambient)
            acp_client.ACPClient(model="synthetic-cloud").start()
            normal_environment = spawn.call_args.kwargs["env"]
            self.assertNotIn("COPILOT_PROVIDER_BASE_URL", normal_environment)
            self.assertNotIn("COPILOT_MODEL", normal_environment)

    def test_pool_separates_provider_model_and_no_tools_without_replacing_singleton(self):
        cloud = acp_client.ACPClient(model=MODEL)
        cloud.alive = True
        provider = core._si_provider_config(BASE_URL, MODEL)
        other_provider = core._si_provider_config("http://127.0.0.1:1234/v1", MODEL)
        with patch.multiple(
            state, acp_client=cloud, acp_pool={}, acp_pool_order=[], configured_mcp_config={}
        ), patch.object(
            acp_client.ACPClient, "start", autospec=True,
            side_effect=lambda client: setattr(client, "alive", True),
        ) as start, patch.object(acp_client, "_telemetry_emit"):
            selected = []
            for model, config, no_tools in (
                (MODEL, provider, False), (MODEL, provider, False),
                (MODEL, other_provider, False),
                ("synthetic/other:test", provider, False), (MODEL, provider, True),
            ):
                with acp_client._acquire_acp_client(
                    model, tool_profile="broad", provider_config=config, no_tools=no_tools
                ) as (client, detail):
                    self.assertIsNotNone(client, detail)
                    self.assertIs(state.acp_client, cloud)
                    self.assertEqual(client.active_requests, 1)
                    selected.append(client)
                self.assertEqual(client.active_requests, 0)
            self.assertEqual(start.call_count, 4)
            self.assertEqual(len({id(client) for client in selected}), 4)
            self.assertIs(selected[0], selected[1])
            self.assertIn("eva-web-search", selected[0].mcp_config)
            self.assertEqual(selected[-1].mcp_config, {})
            self.assertIs(state.acp_client, cloud)


if __name__ == "__main__":
    unittest.main(verbosity=2)
