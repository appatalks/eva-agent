#!/usr/bin/env python3
"""Focused contract for automatic LM Studio loaded-model discovery."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from bridge.local_mcp import _resolve_lmstudio_model


class _ModelsHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404)
            return
        body = json.dumps({"data": [{"id": "loaded-local-model"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = "http://127.0.0.1:" + str(server.server_port) + "/v1"
    try:
        model, error = _resolve_lmstudio_model(base_url)
        assert (model, error) == ("loaded-local-model", "")
        model, error = _resolve_lmstudio_model(base_url, "explicit-override")
        assert (model, error) == ("explicit-override", "")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print("lmstudio model resolution tests: PASS")


if __name__ == "__main__":
    main()