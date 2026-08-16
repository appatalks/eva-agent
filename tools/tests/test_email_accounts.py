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
    def test_known_providers_use_the_implemented_adapter(self):
        for address in ["person@gmail.com", "person@outlook.com", "person@custom.example"]:
            self.assertEqual(accounts_module.suggest_backend(address), "imap_smtp", address)

    def test_workiq_is_never_inferred_from_an_address(self):
        for address in ["person@outlook.com", "person@hotmail.com", "person@company.example"]:
            self.assertNotEqual(accounts_module.suggest_backend(address), "workiq", address)

    def test_invalid_address_suggests_nothing(self):
        self.assertEqual(accounts_module.suggest_backend("nonsense"), "")

    def test_gmail_gets_published_hosts_and_oauth(self):
        hosts = accounts_module.suggest_imap_smtp_hosts("person@gmail.com")
        self.assertEqual(hosts["imap_host"], "imap.gmail.com")
        self.assertEqual(hosts["smtp_host"], "smtp.gmail.com")
        self.assertEqual(hosts["auth_mechanism"], "xoauth2")

    def test_outlook_gets_published_hosts_and_oauth(self):
        hosts = accounts_module.suggest_imap_smtp_hosts("person@outlook.com")
        self.assertEqual(hosts["imap_host"], "outlook.office365.com")
        self.assertEqual(hosts["auth_mechanism"], "xoauth2")

    def test_custom_domain_falls_back_to_convention(self):
        hosts = accounts_module.suggest_imap_smtp_hosts("person@custom.example")
        self.assertEqual(hosts["imap_host"], "imap.custom.example")
        self.assertEqual(hosts["smtp_host"], "smtp.custom.example")
        self.assertEqual(hosts["imap_port"], 993)


class ProviderAccountTests(unittest.TestCase):
    def test_gmail_account_is_configured_without_any_input(self):
        record, error = accounts_module.normalize_account(
            {"id": "personal", "address": "person@gmail.com", "status": "connected"}
        )
        self.assertEqual(error, "")
        self.assertEqual(record["backend"], "imap_smtp")
        self.assertEqual(record["settings"]["imap_host"], "imap.gmail.com")
        self.assertEqual(record["settings"]["auth_mechanism"], "xoauth2")

    def test_outlook_account_is_configured_without_any_input(self):
        record, error = accounts_module.normalize_account(
            {"id": "work", "address": "person@outlook.com", "status": "connected"}
        )
        self.assertEqual(error, "")
        self.assertEqual(record["settings"]["imap_host"], "outlook.office365.com")
        self.assertEqual(record["settings"]["auth_mechanism"], "xoauth2")

    def test_custom_domain_defaults_to_password_auth(self):
        record, _ = accounts_module.normalize_account(
            {"id": "custom", "address": "me@custom.example", "status": "connected"}
        )
        self.assertEqual(record["settings"]["auth_mechanism"], "password")

    def test_explicit_settings_override_provider_defaults(self):
        record, _ = accounts_module.normalize_account({
            "id": "personal", "address": "person@gmail.com", "status": "connected",
            "settings": {"auth_mechanism": "password"},
        })
        self.assertEqual(record["settings"]["auth_mechanism"], "password")
        self.assertEqual(record["settings"]["imap_host"], "imap.gmail.com")

    def test_unknown_mechanism_falls_back_to_password(self):
        record, _ = accounts_module.normalize_account({
            "id": "custom", "address": "me@custom.example", "status": "connected",
            "settings": {"auth_mechanism": "magic"},
        })
        self.assertEqual(record["settings"]["auth_mechanism"], "password")


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
        self.assertEqual(record["backend"], "imap_smtp")

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
                    settings={"direct_consent": ["peer@company.example", "@lab.internal"],
                              "delivery_mode": "relay", "relay_account_id": "owned"})
        )
        self.relay, _ = accounts_module.normalize_account(
            account(id="owned", backend="imap_smtp", address="me@home.example")
        )
        self.pool = [self.direct, self.relay]

    def test_consenting_recipient_is_allowed(self):
        result = accounts_module.authorize_send_for_account(
            self.direct, send(), ["peer@company.example"], accounts=self.pool
        )
        self.assertEqual(result["decision"], "allowed")
        self.assertEqual(result["backend"], "eva_direct")

    def test_domain_consent_covers_internal_hosts(self):
        result = accounts_module.authorize_send_for_account(
            self.direct, send(to="box@lab.internal"), ["@lab.internal"], accounts=self.pool
        )
        self.assertEqual(result["decision"], "allowed")

    def test_unconsented_recipient_is_rejected_not_confirmable(self):
        result = accounts_module.authorize_send_for_account(
            self.direct, send(to="stranger@elsewhere.example"), ["stranger@elsewhere.example"],
            accounts=self.pool,
        )
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(result["unconsented_recipients"], ["stranger@elsewhere.example"])

    def test_consent_cannot_be_supplied_by_a_send_confirmation(self):
        request = send(to="stranger@elsewhere.example")
        pending = email_policy.authorize_send(request, [])
        result = accounts_module.authorize_send_for_account(
            self.direct, request, [],
            {"digest": pending["digest"], "addresses": ["stranger@elsewhere.example"]},
            accounts=self.pool,
        )
        self.assertEqual(result["decision"], "rejected")


