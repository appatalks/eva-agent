"""Backend-independent normalization helpers for Eva memory.

Provides deterministic normalization of timestamps, JSON values, and
boolean/number conversions so that SQLite and ADX backends produce
identical visible output for equivalent data.

Also provides ``latest_row_sql`` for arg_max-equivalent semantics in
SQLite (with deterministic tie-breaking by rowid), and a
reconciliation-status helper for backend switching.
"""

import datetime
import json
import re


# ── Timestamp normalization ─────────────────────────────────────────

_ISO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})"
    r"(?:\.(\d+))?"
    r"(Z|[+-]\d{2}:\d{2})?$"
)


def normalize_timestamp(value):
    """Normalize a timestamp string to UTC ISO-8601 with seconds precision.

    Accepts ISO-8601 strings with or without timezone.  Returns ``None``
    for unparseable values (rather than raising).
    """
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    text = str(value or "").strip()
    if not text:
        return None

    m = _ISO_RE.match(text)
    if not m:
        return None

    date_part, time_part, _frac, tz = m.groups()
    base = f"{date_part}T{time_part}"

    try:
        if tz and tz != "Z":
            dt = datetime.datetime.fromisoformat(f"{base}{tz}")
        else:
            dt = datetime.datetime.fromisoformat(base)
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


# ── JSON normalization ──────────────────────────────────────────────

def normalize_json(value):
    """Parse a JSON string or pass-through a dict/list.  Returns a Python
    object.  Returns the original string on parse failure."""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def canonical_json_value(value):
    """Return a canonical JSON string for a value (sorted keys, compact)."""
    obj = normalize_json(value)
    if isinstance(obj, (dict, list)):
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return str(obj)


# ── Bool / number normalization ─────────────────────────────────────

def normalize_bool(value):
    """Normalize a bool-ish value to Python bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


def normalize_number(value, default=0.0):
    """Coerce to float, returning *default* on failure."""
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def normalize_int(value, default=0):
    """Coerce to int, returning *default* on failure."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


# ── Latest-row SQL (arg_max equivalent) ─────────────────────────────

def latest_row_sql(table, id_column, version_column, select_columns=None):
    """Build a SQLite query that returns the latest version of each entity.

    Equivalent to ADX:
        ``Table | summarize arg_max(VersionCol, *) by IdCol``

    Deterministic tie-breaking: when two rows have equal version_column values,
    the row with the higher rowid wins (later physical insert).

    A row whose Status='deleted' or Status='dropped' hides all prior versions
    of the same entity.

    Parameters
    ----------
    table : str
    id_column : str
        The grouping column (e.g. GoalId, SkillId).
    version_column : str
        The ordering column (e.g. UpdatedAt, CreatedAt).
    select_columns : list[str] or None
        Columns to return.  ``None`` means ``*``.

    Returns
    -------
    str – a SELECT statement.
    """
    proj = ", ".join(select_columns) if select_columns else "*"
    return (
        f"SELECT {proj} FROM {table} t1 "
        f"WHERE NOT EXISTS ("
        f"  SELECT 1 FROM {table} t2 "
        f"  WHERE t2.{id_column} = t1.{id_column} "
        f"  AND (t2.{version_column} > t1.{version_column} "
        f"       OR (t2.{version_column} = t1.{version_column} AND t2.rowid > t1.rowid))"
        f") "
        f"AND COALESCE(t1.Status, 'active') NOT IN ('deleted', 'dropped')"
    )


# ── Reconciliation status ──────────────────────────────────────────

def reconciliation_status(sqlite_mem, event_repo, target_backend=None):
    """Return a dict describing whether the event journal and legacy tables
    are reconciled.

    Direction-aware status includes event/outbox/receipt counts.
    """
    try:
        conn = sqlite_mem._conn()
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='MemoryEvents'"
        ).fetchone()
        if not exists:
            return {
                "reconciled": True,
                "event_count": 0,
                "outbox_pending": 0,
                "outbox_projected": 0,
                "receipt_count": 0,
                "unreceipted": 0,
                "adx_unprojected": 0,
                "local_only_events": 0,
                "target_backend": target_backend or "current",
                "message": "No event journal (pre-Phase 1 database).",
            }

        event_count = conn.execute("SELECT COUNT(*) FROM MemoryEvents").fetchone()[0]
        outbox_pending = conn.execute(
            "SELECT COUNT(*) FROM MemoryOutbox WHERE Status IN ('pending', 'retry', 'processing')"
        ).fetchone()[0]
        outbox_projected = conn.execute(
            "SELECT COUNT(*) FROM MemoryOutbox WHERE Status = 'projected'"
        ).fetchone()[0]
        receipt_count = conn.execute(
            "SELECT COUNT(*) FROM LegacyProjectionReceipts"
        ).fetchone()[0]
        unreceipted = conn.execute(
            "SELECT COUNT(*) FROM MemoryEvents e "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM LegacyProjectionReceipts r "
            "  WHERE r.EventId = e.EventId"
            ")"
        ).fetchone()[0]
        adx_unprojected = conn.execute(
            "SELECT COUNT(*) FROM MemoryEvents e WHERE e.ConsentScope='cloud_allowed' "
            "AND NOT EXISTS (SELECT 1 FROM MemoryProjectionReceipts r "
            "WHERE r.EventId=e.EventId AND r.Destination='adx')"
        ).fetchone()[0]
        local_only_events = conn.execute(
            "SELECT COUNT(*) FROM MemoryEvents WHERE ConsentScope IN ('local_only','session')"
        ).fetchone()[0]

        needs_adx = target_backend == "kusto"
        reconciled = unreceipted == 0 and (
            not needs_adx or (
                outbox_pending == 0 and adx_unprojected == 0 and local_only_events == 0
            )
        )
        msg = "Journals reconciled." if reconciled else (
            f"{unreceipted} local projection(s), {adx_unprojected} ADX projection(s), "
            f"and {local_only_events} local-only event(s) prevent transparent switching."
        )

        return {
            "reconciled": reconciled,
            "event_count": event_count,
            "outbox_pending": outbox_pending,
            "outbox_projected": outbox_projected,
            "receipt_count": receipt_count,
            "unreceipted": unreceipted,
            "adx_unprojected": adx_unprojected,
            "local_only_events": local_only_events,
            "target_backend": target_backend or "current",
            "message": msg,
        }
    except Exception as exc:
        return {
            "reconciled": False,
            "event_count": -1,
            "outbox_pending": -1,
            "outbox_projected": -1,
            "receipt_count": -1,
            "unreceipted": -1,
            "adx_unprojected": -1,
            "local_only_events": -1,
            "target_backend": target_backend or "current",
            "message": f"Reconciliation check failed: {exc}",
        }
