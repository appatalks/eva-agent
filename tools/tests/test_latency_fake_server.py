#!/usr/bin/env python3
"""Fake-server tests for the production-shaped latency harness."""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import test_latency


class _FakeHandler(BaseHTTPRequestHandler):
    requests = []
    review_verdict = "APPROVE"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path.startswith("/v1/telemetry"):
            body = json.dumps({"summary": {"stream_turn": {"n": len(self.requests)}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(size).decode("utf-8"))
        self.__class__.requests.append(payload)
        stage = payload.get("latency_stage")
        if stage == "review":
            content = "VERDICT: " + self.__class__.review_verdict
        elif stage == "revision":
            content = "Revised final answer."
        elif stage == "draft":
            content = "Draft answer."
        else:
            content = "Fast answer."
        response = {
            "model": payload.get("model"),
            "choices": [{"message": {"content": content}}],
            "metrics": {"component_ms": {"route": 1.2}, "output_tokens": 3},
        }
        events = [
            {"type": "chunk", "text": content[:5]},
            {"type": "chunk", "text": content[5:]},
            {"type": "done", "response": response},
        ]
        encoded = b"".join((json.dumps(event).encode("utf-8") + b"\n") for event in events)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class LatencyHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _FakeHandler.requests = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        _FakeHandler.requests = []

    def test_fast_route_is_one_production_call_with_ndjson_metrics(self):
        result = test_latency.run_fast_pass(
            self.url, "eva-model", "2+2?", "session-fast", "fast/basic", 5, "cold"
        )
        self.assertEqual(len(_FakeHandler.requests), 1)
        request = _FakeHandler.requests[0]
        self.assertTrue(request["production"])
        self.assertEqual(request["session_id"], "session-fast")
        self.assertEqual(request["model"], "eva-model")
        call = result["calls"][0]
        self.assertEqual(call["chunk_count"], 2)
        self.assertIsNotNone(call["ttft_ms"])
        self.assertEqual(call["metrics"]["output_tokens"], 3)

    def test_approved_review_skips_revision(self):
        _FakeHandler.review_verdict = "APPROVE"
        result = test_latency.run_cognition_pass(
            self.url, "eva-model", "review-model", "hello", "session-approve", "general", 5, "warm"
        )
        self.assertEqual(len(_FakeHandler.requests), 2)
        self.assertEqual(result["verdict"], "APPROVE")
        self.assertEqual([request["latency_stage"] for request in _FakeHandler.requests], ["draft", "review"])
        self.assertTrue(_FakeHandler.requests[1]["no_tools"])

    def test_request_changes_adds_one_revision(self):
        _FakeHandler.review_verdict = "REQUEST_CHANGES"
        result = test_latency.run_cognition_pass(
            self.url, "eva-model", "review-model", "hello", "session-revise", "general", 5, "cold"
        )
        self.assertEqual(len(_FakeHandler.requests), 3)
        self.assertEqual(result["verdict"], "REQUEST_CHANGES")
        self.assertEqual([request["latency_stage"] for request in _FakeHandler.requests], ["draft", "review", "revision"])


if __name__ == "__main__":
    unittest.main()