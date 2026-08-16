#!/usr/bin/env python3
"""Contract: multi-account email routing, capabilities, and Eva-direct consent."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import email_accounts as accounts_module
from bridge import email_policy


def account(**overrides):
    record = {
        "id": "work",
        "label": "Work",
        "backend": "workiq",
        "address": "user@company.example",
        "status": "connected",
    }
    record.update(overrides)
    return record


def send(**overrides):
    request = {"to": "peer@company.example", "subject": "Status", "body": "All green."}
    request.update(overrides)
    return request


class BackendSuggestionTests(unittest.TestCase):
    def test_recognizes_hosted_providers(self):
        self.assertEqual(accounts_module.suggest_backend("person@gmail.com"), "gmail_oauth")
        self.assertEqual(accounts_module.suggest_backend("person@outlook.com"), "workiq")

    def test_falls_back_to_imap_for_custom_domains(self):
        self.assertEqual(accounts_module.suggest_backend("person@custom.example"), "imap_smtp")

    def test_invalid_address_suggests_nothing(self):
        self.assertEqual(accounts_module.suggest_backend("nonsense"), "")

    def test_host_guess_follows_convention(self):
        hosts = accounts_module.suggest_imap_smtp_hosts("person@custom.example")
        self.assertEqual(hosts["imap_host"], "imap.custom.example")
        self.assertEqual(hosts["smtp_host"], "smtp.custom.example")
        self.assertEqual(hosts["imap_port"], 993)


class NormalizeAccountTests(unittest.TestCase):
    def test_defaults_capabilities_from_backend(self):
        record, error = accounts_module.normalize_account(account())
        self.assertEqual(error, "")
        self.assertEqual(record["capabilities"], ["read", "send", "delete"])

    def test_infers_backend_from_address(self):
        record, error = accounts_module.normalize_account(
            {"id": "personal", "address": "person@gmail.com", "status": "connected"}
        )
        self.assertEqual(error, "")
        self.assertEqual(record["backend"], "gmail_oauth")

    def test_rejects_invalid_id_and_address(self):
        self.assertIn("id", accounts_module.normalize_account(account(id="bad id!"))[1])
        self.assertIn("address", accounts_module.normalize_account(account(address="nope"))[1])

    def test_capability_cannot_exceed_backend_support(self):
        record, error = accounts_module.normalize_account(
            account(id="direct", backend="eva_direct", capabilities=["read", "send", "delete"])
        )
        self.assertEqual(error, "")
        self.assertEqual(record["capabilities"], ["send"])

    def test_account_with_no_supported_capability_is_rejected(self):
        self.assertIn(
            "capability",
            accounts_module.normalize_account(account(backend="eva_direct", capabilities=["read"]))[1],
        )

    def test_imap_defaults_are_filled_in(self):
        record, error = accounts_module.normalize_account(
            account(id="custom", backend="imap_smtp", address="me@custom.example")
        )
        self.assertEqual(error, "")
        self.assertEqual(record["settings"]["imap_host"], "imap.custom.example")
        self.assertEqual(record["settings"]["smtp_port"], 587)

    def test_imap_without_tls_is_refused(self):
        self.assertIn("TLS", accounts_module.normalize_account(account(
            id="custom", backend="imap_smtp", address="me@custom.example",
            settings={"imap_tls": False},
        ))[1])

    def test_unknown_status_becomes_needs_auth(self):
        record, _ = accounts_module.normalize_account(account(status="whatever"))
        self.assertEqual(record["status"], "needs_auth")

    def test_eva_direct_never_joins_the_morning_pull(self):
        record, _ = accounts_module.normalize_account(
            account(id="direct", backend="eva_direct", address="eva@home.example", morning_pull=True)
        )
        self.assertFalse(record["morning_pull"])


class NormalizeAccountsTests(unittest.TestCase):
    def test_rejects_duplicate_ids_and_addresses(self):
        records, errors = accounts_module.normalize_accounts([
            account(), account(), account(id="other"),
        ])
        self.assertEqual(len(records), 1)
        self.assertEqual(len(errors), 2)

    def test_one_bad_account_does_not_discard_the_others(self):
        records, errors = accounts_module.normalize_accounts([
            account(), {"id": "broken"},
        ])
        self.assertEqual(len(records), 1)
        self.assertEqual(len(errors), 1)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.accounts, _ = accounts_module.normalize_accounts([
            account(id="work", address="user@company.example"),
            account(id="personal", backend="gmail_oauth", address="person@gmail.com"),
            account(id="direct", backend="eva_direct", address="eva@home.example",
                    settings={"direct_consent": ["peer@company.example"]}),
        ])

    def test_read_accounts_exclude_send_only_identities(self):
        readable = [a["id"] for a in accounts_module.read_accounts(self.accounts)]
        self.assertEqual(readable, ["work", "personal"])

    def test_morning_pull_filter(self):
        self.accounts[1]["morning_pull"] = False
        morning = [a["id"] for a in accounts_module.read_accounts(self.accounts, morning_only=True)]
        self.assertEqual(morning, ["work"])

    def test_disconnected_account_is_never_used(self):
        self.accounts[0]["status"] = "needs_auth"
        self.assertEqual([a["id"] for a in accounts_module.read_accounts(self.accounts)], ["personal"])

    def test_explicit_account_id_wins(self):
        chosen, error = accounts_module.select_send_account(self.accounts, account_id="personal")
        self.assertEqual(error, "")
        self.assertEqual(chosen["id"], "personal")

    def test_from_address_selects_the_matching_account(self):
        chosen, _ = accounts_module.select_send_account(self.accounts, from_address="person@gmail.com")
        self.assertEqual(chosen["id"], "personal")

    def test_unknown_from_address_is_an_error_not_a_fallback(self):
        chosen, error = accounts_module.select_send_account(self.accounts, from_address="ghost@nowhere.example")
        self.assertIsNone(chosen)
        self.assertIn("no connected sending account", error)

    def test_default_send_flag_is_honored(self):
        self.accounts[1]["default_send"] = True
        chosen, _ = accounts_module.select_send_account(self.accounts)
        self.assertEqual(chosen["id"], "personal")

    def test_eva_direct_is_never_an_implicit_default(self):
        only_direct = [a for a in self.accounts if a["backend"] == "eva_direct"]
        chosen, error = accounts_module.select_send_account(only_direct)
        self.assertIsNone(chosen)
        self.assertIn("explicitly", error)


class EvaDirectConsentTests(unittest.TestCase):
    def setUp(self):
        self.direct, _ = accounts_module.normalize_account(
            account(id="direct", backend="eva_direct", address="eva@home.example",
                    settings={"direct_consent": ["peer@company.example", "@lab.internal"]})
        )

    def test_consenting_recipient_is_allowed(self):
        result = accounts_module.authorize_send_for_account(self.direct, send(), ["peer@company.example"])
        self.assertEqual(result["decision"], "allowed")
        self.assertEqual(result["backend"], "eva_direct")

    def test_domain_consent_covers_internal_hosts(self):
        result = accounts_module.authorize_send_for_account(
            self.direct, send(to="box@lab.internal"), ["@lab.internal"]
        )
        self.assertEqual(result["decision"], "allowed")

    def test_unconsented_recipient_is_rejected_not_confirmable(self):
        result = accounts_module.authorize_send_for_account(
            self.direct, send(to="stranger@elsewhere.example"), ["stranger@elsewhere.example"]
        )
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["unconsented_recipients"], ["stranger@elsewhere.example"])

    def test_consent_cannot_be_supplied_by_a_send_confirmation(self):
        request = send(to="stranger@elsewhere.example")
        pending = email_policy.authorize_send(request, [])
        result = accounts_module.authorize_send_for_account(
            self.direct, request, [],
            {"digest": pending["digest"], "addresses": ["stranger@elsewhere.example"]},
        )
        self.assertEqual(result["decision"], "rejected")


class AccountAuthorizationTests(unittest.TestCase):
    def test_account_allowlist_supplements_the_global_one(self):
        record, _ = accounts_module.normalize_account(account(allowlist=["peer@company.example"]))
        result = accounts_module.authorize_send_for_account(record, send(), [])
        self.assertEqual(result["decision"], "allowed")

    def test_unknown_recipient_still_requires_confirmation(self):
        record, _ = accounts_module.normalize_account(account())
        result = accounts_module.authorize_send_for_account(record, send(to="new@elsewhere.example"), [])
        self.assertEqual(result["decision"], "needs_confirmation")
        self.assertEqual(result["unknown_recipients"], ["new@elsewhere.example"])

    def test_send_through_a_read_only_account_is_refused(self):
        record, _ = accounts_module.normalize_account(account(capabilities=["read"]))
        result = accounts_module.authorize_send_for_account(record, send(), ["peer@company.example"])
        self.assertEqual(result["decision"], "rejected")

    def test_disconnected_account_cannot_send(self):
        record, _ = accounts_module.normalize_account(account(status="needs_auth"))
        result = accounts_module.authorize_send_for_account(record, send(), ["peer@company.example"])
        self.assertEqual(result["decision"], "rejected")

    def test_result_records_the_account_used(self):
        record, _ = accounts_module.normalize_account(account(allowlist=["peer@company.example"]))
        result = accounts_module.authorize_send_for_account(record, send(), [])
        self.assertEqual(result["account_id"], "work")


class SecretHygieneTests(unittest.TestCase):
    def test_account_record_never_retains_credential_fields(self):
        record, _ = accounts_module.normalize_account(account(
            backend="imap_smtp", address="me@custom.example",
            password="hunter2", settings={"password": "hunter2", "imap_host": "imap.custom.example"},
        ))
        serialized = repr(record)
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("password", serialized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
