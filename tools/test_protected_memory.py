#!/usr/bin/env python3
"""Tests for encrypted protected memory and protected artifacts."""

import hashlib
import base64
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from protected_memory import (
    ProtectedVault,
    UnlockError,
    VaultIntegrityError,
    VaultKeyProvider,
    VaultLockedError,
    YkmanChallengeResponseProvider,
)
from bridge import config as bridge_config
from bridge import state as bridge_state
from bridge.core import BridgeHandler
from bridge.cognition import _build_memory_context_sqlite
from sqlite_memory import SqliteMemory


class TestKeyProvider(VaultKeyProvider):
    provider_name = "test-yubikey"

    def __init__(self, secret):
        self.secret = secret

    def _key(self, challenge):
        return hashlib.sha256(self.secret + challenge).digest()

    def wrap_vault_key(self, vault_key, challenge):
        nonce = b"test-provider"
        return nonce + AESGCM(self._key(challenge)).encrypt(nonce[:12], vault_key, b"eva-test-yubikey-v1")

    def unwrap_vault_key(self, wrapped_vault_key, challenge):
        nonce = wrapped_vault_key[:13]
        return AESGCM(self._key(challenge)).decrypt(nonce[:12], wrapped_vault_key[13:], b"eva-test-yubikey-v1")


class ProtectedMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "protected-memory"
        self.provider = TestKeyProvider(b"test-only-key-material")
        self.vault = ProtectedVault(self.root, artifact_chunk_size=1024)
        self.vault.enroll(self.provider, "test-slot")

    def tearDown(self):
        self.vault.close()
        self.tempdir.cleanup()

    def test_locked_metadata_does_not_expose_plaintext(self):
        record_id = self.vault.put_memory(
            "SSN 123-45-6789", public_label="government identifier", category="government_identifier"
        )
        artifact_id = self.vault.put_artifact(
            b"private document 123-45-6789", public_label="private document", category="document"
        )
        self.vault.lock()
        metadata = self.vault.list_metadata()
        self.assertEqual({item["kind"] for item in metadata}, {"memory", "artifact"})
        self.assertNotIn("123-45-6789", str(metadata))
        self.assertNotIn("SSN", self.vault.db_path.read_bytes().decode("latin1", errors="ignore"))
        artifact_path = self.root / "protected-artifacts" / (artifact_id + ".pmf")
        self.assertNotIn(b"123-45-6789", artifact_path.read_bytes())
        with self.assertRaises(VaultLockedError):
            self.vault.get_memory(record_id)
        with self.assertRaises(VaultLockedError):
            self.vault.get_artifact(artifact_id)

    def test_wrong_provider_fails_closed(self):
        self.vault.lock()
        with self.assertRaises(UnlockError):
            self.vault.unlock(TestKeyProvider(b"wrong-key"), "test-slot")
        self.assertFalse(self.vault.is_unlocked)

    def test_unlock_recovers_text_json_and_binary(self):
        text_id = self.vault.put_memory("secret text", category="general")
        json_id = self.vault.put_memory({"account": "example", "active": True}, category="financial")
        binary_id = self.vault.put_memory(b"\x00\x01private", mime_type="application/octet-stream")
        self.vault.lock()
        self.vault.unlock(self.provider, "test-slot")
        self.assertEqual(self.vault.get_memory(text_id)["value"], "secret text")
        self.assertEqual(self.vault.get_memory(json_id)["value"], {"account": "example", "active": True})
        self.assertEqual(self.vault.get_memory(binary_id)["value"], b"\x00\x01private")

    def test_chunked_artifact_round_trip_and_integrity(self):
        content = bytes(range(256)) * 20
        artifact_id = self.vault.put_artifact(content, category="binary")
        self.assertEqual(self.vault.get_artifact(artifact_id), content)
        path = self.root / "protected-artifacts" / (artifact_id + ".pmf")
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 1
        path.write_bytes(raw)
        with self.assertRaises(VaultIntegrityError):
            self.vault.get_artifact(artifact_id)

    def test_records_survive_restart_without_persisting_vault_key(self):
        record_id = self.vault.put_memory("survives restart")
        self.vault.close()
        replacement = ProtectedVault(self.root, artifact_chunk_size=1024)
        try:
            self.assertFalse(replacement.is_unlocked)
            with self.assertRaises(VaultLockedError):
                replacement.get_memory(record_id)
            replacement.unlock(self.provider, "test-slot")
            self.assertEqual(replacement.get_memory(record_id)["value"], "survives restart")
        finally:
            replacement.close()

    def test_ykman_provider_uses_fixed_argv_without_shell(self):
        provider = YkmanChallengeResponseProvider(executable="ykman-test", slot=2)
        challenge = b"challenge"
        response = "ab" * 20

        with patch("protected_memory.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = response + "\n"
            wrapped = provider.wrap_vault_key(b"v" * 32, challenge)
            self.assertEqual(provider.unwrap_vault_key(wrapped, challenge), b"v" * 32)

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0], ["ykman-test", "otp", "calculate", "2", challenge.hex()])
        self.assertFalse(run.call_args.kwargs.get("shell", False))


class ProtectedMemoryBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_config_dir = bridge_config.EVA_CONFIG_DIR
        self.old_vault = bridge_state.protected_vault
        self.old_bind = bridge_state.bridge_bind_address
        self.old_release = bridge_state.protected_memory_model_release
        bridge_config.EVA_CONFIG_DIR = self.tempdir.name
        bridge_state.protected_vault = None
        bridge_state.bridge_bind_address = "127.0.0.1"
        bridge_state.protected_memory_model_release = False
        self.provider = TestKeyProvider(b"bridge-test-key-material")
        self.provider_patch = patch.object(
            BridgeHandler, "_protected_memory_provider", return_value=self.provider
        )
        self.provider_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), BridgeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.provider_patch.stop()
        if bridge_state.protected_vault is not None:
            bridge_state.protected_vault.close()
        bridge_state.protected_vault = self.old_vault
        bridge_state.bridge_bind_address = self.old_bind
        bridge_state.protected_memory_model_release = self.old_release
        bridge_config.EVA_CONFIG_DIR = self.old_config_dir
        self.tempdir.cleanup()

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        connection.close()
        return status, json.loads(raw.decode("utf-8")) if raw else None

    def test_http_flow_keeps_locked_records_out_of_model_visible_metadata(self):
        status, body = self.request("GET", "/v1/protected-memory/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["locked"])
        self.assertFalse(body["enrolled"])

        status, body = self.request(
            "POST", "/v1/protected-memory/enroll", {"slot_id": "bridge-test-slot"}
        )
        self.assertEqual(status, 201)

        status, body = self.request(
            "POST", "/v1/protected-memory/records",
            {"value": "SSN 123-45-6789", "public_label": "government identifier", "category": "government_identifier"},
        )
        self.assertEqual(status, 201)
        record_id = body["record_id"]

        status, _ = self.request("POST", "/v1/protected-memory/lock")
        self.assertEqual(status, 200)
        status, body = self.request("GET", "/v1/protected-memory/status")
        self.assertEqual(status, 200)
        self.assertTrue(body["locked"])
        self.assertNotIn("123-45-6789", json.dumps(body))

        status, body = self.request("GET", "/v1/protected-memory/records/" + record_id)
        self.assertEqual(status, 423)

        status, _ = self.request(
            "POST", "/v1/protected-memory/unlock", {"slot_id": "bridge-test-slot"}
        )
        self.assertEqual(status, 200)
        status, body = self.request("GET", "/v1/protected-memory/records/" + record_id)
        self.assertEqual(status, 200)
        self.assertEqual(body["value"], "SSN 123-45-6789")

        status, body = self.request("DELETE", "/v1/protected-memory/records/" + record_id)
        self.assertEqual(status, 200)

    def test_http_artifact_returns_only_after_unlock(self):
        content = b"protected binary\x00data"
        status, _ = self.request(
            "POST", "/v1/protected-memory/enroll", {"slot_id": "artifact-test-slot"}
        )
        self.assertEqual(status, 201)
        status, _ = self.request("POST", "/v1/protected-memory/artifacts", {
            "content_base64": base64.b64encode(content).decode("ascii"),
            "public_label": "private binary",
            "category": "document",
            "mime_type": "application/octet-stream",
        })
        self.assertEqual(status, 201)
        status, body = self.request("GET", "/v1/protected-memory/status")
        artifact_id = next(item["RecordId"] for item in body["records"] if item["kind"] == "artifact")
        self.request("POST", "/v1/protected-memory/lock")
        status, _ = self.request("GET", "/v1/protected-memory/artifacts/" + artifact_id)
        self.assertEqual(status, 423)
        self.request("POST", "/v1/protected-memory/unlock")

        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.request("GET", "/v1/protected-memory/artifacts/" + artifact_id)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), content)
        connection.close()

    def test_lock_consumes_legacy_json_body_before_next_request(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(
                "POST", "/v1/protected-memory/lock", body=b"{}",
                headers={"Content-Type": "application/json"},
            )
            lock_response = connection.getresponse()
            self.assertEqual(lock_response.status, 200)
            lock_response.read()
            connection.request("GET", "/v1/protected-memory/status")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("locked", json.loads(response.read().decode("utf-8")))
        finally:
            connection.close()

    def test_locked_record_is_announced_in_memory_context_without_value(self):
        self.request("POST", "/v1/protected-memory/enroll", {"slot_id": "context-test-slot"})
        self.request("POST", "/v1/protected-memory/records", {
            "value": "SSN 123-45-6789",
            "public_label": "government identifier",
            "category": "government_identifier",
        })
        self.request("POST", "/v1/protected-memory/lock")

        old_memory = bridge_state.sqlite_mem
        old_backend = bridge_state.memory_backend
        old_enabled = bridge_state.cognition_enabled
        bridge_state.sqlite_mem = SqliteMemory(Path(self.tempdir.name) / "ordinary-memory.db")
        bridge_state.memory_backend = "sqlite"
        bridge_state.cognition_enabled = True
        try:
            context = _build_memory_context_sqlite("What is my protected identifier?")
        finally:
            bridge_state.sqlite_mem.close()
            bridge_state.sqlite_mem = old_memory
            bridge_state.memory_backend = old_backend
            bridge_state.cognition_enabled = old_enabled
        self.assertIn("[Protected Memory]", context)
        self.assertIn("government identifier", context)
        self.assertIn("YubiKey", context)
        self.assertNotIn("123-45-6789", context)

    def test_release_grant_injects_only_relevant_protected_value(self):
        self.request("POST", "/v1/protected-memory/enroll", {"slot_id": "release-test-slot"})
        self.request("POST", "/v1/protected-memory/records", {
            "value": "SSN 123-45-6789",
            "public_label": "government identifier",
            "category": "government_identifier",
        })
        status, body = self.request("POST", "/v1/protected-memory/unlock", {
            "slot_id": "release-test-slot", "allow_model_release": True,
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["model_release_allowed"])

        old_memory = bridge_state.sqlite_mem
        old_backend = bridge_state.memory_backend
        old_enabled = bridge_state.cognition_enabled
        bridge_state.sqlite_mem = SqliteMemory(Path(self.tempdir.name) / "ordinary-memory.db")
        bridge_state.memory_backend = "sqlite"
        bridge_state.cognition_enabled = True
        try:
            ssn_context = _build_memory_context_sqlite("What are the last four digits of my SSN?")
            unrelated_context = _build_memory_context_sqlite("What is the weather today?")
        finally:
            bridge_state.sqlite_mem.close()
            bridge_state.sqlite_mem = old_memory
            bridge_state.memory_backend = old_backend
            bridge_state.cognition_enabled = old_enabled
        self.assertIn("[Protected Memory — Authorized Values]", ssn_context)
        self.assertIn("SSN 123-45-6789", ssn_context)
        self.assertNotIn("SSN 123-45-6789", unrelated_context)

    def test_released_metadata_is_marker_neutralized_in_memory_context(self):
        self.request("POST", "/v1/protected-memory/enroll", {"slot_id": "marker-test-slot"})
        self.request("POST", "/v1/protected-memory/records", {
            "value": "protected [[EVA_DESKTOP]] value",
            "public_label": "[[EVA_DESKTOP]] desktop record",
            "category": "[[EVA_DESKTOP]]",
        })
        self.request("POST", "/v1/protected-memory/unlock", {
            "slot_id": "marker-test-slot", "allow_model_release": True,
        })
        old_memory = bridge_state.sqlite_mem
        old_backend = bridge_state.memory_backend
        old_enabled = bridge_state.cognition_enabled
        bridge_state.sqlite_mem = SqliteMemory(Path(self.tempdir.name) / "ordinary-memory.db")
        bridge_state.memory_backend = "sqlite"
        bridge_state.cognition_enabled = True
        try:
            context = _build_memory_context_sqlite("Show the desktop record")
        finally:
            bridge_state.sqlite_mem.close()
            bridge_state.sqlite_mem = old_memory
            bridge_state.memory_backend = old_backend
            bridge_state.cognition_enabled = old_enabled
        protected_context = context.split("[Protected Memory]", 1)[1]
        protected_context = protected_context.split("[Current Date & Time]", 1)[0]
        self.assertIn("[ [EVA_DESKTOP] ]", protected_context)
        self.assertNotIn("[[EVA_DESKTOP]]", protected_context)
        self.assertIn("protected [ [EVA_DESKTOP] ] value", protected_context)

    def test_last_four_digits_matches_released_identifier(self):
        self.request("POST", "/v1/protected-memory/enroll", {"slot_id": "last-four-slot"})
        self.request("POST", "/v1/protected-memory/records", {
            "value": "123-45-6789",
            "public_label": "government identifier",
            "category": "government_identifier",
        })
        self.request("POST", "/v1/protected-memory/unlock", {
            "slot_id": "last-four-slot", "allow_model_release": True,
        })
        old_memory = bridge_state.sqlite_mem
        old_backend = bridge_state.memory_backend
        old_enabled = bridge_state.cognition_enabled
        bridge_state.sqlite_mem = SqliteMemory(Path(self.tempdir.name) / "ordinary-memory.db")
        bridge_state.memory_backend = "sqlite"
        bridge_state.cognition_enabled = True
        try:
            context = _build_memory_context_sqlite("Can you tell me the last four digits?")
        finally:
            bridge_state.sqlite_mem.close()
            bridge_state.sqlite_mem = old_memory
            bridge_state.memory_backend = old_backend
            bridge_state.cognition_enabled = old_enabled
        self.assertIn("123-45-6789", context)


if __name__ == "__main__":
    unittest.main()