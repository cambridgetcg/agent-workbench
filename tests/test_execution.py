"""Integration boundaries exercised through real, local fake native processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agent_workbench import capabilities
from agent_workbench.bridge import request_native
from agent_workbench.runner import recover_run, request_control, run_workflow
from agent_workbench.store import Store, now
from agent_workbench.views import timeline_view
from agent_workbench.workspaces import remove_clean


FAKE_NATIVE = r'''
import json, os, pathlib, signal, subprocess, sys, time
if "--version" in sys.argv:
    print("Claude Code 2.1.282")
    sys.exit(0)
if "--help" in sys.argv:
    print("--input-format stream-json --output-format stream-json --replay-user-messages --resume")
    sys.exit(0)
mode = os.environ.get("WORKBENCH_TEST_MODE", "simple")
record = pathlib.Path(os.environ["WORKBENCH_TEST_RECORD"])
def save(frame):
    with record.open("a") as stream:
        stream.write(json.dumps(frame) + "\n")
        stream.flush()
def emit(frame):
    print(json.dumps(frame), flush=True)
def result(number, interrupted=False):
    identity = {"user_message_uuid": user_ids[number - 1]} if "user_ids" in globals() and 0 < number <= len(user_ids) else {}
    emit({"type": "result", "uuid": "result-" + str(number), "subtype": "success", "is_error": False,
          "session_id": "fixture-session", "result": "fixture-complete", "num_turns": 1,
          "terminal_reason": "aborted_streaming" if interrupted else "completed", **identity})
def public_events():
    emit({"type": "system", "subtype": "init", "session_id": "fixture-session"})
    emit({"type": "assistant", "session_id": "fixture-session", "parent_tool_use_id": "parent-tool",
          "message": {"role": "assistant", "content": [
              {"type": "thinking", "thinking": "PRIVATE_THINKING_SENTINEL"},
              {"type": "tool_use", "id": "tool-17", "name": "Bash", "input": {"command": "PRIVATE_COMMAND_SENTINEL"}},
              {"type": "text", "text": "PRIVATE_TEXT_SENTINEL sk-fixturesecret12345"}]}})
if mode in ("process", "process-child"):
    save({"prompt": sys.stdin.read(), "cwd": os.getcwd()})
    public_events()
    if mode == "process-child":
        child = subprocess.Popen([sys.executable, "-c", "import os,time\nwhile True:\n os.write(1,b'{\"type\":\"keepalive\"}\\n');time.sleep(.01)"])
        save({"child_pid": child.pid})
        result(1)
        sys.exit(0)
    gate = pathlib.Path(os.environ["WORKBENCH_TEST_GATE"])
    while not gate.exists():
        time.sleep(0.01)
    result(1)
    sys.exit(0)
signal.signal(signal.SIGTERM, lambda *_: None if mode in ("permission", "unknown") else sys.exit(0))
initialized = False
users = 0
user_ids = []
for line in sys.stdin:
    frame = json.loads(line)
    save(frame)
    if frame["type"] == "control_request":
        action = frame["request"]["subtype"]
        if action == "initialize":
            if mode == "handshake_wait":
                time.sleep(5)
            if mode == "refuse":
                emit({"type": "control_response", "response": {"subtype": "error", "request_id": frame["request_id"], "error": "fixture refusal"}})
                break
            emit({"type": "control_response", "response": {"subtype": "success", "request_id": frame["request_id"], "response": {}}})
            initialized = True
        elif action == "interrupt":
            if mode == "late_interrupt":
                result(users)
                time.sleep(0.75)
            emit({"type": "control_response", "response": {"subtype": "success", "request_id": frame["request_id"], "response": {}}})
            if mode != "late_interrupt":
                result(1 if mode == "interrupt-partial" else users, interrupted=True)
            break
    elif frame["type"] == "user":
        if not initialized:
            save({"violation": "prompt_before_initialize_ack"})
            sys.exit(9)
        users += 1
        user_ids.append(frame["uuid"])
        if mode == "loss" and users == 2:
            sys.exit(0)
        emit({"type": "user", "uuid": frame["uuid"], "session_id": "fixture-session", "parent_tool_use_id": None,
              "message": {"role": "user", "content": "PRIVATE_ECHO_SENTINEL"}})
        if users == 1:
            public_events()
        if mode == "backpressure":
            if users == 1:
                gate = pathlib.Path(os.environ["WORKBENCH_TEST_GATE"])
                while not gate.exists():
                    time.sleep(.01)
            # More than a pipe buffer of output before reading the next input.
            emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "private" * 32000}]}})
            result(users)
            if users == 21:
                break
            continue
        if mode in ("permission", "unknown"):
            request = {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "PRIVATE_PERMISSION_SENTINEL"}} if mode == "permission" else {"subtype": "future_callback", "private": "PRIVATE_CALLBACK_SENTINEL"}
            emit({"type": "control_request", "request_id": "callback-1", "request": request})
            continue
        if mode in ("conversation", "loss"):
            if users == 2:
                result(1)
                result(2)
                break
            continue
        if mode == "partial-success":
            if users == 2:
                result(1)
                break
            continue
        if mode == "coalesced":
            if users == 2:
                emit({"type": "result", "uuid": "merged-result", "subtype": "success", "is_error": False,
                      "session_id": "fixture-session", "result": "fixture-complete",
                      "user_message_uuid": user_ids[-1], "user_message_uuids": user_ids, "queued_turn_count": 0})
                emit({"type": "system", "subtype": "session_state_changed", "state": "idle", "session_id": "fixture-session"})
            # Native stream-json awaits stdin EOF after the one merged turn.
            continue
        if mode in ("interrupt", "late_interrupt", "interrupt-partial"):
            continue
        if mode == "active-task-eof":
            emit({"type": "system", "subtype": "task_started", "task_id": "unfinished-agent", "task_type": "local_agent"})
        result(1)
        if mode == "truncated-final":
            sys.stdout.write('{"type":"keepalive"}')
            sys.stdout.flush()
        if mode == "closed":
            continue
        break
    elif frame["type"] == "control_response":
        save({"callback_response_received": True})
        break
if mode == "closed":
    time.sleep(0.25)
'''


class ExecutionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.native = self.bin / "claude"
        self.native.write_text(f"#!{sys.executable}\n" + FAKE_NATIVE)
        self.native.chmod(0o700)
        self.record = self.root / "frames.jsonl"
        self.gate = self.root / "gate"
        self.environment = patch.dict(os.environ, {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude"), "CODEX_HOME": str(self.home / ".codex"),
            "WORKBENCH_TEST_MODE": "simple", "WORKBENCH_TEST_RECORD": str(self.record),
            "WORKBENCH_TEST_GATE": str(self.gate),
        })
        self.environment.start()
        self.store = Store(self.root / "state")
        self.run_ids = []
        self.workers = []

    def tearDown(self):
        self.gate.touch()
        for run_id in self.run_ids:
            try:
                request_control(self.store, run_id, "stop")
            except (OSError, ValueError):
                pass
        for worker, _ in self.workers:
            worker.join(5)
        self.environment.stop()
        self.temp.cleanup()

    def create(self, **overrides):
        task = {"id": "work", "harness": "claude", "adapter": "duplex", "prompt": "PRIVATE_INITIAL_PROMPT", **overrides}
        if task["harness"] == "command":
            task.pop("prompt", None)
        run_id = self.store.create({"version": 1, "name": "Fixture integration", "goal": "Verify lifecycle contracts", "tasks": [task]}, self.project)["id"]
        self.run_ids.append(run_id)
        return run_id

    def start(self, run_id, **kwargs):
        outcome = {}
        def run():
            try:
                outcome["state"] = run_workflow(self.store, run_id, timeout_seconds=8, **kwargs)
            except BaseException as exc:
                outcome["error"] = exc
        worker = threading.Thread(target=run)
        worker.start()
        self.workers.append((worker, outcome))
        return worker, outcome

    def wait(self, condition, description="condition", seconds=4):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            value = condition()
            if value:
                return value
            time.sleep(0.01)
        self.fail("Timed out waiting for " + description)

    def initialized(self, run_id):
        state = self.store.read(run_id)
        attempts = state["tasks"][0]["attempts"]
        return attempts and attempts[-1].get("duplex_initialized")

    def finish(self, worker, outcome):
        worker.join(5)
        self.assertFalse(worker.is_alive(), "Fixture controller did not finish")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["state"]

    def frames(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()] if self.record.exists() else []

    def test_duplex_handshake_live_input_receipt_and_launch_evidence(self):
        os.environ["WORKBENCH_TEST_MODE"] = "conversation"
        (self.home / ".claude").mkdir()
        (self.home / ".claude/settings.json").write_text(json.dumps({"model": "opus", "env": {"PRIVATE_VALUE": "PRIVATE_SETTINGS_SENTINEL"}}))
        run_id = self.create()
        worker, outcome = self.start(run_id)
        self.wait(lambda: self.initialized(run_id), "duplex initialization")
        receipt = request_native(self.store, run_id, "work", "input", "PRIVATE_FOLLOWUP_PROMPT", "followup-1")
        self.assertEqual(receipt["status"], "queued")
        self.assertNotIn("text", receipt)
        state = self.finish(worker, outcome)
        self.assertEqual(state["status"], "succeeded")
        frames = self.frames()
        self.assertEqual(frames[0]["request"]["subtype"], "initialize")
        users = [frame for frame in frames if frame.get("type") == "user"]
        self.assertEqual(len(users), 2)
        self.assertIn("PRIVATE_INITIAL_PROMPT", users[0]["message"]["content"])
        self.assertEqual(users[1]["message"]["content"], "PRIVATE_FOLLOWUP_PROMPT")
        self.assertNotEqual(users[0]["uuid"], users[1]["uuid"])
        request = state["control"]["native_requests"][0]
        self.assertEqual(request["status"], "acknowledged")
        self.assertNotIn("applied", request)
        attempt = state["tasks"][0]["attempts"][0]
        self.assertEqual(attempt["launch"]["executable"], str(self.native))
        self.assertFalse(attempt["launch"]["capabilities"]["stream_input"]["runtime_verified"])
        self.assertNotIn("configuration", attempt["launch"])
        profile_path = self.store.directory(run_id) / attempt["launch_profile"]
        profile = json.loads(profile_path.read_text())
        self.assertEqual(profile["configuration"]["claude"]["inferred_selected"]["model"]["value"], "opus")
        self.assertNotIn("PRIVATE_SETTINGS_SENTINEL", json.dumps(profile))
        self.assertEqual(profile_path.stat().st_mode & 0o777, 0o600)

    def test_interrupt_acknowledgment_does_not_claim_applied_or_success(self):
        os.environ["WORKBENCH_TEST_MODE"] = "interrupt"
        run_id = self.create()
        worker, outcome = self.start(run_id)
        self.wait(lambda: self.initialized(run_id), "duplex initialization")
        request_native(self.store, run_id, "work", "interrupt", request_id="interrupt-1")
        state = self.finish(worker, outcome)
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["tasks"][0]["result"]["failure_kind"], "native_turn_interrupt_acknowledged")
        self.assertEqual(state["control"]["native_requests"][0]["status"], "acknowledged")
        self.assertNotIn("applied", json.dumps(state["tasks"][0]["attempts"][0]["control_receipts"]))

    def test_permission_and_unknown_callbacks_never_grant(self):
        for mode in ("permission", "unknown"):
            with self.subTest(mode=mode):
                self.record.unlink(missing_ok=True)
                os.environ["WORKBENCH_TEST_MODE"] = mode
                run_id = self.create()
                state = run_workflow(self.store, run_id, timeout_seconds=5)
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["tasks"][0]["result"]["failure_kind"], "native_decision_required")
                responses = [frame["response"] for frame in self.frames() if frame.get("type") == "control_response"]
                self.assertEqual(len(responses), 1)
                if mode == "permission":
                    self.assertEqual(responses[0]["response"]["behavior"], "deny")
                else:
                    self.assertEqual(responses[0]["subtype"], "error")
                self.assertNotIn('"allow"', json.dumps(responses))

    def test_late_interrupt_ack_is_observed_even_after_turn_drain(self):
        os.environ["WORKBENCH_TEST_MODE"] = "late_interrupt"
        run_id = self.create()
        worker, outcome = self.start(run_id)
        self.wait(lambda: self.initialized(run_id), "duplex initialization")
        request_native(self.store, run_id, "work", "interrupt", request_id="late-interrupt-1")
        state = self.finish(worker, outcome)
        self.assertEqual(state["control"]["native_requests"][0]["status"], "acknowledged")
        self.assertEqual(state["status"], "interrupted")

    def test_one_coalesced_result_drains_multiple_live_inputs(self):
        os.environ["WORKBENCH_TEST_MODE"] = "coalesced"
        run_id = self.create(timeout_seconds=3)
        worker, outcome = self.start(run_id)
        self.wait(lambda: self.initialized(run_id), "duplex initialization")
        request_native(self.store, run_id, "work", "input", "coalesced-followup", "merged-1")
        state = self.finish(worker, outcome)
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(state["control"]["native_requests"][0]["status"], "acknowledged")
        self.assertEqual(sum(frame.get("type") == "user" for frame in self.frames()), 2)
        results = [event for event in state["events"] if event.get("kind") == "native_turn"]
        self.assertEqual(len(results), 1)

    def test_large_live_input_batch_drains_stdout_while_writing(self):
        os.environ["WORKBENCH_TEST_MODE"] = "backpressure"
        run_id = self.create(timeout_seconds=12)
        worker, outcome = self.start(run_id)
        self.wait(lambda: self.initialized(run_id), "duplex initialization")
        for index in range(20):
            request_native(self.store, run_id, "work", "input", "x" * 16000, "pressure-" + str(index))
        self.gate.touch()
        state = self.finish(worker, outcome)
        self.assertEqual(state["status"], "succeeded", state["tasks"][0].get("result"))
        self.assertEqual(len(state["control"]["native_requests"]), 20)
        self.assertTrue(all(request["status"] == "acknowledged" for request in state["control"]["native_requests"]))
        self.assertEqual(sum(frame.get("type") == "user" for frame in self.frames()), 21)

    def test_native_descendants_stop_before_final_event_drain(self):
        from agent_workbench.bridge import EventTail
        os.environ["WORKBENCH_TEST_MODE"] = "process-child"
        original = EventTail.poll
        final_checks = []
        def checked(tail, final=False):
            if final:
                pid = next(frame["child_pid"] for frame in self.frames() if "child_pid" in frame)
                result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
                alive = result.returncode == 0 and result.stdout.strip() and not result.stdout.strip().startswith("Z")
                self.assertFalse(alive, "Inherited stdout writer must stop before final draining")
                final_checks.append(True)
            return original(tail, final=final)
        with patch.object(EventTail, "poll", checked):
            state = run_workflow(self.store, self.create(adapter="process"), timeout_seconds=5)
        self.assertEqual(state["status"], "succeeded", state["tasks"][0].get("result"))
        self.assertTrue(final_checks)

    def test_final_event_drain_honors_new_stop_request(self):
        from agent_workbench.bridge import EventTail
        os.environ["WORKBENCH_TEST_MODE"] = "process-child"
        run_id = self.create(adapter="process")
        original = EventTail.poll
        final_polls = []
        def still_growing(tail, final=False):
            if final:
                final_polls.append(True)
                request_control(self.store, run_id, "stop")
                return True
            return original(tail, final=final)
        with patch.object(EventTail, "poll", still_growing):
            state = run_workflow(self.store, run_id, timeout_seconds=5)
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(len(final_polls), 1)

    def test_initialize_refusal_never_sends_the_task_prompt(self):
        os.environ["WORKBENCH_TEST_MODE"] = "refuse"
        state = run_workflow(self.store, self.create(), timeout_seconds=5)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["tasks"][0]["result"]["failure_kind"], "initialize_rejected")
        self.assertFalse(any(frame.get("type") == "user" for frame in self.frames()))

    def test_handshake_timeout_never_sends_the_task_prompt(self):
        os.environ["WORKBENCH_TEST_MODE"] = "handshake_wait"
        state = run_workflow(self.store, self.create(timeout_seconds=0.4), timeout_seconds=5)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["tasks"][0]["result"]["failure_kind"], "task_timeout")
        self.assertFalse(any(frame.get("type") == "user" for frame in self.frames()))

    def test_lost_echo_remains_uncertain_and_is_not_replayed(self):
        os.environ["WORKBENCH_TEST_MODE"] = "loss"
        run_id = self.create()
        worker, outcome = self.start(run_id)
        self.wait(lambda: self.initialized(run_id), "duplex initialization")
        request_native(self.store, run_id, "work", "input", "uncertain-followup", "lost-1")
        state = self.finish(worker, outcome)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["control"]["native_requests"][0]["status"], "unacknowledged")
        before = self.frames()
        with self.assertRaisesRegex(RuntimeError, "retry"):
            run_workflow(self.store, run_id, timeout_seconds=5)
        self.assertEqual(self.frames(), before)
        self.assertEqual(sum(frame.get("type") == "user" for frame in before), 2)

    def test_successful_exit_with_unanswered_live_input_is_not_success(self):
        os.environ["WORKBENCH_TEST_MODE"] = "partial-success"
        run_id = self.create()
        worker, outcome = self.start(run_id)
        self.wait(lambda: self.initialized(run_id), "duplex initialization")
        request_native(self.store, run_id, "work", "input", "unanswered-followup", "unanswered-1")
        state = self.finish(worker, outcome)
        self.assertEqual(state["control"]["native_requests"][0]["status"], "acknowledged")
        self.assertEqual(state["tasks"][0]["result"]["exit_code"], 0)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["tasks"][0]["result"]["failure_kind"], "native_input_completion_unverified")
        self.assertEqual(sum(frame.get("type") == "user" for frame in self.frames()), 2)

    def test_successful_exit_with_unfinished_native_agent_is_not_success(self):
        os.environ["WORKBENCH_TEST_MODE"] = "active-task-eof"
        state = run_workflow(self.store, self.create(), timeout_seconds=5)
        self.assertEqual(state["tasks"][0]["result"]["exit_code"], 0)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["tasks"][0]["result"]["failure_kind"], "native_activity_completion_unverified")

    def test_incomplete_final_frame_cannot_record_success(self):
        os.environ["WORKBENCH_TEST_MODE"] = "truncated-final"
        state = run_workflow(self.store, self.create(), timeout_seconds=5)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["tasks"][0]["result"]["failure_kind"], "incomplete_json_frame")
        self.assertTrue(any(event.get("code") == "incomplete_json_frame" for event in state["events"]))

    def test_acknowledged_interrupt_precedes_unanswered_input_at_eof(self):
        os.environ["WORKBENCH_TEST_MODE"] = "interrupt-partial"
        run_id = self.create()
        worker, outcome = self.start(run_id)
        self.wait(lambda: self.initialized(run_id), "duplex initialization")
        request_native(self.store, run_id, "work", "input", "interrupted-followup", "interrupted-input")
        request_native(self.store, run_id, "work", "interrupt", request_id="partial-interrupt")
        state = self.finish(worker, outcome)
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["tasks"][0]["result"]["failure_kind"], "native_turn_interrupt_acknowledged")
        requests = {request["id"]: request for request in state["control"]["native_requests"]}
        self.assertEqual(requests["partial-interrupt"]["status"], "acknowledged")
        self.assertEqual(sum(frame.get("type") == "user" for frame in self.frames()), 2)

    def test_closed_input_refuses_late_queue_entries(self):
        os.environ["WORKBENCH_TEST_MODE"] = "closed"
        run_id = self.create()
        worker, outcome = self.start(run_id)
        self.wait(lambda: self.store.read(run_id)["tasks"][0]["attempts"] and self.store.read(run_id)["tasks"][0]["attempts"][-1].get("input_closed"), "closed native input")
        with self.assertRaisesRegex(ValueError, "not open"):
            request_native(self.store, run_id, "work", "input", "too late", "late-1")
        state = self.finish(worker, outcome)
        self.assertEqual(state["status"], "succeeded")

    def test_events_are_visible_before_completion_without_private_payloads(self):
        os.environ["WORKBENCH_TEST_MODE"] = "process"
        run_id = self.create(adapter="process")
        worker, outcome = self.start(run_id)
        state = self.wait(lambda: (state if any(event.get("kind") == "native_tool" for event in state["events"]) else None)
                          if (state := self.store.read(run_id)) else None, "durable native events")
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["tasks"][0]["status"], "running")
        events = state["events"]
        tool = next(event for event in events if event.get("kind") == "native_tool")
        self.assertEqual(tool["tool"], "Bash")
        self.assertEqual(tool["parent_tool_use_id"], "parent-tool")
        timeline = timeline_view(state, limit=100)
        visible = next(event for event in timeline["entries"] if event.get("kind") == "native_tool")
        self.assertEqual(visible["tool"], "Bash")
        self.assertEqual(visible["tool_use_id"], "tool-17")
        rendered = json.dumps(events) + json.dumps(timeline)
        for private in ("PRIVATE_THINKING_SENTINEL", "PRIVATE_COMMAND_SENTINEL", "PRIVATE_TEXT_SENTINEL", "sk-fixturesecret12345"):
            self.assertNotIn(private, rendered)
        self.gate.touch()
        self.assertEqual(self.finish(worker, outcome)["status"], "succeeded")

    def test_timeout_during_profile_collection_never_launches_task(self):
        inspect = capabilities.inspect_launch
        def slow_profile(*args, **kwargs):
            time.sleep(0.08)
            return inspect(*args, **kwargs)
        with patch.object(capabilities, "inspect_launch", side_effect=slow_profile):
            state = run_workflow(self.store, self.create(timeout_seconds=0.03), timeout_seconds=5)
        attempt = state["tasks"][0]["attempts"][0]
        self.assertEqual(state["status"], "failed")
        self.assertEqual(attempt["failure_kind"], "task_timeout")
        self.assertIsNone(attempt["pid"])
        self.assertEqual(self.frames(), [])

    def test_checkout_contention_after_attempt_creation_finishes_that_attempt(self):
        from agent_workbench.workspaces import WorkspaceBusy
        with patch("agent_workbench.workspaces.prepare", side_effect=WorkspaceBusy("fixture contention")):
            state = run_workflow(self.store, self.create(workspace="worktree"), timeout_seconds=5)
        self.assertEqual(state["status"], "failed")
        task = state["tasks"][0]
        self.assertEqual(task["status"], "failed")
        self.assertEqual(len(task["attempts"]), 1)
        self.assertEqual(task["attempts"][0]["failure_kind"], "workspace_busy_after_attempt")
        self.assertEqual(self.frames(), [])

    def test_recovery_closes_delivery_receipts_without_replaying(self):
        run_id = self.create()
        def stale(state):
            state["status"] = "running"
            state["controller"] = {"pid": 999999999}
            task = state["tasks"][0]
            task["status"] = "running"
            task["attempts"] = [{"id": "stale-attempt", "status": "running", "started_at": now(), "pid": None,
                                 "process_group": None, "duplex_initialized": True}]
            state["control"]["native_requests"] = [{"id": status, "task_id": "work", "attempt_id": "stale-attempt",
                                                     "action": "input", "status": status, "text": "private-input"}
                                                    for status in ("queued", "dispatching", "sent", "acknowledged")]
        self.store.update(run_id, stale)
        state = recover_run(self.store, run_id)
        self.assertEqual(state["status"], "interrupted")
        statuses = {entry["id"]: entry["status"] for entry in state["control"]["native_requests"]}
        self.assertEqual(statuses, {"queued": "not_sent", "dispatching": "unacknowledged", "sent": "unacknowledged", "acknowledged": "acknowledged"})
        self.assertTrue(state["tasks"][0]["attempts"][0]["input_closed"])
        self.assertEqual(self.frames(), [])

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.project), *args], capture_output=True, text=True, check=True).stdout.strip()

    def repository(self):
        self.git("init", "-q")
        (self.project / "tracked.txt").write_text("committed\n")
        self.git("add", "tracked.txt")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture snapshot")

    def test_worktree_uses_committed_snapshot_retains_changes_and_protects_cleanup(self):
        self.repository()
        (self.project / "tracked.txt").write_text("uncommitted source\n")
        (self.project / "untracked.txt").write_text("source only\n")
        code = "from pathlib import Path; assert Path('tracked.txt').read_text() == 'committed\\n'; assert not Path('untracked.txt').exists(); Path('result.txt').write_text('retained result')"
        run_id = self.create(harness="command", adapter="process", workspace="worktree", prompt=None, argv=[sys.executable, "-c", code])
        state = run_workflow(self.store, run_id, timeout_seconds=10)
        self.assertEqual(state["status"], "succeeded")
        attempt = state["tasks"][0]["attempts"][0]
        workspace = attempt["workspace"]
        self.assertTrue(workspace["source_dirty"])
        self.assertTrue(workspace["retained"])
        self.assertFalse(workspace["sandboxed"])
        root = Path(workspace["root"])
        self.assertEqual((root / "result.txt").read_text(), "retained result")
        with self.assertRaisesRegex(ValueError, "changes or new commits"):
            remove_clean(self.store, run_id, "work", attempt["id"])
        (root / "result.txt").unlink()
        state = remove_clean(self.store, run_id, "work", attempt["id"])
        self.assertFalse(root.exists())
        self.assertFalse(state["tasks"][0]["attempts"][0]["workspace"]["retained"])
        self.assertEqual((self.project / "tracked.txt").read_text(), "uncommitted source\n")

    def test_worktree_cleanup_protects_detached_commits(self):
        self.repository()
        run_id = self.create(harness="command", adapter="process", workspace="worktree", prompt=None, argv=[sys.executable, "-c", "pass"])
        state = run_workflow(self.store, run_id, timeout_seconds=10)
        attempt = state["tasks"][0]["attempts"][0]
        root = Path(attempt["workspace"]["root"])
        (root / "tracked.txt").write_text("new committed result\n")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qam", "retained result", cwd=root)
        self.assertEqual(self.git("status", "--porcelain", cwd=root), "")
        with self.assertRaisesRegex(ValueError, "changes or new commits"):
            remove_clean(self.store, run_id, "work", attempt["id"])
        self.assertTrue(root.is_dir())

    def test_worktree_relative_executable_comes_from_committed_snapshot(self):
        self.repository()
        script = self.project / "job"
        script.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath('executed.txt').write_text('committed executable')\n")
        script.chmod(0o700)
        self.git("add", "job")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "add executable")
        script.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath('executed.txt').write_text('uncommitted executable')\n")
        run_id = self.create(harness="command", adapter="process", workspace="worktree", argv=["./job"])
        state = run_workflow(self.store, run_id, timeout_seconds=10)
        self.assertEqual(state["status"], "succeeded")
        attempt = state["tasks"][0]["attempts"][0]
        root = Path(attempt["workspace"]["root"])
        self.assertEqual((root / "executed.txt").read_text(), "committed executable")
        self.assertEqual(Path(attempt["launch"]["executable"]), root / "job")
        self.assertFalse((self.project / "executed.txt").exists())


if __name__ == "__main__":
    unittest.main()
