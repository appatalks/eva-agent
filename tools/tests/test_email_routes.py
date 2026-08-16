#!/usr/bin/env python3
"""Contract: email bridge endpoints and the briefing mail source."""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import briefing, core, email_service

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
            ('"/v1/email/exim-status"', "_email_exim_status()"),
        ]:
            self.assertIn(route, self.source, route)
            self.assertIn(handler, self.source, handler)

    def test_write_routes_are_registered(self):
        for handler in [
            "_email_accounts_update()", "_email_account_upsert()", "_email_allowlist_update()",
            "_email_credential_set()", "_email_send_request()",
        ]:
            self.assertIn(handler, self.source, handler)

    def test_delete_route_is_registered(self):
        self.assertIn(r'/v1/email/messages/[^/]+', self.source)
        self.assertIn("_email_message_delete(", self.source)
        self.assertIn(r'/v1/email/accounts/[^/]+', self.source)
        self.assertIn("_email_account_delete(", self.source)

    def test_focused_mutations_do_not_require_full_document_replacement(self):
        self.assertIn('parsed_path == "/v1/email/account"', self.source)
        self.assertIn('parsed_path == "/v1/email/allowlist"', self.source)
        self.assertIn("email_service.upsert_account(data.get(\"account\"))", self.source)
        self.assertIn("email_service.update_allowlist(data.get(\"allowlist\"))", self.source)

    def test_validation_and_persistence_failures_have_distinct_statuses(self):
        self.assertIn("except email_service.EmailValidationError", self.source)
        self.assertIn("except email_service.EmailPersistenceError", self.source)
        self.assertIn("self._json_response(500", self.source)

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

    def test_exim_status_is_read_only_and_returns_503_when_access_is_unavailable(self):
        start = self.source.index("def _email_exim_status(self):")
        end = self.source.index("def _email_send_request(self):")
        block = self.source[start:end]
        self.assertIn("email_service.inspect_local_mta_status", block)
        self.assertIn("self._json_response(503", block)
        self.assertNotIn("subprocess", block)

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
        for decision in ("sent", "submitted", "partially_sent", "needs_confirmation"):
            self.assertIn(decision, success, decision)

    def test_credential_endpoint_does_not_return_the_secret(self):
        start = self.source.index("def _email_credential_set(self):")
        end = self.source.index("def _email_messages_list(self):")
        block = self.source[start:end]
        self.assertIn('{"status": "stored"}', block)
        self.assertNotIn('"credential":', block.split("data.get(\"credential\")")[-1])


class FocusedMutationHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.original_path = email_service._EMAIL_CONFIG_PATH
        cls.original_token = os.environ.get("EVA_BRIDGE_TOKEN")
        email_service._EMAIL_CONFIG_PATH = os.path.join(cls.directory.name, "email_accounts.json")
        email_service._credentials.clear()
        cls.token = "email-route-" + "test-token"
        os.environ["EVA_BRIDGE_TOKEN"] = cls.token
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), core.BridgeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        email_service._EMAIL_CONFIG_PATH = cls.original_path
        email_service._credentials.clear()
        if cls.original_token is None:
            os.environ.pop("EVA_BRIDGE_TOKEN", None)
        else:
            os.environ["EVA_BRIDGE_TOKEN"] = cls.original_token
        cls.directory.cleanup()

    def call(self, method, path, body=None):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Origin": self.base,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_focused_mutations_persist_and_invalid_edit_is_atomic(self):
        account = {
            "id": "eva", "label": "Eva local", "backend": "eva_direct",
            "address": "eva@lab.internal", "status": "connected",
            "allowlist": ["@localhost.localdomain"],
            "settings": {
                "direct_consent": ["@localhost.localdomain"],
                "delivery_mode": "internal",
                "internal_domains": ["localhost.localdomain"],
                "internal_smtp_host": "127.0.0.1",
                "internal_smtp_port": 25,
                "internal_smtp_starttls": False,
            },
        }
        self.assertEqual(self.call("POST", "/v1/email/account", {"account": account})[0], 200)
        self.assertEqual(self.call(
            "POST", "/v1/email/allowlist", {"allowlist": ["approved@example.com"]}
        )[0], 200)

        status, current = self.call("GET", "/v1/email/accounts")
        self.assertEqual(status, 200)
        self.assertEqual(current["accounts"][0]["settings"]["direct_consent"], ["@localhost.localdomain"])
        self.assertEqual(current["accounts"][0]["settings"]["internal_domains"], ["localhost.localdomain"])
        self.assertEqual(current["allowlist"], ["approved@example.com"])

        status, rejected = self.call("POST", "/v1/email/account", {"account": {"id": "eva"}})
        self.assertEqual(status, 400)
        self.assertIn("address", rejected["error"]["message"])
        _, unchanged = self.call("GET", "/v1/email/accounts")
        self.assertEqual(unchanged["accounts"][0]["address"], "eva@lab.internal")
        self.assertEqual(unchanged["allowlist"], ["approved@example.com"])

        status, invalid_credential = self.call(
            "POST", "/v1/email/credential", {"account_id": "", "credential": ""}
        )
        self.assertEqual(status, 400)
        self.assertIn("required", invalid_credential["error"]["message"])

        status, deleted = self.call("DELETE", "/v1/email/accounts/eva")
        self.assertEqual(status, 200)
        self.assertEqual(deleted["accounts"], [])
        self.assertEqual(deleted["allowlist"], ["approved@example.com"])

    def test_identifierless_opaque_record_survives_http_upsert_and_delete(self):
        opaque = {"opaque": {"keep": True}}
        email_service.save_config({"accounts": [opaque], "allowlist": []})
        account = {
            "id": "editable", "backend": "imap_smtp", "address": "user@example.com",
            "status": "connected",
        }
        self.assertEqual(self.call("POST", "/v1/email/account", {"account": account})[0], 200)
        self.assertEqual(self.call("DELETE", "/v1/email/accounts/editable")[0], 200)
        with open(email_service._EMAIL_CONFIG_PATH, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(raw["accounts"], [opaque])


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
