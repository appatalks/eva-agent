"""Bridge domain: acp_client."""

import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from bridge import config as _cfg
from bridge import state as _st
from bridge.kusto import _inject_kusto_token
from bridge.sensitive import redact_credentials
from bridge.telemetry import _telemetry_emit

_ACP_POOL_MAX = _cfg.ACP_POOL_MAX
_ARTIFACTS_DIR = _cfg.ARTIFACTS_DIR
_COPILOT_PREFLIGHT = {}
_COPILOT_PREFLIGHT_LOCK = threading.Lock()
_COPILOT_REQUIRED_FLAGS = (
    "--acp", "--disable-builtin-mcps", "--no-bash-env",
    "--no-custom-instructions", "--no-remote", "--no-remote-export",
)


def _parse_jsonc(text):
    if not isinstance(text, str) or len(text) > 2 * 1024 * 1024:
        raise RuntimeError("Copilot auth config is invalid")
    output = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(text) and text[index:index + 2] != "*/":
                if text[index] in "\r\n":
                    output.append(text[index])
                index += 1
            if index + 1 >= len(text):
                raise RuntimeError("Copilot auth config has an unterminated comment")
            index += 2
            continue
        output.append(char)
        index += 1
    if in_string:
        raise RuntimeError("Copilot auth config has an unterminated string")

    stripped = "".join(output)
    output = []
    index = 0
    in_string = False
    escaped = False
    while index < len(stripped):
        char = stripped[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(stripped) and stripped[lookahead].isspace():
                lookahead += 1
            if lookahead < len(stripped) and stripped[lookahead] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    try:
        value = json.loads("".join(output))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeError("Copilot auth config is invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Copilot auth config must be an object")
    return value


def _safe_auth_value(value, *, depth=0, budget=None):
    if budget is None:
        budget = [0]
    budget[0] += 1
    if depth > 8 or budget[0] > 2048:
        raise RuntimeError("Copilot auth state is too complex")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeError("Copilot auth state contains a non-finite number")
        return value
    if isinstance(value, str):
        if "\x00" in value or len(value) > 16384:
            raise RuntimeError("Copilot auth state contains invalid text")
        return value
    if isinstance(value, list):
        if len(value) > 128:
            raise RuntimeError("Copilot auth state list is too large")
        return [
            _safe_auth_value(item, depth=depth + 1, budget=budget)
            for item in value
        ]
    if isinstance(value, dict):
        if len(value) > 128:
            raise RuntimeError("Copilot auth state object is too large")
        result = {}
        for key, item in value.items():
            if (
                not isinstance(key, str) or not key or "\x00" in key
                or len(key) > 256 or key in ("__proto__", "constructor", "prototype")
            ):
                raise RuntimeError("Copilot auth state key is invalid")
            result[key] = _safe_auth_value(
                item, depth=depth + 1, budget=budget
            )
        return result
    raise RuntimeError("Copilot auth state contains unsupported data")


def _auth_projection(source_path):
    try:
        info = os.stat(source_path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("Copilot authentication config is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or hasattr(os, "getuid") and info.st_uid != os.getuid()
    ):
        raise RuntimeError("Copilot authentication config is not owner-only")
    try:
        with open(source_path, encoding="utf-8") as handle:
            source = _parse_jsonc(handle.read())
    except OSError as exc:
        raise RuntimeError("Copilot authentication config is unreadable") from exc
    projection = {
        "disableAllHooks": True,
        "trustedFolders": [],
        "ide": {"autoConnect": False},
        "bashEnv": False,
    }
    for key in ("lastLoggedInUser", "loggedInUsers", "schemaVersion"):
        if key in source:
            projection[key] = _safe_auth_value(source[key])
    return projection


def _trusted_executable(path):
    candidate = path
    if not os.path.isabs(candidate):
        candidate = shutil.which(candidate, path=_cfg._fixed_child_path()) or ""
    candidate = os.path.realpath(candidate) if candidate else ""
    if not candidate:
        raise RuntimeError("Copilot CLI executable was not found on the trusted path")
    try:
        info = os.stat(candidate)
    except OSError as exc:
        raise RuntimeError("Copilot CLI executable is unavailable") from exc
    owners = {0}
    if hasattr(os, "getuid"):
        owners.add(os.getuid())
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in owners
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not info.st_mode & stat.S_IXUSR
    ):
        raise RuntimeError("Copilot CLI executable is not trusted")
    parent = os.path.dirname(candidate)
    while parent and parent != "/":
        parent_info = os.stat(parent)
        if parent_info.st_uid not in owners or parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError("Copilot CLI executable parent is not trusted")
        parent = os.path.dirname(parent)
    return candidate


def _platform_copilot_candidate(loader):
    if os.path.basename(loader) != "npm-loader.js":
        return loader
    system = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    machine = {"x86_64": "x64", "AMD64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if not system or not machine:
        raise RuntimeError("Copilot CLI platform is unsupported")
    return os.path.join(
        os.path.dirname(loader), "node_modules",
        f"@github/copilot-{system}-{machine}", "copilot",
    )


def _resolve_and_preflight_copilot(path):
    loader = _trusted_executable(path)
    executable = _trusted_executable(_platform_copilot_candidate(loader))
    try:
        identity = (executable, os.stat(executable).st_mtime_ns, os.stat(executable).st_size)
    except OSError as exc:
        raise RuntimeError("Copilot CLI executable changed during validation") from exc
    with _COPILOT_PREFLIGHT_LOCK:
        if _COPILOT_PREFLIGHT.get(identity):
            return executable
    env = _cfg.child_process_env(profile="acp")
    try:
        version = subprocess.run(
            [executable, "--version"], capture_output=True, text=True,
            timeout=10, env=env, cwd="/",
        )
        help_result = subprocess.run(
            [executable, "--help"], capture_output=True, text=True,
            timeout=10, env=env, cwd="/",
        )
        contained_help = subprocess.run(
            [
                executable, "--acp", "--stdio", "--disable-builtin-mcps",
                "--no-bash-env", "--no-custom-instructions", "--no-remote",
                "--no-remote-export", "--help",
            ],
            capture_output=True, text=True, timeout=10, env=env, cwd="/",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Copilot CLI preflight failed") from exc
    help_text = (help_result.stdout or "") + (help_result.stderr or "")
    version_text = (version.stdout or "") + (version.stderr or "")
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)(?:[-.]\d+)?\b", version_text)
    if (
        version.returncode != 0 or help_result.returncode != 0
        or contained_help.returncode != 0 or not match
        or tuple(int(part) for part in match.groups()) < (1, 0, 0)
        or any(flag not in help_text for flag in _COPILOT_REQUIRED_FLAGS)
    ):
        raise RuntimeError("Copilot CLI does not satisfy the ACP containment contract")
    with _COPILOT_PREFLIGHT_LOCK:
        _COPILOT_PREFLIGHT[identity] = True
    return executable


def _inherited_disabled_mcp_names(home=None):
    names = {"computer-use-linux"}
    home = home or os.environ.get("COPILOT_HOME") or os.path.expanduser("~/.copilot")
    path = os.path.join(home, "mcp-config.json")
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return tuple(sorted(names))
    except OSError as exc:
        raise RuntimeError(
            "Copilot MCP config could not be inspected safely"
        ) from exc
    if size > 1024 * 1024:
        raise RuntimeError("Copilot MCP config is too large to inspect safely")
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise RuntimeError(
            "Copilot MCP config could not be inspected safely"
        ) from exc
    if not isinstance(raw, dict):
        raise RuntimeError("Copilot MCP config must be an object")
    servers = raw.get("mcpServers", raw)
    if not isinstance(servers, dict):
        raise RuntimeError("Copilot MCP server config must be an object")
    for name in servers:
        names.add(str(name))
    return tuple(sorted(names))

class ACPClient:
    """Manages the copilot --acp --stdio subprocess and ACP JSON-RPC protocol."""

    PROTOCOL_VERSION = 1  # ACP protocol major version

    def __init__(self, copilot_path="copilot", cwd=None, model=None, mcp_config=None):
        self.copilot_path = copilot_path
        self.cwd = cwd or os.getcwd()
        self.model = model  # None = use CLI default
        self.mcp_config = mcp_config or {}  # MCP servers config dict
        self.process = None
        self.request_id = 0
        self.lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.pending = {}           # id -> {"event": Event, "result": None, "error": None}
        self.session_id = None
        self.response_chunks = {}   # prompt_id -> accumulated text
        self.reader_thread = None
        self.agent_info = {}
        self.alive = False
        self.terminals = {}  # terminal_id -> {"process": Popen, "output": str}
        self._prompt_lock = threading.Lock()  # Serialize prompt calls
        # Terminal authority remains disabled until the complete ACP terminal
        # contract is implemented behind Eva's capability broker.
        self._terminal_allowed = False
        self._source_copilot_home = (
            os.environ.get("COPILOT_HOME") or os.path.expanduser("~/.copilot")
        )
        self._runtime_dir = None
        self._runtime_home = None
        self._runtime_os_home = None
        self._runtime_cwd = None

    def _prepare_isolated_runtime(self):
        if self._runtime_dir:
            return
        os.makedirs(_ARTIFACTS_DIR, mode=0o700, exist_ok=True)
        root = tempfile.mkdtemp(prefix="copilot-acp-", dir=_ARTIFACTS_DIR)
        os.chmod(root, 0o700)
        runtime_home = os.path.join(root, "home")
        runtime_os_home = os.path.join(root, "os-home")
        runtime_cwd = os.path.join(root, "workspace")
        os.mkdir(runtime_home, 0o700)
        os.mkdir(runtime_os_home, 0o700)
        os.mkdir(runtime_cwd, 0o700)

        auth_config = os.path.join(self._source_copilot_home, "config.json")
        try:
            projection = _auth_projection(auth_config)
            if projection is not None:
                output_path = os.path.join(runtime_home, "config.json")
                descriptor = os.open(
                    output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(projection, handle, sort_keys=True, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

        self._runtime_dir = root
        self._runtime_home = runtime_home
        self._runtime_os_home = runtime_os_home
        self._runtime_cwd = runtime_cwd

    def _cleanup_isolated_runtime(self):
        root = self._runtime_dir
        self._runtime_dir = None
        self._runtime_home = None
        self._runtime_os_home = None
        self._runtime_cwd = None
        if root:
            shutil.rmtree(root, ignore_errors=True)

    def _session_mcp_servers(self):
        servers = []
        for name, cfg in self.mcp_config.items():
            env = []
            for key, value in sorted((cfg.get("env") or {}).items()):
                if str(key).startswith("_"):
                    continue
                if not isinstance(value, str):
                    raise RuntimeError("MCP environment values must be strings")
                env.append({"name": str(key), "value": value})
            servers.append({
                "name": name,
                "command": cfg["command"],
                "args": list(cfg["args"]),
                "env": env,
            })
        return servers

    # --- Lifecycle ---

    def start(self):
        """Spawn copilot subprocess, initialize ACP, create session."""
        safe_mcp, rejected_mcp = _cfg.mcp_config_for_egress(
            self.mcp_config, _st.egress_mode
        )
        if rejected_mcp:
            raise RuntimeError(
                "MCP process policy rejected server(s): "
                + ", ".join(sorted(rejected_mcp))
            )
        self.mcp_config = safe_mcp
        if os.name == "nt":
            raise RuntimeError(
                "ACP MCP isolation is unavailable on Windows in this release"
            )
        inherited_names = _inherited_disabled_mcp_names(
            self._source_copilot_home
        )
        executable = _resolve_and_preflight_copilot(self.copilot_path)
        self._prepare_isolated_runtime()
        cmd = [
            executable, "--acp", "--stdio", "--disable-builtin-mcps",
            "--no-bash-env",
            "--no-custom-instructions", "--no-remote", "--no-remote-export",
        ]
        for server_name in inherited_names:
            cmd.extend(["--disable-mcp-server", server_name])
        if self.model:
            cmd.extend(["--model", self.model])
        try:
            process_env = _cfg.child_process_env(profile="acp")
            process_env["EVA_ARTIFACTS_DIR"] = _ARTIFACTS_DIR
            process_env["COPILOT_HOME"] = self._runtime_home
            process_env["HOME"] = self._runtime_os_home

            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=process_env,
                cwd=self._runtime_cwd,
            )
        except FileNotFoundError:
            self._cleanup_isolated_runtime()
            raise RuntimeError(
                f"Copilot CLI not found at '{self.copilot_path}'. "
                "Install it (https://github.com/github/copilot-cli) and authenticate with 'copilot auth login'."
            )
        except Exception:
            self._cleanup_isolated_runtime()
            raise

        self.alive = True

        # Start reader thread
        self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.reader_thread.start()

        # Start stderr reader (for debug logging)
        threading.Thread(target=self._stderr_loop, daemon=True).start()

        # Initialize connection
        init_result = self._send_request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "clientCapabilities": {
                "terminal": self._terminal_allowed
            },
            "clientInfo": {
                "name": "eva-acp-bridge",
                "title": "Eva ACP Bridge",
                "version": "1.0.0"
            }
        }, timeout=30)

        if init_result and "error" not in init_result:
            self.agent_info = init_result.get("agentInfo", {})
            caps = init_result.get("agentCapabilities", {})
            print(f"[ACP] Connected to: {self.agent_info.get('name', 'unknown')} "
                  f"v{self.agent_info.get('version', '?')} "
                  f"(protocol v{init_result.get('protocolVersion', '?')})")
            print(f"[ACP] Capabilities: {json.dumps(caps, indent=2)}")
        else:
            self.stop()
            raise RuntimeError("Copilot ACP initialize failed")

        # Create session — pass MCP servers via ACP session/new if configured
        mcp_servers_for_session = self._session_mcp_servers()
        session_result = self._send_request("session/new", {
            "cwd": self._runtime_cwd,
            "mcpServers": mcp_servers_for_session
        }, timeout=30)

        if session_result and "sessionId" in session_result:
            self.session_id = session_result["sessionId"]
            print(f"[ACP] Session created: {self.session_id}")
        else:
            self.stop()
            raise RuntimeError("Copilot ACP session creation failed")

    def stop(self):
        """Shut down the copilot subprocess."""
        self.alive = False
        self.session_id = None
        self._current_prompt_id = None
        if self.process:
            try:
                self.process.stdin.close()
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
        self._cleanup_isolated_runtime()

    # --- JSON-RPC Communication ---

    def _next_id(self):
        with self.lock:
            self.request_id += 1
            return self.request_id

    def _send_request(self, method, params, timeout=120):
        """Send a JSON-RPC request and wait for the response."""
        rid = self._next_id()
        event = threading.Event()
        self.pending[rid] = {"event": event, "result": None, "error": None}

        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params
        }) + "\n"

        try:
            self._write_raw(msg)
        except (BrokenPipeError, OSError, RuntimeError) as e:
            self.pending.pop(rid, None)
            return {"error": f"Copilot process pipe error: {e}"}

        completed = event.wait(timeout=timeout)
        if not completed:
            self.pending.pop(rid, None)
            self._cancel_and_quarantine(f"request timeout: {method}")
            return {"error": f"Copilot request timed out after {timeout}s"}

        entry = self.pending.pop(rid, {})
        if entry.get("error"):
            return {"error": entry["error"]}
        return entry.get("result")

    def _write_raw(self, text):
        """Write one complete NDJSON frame without interleaving writers."""
        if not self.process or not self.process.stdin or not self.alive:
            raise RuntimeError("Copilot process is not active")
        with self._write_lock:
            self.process.stdin.write(text.encode("utf-8"))
            self.process.stdin.flush()

    def _cancel_and_quarantine(self, reason):
        """Best-effort cancel, then retire a timed-out ACP session.

        ACP updates do not carry a prompt identifier Eva can safely use for
        late-chunk routing. A timed-out session is therefore never reused.
        """
        session_id = self.session_id
        if session_id and self.process and self.process.stdin and self.alive:
            notice = json.dumps({
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": session_id},
            }) + "\n"
            try:
                self._write_raw(notice)
            except (BrokenPipeError, OSError, RuntimeError):
                pass
        print(f"[ACP] Quarantining session ({reason})")
        self.stop()

    def _send_response(self, rid, result):
        """Send a JSON-RPC response (for server-initiated requests like requestPermission)."""
        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "result": result
        }) + "\n"
        try:
            self._write_raw(msg)
        except (BrokenPipeError, OSError, RuntimeError):
            pass

    def _send_error_response(self, rid, code, message):
        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": code, "message": message},
        }) + "\n"
        try:
            self._write_raw(msg)
        except (BrokenPipeError, OSError, RuntimeError):
            pass

    # --- Reader Loop ---

    def _read_loop(self):
        """Continuously read NDJSON lines from copilot stdout."""
        while self.alive:
            try:
                line = self.process.stdout.readline()
                if not line:
                    print("[ACP] Copilot stdout closed")
                    self.alive = False
                    # Unblock any pending requests
                    for rid in list(self.pending):
                        self.pending[rid]["error"] = "Copilot process exited"
                        self.pending[rid]["event"].set()
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    self._handle_message(msg)
                except json.JSONDecodeError:
                    print(f"[ACP] Non-JSON line: {redact_credentials(line[:200])}")
            except Exception as e:
                print(f"[ACP] Reader error: {e}")
                break

    def _stderr_loop(self):
        """Read copilot stderr for debug output."""
        while self.alive:
            try:
                line = self.process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                print(f"[Copilot stderr] {redact_credentials(text)}")
            except Exception:
                break

    def _handle_message(self, msg):
        """Route incoming JSON-RPC messages."""
        # Response to our request
        if "id" in msg and "result" in msg:
            rid = msg["id"]
            if rid in self.pending:
                self.pending[rid]["result"] = msg["result"]
                self.pending[rid]["event"].set()
            return

        # Error response to our request
        if "id" in msg and "error" in msg:
            rid = msg["id"]
            if rid in self.pending:
                self.pending[rid]["error"] = msg["error"]
                self.pending[rid]["event"].set()
            return

        # Notification: session/update
        if msg.get("method") == "session/update":
            self._handle_session_update(msg.get("params", {}))
            return

        # Server-initiated request: session/request_permission
        if "id" in msg and msg.get("method") == "session/request_permission":
            params = msg.get("params", {})
            tool_call = params.get("toolCall", {}) if isinstance(params.get("toolCall"), dict) else {}
            tool_kind = str(tool_call.get("kind", "other") or "other")
            tool_id = str(tool_call.get("toolCallId", "") or "")[:64]
            options = params.get("options", []) if isinstance(params.get("options"), list) else []

            reject_option = next((
                opt for opt in options
                if isinstance(opt, dict) and opt.get("kind") in ("reject_once", "reject_always") and opt.get("optionId")
            ), None)
            if reject_option:
                print(f"[ACP] Permission REJECT kind={tool_kind} id={tool_id}")
                outcome = {"outcome": "selected", "optionId": reject_option["optionId"]}
            else:
                print(f"[ACP] Permission CANCEL kind={tool_kind} id={tool_id}")
                outcome = {"outcome": "cancelled"}
            self._send_response(msg["id"], {"outcome": outcome})
            return

        # Server-initiated requests for terminal
        if "id" in msg and msg.get("method") == "terminal/create":
            print("[ACP] terminal/create DENIED (terminal capability disabled)")
            self._send_error_response(msg["id"], -32601, "Terminal capability is disabled")
            return

        if "id" in msg and msg.get("method") == "terminal/output":
            self._send_error_response(msg["id"], -32601, "Terminal capability is disabled")
            return

        if "id" in msg and msg.get("method") == "terminal/release":
            self._send_error_response(msg["id"], -32601, "Terminal capability is disabled")
            return

        if "id" in msg and msg.get("method", "").startswith("terminal/"):
            self._send_error_response(msg["id"], -32601, "Terminal capability is disabled")
            return

        # Server-initiated requests for fs (decline)
        if "id" in msg and msg.get("method", "").startswith("fs/"):
            print(f"[ACP] Declining capability request: {msg.get('method')}")
            self._send_error_response(msg["id"], -32601, "Method not supported by bridge")
            return

        # Unknown message
        if "id" in msg and "method" in msg:
            # Unknown server request — respond with error
            print(f"[ACP] Unknown server request: {msg.get('method')}")
            self._send_error_response(msg["id"], -32601, "Not implemented")

    def _handle_session_update(self, params):
        """Accumulate text from agent_message_chunk updates."""
        update = params.get("update", {})
        update_type = update.get("sessionUpdate", "")

        if update_type == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text":
                text = content.get("text", "")
                # Accumulate into current prompt's response
                if "_current_prompt_id" in self.__dict__ and self._current_prompt_id:
                    pid = self._current_prompt_id
                    if pid not in self.response_chunks:
                        self.response_chunks[pid] = ""
                    self.response_chunks[pid] += text

        elif update_type == "plan":
            # Log the plan for debugging
            entries = update.get("entries", [])
            if entries:
                print(f"[ACP] Agent plan: {', '.join(e.get('content','') for e in entries[:5])}")

        elif update_type in ("tool_call", "tool_call_update"):
            status = update.get("status", "")
            title = update.get("title", "")
            if title or status:
                print(f"[ACP] Tool: {title} [{status}]")

    # --- Terminal handlers (for ACP tool execution) ---

    def _handle_terminal_create(self, rid, params):
        """Terminal execution is unavailable until brokered."""
        self._send_error_response(rid, -32601, "Terminal capability is disabled")

    def _handle_terminal_output(self, rid, params):
        """Terminal output is unavailable until brokered."""
        self._send_error_response(rid, -32601, "Terminal capability is disabled")

    def _handle_terminal_release(self, rid, params):
        """Terminal release is unavailable until brokered."""
        self._send_error_response(rid, -32601, "Terminal capability is disabled")

    # --- Public API ---

    def prompt(self, text, timeout=120):
        """Send a text prompt and return the accumulated response text.
        Serialized per client to prevent cross-talk between concurrent callers."""
        with self._prompt_lock:
            return self._prompt_impl(text, timeout)

    def _prompt_impl(self, text, timeout=120):
        if not self.alive or not self.session_id:
            return {"error": "No active ACP session"}

        pid = self._next_id()
        self._current_prompt_id = pid
        self.response_chunks[pid] = ""

        _t0 = time.perf_counter()
        try:
            result = self._send_request("session/prompt", {
                "sessionId": self.session_id,
                "prompt": [{"type": "text", "text": text}]
            }, timeout=timeout)
        finally:
            response_text = self.response_chunks.pop(pid, "")
            if self._current_prompt_id == pid:
                self._current_prompt_id = None
        _ms = round((time.perf_counter() - _t0) * 1000.0, 1)

        if result and isinstance(result, dict):
            if "error" in result:
                _telemetry_emit("acp_prompt", model=self.model or "default",
                                prompt_chars=len(text or ""), response_chars=0,
                                ms=_ms, stop_reason="error")
                return {"error": result["error"]}
            stop_reason = result.get("stopReason", "end_turn")
            _telemetry_emit("acp_prompt", model=self.model or "default",
                            prompt_chars=len(text or ""), response_chars=len(response_text or ""),
                            ms=_ms, stop_reason=stop_reason)
            return {"text": response_text, "stop_reason": stop_reason}

        _telemetry_emit("acp_prompt", model=self.model or "default",
                        prompt_chars=len(text or ""), response_chars=len(response_text or ""),
                        ms=_ms, stop_reason="end_turn")
        return {"text": response_text, "stop_reason": "end_turn"}

    def prompt_with_image(self, text, image_b64, mime="image/jpeg", timeout=120):
        """Send a text + image prompt (serialized)."""
        with self._prompt_lock:
            return self._prompt_with_image_impl(text, image_b64, mime, timeout)

    def _prompt_with_image_impl(self, text, image_b64, mime="image/jpeg", timeout=120):
        if not self.session_id:
            return {"error": "No active ACP session"}

        pid = self._next_id()
        self._current_prompt_id = pid
        self.response_chunks[pid] = ""

        _t0 = time.perf_counter()
        result = self._send_request("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [
                {"type": "text", "text": text},
                {"type": "image", "data": image_b64, "mimeType": mime},
            ]
        }, timeout=timeout)

        response_text = self.response_chunks.pop(pid, "")
        self._current_prompt_id = None
        _ms = round((time.perf_counter() - _t0) * 1000.0, 1)

        if result and isinstance(result, dict):
            if "error" in result:
                _telemetry_emit("acp_vision", model=self.model or "default",
                                prompt_chars=len(text or ""), response_chars=0,
                                ms=_ms, stop_reason="error")
                return {"error": result["error"]}
            stop_reason = result.get("stopReason", "end_turn")
            _telemetry_emit("acp_vision", model=self.model or "default",
                            prompt_chars=len(text or ""), response_chars=len(response_text or ""),
                            ms=_ms, stop_reason=stop_reason)
            return {"text": response_text, "stop_reason": stop_reason}

        _telemetry_emit("acp_vision", model=self.model or "default",
                        prompt_chars=len(text or ""), response_chars=len(response_text or ""),
                        ms=_ms, stop_reason="end_turn")
        return {"text": response_text, "stop_reason": "end_turn"}


