"""Local, durable public reports; never persist private task/provider settings."""

import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from bridge import config as _cfg

_confirmation_lock = threading.Lock()
_confirmations = {}


@contextmanager
def _database():
    os.makedirs(_cfg.EVA_CONFIG_DIR, exist_ok=True)
    path = os.path.join(_cfg.EVA_CONFIG_DIR, "mission-log.sqlite3")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    os.chmod(path, 0o600)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS reports ("
            "id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, label TEXT NOT NULL, "
            "status TEXT NOT NULL, report TEXT NOT NULL, ended_at TEXT NOT NULL, "
            "session_id TEXT NOT NULL, coding_run_id TEXT NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0)"
        )
        connection.execute("CREATE TABLE IF NOT EXISTS deleted_reports (id TEXT PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS permission_blocks ("
            "id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source TEXT NOT NULL, "
            "tool_kind TEXT NOT NULL, reason TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS permission_policies ("
            "category TEXT PRIMARY KEY, allowed INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS url_observations ("
            "id TEXT PRIMARY KEY, method TEXT NOT NULL, url TEXT NOT NULL, "
            "observation TEXT NOT NULL, hits INTEGER NOT NULL DEFAULT 1, "
            "first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, "
            "UNIQUE(method, url, observation))"
        )
        with connection:
            yield connection
    finally:
        connection.close()


def save_report(task):
    """An idempotent completion snapshot must commit before its task disappears."""
    if task.get("status") not in {"done", "error", "cancelled"} or not task.get("ended_at"):
        return None
    key = str(task.get("id") or "") + ":" + str(task["ended_at"])
    report_id = hashlib.sha256(key.encode()).hexdigest()[:32]
    with _database() as database:
        database.execute(
            "INSERT OR IGNORE INTO reports "
            "(id,agent_id,label,status,report,ended_at,session_id,coding_run_id) "
            "SELECT ?,?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM deleted_reports WHERE id=?)",
            (
                report_id, str(task.get("id") or ""), str(task.get("label") or "Agent")[:120],
                task["status"], str(task.get("_completed_report") or task.get("result") or "")[:64000], str(task["ended_at"]),
                str(task.get("session_id") or "")[:120], str(task.get("coding_run_id") or "")[:120], report_id,
            ),
        )
    return report_id


def list_reports(limit=100):
    with _database() as database:
        rows = database.execute(
            "SELECT * FROM reports ORDER BY ended_at DESC, id DESC LIMIT ?", (min(100, max(1, limit)),),
        ).fetchall()
    return [dict(row, reviewed=bool(row["reviewed"])) for row in rows]


def review_report(report_id, reviewed):
    with _database() as database:
        cursor = database.execute("UPDATE reports SET reviewed=? WHERE id=?", (int(reviewed), report_id))
        return cursor.rowcount == 1


def record_permission_block(source, tool_kind, reason):
    allowed_sources = {"workspace", "background"}
    allowed_kinds = {"read", "edit", "execute", "search", "fetch", "delete", "other"}
    allowed_reasons = {
        "autonomy_disabled", "unsupported_tool", "destructive_action", "mailbox_access",
        "protected_path", "outside_workspace_edit", "outside_workspace_read",
        "opaque_remote_action", "unclassified_remote_action", "protected_content",
        "destructive_execution", "opaque_execution", "outside_workspace",
        "inline_script", "shell_interpreter", "shell_composition", "unclassified_execution",
        "interactive_execution", "no_allow_once", "other_policy", "user_rejected", "permission_timeout",
    }
    if source not in allowed_sources:
        raise ValueError("Invalid permission source")
    kind = tool_kind if tool_kind in allowed_kinds else "other"
    reason = reason if reason in allowed_reasons else "other_policy"
    block_id = uuid.uuid4().hex
    with _database() as database:
        database.execute(
            "INSERT INTO permission_blocks(id,created_at,source,tool_kind,reason) VALUES (?,strftime('%Y-%m-%dT%H:%M:%SZ','now'),?,?,?)",
            (block_id, source, kind, reason),
        )
    return block_id


def list_permission_blocks(limit=100, offset=0):
    with _database() as database:
        rows = database.execute(
            "SELECT id,created_at,source,tool_kind,reason FROM permission_blocks "
            "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (min(200, max(1, limit)), max(0, offset)),
        ).fetchall()
    return [dict(row) for row in rows]


