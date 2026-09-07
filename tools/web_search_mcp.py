#!/usr/bin/env python3
"""
Eva Web Search MCP Server
A lightweight MCP server providing web search (DuckDuckGo, no API key) and
page content extraction. Designed for Eva's local mode where the Copilot CLI
(and its built-in Bing) is not available.

Tools:
  web_search       — Search DuckDuckGo and return results with snippets
  web_fetch        — Fetch a URL and extract readable text content
  web_search_news  — Search DuckDuckGo News for recent headlines

Runs as a stdio MCP server (JSON-RPC over stdin/stdout).
"""

import html
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import subprocess
import sys
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_WEB_FETCH_MAX_BYTES = 512 * 1024
_WEB_FETCH_MAX_REDIRECTS = 4
_WEB_FETCH_PORTS = {80, 443}
_WEB_FETCH_REDIRECTS = {301, 302, 303, 307, 308}

TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web using DuckDuckGo. Returns a list of results with "
            "titles, URLs, and text snippets. No API key required."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 8, max 20)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch a web page and extract its readable text content. "
            "Strips HTML tags, scripts, styles, and returns clean text. "
            "Useful for reading articles, documentation, or any web page."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch",
                },
                "max_length": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 6000)",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "web_search_news",
        "description": (
            "Search DuckDuckGo News for recent headlines and articles. "
            "Returns news results with titles, sources, dates, and snippets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The news search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default 8, max 20)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "weather_current",
        "description": "Return verified current conditions and today's forecast for an explicit city or region, preferring the National Weather Service for U.S. locations.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Explicit approved city or region",
                },
            },
            "required": ["location"],
        },
    },
    {
        "name": "stock_quote",
        "description": "Retrieve one current stock quote from a local provider or Google Finance for a ticker and optional exchange.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A stock ticker or a request containing one ticker, such as PLG or PLG:NYSEAMERICAN",
                },
            },
            "required": ["query"],
        },
    },
]

_STOCK_TICKER_RE = re.compile(r"(?:\$|\b)([A-Za-z]{1,10})(?:\s*:\s*([A-Za-z.]{2,20}))?\b")
_STOCK_CONTEXT_RE = re.compile(
    r"\b(?:stock|share|ticker|quote|price)"
    r"(?:\s+(?:stock|share|ticker|quote|price|of|for|the|last|current|latest))*"
    r"\s+(?:of|for\s+)?\$?([A-Za-z]{1,10})(?:\s*:\s*([A-Za-z.]{2,20}))?\b",
    re.IGNORECASE,
)
_STOCK_EXCHANGE_ALIASES = {
    "AMEX": "NYSEAMERICAN",
    "NYSEAMERICAN": "NYSEAMERICAN",
    "NYSEAMERICANEXCHANGE": "NYSEAMERICAN",
    "NYSEMKT": "NYSEAMERICAN",
    "NASDAQ": "NASDAQ",
    "NYSE": "NYSE",
    "OTC": "OTCMKTS",
    "OTCMKTS": "OTCMKTS",
}
_STOCK_DEFAULT_EXCHANGES = ("NYSEAMERICAN", "NASDAQ", "NYSE", "OTCMKTS")
_STOCK_STOPWORDS = {
    "A", "AN", "AND", "ARE", "AT", "CURRENT", "FOR", "HOW", "IS", "MARKET", "ME", "MY",
    "NOT", "OF", "ON", "PRICE", "QUOTE", "SHARE", "SHOW", "STOCK", "THE", "TICKER", "TODAY", "WHAT",
}
_TICKER_SH_DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".local", "share", "eva", "ticker.sh", "ticker.sh")
_TICKER_SH_OUTPUT_RE = re.compile(
    r"^\s*([A-Z][A-Z0-9.-]{0,14})\s+"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s+"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s+"
    r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))%\s*$"
)


# ---------------------------------------------------------------------------
# DuckDuckGo search (HTML scraping, no API key)
# ---------------------------------------------------------------------------

