#!/usr/bin/env python3
"""Focused contract checks for pure AIG request normalization."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from bridge.aig_request import normalize_aig_request


def parse_backend(value):
    value = str(value or "gpt-5.6-luna")
    if value.startswith("openai:"):
        return "openai", value.split(":", 1)[1]
    if value == "invalid":
        raise ValueError("Unsupported Eva backend model name")
    return "acp", value


def completion_limit(value):
    if value == "bad":
        raise ValueError("max_completion_tokens must be an integer")
    return 16384 if value in (None, "") else int(value)


class AigRequestContractTests(unittest.TestCase):
    def normalize(self, data, key=""):
        return normalize_aig_request(
            data,
            parse_backend=parse_backend,
            completion_token_limit=completion_limit,
            allowed_reasoning_efforts={"none", "low", "medium", "high"},
            openai_api_key=key,
        )

    def test_uses_last_user_message_and_bounds_session_id(self):
        result = self.normalize({
            "messages": [
                {"role": "user", "content": "older"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "latest"},
            ],
            "session_id": "s" * 140,
        })
        self.assertEqual(result["user_message"], "latest")
        self.assertEqual(result["conversation_id"], "s" * 120)
        self.assertFalse(result["internal"])
        self.assertFalse(result["no_tools"])
        self.assertEqual(result["max_completion_tokens"], 16384)

    def test_translation_and_terminal_planning_are_internal_and_tool_free(self):
        translation = self.normalize({"user_message": "translate", "translation_mode": True})
        self.assertTrue(translation["internal"])
        self.assertTrue(translation["no_tools"])
        terminal = self.normalize({"user_message": "status", "native_terminal_candidate": True})
        self.assertTrue(terminal["native_terminal_plan"])
        self.assertTrue(terminal["internal"])
        self.assertTrue(terminal["no_tools"])

    def test_direct_openai_requires_key(self):
        with self.assertRaisesRegex(ValueError, "OpenAI API key"):
            self.normalize({"user_message": "hello", "model": "openai:gpt-5"})
        result = self.normalize({"user_message": "hello", "model": "openai:gpt-5"}, key="sk-FAKE")
        self.assertEqual(result["responder_provider"], "openai")
        self.assertEqual(result["model_for_response"], "gpt-5")
        automatic = self.normalize({"user_message": "hello", "model": "openai:gpt-5", "model_policy_mode": "auto-balanced"})
        self.assertEqual(automatic["model_policy_mode"], "auto-balanced")

    def test_rejects_invalid_reasoning_tokens_and_missing_user_message(self):
        with self.assertRaisesRegex(ValueError, "Unsupported acp_reasoning_effort"):
            self.normalize({"user_message": "hello", "acp_reasoning_effort": "xhigh"})
        with self.assertRaisesRegex(ValueError, "max_completion_tokens"):
            self.normalize({"user_message": "hello", "max_completion_tokens": "bad"})
        with self.assertRaisesRegex(ValueError, "No user message"):
            self.normalize({"messages": [{"role": "assistant", "content": "only"}]})

    def test_normalizes_explicit_acp_auto_approval(self):
        self.assertTrue(self.normalize({"user_message": "fix the alerts", "acp_auto_approve": True})["acp_auto_approve"])
        self.assertFalse(self.normalize({"user_message": "fix the alerts"})["acp_auto_approve"])
        with self.assertRaisesRegex(ValueError, "acp_auto_approve"):
            self.normalize({"user_message": "fix the alerts", "acp_auto_approve": "true"})


if __name__ == "__main__":
    unittest.main(verbosity=2)