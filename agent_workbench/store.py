"""Private, atomic workflow snapshots shared by both harnesses."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Callable
import uuid


RUN_ID = re.compile(r"[a-f0-9]{32}\Z")
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
HARNESSES = {"codex", "claude", "astra", "astra-ultra", "hermes", "hermes-astra-ultra", "command"}
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra", "ultracode"}
MAX_DOCUMENT_BYTES = 2_000_000


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_event(state: dict, kind: str, **fields) -> None:
    state["events"].append({"at": now(), "kind": kind, **fields})


def _text(value, name: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or len(value) > limit:
        raise ValueError(f"Invalid {name}.")
    if not empty and not value.strip():
        raise ValueError(f"{name} must not be empty.")
    return value


def validate_manifest(manifest: dict) -> dict:
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError("Workflow manifest must have version 1.")
    if set(manifest) - {"version", "name", "goal", "tasks"}:
        raise ValueError("Workflow manifest contains unsupported fields.")
    name = _text(manifest.get("name"), "workflow name", 160)
    goal = _text(manifest.get("goal"), "goal", 20_000)
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 200:
        raise ValueError("Workflow must contain between 1 and 200 tasks.")
    result = {"version": 1, "name": name, "goal": goal, "tasks": []}
    seen = set()
    for task in tasks:
        if not isinstance(task, dict) or set(task) - {"id", "harness", "depends_on", "prompt", "argv", "model", "effort", "timeout_seconds", "adapter", "workspace"}:
            raise ValueError("Task contains unsupported fields.")
        ident = task.get("id")
        if not isinstance(ident, str) or not TASK_ID.fullmatch(ident) or ident in seen:
            raise ValueError("Task IDs must be unique letters, numbers, underscores or hyphens.")
        seen.add(ident)
        harness = task.get("harness")
        if not isinstance(harness, str) or harness not in HARNESSES:
            raise ValueError("Unknown task harness.")
        deps = task.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(x, str) or not TASK_ID.fullmatch(x) for x in deps) or len(set(deps)) != len(deps):
            raise ValueError("Invalid task dependencies.")
        out = {"id": ident, "harness": harness, "depends_on": list(deps)}
        if "adapter" in task:
            if task["adapter"] not in ("process", "duplex"):
                raise ValueError("Adapter must be process or duplex.")
            if task["adapter"] == "duplex" and harness not in {"claude", "astra", "astra-ultra"}:
                raise ValueError("Duplex control requires a Claude-compatible harness.")
            out["adapter"] = task["adapter"]
        if "workspace" in task:
            if task["workspace"] not in ("shared", "read-only", "worktree"):
                raise ValueError("Workspace must be shared, read-only, or worktree.")
            out["workspace"] = task["workspace"]
        if harness == "command":
            argv = task.get("argv")
            if "prompt" in task or "model" in task or "effort" in task:
                raise ValueError("Command tasks accept argv, not model settings or prompts.")
            if not isinstance(argv, list) or not 1 <= len(argv) <= 256:
                raise ValueError("Command task requires an argv array.")
            out["argv"] = [_text(a, "command argument", 16_000, empty=i > 0) for i, a in enumerate(argv)]
        else:
            if "argv" in task:
                raise ValueError("Agent tasks accept prompts, not argv.")
            out["prompt"] = _text(task.get("prompt"), "task prompt", 100_000)
            if "model" in task:
                out["model"] = _text(task["model"], "model", 200)
                if out["model"].startswith("-"):
                    raise ValueError("Invalid model.")
                if harness in {"astra", "astra-ultra", "hermes-astra-ultra"} and out["model"] != "gpt-6-astra":
                    raise ValueError("Astra launchers support gpt-6-astra only.")
            if "effort" in task:
                effort = task["effort"]
                if not isinstance(effort, str) or effort not in EFFORTS:
                    raise ValueError("Unknown effort.")
                if harness in {"codex", "hermes", "hermes-astra-ultra"} and effort == "ultracode":
                    raise ValueError("Ultracode is a Claude workflow setting.")
                if harness == "claude" and effort == "ultra":
                    raise ValueError("Native Claude does not accept ultra; use max or ultracode.")
                out["effort"] = effort
        if "timeout_seconds" in task:
            timeout = task["timeout_seconds"]
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 604800:
                raise ValueError("Task timeout must be positive and at most one week.")
            out["timeout_seconds"] = timeout
        result["tasks"].append(out)
    graph = {t["id"]: t["depends_on"] for t in result["tasks"]}
    visited, active = set(), set()

    def visit(ident):
        if ident in active:
            raise ValueError("Task dependencies contain a cycle.")
        if ident in visited:
            return
        active.add(ident)
        for dep in graph[ident]:
            if dep not in graph:
                raise ValueError("Task dependency does not exist.")
            visit(dep)
        active.remove(ident)
        visited.add(ident)

    for ident in graph:
        visit(ident)
    if len(json.dumps(result).encode()) > MAX_DOCUMENT_BYTES:
        raise ValueError("Workflow manifest is too large.")
    return result


def read_json(path: Path) -> dict:
    if path.stat().st_size > 20_000_000:
        raise ValueError("State document is too large.")
    return json.loads(path.read_text())


def write_private_json(path: Path, document: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(document, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or os.environ.get("AGENT_WORKBENCH_STATE_DIR", Path.home() / ".local/state/agent-workbench")).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.runs = self.root / "runs"
        if self.runs.is_symlink():
            raise ValueError("Run directory must not be a symbolic link.")
        self.runs.mkdir(exist_ok=True, mode=0o700)
        self.runs.chmod(0o700)

    def directory(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError("Invalid run ID.")
        path = self.runs / run_id
        if path.is_symlink():
            raise ValueError("Run directory must not be a symbolic link.")
        return path

    def create(self, manifest: dict, project: Path) -> dict:
        manifest = validate_manifest(manifest)
        project = Path(project).expanduser().resolve()
        if not project.is_dir():
            raise ValueError("Project must be an existing directory.")
        ident = uuid.uuid4().hex
        directory = self.directory(ident)
        directory.mkdir(mode=0o700)
        state = {**manifest, "id": ident, "project": str(project), "created_at": now(), "updated_at": now(), "revision": 0, "status": "ready", "events": [], "notes": [], "control": {"stop_requested": False, "pause_requested": False, "guidance": []}, "controller": None}
        for task in state["tasks"]:
            task.update(status="pending", attempts=[], result=None)
        add_event(state, "created")
        write_private_json(directory / "state.json", state)
        return state

    def read(self, run_id: str) -> dict:
        path = self.directory(run_id) / "state.json"
        if path.is_symlink():
            raise ValueError("State document must not be a symbolic link.")
        state = read_json(path)
        if not isinstance(state, dict) or state.get("id") != run_id or state.get("version") != 1:
            raise ValueError("Invalid run state.")
        return state

    def resolve_run_id(self, reference: str) -> str:
        """Resolve an explicit ID or unique prefix, never an implicit latest run."""
        if not isinstance(reference, str) or not re.fullmatch(r"[a-f0-9]{8,32}", reference):
            raise ValueError("Use a run ID or a unique prefix of at least 8 hexadecimal characters.")
        if RUN_ID.fullmatch(reference):
            self.read(reference)
            return reference
        matches = sorted(path.name for path in self.runs.iterdir()
                         if RUN_ID.fullmatch(path.name) and path.name.startswith(reference) and path.is_dir())
        if not matches:
            raise ValueError("No saved run matches that prefix. Use list to find a run.")
        if len(matches) != 1:
            raise ValueError("Run prefix is ambiguous; use more characters or the full run ID.")
        self.read(matches[0])
        return matches[0]

    def update(self, run_id: str, callback: Callable[[dict], None]) -> dict:
        directory = self.directory(run_id)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(directory / "state.lock", flags, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.read(run_id)
            callback(state)
            state["revision"] += 1
            state["updated_at"] = now()
            write_private_json(directory / "state.json", state)
        return state

    def list_runs(self) -> list[dict]:
        states = []
        for directory in self.runs.iterdir():
            if RUN_ID.fullmatch(directory.name) and directory.is_dir():
                states.append(self.read(directory.name))
        return sorted(states, key=lambda s: s["created_at"], reverse=True)
