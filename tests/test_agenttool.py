from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

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


CODEX_TOML = '''model = "gpt-6-astra"

[mcp_servers.agenttool]
url = "https://api.agenttool.dev/v1/mcp"
enabled_tools = ["canon.lookup", "canon.by_type", "canon.list_types", "canon.summary", "wake.platform"]

[mcp_servers.castle-understanding]
command = "node"
args = ["/var/empty/castle/server.mjs"]
enabled_tools = ["search", "fetch"]

[mcp_servers.hf-mcp-server]
url = "https://huggingface.co/mcp"
bearer_token_env_var = "HF_PRIVATE_TOKEN_NAME"

[mcp_servers.hermes-tools]
command = "/var/empty/venv/bin/python3"
args = ["-m", "agent.transports.hermes_tools_mcp_server"]
env = { HERMES_QUIET = "1" }
'''

HERMES_YAML = '''model:
  default: gpt-6-astra
  api_key: &key sk-hermessentinelsecret0000000
mcp_servers:
  agenttool_canon:
    url: https://api.agenttool.dev/v1/mcp/canon
    enabled: true
    connect_timeout: 30
    timeout: 60
    tools:
      include:
        - search
        - fetch
      resources: false
      prompts: false
    sampling:
      enabled: false
    elicitation:
      enabled: false
  agenttool:
    url: "https://api.agenttool.dev/v1/mcp"  # public, read-only
    enabled: true
    tools:
      include:
      - canon.lookup
      - canon.by_type
      - canon.list_types
      - canon.summary
      - wake.platform
  collab:
    command: bun
    args:
      - COLLAB_ROOT/dist/agenttool-collab-mcp.js
    env:
      AGENTOOL_COLLAB_DB: /var/empty/secret-db-path
    enabled: true
    supports_parallel_tool_calls: false
    sampling:
      enabled: false
    tools:
      resources: false
      prompts: false
platform_toolsets:
  cli:
  - hermes-cli
'''


