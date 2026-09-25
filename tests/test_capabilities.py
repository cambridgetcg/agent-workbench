import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_workbench import capabilities


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.project = self.home / "project"
        self.project.mkdir()
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.env = {"PATH": str(self.bin)}
        self.calls = []
        self.version = "Claude Code 2.1.221 private-version-value"
        self.help = "--output-format stream-json --input-format stream-json --replay-user-messages --resume --worktree --include-hook-events --bg --settings --effort"

        def probe(argv, **kwargs):
            self.calls.append((argv, kwargs))
            text = self.version if argv[-1] == "--version" else self.help
            return {"outcome": "exited", "exit_code": 0, "stdout": text, "stderr": "", "signal": None}

        self.run = patch.object(capabilities, "run_owned", side_effect=probe)
        self.run_mock = self.run.start()
        self.addCleanup(self.run.stop)

    def write(self, path, data):
        path = self.home / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def executable(self, name="claude", directory=None):
        path = (directory or self.bin) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
        return path

    def inspect(self, task=None):
        return capabilities.inspect_launch(task or {"harness": "claude"}, self.project,
                                           env=self.env, home=self.home)

    def explain(self):
        return capabilities.explain_configuration(self.project, env=self.env, home=self.home)

    def test_launch_pins_absolute_entry_and_distinguishes_help_from_runtime(self):
        executable = self.executable()
        profile = self.inspect()
        self.assertEqual(profile["executable"], str(executable))
        self.assertEqual(profile["version"]["version"], "2.1.221")
        self.assertEqual(profile["version"]["subject"], "selected_executable")
        self.assertIn("--replay-user-messages", profile["help"]["flags"])
        self.assertEqual(profile["display_executable"], "~/bin/claude")
        structured = profile["capabilities"]["structured_events"]
        self.assertEqual(structured["status"], "supported")
        self.assertFalse(structured["runtime_verified"])
        self.assertIn("advertised", structured["evidence"])
        self.assertEqual(profile["capabilities"]["live_input"]["status"], "unverified")
        self.assertEqual(profile["capabilities"]["native_agents_md"]["status"], "unavailable")
        self.assertNotIn("private-version-value", json.dumps(profile))
        self.assertEqual([call[0][1:] for call in self.calls], [["--version"], ["--help"]])
        self.assertTrue(all(call[1]["cwd"] == self.project and call[1]["timeout"] == 3 for call in self.calls))

    def test_path_drift_and_symlink_aliases_are_distinguished(self):
        first = self.executable()
        second = self.executable(directory=self.home / "second")
        self.env["PATH"] += os.pathsep + str(second.parent)
        profile = self.inspect()
        self.assertEqual(profile["executable"], str(first))
        self.assertEqual(profile["candidate_count"], 2)
        self.assertTrue(any("Multiple distinct" in warning for warning in profile["warnings"]))
        second.unlink()
        second.symlink_to(first)
        profile = self.inspect()
        self.assertFalse(any("Multiple distinct" in warning for warning in profile["warnings"]))

    def test_relative_path_entries_resolve_against_declared_project(self):
        path = self.executable(directory=self.project / "relative-bin")
        self.env["PATH"] = "relative-bin"
        self.assertEqual(self.inspect()["executable"], str(path))

    def test_command_tasks_never_execute_probes_or_read_configuration(self):
        path = self.executable(name="dangerous-command", directory=self.project)
        with patch.object(capabilities, "explain_configuration", side_effect=AssertionError("No config read")):
            profile = self.inspect({"harness": "command", "argv": ["./dangerous-command", "private-argument"]})
        self.assertEqual(profile["executable"], str(path))
        self.assertEqual(profile["version"]["status"], "not_probed")
        self.assertEqual(self.calls, [])
        self.assertNotIn("private-argument", json.dumps(profile))

    def test_wrappers_are_never_executed_and_forwarding_is_unverified(self):
        wrapper = self.executable(name="claude-astra")
        native = self.executable()
        profile = self.inspect({"harness": "astra"})
        self.assertEqual(profile["executable"], str(wrapper))
        self.assertEqual(profile["native"]["executable"], str(native))
        self.assertEqual(profile["version"]["subject"], "native_child_candidate")
        self.assertTrue(all(call[0][0] == str(native) for call in self.calls))
        self.assertEqual(profile["capabilities"]["structured_events"]["status"], "unverified")
        self.assertEqual(profile["capabilities"]["background"]["status"], "unverified")

    def test_codex_exec_help_is_inspected_without_running_a_task(self):
        self.executable(name="codex")
        self.version = "codex-cli 0.99.0"
        self.help = "exec --json --no-daemon --sandbox --ask-for-approval"
        profile = self.inspect({"harness": "codex", "prompt": "private-task-text"})
        self.assertIn(["exec", "--help"], [call[0][1:] for call in self.calls])
        self.assertEqual(profile["capabilities"]["structured_events"]["status"], "supported")
        self.assertEqual(profile["capabilities"]["live_input"]["status"], "unavailable")
        self.assertNotIn("private-task-text", json.dumps(profile))

    def test_missing_executables_and_failed_probes_are_honest(self):
        missing = self.inspect()
        self.assertIsNone(missing["executable"])
        self.assertEqual(missing["status"], "unavailable")
        self.assertEqual(missing["capabilities"]["structured_events"]["status"], "unverified")
        self.assertEqual(self.calls, [])
        self.executable()
        self.run_mock.side_effect = None
        self.run_mock.return_value = {"outcome": "timed_out", "exit_code": -9, "stdout": "", "stderr": "", "signal": None}
        profile = self.inspect()
        self.assertEqual(profile["version"]["status"], "timed_out")
        self.assertEqual(profile["help"]["status"], "timed_out")
        self.assertEqual(profile["capabilities"]["structured_events"]["status"], "unverified")

    def test_interrupted_version_probe_does_not_launch_following_help(self):
        self.executable()
        self.run_mock.side_effect = None
        self.run_mock.return_value = {"outcome": "interrupted", "exit_code": -15, "stdout": "", "stderr": "", "signal": signal.SIGTERM}
        profile = self.inspect()
        self.assertEqual(profile["version"]["status"], "interrupted")
        self.assertEqual(profile["help"]["status"], "interrupted")
        self.assertEqual(self.run_mock.call_count, 1)

    def test_newer_version_does_not_claim_runtime_feature_activation(self):
        self.executable()
        self.version = "Claude Code 2.1.282"
        self.help += " --safe-mode"
        self.write("project/AGENTS.md", "private-instruction-body")
        profile = self.inspect()
        self.assertEqual(profile["capabilities"]["native_agents_md"]["status"], "unverified")
        self.assertEqual(profile["capabilities"]["function_hooks"]["status"], "unverified")
        self.assertFalse(any("predates" in warning for warning in profile["warnings"]))

    def test_configuration_provenance_counts_and_no_sensitive_content(self):
        self.write(".claude/settings.json", {"model": "sonnet", "effortLevel": "low",
            "permissions": {"allow": ["Bash(private-command *)"], "deny": ["Read(private-path)", "private-secret"], "defaultMode": "dontAsk"},
            "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "private-hook-command"}]}], "private-hook-event": []},
            "enabledPlugins": {"agent-workbench@personal": True},
            "env": {"ANTHROPIC_AUTH_TOKEN": "sk-private-secret-token", "ANTHROPIC_MODEL": "opus", "PRIVATE_ENV": "private-environment-value"}})
        self.write("project/.claude/settings.json", {"model": "opus", "effortLevel": "high"})
        self.write("project/.claude/settings.local.json", {"effortLevel": "max"})
        self.write(".codex/config.toml", 'model="gpt-6-astra"\nmodel_reasoning_effort="xhigh"\n')
        self.write("project/.codex/config.toml", 'approval_policy="never"\n')
        self.env.update(ANTHROPIC_API_KEY="private-api-key", CLAUDE_CODE_EFFORT_LEVEL="low", PRIVATE_ENV="private-process-env")
        report = self.explain()
        selected = report["claude"]["inferred_selected"]
        self.assertEqual(selected["model"], {"value": "opus", "source": "$PROJECT/.claude/settings.json"})
        self.assertEqual(selected["effortLevel"]["source"], "$PROJECT/.claude/settings.local.json")
        self.assertEqual(len(report["claude"]["contributing_sources"]["effortLevel"]), 3)
        user = report["claude"]["sources"][0]
        self.assertEqual(user["permissions"]["deny_count"], 2)
        self.assertEqual(user["permissions"]["defaultMode"], "dontAsk")
        self.assertEqual(user["hooks"]["events"], ["PreToolUse"])
        self.assertEqual(user["hooks"]["unknown_event_count"], 1)
        self.assertEqual(report["claude"]["process_model_environment"]["CLAUDE_CODE_EFFORT_LEVEL"], "low")
        self.assertEqual(report["codex"]["inferred_selected"]["approval_policy"]["value"], "never")
        rendered = json.dumps(report)
        for secret in ("private-command", "private-path", "private-secret", "private-hook-command", "private-hook-event",
                       "sk-private-secret-token", "private-environment-value", "private-api-key", "private-process-env"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(report["scope"], "partial")

    def test_authentication_and_instruction_bodies_are_never_opened(self):
        self.write(".claude/CLAUDE.md", "private-instruction-body")
        self.write("project/AGENTS.md", "private-agent-instruction-body")
        self.write(".claude/.credentials.json", {"private-credential": "secret"})
        self.write(".claude.json", {"private-state": "secret"})
        self.write(".codex/auth.json", {"private-credential": "secret"})
        read = capabilities._read
        opened = []

        def checked(path, **kwargs):
            opened.append(str(path))
            self.assertNotIn(path.name, {"CLAUDE.md", "AGENTS.md", ".credentials.json", ".claude.json", "auth.json"})
            return read(path, **kwargs)

        with patch.object(capabilities, "_read", side_effect=checked):
            report = self.explain()
        self.assertFalse(report["instructions"]["contents_read"])
        self.assertFalse(report["authentication_files_read"])
        self.assertTrue(any(row["name"] == "AGENTS.md" for row in report["instructions"]["files"]))
        self.assertNotIn("private-instruction-body", json.dumps(report))
        self.assertTrue(opened)

    def test_old_native_warns_about_discovered_agents_without_claiming_import_status(self):
        self.executable()
        self.write("project/AGENTS.md", "private-body")
        self.write("project/CLAUDE.md", "@AGENTS.md")
        profile = self.inspect()
        self.assertTrue(any("imports were not evaluated" in warning for warning in profile["warnings"]))

    def test_root_overrides_and_malformed_settings(self):
        self.env.update(CLAUDE_CONFIG_DIR="~/claude-other", CODEX_HOME=str(self.home / "codex-other"))
        self.write("claude-other/settings.json", {"model": "private-person@example.invalid"})
        self.write("codex-other/config.toml", "model = [broken")
        self.write("project/.claude/settings.json", [1, 2])
        report = self.explain()
        self.assertEqual(report["claude"]["sources"][0]["source"], "$CLAUDE_CONFIG_DIR/settings.json")
        self.assertEqual(report["codex"]["sources"][0]["status"], "malformed")
        self.assertEqual(report["claude"]["sources"][1]["status"], "malformed")
        self.assertNotIn("private-person@example.invalid", json.dumps(report))

    def test_plugin_metadata_detects_registration_and_source_cache_drift(self):
        cache = self.home / ".claude/plugins/cache/local/workbench/0.1.0"
        self.write(cache / ".claude-plugin/plugin.json", {"name": "workbench", "version": "0.1.0", "hooks": "private-hook-location", "mcpServers": {"private-server": {"command": "private-mcp-command"}}})
        self.write(".claude/plugins/installed_plugins.json", {"version": 2, "plugins": {"workbench@local": [{"scope": "user", "installPath": str(cache), "version": "0.2.0"}]}})
        market = self.home / "market"
        self.write(".claude/plugins/known_marketplaces.json", {"local": {"source": {"source": "directory", "path": str(market)}}})
        self.write("market/.claude-plugin/marketplace.json", {"plugins": [{"name": "workbench", "source": "./workbench"}]})
        self.write("market/workbench/.claude-plugin/plugin.json", {"name": "workbench", "version": "0.3.0"})
        report = self.explain()
        plugin = report["plugins"]["entries"][0]
        self.assertEqual(plugin["registered_cache_consistency"], "mismatch")
        self.assertEqual(plugin["source_cache_consistency"], "mismatch")
        self.assertEqual(plugin["components"], ["hooks", "mcpServers"])
        self.assertIn("do not prove", report["plugins"]["comparison"])
        self.assertNotIn("private-hook-location", json.dumps(report))
        self.assertNotIn("private-mcp-command", json.dumps(report))

    def test_fifo_and_oversized_settings_do_not_block(self):
        path = self.home / ".claude/settings.json"
        path.parent.mkdir()
        os.mkfifo(path)
        self.assertEqual(self.explain()["claude"]["sources"][0]["status"], "not_regular")
        path.unlink()
        path.write_bytes(b" " * (capabilities._LIMIT + 1))
        self.assertEqual(self.explain()["claude"]["sources"][0]["status"], "too_large")

    def test_inspection_is_read_only(self):
        self.executable()
        self.write(".claude/settings.json", {"model": "opus"})
        before = {str(path.relative_to(self.home)): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        self.inspect()
        after = {str(path.relative_to(self.home)): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(before, after)


PROBE_WITH_CHILD = r'''
import json, os, pathlib, subprocess, sys, time
root = pathlib.Path(os.environ["WORKBENCH_PROBE_RECORDS"])
phase = sys.argv[-1].lstrip("-")
(root / (phase + ".leader")).write_text(str(os.getpid()))
child_code = "import json,os,pathlib,signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'group':os.getpgrp()}));time.sleep(30)"
child_path = root / (phase + ".child")
subprocess.Popen([sys.executable, "-c", child_code, str(child_path)])
while not child_path.exists():
    time.sleep(.005)
print("2.1.282 (Claude Code)" if phase == "version" else "--output-format stream-json --input-format stream-json --replay-user-messages", flush=True)
if os.environ.get("WORKBENCH_PROBE_MODE") == "hang":
    time.sleep(30)
'''


class CapabilityOwnedProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.records = self.root / "records"
        self.records.mkdir()
        self.native = self.bin / "claude"
        self.native.write_text(f"#!{sys.executable}\n" + PROBE_WITH_CHILD)
        self.native.chmod(0o700)
        self.env = {**os.environ, "PATH": str(self.bin), "WORKBENCH_PROBE_RECORDS": str(self.records),
                    "CLAUDE_CONFIG_DIR": str(self.root / ".claude"), "CODEX_HOME": str(self.root / ".codex")}
        self.addCleanup(self.cleanup_groups)

    def cleanup_groups(self):
        for path in self.records.glob("*.leader"):
            try:
                os.killpg(int(path.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass

    def assert_groups_stopped(self, expected):
        from agent_workbench.runner import _group_has_running_members
        paths = list(self.records.glob("*.child"))
        self.assertEqual(len(paths), expected)
        for path in paths:
            record = json.loads(path.read_text())
            deadline = time.monotonic() + 2
            while _group_has_running_members(record["group"]) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(_group_has_running_members(record["group"]))

    def test_successful_version_and_help_clean_helpers_after_leader_exit(self):
        profile = capabilities.inspect_launch({"harness": "claude"}, self.project, env=self.env, home=self.root)
        self.assertEqual(profile["version"]["status"], "reported")
        self.assertEqual(profile["version"]["version"], "2.1.282")
        self.assertEqual(profile["help"]["status"], "reported")
        self.assert_groups_stopped(2)

    def test_timeout_cleans_sigterm_ignoring_helper_and_withholds_output(self):
        report, text = capabilities._probe(self.native, ["--version"], self.project,
                                           {**self.env, "WORKBENCH_PROBE_MODE": "hang"})
        self.assertEqual(report["status"], "timed_out")
        self.assertEqual(text, "")
        self.assert_groups_stopped(1)

    def test_sigterm_during_probe_cleans_group_and_prevents_next_probe(self):
        code = "from pathlib import Path; import json,sys; from agent_workbench.capabilities import inspect_launch; print(json.dumps(inspect_launch({'harness':'claude'},Path(sys.argv[1]),home=Path(sys.argv[1]).parent)))"
        driver = subprocess.Popen([sys.executable, "-c", code, str(self.project)],
                                  cwd=Path(__file__).resolve().parents[1], env={**self.env, "WORKBENCH_PROBE_MODE": "hang"},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 3
            while not (self.records / "version.child").exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue((self.records / "version.child").exists())
            driver.send_signal(signal.SIGTERM)
            stdout, stderr = driver.communicate(timeout=4)
            self.assertEqual(driver.returncode, 0, stderr)
            profile = json.loads(stdout)
            self.assertEqual(profile["version"]["status"], "interrupted")
            self.assertEqual(profile["help"]["status"], "interrupted")
            self.assertFalse((self.records / "help.leader").exists())
            self.assert_groups_stopped(1)
        finally:
            if driver.poll() is None:
                driver.kill()
                driver.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
