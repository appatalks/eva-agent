#!/usr/bin/env python3
"""Contract: mailbox tool calls always require a live ACP permission decision.

Reading a mailbox exposes more than an ordinary file read, and sending is
irreversible and reaches third parties. Neither may be granted by standing
consent, by the read-only execute shortcut, or by workspace autonomy.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import acp_client


class MailDetectionTests(unittest.TestCase):
    def test_detects_graph_mail_paths(self):
        for path in [
            "/me/messages",
            "/me/messages/AAMk123",
            "/me/mailFolders/inbox/messages",
            "/me/sendMail",
            "/users/someone@example.com/messages",
        ]:
            self.assertTrue(
                acp_client._mail_scoped_tool_call({"rawInput": {"path": path}}),
                path,
            )

    def test_detects_mail_tool_names(self):
        for name in ["send_email", "sendMail", "read_mail", "list_messages", "imap_fetch", "smtp_send"]:
            self.assertTrue(acp_client._mail_scoped_tool_call({"title": name}), name)

    def test_ignores_unrelated_tool_calls(self):
        for tool_call in [
            {"title": "read_file", "rawInput": {"path": "/home/user/notes.txt"}},
            {"title": "web_search", "rawInput": {"query": "weather today"}},
            {"rawInput": {"path": "/me/events"}},
            {"rawInput": {"path": "/me/drive/root/children"}},
            {},
            None,
        ]:
            self.assertFalse(acp_client._mail_scoped_tool_call(tool_call), repr(tool_call))

    def test_tolerates_unserializable_raw_input(self):
        self.assertFalse(acp_client._mail_scoped_tool_call({"rawInput": {"blob": {1, 2}}}))


class StandingConsentTests(unittest.TestCase):
    """The routine-tools shortcut must not cover mailbox reads."""

    def _routine_allowed(self, tool_kind, tool_call, consent=True):
        mail_scoped = acp_client._mail_scoped_tool_call(tool_call)
        return bool(consent) and tool_kind in {"read", "search", "fetch", "think"} and not mail_scoped

    def test_ordinary_fetch_still_rides_standing_consent(self):
        self.assertTrue(self._routine_allowed("fetch", {"rawInput": {"path": "/me/events"}}))

    def test_mailbox_fetch_never_rides_standing_consent(self):
        self.assertFalse(self._routine_allowed("fetch", {"rawInput": {"path": "/me/messages"}}))

    def test_mailbox_search_never_rides_standing_consent(self):
        self.assertFalse(self._routine_allowed("search", {"title": "search_messages"}))


class SourceGuardTests(unittest.TestCase):
    """Guard the wiring, since the decision branches need a live ACP session."""

    def setUp(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge", "acp_client.py")
        with open(path, "r", encoding="utf-8") as handle:
            self.source = handle.read()

    def test_standing_consent_branch_excludes_mail(self):
        self.assertIn('"read", "search", "fetch", "think"\n        } and not mail_scoped', self.source)

    def test_read_execute_shortcut_excludes_mail(self):
        self.assertIn("and not mail_scoped and _workspace_read_only_execute", self.source)

    def test_workspace_autonomy_blocks_mail(self):
        self.assertIn('block_reason = "mailbox_access"', self.source)

    def test_detection_runs_before_any_auto_approval(self):
        detection = self.source.index("mail_scoped = _mail_scoped_tool_call(tool_call)")
        for marker in [
            "and not mail_scoped and _workspace_read_only_execute",
            'block_reason = "mailbox_access"',
            "} and not mail_scoped",
        ]:
            self.assertLess(detection, self.source.index(marker), marker)


if __name__ == "__main__":
    unittest.main(verbosity=2)
