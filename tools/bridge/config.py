"""Immutable configuration constants for the Eva ACP Bridge.

This module centralizes path definitions, tuning thresholds, column
schemas, and other values that do not change at runtime. Mutable
state (token caches, flags, buffers) remains in ``core.py`` until
a future phase extracts it into ``state.py``.
"""

import datetime
import fcntl
import json
import math
import os
import pwd
import re
import secrets
import stat
import sys
import urllib.parse


def env_truthy(name):
    """Return True when an environment flag uses the shared truthy form."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def utc_now():
    """Current UTC datetime (timezone-aware)."""
    return datetime.datetime.now(datetime.timezone.utc)


def to_utc_iso(value):
    """Convert a datetime (or None) to a UTC ISO-8601 string."""
    if isinstance(value, datetime.datetime):
        active_value = value
    else:
        active_value = utc_now()
    if active_value.tzinfo is None:
        active_value = active_value.replace(tzinfo=datetime.timezone.utc)
    return active_value.astimezone(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Filesystem paths ────────────────────────────────────────────────
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = os.path.dirname(BRIDGE_DIR)
PROJECT_ROOT = os.path.dirname(TOOLS_DIR)
EVA_CONFIG_DIR = os.path.expanduser("~/.config/eva-standalone")
ARTIFACTS_DIR = os.path.join(EVA_CONFIG_DIR, "artifacts")
ACP_RUNTIME_DIR = os.path.join(EVA_CONFIG_DIR, "acp_runtime")
KUSTO_CLUSTER_CACHE_PATH = os.path.join(EVA_CONFIG_DIR, "kusto_cluster.txt")
MCP_CONFIG_CACHE_PATH = os.path.join(EVA_CONFIG_DIR, "mcp_config.json")
RUNTIME_STATE_PATH = os.path.join(EVA_CONFIG_DIR, "runtime_state.json")
ARTIFACT_EPOCH_PATH = os.path.join(EVA_CONFIG_DIR, "artifact_epoch.txt")
ARTIFACT_EPOCH_LOCK_PATH = os.path.join(
    EVA_CONFIG_DIR, "artifact_epoch.lock"
)
ARTIFACT_STORE_MARKER_PATH = os.path.join(
    EVA_CONFIG_DIR, "artifact_store.marker"
)
ARTIFACT_NAMESPACE_BLOCK_PATH = os.path.join(
    EVA_CONFIG_DIR, "artifact_namespace.blocked"
)
ALERTS_CONFIG_PATH = os.path.join(EVA_CONFIG_DIR, "alerts.json")
NOTIFY_PATH = os.path.join(EVA_CONFIG_DIR, "notifications.jsonl")
EMBEDDING_CACHE_PATH = os.path.join(EVA_CONFIG_DIR, "embeddings_cache.json")
MEMORY_BACKEND_PREF_PATH = os.path.join(EVA_CONFIG_DIR, "memory_backend.txt")
MODE_PREF_PATH = os.path.join(EVA_CONFIG_DIR, "mode.txt")
TELEMETRY_PATH = os.path.join(EVA_CONFIG_DIR, "telemetry.jsonl")
BRIDGE_DEBUG_LOG_PATH = os.path.join(EVA_CONFIG_DIR, "bridge_debug.log")


def artifact_namespace_blocked():
    try:
        with open_private_file(
            ARTIFACT_NAMESPACE_BLOCK_PATH, "r", encoding="utf-8"
        ) as handle:
            handle.read(64)
            return True
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError, PrivateStorageError):
        return True


def set_artifact_namespace_blocked(blocked):
    ensure_private_directory(EVA_CONFIG_DIR)
    if blocked:
        with open_private_file(
            ARTIFACT_NAMESPACE_BLOCK_PATH, "w", encoding="utf-8"
        ) as handle:
            handle.write("blocked-v1\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    _display, parent_fd = _open_private_directory(EVA_CONFIG_DIR)
    try:
        try:
            os.unlink(
                os.path.basename(ARTIFACT_NAMESPACE_BLOCK_PATH),
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            pass
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return True


def load_runtime_state_document_status(path=None):
    """Return (absent|invalid|valid, document) for the atomic runtime state."""
    target = path or RUNTIME_STATE_PATH

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate runtime-state member")
            result[key] = value
        return result

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite runtime-state number")
        return parsed

    try:
        with open_private_file(target, "r", encoding="utf-8") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw.encode("utf-8")) > 1024 * 1024:
            return "invalid", None
        data = json.loads(
            raw, object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-standard runtime-state number")
            ),
            parse_float=finite_float,
        )
    except FileNotFoundError:
        return "absent", None
    except (
        OSError, UnicodeError, ValueError, json.JSONDecodeError,
        PrivateStorageError,
    ):
        return "invalid", None
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "mode", "mcp_servers"}
        or data.get("version") != 1
        or isinstance(data.get("version"), bool)
        or data.get("mode") not in ("local", "cloud")
        or not isinstance(data.get("mcp_servers"), dict)
    ):
        return "invalid", None
    if len(data["mcp_servers"]) > 16:
        return "invalid", None
    canonical_servers = {}
    for name, server in data["mcp_servers"].items():
        canonical = _canonical_mcp_server(name, server, "cloud")
        if canonical is None:
            return "invalid", None
        canonical_servers[name] = canonical
    return "valid", {
        "version": 1, "mode": data["mode"],
        "mcp_servers": canonical_servers,
    }


def load_runtime_state_document(path=None):
    """Load one exact versioned mode/MCP document or return None."""
    status, data = load_runtime_state_document_status(path)
    return data if status == "valid" else None


class PrivateStorageError(RuntimeError):
    """A sensitive runtime path cannot be accessed without weakening policy."""


def _open_private_directory(path, *, create=True):
    target = os.path.abspath(os.path.expanduser(path))
    if target == os.path.sep:
        raise PrivateStorageError("the filesystem root cannot be a private directory")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise PrivateStorageError("descriptor-safe private directories are unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(os.path.sep, flags)
    try:
        for component in [part for part in target.split(os.path.sep) if part]:
            try:
                # codeql[py/path-injection]: each component is opened beneath
                # a pinned directory descriptor with O_NOFOLLOW; separators
                # and parent traversal cannot escape that descriptor.
                child = os.open(component, flags, dir_fd=descriptor)  # lgtm[py/path-injection]
            except FileNotFoundError:
                if not create:
                    raise
                # codeql[py/path-injection]: descriptor-relative creation uses
                # the same pinned O_NOFOLLOW parent as the open above.
                os.mkdir(component, 0o700, dir_fd=descriptor)  # lgtm[py/path-injection]
                # codeql[py/path-injection]: descriptor-relative traversal is
                # pinned to the verified parent descriptor.
                child = os.open(component, flags, dir_fd=descriptor)  # lgtm[py/path-injection]
                os.fchmod(child, 0o700)
            except OSError as exc:
                raise PrivateStorageError(
                    f"private directory component is unsafe: {component}"
                ) from exc
            os.close(descriptor)
            descriptor = child
        return target, descriptor
    except Exception:
        os.close(descriptor)
        raise


def ensure_private_directory(path, *, create=True):
    """Create/repair one owner-only directory without following path links."""
    target, descriptor = _open_private_directory(path, create=create)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise PrivateStorageError(f"private runtime path is not a directory: {target}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise PrivateStorageError(f"private runtime path has the wrong owner: {target}")
        os.fchmod(descriptor, 0o700)
        if os.fstat(descriptor).st_mode & 0o077:
            raise PrivateStorageError(f"private runtime path is not owner-only: {target}")
        return target
    finally:
        os.close(descriptor)


def secure_private_tree(path):
    """Repair one private tree using only descriptor-relative traversal."""
    root, descriptor = _open_private_directory(path)

    def secure_dir(directory_fd, display_path):
        os.fchmod(directory_fd, 0o700)
        for name in os.listdir(directory_fd):
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            child_display = os.path.join(display_path, name)
            if stat.S_ISLNK(info.st_mode):
                raise PrivateStorageError(
                    f"private runtime entry is a symlink: {child_display}"
                )
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise PrivateStorageError(
                    f"private runtime entry has wrong owner: {child_display}"
                )
            if stat.S_ISDIR(info.st_mode):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    secure_dir(child_fd, child_display)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise PrivateStorageError(
                        f"private runtime file has multiple links: {child_display}"
                    )
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    current = os.fstat(child_fd)
                    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                        raise PrivateStorageError(
                            f"private runtime file changed identity: {child_display}"
                        )
                    os.fchmod(child_fd, 0o600)
                finally:
                    os.close(child_fd)
            else:
                raise PrivateStorageError(
                    f"invalid private runtime entry: {child_display}"
                )

    try:
        secure_dir(descriptor, root)
        return root
    finally:
        os.close(descriptor)


def visit_private_files(root, visitor):
    """Visit regular files beneath *root* through pinned directory descriptors."""
    if not callable(visitor):
        raise TypeError("private file visitor must be callable")
    display, root_fd = _open_private_directory(root)
    results = []

    def visit_dir(directory_fd, relative_parts):
        for name in os.listdir(directory_fd):
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            child_display = os.path.join(display, *relative_parts, name)
            if stat.S_ISLNK(before.st_mode):
                raise PrivateStorageError(
                    f"private scan encountered symlink: {child_display}"
                )
            if hasattr(os, "getuid") and before.st_uid != os.getuid():
                raise PrivateStorageError(
                    f"private scan entry has wrong owner: {child_display}"
                )
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if stat.S_ISDIR(before.st_mode):
                child_fd = os.open(
                    name, flags | os.O_DIRECTORY, dir_fd=directory_fd
                )
                try:
                    current = os.fstat(child_fd)
                    if (
                        not stat.S_ISDIR(current.st_mode)
                        or current.st_dev != before.st_dev
                        or current.st_ino != before.st_ino
                    ):
                        raise PrivateStorageError(
                            f"private scan directory changed identity: {child_display}"
                        )
                    os.fchmod(child_fd, 0o700)
                    visit_dir(child_fd, relative_parts + (name,))
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise PrivateStorageError(
                    f"private scan entry is invalid: {child_display}"
                )
            child_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                current = os.fstat(child_fd)
                if (
                    not stat.S_ISREG(current.st_mode)
                    or current.st_nlink != 1
                    or current.st_dev != before.st_dev
                    or current.st_ino != before.st_ino
                ):
                    raise PrivateStorageError(
                        f"private scan file changed identity: {child_display}"
                    )
                os.fchmod(child_fd, 0o600)
                with os.fdopen(child_fd, "rb") as handle:
                    child_fd = -1
                    results.append(
                        visitor(relative_parts + (name,), handle, current)
                    )
            finally:
                if child_fd >= 0:
                    os.close(child_fd)

    try:
        root_info = os.fstat(root_fd)
        if hasattr(os, "getuid") and root_info.st_uid != os.getuid():
            raise PrivateStorageError(
                f"private scan root has wrong owner: {display}"
            )
        os.fchmod(root_fd, 0o700)
        visit_dir(root_fd, ())
        return results
    finally:
        os.close(root_fd)


def _remove_private_entry(parent_fd, name, display_path):
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        raise PrivateStorageError(f"private cleanup encountered symlink: {display_path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PrivateStorageError(f"private cleanup entry has wrong owner: {display_path}")
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise PrivateStorageError(f"private cleanup file has multiple links: {display_path}")
        os.unlink(name, dir_fd=parent_fd)
        return 1
    if not stat.S_ISDIR(info.st_mode):
        raise PrivateStorageError(f"private cleanup entry is invalid: {display_path}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    child_fd = os.open(name, flags, dir_fd=parent_fd)
    removed = 0
    try:
        current = os.fstat(child_fd)
        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_dev != info.st_dev
            or current.st_ino != info.st_ino
        ):
            raise PrivateStorageError(
                f"private cleanup directory changed identity: {display_path}"
            )
        for child_name in os.listdir(child_fd):
            removed += _remove_private_entry(
                child_fd, child_name, os.path.join(display_path, child_name)
            )
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)
    return removed


def remove_private_subdirectory(root, name):
    if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name) is None:
        raise PrivateStorageError("private cleanup name is invalid")
    display, root_fd = _open_private_directory(root, create=False)
    try:
        return _remove_private_entry(root_fd, name, os.path.join(display, name))
    finally:
        os.close(root_fd)


def clear_private_directory(root):
    """Remove all entries beneath one pinned private directory."""
    display, root_fd = _open_private_directory(root)
    removed = 0
    try:
        for name in os.listdir(root_fd):
            removed += _remove_private_entry(
                root_fd, name, os.path.join(display, name)
            )
        return removed
    finally:
        os.close(root_fd)


def fsync_private_directory(path):
    """Fsync one owner-only directory without following symbolic links."""
    _display, directory_fd = _open_private_directory(path, create=False)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def detach_private_directory(path, prefix=".revoked-"):
    """Atomically replace one private directory with a new empty directory."""
    target = os.path.abspath(os.path.expanduser(path))
    parent_path = os.path.dirname(target)
    name = os.path.basename(target)
    if (
        not name or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name) is None
        or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", prefix) is None
    ):
        raise PrivateStorageError("private rotation path is invalid")
    display, parent_fd = _open_private_directory(parent_path)
    quarantine = prefix + secrets.token_hex(16)
    try:
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
            return display, None
        if (
            stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode)
            or hasattr(os, "getuid") and before.st_uid != os.getuid()
        ):
            raise PrivateStorageError("private rotation target is unsafe")
        os.rename(name, quarantine, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            replacement_fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                os.fchmod(replacement_fd, 0o700)
            finally:
                os.close(replacement_fd)
            os.fsync(parent_fd)
        except Exception:
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
            os.rename(
                quarantine, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
            )
            os.fsync(parent_fd)
            raise
        return display, quarantine
    finally:
        os.close(parent_fd)


def detach_private_subdirectory(root, name, prefix=".revoked-"):
    """Atomically remove one named subtree from an active private namespace."""
    if (
        not isinstance(name, str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name) is None
        or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", prefix) is None
    ):
        raise PrivateStorageError("private detachment name is invalid")
    display, root_fd = _open_private_directory(root)
    quarantine = prefix + secrets.token_hex(16)
    try:
        try:
            # codeql[py/path-injection]: `name` has the bounded component
            # grammar above and is resolved only below `root_fd`.
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)  # lgtm[py/path-injection]
        except FileNotFoundError:
            return display, None
        if (
            stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or hasattr(os, "getuid") and info.st_uid != os.getuid()
        ):
            raise PrivateStorageError("private detachment target is unsafe")
        # codeql[py/path-injection]: both names are bounded single path
        # components and this is an atomic rename below the pinned root fd.
        os.rename(name, quarantine, src_dir_fd=root_fd, dst_dir_fd=root_fd)  # lgtm[py/path-injection]
        os.fsync(root_fd)
        return display, quarantine
    finally:
        os.close(root_fd)


def list_private_subdirectories(root, prefix):
    """List exact private child directories matching a bounded prefix."""
    if (
        not isinstance(prefix, str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,64}", prefix) is None
    ):
        raise PrivateStorageError("private listing prefix is invalid")
    display, root_fd = _open_private_directory(root)
    names = []
    try:
        for name in os.listdir(root_fd):
            if not name.startswith(prefix) or re.fullmatch(
                re.escape(prefix) + r"[0-9a-f]{32}", name
            ) is None:
                continue
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if (
                stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or hasattr(os, "getuid") and info.st_uid != os.getuid()
            ):
                raise PrivateStorageError(
                    "private quarantine entry is unsafe: "
                    + os.path.join(display, name)
                )
            names.append(name)
        return sorted(names)
    finally:
        os.close(root_fd)


def _remove_detached_entry(parent_fd, name):
    """Delete a detached entry without following links or mutating link targets."""
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return 1
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    child_fd = os.open(name, flags, dir_fd=parent_fd)
    removed = 0
    try:
        current = os.fstat(child_fd)
        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_dev != info.st_dev or current.st_ino != info.st_ino
        ):
            raise PrivateStorageError("detached directory changed identity")
        for child_name in os.listdir(child_fd):
            removed += _remove_detached_entry(child_fd, child_name)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)
    return removed


def remove_detached_subdirectory(root, name):
    if not isinstance(name, str) or re.fullmatch(
        r"\.(?:revoked|artifact-revoked|session-revoked)-[0-9a-f]{32}",
        name,
    ) is None:
        raise PrivateStorageError("detached cleanup name is invalid")
    _display, root_fd = _open_private_directory(root, create=False)
    try:
        removed = _remove_detached_entry(root_fd, name)
        os.fsync(root_fd)
        return removed
    finally:
        os.close(root_fd)


def scavenge_private_directories(root, name_pattern, cutoff, *, remove_all=False):
    display, root_fd = _open_private_directory(root)
    removed = 0
    try:
        for name in os.listdir(root_fd):
            if re.fullmatch(name_pattern, name) is None:
                continue
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise PrivateStorageError(
                    f"private scavenger found invalid entry: {os.path.join(display, name)}"
                )
            if remove_all or info.st_mtime < cutoff:
                _remove_private_entry(
                    root_fd, name, os.path.join(display, name)
                )
                removed += 1
        return removed
    finally:
        os.close(root_fd)


def scavenge_private_process_directories(root, name_pattern):
    """Remove owner-controlled runtime directories whose encoded PID is gone."""
    display, root_fd = _open_private_directory(root)
    removed = 0
    try:
        for name in os.listdir(root_fd):
            match = re.fullmatch(name_pattern, name)
            if match is None:
                continue
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise PrivateStorageError(
                    f"private process scavenger found invalid entry: {os.path.join(display, name)}"
                )
            try:
                pid = int(match.group("pid"))
            except (TypeError, ValueError, IndexError) as exc:
                raise PrivateStorageError("private runtime PID is invalid") from exc
            alive = pid > 0
            if alive:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
            if not alive:
                _remove_private_entry(
                    root_fd, name, os.path.join(display, name)
                )
                removed += 1
        return removed
    finally:
        os.close(root_fd)


class _AtomicPrivateFile:
    def __init__(self, handle, parent_fd, temp_name, final_name, *, exclusive=False):
        self._handle = handle
        self._parent_fd = parent_fd
        self._temp_name = temp_name
        self._final_name = final_name
        self._exclusive = exclusive
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        return self

    def close(self, *, commit=True):
        if self._closed:
            return
        published_exclusive = False
        try:
            if not self._handle.closed:
                self._handle.flush()
                os.fsync(self._handle.fileno())
                self._handle.close()
            if commit:
                if self._exclusive:
                    # codeql[py/path-injection]: both names were generated or
                    # basename-validated and stay below the pinned parent fd.
                    os.link(  # lgtm[py/path-injection]
                        self._temp_name, self._final_name,
                        src_dir_fd=self._parent_fd,
                        dst_dir_fd=self._parent_fd,
                        follow_symlinks=False,
                    )
                    published_exclusive = True
                    # codeql[py/path-injection]: generated temporary name is
                    # resolved beneath the pinned parent descriptor.
                    os.unlink(self._temp_name, dir_fd=self._parent_fd)  # lgtm[py/path-injection]
                else:
                    # codeql[py/path-injection]: atomic replacement is wholly
                    # descriptor-relative below the pinned private directory.
                    os.replace(  # lgtm[py/path-injection]
                        self._temp_name, self._final_name,
                        src_dir_fd=self._parent_fd, dst_dir_fd=self._parent_fd,
                    )
                os.fsync(self._parent_fd)
            else:
                # codeql[py/path-injection]: generated temporary name remains
                # below the pinned parent descriptor.
                os.unlink(self._temp_name, dir_fd=self._parent_fd)  # lgtm[py/path-injection]
        except Exception:
            try:
                if not self._handle.closed:
                    self._handle.close()
            except Exception:
                pass
            if published_exclusive:
                try:
                    # codeql[py/path-injection]: basename-validated final name
                    # is resolved only below the pinned parent descriptor.
                    os.unlink(self._final_name, dir_fd=self._parent_fd)  # lgtm[py/path-injection]
                except OSError:
                    pass
            try:
                # codeql[py/path-injection]: generated temporary name remains
                # below the pinned parent descriptor.
                os.unlink(self._temp_name, dir_fd=self._parent_fd)  # lgtm[py/path-injection]
            except OSError:
                pass
            raise
        finally:
            os.close(self._parent_fd)
            self._closed = True

    def __exit__(self, exc_type, _exc, _tb):
        self.close(commit=exc_type is None)
        return False


class _DurablePrivateFile:
    def __init__(self, handle, parent_fd):
        self._handle = handle
        self._parent_fd = parent_fd
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def __enter__(self):
        return self

    def close(self):
        if self._closed:
            return
        try:
            if not self._handle.closed:
                self._handle.flush()
                os.fsync(self._handle.fileno())
                self._handle.close()
            os.fsync(self._parent_fd)
        finally:
            if not self._handle.closed:
                try:
                    self._handle.close()
                except Exception:
                    pass
            os.close(self._parent_fd)
            self._closed = True

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()
        return False


def open_private_file(path, mode, *, encoding=None, buffering=-1):
    """Open a sensitive regular file as 0600 without following its final link."""
    if mode not in ("r", "rb", "w", "a", "x", "wb", "ab", "xb"):
        raise ValueError("unsupported private file mode")
    target = os.path.abspath(os.path.expanduser(path))
    _parent, parent_fd = _open_private_directory(os.path.dirname(target))
    name = os.path.basename(target)
    try:
        try:
            # codeql[py/path-injection]: `name` is os.path.basename(target)
            # and is opened only beneath the descriptor-safe parent fd.
            existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)  # lgtm[py/path-injection]
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise PrivateStorageError(f"private file is not a regular file: {target}")
            if hasattr(os, "getuid") and existing.st_uid != os.getuid():
                raise PrivateStorageError(f"private file has the wrong owner: {target}")
            if existing.st_nlink != 1:
                raise PrivateStorageError(f"private file has multiple links: {target}")

        if mode in ("w", "wb", "x", "xb"):
            if mode in ("x", "xb") and existing is not None:
                raise FileExistsError(target)
            temp_name = "." + name + ".tmp-" + secrets.token_hex(16)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            # codeql[py/path-injection]: generated temporary name is a single
            # component beneath the descriptor-pinned parent directory.
            descriptor = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)  # lgtm[py/path-injection]
            os.fchmod(descriptor, 0o600)
            fdopen_mode = "wb" if "b" in mode else "w"
            if "b" in mode:
                handle = os.fdopen(descriptor, fdopen_mode, buffering=buffering)
            else:
                handle = os.fdopen(
                    descriptor, fdopen_mode, encoding=encoding or "utf-8",
                    buffering=buffering,
                )
            return _AtomicPrivateFile(
                handle, parent_fd, temp_name, name,
                exclusive=mode in ("x", "xb"),
            )

        if mode in ("r", "rb"):
            flags = os.O_RDONLY
        else:
            flags = os.O_WRONLY | os.O_CREAT
        if "a" in mode:
            flags |= os.O_APPEND
        elif "x" in mode:
            flags |= os.O_EXCL
        elif mode not in ("r", "rb"):
            flags |= os.O_TRUNC
        flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        # codeql[py/path-injection]: basename-only name is resolved below the
        # descriptor-pinned parent and O_NOFOLLOW rejects a final symlink.
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)  # lgtm[py/path-injection]
    except Exception:
        os.close(parent_fd)
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (
            hasattr(os, "getuid") and info.st_uid != os.getuid()
        ) or info.st_nlink != 1:
            raise PrivateStorageError(
                f"private file validation failed: {target}"
            )
        os.fchmod(descriptor, 0o600)
    except Exception:
        os.close(descriptor)
        os.close(parent_fd)
        raise
    if mode in ("r", "rb"):
        os.close(parent_fd)
        if "b" in mode:
            return os.fdopen(descriptor, mode, buffering=buffering)
        return os.fdopen(
            descriptor, mode, encoding=encoding or "utf-8", buffering=buffering
        )
    if "b" in mode:
        handle = os.fdopen(descriptor, mode, buffering=buffering)
    else:
        handle = os.fdopen(
            descriptor, mode, encoding=encoding or "utf-8", buffering=buffering
        )
    return _DurablePrivateFile(
        handle, parent_fd
    )


def ensure_private_runtime_storage():
    root = secure_private_tree(EVA_CONFIG_DIR)
    for name in (
        "artifacts", "browser_trajectories", "desktop_trajectories",
        "browser_profile", "camera", "acp_runtime",
    ):
        secure_private_tree(os.path.join(root, name))
    scavenge_private_process_directories(
        ACP_RUNTIME_DIR, r"copilot-acp-(?P<pid>[1-9][0-9]*)-[0-9a-f]{32}"
    )
    # Legacy notification JSONL contained private title/body text. Content is
    # transient-only now, so revoke any retained legacy records at startup.
    with open_private_file(NOTIFY_PATH, "w"):
        pass
    with open_private_file(TELEMETRY_PATH, "w"):
        pass
    with open_private_file(BRIDGE_DEBUG_LOG_PATH, "w"):
        pass
    return root


def _advance_artifact_epoch_locked(lock_handle):
    """Advance the epoch while the caller holds the durable epoch lock."""
    initialized = os.fstat(lock_handle.fileno()).st_size > 0
    current = 0
    epoch_exists = False
    try:
        with open_private_file(ARTIFACT_EPOCH_PATH, "r") as handle:
            raw = handle.read().strip()
        if re.fullmatch(r"0|[1-9][0-9]{0,39}", raw) is None:
            raise PrivateStorageError("artifact epoch is malformed")
        current = int(raw)
        epoch_exists = True
    except FileNotFoundError:
        pass

    marker_exists = False
    try:
        with open_private_file(
            ARTIFACT_STORE_MARKER_PATH, "r"
        ) as marker_handle:
            marker = marker_handle.read().strip()
        if re.fullmatch(r"[0-9a-f]{64}", marker) is None:
            raise PrivateStorageError("artifact store marker is malformed")
        marker_exists = True
    except FileNotFoundError:
        pass

    if not epoch_exists:
        retained_artifacts = False
        try:
            retained_artifacts = bool(visit_private_files(
                ARTIFACTS_DIR, lambda _parts, _handle, _info: True
            ))
        except FileNotFoundError:
            pass
        if initialized or marker_exists or retained_artifacts:
            raise PrivateStorageError("artifact epoch is missing")

    if not marker_exists:
        with open_private_file(
            ARTIFACT_STORE_MARKER_PATH, "x"
        ) as marker_handle:
            marker_handle.write(secrets.token_hex(32))
    next_epoch = current + 1
    if next_epoch >= 10 ** 40:
        raise PrivateStorageError("artifact epoch exhausted")
    with open_private_file(ARTIFACT_EPOCH_PATH, "w") as handle:
        handle.write(str(next_epoch))
    if not initialized:
        lock_handle.write("initialized\n")
        lock_handle.flush()
        os.fsync(lock_handle.fileno())
    return str(next_epoch)


def advance_artifact_epoch():
    """Atomically advance and return a durable non-reusable decimal epoch."""
    with open_private_file(ARTIFACT_EPOCH_LOCK_PATH, "a") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            return _advance_artifact_epoch_locked(lock_handle)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _legacy_flat_artifact_store_present(lock_handle):
    """Return True only for the pre-identity flat artifact layout.

    The legacy release wrote regular files directly beneath ``artifacts/`` and
    had no epoch or store marker. A missing epoch in any newer/nested layout is
    still treated as corruption and must fail closed.
    """
    for path in (ARTIFACT_EPOCH_PATH, ARTIFACT_STORE_MARKER_PATH):
        try:
            with open_private_file(path, "rb"):
                return False
        except FileNotFoundError:
            pass

    if os.fstat(lock_handle.fileno()).st_size > 0:
        return False

    _display, artifacts_fd = _open_private_directory(
        ARTIFACTS_DIR, create=False
    )
    try:
        names = os.listdir(artifacts_fd)
        if not names:
            return False
        for name in names:
            info = os.stat(
                name, dir_fd=artifacts_fd, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or hasattr(os, "getuid") and info.st_uid != os.getuid()
            ):
                return False
        return True
    finally:
        os.close(artifacts_fd)


def initialize_artifact_epoch():
    """Advance the epoch, revoking a verified pre-epoch flat store once.

    Legacy bytes are atomically detached into the existing private quarantine
    namespace and are never re-authorized. Any failure leaves the durable
    namespace block in place. Missing epochs for current/nested stores continue
    to fail closed rather than cycling authority.
    """
    with open_private_file(ARTIFACT_EPOCH_LOCK_PATH, "a") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            try:
                return _advance_artifact_epoch_locked(lock_handle), False
            except PrivateStorageError as exc:
                if (
                    str(exc) != "artifact epoch is missing"
                    or not _legacy_flat_artifact_store_present(lock_handle)
                ):
                    raise

            set_artifact_namespace_blocked(True)
            try:
                _parent, detached = detach_private_directory(
                    ARTIFACTS_DIR, prefix=".artifact-revoked-"
                )
                if not detached:
                    raise PrivateStorageError(
                        "legacy artifact detachment failed"
                    )
                generation = _advance_artifact_epoch_locked(lock_handle)
                set_artifact_namespace_blocked(False)
                return generation, True
            except Exception:
                # The durable block was committed before detachment and
                # intentionally remains until successful recovery.
                raise
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

# ── Networking / validation ─────────────────────────────────────────
LMSTUDIO_ALLOWED_PORTS = {1234, 8000, 8080, 11434}
HTTP_CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")

# ── Request limits ──────────────────────────────────────────────────
MAX_JSON_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB

# ── Egress mode ─────────────────────────────────────────────────────
EGRESS_MODE_VALUES = ("offline", "local-network", "cloud")
REQUEST_ENVELOPE_FIELDS = {
    "request_id", "correlation_id", "session_id", "turn_id",
    "actor", "origin", "installation_id", "user_id",
}
SENSITIVE_ENV_MARKERS = {
    "TOKEN", "TOKENS", "KEY", "KEYS", "SECRET", "SECRETS", "PAT",
    "PASSWORD", "PASSWORDS", "CREDENTIAL", "CREDENTIALS", "AUTH",
    "AUTHORIZATION",
}
SENSITIVE_ENV_SUFFIXES = (
    "APIKEY", "ACCESSKEY", "PRIVATEKEY", "TOKEN", "SECRET", "PASSWORD",
    "CREDENTIAL", "CREDENTIALS", "AUTH", "AUTHORIZATION", "PAT",
)
_MCP_CONFIG_FIELDS = frozenset({"command", "args", "env"})
_SQLITE_MCP_NAMES = frozenset({"sqlite", "sqlite-mcp-server", "eva-sqlite"})
_READ_ONLY_MEMORY_MCP_TOOLS = frozenset({
    "kusto_list_databases", "kusto_show_tables",
    "kusto_show_schema", "kusto_sample_data", "eva_recall_knowledge",
    "eva_get_emotion_state", "eva_get_recent_reflections",
    "eva_get_active_goals", "eva_get_memory_summary",
})
_LOCAL_MCP_READ_ONLY_TOOLS = {
    "sqlite": _READ_ONLY_MEMORY_MCP_TOOLS,
    "sqlite-mcp-server": _READ_ONLY_MEMORY_MCP_TOOLS,
    "eva-sqlite": _READ_ONLY_MEMORY_MCP_TOOLS,
    "kusto-mcp-server": _READ_ONLY_MEMORY_MCP_TOOLS,
    "eva-web-search": frozenset({"web_search", "web_search_news"}),
}
_KUSTO_SUFFIXES = (".kusto.windows.net", ".kusto.data.microsoft.com")
_COMMON_CHILD_ENV = frozenset({
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LOGNAME", "TZ", "USER",
})
_GUI_CHILD_ENV = frozenset({
    "DBUS_SESSION_BUS_ADDRESS", "DESKTOP_SESSION", "DISPLAY", "WAYLAND_DISPLAY",
    "XAUTHORITY", "XDG_CURRENT_DESKTOP", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
})
_CHILD_ENV_PROFILES = {
    "base": _COMMON_CHILD_ENV,
    "acp": _COMMON_CHILD_ENV | frozenset({
        "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
    }),
    "camera": _COMMON_CHILD_ENV,
    "gui": _COMMON_CHILD_ENV | _GUI_CHILD_ENV,
    "mcp": _COMMON_CHILD_ENV,
    "notification": _COMMON_CHILD_ENV | _GUI_CHILD_ENV,
}
_BLOCKED_CHILD_ENV = frozenset({
    "BASH_ENV", "CDPATH", "COPILOT_ALLOW_ALL", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH", "ELECTRON_RUN_AS_NODE", "ENV", "GIT_CONFIG",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
    "LD_AUDIT", "LD_LIBRARY_PATH", "LD_PRELOAD", "NODE_EXTRA_CA_CERTS",
    "NODE_OPTIONS", "NODE_PATH", "PYTHONHOME", "PYTHONPATH", "RUBYOPT",
})
_BLOCKED_CHILD_ENV_PREFIXES = (
    "COPILOT_", "CURL_", "DYLD_", "GIT_CONFIG_", "HTTPS_PROXY", "HTTP_PROXY",
    "LD_", "NODE_", "NPM_CONFIG_", "OTEL_", "PIP_", "PYTHON", "REQUESTS_",
    "SSL_CERT_", "ALL_PROXY", "NO_PROXY",
)


def _fixed_child_path():
    candidates = (
        "/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
        "/usr/local/sbin", "/usr/bin", "/usr/sbin", "/bin", "/sbin",
    )
    return os.pathsep.join(path for path in candidates if os.path.isdir(path))


def _account_home():
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (AttributeError, KeyError, OSError):
        return os.path.expanduser("~")


def is_sensitive_env_name(name):
    upper = str(name or "").upper()
    if upper == "EVA_BRIDGE_TOKEN":
        return True
    parts = {part for part in re.split(r"[^A-Z0-9]+", upper) if part}
    if parts.intersection(SENSITIVE_ENV_MARKERS):
        return True
    compact = re.sub(r"[^A-Z0-9]", "", upper)
    return compact != "PATH" and any(compact.endswith(suffix) for suffix in SENSITIVE_ENV_SUFFIXES)


def child_process_env(explicit=None, *, profile="base"):
    """Build a minimal, profile-specific environment for one child process."""
    allowed = _CHILD_ENV_PROFILES.get(profile)
    if allowed is None:
        raise ValueError(f"unknown child environment profile: {profile}")
    result = {
        "HOME": _account_home(),
        "PATH": _fixed_child_path(),
    }
    for name in allowed:
        value = os.environ.get(name)
        if isinstance(value, str) and "\x00" not in value and len(value) <= 4096:
            result[name] = value
    if explicit and profile != "mcp":
        raise ValueError("explicit child environment is restricted to MCP children")
    for name, value in (explicit or {}).items():
        key = str(name)
        upper = key.upper()
        if (
            key == "EVA_BRIDGE_TOKEN"
            or upper in _BLOCKED_CHILD_ENV
            or any(upper.startswith(prefix) for prefix in _BLOCKED_CHILD_ENV_PREFIXES)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        ):
            raise ValueError(f"unsafe child environment variable: {key}")
        text = str(value)
        if "\x00" in text or len(text) > 16384:
            raise ValueError(f"invalid child environment value: {key}")
        result[key] = text
    return result


def normalize_kusto_origin(value):
    """Return one exact Microsoft Kusto HTTPS origin or raise ``ValueError``."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Kusto cluster must be a non-empty HTTPS origin")
    text = value if "://" in value else "https://" + value
    try:
        parsed = urllib.parse.urlsplit(text)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Kusto cluster URL is invalid") from exc
    hostname = (parsed.hostname or "").lower()
    if hostname.endswith("."):
        hostname = hostname[:-1]
    try:
        hostname.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Kusto cluster host must be ASCII") from exc
    labels = hostname.split(".")
    valid_labels = bool(labels) and all(
        label
        and len(label) <= 63
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    )
    allowed_host = any(
        hostname.endswith(suffix) and hostname != suffix[1:]
        for suffix in _KUSTO_SUFFIXES
    )
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
        or not valid_labels
        or not allowed_host
    ):
        raise ValueError("Kusto cluster must be an exact Microsoft HTTPS origin")
    return "https://" + hostname


