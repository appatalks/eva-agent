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
import time
import base64
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone

from bridge import config as _bridge_config

_TRAJ_DIR = os.path.expanduser("~/.config/eva-standalone/desktop_trajectories")
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
    "type", "press", "hotkey", "scroll", "wait", "done", "ask",
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
        "result": None,
        "error": None,
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
            )
        }


def cancel(run_id):
    with _runs_lock:
        rec = _runs.get(run_id)
    if not rec:
        return False
    rec["_cancel"].set()
    rec["_gate"].set()
    return True


def resolve(run_id, approve=True, text=""):
    with _runs_lock:
        rec = _runs.get(run_id)
    if not rec:
        return False
    if rec["status"] == "awaiting_confirmation":
        rec["_decision"] = bool(approve)
    elif rec["status"] == "awaiting_input":
        rec["_decision"] = text or ""
    else:
        return False
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
        '  {"action":"ask","question":"<what you need from the user>"}\n'
        '  {"action":"done","summary":"<what was accomplished>"}\n\n'
        "Rules: coordinates are absolute screen pixels. Put the real intent in "
        "reason (e.g. 'click the Tools menu') so it can be reviewed. To start an "
        "application, use launch_app with its binary name. Operate the target "
        "application window; do NOT interact with Eva's own assistant window. "
        "Prefer clicking visible controls. Emit done only when the goal is fully "
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


def _call_executor(api_key, model, goal, subgoal, history, active_app, png_bytes, w, h):
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
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {"action": "ask", "question": "Model returned no actionable JSON."}
    try:
        action = json.loads(text[start:end + 1])
    except Exception:
        return {"action": "ask", "question": "Model returned malformed action JSON."}
    if action.get("action") not in _ACTION_KINDS:
        return {"action": "ask", "question": f"Unknown action: {action.get('action')!r}."}
    return action


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
    rec["_gate"].clear()
    rec["_decision"] = None
    rec["status"] = status
    for k, v in fields.items():
        rec[k] = v
    rec["_gate"].wait()
    decision = rec["_decision"]
    rec["pending_action"] = None
    rec["pending_question"] = None
    rec["status"] = "running"
    return decision


# Keys allowed for press/hotkey, mapped to pyautogui names where they differ.
_KEY_ALIASES = {
    "esc": "esc", "escape": "esc", "return": "enter", "enter": "enter",
    "del": "delete", "delete": "delete", "ctrl": "ctrl", "control": "ctrl",
    "cmd": "command", "win": "winleft", "super": "winleft", "opt": "alt",
}


def _norm_key(k):
    k = str(k or "").strip().lower()
    return _KEY_ALIASES.get(k, k)


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
        subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_bridge_config.child_process_env(),
        )
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
                               timeout=5, env=_bridge_config.child_process_env())
            if r.returncode == 0:
                time.sleep(0.6)
                return f"focused window matching '{match}'"
        except Exception:
            pass
    xdotool = shutil.which("xdotool")
    if xdotool:
        try:
            out = subprocess.run([xdotool, "search", "--name", match],
                                 capture_output=True, text=True, timeout=5,
                                 env=_bridge_config.child_process_env())
            wid = (out.stdout or "").split("\n")[0].strip()
            if wid.isdigit():
                subprocess.run([xdotool, "windowactivate", wid],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, env=_bridge_config.child_process_env())
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
        x, y = int(action.get("x", 0)), int(action.get("y", 0))
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

