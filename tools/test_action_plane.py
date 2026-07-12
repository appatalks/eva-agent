#!/usr/bin/env python3
"""Deterministic action-plane containment tests.

No GUI, providers, models, external network, Phase 3 reporting, candidate
execution, or user data. All files and run registries are temporary.
"""

import json
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

TEST_HOME = tempfile.mkdtemp(prefix="eva_action_plane_home_")
os.environ["HOME"] = TEST_HOME

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TOOLS_DIR)
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import browser_agent  # noqa: E402
import desktop_agent  # noqa: E402
import kusto_mcp  # noqa: E402
import web_search_mcp  # noqa: E402
from bridge import action_runs  # noqa: E402
from bridge import acp_client  # noqa: E402
from bridge import config as bridge_config  # noqa: E402
from bridge import kusto as bridge_kusto  # noqa: E402
from bridge import local_mcp  # noqa: E402
from bridge import utils as bridge_utils  # noqa: E402
from bridge import public_egress_proxy  # noqa: E402
from bridge.action_runs import (  # noqa: E402
    ActionRunValidationError,
    ActionRunCancelled,
    admit_action_run,
    begin_effect,
    cancel_run,
    finish_effect,
    initialize_run,
    set_postcondition_baseline,
    open_gate,
    public_snapshot,
    resolve_gate,
    terminalize,
    typed_action_result,
    validate_autonomy,
    validate_launch_capability,
    validate_postcondition,
    validate_public_url,
)


