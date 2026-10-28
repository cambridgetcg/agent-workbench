import json
from pathlib import Path
import re
import unittest

from agent_workbench.agenttool import names_collab_session_file, refused_command
from agent_workbench.store import validate_manifest


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class ExampleTests(unittest.TestCase):
    def test_every_example_is_a_valid_manifest_without_user_paths_or_refused_commands(self):
        paths = sorted(EXAMPLES.glob("*.json"))
        self.assertGreaterEqual(len(paths), 2)
        for path in paths:
            with self.subTest(example=path.name):
                text = path.read_text()
                manifest = validate_manifest(json.loads(text))
                self.assertIsNone(re.search(r"/(?:Users|home)/[^/\s]+/", text))
                self.assertFalse(names_collab_session_file(text))
                for task in manifest["tasks"]:
                    if task["harness"] == "command":
                        self.assertIsNone(refused_command(task["argv"]))

    def test_collab_peer_example_keeps_review_with_the_parent(self):
        manifest = validate_manifest(json.loads((EXAMPLES / "collab-peer.json").read_text()))
        peer, check = manifest["tasks"]
        self.assertEqual(peer["harness"], "hermes")
        self.assertEqual((check["harness"], check["depends_on"], check["workspace"]), ("command", ["peer"], "read-only"))
        for phrase in ("Do not review your own task", "End your own session", "actor REPLACE_ACTOR_LABEL",
                       "never set AGENTOOL_COLLAB_SESSION_FILE", "Do not use the lobby"):
            self.assertIn(phrase, peer["prompt"])


if __name__ == "__main__":
    unittest.main()
