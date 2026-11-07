"""User-initiated Signal provisioning; QR credentials never reach logs or disk."""

import base64
import json
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from bridge import config as _cfg

_lock = threading.RLock()
_job = None
_TIMEOUT = 150


def active_config():
    pointer = Path(_cfg.EVA_CONFIG_DIR) / "signal-profile.json"
    if not pointer.exists():
        return None
    profile = json.loads(pointer.read_text())["profile"]
    if not isinstance(profile, str) or not re.fullmatch(r"[a-f0-9]{32}", profile):
        raise ValueError("Invalid Signal profile")
    path = Path(_cfg.EVA_CONFIG_DIR) / "signal-profiles" / profile
    if not path.is_dir():
        raise ValueError("Signal profile unavailable")
    return str(path)


def _public(job):
    if not job:
        return {"status": "idle"}
    value = {key: job[key] for key in ("id", "status", "message")}
    if job["status"] == "pending" and job.get("qr"):
        value["qr"] = job["qr"]
    if job["status"] == "linked":
        value["sender"] = job["sender"]
        value["recipient"] = job["recipient"]
    return value


def status():
    with _lock:
        return _public(_job)


def start():
    global _job
    with _lock:
        if _job and _job["status"] in {"starting", "pending", "cancelling"}:
            return _public(_job)
        if not shutil.which(_cfg.SIGNAL_CLI_PATH):
            raise ValueError("Install signal-cli on the bridge host before linking.")
        if not shutil.which("qrencode"):
            raise ValueError("Install qrencode on the bridge host to display the Signal linking QR.")
        identifier = uuid.uuid4().hex
        _job = {"id": identifier, "status": "starting", "message": "Preparing a Signal linking QR.",
                "qr": "", "process": None, "cancel": threading.Event()}
        threading.Thread(target=_run, args=(_job,), daemon=True).start()
        return _public(_job)


def cancel(identifier):
    with _lock:
        if not _job or _job["id"] != identifier:
            return None
        if _job["status"] in {"starting", "pending"}:
            _job["cancel"].set()
            _job["qr"] = ""
            _job["status"] = "cancelling"
            _job["message"] = "Cancelling linking."
        return _public(_job)


def stop():
    with _lock:
        if _job and _job["status"] in {"starting", "pending", "cancelling"}:
            _job["cancel"].set()
            _job["qr"] = ""
            process = _job.get("process")
            if process and process.poll() is None:
                process.terminate()


def _finish(job, state, message):
    with _lock:
        job.update(status=state, message=message, qr="", process=None)


def _activate(job, profile, sender):
    from bridge.alerts import _load_alerts, _save_alerts
    from bridge import state as runtime
    with runtime.alerts_lock:
        document = _load_alerts()
        previous = json.loads(json.dumps(document))
        recipient = document["settings"].get("signal_recipient") or sender
        document["settings"].update(signal_sender=sender, signal_recipient=recipient)
        _save_alerts(document)
        saved = _load_alerts()["settings"]
        if saved.get("signal_sender") != sender or saved.get("signal_recipient") != recipient:
            raise OSError("Settings save failed")
        directory = Path(_cfg.EVA_CONFIG_DIR)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=directory, prefix="signal-profile-", suffix=".json", delete=False) as file:
                temporary = Path(file.name)
                json.dump({"profile": profile.name}, file)
            os.replace(temporary, directory / "signal-profile.json")
        except OSError:
            _save_alerts(previous)
            raise
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    with _lock:
        job.update(sender=sender, recipient=recipient)


def _run(job):
    process = None
    selector = selectors.DefaultSelector()
    try:
        profile = Path(_cfg.EVA_CONFIG_DIR) / "signal-profiles" / job["id"]
        profile.mkdir(parents=True, mode=0o700)
        process = subprocess.Popen(
            [_cfg.SIGNAL_CLI_PATH, "--config", str(profile), "--scrub-log", "link", "-n", "Eva"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        with _lock:
            job["process"] = process
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        deadline = time.monotonic() + _TIMEOUT
        sender = ""
        timed_out = False
        while time.monotonic() < deadline and (process.poll() is None or selector.get_map()):
            if job["cancel"].is_set():
                break
            for key, _ in selector.select(timeout=0.5):
                line = key.fileobj.readline(8192)
                if not line:
                    selector.unregister(key.fileobj)
                    continue
                value = line.strip()
                if key.fileobj is process.stderr and ("timed out" in value.lower() or "timeout" in value.lower()):
                    timed_out = True
                if value.startswith("sgnl://linkdevice?") and len(value) <= 4096:
                    png = subprocess.run(
                        ["qrencode", "-s", "8", "-m", "4", "-t", "PNG", "-o", "-"],
                        input=value.encode(), capture_output=True, timeout=10, check=True,
                    ).stdout
                    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > 512000:
                        raise ValueError("Invalid QR image")
                    with _lock:
                        if not job["cancel"].is_set():
                            job.update(status="pending", message="Scan with Signal Settings > Linked devices.",
                                       qr="data:image/png;base64," + base64.b64encode(png).decode())
                elif key.fileobj is process.stdout and value.startswith("Associated with:"):
                    account = value.partition(":")[2].strip()
                    if re.fullmatch(r"\+[1-9][0-9]{6,14}", account):
                        sender = account
        if job["cancel"].is_set():
            _finish(job, "cancelled", "Linking cancelled. Previous Signal setup was kept.")
        elif process.poll() is None or timed_out:
            _finish(job, "expired", "QR expired. Select Link Signal to generate a fresh code.")
        elif process.returncode != 0 or not sender:
            _finish(job, "failed", "Signal linking did not complete. Check your phone's Linked devices and try again.")
        else:
            with _lock:
                if job["cancel"].is_set():
                    _finish(job, "cancelled", "Linking cancelled. Previous Signal setup was kept.")
                else:
                    _activate(job, profile, sender)
                    _finish(job, "linked", "Signal linked. Sender saved; your existing recipient was preserved.")
    except (OSError, ValueError, subprocess.SubprocessError):
        _finish(job, "failed", "Signal linking is unavailable or could not save its setup. Previous account files were kept.")
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        selector.close()
        if process:
            process.stdout.close()
            process.stderr.close()
