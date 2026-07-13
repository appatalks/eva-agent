#!/usr/bin/env python3
"""
Phase 0 Containment Tests — deterministic, no network, no providers, no real
databases. Uses temp homes, fake handlers, and in-process assertions.

Usage:
    python3 tools/test_phase0.py
"""

import ast
import hmac
import http.server
import ipaddress
import io
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from unittest import mock

# ── Ensure bridge package is importable ─────────────────────────────
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TOOLS_DIR)
sys.path.insert(0, TOOLS_DIR)

# Set a temp home so nothing touches real config
_TMP_HOME = tempfile.mkdtemp(prefix="eva_test_phase0_")
os.environ["HOME"] = _TMP_HOME
os.environ.setdefault("EVA_MEMORY_BACKEND", "sqlite")
os.environ.setdefault("EVA_MEMORY_DB", os.path.join(_TMP_HOME, "test.db"))
os.environ.pop("KUSTO_CLUSTER_URL", None)
os.environ.pop("KUSTO_DATABASE", None)
os.environ.pop("OPENAI_API_KEY", None)
# Ensure test mode doesn't auto-generate token files
os.environ["EVA_ALLOW_UNAUTHENTICATED_LOOPBACK"] = "1"


# ── Test server helper ──────────────────────────────────────────────
def _start_test_server(handler_class, token=""):
    """Start a bridge HTTP server on a random port; return (server, port, thread)."""
    from bridge import state as _st
    _st.bridge_auth_token = token
    _st.bridge_bind_address = "127.0.0.1"
    _st.acp_client = None
    _st.cognition_enabled = False
    _st.egress_mode = "cloud"

    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread


def _request(port, method, path, body=None, headers=None, token=None):
    """Make an HTTP request to the test server. Returns (status, body_dict_or_str, resp_headers)."""
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if headers:
        hdrs.update(headers)
    if data:
        hdrs["Content-Length"] = str(len(data))
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(req)
        raw = resp.read().decode()
        resp_headers = dict(resp.headers)
        try:
            return resp.status, json.loads(raw), resp_headers
        except json.JSONDecodeError:
            return resp.status, raw, resp_headers
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        resp_headers = dict(e.headers)
        try:
            return e.code, json.loads(raw), resp_headers
        except json.JSONDecodeError:
            return e.code, raw, resp_headers


# ═══════════════════════════════════════════════════════════════════
class TestAuthFailClosed(unittest.TestCase):
    """Per-launch bearer auth for /v1/* routes — fail closed."""

    @classmethod
    def setUpClass(cls):
        from bridge.core import BridgeHandler
        cls.token = secrets.token_urlsafe(32)
        cls.server, cls.port, cls.thread = _start_test_server(BridgeHandler, token=cls.token)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_health_no_auth(self):
        """GET /health works without auth (readiness probe)."""
        status, data, _ = _request(self.port, "GET", "/health")
        self.assertIn(status, (200,))
        self.assertIn("status", data)

    def test_health_redacted_without_auth(self):
        """GET /health without auth omits session details."""
        status, data, _ = _request(self.port, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertNotIn("session_id", data)
        self.assertNotIn("agent", data)

    def test_v1_get_rejected_without_token(self):
        """GET /v1/models returns 401 without auth."""
        status, data, _ = _request(self.port, "GET", "/v1/models")
        self.assertEqual(status, 401)

    def test_v1_get_rejected_wrong_token(self):
        """GET /v1/models returns 401 with wrong token."""
        status, data, _ = _request(self.port, "GET", "/v1/models", token="wrong-token")
        self.assertEqual(status, 401)

    def test_v1_get_accepted_correct_token(self):
        """GET /v1/models succeeds with correct token."""
        status, data, _ = _request(self.port, "GET", "/v1/models", token=self.token)
        self.assertNotEqual(status, 401)

    def test_post_rejected_without_token(self):
        """POST /v1/chat/completions returns 401 without auth."""
        status, data, _ = _request(self.port, "POST", "/v1/chat/completions",
                                body={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 401)

    def test_patch_rejected_without_token(self):
        """PATCH /v1/goals/test-id returns 401 without auth."""
        status, data, _ = _request(self.port, "PATCH", "/v1/goals/test-id",
                                body={"title": "test"})
        self.assertEqual(status, 401)

    def test_delete_rejected_without_token(self):
        """DELETE /v1/goals/test-id returns 401 without auth."""
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/goals/test-id",
            method="DELETE"
        )
        try:
            resp = urllib.request.urlopen(req)
            status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 401)

    def test_options_no_auth(self):
        """OPTIONS does not require auth."""
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/models",
            method="OPTIONS"
        )
        resp = urllib.request.urlopen(req)
        self.assertEqual(resp.status, 200)


class TestBridgeTokenFile(unittest.TestCase):

    def test_token_created_atomically_without_following_symlink(self):
        from bridge.core import _write_private_token_file
        root = tempfile.mkdtemp(prefix="eva_token_", dir=_TMP_HOME)
        token_dir = os.path.join(root, "config")
        os.makedirs(token_dir, mode=0o755)
        victim = os.path.join(root, "victim")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("victim-unchanged")
        token_path = os.path.join(token_dir, "bridge_token")
        os.symlink(victim, token_path)

        synthetic = "synthetic-bridge-token"
        _write_private_token_file(token_path, synthetic)

        self.assertFalse(os.path.islink(token_path))
        self.assertTrue(stat.S_ISREG(os.lstat(token_path).st_mode))
        self.assertEqual(stat.S_IMODE(os.lstat(token_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(token_dir).st_mode), 0o700)
        with open(token_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), synthetic)
        with open(victim, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "victim-unchanged")
        self.assertEqual(
            [name for name in os.listdir(token_dir) if name.endswith(".tmp")], []
        )

    def test_symlinked_token_directory_is_rejected(self):
        from bridge.core import _write_private_token_file
        root = tempfile.mkdtemp(prefix="eva_token_dir_", dir=_TMP_HOME)
        actual = os.path.join(root, "actual")
        os.mkdir(actual)
        linked = os.path.join(root, "linked")
        os.symlink(actual, linked)
        with self.assertRaises(OSError):
            _write_private_token_file(
                os.path.join(linked, "bridge_token"), "synthetic-token"
            )
        self.assertEqual(os.listdir(actual), [])

    def test_directory_swap_is_rejected_without_token_escape(self):
        from bridge.core import _write_private_token_file
        root = tempfile.mkdtemp(prefix="eva_token_swap_", dir=_TMP_HOME)
        token_dir = os.path.join(root, "config")
        replacement = os.path.join(root, "replacement")
        pinned = os.path.join(root, "pinned-original")
        os.mkdir(token_dir)
        os.mkdir(replacement)
        original_fchmod = os.fchmod
        swapped = {"done": False}

        def swap_after_directory_pin(fd, mode):
            original_fchmod(fd, mode)
            if not swapped["done"] and stat.S_ISDIR(os.fstat(fd).st_mode):
                swapped["done"] = True
                os.rename(token_dir, pinned)
                os.symlink(replacement, token_dir)

        with mock.patch(
            "bridge.core.os.fchmod", side_effect=swap_after_directory_pin
        ):
            with self.assertRaises(OSError):
                _write_private_token_file(
                    os.path.join(token_dir, "bridge_token"), "synthetic-token"
                )

        self.assertTrue(swapped["done"])
        self.assertEqual(os.listdir(replacement), [])
        self.assertEqual(os.listdir(pinned), [])


# ═══════════════════════════════════════════════════════════════════
class TestAuthDevEscape(unittest.TestCase):
    """EVA_ALLOW_UNAUTHENTICATED_LOOPBACK=1 dev escape hatch."""

    @classmethod
    def setUpClass(cls):
        os.environ["EVA_ALLOW_UNAUTHENTICATED_LOOPBACK"] = "1"
        from bridge.core import BridgeHandler
        cls.server, cls.port, cls.thread = _start_test_server(BridgeHandler, token="")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_v1_allowed_without_token(self):
        """GET /v1/models succeeds when escape hatch is set."""
        status, data, _ = _request(self.port, "GET", "/v1/models")
        self.assertNotEqual(status, 401)

    def test_escape_hatch_rejected_for_non_loopback_bind(self):
        from bridge import state as _st
        from bridge.core import BridgeHandler
        saved_bind = _st.bridge_bind_address
        saved_token = _st.bridge_auth_token
        try:
            _st.bridge_bind_address = "0.0.0.0"
            _st.bridge_auth_token = ""
            handler = object.__new__(BridgeHandler)
            handler.headers = {}
            errors = []
            handler._send_simple_error = lambda code, message: errors.append((code, message))
            self.assertFalse(handler._check_auth())
            self.assertEqual(errors[0][0], 401)
        finally:
            _st.bridge_bind_address = saved_bind
            _st.bridge_auth_token = saved_token


# ═══════════════════════════════════════════════════════════════════
class TestAuthFailClosedNoEscape(unittest.TestCase):
    """Without EVA_ALLOW_UNAUTHENTICATED_LOOPBACK, no-token mode rejects."""

    @classmethod
    def setUpClass(cls):
        cls._saved = os.environ.pop("EVA_ALLOW_UNAUTHENTICATED_LOOPBACK", None)
        from bridge.core import BridgeHandler
        cls.server, cls.port, cls.thread = _start_test_server(BridgeHandler, token="")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        if cls._saved is not None:
            os.environ["EVA_ALLOW_UNAUTHENTICATED_LOOPBACK"] = cls._saved

    def test_v1_rejected_no_token_no_escape(self):
        """GET /v1/models returns 401 with no token and no escape hatch."""
        status, data, _ = _request(self.port, "GET", "/v1/models")
        self.assertEqual(status, 401)


