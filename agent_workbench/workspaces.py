"""Declared checkout ownership and retained, per-attempt Git worktrees.

Leases coordinate Workbench controllers sharing a state root. They are not an
OS sandbox and do not constrain unrelated editors or native agent tool calls.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import selectors
import subprocess
import time


GIT_TIMEOUT_SECONDS = 30
GIT_OUTPUT_LIMIT = 1_048_576


class WorkspaceBusy(RuntimeError):
    pass


class GitWorkspaceError(ValueError):
    """Content-free helper failure; output may contain hooks or credentials."""

    def __init__(self, reason, exit_code=None):
        super().__init__("Git workspace operation failed; inspect the project locally")
        self.reason = reason
        self.exit_code = exit_code


@contextmanager
def lease(store, project, mode):
    if mode == "worktree":
        yield
        return
    locks = store.root / "checkout-locks"
    if locks.is_symlink():
        raise ValueError("Checkout lock directory must not be a symbolic link")
    locks.mkdir(mode=0o700, exist_ok=True)
    locks.chmod(0o700)
    try:
        checkout = Path(_git(project, "rev-parse", "--show-toplevel")).resolve()
    except GitWorkspaceError as exc:
        # A failed metadata helper must not weaken checkout ownership to a
        # subdirectory lock. Only Git's ordinary outside-repository result is
        # eligible for fallback, and even that must not hide a broken .git.
        project_path = Path(project).resolve()
        marker = any(os.path.lexists(parent / ".git") for parent in (project_path, *project_path.parents))
        no_repository = exc.reason == "exit" and exc.exit_code == 128
        no_git = exc.reason == "missing_executable"
        directed_git = any(os.environ.get(key) for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"))
        if not (no_repository or no_git) or marker or directed_git:
            raise
        checkout = Path(project).resolve()
    key = hashlib.sha256(os.fsencode(checkout)).hexdigest()
    descriptor = os.open(locks / key, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_SH if mode == "read-only" else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceBusy("Another Workbench task owns this checkout") from exc
        yield
    finally:
        os.close(descriptor)


def compatible(task, active_tasks):
    """A declared reader may overlap readers, never a shared writer."""
    mode = task.get("workspace", "shared")
    if mode == "worktree":
        return True
    others = [other.get("workspace", "shared") for other in active_tasks]
    return all(other == "worktree" or (mode == other == "read-only") for other in others)


def _git(project, *args):
    """Run Git with bounded capture and ownership of its hook/helper group.

    A timeout or a parent exiting while a hook keeps the pipes open must not
    leave descendants behind. Capture limits apply to stdout and stderr
    together; stderr is counted but never retained or included in exceptions.
    A helper that deliberately creates a new session is outside this process
    group, just as for native task process groups; this is not an OS sandbox.
    """
    from .runner import _terminate_group
    process = None
    selector = selectors.DefaultSelector()
    output = bytearray()
    captured = 0
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(["git", "-C", str(project), *args], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, close_fds=True)
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitWorkspaceError("timeout")
            exited = process.poll() is not None
            ready = selector.select(0 if exited else min(0.05, remaining))
            if exited and not ready:
                # Drain bytes already available, then clean a hook that still
                # holds a pipe instead of waiting for it until the full timeout.
                break
            for key, _ in ready:
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                captured += len(chunk)
                if captured > GIT_OUTPUT_LIMIT:
                    raise GitWorkspaceError("output_limit")
                if key.fileobj is process.stdout:
                    output.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitWorkspaceError("timeout")
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise GitWorkspaceError("timeout") from None
        if code:
            raise GitWorkspaceError("exit", code)
        return output.decode("utf-8", "replace").strip()
    except FileNotFoundError:
        raise GitWorkspaceError("missing_executable") from None
    except OSError:
        raise GitWorkspaceError("helper_error") from None
    finally:
        selector.close()
        if process is not None:
            try:
                _terminate_group(process.pid, process, grace_seconds=0.2)
            finally:
                process.stdout.close()
                process.stderr.close()


def _record_workspace(store, run_id, task_id, attempt_id, workspace):
    def record(state):
        task = next((item for item in state["tasks"] if item["id"] == task_id), None)
        attempt = next((item for item in (task or {}).get("attempts", []) if item["id"] == attempt_id), None)
        if attempt is None:
            raise ValueError("Workspace preparation requires a recorded attempt")
        attempt["workspace"] = dict(workspace)
    store.update(run_id, record)


def prepare(store, run_id, task, attempt_id, project):
    project = Path(project).resolve()
    mode = task.get("workspace", "shared")
    if mode != "worktree":
        return {"mode": mode, "path": str(project), "owned": False,
                "isolation": "declared_read_only" if mode == "read-only" else "shared_checkout",
                "sandboxed": False}
    source_root = Path(_git(project, "rev-parse", "--show-toplevel")).resolve()
    relative = project.relative_to(source_root)
    base = _git(project, "rev-parse", "HEAD")
    dirty = bool(_git(source_root, "status", "--porcelain"))
    parent = store.directory(run_id) / "worktrees"
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.is_symlink():
        raise ValueError("Worktree directory must not be a symbolic link")
    root = parent / (task["id"] + "-" + attempt_id)
    if root.exists() or root.is_symlink():
        raise ValueError("Attempt worktree already exists; it will not be reused")
    cwd = root / relative
    workspace = {"mode": mode, "path": str(cwd), "root": str(root), "source_root": str(source_root),
                 "base_commit": base, "source_dirty": dirty, "owned": True, "retained": False,
                 "preparation_status": "preparing", "path_exists": False,
                 "isolation": "git_worktree", "sandboxed": False,
                 "snapshot": "committed_HEAD_only; uncommitted source changes are not copied"}
    # Git can create the checkout before a post-checkout hook fails or times
    # out. Persist its owned location before the first effect so recovery can
    # discover and retain it even when prepare never returns to the runner.
    _record_workspace(store, run_id, task["id"], attempt_id, workspace)
    try:
        _git(source_root, "worktree", "add", "--detach", str(root), base)
        if not cwd.is_dir():
            workspace["preparation_failure"] = "committed_project_directory_missing"
            raise ValueError("Project directory is absent from committed worktree snapshot")
        workspace.update(preparation_status="prepared", retained=True, path_exists=True)
        _record_workspace(store, run_id, task["id"], attempt_id, workspace)
        return workspace
    except BaseException as exc:
        failure = "git_helper_" + exc.reason if isinstance(exc, GitWorkspaceError) else "preparation_interrupted"
        workspace.update(preparation_status="failed", retained=os.path.lexists(root), path_exists=cwd.is_dir())
        workspace.setdefault("preparation_failure", failure)
        _record_workspace(store, run_id, task["id"], attempt_id, workspace)
        raise


def workspace_view(state):
    rows = []
    for task in state["tasks"]:
        for attempt in task.get("attempts", []):
            workspace = attempt.get("workspace")
            if workspace:
                rows.append({"task_id": task["id"], "attempt_id": attempt["id"],
                             "status": attempt.get("status"), **workspace})
    return {"run_id": state["id"], "workspaces": rows,
            "policy": "Worktrees are retained. No automatic merge, cleanup or effect replay."}


def remove_clean(store, run_id, task_id, attempt_id):
    # Same controller lock as recovery/retry: never remove a live run's checkout.
    from .runner import _controller_lock, _check_stale_children
    with _controller_lock(store, run_id):
        state = store.read(run_id)
        if state.get("controller") or any(task["status"] == "running" for task in state["tasks"]):
            raise RuntimeError("Cannot remove a workspace while execution is recorded active")
        _check_stale_children(state)
        task = next((task for task in state["tasks"] if task["id"] == task_id), None)
        attempt = next((a for a in (task or {}).get("attempts", []) if a["id"] == attempt_id), None)
        workspace = (attempt or {}).get("workspace") or {}
        if not workspace.get("owned") or workspace.get("mode") != "worktree" or not workspace.get("retained"):
            raise ValueError("No retained Workbench-owned worktree matches this attempt")
        if workspace.get("preparation_status", "prepared") != "prepared":
            raise ValueError("Workspace preparation did not complete; inspect the retained checkout before cleanup")
        root = Path(workspace["root"])
        expected = store.directory(run_id) / "worktrees" / (task_id + "-" + attempt_id)
        if (root != expected or root.parent.is_symlink() or root.is_symlink()
                or not root.is_dir() or root.resolve() != expected):
            raise ValueError("Workspace ownership path does not match")
        if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
            raise ValueError("Workspace is not its recorded Git checkout root")
        # A state path alone does not prove this checkout still belongs to the
        # source repository. Verify Git's common directory before removing it.
        source = Path(workspace["source_root"]).resolve()
        common = Path(_git(root, "rev-parse", "--git-common-dir"))
        source_common = Path(_git(source, "rev-parse", "--git-common-dir"))
        common = common if common.is_absolute() else root / common
        source_common = source_common if source_common.is_absolute() else source / source_common
        if common.resolve() != source_common.resolve():
            raise ValueError("Workspace Git ownership does not match its source repository")
        # Refuse both uncommitted work and detached commits, which could become
        # unreachable after removal. Deliberate integration/cleanup is separate.
        if _git(root, "status", "--porcelain") or _git(root, "rev-parse", "HEAD") != workspace["base_commit"]:
            raise ValueError("Workspace has changes or new commits; preserve or integrate them first")
        _git(workspace["source_root"], "worktree", "remove", str(root))
        def removed(current):
            from .store import add_event, now
            target = next(t for t in current["tasks"] if t["id"] == task_id)
            entry = next(a for a in target["attempts"] if a["id"] == attempt_id)
            entry["workspace"].update(retained=False, removed_at=now())
            add_event(current, "workspace_removed", task_id=task_id, attempt_id=attempt_id)
        return store.update(run_id, removed)
