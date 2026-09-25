from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_workbench.runner import recover_run, retry_task
from agent_workbench.store import Store
from agent_workbench.views import status_details, timeline_view


def run_state(status="ready"):
    return {"id": "a" * 32, "status": status, "controller": None, "events": [], "tasks": [
        {"id": "first", "status": "pending", "depends_on": [], "attempts": [], "result": None},
        {"id": "second", "status": "pending", "depends_on": ["first"], "attempts": [], "result": None},
    ]}


class ViewTests(unittest.TestCase):
    def test_paused_continuation_preserves_completed_work(self):
        state = run_state("paused")
        state["tasks"][0]["status"] = "succeeded"
        before = deepcopy(state)
        view = status_details(state)
        self.assertEqual(view["next_actions"][0]["action"], "run")
        self.assertIn("paused", view["tasks"]["second"]["status_reason"])
        self.assertEqual(view["tasks"]["second"]["waiting_on"], [])
        self.assertIn("without repeating", view["tasks"]["first"]["status_reason"])
        self.assertEqual(state, before)

    def test_failure_blocks_dependencies_and_exposes_attempt(self):
        state = run_state("failed")
        state["tasks"][0].update(status="failed", attempts=[{
            "id": "attempt-one", "status": "failed", "failure_kind": "native_result",
            "native_session_id": "session-one", "stdout": "private/stdout.log",
        }])
        view = status_details(state)
        self.assertEqual(view["next_actions"][0]["action"], "retry")
        self.assertEqual(view["next_actions"][0]["task_id"], "first")
        self.assertEqual(view["tasks"]["second"]["waiting_on"], ["first"])
        self.assertIn("first (failed)", view["tasks"]["second"]["status_reason"])
        self.assertEqual(view["tasks"]["first"]["latest_attempt"]["native_session_id"], "session-one")
        self.assertNotIn("stdout", view["tasks"]["first"]["latest_attempt"])

    def test_independent_pending_task_reports_global_retry_requirement(self):
        state = run_state("failed")
        state["tasks"][0]["status"] = "failed"
        state["tasks"][1]["depends_on"] = []
        self.assertIn("explicit retry", status_details(state)["tasks"]["second"]["status_reason"])

    def test_live_or_ambiguous_controller_never_suggests_recovery(self):
        for status in ("running", "interrupted"):
            with self.subTest(status=status):
                state = run_state(status)
                state["controller"] = {"pid": 123}
                state["tasks"][0]["status"] = "running"
                actions = status_details(state)["next_actions"]
                self.assertNotIn("recover", [a["action"] for a in actions])
                self.assertNotIn("retry", [a["action"] for a in actions])

    def test_interrupted_work_requires_recovery_only_when_attempt_remains_running(self):
        state = run_state("interrupted")
        state["tasks"][0]["status"] = "running"
        self.assertEqual(status_details(state)["next_actions"][0]["action"], "recover")
        state["tasks"][0]["status"] = "interrupted"
        self.assertEqual(status_details(state)["next_actions"][0]["action"], "retry")

    def test_retried_task_retains_last_attempt_pointer_and_is_ready(self):
        state = run_state()
        state["tasks"][0]["attempts"] = [{"id": "previous", "status": "failed", "native_session_id": "previous-session"}]
        view = status_details(state)
        self.assertEqual(view["next_actions"][0]["action"], "run")
        self.assertEqual(view["tasks"]["first"]["latest_attempt"]["status"], "failed")
        self.assertIn("Ready", view["tasks"]["first"]["status_reason"])

    def test_recovered_unresolved_group_requires_inspection_before_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "state")
            state = store.create({"version": 1, "name": "Recover", "goal": "Inspect", "tasks": [
                {"id": "work", "harness": "command", "argv": ["true"]},
            ]}, Path(temporary))

            def abandoned(current):
                current.update(status="running", controller={"pid": 123})
                current["tasks"][0].update(status="running", attempts=[{
                    "id": "abandoned", "status": "running", "started_at": "2026-01-01T00:00:00",
                    "process_group": 98765, "process_identity": "original process start",
                }])

            store.update(state["id"], abandoned)
            with patch("agent_workbench.runner._group_exists", return_value=True), \
                    patch("agent_workbench.runner._process_identity", return_value="different start"), \
                    patch("agent_workbench.runner._terminate_group") as terminate:
                recovered = recover_run(store, state["id"])
                with self.assertRaisesRegex(RuntimeError, "live descendants"):
                    retry_task(store, state["id"], "work")
                terminate.assert_not_called()
            view = status_details(recovered)
            self.assertEqual([item["action"] for item in view["next_actions"]], ["inspect"])
            task = view["tasks"]["work"]
            self.assertTrue(task["latest_attempt"]["unresolved_process_group"])
            self.assertEqual(task["unresolved_attempt_count"], 1)
            self.assertEqual(task["unresolved_attempts"][0]["process_group"], 98765)
            self.assertIn("98765", task["status_reason"])
            entry = next(item for item in timeline_view(recovered)["entries"] if item["kind"] == "task_finished")
            self.assertTrue(entry["unresolved_process_group"])
            self.assertEqual(entry["process_identity"], "original process start")
            with patch("agent_workbench.runner._group_exists", return_value=False):
                retried = retry_task(store, state["id"], "work")
            self.assertEqual(status_details(retried)["next_actions"][0]["action"], "run")
            self.assertEqual(status_details(retried)["tasks"]["work"]["unresolved_attempt_count"], 1)

    def test_older_unresolved_attempt_blocks_retry_suggestions_across_tasks(self):
        state = run_state("failed")
        state["tasks"][0].update(status="interrupted", attempts=[
            {"id": "old", "status": "interrupted", "unresolved_process_group": True, "process_group": 98765},
            {"id": "new", "status": "interrupted"},
        ])
        state["tasks"][1]["status"] = "failed"
        view = status_details(state)
        self.assertEqual(view["tasks"]["first"]["latest_attempt"]["id"], "new")
        self.assertEqual(view["tasks"]["first"]["unresolved_attempts"][0]["id"], "old")
        self.assertIn("every flagged group", view["tasks"]["second"]["status_reason"])
        self.assertEqual([action["action"] for action in view["next_actions"]], ["inspect"])
        state["controller"] = {"pid": 123}
        state["status"] = "running"
        actions = status_details(state)["next_actions"]
        self.assertEqual([action["action"] for action in actions], ["wait", "pause", "stop"])

    def test_completed_run_has_no_execution_action(self):
        state = run_state("succeeded")
        for task in state["tasks"]:
            task["status"] = "succeeded"
        self.assertEqual(status_details(state)["next_actions"], [])

    def test_timeline_keeps_retry_attempts_without_duplicate_events(self):
        state = run_state("succeeded")
        state["tasks"][0]["attempts"] = [
            {"id": "old", "status": "failed", "started_at": "2026-01-01T00:00:01", "finished_at": "2026-01-01T00:00:02", "native_session_id": "old-session"},
            {"id": "new", "status": "succeeded", "started_at": "2026-01-01T00:00:04", "finished_at": "2026-01-01T00:00:05", "native_session_id": "new-session"},
        ]
        state["events"] = [
            {"kind": "task_finished", "at": "2026-01-01T00:00:02", "task_id": "first", "attempt_id": "old", "status": "failed"},
            {"kind": "task_retry_requested", "at": "2026-01-01T00:00:03", "task_id": "first", "previous_attempts": 1},
        ]
        before = deepcopy(state)
        view = timeline_view(state)
        self.assertEqual(view["total_entries"], 5)
        finishes = [item for item in view["entries"] if item["kind"] == "task_finished"]
        self.assertEqual([item["native_session_id"] for item in finishes], ["old-session", "new-session"])
        self.assertEqual([item["status"] for item in finishes], ["failed", "succeeded"])
        self.assertEqual(state, before)
        short = timeline_view(state, 2)
        self.assertTrue(short["truncated"])
        self.assertEqual(short["entries"], view["entries"][-2:])

    def test_selected_metadata_is_redacted_and_raw_payloads_are_omitted(self):
        state = run_state("failed")
        state["events"] = [{"kind": "task_finished", "at": "today", "task_id": "first", "attempt_id": "old", "reason": "api_key=syntheticcredential123456", "raw": "PRIVATE_RAW", "analysis": "PRIVATE_REASONING"}]
        state["tasks"][0]["attempts"] = [{"id": "old", "status": "failed", "native_session_id": "Bearer abcdefghijklmnop", "stdout": "PRIVATE_LOG", "environment": {"TOKEN": "PRIVATE_ENV"}}]
        data = json.dumps([status_details(state), timeline_view(state)])
        for value in ("syntheticcredential123456", "abcdefghijklmnop", "PRIVATE_RAW", "PRIVATE_REASONING", "PRIVATE_LOG", "PRIVATE_ENV"):
            self.assertNotIn(value, data)
        self.assertIn("redact", data)

    def test_timeline_limit_is_bounded_and_validated(self):
        state = run_state()
        for limit in (0, -1, 201, True, 1.0, "20", None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                timeline_view(state, limit)


if __name__ == "__main__":
    unittest.main()