def _http_get(url, timeout=15):
    """Fetch a URL and return (status, body_text)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Read up to 512KB
            body = resp.read(512 * 1024)
            charset = resp.headers.get_content_charset() or "utf-8"
            # DuckDuckGo sometimes returns 202 with valid content
            return 200, body.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        # Some "errors" still have useful bodies (e.g. 202)
        try:
            body = e.read(512 * 1024)
            charset = e.headers.get_content_charset() or "utf-8"
            if len(body) > 500:
                return 200, body.decode(charset, errors="replace")
        except Exception:
            pass
        return e.code, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return 0, f"Error: {e}"


def _public_address(address):
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.is_global and not any((
        parsed.is_private,
        parsed.is_loopback,
        parsed.is_link_local,
        parsed.is_reserved,
        parsed.is_multicast,
        parsed.is_unspecified,
    ))


def _resolve_public_addresses(hostname, port):
    """Resolve a host and reject any answer that is not globally routable."""
    try:
        direct = ipaddress.ip_address(hostname)
    except ValueError:
        direct = None
    if direct is not None:
        return ([str(direct)], "") if _public_address(direct) else ([], "non_global_address")
    try:
        records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror):
        return [], "dns_unavailable"
    addresses = []
    for record in records:
        address = str(record[4][0])
        if not _public_address(address):
            return [], "non_global_address"
        if address not in addresses:
            addresses.append(address)
    return (addresses, "") if addresses else ([], "dns_unavailable")


def _validate_public_fetch_url(url):
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        hostname = parsed.hostname or ""
        port = parsed.port
    except (TypeError, ValueError):
        return None, "invalid_url"
    hostname = hostname.rstrip(".").lower()
    if parsed.scheme.lower() not in {"http", "https"}:
        return None, "scheme_not_allowed"
    if not hostname:
        return None, "missing_hostname"
    if parsed.username is not None or parsed.password is not None:
        return None, "userinfo_not_allowed"
    if port is not None and port not in _WEB_FETCH_PORTS:
        return None, "port_not_allowed"
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith((".localhost", ".local", ".internal")):
        return None, "local_hostname"
    effective_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    addresses, reason = _resolve_public_addresses(hostname, effective_port)
    if not addresses:
        return None, reason or "non_global_address"
    return {
        "url": str(url),
        "parsed": parsed,
        "hostname": hostname,
        "port": effective_port,
        "addresses": addresses,
    }, ""


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, address, hostname, port, timeout):
        super().__init__(address, port, timeout=timeout)
        self._pinned_address = address
        self._target_hostname = hostname

    def connect(self):
        self.sock = socket.create_connection((self._pinned_address, self.port), self.timeout)


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    def __init__(self, address, hostname, port, timeout, context):
        super().__init__(address, hostname, port, timeout)
        self._ssl_context = context

    def connect(self):
        raw_socket = socket.create_connection((self._pinned_address, self.port), self.timeout)
        try:
            self.sock = self._ssl_context.wrap_socket(raw_socket, server_hostname=self._target_hostname)
        except Exception:
            raw_socket.close()
            raise


def _pinned_http_get(target, timeout=20):
    """Fetch one already-validated URL while connecting to its validated IP."""
    parsed = target["parsed"]
    target_path = parsed.path or "/"
    if parsed.query:
        target_path += "?" + parsed.query
    hostname = target["hostname"]
    host_header = hostname
    if ":" in hostname and not hostname.startswith("["):
        host_header = "[" + hostname + "]"
    explicit_port = parsed.port is not None
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if explicit_port and parsed.port != default_port:
        host_header += ":" + str(parsed.port)
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "Host": host_header,
        "Connection": "close",
    }
    context = ssl.create_default_context() if parsed.scheme.lower() == "https" else None
    last_error = "network_error"
    for address in target["addresses"][:2]:
        connection = None
        try:
            if context is None:
                connection = _PinnedHTTPConnection(address, hostname, target["port"], timeout)
            else:
                connection = _PinnedHTTPSConnection(address, hostname, target["port"], timeout, context)
            connection.request("GET", target_path, headers=headers)
            response = connection.getresponse()
            body = response.read(_WEB_FETCH_MAX_BYTES + 1)
            response_headers = {str(key).lower(): str(value) for key, value in response.getheaders()}
            return int(response.status), response_headers, body, len(body) > _WEB_FETCH_MAX_BYTES, ""
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            last_error = type(error).__name__
        finally:
            if connection is not None:
                connection.close()
    return 0, {}, b"", False, last_error


def _guarded_http_fetch(url, timeout=20):
    current_url = str(url or "")
    for redirect_count in range(_WEB_FETCH_MAX_REDIRECTS + 1):
        target, reason = _validate_public_fetch_url(current_url)
        if target is None:
            return 0, {}, "", current_url, "unsafe_url:" + reason
        status, headers, body, too_large, error = _pinned_http_get(target, timeout=timeout)
        if status in _WEB_FETCH_REDIRECTS and headers.get("location"):
            if redirect_count >= _WEB_FETCH_MAX_REDIRECTS:
                return status, headers, "", current_url, "redirect_limit"
            current_url = urllib.parse.urljoin(current_url, headers["location"])
            continue
        charset_match = re.search(r"charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", headers.get("content-type", ""), re.I)
        charset = charset_match.group(1) if charset_match else "utf-8"
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        if too_large:
            return status, headers, text[:2000], current_url, "content_too_large"
        if error:
            return status, headers, text[:2000], current_url, error
        return status, headers, text, current_url, ""
    return 0, {}, "", current_url, "redirect_limit"


def _web_fetch_challenge_page(body, text):
    title_match = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    title = _strip_html(title_match.group(1)).lower() if title_match else ""
    sample = (title + " " + _strip_html(body[:6000])).lower()
    return bool(re.search(
        r"just\s+a\s+moment|cf-chl-|challenge-platform|checking\s+your\s+browser|"
        r"verify\s+(?:you\s+are\s+)?human|captcha|access\s+denied|"
        r"\b(?:403|404|500|502|503)\s+(?:forbidden|not\s+found|error)\b",
        sample,
    )) or not text.strip()


def _stock_request(query):
    """Return a normalized ticker and bounded candidate venue list."""
    text = str(query or "").strip()
    matches = [candidate for candidate in _STOCK_TICKER_RE.finditer(text)
               if candidate.group(1).upper() not in _STOCK_STOPWORDS]
    if not matches:
        return "", ()
    explicit = [candidate for candidate in matches
                if candidate.group(0).lstrip().startswith("$") or candidate.group(2)]
    if len(explicit) > 1:
        return "", ()
    if explicit:
        match = explicit[0]
    else:
        finance_keyword = re.compile(r"\b(?:stock|share|ticker|quote|price)\b", re.IGNORECASE)
        contextual = [candidate for candidate in matches
                      if candidate.group(1).isupper()
                      and finance_keyword.search(text[candidate.end():candidate.end() + 80])]
        contextual.extend(
            candidate for candidate in matches
            if _STOCK_CONTEXT_RE.search(text[max(0, candidate.start() - 80):candidate.end()])
        )
        symbols = {candidate.group(1).upper() for candidate in contextual}
        if len(symbols) != 1:
            return "", ()
        match = next(candidate for candidate in contextual if candidate.group(1).upper() in symbols)
    symbol = match.group(1).upper()
    has_explicit_symbol = match.group(0).lstrip().startswith("$") or bool(match.group(2))
    has_quote_context = bool(re.search(r"\b(?:stock|share|ticker|quote|price|market)\b", text, re.IGNORECASE))
    if not has_explicit_symbol and not has_quote_context:
        return "", ()
    raw_exchange = re.sub(r"[^A-Za-z]", "", match.group(2) or "").upper()
    exchange = _STOCK_EXCHANGE_ALIASES.get(raw_exchange, raw_exchange)
    if raw_exchange and exchange not in _STOCK_EXCHANGE_ALIASES.values():
        return "", ()
    return symbol, (exchange,) if exchange else _STOCK_DEFAULT_EXCHANGES


def _local_stock_quote_url():
    value = str(os.environ.get("EVA_STOCK_QUOTE_URL", "") or "").strip()
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "http" or hostname not in {"localhost", "127.0.0.1", "::1"}:
        return ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    return value.rstrip("/")


def _local_stock_quote(symbol, exchange):
    endpoint = _local_stock_quote_url()
    if not endpoint:
        return None
    query = urllib.parse.urlencode({"symbol": symbol, "exchange": exchange})
    status, body = _http_get(endpoint + "?" + query, timeout=8)
    if status != 200:
        return None
    try:
        receipt = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(receipt, dict) and isinstance(receipt.get("quote"), dict):
        receipt = receipt["quote"]
    if not isinstance(receipt, dict):
        return None
    if str(receipt.get("symbol") or "").upper() != symbol:
        return None
    price = receipt.get("price")
    if not isinstance(price, (int, float)) or not math.isfinite(price):
        return None
    resolved_exchange = str(receipt.get("exchange") or exchange).upper()
    if resolved_exchange != exchange:
        return None
    result = {
        "symbol": symbol,
        "exchange": exchange,
        "name": str(receipt.get("name") or "")[:160],
        "currency": str(receipt.get("currency") or "")[:16],
        "price": price,
        "source": str(receipt.get("source") or "Local quote service")[:80],
        "source_url": endpoint,
        "retrieved_at": _quote_retrieved_at(),
    }
    for key in ("change", "change_percent", "observed_at"):
        value = receipt.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            result[key] = value
        elif key == "observed_at" and isinstance(value, str):
            result[key] = value[:80]
    return result


def _quote_retrieved_at():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ticker_sh_path():
    """Return the configured local ticker.sh executable when it is safe to invoke."""
    configured = str(os.environ.get("EVA_TICKER_SH_PATH", _TICKER_SH_DEFAULT_PATH) or "").strip()
    candidate = os.path.abspath(os.path.expanduser(configured)) if configured else ""
    if os.path.basename(candidate) != "ticker.sh":
        return ""
    if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
        return ""
    return candidate


def _ticker_sh_stock_quote(symbol):
    """Run the fixed local ticker.sh quote path and validate its plain-text receipt."""
    executable = _ticker_sh_path()
    if not executable:
        return None
    environment = {
        "HOME": os.path.expanduser("~"),
        "LANG": "C",
        "LC_ALL": "C",
        "NO_COLOR": "1",
        "PATH": os.defpath,
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    try:
        completed = subprocess.run(
            [executable, symbol],
            check=False,
            cwd=os.path.dirname(executable),
            env=environment,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    records = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(records) != 1:
        return None
    match = _TICKER_SH_OUTPUT_RE.fullmatch(records[0])
    if not match or match.group(1) != symbol:
        return None
    try:
        price = float(match.group(2))
        change = float(match.group(3))
        change_percent = float(match.group(4))
    except ValueError:
        return None
    if not all(math.isfinite(value) for value in (price, change, change_percent)) or price <= 0:
        return None
    return {
        "symbol": symbol,
        "currency": "USD",
        "price": price,
        "change": change,
        "change_percent": change_percent,
        "source": "ticker.sh (Yahoo Finance)",
        "source_url": "https://finance.yahoo.com/quote/" + symbol,
        "retrieved_at": _quote_retrieved_at(),
    }


def _balanced_json_array(text, start):
    """Decode one JSON array from a JavaScript data field with string-aware balancing."""
    index = text.find("[", start)
    if index < 0:
        return None
    depth = 0
    quoted = False
    escaped = False
    for cursor in range(index, len(text)):
        char = text[cursor]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[index:cursor + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _google_finance_payloads(page):
    """Yield balanced data arrays from Google Finance callback records."""
    marker = "AF_initDataCallback("
    cursor = 0
    while True:
        start = page.find(marker, cursor)
        if start < 0:
            return
        data_start = page.find("data:", start)
        if data_start < 0:
            cursor = start + len(marker)
            continue
        payload = _balanced_json_array(page, data_start + len("data:"))
        if payload is not None:
            yield payload
        cursor = data_start + len("data:")


def _nested_lists(value):
    if isinstance(value, list):
        yield value
        for item in value:
            yield from _nested_lists(item)


def _google_quote_record(payloads, symbol, exchange):
    """Find a validated identity record and its adjacent quote vector."""
    for payload in payloads:
        for record in _nested_lists(payload):
            for index, item in enumerate(record):
                if not (isinstance(item, list) and len(item) >= 2):
                    continue
                if str(item[0]).upper() != symbol or str(item[1]).upper() != exchange:
                    continue
                name = ""
                quote = None
                for candidate in record[index + 1:index + 6]:
                    if isinstance(candidate, str) and not name:
                        name = candidate[:160]
                    if isinstance(candidate, list) and len(candidate) >= 3 and isinstance(candidate[0], (int, float)):
                        quote = candidate
                        break
                if quote is not None:
                    return {"name": name, "price": quote[0], "change": quote[1], "change_percent": quote[2]}
    return None


def google_stock_quote(query):
    """Return one exact local, Google Finance, or bounded failure quote receipt."""
    symbol, exchanges = _stock_request(query)
    if not symbol:
        return {"error": "quote_invalid_ticker"}
    for exchange in exchanges:
        local_quote = _local_stock_quote(symbol, exchange)
        if local_quote is not None:
            return local_quote
    if len(exchanges) > 1:
        ticker_quote = _ticker_sh_stock_quote(symbol)
        if ticker_quote is not None:
            return ticker_quote
    for exchange in exchanges:
        url = "https://www.google.com/finance/quote/" + symbol + ":" + exchange + "?hl=en&gl=us"
        status, page = _http_get(url, timeout=12)
        if status != 200:
            continue
        record = _google_quote_record(_google_finance_payloads(page), symbol, exchange)
        if record is None:
            continue
        return {
            "symbol": symbol,
            "exchange": exchange,
            "name": record["name"],
            "currency": "USD",
            "price": record["price"],
            "change": record["change"],
            "change_percent": record["change_percent"],
            "source": "Google Finance",
            "source_url": url,
            "retrieved_at": _quote_retrieved_at(),
        }
    return {"error": "quote_unavailable", "symbol": symbol}


def _strip_html(text):
    """Remove HTML tags and decode entities."""
    # Remove script/style blocks
    text = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", "", text, flags=re.S | re.I)
    # Remove tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode entities
    text = html.unescape(text)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_readable(html_text, max_length=6000):
    """Extract readable text from HTML, focusing on article content."""
    # Try to find article/main content
    for tag in ("article", "main", '[role="main"]', ".article-body", ".post-content"):
        pattern = re.compile(
            r"<(?:article|main|div)[^>]*(?:class|role)=['\"][^'\"]*"
            + re.escape(tag.lstrip(".").lstrip("[").split("=")[0].split('"')[0])
            + r"[^>]*>(.*?)</(?:article|main|div)>",
            re.S | re.I,
        )
        m = pattern.search(html_text)
        if m and len(m.group(1)) > 200:
            text = _strip_html(m.group(1))
            if len(text) > 100:
                return text[:max_length]

    # Fallback: strip everything and take the largest text block
    text = _strip_html(html_text)
    # Remove nav-like short lines
    lines = text.split("\n")
    content_lines = [l for l in lines if len(l.strip()) > 40]
    result = "\n".join(content_lines)
    return result[:max_length] if result else text[:max_length]


def _google_fallback(query, max_results=5):
    """Last-resort search via Google's HTML. Used when DDG rate-limits."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://www.google.com/search?q={encoded}&hl=en&num={max_results}"
    status, body = _http_get(url, timeout=10)
    if status != 200:
        return [{"info": "Search temporarily unavailable (rate-limited)", "query": query}]

    results = []
    # Google wraps results in <div class="g"> or similar
    # Find <a href="/url?q=..."> patterns
    links = re.findall(r'<a[^>]*href="/url\?q=(https?://[^&"]+)[^"]*"[^>]*>(.*?)</a>', body, re.S)
    if not links:
        links = re.findall(r'<a[^>]*href="(https?://(?!www\.google|accounts\.google|support\.google)[^"]+)"[^>]*>(.*?)</a>', body, re.S)

    seen = set()
    for raw_url, title_html in links:
        raw_url = urllib.parse.unquote(raw_url).split("&")[0]
        title = _strip_html(title_html).strip()
        if not title or len(title) < 5 or raw_url in seen:
            continue
        result_host = (urllib.parse.urlparse(raw_url).hostname or "").lower().rstrip(".")
        if (result_host == "google.com" or result_host.endswith(".google.com")
                or (result_host in ("youtube.com", "www.youtube.com")
                    and urllib.parse.urlparse(raw_url).path.startswith("/sorry"))):
            continue
        seen.add(raw_url)
        results.append({"title": title, "url": raw_url, "snippet": ""})
        if len(results) >= max_results:
            break

    return results if results else [{"info": "Search temporarily unavailable", "query": query}]


