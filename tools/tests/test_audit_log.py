#!/usr/bin/env python3
"""Focused contract tests for the local application audit writer."""

import json
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
                    user_message="Show token=do-not-write and Authorization: leaked-value and sk-abcdefghijklmnop",
                    assistant_response="Done. https://example.test/?sig=do-not-write&X-Amz-Signature=also-do-not-write ghp_abcdefghijklmnop",
                    api_key="do-not-write",
                    nested={"password": "do-not-write", "result": "safe"},
                )
            assert record is not None
            stored = json.loads(path.read_text(encoding="utf-8"))
            serialized = json.dumps(stored)
            assert "do-not-write" not in serialized
            assert stored["api_key"] == "<redacted>"
            assert stored["nested"]["password"] == "<redacted>"
            assert stored["nested"]["result"] == "safe"
            assert stored["correlation_id"] == "invalid"
            assert stored["outcome"] == "completed"
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert created_modes == [0o600]

    with patch.object(audit, "_AUDIT_PATH", "/dev/null/audit.jsonl"):
        assert audit.audit_event("turn.response", outcome="failed") is None

    print("audit log tests: PASS")


if __name__ == "__main__":
    main()