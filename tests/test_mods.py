import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_workbench import mods


class ModCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plugin = self.root / "plugin"
        (self.plugin / ".claude-plugin").mkdir(parents=True)
        (self.plugin / ".claude-plugin/plugin.json").write_text('{"name":"synthetic-mod"}')
        self.plugin = self.plugin.resolve()
        self.executable = self.root / "claude"
        self.executable.write_text("placeholder")
        self.executable.chmod(0o700)
        self.calls = []
        self.environments = []

    def fake_probe(self, argv, *, environment, cwd, timeout):
        self.calls.append(argv[1:])
        self.environments.append(dict(environment))
        arguments = argv[1:]
        if arguments == ["--version"]:
            text = "2.1.282 (Claude Code)"
        elif arguments == ["plugin", "validate", "--help"]:
            text = "Usage: claude plugin validate [options] <path>\n  --strict Treat warnings as errors"
        elif arguments == ["plugin", "test", "--help"]:
            text = "Usage: claude plugin test [dir]"
        elif arguments[:2] == ["plugin", "validate"]:
            text = "PRIVATE OUTPUT\n✔ Validation passed"
        else:
            text = "PRIVATE TRACE\n 7 pass\n 0 fail\nRan 7 tests across 1 file. [0.1s]"
        return {"outcome": "exited", "exit_code": 0, "text": text}

    def check(self, **kwargs):
        with patch.object(mods, "_probe", side_effect=self.fake_probe):
            return mods.check_mod(self.plugin, executable=self.executable, **kwargs)

    def test_validation_is_separate_from_unrequested_tests(self):
        report = self.check(enable_function_hooks=True)
        self.assertEqual(report["validation"]["status"], "passed")
        self.assertEqual(report["tests"]["diagnostic"], "advertised_not_run")
        self.assertEqual(report["support"], "unverified")
        self.assertTrue(report["ok"])
        self.assertNotIn(["plugin", "test", str(self.plugin)], self.calls)

    def test_explicit_tests_require_positive_recognized_results(self):
        report = self.check(enable_function_hooks=True, run_tests=True)
        self.assertEqual(report["support"], "supported")
        self.assertTrue(report["ok"])
        self.assertEqual(report["tests"]["counts"], {"passed": 7, "failed": 0, "total": 7})
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertLess(self.calls.index(["plugin", "validate", str(self.plugin), "--strict"]),
                        self.calls.index(["plugin", "test", str(self.plugin)]))

    def test_isolation_and_opt_in_are_local_and_do_not_inherit_gate(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "unchanged-config", "CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1"}):
            self.check()
            for environment in self.environments:
                self.assertNotIn("CLAUDE_CODE_ENABLE_FUNCTION_HOOKS", environment)
                self.assertNotEqual(environment["CLAUDE_CONFIG_DIR"], "unchanged-config")
                self.assertEqual(environment["DISABLE_AUTOUPDATER"], "1")
                self.assertEqual(environment["DISABLE_UPDATES"], "1")
                self.assertEqual(environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
            self.assertEqual(os.environ["CLAUDE_CONFIG_DIR"], "unchanged-config")
            self.assertEqual(os.environ["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"], "1")
        self.assertFalse(Path(self.environments[0]["CLAUDE_CONFIG_DIR"]).exists())
        self.environments.clear()
        self.check(enable_function_hooks=True)
        self.assertTrue(all(environment["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"] == "1" for environment in self.environments))

    def test_generic_help_success_is_unavailable(self):
        def fake(argv, **kwargs):
            result = self.fake_probe(argv, **kwargs)
            if argv[1:] == ["plugin", "test", "--help"]:
                result["text"] = "Usage: claude plugin|plugins [options] [command]"
            return result
        with patch.object(mods, "_probe", side_effect=fake):
            report = mods.check_mod(self.plugin, executable=self.executable, run_tests=True)
        self.assertEqual(report["support"], "unavailable")
        self.assertFalse(report["ok"])
        self.assertNotIn(["plugin", "test", str(self.plugin)], self.calls)

    def test_explicit_function_gate_is_unavailable_and_content_free(self):
        def fake(argv, **kwargs):
            result = self.fake_probe(argv, **kwargs)
            if argv[1:] == ["plugin", "test", "--help"]:
                result.update(exit_code=1, text="hooks modules are not turned on in this build yet; set CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1\nPRIVATE")
            return result
        with patch.object(mods, "_probe", side_effect=fake):
            report = mods.check_mod(self.plugin, executable=self.executable, run_tests=True)
        self.assertEqual(report["tests"]["diagnostic"], "function_hooks_disabled")
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_failed_validation_never_executes_host_tests(self):
        def fake(argv, **kwargs):
            result = self.fake_probe(argv, **kwargs)
            if argv[1:3] == ["plugin", "validate"] and "--strict" in argv:
                result.update(exit_code=1, text="PRIVATE validation error")
            return result
        with patch.object(mods, "_probe", side_effect=fake):
            report = mods.check_mod(self.plugin, executable=self.executable, run_tests=True)
        self.assertEqual(report["validation"]["status"], "failed")
        self.assertEqual(report["tests"]["diagnostic"], "validation_required_before_tests")
        self.assertNotIn(["plugin", "test", str(self.plugin)], self.calls)

    def test_zero_tests_and_unknown_output_never_pass(self):
        for text in ("0 pass\n0 fail\nRan 0 tests across 0 files.", "Usage: claude plugin test [dir]", "OK"):
            def fake(argv, **kwargs):
                result = self.fake_probe(argv, **kwargs)
                if argv[1:3] == ["plugin", "test"] and "--help" not in argv:
                    result["text"] = text
                return result
            with self.subTest(text=text), patch.object(mods, "_probe", side_effect=fake):
                report = mods.check_mod(self.plugin, executable=self.executable, run_tests=True)
            self.assertEqual(report["tests"]["status"], "unverified")
            self.assertEqual(report["support"], "unverified")

    def test_native_test_failures_prove_support_not_success(self):
        def fake(argv, **kwargs):
            result = self.fake_probe(argv, **kwargs)
            if argv[1:3] == ["plugin", "test"] and "--help" not in argv:
                result.update(exit_code=1, text="PRIVATE FAILURE\n2 pass\n1 fail\nRan 3 tests across 1 file.")
            return result
        with patch.object(mods, "_probe", side_effect=fake):
            report = mods.check_mod(self.plugin, executable=self.executable, run_tests=True)
        self.assertEqual(report["support"], "supported")
        self.assertEqual(report["tests"]["status"], "failed")
        self.assertFalse(report["ok"])
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_missing_executable_is_reported_without_launch(self):
        with patch.object(mods.shutil, "which", return_value=None), patch.object(mods, "_probe") as probe:
            report = mods.check_mod(self.plugin)
        self.assertEqual(report["support"], "unavailable")
        probe.assert_not_called()

    def test_probe_output_is_file_backed_bounded_and_not_exposed_on_limit(self):
        with patch.object(mods, "OUTPUT_LIMIT", 100):
            report = mods._probe([sys.executable, "-c", "print('PRIVATE'*1000)"],
                                 environment=os.environ.copy(), cwd=self.root, timeout=2)
        self.assertEqual(report["outcome"], "output_limit")
        self.assertEqual(report["text"], "")

    def test_timeout_ends_owned_descendant_and_returns_no_error_text(self):
        marker = self.root / "should-not-exist"
        child = "import time; from pathlib import Path; time.sleep(.6); Path(" + repr(str(marker)) + ").write_text('leaked')"
        parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); print('PRIVATE',flush=True); time.sleep(10)"
        report = mods._probe([sys.executable, "-c", parent], environment=os.environ.copy(), cwd=self.root, timeout=0.15)
        self.assertEqual(report["outcome"], "timed_out")
        self.assertEqual(report["text"], "")
        time.sleep(0.6)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
