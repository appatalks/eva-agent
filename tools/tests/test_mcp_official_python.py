#!/usr/bin/env python3
"""Interop test between Eva's stdio transport and the official MCP Python SDK v2."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from bridge.local_mcp import MCPServer


def official_python():
    configured = os.environ.get("EVA_OFFICIAL_MCP_PYTHON", "")
    if configured:
        return configured
    return sys.executable if importlib.util.find_spec("mcp") else ""


def main():
    interpreter = official_python()
    if not interpreter:
        print("official Python MCP SDK interop: SKIP (set EVA_OFFICIAL_MCP_PYTHON or install mcp==2.0.0)")
        return
    fixture = ROOT / "tools" / "mcp_fixtures" / "official_python_v2_server.py"
    server = MCPServer("official-python-v2", interpreter, [str(fixture)])
    try:
        server.start()
        assert server.protocol_era == "modern"
        assert server.protocol_version == "2026-07-28"
        assert server.server_info == {"name": "eva-official-python-fixture", "version": "2.0.0"}
        assert any(tool.get("name") == "official_echo" for tool in server.tools)
        assert server.call_tool("official_echo", {"value": "ok"}) == {"text": "official:ok"}
    finally:
        server.stop()
    print("official Python MCP SDK v2 interop: PASS")


if __name__ == "__main__":
    main()