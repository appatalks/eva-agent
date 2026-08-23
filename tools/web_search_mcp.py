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
import json
import math
import os
import re
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
        "description": "Return current Google weather conditions for an explicit city or region.",
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


def google_weather(location):
    location = str(location or "").strip()[:120]
    if not location:
        return [{"info": "Weather location is required"}]
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
    if not temperature and not condition:
        return _google_news_rss("weather forecast " + location + " today", 4)
    details = [value for value in (
        condition,
        (temperature + " F") if temperature else "",
        ("precipitation " + precipitation) if precipitation else "",
        ("humidity " + humidity) if humidity else "",
        ("wind " + wind) if wind else "",
    ) if value]
    return [{
        "title": "Current weather for " + location,
        "snippet": "; ".join(details),
        "url": url,
        "source": "Google Weather",
    }]


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
    """Fetch a URL and return extracted text."""
    max_length = min(max(100, max_length), 20000)
    # Basic URL validation
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"error": "Only http/https URLs are supported"}
    if not parsed.hostname:
        return {"error": "Invalid URL"}

    status, body = _http_get(url, timeout=20)
    if status != 200:
        return {"error": f"HTTP {status}", "body": body[:500]}

    text = _extract_readable(body, max_length)
    return {
        "url": url,
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
