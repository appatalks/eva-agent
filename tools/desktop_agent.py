"""Vision-driven desktop agent for Eva ("computer use").

A closed loop: screenshot -> multimodal model -> structured action JSON ->
pyautogui executes on the real desktop -> new screenshot -> repeat. It mirrors
browser_agent.py but drives the whole desktop (and can launch applications)
instead of a Chromium page.

Two roles:
  - Director (text only): high level planner, wired by the bridge to Claude via
    ACP. Sees a text state summary, sets the current subgoal.
  - Executor (vision): looks at the screenshot and emits the next concrete
    action. Defaults to an OpenAI vision model.

pyautogui (and PIL) are imported lazily so a missing install never breaks bridge
import. pyautogui's FAILSAFE stays ON: slamming the mouse into a screen corner
aborts the run as an emergency stop.

SAFETY: this controls the user's real machine. App launches and any action whose
intent matches the destructive/sensitive pattern park for confirmation when
autonomy == "pause".
"""

import os
import re
import json
import io
import time
import base64
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone

from bridge.automation import (
    action_signature,
    automation_audit,
    is_explicit_cancel,
    parse_action,
    parse_json_object,
    recent_signature_count,
)

_EVA_CONFIG_DIR = os.path.expanduser(os.environ.get("EVA_CONFIG_DIR", "~/.config/eva-standalone"))
_TRAJ_DIR = os.path.join(_EVA_CONFIG_DIR, "desktop_trajectories")
_DEFAULT_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o")
_MAX_STEPS_DEFAULT = 25
_DIRECTOR_INTERVAL = 4  # re-consult the director every N executor steps

# Action intent matching this pattern parks for confirmation under autonomy
# "pause". Covers purchases/auth (as in the browser agent) plus desktop-level
# destructive operations.
_SENSITIVE_RE = re.compile(
    r"\b(buy|purchase|place\s+order|order\s+now|add\s+to\s+cart|pay|payment|"
    r"checkout|complete\s+(?:order|purchase)|confirm\s+(?:order|purchase|payment)|"
    r"log\s*in|sign\s*in|password|delete|remove|uninstall|format|erase|wipe|"
    r"shut\s*down|shutdown|reboot|restart|power\s*off|sudo|rm\s+-|overwrite|"
    r"transfer\s+(?:money|funds)|wire\b|send\s+(?:email|message))",
    re.I,
)

_ACTION_KINDS = {
    "launch_app", "focus_window", "click", "double_click", "right_click", "move",
    "type", "press", "hotkey", "scroll", "wait", "crop", "done", "ask",
}

