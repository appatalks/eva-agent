"""Rootless, network-isolated execution for explicitly opted-in workspace runs."""

import json
import os
import platform
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from bridge import config, state
from bridge.utils import _safe_child_environment
from workspace_diff_guard import scan_text, sensitive_path

_IMAGE = "localhost/eva-coding-sandbox:debian"
_IMAGE_ID = re.compile(r"^[a-f0-9]{64}$")
_CONTAINER_ID = re.compile(r"^[a-f0-9]{64}$")
_MAX_WORKSPACE_FILES = 50000
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_SCAN_BYTES = 128 * 1024 * 1024
_MAX_OUTPUT_BYTES = 65536
_SECURITY_FILE = "security-officer.jsonl"
_LEASE_DIR = "sandbox-leases"
_APPROVED_IMAGE_FILE = "sandbox-approved-image.json"
_PROTECTED_NAME = re.compile(
    r"(?:^|/)(?:\.env(?:\.[^/]*)?|\.ssh|\.aws|\.azure|\.netrc|"
    r"\.git-credentials|id_rsa|id_ed25519|config\.json|config\.local\.js|"
    r"[^/]*\.(?:pem|p12|pfx|key)|"
    r"(?:credentials?|secrets?|tokens?)(?:[._-](?:local|production|prod|dev|cache))?"
    r"\.(?:json|txt|ini|ya?ml|db))$",
    re.IGNORECASE,
)
_PRIVATE_KEY_MATERIAL = re.compile(
    r"(?m)^-----BEGIN (?:[A-Z0-9 ]*PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----\r?\n"
    r"(?:(?:[A-Za-z-]+:[^\r\n]*\r?\n)|(?:[ \t]*\r?\n)){0,6}[A-Za-z0-9+/=]{32,}"
)
_EVENT_REASONS = {
    "", "protected_path", "credential_detected", "nested_mount",
    "image_changed", "runtime_unavailable", "credential_output", "git_metadata_changed",
}
_REASON_TEXT = {
    "protected_path": "A protected path or symlink prevented a workspace run.",
    "credential_detected": "Recognizable credential material prevented a workspace run.",
    "nested_mount": "A nested mount prevented a workspace run.",
    "image_changed": "The sandbox image no longer matches the approved identity.",
    "runtime_unavailable": "Rootless sandbox runtime is unavailable.",
    "credential_output": "Sandbox output containing recognizable credential material was withheld.",
    "git_metadata_changed": "Workspace Git metadata changed during an isolated run.",
}


class SandboxError(Exception):
    """A safe, user-visible sandbox failure."""

    def __init__(self, message, reason=""):
        super().__init__(message)
        self.reason = reason if reason in _EVENT_REASONS else ""


def _podman():
    executable = "/usr/bin/podman"
    if os.name != "posix" or not os.path.isfile(executable):
        raise SandboxError("Rootless Podman is not installed.", "runtime_unavailable")
    resolved = os.path.realpath(executable)
    try:
        for path in (resolved, os.path.dirname(resolved), os.path.dirname(os.path.dirname(resolved))):
            info = os.stat(path)
            if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise SandboxError("The Podman executable is not trusted.", "runtime_unavailable")
    except OSError as error:
        raise SandboxError("The Podman executable cannot be verified.", "runtime_unavailable") from error
    return resolved


