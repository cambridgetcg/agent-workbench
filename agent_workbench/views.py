"""Read-only continuation and timeline views of selected local run metadata."""

from __future__ import annotations

import math

from .runner import redact_summary


_RETRYABLE = {"failed", "cancelled", "interrupted"}
_ATTEMPT_FIELDS = (
    "id", "status", "started_at", "finished_at", "elapsed_seconds", "exit_code",
    "failure_kind", "native_success", "native_session_id", "unresolved_process_group",
    "process_group", "process_identity",
    "duplex_initialized", "input_closed",
    "input_drained", "input_drain_evidence",
)
_EVENT_FIELDS = (
    "at", "kind", "task_id", "attempt_id", "status", "reason", "failure_kind",
    "note_kind", "applies_to", "timeout_seconds", "max_launches", "previous_attempts",
    "unresolved_process_groups", "source_run_id",
    "session_id", "native_task_id", "tool_use_id", "parent_tool_use_id", "item_id", "agent_id",
    "tool", "role", "terminal_reason", "request_id", "action", "code", "decision_type", "subtype",
    "input_tokens", "output_tokens", "total_cost_usd", "num_turns", "duration_ms", "duration_api_ms", "task_type",
    "jobs",
)


def _selected(source, fields):
    """Only scalar metadata is displayable; paths, logs and nested payloads are not."""
    result = {}
    for field in fields:
        value = source.get(field)
        if isinstance(value, str):
            result[field] = redact_summary(value, limit=512)
        elif value is None or type(value) in {bool, int}:
            if field in source:
                result[field] = value
        elif type(value) is float and math.isfinite(value):
            result[field] = value
    return result


def status_details(state: dict) -> dict:
    """Explain task waits and explicit next actions without checking or taking locks.

    ``tasks`` is keyed by task ID for merging into a caller's status rows. Native
    session IDs are local pointers only. Controller metadata is not proof that a
    process is alive; this view never declares a recorded controller stale.
    """
    tasks = state.get("tasks", [])
    statuses = {task["id"]: task["status"] for task in tasks}
    retryable = [task for task in tasks if task["status"] in _RETRYABLE]
    controller = state.get("controller") is not None
    active = any(task["status"] == "running" for task in tasks)
    run_status = state.get("status")
    unresolved_count = sum(bool(attempt.get("unresolved_process_group"))
                           for task in tasks for attempt in task.get("attempts", []))
    details = {}
    for task in tasks:
        status = task["status"]
        waiting = [dep for dep in task.get("depends_on", []) if statuses.get(dep) != "succeeded"]
        attempts = task.get("attempts", [])
        unresolved = [attempt for attempt in attempts if attempt.get("unresolved_process_group")]
        latest = _selected(attempts[-1], _ATTEMPT_FIELDS) if attempts else None
        if latest is not None:
            attempt = attempts[-1]
            launch = attempt.get("launch", {})
            latest["launch"] = {**_selected(launch, ("harness", "display_executable", "status")),
                                "version": _selected(launch.get("version", {}), ("status", "version", "subject"))}
            latest["workspace"] = _selected(attempt.get("workspace", {}), ("mode", "path", "base_commit", "retained", "sandboxed", "source_dirty"))
            latest["control_receipts"] = [_selected(r, ("at", "request_id", "action", "status")) for r in attempt.get("control_receipts", [])[-64:]]
        result = task.get("result") or {}
        if latest is not None and result.get("attempt_id") == latest.get("id"):
            if not latest.get("native_session_id") and result.get("native_session_id"):
                latest.update(_selected(result, ("native_session_id",)))
        if status == "pending":
            if waiting:
                reason = "Waiting for dependencies: " + ", ".join(
                    f"{dep} ({statuses.get(dep, 'missing')})" for dep in waiting
                ) + "."
            elif retryable:
                reason = "Dependencies are complete, but this run requires explicit retry of unsuccessful work."
            elif run_status in {"paused", "stopped"} and not controller:
                reason = "Ready; the run is " + run_status + ". An explicit run continues pending work."
            elif controller or active:
                reason = "Ready; waiting for controller capacity and checkout ownership."
            else:
                reason = "Ready for an explicit run."
        elif status in _RETRYABLE:
            reason = "An explicit retry is required before this task can run again; inspect effects already performed."
        elif status == "running":
            reason = "Execution is recorded as running; controller metadata alone does not establish liveness."
        else:
            reason = "Observed process success; continuation retains this task without repeating it."
        if unresolved:
            groups = ", ".join(str(attempt.get("process_group", "unknown")) for attempt in unresolved[-20:])
            reason += f" Recovery recorded {len(unresolved)} unresolved process group(s): {groups}. These are historical ownership warnings, not proof that the groups are still alive."
        if status in _RETRYABLE and unresolved_count:
            reason = "Inspect the unresolved process groups recorded in this run before retrying; retry checks every flagged group and refuses while any remains present. " + reason
        details[task["id"]] = {
            "status_reason": redact_summary(reason, limit=2400),
            "waiting_on": [redact_summary(dep, limit=64) for dep in waiting],
            "latest_attempt": latest,
            "unresolved_attempts": [_selected(attempt, _ATTEMPT_FIELDS) for attempt in unresolved],
            "unresolved_attempt_count": len(unresolved),
        }

    def action(name, reason, task_id=None):
        item = {"action": name, "reason": reason}
        if task_id is not None:
            item["task_id"] = task_id
        return item

    if controller or run_status == "running":
        actions = [action("inspect", "A controller is recorded. Check its activity before treating it as stale; recovery must first acquire the controller lock.")]
        if run_status == "running":
            actions = [
                action("wait", "Execution is recorded as running; inspect activity if progress has stopped."),
                action("pause", "Request a pause between tasks."),
                action("stop", "Request termination of the controller's owned active subprocess group."),
            ]
            for task in tasks:
                if task["status"] == "running" and task.get("adapter") == "duplex" and task.get("attempts"):
                    attempt = task["attempts"][-1]
                    if attempt.get("duplex_initialized") and not attempt.get("input_closed"):
                        actions.extend([
                            action("send", "Queue input for this active native session; inspect its receipt before assuming delivery.", task["id"]),
                            action("interrupt", "Request native turn interruption; acknowledgment is not proof effects have stopped.", task["id"]),
                        ])
    elif active:
        actions = [action("recover", "Inspect work first. Recovery checks that the controller lock is free and records interrupted attempts without rerunning them.")]
    elif unresolved_count and retryable:
        actions = [action("inspect", "Inspect recorded unresolved process groups and their ownership before retrying. Do not signal a group based only on its recorded ID. Retry checks group presence again; recovery does not reassess already-interrupted attempts.")]
    elif retryable:
        actions = [action("retry", "Inspect existing effects, then explicitly reset this unsuccessful task. Retry itself launches nothing.", task["id"]) for task in retryable]
    elif run_status == "succeeded":
        actions = []
    elif any(task["status"] == "pending" and not details[task["id"]]["waiting_on"] for task in tasks):
        actions = [action("run", "Continue pending tasks; completed tasks are retained. This explicitly launches native agents or commands.")]
    else:
        actions = [action("inspect", "No task is ready. Inspect the saved plan and dependency outcomes before continuing.")]
    return {"next_actions": actions, "tasks": details}


