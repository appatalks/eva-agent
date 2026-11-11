"""Privacy-preserving URL observations from Eva's visible tool boundaries."""

import html
import re
import shlex
import urllib.parse

_URL_RE = re.compile(r"https?://[^\s<>\"'\\]+", re.IGNORECASE)
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_SENSITIVE_NAME = r"(?:x[_-]?)?api[_-]?key|(?:access|refresh|auth|bearer)[_-]?token|token|(?:client[_-]?)?secret|password|credentials?|authorization"
_SENSITIVE_SEGMENT = re.compile(r"^(?:" + _SENSITIVE_NAME + r")$", re.I)
_SENSITIVE_ASSIGNMENT = re.compile(r"^((?:" + _SENSITIVE_NAME + r")[=:]).+$", re.I)
_SENSITIVE_MATRIX = re.compile(r"((?:^|[;,&])(?:" + _SENSITIVE_NAME + r")[=:])[^/;,&]+", re.I)
_CREDENTIAL_VALUE = re.compile(
    r"(?:\bsk-(?!FAKE)[A-Za-z0-9_-]{20,}\b|\bgh[pousr]_[A-Za-z0-9_]{20,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|\bAKIA[0-9A-Z]{16}\b|"
    r"\bAIza[0-9A-Za-z_-]{35}\b|\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b)"
)
_PYTHON_REQUEST = re.compile(
    r"""\b(?:requests|httpx)\.(get|post|put|patch|delete|head|options)\s*\(\s*["'](https?://[^"'\s]+)""",
    re.IGNORECASE,
)
_METHOD_URL_RE = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(https?://[^\s<>\"'\\]+)",
    re.IGNORECASE,
)
_JS_FETCH_REQUEST = re.compile(
    r"""\bfetch\s*\(\s*["'](https?://[^"'\s]+)["']\s*,\s*\{[^}]{0,1024}?\bmethod\s*:\s*["'](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)["']""",
    re.IGNORECASE | re.DOTALL,
)
_XHR_REQUEST = re.compile(
    r"""\.open\s*\(\s*["'](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)["']\s*,\s*["'](https?://[^"'\s]+)""",
    re.IGNORECASE,
)


def sanitize_url(value):
    """Keep HTTP(S) host/path only, dropping userinfo, query, and fragment."""
    if not isinstance(value, str) or not value or len(value) > 8192:
        return ""
    try:
        parsed = urllib.parse.urlsplit(html.unescape(value.strip()))
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
        if scheme not in {"http", "https"} or not hostname or any(ord(char) < 33 for char in hostname):
            return ""
        if _CREDENTIAL_VALUE.search(urllib.parse.unquote(hostname)):
            hostname = "redacted"
        path = parsed.path
        for _ in range(4):
            decoded = urllib.parse.unquote(path, errors="strict")
            if decoded == path:
                break
            path = decoded
        if re.search(r"%[0-9A-Fa-f]{2}", path):
            return ""
        path = path.split("?", 1)[0].split("#", 1)[0]
        if any(ord(char) < 32 or ord(char) == 127 for char in path):
            return ""
        segments = []
        redact_next = False
        for segment in path.split("/"):
            if redact_next and segment:
                segments.append("REDACTED")
                redact_next = False
            elif _SENSITIVE_ASSIGNMENT.match(segment):
                segments.append(_SENSITIVE_ASSIGNMENT.match(segment).group(1) + "REDACTED")
            elif _CREDENTIAL_VALUE.search(segment):
                segments.append("REDACTED")
            else:
                segments.append(_SENSITIVE_MATRIX.sub(r"\1REDACTED", segment))
                redact_next = bool(_SENSITIVE_SEGMENT.fullmatch(segment))
        clean_path = urllib.parse.quote("/".join(segments), safe="/-._~!$&'()*+,;=:@")
        host = "[" + hostname + "]" if ":" in hostname else hostname
        if port is not None:
            host += ":" + str(port)
        return urllib.parse.urlunsplit((scheme, host, clean_path, "", ""))[:2048]
    except (TypeError, ValueError, UnicodeError):
        return ""


