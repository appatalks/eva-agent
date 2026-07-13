#!/usr/bin/env python3
"""
Eva Web Search MCP Server
A lightweight MCP server providing web search (DuckDuckGo, no API key) and
page content extraction. Designed for Eva's local mode where the Copilot CLI
(and its built-in Bing) is not available.

Tools:
    web_search       — Search DuckDuckGo and return results with snippets
    web_search_news  — Search DuckDuckGo News for recent headlines

Runs as a stdio MCP server (JSON-RPC over stdin/stdout).
"""

import html
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

from bridge.mcp_protocol import (
    MCPProtocolError,
    MAX_MCP_FRAME_BYTES,
    decode_request_line,
    encode_response_line,
    fixed_tool_schema,
    validate_fixed_tool_arguments,
)

_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_SEARCH_HOSTS = frozenset({
    "html.duckduckgo.com", "lite.duckduckgo.com", "www.google.com",
})

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
]
TOOLS = [tool for tool in TOOLS if tool.get("name") != "web_fetch"]
for _tool in TOOLS:
    _tool["inputSchema"] = fixed_tool_schema(_tool["name"])


# ---------------------------------------------------------------------------
# DuckDuckGo search (HTML scraping, no API key)
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_get(url, timeout=15):
    """Fetch one fixed search-provider HTTPS URL without redirects."""
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return 0, "Error: invalid search URL"
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _SEARCH_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        return 0, "Error: search destination is not allowed"
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        import certifi
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls_context.load_verify_locations(cafile=certifi.where())
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=tls_context),
            _NoRedirect(),
        )
        with opener.open(req, timeout=timeout) as resp:
            # Read up to 512KB
            body = resp.read(512 * 1024)
            charset = resp.headers.get_content_charset() or "utf-8"
            # DuckDuckGo sometimes returns 202 with valid content
            return 200, body.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return 0, f"Error: {e}"


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
    content_lines = [line for line in lines if len(line.strip()) > 40]
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
        if "google.com" in raw_url or "youtube.com/sorry" in raw_url:
            continue
        seen.add(raw_url)
        results.append({"title": title, "url": raw_url, "snippet": ""})
        if len(results) >= max_results:
            break

    return results if results else [{"info": "Search temporarily unavailable", "query": query}]


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
    return ddg_search(news_query, max_results)


def web_fetch(url, max_length=6000):
    """Arbitrary page retrieval is disabled until brokered DNS pinning exists."""
    return {"error": "web_fetch is disabled by Eva's public-egress policy"}


# ---------------------------------------------------------------------------
# MCP server protocol (JSON-RPC over stdio)
# ---------------------------------------------------------------------------

def _respond(rid, result):
    msg = {"jsonrpc": "2.0", "id": rid, "result": result}
    line = encode_response_line(msg)
    sys.stdout.write(line)
    sys.stdout.flush()


def _respond_error(rid, code, message):
    msg = {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
    line = encode_response_line(msg)
    sys.stdout.write(line)
    sys.stdout.flush()


def _tool_result(text):
    return {"content": [{"type": "text", "text": text}]}


def handle_request(msg):
    rid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params", {})

    if method == "initialize":
        _respond(rid, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "eva-web-search", "version": "1.0.0"},
        })
        return

    if method == "notifications/initialized":
        return  # no response needed for notifications

    if method == "tools/list":
        if params:
            _respond_error(rid, -32602, "MCP pagination is unsupported")
            return
        _respond(rid, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name", "")
        try:
            args = validate_fixed_tool_arguments(
                name, params.get("arguments", {})
            )
        except MCPProtocolError:
            _respond_error(rid, -32602, "Invalid tool arguments")
            return

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

        _respond_error(rid, -32601, f"Unknown tool: {name}")
        return

    if method == "ping":
        _respond(rid, {})
        return

    if rid is not None:
        _respond_error(rid, -32601, f"Method not found: {method}")


def main():
    print("[WebSearch MCP] Starting...", file=sys.stderr)
    while True:
        line = sys.stdin.buffer.readline(MAX_MCP_FRAME_BYTES + 1)
        if not line:
            break
        if len(line) > MAX_MCP_FRAME_BYTES or not line.endswith(b"\n"):
            break
        if not line.strip():
            continue
        try:
            msg = decode_request_line(line)
        except MCPProtocolError:
            continue
        try:
            handle_request(msg)
        except Exception:
            rid = msg.get("id")
            if rid is not None:
                _respond_error(rid, -32603, "Internal error")
            print("[WebSearch MCP] Request failed", file=sys.stderr)


if __name__ == "__main__":
    main()
