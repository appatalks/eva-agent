#!/usr/bin/env python3
"""Phase 2 iteration-3 consolidation tests.

Temporary SQLite only; no models, providers, external network, real memory DB,
background loop, or UI. Covers journal-sequence scan receipts, deterministic
proposal/classification, immutable decisions, exactly-once approval, rollback,
and additive schema v2.
"""

import datetime
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from bridge.events import IdempotencyCollisionError  # noqa: E402
from bridge.phase2_consolidation import (  # noqa: E402
    EXTRACTOR_VERSION,
    ConsolidationCollisionError,
    ProposalDecisionConflictError,
    ProposalValidationError,
    _proposal_values,
    _receipt_values,
    _sha256,
    decide_claim_proposal,
    get_claim_proposal,
    list_claim_proposals,
    scan_claim_proposals,
)
from bridge.phase2_schema import (  # noqa: E402
    Phase2MigrationError,
    Phase2SchemaVerificationError,
    _current_version,
    run_phase2_migrations,
    verify_phase2_schema,
)
from bridge.migrations import run_migrations  # noqa: E402
from sqlite_memory import SqliteMemory  # noqa: E402


NOW = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.timezone.utc)
NOW_ISO = "2026-08-01T12:00:00Z"
OP1 = "11111111-1111-4111-8111-111111111111"
OP2 = "22222222-2222-4222-8222-222222222222"


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="eva_p2_consolidation_")
        self.db_path = os.path.join(self.tmpdir, "memory.db")
        self.mem = SqliteMemory(self.db_path)
        self.repo = self.mem.event_repository()

    def tearDown(self):
        self.mem.close()
        shutil.rmtree(self.tmpdir)

    def _append_event(
        self,
        key,
        *,
        event_type="memory.fact_candidate_extracted",
        payload=None,
        occurred_at=NOW_ISO,
        trust=0.9,
        sensitivity="private",
        consent_scope="local_only",
    ):
        if payload is None:
            payload = {
                "entity": "User",
                "relation": "likes",
                "value": "coffee",
                "confidence": 0.9,
            }
        return self.repo.append_event(
            stream_id=f"test:{key}",
            event_type=event_type,
            payload=payload,
            actor_type="system",
            actor_id="eva",
            origin="test",
            occurred_at=occurred_at,
            trust=trust,
            sensitivity=sensitivity,
            consent_scope=consent_scope,
            idempotency_key=f"consolidation-test:{key}",
        )

    def _insert_claim(
        self,
        claim_id,
        *,
        subject="User",
        predicate="likes",
        object_value="tea",
        observed_at="2026-07-01T00:00:00Z",
    ):
        with self.mem.transaction() as conn:
            conn.execute(
                "INSERT INTO MemorySemanticClaims "
                "(ClaimId,Subject,Predicate,Object,Confidence,Trust,ObservedAt) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    claim_id, subject, predicate, object_value,
                    0.8, 0.9, observed_at,
                ),
            )

    def _resolve(self, claim_id, action):
        with self.mem.transaction() as conn:
            conn.execute(
                "INSERT INTO MemoryClaimResolutions "
                "(ResolutionId,ClaimId,Action,ResolvedBy) VALUES (?,?,?,?)",
                (f"resolution-{claim_id}-{action}", claim_id, action, "user"),
            )

    def _scan(self, **kwargs):
        options = {"clock": lambda: NOW}
        options.update(kwargs)
        return scan_claim_proposals(self.mem, **options)

    def _proposal(self, proposal_id=None):
        if proposal_id is None:
            proposal_id = self.mem.query_strict(
                "SELECT ProposalId FROM MemoryClaimProposals "
                "ORDER BY SourceJournalSequence DESC LIMIT 1"
            )[0]["ProposalId"]
        return get_claim_proposal(self.mem, proposal_id)

    def _decide(self, proposal, **kwargs):
        options = {
            "proposal_id": proposal["ProposalId"],
            "proposal_digest": proposal["ProposalDigest"],
            "operation_id": OP1,
            "actor_type": "user",
            "actor_id": "test-user",
            "origin": "test",
            "action": "approve_new",
            "target_claim_ids": (),
            "reason": "",
            "clock": lambda: NOW,
        }
        options.update(kwargs)
        return decide_claim_proposal(self.mem, self.repo, **options)

    def test_scan_uses_journal_sequence_and_receipts_every_event(self):
        ignored = self._append_event(
            "ignored", event_type="conversation.user_observed",
            payload={"role": "user", "content": "hello"},
        )
        proposed = self._append_event("proposed")
        invalid = self._append_event(
            "invalid", payload={"entity": "User", "relation": "likes"},
        )
        result = self._scan(limit=3)
        self.assertEqual(result["from_sequence"], 0)
        self.assertEqual(result["to_sequence"], invalid["JournalSequence"])
        self.assertEqual(result["events_scanned"], 3)
        self.assertEqual(result["ignored_events"], 1)
        self.assertEqual(result["invalid_events"], 1)
        self.assertEqual(result["proposals_created"], 1)
        receipts = self.mem.query_strict(
            "SELECT SourceEventId,SourceJournalSequence,Disposition,ReasonCode "
            "FROM MemoryConsolidationReceipts ORDER BY SourceJournalSequence"
        )
        self.assertEqual(
            receipts,
            [
                {
                    "SourceEventId": ignored["EventId"],
                    "SourceJournalSequence": ignored["JournalSequence"],
                    "Disposition": "ignored", "ReasonCode": "unsupported_event",
                },
                {
                    "SourceEventId": proposed["EventId"],
                    "SourceJournalSequence": proposed["JournalSequence"],
                    "Disposition": "proposed", "ReasonCode": "proposed",
                },
                {
                    "SourceEventId": invalid["EventId"],
                    "SourceJournalSequence": invalid["JournalSequence"],
                    "Disposition": "invalid", "ReasonCode": "invalid_payload",
                },
            ],
        )

    def test_lone_surrogates_become_invalid_receipts_and_do_not_stall_scan(self):
        events = []
        for field in ("entity", "relation", "value"):
            for suffix, surrogate in (("high", "\ud800"), ("low", "\udc00")):
                payload = {
                    "entity": "User", "relation": "likes", "value": "coffee",
                    "confidence": 0.9,
                }
                payload[field] = "bad" + surrogate
                events.append(self._append_event(
                    f"surrogate-{field}-{suffix}", payload=payload
                ))
        valid = self._append_event(
            "after-surrogates",
            payload={
                "entity": "User", "relation": "favorite_color", "value": "blue",
                "confidence": 0.9,
            },
        )
        result = self._scan(limit=7)
        self.assertEqual(result["invalid_events"], 6)
        self.assertEqual(result["proposals_created"], 1)
        self.assertEqual(result["to_sequence"], valid["JournalSequence"])
        self.assertEqual(self.mem.count("MemoryConsolidationReceipts"), 7)
        self.assertEqual(self.mem.count("MemoryClaimProposals"), 1)
        dispositions = self.mem.query_strict(
            "SELECT SourceEventId,Disposition FROM MemoryConsolidationReceipts "
            "ORDER BY SourceJournalSequence"
        )
        self.assertEqual(
            [row["Disposition"] for row in dispositions],
            ["invalid"] * 6 + ["proposed"],
        )
        self.assertEqual(self._scan()["events_scanned"], 0)

    def test_scan_batches_resume_without_timestamp_gaps(self):
        events = [self._append_event(f"batch-{index}") for index in range(5)]
        first = self._scan(limit=2)
        second = self._scan(limit=2)
        third = self._scan(limit=2)
        fourth = self._scan(limit=2)
        self.assertEqual(first["to_sequence"], events[1]["JournalSequence"])
        self.assertEqual(second["from_sequence"], first["to_sequence"])
        self.assertEqual(second["to_sequence"], events[3]["JournalSequence"])
        self.assertEqual(third["to_sequence"], events[4]["JournalSequence"])
        self.assertEqual(fourth["events_scanned"], 0)
        self.assertEqual(self.mem.count("MemoryClaimProposals"), 5)
        self.assertEqual(self.mem.count("MemoryConsolidationReceipts"), 5)

    def test_scan_retry_from_stale_checkpoint_reuses_exact_receipt(self):
        event = self._append_event("stale-checkpoint")
        first = self._scan()
        checkpoint = self.mem.query_strict(
            "SELECT CheckpointId FROM MemoryConsolidationCheckpoints"
        )[0]["CheckpointId"]
        with self.mem.transaction() as conn:
            conn.execute(
                "UPDATE MemoryConsolidationCheckpoints SET CursorValue='0' "
                "WHERE CheckpointId=?", (checkpoint,),
            )
        replay = self._scan()
        self.assertEqual(first["proposal_ids"], replay["proposal_ids"])
        self.assertEqual(replay["proposals_existing"], 1)
        self.assertEqual(replay["to_sequence"], event["JournalSequence"])
        self.assertEqual(self.mem.count("MemoryClaimProposals"), 1)

    def test_forward_checkpoint_cannot_skip_unreceipted_event(self):
        first = self._append_event("checkpoint-first")
        second = self._append_event("checkpoint-second")
        self._scan(limit=1)
        checkpoint = self.mem.query_strict(
            "SELECT CheckpointId FROM MemoryConsolidationCheckpoints"
        )[0]["CheckpointId"]
        with self.mem.transaction() as conn:
            conn.execute(
                "UPDATE MemoryConsolidationCheckpoints SET CursorValue=? "
                "WHERE CheckpointId=?",
                (str(second["JournalSequence"]), checkpoint),
            )
        with self.assertRaises(ConsolidationCollisionError):
            self._scan()
        receipts = self.mem.query_strict(
            "SELECT SourceEventId FROM MemoryConsolidationReceipts"
        )
        self.assertEqual(receipts, [{"SourceEventId": first["EventId"]}])

    def test_receipt_collision_rolls_back_checkpoint(self):
        from bridge.phase2_consolidation_schema import V2_TRIGGERS

        self._append_event("receipt-collision")
        self._scan()
        checkpoint = self.mem.query_strict(
            "SELECT CheckpointId FROM MemoryConsolidationCheckpoints"
        )[0]["CheckpointId"]
        update_trigger = next(
            sql for name, _table, sql in V2_TRIGGERS
            if name == "trg_consolidation_receipts_no_update"
        )
        with self.mem.transaction() as conn:
            conn.execute("DROP TRIGGER trg_consolidation_receipts_no_update")
            conn.execute(
                "UPDATE MemoryConsolidationReceipts SET ReceiptHash=?",
                ("f" * 64,),
            )
            conn.execute(update_trigger)
            conn.execute(
                "UPDATE MemoryConsolidationCheckpoints SET CursorValue='0' "
                "WHERE CheckpointId=?", (checkpoint,),
            )
        with self.assertRaises(ConsolidationCollisionError):
            self._scan()
        cursor = self.mem.query_strict(
            "SELECT CursorValue FROM MemoryConsolidationCheckpoints "
            "WHERE CheckpointId=?", (checkpoint,),
        )[0]["CursorValue"]
        self.assertEqual(cursor, "0")

    def test_scan_exception_rolls_back_proposal_receipt_and_checkpoint(self):
        self._append_event("scan-rollback")
        with mock.patch(
            "bridge.phase2_consolidation._insert_proposal",
            side_effect=RuntimeError("injected"),
        ):
            with self.assertRaises(RuntimeError):
                self._scan()
        self.assertEqual(self.mem.count("MemoryClaimProposals"), 0)
        self.assertEqual(self.mem.count("MemoryConsolidationReceipts"), 0)
        self.assertEqual(self.mem.count("MemoryConsolidationCheckpoints"), 0)

    def test_new_proposal_is_deterministic_and_provenance_complete(self):
        event = self._append_event("deterministic")
        result = self._scan()
        proposal = self._proposal(result["proposal_ids"][0])
        self.assertEqual(proposal["SourceEventId"], event["EventId"])
        self.assertEqual(proposal["SourceJournalSequence"], event["JournalSequence"])
        self.assertEqual(proposal["SourcePayloadHash"], event["PayloadHash"])
        self.assertEqual(proposal["ExtractorVersion"], EXTRACTOR_VERSION)
        self.assertEqual(proposal["Classification"], "new")
        self.assertEqual(proposal["Status"], "pending")
        self.assertEqual(proposal["conflicts"], [])
        self.assertEqual(len(proposal["ProposalId"]), 64)
        self.assertEqual(len(proposal["ProposalDigest"]), 64)

    def test_extractor_version_is_identity_scoped(self):
        self._append_event("extractor-version")
        first = self._scan(extractor_version="extractor-a")
        second = self._scan(extractor_version="extractor-b")
        self.assertNotEqual(first["proposal_ids"], second["proposal_ids"])
        self.assertEqual(self.mem.count("MemoryClaimProposals"), 2)
        self.assertEqual(self.mem.count("MemoryConsolidationCheckpoints"), 2)

    def test_confirmation_classification_uses_active_claim(self):
        self._insert_claim("existing-confirm", object_value="  COFFEE ")
        self._append_event("confirmation")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        self.assertEqual(proposal["Classification"], "confirmation")
        self.assertEqual(
            proposal["conflicts"][0]["ConflictType"], "confirmation"
        )
        self.assertEqual(proposal["conflicts"][0]["ClaimId"], "existing-confirm")

    def test_unicode_casefold_conflict_matching_is_not_sqlite_nocase_limited(self):
        self._insert_claim(
            "unicode-existing",
            subject="JOSÉ",
            predicate="PRÉFÈRE",
            object_value="Café",
        )
        self._append_event(
            "unicode-confirmation",
            payload={
                "entity": "jose\u0301", "relation": "pre\u0301fe\u0300re",
                "value": "cafe\u0301", "confidence": 0.9,
            },
        )
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        self.assertEqual(proposal["Classification"], "confirmation")
        self.assertEqual(proposal["conflicts"][0]["ClaimId"], "unicode-existing")

    def test_temporal_change_and_contradiction_are_deterministic(self):
        self._insert_claim(
            "old-location", predicate="location", object_value="Paris",
            observed_at="2026-01-01T00:00:00Z",
        )
        self._append_event(
            "new-location",
            payload={
                "entity": "User", "relation": "location", "value": "Rome",
                "confidence": 0.9,
            },
            occurred_at="2026-08-01T00:00:00Z",
        )
        temporal = self._proposal(self._scan()["proposal_ids"][0])
        self.assertEqual(temporal["Classification"], "temporal_change")

        self._insert_claim(
            "future-employer", predicate="employer", object_value="A",
            observed_at="2027-01-01T00:00:00Z",
        )
        self._append_event(
            "old-employer",
            payload={
                "entity": "User", "relation": "employer", "value": "B",
                "confidence": 0.9,
            },
            occurred_at="2026-08-02T00:00:00Z",
        )
        contradiction = self._proposal(self._scan()["proposal_ids"][0])
        self.assertEqual(contradiction["Classification"], "contradiction")

    def test_newer_static_fact_is_contradiction_not_temporal_change(self):
        self._insert_claim(
            "old-preference", object_value="tea",
            observed_at="2026-01-01T00:00:00Z",
        )
        self._append_event(
            "newer-preference",
            occurred_at="2026-08-01T00:00:00Z",
        )
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        self.assertEqual(proposal["Classification"], "contradiction")

    def test_terminal_existing_claim_is_not_a_conflict(self):
        self._insert_claim("terminal-old", object_value="tea")
        self._resolve("terminal-old", "retract")
        self._append_event("after-terminal")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        self.assertEqual(proposal["Classification"], "new")
        self.assertEqual(proposal["conflicts"], [])

    def test_deleted_consent_claim_is_not_a_conflict(self):
        with self.mem.transaction() as conn:
            conn.execute(
                "INSERT INTO MemorySemanticClaims "
                "(ClaimId,Subject,Predicate,Object,Confidence,Trust,ConsentScope,"
                "ObservedAt) VALUES (?,?,?,?,?,?,?,?)",
                (
                    "deleted-old", "User", "likes", "tea", 0.8, 0.9,
                    "deleted", "2026-07-01T00:00:00Z",
                ),
            )
        self._append_event("after-deleted")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        self.assertEqual(proposal["Classification"], "new")
        self.assertEqual(proposal["conflicts"], [])

    def test_valid_fact_cannot_replay_as_self_consistent_ignored_receipt(self):
        event = self._append_event("forged-ignored")
        event_view = {
            key: event[key] for key in (
                "JournalSequence", "EventId", "EventType", "OccurredAt", "Trust",
                "Sensitivity", "ConsentScope", "Payload", "PayloadHash",
            )
        }
        body, receipt_hash = _receipt_values(
            event_view, EXTRACTOR_VERSION, "ignored", None, "unsupported_event"
        )
        with self.mem.transaction() as conn:
            conn.execute(
                "INSERT INTO MemoryConsolidationReceipts "
                "(ExtractorVersion,SourceEventId,SourceJournalSequence,SourcePayloadHash,"
                "Disposition,ProposalId,ReasonCode,ReceiptHash) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    body["extractor_version"], body["source_event_id"],
                    body["source_journal_sequence"], body["source_payload_hash"],
                    body["disposition"], body["proposal_id"], body["reason_code"],
                    receipt_hash,
                ),
            )
        with self.assertRaises(ConsolidationCollisionError):
            self._scan()

    def test_approve_new_is_atomic_and_does_not_touch_knowledge(self):
        event = self._append_event("approve-new")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        knowledge_before = self.mem.count("Knowledge")
        result = self._decide(proposal)
        self.assertEqual(result["status"], "approved")
        self.assertFalse(result["idempotent"])
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 1)
        self.assertEqual(self.mem.count("MemoryClaimEvidence"), 1)
        self.assertEqual(self.mem.count("MemoryClaimProposalDecisions"), 1)
        self.assertEqual(self.mem.count("Knowledge"), knowledge_before)
        evidence = self.mem.query_strict(
            "SELECT ClaimId,EventId,EvidenceType FROM MemoryClaimEvidence"
        )[0]
        self.assertEqual(evidence["ClaimId"], result["claim_id"])
        self.assertEqual(evidence["EventId"], event["EventId"])
        self.assertEqual(evidence["EvidenceType"], "direct")
        decision_event = self.repo.get_event(result["decision_event_id"])
        self.assertEqual(decision_event["EventType"], "memory.claim_proposal_decided")

    def test_same_operation_replays_and_altered_command_collides(self):
        self._append_event("decision-replay")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        first = self._decide(proposal)
        replay = self._decide(proposal)
        self.assertEqual(first["decision_id"], replay["decision_id"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 1)
        with self.assertRaises(IdempotencyCollisionError):
            self._decide(proposal, reason="different command")
        with self.assertRaises(IdempotencyCollisionError):
            self._decide(proposal, actor_type="admin")

    def test_different_operation_cannot_redecide_proposal(self):
        self._append_event("decision-conflict")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        self._decide(proposal)
        with self.assertRaises(ProposalDecisionConflictError):
            self._decide(proposal, operation_id=OP2)

    def test_digest_mismatch_and_invalid_action_write_nothing(self):
        self._append_event("digest-mismatch")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        with self.assertRaises(ProposalValidationError):
            self._decide(proposal, proposal_digest="f" * 64)
        with self.assertRaises(ProposalValidationError):
            self._decide(proposal, action="auto_apply")
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 0)
        self.assertEqual(self.mem.count("MemoryClaimProposalDecisions"), 0)

    def test_proposal_conflict_set_must_still_be_fresh_at_decision(self):
        self._append_event("stale-proposal")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        self._insert_claim("late-conflict", object_value="tea")
        with self.assertRaises(ProposalDecisionConflictError):
            self._decide(proposal)
        self.assertEqual(self.mem.count("MemoryClaimProposalDecisions"), 0)
        self.assertEqual(self.mem.count("MemoryClaimEvidence"), 0)
        rejected = self._decide(proposal, action="reject", operation_id=OP2)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 1)

    def test_conflict_membership_is_sealed_and_digest_rechecked_on_approval(self):
        import bridge.phase2_consolidation_schema as consolidation_schema

        self._insert_claim("old-a", object_value="tea")
        self._append_event("sealed-conflicts")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        self._insert_claim("old-b", object_value="juice")
        with self.mem.transaction() as conn:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "membership change not allowed"
            ):
                conn.execute(
                    "INSERT INTO MemoryClaimProposalConflicts "
                    "(ProposalId,ClaimId,ConflictType,ExistingObjectHash) "
                    "VALUES (?,?,?,?)",
                    (
                        proposal["ProposalId"], "old-b", "contradiction",
                        _sha256("juice"),
                    ),
                )

        seal_sql = next(
            sql for name, _table, sql in consolidation_schema.V2_TRIGGERS
            if name == "trg_proposal_conflicts_sealed"
        )
        with self.mem.transaction() as conn:
            conn.execute("DROP TRIGGER trg_proposal_conflicts_sealed")
            conn.execute(
                "INSERT INTO MemoryClaimProposalConflicts "
                "(ProposalId,ClaimId,ConflictType,ExistingObjectHash) "
                "VALUES (?,?,?,?)",
                (
                    proposal["ProposalId"], "old-b", "contradiction",
                    _sha256("juice"),
                ),
            )
            conn.execute(seal_sql)
        with self.assertRaises(ProposalDecisionConflictError):
            self._decide(
                proposal,
                action="supersede_existing",
                target_claim_ids=["old-a", "old-b"],
            )
        self.assertEqual(self.mem.count("MemoryClaimProposalDecisions"), 0)
        self.assertEqual(self.mem.count("MemoryClaimEvidence"), 0)
        self.assertEqual(self.mem.count("MemoryClaimResolutions"), 0)
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 2)

    def test_decision_targets_must_be_unique_and_sorted(self):
        self._insert_claim("old-a", object_value="tea")
        self._insert_claim("old-b", object_value="juice")
        self._append_event("target-order")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        targets = [row["ClaimId"] for row in proposal["conflicts"]]
        with self.assertRaises(ProposalValidationError):
            self._decide(
                proposal, action="supersede_existing",
                target_claim_ids=list(reversed(targets)),
            )
        with self.assertRaises(ProposalValidationError):
            self._decide(
                proposal, action="supersede_existing",
                target_claim_ids=[targets[0], targets[0]],
            )

    def test_decision_targets_preserve_binary_claim_ids_exactly(self):
        repeated = "old  claim"
        self._insert_claim(repeated, object_value="tea")
        self._append_event("binary-id-supersede")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        self.assertEqual(proposal["conflicts"][0]["ClaimId"], repeated)
        result = self._decide(
            proposal, action="supersede_existing",
            target_claim_ids=[repeated],
        )
        self.assertTrue(result["claim_id"].startswith("clm-"))

        decomposed = "cafe\u0301"
        self._insert_claim(
            decomposed, predicate="favorite_drink", object_value="coffee"
        )
        self._append_event(
            "binary-id-confirm",
            payload={
                "entity": "User", "relation": "favorite_drink",
                "value": "coffee", "confidence": 0.9,
            },
        )
        confirmation = self._proposal(self._scan()["proposal_ids"][0])
        self.assertEqual(confirmation["conflicts"][0]["ClaimId"], decomposed)
        confirmed = self._decide(
            confirmation, operation_id=OP2, action="confirm_existing",
            target_claim_ids=[decomposed],
        )
        self.assertEqual(confirmed["claim_id"], decomposed)

    def test_binary_distinct_claim_ids_have_distinct_digests_and_audit_bytes(self):
        decomposed = "cafe\u0301"
        precomposed = "caf\u00e9"
        event = {
            "EventId": "binary-digest-event",
            "JournalSequence": 1,
            "PayloadHash": "a" * 64,
        }
        claim = {
            "Subject": "User", "Predicate": "likes", "Object": "coffee",
            "Confidence": 0.9, "Trust": 0.9, "DecayRate": 0.01,
            "Sensitivity": "private", "ConsentScope": "local_only",
            "ObservedAt": NOW_ISO, "EvidenceType": "direct",
        }
        conflicts_a = [{
            "ClaimId": decomposed, "ConflictType": "confirmation",
            "ExistingObjectHash": "b" * 64,
        }]
        conflicts_b = [{
            "ClaimId": precomposed, "ConflictType": "confirmation",
            "ExistingObjectHash": "b" * 64,
        }]
        proposal_a = _proposal_values(
            event, EXTRACTOR_VERSION, claim, "confirmation", conflicts_a
        )
        proposal_b = _proposal_values(
            event, EXTRACTOR_VERSION, claim, "confirmation", conflicts_b
        )
        self.assertEqual(proposal_a[0], proposal_b[0])
        self.assertNotEqual(proposal_a[1], proposal_b[1])

        self._insert_claim(decomposed, object_value="coffee")
        self._append_event("binary-audit")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        result = self._decide(
            proposal, action="confirm_existing",
            target_claim_ids=[decomposed],
        )
        with self.assertRaises(IdempotencyCollisionError):
            self._decide(
                proposal, action="confirm_existing",
                target_claim_ids=[precomposed],
            )
        event_row = self.repo.get_event(result["decision_event_id"])
        audit = json.loads(event_row["Payload"])
        target_hex = audit["command"]["target_claim_ids_utf8_hex"][0]
        claim_hex = audit["result"]["claim_id_utf8_hex"]
        self.assertEqual(bytes.fromhex(target_hex).decode("utf-8"), decomposed)
        self.assertEqual(bytes.fromhex(claim_hex).decode("utf-8"), decomposed)
        self.assertNotEqual(target_hex, precomposed.encode("utf-8").hex())

    def test_binary_distinct_claim_ids_get_distinct_resolution_ids(self):
        decomposed = "cafe\u0301"
        precomposed = "caf\u00e9"
        self._insert_claim(decomposed, object_value="tea")
        self._insert_claim(precomposed, object_value="juice")
        self._append_event("binary-resolution-ids")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        targets = [row["ClaimId"] for row in proposal["conflicts"]]
        self.assertEqual(len(targets), 2)
        self._decide(
            proposal, action="supersede_existing", target_claim_ids=targets
        )
        resolutions = self.mem.query_strict(
            "SELECT ResolutionId,ClaimId FROM MemoryClaimResolutions "
            "ORDER BY ClaimId COLLATE BINARY"
        )
        self.assertEqual(len(resolutions), 2)
        self.assertEqual({row["ClaimId"] for row in resolutions}, {decomposed, precomposed})
        self.assertEqual(len({row["ResolutionId"] for row in resolutions}), 2)

    def test_source_event_type_drift_blocks_approval_but_allows_reject(self):
        self._append_event("event-type-drift")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        with self.mem.transaction() as conn:
            trigger_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_events_no_update'"
            ).fetchone()[0]
            conn.execute("DROP TRIGGER trg_events_no_update")
            conn.execute(
                "UPDATE MemoryEvents SET EventType='conversation.user_observed' "
                "WHERE EventId=?", (proposal["SourceEventId"],),
            )
            conn.execute(trigger_sql)
        with self.assertRaises(ProposalDecisionConflictError):
            self._decide(proposal)
        rejected = self._decide(proposal, operation_id=OP2, action="reject")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 0)

    def test_reject_is_terminal_and_writes_no_claim(self):
        self._append_event("reject")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        result = self._decide(proposal, action="reject", reason="not correct")
        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(result["claim_id"])
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 0)
        listed = list_claim_proposals(self.mem, status="rejected")
        self.assertEqual(listed[0]["Status"], "rejected")

    def test_confirm_existing_adds_corroborating_evidence_only(self):
        self._insert_claim("existing-confirm", object_value="coffee")
        self._append_event("confirm-decision")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        result = self._decide(
            proposal,
            action="confirm_existing",
            target_claim_ids=["existing-confirm"],
        )
        self.assertEqual(result["claim_id"], "existing-confirm")
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 1)
        self.assertEqual(
            self.mem.query_strict("SELECT EvidenceType FROM MemoryClaimEvidence")[0]["EvidenceType"],
            "corroborated",
        )
        self.assertEqual(
            self.mem.query_strict("SELECT Action FROM MemoryClaimResolutions")[0]["Action"],
            "confirm",
        )

    def test_confirm_existing_rejects_target_resolved_after_scan(self):
        self._insert_claim("resolved-confirm", object_value="coffee")
        self._append_event("resolved-confirm-proposal")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        self._resolve("resolved-confirm", "retract")
        with self.assertRaises(ProposalDecisionConflictError):
            self._decide(
                proposal, action="confirm_existing",
                target_claim_ids=["resolved-confirm"],
            )
        self.assertEqual(self.mem.count("MemoryClaimEvidence"), 0)
        self.assertEqual(self.mem.count("MemoryClaimProposalDecisions"), 0)

    def test_keep_both_creates_new_without_terminal_resolution(self):
        self._insert_claim(
            "existing-keep", object_value="tea", observed_at="2027-01-01T00:00:00Z"
        )
        self._append_event("keep-both")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        result = self._decide(proposal, action="keep_both")
        self.assertNotEqual(result["claim_id"], "existing-keep")
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 2)
        self.assertEqual(self.mem.count("MemoryClaimResolutions"), 0)

    def test_mixed_confirmation_conflict_cannot_keep_duplicate_claim(self):
        self._insert_claim("same-object", object_value="coffee")
        self._insert_claim("different-object", object_value="tea")
        self._append_event("mixed-conflicts")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        self.assertEqual(proposal["Classification"], "contradiction")
        self.assertEqual(
            {row["ConflictType"] for row in proposal["conflicts"]},
            {"confirmation", "contradiction"},
        )
        with self.assertRaises(ProposalValidationError):
            self._decide(proposal, action="keep_both")
        targets = [row["ClaimId"] for row in proposal["conflicts"]]
        result = self._decide(
            proposal, action="supersede_existing", target_claim_ids=targets,
        )
        self.assertEqual(self.mem.count("MemoryClaimResolutions"), 2)
        self.assertTrue(result["claim_id"].startswith("clm-"))

    def test_supersede_requires_exact_sorted_conflict_set(self):
        self._insert_claim("old-a", object_value="tea")
        self._insert_claim("old-b", object_value="juice")
        self._append_event("supersede")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        targets = [row["ClaimId"] for row in proposal["conflicts"]]
        with self.assertRaises(ProposalValidationError):
            self._decide(
                proposal, action="supersede_existing",
                target_claim_ids=targets[:1],
            )
        result = self._decide(
            proposal, action="supersede_existing", target_claim_ids=targets,
        )
        self.assertEqual(self.mem.count("MemoryClaimResolutions"), 2)
        active = self.mem.query_strict(
            "SELECT ClaimId FROM MemorySemanticClaims c WHERE NOT EXISTS ("
            "SELECT 1 FROM MemoryClaimResolutions r WHERE r.ClaimId=c.ClaimId "
            "AND r.Action IN ('deny','supersede','retract','merge'))"
        )
        self.assertEqual(active, [{"ClaimId": result["claim_id"]}])

    def test_decision_failure_rolls_back_claim_evidence_and_event(self):
        self._append_event("decision-rollback")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        original_append = self.repo.append_event

        def fail_after_claim(*args, **kwargs):
            raise RuntimeError("injected")

        with mock.patch.object(self.repo, "append_event", side_effect=fail_after_claim):
            with self.assertRaises(RuntimeError):
                self._decide(proposal)
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 0)
        self.assertEqual(self.mem.count("MemoryClaimEvidence"), 0)
        self.assertEqual(self.mem.count("MemoryClaimProposalDecisions"), 0)
        self.repo.append_event = original_append

    def test_concurrent_same_operation_creates_one_decision(self):
        self._append_event("concurrent")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        results = []
        errors = []
        lock = threading.Lock()

        def worker():
            try:
                value = self._decide(proposal)
                with lock:
                    results.append(value)
            except Exception as exc:
                with lock:
                    errors.append(type(exc).__name__)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(not row["idempotent"] for row in results), 1)
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 1)
        self.assertEqual(self.mem.count("MemoryClaimProposalDecisions"), 1)

    def test_mixed_ordinary_append_and_decision_do_not_deadlock(self):
        self._append_event("mixed-lock-order-source")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        barrier = threading.Barrier(2)
        results = []
        errors = []
        result_lock = threading.Lock()

        def ordinary_append():
            try:
                barrier.wait(timeout=5)
                event = self.repo.append_event(
                    stream_id="mixed-lock-order:ordinary",
                    event_type="test.mixed_append",
                    payload={"kind": "ordinary"},
                    actor_type="system", origin="test",
                    trust=1.0, sensitivity="normal",
                    consent_scope="local_only",
                    idempotency_key="mixed-lock-order-ordinary",
                )
                with result_lock:
                    results.append(("event", event["EventId"]))
            except Exception as exc:
                with result_lock:
                    errors.append(("event", type(exc).__name__))

        def approve_proposal():
            try:
                barrier.wait(timeout=5)
                decision = self._decide(proposal)
                with result_lock:
                    results.append(("decision", decision["decision_id"]))
            except Exception as exc:
                with result_lock:
                    errors.append(("decision", type(exc).__name__))

        threads = [
            threading.Thread(target=ordinary_append, daemon=True),
            threading.Thread(target=approve_proposal, daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual({kind for kind, _value in results}, {"event", "decision"})
        self.assertEqual(self.mem.count("MemoryClaimProposalDecisions"), 1)
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 1)

    def test_list_and_get_derive_status_from_immutable_decision(self):
        self._append_event("list-a")
        self._append_event("list-b")
        self._scan()
        proposals = list_claim_proposals(self.mem, status="pending")
        self.assertEqual(len(proposals), 2)
        first = get_claim_proposal(self.mem, proposals[0]["ProposalId"])
        self._decide(first, action="reject")
        self.assertEqual(len(list_claim_proposals(self.mem, status="pending")), 1)
        self.assertEqual(len(list_claim_proposals(self.mem, status="rejected")), 1)
        self.assertEqual(len(list_claim_proposals(self.mem, status="approved")), 0)

    def test_reason_is_credential_redacted(self):
        self._append_event("reason-redaction")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        credential = "Bearer " + "examplecredentialmaterial" * 2
        self._decide(proposal, action="reject", reason="token " + credential)
        reason = self.mem.query_strict(
            "SELECT Reason FROM MemoryClaimProposalDecisions"
        )[0]["Reason"]
        self.assertNotIn(credential, reason)

    def test_lone_surrogate_reason_is_validation_error_and_operation_can_retry(self):
        self._append_event("surrogate-reason")
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        for surrogate in ("\ud800", "\udc00"):
            with self.assertRaises(ProposalValidationError):
                self._decide(proposal, reason="bad" + surrogate)
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 0)
        self.assertEqual(self.mem.count("MemoryClaimProposalDecisions"), 0)
        valid = self._decide(proposal, reason="valid retry")
        self.assertEqual(valid["status"], "approved")

    def test_multibyte_target_set_over_audit_budget_fails_before_writes(self):
        target_ids = []
        with self.mem.transaction() as conn:
            for index in range(40):
                claim_id = f"{index:03d}" + "😀" * 253
                target_ids.append(claim_id)
                conn.execute(
                    "INSERT INTO MemorySemanticClaims "
                    "(ClaimId,Subject,Predicate,Object,Confidence,Trust,ObservedAt) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        claim_id, "User", "favorite_color", f"old-{index}",
                        0.8, 0.9, "2026-07-01T00:00:00Z",
                    ),
                )
        self._append_event(
            "oversize-audit",
            payload={
                "entity": "User", "relation": "favorite_color", "value": "blue",
                "confidence": 0.9,
            },
        )
        proposal = self._proposal(self._scan()["proposal_ids"][0])
        self.assertEqual(
            [row["ClaimId"] for row in proposal["conflicts"]], target_ids
        )
        with self.assertRaisesRegex(
            ProposalValidationError, "journal byte limit"
        ):
            self._decide(
                proposal, action="supersede_existing",
                target_claim_ids=target_ids,
            )
        self.assertEqual(self.mem.count("MemoryClaimProposalDecisions"), 0)
        self.assertEqual(self.mem.count("MemoryClaimEvidence"), 0)
        self.assertEqual(self.mem.count("MemoryClaimResolutions"), 0)
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 40)

    def test_no_network_calls_during_scan_or_decision(self):
        self._append_event("no-network")
        original_connect = socket.socket.connect
        calls = []

        def blocked(sock, address):
            calls.append(address)
            raise AssertionError("network attempted")

        socket.socket.connect = blocked
        try:
            proposal = self._proposal(self._scan()["proposal_ids"][0])
            self._decide(proposal)
        finally:
            socket.socket.connect = original_connect
        self.assertEqual(calls, [])