# ═══════════════════════════════════════════════════════════════════
class TestCORSOriginValidation(unittest.TestCase):
    """Exact-origin CORS policy with full serialized-origin validation."""

    @classmethod
    def setUpClass(cls):
        os.environ["EVA_ALLOW_UNAUTHENTICATED_LOOPBACK"] = "1"
        from bridge.core import BridgeHandler
        cls.handler = BridgeHandler
        cls.server, cls.port, cls.thread = _start_test_server(BridgeHandler, token="")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _options_origin(self, origin):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/models",
            method="OPTIONS",
            headers={"Origin": origin}
        )
        resp = urllib.request.urlopen(req)
        return dict(resp.headers)

    def test_loopback_allowed(self):
        headers = self._options_origin("http://127.0.0.1:8888")
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")

    def test_localhost_allowed(self):
        headers = self._options_origin("http://localhost:3000")
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")

    def test_file_origin_allowed(self):
        headers = self._options_origin("file://")
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")

    def test_evil_origin_rejected(self):
        headers = self._options_origin("http://localhost.evil")
        self.assertNotIn("http://localhost.evil", headers.get("Access-Control-Allow-Origin", ""))

    def test_external_origin_rejected(self):
        headers = self._options_origin("https://evil.example.com")
        acao = headers.get("Access-Control-Allow-Origin", "")
        self.assertNotIn("evil.example.com", acao)

    def test_null_origin_rejected(self):
        self.assertFalse(self.handler._origin_allowed("null"))

    def test_origin_with_path_rejected(self):
        self.assertFalse(self.handler._origin_allowed("http://127.0.0.1:8888/foo"))

    def test_origin_with_trailing_slash_rejected(self):
        self.assertFalse(self.handler._origin_allowed("http://127.0.0.1:8888/"))

    def test_origin_with_query_rejected(self):
        self.assertFalse(self.handler._origin_allowed("http://127.0.0.1:8888?x=1"))

    def test_origin_with_credentials_rejected(self):
        self.assertFalse(self.handler._origin_allowed("http://user:pass@127.0.0.1:8888"))

    def test_ftp_scheme_rejected(self):
        self.assertFalse(self.handler._origin_allowed("ftp://127.0.0.1"))

    def test_file_with_path_rejected(self):
        self.assertFalse(self.handler._origin_allowed("file:///path/to/index.html"))

    def test_explicit_external_origin_allowlist_is_exact(self):
        saved = os.environ.get("EVA_ALLOWED_ORIGINS")
        os.environ["EVA_ALLOWED_ORIGINS"] = "https://eva.example.com"
        try:
            self.assertTrue(self.handler._origin_allowed("https://eva.example.com"))
            self.assertFalse(self.handler._origin_allowed("https://eva.example.com.evil"))
            self.assertFalse(self.handler._origin_allowed("https://eva.example.com/"))
        finally:
            if saved is None:
                os.environ.pop("EVA_ALLOWED_ORIGINS", None)
            else:
                os.environ["EVA_ALLOWED_ORIGINS"] = saved

    def test_cors_header_uses_a_canonical_origin_value(self):
        self.assertEqual(
            self.handler._allowed_cors_origin("http://127.0.0.1:8888"),
            "*",
        )
        self.assertIsNone(
            self.handler._allowed_cors_origin(
                "http://127.0.0.1:8888\r\nX-Injected: true"
            )
        )

    def test_vary_origin_present(self):
        headers = self._options_origin("http://127.0.0.1:8888")
        self.assertIn("Origin", headers.get("Vary", ""))

    def test_error_response_has_cors(self):
        from bridge import state as _st
        saved_token = _st.bridge_auth_token
        _st.bridge_auth_token = "test-token-cors"
        try:
            status, _, headers = _request(self.port, "GET", "/v1/models",
                                          headers={"Origin": "http://127.0.0.1:9999"})
            self.assertEqual(status, 401)
            self.assertIn("Origin", headers.get("Vary", ""))
        finally:
            _st.bridge_auth_token = saved_token


# ═══════════════════════════════════════════════════════════════════
class TestContentEnforcement(unittest.TestCase):
    """Content-Type, Content-Length, and Transfer-Encoding enforcement."""

    @classmethod
    def setUpClass(cls):
        os.environ["EVA_ALLOW_UNAUTHENTICATED_LOOPBACK"] = "1"
        from bridge.core import BridgeHandler
        cls.server, cls.port, cls.thread = _start_test_server(BridgeHandler, token="")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_post_wrong_content_type(self):
        body = b'{"messages":[]}'
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=body, method="POST",
            headers={"Content-Type": "text/plain", "Content-Length": str(len(body))}
        )
        try:
            resp = urllib.request.urlopen(req)
            status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 415)

    def test_post_jsonp_rejected(self):
        body = b'{"messages":[]}'
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=body, method="POST",
            headers={"Content-Type": "application/jsonp", "Content-Length": str(len(body))}
        )
        try:
            resp = urllib.request.urlopen(req)
            status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 415)

    def test_post_too_large_body(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=b'{}', method="POST",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(2 * 1024 * 1024)
            }
        )
        try:
            resp = urllib.request.urlopen(req)
            status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 413)

    def test_post_negative_content_length(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=b'{}', method="POST",
            headers={
                "Content-Type": "application/json",
                "Content-Length": "-1"
            }
        )
        try:
            resp = urllib.request.urlopen(req)
            status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 400)

    def test_post_nonnumeric_content_length(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=b'{}', method="POST",
            headers={
                "Content-Type": "application/json",
                "Content-Length": "abc"
            }
        )
        try:
            resp = urllib.request.urlopen(req)
            status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 400)

    def test_transfer_encoding_rejected(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=b'{}', method="POST",
            headers={
                "Content-Type": "application/json",
                "Content-Length": "2",
                "Transfer-Encoding": "chunked"
            }
        )
        try:
            resp = urllib.request.urlopen(req)
            status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 400)


# ═══════════════════════════════════════════════════════════════════
class TestBodylessRoutes(unittest.TestCase):
    """Bodyless action routes accept requests without JSON body."""

    @classmethod
    def setUpClass(cls):
        os.environ["EVA_ALLOW_UNAUTHENTICATED_LOOPBACK"] = "1"
        from bridge.core import BridgeHandler
        cls.server, cls.port, cls.thread = _start_test_server(BridgeHandler, token="")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post_no_body(self, path):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=b'', method="POST",
            headers={"Content-Length": "0"}
        )
        try:
            resp = urllib.request.urlopen(req)
            return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_camera_stop_no_body(self):
        status = self._post_no_body("/v1/camera/stop")
        self.assertEqual(status, 200)

    def test_files_purge_no_body(self):
        status = self._post_no_body("/v1/files/purge")
        self.assertEqual(status, 200)

    def test_payload_route_requires_json(self):
        status = self._post_no_body("/v1/browser/cancel")
        self.assertEqual(status, 415)

    def test_bodyless_route_rejects_nonempty_body(self):
        status, _, _ = _request(
            self.port, "POST", "/v1/files/purge", body={"unexpected": True}
        )
        self.assertEqual(status, 400)


