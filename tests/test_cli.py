import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from agent_workbench.cli import main, plan_view, status_view
from agent_workbench.store import Store


class CliTests(unittest.TestCase):
    def test_create_note_status_without_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "state"
            manifest = root / "workflow.json"
            marker = root / "must-not-exist"
            manifest.write_text(json.dumps({"version": 1, "name": "CLI", "goal": "Do not run on creation", "tasks": [{"id": "work", "harness": "command", "argv": ["touch", str(marker)]}]}))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--state-dir", str(state), "create", str(manifest), "--project", str(root)]), 0)
            ident = output.getvalue().strip()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--state-dir", str(state), "note", ident, "--kind", "decision", "Keep scope"]), 0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--state-dir", str(state), "status", ident, "--json"]), 0)
            data = json.loads(output.getvalue())
            self.assertEqual(data["status"], "ready")
            self.assertEqual(data["notes_count"], 1)
            self.assertNotIn("prompt", data["tasks"][0])
            self.assertNotIn("argv", data["tasks"][0])
            self.assertFalse(marker.exists())
            view = plan_view(Store(state).read(ident))
            self.assertEqual(view["manifest"]["tasks"][0]["argv"], ["touch", str(marker)])
            self.assertEqual(view["notes"][0]["text"], "Keep scope")

    def test_plan_redacts_display_without_mutating_state(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "state")
            state = store.create({"version": 1, "name": "Plan", "goal": "Inspect", "tasks": [{"id": "one", "harness": "codex", "prompt": "Do not print api_key=syntheticcredential123456"}]}, Path(temp))
            original = state["tasks"][0]["prompt"]
            self.assertNotIn("syntheticcredential123456", json.dumps(plan_view(state)))
            self.assertEqual(state["tasks"][0]["prompt"], original)

    def test_status_explains_planned_pause(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "state")
            state = store.create({"version": 1, "name": "Pause", "goal": "Bound execution", "tasks": [{"id": "one", "harness": "command", "argv": ["true"]}]}, Path(temp))
            state["status"] = "paused"
            state["events"].append({"kind": "run_paused", "reason": "launch_limit"})
            self.assertEqual(status_view(state)["status_reason"], "launch_limit")

    def test_invalid_manifest_has_controlled_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "bad.json"
            manifest.write_text('{"version": 1}')
            with contextlib.redirect_stderr(io.StringIO()) as err:
                result = main(["--state-dir", str(root / "state"), "create", str(manifest), "--project", str(root)])
            self.assertEqual(result, 1)
            self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