def _mcp_env(raw, allowed, *, strip_unknown=False):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return None
    safe = {}
    for key, value in raw.items():
        if key not in allowed:
            if strip_unknown:
                continue
            return None
        if key == "_useGitHubPAT":
            if value is not True:
                return None
            safe[key] = True
            continue
        if not isinstance(value, str) or "\x00" in value or len(value) > 16384:
            return None
        safe[str(key)] = value
    return safe


def _trusted_mcp_script(value, basename):
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    candidate = os.path.expanduser(value)
    if not os.path.isabs(candidate):
        candidate = os.path.join(PROJECT_ROOT, candidate)
    real = os.path.realpath(candidate)
    expected = os.path.realpath(os.path.join(TOOLS_DIR, basename))
    return expected if real == expected else None


def _trusted_python_command(value):
    if value == "python3":
        return True
    return (
        isinstance(value, str)
        and os.path.realpath(os.path.expanduser(value))
        == os.path.realpath(sys.executable)
    )


def configured_memory_db_path():
    """Return the operator-configured SQLite path; request data cannot alter it."""
    raw = os.environ.get("EVA_MEMORY_DB", "").strip()
    if not raw:
        raw = os.path.expanduser("~/.eva/memory.db")
    if "\x00" in raw:
        raise PrivateStorageError("configured memory database path is invalid")
    target = os.path.abspath(os.path.expanduser(raw))
    if target == os.path.sep or not os.path.basename(target):
        raise PrivateStorageError("configured memory database path is invalid")
    return target