# ═══════════════════════════════════════════════════════════════════
class TestBackgroundProposals(unittest.TestCase):
    """Background proposals: auto_apply flag, pending creation."""

    def test_create_pending_proposal(self):
        from bridge.background import _create_background_proposal_row
        row = _create_background_proposal_row(
            "test_job", "MemorySummaries",
            {"Summary": "test"}, "2026-01-01", "2026-01-02",
            "test notes", status="pending"
        )
        self.assertEqual(row["Status"], "pending")

    def test_create_applied_proposal(self):
        from bridge.background import _create_background_proposal_row
        row = _create_background_proposal_row(
            "test_job", "MemorySummaries",
            {"Summary": "test"}, "2026-01-01", "2026-01-02",
            "auto-applied test", status="applied"
        )
        self.assertEqual(row["Status"], "applied")

    def test_sqlite_latest_proposal_uses_reviewed_version(self):
        from bridge import config as cfg
        from bridge import state as _st
        from bridge.core import BridgeHandler
        from sqlite_memory import SqliteMemory

        saved_backend, saved_mem = _st.memory_backend, _st.sqlite_mem
        with tempfile.TemporaryDirectory() as td:
            try:
                _st.memory_backend = "sqlite"
                _st.sqlite_mem = SqliteMemory(os.path.join(td, "proposal.db"))
                base = {
                    "ProposalId": "bgp-latest", "CreatedAt": "2026-01-01T00:00:00Z",
                    "JobType": "test", "TargetTable": "Reflections", "Payload": {},
                    "Status": "pending", "SourceWindowStart": "", "SourceWindowEnd": "",
                    "Notes": "", "ReviewedAt": "", "ReviewedBy": "",
                }
                _st.sqlite_mem.ingest("BackgroundProposals", cfg.BG_PROPOSAL_COLUMNS, [base])
                applying = dict(base, Status="applying", ReviewedAt="2026-01-01T00:00:01Z", ReviewedBy="test")
                _st.sqlite_mem.ingest("BackgroundProposals", cfg.BG_PROPOSAL_COLUMNS, [applying])
                handler = object.__new__(BridgeHandler)
                row, error = handler._background_latest_proposal_by_id(None, None, "bgp-latest")
                self.assertEqual(error, "")
                self.assertEqual(row["Status"], "applying")
            finally:
                _st.sqlite_mem._conn().close()
                _st.memory_backend, _st.sqlite_mem = saved_backend, saved_mem

    def test_reflection_application_is_retry_idempotent(self):
        from bridge import state as _st
        from bridge.background import _apply_proposal_payload
        from sqlite_memory import SqliteMemory

        saved_backend, saved_mem = _st.memory_backend, _st.sqlite_mem
        with tempfile.TemporaryDirectory() as td:
            try:
                _st.memory_backend = "sqlite"
                _st.sqlite_mem = SqliteMemory(os.path.join(td, "apply.db"))
                payload = {
                    "Timestamp": "2026-07-09T12:00:00Z",
                    "Trigger": "test",
                    "Observation": "A deterministic observation",
                    "ActionTaken": "none",
                    "Effectiveness": 0.5,
                }
                first = _apply_proposal_payload(None, None, "Reflections", payload)
                second = _apply_proposal_payload(None, None, "Reflections", payload)
                self.assertTrue(first[0])
                self.assertTrue(second[0])
                rows = _st.sqlite_mem.query(
                    "SELECT COUNT(*) AS N FROM Reflections WHERE Trigger = ? AND Observation = ?",
                    (payload["Trigger"], payload["Observation"]),
                )
                self.assertEqual(rows[0]["N"], 1)
            finally:
                _st.sqlite_mem._conn().close()
                _st.memory_backend, _st.sqlite_mem = saved_backend, saved_mem

    def test_summary_application_with_special_text_is_retry_idempotent(self):
        from bridge import state as _st
        from bridge.background import _apply_proposal_payload
        from sqlite_memory import SqliteMemory

        saved_backend, saved_mem = _st.memory_backend, _st.sqlite_mem
        with tempfile.TemporaryDirectory() as td:
            try:
                _st.memory_backend = "sqlite"
                _st.sqlite_mem = SqliteMemory(os.path.join(td, "summary.db"))
                payload = {
                    "Period": "day:2026-07-09",
                    "Summary": "user's summary, with \"quotes\"\nand a newline",
                    "Timestamp": "2026-07-09T12:00:00Z",
                }
                self.assertTrue(_apply_proposal_payload(None, None, "MemorySummaries", payload)[0])
                self.assertTrue(_apply_proposal_payload(None, None, "MemorySummaries", payload)[0])
                rows = _st.sqlite_mem.query(
                    "SELECT COUNT(*) AS N FROM MemorySummaries WHERE Period = ? AND Summary = ?",
                    (payload["Period"], payload["Summary"]),
                )
                self.assertEqual(rows[0]["N"], 1)
            finally:
                _st.sqlite_mem._conn().close()
                _st.memory_backend, _st.sqlite_mem = saved_backend, saved_mem

    def test_kusto_summary_retry_uses_ingest_canonicalization(self):
        from bridge import state as _st
        from bridge.background import _apply_proposal_payload
        from bridge.kusto import _canonical_kusto_ingest_string

        saved_backend, saved_mode = _st.memory_backend, _st.egress_mode
        inserted = []
        queries = []
        payload = {
            "Period": "day:2026-07-09",
            "Summary": "user's \"quoted\" line\r\nsecond line",
            "Timestamp": "2026-07-09T12:00:00Z",
        }

        def fake_query(cluster, database, query):
            queries.append(query)
            return [{"exists": 1}] if inserted else []

        def fake_ingest(cluster, database, table, columns, rows):
            inserted.append({
                key: _canonical_kusto_ingest_string(value)
                for key, value in rows[0].items()
            })
            return True

        try:
            _st.memory_backend = "kusto"
            _st.egress_mode = "cloud"
            with mock.patch("bridge.background._kusto_query_direct", side_effect=fake_query), \
                 mock.patch("bridge.background._kusto_ingest_direct", side_effect=fake_ingest):
                self.assertTrue(_apply_proposal_payload("cluster", "db", "MemorySummaries", payload)[0])
                self.assertTrue(_apply_proposal_payload("cluster", "db", "MemorySummaries", payload)[0])
            self.assertEqual(len(inserted), 1)
            self.assertNotIn("\r", inserted[0]["Summary"])
            self.assertIn("\\n", inserted[0]["Summary"])
            expected = _canonical_kusto_ingest_string(payload["Summary"])
            # KQL escapes backslashes, but must query the same canonical value.
            self.assertIn(expected.replace("\\", "\\\\").replace("'", "\\'"), queries[-1])
        finally:
            _st.memory_backend, _st.egress_mode = saved_backend, saved_mode

    def test_kusto_reflection_retry_uses_ingest_canonicalization(self):
        from bridge import state as _st
        from bridge.background import _apply_proposal_payload
        from bridge.kusto import _canonical_kusto_ingest_string

        saved_backend, saved_mode = _st.memory_backend, _st.egress_mode
        inserted = []
        queries = []
        payload = {
            "Timestamp": "2026-07-09T12:00:00Z",
            "Trigger": "user's trigger\r\ncontinued",
            "Observation": "first line\r\nsecond \"quoted\" line",
            "ActionTaken": "reviewed\ncarefully",
            "Effectiveness": 0.75,
        }

        def fake_query(cluster, database, query):
            queries.append(query)
            return [{"exists": 1}] if inserted else []

        def fake_ingest(cluster, database, table, columns, rows):
            inserted.append({
                key: _canonical_kusto_ingest_string(value)
                if key in ("Trigger", "Observation", "ActionTaken") else value
                for key, value in rows[0].items()
            })
            return True

        try:
            _st.memory_backend = "kusto"
            _st.egress_mode = "cloud"
            with mock.patch("bridge.background._kusto_query_direct", side_effect=fake_query), \
                 mock.patch("bridge.background._kusto_ingest_direct", side_effect=fake_ingest):
                self.assertTrue(_apply_proposal_payload("cluster", "db", "Reflections", payload)[0])
                self.assertTrue(_apply_proposal_payload("cluster", "db", "Reflections", payload)[0])
            self.assertEqual(len(inserted), 1)
            for field in ("Trigger", "Observation", "ActionTaken"):
                expected = _canonical_kusto_ingest_string(inserted[0][field])
                self.assertIn(expected.replace("\\", "\\\\").replace("'", "\\'"), queries[-1])
        finally:
            _st.memory_backend, _st.egress_mode = saved_backend, saved_mode

    def test_pending_helpers_ignore_applied_and_rejected_history(self):
        from bridge import config as cfg
        from bridge import state as _st
        from bridge.background import _existing_goal_checkin_ids, _pending_proposal_exists
        from sqlite_memory import SqliteMemory

        saved_backend, saved_mem = _st.memory_backend, _st.sqlite_mem
        with tempfile.TemporaryDirectory() as td:
            try:
                _st.memory_backend = "sqlite"
                _st.sqlite_mem = SqliteMemory(os.path.join(td, "dedup.db"))
                for proposal_id, final_status, goal_id in (
                    ("bgp-applied", "applied", "goal-applied"),
                    ("bgp-rejected", "rejected", "goal-rejected"),
                ):
                    base = {
                        "ProposalId": proposal_id, "CreatedAt": "2026-01-01T00:00:00Z",
                        "JobType": "goal_checkin", "TargetTable": "Reflections",
                        "Payload": {"GoalId": goal_id}, "Status": "pending",
                        "SourceWindowStart": "", "SourceWindowEnd": "", "Notes": "",
                        "ReviewedAt": "", "ReviewedBy": "",
                    }
                    latest = dict(
                        base, Status=final_status, ReviewedAt="2026-01-01T00:00:01Z",
                        ReviewedBy="test",
                    )
                    _st.sqlite_mem.ingest("BackgroundProposals", cfg.BG_PROPOSAL_COLUMNS, [base, latest])
                self.assertFalse(_pending_proposal_exists(None, None, "goal_checkin"))
                self.assertEqual(_existing_goal_checkin_ids(None, None), set())
            finally:
                _st.sqlite_mem._conn().close()
                _st.memory_backend, _st.sqlite_mem = saved_backend, saved_mem

    def test_pending_helpers_treat_applying_as_active(self):
        from bridge import config as cfg
        from bridge import state as _st
        from bridge.background import _existing_goal_checkin_ids, _pending_proposal_exists
        from sqlite_memory import SqliteMemory

        saved_backend, saved_mem = _st.memory_backend, _st.sqlite_mem
        with tempfile.TemporaryDirectory() as td:
            try:
                _st.memory_backend = "sqlite"
                _st.sqlite_mem = SqliteMemory(os.path.join(td, "applying.db"))
                base = {
                    "ProposalId": "bgp-applying", "CreatedAt": "2026-01-01T00:00:00Z",
                    "JobType": "goal_checkin", "TargetTable": "Reflections",
                    "Payload": {"GoalId": "goal-active"}, "Status": "pending",
                    "SourceWindowStart": "", "SourceWindowEnd": "", "Notes": "",
                    "ReviewedAt": "", "ReviewedBy": "",
                }
                applying = dict(base, Status="applying", ReviewedAt="2026-01-01T00:00:01Z")
                _st.sqlite_mem.ingest("BackgroundProposals", cfg.BG_PROPOSAL_COLUMNS, [base, applying])
                self.assertTrue(_pending_proposal_exists(None, None, "goal_checkin"))
                self.assertEqual(_existing_goal_checkin_ids(None, None), {"goal-active"})
            finally:
                _st.sqlite_mem._conn().close()
                _st.memory_backend, _st.sqlite_mem = saved_backend, saved_mem

    def test_proposal_transition_keys_are_strictly_increasing(self):
        from bridge import state as _st
        from bridge.background import _proposal_transition_iso
        saved = _st.proposal_last_transition_at
        try:
            _st.proposal_last_transition_at = None
            values = [_proposal_transition_iso() for _ in range(4)]
            self.assertEqual(values, sorted(values))
            self.assertEqual(len(values), len(set(values)))
            self.assertTrue(all("." in value for value in values))
        finally:
            _st.proposal_last_transition_at = saved


