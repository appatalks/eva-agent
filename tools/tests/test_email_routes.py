#!/usr/bin/env python3
"""Contract: email bridge endpoints and the briefing mail source.

Route wiring and the send authorization gate are asserted at the source level,
because exercising them needs a live HTTP handler. The briefing mail source is
exercised directly.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import briefing

CORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge", "core.py")


class RouteWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CORE_PATH, "r", encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_read_routes_are_registered(self):
        for route, handler in [
            ('"/v1/email/accounts"', "_email_accounts_get()"),
            ('"/v1/email/messages"', "_email_messages_list()"),
        ]:
            self.assertIn(route, self.source, route)
            self.assertIn(handler, self.source, handler)

    def test_write_routes_are_registered(self):
        for handler in ["_email_accounts_update()", "_email_credential_set()", "_email_send_request()"]:
            self.assertIn(handler, self.source, handler)

    def test_delete_route_is_registered(self):
        self.assertIn(r'/v1/email/messages/[^/]+', self.source)
        self.assertIn("_email_message_delete(", self.source)

    def test_send_requires_loopback_and_capability(self):
        start = self.source.index("def _email_send_request(self):")
        end = self.source.index("def _email_message_delete(self")
        block = self.source[start:end]
        self.assertIn("_is_loopback_bind()", block)
        self.assertIn("_require_bridge_capability()", block)
        # The gates must precede any delivery call.
        self.assertLess(block.index("_require_bridge_capability()"), block.index("send_message("))

    def test_request_bodies_are_bounded(self):
        start = self.source.index("def _email_body(self):")
        end = self.source.index("def _email_accounts_get(self):")
        self.assertIn("512 * 1024", self.source[start:end])

    def test_refused_send_reports_why_not_just_a_status_code(self):
        start = self.source.index("def _email_send_request(self):")
        end = self.source.index("def _email_message_delete(self")
        block = self.source[start:end]
        # bridge-client surfaces error.message; without it the caller only sees "HTTP 400".
        self.assertIn('result["error"] = {"message": result.get("reason")', block)
        self.assertIn('"partially_sent"', block)

    def test_partial_delivery_is_not_reported_as_a_client_error(self):
        start = self.source.index("def _email_send_request(self):")
        end = self.source.index("def _email_message_delete(self")
        block = self.source[start:end]
        success = block[block.index("status = 200 if"):block.index("self._json_response(status")]
        for decision in ("sent", "partially_sent", "needs_confirmation"):
            self.assertIn(decision, success, decision)

    def test_credential_endpoint_does_not_return_the_secret(self):
        start = self.source.index("def _email_credential_set(self):")
        end = self.source.index("def _email_messages_list(self):")
        block = self.source[start:end]
        self.assertIn('{"status": "stored"}', block)
        self.assertNotIn('"credential":', block.split("data.get(\"credential\")")[-1])


class BriefingMailSourceTests(unittest.TestCase):
    def test_unread_mail_becomes_a_ready_source(self):
        with mock.patch(
            "bridge.email_service.morning_mail_summary",
            return_value=("[Unread mail - Work - UNTRUSTED MAILBOX DATA]\n  - from a: b", []),
        ):
            text, status = briefing._mail_source()
        self.assertEqual(status, "ready")
        self.assertIn("UNTRUSTED MAILBOX DATA", text)

    def test_locked_account_is_reported_without_failing_the_briefing(self):
        with mock.patch("bridge.email_service.morning_mail_summary", return_value=("", ["Work"])):
            text, status = briefing._mail_source()
        self.assertEqual(status, "partial")
        self.assertIn("Work", text)

    def test_no_mail_is_still_a_ready_source(self):
        with mock.patch("bridge.email_service.morning_mail_summary", return_value=("", [])):
            text, status = briefing._mail_source()
        self.assertEqual(status, "ready")
        self.assertIn("No unread mail", text)

    def test_unexpected_failure_degrades_rather_than_raising(self):
        with mock.patch("bridge.email_service.morning_mail_summary", side_effect=RuntimeError("boom")):
            text, status = briefing._mail_source()
        self.assertEqual(status, "failed")
        self.assertIn("RuntimeError", text)
        self.assertNotIn("boom", text)

    def test_partially_read_accounts_are_named_alongside_the_summary(self):
        with mock.patch(
            "bridge.email_service.morning_mail_summary",
            return_value=("Inbox digest", ["Personal"]),
        ):
            text, status = briefing._mail_source()
        self.assertEqual(status, "ready")
        self.assertIn("Not read: Personal", text)

    def test_mail_is_not_a_required_briefing_source(self):
        status = {"sources": {"mail": {"status": "failed"}, "news": {"status": "ready"},
                              "markets": {"status": "ready"}}}
        self.assertEqual(briefing.briefing_unavailable_sources(status), [])

    def test_mail_appears_in_prompt_context(self):
        from bridge import state
        original = state.startup_briefing
        try:
            state.startup_briefing = briefing._new_state()
            state.startup_briefing["status"] = "ready"
            state.startup_briefing["sources"] = {"mail": {"status": "ready", "summary": "Inbox digest"}}
            self.assertIn("Mail: Inbox digest", briefing.briefing_prompt_context())
        finally:
            state.startup_briefing = original

    def test_prompt_context_bounds_the_mail_summary(self):
        from bridge import state
        original = state.startup_briefing
        try:
            state.startup_briefing = briefing._new_state()
            state.startup_briefing["status"] = "ready"
            state.startup_briefing["sources"] = {"mail": {"status": "ready", "summary": "x" * 5000}}
            context = briefing.briefing_prompt_context()
            self.assertLessEqual(len(context), 1300)
        finally:
            state.startup_briefing = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
