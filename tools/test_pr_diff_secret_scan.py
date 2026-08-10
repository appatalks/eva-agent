#!/usr/bin/env python3
"""Tests for the fork-safe pull-request credential diff scanner."""

import unittest

from scan_pr_diff_secrets import scan_diff, scan_text


class PrDiffSecretScanTests(unittest.TestCase):
    def test_scans_added_lines_without_returning_secret_values(self):
        secret = "sk-" + "A" * 24
        findings = scan_diff(
            "diff --git a/app.js b/app.js\n"
            "+++ b/app.js\n"
            "@@ -1,0 +1,2 @@\n"
            f"+const key = '{secret}';\n"
            "+const safe = 'hello';\n"
        )
        self.assertEqual(findings[0]["kind"], "openai-key")
        self.assertEqual(findings[0]["path"], "pr-diff")
        self.assertNotIn(secret, str(findings))

    def test_ignores_removed_secrets_and_obvious_examples(self):
        findings = scan_diff(
            "diff --git a/docs.md b/docs.md\n"
            "+++ b/docs.md\n"
            "@@ -1,1 +1,1 @@\n"
            "-sk-" + "B" * 24 + "\n"
            "+Use sk-FAKE-example in documentation.\n"
        )
        self.assertEqual(findings, [])

    def test_flags_sensitive_files(self):
        findings = scan_diff(
            "diff --git a/.env b/.env\n"
            "new file mode 100644\n"
            "+++ b/.env\n"
            "@@ -0,0 +1 @@\n"
            "+EXAMPLE=value\n"
        )
        self.assertEqual(findings[0]["kind"], "sensitive-file")
        self.assertEqual(findings[0]["path"], "sensitive-path")

    def test_flags_private_key_variants(self):
        for header in (
            "-----BEGIN ENCRYPTED PRIVATE KEY-----",
            "-----BEGIN PGP PRIVATE KEY BLOCK-----",
        ):
            findings = scan_diff(
                "diff --git a/key.txt b/key.txt\n"
                "+++ b/key.txt\n"
                "@@ -0,0 +1 @@\n"
                f"+{header}\n"
            )
            self.assertEqual(findings[0]["kind"], "private-key", header)

    def test_flags_env_variants_and_rename_only_moves(self):
        findings = scan_diff(
            "diff --git a/example.txt b/.env.development\n"
            "similarity index 100%\n"
            "rename from example.txt\n"
            "rename to .env.development\n"
        )
        self.assertEqual(findings[0]["kind"], "sensitive-file")
        self.assertEqual(findings[0]["path"], "sensitive-path")

    def test_flags_copy_only_moves(self):
        findings = scan_diff(
            "diff --git a/example.txt b/.env.staging\n"
            "similarity index 100%\n"
            "copy from example.txt\n"
            "copy to .env.staging\n"
        )
        self.assertEqual(findings[0]["kind"], "sensitive-file")
        self.assertEqual(findings[0]["path"], "sensitive-path")

    def test_flags_binary_and_empty_sensitive_files_from_diff_header(self):
        for path, detail in (
            (".env.production", "Binary files /dev/null and b/.env.production differ\n"),
            (".env.staging", "new file mode 100644\nindex e69de29..e69de29\n"),
            ("config.json", "Binary files /dev/null and b/config.json differ\n"),
        ):
            findings = scan_diff(f"diff --git a/{path} b/{path}\n" + detail)
            self.assertEqual(findings[0]["kind"], "sensitive-file", path)
            self.assertEqual(findings[0]["path"], "sensitive-path")

    def test_never_retains_secret_bearing_diff_paths(self):
        secret = "sk-" + "D" * 24
        findings = scan_diff(
            f"diff --git a/.env.{secret} b/.env.{secret}\n"
            "new file mode 100644\n"
        )
        self.assertEqual(findings[0]["path"], "sensitive-path")
        self.assertNotIn(secret, str(findings))

    def test_scans_untrusted_comment_text_without_returning_value(self):
        secret = "github_pat_" + "C" * 24
        findings = scan_text('{"body":"' + secret + '"}', "inline-comments.json")
        self.assertEqual(findings[0]["kind"], "github-token")
        self.assertEqual(findings[0]["path"], "inline-comments.json")
        self.assertNotIn(secret, str(findings))


if __name__ == "__main__":
    unittest.main()