# ═══════════════════════════════════════════════════════════════════
class TestACPContainment(unittest.TestCase):
    """ACP command arg/shell policy and default-deny permissions."""

    def test_terminal_default_disabled(self):
        os.environ.pop("EVA_ALLOW_ACP_TERMINAL", None)
        from bridge.acp_client import ACPClient
        client = ACPClient.__new__(ACPClient)
        client.__init__()
        client.session_id = "session-test"
        self.assertFalse(client._terminal_allowed)

    def test_terminal_enabled_by_env(self):
        os.environ["EVA_ALLOW_ACP_TERMINAL"] = "1"
        try:
            from bridge.acp_client import ACPClient
            client = ACPClient.__new__(ACPClient)
            client.__init__()
            self.assertFalse(client._terminal_allowed)
        finally:
            os.environ.pop("EVA_ALLOW_ACP_TERMINAL", None)

    def test_terminal_create_helper_is_always_disabled(self):
        from bridge.acp_client import ACPClient
        client = ACPClient.__new__(ACPClient)
        client.__init__()
        client._terminal_allowed = True
        responses = []
        client._send_error_response = lambda rid, code, message: responses.append(
            (rid, code, message)
        )
        client._handle_terminal_create(99, {"command": 123, "args": []})
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0][1], -32601)

    def test_terminal_create_cannot_be_enabled_by_valid_arguments(self):
        from bridge.acp_client import ACPClient
        client = ACPClient.__new__(ACPClient)
        client.__init__(cwd="/tmp/safe")
        client._terminal_allowed = True
        responses = []
        client._send_error_response = lambda rid, code, message: responses.append(
            (rid, code, message)
        )
        client._handle_terminal_create(99, {"command": "ls", "args": [], "cwd": "/etc"})
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0][1], -32601)

    def test_default_deny_permission(self):
        from bridge.acp_client import ACPClient
        client = ACPClient.__new__(ACPClient)
        client.__init__()
        client.session_id = "session-test"
        client._terminal_allowed = False
        responses = []
        client._send_response = lambda rid, result: responses.append((rid, result))
        msg = {
            "jsonrpc": "2.0",
            "id": 42,
            "method": "session/request_permission",
            "params": {
                "sessionId": "session-test",
                "toolCall": {"toolCallId": "tool-42", "kind": "other", "status": "pending"},
                "options": [
                    {"optionId": "option-allow", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "option-reject", "name": "Reject", "kind": "reject_once"},
                ]
            }
        }
        client._handle_message(msg)
        self.assertEqual(len(responses), 1)
        outcome = responses[0][1].get("outcome", {})
        self.assertEqual(outcome, {"outcome": "selected", "optionId": "option-reject"})

    def test_terminal_permission_rejects_even_with_allow_once(self):
        from bridge.acp_client import ACPClient
        client = ACPClient.__new__(ACPClient)
        client.__init__()
        client.session_id = "session-test"
        client._terminal_allowed = False
        responses = []
        client._send_response = lambda rid, result: responses.append((rid, result))
        msg = {
            "jsonrpc": "2.0",
            "id": 43,
            "method": "session/request_permission",
            "params": {
                "sessionId": "session-test",
                "toolCall": {"toolCallId": "tool-43", "kind": "execute", "status": "pending"},
                "options": [
                    {"optionId": "option-allow", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "option-reject", "name": "Reject", "kind": "reject_once"},
                ]
            }
        }
        client._handle_message(msg)
        self.assertEqual(len(responses), 1)
        outcome = responses[0][1].get("outcome", {})
        self.assertEqual(outcome.get("outcome"), "selected")
        self.assertEqual(outcome.get("optionId"), "option-reject")

    def test_terminal_denied_without_allow_once_option(self):
        from bridge.acp_client import ACPClient
        client = ACPClient.__new__(ACPClient)
        client.__init__()
        client.session_id = "session-test"
        client._terminal_allowed = True
        responses = []
        client._send_response = lambda rid, result: responses.append((rid, result))
        msg = {
            "jsonrpc": "2.0",
            "id": 44,
            "method": "session/request_permission",
            "params": {
                "sessionId": "session-test",
                "toolCall": {"toolCallId": "tool-44", "kind": "execute", "status": "pending"},
                "options": [{"optionId": "option-reject", "name": "Reject", "kind": "reject_once"}]
            }
        }
        client._handle_message(msg)
        self.assertEqual(len(responses), 1)
        outcome = responses[0][1].get("outcome", {})
        self.assertEqual(outcome, {"outcome": "selected", "optionId": "option-reject"})

    def test_permission_without_reject_option_is_cancelled(self):
        from bridge.acp_client import ACPClient
        client = ACPClient()
        client.session_id = "session-test"
        responses = []
        client._send_response = lambda rid, result: responses.append((rid, result))
        client._handle_message({
            "jsonrpc": "2.0",
            "id": 45,
            "method": "session/request_permission",
            "params": {
                "sessionId": "session-test",
                "toolCall": {"toolCallId": "tool-45", "kind": "other", "status": "pending"},
                "options": [{"optionId": "option-allow", "name": "Allow", "kind": "allow_once"}],
            },
        })
        self.assertEqual(responses[0][1], {"outcome": {"outcome": "cancelled"}})

    def test_all_terminal_methods_are_protocol_errors(self):
        from bridge.acp_client import ACPClient
        client = ACPClient()
        errors = []
        client._send_error_response = lambda rid, code, message: errors.append((rid, code, message))
        for index, method in enumerate((
            "terminal/create", "terminal/output", "terminal/wait_for_exit",
            "terminal/kill", "terminal/release",
        ), start=1):
            client._handle_message({
                "jsonrpc": "2.0", "id": index,
                "method": method, "params": {},
            })
        self.assertEqual(len(errors), 5)
        self.assertTrue(all(item[1] == -32601 for item in errors))

    def test_request_timeout_quarantines_session(self):
        from bridge.acp_client import ACPClient, ACPRequestError

        class FakeStdin:
            def __init__(self):
                self.frames = []
            def write(self, value):
                self.frames.append(value)
            def flush(self):
                pass
            def close(self):
                pass

        class FakeProcess:
            def __init__(self):
                self.stdin = FakeStdin()
                self.terminated = False
            def terminate(self):
                self.terminated = True
            def wait(self, timeout=None):
                return 0
            def kill(self):
                self.terminated = True

        client = ACPClient()
        client.process = FakeProcess()
        client.alive = True
        client.session_id = "session-timeout"
        with self.assertRaisesRegex(ACPRequestError, "timed out"):
            client._send_request(
                "session/prompt", {"sessionId": client.session_id}, timeout=0.01
            )
        self.assertFalse(client.alive)
        self.assertIsNone(client.session_id)
        self.assertTrue(client.process.terminated)
        frames = b"".join(client.process.stdin.frames).decode("utf-8")
        self.assertIn('"method": "session/cancel"', frames)

        # A late chunk from the retired process has no active prompt target.
        client.response_chunks[999] = "sentinel"
        client._handle_session_update({
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "late"}}
        })
        self.assertEqual(client.response_chunks[999], "sentinel")


