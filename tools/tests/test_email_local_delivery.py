#!/usr/bin/env python3
"""Contract: real unauthenticated delivery through Eva's internal route.

A minimal SMTP sink runs in-process on a loopback port. Mail is composed,
authorized, and delivered by the real bridge modules, then inspected as the
receiving server actually saw it. Nothing is mocked below the socket, so this
covers envelope construction, header hygiene, and the no-credential path that
Eva uses for internal or unauthenticated delivery.
"""

import importlib
import os
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import config as _cfg


class _SMTPSinkHandler(socketserver.StreamRequestHandler):
    """Accepts one SMTP transaction and records the envelope and data."""

    def handle(self):
        self.wfile.write(b"220 sink.lab.internal ESMTP\r\n")
        envelope = {"mail_from": "", "rcpt_to": [], "data": "", "authenticated": False}
        while True:
            line = self.rfile.readline()
            if not line:
                break
            command = line.decode("utf-8", "replace").strip()
            upper = command.upper()
            if upper.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250-sink.lab.internal\r\n250 HELP\r\n")
            elif upper.startswith("AUTH"):
                envelope["authenticated"] = True
                self.wfile.write(b"535 authentication not offered\r\n")
            elif upper.startswith("MAIL FROM:"):
                envelope["mail_from"] = command.split(":", 1)[1].strip().strip("<>")
                self.wfile.write(b"250 OK\r\n")
            elif upper.startswith("RCPT TO:"):
                envelope["rcpt_to"].append(command.split(":", 1)[1].strip().strip("<>"))
                self.wfile.write(b"250 OK\r\n")
            elif upper == "DATA":
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                lines = []
                while True:
                    data_line = self.rfile.readline()
                    if not data_line or data_line in (b".\r\n", b".\n"):
                        break
                    lines.append(data_line.decode("utf-8", "replace"))
                envelope["data"] = "".join(lines)
                self.wfile.write(b"250 Queued\r\n")
            elif upper.startswith("QUIT"):
                self.wfile.write(b"221 Bye\r\n")
                break
            elif upper.startswith("RSET"):
                self.wfile.write(b"250 OK\r\n")
            else:
                self.wfile.write(b"250 OK\r\n")
        self.server.received.append(envelope)