_ACTION_FIELDS = {
    "launch_app": {"app": str},
    "focus_window": {"match": str},
    "click": {"x": int, "y": int},
    "double_click": {"x": int, "y": int},
    "right_click": {"x": int, "y": int},
    "move": {"x": int, "y": int},
    "type": {"text": str},
    "press": {"key": str},
    "hotkey": {"keys": list},
    "scroll": {"dy": int},
    "wait": {"ms": int},
    "crop": {"x": int, "y": int, "width": int, "height": int},
    "ask": {"question": str},
    "done": {"summary": str},
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
    except Exception as e:
        return False, f"pyautogui not installed: {e}"
    return True, "ok"


def _get_pyautogui():
    import pyautogui
    pyautogui.FAILSAFE = True       # mouse to a corner aborts (emergency stop)
    pyautogui.PAUSE = 0.15          # small settle delay between calls
    return pyautogui


# ---------------------------------------------------------------------------
# Run registry
# ---------------------------------------------------------------------------

def _new_run(goal):
    run_id = uuid.uuid4().hex[:16]
    rec = {
        "id": run_id,
        "goal": goal,
        "status": "starting",        # starting|running|awaiting_confirmation|awaiting_input|done|cancelled|error
        "step": 0,
        "active_app": "",
        "subgoal": "",
        "clarifications": [],
        "result": None,
        "error": None,
        "completion_verified": False,
        "verification": None,
        "executor_provider": "openai",
        "executor_model": _DEFAULT_VISION_MODEL,
        "backend": "legacy",
        "pending_action": None,
        "pending_question": None,
        "last_screenshot": None,
        "screen": "",
        "started": datetime.now(timezone.utc).isoformat(),
        "finished": None,
        "steps": [],
        "_cancel": threading.Event(),
        "_gate": threading.Event(),
        "_decision": None,
        "_state_lock": threading.RLock(),
        "_action_lock": threading.Lock(),
        "_thread": None,
    }
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
        return {
            k: rec[k] for k in (
                "id", "goal", "status", "step", "active_app", "subgoal",
                "result", "error", "pending_action", "pending_question",
                "last_screenshot", "screen", "started", "finished", "steps",
                "completion_verified", "verification", "executor_provider", "executor_model",
                "backend",
            )
        }


def _mark_cancelled(rec, outcome="cancelled"):
    with rec["_state_lock"]:
        if rec["status"] in ("done", "blocked", "error"):
            return
        rec["status"] = "cancelled"
        rec["completion_verified"] = False
        rec["result"] = "Automation was cancelled before completion was verified."
        rec["pending_action"] = None
        rec["pending_question"] = None
    rec["_cancel"].set()
    rec["_gate"].set()
    automation_audit(rec["id"], "cancel", rec.get("backend"), "run", outcome)


def cancel(run_id):
    with _runs_lock:
        rec = _runs.get(run_id)
    if not rec:
        return False
    with rec["_action_lock"]:
        _mark_cancelled(rec)
    return True


def resolve(run_id, approve=True, text=""):
    with _runs_lock:
        rec = _runs.get(run_id)
    if not rec:
        return False
    with rec["_state_lock"]:
        status = rec["status"]
    if status not in ("awaiting_confirmation", "awaiting_input"):
        return False
    if not approve or is_explicit_cancel(text):
        _mark_cancelled(rec, "declined" if not approve else "cancelled")
        return True
    with rec["_state_lock"]:
        rec["_decision"] = bool(approve) if status == "awaiting_confirmation" else (text or "")
    rec["_gate"].set()
    return True


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
        '  {"action":"launch_app","app":"<binary name, e.g. gimp>","args":["..."],"reason":"..."}\n'
        '  {"action":"focus_window","match":"<window title substring, e.g. Chrome>","reason":"..."}\n'
        '  {"action":"click","x":<int>,"y":<int>,"reason":"<intent>"}\n'
        '  {"action":"double_click","x":<int>,"y":<int>,"reason":"..."}\n'
        '  {"action":"right_click","x":<int>,"y":<int>,"reason":"..."}\n'
        '  {"action":"move","x":<int>,"y":<int>,"reason":"..."}\n'
        '  {"action":"type","text":"<text>","reason":"..."}   (types into the focused field; click it first)\n'
        '  {"action":"press","key":"<enter|tab|esc|down|up|ctrl|...>","reason":"..."}\n'
        '  {"action":"hotkey","keys":["ctrl","s"],"reason":"..."}   (chord, e.g. ctrl+s to save)\n'
        '  {"action":"scroll","dy":<int>,"reason":"..."}      (positive scrolls up, negative down)\n'
        '  {"action":"wait","ms":<int>,"reason":"..."}\n'
        '  {"action":"crop","x":<int>,"y":<int>,"width":<int>,"height":<int>,"reason":"inspect this region"}\n'
        '  {"action":"ask","question":"<what you need from the user>"}\n'
        '  {"action":"done","summary":"<what was accomplished>"}\n\n'
        "Rules: coordinates are absolute screen pixels. Put the real intent in "
        "reason (e.g. 'click the Tools menu') so it can be reviewed. To start an "
        "application, use launch_app with its binary name. Operate the target "
        "application window; do NOT interact with Eva's own assistant window. "
        "Prefer clicking visible controls. Use crop only to inspect the current "
        "screen; it never clicks or changes the desktop. Emit done only when the goal is fully "
        "achieved, and ask when blocked or needing info only the user has.\n"
        "WEB TASKS: if the user already has a browser open (e.g. Chrome) and is "
        "signed in, USE IT instead of launching a new one: focus_window with "
        'match \"Chrome\" (or \"Firefox\") to raise the existing window, then open '
        "a NEW TAB with hotkey ctrl+t, focus the address bar with hotkey ctrl+l, "
        "type the URL or search, and press enter. This reuses the user's logged-in "
        "session (Amazon, etc.). Only launch_app a browser if none is open.\n"
        "Never output prose outside the JSON."
    )