# ═══════════════════════════════════════════════════════════════════
class TestOfflinePolicy(unittest.TestCase):
    """Offline egress mode policy helpers."""

    def test_offline_state(self):
        os.environ["EVA_EGRESS_MODE"] = "offline"
        import importlib
        from bridge import state as _st
        importlib.reload(_st)
        self.assertEqual(_st.egress_mode, "offline")
        os.environ["EVA_EGRESS_MODE"] = "cloud"
        importlib.reload(_st)

    def test_cloud_default(self):
        os.environ.pop("EVA_EGRESS_MODE", None)
        import importlib
        from bridge import state as _st
        importlib.reload(_st)
        self.assertEqual(_st.egress_mode, "cloud")
        os.environ["EVA_EGRESS_MODE"] = "cloud"
        importlib.reload(_st)

    def test_invalid_egress_falls_back(self):
        os.environ["EVA_EGRESS_MODE"] = "invalid"
        import importlib
        from bridge import state as _st
        importlib.reload(_st)
        self.assertEqual(_st.egress_mode, "cloud")
        self.assertTrue(_st.egress_mode_invalid)
        os.environ["EVA_EGRESS_MODE"] = "cloud"
        importlib.reload(_st)

    def test_offline_rejects_npx_mcp(self):
        from bridge.config import mcp_config_for_egress
        config = {
            "azure-mcp": {"command": "npx", "args": ["@azure/mcp"]},
            "local": {"command": "/usr/bin/tool", "args": []},
        }
        safe, rejected = mcp_config_for_egress(config, "offline")
        self.assertNotIn("azure-mcp", safe)
        self.assertNotIn("local", safe)
        self.assertEqual(set(rejected), {"azure-mcp", "local"})

    def test_offline_rejects_docker_mcp(self):
        from bridge.config import mcp_config_for_egress
        config = {"github": {"command": "docker", "args": ["run"]}}
        safe, rejected = mcp_config_for_egress(config, "offline")
        self.assertNotIn("github", safe)
        self.assertEqual(rejected, ["github"])

    def test_offline_allows_only_bundled_sqlite_mcp(self):
        from bridge import config as cfg
        config = {
            "sqlite": {"command": sys.executable, "args": [os.path.join(TOOLS_DIR, "sqlite_mcp.py")]},
            "arbitrary": {"command": sys.executable, "args": [os.path.join(TOOLS_DIR, "web_search_mcp.py")]},
        }
        safe, rejected = cfg.mcp_config_for_egress(config, "offline")
        self.assertEqual(set(safe), {"sqlite"})
        self.assertEqual(rejected, ["arbitrary"])

    def test_restricted_mcp_rejects_fake_python_and_extra_args(self):
        from bridge import config as cfg
        sqlite_path = os.path.join(TOOLS_DIR, "sqlite_mcp.py")
        source = {
            "fake-python": {
                "command": "/tmp/python-evil", "args": [sqlite_path],
                "env": {"PYTHONPATH": "/tmp/attacker"},
            },
            "extra-args": {
                "command": sys.executable, "args": [sqlite_path, "--unexpected"],
            },
        }
        safe, rejected = cfg.mcp_config_for_egress(source, "offline")
        self.assertEqual(safe, {})
        self.assertEqual(set(rejected), set(source))

    def test_restricted_mcp_strips_unapproved_environment(self):
        from bridge import config as cfg
        canonical_db = os.path.join(_TMP_HOME, "memory.db")
        source = {
            "sqlite": {
                "command": sys.executable,
                "args": [os.path.join(TOOLS_DIR, "sqlite_mcp.py")],
                "env": {"EVA_MEMORY_DB": "/tmp/test.db", "PYTHONPATH": "/tmp/attacker"},
            }
        }
        with mock.patch.dict(os.environ, {"EVA_MEMORY_DB": canonical_db}):
            safe, rejected = cfg.mcp_config_for_egress(source, "offline")
            self.assertEqual(safe, {})
            self.assertEqual(rejected, ["sqlite"])
            source["sqlite"]["env"]["EVA_MEMORY_DB"] = canonical_db
            safe, rejected = cfg.mcp_config_for_egress(source, "offline")
        self.assertEqual(rejected, [])
        self.assertEqual(safe["sqlite"]["env"], {"EVA_MEMORY_DB": canonical_db})

    def test_local_network_does_not_enable_cloud_mcp(self):
        from bridge.config import mcp_config_for_egress
        safe, rejected = mcp_config_for_egress(
            {"playwright": {"command": "npx", "args": ["@playwright/mcp"]}},
            "local-network",
        )
        self.assertEqual(safe, {})
        self.assertEqual(rejected, ["playwright"])

    def test_cloud_rejects_unbrokered_mcp(self):
        from bridge.config import mcp_config_for_egress
        source = {"playwright": {"command": "npx", "args": ["@playwright/mcp"]}}
        safe, rejected = mcp_config_for_egress(source, "cloud")
        self.assertEqual(safe, {})
        self.assertEqual(rejected, ["playwright"])

    def test_cloud_allows_only_exact_release_presets(self):
        from bridge import config as cfg
        source = {
            "azure-mcp-server": {
                "command": "npx",
                "args": ["-y", "@azure/mcp@latest", "server", "start"],
                "env": {"AZURE_MCP_COLLECT_TELEMETRY": "false"},
            },
            "github-mcp-server": {
                "command": "docker",
                "args": [
                    "run", "-i", "--rm", "-e",
                    "GITHUB_PERSONAL_ACCESS_TOKEN",
                    "ghcr.io/github/github-mcp-server",
                ],
                "env": {"_useGitHubPAT": True},
            },
            "kusto-mcp-server": {
                "command": "python3",
                "args": ["tools/kusto_mcp.py"],
                "env": {
                    "KUSTO_DATABASE_LOCKED": "1",
                    "KUSTO_DATABASE": "Eva",
                    "KUSTO_CLUSTER_URL":
                        "HTTPS://CLUSTER.REGION.KUSTO.WINDOWS.NET:443/",
                },
            },
            "eva-web-search": {
                "command": sys.executable,
                "args": [os.path.join(TOOLS_DIR, "web_search_mcp.py")],
            },
        }
        safe, rejected = cfg.mcp_config_for_egress(source, "cloud")
        self.assertEqual(rejected, [])
        self.assertEqual(set(safe), set(source))
        self.assertEqual(safe["kusto-mcp-server"]["command"], sys.executable)
        self.assertEqual(
            safe["kusto-mcp-server"]["args"],
            [os.path.realpath(os.path.join(TOOLS_DIR, "kusto_mcp.py"))],
        )
        self.assertEqual(
            safe["kusto-mcp-server"]["env"]["KUSTO_CLUSTER_URL"],
            "https://cluster.region.kusto.windows.net",
        )

    def test_cloud_rejects_invalid_kusto_origin_and_unknown_env(self):
        from bridge.config import mcp_config_for_egress

        base = {"command": "python3", "args": ["tools/kusto_mcp.py"]}
        variants = (
            {**base, "env": {"KUSTO_CLUSTER_URL": "https://evil.example/path"}},
            {**base, "env": {"KUSTO_CLUSTER_URL": "file:///tmp/socket"}},
            {**base, "env": {
                "KUSTO_CLUSTER_URL": "https://cluster.kusto.windows.net",
                "PYTHONPATH": "/tmp/inject",
            }},
        )
        for config in variants:
            with self.subTest(config=config):
                safe, rejected = mcp_config_for_egress(
                    {"kusto-mcp-server": config}, "cloud"
                )
                self.assertEqual(safe, {})
                self.assertEqual(rejected, ["kusto-mcp-server"])

    def test_cloud_rejects_wrappers_even_under_approved_names(self):
        from bridge.config import mcp_config_for_egress
        source = {
            "azure-mcp-server": {
                "command": "npx", "args": ["-c", "computer-use-linux mcp"],
            },
            "kusto-mcp-server": {
                "command": "python3.12", "args": ["-c", "print('unsafe')"],
            },
            "github-mcp-server": {
                "command": "busybox", "args": ["sh", "-c", "unsafe"],
            },
        }
        safe, rejected = mcp_config_for_egress(source, "cloud")
        self.assertEqual(safe, {})
        self.assertEqual(set(rejected), set(source))

    def test_restricted_mode_blocks_kusto_below_http_layer(self):
        from bridge import state as _st
        from bridge.kusto import _ensure_kusto_token, _kusto_query_direct
        saved_mode, saved_token = _st.egress_mode, _st.kusto_token_cache
        try:
            _st.egress_mode = "offline"
            _st.kusto_token_cache = "fake-token"
            ok, error = _ensure_kusto_token()
            self.assertFalse(ok)
            self.assertIn("EVA_EGRESS_MODE", error)
            self.assertIsNone(_kusto_query_direct("https://example.invalid", "Eva", "print 1"))
        finally:
            _st.egress_mode, _st.kusto_token_cache = saved_mode, saved_token

    def test_local_network_does_not_call_public_embeddings(self):
        from bridge import state as _st
        from bridge.memory import _embed_texts
        saved = (
            _st.egress_mode, _st.openai_api_key_cache,
            _st.embedding_cache, _st.embedding_disabled_logged,
        )
        try:
            _st.egress_mode = "local-network"
            _st.openai_api_key_cache = "fake-key"
            _st.embedding_cache = {}
            _st.embedding_disabled_logged = False
            with mock.patch("requests.post") as post:
                self.assertEqual(_embed_texts(["phase-zero-no-egress-sentinel"]), {})
                post.assert_not_called()
        finally:
            (
                _st.egress_mode, _st.openai_api_key_cache,
                _st.embedding_cache, _st.embedding_disabled_logged,
            ) = saved

    def test_skill_import_never_fetches_external_urls(self):
        from bridge.skills import _fetch_skill_source
        for source_type, data in (
            ("url", {"url": "https://example.invalid/skill"}),
            ("github", {"repo": "owner/repo"}),
        ):
            with self.subTest(source_type=source_type):
                text, error = _fetch_skill_source(source_type, data)
                self.assertIsNone(text)
                self.assertIn("remote skill imports are disabled", error)

    def test_restricted_mode_blocks_signal_before_subprocess(self):
        from bridge import state as _st
        from bridge.alerts import _signal_send
        saved_mode = _st.egress_mode
        try:
            _st.egress_mode = "offline"
            self.assertFalse(_signal_send("test"))
        finally:
            _st.egress_mode = saved_mode

    def test_offline_lmstudio_requires_loopback(self):
        from bridge import state as _st
        from bridge.utils import _validate_lmstudio_base_url
        saved_mode = _st.egress_mode
        private_url = (
            "http://" + str(ipaddress.IPv4Address(3232235778)) + ":1234/v1"
        )
        try:
            _st.egress_mode = "offline"
            self.assertTrue(_validate_lmstudio_base_url(private_url)[1])
            self.assertEqual(
                _validate_lmstudio_base_url("http://127.0.0.1:1234/v1")[0],
                "http://127.0.0.1:1234/v1",
            )
            _st.egress_mode = "local-network"
            self.assertEqual(
                _validate_lmstudio_base_url(private_url)[0],
                private_url,
            )
        finally:
            _st.egress_mode = saved_mode

    def test_child_environment_strips_ambient_credentials(self):
        from bridge.config import child_process_env, is_sensitive_env_name
        saved = dict(os.environ)
        try:
            os.environ["EVA_BRIDGE_TOKEN"] = "bridge-secret"
            os.environ["OPENAI_API_KEY"] = "provider-secret"
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/tmp/credential.json"
            os.environ["NPM_CONFIG__AUTH"] = "npm-secret"
            os.environ["SERVICE_APIKEY"] = "compact-secret"
            os.environ["ORDINARY_SETTING"] = "kept"
            os.environ["PATH"] = "/tmp/attacker:/usr/bin"
            for name in (
                "NODE_OPTIONS", "LD_PRELOAD", "PYTHONPATH", "BASH_ENV",
                "COPILOT_ALLOW_ALL", "COPILOT_PROVIDER_BASE_URL",
                "HTTP_PROXY", "REQUESTS_CA_BUNDLE", "OTEL_EXPORTER_OTLP_ENDPOINT",
            ):
                os.environ[name] = "unsafe"
            child = child_process_env(profile="acp")
            self.assertNotIn("EVA_BRIDGE_TOKEN", child)
            self.assertNotIn("OPENAI_API_KEY", child)
            self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", child)
            self.assertNotIn("NPM_CONFIG__AUTH", child)
            self.assertNotIn("SERVICE_APIKEY", child)
            self.assertNotIn("ORDINARY_SETTING", child)
            self.assertNotIn("/tmp/attacker", child.get("PATH", ""))
            for name in (
                "NODE_OPTIONS", "LD_PRELOAD", "PYTHONPATH", "BASH_ENV",
                "COPILOT_ALLOW_ALL", "COPILOT_PROVIDER_BASE_URL",
                "HTTP_PROXY", "REQUESTS_CA_BUNDLE", "OTEL_EXPORTER_OTLP_ENDPOINT",
            ):
                self.assertNotIn(name, child)
            self.assertFalse(is_sensitive_env_name("PATH"))
            for name in ("GOOGLE_APPLICATION_CREDENTIALS", "NPM_CONFIG__AUTH", "SERVICE_APIKEY", "GITHUB_PAT"):
                self.assertTrue(is_sensitive_env_name(name), name)
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_mcp_persistence_uses_shared_secret_classifier(self):
        from bridge.utils import (
            _load_persisted_mcp_config, _persist_mcp_config,
            _sanitize_mcp_for_persist,
        )
        segmented = "sk-" + "proj-" + "A" * 24 + "-" + "B" * 12
        sanitized = _sanitize_mcp_for_persist({
            "server": {
                "command": "tool",
                "args": ["--label", segmented],
                "env": {
                    "PATH": "/usr/bin",
                    "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/credential.json",
                    "NPM_CONFIG__AUTH": "secret",
                    "SERVICE_APIKEY": "secret",
                    "GITHUBPAT": "compact-secret",
                    "ORDINARY_SETTING": segmented,
                    "_unknownFlag": segmented,
                    "_useGitHubPAT": True,
                },
            },
            "mistyped": {"command": "tool", "env": {"_useGitHubPAT": "true"}},
            segmented: {"command": "tool", "env": {}},
        })
        env = sanitized["server"]["env"]
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["_useGitHubPAT"], True)
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", env)
        self.assertNotIn("NPM_CONFIG__AUTH", env)
        self.assertNotIn("SERVICE_APIKEY", env)
        self.assertNotIn("GITHUBPAT", env)
        self.assertNotIn("ORDINARY_SETTING", env)
        self.assertNotIn("_unknownFlag", env)
        self.assertNotIn("_useGitHubPAT", sanitized["mistyped"]["env"])
        self.assertNotIn(segmented, json.dumps(sanitized, sort_keys=True))
        self.assertIn("[REDACTED]", json.dumps(sanitized, sort_keys=True))

        path = os.path.join(_TMP_HOME, "mcp-sanitized.json")
        with mock.patch("bridge.utils._RUNTIME_STATE_PATH", path):
            _persist_mcp_config({
                segmented: {
                    "command": "tool", "args": [segmented],
                    "env": {"MODE": segmented, "_useGitHubPAT": True},
                }
            })
        with open(path, encoding="utf-8") as handle:
            persisted = handle.read()
        self.assertNotIn(segmented, persisted)
        self.assertIn("[REDACTED]", persisted)

        stale_path = os.path.join(_TMP_HOME, "mcp-stale.json")
        with open(stale_path, "w", encoding="utf-8") as handle:
            json.dump({
                "server": {
                    "command": "tool",
                    "env": {"GITHUBPAT": segmented, "PATH": "/usr/bin"},
                }
            }, handle)
        with mock.patch("bridge.utils._MCP_CONFIG_CACHE_PATH", stale_path):
            loaded = _load_persisted_mcp_config()
        self.assertNotIn("server", loaded)
        with open(stale_path, encoding="utf-8") as handle:
            rewritten = handle.read()
        self.assertNotIn(segmented, rewritten)
        self.assertNotIn("GITHUBPAT", rewritten)

    def test_nonboolean_pat_flag_never_resolves_ambient_credential(self):
        from types import SimpleNamespace
        from bridge import core
        from bridge import state as st
        from bridge.core import BridgeHandler

        ambient = "github_" + "pat_" + "A" * 60
        saved = (
            st.egress_mode, st.acp_client, st.cognition_enabled,
            os.environ.get("GITHUB_PAT"),
        )
        captured = []

        class FakeACPClient:
            def __init__(self, **kwargs):
                captured.append(kwargs.get("mcp_config", {}))
                self.copilot_path = kwargs.get("copilot_path")
                self.cwd = kwargs.get("cwd")
                self.model = kwargs.get("model")
                self.mcp_config = kwargs.get("mcp_config", {})
            def start(self):
                return None
            def stop(self):
                return None

        try:
            os.environ["GITHUB_PAT"] = ambient
            st.egress_mode = "cloud"
            st.cognition_enabled = True
            st.acp_client = SimpleNamespace(
                copilot_path="copilot", cwd="/tmp", model=None,
                stop=lambda: None,
            )
            handler = object.__new__(BridgeHandler)
            handler.server = SimpleNamespace(server_port=8888)
            responses = []
            handler._read_json_body = lambda: ({
                "mcp_servers": {
                    "github-mcp-server": {
                        "command": "docker",
                        "args": [
                            "run", "-i", "--rm", "-e",
                            "GITHUB_PERSONAL_ACCESS_TOKEN",
                            "ghcr.io/github/github-mcp-server",
                        ],
                        "env": {"_useGitHubPAT": "false"},
                    }
                }
            }, "")
            handler._json_response = lambda status, data: responses.append((status, data))
            with mock.patch.object(core, "ACPClient", FakeACPClient), \
                    mock.patch.object(core, "_persist_mcp_config"), \
                    mock.patch.object(core, "_reset_acp_pool"):
                handler._mcp_configure()
            self.assertEqual(responses[-1][0], 403)
            self.assertEqual(captured, [])
        finally:
            st.egress_mode, st.acp_client, st.cognition_enabled = saved[:3]
            if saved[3] is None:
                os.environ.pop("GITHUB_PAT", None)
            else:
                os.environ["GITHUB_PAT"] = saved[3]

    def test_lmstudio_transport_rejects_public_and_redirects(self):
        from bridge import state as _st
        from bridge.lmstudio import post_json
        saved_mode = _st.egress_mode
        try:
            _st.egress_mode = "offline"
            with mock.patch("requests.Session") as session_factory:
                _, _, error = post_json("https://public.example/v1", {"model": "test"})
                self.assertTrue(error)
                session_factory.assert_not_called()

            class RedirectResponse:
                status_code = 302

            class FakeSession:
                def __init__(self):
                    self.trust_env = True
                    self.calls = []
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                def post(self, *args, **kwargs):
                    self.calls.append((args, kwargs, self.trust_env))
                    return RedirectResponse()

            fake = FakeSession()
            with mock.patch("requests.Session", return_value=fake):
                status, body, error = post_json("http://127.0.0.1:1234/v1", {"model": "test"})
                self.assertEqual(status, 302)
                self.assertIsNone(body)
                self.assertIn("redirect", error.lower())
                self.assertFalse(fake.calls[0][1]["allow_redirects"])
                self.assertFalse(fake.calls[0][2], "ambient proxy environment must be disabled")
        finally:
            _st.egress_mode = saved_mode

    def test_local_retrieval_without_acp_uses_validated_transport(self):
        from bridge import state as _st
        from bridge.core import BridgeHandler
        saved_client, saved_mode, saved_manager = _st.acp_client, _st.local_mode, _st.local_mcp_manager

        class FakeManager:
            alive = True
            def list_tools(self):
                return [{"type": "function", "function": {"name": "noop", "parameters": {}}}]

        try:
            _st.acp_client = None
            _st.local_mode = True
            _st.local_mcp_manager = FakeManager()
            with mock.patch("bridge.core._load_client_prefs", return_value={
                "lmstudio_base_url": "https://public.example/v1", "lmstudio_model": "test"
            }), mock.patch("requests.Session") as session_factory:
                self.assertEqual(BridgeHandler._retrieve_local_data("retrieve a fact"), ("", ""))
                session_factory.assert_not_called()
        finally:
            _st.acp_client, _st.local_mode, _st.local_mcp_manager = saved_client, saved_mode, saved_manager


