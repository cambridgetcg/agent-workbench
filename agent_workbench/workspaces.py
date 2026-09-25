"""Declared checkout ownership and retained, per-attempt Git worktrees.

Leases coordinate Workbench controllers sharing a state root. They are not an
OS sandbox and do not constrain unrelated editors or native agent tool calls.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import threading
import time
import uuid


GIT_TIMEOUT_SECONDS = 30
GIT_OUTPUT_LIMIT = 1_048_576
_CLAIM_LIMIT = 256
_CLAIM_BYTES = 1_048_576
_KEY = re.compile(r"[a-f0-9]{64}\Z")
_TOKEN = re.compile(r"[a-f0-9]{32}\Z")
_CURRENT_LEASE = threading.local()
_SPAWN_GATE = """import os,sys
fd=int(sys.argv[1])
try:
    ready=os.read(fd,1)
finally:
    os.close(fd)
if ready != b'1':
    sys.exit(125)
try:
    os.execvpe(sys.argv[2],sys.argv[2:],os.environ)
except OSError:
    sys.stderr.write('Workbench executable launch failed after ownership registration\\n')
    sys.exit(126)
"""


class WorkspaceBusy(RuntimeError):
    pass


class GitWorkspaceError(ValueError):
    """Content-free helper failure; output may contain hooks or credentials."""

    def __init__(self, reason, exit_code=None):
        super().__init__("Git workspace operation failed; inspect the project locally")
        self.reason = reason
        self.exit_code = exit_code


def current_lease():
    """Current worker's checkout owner, for bounded helper launch/cleanup."""
    return getattr(_CURRENT_LEASE, "value", None)


def _lock_directory(store):
    locks = store.root / "checkout-locks"
    if locks.is_symlink():
        raise ValueError("Checkout lock directory must not be a symbolic link")
    locks.mkdir(mode=0o700, exist_ok=True)
    locks.chmod(0o700)
    return locks


def _checkout(project):
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
    return checkout


@contextmanager
def _claim_lock(locks, key):
    descriptor = os.open(locks / (key + ".claims.lock"), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _claims(locks, key, checkout=None):
    path = locks / (key + ".claims.json")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return {"version": 1, "checkout": str(checkout) if checkout is not None else None, "claims": []}
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Checkout ownership ledger is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(_CLAIM_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw) if len(raw) <= _CLAIM_BYTES else None
    except (ValueError, UnicodeError):
        value = None
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("claims"), list) or len(value["claims"]) > _CLAIM_LIMIT
            or any(not isinstance(claim, dict) or not isinstance(claim.get("id"), str)
                   or not _TOKEN.fullmatch(claim["id"]) or not isinstance(claim.get("processes"), list)
                   or len(claim["processes"]) > 64 for claim in value["claims"])):
        raise ValueError("Checkout ownership ledger is invalid; inspect it before proceeding")
    if checkout is not None and value.get("checkout") != str(checkout):
        raise ValueError("Checkout ownership ledger path does not match")
    return value


def _save_claims(locks, key, ledger):
    from .store import write_private_json
    if len(json.dumps(ledger, ensure_ascii=True).encode("utf-8")) > _CLAIM_BYTES:
        raise WorkspaceBusy("Checkout ownership ledger size limit reached")
    write_private_json(locks / (key + ".claims.json"), ledger)


def _group_running(group):
    from .runner import _group_exists, _group_has_running_members
    return _group_exists(group) and _group_has_running_members(group)


def _claim_controller_live(store, claim):
    from .runner import _process_identity
    pid = claim.get("controller_pid")
    if type(pid) is not int or not claim.get("controller_identity") or _process_identity(pid) != claim["controller_identity"]:
        return False
    try:
        path = store.directory(claim.get("run_id")) / "controller.lock"
        descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, ValueError):
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(descriptor)


