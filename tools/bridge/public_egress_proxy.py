"""Loopback HTTP CONNECT proxy with public-address DNS pinning.

Used by Eva's browser action agent so Chromium never resolves or connects to a
model-selected destination directly. Each proxy request resolves once, rejects
all non-global answers, and connects to one exact validated address. The proxy
stores no request data and is scoped to one action run.
"""

import contextlib
import ipaddress
import select
import socket
import socketserver
import threading
import urllib.parse

from bridge.action_runs import is_public_unicast


_MAX_HEADERS = 64 * 1024
_BUFFER = 64 * 1024
_SOCKET_TIMEOUT = 30
_ALLOWED_METHODS = frozenset({
    "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT",
})
_HOP_BY_HOP = frozenset({
    "connection", "proxy-connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
})
_MAX_BODY = 16 * 1024 * 1024
_HEADER_NAME = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+-.^_`|~"
)


class PublicEgressProxyError(RuntimeError):
    """Proxy could not validate or relay a request."""


def _public_addresses(host, port):
    normalized = str(host or "").lower().rstrip(".")
    if not normalized or normalized == "localhost" or normalized.endswith((".local", ".localhost")):
        raise PublicEgressProxyError("local host is blocked")
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        if all(char in "0123456789abcdefx:." for char in normalized):
            raise PublicEgressProxyError("ambiguous numeric host is blocked")
    else:
        if not is_public_unicast(literal):
            raise PublicEgressProxyError("non-public address is blocked")
    try:
        rows = socket.getaddrinfo(
            normalized, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise PublicEgressProxyError("host resolution failed") from exc
    addresses = []
    for row in rows:
        raw = row[4][0].split("%", 1)[0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise PublicEgressProxyError("invalid resolved address") from exc
        if not is_public_unicast(address):
            raise PublicEgressProxyError("host has a non-public DNS answer")
        item = (address.compressed, int(port))
        if item not in addresses:
            addresses.append(item)
    if not addresses:
        raise PublicEgressProxyError("host has no addresses")
    return addresses


def _connect_pinned(host, port):
    last_error = None
    for address in _public_addresses(host, port):
        try:
            return socket.create_connection(address, timeout=_SOCKET_TIMEOUT)
        except OSError as exc:
            last_error = exc
    raise PublicEgressProxyError("validated destination was unreachable") from last_error


def _read_headers(client):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = client.recv(min(4096, _MAX_HEADERS - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) >= _MAX_HEADERS:
            raise PublicEgressProxyError("request headers are too large")
    marker = data.find(b"\r\n\r\n")
    if marker < 0:
        raise PublicEgressProxyError("request headers are incomplete")
    return bytes(data[:marker + 4]), bytes(data[marker + 4:])


def _parse_authority(value, default_port):
    text = str(value or "").strip()
    if "@" in text:
        raise PublicEgressProxyError("credentials in authority are blocked")
    if any(char in text for char in "/?#") or text.endswith(":"):
        raise PublicEgressProxyError("invalid authority")
    try:
        parsed = urllib.parse.urlsplit("//" + text)
        host = parsed.hostname
        if parsed.path or parsed.query or parsed.fragment:
            raise PublicEgressProxyError("invalid authority")
        port = parsed.port if parsed.port is not None else default_port
    except ValueError as exc:
        raise PublicEgressProxyError("invalid authority") from exc
    if not host or not 1 <= port <= 65535:
        raise PublicEgressProxyError("invalid authority")
    return host, port


def _host_header(host, port, default_port):
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        authority = host.lower().rstrip(".")
    else:
        authority = f"[{literal.compressed}]" if literal.version == 6 else literal.compressed
    return authority if port == default_port else f"{authority}:{port}"


def _sanitize_http_headers(lines, host, port):
    parsed = []
    connection_tokens = set()
    content_lengths = []
    host_count = 0
    for raw in lines:
        if not raw or raw[:1] in (b" ", b"\t") or b":" not in raw:
            raise PublicEgressProxyError("malformed HTTP header")
        name_raw, value_raw = raw.split(b":", 1)
        try:
            name = name_raw.decode("ascii").lower()
            value = value_raw.decode("latin-1").strip()
        except UnicodeError as exc:
            raise PublicEgressProxyError("malformed HTTP header") from exc
        if not name or any(char not in _HEADER_NAME for char in name):
            raise PublicEgressProxyError("invalid HTTP header name")
        if "\r" in value or "\n" in value:
            raise PublicEgressProxyError("invalid HTTP header value")
        if name == "connection":
            connection_tokens.update(
                token.strip().lower() for token in value.split(",") if token.strip()
            )
        elif name == "content-length":
            content_lengths.append(value)
        elif name == "transfer-encoding":
            raise PublicEgressProxyError("Transfer-Encoding is unsupported")
        elif name == "host":
            host_count += 1
        parsed.append((name, name_raw, value_raw.strip()))
    if host_count > 1 or len(content_lengths) > 1:
        raise PublicEgressProxyError("duplicate authority or body framing")
    if connection_tokens.intersection({"content-length", "transfer-encoding"}):
        raise PublicEgressProxyError("Connection cannot nominate body framing")
    content_length = 0
    if content_lengths:
        if not content_lengths[0].isdigit():
            raise PublicEgressProxyError("invalid Content-Length")
        content_length = int(content_lengths[0])
        if not 0 <= content_length <= _MAX_BODY:
            raise PublicEgressProxyError("request body is too large")
    blocked = _HOP_BY_HOP | connection_tokens
    output = []
    for name, name_raw, value_raw in parsed:
        if name in blocked or name == "host":
            continue
        output.append(name_raw + b": " + value_raw)
    output.append(f"Host: {_host_header(host, port, 80)}".encode("ascii"))
    output.append(b"Connection: close")
    return output, content_length


def _read_exact(client, initial, length):
    if len(initial) > length:
        raise PublicEgressProxyError("request contains bytes beyond Content-Length")
    data = bytearray(initial)
    while len(data) < length:
        chunk = client.recv(min(_BUFFER, length - len(data)))
        if not chunk:
            raise PublicEgressProxyError("request body ended early")
        data.extend(chunk)
    return bytes(data)


def _relay_response(upstream, client):
    while True:
        data = upstream.recv(_BUFFER)
        if not data:
            return
        client.sendall(data)


def _relay(left, right):
    sockets = [left, right]
    while True:
        readable, _, exceptional = select.select(sockets, [], sockets, _SOCKET_TIMEOUT)
        if exceptional or not readable:
            return
        for source in readable:
            target = right if source is left else left
            data = source.recv(_BUFFER)
            if not data:
                return
            target.sendall(data)


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(_SOCKET_TIMEOUT)
        upstream = None
        try:
            headers, remainder = _read_headers(client)
            lines = headers[:-4].split(b"\r\n")
            parts = lines[0].decode("ascii", "strict").split(" ")
            if len(parts) != 3:
                raise PublicEgressProxyError("invalid request line")
            method, target, version = parts
            if method not in _ALLOWED_METHODS or not version.startswith("HTTP/1."):
                raise PublicEgressProxyError("unsupported proxy request")
            if method == "CONNECT":
                host, port = _parse_authority(target, 443)
                upstream = _connect_pinned(host, port)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if remainder:
                    upstream.sendall(remainder)
                _relay(client, upstream)
                return

            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme.lower() != "http" or not parsed.hostname:
                raise PublicEgressProxyError("absolute public HTTP URL required")
            if parsed.username is not None or parsed.password is not None:
                raise PublicEgressProxyError("URL credentials are blocked")
            authority = parsed.netloc.rsplit("@", 1)[-1]
            if authority.endswith(":") or parsed.port == 0:
                raise PublicEgressProxyError("invalid authority")
            port = parsed.port if parsed.port is not None else 80
            path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            output = [f"{method} {path} {version}".encode("ascii")]
            clean_headers, content_length = _sanitize_http_headers(
                lines[1:], parsed.hostname, port
            )
            body = _read_exact(client, remainder, content_length)
            upstream = _connect_pinned(parsed.hostname, port)
            output.extend(clean_headers)
            upstream.sendall(b"\r\n".join(output) + b"\r\n\r\n" + body)
            _relay_response(upstream, client)
        except (OSError, UnicodeError, ValueError, PublicEgressProxyError):
            with contextlib.suppress(OSError):
                client.sendall(
                    b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n"
                    b"Content-Length: 0\r\n\r\n"
                )
        finally:
            if upstream is not None:
                with contextlib.suppress(OSError):
                    upstream.close()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True


class PublicEgressProxy:
    """One-run loopback proxy. Call ``start()`` before passing ``url`` to Chromium."""

    def __init__(self):
        self._server = None
        self._thread = None

    @property
    def url(self):
        if self._server is None:
            raise PublicEgressProxyError("proxy is not started")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self):
        if self._server is not None:
            return self
        self._server = _Server(("127.0.0.1", 0), _ProxyHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="eva-public-egress-proxy",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self):
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def __enter__(self):
        return self.start()

    def __exit__(self, _type, _value, _traceback):
        self.close()