# ═══════════════════════════════════════════════════════════════════
class TestRegistryWiring(unittest.TestCase):
    def test_bg_jobs_not_empty(self):
        from bridge.background import _BG_JOBS
        self.assertGreater(len(_BG_JOBS), 0)

    def test_core_references_same_registries(self):
        from bridge import core as _core
        from bridge import background as _bg
        self.assertIs(_core._BG_JOBS, _bg._BG_JOBS)
        self.assertIs(_core._BG_JOBS_ENABLED, _bg._BG_JOBS_ENABLED)


# ═══════════════════════════════════════════════════════════════════
class TestPackagePaths(unittest.TestCase):
    def test_eva_seed_path(self):
        from bridge import config as cfg
        self.assertTrue(os.path.isfile(os.path.join(cfg.TOOLS_DIR, "eva_seed.kql")))
        self.assertTrue(os.path.isfile(os.path.join(cfg.TOOLS_DIR, "kusto_mcp.py")))

    def test_project_mcp_is_not_auto_discovered(self):
        from bridge import config as cfg
        self.assertEqual(os.path.realpath(cfg.PROJECT_ROOT), os.path.realpath(PROJECT_ROOT))
        self.assertFalse(os.path.isfile(os.path.join(cfg.PROJECT_ROOT, "mcp.json")))
        with open(os.path.join(TOOLS_DIR, "bridge", "core.py")) as f:
            source = f.read()
        self.assertNotIn("Auto-discovered MCP config", source)

    def test_packaged_runtime_helpers_are_declared(self):
        package_path = os.path.join(PROJECT_ROOT, "standalone", "package.json")
        with open(package_path) as f:
            package = json.load(f)
        filters = package["build"]["extraResources"][0]["filter"]
        for required in ("tools/web_search_mcp.py", "tools/sqlite_mcp.py", "tools/eva_seed.kql"):
            self.assertIn(required, filters)