def local_mcp_tool_allowlist(server_name):
    """Return an immutable read-only tool set for direct local-model execution."""
    return _LOCAL_MCP_READ_ONLY_TOOLS.get(server_name)


def _canonical_mcp_server(name, raw, mode):
    """Return one exact release-approved MCP process shape, else ``None``."""
    if (
        not isinstance(name, str)
        or not isinstance(raw, dict)
        or set(raw) - _MCP_CONFIG_FIELDS
        or not isinstance(raw.get("command"), str)
        or not isinstance(raw.get("args"), list)
        or not all(isinstance(arg, str) for arg in raw["args"])
    ):
        return None
    command = raw["command"]
    args = raw["args"]

    if name in _SQLITE_MCP_NAMES and _trusted_python_command(command):
        script = _trusted_mcp_script(args[0], "sqlite_mcp.py") if len(args) == 1 else None
        env = _mcp_env(
            raw.get("env"), {"EVA_MEMORY_DB"}, strip_unknown=True
        )
        if script and env is not None:
            try:
                expected_db = configured_memory_db_path()
            except PrivateStorageError:
                return None
            requested_db = env.get("EVA_MEMORY_DB", "")
            if requested_db and os.path.abspath(
                os.path.expanduser(requested_db)
            ) != expected_db:
                return None
            return {
                "command": sys.executable, "args": [script],
                "env": {"EVA_MEMORY_DB": expected_db},
            }

    if mode != "cloud":
        return None

    if name == "kusto-mcp-server" and _trusted_python_command(command):
        script = _trusted_mcp_script(args[0], "kusto_mcp.py") if len(args) == 1 else None
        env = _mcp_env(
            raw.get("env"), {
                "KUSTO_ACCESS_TOKEN", "KUSTO_CLUSTER_URL", "KUSTO_DATABASE",
                "KUSTO_DATABASE_LOCKED",
            },
            strip_unknown=False,
        )
        if script and env is not None:
            if env.get("KUSTO_CLUSTER_URL"):
                try:
                    env["KUSTO_CLUSTER_URL"] = normalize_kusto_origin(
                        env["KUSTO_CLUSTER_URL"]
                    )
                except ValueError:
                    return None
            if (
                not env.get("KUSTO_CLUSTER_URL")
                or not isinstance(env.get("KUSTO_DATABASE"), str)
                or re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_-]{0,127}", env["KUSTO_DATABASE"]
                ) is None
                or str(env.get("KUSTO_DATABASE_LOCKED", "")).lower()
                not in ("1", "true", "yes")
            ):
                return None
            return {"command": sys.executable, "args": [script], "env": env}

    if name == "eva-web-search" and _trusted_python_command(command):
        script = _trusted_mcp_script(args[0], "web_search_mcp.py") if len(args) == 1 else None
        env = _mcp_env(raw.get("env"), set(), strip_unknown=True)
        if script and env is not None:
            return {"command": sys.executable, "args": [script], "env": env}

    if (
        name == "azure-mcp-server"
        and command == "npx"
        and args == ["-y", "@azure/mcp@latest", "server", "start"]
    ):
        env = _mcp_env(raw.get("env"), {"AZURE_MCP_COLLECT_TELEMETRY"})
        if env is not None and env.get("AZURE_MCP_COLLECT_TELEMETRY", "false") == "false":
            return {
                "command": "npx", "args": list(args),
                "env": {"AZURE_MCP_COLLECT_TELEMETRY": "false"},
            }

    if (
        name == "github-mcp-server"
        and command == "docker"
        and args == [
            "run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
            "ghcr.io/github/github-mcp-server",
        ]
    ):
        env = _mcp_env(
            raw.get("env"), {"_useGitHubPAT", "GITHUB_PERSONAL_ACCESS_TOKEN"}
        )
        if env is not None:
            return {"command": "docker", "args": list(args), "env": env}
    return None


