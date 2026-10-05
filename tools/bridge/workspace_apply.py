"""Reviewed, local-only integration of retained coding-run worktrees."""

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from workspace_diff_guard import scan_diff, scan_text, sensitive_path
from bridge.acp_client import _workspace_argument_is_protected
from bridge.workspaces import WorkspaceError


def changed_paths(store, root, arguments):
    code, output = store._git_status_output(str(root), arguments)
    if code != 0:
        raise WorkspaceError("Changed files could not be reviewed.")
    return set(filter(None, output.split("\0")))


def _reviewed_snapshot(store, root, paths):
    """Capture the exact reviewed bytes and executable modes for each path."""
    fingerprints = []
    for relative in sorted(paths):
        if sensitive_path(relative) or _workspace_argument_is_protected(relative) or ".git" in Path(relative).parts:
            raise WorkspaceError("Protected files cannot be committed or applied by Eva.")
        candidate = root / relative
        if candidate.exists() or candidate.is_symlink():
            candidate = store._resolve_checkout_file(root, relative)
            metadata = candidate.stat()
            if metadata.st_nlink > 1 or metadata.st_size > 32 * 1024 * 1024:
                raise WorkspaceError("Hardlinked or oversized files require manual review.")
            content = candidate.read_bytes()
            if scan_text(content.decode("utf-8", errors="ignore"), "workspace"):
                raise WorkspaceError("Credential material was detected in a changed file. Nothing was applied.")
            fingerprints.append([relative, hashlib.sha256(content).hexdigest(), stat.S_IMODE(metadata.st_mode)])
        else:
            fingerprints.append([relative, "deleted", None])
    return fingerprints


def _git_bytes(store, root, arguments, index_file):
    environment = store._git_environment()
    environment["GIT_INDEX_FILE"] = os.path.abspath(os.fspath(index_file))
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkspaceError("Git operation failed.") from error
    if completed.returncode != 0:
        raise WorkspaceError("Git operation failed.")
    return completed.stdout


