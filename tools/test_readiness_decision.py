#!/usr/bin/env python3
"""Tests for strict maintainer readiness command parsing."""

import unittest

from readiness_decision import parse_command


class ReadinessDecisionTests(unittest.TestCase):
    HEAD = "a" * 40

    def test_accepts_exact_approve_and_request_changes_commands(self):
        approved = parse_command("/eva-readiness approve " + self.HEAD)
        rejected = parse_command("/eva-readiness request-changes " + self.HEAD)
        self.assertEqual(approved, {
            "valid": True, "decision": "approve", "head_sha": self.HEAD,
            "state": "success", "description": "Maintainer approved readiness",
        })
        self.assertEqual(rejected["state"], "failure")
        self.assertEqual(rejected["description"], "Maintainer requested changes")

    def test_rejects_non_exact_commands(self):
        invalid = (
            "/eva-readiness approve\n " + self.HEAD,
            "/eva-readiness\tapprove " + self.HEAD,
            "/eva-readiness approve " + self.HEAD + " extra",
            "/eva-readiness approve " + self.HEAD.upper(),
            "/eva-readiness approve " + self.HEAD[:-1],
            "please /eva-readiness approve " + self.HEAD,
        )
        for command in invalid:
            with self.subTest(command=command):
                self.assertEqual(parse_command(command), {"valid": False})


if __name__ == "__main__":
    unittest.main()