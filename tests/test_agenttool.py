from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from agent_workbench import handoff
from agent_workbench.agenttool import (SESSION_FILE_REFUSAL, WITHHELD, launch_refusal, names_collab_session_file,
                                       refused_command, withhold_session_files)
from agent_workbench.store import Store


class RefusedCommandTests(unittest.TestCase):
    def test_lobby_cli_is_refused_in_every_launch_form(self):
        for argv in (["agenttool-lobby", "room", "say", "room_" + "0" * 32, "--to", "U", "--text", "hi"],
                     ["/opt/desk/bin/agenttool-lobby", "room", "list"],
                     ["bun", "/opt/desk/bin/agenttool-lobby.ts", "room", "list"],
                     ["env", "X=1", "agenttool-lobby", "fleet", "sync"],
                     ["sh", "-c", "agenttool-lobby room list"],
                     ["script", "-q", "/dev/null", "agenttool-lobby", "hub", "init"],
                     ["python3", "-c", 'import subprocess;subprocess.run(["agenttool-lobby","room","list"])'],
                     ["env", "PAGER=agenttool-lobby", "git", "log"]):
            with self.subTest(argv=argv):
                reason = refused_command(argv)
                self.assertIsNotNone(reason)
                self.assertIn("terminal", reason)
                self.assertNotIn("--lease", reason)

    def test_lease_sends_are_refused_whatever_the_executable(self):
        for argv in (["relay", "room", "say", "r", "--lease", "L"], ["x", "room", "say", "--lease=L"],
                     ["sh", "-c", "relay room say r --lease L"]):
            with self.subTest(argv=argv):
                self.assertIn("seat lease", refused_command(argv))
        self.assertIsNone(refused_command(["tool", "--lease", "L"]))

    def test_consequential_agenttool_acts_are_refused(self):
        cases = {"courier": [["agenttool-collab-courier", "send"], ["collab-courier", "send"]],
                 "anchor": [["collab-zerone", "anchor"], ["bun", "agenttool-collab-zerone.ts", "--x", "anchor"]],
                 "mission": [["node", "bin/agenttool-mission.mjs", "chat"], ["node", "mission-chat.mjs"],
                             ["mission-wake-codex"], ["python3", "bin/mission-work-hermes-runner.py"]],
                 "fences": [["agenttool-collab-host", "end", "--session", "S"]],
                 "personal wake": [["curl", "-s", "https://api.agenttool.dev/v1/wake"],
                                   ["curl", "https://api.agenttool.dev/v1/wake?format=anthropic"]]}
        for word, argvs in cases.items():
            for argv in argvs:
                with self.subTest(argv=argv):
                    self.assertIn(word, refused_command(argv))

    def test_search_commands_and_read_only_neighbours_pass(self):
        for argv in (["git", "grep", "agenttool-lobby"], ["grep", "-r", "agenttool-lobby room say", "."],
                     ["git", "log", "-S", "agenttool-lobby"], ["agenttool-lobby-mcp"], ["agenttool-wake", "--json"],
                     ["agenttool-collab-host", "wait", "--session", "S"], ["collab-zerone", "status"],
                     ["python3", "-m", "unittest"], ["curl", "https://api.agenttool.dev/public/self"],
                     ["curl", "https://api.agenttool.dev/v1/wake-thread-docs"]):
            with self.subTest(argv=argv):
                self.assertIsNone(refused_command(argv))

    def test_search_exemption_is_per_segment_and_not_for_pagers(self):
        self.assertIsNotNone(refused_command(["sh", "-c", "grep -q agenttool-lobby x && agenttool-lobby room list"]))
        self.assertIsNotNone(refused_command(["git", "grep", "-O", "agenttool-lobby", "x"]))
        self.assertIsNotNone(refused_command(["git", "-c", "core.pager=agenttool-lobby", "log"]))
        self.assertIsNone(refused_command(["sh", "-c", "grep -q agenttool-lobby x && echo found"]))


