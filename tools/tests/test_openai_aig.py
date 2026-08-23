#!/usr/bin/env python3
"""End-to-end coverage for Eva AIG with a direct OpenAI responder."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = Path(os.environ.get("EVA_TEST_APP_ROOT", ROOT)).resolve()
sys.path.insert(0, str(ROOT / "tools"))
from bridge.core import _missing_tool_result_message, _parse_aig_backend


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size).decode("utf-8"))
        self.__class__.requests.append({
            "authorization": self.headers.get("Authorization", ""),
            "payload": payload,
        })
        system_prompt = (payload.get("messages") or [{}])[0].get("content", "")
        if "terminal applicability classifier" in system_prompt:
            content = '{"applicable":false,"command":""}'
        elif "terminal command planner" in system_prompt:
            content = '{"command":"git status"}'
        else:
            content = "Direct Eva response. [[EVA_LOOK]]{\"question\":\"what is visible?\"}[[/EVA_LOOK]]"
        finish_reason = "length" if payload.get("messages", [{}])[-1].get("content") == "Force token limit." else "stop"
        if payload.get("stream") is True:
            chunks = ["Direct Eva response. ", "[[EVA_LOOK]]{\"question\":", "\"what is visible?\"}[[/EVA_LOOK]]"]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for chunk in chunks:
                event = {"choices": [{"delta": {"content": chunk}}]}
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                if payload.get("messages", [{}])[-1].get("content") == "Trigger stream error.":
                    error = {"error": {"message": "synthetic stream failure"}}
                    self.wfile.write(("data: " + json.dumps(error) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    return
                threading.Event().wait(0.02)
            if finish_reason == "length":
                event = {"choices": [{"delta": {}, "finish_reason": "length"}]}
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        body = json.dumps({
            "id": "chatcmpl-test",
            "model": payload.get("model"),
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _json_request(url, payload=None, timeout=5):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _ndjson_request(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return [json.loads(line) for line in response if line.strip()]


class OpenAIAIGEndToEndTests(unittest.TestCase):
    def test_legacy_aig_backends_use_acp_not_github_models(self):
        self.assertEqual(_parse_aig_backend("gpt-5.6-luna"), ("acp", "gpt-5.6-luna"))
        self.assertEqual(_parse_aig_backend("claude-sonnet-4.6"), ("acp", "claude-sonnet-4.6"))
        self.assertEqual(_parse_aig_backend("openai:gpt-5-mini"), ("openai", "gpt-5-mini"))

    @classmethod
    def setUpClass(cls):
        _FakeOpenAIHandler.requests = []
        cls.openai_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIHandler)
        cls.openai_thread = threading.Thread(target=cls.openai_server.serve_forever, daemon=True)
        cls.openai_thread.start()

        cls.temp_dir = tempfile.TemporaryDirectory(prefix="eva-openai-aig-")
        cls.bridge_port = _free_port()
        cls.bridge_url = f"http://127.0.0.1:{cls.bridge_port}"
        env = os.environ.copy()
        env.update({
            "EVA_CONFIG_DIR": cls.temp_dir.name,
            "EVA_MEMORY_BACKEND": "sqlite",
            "EVA_MEMORY_DB": os.path.join(cls.temp_dir.name, "memory.db"),
            "EVA_OPENAI_CHAT_COMPLETIONS_URL": (
                f"http://127.0.0.1:{cls.openai_server.server_port}/v1/chat/completions"
            ),
            "PYTHONUNBUFFERED": "1",
        })
        cls.bridge = subprocess.Popen(
            [
                sys.executable,
                str(APP_ROOT / "tools" / "acp_bridge.py"),
                "--bind", "127.0.0.1",
                "--port", str(cls.bridge_port),
                "--copilot-path", "eva-test-copilot-does-not-exist",
                "--cwd", str(APP_ROOT),
            ],
            cwd=APP_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        deadline = time.monotonic() + 15
        last_error = None
        while time.monotonic() < deadline:
            if cls.bridge.poll() is not None:
                output = cls.bridge.stdout.read() if cls.bridge.stdout else ""
                raise RuntimeError(f"bridge exited before readiness:\n{output}")
            try:
                status, health = _json_request(cls.bridge_url + "/health")
                if status == 200 and health.get("status") == "ok":
                    break
            except (OSError, urllib.error.URLError) as error:
                last_error = error
            threading.Event().wait(0.05)
        else:
            raise RuntimeError(f"bridge did not become ready: {last_error}")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "bridge", None):
            cls.bridge.terminate()
            try:
                cls.bridge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.bridge.kill()
                cls.bridge.wait(timeout=5)
        if getattr(cls, "openai_server", None):
            cls.openai_server.shutdown()
            cls.openai_server.server_close()
        if getattr(cls, "openai_thread", None):
            cls.openai_thread.join(timeout=2)
        if getattr(cls, "temp_dir", None):
            cls.temp_dir.cleanup()

    def setUp(self):
        _FakeOpenAIHandler.requests = []

    def test_direct_openai_preserves_eva_contract_without_copilot(self):
        status, response = _json_request(self.bridge_url + "/v1/aig/chat", {
            "messages": [{"role": "user", "content": "Look at what is in front of me."}],
            "user_message": "Look at what is in front of me.",
            "model": "openai:gpt-5",
            "openai_api_key": "sk-FAKE-OPENAI-E2E",
            "acp_reasoning_effort": "high",
            "internal": True,
            "no_tools": True,
        })

        self.assertEqual(status, 200)
        self.assertEqual(response["model"], "aig:gpt-5+openai-direct")
        content = response["choices"][0]["message"]["content"]
        self.assertIn("[[EVA_LOOK]]", content)
        self.assertEqual(len(_FakeOpenAIHandler.requests), 1)

        captured = _FakeOpenAIHandler.requests[0]
        self.assertEqual(captured["authorization"], "Bearer sk-FAKE-OPENAI-E2E")
        payload = captured["payload"]
        self.assertEqual(payload["model"], "gpt-5")
        self.assertEqual(payload["max_completion_tokens"], 16384)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["messages"][-1]["content"], "Look at what is in front of me.")
        system_prompt = payload["messages"][0]["content"]
        self.assertIn("You are Eva, a personal AI assistant with persistent memory.", system_prompt)
        self.assertIn("Routing path: OpenAI API (direct)", system_prompt)
        self.assertIn("[[EVA_LOOK]]", system_prompt)

    def test_direct_openai_requires_a_key(self):
        request = urllib.request.Request(
            self.bridge_url + "/v1/aig/chat",
            data=json.dumps({
                "user_message": "hello",
                "model": "openai:gpt-5",
                "internal": True,
                "no_tools": True,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 400)
        body = json.loads(raised.exception.read().decode("utf-8"))
        self.assertIn("OpenAI API key", body["error"]["message"])

    def test_aig_automatic_policy_selects_available_responder(self):
        _FakeOpenAIHandler.requests = []
        status, response = _json_request(self.bridge_url + "/v1/aig/chat", {
            "user_message": "Give me a concise status sentence.",
            "messages": [{"role": "user", "content": "Give me a concise status sentence."}],
            "model": "gpt-5.6-luna",
            "model_policy_mode": "auto-balanced",
            "openai_api_key": "sk-FAKE-OPENAI-E2E",
        })
        self.assertEqual(status, 200)
        self.assertEqual(response["model"], "aig:gpt-5.6-luna+openai-direct")
        self.assertEqual(len(_FakeOpenAIHandler.requests), 1)

    def test_aig_automatic_policy_requires_tools_for_live_data(self):
        request = urllib.request.Request(
            self.bridge_url + "/v1/aig/chat",
            data=json.dumps({
                "user_message": "Search the web for today's headlines.",
                "messages": [{"role": "user", "content": "Search the web for today's headlines."}],
                "model": "openai:gpt-5.6-luna",
                "model_policy_mode": "auto-balanced",
                "openai_api_key": "sk-FAKE-OPENAI-E2E",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 503)
        self.assertIn("tool-capable route", raised.exception.read().decode("utf-8"))

    def test_aig_pinned_policy_still_fails_closed_for_live_data(self):
        request = urllib.request.Request(
            self.bridge_url + "/v1/aig/chat",
            data=json.dumps({
                "user_message": "Search the web for today's headlines.",
                "messages": [{"role": "user", "content": "Search the web for today's headlines."}],
                "model": "openai:gpt-5.6-luna",
                "model_policy_mode": "pinned",
                "openai_api_key": "sk-FAKE-OPENAI-E2E",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 503)
        self.assertIn("live tools", raised.exception.read().decode("utf-8"))

    def test_aig_direct_openai_preserves_image_attachment(self):
        _FakeOpenAIHandler.requests = []
        image = "iVBORw0KGgo="
        status, response = _json_request(self.bridge_url + "/v1/aig/chat", {
            "user_message": "What is in this image?",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "What is in this image?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}},
            ]}],
            "model": "openai:gpt-5.6-luna",
            "model_policy_mode": "pinned",
            "image_b64": image,
            "image_mime": "image/png",
            "openai_api_key": "sk-FAKE-OPENAI-E2E",
        })
        self.assertEqual(status, 200)
        self.assertEqual(response["model"], "aig:gpt-5.6-luna+openai-direct")
        sent_messages = _FakeOpenAIHandler.requests[0]["payload"]["messages"]
        self.assertTrue(any(isinstance(message.get("content"), list) for message in sent_messages))

    def test_aig_local_tool_failure_is_explicit(self):
        self.assertIn("LocalMCP", _missing_tool_result_message(True))
        self.assertNotIn("LocalMCP", _missing_tool_result_message(False))

    def test_aig_automatic_policy_prefers_deep_model_for_analysis(self):
        _FakeOpenAIHandler.requests = []
        status, response = _json_request(self.bridge_url + "/v1/aig/chat", {
            "user_message": "Analyze the security tradeoffs in this design.",
            "messages": [{"role": "user", "content": "Analyze the security tradeoffs in this design."}],
            "model": "gpt-5.6-luna",
            "model_policy_mode": "auto-balanced",
            "openai_api_key": "sk-FAKE-OPENAI-E2E",
        })
        self.assertEqual(status, 200)
        self.assertEqual(response["model"], "aig:gpt-5.6-sol+openai-direct")

    def test_terminal_planner_uses_one_tool_free_direct_response(self):
        _FakeOpenAIHandler.requests = []
        status, response = _json_request(self.bridge_url + "/v1/aig/chat", {
            "user_message": "show the current git status",
            "messages": [{"role": "user", "content": "show the current git status"}],
            "model": "openai:gpt-5",
            "openai_api_key": "sk-FAKE-OPENAI-E2E",
            "native_terminal_plan": True,
            "internal": True,
            "no_tools": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(len(_FakeOpenAIHandler.requests), 1)
        self.assertEqual(response["choices"][0]["message"]["content"], '{"command":"git status"}')
        system_prompt = _FakeOpenAIHandler.requests[0]["payload"]["messages"][0]["content"]
        self.assertIn("terminal command planner", system_prompt)
        self.assertIn("Do not execute tools", system_prompt)

    def test_terminal_candidate_can_decline_without_tools(self):
        _FakeOpenAIHandler.requests = []
        status, response = _json_request(self.bridge_url + "/v1/aig/chat", {
            "user_message": "What is your favorite color?",
            "messages": [{"role": "user", "content": "What is your favorite color?"}],
            "model": "openai:gpt-5",
            "openai_api_key": "sk-FAKE-OPENAI-E2E",
            "native_terminal_candidate": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(len(_FakeOpenAIHandler.requests), 1)
        self.assertEqual(response["choices"][0]["message"]["content"], '{"applicable":false,"command":""}')
        system_prompt = _FakeOpenAIHandler.requests[0]["payload"]["messages"][0]["content"]
        self.assertIn("terminal applicability classifier", system_prompt)
        self.assertIn("Do not execute tools", system_prompt)

    def test_briefing_uses_one_responder_without_acp_preflight(self):
        _FakeOpenAIHandler.requests = []
        status, response = _json_request(self.bridge_url + "/v1/aig/chat", {
            "user_message": "Please give me my morning briefing.",
            "model": "openai:gpt-5",
            "openai_api_key": "sk-FAKE-OPENAI-E2E",
        })
        self.assertEqual(status, 200)
        self.assertEqual(response["model"], "aig:gpt-5+openai-direct")
        self.assertEqual(len(_FakeOpenAIHandler.requests), 1)
        system_prompt = _FakeOpenAIHandler.requests[0]["payload"]["messages"][0]["content"]
        self.assertIn("[Morning Briefing Preparation]", system_prompt)
        self.assertIn("Do not call tools or start searches", system_prompt)

    def test_acp_unavailable_closes_audit_as_failed(self):
        request = urllib.request.Request(
            self.bridge_url + "/v1/aig/chat",
            data=json.dumps({
                "user_message": "Use the unavailable ACP responder.",
                "model": "gpt-5.6-luna",
                "internal": True,
                "no_tools": True,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 503)
        audit_path = Path(self.temp_dir.name) / "audit.jsonl"
        records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(any(record.get("event") == "turn.response" and record.get("outcome") == "failed" and record.get("provider") == "acp" for record in records))

    def test_renderer_audit_rejects_freeform_reason_text(self):
        request = urllib.request.Request(
            self.bridge_url + "/v1/audit/event",
            data=json.dumps({
                "event": "native_action",
                "outcome": "failed",
                "correlation_id": "turn-audit-reason",
                "action": "run_terminal_command",
                "reason": "git status && disclose objective",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(raised.exception.code, 400)
        body = json.loads(raised.exception.read().decode("utf-8"))
        self.assertIn("Unsupported audit reason", body["error"]["message"])

    def test_direct_openai_streams_chunks_before_done(self):
        events = _ndjson_request(self.bridge_url + "/v1/aig/chat", {
            "messages": [{"role": "user", "content": "Look now."}],
            "user_message": "Look now.",
            "model": "openai:gpt-5",
            "openai_api_key": "sk-FAKE-OPENAI-E2E",
            "internal": True,
            "no_tools": True,
            "stream": True,
        })

        self.assertEqual([event["type"] for event in events], ["status", "chunk", "chunk", "chunk", "done"])
        self.assertEqual(events[0]["text"], "Eva is preparing context...")
        self.assertEqual("".join(event["text"] for event in events[1:-1]), events[-1]["response"]["choices"][0]["message"]["content"])
        self.assertEqual(events[-1]["response"]["model"], "aig:gpt-5+openai-direct")
        self.assertTrue(_FakeOpenAIHandler.requests[0]["payload"]["stream"])

    def test_reasoning_effort_is_model_specific(self):
        cases = [
            ("openai:gpt-5.6-luna", "none", "none"),
            ("openai:gpt-5.6-terra", "xhigh", "xhigh"),
            ("openai:gpt-5.6-sol", "max", "max"),
            ("openai:gpt-4.1-nano", "high", None),
            ("openai:gpt-5", "minimal", "minimal"),
            ("openai:o3", "minimal", None),
            ("openai:o3", "high", "high"),
            ("openai:gpt-4o", "high", None),
        ]
        for backend, requested_effort, expected_effort in cases:
            _FakeOpenAIHandler.requests = []
            status, _ = _json_request(self.bridge_url + "/v1/aig/chat", {
                "user_message": "Reasoning matrix test.",
                "model": backend,
                "openai_api_key": "sk-FAKE-OPENAI-E2E",
                "acp_reasoning_effort": requested_effort,
                "internal": True,
                "no_tools": True,
            })
            self.assertEqual(status, 200)
            payload = _FakeOpenAIHandler.requests[0]["payload"]
            self.assertEqual(payload.get("reasoning_effort"), expected_effort, backend)

    def test_partial_openai_stream_ends_with_error(self):
        events = _ndjson_request(self.bridge_url + "/v1/aig/chat", {
            "user_message": "Trigger stream error.",
            "model": "openai:gpt-5",
            "openai_api_key": "sk-FAKE-OPENAI-E2E",
            "internal": True,
            "no_tools": True,
            "stream": True,
        })

        self.assertEqual([event["type"] for event in events], ["status", "chunk", "error"])
        self.assertEqual(events[0]["text"], "Eva is preparing context...")
        self.assertEqual(events[-1]["status"], 502)
        self.assertIn("synthetic stream failure", events[-1]["message"])
        audit_path = Path(self.temp_dir.name) / "audit.jsonl"
        records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(any(record.get("event") == "turn.response" and record.get("outcome") == "failed" for record in records))

    def test_configured_token_limit_and_truncation_are_preserved(self):
        status, response = _json_request(self.bridge_url + "/v1/aig/chat", {
            "user_message": "Force token limit.",
            "model": "openai:gpt-5.6-luna",
            "openai_api_key": "sk-FAKE-OPENAI-E2E",
            "max_completion_tokens": 32768,
            "internal": True,
            "no_tools": True,
        })

        self.assertEqual(status, 200)
        self.assertEqual(_FakeOpenAIHandler.requests[0]["payload"]["max_completion_tokens"], 32768)
        self.assertEqual(response["choices"][0]["finish_reason"], "length")

    def test_out_of_range_token_limit_is_rejected(self):
        for invalid_value, expected_message in (
            (128001, "between 1 and 128000"),
            (1.9, "must be an integer"),
            (100.0, "must be an integer"),
            ("1e2", "must be an integer"),
        ):
            request = urllib.request.Request(
                self.bridge_url + "/v1/aig/chat",
                data=json.dumps({
                    "user_message": "hello",
                    "model": "openai:gpt-5.6-luna",
                    "openai_api_key": "sk-FAKE-OPENAI-E2E",
                    "max_completion_tokens": invalid_value,
                    "internal": True,
                    "no_tools": True,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(raised.exception.code, 400)
            body = json.loads(raised.exception.read().decode("utf-8"))
            self.assertIn(expected_message, body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