def _release_disabled_mcp(name, raw):
    """True unless the server is an exact release-approved cloud shape."""
    return _canonical_mcp_server(name, raw, "cloud") is None


def mcp_config_for_egress(mcp_config, mode):
    """Return the MCP subset permitted by an egress policy.

    Every mode is fail-closed over exact process shapes. Cloud permits only the
    release presets plus trusted bundled servers. Offline and local-network
    permit only Eva's bundled SQLite MCP. Unknown, aliased, wrapped, or
    pointer/keyboard-capable servers are rejected before process startup.
    """
    source = dict(mcp_config or {}) if isinstance(mcp_config, dict) else {}
    allowed = {}
    rejected = []
    for name, raw in source.items():
        canonical = _canonical_mcp_server(name, raw, mode)
        if canonical is None:
            rejected.append(str(name))
        else:
            allowed[name] = canonical
    return allowed, rejected


def mcp_config_for_local_execution(mcp_config, mode):
    """Return only exact MCP servers with fixed read-only local tool profiles."""
    canonical, rejected = mcp_config_for_egress(mcp_config, mode)
    allowed = {}
    blocked = list(rejected)
    for name, server in canonical.items():
        if local_mcp_tool_allowlist(name) is None:
            blocked.append(name)
        elif name == "kusto-mcp-server":
            env = server.get("env", {})
            if (
                not env.get("KUSTO_CLUSTER_URL")
                or not isinstance(env.get("KUSTO_DATABASE"), str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,127}", env["KUSTO_DATABASE"])
                is None
                or str(env.get("KUSTO_DATABASE_LOCKED", "")).lower()
                not in ("1", "true", "yes")
            ):
                blocked.append(name)
            else:
                allowed[name] = server
        else:
            allowed[name] = server
    return allowed, sorted(set(blocked))

