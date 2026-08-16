#!/usr/bin/env python3
"""Contract: email service orchestration, credential handling, and delivery.

Adapters are replaced with fakes and the config path is redirected to a
temporary directory, so no mailbox or network is touched.
"""

import importlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import config as _cfg


class FakeMailbox:
    instances = []
    fail_with = None

    def __init__(self, settings, address, timeout=None):
        self.settings = settings
        self.address = address
        self.sent = []
        FakeMailbox.instances.append(self)

    def fetch_recent(self, password, folder="INBOX", limit=10, unseen_only=False):
        if FakeMailbox.fail_with:
            raise FakeMailbox.fail_with("mailbox unavailable")
        self.last_fetch = {"password": password, "folder": folder, "limit": limit, "unseen": unseen_only}
        return [{"from": "peer@company.example", "subject": "Status", "preview": "All green."}]

    def delete_message(self, password, folder, message_id):
        self.deleted = {"folder": folder, "id": message_id}
        return True

    def send(self, password, normalized, from_address="", reply_to=""):
        self.sent.append({"password": password, "request": normalized, "from": from_address})
        return {"message_id": "<x@test>", "recipient_count": len(normalized.get("to", []))
                + len(normalized.get("cc", [])) + len(normalized.get("bcc", []))}


def account(**overrides):
    record = {
        "id": "work", "label": "Work", "backend": "imap_smtp",
        "address": "me@custom.example", "status": "connected",
    }
    record.update(overrides)
    return record


def send_request(**overrides):
    request = {"to": "peer@company.example", "subject": "Status", "body": "All green."}
    request.update(overrides)
    return request


class EmailServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config_path = os.path.join(self.directory.name, "email_accounts.json")
        patcher = mock.patch.object(_cfg, "EMAIL_CONFIG_PATH", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        global email_service
        from bridge import email_service as module
        importlib.reload(module)
        email_service = module
        email_service._EMAIL_CONFIG_PATH = self.config_path

        FakeMailbox.instances = []
        FakeMailbox.fail_with = None
        adapter_patcher = mock.patch.object(email_service, "ImapSmtpMailbox", FakeMailbox)
        adapter_patcher.start()
        self.addCleanup(adapter_patcher.stop)
        self.addCleanup(lambda: email_service._credentials.clear())


class ConfigTests(EmailServiceTestCase):
    def test_missing_config_returns_empty_document(self):
        document = email_service.load_config()
        self.assertEqual(document["accounts"], [])
        self.assertEqual(document["allowlist"], [])

    def test_accounts_round_trip(self):
        document, errors = email_service.replace_accounts([account()], ["peer@company.example"])
        self.assertEqual(errors, [])
        reloaded = email_service.load_config()
        self.assertEqual(reloaded["accounts"][0]["id"], "work")
        self.assertEqual(reloaded["allowlist"], ["peer@company.example"])

    def test_config_file_is_owner_readable_only(self):
        email_service.replace_accounts([account()], [])
        self.assertEqual(os.stat(self.config_path).st_mode & 0o777, 0o600)

    def test_corrupt_config_degrades_to_empty(self):
        with open(self.config_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(email_service.load_config()["accounts"], [])

    def test_invalid_account_is_reported_not_persisted(self):
        _, errors = email_service.replace_accounts([account(), {"id": "broken"}], [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(email_service.load_config()["accounts"]), 1)


class CredentialTests(EmailServiceTestCase):
    def setUp(self):
        super().setUp()
        email_service.replace_accounts([account()], ["peer@company.example"])

    def test_credential_is_never_written_to_disk(self):
        email_service.set_credential("work", "hunter2")
        with open(self.config_path, "r", encoding="utf-8") as handle:
            contents = handle.read()
        self.assertNotIn("hunter2", contents)

    def test_public_accounts_report_presence_without_the_secret(self):
        email_service.set_credential("work", "hunter2")
        listed = email_service.public_accounts()
        self.assertTrue(listed[0]["credential_present"])
        self.assertNotIn("hunter2", json.dumps(listed))

    def test_operation_without_a_credential_is_refused(self):
        with self.assertRaises(email_service.EmailServiceError) as caught:
            email_service.fetch_messages("work")
        self.assertIn("credential", str(caught.exception))

    def test_empty_credential_is_rejected(self):
        with self.assertRaises(email_service.EmailServiceError):
            email_service.set_credential("work", "")

    def test_removing_an_account_forgets_its_credential(self):
        email_service.set_credential("work", "hunter2")
        email_service.replace_accounts([], [])
        self.assertFalse(email_service.public_accounts())
        self.assertNotIn("work", email_service._credentials)

    def test_clear_credential(self):
        email_service.set_credential("work", "hunter2")
        self.assertTrue(email_service.clear_credential("work"))
        self.assertFalse(email_service.clear_credential("work"))


class FetchTests(EmailServiceTestCase):
    def setUp(self):
        super().setUp()
        email_service.replace_accounts([account()], ["peer@company.example"])
        email_service.set_credential("work", "hunter2")

    def test_fetch_returns_summaries(self):
        messages = email_service.fetch_messages("work", limit=5, unseen_only=True)
        self.assertEqual(messages[0]["subject"], "Status")
        self.assertEqual(FakeMailbox.instances[0].last_fetch["limit"], 5)
        self.assertTrue(FakeMailbox.instances[0].last_fetch["unseen"])

    def test_limit_is_capped(self):
        email_service.fetch_messages("work", limit=10000)
        self.assertLessEqual(FakeMailbox.instances[0].last_fetch["limit"], email_service.MAX_FETCH_LIMIT)

    def test_unknown_account_is_refused(self):
        with self.assertRaises(email_service.EmailServiceError):
            email_service.fetch_messages("ghost")

    def test_read_capability_is_required(self):
        email_service.replace_accounts([account(capabilities=["send"])], [])
        email_service.set_credential("work", "hunter2")
        with self.assertRaises(email_service.EmailServiceError):
            email_service.fetch_messages("work")

    def test_adapter_failure_surfaces_as_service_error(self):
        from bridge.mailbox_imap import MailboxError
        FakeMailbox.fail_with = MailboxError
        with self.assertRaises(email_service.EmailServiceError):
            email_service.fetch_messages("work")


class SendTests(EmailServiceTestCase):
    def setUp(self):
        super().setUp()
        email_service.replace_accounts([account()], ["peer@company.example"])
        email_service.set_credential("work", "hunter2")

    def test_allowlisted_send_is_delivered(self):
        result = email_service.send_message(send_request(), account_id="work")
        self.assertEqual(result["decision"], "sent")
        self.assertEqual(len(FakeMailbox.instances[0].sent), 1)

    def test_unknown_recipient_is_not_delivered(self):
        result = email_service.send_message(send_request(to="stranger@elsewhere.example"), account_id="work")
        self.assertEqual(result["decision"], "needs_confirmation")
        self.assertEqual(FakeMailbox.instances, [])

    def test_confirmation_completes_the_send(self):
        request = send_request(to="stranger@elsewhere.example")
        pending = email_service.authorize(request, account_id="work")
        result = email_service.send_message(
            request, account_id="work",
            confirmation={"digest": pending["digest"], "addresses": ["stranger@elsewhere.example"]},
        )
        self.assertEqual(result["decision"], "sent")

    def test_rejected_request_never_reaches_an_adapter(self):
        result = email_service.send_message(send_request(to="bogus"), account_id="work")
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(FakeMailbox.instances, [])

    def test_authorize_does_not_deliver(self):
        email_service.authorize(send_request(), account_id="work")
        self.assertEqual(FakeMailbox.instances, [])


class EvaDirectDeliveryTests(EmailServiceTestCase):
    def setUp(self):
        super().setUp()
        email_service.replace_accounts([
            account(id="owned", address="me@home.example"),
            account(id="direct", backend="eva_direct", address="eva@home.example", settings={
                "direct_consent": ["@lab.internal", "peer@company.example"],
                "delivery_mode": "auto",
                "internal_domains": ["lab.internal"],
                "internal_smtp_host": "mail.lab.internal",
                "internal_smtp_starttls": False,
                "relay_account_id": "owned",
            }),
        ], ["@lab.internal", "peer@company.example"])
        email_service.set_credential("owned", "hunter2")

    def test_direct_consent_does_not_replace_the_send_allowlist(self):
        email_service.replace_accounts(email_service.load_config()["accounts"], [])
        result = email_service.send_message(
            send_request(to="box@lab.internal"), account_id="direct"
        )
        self.assertEqual(result["decision"], "needs_confirmation")
        self.assertEqual(FakeMailbox.instances, [])

    def test_internal_recipient_uses_the_internal_mta_without_credentials(self):
        result = email_service.send_message(
            send_request(to="box@lab.internal"), account_id="direct"
        )
        self.assertEqual(result["decision"], "sent")
        mailbox = FakeMailbox.instances[0]
        self.assertEqual(mailbox.settings["smtp_host"], "mail.lab.internal")
        self.assertEqual(mailbox.sent[0]["password"], "")
        self.assertTrue(mailbox.settings["smtp_allow_plaintext"])

    def test_external_recipient_relays_with_the_owned_identity(self):
        result = email_service.send_message(
            send_request(to="peer@company.example"), account_id="direct"
        )
        self.assertEqual(result["decision"], "sent")
        mailbox = FakeMailbox.instances[0]
        self.assertEqual(mailbox.sent[0]["from"], "eva@home.example")
        self.assertEqual(mailbox.sent[0]["password"], "hunter2")

    def test_mixed_message_splits_into_two_deliveries(self):
        result = email_service.send_message(
            send_request(to=["box@lab.internal", "peer@company.example"]), account_id="direct"
        )
        routes = sorted(d["route"] for d in result["deliveries"])
        self.assertEqual(routes, ["internal", "relay"])

    def test_unconsented_recipient_is_never_delivered(self):
        result = email_service.send_message(
            send_request(to="stranger@elsewhere.example"), account_id="direct"
        )
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(FakeMailbox.instances, [])


class MorningSummaryTests(EmailServiceTestCase):
    def setUp(self):
        super().setUp()
        email_service.replace_accounts([account(morning_pull=True)], [])

    def test_summary_frames_mail_as_untrusted(self):
        email_service.set_credential("work", "hunter2")
        summary, unavailable = email_service.morning_mail_summary()
        self.assertIn("UNTRUSTED MAILBOX DATA", summary)
        self.assertEqual(unavailable, [])

    def test_locked_account_degrades_instead_of_raising(self):
        summary, unavailable = email_service.morning_mail_summary()
        self.assertEqual(summary, "")
        self.assertEqual(unavailable, ["Work"])

    def test_account_outside_the_morning_routine_is_skipped(self):
        email_service.replace_accounts([account(morning_pull=False)], [])
        email_service.set_credential("work", "hunter2")
        summary, unavailable = email_service.morning_mail_summary()
        self.assertEqual(summary, "")
        self.assertEqual(unavailable, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
