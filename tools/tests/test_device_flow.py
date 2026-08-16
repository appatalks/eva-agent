#!/usr/bin/env python3
"""Contract: device authorization grant (RFC 8628) for providers that allow it.

Every network call and every sleep is injected, so polling behaviour is tested
without waiting.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import mail_oauth
from bridge import oauth_client

ISSUER = "https://login.microsoftonline.com/common/v2.0"
METADATA = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "/authorize",
    "token_endpoint": ISSUER + "/token",
    "device_authorization_endpoint": ISSUER + "/devicecode",
}

DEVICE_RESPONSE = {
    "device_code": "DEV-CODE",
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://microsoft.com/devicelogin",
    "interval": 5,
    "expires_in": 900,
}


class Recorder:
    """Replays queued documents and records requests and sleeps."""

    def __init__(self, *documents):
        self.documents = list(documents)
        self.requests = []
        self.sleeps = []

    def fetch(self, url, method="GET", data=None, headers=None, timeout=None):
        self.requests.append({"url": url, "data": data})
        return self.documents.pop(0) if self.documents else {}

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def monotonic(self):
        return float(len(self.sleeps))


class CapabilityTests(unittest.TestCase):
    def test_metadata_without_device_endpoint_is_unsupported(self):
        self.assertFalse(oauth_client.supports_device_flow({"token_endpoint": "https://x/t"}))
        self.assertTrue(oauth_client.supports_device_flow(METADATA))

    def test_non_https_device_endpoint_is_unsupported(self):
        self.assertFalse(oauth_client.supports_device_flow(
            {"device_authorization_endpoint": "http://evil.test/devicecode"}
        ))

    def test_metadata_parsing_captures_the_device_endpoint(self):
        parsed = oauth_client.parse_authorization_server_metadata(METADATA, ISSUER)
        self.assertEqual(parsed["device_authorization_endpoint"], ISSUER + "/devicecode")


class BeginDeviceAuthorizationTests(unittest.TestCase):
    def test_returns_codes_and_polling_schedule(self):
        recorder = Recorder(DEVICE_RESPONSE)
        result = oauth_client.begin_device_authorization(
            METADATA, "client-1", ["IMAP.AccessAsUser.All"], fetch=recorder.fetch
        )
        self.assertEqual(result["user_code"], "ABCD-EFGH")
        self.assertEqual(result["verification_uri"], "https://microsoft.com/devicelogin")
        self.assertEqual(result["interval"], 5)
        self.assertEqual(recorder.requests[0]["data"]["scope"], "IMAP.AccessAsUser.All")

    def test_provider_without_device_support_is_refused(self):
        with self.assertRaises(oauth_client.OAuthError):
            oauth_client.begin_device_authorization(
                {"token_endpoint": "https://x/t"}, "client-1", [], fetch=Recorder().fetch
            )

    def test_incomplete_response_is_refused(self):
        recorder = Recorder({"user_code": "ABCD"})
        with self.assertRaises(oauth_client.OAuthError):
            oauth_client.begin_device_authorization(METADATA, "client-1", [], fetch=recorder.fetch)

    def test_non_https_verification_url_is_refused(self):
        recorder = Recorder(dict(DEVICE_RESPONSE, verification_uri="http://evil.test"))
        with self.assertRaises(oauth_client.OAuthError):
            oauth_client.begin_device_authorization(METADATA, "client-1", [], fetch=recorder.fetch)

    def test_interval_is_clamped_to_a_sane_range(self):
        recorder = Recorder(dict(DEVICE_RESPONSE, interval=99999))
        result = oauth_client.begin_device_authorization(METADATA, "client-1", [], fetch=recorder.fetch)
        self.assertLessEqual(result["interval"], oauth_client.DEVICE_MAX_INTERVAL_SECONDS)

    def test_missing_client_id_is_refused(self):
        with self.assertRaises(oauth_client.OAuthError):
            oauth_client.begin_device_authorization(METADATA, "", [], fetch=Recorder().fetch)


class PollDeviceTokenTests(unittest.TestCase):
    def authorization(self, **overrides):
        base = {"device_code": "DEV-CODE", "interval": 5, "expires_in": 900}
        base.update(overrides)
        return base

    def test_pending_then_granted(self):
        recorder = Recorder(
            {"error": "authorization_pending"},
            {"error": "authorization_pending"},
            {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
        )
        token = oauth_client.poll_device_token(
            METADATA, "client-1", self.authorization(),
            fetch=recorder.fetch, sleep=recorder.sleep, now=0, monotonic=recorder.monotonic,
        )
        self.assertEqual(token["access_token"], "at")
        self.assertEqual(token["expires_at"], 3600)
        self.assertEqual(len(recorder.requests), 3)

    def test_slow_down_increases_the_interval(self):
        recorder = Recorder(
            {"error": "slow_down"},
            {"access_token": "at", "expires_in": 60},
        )
        oauth_client.poll_device_token(
            METADATA, "client-1", self.authorization(),
            fetch=recorder.fetch, sleep=recorder.sleep, now=0, monotonic=recorder.monotonic,
        )
        self.assertGreater(recorder.sleeps[1], recorder.sleeps[0])

    def test_access_denied_is_terminal(self):
        recorder = Recorder({"error": "access_denied"})
        with self.assertRaises(oauth_client.OAuthError) as caught:
            oauth_client.poll_device_token(
                METADATA, "client-1", self.authorization(),
                fetch=recorder.fetch, sleep=recorder.sleep, monotonic=recorder.monotonic,
            )
        self.assertIn("refused", str(caught.exception))

    def test_expired_token_is_terminal(self):
        recorder = Recorder({"error": "expired_token"})
        with self.assertRaises(oauth_client.OAuthError):
            oauth_client.poll_device_token(
                METADATA, "client-1", self.authorization(),
                fetch=recorder.fetch, sleep=recorder.sleep, monotonic=recorder.monotonic,
            )

    def test_polling_stops_at_the_deadline(self):
        recorder = Recorder(*([{"error": "authorization_pending"}] * 50))
        with self.assertRaises(oauth_client.OAuthError) as caught:
            oauth_client.poll_device_token(
                METADATA, "client-1", self.authorization(expires_in=3),
                fetch=recorder.fetch, sleep=recorder.sleep, monotonic=recorder.monotonic,
            )
        self.assertIn("expired", str(caught.exception))
        self.assertLess(len(recorder.requests), 10)

    def test_missing_device_code_is_refused(self):
        with self.assertRaises(oauth_client.OAuthError):
            oauth_client.poll_device_token(METADATA, "client-1", {}, fetch=Recorder().fetch)

    def test_unexpected_error_is_surfaced_without_leaking_the_body(self):
        recorder = Recorder({"error": "invalid_client", "error_description": "secret detail"})
        with self.assertRaises(oauth_client.OAuthError) as caught:
            oauth_client.poll_device_token(
                METADATA, "client-1", self.authorization(),
                fetch=recorder.fetch, sleep=recorder.sleep, monotonic=recorder.monotonic,
            )
        self.assertIn("invalid_client", str(caught.exception))
        self.assertNotIn("secret detail", str(caught.exception))

    def test_device_code_is_sent_with_the_documented_grant_type(self):
        recorder = Recorder({"access_token": "at", "expires_in": 60})
        oauth_client.poll_device_token(
            METADATA, "client-1", self.authorization(),
            fetch=recorder.fetch, sleep=recorder.sleep, monotonic=recorder.monotonic,
        )
        sent = recorder.requests[0]["data"]
        self.assertEqual(sent["grant_type"], "urn:ietf:params:oauth:grant-type:device_code")
        self.assertEqual(sent["device_code"], "DEV-CODE")


class ProviderDispatchTests(unittest.TestCase):
    def test_google_refuses_device_sign_in(self):
        with self.assertRaises(oauth_client.OAuthError) as caught:
            mail_oauth.begin_device_authorization("google", "client-1")
        self.assertIn("does not allow device-code", str(caught.exception))

    def test_microsoft_is_configured_for_device_sign_in(self):
        self.assertEqual(mail_oauth.provider_profile("microsoft")["auth_style"], "device_code")

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(oauth_client.OAuthError):
            mail_oauth.begin_device_authorization("yahoo", "client-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