def record_url_observations(observations):
    from bridge.security_urls import sanitize_url
    valid = []
    for item in observations:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            continue
        method, url, observation = item
        method = str(method or "").upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            method = "UNKNOWN"
        clean = sanitize_url(url)
        if clean and isinstance(observation, str) and observation in {"request", "proposed", "denied", "discovered"}:
            valid.append((uuid.uuid4().hex, method, clean, observation))
    if not valid:
        return
    with _database() as database:
        enabled = database.execute(
            "SELECT allowed FROM permission_policies WHERE category='url_observation_enabled'"
        ).fetchone()
        if not enabled or not enabled["allowed"]:
            return
        database.execute("BEGIN IMMEDIATE")
        enabled = database.execute(
            "SELECT allowed FROM permission_policies WHERE category='url_observation_enabled'"
        ).fetchone()
        if not enabled or not enabled["allowed"]:
            return
        database.executemany(
            "INSERT INTO url_observations(id,method,url,observation,hits,first_seen,last_seen) "
            "VALUES (?,?,?,?,1,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
            "ON CONFLICT(method,url,observation) DO UPDATE SET hits=hits+1,last_seen=excluded.last_seen",
            valid,
        )


def list_url_observations(limit=100, offset=0):
    with _database() as database:
        rows = database.execute(
            "SELECT id,method,url,observation,hits,first_seen,last_seen FROM url_observations "
            "ORDER BY last_seen DESC,rowid DESC LIMIT ? OFFSET ?",
            (min(200, max(1, limit)), max(0, offset)),
        ).fetchall()
    return [dict(row) for row in rows]


def url_logging_enabled():
    with _database() as database:
        row = database.execute(
            "SELECT allowed FROM permission_policies WHERE category='url_observation_enabled'"
        ).fetchone()
    return bool(row and row["allowed"])


def set_url_logging_enabled(allowed):
    if not isinstance(allowed, bool):
        raise ValueError("URL logging permission must be boolean")
    with _database() as database:
        database.execute(
            "INSERT INTO permission_policies(category,allowed) VALUES ('url_observation_enabled',?) "
            "ON CONFLICT(category) DO UPDATE SET allowed=excluded.allowed", (int(allowed),)
        )


def research_permission():
    with _database() as database:
        row = database.execute(
            "SELECT allowed FROM permission_policies WHERE category='unverified_remote_research'"
        ).fetchone()
    return bool(row and row["allowed"])


def set_research_permission(allowed):
    if not isinstance(allowed, bool):
        raise ValueError("Permission must be boolean")
    from bridge import state
    with state.bg_auto_actions_lock, _database() as database:
        database.execute(
            "INSERT INTO permission_policies(category,allowed) VALUES ('unverified_remote_research',?) "
            "ON CONFLICT(category) DO UPDATE SET allowed=excluded.allowed", (int(allowed),)
        )


def prepare_delete(scope):
    if not isinstance(scope, str) or (scope not in {"all", "reports", "security", "urls"} and not re.fullmatch(r"(?:report|block):[a-f0-9]{32}", scope)):
        raise ValueError("Invalid deletion scope")
    token = uuid.uuid4().hex
    now = time.monotonic()
    with _confirmation_lock:
        for key in list(_confirmations):
            if _confirmations[key][1] <= now:
                _confirmations.pop(key, None)
        if len(_confirmations) >= 100:
            raise ValueError("Too many pending confirmations")
        _confirmations[token] = (scope, now + 60)
    return token


def delete_confirmed(scope, token):
    now = time.monotonic()
    with _confirmation_lock:
        confirmation = _confirmations.pop(token, None)
    if not confirmation or confirmation[0] != scope or confirmation[1] <= now:
        raise ValueError("Deletion confirmation expired or invalid")
    with _database() as database:
        if scope in {"all", "reports"}:
            database.execute("INSERT OR IGNORE INTO deleted_reports SELECT id FROM reports")
            reports = database.execute("DELETE FROM reports").rowcount
            blocks = database.execute("DELETE FROM permission_blocks").rowcount if scope == "all" else 0
            urls = database.execute("DELETE FROM url_observations").rowcount if scope == "all" else 0
            return reports + blocks + urls
        if scope == "security":
            blocks = database.execute("DELETE FROM permission_blocks").rowcount
            urls = database.execute("DELETE FROM url_observations").rowcount
            return blocks + urls
        if scope == "urls":
            return database.execute("DELETE FROM url_observations").rowcount
        if scope.startswith("report:"):
            report_id = scope[7:]
            database.execute("INSERT OR IGNORE INTO deleted_reports VALUES (?)", (report_id,))
            return database.execute("DELETE FROM reports WHERE id=?", (report_id,)).rowcount
        return database.execute("DELETE FROM permission_blocks WHERE id=?", (scope[6:],)).rowcount
