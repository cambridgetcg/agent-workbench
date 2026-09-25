from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_workbench.runner import _group_has_running_members, _set_run_status, request_control
from agent_workbench.scheduler import schedule
from agent_workbench.store import Store
from agent_workbench.workspaces import lease, remove_clean


BARRIER_SCRIPT = r'''
import json, pathlib, sys, time
shared, identity, expected, delay, fail, mutate = sys.argv[1:]
shared = pathlib.Path(shared)
started = time.monotonic()
(shared / (identity + ".started")).write_text(str(started))
deadline = started + 5
while len(list(shared.glob("*.started"))) < int(expected):
    if time.monotonic() >= deadline:
        sys.exit(17)
    time.sleep(.01)
if mutate == "yes":
    pathlib.Path("result.txt").write_text(identity)
time.sleep(float(delay))
(shared / (identity + ".finished")).write_text(str(time.monotonic()))
sys.exit(int(fail))
'''


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.barrier = self.root / "barrier"
        self.barrier.mkdir()
        self.store = Store(self.root / "state")

    def task(self, ident, workspace="shared", *, expected=1, delay=.08, fail=0, mutate=False, depends_on=None):
        return {"id": ident, "harness": "command", "workspace": workspace,
                "depends_on": depends_on or [],
                "argv": [sys.executable, "-c", BARRIER_SCRIPT, str(self.barrier), ident,
                         str(expected), str(delay), str(fail), "yes" if mutate else "no"]}

    def create(self, tasks):
        return self.store.create({"version": 1, "name": "scheduler test", "goal": "synthetic commands", "tasks": tasks}, self.project)["id"]

    def run_schedule(self, run_id, *, jobs=2, max_launches=20, timeout=8, interrupted=None):
        interrupted = interrupted if interrupted is not None else {"signal": None}
        from agent_workbench.runner import _controller_lock
        # schedule's caller owns the controller lock; durable read leases use
        # that evidence when deciding whether concurrent readers are live.
        with _controller_lock(self.store, run_id):
            self.store.update(run_id, lambda state: state.update(status="running"))
            status, reason = schedule(self.store, run_id, time.monotonic() + timeout, interrupted, max_launches, jobs)
            return _set_run_status(self.store, run_id, status, reason)

    def async_run(self, run_id, **kwargs):
        results = []
        errors = []
        def run():
            try:
                results.append(self.run_schedule(run_id, **kwargs))
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        def cleanup():
            if worker.is_alive():
                request_control(self.store, run_id, "stop")
                worker.join(10)
        self.addCleanup(cleanup)
        return worker, results, errors

    def wait_for(self, condition, seconds=5):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(condition())

    def times(self, ident):
        return tuple(float((self.barrier / (ident + suffix)).read_text()) for suffix in (".started", ".finished"))

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.project), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def git_project(self):
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic Test")
        self.git("config", "user.email", "synthetic@example.invalid")
        (self.project / "tracked.txt").write_text("committed snapshot\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "test base")

    def test_readers_overlap_but_shared_writers_serialize(self):
        run_id = self.create([self.task("a", "read-only", expected=2), self.task("b", "read-only", expected=2), self.task("writer")])
        state = self.run_schedule(run_id)
        self.assertEqual(state["status"], "succeeded")
        a, b, writer = self.times("a"), self.times("b"), self.times("writer")
        self.assertLess(max(a[0], b[0]), min(a[1], b[1]))
        self.assertGreaterEqual(writer[0], max(a[1], b[1]))

    def test_default_shared_tasks_never_overlap_at_high_job_count(self):
        first, second = self.task("a"), self.task("b")
        first.pop("workspace")
        second.pop("workspace")
        run_id = self.create([first, second])
        self.assertEqual(self.run_schedule(run_id, jobs=16)["status"], "succeeded")
        self.assertGreaterEqual(self.times("b")[0], self.times("a")[1])

    def test_jobs_one_preserves_manifest_order(self):
        run_id = self.create([self.task("b", "read-only"), self.task("a", "read-only")])
        self.assertEqual(self.run_schedule(run_id, jobs=1)["status"], "succeeded")
        self.assertGreaterEqual(self.times("a")[0], self.times("b")[1])

    def test_requested_job_bound_limits_overlapping_commands(self):
        run_id = self.create([self.task(str(index), "read-only", expected=2, delay=.12) for index in range(6)])
        self.assertEqual(self.run_schedule(run_id, jobs=2)["status"], "succeeded")
        points = []
        for index in range(6):
            start, end = self.times(str(index))
            points.extend(((start, 1), (end, -1)))
        running = peak = 0
        for _, change in sorted(points):
            running += change
            peak = max(peak, running)
        self.assertEqual(peak, 2)

    def test_dependency_join_starts_only_after_both_prerequisites(self):
        run_id = self.create([self.task("a", "read-only", expected=2), self.task("b", "read-only", expected=2, delay=.15),
                              self.task("join", depends_on=["a", "b"])])
        self.assertEqual(self.run_schedule(run_id)["status"], "succeeded")
        self.assertGreaterEqual(self.times("join")[0], max(self.times("a")[1], self.times("b")[1]))

    def test_worktrees_overlap_and_keep_separate_results(self):
        self.git_project()
        run_id = self.create([self.task("a", "worktree", expected=2, mutate=True), self.task("b", "worktree", expected=2, mutate=True)])
        state = self.run_schedule(run_id)
        self.assertEqual(state["status"], "succeeded")
        self.assertLess(max(self.times("a")[0], self.times("b")[0]), min(self.times("a")[1], self.times("b")[1]))
        roots = []
        for task in state["tasks"]:
            workspace = task["attempts"][0]["workspace"]
            roots.append(workspace["root"])
            self.assertTrue(workspace["owned"])
            self.assertEqual((Path(workspace["path"]) / "result.txt").read_text(), task["id"])
            with self.assertRaisesRegex(ValueError, "changes or new commits"):
                remove_clean(self.store, run_id, task["id"], task["attempts"][0]["id"])
        self.assertNotEqual(*roots)
        self.assertFalse((self.project / "result.txt").exists())

    def test_clean_worktree_cleanup_only_removes_owned_attempt(self):
        self.git_project()
        run_id = self.create([self.task("clean", "worktree")])
        state = self.run_schedule(run_id)
        task = state["tasks"][0]
        attempt = task["attempts"][0]
        root = Path(attempt["workspace"]["root"])
        self.assertTrue(root.exists())
        with self.assertRaises(ValueError):
            remove_clean(self.store, run_id, "missing", attempt["id"])
        self.assertTrue(root.exists())
        removed = remove_clean(self.store, run_id, task["id"], attempt["id"])
        self.assertFalse(root.exists())
        self.assertFalse(removed["tasks"][0]["attempts"][0]["workspace"]["retained"])
        self.assertTrue((self.project / "tracked.txt").exists())

    def test_failure_stops_peer_and_prevents_dependent_and_later_launches(self):
        run_id = self.create([self.task("bad", "read-only", expected=2, fail=7), self.task("peer", "read-only", expected=2, delay=5),
                              self.task("dependent", depends_on=["bad"]), self.task("later", "read-only")])
        started = time.monotonic()
        state = self.run_schedule(run_id)
        self.assertEqual(state["status"], "failed")
        self.assertLess(time.monotonic() - started, 3)
        tasks = {task["id"]: task for task in state["tasks"]}
        self.assertEqual(tasks["bad"]["status"], "failed")
        self.assertEqual(tasks["peer"]["status"], "interrupted")
        self.assertEqual(tasks["peer"]["result"]["failure_kind"], "peer_failed")
        self.assertEqual(tasks["dependent"]["status"], "pending")
        self.assertEqual(tasks["later"]["status"], "pending")
        self.assertFalse(tasks["later"]["attempts"])

    def test_controller_exception_cancels_workers_before_executor_join(self):
        from agent_workbench.runner import _execute
        run_id = self.create([self.task("peer", "read-only", delay=5), self.task("boom", "read-only"), self.task("later")])
        def execute(store, ident, task_id, deadline, interrupted):
            if task_id != "boom":
                return _execute(store, ident, task_id, deadline, interrupted)
            self.wait_for(lambda: (self.barrier / "peer.started").exists())
            raise RuntimeError("synthetic controller failure")
        started = time.monotonic()
        with patch("agent_workbench.runner._execute", execute), self.assertRaisesRegex(RuntimeError, "synthetic controller failure"):
            self.run_schedule(run_id)
        self.assertLess(time.monotonic() - started, 3)
        tasks = self.store.read(run_id)["tasks"]
        self.assertEqual(tasks[0]["status"], "interrupted")
        self.assertEqual(tasks[0]["result"]["failure_kind"], "controller_error")
        self.assertFalse(tasks[2]["attempts"])

    def test_pause_drains_active_then_keeps_remaining_tasks_pending(self):
        run_id = self.create([self.task("a", "read-only", expected=2, delay=.3), self.task("b", "read-only", expected=2, delay=.3), self.task("later")])
        worker, results, errors = self.async_run(run_id)
        self.wait_for(lambda: len(list(self.barrier.glob("*.started"))) == 2)
        request_control(self.store, run_id, "pause")
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(results[0]["status"], "paused")
        self.assertEqual([task["status"] for task in results[0]["tasks"]], ["succeeded", "succeeded", "pending"])

    def test_launch_limit_drains_started_tasks(self):
        run_id = self.create([self.task("a", "read-only", expected=2), self.task("b", "read-only", expected=2), self.task("later", "read-only")])
        state = self.run_schedule(run_id, max_launches=2, jobs=3)
        self.assertEqual(state["status"], "paused")
        self.assertEqual(state["events"][-1]["reason"], "launch_limit")
        self.assertEqual([task["status"] for task in state["tasks"]], ["succeeded", "succeeded", "pending"])

    def test_workspace_contention_pauses_without_attempt_or_retry(self):
        run_id = self.create([self.task("blocked")])
        with lease(self.store, self.project, "shared"):
            state = self.run_schedule(run_id)
        self.assertEqual(state["status"], "paused")
        self.assertEqual(state["events"][-1]["reason"], "workspace_busy")
        self.assertEqual(state["tasks"][0]["status"], "pending")
        self.assertFalse(state["tasks"][0]["attempts"])

    def test_contention_drains_already_running_isolated_work(self):
        self.git_project()
        run_id = self.create([self.task("isolated", "worktree", delay=.2), self.task("blocked"), self.task("later", "worktree")])
        with lease(self.store, self.project, "shared"):
            state = self.run_schedule(run_id)
        self.assertEqual(state["status"], "paused")
        self.assertEqual(state["events"][-1]["reason"], "workspace_busy")
        self.assertEqual([task["status"] for task in state["tasks"]], ["succeeded", "pending", "pending"])
        self.assertFalse(state["tasks"][1]["attempts"])
        self.assertFalse(state["tasks"][2]["attempts"])

    def test_failed_tasks_are_never_retried_implicitly(self):
        run_id = self.create([self.task("bad", fail=4), self.task("later")])
        first = self.run_schedule(run_id)
        second = self.run_schedule(run_id)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "failed")
        self.assertEqual(len(second["tasks"][0]["attempts"]), 1)
        self.assertFalse(second["tasks"][1]["attempts"])

    def test_stop_terminates_multiple_owned_groups(self):
        spawn = r'''
import pathlib, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(30)
'''
        tasks = [{"id": ident, "harness": "command", "workspace": "read-only",
                  "argv": [sys.executable, "-c", spawn, str(self.barrier / (ident + ".pid"))]} for ident in ("a", "b")]
        run_id = self.create(tasks)
        worker, results, errors = self.async_run(run_id)
        self.wait_for(lambda: len(list(self.barrier.glob("*.pid"))) == 2)
        request_control(self.store, run_id, "stop")
        worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(results[0]["status"], "stopped")
        for task in results[0]["tasks"]:
            self.assertEqual(task["status"], "cancelled")
            self.assertFalse(_group_has_running_members(task["attempts"][0]["process_group"]))

    def test_signal_terminates_active_without_starting_remaining(self):
        interrupted = {"signal": None}
        run_id = self.create([self.task("a", "read-only", expected=2, delay=5), self.task("b", "read-only", expected=2, delay=5), self.task("later")])
        worker, results, errors = self.async_run(run_id, interrupted=interrupted)
        self.wait_for(lambda: len(list(self.barrier.glob("*.started"))) == 2)
        interrupted["signal"] = signal.SIGTERM
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(results[0]["status"], "interrupted")
        self.assertEqual(results[0]["tasks"][-1]["status"], "pending")

    def test_invocation_deadline_bounds_active_workers(self):
        run_id = self.create([self.task("a", "read-only", expected=2, delay=5), self.task("b", "read-only", expected=2, delay=5)])
        state = self.run_schedule(run_id, timeout=.35)
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["events"][-1]["reason"], "invocation_timeout")
        self.assertTrue(all(task["status"] == "interrupted" for task in state["tasks"]))

    def test_jobs_validation(self):
        run_id = self.create([self.task("a")])
        for jobs in (0, 17, True, 1.5, "2"):
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                schedule(self.store, run_id, time.monotonic() + 1, {"signal": None}, 20, jobs)
        self.assertFalse(self.store.read(run_id)["tasks"][0]["attempts"])


if __name__ == "__main__":
    unittest.main()
