#!/usr/bin/env python3
"""Tests for safe maintainer-summary comments from readiness verdicts."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".github" / "scripts"))

from readiness_maintainer_comment import category_from_review, comment_body, trusted_marker_comment


class ReadinessMaintainerCommentTests(unittest.TestCase):
    def test_known_category_generates_fixed_summary_without_review_text(self):
        review = (
            "VERDICT: NEEDS_MAINTAINER\n"
            "MAINTAINER_CATEGORY: security-boundary\n"
            "A raw PR-controlled path /private/token should never be posted."
        )
        body = comment_body(review, "a" * 40)
        self.assertEqual(category_from_review(review), "security-boundary")
        self.assertIn("Security boundary", body)
        self.assertIn("a" * 12, body)
        self.assertIn("/eva-readiness approve " + "a" * 40, body)
        self.assertIn("/eva-readiness request-changes " + "a" * 40, body)
        self.assertNotIn("/private/token", body)
        self.assertNotIn("raw PR-controlled", body)

    def test_unknown_or_missing_category_uses_safe_default(self):
        for review in ("VERDICT: NEEDS_MAINTAINER", "MAINTAINER_CATEGORY: arbitrary-value"):
            body = comment_body(review, "b" * 40)
            self.assertEqual(category_from_review(review), "other")
            self.assertIn("Maintainer judgment", body)
            self.assertNotIn("arbitrary-value", body)

    def test_only_workflow_bot_marker_suppresses_duplicate_comment(self):
        head_sha = "c" * 40
        marker = "<!-- eva-readiness-maintainer:" + head_sha + " -->"
        self.assertFalse(trusted_marker_comment({
            "user": {"login": "contributor", "type": "User"}, "body": marker,
        }, head_sha))
        self.assertTrue(trusted_marker_comment({
            "user": {"login": "github-actions[bot]", "type": "Bot"}, "body": marker,
        }, head_sha))


if __name__ == "__main__":
    unittest.main()