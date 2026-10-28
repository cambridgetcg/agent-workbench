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

# The same Hermes servers in the styles Hermes and editors write: PyYAML's block
# style with indentless sequences, a one-line flow document and wrapped flow.
HERMES_EXPECTED = [{"name": "agentlayer", "tools_include_count": 2},
                   {"name": "telescope", "enabled": False, "trust": "untrusted", "tools_include_count": 0},
                   {"name": "public", "enabled": True, "trust": "full"}]
HERMES_BLOCK = """model:
  default: gpt-6-astra  # comment
mcp_servers:
  agentlayer:
    command: private-command
    args:
    - private-argument
    - "--flag: private-argument"
    env:
      API_TOKEN: 'PRIVATE_ENV_VALUE'
    tools:
      include:
      - collab_next
      - task_*
  telescope:
    command: private-command
    enabled: false  # off
    trust: untrusted
    tools: {include: []}
  public:
    url: https://private.example/mcp
    headers:
      Authorization: Bearer private-bearer-token
    enabled: 'yes'
    trust: Full
compression:
  threshold: 0.6
"""
HERMES_FLOW = ("{model: {default: gpt-6-astra}, mcp_servers: {agentlayer: {command: private-command, "
               "args: [private-argument, '--flag: private-argument'], env: {API_TOKEN: \"PRIVATE_ENV_VALUE\"}, "
               "tools: {include: [collab_next, task_*]}}, telescope: {command: private-command, enabled: false, "
               "trust: untrusted, tools: {include: []}}, public: {url: 'https://private.example/mcp', "
               "headers: {Authorization: Bearer private-bearer-token}, enabled: yes, trust: Full}}, "
               "platform_hints: {cli: {append: \"a, b\"}}}\n")
