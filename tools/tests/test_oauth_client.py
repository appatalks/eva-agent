#!/usr/bin/env python3
"""Contract: OAuth authorization safety for remote MCP servers.

Every network call is stubbed. These checks protect PKCE enforcement, state
validation, loopback-only redirects, HTTPS-only discovery, and token redaction.
"""

import base64
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import oauth_client as oauth

RESOURCE = "https://workiq.svc.cloud.microsoft/mcp"
ISSUER = "https://login.microsoftonline.com/common/v2.0"
REDIRECT = "http://127.0.0.1:53682/callback"

SERVER_METADATA = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "/authorize",
    "token_endpoint": ISSUER + "/token",
    "code_challenge_methods_supported": ["S256"],
}


def stub(*documents):
    """Return a fetch callable that yields the given documents in order."""
    queue = list(documents)
    calls = []

    def fetch(url, method="GET", data=None, headers=None, timeout=None):
        calls.append({"url": url, "method": method, "data": data})
        return queue.pop(0) if queue else {}

    fetch.calls = calls
    return fetch


class PkceTests(unittest.TestCase):
    def test_challenge_is_the_s256_of_the_verifier(self):
        verifier, challenge = oauth.generate_pkce()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(challenge, expected)

    def test_verifier_length_is_within_the_spec_range(self):
        verifier, _ = oauth.generate_pkce()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertLessEqual(len(verifier), 128)

    def test_each_pair_is_unique(self):
        self.assertNotEqual(oauth.generate_pkce()[0], oauth.generate_pkce()[0])


class RedirectTests(unittest.TestCase):
    def test_accepts_loopback_with_explicit_port(self):
        self.assertTrue(oauth.is_loopback_redirect(REDIRECT))

    def test_rejects_non_loopback_and_ambiguous_hosts(self):
        for value in [
            "http://localhost:53682/callback",
            "http://0.0.0.0:53682/callback",
            "http://example.com:53682/callback",
            "https://127.0.0.1:53682/callback",
            "http://127.0.0.1/callback",
            "http://user:pw@127.0.0.1:53682/callback",
            "http://127.0.0.1:53682/callback?next=https://evil.test",
            "",
        ]:
            self.assertFalse(oauth.is_loopback_redirect(value), value)


