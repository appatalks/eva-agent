"""Opted-in, file-only goal rounds using the local workspace runner."""

import json
import os
import random
import threading
import time
import uuid

from bridge import state

_lock = threading.RLock()
_MIN_DELAY = 2 * 60 * 60
_MAX_DELAY = 24 * 60 * 60
_lease = None
_lease_path = None


def _acquire_scheduler(store):
    global _lease, _lease_path
    try:
        import fcntl
    except ImportError:
        return False
    path = str(store.config_dir / "goal-autonomy.lock")
    if _lease is not None and _lease_path == path:
        return True
    if _lease is not None:
        _lease.close()
        _lease = None
        _lease_path = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        lease = os.fdopen(fd, "r+")
        try:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lease.close()
            return False
        _lease, _lease_path = lease, path
        return True
    except OSError:
        return False


def _next_due(now):
    return now + random.SystemRandom().uniform(_MIN_DELAY, _MAX_DELAY)


def _store():
    from bridge.core import _workspace_store
    store = _workspace_store()
    with store.lock, store.connection:
        store.connection.execute(
            "CREATE TABLE IF NOT EXISTS goal_autonomy ("
            "goal_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, provider_config TEXT NOT NULL, "
            "last_started REAL NOT NULL DEFAULT 0)"
        )
        store.connection.execute(
            "CREATE TABLE IF NOT EXISTS goal_autonomy_round ("
            "id INTEGER PRIMARY KEY CHECK(id = 1), run_id TEXT NOT NULL DEFAULT '', "
            "goal_id TEXT NOT NULL DEFAULT '', next_due REAL NOT NULL DEFAULT 0, "
            "last_status TEXT NOT NULL DEFAULT '', last_finished REAL NOT NULL DEFAULT 0)"
        )
        # Keep the association as a tombstone if its workspace is deleted.
        store.connection.execute(
            "CREATE TABLE IF NOT EXISTS goal_autonomy_workspaces ("
            "goal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL)"
        )
        store.connection.execute("INSERT OR IGNORE INTO goal_autonomy_round(id) VALUES (1)")
    return store


def goal_settings():
    store = _store()
    with _lock, store.lock:
        goals = {row["goal_id"]: bool(row["enabled"]) for row in
                 store.connection.execute("SELECT goal_id, enabled FROM goal_autonomy")}
        workspaces = {row["goal_id"]: row["run_id"] for row in
                      store.connection.execute("SELECT goal_id, run_id FROM goal_autonomy_workspaces")}
        row = dict(store.connection.execute("SELECT * FROM goal_autonomy_round WHERE id = 1").fetchone())
    return {"goals": goals, "workspaces": workspaces, "round": row}


def set_goal(goal_id, enabled, config=None):
    from bridge.workspace_local import normalize_goal_config
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    if enabled:
        try:
            import fcntl  # noqa: F401
        except ImportError as error:
            raise ValueError("Automatic goal work requires filesystem locking on this platform.") from error
    normalized = normalize_goal_config(config) if enabled else None
    store = _store()
    with _lock, store.lock, store.connection:
        existing_enabled = store.connection.execute(
            "SELECT count(*) FROM goal_autonomy WHERE enabled=1"
        ).fetchone()[0]
        row = store.connection.execute(
            "SELECT provider_config FROM goal_autonomy WHERE goal_id = ?", (goal_id,)
        ).fetchone()
        stored_config = json.dumps(normalized) if enabled else (row["provider_config"] if row else "{}")
        store.connection.execute(
            "INSERT INTO goal_autonomy(goal_id, enabled, provider_config) VALUES (?, ?, ?) "
            "ON CONFLICT(goal_id) DO UPDATE SET enabled=excluded.enabled, provider_config=excluded.provider_config",
            (goal_id, int(enabled), stored_config),
        )
        round_row = store.connection.execute("SELECT run_id, next_due FROM goal_autonomy_round WHERE id = 1").fetchone()
        if enabled and not existing_enabled and not round_row["run_id"]:
            store.connection.execute("UPDATE goal_autonomy_round SET next_due=? WHERE id=1", (_next_due(time.time()),))


def _finish(store, status, now):
    with store.lock, store.connection:
        store.connection.execute(
            "UPDATE goal_autonomy_round SET run_id='', last_status=?, last_finished=?, next_due=? WHERE id=1",
            (status, now, _next_due(now)),
        )


def reset_goal_workspace(goal_id):
    """Detach a retained workspace without modifying or deleting its contents."""
    store = _store()
    with _lock, store.lock:
        store.connection.execute("BEGIN IMMEDIATE")
        try:
            active = store.connection.execute(
                "SELECT run_id FROM goal_autonomy_round WHERE id=1 AND goal_id=?", (goal_id,)
            ).fetchone()
            if active and active["run_id"]:
                raise ValueError("Wait for the current goal round to finish before starting fresh.")
            store.connection.execute("DELETE FROM goal_autonomy_workspaces WHERE goal_id=?", (goal_id,))
            store.connection.commit()
        except Exception:
            store.connection.rollback()
            raise