def _google_news_rss(query, max_results=8):
    encoded = urllib.parse.quote_plus(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    status, body = _http_get(url, timeout=12)
    if status != 200:
        return [{"info": "News search temporarily unavailable", "query": query}]
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return [{"info": "News search returned an invalid feed", "query": query}]
    results = []
    for item in root.findall(".//item")[:max_results]:
        title = str(item.findtext("title") or "").strip()
        link = str(item.findtext("link") or "").strip()
        published = str(item.findtext("pubDate") or "").strip()
        source_node = item.find("source")
        source = str(source_node.text or "").strip() if source_node is not None else ""
        if title and link:
            results.append({"title": title, "url": link, "source": source, "date": published})
    return results or [{"info": "News search returned no current items", "query": query}]


_US_STATE_ABBREVIATIONS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}


def _us_city_state(location):
    value = re.sub(r"\s+", " ", str(location or "").strip(" ,"))
    lowered = value.lower()
    for state_name, abbreviation in sorted(_US_STATE_ABBREVIATIONS.items(), key=lambda item: -len(item[0])):
        if lowered.endswith(" " + state_name):
            city = value[:-(len(state_name))].strip(" ,")
            return (city, abbreviation) if city else ("", "")
    match = re.match(r"^(.+?)[, ]+([A-Za-z]{2})$", value)
    if match and match.group(2).upper() in _US_STATE_ABBREVIATIONS.values():
        return match.group(1).strip(" ,"), match.group(2).upper()
    return "", ""


