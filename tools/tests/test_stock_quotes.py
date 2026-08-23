#!/usr/bin/env python3
"""Deterministic local stock-quote capability contracts."""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import web_search_mcp as web
from bridge import core, state
from bridge.local_mcp import LocalMCPManager


FIXTURE_PAGE = '''AF_initDataCallback({key: "ds:3", data: [[[["PLG", "NYSEAMERICAN"], "Platinum Group Metals", [1.64, 0.12, 7.89]]]]});'''


class QuoteManager:
    alive = True

    def __init__(self, text):
        self.text = text
        self.calls = []

    def call_tool(self, name, arguments, timeout):
        self.calls.append((name, arguments, timeout))
        return {"text": self.text}


class QuoteFixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        body = json.dumps({
            "symbol": "PLG", "exchange": "NYSEAMERICAN", "price": 1.64,
            "currency": "USD", "source": "Loopback fixture",
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    assert web._stock_request("What is PLG stock price?") == (
        "PLG", ("NYSEAMERICAN", "NASDAQ", "NYSE", "OTCMKTS")
    )
    assert web._stock_request("PLG:AMEX") == ("PLG", ("NYSEAMERICAN",))
    assert web._stock_request(
        "Hi Eva, please give me a morning briefing and let me know the last stock price of PLG"
    ) == ("PLG", ("NYSEAMERICAN", "NASDAQ", "NYSE", "OTCMKTS"))
    assert web._stock_request("not a stock") == ("", ())

    ticker_output = "PLG           1.64      0.12     7.89%\n"
    with (patch.object(web, "_ticker_sh_path", return_value="/opt/eva/ticker.sh"),
          patch.object(web.subprocess, "run", return_value=web.subprocess.CompletedProcess(
              ["/opt/eva/ticker.sh", "PLG"], 0, ticker_output, "")) as ticker_run,
          patch.object(web, "_http_get", side_effect=AssertionError("ticker.sh should satisfy the quote"))):
        quote = web.google_stock_quote("PLG:AMEX")
    assert quote["symbol"] == "PLG"
    assert quote["exchange"] == "NYSEAMERICAN"
    assert quote["price"] == 1.64
    assert quote["change"] == 0.12
    assert quote["change_percent"] == 7.89
    assert quote["source"] == "ticker.sh (Yahoo Finance)"
    assert ticker_run.call_args.args[0] == ["/opt/eva/ticker.sh", "PLG"]

    with (patch.object(web, "_ticker_sh_path", return_value="/opt/eva/ticker.sh"),
          patch.object(web.subprocess, "run", return_value=web.subprocess.CompletedProcess(
              ["/opt/eva/ticker.sh", "PLG"], 0, "PLG 1.64 incomplete\n", ""))):
        assert web._ticker_sh_stock_quote("PLG", "NYSEAMERICAN") is None

    with (patch.object(web, "_ticker_sh_stock_quote", return_value=None),
          patch.object(web, "_http_get", return_value=(200, FIXTURE_PAGE))):
        quote = web.google_stock_quote("PLG:AMEX")
    assert quote["symbol"] == "PLG"
    assert quote["exchange"] == "NYSEAMERICAN"
    assert quote["price"] == 1.64
    assert quote["source"] == "Google Finance"

    local_receipt = {
        "symbol": "PLG", "exchange": "NYSEAMERICAN", "price": 1.64,
        "currency": "USD", "source": "Fixture quote",
    }
    with (patch.dict(os.environ, {"EVA_STOCK_QUOTE_URL": "http://127.0.0.1:9111/quote"}, clear=False),
          patch.object(web, "_ticker_sh_stock_quote", return_value=None),
          patch.object(web, "_http_get", return_value=(200, json.dumps(local_receipt)))):
        quote = web.google_stock_quote("PLG:AMEX")
    assert quote["source"] == "Fixture quote"
    with patch.dict(os.environ, {"EVA_STOCK_QUOTE_URL": "https://example.com/quote"}, clear=False):
        assert web._local_stock_quote_url() == ""

    with (patch.object(web, "_ticker_sh_stock_quote", return_value=None),
          patch.object(web, "_http_get", return_value=(200, "<title>Google Finance</title>"))):
        unavailable = web.google_stock_quote("PLG:NYSEAMERICAN")
    assert unavailable == {"error": "quote_unavailable", "symbol": "PLG"}

    quote_server = ThreadingHTTPServer(("127.0.0.1", 0), QuoteFixtureHandler)
    server_thread = threading.Thread(target=quote_server.serve_forever, daemon=True)
    server_thread.start()
    manager = LocalMCPManager()
    try:
        manager.start_servers({"eva-web-search": {
            "env": {"EVA_STOCK_QUOTE_URL": "http://127.0.0.1:" + str(quote_server.server_address[1]) + "/quote"}
        }})
        result = manager.call_tool("stock_quote", {"query": "PLG:AMEX"}, timeout=10)
        receipt = json.loads(result["text"])
        assert receipt["symbol"] == "PLG"
        assert receipt["exchange"] == "NYSEAMERICAN"
        assert receipt["source"] == "Loopback fixture"
    finally:
        manager.stop_all()
        quote_server.shutdown()
        quote_server.server_close()

    manager = QuoteManager(json.dumps(quote))
    original_manager = state.local_mcp_manager
    state.local_mcp_manager = manager
    try:
        receipt, model = core.BridgeHandler._retrieve_local_data("What is PLG stock price?")
    finally:
        state.local_mcp_manager = original_manager
    assert model == "local-stock-quote"
    assert json.loads(receipt)["stock_quote"]["symbol"] == "PLG"
    assert manager.calls == [("stock_quote", {"query": "What is PLG stock price?"}, 20)]

    unavailable_manager = QuoteManager(json.dumps(unavailable))
    state.local_mcp_manager = unavailable_manager
    try:
        receipt, model = core.BridgeHandler._retrieve_local_data("What is PLG stock price?")
    finally:
        state.local_mcp_manager = original_manager
    assert (receipt, model) == ("", "local-stock-quote")
    print("stock quote tests: PASS")


if __name__ == "__main__":
    main()