# ── ACP pool ────────────────────────────────────────────────────────
ACP_POOL_MAX = 4

# ── Cognition tuning ───────────────────────────────────────────────
CANDIDATE_HISTORY_TTL_SECONDS = 60
CONVO_CONTENT_CAP = 8000
EMBEDDING_MODEL = "text-embedding-3-small"
SEMANTIC_MIN_SCORE = 0.30
SEMANTIC_POOL_SIZE = 150

# ── Memory tables ───────────────────────────────────────────────────
MEMORY_TABLES = [
    "Knowledge", "Conversations", "EmotionState", "MemorySummaries",
    "Reflections", "Goals", "SelfState", "HeuristicsIndex",
    "EmotionBaseline", "BackgroundProposals", "BackgroundActivity", "Skills",
]

# ── Goals ───────────────────────────────────────────────────────────
GOAL_CATEGORIES = {"self_improvement", "knowledge_curation", "relational"}
GOAL_STATUSES = {"active", "paused", "done", "dropped"}
GOAL_COLUMNS = [
    "GoalId", "Title", "Description", "Category", "Status",
    "Priority", "RelatedTopics", "CreatedAt", "UpdatedAt",
]
GOALS_LATEST_QUERY = (
    "Goals | summarize arg_max(UpdatedAt, *) by GoalId "
    "| project GoalId, Title, Description, Category, Status, Priority, "
    "RelatedTopics, CreatedAt, UpdatedAt"
)

