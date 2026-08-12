#!/usr/bin/env python3
"""Focused lifecycle tests for durable Eva projects, worktrees, and coding runs."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from bridge.workspaces import WorkspaceError, WorkspaceStore
from bridge.utils import _subagent_mcp_config


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
        (repository / " leading.txt").write_text("leading space\n", encoding="utf-8")
        git(repository, "add", "README.md", " leading.txt")
        git(repository, "commit", "-m", "Initial commit")

        store = WorkspaceStore(sandbox / "eva-config")
        template = type("Template", (), {"mcp_config": {
            "global-github": {"command": "github-mcp", "env": {"GITHUB_PAT": "must-not-cross"}}
        }})()
        workspace_task = {"_workspace_mcp_config": {
            "workspace-docs": {"command": "workspace-mcp", "args": [], "env": {}}
        }}
        assert _subagent_mcp_config(template, workspace_task) == workspace_task["_workspace_mcp_config"]
        assert "global-github" not in _subagent_mcp_config(template, workspace_task)
        assert _subagent_mcp_config(template, {}) == template.mcp_config
        project = store.register_project(repository)
        assert project["path"] == str(repository.resolve())
        assert project["source_checkout"]["kind"] == "source"
        project_files = store.list_project_files(project["id"])
        assert project_files == {"files": [" leading.txt", "README.md"], "truncated": False}
        assert store.resolve_project_file(project["id"], " leading.txt") == str(repository / " leading.txt")
        assert store.resolve_project_file(project["id"], "README.md") == str(repository / "README.md")
        try:
            store.resolve_project_file(project["id"], "../README.md")
            raise AssertionError("project file traversal should be rejected")
        except WorkspaceError:
            pass

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

        (repository / "mcp.json").write_text(
            '{"mcpServers":{"project-docs":{"command":"example-mcp","args":["--docs"],"env":{"EXAMPLE_TOKEN":"not-rendered"}}}}',
            encoding="utf-8",
        )
        git(repository, "add", "mcp.json")
        git(repository, "commit", "-m", "Add workspace MCP configuration")
        mcp_metadata = restored.get_project(project["id"])["mcp_servers"]
        assert mcp_metadata["source"] == "workspace MCP discovery" and mcp_metadata["state"] == "ready"
        server_metadata = mcp_metadata["servers"][0]
        assert server_metadata["name"] == "project-docs" and server_metadata["transport"] == "stdio"
        assert server_metadata["source"] == "mcp.json"
        assert server_metadata["command"] == "example-mcp" and server_metadata["args"] == ["--docs"]
        assert server_metadata["env_keys"] == ["EXAMPLE_TOKEN"] and "env" not in server_metadata
        assert server_metadata["enabled"] is False and len(server_metadata["digest"]) == 64
        unapproved_run = restored.create_run(project["id"], "Do not use an unapproved server")
        assert restored.mcp_config_for_run(unapproved_run["id"]) == {}
        assert restored.discard_run(unapproved_run["id"])["status"] == "discarded"
        try:
            restored.set_project_mcp_server_enabled(project["id"], "project-docs", True)
            raise AssertionError("workspace MCP enablement should require the reviewed digest")
        except WorkspaceError:
            pass
        enabled_project = restored.set_project_mcp_server_enabled(
            project["id"], "project-docs", True, server_metadata["digest"]
        )
        assert enabled_project["mcp_servers"]["servers"][0]["enabled"] is True
        mcp_run = restored.create_run(project["id"], "Use the project documentation server")
        mcp_run_config = Path(mcp_run["checkout"]["path"]) / "mcp.json"
        original_run_config = mcp_run_config.read_text(encoding="utf-8")
        mcp_run_config.write_text(
            '{"mcpServers":{"project-docs":{"command":"changed-command"}}}', encoding="utf-8"
        )
        assert restored.mcp_config_for_run(mcp_run["id"]) == {}
        mcp_run_config.write_text(original_run_config, encoding="utf-8")
        assert restored.mcp_config_for_run(mcp_run["id"]) == {}
        revoked_metadata = restored.get_project(project["id"])["mcp_servers"]["servers"][0]
        assert revoked_metadata["enabled"] is False
        restored.set_project_mcp_server_enabled(project["id"], "project-docs", True, revoked_metadata["digest"])
        assert restored.mcp_config_for_run(mcp_run["id"]) == {
            "project-docs": {
                "command": "example-mcp",
                "args": ["--docs"],
                "env": {"EXAMPLE_TOKEN": "not-rendered"},
            }
        }
        assert restored.discard_run(mcp_run["id"])["status"] == "discarded"
        try:
            restored.set_project_mcp_server_enabled(project["id"], "unknown-server", True)
            raise AssertionError("unknown workspace MCP server should be rejected")
        except WorkspaceError:
            pass
        for reserved_key in ("PATH", "BASH_ENV", "PYTHONSTARTUP", "LD_AUDIT", "DYLD_INSERT_LIBRARIES"):
            (repository / "mcp.json").write_text(
                '{"mcpServers":{"unsafe":{"command":"unsafe-command","env":{"' + reserved_key + '":"unsafe"}}}}',
                encoding="utf-8",
            )
            assert restored.list_project_mcp_servers(project["id"])["state"] == "invalid"
        (repository / "mcp.json").write_text(
            '{"mcpServers":{"project-docs":{"command":"example-mcp","args":["--docs"],"env":{"EXAMPLE_TOKEN":"not-rendered"}}}}',
            encoding="utf-8",
        )

        multi_repository = sandbox / "multi-mcp-repository"
        multi_repository.mkdir()
        git(multi_repository, "init", "-b", "main")
        git(multi_repository, "config", "user.name", "Eva Test")
        git(multi_repository, "config", "user.email", "eva-test@example.invalid")
        (multi_repository / ".vscode").mkdir()
        (multi_repository / ".github").mkdir()
        (multi_repository / ".mcp.json").write_text(
            '{"mcpServers":{"root-module":{"command":"root-mcp"}}}', encoding="utf-8"
        )
        (multi_repository / ".vscode" / "mcp.json").write_text(
            '{"servers":{"editor-module":{"command":"editor-mcp"}}}', encoding="utf-8"
        )
        (multi_repository / ".github" / "mcp.json").write_text(
            '{"mcpServers":{"workflow-module":{"command":"workflow-mcp"}}}', encoding="utf-8"
        )
        git(multi_repository, "add", ".")
        git(multi_repository, "commit", "-m", "Add distributed MCP configuration")
        multi_project = restored.register_project(multi_repository)
        multi_mcp = restored.get_project(multi_project["id"])["mcp_servers"]
        multi_servers = {server["name"]: server for server in multi_mcp["servers"]}
        assert set(multi_servers) == {"root-module", "editor-module", "workflow-module"}
        assert multi_servers["root-module"]["source"] == ".mcp.json"
        assert multi_servers["editor-module"]["source"] == ".vscode/mcp.json"
        assert multi_servers["workflow-module"]["source"] == ".github/mcp.json"
        restored.set_project_mcp_server_enabled(
            multi_project["id"], "editor-module", True, multi_servers["editor-module"]["digest"]
        )
        multi_run = restored.create_run(multi_project["id"], "Use the editor MCP module")
        assert restored.mcp_config_for_run(multi_run["id"]) == {
            "editor-module": {"command": "editor-mcp", "args": [], "env": {}}
        }
        assert restored.discard_run(multi_run["id"])["status"] == "discarded"

        def fake_github_clone(source_url, destination, github_token=""):
            assert source_url == "https://github.com/eva-test/demo.git"
            assert github_token == ""
            destination.mkdir(parents=True)
            git(destination, "init", "-b", "main")
            git(destination, "config", "user.name", "Eva Test")
            git(destination, "config", "user.email", "eva-test@example.invalid")
            (destination / "README.md").write_text("# imported workspace\n", encoding="utf-8")
            git(destination, "add", "README.md")
            git(destination, "commit", "-m", "Initial import")

        with mock.patch.object(restored, "_clone_github_repository", side_effect=fake_github_clone) as clone:
            imported = restored.import_github_repository("https://github.com/eva-test/demo")
            assert imported["name"] == "eva-test/demo"
            assert imported["mcp_servers"]["state"] == "missing"
            assert restored.import_github_repository("https://github.com/eva-test/demo.git")["id"] == imported["id"]
            clone.assert_called_once()
        for invalid_url in (
            "git@github.com:eva-test/demo.git",
            "https://github.com/eva-test/demo?token=not-allowed",
            "https://github.com/eva-test/demo#fragment",
            "https://github.com/eva-test/demo/extra",
            "https://github.com:443/eva-test/demo",
            "https://github.com.evil.example/eva-test/demo",
            "https://evil.example/https://github.com/eva-test/demo",
            "https://attacker@example.com@github.com/eva-test/demo",
            "https://example.com/eva-test/demo",
        ):
            try:
                restored.import_github_repository(invalid_url)
                raise AssertionError("unsafe GitHub repository URL should be rejected")
            except WorkspaceError:
                pass

        clone_destination = sandbox / "clone-environment-test"
        with mock.patch("bridge.workspaces.subprocess.run") as clone_run:
            clone_run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="failed")
            try:
                restored._clone_github_repository("https://github.com/eva-test/demo.git", clone_destination)
                raise AssertionError("failed clone should raise")
            except WorkspaceError:
                pass
            clone_command = clone_run.call_args.args[0]
            clone_environment = clone_run.call_args.kwargs["env"]
            assert clone_command[:5] == ["git", "-c", "credential.helper=", "-c", "core.askPass="]
            assert clone_environment["GIT_CONFIG_NOSYSTEM"] == "1"
            assert clone_environment["GIT_CONFIG_GLOBAL"] == os.devnull
            assert clone_environment["GIT_TERMINAL_PROMPT"] == "0"
            assert clone_environment["GCM_INTERACTIVE"] == "Never"
            assert clone_environment["HOME"].endswith("git-import-home")
            assert "EVA_GITHUB_TOKEN" not in clone_environment

        clone_failure_cases = [
            ("remote: Repository not found.", True, "Contents: Read"),
            ("fatal: Authentication failed", True, "authentication was rejected"),
            ("fatal: unable to access: Could not resolve host: github.com", True, "could not be reached"),
            ("fatal: repository not found", False, "Configure a GitHub PAT"),
        ]
        for stderr, authenticated, expected_message in clone_failure_cases:
            with mock.patch("bridge.workspaces.subprocess.run") as clone_run:
                clone_run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)
                try:
                    restored._clone_github_repository(
                        "https://github.com/eva-test/private-demo.git",
                        sandbox / ("clone-failure-" + str(len(stderr))),
                        github_token="ghp_TEST_RUNTIME_ONLY" if authenticated else "",
                    )
                    raise AssertionError("failed clone should raise")
                except WorkspaceError as error:
                    assert expected_message.lower() in str(error).lower()
                    assert stderr not in str(error)
                    assert "ghp_TEST_RUNTIME_ONLY" not in str(error)

        authenticated_destination = sandbox / "authenticated-clone-test"
        observed_askpass = []
        def authenticated_clone(command, **kwargs):
            environment = kwargs["env"]
            askpass = Path(environment["GIT_ASKPASS"])
            observed_askpass.append((askpass, askpass.exists(), askpass.stat().st_mode & 0o777, command, dict(environment)))
            return subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch("bridge.workspaces.subprocess.run", side_effect=authenticated_clone):
            restored._clone_github_repository(
                "https://github.com/eva-test/private-demo.git",
                authenticated_destination,
                github_token="ghp_TEST_RUNTIME_ONLY",
            )
        askpass, existed, mode, command, environment = observed_askpass[0]
        assert existed and mode == 0o700
        assert "ghp_TEST_RUNTIME_ONLY" not in " ".join(command)
        assert environment["EVA_GITHUB_TOKEN"] == "ghp_TEST_RUNTIME_ONLY"
        assert not askpass.exists()

        symlink_import_store = WorkspaceStore(sandbox / "symlink-import-config")
        symlink_import_external = sandbox / "symlink-import-external"
        symlink_import_external.mkdir()
        (symlink_import_store.config_dir / "projects").symlink_to(symlink_import_external, target_is_directory=True)
        with mock.patch.object(symlink_import_store, "_clone_github_repository") as clone:
            try:
                symlink_import_store.import_github_repository("https://github.com/eva-test/escaped")
                raise AssertionError("symlinked import ancestor should be rejected")
            except WorkspaceError:
                pass
            clone.assert_not_called()
        symlink_import_store.close()

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

        cancelled_run = restored.create_run(project["id"], "Report a cancelled workspace permission")
        cancelled_agent_id = "22222222-2222-4222-8222-222222222222"
        restored.create_agent_run(
            cancelled_agent_id, cancelled_run["id"], cancelled_run["checkout"]["id"], "cancelled-test"
        )
        restored.update_agent_run(cancelled_agent_id, "cancelled", "Execution permission was not approved.")
        assert restored.get_run(cancelled_run["id"])["status"] == "cancelled"
        assert restored.get_run(cancelled_run["id"])["final_disposition"] == "agent_cancelled"
        assert restored.discard_run(cancelled_run["id"])["status"] == "discarded"

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

        source_parent = sandbox / "source-parent"
        source_repository = source_parent / "repository"
        source_repository.mkdir(parents=True)
        git(source_repository, "init", "-b", "main")
        git(source_repository, "config", "user.name", "Eva Test")
        git(source_repository, "config", "user.email", "eva-test@example.invalid")
        (source_repository / "README.md").write_text("# original source\n", encoding="utf-8")
        git(source_repository, "add", "README.md")
        git(source_repository, "commit", "-m", "Initial source")
        source_store = WorkspaceStore(sandbox / "source-config")
        source_project = source_store.register_project(source_repository)
        source_parent.rename(sandbox / "source-parent-original")
        replacement_parent = sandbox / "source-parent-replacement"
        replacement_repository = replacement_parent / "repository"
        replacement_repository.mkdir(parents=True)
        git(replacement_repository, "init", "-b", "main")
        git(replacement_repository, "config", "user.name", "Eva Test")
        git(replacement_repository, "config", "user.email", "eva-test@example.invalid")
        (replacement_repository / "README.md").write_text("# replacement source\n", encoding="utf-8")
        (replacement_repository / "mcp.json").write_text(
            '{"mcpServers":{"replacement":{"command":"replacement-command"}}}', encoding="utf-8"
        )
        git(replacement_repository, "add", "README.md", "mcp.json")
        git(replacement_repository, "commit", "-m", "Replacement source")
        source_parent.symlink_to(replacement_parent, target_is_directory=True)
        source_checkout_id = source_project["source_checkout"]["id"]
        for operation in (
            lambda: source_store.checkout_status(source_checkout_id),
            lambda: source_store.list_project_files(source_project["id"]),
            lambda: source_store.set_project_mcp_server_enabled(source_project["id"], "replacement", True),
        ):
            try:
                operation()
                raise AssertionError("ancestor-swapped source project should be rejected")
            except WorkspaceError:
                pass
        assert source_store.list_project_mcp_servers(source_project["id"])["state"] == "invalid"
        source_store.close()

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
