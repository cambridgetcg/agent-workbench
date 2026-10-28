import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_workbench import mcp
from agent_workbench.doctor import format_report


SECRETS = ("private-command", "private-argument", "PRIVATE_ENV_VALUE", "private.example", "private-bearer-token")
SERVER = {"command": "private-command", "args": ["private-argument"], "env": {"API_TOKEN": "PRIVATE_ENV_VALUE"}}
REMOTE = {"url": "https://private.example/mcp", "headers": {"Authorization": "Bearer private-bearer-token"}}

class McpInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.project = self.home / "project"
        self.project.mkdir()
        self.env = {}

    def write(self, relative, data):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def inventory(self, harness, **kwargs):
        return mcp.mcp_inventory(harness, self.project, home=self.home, env=self.env, **kwargs)

    def assert_private(self, *reports):
        rendered = json.dumps(reports) + format_report({"mcp_servers": dict(enumerate(reports))})
        for secret in (*SECRETS, str(self.home), "private-person@example.invalid", "other-project-server"):
            self.assertNotIn(secret, rendered)

    def test_claude_user_local_and_project_scopes_are_names_only(self):
        self.write(".claude.json", {"oauthAccount": {"emailAddress": "private-person@example.invalid"},
                                    "mcpServers": {"agentlayer": {"type": "stdio", **SERVER}, "remote": {"type": "http", **REMOTE}},
                                    "projects": {str(self.project): {"mcpServers": {"local-only": SERVER}},
                                                 str(self.home / "other"): {"mcpServers": {"other-project-server": SERVER}}}})
        self.write("project/.mcp.json", {"mcpServers": {"shared": REMOTE}})
        for harness in ("claude", "astra", "astra-ultra"):
            with self.subTest(harness=harness):
                report = self.inventory(harness)
                self.assertEqual(report["status"], "unverified")
                self.assertEqual([(row["source"], row["scope"], [server["name"] for server in row["servers"]]) for row in report["sources"]],
                                 [("~/.claude.json", "user", ["agentlayer", "remote"]),
                                  ("~/.claude.json projects[$PROJECT]", "local", ["local-only"]),
                                  ("$PROJECT/.mcp.json", "project", ["shared"])])
                self.assertEqual(report["server_count"], 4)
                self.assertEqual(report["warnings"], [])
                self.assertEqual(any("Astra wrapper" in text for text in report["limitations"]), harness != "claude")
                self.assert_private(report)

    def test_claude_local_scope_follows_the_git_root_and_config_dir(self):
        self.project = self.home / "repo/sub"
        self.project.mkdir(parents=True)
        (self.home / "repo/.git").write_text("gitdir: elsewhere\n")
        self.env["CLAUDE_CONFIG_DIR"] = "~/claude-other"
        self.write("claude-other/.claude.json", {"projects": {str(self.home / "repo"): {"mcpServers": {"repo-server": SERVER}},
                                                              str(self.project): {"mcpServers": {"subdirectory-key": SERVER}}}})
        self.write(".claude.json", {"mcpServers": {"default-location": SERVER}})
        report = self.inventory("claude")
        self.assertEqual([(row["source"], [server["name"] for server in row["servers"]]) for row in report["sources"]],
                         [("$CLAUDE_CONFIG_DIR/.claude.json", []),
                          ("$CLAUDE_CONFIG_DIR/.claude.json projects[~/repo]", ["repo-server"])])

    def test_codex_servers_report_enabled_and_enabled_tools_count(self):
        self.write(".codex/config.toml", '[mcp_servers.agentlayer]\ncommand = "private-command"\nargs = ["private-argument"]\n'
                                         'enabled_tools = ["collab_next", "task_claim", "task_list"]\n'
                                         '[mcp_servers.agentlayer.env]\nAPI_TOKEN = "PRIVATE_ENV_VALUE"\n'
                                         '[mcp_servers.docs]\nurl = "https://private.example/mcp"\nenabled = false\n')
        self.write("project/.codex/config.toml", '[mcp_servers.project-server]\ncommand = "private-command"\nenabled = "sometimes"\n')
        report = self.inventory("codex")
        self.assertEqual(report["status"], "unverified")
        self.assertEqual([row["servers"] for row in report["sources"]],
                         [[{"name": "agentlayer", "enabled_tools_count": 3}, {"name": "docs", "enabled": False}],
                          [{"name": "project-server", "enabled": "[invalid boolean withheld]"}]])
        self.assertTrue(any("--ignore-user-config" in text for text in report["limitations"]))
        self.assert_private(report)
        self.env["CODEX_HOME"] = str(self.home / "codex-other")
        (self.project / ".codex/config.toml").unlink()
        report = self.inventory("codex")
        self.assertEqual(report["status"], "not_configured")
        self.assertEqual([row["source"] for row in report["sources"]], ["$CODEX_HOME/config.toml", "$PROJECT/.codex/config.toml"])

    def test_hermes_profile_is_located_but_not_parsed(self):
        self.write(".hermes/config.yaml", "mcp_servers:\n  agentlayer:\n    command: private-command\n")
        report = self.inventory("hermes")
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["sources"], [{"source": "~/.hermes/config.yaml", "scope": "profile", "status": "not_inspected",
                                              "server_count": 0, "servers": []}])
        self.assertIn("could not be inspected (not_inspected)", report["warnings"][0])
        self.assert_private(report)
        (self.home / ".hermes/config.yaml").unlink()
        self.assertEqual(self.inventory("hermes")["status"], "not_configured")

    def test_hermes_profile_selection_matches_native_resolution(self):
        root = self.home / "installation"
        for profile, server in (("installation", "root-server"), ("installation/profiles/work", "work-server"),
                                ("installation/profiles/astra-ultra", "astra-server"), (".hermes", "default-server")):
            self.write(profile + "/config.yaml", f"mcp_servers:\n  {server}: {{}}\n")
        def selected(harness, **kwargs):
            report = self.inventory(harness, **kwargs)
            return report["profile_selection"], report["sources"][0]["source"]
        self.assertEqual(selected("hermes"), ("default_home", "~/.hermes/config.yaml"))
        self.env["HERMES_HOME"] = str(root / "profiles/work")
        self.assertEqual(selected("hermes"), ("hermes_home_profile", "~/installation/profiles/work/config.yaml"))
        self.assertEqual(selected("hermes-astra-ultra"), ("astra_ultra_wrapper_candidate", "~/installation/profiles/astra-ultra/config.yaml"))
        self.env["HERMES_HOME"] = str(root)
        self.assertEqual(selected("hermes"), ("hermes_home", "~/installation/config.yaml"))
        self.write("installation/active_profile", "\ufeffWork\n")
        self.assertEqual(selected("hermes"), ("sticky_active_profile", "~/installation/profiles/work/config.yaml"))
        self.write("installation/active_profile", "Default\n")
        self.assertEqual(selected("hermes"), ("hermes_home", "~/installation/config.yaml"))
        self.write("installation/active_profile", "../escape\n")
        report = self.inventory("hermes")
        self.assertEqual(report["profile_selection"], "unverified")
        self.assertIn("active_profile could not be interpreted", report["warnings"][0])
        self.env["HERMES_HOME"] = "./installation/profiles/work"
        self.assertEqual(selected("hermes", cwd=self.home), ("hermes_home_profile", "~/installation/profiles/work/config.yaml"))

    def test_astra_wrapper_candidate_and_safe_mode_are_flagged(self):
        report = self.inventory("hermes-astra-ultra")
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("may use another installation", report["warnings"][0])
        self.assertTrue(any("wrapper was not executed" in text for text in report["limitations"]))
        self.write(".hermes/profiles/astra-ultra/config.yaml", "mcp_servers: {agenttool: {trust: full}}\n")
        self.env["HERMES_SAFE_MODE"] = "1"
        report = self.inventory("hermes-astra-ultra")
        self.assertEqual(report["sources"][0]["status"], "not_inspected")
        self.assertEqual(report["warnings"][0], "HERMES_SAFE_MODE is set; Hermes loads no MCP servers in safe mode.")
        self.assertFalse(any("may use another installation" in text for text in report["warnings"]))
        self.assertEqual(self.inventory("hermes")["status"], "not_configured")

    def test_unreadable_sources_and_entries_warn_without_values(self):
        self.write(".claude.json", {"mcpServers": {"good": SERVER, "bad": "private-command"}})
        self.write(".codex/config.toml", "mcp_servers = [broken")
        claude, codex = self.inventory("claude"), self.inventory("codex")
        self.assertEqual(claude["status"], "incomplete")
        self.assertEqual(claude["sources"][0]["servers"], [{"name": "good"}, {"name": "bad", "status": "malformed"}])
        self.assertEqual(codex["status"], "incomplete")
        self.assertEqual(codex["sources"][0]["status"], "malformed")
        self.assertIn("could not be inspected (malformed)", codex["warnings"][0])
        self.write(".claude.json", {"mcpServers": {f"server-{number}": {} for number in range(mcp._MAX_ROWS + 1)}})
        capped = self.inventory("claude")
        self.assertEqual((capped["status"], capped["server_count"], capped["sources"][0]["server_count"]),
                         ("incomplete", mcp._MAX_ROWS, mcp._MAX_ROWS + 1))
        self.assert_private(claude, codex)

    def test_inventory_only_reads_files(self):
        self.write(".claude.json", {"mcpServers": {"agentlayer": SERVER}})
        self.write(".codex/config.toml", '[mcp_servers.agentlayer]\ncommand = "private-command"\n')
        self.write(".hermes/config.yaml", "mcp_servers:\n  agentlayer:\n    command: private-command\n")
        before = {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        with patch("subprocess.Popen", side_effect=AssertionError("No process may start")):
            reports = [self.inventory(harness) for harness in ("codex", "claude", "astra", "astra-ultra", "hermes", "hermes-astra-ultra")]
        self.assertEqual(before, {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()})
        self.assert_private(*reports)
        with self.assertRaises(ValueError):
            self.inventory("command")


if __name__ == "__main__":
    unittest.main()
