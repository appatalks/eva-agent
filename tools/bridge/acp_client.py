"""Bridge domain: acp_client."""

import json
import os
import re
import subprocess
import sys
import threading
import time
from bridge import config as _cfg
from bridge import state as _st
from bridge.kusto import _inject_kusto_token
from bridge.telemetry import _telemetry_emit

_ACP_POOL_MAX = _cfg.ACP_POOL_MAX
_ARTIFACTS_DIR = _cfg.ARTIFACTS_DIR

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

    # --- Lifecycle ---

    def start(self):
        """Spawn copilot subprocess, initialize ACP, create session."""
        cmd = [self.copilot_path, "--acp", "--stdio"]
        if self.model:
            cmd.extend(["--model", self.model])
        # Pass MCP server config via --additional-mcp-config
        if self.mcp_config:
            # Secrets are inherited through the scrubbed child environment, not
            # serialized into argv where process listings can expose them.
            cli_config = {}
            for name, raw in self.mcp_config.items():
                cfg = dict(raw) if isinstance(raw, dict) else {}
                cfg.pop("env", None)
                cli_config[name] = cfg
            mcp_json = json.dumps({"mcpServers": cli_config})
            cmd.extend(["--additional-mcp-config", mcp_json])
        try:
            # Pass env vars from MCP config to the copilot process itself
            # (copilot spawns MCP servers as children, inheriting the env)
            os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
            explicit_env = {}
            for srv_name, srv_cfg in self.mcp_config.items():
                for k, v in srv_cfg.get('env', {}).items():
                    if k == "EVA_BRIDGE_TOKEN":
                        continue
                    # subprocess.Popen env requires all values to be strings
                    explicit_env[k] = str(v) if not isinstance(v, str) else v
            process_env = _cfg.child_process_env(explicit_env)
            process_env["EVA_ARTIFACTS_DIR"] = _ARTIFACTS_DIR

            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=process_env
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"Copilot CLI not found at '{self.copilot_path}'. "
                "Install it (https://github.com/github/copilot-cli) and authenticate with 'copilot auth login'."
            )

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
            print(f"[ACP] Warning: initialize returned: {init_result}")

        # Create session — pass MCP servers via ACP session/new if configured
        mcp_servers_for_session = []
        # Note: MCP servers are typically passed via CLI --additional-mcp-config
        # but we also pass them in session/new for full ACP compliance
        session_result = self._send_request("session/new", {
            "cwd": self.cwd,
            "mcpServers": mcp_servers_for_session
        }, timeout=30)

        if session_result and "sessionId" in session_result:
            self.session_id = session_result["sessionId"]
            print(f"[ACP] Session created: {self.session_id}")
        else:
            print(f"[ACP] Warning: session/new returned: {session_result}")

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
                    print(f"[ACP] Non-JSON line: {line[:200]}")
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
                print(f"[Copilot stderr] {line.decode('utf-8', errors='replace').rstrip()}")
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
        """Execute a command requested by the agent (shell=False, cwd-constrained)."""
        command = params.get("command", "")
        args = params.get("args", [])
        cwd = params.get("cwd") or self.cwd
        env_vars = params.get("env", [])

        # Validate command and args are strings
        if not isinstance(command, str) or not command.strip():
            self._send_response(rid, {"error": {"code": -32602, "message": "command must be a non-empty string"}})
            return
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            self._send_response(rid, {"error": {"code": -32602, "message": "args must be a list of strings"}})
            return

        # Constrain cwd under the configured ACP cwd
        if not isinstance(cwd, str):
            cwd = self.cwd
        real_cwd = os.path.realpath(cwd)
        allowed_base = os.path.realpath(self.cwd)
        if not (real_cwd == allowed_base or real_cwd.startswith(allowed_base + os.sep)):
            print(f"[ACP Terminal] DENIED: cwd {cwd!r} outside allowed base {self.cwd!r}")
            self._send_response(rid, {"error": {"code": -32602, "message": "cwd outside allowed base directory"}})
            return

        argv = [command] + args
        print(f"[ACP Terminal] Creating executable={os.path.basename(command)!r} argc={len(args)}")

        # Build environment
        explicit_env = {}
        for ev in env_vars:
            if isinstance(ev, dict) and "name" in ev and "value" in ev:
                name = str(ev["name"])
                if name != "EVA_BRIDGE_TOKEN":
                    explicit_env[name] = str(ev["value"])
        env = _cfg.child_process_env(explicit_env)
        os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
        env["EVA_ARTIFACTS_DIR"] = _ARTIFACTS_DIR

        import uuid
        terminal_id = str(uuid.uuid4())

        try:
            proc = subprocess.Popen(
                argv,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=real_cwd,
                env=env
            )
            self.terminals[terminal_id] = {"process": proc, "output": ""}

            # Read output in background
            def read_output():
                try:
                    out, _ = proc.communicate(timeout=60)
                    self.terminals[terminal_id]["output"] = out.decode("utf-8", errors="replace")
                    self.terminals[terminal_id]["exit_code"] = proc.returncode
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, _ = proc.communicate()
                    self.terminals[terminal_id]["output"] = out.decode("utf-8", errors="replace") + "\n[TIMEOUT]"
                    self.terminals[terminal_id]["exit_code"] = -1

            t = threading.Thread(target=read_output, daemon=True)
            t.start()

            self._send_response(rid, {"terminalId": terminal_id})
            print(f"[ACP Terminal] Started: {terminal_id}")

        except Exception as e:
            print(f"[ACP Terminal] Error: {e}")
            self._send_response(rid, {"error": {"code": -32000, "message": str(e)}})

    def _handle_terminal_output(self, rid, params):
        """Return terminal output and exit status."""
        terminal_id = params.get("terminalId", "")
        term = self.terminals.get(terminal_id)

        if not term:
            self._send_response(rid, {"error": {"code": -32000, "message": "Unknown terminal"}})
            return

        proc = term["process"]
        # Never block the sole ACP reader; the output worker updates the record.
        output = term.get("output", "")
        exit_code = term.get("exit_code", proc.returncode)

        print(f"[ACP Terminal] Output ({terminal_id[:8]}): exit={exit_code}, len={len(output)}")

        self._send_response(rid, {
            "output": output,
            "exitCode": exit_code if exit_code is not None else -1,
            "isRunning": proc.poll() is None
        })

    def _handle_terminal_release(self, rid, params):
        """Release a terminal."""
        terminal_id = params.get("terminalId", "")
        term = self.terminals.pop(terminal_id, None)
        if term and term["process"].poll() is None:
            term["process"].kill()
        print(f"[ACP Terminal] Released: {terminal_id[:8] if terminal_id else '?'}")
        self._send_response(rid, {})

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



