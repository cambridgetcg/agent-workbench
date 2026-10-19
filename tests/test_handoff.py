"""Handoffs stay portable data, including when their historical claims say success."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from agent_workbench import handoff
from agent_workbench.store import Store


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-workbench-handoff-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.target_project = self.root / "target"
        self.target_project.mkdir()
        self.store = Store(self.root / "state")
        self.manifest = {
            "version": 1, "name": "Portable review", "goal": "Verify the proposed change.",
            "tasks": [
                {"id": "inspect", "harness": "codex", "prompt": "Inspect the change.", "effort": "high"},
                {"id": "verify", "harness": "command", "argv": ["python3", "-c", "print('verified')"], "depends_on": ["inspect"], "timeout_seconds": 30},
                {"id": "review", "harness": "claude", "prompt": "Review the explicit evidence.", "depends_on": ["verify"]},
            ],
        }
        self.state = self.store.create(self.manifest, self.project)
        self.output = self.root / "portable.json"

    def export(self):
        return handoff.export_handoff(self.store, self.state["id"], self.output)

    def write_document(self, document, *, resign=True):
        if resign:
            unsigned = {key: value for key, value in document.items() if key != "digest"}
            encoded = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            document["digest"] = {"algorithm": "sha256", "value": hashlib.sha256(encoded).hexdigest()}
        self.output.write_text(json.dumps(document))

    def populate(self):
        def update(state):
            state["status"] = "succeeded"
            for task in state["tasks"]:
                task["status"] = "succeeded"
                task["attempts"] = [{"status": "succeeded", "exit_code": 0, "stdout": "PRIVATE-RAW-TRACE"}]
                task["result"] = {"text": "PRIVATE-RESULT", "reasoning": "PRIVATE-HIDDEN-REASONING"}
            state["events"].append({"kind": "raw", "text": "PRIVATE-EVENT-TRACE"})
            state["notes"] = [
                {"kind": "decision", "text": "Keep the existing public API.", "at": "2026-09-25T00:00:00Z"},
                {"kind": "question", "text": "Does the downstream client accept the result?", "at": "2026-09-25T00:01:00Z"},
                {"kind": "evidence", "text": "The isolated check printed verified.", "at": "2026-09-25T00:02:00Z"},
                {"kind": "next", "text": "Repeat verification in the receiving project.", "at": "2026-09-25T00:03:00Z"},
            ]
        return self.store.update(self.state["id"], update)

    def test_roundtrip_preserves_notes_task_manifest_and_dependencies(self):
        original = self.populate()
        document = self.export()
        raw = self.output.read_bytes()
        imported = handoff.import_handoff(self.store, self.output, self.target_project)
        self.assertNotEqual(imported["id"], original["id"])
        self.assertEqual(imported["project"], str(self.target_project.resolve()))
        self.assertEqual(imported["goal"], original["goal"])
        self.assertEqual(imported["notes"], original["notes"])
        actual_manifest = [{key: value for key, value in task.items() if key in handoff.TASK_FIELDS} for task in imported["tasks"]]
        self.assertEqual(actual_manifest, document["manifest"]["tasks"])
        self.assertEqual(imported["handoff"]["historical"], document["history"])
        artifact = self.store.directory(imported["id"]) / imported["handoff"]["source_artifact"]
        self.assertEqual(artifact.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)

    def test_new_task_options_survive_export_import_and_plan(self):
        from agent_workbench.cli import plan_view
        tasks = [
            {"id": "lane", "harness": "codex", "prompt": "Change it.", "workspace": "worktree",
             "base_ref": "origin/main", "sandbox": "workspace-write"},
            {"id": "astra", "harness": "hermes-astra-ultra", "prompt": "Review it.", "depends_on": ["lane"],
             "max_turns": 150, "skills": ["zerone-development"], "run_budget_seconds": 3000, "timeout_seconds": 3300},
            {"id": "pane", "harness": "claude", "prompt": "Watch it.", "depends_on": ["astra"], "model": "opus",
             "effort": "high", "adapter": "duplex", "workspace": "read-only"},
            {"id": "pair", "harness": "hermes", "prompt": "Pair on it.", "depends_on": ["pane"], "skills": ["one", "two"]},
        ]
        self.state = self.store.create({"version": 1, "name": "Options", "goal": "Carry options.", "tasks": deepcopy(tasks)}, self.project)
        self.export()
        imported = handoff.import_handoff(self.store, self.output, self.target_project)
        for expected, actual in zip(tasks, imported["tasks"]):
            self.assertEqual({key: actual[key] for key in expected}, expected)
        self.assertEqual(plan_view(imported)["manifest"]["tasks"][1]["skills"], ["zerone-development"])
        markdown = self.output.with_suffix(".md").read_text()
        self.assertIn("dependencies: none; `workspace` worktree; `base_ref` origin/main; `sandbox` workspace\\-write.", markdown)
        self.assertIn("dependencies: lane; `timeout_seconds` 3300; `max_turns` 150; `skills` zerone\\-development; "
                      "`run_budget_seconds` 3000.", markdown)
        self.assertIn("dependencies: astra; `model` opus; `effort` high; `adapter` duplex; `workspace` read\\-only.", markdown)
        self.assertIn("dependencies: pane; `skills` one, two.", markdown)
        self.assertNotIn("Review it", markdown)

    def test_markdown_settings_escape_untrusted_model_text(self):
        tasks = [{"id": "lane", "harness": "claude", "prompt": "Change it.",
                  "model": "<b>[x](y)</b>\nnext `line`\u202e\u0085\u200b!", "effort": "high"}]
        self.state = self.store.create({"version": 1, "name": "Model", "goal": "Escape it.", "tasks": tasks}, self.project)
        self.export()
        markdown = self.output.with_suffix(".md").read_text()
        line = next(line for line in markdown.splitlines() if line.startswith("- `lane`"))
        self.assertNotIn("<b>", line)
        self.assertNotIn("[x](", line)
        # U+0085 is whitespace and collapses; the bidi override and zero-width space are replaced.
        self.assertIn("`model` &lt;b&gt;\\[x\\]\\(y\\)&lt;/b&gt; next \\`line\\`\ufffd \ufffd\\!; `effort` high.", line)

    def test_historical_success_never_becomes_executable_success(self):
        self.populate()
        self.export()
        imported = handoff.import_handoff(self.store, self.output, self.target_project)
        self.assertEqual(imported["status"], "ready")
        self.assertIsNone(imported["controller"])
        self.assertEqual(imported["control"], {"stop_requested": False, "pause_requested": False, "guidance": []})
        self.assertEqual(imported["handoff"]["historical"]["run_status"], "succeeded")
        for task in imported["tasks"]:
            self.assertEqual(task["status"], "pending")
            self.assertEqual(task["attempts"], [])
            self.assertIsNone(task["result"])

    def test_import_never_executes_commands_or_contacts_network(self):
        self.export()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("No process may start")), \
             mock.patch("socket.socket", side_effect=AssertionError("No network may start")):
            imported = handoff.import_handoff(self.store, self.output, self.target_project)
        self.assertEqual(imported["tasks"][1]["status"], "pending")

    def test_export_omits_project_raw_logs_results_and_hidden_reasoning(self):
        self.populate()
        document = self.export()
        serialized = self.output.read_text() + self.output.with_suffix(".md").read_text()
        self.assertNotIn("PRIVATE-", serialized)
        self.assertNotIn(str(self.project), serialized)
        self.assertNotIn("events", document)
        self.assertNotIn("attempts", document["manifest"]["tasks"][0])
        self.assertEqual(document["history"]["tasks"][0]["attempt_count"], 1)

    def test_digest_is_canonical_not_a_signature(self):
        document = self.export()
        self.output.write_text(json.dumps(document, indent=4, sort_keys=False))
        imported = handoff.import_handoff(self.store, self.output, self.target_project)
        self.assertEqual(imported["handoff"]["source_digest"], document["digest"])
        markdown = self.output.with_suffix(".md").read_text()
        self.assertIn("not a signature", markdown)
        self.assertIn("Import resets all runnable tasks to pending", markdown)

    def test_tampered_digest_rejected_before_creating_run(self):
        document = self.export()
        document["manifest"]["goal"] = "An edited goal without a new digest."
        self.write_document(document, resign=False)
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            handoff.import_handoff(self.store, self.output, self.target_project)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_schema_rejections_even_with_recomputed_digest(self):
        original = self.export()
        variants = []
        for version in (2, True, "1"):
            changed = deepcopy(original)
            changed["version"] = version
            variants.append(changed)
        changed = deepcopy(original)
        changed["raw_logs"] = "unsupported"
        variants.append(changed)
        changed = deepcopy(original)
        changed["manifest"]["tasks"][0]["status"] = "succeeded"
        variants.append(changed)
        changed = deepcopy(original)
        changed["history"]["tasks"][0]["id"] = "another"
        variants.append(changed)
        changed = deepcopy(original)
        changed["notes"] = [{"kind": "reasoning", "text": "private", "at": "today"}]
        variants.append(changed)
        changed = deepcopy(original)
        changed["manifest"]["tasks"][0]["depends_on"] = ["review"]
        variants.append(changed)
        for document in variants:
            with self.subTest(document=document):
                self.write_document(document)
                with self.assertRaises(ValueError):
                    handoff.import_handoff(self.store, self.output, self.target_project)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_path_fields_and_traversal_never_control_local_writes(self):
        document = self.export()
        document["source"]["run_id"] = "../../outside"
        self.write_document(document)
        with self.assertRaises(ValueError):
            handoff.import_handoff(self.store, self.output, self.target_project)
        document["source"]["run_id"] = self.state["id"]
        document["source"]["artifact"] = "../../outside"
        self.write_document(document)
        with self.assertRaises(ValueError):
            handoff.import_handoff(self.store, self.output, self.target_project)
        self.assertFalse((self.root / "outside").exists())
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_size_and_shape_bounds(self):
        original = self.export()
        self.output.write_bytes(b" " * (handoff.MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "size limit"):
            handoff.import_handoff(self.store, self.output, self.target_project)
        changed = deepcopy(original)
        changed["notes"] = [{"kind": "next", "text": "Review.", "at": "today"}] * 501
        self.write_document(changed)
        with self.assertRaisesRegex(ValueError, "too many notes"):
            handoff.import_handoff(self.store, self.output, self.target_project)
        for raw in (b"[]", b'{"version":1,"version":1}', b'{"version":NaN}', b"\xff", b"{" * 2000):
            with self.subTest(raw=raw[:30]):
                self.output.write_bytes(raw)
                with self.assertRaises(ValueError):
                    handoff.import_handoff(self.store, self.output, self.target_project)

    def test_existing_json_or_markdown_is_never_overwritten(self):
        for path in (self.output, self.output.with_suffix(".md")):
            with self.subTest(path=path.name):
                path.write_text("existing content")
                with self.assertRaises(FileExistsError):
                    self.export()
                self.assertEqual(path.read_text(), "existing content")
                other = self.output.with_suffix(".md") if path == self.output else self.output
                self.assertFalse(other.exists())
                path.unlink()

    def test_symlink_output_is_not_followed(self):
        target = self.root / "config.json"
        target.write_text("private configuration")
        self.output.symlink_to(target)
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(target.read_text(), "private configuration")

    def test_failed_markdown_write_rolls_back_new_json(self):
        real_write = handoff._write_new
        def fail_markdown(path, data):
            if path.suffix == ".md":
                raise OSError("synthetic disk error")
            real_write(path, data)
        with mock.patch.object(handoff, "_write_new", side_effect=fail_markdown):
            with self.assertRaisesRegex(OSError, "synthetic disk error"):
                self.export()
        self.assertFalse(self.output.exists())

    def test_export_uses_private_file_permissions(self):
        self.export()
        for path in (self.output, self.output.with_suffix(".md")):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_credential_like_text_rejected_without_echoing_it(self):
        secrets = [
            "sk-proj-FAKEPRIVATEKEY12345678901234567890",
            "Authorization: Bearer PRIVATEVALUE123456789",
            '"refresh_token": "PRIVATEVALUE123456789"',
            "OPENAI_API_KEY=PRIVATEVALUE123456789",
            "-----BEGIN PRIVATE KEY-----",
            "ghp_ABCDEFGHIJKLMNOPQRSTUV1234567890",
            "SERVICE_TOKEN=PRIVATEVALUE123456789",
            '"client_secret": "PRIVATEVALUE123456789"',
            "Authorization: Basic UFJJVkFURVZBTFVFMTIzNDU2Nzg5",
            "https://user:privatevalue@example.invalid",
        ]
        for secret in secrets:
            with self.subTest(kind=secret.split()[0][:12]):
                self.store.update(self.state["id"], lambda state: state["notes"].append({"kind": "evidence", "text": secret, "at": "today"}))
                with self.assertRaisesRegex(ValueError, "credential-like") as caught:
                    self.export()
                self.assertNotIn(secret, str(caught.exception))
                self.assertFalse(self.output.exists())
                self.store.update(self.state["id"], lambda state: state["notes"].clear())

    def test_import_rejects_secret_after_valid_digest_and_accepts_manual_redaction(self):
        document = self.export()
        document["notes"] = [{"kind": "evidence", "text": "OPENAI_API_KEY=PRIVATEVALUE123456789", "at": "today"}]
        self.write_document(document)
        with self.assertRaisesRegex(ValueError, "credential-like"):
            handoff.import_handoff(self.store, self.output, self.target_project)
        document["notes"][0]["text"] = "OPENAI_API_KEY=[REDACTED]"
        self.write_document(document)
        imported = handoff.import_handoff(self.store, self.output, self.target_project)
        self.assertEqual(imported["notes"][0]["text"], "OPENAI_API_KEY=[REDACTED]")

    def test_sensitive_task_prompt_is_not_silently_rewritten(self):
        self.store.update(self.state["id"], lambda state: state["tasks"][0].update(prompt="Use sk-proj-PRIVATEVALUE12345678901234567890"))
        with self.assertRaisesRegex(ValueError, "credential-like"):
            self.export()
        self.assertIn("sk-proj-", self.store.read(self.state["id"])["tasks"][0]["prompt"])
        self.assertFalse(self.output.exists())

    def test_markdown_escapes_untrusted_html(self):
        self.store.update(self.state["id"], lambda state: state["notes"].append({"kind": "next", "text": '<script>alert("task")</script> [click](file:///secret)', "at": "today"}))
        self.export()
        markdown = self.output.with_suffix(".md").read_text()
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("[click](", markdown)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO check requires POSIX")
    def test_fifo_input_is_rejected_without_waiting_for_a_writer(self):
        os.mkfifo(self.output)
        with self.assertRaisesRegex(ValueError, "regular JSON file"):
            handoff.import_handoff(self.store, self.output, self.target_project)

    def test_import_requires_existing_explicit_project(self):
        self.export()
        with self.assertRaisesRegex(ValueError, "existing directory"):
            handoff.import_handoff(self.store, self.output, self.root / "does-not-exist")
        self.assertEqual(len(self.store.list_runs()), 1)


if __name__ == "__main__":
    unittest.main()
