"""Bridge domain: telemetry."""

import datetime
import hashlib
import json
import os
import re
import sys
import threading
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

# Retired debug-log path is truncated at startup for legacy cleanup only.
_DEBUG_LOG_PATH = _cfg.BRIDGE_DEBUG_LOG_PATH
_debug_log_file = None
_TELEMETRY_ENUMS = {
    "result": frozenset({
        "hit", "miss", "warm", "warm_failed", "evict", "emit", "suppressed",
        "ok", "error", "approve", "deny",
    }),
    "reason": frozenset({
        "below_min_salience", "quiet_hours", "rate_cap", "timeout",
        "protocol", "storage", "unavailable",
    }),
    "route": frozenset({
        "default", "internal-cognition", "acp-unavailable", "trivial",
        "local", "acp", "github-models", "lmstudio",
    }),
    "request_type": frozenset({
        "chat", "data", "weather", "news", "market", "search", "memory",
        "unknown",
    }),
    "stop_reason": frozenset({
        "end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled",
        "error",
    }),
    "channels": frozenset({"chat", "signal", "chat,signal", "signal,chat"}),
}


def _open_debug_log():
    """Revoke the retired free-form debug log without keeping it open."""
    global _debug_log_file
    try:
        _cfg.ensure_private_directory(os.path.dirname(_DEBUG_LOG_PATH))
        with _cfg.open_private_file(_DEBUG_LOG_PATH, "w"):
            pass
    except Exception:
        pass
    _debug_log_file = None


def _debug_log_write(line):
    """Legacy free-form durable logging is disabled."""
    return None

class _StdoutTee:
    """Mirror output to the original stream without persisting free-form text."""

    def __init__(self, original, is_stderr=False):
        self._orig = original
        self._is_stderr = is_stderr

    def write(self, s):
        try:
            self._orig.write(s)
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
    """Legacy free-form logging is disabled; use structured telemetry events."""
    return None



def _install_log_tee():
    """Route stdout/stderr through a non-persisting tee once (idempotent)."""
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
        event_name = str(event)
        if re.fullmatch(r"[a-z][a-z0-9_.-]{0,47}", event_name) is None:
            return
        record = {"ts": _to_utc_iso(_utc_now()), "event": event_name}
        for k, v in fields.items():
            if isinstance(v, bool) or isinstance(v, (int, float)) or v is None:
                record[k] = v
            elif (
                k in _TELEMETRY_ENUMS and isinstance(v, str)
                and v in _TELEMETRY_ENUMS[k]
            ):
                record[k] = v
            elif k in ("model", "model_used") and isinstance(v, str) and v:
                record[k + "_hash"] = hashlib.sha256(
                    v.encode("utf-8", errors="strict")
                ).hexdigest()
        with _st.telemetry_lock:
            _st.telemetry_ring.append(record)
            if len(_st.telemetry_ring) > _TELEMETRY_RING_MAX:
                del _st.telemetry_ring[:-_TELEMETRY_RING_MAX]
            try:
                _cfg.ensure_private_directory(os.path.dirname(_TELEMETRY_PATH))
                try:
                    with _cfg.open_private_file(_TELEMETRY_PATH, "rb") as existing:
                        if os.fstat(existing.fileno()).st_size >= _TELEMETRY_MAX_BYTES:
                            with _cfg.open_private_file(_TELEMETRY_PATH, "w"):
                                pass
                except FileNotFoundError:
                    pass
                with _cfg.open_private_file(
                    _TELEMETRY_PATH, "a", encoding="utf-8"
                ) as f:
                    f.write(json.dumps(record) + "\n")
            except (OSError, RuntimeError):
                pass
        # Compact stdout mirror for live tailing.
        kv = " ".join(f"{k}={record[k]}" for k in record if k not in ("ts", "event"))
        print(f"[Telemetry] {record['event']} {kv}".rstrip())
    except Exception:
        # Telemetry must never break the request path.
        pass



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
    for ev in events:
        name = ev.get("event", "?")
        counts[name] = counts.get(name, 0) + 1
        if name == "acp_pool":
            r = ev.get("result")
            if r in pool:
                pool[r] += 1
        elif name == "acp_prompt" and isinstance(ev.get("ms"), (int, float)):
            prompt_ms.append(ev["ms"])
        elif name == "aig_turn" and isinstance(ev.get("total_ms"), (int, float)):
            turn_ms.append(ev["total_ms"])
    pool_selects = pool["hit"] + pool["warm"]
    summary = {
        "event_counts": counts,
        "pool": dict(pool, hit_rate=(round(pool["hit"] / pool_selects, 3) if pool_selects else None)),
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


