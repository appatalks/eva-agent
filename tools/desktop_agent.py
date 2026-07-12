"""Vision-assisted, launch-only desktop agent for Eva.

A contained loop: screenshot -> multimodal model -> allowlisted GUI launch ->
fresh process verification. Pointer, keyboard, shell, arguments, arbitrary
window helpers, and arbitrary file opening are structurally unavailable until
the capability broker is implemented.

Two roles:
  - Director (text only): high level planner, wired by the bridge to Claude via
    ACP. Sees a text state summary, sets the current subgoal.
    - Executor (vision): looks at the screenshot and may request one allowlisted
        GUI launch, wait, ask for input, or request terminal verification.

pyautogui (and PIL) are imported lazily so a missing install never breaks bridge
import. It is used only for private screenshots in this release.

SAFETY: Electron authorizes the complete launch spec, the exact launch receives
a separate one-use approval, and success requires no prior run-scoped spawn
receipt followed by the same live canonical process receipt.
"""

import os
import re
import json
import time
import base64
import shutil
import stat
import subprocess
import threading
import uuid
from datetime import datetime, timezone

from bridge import config as _bridge_config
from bridge.action_runs import (
    ActionRunValidationError,
    ActionRunCancelled,
    ActionRunTimeout,
    admit_action_run,
    begin_effect,
    bounded_text,
    cancel_run,
    effectful_action,
    finite_int,
    finish_effect,
    initialize_run,
    launch_spec,
    observation,
    open_gate,
    public_snapshot,
    resolve_gate,
    runtime_expired,
    run_bounded_call,
    set_postcondition_baseline,
    sha256,
    strict_json_object,
    terminalize,
    typed_action_result,
    unknown_postcondition,
    update_run,
)

_TRAJ_DIR = os.path.expanduser("~/.config/eva-standalone/desktop_trajectories")
_MAX_STEPS_DEFAULT = 25
_DIRECTOR_INTERVAL = 4  # re-consult the director every N executor steps
_ARTIFACT_TTL_SECONDS = 600

_ACTION_KINDS = {
    "launch_app", "wait", "done", "ask",
}

