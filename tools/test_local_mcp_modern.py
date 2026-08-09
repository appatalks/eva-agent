#!/usr/bin/env python3
"""Dual-era stdio MCP regression tests using deterministic local fixtures."""
import json
import sys
import tempfile
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bridge.local_mcp import MCPServer


FIXTURE = r'''
import json
import sys
import time

mode = sys.argv[1]
discover_count = 0
for raw_line in sys.stdin:
    request = json.loads(raw_line)
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    def reply(result=None, error=None):
        payload = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        print(json.dumps(payload), flush=True)

    if mode in {"legacy", "legacy-generic-error", "modern-legacy-advertised", "modern-reject-legacy-advertised", "modern-missing-result-type", "modern-probe-error-32020", "modern-probe-error-32021"}:
        if method == "server/discover":
            if mode == "modern-legacy-advertised":
                reply({"resultType": "complete", "supportedVersions": ["2024-11-05"]})
            elif mode == "modern-reject-legacy-advertised":
                reply(error={"code": -32022, "message": "Unsupported protocol version", "data": {"supported": ["2024-11-05"]}})
            elif mode == "modern-missing-result-type":
                reply({"supportedVersions": ["2026-07-28"]})
            elif mode in {"modern-probe-error-32020", "modern-probe-error-32021"}:
                code = -32020 if mode.endswith("32020") else -32021
                reply(error={"code": code, "message": "Retry legacy negotiation"})
            else:
                code = -32603 if mode == "legacy-generic-error" else -32601
                reply(error={"code": code, "message": "Method not found"})
        elif method == "initialize":
            assert params.get("protocolVersion") == "2024-11-05"
            reply({"serverInfo": {"name": "legacy-fixture"}, "capabilities": {"tools": {}}})
        elif method == "tools/list":
            reply({"tools": [{"name": "legacy_echo", "inputSchema": {"type": "object"}}]})
        elif method == "tools/call":
            reply({"content": [{"type": "text", "text": "legacy:" + params["arguments"]["value"]}]})
    elif mode in {"modern-unsupported", "modern-disjoint-advertised"}:
        if method == "server/discover":
            if mode == "modern-disjoint-advertised":
                reply({"resultType": "complete", "supportedVersions": ["2025-11-25"]})
            else:
                reply(error={"code": -32022, "message": "Unsupported protocol version", "data": {"supported": ["2025-11-25"]}})
        elif method == "initialize":
            reply({"serverInfo": {"name": "incorrect-fallback"}, "capabilities": {"tools": {}}})
    elif mode == "modern-late-discover":
        if method == "server/discover":
            discover_count += 1
            if discover_count == 1:
                time.sleep(3.1)
            reply({"resultType": "complete", "supportedVersions": ["2026-07-28"], "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "late-modern-fixture"}}})
        elif method == "initialize":
            reply(error={"code": -32022, "message": "Unsupported protocol version", "data": {"supported": ["2026-07-28"]}})
        elif method == "tools/list":
            reply({"resultType": "complete", "tools": [{"name": "late_echo", "inputSchema": {"type": "object"}}], "ttlMs": 1000, "cacheScope": "private"})
        elif method == "tools/call":
            reply({"resultType": "complete", "content": [{"type": "text", "text": "late:" + params["arguments"]["value"]}]})
    else:
        meta = params.get("_meta") or {}
        assert meta.get("io.modelcontextprotocol/protocolVersion") == "2026-07-28"
        assert meta.get("io.modelcontextprotocol/clientCapabilities") == {}
        if method == "server/discover":
            reply({"resultType": "complete", "supportedVersions": ["2026-07-28"], "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "modern-fixture"}}, "capabilities": {"tools": {}}})
        elif method == "tools/list":
            if mode == "modern-malformed-cache":
                reply({"resultType": "complete", "tools": [{"name": "modern_echo", "inputSchema": {"type": "object"}}], "ttlMs": "invalid", "cacheScope": "untrusted"})
            elif mode == "modern-invalid-page":
                reply({"resultType": "complete", "tools": "invalid", "ttlMs": 1000, "cacheScope": "private"})
            elif mode == "modern-invalid-cursor":
                reply({"resultType": "complete", "tools": [{"name": "modern_echo", "inputSchema": {"type": "object"}}], "nextCursor": 0, "ttlMs": 1000, "cacheScope": "private"})
            elif mode == "modern-tool-error":
                reply(error={"code": -32603, "message": "tool list failed"})
            elif params.get("cursor") == "page-2":
                reply({"resultType": "complete", "tools": [{"name": "modern_extra", "inputSchema": {"type": "object"}}], "ttlMs": 1000, "cacheScope": "private"})
            else:
                reply({"resultType": "complete", "tools": [{"name": "modern_echo", "inputSchema": {"type": "object"}}], "nextCursor": "page-2", "ttlMs": 1000, "cacheScope": "private"})
        elif method == "tools/call":
            reply({"resultType": "complete", "content": [{"type": "text", "text": "modern:" + params["arguments"]["value"]}]})
'''


