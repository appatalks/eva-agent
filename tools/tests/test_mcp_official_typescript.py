#!/usr/bin/env python3
"""Interop test between Eva's stdio transport and the official MCP TypeScript SDK v2."""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from bridge.local_mcp import MCPServer


def main():
    sdk_root = Path(os.environ.get("EVA_OFFICIAL_MCP_TYPESCRIPT_ROOT", ""))
    if not sdk_root.is_dir() or not (sdk_root / "node_modules" / "@modelcontextprotocol" / "server").is_dir():
        print("official TypeScript MCP SDK interop: SKIP (set EVA_OFFICIAL_MCP_TYPESCRIPT_ROOT)")
        return
    fixture_source = ROOT / "tools" / "mcp_fixtures" / "official_typescript_v2_server.cjs"
    fixture_path = sdk_root / "eva-official-typescript-fixture.cjs"
    shutil.copyfile(fixture_source, fixture_path)
    server = MCPServer("official-typescript-v2", "node", [str(fixture_path)])
    try:
        server.start()
        assert server.protocol_era == "modern"
        assert server.protocol_version == "2026-07-28"
        assert any(tool.get("name") == "official_typescript_echo" for tool in server.tools)
        assert server.call_tool("official_typescript_echo", {"value": "ok"}) == {"text": "typescript:ok"}
    finally:
        server.stop()
        fixture_path.unlink(missing_ok=True)
    print("official TypeScript MCP SDK v2 interop: PASS")


if __name__ == "__main__":
    main()