class ConsolidationSchemaTests(unittest.TestCase):
    def _fresh_conn(self):
        tmpdir = tempfile.mkdtemp(prefix="eva_p2_schema_v2_")
        path = os.path.join(tmpdir, "schema.db")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        run_migrations(conn)
        return conn, tmpdir

    def test_v1_checksum_is_frozen_and_v2_is_additive(self):
        import bridge.phase2_schema as schema

        self.assertEqual(
            schema._manifest_hash(1),
            "56fc41bc87a6fee931125daf0611c0264f7fb69325f6062fcba4bf7e801f4dca",
        )
        conn, tmpdir = self._fresh_conn()
        original = schema._PHASE2_MIGRATIONS
        try:
            schema._PHASE2_MIGRATIONS = [original[0]]
            self.assertEqual(run_phase2_migrations(conn), 1)
            self.assertEqual(_current_version(conn), 1)
            schema._PHASE2_MIGRATIONS = original
            self.assertEqual(run_phase2_migrations(conn), 1)
            self.assertEqual(_current_version(conn), 2)
            self.assertEqual(verify_phase2_schema(conn), 2)
        finally:
            schema._PHASE2_MIGRATIONS = original
            conn.close()
            shutil.rmtree(tmpdir)

    def test_every_v2_noop_trigger_is_detected_at_runtime(self):
        import bridge.phase2_consolidation_schema as consolidation_schema

        for index, (name, table, sql) in enumerate(
            list(consolidation_schema.V2_TRIGGERS)
        ):
            with self.subTest(trigger=name):
                conn, tmpdir = self._fresh_conn()
                try:
                    run_phase2_migrations(conn)
                    if name.endswith("_no_replace"):
                        operation = "INSERT"
                    elif name.endswith("_no_update"):
                        operation = "UPDATE"
                    else:
                        operation = "DELETE"
                    no_op = (
                        f"CREATE TRIGGER {name} BEFORE {operation} ON {table} "
                        "BEGIN SELECT 1; END"
                    )
                    conn.execute(f"DROP TRIGGER {name}")
                    conn.execute(no_op)
                    consolidation_schema.V2_TRIGGERS[index] = (name, table, no_op)
                    try:
                        with self.assertRaises(Phase2SchemaVerificationError):
                            verify_phase2_schema(conn)
                    finally:
                        consolidation_schema.V2_TRIGGERS[index] = (name, table, sql)
                finally:
                    conn.close()
                    shutil.rmtree(tmpdir)

    def test_schema_verifier_coexists_with_real_verify_v1_extractor_data(self):
        tmpdir = tempfile.mkdtemp(prefix="eva_p2_verify_identity_")
        memory = SqliteMemory(os.path.join(tmpdir, "memory.db"))
        repo = memory.event_repository()
        try:
            repo.append_event(
                stream_id="verify:v1:real",
                event_type="memory.fact_candidate_extracted",
                payload={
                    "entity": "User", "relation": "likes", "value": "coffee",
                    "confidence": 0.9,
                }, actor_type="system", origin="test", occurred_at=NOW_ISO,
                trust=0.9, sensitivity="private", consent_scope="local_only",
                idempotency_key="real-verify-v1",
            )
            scan_claim_proposals(
                memory, extractor_version="verify-v1", clock=lambda: NOW
            )
            before = {
                table: memory.count(table)
                for table in (
                    "MemoryClaimProposals", "MemoryConsolidationReceipts",
                    "MemoryConsolidationCheckpoints",
                )
            }
            self.assertEqual(verify_phase2_schema(memory._conn()), 2)
            after = {table: memory.count(table) for table in before}
            self.assertEqual(after, before)
        finally:
            memory.close()
            shutil.rmtree(tmpdir)

    def test_v2_text_ids_reject_nul_and_blob_bypasses(self):
        conn, tmpdir = self._fresh_conn()
        try:
            run_phase2_migrations(conn)
            conn.execute(
                "INSERT INTO MemoryEvents (EventId,StreamId,StreamVersion,EventType,"
                "SchemaVersion,ActorType,ActorId,Origin,OccurredAt,CorrelationId,"
                "CausationId,SessionId,TurnId,SourceMessageId,Trust,Sensitivity,"
                "ConsentScope,Payload,PayloadHash,EventHash,IdempotencyKey) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "schema-text-event", "schema:text", 0,
                    "memory.fact_candidate_extracted", 1, "system", "", "test",
                    NOW_ISO, "", "", "", "", "", 0.9, "private", "local_only",
                    "{}", "a" * 64, "b" * 64, "schema-text-event",
                ),
            )
            event = conn.execute(
                "SELECT EventId,JournalSequence,PayloadHash FROM MemoryEvents "
                "WHERE EventId='schema-text-event'"
            ).fetchone()
            proposal_sql = (
                "INSERT INTO MemoryClaimProposals "
                "(ProposalId,SourceEventId,SourceJournalSequence,SourcePayloadHash,"
                "ProposalDigest,ExtractorVersion,Subject,Predicate,Object,Confidence,"
                "Trust,Sensitivity,ConsentScope,ObservedAt,Classification,EvidenceType) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            )
            base = [
                "1" * 64, event[0], event[1], event[2], "2" * 64,
                "test-v1", "User", "likes", "coffee", 0.9, 0.9,
                "private", "local_only", NOW_ISO, "new", "direct",
            ]
            for label, position, value in (
                ("nul_proposal_id", 0, "1" * 64 + "\x00suffix"),
                ("blob_proposal_id", 0, b"1" * 64),
                ("nul_subject", 6, "User\x00" + "x" * 1000),
                ("blob_subject", 6, b"User"),
            ):
                values = list(base)
                values[position] = value
                with self.subTest(case=label):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(proposal_sql, tuple(values))
        finally:
            conn.close()
            shutil.rmtree(tmpdir)

    def test_alternate_unique_key_replacements_are_all_blocked(self):
        tmpdir = tempfile.mkdtemp(prefix="eva_p2_alt_unique_")
        memory = SqliteMemory(os.path.join(tmpdir, "memory.db"))
        repo = memory.event_repository()
        try:
            first_event = repo.append_event(
                stream_id="alt:first", event_type="memory.fact_candidate_extracted",
                payload={
                    "entity": "User", "relation": "likes", "value": "coffee",
                    "confidence": 0.9,
                },
                actor_type="system", origin="test", occurred_at=NOW_ISO,
                trust=0.9, sensitivity="private", consent_scope="local_only",
                idempotency_key="alt-first",
            )
            repo.append_event(
                stream_id="alt:second", event_type="memory.fact_candidate_extracted",
                payload={
                    "entity": "User", "relation": "likes", "value": "tea",
                    "confidence": 0.9,
                },
                actor_type="system", origin="test", occurred_at=NOW_ISO,
                trust=0.9, sensitivity="private", consent_scope="local_only",
                idempotency_key="alt-second",
            )
            scan_claim_proposals(memory, clock=lambda: NOW)
            proposals = memory.query_strict(
                "SELECT * FROM MemoryClaimProposals ORDER BY SourceJournalSequence"
            )
            first, second = proposals
            decide_claim_proposal(
                memory, repo,
                proposal_id=first["ProposalId"],
                proposal_digest=first["ProposalDigest"], operation_id=OP1,
                actor_type="user", origin="test", action="approve_new",
                clock=lambda: NOW,
            )
            decision = memory.query_strict(
                "SELECT * FROM MemoryClaimProposalDecisions"
            )[0]
            extra_events = [
                repo.append_event(
                    stream_id=f"alt:decision:{index}", event_type="phase2.test",
                    payload={"index": index}, actor_type="system", origin="test",
                    trust=1.0, sensitivity="normal", consent_scope="local_only",
                    idempotency_key=f"alt-decision-{index}",
                )
                for index in range(3)
            ]
            with memory.transaction() as conn:
                proposal_values = [
                    "a" * 64, first["SourceEventId"], first["SourceJournalSequence"],
                    first["SourcePayloadHash"], "b" * 64,
                    first["ExtractorVersion"], first["Subject"], first["Predicate"],
                    "alternate", first["Confidence"], first["Trust"],
                    first["DecayRate"], first["Sensitivity"], first["ConsentScope"],
                    first["ObservedAt"], first["Classification"], first["EvidenceType"],
                ]
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "replacement not allowed"
                ):
                    conn.execute(
                        "INSERT OR REPLACE INTO MemoryClaimProposals "
                        "(ProposalId,SourceEventId,SourceJournalSequence,SourcePayloadHash,"
                        "ProposalDigest,ExtractorVersion,Subject,Predicate,Object,Confidence,"
                        "Trust,DecayRate,Sensitivity,ConsentScope,ObservedAt,Classification,"
                        "EvidenceType) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        proposal_values,
                    )

                receipt = memory.query_strict(
                    "SELECT * FROM MemoryConsolidationReceipts "
                    "WHERE SourceEventId=?", (first_event["EventId"],),
                )[0]
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "replacement not allowed"
                ):
                    conn.execute(
                        "INSERT OR REPLACE INTO MemoryConsolidationReceipts "
                        "(ExtractorVersion,SourceEventId,SourceJournalSequence,"
                        "SourcePayloadHash,Disposition,ProposalId,ReasonCode,ReceiptHash) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            receipt["ExtractorVersion"], second["SourceEventId"],
                            receipt["SourceJournalSequence"], second["SourcePayloadHash"],
                            "ignored", None, "unsupported_event", "c" * 64,
                        ),
                    )

                decision_sql = (
                    "INSERT OR REPLACE INTO MemoryClaimProposalDecisions "
                    "(DecisionId,ProposalId,ProposalDigest,OperationId,CommandHash,Action,"
                    "ClaimId,DecisionEventId,ActorType) VALUES (?,?,?,?,?,?,?,?,?)"
                )
                variants = (
                    (
                        "d" * 64, first["ProposalId"], first["ProposalDigest"], OP2,
                        "e" * 64, "approve_new", decision["ClaimId"],
                        extra_events[0]["EventId"], "user",
                    ),
                    (
                        "d" * 64, second["ProposalId"], second["ProposalDigest"], OP1,
                        "e" * 64, "approve_new", decision["ClaimId"],
                        extra_events[1]["EventId"], "user",
                    ),
                    (
                        "d" * 64, second["ProposalId"], second["ProposalDigest"], OP2,
                        "e" * 64, "approve_new", decision["ClaimId"],
                        decision["DecisionEventId"], "user",
                    ),
                )
                for values in variants:
                    with self.assertRaisesRegex(
                        sqlite3.IntegrityError, "replacement not allowed"
                    ):
                        conn.execute(decision_sql, values)
            self.assertEqual(verify_phase2_schema(memory._conn()), 2)
        finally:
            memory.close()
            shutil.rmtree(tmpdir)

    def test_receipt_composite_fk_rejects_cross_event_proposal(self):
        tmpdir = tempfile.mkdtemp(prefix="eva_p2_cross_receipt_")
        memory = SqliteMemory(os.path.join(tmpdir, "memory.db"))
        repo = memory.event_repository()
        try:
            first = repo.append_event(
                stream_id="cross:first", event_type="memory.fact_candidate_extracted",
                payload={
                    "entity": "User", "relation": "likes", "value": "coffee",
                    "confidence": 0.9,
                }, actor_type="system", origin="test", occurred_at=NOW_ISO,
                trust=0.9, sensitivity="private", consent_scope="local_only",
                idempotency_key="cross-first",
            )
            scan_claim_proposals(memory, clock=lambda: NOW)
            proposal = memory.query_strict(
                "SELECT * FROM MemoryClaimProposals"
            )[0]
            second = repo.append_event(
                stream_id="cross:second", event_type="memory.fact_candidate_extracted",
                payload={
                    "entity": "User", "relation": "likes", "value": "tea",
                    "confidence": 0.9,
                }, actor_type="system", origin="test", occurred_at=NOW_ISO,
                trust=0.9, sensitivity="private", consent_scope="local_only",
                idempotency_key="cross-second",
            )
            with memory.transaction() as conn:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO MemoryConsolidationReceipts "
                        "(ExtractorVersion,SourceEventId,SourceJournalSequence,"
                        "SourcePayloadHash,Disposition,ProposalId,ReasonCode,ReceiptHash) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (
                            proposal["ExtractorVersion"], second["EventId"],
                            second["JournalSequence"], second["PayloadHash"],
                            "proposed", proposal["ProposalId"], "proposed", "f" * 64,
                        ),
                    )
            self.assertNotEqual(first["EventId"], second["EventId"])
        finally:
            memory.close()
            shutil.rmtree(tmpdir)

    def test_decision_composite_fk_rejects_wrong_proposal_digest(self):
        tmpdir = tempfile.mkdtemp(prefix="eva_p2_wrong_digest_")
        memory = SqliteMemory(os.path.join(tmpdir, "memory.db"))
        repo = memory.event_repository()
        try:
            repo.append_event(
                stream_id="digest:source", event_type="memory.fact_candidate_extracted",
                payload={
                    "entity": "User", "relation": "likes", "value": "coffee",
                    "confidence": 0.9,
                }, actor_type="system", origin="test", occurred_at=NOW_ISO,
                trust=0.9, sensitivity="private", consent_scope="local_only",
                idempotency_key="digest-source",
            )
            scan_claim_proposals(memory, clock=lambda: NOW)
            proposal = memory.query_strict("SELECT * FROM MemoryClaimProposals")[0]
            decision_event = repo.append_event(
                stream_id="digest:decision", event_type="phase2.test",
                payload={"test": True}, actor_type="system", origin="test",
                trust=1.0, sensitivity="normal", consent_scope="local_only",
                idempotency_key="digest-decision",
            )
            with memory.transaction() as conn:
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO MemoryClaimProposalDecisions "
                        "(DecisionId,ProposalId,ProposalDigest,OperationId,CommandHash,"
                        "Action,ClaimId,DecisionEventId,ActorType) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            "c" * 64, proposal["ProposalId"],
                            proposal["ProposalDigest"], "-" * 36, "e" * 64,
                            "reject", None, decision_event["EventId"], "user",
                        ),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO MemoryClaimProposalDecisions "
                        "(DecisionId,ProposalId,ProposalDigest,OperationId,CommandHash,"
                        "Action,ClaimId,DecisionEventId,ActorType) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            "d" * 64, proposal["ProposalId"], "f" * 64, OP1,
                            "e" * 64, "reject", None,
                            decision_event["EventId"], "user",
                        ),
                    )
        finally:
            memory.close()
            shutil.rmtree(tmpdir)


class ConsolidationEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="eva_p2_endpoint_")
        self.mem = SqliteMemory(os.path.join(self.tmpdir, "memory.db"))
        self.repo = self.mem.event_repository()
        self.repo.append_event(
            stream_id="endpoint:test",
            event_type="memory.fact_candidate_extracted",
            payload={
                "entity": "User", "relation": "likes", "value": "coffee",
                "confidence": 0.9,
            },
            actor_type="system", actor_id="eva", origin="test",
            occurred_at=NOW_ISO, trust=0.9, sensitivity="private",
            consent_scope="local_only", idempotency_key="endpoint-fact",
        )

    def tearDown(self):
        self.mem.close()
        shutil.rmtree(self.tmpdir)

    @staticmethod
    def _envelope(request_id=OP1):
        class Envelope:
            pass

        envelope = Envelope()
        envelope.request_id = request_id
        envelope.user_id = "endpoint-user"
        envelope.origin = "api"
        envelope.correlation_id = request_id
        envelope.session_id = ""
        envelope.turn_id = "33333333-3333-4333-8333-333333333333"
        return envelope

    @staticmethod
    def _handler(body=None, path="/v1/memory/claim-proposals"):
        class Handler:
            def __init__(self):
                self.path = path
                self.responses = []
                self.body = body if body is not None else {}

            def _json_response(self, status, payload):
                self.responses.append((status, payload))

            def _claim_proposals_enabled(self):
                return True

            def _read_json_body(self):
                return self.body, ""

            def _build_envelope(self, data, **kwargs):
                return ConsolidationEndpointTests._envelope(
                    data.get("request_id", OP1)
                )

        return Handler()

    def test_feature_gate_requires_loopback_and_explicit_mode(self):
        from bridge import core

        handler = self._handler()
        with mock.patch.object(core._st, "bridge_auth_token", ""):
            self.assertFalse(core.BridgeHandler._claim_proposals_enabled(handler))
        self.assertEqual(handler.responses[-1][0], 401)

        handler = self._handler()
        with mock.patch.object(core._st, "bridge_auth_token", "configured"), \
                mock.patch.object(core, "_is_loopback_bind", return_value=False):
            self.assertFalse(core.BridgeHandler._claim_proposals_enabled(handler))
        self.assertEqual(handler.responses[-1][0], 403)

        handler = self._handler()
        with mock.patch.object(core._st, "bridge_auth_token", "configured"), \
                mock.patch.object(core, "_is_loopback_bind", return_value=True), \
                mock.patch.object(core._cfg, "phase2_effective_enabled", return_value=True), \
                mock.patch.object(
                    core._cfg, "phase2_effective_modes",
                    return_value={"consolidation": "off"},
                ):
            self.assertFalse(core.BridgeHandler._claim_proposals_enabled(handler))
        self.assertEqual(handler.responses[-1][0], 409)

    def test_scan_list_detail_and_operation_decision_handlers(self):
        from bridge import core

        scan = self._handler(
            {"request_id": OP1, "limit": 50},
            "/v1/memory/claim-proposals/scan",
        )
        with mock.patch.object(core, "_get_sqlite_mem", return_value=self.mem):
            core.BridgeHandler._claim_proposals_scan(scan)
        self.assertEqual(scan.responses[0][0], 200)
        self.assertEqual(scan.responses[0][1]["request_id"], OP1)
        proposal_id = scan.responses[0][1]["proposal_ids"][0]

        listing = self._handler(path="/v1/memory/claim-proposals?status=pending")
        with mock.patch.object(core, "_get_sqlite_mem", return_value=self.mem):
            core.BridgeHandler._claim_proposals_list(listing)
        self.assertEqual(listing.responses[0][0], 200)
        self.assertEqual(listing.responses[0][1]["proposals"][0]["ProposalId"], proposal_id)

        detail = self._handler(path=f"/v1/memory/claim-proposals/{proposal_id}")
        with mock.patch.object(core, "_get_sqlite_mem", return_value=self.mem):
            core.BridgeHandler._claim_proposal_get(detail, proposal_id)
        proposal = detail.responses[0][1]["proposal"]
        self.assertEqual(detail.responses[0][0], 200)

        body = {
            "request_id": OP2,
            "proposal_digest": proposal["ProposalDigest"],
            "action": "approve_new",
            "target_claim_ids": [],
            "reason": "approved",
        }
        decision = self._handler(
            body, f"/v1/memory/claim-proposals/{proposal_id}/decide"
        )
        with mock.patch.object(core, "_get_sqlite_mem", return_value=self.mem):
            core.BridgeHandler._claim_proposal_decide(decision, proposal_id)
        self.assertEqual(decision.responses[0][0], 200)
        self.assertFalse(decision.responses[0][1]["idempotent"])
        stored = self.mem.query_strict(
            "SELECT OperationId FROM MemoryClaimProposalDecisions"
        )[0]
        self.assertEqual(stored["OperationId"], OP2)

        replay = self._handler(body)
        with mock.patch.object(core, "_get_sqlite_mem", return_value=self.mem):
            core.BridgeHandler._claim_proposal_decide(replay, proposal_id)
        self.assertEqual(replay.responses[0][0], 200)
        self.assertTrue(replay.responses[0][1]["idempotent"])

        altered_body = dict(body)
        altered_body["reason"] = "different"
        altered = self._handler(altered_body)
        with mock.patch.object(core, "_get_sqlite_mem", return_value=self.mem):
            core.BridgeHandler._claim_proposal_decide(altered, proposal_id)
        self.assertEqual(altered.responses[0][0], 409)

        invalid_body = dict(body)
        invalid_body["request_id"] = "44444444-4444-4444-8444-444444444444"
        invalid_body["action"] = "auto_apply"
        invalid = self._handler(invalid_body)
        with mock.patch.object(core, "_get_sqlite_mem", return_value=self.mem):
            core.BridgeHandler._claim_proposal_decide(invalid, proposal_id)
        self.assertEqual(invalid.responses[0][0], 400)

    def test_handlers_reject_unknown_fields_and_missing_proposal(self):
        from bridge import core

        scan = self._handler({"request_id": OP1, "unexpected": True})
        core.BridgeHandler._claim_proposals_scan(scan)
        self.assertEqual(scan.responses[0][0], 400)

        missing_id = "f" * 64
        detail = self._handler()
        with mock.patch.object(core, "_get_sqlite_mem", return_value=self.mem):
            core.BridgeHandler._claim_proposal_get(detail, missing_id)
        self.assertEqual(detail.responses[0][0], 404)

    def test_non_string_decision_actions_return_http_400(self):
        from bridge import core

        scan_claim_proposals(self.mem, clock=lambda: NOW)
        proposal = self.mem.query_strict(
            "SELECT ProposalId,ProposalDigest FROM MemoryClaimProposals"
        )[0]
        before = self.mem.count("MemoryClaimProposalDecisions")
        for index, action in enumerate(([], {}), start=5):
            body = {
                "request_id": f"{index}" * 8 + "-5555-4555-8555-555555555555",
                "proposal_digest": proposal["ProposalDigest"],
                "action": action,
            }
            handler = self._handler(body)
            with mock.patch.object(core, "_get_sqlite_mem", return_value=self.mem):
                core.BridgeHandler._claim_proposal_decide(
                    handler, proposal["ProposalId"]
                )
            self.assertEqual(handler.responses[0][0], 400)
        self.assertEqual(self.mem.count("MemoryClaimProposalDecisions"), before)

    def test_surrogate_reason_and_oversize_audit_return_http_400(self):
        from bridge import core

        scan_claim_proposals(self.mem, clock=lambda: NOW)
        proposal = self.mem.query_strict(
            "SELECT ProposalId,ProposalDigest FROM MemoryClaimProposals"
        )[0]
        for index, surrogate in enumerate(("\ud800", "\udc00"), start=6):
            handler = self._handler({
                "request_id": f"{index}" * 8 + "-6666-4666-8666-666666666666",
                "proposal_digest": proposal["ProposalDigest"],
                "action": "approve_new",
                "reason": "bad" + surrogate,
            })
            with mock.patch.object(core, "_get_sqlite_mem", return_value=self.mem):
                core.BridgeHandler._claim_proposal_decide(
                    handler, proposal["ProposalId"]
                )
            self.assertEqual(handler.responses[0][0], 400)
        self.assertEqual(self.mem.count("MemoryClaimProposalDecisions"), 0)
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 0)

    def test_oversize_multibyte_decision_audit_returns_http_400(self):
        from bridge import core

        target_ids = []
        with self.mem.transaction() as conn:
            for index in range(40):
                claim_id = f"{index:03d}" + "😀" * 253
                target_ids.append(claim_id)
                conn.execute(
                    "INSERT INTO MemorySemanticClaims "
                    "(ClaimId,Subject,Predicate,Object,Confidence,Trust,ObservedAt) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        claim_id, "User", "favorite_color", f"old-{index}",
                        0.8, 0.9, "2026-07-01T00:00:00Z",
                    ),
                )
        self.repo.append_event(
            stream_id="endpoint:oversize",
            event_type="memory.fact_candidate_extracted",
            payload={
                "entity": "User", "relation": "favorite_color", "value": "blue",
                "confidence": 0.9,
            }, actor_type="system", origin="test", occurred_at=NOW_ISO,
            trust=0.9, sensitivity="private", consent_scope="local_only",
            idempotency_key="endpoint-oversize",
        )
        scan_claim_proposals(self.mem, clock=lambda: NOW)
        proposal = self.mem.query_strict(
            "SELECT ProposalId,ProposalDigest FROM MemoryClaimProposals "
            "WHERE Predicate='favorite_color'"
        )[0]
        handler = self._handler({
            "request_id": "88888888-8888-4888-8888-888888888888",
            "proposal_digest": proposal["ProposalDigest"],
            "action": "supersede_existing",
            "target_claim_ids": target_ids,
        })
        with mock.patch.object(core, "_get_sqlite_mem", return_value=self.mem):
            core.BridgeHandler._claim_proposal_decide(
                handler, proposal["ProposalId"]
            )
        self.assertEqual(handler.responses[0][0], 400)
        self.assertIn("journal byte limit", handler.responses[0][1]["error"]["message"])
        self.assertEqual(self.mem.count("MemoryClaimProposalDecisions"), 0)
        self.assertEqual(self.mem.count("MemoryClaimEvidence"), 0)
        self.assertEqual(self.mem.count("MemoryClaimResolutions"), 0)
        self.assertEqual(self.mem.count("MemorySemanticClaims"), 40)

    def test_durable_scan_collision_returns_http_409(self):
        from bridge import core

        handler = self._handler({"request_id": OP1, "limit": 50})
        with mock.patch(
            "bridge.phase2_consolidation.scan_claim_proposals",
            side_effect=ConsolidationCollisionError("durable conflict"),
        ):
            core.BridgeHandler._claim_proposals_scan(handler)
        self.assertEqual(handler.responses[0][0], 409)
        self.assertIn("durable conflict", handler.responses[0][1]["error"]["message"])

    def test_real_proposal_alternate_key_collision_returns_http_409(self):
        from bridge import core

        source = self.mem.query_strict(
            "SELECT EventId,JournalSequence,PayloadHash FROM MemoryEvents "
            "WHERE EventType='memory.fact_candidate_extracted'"
        )[0]
        with self.mem.transaction() as conn:
            conn.execute(
                "INSERT INTO MemoryClaimProposals "
                "(ProposalId,SourceEventId,SourceJournalSequence,SourcePayloadHash,"
                "ProposalDigest,ExtractorVersion,Subject,Predicate,Object,Confidence,"
                "Trust,Sensitivity,ConsentScope,ObservedAt,Classification,EvidenceType) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "a" * 64, source["EventId"], source["JournalSequence"],
                    source["PayloadHash"], "b" * 64, EXTRACTOR_VERSION,
                    "User", "likes", "forged", 0.9, 0.9, "private",
                    "local_only", NOW_ISO, "new", "direct",
                ),
            )
        handler = self._handler({"request_id": OP1, "limit": 50})
        with mock.patch.object(core, "_get_sqlite_mem", return_value=self.mem):
            core.BridgeHandler._claim_proposals_scan(handler)
        self.assertEqual(handler.responses[0][0], 409)
        self.assertEqual(self.mem.count("MemoryConsolidationReceipts"), 0)
        self.assertEqual(self.mem.count("MemoryConsolidationCheckpoints"), 0)

    def test_real_receipt_sequence_collision_returns_http_409(self):
        from bridge import core

        first = self.mem.query_strict(
            "SELECT EventId,JournalSequence,PayloadHash FROM MemoryEvents "
            "WHERE EventType='memory.fact_candidate_extracted'"
        )[0]
        second = self.repo.append_event(
            stream_id="endpoint:ignored",
            event_type="conversation.user_observed",
            payload={"role": "user", "content": "ignored"},
            actor_type="user", origin="test", occurred_at=NOW_ISO,
            trust=1.0, sensitivity="private", consent_scope="local_only",
            idempotency_key="endpoint-ignored",
        )
        event_view = {
            key: second[key] for key in (
                "JournalSequence", "EventId", "EventType", "OccurredAt", "Trust",
                "Sensitivity", "ConsentScope", "Payload", "PayloadHash",
            )
        }
        body, receipt_hash = _receipt_values(
            event_view, EXTRACTOR_VERSION, "ignored", None, "unsupported_event"
        )
        with self.mem.transaction() as conn:
            conn.execute(
                "INSERT INTO MemoryConsolidationReceipts "
                "(ExtractorVersion,SourceEventId,SourceJournalSequence,SourcePayloadHash,"
                "Disposition,ProposalId,ReasonCode,ReceiptHash) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    body["extractor_version"], body["source_event_id"],
                    first["JournalSequence"], body["source_payload_hash"],
                    body["disposition"], body["proposal_id"], body["reason_code"],
                    receipt_hash,
                ),
            )
        handler = self._handler({"request_id": OP1, "limit": 50})
        with mock.patch.object(core, "_get_sqlite_mem", return_value=self.mem):
            core.BridgeHandler._claim_proposals_scan(handler)
        self.assertEqual(handler.responses[0][0], 409)
        self.assertEqual(self.mem.count("MemoryClaimProposals"), 0)
        self.assertEqual(self.mem.count("MemoryConsolidationCheckpoints"), 0)

    def test_proposal_mode_is_frozen_and_master_gated(self):
        script = (
            "import sys;sys.path.insert(0,'tools');"
            "from bridge.config import validate_phase2_startup,phase2_effective_modes;"
            "ok,msg=validate_phase2_startup();m=phase2_effective_modes();"
            "sys.exit(0 if ok and msg is None and m['consolidation']==EXPECTED else 1)"
        )
        for master, expected in (("1", "proposals"), ("0", "off")):
            env = os.environ.copy()
            for name in (
                "EVA_PHASE2_MEMORY", "EVA_MEMORY_RECALL_MODE",
                "EVA_MEMORY_SEMANTIC_MODE", "EVA_MEMORY_SEMANTIC_QUERY_CONSENT",
                "EVA_MEMORY_CONSOLIDATION", "EVA_MEMORY_ANALYTICS",
            ):
                env.pop(name, None)
            env["EVA_PHASE2_MEMORY"] = master
            env["EVA_MEMORY_CONSOLIDATION"] = "proposals"
            result = subprocess.run(
                [sys.executable, "-c", f"EXPECTED={expected!r};" + script],
                cwd=os.path.dirname(TOOLS_DIR), env=env,
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_background_module_has_no_autonomous_consolidation_callsite(self):
        background_path = os.path.join(TOOLS_DIR, "bridge", "background.py")
        with open(background_path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("scan_claim_proposals", source)
        self.assertNotIn("decide_claim_proposal", source)


class ConsolidationSchemaRollbackTests(unittest.TestCase):
    def _fresh_conn(self):
        tmpdir = tempfile.mkdtemp(prefix="eva_p2_schema_v2_rollback_")
        path = os.path.join(tmpdir, "schema.db")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        run_migrations(conn)
        return conn, tmpdir

    def test_v2_verifier_failure_rolls_back_all_new_tables_and_version(self):
        import bridge.phase2_schema as schema
        import bridge.phase2_consolidation_schema as consolidation_schema

        conn, tmpdir = self._fresh_conn()
        original_migrations = schema._PHASE2_MIGRATIONS
        original_verify = consolidation_schema.verify_consolidation_schema
        try:
            schema._PHASE2_MIGRATIONS = [original_migrations[0]]
            run_phase2_migrations(conn)
            schema._PHASE2_MIGRATIONS = original_migrations

            def fail_verify(*args, **kwargs):
                raise Phase2SchemaVerificationError(2, "test", "injected")

            consolidation_schema.verify_consolidation_schema = fail_verify
            with self.assertRaises(Phase2MigrationError):
                run_phase2_migrations(conn)
            self.assertEqual(_current_version(conn), 1)
            for table in (
                "MemoryClaimProposals", "MemoryClaimProposalConflicts",
                "MemoryConsolidationReceipts", "MemoryClaimProposalDecisions",
            ):
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone())
        finally:
            consolidation_schema.verify_consolidation_schema = original_verify
            schema._PHASE2_MIGRATIONS = original_migrations
            conn.close()
            shutil.rmtree(tmpdir)

    def test_exact_v2_schema_detects_dropped_index(self):
        conn, tmpdir = self._fresh_conn()
        try:
            run_phase2_migrations(conn)
            conn.execute("DROP INDEX idx_claim_proposals_sequence")
            with self.assertRaises(Phase2SchemaVerificationError):
                verify_phase2_schema(conn)
        finally:
            conn.close()
            shutil.rmtree(tmpdir)


if __name__ == "__main__":
    unittest.main(verbosity=2)
