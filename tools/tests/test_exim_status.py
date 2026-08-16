#!/usr/bin/env python3
"""Contract: bounded, read-only Exim queue status parsing."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import exim_status

QUEUE_ID = "1wvbwq-00000004LAd-0WIt"


def runner_for(log, direct_code=0, sudo_code=0, direct_stderr=b"", sudo_stderr=b""):
    calls = []

    class Result:
        def __init__(self, code, stdout, stderr):
            self.returncode = code
            self.stdout = stdout
            self.stderr = stderr

    def run(args, **_kwargs):
        calls.append(args)
        is_sudo = args[0] == exim_status.SUDO_COMMAND
        return Result(
            sudo_code if is_sudo else direct_code,
            (log if (is_sudo or direct_code == 0) else "").encode(),
            sudo_stderr if is_sudo else direct_stderr,
        )

    run.calls = calls
    return run


class QueueIdTests(unittest.TestCase):
    def test_extracts_exim_queue_id(self):
        self.assertEqual(exim_status.extract_queue_id("250 OK id=" + QUEUE_ID), QUEUE_ID)

    def test_missing_or_malformed_queue_id_is_empty(self):
        self.assertEqual(exim_status.extract_queue_id("250 OK"), "")
        self.assertEqual(exim_status.extract_queue_id("250 id=bad!"), "")


class InspectionTests(unittest.TestCase):
    def test_mainlog_path_is_fixed_even_if_environment_is_set(self):
        self.assertEqual(exim_status.MAIN_LOG_PATH, "/var/log/exim4/mainlog")
    def test_delivered_after_completed(self):
        log = "2026-08-16 10:00:00 " + QUEUE_ID + " => remote@example.net R=lookuphost T=remote_smtp\n" \
              "2026-08-16 10:00:01 " + QUEUE_ID + " Completed\n"
        result = exim_status.inspect(QUEUE_ID, runner=runner_for(log))
        self.assertEqual(result["status"], "delivered")
        self.assertTrue(result["completed"])
        self.assertNotIn("remote@example.net", result["detail"])

    def test_deferred_status(self):
        log = "2026-08-16 10:00:00 " + QUEUE_ID + " == remote@example.net R=lookuphost: Connection timed out\n"
        result = exim_status.inspect(QUEUE_ID, runner=runner_for(log))
        self.assertEqual(result["status"], "deferred")
        self.assertNotIn("remote@example.net", result["detail"])

    def test_failed_status(self):
        log = "2026-08-16 10:00:00 " + QUEUE_ID + " ** remote@example.net R=nonlocal: Mailing to remote domains not supported\n"
        result = exim_status.inspect(QUEUE_ID, runner=runner_for(log))
        self.assertEqual(result["status"], "failed")
        self.assertIn("Mailing to remote domains", result["detail"])
        self.assertNotIn("remote@example.net", result["detail"])

    def test_pending_delivery(self):
        log = "2026-08-16 10:00:00 " + QUEUE_ID + " => remote@example.net R=lookuphost T=remote_smtp\n"
        result = exim_status.inspect(QUEUE_ID, runner=runner_for(log))
        self.assertEqual(result["status"], "pending")

    def test_partial_deferred_status_preserves_prior_handoff_without_recipient(self):
        log = "2026 " + QUEUE_ID + " => first@example.net R=lookuphost T=remote_smtp\n" \
              "2026 " + QUEUE_ID + " == second@example.net R=lookuphost: timed out\n"
        result = exim_status.inspect(QUEUE_ID, runner=runner_for(log))
        self.assertEqual(result["status"], "deferred")
        self.assertTrue(result["partial"])
        self.assertIn("already handed off", result["detail"])
        self.assertNotIn("first@example.net", result["detail"])
        self.assertNotIn("second@example.net", result["detail"])

    def test_unknown_when_not_in_bounded_log(self):
        result = exim_status.inspect(QUEUE_ID, runner=runner_for("2026 unrelated\n"))
        self.assertEqual(result["status"], "unknown")

    def test_rejects_unsafe_queue_id_without_running_command(self):
        runner = runner_for("")
        with self.assertRaises(exim_status.EximStatusError):
            exim_status.inspect("x; touch /tmp/no", runner=runner)
        self.assertEqual(runner.calls, [])

    def test_direct_permission_error_is_clear(self):
        runner = runner_for("", direct_code=1, direct_stderr=b"permission denied")
        with self.assertRaises(exim_status.EximStatusError) as caught:
            exim_status.inspect(QUEUE_ID, runner=runner)
        self.assertIn("not readable", str(caught.exception))

    def test_fixed_noninteractive_sudo_fallback(self):
        log = "2026 " + QUEUE_ID + " ** x@example.net R=nonlocal: denied\n"
        runner = runner_for(log, direct_code=1, direct_stderr=b"permission denied")
        result = exim_status.inspect(QUEUE_ID, allow_sudo=True, runner=runner)
        self.assertEqual(result["access"], "sudo")
        self.assertEqual(runner.calls[1], [
            exim_status.SUDO_COMMAND, "-n", exim_status.TAIL_COMMAND,
            "-n", str(exim_status.TAIL_LINES), exim_status.MAIN_LOG_PATH,
        ])

    def test_sudo_never_prompts_for_a_password(self):
        runner = runner_for("", direct_code=1, sudo_code=1,
                            direct_stderr=b"permission denied", sudo_stderr=b"a password is required")
        with self.assertRaises(exim_status.EximStatusError) as caught:
            exim_status.inspect(QUEUE_ID, allow_sudo=True, runner=runner)
        self.assertIn("non-interactive", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
