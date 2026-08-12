"""Durable local projects, Git worktrees, and coding-run records for Eva."""

import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse


_SCHEMA_VERSION = 3
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE)
_MCP_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_MCP_CONFIG_MAX_BYTES = 256 * 1024
_MCP_CONFIG_MAX_FILES = 32
_MCP_DISCOVERY_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "target", "vendor", "__pycache__"}
_MCP_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MCP_RESERVED_ENV_KEYS = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "PYTHONPATH", "NODE_OPTIONS",
    "ELECTRON_RUN_AS_NODE", "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV", "ENV", "KSH_ENV", "ZDOTDIR",
    "PYTHONHOME", "PYTHONSTARTUP", "PERL5OPT", "PERL5LIB", "RUBYOPT", "RUBYLIB",
}
_GITHUB_REPOSITORY_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class WorkspaceError(Exception):
    """A safe, user-visible workspace operation error."""


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_text(value):
    if value is None:
        return ""
    return str(value)


class WorkspaceStore:
    """SQLite-backed workspace records with confined Git worktree operations."""

    def __init__(self, config_dir, id_factory=None):
        self.config_dir = Path(config_dir).expanduser().resolve()
        self.runtime_root = self.config_dir / "worktrees"
        self.db_path = self.config_dir / "workspaces.sqlite3"
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.lock = threading.RLock()
        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self):
        with self.lock:
            self.connection.close()

    def _migrate(self):
        with self.lock, self.connection:
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {row["version"] for row in self.connection.execute("SELECT version FROM schema_migrations")}
            if 1 not in applied:
                self.connection.executescript(
                    """
                    CREATE TABLE projects (
                        id TEXT PRIMARY KEY,
                        root_path TEXT NOT NULL UNIQUE,
                        display_name TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE checkouts (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                        run_id TEXT,
                        kind TEXT NOT NULL CHECK(kind IN ('source', 'worktree')),
                        path TEXT NOT NULL UNIQUE,
                        branch TEXT NOT NULL DEFAULT '',
                        base_revision TEXT NOT NULL DEFAULT '',
                        lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active', 'disposed', 'archived')),
                        dirty_file_count INTEGER NOT NULL DEFAULT 0,
                        owner_refs INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE coding_runs (
                        id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                        checkout_id TEXT NOT NULL REFERENCES checkouts(id),
                        objective TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('active', 'archived', 'discarded', 'completed', 'cancelled')),
                        primary_session_id TEXT NOT NULL DEFAULT '',
                        model_policy TEXT NOT NULL DEFAULT '',
                        final_disposition TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        archived_at TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE agent_runs (
                        id TEXT PRIMARY KEY,
                        coding_run_id TEXT NOT NULL REFERENCES coding_runs(id) ON DELETE CASCADE,
                        parent_agent_id TEXT,
                        checkout_id TEXT REFERENCES checkouts(id),
                        conversation_key TEXT NOT NULL DEFAULT '',
                        capability_policy TEXT NOT NULL DEFAULT 'read_only',
                        status TEXT NOT NULL DEFAULT 'created',
                        report TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE terminal_sessions (
                        id TEXT PRIMARY KEY,
                        checkout_id TEXT NOT NULL REFERENCES checkouts(id) ON DELETE CASCADE,
                        coding_run_id TEXT REFERENCES coding_runs(id) ON DELETE SET NULL,
                        agent_run_id TEXT REFERENCES agent_runs(id) ON DELETE SET NULL,
                        label TEXT NOT NULL DEFAULT '',
                        state TEXT NOT NULL DEFAULT 'closed',
                        scrollback_checkpoint TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE run_attachments (
                        id TEXT PRIMARY KEY,
                        coding_run_id TEXT REFERENCES coding_runs(id) ON DELETE CASCADE,
                        agent_run_id TEXT REFERENCES agent_runs(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        reference TEXT NOT NULL,
                        checksum TEXT NOT NULL DEFAULT '',
                        access_policy TEXT NOT NULL DEFAULT 'private',
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE approvals (
                        id TEXT PRIMARY KEY,
                        coding_run_id TEXT REFERENCES coding_runs(id) ON DELETE CASCADE,
                        agent_run_id TEXT REFERENCES agent_runs(id) ON DELETE CASCADE,
                        terminal_session_id TEXT REFERENCES terminal_sessions(id) ON DELETE CASCADE,
                        action TEXT NOT NULL,
                        decision TEXT NOT NULL CHECK(decision IN ('pending', 'approved', 'rejected')),
                        evidence TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        decided_at TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX checkouts_project_index ON checkouts(project_id, lifecycle);
                    CREATE INDEX coding_runs_project_index ON coding_runs(project_id, status, updated_at DESC);
                    CREATE INDEX agent_runs_coding_run_index ON agent_runs(coding_run_id, status);
                    CREATE INDEX approvals_coding_run_index ON approvals(coding_run_id, decision);
                    """
                )
                self.connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, _utc_now()),
                )
            if 2 not in applied:
                self.connection.executescript(
                    """
                    CREATE TABLE project_mcp_preferences (
                        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                        server_name TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(project_id, server_name)
                    );
                    CREATE INDEX project_mcp_preferences_project_index
                    ON project_mcp_preferences(project_id, enabled);
                    """
                )
                self.connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, _utc_now()),
                )
            if 3 not in applied:
                self.connection.execute(
                    "ALTER TABLE project_mcp_preferences ADD COLUMN approved_digest TEXT NOT NULL DEFAULT ''"
                )
                self.connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (_SCHEMA_VERSION, _utc_now()),
                )

    def register_project(self, requested_path, display_name=None):
        root_path = self._canonical_git_root(requested_path)
        name = _json_text(display_name).strip()[:120] or Path(root_path).name
        now = _utc_now()
        with self.lock, self.connection:
            existing = self.connection.execute(
                "SELECT id FROM projects WHERE root_path = ?", (root_path,)
            ).fetchone()
            if existing:
                self.connection.execute(
                    "UPDATE projects SET display_name = ?, updated_at = ? WHERE id = ?",
                    (name, now, existing["id"]),
                )
                return self.get_project(existing["id"])
            project_id = self._new_id()
            checkout_id = self._new_id()
            branch = self._current_branch(root_path)
            revision = self._git(root_path, ["rev-parse", "HEAD"])
            self.connection.execute(
                "INSERT INTO projects(id, root_path, display_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (project_id, root_path, name, now, now),
            )
            self.connection.execute(
                """INSERT INTO checkouts(
                    id, project_id, kind, path, branch, base_revision, lifecycle, created_at, updated_at
                ) VALUES (?, ?, 'source', ?, ?, ?, 'active', ?, ?)""",
                (checkout_id, project_id, root_path, branch, revision, now, now),
            )
        return self.get_project(project_id)

    def ensure_eva_ready_project(self):
        """Create Eva's managed local Git project once and return its durable record."""
        project_path = (self.config_dir / "projects" / "eva-ready").resolve()
        if not self._is_within(project_path, self.config_dir):
            raise WorkspaceError("Invalid Eva-ready workspace path.")
        with self.lock:
            project_path.mkdir(mode=0o700, parents=True, exist_ok=True)
            git_directory = project_path / ".git"
            if not git_directory.exists():
                if any(project_path.iterdir()):
                    raise WorkspaceError("Eva-ready workspace directory is not empty.")
                self._git(str(project_path), ["init", "-b", "main"])
                self._git(str(project_path), ["config", "user.name", "Eva Workspace"])
                self._git(str(project_path), ["config", "user.email", "eva-workspace@local.invalid"])
                readme = project_path / "README.md"
                readme.write_text("# Eva Ready Workspace\n\nManaged local workspace for Eva coding runs.\n", encoding="utf-8")
                self._git(str(project_path), ["add", "--", "README.md"])
                self._git(str(project_path), ["commit", "-m", "Initialize Eva ready workspace"])
        return self.register_project(project_path, "Eva Ready Workspace")

    def import_github_repository(self, repository_url, github_token=""):
        """Clone a selected github.com repository into Eva-owned workspace storage."""
        normalized_url, owner, repository = self._normalize_github_repository_url(repository_url)
        destination_name = owner + "-" + repository + "-" + hashlib.sha256(
            normalized_url.encode("utf-8")
        ).hexdigest()[:12]
        import_root = self._prepare_managed_import_root()
        destination = import_root / destination_name
        if not self._is_within(destination, import_root) or not self._is_within(destination, self.config_dir):
            raise WorkspaceError("Invalid GitHub workspace destination.")
        with self.lock:
            if destination.exists():
                if destination.is_symlink() or not destination.is_dir():
                    raise WorkspaceError("GitHub workspace destination is unavailable.")
                return self.register_project(destination, owner + "/" + repository)
            try:
                self._clone_github_repository(normalized_url, destination, github_token=github_token)
            except WorkspaceError:
                shutil.rmtree(destination, ignore_errors=True)
                raise
        return self.register_project(destination, owner + "/" + repository)

    def list_projects(self):
        with self.lock:
            project_rows = self.connection.execute(
                "SELECT id FROM projects ORDER BY updated_at DESC, display_name COLLATE NOCASE"
            ).fetchall()
        return [self.get_project(row["id"]) for row in project_rows]

    def list_project_files(self, project_id, limit=1000):
        root = self._validated_source_project(project_id)
        code, output = self._git_status_output(
            str(root), ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
        )
        if code != 0:
            raise WorkspaceError("Git operation failed.")
        relative_paths = sorted(set(filter(None, output.split("\0"))))
        bounded_limit = min(max(int(limit), 1), 1000)
        return {
            "files": relative_paths[:bounded_limit],
            "truncated": len(relative_paths) > bounded_limit,
        }

    def resolve_project_file(self, project_id, relative_path):
        return str(self._resolve_checkout_file(self._validated_source_project(project_id), relative_path))

    def get_project(self, project_id):
        with self.lock:
            project_row = self.connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not project_row:
                raise WorkspaceError("Unknown project.")
            source_checkout = self.connection.execute(
                "SELECT * FROM checkouts WHERE project_id = ? AND kind = 'source'", (project_id,)
            ).fetchone()
            run_count = self.connection.execute(
                "SELECT COUNT(*) AS count FROM coding_runs WHERE project_id = ? AND status = 'active'", (project_id,)
            ).fetchone()["count"]
        return {
            "id": project_row["id"],
            "name": project_row["display_name"],
            "path": project_row["root_path"],
            "created_at": project_row["created_at"],
            "updated_at": project_row["updated_at"],
            "active_run_count": run_count,
            "source_checkout": self._checkout_payload(source_checkout),
            "mcp_servers": self.list_project_mcp_servers(project_id),
        }

    def list_project_mcp_servers(self, project_id):
        """Return safe workspace MCP metadata without commands or environment values."""
        with self.lock:
            preferences = {
                row["server_name"]: {
                    "enabled": bool(row["enabled"]),
                    "approved_digest": row["approved_digest"],
                }
                for row in self.connection.execute(
                    "SELECT server_name, enabled, approved_digest FROM project_mcp_preferences WHERE project_id = ?",
                    (project_id,),
                )
            }
        try:
            servers, sources = self._discover_mcp_servers(self._validated_source_project(project_id))
        except WorkspaceError as error:
            self._revoke_project_mcp_preferences(project_id)
            return {"source": "workspace MCP discovery", "state": "invalid", "message": str(error), "servers": []}
        if servers is None:
            self._revoke_project_mcp_preferences(project_id)
            return {"source": "workspace MCP discovery", "state": "missing", "message": "", "servers": []}
        preferences = self._revoke_stale_mcp_preferences(project_id, servers, preferences)
        return {
            "source": "workspace MCP discovery",
            "state": "ready",
            "message": "",
            "servers": [
                self._mcp_server_metadata(name, config, preferences.get(name, {}), sources.get(name, "mcp.json"))
                for name, config in sorted(servers.items(), key=lambda item: item[0].lower())
            ],
        }

    def set_project_mcp_server_enabled(self, project_id, server_name, enabled, approved_digest=""):
        name = _json_text(server_name).strip()
        if not _MCP_SERVER_NAME_RE.fullmatch(name):
            raise WorkspaceError("Invalid workspace MCP server.")
        if not isinstance(enabled, bool):
            raise WorkspaceError("Workspace MCP server state must be enabled or disabled.")
        servers = self._read_mcp_servers(self._validated_source_project(project_id))
        if not servers or name not in servers:
            raise WorkspaceError("Workspace MCP server is not available from mcp.json.")
        digest = self._mcp_config_digest(servers[name])
        requested_digest = _json_text(approved_digest).strip()
        if enabled and requested_digest != digest:
            raise WorkspaceError("Workspace MCP configuration changed. Review it again before enabling.")
        now = _utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                """INSERT INTO project_mcp_preferences(project_id, server_name, enabled, approved_digest, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, server_name)
                   DO UPDATE SET enabled = excluded.enabled,
                                 approved_digest = excluded.approved_digest,
                                 updated_at = excluded.updated_at""",
                (project_id, name, int(enabled), digest if enabled else requested_digest, now),
            )
            self.connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
        return self.get_project(project_id)

    def mcp_config_for_run(self, run_id):
        """Read enabled MCP definitions from the exact isolated worktree for a run."""
        run = self.get_run(run_id)
        checkout = run["checkout"]
        if checkout["lifecycle"] != "active":
            raise WorkspaceError("Coding workspace is unavailable.")
        checkout_path = self._validated_managed_checkout(checkout)
        with self.lock:
            approvals = {
                row["server_name"]: row["approved_digest"]
                for row in self.connection.execute(
                    "SELECT server_name, approved_digest FROM project_mcp_preferences WHERE project_id = ? AND enabled = 1",
                    (run["project_id"],),
                )
            }
        try:
            servers = self._read_mcp_servers(checkout_path)
        except WorkspaceError:
            self._revoke_project_mcp_preferences(run["project_id"])
            return {}
        if not servers or not approvals:
            if not servers:
                self._revoke_project_mcp_preferences(run["project_id"])
            return {}
        stale_names = {
            name for name, digest in approvals.items()
            if name not in servers or digest != self._mcp_config_digest(servers[name])
        }
        if stale_names:
            self._revoke_project_mcp_preferences(run["project_id"], stale_names)
        return {name: config for name, config in servers.items() if name in approvals and name not in stale_names}

    def _revoke_stale_mcp_preferences(self, project_id, servers, preferences):
        stale_names = {
            name for name, preference in preferences.items()
            if preference.get("enabled") and (
                name not in servers
                or preference.get("approved_digest") != self._mcp_config_digest(servers[name])
            )
        }
        if stale_names:
            self._revoke_project_mcp_preferences(project_id, stale_names)
            for name in stale_names:
                preferences[name] = {"enabled": False, "approved_digest": ""}
        return preferences

    def _revoke_project_mcp_preferences(self, project_id, server_names=None):
        now = _utc_now()
        with self.lock, self.connection:
            if server_names:
                placeholders = ",".join("?" for _ in server_names)
                self.connection.execute(
                    "UPDATE project_mcp_preferences SET enabled = 0, approved_digest = '', updated_at = ? "
                    "WHERE project_id = ? AND server_name IN (" + placeholders + ")",
                    [now, project_id, *sorted(server_names)],
                )
            else:
                self.connection.execute(
                    "UPDATE project_mcp_preferences SET enabled = 0, approved_digest = '', updated_at = ? "
                    "WHERE project_id = ? AND enabled = 1",
                    (now, project_id),
                )

    def create_run(self, project_id, objective, primary_session_id="", base_ref="HEAD", model_policy=""):
        clean_objective = _json_text(objective).strip()
        if not clean_objective or len(clean_objective) > 4000:
            raise WorkspaceError("A coding-run objective between 1 and 4000 characters is required.")
        session_id = _json_text(primary_session_id).strip()
        if len(session_id) > 160:
            raise WorkspaceError("Invalid primary session ID.")
        reference = _json_text(base_ref).strip() or "HEAD"
        if len(reference) > 256 or reference.startswith("-"):
            raise WorkspaceError("Invalid base revision.")
        policy = _json_text(model_policy).strip()
        if len(policy) > 160:
            raise WorkspaceError("Invalid model policy.")

        with self.lock:
            project_row = self.connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project_row:
            raise WorkspaceError("Unknown project.")
        source_root = project_row["root_path"]
        base_revision = self._git(source_root, ["rev-parse", "--verify", reference + "^{commit}"])
        run_id = self._new_id()
        checkout_id = self._new_id()
        branch = "eva/run-" + run_id.replace("-", "")[:8]
        checkout_path = (self.runtime_root / project_id / run_id).resolve()
        if not self._is_within(checkout_path, self.runtime_root):
            raise WorkspaceError("Invalid managed worktree path.")
        branch_ref = "refs/heads/" + branch
        branch_status = self._git_status(source_root, ["show-ref", "--verify", "--quiet", branch_ref])
        if branch_status == 0:
            raise WorkspaceError("Generated run branch already exists.")
        if branch_status not in (0, 1):
            raise WorkspaceError("Could not validate the generated run branch.")
        if checkout_path.exists():
            raise WorkspaceError("Generated worktree path already exists.")
        checkout_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self._git(source_root, ["worktree", "add", "-b", branch, str(checkout_path), base_revision])
        except WorkspaceError:
            raise

        now = _utc_now()
        try:
            with self.lock, self.connection:
                self.connection.execute(
                    """INSERT INTO checkouts(
                        id, project_id, kind, path, branch, base_revision, lifecycle, owner_refs, created_at, updated_at
                    ) VALUES (?, ?, 'worktree', ?, ?, ?, 'active', 1, ?, ?)""",
                    (checkout_id, project_id, str(checkout_path), branch, base_revision, now, now),
                )
                self.connection.execute(
                    """INSERT INTO coding_runs(
                        id, project_id, checkout_id, objective, status, primary_session_id, model_policy, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                    (run_id, project_id, checkout_id, clean_objective, session_id, policy, now, now),
                )
                self.connection.execute(
                    "UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id)
                )
        except Exception:
            self._remove_worktree(source_root, checkout_path, branch, force=True)
            raise
        return self.get_run(run_id)

    def list_runs(self, project_id=None):
        query = "SELECT id FROM coding_runs"
        parameters = []
        if project_id:
            query += " WHERE project_id = ?"
            parameters.append(project_id)
        query += " ORDER BY updated_at DESC"
        with self.lock:
            run_rows = self.connection.execute(query, parameters).fetchall()
        return [self.get_run(row["id"]) for row in run_rows]

    def list_workspace_assets(self):
        assets = []
        for run in self.list_runs():
            checkout = run["checkout"]
            if run["status"] == "discarded" or checkout["lifecycle"] != "active":
                continue
            checkout_path = Path(checkout["path"])
            if not checkout_path.exists() and not checkout_path.is_symlink():
                continue
            root = self._validated_managed_checkout(checkout)
            tracked = self._git(str(root), ["diff", "--name-only", "-z", checkout["base_revision"], "--"])
            untracked = self._git(str(root), ["ls-files", "--others", "--exclude-standard", "-z"])
            relative_paths = sorted(set(filter(None, tracked.split("\0") + untracked.split("\0"))))
            for relative_path in relative_paths[:500]:
                try:
                    asset_path = self._resolve_checkout_file(root, relative_path)
                    stat = asset_path.stat()
                except (WorkspaceError, OSError):
                    continue
                assets.append({
                    "id": run["id"] + ":" + relative_path,
                    "source": "workspace",
                    "run_id": run["id"],
                    "checkout_id": checkout["id"],
                    "project_name": run["project"]["name"],
                    "objective": run["objective"],
                    "name": asset_path.name,
                    "relative_path": relative_path,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "agent_status": (run.get("agent") or {}).get("status", ""),
                })
        assets.sort(key=lambda item: item["modified"], reverse=True)
        return assets

    def resolve_workspace_asset(self, run_id, relative_path):
        run = self.get_run(run_id)
        checkout = run["checkout"]
        if run["status"] == "discarded" or checkout["lifecycle"] != "active":
            raise WorkspaceError("Workspace asset is unavailable.")
        return str(self._resolve_checkout_file(self._validated_managed_checkout(checkout), relative_path))

    def _validated_managed_checkout(self, checkout):
        return self._validate_managed_checkout_location(checkout, require_exists=True)

    def _validate_managed_checkout_location(self, checkout, require_exists):
        if checkout.get("kind") != "worktree" or checkout.get("lifecycle") != "active":
            raise WorkspaceError("Managed worktree is unavailable.")
        runtime_path = Path(os.path.abspath(self.runtime_root))
        checkout_path = Path(os.path.abspath(checkout.get("path") or ""))
        if not self._is_within(checkout_path, runtime_path):
            raise WorkspaceError("Managed worktree escaped Eva's runtime root.")
        current = runtime_path
        if current.is_symlink():
            raise WorkspaceError("Eva runtime root cannot be a symlink.")
        try:
            relative_parts = checkout_path.relative_to(runtime_path).parts
        except ValueError:
            raise WorkspaceError("Managed worktree escaped Eva's runtime root.")
        for part in relative_parts:
            current = current / part
            if current.is_symlink():
                raise WorkspaceError("Managed worktree path cannot contain symlinks.")
            if not current.exists():
                if require_exists:
                    raise WorkspaceError("Managed worktree is unavailable.")
                return checkout_path
        if not require_exists:
            return checkout_path
        try:
            canonical_runtime = runtime_path.resolve(strict=True)
            canonical_checkout = checkout_path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise WorkspaceError("Managed worktree is unavailable.")
        if not canonical_checkout.is_dir() or not self._is_within(canonical_checkout, canonical_runtime):
            raise WorkspaceError("Managed worktree escaped Eva's runtime root.")
        return canonical_checkout

    def _resolve_checkout_file(self, checkout_root, relative_path):
        if not isinstance(relative_path, str) or not relative_path or len(relative_path) > 4096:
            raise WorkspaceError("Invalid workspace asset path.")
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise WorkspaceError("Invalid workspace asset path.")
        try:
            root = checkout_root.resolve(strict=True)
            unresolved = root
            for part in relative.parts:
                unresolved = unresolved / part
                if unresolved.is_symlink():
                    raise WorkspaceError("Workspace asset is unavailable.")
            candidate = unresolved.resolve(strict=True)
        except (OSError, RuntimeError):
            raise WorkspaceError("Workspace asset is unavailable.")
        if not self._is_within(candidate, root) or not candidate.is_file():
            raise WorkspaceError("Workspace asset is unavailable.")
        return candidate

    def get_run(self, run_id):
        with self.lock:
            run_row = self.connection.execute("SELECT * FROM coding_runs WHERE id = ?", (run_id,)).fetchone()
            if not run_row:
                raise WorkspaceError("Unknown coding run.")
            checkout_row = self.connection.execute(
                "SELECT * FROM checkouts WHERE id = ?", (run_row["checkout_id"],)
            ).fetchone()
            project_row = self.connection.execute(
                "SELECT id, display_name, root_path FROM projects WHERE id = ?", (run_row["project_id"],)
            ).fetchone()
            agent_row = self.connection.execute(
                "SELECT * FROM agent_runs WHERE coding_run_id = ? ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return {
            "id": run_row["id"],
            "project_id": run_row["project_id"],
            "project": {
                "id": project_row["id"],
                "name": project_row["display_name"],
                "path": project_row["root_path"],
            },
            "checkout": self._checkout_payload(checkout_row),
            "objective": run_row["objective"],
            "status": run_row["status"],
            "primary_session_id": run_row["primary_session_id"],
            "model_policy": run_row["model_policy"],
            "final_disposition": run_row["final_disposition"],
            "created_at": run_row["created_at"],
            "updated_at": run_row["updated_at"],
            "archived_at": run_row["archived_at"],
            "agent": self._agent_payload(agent_row) if agent_row else None,
        }

    def validated_checkout_path(self, checkout_id):
        with self.lock:
            checkout_row = self.connection.execute(
                "SELECT * FROM checkouts WHERE id = ?", (checkout_id,)
            ).fetchone()
        if not checkout_row:
            raise WorkspaceError("Unknown checkout.")
        return str(self._validated_managed_checkout(self._checkout_payload(checkout_row)))

    def create_agent_run(self, agent_id, coding_run_id, checkout_id, conversation_key, capability_policy="workspace_write"):
        now = _utc_now()
        with self.lock, self.connection:
            run = self.connection.execute(
                "SELECT id, checkout_id, status FROM coding_runs WHERE id = ?", (coding_run_id,)
            ).fetchone()
            if not run or run["checkout_id"] != checkout_id or run["status"] != "active":
                raise WorkspaceError("Coding run is unavailable for agent dispatch.")
            existing = self.connection.execute("SELECT id FROM agent_runs WHERE id = ?", (agent_id,)).fetchone()
            if existing:
                raise WorkspaceError("Agent run already exists.")
            self.connection.execute(
                """INSERT INTO agent_runs(
                    id, coding_run_id, checkout_id, conversation_key, capability_policy,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'starting', ?, ?)""",
                (agent_id, coding_run_id, checkout_id, conversation_key, capability_policy, now, now),
            )
        return self.get_run(coding_run_id)["agent"]

    def update_agent_run(self, agent_id, status, report=""):
        allowed = {"starting", "running", "steering", "done", "error", "cancelled"}
        if status not in allowed:
            raise WorkspaceError("Invalid agent run status.")
        now = _utc_now()
        with self.lock, self.connection:
            row = self.connection.execute("SELECT coding_run_id FROM agent_runs WHERE id = ?", (agent_id,)).fetchone()
            if not row:
                raise WorkspaceError("Unknown agent run.")
            self.connection.execute(
                "UPDATE agent_runs SET status = ?, report = ?, updated_at = ? WHERE id = ?",
                (status, _json_text(report)[-4000:], now, agent_id),
            )
            if status == "done":
                self.connection.execute(
                    """UPDATE coding_runs
                       SET status = 'completed', final_disposition = 'agent_completed', updated_at = ?
                       WHERE id = ? AND status = 'active'""",
                    (now, row["coding_run_id"]),
                )
            elif status == "cancelled":
                self.connection.execute(
                    """UPDATE coding_runs
                       SET status = 'cancelled', final_disposition = 'agent_cancelled', updated_at = ?
                       WHERE id = ? AND status = 'active'""",
                    (now, row["coding_run_id"]),
                )
            else:
                self.connection.execute(
                    "UPDATE coding_runs SET updated_at = ? WHERE id = ?", (now, row["coding_run_id"])
                )
        return self.get_run(row["coding_run_id"])["agent"]

    def checkout_status(self, checkout_id):
        with self.lock:
            checkout_row = self.connection.execute("SELECT * FROM checkouts WHERE id = ?", (checkout_id,)).fetchone()
        if not checkout_row:
            raise WorkspaceError("Unknown checkout.")
        checkout_path = Path(checkout_row["path"])
        if checkout_row["lifecycle"] == "disposed":
            return self._checkout_payload(checkout_row)
        if checkout_row["kind"] == "worktree":
            if not checkout_path.exists() and not checkout_path.is_symlink():
                self._validate_managed_checkout_location(self._checkout_payload(checkout_row), require_exists=False)
                return self._checkout_payload(checkout_row)
            validated_root = self._validated_managed_checkout(self._checkout_payload(checkout_row))
        else:
            validated_root = self._validated_source_project(checkout_row["project_id"])
        status_output = self._git(str(validated_root), ["status", "--porcelain=v1", "-z"])
        dirty_count = self._dirty_file_count(status_output)
        now = _utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE checkouts SET dirty_file_count = ?, updated_at = ? WHERE id = ?",
                (dirty_count, now, checkout_id),
            )
            refreshed = self.connection.execute("SELECT * FROM checkouts WHERE id = ?", (checkout_id,)).fetchone()
        return self._checkout_payload(refreshed)

    def archive_run(self, run_id):
        now = _utc_now()
        with self.lock, self.connection:
            existing = self.connection.execute(
                """SELECT coding_runs.id, agent_runs.status AS agent_status
                   FROM coding_runs
                   LEFT JOIN agent_runs ON agent_runs.id = (
                       SELECT id FROM agent_runs WHERE coding_run_id = coding_runs.id
                       ORDER BY created_at DESC LIMIT 1
                   )
                   WHERE coding_runs.id = ?""",
                (run_id,),
            ).fetchone()
            if not existing:
                raise WorkspaceError("Unknown coding run.")
            if existing["agent_status"] in {"starting", "running", "steering"}:
                raise WorkspaceError("The workspace agent is still running. Wait for completion before archive.")
            self.connection.execute(
                "UPDATE coding_runs SET status = 'archived', archived_at = ?, updated_at = ? WHERE id = ?",
                (now, now, run_id),
            )
        return self.get_run(run_id)

    def discard_run(self, run_id, confirm_dirty=False):
        run = self.get_run(run_id)
        if run["status"] == "discarded":
            return run
        if run.get("agent") and run["agent"].get("status") in {"starting", "running", "steering"}:
            raise WorkspaceError("The workspace agent is still running. Wait for completion before discard.")
        checkout = self.checkout_status(run["checkout"]["id"])
        if checkout["dirty_file_count"] and not confirm_dirty:
            raise WorkspaceError("This worktree has local changes. Confirm dirty cleanup before discarding it.")
        self._remove_worktree(
            run["project"]["path"], Path(checkout["path"]), checkout["branch"], bool(confirm_dirty)
        )
        now = _utc_now()
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE checkouts SET lifecycle = 'disposed', owner_refs = 0, updated_at = ? WHERE id = ?",
                (now, checkout["id"]),
            )
            self.connection.execute(
                """UPDATE coding_runs
                   SET status = 'discarded', final_disposition = 'discarded', updated_at = ?, archived_at = ?
                   WHERE id = ?""",
                (now, now, run_id),
            )
        return self.get_run(run_id)

    def _remove_worktree(self, project_root, checkout_path, branch, force):
        if checkout_path.exists():
            arguments = ["worktree", "remove"]
            if force:
                arguments.append("--force")
            arguments.append(str(checkout_path))
            self._git(project_root, arguments)
        else:
            self._git(project_root, ["worktree", "prune"])
            if self._worktree_registered(project_root, checkout_path):
                raise WorkspaceError("The missing managed worktree could not be safely recovered.")
        if branch:
            branch_status = self._git_status(project_root, ["show-ref", "--verify", "--quiet", "refs/heads/" + branch])
            if branch_status == 0:
                self._git(project_root, ["branch", "-D", branch])
            elif branch_status != 1:
                raise WorkspaceError("Could not validate the managed run branch.")

    def _worktree_registered(self, project_root, checkout_path):
        output = self._git(project_root, ["worktree", "list", "--porcelain"])
        expected = "worktree " + str(checkout_path)
        return any(line == expected for line in output.splitlines())

    def _canonical_git_root(self, requested_path):
        if not isinstance(requested_path, (str, os.PathLike)):
            raise WorkspaceError("A local Git repository directory is required.")
        candidate = Path(requested_path).expanduser()
        try:
            canonical = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise WorkspaceError("The selected project directory is unavailable.")
        if not canonical.is_dir():
            raise WorkspaceError("The selected project must be a directory.")
        try:
            git_root = self._git(str(canonical), ["rev-parse", "--show-toplevel"])
        except WorkspaceError:
            raise WorkspaceError("The selected directory is not inside a Git repository.")
        try:
            return str(Path(git_root).resolve(strict=True))
        except (OSError, RuntimeError):
            raise WorkspaceError("The selected Git repository is unavailable.")

    def _validated_source_project(self, project_id):
        with self.lock:
            project_row = self.connection.execute(
                "SELECT root_path FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if not project_row:
            raise WorkspaceError("Unknown project.")
        root = Path(os.path.abspath(project_row["root_path"]))
        try:
            canonical = root.resolve(strict=True)
        except (OSError, RuntimeError):
            raise WorkspaceError("Source workspace is unavailable.")
        if root.is_symlink() or canonical != root or not canonical.is_dir():
            raise WorkspaceError("Source workspace is unavailable.")
        return canonical

    def _normalize_github_repository_url(self, repository_url):
        value = _json_text(repository_url).strip()
        if len(value) > 2048:
            raise WorkspaceError("GitHub repository URL is too long.")
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.lower() != "github.com"
            or parsed.netloc.lower() != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.params
        ):
            raise WorkspaceError("Use a credential-free https://github.com/owner/repository URL.")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2:
            raise WorkspaceError("Use a GitHub repository URL with an owner and repository name.")
        owner, repository = parts
        if repository.endswith(".git"):
            repository = repository[:-4]
        if not _GITHUB_REPOSITORY_PART_RE.fullmatch(owner) or not _GITHUB_REPOSITORY_PART_RE.fullmatch(repository):
            raise WorkspaceError("GitHub repository URL contains an invalid owner or repository name.")
        return "https://github.com/" + owner + "/" + repository + ".git", owner, repository

    def _prepare_managed_import_root(self):
        current = self.config_dir
        if current.is_symlink() or not current.is_dir():
            raise WorkspaceError("GitHub workspace destination is unavailable.")
        for part in ("projects", "github"):
            current = current / part
            if current.exists() or current.is_symlink():
                if current.is_symlink() or not current.is_dir():
                    raise WorkspaceError("GitHub workspace destination is unavailable.")
            else:
                current.mkdir(mode=0o700)
        return current

    def _clone_github_repository(self, repository_url, destination, github_token=""):
        git_home = self.config_dir / "git-import-home"
        if git_home.exists() or git_home.is_symlink():
            if git_home.is_symlink() or not git_home.is_dir():
                raise WorkspaceError("GitHub import environment is unavailable.")
        else:
            git_home.mkdir(mode=0o700)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(git_home),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
        askpass_path = None
        if github_token:
            askpass_path = git_home / ("askpass-" + uuid.uuid4().hex + ".sh")
            askpass_path.write_text(
                "#!/bin/sh\ncase \"$1\" in *Username*) printf '%s\\n' x-access-token ;; *) printf '%s\\n' \"$EVA_GITHUB_TOKEN\" ;; esac\n",
                encoding="utf-8",
            )
            askpass_path.chmod(0o700)
            environment["GIT_ASKPASS"] = str(askpass_path)
            environment["EVA_GITHUB_TOKEN"] = github_token
        clone_command = ["git", "-c", "credential.helper="]
        if not github_token:
            clone_command.extend(["-c", "core.askPass="])
        clone_command.extend(["clone", "--origin", "origin", repository_url, str(destination)])
        try:
            completed = subprocess.run(
                clone_command,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise WorkspaceError("GitHub repository import could not start.")
        finally:
            environment.pop("EVA_GITHUB_TOKEN", None)
            if askpass_path is not None:
                try:
                    askpass_path.unlink()
                except OSError:
                    pass
        if completed.returncode != 0:
            raise WorkspaceError(self._github_clone_failure_message(completed.stderr, bool(github_token)))

    @staticmethod
    def _github_clone_failure_message(stderr, authenticated):
        """Map Git transport failures to safe operator guidance without leaking output."""
        detail = str(stderr or "").lower()
        if any(marker in detail for marker in (
            "authentication failed", "http basic: access denied", "could not read username",
            "terminal prompts disabled", "invalid username or token",
        )):
            return "GitHub authentication was rejected. Update the GitHub PAT in Settings > Auth."
        if any(marker in detail for marker in (
            "repository not found", "not found", "permission denied", "access denied",
        )):
            if authenticated:
                return "GitHub denied access to this repository. For private repositories, grant the configured PAT Contents: Read access to this repository."
            return "GitHub denied access to this repository. Configure a GitHub PAT with repository Contents: Read access in Settings > Auth."
        if any(marker in detail for marker in (
            "could not resolve host", "failed to connect", "connection timed out",
            "network is unreachable", "ssl certificate",
        )):
            return "GitHub could not be reached. Check the network connection and retry."
        return "GitHub repository import failed. Confirm that the repository is available to your GitHub account."

    def _read_mcp_servers(self, workspace_root):
        return self._discover_mcp_servers(workspace_root)[0]

    def _discover_mcp_servers(self, workspace_root):
        root = Path(workspace_root).resolve()
        candidates = []
        for current, directories, filenames in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if name not in _MCP_DISCOVERY_SKIP_DIRS]
            relative_dir = Path(current).relative_to(root)
            if len(relative_dir.parts) > 5:
                directories[:] = []
                continue
            for filename in filenames:
                if filename in {"mcp.json", ".mcp.json"}:
                    candidates.append(Path(current) / filename)
        candidates.sort(key=lambda item: item.relative_to(root).as_posix().lower())
        if len(candidates) > _MCP_CONFIG_MAX_FILES:
            raise WorkspaceError("Workspace has too many MCP configuration files.")
        servers = {}
        sources = {}
        for config_path in candidates:
            relative_source = config_path.relative_to(root).as_posix()
            parsed = self._read_mcp_config_file(config_path)
            for name, config in parsed.items():
                server_name = name
                if server_name in servers:
                    prefix = re.sub(r"[^A-Za-z0-9]+", "-", relative_source.rsplit(".", 1)[0]).strip("-") or "workspace"
                    server_name = (prefix + "--" + name)[:119]
                    suffix = 2
                    while server_name in servers:
                        suffix_text = "-" + str(suffix)
                        server_name = (prefix + "--" + name)[:119 - len(suffix_text)] + suffix_text
                        suffix += 1
                servers[server_name] = config
                sources[server_name] = relative_source
        return (servers or None), sources

    def _read_mcp_config_file(self, config_path):
        try:
            if not config_path.exists():
                return None
            if config_path.is_symlink() or not config_path.is_file():
                raise WorkspaceError("Workspace mcp.json must be a regular file.")
            if config_path.stat().st_size > _MCP_CONFIG_MAX_BYTES:
                raise WorkspaceError("Workspace mcp.json is too large.")
            with config_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError):
            raise WorkspaceError("Workspace mcp.json is not valid JSON.")
        if not isinstance(payload, dict):
            raise WorkspaceError("Workspace mcp.json must contain an object.")
        candidates = payload.get("mcpServers", payload.get("servers", payload))
        if not isinstance(candidates, dict):
            raise WorkspaceError("Workspace mcp.json must define mcpServers.")
        normalized = {}
        for name, config in candidates.items():
            if not isinstance(name, str) or not _MCP_SERVER_NAME_RE.fullmatch(name) or not isinstance(config, dict):
                raise WorkspaceError("Workspace mcp.json contains an invalid server definition.")
            normalized[name] = self._normalize_mcp_server_config(config)
        return normalized

    def _normalize_mcp_server_config(self, config):
        command = config.get("command")
        url = config.get("url")
        if (command is None) == (url is None):
            raise WorkspaceError("Each workspace MCP server must define exactly one command or URL.")
        if command is not None:
            if not isinstance(command, str) or not command.strip() or len(command) > 1024:
                raise WorkspaceError("Workspace MCP server command is invalid.")
            args = config.get("args", [])
            if not isinstance(args, list) or len(args) > 128 or any(
                not isinstance(argument, str) or len(argument) > 2048 for argument in args
            ):
                raise WorkspaceError("Workspace MCP server arguments are invalid.")
            env = config.get("env", {})
            if not isinstance(env, dict) or len(env) > 128:
                raise WorkspaceError("Workspace MCP server environment is invalid.")
            normalized_env = {}
            for key, value in env.items():
                if (
                    not isinstance(key, str)
                    or not _MCP_ENV_NAME_RE.fullmatch(key)
                    or key in _MCP_RESERVED_ENV_KEYS
                    or key.startswith("EVA_")
                    or key.startswith("LD_")
                    or key.startswith("DYLD_")
                    or not isinstance(value, (str, int, float, bool))
                    or len(str(value)) > 4096
                ):
                    raise WorkspaceError("Workspace MCP server environment contains a reserved or invalid entry.")
                normalized_env[key] = value
            return {"command": command, "args": args, "env": normalized_env}
        if not isinstance(url, str) or not url.startswith("https://") or len(url) > 4096:
            raise WorkspaceError("Workspace MCP server URL must use HTTPS.")
        headers = config.get("headers", {})
        if not isinstance(headers, dict) or len(headers) > 128 or any(
            not isinstance(key, str) or not key.strip() or len(key) > 256
            or not isinstance(value, str) or len(value) > 4096
            for key, value in headers.items()
        ):
            raise WorkspaceError("Workspace MCP server headers are invalid.")
        return {"url": url, "headers": headers}

    @staticmethod
    def _mcp_config_digest(config):
        encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _mcp_server_metadata(self, name, config, preference, source):
        digest = self._mcp_config_digest(config)
        enabled = bool(preference.get("enabled")) and preference.get("approved_digest") == digest
        return {
            "name": name,
            "source": source,
            "transport": self._mcp_transport(config),
            "enabled": enabled,
            "digest": digest,
            "command": config.get("command", ""),
            "args": list(config.get("args") or []),
            "url": config.get("url", ""),
            "env_keys": sorted((config.get("env") or {}).keys()),
            "header_keys": sorted((config.get("headers") or {}).keys()),
        }

    @staticmethod
    def _mcp_transport(config):
        if isinstance(config.get("url"), str):
            return "remote"
        if isinstance(config.get("command"), str):
            return "stdio"
        return "configured"

    def _current_branch(self, root_path):
        result = self._git_status_output(root_path, ["symbolic-ref", "--quiet", "--short", "HEAD"])
        return result[1].strip() if result[0] == 0 else ""

    def _git(self, cwd, arguments):
        code, output = self._git_status_output(cwd, arguments)
        if code != 0:
            raise WorkspaceError("Git operation failed.")
        return output.strip()

    def _git_status(self, cwd, arguments):
        return self._git_status_output(cwd, arguments)[0]

    def _git_status_output(self, cwd, arguments):
        try:
            requested_cwd = os.path.abspath(os.fspath(cwd))
            normalized_cwd = os.path.realpath(requested_cwd)
        except (TypeError, ValueError, OSError):
            raise WorkspaceError("The Git working directory is invalid.")
        if normalized_cwd != requested_cwd or not os.path.isdir(normalized_cwd):
            raise WorkspaceError("The Git working directory is unavailable or contains a symbolic link.")
        try:
            completed = subprocess.run(
                ["git", "-C", normalized_cwd, *arguments],
                env=self._git_environment(),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise WorkspaceError("Git is unavailable for this workspace operation.")
        return completed.returncode, completed.stdout

    def _git_environment(self):
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        }
        if os.name == "nt":
            environment["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
            environment["USERPROFILE"] = os.environ.get("USERPROFILE", "")
        return environment

    def _new_id(self):
        candidate = _json_text(self.id_factory()).strip()
        if not _UUID_RE.fullmatch(candidate):
            raise WorkspaceError("Workspace ID generation failed.")
        return candidate.lower()

    @staticmethod
    def _is_within(candidate, root):
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _dirty_file_count(status_output):
        entries = status_output.split("\0")
        count = 0
        index = 0
        while index < len(entries):
            entry = entries[index]
            if not entry:
                index += 1
                continue
            status_code = entry[:2]
            if len(entry) < 3 or entry[2] != " ":
                index += 1
                continue
            count += 1
            index += 2 if "R" in status_code or "C" in status_code else 1
        return count

    @staticmethod
    def _checkout_payload(row):
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "path": row["path"],
            "branch": row["branch"],
            "base_revision": row["base_revision"],
            "lifecycle": row["lifecycle"],
            "dirty_file_count": row["dirty_file_count"],
            "owner_refs": row["owner_refs"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _agent_payload(row):
        return {
            "id": row["id"],
            "coding_run_id": row["coding_run_id"],
            "parent_agent_id": row["parent_agent_id"],
            "checkout_id": row["checkout_id"],
            "conversation_key": row["conversation_key"],
            "capability_policy": row["capability_policy"],
            "status": row["status"],
            "report": row["report"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
