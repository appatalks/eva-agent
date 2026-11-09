"""Bridge domain: cron."""

import datetime
import json
import os
import threading
import time
import uuid
from bridge import config as _cfg
from bridge import state as _st

_CRON_TASKS_PATH = os.path.join(os.path.expanduser("~/.config/eva-standalone"), "cron_tasks.json")
_NOTIFY_RING_MAX = _cfg.NOTIFY_RING_MAX

def _load_cron_tasks():
    # global statement removed — writes go to _st.*
    try:
        with open(_CRON_TASKS_PATH, "r") as f:
            _st.cron_tasks = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _st.cron_tasks = []
    interrupted = False
    for task in _st.cron_tasks:
        if task.get("last_status") == "running":
            task["last_status"] = "interrupted"
            task["last_error"] = "The application stopped before the result was recorded."
            interrupted = True
    if interrupted:
        _save_cron_tasks()



def _save_cron_tasks():
    os.makedirs(os.path.dirname(_CRON_TASKS_PATH), exist_ok=True)
    with open(_CRON_TASKS_PATH, "w") as f:
        json.dump(_st.cron_tasks, f, indent=2)



def _parse_cron_expr(expr):
    """Parse a 5-field cron expression into (minute, hour, dom, month, dow) tuples.
    Each field is a set of valid integers, or None for wildcard (*)."""
    parts = (expr or "").strip().split()
    if len(parts) != 5:
        return None, "expected 5 fields (minute hour dom month dow)"
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    parsed = []
    for i, (field, (lo, hi)) in enumerate(zip(parts, ranges)):
        if field == "*":
            parsed.append(None)
            continue
        vals = set()
        for segment in field.split(","):
            step_parts = segment.split("/", 1)
            range_part = step_parts[0]
            try:
                step = int(step_parts[1]) if len(step_parts) > 1 else 1
            except ValueError:
                return None, f"invalid step in field {i}"
            if step < 1:
                return None, f"invalid step in field {i}"
            if range_part == "*":
                vals.update(range(lo, hi + 1, step))
                continue
            dash = range_part.split("-", 1)
            try:
                a = int(dash[0])
                b = int(dash[1]) if len(dash) > 1 else a
            except ValueError:
                return None, f"invalid value in field {i}"
            if a < lo or b > hi or a > b:
                return None, f"out of range in field {i}"
            vals.update(range(a, b + 1, step))
        parsed.append(vals)
    return tuple(parsed), ""



def _cron_matches(parsed, dt):
    """Check if a datetime matches a parsed cron expression."""
    minute, hour, dom, month, dow = parsed
    if minute is not None and dt.minute not in minute:
        return False
    if hour is not None and dt.hour not in hour:
        return False
    if dom is not None and dt.day not in dom:
        return False
    if month is not None and dt.month not in month:
        return False
    if dow is not None:
        # Cron: 0=Sunday, Python weekday(): 0=Monday. Convert.
        cron_dow = (dt.weekday() + 1) % 7
        if cron_dow not in dow:
            return False
    return True



def _cron_next_run(expr, after_dt=None):
    """Compute the next run time within one Gregorian calendar cycle."""
    parsed, err = _parse_cron_expr(expr)
    if err or parsed is None:
        return None
    if after_dt is None:
        after_dt = datetime.datetime.now(datetime.timezone.utc)
    candidate = after_dt.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
    minutes, hours, dom, month, dow = parsed
    valid_hours = sorted(hours if hours is not None else range(24))
    valid_minutes = sorted(minutes if minutes is not None else range(60))
    day = candidate.replace(hour=0, minute=0)
    for _ in range(146097):
        if ((month is None or day.month in month) and
                (dom is None or day.day in dom) and
                (dow is None or (day.weekday() + 1) % 7 in dow)):
            for hour in valid_hours:
                for minute in valid_minutes:
                    result = day.replace(hour=hour, minute=minute)
                    if result >= candidate:
                        return result.isoformat()
        try:
            day += datetime.timedelta(days=1)
        except OverflowError:
            break
    return None



def _cron_tick():
    """Called from the background loop worker. Runs any due cron tasks."""
    if not _st.cron_tasks:
        return
    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()
    ran = []
    with _st.cron_lock:
        for task in _st.cron_tasks:
            if not task.get("enabled", True):
                continue
            parsed, err = _parse_cron_expr(task.get("schedule", ""))
            if err or parsed is None:
                continue
            if not _cron_matches(parsed, now):
                continue
            # Don't re-run within same minute
            last = task.get("last_attempt", "") or task.get("last_run", "")
            if last and last[:16] == now_iso[:16]:
                continue
            task["last_attempt"] = now_iso
            task["last_status"] = "running"
            ran.append(task)
        if ran:
            _save_cron_tasks()
    for task in ran:
        with _st.cron_lock:
            if task not in _st.cron_tasks:
                continue
            if not task.get("enabled", True):
                task["last_status"] = "skipped"
                task["last_error"] = "Disabled before execution."
                _save_cron_tasks()
                continue
        prompt = task.get("prompt", "")
        label = task.get("label", "cron task")
        print(f"[Cron] Running: {label}")
        try:
            _cron_execute_task(task["id"], prompt, label)
            status, error = "succeeded", ""
        except Exception as e:
            print(f"[Cron] Error running {label}: {e}")
            status, error = "failed", str(e)[:300]
        with _st.cron_lock:
            if status == "succeeded":
                task["last_run"] = now_iso
            task["last_status"] = status
            task["last_error"] = error
            task["next_run"] = _cron_next_run(task.get("schedule", ""), now)
            _save_cron_tasks()



def _cron_execute_task(task_id, prompt, label):
    """Execute a cron task by sending its prompt through ACP."""
    if not _st.acp_client or not _st.acp_client.alive:
        raise RuntimeError("ACP is unavailable.")
    result = _st.acp_client.prompt(
        f"[Scheduled task: {label}] {prompt}", conversation_id="cron:" + str(task_id),
        permission_mode="background_auto" if _st.bg_auto_actions else "interactive",
    )
    if not isinstance(result, dict) or result.get("error"):
        raise RuntimeError("The scheduled prompt failed.")
    if result.get("permission_cancelled"):
        if result.get("permission_reason") == "policy_blocked":
            raise RuntimeError("A scheduled action was blocked by the autonomy policy.")
        raise RuntimeError("The scheduled action was interrupted or not authorized.")
    if result.get("stop_reason", "end_turn") != "end_turn":
        raise RuntimeError("The scheduled prompt ended before completing its response.")
    text = str(result.get("text") or "").strip()
    if not text:
        raise RuntimeError("The scheduled prompt returned no response.")
    _push_notification(f"Cron: {label}", text[:500], channel="chat")



def _push_notification(title, body, channel="chat"):
    """Push an internal notification into the notification ring."""
    note = {
        "id": "n-" + uuid.uuid4().hex[:8],
        "title": title,
        "body": body[:500],
        "channel": channel,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "seen": False,
    }
    with _st.notify_lock:
        _st.notify_ring.append(note)
        if len(_st.notify_ring) > _NOTIFY_RING_MAX:
            del _st.notify_ring[:-_NOTIFY_RING_MAX]

# ---------------------------------------------------------------------------
# Subagent parallelism — spawn isolated ACP tasks that run concurrently
# ---------------------------------------------------------------------------
_st.subagent_tasks = _st.subagent_tasks
_st.subagent_lock = _st.subagent_lock
_SUBAGENT_MAX = 4


