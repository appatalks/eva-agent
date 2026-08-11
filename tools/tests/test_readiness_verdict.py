#!/usr/bin/env python3
"""Focused tests for strict readiness verdict parsing."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from readiness_verdict import parse_verdict


class ReadinessVerdictTests(unittest.TestCase):
    def test_accepts_one_standalone_verdict_after_cli_trace(self):
        result = parse_verdict(
            b"\xe2\x97\x8f Read evidence.md\nVERDICT: APPROVE\nAll checks passed.\n"
        )
        self.assertEqual(result, {"valid": True, "kind": "valid", "verdict": "APPROVE"})

    def test_rejects_missing_or_ambiguous_verdicts(self):
        self.assertEqual(parse_verdict(b""), {"valid": False, "kind": "empty"})
        self.assertEqual(parse_verdict(b"No verdict\n"), {"valid": False, "kind": "malformed"})
        self.assertEqual(
            parse_verdict(b"VERDICT: APPROVE\nVERDICT: REQUEST_CHANGES\n"),
            {"valid": False, "kind": "malformed"},
        )


if __name__ == "__main__":
    unittest.main()