def _nws_weather(location):
    city, state = _us_city_state(location)
    if not city:
        return []
    url = "https://forecast.weather.gov/zipcity.php?" + urllib.parse.urlencode({
        "inputstring": city + "," + state,
    })
    status, body = _http_get(url, timeout=12)
    if status != 200:
        return []
    page_text = _strip_html(body)
    lowered = page_text.lower()
    city_terms = [term.lower() for term in re.findall(r"[A-Za-z]+", city)]
    if not city_terms or not all(term in lowered for term in city_terms):
        return []

    def first_text_from(source, pattern):
        match = re.search(pattern, source, re.S | re.I)
        return _strip_html(match.group(1)).strip() if match else ""

    def first_text(pattern):
        return first_text_from(body, pattern)

    temperature = first_text(r'<p[^>]*class=["\'][^"\']*myforecast-current-lrg[^"\']*["\'][^>]*>(.*?)</p>')
    condition = first_text(r'<p[^>]*class=["\']myforecast-current["\'][^>]*>(.*?)</p>')
    forecast_parts = []
    cards = re.findall(
        r'<li[^>]*class=["\'][^"\']*forecast-tombstone[^"\']*["\'][^>]*>(.*?)</li>',
        body,
        re.S | re.I,
    )
    for card in cards[:2]:
        period = first_text_from(card, r'<p[^>]*class=["\'][^"\']*period-name[^"\']*["\'][^>]*>(.*?)</p>')
        description = first_text_from(card, r'<p[^>]*class=["\'][^"\']*short-desc[^"\']*["\'][^>]*>(.*?)</p>')
        temperature_range = first_text_from(card, r'<p[^>]*class=["\'][^"\']*temp[^"\']*["\'][^>]*>(.*?)</p>')
        if period and description and temperature_range:
            forecast_parts.append(period + ": " + description + ", " + temperature_range)
    if not temperature or not condition or not forecast_parts:
        return []
    retrieved_at = _quote_retrieved_at()
    display_location = city + ", " + state
    return [{
        "kind": "current",
        "title": "Current weather for " + display_location,
        "snippet": condition + "; " + temperature,
        "url": url,
        "source": "National Weather Service",
        "retrieved_at": retrieved_at,
    }, {
        "kind": "forecast",
        "title": "Today's forecast for " + display_location,
        "snippet": "; ".join(forecast_parts),
        "url": url,
        "source": "National Weather Service",
        "retrieved_at": retrieved_at,
    }]


