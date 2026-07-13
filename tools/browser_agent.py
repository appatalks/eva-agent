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
import base64
import shutil
import threading
import urllib.parse
import uuid
from datetime import datetime, timezone

from bridge import config as _bridge_config
from bridge.public_egress_proxy import PublicEgressProxy
from bridge.sensitive import redact_credentials
from bridge.action_runs import (
    ActionRunValidationError,
    ActionRunCancelled,
    ActionRunTimeout,
    admit_action_run,
    begin_effect,
    begin_startup,
    bounded_text,
    cancel_run,
    effectful_action,
    finite_int,
    finish_effect,
    finish_startup,
    initialize_run,
    launch_spec,
    observation,
    open_gate,
    public_snapshot,
    public_url,
    resolve_gate,
    runtime_expired,
    run_bounded_call,
    set_postcondition_baseline,
    terminalize,
    typed_action_result,
    update_run,
    validate_public_url,
    sha256,
    strict_json_object,
    unknown_postcondition,
)

_TRAJ_DIR = os.path.expanduser("~/.config/eva-standalone/browser_trajectories")
# Dedicated, persistent Chrome profile for the agent. Logins (e.g. Amazon) made
# once in the agent window persist here across runs, so the agent is not a fresh
# unauthenticated session every time. Kept separate from the user's real Chrome
# profile so it can run alongside an already-open Chrome.
_PROFILE_DIR = os.path.expanduser("~/.config/eva-standalone/browser_profile")
_VIEWPORT = {"width": 1280, "height": 800}
_MAX_STEPS_DEFAULT = 25
_DIRECTOR_INTERVAL = 4  # re-consult the director every N executor steps
_ARTIFACT_TTL_SECONDS = 600

_ACTION_KINDS = {
    "click", "double_click", "click_ref", "type_ref", "scroll",
    "navigate", "wait", "done", "ask",
}