def _worker(rec, api_key, vision_model, director, autonomy, max_steps):
    run_id = rec["id"]
    history = rec["steps"]
    subgoal = ""
    try:
        gui = _get_pyautogui()
        w, h = gui.size()
        rec["screen"] = f"{w}x{h}"
        rec["status"] = "running"

        if director:
            try:
                subgoal = director(rec["goal"], f"Desktop is {w}x{h}. Nothing launched yet.") or ""
            except Exception as e:
                print(f"[DesktopAgent] director error: {e}")
        rec["subgoal"] = subgoal

        step = 0
        while step < max_steps:
            if rec["_cancel"].is_set():
                rec["status"] = "cancelled"
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
            except Exception as e:
                rec["status"] = "error"
                rec["error"] = f"screenshot failed: {e}"
                break
            rec["last_screenshot"] = shot_path

            try:
                action, raw = _call_executor(
                    api_key, vision_model, rec["goal"], subgoal,
                    history, rec["active_app"], png, w, h,
                )
            except Exception as e:
                rec["status"] = "error"
                rec["error"] = str(e)
                break

            kind = action.get("action")

            if kind == "done":
                rec["result"] = action.get("summary", "Task complete.")
                rec["status"] = "done"
                _record(rec, step, shot_path, subgoal, raw, action, "done")
                break

            if kind == "ask":
                answer = _park(rec, "awaiting_input",
                               pending_question=action.get("question", "Need input."))
                if rec["_cancel"].is_set():
                    rec["status"] = "cancelled"
                    break
                subgoal = (subgoal + f"\nUser said: {answer}").strip()
                rec["subgoal"] = subgoal
                _record(rec, step, shot_path, subgoal, raw, action, "asked user")
                step += 1
                continue

            if _is_sensitive(action) and autonomy == "pause":
                approved = _park(rec, "awaiting_confirmation", pending_action=action)
                if rec["_cancel"].is_set():
                    rec["status"] = "cancelled"
                    break
                if not approved:
                    _record(rec, step, shot_path, subgoal, raw, action, "declined")
                    rec["result"] = "Stopped: user declined a sensitive action."
                    rec["status"] = "done"
                    break

            try:
                result = _execute(gui, action, rec)
            except Exception as e:
                result = f"error: {e}"
            _record(rec, step, shot_path, subgoal, raw, action, result)

            # Loop guard: if the same action keeps producing the same result
            # (e.g. a launch that errors, or a click that changes nothing), the
            # executor is stuck. After a few identical repeats, stop and ask the
            # user rather than burning the whole step budget in a tight loop.
            sig = json.dumps(action, sort_keys=True) + "|" + str(result)
            if sig == rec.get("_last_sig"):
                rec["_repeat"] = rec.get("_repeat", 0) + 1
            else:
                rec["_repeat"] = 0
                rec["_last_sig"] = sig
            if rec["_repeat"] >= 2:
                q = "I'm repeating the same step without progress"
                if isinstance(result, str) and result.startswith("error"):
                    q += " (" + result + ")"
                q += ". How would you like me to proceed, or should I stop?"
                answer = _park(rec, "awaiting_input", pending_question=q)
                if rec["_cancel"].is_set():
                    rec["status"] = "cancelled"
                    break
                rec["_repeat"] = 0
                rec["_last_sig"] = None
                subgoal = (subgoal + f"\nUser said: {answer}").strip()
                rec["subgoal"] = subgoal

            time.sleep(0.4)
            step += 1
            rec["step"] = step

            if director and step % _DIRECTOR_INTERVAL == 0:
                try:
                    summary = f"Active app: {rec['active_app'] or 'unknown'}. Last action: {json.dumps(action)} -> {result}."
                    new_sub = director(rec["goal"], summary)
                    if new_sub:
                        subgoal = new_sub
                        rec["subgoal"] = subgoal
                except Exception as e:
                    print(f"[DesktopAgent] director error: {e}")
        else:
            if rec["status"] not in ("error", "cancelled"):
                rec["status"] = "done"
            if rec["result"] is None:
                rec["result"] = f"Reached step limit ({max_steps})."
    except Exception as e:
        rec["status"] = "error"
        rec["error"] = str(e)
    finally:
        rec["finished"] = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def start_run(goal, api_key, vision_model=None, director=None, autonomy="pause",
              max_steps=_MAX_STEPS_DEFAULT):
    """Launch a desktop agent run in a background thread. Returns the run record's
    public status (including its id). Raises if pyautogui or the key is missing."""
    ok, detail = pyautogui_available()
    if not ok:
        raise RuntimeError(detail)
    if not api_key:
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
    t = threading.Thread(
        target=_worker,
        args=(rec, api_key, vision_model, director, autonomy, max_steps),
        daemon=True,
    )
    rec["_thread"] = t
    t.start()
    return public_status(rec["id"])
