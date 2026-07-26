"""Bridge domain: acp_client."""

import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from bridge import config as _cfg
from bridge import state as _st
from bridge.kusto import _inject_kusto_token
from bridge.telemetry import _telemetry_emit

_ACP_POOL_MAX = _cfg.ACP_POOL_MAX
_ARTIFACTS_DIR = _cfg.ARTIFACTS_DIR

class ACPClient:
    """Manages the copilot --acp --stdio subprocess and ACP JSON-RPC protocol."""

    PROTOCOL_VERSION = 1  # ACP protocol major version

    def __init__(self, copilot_path="copilot", cwd=None, model=None, mcp_config=None, reasoning_effort=None):
        self.copilot_path = copilot_path
        self.cwd = cwd or os.getcwd()
        self.model = model  # None = use CLI default
        self.reasoning_effort = reasoning_effort  # None = use model default
        self.mcp_config = mcp_config or {}  # MCP servers config dict
        self.process = None
        self.request_id = 0
        self.lock = threading.Lock()
        self.prompt_lock = threading.Lock()
        self.pending = {}           # id -> {"event": Event, "result": None, "error": None}
        self.session_id = None
        self.response_chunks = {}   # prompt_id -> accumulated text
        self.reader_thread = None
        self.agent_info = {}
        self.alive = False
        self.active_requests = 0
        self.terminals = {}  # terminal_id -> {"process": Popen, "output": str}

    # --- Lifecycle ---

    def start(self):
        """Spawn copilot subprocess, initialize ACP, create session."""
        cmd = [self.copilot_path, "--acp", "--stdio", "--allow-all-tools"]
        if self.model:
            cmd.extend(["--model", self.model])
        if self.reasoning_effort:
            cmd.extend(["--reasoning-effort", self.reasoning_effort])
        # Pass MCP server config via --additional-mcp-config
        if self.mcp_config:
            mcp_json = json.dumps({"mcpServers": self.mcp_config})
            cmd.extend(["--additional-mcp-config", mcp_json])
        try:
            # Pass env vars from MCP config to the copilot process itself
            # (copilot spawns MCP servers as children, inheriting the env)
            os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
            process_env = os.environ.copy()
            process_env.pop("EVA_BRIDGE_TOKEN", None)
            for srv_name, srv_cfg in self.mcp_config.items():
                for k, v in srv_cfg.get('env', {}).items():
                    # subprocess.Popen env requires all values to be strings
                    process_env[k] = str(v) if not isinstance(v, str) else v
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
                "terminal": True
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
            self.process.stdin.write(msg.encode("utf-8"))
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            self.pending.pop(rid, None)
            return {"error": f"Copilot process pipe error: {e}"}

        event.wait(timeout=timeout)

        entry = self.pending.pop(rid, {})
        if entry.get("error"):
            return {"error": entry["error"]}
        return entry.get("result")

    def _send_response(self, rid, result):
        """Send a JSON-RPC response (for server-initiated requests like requestPermission)."""
        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "result": result
        }) + "\n"
        try:
            self.process.stdin.write(msg.encode("utf-8"))
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
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
            # Auto-grant permissions for chat usage
            print(f"[ACP] Permission requested: {json.dumps(msg.get('params', {}))}")
            self._send_response(msg["id"], {"outcome": {"outcome": "granted"}})
            return

        # Server-initiated requests for terminal
        if "id" in msg and msg.get("method") == "terminal/create":
            self._handle_terminal_create(msg["id"], msg.get("params", {}))
            return

        if "id" in msg and msg.get("method") == "terminal/output":
            self._handle_terminal_output(msg["id"], msg.get("params", {}))
            return

        if "id" in msg and msg.get("method") == "terminal/release":
            self._handle_terminal_release(msg["id"], msg.get("params", {}))
            return

        # Server-initiated requests for fs (decline)
        if "id" in msg and msg.get("method", "").startswith("fs/"):
            print(f"[ACP] Declining capability request: {msg.get('method')}")
            self._send_response(msg["id"], {
                "error": {"code": -32601, "message": "Method not supported by bridge"}
            })
            return

        # Unknown message
        if "id" in msg and "method" in msg:
            # Unknown server request — respond with error
            print(f"[ACP] Unknown server request: {msg.get('method')}")
            self._send_response(msg["id"], {
                "error": {"code": -32601, "message": "Not implemented"}
            })

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
        """Execute a shell command requested by the agent."""
        command = params.get("command", "")
        args = params.get("args", [])
        cwd = params.get("cwd") or self.cwd
        env_vars = params.get("env", [])

        # Build the full command
        full_cmd = command
        if args:
            full_cmd = command + " " + " ".join(args)

        print(f"[ACP Terminal] Creating terminal: {full_cmd[:100]}")

        # Build environment
        env = os.environ.copy()
        env.pop("EVA_BRIDGE_TOKEN", None)
        for ev in env_vars:
            if isinstance(ev, dict) and "name" in ev and "value" in ev:
                env[ev["name"]] = ev["value"]
        os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
        env["EVA_ARTIFACTS_DIR"] = _ARTIFACTS_DIR

        import uuid
        terminal_id = str(uuid.uuid4())

        try:
            proc = subprocess.Popen(
                full_cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
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
        # Wait a bit if still running
        if proc.poll() is None:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass

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
        with _pin_acp_client(self) as acquired:
            if not acquired:
                return {"error": "ACP client is unavailable"}
            with self.prompt_lock:
                return self._prompt(text, timeout)

    def _prompt(self, text, timeout=120):
        """Send a text prompt and return the accumulated response text."""
        if not self.session_id:
            return {"error": "No active ACP session"}

        pid = self._next_id()
        self._current_prompt_id = pid
        self.response_chunks[pid] = ""

        _t0 = time.perf_counter()
        result = self._send_request("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}]
        }, timeout=timeout)

        response_text = self.response_chunks.pop(pid, "")
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
        with _pin_acp_client(self) as acquired:
            if not acquired:
                return {"error": "ACP client is unavailable"}
            with self.prompt_lock:
                return self._prompt_with_image(text, image_b64, mime, timeout)

    def _prompt_with_image(self, text, image_b64, mime="image/jpeg", timeout=120):
        """Send a text + image prompt and return the accumulated response text.

        Uses the ACP content-block image type (the agent advertised
        promptCapabilities.image=true). image_b64 is base64 with no data: prefix.
        """
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