# ── Skills ──────────────────────────────────────────────────────────
SKILL_STATUSES = {"active", "disabled", "deleted"}
SKILL_COLUMNS = [
    "SkillId", "Name", "Description", "Instructions", "Tools",
    "Tags", "Source", "Status", "CreatedAt", "UpdatedAt",
]
SKILLS_LATEST_QUERY = (
    "Skills | summarize arg_max(UpdatedAt, *) by SkillId "
    "| project SkillId, Name, Description, Instructions, Tools, Tags, "
    "Source, Status, CreatedAt, UpdatedAt"
)
SKILL_SOURCE_MAX_BYTES = 200 * 1024
SKILL_INSTRUCTIONS_INJECT_CAP = 1500
SKILL_INJECT_MAX = 2

# ── Background jobs ─────────────────────────────────────────────────
BG_JOB_TYPE = "memory_consolidation"
BG_TARGET_TABLE = "MemorySummaries"
BG_JOB_GOAL_CHECKIN = "goal_checkin"
BG_JOB_DAILY_DIGEST = "daily_digest"
BG_JOB_KNOWLEDGE_HYGIENE = "knowledge_hygiene"
BG_JOB_REFLECTION_SYNTHESIS = "reflection_synthesis"
BG_JOB_EMOTION_DRIFT = "emotion_drift"
BG_JOB_TOKEN_TELEMETRY = "token_telemetry"
BG_JOB_PROACTIVE_BRIEFING = "proactive_briefing"
BG_JOB_MARKET_SNAPSHOT = "market_snapshot"
BG_JOB_SEC_FILINGS = "sec_filing_watch"
BG_JOB_SPACE_WEATHER = "space_weather_alert"
BG_JOB_RESEARCH_DEEPDIVE = "research_deepdive"
BG_JOB_ALERT_WATCH = "alert_watch"
BG_JOB_ADX_PROJECTION = "adx_projection"
BG_APPLY_TABLES = {"MemorySummaries", "Reflections"}
GOAL_STALE_DAYS = 3
GOAL_CHECKIN_MAX = 2
KNOWLEDGE_STALE_CONFIDENCE = 0.3
EMOTION_DRIFT_THRESHOLD = 0.15
REFLECTION_SYNTH_MIN = 3
SEC_WATCH_SYMBOLS = ["PLG", "PKX"]