class ActionRunContractTests(unittest.TestCase):
    def setUp(self):
        self.dns_patch = mock.patch.object(
            action_runs.socket, "getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        )
        self.dns_patch.start()

    def tearDown(self):
        self.dns_patch.stop()

    def _record(self, agent="browser", autonomy="pause", postcondition=None):
        import datetime

        rec = {
            "id": "0123456789abcdef",
            "goal": "bounded test goal",
            "status": "running",
            "step": 0,
            "result": None,
            "error": None,
            "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "finished": None,
            "pending_action": None,
            "pending_question": None,
            "last_screenshot": "/private/path/screenshot.png",
            "steps": [],
            "url": "https://example.com/path?secret=value#fragment",
            "_cancel": threading.Event(),
            "_gate": threading.Event(),
        }
        return initialize_run(rec, agent, autonomy, postcondition)

    def test_autonomy_is_strict_and_fail_closed(self):
        self.assertEqual(validate_autonomy("pause"), ("pause", "confirm_all"))
        self.assertEqual(
            validate_autonomy("confirm_all"), ("confirm_all", "confirm_all")
        )
        for value in ("auto", "unknown", "", None, 1):
            with self.subTest(value=value):
                with self.assertRaises(ActionRunValidationError):
                    validate_autonomy(value)

    def test_model_done_without_trusted_condition_is_indeterminate(self):
        rec = self._record()
        self.assertTrue(terminalize(
            rec, "indeterminate", "unverified_completion_claim", "model_done",
            model_summary="I did it",
        ))
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["outcome"]["state"], "indeterminate")

        forged = self._record(postcondition={
            "type": "browser.url_match", "origin": "https://example.com", "path": "/",
        })
        terminalize(forged, "succeeded", "claimed", "model_done")
        self.assertEqual(forged["outcome"]["state"], "indeterminate")
        self.assertEqual(forged["outcome"]["reason"], "success_proof_invalid")
        self.assertEqual(rec["outcome"]["postcondition"]["verdict"], "unknown")
        self.assertFalse(terminalize(rec, "succeeded", "late", "late"))
        self.assertEqual(rec["outcome"]["state"], "indeterminate")

    def test_cancellation_has_terminal_precedence(self):
        rec = self._record()
        self.assertEqual(cancel_run(rec), (True, "cancellation_accepted"))
        terminalize(rec, "succeeded", "postcondition_observed", "model_done")
        self.assertEqual(rec["status"], "cancelled")
        self.assertEqual(rec["outcome"]["state"], "aborted")
        self.assertEqual(rec["outcome"]["reason"], "user_cancelled")

    def test_approval_is_bound_one_use_and_wrong_gate_fails(self):
        rec = self._record()
        result = []

        def park():
            result.append(open_gate(rec, "approval", action={
                "action": "click", "x": 10, "y": 20,
            }))

        worker = threading.Thread(target=park)
        worker.start()
        for _ in range(100):
            request = public_snapshot(rec, ("status",)).get("approval_request")
            if request:
                break
            time.sleep(0.005)
        self.assertIsNotNone(request)
        self.assertEqual(request["kind"], "approval")
        self.assertEqual(len(request["gate_id"]), 32)
        self.assertIn(request["action_digest"], request["description"])
        self.assertIn(request["binding_digest"], request["description"])
        ok, reason = resolve_gate(
            rec, gate_id="f" * 32, kind="approval", decision="approve"
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "stale_gate")
        ok, reason = resolve_gate(
            rec, gate_id=request["gate_id"], kind="approval", decision="approve"
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "accepted")
        duplicate = resolve_gate(
            rec, gate_id=request["gate_id"], kind="approval", decision="approve"
        )
        self.assertEqual(duplicate, (False, "gate_already_consumed"))
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["state"], "approved")
        self.assertEqual(result[0]["action"], {"action": "click", "x": 10, "y": 20})
        self.assertEqual(len(result[0]["action_digest"]), 64)

    def test_approval_display_contains_complete_effect_and_binding(self):
        action = {
            "action": "navigate",
            "url": "https://example.com/private/path?complete=value#fragment",
        }
        binding = {
            "kind": "navigate", "target_valid": True,
            "destination_hash": "a" * 64,
        }
        description = action_runs.action_description(
            "browser", action, "target\u2028label", binding
        )
        effect_json = json.dumps(
            action, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        binding_json = json.dumps(
            binding, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        self.assertIn(effect_json, description)
        self.assertIn(binding_json, description)
        self.assertIn(action_runs.action_digest(action), description)
        self.assertIn(action_runs.sha256(binding), description)
        self.assertIn("target\\u2028label", description)
        self.assertNotIn("target\u2028label", description)

    def test_approved_action_is_frozen_and_binding_is_rechecked(self):
        rec = self._record()
        action = {"action": "press", "key": "a"}
        binding = {"window": "editor"}
        result = []
        worker = threading.Thread(
            target=lambda: result.append(
                open_gate(rec, "approval", action=action, binding=binding)
            )
        )
        worker.start()
        for _ in range(100):
            request = public_snapshot(rec, ("status",)).get("approval_request")
            if request:
                break
            time.sleep(0.005)
        action["key"] = "enter"
        self.assertEqual(resolve_gate(
            rec, gate_id=request["gate_id"], kind="approval", decision="approve"
        ), (True, "accepted"))
        worker.join(timeout=2)
        approved = result[0]
        self.assertEqual(approved["action"]["key"], "a")
        self.assertEqual(
            begin_effect(rec, approved, {"window": "changed"})[:2],
            (False, "approved_target_changed"),
        )
        leased, reason, frozen = begin_effect(rec, approved, binding)
        self.assertTrue(leased)
        self.assertEqual(reason, "executing")
        self.assertEqual(frozen["key"], "a")

    def test_inflight_cancel_is_pending_and_effect_is_recorded_first(self):
        rec = self._record()
        approval = {
            "state": "approved", "action": {"action": "click", "x": 1, "y": 1},
            "action_digest": action_runs.action_digest({"action": "click", "x": 1, "y": 1}),
            "binding_digest": action_runs.sha256({"target": "one"}),
        }
        leased, _reason, _action = begin_effect(rec, approval, {"target": "one"})
        self.assertTrue(leased)
        self.assertEqual(cancel_run(rec), (True, "cancellation_pending"))
        result = typed_action_result("executed", "clicked", "Click completed.")
        rec["steps"].append({"step": 0, "result": result})
        _finished, pending = finish_effect(rec, result)
        self.assertTrue(pending)
        terminalize(rec, "aborted", "user_cancelled", "cancel")
        self.assertEqual(len(rec["steps"]), 1)
        self.assertEqual(rec["outcome"]["state"], "aborted")

    def test_effect_and_startup_leases_refuse_hard_runtime_deadline(self):
        rec = self._record()
        rec["_started_monotonic"] = (
            time.monotonic() - action_runs.MAX_RUNTIME_SECONDS
        )
        action = {"action": "navigate", "url": "https://example.com"}
        binding = {"target": "browser"}
        approval = {
            "state": "approved", "action": action,
            "action_digest": action_runs.action_digest(action),
            "binding_digest": action_runs.sha256(binding),
        }
        self.assertEqual(
            begin_effect(rec, approval, binding),
            (False, "timed_out", None),
        )
        self.assertFalse(action_runs.begin_startup(rec))
        rec["_started_monotonic"] = (
            time.monotonic() - action_runs.MAX_RUNTIME_SECONDS + 0.1
        )
        self.assertTrue(action_runs.begin_startup(rec))
        action_runs.finish_startup(rec)

    def test_bounded_blocking_call_releases_owner_on_cancel(self):
        rec = self._record()
        blocker = threading.Event()
        result = []

        def own_call():
            try:
                action_runs.run_bounded_call(
                    rec, lambda: blocker.wait(60), timeout_seconds=60
                )
            except Exception as exc:
                result.append(type(exc))

        worker = threading.Thread(target=own_call)
        worker.start()
        time.sleep(0.02)
        cancel_run(rec)
        worker.join(timeout=1)
        blocker.set()
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [ActionRunCancelled])

    def test_cross_agent_admission_is_singleton_until_terminal(self):
        first = self._record("browser")
        second = self._record("desktop")
        original = action_runs._ACTIVE_RUN
        action_runs._ACTIVE_RUN = None
        try:
            admit_action_run(first)
            with self.assertRaises(ActionRunValidationError):
                admit_action_run(second)
            terminalize(first, "aborted", "user_cancelled", "cancel")
            admit_action_run(second)
            terminalize(second, "aborted", "user_cancelled", "cancel")
        finally:
            action_runs._ACTIVE_RUN = original

    def test_gate_expires_and_sensitive_input_is_rejected(self):
        rec = self._record()
        previous = action_runs.GATE_TTL_SECONDS
        action_runs.GATE_TTL_SECONDS = 0.02
        try:
            decision = open_gate(rec, "approval", action={"action": "click"})
        finally:
            action_runs.GATE_TTL_SECONDS = previous
        self.assertEqual(decision, {"state": "expired"})

        result = []
        worker = threading.Thread(
            target=lambda: result.append(open_gate(rec, "input", question="Answer?"))
        )
        worker.start()
        for _ in range(100):
            request = public_snapshot(rec, ("status",)).get("approval_request")
            if request:
                break
            time.sleep(0.005)
        token = "Bearer " + "syntheticmaterial" * 2
        rejected = resolve_gate(
            rec, gate_id=request["gate_id"], kind="input", text=token
        )
        self.assertEqual(rejected, (False, "sensitive_input_rejected"))
        accepted = resolve_gate(
            rec, gate_id=request["gate_id"], kind="input", text="bounded answer"
        )
        self.assertEqual(accepted, (True, "accepted"))
        worker.join(timeout=2)
        self.assertEqual(result, [{"state": "answered", "text": "bounded answer"}])

    def test_gate_wait_is_bounded_by_remaining_runtime(self):
        rec = self._record()
        rec["_started_monotonic"] = (
            time.monotonic() - action_runs.MAX_RUNTIME_SECONDS + 0.03
        )
        previous = action_runs.GATE_TTL_SECONDS
        action_runs.GATE_TTL_SECONDS = 1
        started = time.monotonic()
        try:
            decision = open_gate(rec, "approval", action={"action": "click"})
        finally:
            action_runs.GATE_TTL_SECONDS = previous
        self.assertEqual(decision, {"state": "expired"})
        self.assertLess(time.monotonic() - started, 0.2)

    def test_public_snapshot_omits_paths_actions_and_url_queries(self):
        rec = self._record()
        rec["pending_action"] = {"action": "type", "text": "private"}
        rec["approval_request"] = {
            "gate_id": "a" * 32, "kind": "approval",
            "action_digest": "b" * 64, "description": "Type redacted text",
            "expires_at": "2026-01-01T00:00:00Z",
        }
        snapshot = public_snapshot(rec, (
            "id", "goal", "status", "url", "pending_action", "last_screenshot",
        ))
        encoded = json.dumps(snapshot)
        self.assertNotIn("pending_action", snapshot)
        self.assertNotIn("last_screenshot", snapshot)
        self.assertNotIn("?", snapshot["url"])
        self.assertNotIn("private", encoded)
        self.assertIn("approval_request", snapshot)

    def test_public_url_policy_rejects_local_credentials_and_bad_schemes(self):
        loopback = ".".join(map(str, (127, 0, 0, 1)))
        private10 = ".".join(map(str, (10, 1, 2, 3)))
        self.assertEqual(
            validate_public_url("https://example.com/a?b=c"),
            "https://example.com/a?b=c",
        )
        for value in (
            "file:///tmp/x", "http://localhost/x", f"http://{loopback}/x",
            f"http://{private10}/x", "http://127.1/x", "http://0177.0.0.1/x",
            "http://0x7f.0.0.1/x", "https://user:pass@example.com/x",
            "http://intranet/x", "https://example.com:0/",
            "https://example.com:000/", "https://example.com:/",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ActionRunValidationError):
                    validate_public_url(value)

        with mock.patch.object(
            action_runs.socket, "getaddrinfo",
            return_value=[(2, 1, 6, "", (loopback, 443))],
        ):
            with self.assertRaises(ActionRunValidationError):
                validate_public_url("https://public-name.example/x")

    def test_electron_launch_capability_matches_python_and_is_one_use(self):
        module = os.path.join(PROJECT_ROOT, "standalone", "launch-capability.js")
        spec = {
            "goal": "bounded launch", "autonomy": "pause", "max_steps": 4,
            "use_director": False, "start_url": "https://example.com",
            "postcondition": None,
        }
        script = (
            "const c=require(process.argv[1]);"
            "const spec=JSON.parse(process.argv[2]);"
            "process.stdout.write(c.issue('synthetic-secret','browser',spec,2000000000));"
        )
        token = subprocess.run(
            ["node", "-e", script, module, json.dumps(spec)],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertTrue(validate_launch_capability(
            token, "browser", spec, "synthetic-secret", now=2000000000
        ))
        with self.assertRaises(ActionRunValidationError):
            validate_launch_capability(
                token, "browser", spec, "synthetic-secret", now=2000000000
            )
        boundary_token = subprocess.run(
            ["node", "-e", script, module, json.dumps(spec)],
            capture_output=True, text=True, check=True,
        ).stdout
        with self.assertRaises(ActionRunValidationError):
            validate_launch_capability(
                boundary_token, "browser", spec, "synthetic-secret",
                now=2000000060,
            )

    def test_native_authorization_displays_exact_signed_spec(self):
        module = os.path.join(PROJECT_ROOT, "standalone", "launch-capability.js")
        spec = {
            "goal": "line one\nline two", "vision_model": "vision-model",
            "use_director": False, "autonomy": "confirm_all", "max_steps": 7,
            "start_url": "https://example.com/start", "headless": True,
            "postcondition": {
                "type": "browser.url_match", "origin": "https://example.com",
                "path": "/done",
            },
        }
        script = (
            "const c=require(process.argv[1]);const raw=JSON.parse(process.argv[2]);"
            "const built=c.buildSpec('browser',raw);"
            "process.stdout.write(JSON.stringify({canonical:c.canonicalSpec('browser',built),"
            "display:c.displaySummary('browser',built)}));"
        )
        result = subprocess.run(
            ["node", "-e", script, module, json.dumps(spec)],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertIn(data["canonical"], data["display"])
        for field in (
            "goal", "vision_model", "use_director", "autonomy", "max_steps",
            "start_url", "headless", "postcondition",
        ):
            self.assertIn(f'"{field}"', data["canonical"])
        self.assertIn("line one\\nline two", data["display"])
        self.assertNotIn("line one\nline two", data["display"])
        reject_script = (
            "const c=require(process.argv[1]);"
            "try{c.buildSpec('desktop',{goal:'spoof\\u202e',max_steps:1});process.exit(1)}"
            "catch(_){process.exit(0)}"
        )
        self.assertEqual(
            subprocess.run(["node", "-e", reject_script, module]).returncode, 0
        )

    def test_launch_canonicalizers_match_defaults_origins_and_unicode(self):
        module = os.path.join(PROJECT_ROOT, "standalone", "launch-capability.js")
        cases = (
            ("browser", {"goal": "defaults"}),
            ("browser", {
                "goal": "url", "vision_model": "",
                "start_url": "https://EXAMPLE.com:443/a?x=1#fragment",
                "postcondition": {
                    "type": "browser.url_match",
                    "origin": "HTTPS://EXAMPLE.COM:443/", "path": "/done",
                },
            }),
            ("browser", {
                "goal": "cafe\u0301 😀", "vision_model": "vision-model",
                "use_director": False, "autonomy": "confirm_all",
                "max_steps": 60, "headless": True,
                "postcondition": {
                    "type": "browser.element_state", "selector": "main",
                    "state": "count_equals", "count": 1,
                },
            }),
            ("desktop", {
                "goal": "launch",
                "postcondition": {
                    "type": "desktop.process_spawned", "executable": "FireFox",
                    "state": "started",
                },
            }),
        )
        script = (
            "const c=require(process.argv[1]);const agent=process.argv[2];"
            "const raw=JSON.parse(process.argv[3]);"
            "process.stdout.write(JSON.stringify({spec:c.buildSpec(agent,raw),"
            "hash:c.specHash(agent,raw)}));"
        )
        for agent, raw in cases:
            with self.subTest(agent=agent, goal=raw["goal"]):
                result = subprocess.run(
                    ["node", "-e", script, module, agent,
                     json.dumps(raw, ensure_ascii=False)],
                    capture_output=True, text=True, check=True,
                )
                node = json.loads(result.stdout)
                self.assertEqual(node["spec"], action_runs.launch_spec(agent, raw))
                self.assertEqual(
                    node["hash"], action_runs.launch_spec_hash(agent, raw)
                )

        invalid_cases = (
            ("browser", {"goal": "x", "vision_model": "x" * 129}),
            ("browser", {"goal": "x", "max_steps": 1.5}),
            ("browser", {
                "goal": "x", "postcondition": {
                    "type": "browser.url_match",
                    "origin": "https://éxample.com", "path": "/",
                },
            }),
            ("browser", {
                "goal": "x", "postcondition": {
                    "type": "browser.url_match",
                    "origin": "https://example.com/path", "path": "/",
                },
            }),
            ("browser", {"goal": "x", "start_url": "https://example.com:0/"}),
            ("browser", {"goal": "x", "start_url": "https://example.com:000/"}),
            ("browser", {"goal": "x", "start_url": "https://example.com:/"}),
            ("browser", {"goal": "x", "start_url": "https://example.com:65536/"}),
        )
        rejection_script = (
            "const c=require(process.argv[1]);try{c.buildSpec(process.argv[2],"
            "JSON.parse(process.argv[3]));process.exit(1)}catch(_){process.exit(0)}"
        )
        for agent, raw in invalid_cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ActionRunValidationError):
                    action_runs.launch_spec(agent, raw)
                self.assertEqual(subprocess.run(
                    ["node", "-e", rejection_script, module, agent,
                     json.dumps(raw, ensure_ascii=False)]
                ).returncode, 0)
        for separator in ("\u2028", "\u2029"):
            for field_spec in (
                {"goal": "before" + separator + "after"},
                {"goal": "x", "vision_model": "v" + separator + "m"},
                {"goal": "x", "postcondition": {
                    "type": "browser.element_state",
                    "selector": "main" + separator + "aside", "state": "visible",
                }},
            ):
                with self.subTest(separator=hex(ord(separator)), raw=field_spec):
                    with self.assertRaises(ActionRunValidationError):
                        action_runs.launch_spec("browser", field_spec)
                    self.assertEqual(subprocess.run(
                        ["node", "-e", rejection_script, module, "browser",
                         json.dumps(field_spec, ensure_ascii=False)]
                    ).returncode, 0)

    def test_launch_nonce_cache_never_clears_live_entries(self):
        original = dict(action_runs._LAUNCH_NONCES)
        action_runs._LAUNCH_NONCES.clear()
        try:
            for index in range(4096):
                action_runs._LAUNCH_NONCES[f"{index:032x}"] = 2000000060
            module = os.path.join(PROJECT_ROOT, "standalone", "launch-capability.js")
            spec = {"goal": "capacity", "autonomy": "pause", "max_steps": 1}
            script = (
                "const c=require(process.argv[1]);"
                "process.stdout.write(c.issue('secret','desktop',JSON.parse(process.argv[2]),2000000000));"
            )
            token = subprocess.run(
                ["node", "-e", script, module, json.dumps(spec)],
                capture_output=True, text=True, check=True,
            ).stdout
            with self.assertRaises(ActionRunValidationError):
                validate_launch_capability(
                    token, "desktop", spec, "secret", now=2000000000
                )
            self.assertEqual(len(action_runs._LAUNCH_NONCES), 4096)
        finally:
            action_runs._LAUNCH_NONCES.clear()
            action_runs._LAUNCH_NONCES.update(original)

    def test_postcondition_schema_is_closed(self):
        browser = validate_postcondition("browser", {
            "type": "browser.url_match", "origin": "https://example.com", "path": "/done",
        })
        self.assertEqual(browser["origin"], "https://example.com")
        desktop = validate_postcondition("desktop", {
            "type": "desktop.process_spawned", "executable": "firefox", "state": "started",
        })
        self.assertEqual(desktop["executable"], "firefox")
        with self.assertRaises(ActionRunValidationError):
            validate_postcondition("browser", {"type": "browser.url_match", "origin": "https://example.com", "path": "/", "weaken": True})
        with self.assertRaises(ActionRunValidationError):
            validate_postcondition("desktop", {"type": "desktop.process_spawned", "executable": "sh", "state": "running"})


class FakeLocator:
    def __init__(self, count=1, visible=True, text=""):
        self._count = count
        self._visible = visible
        self._text = text
        self.first = self

    def count(self):
        return self._count

    def is_visible(self):
        return self._visible

    def inner_text(self, timeout=0):
        return self._text

    def nth(self, index):
        return self


class FakePage:
    def __init__(self, url="https://example.com/done", locator=None):
        self.url = url
        self._locator = locator or FakeLocator()

    def locator(self, selector):
        return self._locator


class AgentOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.dns_patch = mock.patch.object(
            action_runs.socket, "getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        )
        self.dns_patch.start()

    def tearDown(self):
        self.dns_patch.stop()

    def test_public_unicast_rejects_multicast_and_transition_addresses(self):
        import ipaddress

        for value in ("224.0.0.1", "ff02::1", "::ffff:93.184.216.34", "2002:5db8:d822::1"):
            self.assertFalse(action_runs.is_public_unicast(ipaddress.ip_address(value)))

    def test_plain_http_proxy_rewrites_host_header(self):
        self.assertEqual(
            public_egress_proxy._host_header("example.com", 80, 80), "example.com"
        )
        self.assertEqual(
            public_egress_proxy._host_header("example.com", 8080, 80),
            "example.com:8080",
        )
        headers, length = public_egress_proxy._sanitize_http_headers(
            [
                b"Host: ignored.example", b"Connection: X-Hop, keep-alive",
                b"X-Hop: remove-me", b"X-End: keep-me", b"Content-Length: 4",
            ],
            "example.com", 80,
        )
        rendered = b"\r\n".join(headers)
        self.assertEqual(length, 4)
        self.assertNotIn(b"X-Hop", rendered)
        self.assertNotIn(b"keep-alive", rendered.lower())
        self.assertIn(b"X-End: keep-me", rendered)
        self.assertIn(b"Host: example.com", rendered)
        for raw in (
            [b"Transfer-Encoding: chunked"],
            [b"Content-Length: 1", b"Content-Length: 1"],
            [b"Connection: Content-Length", b"Content-Length: 4"],
        ):
            with self.assertRaises(public_egress_proxy.PublicEgressProxyError):
                public_egress_proxy._sanitize_http_headers(raw, "example.com", 80)
        with self.assertRaises(public_egress_proxy.PublicEgressProxyError):
            public_egress_proxy._read_exact(mock.Mock(), b"extra", 1)
        for authority in (
            "example.com:0", "example.com:000", "example.com:",
            "example.com/path", "example.com?query", "example.com#fragment",
        ):
            with self.subTest(authority=authority), self.assertRaises(
                public_egress_proxy.PublicEgressProxyError
            ):
                public_egress_proxy._parse_authority(authority, 443)

    def test_dns_pinning_proxy_rejects_private_or_mixed_answers(self):
        loopback = ".".join(map(str, (127, 0, 0, 1)))
        private10 = ".".join(map(str, (10, 0, 0, 2)))
        private = [(2, 1, 6, "", (loopback, 443))]
        mixed = [
            (2, 1, 6, "", ("93.184.216.34", 443)),
            (2, 1, 6, "", (private10, 443)),
        ]
        for rows in (private, mixed):
            with self.subTest(rows=rows), \
                    mock.patch.object(public_egress_proxy.socket, "getaddrinfo", return_value=rows):
                with self.assertRaises(public_egress_proxy.PublicEgressProxyError):
                    public_egress_proxy._public_addresses("example.com", 443)

    def test_dns_pinning_proxy_connects_to_validated_address_not_hostname(self):
        rows = [(2, 1, 6, "", ("93.184.216.34", 443))]
        sentinel = object()
        with mock.patch.object(public_egress_proxy.socket, "getaddrinfo", return_value=rows), \
                mock.patch.object(public_egress_proxy.socket, "create_connection", return_value=sentinel) as connect:
            self.assertIs(
                public_egress_proxy._connect_pinned("example.com", 443), sentinel
            )
        connect.assert_called_once_with(("93.184.216.34", 443), timeout=30)

    def test_browser_model_claim_requires_postcondition(self):
        rec = browser_agent._new_run("test", "pause")
        browser_agent._finish_with_postcondition(rec, FakePage(), "model_done", "done")
        self.assertEqual(rec["outcome"]["state"], "indeterminate")

        spec = {
            "type": "browser.url_match", "origin": "https://example.com", "path": "/done",
        }
        verified = browser_agent._new_run("test", "pause", spec)
        set_postcondition_baseline(
            verified,
            browser_agent._verify_postcondition(
                verified, FakePage("https://example.com/before"), 0
            ),
        )
        action = {"action": "navigate", "url": "https://example.com/done"}
        binding = {"target": "browser"}
        approval = {
            "state": "approved", "action": action,
            "action_digest": action_runs.action_digest(action),
            "binding_digest": action_runs.sha256(binding),
        }
        self.assertTrue(begin_effect(verified, approval, binding)[0])
        finish_effect(verified, typed_action_result("executed", "navigated", "done"))
        browser_agent._finish_with_postcondition(
            verified, FakePage(), "model_done", "done"
        )
        self.assertEqual(verified["outcome"]["state"], "succeeded")
        self.assertEqual(
            verified["outcome"]["postcondition"]["verified_by"], "tool"
        )

    def test_unattested_baseline_and_count_overflow_cannot_succeed(self):
        spec = {
            "type": "browser.url_match", "origin": "https://example.com", "path": "/done",
        }
        rec = browser_agent._new_run("test", "pause", spec)
        rec["_baseline_postcondition"] = {"verdict": "not_observed"}
        action = {"action": "launch_app", "app": "firefox", "args": []}
        binding = {"target": "desktop"}
        approval = {
            "state": "approved", "action": action,
            "action_digest": action_runs.action_digest(action),
            "binding_digest": action_runs.sha256(binding),
        }
        self.assertTrue(begin_effect(rec, approval, binding)[0])
        finish_effect(rec, typed_action_result("executed", "app_started", "done"))
        browser_agent._finish_with_postcondition(rec, FakePage(), "model_done")
        self.assertEqual(rec["outcome"]["state"], "indeterminate")

        count_spec = {
            "type": "browser.element_state", "selector": ".item",
            "state": "count_equals", "count": 1000,
        }
        count_rec = browser_agent._new_run("test", "pause", count_spec)
        verdict = browser_agent._verify_postcondition(
            count_rec, FakePage(locator=FakeLocator(count=1001)), 0
        )
        self.assertEqual(verdict["verdict"], "not_observed")

        hidden_spec = {
            "type": "browser.element_state", "selector": ".item", "state": "hidden",
        }
        hidden_rec = browser_agent._new_run("test", "pause", hidden_spec)
        locator = mock.Mock()
        locator.count.return_value = 2
        first, second = mock.Mock(), mock.Mock()
        first.is_visible.return_value = False
        second.is_visible.return_value = True
        locator.nth.side_effect = [first, second]
        hidden_page = FakePage(locator=locator)
        hidden = browser_agent._verify_postcondition(hidden_rec, hidden_page, 0)
        self.assertEqual(hidden["verdict"], "not_observed")

    def _valid_browser_proof(self, effects=1):
        spec = {
            "type": "browser.url_match", "origin": "https://example.com",
            "path": "/done",
        }
        rec = browser_agent._new_run("test", "pause", spec)
        baseline = browser_agent._verify_postcondition(
            rec, FakePage("https://example.com/before"), 0
        )
        set_postcondition_baseline(rec, baseline)
        for index in range(effects):
            action = {"action": "navigate", "url": f"https://example.com/{index}"}
            binding = {"target": str(index)}
            approval = {
                "state": "approved", "action": action,
                "action_digest": action_runs.action_digest(action),
                "binding_digest": action_runs.sha256(binding),
            }
            self.assertTrue(begin_effect(rec, approval, binding)[0])
            finish_effect(rec, typed_action_result("executed", "navigated", "done"))
        final = browser_agent._verify_postcondition(rec, FakePage(), effects)
        return rec, final

    def test_success_proof_rejects_missing_fields_inconsistent_facts_and_overlap(self):
        for mutation in ("baseline_check", "final_kind", "receipt_field", "facts"):
            rec, final = self._valid_browser_proof()
            if mutation == "baseline_check":
                del rec["_baseline_postcondition"]["checks"][0]["check_id"]
            elif mutation == "final_kind":
                evidence = final["checks"][0]["evidence"][0]
                evidence["kind"] = "browser.element_state"
                logical = dict(evidence)
                logical.pop("digest")
                evidence["digest"] = action_runs.sha256(logical)
            elif mutation == "receipt_field":
                receipt = rec["_effect_receipts"][0]
                del receipt["binding_digest"]
                logical = dict(receipt)
                logical.pop("receipt_digest")
                receipt["receipt_digest"] = action_runs.sha256(logical)
            else:
                evidence = final["checks"][0]["evidence"][0]
                evidence["facts"]["path"] = "/wrong"
                logical = dict(evidence)
                logical.pop("digest")
                evidence["digest"] = action_runs.sha256(logical)
            terminalize(
                rec, "succeeded", "postcondition_observed", "model_done",
                postcondition=final,
            )
            self.assertEqual(rec["outcome"]["state"], "indeterminate", mutation)

        rec, final = self._valid_browser_proof(effects=2)
        first, second = rec["_effect_receipts"]
        second["started_at"] = first["started_at"]
        second["completed_at"] = first["completed_at"]
        logical = dict(second)
        logical.pop("receipt_digest")
        second["receipt_digest"] = action_runs.sha256(logical)
        terminalize(
            rec, "succeeded", "postcondition_observed", "model_done",
            postcondition=final,
        )
        self.assertEqual(rec["outcome"]["state"], "indeterminate")

        for target in ("baseline", "final"):
            rec, final = self._valid_browser_proof()
            selected = rec["_baseline_postcondition"] if target == "baseline" else final
            selected["unexpected"] = True
            terminalize(
                rec, "succeeded", "postcondition_observed", "model_done",
                postcondition=final,
            )
            self.assertEqual(rec["outcome"]["state"], "indeterminate", target)

    def test_late_model_done_cannot_beat_runtime_timeout(self):
        rec = browser_agent._new_run("test", "pause")
        rec["_started_monotonic"] = time.monotonic() - action_runs.MAX_RUNTIME_SECONDS - 1
        self.assertTrue(action_runs.runtime_expired(rec))
        browser_agent._finish_with_postcondition(rec, FakePage(), "timeout", "done")
        self.assertEqual(rec["outcome"]["state"], "aborted")
        self.assertEqual(rec["outcome"]["reason"], "timed_out")
        proven, final = self._valid_browser_proof()
        proven["_started_monotonic"] = (
            time.monotonic() - action_runs.MAX_RUNTIME_SECONDS - 1
        )
        terminalize(
            proven, "succeeded", "postcondition_observed", "model_done",
            postcondition=final,
        )
        self.assertEqual(proven["outcome"]["state"], "aborted")
        self.assertEqual(proven["outcome"]["reason"], "timed_out")
        reserved, reserved_final = self._valid_browser_proof()
        reserved["_started_monotonic"] = (
            time.monotonic() - action_runs.MAX_RUNTIME_SECONDS - 1
        )
        terminalize(
            reserved, "succeeded", "timed_out", "model_done",
            postcondition=reserved_final,
        )
        self.assertEqual(reserved["outcome"]["state"], "aborted")
        self.assertEqual(reserved["outcome"]["reason"], "timed_out")

    def test_step_limit_without_evidence_aborts(self):
        browser = browser_agent._new_run("test", "pause")
        browser_agent._finish_with_postcondition(browser, FakePage(), "step_limit")
        self.assertEqual(browser["outcome"]["state"], "aborted")
        self.assertEqual(browser["outcome"]["reason"], "budget_exhausted")
        desktop = desktop_agent._new_run("test", "pause")
        desktop_agent._finish_with_postcondition(desktop, "step_limit")
        self.assertEqual(desktop["outcome"]["state"], "aborted")

    def test_desktop_spawn_receipt_condition_is_tool_verified(self):
        spec = {
            "type": "desktop.process_spawned", "executable": "firefox", "state": "started",
        }
        rec = desktop_agent._new_run("test", "pause", spec)
        set_postcondition_baseline(rec, desktop_agent._verify_postcondition(rec, 0))
        process = mock.Mock(pid=4242)
        process.poll.return_value = None
        rec["_process_receipts"].append({
            "requested_app": "firefox", "binary": "/usr/bin/firefox",
            "pid": 4242, "started_monotonic": time.monotonic(),
            "process_start_ticks": 12345, "process_handle": process,
        })
        action = {"action": "launch_app", "app": "firefox", "args": []}
        binding = {"target": "desktop"}
        approval = {
            "state": "approved", "action": action,
            "action_digest": action_runs.action_digest(action),
            "binding_digest": action_runs.sha256(binding),
        }
        self.assertTrue(begin_effect(rec, approval, binding)[0])
        finish_effect(rec, typed_action_result("executed", "app_started", "done"))
        with mock.patch.object(
            desktop_agent, "_process_start_ticks", return_value=12345
        ), mock.patch.object(
            desktop_agent.os.path, "realpath",
            side_effect=lambda value: "/usr/bin/firefox"
            if value == "/proc/4242/exe" else value,
        ):
            desktop_agent._finish_with_postcondition(rec, "model_done", "done")
        self.assertEqual(rec["outcome"]["state"], "succeeded")

    def test_desktop_spawn_receipt_rejects_pid_reuse(self):
        spec = {
            "type": "desktop.process_spawned", "executable": "firefox",
            "state": "started",
        }
        rec = desktop_agent._new_run("test", "pause", spec)
        exited = mock.Mock(pid=4242)
        exited.poll.return_value = 0
        rec["_process_receipts"].append({
            "requested_app": "firefox", "binary": "/usr/bin/firefox",
            "pid": 4242, "started_monotonic": time.monotonic(),
            "process_start_ticks": 12345, "process_handle": exited,
        })
        with mock.patch.object(
            desktop_agent, "_process_start_ticks", return_value=12345
        ), mock.patch.object(
            desktop_agent.os.path, "realpath", return_value="/usr/bin/firefox"
        ):
            verdict = desktop_agent._verify_postcondition(rec, 1)
        self.assertEqual(verdict["verdict"], "not_observed")

    def test_browser_execute_returns_typed_rejection_for_private_navigation(self):
        loopback = ".".join(map(str, (127, 0, 0, 1)))
        page = mock.Mock()
        with self.assertRaises(ActionRunValidationError):
            browser_agent._execute(page, {
                "action": "navigate", "url": f"http://{loopback}/private",
            })
        with self.assertRaises(ActionRunValidationError):
            browser_agent._execute(page, {
                "action": "navigate", "url": "https://example.com:0/path",
            })
        redirect = mock.Mock()
        redirect.url = "https://example.com"
        redirect.goto.side_effect = lambda *args, **kwargs: setattr(
            redirect, "url", f"http://{loopback}/private"
        )
        with self.assertRaises(ActionRunValidationError):
            browser_agent._execute(redirect, {
                "action": "navigate", "url": "https://example.com",
            })

    def test_type_ref_fill_failure_never_uses_page_keyboard(self):
        handle = mock.Mock()
        handle.fill.side_effect = RuntimeError("not editable")
        page = mock.Mock()
        result = browser_agent._execute(
            page, {"action": "type_ref", "ref": "e1", "text": "bounded"},
            handle,
        )
        self.assertEqual(result["code"], "target_fill_failed")
        page.keyboard.type.assert_not_called()

    def test_coordinate_click_executes_exact_approved_point(self):
        page = mock.Mock()
        handle = mock.Mock()
        result = browser_agent._execute(
            page, {"action": "double_click", "x": 321, "y": 222}, handle
        )
        self.assertEqual(result["state"], "executed")
        page.mouse.click.assert_called_once_with(321, 222, click_count=2)
        handle.click.assert_not_called()
        handle.dblclick.assert_not_called()

    def test_desktop_launch_blocks_shells_and_all_arguments(self):
        with mock.patch.object(desktop_agent, "_resolve_app_binary", side_effect=lambda app: None if app == "sh" else "/usr/bin/firefox"):
            denied = desktop_agent._launch_app({
                "action": "launch_app", "app": "sh", "args": [],
            })
            self.assertEqual(denied["state"], "rejected")
            args = desktop_agent._launch_app({
                "action": "launch_app", "app": "firefox", "args": ["--private-window"],
            })
            self.assertEqual(args["code"], "app_arguments_forbidden")
        with mock.patch.object(desktop_agent.shutil, "which", return_value="/bin/sh"):
            self.assertIsNone(desktop_agent._resolve_app_binary("firefox"))
        with open(os.path.join(TOOLS_DIR, "desktop_agent.py")) as handle:
            desktop_source = handle.read()
        self.assertNotIn("subprocess.run([wmctrl", desktop_source)
        self.assertNotIn("subprocess.run([xdotool", desktop_source)

    def test_model_action_json_is_duplicate_safe_and_schema_closed(self):
        with self.assertRaises(ActionRunValidationError):
            browser_agent._parse_action(
                '{"action":"wait","action":"done","summary":"bad"}'
            )
        with self.assertRaises(ActionRunValidationError):
            browser_agent._parse_action(
                '{"action":"click","x":1,"y":2,"extra":true}'
            )
        with self.assertRaises(ActionRunValidationError):
            desktop_agent._parse_action(
                '{"action":"launch_app","app":"firefox","args":""}'
            )
        for payload in (
            'prefix {"action":"wait"}',
            '{"action":"wait"} suffix',
            '```json\n{"action":"wait"}\n```',
        ):
            with self.assertRaises(ActionRunValidationError):
                browser_agent._parse_action(payload)

    def test_dom_and_target_fingerprints_never_read_current_form_values(self):
        self.assertNotIn("el.value", browser_agent._DOM_SNAPSHOT_JS)
        self.assertNotIn("el.value", browser_agent._ELEMENT_FINGERPRINT_JS)
        sentinel = "SENTINEL_PASSWORD_VALUE"
        rendered = browser_agent._dom_list_text([{
            "ref": "e1", "tag": "password", "text": "Password",
            "value": sentinel, "onscreen": True,
        }])
        self.assertNotIn(sentinel, rendered)

    def test_type_ref_target_must_be_fillable_and_not_readonly(self):
        def binding(fingerprint):
            handle = mock.Mock()
            handle.evaluate.return_value = fingerprint
            locator = mock.Mock()
            locator.count.return_value = 1
            locator.element_handle.return_value = handle
            page = mock.Mock()
            page.url = "https://example.com/form"
            page.main_frame.url = page.url
            page.locator.return_value = locator
            return browser_agent._browser_action_target(
                page, {"action": "type_ref", "ref": "e1", "text": "x"}
            )[0]

        base = {
            "tag": "input", "role": "", "type": "text", "name": "field",
            "placeholder": "", "aria": "", "label": "Field", "text": "",
            "href": "", "target": "", "form_action": "", "form_method": "",
            "disabled": False, "readonly": False, "checked": False,
            "selected": False, "contenteditable": False, "rect": [0, 0, 10, 10],
        }
        self.assertTrue(binding(dict(base))["target_valid"])
        exact_label = "Bearer " + "A" * 32
        self.assertEqual(browser_agent._approval_label(exact_label), exact_label)
        for changes in (
            {"readonly": True}, {"disabled": True}, {"type": "checkbox"},
            {"type": "submit"}, {"type": "file"},
        ):
            candidate = dict(base)
            candidate.update(changes)
            self.assertFalse(binding(candidate)["target_valid"], changes)

    def test_launch_only_prompt_omits_pointer_actions_when_unattestable(self):
        prompt = desktop_agent._executor_system(100, 100)
        self.assertNotIn('"action":"click"', prompt)
        self.assertNotIn('"action":"scroll"', prompt)
        self.assertIn('"action":"launch_app"', prompt)
        with open(os.path.join(TOOLS_DIR, "desktop_agent.py")) as handle:
            source = handle.read()
        self.assertNotIn('if kind == "click"', source)
        self.assertNotIn('if kind == "scroll"', source)

    def test_restart_scavenger_removes_abandoned_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = os.path.join(tmp, "a" * 16)
            os.makedirs(run_dir)
            with open(os.path.join(run_dir, "step.png"), "wb") as handle:
                handle.write(b"pixels")
            previous = browser_agent._TRAJ_DIR
            browser_agent._TRAJ_DIR = tmp
            try:
                browser_agent._scavenge_artifacts(remove_all=True)
            finally:
                browser_agent._TRAJ_DIR = previous
            self.assertFalse(os.path.exists(run_dir))

    def test_start_run_rejects_auto_before_gui_or_provider_access(self):
        with self.assertRaises(ActionRunValidationError):
            browser_agent.start_run("test", "key", autonomy="auto")
        with self.assertRaises(ActionRunValidationError):
            desktop_agent.start_run("test", "key", autonomy="auto")

    def test_trajectory_records_hashes_not_raw_content(self):
        secret = "Bearer " + "syntheticmaterial" * 2
        with tempfile.TemporaryDirectory() as tmp:
            browser = browser_agent._new_run("private goal", "pause")
            browser["url"] = "https://example.com/path?private=yes"
            previous = browser_agent._TRAJ_DIR
            browser_agent._TRAJ_DIR = tmp
            try:
                browser_agent._record(
                    browser, 0, "/private/path/shot.png", "private subgoal",
                    secret, {"action": "type", "text": secret}, "private element",
                    typed_action_result("executed", "typed", "Typed redacted text."),
                )
                path = os.path.join(tmp, browser["id"], "trajectory.jsonl")
                with open(path) as handle:
                    payload = handle.read()
            finally:
                browser_agent._TRAJ_DIR = previous
        self.assertNotIn(secret, payload)
        self.assertNotIn("private subgoal", payload)
        self.assertNotIn("/private/path", payload)
        self.assertNotIn("?private=yes", payload)

    def test_public_terminal_snapshot_removes_private_evidence_facts(self):
        rec, final = self._valid_browser_proof()
        terminalize(
            rec, "succeeded", "postcondition_observed", "model_done",
            postcondition=final,
        )
        snapshot = public_snapshot(rec, ("id", "status"))
        encoded = json.dumps(snapshot)
        self.assertNotIn('"facts"', encoded)
        self.assertNotIn("/done", encoded)
        self.assertIn('"digest"', encoded)

    def test_public_browser_title_is_bounded_and_credential_redacted(self):
        rec = browser_agent._new_run("test", "pause")
        rec["title"] = "Bearer " + "syntheticmaterial" * 4 + ("x" * 500)
        snapshot = public_snapshot(rec, ("title",))
        self.assertNotIn("syntheticmaterial", snapshot["title"])
        self.assertLessEqual(len(snapshot["title"]), 160)

    def test_cancel_before_browser_worker_prevents_runtime_startup(self):
        rec = browser_agent._new_run("test", "pause")
        self.assertEqual(cancel_run(rec), (True, "cancellation_accepted"))
        fake_sync = mock.Mock(side_effect=AssertionError("runtime was entered"))
        fake_module = types.ModuleType("playwright.sync_api")
        fake_module.sync_playwright = fake_sync
        fake_package = types.ModuleType("playwright")
        fake_package.sync_api = fake_module
        with mock.patch.dict(sys.modules, {
            "playwright": fake_package, "playwright.sync_api": fake_module,
        }):
            browser_agent._worker(
                rec, "key", "model", None, 1,
                "https://example.com/start", True,
            )
        fake_sync.assert_not_called()
        self.assertEqual(rec["outcome"]["state"], "aborted")

    def test_cancel_during_browser_startup_is_pending_and_never_goes_public(self):
        entered_goto = threading.Event()
        release_goto = threading.Event()
        destinations = []

        class FakePageContext:
            def route(self, *_args, **_kwargs):
                return None

        class StartupPage:
            url = "about:blank"
            context = FakePageContext()

            def set_default_timeout(self, _value):
                return None

            def set_default_navigation_timeout(self, _value):
                return None

            def goto(self, destination, **_kwargs):
                destinations.append(destination)
                entered_goto.set()
                release_goto.wait(2)
                self.url = destination

        page = StartupPage()

        class FakeContext:
            pages = [page]

            def close(self):
                return None

        class FakeChromium:
            def launch_persistent_context(self, *_args, **_kwargs):
                return FakeContext()

        class FakePlaywright:
            chromium = FakeChromium()

        class FakeManager:
            def __enter__(self):
                return FakePlaywright()

            def __exit__(self, *_args):
                return None

        fake_module = types.ModuleType("playwright.sync_api")
        fake_module.sync_playwright = lambda: FakeManager()
        fake_package = types.ModuleType("playwright")
        fake_package.sync_api = fake_module
        rec = browser_agent._new_run("test", "pause")
        worker = threading.Thread(target=browser_agent._worker, args=(
            rec, "key", "model", None, 1,
            "https://example.com/start", True,
        ))
        with mock.patch.dict(sys.modules, {
            "playwright": fake_package, "playwright.sync_api": fake_module,
        }), mock.patch.object(
            browser_agent.PublicEgressProxy, "start", return_value=mock.Mock(
                url="http://127.0.0.1:1", close=lambda: None
            )
        ):
            worker.start()
            self.assertTrue(entered_goto.wait(1))
            self.assertEqual(cancel_run(rec), (True, "cancellation_pending"))
            release_goto.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(destinations, ["about:blank"])
        self.assertEqual(rec["outcome"]["state"], "aborted")

    def test_agents_have_no_phase3_reporting_path(self):
        for path in (
            os.path.join(TOOLS_DIR, "browser_agent.py"),
            os.path.join(TOOLS_DIR, "desktop_agent.py"),
            os.path.join(TOOLS_DIR, "bridge", "action_runs.py"),
        ):
            with open(path) as handle:
                source = handle.read()
            self.assertNotIn("phase3_learning", source)
            self.assertNotIn("/v1/learning/", source)


class EndpointContractTests(unittest.TestCase):
    @staticmethod
    def _handler(body):
        class Handler:
            def __init__(self):
                self.body = body
                self.responses = []

            def _read_json_body(self):
                return self.body, ""

            def _json_response(self, status, payload):
                self.responses.append((status, payload))

        return Handler()

    def test_launch_requires_explicit_authorization_before_agent_access(self):
        from bridge import core

        handler = self._handler({"goal": "test"})
        with mock.patch.object(core._st, "egress_mode", "cloud"), \
                mock.patch.object(core, "_BROWSER_AGENT") as agent, \
                mock.patch.object(core, "_set_openai_key_from") as key_setup:
            agent.playwright_available.return_value = (True, "ok")
            core.BridgeHandler._browser_run(handler)
        self.assertEqual(handler.responses[0][0], 403)
        key_setup.assert_not_called()
        agent.start_run.assert_not_called()

    def test_json_parser_rejects_duplicate_members(self):
        from bridge import core

        class Headers:
            def get(self, name, default=None):
                return str(len(payload)) if name == "Content-Length" else default

        payload = b'{"decision":"deny","decision":"approve"}'
        handler = type("Handler", (), {})()
        handler.headers = Headers()
        handler.rfile = io.BytesIO(payload)
        data, error = core.BridgeHandler._read_json_body(handler)
        self.assertIsNone(data)
        self.assertEqual(error, "Invalid JSON")

        for raw, declared, expected in (
            (b"{}", 9, "Request body ended before Content-Length"),
            (b"\xff", 1, "Request body must be UTF-8 JSON"),
            (b'{"value":NaN}', 13, "Invalid JSON"),
        ):
            target = type("Handler", (), {})()
            target.headers = type("Headers", (), {
                "get": lambda self, name, default=None: (
                    str(declared) if name == "Content-Length" else default
                )
            })()
            target.rfile = io.BytesIO(raw)
            parsed, failure = core.BridgeHandler._read_json_body(target)
            self.assertIsNone(parsed)
            self.assertEqual(failure, expected)

    def test_http_framing_rejects_duplicate_or_signed_lengths(self):
        from bridge import core

        class Headers:
            def __init__(self, values):
                self.values = values

            def get_all(self, name):
                return self.values.get(name, [])

            def get(self, name, default=None):
                rows = self.values.get(name, [])
                return rows[0] if rows else default

        for values, expected in (
            ({
                "Content-Type": ["application/json"],
                "Content-Length": ["2", "99"],
            }, 400),
            ({
                "Content-Type": ["application/json"],
                "Content-Length": ["+2"],
            }, 400),
            ({
                "Content-Type": ["application/json"],
                "Content-Length": ["9" * 5000],
            }, 413),
            ({
                "Content-Type": ["application/json", "application/json"],
                "Content-Length": ["2"],
            }, 415),
            ({
                "Content-Type": ["application/json"],
                "Content-Length": ["2"],
                "Transfer-Encoding": ["chunked"],
            }, 400),
        ):
            handler = type("Handler", (), {})()
            handler.headers = Headers(values)
            handler.responses = []
            handler._send_simple_error = lambda status, message: handler.responses.append(
                (status, message)
            )
            handler._header_values = lambda name: core.BridgeHandler._header_values(
                handler, name
            )
            handler._validated_content_lengths = (
                lambda required: core.BridgeHandler._validated_content_lengths(
                    handler, required=required
                )
            )
            ok = core.BridgeHandler._enforce_json_content(handler)
            self.assertFalse(ok)
            self.assertEqual(handler.responses[0][0], expected)

    def test_legacy_or_malformed_approval_body_fails_closed(self):
        from bridge import core

        for body in (
            {"run_id": "0" * 16, "approve": True},
            {"run_id": "0" * 16, "gate_id": "1" * 32, "kind": "approval"},
            {"run_id": "0" * 16, "gate_id": "1" * 32, "kind": "approval", "decision": "yes"},
        ):
            handler = self._handler(body)
            agent = mock.Mock()
            if "decision" in body:
                agent.resolve.return_value = (False, "invalid_decision")
            core.BridgeHandler._agent_gate_resolve(handler, agent, body)
            self.assertEqual(handler.responses[0][0], 400)

    def test_frontend_consumes_typed_outcome_and_bound_gate(self):
        browser_js = os.path.join(PROJECT_ROOT, "core", "js", "browser-agent.js")
        options_js = os.path.join(PROJECT_ROOT, "core", "js", "options.js")
        with open(browser_js) as handle:
            browser_source = handle.read()
        with open(options_js) as handle:
            options_source = handle.read()
        self.assertIn("authorizeAgentLaunch", browser_source)
        self.assertIn("launch_capability", browser_source)
        self.assertIn("gate_id: gate.gate_id", browser_source)
        self.assertIn("outcome && outcome.state", options_source)
        self.assertIn("state === 'succeeded'", options_source)
        self.assertNotIn("state === 'done' && goal", options_source)
        self.assertNotIn("autoLearnSkill(recent, goal", options_source)

    def test_frontend_success_validator_is_exact_and_shared(self):
        validator = os.path.join(PROJECT_ROOT, "core", "js", "action-outcomes.js")
        script = r"""
const v=require(process.argv[1]);
const h='a'.repeat(64);
const status={contract_version:'eva.action-run/1',status:'done',outcome:{
    state:'succeeded',reason:'postcondition_observed',
    termination:{cause:'model_done',step:1},model_claim:{summary_hash:h},
    postcondition:{verdict:'observed',spec_source:'request',verified_by:'tool',spec_hash:h,
        checks:[{check_id:'browser-url',type:'browser.url_match',verdict:'observed',
            evidence:[{kind:'browser.url_match',source:'tool',captured_at:'2026-01-01T00:00:01Z',step:1,digest:h}]}]},
    proof:{baseline_verdict:'not_observed',effect_count:1,effect_receipt_digests:[h]},
    started_at:'2026-01-01T00:00:00Z',finished_at:'2026-01-01T00:00:01Z',duration_ms:1000
}};
const wrong=JSON.parse(JSON.stringify(status));wrong.outcome.postcondition.checks[0].check_id='wrong';
const extra=JSON.parse(JSON.stringify(status));extra.outcome.postcondition.extra=true;
const malformed=JSON.parse(JSON.stringify(status));malformed.outcome.postcondition.checks[0].evidence[0].extra=true;
console.log(JSON.stringify({valid:v.isVerifiedSuccess(status),wrong:v.isVerifiedSuccess(wrong),
    extra:v.isVerifiedSuccess(extra),malformed:v.isVerifiedSuccess(malformed),
    wrongState:v.displayState(wrong)}));
"""
        result = subprocess.run(
            ["node", "-e", script, validator], capture_output=True, text=True,
            check=True,
        )
        data = json.loads(result.stdout)
        self.assertTrue(data["valid"])
        self.assertFalse(data["wrong"])
        self.assertFalse(data["extra"])
        self.assertFalse(data["malformed"])
        self.assertEqual(data["wrongState"], "indeterminate")

    def test_natural_approval_grammar_is_anchored_and_fail_closed(self):
        validator = os.path.join(PROJECT_ROOT, "core", "js", "action-outcomes.js")
        replies = {
            "yes": "approve", "please proceed": "approve",
            "not sure": "deny", "not okay": "deny",
            "maybe okay": "ambiguous", "I cannot confirm": "deny",
            "I did not approve this": "deny", "yes, do it": "ambiguous",
            "okay but not really": "deny", "I think yes": "ambiguous",
            "the word yes appears here": "ambiguous", "": "ambiguous",
        }
        script = (
            "const v=require(process.argv[1]);const rows=JSON.parse(process.argv[2]);"
            "const out={};for(const row of rows)out[row]=v.classifyApprovalReply(row);"
            "process.stdout.write(JSON.stringify(out));"
        )
        result = subprocess.run(
            ["node", "-e", script, validator, json.dumps(list(replies))],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout), replies)

    def test_bridge_executes_the_exact_validated_signed_spec(self):
        from bridge import core

        secret = "synthetic-bridge-authority"
        raw = {
            "goal": "cafe\u0301 exact goal", "vision_model": "vision-model",
            "use_director": False, "autonomy": "confirm_all", "max_steps": 7,
            "start_url": "https://example.com/start", "headless": True,
            "postcondition": {
                "type": "browser.url_match",
                "origin": "HTTPS://EXAMPLE.COM:443/", "path": "/done",
            },
        }
        module = os.path.join(PROJECT_ROOT, "standalone", "launch-capability.js")
        issue_script = (
            "const c=require(process.argv[1]);const raw=JSON.parse(process.argv[2]);"
            "process.stdout.write(c.issue(process.argv[3],'browser',raw));"
        )
        body = dict(raw)
        body["launch_capability"] = subprocess.run(
            ["node", "-e", issue_script, module,
             json.dumps(raw, ensure_ascii=False), secret],
            capture_output=True, text=True, check=True,
        ).stdout
        body["openai_api_key"] = "synthetic-key"
        expected = action_runs.launch_spec("browser", raw)
        handler = self._handler(body)
        agent = mock.Mock()
        agent.playwright_available.return_value = (True, "ok")
        agent.start_run.return_value = {"id": "0" * 16, "status": "running"}
        with mock.patch.object(core._st, "egress_mode", "cloud"), \
                mock.patch.object(core._st, "bridge_auth_token", secret), \
                mock.patch.object(core, "_BROWSER_AGENT", agent), \
                mock.patch.object(core, "_set_openai_key_from", return_value="key"):
            core.BridgeHandler._browser_run(handler)
        self.assertEqual(handler.responses[0][0], 202)
        kwargs = agent.start_run.call_args.kwargs
        for field in (
            "goal", "vision_model", "use_director", "autonomy", "max_steps",
            "start_url", "headless", "postcondition",
        ):
            self.assertEqual(kwargs[field], expected[field], field)

    def test_computer_use_mcp_is_rejected_in_every_mode(self):
        variants = (
            ("computer-use-linux", {"command": "safe-command", "args": []}),
            ("alias", {"command": "/usr/bin/computer-use-linux", "args": ["mcp"]}),
            ("alias", {"command": "npx", "args": ["-y", "@agent-sh/computer-use-linux"]}),
            ("alias", {"command": "computer-use-linux@1.2.3", "args": ["mcp"]}),
            ("alias", {"command": "npx", "args": [
                "cul@npm:@agent-sh/computer-use-linux@1.2.3",
            ]}),
            ("alias", {"command": "npx", "args": [
                "--package=cul@npm:@agent-sh/computer-use-linux@latest",
            ]}),
            ("alias", {"command": "sh", "args": [
                "-c", "exec computer-use-linux mcp",
            ]}),
            ("alias", {"command": "npx", "args": [
                "https://example.com/computer-use-linux.tgz",
            ]}),
            ("alias", {"command": "npx", "args": [
                "@agent-sh%2fcomputer-use-linux%40latest",
            ]}),
            ("alias", {"command": "sh", "args": [
                "-c", "computer-use-", "linux",
            ]}),
            ("alias", {"command": "sh", "args": [
                "-c", "x=linux; exec computer-use-$x mcp",
            ]}),
            ("azure-mcp-server", {"command": "npx", "args": [
                "-c", "x=linux; exec computer-use-$x mcp",
            ]}),
            ("kusto-mcp-server", {"command": "python3.12", "args": [
                "-c", "x='linux'; run('computer-use-'+x)",
            ]}),
            ("github-mcp-server", {"command": "busybox", "args": [
                "sh", "-c", "unsafe",
            ]}),
            ("playwright", {"command": "npx", "args": [
                "-y", "@playwright/mcp@latest",
            ]}),
        )
        for mode in bridge_config.EGRESS_MODE_VALUES:
            for name, config in variants:
                with self.subTest(mode=mode, name=name, config=config):
                    raw = {name: config}
                    allowed, rejected = bridge_config.mcp_config_for_egress(
                        raw, mode
                    )
                    self.assertNotIn(name, allowed)
                    self.assertIn(name, rejected)
        for relative in ("index.html", "core/js/copilot.js"):
            with open(os.path.join(PROJECT_ROOT, relative)) as handle:
                source = handle.read()
            self.assertNotIn("mcpComputerUse", source)
            self.assertNotIn("computer-use-linux', {", source)

    def test_persisted_computer_use_alias_is_removed_before_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mcp_config.json")
            persisted = {
                "desktop-alias": {
                    "command": "npx",
                    "args": ["-y", "@agent-sh/computer-use-linux@latest"],
                },
                "azure-mcp-server": {
                    "command": "npx",
                    "args": ["-y", "@azure/mcp@latest", "server", "start"],
                    "env": {"AZURE_MCP_COLLECT_TELEMETRY": "false"},
                },
            }
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(persisted, handle)
            with mock.patch.object(bridge_utils, "_MCP_CONFIG_CACHE_PATH", path):
                loaded = bridge_utils._load_persisted_mcp_config()
            self.assertNotIn("desktop-alias", loaded)
            self.assertIn("azure-mcp-server", loaded)
            with open(path, encoding="utf-8") as handle:
                rewritten = json.load(handle)
            self.assertEqual(rewritten, loaded)

    def test_renderer_sanitizes_computer_use_aliases(self):
        source_path = os.path.join(PROJECT_ROOT, "core", "js", "copilot.js")
        configs = [
            {"command": "computer-use-linux@1.2.3", "args": ["mcp"]},
            {"command": "npx", "args": [
                "cul@npm:@agent-sh/computer-use-linux@latest",
            ]},
            {"command": "sh", "args": [
                "-c", "exec computer-use-linux mcp",
            ]},
            {"command": "npx", "args": [
                "@agent-sh%252fcomputer-use-linux%2540latest",
            ]},
            {"command": "sh", "args": ["computer-use-", "linux"]},
            {"command": "sh", "args": [
                "-c", "x=linux; exec computer-use-$x mcp",
            ]},
        ]
        script = r"""
const fs=require('fs'),vm=require('vm');
global.document={addEventListener:()=>{},getElementById:()=>null};
global.window={};global.location={hostname:'localhost',protocol:'http:'};
global.localStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};
global.AbortSignal={timeout:()=>({})};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
const configs=JSON.parse(process.argv[2]);
const rejected=configs.map((cfg,index)=>
  Object.keys(sanitizeMCPConfig({['alias'+index]:cfg})).length===0);
const unknown=sanitizeMCPConfig({safe:{command:'safe-command',args:[]}});
const approved=sanitizeMCPConfig({'azure-mcp-server':{
    command:'npx',args:['-y','@azure/mcp@latest','server','start'],
    env:{AZURE_MCP_COLLECT_TELEMETRY:'false'}}});
process.stdout.write(JSON.stringify({rejected,unknown:Object.keys(unknown),
    approved:Object.keys(approved)}));
"""
        result = subprocess.run(
            ["node", "-e", script, source_path, json.dumps(configs)],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertTrue(all(data["rejected"]))
        self.assertEqual(data["unknown"], [])
        self.assertEqual(data["approved"], ["azure-mcp-server"])

    def test_inherited_copilot_mcp_alias_is_disabled_before_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mcp-config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"mcpServers": {
                    "legacy-desktop": {
                        "command": "npx",
                        "args": ["cul@npm:@agent-sh/computer-use-linux@latest"],
                    },
                    "safe": {"command": "safe-command", "args": []},
                    "azure-mcp-server": {
                        "command": "npx",
                        "args": [
                            "-y", "@azure/mcp@latest", "server", "start",
                        ],
                        "env": {"AZURE_MCP_COLLECT_TELEMETRY": "false"},
                    },
                }}, handle)
            with mock.patch.dict(os.environ, {"COPILOT_HOME": tmp}):
                disabled = acp_client._inherited_disabled_mcp_names()
            self.assertIn("computer-use-linux", disabled)
            self.assertIn("legacy-desktop", disabled)
            self.assertIn("safe", disabled)
            self.assertIn("azure-mcp-server", disabled)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{")
            with mock.patch.dict(os.environ, {"COPILOT_HOME": tmp}), \
                    self.assertRaises(RuntimeError):
                acp_client._inherited_disabled_mcp_names()
        with open(acp_client.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('cmd.extend(["--disable-mcp-server", server_name])', source)

    def test_acp_isolates_sources_and_per_server_credentials(self):
        github_secret = "ghp_" + "A" * 40
        kusto_secret = "Bearer" + "B" * 40
        config = {
            "github-mcp-server": {
                "command": "docker",
                "args": [
                    "run", "-i", "--rm", "-e",
                    "GITHUB_PERSONAL_ACCESS_TOKEN",
                    "ghcr.io/github/github-mcp-server",
                ],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": github_secret},
            },
            "kusto-mcp-server": {
                "command": sys.executable,
                "args": [os.path.join(TOOLS_DIR, "kusto_mcp.py")],
                "env": {
                    "KUSTO_ACCESS_TOKEN": kusto_secret,
                    "KUSTO_CLUSTER_URL": "https://example.kusto.windows.net",
                },
            },
        }
        calls = []

        def request(method, params, timeout=0):
            calls.append((method, params))
            return {"agentInfo": {}, "agentCapabilities": {}} \
                if method == "initialize" else {"sessionId": "session"}

        fake_process = mock.Mock()
        fake_process.stdin = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(acp_client, "_ARTIFACTS_DIR", tmp), \
                mock.patch.object(acp_client._st, "egress_mode", "cloud"), \
                mock.patch.object(
                    acp_client, "_inherited_disabled_mcp_names",
                    return_value=("computer-use-linux", "workspace-alias"),
                ), \
                mock.patch.object(
                    acp_client, "_resolve_and_preflight_copilot",
                    return_value="/usr/bin/copilot-pinned",
                ), \
                mock.patch.object(acp_client.subprocess, "Popen", return_value=fake_process) as popen, \
                mock.patch.object(acp_client.threading, "Thread") as thread:
            client = acp_client.ACPClient(mcp_config=config)
            client._source_copilot_home = os.path.join(tmp, "source-home")
            client._send_request = request
            client.start()
            command = popen.call_args.args[0]
            options = popen.call_args.kwargs
            parent_env = options["env"]
            runtime_root = client._runtime_dir
            self.assertIn("--disable-builtin-mcps", command)
            self.assertIn("--no-bash-env", command)
            self.assertIn("--no-custom-instructions", command)
            self.assertNotIn("--additional-mcp-config", command)
            self.assertNotIn(github_secret, json.dumps(command))
            self.assertNotIn(kusto_secret, json.dumps(command))
            self.assertNotIn(github_secret, json.dumps(parent_env))
            self.assertNotIn(kusto_secret, json.dumps(parent_env))
            self.assertEqual(options["cwd"], client._runtime_cwd)
            self.assertEqual(parent_env["COPILOT_HOME"], client._runtime_home)
            self.assertEqual(parent_env["HOME"], client._runtime_os_home)
            session = next(params for method, params in calls if method == "session/new")
            rows = {row["name"]: row for row in session["mcpServers"]}
            github_env = {row["name"]: row["value"] for row in rows["github-mcp-server"]["env"]}
            kusto_env = {row["name"]: row["value"] for row in rows["kusto-mcp-server"]["env"]}
            self.assertEqual(github_env, {"GITHUB_PERSONAL_ACCESS_TOKEN": github_secret})
            self.assertNotIn("KUSTO_ACCESS_TOKEN", github_env)
            self.assertEqual(kusto_env["KUSTO_ACCESS_TOKEN"], kusto_secret)
            self.assertNotIn("GITHUB_PERSONAL_ACCESS_TOKEN", kusto_env)
            thread.return_value.start.assert_called()
            client.stop()
            self.assertFalse(os.path.exists(runtime_root))

    def test_copilot_runtime_config_projects_only_auth_and_hardening(self):
        source = """
// synthetic Copilot config
{
  "lastLoggedInUser": {"host": "https://github.com", "login": "example"},
  "loggedInUsers": [{"host": "https://github.com", "login": "example"}],
  "trustedFolders": ["/unsafe"],
  "hooks": {"userPromptSubmitted": [{"command": "unsafe"}]},
  "ide": {"autoConnect": true},
  "installed_plugins": [{"name": "unsafe"}],
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            source_home = os.path.join(tmp, "source")
            artifacts = os.path.join(tmp, "artifacts")
            os.mkdir(source_home, 0o700)
            os.mkdir(artifacts, 0o700)
            source_path = os.path.join(source_home, "config.json")
            descriptor = os.open(
                source_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(source)
            projection = acp_client._auth_projection(source_path)
            self.assertEqual(projection["trustedFolders"], [])
            self.assertTrue(projection["disableAllHooks"])
            self.assertFalse(projection["ide"]["autoConnect"])
            self.assertNotIn("hooks", projection)
            self.assertNotIn("installed_plugins", projection)

            client = acp_client.ACPClient()
            client._source_copilot_home = source_home
            with mock.patch.object(acp_client, "_ARTIFACTS_DIR", artifacts):
                client._prepare_isolated_runtime()
                runtime_config = os.path.join(client._runtime_home, "config.json")
                self.assertFalse(os.path.islink(runtime_config))
                self.assertEqual(os.stat(runtime_config).st_mode & 0o777, 0o600)
                with open(runtime_config, encoding="utf-8") as handle:
                    written = json.load(handle)
                self.assertEqual(written, projection)
                client._cleanup_isolated_runtime()

            os.chmod(source_path, 0o644)
            with self.assertRaises(RuntimeError):
                acp_client._auth_projection(source_path)

    def test_web_fetch_is_disabled_before_network_access(self):
        self.assertNotIn("web_fetch", {tool["name"] for tool in web_search_mcp.TOOLS})
        with mock.patch.object(web_search_mcp.urllib.request, "build_opener") as opener:
            result = web_search_mcp.web_fetch("http://127.0.0.1/private")
            status, _body = web_search_mcp._http_get("http://127.0.0.1/private")
        self.assertIn("disabled", result["error"])
        self.assertEqual(status, 0)
        opener.assert_not_called()

    def test_kusto_cluster_origin_is_strict_locked_and_no_redirect(self):
        valid = "https://cluster.region.kusto.windows.net"
        self.assertEqual(kusto_mcp._normalize_kusto_origin(valid), (valid, None))
        for value in (
            "http://cluster.region.kusto.windows.net",
            "https://user@cluster.region.kusto.windows.net",
            "https://cluster.region.kusto.windows.net/path",
            "https://cluster.region.kusto.windows.net.evil.example",
            "https://evil.example/cluster.kusto.windows.net",
            "https://localhost.kusto.windows.net@127.0.0.1",
            "https://cluster.region.kusto.windows.net:444",
        ):
            with self.subTest(value=value):
                self.assertIsNotNone(kusto_mcp._normalize_kusto_origin(value)[1])
        with mock.patch.dict(os.environ, {
            "KUSTO_CLUSTER_URL": valid, "KUSTO_DATABASE": "Eva",
        }, clear=False):
            server = kusto_mcp.KustoMCPServer()
        self.assertEqual(server._resolve_cluster({}), (valid, None))
        self.assertIsNotNone(server._resolve_cluster({
            "cluster_url": "https://other.region.kusto.windows.net",
        })[1])
        response = mock.Mock(status_code=302, text="redirect")
        server._http = mock.Mock()
        server._http.post.return_value = response
        server._get_token = mock.Mock(return_value="synthetic-token")
        result = server._kusto_query(valid, "Eva", "print 1")
        self.assertIn("302", result)
        self.assertEqual(server._http.post.call_args.args[0], valid + "/v1/rest/query")
        self.assertFalse(server._http.post.call_args.kwargs["allow_redirects"])

    def test_direct_bridge_kusto_is_origin_locked_and_proxy_free(self):
        from bridge import state as bridge_state

        valid = "https://cluster.region.kusto.windows.net"
        saved = (
            bridge_state.egress_mode, bridge_state.kusto_token_cache,
            bridge_state.active_kusto_cluster,
        )
        try:
            bridge_state.egress_mode = "cloud"
            bridge_state.kusto_token_cache = "synthetic-token"
            bridge_state.active_kusto_cluster = valid
            with mock.patch("requests.Session") as factory:
                self.assertIsNone(bridge_kusto._kusto_query_direct(
                    "https://evil.example", "Eva", "print 1"
                ))
                factory.assert_not_called()

            response = mock.Mock(status_code=200, text="", json=lambda: {
                "Tables": [{"Columns": [], "Rows": []}],
            })
            session = mock.Mock()
            session.post.return_value = response
            with mock.patch("requests.Session", return_value=session):
                self.assertEqual(
                    bridge_kusto._kusto_query_direct(
                        valid, "Eva", "print 1"
                    ),
                    [],
                )
            self.assertFalse(session.trust_env)
            self.assertEqual(
                session.post.call_args.args[0], valid + "/v1/rest/query"
            )
            self.assertFalse(
                session.post.call_args.kwargs["allow_redirects"]
            )

            ingest_session = mock.Mock()
            ingest_session.post.return_value = response
            with mock.patch.object(
                bridge_kusto, "_get_table_columns", return_value=["Value"]
            ), mock.patch("requests.Session", return_value=ingest_session):
                self.assertTrue(bridge_kusto._kusto_ingest_direct(
                    valid, "Eva", "Knowledge", ["Value"], [{"Value": "x"}]
                ))
            self.assertFalse(ingest_session.trust_env)
            self.assertFalse(
                ingest_session.post.call_args.kwargs["allow_redirects"]
            )

            bridge_kusto._capture_active_kusto_env({
                "kusto-mcp-server": {
                    "env": {"KUSTO_CLUSTER_URL": "https://evil.example"},
                }
            })
            self.assertEqual(bridge_state.active_kusto_cluster, "")
        finally:
            (
                bridge_state.egress_mode, bridge_state.kusto_token_cache,
                bridge_state.active_kusto_cluster,
            ) = saved

    def test_release_has_no_automatic_playwright_mcp_route(self):
        self.assertFalse(os.path.exists(os.path.join(PROJECT_ROOT, "mcp.json")))
        package_path = os.path.join(PROJECT_ROOT, "standalone", "package.json")
        with open(package_path, encoding="utf-8") as handle:
            package = json.load(handle)
        filters = package["build"]["extraResources"][0]["filter"]
        self.assertNotIn("mcp.json", filters)
        with open(os.path.join(TOOLS_DIR, "bridge", "core.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("Auto-discovered MCP config", source)

    def test_mcp_process_boundaries_revalidate_exact_allowlist(self):
        malicious = {
            "playwright": {
                "command": "npx", "args": ["-y", "@playwright/mcp@latest"],
            }
        }
        client = acp_client.ACPClient(mcp_config=malicious)
        with mock.patch.object(acp_client._st, "egress_mode", "cloud"), \
            mock.patch.object(acp_client.subprocess, "Popen") as popen, \
                self.assertRaises(RuntimeError):
            client.start()
        popen.assert_not_called()

        manager = local_mcp.LocalMCPManager()
        with mock.patch.object(acp_client._st, "egress_mode", "cloud"), \
            mock.patch.object(local_mcp, "MCPServer") as server, \
                self.assertRaises(RuntimeError):
            manager.start_servers(malicious)
        server.assert_not_called()

    def test_nested_agent_markers_parse_complete_postconditions(self):
        parser = os.path.join(PROJECT_ROOT, "core", "js", "agent-markers.js")
        marker = (
            '[[EVA_BROWSER]]{"goal":"visit","postcondition":'
            '{"type":"browser.url_match","origin":"https://example.com",'
            '"path":"/done"}}[[/EVA_BROWSER]]'
        )
        script = (
            "const p=require(process.argv[1]);"
            "const r=p.extract(process.argv[2],'browser');"
            "process.stdout.write(JSON.stringify(r.payload));"
        )
        result = subprocess.run(
            ["node", "-e", script, parser, marker],
            capture_output=True, text=True, check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload["postcondition"]["path"], "/done")

        conflict = marker + '[[EVA_DESKTOP]]{"goal":"also"}[[/EVA_DESKTOP]]'
        conflict_script = (
            "const p=require(process.argv[1]);"
            "process.stdout.write(JSON.stringify(p.extractControlMarkers(process.argv[2])));"
        )
        conflict_result = subprocess.run(
            ["node", "-e", conflict_script, parser, conflict],
            capture_output=True, text=True, check=True,
        )
        conflict_payload = json.loads(conflict_result.stdout)
        self.assertTrue(conflict_payload["conflict"])
        self.assertIsNone(conflict_payload["browser"])
        self.assertIsNone(conflict_payload["desktop"])
        duplicate = marker + marker.replace('"visit"', '"visit again"')
        duplicate_result = subprocess.run(
            ["node", "-e", conflict_script, parser, duplicate],
            capture_output=True, text=True, check=True,
        )
        duplicate_payload = json.loads(duplicate_result.stdout)
        self.assertTrue(duplicate_payload["conflict"])
        self.assertIsNone(duplicate_payload["browser"])

    def test_packaged_files_include_launch_authority_module(self):
        package_path = os.path.join(PROJECT_ROOT, "standalone", "package.json")
        with open(package_path) as handle:
            package = json.load(handle)
        self.assertIn("launch-capability.js", package["build"]["files"])

    def test_runtime_banner_lists_all_action_endpoints(self):
        core_path = os.path.join(TOOLS_DIR, "bridge", "core.py")
        with open(core_path) as handle:
            source = handle.read()
        for agent in ("browser", "desktop"):
            for route in ("run", "status", "screenshot", "confirm", "cancel"):
                self.assertIn(f"/v1/{agent}/{route}", source)

    def test_launch_canonicalizers_reject_lone_surrogates(self):
        with self.assertRaises(ActionRunValidationError):
            action_runs.launch_spec_hash("desktop", {"goal": "bad\ud800"})
        module = os.path.join(PROJECT_ROOT, "standalone", "launch-capability.js")
        script = (
            "const c=require(process.argv[1]);"
            "try{c.specHash('desktop',{goal:'bad\\ud800'});process.exit(1)}"
            "catch(_){process.exit(0)}"
        )
        result = subprocess.run(["node", "-e", script, module])
        self.assertEqual(result.returncode, 0)
        composed = action_runs.launch_spec_hash("desktop", {"goal": "caf\u00e9 😀"})
        decomposed = action_runs.launch_spec_hash("desktop", {"goal": "cafe\u0301 😀"})
        self.assertEqual(composed, decomposed)
        parity_script = (
            "const c=require(process.argv[1]);"
            "process.stdout.write(c.specHash('desktop',{goal:'cafe\\u0301 😀'}));"
        )
        node_hash = subprocess.run(
            ["node", "-e", parity_script, module],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(node_hash, composed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