HERMES_WRAPPED = """{model: {default: gpt-6-astra},
  mcp_servers: {agentlayer: {command: private-command, args: [private-argument,
        '--flag: private-argument'], env: {API_TOKEN: PRIVATE_ENV_VALUE}, tools: {include: [
          collab_next, task_*]}},
    telescope: {command: private-command, enabled: false, trust: untrusted, tools: {
        include: []}},
    public: {url: 'https://private.example/mcp', headers: {Authorization: Bearer private-bearer-token},
      enabled: yes, trust: Full}}}
"""


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
        self.write(".mcp.json", {"mcpServers": {"ancestor": SERVER}})
        for harness in ("claude", "astra", "astra-ultra"):
            with self.subTest(harness=harness):
                report = self.inventory(harness)
                self.assertEqual(report["status"], "unverified")
                self.assertEqual([(row["source"], row["scope"], [server["name"] for server in row["servers"]]) for row in report["sources"]],
                                 [("~/.claude.json", "user", ["agentlayer", "remote"]),
                                  ("~/.claude.json projects[$PROJECT]", "local", ["local-only"]),
                                  ("~/.mcp.json", "project", ["ancestor"]),
                                  ("$PROJECT/.mcp.json", "project", ["shared"])])
                self.assertEqual(report["listed_server_count"], 5)
                self.assertTrue(report["account_state_file_read"])
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

    def test_claude_legacy_global_config_is_flagged_not_read(self):
        self.write(".claude.json", {"mcpServers": {"current": SERVER}})
        self.assertFalse(self.inventory("claude")["warnings"])
        self.write(".claude/.config.json", {"mcpServers": {"legacy-server": SERVER}})
        report = self.inventory("claude")
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("~/.claude/.config.json exists; Claude Code may read user MCP servers from it", report["warnings"][0])
        self.assertNotIn("legacy-server", json.dumps(report))
        self.env["CLAUDE_CONFIG_DIR"] = str(self.home / "elsewhere")
        report = self.inventory("claude")
        self.assertEqual((report["status"], report["account_state_file_read"]), ("not_configured", False))

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

    def test_hermes_block_and_flow_yaml_give_the_same_inventory(self):
        for style, text in (("block", HERMES_BLOCK), ("flow", HERMES_FLOW), ("wrapped flow", HERMES_WRAPPED),
                            ("crlf block", HERMES_BLOCK.replace("\n", "\r\n")), ("document marker", "---\n" + HERMES_BLOCK),
                            ("byte order mark", "\ufeff" + HERMES_FLOW)):
            with self.subTest(style=style):
                self.write(".hermes/config.yaml", text)
                report = self.inventory("hermes")
                self.assertEqual(report["status"], "unverified")
                self.assertEqual(report["sources"][0]["status"], "read")
                self.assertEqual(report["sources"][0]["servers"], HERMES_EXPECTED)
                self.assertEqual((report["profile"], report["profile_selection"]), ("~/.hermes", "default_home"))
                self.assert_private(report)

    def test_unrelated_yaml_features_do_not_block_the_inventory(self):
        self.write(".hermes/config.yaml", "base: &base {x: 1}\nother:\n  <<: *base\n  ? ''\n  : value\n"
                                          "prompt: |\n  mcp_servers:\n    fake: {}\nnote: 'quoted\nmcp_servers: {fake: {}}'\n"
                                          "mcp_servers:\n  real:\n    env: &env {A: \"\\x41\"}\n    headers: *env\n"
                                          "    args:\n    - - nested\n    - name: compact\n      value: |\n        enabled: false\n")
        report = self.inventory("hermes")
        self.assertEqual(report["sources"][0]["status"], "read")
        self.assertEqual(report["sources"][0]["servers"], [{"name": "real"}])

    def test_unsupported_or_malformed_yaml_is_unparsed_not_guessed(self):
        cases = {"unterminated flow": "mcp_servers: {a: {enabled: true}\n",
                 "alias server": "mcp_servers:\n  a: &s {enabled: false}\n  b: *s\n",
                 "merge key": "mcp_servers:\n  a:\n    <<: {enabled: false}\n",
                 "flow merge key": "mcp_servers: {a: {<<: {enabled: false}}}\n",
                 "flow merge key in a block server": "mcp_servers:\n  a: {<<: {trust: untrusted}}\n",
                 "flow merge of servers": "mcp_servers: {<<: {a: {}}, b: {}}\n",
                 "flow merge at the top level": "{<<: {mcp_servers: {a: {}}}}\n",
                 "flow alias server": "mcp_servers: {a: &s {enabled: false}, b: *s}\n",
                 "flow tag": "mcp_servers: {a: {enabled: !!str false}}\n",
                 "flow duplicate server": "mcp_servers: {a: {}, a: {}}\n",
                 "deeper row after a flow value": "mcp_servers: {a: {}}\n  b: {}\n",
                 "multi-line quoted flag": "mcp_servers:\n  a:\n    trust: 'unt\n      rusted'\n",
                 "block scalar flag": "mcp_servers:\n  a:\n    enabled: |\n      false\n",
                 "tag": "mcp_servers: !!map\n  a: {}\n",
                 "tab indentation": "mcp_servers:\n\ta: {}\n",
                 "duplicate server": "mcp_servers:\n  a: {}\n  a: {enabled: false}\n",
                 "two documents": "mcp_servers: {a: {}}\n---\nmcp_servers: {b: {}}\n",
                 "stray indentation": "mcp_servers:\n    a: {}\n  b: {}\n",
                 "mapping in a scalar": "mcp_servers:\n  a:\n    trust: full: yes\n",
                 "control character": "mcp_servers:\n  a: {}\nnote: \"\x00\"\n",
                 "invalid UTF-8": b"mcp_servers:\n  a: {}\nnote: \xff\n"}
        for name, text in cases.items():
            with self.subTest(case=name):
                path = self.home / ".hermes/config.yaml"
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(text if isinstance(text, bytes) else text.encode())
                report = self.inventory("hermes")
                self.assertEqual(report["status"], "incomplete")
                self.assertEqual(report["sources"][0]["status"], "unparsed")
                self.assertEqual(report["sources"][0]["servers"], [])
                self.assertIn("could not be inspected (unparsed)", report["warnings"][0])
        self.write(".hermes/config.yaml", "model:\n  default: x\n")
        self.assertEqual(self.inventory("hermes")["status"], "not_configured")
        self.write(".hermes/config.yaml", "mcp_servers:\n  a:\n  b: {}\n")
        report = self.inventory("hermes")
        self.assertEqual((report["status"], report["sources"][0]["servers"]),
                         ("incomplete", [{"name": "a", "status": "malformed"}, {"name": "b"}]))
        self.assertIn("1 MCP server entry", report["warnings"][0])
        # YAML loaders type these differently (0.0 is off, an include of 5 is ignored).
        self.write(".hermes/config.yaml", "mcp_servers:\n  a: {enabled: 0.0}\n  b: {tools: {include: 5}}\n  c: {enabled: off}\n")
        report = self.inventory("hermes")
        self.assertEqual((report["status"], report["sources"][0]["servers"]),
                         ("incomplete", [{"name": "a", "status": "unparsed"}, {"name": "b", "status": "unparsed"},
                                         {"name": "c", "enabled": False}]))
        self.assertIn("2 MCP server entries", report["warnings"][0])

    def test_fixed_corpus_matches_hermes_or_is_unparsed(self):
        # Each expectation was checked once against Hermes's own value rules under
        # PyYAML and under Hermes's ruamel YAML 1.1 loader (see "about").
        corpus = json.loads((Path(__file__).parent / "hermes_mcp_corpus.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(corpus["cases"]), 20)
        for case in corpus["cases"]:
            with self.subTest(case=case["case"]):
                (self.home / ".hermes").mkdir(exist_ok=True)
                (self.home / ".hermes/config.yaml").write_bytes(case["yaml"].encode("utf-8"))
                report = self.inventory("hermes")
                source = report["sources"][0]
                self.assertEqual({"status": source["status"], "servers": source["servers"]}, case["expected"])
                self.assert_private(report)

    def test_hermes_profile_selection_matches_native_resolution(self):
        root = self.home / "installation"
        for profile, server in (("installation", "root-server"), ("installation/profiles/work", "work-server"),
                                ("installation/profiles/astra-ultra", "astra-server"), (".hermes", "default-server")):
            self.write(profile + "/config.yaml", f"mcp_servers:\n  {server}: {{}}\n")
        def selected(harness, **kwargs):
            report = self.inventory(harness, **kwargs)
            return report["profile_selection"], [server["name"] for server in report["sources"][0]["servers"]]
        self.assertEqual(selected("hermes"), ("default_home", ["default-server"]))
        self.env["HERMES_HOME"] = str(root / "profiles/work")
        self.assertEqual(selected("hermes"), ("hermes_home_profile", ["work-server"]))
        self.assertEqual(selected("hermes-astra-ultra"), ("astra_ultra_wrapper_candidate", ["astra-server"]))
        self.env["HERMES_HOME"] = str(root)
        self.assertEqual(selected("hermes"), ("hermes_home", ["root-server"]))
        self.write("installation/active_profile", "\ufeffWork\n")
        self.assertEqual(selected("hermes"), ("sticky_active_profile", ["work-server"]))
        self.write("installation/active_profile", "Default\n")
        self.assertEqual(selected("hermes"), ("hermes_home", ["root-server"]))
        self.write("installation/active_profile", "../escape\n")
        report = self.inventory("hermes")
        self.assertEqual(report["profile_selection"], "active_profile_uninterpretable")
        self.assertIn("active_profile could not be interpreted", report["warnings"][0])
        self.env["HERMES_HOME"] = "./installation/profiles/work"
        self.assertEqual(selected("hermes", cwd=self.home), ("hermes_home_profile", ["work-server"]))

    def test_astra_wrapper_candidate_and_safe_mode_are_flagged(self):
        report = self.inventory("hermes-astra-ultra")
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("may use another installation", report["warnings"][0])
        self.assertTrue(any("wrapper was not executed" in text for text in report["limitations"]))
        self.env["HERMES_SAFE_MODE"] = "yes"
        line = format_report({"mcp_servers": {"hermes-astra-ultra": self.inventory("hermes-astra-ultra")}})
        self.assertTrue(line.endswith("may use another installation. (+1 more in JSON)"))
        self.write(".hermes/profiles/astra-ultra/config.yaml", "mcp_servers: {agenttool: {trust: full}}\n")
        self.env["HERMES_SAFE_MODE"] = "1"
        report = self.inventory("hermes-astra-ultra")
        self.assertEqual(report["status"], "unverified")
        self.assertEqual(report["warnings"], ["HERMES_SAFE_MODE is set; Hermes loads no MCP servers in safe mode."])
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
        self.assertEqual((capped["status"], capped["listed_server_count"], capped["sources"][0]["server_count"]),
                         ("incomplete", mcp._MAX_ROWS, mcp._MAX_ROWS + 1))
        self.assert_private(claude, codex)

    def test_deeply_nested_files_are_malformed_not_fatal(self):
        # A repository controls .mcp.json and .codex/config.toml in its own tree.
        self.write("project/.mcp.json", '{"mcpServers": ' + "[" * 200_000 + "]" * 200_000 + "}")
        self.write("project/.codex/config.toml", "mcp_servers = " + "[" * 200_000 + "]" * 200_000 + "\n")
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                report = self.inventory(harness)
                self.assertEqual(report["status"], "incomplete")
                self.assertEqual(report["sources"][-1]["status"], "malformed")

    def test_only_mcp_configuration_files_are_opened(self):
        self.env.update(AGENTOOL_COLLAB_SESSION_FILE=str(self.write("collab/session.json", {"token": "private-bearer-token"})),
                        HERMES_HOME=str(self.home / ".hermes"))
        for path in (".claude.json", ".mcp.json", "project/.mcp.json", ".codex/config.toml", ".hermes/config.yaml",
                     ".hermes/active_profile", ".claude/.credentials.json", ".claude/sessions/1.json", ".codex/auth.json",
                     ".hermes/auth.json", ".hermes/.env"):
            self.write(path, {"mcpServers": {}} if path.endswith(".json") else "")
        opened = []
        def recorded(function):
            def read(path, *args, **kwargs):
                opened.append(Path(path))
                return function(path, *args, **kwargs)
            return read
        with patch.object(mcp, "_read", recorded(mcp._read)), patch.object(mcp, "_read_bytes", recorded(mcp._read_bytes)):
            for harness in ("codex", "claude", "astra", "astra-ultra", "hermes", "hermes-astra-ultra"):
                self.inventory(harness)
        self.assertTrue({path.name for path in opened} <= {".claude.json", ".mcp.json", "config.toml", "active_profile", "config.yaml"},
                        sorted({path.name for path in opened}))
        self.assertEqual(sum(path.name == ".claude.json" for path in opened), 3)

    def test_inventory_only_reads_files(self):
        self.write(".claude.json", {"mcpServers": {"agentlayer": SERVER}})
        self.write(".codex/config.toml", '[mcp_servers.agentlayer]\ncommand = "private-command"\n')
        self.write(".hermes/config.yaml", HERMES_BLOCK)
        before = {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        with patch("subprocess.Popen", side_effect=AssertionError("No process may start")):
            reports = [self.inventory(harness) for harness in ("codex", "claude", "astra", "astra-ultra", "hermes", "hermes-astra-ultra")]
        self.assertEqual(before, {path: path.read_bytes() for path in self.home.rglob("*") if path.is_file()})
        self.assert_private(*reports)
        with self.assertRaises(ValueError):
            self.inventory("command")


if __name__ == "__main__":
    unittest.main()
