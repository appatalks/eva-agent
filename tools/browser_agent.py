"""Vision-driven browser agent for Eva.

A closed loop: screenshot -> multimodal model -> structured action JSON ->
Playwright executes -> new screenshot -> repeat. The action schema is Eva's own
(not a vendor format), and every step is logged as JSONL plus a PNG so the
trajectories can be used to fine-tune a future in-house policy model.

Two roles:
  - Director (text only): high level planner. Wired by the bridge to Claude
    Opus 4.8 via ACP. Sees a text state summary, sets the current subgoal. It
    never sees pixels because the ACP prompt path is text only.
  - Executor (vision): looks at the screenshot and emits the next concrete
    action. Defaults to an OpenAI vision model (gpt-4o or better).

Playwright is imported lazily so a missing install never breaks bridge import.
"""

import os
import re
import json
import time
import base64
import shutil
import socket
import subprocess
import threading
import urllib.request
import uuid
from datetime import datetime, timezone

from bridge import config as _bridge_config

_TRAJ_DIR = os.path.expanduser("~/.config/eva-standalone/browser_trajectories")
# Dedicated, persistent Chrome profile for the agent. Logins (e.g. Amazon) made
# once in the agent window persist here across runs, so the agent is not a fresh
# unauthenticated session every time. Kept separate from the user's real Chrome
# profile so it can run alongside an already-open Chrome.
_PROFILE_DIR = os.path.expanduser("~/.config/eva-standalone/browser_profile")
# A long-lived Chrome we launch once with a remote-debugging port and reuse: the
# agent connects over CDP and opens a NEW TAB in that existing window each run,
# instead of spawning a fresh browser. The window stays open between runs so the
# session (and login) persists and the user can watch.
_CDP_PORT = int(os.environ.get("EVA_BROWSER_CDP_PORT", "9333"))
_chrome_proc = None          # subprocess.Popen for the long-lived Chrome
_chrome_lock = threading.Lock()
_VIEWPORT = {"width": 1280, "height": 800}
_DEFAULT_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o")
_MAX_STEPS_DEFAULT = 25
_DIRECTOR_INTERVAL = 4  # re-consult the director every N executor steps

# Only the FINAL purchase commit requires confirmation. Everything else (search,
# navigate, add-to-cart, sign-in, fill forms) is auto-approved so the agent
# flows naturally. The gate is deliberately narrow: it matches the irreversible
# "spend money now" buttons, not browsing or cart actions.
_SENSITIVE_RE = re.compile(
    r"\b(buy\s*now|place\s+(?:your\s+)?order|complete\s+(?:purchase|order)|"
    r"confirm\s+(?:and\s+)?(?:order|purchase|payment)|submit\s+order|"
    r"pay\s+now|proceed\s+to\s+(?:buy|pay)|place\s+order)\b",
    re.I,
)

_ACTION_KINDS = {
    "click", "double_click", "click_ref", "type", "type_ref", "press", "scroll",
    "navigate", "wait", "done", "ask",
}

# run_id -> run record (see _new_run). Guarded by _runs_lock.
_runs = {}
_runs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Long-lived Chrome (CDP) — open a new tab in an existing window each run
# ---------------------------------------------------------------------------

def _cdp_alive(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def _find_chrome_binary():
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _clean_chrome_env():
    """Strip bundled-runtime variables so the system Chrome uses system libraries.

    When the bridge runs inside the Electron AppImage, the environment carries
    LD_LIBRARY_PATH / LD_PRELOAD / APPDIR pointing at the AppImage's bundled libs.
    The system Chrome inherits those and its sandbox helper fails (the "sandbox"
    launch error). Removing them lets Chrome load its own libraries normally.
    """
    env = _bridge_config.child_process_env()
    for key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "APPDIR", "APPIMAGE", "ARGV0",
                "GTK_PATH", "GDK_PIXBUF_MODULE_FILE", "GIO_MODULE_DIR",
                "GSETTINGS_SCHEMA_DIR", "FONTCONFIG_PATH", "FONTCONFIG_FILE",
                "ELECTRON_RUN_AS_NODE", "CHROME_DEVEL_SANDBOX"):
        env.pop(key, None)
    return env


