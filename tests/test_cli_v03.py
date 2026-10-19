"""CLI/schema boundaries for durable controls, profiles and plugin evaluations."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from agent_workbench.bridge import claim_requests, close_requests
from agent_workbench.cli import main
from agent_workbench.store import Store


FAKE_EVALUATOR = r'''
import json, os, pathlib, sys
if "--version" in sys.argv:
    print("2.1.282 (Claude Code)")
    sys.exit(0)
if "--help" in sys.argv:
    print("Usage: claude plugin eval [options] [target]")
    print(" --ablation --runs --case --max-cost-usd --output-dir --json --no-publish --no-scaffold")
    sys.exit(0)
assert sys.argv[1:3] == ["plugin", "eval"]
assert "--no-publish" in sys.argv
pathlib.Path(os.environ["WORKBENCH_CLI_EVAL_MARKER"]).write_text("evaluation was explicitly invoked")
mode = os.environ.get("WORKBENCH_CLI_EVAL_MODE", "completed")
if mode != "missing-report":
    report = {"schemaVersion": 1, "partial": mode == "partial", "claudeVersion": "2.1.282",
              "costUsd": .01, "durationSeconds": .1,
              "cases": [{"name": "case", "aggregates": {"score": 1, "delta": .5},
                         "arms": {"with": [{"score": 1}], "without": [{"score": .5}]}}]}
    if mode == "partial":
        report["partialReason"] = "cost_ceiling"
    report["trace"] = "PRIVATE_NATIVE_EVALUATION_TRACE"
    pathlib.Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(report))
print("PRIVATE_NATIVE_EVALUATION_LOG")
sys.exit(7 if mode == "failed" else 0)
'''


class CliV03Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = Store(self.root / "state")
        self.prefix = ["--state-dir", str(self.store.root)]

    def cli(self, *args, stdin=None):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            if stdin is None:
                code = main(self.prefix + list(args))
            else:
                with patch("sys.stdin", io.StringIO(stdin)):
                    code = main(self.prefix + list(args))
        return code, output.getvalue(), error.getvalue()

    def create(self, *, tasks=None):
        tasks = tasks or [{"id": "work", "harness": "claude", "adapter": "duplex",
                           "workspace": "read-only", "prompt": "Inspect without editing."}]
        state = self.store.create({"version": 1, "name": "CLI contract", "goal": "Verify APIs", "tasks": tasks}, self.project)
        return state["id"]

    def activate(self, run_id, task_id="work", attempt_id="attempt-1"):
        def change(state):
            state["status"] = "running"
            task = next(t for t in state["tasks"] if t["id"] == task_id)
            task["status"] = "running"
            task["attempts"].append({"id": attempt_id, "status": "running", "duplex_initialized": True,
                                      "input_closed": False, "started_at": "2026-09-25T00:00:00Z"})
        self.store.update(run_id, change)

    def profile(self, run_id, value, attempt_id="attempt-1"):
        relative = "attempts/work/" + attempt_id + "/launch.json"
        path = self.store.directory(run_id) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        def attach(state):
            state["tasks"][0]["attempts"].append({"id": attempt_id, "status": "succeeded", "launch_profile": relative})
        self.store.update(run_id, attach)
        return path

    def test_create_and_handoff_preserve_adapter_workspace_without_running(self):
        code, output, error = self.cli("create", "--project", str(self.project), "--harness", "astra",
                                       "--adapter", "duplex", "--workspace", "worktree",
                                       "--prompt", "Inspect the source.")
        self.assertEqual((code, error), (0, ""))
        run_id = output.strip()
        original = self.store.read(run_id)
        self.assertEqual(original["tasks"][0]["adapter"], "duplex")
        self.assertEqual(original["tasks"][0]["workspace"], "worktree")
        self.assertFalse(original["tasks"][0]["attempts"])
        artifact = self.root / "handoff.json"
        target = self.root / "target"
        target.mkdir()
        with patch("subprocess.Popen", side_effect=AssertionError("Authoring must not launch a process")):
            self.assertEqual(self.cli("export", run_id[:8], str(artifact))[0], 0)
            code, output, error = self.cli("import", str(artifact), "--project", str(target))
        self.assertEqual((code, error), (0, ""))
        imported = self.store.read(output.strip())
        self.assertEqual(imported["tasks"][0]["adapter"], "duplex")
        self.assertEqual(imported["tasks"][0]["workspace"], "worktree")
        self.assertEqual(imported["tasks"][0]["status"], "pending")
        self.assertFalse(imported["tasks"][0]["attempts"])
        self.assertNotIn("native_requests", imported["control"])

    def test_invalid_adapter_workspace_combinations_do_not_create_runs(self):
        cases = [
            {"id": "work", "harness": "codex", "adapter": "duplex", "prompt": "Inspect"},
            {"id": "work", "harness": "command", "adapter": "duplex", "argv": ["true"]},
            {"id": "work", "harness": "claude", "adapter": "automatic", "prompt": "Inspect"},
            {"id": "work", "harness": "claude", "workspace": "isolated", "prompt": "Inspect"},
            {"id": "work", "harness": "claude", "workspace": {"path": "/outside"}, "prompt": "Inspect"},
        ]
        for task in cases:
            with self.subTest(task=task):
                manifest = {"version": 1, "name": "Invalid", "goal": "Reject", "tasks": [task]}
                code, output, error = self.cli("create", "-", "--project", str(self.project), stdin=json.dumps(manifest))
                self.assertEqual(code, 1)
                self.assertFalse(output)
                self.assertNotIn("Traceback", error)
        self.assertFalse(self.store.list_runs())

    def test_run_jobs_cli_bounds_exit_status_and_resume_preserve_finished_attempts(self):
        tasks = [{"id": ident, "harness": "command", "workspace": "read-only",
                  "argv": [sys.executable, "-c", "print('synthetic command complete')"]} for ident in ("a", "b")]
        run_id = self.create(tasks=tasks)
        before = self.store.read(run_id)
        for jobs in ("0", "17"):
            self.assertEqual(self.cli("run", run_id, "--jobs", jobs)[0], 1)
            self.assertEqual(self.store.read(run_id), before)
        code, _, error = self.cli("run", run_id, "--jobs", "2", "--max-launches", "1", "--timeout", "4")
        self.assertEqual((code, error), (2, ""))
        paused = self.store.read(run_id)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual([task["status"] for task in paused["tasks"]], ["succeeded", "pending"])
        self.assertEqual(next(event["jobs"] for event in paused["events"] if event["kind"] == "run_started"), 2)
        code, _, error = self.cli("run", run_id, "--jobs", "2", "--timeout", "4")
        self.assertEqual((code, error), (0, ""))
        completed = self.store.read(run_id)
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual([len(task["attempts"]) for task in completed["tasks"]], [1, 1])

    def test_run_warns_before_starting_when_its_timeout_cuts_a_hermes_budget(self):
        run_id = self.create(tasks=[{"id": "astra", "harness": "hermes-astra-ultra", "prompt": "Work.",
                                     "run_budget_seconds": 2400, "timeout_seconds": 3000}])
        observed = []
        def fake_run(store, ident, **kwargs):
            observed.append(kwargs["timeout_seconds"])
            return store.read(ident)
        with patch("agent_workbench.runner.run_workflow", side_effect=fake_run):
            code, _, error = self.cli("run", run_id)
            self.assertEqual(code, 2)
            self.assertIn("task astra has a Hermes run budget of 2400 s, but run --timeout 1800 s stops the run first", error)
            code, _, error = self.cli("run", run_id, "--timeout", "3300")
            self.assertEqual((code, error), (2, ""))
        self.assertEqual(observed, [1800, 3300])

    def test_caller_request_id_is_durable_and_identical_reuse_does_not_enqueue(self):
        run_id = self.create()
        self.activate(run_id)
        code, output, error = self.cli("send", run_id[:8], "work", "PRIVATE_USER_GUIDANCE", "--request-id", "stable-1")
        self.assertEqual((code, error), (0, ""))
        first = json.loads(output)
        self.assertEqual(first["status"], "queued")
        self.assertEqual(first["id"], "stable-1")
        self.assertNotIn("PRIVATE_USER_GUIDANCE", output)
        # Each main() creates a fresh Store; this tests durable reuse rather
        # than an in-memory deduplication cache.
        code, output, _ = self.cli("send", run_id, "work", "PRIVATE_USER_GUIDANCE", "--request-id", "stable-1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), first)
        state = self.store.read(run_id)
        self.assertEqual(len(state["control"]["native_requests"]), 1)
        self.assertEqual(sum(event["kind"] == "native_control_queued" for event in state["events"]), 1)

    def test_request_id_conflicts_leave_saved_state_unchanged(self):
        run_id = self.create()
        self.activate(run_id)
        self.assertEqual(self.cli("send", run_id, "work", "first", "--request-id", "stable-1")[0], 0)
        before = self.store.read(run_id)
        for arguments in (("send", run_id, "work", "different", "--request-id", "stable-1"),
                          ("interrupt", run_id, "work", "--request-id", "stable-1"),
                          ("send", run_id, "other", "first", "--request-id", "stable-1")):
            with self.subTest(arguments=arguments):
                code, output, error = self.cli(*arguments)
                self.assertEqual(code, 1)
                self.assertIn("different input", error)
                self.assertFalse(output)
                self.assertEqual(self.store.read(run_id), before)

    def test_old_request_id_never_redispatches_into_a_new_attempt(self):
        run_id = self.create()
        self.activate(run_id)
        self.assertEqual(self.cli("send", run_id, "work", "same text", "--request-id", "stable-1")[0], 0)
        self.assertEqual(len(claim_requests(self.store, run_id, "attempt-1")), 1)
        close_requests(self.store, run_id, "attempt-1")
        self.activate(run_id, attempt_id="attempt-2")
        code, output, _ = self.cli("send", run_id, "work", "same text", "--request-id", "stable-1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["attempt_id"], "attempt-1")
        self.assertEqual(json.loads(output)["status"], "unacknowledged")
        self.assertEqual(claim_requests(self.store, run_id, "attempt-2"), [])
        self.assertEqual(self.cli("send", run_id, "work", "same text", "--request-id", "fresh-2")[0], 0)
        claimed = claim_requests(self.store, run_id, "attempt-2")
        self.assertEqual([request["id"] for request in claimed], ["fresh-2"])

    def test_invalid_explicit_request_ids_never_create_queue_entries(self):
        run_id = self.create()
        self.activate(run_id)
        before = self.store.read(run_id)
        for request_id in ("", "../other", "a" * 81, "sk-syntheticcredential"):
            with self.subTest(request_id=request_id):
                code, output, error = self.cli("interrupt", run_id, "work", "--request-id", request_id)
                self.assertEqual(code, 1)
                self.assertFalse(output)
                if request_id:
                    self.assertNotIn(request_id, error)
                self.assertEqual(self.store.read(run_id), before)

    def test_reserved_controller_request_ids_are_rejected_atomically(self):
        run_id = self.create()
        self.activate(run_id)
        before = self.store.read(run_id)
        identifiers = ("attempt-1-initial", "attempt-1-initialize", "attempt-1-future-control",
                       "f" * 32 + "-future-control", "workbench-future-internal")
        for ident in identifiers:
            for action, body in (("send", ("followup",)), ("interrupt", ())):
                with self.subTest(ident=ident, action=action):
                    code, output, error = self.cli(action, run_id, "work", *body, "--request-id", ident)
                    self.assertEqual(code, 1)
                    self.assertIn("reserved controller namespace", error)
                    self.assertFalse(output)
                    self.assertEqual(self.store.read(run_id), before)
        self.assertEqual(self.cli("send", run_id, "work", "legitimate", "--request-id", "caller-followup")[0], 0)

    def test_native_wire_id_collisions_are_rejected_before_dispatch(self):
        from agent_workbench.native import _NAMESPACE
        run_id = self.create()
        self.activate(run_id)
        initial_wire_id = str(uuid.uuid5(_NAMESPACE, "attempt-1-initial"))
        before = self.store.read(run_id)
        code, _, error = self.cli("interrupt", run_id, "work", "--request-id", initial_wire_id)
        self.assertEqual(code, 1)
        self.assertIn("wire identifier", error)
        self.assertEqual(self.store.read(run_id), before)
        self.assertEqual(self.cli("send", run_id, "work", "queued input", "--request-id", "caller-input")[0], 0)
        input_wire_id = str(uuid.uuid5(_NAMESPACE, "caller-input"))
        before = self.store.read(run_id)
        self.assertEqual(self.cli("interrupt", run_id, "work", "--request-id", input_wire_id)[0], 1)
        self.assertEqual(self.store.read(run_id), before)
        future_input_wire = str(uuid.uuid5(_NAMESPACE, "future-caller-input"))
        self.assertEqual(self.cli("interrupt", run_id, "work", "--request-id", future_input_wire)[0], 0)
        before = self.store.read(run_id)
        code, _, error = self.cli("send", run_id, "work", "colliding future input", "--request-id", "future-caller-input")
        self.assertEqual(code, 1)
        self.assertIn("wire identifier", error)
        self.assertEqual(self.store.read(run_id), before)

    def test_existing_reserved_looking_receipt_is_lookup_only_after_upgrade(self):
        run_id = self.create()
        self.activate(run_id)
        def old_receipt(state):
            state["control"]["native_requests"] = [{"id": "workbench-old-caller", "task_id": "work", "attempt_id": "old-attempt",
                                                     "action": "interrupt", "status": "acknowledged", "at": "earlier"}]
        self.store.update(run_id, old_receipt)
        code, output, error = self.cli("interrupt", run_id, "work", "--request-id", "workbench-old-caller")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["attempt_id"], "old-attempt")
        self.assertEqual(claim_requests(self.store, run_id, "attempt-1"), [])

    def test_input_batch_bound_preserves_interrupt_capacity_and_is_per_attempt(self):
        run_id = self.create()
        self.activate(run_id)
        for number in range(63):
            self.assertEqual(self.cli("send", run_id, "work", "bounded input", "--request-id", f"input-{number}")[0], 0)
        # Dispatching is still potentially consumed by the native batch; it
        # must not reset the per-attempt limit just because the queue drained.
        self.assertEqual(len(claim_requests(self.store, run_id, "attempt-1")), 63)
        before = self.store.read(run_id)
        code, output, error = self.cli("send", run_id, "work", "extra", "--request-id", "input-overflow")
        self.assertEqual(code, 1)
        self.assertIn("input limit", error)
        self.assertFalse(output)
        self.assertEqual(self.store.read(run_id), before)
        self.assertEqual(self.cli("interrupt", run_id, "work", "--request-id", "interrupt-after-inputs")[0], 0)
        close_requests(self.store, run_id, "attempt-1")
        self.activate(run_id, attempt_id="attempt-2")
        self.assertEqual(self.cli("send", run_id, "work", "new attempt", "--request-id", "new-input")[0], 0)
        state = self.store.read(run_id)
        self.assertEqual(state["control"]["native_requests"][-1]["attempt_id"], "attempt-2")

    def test_queue_and_total_request_bounds_do_not_block_existing_receipt_lookup(self):
        run_id = self.create()
        self.activate(run_id)
        for number in range(64):
            self.assertEqual(self.cli("interrupt", run_id, "work", "--request-id", f"interrupt-{number}")[0], 0)
        before = self.store.read(run_id)
        self.assertEqual(self.cli("interrupt", run_id, "work", "--request-id", "queued-overflow")[0], 1)
        self.assertEqual(self.store.read(run_id), before)
        self.assertEqual(len(claim_requests(self.store, run_id, "attempt-1")), 64)
        for number in range(64, 128):
            self.assertEqual(self.cli("interrupt", run_id, "work", "--request-id", f"interrupt-{number}")[0], 0)
        self.assertEqual(len(claim_requests(self.store, run_id, "attempt-1")), 64)
        before = self.store.read(run_id)
        self.assertEqual(self.cli("interrupt", run_id, "work", "--request-id", "total-overflow")[0], 1)
        self.assertEqual(self.store.read(run_id), before)
        code, output, error = self.cli("interrupt", run_id, "work", "--request-id", "interrupt-0")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["status"], "dispatching")
        self.assertEqual(len(self.store.read(run_id)["control"]["native_requests"]), 128)

    def test_metadata_status_omits_private_content_and_local_paths(self):
        run_id = self.create()
        self.activate(run_id)
        def populate(state):
            state.update(name="PRIVATE_NAME", goal="PRIVATE_GOAL")
            state["tasks"][0]["prompt"] = "PRIVATE_PROMPT"
            state["tasks"][0]["result"] = {"summary": "PRIVATE_SUMMARY"}
            state["tasks"][0]["attempts"][0]["workspace"] = {"path": str(self.project)}
            state["notes"] = [{"kind": "next", "text": "PRIVATE_NOTE", "at": "today"}]
        self.store.update(run_id, populate)
        self.cli("send", run_id, "work", "PRIVATE_FOLLOWUP", "--request-id", "input-1")
        before = self.store.read(run_id)
        code, output, error = self.cli("status", run_id[:8], "--metadata")
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn("PRIVATE", output)
        self.assertNotIn(str(self.root), output)
        metadata = json.loads(output)
        self.assertEqual(metadata["native_request_count"], 1)
        self.assertEqual(metadata["tasks"][0]["adapter"], "duplex")
        self.assertEqual(self.store.read(run_id), before)

    def test_launch_selects_latest_or_exact_attempt_without_state_mutation(self):
        run_id = self.create()
        self.profile(run_id, {"schema_version": 1, "harness": "claude", "evidence": "first"}, "first")
        self.profile(run_id, {"schema_version": 1, "harness": "claude", "evidence": "second"}, "second")
        before = self.store.read(run_id)
        for arguments, expected in (((), "second"), (("--attempt", "first"), "first")):
            code, output, error = self.cli("launch", run_id[:8], "work", *arguments)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(json.loads(output)["evidence"], expected)
        self.assertEqual(self.cli("launch", run_id, "work", "--attempt", "unknown")[0], 1)
        self.assertEqual(self.store.read(run_id), before)

    def test_launch_refuses_outside_paths_and_symlinks_before_reading(self):
        run_id = self.create()
        path = self.profile(run_id, {"schema_version": 1})
        outside = self.root / "outside.json"
        outside.write_text('{"private":"PRIVATE_PROFILE_SENTINEL"}')
        link = path.parent / "link.json"
        link.symlink_to(outside)
        parent_link = self.store.directory(run_id) / "outside-directory"
        parent_link.symlink_to(self.root, target_is_directory=True)
        for target in (str(outside), "../../outside.json", str(link.relative_to(self.store.directory(run_id))), "outside-directory/outside.json"):
            with self.subTest(target=target):
                self.store.update(run_id, lambda state: state["tasks"][0]["attempts"][0].update(launch_profile=target))
                before = self.store.read(run_id)
                code, output, error = self.cli("launch", run_id, "work")
                self.assertEqual(code, 1)
                self.assertIn("Invalid launch profile path", error)
                self.assertNotIn("PRIVATE_PROFILE_SENTINEL", output + error)
                self.assertEqual(self.store.read(run_id), before)

    def test_launch_refuses_contained_fifo_without_waiting_for_a_writer(self):
        run_id = self.create()
        path = self.profile(run_id, {"schema_version": 1})
        path.unlink()
        os.mkfifo(path)
        executable = Path(__file__).resolve().parents[1] / "scripts/agent-workbench"
        try:
            result = subprocess.run([sys.executable, str(executable), *self.prefix, "launch", run_id, "work"],
                                    capture_output=True, text=True, timeout=2)
        except subprocess.TimeoutExpired:
            self.fail("launch blocked on a non-regular contained profile")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(result.stdout)

    def test_explain_cli_is_partial_read_only_and_does_not_load_instruction_text(self):
        home = self.root / "home"
        claude = home / ".claude"
        claude.mkdir(parents=True)
        (claude / "settings.json").write_text(json.dumps({"model": "opus", "env": {"PRIVATE_CONFIG": "PRIVATE_SECRET"}}))
        (self.project / "CLAUDE.md").write_text("PRIVATE_INSTRUCTION_BODY")
        environment = {"CLAUDE_CONFIG_DIR": str(claude), "CODEX_HOME": str(home / ".codex")}
        with patch.dict(os.environ, environment), patch("pathlib.Path.home", return_value=home), \
             patch("subprocess.Popen", side_effect=AssertionError("Configuration explanation must not launch")):
            code, output, error = self.cli("explain", "--project", str(self.project), "--json")
        self.assertEqual((code, error), (0, ""))
        report = json.loads(output)
        self.assertEqual(report["scope"], "partial")
        self.assertFalse(report["authentication_files_read"])
        self.assertFalse(report["instructions"]["contents_read"])
        self.assertEqual(report["claude"]["inferred_selected"]["model"]["value"], "opus")
        self.assertNotIn("PRIVATE", output)
        self.assertFalse(self.store.list_runs())

    def test_evaluation_cli_plan_and_execution_exit_statuses_use_real_fake_process(self):
        plugin = self.root / "plugin"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin/plugin.json").write_text('{"name":"fixture","version":"1.0.0"}')
        (plugin / "evals/case").mkdir(parents=True)
        (plugin / "evals/case/prompt.md").write_text("Check the fixture.")
        executable = self.root / "claude"
        executable.write_text(f"#!{sys.executable}\n" + FAKE_EVALUATOR)
        executable.chmod(0o700)
        marker = self.root / "evaluation-invoked"
        plan = self.root / "plan.json"
        with patch.dict(os.environ, {"WORKBENCH_CLI_EVAL_MARKER": str(marker)}):
            code, output, error = self.cli("eval-plan", str(plugin), "--executable", str(executable), "--runs", "1", "--output", str(plan))
            self.assertEqual((code, error), (0, ""))
            self.assertFalse(marker.exists())
            self.assertEqual(plan.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(output)["maximum_agent_runs"], 2)
            for mode, expected_exit in (("completed", 0), ("partial", 1), ("failed", 1), ("missing-report", 1)):
                with self.subTest(mode=mode), patch.dict(os.environ, {"WORKBENCH_CLI_EVAL_MODE": mode}):
                    code, output, error = self.cli("eval-run", str(plan))
                    self.assertEqual((code, error), (expected_exit, ""))
                    self.assertTrue(marker.exists())
                    self.assertNotIn("PRIVATE", output)
                    report = json.loads(output)
                    self.assertEqual(report["publication"], "local only")
                    if mode == "completed":
                        self.assertTrue(report["eligible_for_comparison"])
                    else:
                        self.assertFalse(report["eligible_for_comparison"])


if __name__ == "__main__":
    unittest.main()
