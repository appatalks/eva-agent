"""Bridge domain: acp_client."""

import json
import hashlib
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import stat
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from bridge import config as _cfg
from bridge import state as _st
from bridge.utils import _safe_child_environment
from bridge.kusto import _inject_kusto_token
from bridge.learning import get_consent as _get_learning_consent
from bridge.telemetry import _telemetry_emit, _verbose_debug_emit

_ACP_POOL_MAX = _cfg.ACP_POOL_MAX
_ACP_SESSION_MAX = _cfg.ACP_SESSION_MAX
_ACP_SESSION_MAX_PROMPTS = _cfg.ACP_SESSION_MAX_PROMPTS
_ACP_SESSION_IDLE_SECONDS = _cfg.ACP_SESSION_IDLE_SECONDS
_ARTIFACTS_DIR = _cfg.ARTIFACTS_DIR
_ACP_TOOL_PROFILES = _cfg.ACP_TOOL_PROFILES
_SECRET_MARKERS = ("TOKEN", "KEY", "SECRET", "PAT", "PASSWORD", "CREDENTIAL")
_SECRET_ARGUMENT_RE = re.compile(
    r"(?:^|[^A-Z0-9])(?:TOKEN|KEY|SECRET|PAT|PASSWORD|CREDENTIAL)(?:$|[^A-Z0-9])"
)
_WORKSPACE_READ_ONLY_COMMANDS = {"pwd", "ls", "git"}
_WORKSPACE_READ_ONLY_GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "rev-parse"}
_WORKSPACE_SENSITIVE_COMMANDS = {
    "sudo", "su", "doas", "pkexec", "rm", "shred", "mkfs", "fdisk", "parted", "dd",
    "mount", "umount", "chmod", "chown", "chgrp", "curl", "wget", "ssh", "scp", "sftp",
    "nc", "ncat", "telnet", "ftp", "rsync", "docker", "kubectl", "gh", "aws", "az",
    "gcloud", "npm", "npx", "pnpm", "yarn", "pip", "pip3", "uv", "poetry", "gem", "cargo",
}
_WORKSPACE_AUTONOMY_BLOCKED_EXECUTABLES = {
    "sudo", "su", "doas", "pkexec", "rm", "shred", "mkfs", "fdisk", "parted", "dd", "mount", "umount",
    "systemctl", "service", "launchctl",
}
_WORKSPACE_AUTONOMY_DESTRUCTIVE_PATTERN = re.compile(
    r"\b(?:rm|shred|mkfs|fdisk|parted|dd|mount|umount|sudo|su|doas|pkexec|"
    r"systemctl|service|launchctl|os\.remove|os\.unlink|shutil\.rmtree|remove-item)\b",
    re.IGNORECASE,
)
_WORKSPACE_SAFE_GIT_SUBCOMMANDS = {"add", "commit", "status", "diff", "log", "show", "rev-parse", "ls-files"}
_WORKSPACE_SAFE_PACKAGE_SUBCOMMANDS = {"test", "run", "lint", "check", "build"}
_GIT_CONTEXT_OPTIONS = {
    "--git-dir", "--work-tree", "--exec-path", "--namespace", "--config-env", "--config",
}
_GH_FILE_OPTIONS = {"--body-file", "--template", "--input"}
_WORKSPACE_SENSITIVE_PATH_RE = re.compile(
    r"(?:^|[/\s])(?:\.env(?:\.[A-Za-z0-9_.-]+)?|\.ssh|\.aws|\.azure|\.npmrc|\.pypirc|"
    r"\.netrc|\.git-credentials|id_rsa|id_ed25519|kubeconfig|service-account|hosts\.yml|"
    r"config\.json|config\.local\.(?:js|json))(?:[/\s]|$)",
    re.IGNORECASE,
)
_CODING_CREDENTIAL_RE = re.compile(
    r"(?:\b[A-Z][A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|PASSWORD|SECRET|CREDENTIALS?)\b|"
    r"(?:^|[/\s\"'])(?:[^/\s\"']*(?:credentials?|secrets?|tokens?)[^/\s\"']*\.[a-z0-9]+|"
    r"[^/\s\"']+\.(?:pem|p12|pfx))(?:[/\s\"']|$)|"
    r"\b(?:authorization\s*:|--(?:password|secret|token|api-key)\b))",
    re.IGNORECASE,
)
_CODING_DESTRUCTIVE_RE = re.compile(
    r"\b(?:rm|shred|mkfs|fdisk|parted|dd|sudo|su|doas|pkexec|"
    r"remove-item|os\.remove|os\.unlink|shutil\.rmtree)\b|"
    r"\bgit\s+(?:clean|reset)\b",
    re.IGNORECASE,
)
_CODING_REMOTE_WRITE_RE = re.compile(
    r"\b(?:git\s+push|gh\s+(?:api|issue\s+(?:create|comment|close|reopen|edit|delete)|"
    r"pr\s+(?:create|comment|close|reopen|edit|merge))|"
    r"(?:scp|sftp|ssh)\b|curl\b[^\n]*(?:--data(?:-binary|-raw|-urlencode)?\b|"
    r"--upload-file\b|-d(?:\s|$)|-T(?:\s|$)|(?:-X|--request)\s*(?:POST|PUT|PATCH|DELETE)\b))",
    re.IGNORECASE,
)
_GITHUB_AUTH_FAILURE_RE = re.compile(
    r"(?:does not have write access|write access (?:is )?denied|resource not accessible|bad credentials|"
    r"(?:authentication|authorization) (?:is )?(?:required|failed|rejected)|permission denied|"
    r"must authenticate|http (?:401|403))",
    re.IGNORECASE,
)
_GITHUB_TOOL_CONTEXT_RE = re.compile(r"(?:github|\bgh\b|repository|pull request|\bissue\b)", re.IGNORECASE)

# Mailbox access is never covered by standing consent or workspace autonomy.
# Reading a mailbox exposes far more than an ordinary file read, and sending is
# irreversible and reaches third parties, so both always need a live decision.
_MAIL_TOOL_NAME_RE = re.compile(
    r"(?:send_?e?mail|read_?e?mail|fetch_?e?mail|list_?messages|search_?messages|"
    r"mail_?folder|mailbox|(?<![a-z0-9])(?:imap|smtp)(?![a-z0-9]))",
    re.IGNORECASE,
)
_MAIL_PATH_RE = re.compile(
    r"/(?:me|users/[^/\s\"]+)/(?:messages|mailfolders|sendmail|mailboxsettings)\b",
    re.IGNORECASE,
)
_BACKGROUND_MAIL_INTENT_RE = re.compile(
    r"\b(?:send|read|fetch|list|search)\s+(?:(?:an?|the)\s+)?(?:e[- ]?mail|mailbox|messages?)\b",
    re.IGNORECASE,
)
_BACKGROUND_DESTRUCTIVE_TOOL_RE = re.compile(
    r"(?:^|[_./:-])(?:delete|remove|destroy|purge|wipe|erase|uninstall|shutdown|reboot|terminate)"
    r"(?:$|[_./:\s-])",
    re.IGNORECASE,
)
_BACKGROUND_PROTECTED_PATH_RE = re.compile(
    r"(?:^|[/\s=:])(?:\.git(?:[/\s]|$)|memory\.db(?:$|[-.\s/]))", re.IGNORECASE
)
_BACKGROUND_WEB_SCHEMAS = {
    "web_search": ({"query"}, {"query", "max_results"}),
    "web_search_news": ({"query"}, {"query", "max_results"}),
    "web_fetch": ({"url"}, {"url", "max_length", "method"}),
    "weather_current": ({"location"}, {"location"}),
    "stock_quote": ({"query"}, {"query"}),
}


def _record_autonomy_block(source, tool_kind, reason):
    try:
        from bridge.mission_log import record_permission_block
        record_permission_block(source, tool_kind, reason)
    except Exception:
        _telemetry_emit("mission_log", result="block_record_failed")


def _mail_scoped_tool_call(tool_call):
    """Return True when a tool call reads or sends mail on any backend."""
    tool_call = tool_call if isinstance(tool_call, dict) else {}
    for value in (tool_call.get("title"), tool_call.get("name")):
        if isinstance(value, str) and _MAIL_TOOL_NAME_RE.search(value):
            return True
    raw_input = tool_call.get("rawInput")
    if isinstance(raw_input, dict):
        try:
            serialized = json.dumps(raw_input, separators=(",", ":"))[:4000]
        except (TypeError, ValueError):
            serialized = ""
        if _MAIL_PATH_RE.search(serialized) or _MAIL_TOOL_NAME_RE.search(serialized):
            return True
    return False


def _github_tool_update_text(value, details=None):
    """Collect bounded strings from an ACP tool result without recording them."""
    details = details if details is not None else []
    if len(details) >= 32:
        return details
    if isinstance(value, str):
        details.append(value[:2000])
    elif isinstance(value, dict):
        for item in value.values():
            _github_tool_update_text(item, details)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _github_tool_update_text(item, details)
    return details


def _github_authorization_needed(update):
    """Return true only for an explicit GitHub tool authorization failure."""
    if not isinstance(update, dict):
        return False
    text = "\n".join(_github_tool_update_text(update))
    return bool(_GITHUB_TOOL_CONTEXT_RE.search(text) and _GITHUB_AUTH_FAILURE_RE.search(text))


def _tool_call_command(tool_call):
    tool_call = tool_call if isinstance(tool_call, dict) else {}
    raw_input = tool_call.get("rawInput")
    if isinstance(raw_input, dict):
        candidate = raw_input.get("command") or raw_input.get("cmd") or ""
        arguments = raw_input.get("args")
        if isinstance(candidate, str):
            if isinstance(arguments, list) and all(isinstance(argument, str) for argument in arguments):
                return shlex.join([candidate, *arguments])
            return candidate
    return ""


def _command_summary(tool_call):
    """Return a bounded redacted descriptor only for structured command input."""
    tool_call = tool_call if isinstance(tool_call, dict) else {}
    raw_input = tool_call.get("rawInput")
    if not isinstance(raw_input, dict):
        return ""
    command = raw_input.get("command") or raw_input.get("cmd") or ""
    arguments = raw_input.get("args")
    if not isinstance(command, str) or not isinstance(arguments, list) or not all(isinstance(argument, str) for argument in arguments):
        return ""
    if re.search(r"[\x00-\x1f\x7f]", command) or any(re.search(r"[\x00-\x1f\x7f]", argument) for argument in arguments):
        return ""
    safe = [command]
    redact_next = False
    for argument in arguments:
        upper = argument.upper()
        sensitive = redact_next or any(marker in upper for marker in _SECRET_MARKERS)
        safe.append("<redacted>" if sensitive else argument)
        redact_next = argument.lower() in {"--token", "--password", "--secret", "--api-key", "--key"}
    return shlex.join(safe)[:300]


def _workspace_local_path(value, cwd):
    if not value or value.startswith(("-", ":")):
        return True
    if value.startswith("~"):
        return False
    root = os.path.realpath(cwd or os.getcwd())
    candidate = os.path.realpath(value) if os.path.isabs(value) else os.path.realpath(os.path.join(root, value))
    try:
        return os.path.commonpath([root, candidate]) == root
    except ValueError:
        return False


def _workspace_edit_targets(tool_call):
    """Return explicitly named edit targets from ACP raw input, locations, or diffs."""
    targets = []
    raw_input = tool_call.get("rawInput") if isinstance(tool_call, dict) else None
    if isinstance(raw_input, dict):
        for key in ("path", "filePath", "file_path", "targetPath", "target_path"):
            if isinstance(raw_input.get(key), str):
                targets.append(raw_input[key])
    for location in tool_call.get("locations", []) if isinstance(tool_call, dict) else []:
        if isinstance(location, dict) and isinstance(location.get("path"), str):
            targets.append(location["path"])
    for item in tool_call.get("content", []) if isinstance(tool_call, dict) else []:
        if isinstance(item, dict) and item.get("type") == "diff" and isinstance(item.get("path"), str):
            targets.append(item["path"])
    return list(dict.fromkeys(targets))


def _workspace_edit_target_is_local(tool_call, cwd=None):
    """Return true only when every explicit edit target is inside the workspace."""
    targets = _workspace_edit_targets(tool_call)
    return bool(targets) and all(_workspace_local_path(target, cwd) for target in targets)


def _workspace_edit_target_is_protected(tool_call):
    targets = _workspace_edit_targets(tool_call)
    return not targets or any(_workspace_argument_is_protected(target) for target in targets)


