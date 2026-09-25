import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_workbench import evaluation


HELP = """Usage: claude plugin eval [options] [target]
 --ablation <mode> Compare plugin
 --runs <n> Repeat
 --case <glob> Filter
 --max-cost-usd <usd> Budget
 --output-dir <dir> Reports
 --json [path] JSON
 --no-publish Keep local
 --no-scaffold Skip scripts
 --trust-plugin Trust directory
 --model <model> Model
 --judge-model <model> Judge
 --allow-tools <tools...> Grants
"""


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugin = self.root / "plugin"
        (self.plugin / ".claude-plugin").mkdir(parents=True)
        (self.plugin / ".claude-plugin/plugin.json").write_text('{"name":"example","version":"1.0.0"}')
        (self.plugin / "skills/example").mkdir(parents=True)
        (self.plugin / "skills/example/SKILL.md").write_text("abcdefgh")
        (self.plugin / "evals/check").mkdir(parents=True)
        (self.plugin / "evals/check/prompt.md").write_text("Check the example.")
        self.executable = self.root / "claude"
        self.executable.write_text("placeholder")
        self.executable.chmod(0o700)

    def cli(self, argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "2.1.282 (Claude Code)" if argv[-1] == "--version" else HELP, "")

    def plan(self, **kwargs):
        with patch.object(evaluation.subprocess, "run", side_effect=self.cli):
            return evaluation.evaluation_plan(self.plugin, executable=self.executable, **kwargs)

    def report(self):
        return {"schemaVersion": 1, "partial": False, "claudeVersion": "2.1.282", "costUsd": 0.5,
                "durationSeconds": 15, "aggregates": {"overallScore": 1, "meanDelta": 0.5},
                "cases": [{"name": "check", "aggregates": {"score": 1, "delta": 0.5},
                           "arms": {"with": [{"score": 1, "error": None}],
                                    "without": [{"score": 0.5, "error": None}]}}]}

    def test_plan_is_bounded_two_arm_comparison(self):
        plan = self.plan(runs=2)
        self.assertEqual(plan["maximum_agent_runs"], 4)
        self.assertEqual(plan["capabilities"]["publication"], "explicit_no_publish")
        self.assertEqual(plan["ablation"], "with-without")
        self.assertEqual(plan["inventory"]["approximate_tokens"], 2)
        for option in ({"runs": 0}, {"runs": 11}, {"runs": True}, {"timeout_seconds": 3601},
                       {"max_cost_usd": float("nan")}, {"max_cases": 0}):
            with self.subTest(option=option), self.assertRaises(ValueError):
                self.plan(**option)

    def test_counts_all_cases_even_when_filter_would_reduce_them(self):
        (self.plugin / "evals/second").mkdir()
        (self.plugin / "evals/second/case.yaml").write_text("name: custom-name")
        with self.assertRaisesRegex(ValueError, "case directories"):
            self.plan(case="check", max_cases=1)

    def test_custom_native_directory_cannot_escape_case_bound(self):
        (self.plugin / ".claude-plugin/plugin.json").write_text(json.dumps({
            "name": "example", "version": "1.0.0", "experimental": {"evals": "large-other-suite"}}))
        with self.assertRaisesRegex(ValueError, "Custom native eval"):
            self.plan()

    def test_refuses_unknown_publication_and_generic_help_success(self):
        for version, help_text in (("2.1.282", HELP.replace(" --no-publish Keep local", " --publish-report Publish the HTML report")),
                                   ("2.1.221", "Usage: claude plugin [options]")):
            def fake(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, version if argv[-1] == "--version" else help_text, "")
            with patch.object(evaluation.subprocess, "run", side_effect=fake), self.assertRaises(ValueError):
                evaluation.evaluation_capabilities(self.executable)

    def test_accepts_observed_legacy_opt_in_publication(self):
        def fake(argv, **kwargs):
            text = "2.1.221" if argv[-1] == "--version" else HELP.replace(" --no-publish Keep local", " --publish-report Publish the HTML report")
            return subprocess.CompletedProcess(argv, 0, text, "")
        with patch.object(evaluation.subprocess, "run", side_effect=fake):
            self.assertEqual(evaluation.evaluation_capabilities(self.executable)["publication"], "legacy_opt_in_not_requested")

    def test_inventory_never_follows_external_component_links(self):
        outside = self.root / "secret.md"
        outside.write_text("private material")
        (self.plugin / "skills/leak.md").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            evaluation.plugin_inventory(self.plugin)

    def test_inventory_counts_bytes_and_marks_token_estimate(self):
        (self.plugin / "skills/example/SKILL.md").write_text("😀😀😀😀")
        inventory = evaluation.plugin_inventory(self.plugin)
        self.assertEqual(inventory["authoring_bytes"], 16)
        self.assertEqual(inventory["approximate_tokens"], 1)
        self.assertIn("not tokenizer output", inventory["estimate_method"])

    def test_summary_excludes_content_and_rejects_nonfinite_metrics(self):
        native = self.report()
        native["prompts"] = "PRIVATE PROMPT"
        native["costUsd"] = float("nan")
        native["cases"][0]["arms"]["with"][0]["trace"] = "SECRET TRACE"
        summary = evaluation.evaluation_report(native)
        self.assertTrue(summary["eligible_for_comparison"])
        self.assertIsNone(summary["estimated_cost_usd"])
        self.assertNotIn("PRIVATE", json.dumps(summary))
        self.assertNotIn("SECRET", json.dumps(summary))
        self.assertIsNone(summary["cases"][0]["arms"]["with"][0]["duration_seconds"])

    def test_partial_errors_missing_arms_and_skipped_judges_exclude_comparison(self):
        for alteration in ("partial", "error", "missing", "skipped"):
            native = self.report()
            if alteration == "partial":
                native["partial"] = True
            elif alteration == "error":
                native["cases"][0]["arms"]["with"][0]["error"] = "secret arbitrary failure"
            elif alteration == "missing":
                del native["cases"][0]["arms"]["without"]
            else:
                native["cases"][0]["arms"]["with"][0]["skippedPaidGraders"] = True
            with self.subTest(alteration=alteration):
                summary = evaluation.evaluation_report(native)
                self.assertFalse(summary["eligible_for_comparison"])
                self.assertNotIn("secret arbitrary", json.dumps(summary))

    def test_run_uses_private_files_and_never_publishes(self):
        report = self.report()
        script = "#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\na=sys.argv\n"
        script += "if '--version' in a: print('2.1.282 (Claude Code)')\n"
        script += "elif '--help' in a: print(" + repr(HELP) + ")\n"
        script += "else:\n assert '--no-publish' in a\n assert '--publish-report' not in a\n assert a[a.index('--ablation')+1]=='with-without'\n"
        script += " Path(a[a.index('--json')+1]).write_text(json.dumps(" + repr(report) + "))\n"
        self.executable.write_text(script)
        plan = evaluation.evaluation_plan(self.plugin, executable=self.executable)
        summary = evaluation.run_evaluation(plan, state_root=self.root / "state")
        self.assertEqual(summary["execution"], "completed")
        self.assertTrue(summary["eligible_for_comparison"])
        directory = Path(summary["state_directory"])
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / "summary.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((directory / "native-output.log").stat().st_mode & 0o777, 0o600)

    def test_run_rejects_changed_plugin_before_launch(self):
        plan = self.plan()
        (self.plugin / "skills/example/SKILL.md").write_text("changed")
        with patch.object(evaluation.subprocess, "run", side_effect=self.cli), patch.object(evaluation.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "changed"):
                evaluation.run_evaluation(plan, state_root=self.root / "state")
            launch.assert_not_called()

    def test_run_rejects_changed_eval_case_before_launch(self):
        plan = self.plan()
        (self.plugin / "evals/check/prompt.md").write_text("Changed task")
        with patch.object(evaluation.subprocess, "run", side_effect=self.cli), patch.object(evaluation.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(ValueError, "changed"):
                evaluation.run_evaluation(plan, state_root=self.root / "state")
            launch.assert_not_called()

    def test_missing_scores_or_unbalanced_arms_cannot_support_comparison(self):
        for kind in ("score", "arm"):
            native = self.report()
            if kind == "score":
                del native["cases"][0]["arms"]["with"][0]["score"]
            else:
                native["cases"][0]["arms"]["without"].append({"score": 0.5, "error": None})
            self.assertFalse(evaluation.evaluation_report(native)["eligible_for_comparison"])

    def test_unknown_report_schema_and_nonregular_file_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "schema"):
            evaluation.evaluation_report({"schemaVersion": 2, "cases": []})
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "unreadable"):
            evaluation.evaluation_report(fifo)


if __name__ == "__main__":
    unittest.main()
