import json
from pathlib import Path
import tempfile
import unittest

from agent_workbench.bridge import FLUSH_EVENTS, EventTail, PublicJournal


class CountingStore:
    def __init__(self):
        self.updates = 0
        self.state = {"events": [], "control": {}, "tasks": [{"id": "task", "attempts": [{"id": "attempt"}]}]}

    def update(self, _run_id, callback):
        self.updates += 1
        callback(self.state)
        return self.state


class PublicJournalTests(unittest.TestCase):
    def test_hermes_text_deltas_become_one_message_per_contiguous_run(self):
        frames = [{"type": "system", "subtype": "init", "session_id": "native-session"}]
        frames += [{"type": "text", "text": "PRIVATE delta"}] * 80
        frames += [{"type": "reasoning", "text": "PRIVATE reasoning"}]
        frames += [{"type": "text", "text": "PRIVATE delta"}] * 5
        frames += [{"type": "tool_use", "name": "terminal", "tool_call_id": "tool-1"},
                   {"type": "tool_result", "name": "terminal", "tool_call_id": "tool-1", "is_error": False}]
        frames += [{"type": "text", "text": "PRIVATE delta"}] * 80
        frames += [{"type": "result", "session_id": "native-session", "exit_code": 0, "text": "PRIVATE final"}]
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "stdout.log"
            log.write_text("".join(json.dumps(frame) + "\n" for frame in frames))
            for harness in ("hermes", "hermes-astra-ultra"):
                events = []
                EventTail(log, harness, events.append).poll(final=True)
                self.assertEqual([event["kind"] for event in events],
                                 ["native_session", "native_message", "native_tool", "native_tool", "native_message", "native_turn"])
                self.assertNotIn("PRIVATE", json.dumps(events))
            # Claude emits whole messages; consecutive ones stay separate events.
            claude = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "PRIVATE"}]}}] * 3
            log.write_text("".join(json.dumps(frame) + "\n" for frame in claude))
            events = []
            EventTail(log, "claude", events.append).poll(final=True)
            self.assertEqual([event["kind"] for event in events], ["native_message"] * 3)

    def test_polling_flushes_are_batched_and_boundaries_are_forced(self):
        store = CountingStore()
        journal = PublicJournal(store, "run", "task", "attempt")
        journal.last_flush = float("inf")  # Hold the time threshold open for this test.
        for _ in range(FLUSH_EVENTS - 1):
            journal.emit({"kind": "native_message"})
            journal.flush(force=False)
        self.assertEqual(store.updates, 0)
        journal.emit({"kind": "native_message"})
        self.assertEqual(store.updates, 1)
        self.assertEqual(len(store.state["events"]), FLUSH_EVENTS)
        journal.emit({"kind": "native_turn", "status": "succeeded"})
        journal.flush(force=False)
        self.assertEqual(store.updates, 1)
        journal.flush()
        self.assertEqual(store.updates, 2)
        self.assertEqual(store.state["events"][-1]["kind"], "native_turn")
        journal.flush()
        self.assertEqual(store.updates, 2)


if __name__ == "__main__":
    unittest.main()