def timeline_view(state: dict, limit: int = 20) -> dict:
    """Return the latest 1–200 selected event/attempt entries, oldest first.

    Existing task events are enriched with their matching attempt metadata.
    Attempts missing events (such as a recovered interruption) still appear.
    No prompt, note text, raw output, log path or process environment is copied.
    """
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Timeline limit must be an integer between 1 and 200.")
    attempts = {
        (task["id"], attempt["id"]): attempt
        for task in state.get("tasks", []) for attempt in task.get("attempts", [])
    }
    entries = []
    represented = set()
    for event in state.get("events", []):
        entry = _selected(event, _EVENT_FIELDS)
        if isinstance(event.get("usage"), dict):
            entry["usage"] = {key: value for key, value in event["usage"].items()
                              if key in {"input_tokens", "output_tokens", "cached_input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "total_tokens"}
                              and type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1e15}
        key = (event.get("task_id"), event.get("attempt_id"))
        attempt = attempts.get(key)
        if attempt is not None and event.get("kind") in {"task_started", "task_finished"}:
            represented.add((key, event["kind"]))
            if event["kind"] == "task_finished":
                entry.update(_selected(attempt, tuple(field for field in _ATTEMPT_FIELDS if field not in {"id", "status"})))
        entries.append(entry)
    for key, attempt in attempts.items():
        for kind, time_field in (("task_started", "started_at"), ("task_finished", "finished_at")):
            if not attempt.get(time_field) or (key, kind) in represented:
                continue
            entry = _selected({"at": attempt[time_field], "kind": kind, "task_id": key[0], "attempt_id": key[1]}, _EVENT_FIELDS)
            if kind == "task_finished":
                entry.update(_selected(attempt, tuple(field for field in _ATTEMPT_FIELDS if field != "id")))
            entries.append(entry)
    entries.sort(key=lambda item: item.get("at") or "")
    return {
        **_selected(state, ("id", "status")),
        "entries": entries[-limit:],
        "total_entries": len(entries) + state.get("native_events_dropped", 0),
        "truncated": len(entries) > limit or bool(state.get("native_events_dropped")),
        "dropped_native_events": state.get("native_events_dropped", 0),
    }


def metadata_view(state):
    """Minimal plugin/UI contract: no goals, summaries, notes, paths or logs."""
    return {**_selected(state, ("id", "status", "revision")),
            "tasks": [{**_selected(task, ("id", "harness", "status", "adapter", "workspace")),
                       "attempts": len(task.get("attempts", []))} for task in state.get("tasks", [])],
            "queued_guidance_count": len(state.get("control", {}).get("guidance", [])),
            "native_request_count": len(state.get("control", {}).get("native_requests", []))}