def _workspace_argument_is_protected(value):
    candidates = [str(value or "")]
    if "=" in candidates[0]:
        candidates.append(candidates[0].split("=", 1)[1])
    return any(_WORKSPACE_SENSITIVE_PATH_RE.search(candidate.lstrip("@")) for candidate in candidates)


def _workspace_argument_mentions_secret(value):
    return bool(_SECRET_ARGUMENT_RE.search(str(value or "").upper()))


def _workspace_gh_path_category(arguments, cwd=None):
    """Check only gh options whose values are filesystem paths."""
    for index, argument in enumerate(arguments):
        option, separator, assigned = argument.partition("=")
        if option not in _GH_FILE_OPTIONS:
            continue
        value = assigned if separator else (arguments[index + 1] if index + 1 < len(arguments) else "")
        if not value or value == "-":
            continue
        if _workspace_argument_is_protected(value):
            return "secret_or_sensitive_path"
        if not _workspace_local_path(value, cwd):
            return "outside_workspace"
    return ""


def _workspace_git_has_context_option(arguments):
    """Reject Git options that can redirect repository/configuration context."""
    before_subcommand = True
    for argument in arguments:
        if argument == "--":
            break
        option = argument.split("=", 1)[0]
        if option in _GIT_CONTEXT_OPTIONS or argument.startswith("-C") or argument == "-c" or argument.startswith("-c="):
            return True
        if before_subcommand:
            if argument.startswith("-c"):
                return True
            if not argument.startswith("-"):
                before_subcommand = False
    return False


def _workspace_read_only_execute(tool_call, cwd=None):
    """Allow only transparent, non-mutating workspace inspection commands."""
    command = _tool_call_command(tool_call)
    if not command or re.search(r"[\n\r;|&><`]|\$\(|\$\{", command):
        return False
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return False
    if not parts or parts[0] not in _WORKSPACE_READ_ONLY_COMMANDS:
        return False
    if parts[0] == "pwd":
        return all(part in {"-L", "-P"} for part in parts[1:])
    if parts[0] == "ls":
        allowed_ls_flags = re.compile(r"^-[aAlh1dF]+$")
        return all(allowed_ls_flags.fullmatch(part) or _workspace_local_path(part, cwd) for part in parts[1:])
    if parts[0] == "git":
        if len(parts) < 2 or parts[1] not in _WORKSPACE_READ_ONLY_GIT_SUBCOMMANDS:
            return False
        forbidden_git = {"--output", "--ext-diff", "--textconv", "--no-index"}
        if _workspace_git_has_context_option(parts[1:]) or any(
            part.split("=", 1)[0] in forbidden_git for part in parts[2:]
        ):
            return False
        return all(_workspace_local_path(part, cwd) for part in parts[2:])
    return False


def _workspace_execute_category(tool_call, cwd=None):
    """Classify workspace execution without retaining or emitting command content."""
    command = _tool_call_command(tool_call)
    if not command:
        return "missing_command"
    if re.search(r"[\n\r;|&><`] |\$\(|\$\{", command, re.VERBOSE):
        return "shell_composition"
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return "parse_error"
    if not parts:
        return "missing_command"
    executable = os.path.basename(parts[0]).lower()
    arguments = parts[1:]
    if executable in {"node", "nodejs", "python", "python3", "ruby", "perl", "php"} and any(
        argument in {"-c", "-e", "--eval"} for argument in arguments
    ):
        return "inline_script"
    if executable == "gh":
        gh_path_category = _workspace_gh_path_category(arguments, cwd)
        if gh_path_category:
            return gh_path_category
    for argument in arguments:
        if _workspace_argument_mentions_secret(argument) or _workspace_argument_is_protected(argument):
            return "secret_or_sensitive_path"
        if executable != "gh" and not argument.startswith("-") and not _workspace_local_path(argument, cwd):
            return "outside_workspace"
    if executable in _WORKSPACE_SENSITIVE_COMMANDS:
        if executable not in {"npm", "npx", "pnpm", "yarn", "pip", "pip3", "uv", "poetry", "gem", "cargo"}:
            return "sensitive_executable"
        if any(argument in {"install", "add", "remove", "uninstall", "publish", "login", "logout"} for argument in arguments):
            return "package_or_auth_mutation"
    if executable in {"bash", "sh", "zsh", "fish", "env", "eval", "source"}:
        return "shell_interpreter"
    if executable == "git":
        if len(arguments) < 1 or _workspace_git_has_context_option(arguments):
            return "git_configuration_override"
        subcommand = next((argument for argument in arguments if not argument.startswith("-")), "")
        if subcommand not in _WORKSPACE_SAFE_GIT_SUBCOMMANDS:
            return "git_remote_or_destructive"
        if any(argument == "--hard" or argument.startswith("--force") for argument in arguments[1:]):
            return "git_force"
    if executable == "git":
        return "trusted_local"
    if executable in {"npm", "pnpm", "yarn"}:
        subcommand = next((argument for argument in arguments if not argument.startswith("-")), "")
        return "trusted_local" if subcommand in _WORKSPACE_SAFE_PACKAGE_SUBCOMMANDS else "approval_required"
    if executable in {"node", "nodejs", "python", "python3"}:
        return "trusted_local" if any(
            not argument.startswith("-") and _workspace_local_path(argument, cwd)
            for argument in arguments
        ) else "approval_required"
    return "approval_required"


def _workspace_sensitive_execute(tool_call, cwd=None):
    """Return true when a workspace command needs an explicit user decision."""
    return _workspace_execute_category(tool_call, cwd) != "trusted_local"


def _workspace_autonomy_block_reason(tool_call, cwd=None):
    """Return the hard safety reason that prevents autonomous execution."""
    category = _workspace_execute_category(tool_call, cwd)
    if category == "secret_or_sensitive_path":
        return "protected_path"
    command = _tool_call_command(tool_call)
    if not command or category in {"missing_command", "parse_error", "git_configuration_override"}:
        return "opaque_execution"
    if _workspace_argument_mentions_secret(command) or _workspace_argument_is_protected(command):
        return "protected_path"
    if _WORKSPACE_AUTONOMY_DESTRUCTIVE_PATTERN.search(command):
        return "destructive_execution"
    if category in {"outside_workspace", "inline_script", "shell_interpreter", "shell_composition"}:
        return category
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return "opaque_execution"
    if not parts:
        return "opaque_execution"
    executable = os.path.basename(parts[0]).lower()
    arguments = parts[1:]
    if executable in _WORKSPACE_AUTONOMY_BLOCKED_EXECUTABLES:
        return "destructive_execution"
    if executable == "git":
        subcommand = next((argument for argument in arguments if not argument.startswith("-")), "")
        if subcommand in {"clean", "reset"} or any(
            argument == "--hard" or argument.startswith("--force") for argument in arguments
        ):
            return "destructive_execution"
    return ""


def _coding_autonomy_approval_reason(tool_call, tool_kind, mail_scoped=False):
    if mail_scoped:
        return "mailbox access"
    targets = _workspace_edit_targets(tool_call)
    if any(_workspace_argument_is_protected(target) or _CODING_CREDENTIAL_RE.search(target) for target in targets):
        return "credential or protected file access"
    if tool_kind == "execute":
        command = _tool_call_command(tool_call)
        if not command:
            return "unidentified command"
        if _WORKSPACE_SENSITIVE_PATH_RE.search(command) or _CODING_CREDENTIAL_RE.search(command):
            return "credential or protected file access"
        if _CODING_DESTRUCTIVE_RE.search(command):
            return "potentially destructive command"
        if _CODING_REMOTE_WRITE_RE.search(command):
            return "external change"
        return ""
    if tool_kind in {"delete", "other"}:
        return "unclassified or destructive action"
    if tool_kind in {"read", "edit", "search", "fetch", "think"}:
        if tool_kind in {"search", "fetch"}:
            remote_reason = _unverified_research_block_reason(tool_call)
            if remote_reason == "destructive_action":
                return "external change"
            if remote_reason == "protected_content":
                return "credential or protected content"
        raw = tool_call.get("rawInput")
        if isinstance(raw, dict):
            if any(
                str(raw.get(key) or "").lower() not in {"", "get", "head", "read", "view", "search", "list"}
                for key in ("method", "action", "operation")
            ):
                return "external change"
        return ""
    return "unclassified action"


def _coding_command_summary(tool_call):
    command = _tool_call_command(tool_call)
    if not command or len(command) > 8192 or re.search(r"[\x00-\x1f\x7f;|&<>`]|\$\(", command):
        return ""
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    if not parts:
        return ""
    from workspace_diff_guard import scan_text
    result = []
    redact_next = False
    sensitive_options = {"--password", "--secret", "--token", "--api-key", "--key", "-H", "--header"}
    for part in parts:
        if redact_next:
            result.append("<redacted>")
            redact_next = False
            continue
        option, separator, value = part.partition("=")
        if option in sensitive_options:
            result.append(option + "=<redacted>" if separator else option)
            redact_next = not separator
        elif re.search(r"(?i)(?:password|secret|token|api[_-]?key)=", part):
            result.append(option + "=<redacted>" if separator else "<redacted>")
        elif scan_text(part, "permission-request") or re.search(r"(?i)authorization\s*:", part):
            result.append("<redacted>")
        elif part.startswith(("https://", "http://")):
            parsed = urllib.parse.urlsplit(part)
            if parsed.username or parsed.password:
                return ""
            result.append(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
                          + ("?<redacted>" if parsed.query else ""))
        else:
            result.append(part)
    summary = shlex.join(result)
    return summary if len(summary) <= 8192 else ""


def _coding_action_summary(tool_call):
    from workspace_diff_guard import scan_text
    targets = _workspace_edit_targets(tool_call)
    if targets:
        if any(len(target) > 1024 or re.search(r"[\x00-\x1f\x7f]", target)
               or scan_text(target, "permission-request") for target in targets):
            return ""
        return "Paths: " + ", ".join(targets[:8])
    raw = tool_call.get("rawInput")
    if not isinstance(raw, dict):
        return ""
    url = raw.get("url") or raw.get("uri")
    if not isinstance(url, str) or len(url) > 2000 or scan_text(url, "permission-request"):
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        actions = []
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() in {"action", "method", "operation", "command", "op"}:
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
                    return ""
                actions.append(key.lower() + "=" + value)
        base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        method = str(raw.get("method") or "").upper()
        if method and not re.fullmatch(r"[A-Z]{1,12}", method):
            return ""
        return ("Method: " + method + "; " if method else "") + "URL: " + base + (
            "; " + ", ".join(actions[:8]) if actions else ""
        ) + ("; other query details hidden" if parsed.query else "")
    except ValueError:
        return ""


def _sandbox_copilot_executable(executable):
    resolved = os.path.realpath(executable)
    if os.path.basename(resolved) == "npm-loader.js":
        architecture = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine().lower())
        if not architecture:
            raise RuntimeError("Sandboxed ACP is unavailable on this CPU architecture.")
        resolved = os.path.realpath(os.path.join(
            os.path.dirname(resolved), "node_modules", "@github", "copilot-linux-" + architecture, "copilot"
        ))
    try:
        current = resolved
        while True:
            info = os.stat(current)
            if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise RuntimeError("The sandboxed Copilot executable is not trusted.")
            if current == resolved and (not stat.S_ISREG(info.st_mode) or not os.access(current, os.X_OK)):
                raise RuntimeError("The sandboxed Copilot executable is invalid.")
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        with open(resolved, "rb") as binary:
            if binary.read(4) != b"\x7fELF":
                raise RuntimeError("Sandboxed ACP requires a trusted native Copilot executable.")
    except OSError as error:
        raise RuntimeError("The sandboxed Copilot executable cannot be verified.") from error
    return resolved


