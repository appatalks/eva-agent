"""Deterministic tests for structured learning records and consent."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge import config, learning
from bridge.core import BridgeHandler


class LearningStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_dir = config.EVA_CONFIG_DIR
        config.EVA_CONFIG_DIR = self.tempdir.name
        learning.reset_for_tests()

    def tearDown(self):
        learning.reset_for_tests()
        config.EVA_CONFIG_DIR = self.old_dir
        self.tempdir.cleanup()

    def signal(self, **overrides):
        value = {
            "source": "explicit-user",
            "kind": "feedback",
            "status": "misunderstood",
            "value": "misunderstood",
            "confidence": 1.0,
            "scope": "session",
            "session_id": "session-1",
            "permission_basis": "explicit-user",
            "detail": {"control": "misunderstood"},
        }
        value.update(overrides)
        return value

    def test_record_shape_is_bounded_and_persistent(self):
        row, error = learning.create_signal(self.signal())
        self.assertFalse(error)
        self.assertRegex(row["id"], r"^[0-9a-f-]{36}$")
        self.assertEqual(set(row), {"id", "timestamp", "source", "kind", "status", "value", "confidence", "scope", "session_id", "permission_basis", "retention_days", "expires_at", "applied", "detail"})
        self.assertEqual(learning.list_signals()[0]["detail"], {"control": "misunderstood"})

    def test_validation_rejects_sensitive_and_unbounded_fields(self):
        with self.assertRaises(ValueError):
            learning.create_signal(self.signal(detail={"prompt": "private"}))
        with self.assertRaises(ValueError):
            learning.create_signal(self.signal(value="private response content"))
        with self.assertRaises(ValueError):
            learning.create_signal(self.signal(id="not a safe id"))

    def test_consent_revoke_blocks_future_collection(self):
        learning.update_consent({"explicit_feedback": False})
        row, error = learning.create_signal(self.signal())
        self.assertIsNone(row)
        self.assertEqual(error, "consent_denied")
        self.assertEqual(learning.list_signals(), [])

    def test_routine_tool_consent_is_revocable_and_defaults_off(self):
        self.assertFalse(learning.get_consent()["routine_tools"])
        self.assertTrue(learning.update_consent({"routine_tools": True})["routine_tools"])
        self.assertFalse(learning.update_consent({"routine_tools": False})["routine_tools"])

    def test_routine_outcomes_allowed_voice_is_conservative(self):
        row, error = learning.create_signal({
            "source": "action-result", "kind": "action-outcome", "status": "done", "value": "done",
            "confidence": 0.95, "scope": "session", "session_id": "session-1",
            "permission_basis": "routine-outcome", "detail": {"agent": "browser", "operation": "run"},
        })
        self.assertFalse(error)
        self.assertEqual(row["kind"], "action-outcome")
        row, error = learning.create_signal({
            "source": "voice-inferred", "kind": "voice-diagnostic", "status": "diagnostic", "value": "merged",
            "confidence": 0.4, "scope": "session", "session_id": "session-1",
            "permission_basis": "standing-consent", "detail": {"event": "merged", "chars": 12},
        })
        self.assertIsNone(row)
        self.assertEqual(error, "consent_denied")

    def test_expiry_deletion_and_applied_effect(self):
        row, _ = learning.create_signal(self.signal())
        applied = learning.mark_applied(row["id"], "ask for clarification before proceeding")
        self.assertEqual(applied["applied"]["status"], "applied")
        self.assertEqual(learning.delete_signals(signal_id=row["id"]), 1)
        self.assertEqual(learning.list_signals(), [])

    def test_feedback_guidance_is_signal_linked_and_reversible(self):
        row, _ = learning.create_signal(self.signal(status="misunderstood", value="misunderstood"))
        effect = learning.feedback_effect(row)
        self.assertEqual(effect, "The user recently marked a response as misunderstood; clarify intent before relying on assumptions.")
        learning.mark_applied(row["id"], effect)
        active = learning.list_active_guidance(session_id="session-1")
        self.assertEqual(active[0]["signal_id"], row["id"])
        self.assertEqual(active[0]["guidance"], effect)
        self.assertEqual(learning.delete_signals(signal_id=row["id"]), 1)
        self.assertEqual(learning.list_active_guidance(session_id="session-1"), [])

    def test_feedback_guidance_is_hidden_when_consent_is_revoked(self):
        row, _ = learning.create_signal(self.signal(status="helpful", value="helpful"))
        learning.mark_applied(row["id"], learning.feedback_effect(row))
        self.assertEqual(len(learning.list_active_guidance(session_id="session-1")), 1)
        learning.update_consent({"explicit_feedback": False})
        self.assertEqual(learning.list_active_guidance(session_id="session-1"), [])

    def test_session_guidance_does_not_cross_into_another_session(self):
        first, _ = learning.create_signal(self.signal(session_id="session-1", status="helpful", value="helpful"))
        second, _ = learning.create_signal(self.signal(session_id="session-2", status="misunderstood", value="misunderstood"))
        learning.mark_applied(first["id"], learning.feedback_effect(first))
        learning.mark_applied(second["id"], learning.feedback_effect(second))
        first_guidance = learning.list_active_guidance(session_id="session-1")
        second_guidance = learning.list_active_guidance(session_id="session-2")
        self.assertEqual([row["signal_id"] for row in first_guidance], [first["id"]])
        self.assertEqual([row["signal_id"] for row in second_guidance], [second["id"]])

    def test_feedback_rejects_user_and_global_scope(self):
        for scope in ("user", "global"):
            with self.assertRaisesRegex(ValueError, "feedback signals require session scope"):
                learning.create_signal(self.signal(scope=scope, session_id=""))

    def test_bridge_applies_bounded_feedback_without_reflection_write(self):
        row, _ = learning.create_signal(self.signal(status="unhelpful", value="unhelpful"))
        with patch("bridge.core._memory_ingest") as ingest:
            effect = BridgeHandler._apply_learning_signal(None, row)
        self.assertEqual(effect, learning.feedback_effect(row))
        ingest.assert_not_called()

    def test_retention_is_server_controlled_and_expiry_is_purged(self):
        created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        far_future = datetime(2126, 1, 1, tzinfo=timezone.utc)
        with patch("bridge.learning._now", return_value=created_at):
            learning.update_consent({"retention_days": 7})
            row, error = learning.create_signal(self.signal(
                timestamp=far_future.isoformat().replace("+00:00", "Z"),
                expires_at=far_future.isoformat().replace("+00:00", "Z"),
            ))
        self.assertFalse(error)
        self.assertEqual(row["timestamp"], "2026-01-01T00:00:00Z")
        self.assertEqual(row["expires_at"], "2026-01-08T00:00:00Z")
        with patch("bridge.learning._now", return_value=created_at + timedelta(days=8)):
            self.assertEqual(learning.list_signals(), [])

    def test_session_scope_isolation_and_explicit_delete(self):
        first, _ = learning.create_signal(self.signal(session_id="session-1"))
        second, _ = learning.create_signal(self.signal(session_id="session-2"))
        self.assertEqual([row["id"] for row in learning.list_signals(scope="session", session_id="session-1")], [first["id"]])
        self.assertEqual([row["id"] for row in learning.list_signals(scope="session", session_id="session-2")], [second["id"]])
        with self.assertRaises(ValueError):
            learning.delete_signals()
        with self.assertRaises(ValueError):
            learning.delete_signals(scope="session")
        self.assertEqual(
            learning.delete_signals(
                signal_id=second["id"], scope="session", session_id="session-1"
            ),
            0,
        )
        self.assertEqual(
            [row["id"] for row in learning.list_signals(scope="session", session_id="session-2")],
            [second["id"]],
        )
        self.assertEqual(learning.delete_signals(scope="session", session_id="session-1"), 1)
        self.assertEqual([row["id"] for row in learning.list_signals()], [second["id"]])


if __name__ == "__main__":
    unittest.main()