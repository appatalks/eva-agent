#!/usr/bin/env python3
"""Focused lifecycle tests for durable Eva projects, worktrees, and coding runs."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from bridge.workspaces import WorkspaceError, WorkspaceStore


def git(directory, *args):
    return subprocess.run(
        ["git", *args], cwd=directory, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def main():
    sandbox = Path(tempfile.mkdtemp(prefix="eva-workspace-test-"))
    try:
        repository = sandbox / "repository"
        repository.mkdir()
        git(repository, "init", "-b", "main")
        git(repository, "config", "user.name", "Eva Test")
        git(repository, "config", "user.email", "eva-test@example.invalid")
        (repository / "README.md").write_text("# workspace test\n", encoding="utf-8")
        git(repository, "add", "README.md")
        git(repository, "commit", "-m", "Initial commit")

        store = WorkspaceStore(sandbox / "eva-config")
        project = store.register_project(repository)
        assert project["path"] == str(repository.resolve())
        assert project["source_checkout"]["kind"] == "source"

        run = store.create_run(project["id"], "Add durable workspace behavior", primary_session_id="sess_test")
        checkout = run["checkout"]
        checkout_path = Path(checkout["path"])
        assert checkout_path.is_dir()
        assert checkout["branch"].startswith("eva/run-")
        assert run["primary_session_id"] == "sess_test"
        assert git(checkout_path, "branch", "--show-current") == checkout["branch"]
        assert git(repository, "branch", "--show-current") == "main"

        restored = WorkspaceStore(sandbox / "eva-config")
        restored_run = restored.get_run(run["id"])
        assert restored_run["checkout"]["path"] == str(checkout_path)
        assert restored.list_projects()[0]["id"] == project["id"]
        with mock.patch.object(restored, "checkout_status", side_effect=AssertionError("list_runs must be metadata-only")):
            assert restored.list_runs(project["id"])[0]["id"] == run["id"]

        (checkout_path / "dirty.txt").write_text("must not disappear\n", encoding="utf-8")
        status = restored.checkout_status(checkout["id"])
        assert status["dirty_file_count"] == 1
        try:
            restored.discard_run(run["id"])
            raise AssertionError("dirty worktree cleanup should require confirmation")
        except WorkspaceError as error:
            assert "dirty" in str(error).lower()
        assert checkout_path.is_dir()

        discarded = restored.discard_run(run["id"], confirm_dirty=True)
        assert discarded["status"] == "discarded"
        assert not checkout_path.exists()
        assert restored.get_run(run["id"])["status"] == "discarded"

        missing_run = restored.create_run(project["id"], "Recover a missing worktree")
        missing_checkout_path = Path(missing_run["checkout"]["path"])
        shutil.rmtree(missing_checkout_path)
        recovered = restored.discard_run(missing_run["id"])
        assert recovered["status"] == "discarded"
        assert "worktree " + str(missing_checkout_path) not in git(repository, "worktree", "list", "--porcelain")

        try:
            restored.create_run(project["id"], "Reject option-like base ref", base_ref="--version")
            raise AssertionError("option-like base refs should be rejected")
        except WorkspaceError:
            pass

        try:
            store.register_project(sandbox)
            raise AssertionError("non-Git project registration should fail")
        except WorkspaceError:
            pass

        ready_project = restored.ensure_eva_ready_project()
        ready_path = Path(ready_project["path"])
        assert ready_project["name"] == "Eva Ready Workspace"
        assert (ready_path / ".git").is_dir()
        assert (ready_path / "README.md").is_file()
        ready_run = restored.create_run(ready_project["id"], "Run in Eva ready workspace")
        assert Path(ready_run["checkout"]["path"]).is_dir()
        assert restored.discard_run(ready_run["id"])["status"] == "discarded"

        lifecycle_run = restored.create_run(project["id"], "Protect archive lifecycle")
        lifecycle_agent_id = "11111111-1111-4111-8111-111111111111"
        restored.create_agent_run(
            lifecycle_agent_id, lifecycle_run["id"], lifecycle_run["checkout"]["id"], "lifecycle-test"
        )
        restored.update_agent_run(lifecycle_agent_id, "running")
        try:
            restored.archive_run(lifecycle_run["id"])
            raise AssertionError("running workspace agent should block archive")
        except WorkspaceError as error:
            assert "running" in str(error).lower()
        restored.update_agent_run(lifecycle_agent_id, "done")
        archived = restored.archive_run(lifecycle_run["id"])
        assert archived["status"] == "archived"
        restored.update_agent_run(lifecycle_agent_id, "done")
        assert restored.get_run(lifecycle_run["id"])["status"] == "archived"
        assert restored.discard_run(lifecycle_run["id"])["status"] == "discarded"

        symlink_run = restored.create_run(project["id"], "Reject workspace symlink escapes")
        symlink_checkout = Path(symlink_run["checkout"]["path"])
        external = sandbox / "external-files"
        external.mkdir()
        (external / "outside.txt").write_text("outside\n", encoding="utf-8")
        (symlink_checkout / "intermediate").symlink_to(external, target_is_directory=True)
        (symlink_checkout / "leaf.txt").symlink_to(external / "outside.txt")
        for relative_path in ("intermediate/outside.txt", "leaf.txt"):
            try:
                restored.resolve_workspace_asset(symlink_run["id"], relative_path)
                raise AssertionError("workspace asset symlink should be rejected")
            except WorkspaceError:
                pass
        (symlink_checkout / "intermediate").unlink()
        (symlink_checkout / "leaf.txt").unlink()
        shutil.rmtree(symlink_checkout)
        symlink_checkout.symlink_to(external, target_is_directory=True)
        for operation in (
            lambda: restored.checkout_status(symlink_run["checkout"]["id"]),
            lambda: restored.list_workspace_assets(),
            lambda: restored.resolve_workspace_asset(symlink_run["id"], "outside.txt"),
        ):
            try:
                operation()
                raise AssertionError("managed checkout root symlink should be rejected")
            except WorkspaceError:
                pass
        symlink_checkout.unlink()
        assert restored.discard_run(symlink_run["id"])["status"] == "discarded"

        component_store = WorkspaceStore(sandbox / "component-config")
        component_project = component_store.register_project(repository)
        component_run = component_store.create_run(component_project["id"], "Reject managed component links")
        component_checkout = Path(component_run["checkout"]["path"])
        component_parent = component_checkout.parent
        shutil.rmtree(component_checkout)
        component_parent.rmdir()
        component_parent.symlink_to(external, target_is_directory=True)
        try:
            component_store.checkout_status(component_run["checkout"]["id"])
            raise AssertionError("project-ID component symlink should be rejected")
        except WorkspaceError:
            pass
        component_parent.unlink()
        component_parent.mkdir()
        assert component_store.discard_run(component_run["id"])["status"] == "discarded"

        runtime_run = component_store.create_run(component_project["id"], "Reject runtime root link")
        runtime_checkout = Path(runtime_run["checkout"]["path"])
        shutil.rmtree(runtime_checkout)
        runtime_backup = component_store.runtime_root.with_name("worktrees-backup")
        component_store.runtime_root.rename(runtime_backup)
        component_store.runtime_root.symlink_to(external, target_is_directory=True)
        try:
            component_store.checkout_status(runtime_run["checkout"]["id"])
            raise AssertionError("runtime-root symlink should be rejected")
        except WorkspaceError:
            pass
        component_store.runtime_root.unlink()
        runtime_backup.rename(component_store.runtime_root)
        assert component_store.discard_run(runtime_run["id"])["status"] == "discarded"

        git_cwd_link = sandbox / "git-cwd-link"
        git_cwd_link.symlink_to(repository, target_is_directory=True)
        git_cwd_parent_link = sandbox / "git-cwd-parent-link"
        git_cwd_parent_link.symlink_to(sandbox, target_is_directory=True)
        with mock.patch("bridge.workspaces.subprocess.run") as git_run:
            for invalid_cwd in (
                git_cwd_link,
                git_cwd_parent_link / repository.name,
                sandbox / "missing-git-cwd",
            ):
                try:
                    component_store._git_status_output(invalid_cwd, ["status", "--short"])
                    raise AssertionError("unsafe Git working directory should be rejected")
                except WorkspaceError:
                    pass
            git_run.assert_not_called()

            git_run.return_value = subprocess.CompletedProcess([], 0, stdout="clean\n", stderr="")
            code, output = component_store._git_status_output(repository, ["status", "--short"])
            assert (code, output) == (0, "clean\n")
            command = git_run.call_args.args[0]
            assert command == ["git", "-C", str(repository.resolve()), "status", "--short"]
            assert "cwd" not in git_run.call_args.kwargs
        component_store.close()

        print("workspace lifecycle tests: PASS")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    main()
