"""Deterministic, proposal-only Phase 2 claim consolidation.

Only ``memory.fact_candidate_extracted`` journal events become immutable
proposals. Scans advance by ``JournalSequence`` with a durable receipt for every
visited event. Canonical claims are written only by an explicit terminal
decision tied to a UUID operation identity and the exact proposal digest.
"""

import datetime
import hashlib
import json
import math
import re
import sqlite3
import unicodedata

from bridge.events import (
    IdempotencyCollisionError,
    MAX_PAYLOAD_BYTES,
    canonical_json,
    payload_hash,
)
from bridge.retrieval import parse_timestamp
from bridge.sensitive import redact_credentials


EXTRACTOR_VERSION = "deterministic-fact-v1"
CHECKPOINT_JOB_TYPE = "claim_proposal_scan"
_SCAN_LIMIT_MAX = 100
_TERMINAL_RESOLUTIONS = ("deny", "supersede", "retract", "merge")
_TEMPORAL_PREDICATES = frozenset({
    "address", "age", "city", "employer", "employment", "job",
    "location", "mood", "occupation", "role", "status",
})
_DECISION_ACTIONS = frozenset({
    "reject", "approve_new", "confirm_existing", "keep_both",
    "supersede_existing",
})
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class ConsolidationError(Exception):
    """Base error for deterministic consolidation operations."""


class ConsolidationCollisionError(ConsolidationError):
    """Durable identity was reused for different immutable content."""


class ProposalNotFoundError(ConsolidationError):
    """Requested proposal does not exist."""


class ProposalDecisionConflictError(ConsolidationError):
    """Proposal already has a different terminal decision."""


class ProposalValidationError(ConsolidationError):
    """Proposal command or source content violates the contract."""


def _utc_now(clock=None):
    now = clock() if clock else datetime.datetime.now(datetime.timezone.utc)
    if not isinstance(now, datetime.datetime):
        raise ProposalValidationError("clock must return datetime")
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc)