def extract_urls(value):
    """Find URLs in tool output without retaining the surrounding content."""
    texts = []
    def collect(item, depth=0):
        if depth > 12:
            return
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict):
            for child in item.values():
                collect(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child, depth + 1)
    collect(value)
    urls = set()
    for text in texts:
        for match in _URL_RE.finditer(text):
            url = sanitize_url(match.group(0).rstrip(".,;:!?)"))
            if url:
                urls.add(url)
    return sorted(urls)


def _explicit_methods(text):
    values = set()
    for pattern in (_METHOD_URL_RE, _PYTHON_REQUEST, _XHR_REQUEST):
        for match in pattern.finditer(text):
            clean = sanitize_url(match.group(2).rstrip(".,;:!?)"))
            if clean:
                values.add((match.group(1).upper(), clean))
    for match in _JS_FETCH_REQUEST.finditer(text):
        clean = sanitize_url(match.group(1))
        if clean:
            values.add((match.group(2).upper(), clean))
    return values


def tool_input_observations(tool_call):
    """Extract literals from a tool request; never treat them as executed."""
    raw = tool_call.get("rawInput") if isinstance(tool_call, dict) else None
    if not isinstance(raw, dict):
        return []
    result = set()
    method = str(raw.get("method") or "").upper()
    method = method if method in _METHODS else "UNKNOWN"
    for key in ("url", "uri"):
        url = sanitize_url(raw.get(key))
        if url:
            result.add((method, url))
    command = raw.get("command") or raw.get("cmd")
    if isinstance(command, str):
        args = raw.get("args")
        if isinstance(args, list) and all(isinstance(arg, str) for arg in args):
            command = shlex.join([command, *args])
        command_method = "UNKNOWN"
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            lexer.commenters = ""
            parts = list(lexer)
            composed = any(part and set(part) <= set(";&|<>") for part in parts) or bool(re.search(r"[\n\r`]|\$\(", command))
            if parts and parts[0].rsplit("/", 1)[-1] == "curl" and not composed:
                command_method = "GET"
                explicit = ""
                for index, part in enumerate(parts[1:], 1):
                    if part in {"-X", "--request"} and index + 1 < len(parts):
                        candidate = parts[index + 1].upper()
                        if candidate in _METHODS:
                            explicit = candidate
                    elif part.startswith("-X") and part[2:].upper() in _METHODS:
                        explicit = part[2:].upper()
                    elif part.startswith("--request=") and part[10:].upper() in _METHODS:
                        explicit = part[10:].upper()
                    elif part in {"-d", "--data", "--data-raw", "--data-binary"} or part.startswith(("--data=", "-d")):
                        command_method = "POST"
                    elif part in {"-I", "--head"}:
                        command_method = "HEAD"
                    elif part in {"-T", "--upload-file"}:
                        command_method = "PUT"
                command_method = explicit or command_method
        except ValueError:
            pass
        result.update(_explicit_methods(command))
        known_urls = {url for _, url in result}
        for url in extract_urls(command):
            if url not in known_urls:
                result.add((command_method, url))
    return sorted(result)


def discovered_observations(value):
    """Preserve explicitly stated HTTP methods without implying execution."""
    typed = set()
    def collect(item, depth=0):
        if depth > 12:
            return
        if isinstance(item, str):
            typed.update(_explicit_methods(item))
        elif isinstance(item, dict):
            method = str(item.get("method") or "").upper()
            if method in _METHODS:
                for key in ("url", "uri"):
                    url = sanitize_url(item.get(key))
                    if url:
                        typed.add((method, url))
            for child in item.values():
                collect(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                collect(child, depth + 1)
    collect(value)
    known_urls = {url for _, url in typed}
    typed.update(("UNKNOWN", url) for url in extract_urls(value) if url not in known_urls)
    return sorted(typed)


def record(observations):
    """Persist sanitized observations; logging failures never affect a tool call."""
    try:
        from bridge.mission_log import record_url_observations
        record_url_observations(observations)
    except Exception:
        pass


def observe_request(method, url):
    clean = sanitize_url(url)
    if clean:
        record([(method, clean, "request")])


def observe_discovered(value):
    record([(method, url, "discovered") for method, url in discovered_observations(value)])