# ═══════════════════════════════════════════════════════════════════
class TestUndefinedNames(unittest.TestCase):
    def test_all_bridge_files_compile(self):
        bridge_dir = os.path.join(TOOLS_DIR, "bridge")
        for name in sorted(os.listdir(bridge_dir)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(bridge_dir, name)
            with self.subTest(file=name):
                with open(path) as f:
                    compile(f.read(), path, "exec")

    def test_production_subprocess_calls_supply_environment(self):
        paths = [
            os.path.join(TOOLS_DIR, "bridge", "alerts.py"),
            os.path.join(TOOLS_DIR, "bridge", "core.py"),
            os.path.join(TOOLS_DIR, "bridge", "acp_client.py"),
            os.path.join(TOOLS_DIR, "bridge", "local_mcp.py"),
            os.path.join(TOOLS_DIR, "browser_agent.py"),
            os.path.join(TOOLS_DIR, "desktop_agent.py"),
            os.path.join(TOOLS_DIR, "camera_sense.py"),
        ]
        missing = []
        for path in paths:
            with open(path) as f:
                tree = ast.parse(f.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if not isinstance(node.func.value, ast.Name) or node.func.value.id != "subprocess":
                    continue
                if node.func.attr not in ("Popen", "run", "check_output"):
                    continue
                if not any(keyword.arg == "env" for keyword in node.keywords):
                    missing.append(f"{os.path.basename(path)}:{node.lineno}:{node.func.attr}")
        self.assertEqual(missing, [])


# ═══════════════════════════════════════════════════════════════════
class TestElectronSourceAssertions(unittest.TestCase):
    """Token must be absent from preload exposure and renderer argv."""

    def test_bridge_health_is_ignored_until_spawned_child_proves_bind(self):
        readiness_path = os.path.join(
            PROJECT_ROOT, "standalone", "bridge-readiness.js"
        )
        script = r"""
const {EventEmitter}=require('events');
const readiness=require(process.argv[1]);
const child=new EventEmitter();
child.stdout=new EventEmitter();
child.pid=4242;
const expected={
  token:'synthetic-parent-bearer',nonce:'N'.repeat(43),pid:child.pid,
  host:'127.0.0.1',port:43123
};
const tracker=readiness.createChildProofTracker(child,expected);
let healthCalls=0,windowCreated=false,headerInjectionInstalled=false;
(async()=>{
  const boot=readiness.waitForVerifiedBridge({
    childProcess:child,baseUrl:'http://127.0.0.1:43123',timeoutMs:2000,
    pollIntervalMs:1,
    requestHealth:async()=>{healthCalls+=1;return {status:'ok'}}
  }).then(()=>{windowCreated=true;headerInjectionInstalled=true});
  await new Promise(resolve=>setImmediate(resolve));
  const before={healthCalls,windowCreated,headerInjectionInstalled};
  const proof={
    version:1,pid:expected.pid,host:expected.host,port:expected.port,
    proof:readiness.computeBridgeBindProof(
      expected.token,expected.nonce,expected.pid,expected.host,expected.port)
  };
  const line=readiness.READY_PREFIX+JSON.stringify(proof)+'\n';
  tracker.push(line.slice(0,17));
  tracker.push(line.slice(17));
  await boot;
  console.log(JSON.stringify({before,after:{healthCalls,windowCreated,headerInjectionInstalled}}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script, readiness_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertEqual(data["before"], {
            "healthCalls": 0,
            "windowCreated": False,
            "headerInjectionInstalled": False,
        })
        self.assertEqual(data["after"], {
            "healthCalls": 1,
            "windowCreated": True,
            "headerInjectionInstalled": True,
        })

    def test_python_bind_proof_matches_electron_verifier(self):
        from bridge import state as st
        from bridge.core import _emit_bridge_bind_proof
        import contextlib
        from types import SimpleNamespace

        readiness_path = os.path.join(
            PROJECT_ROOT, "standalone", "bridge-readiness.js"
        )
        token = "synthetic-parent-bearer"
        nonce = "R" * 43
        saved_token = st.bridge_auth_token
        try:
            st.bridge_auth_token = token
            capture = io.StringIO()
            server = SimpleNamespace(server_address=("127.0.0.1", 43124))
            with contextlib.redirect_stdout(capture):
                _emit_bridge_bind_proof(server, nonce)
            line = capture.getvalue().strip()
        finally:
            st.bridge_auth_token = saved_token

        script = (
            "const r=require(process.argv[1]);"
            "const expected=JSON.parse(process.argv[2]);"
            "const proof=r.verifyBridgeBindProofLine(process.argv[3],expected);"
            "console.log(JSON.stringify(proof));"
        )
        expected = {
            "token": token,
            "nonce": nonce,
            "pid": os.getpid(),
            "host": "127.0.0.1",
            "port": 43124,
        }
        result = subprocess.run(
            ["node", "-e", script, readiness_path, json.dumps(expected), line],
            capture_output=True, text=True, check=True,
        )
        proof = json.loads(result.stdout)
        self.assertEqual(proof["pid"], os.getpid())
        self.assertEqual(proof["host"], "127.0.0.1")
        self.assertEqual(proof["port"], 43124)

    def test_bind_proof_rejects_digest_replay_identity_and_duplicates(self):
        readiness_path = os.path.join(
            PROJECT_ROOT, "standalone", "bridge-readiness.js"
        )
        script = r"""
const {EventEmitter}=require('events');
const readiness=require(process.argv[1]);
const expected={token:'synthetic-bearer',nonce:'A'.repeat(43),pid:4444,
    host:'127.0.0.1',port:43125};
function line(fields){return readiness.READY_PREFIX+JSON.stringify(Object.assign({
    version:1,pid:expected.pid,host:expected.host,port:expected.port,
    proof:readiness.computeBridgeBindProof(expected.token,expected.nonce,
        expected.pid,expected.host,expected.port)},fields||{}));}
const failures={};
for(const [name,value] of Object.entries({
    badDigest:line({proof:'0'.repeat(64)}),
    oldNonce:readiness.READY_PREFIX+JSON.stringify({version:1,pid:expected.pid,
        host:expected.host,port:expected.port,proof:readiness.computeBridgeBindProof(
            expected.token,'B'.repeat(43),expected.pid,expected.host,expected.port)}),
    wrongPid:line({pid:9999}),wrongPort:line({port:43126}),wrongHost:line({host:'localhost'})
})){
    try{readiness.verifyBridgeBindProofLine(value,expected);failures[name]=false}
    catch(_){failures[name]=true}
}
const child=new EventEmitter();let proofs=0,errors=0;
child.on('eva-bind-proof',()=>{proofs+=1});child.on('eva-bind-proof-error',()=>{errors+=1});
const tracker=readiness.createChildProofTracker(child,expected);
tracker.push(line()+'\n'+line()+'\n');
console.log(JSON.stringify({failures,proofs,errors}));
"""
        result = subprocess.run(
            ["node", "-e", script, readiness_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertTrue(all(data["failures"].values()))
        self.assertEqual(data["proofs"], 1)
        self.assertEqual(data["errors"], 0)

    def test_child_exit_before_or_during_health_never_activates(self):
        readiness_path = os.path.join(
            PROJECT_ROOT, "standalone", "bridge-readiness.js"
        )
        script = r"""
const {EventEmitter}=require('events');
const readiness=require(process.argv[1]);
function makeChild(pid){const child=new EventEmitter();child.pid=pid;return child}
const expected={token:'synthetic-bearer',nonce:'C'.repeat(43),pid:4555,
    host:'127.0.0.1',port:43127};
function proofLine(){return readiness.READY_PREFIX+JSON.stringify({version:1,
    pid:expected.pid,host:expected.host,port:expected.port,
    proof:readiness.computeBridgeBindProof(expected.token,expected.nonce,
        expected.pid,expected.host,expected.port)})+'\n'}
(async()=>{
    const before=makeChild(expected.pid);let beforeHealth=0;
    const beforeWait=readiness.waitForVerifiedBridge({childProcess:before,
        baseUrl:'http://127.0.0.1:43127',timeoutMs:500,pollIntervalMs:1,
        requestHealth:async()=>{beforeHealth+=1;return {status:'ok'}}
    }).then(()=>false,()=>true);
    before.emit('exit',1,null);

    const during=makeChild(expected.pid);let releaseHealth,duringActivated=false;
    const tracker=readiness.createChildProofTracker(during,expected);
    const duringWait=readiness.waitForVerifiedBridge({childProcess:during,
        baseUrl:'http://127.0.0.1:43127',timeoutMs:500,pollIntervalMs:1,
        requestHealth:()=>new Promise(resolve=>{releaseHealth=resolve})
    }).then(()=>{duringActivated=true;return false},()=>true);
    tracker.push(proofLine());
    await new Promise(resolve=>setImmediate(resolve));
    during.emit('exit',1,null);
    releaseHealth({status:'ok'});
    console.log(JSON.stringify({beforeRejected:await beforeWait,beforeHealth,
        duringRejected:await duringWait,duringActivated}));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script, readiness_path],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertTrue(data["beforeRejected"])
        self.assertEqual(data["beforeHealth"], 0)
        self.assertTrue(data["duringRejected"])
        self.assertFalse(data["duringActivated"])

    def test_bind_proof_emitted_only_after_server_construction(self):
        core_path = os.path.join(TOOLS_DIR, "bridge", "core.py")
        main_path = os.path.join(PROJECT_ROOT, "standalone", "main.js")
        package_path = os.path.join(PROJECT_ROOT, "standalone", "package.json")
        with open(core_path) as handle:
            core_source = handle.read()
        with open(main_path) as handle:
            main_source = handle.read()
        with open(package_path) as handle:
            package = json.load(handle)
        self.assertLess(
            core_source.index("server = ThreadingHTTPServer"),
            core_source.index("_emit_bridge_bind_proof(server, ready_nonce)"),
        )
        self.assertIn("waitForVerifiedBridge", main_source)
        self.assertIn("EVA_BRIDGE_READY_NONCE", main_source)
        self.assertIn("bridge-readiness.js", package["build"]["files"])

    def test_preload_no_bridge_token(self):
        preload_path = os.path.join(PROJECT_ROOT, "standalone", "preload.js")
        if not os.path.isfile(preload_path):
            self.skipTest("preload.js not found")
        with open(preload_path) as f:
            content = f.read()
        self.assertNotIn("bridgeToken", content)
        self.assertNotIn("bridge-token", content)

    def test_main_no_token_in_additional_args(self):
        main_path = os.path.join(PROJECT_ROOT, "standalone", "main.js")
        if not os.path.isfile(main_path):
            self.skipTest("main.js not found")
        with open(main_path) as f:
            content = f.read()
        self.assertNotIn("eva-bridge-token", content)

    def test_main_has_webRequest_injection(self):
        main_path = os.path.join(PROJECT_ROOT, "standalone", "main.js")
        if not os.path.isfile(main_path):
            self.skipTest("main.js not found")
        with open(main_path) as f:
            content = f.read()
        self.assertIn("onBeforeSendHeaders", content)
        self.assertIn("/v1/*", content)
        self.assertIn("details.webContentsId === mainWindow.webContents.id", content)

    def test_main_enforces_egress_and_navigation_pinning(self):
        main_path = os.path.join(PROJECT_ROOT, "standalone", "main.js")
        with open(main_path) as f:
            content = f.read()
        self.assertIn("requestAllowedByEgress", content)
        self.assertIn("onBeforeRequest", content)
        self.assertIn("trustedDocumentUrl", content)
        self.assertIn("will-redirect", content)

    def test_electron_egress_policy_matrix(self):
        policy_path = os.path.join(PROJECT_ROOT, "standalone", "security-policy.js")
        script = (
            "const p=require(process.argv[1]);"
            "const lan=[192,168,1,2].join('.');"
            "const lan10=[10,0,0,2].join('.');"
            "const out={"
            "offlineLoop:p.requestAllowedByEgress('http://127.0.0.1:1234/v1','offline'),"
            "offlineLan:p.requestAllowedByEgress('http://'+lan+':1234/v1','offline'),"
            "offlineCloud:p.requestAllowedByEgress('https://api.openai.com/v1','offline'),"
            "lanPrivate:p.requestAllowedByEgress('http://'+lan10+':1234/v1','local-network'),"
            "lanCloud:p.requestAllowedByEgress('https://api.openai.com/v1','local-network'),"
            "cloudPublic:p.requestAllowedByEgress('https://api.openai.com/v1','cloud')};"
            "try{p.normalizeEgressMode('invalid');out.invalid=false}catch(e){out.invalid=true}"
            "console.log(JSON.stringify(out));"
        )
        result = subprocess.run(
            ["node", "-e", script, policy_path], capture_output=True, text=True, check=True
        )
        values = json.loads(result.stdout)
        self.assertEqual(values, {
            "offlineLoop": True, "offlineLan": False, "offlineCloud": False,
            "lanPrivate": True, "lanCloud": False, "cloudPublic": True,
            "invalid": True,
        })

    def test_renderer_no_bridge_token(self):
        opts_path = os.path.join(PROJECT_ROOT, "core", "js", "options.js")
        if not os.path.isfile(opts_path):
            self.skipTest("options.js not found")
        with open(opts_path) as f:
            content = f.read()
        self.assertNotIn("bridgeToken", content)
        self.assertNotIn("eva_bridge_token", content)
        self.assertNotIn("installBridgeFetchWrapper", content)


# ═══════════════════════════════════════════════════════════════════
class TestLegacyHTMLNeverAssigned(unittest.TestCase):
    def test_sessions_no_legacy_innerhtml(self):
        path = os.path.join(PROJECT_ROOT, "core", "js", "sessions.js")
        if not os.path.isfile(path):
            self.skipTest("sessions.js not found")
        with open(path) as f:
            content = f.read()
        lines = content.split("\n")
        in_legacy = False
        for i, line in enumerate(lines):
            if "data._htmlSnapshot" in line and "else if" in line:
                in_legacy = True
                continue
            if in_legacy:
                if "innerHTML" in line:
                    self.fail(f"Legacy HTML snapshot assigns innerHTML at line {i+1}")
                if line.strip().startswith("}"):
                    break

    def test_structured_snapshot_uses_provider_message_stores(self):
        path = os.path.join(PROJECT_ROOT, "core", "js", "sessions.js")
        with open(path) as f:
            content = f.read()
        self.assertIn("_structuredMessagesFromStores", content)
        self.assertIn("geminiMessages", content)
        self.assertIn("message.parts", content)
        self.assertNotIn("querySelectorAll('.chat-bubble')", content)

    def test_applying_proposals_remain_recoverable(self):
        path = os.path.join(PROJECT_ROOT, "core", "js", "options.js")
        with open(path) as f:
            content = f.read()
        self.assertIn("/v1/background/proposals?status=all", content)
        self.assertIn("Retry Apply", content)
        self.assertIn("s === 'pending' || s === 'applying'", content)

    def test_gemini_history_persisted_before_render_and_bootstrap_filtered(self):
        gemini_path = os.path.join(PROJECT_ROOT, "core", "js", "gl-google.js")
        sessions_path = os.path.join(PROJECT_ROOT, "core", "js", "sessions.js")
        with open(gemini_path) as f:
            gemini = f.read()
        with open(sessions_path) as f:
            sessions = f.read()
        persist_at = gemini.index('localStorage.setItem("geminiMessages"')
        render_at = gemini.index("await renderEvaResponse(")
        finalize_at = gemini.index("await finalizeDirectProviderTurn(")
        self.assertLess(finalize_at, render_at)
        self.assertLess(render_at, persist_at)
        self.assertIn("parts: [{ text: mainResponse }]", gemini)
        self.assertIn("if (typeof saveCurrentSession === 'function') saveCurrentSession();", gemini)
        self.assertIn("keys[i] === 'geminiMessages' && index < 2", sessions)

    def test_exact_o1_uses_o1_family_payload_contract(self):
        path = os.path.join(PROJECT_ROOT, "core", "js", "gpt-core.js")
        script = r"""
const fs=require('fs'),vm=require('vm');
const store={messages:JSON.stringify([
    {role:'developer',content:'developer instructions'},
    {role:'user',content:'prior user'}
])};
global.localStorage={
    getItem:k=>Object.prototype.hasOwnProperty.call(store,k)?store[k]:null,
    setItem:(k,v)=>{store[k]=String(v)}
};
global.txtMsg={innerHTML:'hello there',focus:()=>{}};
global.txtOutput={innerHTML:'',innerText:'',scrollTop:0,scrollHeight:0};
global.document={getElementById:id=>id==='txtOutput'?txtOutput:null};
global.imgSrcGlobal='';
global.selModel={value:'o1'};
global.OPENAI_API_KEY='synthetic';
global.lastResponse='';global.masterOutput='';global.retryCount=0;global.maxRetries=1;
global.dateContents='';global.alert=()=>{};global.escapeHtml=s=>String(s);
global.getSystemPrompt=()=>'';global.getModelMaxTokens=()=>1234;
global.getModelTemperature=()=>0.7;global.getACPBridgeUrl=()=> 'http://localhost:8888';
global.AbortSignal={timeout:()=>({})};
let sent=null;
global.fetch=async(url,options)=>{
    if(String(url).startsWith('https://api.openai.com')){
        sent=JSON.parse(options.body);return {ok:true,status:200,
            text:async()=>'{"usage":{"completion_tokens":0,"total_tokens":0},"choices":[]}'};
    }
    return {ok:false,status:404};
};
global.isCurrentRequestEnvelope=()=>true;
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
(async()=>{
    await trboSend({session_id:'s',turn_id:'t',request_id:'r',correlation_id:'c'});
    await new Promise(resolve=>setTimeout(resolve,0));
    console.log(JSON.stringify(sent));
})().catch(error=>{console.error(error);process.exit(1)});
"""
        result = subprocess.run(
            ["node", "-e", script, path],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["model"], "o1")
        self.assertEqual(payload["temperature"], 1)
        self.assertFalse(any(
            message["role"] in ("developer", "system")
            for message in payload["messages"]
        ))


# ═══════════════════════════════════════════════════════════════════
class TestCSPDirectives(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(PROJECT_ROOT, "index.html")
        if not os.path.isfile(self.path):
            self.skipTest("index.html not found")
        with open(self.path) as f:
            self.content = f.read()
        m = re.search(r'http-equiv="Content-Security-Policy"\s+content="([^"]*)"', self.content)
        self.assertIsNotNone(m, "CSP meta tag not found")
        self.csp = m.group(1)

    def test_object_src_none(self):
        self.assertIn("object-src 'none'", self.csp)

    def test_frame_ancestors_none(self):
        self.assertIn("frame-ancestors 'none'", self.csp)

    def test_frame_src_none(self):
        self.assertIn("frame-src 'none'", self.csp)

    def test_form_action_none(self):
        self.assertIn("form-action 'none'", self.csp)

    def test_connect_openai(self):
        self.assertIn("https://api.openai.com", self.csp)

    def test_connect_gemini(self):
        self.assertIn("https://generativelanguage.googleapis.com", self.csp)

    def test_connect_github_models(self):
        self.assertIn("https://models.github.ai", self.csp)

    def test_google_font_hosts(self):
        self.assertIn("https://fonts.googleapis.com", self.csp)
        self.assertIn("https://fonts.gstatic.com", self.csp)

    def test_connect_loopback(self):
        self.assertIn("http://127.0.0.1:*", self.csp)

    def test_connect_aws(self):
        self.assertIn("amazonaws.com", self.csp)

    def test_connect_vision(self):
        self.assertIn("https://vision.googleapis.com", self.csp)


class TestRuntimePrerequisites(unittest.TestCase):
    def test_setup_script_enforces_documented_versions_and_auth(self):
        path = os.path.join(TOOLS_DIR, "acp_setup.sh")
        with open(path) as f:
            content = f.read()
        self.assertIn("major >= 24", content)
        self.assertIn("py_minor >= 12", content)
        self.assertIn("copilot auth login", content)
        self.assertIn('"arm64"', content)
        self.assertNotIn("copilot login", content)
        self.assertGreaterEqual(content.count("check_auth ||"), 2)
        self.assertIn('EXPECTED_ROOT="$HOME/.eva"', content)
        self.assertIn("systemctl --user", content)
        self.assertIn("Do not run the ACP service installer as root", content)
        self.assertIn('Bridge URL: http://localhost:${BRIDGE_PORT}', content)
        self.assertNotIn("Bridge URL: http://$(hostname -I", content)
        with open(os.path.join(TOOLS_DIR, "acp_bridge.service")) as service_file:
            service = service_file.read()
        self.assertIn("WorkingDirectory=%h/.eva", service)
        self.assertIn("Environment=HOME=%h", service)
        self.assertNotIn("User=www-data", service)
        self.assertNotIn("/opt/eva-agent", service)

    def test_bridge_header_qualifies_cloud_prerequisites(self):
        path = os.path.join(TOOLS_DIR, "bridge", "core.py")
        with open(path) as f:
            header = f.read(700)
        self.assertIn("Python 3.12+", header)
        self.assertIn("x86_64 or arm64/aarch64", header)
        self.assertIn("Cloud mode only: Node.js 24+", header)

    def test_auth_probe_requires_affirmative_success(self):
        setup_path = os.path.join(TOOLS_DIR, "acp_setup.sh")
        with tempfile.TemporaryDirectory() as td:
            stub = os.path.join(td, "copilot")
            with open(stub, "w") as f:
                f.write(
                    "#!/bin/sh\n"
                    "case \"$STUB_MODE\" in\n"
                    "  success) echo EVA_AUTH_OK; exit 0 ;;\n"
                    "  silent) exit 0 ;;\n"
                    "  *) echo unauthorized >&2; exit 1 ;;\n"
                    "esac\n"
                )
            os.chmod(stub, 0o755)
            base_env = dict(os.environ)
            base_env["PATH"] = td + os.pathsep + base_env.get("PATH", "")
            command = ['bash', '-c', 'source "$1"; check_auth', 'bash', setup_path]
            for mode, expected in (("success", 0), ("silent", 1), ("failure", 1)):
                env = dict(base_env, STUB_MODE=mode)
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, (mode, result.stdout, result.stderr))
                if mode != "success":
                    self.assertIn("copilot auth login", result.stdout + result.stderr)


# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    unittest.main(verbosity=2)
