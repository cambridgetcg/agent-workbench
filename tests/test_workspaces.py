import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_workbench import workspaces
from agent_workbench.store import Store


GIT_HELPER = r'''
import os, pathlib, subprocess, sys, time
mode = os.environ["WORKBENCH_GIT_FIXTURE"]
if mode in ("timeout", "parent-exit", "failure"):
    child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
    pathlib.Path(os.environ["WORKBENCH_GIT_CHILD"]).write_text(str(child.pid))
    time.sleep(0.08)
    if mode == "parent-exit":
        print("selected-metadata", flush=True)
        sys.exit(0)
    if mode == "failure":
        print("PRIVATE_HOOK_TOKEN sk-testprivatedata123", file=sys.stderr, flush=True)
        sys.exit(7)
    time.sleep(60)
elif mode in ("stdout-flood", "stderr-flood"):
    descriptor = 1 if mode == "stdout-flood" else 2
    while True:
        os.write(descriptor, b"PRIVATE_HELPER_BODY" * 4096)
else:
    print("selected-metadata", flush=True)
'''


class GitHelperSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        helper = self.bin / "git"
        helper.write_text(f"#!{sys.executable}\n" + GIT_HELPER)
        helper.chmod(0o700)
        self.child_file = self.root / "child.pid"
        self.environment = patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                                                   "WORKBENCH_GIT_CHILD": str(self.child_file)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.cleanup_child)

    def child_running(self):
        if not self.child_file.exists():
            return False
        pid = int(self.child_file.read_text())
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, check=False)
        return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")

    def cleanup_child(self):
        if self.child_running():
            os.kill(int(self.child_file.read_text()), signal.SIGKILL)

    def assert_child_stopped(self):
        deadline = time.monotonic() + 1
        while self.child_running() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.child_running(), "A Git helper descendant survived owned-group cleanup")

    def test_timeout_terminates_helper_and_descendant_group(self):
        os.environ["WORKBENCH_GIT_FIXTURE"] = "timeout"
        started = time.monotonic()
        with patch.object(workspaces, "GIT_TIMEOUT_SECONDS", 0.4), self.assertRaises(workspaces.GitWorkspaceError) as caught:
            workspaces._git(self.root, "status")
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(self.child_file.exists())
        self.assert_child_stopped()

    def test_successful_parent_does_not_leave_inherited_pipe_helper_running(self):
        os.environ["WORKBENCH_GIT_FIXTURE"] = "parent-exit"
        started = time.monotonic()
        self.assertEqual(workspaces._git(self.root, "status"), "selected-metadata")
        self.assertLess(time.monotonic() - started, 2)
        self.assert_child_stopped()

    def test_nonzero_exit_redacts_stderr_and_cleans_descendants(self):
        os.environ["WORKBENCH_GIT_FIXTURE"] = "failure"
        with self.assertRaises(workspaces.GitWorkspaceError) as caught:
            workspaces._git(self.root, "status")
        self.assertEqual(caught.exception.reason, "exit")
        self.assertEqual(caught.exception.exit_code, 7)
        self.assertNotIn("PRIVATE_HOOK_TOKEN", str(caught.exception))
        self.assertNotIn("sk-testprivatedata123", str(caught.exception))
        self.assert_child_stopped()

    def test_stdout_and_stderr_capture_are_bounded(self):
        for mode in ("stdout-flood", "stderr-flood"):
            with self.subTest(mode=mode):
                os.environ["WORKBENCH_GIT_FIXTURE"] = mode
                started = time.monotonic()
                with patch.object(workspaces, "GIT_OUTPUT_LIMIT", 65536), self.assertRaises(workspaces.GitWorkspaceError) as caught:
                    workspaces._git(self.root, "status")
                self.assertEqual(caught.exception.reason, "output_limit")
                self.assertNotIn("PRIVATE_HELPER_BODY", str(caught.exception))
                self.assertLess(time.monotonic() - started, 2)


class WorkspaceOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = Store(self.root / "state")

    def git(self, project, *args):
        return subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, check=True).stdout.strip()

    def repository(self, project):
        project.mkdir(exist_ok=True)
        self.git(project, "init", "-q")
        (project / "tracked.txt").write_text("fixture\n")
        self.git(project, "add", "tracked.txt")
        self.git(project, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")

    def owned(self):
        self.repository(self.project)
        task = {"id": "work", "harness": "command", "argv": [sys.executable, "-c", "pass"], "workspace": "worktree"}
        state = self.store.create({"version": 1, "name": "Ownership fixture", "goal": "Verify cleanup ownership", "tasks": [task]}, self.project)
        run_id = state["id"]
        attempt_id = "fixture-attempt"
        self.store.update(run_id, lambda current: current["tasks"][0].update(attempts=[{"id": attempt_id, "status": "running"}]))
        workspace = workspaces.prepare(self.store, run_id, task, attempt_id, self.project)
        def completed(current):
            current["status"] = "succeeded"
            current["tasks"][0].update(status="succeeded", attempts=[{"id": attempt_id, "status": "succeeded", "workspace": workspace}])
        self.store.update(run_id, completed)
        return run_id, attempt_id, Path(workspace["root"])

    def test_helper_failure_does_not_downgrade_checkout_lease(self):
        for reason in ("timeout", "output_limit", "helper_error"):
            with self.subTest(reason=reason), patch.object(workspaces, "_git", side_effect=workspaces.GitWorkspaceError(reason)):
                with self.assertRaises(workspaces.GitWorkspaceError):
                    with workspaces.lease(self.store, self.project, "shared"):
                        self.fail("Unverified repository resolution must not downgrade the lease")

    def test_nonrepository_fallback_does_not_hide_broken_git_metadata(self):
        outside = workspaces.GitWorkspaceError("exit", 128)
        with patch.object(workspaces, "_git", side_effect=outside):
            with workspaces.lease(self.store, self.project, "shared"):
                pass
            (self.project / ".git").write_text("invalid metadata")
            with self.assertRaises(workspaces.GitWorkspaceError):
                with workspaces.lease(self.store, self.project, "shared"):
                    self.fail("Broken repository metadata must not weaken the lease")

    def test_plain_command_does_not_require_git_on_path(self):
        from agent_workbench.runner import run_workflow
        task = {"id": "work", "harness": "command", "argv": [sys.executable, "-c", "print('fixture-complete')"]}
        run_id = self.store.create({"version": 1, "name": "No Git fixture", "goal": "Run a plain command", "tasks": [task]}, self.project)["id"]
        with patch.dict(os.environ, {"PATH": "", "GIT_DIR": "", "GIT_WORK_TREE": "", "GIT_COMMON_DIR": ""}):
            state = run_workflow(self.store, run_id, timeout_seconds=5)
        self.assertEqual(state["status"], "succeeded")

    def test_partial_prepare_retains_discoverable_checkout_evidence(self):
        self.repository(self.project)
        subdirectory = self.project / "uncommitted-subdirectory"
        subdirectory.mkdir()
        task = {"id": "work", "harness": "command", "argv": [sys.executable, "-c", "pass"], "workspace": "worktree"}
        run_id = self.store.create({"version": 1, "name": "Partial fixture", "goal": "Retain partial preparation", "tasks": [task]}, subdirectory)["id"]
        attempt_id = "partial-attempt"
        self.store.update(run_id, lambda state: state["tasks"][0].update(attempts=[{"id": attempt_id, "status": "failed"}]))
        with self.assertRaisesRegex(ValueError, "absent from committed"):
            workspaces.prepare(self.store, run_id, task, attempt_id, subdirectory)
        state = self.store.read(run_id)
        workspace = state["tasks"][0]["attempts"][0]["workspace"]
        self.assertEqual(workspace["preparation_status"], "failed")
        self.assertEqual(workspace["preparation_failure"], "committed_project_directory_missing")
        self.assertTrue(workspace["retained"])
        self.assertTrue(Path(workspace["root"]).is_dir())
        self.assertFalse(workspace["path_exists"])
        self.assertEqual(workspaces.workspace_view(state)["workspaces"][0]["root"], workspace["root"])
        with self.assertRaisesRegex(ValueError, "preparation did not complete"):
            workspaces.remove_clean(self.store, run_id, "work", attempt_id)

    def test_cleanup_rejects_symlinked_worktree_parent(self):
        run_id, attempt_id, checkout = self.owned()
        parent = checkout.parent
        retained = parent.with_name("retained-worktrees")
        parent.rename(retained)
        parent.symlink_to(retained, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "ownership path"):
            workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        self.assertTrue((retained / checkout.name / "tracked.txt").is_file())
        self.assertTrue(self.store.read(run_id)["tasks"][0]["attempts"][0]["workspace"]["retained"])

    def test_cleanup_verifies_source_git_common_directory(self):
        run_id, attempt_id, checkout = self.owned()
        other = self.root / "other"
        self.repository(other)
        self.store.update(run_id, lambda state: state["tasks"][0]["attempts"][0]["workspace"].update(source_root=str(other)))
        with self.assertRaisesRegex(ValueError, "Git ownership"):
            workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        self.assertTrue(checkout.is_dir())
        self.assertTrue(self.store.read(run_id)["tasks"][0]["attempts"][0]["workspace"]["retained"])


if __name__ == "__main__":
    unittest.main()
