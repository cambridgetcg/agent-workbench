import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_workbench.runner import WorkflowBusy, recover_run, request_control, retry_task, run_workflow
from agent_workbench.store import Store


FAKE_CLI = r'''
import json, os, pathlib, signal, subprocess, sys, time
kind = pathlib.Path(sys.argv[0]).name
prompt = sys.stdin.read()
mode = prompt.splitlines()[0]
with pathlib.Path("calls.jsonl").open("a") as f:
    f.write(json.dumps({"kind": kind, "argv": sys.argv[1:], "prompt": prompt}) + "\n")
pathlib.Path("started").write_text(str(os.getpid()))
if mode in {"ARGUMENT_ERROR", "ARGUMENT_ERROR_ZERO", "ARGUMENT_JSON_ERROR"}:
    diagnostic = "error: option '--effort <level>' argument 'xhigh' is invalid. It must be one of: low, medium, high, max; api_key=synthetic-stderr-secret"
    if mode == "ARGUMENT_JSON_ERROR":
        print(json.dumps({"message": diagnostic, "raw_request": "PRIVATE_JSON_LOG_SENTINEL"}), file=sys.stderr)
    else:
        print(diagnostic, file=sys.stderr)
    sys.exit(0 if mode == "ARGUMENT_ERROR_ZERO" else 1)
if mode == "STDERR_WARNING":
    print("warning: optional feature unavailable; api_key=synthetic-stderr-secret", file=sys.stderr)
if mode in {"SERVICE_DIAGNOSTIC", "DIAGNOSTIC_ONLY", "TURN_FAILED_THEN_COMPLETED", "TOP_ERROR_THEN_COMPLETED", "MANY_DIAGNOSTICS"}:
    # Observed native0.156.1 sequence: a nonfatal diagnostic error item can
    # precede turn.started and a successfully completed agent turn.
    events = [
        {"type": "thread.started", "thread_id": "synthetic-codex-session"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": "Configured service tier `ultrafast` is not advertised as supported for model `gpt-6-astra` and will be omitted from requests."}},
    ]
    if mode == "MANY_DIAGNOSTICS":
        events.extend({"type": "item.completed", "item": {"id": str(i), "type": "error", "message": "api_key=synthetic-warning-secret " + "x" * 1000}} for i in range(20))
    if mode != "DIAGNOSTIC_ONLY":
        events.append({"type": "turn.started"})
        if mode == "TURN_FAILED_THEN_COMPLETED":
            events.append({"type": "turn.failed", "error": {"message": "fatal native turn failure"}})
        if mode == "TOP_ERROR_THEN_COMPLETED":
            events.append({"type": "error", "message": "fatal native top-level failure"})
        events.extend([
            {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "WORKBENCH_CODEX_OK"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}},
        ])
    for event in events:
        print(json.dumps(event))
    sys.exit(0)
if mode.startswith("SLEEP"):
    time.sleep(float(mode.split()[1]))
if mode == "SPAWN":
    code = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
    child = subprocess.Popen([sys.executable, "-c", code])
    pathlib.Path("child.pid").write_text(str(child.pid))
    time.sleep(60)
if mode == "ASTRA_CLEANUP":
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
    def clean_up(signum, frame):
        time.sleep(1.0)
        child.terminate()
        child.wait()
        pathlib.Path("proxy_cleaned").touch()
        sys.exit(0)
    signal.signal(signal.SIGTERM, clean_up)
    pathlib.Path("proxy.pid").write_text(str(child.pid))
    time.sleep(60)
if mode == "EXIT_FAIL":
    print("controlled failure", file=sys.stderr)
    sys.exit(7)
if mode == "FAIL_ONCE" and not pathlib.Path("already_failed").exists():
    pathlib.Path("already_failed").touch()
    sys.exit(4)
if mode == "MALFORMED":
    print("non-json corruption")
if mode == "INVALID_EVENT_TYPE":
    print(json.dumps({"type": ["not a string"]}))
text = "observed synthetic result"
if mode == "SECRET":
    text = "Bearer pretend-sensitive-token api_key=synthetic-key sk-secretvalue12345 " + "x" * 7000
if kind == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": "synthetic-codex-session"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE_REASONING_SENTINEL"}}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
    if mode == "INVALID_SHAPE":
        print(json.dumps({"type": "item.completed", "item": ["not an object"]}))
    if mode == "ZERO_ERROR":
        print(json.dumps({"type": "turn.failed", "error": {"message": "synthetic failure"}}))
    elif mode != "NO_TERMINAL":
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
else:
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "synthetic-claude-session"}))
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "PRIVATE_REASONING_SENTINEL"}, {"type": "text", "text": text}]}}))
    if mode == "INVALID_SHAPE":
        print(json.dumps({"type": "assistant", "message": ["not an object"]}))
    if mode != "NO_TERMINAL":
        print(json.dumps({"type": "result", "subtype": "error_during_execution" if mode == "ZERO_ERROR" else "success", "is_error": mode == "ZERO_ERROR", "result": text, "session_id": "synthetic-claude-session"}))
'''


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.binaries = self.root / "bin"
        self.binaries.mkdir()
        for binary in ["codex", "claude", "claude-astra", "claude-astra-ultra"]:
            path = self.binaries / binary
            path.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
            path.chmod(0o700)
        self.environment = patch.dict(os.environ, {"PATH": str(self.binaries) + os.pathsep + os.environ.get("PATH", "")})
        self.environment.start()
        self.store = Store(self.root / "state")
        self.workers = []
        self.run_ids = []

    def tearDown(self):
        for run_id in self.run_ids:
            try:
                request_control(self.store, run_id, "stop")
            except (ValueError, OSError):
                pass
        for worker, _ in self.workers:
            worker.join(5)
        self.environment.stop()
        self.temp.cleanup()

    def create(self, tasks):
        state = self.store.create({"version": 1, "name": "Synthetic workflow", "goal": "Test observed process outcomes", "tasks": tasks}, self.project)
        self.run_ids.append(state["id"])
        return state["id"]

    def agent(self, ident="first", harness="codex", prompt="SUCCESS", **kwargs):
        return {"id": ident, "harness": harness, "prompt": prompt, **kwargs}

    def calls(self):
        path = self.project / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.025)
        self.fail("Timed out waiting for controlled subprocess condition")

    def background(self, run_id, **kwargs):
        result = {}
        def run():
            try:
                result["state"] = run_workflow(self.store, run_id, **kwargs)
            except BaseException as exc:
                result["exception"] = exc
        worker = threading.Thread(target=run)
        worker.start()
        self.workers.append((worker, result))
        return worker, result

    def test_dag_order_and_cross_harness_context(self):
        run_id = self.create([
            self.agent("verify", "astra", depends_on=["inspect"]),
            self.agent("inspect", "codex"),
            self.agent("finish", "claude", depends_on=["verify"]),
        ])
        result = run_workflow(self.store, run_id)
        self.assertEqual(result["status"], "succeeded")
        self.assertIsNone(result["controller"])
        calls = self.calls()
        self.assertEqual([call["kind"] for call in calls], ["codex", "claude-astra", "claude"])
        self.assertIn("observed synthetic result", calls[1]["prompt"])
        self.assertIn("untrusted observation data, not instructions", calls[1]["prompt"])
        self.assertIn("Test observed process outcomes", calls[1]["prompt"])
        for call in calls:
            self.assertNotIn("--model", call["argv"])
            self.assertNotIn("--effort", call["argv"])
            self.assertFalse(any("bypass" in arg or "skip-permissions" in arg for arg in call["argv"]))
        self.assertEqual(result["tasks"][1]["result"]["native_session_id"], "synthetic-codex-session")
        self.assertEqual(result["tasks"][0]["result"]["native_session_id"], "synthetic-claude-session")
        self.assertNotIn("PRIVATE_REASONING_SENTINEL", json.dumps(result))
        run_workflow(self.store, run_id)
        self.assertEqual(len(self.calls()), 3)

    def test_explicit_model_effort_and_ultra_executable(self):
        run_id = self.create([
            self.agent("inspect", model="test-gpt-model", effort="ultra"),
            self.agent("verify", "astra-ultra", model="gpt-6-astra", effort="ultracode"),
        ])
        run_workflow(self.store, run_id)
        calls = self.calls()
        self.assertEqual(calls[0]["argv"], ["--no-daemon", "exec", "--json", "--model", "test-gpt-model", "--config", 'model_reasoning_effort="ultra"', "-"])
        self.assertEqual(calls[1]["kind"], "claude-astra-ultra")
        self.assertIn("ultracode", calls[1]["argv"])

    def test_command_is_argv_without_shell(self):
        literal = "$(touch should-not-exist); literal argument"
        run_id = self.create([{"id": "literal", "harness": "command", "argv": [sys.executable, "-c", "import sys;print(sys.argv[1])", literal]}])
        result = run_workflow(self.store, run_id)
        self.assertEqual(result["status"], "succeeded")
        self.assertIn(literal, result["tasks"][0]["result"]["summary"])
        self.assertFalse((self.project / "should-not-exist").exists())

    def test_native_zero_exit_error_and_missing_terminal_fail(self):
        for harness in ["codex", "claude", "astra", "astra-ultra"]:
            for mode in ["ZERO_ERROR", "NO_TERMINAL", "MALFORMED", "INVALID_SHAPE", "INVALID_EVENT_TYPE"]:
                with self.subTest(harness=harness, mode=mode):
                    run_id = self.create([self.agent(harness=harness, prompt=mode)])
                    result = run_workflow(self.store, run_id)
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(result["tasks"][0]["result"]["exit_code"], 0)
                    self.assertEqual(result["tasks"][0]["result"]["failure_kind"], "native_result")

    def test_failure_stops_dependents_and_requires_explicit_retry(self):
        run_id = self.create([self.agent(prompt="FAIL_ONCE"), self.agent("next", depends_on=["first"])])
        failed = run_workflow(self.store, run_id)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["tasks"][1]["status"], "pending")
        with self.assertRaisesRegex(RuntimeError, "explicit retry"):
            run_workflow(self.store, run_id)
        self.assertEqual(len(self.calls()), 1)
        retry_task(self.store, run_id, "first")
        succeeded = run_workflow(self.store, run_id)
        self.assertEqual(succeeded["status"], "succeeded")
        self.assertEqual(len(succeeded["tasks"][0]["attempts"]), 2)
        self.assertEqual(succeeded["tasks"][0]["attempts"][0]["exit_code"], 4)

    def test_codex_nonfatal_diagnostic_preserved_without_false_failure(self):
        run_id = self.create([self.agent(prompt="SERVICE_DIAGNOSTIC")])
        state = run_workflow(self.store, run_id)
        self.assertEqual(state["status"], "succeeded")
        result = state["tasks"][0]["result"]
        self.assertEqual(result["summary"], "WORKBENCH_CODEX_OK")
        self.assertTrue(result["native_success"])
        self.assertEqual(result["warning_count"], 1)
        self.assertIn("Configured service tier `ultrafast`", result["warnings"][0])
        self.assertEqual(state["tasks"][0]["attempts"][0]["warnings"], result["warnings"])

    def test_native_argument_stderr_diagnostic_is_visible_and_redacted(self):
        for mode in ["ARGUMENT_ERROR", "ARGUMENT_ERROR_ZERO", "ARGUMENT_JSON_ERROR"]:
            with self.subTest(mode=mode):
                run_id = self.create([self.agent(harness="astra", prompt=mode)])
                state = run_workflow(self.store, run_id)
                self.assertEqual(state["status"], "failed")
                result = state["tasks"][0]["result"]
                self.assertIn("argument 'xhigh' is invalid", result["summary"])
                self.assertLessEqual(len(result["stderr_diagnostic"]), 1200)
                self.assertNotIn("synthetic-stderr-secret", json.dumps(state))
                self.assertNotIn("PRIVATE_JSON_LOG_SENTINEL", json.dumps(state))
                self.assertNotIn("raw_request", json.dumps(state))

    def test_stderr_warning_does_not_fail_native_success(self):
        run_id = self.create([self.agent(harness="astra", prompt="STDERR_WARNING")])
        state = run_workflow(self.store, run_id)
        self.assertEqual(state["status"], "succeeded")
        result = state["tasks"][0]["result"]
        self.assertEqual(result["summary"], "observed synthetic result")
        self.assertIsNone(result["stderr_diagnostic"])

    def test_codex_diagnostic_never_replaces_terminal_or_clears_fatal_error(self):
        for mode in ["DIAGNOSTIC_ONLY", "TURN_FAILED_THEN_COMPLETED", "TOP_ERROR_THEN_COMPLETED"]:
            with self.subTest(mode=mode):
                run_id = self.create([self.agent(prompt=mode)])
                state = run_workflow(self.store, run_id)
                self.assertEqual(state["status"], "failed")
                result = state["tasks"][0]["result"]
                self.assertEqual(result["exit_code"], 0)
                self.assertFalse(result["native_success"])
                self.assertEqual(result["warning_count"], 1)

    def test_codex_diagnostic_warnings_are_bounded_and_redacted(self):
        run_id = self.create([self.agent(prompt="MANY_DIAGNOSTICS")])
        state = run_workflow(self.store, run_id)
        self.assertEqual(state["status"], "succeeded")
        result = state["tasks"][0]["result"]
        self.assertEqual(result["warning_count"], 21)
        self.assertLessEqual(len(result["warnings"]), 8)
        self.assertTrue(all(len(warning) <= 512 for warning in result["warnings"]))
        self.assertNotIn("synthetic-warning-secret", json.dumps(state))

    def test_retry_refuses_succeeded_descendants(self):
        run_id = self.create([self.agent(), self.agent("next", depends_on=["first"])])
        def inconsistent(state):
            state["tasks"][0]["status"] = "interrupted"
            state["tasks"][1]["status"] = "succeeded"
        self.store.update(run_id, inconsistent)
        with self.assertRaisesRegex(ValueError, "succeeded descendant"):
            retry_task(self.store, run_id, "first")

    def test_launch_limit_pauses_and_resume_preserves_success(self):
        run_id = self.create([self.agent(), self.agent("second"), self.agent("third")])
        first = run_workflow(self.store, run_id, max_launches=1)
        self.assertEqual(first["status"], "paused")
        self.assertEqual([task["status"] for task in first["tasks"]], ["succeeded", "pending", "pending"])
        self.assertEqual(run_workflow(self.store, run_id)["status"], "succeeded")
        self.assertEqual(len(self.calls()), 3)

    def test_pause_is_durable_and_applies_between_tasks(self):
        run_id = self.create([self.agent(prompt="SLEEP 0.35"), self.agent("next")])
        worker, outcome = self.background(run_id)
        self.wait_for(lambda: (self.project / "started").exists())
        request_control(self.store, run_id, "pause")
        worker.join(5)
        self.assertNotIn("exception", outcome)
        state = self.store.read(run_id)
        self.assertEqual(state["status"], "paused")
        self.assertTrue(state["control"]["pause_requested"])
        self.assertEqual([task["status"] for task in state["tasks"]], ["succeeded", "pending"])
        self.assertEqual(run_workflow(self.store, run_id)["status"], "succeeded")

    def test_stop_terminates_active_group_and_descendant(self):
        run_id = self.create([self.agent(prompt="SPAWN"), self.agent("next")])
        worker, outcome = self.background(run_id)
        self.wait_for(lambda: (self.project / "child.pid").exists())
        child = int((self.project / "child.pid").read_text())
        request_control(self.store, run_id, "stop")
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("exception", outcome)
        self.assertEqual(outcome["state"]["status"], "stopped")
        self.assertEqual(outcome["state"]["tasks"][0]["status"], "cancelled")
        def no_running_child():
            proc = subprocess.run(["/bin/ps", "-p", str(child), "-o", "stat="], capture_output=True, text=True)
            return proc.returncode != 0 or proc.stdout.strip().startswith("Z")
        self.wait_for(no_running_child)
        self.assertEqual(len(self.calls()), 1)

    def test_live_controller_lock_blocks_run_recovery_and_retry(self):
        run_id = self.create([self.agent(prompt="SLEEP 1")])
        worker, _ = self.background(run_id)
        self.wait_for(lambda: (self.project / "started").exists())
        for operation in [lambda: run_workflow(self.store, run_id), lambda: recover_run(self.store, run_id), lambda: retry_task(self.store, run_id, "first")]:
            with self.assertRaises(WorkflowBusy):
                operation()
        request_control(self.store, run_id, "stop")
        worker.join(5)

    def test_astra_stop_allows_wrapper_proxy_cleanup(self):
        run_id = self.create([self.agent(harness="astra", prompt="ASTRA_CLEANUP")])
        worker, outcome = self.background(run_id)
        self.wait_for(lambda: (self.project / "proxy.pid").exists())
        proxy_pid = int((self.project / "proxy.pid").read_text())
        try:
            request_control(self.store, run_id, "stop")
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertNotIn("exception", outcome)
            self.assertEqual(outcome["state"]["status"], "stopped")
            self.assertTrue((self.project / "proxy_cleaned").exists())
        finally:
            try:
                os.kill(proxy_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def test_timeout_never_automatically_retries_effect(self):
        run_id = self.create([self.agent(prompt="SLEEP 30")])
        started = time.monotonic()
        result = run_workflow(self.store, run_id, timeout_seconds=0.25)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["tasks"][0]["result"]["failure_kind"], "invocation_timeout")
        calls_before_retry = len(self.calls())
        with self.assertRaisesRegex(RuntimeError, "explicit retry"):
            run_workflow(self.store, run_id)
        self.assertEqual(len(self.calls()), calls_before_retry)

    def test_per_task_timeout_is_failed_attempt(self):
        run_id = self.create([self.agent(prompt="SLEEP 30", timeout_seconds=0.2)])
        result = run_workflow(self.store, run_id)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["tasks"][0]["result"]["failure_kind"], "task_timeout")

    def test_queued_guidance_waits_for_next_agent_not_command(self):
        run_id = self.create([
            {"id": "cmd", "harness": "command", "argv": [sys.executable, "-c", "print('command evidence')"]},
            self.agent("agent"), self.agent("later"),
        ])
        request_control(self.store, run_id, "steer", "Add a regression check")
        first = run_workflow(self.store, run_id, max_launches=1)
        self.assertEqual(len(first["control"]["guidance"]), 1)
        final = run_workflow(self.store, run_id)
        calls = self.calls()
        self.assertIn("Add a regression check", calls[0]["prompt"])
        self.assertNotIn("Add a regression check", calls[1]["prompt"])
        self.assertEqual(final["control"]["guidance"], [])

    def test_guidance_during_task_is_not_inflight_steering(self):
        run_id = self.create([self.agent(prompt="SLEEP 0.3"), self.agent("next")])
        worker, outcome = self.background(run_id)
        self.wait_for(lambda: (self.project / "started").exists())
        request_control(self.store, run_id, "steer", "next-only guidance")
        worker.join(5)
        self.assertNotIn("exception", outcome)
        calls = self.calls()
        self.assertNotIn("next-only guidance", calls[0]["prompt"])
        self.assertIn("next-only guidance", calls[1]["prompt"])

    def test_stale_recovery_records_interruption_without_execution(self):
        run_id = self.create([self.agent()])
        def stale(state):
            state["status"] = "running"
            state["controller"] = {"pid": 999999999}
            state["tasks"][0]["status"] = "running"
            state["tasks"][0]["attempts"].append({"id": "old", "status": "running", "process_group": None})
        self.store.update(run_id, stale)
        with self.assertRaisesRegex(RuntimeError, "recover"):
            run_workflow(self.store, run_id)
        state = recover_run(self.store, run_id)
        self.assertEqual(state["status"], "interrupted")
        self.assertIsNone(state["controller"])
        self.assertEqual(state["tasks"][0]["attempts"][0]["failure_kind"], "stale_controller")
        self.assertEqual(self.calls(), [])
        with self.assertRaisesRegex(RuntimeError, "explicit retry"):
            run_workflow(self.store, run_id)
        retry_task(self.store, run_id, "first")
        self.assertEqual(run_workflow(self.store, run_id)["status"], "succeeded")

    def test_logs_private_and_summary_bounded_redacted(self):
        run_id = self.create([self.agent(prompt="SECRET")])
        state = run_workflow(self.store, run_id)
        task = state["tasks"][0]
        summary = task["result"]["summary"]
        self.assertLessEqual(len(summary), 2400)
        for secret in ["pretend-sensitive-token", "synthetic-key", "sk-secretvalue12345", "PRIVATE_REASONING_SENTINEL"]:
            self.assertNotIn(secret, json.dumps(state))
        attempt = task["attempts"][0]
        for key in ["stdout", "stderr", "final_text"]:
            path = self.store.directory(run_id) / attempt[key]
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn("pretend-sensitive-token", (self.store.directory(run_id) / attempt["final_text"]).read_text())
        self.assertNotIn("PRIVATE_REASONING_SENTINEL", (self.store.directory(run_id) / attempt["final_text"]).read_text())

    def test_launch_failure_is_durable_not_running(self):
        run_id = self.create([{"id": "missing", "harness": "command", "argv": [str(self.root / "does-not-exist")]}])
        state = run_workflow(self.store, run_id)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["tasks"][0]["result"]["failure_kind"], "launch_error")
        self.assertIn("No such file", state["tasks"][0]["result"]["summary"])
        self.assertIsNone(self.store.read(run_id)["controller"])

    def test_invalid_invocation_limits(self):
        run_id = self.create([self.agent()])
        for kwargs in [{"timeout_seconds": 0}, {"timeout_seconds": float("nan")}, {"max_launches": 0}, {"max_launches": True}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                run_workflow(self.store, run_id, **kwargs)

    def test_signal_preserves_interrupted_attempt(self):
        run_id = self.create([self.agent(prompt="SPAWN")])
        code = "from pathlib import Path;from agent_workbench.store import Store;from agent_workbench.runner import run_workflow;import sys;run_workflow(Store(Path(sys.argv[1])),sys.argv[2])"
        process = subprocess.Popen([sys.executable, "-c", code, str(self.store.root), run_id], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.wait_for(lambda: (self.project / "child.pid").exists())
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=6)
            state = self.store.read(run_id)
            self.assertEqual(state["status"], "interrupted")
            self.assertEqual(state["tasks"][0]["result"]["failure_kind"], "signal")
            self.assertIsNone(state["controller"])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == "__main__":
    unittest.main()
