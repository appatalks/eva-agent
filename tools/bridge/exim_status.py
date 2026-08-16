"""Read-only Exim transport status for Eva local-MTA submissions.

The module never sends mail, invokes a shell, or accepts a caller-selected
command/path. It receives an Exim queue id captured from Exim's SMTP `250 id=`
response, validates it, and reads a bounded tail of the configured main log.

The log may be readable directly after an administrator adds the desktop user
to the mail-log group. When it is not, an explicitly enabled account may try
one fixed non-interactive sudo command. No password prompt is ever attempted.
"""

import os
import re
import subprocess

from bridge import config as _cfg

EXIM_COMMAND = "/usr/sbin/exim4"
SUDO_COMMAND = "/usr/bin/sudo"
TAIL_COMMAND = "/usr/bin/tail"
# Keep this literal: the optional sudo invocation must never receive a caller-
# controlled path. Tests inject a runner, not an alternate privileged target.
MAIN_LOG_PATH = "/var/log/exim4/mainlog"
TAIL_LINES = 4000
TIMEOUT_SECONDS = 8
MAX_OUTPUT_BYTES = 512 * 1024
QUEUE_ID_RE = re.compile(r"^[A-Za-z0-9-]{6,64}$")


class EximStatusError(Exception):
    """Raised when read-only Exim status is unavailable."""


def extract_queue_id(response):
    """Return an Exim queue id from a successful SMTP response, if present."""
    text = response.decode("utf-8", "replace") if isinstance(response, bytes) else str(response or "")
    match = re.search(r"\bid=([A-Za-z0-9-]{6,64})\b", text, re.IGNORECASE)
    return match.group(1) if match else ""


def _run(args, runner=None):
    """Run a fixed argument array and return bounded text without a shell."""
    runner = runner or subprocess.run
    try:
        completed = runner(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise EximStatusError("Exim status command is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise EximStatusError("Exim status inspection timed out") from exc
    except OSError as exc:
        raise EximStatusError("Exim status inspection could not run: " + type(exc).__name__) from exc
    stdout = (completed.stdout or b"")[:MAX_OUTPUT_BYTES].decode("utf-8", "replace")
    stderr = (completed.stderr or b"")[:4096].decode("utf-8", "replace")
    return completed.returncode, stdout, stderr


def _read_mainlog(allow_sudo=False, runner=None):
    """Read only the last bounded mainlog lines, optionally through fixed sudo."""
    direct = [TAIL_COMMAND, "-n", str(TAIL_LINES), MAIN_LOG_PATH]
    code, stdout, stderr = _run(direct, runner)
    if code == 0:
        return stdout, "direct"
    if not allow_sudo:
        raise EximStatusError(
            "Exim mainlog is not readable. Enable status inspection only after granting Eva read-only log access."
        )
    sudo = [SUDO_COMMAND, "-n", TAIL_COMMAND, "-n", str(TAIL_LINES), MAIN_LOG_PATH]
    code, stdout, stderr = _run(sudo, runner)
    if code == 0:
        return stdout, "sudo"
    if "password" in stderr.lower() or "sudo" in stderr.lower():
        raise EximStatusError(
            "Exim status requires non-interactive access. Add the user to the mail-log group or configure a fixed NOPASSWD rule."
        )
    raise EximStatusError("Exim mainlog could not be read")


def _safe_detail(value):
    text = " ".join(str(value or "").split())
    text = re.sub(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", "<recipient>", text)
    text = re.sub(r"(?:https?://\S+|\b(?:token|password|secret|key)\s*[=:]\s*\S+)", "<redacted>", text, flags=re.I)
    return text[:240]


def inspect(queue_id, allow_sudo=False, runner=None):
    """Return Exim's latest transport state for one known queue id.

    `delivered` means Exim successfully handed every recorded recipient to the
    next SMTP hop. It is not a recipient inbox/read receipt. `unknown` means the
    bounded log window no longer includes this submission.
    """
    queue_id = str(queue_id or "").strip()
    if not QUEUE_ID_RE.fullmatch(queue_id):
        raise EximStatusError("Invalid Exim queue id")
    log_text, access = _read_mainlog(allow_sudo=allow_sudo, runner=runner)
    lines = [line for line in log_text.splitlines() if re.search(r"\b" + re.escape(queue_id) + r"\b", line)]
    delivered = []
    deferred = []
    failed = []
    completed = False
    for line in lines:
        if " => " in line:
            delivered.append(line)
        elif " == " in line:
            deferred.append(line)
        elif " ** " in line:
            failed.append(line)
        elif line.rstrip().endswith(" Completed"):
            completed = True
    partial = bool(delivered and (deferred or failed))
    if failed:
        detail = _safe_detail(failed[-1])
        if partial:
            detail = "One or more recipients were already handed off; Exim also reported: " + detail
        return {"queue_id": queue_id, "status": "failed", "access": access,
                "detail": detail, "completed": completed, "partial": partial}
    if deferred:
        detail = _safe_detail(deferred[-1])
        if partial:
            detail = "One or more recipients were already handed off; Exim also deferred: " + detail
        return {"queue_id": queue_id, "status": "deferred", "access": access,
                "detail": detail, "completed": completed, "partial": partial}
    if delivered and completed:
        return {"queue_id": queue_id, "status": "delivered", "access": access,
                "detail": "Exim handed the message to its next SMTP hop.", "completed": True, "partial": False}
    if delivered:
        return {"queue_id": queue_id, "status": "pending", "access": access,
                "detail": "Exim has a recorded delivery hop and is still processing the message.", "completed": completed, "partial": False}
    return {"queue_id": queue_id, "status": "unknown", "access": access,
            "detail": "No matching Exim status was found in the recent log window.", "completed": False, "partial": False}
