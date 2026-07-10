"""
Local MCP Client + Tool-Calling Agent
Provides the same data retrieval capability as ACP (Copilot CLI) but uses
a local LM Studio model for tool-calling reasoning, with no cloud AI access.

MCP servers are spawned directly as subprocesses and spoken to via JSON-RPC
over stdio, exactly like the Copilot CLI does internally.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error

from bridge import config as _cfg

_ARTIFACTS_DIR = os.path.expanduser("~/.config/eva-standalone/artifacts")


# ---------------------------------------------------------------------------
# MCP Server subprocess management
# ---------------------------------------------------------------------------

class MCPServer:
    """Manages a single MCP server subprocess (JSON-RPC over stdio)."""

    def __init__(self, name, command, args=None, env=None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.process = None
        self.tools = []           # list of tool dicts from tools/list
        self.lock = threading.Lock()
        self._request_id = 0
        self._pending = {}        # id -> {"event": Event, "result": ...}
        self._reader = None
        self.alive = False

    def start(self):
        """Spawn the MCP server process and initialize."""
        cmd = [self.command] + self.args
        explicit_env = {}
        for k, v in self.env.items():
            if k != "EVA_BRIDGE_TOKEN":
                explicit_env[k] = str(v) if not isinstance(v, str) else v
        explicit_env["EVA_ARTIFACTS_DIR"] = _ARTIFACTS_DIR
        process_env = _cfg.child_process_env(explicit_env)
        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=process_env,
            )
        except FileNotFoundError:
            raise RuntimeError(f"MCP server '{self.name}': command not found: {self.command}")

        self.alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # stderr drain
        threading.Thread(target=self._stderr_loop, daemon=True).start()

        # MCP initialize handshake
        init = self._send({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "eva-local-mcp", "version": "1.0.0"},
            },
        }, timeout=15)
        if init and "error" not in init:
            # Send initialized notification
            self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # Discover tools
        tools_resp = self._send({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/list",
            "params": {},
        }, timeout=10)
        if tools_resp and "tools" in tools_resp:
            self.tools = tools_resp["tools"]
        elif tools_resp and "result" in tools_resp and "tools" in tools_resp["result"]:
            self.tools = tools_resp["result"]["tools"]
        print(f"[LocalMCP] {self.name}: {len(self.tools)} tools discovered")

    def call_tool(self, tool_name, arguments, timeout=60):
        """Call an MCP tool and return the result text."""
        resp = self._send({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }, timeout=timeout)
        if not resp:
            return {"error": "no response"}
        if "error" in resp:
            return {"error": resp["error"]}
        result = resp.get("result", resp)
        # MCP tools return content as an array of {type, text} blocks
        if isinstance(result, dict) and "content" in result:
            parts = result["content"]
            if isinstance(parts, list):
                return {"text": "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")}
            return {"text": str(parts)}
        return {"text": json.dumps(result)}

    def stop(self):
        self.alive = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass

    def _next_id(self):
        self._request_id += 1
        return self._request_id

    def _write(self, msg):
        if not self.process or not self.process.stdin:
            return
        line = json.dumps(msg) + "\n"
        try:
            self.process.stdin.write(line.encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            self.alive = False

    def _send(self, msg, timeout=30):
        rid = msg.get("id")
        if rid is None:
            self._write(msg)
            return None
        event = threading.Event()
        self._pending[rid] = {"event": event, "result": None}
        self._write(msg)
        event.wait(timeout=timeout)
        entry = self._pending.pop(rid, {})
        return entry.get("result")

    def _read_loop(self):
        try:
            while self.alive and self.process and self.process.stdout:
                line = self.process.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = msg.get("id")
                if rid is not None and rid in self._pending:
                    # Unwrap result envelope
                    result = msg.get("result", msg)
                    if "error" in msg:
                        result = {"error": msg["error"]}
                    self._pending[rid]["result"] = result
                    self._pending[rid]["event"].set()
        except Exception:
            pass
        self.alive = False

    def _stderr_loop(self):
        try:
            while self.process and self.process.stderr:
                line = self.process.stderr.readline()
                if not line:
                    break
                print(f"[MCP:{self.name}] {line.decode(errors='replace').rstrip()}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Local MCP Manager — spawns/manages multiple MCP servers
# ---------------------------------------------------------------------------

class LocalMCPManager:
    """Manages multiple MCP servers and provides a unified tool catalog."""

    def __init__(self):
        self.servers = {}         # name -> MCPServer
        self._tool_map = {}       # tool_name -> server_name

    def start_servers(self, mcp_config):
        """Start MCP servers from config dict (same format as mcp.json mcpServers)."""
        for name, cfg in mcp_config.items():
            cmd = cfg.get("command", "")
            args = cfg.get("args", [])
            env = cfg.get("env", {})
            try:
                srv = MCPServer(name, cmd, args, env)
                srv.start()
                self.servers[name] = srv
                for tool in srv.tools:
                    tname = tool.get("name", "")
                    if tname:
                        self._tool_map[tname] = name
            except Exception as e:
                print(f"[LocalMCP] Failed to start {name}: {e}")

    def list_tools(self):
        """Return all tools across all servers as OpenAI-format function schemas."""
        tools = []
        for srv in self.servers.values():
            for t in srv.tools:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
                    },
                })
        return tools

    def call_tool(self, tool_name, arguments, timeout=60):
        """Route a tool call to the correct MCP server."""
        srv_name = self._tool_map.get(tool_name)
        if not srv_name or srv_name not in self.servers:
            return {"error": f"unknown tool: {tool_name}"}
        return self.servers[srv_name].call_tool(tool_name, arguments, timeout)

    def stop_all(self):
        for srv in self.servers.values():
            srv.stop()
        self.servers.clear()
        self._tool_map.clear()

    @property
    def alive(self):
        return any(s.alive for s in self.servers.values())

    @property
    def tool_count(self):
        return len(self._tool_map)


# ---------------------------------------------------------------------------
# Local Tool-Calling Agent — uses LM Studio for reasoning
# ---------------------------------------------------------------------------

def local_agent_query(user_message, mcp_manager, lms_base_url="http://localhost:1234/v1",
                      lms_model="", max_iterations=5, timeout=90):
    """Run a tool-calling agent loop using the local LM Studio model.

    1. Send the user message + tool schemas to LM Studio
    2. If the model returns tool_calls, execute them via MCP
    3. Feed results back and repeat until the model produces a text answer
    4. Return the final text

    Returns (data_text, model_used) matching _retrieve_acp_data_for() signature.
    """
    if not mcp_manager or not mcp_manager.alive:
        return "", ""

    tools = mcp_manager.list_tools()
    if not tools:
        return "", ""

    from bridge.lmstudio import post_json as _lmstudio_post_json

    messages = [
        {
            "role": "system",
            "content": (
                "You are a data retrieval assistant with access to tools. "
                "Use the tools to answer the user's request with REAL data. "
                "Do NOT fabricate data. Call tools to get actual results. "
                "After getting tool results, summarize the findings concisely. "
                "If no relevant tool exists for the request, say so."
            ),
        },
        {"role": "user", "content": user_message},
    ]

    model_used = lms_model or "local"
    _t0 = time.perf_counter()
    _deadline = _t0 + timeout

    for iteration in range(max_iterations):
        if time.perf_counter() > _deadline:
            print(f"[LocalAgent] Timeout after {iteration} iterations")
            break

        payload = {
            "model": lms_model or "default",
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.1,
        }

        remaining = max(10, _deadline - time.perf_counter())
        status, result, error = _lmstudio_post_json(lms_base_url, payload, timeout=remaining)
        if error:
            print(f"[LocalAgent] LM Studio request failed: {error}")
            return "", ""

        choice = (result.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        finish = choice.get("finish_reason", "")

        # Model wants to call tools
        tool_calls = msg.get("tool_calls", [])
        if tool_calls:
            # Append the assistant message with tool_calls
            messages.append(msg)

            for tc in tool_calls:
                fn = tc.get("function", {})
                tname = fn.get("name", "")
                try:
                    targs = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    targs = {}

                print(f"[LocalAgent] Calling tool: {tname}({json.dumps(targs)[:80]})")
                tool_result = mcp_manager.call_tool(tname, targs, timeout=30)

                result_text = tool_result.get("text", "") or tool_result.get("error", "tool error")
                # Truncate massive results
                if len(result_text) > 8000:
                    result_text = result_text[:8000] + "\n... (truncated)"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", f"call_{iteration}_{tname}"),
                    "content": result_text,
                })

            continue  # next iteration with tool results

        # Model produced a text response (no more tool calls)
        content = msg.get("content", "")
        if content:
            ms = round((time.perf_counter() - _t0) * 1000)
            print(f"[LocalAgent] Done in {iteration + 1} iterations, {ms}ms, {len(content)} chars")
            return content, model_used

        # finish_reason is "stop" but no content — done
        if finish == "stop":
            break

    return "", ""