def _background_autonomy_block_reason(tool_call, cwd=None):
    """Allow transparent commands while rejecting opaque or destructive execution."""
    reason = _workspace_autonomy_block_reason(tool_call, cwd)
    if reason:
        return reason
    command = _tool_call_command(tool_call)
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return "opaque_execution"
    if any(_workspace_argument_is_protected(part.lstrip(":@")) for part in parts):
        return "protected_path"
    if any(_BACKGROUND_PROTECTED_PATH_RE.search(part) for part in parts):
        return "protected_path"
    if parts and parts[0] == "ls" and any(
        _background_path_is_protected(part, cwd) for part in parts[1:] if not part.startswith("-")
    ):
        return "protected_path"
    if _workspace_read_only_execute(tool_call, cwd):
        return ""
    if _background_safe_local_read(parts, cwd):
        return ""
    allowed_gh = {
        ("issue", "list"), ("issue", "view"), ("issue", "create"), ("issue", "comment"),
        ("pr", "list"), ("pr", "view"), ("pr", "create"), ("pr", "comment"),
        ("repo", "view"), ("search", "issues"), ("search", "prs"),
    }
    if len(parts) < 3 or parts[0] != "gh" or tuple(parts[1:3]) not in allowed_gh:
        return "unclassified_execution"
    if any(arg.split("=", 1)[0] in _GH_FILE_OPTIONS for arg in parts[3:]):
        return "unclassified_execution"
    if any(arg in {"--web", "-w", "--editor", "-e"} or arg.startswith("--web=") for arg in parts[3:]):
        return "interactive_execution"
    from workspace_diff_guard import scan_text
    if scan_text(command, "background-command"):
        return "protected_content"
    return ""


def _background_path_is_protected(target, cwd):
    root = os.path.realpath(cwd or os.getcwd())
    candidate = os.path.abspath(os.path.join(root, target))
    if (_workspace_argument_is_protected(target) or _BACKGROUND_PROTECTED_PATH_RE.search(target)
            or _workspace_argument_is_protected(candidate) or _BACKGROUND_PROTECTED_PATH_RE.search(candidate)):
        return True
    try:
        relative = os.path.relpath(candidate, root)
    except ValueError:
        return True
    current = root
    for part in relative.split(os.sep):
        current = os.path.join(current, part)
        if os.path.islink(current):
            return True
    return False


def _background_safe_local_read(parts, cwd):
    if not parts or parts[0] not in {"cat", "head", "tail", "wc"}:
        return False
    flags = {
        "cat": {"-n", "-b", "-s", "--number", "--number-nonblank", "--squeeze-blank"},
        "head": set(),
        "tail": set(),
        "wc": {"-l", "-w", "-c", "-m", "-L", "--lines", "--words", "--bytes", "--chars"},
    }
    files = 0
    index = 1
    while index < len(parts):
        part = parts[index]
        if part == "-n" and parts[0] in {"head", "tail"}:
            index += 1
            if index >= len(parts) or len(parts[index]) > 4 or not parts[index].isdigit() or int(parts[index]) > 2000:
                return False
        elif part.startswith("--lines=") and parts[0] in {"head", "tail"}:
            count = part.partition("=")[2]
            if len(count) > 4 or not count.isdigit() or int(count) > 2000:
                return False
        elif part.startswith("-"):
            if part not in flags[parts[0]]:
                return False
        else:
            if not _workspace_local_path(part, cwd) or _background_path_is_protected(part, cwd):
                return False
            files += 1
        index += 1
    return files > 0


def _background_protected_targets(tool_call, cwd=None):
    return any(
        _background_path_is_protected(target, cwd)
        for target in _workspace_edit_targets(tool_call)
    )


def _background_decode_url_component(value):
    for _ in range(4):
        if re.search(r"%(?![0-9A-Fa-f]{2})", value):
            raise ValueError("Invalid URL encoding")
        decoded = urllib.parse.unquote(value, errors="strict")
        if decoded == value:
            return value
        value = decoded
    if "%" in value:
        raise ValueError("Ambiguous URL encoding")
    return value


def _background_edgar_read_action(parsed, path, key, value):
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() in {"sec.gov", "www.sec.gov"}
        and path == "/cgi-bin/browse-edgar"
        and key.lower() == "action"
        and value.lower() == "getcompany"
    )


def _background_web_tool_name(tool_call):
    name = ""
    for field in ("name", "title"):
        value = str(tool_call.get(field) or "").lower().strip()
        for candidate in sorted(_BACKGROUND_WEB_SCHEMAS, key=len, reverse=True):
            if value == candidate or value.endswith("__" + candidate):
                name = candidate
                break
        if name:
            break
    return name


def _background_remote_block_reason(tool_call):
    raw = tool_call.get("rawInput")
    if not isinstance(raw, dict):
        return "opaque_remote_action"
    name = _background_web_tool_name(tool_call)
    if not name:
        return "unclassified_remote_action"
    required, allowed = _BACKGROUND_WEB_SCHEMAS[name]
    if not required.issubset(raw) or set(raw) - allowed:
        return "unclassified_remote_action"
    for field in required:
        if not isinstance(raw[field], str) or not raw[field].strip() or len(raw[field]) > 2000:
            return "unclassified_remote_action"
    for field, maximum in (("max_results", 20), ("max_length", 50000)):
        if field in raw and (isinstance(raw[field], bool) or not isinstance(raw[field], int)
                             or not 1 <= raw[field] <= maximum):
            return "unclassified_remote_action"
    if name == "web_fetch":
        if str(raw.get("method", "GET")).upper() not in {"GET", "HEAD"}:
            return "destructive_action"
        try:
            parsed = urllib.parse.urlsplit(raw["url"])
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                return "unclassified_remote_action"
            path = _background_decode_url_component(parsed.path)
            if any(ord(char) < 32 or char == "\\" for char in path):
                return "unclassified_remote_action"
            if _BACKGROUND_DESTRUCTIVE_TOOL_RE.search(path):
                return "destructive_action"
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                key = _background_decode_url_component(key)
                value = _background_decode_url_component(value)
                if key.lower() in {"token", "api_key", "apikey", "password", "secret", "signature"}:
                    return "protected_content"
                if key.lower() in {"action", "method", "operation", "command", "op"}:
                    if (value.lower() not in {"get", "head", "read", "view", "search"}
                            and not _background_edgar_read_action(parsed, path, key, value)):
                        return "destructive_action"
        except ValueError:
            return "unclassified_remote_action"
    return ""


def _unverified_research_block_reason(tool_call):
    raw = tool_call.get("rawInput")
    if not isinstance(raw, dict) or not raw:
        return "opaque_remote_action"
    from workspace_diff_guard import scan_text
    try:
        serialized = json.dumps(raw, ensure_ascii=True)
    except (TypeError, ValueError):
        return "opaque_remote_action"
    if len(serialized) > 8192 or scan_text(serialized, "remote-request"):
        return "protected_content"

    def inspect(value, depth=0):
        if depth > 4:
            return "opaque_remote_action"
        if isinstance(value, dict):
            if len(value) > 32:
                return "opaque_remote_action"
            for key, item in value.items():
                key = str(key).lower()
                if re.search(r"(?:token|secret|password|credential|authorization|cookie|api.?key)", key):
                    return "protected_content"
                if key in {"method", "action", "operation", "command", "op"}:
                    if not isinstance(item, str) or item.lower() not in {"get", "head", "read", "view", "search", "list"}:
                        return "destructive_action"
                if key in {"url", "uri"} and isinstance(item, str):
                    try:
                        parsed = urllib.parse.urlsplit(item)
                        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                            return "unclassified_remote_action"
                        path = _background_decode_url_component(parsed.path)
                        if _BACKGROUND_DESTRUCTIVE_TOOL_RE.search(path):
                            return "destructive_action"
                        for query_key, query_value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                            query_key = _background_decode_url_component(query_key).lower()
                            query_value = _background_decode_url_component(query_value).lower()
                            if re.search(r"(?:token|secret|password|credential|authorization|api.?key)", query_key):
                                return "protected_content"
                            if query_key in {"action", "method", "operation", "command", "op"}:
                                if (query_value not in {"get", "head", "read", "view", "search", "list"}
                                        and not _background_edgar_read_action(parsed, path, query_key, query_value)):
                                    return "destructive_action"
                    except ValueError:
                        return "unclassified_remote_action"
                reason = inspect(item, depth + 1)
                if reason:
                    return reason
        elif isinstance(value, list):
            if len(value) > 32:
                return "opaque_remote_action"
            for item in value:
                reason = inspect(item, depth + 1)
                if reason:
                    return reason
        elif isinstance(value, str):
            if len(value) > 2000 or _WORKSPACE_SENSITIVE_PATH_RE.search(value) or _BACKGROUND_PROTECTED_PATH_RE.search(value):
                return "protected_content"
        elif value is not None and not isinstance(value, (bool, int, float)):
            return "opaque_remote_action"
        return ""

    return inspect(raw)


def _unverified_research_allowed():
    try:
        from bridge.mission_log import research_permission
        return research_permission()
    except Exception:
        return False


def _hidden_subprocess_options():
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def _normalize_tool_profile(profile, has_config=False):
    value = str(profile or ("broad" if has_config else "none")).strip().lower()
    return value if value in _ACP_TOOL_PROFILES else "broad"


def _acp_tool_profile_config(mcp_config, profile):
    """Return only the configured MCP servers allowed by a route profile."""
    source = mcp_config if isinstance(mcp_config, dict) else {}
    selected = _normalize_tool_profile(profile, bool(source))
    if selected == "none":
        return {}
    if selected == "broad":
        return dict(source)
    if selected == "github":
        names = {"github-mcp-server"}
    elif selected == "kusto":
        names = {"kusto-mcp-server"}
    else:
        names = {name for name in source if "web" in name.lower() or "search" in name.lower()}
    return {name: cfg for name, cfg in source.items() if name in names}


def _provider_config_fingerprint(provider_config):
    """Hash non-secret BYOK settings; never include endpoint credentials."""
    config = provider_config if isinstance(provider_config, dict) else {}
    safe = {}
    for key, value in sorted(config.items()):
        if value in (None, ""):
            continue
        upper = str(key).upper()
        if "KEY" in upper or "TOKEN" in upper:
            # Distinguish credentials for pool isolation without retaining
            # or emitting the credential itself.
            safe[str(key)] = "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()
        else:
            safe[str(key)] = str(value)
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16] if safe else "none"


def _acp_runtime_tool_config(profile, provider_config=None, no_tools=False):
    config = _acp_tool_profile_config(_st.configured_mcp_config, profile)
    if provider_config and not no_tools and profile in {"broad", "web"}:
        if not any("web" in name.lower() or "search" in name.lower() for name in config):
            path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web_search_mcp.py"
            )
            if os.path.isfile(path):
                config["eva-web-search"] = {"command": sys.executable, "args": [path]}
    return {} if no_tools else config


def _si_isolated_profile():
    """Create a controlled Copilot profile/workspace for request-scoped BYOK."""
    root = os.path.join(_cfg.EVA_CONFIG_DIR, "copilot-si-runtime")
    home = os.path.join(root, "home")
    cwd = os.path.join(root, "workspace")
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.makedirs(cwd, mode=0o700, exist_ok=True)
    config_path = os.path.join(home, "settings.json")
    if not os.path.exists(config_path):
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({
                "autoUpdate": False,
                "disableAllHooks": True,
                "customAgents": {"defaultLocalOnly": True},
            }, handle)
        os.chmod(config_path, 0o600)
    return home, cwd


def _acp_config_fingerprint(mcp_config):
    """Hash non-secret MCP shape and settings; secret values never enter the hash."""
    safe = []
    for name in sorted((mcp_config or {}).keys()):
        cfg = mcp_config.get(name) or {}
        env = {}
        for key in sorted((cfg.get("env") or {}).keys()):
            upper = str(key).upper()
            env[str(key)] = "<secret>" if str(key).startswith("_") or any(marker in upper for marker in _SECRET_MARKERS) else str(cfg["env"][key])
        safe.append({"name": name, "command": cfg.get("command", ""), "args": list(cfg.get("args") or []), "env": env})
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]