def _podman_command(arguments, timeout=15):
    try:
        return subprocess.run(
            [_podman(), *arguments], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=timeout, env=_safe_child_environment(), check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SandboxError("The rootless container runtime is unavailable.", "runtime_unavailable") from error


def _runtime_security():
    info = _podman_command(["info", "--format", "json"])
    try:
        security = json.loads(info.stdout)["host"]["security"]
    except (ValueError, KeyError, TypeError):
        raise SandboxError("Podman security settings could not be verified.", "runtime_unavailable") from None
    if info.returncode or not isinstance(security, dict) or security.get("rootless") is not True:
        raise SandboxError("Rootless Podman is unavailable; sandbox execution was not started.", "runtime_unavailable")
    if security.get("seccompEnabled") is not True:
        raise SandboxError("Podman seccomp is unavailable; sandbox execution was not started.", "runtime_unavailable")
    if platform.machine().lower() not in {"x86_64", "aarch64"}:
        raise SandboxError("Sandbox namespace filtering is unavailable on this CPU architecture.", "runtime_unavailable")
    return security


def _hardened_seccomp_profile(security):
    profile_path = security.get("seccompProfilePath")
    if not isinstance(profile_path, str) or not os.path.isabs(profile_path):
        raise SandboxError("The Podman seccomp profile cannot be verified.", "runtime_unavailable")
    path = os.path.realpath(profile_path)
    try:
        current = path
        while True:
            info = os.stat(current)
            if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise SandboxError("The Podman seccomp profile is not trusted.", "runtime_unavailable")
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r", encoding="utf-8") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                raise SandboxError("The Podman seccomp profile is invalid.", "runtime_unavailable")
            profile = json.load(file)
    except (OSError, ValueError, UnicodeError) as error:
        raise SandboxError("The Podman seccomp profile cannot be read safely.", "runtime_unavailable") from error
    return _restrict_seccomp_profile(profile)


def _restrict_seccomp_profile(profile):
    if (not isinstance(profile, dict) or profile.get("defaultAction") != "SCMP_ACT_ERRNO"
            or not isinstance(profile.get("syscalls"), list)):
        raise SandboxError("The Podman seccomp profile is not compatible with namespace filtering.", "runtime_unavailable")
    restricted = {"unshare", "setns", "clone", "clone3"}
    rules = []
    clone_rules = []
    for rule in profile["syscalls"]:
        if not isinstance(rule, dict) or not isinstance(rule.get("names"), list):
            raise SandboxError("The Podman seccomp profile contains an invalid rule.", "runtime_unavailable")
        names = rule["names"]
        if any(not isinstance(name, str) for name in names):
            raise SandboxError("The Podman seccomp profile contains an invalid rule.", "runtime_unavailable")
        if "clone" in names:
            clone_rules.append(rule)
        filtered = [name for name in names if name not in restricted]
        if filtered:
            rules.append(dict(rule, names=filtered))
    if (len(clone_rules) != 1 or clone_rules[0].get("action") != "SCMP_ACT_ALLOW"
            or clone_rules[0].get("args") or clone_rules[0].get("includes") or clone_rules[0].get("excludes")):
        raise SandboxError("The Podman seccomp clone baseline is not compatible with namespace filtering.", "runtime_unavailable")
    rules.append({"names": ["clone3"], "action": "SCMP_ACT_ERRNO", "errnoRet": 38, "args": []})
    rules.append({
        "names": ["clone"], "action": "SCMP_ACT_ALLOW",
        "args": [{"index": 0, "value": 0x10000000, "valueTwo": 0, "op": "SCMP_CMP_MASKED_EQ"}],
    })
    profile["syscalls"] = rules
    return profile


def sandbox_candidate():
    _hardened_seccomp_profile(_runtime_security())
    image = _podman_command(["image", "inspect", "--format", "{{.Id}}", _IMAGE])
    image_id = image.stdout.strip().removeprefix("sha256:")
    if image.returncode or not _IMAGE_ID.fullmatch(image_id):
        raise SandboxError("The approved local sandbox image is unavailable.", "runtime_unavailable")
    return image_id


def approve_sandbox_image(image_id):
    if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
        raise SandboxError("The sandbox image identity is invalid.")
    if sandbox_candidate() != image_id:
        raise SandboxError("The local image changed; review its identity again.")
    os.makedirs(config.EVA_CONFIG_DIR, mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=".sandbox-image-", dir=config.EVA_CONFIG_DIR, delete=False
    ) as file:
        path = file.name
        try:
            os.fchmod(file.fileno(), 0o600)
            json.dump({"image_id": image_id}, file)
        except Exception:
            os.unlink(path)
            raise
    try:
        os.replace(path, os.path.join(config.EVA_CONFIG_DIR, _APPROVED_IMAGE_FILE))
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return image_id


def sandbox_preflight():
    image_id = sandbox_candidate()
    try:
        descriptor = os.open(os.path.join(config.EVA_CONFIG_DIR, _APPROVED_IMAGE_FILE), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r", encoding="utf-8") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
                raise ValueError("Invalid image approval file")
            approved = json.load(file)
    except (OSError, ValueError):
        raise SandboxError("Review and approve the local sandbox image in the Security Officer first.") from None
    if not isinstance(approved, dict) or approved.get("image_id") != image_id:
        raise SandboxError("The local sandbox image does not match the approved identity.", "image_changed")
    return image_id


def _nested_mounts(root):
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as file:
            for line in file:
                fields = line.split()
                if len(fields) < 5:
                    raise SandboxError("Workspace mounts cannot be verified.")
                mountpoint = re.sub(
                    r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), fields[4]
                )
                if mountpoint != root and os.path.commonpath([root, mountpoint]) == root:
                    return True
    except (OSError, ValueError) as error:
        raise SandboxError("Workspace mounts cannot be verified.") from error
    return False


def _git_marker(root):
    try:
        descriptor = os.open(os.path.join(root, ".git"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SandboxError("Workspace Git metadata cannot be safely verified.", "protected_path") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 4096:
            raise SandboxError("Workspace Git metadata cannot be safely mounted.", "protected_path")
        content = os.read(descriptor, 4097)
        after = os.fstat(descriptor)
        if (len(content) > 4096 or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            raise SandboxError("Workspace Git metadata changed during verification.", "git_metadata_changed")
        return (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, content)
    except OSError as error:
        raise SandboxError("Workspace Git metadata cannot be safely verified.", "protected_path") from error
    finally:
        os.close(descriptor)


def validate_workspace(workspace):
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir() or ":" in str(root) or "\n" in str(root) or "\r" in str(root):
        raise SandboxError("The workspace path cannot be safely mounted.")
    if _nested_mounts(str(root)):
        raise SandboxError("The workspace contains a nested mount and cannot be sandboxed.", "nested_mount")
    _git_marker(str(root))
    total = 0
    files = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in list(dirs) + names:
            path = Path(current, name)
            relative = path.relative_to(root).as_posix()
            if relative == ".git":
                continue
            if ".git" in Path(relative).parts:
                raise SandboxError("Nested Git metadata cannot be sandboxed.", "protected_path")
            if path.is_symlink() or sensitive_path(relative) or _PROTECTED_NAME.search(relative):
                raise SandboxError("The workspace contains a protected path or symlink; it cannot be sandboxed.", "protected_path")
            if path.is_dir():
                continue
            info = path.stat(follow_symlinks=False)
            if not path.is_file() or info.st_nlink != 1 or info.st_size > _MAX_FILE_BYTES:
                raise SandboxError("The workspace contains an unsupported file or hardlink.")
            files += 1
            total += info.st_size
            if files > _MAX_WORKSPACE_FILES or total > _MAX_SCAN_BYTES:
                raise SandboxError("The workspace exceeds the sandbox preflight scan limit.")
            with path.open("rb") as source:
                sample = source.read()
            if b"\x00" not in sample:
                text = sample.decode("utf-8", errors="replace")
                findings = scan_text(text, "workspace")
                if any(finding["kind"] != "private-key" for finding in findings) or (
                    any(finding["kind"] == "private-key" for finding in findings)
                    and _PRIVATE_KEY_MATERIAL.search(text)
                ):
                    raise SandboxError("The workspace contains a recognizable credential; sandbox execution was not started.", "credential_detected")
    return str(root)


def record_event(kind, outcome, reason=""):
    if kind not in {"runtime", "command", "self_test", "boundary"} or outcome not in {"ready", "unavailable", "started", "completed", "failed", "cancelled", "timeout", "blocked"}:
        return
    if reason not in _EVENT_REASONS:
        return
    try:
        os.makedirs(config.EVA_CONFIG_DIR, mode=0o700, exist_ok=True)
        path = os.path.join(config.EVA_CONFIG_DIR, _SECURITY_FILE)
        payload = (json.dumps({"id": uuid.uuid4().hex, "time": int(time.time()), "kind": kind, "outcome": outcome, "reason": reason}) + "\n").encode()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _recent_events():
    try:
        path = os.path.join(config.EVA_CONFIG_DIR, _SECURITY_FILE)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            source.seek(0, os.SEEK_END)
            source.seek(max(0, source.tell() - 8192))
            lines = source.read().splitlines()[-30:]
        result = []
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if (isinstance(event, dict) and re.fullmatch(r"[a-f0-9]{32}", str(event.get("id") or ""))
                    and isinstance(event.get("kind"), str) and event["kind"] in {"runtime", "command", "self_test", "boundary"}
                    and isinstance(event.get("outcome"), str)
                    and event["outcome"] in {"ready", "unavailable", "started", "completed", "failed", "cancelled", "timeout", "blocked"}
                    and isinstance(event.get("reason", ""), str) and event.get("reason", "") in _EVENT_REASONS):
                result.append(event)
        return result
    except OSError:
        return []


def _process_start(pid):
    try:
        stat = Path("/proc", str(pid), "stat").read_text(encoding="ascii")
        return stat[stat.rfind(")") + 2:].split()[19]
    except (OSError, IndexError, UnicodeError):
        return ""


def _lease_directory():
    root = os.path.join(config.EVA_CONFIG_DIR, _LEASE_DIR)
    os.makedirs(root, mode=0o700, exist_ok=True)
    if os.path.islink(root):
        raise SandboxError("The sandbox lease directory is not safe.")
    return root


def _create_lease(lease_id, name):
    root = _lease_directory()
    start = _process_start(os.getpid())
    if not start:
        raise SandboxError("Sandbox process ownership cannot be verified.")
    path = os.path.join(root, lease_id + ".json")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump({"lease": lease_id, "name": name, "pid": os.getpid(), "start": start}, file)
    return path


def _cleanup_container(name, lease_id):
    inspect = _podman_command([
        "container", "inspect", "--format",
        '{{.Id}} {{index .Config.Labels "io.eva.sandbox.lease"}}', name,
    ])
    if inspect.returncode == 0:
        fields = inspect.stdout.strip().split()
        if len(fields) != 2 or not _CONTAINER_ID.fullmatch(fields[0]) or fields[1] != lease_id:
            return False
        _podman_command(["rm", "-f", fields[0]], timeout=15)
        return _podman_command(["container", "exists", fields[0]]).returncode == 1
    return _podman_command(["container", "exists", name]).returncode == 1


def _reap_stale_leases():
    try:
        root = _lease_directory()
        entries = list(os.scandir(root))
    except (OSError, SandboxError):
        return
    for entry in entries:
        match = re.fullmatch(r"([a-f0-9]{32})\.json", entry.name)
        if not match or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            continue
        try:
            with open(entry.path, "r", encoding="utf-8") as file:
                lease = json.load(file)
            lease_id = match.group(1)
            name = "eva-sandbox-" + lease_id
            pid = lease.get("pid")
            if (lease.get("lease") != lease_id or lease.get("name") != name
                    or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
                continue
            closing = os.path.join(root, lease_id + ".closing")
            if _process_start(pid) == lease.get("start") and lease.get("start") and not os.path.isfile(closing):
                continue
            try:
                cleaned = _cleanup_container(name, lease_id)
            except SandboxError:
                cleaned = False
            if not cleaned:
                continue
            record_event("command", "cancelled")
            os.unlink(entry.path)
            if not os.path.islink(closing):
                try:
                    os.unlink(closing)
                except FileNotFoundError:
                    pass
            cidfile = os.path.join(root, lease_id + ".cid")
            if not os.path.islink(cidfile):
                try:
                    os.unlink(cidfile)
                except FileNotFoundError:
                    pass
        except (OSError, ValueError, SandboxError):
            continue


class SecurityOfficer:
    """Lightweight runtime health monitor, not a model or a policy authority."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = {"status": "starting", "activity": "Checking rootless sandbox availability"}
        self.thread = None
        self.alert = None

    def start(self, alert=None):
        if self.thread and self.thread.is_alive():
            return
        self.alert = alert
        self.thread = threading.Thread(target=self._monitor, name="eva-security-officer", daemon=True)
        self.thread.start()

    def _notify(self, message):
        if callable(self.alert):
            try:
                self.alert(message)
            except Exception:
                pass

    def refresh(self):
        try:
            sandbox_preflight()
            status = {"status": "ready", "activity": "Rootless sandbox ready; ACP chat remains unsandboxed"}
        except SandboxError as error:
            status = {"status": "unavailable", "activity": str(error)}
        with self.lock:
            self.status = status
        return status

    def _monitor(self):
        previous = None
        seen = {event["id"] for event in _recent_events()}
        while not state.acp_shutdown.is_set():
            _reap_stale_leases()
            current = self.refresh()["status"]
            if current != previous:
                record_event("runtime", current)
                if previous == "ready" and current == "unavailable":
                    self._notify("Rootless sandbox became unavailable; isolated runs will fail closed.")
                previous = current
            for _ in range(20):
                if state.acp_shutdown.wait(3):
                    return
                _reap_stale_leases()
                for event in _recent_events():
                    if event["id"] not in seen and event["kind"] == "boundary" and event["outcome"] == "blocked":
                        self._notify(_REASON_TEXT.get(event.get("reason"), "A workspace sandbox safety check blocked an action."))
                    seen.add(event["id"])
                if len(seen) > 200:
                    seen = {event["id"] for event in _recent_events()}

    def snapshot(self):
        with self.lock:
            result = dict(self.status)
        events = [event for event in _recent_events() if event["kind"] in {"command", "boundary", "self_test"}]
        if events:
            result["activity"] += "; last sandbox event " + events[-1]["outcome"]
            if events[-1].get("reason") in _REASON_TEXT:
                result["activity"] += ": " + _REASON_TEXT[events[-1]["reason"]]
        return result


officer = SecurityOfficer()


def analysis_summary():
    counts = {}
    for event in _recent_events():
        key = "/".join(filter(None, (event["kind"], event["outcome"], event.get("reason", ""))))
        counts[key] = counts.get(key, 0) + 1
    status = officer.snapshot().get("status")
    return {
        "officer_status": status if status in {"starting", "ready", "unavailable"} else "unknown",
        "recent_event_counts": counts,
        "sandbox_network": "disabled",
        "chat_sandboxed": False,
        "workspace_output_may_reach_model_provider": True,
        "credential_detection": "non-exhaustive",
    }


def run_self_test():
    image = sandbox_preflight()
    with tempfile.TemporaryDirectory(prefix="eva-security-test-") as directory:
        workspace = os.path.join(directory, "workspace")
        os.mkdir(workspace, 0o700)
        command = (
            "python3 -c 'import ctypes,os,subprocess,threading; "
            "assert not os.path.exists(\"/run/podman/podman.sock\"); "
            "assert not os.path.exists(\"/root/.ssh\"); "
            "assert set(os.listdir(\"/sys/class/net\")) == {\"lo\"}; "
            "assert len(open(\"/proc/net/route\").readlines()) == 1; "
            "assert not any(k.lower().endswith(\"_proxy\") for k in os.environ); "
            "libc=ctypes.CDLL(None,use_errno=True); "
            "assert libc.unshare(0x10000000) == -1; "
            "clone_nr=56 if os.uname().machine == \"x86_64\" else 220; "
            "assert libc.syscall(clone_nr,0x10000011,0,0,0,0) == -1; "
            "assert libc.syscall(435,0,0) == -1 and ctypes.get_errno() == 38; "
            "t=threading.Thread(target=lambda:None); t.start(); t.join(); "
            "subprocess.run([\"/bin/true\"],check=True); "
            "print(\"sandbox boundary checks passed\")'"
        )
        try:
            result = run_in_sandbox(workspace, image, command, timeout=30)
            if result["exit_code"] != 0:
                raise SandboxError("Sandbox boundary self-test failed.")
        except SandboxError:
            record_event("self_test", "failed")
            raise
        record_event("self_test", "ready")
        return {"passed": True, "message": "Offline sandbox boundary checks passed."}


def run_in_sandbox(workspace, image_id, command, timeout=120, cancel=None):
    if not _IMAGE_ID.fullmatch(str(image_id or "")):
        raise SandboxError("The sandbox image identity is invalid.")
    if not isinstance(command, str) or not command.strip() or len(command) > 8192 or "\x00" in command:
        raise SandboxError("The sandbox command is invalid.")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise SandboxError("Sandbox timeout must be between 1 and 300 seconds.")
    try:
        root = validate_workspace(workspace)
        git_marker = _git_marker(root)
        current_image = sandbox_preflight()
        if current_image != image_id:
            raise SandboxError("The approved sandbox image changed; start a new isolated run.", "image_changed")
    except (SandboxError, OSError) as error:
        record_event("boundary", "blocked", getattr(error, "reason", ""))
        raise
    with tempfile.TemporaryDirectory(prefix="eva-sandbox-") as directory:
        profile_path = os.path.join(directory, "seccomp.json")
        profile = _hardened_seccomp_profile(_runtime_security())
        descriptor = os.open(profile_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(profile, file)
        git_mount = []
        if git_marker is not None:
            placeholder = os.path.join(directory, "git-placeholder")
            descriptor = os.open(placeholder, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
            os.close(descriptor)
            git_mount = ["--volume", placeholder + ":/workspace/.git:ro"]
        lease_id = uuid.uuid4().hex
        name = "eva-sandbox-" + lease_id
        lease_path = _create_lease(lease_id, name)
        cidfile = os.path.join(_lease_directory(), lease_id + ".cid")
        arguments = [
            _podman(), "run", "--rm", "--pull=never", "--cidfile", cidfile, "--name", name,
            "--label", "io.eva.sandbox=true", "--label", "io.eva.sandbox.lease=" + lease_id,
            "--network=none", "--http-proxy=false",
            "--userns=keep-id", "--user", f"{os.getuid()}:{os.getgid()}",
            "--cap-drop=all", "--security-opt=no-new-privileges",
            "--security-opt=seccomp=" + profile_path, "--read-only",
            "--pids-limit=64", "--memory=512m", "--cpus=2",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--volume", root + ":/workspace:rw", *git_mount, "--workdir", "/workspace",
            "--env", "HOME=/tmp", "--env", "PATH=/usr/local/bin:/usr/bin:/bin",
            "--", "sha256:" + image_id, "/usr/bin/timeout", "--signal=KILL", str(timeout + 5),
            "/bin/sh", "-c", command,
        ]
        record_event("command", "started")
        try:
            process = subprocess.Popen(
                arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=_safe_child_environment(), start_new_session=True,
            )
        except OSError as error:
            os.unlink(lease_path)
            record_event("command", "failed")
            raise SandboxError("Sandbox process could not be started.") from error
        output = bytearray()
        output_limit = threading.Event()

        def drain():
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    return
                remaining = _MAX_OUTPUT_BYTES - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    output_limit.set()

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        outcome = "completed"
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel and cancel.is_set():
                    outcome = "cancelled"
                    break
                if time.monotonic() >= deadline or output_limit.is_set():
                    outcome = "timeout" if time.monotonic() >= deadline else "failed"
                    break
                time.sleep(0.05)
            if outcome != "completed":
                try:
                    container_id = Path(cidfile).read_text(encoding="ascii").strip()
                except (OSError, UnicodeError):
                    container_id = ""
                if _CONTAINER_ID.fullmatch(container_id):
                    _podman_command(["rm", "-f", container_id], timeout=15)
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            reader.join(timeout=5)
            if outcome != "completed":
                raise SandboxError("Sandbox command " + outcome + ".")
            text = bytes(output).decode("utf-8", errors="replace")
            if scan_text(text, "sandbox-output"):
                record_event("boundary", "blocked", "credential_output")
                raise SandboxError("Sandbox output contained recognizable credentials and was withheld.", "credential_output")
            return {"exit_code": process.returncode, "output": text, "truncated": False}
        except (OSError, SandboxError):
            if outcome == "completed":
                outcome = "failed"
            raise
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    outcome = "failed"
            reader.join(timeout=5)
            if process.stdout is not None and not reader.is_alive():
                process.stdout.close()
            try:
                cleaned = _cleanup_container(name, lease_id)
            except SandboxError:
                cleaned = False
            if not cleaned:
                closing = os.path.join(_lease_directory(), lease_id + ".closing")
                descriptor = os.open(closing, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(descriptor)
                record_event("command", "failed")
                raise SandboxError("Sandbox cleanup could not be verified.")
            if not os.path.islink(cidfile):
                try:
                    os.unlink(cidfile)
                except FileNotFoundError:
                    pass
            os.unlink(lease_path)
            try:
                marker_unchanged = _git_marker(root) == git_marker
            except SandboxError:
                marker_unchanged = False
            if not marker_unchanged:
                record_event("boundary", "blocked", "git_metadata_changed")
                record_event("command", "failed")
                raise SandboxError("Workspace Git metadata changed during the sandbox run.", "git_metadata_changed")
            record_event("command", outcome if outcome != "completed" or process.returncode == 0 else "failed")
