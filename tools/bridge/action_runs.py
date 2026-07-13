"""Shared safety contract for browser and desktop action runs.

The action loop lifecycle remains backward-compatible (``status`` still reaches
``done``/``cancelled``/``error``), but success is represented only by a typed
``outcome`` backed by a trusted postcondition. Model completion claims are never
proof. This module deliberately has no Phase 3, provider, or tool dispatch path.
"""

import copy
import base64
import datetime
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import threading
import time
import unicodedata
import urllib.parse

from bridge.events import ValidationError as EventValidationError, canonical_json
from bridge.sensitive import redact_credentials


CONTRACT_VERSION = "eva.action-run/1"
OUTCOME_STATES = frozenset({"succeeded", "failed", "aborted", "indeterminate"})
POSTCONDITION_VERDICTS = frozenset({
    "observed", "not_observed", "unknown", "not_applicable",
})
TERMINAL_STATUSES = frozenset({"done", "cancelled", "error"})
GATE_TTL_SECONDS = 60
MAX_RUNTIME_SECONDS = 300

_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_APP_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_ORIGIN_RE = re.compile(
    r"^(https?)://([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?/?$",
    re.IGNORECASE | re.ASCII,
)
_FORBIDDEN_LAUNCH_TEXT_RE = re.compile(
    r"[\x00\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u061c\u200b-\u200f"
    r"\u2028-\u202e\u2060\u2066-\u2069\ufeff]"
)
_LAUNCH_NONCES = {}
_LAUNCH_NONCES_LOCK = threading.Lock()
_ACTIVE_RUN = None
_ACTIVE_RUN_LOCK = threading.Lock()


class ActionRunValidationError(ValueError):
    """A request violates the action-run contract."""


class ActionRunCancelled(RuntimeError):
    """A bounded operation was abandoned because the run was cancelled."""


class ActionRunTimeout(RuntimeError):
    """A bounded operation exceeded its per-call or run deadline."""


def _launch_text(value, field, limit, *, allow_empty):
    if not isinstance(value, str):
        raise ActionRunValidationError(f"{field} must be text")
    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ActionRunValidationError(
            "launch specification contains invalid Unicode"
        ) from exc
    if _FORBIDDEN_LAUNCH_TEXT_RE.search(normalized):
        raise ActionRunValidationError(
            "launch specification contains forbidden control text"
        )
    if not allow_empty and not normalized.strip():
        raise ActionRunValidationError(f"{field} is required")
    if len(normalized) > limit:
        raise ActionRunValidationError(f"{field} is too long")
    return normalized


def _exact_keys(value, expected):
    return isinstance(value, dict) and set(value) == set(expected)