def google_weather(location):
    location = str(location or "").strip()[:120]
    if not location:
        return [{"info": "Weather location is required"}]
    nws_results = _nws_weather(location)
    if nws_results:
        return nws_results
    query = urllib.parse.quote_plus("weather " + location)
    url = f"https://www.google.com/search?q={query}&hl=en&gl=us"
    status, body = _http_get(url, timeout=12)
    if status != 200:
        return [{"info": "Weather search temporarily unavailable"}]

    def card_value(element_id):
        match = re.search(r'id=["\']' + re.escape(element_id) + r'["\'][^>]*>(.*?)<', body, re.I | re.S)
        return _strip_html(match.group(1)).strip() if match else ""

    temperature = card_value("wob_tm")
    condition = card_value("wob_dc")
    precipitation = card_value("wob_pp")
    humidity = card_value("wob_hm")
    wind = card_value("wob_ws")
    results = []
    if temperature or condition:
        details = [value for value in (
            condition,
            (temperature + " F") if temperature else "",
            ("precipitation " + precipitation) if precipitation else "",
            ("humidity " + humidity) if humidity else "",
            ("wind " + wind) if wind else "",
        ) if value]
        results.append({
            "kind": "current",
            "title": "Current weather for " + location,
            "snippet": "; ".join(details),
            "url": url,
            "source": "Google Weather",
            "retrieved_at": _quote_retrieved_at(),
        })

    location_terms = [term.lower() for term in re.findall(r"[A-Za-z]+", location)]
    location_terms = [term for term in location_terms if term not in {
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
        "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
        "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
        "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada", "hampshire",
        "jersey", "mexico", "york", "carolina", "dakota", "ohio", "oklahoma", "oregon",
        "pennsylvania", "rhode", "island", "tennessee", "texas", "utah", "vermont", "virginia",
        "washington", "wisconsin", "wyoming", "united", "states", "usa", "us", "tx",
    }]

    def matches_location(item):
        haystack = " ".join(str(item.get(key) or "") for key in ("title", "snippet", "url")).lower()
        return bool(location_terms) and all(term in haystack for term in location_terms)

    if not results:
        current_candidates = ddg_search("current weather " + location + " temperature", 6)
        for item in current_candidates:
            hostname = (urllib.parse.urlparse(str(item.get("url") or "")).hostname or "").lower()
            snippet = str(item.get("snippet") or "").strip()
            if not matches_location(item) or not re.search(r"\b(?:currently|temperature)\b", snippet, re.I):
                continue
            source = {
                "www.accuweather.com": "AccuWeather",
                "accuweather.com": "AccuWeather",
                "weather.com": "The Weather Channel",
                "www.weather.com": "The Weather Channel",
                "www.theweathernetwork.com": "The Weather Network",
                "theweathernetwork.com": "The Weather Network",
            }.get(hostname)
            if source:
                results.append({
                    "kind": "current",
                    "title": "Current weather for " + location,
                    "snippet": snippet[:500],
                    "url": str(item.get("url") or "")[:500],
                    "source": source,
                    "retrieved_at": _quote_retrieved_at(),
                })
                break

    forecast_candidates = ddg_search(
        "site:forecast.weather.gov " + location + " Today High Low", 8
    )
    for item in forecast_candidates:
        hostname = (urllib.parse.urlparse(str(item.get("url") or "")).hostname or "").lower()
        snippet = str(item.get("snippet") or "").strip()
        if hostname not in {"weather.gov", "www.weather.gov", "forecast.weather.gov"}:
            continue
        if not matches_location(item) or not re.search(r"\b(?:high|low|forecast)\b", snippet, re.I):
            continue
        results.append({
            "kind": "forecast",
            "title": "Today's forecast for " + location,
            "snippet": snippet[:500],
            "url": str(item.get("url") or "")[:500],
            "source": "National Weather Service",
            "retrieved_at": _quote_retrieved_at(),
        })
        break

    return results or [{"info": "Current weather conditions unavailable for " + location}]