class ACPClient:
    """Manages the copilot --acp --stdio subprocess and ACP JSON-RPC protocol."""

    PROTOCOL_VERSION = 1  # ACP protocol major version

    def __init__(self, copilot_path="copilot", cwd=None, model=None, mcp_config=None,
                 reasoning_effort=None, tool_profile=None, no_tools=False,
                 provider_config=None, sandbox_image="", isolated_reasoning=False):
        self.copilot_path = copilot_path
        self.cwd = cwd or os.getcwd()
        self.model = model  # None = use CLI default
        self.no_tools = bool(no_tools)
        self.reasoning_effort = reasoning_effort  # None = use model default
        self.mcp_config = mcp_config or {}  # MCP servers config dict
        self.tool_profile = _normalize_tool_profile(tool_profile, bool(self.mcp_config))
        self.config_fingerprint = _acp_config_fingerprint(self.mcp_config)
        self.provider_config = dict(provider_config or {})
        self.sandbox_image = str(sandbox_image or "")
        self.isolated_reasoning = bool(isolated_reasoning)
        self._sandbox_workspace = self.cwd if self.sandbox_image else ""
        self._sandbox_profile = None
        self.provider_fingerprint = _provider_config_fingerprint(self.provider_config)
        self.process = None
        self._owns_process_group = False
        self._stop_lock = threading.RLock()
        self._stopped = False
        self._terminal_lock = threading.RLock()
        self.request_id = 0
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.prompt_lock = threading.Lock()
        self.pending = {}           # id -> {"event": Event, "result": None, "error": None}
        self.session_id = None
        self._conversation_sessions = {}
        self._conversation_session_order = []
        self.response_chunks = {}   # prompt_id -> accumulated text
        self.session_usage = {}     # session_id -> latest context usage metadata
        self._prompt_state_lock = threading.RLock()
        self._active_prompts = {}   # prompt_id -> session/callback/timing state
        self._cancel_requested = threading.Event()
        self._session_permission_modes = {}  # session_id -> last explicit prompt policy
        self._session_permission_mode_order = []
        self.reader_thread = None
        self.stderr_thread = None
        self.agent_info = {}
        self.alive = False
        self.active_requests = 0
        self.terminals = {}  # terminal_id -> {"process": Popen, "output": str}
        self.permission_lock = threading.RLock()
        self.pending_permissions = {}
        self._github_auth_notified = False

    # --- Lifecycle ---

    def start(self):
        """Spawn copilot subprocess, initialize ACP, create session."""
        with self._stop_lock:
            if self._cancel_requested.is_set() or _st.acp_shutdown.is_set():
                raise RuntimeError("ACP client startup was interrupted.")
        copilot_executable = self._validated_copilot_executable()
        restricted_profile = bool(self.sandbox_image or self.isolated_reasoning)
        if restricted_profile:
            copilot_executable = _sandbox_copilot_executable(copilot_executable)
        cmd = [copilot_executable, "--acp", "--stdio"]
        if restricted_profile:
            from bridge.security_sandbox import SandboxError, sandbox_preflight, validate_workspace
            try:
                if self.sandbox_image:
                    if self.mcp_config or self.no_tools or sandbox_preflight() != self.sandbox_image:
                        raise SandboxError("Sandboxed ACP cannot use host MCP servers or an unapproved image.")
                    validate_workspace(self._sandbox_workspace)
                elif not self.no_tools or self.mcp_config:
                    raise SandboxError("Isolated analysis cannot use tools or MCP servers.")
                version = subprocess.run(
                    [copilot_executable, "--version"], capture_output=True, text=True, timeout=10,
                    env=_safe_child_environment(), check=False, **_hidden_subprocess_options(),
                )
                if version.returncode or not re.search(r"\b1\.0\.109\b", version.stdout):
                    raise SandboxError("This Copilot CLI version has not been qualified for restricted security work.")
            except (SandboxError, OSError, subprocess.TimeoutExpired) as error:
                raise RuntimeError(str(error)) from error
            self._sandbox_profile = tempfile.TemporaryDirectory(prefix="eva-sandbox-acp-")
            sandbox_home = os.path.join(self._sandbox_profile.name, "home")
            self.cwd = os.path.join(self._sandbox_profile.name, "session")
            os.mkdir(sandbox_home, 0o700)
            os.mkdir(self.cwd, 0o700)
            settings_path = os.path.join(sandbox_home, "settings.json")
            descriptor = os.open(settings_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as settings:
                json.dump({"autoUpdate": False, "disableAllHooks": True,
                           "customAgents": {"defaultLocalOnly": True}}, settings)
            if self.sandbox_image:
                server_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                           "security_workspace_mcp.py")
                self.mcp_config = {"eva-sandbox": {
                    "command": sys.executable,
                    "args": [server_path, "--workspace", self._sandbox_workspace,
                             "--image", self.sandbox_image, "--config-dir", _cfg.EVA_CONFIG_DIR],
                    "tools": ["*"],
                }}
                cmd.extend(["--disable-builtin-mcps", "--no-custom-instructions",
                            "--disable-client-telemetry", "--available-tools", "eva-sandbox-execute"])
            else:
                cmd.append("--disable-client-telemetry")
        isolated_si = bool(self.provider_config)
        if isolated_si and not restricted_profile:
            cmd.extend(["--no-custom-instructions", "--disable-client-telemetry", "--disable-builtin-mcps"])
        if self.no_tools:
            cmd.extend(["--available-tools=__eva_no_tools__", "--disable-builtin-mcps", "--no-custom-instructions"])
        if self.model and not isolated_si:
            cmd.extend(["--model", self.model])
        if self.reasoning_effort:
            cmd.extend(["--reasoning-effort", self.reasoning_effort])
        # Pass MCP server config via --additional-mcp-config
        if self.mcp_config:
            mcp_json = json.dumps({"mcpServers": self.mcp_config})
            cmd.extend(["--additional-mcp-config", mcp_json])
        try:
            # Pass env vars from MCP config to the copilot process itself
            # (copilot spawns MCP servers as children, inheriting the env)
            os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
            process_env = _safe_child_environment()
            process_env.pop("EVA_BRIDGE_TOKEN", None)
            if restricted_profile:
                process_env["COPILOT_HOME"] = sandbox_home
                process_env["COPILOT_DISABLE_LOGIN_SHELL_ENV"] = "1"
                process_env["COPILOT_PLUGIN_DIR_ONLY"] = "true"
            if isolated_si:
                for key in list(process_env):
                    if key == "COPILOT_MODEL" or key.startswith("COPILOT_PROVIDER_"):
                        process_env.pop(key, None)
                if not restricted_profile:
                    copilot_home, isolated_cwd = _si_isolated_profile()
                    process_env["COPILOT_HOME"] = copilot_home
                    if not getattr(self, "workspace_run_id", ""):
                        self.cwd = isolated_cwd
            # BYOK provider settings are deliberately per-client.  Do not
            # inherit or mutate the user's global Copilot environment/config.
            for key, value in self.provider_config.items():
                if value not in (None, ""):
                    process_env[str(key)] = str(value)
            for srv_name, srv_cfg in self.mcp_config.items():
                for k, v in srv_cfg.get('env', {}).items():
                    # subprocess.Popen env requires all values to be strings
                    process_env[k] = str(v) if not isinstance(v, str) else v
            process_env["EVA_ARTIFACTS_DIR"] = _ARTIFACTS_DIR

            with self._stop_lock:
                if self._cancel_requested.is_set() or _st.acp_shutdown.is_set():
                    raise RuntimeError("ACP client startup was interrupted.")
                self._owns_process_group = bool(getattr(self, "workspace_run_id", "") or self.isolated_reasoning) and os.name != "nt"
                self.process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                    env=process_env,
                    cwd=self.cwd if isolated_si or restricted_profile else None,
                    start_new_session=self._owns_process_group,
                    **_hidden_subprocess_options(),
                )
                self.alive = True
        except FileNotFoundError:
            raise RuntimeError(
                f"Copilot CLI not found at '{self.copilot_path}'. "
                "Install it (https://github.com/github/copilot-cli) and authenticate with 'copilot auth login'."
            )
        except OSError as error:
            raise RuntimeError(
                f"Copilot CLI at '{self.copilot_path}' could not be started: {error}"
            ) from error

        # Start reader thread
        self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.reader_thread.start()

        # Start stderr reader (for debug logging)
        self.stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self.stderr_thread.start()

        # Initialize connection
        init_result = self._send_request("initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "clientCapabilities": {
                "terminal": False
            },
            "clientInfo": {
                "name": "eva-acp-bridge",
                "title": "Eva ACP Bridge",
                "version": "1.0.0"
            }
        }, timeout=30)

        if init_result and "error" not in init_result:
            self.agent_info = init_result.get("agentInfo", {})
            caps = init_result.get("agentCapabilities", {})
            print(f"[ACP] Connected to: {self.agent_info.get('name', 'unknown')} "
                  f"v{self.agent_info.get('version', '?')} "
                  f"(protocol v{init_result.get('protocolVersion', '?')})")
            print(f"[ACP] Capabilities: {json.dumps(caps, indent=2)}")
        else:
            print(f"[ACP] Warning: initialize returned: {init_result}")
        if restricted_profile and (
            not init_result or "error" in init_result or self.agent_info.get("version") != "1.0.109"
        ):
            self.stop()
            raise RuntimeError("Restricted security ACP requires the verified Copilot CLI version.")

        # Create session — pass MCP servers via ACP session/new if configured
        mcp_servers_for_session = []
        # Note: MCP servers are typically passed via CLI --additional-mcp-config
        # but we also pass them in session/new for full ACP compliance
        session_result = self._send_request("session/new", {
            "cwd": self.cwd,
            "mcpServers": mcp_servers_for_session
        }, timeout=30)

        if session_result and "sessionId" in session_result:
            self.session_id = session_result["sessionId"]
            self._remember_conversation_session("__default__", self.session_id)
            print(f"[ACP] Session created: {self.session_id}")
        else:
            print(f"[ACP] Warning: session/new returned: {session_result}")

    def _validated_copilot_executable(self):
        """Allow only the named CLI or a canonical absolute executable path."""
        configured = self.copilot_path
        if not isinstance(configured, (str, os.PathLike)):
            raise RuntimeError("Copilot CLI path must be 'copilot' or an absolute executable path.")
        configured = os.fspath(configured)
        if not isinstance(configured, str) or not configured or "\x00" in configured:
            raise RuntimeError("Copilot CLI path is invalid.")
        if configured == "copilot":
            resolved = shutil.which(configured)
            if not resolved:
                raise RuntimeError(
                    "Copilot CLI 'copilot' was not found on PATH. "
                    "Install it (https://github.com/github/copilot-cli) and authenticate with 'copilot auth login'."
                )
            resolved = os.path.realpath(resolved)
        else:
            if not os.path.isabs(configured):
                raise RuntimeError("Copilot CLI path must be the bare 'copilot' name or an absolute executable path.")
            resolved = os.path.realpath(configured)
        if not os.path.isfile(resolved) or not os.path.isdir(os.path.dirname(resolved)):
            raise RuntimeError(f"Copilot CLI path is not a regular file: '{configured}'.")
        if os.name != "nt" and not os.access(resolved, os.X_OK):
            raise RuntimeError(f"Copilot CLI path is not executable: '{configured}'.")
        return resolved

    def stop(self):
        """Stop this client's owned processes before releasing its requests."""
        with self._stop_lock:
            if self._stopped:
                return
            self.alive = False
            self._cancel_requested.set()
            with self._terminal_lock:
                processes = [(term["process"], term.get("owns_process_group", False)) for term in self.terminals.values()]
                if self.process is not None:
                    processes.append((self.process, self._owns_process_group))
                if processes:
                    with ThreadPoolExecutor(max_workers=min(len(processes), 16)) as pool:
                        futures = [pool.submit(self._terminate_owned_process, proc, group) for proc, group in processes]
                        for future in futures:
                            future.result()
                self.terminals.clear()
            if self.process is not None:
                if self.process.stdin and not self.process.stdin.closed:
                    self.process.stdin.close()
                if self.isolated_reasoning:
                    for reader in (self.reader_thread, self.stderr_thread):
                        if reader and reader is not threading.current_thread():
                            reader.join(timeout=2)
                    for stream in (self.process.stdout, self.process.stderr):
                        if stream and not stream.closed:
                            stream.close()
            if self._sandbox_profile is not None:
                self._sandbox_profile.cleanup()
                self._sandbox_profile = None
            with self.permission_lock:
                self.pending_permissions.clear()
            self._stopped = True
            for entry in list(self.pending.values()):
                entry["error"] = "ACP client stopped."
                entry["event"].set()

    @staticmethod
    def _terminate_owned_process(proc, owns_group=False):
        if os.name == "nt":
            if proc.poll() is None:
                result = subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    **_hidden_subprocess_options(),
                )
                if result.returncode and proc.poll() is None:
                    raise RuntimeError("Agent process tree could not be stopped.")
        elif owns_group:
            # A reaped leader with a live PID now belongs to somebody else.
            if proc.poll() is not None:
                try:
                    os.getpgid(proc.pid)
                except ProcessLookupError:
                    pass
                else:
                    return
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            if proc.poll() is not None:
                try:
                    os.getpgid(proc.pid)
                except ProcessLookupError:
                    pass
                else:
                    return
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired as error:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise RuntimeError("Agent process did not stop.") from error

    def cancel(self):
        """Interrupt this client's prompts and prevent further prompt dispatch."""
        self._cancel_requested.set()
        with self._prompt_state_lock:
            sessions = {state["session_id"] for state in self._active_prompts.values()}
            for state in self._active_prompts.values():
                state["permission_cancelled"] = True
                state["permission_reason"] = "user_interrupted"
        notified = all(self._send_notification("session/cancel", {"sessionId": session_id}) for session_id in sessions)
        try:
            self.stop()
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("Agent-owned processes could not be stopped.") from error
        if not notified:
            print("[ACP] Cancellation notification unavailable; owned processes were stopped directly.")

    # --- JSON-RPC Communication ---

    def _next_id(self):
        with self.lock:
            self.request_id += 1
            return self.request_id

    def _send_request(self, method, params, timeout=120):
        """Send a JSON-RPC request and wait for the response."""
        rid = self._next_id()
        event = threading.Event()
        started_at = time.monotonic()
        entry = {"event": event, "result": None, "error": None}
        if method == "session/prompt":
            entry.update(session_id=params.get("sessionId"), last_activity_at=started_at)
        self.pending[rid] = entry

        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params
        }) + "\n"

        try:
            with self.write_lock:
                if self._cancel_requested.is_set():
                    self.pending.pop(rid, None)
                    return {"error": "ACP prompt was interrupted."}
                self.process.stdin.write(msg.encode("utf-8"))
                self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            self.pending.pop(rid, None)
            return {"error": f"Copilot process pipe error: {e}"}

        # Prompt updates renew the idle budget, but never the total time limit.
        hard_deadline = started_at + timeout * (3 if method == "session/prompt" else 1)
        while True:
            remaining = min(
                hard_deadline,
                entry.get("last_activity_at", started_at) + timeout,
            ) - time.monotonic()
            if event.is_set() or (remaining > 0 and event.wait(timeout=remaining)):
                break
            if time.monotonic() >= min(
                hard_deadline, entry.get("last_activity_at", started_at) + timeout
            ):
                if method == "session/prompt":
                    notified = self._send_notification("session/cancel", {"sessionId": params.get("sessionId")})
                    if not notified or not event.wait(timeout=5):
                        # Do not let an abandoned turn emit into a later prompt.
                        self.stop()
                self.pending.pop(rid, None)
                return {
                    "error": "ACP request timed out before completion. Partial output is not a completed answer.",
                    "stop_reason": "timeout",
                }

        entry = self.pending.pop(rid, {})
        if entry.get("error"):
            return {"error": entry["error"]}
        result = entry.get("result")
        if method == "session/prompt" and (
            not isinstance(result, dict) or not result.get("stopReason")
        ):
            return {"error": "ACP prompt ended without a completion receipt."}
        return result

    def _send_response(self, rid, result):
        """Send a JSON-RPC response (for server-initiated requests like requestPermission)."""
        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "result": result
        }) + "\n"
        try:
            with self.write_lock:
                self.process.stdin.write(msg.encode("utf-8"))
                self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _send_rpc_error(self, rid, code, message):
        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": int(code), "message": str(message)[:160]},
        }) + "\n"
        try:
            with self.write_lock:
                self.process.stdin.write(msg.encode("utf-8"))
                self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def _send_notification(self, method, params):
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
        try:
            with self.write_lock:
                self.process.stdin.write(msg.encode("utf-8"))
                self.process.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    # --- Reader Loop ---

    def _read_loop(self):
        """Continuously read NDJSON lines from copilot stdout."""
        while self.alive:
            try:
                line = self.process.stdout.readline()
                if not line:
                    print("[ACP] Copilot stdout closed")
                    self.alive = False
                    # Unblock any pending requests
                    for rid in list(self.pending):
                        self.pending[rid]["error"] = "Copilot process exited"
                        self.pending[rid]["event"].set()
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    self._handle_message(msg)
                except json.JSONDecodeError:
                    print(f"[ACP] Non-JSON line: {line[:200]}")
            except Exception as e:
                print(f"[ACP] Reader error: {e}")
                break

    def _stderr_loop(self):
        """Read copilot stderr for debug output."""
        while self.alive:
            try:
                line = self.process.stderr.readline()
                if not line:
                    break
                print(f"[Copilot stderr] {line.decode('utf-8', errors='replace').rstrip()}")
            except Exception:
                break

    def _handle_message(self, msg):
        """Route incoming JSON-RPC messages."""
        # Response to our request
        if "id" in msg and "result" in msg:
            rid = msg["id"]
            if rid in self.pending:
                self.pending[rid]["result"] = msg["result"]
                self.pending[rid]["event"].set()
            return

        # Error response to our request
        if "id" in msg and "error" in msg:
            rid = msg["id"]
            if rid in self.pending:
                self.pending[rid]["error"] = msg["error"]
                self.pending[rid]["event"].set()
            return

        # Notification: session/update
        if msg.get("method") == "session/update":
            self._handle_session_update(msg.get("params", {}))
            return

        # Server-initiated request: session/request_permission
        if "id" in msg and msg.get("method") == "session/request_permission":
            self._handle_permission_request(msg["id"], msg.get("params", {}))
            return

        # Server-initiated requests for terminal
        if "id" in msg and msg.get("method", "").startswith("terminal/"):
            self._send_response(msg["id"], {
                "error": {"code": -32601, "message": "Terminal capability is disabled by Eva permission policy"}
            })
            return

        # Server-initiated requests for fs (decline)
        if "id" in msg and msg.get("method", "").startswith("fs/"):
            print(f"[ACP] Declining capability request: {msg.get('method')}")
            self._send_response(msg["id"], {
                "error": {"code": -32601, "message": "Method not supported by bridge"}
            })
            return

        # Unknown message
        if "id" in msg and "method" in msg:
            # Unknown server request — respond with error
            print(f"[ACP] Unknown server request: {msg.get('method')}")
            self._send_response(msg["id"], {
                "error": {"code": -32601, "message": "Not implemented"}
            })

    def _handle_session_update(self, params):
        """Accumulate text from agent_message_chunk updates."""
        update = params.get("update", {})
        update_type = update.get("sessionUpdate", "")
        if update_type in {
            "agent_message_chunk", "agent_thought_chunk", "tool_call",
            "tool_call_update", "plan", "usage_update",
        }:
            session_id = params.get("sessionId") or params.get("session_id")
            candidates = [
                entry for entry in list(self.pending.values())
                if "last_activity_at" in entry and (not session_id or entry["session_id"] == session_id)
            ]
            if len(candidates) == 1:
                candidates[0]["last_activity_at"] = time.monotonic()

        if update_type == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text":
                text = content.get("text", "")
                if not text:
                    return
                # ACP emits tool-filter diagnostics as assistant text.
                if self.no_tools and text.startswith((
                    "Info: Disabled tools:",
                    'Info: Unknown tool name in the tool allowlist: "__eva_no_tools__"',
                )):
                    return
                session_id = params.get("sessionId") or params.get("session_id")
                callback = None
                with self._prompt_state_lock:
                    candidates = [
                        (pid, state) for pid, state in self._active_prompts.items()
                        if not session_id or state["session_id"] == session_id
                    ]
                    if len(candidates) != 1:
                        return
                    pid, state = candidates[0]
                    self.response_chunks[pid] = self.response_chunks.get(pid, "") + text
                    state["chunk_count"] += 1
                    if state["first_chunk_at"] is None:
                        state["first_chunk_at"] = time.perf_counter()
                    callback = state.get("on_chunk")
                # The reader remains the ordering authority, but user callbacks
                # never run while the prompt registry lock is held.
                if callable(callback):
                    try:
                        callback(text)
                    except Exception as callback_error:
                        print(f"[ACP] Chunk callback failed: {callback_error}")

        elif update_type == "agent_thought_chunk":
            content = update.get("content", {})
            text = content.get("text", "") if isinstance(content, dict) and content.get("type") == "text" else ""
            if not isinstance(text, str) or not text:
                return
            session_id = params.get("sessionId") or params.get("session_id")
            callback = None
            with self._prompt_state_lock:
                candidates = [
                    state for state in self._active_prompts.values()
                    if not session_id or state["session_id"] == session_id
                ]
                if len(candidates) != 1 or not candidates[0].get("include_reasoning"):
                    return
                state = candidates[0]
                text = text[:max(0, 65536 - len(state["reasoning_content"]))]
                state["reasoning_content"] += text
                callback = state.get("on_reasoning")
            if text and callable(callback):
                try:
                    callback(text)
                except Exception:
                    pass

        elif update_type == "plan":
            entries = update.get("entries", [])
            if entries:
                print(f"[ACP] Agent plan received: entries={len(entries)}")
                self._dispatch_prompt_event(params, {
                    "kind": "plan",
                    "label": "Planning next steps",
                })

        elif update_type in ("tool_call", "tool_call_update"):
            status = update.get("status", "")
            kind = str(update.get("kind") or "other")[:32]
            if status:
                print(f"[ACP] Tool update: kind={kind} status={str(status)[:24]}")
                self._dispatch_prompt_event(params, {
                    "kind": "tool",
                    "label": "Using " + kind.replace("_", " ") + " (" + str(status)[:24] + ")",
                })
            if not self._github_auth_notified and _github_authorization_needed(update):
                self._github_auth_notified = True
                from bridge.alerts import _notify_enqueue
                _notify_enqueue(
                    "GitHub authorization needed",
                    "Eva needs GitHub write access to continue the requested work. Starting device authorization now; complete the GitHub prompt when it appears.",
                    "github-auth-needed", 0.95, ["chat", "voice"],
                )

        elif update_type == "usage_update":
            session_id = str(params.get("sessionId") or params.get("session_id") or "")[:120]
            used = update.get("used")
            size = update.get("size")
            if not isinstance(used, int) or isinstance(used, bool) or used < 0:
                return
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                return
            usage = {
                "used": used,
                "size": size,
                "percent": round(used / size * 100.0, 2),
            }
            cost = update.get("cost")
            if isinstance(cost, dict) and isinstance(cost.get("amount"), (int, float)):
                usage["cost_amount"] = round(float(cost["amount"]), 6)
                usage["cost_currency"] = str(cost.get("currency") or "")[:8]
            self.session_usage[session_id] = usage
            _telemetry_emit("acp_usage", model=self.model or "default", **usage)

    def _dispatch_prompt_event(self, params, event):
        """Deliver a sanitized lifecycle event to the matching active prompt."""
        session_id = params.get("sessionId") or params.get("session_id")
        callback = None
        with self._prompt_state_lock:
            candidates = [
                state for state in self._active_prompts.values()
                if not session_id or state["session_id"] == session_id
            ]
            if len(candidates) == 1:
                callback = candidates[0].get("on_event")
        if callable(callback):
            try:
                callback(event)
            except Exception as callback_error:
                print(f"[ACP] Event callback failed: {callback_error}")

    def _handle_permission_request(self, rpc_id, params):
        params = params if isinstance(params, dict) else {}
        tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        options = []
        for option in params.get("options", []) if isinstance(params.get("options"), list) else []:
            if not isinstance(option, dict) or not option.get("optionId"):
                continue
            options.append({
                "option_id": str(option["optionId"])[:120],
                "kind": str(option.get("kind") or "")[:32],
            })
        tool_kind = str(tool_call.get("kind") or "other")[:32]
        session_id = str(params.get("sessionId") or "")[:120]
        with self._prompt_state_lock:
            prompt_states = [
                state for state in self._active_prompts.values()
                if state.get("session_id") == session_id
            ]
            remembered_mode = self._session_permission_modes.get(session_id, "interactive")
            background_context = (
                remembered_mode == "background_auto"
                or any(state.get("permission_mode") == "background_auto" for state in prompt_states)
                or (not session_id and any(
                    state.get("permission_mode") == "background_auto" for state in self._active_prompts.values()
                ))
            )
        if background_context and (not session_id or len(prompt_states) != 1):
            reject_option = next((option for option in options if option["kind"] == "reject_once"), None)
            reject_option = reject_option or next((option for option in options if option["kind"] == "reject_always"), None)
            if reject_option:
                self._send_response(rpc_id, {
                    "outcome": {"outcome": "selected", "optionId": reject_option["option_id"]}
                })
            else:
                self._send_response(rpc_id, {"outcome": {"outcome": "cancelled"}})
            _telemetry_emit("acp_permission", decision="background-autonomy-stale",
                            tool_kind=tool_kind, option_count=len(options))
            return
        permission_mode = prompt_states[0].get("permission_mode", "interactive") \
            if len(prompt_states) == 1 else remembered_mode
        sandbox_manual = False
        if self.sandbox_image:
            raw = tool_call.get("rawInput")
            valid = (
                bool(session_id) and len(prompt_states) == 1
                and permission_mode in {"sandbox_auto", "workspace_write"}
                and tool_kind == "other" and tool_call.get("title") == "execute"
                and isinstance(raw, dict) and set(raw) <= {"command", "timeout_seconds"}
                and isinstance(raw.get("command"), str) and bool(raw["command"].strip())
                and len(raw["command"]) <= 8192
                and ("timeout_seconds" not in raw or (
                    isinstance(raw["timeout_seconds"], int) and not isinstance(raw["timeout_seconds"], bool)
                    and 1 <= raw["timeout_seconds"] <= 300
                ))
            )
            allow_once = next((option for option in options if option["kind"] == "allow_once"), None)
            if valid and permission_mode == "sandbox_auto" and allow_once:
                self._send_response(rpc_id, {
                    "outcome": {"outcome": "selected", "optionId": allow_once["option_id"]}
                })
                _telemetry_emit("acp_permission", decision="sandbox-autonomy-approve",
                                tool_kind="execute", option_count=len(options))
                return
            if not valid:
                reject = next((option for option in options if option["kind"] in {"reject_once", "reject_always"}), None)
                self._send_response(rpc_id, {
                    "outcome": {"outcome": "selected", "optionId": reject["option_id"]} if reject
                    else {"outcome": "cancelled"}
                })
                _telemetry_emit("acp_permission", decision="sandbox-autonomy-reject",
                                tool_kind=tool_kind, option_count=len(options))
                return
            sandbox_manual = True
            tool_kind = "execute"
        if self.no_tools or permission_mode == "automation_no_tools":
            reject_option = next((option for option in options if option["kind"] == "reject_once"), None)
            reject_option = reject_option or next(
                (option for option in options if option["kind"] == "reject_always"), None
            )
            if reject_option:
                self._send_response(rpc_id, {
                    "outcome": {"outcome": "selected", "optionId": reject_option["option_id"]}
                })
            else:
                self._send_rpc_error(rpc_id, -32602, "Automation vision does not authorize tools")
            _telemetry_emit("acp_permission", decision="automation-no-tools-deny",
                            tool_kind=tool_kind, option_count=len(options))
            return
        workspace_mode = permission_mode == "workspace_auto"
        background_mode = permission_mode == "background_auto"
        coding_mode = permission_mode == "coding_auto"
        mail_scoped = _mail_scoped_tool_call(tool_call) or (
            background_mode and bool(_BACKGROUND_MAIL_INTENT_RE.search(str(tool_call.get("title") or "")))
        )
        execute_category = _workspace_execute_category(tool_call, self.cwd) \
            if (workspace_mode or background_mode) and tool_kind == "execute" else ""
        _verbose_debug_emit(
            "permission_request", tool_kind=tool_kind, permission_mode=permission_mode,
            option_count=len(options), execute_category=execute_category,
        )
        if permission_mode == "passive_recall":
            reject_option = next((option for option in options if option["kind"] == "reject_once"), None)
            if reject_option is None:
                reject_option = next((option for option in options if option["kind"] == "reject_always"), None)
            if reject_option:
                self._send_response(rpc_id, {
                    "outcome": {"outcome": "selected", "optionId": reject_option["option_id"]}
                })
                _telemetry_emit("acp_permission", decision="policy-reject",
                                tool_kind=tool_kind, option_count=len(options))
            else:
                self._send_rpc_error(rpc_id, -32602, "Eva passive recall does not authorize tools")
                _telemetry_emit("acp_permission", decision="policy-deny",
                                tool_kind=tool_kind, option_count=len(options))
            return
        coding_reason = ""
        if coding_mode:
            allow_once = next((option for option in options if option["kind"] == "allow_once"), None)
            coding_reason = _coding_autonomy_approval_reason(tool_call, tool_kind, mail_scoped)
            if not coding_reason and allow_once:
                self._send_response(rpc_id, {
                    "outcome": {"outcome": "selected", "optionId": allow_once["option_id"]}
                })
                _telemetry_emit("acp_permission", decision="coding-autonomy-approve",
                                tool_kind=tool_kind, option_count=len(options))
                return
            coding_reason = coding_reason or "no one-time approval option"
        if workspace_mode or background_mode:
            allow_once = next((option for option in options if option["kind"] == "allow_once"), None)
            reject_once = next((option for option in options if option["kind"] == "reject_once"), None)
            reject_option = reject_once or next(
                (option for option in options if option["kind"] == "reject_always"), None
            )
            block_reason = ""
            unverified_research = False
            if background_mode and not _st.bg_auto_actions:
                block_reason = "autonomy_disabled"
            elif (background_mode and tool_kind not in {"read", "edit", "execute", "search", "fetch"}) or tool_kind in {"delete", "other"}:
                block_reason = "unsupported_tool"
            elif background_mode and (
                _BACKGROUND_DESTRUCTIVE_TOOL_RE.search(str(tool_call.get("name") or ""))
                or (
                    _BACKGROUND_DESTRUCTIVE_TOOL_RE.search(str(tool_call.get("title") or ""))
                    and not (
                        tool_kind == "fetch"
                        and _background_web_tool_name({"name": tool_call.get("name")}) == "web_fetch"
                        and not _background_remote_block_reason(tool_call)
                    )
                )
            ):
                block_reason = "destructive_action"
            elif mail_scoped:
                block_reason = "mailbox_access"
            elif tool_kind == "edit":
                if _workspace_edit_target_is_protected(tool_call) or (background_mode and _background_protected_targets(tool_call, self.cwd)):
                    block_reason = "protected_path"
                elif not _workspace_edit_target_is_local(tool_call, self.cwd):
                    block_reason = "outside_workspace_edit"
            elif tool_kind == "execute":
                block_reason = (_background_autonomy_block_reason(tool_call, self.cwd)
                                if background_mode else _workspace_autonomy_block_reason(tool_call, self.cwd))
            elif background_mode and tool_kind == "read":
                if _workspace_edit_targets(tool_call):
                    if _background_protected_targets(tool_call, self.cwd):
                        block_reason = "protected_path"
                    elif not _workspace_edit_target_is_local(tool_call, self.cwd):
                        block_reason = "outside_workspace_read"
                else:
                    block_reason = _background_remote_block_reason(tool_call)
            elif background_mode and tool_kind in {"search", "fetch"}:
                block_reason = _background_remote_block_reason(tool_call)
                if block_reason == "unclassified_remote_action" and not _background_web_tool_name(tool_call):
                    candidate_reason = _unverified_research_block_reason(tool_call)
                    if candidate_reason:
                        block_reason = candidate_reason
                    elif _unverified_research_allowed():
                        block_reason = ""
                        unverified_research = True
            if block_reason:
                with self._prompt_state_lock:
                    for state in self._active_prompts.values():
                        if state.get("session_id") == session_id:
                            state["permission_cancelled"] = True
                            state["permission_reason"] = "policy_blocked"
                if reject_option:
                    self._send_response(rpc_id, {
                        "outcome": {"outcome": "selected", "optionId": reject_option["option_id"]}
                    })
                else:
                    self._send_response(rpc_id, {"outcome": {"outcome": "cancelled"}})
                if background_mode:
                    _telemetry_emit("acp_permission", decision="background-autonomy-reject-" + block_reason,
                                    tool_kind=tool_kind, option_count=len(options))
                else:
                    _telemetry_emit("acp_permission", decision="workspace-autonomy-reject-" + block_reason,
                                    tool_kind=tool_kind, option_count=len(options))
                _record_autonomy_block("background" if background_mode else "workspace", tool_kind, block_reason)
                return
            if allow_once:
                if background_mode:
                    with _st.bg_auto_actions_lock:
                        if not _st.bg_auto_actions or (unverified_research and not _unverified_research_allowed()):
                            revoked_reason = "unclassified_remote_action" if _st.bg_auto_actions else "autonomy_disabled"
                            with self._prompt_state_lock:
                                for state in self._active_prompts.values():
                                    if state.get("session_id") == session_id:
                                        state["permission_cancelled"] = True
                                        state["permission_reason"] = "policy_blocked"
                            if reject_option:
                                self._send_response(rpc_id, {
                                    "outcome": {"outcome": "selected", "optionId": reject_option["option_id"]}
                                })
                            else:
                                self._send_response(rpc_id, {"outcome": {"outcome": "cancelled"}})
                            _telemetry_emit("acp_permission", decision="background-autonomy-reject-" + revoked_reason,
                                            tool_kind=tool_kind, option_count=len(options))
                            _record_autonomy_block("background", tool_kind, revoked_reason)
                            return
                        self._send_response(rpc_id, {
                            "outcome": {"outcome": "selected", "optionId": allow_once["option_id"]}
                        })
                    _telemetry_emit("acp_permission", decision="background-autonomy-approve-unverified-research" if unverified_research else "background-autonomy-approve",
                                    tool_kind=tool_kind, option_count=len(options))
                else:
                    self._send_response(rpc_id, {
                        "outcome": {"outcome": "selected", "optionId": allow_once["option_id"]}
                    })
                    _telemetry_emit("acp_permission", decision="workspace-autonomy-approve",
                                    tool_kind=tool_kind, option_count=len(options))
                return
            if background_mode:
                with self._prompt_state_lock:
                    for state in self._active_prompts.values():
                        if state.get("session_id") == session_id:
                            state["permission_cancelled"] = True
                            state["permission_reason"] = "policy_blocked"
                if reject_option:
                    self._send_response(rpc_id, {
                        "outcome": {"outcome": "selected", "optionId": reject_option["option_id"]}
                    })
                else:
                    self._send_response(rpc_id, {"outcome": {"outcome": "cancelled"}})
                _telemetry_emit("acp_permission", decision="background-autonomy-no-allow-once",
                                tool_kind=tool_kind, option_count=len(options))
                _record_autonomy_block("background", tool_kind, "no_allow_once")
                return
        allow_once = next((option for option in options if option["kind"] == "allow_once"), None)
        if not coding_mode and not sandbox_manual and tool_kind == "execute" and allow_once and not mail_scoped and _workspace_read_only_execute(tool_call, self.cwd):
            self._send_response(rpc_id, {
                "outcome": {"outcome": "selected", "optionId": allow_once["option_id"]}
            })
            decision = "workspace-auto-allow-read-execute" if workspace_mode else "auto-allow-read-execute"
            _telemetry_emit("acp_permission", decision=decision,
                            tool_kind=tool_kind, option_count=len(options))
            return
        routine_allowed = not coding_mode and bool(_get_learning_consent().get("routine_tools")) and tool_kind in {
            "read", "search", "fetch", "think"
        } and not mail_scoped
        if routine_allowed and allow_once:
            self._send_response(rpc_id, {"outcome": {"outcome": "selected", "optionId": allow_once["option_id"]}})
            _telemetry_emit("acp_permission", decision="standing-consent", tool_kind=tool_kind, option_count=len(options))
            return

        permission_id = uuid.uuid4().hex
        command_summary = _coding_command_summary(tool_call) if sandbox_manual else (
            _coding_command_summary(tool_call) if tool_kind == "execute" else _coding_action_summary(tool_call)
        ) if coding_mode else _command_summary(tool_call) if tool_kind == "execute" else ""
        entry = {
            "id": permission_id,
            "rpc_id": rpc_id,
            "session_id": str(params.get("sessionId") or "")[:120],
            "tool_kind": tool_kind,
            "permission_mode": permission_mode,
            "command_summary": command_summary,
            "approval_allowed": bool(command_summary) if coding_mode else tool_kind != "execute" or bool(command_summary),
            "approval_reason": coding_reason,
            "options": options,
            "created_at": time.time(),
        }
        with self.permission_lock:
            self.pending_permissions[permission_id] = entry
        _telemetry_emit("acp_permission", decision="pending", tool_kind=tool_kind, option_count=len(options))
        timer = threading.Timer(60, self._expire_permission, args=(permission_id,))
        timer.daemon = True
        timer.start()

    def list_pending_permissions(self):
        with self.permission_lock:
            return [{
                "id": entry["id"],
                "tool_kind": entry["tool_kind"],
                "command_summary": entry.get("command_summary", ""),
                "approval_allowed": entry.get("approval_allowed", True),
                "approval_reason": entry.get("approval_reason", ""),
                "options": list(entry["options"]),
                "created_at": entry["created_at"],
            } for entry in self.pending_permissions.values()]

    def resolve_permission(self, permission_id, option_id=None, decision=None):
        with self.permission_lock:
            entry = self.pending_permissions.get(str(permission_id))
        if not entry:
            return False
        normalized_decision = str(decision or "").strip().lower()
        if normalized_decision in {"allow", "reject"}:
            requested_kind = "allow_once" if normalized_decision == "allow" else "reject_once"
            selected = next((option for option in entry["options"] if option["kind"] == requested_kind), None)
        else:
            selected = next((
                option for option in entry["options"]
                if option["option_id"] == str(option_id or "")
                and option["kind"] in {"allow_once", "reject_once"}
            ), None)
        if not selected or (selected["kind"] == "allow_once" and not entry.get("approval_allowed", True)):
            _telemetry_emit("acp_permission", decision="invalid-decision",
                            tool_kind=entry["tool_kind"], option_count=len(entry["options"]))
            return False
        with self.permission_lock:
            if self.pending_permissions.pop(str(permission_id), None) is None:
                return False
        if selected["kind"] == "allow_once":
            self._send_response(entry["rpc_id"], {"outcome": {"outcome": "selected", "optionId": selected["option_id"]}})
            resolved_decision = "allow_once"
        else:
            with self._prompt_state_lock:
                for state in self._active_prompts.values():
                    if state.get("session_id") == entry["session_id"]:
                        state["permission_cancelled"] = True
                        state["permission_reason"] = "user_rejected"
            self._send_notification("session/cancel", {"sessionId": entry["session_id"]})
            self._send_response(entry["rpc_id"], {"outcome": {"outcome": "cancelled"}})
            resolved_decision = "user-rejected"
            if entry.get("permission_mode") in {"workspace_write", "workspace_auto", "coding_auto"}:
                _record_autonomy_block("workspace", entry["tool_kind"], "user_rejected")
        _telemetry_emit("acp_permission", decision=resolved_decision, tool_kind=entry["tool_kind"], option_count=len(entry["options"]))
        return True

    def _expire_permission(self, permission_id):
        with self.permission_lock:
            entry = self.pending_permissions.pop(str(permission_id), None)
        if not entry:
            return
        with self._prompt_state_lock:
            for state in self._active_prompts.values():
                if state.get("session_id") == entry["session_id"]:
                    state["permission_cancelled"] = True
                    state["permission_reason"] = "permission_timeout"
        self._send_notification("session/cancel", {"sessionId": entry["session_id"]})
        self._send_response(entry["rpc_id"], {"outcome": {"outcome": "cancelled"}})
        _telemetry_emit("acp_permission", decision="expired", tool_kind=entry["tool_kind"], option_count=len(entry["options"]))
        if entry.get("permission_mode") in {"workspace_write", "workspace_auto", "coding_auto"}:
            _record_autonomy_block("workspace", entry["tool_kind"], "permission_timeout")

    # --- Terminal handlers (for ACP tool execution) ---

    def _handle_terminal_create(self, rid, params):
        """Execute a shell command requested by the agent."""
        if self.sandbox_image or self.isolated_reasoning:
            self._send_rpc_error(rid, -32602, "Host terminals are unavailable to sandboxed workspace runs.")
            return
        command = params.get("command", "")
        args = params.get("args", [])
        cwd = params.get("cwd") or self.cwd
        env_vars = params.get("env", [])

        # Build the full command
        full_cmd = command
        if args:
            full_cmd = command + " " + " ".join(args)

        print("[ACP Terminal] Creating authorized terminal")

        # Build environment
        env = _safe_child_environment()
        env.pop("EVA_BRIDGE_TOKEN", None)
        for ev in env_vars:
            if isinstance(ev, dict) and "name" in ev and "value" in ev:
                env[ev["name"]] = ev["value"]
        env.pop("EVA_BRIDGE_TOKEN", None)
        os.makedirs(_ARTIFACTS_DIR, exist_ok=True)
        env["EVA_ARTIFACTS_DIR"] = _ARTIFACTS_DIR

        import uuid
        terminal_id = str(uuid.uuid4())

        try:
            with self._terminal_lock:
                if self._cancel_requested.is_set() or _st.acp_shutdown.is_set():
                    raise RuntimeError("This agent has been interrupted.")
                proc = subprocess.Popen(
                    full_cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=cwd,
                    env=env,
                    start_new_session=os.name != "nt",
                    **_hidden_subprocess_options(),
                )
                entry = {"process": proc, "output": "", "owns_process_group": os.name != "nt"}
                self.terminals[terminal_id] = entry

            # Read output in background
            def read_output():
                try:
                    out, _ = proc.communicate(timeout=60)
                    entry["output"] = out.decode("utf-8", errors="replace")
                    entry["exit_code"] = proc.returncode
                except subprocess.TimeoutExpired:
                    self._terminate_owned_process(proc, entry["owns_process_group"])
                    out, _ = proc.communicate()
                    entry["output"] = out.decode("utf-8", errors="replace") + "\n[TIMEOUT]"
                    entry["exit_code"] = -1

            t = threading.Thread(target=read_output, daemon=True)
            t.start()

            self._send_response(rid, {"terminalId": terminal_id})
            print(f"[ACP Terminal] Started: {terminal_id}")

        except Exception as e:
            print(f"[ACP Terminal] Error: {e}")
            self._send_response(rid, {"error": {"code": -32000, "message": str(e)}})

    def _handle_terminal_output(self, rid, params):
        """Return terminal output and exit status."""
        terminal_id = params.get("terminalId", "")
        term = self.terminals.get(terminal_id)

        if not term:
            self._send_response(rid, {"error": {"code": -32000, "message": "Unknown terminal"}})
            return

        proc = term["process"]
        # Wait a bit if still running
        if proc.poll() is None:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass

        output = term.get("output", "")
        exit_code = term.get("exit_code", proc.returncode)

        print(f"[ACP Terminal] Output ({terminal_id[:8]}): exit={exit_code}, len={len(output)}")

        self._send_response(rid, {
            "output": output,
            "exitCode": exit_code if exit_code is not None else -1,
            "isRunning": proc.poll() is None
        })

    def _handle_terminal_release(self, rid, params):
        """Release a terminal."""
        terminal_id = params.get("terminalId", "")
        with self._terminal_lock:
            term = self.terminals.pop(terminal_id, None)
            if term:
                self._terminate_owned_process(term["process"], term.get("owns_process_group", False))
        print(f"[ACP Terminal] Released: {terminal_id[:8] if terminal_id else '?'}")
        self._send_response(rid, {})

    # --- Public API ---

    def prompt(self, text, timeout=120, conversation_id=None, on_chunk=None,
               permission_mode="interactive", on_event=None, on_reasoning=None, include_reasoning=False):
        with _pin_acp_client(self) as acquired:
            if not acquired:
                return {"error": "ACP client is unavailable"}
            with self.prompt_lock:
                return self._prompt(text, timeout, conversation_id, on_chunk, permission_mode, on_event,
                                    on_reasoning, include_reasoning)

    def _begin_prompt(self, prompt_id, session_id, on_chunk, permission_mode="interactive", on_event=None,
                      on_reasoning=None, include_reasoning=False):
        with self._prompt_state_lock:
            self.response_chunks[prompt_id] = ""
            self._session_permission_modes[session_id] = permission_mode
            try:
                self._session_permission_mode_order.remove(session_id)
            except ValueError:
                pass
            self._session_permission_mode_order.append(session_id)
            while len(self._session_permission_mode_order) > _ACP_SESSION_MAX:
                evicted_session_id = self._session_permission_mode_order.pop(0)
                self._session_permission_modes.pop(evicted_session_id, None)
            self._active_prompts[prompt_id] = {
                "session_id": session_id,
                "on_chunk": on_chunk,
                "on_event": on_event,
                "on_reasoning": on_reasoning,
                "include_reasoning": bool(include_reasoning),
                "reasoning_content": "",
                "permission_mode": permission_mode,
                "permission_cancelled": False,
                "permission_reason": "",
                "chunk_count": 0,
                "first_chunk_at": None,
                "started_at": time.perf_counter(),
            }

    def _finish_prompt(self, prompt_id):
        with self._prompt_state_lock:
            state = self._active_prompts.pop(prompt_id, {})
            response_text = self.response_chunks.pop(prompt_id, "")
        first_chunk_at = state.get("first_chunk_at")
        started_at = state.get("started_at")
        return response_text, {
            "chunk_count": state.get("chunk_count", 0),
            "permission_cancelled": bool(state.get("permission_cancelled")),
            "permission_reason": str(state.get("permission_reason") or "")[:32],
            "reasoning_content": state.get("reasoning_content", ""),
            "first_chunk_ms": round((first_chunk_at - started_at) * 1000.0, 1)
            if first_chunk_at is not None and started_at is not None else None,
        }

    def _prompt(self, text, timeout=120, conversation_id=None, on_chunk=None,
                permission_mode="interactive", on_event=None, on_reasoning=None, include_reasoning=False):
        """Send a text prompt and return the accumulated response text."""
        session_id = self._session_for_conversation(conversation_id)
        if not session_id:
            return {"error": "No active ACP session"}

        pid = self._next_id()
        self._begin_prompt(pid, session_id, on_chunk, permission_mode, on_event, on_reasoning, include_reasoning)

        _t0 = time.perf_counter()
        try:
            result = self._send_request("session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}]
            }, timeout=timeout)
        finally:
            response_text, prompt_metrics = self._finish_prompt(pid)
        _ms = round((time.perf_counter() - _t0) * 1000.0, 1)

        if not isinstance(result, dict) or not (result.get("stopReason") or result.get("error")):
            result = {"error": "ACP prompt ended without a completion receipt."}
        if result and isinstance(result, dict):
            if "error" in result:
                _telemetry_emit("acp_prompt", model=self.model or "default",
                                prompt_chars=len(text or ""), response_chars=0,
                                ms=_ms, stop_reason=result.get("stop_reason", "error"),
                                chunk_count=prompt_metrics["chunk_count"],
                                first_chunk_ms=prompt_metrics["first_chunk_ms"])
                return {"error": result["error"],
                    "permission_cancelled": prompt_metrics["permission_cancelled"],
                    "permission_reason": prompt_metrics["permission_reason"]}
            stop_reason = result.get("stopReason", "end_turn")
            _telemetry_emit("acp_prompt", model=self.model or "default",
                            prompt_chars=len(text or ""), response_chars=len(response_text or ""),
                            ms=_ms, stop_reason=stop_reason,
                            chunk_count=prompt_metrics["chunk_count"],
                            first_chunk_ms=prompt_metrics["first_chunk_ms"])
            return {
                "text": response_text,
                "stop_reason": stop_reason,
                "permission_cancelled": prompt_metrics["permission_cancelled"],
                "permission_reason": prompt_metrics["permission_reason"],
                "reasoning_content": prompt_metrics["reasoning_content"],
            }

        _telemetry_emit("acp_prompt", model=self.model or "default",
                        prompt_chars=len(text or ""), response_chars=len(response_text or ""),
                        ms=_ms, stop_reason="end_turn",
                        chunk_count=prompt_metrics["chunk_count"],
                        first_chunk_ms=prompt_metrics["first_chunk_ms"])
        return {"text": response_text, "stop_reason": "end_turn",
            "reasoning_content": prompt_metrics["reasoning_content"],
            "permission_cancelled": prompt_metrics["permission_cancelled"],
            "permission_reason": prompt_metrics["permission_reason"]}

    def prompt_with_image(self, text, image_b64, mime="image/jpeg", timeout=120, conversation_id=None, on_chunk=None,
                          permission_mode="interactive", on_reasoning=None, include_reasoning=False):
        with _pin_acp_client(self) as acquired:
            if not acquired:
                return {"error": "ACP client is unavailable"}
            with self.prompt_lock:
                return self._prompt_with_image(text, image_b64, mime, timeout, conversation_id, on_chunk,
                                               permission_mode, on_reasoning, include_reasoning)

    def _prompt_with_image(self, text, image_b64, mime="image/jpeg", timeout=120, conversation_id=None, on_chunk=None,
                           permission_mode="interactive", on_reasoning=None, include_reasoning=False):
        """Send a text + image prompt and return the accumulated response text.

        Uses the ACP content-block image type (the agent advertised
        promptCapabilities.image=true). image_b64 is base64 with no data: prefix.
        """
        session_id = self._session_for_conversation(conversation_id)
        if not session_id:
            return {"error": "No active ACP session"}

        pid = self._next_id()
        self._begin_prompt(pid, session_id, on_chunk, permission_mode,
                           on_reasoning=on_reasoning, include_reasoning=include_reasoning)

        _t0 = time.perf_counter()
        try:
            result = self._send_request("session/prompt", {
                "sessionId": session_id,
                "prompt": [
                    {"type": "text", "text": text},
                    {"type": "image", "data": image_b64, "mimeType": mime},
                ]
            }, timeout=timeout)
        finally:
            response_text, prompt_metrics = self._finish_prompt(pid)
        _ms = round((time.perf_counter() - _t0) * 1000.0, 1)

        if not isinstance(result, dict) or not (result.get("stopReason") or result.get("error")):
            result = {"error": "ACP prompt ended without a completion receipt."}
        if result and isinstance(result, dict):
            if "error" in result:
                _telemetry_emit("acp_vision", model=self.model or "default",
                                prompt_chars=len(text or ""), response_chars=0,
                                ms=_ms, stop_reason=result.get("stop_reason", "error"),
                                chunk_count=prompt_metrics["chunk_count"],
                                first_chunk_ms=prompt_metrics["first_chunk_ms"])
                return {"error": result["error"]}
            stop_reason = result.get("stopReason", "end_turn")
            _telemetry_emit("acp_vision", model=self.model or "default",
                            prompt_chars=len(text or ""), response_chars=len(response_text or ""),
                            ms=_ms, stop_reason=stop_reason,
                            chunk_count=prompt_metrics["chunk_count"],
                            first_chunk_ms=prompt_metrics["first_chunk_ms"])
            return {"text": response_text, "stop_reason": stop_reason,
                    "reasoning_content": prompt_metrics["reasoning_content"]}

        _telemetry_emit("acp_vision", model=self.model or "default",
                        prompt_chars=len(text or ""), response_chars=len(response_text or ""),
                        ms=_ms, stop_reason="end_turn",
                        chunk_count=prompt_metrics["chunk_count"],
                        first_chunk_ms=prompt_metrics["first_chunk_ms"])
        return {"text": response_text, "stop_reason": "end_turn",
                "reasoning_content": prompt_metrics["reasoning_content"]}

    def _new_session(self):
        """Create an ACP conversation without restarting the warm CLI process."""
        result = self._send_request("session/new", {
            "cwd": self.cwd,
            "mcpServers": []
        }, timeout=30)
        if result and isinstance(result, dict) and result.get("sessionId"):
            return result["sessionId"]
        return None

    def _remember_conversation_session(self, key, session_id, prompts=0):
        self._conversation_sessions[key] = {
            "session_id": session_id,
            "prompts": prompts,
            "last_used": time.monotonic(),
        }
        try:
            self._conversation_session_order.remove(key)
        except ValueError:
            pass
        self._conversation_session_order.append(key)
        while len(self._conversation_session_order) > _ACP_SESSION_MAX:
            evicted = self._conversation_session_order.pop(0)
            self._conversation_sessions.pop(evicted, None)

    def _session_for_conversation(self, conversation_id):
        """Return a bounded ACP session for one frontend conversation.

        ACP has no session-delete method in the protocol version used here, so
        old sessions are dropped from the bridge routing table. The prompt and
        idle caps ensure a browser conversation cannot keep one hidden ACP
        context growing forever while the CLI process remains warm.
        """
        key = str(conversation_id or "").strip()[:120] or "__default__"
        now = time.monotonic()
        entry = self._conversation_sessions.get(key)
        if entry and entry["prompts"] < _ACP_SESSION_MAX_PROMPTS and now - entry["last_used"] <= _ACP_SESSION_IDLE_SECONDS:
            entry["prompts"] += 1
            entry["last_used"] = now
            self._remember_conversation_session(key, entry["session_id"], entry["prompts"])
            return entry["session_id"]

        session_id = self._new_session()
        if not session_id:
            return None
        self._remember_conversation_session(key, session_id, prompts=1)
        return session_id


