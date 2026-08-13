"""Bridge domain: telemetry."""

import datetime
import json
import os
import sys
import threading
import time
from bridge import config as _cfg
from bridge import state as _st

_utc_now = _cfg.utc_now
_to_utc_iso = _cfg.to_utc_iso

_LOG_LINE_CAP = _cfg.LOG_LINE_CAP
_LOG_RING_MAX = _cfg.LOG_RING_MAX
_TELEMETRY_ENABLED = os.environ.get("EVA_TELEMETRY", "1") not in ("0", "false", "no")
_TELEMETRY_MAX_BYTES = _cfg.TELEMETRY_MAX_BYTES
_TELEMETRY_PATH = _cfg.TELEMETRY_PATH
_TELEMETRY_RING_MAX = _cfg.TELEMETRY_RING_MAX

# Debug log file: captures all bridge stdout to a rotating file.
# Path: ~/.config/eva-standalone/bridge_debug.log (gitignored via *.log)
_DEBUG_LOG_PATH = os.path.join(_cfg.EVA_CONFIG_DIR, "bridge_debug.log")
_DEBUG_LOG_MAX_BYTES = 10 * 1024 * 1024  # rotate at 10 MB
_debug_log_file = None
_debug_log_lock = threading.Lock()


def _open_debug_log():
    """Open (or rotate) the debug log file. Called once at startup."""
    global _debug_log_file
    try:
        os.makedirs(os.path.dirname(_DEBUG_LOG_PATH), exist_ok=True)
        # Rotate if too large
        if os.path.isfile(_DEBUG_LOG_PATH):
            try:
                size = os.path.getsize(_DEBUG_LOG_PATH)
                if size > _DEBUG_LOG_MAX_BYTES:
                    backup = _DEBUG_LOG_PATH + ".1"
                    if os.path.isfile(backup):
                        os.remove(backup)
                    os.rename(_DEBUG_LOG_PATH, backup)
            except OSError:
                pass
        _debug_log_file = open(_DEBUG_LOG_PATH, "a", encoding="utf-8", buffering=1)
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _debug_log_file.write(f"\n{'='*60}\n[{ts}] Bridge debug log started\n{'='*60}\n")
        _debug_log_file.flush()
    except Exception as e:
        print(f"[Bridge] Debug log unavailable: {e}")
        _debug_log_file = None


def _debug_log_write(line):
    """Write a line to the debug log file (thread-safe)."""
    if _debug_log_file is None:
        return
    try:
        with _debug_log_lock:
            ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:12]
            _debug_log_file.write(f"[{ts}] {line}\n")
    except Exception:
        pass

class _StdoutTee:
    """Wrap a stream so writes go to the original AND into the log ring.
    Buffers partial writes until a newline so ring entries are whole lines."""

    def __init__(self, original, is_stderr=False):
        self._orig = original
        self._buf = ""
        self._is_stderr = is_stderr

    def write(self, s):
        try:
            self._orig.write(s)
        except Exception:
            pass
        try:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if self._is_stderr:
                    _debug_log_write(f"[STDERR] {line}")
                else:
                    _log_ring_add(line)
        except Exception:
            pass

    def flush(self):
        try:
            self._orig.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._orig, name)



def _log_ring_add(line):
    # global statement removed — writes go to _st.*
    line = (line or "").rstrip()
    if not line:
        return
    _debug_log_write(line)
    if len(line) > _LOG_LINE_CAP:
        line = line[:_LOG_LINE_CAP] + "…"
    with _st.log_lock:
        _st.log_seq += 1
        _st.log_ring.append((_st.log_seq, line))
        if len(_st.log_ring) > _LOG_RING_MAX:
            del _st.log_ring[:-_LOG_RING_MAX]



def _install_log_tee():
    """Route stdout/stderr through the tee once (idempotent). Also opens the debug log."""
    _open_debug_log()
    if not isinstance(sys.stdout, _StdoutTee):
        sys.stdout = _StdoutTee(sys.stdout)
    if not isinstance(sys.stderr, _StdoutTee):
        sys.stderr = _StdoutTee(sys.stderr, is_stderr=True)