def fixture_path():
    directory = Path(tempfile.mkdtemp(prefix="eva-mcp-modern-"))
    path = directory / "fixture.py"
    path.write_text(textwrap.dedent(FIXTURE), encoding="utf-8")
    return path


def test_server(mode, expected_era, expected_tool, expected_text):
    path = fixture_path()
    server = MCPServer("fixture-" + mode, sys.executable, [str(path), mode])
    try:
        server.start()
        assert server.protocol_era == expected_era
        assert server.tools[0]["name"] == expected_tool
        if expected_era == "modern":
            assert server.server_info == {"name": "modern-fixture"}
            assert [tool["name"] for tool in server.tools] == ["modern_echo", "modern_extra"]
            assert server.tool_cache_ttl_ms == 1000
            assert server.tool_cache_scope == "private"
        assert server.call_tool(expected_tool, {"value": "ok"}) == {"text": expected_text}
    finally:
        server.stop()
        path.unlink(missing_ok=True)
        path.parent.rmdir()


def assert_start_failure(mode, error_fragment):
    path = fixture_path()
    server = MCPServer("fixture-" + mode, sys.executable, [str(path), mode])
    try:
        try:
            server.start()
            raise AssertionError(mode + " should fail during startup")
        except RuntimeError as error:
            assert error_fragment in str(error)
        assert server.process is not None
        server.process.wait(timeout=5)
        assert server.process.poll() is not None
    finally:
        server.stop()
        path.unlink(missing_ok=True)
        path.parent.rmdir()


def main():
    test_server("legacy", "legacy", "legacy_echo", "legacy:ok")
    test_server("legacy-generic-error", "legacy", "legacy_echo", "legacy:ok")
    test_server("modern-legacy-advertised", "legacy", "legacy_echo", "legacy:ok")
    test_server("modern-reject-legacy-advertised", "legacy", "legacy_echo", "legacy:ok")
    test_server("modern-missing-result-type", "legacy", "legacy_echo", "legacy:ok")
    test_server("modern-probe-error-32020", "legacy", "legacy_echo", "legacy:ok")
    test_server("modern-probe-error-32021", "legacy", "legacy_echo", "legacy:ok")
    test_server("modern", "modern", "modern_echo", "modern:ok")
    malformed_path = fixture_path()
    malformed_server = MCPServer("fixture-malformed-cache", sys.executable, [str(malformed_path), "modern-malformed-cache"])
    try:
        malformed_server.start()
        assert malformed_server.tools[0]["name"] == "modern_echo"
        assert malformed_server.tool_cache_ttl_ms == 0
        assert malformed_server.tool_cache_scope == ""
    finally:
        malformed_server.stop()
        malformed_path.unlink(missing_ok=True)
        malformed_path.parent.rmdir()
    assert_start_failure("modern-unsupported", "rejected Eva's modern protocol")
    assert_start_failure("modern-disjoint-advertised", "rejected Eva's modern protocol")
    assert_start_failure("modern-invalid-page", "invalid tools/list page")
    assert_start_failure("modern-invalid-cursor", "invalid tools/list cursor")
    assert_start_failure("modern-tool-error", "rejected tools/list")
    late_path = fixture_path()
    late_server = MCPServer("fixture-modern-late-discover", sys.executable, [str(late_path), "modern-late-discover"])
    try:
        late_server.start()
        assert late_server.protocol_era == "modern"
        assert late_server.server_info == {"name": "late-modern-fixture"}
        assert late_server.call_tool("late_echo", {"value": "ok"}) == {"text": "late:ok"}
    finally:
        late_server.stop()
        late_path.unlink(missing_ok=True)
        late_path.parent.rmdir()
    print("local MCP modern compatibility tests: PASS")


if __name__ == "__main__":
    main()