# run_id -> run record (see _new_run). Guarded by _runs_lock.
_runs = {}
_runs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def playwright_available():
    """Return (ok, detail). Lazy import so the bridge never fails to load."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        return False, "playwright is not installed or could not be loaded"
    return True, "ok"


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
    initialize_run(rec, "browser", autonomy, postcondition)
    with _runs_lock:
        _runs[run_id] = rec
    return rec


def latest_screenshot_path(run_id):
    """Absolute path to the most recent screenshot PNG for a run, or None."""
    with _runs_lock:
        rec = _runs.get(run_id)
        shot = rec.get("last_screenshot") if rec else None
    return shot if isinstance(shot, str) and shot else None


def public_status(run_id):
    """Serializable status snapshot, or None if unknown."""
    with _runs_lock:
        rec = _runs.get(run_id)
        if not rec:
            return None
    return public_snapshot(rec, (
        "id", "goal", "status", "step", "url", "title", "subgoal",
        "result", "error", "pending_question", "started", "finished", "steps",
    ))


def cancel(run_id):
    with _runs_lock:
        rec = _runs.get(run_id)
    if not rec:
        return False, "unknown_run"
    return cancel_run(rec)


def has_active_runs():
    with _runs_lock:
        return any(
            int(rec.get("_bounded_operations", 0)) > 0
            or (
                not rec.get("_terminalized")
                and rec.get("_thread") is not None
                and rec["_thread"].is_alive()
            )
            for rec in _runs.values()
        )


def resolve(run_id, *, gate_id, kind, decision=None, text=None):
    """Resolve one exact, unexpired approval/input gate at most once."""
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

_OPENAI_VISION_ENDPOINT = "https://api.openai.com/v1/chat/completions"

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


def _post_vision_request(requests_module, api_key, payload, *, endpoint=None):
    target = endpoint or _OPENAI_VISION_ENDPOINT
    if target != _OPENAI_VISION_ENDPOINT:
        raise ActionRunValidationError("vision endpoint is not allowed")
    session = requests_module.Session()
    session.trust_env = False
    try:
        response = session.post(
            target,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
            allow_redirects=False,
            verify=True,
        )
    finally:
        session.close()
    if 300 <= response.status_code < 400:
        raise RuntimeError("vision endpoint redirect was blocked")
    return response


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
    resp = _post_vision_request(_req, api_key, payload)
    if resp.status_code != 200:
        raise RuntimeError(
            f"vision model request failed (HTTP {resp.status_code})"
        )
    raw = resp.json()["choices"][0]["message"]["content"] or ""
    return _parse_action(raw), raw


def _parse_action(raw):
    """Parse one complete model JSON object and validate its closed schema."""
    text = raw.strip()
    if not text.startswith("{") or not text.endswith("}"):
        raise ActionRunValidationError("model returned no browser action JSON")
    action = strict_json_object(text)
    kind = action.get("action")
    schemas = {
        "click_ref": ({"action", "ref", "reason"}, {"action", "ref"}),
        "type_ref": ({"action", "ref", "text", "reason"}, {"action", "ref", "text"}),
        "click": ({"action", "x", "y", "reason"}, {"action", "x", "y"}),
        "double_click": ({"action", "x", "y", "reason"}, {"action", "x", "y"}),
        "scroll": ({"action", "dy", "reason"}, {"action", "dy"}),
        "navigate": ({"action", "url", "reason"}, {"action", "url"}),
        "wait": ({"action", "ms", "reason"}, {"action"}),
        "done": ({"action", "summary"}, {"action"}),
        "ask": ({"action", "question"}, {"action", "question"}),
    }
    if kind not in _ACTION_KINDS or kind not in schemas:
        raise ActionRunValidationError("model returned an unsupported browser action")
    allowed, required = schemas[kind]
    if set(action) - allowed or not required.issubset(action):
        raise ActionRunValidationError("model browser action fields are invalid")
    for field in ("reason", "ref", "text", "url", "summary", "question"):
        if field in action and not isinstance(action[field], str):
            raise ActionRunValidationError(f"model browser action {field} must be text")
    for field in ("x", "y", "dy", "ms"):
        if field in action and (
            isinstance(action[field], bool) or not isinstance(action[field], int)
        ):
            raise ActionRunValidationError(f"model browser action {field} must be an integer")
    if kind == "type_ref":
        _validate_type_effect_text(action["text"])
    return action


def _validate_type_effect_text(value):
    if not isinstance(value, str):
        raise ActionRunValidationError("type_ref text must be text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ActionRunValidationError("type_ref text must be valid UTF-8") from exc
    if "\x00" in value or len(value) > 2000:
        raise ActionRunValidationError("type_ref text is invalid or too long")
    if redact_credentials(value) != value:
        raise ActionRunValidationError(
            "credential-bearing type_ref text requires a separate secure input path"
        )
    return value


# ---------------------------------------------------------------------------
# Trajectory logging
# ---------------------------------------------------------------------------

def _run_dir(run_id):
    _bridge_config.ensure_private_directory(_TRAJ_DIR)
    d = os.path.join(_TRAJ_DIR, run_id)
    return _bridge_config.ensure_private_directory(d)


def _scavenge_artifacts(remove_all=False):
    cutoff = datetime.now(timezone.utc).timestamp() - _ARTIFACT_TTL_SECONDS
    try:
        return _bridge_config.scavenge_private_directories(
            _TRAJ_DIR, r"[0-9a-f]{16}", cutoff, remove_all=remove_all
        )
    except (OSError, _bridge_config.PrivateStorageError):
        return 0


def _log_step(run_id, record):
    try:
        path = os.path.join(_run_dir(run_id), "trajectory.jsonl")
        with _bridge_config.open_private_file(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        print("[BrowserAgent] trajectory write failed")


def _schedule_artifact_cleanup(rec):
    def cleanup():
        try:
            _bridge_config.remove_private_subdirectory(_TRAJ_DIR, rec["id"])
        except (FileNotFoundError, OSError, _bridge_config.PrivateStorageError):
            pass
        with rec["_record_lock"]:
            rec["last_screenshot"] = None

    timer = threading.Timer(_ARTIFACT_TTL_SECONDS, cleanup)
    timer.daemon = True
    timer.start()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _park_approval(rec, action, element_text="", binding=None):
    return open_gate(
        rec, "approval", action=action, element_text=element_text, binding=binding
    )


def _park_input(rec, question):
    return open_gate(rec, "input", question=question)


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
    const labels = Array.from(el.labels || []).map(label => label.innerText || '').join(' ');
    let label = (labels || el.getAttribute('aria-label') ||
                 el.getAttribute('placeholder') || el.getAttribute('title') ||
                 el.getAttribute('alt') || el.innerText || '').trim();
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


def _install_network_policy(context, rec):
    def enforce(route, request):
        url = request.url or ""
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme in ("about", "data", "blob"):
            route.continue_()
            return
        try:
            validate_public_url(url, "browser request", resolve_dns=False)
        except ActionRunValidationError as exc:
            update_run(rec, error=f"Browser network policy blocked a request: {exc}")
            route.abort("blockedbyclient")
            return
        route.continue_()

    context.route("**/*", enforce)


def _validate_page_destination(page):
    url = page.url or ""
    if url == "about:blank":
        return
    validate_public_url(url, "browser destination", resolve_dns=False)


def _approval_label(value):
    if not isinstance(value, str):
        raise ActionRunValidationError("browser target label must be text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ActionRunValidationError("browser target label is invalid") from exc
    if "\x00" in value or len(value) > 240:
        raise ActionRunValidationError("browser target label is too long")
    return value


_ELEMENT_FINGERPRINT_JS = """el => {
    const form = el.form || null;
    const rect = el.getBoundingClientRect();
    return {
        tag: (el.tagName || '').toLowerCase(),
        role: el.getAttribute('role') || '',
        type: el.getAttribute('type') || '',
        name: el.getAttribute('name') || '',
        placeholder: el.getAttribute('placeholder') || '',
        aria: el.getAttribute('aria-label') || '',
        label: Array.from(el.labels || []).map(label => label.innerText || '').join(' ').trim().slice(0, 240),
        text: (el.innerText || '').trim().slice(0, 240),
        href: el.href || '',
        target: el.getAttribute('target') || '',
        form_action: el.formAction || (form && form.action) || '',
        form_method: (el.formMethod || (form && form.method) || '').toLowerCase(),
        disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
        readonly: el.readOnly === true || el.getAttribute('aria-readonly') === 'true',
        checked: el.checked === true,
        selected: el.selected === true,
        contenteditable: el.isContentEditable === true,
        rect: [Math.round(rect.left), Math.round(rect.top),
               Math.round(rect.width), Math.round(rect.height)]
    };
}"""


def _browser_action_target(page, action):
    kind = action.get("action")
    binding = {
        "url_hash": sha256(page.url or ""),
        "frame_hash": sha256(getattr(page.main_frame, "url", "") or ""),
        "kind": kind,
        "target_valid": True,
    }
    label = ""
    handle = None
    if kind in ("click_ref", "type_ref"):
        ref = str(action.get("ref", ""))
        binding["ref"] = ref
        try:
            loc = page.locator(f"[data-eva-ref='{ref}']")
            if loc.count() != 1:
                raise ActionRunValidationError("browser ref is not unique")
            handle = loc.element_handle(timeout=1500)
            fingerprint = handle.evaluate(_ELEMENT_FINGERPRINT_JS) if handle else None
        except Exception:
            fingerprint = None
            handle = None
        binding["target_valid"] = isinstance(fingerprint, dict)
        if kind == "type_ref" and isinstance(fingerprint, dict):
            _validate_type_effect_text(action.get("text"))
            input_types = {
                "text", "email", "search", "tel", "url", "number", "date",
                "time", "datetime-local", "month", "week",
            }
            editable_tag = (
                fingerprint.get("tag") == "textarea"
                or (
                    fingerprint.get("tag") == "input"
                    and (fingerprint.get("type") or "text").lower() in input_types
                )
            )
            binding["target_valid"] = bool(
                not fingerprint.get("disabled")
                and not fingerprint.get("readonly")
                and (editable_tag or fingerprint.get("contenteditable"))
            )
        binding["target_hash"] = sha256(fingerprint or {})
        if isinstance(fingerprint, dict):
            label = (
                fingerprint.get("label") or fingerprint.get("text")
                or fingerprint.get("aria")
                or fingerprint.get("placeholder") or fingerprint.get("tag") or ""
            )
    elif kind in ("click", "double_click"):
        x, y = int(action.get("x", 0)), int(action.get("y", 0))
        try:
            js_handle = page.evaluate_handle(
                "([x,y]) => document.elementFromPoint(x,y)", [x, y]
            )
            handle = js_handle.as_element()
            fingerprint = handle.evaluate(_ELEMENT_FINGERPRINT_JS) if handle else None
        except Exception:
            fingerprint = None
            handle = None
        binding.update({
            "x": x, "y": y,
            "target_hash": sha256(fingerprint or {}),
            "target_valid": isinstance(fingerprint, dict),
        })
        if isinstance(fingerprint, dict):
            label = fingerprint.get("text") or fingerprint.get("aria") or fingerprint.get("tag") or ""
    elif kind == "navigate":
        destination = validate_public_url(
            action.get("url", ""), "action.url", resolve_dns=False
        )
        binding["destination_hash"] = sha256(destination)
        label = public_url(destination)
    return binding, _approval_label(label), handle


def _execute(page, action, target_handle=None):
    """Run one validated action against the page and return a typed result."""
    kind = action["action"]
    if kind == "click_ref":
        ref = str(action.get("ref", "")).strip()
        if not re.fullmatch(r"e\d{1,3}", ref):
            return typed_action_result("rejected", "invalid_ref", "Invalid page control reference.")
        # .first guards against any residual duplicate so a click never fails
        # Playwright strict mode if two nodes briefly share a ref.
        loc = target_handle
        if loc is None:
            return typed_action_result("rejected", "target_missing", "Approved target is unavailable.")
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        loc.click(timeout=6000)
        return typed_action_result("executed", "clicked_ref", f"Selected page control {ref}.")
    if kind == "type_ref":
        ref = str(action.get("ref", "")).strip()
        if not re.fullmatch(r"e\d{1,3}", ref):
            return typed_action_result("rejected", "invalid_ref", "Invalid page control reference.")
        text = _validate_type_effect_text(action.get("text"))
        loc = target_handle
        if loc is None:
            return typed_action_result("rejected", "target_missing", "Approved target is unavailable.")
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            loc.fill(text, timeout=4000)
        except Exception:
            return typed_action_result(
                "failed", "target_fill_failed",
                "The exact approved page control could not be filled.",
            )
        return typed_action_result("executed", "typed_ref", f"Typed redacted text into {ref}.")
    if kind in ("click", "double_click"):
        finite_int(action.get("x"), "x", 0, _VIEWPORT["width"] - 1)
        finite_int(action.get("y"), "y", 0, _VIEWPORT["height"] - 1)
        if target_handle is None:
            return typed_action_result("rejected", "target_missing", "Approved target is unavailable.")
        page.mouse.click(
            action["x"], action["y"],
            click_count=2 if kind == "double_click" else 1,
        )
        return typed_action_result("executed", kind, f"{kind.replace('_', ' ').title()} completed.")
    if kind == "scroll":
        dy = finite_int(action.get("dy", 400), "dy", -5000, 5000)
        page.mouse.wheel(0, dy)
        return typed_action_result("executed", "scrolled", "Scrolled the active page.")
    if kind == "navigate":
        url = validate_public_url(action.get("url"), "action.url", resolve_dns=False)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        _validate_page_destination(page)
        return typed_action_result("executed", "navigated", "Navigated to a validated public page.")
    if kind == "wait":
        ms = finite_int(action.get("ms", 500), "ms", 0, 5000)
        page.wait_for_timeout(ms)
        return typed_action_result("executed", "waited", "Waited for the page to settle.")
    return typed_action_result("rejected", "unsupported_action", "Unsupported browser action.")


def _verify_postcondition(rec, page, step):
    spec = rec.get("_postcondition")
    if not spec:
        return unknown_postcondition()
    try:
        if spec["type"] == "browser.url_match":
            if page.url == "about:blank":
                actual_origin = ""
                path = ""
            else:
                validate_public_url(
                    page.url, "browser postcondition URL", resolve_dns=False
                )
                parsed = urllib.parse.urlsplit(page.url)
                actual_origin = public_url(page.url)
                path = parsed.path or "/"
            facts = {"origin": actual_origin, "path": path}
            matched = actual_origin == spec["origin"] and facts["path"] == spec["path"]
            check = observation(
                "browser-url", "browser.url_match",
                "observed" if matched else "not_observed", facts, step,
            )
        else:
            locator = page.locator(spec["selector"])
            raw_count = int(locator.count())
            count = min(max(raw_count, 0), 1000)
            state = spec["state"]
            overflow = raw_count > 1000
            if state in ("visible", "hidden"):
                visibility_overflow = raw_count > 100
                visible_count = 0
                if not visibility_overflow:
                    for index in range(raw_count):
                        if locator.nth(index).is_visible():
                            visible_count += 1
                facts = {
                    "matched_count": count,
                    "count_overflow": overflow,
                    "visible_count": visible_count,
                    "visibility_overflow": visibility_overflow,
                }
                if visibility_overflow:
                    verdict = "unknown"
                    matched = False
                else:
                    matched = visible_count > 0 if state == "visible" else visible_count == 0
                    verdict = "observed" if matched else "not_observed"
            elif state == "count_equals":
                facts = {"matched_count": count, "count_overflow": overflow}
                matched = raw_count == spec["count"]
                verdict = "observed" if matched else "not_observed"
            else:
                if raw_count == 1:
                    text = bounded_text(locator.first.inner_text(timeout=3000), 8000)
                    observed_hash = sha256(text)
                else:
                    observed_hash = ""
                facts = {
                    "matched_count": count,
                    "count_overflow": overflow,
                    "text_hash": observed_hash,
                }
                matched = raw_count == 1 and observed_hash == spec["text_hash"]
                verdict = "observed" if matched else "not_observed"
            check = observation(
                "browser-element", "browser.element_state",
                verdict, facts, step,
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


def _finish_with_postcondition(rec, page, cause, model_summary=""):
    postcondition = _verify_postcondition(rec, page, rec.get("step", 0))
    verdict = postcondition["verdict"]
    if cause in ("step_limit", "timeout"):
        terminalize(
            rec, "aborted", "budget_exhausted" if cause == "step_limit" else "timed_out",
            cause, result="The browser run stopped before completion could be verified.",
            model_summary=model_summary, postcondition=postcondition,
        )
    elif verdict == "observed":
        terminalize(
            rec, "succeeded", "postcondition_observed", cause,
            result="Verified the requested browser postcondition.",
            model_summary=model_summary, postcondition=postcondition,
        )
    elif verdict == "not_observed":
        terminalize(
            rec, "failed", "postcondition_not_observed", cause,
            error="The requested browser postcondition was not observed.",
            model_summary=model_summary, postcondition=postcondition,
        )
    else:
        terminalize(
            rec, "indeterminate", "unverified_completion_claim", cause,
            result=(
                "The browser agent stopped after claiming completion, but the "
                "result was not independently verified."
            ),
            model_summary=model_summary, postcondition=postcondition,
        )


def _worker(rec, api_key, vision_model, director, max_steps, start_url, headless):
    from playwright.sync_api import sync_playwright

    run_id = rec["id"]
    history = rec["steps"]
    subgoal = ""
    proxy = None
    startup_lease = False
    try:
        _bridge_config.secure_private_tree(_PROFILE_DIR)
        _bridge_config.ensure_private_directory(_TRAJ_DIR)
        if not begin_startup(rec):
            raise ActionRunCancelled()
        startup_lease = True
        with sync_playwright() as p:
            # Each run owns an isolated context whose network stack is forced
            # through a DNS-pinning loopback proxy. Never attach to an existing
            # user browser: that would bypass the proxy and affect unrelated tabs.
            ctx = None
            browser = None
            page = None
            if rec["_cancel"].is_set():
                raise ActionRunCancelled()
            proxy = PublicEgressProxy().start()
            for _channel in ("chrome", None):
                if rec["_cancel"].is_set():
                    raise ActionRunCancelled()
                try:
                    ctx = p.chromium.launch_persistent_context(
                        _PROFILE_DIR,
                        channel=_channel,
                        headless=headless,
                        viewport=_VIEWPORT,
                        proxy={"server": proxy.url},
                        args=[
                            "--no-first-run", "--no-default-browser-check",
                            "--proxy-bypass-list=<-loopback>",
                            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                        ],
                    )
                    break
                except Exception:
                    print(f"[BrowserAgent] isolated context channel {_channel} failed")
                    ctx = None
            if ctx is not None:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            else:
                browser = p.chromium.launch(
                    headless=headless, proxy={"server": proxy.url},
                    args=[
                        "--proxy-bypass-list=<-loopback>",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    ],
                )
                page = browser.new_page(viewport=_VIEWPORT)

            _install_network_policy(page.context, rec)
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(30000)
            if rec["_cancel"].is_set():
                raise ActionRunCancelled()
            page.goto("about:blank", wait_until="domcontentloaded", timeout=30000)
            _validate_page_destination(page)
            if finish_startup(rec):
                startup_lease = False
                raise ActionRunCancelled()
            startup_lease = False
            update_run(rec, status="running")
            set_postcondition_baseline(rec, _verify_postcondition(rec, page, 0))

            if start_url:
                initial_action = {"action": "navigate", "url": start_url}
                binding, _label, _handle = _browser_action_target(
                    page, initial_action
                )
                decision = _park_approval(rec, initial_action, "", binding)
                if rec["_cancel"].is_set():
                    raise ActionRunCancelled()
                if decision.get("state") != "approved":
                    reason = (
                        "user_denied" if decision.get("state") == "denied"
                        else "approval_expired"
                    )
                    terminalize(
                        rec, "aborted", reason, "approval_gate",
                        result=(
                            "The browser run stopped before its initial "
                            "navigation was executed."
                        ),
                    )
                    return
                current_binding, _label, _handle = _browser_action_target(
                    page, decision.get("action") or initial_action
                )
                leased, lease_reason, execution_action = begin_effect(
                    rec, decision, current_binding
                )
                if not leased:
                    terminalize(
                        rec, "aborted", lease_reason, "execution_lease",
                        result="The approved initial navigation changed.",
                    )
                    return
                try:
                    initial_result = _execute(page, execution_action)
                    _validate_page_destination(page)
                except Exception:
                    initial_result = typed_action_result(
                        "failed", "action_exception",
                        "The initial browser navigation failed.",
                    )
                _finished, cancellation_pending = finish_effect(
                    rec, initial_result
                )
                if cancellation_pending:
                    raise ActionRunCancelled()
                if initial_result["state"] != "executed":
                    terminalize(
                        rec, "failed", initial_result["code"],
                        "initial_navigation", error=initial_result["summary"],
                    )
                    return

            # Initial plan from the director (Opus), if wired.
            if director:
                try:
                    subgoal = run_bounded_call(
                        rec,
                        lambda: director(
                            rec["goal"],
                            f"Just opened {public_url(page.url)}. Page title is redacted.",
                        ),
                        timeout_seconds=30,
                    ) or ""
                except (ActionRunCancelled, ActionRunTimeout):
                    raise
                except Exception:
                    print("[BrowserAgent] director request failed")
            update_run(rec, subgoal=subgoal)

            step = 0
            while step < max_steps:
                if rec["_cancel"].is_set():
                    terminalize(rec, "aborted", "user_cancelled", "cancel")
                    break
                if runtime_expired(rec):
                    _finish_with_postcondition(rec, page, "timeout")
                    break
                update_run(
                    rec, url=page.url,
                    title=bounded_text(page.title(), 160),
                )
                png = page.screenshot(type="png", timeout=5000)
                if rec["_cancel"].is_set():
                    raise ActionRunCancelled()
                if runtime_expired(rec):
                    raise ActionRunTimeout()
                shot_path = os.path.join(_run_dir(run_id), f"step_{step:02d}.png")
                try:
                    with _bridge_config.open_private_file(shot_path, "wb") as f:
                        f.write(png)
                except Exception:
                    shot_path = None
                update_run(rec, last_screenshot=shot_path)

                # DOM snapshot: tag interactive elements so the model can click by
                # ref (DOM-precise) instead of guessing pixels. Falls back to pure
                # vision if extraction fails.
                dom_items = _dom_snapshot(page)
                if rec["_cancel"].is_set():
                    raise ActionRunCancelled()
                if runtime_expired(rec):
                    raise ActionRunTimeout()
                dom_list = _dom_list_text(dom_items)

                try:
                    action, raw = run_bounded_call(
                        rec,
                        lambda: _call_executor(
                            api_key, vision_model, rec["goal"], subgoal,
                            history, public_url(rec["url"]), "", png, dom_list,
                        ),
                        timeout_seconds=65,
                    )
                except (ActionRunCancelled, ActionRunTimeout):
                    raise
                except Exception:
                    terminalize(
                        rec, "failed", "executor_call_failed", "model_error",
                        error="The browser vision executor request failed.",
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
                        rec, step, shot_path, subgoal, raw, action, "",
                        typed_action_result(
                            "skipped", "model_completion_claim",
                            "Model requested terminal verification."
                        ),
                    )
                    _finish_with_postcondition(rec, page, "model_done", claim)
                    break

                if kind == "ask":
                    decision = _park_input(
                        rec, action.get("question", "Need input.")
                    )
                    if rec["_cancel"].is_set():
                        terminalize(rec, "aborted", "user_cancelled", "cancel")
                        break
                    if runtime_expired(rec):
                        _finish_with_postcondition(rec, page, "timeout")
                        break
                    if decision.get("state") != "answered":
                        reason = (
                            "user_cancelled" if decision.get("state") == "cancelled"
                            else "approval_expired"
                        )
                        terminalize(
                            rec, "aborted", reason, "input_gate",
                            result="The browser run stopped because required input was not received."
                        )
                        break
                    answer = decision["text"]
                    subgoal = (subgoal + f"\nUser said: {answer}").strip()
                    update_run(rec, subgoal=subgoal)
                    _record(
                        rec, step, shot_path, subgoal, raw, action, "",
                        typed_action_result("executed", "input_received", "Received bounded user input."),
                    )
                    step += 1
                    continue

                element_text = ""
                execution_action = action
                effect_lease = False
                target_handle = None
                if effectful_action(action):
                    binding, element_text, initial_handle = _browser_action_target(
                        page, action
                    )
                    if initial_handle is not None:
                        try:
                            initial_handle.dispose()
                        except Exception:
                            pass
                    if not binding.get("target_valid", False):
                        terminalize(
                            rec, "failed", "target_not_attestable", "target_binding",
                            error="The browser target could not be bound safely.",
                        )
                        break
                    decision = _park_approval(rec, action, element_text, binding)
                    if rec["_cancel"].is_set():
                        terminalize(rec, "aborted", "user_cancelled", "cancel")
                        break
                    if runtime_expired(rec):
                        _finish_with_postcondition(rec, page, "timeout")
                        break
                    if decision.get("state") != "approved":
                        reason = (
                            "user_denied" if decision.get("state") == "denied"
                            else "approval_expired"
                        )
                        _record(
                            rec, step, shot_path, subgoal, raw, action, element_text,
                            typed_action_result("rejected", reason, "Action was not approved."),
                        )
                        terminalize(
                            rec, "aborted", reason, "approval_gate",
                            result="The browser run stopped before the action was executed."
                        )
                        break
                    current_binding, _current_label, target_handle = _browser_action_target(
                        page, decision.get("action") or action
                    )
                    if runtime_expired(rec):
                        _finish_with_postcondition(rec, page, "timeout")
                        break
                    leased, lease_reason, execution_action = begin_effect(
                        rec, decision, current_binding
                    )
                    if not leased:
                        terminalize(
                            rec, "aborted", lease_reason, "execution_lease",
                            result="The approved browser target changed before execution.",
                        )
                        break
                    effect_lease = True
                try:
                    result = _execute(page, execution_action, target_handle)
                    _validate_page_destination(page)
                except Exception:
                    result = typed_action_result(
                        "failed", "action_exception", "The browser action failed."
                    )
                finally:
                    if target_handle is not None:
                        try:
                            target_handle.dispose()
                        except Exception:
                            pass
                _record(
                    rec, step, shot_path, subgoal, raw, execution_action,
                    element_text, result,
                )
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

                # Loop guard with self-recovery: a vision agent often re-clicks
                # the same control because it cannot tell the click landed. On the
                # first repeat, inject a corrective hint so the model tries a
                # different element (prefer click_ref) or scrolls, instead of
                # grinding. Only after several repeats does it stop and ask.
                sig = json.dumps(execution_action, sort_keys=True)
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
                        update_run(rec, subgoal=subgoal)
                elif rec["_repeat"] >= 3:
                    rec["_repeat"] = 0
                    rec["_last_sig"] = None
                    q = ("I'm stuck repeating the same step and it isn't changing the "
                         "page. Want me to keep trying, or should I do something else?")
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
                    subgoal = (subgoal + f"\nUser said: {answer}").strip()
                    update_run(rec, subgoal=subgoal)

                page.wait_for_timeout(400)
                step += 1
                update_run(rec, step=step)

                # Re-consult the director periodically.
                if director and step % _DIRECTOR_INTERVAL == 0:
                    try:
                        summary = (
                            f"At {public_url(page.url)}. Last action kind: "
                            f"{action.get('action')} -> {result.get('state')}."
                        )
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
                        print("[BrowserAgent] director request failed")

            else:
                if not rec.get("_terminalized"):
                    update_run(rec, step=max_steps)
                    _finish_with_postcondition(rec, page, "step_limit")

            try:
                if ctx is not None:
                    ctx.close()
                elif browser is not None:
                    browser.close()
            except Exception:
                pass
    except ActionRunCancelled:
        terminalize(rec, "aborted", "user_cancelled", "cancel")
    except ActionRunTimeout:
        if 'page' in locals() and page is not None:
            _finish_with_postcondition(rec, page, "timeout")
        else:
            terminalize(rec, "aborted", "timed_out", "timeout")
    except Exception:
        terminalize(
            rec, "failed", "browser_runtime_error", "runtime_exception",
            error="The browser runtime failed.",
        )
    finally:
        if startup_lease:
            finish_startup(rec)
        if proxy is not None:
            proxy.close()
        if not rec.get("_terminalized"):
            if rec["_cancel"].is_set():
                terminalize(rec, "aborted", "user_cancelled", "cancel")
            else:
                terminalize(
                    rec, "indeterminate", "runtime_ended_without_outcome",
                    "runtime_exit",
                )
        _schedule_artifact_cleanup(rec)


def _record(rec, step, shot_path, subgoal, raw, action, element_text, result):
    def private_hash(value):
        return sha256({"salt": rec["_log_salt"], "value": value})
    try:
        with _bridge_config.open_private_file(shot_path, "rb") as handle:
            screenshot_hash = sha256(handle.read())
    except (OSError, TypeError, _bridge_config.PrivateStorageError):
        screenshot_hash = ""
    action_view = {
        "kind": action.get("action", "unknown"),
        "digest": private_hash(action),
    }
    entry = {
        "contract_version": "eva.action-step/1",
        "step": step,
        "ts": datetime.now(timezone.utc).isoformat(),
        "url": public_url(rec["url"]),
        "goal_hash": private_hash(rec["goal"]),
        "subgoal_hash": private_hash(subgoal or ""),
        "model_output_hash": private_hash(raw or ""),
        "action": action_view,
        "element_hash": private_hash(element_text or ""),
        "result": result,
        "screenshot_hash": screenshot_hash,
    }
    with rec["_record_lock"]:
        rec["steps"].append({"step": step, "action": action_view, "result": result})
    _log_step(rec["id"], entry)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def start_run(goal, api_key, vision_model=None, director=None, autonomy="pause",
              max_steps=_MAX_STEPS_DEFAULT, start_url="", headless=False,
              postcondition=None, use_director=None):
    """Launch a browser agent run in a background thread. Returns the run record's
    public status (including its id). Raises if Playwright or the key is missing."""
    raw_spec = {
        "goal": goal,
        "use_director": director is not None if use_director is None else use_director,
        "autonomy": autonomy,
        "max_steps": max_steps,
        "start_url": start_url,
        "headless": headless,
        "postcondition": postcondition,
    }
    if vision_model is not None:
        raw_spec["vision_model"] = vision_model
    spec = launch_spec("browser", raw_spec)
    goal = spec["goal"]
    requested_autonomy = spec["autonomy"]
    max_steps = spec["max_steps"]
    start_url = spec["start_url"]
    headless = spec["headless"]
    postcondition = spec["postcondition"]
    vision_model = spec["vision_model"]
    if start_url:
        validate_public_url(start_url, "start_url", resolve_dns=False)
    ok, detail = playwright_available()
    if not ok:
        raise RuntimeError(detail)
    if not api_key:
        raise RuntimeError("OpenAI API key required for the vision executor.")

    rec = _new_run(goal, requested_autonomy, postcondition)
    admit_action_run(rec)
    t = threading.Thread(
        target=_worker,
          args=(rec, api_key, vision_model, director, max_steps, start_url, headless),
        daemon=True,
    )
    rec["_thread"] = t
    try:
        t.start()
    except Exception:
        terminalize(rec, "failed", "worker_start_failed", "runtime_start")
        raise
    return public_status(rec["id"])
