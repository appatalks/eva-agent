"""OAuth 2.1 client for remote MCP servers, used by Eva's Work IQ backend.

Network access is injected so every security decision below can be tested
offline. The rules enforced here follow the MCP authorization spec and RFC 8707:

- PKCE is mandatory and only S256 is accepted; a server that offers `plain` is
  refused rather than downgraded.
- The redirect URI must be a loopback address on the local machine. Eva never
  registers an externally reachable callback.
- `state` is single-use and compared in constant time.
- Discovered issuers, authorization endpoints, and token endpoints must be
  HTTPS, and the issuer must match the document that advertised it.
- The `resource` indicator is always sent so a token minted for Work IQ cannot
  be replayed against a different resource server.

Tokens never enter logs, telemetry, audit records, or exception messages.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

PKCE_METHOD = "S256"
DEFAULT_TIMEOUT = 20
MAX_METADATA_BYTES = 256 * 1024
TOKEN_EXPIRY_SKEW_SECONDS = 120
LOOPBACK_HOSTS = {"127.0.0.1", "[::1]", "::1"}


class OAuthError(Exception):
    """Raised when an authorization step cannot be completed safely."""


def _is_https_url(value):
    try:
        parsed = urllib.parse.urlparse(str(value or ""))
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.fragment


def _same_origin(first, second):
    try:
        a = urllib.parse.urlparse(str(first or ""))
        b = urllib.parse.urlparse(str(second or ""))
    except ValueError:
        return False
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


def is_loopback_redirect(value):
    """Return True only for a loopback HTTP callback Eva can host locally."""
    try:
        parsed = urllib.parse.urlparse(str(value or ""))
    except ValueError:
        return False
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    host = parsed.hostname
    if host is None:
        return False
    if host.lower() == "localhost":
        return False  # name resolution is attacker-influenceable; require a literal address
    if host not in LOOPBACK_HOSTS and host.strip("[]") not in LOOPBACK_HOSTS:
        return False
    return bool(parsed.port)


def generate_pkce():
    """Return a fresh (verifier, challenge) pair using S256."""
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode("ascii").rstrip("=")[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def generate_state():
    """Return a single-use CSRF state value."""
    return secrets.token_urlsafe(32)


def parse_www_authenticate_resource(header):
    """Return the `resource_metadata` URL advertised by a 401 challenge."""
    text = str(header or "")
    marker = "resource_metadata="
    index = text.find(marker)
    if index < 0:
        return ""
    value = text[index + len(marker):].strip()
    if value.startswith('"'):
        end = value.find('"', 1)
        value = value[1:end] if end > 0 else ""
    else:
        value = value.split(",", 1)[0].strip()
    return value if _is_https_url(value) else ""


def parse_protected_resource_metadata(document, metadata_url=""):
    """Validate `/.well-known/oauth-protected-resource` output."""
    if not isinstance(document, dict):
        raise OAuthError("Protected resource metadata must be a JSON object")
    resource = str(document.get("resource") or "")
    if not _is_https_url(resource):
        raise OAuthError("Protected resource metadata has no HTTPS resource identifier")
    if metadata_url and not _same_origin(metadata_url, resource):
        raise OAuthError("Protected resource metadata does not match its own origin")
    servers = [s for s in document.get("authorization_servers") or [] if _is_https_url(s)]
    if not servers:
        raise OAuthError("Protected resource metadata lists no HTTPS authorization server")
    scopes = [str(s)[:128] for s in document.get("scopes_supported") or [] if str(s or "").strip()]
    return {"resource": resource, "authorization_servers": servers[:5], "scopes_supported": scopes[:50]}


def parse_authorization_server_metadata(document, expected_issuer=""):
    """Validate authorization server metadata and refuse unsafe configurations."""
    if not isinstance(document, dict):
        raise OAuthError("Authorization server metadata must be a JSON object")
    issuer = str(document.get("issuer") or "")
    if not _is_https_url(issuer):
        raise OAuthError("Authorization server metadata has no HTTPS issuer")
    if expected_issuer and not _same_origin(expected_issuer, issuer):
        raise OAuthError("Authorization server issuer does not match the advertised server")
    authorization_endpoint = str(document.get("authorization_endpoint") or "")
    token_endpoint = str(document.get("token_endpoint") or "")
    if not _is_https_url(authorization_endpoint):
        raise OAuthError("Authorization endpoint must be HTTPS")
    if not _is_https_url(token_endpoint):
        raise OAuthError("Token endpoint must be HTTPS")
    methods = [str(m) for m in document.get("code_challenge_methods_supported") or []]
    if methods and PKCE_METHOD not in methods:
        raise OAuthError("Authorization server does not support PKCE S256")
    return {
        "issuer": issuer,
        "authorization_endpoint": authorization_endpoint,
        "token_endpoint": token_endpoint,
        "device_authorization_endpoint": str(document.get("device_authorization_endpoint") or ""),
        "registration_endpoint": str(document.get("registration_endpoint") or ""),
        "code_challenge_methods_supported": methods,
    }


def build_authorization_url(metadata, client_id, redirect_uri, resource, scopes, state, challenge, extra_params=None):
    """Return the browser URL that starts an authorization-code+PKCE flow.

    `resource` is the RFC 8707 indicator. It is omitted when empty, because some
    providers (Google among them) do not implement it; when supplied it must be
    HTTPS so a token cannot be minted for an unverified audience.
    """
    if not is_loopback_redirect(redirect_uri):
        raise OAuthError("Redirect URI must be a loopback address with an explicit port")
    if not str(client_id or "").strip():
        raise OAuthError("Client id is required")
    if resource and not _is_https_url(resource):
        raise OAuthError("Resource indicator must be HTTPS")
    query = {
        "response_type": "code",
        "client_id": str(client_id),
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": PKCE_METHOD,
    }
    if resource:
        query["resource"] = resource
    scope = " ".join(str(s).strip() for s in scopes or [] if str(s or "").strip())
    if scope:
        query["scope"] = scope
    for key, value in (extra_params or {}).items():
        safe_key = str(key).strip()
        if safe_key and safe_key not in query:
            query[safe_key] = str(value)
    endpoint = metadata["authorization_endpoint"]
    separator = "&" if urllib.parse.urlparse(endpoint).query else "?"
    return endpoint + separator + urllib.parse.urlencode(query)


def validate_callback(raw_query, expected_state):
    """Return the authorization code from a callback, or raise on any mismatch."""
    params = urllib.parse.parse_qs(str(raw_query or ""), keep_blank_values=True)
    received_state = (params.get("state") or [""])[0]
    if not expected_state or not hmac.compare_digest(str(received_state), str(expected_state)):
        raise OAuthError("Authorization callback state did not match")
    error = (params.get("error") or [""])[0]
    if error:
        raise OAuthError(f"Authorization was refused: {str(error)[:80]}")
    code = (params.get("code") or [""])[0]
    if not code:
        raise OAuthError("Authorization callback contained no code")
    return code


def normalize_token_response(document, now=None):
    """Validate a token response and compute its absolute expiry."""
    if not isinstance(document, dict):
        raise OAuthError("Token response must be a JSON object")
    access_token = str(document.get("access_token") or "")
    if not access_token:
        raise OAuthError("Token response contained no access token")
    token_type = str(document.get("token_type") or "Bearer")
    if token_type.lower() != "bearer":
        raise OAuthError("Only bearer tokens are supported")
    try:
        expires_in = int(document.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    current = float(now if now is not None else time.time())
    return {
        "access_token": access_token,
        "refresh_token": str(document.get("refresh_token") or ""),
        "expires_at": current + expires_in if expires_in > 0 else 0.0,
        "scope": str(document.get("scope") or ""),
    }


def token_needs_refresh(token, now=None):
    """Return True when a stored token is missing, expired, or close to expiry."""
    if not isinstance(token, dict) or not token.get("access_token"):
        return True
    expires_at = float(token.get("expires_at") or 0)
    if expires_at <= 0:
        return False
    current = float(now if now is not None else time.time())
    return current >= expires_at - TOKEN_EXPIRY_SKEW_SECONDS


def redact_token_fields(token):
    """Return token metadata safe for diagnostics. Never returns secret material."""
    if not isinstance(token, dict):
        return {"present": False}
    return {
        "present": bool(token.get("access_token")),
        "has_refresh": bool(token.get("refresh_token")),
        "expires_at": float(token.get("expires_at") or 0),
        "scope": str(token.get("scope") or "")[:200],
    }


def _http_json(url, method="GET", data=None, headers=None, timeout=DEFAULT_TIMEOUT):
    """Perform one HTTPS JSON request. Only HTTPS URLs are permitted."""
    if not _is_https_url(url):
        raise OAuthError("Refusing a non-HTTPS authorization request")
    body = None
    request_headers = {"Accept": "application/json"}
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    request_headers.update(headers or {})
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_METADATA_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read(MAX_METADATA_BYTES).decode("utf-8", "replace")).get("error", "")
        except (ValueError, OSError):
            detail = ""
        raise OAuthError(f"Authorization request failed with HTTP {exc.code}{': ' + str(detail)[:80] if detail else ''}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise OAuthError(f"Authorization request could not reach the server: {type(exc).__name__}") from None
    if len(payload) > MAX_METADATA_BYTES:
        raise OAuthError("Authorization response exceeded the safe size limit")
    try:
        return json.loads(payload.decode("utf-8", "replace"))
    except ValueError:
        raise OAuthError("Authorization response was not valid JSON") from None


def discover_protected_resource(metadata_url, fetch=None):
    """Fetch and validate protected-resource metadata for a remote MCP server."""
    fetch = fetch or _http_json
    document = fetch(metadata_url)
    return parse_protected_resource_metadata(document, metadata_url)


def discover_authorization_server(issuer, fetch=None):
    """Fetch authorization server metadata, trying OAuth then OpenID discovery."""
    fetch = fetch or _http_json
    base = str(issuer or "").rstrip("/")
    last_error = None
    for suffix in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        try:
            return parse_authorization_server_metadata(fetch(base + suffix), issuer)
        except OAuthError as exc:
            last_error = exc
    raise last_error or OAuthError("Authorization server metadata is unavailable")


def exchange_code(metadata, client_id, code, verifier, redirect_uri, resource, fetch=None, now=None):
    """Exchange an authorization code for tokens."""
    fetch = fetch or _http_json
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
    }
    if resource:
        payload["resource"] = resource
    document = fetch(metadata["token_endpoint"], method="POST", data=payload)
    return normalize_token_response(document, now=now)


def refresh_access_token(metadata, client_id, refresh_token, resource, fetch=None, now=None):
    """Exchange a refresh token for a new access token."""
    if not str(refresh_token or ""):
        raise OAuthError("No refresh token is available; re-authorization is required")
    fetch = fetch or _http_json
    payload = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    if resource:
        payload["resource"] = resource
    document = fetch(metadata["token_endpoint"], method="POST", data=payload)
    token = normalize_token_response(document, now=now)
    if not token["refresh_token"]:
        token["refresh_token"] = str(refresh_token)
    return token


# ── Device authorization grant (RFC 8628) ──────────────────────────────
#
# Used where the provider supports it for mail scopes. Microsoft does; Google
# restricts its device flow to sign-in, Drive file/appdata, and YouTube scopes,
# so Gmail must use the loopback redirect instead.

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEVICE_MIN_INTERVAL_SECONDS = 5
DEVICE_MAX_INTERVAL_SECONDS = 60
DEVICE_MAX_WAIT_SECONDS = 900


def supports_device_flow(metadata):
    """Return True when the authorization server advertises a device endpoint."""
    return _is_https_url((metadata or {}).get("device_authorization_endpoint"))


def begin_device_authorization(metadata, client_id, scopes, fetch=None):
    """Request a device and user code. Returns the codes and polling schedule."""
    endpoint = (metadata or {}).get("device_authorization_endpoint") or ""
    if not _is_https_url(endpoint):
        raise OAuthError("This provider does not offer device-code sign-in")
    if not str(client_id or "").strip():
        raise OAuthError("Client id is required")

    fetch = fetch or _http_json
    scope = " ".join(str(s).strip() for s in scopes or [] if str(s or "").strip())
    document = fetch(endpoint, method="POST", data={"client_id": client_id, "scope": scope})
    if not isinstance(document, dict):
        raise OAuthError("Device authorization response was not a JSON object")

    device_code = str(document.get("device_code") or "")
    user_code = str(document.get("user_code") or "")
    verification_uri = str(
        document.get("verification_uri") or document.get("verification_url") or ""
    )
    if not device_code or not user_code:
        raise OAuthError("Device authorization response was incomplete")
    if not _is_https_url(verification_uri):
        raise OAuthError("Device verification URL must be HTTPS")

    try:
        interval = int(document.get("interval") or DEVICE_MIN_INTERVAL_SECONDS)
    except (TypeError, ValueError):
        interval = DEVICE_MIN_INTERVAL_SECONDS
    try:
        expires_in = int(document.get("expires_in") or DEVICE_MAX_WAIT_SECONDS)
    except (TypeError, ValueError):
        expires_in = DEVICE_MAX_WAIT_SECONDS

    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": str(document.get("verification_uri_complete") or ""),
        "interval": min(max(interval, DEVICE_MIN_INTERVAL_SECONDS), DEVICE_MAX_INTERVAL_SECONDS),
        "expires_in": min(max(expires_in, 0), DEVICE_MAX_WAIT_SECONDS),
    }


def poll_device_token(metadata, client_id, authorization, fetch=None, sleep=None, now=None, monotonic=None):
    """Poll until the user approves the device, or raise a terminal error.

    Honours the server's polling interval and `slow_down` backoff, and stops at
    the advertised expiry rather than polling indefinitely.
    """
    import time as _time

    fetch = fetch or _http_json
    sleep = sleep or _time.sleep
    monotonic = monotonic or _time.monotonic

    device_code = str((authorization or {}).get("device_code") or "")
    if not device_code:
        raise OAuthError("No device code is available to poll")
    interval = int(authorization.get("interval") or DEVICE_MIN_INTERVAL_SECONDS)
    deadline = monotonic() + min(
        int(authorization.get("expires_in") or DEVICE_MAX_WAIT_SECONDS), DEVICE_MAX_WAIT_SECONDS
    )

    while True:
        if monotonic() >= deadline:
            raise OAuthError("Device sign-in expired before it was approved")
        sleep(interval)
        document = fetch(metadata["token_endpoint"], method="POST", data={
            "grant_type": DEVICE_GRANT_TYPE,
            "client_id": client_id,
            "device_code": device_code,
        })
        if not isinstance(document, dict):
            raise OAuthError("Device token response was not a JSON object")
        error = str(document.get("error") or "")
        if not error:
            return normalize_token_response(document, now=now)
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = min(interval + DEVICE_MIN_INTERVAL_SECONDS, DEVICE_MAX_INTERVAL_SECONDS)
            continue
        if error == "access_denied":
            raise OAuthError("Device sign-in was refused")
        if error == "expired_token":
            raise OAuthError("Device sign-in expired before it was approved")
        raise OAuthError(f"Device sign-in failed: {error[:80]}")
