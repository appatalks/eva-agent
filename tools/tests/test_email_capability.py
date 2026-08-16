#!/usr/bin/env python3
"""Contract: Eva's email capability awareness is derived from live config.

The point is truthfulness. Eva must never assert an email ability she does not
currently have, and must never present a locked account as usable.
"""

import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import config as _cfg

CORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge", "core.py")


class CapabilityAwarenessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = os.path.join(self.directory.name, "email_accounts.json")
        patcher = mock.patch.object(_cfg, "EMAIL_CONFIG_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)

        from bridge import email_service as module
        importlib.reload(module)
        self.service = module
        self.service._EMAIL_CONFIG_PATH = path
        self.addCleanup(lambda: self.service._credentials.clear())

    def test_unconfigured_state_denies_the_ability(self):
        summary = self.service.capability_summary()
        self.assertIn("No email account is configured", summary)
        self.assertIn("cannot read or send email", summary)
        self.assertIn("Never claim a message was sent", summary)

    def test_configured_accounts_are_listed_with_capabilities(self):
        self.service.replace_accounts([
            {"id": "work", "label": "Work", "address": "me@custom.example", "status": "connected"},
        ], [])
        self.service.set_credential("work", "secret")
        summary = self.service.capability_summary()
        self.assertIn("Work <me@custom.example>", summary)
        self.assertIn("read/send/delete", summary)
        self.assertIn("connected", summary)

    def test_missing_credential_is_reported_as_locked(self):
        self.service.replace_accounts([
            {"id": "work", "label": "Work", "address": "me@custom.example", "status": "connected"},
        ], [])
        summary = self.service.capability_summary()
        self.assertIn("locked (needs sign-in)", summary)

    def test_account_awaiting_auth_is_not_presented_as_usable(self):
        self.service.replace_accounts([
            {"id": "work", "label": "Work", "address": "me@custom.example", "status": "needs_auth"},
        ], [])
        self.assertIn("needs_auth", self.service.capability_summary())

    def test_eva_direct_identity_is_described_as_such(self):
        self.service.replace_accounts([{
            "id": "eva", "label": "Eva direct", "backend": "eva_direct",
            "address": "eva@lab.internal", "status": "connected",
            "settings": {"direct_consent": ["@lab.internal"], "delivery_mode": "internal",
                         "internal_domains": ["lab.internal"],
                         "internal_smtp_host": "mail.lab.internal"},
        }], [])
        summary = self.service.capability_summary()
        self.assertIn("Eva's own identity", summary)
        self.assertIn("internal delivery", summary)
        self.assertNotIn("locked (needs sign-in)", summary)

    def test_summary_always_states_the_recipient_constraint(self):
        self.service.replace_accounts([
            {"id": "work", "label": "Work", "address": "me@custom.example", "status": "connected"},
        ], [])
        summary = self.service.capability_summary()
        self.assertIn("never choose a recipient on your own authority", summary.lower())
        self.assertIn("Never state that mail was sent", summary)

    def test_summary_is_bounded(self):
        self.service.replace_accounts([
            {"id": f"a{i}", "label": f"Account {i}",
             "address": f"user{i}@custom.example", "status": "connected"}
            for i in range(12)
        ], [])
        summary = self.service.capability_summary()
        listed = [line for line in summary.splitlines() if line.startswith("  - ")]
        self.assertLessEqual(len(listed), self.service.MAX_ACCOUNT_SUMMARY)

    def test_unreadable_config_does_not_claim_ability(self):
        with mock.patch.object(self.service, "load_config", side_effect=RuntimeError("boom")):
            summary = self.service.capability_summary()
        self.assertIn("Do not claim you can send or read email", summary)

    def test_morning_routine_membership_is_visible(self):
        self.service.replace_accounts([
            {"id": "work", "label": "Work", "address": "me@custom.example",
             "status": "connected", "morning_pull": True},
        ], [])
        self.assertIn("in morning routine", self.service.capability_summary())


class PromptWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CORE_PATH, "r", encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_capability_summary_is_added_to_the_system_prompt(self):
        self.assertIn("from bridge.email_service import capability_summary", self.source)
        self.assertIn("eva_system += _email_capability()", self.source)

    def test_wiring_does_not_swallow_unrelated_errors(self):
        start = self.source.index("from bridge.email_service import capability_summary")
        block = self.source[start:start + 260]
        self.assertIn("except ImportError", block)
        self.assertNotIn("except Exception", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
