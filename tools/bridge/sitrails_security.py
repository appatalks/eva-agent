"""Local, advisory Sitrails alert monitoring for the Security Officer."""

import contextlib
import datetime
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from bridge import config, state
from bridge.utils import _load_client_prefs, _save_client_prefs


_RULES = {
    "secret-in-context": (1, "Sensitive pattern in input"),
    "secret-in-output": (2, "Sensitive pattern in output"),
    "sensitive-file-access": (3, "Sensitive file reference"),
    "potential-exfiltration": (4, "Possible data transfer"),
    "download-and-execute": (5, "Download and execution pattern"),
    "staged-sensitive-upload": (6, "Staged sensitive-file upload"),
    "downloaded-persistence-placement": (7, "Downloaded artifact in startup location"),
}
_DESCRIPTIONS = {
    0: "A custom rule matched. Inspect the original evidence and rule in Sitrails.",
    1: "A sensitive pattern was reported in input; this alone does not establish exposure.",
    2: "A sensitive pattern was reported in output; verify whether it is real and where it appeared.",
    3: "A protected file was referenced; this does not prove access or transmission.",
    4: "A possible transfer pattern was recorded; this does not prove execution or delivery.",
    5: "A download-and-execute pattern was recorded; legitimate installers can also match.",
    6: "A staged sensitive-file upload pattern was recorded; execution and delivery are not proven.",
    7: "A downloaded artifact and startup-location placement were recorded; execution is not proven.",
}
_EXAMPLE = re.compile(r"\b(?:example|sample|fixture|mock|fake|dummy|placeholder)\b", re.I)
_PLACEHOLDER = re.compile(
    r"^(?:sk-(?:proj-)?(?:fake|example|dummy)[a-z0-9_-]*|"
    r"gh[pousr]_(?:fake|example|dummy)[a-z0-9_-]*|"
    r"(?:fake|example|dummy|placeholder|your)[_-][a-z0-9_-]+|"
    r"<(?:your|example|fake|dummy)[^<>]{0,80}>)$", re.I,
)
_MATERIAL = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.)"
)
_SIGNALS = {
    "credential_format", "private_key", "jwt", "aws_signed_url", "aws_signature",
    "aws_session_token", "azure_storage_key", "azure_shared_key", "azure_api_key",
    "sas_signature", "verified_authorization", "verified_secret_assignment",
    "credential_field", "github_cli_example", "github_auth_fingerprint", "authorization",
}
_REVIEW_VERSION = 2
_CLEAR_LIMIT = 10000
_SCHEMA_LOCK = threading.Lock()


class MonitorError(Exception):
    pass