# run_id -> run record. Guarded by _runs_lock.
_runs = {}
_runs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def pyautogui_available():
    """Return (ok, detail). Lazy import so the bridge never fails to load."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False, "no display server (DISPLAY/WAYLAND_DISPLAY unset)"
    try:
        import pyautogui  # noqa: F401
    except Exception:
        return False, "pyautogui is not installed or could not be loaded"
    return True, "launch-only desktop containment available"


def _get_pyautogui():
    import pyautogui
    pyautogui.FAILSAFE = True       # mouse to a corner aborts (emergency stop)
    pyautogui.PAUSE = 0.15          # small settle delay between calls
    return pyautogui


# ---------------------------------------------------------------------------
# Run registry
# ---------------------------------------------------------------------------

def _new_run(goal, autonomy="pause", postcondition=None):
    _scavenge_artifacts()
    run_id = uuid.uuid4().hex[:16]
    rec = {
        "id": run_id,
        "goal": goal,
        "status": "starting",        # starting|running|awaiting_confirmation|awaiting_input|done|cancelled|error
        "step": 0,
        "active_app": "",
        "subgoal": "",
        "result": None,
        "error": None,
        "pending_action": None,
        "pending_question": None,
        "last_screenshot": None,
        "screen": "",
        "started": datetime.now(timezone.utc).isoformat(),
        "finished": None,
        "steps": [],
        "capabilities": ["launch_app"],
        "_cancel": threading.Event(),
        "_gate": threading.Event(),
        "_decision": None,
        "_thread": None,
        "_process_receipts": [],
    }
    initialize_run(rec, "desktop", autonomy, postcondition)
    with _runs_lock:
        _runs[run_id] = rec
    return rec


def latest_screenshot_path(run_id):
    with _runs_lock:
        rec = _runs.get(run_id)
        shot = rec.get("last_screenshot") if rec else None
    if shot and os.path.isfile(shot):
        return shot
    return None


def public_status(run_id):
    with _runs_lock:
        rec = _runs.get(run_id)
        if not rec:
            return None
    return public_snapshot(rec, (
        "id", "goal", "status", "step", "active_app", "subgoal",
        "result", "error", "pending_question", "screen", "started", "finished",
        "steps", "capabilities",
    ))


def cancel(run_id):
    with _runs_lock:
        rec = _runs.get(run_id)
    if not rec:
        return False, "unknown_run"
    return cancel_run(rec)


def resolve(run_id, *, gate_id, kind, decision=None, text=None):
    with _runs_lock:
        rec = _runs.get(run_id)
    if not rec:
        return False, "unknown_run"
    return resolve_gate(
        rec, gate_id=gate_id, kind=kind, decision=decision, text=text
    )


# ---------------------------------------------------------------------------
# Vision executor (OpenAI multimodal)
# ---------------------------------------------------------------------------

def _executor_system(w, h):
    return (
        "You are the executor for a desktop automation agent. You see a screenshot "
        f"of the user's entire screen, {w}x{h} pixels, origin top-left. Decide the "
        "SINGLE next action to make progress on the current subgoal. Reply with ONE "
        "JSON object and nothing else.\n\n"
        "Schema (pick one action):\n"
        '  {"action":"launch_app","app":"<allowlisted GUI name, e.g. gimp>","args":[],"reason":"..."}\n'
        '  {"action":"wait","ms":<int>,"reason":"..."}\n'
        '  {"action":"ask","question":"<what you need from the user>"}\n'
        '  {"action":"done","summary":"<what was accomplished>"}\n\n'
        "Rules: put the real intent in reason so it can be reviewed. To start an "
        "application, use launch_app with an allowlisted GUI name and an empty args "
        "list. Pointer, keyboard, shell, arguments, and window control are unavailable. "
        "Emit done only when the launch goal is fully "
        "achieved, and ask when blocked or needing info only the user has. "
        "Keyboard entry and shortcuts are unavailable until the capability broker "
        "can authorize their command semantics.\n"
        "Never output prose outside the JSON."
    )


def _b64_png(data):
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _call_executor(
    api_key, model, goal, subgoal, history, active_app, png_bytes, w, h,
):
    import requests as _req

    hist_lines = []
    for entry in history[-8:]:
        a = entry.get("action", {})
        hist_lines.append(f"step {entry.get('step')}: {json.dumps(a)} -> {entry.get('result','')}")
    history_text = "\n".join(hist_lines) if hist_lines else "(none yet)"

    user_text = (
        f"GOAL: {goal}\n"
        f"CURRENT SUBGOAL: {subgoal or goal}\n"
        f"ACTIVE APP (best guess): {active_app or 'unknown'}\n"
        f"SCREEN: {w}x{h}\n"
        f"RECENT ACTIONS:\n{history_text}\n\n"
        "Return the next action JSON."
    )

    payload = {
        "model": model,
        "max_tokens": 400,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _executor_system(w, h)},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": _b64_png(png_bytes)}},
            ]},
        ],
    }
    resp = _req.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"vision model {resp.status_code}: {resp.text[:200]}")
    raw = resp.json()["choices"][0]["message"]["content"] or ""
    return _parse_action(raw), raw


def _parse_action(raw):
    text = (raw or "").strip()
    if not text.startswith("{") or not text.endswith("}"):
        raise ActionRunValidationError("model returned no desktop action JSON")
    action = strict_json_object(text)
    kind = action.get("action")
    schemas = {
        "launch_app": (
            {"action", "app", "args", "reason"}, {"action", "app", "args"}
        ),
        "click": ({"action", "x", "y", "reason"}, {"action", "x", "y"}),
        "double_click": ({"action", "x", "y", "reason"}, {"action", "x", "y"}),
        "right_click": ({"action", "x", "y", "reason"}, {"action", "x", "y"}),
        "move": ({"action", "x", "y", "reason"}, {"action", "x", "y"}),
        "scroll": ({"action", "dy", "reason"}, {"action", "dy"}),
        "wait": ({"action", "ms", "reason"}, {"action"}),
        "done": ({"action", "summary"}, {"action"}),
        "ask": ({"action", "question"}, {"action", "question"}),
    }
    if kind not in _ACTION_KINDS or kind not in schemas:
        raise ActionRunValidationError("model returned an unsupported desktop action")
    allowed, required = schemas[kind]
    if set(action) - allowed or not required.issubset(action):
        raise ActionRunValidationError("model desktop action fields are invalid")
    for field in ("reason", "app", "summary", "question"):
        if field in action and not isinstance(action[field], str):
            raise ActionRunValidationError(f"model desktop action {field} must be text")
    for field in ("x", "y", "dy", "ms"):
        if field in action and (
            isinstance(action[field], bool) or not isinstance(action[field], int)
        ):
            raise ActionRunValidationError(f"model desktop action {field} must be an integer")
    if kind == "launch_app" and (
        not isinstance(action.get("args"), list) or action["args"]
    ):
        raise ActionRunValidationError("desktop application arguments must be an empty list")
    return action


# ---------------------------------------------------------------------------
# Trajectory logging
# ---------------------------------------------------------------------------

def _run_dir(run_id):
    d = os.path.join(_TRAJ_DIR, run_id)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _scavenge_artifacts(remove_all=False):
    cutoff = datetime.now(timezone.utc).timestamp() - _ARTIFACT_TTL_SECONDS
    try:
        entries = os.scandir(_TRAJ_DIR)
    except OSError:
        return
    with entries:
        for entry in entries:
            try:
                if (
                    entry.is_dir(follow_symlinks=False)
                    and re.fullmatch(r"[0-9a-f]{16}", entry.name)
                    and (
                        remove_all
                        or entry.stat(follow_symlinks=False).st_mtime < cutoff
                    )
                ):
                    shutil.rmtree(entry.path, ignore_errors=True)
            except OSError:
                continue


_scavenge_artifacts(remove_all=True)


def _log_step(run_id, record):
    try:
        path = os.path.join(_run_dir(run_id), "trajectory.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        print("[DesktopAgent] trajectory write failed")


def _schedule_artifact_cleanup(rec):
    def cleanup():
        shutil.rmtree(os.path.join(_TRAJ_DIR, rec["id"]), ignore_errors=True)
        with rec["_record_lock"]:
            rec["last_screenshot"] = None

    timer = threading.Timer(_ARTIFACT_TTL_SECONDS, cleanup)
    timer.daemon = True
    timer.start()


def _record(rec, step, shot_path, subgoal, raw, action, result):
    def private_hash(value):
        return sha256({"salt": rec["_log_salt"], "value": value})
    try:
        with open(shot_path, "rb") as handle:
            screenshot_hash = sha256(handle.read())
    except (OSError, TypeError):
        screenshot_hash = ""
    action_view = {
        "kind": action.get("action", "unknown"),
        "digest": private_hash(action),
    }
    entry = {
        "contract_version": "eva.action-step/1",
        "step": step,
        "ts": datetime.now(timezone.utc).isoformat(),
        "active_app_hash": private_hash(rec["active_app"] or ""),
        "goal_hash": private_hash(rec["goal"]),
        "subgoal_hash": private_hash(subgoal or ""),
        "model_output_hash": private_hash(raw or ""),
        "action": action_view,
        "result": result,
        "screenshot_hash": screenshot_hash,
    }
    with rec["_record_lock"]:
        rec["steps"].append({"step": step, "action": action_view, "result": result})
    _log_step(rec["id"], entry)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _park_approval(rec, action, binding=None):
    return open_gate(rec, "approval", action=action, binding=binding)


def _park_input(rec, question):
    return open_gate(rec, "input", question=question)


def _desktop_action_binding(rec, action):
    kind = action.get("action")
    binding = {
        "kind": kind,
        "screen": rec.get("screen", ""),
        "target_valid": True,
    }
    for field in ("app",):
        if field in action:
            binding[field] = action[field]
    return binding


def _fresh_desktop_binding(gui, rec, action):
    return _desktop_action_binding(rec, action)


# Common friendly names that vision models reach for, mapped to the candidate
# binaries that actually exist across desktops. The first candidate found on
# PATH wins, so this works regardless of which desktop environment is installed.
_APP_ALIASES = {
    "calculator": ["gnome-calculator", "kcalc", "qalculate-gtk", "galculator", "mate-calc", "xcalc"],
    "calc": ["gnome-calculator", "kcalc", "qalculate-gtk", "galculator", "mate-calc", "xcalc"],
    "files": ["nautilus", "dolphin", "nemo", "thunar", "pcmanfm", "caja"],
    "file manager": ["nautilus", "dolphin", "nemo", "thunar", "pcmanfm", "caja"],
    "filemanager": ["nautilus", "dolphin", "nemo", "thunar", "pcmanfm", "caja"],
    "text editor": ["gedit", "kate", "gnome-text-editor", "mousepad", "xed"],
    "editor": ["gedit", "kate", "gnome-text-editor", "mousepad", "xed"],
    "browser": ["firefox", "google-chrome", "chromium", "chromium-browser", "brave-browser"],
    "web browser": ["firefox", "google-chrome", "chromium", "chromium-browser", "brave-browser"],
    "screenshot": ["gnome-screenshot", "spectacle", "flameshot", "scrot"],
    "image editor": ["gimp", "krita", "pinta"],
    "paint": ["gimp", "krita", "pinta", "kolourpaint"],
}
_ALLOWED_GUI_BINARIES = frozenset(
    candidate for candidates in _APP_ALIASES.values() for candidate in candidates
)
_NATIVE_MAGICS = (
    b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
)


def _trusted_native_path(found, expected_basename):
    if not found or os.path.basename(found) != expected_basename or os.path.islink(found):
        return None
    real = os.path.realpath(found)
    trusted_roots = ("/usr/bin/", "/usr/lib/", "/usr/libexec/", "/opt/")
    if not real.startswith(trusted_roots):
        return None
    try:
        info = os.stat(real)
        forbidden = stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & forbidden
            or not info.st_mode & stat.S_IXUSR
        ):
            return None
        parent = os.path.dirname(real)
        while parent and parent != "/":
            parent_info = os.stat(parent)
            if (
                parent_info.st_uid != 0
                or parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                return None
            parent = os.path.dirname(parent)
        with open(real, "rb") as handle:
            magic = handle.read(4)
        if magic not in _NATIVE_MAGICS:
            return None
    except OSError:
        return None
    return real


def _resolve_app_binary(app):
    """Resolve a friendly or exact app name to a real binary on PATH.

    Vision models reach for generic names ("calculator") that are rarely the
    actual binary ("gnome-calculator"). Try the literal name first, then a
    curated alias table, then a couple of common naming variants. Returns the
    absolute binary path or None.
    """
    key = app.strip().lower()
    candidates = [key] if key in _ALLOWED_GUI_BINARIES else _APP_ALIASES.get(key, [])
    for cand in candidates:
        trusted = _trusted_native_path(shutil.which(cand), cand)
        if trusted:
            return trusted
    return None


def _process_start_ticks(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            raw = handle.read(4096)
        close = raw.rfind(")")
        fields = raw[close + 2:].split() if close >= 0 else []
        ticks = int(fields[19])
        return ticks if ticks >= 0 else None
    except (OSError, ValueError, IndexError):
        return None


def _launch_app(action):
    app = str(action.get("app", "")).strip()
    if not app or not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", app):
        return typed_action_result("rejected", "invalid_app", "Invalid application name.")
    binary = _resolve_app_binary(app)
    if not binary:
        return typed_action_result(
            "rejected", "app_not_allowlisted",
            "Application is not installed or is not in the GUI allowlist.",
        )
    args = action.get("args", [])
    if not isinstance(args, list) or args:
        return typed_action_result(
            "rejected", "app_arguments_forbidden",
            "Model-supplied application arguments are disabled.",
        )
    cmd = [binary]
    try:
        # No shell; arguments passed as a list so nothing is interpreted.
        process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_bridge_config.child_process_env(profile="gui"),
        )
        start_ticks = _process_start_ticks(process.pid)
        try:
            live_binary = os.path.realpath(f"/proc/{process.pid}/exe")
        except OSError:
            live_binary = ""
        if process.poll() is not None or start_ticks is None or live_binary != binary:
            try:
                if process.poll() is None:
                    process.terminate()
            except Exception:
                pass
            return typed_action_result(
                "failed", "app_identity_unavailable",
                "The launched application identity could not be attested.",
            )
        result = typed_action_result(
            "executed", "app_started", f"Started {os.path.basename(binary)}."
        )
        result["_launch_receipt"] = {
            "requested_app": app.lower(),
            "binary": binary,
            "pid": int(process.pid),
            "started_monotonic": time.monotonic(),
            "process_start_ticks": start_ticks,
            "process_handle": process,
        }
        return result
    except Exception:
        return typed_action_result("failed", "app_launch_failed", "Application launch failed.")


def _execute(gui, action, rec):
    kind = action["action"]
    if kind == "launch_app":
        result = _launch_app(action)
        if result["state"] == "executed":
            receipt = result.pop("_launch_receipt", None)
            app = str(action.get("app", "")).strip()
            with rec["_record_lock"]:
                rec["active_app"] = app or rec["active_app"]
                if receipt:
                    rec["_process_receipts"].append(receipt)
            time.sleep(1.5)  # give the window time to appear
        return result
    if kind == "wait":
        ms = finite_int(action.get("ms", 500), "ms", 0, 5000)
        time.sleep(ms / 1000.0)
        return typed_action_result("executed", "waited", "Waited for the desktop to settle.")
    return typed_action_result("rejected", "unsupported_action", "Unsupported desktop action.")


def _verify_postcondition(rec, step):
    spec = rec.get("_postcondition")
    if not spec:
        return unknown_postcondition()
    try:
        executable = spec["executable"].lower()
        with rec["_record_lock"]:
            receipts = list(rec.get("_process_receipts", []))
        matched = False
        verified_pid = 0
        for receipt in receipts:
            pid = receipt.get("pid")
            binary = receipt.get("binary")
            process = receipt.get("process_handle")
            start_ticks = receipt.get("process_start_ticks")
            started_monotonic = receipt.get("started_monotonic")
            if (
                not isinstance(binary, str)
                or os.path.basename(binary).lower() != executable
                or isinstance(start_ticks, bool)
                or not isinstance(start_ticks, int)
                or start_ticks < 0
                or not isinstance(started_monotonic, (int, float))
                or isinstance(started_monotonic, bool)
                or not rec.get("_started_monotonic", 0) <= started_monotonic <= time.monotonic()
            ):
                continue
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                continue
            try:
                if (
                    process is None or getattr(process, "pid", None) != pid
                    or process.poll() is not None
                    or _process_start_ticks(pid) != start_ticks
                ):
                    continue
                live_binary = os.path.realpath(f"/proc/{pid}/exe")
            except (OSError, AttributeError):
                continue
            if live_binary == binary:
                matched = True
                verified_pid = pid
                break
        facts = {
            "executable": executable,
            "started": matched,
            "pid": verified_pid,
        }
        check = observation(
            "desktop-process", "desktop.process_spawned",
            "observed" if matched else "not_observed", facts, step,
        )
        return {
            "verdict": check["verdict"],
            "spec_source": "request",
            "verified_by": "tool",
            "spec_hash": sha256(spec),
            "checks": [check],
        }
    except Exception:
        return unknown_postcondition(spec)


def _finish_with_postcondition(rec, cause, model_summary=""):
    postcondition = _verify_postcondition(rec, rec.get("step", 0))
    verdict = postcondition["verdict"]
    if cause in ("step_limit", "timeout"):
        terminalize(
            rec, "aborted", "budget_exhausted" if cause == "step_limit" else "timed_out",
            cause, result="The desktop run stopped before completion could be verified.",
            model_summary=model_summary, postcondition=postcondition,
        )
    elif verdict == "observed":
        terminalize(
            rec, "succeeded", "postcondition_observed", cause,
            result="Verified the requested desktop postcondition.",
            model_summary=model_summary, postcondition=postcondition,
        )
    elif verdict == "not_observed":
        terminalize(
            rec, "failed", "postcondition_not_observed", cause,
            error="The requested desktop postcondition was not observed.",
            model_summary=model_summary, postcondition=postcondition,
        )
    else:
        terminalize(
            rec, "indeterminate", "unverified_completion_claim", cause,
            result=(
                "The desktop agent stopped after claiming completion, but the "
                "result was not independently verified."
            ),
            model_summary=model_summary, postcondition=postcondition,
        )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _worker(rec, api_key, vision_model, director, max_steps):
    run_id = rec["id"]
    history = rec["steps"]
    subgoal = ""
    try:
        gui = _get_pyautogui()
        w, h = gui.size()
        rec["_screen_size"] = (int(w), int(h))
        update_run(
            rec, screen=f"{w}x{h}", status="running", capabilities=["launch_app"]
        )
        set_postcondition_baseline(rec, _verify_postcondition(rec, 0))

        if director:
            try:
                subgoal = run_bounded_call(
                    rec,
                    lambda: director(
                        rec["goal"], f"Desktop is {w}x{h}. Nothing launched yet."
                    ),
                    timeout_seconds=30,
                ) or ""
            except (ActionRunCancelled, ActionRunTimeout):
                raise
            except Exception:
                print("[DesktopAgent] director request failed")
        update_run(rec, subgoal=subgoal)

        step = 0
        while step < max_steps:
            if rec["_cancel"].is_set():
                terminalize(rec, "aborted", "user_cancelled", "cancel")
                break
            if runtime_expired(rec):
                _finish_with_postcondition(rec, "timeout")
                break
            if runtime_expired(rec):
                _finish_with_postcondition(rec, "timeout")
                break

            try:
                # Pass an explicit path: pyautogui's Linux backend (scrot) writes
                # its intermediate file to the CURRENT WORKING DIRECTORY when no
                # filename is given, which fails when cwd is read-only (e.g. an
                # AppImage mount). Writing straight to the run dir avoids that.
                shot_path = os.path.join(_run_dir(run_id), f"step_{step:02d}.png")
                def capture():
                    gui.screenshot(shot_path)
                    os.chmod(shot_path, 0o600)
                    with open(shot_path, "rb") as handle:
                        return handle.read()

                png = run_bounded_call(rec, capture, timeout_seconds=10)
            except (ActionRunCancelled, ActionRunTimeout):
                raise
            except Exception:
                terminalize(
                    rec, "failed", "screenshot_failed", "tool_error",
                    error="Desktop screenshot capture failed.",
                )
                break
            update_run(rec, last_screenshot=shot_path)

            try:
                action, raw = run_bounded_call(
                    rec,
                    lambda: _call_executor(
                        api_key, vision_model, rec["goal"], subgoal,
                        history, rec["active_app"], png, w, h,
                    ),
                    timeout_seconds=65,
                )
            except (ActionRunCancelled, ActionRunTimeout):
                raise
            except Exception:
                terminalize(
                    rec, "failed", "executor_call_failed", "model_error",
                    error="The desktop vision executor request failed.",
                )
                break

            if rec["_cancel"].is_set():
                terminalize(rec, "aborted", "user_cancelled", "cancel")
                break

            kind = action.get("action")

            if kind == "done":
                update_run(rec, step=step)
                claim = bounded_text(action.get("summary", ""), 300)
                _record(
                    rec, step, shot_path, subgoal, raw, action,
                    typed_action_result(
                        "skipped", "model_completion_claim",
                        "Model requested terminal verification.",
                    ),
                )
                _finish_with_postcondition(rec, "model_done", claim)
                break

            if kind == "ask":
                decision = _park_input(rec, action.get("question", "Need input."))
                if rec["_cancel"].is_set():
                    terminalize(rec, "aborted", "user_cancelled", "cancel")
                    break
                if runtime_expired(rec):
                    _finish_with_postcondition(rec, "timeout")
                    break
                if decision.get("state") != "answered":
                    reason = (
                        "user_cancelled" if decision.get("state") == "cancelled"
                        else "approval_expired"
                    )
                    terminalize(
                        rec, "aborted", reason, "input_gate",
                        result="The desktop run stopped because required input was not received.",
                    )
                    break
                answer = decision["text"]
                subgoal = (subgoal + f"\nUser said: {answer}").strip()
                update_run(rec, subgoal=subgoal)
                _record(
                    rec, step, shot_path, subgoal, raw, action,
                    typed_action_result("executed", "input_received", "Received bounded user input."),
                )
                step += 1
                continue

            execution_action = action
            effect_lease = False
            if effectful_action(action):
                binding = _desktop_action_binding(rec, action)
                if not binding.get("target_valid", False):
                    terminalize(
                        rec, "failed", "target_not_attestable", "target_binding",
                        error="The desktop target could not be bound safely.",
                    )
                    break
                decision = _park_approval(rec, action, binding)
                if rec["_cancel"].is_set():
                    terminalize(rec, "aborted", "user_cancelled", "cancel")
                    break
                if runtime_expired(rec):
                    _finish_with_postcondition(rec, "timeout")
                    break
                if decision.get("state") != "approved":
                    reason = (
                        "user_denied" if decision.get("state") == "denied"
                        else "approval_expired"
                    )
                    _record(
                        rec, step, shot_path, subgoal, raw, action,
                        typed_action_result("rejected", reason, "Action was not approved."),
                    )
                    terminalize(
                        rec, "aborted", reason, "approval_gate",
                        result="The desktop run stopped before the action was executed.",
                    )
                    break
                current_binding = _fresh_desktop_binding(
                    gui, rec, decision.get("action") or action
                )
                if runtime_expired(rec):
                    _finish_with_postcondition(rec, "timeout")
                    break
                leased, lease_reason, execution_action = begin_effect(
                    rec, decision, current_binding
                )
                if not leased:
                    terminalize(
                        rec, "aborted", lease_reason, "execution_lease",
                        result="The approved desktop target changed before execution.",
                    )
                    break
                effect_lease = True
            try:
                result = _execute(gui, execution_action, rec)
            except Exception:
                result = typed_action_result(
                    "failed", "action_exception", "The desktop action failed."
                )
            _record(rec, step, shot_path, subgoal, raw, execution_action, result)
            cancellation_pending = False
            if effect_lease:
                _finished, cancellation_pending = finish_effect(rec, result)
            if cancellation_pending:
                terminalize(rec, "aborted", "user_cancelled", "cancel")
                break
            if result["state"] in ("failed", "rejected"):
                terminalize(
                    rec, "failed", result["code"], "action_execution",
                    error=result["summary"],
                )
                break

            # Loop guard: if the same action keeps producing the same result
            # (e.g. a launch that errors, or a click that changes nothing), the
            # executor is stuck. After a few identical repeats, stop and ask the
            # user rather than burning the whole step budget in a tight loop.
            sig = json.dumps(execution_action, sort_keys=True) + "|" + str(result)
            if sig == rec.get("_last_sig"):
                rec["_repeat"] = rec.get("_repeat", 0) + 1
            else:
                rec["_repeat"] = 0
                rec["_last_sig"] = sig
            if rec["_repeat"] >= 2:
                q = "I'm repeating the same step without progress"
                if result["state"] == "failed":
                    q += " (" + result["summary"] + ")"
                q += ". How would you like me to proceed, or should I stop?"
                decision = _park_input(rec, q)
                if rec["_cancel"].is_set():
                    terminalize(rec, "aborted", "user_cancelled", "cancel")
                    break
                if decision.get("state") != "answered":
                    reason = (
                        "user_cancelled" if decision.get("state") == "cancelled"
                        else "approval_expired"
                    )
                    terminalize(rec, "aborted", reason, "input_gate")
                    break
                answer = decision["text"]
                rec["_repeat"] = 0
                rec["_last_sig"] = None
                subgoal = (subgoal + f"\nUser said: {answer}").strip()
                update_run(rec, subgoal=subgoal)

            time.sleep(0.4)
            step += 1
            update_run(rec, step=step)

            if director and step % _DIRECTOR_INTERVAL == 0:
                try:
                    summary = f"Active app: {rec['active_app'] or 'unknown'}. Last action: {json.dumps(action)} -> {result}."
                    new_sub = run_bounded_call(
                        rec, lambda: director(rec["goal"], summary),
                        timeout_seconds=30,
                    )
                    if new_sub:
                        subgoal = new_sub
                        update_run(rec, subgoal=subgoal)
                except (ActionRunCancelled, ActionRunTimeout):
                    raise
                except Exception:
                    print("[DesktopAgent] director request failed")
        else:
            if not rec.get("_terminalized"):
                update_run(rec, step=max_steps)
                _finish_with_postcondition(rec, "step_limit")
    except ActionRunCancelled:
        terminalize(rec, "aborted", "user_cancelled", "cancel")
    except ActionRunTimeout:
        _finish_with_postcondition(rec, "timeout")
    except Exception:
        terminalize(
            rec, "failed", "desktop_runtime_error", "runtime_exception",
            error="The desktop runtime failed.",
        )
    finally:
        if not rec.get("_terminalized"):
            if rec["_cancel"].is_set():
                terminalize(rec, "aborted", "user_cancelled", "cancel")
            else:
                terminalize(
                    rec, "indeterminate", "runtime_ended_without_outcome",
                    "runtime_exit",
                )
        _schedule_artifact_cleanup(rec)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def start_run(goal, api_key, vision_model=None, director=None, autonomy="pause",
              max_steps=_MAX_STEPS_DEFAULT, postcondition=None,
              use_director=None):
    """Launch a desktop agent run in a background thread. Returns the run record's
    public status (including its id). Raises if pyautogui or the key is missing."""
    raw_spec = {
        "goal": goal,
        "use_director": director is not None if use_director is None else use_director,
        "autonomy": autonomy,
        "max_steps": max_steps,
        "postcondition": postcondition,
    }
    if vision_model is not None:
        raw_spec["vision_model"] = vision_model
    spec = launch_spec("desktop", raw_spec)
    goal = spec["goal"]
    requested_autonomy = spec["autonomy"]
    max_steps = spec["max_steps"]
    postcondition = spec["postcondition"]
    vision_model = spec["vision_model"]
    ok, detail = pyautogui_available()
    if not ok:
        raise RuntimeError(detail)
    if not api_key:
        raise RuntimeError("OpenAI API key required for the vision executor.")

    rec = _new_run(goal, requested_autonomy, postcondition)
    admit_action_run(rec)
    t = threading.Thread(
        target=_worker,
        args=(rec, api_key, vision_model, director, max_steps),
        daemon=True,
    )
    rec["_thread"] = t
    try:
        t.start()
    except Exception:
        terminalize(rec, "failed", "worker_start_failed", "runtime_start")
        raise
    return public_status(rec["id"])