def _ensure_chrome(port, profile, headless=False):
    """Ensure a long-lived Chrome with a CDP port is running. Returns True on
    success. Launches one (detached, with a sanitized env) if not already up."""
    global _chrome_proc
    with _chrome_lock:
        if _cdp_alive(port):
            return True
        binary = _find_chrome_binary()
        if not binary:
            return False
        try:
            os.makedirs(profile, exist_ok=True)
        except Exception:
            pass
        args = [
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--restore-last-session=false",
            "--disable-session-crashed-bubble",
            "about:blank",
        ]
        if headless:
            args.insert(1, "--headless=new")
        try:
            _chrome_proc = subprocess.Popen(
                args, env=_clean_chrome_env(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            print(f"[BrowserAgent] failed to launch Chrome: {e}")
            return False
        # Wait for the debugging endpoint to come up.
        for _ in range(48):
            if _cdp_alive(port):
                return True
            time.sleep(0.25)
        return False


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def playwright_available():
    """Return (ok, detail). Lazy import so the bridge never fails to load."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception as e:
        return False, f"playwright not installed: {e}"
    return True, "ok"


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
        "url": "",
        "title": "",
        "subgoal": "",
        "result": None,
        "error": None,
        "pending_action": None,      # action waiting for confirmation
        "pending_question": None,    # question waiting for user input
        "last_screenshot": None,
        "started": datetime.now(timezone.utc).isoformat(),
        "finished": None,
        "steps": [],                 # compact per-step log for status polling
        # threading primitives (never serialized)
        "_cancel": threading.Event(),
        "_gate": threading.Event(),  # set when a parked run may proceed
        "_decision": None,           # bool for confirm; str for input
        "_thread": None,
    }
    with _runs_lock:
        _runs[run_id] = rec
    return rec


def latest_screenshot_path(run_id):
    """Absolute path to the most recent screenshot PNG for a run, or None."""
    with _runs_lock:
        rec = _runs.get(run_id)
        shot = rec.get("last_screenshot") if rec else None
    if shot and os.path.isfile(shot):
        return shot
    return None


def public_status(run_id):
    """Serializable status snapshot, or None if unknown."""
    with _runs_lock:
        rec = _runs.get(run_id)
        if not rec:
            return None
        return {
            k: rec[k] for k in (
                "id", "goal", "status", "step", "url", "title", "subgoal",
                "result", "error", "pending_action", "pending_question",
                "last_screenshot", "started", "finished", "steps",
            )
        }


def cancel(run_id):
    with _runs_lock:
        rec = _runs.get(run_id)
    if not rec:
        return False
    rec["_cancel"].set()
    rec["_gate"].set()  # unblock if parked
    return True


def resolve(run_id, approve=True, text=""):
    """Resolve a parked run. For confirmation, approve gates a sensitive action.
    For an input request, text supplies the answer."""
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

_EXECUTOR_SYSTEM = (
    "You are the executor for a web browsing agent. You see a screenshot of a "
    f"Chromium viewport that is exactly {_VIEWPORT['width']}x{_VIEWPORT['height']} "
    "pixels, origin top-left. Decide the SINGLE next action to make progress on "
    "the current subgoal. Reply with ONE JSON object and nothing else.\n\n"
    "Schema (pick one action):\n"
    '  {"action":"click_ref","ref":"<e#>","reason":"<intent>"}   (PREFERRED: click an element from the list)\n'
    '  {"action":"type_ref","ref":"<e#>","text":"<text>","reason":"..."}   (focus an element and type into it)\n'
    '  {"action":"click","x":<int>,"y":<int>,"reason":"<intent>"}   (only when no matching ref exists)\n'
    '  {"action":"double_click","x":<int>,"y":<int>,"reason":"..."}\n'
    '  {"action":"type","text":"<text>","reason":"..."}   (types into the already-focused field)\n'
    '  {"action":"press","key":"<Enter|Tab|Escape|ArrowDown|...>","reason":"..."}\n'
    '  {"action":"scroll","dy":<int>,"reason":"..."}      (positive scrolls down)\n'
    '  {"action":"navigate","url":"<absolute url>","reason":"..."}\n'
    '  {"action":"wait","ms":<int>,"reason":"..."}\n'
    '  {"action":"ask","question":"<what you need from the user>"}\n'
    '  {"action":"done","summary":"<what was accomplished>"}\n\n'
    "You are given a numbered list of the page's interactive elements (refs like "
    "e0, e1, ...), including ones below the current view (marked 'offscreen'). "
    "ALWAYS prefer click_ref / type_ref using a ref from that list: it clicks the "
    "exact element and cannot miss, and it auto-scrolls offscreen elements into "
    "view first. Pixel click/type is a LAST RESORT only when no matching ref "
    "exists (e.g. a bare canvas). To open a product, click_ref its title link in "
    "the list (match by the product name). Do NOT guess pixel coordinates for a "
    "link or button that is present in the list.\n"
    "Rules: put the real intent of the action in reason (for example 'click the "
    "Add to Cart button') so it can be reviewed.\n"
    "VERIFY BEFORE REPEATING: after a click, look at the NEW screenshot before "
    "acting again. If the page changed as expected (e.g. an 'Added to Cart' "
    "confirmation, the item count went up, a cart panel appeared), move ON; do "
    "NOT click the same button again. Re-click only if the screenshot clearly "
    "shows nothing happened, and never the same spot more than twice.\n"
    "STOP WHEN DONE: once the goal is achieved (e.g. the item is in the cart), "
    "emit done immediately. Do NOT go back to searching or navigate away. If the "
    "user only asked to add to cart, do NOT proceed to checkout or buy.\n"
    "Emit done only when the goal is fully achieved. Emit ask when you are "
    "blocked or need information only the user has. Never output prose outside "
    "the JSON."
)


def _b64_png(data):
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _call_executor(api_key, model, goal, subgoal, history, url, title, png_bytes, dom_list=""):
    """Ask the vision model for the next action. Returns (action_dict, raw_text)."""
    import requests as _req

    hist_lines = []
    for h in history[-8:]:
        a = h.get("action", {})
        hist_lines.append(f"step {h.get('step')}: {json.dumps(a)} -> {h.get('result','')}")
    history_text = "\n".join(hist_lines) if hist_lines else "(none yet)"

    user_text = (
        f"GOAL: {goal}\n"
        f"CURRENT SUBGOAL: {subgoal or goal}\n"
        f"CURRENT URL: {url}\n"
        f"PAGE TITLE: {title}\n"
        f"INTERACTIVE ELEMENTS (prefer click_ref/type_ref by ref):\n{dom_list or '(none)'}\n\n"
        f"RECENT ACTIONS:\n{history_text}\n\n"
        "Return the next action JSON."
    )

    payload = {
        "model": model,
        "max_tokens": 400,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": _EXECUTOR_SYSTEM},
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
    """Extract the first JSON object from the model output and validate it."""
    text = raw.strip()
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

def _registrable(url):
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
    except Exception:
        host = ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _is_sensitive(action, element_text, current_url):
    # Only the final purchase commit is gated. Navigation, search, add-to-cart,
    # and sign-in are auto-approved so the agent flows without interruption.
    probe = " ".join(str(action.get(k, "")) for k in ("reason", "text", "question"))
    probe += " " + (element_text or "")
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
        path = os.path.join(_run_dir(run_id), "trajectory.jsonl")
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        print(f"[BrowserAgent] log write failed: {e}")


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _park(rec, status, **fields):
    """Park the run and block until resolved or cancelled. Returns the decision."""
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


def _element_text_at(page, x, y):
    try:
        return (page.evaluate(
            "([x,y]) => { const el = document.elementFromPoint(x,y);"
            " return el ? (el.innerText || el.value || el.getAttribute('aria-label') || '').slice(0,120) : ''; }",
            [x, y],
        ) or "").strip()
    except Exception:
        return ""


# JS that tags every visible interactive element with a stable data-eva-ref and
# returns a compact list [{ref, tag, role, text, x, y}] for the model to pick
# from. Refs let us click the exact element via a Playwright locator (DOM-precise)
# instead of guessing pixel coordinates from the screenshot.
_DOM_SNAPSHOT_JS = r"""
() => {
  // Clear refs from any previous snapshot first, so a ref string is never
  // attached to more than one element (which would make the locator match
  // multiple nodes and fail Playwright strict mode).
  document.querySelectorAll('[data-eva-ref]').forEach(el => el.removeAttribute('data-eva-ref'));
  const out = [];
  let n = 0;
  const sel = 'a,button,input,textarea,select,[role=button],[role=link],[role=tab],[role=menuitem],[onclick],summary,[contenteditable=true]';
  const nodes = document.querySelectorAll(sel);
  const vh = window.innerHeight;
  const scrollY = window.scrollY || window.pageYOffset || 0;
  for (const el of nodes) {
    if (n >= 200) break;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    // Include elements anywhere in the document, not just the current viewport:
    // product results are usually below the fold on load, and click_ref scrolls
    // the chosen element into view before clicking. Skip only far-offscreen
    // horizontal junk (hidden mega-menus positioned way off to the side).
    if (r.right < -50 || r.left > window.innerWidth + 50) continue;
    const disabled = el.disabled === true || el.getAttribute('aria-disabled') === 'true';
    let label = (el.innerText || el.value || el.getAttribute('aria-label') ||
                 el.getAttribute('placeholder') || el.getAttribute('title') ||
                 el.getAttribute('alt') || '').trim();
    // Fall back to a nested image's alt text (Amazon product links wrap an img
    // with no direct text of their own).
    if (!label) {
      const im = el.querySelector('img[alt]');
      if (im) label = (im.getAttribute('alt') || '').trim();
    }
    label = label.replace(/\s+/g, ' ').slice(0, 90);
    if (!label) continue;  // skip anonymous controls the model cannot identify
    const ref = 'e' + (n++);
    el.setAttribute('data-eva-ref', ref);
    const tag = el.tagName.toLowerCase();
    let kind = el.getAttribute('role') || tag;
    if (tag === 'input') kind = (el.getAttribute('type') || 'text');
    // Mark whether the element is currently on screen, so the model knows it may
    // need to scroll (click_ref will still scroll it into view automatically).
    const onScreen = (r.bottom > 0 && r.top < vh);
    out.push({
      ref, tag: kind,
      text: label,
      onscreen: onScreen || undefined,
      y: Math.round(r.top + scrollY),
    });
  }
  // Order top-to-bottom by document position so the list reads naturally.
  out.sort((a, b) => (a.y || 0) - (b.y || 0));
  return out;
}
"""


def _dom_snapshot(page):
    """Tag interactive elements and return a compact list for the model. Returns
    [] on any failure so the agent silently falls back to pure vision."""
    try:
        items = page.evaluate(_DOM_SNAPSHOT_JS)
        return items if isinstance(items, list) else []
    except Exception:
        return []


def _dom_list_text(items):
    """Render the element list for the prompt: 'e3 [button] Add to Cart'.
    Off-screen elements are marked so the model knows a scroll may be needed."""
    lines = []
    for it in items[:200]:
        ref = it.get("ref", "")
        tag = it.get("tag", "")
        txt = it.get("text", "")
        off = "" if it.get("onscreen") else " (offscreen)"
        lines.append(f"{ref} [{tag}]{off} {txt}".rstrip())
    return "\n".join(lines) if lines else "(no interactive elements detected)"


def _execute(page, action):
    """Run one action against the page. Returns a short result string."""
    kind = action["action"]
    if kind == "click_ref":
        ref = str(action.get("ref", "")).strip()
        if not re.fullmatch(r"e\d{1,3}", ref):
            return "error: invalid ref"
        # .first guards against any residual duplicate so a click never fails
        # Playwright strict mode if two nodes briefly share a ref.
        loc = page.locator(f"[data-eva-ref='{ref}']").first
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        loc.click(timeout=6000)
        return f"clicked {ref}"
    if kind == "type_ref":
        ref = str(action.get("ref", "")).strip()
        if not re.fullmatch(r"e\d{1,3}", ref):
            return "error: invalid ref"
        loc = page.locator(f"[data-eva-ref='{ref}']").first
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        loc.click(timeout=6000)
        try:
            loc.fill(str(action.get("text", "")), timeout=4000)
        except Exception:
            page.keyboard.type(str(action.get("text", "")), delay=20)
        return f"typed into {ref}"
    if kind in ("click", "double_click"):
        x, y = int(action.get("x", 0)), int(action.get("y", 0))
        if kind == "click":
            page.mouse.click(x, y)
        else:
            page.mouse.dblclick(x, y)
        return f"{kind} at ({x},{y})"
    if kind == "type":
        page.keyboard.type(str(action.get("text", "")), delay=20)
        return "typed text"
    if kind == "press":
        page.keyboard.press(str(action.get("key", "Enter")))
        return f"pressed {action.get('key')}"
    if kind == "scroll":
        dy = int(action.get("dy", 400))
        page.mouse.wheel(0, dy)
        return f"scrolled {dy}"
    if kind == "navigate":
        page.goto(action.get("url", ""), wait_until="domcontentloaded", timeout=30000)
        return f"navigated to {action.get('url')}"
    if kind == "wait":
        page.wait_for_timeout(min(int(action.get("ms", 500)), 5000))
        return "waited"
    return "noop"


def _worker(rec, api_key, vision_model, director, autonomy, max_steps, start_url, headless):
    from playwright.sync_api import sync_playwright

    run_id = rec["id"]
    history = rec["steps"]
    subgoal = ""
    try:
        with sync_playwright() as p:
            # Preferred: connect to a long-lived Chrome over CDP and open a NEW
            # TAB in its existing window. The window persists between runs (login
            # stays), the user can watch, and we avoid relaunching a browser each
            # time. Fall back to a persistent context, then an ephemeral browser.
            ctx = None              # persistent-context fallback handle
            browser = None          # ephemeral-launch fallback handle
            cdp = None              # CDP connection (leave Chrome running on close)
            page = None
            close_tab_only = False  # in CDP mode, close just our tab, not Chrome

            if _ensure_chrome(_CDP_PORT, _PROFILE_DIR, headless=headless):
                try:
                    cdp = p.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
                    context = cdp.contexts[0] if cdp.contexts else cdp.new_context()
                    # Reuse an existing blank/new-tab page if there is one (Chrome
                    # opens an about:blank home tab on launch); only open a fresh
                    # tab when none is reusable. Avoids leaving stray blank tabs.
                    page = None
                    for _pg in context.pages:
                        try:
                            u = _pg.url or ""
                        except Exception:
                            u = ""
                        if u in ("", "about:blank", "chrome://newtab/", "chrome://new-tab-page/"):
                            page = _pg
                            break
                    if page is None:
                        page = context.new_page()
                    close_tab_only = True
                    try:
                        page.set_viewport_size(_VIEWPORT)
                    except Exception:
                        pass
                except Exception as e:
                    print(f"[BrowserAgent] CDP connect failed: {e}")
                    cdp = None
                    page = None

            if page is None:
                try:
                    os.makedirs(_PROFILE_DIR, exist_ok=True)
                except Exception:
                    pass
                for _channel in ("chrome", None):
                    try:
                        ctx = p.chromium.launch_persistent_context(
                            _PROFILE_DIR,
                            channel=_channel,
                            headless=headless,
                            viewport=_VIEWPORT,
                            args=["--no-first-run", "--no-default-browser-check"],
                        )
                        break
                    except Exception as e:
                        print(f"[BrowserAgent] persistent context (channel={_channel}) failed: {e}")
                        ctx = None
                if ctx is not None:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    try:
                        page.set_viewport_size(_VIEWPORT)
                    except Exception:
                        pass
                else:
                    browser = p.chromium.launch(headless=headless)
                    page = browser.new_page(viewport=_VIEWPORT)

            page.goto(start_url or "about:blank", wait_until="domcontentloaded", timeout=30000)
            rec["status"] = "running"

            # Initial plan from the director (Opus), if wired.
            if director:
                try:
                    subgoal = director(rec["goal"], f"Just opened {page.url}. Page title: {page.title()}.") or ""
                except Exception as e:
                    print(f"[BrowserAgent] director error: {e}")
            rec["subgoal"] = subgoal

            step = 0
            while step < max_steps:
                if rec["_cancel"].is_set():
                    rec["status"] = "cancelled"
                    break

                rec["url"], rec["title"] = page.url, page.title()
                png = page.screenshot(type="png")
                shot_path = os.path.join(_run_dir(run_id), f"step_{step:02d}.png")
                try:
                    with open(shot_path, "wb") as f:
                        f.write(png)
                except Exception:
                    shot_path = None
                rec["last_screenshot"] = shot_path

                # DOM snapshot: tag interactive elements so the model can click by
                # ref (DOM-precise) instead of guessing pixels. Falls back to pure
                # vision if extraction fails.
                dom_items = _dom_snapshot(page)
                dom_list = _dom_list_text(dom_items)

                try:
                    action, raw = _call_executor(
                        api_key, vision_model, rec["goal"], subgoal,
                        history, rec["url"], rec["title"], png, dom_list,
                    )
                except Exception as e:
                    rec["status"] = "error"
                    rec["error"] = str(e)
                    break

                kind = action.get("action")

                if kind == "done":
                    rec["result"] = action.get("summary", "Task complete.")
                    rec["status"] = "done"
                    _record(rec, step, shot_path, subgoal, raw, action, "", "done")
                    break

                if kind == "ask":
                    answer = _park(rec, "awaiting_input",
                                   pending_question=action.get("question", "Need input."))
                    if rec["_cancel"].is_set():
                        rec["status"] = "cancelled"
                        break
                    subgoal = (subgoal + f"\nUser said: {answer}").strip()
                    rec["subgoal"] = subgoal
                    _record(rec, step, shot_path, subgoal, raw, action, "", "asked user")
                    step += 1
                    continue

                # Determine target element text for click actions (sensitivity + dataset value).
                element_text = ""
                if kind in ("click", "double_click"):
                    element_text = _element_text_at(page, int(action.get("x", 0)), int(action.get("y", 0)))
                elif kind in ("click_ref", "type_ref"):
                    # Use the label from the DOM snapshot for the chosen ref so the
                    # BUY-gate still sees the button text.
                    _ref = str(action.get("ref", ""))
                    for _it in dom_items:
                        if _it.get("ref") == _ref:
                            element_text = _it.get("text", "")
                            break

                sensitive = _is_sensitive(action, element_text, rec["url"])
                if sensitive and autonomy == "pause":
                    approved = _park(rec, "awaiting_confirmation", pending_action=action)
                    if rec["_cancel"].is_set():
                        rec["status"] = "cancelled"
                        break
                    if not approved:
                        _record(rec, step, shot_path, subgoal, raw, action, element_text, "declined")
                        rec["result"] = "Stopped: user declined a sensitive action."
                        rec["status"] = "done"
                        break

                try:
                    result = _execute(page, action)
                except Exception as e:
                    result = f"error: {e}"
                _record(rec, step, shot_path, subgoal, raw, action, element_text, result)

                # Loop guard with self-recovery: a vision agent often re-clicks
                # the same control because it cannot tell the click landed. On the
                # first repeat, inject a corrective hint so the model tries a
                # different element (prefer click_ref) or scrolls, instead of
                # grinding. Only after several repeats does it stop and ask.
                sig = json.dumps(action, sort_keys=True)
                if sig == rec.get("_last_sig"):
                    rec["_repeat"] = rec.get("_repeat", 0) + 1
                else:
                    rec["_repeat"] = 0
                    rec["_last_sig"] = sig

                if rec["_repeat"] == 1:
                    # Self-correct: tell the executor not to repeat and to pick a
                    # different element by ref next time.
                    hint = ("\nNOTE: the last action did not change the page. Do NOT "
                            "repeat the same click. Pick a DIFFERENT element from the "
                            "list using click_ref (e.g. the product title link or the "
                            "Add to Cart button), or scroll to reveal it.")
                    if hint not in subgoal:
                        subgoal = (subgoal + hint).strip()
                        rec["subgoal"] = subgoal
                elif rec["_repeat"] >= 3:
                    rec["_repeat"] = 0
                    rec["_last_sig"] = None
                    q = ("I'm stuck repeating the same step and it isn't changing the "
                         "page. Want me to keep trying, or should I do something else?")
                    answer = _park(rec, "awaiting_input", pending_question=q)
                    if rec["_cancel"].is_set():
                        rec["status"] = "cancelled"
                        break
                    subgoal = (subgoal + f"\nUser said: {answer}").strip()
                    rec["subgoal"] = subgoal

                page.wait_for_timeout(400)
                step += 1
                rec["step"] = step

                # Re-consult the director periodically.
                if director and step % _DIRECTOR_INTERVAL == 0:
                    try:
                        summary = (f"At {page.url} (title: {page.title()}). "
                                   f"Last action: {json.dumps(action)} -> {result}.")
                        new_sub = director(rec["goal"], summary)
                        if new_sub:
                            subgoal = new_sub
                            rec["subgoal"] = subgoal
                    except Exception as e:
                        print(f"[BrowserAgent] director error: {e}")

            else:
                rec["status"] = rec["status"] if rec["status"] in ("error", "cancelled") else "done"
                if rec["result"] is None:
                    rec["result"] = f"Reached step limit ({max_steps})."

            try:
                if cdp is not None:
                    # CDP mode: leave the result page OPEN on success so the user
                    # can see the outcome (e.g. the cart) and continue manually.
                    # Only close our tab when the run errored or was cancelled, so
                    # a failed attempt does not leave a stray tab behind.
                    if close_tab_only and page is not None and rec["status"] in ("error", "cancelled"):
                        try:
                            page.close()
                        except Exception:
                            pass
                    cdp.close()
                elif ctx is not None:
                    ctx.close()
                elif browser is not None:
                    browser.close()
            except Exception:
                pass
    except Exception as e:
        rec["status"] = "error"
        rec["error"] = str(e)
    finally:
        rec["finished"] = datetime.now(timezone.utc).isoformat()


def _record(rec, step, shot_path, subgoal, raw, action, element_text, result):
    entry = {
        "step": step,
        "ts": datetime.now(timezone.utc).isoformat(),
        "url": rec["url"],
        "title": rec["title"],
        "goal": rec["goal"],
        "subgoal": subgoal,
        "model_raw": raw[:1000] if isinstance(raw, str) else "",
        "action": action,
        "element_text": element_text,
        "result": result,
        "screenshot": shot_path,
    }
    rec["steps"].append({"step": step, "action": action, "result": result})
    _log_step(rec["id"], entry)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def start_run(goal, api_key, vision_model=None, director=None, autonomy="pause",
              max_steps=_MAX_STEPS_DEFAULT, start_url="", headless=False):
    """Launch a browser agent run in a background thread. Returns the run record's
    public status (including its id). Raises if Playwright or the key is missing."""
    ok, detail = playwright_available()
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
        args=(rec, api_key, vision_model, director, autonomy, max_steps, start_url, headless),
        daemon=True,
    )
    rec["_thread"] = t
    t.start()
    return public_status(rec["id"])