class SessionFileTests(unittest.TestCase):
    def test_locator_matches_paths_and_assignments_not_mentions(self):
        for text in ("/var/empty/x/collab-sessions/session_1f.json", "AGENTOOL_COLLAB_SESSION_FILE=/tmp/s.json",
                     'AGENTOOL_COLLAB_SESSION_FILE: "/tmp/s.json"', '"AGENTOOL_COLLAB_SESSION_FILE": "s.json"',
                     "resume with collab-sessions\\abc.json"):
            with self.subTest(text=text):
                self.assertTrue(names_collab_session_file(text))
        for text in ("never read collab-sessions/*.json", "collab-sessions/<id>.json", "AGENTOOL_COLLAB_SESSION_FILE",
                     "${AGENTOOL_COLLAB_SESSION_FILE:-}", 'AGENTOOL_COLLAB_SESSION_FILE="$X"',
                     "AGENTOOL_COLLAB_SESSION_FILE: withheld", None):
            with self.subTest(text=text):
                self.assertFalse(names_collab_session_file(text))

    def test_withhold_replaces_path_and_value_only(self):
        text = "ended /var/empty/x/collab-sessions/session_1f.json; AGENTOOL_COLLAB_SESSION_FILE=/tmp/s.json kept"
        once = withhold_session_files(text)
        self.assertEqual(once, f"ended {WITHHELD}; AGENTOOL_COLLAB_SESSION_FILE={WITHHELD} kept")
        self.assertEqual(withhold_session_files(once), once)
        self.assertFalse(names_collab_session_file(once))

    def test_launch_refusal_names_the_task_and_never_the_path(self):
        locator = "/var/empty/x/collab-sessions/s.json"
        self.assertEqual(launch_refusal({"id": "peer", "harness": "codex"}, "resume " + locator),
                         "Task peer " + SESSION_FILE_REFUSAL)
        self.assertIn("lobby", launch_refusal({"id": "notify", "harness": "command",
                                               "argv": ["agenttool-lobby", "room", "list"]}, ""))
        self.assertIsNone(launch_refusal({"id": "fine", "harness": "command", "argv": ["true"]}, ""))
        self.assertNotIn(locator, launch_refusal({"id": "c", "harness": "command", "argv": ["cat", locator]}, ""))


class ManifestGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-workbench-agenttool-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")

    def manifest(self, **task):
        return {"version": 1, "name": "t", "goal": "t", "tasks": [{"id": "notify", "harness": "command", "argv": ["true"], **task}]}

    def test_command_task_running_lobby_is_refused_at_create_and_import(self):
        with self.assertRaises(ValueError) as caught:
            self.store.create(self.manifest(argv=["agenttool-lobby", "room", "list"]), self.root)
        self.assertTrue(str(caught.exception).startswith("Task notify runs the AgentTool lobby CLI"))
        self.assertEqual(self.store.list_runs(), [])
        state = self.store.create(self.manifest(), self.root)
        output = self.root / "portable.json"
        document = handoff.export_handoff(self.store, state["id"], output)
        changed = deepcopy(document)
        changed["manifest"]["tasks"][0]["argv"] = ["agenttool-lobby", "room", "list"]
        unsigned = {key: value for key, value in changed.items() if key != "digest"}
        encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        changed["digest"] = {"algorithm": "sha256", "value": hashlib.sha256(encoded).hexdigest()}
        output.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            handoff.import_handoff(self.store, output, self.root)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_name_goal_prompt_and_argv_naming_a_session_file_are_refused_without_echoing_it(self):
        locator = "/var/empty/aw/collab-sessions/session_1f.json"
        cases = {"name": {"version": 1, "name": "resume " + locator, "goal": "g",
                          "tasks": [{"id": "w", "harness": "codex", "prompt": "p"}]},
                 "goal": {"version": 1, "name": "n", "goal": "use " + locator,
                          "tasks": [{"id": "w", "harness": "codex", "prompt": "p"}]},
                 "prompt": {"version": 1, "name": "n", "goal": "g",
                            "tasks": [{"id": "w", "harness": "hermes", "prompt": "AGENTOOL_COLLAB_SESSION_FILE=" + locator}]},
                 "argv": self.manifest(argv=["cat", locator])}
        for label, manifest in cases.items():
            with self.subTest(field=label):
                with self.assertRaises(ValueError) as caught:
                    self.store.create(manifest, self.root)
                self.assertIn("Collab session credential file", str(caught.exception))
                self.assertNotIn(locator, str(caught.exception))
        self.assertEqual(self.store.list_runs(), [])


if __name__ == "__main__":
    unittest.main()
