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
import ssl
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bridge import config as _cfg


def _self_signed_cert(directory):
    """Generate a throwaway certificate for 127.0.0.1. Test-only material."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = os.path.join(directory, "sink-cert.pem")
    key_path = os.path.join(directory, "sink-key.pem")
    with open(cert_path, "wb") as handle:
        handle.write(certificate.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as handle:
        handle.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    return cert_path, key_path


class _SMTPSinkHandler(socketserver.StreamRequestHandler):
    """Accepts one SMTP transaction and records the envelope and data."""

    def handle(self):
        require_auth = getattr(self.server, "require_auth", False)
        tls_context = getattr(self.server, "tls_context", None)
        self.wfile.write(b"220 sink.lab.internal ESMTP\r\n")
        envelope = {"mail_from": "", "rcpt_to": [], "data": "", "authenticated": False,
                    "encrypted": False}
        while True:
            line = self.rfile.readline()
            if not line:
                break
            command = line.decode("utf-8", "replace").strip()
            upper = command.upper()
            if upper.startswith(("EHLO", "HELO")):
                extensions = [b"250-sink.lab.internal\r\n"]
                if tls_context and not envelope["encrypted"]:
                    extensions.append(b"250-STARTTLS\r\n")
                if require_auth:
                    extensions.append(b"250-AUTH PLAIN LOGIN\r\n")
                extensions.append(b"250 HELP\r\n")
                self.wfile.write(b"".join(extensions))
            elif upper == "STARTTLS" and tls_context:
                self.wfile.write(b"220 Ready to start TLS\r\n")
                self.wfile.flush()
                self.connection = tls_context.wrap_socket(self.connection, server_side=True)
                self.rfile = self.connection.makefile("rb", -1)
                self.wfile = self.connection.makefile("wb", 0)
                envelope["encrypted"] = True
            elif upper.startswith("AUTH"):
                envelope["authenticated"] = True
                if require_auth:
                    self.wfile.write(b"235 Authentication succeeded\r\n")
                else:
                    self.wfile.write(b"535 authentication not offered\r\n")
            elif upper.startswith("MAIL FROM:"):
                if require_auth and not envelope["authenticated"]:
                    self.wfile.write(b"530 Authentication required\r\n")
                    continue
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
                self.wfile.write(b"250 Queued id=1wtest-00000004LAd-0WIt\r\n")
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

    def __init__(self, require_auth=False, tls_context=None):
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPSinkHandler)
        self.server.received = []
        self.server.require_auth = require_auth
        self.server.tls_context = tls_context
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

    def test_internal_delivery_retains_the_mta_queue_id_when_requested(self):
        account = self.service.load_config()["accounts"][0]
        account["settings"]["delivery_mode"] = "local_mta"
        self.service.upsert_account(account)
        result = self.send()
        self.assertEqual(result["decision"], "submitted")
        self.assertEqual(result["deliveries"][0]["mta_queue_id"], "1wtest-00000004LAd-0WIt")

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


class BestEffortLocalMTATests(LocalDeliveryTestCase):
    def setUp(self):
        super().setUp()
        self.configure([])

    def configure(self, consent):
        self.service.upsert_account({
            "id": "eva", "label": "Eva best effort", "backend": "eva_direct",
            "address": "eva@lab.internal", "status": "connected",
            "settings": {
                "direct_consent": consent,
                "delivery_mode": "local_mta",
                "internal_domains": [],
                "internal_smtp_host": "127.0.0.1",
                "internal_smtp_port": self.sink.port,
                "internal_smtp_starttls": False,
            },
        })

    @staticmethod
    def request(body="Best-effort message"):
        return {
            "to": "outside@example.net",
            "subject": "Best-effort local MTA test",
            "body": body,
        }

    def test_unconfirmed_external_message_never_reaches_the_mta(self):
        result = self.service.send_message(self.request(), account_id="eva")
        self.assertEqual(result["decision"], "needs_confirmation")
        self.assertEqual(self.sink.received, [])

    def test_confirmed_external_message_is_submitted_to_the_real_mta(self):
        request = self.request()
        pending = self.service.send_message(request, account_id="eva")
        result = self.service.send_message(
            request,
            account_id="eva",
            confirmation={
                "digest": pending["digest"],
                "addresses": pending["unknown_recipients"],
            },
        )
        self.assertEqual(result["decision"], "submitted")
        self.assertIn("not verified", result["warning"])
        self.assertEqual(self.sink.received[0]["rcpt_to"], ["outside@example.net"])

    def test_confirmation_cannot_authorize_changed_content(self):
        pending = self.service.send_message(self.request(), account_id="eva")
        result = self.service.send_message(
            self.request(body="Changed after approval"),
            account_id="eva",
            confirmation={
                "digest": pending["digest"],
                "addresses": pending["unknown_recipients"],
            },
        )
        self.assertEqual(result["decision"], "needs_confirmation")
        self.assertEqual(self.sink.received, [])

    def test_preconsented_recipient_submits_without_a_prompt(self):
        self.configure(["outside@example.net"])
        result = self.service.send_message(self.request(), account_id="eva")
        self.assertEqual(result["decision"], "submitted")
        self.assertEqual(len(self.sink.received), 1)


class RelayRouteTests(unittest.TestCase):
    """The relay leg over a real STARTTLS-protected, authenticating SMTP server."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

        cert_path, key_path = _self_signed_cert(self.directory.name)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)

        # The adapter verifies certificates; trust only this throwaway CA.
        client_context = ssl.create_default_context(cafile=cert_path)
        patcher = mock.patch("bridge.mailbox_imap._tls_context", return_value=client_context)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.internal = SMTPSink()
        self.addCleanup(self.internal.close)
        self.relay = SMTPSink(require_auth=True, tls_context=server_context)
        self.addCleanup(self.relay.close)

        config_path = os.path.join(self.directory.name, "email_accounts.json")
        config_patcher = mock.patch.object(_cfg, "EMAIL_CONFIG_PATH", config_path)
        config_patcher.start()
        self.addCleanup(config_patcher.stop)

        from bridge import email_service as module
        importlib.reload(module)
        self.service = module
        self.service._EMAIL_CONFIG_PATH = config_path
        self.addCleanup(lambda: self.service._credentials.clear())
        self.configure()

    def configure(self, relay_port=None, internal_port=None):
        _, errors = self.service.replace_accounts([
            {"id": "owned", "label": "Owned domain", "address": "me@lab.internal",
             "status": "connected",
             "settings": {"imap_host": "imap.lab.internal",
                          "smtp_host": "127.0.0.1",
                          "smtp_port": relay_port or self.relay.port,
                          "smtp_starttls": True}},
            {"id": "eva", "label": "Eva direct", "backend": "eva_direct",
             "address": "eva@lab.internal", "status": "connected",
             "settings": {"direct_consent": ["@lab.internal", "@partner.example"],
                          "delivery_mode": "auto",
                          "internal_domains": ["lab.internal"],
                          "internal_smtp_host": "127.0.0.1",
                          "internal_smtp_port": internal_port or self.internal.port,
                          "internal_smtp_starttls": False,
                          "relay_account_id": "owned"}},
        ], ["@lab.internal", "@partner.example"])
        self.assertEqual(errors, [])
        self.service.set_credential("owned", "relay-secret")

    def send(self, **overrides):
        message = {"to": "ops@lab.internal", "subject": "Report", "body": "Done."}
        message.update(overrides)
        return self.service.send_message(message, account_id="eva")

    def test_external_recipient_goes_out_through_the_relay(self):
        result = self.send(to="boss@partner.example")
        self.assertEqual(result["decision"], "sent")
        self.assertEqual(len(self.relay.received), 1)
        self.assertEqual(self.internal.received, [])

    def test_relay_upgrades_to_tls_before_authenticating(self):
        self.send(to="boss@partner.example")
        envelope = self.relay.received[0]
        self.assertTrue(envelope["encrypted"])
        self.assertTrue(envelope["authenticated"])

    def test_relay_preserves_evas_identity_as_the_sender(self):
        self.send(to="boss@partner.example")
        envelope = self.relay.received[0]
        self.assertEqual(envelope["mail_from"], "eva@lab.internal")
        self.assertIn("From: eva@lab.internal", envelope["data"])

    def test_mixed_message_splits_across_both_real_servers(self):
        result = self.send(to=["ops@lab.internal", "boss@partner.example"])
        self.assertEqual(result["decision"], "sent")
        self.assertEqual(self.internal.received[0]["rcpt_to"], ["ops@lab.internal"])
        self.assertEqual(self.relay.received[0]["rcpt_to"], ["boss@partner.example"])

    def test_each_route_only_sees_its_own_recipients(self):
        self.send(to=["ops@lab.internal", "boss@partner.example"])
        self.assertNotIn("boss@partner.example", self.internal.received[0]["rcpt_to"])
        self.assertNotIn("ops@lab.internal", self.relay.received[0]["rcpt_to"])

    def test_internal_route_stays_unauthenticated(self):
        self.send(to=["ops@lab.internal", "boss@partner.example"])
        self.assertFalse(self.internal.received[0]["authenticated"])

    def test_partial_failure_reports_what_was_delivered(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        self.configure(relay_port=dead_port)

        result = self.send(to=["ops@lab.internal", "boss@partner.example"])
        self.assertEqual(result["decision"], "partially_sent")
        self.assertEqual([d["route"] for d in result["deliveries"]], ["internal"])
        self.assertTrue(result["failures"])
        self.assertIn("relay", result["failures"][0])
        # The internal recipient really does have the message.
        self.assertEqual(self.internal.received[0]["rcpt_to"], ["ops@lab.internal"])

    def test_total_failure_still_raises(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            dead_port = probe.getsockname()[1]
        self.configure(relay_port=dead_port, internal_port=dead_port)
        with self.assertRaises(self.service.EmailServiceError):
            self.send(to=["ops@lab.internal", "boss@partner.example"])

    def test_relay_without_a_credential_does_not_deliver(self):
        self.service.clear_credential("owned")
        # The relay is the only route here, so a total failure is an error.
        with self.assertRaises(self.service.EmailServiceError):
            self.send(to="boss@partner.example")
        self.assertEqual(self.relay.received, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