def ddg_search(query, max_results=8):
    """Search DuckDuckGo HTML and parse results, with Google fallback."""
    max_results = min(max(1, max_results), 20)
    encoded = urllib.parse.quote_plus(query)

    # Try html.duckduckgo.com first, fall back to lite
    for base in ("https://html.duckduckgo.com/html/?q=", "https://lite.duckduckgo.com/lite/?q="):
        url = f"{base}{encoded}"
        status, body = _http_get(url)
        if status != 200:
            continue

        results = []
        blocks = re.findall(
            r'<div[^>]*class="[^"]*result[^"]*"[^>]*>(.*?)</div>\s*(?=<div[^>]*class="[^"]*result|$)',
            body,
            re.S,
        )
        if not blocks:
            blocks = re.findall(r'<a[^>]*class="result__a"[^>]*>.*?</a>.*?(?=<a[^>]*class="result__a"|$)', body, re.S)
        # Lite fallback: table rows
        if not blocks:
            blocks = re.findall(r'<tr>.*?</tr>', body, re.S)

        for block in blocks[:max_results]:
            link_m = re.search(r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block, re.S)
            if not link_m:
                link_m = re.search(r'<a[^>]*class="result-link"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block, re.S)
            if not link_m:
                link_m = re.search(r'<a[^>]*href="(https?://[^"]*)"[^>]*>(.*?)</a>', block, re.S)
            if not link_m:
                continue

            raw_url = html.unescape(link_m.group(1))
            title = _strip_html(link_m.group(2)).strip()

            if "uddg=" in raw_url:
                real = re.search(r"uddg=([^&]+)", raw_url)
                if real:
                    raw_url = urllib.parse.unquote(real.group(1))

            snippet_m = re.search(r'class="result__snippet[^"]*"[^>]*>(.*?)</(?:a|span|div)>', block, re.S)
            if not snippet_m:
                snippet_m = re.search(r'<td[^>]*class="result-snippet[^"]*"[^>]*>(.*?)</td>', block, re.S)
            snippet = _strip_html(snippet_m.group(1)).strip() if snippet_m else ""

            if title and raw_url and "duckduckgo" not in raw_url:
                results.append({"title": title, "url": raw_url, "snippet": snippet})

        if results:
            return results

    # DDG rate-limited or unavailable, try Google
    return _google_fallback(query, max_results)


def ddg_news(query, max_results=8):
    """Search DuckDuckGo for news by appending 'news' context to the query.

    DDG's dedicated news tab requires JavaScript, so we use the regular
    HTML search with news-oriented query terms instead.
    """
    # Add "news" / "latest" to bias toward recent articles
    news_query = query
    q_lower = query.lower()
    if "news" not in q_lower and "latest" not in q_lower and "recent" not in q_lower:
        news_query = f"{query} latest news"
    rss_results = _google_news_rss(news_query, max_results)
    if any(isinstance(item, dict) and item.get("title") and item.get("url") for item in rss_results):
        return rss_results
    results = ddg_search(news_query, max_results)
    if any(isinstance(item, dict) and item.get("title") and item.get("url") for item in results):
        return results
    return rss_results


def web_fetch(url, max_length=6000):
    """Fetch one public web page with pinned DNS and bounded redirects."""
    try:
        max_length = min(max(100, int(max_length)), 20000)
    except (TypeError, ValueError):
        max_length = 6000
    status, headers, body, final_url, guard_error = _guarded_http_fetch(url, timeout=20)
    if guard_error.startswith("unsafe_url:"):
        return {"error": "unsafe_url", "reason": guard_error.split(":", 1)[1]}
    if guard_error == "redirect_limit":
        return {"error": "redirect_limit"}
    if guard_error == "content_too_large":
        return {"error": "content_too_large"}
    if status != 200:
        result = {"error": f"HTTP {status}" if status else "fetch_failed"}
        if status:
            result["status"] = status
        return result
    content_type = headers.get("content-type", "").lower()
    if content_type and not any(kind in content_type for kind in ("text/html", "application/xhtml+xml", "text/plain")):
        return {"error": "unsupported_content_type"}
    text = _extract_readable(body, max_length)
    if _web_fetch_challenge_page(body, text):
        return {"error": "blocked_page"}
    return {
        "url": final_url,
        "length": len(text),
        "content": text,
    }


# ---------------------------------------------------------------------------
# MCP server protocol (JSON-RPC over stdio)
# ---------------------------------------------------------------------------

def _respond(rid, result):
    msg = {"jsonrpc": "2.0", "id": rid, "result": result}
    line = json.dumps(msg) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


def _respond_error(rid, code, message):
    msg = {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
    line = json.dumps(msg) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


def _tool_result(text):
    return {"resultType": "complete", "content": [{"type": "text", "text": text}]}


def handle_request(msg):
    rid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params", {})

    if method == "server/discover":
        _respond(rid, {
            "resultType": "complete",
            "supportedVersions": ["2026-07-28"],
            "capabilities": {"tools": {"listChanged": False}},
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "eva-web-search", "version": "1.0.0"}},
        })
        return

    if method == "tools/list":
        _respond(rid, {"resultType": "complete", "tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})

        if name == "web_search":
            query = args.get("query", "")
            if not query:
                _respond(rid, _tool_result("Error: query is required"))
                return
            results = ddg_search(query, args.get("max_results", 8))
            _respond(rid, _tool_result(json.dumps(results, indent=2)))
            return

        if name == "web_search_news":
            query = args.get("query", "")
            if not query:
                _respond(rid, _tool_result("Error: query is required"))
                return
            results = ddg_news(query, args.get("max_results", 8))
            _respond(rid, _tool_result(json.dumps(results, indent=2)))
            return

        if name == "weather_current":
            location = args.get("location", "")
            if not location:
                _respond(rid, _tool_result("Error: location is required"))
                return
            _respond(rid, _tool_result(json.dumps(google_weather(location), indent=2)))
            return

        if name == "stock_quote":
            query = args.get("query", "")
            if not query:
                _respond(rid, _tool_result("Error: query is required"))
                return
            _respond(rid, _tool_result(json.dumps(google_stock_quote(query), indent=2)))
            return

        if name == "web_fetch":
            url = args.get("url", "")
            if not url:
                _respond(rid, _tool_result("Error: url is required"))
                return
            result = web_fetch(url, args.get("max_length", 6000))
            _respond(rid, _tool_result(json.dumps(result, indent=2)))
            return

        _respond_error(rid, -32601, f"Unknown tool: {name}")
        return

    if method == "ping":
        _respond(rid, {})
        return

    if rid is not None:
        _respond_error(rid, -32601, f"Method not found: {method}")


def main():
    print("[WebSearch MCP] Starting...", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle_request(msg)
        except Exception as e:
            rid = msg.get("id")
            if rid is not None:
                _respond_error(rid, -32603, f"Internal error: {e}")
            print(f"[WebSearch MCP] Error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