class ProtectedResourceMetadataTests(unittest.TestCase):
    def test_accepts_valid_metadata(self):
        parsed = oauth.parse_protected_resource_metadata(
            {"resource": RESOURCE, "authorization_servers": [ISSUER]}, RESOURCE
        )
        self.assertEqual(parsed["resource"], RESOURCE)
        self.assertEqual(parsed["authorization_servers"], [ISSUER])

    def test_rejects_non_https_authorization_server(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.parse_protected_resource_metadata(
                {"resource": RESOURCE, "authorization_servers": ["http://evil.test"]}, RESOURCE
            )

    def test_rejects_metadata_from_a_different_origin(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.parse_protected_resource_metadata(
                {"resource": "https://evil.test/mcp", "authorization_servers": [ISSUER]}, RESOURCE
            )

    def test_rejects_document_without_authorization_server(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.parse_protected_resource_metadata({"resource": RESOURCE}, RESOURCE)


class AuthorizationServerMetadataTests(unittest.TestCase):
    def test_accepts_valid_metadata(self):
        parsed = oauth.parse_authorization_server_metadata(SERVER_METADATA, ISSUER)
        self.assertEqual(parsed["token_endpoint"], ISSUER + "/token")

    def test_refuses_a_server_without_s256(self):
        document = dict(SERVER_METADATA, code_challenge_methods_supported=["plain"])
        with self.assertRaises(oauth.OAuthError):
            oauth.parse_authorization_server_metadata(document, ISSUER)

    def test_refuses_non_https_endpoints(self):
        for field in ("authorization_endpoint", "token_endpoint"):
            document = dict(SERVER_METADATA, **{field: "http://evil.test/x"})
            with self.assertRaises(oauth.OAuthError):
                oauth.parse_authorization_server_metadata(document, ISSUER)

    def test_refuses_issuer_mismatch(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.parse_authorization_server_metadata(SERVER_METADATA, "https://evil.test")

    def test_discovery_falls_back_to_openid_configuration(self):
        def failing(url, **kwargs):
            if url.endswith("/.well-known/oauth-authorization-server"):
                return {}
            return SERVER_METADATA

        parsed = oauth.discover_authorization_server(ISSUER, fetch=failing)
        self.assertEqual(parsed["issuer"], ISSUER)


class AuthorizationUrlTests(unittest.TestCase):
    def test_includes_pkce_state_and_resource(self):
        url = oauth.build_authorization_url(
            SERVER_METADATA, "client-1", REDIRECT, RESOURCE, ["Mail.Read"], "state-1", "challenge-1"
        )
        for fragment in [
            "response_type=code",
            "code_challenge=challenge-1",
            "code_challenge_method=S256",
            "state=state-1",
            "scope=Mail.Read",
            "resource=https%3A%2F%2Fworkiq.svc.cloud.microsoft%2Fmcp",
        ]:
            self.assertIn(fragment, url, fragment)

    def test_refuses_a_non_loopback_redirect(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.build_authorization_url(
                SERVER_METADATA, "client-1", "https://evil.test/cb", RESOURCE, [], "s", "c"
            )

    def test_refuses_a_non_https_resource(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.build_authorization_url(
                SERVER_METADATA, "client-1", REDIRECT, "http://evil.test", [], "s", "c"
            )


class CallbackTests(unittest.TestCase):
    def test_returns_code_when_state_matches(self):
        self.assertEqual(oauth.validate_callback("code=abc&state=s1", "s1"), "abc")

    def test_rejects_mismatched_or_missing_state(self):
        for query, expected in [("code=abc&state=other", "s1"), ("code=abc", "s1"), ("code=abc&state=s1", "")]:
            with self.assertRaises(oauth.OAuthError):
                oauth.validate_callback(query, expected)

    def test_surfaces_an_authorization_error(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.validate_callback("error=access_denied&state=s1", "s1")

    def test_rejects_callback_without_a_code(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.validate_callback("state=s1", "s1")


class TokenTests(unittest.TestCase):
    def test_normalizes_expiry_to_an_absolute_time(self):
        token = oauth.normalize_token_response(
            {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}, now=1000
        )
        self.assertEqual(token["expires_at"], 4600)

    def test_rejects_a_non_bearer_or_empty_token(self):
        for document in [{"access_token": "at", "token_type": "mac"}, {"token_type": "Bearer"}]:
            with self.assertRaises(oauth.OAuthError):
                oauth.normalize_token_response(document)

    def test_refresh_is_requested_before_actual_expiry(self):
        token = {"access_token": "at", "expires_at": 5000}
        self.assertFalse(oauth.token_needs_refresh(token, now=4000))
        self.assertTrue(oauth.token_needs_refresh(token, now=4900))

    def test_missing_token_always_needs_refresh(self):
        self.assertTrue(oauth.token_needs_refresh(None))
        self.assertTrue(oauth.token_needs_refresh({}))

    def test_refresh_keeps_the_previous_refresh_token_when_absent(self):
        fetch = stub({"access_token": "new", "expires_in": 60})
        token = oauth.refresh_access_token(SERVER_METADATA, "client-1", "rt", RESOURCE, fetch=fetch, now=0)
        self.assertEqual(token["refresh_token"], "rt")
        self.assertEqual(fetch.calls[0]["data"]["resource"], RESOURCE)

    def test_refresh_without_a_token_raises_rather_than_silently_failing(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.refresh_access_token(SERVER_METADATA, "client-1", "", RESOURCE, fetch=stub())

    def test_exchange_sends_verifier_and_resource(self):
        fetch = stub({"access_token": "at", "expires_in": 60})
        oauth.exchange_code(SERVER_METADATA, "client-1", "code", "verifier", REDIRECT, RESOURCE, fetch=fetch, now=0)
        sent = fetch.calls[0]["data"]
        self.assertEqual(sent["code_verifier"], "verifier")
        self.assertEqual(sent["resource"], RESOURCE)
        self.assertEqual(sent["grant_type"], "authorization_code")

    def test_redaction_never_exposes_secret_material(self):
        summary = oauth.redact_token_fields({"access_token": "super-secret", "refresh_token": "also-secret"})
        serialized = repr(summary)
        self.assertNotIn("super-secret", serialized)
        self.assertNotIn("also-secret", serialized)
        self.assertTrue(summary["present"])
        self.assertTrue(summary["has_refresh"])


class ChallengeHeaderTests(unittest.TestCase):
    def test_extracts_quoted_resource_metadata_url(self):
        header = f'Bearer realm="mcp", resource_metadata="{RESOURCE}/.well-known/oauth-protected-resource"'
        self.assertTrue(oauth.parse_www_authenticate_resource(header).startswith("https://"))

    def test_ignores_non_https_or_absent_metadata(self):
        self.assertEqual(oauth.parse_www_authenticate_resource('Bearer resource_metadata="http://evil.test"'), "")
        self.assertEqual(oauth.parse_www_authenticate_resource("Bearer realm=mcp"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
