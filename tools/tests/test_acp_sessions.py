"""Focused tests for warm ACP process and bounded conversation sessions."""

import os
import sys
import unittest
from unittest.mock import patch


TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(TOOLS_DIR)
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from bridge.acp_client import ACPClient


class FakeACPClient(ACPClient):
    """ACP client double that records protocol requests without a subprocess."""

    def __init__(self):
        super().__init__(model="test-model")
        self.alive = True
        self.process = object()
        self.session_id = "startup-session"
        self._remember_conversation_session("__default__", self.session_id)
        self.created_sessions = []
        self.prompt_sessions = []

    def _send_request(self, method, params, timeout=120):
        if method == "session/new":
            session_id = "session-" + str(len(self.created_sessions) + 1)
            self.created_sessions.append(session_id)
            return {"sessionId": session_id}
        if method == "session/prompt":
            self.prompt_sessions.append(params["sessionId"])
            return {"stopReason": "end_turn"}
        raise AssertionError("unexpected ACP method: " + method)


class ACPConversationSessionTests(unittest.TestCase):
    def test_warm_process_is_reused_and_conversations_are_isolated(self):
        client = FakeACPClient()
        process = client.process

        client.prompt("first", conversation_id="conversation-a")
        client.prompt("follow-up", conversation_id="conversation-a")
        client.prompt("other", conversation_id="conversation-b")

        self.assertIs(client.process, process)
        self.assertEqual(client.created_sessions, ["session-1", "session-2"])
        self.assertEqual(
            client.prompt_sessions,
            ["session-1", "session-1", "session-2"],
        )

    def test_prompt_limit_rotates_only_the_conversation_session(self):
        client = FakeACPClient()
        process = client.process
        with patch("bridge.acp_client._ACP_SESSION_MAX_PROMPTS", 2):
            client.prompt("one", conversation_id="conversation-a")
            client.prompt("two", conversation_id="conversation-a")
            client.prompt("three", conversation_id="conversation-a")
            client.prompt("other", conversation_id="conversation-b")

        self.assertEqual(
            client.prompt_sessions,
            ["session-1", "session-1", "session-2", "session-3"],
        )
        self.assertIs(client.process, process)

    def test_idle_limit_rotates_a_session_without_restarting_process(self):
        client = FakeACPClient()
        with patch("bridge.acp_client._ACP_SESSION_IDLE_SECONDS", 0):
            client.prompt("first", conversation_id="conversation-a")
            client.prompt("after idle", conversation_id="conversation-a")

        self.assertEqual(client.prompt_sessions, ["session-1", "session-2"])
        self.assertTrue(client.alive)

    def test_conversation_session_lru_is_bounded(self):
        client = FakeACPClient()
        with patch("bridge.acp_client._ACP_SESSION_MAX", 2):
            client.prompt("a", conversation_id="conversation-a")
            client.prompt("b", conversation_id="conversation-b")
            client.prompt("c", conversation_id="conversation-c")

        self.assertLessEqual(len(client._conversation_sessions), 2)
        self.assertNotIn("conversation-a", client._conversation_sessions)
        self.assertIn("conversation-b", client._conversation_sessions)
        self.assertIn("conversation-c", client._conversation_sessions)

    def test_plan_and_tool_updates_emit_sanitized_prompt_events(self):
        client = FakeACPClient()
        events = []
        client._active_prompts[1] = {
            "session_id": "session-a",
            "on_event": events.append,
        }

        client._handle_session_update({
            "sessionId": "session-a",
            "update": {
                "sessionUpdate": "plan",
                "entries": [{"content": "Inspect secrets and internal reasoning"}],
            },
        })
        client._handle_session_update({
            "sessionId": "session-a",
            "update": {
                "sessionUpdate": "tool_call_update",
                "kind": "read_file",
                "status": "running",
            },
        })

        self.assertEqual(events, [
            {"kind": "plan", "label": "Planning next steps"},
            {"kind": "tool", "label": "Using read file (running)"},
        ])

    def test_frontend_request_paths_emit_conversation_identity(self):
        def source(relative_path):
            with open(os.path.join(REPO_DIR, relative_path), encoding="utf-8") as handle:
                return handle.read()

        aig_source = source("core/js/aig.js")
        cognition_source = source("core/js/cognition.js")
        copilot_source = source("core/js/copilot.js")
        gpt_source = source("core/js/gpt-core.js")
        lm_source = source("core/js/lm-studio.js")

        self.assertIn("sessionId: sessionId", aig_source)
        self.assertIn("session_id: sessionId", aig_source)
        self.assertIn("session_id: sessionId", cognition_source)
        self.assertIn("session_id:", copilot_source)
        self.assertIn("session_id:", gpt_source)
        self.assertIn("session_id:", lm_source)
        self.assertIn("/v1/data/retrieve?message=", lm_source)
        self.assertIn("&session_id=", lm_source)


if __name__ == "__main__":
    unittest.main()