class SMTPSink:
    """One-shot loopback SMTP server for delivery tests."""

    def __init__(self):
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPSinkHandler)
        self.server.received = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def received(self):
        return self.server.received

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class LocalDeliveryTestCase(unittest.TestCase):
    def setUp(self):
        self.sink = SMTPSink()
        self.addCleanup(self.sink.close)

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config_path = os.path.join(self.directory.name, "email_accounts.json")
        patcher = mock.patch.object(_cfg, "EMAIL_CONFIG_PATH", config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        from bridge import email_service as module
        importlib.reload(module)
        self.service = module
        self.service._EMAIL_CONFIG_PATH = config_path
        self.addCleanup(lambda: self.service._credentials.clear())

        _, errors = self.service.replace_accounts([{
            "id": "eva", "label": "Eva direct", "backend": "eva_direct",
            "address": "eva@lab.internal", "status": "connected",
            "settings": {
                "direct_consent": ["@lab.internal"],
                "delivery_mode": "internal",
                "internal_domains": ["lab.internal"],
                "internal_smtp_host": "127.0.0.1",
                "internal_smtp_port": self.sink.port,
                "internal_smtp_starttls": False,
            },
        }], ["@lab.internal"])
        self.assertEqual(errors, [])

    def send(self, **overrides):
        message = {
            "to": "ops@lab.internal",
            "subject": "Nightly status",
            "body": "All systems nominal.",
        }
        message.update(overrides)
        return self.service.send_message(message, account_id="eva")


class UnauthenticatedDeliveryTests(LocalDeliveryTestCase):
    def test_message_is_delivered_without_any_credential(self):
        result = self.send()
        self.assertEqual(result["decision"], "sent")
        self.assertEqual(len(self.sink.received), 1)
        self.assertFalse(self.sink.received[0]["authenticated"])

    def test_envelope_carries_sender_and_recipient(self):
        self.send()
        envelope = self.sink.received[0]
        self.assertEqual(envelope["mail_from"], "eva@lab.internal")
        self.assertEqual(envelope["rcpt_to"], ["ops@lab.internal"])

    def test_headers_are_well_formed(self):
        self.send()
        data = self.sink.received[0]["data"]
        self.assertIn("From: eva@lab.internal", data)
        self.assertIn("To: ops@lab.internal", data)
        self.assertIn("Subject: Nightly status", data)
        self.assertIn("Message-ID:", data)
        self.assertIn("Date:", data)
        self.assertIn("All systems nominal.", data)

    def test_bcc_reaches_the_envelope_but_never_the_headers(self):
        self.send(bcc="audit@lab.internal")
        envelope = self.sink.received[0]
        self.assertIn("audit@lab.internal", envelope["rcpt_to"])
        self.assertNotIn("audit@lab.internal", envelope["data"])
        self.assertNotIn("Bcc:", envelope["data"])

    def test_cc_appears_in_both_envelope_and_headers(self):
        self.send(cc="team@lab.internal")
        envelope = self.sink.received[0]
        self.assertIn("team@lab.internal", envelope["rcpt_to"])
        self.assertIn("Cc: team@lab.internal", envelope["data"])

    def test_multiple_recipients_each_get_an_envelope_entry(self):
        self.send(to=["ops@lab.internal", "oncall@lab.internal"])
        self.assertEqual(len(self.sink.received[0]["rcpt_to"]), 2)

    def test_subject_newline_cannot_inject_a_header(self):
        self.send(subject="Status\r\nBcc: attacker@lab.internal")
        envelope = self.sink.received[0]
        self.assertNotIn("attacker@lab.internal", envelope["rcpt_to"])
        # The folded text may appear inside the Subject value; what must never
        # happen is a new header line starting with Bcc.
        header_block = envelope["data"].split("\r\n\r\n", 1)[0]
        self.assertFalse(
            any(line.lower().startswith("bcc:") for line in header_block.split("\r\n")),
            header_block,
        )

    def test_unicode_body_survives_delivery(self):
        self.send(body="Status: 정상 — all good")
        self.assertIn("=", self.sink.received[0]["data"])  # encoded, not dropped
        self.assertEqual(len(self.sink.received), 1)


class LocalPolicyTests(LocalDeliveryTestCase):
    def test_recipient_outside_the_internal_domain_is_refused(self):
        result = self.send(to="stranger@elsewhere.example")
        self.assertNotEqual(result["decision"], "sent")
        self.assertEqual(self.sink.received, [])

    def test_unconsented_internal_recipient_is_still_checked(self):
        self.service.replace_accounts([{
            "id": "eva", "backend": "eva_direct", "address": "eva@lab.internal",
            "status": "connected",
            "settings": {
                "direct_consent": ["ops@lab.internal"],
                "delivery_mode": "internal",
                "internal_domains": ["lab.internal"],
                "internal_smtp_host": "127.0.0.1",
                "internal_smtp_port": self.sink.port,
                "internal_smtp_starttls": False,
            },
        }], ["@lab.internal"])
        result = self.send(to="nobody@lab.internal")
        self.assertEqual(result["decision"], "rejected")
        self.assertEqual(self.sink.received, [])

    def test_unreachable_mta_reports_a_service_error(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        self.service.replace_accounts([{
            "id": "eva", "backend": "eva_direct", "address": "eva@lab.internal",
            "status": "connected",
            "settings": {
                "direct_consent": ["@lab.internal"],
                "delivery_mode": "internal",
                "internal_domains": ["lab.internal"],
                "internal_smtp_host": "127.0.0.1",
                "internal_smtp_port": dead_port,
                "internal_smtp_starttls": False,
            },
        }], ["@lab.internal"])
        with self.assertRaises(self.service.EmailServiceError):
            self.send()


if __name__ == "__main__":
    unittest.main(verbosity=2)
