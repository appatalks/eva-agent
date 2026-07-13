"""
Local MCP Client + Tool-Calling Agent
Provides the same data retrieval capability as ACP (Copilot CLI) but uses
a local LM Studio model for tool-calling reasoning, with no cloud AI access.

MCP servers are spawned directly as subprocesses and spoken to via JSON-RPC
over stdio, exactly like the Copilot CLI does internally.
"""

import json
import math
import os
import re
import signal
import subprocess
import threading
import time

from bridge import config as _cfg

_MCP_FRAME_MAX_BYTES = 1024 * 1024
_MCP_TOOL_ARGUMENT_MAX_BYTES = 64 * 1024
_MCP_TOOL_RESULT_MAX_BYTES = 1024 * 1024
_LOCAL_TOOL_BATCH_MAX = 4
_LOCAL_TOOL_CALL_TOTAL_MAX = 8
_LOCAL_TOOL_CONTRACTS = {
    "kusto_list_databases": ({}, frozenset()),
    "kusto_show_tables": ({}, frozenset()),
    "kusto_show_schema": ({"table": "identifier"}, frozenset({"table"})),
    "kusto_sample_data": ({"table": "identifier", "count": "count"}, frozenset({"table"})),
    "eva_recall_knowledge": ({"entity": "text", "limit": "count"}, frozenset({"entity"})),
    "eva_get_emotion_state": ({}, frozenset()),
    "eva_get_recent_reflections": ({"limit": "count"}, frozenset()),
    "eva_get_active_goals": ({"category": "category", "limit": "count"}, frozenset()),
    "eva_get_memory_summary": ({"period": "text", "limit": "count"}, frozenset()),
    "web_search": ({"query": "text", "max_results": "search_count"}, frozenset({"query"})),
    "web_search_news": ({"query": "text", "max_results": "search_count"}, frozenset({"query"})),
}