# ---------------------------------------------------------------------------
# Token cache helper
# ---------------------------------------------------------------------------


def _acp_model_key(model):
    """Normalize a model name into a pool key. Empty/None -> the CLI default."""
    return (model or "").strip() or "__default__"



def _acp_pool_touch(key):
    """Mark a pool key as most-recently-used."""
    try:
        _st.acp_pool_order.remove(key)
    except ValueError:
        pass
    _st.acp_pool_order.append(key)



def _acp_pool_register(client):
    """Register an externally-built client (e.g. the startup singleton or a
    reconfigured client) into the pool under its model key. Caller holds the lock."""
    if not client:
        return
    key = _acp_model_key(client.model)
    _st.acp_pool[key] = client
    _acp_pool_touch(key)



def _acp_pool_evict_if_needed(protect_key):
    """Evict least-recently-used warm clients past the cap. Never evicts the
    protected key or the client currently referenced by the _st.acp_client pointer.
    Caller holds the lock."""
    while len(_st.acp_pool) > _ACP_POOL_MAX:
        victim_key = None
        for k in list(_st.acp_pool_order):
            if k == protect_key:
                continue
            if _st.acp_client is not None and _st.acp_pool.get(k) is _st.acp_client:
                continue
            victim_key = k
            break
        if victim_key is None:
            break
        victim = _st.acp_pool.pop(victim_key, None)
        try:
            _st.acp_pool_order.remove(victim_key)
        except ValueError:
            pass
        if victim:
            print(f"[Bridge] Evicting warm ACP client: {victim_key}")
            _telemetry_emit("acp_pool", result="evict", model=victim_key, pool_size=len(_st.acp_pool))
            try:
                victim.stop()
            except Exception:
                pass