def _b64_png(data):
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _call_openai_raw(api_key, model, system_text, user_text, png_bytes):
    import requests as _req

    payload = {
        "model": model,
        "max_tokens": 400,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_text},
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
        raise RuntimeError(f"vision model unavailable ({resp.status_code})")
    try:
        return resp.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("vision model returned no content") from exc


def _call_executor(api_key, model, goal, subgoal, history, active_app, png_bytes, w, h,
                   vision_call=None, vision_note="", clarifications=None):

    hist_lines = []
    for entry in history[-8:]:
        a = entry.get("action", {})
        hist_lines.append(f"step {entry.get('step')}: {json.dumps(a)} -> {entry.get('result','')}")
    history_text = "\n".join(hist_lines) if hist_lines else "(none yet)"

    user_text = (
        f"GOAL: {goal}\n"
        f"USER CLARIFICATIONS: {' | '.join(clarifications or []) or '(none)'}\n"
        f"CURRENT SUBGOAL: {subgoal or goal}\n"
        f"ACTIVE APP (best guess): {active_app or 'unknown'}\n"
        f"SCREEN: {w}x{h}\n"
        + (f"VIEW NOTE: {vision_note}\n" if vision_note else "")
        + f"RECENT ACTIONS:\n{history_text}\n\n"
        "Return the next action JSON."
    )

    raw = (vision_call(_executor_system(w, h), user_text, png_bytes)
           if vision_call else _call_openai_raw(api_key, model, _executor_system(w, h), user_text, png_bytes))
    return _parse_action(raw), raw


def _parse_action(raw):
    return parse_action(raw, _ACTION_KINDS, _ACTION_FIELDS)


# ---------------------------------------------------------------------------
# Sensitivity
# ---------------------------------------------------------------------------

def _is_sensitive(action):
    # Launching a PATH-resolved application with no shell is low-risk, and
    # gating every launch made the agent feel stuck behind an approval prompt.
    # Launches now proceed automatically; the destructive-intent scan below
    # still gates genuinely risky actions (delete, shutdown, purchase, etc.),
    # including a launch whose name/args carry destructive intent.
    probe = " ".join(str(action.get(k, "")) for k in ("reason", "text", "question", "app"))
    if isinstance(action.get("keys"), list):
        probe += " " + " ".join(str(k) for k in action["keys"])
    return bool(_SENSITIVE_RE.search(probe))


# ---------------------------------------------------------------------------
# Trajectory logging
# ---------------------------------------------------------------------------

def _run_dir(run_id):
    d = os.path.join(_TRAJ_DIR, run_id)
    os.makedirs(d, exist_ok=True)
    return d


