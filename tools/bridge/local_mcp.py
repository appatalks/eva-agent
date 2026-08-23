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
from bridge.utils import _safe_child_environment

_ARTIFACTS_DIR = os.path.join(
    os.path.expanduser(os.environ.get("EVA_CONFIG_DIR", "~/.config/eva-standalone")), "artifacts"
)
_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MCP_MODERN_PROTOCOL_VERSION = "2026-07-28"
_MCP_DISCOVERY_TIMEOUT_SECONDS = 3
_MCP_TOOL_LIST_PAGE_MAX = 32
_MCP_CLIENT_INFO = {"name": "eva-local-mcp", "version": "1.0.0"}
# Eva consumes tools; it does not yet offer elicitation, subscriptions, or extensions.
_MCP_CLIENT_CAPABILITIES = {}


def _resolve_lmstudio_model(base_url, requested_model="", timeout=3):
    """Use an explicit override or the model currently exposed by LM Studio."""
    override = str(requested_model or "").strip()
    if override:
        return override, ""
    try:
        request = urllib.request.Request(base_url.rstrip("/") + "/models", method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        return "", "LM Studio model discovery failed: " + str(error)[:160]
    for item in payload.get("data") or []:
        model_id = str((item or {}).get("id") or "").strip()
        if model_id:
            return model_id[:240], ""
    return "", "LM Studio did not report a loaded model"

_MCP_ENV_KEYS = {
    "playwright": set(),
    "azure-mcp-server": {"AZURE_MCP_COLLECT_TELEMETRY"},
    "github-mcp-server": {"_useGitHubPAT", "GITHUB_PERSONAL_ACCESS_TOKEN"},
    "kusto-mcp-server": {
        "KUSTO_ACCESS_TOKEN", "KUSTO_CLUSTER_URL", "KUSTO_DATABASE",
        "KUSTO_DATABASE_LOCKED",
    },
    "computer-use-linux": set(),
    "eva-web-search": {"EVA_STOCK_QUOTE_URL", "EVA_TICKER_SH_PATH"},
}


def _mcp_launch_spec(name):
    """Return a fixed executable and arguments for a supported MCP server."""
    if name == "playwright":
        return "npx", ["-y", "@playwright/mcp@0.0.78"]
    if name == "azure-mcp-server":
        return "npx", ["-y", "@azure/mcp@3.0.0-beta.31", "server", "start"]
    if name == "github-mcp-server":
        return "docker", [
            "run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
            "ghcr.io/github/github-mcp-server",
        ]
    if name == "kusto-mcp-server":
        return sys.executable, [os.path.join(_TOOLS_DIR, "kusto_mcp.py")]
    if name == "computer-use-linux":
        return "computer-use-linux", ["mcp"]
    if name == "eva-web-search":
        return sys.executable, [os.path.join(_TOOLS_DIR, "web_search_mcp.py")]
    return None


def normalize_mcp_config(mcp_config):
    """Discard user-supplied commands and retain allowlisted server settings."""
    normalized = {}
    if not isinstance(mcp_config, dict):
        return normalized
    for name, config in mcp_config.items():
        spec = _mcp_launch_spec(name)
        if spec is None or not isinstance(config, dict):
            continue
        command, args = spec
        source_env = config.get("env", {})
        if not isinstance(source_env, dict):
            source_env = {}
        allowed_env = _MCP_ENV_KEYS[name]
        env = {key: value for key, value in source_env.items() if key in allowed_env}
        normalized[name] = {"command": command, "args": args, "env": env}
    return normalized


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
        self._generation = 0
        self.alive = False
        self.protocol_era = "modern"
        self.protocol_version = _MCP_MODERN_PROTOCOL_VERSION
        self.server_info = {}
        self.tool_cache_ttl_ms = 0
        self.tool_cache_scope = ""

    def start(self):
        """Spawn the MCP server process and initialize."""
        self._spawn()

        try:
            self._negotiate_protocol()
            self.tools = self._discover_tools()
            print(f"[LocalMCP] {self.name}: {len(self.tools)} tools discovered ({self.protocol_era})")
        except Exception:
            self.stop()
            raise

    def _spawn(self):
        """Start a fresh server process and its stdio readers."""
        cmd = [self.command] + self.args
        process_env = _safe_child_environment({"EVA_ARTIFACTS_DIR": _ARTIFACTS_DIR})
        process_env.update(_safe_child_environment(self.env))
        process_env.pop("EVA_BRIDGE_TOKEN", None)
        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=process_env,
            )
        except FileNotFoundError:
            raise RuntimeError(f"MCP server '{self.name}': command not found: {self.command}")

        self._generation += 1
        generation = self._generation
        self.process = process
        self.alive = True
        self._reader = threading.Thread(target=self._read_loop, args=(process, generation), daemon=True)
        self._reader.start()
        # stderr drain
        threading.Thread(target=self._stderr_loop, args=(process,), daemon=True).start()

    def call_tool(self, tool_name, arguments, timeout=60):
        """Call an MCP tool and return the result text."""
        resp = self._send_request("tools/call", {"name": tool_name, "arguments": arguments or {}}, timeout=timeout)
        if not resp:
            return {"error": "no response"}
        if "error" in resp:
            return {"error": resp["error"]}
        result = self._complete_result(resp, "tools/call")
        if isinstance(result, dict) and result.get("_mcp_error"):
            return {"error": result["_mcp_error"]}
        # MCP tools return content as an array of {type, text} blocks
        if isinstance(result, dict) and "content" in result:
            parts = result["content"]
            if isinstance(parts, list):
                return {"text": "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")}
            return {"text": str(parts)}
        return {"text": json.dumps(result)}

    def _negotiate_protocol(self):
        """Negotiate the supported modern MCP protocol."""
        discover = self._modern_discover()
        if (
            discover is None
            and self.alive
            and self.process
            and self.process.poll() is None
        ):
            discover = self._modern_discover()
        if self._is_modern_discover_result(discover):
            supported = self._supported_versions(discover)
            if _MCP_MODERN_PROTOCOL_VERSION in supported:
                self._select_modern_protocol(discover)
                return
            self._raise_unsupported_protocol(supported)

        if self._is_unsupported_protocol_error(discover):
            supported = self._supported_versions(discover.get("error") or {})
            self._raise_unsupported_protocol(supported)

        raise RuntimeError(f"MCP server '{self.name}' did not complete modern discovery.")

    def _modern_discover(self):
        return self._send(
            self._modern_request("server/discover", {}), timeout=_MCP_DISCOVERY_TIMEOUT_SECONDS
        )

    def _select_modern_protocol(self, discover):
        self.protocol_era = "modern"
        self.protocol_version = _MCP_MODERN_PROTOCOL_VERSION
        metadata = discover.get("_meta") if isinstance(discover, dict) else None
        server_info = metadata.get("io.modelcontextprotocol/serverInfo") if isinstance(metadata, dict) else None
        self.server_info = server_info if isinstance(server_info, dict) else {}

    def _raise_unsupported_protocol(self, supported):
        detail = ", ".join(str(version) for version in supported) or "no compatible versions"
        raise RuntimeError(f"MCP server '{self.name}' rejected Eva's modern protocol ({detail}).")

    @staticmethod
    def _supported_versions(value):
        source = value.get("supportedVersions") if isinstance(value, dict) else None
        if source is None and isinstance(value, dict):
            source = (value.get("data") or {}).get("supported")
        return [version for version in source if isinstance(version, str)] if isinstance(source, list) else []

    @staticmethod
    def _is_modern_discover_result(result):
        return (
            isinstance(result, dict)
            and result.get("resultType") == "complete"
            and isinstance(result.get("supportedVersions"), list)
        )

    @staticmethod
    def _is_unsupported_protocol_error(result):
        if not isinstance(result, dict) or not isinstance(result.get("error"), dict):
            return False
        return result["error"].get("code") == -32022

    def _modern_request(self, method, params):
        request_params = dict(params or {})
        request_params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": _MCP_MODERN_PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientInfo": dict(_MCP_CLIENT_INFO),
            "io.modelcontextprotocol/clientCapabilities": dict(_MCP_CLIENT_CAPABILITIES),
        }
        return {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": request_params,
        }

    def _send_request(self, method, params, timeout):
        return self._send(self._modern_request(method, params), timeout=timeout)

    def _discover_tools(self):
        tools = []
        cursor = None
        seen_cursors = set()
        for _ in range(_MCP_TOOL_LIST_PAGE_MAX):
            params = {"cursor": cursor} if cursor else {}
            tools_resp = self._send_request("tools/list", params, timeout=10)
            if not tools_resp or "error" in tools_resp:
                raise RuntimeError(f"MCP server '{self.name}' rejected tools/list.")
            result = self._complete_result(tools_resp, "tools/list")
            if not isinstance(result, dict) or result.get("_mcp_error"):
                raise RuntimeError(f"MCP server '{self.name}' returned an invalid tools/list result.")
            page_tools = result.get("tools")
            if not isinstance(page_tools, list) or any(not isinstance(tool, dict) for tool in page_tools):
                raise RuntimeError(f"MCP server '{self.name}' returned an invalid tools/list page.")
            tools.extend(page_tools)
            ttl_ms = result.get("ttlMs")
            self.tool_cache_ttl_ms = min(ttl_ms, 24 * 60 * 60 * 1000) \
                if isinstance(ttl_ms, int) and not isinstance(ttl_ms, bool) and ttl_ms >= 0 else 0
            cache_scope = result.get("cacheScope")
            self.tool_cache_scope = cache_scope if cache_scope in {"public", "private"} else ""
            cursor = result.get("nextCursor")
            if cursor is None:
                return tools
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise RuntimeError(f"MCP server '{self.name}' returned an invalid tools/list cursor.")
            seen_cursors.add(cursor)
        raise RuntimeError(f"MCP server '{self.name}' exceeded the tools/list page limit.")

    def _complete_result(self, response, method):
        if not response or "error" in response:
            return response
        result = response.get("result", response)
        if not isinstance(result, dict):
            return {"_mcp_error": f"Modern MCP server returned an invalid {method} result."}
        if result.get("resultType") == "complete":
            return result
        if result.get("resultType") == "input_required":
            return {"_mcp_error": f"MCP {method} requires interactive input, which is not enabled for this server."}
        return {"_mcp_error": f"Modern MCP server returned an unsupported {method} result type."}

    def stop(self):
        self._generation += 1
        self.alive = False
        process = self.process
        if process:
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    pass

    def _next_id(self):
        self._request_id += 1
        return self._request_id

    def _write(self, msg):
        process = self.process
        if not process or not process.stdin:
            return
        line = json.dumps(msg) + "\n"
        try:
            process.stdin.write(line.encode())
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            if process is self.process:
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

    def _read_loop(self, process, generation):
        try:
            while generation == self._generation and self.alive and process.stdout:
                line = process.stdout.readline()
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
        if generation == self._generation and process is self.process:
            self.alive = False

    def _stderr_loop(self, process):
        try:
            while process.stderr:
                line = process.stderr.readline()
                if not line:
                    break
                message = line.decode(errors="replace").rstrip()
                expected_discover_rejection = "server/discover" in message and (
                    "expect initialized request" in message
                    or "method invalid during initialization" in message
                )
                if not expected_discover_rejection:
                    print(f"[MCP:{self.name}] {message}")
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
        self.start_failures = {}  # name -> content-free reason

    def start_servers(self, mcp_config):
        """Start MCP servers from config dict (same format as mcp.json mcpServers)."""
        for name, cfg in normalize_mcp_config(mcp_config).items():
            cmd = cfg["command"]
            args = cfg["args"]
            env = cfg.get("env", {})
            unresolved_flags = [key for key in env if str(key).startswith("_")]
            if unresolved_flags:
                self.start_failures[name] = "credentials_unresolved"
                print(f"[LocalMCP] Skipping {name}: credentials are not resolved yet")
                continue
            try:
                srv = MCPServer(name, cmd, args, env)
                srv.start()
                self.servers[name] = srv
                for tool in srv.tools:
                    tname = tool.get("name", "")
                    if tname:
                        self._tool_map[tname] = name
            except Exception as e:
                self.start_failures[name] = "command_not_found" if "command not found" in str(e).lower() else "start_failed"
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

    lms_base = lms_base_url.rstrip("/")
    endpoint = f"{lms_base}/chat/completions"

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

    lms_model, model_error = _resolve_lmstudio_model(lms_base, lms_model)
    if model_error:
        print("[LocalAgent] " + model_error)
        return "", ""
    model_used = lms_model
    _t0 = time.perf_counter()
    _deadline = _t0 + timeout

    for iteration in range(max_iterations):
        if time.perf_counter() > _deadline:
            print(f"[LocalAgent] Timeout after {iteration} iterations")
            break

        payload = {
            "model": lms_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.1,
        }

        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            remaining = max(10, _deadline - time.perf_counter())
            with urllib.request.urlopen(req, timeout=remaining) as resp:
                result = json.loads(resp.read().decode())
        except Exception as e:
            print(f"[LocalAgent] LM Studio request failed: {e}")
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