# ---------------------------------------------------------------------------
# Token cache helper
# ---------------------------------------------------------------------------


def _acp_model_key(model, reasoning_effort=None, tool_profile=None, mcp_config=None,
                   provider_config=None, no_tools=False):
    """Normalize model and reasoning effort into a warm-client pool key."""
    model_key = (model or "").strip() or "__default__"
    effort_key = (reasoning_effort or "").strip() or "__default__"
    profile_key = _normalize_tool_profile(tool_profile, bool(mcp_config))
    fingerprint = _acp_config_fingerprint(mcp_config or {})
    provider_fingerprint = _provider_config_fingerprint(provider_config)
    tools_key = "no-tools" if no_tools else "tools"
    return f"{model_key}::{effort_key}::{profile_key}::{tools_key}::{fingerprint}::{provider_fingerprint}"



def _acp_pool_touch(key):
    """Mark a pool key as most-recently-used."""
    try:
        _st.acp_pool_order.remove(key)
    except ValueError:
        pass
    _st.acp_pool_order.append(key)



def _acp_pool_register(client):
    """Register an externally-built client (e.g. the startup singleton or a
    reconfigured client) into the pool under its model key. Caller holds the lock."""
    if not client:
        return
    key = _acp_model_key(
        client.model, client.reasoning_effort, client.tool_profile,
        client.mcp_config, client.provider_config, client.no_tools,
    )
    _st.acp_pool[key] = client
    _acp_pool_touch(key)



