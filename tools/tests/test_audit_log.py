#!/usr/bin/env python3
"""Focused contract tests for the local application audit writer."""

import json
import io
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from bridge import audit


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "audit.jsonl"
        with patch.object(audit, "_AUDIT_PATH", str(path)), patch.object(audit, "_AUDIT_MAX_BYTES", 1024):
            original_fdopen = audit.os.fdopen
            created_modes = []
            def observe_fdopen(descriptor, *args, **kwargs):
                created_modes.append(stat.S_IMODE(path.stat().st_mode))
                return original_fdopen(descriptor, *args, **kwargs)
            with patch.object(audit.os, "fdopen", side_effect=observe_fdopen):
                record = audit.audit_event(
                    "turn.response",
                    correlation_id="sk-abcdefghijklmnop",
                    outcome="completed",
                    user_message="Show token=do-not-write and Authorization: Basic leaked-value and sk-abcdefghijklmnop",
                    assistant_response="Done. https://example.test/?sig=do-not-write&X-Amz-Signature=also-do-not-write ghp_abcdefghijklmnop",
                    api_key="do-not-write",
                    nested={"password": "do-not-write", "result": "safe"},
                )
            assert record is not None
            stored = json.loads(path.read_text(encoding="utf-8"))
            serialized = json.dumps(stored)
            assert "do-not-write" not in serialized
            assert "leaked-value" not in serialized
            assert stored["api_key"] == "<redacted>"
            assert stored["nested"]["password"] == "<redacted>"
            assert stored["nested"]["result"] == "safe"
            assert stored["correlation_id"] == "invalid"
            assert stored["outcome"] == "completed"
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert created_modes == [0o600]

            for authorization in (
                "Authorization: Basic opaque-basic-value",
                "Authorization: Token opaque-token-value",
            ):
                sanitized = audit._sanitize_text(authorization)
                assert "opaque-" not in sanitized
                assert "<redacted>" in sanitized

            pem = (
                "PRIVATE_KEY=-----BEGIN " + "PRIVATE KEY-----\n"
                "MII-private-value\n-----END " + "PRIVATE KEY-----"
            )
            sanitized_pem = audit._sanitize_text(pem)
            assert "MII-private-value" not in sanitized_pem
            assert "BEGIN PRIVATE KEY" not in sanitized_pem
            assert audit._sanitize({"private_key": pem})["private_key"] == "<redacted>"

            forwarded = io.StringIO()
            with patch.dict(os.environ, {"EVA_RUNTIME_AUDIT_STDOUT": "1"}), patch("sys.stdout", forwarded):
                audit.audit_event(
                    "email_send", correlation_id="eva", outcome="submitted",
                    password="do-not-forward", body_chars=42,
                    assistant_response="ordinary private answer text",
                    user_message="ordinary private user text",
                )
            aggregate_line = forwarded.getvalue()
            assert aggregate_line.startswith("[Audit] {")
            assert '"event":"email_send"' in aggregate_line
            assert '"outcome":"submitted"' in aggregate_line
            assert '"body_chars":42' in aggregate_line
            assert "do-not-forward" not in aggregate_line
            assert "ordinary private answer text" not in aggregate_line
            assert "ordinary private user text" not in aggregate_line
            assert "assistant_response" not in aggregate_line
            assert "user_message" not in aggregate_line

            projected = audit._runtime_audit_record({
                "event": "turn.response", "outcome": "ok", "assistant_response": "private",
                "message_count": 2, "route": "internal", "unknown_field": "private-too",
                "model": "ordinary private answer", "provider": "private provider",
                "source": "private source", "category": "private category",
                "correlation_id": "private-correlation",
            })
            assert projected == {
                "event": "turn.response", "outcome": "ok", "message_count": 2, "route": "internal",
            }

            forwarded = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), patch("sys.stdout", forwarded):
                audit.audit_event("email_send", outcome="submitted")
            assert forwarded.getvalue() == ""

    with patch.object(audit, "_AUDIT_PATH", "/dev/null/audit.jsonl"):
        assert audit.audit_event("turn.response", outcome="failed") is None

    print("audit log tests: PASS")


if __name__ == "__main__":
    main()