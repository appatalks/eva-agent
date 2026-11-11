#!/usr/bin/env python3
"""Restricted ACP workspace tool backed exclusively by the Podman sandbox."""

import argparse
import json
import os
import signal
import sys
import threading

from bridge import config
from bridge.security_sandbox import SandboxError, run_in_sandbox, sandbox_preflight, validate_workspace


TOOLS = [{
    "name": "execute",
    "description": (
        "Run a coding command in the assigned network-isolated workspace container. "
        "Only /workspace is writable. Host files, credentials, external MCP servers, "
        "and network access are unavailable. Git worktree metadata is not mounted."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to execute inside /workspace."},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
        },
        "required": ["command"],
        "additionalProperties": False,
    },
}]


def respond(identifier, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": identifier, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--config-dir", required=True)
    args = parser.parse_args()
    config.EVA_CONFIG_DIR = os.path.abspath(args.config_dir)
    workspace = validate_workspace(args.workspace)
    if sandbox_preflight() != args.image:
        raise SandboxError("The approved sandbox image changed.")
    cancelled = threading.Event()
    def cancel(_signum, _frame):
        cancelled.set()
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    for line in sys.stdin:
        request = {}
        try:
            if cancelled.is_set():
                break
            if len(line) > 16384:
                continue
            request = json.loads(line)
            identifier = request.get("id")
            method = request.get("method")
            params = request.get("params") or {}
            if identifier is None:
                continue
            if method == "initialize":
                version = params.get("protocolVersion")
                respond(identifier, {
                    "protocolVersion": version if version in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"} else "2025-11-25",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "eva-sandbox", "version": "1.0.0"},
                })
            elif method == "server/discover":
                respond(identifier, {
                    "resultType": "complete", "supportedVersions": ["2026-07-28"],
                    "capabilities": {"tools": {"listChanged": False}},
                    "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "eva-sandbox", "version": "1.0.0"}},
                })
            elif method == "tools/list":
                respond(identifier, {"resultType": "complete", "tools": TOOLS})
            elif method == "ping":
                respond(identifier, {})
            elif method == "tools/call":
                values = params.get("arguments")
                if params.get("name") != "execute" or not isinstance(values, dict) or set(values) - {"command", "timeout_seconds"}:
                    raise SandboxError("Invalid sandbox tool request.")
                result = run_in_sandbox(workspace, args.image, values.get("command"), values.get("timeout_seconds", 120), cancelled)
                respond(identifier, {"content": [{"type": "text", "text": json.dumps(result)}]})
            else:
                respond(identifier, {"error": "Unsupported sandbox method."})
        except (ValueError, TypeError, SandboxError) as error:
            if isinstance(request, dict) and request.get("id") is not None:
                respond(request["id"], {"isError": True, "content": [{"type": "text", "text": str(error)[:240]}]})


if __name__ == "__main__":
    main()