# ── Background proposals ───────────────────────────────────────────
BG_PROPOSAL_STATUSES = {"pending", "approved", "rejected", "applying", "applied", "failed"}
BG_PROPOSAL_COLUMNS = [
    "ProposalId", "CreatedAt", "JobType", "TargetTable", "Payload",
    "Status", "SourceWindowStart", "SourceWindowEnd", "Notes",
    "ReviewedAt", "ReviewedBy",
]
BG_ACTIVITY_COLUMNS = [
    "TickId", "StartedAt", "EndedAt", "JobType", "Status",
    "ProposalCount", "TokenEstimate", "Notes",
]

# ── Telemetry ───────────────────────────────────────────────────────
TELEMETRY_MAX_BYTES = 5 * 1024 * 1024
TELEMETRY_RING_MAX = 300
LOG_RING_MAX = 200
LOG_LINE_CAP = 240

# ── Alerts / notifications ─────────────────────────────────────────
ALERT_TYPES = ("sec_filing", "weather", "space_weather", "keyword_watch", "research_question")
ALERT_CHANNELS = ("chat", "voice", "signal")
NOTIFY_RING_MAX = 100
NOTIFY_MAX_BYTES = 2 * 1024 * 1024
NOTIFY_CRITICAL_SALIENCE = 0.9
DEFAULT_ALERT_SETTINGS = {
    "rate_limit_per_hour": 8,
    "quiet_hours_start": None,
    "quiet_hours_end": None,
}

# ── Signal (send-only) ─────────────────────────────────────────────
SIGNAL_CLI_PATH = os.environ.get("EVA_SIGNAL_CLI", "signal-cli")
SIGNAL_SENDER = os.environ.get("EVA_SIGNAL_SENDER", "").strip()
SIGNAL_RECIPIENT = os.environ.get("EVA_SIGNAL_RECIPIENT", "").strip()
SIGNAL_SEND_TIMEOUT = 15

# ── Entity extraction ──────────────────────────────────────────────
ENTITY_IGNORE_WORDS = {
    "the", "this", "that", "what", "when", "where", "how", "why", "who", "can", "could",
    "would", "should", "hello", "please", "thanks", "hey", "eva", "image", "tell", "today",
    "tomorrow", "yesterday", "time", "date", "reply", "respond", "answer", "exactly",
    "its", "whats", "have", "has", "had", "does", "did", "was", "were", "are", "been",
    "being", "will", "shall", "may", "might", "must", "let", "lets", "also", "just",
    "here", "there", "some", "any", "all", "each", "every", "many", "much", "very",
    "yes", "not", "but", "and", "for", "with", "from", "about", "into", "over",
    "your", "you", "they", "them", "their", "then", "than", "our", "his", "her",
    "great", "good", "like", "sure", "okay", "right", "know", "think", "want",
    "need", "make", "get", "see", "say", "said", "new", "use", "try", "give",
    "look", "help", "come", "take", "back", "well", "too", "now",
    "fetching", "searching", "getting", "running", "checking",
}

ENTITY_RESERVED_TERMS = {
    "run", "show", "query", "timestamp", "schema", "table", "tables", "database", "databases",
    "count", "sum", "average", "filter", "where", "join", "project", "distinct", "take", "top",
    "execute", "save", "remember", "store", "write", "reply", "respond", "answer",
    "kusto", "adx", "conversation", "conversations", "knowledge", "emotionstate", "reflections", "goals",
    "memorysummaries", "selfstate", "heuristicsindex", "emotionbaseline", "backgroundproposals",
    "backgroundactivity",
}

# ═══════════════════════════════════════════════════════════════════════
#  Phase 2 – Startup-immutable, fail-closed feature flags
#
#  These are frozen at import time. Invalid enum values produce a sentinel
#  (the string "INVALID") so downstream code can detect misconfiguration
#  deterministically without crashing on import.
# ═══════════════════════════════════════════════════════════════════════

_PHASE2_RECALL_MODES = frozenset({"legacy", "shadow", "hybrid"})
_PHASE2_SEMANTIC_MODES = frozenset({"off", "cache", "openai"})
_PHASE2_CONSOLIDATION_VALUES = frozenset({"off", "proposals"})
_PHASE2_ANALYTICS_VALUES = frozenset({"off", "local"})
_PHASE2_BOOL_TRUTHY = frozenset({"1", "true", "yes"})
_PHASE2_BOOL_FALSY = frozenset({"0", "false", "no"})


def _phase2_enum(env_name, valid_set, default):
    """Read an env var as a constrained enum. Returns 'INVALID' on bad value."""
    raw = os.environ.get(env_name, "").strip().lower()
    if not raw:
        return default
    if raw in valid_set:
        return raw
    return "INVALID"


def _phase2_bool(env_name, default=False):
    """Read env var as strict boolean. Returns (value, is_valid).

    Valid values: '1','true','yes' (True); '0','false','no' (False); '' (default).
    Invalid values (e.g. 'maybe','2','on') return (default, False) — the
    sentinel records the flag name as invalid rather than silently defaulting.
    """
    raw = os.environ.get(env_name, "").strip().lower()
    if not raw:
        return (default, True)
    if raw in _PHASE2_BOOL_TRUTHY:
        return (True, True)
    if raw in _PHASE2_BOOL_FALSY:
        return (False, True)
    # Invalid: not silently false, records invalidity
    return (default, False)


