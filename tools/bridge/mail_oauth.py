"""Provider profiles and the loopback callback that completes a browser sign-in.

Google does not permit the OAuth device flow for mailbox scopes; its allowed
device scopes are limited to sign-in, Drive file/appdata, and YouTube. Google
directs desktop and command-line applications to the authorization-code flow
with PKCE and a loopback redirect, which is what Eva uses here and what
`bridge.oauth_client` implements.

Microsoft does support the device-code flow for mail scopes, so an Outlook or
Microsoft 365 account can be authorized without a browser redirect. That path
reuses the same token handling and is selected per provider.

The callback listener binds to 127.0.0.1 on an ephemeral port, accepts exactly
one request, and shuts down. It never writes the authorization code to disk or
to a log, and it returns a plain page containing no token material.
"""

import http.server
import socket
import threading
import urllib.parse

from bridge import oauth_client

# Public client identifiers only. These are not secrets, and PKCE is what
# actually protects the exchange; no client secret is stored or required.
PROVIDERS = {
    "google": {
        "label": "Google",
        "auth_style": "loopback",
        "issuer": "https://accounts.google.com",
        "scopes": ["https://mail.google.com/"],
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "auth_mechanism": "xoauth2",
        "authorization_params": {"access_type": "offline", "prompt": "consent"},
        "setup_note": (
            "Create a Desktop OAuth client in Google Cloud Console, enable the Gmail API, "
            "and add your address as a test user. Google does not allow device-code login "
            "for mailbox access."
        ),
    },
    "microsoft": {
        "label": "Microsoft",
        "auth_style": "device_code",
        "issuer": "https://login.microsoftonline.com/common/v2.0",
        "scopes": [
            "https://outlook.office.com/IMAP.AccessAsUser.All",
            "https://outlook.office.com/SMTP.Send",
            "offline_access",
        ],
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "auth_mechanism": "xoauth2",
        "setup_note": "Device-code sign-in is supported; no browser redirect is required.",
    },
}

CALLBACK_TIMEOUT_SECONDS = 300
_SUCCESS_BODY = b"<html><body><h3>Eva is connected.</h3><p>You can close this window.</p></body></html>"
_FAILURE_BODY = b"<html><body><h3>Sign-in did not complete.</h3><p>Return to Eva and try again.</p></body></html>"


def provider_profile(name):
    """Return a provider profile, or None when the provider is unknown."""
    return PROVIDERS.get(str(name or "").strip().lower())


def provider_for_address(address):
    """Return the provider name that serves an address domain."""
    domain = str(address or "").rsplit("@", 1)[-1].lower()
    if domain in {"gmail.com", "googlemail.com"}:
        return "google"
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        return "microsoft"
    return ""


def account_settings_for(provider_name):
    """Return IMAP/SMTP account settings implied by a provider profile."""
    profile = provider_profile(provider_name)
    if not profile:
        return {}
    return {
        "imap_host": profile["imap_host"],
        "imap_port": profile["imap_port"],
        "imap_tls": True,
        "smtp_host": profile["smtp_host"],
        "smtp_port": profile["smtp_port"],
        "smtp_starttls": True,
        "auth_mechanism": profile["auth_mechanism"],
    }


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        self.server.received_query = parsed.query
        body = _SUCCESS_BODY if "code=" in (parsed.query or "") else _FAILURE_BODY
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Suppress default logging: the query string carries an authorization code."""


class LoopbackCallbackServer:
    """One-shot loopback listener for an OAuth authorization redirect."""

    def __init__(self, host="127.0.0.1"):
        self._server = http.server.HTTPServer((host, 0), _CallbackHandler)
        self._server.received_query = None
        self._server.timeout = CALLBACK_TIMEOUT_SECONDS
        self._thread = None

    @property
    def port(self):
        return self._server.server_address[1]

    @property
    def redirect_uri(self):
        return f"http://127.0.0.1:{self.port}/callback"

    def start(self):
        self._thread = threading.Thread(target=self._server.handle_request, daemon=True)
        self._thread.start()
        return self

    def wait_for_code(self, expected_state, timeout=CALLBACK_TIMEOUT_SECONDS):
        """Block until the browser redirect arrives, then validate and return the code."""
        if self._thread:
            self._thread.join(timeout)
        query = getattr(self._server, "received_query", None)
        if not query:
            raise oauth_client.OAuthError("Sign-in did not complete before the timeout")
        return oauth_client.validate_callback(query, expected_state)

    def close(self):
        try:
            self._server.server_close()
        except (OSError, socket.error):
            pass


def begin_loopback_authorization(provider_name, client_id):
    """Prepare a browser authorization request. Returns (url, pending_state)."""
    profile = provider_profile(provider_name)
    if not profile:
        raise oauth_client.OAuthError("Unknown mail provider")
    if profile["auth_style"] != "loopback":
        raise oauth_client.OAuthError(f"{profile['label']} uses device-code sign-in")

    metadata = oauth_client.discover_authorization_server(profile["issuer"])
    server = LoopbackCallbackServer().start()
    verifier, challenge = oauth_client.generate_pkce()
    state = oauth_client.generate_state()
    try:
        url = oauth_client.build_authorization_url(
            metadata, client_id, server.redirect_uri, "",
            profile["scopes"], state, challenge,
            extra_params=profile.get("authorization_params"),
        )
    except oauth_client.OAuthError:
        server.close()
        raise
    return url, {
        "server": server,
        "state": state,
        "verifier": verifier,
        "metadata": metadata,
        "redirect_uri": server.redirect_uri,
        "resource": "",
    }


def complete_loopback_authorization(pending, client_id):
    """Wait for the redirect and exchange the code for tokens."""
    server = pending["server"]
    try:
        code = server.wait_for_code(pending["state"])
        return oauth_client.exchange_code(
            pending["metadata"], client_id, code, pending["verifier"],
            pending["redirect_uri"], pending["resource"],
        )
    finally:
        server.close()


def begin_device_authorization(provider_name, client_id):
    """Start device-code sign-in. Returns (authorization, metadata)."""
    profile = provider_profile(provider_name)
    if not profile:
        raise oauth_client.OAuthError("Unknown mail provider")
    if profile["auth_style"] != "device_code":
        raise oauth_client.OAuthError(
            f"{profile['label']} does not allow device-code sign-in for mailbox access"
        )
    metadata = oauth_client.discover_authorization_server(profile["issuer"])
    authorization = oauth_client.begin_device_authorization(
        metadata, client_id, profile["scopes"]
    )
    return authorization, metadata


def complete_device_authorization(metadata, client_id, authorization):
    """Poll until the user approves the device sign-in."""
    return oauth_client.poll_device_token(metadata, client_id, authorization)


def authorize(provider_name, client_id):
    """Run whichever sign-in style the provider supports."""
    profile = provider_profile(provider_name)
    if not profile:
        raise oauth_client.OAuthError("Unknown mail provider")
    if profile["auth_style"] == "device_code":
        authorization, metadata = begin_device_authorization(provider_name, client_id)
        return {"style": "device_code", "authorization": authorization, "metadata": metadata}
    url, pending = begin_loopback_authorization(provider_name, client_id)
    return {"style": "loopback", "url": url, "pending": pending}
