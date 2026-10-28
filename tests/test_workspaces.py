import contextlib
import io
import json
import os
from pathlib import Path
import shutil
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
elif mode == "optional-locks":
    print(os.environ.get("GIT_OPTIONAL_LOCKS", "unset"), flush=True)
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
        # Launch the new executable once, untimed: macOS checks a fresh file on
        # its first run, which can take seconds on a loaded machine.
        subprocess.run([str(helper)], env={**os.environ, "WORKBENCH_GIT_FIXTURE": "warm"},
                       capture_output=True, timeout=120, check=True)
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
        # Cleanup has already sent SIGKILL; give a loaded machine time to deliver it.
        deadline = time.monotonic() + 5
        while self.child_running() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(self.child_running(), "A Git helper descendant survived owned-group cleanup")

    def test_timeout_terminates_helper_and_descendant_group(self):
        os.environ["WORKBENCH_GIT_FIXTURE"] = "timeout"
        started = time.monotonic()
        with patch.object(workspaces, "GIT_TIMEOUT_SECONDS", 0.4), self.assertRaises(workspaces.GitWorkspaceError) as caught:
            workspaces._git(self.root, "status")
        self.assertEqual(caught.exception.reason, "timeout")
        # The timeout plus cleanup, far below the helper's 60-second sleep.
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(self.child_file.exists(), "The helper had not started its descendant")
        self.assert_child_stopped()

    def test_successful_parent_does_not_leave_inherited_pipe_helper_running(self):
        os.environ["WORKBENCH_GIT_FIXTURE"] = "parent-exit"
        started = time.monotonic()
        self.assertEqual(workspaces._git(self.root, "status"), "selected-metadata")
        # Well before the 30-second Git timeout the inherited pipe would reach.
        self.assertLess(time.monotonic() - started, 10)
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

    def test_helpers_never_take_optional_index_locks(self):
        os.environ["WORKBENCH_GIT_FIXTURE"] = "optional-locks"
        self.assertEqual(workspaces._git(self.root, "status", "--porcelain"), "0")

    def test_stdout_and_stderr_capture_are_bounded(self):
        for mode in ("stdout-flood", "stderr-flood"):
            with self.subTest(mode=mode):
                os.environ["WORKBENCH_GIT_FIXTURE"] = mode
                started = time.monotonic()
                with patch.object(workspaces, "GIT_OUTPUT_LIMIT", 65536), self.assertRaises(workspaces.GitWorkspaceError) as caught:
                    workspaces._git(self.root, "status")
                self.assertEqual(caught.exception.reason, "output_limit")
                self.assertNotIn("PRIVATE_HELPER_BODY", str(caught.exception))
                # Stopped by the limit, well before the 30-second Git timeout.
                self.assertLess(time.monotonic() - started, 10)


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

    def create_run(self, project=None, *, code="pass", mode="shared"):
        task = {"id": "work", "harness": "command", "argv": [sys.executable, "-c", code], "workspace": mode}
        return self.store.create({"version": 1, "name": "Durable ownership fixture", "goal": "Verify surviving checkout ownership", "tasks": [task]}, project or self.project)["id"]

    def controller(self, run_id, *, gate_crash=False, env=None):
        source = str(Path(workspaces.__file__).resolve().parent.parent)
        program = r'''
import os,sys
sys.path.insert(0,sys.argv[1])
from agent_workbench.store import Store
from agent_workbench.runner import run_workflow
from agent_workbench.workspaces import CheckoutLease
if sys.argv[4] == 'gate-crash':
    original = CheckoutLease._change
    def change(self, callback):
        original(self, callback)
        if callback.__name__ == 'register':
            os._exit(77)
    CheckoutLease._change = change
run_workflow(Store(sys.argv[2]),sys.argv[3],timeout_seconds=20)
'''
        process = subprocess.Popen([sys.executable, "-I", "-S", "-c", program, source,
                                    str(self.store.root), run_id, "gate-crash" if gate_crash else "normal"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=env, start_new_session=True)
        def close():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
        self.addCleanup(close)
        return process

    def wait(self, condition, description):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = condition()
            if result:
                return result
            time.sleep(0.01)
        self.fail("Timed out waiting for " + description)

    def stop_group_after_test(self, group):
        from agent_workbench.runner import _terminate_group
        self.addCleanup(lambda: _terminate_group(group, grace_seconds=0.2))

    def owned(self, project=None):
        project = project or self.project
        self.repository(project)
        task = {"id": "work", "harness": "command", "argv": [sys.executable, "-c", "pass"], "workspace": "worktree"}
        state = self.store.create({"version": 1, "name": "Ownership fixture", "goal": "Verify cleanup ownership", "tasks": [task]}, project)
        run_id = state["id"]
        attempt_id = "fixture-attempt"
        self.store.update(run_id, lambda current: current["tasks"][0].update(attempts=[{"id": attempt_id, "status": "running"}]))
        workspace = workspaces.prepare(self.store, run_id, task, attempt_id, project)
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

    def observed_row(self, run_id):
        return workspaces.workspace_view(self.store.read(run_id), observe=True)["workspaces"][0]

    def test_workspaces_view_observes_live_state_not_recorded_state(self):
        run_id, attempt_id, checkout = self.owned()
        # Attempt-end evidence said dirty; the tree has been cleaned since.
        self.store.update(run_id, lambda state: state["tasks"][0]["attempts"][0]["workspace"].update(dirty=True))
        row = self.observed_row(run_id)
        self.assertEqual((row["path_exists"], row["dirty"]), (True, False))
        self.assertEqual((row["recorded_path_exists"], row["recorded_dirty"]), (True, True))
        self.assertIn("observed_at", workspaces.workspace_view(self.store.read(run_id), observe=True))
        (checkout / "late.txt").write_text("written after the attempt\n")
        self.assertTrue(self.observed_row(run_id)["dirty"])
        # A tree removed outside Workbench is reported gone, not present and dirty.
        self.git(self.project, "worktree", "remove", "--force", str(checkout))
        row = self.observed_row(run_id)
        self.assertEqual((row["path_exists"], row["dirty"], row["observation_failure"]),
                         (False, None, "checkout_missing"))
        recorded = workspaces.workspace_view(self.store.read(run_id))["workspaces"][0]
        self.assertEqual((recorded["path_exists"], recorded["dirty"]), (True, True))
        self.assertNotIn("recorded_dirty", recorded)

    def test_workspaces_command_reports_observed_state(self):
        from agent_workbench.cli import main
        run_id, attempt_id, checkout = self.owned()
        self.git(self.project, "worktree", "remove", "--force", str(checkout))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.store.root), "workspaces", run_id]), 0)
        row = json.loads(output.getvalue())["workspaces"][0]
        self.assertEqual((row["path_exists"], row["dirty"], row["recorded_path_exists"]), (False, None, True))

    def test_workspaces_view_never_reports_an_enclosing_repository(self):
        run_id, attempt_id, checkout = self.owned()
        self.git(self.root, "init", "-q")  # Encloses the state directory.
        (checkout / ".git").unlink()
        row = self.observed_row(run_id)
        self.assertEqual((row["path_exists"], row["dirty"], row["observation_failure"]),
                         (True, None, "not_checkout_root"))

    def test_workspaces_view_never_reports_a_replacement_as_the_recorded_checkout(self):
        run_id, attempt_id, checkout = self.owned()
        self.git(self.project, "worktree", "remove", "--force", str(checkout))
        # An unrelated repository created where the removed tree was is its own
        # checkout root, but its Git common directory is not the source's.
        self.repository(checkout)
        row = self.observed_row(run_id)
        self.assertEqual((row["path_exists"], row["dirty"], row["observation_failure"]),
                         (True, None, "git_ownership_mismatch"))
        # A symbolic link to another worktree of the same source shares the
        # common directory; the recorded path itself must not be a link.
        shutil.rmtree(checkout)
        sibling = self.root / "sibling"
        self.git(self.project, "worktree", "add", "--detach", str(sibling), "HEAD")
        checkout.symlink_to(sibling, target_is_directory=True)
        row = self.observed_row(run_id)
        self.assertEqual((row["path_exists"], row["dirty"], row["observation_failure"]),
                         (True, None, "ownership_path_mismatch"))
        with self.assertRaisesRegex(ValueError, "ownership path"):
            workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        self.assertTrue((sibling / "tracked.txt").is_file())

    def test_cleanup_accepts_new_commits_only_when_a_remote_ref_contains_them(self):
        run_id, attempt_id, checkout = self.owned()
        (checkout / "tracked.txt").write_text("result\n")
        self.git(checkout, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qam", "result")
        head = self.git(checkout, "rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "no remote-tracking ref contains"):
            workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        self.git(self.project, "update-ref", "refs/heads/kept-locally", head)
        with self.assertRaisesRegex(ValueError, "no remote-tracking ref contains"):
            workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        # As a fetch would record a pushed branch; no remote is contacted.
        self.git(self.project, "update-ref", "refs/remotes/origin/result", head)
        (checkout / "notes.txt").write_text("uncommitted\n")
        with self.assertRaisesRegex(ValueError, "changes or new commits"):
            workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        (checkout / "notes.txt").unlink()
        state = workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        self.assertFalse(checkout.exists())
        workspace = state["tasks"][0]["attempts"][0]["workspace"]
        self.assertEqual((workspace["retained"], workspace["removal_basis"], workspace["removed_head"]),
                         (False, "head_on_remote_ref", head))
        self.assertEqual(state["events"][-1]["removal_basis"], "head_on_remote_ref")

    def test_cleanup_records_unchanged_base_as_its_basis(self):
        run_id, attempt_id, checkout = self.owned()
        state = workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        self.assertFalse(checkout.exists())
        self.assertEqual(state["tasks"][0]["attempts"][0]["workspace"]["removal_basis"], "unchanged_base")

    def evidenced(self, project=None):
        """An owned worktree with attempt-end evidence recorded, as after a real attempt."""
        project = project or self.project
        run_id, attempt_id, checkout = self.owned(project)
        workspace = self.store.read(run_id)["tasks"][0]["attempts"][0]["workspace"]
        recorded = workspaces.record_worktree_evidence(self.store, run_id, "work", attempt_id, workspace)
        self.assertEqual((recorded["evidence_status"], recorded["dirty"]), ("recorded", False))
        return run_id, attempt_id, checkout, recorded["final_head"]

    def recorded_workspace(self, run_id):
        return self.store.read(run_id)["tasks"][0]["attempts"][0]["workspace"]

    def refusal(self, run_id, attempt_id):
        with self.assertRaises(workspaces.WorkspaceRemovalRefused) as caught:
            workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        self.assertTrue(self.recorded_workspace(run_id)["retained"])
        return caught.exception

    def test_absent_unregistered_tree_closes_its_record_without_removing_anything(self):
        run_id, attempt_id, checkout, head = self.evidenced()
        # Removed and unregistered outside Workbench, as a raw `git worktree remove` does.
        self.git(self.project, "worktree", "remove", "--force", str(checkout))
        state = workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        workspace = state["tasks"][0]["attempts"][0]["workspace"]
        self.assertEqual((workspace["retained"], workspace["removal_basis"], workspace["removed_head"]),
                         (False, "absent_outside_workbench", None))
        self.assertEqual(workspace["last_recorded_head"], head)
        # No remote-tracking ref holds the recorded HEAD, so work may be lost.
        self.assertEqual((workspace["final_head_on_remote_ref"], workspace["recorded_dirty"],
                          workspace["possible_unpushed_loss"]), (False, False, True))
        self.assertTrue(workspace["observed_absent_at"])
        self.assertEqual(workspace["removed_at"], workspace["observed_absent_at"])
        event = state["events"][-1]
        self.assertEqual((event["kind"], event["task_id"], event["attempt_id"], event["removal_basis"],
                          event["possible_unpushed_loss"], event["final_head_on_remote_ref"], event["recorded_dirty"]),
                         ("workspace_removed", "work", attempt_id, "absent_outside_workbench", True, False, False))
        self.assertTrue(all(value is None or type(value) in {str, bool, int} for value in event.values()))
        self.assertTrue((self.project / "tracked.txt").is_file())
        with self.assertRaisesRegex(ValueError, "No retained"):
            workspaces.remove_clean(self.store, run_id, "work", attempt_id)

    def test_absent_tree_reports_possible_unpushed_loss_from_recorded_evidence(self):
        def on_remote(project, run_id, head):
            self.git(project, "update-ref", "refs/remotes/origin/main", head)
        def dirty(project, run_id, head):
            on_remote(project, run_id, head)
            self.store.update(run_id, lambda state: state["tasks"][0]["attempts"][0]["workspace"].update(dirty=True))
        def unavailable(project, run_id, head):
            on_remote(project, run_id, head)
            self.store.update(run_id, lambda state: state["tasks"][0]["attempts"][0]["workspace"].update(
                evidence_status="unavailable", evidence_failure="git_helper_exit"))
        def missing_object(project, run_id, head):
            on_remote(project, run_id, head)
            self.store.update(run_id, lambda state: state["tasks"][0]["attempts"][0]["workspace"].update(
                final_head="0123456789abcdef" * 2 + "01234567"))
        cases = {"pushed_and_clean": (on_remote, True, False, False, True),
                 "recorded_dirty": (dirty, True, True, True, True),
                 "evidence_unavailable": (unavailable, None, False, True, False),
                 "commit_object_missing": (missing_object, None, False, True, True)}
        for index, (name, (arrange, remote, recorded_dirty, loss, has_head)) in enumerate(cases.items()):
            with self.subTest(case=name):
                project = self.root / f"project-{index}"
                run_id, attempt_id, checkout, head = self.evidenced(project)
                arrange(project, run_id, head)
                self.git(project, "worktree", "remove", "--force", str(checkout))
                workspace = workspaces.remove_clean(self.store, run_id, "work", attempt_id)["tasks"][0]["attempts"][0]["workspace"]
                self.assertEqual((workspace["removal_basis"], workspace["final_head_on_remote_ref"],
                                  workspace["recorded_dirty"], workspace["possible_unpushed_loss"]),
                                 ("absent_outside_workbench", remote, recorded_dirty, loss))
                self.assertEqual(workspace["last_recorded_head"] is not None, has_head)

    def test_absent_tree_check_never_lazy_fetches_in_a_partial_clone(self):
        # A local "remote" stands in for a network one: a lazy fetch would
        # start upload-pack there to look for the missing commit.
        server = self.root / "server"
        self.repository(server)
        (server / "tracked.txt").write_text("server\n")
        self.git(server, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qam", "server")
        self.git(server, "config", "uploadpack.allowFilter", "true")
        project = self.root / "partial"
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", server.as_uri(), str(project)],
                       capture_output=True, check=True)
        run_id, attempt_id, checkout, head = self.evidenced(project)
        # The attempt's last commit was garbage-collected after its tree went.
        self.store.update(run_id, lambda state: state["tasks"][0]["attempts"][0]["workspace"].update(
            final_head="0123456789abcdef" * 2 + "01234567"))
        self.git(project, "worktree", "remove", "--force", str(checkout))
        trace = self.root / "git-trace.txt"
        with patch.dict(os.environ, {"GIT_TRACE": str(trace)}):
            workspace = workspaces.remove_clean(self.store, run_id, "work", attempt_id)["tasks"][0]["attempts"][0]["workspace"]
        self.assertEqual((workspace["removal_basis"], workspace["final_head_on_remote_ref"], workspace["possible_unpushed_loss"]),
                         ("absent_outside_workbench", None, True))
        commands = [line.split("built-in: ", 1)[1] for line in trace.read_text().splitlines() if "built-in: " in line]
        self.assertTrue(any(command.startswith("git for-each-ref") for command in commands))
        self.assertEqual([command for command in commands if "upload-pack" in command], [])

    def test_workspace_remove_command_says_plainly_when_work_may_be_lost(self):
        from agent_workbench.cli import main
        run_id, attempt_id, checkout, head = self.evidenced()
        self.git(self.project, "worktree", "remove", "--force", str(checkout))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.store.root), "workspace-remove", run_id, "work", attempt_id]), 0)
        text = output.getvalue()
        self.assertIn("already gone, so Workbench removed nothing", text)
        self.assertIn("Possible unpushed loss: its last recorded HEAD is on no remote-tracking ref", text)
        other = self.root / "pushed"
        run_id, attempt_id, checkout, head = self.evidenced(other)
        self.git(other, "update-ref", "refs/remotes/origin/main", head)
        self.git(other, "worktree", "remove", "--force", str(checkout))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--state-dir", str(self.store.root), "workspace-remove", run_id, "work", attempt_id]), 0)
        self.assertIn("Workbench removed nothing", output.getvalue())
        self.assertNotIn("Possible unpushed loss", output.getvalue())

    def test_absent_tree_that_git_still_registers_is_refused_and_never_pruned(self):
        from agent_workbench.cli import main
        run_id, attempt_id, checkout, head = self.evidenced()
        shutil.rmtree(checkout)  # Deleted, not unregistered: Git lists it as prunable.
        refused = self.refusal(run_id, attempt_id)
        self.assertEqual(refused.reason, "registered_but_missing")
        self.assertIn("Workbench never prunes", str(refused))
        self.assertIn("worktree " + str(checkout), self.git(self.project, "worktree", "list", "--porcelain"))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--state-dir", str(self.store.root), "workspace-remove", run_id, "work", attempt_id]), 1)
        self.assertIn("registered_but_missing", errors.getvalue())

    def test_absent_tree_named_by_an_admin_gitdir_is_refused(self):
        run_id, attempt_id, checkout, head = self.evidenced()
        shutil.rmtree(checkout)
        original = workspaces._git
        def unlisted(project, *args):
            # As if `worktree list` printed this tree's path in a form that does not compare equal.
            output = original(project, *args)
            if args[:2] == ("worktree", "list"):
                output = "\n\n".join(block for block in output.split("\n\n")
                                     if not block.startswith("worktree " + str(checkout)))
            return output
        admin = next((self.project / ".git/worktrees").iterdir())
        with patch.object(workspaces, "_git", side_effect=unlisted):
            self.assertNotIn(str(checkout), workspaces._git(self.project, "worktree", "list", "--porcelain"))
            self.assertEqual(self.refusal(run_id, attempt_id).reason, "registered_but_missing")
            # worktree.useRelativePaths stores the path relative to the admin entry.
            (admin / "gitdir").write_text(os.path.relpath(checkout / ".git", admin) + "\n")
            self.assertEqual(self.refusal(run_id, attempt_id).reason, "registered_but_missing")
        self.assertTrue(admin.is_dir())

    def test_absent_tree_moved_with_git_is_refused_not_closed(self):
        run_id, attempt_id, checkout, head = self.evidenced()
        (checkout / "work-in-progress.txt").write_text("uncommitted work kept in the moved tree\n")
        moved = self.root / "moved-tree"
        # Git keeps the admin entry Workbench created, named <task>-<attempt>,
        # and points its gitdir at the new location.
        self.git(self.project, "worktree", "move", str(checkout), str(moved))
        refused = self.refusal(run_id, attempt_id)
        self.assertEqual(refused.reason, "registered_elsewhere")
        self.assertIn("may have been moved", str(refused))
        self.assertNotIn(str(moved), str(refused))
        self.assertTrue((moved / "work-in-progress.txt").is_file())
        # Moved, then deleted without pruning: Git still names the entry.
        shutil.rmtree(moved)
        self.assertEqual(self.refusal(run_id, attempt_id).reason, "registered_but_missing")
        # Git suffixes the entry name with a number when the plain name is taken.
        admin = self.project / ".git/worktrees"
        (admin / ("work-" + attempt_id)).rename(admin / ("work-" + attempt_id + "1"))
        self.assertEqual(self.refusal(run_id, attempt_id).reason, "registered_but_missing")
        self.git(self.project, "worktree", "prune")
        state = workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        self.assertEqual(state["tasks"][0]["attempts"][0]["workspace"]["removal_basis"], "absent_outside_workbench")

    def test_absent_tree_whose_source_cannot_answer_is_refused(self):
        run_id, attempt_id, checkout, head = self.evidenced()
        self.git(self.project, "worktree", "remove", "--force", str(checkout))
        moved = self.root / "moved-project"
        self.project.rename(moved)
        self.assertEqual(self.refusal(run_id, attempt_id).reason, "source_repository_unavailable")
        moved.rename(self.project)
        # A Git failure must never read as "not listed".
        for reason in ("timeout", "missing_executable", "output_limit"):
            with self.subTest(reason=reason), patch.object(workspaces, "_git", side_effect=workspaces.GitWorkspaceError(reason)):
                self.assertEqual(self.refusal(run_id, attempt_id).reason, "source_repository_unavailable")
        # Without its own repository, Git would answer for an enclosing one instead.
        shutil.rmtree(self.project / ".git")
        self.git(self.root, "init", "-q")
        self.assertEqual(self.refusal(run_id, attempt_id).reason, "source_repository_unavailable")

    def test_absent_closure_still_refuses_foreign_paths_at_the_root(self):
        run_id, attempt_id, checkout, head = self.evidenced()
        self.git(self.project, "worktree", "remove", "--force", str(checkout))
        checkout.mkdir()
        (checkout / "foreign.txt").write_text("not Workbench's\n")
        with self.assertRaises(ValueError) as caught:
            workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        self.assertNotIsInstance(caught.exception, workspaces.WorkspaceRemovalRefused)
        self.assertTrue((checkout / "foreign.txt").is_file())
        shutil.rmtree(checkout)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        for target in (elsewhere, self.root / "dangling-target"):
            with self.subTest(target=target.name):
                checkout.symlink_to(target, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, "ownership path"):
                    workspaces.remove_clean(self.store, run_id, "work", attempt_id)
                checkout.unlink()
        # A linked worktree directory is refused even when the tree is absent.
        parent = checkout.parent
        parent.rename(parent.with_name("moved-worktrees"))
        parent.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "ownership path"):
            workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        self.assertTrue(self.recorded_workspace(run_id)["retained"])

    def test_absent_closure_refuses_while_execution_may_be_active(self):
        run_id, attempt_id, checkout, head = self.evidenced()
        self.git(self.project, "worktree", "remove", "--force", str(checkout))
        self.store.update(run_id, lambda state: state["tasks"][0].update(status="running"))
        with self.assertRaisesRegex(RuntimeError, "recorded active"):
            workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        survivor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(survivor.wait, 5)  # Cleanups run last-first: stop the group, then reap.
        self.stop_group_after_test(survivor.pid)
        def stale(state):
            state["tasks"][0]["status"] = "interrupted"
            state["tasks"][0]["attempts"][0].update(unresolved_process_group=True, process_group=survivor.pid)
        self.store.update(run_id, stale)
        with self.assertRaisesRegex(RuntimeError, "stale attempt"):
            workspaces.remove_clean(self.store, run_id, "work", attempt_id)
        self.assertTrue(self.recorded_workspace(run_id)["retained"])

    def test_controller_crash_keeps_checkout_owned_until_explicit_recovery(self):
        from agent_workbench.runner import recover_run, run_workflow
        marker = self.root / "native.pid"
        code = "import os,pathlib,time; pathlib.Path(" + repr(str(marker)) + ").write_text(str(os.getpid())); time.sleep(30)"
        owner_run = self.create_run(code=code)
        owner = self.controller(owner_run)
        self.wait(marker.exists, "owned native child")
        child = int(marker.read_text())
        self.stop_group_after_test(child)
        self.wait(lambda: self.store.read(owner_run)["tasks"][0]["attempts"][-1].get("pid"), "native process recording")
        owner.kill()
        owner.wait(timeout=3)
        self.assertTrue(workspaces._group_running(child))
        self.assertTrue(self.store.read(owner_run)["checkout_claims"])
        second_marker = self.root / "second-started"
        second_run = self.create_run(code="from pathlib import Path; Path(" + repr(str(second_marker)) + ").touch()")
        blocked = run_workflow(self.store, second_run, timeout_seconds=5)
        self.assertEqual(blocked["status"], "paused")
        self.assertFalse(blocked["tasks"][0]["attempts"])
        self.assertFalse(second_marker.exists())
        recovered = recover_run(self.store, owner_run)
        self.assertEqual(recovered["checkout_claims"], [])
        self.assertFalse(workspaces._group_running(child))
        self.assertEqual(run_workflow(self.store, second_run, timeout_seconds=5)["status"], "succeeded")
        self.assertTrue(second_marker.exists())

    def test_gate_crash_cannot_execute_native_or_python_startup_before_registration(self):
        from agent_workbench.runner import recover_run, run_workflow
        site = self.root / "python-startup"
        site.mkdir()
        sentinel = self.root / "unregistered-startup-effect"
        (site / "sitecustomize.py").write_text("from pathlib import Path\nPath(" + repr(str(sentinel)) + ").touch()\n")
        native_effect = self.root / "unregistered-native-effect"
        owner_run = self.create_run(code="from pathlib import Path; Path(" + repr(str(native_effect)) + ").touch()")
        environment = dict(os.environ, PYTHONPATH=str(site))
        owner = self.controller(owner_run, gate_crash=True, env=environment)
        self.assertEqual(owner.wait(timeout=5), 77)
        self.assertFalse(native_effect.exists())
        self.assertFalse(sentinel.exists())
        state = self.store.read(owner_run)
        self.assertTrue(state["checkout_claims"])
        index = state["checkout_claims"][0]
        ledger = workspaces._claims(workspaces._lock_directory(self.store), index["checkout_key"])
        group = ledger["claims"][0]["processes"][0]["process_group"]
        self.stop_group_after_test(group)
        self.wait(lambda: not workspaces._group_running(group), "closed gate broker exit")
        second_run = self.create_run()
        self.assertEqual(run_workflow(self.store, second_run, timeout_seconds=5)["status"], "paused")
        self.assertEqual(recover_run(self.store, owner_run)["checkout_claims"], [])
        self.assertEqual(run_workflow(self.store, second_run, timeout_seconds=5)["status"], "succeeded")

    def test_cleanup_refuses_other_run_actively_using_retained_tree(self):
        from agent_workbench.runner import run_workflow
        owner_run, attempt_id, checkout = self.owned()
        marker, gate = self.root / "foreign-started", self.root / "foreign-finish"
        code = ("from pathlib import Path; import time; Path(" + repr(str(marker)) + ").touch(); "
                "gate=Path(" + repr(str(gate)) + ")\nwhile not gate.exists(): time.sleep(0.01)\n")
        foreign_run = self.create_run(checkout, code=code, mode="read-only")
        controller = self.controller(foreign_run)
        self.wait(marker.exists, "foreign checkout reader")
        attempt = self.store.read(foreign_run)["tasks"][0]["attempts"][-1]
        self.stop_group_after_test(attempt["process_group"])
        with self.assertRaises(workspaces.WorkspaceBusy):
            workspaces.remove_clean(self.store, owner_run, "work", attempt_id)
        self.assertTrue(checkout.is_dir())
        gate.touch()
        self.assertEqual(controller.wait(timeout=5), 0)
        self.assertEqual(self.store.read(foreign_run)["status"], "succeeded")
        removed = workspaces.remove_clean(self.store, owner_run, "work", attempt_id)
        self.assertFalse(checkout.exists())
        self.assertFalse(removed["tasks"][0]["attempts"][0]["workspace"]["retained"])

    def test_worktree_hook_survives_controller_crash_but_retains_checkout_claim(self):
        from agent_workbench.runner import recover_run, run_workflow
        self.repository(self.project)
        hooks = self.project / ".git/hooks"
        marker = self.root / "post-checkout-started"
        hook = hooks / "post-checkout"
        hook.write_text(f"#!{sys.executable}\nimport pathlib,time\npathlib.Path({str(marker)!r}).touch()\ntime.sleep(30)\n")
        hook.chmod(0o700)
        self.git(self.project, "config", "core.hooksPath", str(hooks))
        owner_run = self.create_run(mode="worktree")
        controller = self.controller(owner_run)
        self.wait(marker.exists, "worktree preparation hook")
        state = self.store.read(owner_run)
        workspace = state["tasks"][0]["attempts"][-1]["workspace"]
        checkout = Path(workspace["root"])
        self.assertTrue(checkout.is_dir())
        index = state["checkout_claims"][0]
        ledger = workspaces._claims(workspaces._lock_directory(self.store), index["checkout_key"])
        helper_group = ledger["claims"][0]["processes"][0]["process_group"]
        self.stop_group_after_test(helper_group)
        controller.kill()
        controller.wait(timeout=3)
        self.assertTrue(workspaces._group_running(helper_group))
        foreign_run = self.create_run(checkout)
        self.assertEqual(run_workflow(self.store, foreign_run, timeout_seconds=5)["status"], "paused")
        recovered = recover_run(self.store, owner_run)
        self.assertEqual(recovered["checkout_claims"], [])
        self.assertFalse(workspaces._group_running(helper_group))
        self.assertTrue(checkout.is_dir())

    def test_ledger_only_native_recovery_preserves_owned_cleanup_grace(self):
        from agent_workbench.runner import _controller_lock
        for harness in ("astra", "hermes", "hermes-astra-ultra"):
            with self.subTest(harness=harness):
                task = {"id": "work", "harness": harness, "prompt": "fixture; never executed"}
                run_id = self.store.create({"version": 1, "name": "Native ownership fixture", "goal": "Verify recorded cleanup grace", "tasks": [task]}, self.project)["id"]
                with _controller_lock(self.store, run_id):
                    with workspaces.lease(self.store, self.project, "shared", run_id=run_id, task_id="work") as owner:
                        owner._change(lambda claim: claim["processes"].append({"pid": 123456789,
                            "process_group": 123456789, "process_identity": "matching-start"}))
                        with patch.object(workspaces, "_group_running", side_effect=[True, False]), \
                                patch("agent_workbench.runner._process_identity", return_value="matching-start"), \
                                patch("agent_workbench.runner._terminate_group") as terminate:
                            workspaces.reconcile_claims(self.store, run_id, terminate=True)
                        terminate.assert_called_once_with(123456789, grace_seconds=12)
                        self.assertEqual(self.store.read(run_id)["checkout_claims"], [])


if __name__ == "__main__":
    unittest.main()
