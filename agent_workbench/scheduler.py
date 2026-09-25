"""Bounded dependency scheduling with declared checkout ownership.

The caller owns the controller lock, signal handlers and final run-state update.
Each runner worker owns its attempt, checkout lease and process-group cleanup.
No task is retried here. Cancellation stops peers without inventing an OS signal.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import math
import time

from .workspaces import WorkspaceBusy, compatible


POLL_SECONDS = 0.05


def _outcome(status):
    return {"cancelled": "stopped", "interrupted": "interrupted"}.get(status, "failed"), status


def schedule(store, run_id, deadline, interrupted, max_launches, jobs):
    """Run ready tasks and return ``(run_status, reason)`` after workers exit.

    ``jobs`` is 1..16; one preserves declared serial ordering. Pending shared
    writers cannot overlap other shared readers/writers. Worktrees may overlap
    either, and declared read-only tasks may overlap one another. Leases in
    ``runner._execute`` also protect against other Workbench controllers.

    Pause and launch limits drain existing workers. Task failure sets
    ``interrupted['cancel']='peer_failed'``; unexpected controller failures use
    ``controller_error``. Runner boundaries must honor this separate flag and
    terminate owned process groups. Lease contention drains existing work and
    pauses without retrying the blocked task or creating an attempt for it.
    """
    if type(jobs) is not int or not 1 <= jobs <= 16:
        raise ValueError("jobs must be an integer between 1 and 16")
    if type(max_launches) is not int or max_launches < 1:
        raise ValueError("max_launches must be a positive integer")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise ValueError("deadline must be finite")
    if not isinstance(interrupted, dict) or "signal" not in interrupted:
        raise ValueError("interrupted must contain the signal state")
    # Runtime import avoids the runner -> scheduler -> runner import cycle.
    from .runner import _ControlBoundary, _execute

    active = {}
    launches = 0
    terminal = None
    draining = None
    with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="workbench-task") as pool:
        try:
            while True:
                # Reap before looking for more work: a completed failure must
                # prevent downstream and independent future launches alike.
                for future in list(active):
                    if not future.done():
                        continue
                    active.pop(future)
                    try:
                        outcome = future.result()
                    except WorkspaceBusy:
                        launches -= 1  # Lease acquisition precedes attempts.
                        draining = ("paused", "workspace_busy")
                    except _ControlBoundary:
                        launches -= 1
                        # Normally a durable stop/pause flag explains this;
                        # retain a boundary even if that flag changes later.
                        draining = draining or ("paused", "control_boundary")
                    else:
                        if outcome != "succeeded" and terminal is None:
                            terminal = _outcome(outcome)
                            interrupted.setdefault("cancel", "peer_failed")

                state = store.read(run_id)
                tasks = state["tasks"]
                control = state["control"]
                if terminal == ("interrupted", "interrupted") and any(
                    task["status"] == "interrupted"
                    and (task.get("result") or {}).get("failure_kind") == "invocation_timeout"
                    for task in tasks
                ):
                    # A worker can observe the same deadline a fraction before
                    # the scheduler. Keep the reason independent of that race.
                    terminal = ("interrupted", "invocation_timeout")
                if interrupted.get("signal") is not None:
                    terminal = ("interrupted", "signal")
                elif control.get("stop_requested"):
                    terminal = ("stopped", "stop_requested")
                elif terminal is None:
                    if not active and all(task["status"] == "succeeded" for task in tasks):
                        return "succeeded", None
                    failed = next((task for task in tasks if task["status"] in {"failed", "cancelled", "interrupted"}), None)
                    if failed:
                        terminal = _outcome(failed["status"])
                        interrupted.setdefault("cancel", "peer_failed")
                    elif interrupted.get("cancel"):
                        terminal = ("interrupted", interrupted["cancel"])
                    elif time.monotonic() >= deadline:
                        terminal = ("interrupted" if active else "paused", "invocation_timeout")
                        interrupted.setdefault("cancel", "invocation_timeout")

                if terminal:
                    if not active:
                        return terminal
                else:
                    if not active and all(task["status"] == "succeeded" for task in tasks):
                        return "succeeded", None
                    if control.get("pause_requested"):
                        draining = ("paused", "pause_requested")
                    elif launches >= max_launches:
                        draining = draining or ("paused", "launch_limit")
                    if draining and not active:
                        return draining
                    if not draining and len(active) < jobs:
                        succeeded = {task["id"] for task in tasks if task["status"] == "succeeded"}
                        active_tasks = list(active.values())
                        active_ids = {task["id"] for task in active_tasks}
                        ready = next((task for task in tasks
                                      if task["status"] == "pending" and task["id"] not in active_ids
                                      and set(task.get("depends_on", [])) <= succeeded
                                      and compatible(task, active_tasks)), None)
                        if ready:
                            future = pool.submit(_execute, store, run_id, ready["id"], deadline, interrupted)
                            active[future] = ready
                            launches += 1
                            # Re-read state and completed futures before each
                            # submission instead of filling a stale ready list.
                            continue
                        if not active:
                            return "failed", "no_ready_tasks"

                if active:
                    wait(active, timeout=POLL_SECONDS, return_when=FIRST_COMPLETED)
        except BaseException:
            # ThreadPoolExecutor joins workers on exit. Set cancellation first
            # so an exception does not leave their subprocesses running until
            # the invocation timeout. The original exception reaches the
            # runner's existing interrupted-state recovery handler afterward.
            interrupted.setdefault("cancel", "controller_error")
            raise