def _normalize_postcondition_origin(value):
    text = _launch_text(
        value, "postcondition.origin", 2048, allow_empty=False
    )
    match = _ORIGIN_RE.fullmatch(text)
    if match is None:
        raise ActionRunValidationError(
            "postcondition.origin must contain only an HTTP(S) origin"
        )
    scheme, raw_host, raw_port = match.groups()
    host = raw_host.lower()
    if host.endswith("."):
        host = host[:-1]
    if (
        len(host) > 253
        or "." not in host
        or host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
        or re.fullmatch(r"[0-9.]+", host) is not None
    ):
        raise ActionRunValidationError("postcondition.origin host is invalid")
    labels = host.split(".")
    if any(
        not label
        or len(label) > 63
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
        for label in labels
    ):
        raise ActionRunValidationError("postcondition.origin host is invalid")
    port = int(raw_port) if raw_port is not None else None
    if port is not None and not 1 <= port <= 65535:
        raise ActionRunValidationError("postcondition.origin port is invalid")
    scheme = scheme.lower()
    default_port = 443 if scheme == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{host}{suffix}"


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def utc_iso(value=None):
    active = value or utc_now()
    return active.astimezone(datetime.timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def sha256(value):
    if not isinstance(value, (str, bytes)):
        value = canonical_json(value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def bounded_text(value, limit, *, allow_empty=True):
    if not isinstance(value, str):
        value = str(value or "")
    if "\x00" in value:
        raise ActionRunValidationError("text contains NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ActionRunValidationError("text must be valid UTF-8") from exc
    value = redact_credentials(value)
    if not allow_empty and not value.strip():
        raise ActionRunValidationError("text is required")
    return value[:limit]


def strict_json_object(value):
    if not isinstance(value, str):
        raise ActionRunValidationError("JSON payload must be text")

    def unique_object(pairs):
        output = {}
        for key, item in pairs:
            if key in output:
                raise ActionRunValidationError("JSON payload has duplicate members")
            output[key] = item
        return output

    def reject_constant(_value):
        raise ActionRunValidationError("JSON payload has a non-standard number")

    try:
        result = json.loads(
            value, object_pairs_hook=unique_object, parse_constant=reject_constant
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, ActionRunValidationError):
            raise
        raise ActionRunValidationError("JSON payload is invalid") from exc
    if not isinstance(result, dict):
        raise ActionRunValidationError("JSON payload must be an object")
    return result


def validate_autonomy(value):
    if not isinstance(value, str):
        raise ActionRunValidationError("autonomy must be a string")
    requested = value.strip().lower()
    if requested == "auto":
        raise ActionRunValidationError(
            "autonomy=auto is disabled until the capability broker is available"
        )
    if requested not in ("pause", "confirm_all"):
        raise ActionRunValidationError(
            "autonomy must be 'pause' or 'confirm_all'"
        )
    # Legacy pause and explicit confirm_all both use the fail-closed policy.
    return requested, "confirm_all"


def _normalize_origin(parsed):
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port
    default = 80 if scheme == "http" else 443
    return f"{scheme}://{host}" + (f":{port}" if port and port != default else "")


def is_public_unicast(address):
    if not isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return False
    if (
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_private
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None:
            return False
    return True


def _resolved_public_addresses(host, port):
    try:
        rows = socket.getaddrinfo(
            host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise ActionRunValidationError("host could not be resolved") from exc
    addresses = set()
    for row in rows:
        try:
            address = ipaddress.ip_address(row[4][0].split("%", 1)[0])
        except ValueError as exc:
            raise ActionRunValidationError("host resolved to an invalid address") from exc
        if not is_public_unicast(address):
            raise ActionRunValidationError("host resolved to a non-public address")
        addresses.add(address.compressed)
    if not addresses:
        raise ActionRunValidationError("host resolved to no addresses")
    return tuple(sorted(addresses))


def validate_public_url(value, field="url", *, resolve_dns=True):
    if not isinstance(value, str):
        raise ActionRunValidationError(f"{field} must be text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ActionRunValidationError(f"{field} must be valid UTF-8") from exc
    if not value or not value.strip() or value != value.strip() or len(value) > 2048:
        raise ActionRunValidationError(f"{field} is invalid")
    text = value
    try:
        parsed = urllib.parse.urlsplit(text)
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise ActionRunValidationError(f"{field} is invalid") from exc
    if parsed.scheme.lower() not in ("https", "http") or not parsed.hostname:
        raise ActionRunValidationError(f"{field} must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ActionRunValidationError(f"{field} must not contain credentials")
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if authority.endswith(":") or parsed.port == 0:
        raise ActionRunValidationError(f"{field} port is invalid")
    host = parsed.hostname.lower()
    if host.endswith("."):
        host = host[:-1]
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ActionRunValidationError(f"{field} host must be ASCII") from exc
    if re.fullmatch(r"[a-z0-9.-]+", host) is None:
        raise ActionRunValidationError(f"{field} host is invalid")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ActionRunValidationError(f"{field} must not target a local host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if re.fullmatch(r"[0-9a-fx:.]+", host, re.IGNORECASE):
            raise ActionRunValidationError(f"{field} contains an ambiguous numeric host")
        if "." not in host:
            raise ActionRunValidationError(f"{field} host must be fully qualified")
        if any(
            not label
            or len(label) > 63
            or re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label
            ) is None
            for label in host.split(".")
        ):
            raise ActionRunValidationError(f"{field} host is invalid")
    else:
        if not is_public_unicast(address):
            raise ActionRunValidationError(f"{field} must target a public address")
    if resolve_dns:
        try:
            _resolved_public_addresses(
                host,
                parsed.port if parsed.port is not None
                else (443 if parsed.scheme == "https" else 80),
            )
        except ActionRunValidationError as exc:
            raise ActionRunValidationError(f"{field} {exc}") from exc
    return text


def _normalize_launch_value(value):
    if isinstance(value, dict):
        return {
            str(key): _normalize_launch_value(value[key])
            for key in sorted(value)
        }
    if isinstance(value, list):
        return [_normalize_launch_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ActionRunValidationError("launch specification contains non-finite data")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ActionRunValidationError(
                "launch specification contains invalid Unicode"
            ) from exc
        return unicodedata.normalize("NFC", value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    raise ActionRunValidationError("launch specification contains unsupported data")


def launch_spec(agent, data):
    if agent not in ("browser", "desktop", "camera") or not isinstance(data, dict):
        raise ActionRunValidationError("invalid launch specification")

    if agent == "camera":
        if set(data) - {"question", "device", "launch_capability", "purpose"}:
            raise ActionRunValidationError("invalid camera launch specification")
        question = _launch_text(
            data.get("question"), "question", 1000, allow_empty=False
        )
        device = data.get("device")
        if isinstance(device, bool) or not isinstance(device, int) or not 0 <= device <= 32:
            raise ActionRunValidationError("camera device is invalid")
        return _normalize_launch_value({"question": question, "device": device})

    goal = _launch_text(data.get("goal"), "goal", 2000, allow_empty=False)
    default_vision_model = os.environ.get("OPENAI_VISION_MODEL") or "gpt-4o"
    raw_vision_model = data.get("vision_model", default_vision_model)
    if raw_vision_model == "":
        raw_vision_model = default_vision_model
    vision_model = _launch_text(
        raw_vision_model,
        "vision_model", 128, allow_empty=True
    )
    use_director = data.get("use_director", True)
    if not isinstance(use_director, bool):
        raise ActionRunValidationError("use_director must be boolean")
    autonomy = data.get("autonomy", "pause")
    if autonomy not in ("pause", "confirm_all"):
        raise ActionRunValidationError("autonomy must be pause or confirm_all")
    max_steps = data.get("max_steps", 25)
    if (
        isinstance(max_steps, bool)
        or not isinstance(max_steps, int)
        or not 1 <= max_steps <= 60
    ):
        raise ActionRunValidationError(
            "max_steps must be an integer between 1 and 60"
        )
    common = {
        "goal": goal,
        "vision_model": vision_model,
        "use_director": use_director,
        "autonomy": autonomy,
        "max_steps": max_steps,
        "postcondition": validate_postcondition(agent, data.get("postcondition")),
    }
    if agent == "browser":
        start_url = _launch_text(
            data.get("start_url", ""), "start_url", 2048, allow_empty=True
        )
        if start_url and start_url != start_url.strip():
            raise ActionRunValidationError(
                "start_url must not contain surrounding whitespace"
            )
        if start_url:
            validate_public_url(
                start_url, "start_url", resolve_dns=False
            )
            start_host = urllib.parse.urlsplit(start_url).hostname or ""
            try:
                ipaddress.ip_address(start_host)
            except ValueError:
                pass
            else:
                raise ActionRunValidationError(
                    "start_url host must be a public domain name"
                )
        headless = data.get("headless", False)
        if not isinstance(headless, bool):
            raise ActionRunValidationError("headless must be boolean")
        common.update({
            "start_url": start_url,
            "headless": headless,
        })
    return _normalize_launch_value(common)


def launch_spec_hash(agent, data):
    try:
        encoded = json.dumps(
            launch_spec(agent, data), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError) as exc:
        raise ActionRunValidationError(
            "launch specification could not be canonicalized"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def validate_launch_capability(token, agent, data, secret, *, now=None):
    if not isinstance(token, str) or token.count(".") != 1:
        raise ActionRunValidationError("launch capability is invalid")
    if not isinstance(secret, str) or not secret:
        raise ActionRunValidationError("launch capability authority is unavailable")
    encoded, signature = token.split(".", 1)
    expected = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    if not hmac.compare_digest(signature, expected):
        raise ActionRunValidationError("launch capability signature is invalid")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActionRunValidationError("launch capability payload is invalid") from exc
    required = {"version", "agent", "spec_hash", "nonce", "expires_at"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ActionRunValidationError("launch capability payload is invalid")
    timestamp = int(time.time() if now is None else now)
    if (
        payload.get("version") != 1
        or payload.get("agent") != agent
        or not isinstance(payload.get("expires_at"), int)
        or payload["expires_at"] <= timestamp
        or payload["expires_at"] > timestamp + 120
        or not isinstance(payload.get("nonce"), str)
        or _HEX32_RE.fullmatch(payload["nonce"]) is None
        or not isinstance(payload.get("spec_hash"), str)
        or _HEX64_RE.fullmatch(payload["spec_hash"]) is None
        or payload["spec_hash"] != launch_spec_hash(agent, data)
    ):
        raise ActionRunValidationError("launch capability does not match this run")
    with _LAUNCH_NONCES_LOCK:
        expired = [nonce for nonce, expiry in _LAUNCH_NONCES.items() if expiry <= timestamp]
        for nonce in expired:
            del _LAUNCH_NONCES[nonce]
        if payload["nonce"] in _LAUNCH_NONCES:
            raise ActionRunValidationError("launch capability was already consumed")
        if len(_LAUNCH_NONCES) >= 4096:
            raise ActionRunValidationError("launch capability replay cache is full")
        _LAUNCH_NONCES[payload["nonce"]] = payload["expires_at"]
    return True


def public_url(value):
    """Return only a URL origin; paths, queries, and fragments stay private."""
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(str(value))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return ""
        return _normalize_origin(parsed)
    except (TypeError, ValueError):
        return ""


def validate_postcondition(agent, raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ActionRunValidationError("postcondition must be an object")
    kind = raw.get("type")
    if agent == "browser" and kind == "browser.url_match":
        if not _exact_keys(raw, {"type", "origin", "path"}):
            raise ActionRunValidationError("invalid URL postcondition")
        origin = _normalize_postcondition_origin(raw.get("origin"))
        path = _launch_text(
            raw.get("path"), "postcondition.path", 512, allow_empty=False
        )
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ActionRunValidationError("postcondition.path must be an absolute path")
        return {"type": kind, "origin": origin, "path": path}
    if agent == "browser" and kind == "browser.element_state":
        state = raw.get("state")
        expected = (
            {"type", "selector", "state", "count"}
            if state == "count_equals" else
            {"type", "selector", "state", "text_hash"}
            if state == "text_hash_equals" else
            {"type", "selector", "state"}
        )
        if (
            state not in ("visible", "hidden", "count_equals", "text_hash_equals")
            or not _exact_keys(raw, expected)
        ):
            raise ActionRunValidationError("invalid element postcondition")
        selector = _launch_text(
            raw.get("selector"), "postcondition.selector", 256,
            allow_empty=False,
        )
        output = {"type": kind, "selector": selector, "state": state}
        if state == "count_equals":
            count = raw.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 1000:
                raise ActionRunValidationError("postcondition.count is invalid")
            output["count"] = count
        elif state == "text_hash_equals":
            digest = raw.get("text_hash")
            if not isinstance(digest, str) or _HEX64_RE.fullmatch(digest) is None:
                raise ActionRunValidationError("postcondition.text_hash is invalid")
            output["text_hash"] = digest
        return output
    if agent == "desktop" and kind == "desktop.process_spawned":
        if not _exact_keys(raw, {"type", "executable", "state"}):
            raise ActionRunValidationError("invalid process postcondition")
        executable = _launch_text(
            raw.get("executable"), "postcondition.executable", 64,
            allow_empty=False,
        )
        if _APP_RE.fullmatch(executable) is None:
            raise ActionRunValidationError("postcondition.executable is invalid")
        state = raw.get("state")
        if state != "started":
            raise ActionRunValidationError("only desktop process state 'started' is supported")
        return {
            "type": kind, "executable": executable.lower(), "state": state
        }
    raise ActionRunValidationError("unsupported postcondition type")


def unknown_postcondition(spec=None, *, verdict="unknown", checks=None):
    if verdict not in POSTCONDITION_VERDICTS:
        raise ActionRunValidationError("invalid postcondition verdict")
    return {
        "verdict": verdict,
        "spec_source": "request" if spec else "none",
        "verified_by": "tool" if checks else "none",
        "spec_hash": sha256(spec) if spec else "",
        "checks": checks or [],
    }


def observation(check_id, kind, verdict, facts, step):
    if verdict not in POSTCONDITION_VERDICTS:
        raise ActionRunValidationError("invalid observation verdict")
    safe_facts = redact_credentials(copy.deepcopy(facts or {}))
    evidence = {
        "kind": kind,
        "source": "tool",
        "captured_at": utc_iso(),
        "step": int(step),
        "facts": safe_facts,
    }
    evidence["digest"] = sha256(evidence)
    return {
        "check_id": bounded_text(check_id, 64, allow_empty=False),
        "type": kind,
        "verdict": verdict,
        "evidence": [evidence],
    }


def action_digest(action):
    _normalized, canonical = canonical_effect_object(
        action if isinstance(action, dict) else {}, "action"
    )
    return sha256(canonical)


def canonical_effect_object(value, field):
    """Return one normalized object and the exact canonical JSON representing it."""
    if not isinstance(value, dict):
        raise ActionRunValidationError(f"{field} must be an object")
    try:
        canonical = canonical_json(value)
        normalized = json.loads(canonical)
    except (EventValidationError, TypeError, ValueError, RecursionError) as exc:
        raise ActionRunValidationError(f"{field} is not canonicalizable") from exc
    if not isinstance(normalized, dict) or len(canonical) > 16384:
        raise ActionRunValidationError(f"{field} is invalid or too large")
    return normalized, canonical


def action_description(agent, action, element_text="", binding=None):
    """Return an exact escaped approval display with no material truncation."""
    if not isinstance(action, dict) or not isinstance(binding, dict):
        raise ActionRunValidationError("approval effect or binding is invalid")
    try:
        _normalized_action, effect_json = canonical_effect_object(action, "action")
        _normalized_binding, binding_json = canonical_effect_object(
            binding, "binding"
        )
        label_json = json.dumps(
            unicodedata.normalize("NFC", str(element_text or "")),
            ensure_ascii=True, allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ActionRunValidationError("approval display is invalid") from exc
    description = (
        f"execute this exact frozen {agent} effect:\n"
        f"Effect: {effect_json}\n"
        f"Effect SHA-256: {sha256(effect_json)}\n"
        f"Target label: {label_json}\n"
        f"Binding: {binding_json}\n"
        f"Binding SHA-256: {sha256(binding_json)}"
    )
    if len(description) > 16384:
        raise ActionRunValidationError("approval display is too large")
    return description


def typed_action_result(state, code, summary):
    if state not in ("executed", "failed", "rejected", "skipped"):
        raise ActionRunValidationError("invalid action result state")
    return {
        "state": state,
        "code": bounded_text(code, 64, allow_empty=False),
        "summary": bounded_text(summary, 240, allow_empty=False),
    }


def initialize_run(rec, agent, requested_autonomy, postcondition=None):
    requested, effective = validate_autonomy(requested_autonomy)
    spec = validate_postcondition(agent, postcondition)
    rec.update({
        "contract_version": CONTRACT_VERSION,
        "agent": agent,
        "requested_autonomy": requested,
        "effective_autonomy": effective,
        "outcome": None,
        "approval_request": None,
        "_postcondition": spec,
        "_record_lock": rec.get("_record_lock") or threading.RLock(),
        "_terminalized": False,
        "_started_monotonic": time.monotonic(),
        "_gate_state": None,
        "_startup_in_flight": False,
        "_effect_in_flight": False,
        "_effect_count": 0,
        "_active_effect": None,
        "_effect_receipts": [],
        "_approved_effects": {},
        "_bounded_operations": 0,
        "_baseline_postcondition": None,
        "_log_salt": secrets.token_hex(16),
    })
    return rec


def admit_action_run(rec):
    global _ACTIVE_RUN
    identity = (rec.get("agent"), rec.get("id"))
    with _ACTIVE_RUN_LOCK:
        if _ACTIVE_RUN is not None and _ACTIVE_RUN != identity:
            raise ActionRunValidationError("another action-plane run is still active")
        _ACTIVE_RUN = identity


def release_action_run(rec):
    global _ACTIVE_RUN
    identity = (rec.get("agent"), rec.get("id"))
    with _ACTIVE_RUN_LOCK:
        if _ACTIVE_RUN == identity:
            _ACTIVE_RUN = None


def update_run(rec, **fields):
    with rec["_record_lock"]:
        if rec.get("_terminalized"):
            return False
        rec.update(fields)
        return True


def runtime_expired(rec):
    return time.monotonic() - rec.get("_started_monotonic", time.monotonic()) >= MAX_RUNTIME_SECONDS


def remaining_runtime(rec):
    elapsed = time.monotonic() - rec.get("_started_monotonic", time.monotonic())
    return max(0.0, MAX_RUNTIME_SECONDS - elapsed)


def run_bounded_call(rec, callback, *, timeout_seconds):
    if rec["_cancel"].is_set():
        raise ActionRunCancelled()
    remaining = remaining_runtime(rec)
    if remaining <= 0:
        raise ActionRunTimeout()
    timeout = min(float(timeout_seconds), remaining)
    complete = threading.Event()
    box = {}

    with rec["_record_lock"]:
        rec["_bounded_operations"] = int(
            rec.get("_bounded_operations", 0)
        ) + 1

    def invoke():
        try:
            box["value"] = callback()
        except BaseException as exc:  # propagate on the owning worker only
            box["error"] = exc
        finally:
            with rec["_record_lock"]:
                rec["_bounded_operations"] = max(
                    0, int(rec.get("_bounded_operations", 1)) - 1
                )
            complete.set()

    thread = threading.Thread(target=invoke, name="eva-bounded-operation", daemon=True)
    try:
        thread.start()
    except Exception:
        with rec["_record_lock"]:
            rec["_bounded_operations"] = max(
                0, int(rec.get("_bounded_operations", 1)) - 1
            )
        raise
    deadline = time.monotonic() + timeout
    while not complete.wait(0.05):
        if rec["_cancel"].is_set():
            raise ActionRunCancelled()
        if time.monotonic() >= deadline or remaining_runtime(rec) <= 0:
            raise ActionRunTimeout()
    if rec["_cancel"].is_set():
        raise ActionRunCancelled()
    if remaining_runtime(rec) <= 0:
        raise ActionRunTimeout()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def effectful_action(action):
    kind = action.get("action") if isinstance(action, dict) else None
    return kind not in (None, "ask", "done", "wait")


def open_gate(
    rec, kind, *, action=None, question="", element_text="", binding=None
):
    if kind not in ("approval", "input"):
        raise ActionRunValidationError("invalid gate kind")
    ttl = min(float(GATE_TTL_SECONDS), remaining_runtime(rec))
    if ttl <= 0:
        return {"state": "expired"}
    gate_id = secrets.token_hex(16)
    expires_monotonic = time.monotonic() + ttl
    expires_at = utc_iso(utc_now() + datetime.timedelta(seconds=ttl))
    if kind == "approval":
        frozen_action, action_json = canonical_effect_object(
            action if isinstance(action, dict) else {}, "action"
        )
        frozen_binding, binding_json = canonical_effect_object(
            binding if isinstance(binding, dict) else {}, "binding"
        )
        digest = sha256(action_json)
        binding_digest = sha256(binding_json)
        description = action_description(
            rec["agent"], frozen_action, element_text, frozen_binding
        )
        status = "awaiting_confirmation"
    else:
        safe_question = bounded_text(question or "Input required.", 240, allow_empty=False)
        digest = sha256({"kind": "input", "question": safe_question})
        binding_digest = ""
        frozen_action = None
        frozen_binding = None
        description = safe_question
        status = "awaiting_input"
    gate = {
        "gate_id": gate_id,
        "kind": kind,
        "action_digest": digest,
        "binding_digest": binding_digest,
        "description": description,
        "expires_at": expires_at,
        "_expires_monotonic": expires_monotonic,
        "_consumed": False,
        "_decision": None,
        "_action": frozen_action,
        "_binding": frozen_binding,
        "_approval_token": secrets.token_hex(32) if kind == "approval" else "",
    }
    with rec["_record_lock"]:
        if rec.get("_terminalized") or rec["_cancel"].is_set():
            return {"state": "cancelled"}
        rec["_gate"].clear()
        rec["_gate_state"] = gate
        rec["approval_request"] = {
            key: gate[key] for key in (
                "gate_id", "kind", "action_digest", "description", "expires_at"
            )
        }
        rec["approval_request"]["binding_digest"] = binding_digest
        rec["status"] = status
    signaled = rec["_gate"].wait(ttl)
    with rec["_record_lock"]:
        active = rec.get("_gate_state")
        if rec["_cancel"].is_set():
            decision = {"state": "cancelled"}
        elif not signaled or active is not gate or not gate.get("_consumed"):
            decision = {"state": "expired"}
            gate["_consumed"] = True
        else:
            decision = gate.get("_decision") or {"state": "expired"}
            if decision.get("state") == "approved":
                approval_token = gate["_approval_token"]
                rec["_approved_effects"][approval_token] = {
                    "action_digest": gate["action_digest"],
                    "binding_digest": gate["binding_digest"],
                }
                decision = {
                    **decision,
                    "action": copy.deepcopy(gate["_action"]),
                    "action_digest": gate["action_digest"],
                    "binding": copy.deepcopy(gate["_binding"]),
                    "binding_digest": gate["binding_digest"],
                    "_approval_token": approval_token,
                }
        if rec.get("_gate_state") is gate:
            rec["_gate_state"] = None
            rec["approval_request"] = None
            if not rec.get("_terminalized"):
                rec["status"] = "running"
    return decision


def resolve_gate(rec, *, gate_id, kind, decision=None, text=None):
    if not isinstance(gate_id, str) or _HEX32_RE.fullmatch(gate_id) is None:
        return False, "invalid_gate_id"
    with rec["_record_lock"]:
        gate = rec.get("_gate_state")
        if not gate or gate.get("gate_id") != gate_id or gate.get("kind") != kind:
            return False, "stale_gate"
        if gate.get("_consumed"):
            return False, "gate_already_consumed"
        if time.monotonic() > gate.get("_expires_monotonic", 0):
            gate["_consumed"] = True
            gate["_decision"] = {"state": "expired"}
            rec["_gate"].set()
            return False, "gate_expired"
        if kind == "approval":
            if decision not in ("approve", "deny") or text is not None:
                return False, "invalid_decision"
            gate["_decision"] = {
                "state": "approved" if decision == "approve" else "denied"
            }
        elif kind == "input":
            if decision == "cancel" and text is None:
                gate["_decision"] = {"state": "cancelled"}
            elif decision is None and isinstance(text, str):
                try:
                    safe = bounded_text(text.strip(), 1000, allow_empty=False)
                except ActionRunValidationError:
                    return False, "invalid_input"
                if safe != text.strip():
                    return False, "sensitive_input_rejected"
                gate["_decision"] = {"state": "answered", "text": safe}
            else:
                return False, "invalid_input"
        else:
            return False, "invalid_kind"
        gate["_consumed"] = True
        rec["_gate"].set()
        return True, "accepted"


def cancel_run(rec):
    with rec["_record_lock"]:
        if rec.get("_terminalized"):
            return False, "already_terminal"
        rec["_cancel"].set()
        gate = rec.get("_gate_state")
        if gate and not gate.get("_consumed"):
            gate["_consumed"] = True
            gate["_decision"] = {"state": "cancelled"}
        rec["_gate"].set()
        return True, (
            "cancellation_pending" if (
                rec.get("_effect_in_flight") or rec.get("_startup_in_flight")
            ) else "cancellation_accepted"
        )


def begin_startup(rec):
    """Reserve non-causal runtime setup so cancellation cannot overtake it."""
    with rec["_record_lock"]:
        if (
            rec.get("_terminalized") or rec["_cancel"].is_set()
            or runtime_expired(rec)
        ):
            return False
        if rec.get("_startup_in_flight") or rec.get("_effect_in_flight"):
            return False
        rec["_startup_in_flight"] = True
        return True


def finish_startup(rec):
    with rec["_record_lock"]:
        rec["_startup_in_flight"] = False
        return bool(rec["_cancel"].is_set())


def begin_effect(rec, approval, current_binding=None):
    if not isinstance(approval, dict) or approval.get("state") != "approved":
        return False, "not_approved", None
    approval_token = approval.get("_approval_token")
    if (
        not isinstance(approval_token, str)
        or re.fullmatch(r"[0-9a-f]{64}", approval_token) is None
    ):
        return False, "approval_consumed", None
    try:
        action, action_json = canonical_effect_object(
            approval.get("action"), "approved action"
        )
        binding, binding_json = canonical_effect_object(
            current_binding if isinstance(current_binding, dict) else {},
            "current binding",
        )
        parsed = True
    except ActionRunValidationError:
        action = None
        action_json = ""
        binding_json = ""
        parsed = False
    with rec["_record_lock"]:
        authorized = rec.get("_approved_effects", {}).pop(
            approval_token, None
        )
        if authorized is None:
            return False, "approval_consumed", None
        if (
            not parsed
            or authorized.get("action_digest") != approval.get("action_digest")
            or authorized.get("binding_digest") != approval.get("binding_digest")
            or sha256(action_json) != authorized.get("action_digest")
            or sha256(binding_json) != authorized.get("binding_digest")
        ):
            return False, "approved_target_changed", None
        if rec.get("_terminalized") or rec["_cancel"].is_set():
            return False, "cancelled", None
        if runtime_expired(rec):
            return False, "timed_out", None
        if rec.get("_effect_in_flight"):
            return False, "effect_already_in_flight", None
        rec["_effect_in_flight"] = True
        rec["_active_effect"] = {
            "sequence": len(rec.get("_effect_receipts", [])) + 1,
            "action_digest": approval["action_digest"],
            "binding_digest": approval["binding_digest"],
            "started_at": utc_iso(),
        }
        return True, "executing", action


def finish_effect(rec, result):
    with rec["_record_lock"]:
        if not rec.get("_effect_in_flight"):
            return False, bool(rec["_cancel"].is_set())
        rec["_effect_in_flight"] = False
        if isinstance(result, dict) and result.get("state") == "executed":
            rec["_effect_count"] = int(rec.get("_effect_count", 0)) + 1
            receipt = dict(rec.get("_active_effect") or {})
            receipt.update({
                "completed_at": utc_iso(),
                "result_digest": sha256(result),
            })
            receipt["receipt_digest"] = sha256(receipt)
            rec["_effect_receipts"].append(receipt)
        rec["_active_effect"] = None
        return True, bool(rec["_cancel"].is_set())


def set_postcondition_baseline(rec, postcondition):
    with rec["_record_lock"]:
        if rec.get("_baseline_postcondition") is None:
            rec["_baseline_postcondition"] = copy.deepcopy(postcondition)


def _facts_verdict(spec, facts):
    if not isinstance(facts, dict):
        return None
    kind = spec.get("type")
    if kind == "browser.url_match":
        if set(facts) != {"origin", "path"}:
            return None
        return "observed" if (
            facts["origin"] == spec["origin"] and facts["path"] == spec["path"]
        ) else "not_observed"
    if kind == "browser.element_state":
        state = spec.get("state")
        common = {"matched_count", "count_overflow"}
        if (
            isinstance(facts.get("matched_count"), bool)
            or not isinstance(facts.get("matched_count"), int)
            or not 0 <= facts["matched_count"] <= 1000
            or not isinstance(facts.get("count_overflow"), bool)
        ):
            return None
        if state in ("visible", "hidden"):
            if set(facts) != common | {"visible_count", "visibility_overflow"}:
                return None
            if (
                isinstance(facts["visible_count"], bool)
                or not isinstance(facts["visible_count"], int)
                or not 0 <= facts["visible_count"] <= 100
                or not isinstance(facts["visibility_overflow"], bool)
            ):
                return None
            if facts["visibility_overflow"] or facts["count_overflow"]:
                return "unknown"
            matched = (
                facts["visible_count"] > 0
                if state == "visible" else facts["visible_count"] == 0
            )
            return "observed" if matched else "not_observed"
        if state == "count_equals":
            if set(facts) != common:
                return None
            matched = (
                not facts["count_overflow"]
                and facts["matched_count"] == spec["count"]
            )
            return "observed" if matched else "not_observed"
        if state == "text_hash_equals":
            if set(facts) != common | {"text_hash"}:
                return None
            if not isinstance(facts["text_hash"], str):
                return None
            matched = (
                not facts["count_overflow"]
                and facts["matched_count"] == 1
                and facts["text_hash"] == spec["text_hash"]
            )
            return "observed" if matched else "not_observed"
        return None
    if kind == "desktop.process_spawned":
        if set(facts) != {"executable", "started", "pid"}:
            return None
        if (
            facts["executable"] != spec["executable"]
            or not isinstance(facts["started"], bool)
            or isinstance(facts["pid"], bool)
            or not isinstance(facts["pid"], int)
            or facts["pid"] < 0
        ):
            return None
        matched = facts["started"] and facts["pid"] > 0
        return "observed" if matched else "not_observed"
    return None


def _proof_evidence_times(postcondition, spec, verdict):
    if (
        not isinstance(postcondition, dict)
        or set(postcondition) != {
            "verdict", "spec_source", "verified_by", "spec_hash", "checks"
        }
        or postcondition.get("verdict") != verdict
        or postcondition.get("spec_source") != "request"
        or postcondition.get("verified_by") != "tool"
        or postcondition.get("spec_hash") != sha256(spec)
    ):
        return None
    checks = postcondition.get("checks")
    if not isinstance(checks, list) or len(checks) != 1:
        return None
    expected_check_id = {
        "browser.url_match": "browser-url",
        "browser.element_state": "browser-element",
        "desktop.process_spawned": "desktop-process",
    }.get(spec.get("type"))
    times = []
    for check in checks:
        if (
            not isinstance(check, dict)
            or set(check) != {"check_id", "type", "verdict", "evidence"}
            or check.get("check_id") != expected_check_id
            or check.get("type") != spec.get("type")
            or check.get("verdict") != verdict
        ):
            return None
        evidence_rows = check.get("evidence")
        if not isinstance(evidence_rows, list) or len(evidence_rows) != 1:
            return None
        for evidence in evidence_rows:
            if (
                not isinstance(evidence, dict)
                or set(evidence) != {
                    "kind", "source", "captured_at", "step", "facts", "digest"
                }
                or evidence.get("kind") != spec.get("type")
                or evidence.get("source") != "tool"
                or isinstance(evidence.get("step"), bool)
                or not isinstance(evidence.get("step"), int)
                or not 0 <= evidence["step"] <= 60
                or _facts_verdict(spec, evidence.get("facts")) != verdict
            ):
                return None
            digest = evidence.get("digest")
            logical = dict(evidence)
            logical.pop("digest", None)
            if not isinstance(digest, str) or digest != sha256(logical):
                return None
            captured = evidence.get("captured_at")
            if not isinstance(captured, str):
                return None
            try:
                parsed = datetime.datetime.fromisoformat(captured.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
                return None
            times.append(parsed)
    return times


def _valid_success_proof(rec, postcondition):
    spec = rec.get("_postcondition")
    baseline = rec.get("_baseline_postcondition") or {}
    if not isinstance(spec, dict):
        return False
    baseline_times = _proof_evidence_times(baseline, spec, "not_observed")
    final_times = _proof_evidence_times(postcondition, spec, "observed")
    receipts = rec.get("_effect_receipts")
    if not baseline_times or not final_times or not isinstance(receipts, list) or not receipts:
        return False
    previous_sequence = 0
    effect_times = []
    for receipt in receipts:
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {
                "sequence", "action_digest", "binding_digest", "started_at",
                "completed_at", "result_digest", "receipt_digest",
            }
            or any(
                not isinstance(receipt.get(field), str)
                or _HEX64_RE.fullmatch(receipt[field]) is None
                for field in (
                    "action_digest", "binding_digest", "result_digest",
                    "receipt_digest",
                )
            )
        ):
            return False
        digest = receipt.get("receipt_digest")
        logical = dict(receipt)
        logical.pop("receipt_digest", None)
        if digest != sha256(logical):
            return False
        sequence = receipt.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence != previous_sequence + 1:
            return False
        previous_sequence = sequence
        try:
            started = datetime.datetime.fromisoformat(
                receipt["started_at"].replace("Z", "+00:00")
            )
            completed = datetime.datetime.fromisoformat(
                receipt["completed_at"].replace("Z", "+00:00")
            )
        except (KeyError, AttributeError, ValueError):
            return False
        if (
            started.tzinfo is None or completed.tzinfo is None
            or started.utcoffset() != datetime.timedelta(0)
            or completed.utcoffset() != datetime.timedelta(0)
            or completed < started
            or (effect_times and effect_times[-1][1] > started)
        ):
            return False
        effect_times.append((started, completed))
    return (
        max(baseline_times) <= effect_times[0][0]
        and effect_times[-1][1] <= min(final_times)
        and int(rec.get("_effect_count", 0)) == len(receipts)
    )


def terminalize(
    rec, state, reason, cause, *, result="", error="", model_summary="",
    postcondition=None,
):
    if state not in OUTCOME_STATES:
        raise ActionRunValidationError("invalid outcome state")
    if postcondition is None:
        postcondition = unknown_postcondition(rec.get("_postcondition"))
    with rec["_record_lock"]:
        if rec.get("_terminalized"):
            return False
        if rec["_cancel"].is_set() and reason != "user_cancelled":
            state, reason, cause = "aborted", "user_cancelled", "cancel"
            postcondition = unknown_postcondition(rec.get("_postcondition"))
        elif runtime_expired(rec):
            state, reason, cause = "aborted", "timed_out", "timeout"
        elif state == "succeeded" and not _valid_success_proof(rec, postcondition):
            state = "indeterminate"
            reason = "success_proof_invalid"
            result = "The reported success did not satisfy the trusted proof contract."
        finished = utc_now()
        started = rec.get("started") or utc_iso(finished)
        try:
            started_dt = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
            duration_ms = max(0, min(86_400_000, int((finished - started_dt).total_seconds() * 1000)))
        except (TypeError, ValueError, OverflowError):
            duration_ms = 0
        outcome = {
            "state": state,
            "reason": bounded_text(reason, 64, allow_empty=False),
            "termination": {
                "cause": bounded_text(cause, 64, allow_empty=False),
                "step": int(rec.get("step", 0)),
            },
            "model_claim": {
                "summary_hash": sha256(bounded_text(model_summary, 300)),
            },
            "postcondition": copy.deepcopy(postcondition),
            "proof": {
                "baseline_verdict": (
                    rec.get("_baseline_postcondition") or {}
                ).get("verdict", "unknown"),
                "effect_count": int(rec.get("_effect_count", 0)),
                "effect_receipt_digests": [
                    receipt.get("receipt_digest", "")
                    for receipt in rec.get("_effect_receipts", [])
                ],
            },
            "started_at": started,
            "finished_at": utc_iso(finished),
            "duration_ms": duration_ms,
        }
        rec["outcome"] = outcome
        rec["finished"] = outcome["finished_at"]
        rec["_terminalized"] = True
        rec["approval_request"] = None
        rec["_gate_state"] = None
        safe_result = bounded_text(result, 500)
        safe_error = bounded_text(error, 500)
        if state == "succeeded":
            rec["status"] = "done"
            rec["result"] = safe_result or "Verified postcondition observed."
            rec["error"] = None
        elif state == "failed":
            rec["status"] = "error"
            rec["result"] = safe_result or None
            rec["error"] = safe_error or "The action run failed."
        elif state == "aborted":
            rec["status"] = "cancelled"
            rec["result"] = safe_result or "The action run stopped without completion."
            rec["error"] = None
        else:
            rec["status"] = "done"
            rec["result"] = safe_result or (
                "The agent stopped, but the requested result was not independently verified."
            )
            rec["error"] = None
        rec["pending_action"] = None
        rec["pending_question"] = None
        rec["_gate"].set()
        release_action_run(rec)
        return True


def public_snapshot(rec, fields):
    with rec["_record_lock"]:
        result = {key: copy.deepcopy(rec.get(key)) for key in fields}
        result.update({
            "contract_version": rec.get("contract_version", CONTRACT_VERSION),
            "agent": rec.get("agent", ""),
            "requested_autonomy": rec.get("requested_autonomy", ""),
            "effective_autonomy": rec.get("effective_autonomy", ""),
            "outcome": copy.deepcopy(rec.get("outcome")),
            "approval_request": copy.deepcopy(rec.get("approval_request")),
        })
    outcome = result.get("outcome")
    if isinstance(outcome, dict):
        postcondition = outcome.get("postcondition")
        if isinstance(postcondition, dict):
            checks = postcondition.get("checks")
            if isinstance(checks, list):
                for check in checks:
                    if not isinstance(check, dict):
                        continue
                    evidence_rows = check.get("evidence")
                    if not isinstance(evidence_rows, list):
                        continue
                    for evidence in evidence_rows:
                        if isinstance(evidence, dict):
                            evidence.pop("facts", None)
    for key in ("goal", "subgoal", "result", "error", "pending_question"):
        if key in result and result[key] is not None:
            result[key] = bounded_text(result[key], 500)
    if "title" in result and result["title"] is not None:
        result["title"] = bounded_text(result["title"], 160)
    if "url" in result:
        result["url"] = public_url(result["url"])
    result.pop("pending_action", None)
    result.pop("last_screenshot", None)
    return result


def validate_run_id(value):
    return isinstance(value, str) and _HEX16_RE.fullmatch(value) is not None


def finite_int(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActionRunValidationError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ActionRunValidationError(
            f"{field} must be between {minimum} and {maximum}"
        )
    return value


def finite_number(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActionRunValidationError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ActionRunValidationError(f"{field} is out of range")
    return result