class WiringTests(unittest.TestCase):
    def setUp(self):
        from agent_workbench.agenttool import wiring
        self.wiring = wiring
        self.temp = tempfile.TemporaryDirectory(prefix="agent-workbench-wiring-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.project.mkdir(parents=True)
        self.collab = self.root / "collab-0.4.0"
        (self.collab / "dist").mkdir(parents=True)
        (self.collab / "dist/agenttool-collab-mcp.js").write_text("// bundle\n")
        (self.collab / "package.json").write_text(json.dumps({"name": "@agenttool/collab", "version": "0.4.0"}))
        (self.root / "collab-sessions").mkdir()
        (self.root / "collab-sessions/s.json").write_text("{}")
        self.write(".codex/config.toml", CODEX_TOML)
        self.write(".hermes/config.yaml", HERMES_YAML.replace("COLLAB_ROOT", str(self.collab)))
        self.write(".claude.json", json.dumps({
            "oauthAccount": {"emailAddress": "private-person@example.invalid"},
            "primaryApiKey": "sk-ant-claudesentinelsecret000000",
            "projects": {str(self.project): {"mcpServers": {"hf-mcp-server": {"type": "http", "url": "https://huggingface.co/mcp"}}},
                         "/var/empty/other": {"mcpServers": {"agenttool": {"type": "http", "url": "https://api.agenttool.dev/v1/mcp"}}}}}))
        self.env = {"PATH": "/var/empty/bin"}

    def write(self, relative, text):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def report(self, **env):
        return self.wiring(home=self.home, project=self.project, env={**self.env, **env})

    def test_reports_agenttool_servers_by_endpoint_not_name(self):
        report = self.report()
        codex, claude, hermes = (report["harnesses"][name] for name in ("codex", "claude", "hermes"))
        self.assertEqual(codex["summary"], {"public_agenttool": "configured", "public_canon": "not_configured",
                                            "collab": "not_configured", "lobby_view": "not_configured"})
        self.assertEqual(codex["other_server_count"], 3)  # the castle-like search/fetch server is not canon
        self.assertEqual(claude["summary"]["public_agenttool"], "not_configured")  # another project's server is not read
        self.assertEqual(claude["other_server_count"], 1)
        self.assertEqual(hermes["summary"], {"public_agenttool": "configured", "public_canon": "configured",
                                             "collab": "configured", "lobby_view": "not_configured"})
        fits = {row["kind"]: row["tools"]["fit"] for row in hermes["servers"]}
        self.assertEqual(fits, {"public_canon": "exact", "public_agenttool": "exact", "collab": "not_compared"})
        collab = next(row for row in hermes["servers"] if row["kind"] == "collab")
        self.assertEqual(collab["collab"]["package_version"], "0.4.0")
        self.assertEqual(collab["collab"]["host_cli"], "absent")
        self.assertEqual(collab["environment_names"], ["AGENTOOL_COLLAB_DB"])
        self.assertEqual(report["harnesses"]["hermes-astra-ultra"]["sources"][0]["status"], "absent")
        self.write(".claude.json", json.dumps({"mcpServers": {"anything": {"type": "http", "url": "https://api.agenttool.dev/v1/mcp/canon/"}}}))
        renamed = self.report()["harnesses"]["claude"]
        self.assertEqual(renamed["summary"]["public_canon"], "configured")
        self.assertEqual(renamed["servers"][0]["tools"]["fit"], "unrestricted")

    def test_values_other_server_names_and_paths_never_appear(self):
        from agent_workbench.agenttool import format_lines
        report = self.report()
        rendered = json.dumps(report) + "\n".join(format_lines(report))
        for private in ("/var/empty/secret-db-path", "hf-mcp-server", "castle", "hermes-tools", "HF_PRIVATE_TOKEN_NAME",
                        "HERMES_QUIET", "private-person@example.invalid", "sk-ant-claudesentinelsecret", "sk-hermessentinelsecret",
                        str(self.root), "collab-sessions", "huggingface", "gpt-6-astra"):
            self.assertNotIn(private, rendered)

    def test_public_tool_allowlist_fit_is_reported_not_judged(self):
        from agent_workbench.agenttool import _tool_fit
        expected = ("fetch", "search")
        self.assertEqual(_tool_fit(["search"], expected)["fit"], "subset")
        self.assertEqual(_tool_fit(["search"], expected)["missing"], ["fetch"])
        self.assertEqual(_tool_fit(["search", "fetch", "extra"], expected)["unexpected"], ["extra"])
        self.assertEqual(_tool_fit(["search", "other"], expected)["fit"], "different")
        self.assertEqual(_tool_fit(None, expected)["fit"], "unrestricted")
        self.write(".codex/config.toml", '[mcp_servers.agenttool]\nurl = "https://api.agenttool.dev/v1/mcp"\n'
                                         'enabled_tools = ["wake.platform"]\n')
        codex = self.report()["harnesses"]["codex"]
        self.assertEqual(codex["summary"]["public_agenttool"], "partial")
        self.assertEqual(len(codex["servers"][0]["tools"]["missing"]), 4)

    def test_credentials_and_agent_scoped_endpoints_are_flagged(self):
        self.write(".claude.json", json.dumps({"mcpServers": {
            "agenttool": {"type": "http", "url": "https://api.agenttool.dev/v1/mcp", "headers": {"Authorization": "Bearer private-bearer-value"}},
            "mine": {"type": "http", "url": "https://user:pass@api.agenttool.dev/v1/mcp/agents/did:at:privateagent?token=privatequery"}}}))
        report = self.report()
        rows = {row["kind"]: row for row in report["harnesses"]["claude"]["servers"]}
        self.assertTrue(rows["public_agenttool"]["credentials_configured"])
        self.assertIn("credentials", [w["code"] for w in rows["public_agenttool"]["warnings"]])
        self.assertIn("agent_scoped", [w["code"] for w in rows["agent_scoped"]["warnings"]])
        rendered = json.dumps(report)
        for private in ("private-bearer-value", "privateagent", "privatequery", "user:pass", "Authorization"):
            self.assertNotIn(private, rendered)
        self.assertNotIn("claude", {item["harness"] for item in report["suggestions"] if item["surface"] == "public_agenttool"})

    def test_collab_version_comes_from_its_package_file(self):
        hermes = lambda: next(row for row in self.report()["harnesses"]["hermes"]["servers"] if row["kind"] == "collab")
        (self.collab / "dist/agenttool-collab-host.js").write_text("// host\n")
        (self.collab / "package.json").write_text(json.dumps({"name": "@agenttool/collab", "version": "0.5.0"}))
        self.assertEqual(hermes()["collab"], {"package_version": "0.5.0", "host_cli": "present",
                                              "evidence": "files beside the configured entry, not the running process"})
        (self.collab / "package.json").write_text(json.dumps({"name": "something-else", "version": "9.9.9"}))
        self.assertIsNone(hermes()["collab"]["package_version"])
        self.write(".hermes/config.yaml", HERMES_YAML.replace("COLLAB_ROOT", "${CLAUDE_PLUGIN_ROOT}"))
        self.assertEqual(hermes()["collab"]["host_cli"], "unknown")

    def test_launch_environment_and_commands_are_presence_only(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        wake = bin_dir / "agenttool-wake"
        wake.write_text("#!/bin/sh\n")
        wake.chmod(0o755)
        report = self.report(PATH=str(bin_dir), AGENTOOL_COLLAB_SESSION_FILE="/var/empty/aw/s.json", AGENTOOL_COLLAB_DB="")
        environment = report["launch_environment"]
        self.assertEqual(environment["AGENTOOL_COLLAB_SESSION_FILE"], {"status": "present", "launches": "withheld"})
        self.assertEqual(environment["AGENTOOL_COLLAB_DB"], {"status": "empty", "launches": "passed"})
        self.assertEqual(environment["AGENTOOL_COLLAB_CLAUDE_PID"]["status"], "absent")
        self.assertEqual(report["commands"]["agenttool-wake"], "present")
        self.assertEqual(report["commands"]["agenttool-lobby"], "absent")
        self.assertNotIn("/var/empty/aw/s.json", json.dumps(report))

    def test_suggestions_are_public_only_never_applied_and_carry_no_credentials(self):
        before = {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        report = self.report()
        self.assertEqual(before, {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()})
        made = [(item["harness"], item["surface"]) for item in report["suggestions"]]
        self.assertEqual(made, [("codex", "public_canon"), ("claude", "public_agenttool"), ("claude", "public_canon")])
        text = json.dumps(report["suggestions"])
        for forbidden in ("-H", "--header", "bearer", "Bearer", "Authorization", "--lease", "/v1/wake", "collab", "lobby"):
            self.assertNotIn(forbidden, text)
        for item in report["suggestions"]:
            for url in re.findall(r"https://[^\s\"]+", json.dumps(item)):
                self.assertIn(url.rstrip("\\"), ("https://api.agenttool.dev/v1/mcp", "https://api.agenttool.dev/v1/mcp/canon"))
        self.write(".codex/config.toml", CODEX_TOML.replace("castle-understanding", "agenttool_canon"))
        clash = next(item for item in self.report()["suggestions"] if item["harness"] == "codex")
        self.assertIn("[mcp_servers.<choose-a-name>]", clash["snippet"])
        self.write(".codex/config.toml", "this is = not [valid toml")
        self.assertNotIn("codex", {item["harness"] for item in self.report()["suggestions"]})

    def test_report_starts_no_process_opens_no_journal_or_session_file(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        marker = self.root / "lobby-ran"
        lobby = bin_dir / "agenttool-lobby"
        lobby.write_text(f"#!/bin/sh\ntouch {marker}\n")
        lobby.chmod(0o755)
        journal = self.root / "journal/collab.sqlite"
        events = []
        active = {"on": True}
        root = str(self.root)

        def audit(event, args):
            if active["on"] and event in {"open", "os.listdir", "os.scandir", "subprocess.Popen", "os.posix_spawn",
                                          "os.exec", "socket.connect"}:
                if event == "open" and not str(args[0]).startswith(root):
                    return
                events.append((event, str(args[0]) if args else ""))

        sys.addaudithook(audit)
        try:
            with patch("subprocess.Popen", side_effect=AssertionError("no process")):
                report = self.report(PATH=str(bin_dir), AGENTOOL_COLLAB_DB=str(journal))
        finally:
            active["on"] = False
        self.assertEqual(report["commands"]["agenttool-lobby"], "present")
        self.assertFalse(marker.exists())
        self.assertFalse(journal.exists())
        self.assertEqual([e for e in events if e[0] in {"subprocess.Popen", "os.posix_spawn", "os.exec", "socket.connect"}], [])
        opened = {Path(path).name for event, path in events if event == "open"}
        self.assertLessEqual(opened, {"config.toml", ".claude.json", ".mcp.json", "config.yaml", "package.json"})
        self.assertLessEqual({"config.toml", ".claude.json", "config.yaml", "package.json"}, opened)  # the hook saw the reads
        self.assertFalse(any("collab-sessions" in path or "collab.sqlite" in path for _, path in events))


class HermesReaderTests(unittest.TestCase):
    def read(self, text):
        from agent_workbench.agenttool import _hermes_servers
        return _hermes_servers(text)

    def test_real_block_shape_with_unknown_nested_keys_reads_fully(self):
        servers, parse = self.read(HERMES_YAML)
        self.assertEqual(parse, "read")
        self.assertEqual(sorted(servers), ["agenttool", "agenttool_canon", "collab"])
        self.assertEqual(servers["agenttool"]["url"], "https://api.agenttool.dev/v1/mcp")
        self.assertEqual(servers["agenttool"]["tools"]["include"][-1], "wake.platform")
        self.assertEqual(servers["agenttool_canon"]["tools"]["include"], ["search", "fetch"])

    def test_flow_anchor_tag_and_block_scalar_mark_only_that_server_unparsed(self):
        for value in ("{url: x}", "&a https://x", "!!str x", "|", "*alias"):
            with self.subTest(value=value):
                servers, parse = self.read(f"mcp_servers:\n  odd:\n    url: {value}\n  fine:\n    url: https://api.agenttool.dev/v1/mcp\n")
                self.assertEqual(parse, "partial")
                self.assertIsNone(servers["odd"])
                self.assertEqual(servers["fine"]["url"], "https://api.agenttool.dev/v1/mcp")

    def test_tabs_duplicate_servers_or_bad_indentation_mark_block_unparsed_and_surfaces_unknown(self):
        for text in ("mcp_servers:\n\tx:\n\t\turl: y\n", "mcp_servers:\n  a:\n    url: x\n  a:\n    url: y\n",
                     "mcp_servers:\n    a:\n      url: x\n  b:\n    url: y\n"):
            with self.subTest(text=text):
                self.assertEqual(self.read(text), (None, "unparsed"))
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".hermes").mkdir()
            (home / ".hermes/config.yaml").write_text("mcp_servers:\n\tx:\n")
            from agent_workbench.agenttool import wiring
            report = wiring(home=home, project=None, env={"PATH": ""})
            self.assertEqual(set(report["harnesses"]["hermes"]["summary"].values()), {"unknown"})
            self.assertNotIn("hermes", {item["harness"] for item in report["suggestions"]})

    def test_absent_empty_and_other_sections_are_ignored(self):
        self.assertEqual(self.read("model:\n  api_key: &k sk-x\n"), ({}, "no_mcp_servers"))
        self.assertEqual(self.read("mcp_servers: {}\nmodel:\n  x: 1\n"), ({}, "read"))
        servers, parse = self.read("model:\n  api_key: &k sk-hermessentinelsecret0000000\nmcp_servers:\n  a:\n    url: z\n")
        self.assertEqual(parse, "read")
        self.assertNotIn("sk-hermessentinelsecret", repr(servers))

    def test_env_and_header_values_are_dropped_at_parse_time(self):
        servers, _ = self.read("mcp_servers:\n  a:\n    url: https://x\n    headers:\n      Authorization: Bearer sentinel-header-value\n"
                               "    env:\n      AGENTOOL_COLLAB_DB: /var/empty/sentinel-db\n")
        self.assertEqual(servers["a"]["env"], {"AGENTOOL_COLLAB_DB": None})
        self.assertEqual(servers["a"]["headers"], {"Authorization": None})
        self.assertNotIn("sentinel", repr(servers))


if __name__ == "__main__":
    unittest.main()
