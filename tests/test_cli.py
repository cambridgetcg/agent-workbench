import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_workbench.cli import main, plan_view, status_view
from agent_workbench.store import Store


class CliTests(unittest.TestCase):
    def test_note_and_steer_refuse_collab_session_file_paths(self):
        locator = "/var/empty/aw/collab-sessions/session_1f.json"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "state")
            state = store.create({"version": 1, "name": "n", "goal": "g",
                                  "tasks": [{"id": "w", "harness": "codex", "prompt": "p"}]}, root)
            for command in (["note", state["id"], "--kind", "evidence", "resume " + locator],
                            ["steer", state["id"], "AGENTOOL_COLLAB_SESSION_FILE=" + locator]):
                with self.subTest(command=command[0]):
                    errors = io.StringIO()
                    with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(main(["--state-dir", str(root / "state")] + command), 1)
                    self.assertIn("Collab session credential file", errors.getvalue())
                    self.assertNotIn(locator, errors.getvalue())
            after = store.read(state["id"])
            self.assertEqual(after["notes"], [])
            self.assertEqual(after["control"]["guidance"], [])

    def test_create_from_stdin_defaults_to_current_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = {"version": 1, "name": "Unicode 宇", "goal": "Preserve text", "tasks": [
                {"id": "work", "harness": "codex", "prompt": "First line\nSecond 宇 line"}]}
            output = io.StringIO()
            with patch("pathlib.Path.cwd", return_value=root), patch("sys.stdin", io.StringIO(json.dumps(manifest))), contextlib.redirect_stdout(output):
                self.assertEqual(main(["--state-dir", str(root / "state"), "create", "-"]), 0)
            state = Store(root / "state").read(output.getvalue().strip())
            self.assertEqual(state["project"], str(root.resolve()))
            self.assertEqual(state["tasks"][0]["prompt"], manifest["tasks"][0]["prompt"])
            self.assertEqual(state["status"], "ready")
            self.assertEqual(state["tasks"][0]["attempts"], [])

    def test_single_task_authoring_preserves_explicit_choices(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prefix = ["--state-dir", str(root / "state"), "create", "--project", temp]
            for source in (["--prompt", "Check the parser\nKeep Unicode 宇"], ["--prompt-file", "-"]):
                output = io.StringIO()
                with patch("sys.stdin", io.StringIO("Check the parser\nKeep Unicode 宇")), contextlib.redirect_stdout(output):
                    self.assertEqual(main(prefix + ["--harness", "codex", "--model", "chosen-model", "--effort", "high"] + source), 0)
                state = Store(root / "state").read(output.getvalue().strip())
                self.assertEqual(state["name"], "Check the parser")
                self.assertEqual(state["goal"], state["tasks"][0]["prompt"])
                self.assertEqual(state["tasks"][0]["model"], "chosen-model")
                self.assertEqual(state["tasks"][0]["effort"], "high")
                self.assertEqual(state["tasks"][0]["attempts"], [])

    def test_single_task_flags_map_to_new_task_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prefix = ["--state-dir", str(root / "state"), "create", "--project", temp]
            for flags, expected in (
                (["--harness", "codex", "--workspace", "worktree", "--base-ref", "origin/main", "--sandbox", "workspace-write"],
                 {"workspace": "worktree", "base_ref": "origin/main", "sandbox": "workspace-write"}),
                (["--harness", "hermes-astra-ultra", "--max-turns", "150", "--skill", "zerone-development",
                  "--skill", "coordinate-agent-work-hermes", "--run-budget", "3000"],
                 {"max_turns": 150, "skills": ["zerone-development", "coordinate-agent-work-hermes"], "run_budget_seconds": 3000}),
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(prefix + flags + ["--prompt", "Work."]), 0)
                task = Store(root / "state").read(output.getvalue().strip())["tasks"][0]
                self.assertEqual({key: task[key] for key in expected}, expected)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(prefix + ["--harness", "claude", "--sandbox", "read-only", "--prompt", "Work."]), 1)
                self.assertEqual(main(prefix + ["--harness", "codex", "--base-ref", "origin/main", "--prompt", "Work."]), 1)

    def test_single_task_budget_needs_a_longer_task_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prefix = ["--state-dir", str(root / "state"), "create", "--project", temp, "--harness", "hermes", "--prompt", "Work."]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(prefix + ["--run-budget", "3000", "--timeout-seconds", "3300"]), 0)
            task = Store(root / "state").read(output.getvalue().strip())["tasks"][0]
            self.assertEqual((task["run_budget_seconds"], task["timeout_seconds"]), (3000, 3300))
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(main(prefix + ["--run-budget", "3300", "--timeout-seconds", "3300"]), 1)
            self.assertIn("run_budget_seconds must be less than timeout_seconds", error.getvalue())

    def test_invalid_creation_never_saves_a_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prefix = ["--state-dir", str(root / "state"), "create", "--project", temp]
            cases = [([], ""), (["--prompt", "No harness"], ""),
                     (["--harness", "codex", "--prompt", ""], ""),
                     (["-", "--harness", "codex"], "{}"), (["-"], "not json"),
                     (["-"], "宇" * 700_000),
                     (["--harness", "codex", "--prompt-file", "-"], "x" * 400_001)]
            for options, stdin in cases:
                with self.subTest(options=options), patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(prefix + options), 1)
            self.assertEqual(Store(root / "state").list_runs(), [])

    def test_long_prompt_file_preserves_task_text_with_bounded_default_goal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prompt = "Inspect a detailed specification\n" + "宇" * 30_000
            path = root / "prompt.txt"
            path.write_text(prompt)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--state-dir", str(root / "state"), "create", "--project", temp,
                                       "--harness", "codex", "--prompt-file", str(path)]), 0)
            state = Store(root / "state").read(output.getvalue().strip())
            self.assertEqual(state["tasks"][0]["prompt"], prompt)
            self.assertEqual(state["goal"], "Inspect a detailed specification")

    def test_short_reference_controls_and_project_filter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            other = root / "other"
            other.mkdir()
            store = Store(root / "state")
            manifest = {"version": 1, "name": "Ref", "goal": "Inspect", "tasks": [{"id": "one", "harness": "codex", "prompt": "Inspect"}]}
            first = store.create(manifest, root)
            store.create(manifest, other)
            prefix = ["--state-dir", str(store.root)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(prefix + ["pause", first["id"][:8]]), 0)
            self.assertEqual(store.read(first["id"])["status"], "paused")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(prefix + ["list", "--project", temp, "--json"]), 0)
            self.assertEqual([state["id"] for state in json.loads(output.getvalue())], [first["id"]])

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
            self.assertEqual(status_view(state)["next_actions"][0]["action"], "run")

    def test_status_and_timeline_are_redacted_read_only_views(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "state")
            state = store.create({"version": 1, "name": "api_key=syntheticcredential123456", "goal": "Inspect", "tasks": [
                {"id": "one", "harness": "codex", "prompt": "Never launch"}]}, Path(temp))
            prefix = ["--state-dir", str(store.root)]
            for command in (["status", state["id"][:8]], ["status", state["id"], "--json"],
                            ["list"], ["list", "--json"], ["timeline", state["id"], "--json"]):
                output = io.StringIO()
                with self.subTest(command=command), contextlib.redirect_stdout(output):
                    self.assertEqual(main(prefix + command), 0)
                self.assertNotIn("syntheticcredential123456", output.getvalue())
            self.assertEqual(store.read(state["id"]), state)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(prefix + ["timeline", state["id"], "--limit", "0"]), 1)

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