class CheckoutLease:
    """Durable ownership tied to a run; spawn registers a gated process first."""

    def __init__(self, store, checkout=None, key=None, *, run_id=None, task_id=None, mode="shared"):
        self.store, self.checkout, self.key = store, checkout, key
        self.run_id, self.task_id, self.mode = run_id, task_id, mode
        self.claim_id = uuid.uuid4().hex if run_id is not None and checkout is not None else None
        self.attempt_id = None

    def _change(self, callback):
        if self.claim_id is None:
            return
        locks = _lock_directory(self.store)
        with _claim_lock(locks, self.key):
            ledger = _claims(locks, self.key, self.checkout)
            claim = next((row for row in ledger["claims"] if row["id"] == self.claim_id), None)
            if claim is None:
                raise WorkspaceBusy("Checkout ownership claim is no longer present")
            callback(claim)
            _save_claims(locks, self.key, ledger)

    def attach_attempt(self, attempt_id):
        self.attempt_id = attempt_id
        self._change(lambda claim: claim.update(attempt_id=attempt_id))
        if self.claim_id is not None:
            def attach(state):
                for item in state.get("checkout_claims", []):
                    if item["claim_id"] == self.claim_id:
                        item["attempt_id"] = attempt_id
            self.store.update(self.run_id, attach)

    def spawn(self, args, **kwargs):
        """Start native work only after its owned PGID is durable.

        The broker inherits native stdin/stdout/stderr unchanged. Parent death
        before ledger registration closes the private gate and the broker exits
        without executing the task. After release, exec preserves PID/PGID.
        """
        if self.claim_id is None:
            return subprocess.Popen(args, **kwargs)
        if not isinstance(args, (list, tuple)) or not args or kwargs.get("shell"):
            raise ValueError("Checkout-owned launches require an argv array")
        if kwargs.get("start_new_session") is not True or kwargs.get("close_fds", True) is not True:
            raise ValueError("Checkout-owned launches require a private process group")
        from .runner import _process_identity, _terminate_group
        read_gate, write_gate = os.pipe()
        process = None
        try:
            options = dict(kwargs)
            options["pass_fds"] = (*options.get("pass_fds", ()), read_gate)
            process = subprocess.Popen([sys.executable, "-I", "-S", "-c", _SPAWN_GATE, str(read_gate), *args], **options)
            os.close(read_gate)
            read_gate = None
            identity = _process_identity(process.pid)
            if identity is None:
                raise WorkspaceBusy("Could not verify the checkout-owned launch identity")
            record = {"pid": process.pid, "process_group": process.pid, "process_identity": identity}
            def register(claim):
                if len(claim["processes"]) >= 64:
                    raise WorkspaceBusy("Checkout process ownership limit reached")
                claim["processes"].append(record)
            self._change(register)
            os.write(write_gate, b"1")
            return process
        except BaseException:
            if process is not None:
                _terminate_group(process.pid, process, grace_seconds=0.2)
                self.release_process(process.pid)
            raise
        finally:
            if read_gate is not None:
                os.close(read_gate)
            os.close(write_gate)

    def release_process(self, pid):
        if self.claim_id is not None and not _group_running(pid):
            self._change(lambda claim: claim.update(processes=[row for row in claim["processes"] if row["pid"] != pid]))

    def release(self):
        if self.claim_id is None:
            return
        locks = _lock_directory(self.store)
        cleared = False
        with _claim_lock(locks, self.key):
            ledger = _claims(locks, self.key, self.checkout)
            claim = next((row for row in ledger["claims"] if row["id"] == self.claim_id), None)
            if claim is None:
                cleared = True
            elif any(_group_running(row.get("process_group")) for row in claim["processes"]):
                claim["status"] = "unresolved"
                _save_claims(locks, self.key, ledger)
            else:
                ledger["claims"] = [row for row in ledger["claims"] if row["id"] != self.claim_id]
                _save_claims(locks, self.key, ledger)
                cleared = True
        if cleared:
            self.store.update(self.run_id, lambda state: state.update(checkout_claims=[
                row for row in state.get("checkout_claims", []) if row["claim_id"] != self.claim_id]))


