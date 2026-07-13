"""Bridge domain: acp_client."""

import json
import math
import os
import platform
import pwd
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from bridge import config as _cfg
from bridge import state as _st
from bridge.kusto import _inject_kusto_token
from bridge.telemetry import _telemetry_emit

_ACP_POOL_MAX = _cfg.ACP_POOL_MAX
_ACP_RUNTIME_DIR = _cfg.ACP_RUNTIME_DIR
_COPILOT_PREFLIGHT = {}
_COPILOT_PREFLIGHT_LOCK = threading.Lock()
_COPILOT_REQUIRED_FLAGS = (
    "--acp", "--disable-builtin-mcps", "--no-bash-env",
    "--no-custom-instructions", "--no-remote", "--no-remote-export",
)
_ACP_FRAME_MAX_BYTES = 2 * 1024 * 1024
_ACP_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
_ACP_STOP_REASONS = frozenset({
    "end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled",
})


class ACPRequestError(RuntimeError):
    """Transport or JSON-RPC method failure, distinct from a successful result."""



def _decode_json_rpc_frame(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > _ACP_FRAME_MAX_BYTES:
        raise RuntimeError("ACP frame is empty or too large")
    try:
        text = raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("ACP frame is not valid UTF-8") from exc
    if not text:
        raise RuntimeError("ACP frame is empty")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeError("ACP frame has duplicate members")
            result[key] = value
        return result

    def reject_constant(_value):
        raise RuntimeError("ACP frame has a non-standard number")

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise RuntimeError("ACP frame has a non-finite number")
        return parsed

    try:
        result = json.loads(
            text, object_pairs_hook=unique_object,
            parse_constant=reject_constant, parse_float=finite_float,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("ACP frame is invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("ACP frame must be an object")
    return result


def _parse_jsonc(text):
    if not isinstance(text, str) or len(text) > 2 * 1024 * 1024:
        raise RuntimeError("Copilot auth config is invalid")
    output = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < len(text) and text[index:index + 2] != "*/":
                if text[index] in "\r\n":
                    output.append(text[index])
                index += 1
            if index + 1 >= len(text):
                raise RuntimeError("Copilot auth config has an unterminated comment")
            index += 2
            continue
        output.append(char)
        index += 1
    if in_string:
        raise RuntimeError("Copilot auth config has an unterminated string")

    stripped = "".join(output)
    output = []
    index = 0
    in_string = False
    escaped = False
    while index < len(stripped):
        char = stripped[index]
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(stripped) and stripped[lookahead].isspace():
                lookahead += 1
            if lookahead < len(stripped) and stripped[lookahead] in "}]":
                index += 1
                continue
        output.append(char)
        index += 1
    try:
        value = json.loads("".join(output))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RuntimeError("Copilot auth config is invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Copilot auth config must be an object")
    return value


def _safe_auth_value(value, *, depth=0, budget=None):
    if budget is None:
        budget = [0]
    budget[0] += 1
    if depth > 8 or budget[0] > 2048:
        raise RuntimeError("Copilot auth state is too complex")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeError("Copilot auth state contains a non-finite number")
        return value
    if isinstance(value, str):
        if "\x00" in value or len(value) > 16384:
            raise RuntimeError("Copilot auth state contains invalid text")
        return value
    if isinstance(value, list):
        if len(value) > 128:
            raise RuntimeError("Copilot auth state list is too large")
        return [
            _safe_auth_value(item, depth=depth + 1, budget=budget)
            for item in value
        ]
    if isinstance(value, dict):
        if len(value) > 128:
            raise RuntimeError("Copilot auth state object is too large")
        result = {}
        for key, item in value.items():
            if (
                not isinstance(key, str) or not key or "\x00" in key
                or len(key) > 256 or key in ("__proto__", "constructor", "prototype")
            ):
                raise RuntimeError("Copilot auth state key is invalid")
            result[key] = _safe_auth_value(
                item, depth=depth + 1, budget=budget
            )
        return result
    raise RuntimeError("Copilot auth state contains unsupported data")


def _auth_projection(source_path):
    try:
        info = os.stat(source_path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError("Copilot authentication config is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or hasattr(os, "getuid") and info.st_uid != os.getuid()
    ):
        raise RuntimeError("Copilot authentication config is not owner-only")
    try:
        with _cfg.open_private_file(
            source_path, "r", encoding="utf-8"
        ) as handle:
            source = _parse_jsonc(handle.read())
    except OSError as exc:
        raise RuntimeError("Copilot authentication config is unreadable") from exc
    projection = {
        "disableAllHooks": True,
        "trustedFolders": [],
        "ide": {"autoConnect": False},
        "bashEnv": False,
    }
    for key in (
        "lastLoggedInUser", "loggedInUsers", "schemaVersion",
        "expAssignmentsCache", "staff",
    ):
        if key in source:
            projection[key] = _safe_auth_value(source[key])
    return projection


def _trusted_executable(path):
    candidate = path
    if not os.path.isabs(candidate):
        candidate = shutil.which(candidate, path=_cfg._fixed_child_path()) or ""
    candidate = os.path.realpath(candidate) if candidate else ""
    if not candidate:
        raise RuntimeError("Copilot CLI executable was not found on the trusted path")
    try:
        info = os.stat(candidate)
    except OSError as exc:
        raise RuntimeError("Copilot CLI executable is unavailable") from exc
    owners = {0}
    if hasattr(os, "getuid"):
        owners.add(os.getuid())
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in owners
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not info.st_mode & stat.S_IXUSR
    ):
        raise RuntimeError("Copilot CLI executable is not trusted")
    parent = os.path.dirname(candidate)
    while parent and parent != "/":
        parent_info = os.stat(parent)
        if parent_info.st_uid not in owners or parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError("Copilot CLI executable parent is not trusted")
        parent = os.path.dirname(parent)
    return candidate


def _platform_copilot_candidate(loader):
    if os.path.basename(loader) != "npm-loader.js":
        return loader
    system = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    machine = {"x86_64": "x64", "AMD64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
    if not system or not machine:
        raise RuntimeError("Copilot CLI platform is unsupported")
    account_home = pwd.getpwuid(os.getuid()).pw_dir
    package_root = os.path.join(
        account_home, ".copilot", "pkg", f"{system}-{machine}"
    )
    for directory in (
        os.path.join(account_home, ".copilot"),
        os.path.join(account_home, ".copilot", "pkg"),
        package_root,
    ):
        _cfg.ensure_private_directory(directory, create=False)
    try:
        versions = [
            name for name in os.listdir(package_root)
            if re.fullmatch(r"[1-9][0-9]*\.[0-9]+\.[0-9]+-[0-9]+", name)
        ]
    except OSError as exc:
        raise RuntimeError(
            "Copilot CLI package is unavailable; run copilot --version once"
        ) from exc
    if not versions:
        raise RuntimeError("Copilot CLI package is unavailable")
    versions.sort(
        key=lambda value: tuple(int(part) for part in re.split(r"[.-]", value)),
        reverse=True,
    )
    package = os.path.join(package_root, versions[0])
    _secure_copilot_package_tree(package)
    return os.path.join(package, "index.js")


def _secure_copilot_package_tree(path):
    """Repair and validate one user-owned CLI package without following links."""
    display, root_fd = _cfg._open_private_directory(path, create=False)
    expected_uid = os.getuid() if hasattr(os, "getuid") else None
    budget = {"files": 0, "bytes": 0}

    def secure(directory_fd, relative):
        info = os.fstat(directory_fd)
        if expected_uid is not None and info.st_uid != expected_uid:
            raise RuntimeError("Copilot package directory has the wrong owner")
        os.fchmod(directory_fd, 0o700)
        for name in os.listdir(directory_fd):
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if expected_uid is not None and entry.st_uid != expected_uid:
                raise RuntimeError("Copilot package entry has the wrong owner")
            if stat.S_ISDIR(entry.st_mode):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                child = os.open(name, flags, dir_fd=directory_fd)
                try:
                    secure(child, relative + (name,))
                finally:
                    os.close(child)
            elif stat.S_ISREG(entry.st_mode):
                if entry.st_nlink != 1:
                    raise RuntimeError("Copilot package file has multiple links")
                budget["files"] += 1
                budget["bytes"] += entry.st_size
                if budget["files"] > 20000 or budget["bytes"] > 1024 ** 3:
                    raise RuntimeError("Copilot package exceeds the validation budget")
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                child = os.open(name, flags, dir_fd=directory_fd)
                try:
                    current = os.fstat(child)
                    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                        raise RuntimeError("Copilot package file identity changed")
                    executable = bool(current.st_mode & stat.S_IXUSR)
                    os.fchmod(child, 0o700 if executable else 0o600)
                finally:
                    os.close(child)
            else:
                raise RuntimeError(
                    "Copilot package contains an unsupported entry"
                )

    try:
        secure(root_fd, ())
    finally:
        os.close(root_fd)
    return display


def _resolve_and_preflight_copilot(path):
    loader = _trusted_executable(path)
    executable = _trusted_executable(_platform_copilot_candidate(loader))
    try:
        identity = (executable, os.stat(executable).st_mtime_ns, os.stat(executable).st_size)
    except OSError as exc:
        raise RuntimeError("Copilot CLI executable changed during validation") from exc
    with _COPILOT_PREFLIGHT_LOCK:
        if _COPILOT_PREFLIGHT.get(identity):
            return executable
    env = _cfg.child_process_env(profile="acp")
    try:
        version = subprocess.run(
            [executable, "--version"], capture_output=True, text=True,
            timeout=10, env=env, cwd="/",
        )
        help_result = subprocess.run(
            [executable, "--help"], capture_output=True, text=True,
            timeout=10, env=env, cwd="/",
        )
        contained_help = subprocess.run(
            [
                executable, "--acp", "--stdio", "--disable-builtin-mcps",
                "--no-bash-env", "--no-custom-instructions", "--no-remote",
                "--no-remote-export", "--help",
            ],
            capture_output=True, text=True, timeout=10, env=env, cwd="/",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Copilot CLI preflight failed") from exc
    help_text = (help_result.stdout or "") + (help_result.stderr or "")
    version_text = (version.stdout or "") + (version.stderr or "")
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)(?:[-.]\d+)?\b", version_text)
    if (
        version.returncode != 0 or help_result.returncode != 0
        or contained_help.returncode != 0 or not match
        or tuple(int(part) for part in match.groups()) < (1, 0, 0)
        or any(flag not in help_text for flag in _COPILOT_REQUIRED_FLAGS)
    ):
        raise RuntimeError("Copilot CLI does not satisfy the ACP containment contract")
    with _COPILOT_PREFLIGHT_LOCK:
        _COPILOT_PREFLIGHT[identity] = True
    return executable


def _inherited_disabled_mcp_names(home=None):
    names = {"computer-use-linux"}
    home = home or os.environ.get("COPILOT_HOME") or os.path.expanduser("~/.copilot")
    path = os.path.join(home, "mcp-config.json")
    try:
        with _cfg.open_private_file(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > 1024 * 1024:
                raise RuntimeError("Copilot MCP config is too large to inspect safely")
            raw = json.loads(handle.read().decode("utf-8", errors="strict"))
    except FileNotFoundError:
        return tuple(sorted(names))
    except (OSError, UnicodeDecodeError, _cfg.PrivateStorageError) as exc:
        raise RuntimeError(
            "Copilot MCP config could not be inspected safely"
        ) from exc
    except (ValueError, TypeError, RecursionError) as exc:
        raise RuntimeError(
            "Copilot MCP config could not be inspected safely"
        ) from exc
    if not isinstance(raw, dict):
        raise RuntimeError("Copilot MCP config must be an object")
    servers = raw.get("mcpServers", raw)
    if not isinstance(servers, dict):
        raise RuntimeError("Copilot MCP server config must be an object")
    for name in servers:
        names.add(str(name))
    return tuple(sorted(names))


def _acp_text(value, label, limit=1024, *, nullable=False):
    if nullable and value is None:
        return None
    if (
        not isinstance(value, str) or not value or len(value) > limit
        or re.search(r"[\x00-\x1f\x7f]", value)
    ):
        raise RuntimeError(f"{label} is invalid")
    return value


def _validate_acp_meta(value, *, depth=0, budget=None):
    if value is None:
        return
    if budget is None:
        budget = [0]
    budget[0] += 1
    if depth > 8 or budget[0] > 4096:
        raise RuntimeError("ACP metadata is too complex")
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise RuntimeError("ACP metadata number is invalid")
        return
    if isinstance(value, str):
        if len(value) > 4096 or re.search(r"[\x00-\x1f\x7f]", value):
            raise RuntimeError("ACP metadata text is invalid")
        return
    if isinstance(value, list):
        if len(value) > 128:
            raise RuntimeError("ACP metadata list is too large")
        for item in value:
            _validate_acp_meta(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        if len(value) > 64:
            raise RuntimeError("ACP metadata object is too large")
        for key, item in value.items():
            if (
                not isinstance(key, str) or not key or len(key) > 128
                or re.search(r"[\x00-\x1f\x7f]", key)
            ):
                raise RuntimeError("ACP metadata key is invalid")
            _validate_acp_meta(item, depth=depth + 1, budget=budget)
        return
    raise RuntimeError("ACP metadata value is invalid")


def _closed_optional_booleans(value, allowed, label):
    if not isinstance(value, dict) or not set(value).issubset(allowed | {"_meta"}):
        raise RuntimeError(f"{label} is invalid")
    if "_meta" in value:
        _validate_acp_meta(value["_meta"])
    for key in set(value) - {"_meta"}:
        if not isinstance(value[key], bool):
            raise RuntimeError(f"{label}.{key} is invalid")


def _validate_initialize_result(result, expected_version):
    if not isinstance(result, dict):
        raise RuntimeError("ACP initialize result must be an object")
    allowed = {
        "protocolVersion", "agentInfo", "agentCapabilities", "authMethods"
    }
    if set(result) - allowed or not {
        "protocolVersion", "agentInfo", "agentCapabilities"
    }.issubset(result):
        raise RuntimeError("ACP initialize result is incomplete")
    version = result.get("protocolVersion")
    if isinstance(version, bool) or not isinstance(version, int) or version != expected_version:
        raise RuntimeError("ACP protocol version does not match")
    agent_info = result.get("agentInfo")
    capabilities = result.get("agentCapabilities")
    if (
        not isinstance(agent_info, dict)
        or set(agent_info) - {"name", "title", "version", "_meta"}
        or not {"name", "version"}.issubset(agent_info)
        or not isinstance(capabilities, dict)
        or set(capabilities) - {
            "loadSession", "mcpCapabilities", "promptCapabilities",
            "sessionCapabilities", "_meta",
        }
    ):
        raise RuntimeError("ACP initialize metadata must contain objects")
    safe_info = {
        "name": _acp_text(agent_info["name"], "ACP agentInfo.name", 256),
        "version": _acp_text(
            agent_info["version"], "ACP agentInfo.version", 256
        ),
    }
    if "title" in agent_info:
        safe_info["title"] = _acp_text(
            agent_info["title"], "ACP agentInfo.title", 256, nullable=True
        )
    if "_meta" in agent_info:
        _validate_acp_meta(agent_info["_meta"])
    if "_meta" in capabilities:
        _validate_acp_meta(capabilities["_meta"])
    if "loadSession" in capabilities and not isinstance(
        capabilities["loadSession"], bool
    ):
        raise RuntimeError("ACP loadSession capability is invalid")
    if "mcpCapabilities" in capabilities:
        _closed_optional_booleans(
            capabilities["mcpCapabilities"], {"http", "sse"},
            "ACP mcpCapabilities",
        )
    if "promptCapabilities" in capabilities:
        _closed_optional_booleans(
            capabilities["promptCapabilities"],
            {"audio", "embeddedContext", "image"},
            "ACP promptCapabilities",
        )
    if "sessionCapabilities" in capabilities:
        session_caps = capabilities["sessionCapabilities"]
        if not isinstance(session_caps, dict) or not set(session_caps).issubset({
            "fork", "list", "resume", "_meta"
        }):
            raise RuntimeError("ACP sessionCapabilities is invalid")
        if "_meta" in session_caps:
            _validate_acp_meta(session_caps["_meta"])
        for name in set(session_caps) - {"_meta"}:
            value = session_caps[name]
            if value is not None and (
                not isinstance(value, dict) or set(value) - {"_meta"}
            ):
                raise RuntimeError(f"ACP session capability {name} is invalid")
            if isinstance(value, dict) and "_meta" in value:
                _validate_acp_meta(value["_meta"])
    auth_methods = result.get("authMethods", [])
    if not isinstance(auth_methods, list) or len(auth_methods) > 16:
        raise RuntimeError("ACP authMethods is invalid")
    for method in auth_methods:
        if (
            not isinstance(method, dict)
            or set(method) - {"id", "name", "description", "_meta"}
            or not {"id", "name"}.issubset(method)
        ):
            raise RuntimeError("ACP auth method is invalid")
        _acp_text(method["id"], "ACP auth method id", 256)
        _acp_text(method["name"], "ACP auth method name", 256)
        if "description" in method:
            _acp_text(
                method["description"], "ACP auth method description", 1024,
                nullable=True,
            )
        if "_meta" in method:
            _validate_acp_meta(method["_meta"])
    safe_capabilities = {
        key: value for key, value in capabilities.items() if key != "_meta"
    }
    return safe_info, safe_capabilities


def _validate_session_option(option):
    if not isinstance(option, dict):
        raise RuntimeError("ACP session option is invalid")
    if "group" in option:
        if set(option) - {"group", "name", "options", "_meta"} or not {
            "group", "name", "options"
        }.issubset(option):
            raise RuntimeError("ACP grouped session option is invalid")
        _acp_text(option["group"], "ACP session option group", 256)
        _acp_text(option["name"], "ACP session option name", 256)
        nested = option["options"]
        if not isinstance(nested, list) or len(nested) > 256:
            raise RuntimeError("ACP grouped session options are invalid")
        for item in nested:
            _validate_session_option(item)
    else:
        if set(option) - {"name", "value", "description", "_meta"} or not {
            "name", "value"
        }.issubset(option):
            raise RuntimeError("ACP session option fields are invalid")
        _acp_text(option["name"], "ACP session option name", 256)
        _acp_text(option["value"], "ACP session option value", 512)
        if "description" in option:
            _acp_text(
                option["description"], "ACP session option description",
                2048, nullable=True,
            )
    if "_meta" in option:
        _validate_acp_meta(option["_meta"])


def _validate_config_options(config_options):
    if not isinstance(config_options, list) or len(config_options) > 64:
        raise RuntimeError("ACP configOptions is invalid")
    for option in config_options:
        if (
            not isinstance(option, dict)
            or set(option) - {
                "type", "category", "currentValue", "description", "id",
                "name", "options", "_meta",
            }
            or not {"type", "currentValue", "id", "name", "options"}.issubset(option)
            or option["type"] != "select"
        ):
            raise RuntimeError("ACP config option is invalid")
        for field in ("currentValue", "id", "name"):
            _acp_text(option[field], f"ACP config option {field}", 512)
        for field in ("category", "description"):
            if field in option:
                _acp_text(
                    option[field], f"ACP config option {field}", 2048,
                    nullable=True,
                )
        if "_meta" in option:
            _validate_acp_meta(option["_meta"])
        if not isinstance(option["options"], list) or len(option["options"]) > 256:
            raise RuntimeError("ACP config option choices are invalid")
        for choice in option["options"]:
            _validate_session_option(choice)


def _validate_session_result(result):
    if not isinstance(result, dict) or set(result) - {
        "sessionId", "configOptions", "models", "modes", "_meta"
    } or "sessionId" not in result:
        raise RuntimeError("ACP session result must be a closed object")
    if len(json.dumps(result, allow_nan=False).encode("utf-8")) > 1024 * 1024:
        raise RuntimeError("ACP session result is too large")
    if "_meta" in result:
        _validate_acp_meta(result["_meta"])
    session_id = _acp_text(result["sessionId"], "ACP sessionId", 256)
    if session_id != session_id.strip():
        raise RuntimeError("ACP sessionId is invalid")
    config_options = result.get("configOptions")
    if config_options is not None:
        _validate_config_options(config_options)
    models = result.get("models")
    if models is not None:
        if (
            not isinstance(models, dict)
            or set(models) - {"currentModelId", "availableModels", "_meta"}
            or not {"currentModelId", "availableModels"}.issubset(models)
        ):
            raise RuntimeError("ACP models catalog is invalid")
        _acp_text(models["currentModelId"], "ACP currentModelId", 512)
        if "_meta" in models:
            _validate_acp_meta(models["_meta"])
        rows = models["availableModels"]
        if not isinstance(rows, list) or len(rows) > 256:
            raise RuntimeError("ACP models catalog is too large")
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) - {"modelId", "name", "description", "_meta"}
                or not {"modelId", "name"}.issubset(row)
            ):
                raise RuntimeError("ACP model entry is invalid")
            _acp_text(row["modelId"], "ACP modelId", 512)
            _acp_text(row["name"], "ACP model name", 256)
            if "description" in row:
                _acp_text(
                    row["description"], "ACP model description", 2048,
                    nullable=True,
                )
            if "_meta" in row:
                _validate_acp_meta(row["_meta"])
    modes = result.get("modes")
    if modes is not None:
        if (
            not isinstance(modes, dict)
            or set(modes) - {"currentModeId", "availableModes", "_meta"}
            or not {"currentModeId", "availableModes"}.issubset(modes)
        ):
            raise RuntimeError("ACP modes catalog is invalid")
        _acp_text(modes["currentModeId"], "ACP currentModeId", 512)
        if "_meta" in modes:
            _validate_acp_meta(modes["_meta"])
        rows = modes["availableModes"]
        if not isinstance(rows, list) or len(rows) > 32:
            raise RuntimeError("ACP modes catalog is too large")
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) - {"id", "name", "description", "_meta"}
                or not {"id", "name"}.issubset(row)
            ):
                raise RuntimeError("ACP mode entry is invalid")
            _acp_text(row["id"], "ACP mode id", 512)
            _acp_text(row["name"], "ACP mode name", 256)
            if "description" in row:
                _acp_text(
                    row["description"], "ACP mode description", 2048,
                    nullable=True,
                )
            if "_meta" in row:
                _validate_acp_meta(row["_meta"])
    return session_id


def _validate_session_update_params(params, expected_session_id):
    if not isinstance(params, dict) or set(params) != {"sessionId", "update"}:
        raise RuntimeError("ACP session/update params are invalid")
    if (
        not isinstance(expected_session_id, str) or not expected_session_id
        or params.get("sessionId") != expected_session_id
    ):
        raise RuntimeError("ACP session/update does not match the active session")
    update = params.get("update")
    if not isinstance(update, dict):
        raise RuntimeError("ACP session/update payload must be an object")
    update_type = update.get("sessionUpdate")
    if not isinstance(update_type, str) or not update_type or len(update_type) > 128:
        raise RuntimeError("ACP session/update type is invalid")
    if update_type in (
        "user_message_chunk", "agent_message_chunk", "agent_thought_chunk"
    ):
        if set(update) != {"sessionUpdate", "content"}:
            raise RuntimeError("ACP text chunk fields are invalid")
        content = update.get("content")
        if (
            not isinstance(content, dict)
            or set(content) != {"type", "text"}
            or content.get("type") != "text"
            or not isinstance(content.get("text"), str)
            or len(content["text"]) > _ACP_FRAME_MAX_BYTES
        ):
            raise RuntimeError("ACP text chunk is invalid")
    elif update_type == "plan":
        if set(update) != {"sessionUpdate", "entries"}:
            raise RuntimeError("ACP plan fields are invalid")
        entries = update.get("entries", [])
        if not isinstance(entries, list) or len(entries) > 256:
            raise RuntimeError("ACP plan entries are invalid")
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not set(entry).issubset({"content", "priority", "status"})
                or "content" not in entry
                or not isinstance(entry.get("content", ""), str)
                or len(entry.get("content", "")) > 4096
                or entry.get("priority", "medium") not in ("low", "medium", "high")
                or entry.get("status", "pending") not in (
                    "pending", "in_progress", "completed"
                )
            ):
                raise RuntimeError("ACP plan entry is invalid")
    elif update_type in ("tool_call", "tool_call_update"):
        allowed = {
            "sessionUpdate", "toolCallId", "title", "kind", "status",
            "content", "locations", "rawInput", "rawOutput",
        }
        if not set(update).issubset(allowed) or "toolCallId" not in update:
            raise RuntimeError("ACP tool update fields are invalid")
        tool_call_id = update.get("toolCallId")
        if (
            not isinstance(tool_call_id, str) or not tool_call_id
            or len(tool_call_id) > 256
            or re.search(r"[\x00-\x1f\x7f]", tool_call_id)
        ):
            raise RuntimeError("ACP tool call identity is invalid")
        for field in ("status", "title"):
            value = update.get(field, "")
            if not isinstance(value, str) or len(value) > 1024:
                raise RuntimeError(f"ACP tool update {field} is invalid")
        for field in ("kind",):
            value = update.get(field, "")
            if not isinstance(value, str) or len(value) > 128:
                raise RuntimeError("ACP tool update kind is invalid")
        for field in ("content", "locations", "rawInput", "rawOutput"):
            if field not in update:
                continue
            try:
                encoded = json.dumps(
                    update[field], allow_nan=False,
                    separators=(",", ":"), ensure_ascii=True,
                ).encode("utf-8")
            except (TypeError, ValueError, RecursionError) as exc:
                raise RuntimeError("ACP tool update metadata is invalid") from exc
            if len(encoded) > 64 * 1024:
                raise RuntimeError("ACP tool update metadata is too large")
    elif update_type == "usage_update":
        if set(update) - {"sessionUpdate", "size", "used", "cost", "_meta"} or not {
            "sessionUpdate", "size", "used"
        }.issubset(update):
            raise RuntimeError("ACP usage update fields are invalid")
        for field in ("size", "used"):
            value = update[field]
            if (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or value < 0
                or value > 10 ** 15
            ):
                raise RuntimeError(f"ACP usage update {field} is invalid")
        cost = update.get("cost")
        if cost is not None:
            if (
                not isinstance(cost, dict)
                or set(cost) != {"amount", "currency"}
                or isinstance(cost.get("amount"), bool)
                or not isinstance(cost.get("amount"), (int, float))
                or not math.isfinite(float(cost["amount"]))
                or cost["amount"] < 0 or cost["amount"] > 10 ** 12
                or not isinstance(cost.get("currency"), str)
                or re.fullmatch(r"[A-Z]{3}", cost["currency"]) is None
            ):
                raise RuntimeError("ACP usage update cost is invalid")
        if "_meta" in update:
            _validate_acp_meta(update["_meta"])
    elif update_type == "available_commands_update":
        if set(update) - {"sessionUpdate", "availableCommands", "_meta"} or not {
            "sessionUpdate", "availableCommands"
        }.issubset(update):
            raise RuntimeError("ACP available commands update is invalid")
        commands = update["availableCommands"]
        if not isinstance(commands, list) or len(commands) > 128:
            raise RuntimeError("ACP available commands list is invalid")
        for command in commands:
            if (
                not isinstance(command, dict)
                or set(command) - {"name", "description", "input", "_meta"}
                or not {"name", "description"}.issubset(command)
            ):
                raise RuntimeError("ACP available command is invalid")
            _acp_text(command["name"], "ACP command name", 256)
            _acp_text(command["description"], "ACP command description", 2048)
            if "input" in command and command["input"] is not None:
                _validate_acp_meta(command["input"])
            if "_meta" in command:
                _validate_acp_meta(command["_meta"])
        if "_meta" in update:
            _validate_acp_meta(update["_meta"])
    elif update_type == "current_mode_update":
        if set(update) - {"sessionUpdate", "currentModeId", "_meta"} or not {
            "sessionUpdate", "currentModeId"
        }.issubset(update):
            raise RuntimeError("ACP current mode update is invalid")
        _acp_text(update["currentModeId"], "ACP current mode id", 512)
        if "_meta" in update:
            _validate_acp_meta(update["_meta"])
    elif update_type == "config_option_update":
        if set(update) - {"sessionUpdate", "configOptions", "_meta"} or not {
            "sessionUpdate", "configOptions"
        }.issubset(update):
            raise RuntimeError("ACP config option update is invalid")
        options = update["configOptions"]
        _validate_config_options(options)
        if "_meta" in update:
            _validate_acp_meta(update["_meta"])
    elif update_type == "session_info_update":
        if set(update) - {"sessionUpdate", "title", "updatedAt", "_meta"}:
            raise RuntimeError("ACP session info update is invalid")
        for field in ("title", "updatedAt"):
            if field in update:
                _acp_text(
                    update[field], f"ACP session info {field}", 1024,
                    nullable=True,
                )
        if "_meta" in update:
            _validate_acp_meta(update["_meta"])
    else:
        raise RuntimeError("ACP session/update type is unsupported")
    return params


def _validate_permission_params(params, expected_session_id):
    if (
        not isinstance(params, dict)
        or set(params) != {"sessionId", "toolCall", "options"}
        or not isinstance(params.get("toolCall"), dict)
        or not isinstance(params.get("options"), list)
        or not params["options"]
        or len(params["options"]) > 64
    ):
        raise RuntimeError("ACP permission params are invalid")
    if (
        not isinstance(expected_session_id, str) or not expected_session_id
        or params.get("sessionId") != expected_session_id
    ):
        raise RuntimeError("ACP permission does not match the active session")
    tool_call = params["toolCall"]
    if not isinstance(tool_call.get("toolCallId"), str) or not tool_call["toolCallId"]:
        raise RuntimeError("ACP permission toolCallId is invalid")
    for field in ("kind", "status"):
        value = tool_call.get(field, "")
        if not isinstance(value, str) or len(value) > 128:
            raise RuntimeError(f"ACP permission tool {field} is invalid")
    for option in params["options"]:
        if (
            not isinstance(option, dict)
            or not isinstance(option.get("optionId"), str)
            or not option["optionId"] or len(option["optionId"]) > 256
            or not isinstance(option.get("kind"), str)
            or len(option["kind"]) > 64
        ):
            raise RuntimeError("ACP permission option is invalid")
    return params


def _validate_prompt_result(result):
    if (
        not isinstance(result, dict)
        or set(result) != {"stopReason"}
        or result.get("stopReason") not in _ACP_STOP_REASONS
    ):
        raise RuntimeError("ACP session/prompt result is invalid")
    return result["stopReason"]

class ACPClient:
    """Manages the copilot --acp --stdio subprocess and ACP JSON-RPC protocol."""

    PROTOCOL_VERSION = 1  # ACP protocol major version

    def __init__(self, copilot_path="copilot", cwd=None, model=None, mcp_config=None):
        self.copilot_path = copilot_path
        self.cwd = cwd or os.getcwd()
        self.model = model  # None = use CLI default
        self.mcp_config = mcp_config or {}  # MCP servers config dict
        self.process = None
        self.request_id = 0
        self.lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.RLock()
        self.pending = {}           # id -> {"event": Event, "result": None, "error": None}
        self.session_id = None
        self.response_chunks = {}   # prompt_id -> accumulated text
        self.response_chunk_bytes = {}  # prompt_id -> accumulated UTF-8 bytes
        self.reader_thread = None
        self.agent_info = {}
        self.alive = False
        self.terminals = {}  # terminal_id -> {"process": Popen, "output": str}
        self._prompt_lock = threading.Lock()  # Serialize prompt calls
        # Terminal authority remains disabled until the complete ACP terminal
        # contract is implemented behind Eva's capability broker.
        self._terminal_allowed = False
        self._source_copilot_home = (
            os.environ.get("COPILOT_HOME") or os.path.expanduser("~/.copilot")
        )
        self._runtime_dir = None
        self._runtime_home = None
        self._runtime_os_home = None
        self._runtime_cwd = None
        self._stderr_reported = False
        self._process_group_id = None
        self._stop_lock = threading.RLock()
        self._creating_session = False
        self._staged_session_id = None

    def _prepare_isolated_runtime(self):
        if self._runtime_dir:
            return
        _cfg.ensure_private_directory(_ACP_RUNTIME_DIR)
        root_name = f"copilot-acp-{os.getpid()}-" + os.urandom(16).hex()
        root = _cfg.ensure_private_directory(
            os.path.join(_ACP_RUNTIME_DIR, root_name)
        )
        runtime_home = os.path.join(root, "home")
        runtime_os_home = os.path.join(root, "os-home")
        runtime_cwd = os.path.join(root, "workspace")
        _cfg.ensure_private_directory(runtime_home)
        _cfg.ensure_private_directory(runtime_os_home)
        _cfg.ensure_private_directory(runtime_cwd)
        runtime_os_copilot = os.path.join(runtime_os_home, ".copilot")
        _cfg.ensure_private_directory(runtime_os_copilot)

        auth_config = os.path.join(self._source_copilot_home, "config.json")
        try:
            projection = _auth_projection(auth_config)
            if projection is not None:
                for output_path in (
                    os.path.join(runtime_home, "config.json"),
                    os.path.join(runtime_os_copilot, "config.json"),
                ):
                    with _cfg.open_private_file(
                        output_path, "x", encoding="utf-8"
                    ) as handle:
                        json.dump(
                            projection, handle, sort_keys=True,
                            separators=(",", ":"),
                        )
                        handle.flush()
                        os.fsync(handle.fileno())
        except Exception:
            try:
                _cfg.remove_private_subdirectory(_ACP_RUNTIME_DIR, root_name)
            except (FileNotFoundError, OSError, _cfg.PrivateStorageError):
                pass
            raise

        self._runtime_dir = root
        self._runtime_home = runtime_home
        self._runtime_os_home = runtime_os_home
        self._runtime_cwd = runtime_cwd

    def _cleanup_isolated_runtime(self):
        root = self._runtime_dir
        if root:
            try:
                _cfg.remove_private_subdirectory(
                    _ACP_RUNTIME_DIR, os.path.basename(root)
                )
            except FileNotFoundError:
                pass
            except (OSError, _cfg.PrivateStorageError):
                return False
        self._runtime_dir = None
        self._runtime_home = None
        self._runtime_os_home = None
        self._runtime_cwd = None
        return True

    def _session_mcp_servers(self):
        servers = []
        for name, cfg in self.mcp_config.items():
            env = []
            for key, value in sorted((cfg.get("env") or {}).items()):
                if str(key).startswith("_"):
                    continue
                if not isinstance(value, str):
                    raise RuntimeError("MCP environment values must be strings")
                env.append({"name": str(key), "value": value})
            servers.append({
                "name": name,
                "command": cfg["command"],
                "args": list(cfg["args"]),
                "env": env,
            })
        return servers

    # --- Lifecycle ---

    def start(self):
        """Spawn copilot subprocess, initialize ACP, create session."""
        safe_mcp, rejected_mcp = _cfg.mcp_config_for_egress(
            self.mcp_config, _st.egress_mode
        )
        if rejected_mcp:
            raise RuntimeError(
                "MCP process policy rejected server(s): "
                + ", ".join(sorted(rejected_mcp))
            )
        self.mcp_config = safe_mcp
        if os.name == "nt":
            raise RuntimeError(
                "ACP MCP isolation is unavailable on Windows in this release"
            )
        inherited_names = _inherited_disabled_mcp_names(
            self._source_copilot_home
        )
        executable = _resolve_and_preflight_copilot(self.copilot_path)
        self._prepare_isolated_runtime()
        cmd = [
            executable, "--acp", "--stdio", "--disable-builtin-mcps",
            "--no-bash-env",
            "--no-custom-instructions", "--no-remote", "--no-remote-export",
        ]
        for server_name in inherited_names:
            cmd.extend(["--disable-mcp-server", server_name])
        if self.model:
            cmd.extend(["--model", self.model])
        try:
            process_env = _cfg.child_process_env(profile="acp")
            process_env["COPILOT_HOME"] = self._runtime_home
            process_env["HOME"] = self._runtime_os_home

            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=process_env,
                cwd=self._runtime_cwd,
                start_new_session=True,
            )
            if type(self.process.pid) is int and self.process.pid > 0:
                self._process_group_id = self.process.pid
        except FileNotFoundError:
            self._cleanup_isolated_runtime()
            raise RuntimeError(
                f"Copilot CLI not found at '{self.copilot_path}'. "
                "Install it (https://github.com/github/copilot-cli) and authenticate with 'copilot auth login'."
            )
        except Exception:
            self._cleanup_isolated_runtime()
            raise

        try:
            self.alive = True

            self.reader_thread = threading.Thread(
                target=self._read_loop, daemon=True
            )
            self.reader_thread.start()
            threading.Thread(target=self._stderr_loop, daemon=True).start()

            init_result = self._send_request("initialize", {
                "protocolVersion": self.PROTOCOL_VERSION,
                "clientCapabilities": {
                    "terminal": self._terminal_allowed
                },
                "clientInfo": {
                    "name": "eva-acp-bridge",
                    "title": "Eva ACP Bridge",
                    "version": "1.0.0"
                }
            }, timeout=30)

            self.agent_info, caps = _validate_initialize_result(
                init_result, self.PROTOCOL_VERSION
            )
            print(f"[ACP] Handshake validated ({len(caps)} capability groups)")

            mcp_servers_for_session = self._session_mcp_servers()
            self._creating_session = True
            self._staged_session_id = None
            try:
                session_result = self._send_request("session/new", {
                    "cwd": self._runtime_cwd,
                    "mcpServers": mcp_servers_for_session
                }, timeout=30)
                session_id = _validate_session_result(session_result)
                if (
                    self._staged_session_id is not None
                    and self._staged_session_id != session_id
                ):
                    raise RuntimeError("ACP staged session identity changed")
                self.session_id = session_id
            finally:
                self._creating_session = False
                self._staged_session_id = None
            print("[ACP] Session created")
        except Exception as exc:
            try:
                self.stop()
            except Exception:
                self.alive = False
                self.session_id = None
                self._cleanup_isolated_runtime()
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError("Copilot ACP handshake failed") from exc

    def stop(self):
        """Shut down the copilot subprocess."""
        with self._stop_lock:
            self.alive = False
            self.session_id = None
            self._current_prompt_id = None
            self._creating_session = False
            self._staged_session_id = None
            process = self.process
            group_id = self._process_group_id
            if process:
                try:
                    if process.stdin:
                        process.stdin.close()
                except Exception:
                    pass
            group_gone = group_id is None
            if group_id is not None:
                try:
                    os.killpg(group_id, signal.SIGTERM)
                except ProcessLookupError:
                    group_gone = True
                except OSError:
                    pass
            elif process:
                try:
                    process.terminate()
                except Exception:
                    pass
            if process:
                try:
                    process.wait(timeout=5)
                except Exception:
                    pass
            if group_id is not None and not group_gone:
                try:
                    os.killpg(group_id, 0)
                except ProcessLookupError:
                    group_gone = True
                except OSError:
                    pass
            if group_id is not None and not group_gone:
                try:
                    os.killpg(group_id, signal.SIGKILL)
                except ProcessLookupError:
                    group_gone = True
                except OSError:
                    pass
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    try:
                        os.killpg(group_id, 0)
                    except ProcessLookupError:
                        group_gone = True
                        break
                    except OSError:
                        break
                    time.sleep(0.02)
            if process and not group_gone:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except Exception:
                    pass
            if group_gone:
                self._process_group_id = None
                self._cleanup_isolated_runtime()

    # --- JSON-RPC Communication ---

    def _next_id(self):
        with self.lock:
            self.request_id += 1
            return self.request_id

    def _send_request(self, method, params, timeout=120):
        """Send a JSON-RPC request and wait for the response."""
        rid = self._next_id()
        event = threading.Event()
        with self._pending_lock:
            self.pending[rid] = {
                "event": event, "result": None, "error": None,
                "completed": False,
            }

        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params
        }, allow_nan=False) + "\n"

        try:
            self._write_raw(msg)
        except (BrokenPipeError, OSError, RuntimeError) as e:
            with self._pending_lock:
                self.pending.pop(rid, None)
            self._cancel_and_quarantine(f"request pipe failure: {method}")
            raise ACPRequestError("Copilot process pipe error") from e

        completed = event.wait(timeout=timeout)
        if not completed:
            with self._pending_lock:
                self.pending.pop(rid, None)
            self._cancel_and_quarantine(f"request timeout: {method}")
            raise ACPRequestError(
                f"Copilot request timed out after {timeout}s"
            )

        with self._pending_lock:
            entry = self.pending.pop(rid, {})
        if entry.get("error"):
            raise ACPRequestError("Copilot JSON-RPC request failed")
        return entry.get("result")

    def _write_raw(self, text):
        """Write one complete NDJSON frame without interleaving writers."""
        if not self.process or not self.process.stdin or not self.alive:
            raise RuntimeError("Copilot process is not active")
        if not isinstance(text, str) or not text.endswith("\n") or text.count("\n") != 1:
            raise RuntimeError("ACP outbound frame is invalid")
        encoded = text.encode("utf-8", errors="strict")
        if not encoded or len(encoded) > _ACP_FRAME_MAX_BYTES:
            raise RuntimeError("ACP outbound frame exceeds the bounded line limit")
        with self._write_lock:
            self.process.stdin.write(encoded)
            self.process.stdin.flush()

    def _cancel_and_quarantine(self, reason):
        """Best-effort cancel, then retire a timed-out ACP session.

        ACP updates do not carry a prompt identifier Eva can safely use for
        late-chunk routing. A timed-out session is therefore never reused.
        """
        session_id = self.session_id
        if session_id and self.process and self.process.stdin and self.alive:
            notice = json.dumps({
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": session_id},
            }, allow_nan=False) + "\n"
            try:
                self._write_raw(notice)
            except (BrokenPipeError, OSError, RuntimeError):
                pass
        print("[ACP] Quarantining session")
        self.stop()

    def _send_response(self, rid, result):
        """Send a JSON-RPC response (for server-initiated requests like requestPermission)."""
        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "result": result
        }, allow_nan=False) + "\n"
        try:
            self._write_raw(msg)
        except (BrokenPipeError, OSError, RuntimeError):
            pass

    def _send_error_response(self, rid, code, message):
        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "error": {"code": code, "message": message},
        }, allow_nan=False) + "\n"
        try:
            self._write_raw(msg)
        except (BrokenPipeError, OSError, RuntimeError):
            pass

    # --- Reader Loop ---

    def _read_loop(self):
        """Continuously read NDJSON lines from copilot stdout."""
        while self.alive:
            try:
                line = self.process.stdout.readline(_ACP_FRAME_MAX_BYTES + 1)
                if not line:
                    print("[ACP] Copilot stdout closed")
                    # Unblock any pending requests
                    with self._pending_lock:
                        for rid in list(self.pending):
                            entry = self.pending[rid]
                            if not entry.get("completed", False):
                                entry["error"] = "Copilot process exited"
                                entry["completed"] = True
                                entry["event"].set()
                    self.stop()
                    break
                if len(line) > _ACP_FRAME_MAX_BYTES or not line.endswith(b"\n"):
                    self._protocol_violation("ACP frame exceeds the bounded line limit")
                    break
                if not line.strip():
                    continue
                try:
                    msg = _decode_json_rpc_frame(line)
                    self._handle_message(msg)
                except Exception as exc:
                    self._protocol_violation(str(exc)[:200] or "invalid ACP frame")
                    break
            except Exception as e:
                self._protocol_violation(
                    "ACP reader failure: " + str(e)[:160]
                )
                break

    def _stderr_loop(self):
        """Read copilot stderr for debug output."""
        while self.alive:
            try:
                line = self.process.stderr.readline(_ACP_FRAME_MAX_BYTES + 1)
                if not line:
                    break
                if len(line) > _ACP_FRAME_MAX_BYTES or not line.endswith(b"\n"):
                    self._protocol_violation("ACP stderr line exceeded the byte limit")
                    break
                if not self._stderr_reported:
                    self._stderr_reported = True
                    print("[ACP] Child stderr output suppressed")
            except Exception:
                break

    def _handle_message(self, msg):
        """Route incoming JSON-RPC messages."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            self._protocol_violation("invalid JSON-RPC envelope")
            return
        has_id = "id" in msg
        rid = msg.get("id")
        if has_id and (type(rid) is not int or not 1 <= rid <= 2147483647):
            self._protocol_violation("invalid JSON-RPC id")
            return
        has_result = "result" in msg
        has_error = "error" in msg
        has_method = "method" in msg

        if has_result or has_error:
            expected_fields = (
                {"jsonrpc", "id", "result"}
                if has_result and not has_error else
                {"jsonrpc", "id", "error"}
                if has_error and not has_result else set()
            )
            if not expected_fields or set(msg) != expected_fields:
                self._protocol_violation("invalid JSON-RPC response")
                return
            if has_error:
                error = msg["error"]
                if (
                    not isinstance(error, dict)
                    or set(error) - {"code", "message", "data"}
                    or type(error.get("code")) is not int
                    or not isinstance(error.get("message"), str)
                    or not error["message"] or len(error["message"]) > 2000
                ):
                    self._protocol_violation("invalid JSON-RPC error")
                    return
            with self._pending_lock:
                entry = self.pending.get(rid)
                if entry is None or entry.get("completed", False):
                    duplicate = True
                else:
                    duplicate = False
                    entry["completed"] = True
                    if has_error:
                        entry["error"] = error
                    else:
                        entry["result"] = msg["result"]
                    entry["event"].set()
            if duplicate:
                self._protocol_violation("duplicate or unknown JSON-RPC response")
            return

        if not has_method or not isinstance(msg.get("method"), str):
            self._protocol_violation("invalid JSON-RPC request")
            return
        expected_fields = {"jsonrpc", "method", "params"}
        if has_id:
            expected_fields.add("id")
        if set(msg) != expected_fields or not isinstance(msg.get("params"), dict):
            self._protocol_violation("invalid JSON-RPC request fields")
            return

        # Notification: session/update
        if msg.get("method") == "session/update":
            if has_id:
                self._protocol_violation("session/update must be a notification")
                return
            try:
                expected_session = self.session_id
                if not expected_session and self._creating_session:
                    candidate = _acp_text(
                        msg["params"].get("sessionId"),
                        "ACP staged sessionId", 256,
                    )
                    if (
                        self._staged_session_id is not None
                        and self._staged_session_id != candidate
                    ):
                        raise RuntimeError("ACP staged session identity changed")
                    self._staged_session_id = candidate
                    expected_session = candidate
                params = _validate_session_update_params(
                    msg["params"], expected_session
                )
                self._handle_session_update(params)
            except Exception as exc:
                self._protocol_violation(str(exc)[:200])
            return

        # Server-initiated request: session/request_permission
        if "id" in msg and msg.get("method") == "session/request_permission":
            try:
                params = _validate_permission_params(
                    msg["params"], self.session_id
                )
            except Exception as exc:
                self._protocol_violation(str(exc)[:200])
                return
            options = params.get("options", []) if isinstance(params.get("options"), list) else []

            reject_option = next((
                opt for opt in options
                if isinstance(opt, dict) and opt.get("kind") in ("reject_once", "reject_always") and opt.get("optionId")
            ), None)
            if reject_option:
                print("[ACP] Permission rejected")
                outcome = {"outcome": "selected", "optionId": reject_option["optionId"]}
            else:
                print("[ACP] Permission cancelled")
                outcome = {"outcome": "cancelled"}
            self._send_response(msg["id"], {"outcome": outcome})
            return

        # Server-initiated requests for terminal
        if "id" in msg and msg.get("method") == "terminal/create":
            print("[ACP] terminal/create DENIED (terminal capability disabled)")
            self._send_error_response(msg["id"], -32601, "Terminal capability is disabled")
            return

        if "id" in msg and msg.get("method") == "terminal/output":
            self._send_error_response(msg["id"], -32601, "Terminal capability is disabled")
            return

        if "id" in msg and msg.get("method") == "terminal/release":
            self._send_error_response(msg["id"], -32601, "Terminal capability is disabled")
            return

        if "id" in msg and msg.get("method", "").startswith("terminal/"):
            self._send_error_response(msg["id"], -32601, "Terminal capability is disabled")
            return

        # Server-initiated requests for fs (decline)
        if "id" in msg and msg.get("method", "").startswith("fs/"):
            print("[ACP] Declining unsupported capability request")
            self._send_error_response(msg["id"], -32601, "Method not supported by bridge")
            return

        # Unknown message
        if "id" in msg and "method" in msg:
            # Unknown server request — respond with error
            print("[ACP] Unknown server request rejected")
            self._send_error_response(msg["id"], -32601, "Not implemented")
            return
        self._protocol_violation("unsupported ACP notification")

    def _protocol_violation(self, reason):
        with self._pending_lock:
            for entry in list(self.pending.values()):
                if not entry.get("completed", False):
                    entry["error"] = {"code": -32600, "message": reason}
                    entry["completed"] = True
                    entry["event"].set()
        print("[ACP] Protocol violation")
        self.stop()

    def _handle_session_update(self, params):
        """Accumulate text from agent_message_chunk updates."""
        update = params.get("update", {})
        update_type = update.get("sessionUpdate", "")

        if update_type == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text":
                text = content.get("text", "")
                # Accumulate into current prompt's response
                if "_current_prompt_id" in self.__dict__ and self._current_prompt_id:
                    pid = self._current_prompt_id
                    if pid not in self.response_chunks:
                        self.response_chunks[pid] = ""
                        self.response_chunk_bytes[pid] = 0
                    new_bytes = len(text.encode("utf-8"))
                    total = self.response_chunk_bytes.get(pid, 0) + new_bytes
                    if total > _ACP_RESPONSE_MAX_BYTES:
                        raise RuntimeError("ACP response exceeded the cumulative byte limit")
                    self.response_chunk_bytes[pid] = total
                    self.response_chunks[pid] += text

        elif update_type == "agent_thought_chunk":
            # Reasoning is intentionally neither surfaced nor persisted.
            return

        elif update_type == "usage_update":
            # Usage is bounded/validated at the trust boundary, then ignored.
            return

        elif update_type in (
            "user_message_chunk", "available_commands_update",
            "current_mode_update", "config_option_update",
            "session_info_update",
        ):
            # Catalog/session metadata is bounded and intentionally non-authoritative.
            return

        elif update_type == "plan":
            entries = update.get("entries", [])
            if entries:
                print(f"[ACP] Agent plan received ({len(entries)} entries)")

        elif update_type in ("tool_call", "tool_call_update"):
            status = update.get("status", "")
            title = update.get("title", "")
            if title or status:
                print("[ACP] Tool status update received")

    # --- Terminal handlers (for ACP tool execution) ---

    def _handle_terminal_create(self, rid, params):
        """Terminal execution is unavailable until brokered."""
        self._send_error_response(rid, -32601, "Terminal capability is disabled")

    def _handle_terminal_output(self, rid, params):
        """Terminal output is unavailable until brokered."""
        self._send_error_response(rid, -32601, "Terminal capability is disabled")

    def _handle_terminal_release(self, rid, params):
        """Terminal release is unavailable until brokered."""
        self._send_error_response(rid, -32601, "Terminal capability is disabled")

    # --- Public API ---

    def prompt(self, text, timeout=120):
        """Send a text prompt and return the accumulated response text.
        Serialized per client to prevent cross-talk between concurrent callers."""
        with _st.mode_mcp_transition_lock:
            if _st.local_mode:
                return {"error": "ACP is disabled while local mode is active"}
            with self._prompt_lock:
                if _st.local_mode:
                    return {"error": "ACP is disabled while local mode is active"}
                return self._prompt_impl(text, timeout)

    def _prompt_impl(self, text, timeout=120):
        if not self.alive or not self.session_id:
            return {"error": "No active ACP session"}

        pid = self._next_id()
        self._current_prompt_id = pid
        self.response_chunks[pid] = ""
        self.response_chunk_bytes[pid] = 0

        _t0 = time.perf_counter()
        try:
            try:
                result = self._send_request("session/prompt", {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": text}]
                }, timeout=timeout)
            except ACPRequestError as exc:
                return {"error": str(exc)}
        finally:
            response_text = self.response_chunks.pop(pid, "")
            self.response_chunk_bytes.pop(pid, None)
            if self._current_prompt_id == pid:
                self._current_prompt_id = None
        _ms = round((time.perf_counter() - _t0) * 1000.0, 1)

        if result and isinstance(result, dict):
            try:
                stop_reason = _validate_prompt_result(result)
            except RuntimeError as exc:
                self._cancel_and_quarantine(str(exc))
                return {"error": str(exc)}
            _telemetry_emit("acp_prompt", model=self.model or "default",
                            prompt_chars=len(text or ""), response_chars=len(response_text or ""),
                            ms=_ms, stop_reason=stop_reason)
            return {"text": response_text, "stop_reason": stop_reason}

        self._cancel_and_quarantine("ACP session/prompt result is invalid")
        return {"error": "ACP session/prompt result is invalid"}

    def prompt_with_image(self, text, image_b64, mime="image/jpeg", timeout=120):
        """Send a text + image prompt (serialized)."""
        with _st.mode_mcp_transition_lock:
            if _st.local_mode:
                return {"error": "ACP is disabled while local mode is active"}
            with self._prompt_lock:
                if _st.local_mode:
                    return {"error": "ACP is disabled while local mode is active"}
                return self._prompt_with_image_impl(text, image_b64, mime, timeout)

    def _prompt_with_image_impl(self, text, image_b64, mime="image/jpeg", timeout=120):
        if not self.session_id:
            return {"error": "No active ACP session"}

        pid = self._next_id()
        self._current_prompt_id = pid
        self.response_chunks[pid] = ""
        self.response_chunk_bytes[pid] = 0

        _t0 = time.perf_counter()
        try:
            try:
                result = self._send_request("session/prompt", {
                    "sessionId": self.session_id,
                    "prompt": [
                        {"type": "text", "text": text},
                        {"type": "image", "data": image_b64, "mimeType": mime},
                    ]
                }, timeout=timeout)
            except ACPRequestError as exc:
                return {"error": str(exc)}
        finally:
            response_text = self.response_chunks.pop(pid, "")
            self.response_chunk_bytes.pop(pid, None)
            if self._current_prompt_id == pid:
                self._current_prompt_id = None
        _ms = round((time.perf_counter() - _t0) * 1000.0, 1)

        if result and isinstance(result, dict):
            try:
                stop_reason = _validate_prompt_result(result)
            except RuntimeError as exc:
                self._cancel_and_quarantine(str(exc))
                return {"error": str(exc)}
            _telemetry_emit("acp_vision", model=self.model or "default",
                            prompt_chars=len(text or ""), response_chars=len(response_text or ""),
                            ms=_ms, stop_reason=stop_reason)
            return {"text": response_text, "stop_reason": stop_reason}

        self._cancel_and_quarantine("ACP session/prompt result is invalid")
        return {"error": "ACP session/prompt result is invalid"}


# ---------------------------------------------------------------------------
# Token cache helper
# ---------------------------------------------------------------------------


def _acp_model_key(model):
    """Normalize a model name into a pool key. Empty/None -> the CLI default."""
    return (model or "").strip() or "__default__"



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
    key = _acp_model_key(client.model)
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
            print("[Bridge] Evicting warm ACP client")
            _telemetry_emit("acp_pool", result="evict", model=victim_key, pool_size=len(_st.acp_pool))
            try:
                victim.stop()
            except Exception:
                pass



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



def _ensure_acp_model(requested_model):
    with _st.mode_mcp_transition_lock:
        return _ensure_acp_model_locked(requested_model)


def _ensure_acp_model_locked(requested_model):
    """Ensure a warm ACP client for requested_model is selected as _st.acp_client.

    Uses a warm pool so switching between the cognition draft model and the
    reviewer model reuses a live Copilot CLI instead of respawning it every turn.
    Returns (ok, model_or_error)."""
    # global statement removed — writes go to _st.*
    if _st.local_mode:
        return False, "ACP is disabled while local mode is active"

    with _st.acp_pool_lock:
        # Seed the pool with the startup singleton on first use.
        if _st.acp_client and _acp_model_key(_st.acp_client.model) not in _st.acp_pool:
            _acp_pool_register(_st.acp_client)

        if not _st.acp_client and not _st.acp_pool:
            return False, "ACP bridge not connected to Copilot"

        key = _acp_model_key(requested_model)

        # Fast path: a live warm client already exists for this model.
        existing = _st.acp_pool.get(key)
        if existing and existing.alive:
            _st.acp_client = existing
            _acp_pool_touch(key)
            _telemetry_emit("acp_pool", result="hit", model=key, pool_size=len(_st.acp_pool))
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
        if template is None:
            return False, "ACP bridge not connected to Copilot"

        if requested_model:
            print("[Bridge] Warming requested ACP client")
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
                mcp_config=_inject_kusto_token(template.mcp_config),
            )
            _warm_t0 = time.perf_counter()
            new_client.start()
        except RuntimeError as e:
            print("[Bridge] Warm ACP client start failed")
            _telemetry_emit("acp_pool", result="warm_failed", model=key, error=str(e))
            return False, str(e)

        _st.acp_pool[key] = new_client
        _acp_pool_touch(key)
        _st.acp_client = new_client
        _acp_pool_evict_if_needed(key)
        _telemetry_emit("acp_pool", result="warm", model=key, pool_size=len(_st.acp_pool),
                        warm_ms=round((time.perf_counter() - _warm_t0) * 1000.0, 1))
        return True, new_client.model or "default"