def local_base(value, *, model=False):
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port
    except (AttributeError, ValueError):
        raise MonitorError("Only a valid loopback HTTP address is allowed.") from None
    allowed_paths = ("", "/", "/v1") if model else ("", "/")
    if (parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1")
            or parsed.username is not None or parsed.password is not None or not port
            or parsed.path not in allowed_paths or parsed.query or parsed.fragment):
        raise MonitorError("Only a loopback HTTP address with an explicit port is allowed.")
    return "http://127.0.0.1:" + str(port) + ("/v1" if model else "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _request(url, payload=None, limit=1024 * 1024, timeout=8, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    method = method or ("POST" if data is not None else "GET")
    if method not in ("GET", "POST", "PUT"):
        raise MonitorError("Unsupported local request.")
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise MonitorError("A local service returned an oversized response.")
            value = json.loads(body)
            if not isinstance(value, (dict, list)):
                raise MonitorError("A local service returned an invalid response.")
            return value
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        raise MonitorError("A local service is unavailable or returned an invalid response.") from error


def _uuid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return ""


def _timestamp(value):
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return ""
        return parsed.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
    except (AttributeError, ValueError):
        return ""


def _features(payload, descriptor=None, event=None):
    """Derive bounded facts without forwarding or retaining source text."""
    descriptor = descriptor if isinstance(descriptor, dict) else {}
    event = event if isinstance(event, dict) else {}
    redactions = descriptor.get("redactions")
    redactions = redactions if isinstance(redactions, dict) else {}
    signals = {
        key: min(value, 99) for key, value in redactions.items()
        if key in _SIGNALS and isinstance(value, int) and not isinstance(value, bool) and value > 0
    }
    identifiers = descriptor.get("context_identifiers")
    identifiers = identifiers if isinstance(identifiers, list) else []
    placeholders = recognized = unknown = 0
    for identifier in identifiers[:32]:
        value = identifier.get("secret_value") if isinstance(identifier, dict) else None
        if not isinstance(value, str) or not value or len(value) > 4096:
            unknown += 1
        elif _PLACEHOLDER.fullmatch(value.strip().strip("\"'")):
            placeholders += 1
        elif _MATERIAL.search(value):
            recognized += 1
        else:
            unknown += 1
    unknown += max(0, len(identifiers) - 32)
    complete_examples = bool(
        placeholders and not recognized and not unknown and signals
        and all(count <= placeholders for count in signals.values())
        and not any(
            key not in _SIGNALS and isinstance(value, int) and not isinstance(value, bool) and value > 0
            for key, value in redactions.items()
        )
    )
    evidence = event.get("evidence")
    evidence_type = evidence.get("type") if isinstance(evidence, dict) else None
    if evidence_type not in ("observed", "reported", "correlated", "inferred", "interpreted"):
        evidence_type = "unknown"
    kind = event.get("kind")
    if kind not in ("tool.declared", "tool.started", "tool.reported", "tool.completed",
                    "model.request", "model.response", "user.message", "assistant.message",
                    "eva.turn.user", "eva.turn.assistant"):
        kind = "unknown"
    event_status = event.get("status")
    if event_status not in ("active", "ok", "error", "cancelled"):
        event_status = "unknown"
    result = {
        "available": False, "example_context": False, "recognized_value": recognized > 0,
        "placeholder_only": complete_examples, "identified_placeholders": placeholders,
        "unclassified_identifiers": unknown, "detector_signals": signals,
        "evidence_type": evidence_type, "event_kind": kind, "event_status": event_status,
        "source_redacted": descriptor.get("redaction_enabled")
        if isinstance(descriptor.get("redaction_enabled"), bool) else "unknown",
        "sample_file_reference": False, "protected_file_reference": False,
        "transfer_pattern": False, "download_execution_pattern": False,
    }
    try:
        encoded = json.dumps(payload, ensure_ascii=True) if payload is not None else ""
    except (TypeError, ValueError, RecursionError):
        return result
    if not encoded or len(encoded) > 200000:
        return result
    result["recognized_value"] = result["recognized_value"] or any(
        not _PLACEHOLDER.fullmatch(match.group(0)) for match in _MATERIAL.finditer(encoded)
    )
    result.update({
        "available": True,
        "example_context": bool(_EXAMPLE.search(encoded)),
        "sample_file_reference": bool(re.search(r"\.env\.(?:example|sample|template|dist)\b", encoded, re.I)),
        "protected_file_reference": bool(re.search(r"(?:\.env\b(?!\.(?:example|sample|template|dist)\b)|\.ssh|\.aws[/\\]+credentials)", encoded, re.I)),
        "transfer_pattern": bool(re.search(r"\b(?:curl|wget)\b.{0,300}(?:--upload-file|--post-file|--data-binary|-T\b)", encoded, re.I)),
        "download_execution_pattern": bool(re.search(r"\b(?:curl|wget)\b.{0,300}\|.{0,30}\b(?:sh|bash)\b", encoded, re.I)),
    })
    return result


def _assessment(response, rule, features):
    try:
        choice = response["choices"][0]
        message = choice["message"]
        text = message["content"].strip()
        if message.get("tool_calls") or choice.get("finish_reason") not in (None, "stop"):
            raise ValueError()
        if text.startswith("```") and text.endswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        parsed = json.loads(text)
        if not isinstance(parsed, dict) or set(parsed) != {"recommendation", "assessment"}:
            raise ValueError()
        recommendation = parsed["recommendation"]
        assessment = parsed["assessment"]
        if recommendation not in ("investigate", "dismiss_candidate", "insufficient_evidence"):
            raise ValueError()
        if (not isinstance(assessment, str) or not 20 <= len(assessment.strip()) <= 900
                or any(ord(char) < 32 and char not in "\n\t" for char in assessment)
                or re.search(r"https?://|www\.|[\\/]|@|\b[a-zA-Z0-9_-]{32,}\b|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", assessment)
                or _MATERIAL.search(assessment)):
            raise ValueError()
    except (KeyError, IndexError, TypeError, AttributeError, ValueError, json.JSONDecodeError):
        raise MonitorError("The local SI model returned an invalid advisory.") from None
    if recommendation == "dismiss_candidate" and not (
        rule in (1, 2) and features["available"] and features["placeholder_only"]
        and not features["recognized_value"]
    ):
        return ("insufficient evidence",
                "The available evidence does not establish that every detected value is an example. "
                "Inspect the original alert before deciding whether to dismiss it.")
    next_step = {
        "dismiss_candidate": "Verify the matched value and context in Sitrails before dismissing.",
        "investigate": "Inspect the Sitrails evidence and verify any real exposure or execution.",
        "insufficient_evidence": "Inspect the original Sitrails evidence before drawing a conclusion.",
    }[recommendation]
    return recommendation.replace("_", " "), " ".join(assessment.split()) + " " + next_step


@contextlib.contextmanager
def _database():
    os.makedirs(config.EVA_CONFIG_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(config.EVA_CONFIG_DIR, "sitrails-security.sqlite3")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        with _SCHEMA_LOCK:
            db.execute(
                "CREATE TABLE IF NOT EXISTS reviews ("
                "origin TEXT NOT NULL, id TEXT NOT NULL, event_id TEXT NOT NULL, timestamp TEXT NOT NULL, "
                "rule INTEGER NOT NULL, severity TEXT NOT NULL, confidence TEXT NOT NULL, "
                "recommendation TEXT NOT NULL DEFAULT 'pending', reviewed_at TEXT NOT NULL DEFAULT '', "
                "assessment TEXT NOT NULL DEFAULT '', review_version INTEGER NOT NULL DEFAULT 0, "
                "PRIMARY KEY(origin,id))"
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(reviews)")}
            if "assessment" not in columns:
                db.execute("ALTER TABLE reviews ADD COLUMN assessment TEXT NOT NULL DEFAULT ''")
            if "review_version" not in columns:
                db.execute("ALTER TABLE reviews ADD COLUMN review_version INTEGER NOT NULL DEFAULT 0")
            db.commit()
        with db:
            yield db
    finally:
        db.close()


def _active_table(db, active):
    db.execute("CREATE TEMP TABLE active_alerts (id TEXT PRIMARY KEY)")
    db.executemany("INSERT INTO active_alerts(id) VALUES (?)", ((item,) for item in active))


def _review_token(row):
    return hashlib.sha256(
        (row["reviewed_at"] + "\0" + row["recommendation"] + "\0" + row["assessment"]).encode()
    ).hexdigest()


class SitrailsMonitor:
    def __init__(self):
        self.lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self.wake = threading.Event()
        self.thread = None
        self.alert = None
        self.status = "Monitoring is disabled."
        self.partial = False
        self._active = set()
        self._next_review = 0.0
        self._generation = 0
        self._scan_offset = 0
        self._scan_seen = set()
        self._reviewing = ""
        self._review_started_at = None
        self._clear_pending = None
        self._clear_preparing = False
        self._clear = {"state": "idle", "total": 0, "resolved": 0, "unconfirmed": 0, "skipped": 0}

    def start(self, alert=None):
        if self.thread and self.thread.is_alive():
            return
        self.alert = alert
        self.thread = threading.Thread(target=self._run, name="eva-sitrails-monitor", daemon=True)
        self.thread.start()

    def settings(self):
        prefs = _load_client_prefs()
        return {
            "enabled": prefs.get("sitrails_monitor_enabled") is True,
            "url": prefs.get("sitrails_monitor_url") or "http://localhost:8000",
        }

    def configure(self, enabled, url):
        if enabled:
            normalized = local_base(url)
        else:
            try:
                normalized = local_base(self.settings()["url"])
            except MonitorError:
                normalized = local_base("http://localhost:8000")
        saved = _save_client_prefs({"sitrails_monitor_enabled": enabled, "sitrails_monitor_url": normalized})
        if saved.get("sitrails_monitor_enabled") is not enabled or saved.get("sitrails_monitor_url") != normalized:
            raise MonitorError("Monitoring settings could not be saved.")
        with self.lock:
            self._generation += 1
            self.status = "Connecting to Sitrails." if enabled else "Monitoring is disabled."
            self._active = set()
            self._scan_seen = set()
            self._scan_offset = 0
        self.wake.set()
        return self.snapshot()

    def review_activity(self):
        with self.lock:
            return {"reviewing": bool(self._reviewing), "started_at": self._review_started_at}

    def _open_alerts(self, base, target_id=None):
        ready = _request(base + "/api/ready")
        if not isinstance(ready, dict) or ready.get("service") != "sitrails" or ready.get("ready") is not True:
            raise MonitorError("Sitrails is not ready.")
        alerts = {}
        target_details = None
        more = False
        for offset in range(0, _CLEAR_LIMIT, 100):
            page = _request(base + "/api/alerts?status=open&min_confidence=low&limit=100&offset="
                            + str(offset) + "&include_sources=false")
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise MonitorError("Sitrails returned an invalid alert list.")
            items = page["items"]
            total = page.get("total")
            if len(items) > 100 or not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise MonitorError("Sitrails returned an invalid alert count.")
            for item in items:
                if not isinstance(item, dict) or item.get("status") != "open":
                    raise MonitorError("Sitrails returned an unexpected alert status.")
                alert_id = _uuid(item.get("alert_id"))
                event_id = _uuid(item.get("event_id"))
                timestamp = _timestamp(item.get("timestamp"))
                rule_id = item.get("rule_id")
                rule_version = item.get("rule_version")
                severity, confidence = item.get("severity"), item.get("confidence")
                if (not alert_id or not event_id or not timestamp
                        or not isinstance(rule_id, str) or not 1 <= len(rule_id) <= 128
                        or not isinstance(rule_version, int) or isinstance(rule_version, bool)
                        or severity not in ("low", "medium", "high")
                        or confidence not in ("low", "medium", "high")):
                    raise MonitorError("Sitrails returned an invalid alert identifier.")
                fields = [item.get(key, "") for key in ("category", "title", "explanation", "source", "trail_id")]
                if any(not isinstance(value, str) or len(value) > 16000 for value in fields):
                    raise MonitorError("Sitrails returned invalid alert details.")
                fingerprint = hashlib.sha256(json.dumps(
                    [event_id, timestamp, rule_id, rule_version, severity, confidence, *fields],
                    ensure_ascii=True, separators=(",", ":"),
                ).encode()).hexdigest()
                if alert_id in alerts:
                    more = True
                alerts[alert_id] = fingerprint
                if alert_id == target_id:
                    target_details = (event_id, timestamp, _RULES.get(rule_id, (0, ""))[0],
                                      severity, confidence)
            if target_id and target_id in alerts:
                break
            if len(items) < 100 or offset + len(items) >= total:
                more = more or offset + len(items) < total
                break
            if offset + 100 >= _CLEAR_LIMIT:
                more = True
        return alerts, more, target_details

    def prepare_clear(self, alert_id=None, review_token=None):
        settings = self.settings()
        base = local_base(settings["url"])
        target_id = _uuid(alert_id) if alert_id is not None else None
        if alert_id is not None and not target_id:
            raise MonitorError("Invalid Sitrails alert identifier.")
        if target_id and (not isinstance(review_token, str) or not re.fullmatch(r"[a-f0-9]{64}", review_token)):
            raise MonitorError("A current Security Officer review is required.")
        with self.lock:
            if self._clear_preparing or self._clear["state"] == "running":
                raise MonitorError("A Sitrails clear operation is already in progress.")
            if target_id and (not settings["enabled"] or target_id not in self._active):
                raise MonitorError("This open alert is not available for dismissal.")
            self._clear_preparing = True
            self._clear_pending = None
            generation = self._generation
        try:
            alerts, more, details = self._open_alerts(base, target_id)
            receipt = None
            if target_id:
                if target_id not in alerts or details is None:
                    self.wake.set()
                    raise MonitorError("This alert is no longer confirmed open.")
                with _database() as db:
                    row = db.execute(
                        "SELECT * FROM reviews WHERE origin=? AND id=?", (base, target_id),
                    ).fetchone()
                if (row is None or row["review_version"] != _REVIEW_VERSION
                        or row["recommendation"] == "pending" or not row["assessment"]
                        or not row["reviewed_at"]):
                    raise MonitorError("Wait for the Security Officer to finish reviewing this alert.")
                current_review = _review_token(row)
                if current_review != review_token:
                    raise MonitorError("This review changed; refresh it before dismissing the alert.")
                if tuple(row[key] for key in ("event_id", "timestamp", "rule", "severity", "confidence")) != details:
                    self.wake.set()
                    raise MonitorError("This alert changed; wait for its updated review.")
                receipt = (target_id, current_review)
                alerts = {target_id: alerts[target_id]}
                more = False
            token = uuid.uuid4().hex
            with self.lock:
                if self._generation != generation or (target_id and target_id not in self._active):
                    raise MonitorError("Sitrails settings changed while preparing the clear.")
                self._clear_pending = {
                    "token": token, "origin": base, "alerts": alerts, "more": more,
                    "expires": time.monotonic() + 60, "generation": generation, "receipt": receipt,
                }
            return {"confirmation": token, "count": len(alerts), "more": more}
        finally:
            with self.lock:
                self._clear_preparing = False

    def start_clear(self, token):
        base = local_base(self.settings()["url"])
        with self.lock:
            pending = self._clear_pending
            self._clear_pending = None
            if (not pending or not isinstance(token, str) or token != pending["token"]
                    or pending["expires"] <= time.monotonic() or pending["origin"] != base
                    or pending["generation"] != self._generation):
                raise MonitorError("Clear confirmation is invalid or expired.")
            if self._clear["state"] == "running":
                raise MonitorError("A Sitrails clear operation is already in progress.")
            self._generation += 1
            self._scan_offset = 0
            self._scan_seen = set()
            self._clear = {
                "state": "running", "total": len(pending["alerts"]), "resolved": 0,
                "unconfirmed": 0, "skipped": 0, "more": pending["more"],
                "local_cleanup_failed": False,
            }
            worker = threading.Thread(target=self._clear_alerts,
                                      args=(base, pending["alerts"], self._generation, pending["receipt"]),
                                      name="eva-sitrails-clear", daemon=True)
            try:
                worker.start()
            except RuntimeError as error:
                self._clear["state"] = "error"
                raise MonitorError("The Sitrails clear operation could not start.") from error
        return {"started": True, "count": len(pending["alerts"])}

    def _clear_alerts(self, base, selected, generation, receipt):
        with self._poll_lock:
            self._clear_alerts_locked(base, selected, generation, receipt)

    def _clear_alerts_locked(self, base, selected, generation, receipt):
        resolved = set()
        try:
            with self.lock:
                if self._generation != generation:
                    self._clear["skipped"] = len(selected)
                    self._clear["state"] = "stopped"
                    return
            still_open, more, _ = self._open_alerts(base, receipt[0] if receipt else None)
            targets = {alert_id for alert_id, fingerprint in selected.items()
                       if still_open.get(alert_id) == fingerprint}
            if receipt and receipt[0] in targets:
                with _database() as db:
                    row = db.execute("SELECT review_version,recommendation,reviewed_at,assessment "
                                     "FROM reviews WHERE origin=? AND id=?", (base, receipt[0])).fetchone()
                if (row is None or row["review_version"] != _REVIEW_VERSION
                        or row["recommendation"] == "pending" or _review_token(row) != receipt[1]):
                    targets.discard(receipt[0])
            with self.lock:
                self._clear["skipped"] = len(selected) - len(targets)
                self._clear["more"] = self._clear["more"] or (more and not receipt)
            stopped = False
            unavailable = False
            consecutive_unconfirmed = 0
            for alert_id in sorted(targets):
                with self.lock:
                    if self._generation != generation:
                        self._clear["skipped"] += len(targets) - self._clear["resolved"] - self._clear["unconfirmed"]
                        stopped = True
                        break
                try:
                    result = _request(base + "/api/alerts/" + alert_id,
                                      {"action": "resolved"}, method="PUT")
                    confirmed = isinstance(result, dict) and result.get("updated") is True
                except MonitorError:
                    confirmed = False
                with self.lock:
                    if confirmed:
                        resolved.add(alert_id)
                        self._active.discard(alert_id)
                        self._scan_seen.discard(alert_id)
                        self._clear["resolved"] += 1
                        consecutive_unconfirmed = 0
                    else:
                        self._clear["unconfirmed"] += 1
                        consecutive_unconfirmed += 1
                    if consecutive_unconfirmed >= 3:
                        self._clear["skipped"] += len(targets) - self._clear["resolved"] - self._clear["unconfirmed"]
                        unavailable = True
                if unavailable:
                    break
                time.sleep(0.01)
            if resolved:
                try:
                    with _database() as db:
                        db.executemany("DELETE FROM reviews WHERE origin=? AND id=?",
                                       ((base, alert_id) for alert_id in resolved))
                except (OSError, sqlite3.Error):
                    with self.lock:
                        self._clear["local_cleanup_failed"] = True
            with self.lock:
                self._clear["state"] = "stopped" if stopped else "error" if unavailable else "completed"
        except (MonitorError, OSError, sqlite3.Error):
            with self.lock:
                self._clear["state"] = "error"
        finally:
            self.wake.set()

    def snapshot(self):
        settings = self.settings()
        with self.lock:
            status, active, partial, reviewing = self.status, set(self._active), self.partial, self._reviewing
            clear = dict(self._clear)
        items = []
        if settings["enabled"] and active:
            with _database() as db:
                _active_table(db, active)
                rows = db.execute(
                    "SELECT reviews.* FROM reviews JOIN active_alerts USING(id) "
                    "WHERE origin=? ORDER BY timestamp DESC LIMIT 100", (local_base(settings["url"]),),
                ).fetchall()
            for row in rows:
                rule = row["rule"]
                description = _DESCRIPTIONS.get(rule, _DESCRIPTIONS[0])
                reviewed = row["review_version"] == _REVIEW_VERSION and row["recommendation"] != "pending"
                items.append({
                    "id": row["id"], "timestamp": row["timestamp"],
                    "rule": next((label for number, label in _RULES.values() if number == rule), "Custom rule"),
                    "severity": row["severity"], "confidence": row["confidence"],
                    "recommendation": row["recommendation"] if reviewed else "pending",
                    "assessment": row["assessment"] if reviewed else (
                        "Security Officer is reviewing the recorded evidence." if row["id"] == reviewing
                        else "Awaiting automatic Security Officer review."
                    ),
                    "review_token": _review_token(row) if reviewed and row["reviewed_at"] and row["assessment"] else "",
                    "description": description,
                })
        return {**settings, "status": status, "partial": partial, "alerts": items, "clear": clear}

    def _run(self):
        while not state.acp_shutdown.is_set():
            try:
                if self.settings()["enabled"]:
                    self.poll()
            except MonitorError as error:
                with self.lock:
                    self.status = str(error) + " Pending alerts will be retried."
            except (OSError, sqlite3.Error):
                with self.lock:
                    self.status = "Sitrails review storage is unavailable; pending alerts will be retried."
            self.wake.wait(15)
            self.wake.clear()

    def poll(self):
        with self._poll_lock:
            self._poll()

    def _poll(self):
        settings = self.settings()
        if not settings["enabled"]:
            return
        base = local_base(settings["url"])
        with self.lock:
            if self._clear["state"] == "running":
                return
            generation = self._generation
            offset_start = self._scan_offset
            scanned = set(self._scan_seen)
            active = set(self._active)
        if offset_start > 1000000:
            with self.lock:
                self.status = "Sitrails has more alerts than its paging API can scan."
                self.partial = True
            return
        self._check_forwarding(base)
        ready = _request(base + "/api/ready")
        if not isinstance(ready, dict) or ready.get("service") != "sitrails" or ready.get("ready") is not True:
            raise MonitorError("Sitrails is not ready.")
        added = 0
        partial = False
        complete = False
        with _database() as db:
            for offset in range(offset_start, min(offset_start + 1000, 1000100), 100):
                result = _request(base + "/api/alerts?status=open&min_confidence=low&limit=100&offset="
                                  + str(offset) + "&include_sources=false")
                if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                    raise MonitorError("Sitrails returned an invalid alert list.")
                for item in result["items"]:
                    if not isinstance(item, dict) or item.get("status") != "open":
                        continue
                    alert_id, event_id = _uuid(item.get("alert_id")), _uuid(item.get("event_id"))
                    timestamp = _timestamp(item.get("timestamp"))
                    if not alert_id or not event_id or not timestamp:
                        continue
                    active.add(alert_id)
                    scanned.add(alert_id)
                    rule_id = item.get("rule_id")
                    rule = _RULES.get(rule_id, (0, ""))[0] if isinstance(rule_id, str) else 0
                    severity = item.get("severity") if item.get("severity") in ("high", "medium", "low") else "unknown"
                    confidence = item.get("confidence") if item.get("confidence") in ("high", "medium", "low") else "unknown"
                    cursor = db.execute(
                        "INSERT OR IGNORE INTO reviews(origin,id,event_id,timestamp,rule,severity,confidence) VALUES(?,?,?,?,?,?,?)",
                        (base, alert_id, event_id, timestamp, rule, severity, confidence),
                    )
                    added += cursor.rowcount
                    changed = db.execute(
                        "UPDATE reviews SET event_id=?,timestamp=?,rule=?,severity=?,confidence=?,"
                        "recommendation='pending',reviewed_at='',assessment='',review_version=0 WHERE origin=? AND id=? AND "
                        "(event_id!=? OR timestamp!=? OR rule!=? OR severity!=? OR confidence!=?)",
                        (event_id, timestamp, rule, severity, confidence, base, alert_id,
                         event_id, timestamp, rule, severity, confidence),
                    )
                    added += changed.rowcount
                if len(result["items"]) < 100:
                    complete = True
                    break
                total = result.get("total")
                if isinstance(total, int) and offset + 100 >= total:
                    complete = True
                    break
            if not complete:
                partial = True
            _active_table(db, active)
            row = db.execute(
                "SELECT reviews.* FROM reviews JOIN active_alerts USING(id) "
                "WHERE origin=? AND (recommendation='pending' OR review_version<?) "
                "ORDER BY timestamp, reviews.id LIMIT 1", (base, _REVIEW_VERSION),
            ).fetchone()
        with self.lock:
            if self._generation != generation:
                return
            self._scan_offset = 0 if complete else offset + 100
            self._scan_seen = set() if complete else scanned
            if complete:
                active = scanned
            self._active = active
            self.partial = partial
            self.status = "Monitoring open Sitrails alerts." + (" Scanning additional alert pages." if partial else "")
        if added and callable(self.alert):
            self.alert(f"Sitrails reported {added} new open alert(s); local advisory review is pending.")
        if time.monotonic() < self._next_review:
            return
        if row is None or row["id"] not in active:
            return
        self._next_review = time.monotonic() + 15
        with self.lock:
            if self._generation != generation:
                return
            self._reviewing = row["id"]
            self._review_started_at = config.to_utc_iso(None)
        try:
            result = self._review(base, row, generation)
        except MonitorError as error:
            with self.lock:
                if self._generation == generation:
                    self.status = str(error) + " Pending alerts will be retried."
            return
        finally:
            with self.lock:
                if self._reviewing == row["id"]:
                    self._reviewing = ""
                    self._review_started_at = None
        with self.lock:
            if self._generation != generation:
                return
            with _database() as db:
                db.execute(
                    "UPDATE reviews SET recommendation=?, assessment=?, review_version=?, reviewed_at=? "
                    "WHERE origin=? AND id=?",
                    (result[0], result[1], _REVIEW_VERSION, config.to_utc_iso(None), base, row["id"]),
                )
        if callable(self.alert):
            self.alert("Security Officer completed a Sitrails advisory review: " + result[0] + ". See the Security log.")

    def _check_forwarding(self, base):
        status = _request(base + "/api/status")
        if not isinstance(status, dict) or not isinstance(status.get("s3_forwarding"), dict) or status["s3_forwarding"].get("enabled") is not False:
            raise MonitorError("Sitrails monitoring is paused because forwarding is enabled or unknown.")

    def _review(self, base, row, generation):
        prefs = _load_client_prefs()
        model_base = local_base(prefs.get("lmstudio_base_url") or "http://localhost:1234/v1", model=True)
        if model_base.removesuffix("/v1") == base:
            raise MonitorError("The local model endpoint must not be Sitrails.")
        self._check_forwarding(base)
        payload = None
        try:
            descriptor = _request(base + "/api/events/" + row["event_id"] + "/payload")
        except MonitorError:
            descriptor = {}
        try:
            event = _request(base + "/api/events/" + row["event_id"])
        except MonitorError:
            event = {}
        ref = descriptor.get("payload_ref") if isinstance(descriptor, dict) else None
        if isinstance(ref, str) and re.fullmatch(r"[a-f0-9]{64}", ref):
            try:
                payload = _request(base + "/api/objects/" + ref, limit=256000)
            except MonitorError:
                pass
        features = _features(payload, descriptor, event)
        configured_model = prefs.get("lmstudio_model", "")
        model = configured_model.strip() if isinstance(configured_model, str) else ""
        if not model:
            models = _request(model_base + "/models")
            entries = models.get("data", []) if isinstance(models, dict) else []
            model = next((entry.get("id") for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)), "")
        if not model or len(model) > 200 or any(ord(char) < 32 for char in model):
            raise MonitorError("The local SI model is unavailable.")
        with self.lock:
            if self._generation != generation:
                raise MonitorError("Monitoring was stopped.")
        self._check_forwarding(base)
        system = (
            "You are a security officer reviewing an alert using only the supplied categorical evidence. "
            "Explain what the evidence suggests and one useful verification step. "
            "Do not assume that a secret is real, a command executed, data was delivered, or intent was malicious. "
            "The event status is only a recorded status and does not establish execution or delivery. "
            "A placeholder elsewhere in context does not establish that the matched value is a placeholder. "
            "Recommend dismiss_candidate only if placeholder_only is true and no real-looking value was identified. "
            "Otherwise choose investigate or insufficient_evidence. "
            "Return a JSON object with exactly two string fields: recommendation and assessment. "
            "The recommendation must be investigate, dismiss_candidate, or insufficient_evidence. "
            "The assessment must be two concise sentences, contain no URLs, file paths, commands or credentials, "
            "and distinguish observations from uncertainty. Do not output any other text."
        )
        facts = {
            "alert_type": _DESCRIPTIONS.get(row["rule"], _DESCRIPTIONS[0]),
            "severity": row["severity"], "confidence": row["confidence"], **features,
        }
        response = _request(model_base + "/chat/completions", {
            "model": model, "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": json.dumps(facts)}],
            "max_tokens": 1024, "reasoning_effort": "low", "stream": False,
        }, timeout=45)
        return _assessment(response, row["rule"], features)


monitor = SitrailsMonitor()
