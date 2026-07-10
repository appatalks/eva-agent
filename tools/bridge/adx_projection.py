"""ADX (Azure Data Explorer) optional projection from the outbox.

Turns pending outbox events into deterministic ADX rows using
EventId / IdempotencyKey for deduplication.  Never blocks local
append.  Uses existing Kusto transport only in cloud mode.

Disabled by default unless EVA_ADX_PROJECTION=1 is set.
Runs as an explicit background job/worker, not inline with appends.

Since ADX lacks uniqueness constraints, queries/projections
dedup by EventId: ``| summarize arg_max(RecordedAt, *) by EventId``.

Only projects events with ConsentScope='cloud_allowed' and
Sensitivity != 'secret'.
"""

import datetime
import json
import os
import time

from bridge import state as _st


def _adx_projection_enabled():
    """True only when the operator explicitly opted in."""
    return os.environ.get("EVA_ADX_PROJECTION", "").strip().lower() in ("1", "true", "yes")


def _backoff_seconds(attempts, base=2, cap=3600):
    """Exponential backoff with jitter-free cap."""
    return min(base ** attempts, cap)


_ADX_EVENT_COLUMNS = [
    "EventId", "StreamId", "StreamVersion", "EventType", "SchemaVersion",
    "ActorType", "ActorId", "Origin", "OccurredAt", "RecordedAt",
    "CorrelationId", "CausationId", "SessionId", "TurnId",
    "SourceMessageId", "Trust", "Sensitivity", "ConsentScope",
    "Payload", "PayloadHash", "EventHash", "IdempotencyKey",
]


def project_pending_events(event_repo, kusto_ingest_fn, kusto_config_fn,
                           kusto_query_fn=None, limit=20):
    """Process pending outbox entries.  Returns (succeeded, failed) counts.

    Uses claim-based delivery: atomically marks entries as 'processing' before
    attempting delivery.  On success, records both local and ADX receipts.
    Before ingest, checks ADX by EventId; if present marks success without reinserting.

    Parameters
    ----------
    event_repo : bridge.events.EventRepository
    kusto_ingest_fn : callable(cluster, db, table, columns, rows) -> bool
    kusto_config_fn : callable() -> (cluster, db)
    limit : int
    """
    if not _adx_projection_enabled() or _st.egress_mode != "cloud":
        return 0, 0

    cluster, db = kusto_config_fn()
    if not cluster or not db:
        return 0, 0

    # Claim entries atomically
    claimed = event_repo.claim_outbox(limit=limit, destination="adx")
    if not claimed:
        return 0, 0

    succeeded = 0
    failed = 0

    for entry in claimed:
        event_id = entry.get("EventId", "")
        if not event_id:
            continue

        # Skip ineligible events (extra safety beyond outbox creation filter)
        if entry.get("ConsentScope") != "cloud_allowed":
            event_repo.fail_outbox(event_id, "event is not cloud-consented", "adx")
            failed += 1
            continue
        if entry.get("Sensitivity") == "secret":
            event_repo.fail_outbox(event_id, "secret events cannot be projected", "adx")
            failed += 1
            continue

        # Check durable local and ADX receipts/EventId before at-least-once ingest.
        if event_repo.has_projection_receipt(event_id, "adx"):
            event_repo.complete_outbox(event_id, "adx")
            succeeded += 1
            continue
        if kusto_query_fn is not None:
            safe_event_id = str(event_id).replace("'", "''")
            remote = kusto_query_fn(
                cluster, db,
                f"MemoryEvents | where EventId == '{safe_event_id}' | take 1",
            )
            if remote:
                receipt_row = {
                    "EventId": event_id, "Destination": "adx",
                    "ProjectedAt": datetime.datetime.now(datetime.timezone.utc)
                    .isoformat(timespec="microseconds").replace("+00:00", "Z"),
                }
                if kusto_ingest_fn(
                    cluster, db, "MemoryProjectionReceipts",
                    ["EventId", "Destination", "ProjectedAt"], [receipt_row],
                ):
                    event_repo.complete_outbox(event_id, "adx")
                    succeeded += 1
                else:
                    event_repo.fail_outbox(event_id, "ADX projection receipt ingest failed", "adx")
                    failed += 1
                continue

        # Build ADX row from the event
        event = event_repo.get_event(event_id)
        if not event:
            event_repo.fail_outbox(event_id, "event not found", "adx")
            failed += 1
            continue

        adx_row = {col: event.get(col, "") for col in _ADX_EVENT_COLUMNS}

        try:
            ok = kusto_ingest_fn(cluster, db, "MemoryEvents", _ADX_EVENT_COLUMNS, [adx_row])
            if ok:
                receipt_row = {
                    "EventId": event_id,
                    "Destination": "adx",
                    "ProjectedAt": datetime.datetime.now(datetime.timezone.utc)
                    .isoformat(timespec="microseconds").replace("+00:00", "Z"),
                }
                receipt_ok = kusto_ingest_fn(
                    cluster, db, "MemoryProjectionReceipts",
                    ["EventId", "Destination", "ProjectedAt"], [receipt_row],
                )
                if receipt_ok:
                    event_repo.complete_outbox(event_id, "adx")
                    succeeded += 1
                else:
                    raise RuntimeError("ADX projection receipt ingest failed")
            else:
                attempts = entry.get("Attempts", 0)
                delay = _backoff_seconds(attempts)
                next_attempt = (
                    datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(seconds=delay)
                ).isoformat(timespec="seconds").replace("+00:00", "Z")
                event_repo.fail_outbox(event_id, f"ingest returned false (attempt {attempts + 1})", "adx", next_attempt)
                failed += 1
        except Exception as exc:
            attempts = entry.get("Attempts", 0)
            delay = _backoff_seconds(attempts)
            next_attempt = (
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(seconds=delay)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
            event_repo.fail_outbox(event_id, str(exc)[:500], "adx", next_attempt)
            failed += 1

    return succeeded, failed