class DomainAlignmentTests(unittest.TestCase):
    def test_same_domain_is_aligned(self):
        self.assertTrue(accounts_module.domains_aligned("eva@home.example", "me@home.example"))

    def test_subdomain_from_is_aligned(self):
        self.assertTrue(accounts_module.domains_aligned("eva@bot.home.example", "me@home.example"))

    def test_cross_domain_is_never_aligned(self):
        self.assertFalse(accounts_module.domains_aligned("eva@home.example", "me@gmail.com"))

    def test_lookalike_suffix_is_not_alignment(self):
        self.assertFalse(accounts_module.domains_aligned("eva@evilhome.example", "me@home.example"))

    def test_invalid_addresses_are_not_aligned(self):
        self.assertFalse(accounts_module.domains_aligned("nonsense", "me@home.example"))


class DirectDeliveryPlanTests(unittest.TestCase):
    def setUp(self):
        self.relay, _ = accounts_module.normalize_account(
            account(id="owned", backend="imap_smtp", address="me@home.example")
        )

    def direct(self, **settings):
        base = {
            "direct_consent": ["@lab.internal", "peer@company.example"],
            "delivery_mode": "auto",
            "internal_domains": ["lab.internal"],
            "internal_smtp_host": "mail.lab.internal",
            "relay_account_id": "owned",
        }
        base.update(settings)
        record, error = accounts_module.normalize_account(
            account(id="direct", backend="eva_direct", address="eva@home.example", settings=base)
        )
        self.assertEqual(error, "", error)
        return record

    def test_internal_recipient_uses_the_internal_mta(self):
        plan, error = accounts_module.plan_direct_delivery(
            self.direct(), ["box@lab.internal"], [self.relay]
        )
        self.assertEqual(error, "")
        self.assertEqual(plan["routes"][0]["route"], "internal")
        self.assertEqual(plan["routes"][0]["smtp_host"], "mail.lab.internal")

    def test_external_recipient_uses_the_relay(self):
        plan, error = accounts_module.plan_direct_delivery(
            self.direct(), ["peer@company.example"], [self.relay]
        )
        self.assertEqual(error, "")
        self.assertEqual(plan["routes"][0]["route"], "relay")
        self.assertEqual(plan["routes"][0]["relay_account_id"], "owned")

    def test_mixed_recipients_split_across_both_routes(self):
        plan, error = accounts_module.plan_direct_delivery(
            self.direct(), ["box@lab.internal", "peer@company.example"], [self.relay]
        )
        self.assertEqual(error, "")
        routes = {route["route"]: route["recipients"] for route in plan["routes"]}
        self.assertEqual(routes["internal"], ["box@lab.internal"])
        self.assertEqual(routes["relay"], ["peer@company.example"])

    def test_internal_only_mode_refuses_external_recipients(self):
        plan, error = accounts_module.plan_direct_delivery(
            self.direct(delivery_mode="internal"), ["peer@company.example"], [self.relay]
        )
        self.assertIsNone(plan)
        self.assertIn("internal delivery only", error)

    def test_subdomain_of_internal_domain_stays_internal(self):
        plan, _ = accounts_module.plan_direct_delivery(
            self.direct(), ["box@rack.lab.internal"], [self.relay]
        )
        self.assertEqual(plan["routes"][0]["route"], "internal")

    def test_misaligned_relay_domain_is_refused(self):
        foreign, _ = accounts_module.normalize_account(
            account(id="owned", backend="gmail_oauth", address="me@gmail.com")
        )
        plan, error = accounts_module.plan_direct_delivery(
            self.direct(), ["peer@company.example"], [foreign]
        )
        self.assertIsNone(plan)
        self.assertIn("SPF and DMARC", error)

    def test_missing_relay_account_is_refused(self):
        plan, error = accounts_module.plan_direct_delivery(
            self.direct(), ["peer@company.example"], []
        )
        self.assertIsNone(plan)
        self.assertIn("no longer exists", error)

    def test_disconnected_relay_is_refused(self):
        self.relay["status"] = "needs_auth"
        plan, error = accounts_module.plan_direct_delivery(
            self.direct(), ["peer@company.example"], [self.relay]
        )
        self.assertIsNone(plan)
        self.assertIn("cannot send", error)

    def test_internal_mode_requires_a_configured_mta(self):
        record, error = accounts_module.normalize_account(account(
            id="direct", backend="eva_direct", address="eva@home.example",
            settings={"delivery_mode": "internal", "internal_domains": ["lab.internal"]},
        ))
        self.assertIsNone(record)
        self.assertIn("internal SMTP host", error)

    def test_relay_mode_requires_a_relay_account(self):
        record, error = accounts_module.normalize_account(account(
            id="direct", backend="eva_direct", address="eva@home.example",
            settings={"delivery_mode": "relay"},
        ))
        self.assertIsNone(record)
        self.assertIn("relay account", error)

    def test_plan_rejects_a_non_direct_account(self):
        plan, error = accounts_module.plan_direct_delivery(self.relay, ["peer@company.example"], [])
        self.assertIsNone(plan)
        self.assertIn("direct identity", error)

    def test_best_effort_local_mta_accepts_external_recipient_after_confirmation(self):
        direct, error = accounts_module.normalize_account(account(
            id="direct", backend="eva_direct", address="eva@home.example",
            settings={
                "delivery_mode": "local_mta",
                "internal_smtp_host": "127.0.0.1",
                "internal_smtp_port": 25,
                "direct_consent": [],
            },
        ))
        self.assertEqual(error, "")
        request = send(to="outside@example.net")
        pending = accounts_module.authorize_send_for_account(
            direct, request, ["outside@example.net"], accounts=[direct]
        )
        self.assertEqual(pending["decision"], "needs_confirmation")
        allowed = accounts_module.authorize_send_for_account(
            direct, request, ["outside@example.net"],
            {"digest": pending["digest"], "addresses": ["outside@example.net"]},
            accounts=[direct],
        )
        self.assertEqual(allowed["decision"], "allowed")
        self.assertEqual(allowed["delivery_plan"]["routes"][0]["route"], "local_mta")

    def test_best_effort_confirmation_is_bound_to_the_exact_message(self):
        direct, _ = accounts_module.normalize_account(account(
            id="direct", backend="eva_direct", address="eva@home.example",
            settings={"delivery_mode": "local_mta", "internal_smtp_host": "127.0.0.1"},
        ))
        request = send(to="outside@example.net")
        pending = accounts_module.authorize_send_for_account(
            direct, request, ["outside@example.net"], accounts=[direct]
        )
        changed = send(to="outside@example.net", body="Different body")
        result = accounts_module.authorize_send_for_account(
            direct, changed, ["outside@example.net"],
            {"digest": pending["digest"], "addresses": ["outside@example.net"]},
            accounts=[direct],
        )
        self.assertEqual(result["decision"], "needs_confirmation")

    def test_best_effort_confirmation_is_bound_to_all_recipient_fields(self):
        direct, _ = accounts_module.normalize_account(account(
            id="direct", backend="eva_direct", address="eva@home.example",
            settings={"delivery_mode": "local_mta", "internal_smtp_host": "127.0.0.1"},
        ))
        request = send(to="outside@example.net")
        pending = accounts_module.authorize_send_for_account(
            direct, request, ["outside@example.net"], accounts=[direct]
        )
        for field in ("to", "cc", "bcc"):
            with self.subTest(field=field):
                changed = send(to="outside@example.net")
                changed[field] = "other@example.net"
                result = accounts_module.authorize_send_for_account(
                    direct, changed, ["@example.net"],
                    {"digest": pending["digest"], "addresses": ["outside@example.net", "other@example.net"]},
                    accounts=[direct],
                )
                self.assertEqual(result["decision"], "needs_confirmation")

    def test_internal_mode_still_refuses_external_recipient(self):
        direct = self.direct(delivery_mode="internal")
        result = accounts_module.authorize_send_for_account(
            direct, send(to="peer@company.example"), ["peer@company.example"], accounts=[self.relay]
        )
        self.assertEqual(result["decision"], "rejected")

    def test_authorized_send_carries_the_delivery_plan(self):
        result = accounts_module.authorize_send_for_account(
            self.direct(), send(to="box@lab.internal"), ["@lab.internal"], accounts=[self.relay]
        )
        self.assertEqual(result["decision"], "allowed")
        self.assertEqual(result["delivery_plan"]["routes"][0]["route"], "internal")
        self.assertEqual(result["delivery_plan"]["from"], "eva@home.example")


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
        self.assertNotIn("hunter2", repr(record))
        credential_keys = {"password", "secret", "token", "credential", "app_password"}
        self.assertEqual(credential_keys & set(record), set())
        self.assertEqual(credential_keys & set(record["settings"]), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