# ── Frozen flag values ──────────────────────────────────────────────

# Master kill switch (default OFF)
_EVA_PHASE2_MEMORY_RESULT = _phase2_bool("EVA_PHASE2_MEMORY", False)
EVA_PHASE2_MEMORY = _EVA_PHASE2_MEMORY_RESULT[0]

# Recall mode
EVA_MEMORY_RECALL_MODE = _phase2_enum("EVA_MEMORY_RECALL_MODE", _PHASE2_RECALL_MODES, "legacy")

# Semantic mode
EVA_MEMORY_SEMANTIC_MODE = _phase2_enum("EVA_MEMORY_SEMANTIC_MODE", _PHASE2_SEMANTIC_MODES, "off")

# Explicit consent for semantic queries
_EVA_MEMORY_SEMANTIC_QUERY_CONSENT_RESULT = _phase2_bool("EVA_MEMORY_SEMANTIC_QUERY_CONSENT", False)
EVA_MEMORY_SEMANTIC_QUERY_CONSENT = _EVA_MEMORY_SEMANTIC_QUERY_CONSENT_RESULT[0]

# Consolidation engine
EVA_MEMORY_CONSOLIDATION = _phase2_enum("EVA_MEMORY_CONSOLIDATION", _PHASE2_CONSOLIDATION_VALUES, "off")

# Analytics collection
EVA_MEMORY_ANALYTICS = _phase2_enum("EVA_MEMORY_ANALYTICS", _PHASE2_ANALYTICS_VALUES, "off")

# ── Invalid flag tracking ───────────────────────────────────────────

def _collect_invalid_flags():
    """Collect a tuple of flag names with invalid values. No values stored."""
    invalid = []
    # Bool flags
    _bool_flags = [
        ("EVA_PHASE2_MEMORY", _EVA_PHASE2_MEMORY_RESULT),
        ("EVA_MEMORY_SEMANTIC_QUERY_CONSENT", _EVA_MEMORY_SEMANTIC_QUERY_CONSENT_RESULT),
    ]
    for name, (_, valid) in _bool_flags:
        if not valid:
            invalid.append(name)
    # Enum flags
    _enum_flags = [
        ("EVA_MEMORY_RECALL_MODE", EVA_MEMORY_RECALL_MODE),
        ("EVA_MEMORY_SEMANTIC_MODE", EVA_MEMORY_SEMANTIC_MODE),
        ("EVA_MEMORY_CONSOLIDATION", EVA_MEMORY_CONSOLIDATION),
        ("EVA_MEMORY_ANALYTICS", EVA_MEMORY_ANALYTICS),
    ]
    for name, value in _enum_flags:
        if value == "INVALID":
            invalid.append(name)
    return tuple(invalid)


PHASE2_INVALID_FLAGS = _collect_invalid_flags()


def phase2_config_valid():
    """Return True if all Phase 2 flags are in valid states."""
    return len(PHASE2_INVALID_FLAGS) == 0


def phase2_effective_enabled():
    """Return True only if master flag is on AND config is valid."""
    return EVA_PHASE2_MEMORY and phase2_config_valid()


def phase2_effective_modes():
    """Return effective modes when master is off or invalid.

    If master is off or config invalid, returns all-legacy/off/no-consent
    defaults regardless of what was configured.
    """
    if not phase2_effective_enabled():
        return {
            "recall_mode": "legacy",
            "semantic_mode": "off",
            "query_consent": False,
            "consolidation": "off",
            "analytics": "off",
        }
    return {
        "recall_mode": EVA_MEMORY_RECALL_MODE,
        "semantic_mode": EVA_MEMORY_SEMANTIC_MODE,
        "query_consent": EVA_MEMORY_SEMANTIC_QUERY_CONSENT,
        "consolidation": EVA_MEMORY_CONSOLIDATION,
        "analytics": EVA_MEMORY_ANALYTICS,
    }


def validate_phase2_startup():
    """Validate Phase 2 configuration at startup. Returns (ok, message).

    - If invalid flags exist AND master requested enabled => (False, error_msg)
      Caller should print redacted error and exit(2).
    - If invalid flags exist AND master off => (True, warning_msg)
      Caller should print warning; effective disabled.
    - If all valid => (True, None)
    """
    if not PHASE2_INVALID_FLAGS:
        return (True, None)

    flag_list = ", ".join(PHASE2_INVALID_FLAGS)

    if EVA_PHASE2_MEMORY or "EVA_PHASE2_MEMORY" in PHASE2_INVALID_FLAGS:
        return (
            False,
            f"Phase2 startup FATAL: invalid configuration for flags: {flag_list}. "
            f"Master enabled but config invalid. Fix environment or disable EVA_PHASE2_MEMORY.",
        )
    else:
        return (
            True,
            f"Phase2 startup WARNING: invalid configuration for flags: {flag_list}. "
            f"Master is off so Phase2 remains disabled.",
        )


def phase2_startup_summary():
    """Return a fixed, credential-free summary of effective Phase 2 modes."""
    modes = phase2_effective_modes()
    return (
        "Phase2 memory=" + ("enabled" if phase2_effective_enabled() else "disabled")
        + ", recall=" + modes["recall_mode"]
        + ", semantic=" + modes["semantic_mode"]
        + ", query_consent=" + ("enabled" if modes["query_consent"] else "disabled")
        + ", consolidation=" + modes["consolidation"]
        + ", analytics=" + modes["analytics"]
    )


# ═══════════════════════════════════════════════════════════════════════
#  Phase 3 – Safe continual-learning shadow mode
# ═══════════════════════════════════════════════════════════════════════

_PHASE3_LEARNING_VALUES = frozenset({"off", "shadow"})
EVA_PHASE3_LEARNING = _phase2_enum(
    "EVA_PHASE3_LEARNING", _PHASE3_LEARNING_VALUES, "off"
)
_EVA_LEGACY_SKILL_AUTO_LEARN_RESULT = _phase2_bool(
    "EVA_LEGACY_SKILL_AUTO_LEARN", False
)
EVA_LEGACY_SKILL_AUTO_LEARN = _EVA_LEGACY_SKILL_AUTO_LEARN_RESULT[0]
PHASE3_INVALID_FLAGS = tuple(
    name for name, valid in (
        ("EVA_PHASE3_LEARNING", EVA_PHASE3_LEARNING != "INVALID"),
        ("EVA_LEGACY_SKILL_AUTO_LEARN", _EVA_LEGACY_SKILL_AUTO_LEARN_RESULT[1]),
    ) if not valid
)


def phase3_config_valid():
    return not PHASE3_INVALID_FLAGS


def phase3_effective_enabled():
    return phase3_config_valid() and EVA_PHASE3_LEARNING == "shadow"


def validate_phase3_startup():
    if PHASE3_INVALID_FLAGS:
        return (
            False,
            "Phase3 startup FATAL: invalid configuration for flags: "
            + ", ".join(PHASE3_INVALID_FLAGS),
        )
    return (True, None)


def phase3_startup_summary():
    return (
        "Phase3 learning="
        + ("shadow" if phase3_effective_enabled() else "disabled")
        + ", legacy_skill_auto_learn="
        + ("enabled" if EVA_LEGACY_SKILL_AUTO_LEARN else "disabled")
    )
