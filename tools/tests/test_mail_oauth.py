#!/usr/bin/env python3
"""Contract: provider profiles, XOAUTH2, and the loopback sign-in callback."""

import base64
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import mail_oauth
from bridge import mailbox_imap
from bridge import oauth_client


class ProviderProfileTests(unittest.TestCase):
    def test_google_requires_the_browser_flow(self):
        profile = mail_oauth.provider_profile("google")
        self.assertEqual(profile["auth_style"], "loopback")
        self.assertIn("https://mail.google.com/", profile["scopes"])

    def test_microsoft_supports_device_code(self):
        self.assertEqual(mail_oauth.provider_profile("microsoft")["auth_style"], "device_code")

    def test_provider_is_detected_from_the_address(self):
        self.assertEqual(mail_oauth.provider_for_address("someone@gmail.com"), "google")
        self.assertEqual(mail_oauth.provider_for_address("someone@outlook.com"), "microsoft")
        self.assertEqual(mail_oauth.provider_for_address("someone@custom.example"), "")

    def test_unknown_provider_returns_nothing(self):
        self.assertIsNone(mail_oauth.provider_profile("yahoo"))
        self.assertEqual(mail_oauth.account_settings_for("yahoo"), {})

    def test_account_settings_select_oauth_not_password(self):
        settings = mail_oauth.account_settings_for("google")
        self.assertEqual(settings["imap_host"], "imap.gmail.com")
        self.assertEqual(settings["auth_mechanism"], "xoauth2")
        self.assertTrue(settings["imap_tls"])

    def test_google_requests_offline_access_for_a_refresh_token(self):
        params = mail_oauth.provider_profile("google")["authorization_params"]
        self.assertEqual(params["access_type"], "offline")

    def test_no_client_secret_is_stored_in_any_profile(self):
        for name, profile in mail_oauth.PROVIDERS.items():
            self.assertNotIn("client_secret", profile, name)


class Xoauth2Tests(unittest.TestCase):
    def test_sasl_string_matches_the_documented_format(self):
        value = mailbox_imap.xoauth2_string("someone@gmail.com", "ya29.token")
        self.assertEqual(value, "user=someone@gmail.com\x01auth=Bearer ya29.token\x01\x01")

    def test_base64_form_is_decodable(self):
        value = mailbox_imap.xoauth2_string("someone@gmail.com", "ya29.token")
        encoded = base64.b64encode(value.encode()).decode()
        self.assertIn("auth=Bearer", base64.b64decode(encoded).decode())

    def test_missing_token_or_address_is_refused(self):
        for address, token in [("", "t"), ("a@b.com", ""), ("", "")]:
            with self.assertRaises(mailbox_imap.MailboxError):
                mailbox_imap.xoauth2_string(address, token)

    def test_mechanism_defaults_to_password(self):
        box = mailbox_imap.ImapSmtpMailbox({"imap_host": "h", "smtp_host": "h"}, "a@b.com")
        self.assertEqual(box.auth_mechanism, "password")

    def test_unknown_mechanism_falls_back_to_password(self):
        box = mailbox_imap.ImapSmtpMailbox(
            {"imap_host": "h", "smtp_host": "h", "auth_mechanism": "magic"}, "a@b.com"
        )
        self.assertEqual(box.auth_mechanism, "password")

    def test_xoauth2_mechanism_is_selected(self):
        box = mailbox_imap.ImapSmtpMailbox(
            {"imap_host": "h", "smtp_host": "h", "auth_mechanism": "xoauth2"}, "a@b.com"
        )
        self.assertEqual(box.auth_mechanism, "xoauth2")


class FakeIMAP:
    def __init__(self):
        self.authenticated = None
        self.logged_in = None

    def authenticate(self, mechanism, authobject):
        self.authenticated = (mechanism, authobject(b"").decode())

    def login(self, user, password):
        self.logged_in = (user, password)