@contextmanager
def lease(store, project, mode, *, run_id=None, task_id=None, checkout_root=False):
    if mode == "worktree":
        # The runner acquires a real lease for the newly prepared checkout.
        yield CheckoutLease(store, run_id=run_id, task_id=task_id, mode=mode)
        return
    locks = _lock_directory(store)
    checkout = Path(project).resolve() if checkout_root else _checkout(project)
    key = hashlib.sha256(os.fsencode(checkout)).hexdigest()
    descriptor = os.open(locks / key, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    handle = CheckoutLease(store, checkout, key, run_id=run_id, task_id=task_id, mode=mode)
    previous = getattr(_CURRENT_LEASE, "value", None)
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_SH if mode == "read-only" else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceBusy("Another Workbench task owns this checkout") from exc
        with _claim_lock(locks, key):
            ledger = _claims(locks, key, checkout)
            for claim in ledger["claims"]:
                reader = mode == claim.get("mode") == "read-only"
                if not reader or claim.get("status") != "active" or not _claim_controller_live(store, claim):
                    raise WorkspaceBusy("This checkout has unresolved ownership; recover the owning run before reusing it")
            if handle.claim_id is not None:
                if len(ledger["claims"]) >= _CLAIM_LIMIT:
                    raise WorkspaceBusy("Checkout ownership limit reached")
                from .runner import _process_identity
                from .store import now
                index = {"checkout_key": key, "claim_id": handle.claim_id, "task_id": task_id,
                         "attempt_id": None, "checkout": str(checkout)}
                def indexed(state):
                    rows = state.setdefault("checkout_claims", [])
                    if len(rows) >= _CLAIM_LIMIT:
                        raise WorkspaceBusy("Run checkout ownership limit reached")
                    rows.append(index)
                store.update(run_id, indexed)
                ledger["claims"].append({"id": handle.claim_id, "run_id": run_id, "task_id": task_id,
                    "attempt_id": None, "mode": mode, "status": "active", "created_at": now(),
                    "controller_pid": os.getpid(), "controller_identity": _process_identity(os.getpid()), "processes": []})
                _save_claims(locks, key, ledger)
        _CURRENT_LEASE.value = handle
        yield handle
    finally:
        _CURRENT_LEASE.value = previous
        try:
            # No task effect may have started until a durable claim exists.
            if handle.claim_id is not None:
                handle.release()
        finally:
            os.close(descriptor)


def reconcile_claims(store, run_id, *, terminate=False):
    """Explicit recovery under this run's controller lock; never replay work.

    Only this run's indexed ledgers are visited. Surviving groups block removal
    unless recovery explicitly permits termination and their leader identity
    matches. An absent leader with surviving descendants remains unresolved.
    """
    from .runner import _process_identity, _terminate_group
    state = store.read(run_id)
    indices = state.get("checkout_claims", [])
    if not isinstance(indices, list) or len(indices) > _CLAIM_LIMIT:
        raise ValueError("Run checkout ownership index is invalid")
    locks = _lock_directory(store)
    cleared = set()
    for index in indices:
        if (not isinstance(index, dict) or not isinstance(index.get("checkout_key"), str)
                or not _KEY.fullmatch(index["checkout_key"]) or not isinstance(index.get("claim_id"), str)
                or not _TOKEN.fullmatch(index["claim_id"])):
            raise ValueError("Run checkout ownership index is invalid")
        key, token = index["checkout_key"], index["claim_id"]
        with _claim_lock(locks, key):
            ledger = _claims(locks, key)
            claim = next((row for row in ledger["claims"] if row["id"] == token), None)
            if claim is not None:
                if claim.get("run_id") != run_id:
                    raise ValueError("Checkout ownership index names another run")
                task = next((row for row in state["tasks"] if row["id"] == claim.get("task_id")), {})
                grace = 12 if task.get("harness") in {"astra", "astra-ultra"} else 0.7
                for process in claim["processes"]:
                    group = process.get("process_group")
                    if not _group_running(group):
                        continue
                    identity = process.get("process_identity")
                    if not terminate or not identity or _process_identity(group) != identity:
                        raise WorkspaceBusy("Checkout ownership has surviving or unverified processes; reconcile them before reuse")
                    _terminate_group(group, grace_seconds=grace)
                    if _group_running(group):
                        raise WorkspaceBusy("Checkout process cleanup remains unresolved")
                ledger["claims"] = [row for row in ledger["claims"] if row["id"] != token]
                _save_claims(locks, key, ledger)
            cleared.add(token)
    return store.update(run_id, lambda current: current.update(checkout_claims=[
        row for row in current.get("checkout_claims", []) if row["claim_id"] not in cleared]))


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
    owner = current_lease()
    try:
        spawn = owner.spawn if owner is not None else subprocess.Popen
        process = spawn(["git", "-C", str(project), *args], stdin=subprocess.DEVNULL,
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
                if owner is not None:
                    owner.release_process(process.pid)
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


def prepare(store, run_id, task, attempt_id, project, *, lease_stack=None):
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
    ownership_stack = lease_stack if lease_stack is not None else ExitStack()
    try:
        ownership = ownership_stack.enter_context(lease(store, root, "shared", run_id=run_id,
                                                       task_id=task["id"], checkout_root=True))
        ownership.attach_attempt(attempt_id)
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
    finally:
        if lease_stack is None:
            ownership_stack.close()


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
        # Another run can use a retained checkout as its project. The owner
        # run's controller lock does not protect against that foreign lease.
        with lease(store, root, "shared", run_id=run_id, task_id=task_id) as ownership:
            ownership.attach_attempt(attempt_id)
            if root.parent.is_symlink() or root.is_symlink() or root.resolve() != expected:
                raise ValueError("Workspace ownership path does not match")
            if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
                raise ValueError("Workspace is not its recorded Git checkout root")
            # A state path alone does not prove this checkout still belongs to
            # the source repository. Verify Git's common directory as well.
            source = Path(workspace["source_root"]).resolve()
            common = Path(_git(root, "rev-parse", "--git-common-dir"))
            source_common = Path(_git(source, "rev-parse", "--git-common-dir"))
            common = common if common.is_absolute() else root / common
            source_common = source_common if source_common.is_absolute() else source / source_common
            if common.resolve() != source_common.resolve():
                raise ValueError("Workspace Git ownership does not match its source repository")
            # Detached commits could become unreachable after removal.
            if _git(root, "status", "--porcelain") or _git(root, "rev-parse", "HEAD") != workspace["base_commit"]:
                raise ValueError("Workspace has changes or new commits; preserve or integrate them first")
            _git(workspace["source_root"], "worktree", "remove", str(root))
            def removed(current):
                from .store import add_event, now
                target = next(t for t in current["tasks"] if t["id"] == task_id)
                entry = next(a for a in target["attempts"] if a["id"] == attempt_id)
                entry["workspace"].update(retained=False, removed_at=now())
                add_event(current, "workspace_removed", task_id=task_id, attempt_id=attempt_id)
            store.update(run_id, removed)
        return store.read(run_id)
