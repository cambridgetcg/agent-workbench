import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

from agent_workbench.store import Store, validate_manifest


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.manifest = {"version": 1, "name": "Test", "goal": "Verify state", "tasks": [{"id": "first", "harness": "command", "argv": ["echo", "ok"]}]}

    def tearDown(self):
        self.temp.cleanup()

    def test_create_roundtrip_private(self):
        state = self.store.create(self.manifest, self.root)
        self.assertEqual(self.store.read(state["id"]), state)
        self.assertEqual(state["tasks"][0]["status"], "pending")
        self.assertEqual((self.store.directory(state["id"]) / "state.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.list_runs()[0]["id"], state["id"])
        self.assertNotIn("status", self.manifest["tasks"][0])

    def test_reject_bad_graphs_and_ids(self):
        for tasks in [
            [{"id": "x", "harness": "command", "argv": ["true"], "depends_on": ["missing"]}],
            [{"id": "x", "harness": "command", "argv": ["true"], "depends_on": ["x"]}],
            [{"id": "../escape", "harness": "command", "argv": ["true"]}],
            [{"id": "x", "harness": "command", "argv": ["true"]}] * 2,
        ]:
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                validate_manifest({**self.manifest, "tasks": tasks})

    def test_no_shell_or_unknown_fields(self):
        with self.assertRaises(ValueError):
            validate_manifest({**self.manifest, "tasks": [{"id": "x", "harness": "command", "argv": "echo hi"}]})
        with self.assertRaises(ValueError):
            validate_manifest({**self.manifest, "shell": True})

    def test_invalid_types_and_efforts(self):
        for task in [
            {"id": "x", "harness": "codex", "prompt": "hi", "effort": "ultracode"},
            {"id": "x", "harness": "claude", "prompt": "hi", "effort": "ultra"},
            {"id": "x", "harness": "astra", "prompt": "hi", "model": "other"},
            {"id": "x", "harness": "command", "argv": ["true"], "timeout_seconds": float("nan")},
            {"id": "x", "harness": [], "prompt": "hi"},
        ]:
            with self.subTest(task=task), self.assertRaises(ValueError):
                validate_manifest({**self.manifest, "tasks": [task]})

    def test_update_failure_does_not_save(self):
        state = self.store.create(self.manifest, self.root)
        def fail(s):
            s["goal"] = "changed"
            raise RuntimeError("abort")
        with self.assertRaises(RuntimeError):
            self.store.update(state["id"], fail)
        self.assertEqual(self.store.read(state["id"]), state)

    def test_run_reference_resolution_is_explicit_and_unambiguous(self):
        ids = ["12345678" + "a" * 24, "12345678" + "b" * 24]
        for ident in ids:
            with patch("agent_workbench.store.uuid.uuid4", return_value=SimpleNamespace(hex=ident)):
                self.store.create(self.manifest, self.root)
        self.assertEqual(self.store.resolve_run_id(ids[0]), ids[0])
        self.assertEqual(self.store.resolve_run_id(ids[0][:9]), ids[0])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.store.resolve_run_id("12345678")
        with self.assertRaisesRegex(ValueError, "No saved run"):
            self.store.resolve_run_id("87654321")
        for value in ("latest", "123", "../escape", "A" * 32):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.resolve_run_id(value)

    def test_concurrent_updates_do_not_lose_notes(self):
        state = self.store.create(self.manifest, self.root)
        def write(i):
            self.store.update(state["id"], lambda s: s["notes"].append({"text": str(i)}))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(40)))
        result = self.store.read(state["id"])
        self.assertEqual(len(result["notes"]), 40)
        self.assertEqual(result["revision"], 40)

    def test_traversal_and_symlink_rejected(self):
        with self.assertRaises(ValueError):
            self.store.read("../../escape")
        state = self.store.create(self.manifest, self.root)
        path = self.store.directory(state["id"]) / "state.json"
        other = self.root / "other.json"
        path.rename(other)
        path.symlink_to(other)
        with self.assertRaises(ValueError):
            self.store.read(state["id"])


if __name__ == "__main__":
    unittest.main()