def _resume_goal_run(store, run_id, project_id, objective, config):
    from bridge.workspace_local import normalize_goal_config
    from bridge.workspaces import WorkspaceError, _utc_now
    config = normalize_goal_config(config)
    run = store.get_run(run_id)
    if (run["project_id"] != project_id or run["model_policy"] != "goal_local"
            or run["auto_approve"] or run["apply_on_success"]
            or run["status"] not in {"active", "completed", "cancelled"}):
        raise WorkspaceError("The retained goal workspace is unavailable for continuation.")
    checkout_path = store.validated_checkout_path(run["checkout"]["id"])
    if store._current_branch(checkout_path) != run["checkout"]["branch"]:
        raise WorkspaceError("The retained goal workspace changed branches.")
    agent = run.get("agent") or {}
    with state.subagent_lock:
        live = state.subagent_tasks.get(agent.get("id"))
        if live and live.get("_worker_running", False):
            raise WorkspaceError("The previous goal round is still running.")
    with store.lock, store.connection:
        current = store.connection.execute(
            "SELECT status, apply_status, apply_report FROM coding_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not current or current["status"] not in {"active", "completed", "cancelled"}:
            raise WorkspaceError("The retained goal workspace is unavailable for continuation.")
        prior_report = current["apply_report"] or ""
        if current["apply_status"]:
            prior_report = "Previous integration: " + prior_report[:850] + " Continued goal work has not been applied."
        store.connection.execute(
            "UPDATE coding_runs SET status='active', final_disposition='', objective=?, provider_config=?, "
            "apply_status='', apply_report=?, updated_at=? WHERE id=?",
            (objective, json.dumps(config), prior_report[:1000], _utc_now(), run_id),
        )
    return store.get_run(run_id)


def tick():
    """Dispatch one goal only after the previous run has ended and its delay elapsed."""
    from bridge.background import _bg_goals_query
    from bridge.core import _dispatch_workspace_run
    from bridge.kusto import _get_kusto_config
    from bridge.memory import _resolve_memory_backend
    from bridge.workspaces import WorkspaceError

    store = _store()
    now = time.time()
    with _lock:
        if not _acquire_scheduler(store):
            return
        with store.lock:
            round_row = store.connection.execute("SELECT * FROM goal_autonomy_round WHERE id=1").fetchone()
        if round_row["run_id"]:
            run = None
            try:
                run = store.get_run(round_row["run_id"])
                agent = run.get("agent") or {}
                status = agent.get("status", "")
                if status in {"starting", "running", "steering", "cancelling"}:
                    with state.subagent_lock:
                        live = state.subagent_tasks.get(agent.get("id"))
                        if live and live.get("_worker_running", True):
                            return
                    status = "interrupted"
                elif not status:
                    status = "interrupted"
            except WorkspaceError:
                status = "interrupted"
            if run and run.get("model_policy") == "goal_local" and round_row["goal_id"]:
                with store.lock, store.connection:
                    store.connection.execute(
                        "INSERT OR IGNORE INTO goal_autonomy_workspaces(goal_id, run_id) VALUES (?, ?)",
                        (round_row["goal_id"], run["id"]),
                    )
            _finish(store, status, now)
            return
        if not round_row["next_due"] or now < round_row["next_due"]:
            return
        with store.lock:
            enabled = store.connection.execute(
                "SELECT * FROM goal_autonomy WHERE enabled=1 ORDER BY last_started, goal_id"
            ).fetchall()
        if not enabled:
            return
        backend = _resolve_memory_backend()
        cluster, database = (None, None) if backend == "sqlite" else _get_kusto_config()
        goals = _bg_goals_query({"backend": backend, "cluster": cluster, "database": database})
        if goals is None:
            return
        active = {str(goal.get("GoalId")): goal for goal in goals
                  if str(goal.get("Status") or "").lower() == "active"}
        selected = next((row for row in enabled if row["goal_id"] in active), None)
        if not selected:
            return
        reservation = "reserved:" + uuid.uuid4().hex
        with store.lock, store.connection:
            claimed = store.connection.execute(
                "UPDATE goal_autonomy_round SET run_id=?, goal_id=?, next_due=0, last_status='starting' "
                "WHERE id=1 AND run_id='' AND next_due<=? "
                "AND EXISTS (SELECT 1 FROM goal_autonomy WHERE goal_id=? AND enabled=1)",
                (reservation, selected["goal_id"], now, selected["goal_id"]),
            ).rowcount
            if not claimed:
                return
            store.connection.execute("UPDATE goal_autonomy SET last_started=? WHERE goal_id=?",
                                     (now, selected["goal_id"]))
        goal = active[selected["goal_id"]]
        objective = (str(goal.get("Title") or "").strip() + "\n\n" +
                     str(goal.get("Description") or "").strip()).strip()[:4000]
        retained = None
        try:
            project = store.ensure_eva_ready_project()
            config = json.loads(selected["provider_config"])
            with store.lock:
                retained = store.connection.execute(
                    "SELECT run_id FROM goal_autonomy_workspaces WHERE goal_id=?", (selected["goal_id"],)
                ).fetchone()
            if retained:
                run = _resume_goal_run(store, retained["run_id"], project["id"], objective, config)
            else:
                run = store.create_run(project["id"], objective, model_policy="goal_local",
                                       provider_config=config)
            with store.lock, store.connection:
                store.connection.execute(
                    "INSERT OR IGNORE INTO goal_autonomy_workspaces(goal_id, run_id) VALUES (?, ?)",
                    (selected["goal_id"], run["id"]),
                )
                store.connection.execute(
                    "UPDATE goal_autonomy_round SET run_id=? WHERE id=1 AND run_id=?",
                    (run["id"], reservation),
                )
            _dispatch_workspace_run(run)
        except (WorkspaceError, ValueError, OSError, json.JSONDecodeError):
            _finish(store, "blocked: review retained workspace" if retained else "failed to start", time.time())