def _log_step(run_id, record):
    try:
        with open(os.path.join(_run_dir(run_id), "trajectory.jsonl"), "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"[DesktopAgent] log write failed: {e}")


def _record(rec, step, shot_path, subgoal, raw, action, result):
    entry = {
        "step": step,
        "ts": datetime.now(timezone.utc).isoformat(),
        "active_app": rec["active_app"],
        "goal": rec["goal"],
        "subgoal": subgoal,
        "model_raw": raw[:1000] if isinstance(raw, str) else "",
        "action": action,
        "result": result,
        "screenshot": shot_path,
    }
    rec["steps"].append({"step": step, "action": action, "result": result})
    _log_step(rec["id"], entry)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _park(rec, status, **fields):
    with rec["_state_lock"]:
        if rec["_cancel"].is_set():
            rec["status"] = "cancelled"
            return None
        rec["_gate"].clear()
        rec["_decision"] = None
        rec["status"] = status
        for k, v in fields.items():
            rec[k] = v
    rec["_gate"].wait()
    with rec["_state_lock"]:
        cancelled = rec["_cancel"].is_set()
        decision = rec["_decision"]
        rec["pending_action"] = None
        rec["pending_question"] = None
        if not cancelled and rec["status"] not in ("blocked", "error", "done"):
            rec["status"] = "running"
    return None if cancelled else decision


# Keys allowed for press/hotkey, mapped to pyautogui names where they differ.
_KEY_ALIASES = {
    "esc": "esc", "escape": "esc", "return": "enter", "enter": "enter",
    "del": "delete", "delete": "delete", "ctrl": "ctrl", "control": "ctrl",
    "cmd": "command", "win": "winleft", "super": "winleft", "opt": "alt",
}


def _norm_key(k):
    k = str(k or "").strip().lower()
    return _KEY_ALIASES.get(k, k)


def _crop_view(png_bytes, action):
    from PIL import Image

    with Image.open(io.BytesIO(png_bytes)) as source:
        source = source.convert("RGB")
        screen_w, screen_h = source.size
        x, y = action["x"], action["y"]
        width, height = action["width"], action["height"]
        if (width <= 0 or height <= 0 or x < 0 or y < 0 or
                x + width > screen_w or y + height > screen_h):
            raise ValueError("crop rectangle is outside the current screenshot")
        crop = source.crop((x, y, x + width, y + height))
        scale = min(3.0, 1200 / width, 900 / height)
        crop = crop.resize((max(1, round(width * scale)), max(1, round(height * scale))))
        overview = source.copy()
        overview.thumbnail((900, 700))
        margin = 16
        canvas = Image.new(
            "RGB",
            (overview.width + crop.width + margin * 3, max(overview.height, crop.height) + margin * 2),
            "white",
        )
        canvas.paste(overview, (margin, margin))
        canvas.paste(crop, (overview.width + margin * 2, margin))
        output = io.BytesIO()
        canvas.save(output, format="PNG")
        note = (
            f"The displayed view contains a whole-screen overview and an enlarged crop. "
            f"The crop covers original screen coordinates x={x}..{x + width - 1}, "
            f"y={y}..{y + height - 1}; all click coordinates must still use the "
            f"original {screen_w}x{screen_h} screen coordinate system."
        )
        return output.getvalue(), note, (screen_w, screen_h)


def _verify_completion(goal, clarifications, summary, png_bytes, w, h, vision_call, api_key, model, vision_note=""):
    system_text = (
        "You independently verify a desktop automation completion claim. Do not use tools "
        "and do not propose actions. Inspect the current screenshot against the original "
        "goal and user clarifications. Reply with exactly one JSON object: "
        '{"verified":true|false,"evidence":"non-empty visual facts"}. '
        "Verification is not absolute truth: set verified false when the screenshot is "
        "ambiguous, illegible, or does not visibly establish the goal."
    )
    user_text = (
        f"ORIGINAL GOAL: {goal}\n"
        f"USER CLARIFICATIONS: {' | '.join(clarifications or []) or '(none)'}\n"
        f"EXECUTOR SUMMARY (untrusted): {summary}\n"
        f"CURRENT SCREEN: {w}x{h}\n"
        f"VIEW MAPPING: {vision_note or 'Full screenshot in original coordinates.'}\n"
        "Return the structured verification JSON now."
    )
    raw = (vision_call(system_text, user_text, png_bytes)
           if vision_call else _call_openai_raw(api_key, model, system_text, user_text, png_bytes))
    data = parse_json_object(raw)
    evidence = str(data.get("evidence") or "").strip() if data else ""
    return {
        "verified": bool(data and data.get("verified") is True and evidence),
        "evidence": evidence[:500] or "No non-empty visual evidence was provided.",
    }


# Common friendly names that vision models reach for, mapped to the candidate
# binaries that actually exist across desktops. The first candidate found on
# PATH wins, so this works regardless of which desktop environment is installed.
_APP_ALIASES = {
    "calculator": ["gnome-calculator", "kcalc", "qalculate-gtk", "galculator", "mate-calc", "xcalc"],
    "calc": ["gnome-calculator", "kcalc", "qalculate-gtk", "galculator", "mate-calc", "xcalc"],
    "files": ["nautilus", "dolphin", "nemo", "thunar", "pcmanfm", "caja"],
    "file manager": ["nautilus", "dolphin", "nemo", "thunar", "pcmanfm", "caja"],
    "filemanager": ["nautilus", "dolphin", "nemo", "thunar", "pcmanfm", "caja"],
    "terminal": ["gnome-terminal", "konsole", "xterm", "alacritty", "kitty", "xfce4-terminal"],
    "text editor": ["gedit", "kate", "gnome-text-editor", "mousepad", "xed"],
    "editor": ["gedit", "kate", "gnome-text-editor", "mousepad", "xed"],
    "browser": ["firefox", "google-chrome", "chromium", "chromium-browser", "brave-browser"],
    "web browser": ["firefox", "google-chrome", "chromium", "chromium-browser", "brave-browser"],
    "screenshot": ["gnome-screenshot", "spectacle", "flameshot", "scrot"],
    "image editor": ["gimp", "krita", "pinta"],
    "paint": ["gimp", "krita", "pinta", "kolourpaint"],
    "settings": ["gnome-control-center", "systemsettings5", "systemsettings"],
}


def _resolve_app_binary(app):
    """Resolve a friendly or exact app name to a real binary on PATH.

    Vision models reach for generic names ("calculator") that are rarely the
    actual binary ("gnome-calculator"). Try the literal name first, then a
    curated alias table, then a couple of common naming variants. Returns the
    absolute binary path or None.
    """
    direct = shutil.which(app)
    if direct:
        return direct
    key = app.strip().lower()
    for cand in _APP_ALIASES.get(key, []):
        found = shutil.which(cand)
        if found:
            return found
    # Try common variants: gnome-<app>, hyphenated, and stripped 'app' suffix.
    for variant in ("gnome-" + key, key.replace(" ", "-"), key.replace(" app", "").strip()):
        if variant and variant != app:
            found = shutil.which(variant)
            if found:
                return found
    return None


def _launch_app(action):
    app = str(action.get("app", "")).strip()
    if not app or not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", app):
        return "error: invalid app name"
    binary = _resolve_app_binary(app)
    if not binary:
        return f"error: '{app}' is not installed / not on PATH"
    args = action.get("args") or []
    if not isinstance(args, list):
        args = []
    cmd = [binary] + [str(a) for a in args][:12]
    try:
        # No shell; arguments passed as a list so nothing is interpreted.
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"launched {app}"
    except Exception as e:
        return f"error launching {app}: {e}"


def _focus_window(action):
    """Raise and focus an existing window whose title contains `match`.

    Lets the agent reuse the user's already-open, signed-in browser instead of
    launching a new one. Uses wmctrl (preferred) or xdotool; no shell, args as a
    list, and the match string is constrained so it cannot inject options.
    """
    match = str(action.get("match", "")).strip()
    if not match or len(match) > 64 or not re.fullmatch(r"[A-Za-z0-9 ._+:/-]{1,64}", match):
        return "error: invalid window match"
    wmctrl = shutil.which("wmctrl")
    if wmctrl:
        try:
            # -F + exact would be too strict; -i not needed. Use substring match
            # via wmctrl's built-in -a (activates a window by title substring).
            r = subprocess.run([wmctrl, "-a", match],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5)
            if r.returncode == 0:
                time.sleep(0.6)
                return f"focused window matching '{match}'"
        except Exception:
            pass
    xdotool = shutil.which("xdotool")
    if xdotool:
        try:
            out = subprocess.run([xdotool, "search", "--name", match],
                                 capture_output=True, text=True, timeout=5)
            wid = (out.stdout or "").split("\n")[0].strip()
            if wid.isdigit():
                subprocess.run([xdotool, "windowactivate", wid],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5)
                time.sleep(0.6)
                return f"focused window matching '{match}'"
        except Exception:
            pass
    return f"error: no window matching '{match}' (or no window tool available)"


def _execute(gui, action, rec):
    kind = action["action"]
    if kind == "launch_app":
        result = _launch_app(action)
        rec["active_app"] = str(action.get("app", "")) or rec["active_app"]
        time.sleep(1.5)  # give the window time to appear
        return result
    if kind == "focus_window":
        result = _focus_window(action)
        if not result.startswith("error"):
            rec["active_app"] = str(action.get("match", "")) or rec["active_app"]
        return result
    if kind in ("click", "double_click", "right_click", "move"):
        x, y = action.get("x"), action.get("y")
        screen_w, screen_h = rec.get("_screen_size", (0, 0))
        if (not isinstance(x, int) or isinstance(x, bool) or not isinstance(y, int) or
            isinstance(y, bool) or x < 0 or y < 0 or x >= screen_w or y >= screen_h):
            return "error: coordinates outside the current screenshot"
        gui_w, gui_h = rec.get("_gui_size", (screen_w, screen_h))
        x = min(gui_w - 1, int(x * gui_w / screen_w))
        y = min(gui_h - 1, int(y * gui_h / screen_h))
        if kind == "click":
            gui.click(x, y)
        elif kind == "double_click":
            gui.doubleClick(x, y)
        elif kind == "right_click":
            gui.click(x, y, button="right")
        else:
            gui.moveTo(x, y)
        return f"{kind} at ({x},{y})"
    if kind == "type":
        gui.write(str(action.get("text", "")), interval=0.02)
        return "typed text"
    if kind == "press":
        gui.press(_norm_key(action.get("key", "enter")))
        return f"pressed {action.get('key')}"
    if kind == "hotkey":
        keys = [_norm_key(k) for k in (action.get("keys") or []) if str(k).strip()][:5]
        if keys:
            gui.hotkey(*keys)
            return "hotkey " + "+".join(keys)
        return "noop (empty hotkey)"
    if kind == "scroll":
        dy = int(action.get("dy", 0))
        gui.scroll(dy)
        return f"scrolled {dy}"
    if kind == "wait":
        time.sleep(min(int(action.get("ms", 500)) / 1000.0, 5.0))
        return "waited"
    return "noop"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _worker(rec, api_key, vision_model, director, autonomy, max_steps,
            vision_call=None, vision_cleanup=None):
    run_id = rec["id"]
    history = rec["steps"]
    subgoal = ""
    pending_crop = None
    automation_audit(run_id, "start", rec.get("backend"), "run", "started")
    try:
        gui = _get_pyautogui()
        w, h = gui.size()
        rec["screen"] = f"{w}x{h}"
        rec["status"] = "running" if not rec["_cancel"].is_set() else "cancelled"

        if rec["_cancel"].is_set():
            return

        if director:
            try:
                subgoal = director(
                    rec["goal"],
                    f"Desktop is {w}x{h}. Nothing launched yet. User clarifications: "
                    f"{' | '.join(rec['clarifications']) or '(none)'}.",
                ) or ""
            except Exception as e:
                print(f"[DesktopAgent] director error: {e}")
            if rec["_cancel"].is_set():
                return
        rec["subgoal"] = subgoal

        step = 0
        while step < max_steps:
            if rec["_cancel"].is_set():
                break

            try:
                # Pass an explicit path: pyautogui's Linux backend (scrot) writes
                # its intermediate file to the CURRENT WORKING DIRECTORY when no
                # filename is given, which fails when cwd is read-only (e.g. an
                # AppImage mount). Writing straight to the run dir avoids that.
                shot_path = os.path.join(_run_dir(run_id), f"step_{step:02d}.png")
                img = gui.screenshot(shot_path)
                with open(shot_path, "rb") as f:
                    png = f.read()
                actual_size = getattr(img, "size", None)
                if not actual_size or len(actual_size) != 2:
                    actual_size = (w, h)
                w, h = int(actual_size[0]), int(actual_size[1])
                rec["_gui_size"] = tuple(gui.size())
            except Exception as e:
                if not rec["_cancel"].is_set():
                    rec["status"] = "error"
                    rec["error"] = f"screenshot failed: {e}"
                break
            rec["last_screenshot"] = shot_path
            rec["screen"] = f"{w}x{h}"
            rec["_screen_size"] = (w, h)
            model_png, model_note = png, ""
            if pending_crop:
                model_png, model_note, _ = _crop_view(png, pending_crop)
                pending_crop = None

            try:
                action, raw = _call_executor(
                    api_key, vision_model, rec["goal"], subgoal,
                    history, rec["active_app"], model_png, w, h,
                    vision_call=vision_call, vision_note=model_note,
                    clarifications=rec["clarifications"],
                )
            except Exception as e:
                if not rec["_cancel"].is_set():
                    rec["status"] = "error"
                    rec["error"] = "vision provider unavailable"
                break

            if rec["_cancel"].is_set():
                break
            kind = action.get("action")

            if kind == "done":
                summary = str(action.get("summary") or "").strip()
                if not summary:
                    rec["status"] = "blocked"
                    rec["result"] = "Completion was blocked because the model supplied no summary."
                    _record(rec, step, shot_path, subgoal, raw, action, "blocked: empty summary")
                    automation_audit(run_id, "step", rec.get("backend"), "done", "blocked")
                    break
                try:
                    verification = _verify_completion(
                        rec["goal"], rec["clarifications"], summary, model_png, w, h,
                        vision_call, api_key, vision_model, vision_note=model_note,
                    )
                except Exception:
                    verification = {"verified": False, "evidence": "Verification provider failed."}
                with rec["_action_lock"]:
                    if rec["_cancel"].is_set():
                        break
                    rec["verification"] = verification
                    if verification["verified"]:
                        rec["result"] = summary
                        rec["completion_verified"] = True
                        rec["status"] = "done"
                        outcome = "done"
                    else:
                        rec["completion_verified"] = False
                        rec["status"] = "blocked"
                        rec["result"] = "Completion could not be verified from the current screenshot."
                        outcome = "blocked"
                _record(rec, step, shot_path, subgoal, raw, action, outcome)
                automation_audit(run_id, "step", rec.get("backend"), "done", outcome)
                break

            if kind == "ask":
                answer = _park(rec, "awaiting_input",
                               pending_question=action.get("question", "Need input."))
                if rec["_cancel"].is_set():
                    break
                answer = str(answer or "").strip()
                if answer:
                    rec["clarifications"].append(answer[:500])
                _record(rec, step, shot_path, subgoal, raw, action, "asked user")
                automation_audit(run_id, "step", rec.get("backend"), "ask", "awaiting_input")
                step += 1
                rec["step"] = step
                continue

            if kind == "crop":
                try:
                    _crop_view(png, action)
                    pending_crop = action
                    result = "inspected crop"
                except Exception as e:
                    result = "error: invalid crop rectangle"
                    rec["status"] = "blocked"
                    rec["result"] = "Crop inspection was blocked because the rectangle was invalid."
                _record(rec, step, shot_path, subgoal, raw, action, result)
                automation_audit(run_id, "step", rec.get("backend"), "crop",
                                 "blocked" if result.startswith("error") else "observed")
                if rec["status"] == "blocked":
                    break
                step += 1
                rec["step"] = step
                continue

            needs_confirmation = autonomy == "confirm_all" or (
                autonomy == "pause" and _is_sensitive(action)
            )
            if needs_confirmation:
                approved = _park(rec, "awaiting_confirmation", pending_action=action)
                if rec["_cancel"].is_set():
                    break
                if not approved:
                    _record(rec, step, shot_path, subgoal, raw, action, "declined")
                    _mark_cancelled(rec, "declined")
                    break

            with rec["_action_lock"]:
                if rec["_cancel"].is_set():
                    break
                try:
                    result = _execute(gui, action, rec)
                except Exception as e:
                    result = f"error: {e}"
            _record(rec, step, shot_path, subgoal, raw, action, result)
            automation_audit(run_id, "step", rec.get("backend"), kind,
                             "error" if str(result).startswith("error") else "executed")

            sig = action_signature(action)
            if recent_signature_count(history, sig, window=6) >= 3:
                rec["status"] = "blocked"
                rec["result"] = "Automation was blocked after repeating the same physical action."
                break

            if rec["_cancel"].wait(0.4):
                break
            step += 1
            rec["step"] = step

            if director and step % _DIRECTOR_INTERVAL == 0:
                try:
                    summary = (
                        f"Active app: {rec['active_app'] or 'unknown'}. Last action kind: {kind}. "
                        f"Execution receipt: {result}. User clarifications: "
                        f"{' | '.join(rec['clarifications']) or '(none)'}."
                    )
                    new_sub = director(rec["goal"], summary)
                    if rec["_cancel"].is_set():
                        break
                    if new_sub:
                        subgoal = new_sub
                        rec["subgoal"] = subgoal
                except Exception as e:
                    print(f"[DesktopAgent] director error: {e}")
        else:
            if not rec["_cancel"].is_set() and rec["status"] not in ("error", "blocked"):
                rec["status"] = "blocked"
                rec["result"] = "Step limit reached before completion was verified."
    except Exception as e:
        if not rec["_cancel"].is_set():
            rec["status"] = "error"
            rec["error"] = "desktop automation failed"
    finally:
        if vision_cleanup:
            try:
                vision_cleanup()
            except Exception:
                pass
        if rec["_cancel"].is_set():
            with rec["_state_lock"]:
                rec["status"] = "cancelled"
                rec["completion_verified"] = False
        rec["finished"] = datetime.now(timezone.utc).isoformat()
        automation_audit(run_id, "finish", rec.get("backend"), "run", rec.get("status"))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def start_run(goal, api_key=None, vision_model=None, director=None, autonomy="pause",
              max_steps=_MAX_STEPS_DEFAULT, vision_call=None, vision_cleanup=None,
              backend="legacy", executor_provider="openai", executor_model=None):
    """Launch a desktop agent run in a background thread. Returns the run record's
    public status (including its id). Raises if pyautogui or the key is missing."""
    ok, detail = pyautogui_available()
    if not ok:
        raise RuntimeError(detail)
    if not api_key and vision_call is None:
        raise RuntimeError("OpenAI API key required for the vision executor.")

    goal = (goal or "").strip()
    if not goal:
        raise RuntimeError("goal is required.")

    vision_model = vision_model or _DEFAULT_VISION_MODEL
    try:
        max_steps = max(1, min(int(max_steps), 60))
    except Exception:
        max_steps = _MAX_STEPS_DEFAULT
    if autonomy not in ("pause", "confirm_all", "auto"):
        autonomy = "pause"

    rec = _new_run(goal)
    rec["backend"] = str(backend or "legacy")[:64]
    rec["executor_provider"] = str(executor_provider or "openai")[:32]
    rec["executor_model"] = str(executor_model or vision_model or _DEFAULT_VISION_MODEL)[:80]
    t = threading.Thread(
        target=_worker,
        args=(rec, api_key, vision_model, director, autonomy, max_steps, vision_call, vision_cleanup),
        daemon=True,
    )
    rec["_thread"] = t
    t.start()
    return public_status(rec["id"])
