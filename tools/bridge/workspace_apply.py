"""Reviewed, local-only integration of retained coding-run worktrees."""

import hashlib
import json
import os
from pathlib import Path

from workspace_diff_guard import scan_diff, scan_text, sensitive_path
from bridge.acp_client import _workspace_argument_is_protected


def changed_paths(store, root, arguments):
    from bridge.workspaces import WorkspaceError
    code, output = store._git_status_output(str(root), arguments)
    if code != 0:
        raise WorkspaceError("Changed files could not be reviewed.")
    return set(filter(None, output.split("\0")))


def preview_apply(store, run_id):
    from bridge.workspaces import WorkspaceError
    run = store.get_run(run_id)
    if run["status"] not in {"completed", "cancelled", "active", "archived"} or run["checkout"]["lifecycle"] != "active":
        raise WorkspaceError("This retained run is not available to apply.")
    if run.get("agent") and run["agent"]["status"] in {"starting", "running", "steering", "cancelling"}:
        raise WorkspaceError("Wait for the coding agent to stop before applying its changes.")
    root = store._validated_managed_checkout(run["checkout"])
    source = store._validated_source_project(run["project_id"])
    branch = store._current_branch(str(source))
    target = run["target_branch"] or store.get_project(run["project_id"])["source_checkout"]["branch"]
    if not branch or branch == "HEAD" or branch != target:
        raise WorkspaceError("The source checkout changed branches. Return to the original target branch before applying.")
    if store._git(str(source), ["status", "--porcelain=v1", "-z"]):
        raise WorkspaceError("The source folder has local changes. Commit or stash them yourself before applying; Eva will not overwrite them.")
    source_head = store._git(str(source), ["rev-parse", "HEAD"])
    run_head = store._git(str(root), ["rev-parse", "HEAD"])
    current_run_branch = store._current_branch(str(root))
    if current_run_branch != run["checkout"]["branch"]:
        raise WorkspaceError("The coding worktree changed branches and cannot be applied.")
    dirty = store._git(str(root), ["status", "--porcelain=v1", "-z"])
    contained = store._git_status(str(source), ["merge-base", "--is-ancestor", run_head, source_head]) == 0
    if not (contained and not dirty) and store._git_status(str(source), ["merge-base", "--is-ancestor", source_head, run_head]) != 0:
        raise WorkspaceError("The source and run branches have diverged. Review and integrate them manually; Eva only fast-forwards.")
    paths = set()
    for arguments in [
        ["diff", "--name-only", "-z", source_head],
        ["diff", "--cached", "--name-only", "-z"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ]:
        if not (contained and not dirty):
            paths.update(changed_paths(store, root, arguments))
    if len(paths) > 1000:
        raise WorkspaceError("This apply exceeds the 1,000-file review limit.")
    patch = "" if contained and not dirty else store._git(str(root), ["diff", "--binary", "--no-ext-diff", "--no-textconv", source_head])
    if len(patch) > 32 * 1024 * 1024:
        raise WorkspaceError("This apply exceeds the bounded diff review limit.")
    if scan_diff(patch):
        raise WorkspaceError("Credential material or a sensitive file was detected. Review the run manually; nothing was applied.")
    fingerprints = []
    for relative in sorted(paths):
        if sensitive_path(relative) or _workspace_argument_is_protected(relative) or ".git" in Path(relative).parts:
            raise WorkspaceError("Protected files cannot be committed or applied by Eva.")
        candidate = root / relative
        if candidate.exists() or candidate.is_symlink():
            candidate = store._resolve_checkout_file(root, relative)
            if candidate.stat().st_nlink > 1 or candidate.stat().st_size > 32 * 1024 * 1024:
                raise WorkspaceError("Hardlinked or oversized files require manual review.")
            content = candidate.read_bytes()
            if scan_text(content.decode("utf-8", errors="ignore"), "workspace"):
                raise WorkspaceError("Credential material was detected in a changed file. Nothing was applied.")
            fingerprints.append([relative, hashlib.sha256(content).hexdigest(), candidate.stat().st_mode])
        else:
            fingerprints.append([relative, "deleted"])
    snapshot = [run_id, source_head, run_head, branch, dirty, patch, fingerprints]
    return {
        "run_id": run_id, "project_name": run["project"]["name"], "target_branch": branch,
        "source_revision": source_head, "run_revision": run_head,
        "changed_files": sorted(paths), "will_commit": bool(dirty),
        "already_applied": contained and not dirty,
        "fingerprint": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
    }


def apply_run(store, run_id, expected_fingerprint):
    from bridge.workspaces import WorkspaceError
    with store.lock:
        preview = preview_apply(store, run_id)
        if not isinstance(expected_fingerprint, str) or preview["fingerprint"] != expected_fingerprint:
            raise WorkspaceError("The source or run changed after review. Preview it again; nothing was applied.")
        run = store.get_run(run_id)
        root = store._validated_managed_checkout(run["checkout"])
        source = store._validated_source_project(run["project_id"])
        if preview["will_commit"]:
            staged = set()
            for args in [["diff", "--name-only", "-z"], ["diff", "--cached", "--name-only", "-z"], ["ls-files", "--others", "--exclude-standard", "-z"]]:
                staged.update(changed_paths(store, root, args))
            if staged:
                store._git(str(root), ["add", "--all", "--", *sorted(staged)])
                store._git(str(root), [
                    "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false",
                    "-c", "user.name=Eva Workspace", "-c", "user.email=eva-workspace@local.invalid",
                    "commit", "-m", "Apply coding workspace changes",
                    "-m", "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>",
                ])
        revision = store._git(str(root), ["rev-parse", "HEAD"])
        if not preview["already_applied"]:
            if store._current_branch(str(source)) != preview["target_branch"] or store._git(str(source), ["rev-parse", "HEAD"]) != preview["source_revision"]:
                raise WorkspaceError("The source changed while preparing the run commit. The run commit was kept; nothing was merged.")
            if store._git(str(source), ["status", "--porcelain=v1", "-z"]):
                raise WorkspaceError("New source edits appeared. The run commit was kept; nothing was merged.")
            store._git(str(source), [
                "-c", "core.hooksPath=" + os.devnull,
                "merge", "--ff-only", "--no-edit", revision,
            ])
        if store._current_branch(str(source)) != preview["target_branch"] or store._git_status(str(source), ["merge-base", "--is-ancestor", revision, "HEAD"]) != 0:
            raise WorkspaceError("Source integration could not be verified. Inspect both checkouts before continuing.")
        message = "Already applied to " if preview["already_applied"] else "Applied to "
        message += preview["target_branch"] + "; source contains verified run revision " + revision[:12] + ". No remote push was performed."
        store.record_apply(run_id, "applied", message, revision)
        return {"applied": True, "already_applied": preview["already_applied"], "target_branch": preview["target_branch"], "revision": revision, "message": message}