def _utc_iso(value):
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256(value):
    if not isinstance(value, (str, bytes)):
        value = canonical_json(value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _identity_utf8_hex(value, field="identity"):
    if not isinstance(value, str):
        raise ProposalValidationError(f"{field} must be a string")
    try:
        return value.encode("utf-8").hex()
    except UnicodeEncodeError as exc:
        raise ProposalValidationError(f"{field} must be valid UTF-8 text") from exc


def _normalize_text(value, field, max_len, *, allow_empty=False):
    if not isinstance(value, str):
        raise ProposalValidationError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if "\x00" in normalized:
        raise ProposalValidationError(f"{field} contains NUL")
    normalized = " ".join(normalized.split())
    if not allow_empty and not normalized:
        raise ProposalValidationError(f"{field} is required")
    if len(normalized) > max_len:
        raise ProposalValidationError(f"{field} exceeds {max_len} characters")
    _identity_utf8_hex(normalized, field)
    return normalized


def _normalized_key(value):
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def _exact_identity(value, field, max_len):
    if not isinstance(value, str):
        raise ProposalValidationError(f"{field} must be a string")
    if "\x00" in value:
        raise ProposalValidationError(f"{field} contains NUL")
    if not value:
        raise ProposalValidationError(f"{field} is required")
    if len(value) > max_len:
        raise ProposalValidationError(f"{field} exceeds {max_len} characters")
    _identity_utf8_hex(value, field)
    return value


def _strict_unit_float(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProposalValidationError(f"{field} must be a finite number in [0,1]")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ProposalValidationError(f"{field} must be a finite number in [0,1]") from exc
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ProposalValidationError(f"{field} must be a finite number in [0,1]")
    return result


def _checkpoint_id(extractor_version):
    return "claim-proposal-scan:" + _sha256(extractor_version)[:32]


def _claim_from_event(event):
    raw = event.get("Payload")
    if not isinstance(raw, str):
        raise ProposalValidationError("event payload must be canonical JSON text")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProposalValidationError("event payload is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ProposalValidationError("event payload must be an object")

    sensitivity = event.get("Sensitivity")
    consent_scope = event.get("ConsentScope")
    if sensitivity not in ("public", "normal", "private", "secret"):
        raise ProposalValidationError("event sensitivity is invalid")
    if consent_scope not in ("local_only", "session", "cloud_allowed"):
        raise ProposalValidationError("deleted or invalid event consent is not proposable")
    observed = parse_timestamp(event.get("OccurredAt"))
    if observed is None:
        raise ProposalValidationError("event occurrence timestamp is invalid")

    return {
        "Subject": _normalize_text(payload.get("entity"), "entity", 512),
        "Predicate": _normalize_text(payload.get("relation"), "relation", 256),
        "Object": _normalize_text(payload.get("value"), "value", 2048),
        "Confidence": _strict_unit_float(payload.get("confidence"), "confidence"),
        "Trust": _strict_unit_float(event.get("Trust"), "trust"),
        "DecayRate": 0.01,
        "Sensitivity": sensitivity,
        "ConsentScope": consent_scope,
        "ObservedAt": _utc_iso(observed),
        "EvidenceType": "direct",
    }


def _active_related_claims(conn, claim):
    placeholders = ",".join("?" for _ in _TERMINAL_RESOLUTIONS)
    rows = conn.execute(
        "SELECT c.ClaimId,c.Subject,c.Predicate,c.Object,c.ObservedAt "
        "FROM MemorySemanticClaims c "
        "WHERE c.ConsentScope!='deleted' "
        "AND NOT EXISTS (SELECT 1 FROM MemoryClaimResolutions r "
        " WHERE r.ClaimId=c.ClaimId "
        f" AND r.Action IN ({placeholders})) "
        "ORDER BY c.ClaimId COLLATE BINARY",
        _TERMINAL_RESOLUTIONS,
    ).fetchall()
    return [
        {
            "ClaimId": row[0], "Subject": row[1], "Predicate": row[2],
            "Object": row[3], "ObservedAt": row[4],
        }
        for row in rows
        if _normalized_key(row[1]) == _normalized_key(claim["Subject"])
        and _normalized_key(row[2]) == _normalized_key(claim["Predicate"])
    ]


def _classify_claim(conn, claim):
    proposed_time = parse_timestamp(claim["ObservedAt"])
    conflicts = []
    for existing in _active_related_claims(conn, claim):
        if _normalized_key(existing["Object"]) == _normalized_key(claim["Object"]):
            conflict_type = "confirmation"
        else:
            existing_time = parse_timestamp(existing["ObservedAt"])
            conflict_type = (
                "temporal_change"
                if existing_time is not None and proposed_time is not None
                and proposed_time > existing_time
                and _normalized_key(claim["Predicate"]) in _TEMPORAL_PREDICATES
                else "contradiction"
            )
        conflicts.append({
            "ClaimId": existing["ClaimId"],
            "ConflictType": conflict_type,
            "ExistingObjectHash": _sha256(_normalized_key(existing["Object"])),
        })
    conflicts.sort(key=lambda row: row["ClaimId"])
    kinds = {row["ConflictType"] for row in conflicts}
    if "contradiction" in kinds:
        classification = "contradiction"
    elif "temporal_change" in kinds:
        classification = "temporal_change"
    elif "confirmation" in kinds:
        classification = "confirmation"
    else:
        classification = "new"
    return classification, conflicts


def _proposal_values(event, extractor_version, claim, classification, conflicts):
    identity = {
        "extractor_version": extractor_version,
        "source_event_id": event["EventId"],
        "source_journal_sequence": int(event["JournalSequence"]),
        "source_payload_hash": event["PayloadHash"],
        "claim": claim,
    }
    proposal_id = _sha256(identity)
    digest_body = dict(identity)
    digest_body["classification"] = classification
    digest_body["conflicts"] = [
        {
            "claim_id_utf8_hex": _identity_utf8_hex(
                conflict["ClaimId"], "conflict ClaimId"
            ),
            "conflict_type": conflict["ConflictType"],
            "existing_object_hash": conflict["ExistingObjectHash"],
        }
        for conflict in conflicts
    ]
    return proposal_id, _sha256(digest_body)


def _receipt_values(event, extractor_version, disposition, proposal_id, reason_code):
    body = {
        "extractor_version": extractor_version,
        "source_event_id": event["EventId"],
        "source_journal_sequence": int(event["JournalSequence"]),
        "source_payload_hash": event["PayloadHash"],
        "disposition": disposition,
        "proposal_id": proposal_id,
        "reason_code": reason_code,
    }
    return body, _sha256(body)


def _proposal_claim_from_row(proposal):
    return {
        "Subject": proposal[6], "Predicate": proposal[7], "Object": proposal[8],
        "Confidence": proposal[9], "Trust": proposal[10],
        "DecayRate": proposal[11], "Sensitivity": proposal[12],
        "ConsentScope": proposal[13], "ObservedAt": proposal[14],
        "EvidenceType": proposal[16],
    }


def _conflict_dicts(conflicts):
    return [
        {
            "ClaimId": row[0], "ConflictType": row[1],
            "ExistingObjectHash": row[2],
        }
        for row in conflicts
    ]


def _validate_proposal_integrity(proposal, event, extractor_version, conflicts):
    if event.get("EventType") != "memory.fact_candidate_extracted":
        raise ConsolidationCollisionError(
            "proposal source event type is not eligible for claim consolidation"
        )
    try:
        claim = _claim_from_event(event)
    except ProposalValidationError as exc:
        raise ConsolidationCollisionError(
            "proposal source event no longer yields a valid normalized claim"
        ) from exc
    conflict_values = _conflict_dicts(conflicts)
    expected_id, expected_digest = _proposal_values(
        event, extractor_version, claim, proposal[15], conflict_values
    )
    expected_provenance = (
        expected_id, event["EventId"], int(event["JournalSequence"]),
        event["PayloadHash"], expected_digest, extractor_version,
    )
    if (
        proposal[:6] != expected_provenance
        or _proposal_claim_from_row(proposal) != claim
    ):
        raise ConsolidationCollisionError(
            "proposal source, normalized claim, conflicts, ID, or digest differs"
        )


def _validate_existing_receipt(conn, event, extractor_version, receipt):
    body, expected_hash = _receipt_values(
        event, extractor_version, receipt[4], receipt[5], receipt[6]
    )
    observed = {
        "extractor_version": receipt[0],
        "source_event_id": receipt[1],
        "source_journal_sequence": receipt[2],
        "source_payload_hash": receipt[3],
        "disposition": receipt[4],
        "proposal_id": receipt[5],
        "reason_code": receipt[6],
    }
    if observed != body or receipt[7] != expected_hash:
        raise ConsolidationCollisionError("existing scan receipt differs from source event")

    if event["EventType"] != "memory.fact_candidate_extracted":
        if receipt[4:7] != ("ignored", None, "unsupported_event"):
            raise ConsolidationCollisionError(
                "unsupported event receipt has an invalid disposition"
            )
        return
    try:
        _claim_from_event(event)
    except ProposalValidationError:
        if receipt[4:7] != ("invalid", None, "invalid_payload"):
            raise ConsolidationCollisionError(
                "invalid event receipt has an invalid disposition"
            )
        return
    if receipt[4] != "proposed" or receipt[5] is None or receipt[6] != "proposed":
        raise ConsolidationCollisionError(
            "valid fact event receipt does not reference a proposal"
        )

    proposal = conn.execute(
        "SELECT ProposalId,SourceEventId,SourceJournalSequence,SourcePayloadHash,"
        "ProposalDigest,ExtractorVersion,Subject,Predicate,Object,Confidence,Trust,"
        "DecayRate,Sensitivity,ConsentScope,ObservedAt,Classification,EvidenceType "
        "FROM MemoryClaimProposals WHERE ProposalId=?",
        (receipt[5],),
    ).fetchone()
    if proposal is None:
        raise ConsolidationCollisionError("receipt proposal is missing")
    conflicts = conn.execute(
        "SELECT ClaimId,ConflictType,ExistingObjectHash "
        "FROM MemoryClaimProposalConflicts WHERE ProposalId=? "
        "ORDER BY ClaimId COLLATE BINARY", (receipt[5],)
    ).fetchall()
    _validate_proposal_integrity(proposal, event, extractor_version, conflicts)


def _insert_proposal(conn, event, extractor_version, claim, now):
    classification, conflicts = _classify_claim(conn, claim)
    proposal_id, proposal_digest = _proposal_values(
        event, extractor_version, claim, classification, conflicts
    )
    proposal_values = (
        proposal_id, event["EventId"], int(event["JournalSequence"]),
        event["PayloadHash"], proposal_digest, extractor_version,
        claim["Subject"], claim["Predicate"], claim["Object"],
        claim["Confidence"], claim["Trust"], claim["DecayRate"],
        claim["Sensitivity"], claim["ConsentScope"], claim["ObservedAt"],
        classification, claim["EvidenceType"],
    )
    expected_conflicts = [
        (row["ClaimId"], row["ConflictType"], row["ExistingObjectHash"])
        for row in conflicts
    ]

    def existing_matches():
        rows = conn.execute(
            "SELECT ProposalId,SourceEventId,SourceJournalSequence,SourcePayloadHash,"
            "ProposalDigest,ExtractorVersion,Subject,Predicate,Object,Confidence,Trust,"
            "DecayRate,Sensitivity,ConsentScope,ObservedAt,Classification,EvidenceType "
            "FROM MemoryClaimProposals WHERE ProposalId=? OR "
            "(SourceEventId=? AND ExtractorVersion=?) "
            "ORDER BY ProposalId COLLATE BINARY",
            (proposal_id, event["EventId"], extractor_version),
        ).fetchall()
        if not rows:
            return False
        if len(rows) != 1 or tuple(rows[0]) != proposal_values:
            raise ConsolidationCollisionError(
                "proposal primary or source/extractor identity collision"
            )
        stored_conflicts = conn.execute(
            "SELECT ClaimId,ConflictType,ExistingObjectHash "
            "FROM MemoryClaimProposalConflicts WHERE ProposalId=? "
            "ORDER BY ClaimId COLLATE BINARY", (proposal_id,)
        ).fetchall()
        if [tuple(row) for row in stored_conflicts] != expected_conflicts:
            raise ConsolidationCollisionError(
                "existing proposal conflict set differs"
            )
        return True

    if existing_matches():
        return proposal_id, False
    try:
        conn.execute(
            "INSERT INTO MemoryClaimProposals "
            "(ProposalId,SourceEventId,SourceJournalSequence,SourcePayloadHash,"
            "ProposalDigest,ExtractorVersion,Subject,Predicate,Object,Confidence,Trust,"
            "DecayRate,Sensitivity,ConsentScope,ObservedAt,Classification,EvidenceType,"
            "CreatedAt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (*proposal_values, now),
        )
    except sqlite3.IntegrityError:
        if existing_matches():
            return proposal_id, False
        raise
    for conflict in conflicts:
        conn.execute(
            "INSERT INTO MemoryClaimProposalConflicts "
            "(ProposalId,ClaimId,ConflictType,ExistingObjectHash,CreatedAt) "
            "VALUES (?,?,?,?,?)",
            (
                proposal_id, conflict["ClaimId"], conflict["ConflictType"],
                conflict["ExistingObjectHash"], now,
            ),
        )
    return proposal_id, True


def _insert_scan_receipt(conn, receipt_body, receipt_hash, now):
    expected = (
        receipt_body["extractor_version"], receipt_body["source_event_id"],
        receipt_body["source_journal_sequence"],
        receipt_body["source_payload_hash"], receipt_body["disposition"],
        receipt_body["proposal_id"], receipt_body["reason_code"], receipt_hash,
    )

    def existing_matches():
        rows = conn.execute(
            "SELECT ExtractorVersion,SourceEventId,SourceJournalSequence,"
            "SourcePayloadHash,Disposition,ProposalId,ReasonCode,ReceiptHash "
            "FROM MemoryConsolidationReceipts WHERE "
            "(ExtractorVersion=? AND SourceEventId=?) OR "
            "(ExtractorVersion=? AND SourceJournalSequence=?) "
            "ORDER BY SourceEventId COLLATE BINARY",
            (
                receipt_body["extractor_version"], receipt_body["source_event_id"],
                receipt_body["extractor_version"],
                receipt_body["source_journal_sequence"],
            ),
        ).fetchall()
        if not rows:
            return False
        if len(rows) != 1 or tuple(rows[0]) != expected:
            raise ConsolidationCollisionError(
                "consolidation receipt source or sequence identity collision"
            )
        return True

    if existing_matches():
        return False
    try:
        conn.execute(
            "INSERT INTO MemoryConsolidationReceipts "
            "(ExtractorVersion,SourceEventId,SourceJournalSequence,SourcePayloadHash,"
            "Disposition,ProposalId,ReasonCode,ReceiptHash,CreatedAt) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (*expected, now),
        )
    except sqlite3.IntegrityError:
        if existing_matches():
            return False
        raise
    return True


def _validate_checkpoint_coverage(conn, extractor_version, cursor):
    if cursor == 0:
        return
    max_row = conn.execute(
        "SELECT COALESCE(MAX(JournalSequence),0) FROM MemoryEvents"
    ).fetchone()
    if not max_row or cursor > int(max_row[0]):
        raise ConsolidationCollisionError("checkpoint is beyond the journal")
    missing = conn.execute(
        "SELECT e.JournalSequence FROM MemoryEvents e "
        "LEFT JOIN MemoryConsolidationReceipts r "
        "ON r.SourceEventId=e.EventId AND r.ExtractorVersion=? "
        "WHERE e.JournalSequence<=? AND r.SourceEventId IS NULL "
        "ORDER BY e.JournalSequence LIMIT 1",
        (extractor_version, cursor),
    ).fetchone()
    if missing is not None:
        raise ConsolidationCollisionError(
            "checkpoint would skip an event without a consolidation receipt"
        )
    rows = conn.execute(
        "SELECT e.JournalSequence,e.EventId,e.EventType,e.OccurredAt,e.Trust,"
        "e.Sensitivity,e.ConsentScope,e.Payload,e.PayloadHash,"
        "r.ExtractorVersion,r.SourceEventId,r.SourceJournalSequence,"
        "r.SourcePayloadHash,r.Disposition,r.ProposalId,r.ReasonCode,r.ReceiptHash "
        "FROM MemoryEvents e JOIN MemoryConsolidationReceipts r "
        "ON r.SourceEventId=e.EventId AND r.ExtractorVersion=? "
        "WHERE e.JournalSequence<=? ORDER BY e.JournalSequence",
        (extractor_version, cursor),
    ).fetchall()
    for row in rows:
        event = {
            "JournalSequence": row[0], "EventId": row[1], "EventType": row[2],
            "OccurredAt": row[3], "Trust": row[4], "Sensitivity": row[5],
            "ConsentScope": row[6], "Payload": row[7], "PayloadHash": row[8],
        }
        _validate_existing_receipt(conn, event, extractor_version, row[9:])


def scan_claim_proposals(
    memory, *, limit=50, extractor_version=EXTRACTOR_VERSION, clock=None,
):
    """Scan one journal-sequence batch and atomically advance its checkpoint."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _SCAN_LIMIT_MAX:
        raise ProposalValidationError(f"limit must be an integer in [1,{_SCAN_LIMIT_MAX}]")
    extractor_version = _normalize_text(extractor_version, "extractor_version", 128)
    now = _utc_iso(_utc_now(clock))
    checkpoint_id = _checkpoint_id(extractor_version)
    result = {
        "extractor_version": extractor_version,
        "from_sequence": 0, "to_sequence": 0, "events_scanned": 0,
        "proposals_created": 0, "proposals_existing": 0,
        "invalid_events": 0, "ignored_events": 0, "proposal_ids": [],
    }

    with memory.transaction() as conn:
        checkpoint = conn.execute(
            "SELECT JobType,CursorValue FROM MemoryConsolidationCheckpoints "
            "WHERE CheckpointId=?", (checkpoint_id,)
        ).fetchone()
        cursor = 0
        if checkpoint is not None:
            if checkpoint[0] != CHECKPOINT_JOB_TYPE:
                raise ConsolidationCollisionError("checkpoint job type differs")
            if not isinstance(checkpoint[1], str) or not checkpoint[1].isdigit():
                raise ConsolidationCollisionError("checkpoint cursor is invalid")
            cursor = int(checkpoint[1])
            _validate_checkpoint_coverage(conn, extractor_version, cursor)
        result["from_sequence"] = cursor

        event_cursor = conn.execute(
            "SELECT JournalSequence,EventId,EventType,OccurredAt,Trust,Sensitivity,"
            "ConsentScope,Payload,PayloadHash FROM MemoryEvents "
            "WHERE JournalSequence>? ORDER BY JournalSequence LIMIT ?",
            (cursor, limit),
        )
        columns = [description[0] for description in event_cursor.description]
        events = [dict(zip(columns, row)) for row in event_cursor.fetchall()]
        for event in events:
            result["events_scanned"] += 1
            existing = conn.execute(
                "SELECT ExtractorVersion,SourceEventId,SourceJournalSequence,"
                "SourcePayloadHash,Disposition,ProposalId,ReasonCode,ReceiptHash "
                "FROM MemoryConsolidationReceipts "
                "WHERE ExtractorVersion=? AND SourceEventId=?",
                (extractor_version, event["EventId"]),
            ).fetchone()
            if existing is not None:
                _validate_existing_receipt(
                    conn, event, extractor_version, existing
                )
                if existing[4] == "proposed":
                    result["proposals_existing"] += 1
                    result["proposal_ids"].append(existing[5])
                elif existing[4] == "invalid":
                    result["invalid_events"] += 1
                else:
                    result["ignored_events"] += 1
                cursor = int(event["JournalSequence"])
                continue

            proposal_id = None
            if event["EventType"] != "memory.fact_candidate_extracted":
                disposition, reason_code = "ignored", "unsupported_event"
                result["ignored_events"] += 1
            else:
                try:
                    claim = _claim_from_event(event)
                    proposal_id, created = _insert_proposal(
                        conn, event, extractor_version, claim, now
                    )
                    result["proposals_created" if created else "proposals_existing"] += 1
                    result["proposal_ids"].append(proposal_id)
                    disposition, reason_code = "proposed", "proposed"
                except ProposalValidationError:
                    disposition, reason_code, proposal_id = (
                        "invalid", "invalid_payload", None
                    )
                    result["invalid_events"] += 1

            receipt_body, receipt_hash = _receipt_values(
                event, extractor_version, disposition, proposal_id, reason_code
            )
            _insert_scan_receipt(conn, receipt_body, receipt_hash, now)
            cursor = int(event["JournalSequence"])

        result["to_sequence"] = cursor
        metadata = canonical_json({
            key: result[key] for key in (
                "events_scanned", "ignored_events", "invalid_events",
                "proposals_created", "proposals_existing",
            )
        })
        if checkpoint is None:
            conn.execute(
                "INSERT INTO MemoryConsolidationCheckpoints "
                "(CheckpointId,JobType,CursorValue,UpdatedAt,Metadata) "
                "VALUES (?,?,?,?,?)",
                (checkpoint_id, CHECKPOINT_JOB_TYPE, str(cursor), now, metadata),
            )
        elif events:
            conn.execute(
                "UPDATE MemoryConsolidationCheckpoints SET CursorValue=?,UpdatedAt=?,"
                "Metadata=? WHERE CheckpointId=?",
                (str(cursor), now, metadata, checkpoint_id),
            )
    return result


def _decision_command(
    proposal_id, proposal_digest, action, targets, reason,
    actor_type, actor_id, origin,
):
    return {
        "proposal_id": proposal_id, "proposal_digest": proposal_digest,
        "action": action,
        "target_claim_ids_encoding": "utf-8-hex",
        "target_claim_ids_utf8_hex": [
            _identity_utf8_hex(target, "target_claim_id") for target in targets
        ],
        "reason": reason,
        "actor_type": actor_type, "actor_id": actor_id, "origin": origin,
    }


def _validate_decision_inputs(
    *, proposal_id, proposal_digest, operation_id, actor_type, actor_id,
    action, target_claim_ids, reason,
):
    if not isinstance(proposal_id, str) or _HEX64_RE.fullmatch(proposal_id) is None:
        raise ProposalValidationError("proposal_id must be 64 lowercase hex characters")
    if not isinstance(proposal_digest, str) or _HEX64_RE.fullmatch(proposal_digest) is None:
        raise ProposalValidationError("proposal_digest must be 64 lowercase hex characters")
    if not isinstance(operation_id, str) or _UUID_RE.fullmatch(operation_id) is None:
        raise ProposalValidationError("operation_id must be a UUID")
    if actor_type not in ("user", "admin"):
        raise ProposalValidationError("actor_type must be user or admin")
    if not isinstance(action, str) or action not in _DECISION_ACTIONS:
        raise ProposalValidationError("unsupported decision action")
    if not isinstance(target_claim_ids, (list, tuple)) or len(target_claim_ids) > 100:
        raise ProposalValidationError("target_claim_ids must be a bounded list")

    targets = []
    for value in target_claim_ids:
        target = _exact_identity(value, "target_claim_id", 256)
        if target in targets:
            raise ProposalValidationError("target_claim_ids must be unique")
        targets.append(target)
    if targets != sorted(targets):
        raise ProposalValidationError("target_claim_ids must be sorted")
    safe_reason = redact_credentials(
        _normalize_text(reason or "", "reason", 2048, allow_empty=True)
    )
    return (
        operation_id.lower(),
        _normalize_text(actor_id or "", "actor_id", 512, allow_empty=True),
        tuple(targets), safe_reason,
    )


def _decision_result(row, *, idempotent):
    return {
        "decision_id": row[0], "proposal_id": row[1], "action": row[5],
        "status": "rejected" if row[5] == "reject" else "approved",
        "claim_id": row[6], "decision_event_id": row[7],
        "idempotent": idempotent,
    }


def _validated_audit_payload(command, command_hash, audit_result):
    safe_payload = redact_credentials({
        "command": command,
        "command_hash": command_hash,
        "result": audit_result,
    })
    payload_json = canonical_json(safe_payload)
    payload_size = len(payload_json.encode("utf-8"))
    if payload_size > MAX_PAYLOAD_BYTES:
        raise ProposalValidationError(
            "decision audit payload exceeds the immutable journal byte limit"
        )
    return json.loads(payload_json)


def _insert_claim(conn, proposal, claim_id, now):
    conn.execute(
        "INSERT INTO MemorySemanticClaims "
        "(ClaimId,Subject,Predicate,Object,Confidence,Trust,DecayRate,Sensitivity,"
        "ConsentScope,Source,ObservedAt,CreatedAt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            claim_id, proposal[6], proposal[7], proposal[8], proposal[9],
            proposal[10], proposal[11], proposal[12], proposal[13],
            f"proposal:{proposal[0]}", proposal[14], now,
        ),
    )


def _validate_proposal_freshness(conn, proposal, conflicts):
    claim = {
        "Subject": proposal[6], "Predicate": proposal[7], "Object": proposal[8],
        "ObservedAt": proposal[14],
    }
    classification, current = _classify_claim(conn, claim)
    stored = [
        {
            "ClaimId": row[0], "ConflictType": row[1],
            "ExistingObjectHash": row[2],
        }
        for row in conflicts
    ]
    if classification != proposal[15] or current != stored:
        raise ProposalDecisionConflictError(
            "proposal conflict set is stale; rescan with a new extractor version"
        )


def decide_claim_proposal(
    memory, repository, *, proposal_id, proposal_digest, operation_id,
    actor_type, actor_id="", origin="api", action, target_claim_ids=(),
    reason="", correlation_id="", session_id="", turn_id="", clock=None,
):
    """Atomically reject or approve one immutable proposal exactly once."""
    operation_id, actor_id, targets, reason = _validate_decision_inputs(
        proposal_id=proposal_id, proposal_digest=proposal_digest,
        operation_id=operation_id, actor_type=actor_type, actor_id=actor_id,
        action=action, target_claim_ids=target_claim_ids, reason=reason,
    )
    if origin not in ("browser", "api", "test"):
        raise ProposalValidationError("origin is invalid")
    command = _decision_command(
        proposal_id, proposal_digest, action, targets, reason,
        actor_type, actor_id, origin,
    )
    command_hash = payload_hash(canonical_json(command))
    now = _utc_iso(_utc_now(clock))

    with memory.transaction() as conn:
        existing = conn.execute(
            "SELECT DecisionId,ProposalId,ProposalDigest,OperationId,CommandHash,"
            "Action,ClaimId,DecisionEventId FROM MemoryClaimProposalDecisions "
            "WHERE OperationId=?", (operation_id,)
        ).fetchone()
        if existing is not None:
            if existing[4] != command_hash:
                raise IdempotencyCollisionError(
                    operation_id, "operation identity reused with different decision command"
                )
            return _decision_result(existing, idempotent=True)
        if conn.execute(
            "SELECT 1 FROM MemoryClaimProposalDecisions WHERE ProposalId=?",
            (proposal_id,),
        ).fetchone():
            raise ProposalDecisionConflictError(
                "proposal already has a terminal decision"
            )

        proposal = conn.execute(
            "SELECT ProposalId,SourceEventId,SourceJournalSequence,SourcePayloadHash,"
            "ProposalDigest,ExtractorVersion,Subject,Predicate,Object,Confidence,Trust,"
            "DecayRate,Sensitivity,ConsentScope,ObservedAt,Classification,EvidenceType "
            "FROM MemoryClaimProposals WHERE ProposalId=?", (proposal_id,)
        ).fetchone()
        if proposal is None:
            raise ProposalNotFoundError("proposal not found")
        if proposal[4] != proposal_digest:
            raise ProposalValidationError("proposal digest does not match")
        conflicts = conn.execute(
            "SELECT ClaimId,ConflictType,ExistingObjectHash "
            "FROM MemoryClaimProposalConflicts WHERE ProposalId=? "
            "ORDER BY ClaimId COLLATE BINARY", (proposal_id,)
        ).fetchall()
        classification = proposal[15]
        if action != "reject":
            event_row = conn.execute(
                "SELECT JournalSequence,EventId,EventType,OccurredAt,Trust,"
                "Sensitivity,ConsentScope,Payload,PayloadHash FROM MemoryEvents "
                "WHERE EventId=?", (proposal[1],)
            ).fetchone()
            if event_row is None:
                raise ProposalDecisionConflictError(
                    "proposal source event is missing"
                )
            event = {
                "JournalSequence": event_row[0], "EventId": event_row[1],
                "EventType": event_row[2], "OccurredAt": event_row[3],
                "Trust": event_row[4], "Sensitivity": event_row[5],
                "ConsentScope": event_row[6], "Payload": event_row[7],
                "PayloadHash": event_row[8],
            }
            try:
                _validate_proposal_integrity(
                    proposal, event, proposal[5], conflicts
                )
            except ConsolidationCollisionError as exc:
                raise ProposalDecisionConflictError(
                    "proposal digest integrity check failed"
                ) from exc
            _validate_proposal_freshness(conn, proposal, conflicts)

        if action == "reject":
            if targets:
                raise ProposalValidationError("reject does not accept target claims")
            claim_id = None
        elif action == "approve_new":
            if classification != "new" or conflicts or targets:
                raise ProposalValidationError(
                    "approve_new requires a new conflict-free proposal"
                )
            claim_id = "clm-" + proposal_id
        elif action == "confirm_existing":
            valid_targets = tuple(row[0] for row in conflicts if row[1] == "confirmation")
            if classification != "confirmation" or len(targets) != 1 or targets[0] not in valid_targets:
                raise ProposalValidationError(
                    "confirm_existing requires one confirmation conflict target"
                )
            claim_id = targets[0]
        elif action == "keep_both":
            if (
                classification not in ("contradiction", "temporal_change")
                or targets
                or any(row[1] == "confirmation" for row in conflicts)
            ):
                raise ProposalValidationError(
                    "keep_both requires only contradiction/temporal conflicts and no targets"
                )
            claim_id = "clm-" + proposal_id
        else:
            required = tuple(row[0] for row in conflicts)
            if (
                classification not in ("contradiction", "temporal_change")
                or not required or targets != required
            ):
                raise ProposalValidationError(
                    "supersede_existing requires the exact sorted conflict set"
                )
            claim_id = "clm-" + proposal_id

        decision_id = _sha256({
            "operation_id": operation_id, "command_hash": command_hash,
        })
        result = {
            "decision_id": decision_id, "proposal_id": proposal_id,
            "action": action,
            "status": "rejected" if action == "reject" else "approved",
            "claim_id": claim_id,
        }
        audit_result = {
            "decision_id": decision_id,
            "proposal_id": proposal_id,
            "action": action,
            "status": result["status"],
            "claim_id_encoding": "utf-8-hex",
            "claim_id_utf8_hex": (
                _identity_utf8_hex(claim_id, "claim_id")
                if claim_id is not None else None
            ),
        }
        audit_payload = _validated_audit_payload(
            command, command_hash, audit_result
        )

        if action in ("approve_new", "keep_both", "supersede_existing"):
            _insert_claim(conn, proposal, claim_id, now)

        if action != "reject":
            evidence_id = "evd-" + _sha256({
                "proposal_id": proposal_id,
                "claim_id_utf8_hex": _identity_utf8_hex(claim_id, "claim_id"),
                "source_event_id": proposal[1],
            })
            evidence_type = "corroborated" if action == "confirm_existing" else proposal[16]
            conn.execute(
                "INSERT INTO MemoryClaimEvidence "
                "(EvidenceId,ClaimId,EventId,EvidenceType,Strength,RecordedAt) "
                "VALUES (?,?,?,?,?,?)",
                (evidence_id, claim_id, proposal[1], evidence_type, proposal[9], now),
            )
            if action == "confirm_existing":
                resolution_id = "res-" + _sha256({
                    "proposal_id": proposal_id,
                    "claim_id_utf8_hex": _identity_utf8_hex(
                        claim_id, "claim_id"
                    ),
                    "action": "confirm",
                })
                conn.execute(
                    "INSERT INTO MemoryClaimResolutions "
                    "(ResolutionId,ClaimId,Action,Reason,ResolvedBy,ResolvedAt) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        resolution_id, claim_id, "confirm",
                        reason or f"confirmed by proposal {proposal_id}",
                        actor_type, now,
                    ),
                )
            elif action == "supersede_existing":
                for old_claim_id in targets:
                    resolution_id = "res-" + _sha256({
                        "proposal_id": proposal_id,
                        "claim_id_utf8_hex": _identity_utf8_hex(
                            old_claim_id, "claim_id"
                        ),
                        "new_claim_id_utf8_hex": _identity_utf8_hex(
                            claim_id, "new_claim_id"
                        ),
                        "action": "supersede",
                    })
                    conn.execute(
                        "INSERT INTO MemoryClaimResolutions "
                        "(ResolutionId,ClaimId,Action,Reason,ResolvedBy,ResolvedAt) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            resolution_id, old_claim_id, "supersede",
                            reason or f"superseded by {claim_id}", actor_type, now,
                        ),
                    )

        event = repository.append_event(
            connection=conn,
            stream_id=f"claim-proposal:{proposal_id}",
            event_type="memory.claim_proposal_decided",
            payload=audit_payload,
            actor_type=actor_type, actor_id=actor_id, origin=origin,
            occurred_at=now, correlation_id=correlation_id,
            session_id=session_id, turn_id=turn_id, trust=1.0,
            sensitivity="private", consent_scope="local_only",
            idempotency_key=(
                f"request:{operation_id}:memory.claim_proposal_decided"
            ),
        )
        conn.execute(
            "INSERT INTO MemoryClaimProposalDecisions "
            "(DecisionId,ProposalId,ProposalDigest,OperationId,CommandHash,Action,"
            "ClaimId,DecisionEventId,Reason,ActorType,ActorId,DecidedAt) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id, proposal_id, proposal_digest, operation_id,
                command_hash, action, claim_id, event["EventId"], reason,
                actor_type, actor_id, now,
            ),
        )
        result["decision_event_id"] = event["EventId"]
        result["idempotent"] = False
        return result


def list_claim_proposals(memory, *, status="pending", limit=50):
    if status not in ("pending", "approved", "rejected", "all"):
        raise ProposalValidationError("status is invalid")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ProposalValidationError("limit must be an integer in [1,100]")
    where = ""
    if status == "pending":
        where = "WHERE d.ProposalId IS NULL"
    elif status == "approved":
        where = "WHERE d.Action IS NOT NULL AND d.Action!='reject'"
    elif status == "rejected":
        where = "WHERE d.Action='reject'"
    sql = (
        "SELECT p.ProposalId,p.ProposalDigest,p.SourceEventId,"
        "p.SourceJournalSequence,p.ExtractorVersion,p.Subject,p.Predicate,p.Object,"
        "p.Confidence,p.Trust,p.Sensitivity,p.ConsentScope,p.ObservedAt,"
        "p.Classification,p.EvidenceType,p.CreatedAt,"
        "CASE WHEN d.ProposalId IS NULL THEN 'pending' "
        "WHEN d.Action='reject' THEN 'rejected' ELSE 'approved' END AS Status,"
        "d.Action,d.ClaimId,d.DecidedAt "
        "FROM MemoryClaimProposals p LEFT JOIN MemoryClaimProposalDecisions d "
        f"ON d.ProposalId=p.ProposalId {where} "
        "ORDER BY p.SourceJournalSequence DESC,p.ProposalId COLLATE BINARY LIMIT ?"
    )
    return memory.query_strict(sql, (limit,))


def get_claim_proposal(memory, proposal_id):
    if not isinstance(proposal_id, str) or _HEX64_RE.fullmatch(proposal_id) is None:
        raise ProposalValidationError("proposal_id must be 64 lowercase hex characters")
    proposals = memory.query_strict(
        "SELECT p.*,CASE WHEN d.ProposalId IS NULL THEN 'pending' "
        "WHEN d.Action='reject' THEN 'rejected' ELSE 'approved' END AS Status,"
        "d.Action,d.ClaimId,d.DecisionId,d.DecisionEventId,d.DecidedAt "
        "FROM MemoryClaimProposals p LEFT JOIN MemoryClaimProposalDecisions d "
        "ON d.ProposalId=p.ProposalId WHERE p.ProposalId=?", (proposal_id,)
    )
    if not proposals:
        return None
    proposal = proposals[0]
    proposal["conflicts"] = memory.query_strict(
        "SELECT ClaimId,ConflictType,ExistingObjectHash "
        "FROM MemoryClaimProposalConflicts WHERE ProposalId=? "
        "ORDER BY ClaimId COLLATE BINARY", (proposal_id,)
    )
    return proposal
