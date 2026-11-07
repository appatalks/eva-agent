"""Local, durable public reports; never persist private task/provider settings."""

import hashlib
import os
import sqlite3
from contextlib import contextmanager

from bridge import config as _cfg


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
            "(id,agent_id,label,status,report,ended_at,session_id,coding_run_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                report_id, str(task.get("id") or ""), str(task.get("label") or "Agent")[:120],
                task["status"], str(task.get("_completed_report") or task.get("result") or "")[:64000], str(task["ended_at"]),
                str(task.get("session_id") or "")[:120], str(task.get("coding_run_id") or "")[:120],
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
