#!/usr/bin/env python3
"""Contract: remote MCP transport safety and protocol handling.

The HTTP transport is injected, so no network call is made.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import remote_mcp

ENDPOINT = "https://workiq.svc.cloud.microsoft/mcp"


class FakeTransport:
    """Records requests and replays queued (status, body, headers) responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, payload, headers, timeout):
        self.requests.append({"url": url, "payload": payload, "headers": headers})
        if not self.responses:
            return 200, self._json({"jsonrpc": "2.0", "id": payload.get("id"), "result": {}}), {}
        status, body, response_headers = self.responses.pop(0)
        if isinstance(body, dict):
            body = self._json(body)
        return status, body, response_headers

    @staticmethod
    def _json(document):
        return json.dumps(document).encode("utf-8")


def ok(result, request_id=1, headers=None):
    return 200, {"jsonrpc": "2.0", "id": request_id, "result": result}, headers or {"Content-Type": "application/json"}


def client(*responses, token="access-token"):
    transport = FakeTransport(*responses)
    instance = remote_mcp.RemoteMCPClient(ENDPOINT, lambda: token, transport=transport)
    return instance, transport


class ConstructionTests(unittest.TestCase):
    def test_refuses_a_non_https_endpoint(self):
        for endpoint in ["http://workiq.svc.cloud.microsoft/mcp", "ftp://x/y", "", "not a url"]:
            with self.assertRaises(remote_mcp.RemoteMCPError):
                remote_mcp.RemoteMCPClient(endpoint, lambda: "t")

    def test_requires_a_token_provider(self):
        with self.assertRaises(remote_mcp.RemoteMCPError):
            remote_mcp.RemoteMCPClient(ENDPOINT, None)

    def test_missing_token_raises_reauthorization(self):
        instance, _ = client(token="")
        with self.assertRaises(remote_mcp.RemoteMCPAuthRequired):
            instance.initialize()


class HandshakeTests(unittest.TestCase):
    def test_initialize_sends_protocol_version_and_notification(self):
        instance, transport = client(ok({"serverInfo": {"name": "workiq"}}, headers={"Mcp-Session-Id": "sess-1"}))
        info = instance.initialize()
        self.assertEqual(info["serverInfo"]["name"], "workiq")
        self.assertEqual(transport.requests[0]["payload"]["method"], "initialize")
        self.assertEqual(transport.requests[1]["payload"]["method"], "notifications/initialized")
        self.assertNotIn("id", transport.requests[1]["payload"])

    def test_handshake_runs_only_once(self):
        instance, transport = client(ok({}), ok({}), ok({"tools": []}), ok({"tools": []}))
        instance.list_tools()
        instance.list_tools()
        methods = [request["payload"]["method"] for request in transport.requests]
        self.assertEqual(methods.count("initialize"), 1)

    def test_session_id_is_echoed_on_later_requests(self):
        instance, transport = client(ok({}, headers={"Mcp-Session-Id": "sess-9"}), ok({}), ok({"tools": []}))
        instance.list_tools()
        self.assertEqual(transport.requests[-1]["headers"]["Mcp-Session-Id"], "sess-9")

    def test_bearer_token_is_sent(self):
        instance, transport = client(ok({}))
        instance.initialize()
        self.assertEqual(transport.requests[0]["headers"]["Authorization"], "Bearer access-token")


