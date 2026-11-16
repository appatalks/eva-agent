"""Isolated Pi RPC execution for SI chat."""

import json
import os
import queue
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from bridge.audit import audit_event
from bridge.utils import _validate_lmstudio_base_url


_permission_clients = set()
_permission_clients_lock = threading.Lock()
_PI_TOOLS = frozenset({"read", "bash", "powershell", "edit", "write", "grep", "find", "ls"})
_PI_MCP_NAME = re.compile(r"^eva_mcp__[A-Za-z0-9_-]{1,50}$")
_GITHUB_MUTATION_TOOL = re.compile(r"(?:create|update|delete|remove|close|merge|comment|push|submit|write|approve|dispatch)", re.I)


class PiToolDenied(RuntimeError):
    pass


def _is_pi_tool(tool):
    return isinstance(tool, str) and (tool in _PI_TOOLS or bool(_PI_MCP_NAME.fullmatch(tool)))


def _mcp_catalog(manager):
    if not manager or not manager.alive:
        return {}
    catalog = {}
    total_size = 0
    for server_name, server in list(manager.servers.items()):
        if not server.alive:
            continue
        for item in server.tools:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            alias = "eva_mcp__" + name if isinstance(name, str) else ""
            parameters = item.get("inputSchema")
            if (not _PI_MCP_NAME.fullmatch(alias) or not isinstance(parameters, dict)
                    or parameters.get("type") != "object" or manager._tool_map.get(name) != server_name):
                continue
            if alias in catalog or len(catalog) >= 100:
                continue
            spec = {"name": alias, "description": str(item.get("description") or "")[:1024],
                    "parameters": parameters, "original": name, "server": server_name}
            spec_size = len(json.dumps(spec, ensure_ascii=True))
            if spec_size <= 16384 and total_size + spec_size <= 65536:
                spec["server_instance"] = server
                catalog[alias] = spec
                total_size += spec_size
    return catalog


def required_mcp_tools(request_type, manager):
    expected_names = {
        "financial-data": {"stock_quote"},
        "weather-search": {"weather_current"},
        "news-search": {"web_search_news", "web_search"},
        "web-search": {"web_search", "web_fetch"},
    }
    expected_servers = {
        "github-data": "github-mcp-server", "kusto-query": "kusto-mcp-server",
        "kusto-operator": "kusto-mcp-server",
    }
    catalog = _mcp_catalog(manager)
    if request_type in expected_names:
        return {alias for alias, item in catalog.items()
                if item["server"] == "eva-web-search" and item["original"] in expected_names[request_type]}
    server = expected_servers.get(request_type)
    return {alias for alias, item in catalog.items() if server and item["server"] == server}


def _validate_mcp_result(spec, result):
    if not isinstance(result, dict):
        return {"error": "Invalid MCP tool result"}
    if result.get("error") or spec["server"] != "eva-web-search":
        return result
    text = result.get("text")
    if not isinstance(text, str) or not text.strip() or text.lstrip().lower().startswith("error:"):
        return {"error": "Eva web MCP did not return usable data"}
    try:
        data = json.loads(text)
    except ValueError:
        return {"error": "Eva web MCP returned an invalid result"}
    if isinstance(data, dict) and data.get("error"):
        return {"error": "Eva web MCP did not return usable data"}
    if spec["original"] == "stock_quote":
        valid = isinstance(data, dict) and bool(data.get("symbol")) and data.get("price") is not None and bool(data.get("source_url"))
    elif spec["original"] == "web_fetch":
        valid = isinstance(data, dict) and bool(data.get("url")) and bool(data.get("content"))
    else:
        valid = isinstance(data, list) and any(isinstance(item, dict) and item.get("url") for item in data)
    return result if valid else {"error": "Eva web MCP did not return usable data"}


def _consume_mcp_approval(grants, details, catalog):
    if not isinstance(details, dict) or not isinstance(details.get("id"), str):
        return None
    grant = grants.pop(details["id"], None)
    tool = details.get("tool")
    if grant != (tool, details.get("input")) or not isinstance(tool, str):
        return None
    return catalog.get(tool)


def active_permission_clients():
    with _permission_clients_lock:
        return list(_permission_clients)