def _telemetry_clip(value, limit=120):
    """Clip a label/string field so telemetry never stores large or sensitive blobs."""
    if value is None:
        return None
    s = str(value)
    return s if len(s) <= limit else s[:limit] + "…"



def _telemetry_emit(event, **fields):
    """Record a telemetry event. Safe to call from any thread; never raises."""
    if not _TELEMETRY_ENABLED:
        return
    try:
        record = {"ts": _to_utc_iso(_utc_now()), "event": str(event)[:48]}
        for k, v in fields.items():
            if isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
                record[k] = v
            else:
                record[k] = _telemetry_clip(v)
        with _st.telemetry_lock:
            _st.telemetry_ring.append(record)
            if len(_st.telemetry_ring) > _TELEMETRY_RING_MAX:
                del _st.telemetry_ring[:-_TELEMETRY_RING_MAX]
            try:
                os.makedirs(os.path.dirname(_TELEMETRY_PATH), exist_ok=True)
                if (os.path.isfile(_TELEMETRY_PATH)
                        and os.path.getsize(_TELEMETRY_PATH) >= _TELEMETRY_MAX_BYTES):
                    try:
                        os.replace(_TELEMETRY_PATH, _TELEMETRY_PATH + ".1")
                    except OSError:
                        pass
                with open(_TELEMETRY_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
            except OSError:
                pass
        # Compact stdout mirror for live tailing.
        kv = " ".join(f"{k}={record[k]}" for k in record if k not in ("ts", "event"))
        print(f"[Telemetry] {record['event']} {kv}".rstrip())
    except Exception:
        # Telemetry must never break the request path.
        pass


def _verbose_debug_emit(event, **fields):
    """Emit opt-in structured diagnostics without content payloads."""
    if getattr(_st, "verbose_debug", False):
        _telemetry_emit("verbose_" + str(event)[:40], **fields)



def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac, 1)