class AuthorizationSignalTests(unittest.TestCase):
    def test_401_raises_auth_required_with_metadata_url(self):
        metadata = "https://workiq.svc.cloud.microsoft/.well-known/oauth-protected-resource"
        instance, _ = client((401, b"denied", {"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'}))
        with self.assertRaises(remote_mcp.RemoteMCPAuthRequired) as caught:
            instance.initialize()
        self.assertEqual(caught.exception.resource_metadata_url, metadata)

    def test_401_body_is_not_leaked_into_the_error(self):
        instance, _ = client((401, b"token=super-secret-value", {}))
        with self.assertRaises(remote_mcp.RemoteMCPAuthRequired) as caught:
            instance.initialize()
        self.assertNotIn("super-secret-value", str(caught.exception))

    def test_403_is_reported_as_a_policy_refusal(self):
        instance, _ = client((403, b"blocked", {}))
        with self.assertRaises(remote_mcp.RemoteMCPError) as caught:
            instance.initialize()
        self.assertIn("tenant policy", str(caught.exception))

    def test_expired_session_clears_state_for_reconnect(self):
        instance, _ = client(ok({}, headers={"Mcp-Session-Id": "sess-1"}), ok({}), (404, b"", {}))
        instance.initialize()
        with self.assertRaises(remote_mcp.RemoteMCPError):
            instance.list_tools()
        self.assertFalse(instance._initialized)


class ResponseDecodingTests(unittest.TestCase):
    def test_parses_event_stream_responses(self):
        stream = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"fetch"}]}}\n\n'
        instance, _ = client(
            (200, stream, {"Content-Type": "text/event-stream"}),
            ok({}),
            (200, b'data: {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"fetch"}]}}\n',
             {"Content-Type": "text/event-stream"}),
        )
        tools = instance.list_tools()
        self.assertEqual(tools[0]["name"], "fetch")

    def test_stream_without_json_raises(self):
        with self.assertRaises(remote_mcp.RemoteMCPError):
            remote_mcp.parse_sse_payload("event: ping\n\n")

    def test_invalid_json_body_raises(self):
        instance, _ = client((200, b"<html>not json</html>", {"Content-Type": "application/json"}))
        with self.assertRaises(remote_mcp.RemoteMCPError):
            instance.initialize()

    def test_jsonrpc_error_is_surfaced(self):
        instance, _ = client((200, {"jsonrpc": "2.0", "id": 1, "error": {"message": "bad request"}}, {}))
        with self.assertRaises(remote_mcp.RemoteMCPError) as caught:
            instance.initialize()
        self.assertIn("bad request", str(caught.exception))


class ToolCallTests(unittest.TestCase):
    def test_call_tool_sends_name_and_arguments(self):
        instance, transport = client(ok({}), ok({}), ok({"content": [{"type": "text", "text": "done"}]}))
        instance.call_tool("fetch", {"path": "/me/messages"})
        payload = transport.requests[-1]["payload"]
        self.assertEqual(payload["method"], "tools/call")
        self.assertEqual(payload["params"]["name"], "fetch")
        self.assertEqual(payload["params"]["arguments"]["path"], "/me/messages")

    def test_tool_error_flag_raises(self):
        instance, _ = client(ok({}), ok({}), ok({"isError": True, "content": [{"type": "text", "text": "denied"}]}))
        with self.assertRaises(remote_mcp.RemoteMCPError) as caught:
            instance.call_tool("do_action", {"path": "/me/sendMail"})
        self.assertIn("denied", str(caught.exception))

    def test_tool_result_text_flattens_content(self):
        text = remote_mcp.tool_result_text({"content": [
            {"type": "text", "text": "first"},
            {"type": "resource", "resource": {"text": "second"}},
            {"type": "image"},
        ]})
        self.assertEqual(text, "first\nsecond")

    def test_tool_result_text_tolerates_unexpected_shapes(self):
        self.assertEqual(remote_mcp.tool_result_text(None), "")
        self.assertEqual(remote_mcp.tool_result_text({"content": "nope"}), "")


class LifecycleTests(unittest.TestCase):
    def test_close_forces_a_new_handshake(self):
        instance, transport = client(ok({}), ok({}), ok({"tools": []}), ok({}), ok({}), ok({"tools": []}))
        instance.list_tools()
        instance.close()
        instance.list_tools()
        methods = [request["payload"]["method"] for request in transport.requests]
        self.assertEqual(methods.count("initialize"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