def _apply_paths(store, root, source_head):
    paths = set()
    for args in [
        ["diff", "--name-only", "-z", source_head],
        ["diff", "--cached", "--name-only", "-z"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ]:
        paths.update(changed_paths(store, root, args))
    return paths


def _verify_temporary_index(store, root, source_head, reviewed, index_file):
    """Verify the temporary index is exactly the reviewed snapshot."""
    expected = {entry[0]: entry for entry in reviewed}
    staged_names = set(filter(None, store._git(
        str(root), ["diff", "--cached", "--name-only", "-z", source_head, "--"],
        index_file=index_file,
    ).split("\0")))
    if staged_names != set(expected):
        raise WorkspaceError("The coding workspace gained or lost files after review; nothing was applied.")
    staged_entries = {}
    for relative in sorted(expected):
        output = _git_bytes(
            store, root, ["--literal-pathspecs", "ls-files", "--stage", "-z", "--", relative], index_file
        )
        records = [record for record in output.split(b"\0") if record]
        if expected[relative][1] == "deleted":
            if records:
                raise WorkspaceError("A reviewed deletion changed before staging; nothing was applied.")
            continue
        if len(records) != 1 or b"\t" not in records[0]:
            raise WorkspaceError("The reviewed file could not be verified in the temporary index.")
        header, encoded_path = records[0].split(b"\t", 1)
        mode, object_id, stage = header.decode("ascii").split()
        if stage != "0" or encoded_path.decode("utf-8", errors="surrogateescape") != relative:
            raise WorkspaceError("The temporary index contains an unexpected path.")
        expected_mode = "100755" if expected[relative][2] & 0o111 else "100644"
        if mode != expected_mode:
            raise WorkspaceError("A reviewed file mode changed before staging; nothing was applied.")
        blob = _git_bytes(store, root, ["cat-file", "blob", object_id], index_file)
        if hashlib.sha256(blob).hexdigest() != expected[relative][1]:
            raise WorkspaceError("A reviewed file changed before staging; nothing was applied.")
        staged_entries[relative] = object_id
    staged_patch = store._git(
        str(root),
        ["diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", source_head, "--"],
        index_file=index_file,
    )
    if scan_diff(staged_patch):
        raise WorkspaceError("Credential material or a sensitive file was detected. Nothing was applied.")
    return staged_entries


def _require_reviewed_revisions(store, root, source, run, preview):
    if (
        store._current_branch(str(root)) != run["checkout"]["branch"]
        or store._git(str(root), ["rev-parse", "HEAD"]) != preview["run_revision"]
        or store._current_branch(str(source)) != preview["target_branch"]
        or store._git(str(source), ["rev-parse", "HEAD"]) != preview["source_revision"]
        or store._git(str(source), ["status", "--porcelain=v1", "-z"])
    ):
        raise WorkspaceError("The source or run changed after review. Preview it again; nothing was applied.")


def _commit_reviewed_changes(store, root, source, run, preview):
    reviewed_paths = set(preview["changed_files"])
    reviewed = preview["reviewed_snapshot"]
    index_path = Path(store._git(str(root), ["rev-parse", "--path-format=absolute", "--git-path", "index"]))
    lock_path = index_path.with_name(index_path.name + ".lock")
    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        raise WorkspaceError("The coding workspace index is busy or unavailable. Wait for other Git operations before applying.") from error
    temporary_index = None
    published = False
    try:
        _require_reviewed_revisions(store, root, source, run, preview)
        if _apply_paths(store, root, preview["source_revision"]) != reviewed_paths or _reviewed_snapshot(
            store, root, reviewed_paths
        ) != reviewed:
            raise WorkspaceError("The coding workspace changed after review; nothing was applied.")
        with tempfile.NamedTemporaryFile(prefix="eva-apply-index-", suffix=".index", dir=store.runtime_root, delete=False) as file:
            temporary_index = file.name
        os.unlink(temporary_index)
        store._git(str(root), ["read-tree", preview["run_revision"]], index_file=temporary_index)
        store._git(
            str(root), ["--literal-pathspecs", "add", "--all", "--", *sorted(reviewed_paths)],
            index_file=temporary_index,
        )
        _verify_temporary_index(store, root, preview["source_revision"], reviewed, temporary_index)
        if _apply_paths(store, root, preview["source_revision"]) != reviewed_paths or _reviewed_snapshot(
            store, root, reviewed_paths
        ) != reviewed:
            raise WorkspaceError("The coding workspace changed while staging reviewed files; nothing was applied.")
        tree = store._git(str(root), ["write-tree"], index_file=temporary_index)
        revision = store._git(str(root), [
            "-c", "commit.gpgsign=false",
            "-c", "user.name=Eva Workspace", "-c", "user.email=eva-workspace@local.invalid",
            "commit-tree", tree, "-p", preview["run_revision"], "-m", "Apply coding workspace changes",
            "-m", "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>",
        ], index_file=temporary_index)
        # Hold Git's real index lock throughout preparation; never reset an index
        # another Git process may have staged while the review was in progress.
        with open(temporary_index, "rb") as staged_index, os.fdopen(lock_fd, "wb", closefd=False) as locked_index:
            locked_index.write(staged_index.read())
            locked_index.flush()
            os.fsync(lock_fd)
        _require_reviewed_revisions(store, root, source, run, preview)
        store._git(str(root), [
            "update-ref", "refs/heads/" + run["checkout"]["branch"], revision, preview["run_revision"],
        ])
        os.close(lock_fd)
        lock_fd = None
        try:
            os.replace(lock_path, index_path)
        except OSError as error:
            raise WorkspaceError("The reviewed run commit was kept, but its index could not be published. Nothing was merged; inspect the run before retrying.") from error
        published = True
        return revision
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if not published:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
        if temporary_index is not None:
            try:
                os.unlink(temporary_index)
            except FileNotFoundError:
                pass


def preview_apply(store, run_id):
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
    fingerprints = _reviewed_snapshot(store, root, paths)
    snapshot = [run_id, source_head, run_head, branch, dirty, patch, fingerprints]
    return {
        "run_id": run_id, "project_name": run["project"]["name"], "target_branch": branch,
        "source_revision": source_head, "run_revision": run_head,
        "changed_files": sorted(paths), "will_commit": bool(dirty),
        "already_applied": contained and not dirty,
        "reviewed_snapshot": fingerprints,
        "fingerprint": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
    }


def apply_run(store, run_id, expected_fingerprint):
    with store.lock:
        preview = preview_apply(store, run_id)
        if not isinstance(expected_fingerprint, str) or preview["fingerprint"] != expected_fingerprint:
            raise WorkspaceError("The source or run changed after review. Preview it again; nothing was applied.")
        run = store.get_run(run_id)
        root = store._validated_managed_checkout(run["checkout"])
        source = store._validated_source_project(run["project_id"])
        if preview["will_commit"]:
            revision = _commit_reviewed_changes(store, root, source, run, preview)
        else:
            _require_reviewed_revisions(store, root, source, run, preview)
            revision = preview["run_revision"]
        if not preview["already_applied"]:
            if preview["will_commit"]:
                post_paths = _apply_paths(store, root, preview["source_revision"])
                if post_paths != set(preview["changed_files"]) or _reviewed_snapshot(
                    store, root, post_paths
                ) != preview.get("reviewed_snapshot", []):
                    raise WorkspaceError(
                        "The coding workspace changed while its verified commit was prepared. "
                        "The run commit was kept; nothing was merged."
                    )
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