class AuthenticationDispatchTests(unittest.TestCase):
    def test_xoauth2_account_authenticates_with_a_token(self):
        box = mailbox_imap.ImapSmtpMailbox(
            {"imap_host": "h", "smtp_host": "h", "auth_mechanism": "xoauth2"}, "a@b.com"
        )
        connection = FakeIMAP()
        box._imap_authenticate(connection, "ya29.token")
        self.assertEqual(connection.authenticated[0], "XOAUTH2")
        self.assertIn("auth=Bearer ya29.token", connection.authenticated[1])
        self.assertIsNone(connection.logged_in)

    def test_password_account_uses_login(self):
        box = mailbox_imap.ImapSmtpMailbox({"imap_host": "h", "smtp_host": "h"}, "a@b.com")
        connection = FakeIMAP()
        box._imap_authenticate(connection, "app-password")
        self.assertEqual(connection.logged_in, ("a@b.com", "app-password"))
        self.assertIsNone(connection.authenticated)


class AuthorizationUrlTests(unittest.TestCase):
    METADATA = {
        "issuer": "https://accounts.google.com",
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
    }

    def test_resource_is_omitted_when_empty(self):
        url = oauth_client.build_authorization_url(
            self.METADATA, "client", "http://127.0.0.1:1234/callback", "",
            ["https://mail.google.com/"], "state", "challenge",
        )
        self.assertNotIn("resource=", url)
        self.assertIn("code_challenge_method=S256", url)

    def test_extra_params_are_included(self):
        url = oauth_client.build_authorization_url(
            self.METADATA, "client", "http://127.0.0.1:1234/callback", "",
            [], "state", "challenge", extra_params={"access_type": "offline"},
        )
        self.assertIn("access_type=offline", url)

    def test_extra_params_cannot_override_security_parameters(self):
        url = oauth_client.build_authorization_url(
            self.METADATA, "client", "http://127.0.0.1:1234/callback", "",
            [], "state", "challenge",
            extra_params={"code_challenge_method": "plain", "state": "attacker"},
        )
        self.assertIn("code_challenge_method=S256", url)
        self.assertNotIn("plain", url)
        self.assertNotIn("attacker", url)

    def test_non_https_resource_is_still_refused(self):
        with self.assertRaises(oauth_client.OAuthError):
            oauth_client.build_authorization_url(
                self.METADATA, "client", "http://127.0.0.1:1234/callback", "http://evil.test",
                [], "state", "challenge",
            )


class LoopbackCallbackTests(unittest.TestCase):
    def test_callback_binds_loopback_and_yields_the_code(self):
        server = mail_oauth.LoopbackCallbackServer().start()
        self.addCleanup(server.close)
        self.assertTrue(oauth_client.is_loopback_redirect(server.redirect_uri))

        def visit():
            urllib.request.urlopen(server.redirect_uri + "?code=abc123&state=s1", timeout=5).read()

        threading.Thread(target=visit, daemon=True).start()
        self.assertEqual(server.wait_for_code("s1", timeout=10), "abc123")

    def test_state_mismatch_is_rejected(self):
        server = mail_oauth.LoopbackCallbackServer().start()
        self.addCleanup(server.close)

        def visit():
            urllib.request.urlopen(server.redirect_uri + "?code=abc123&state=wrong", timeout=5).read()

        threading.Thread(target=visit, daemon=True).start()
        with self.assertRaises(oauth_client.OAuthError):
            server.wait_for_code("s1", timeout=10)

    def test_response_page_contains_no_token_material(self):
        server = mail_oauth.LoopbackCallbackServer().start()
        self.addCleanup(server.close)
        holder = {}

        def visit():
            holder["body"] = urllib.request.urlopen(
                server.redirect_uri + "?code=secret-code&state=s1", timeout=5
            ).read().decode()

        thread = threading.Thread(target=visit, daemon=True)
        thread.start()
        server.wait_for_code("s1", timeout=10)
        thread.join(5)
        self.assertNotIn("secret-code", holder.get("body", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