def _acp_pool_evict_if_needed(protect_key):
    """Evict least-recently-used warm clients past the cap. Never evicts the
    protected key or the client currently referenced by the _st.acp_client pointer.
    Caller holds the lock."""
    while len(_st.acp_pool) > _ACP_POOL_MAX:
        victim_key = None
        for k in list(_st.acp_pool_order):
            if k == protect_key:
                continue
            if _st.acp_client is not None and _st.acp_pool.get(k) is _st.acp_client:
                continue
            if getattr(_st.acp_pool.get(k), "active_requests", 0) > 0:
                continue
            victim_key = k
            break
        if victim_key is None:
            break
        victim = _st.acp_pool.pop(victim_key, None)
        try:
            _st.acp_pool_order.remove(victim_key)
        except ValueError:
            pass
        if victim:
            print(f"[Bridge] Evicting warm ACP client: {victim_key}")
            _telemetry_emit("acp_pool", result="evict", model=victim_key, pool_size=len(_st.acp_pool))
            try:
                victim.stop()
            except Exception:
                pass


def _release_acp_client(client):
    """Release a request pin and trim any temporary pool overflow."""
    with _st.acp_pool_lock:
        client.active_requests = max(0, client.active_requests - 1)
        current_key = None
        if _st.acp_client is not None:
            current_key = _acp_model_key(_st.acp_client.model, _st.acp_client.reasoning_effort,
                                         _st.acp_client.tool_profile, _st.acp_client.mcp_config,
                                         _st.acp_client.provider_config, _st.acp_client.no_tools)
        _acp_pool_evict_if_needed(current_key)