def _reset_acp_pool(keep_client):
    """Stop and clear all pooled clients except keep_client, then register
    keep_client. Used when MCP config changes so stale clients are not reused."""
    with _st.acp_pool_lock:
        for key, client in list(_st.acp_pool.items()):
            if client is keep_client:
                continue
            try:
                client.stop()
            except Exception:
                pass
        _st.acp_pool.clear()
        _st.acp_pool_order.clear()
        if keep_client:
            _acp_pool_register(keep_client)



def _ensure_acp_model(requested_model):
    """Ensure a warm ACP client for requested_model is selected as _st.acp_client.

    Uses a warm pool so switching between the cognition draft model and the
    reviewer model reuses a live Copilot CLI instead of respawning it every turn.
    Returns (ok, model_or_error)."""
    # global statement removed — writes go to _st.*

    with _st.acp_pool_lock:
        # Seed the pool with the startup singleton on first use.
        if _st.acp_client and _acp_model_key(_st.acp_client.model) not in _st.acp_pool:
            _acp_pool_register(_st.acp_client)

        if not _st.acp_client and not _st.acp_pool:
            return False, "ACP bridge not connected to Copilot"

        key = _acp_model_key(requested_model)

        # Fast path: a live warm client already exists for this model.
        existing = _st.acp_pool.get(key)
        if existing and existing.alive:
            _st.acp_client = existing
            _acp_pool_touch(key)
            _telemetry_emit("acp_pool", result="hit", model=key, pool_size=len(_st.acp_pool))
            return True, existing.model or "default"

        # Need to warm a new client. Use any live client as the cwd/path/MCP template.
        template = _st.acp_client
        if template is None or not template.alive:
            for c in _st.acp_pool.values():
                if c and c.alive:
                    template = c
                    break
        if template is None:
            # Nothing alive to template from; fall back to the existing pointer.
            template = _st.acp_client
        if template is None:
            return False, "ACP bridge not connected to Copilot"

        if requested_model:
            print(f"[Bridge] Warming ACP client for model: {requested_model}")
        else:
            print("[Bridge] Warming ACP client for default model")

        # Drop a dead client occupying this key before replacing it.
        if existing and not existing.alive:
            try:
                existing.stop()
            except Exception:
                pass
            _st.acp_pool.pop(key, None)
            try:
                _st.acp_pool_order.remove(key)
            except ValueError:
                pass

        try:
            new_client = ACPClient(
                copilot_path=template.copilot_path,
                cwd=template.cwd,
                model=(requested_model or None),
                mcp_config=_inject_kusto_token(template.mcp_config),
            )
            _warm_t0 = time.perf_counter()
            new_client.start()
        except RuntimeError as e:
            print(f"[Bridge] Warm client start failed: {e}")
            _telemetry_emit("acp_pool", result="warm_failed", model=key, error=str(e))
            return False, str(e)

        _st.acp_pool[key] = new_client
        _acp_pool_touch(key)
        _st.acp_client = new_client
        _acp_pool_evict_if_needed(key)
        _telemetry_emit("acp_pool", result="warm", model=key, pool_size=len(_st.acp_pool),
                        warm_ms=round((time.perf_counter() - _warm_t0) * 1000.0, 1))
        return True, new_client.model or "default"



