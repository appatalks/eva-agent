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
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import config as _cfg


class FakeMailbox:
    instances = []
    fail_with = None
    refused_recipients = []
    mta_queue_id = ""

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
        recipient_count = len(normalized.get("to", [])) + len(normalized.get("cc", [])) + len(normalized.get("bcc", []))
        return {"message_id": "<x@test>", "recipient_count": recipient_count - len(FakeMailbox.refused_recipients),
            "refused_recipients": list(FakeMailbox.refused_recipients),
            "mta_queue_id": FakeMailbox.mta_queue_id}


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
        FakeMailbox.refused_recipients = []
        FakeMailbox.mta_queue_id = ""
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
        email_service.replace_accounts([account(id="existing")], ["old@custom.example"])
        email_service.set_credential("existing", "held-secret")
        _, errors = email_service.replace_accounts([account(), {"id": "broken"}], [])
        self.assertEqual(len(errors), 1)
        persisted = email_service.load_config()
        self.assertEqual([item["id"] for item in persisted["accounts"]], ["existing"])
        self.assertEqual(persisted["allowlist"], ["old@custom.example"])
        self.assertIn("existing", email_service._credentials)

    def test_missing_accounts_list_never_clears_existing_accounts(self):
        email_service.replace_accounts([account(id="existing")], [])
        with self.assertRaises(email_service.EmailServiceError):
            email_service.replace_accounts(None, [])
        self.assertEqual(email_service.load_config()["accounts"][0]["id"], "existing")

    def test_failed_disk_write_does_not_report_or_apply_success(self):
        email_service.replace_accounts([account(id="existing")], ["old@custom.example"])
        email_service.set_credential("existing", "held-secret")
        with mock.patch.object(email_service, "save_config", return_value=False):
            with self.assertRaises(email_service.EmailServiceError):
                email_service.replace_accounts([account(id="new")], ["new@custom.example"])
        persisted = email_service.load_config()
        self.assertEqual([item["id"] for item in persisted["accounts"]], ["existing"])
        self.assertEqual(persisted["allowlist"], ["old@custom.example"])
        self.assertIn("existing", email_service._credentials)

    def test_upsert_changes_one_account_without_rewriting_others(self):
        email_service.replace_accounts([
            account(id="first", label="First"),
            account(id="second", label="Second", address="second@custom.example"),
        ], ["old@custom.example"])
        email_service.set_credential("first", "first-secret")
        email_service.set_credential("second", "second-secret")
        email_service.upsert_account(account(id="first", label="Updated"))
        persisted = email_service.load_config()
        labels = {item["id"]: item["label"] for item in persisted["accounts"]}
        self.assertEqual(labels, {"first": "Updated", "second": "Second"})
        self.assertEqual(persisted["allowlist"], ["old@custom.example"])
        self.assertEqual(set(email_service._credentials), {"first", "second"})

    def test_invalid_upsert_leaves_everything_unchanged(self):
        email_service.replace_accounts([account(id="existing")], ["old@custom.example"])
        email_service.set_credential("existing", "held-secret")
        with self.assertRaises(email_service.EmailValidationError):
            email_service.upsert_account({"id": "broken"})
        persisted = email_service.load_config()
        self.assertEqual([item["id"] for item in persisted["accounts"]], ["existing"])
        self.assertEqual(persisted["allowlist"], ["old@custom.example"])
        self.assertIn("existing", email_service._credentials)

    def test_recipient_update_never_rewrites_accounts_or_credentials(self):
        email_service.replace_accounts([account(id="existing")], ["old@custom.example"])
        email_service.set_credential("existing", "held-secret")
        email_service.update_allowlist(["new@custom.example"])
        persisted = email_service.load_config()
        self.assertEqual([item["id"] for item in persisted["accounts"]], ["existing"])
        self.assertEqual(persisted["allowlist"], ["new@custom.example"])
        self.assertIn("existing", email_service._credentials)

    def test_failed_recipient_write_preserves_previous_value(self):
        email_service.replace_accounts([account(id="existing")], ["old@custom.example"])
        with mock.patch.object(email_service, "save_config", return_value=False):
            with self.assertRaises(email_service.EmailPersistenceError):
                email_service.update_allowlist(["new@custom.example"])
        self.assertEqual(email_service.load_config()["allowlist"], ["old@custom.example"])

    def test_delete_removes_only_target_and_its_credential(self):
        email_service.replace_accounts([
            account(id="first"),
            account(id="second", address="second@custom.example"),
        ], [])
        email_service.set_credential("first", "first-secret")
        email_service.set_credential("second", "second-secret")
        email_service.delete_account("first")
        self.assertEqual([item["id"] for item in email_service.load_config()["accounts"]], ["second"])
        self.assertNotIn("first", email_service._credentials)
        self.assertIn("second", email_service._credentials)

    def test_failed_delete_write_preserves_account_and_credential(self):
        email_service.replace_accounts([account(id="existing")], [])
        email_service.set_credential("existing", "held-secret")
        with mock.patch.object(email_service, "save_config", return_value=False):
            with self.assertRaises(email_service.EmailPersistenceError):
                email_service.delete_account("existing")
        self.assertEqual(email_service.load_config()["accounts"][0]["id"], "existing")
        self.assertIn("existing", email_service._credentials)

    def test_upsert_clears_credential_when_connection_identity_changes(self):
        email_service.replace_accounts([account(id="existing")], [])
        email_service.set_credential("existing", "held-secret")
        changed = account(id="existing", address="other@custom.example")
        changed["settings"] = {
            "imap_host": "imap.other.example",
            "smtp_host": "smtp.other.example",
        }
        email_service.upsert_account(changed)
        self.assertNotIn("existing", email_service._credentials)

    def test_upsert_preserves_credential_for_non_connection_edit(self):
        email_service.replace_accounts([account(id="existing", label="Old")], [])
        email_service.set_credential("existing", "held-secret")
        email_service.upsert_account(account(id="existing", label="New"))
        self.assertIn("existing", email_service._credentials)

    def test_upsert_preserves_account_order(self):
        email_service.replace_accounts([
            account(id="first"),
            account(id="second", address="second@custom.example"),
        ], [])
        email_service.upsert_account(account(id="first", label="Updated"))
        self.assertEqual(
            [item["id"] for item in email_service.load_config()["accounts"]],
            ["first", "second"],
        )

    def test_upsert_rejects_duplicate_backend_and_address(self):
        email_service.replace_accounts([
            account(id="first"),
            account(id="second", address="second@custom.example"),
        ], [])
        duplicate = account(id="second", address="me@custom.example")
        with self.assertRaises(email_service.EmailValidationError):
            email_service.upsert_account(duplicate)
        self.assertEqual(
            [item["address"] for item in email_service.load_config()["accounts"]],
            ["me@custom.example", "second@custom.example"],
        )

    def test_upsert_rejects_more_than_the_account_limit(self):
        accounts = [
            account(id=f"account{i}", address=f"user{i}@custom.example")
            for i in range(12)
        ]
        email_service.replace_accounts(accounts, [])
        with self.assertRaises(email_service.EmailValidationError):
            email_service.upsert_account(account(id="overflow", address="overflow@custom.example"))
        self.assertEqual(len(email_service.load_config()["accounts"]), 12)

    def test_upsert_preserves_opaque_provider_settings(self):
        email_service.save_config({
            "accounts": [
                {
                    "id": "provider", "backend": "workiq", "address": "user@company.example",
                    "status": "needs_auth", "settings": {"provider_state": "preserve-me"},
                },
                account(id="editable"),
            ],
            "allowlist": [],
        })
        email_service.upsert_account(account(id="editable", label="Updated"))
        with open(self.config_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        provider = next(item for item in raw["accounts"] if item["id"] == "provider")
        self.assertEqual(provider["settings"], {"provider_state": "preserve-me"})

    def test_upsert_preserves_unknown_legacy_record(self):
        legacy = {
            "id": "legacy", "backend": "unsupported_provider",
            "address": "legacy@example.com", "opaque": {"keep": True},
        }
        email_service.save_config({
            "accounts": [legacy, account(id="editable")],
            "allowlist": [],
        })
        email_service.upsert_account(account(id="editable", label="Updated"))
        with open(self.config_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertIn(legacy, raw["accounts"])

    def test_focused_mutations_preserve_identifierless_opaque_record(self):
        opaque = {"opaque": {"keep": True}}
        email_service.save_config({
            "accounts": [opaque, account(id="editable")],
            "allowlist": [],
        })
        email_service.upsert_account(account(id="editable", label="Updated"))
        email_service.delete_account("editable")
        with open(self.config_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self.assertEqual(raw["accounts"], [opaque])


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

    def test_unknown_account_credential_is_rejected(self):
        with self.assertRaises(email_service.EmailValidationError):
            email_service.set_credential("missing", "secret")

    def test_eva_direct_does_not_accept_a_credential(self):
        email_service.upsert_account({
            "id": "eva", "backend": "eva_direct", "address": "eva@lab.internal",
            "status": "connected", "settings": {"delivery_mode": "internal"},
        })
        with self.assertRaises(email_service.EmailValidationError):
            email_service.set_credential("eva", "not-needed")

    def test_exim_status_requires_explicit_account_opt_in(self):
        email_service.upsert_account({
            "id": "eva", "backend": "eva_direct", "address": "eva@lab.internal",
            "status": "connected", "settings": {"delivery_mode": "local_mta", "internal_smtp_host": "127.0.0.1"},
        })
        with self.assertRaises(email_service.EmailValidationError) as caught:
            email_service.inspect_local_mta_status("eva", "1wtest-00000004LAd-0WIt")
        self.assertIn("Enable Exim status", str(caught.exception))

    def test_exim_status_returns_sanitized_transport_state(self):
        email_service.upsert_account({
            "id": "eva", "backend": "eva_direct", "address": "eva@lab.internal",
            "status": "connected", "settings": {
                "delivery_mode": "local_mta", "internal_smtp_host": "127.0.0.1", "exim_status": True,
            },
        })
        email_service._remember_mta_submissions("eva", [{"mta_queue_id": "1wtest-00000004LAd-0WIt"}])
        with mock.patch("bridge.exim_status.inspect", return_value={
            "queue_id": "1wtest-00000004LAd-0WIt", "status": "failed", "access": "direct",
            "detail": "Mailing to remote domains not supported", "completed": True,
        }) as inspect:
            result = email_service.inspect_local_mta_status("eva", "1wtest-00000004LAd-0WIt")
        self.assertEqual(result["status"], "failed")
        inspect.assert_called_once_with("1wtest-00000004LAd-0WIt", allow_sudo=False)

    def test_exim_status_rejects_unknown_but_well_formed_queue_id(self):
        email_service.upsert_account({
            "id": "eva", "backend": "eva_direct", "address": "eva@lab.internal",
            "status": "connected", "settings": {
                "delivery_mode": "local_mta", "internal_smtp_host": "127.0.0.1", "exim_status": True,
            },
        })
        with self.assertRaises(email_service.EmailValidationError) as caught:
            email_service.inspect_local_mta_status("eva", "1wtest-00000004LAd-0WIt")
        self.assertIn("not submitted by Eva", str(caught.exception))

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

    def test_partial_recipient_refusal_is_reported(self):
        email_service.update_allowlist(["peer@company.example", "watcher@company.example"])
        FakeMailbox.refused_recipients = ["watcher@company.example"]
        result = email_service.send_message(
            send_request(cc="watcher@company.example"), account_id="work"
        )
        self.assertEqual(result["decision"], "partially_sent")
        self.assertEqual(result["deliveries"][0]["recipient_count"], 1)
        self.assertTrue(result["failures"])

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

    def test_pending_confirmation_sends_exactly_once(self):
        pending = email_service.prepare_message(
            send_request(to="stranger@elsewhere.example"), "session-a", account_id="work"
        )
        self.assertEqual(pending["decision"], "pending_confirmation")
        self.assertEqual(pending["request"]["subject"], "Status")

        first = email_service.confirm_pending_message("session-a", pending["pending_id"])
        second = email_service.confirm_pending_message("session-a", pending["pending_id"])

        self.assertEqual(first["decision"], "sent")
        self.assertFalse(first["idempotent_replay"])
        self.assertEqual(second["decision"], "sent")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(len(FakeMailbox.instances), 1)
        self.assertEqual(len(FakeMailbox.instances[0].sent), 1)
        tombstone = email_service._pending_messages[pending["pending_id"]]
        self.assertIsNone(tombstone["request"])
        self.assertIsNone(tombstone["confirmation"])

    def test_eva_direct_pending_confirmation_preserves_sender(self):
        email_service.replace_accounts([account(
            id="Eva-agent", backend="eva_direct", address="eva@custom.example",
            settings={
                "direct_consent": [], "delivery_mode": "local_mta",
                "internal_domains": [], "internal_smtp_host": "mail.custom.example",
                "internal_smtp_starttls": False, "exim_status": True,
            },
        )], [])
        pending = email_service.prepare_message(
            send_request(to="outside@example.net"), "session-a", account_id="Eva-agent"
        )
        self.assertEqual(pending["decision"], "pending_confirmation")
        self.assertEqual(pending["account_id"], "Eva-agent")
        self.assertEqual(
            email_service._pending_messages[pending["pending_id"]]["account_id"], "Eva-agent"
        )
        transport = {
            "queue_id": "1abcDEF-000000-xy", "status": "failed",
            "detail": "Mailing to remote domains not supported", "completed": True,
        }
        FakeMailbox.mta_queue_id = transport["queue_id"]
        with mock.patch.object(email_service, "inspect_local_mta_status", return_value=transport):
            result = email_service.confirm_pending_message("session-a", pending["pending_id"])
        self.assertEqual(result["decision"], "submitted")
        self.assertEqual(result["account_id"], "Eva-agent")
        self.assertEqual(result["transport_status"]["status"], "failed")
        replay = email_service.confirm_pending_message("session-a", pending["pending_id"])
        self.assertEqual(replay["transport_status"]["status"], "failed")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(FakeMailbox.instances), 1)
        self.assertEqual(len(FakeMailbox.instances[0].sent), 1)

    def test_completed_receipt_survives_the_draft_ttl(self):
        pending = email_service.prepare_message(send_request(), "session-a", account_id="work")
        draft_expiry = email_service._pending_messages[pending["pending_id"]]["expires_at"]
        first = email_service.confirm_pending_message("session-a", pending["pending_id"])
        with mock.patch.object(email_service.time, "time", return_value=draft_expiry + 1):
            replay = email_service.confirm_pending_message("session-a", pending["pending_id"])
        self.assertEqual(first["decision"], "sent")
        self.assertEqual(replay["decision"], "sent")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(len(FakeMailbox.instances[0].sent), 1)

    def test_unexpected_send_failure_is_terminal_and_not_retried(self):
        pending = email_service.prepare_message(send_request(), "session-a", account_id="work")
        with mock.patch.object(email_service, "send_message", side_effect=RuntimeError("private detail")) as sender:
            first = email_service.confirm_pending_message("session-a", pending["pending_id"])
            replay = email_service.confirm_pending_message("session-a", pending["pending_id"])
        self.assertEqual(first["decision"], "failed")
        self.assertNotIn("private detail", first["reason"])
        self.assertEqual(replay["decision"], "failed")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(sender.call_count, 1)

    def test_concurrent_confirmations_enter_delivery_once(self):
        pending = email_service.prepare_message(send_request(), "session-a", account_id="work")
        entered = threading.Event()
        release = threading.Event()
        original_send = email_service.send_message

        def blocked_send(*args, **kwargs):
            entered.set()
            release.wait(timeout=2)
            return original_send(*args, **kwargs)

        results = []
        with mock.patch.object(email_service, "send_message", side_effect=blocked_send) as sender:
            thread = threading.Thread(target=lambda: results.append(
                email_service.confirm_pending_message("session-a", pending["pending_id"])
            ))
            thread.start()
            self.assertTrue(entered.wait(timeout=2))
            email_service._pending_messages[pending["pending_id"]]["expires_at"] = 0
            concurrent = email_service.confirm_pending_message("session-a", pending["pending_id"])
            release.set()
            thread.join(timeout=2)
        replay = email_service.confirm_pending_message("session-a", pending["pending_id"])
        self.assertEqual(concurrent["decision"], "in_progress")
        self.assertEqual(results[0]["decision"], "sent")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(sender.call_count, 1)

    def test_authorization_change_stores_no_draft_data(self):
        pending = email_service.prepare_message(send_request(), "session-a", account_id="work")
        email_service.update_allowlist([])
        result = email_service.confirm_pending_message("session-a", pending["pending_id"])
        replay = email_service.confirm_pending_message("session-a", pending["pending_id"])
        self.assertEqual(result["decision"], "failed")
        self.assertEqual(replay["decision"], "failed")
        serialized = repr(email_service._pending_messages[pending["pending_id"]])
        self.assertNotIn("peer@company.example", serialized)
        self.assertNotIn("All green", serialized)
        self.assertNotIn("Status", serialized)
        self.assertEqual(FakeMailbox.instances, [])

    def test_pending_confirmation_is_session_bound(self):
        pending = email_service.prepare_message(send_request(), "session-a", account_id="work")
        result = email_service.confirm_pending_message("session-b", pending["pending_id"])
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(FakeMailbox.instances, [])

    def test_pending_confirmation_can_be_cancelled(self):
        pending = email_service.prepare_message(send_request(), "session-a", account_id="work")
        cancelled = email_service.cancel_pending_message("session-a", pending["pending_id"])
        result = email_service.confirm_pending_message("session-a", pending["pending_id"])
        self.assertEqual(cancelled["decision"], "cancelled")
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(FakeMailbox.instances, [])

    def test_expired_pending_confirmation_is_rejected(self):
        pending = email_service.prepare_message(send_request(), "session-a", account_id="work")
        email_service._pending_messages[pending["pending_id"]]["expires_at"] = 0
        result = email_service.confirm_pending_message("session-a", pending["pending_id"])
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(FakeMailbox.instances, [])

    def test_rejected_request_never_reaches_an_adapter(self):
        result = email_service.send_message(send_request(to="bogus"), account_id="work")
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(FakeMailbox.instances, [])

    def test_authorize_does_not_deliver(self):
        email_service.authorize(send_request(), account_id="work")
        self.assertEqual(FakeMailbox.instances, [])

    def test_partial_local_mta_submission_retains_its_queue_id(self):
        email_service.replace_accounts([account(
            id="eva", backend="eva_direct", address="eva@custom.example", settings={
                "direct_consent": ["first@company.example", "second@company.example"],
                "delivery_mode": "local_mta", "internal_domains": [],
                "internal_smtp_host": "mail.custom.example", "internal_smtp_starttls": False,
                "exim_status": True,
            },
        )], [])
        FakeMailbox.refused_recipients = ["second@company.example"]
        FakeMailbox.mta_queue_id = "1abcDEF-000000-xy"
        result = email_service.send_message(send_request(
            to=["first@company.example", "second@company.example"]
        ), account_id="eva")
        self.assertEqual(result["decision"], "partially_sent")
        self.assertTrue(email_service._known_mta_submission("eva", FakeMailbox.mta_queue_id))


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

    def test_direct_consent_is_the_identity_scoped_send_allowlist(self):
        email_service.replace_accounts(email_service.load_config()["accounts"], [])
        result = email_service.send_message(
            send_request(to="box@lab.internal"), account_id="direct"
        )
        self.assertEqual(result["decision"], "sent")
        self.assertEqual(len(FakeMailbox.instances), 1)

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