def _acp_model_key(model, reasoning_effort=None):
    """Normalize model and reasoning effort into a warm-client pool key."""
    model_key = (model or "").strip() or "__default__"
    effort_key = (reasoning_effort or "").strip() or "__default__"
    return f"{model_key}::{effort_key}"



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
    key = _acp_model_key(client.model, client.reasoning_effort)
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
            if getattr(_st.acp_pool.get(k), "active_requests", 0) > 0:
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


def _release_acp_client(client):
    """Release a request pin and trim any temporary pool overflow."""
    with _st.acp_pool_lock:
        client.active_requests = max(0, client.active_requests - 1)
        current_key = None
        if _st.acp_client is not None:
            current_key = _acp_model_key(_st.acp_client.model, _st.acp_client.reasoning_effort)
        _acp_pool_evict_if_needed(current_key)


@contextmanager
def _pin_acp_client(client):
    """Keep an existing client alive for one prompt lifecycle."""
    with _st.acp_pool_lock:
        acquired = bool(client and client.alive)
        if acquired:
            client.active_requests += 1
    try:
        yield acquired
    finally:
        if acquired:
            _release_acp_client(client)



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



def _ensure_acp_model(requested_model, reasoning_effort=None):
    """Ensure a warm ACP client for requested_model is selected as _st.acp_client.

    Uses a warm pool so switching between the cognition draft model and the
    reviewer model reuses a live Copilot CLI instead of respawning it every turn.
    Returns (ok, model_or_error)."""
    # global statement removed — writes go to _st.*

    with _st.acp_pool_lock:
        # Seed the pool with the startup singleton on first use.
        if _st.acp_client and _acp_model_key(_st.acp_client.model, _st.acp_client.reasoning_effort) not in _st.acp_pool:
            _acp_pool_register(_st.acp_client)

        if not _st.acp_client and not _st.acp_pool:
            return False, "ACP bridge not connected to Copilot"

        key = _acp_model_key(requested_model, reasoning_effort)

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
                reasoning_effort=reasoning_effort,
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


@contextmanager
def _acquire_acp_client(requested_model, reasoning_effort=None):
    """Atomically select and pin a model/effort client from the warm pool."""
    selected_client = None
    detail = "ACP bridge not connected to Copilot"
    with _st.acp_pool_lock:
        switched, detail = _ensure_acp_model(requested_model, reasoning_effort)
        if switched:
            key = _acp_model_key(requested_model, reasoning_effort)
            candidate = _st.acp_pool.get(key)
            if candidate and candidate.alive:
                candidate.active_requests += 1
                selected_client = candidate
            else:
                detail = "Selected ACP client is unavailable"
    try:
        yield selected_client, detail
    finally:
        if selected_client:
            _release_acp_client(selected_client)