@contextmanager
def _pin_acp_client(client):
    """Keep an existing client alive for one prompt lifecycle."""
    with _st.acp_pool_lock:
        acquired = bool(client and client.alive)
        if acquired:
            client.active_requests += 1
    try:
        yield acquired
    finally:
        if acquired:
            _release_acp_client(client)



def _reset_acp_pool(keep_client):
    """Stop and clear all pooled clients except keep_client, then register
    keep_client. Used when MCP config changes so stale clients are not reused."""
    with _st.acp_pool_lock:
        for key, client in list(_st.acp_pool.items()):
            if client is keep_client:
                continue
            try:
                client.stop()
            except Exception:
                pass
        _st.acp_pool.clear()
        _st.acp_pool_order.clear()
        if keep_client:
            _acp_pool_register(keep_client)



def _ensure_acp_model(requested_model, reasoning_effort=None, tool_profile=None,
                      provider_config=None, no_tools=False):
    """Ensure a warm ACP client for requested_model is selected as _st.acp_client.

    Uses a warm pool so switching between the cognition draft model and the
    reviewer model reuses a live Copilot CLI instead of respawning it every turn.
    Returns (ok, model_or_error)."""
    # global statement removed — writes go to _st.*

    with _st.acp_pool_lock:
        # Seed the pool with the startup singleton on first use.
        if _st.acp_client and _acp_model_key(_st.acp_client.model, _st.acp_client.reasoning_effort,
                                             _st.acp_client.tool_profile, _st.acp_client.mcp_config,
                                             _st.acp_client.provider_config, _st.acp_client.no_tools) not in _st.acp_pool:
            _acp_pool_register(_st.acp_client)

        if not _st.acp_client and not _st.acp_pool and not provider_config:
            return False, "ACP bridge not connected to Copilot"

        profile = _normalize_tool_profile(tool_profile, bool(_st.configured_mcp_config))
        profile_config = _acp_runtime_tool_config(profile, provider_config, no_tools)
        key = _acp_model_key(requested_model, reasoning_effort, profile, profile_config,
                             provider_config, no_tools)

        # Fast path: a live warm client already exists for this model.
        existing = _st.acp_pool.get(key)
        if existing and existing.alive:
            if not provider_config and not no_tools:
                _st.acp_client = existing
            _acp_pool_touch(key)
            _telemetry_emit("acp_pool", result="hit", model=existing.model or "default",
                            tool_profile=existing.tool_profile, server_count=len(existing.mcp_config),
                            pool_hit=True, pool_warm=False, pool_size=len(_st.acp_pool))
            return True, existing.model or "default"

        # Need to warm a new client. Use any live client as the cwd/path/MCP template.
        template = _st.acp_client
        if template is None or not template.alive:
            for c in _st.acp_pool.values():
                if c and c.alive:
                    template = c
                    break
        if template is None:
            # Nothing alive to template from; fall back to the existing pointer.
            template = _st.acp_client
        if template is None and provider_config:
            template = ACPClient(
                copilot_path="copilot",
                cwd=os.getcwd(),
                model=requested_model or provider_config.get("COPILOT_MODEL"),
                mcp_config={},
                tool_profile=profile,
                no_tools=no_tools,
                provider_config=provider_config,
            )
        if template is None:
            return False, "ACP bridge not connected to Copilot"

        if requested_model:
            print(f"[Bridge] Warming ACP client for model: {requested_model}")
        else:
            print("[Bridge] Warming ACP client for default model")

        # Drop a dead client occupying this key before replacing it.
        if existing and not existing.alive:
            try:
                existing.stop()
            except Exception:
                pass
            _st.acp_pool.pop(key, None)
            try:
                _st.acp_pool_order.remove(key)
            except ValueError:
                pass

        try:
            new_client = ACPClient(
                copilot_path=template.copilot_path,
                cwd=template.cwd,
                model=(requested_model or None),
                mcp_config=_inject_kusto_token(profile_config),
                reasoning_effort=reasoning_effort,
                tool_profile=profile,
                no_tools=no_tools,
                provider_config=provider_config,
            )
            _warm_t0 = time.perf_counter()
            new_client.start()
        except RuntimeError as e:
            print(f"[Bridge] Warm client start failed: {e}")
            _telemetry_emit("acp_pool", result="warm_failed", model=key, error=str(e))
            return False, str(e)

        _st.acp_pool[key] = new_client
        _acp_pool_touch(key)
        if not provider_config and not no_tools:
            _st.acp_client = new_client
        _acp_pool_evict_if_needed(key)
        _telemetry_emit("acp_pool", result="warm", model=new_client.model or "default",
                tool_profile=new_client.tool_profile, server_count=len(new_client.mcp_config),
                pool_hit=False, pool_warm=True, pool_size=len(_st.acp_pool),
                warm_ms=round((time.perf_counter() - _warm_t0) * 1000.0, 1))
        return True, new_client.model or "default"


@contextmanager
def _acquire_acp_client(requested_model, reasoning_effort=None, tool_profile=None,
                        provider_config=None, no_tools=False):
    """Atomically select and pin a model/effort client from the warm pool."""
    selected_client = None
    detail = "ACP bridge not connected to Copilot"
    with _st.acp_pool_lock:
        profile = _normalize_tool_profile(tool_profile, bool(_st.configured_mcp_config))
        switched, detail = _ensure_acp_model(
            requested_model, reasoning_effort, profile, provider_config=provider_config,
            no_tools=no_tools
        )
        if switched:
            profile_config = _acp_runtime_tool_config(profile, provider_config, no_tools)
            key = _acp_model_key(
                requested_model, reasoning_effort, profile, profile_config, provider_config, no_tools
            )
            candidate = _st.acp_pool.get(key)
            if candidate and candidate.alive:
                candidate.active_requests += 1
                selected_client = candidate
            else:
                detail = "Selected ACP client is unavailable"
    try:
        yield selected_client, detail
    finally:
        if selected_client:
            _release_acp_client(selected_client)
