import json
import os
import selectors
import subprocess
import sys
import time
import unittest

from agent_workbench.native import NativeProtocolError, NativeSession, duplex_arguments, normalize_event


def wire(frame):
    return (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")


def acknowledgment(request_id, subtype="success", **extra):
    return {"type": "control_response", "response": {
        "subtype": subtype, "request_id": request_id, "response": {}, **extra}}


class NativeSessionTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.events = []
        self.session = NativeSession(self.sent.append, self.events.append)

    def initialize(self):
        self.session.initialize("initialize-1")
        self.session.feed(wire(acknowledgment("initialize-1")))

    def test_initialize_ack_is_required_and_correlated(self):
        self.session.initialize("initialize-1")
        self.assertFalse(self.session.initialized)
        with self.assertRaisesRegex(NativeProtocolError, "initialize_not_acknowledged"):
            self.session.send_user("hello", "input-1")
        self.session.feed(wire(acknowledgment("unrelated")))
        self.assertFalse(self.session.initialized)
        self.session.feed(wire(acknowledgment("initialize-1")))
        self.assertTrue(self.session.initialized)
        self.assertEqual(json.loads(self.sent[0]), {
            "type": "control_request", "request_id": "initialize-1",
            "request": {"subtype": "initialize", "hooks": None}})
        self.assertEqual(self.session.requests["initialize-1"]["status"], "acknowledged")

    def test_rejected_initialize_has_no_native_error_text(self):
        self.session.initialize("init")
        self.session.feed(wire(acknowledgment("init", "error", error="Bearer PRIVATE api_key=secret")))
        self.assertFalse(self.session.initialized)
        self.assertEqual(self.session.protocol_error, "initialize_rejected")
        self.assertNotIn("PRIVATE", json.dumps(self.events))

    def test_initialize_accepts_null_payload_and_rejects_arbitrary_payload(self):
        self.session.initialize("init")
        self.session.feed(wire(acknowledgment("init", response=None)))
        self.assertTrue(self.session.initialized)
        session = NativeSession(lambda _: None)
        session.initialize("init")
        session.feed(wire(acknowledgment("init", response="PRIVATE")))
        self.assertFalse(session.initialized)
        self.assertEqual(session.protocol_error, "invalid_control_response")

    def test_request_namespace_collisions_are_rejected(self):
        self.initialize()
        self.session.send_user("hello", "input-1")
        wire_id = self.session.requests["input-1"]["wire_request_id"]
        with self.assertRaisesRegex(NativeProtocolError, "wire_request_id_already_sent"):
            self.session.interrupt(wire_id)

    def test_chunked_unicode_frames_and_multiple_lines(self):
        self.session.initialize("init")
        raw = wire({"type": "system", "subtype": "init", "session_id": "native-session", "cwd": "/private/person"})
        raw += wire({"type": "assistant", "message": {"content": [{"type": "text", "text": "你好"}]}})
        raw += wire(acknowledgment("init"))
        for byte in raw:
            self.session.feed(bytes([byte]))
        self.assertTrue(self.session.initialized)
        self.assertEqual(self.session.native_session_id, "native-session")
        self.assertNotIn("你好", json.dumps(self.events))
        self.assertNotIn("private", json.dumps(self.events))

    def test_user_receipt_matches_echo_not_control_response_or_subagent(self):
        self.initialize()
        self.session.send_user("Private prompt", "input-1")
        outgoing = json.loads(self.sent[-1])
        self.assertNotIn("origin", outgoing)
        self.assertNotIn("client_composed", outgoing)
        self.assertEqual(outgoing["message"]["content"], "Private prompt")
        self.assertEqual(self.session.sent_turns, 1)
        self.session.feed(wire(acknowledgment(outgoing["uuid"])))
        self.assertEqual(self.session.requests["input-1"]["status"], "sent")
        self.session.feed(wire({**outgoing, "parent_tool_use_id": "tool-1"}))
        self.assertEqual(self.session.requests["input-1"]["status"], "sent")
        self.session.feed(wire(outgoing))
        self.assertEqual(self.session.requests["input-1"]["status"], "acknowledged")
        self.assertNotIn("Private prompt", json.dumps(self.events))
        self.assertNotIn("applied", json.dumps(self.events))

    def test_interrupt_receipt_and_no_automatic_retry(self):
        self.initialize()
        self.session.interrupt("stop-turn")
        self.assertEqual(json.loads(self.sent[-1])["request"], {"subtype": "interrupt"})
        self.session.feed(wire(acknowledgment("stop-turn")))
        self.assertEqual(self.session.requests["stop-turn"]["status"], "acknowledged")
        self.assertIsNone(self.session.last_result_success)
        with self.assertRaisesRegex(NativeProtocolError, "request_already_sent"):
            self.session.interrupt("stop-turn")

    def test_expiration_preserves_uncertainty_and_ignores_late_receipt(self):
        self.initialize()
        self.session.interrupt("stop-turn")
        self.session.expire("stop-turn")
        self.session.feed(wire(acknowledgment("stop-turn")))
        self.assertEqual(self.session.requests["stop-turn"]["status"], "unacknowledged")
        self.assertIsNone(self.session.protocol_error)

    def test_input_eof_does_not_claim_delivery_or_retry(self):
        self.initialize()
        self.session.send_user("hello", "input-1")
        self.session.finish()
        self.assertEqual(self.session.requests["input-1"]["status"], "unacknowledged")
        with self.assertRaises(NativeProtocolError):
            self.session.send_user("hello", "input-2")

    def test_permission_decision_is_denied_and_blocks(self):
        self.initialize()
        self.session.feed(wire({"type": "control_request", "request_id": "native-permission",
                                "request": {"subtype": "can_use_tool", "tool_name": "Bash",
                                            "input": {"command": "PRIVATE secret"}, "blocked_path": "/private/account"}}))
        decision = json.loads(self.sent[-1])["response"]["response"]
        self.assertEqual(decision["behavior"], "deny")
        self.assertTrue(decision["interrupt"])
        self.assertEqual(self.session.protocol_error, "native_decision_required")
        self.assertTrue(self.session.terminal_error)
        self.assertNotIn("PRIVATE", json.dumps(self.events))
        self.assertNotIn("account", json.dumps(self.events))

    def test_unregistered_hook_and_mcp_are_never_allowed(self):
        for subtype in ("hook_callback", "mcp_message", "unexpected"):
            with self.subTest(subtype=subtype):
                sent, events = [], []
                session = NativeSession(sent.append, events.append)
                session.feed(wire({"type": "control_request", "request_id": "callback-1",
                                   "request": {"subtype": subtype, "input": "PRIVATE"}}))
                self.assertEqual(json.loads(sent[-1])["response"]["subtype"], "error")
                self.assertEqual(session.protocol_error, "native_decision_required")
                self.assertNotIn("PRIVATE", json.dumps(events))

    def test_bounded_frames_and_invalid_json_are_content_free(self):
        cases = [
            (b"x" * 129, "incoming_frame_too_large"),
            (b'{"type":"assistant","private":"PRIVATE"\n', "invalid_json_frame"),
            (b'{"type":[]}\n', "invalid_frame_shape"),
            (b'{"type":"foo","x":NaN}\n', "invalid_json_frame"),
            (b'{"type":"foo","x":"\xff"}\n', "invalid_json_frame"),
        ]
        for raw, code in cases:
            with self.subTest(code=code):
                events = []
                session = NativeSession(lambda _: None, events.append, max_frame_bytes=128)
                session.feed(raw)
                self.assertEqual(session.protocol_error, code)
                self.assertNotIn("PRIVATE", json.dumps(events))
                self.assertLessEqual(len(session._buffer), 128)

    def test_incomplete_frame_at_eof_is_not_discarded_silently(self):
        self.session.feed(b'{"type":"assistant"}')
        self.session.finish()
        self.assertEqual(self.session.protocol_error, "incomplete_json_frame")

    def test_write_failure_retains_unknown_delivery(self):
        def fail(_):
            raise BrokenPipeError("PRIVATE provider/account")
        session = NativeSession(fail, self.events.append)
        with self.assertRaisesRegex(NativeProtocolError, "write_failed_delivery_unknown"):
            session.initialize("init")
        session.finish()
        self.assertEqual(session.requests["init"]["status"], "unacknowledged")
        self.assertNotIn("PRIVATE", json.dumps(self.events))

    def test_task_ledger_ignores_indefinite_shells_and_handles_terminal_patch(self):
        for task_id, task_type in (("agent-1", "local_agent"), ("flow-1", "local_workflow"), ("shell-1", "local_bash")):
            self.session.feed(wire({"type": "system", "subtype": "task_started", "task_id": task_id, "task_type": task_type}))
        self.assertEqual(self.session.active_tasks, {"agent-1", "flow-1"})
        self.session.feed(wire({"type": "result", "subtype": "success", "is_error": False}))
        self.assertEqual(self.session.completed_turns, 1)
        self.assertTrue(self.session.active_tasks)
        self.session.feed(wire({"type": "system", "subtype": "task_updated", "task_id": "agent-1", "patch": {"status": "killed"}}))
        self.session.feed(wire({"type": "system", "subtype": "task_notification", "task_id": "flow-1", "status": "completed"}))
        self.assertFalse(self.session.active_tasks)

    def test_malformed_task_status_and_deep_unknown_frames_are_safe(self):
        self.session.feed(wire({"type": "system", "subtype": "task_updated", "task_id": "agent-1", "patch": {"status": []}}))
        self.assertIsNone(self.session.protocol_error)
        self.session.feed(b'{"type":"unknown","nested":' + b'[' * 3000 + b'0' + b']' * 3000 + b'}\n')
        # Python versions differ in decoder depth support: safely ignore a
        # decoded unknown event, or explicitly report a recursion-bound parse.
        self.assertIn(self.session.protocol_error, (None, "invalid_json_frame"))

    def test_injected_results_do_not_satisfy_sent_turn_count_and_failure_is_sticky(self):
        self.session.feed(wire({"type": "result", "subtype": "error_during_execution", "is_error": True, "uuid": "result-1"}))
        for _ in range(2):
            self.session.feed(wire({"type": "result", "subtype": "success", "is_error": False, "uuid": "result-2", "origin": {"kind": "task-notification"}}))
        self.assertEqual(self.session.completed_turns, 1)
        self.assertTrue(self.session.last_result_success)
        self.assertTrue(self.session.terminal_error)

    def test_interrupted_result_is_not_success(self):
        self.session.feed(wire({"type": "result", "subtype": "success", "is_error": False, "terminal_reason": "aborted_tools"}))
        self.assertFalse(self.session.last_result_success)
        self.assertTrue(self.session.terminal_error)
        self.assertEqual(self.events[-1]["status"], "interrupted")

    def send_acknowledged(self, request_id):
        self.session.send_user("private input", request_id)
        outgoing = json.loads(self.sent[-1])
        self.session.feed(wire(outgoing))
        return outgoing["uuid"]

    def result(self, **fields):
        self.session.feed(wire({"type": "result", "subtype": "success", "is_error": False, **fields}))

    def test_one_result_can_cover_several_coalesced_inputs(self):
        self.initialize()
        first = self.send_acknowledged("input-1")
        second = self.send_acknowledged("input-2")
        self.assertFalse(self.session.input_drained)
        self.result(user_message_uuid=second, user_message_uuids=[first, second], queued_turn_count=0)
        self.assertEqual(self.session.sent_turns, 2)
        self.assertEqual(self.session.completed_turns, 1)
        self.assertTrue(self.session.input_drained)
        self.assertEqual(self.session.input_drain_evidence, "result_prompt_uuid_receipts")
        self.assertTrue(all(self.session.requests[key]["status"] == "acknowledged" for key in ("input-1", "input-2")))

    def test_result_represents_only_named_inputs_even_when_idle_and_queue_empty(self):
        self.initialize()
        first = self.send_acknowledged("input-1")
        second = self.send_acknowledged("input-2")
        self.result(user_message_uuid=second, queued_turn_count=0)
        self.session.feed(wire({"type": "system", "subtype": "session_state_changed", "state": "idle"}))
        self.assertFalse(self.session.input_drained)
        # A later receipt can resolve the unmatched input; no inferred batch
        # membership or chronological-result arithmetic is necessary.
        self.result(user_message_uuid=first, queued_turn_count=0)
        self.assertTrue(self.session.input_drained)

    def test_native_queue_and_running_state_each_block_input_drain(self):
        self.initialize()
        first = self.send_acknowledged("input-1")
        self.session.feed(wire({"type": "system", "subtype": "session_state_changed", "state": "running"}))
        self.result(user_message_uuid=first, queued_turn_count=1)
        self.assertFalse(self.session.input_drained)
        self.session.feed(wire({"type": "system", "subtype": "session_state_changed", "state": "idle"}))
        self.assertFalse(self.session.input_drained)
        self.result(queued_turn_count=0)
        self.assertTrue(self.session.input_drained)
        self.session.feed(wire({"type": "system", "subtype": "session_state_changed", "state": "requires_action"}))
        self.assertFalse(self.session.input_drained)

    def test_legacy_single_input_requires_echo_and_attributable_result(self):
        self.initialize()
        self.session.send_user("private input", "input-1")
        outgoing = json.loads(self.sent[-1])
        self.result()
        self.assertFalse(self.session.input_drained)
        self.session.feed(wire(outgoing))
        self.result(origin={"kind": "task-notification"})
        self.assertFalse(self.session.input_drained)
        self.result()
        self.assertTrue(self.session.input_drained)
        self.assertEqual(self.session.input_drain_evidence, "legacy_single_input_result")

    def test_idle_without_input_receipts_does_not_resolve_legacy_batch(self):
        self.initialize()
        self.send_acknowledged("input-1")
        self.send_acknowledged("input-2")
        self.result(queued_turn_count=0)
        self.session.feed(wire({"type": "system", "subtype": "session_state_changed", "state": "idle"}))
        self.assertFalse(self.session.input_drained)
        self.assertEqual(len(self.session._pending_inputs), 2)
        self.assertEqual(self.session.input_drain_evidence, "unverified_multiple_inputs")

    def test_new_input_invalidates_previous_idle_and_completion(self):
        self.initialize()
        first = self.send_acknowledged("input-1")
        self.result(user_message_uuid=first)
        self.session.feed(wire({"type": "system", "subtype": "session_state_changed", "state": "idle"}))
        self.assertTrue(self.session.input_drained)
        second = self.send_acknowledged("input-2")
        self.assertFalse(self.session.input_drained)
        self.result(user_message_uuid=second)
        self.assertFalse(self.session.input_drained)
        self.session.feed(wire({"type": "system", "subtype": "session_state_changed", "state": "idle"}))
        self.assertTrue(self.session.input_drained)

    def test_explicit_batch_receipts_cover_folded_synthetic_turns(self):
        self.initialize()
        first = self.send_acknowledged("input-1")
        self.result(user_message_uuids=[first], user_message_uuid=first, origin={"kind": "task-notification"})
        self.assertEqual(self.session.completed_turns, 0)
        self.assertTrue(self.session.input_drained)

    def test_malformed_completion_metadata_does_not_cover_inputs_or_leak(self):
        self.initialize()
        first = self.send_acknowledged("input-1")
        for metadata in ({"user_message_uuids": [first] * 65},
                         {"user_message_uuids": {"PRIVATE": first}},
                         {"user_message_uuids": [first], "user_message_uuid": "unrelated"},
                         {"user_message_uuids": ["sk-privatevalue12345"]},
                         {"user_message_uuid": first, "queued_turn_count": True}):
            with self.subTest(metadata=metadata):
                self.result(**metadata)
                self.assertFalse(self.session.input_drained)
        self.assertNotIn("sk-privatevalue12345", json.dumps(self.events))
        self.result(user_message_uuid=first, queued_turn_count=0)
        self.assertTrue(self.session.input_drained)

    def test_flags_preserve_native_prompt_settings_and_permissions(self):
        flags = duplex_arguments()
        self.assertIn("--input-format", flags)
        self.assertIn("--replay-user-messages", flags)
        self.assertNotIn("--settings", flags)
        self.assertNotIn("--system-prompt", flags)
        self.assertFalse(any("bypass" in value or "skip-permissions" in value for value in flags))

    def test_synthetic_child_bidirectional_pipes(self):
        # The process reads incrementally; it cannot receive the prompt until
        # our matching initialize acknowledgment has arrived over stdout.
        program = r'''
import json, sys
for line in sys.stdin.buffer:
    message = json.loads(line)
    if message["type"] == "control_request":
        response = {"type":"control_response", "response":{"subtype":"success", "request_id":message["request_id"], "response":{}}}
        print(json.dumps(response), flush=True)
    elif message["type"] == "user":
        print(json.dumps(message), flush=True)
        print(json.dumps({"type":"result", "subtype":"success", "is_error":False, "session_id":"fake-session"}), flush=True)
'''
        process = subprocess.Popen([sys.executable, "-u", "-c", program], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        def send(payload):
            process.stdin.write(payload)
            process.stdin.flush()
        session = NativeSession(send, self.events.append)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        self.addCleanup(selector.close)
        def wait_until(predicate):
            deadline = time.monotonic() + 3
            while not predicate() and time.monotonic() < deadline:
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 17)
                    if not chunk:
                        self.fail("unexpected child EOF")
                    session.feed(chunk)
            self.assertTrue(predicate())
        session.initialize("init")
        wait_until(lambda: session.initialized)
        session.send_user("private synthetic prompt", "input-1")
        wait_until(lambda: session.completed_turns == 1)
        session.interrupt("interrupt-1")
        wait_until(lambda: session.requests["interrupt-1"]["status"] == "acknowledged")
        self.assertEqual(session.requests["input-1"]["status"], "acknowledged")
        process.stdin.close()
        self.assertEqual(process.wait(timeout=3), 0)
        process.stdout.close()
        process.stderr.close()
        session.finish()
        self.assertNotIn("private synthetic prompt", json.dumps(self.events))


class NativeNormalizationTests(unittest.TestCase):
    def test_claude_content_and_nested_private_fields_never_enter_timeline(self):
        frame = {"type": "assistant", "session_id": "session-1", "parent_tool_use_id": "parent-1",
                 "message": {"model": "PRIVATE", "content": [
                     {"type": "thinking", "thinking": "PRIVATE reasoning"},
                     {"type": "text", "text": "PRIVATE answer"},
                     {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "/private/account"}},
                     {"type": "tool_use", "id": "tool-2", "name": "mcp__PRIVATE_account__fetch", "input": {"token": "PRIVATE"}},
                 ]}}
        events = normalize_event(frame)
        self.assertEqual([event.get("tool") for event in events[:2]], ["Read", "MCP"])
        self.assertNotIn("PRIVATE", json.dumps(events))
        self.assertNotIn("account", json.dumps(events))
        self.assertEqual(events[0]["parent_tool_use_id"], "parent-1")

    def test_thinking_only_and_raw_stream_deltas_are_ignored(self):
        self.assertEqual(normalize_event({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "PRIVATE"}]}}), [])
        self.assertEqual(normalize_event({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"text": "PRIVATE"}}}), [])
        self.assertEqual(normalize_event({"type": "transcript_mirror", "entries": "PRIVATE"}), [])

    def test_pointer_and_usage_validation(self):
        event = normalize_event({"type": "result", "subtype": "success", "is_error": False,
                                 "session_id": "/private/person", "parent_tool_use_id": "sk-PRIVATEsecret",
                                 "usage": {"input_tokens": 3, "output_tokens": True, "total_tokens": -1, "PRIVATE": 5},
                                 "total_cost_usd": float("inf"), "result": "PRIVATE"})[0]
        self.assertEqual(event, {"kind": "native_turn", "status": "succeeded", "usage": {"input_tokens": 3}})
        event = normalize_event({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 10**1000})[0]
        self.assertNotIn("total_cost_usd", event)

    def test_codex_public_metadata_without_command_or_reasoning(self):
        events = []
        for frame in (
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {"type": "item.started", "item": {"type": "command_execution", "id": "item-1", "command": "PRIVATE"}},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE"}},
            {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 4}},
        ):
            events.extend(normalize_event(frame, "codex"))
        self.assertEqual(len(events), 4)
        self.assertEqual(events[2]["tool"], "Bash")
        self.assertNotIn("PRIVATE", json.dumps(events))
        self.assertEqual(events[-1]["usage"]["input_tokens"], 12)

    def test_invalid_nested_shapes_do_not_leak_or_raise(self):
        for frame in ({"type": []}, {"type": "system", "subtype": []},
                      {"type": "assistant", "message": []},
                      {"type": "assistant", "message": {"content": [None, [], {"type": []}]}},
                      {"type": "item.completed", "item": {"type": []}}):
            with self.subTest(frame=frame):
                self.assertEqual(normalize_event(frame), [])
                self.assertEqual(normalize_event(frame, "codex"), [])


if __name__ == "__main__":
    unittest.main()