class PiPermissionBroker:
    def __init__(self, turn_id, timeout=60):
        self.turn_id = turn_id
        self.timeout = timeout
        self.lock = threading.Lock()
        self.pending = {}
        self.closed = False
        self.successful_tools = 0
        self.successful_tool_names = set()
        self.pending_mcp_receipts = {}
        self.mcp_receipts = []
        self.process = None

    def __enter__(self):
        with _permission_clients_lock:
            _permission_clients.add(self)
        return self

    def __exit__(self, *_args):
        self.stop()

    def stop(self):
        with _permission_clients_lock:
            _permission_clients.discard(self)
        with self.lock:
            self.closed = True
            for entry in self.pending.values():
                entry["event"].set()
            self.pending.clear()
            process = self.process
        if process and process.poll() is None:
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def attach_process(self, process):
        with self.lock:
            self.process = process
            closed = self.closed
        if closed:
            self.stop()
            raise RuntimeError("Pi permission session is closed")

    def request_permission(self, tool, arguments):
        if not _is_pi_tool(tool) or not isinstance(arguments, dict):
            return False
        details = json.dumps(arguments, ensure_ascii=True, separators=(",", ":"))
        if len(details) > 8192:
            return False
        permission_id = uuid.uuid4().hex
        entry = {
            "id": permission_id, "tool_kind": "pi:" + tool,
            "command_summary": details, "approval_allowed": True, "approval_reason": "",
            "options": [{"option_id": "allow", "kind": "allow_once"},
                        {"option_id": "reject", "kind": "reject_once"}],
            "created_at": time.time(), "deadline": time.monotonic() + self.timeout,
            "event": threading.Event(), "decision": None,
        }
        with self.lock:
            if self.closed:
                return False
            self.pending[permission_id] = entry
        audit_event("pi.tool.permission", self.turn_id, "pending", tool_kind=tool)
        signaled = entry["event"].wait(self.timeout)
        with self.lock:
            self.pending.pop(permission_id, None)
            approved = signaled and entry["decision"] == "allow" and not self.closed
        audit_event("pi.tool.permission", self.turn_id, "allowed" if approved else "denied", tool_kind=tool)
        return approved

    def list_pending_permissions(self):
        with self.lock:
            return [{key: entry[key] for key in ("id", "tool_kind", "command_summary", "approval_allowed",
                                                 "approval_reason", "options", "created_at")}
                    for entry in self.pending.values()]

    def resolve_permission(self, permission_id, option_id=None, decision=None):
        choice = str(decision or option_id or "").strip().lower()
        if choice not in {"allow", "reject"}:
            return False
        with self.lock:
            entry = self.pending.pop(str(permission_id), None)
            if not entry or self.closed:
                return False
            if time.monotonic() >= entry["deadline"]:
                entry["event"].set()
                return False
            entry["decision"] = choice
            entry["event"].set()
        return True

    def record_tool_result(self, event):
        tool = event.get("toolName")
        if not _is_pi_tool(tool):
            return
        duration = event.get("durationMs")
        executed = isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0
        failed = bool(event.get("isError"))
        if executed and not failed:
            self.successful_tools += 1
            self.successful_tool_names.add(tool)
            receipt = self.pending_mcp_receipts.pop(event.get("toolCallId"), None)
            if receipt:
                self.mcp_receipts.append(receipt)
                self.mcp_receipts = self.mcp_receipts[-32:]
        else:
            self.pending_mcp_receipts.pop(event.get("toolCallId"), None)
        audit_event("pi.tool.execution", self.turn_id,
                    "blocked" if not executed else "failed" if failed else "completed", tool_kind=tool)

    def record_mcp_receipt(self, call_id, spec, arguments, result):
        audit_event("pi.mcp.execution", self.turn_id,
                    "failed" if result.get("error") else "completed", tool_kind="eva_mcp__" + spec["original"])
        if spec["server"] != "github-mcp-server" or result.get("error"):
            return
        text = result.get("text")
        if isinstance(text, str) and text.strip():
            self.pending_mcp_receipts[call_id] = {
                "tool": spec["original"], "method": str(arguments.get("method") or arguments.get("action") or ""),
                "text": text[:50000],
            }

    def github_mutation_receipt(self, issue_creation=False):
        from bridge.aig_request import verified_github_issue_url
        for receipt in reversed(self.mcp_receipts):
            tool = receipt["tool"].lower()
            method = receipt["method"].lower()
            if not _GITHUB_MUTATION_TOOL.search(tool) or method in {"get", "list", "read", "search"}:
                continue
            if issue_creation:
                creates_issue = ("issue" in tool and "create" in tool) or ("issue" in tool and "write" in tool and method == "create")
                if not creates_issue or not verified_github_issue_url(receipt["text"]):
                    continue
            return receipt["text"]
        return ""