def _telemetry_summarize(events):
    """Build lightweight aggregates from a list of event dicts."""
    counts = {}
    pool = {"hit": 0, "warm": 0, "evict": 0, "miss": 0}
    prompt_ms = []
    turn_ms = []
    memory_ms = []
    preflight_ms = []
    responder_ms = []
    stream_ttft_ms = []
    stream_completion_ms = []
    stream_total_ms = []
    pool_profiles = {}
    cache_kinds = {}
    context_used = []
    context_percent = []
    preflight_attempted = 0
    preflight_succeeded = 0
    for ev in events:
        name = ev.get("event", "?")
        counts[name] = counts.get(name, 0) + 1
        if name == "acp_pool":
            r = ev.get("result")
            if r in pool:
                pool[r] += 1
            profile = ev.get("tool_profile")
            if isinstance(profile, str) and profile:
                pool_profiles[profile] = pool_profiles.get(profile, 0) + 1
        elif name == "kusto_metadata_cache":
            kind = ev.get("kind")
            if isinstance(kind, str) and kind:
                bucket = cache_kinds.setdefault(kind, {"hit": 0, "miss": 0})
                bucket["hit" if ev.get("hit") is True else "miss"] += 1
        elif name == "acp_usage":
            if isinstance(ev.get("used"), (int, float)):
                context_used.append(ev["used"])
            if isinstance(ev.get("percent"), (int, float)):
                context_percent.append(ev["percent"])
        elif name == "acp_prompt" and isinstance(ev.get("ms"), (int, float)):
            prompt_ms.append(ev["ms"])
        elif name == "aig_turn" and isinstance(ev.get("total_ms"), (int, float)):
            turn_ms.append(ev["total_ms"])
            if isinstance(ev.get("memory_ms"), (int, float)):
                memory_ms.append(ev["memory_ms"])
            if ev.get("preflight_attempted") is True:
                preflight_attempted += 1
            if ev.get("preflight_succeeded") is True:
                preflight_succeeded += 1
            if ev.get("preflight_attempted") is True and isinstance(ev.get("preflight_ms"), (int, float)):
                preflight_ms.append(ev["preflight_ms"])
            if isinstance(ev.get("responder_ms"), (int, float)):
                responder_ms.append(ev["responder_ms"])
        elif name == "stream_turn":
            if isinstance(ev.get("ttft_ms"), (int, float)):
                stream_ttft_ms.append(ev["ttft_ms"])
            if isinstance(ev.get("completion_ms"), (int, float)):
                stream_completion_ms.append(ev["completion_ms"])
            if isinstance(ev.get("total_ms"), (int, float)):
                stream_total_ms.append(ev["total_ms"])
    pool_selects = pool["hit"] + pool["warm"]
    summary = {
        "event_counts": counts,
        "pool": dict(pool, hit_rate=(round(pool["hit"] / pool_selects, 3) if pool_selects else None)),
        "pool_profiles": pool_profiles,
        "kusto_metadata_cache": cache_kinds,
    }

    def _stats(vals):
        if not vals:
            return None
        sv = sorted(vals)
        return {
            "n": len(sv),
            "avg": round(sum(sv) / len(sv), 1),
            "p50": _percentile(sv, 50),
            "p95": _percentile(sv, 95),
            "max": sv[-1],
        }

    summary["acp_prompt_ms"] = _stats(prompt_ms)
    summary["aig_turn_ms"] = _stats(turn_ms)
    summary["aig_memory_ms"] = _stats(memory_ms)
    summary["aig_preflight_ms"] = _stats(preflight_ms)
    summary["aig_responder_ms"] = _stats(responder_ms)
    summary["stream_ttft_ms"] = _stats(stream_ttft_ms)
    summary["stream_completion_ms"] = _stats(stream_completion_ms)
    summary["stream_total_ms"] = _stats(stream_total_ms)
    summary["acp_context_used_tokens"] = _stats(context_used)
    summary["acp_context_percent"] = _stats(context_percent)
    summary["aig_preflight"] = {
        "turns": len(turn_ms),
        "attempted": preflight_attempted,
        "succeeded": preflight_succeeded,
        "attempt_rate": round(preflight_attempted / len(turn_ms), 3) if turn_ms else None,
        "success_rate": round(preflight_succeeded / preflight_attempted, 3) if preflight_attempted else None,
    }
    return summary


# ---------------------------------------------------------------------------
# Proactive alerts + notifications
# ---------------------------------------------------------------------------
# Two co-operating pieces:
#   1. A user-defined alert rules store (alerts.json). Each rule names something
#      the user wants watched (a topic, a company's filings, weather, a standing
#      research question). The background tick evaluates active rules through the
#      ACP agent and fires a notification when a rule trips, with per-rule
#      cooldown and content-hash dedup so the same finding is not repeated.
#   2. A notification queue (in-memory ring + JSONL) that the front end polls.
#      New notifications are surfaced as an Eva-authored chat message and, when
#      the rule asks for it, spoken aloud.
# Privacy: the rules file holds only labels and watch parameters the user typed;
# the notification log holds the finding text Eva produced (no keys/tokens). The
# telemetry mirror records only labels, counts, and decisions.

_ALERT_TYPES = _cfg.ALERT_TYPES
_ALERT_CHANNELS = _cfg.ALERT_CHANNELS
_NOTIFY_RING_MAX = _cfg.NOTIFY_RING_MAX
_NOTIFY_MAX_BYTES = _cfg.NOTIFY_MAX_BYTES
_NOTIFY_CRITICAL_SALIENCE = _cfg.NOTIFY_CRITICAL_SALIENCE
_st.alerts_lock = _st.alerts_lock
_st.notify_lock = _st.notify_lock
_st.notify_ring = _st.notify_ring

_DEFAULT_ALERT_SETTINGS = _cfg.DEFAULT_ALERT_SETTINGS


