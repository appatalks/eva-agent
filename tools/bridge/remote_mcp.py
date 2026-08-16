"""Streamable-HTTP MCP client for remote, OAuth-protected servers.

Eva's existing `local_mcp` client speaks JSON-RPC over stdio to allowlisted local
subprocesses. That transport cannot reach a hosted server such as Work IQ, which
requires HTTPS plus an Entra bearer token. This module adds only that transport;
authorization decisions live in `bridge.oauth_client` and mail policy lives in
`bridge.email_policy`.

Boundaries enforced here:

- HTTPS only. A remote MCP endpoint is never contacted over plaintext.
- Bounded response bodies and explicit timeouts, so a hostile or broken server
  cannot exhaust memory or hang a bridge worker.
- Bearer tokens are supplied per request by a caller-provided callable and are
  never stored on the instance, logged, or included in raised errors.
- A 401 is surfaced as a typed re-authorization signal carrying only the
  advertised metadata URL, never the response body.
"""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT = 60
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
CLIENT_NAME = "eva-bridge"


class RemoteMCPError(Exception):
    """Raised when a remote MCP call cannot be completed."""


class RemoteMCPAuthRequired(RemoteMCPError):
    """Raised when the server demands (re-)authorization."""

    def __init__(self, message, resource_metadata_url=""):
        super().__init__(message)
        self.resource_metadata_url = resource_metadata_url


def _is_https_url(value):
    try:
        parsed = urllib.parse.urlparse(str(value or ""))
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.netloc)


def parse_sse_payload(text):
    """Return the last JSON object carried by a text/event-stream response."""
    result = None
    for raw_line in str(text or "").splitlines():
        if not raw_line.startswith("data:"):
            continue
        chunk = raw_line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            result = json.loads(chunk)
        except ValueError:
            continue
    if result is None:
        raise RemoteMCPError("Remote MCP stream contained no JSON message")
    return result


def decode_response(body, content_type):
    """Decode either a plain JSON body or a streamed event body."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    if "text/event-stream" in str(content_type or "").lower():
        return parse_sse_payload(text)
    try:
        return json.loads(text)
    except ValueError:
        raise RemoteMCPError("Remote MCP response was not valid JSON") from None


def tool_result_text(result):
    """Flatten an MCP tool result into bounded plain text."""
    if not isinstance(result, dict):
        return ""
    parts = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        elif item.get("type") == "resource":
            resource = item.get("resource") if isinstance(item.get("resource"), dict) else {}
            parts.append(str(resource.get("text") or ""))
    return "\n".join(part for part in parts if part).strip()


class RemoteMCPClient:
    """One authenticated connection to a remote MCP endpoint."""

    def __init__(self, endpoint, token_provider, timeout=DEFAULT_TIMEOUT, transport=None):
        if not _is_https_url(endpoint):
            raise RemoteMCPError("Remote MCP endpoint must be an HTTPS URL")
        if not callable(token_provider):
            raise RemoteMCPError("A token provider callable is required")
        self.endpoint = endpoint
        self._token_provider = token_provider
        self.timeout = timeout
        self._transport = transport or self._https_post
        self._lock = threading.RLock()
        self._session_id = ""
        self._request_id = 0
        self._initialized = False
        self.server_info = {}

    def _https_post(self, url, payload, headers, timeout):
        """Perform one HTTPS POST. Returns (status, body, response_headers)."""
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                return response.status, body, dict(response.headers)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(MAX_RESPONSE_BYTES)
            except OSError:
                body = b""
            return exc.code, body, dict(exc.headers or {})
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise RemoteMCPError(f"Remote MCP server unreachable: {type(exc).__name__}") from None

    def _next_id(self):
        with self._lock:
            self._request_id += 1
            return self._request_id

    def _headers(self):
        token = self._token_provider()
        if not token:
            raise RemoteMCPAuthRequired("No access token is available for the remote MCP server")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _call(self, method, params=None, notification=False):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = self._next_id()

        status, body, headers = self._transport(self.endpoint, payload, self._headers(), self.timeout)

        if status == 401:
            from bridge.oauth_client import parse_www_authenticate_resource
            challenge = headers.get("WWW-Authenticate") or headers.get("www-authenticate") or ""
            raise RemoteMCPAuthRequired(
                "Remote MCP server requires authorization",
                parse_www_authenticate_resource(challenge),
            )
        if status == 403:
            raise RemoteMCPError("Remote MCP server refused the request under tenant policy")
        if status in (404, 410) and self._session_id:
            self._session_id = ""
            self._initialized = False
            raise RemoteMCPError("Remote MCP session expired; reconnect required")
        if status >= 400:
            raise RemoteMCPError(f"Remote MCP server returned HTTP {status}")
        if len(body) > MAX_RESPONSE_BYTES:
            raise RemoteMCPError("Remote MCP response exceeded the safe size limit")

        session_id = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if session_id:
            self._session_id = str(session_id)[:200]

        if notification:
            return None

        message = decode_response(body, headers.get("Content-Type") or headers.get("content-type"))
        if not isinstance(message, dict):
            raise RemoteMCPError("Remote MCP response was not a JSON-RPC object")
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            raise RemoteMCPError(f"Remote MCP error: {str(error.get('message') or 'unknown')[:200]}")
        return message.get("result")

    def initialize(self):
        """Perform the MCP handshake once per session."""
        with self._lock:
            if self._initialized:
                return self.server_info
            result = self._call("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": "1"},
            })
            self.server_info = result if isinstance(result, dict) else {}
            self._call("notifications/initialized", {}, notification=True)
            self._initialized = True
            return self.server_info

    def list_tools(self):
        """Return the tool descriptors advertised by the server."""
        self.initialize()
        result = self._call("tools/list")
        tools = result.get("tools") if isinstance(result, dict) else []
        return [tool for tool in tools or [] if isinstance(tool, dict)]

    def call_tool(self, name, arguments=None):
        """Invoke one remote tool and return its raw result object."""
        self.initialize()
        result = self._call("tools/call", {"name": str(name), "arguments": arguments or {}})
        if isinstance(result, dict) and result.get("isError"):
            raise RemoteMCPError(f"Remote tool '{name}' failed: {tool_result_text(result)[:200]}")
        return result if isinstance(result, dict) else {}

    def close(self):
        """Forget session state so the next call performs a fresh handshake."""
        with self._lock:
            self._session_id = ""
            self._initialized = False
            self.server_info = {}