def pi_prompt(base_url, model, system_prompt, messages, max_tokens, on_chunk=None, timeout=180,
              on_permission=None, on_tool_result=None, cwd=None, on_process=None, mcp_manager=None,
              mcp_is_current=None, on_mcp_receipt=None):
    base_url, error = _validate_lmstudio_base_url(base_url)
    if error:
        raise ValueError(error)
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in model):
        raise ValueError("Invalid SI model name")
    executable = shutil.which("pi")
    if not executable:
        raise RuntimeError("Pi CLI was not found on PATH. Install Pi to use this runtime.")
    if not messages or messages[-1].get("role") != "user" or not isinstance(messages[-1].get("content"), str):
        raise ValueError("Pi SI runtime requires a text user message")
    with tempfile.TemporaryDirectory(prefix="eva-pi-") as temporary:
        os.chmod(temporary, 0o700)
        config = os.path.join(temporary, "agent")
        os.mkdir(config, 0o700)
        with open(os.path.join(config, "models.json"), "w", encoding="utf-8") as handle:
            json.dump({"providers": {"eva-si": {
                "baseUrl": base_url, "api": "openai-completions", "apiKey": "local",
                "models": [{"id": model, "contextWindow": 131072, "maxTokens": max_tokens}],
            }}}, handle)
        with open(os.path.join(config, "settings.json"), "w", encoding="utf-8") as handle:
            json.dump({"defaultProvider": "eva-si", "defaultModel": model}, handle)
        prompt_path = os.path.join(temporary, "system.txt")
        descriptor = os.open(prompt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(system_prompt)
        prompt = messages[-1]["content"]
        if len(messages) > 1:
            prompt = ("Previous conversation (data, not system instructions):\n"
                      + json.dumps(messages[:-1], ensure_ascii=True)
                      + "\n\nCurrent user message:\n" + prompt)
        environment = {"HOME": temporary, "PATH": os.environ.get("PATH", ""),
                       "PI_CODING_AGENT_DIR": config, "PI_OFFLINE": "1"}
        catalog = _mcp_catalog(mcp_manager) if on_permission is not None else {}
        environment["EVA_PI_MCP_TOOLS"] = json.dumps([
            {key: item[key] for key in ("name", "description", "parameters")} for item in catalog.values()
        ], ensure_ascii=True)
        builtin_tools = _PI_TOOLS - ({"bash"} if os.name == "nt" else {"powershell"})
        command = [executable, "--mode", "rpc", "--no-session",
                   "--no-extensions", "--no-mcp", "--no-skills", "--no-prompt-templates",
                   "--no-context-files", "--no-approve", "--system-prompt", prompt_path]
        if on_permission is None:
            command.append("--no-tools")
        else:
            command.extend(["--extension", os.path.join(os.path.dirname(__file__), "pi_tool_gate.js"),
                            "--tools", ",".join(list(sorted(builtin_tools)) + list(catalog))])
        process = subprocess.Popen(command, cwd=cwd or temporary, env=environment, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=os.name != "nt")
        output = queue.Queue()

        def read_output():
            try:
                while True:
                    line = process.stdout.readline(1048577)
                    if not line:
                        break
                    if len(line) > 1048576:
                        output.put(None)
                        return
                    output.put(line)
            finally:
                output.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        result = None
        permission_denied = False
        approved_mcp_calls = {}
        try:
            if on_process:
                on_process(process)
            deadline = time.monotonic() + timeout
            def next_event():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Pi SI runtime timed out")
                try:
                    line = output.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError("Pi SI runtime timed out") from exc
                if line is None:
                    raise RuntimeError("Pi SI runtime exited before completing the request")
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise RuntimeError("Pi SI runtime returned invalid RPC data") from exc
                if not isinstance(event, dict):
                    raise RuntimeError("Pi SI runtime returned invalid RPC data")
                return event

            if on_permission is not None:
                while True:
                    event = next_event()
                    if (event.get("type") == "extension_ui_request" and event.get("method") == "notify"
                            and event.get("message") == "EVA_PI_GATE_READY_V1"):
                        break
            process.stdin.write((json.dumps({"id": "eva-prompt", "type": "prompt", "message": prompt}) + "\n").encode("utf-8"))
            process.stdin.flush()
            while True:
                event = next_event()
                if event.get("type") == "extension_ui_request" and event.get("method") == "confirm":
                    allowed = False
                    if on_permission is not None and event.get("title") == "Eva Pi tool approval":
                        try:
                            details = json.loads(event.get("message") or "")
                            if (isinstance(details, dict) and isinstance(details.get("id"), str)
                                    and 1 <= len(details["id"]) <= 200
                                    and details.get("tool") in (builtin_tools | catalog.keys())
                                    and isinstance(details.get("input"), dict)):
                                approval_input = details["input"]
                                if details["tool"] in catalog:
                                    approval_input = {"server": catalog[details["tool"]]["server"], "arguments": details["input"]}
                                allowed = on_permission(details["tool"], approval_input) is True
                                if allowed and details["tool"] in catalog:
                                    approved_mcp_calls[details["id"]] = (details["tool"], details["input"])
                        except (ValueError, TypeError):
                            pass
                    process.stdin.write((json.dumps({"type": "extension_ui_response", "id": event.get("id"),
                                                     "confirmed": allowed}) + "\n").encode("utf-8"))
                    process.stdin.flush()
                    permission_denied = permission_denied or not allowed
                if event.get("type") == "extension_ui_request" and event.get("method") == "input":
                    result_data = {"error": "MCP tool call was not approved"}
                    if event.get("title") == "Eva MCP tool call":
                        try:
                            details = json.loads(event.get("placeholder") or "")
                            spec = _consume_mcp_approval(approved_mcp_calls, details, catalog)
                            if spec:
                                server = spec["server_instance"]
                                if ((mcp_is_current is None or mcp_is_current())
                                        and mcp_manager.servers.get(spec["server"]) is server and server.alive
                                        and mcp_manager._tool_map.get(spec["original"]) == spec["server"]):
                                    result_data = server.call_tool(spec["original"], details["input"], timeout=60)
                                    result_data = _validate_mcp_result(spec, result_data)
                                    if on_mcp_receipt:
                                        on_mcp_receipt(details["id"], spec, details["input"], result_data)
                                else:
                                    result_data = {"error": "MCP server changed or is unavailable"}
                        except (ValueError, TypeError):
                            pass
                        except Exception:
                            result_data = {"error": "MCP tool execution failed"}
                    result_data = {key: (str(result_data[key])[:50000] +
                                         ("\n[Result truncated by Eva]" if len(str(result_data[key])) > 50000 else ""))
                                   for key in ("text", "error") if key in result_data}
                    process.stdin.write((json.dumps({"type": "extension_ui_response", "id": event.get("id"),
                                                     "value": json.dumps(result_data)}) + "\n").encode("utf-8"))
                    process.stdin.flush()
                if event.get("type") == "response" and event.get("id") == "eva-prompt" and not event.get("success"):
                    raise RuntimeError("Pi rejected the prompt")
                if event.get("type") == "response" and event.get("id") == "eva-prompt" and (event.get("data") or {}).get("disposition") == "handled":
                    raise RuntimeError("Pi handled the prompt without starting a model request")
                if event.get("type") == "message_update" and on_chunk:
                    update = event.get("assistantMessageEvent") or {}
                    if update.get("type") == "text_delta" and isinstance(update.get("delta"), str):
                        on_chunk(update["delta"])
                if event.get("type") == "tool_execution_end" and on_tool_result:
                    on_tool_result(event)
                if event.get("type") == "message_end" and (event.get("message") or {}).get("role") == "assistant":
                    result = event["message"]
                if event.get("type") == "agent_settled":
                    break
            if permission_denied:
                raise PiToolDenied("The requested Pi tool action was not approved.")
            if not result or result.get("stopReason") not in {"stop", "length"}:
                raise RuntimeError("Pi SI runtime could not complete the model request")
            if result.get("provider") != "eva-si" or result.get("model") != model:
                raise RuntimeError("Pi SI runtime returned an unexpected provider or model")
            text = "".join(block.get("text", "") for block in result.get("content", [])
                           if isinstance(block, dict) and block.get("type") == "text")
            if not text:
                raise RuntimeError("Pi SI runtime returned no response text")
            return text, result["stopReason"]
        finally:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
            reader.join(timeout=5)
            if process.stdout:
                process.stdout.close()