def _decode_mcp_frame(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > _MCP_FRAME_MAX_BYTES:
        raise RuntimeError("MCP frame is empty or too large")
    try:
        text = raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("MCP frame is not valid UTF-8") from exc
    if not text:
        raise RuntimeError("MCP frame is empty")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeError("MCP frame has duplicate members")
            result[key] = value
        return result

    def reject_constant(_value):
        raise RuntimeError("MCP frame has a non-standard number")

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise RuntimeError("MCP frame has a non-finite number")
        return parsed

    try:
        result = json.loads(
            text, object_pairs_hook=unique_object,
            parse_constant=reject_constant, parse_float=finite_float,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("MCP frame is invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("MCP frame must be an object")
    return result


def _expected_server_name(config_name):
    if config_name in ("sqlite", "sqlite-mcp-server", "eva-sqlite"):
        return "sqlite-mcp-server"
    return config_name


def _validate_initialize_result(result, expected_name):
    if (
        not isinstance(result, dict)
        or set(result) != {"protocolVersion", "capabilities", "serverInfo"}
        or result.get("protocolVersion") != "2024-11-05"
        or not isinstance(result.get("capabilities"), dict)
        or not isinstance(result.get("serverInfo"), dict)
    ):
        raise RuntimeError("MCP initialize response is invalid")
    info = result["serverInfo"]
    capabilities = result["capabilities"]
    if (
        set(capabilities) != {"tools"}
        or not isinstance(capabilities["tools"], dict)
        or set(capabilities["tools"]) - {"listChanged"}
        or (
            "listChanged" in capabilities["tools"]
            and not isinstance(capabilities["tools"]["listChanged"], bool)
        )
        or set(info) != {"name", "version"}
        or info.get("name") != expected_name
        or not isinstance(info.get("version"), str) or not info["version"]
        or len(info["version"]) > 64
    ):
        raise RuntimeError("MCP initialize metadata is invalid")


def _validate_tool_list(result, allowed_tools):
    if not isinstance(result, dict) or set(result) != {"tools"}:
        raise RuntimeError("MCP tools/list response is invalid")
    tools = result.get("tools")
    if (
        not isinstance(tools, list) or len(tools) > 128
    ):
        raise RuntimeError("MCP tool catalog is invalid")
    validated = []
    seen = set()
    for tool in tools:
        if (
            not isinstance(tool, dict)
            or set(tool) - {"name", "description", "inputSchema"}
            or not isinstance(tool.get("name"), str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool["name"]) is None
            or tool["name"] in seen
            or not isinstance(tool.get("description", ""), str)
            or len(tool.get("description", "")) > 4096
            or not isinstance(tool.get("inputSchema"), dict)
        ):
            raise RuntimeError("MCP advertised an invalid or unauthorized tool")
        try:
            schema = json.dumps(
                tool["inputSchema"], sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise RuntimeError("MCP tool schema is invalid") from exc
        if len(schema.encode("utf-8")) > 64 * 1024:
            raise RuntimeError("MCP tool schema is too large")
        seen.add(tool["name"])
        if tool["name"] not in allowed_tools:
            continue
        contract = _LOCAL_TOOL_CONTRACTS.get(tool["name"])
        if contract is None:
            raise RuntimeError("MCP tool has no local contract")
        validated.append({
            "name": tool["name"],
            "description": tool.get("description", ""),
            "inputSchema": _local_input_schema(tool["name"]),
        })
    return validated


def _local_input_schema(tool_name):
    fields, required = _LOCAL_TOOL_CONTRACTS[tool_name]
    properties = {}
    for name, kind in fields.items():
        properties[name] = {
            "type": "integer" if kind in ("count", "search_count") else "string"
        }
    schema = {
        "type": "object", "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = sorted(required)
    return schema


def _canonical_tool_arguments(tool_name, arguments):
    if not isinstance(arguments, dict):
        raise RuntimeError("MCP tool arguments must be an object")
    contract = _LOCAL_TOOL_CONTRACTS.get(tool_name)
    if contract is None:
        raise RuntimeError("MCP tool has no local argument contract")
    fields, required = contract
    if set(arguments) - set(fields) or not required.issubset(arguments):
        raise RuntimeError("MCP tool arguments have invalid fields")
    for name, value in arguments.items():
        kind = fields[name]
        if kind == "identifier" and (
            not isinstance(value, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value) is None
        ):
            raise RuntimeError("MCP identifier argument is invalid")
        if kind == "text" and (
            not isinstance(value, str) or not value or len(value) > 2000
            or "\x00" in value
        ):
            raise RuntimeError("MCP text argument is invalid")
        if kind == "category" and value not in (
            "self_improvement", "knowledge_curation", "relational"
        ):
            raise RuntimeError("MCP category argument is invalid")
        if kind == "count" and (
            isinstance(value, bool) or not isinstance(value, int)
            or not 1 <= value <= 100
        ):
            raise RuntimeError("MCP count argument is invalid")
        if kind == "search_count" and (
            isinstance(value, bool) or not isinstance(value, int)
            or not 1 <= value <= 20
        ):
            raise RuntimeError("MCP search count is invalid")
    try:
        encoded = json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeError("MCP tool arguments are invalid") from exc
    if len(encoded) > _MCP_TOOL_ARGUMENT_MAX_BYTES:
        raise RuntimeError("MCP tool arguments are too large")
    return arguments


# ---------------------------------------------------------------------------
# MCP Server subprocess management
# ---------------------------------------------------------------------------

class MCPServer:
    """Manages a single MCP server subprocess (JSON-RPC over stdio)."""

    def __init__(self, name, command, args=None, env=None, allowed_tools=None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.allowed_tools = frozenset(allowed_tools or ())
        self.process = None
        self.tools = []           # list of tool dicts from tools/list
        self.lock = threading.RLock()
        self._request_id = 0
        self._pending = {}        # id -> {"event": Event, "result": ...}
        self._reader = None
        self.alive = False
        self._stderr_reported = False
        self._process_group_id = None
        self._stop_lock = threading.Lock()

    def start(self):
        """Spawn the MCP server process and initialize."""
        cmd = [self.command] + self.args
        explicit_env = {}
        for k, v in self.env.items():
            if k != "EVA_BRIDGE_TOKEN":
                explicit_env[k] = str(v) if not isinstance(v, str) else v
        process_env = _cfg.child_process_env(explicit_env, profile="mcp")
        try:
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=process_env,
                start_new_session=True,
            )
            if type(self.process.pid) is int and self.process.pid > 0:
                self._process_group_id = self.process.pid
        except FileNotFoundError:
            raise RuntimeError(f"MCP server '{self.name}': command not found: {self.command}")

        try:
            self.alive = True
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
            threading.Thread(target=self._stderr_loop, daemon=True).start()

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
            _validate_initialize_result(
                init, _expected_server_name(self.name)
            )
            self._write({
                "jsonrpc": "2.0", "method": "notifications/initialized",
            })

            tools_resp = self._send({
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/list",
                "params": {},
            }, timeout=10)
            self.tools = _validate_tool_list(tools_resp, self.allowed_tools)
            if not self.alive:
                raise RuntimeError("MCP process exited during initialization")
            print(f"[LocalMCP] {self.name}: {len(self.tools)} read-only tools ready")
        except Exception:
            self.stop()
            raise

    def call_tool(self, tool_name, arguments, timeout=60):
        """Call an MCP tool and return the result text."""
        if tool_name not in self.allowed_tools or not any(
            tool.get("name") == tool_name for tool in self.tools
        ):
            return {"error": "tool is not authorized for local execution"}
        try:
            arguments = _canonical_tool_arguments(tool_name, arguments or {})
        except RuntimeError as exc:
            return {"error": str(exc)}
        resp = self._send({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }, timeout=timeout)
        result = resp
        # MCP tools return content as an array of {type, text} blocks
        if (
            not isinstance(result, dict)
            or set(result) - {"content", "isError"}
            or not isinstance(result.get("content"), list)
            or len(result["content"]) > 64
            or not isinstance(result.get("isError", False), bool)
        ):
            raise RuntimeError("MCP tool result is invalid")
        output = []
        total = 0
        for part in result["content"]:
            if (
                not isinstance(part, dict) or set(part) != {"type", "text"}
                or part.get("type") != "text"
                or not isinstance(part.get("text"), str)
            ):
                raise RuntimeError("MCP tool content is invalid")
            total += len(part["text"].encode("utf-8"))
            if total > _MCP_TOOL_RESULT_MAX_BYTES:
                raise RuntimeError("MCP tool result is too large")
            output.append(part["text"])
        text = "\n".join(output)
        return {"error" if result.get("isError") else "text": text}

    def stop(self):
        with self._stop_lock:
            self._stop_locked()

    def _stop_locked(self):
        self.alive = False
        process = self.process
        group_id = self._process_group_id
        if process:
            try:
                if process.stdin:
                    process.stdin.close()
            except Exception:
                pass
        group_gone = group_id is None
        if group_id is not None:
            try:
                os.killpg(group_id, signal.SIGTERM)
            except ProcessLookupError:
                group_gone = True
            except OSError:
                pass
        elif process:
            try:
                process.terminate()
            except Exception:
                pass
        if process:
            try:
                process.wait(timeout=5)
            except Exception:
                pass
        if group_id is not None and not group_gone:
            try:
                os.killpg(group_id, 0)
            except ProcessLookupError:
                group_gone = True
            except OSError:
                pass
        if group_id is not None and not group_gone:
            try:
                os.killpg(group_id, signal.SIGKILL)
                group_gone = True
            except ProcessLookupError:
                group_gone = True
            except OSError:
                pass
        if process:
            try:
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        self._process_group_id = None if group_gone else group_id

    def _next_id(self):
        with self.lock:
            self._request_id += 1
            return self._request_id

    def _write(self, msg):
        if not self.process or not self.process.stdin or not self.alive:
            raise RuntimeError("MCP process is not active")
        line = json.dumps(msg, allow_nan=False, separators=(",", ":")) + "\n"
        if len(line.encode("utf-8")) > _MCP_FRAME_MAX_BYTES:
            raise RuntimeError("MCP outbound frame is too large")
        try:
            with self.lock:
                self.process.stdin.write(line.encode("utf-8"))
                self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.alive = False
            raise RuntimeError("MCP process pipe failed") from exc

    def _send(self, msg, timeout=30):
        rid = msg.get("id")
        if rid is None:
            self._write(msg)
            return None
        event = threading.Event()
        with self.lock:
            self._pending[rid] = {
                "event": event, "result": None, "error": None,
                "completed": False,
            }
        try:
            self._write(msg)
        except Exception:
            with self.lock:
                self._pending.pop(rid, None)
            raise
        if not event.wait(timeout=timeout):
            with self.lock:
                self._pending.pop(rid, None)
            self.stop()
            raise RuntimeError("MCP request timed out")
        with self.lock:
            entry = self._pending.pop(rid, {})
        if entry.get("error") is not None:
            raise RuntimeError("MCP request failed")
        return entry.get("result")

    def _read_loop(self):
        try:
            while self.alive and self.process and self.process.stdout:
                line = self.process.stdout.readline(_MCP_FRAME_MAX_BYTES + 1)
                if not line:
                    break
                try:
                    if len(line) > _MCP_FRAME_MAX_BYTES or not line.endswith(b"\n"):
                        raise RuntimeError("MCP line exceeded the byte limit")
                    msg = _decode_mcp_frame(line)
                    if msg.get("jsonrpc") != "2.0":
                        raise RuntimeError("MCP JSON-RPC version is invalid")
                    if set(msg) not in (
                        {"jsonrpc", "id", "result"},
                        {"jsonrpc", "id", "error"},
                    ):
                        raise RuntimeError("MCP response envelope is invalid")
                    rid = msg.get("id")
                    if type(rid) is not int or rid <= 0:
                        raise RuntimeError("MCP response id is invalid")
                    with self.lock:
                        pending = self._pending.get(rid)
                        if pending is None:
                            raise RuntimeError("MCP response id is not pending")
                        if pending.get("completed", False):
                            raise RuntimeError("MCP response id was already completed")
                        if "error" in msg:
                            error = msg["error"]
                            if (
                                not isinstance(error, dict)
                                or set(error) - {"code", "message", "data"}
                                or type(error.get("code")) is not int
                                or not isinstance(error.get("message"), str)
                                or not error["message"]
                                or len(error["message"]) > 2000
                            ):
                                raise RuntimeError("MCP error response is invalid")
                            pending["error"] = error
                        else:
                            pending["result"] = msg["result"]
                        pending["completed"] = True
                        pending["event"].set()
                except Exception as exc:
                    self._protocol_violation(str(exc)[:200])
                    return
        except Exception as exc:
            self._protocol_violation("MCP reader failed: " + str(exc)[:160])
            return
        self.alive = False
        with self.lock:
            for pending in self._pending.values():
                if not pending.get("completed", False):
                    pending["error"] = {"code": -32000, "message": "MCP process exited"}
                    pending["completed"] = True
                    pending["event"].set()
            self.stop()

    def _stderr_loop(self):
        try:
            while self.process and self.process.stderr:
                line = self.process.stderr.readline(_MCP_FRAME_MAX_BYTES + 1)
                if not line:
                    break
                if len(line) > _MCP_FRAME_MAX_BYTES or not line.endswith(b"\n"):
                    self._protocol_violation("MCP stderr line exceeded the byte limit")
                    return
                if not self._stderr_reported:
                    self._stderr_reported = True
                    print(f"[LocalMCP] {self.name}: stderr output suppressed")
        except Exception as exc:
            self._protocol_violation("MCP stderr reader failed: " + str(exc)[:120])

    def _protocol_violation(self, reason):
        self.alive = False
        with self.lock:
            for pending in self._pending.values():
                if not pending.get("completed", False):
                    pending["error"] = {"code": -32600, "message": reason}
                    pending["completed"] = True
                    pending["event"].set()
        print(f"[LocalMCP] {self.name}: protocol violation")
        self.stop()


# ---------------------------------------------------------------------------
# Local MCP Manager — spawns/manages multiple MCP servers
# ---------------------------------------------------------------------------

class LocalMCPManager:
    """Manages multiple MCP servers and provides a unified tool catalog."""

    def __init__(self):
        self.servers = {}         # name -> MCPServer
        self._tool_map = {}       # tool_name -> server_name
        self._ready = False
        self._expected_server_count = 0

    def start_servers(self, mcp_config):
        """Start MCP servers from config dict (same format as mcp.json mcpServers)."""
        from bridge import state as _st

        safe_config, rejected = _cfg.mcp_config_for_local_execution(
            mcp_config, _st.egress_mode
        )
        if rejected:
            raise RuntimeError(
                "MCP process policy rejected server(s): "
                + ", ".join(sorted(rejected))
            )
        staged_servers = {}
        staged_tool_map = {}
        expected_server_count = len(safe_config)
        try:
            for name, cfg in safe_config.items():
                cmd = cfg.get("command", "")
                args = cfg.get("args", [])
                env = cfg.get("env", {})
                allowed_tools = _cfg.local_mcp_tool_allowlist(name)
                if allowed_tools is None:
                    raise RuntimeError(f"MCP server '{name}' has no local read-only profile")
                srv = MCPServer(name, cmd, args, env, allowed_tools)
                srv.start()
                if not srv.alive:
                    raise RuntimeError(f"MCP server '{name}' failed readiness")
                staged_servers[name] = srv
                for tool in srv.tools:
                    tname = tool.get("name", "")
                    if not tname or tname in staged_tool_map:
                        raise RuntimeError("MCP tool catalog contains a duplicate name")
                    staged_tool_map[tname] = name
        except Exception:
            for server in staged_servers.values():
                server.stop()
            raise
        self.stop_all()
        self.servers = staged_servers
        self._tool_map = staged_tool_map
        self._expected_server_count = expected_server_count
        self._ready = True

    def list_tools(self):
        """Return all tools across all servers as OpenAI-format function schemas."""
        tools = []
        for name, srv in self.servers.items():
            allowed = _cfg.local_mcp_tool_allowlist(name)
            if allowed is None:
                continue
            for t in srv.tools:
                if t.get("name") not in allowed:
                    continue
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
        allowed = _cfg.local_mcp_tool_allowlist(srv_name)
        if allowed is None or tool_name not in allowed:
            return {"error": "tool is not authorized for local execution"}
        try:
            return self.servers[srv_name].call_tool(
                tool_name, _canonical_tool_arguments(tool_name, arguments or {}), timeout
            )
        except RuntimeError:
            return {"error": "local MCP tool call failed closed"}

    def stop_all(self):
        for srv in list(self.servers.values()):
            try:
                srv.stop()
            except Exception:
                pass
        self.servers.clear()
        self._tool_map.clear()
        self._ready = False
        self._expected_server_count = 0

    @property
    def alive(self):
        return any(s.alive for s in self.servers.values())

    @property
    def ready(self):
        if not self._ready:
            return False
        if self._expected_server_count == 0:
            return True
        return (
            len(self.servers) == self._expected_server_count
            and all(server.alive for server in self.servers.values())
        )

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
    total_tool_calls = 0

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
            if not isinstance(tool_calls, list) or not 1 <= len(tool_calls) <= _LOCAL_TOOL_BATCH_MAX:
                return "", ""
            if total_tool_calls + len(tool_calls) > _LOCAL_TOOL_CALL_TOTAL_MAX:
                return "", ""
            # Append the assistant message with tool_calls
            messages.append(msg)

            seen_call_ids = set()
            for tc in tool_calls:
                if (
                    not isinstance(tc, dict)
                    or set(tc) != {"id", "type", "function"}
                    or tc.get("type") != "function"
                    or not isinstance(tc.get("id"), str)
                    or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tc["id"]) is None
                    or tc["id"] in seen_call_ids
                    or not isinstance(tc.get("function"), dict)
                    or set(tc["function"]) != {"name", "arguments"}
                ):
                    return "", ""
                seen_call_ids.add(tc["id"])
                fn = tc.get("function", {})
                tname = fn.get("name", "")
                raw_arguments = fn.get("arguments", "")
                if (
                    not isinstance(tname, str)
                    or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tname) is None
                    or not isinstance(raw_arguments, str)
                    or len(raw_arguments.encode("utf-8")) > _MCP_TOOL_ARGUMENT_MAX_BYTES
                ):
                    return "", ""
                try:
                    targs = _decode_mcp_frame(raw_arguments.encode("utf-8"))
                    _canonical_tool_arguments(tname, targs)
                except RuntimeError:
                    return "", ""

                print(f"[LocalAgent] Calling read-only tool: {tname}")
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
                total_tool_calls += 1

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
