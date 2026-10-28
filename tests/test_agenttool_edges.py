"""Edge cases found by an adversarial review of the AgentTool neighbour."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from agent_workbench.agenttool import (WITHHELD, _hermes_servers, argv_names_collab_session_file, format_lines,
                                       names_collab_session_file, refused_command, wiring, withhold_session_files)
from agent_workbench.store import Store


class PagerAndAssignmentTests(unittest.TestCase):
    def test_git_grep_pager_values_are_commands(self):
        for argv in (["git", "grep", "-Oagenttool-lobby", "-e", "x"], ["git", "grep", "-nOagenttool-lobby", "-e", "x"],
                     ["git", "grep", "--open=agenttool-lobby", "-e", "x"],
                     ["git", "grep", "--open-files-in-pager=collab-zerone anchor --workspace W", "-e", "x"],
                     ["git", "grep", "-lO:; agenttool-collab-host end --session S --reason r", "-e", "x"],
                     ["git", "grep", "-Oagenttool-lobby room say r --lease L", "-e", "x"],
                     ["sh", "-c", "git grep -Ocollab-courier x"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(refused_command(argv))
        self.assertIsNone(refused_command(["git", "grep", "-n", "agenttool-lobby"]))

    def test_assignments_before_a_search_are_checked_and_argv0_alone_makes_a_search(self):
        for argv in (["sh", "-c", "GIT_PAGER=agenttool-lobby git log"],
                     ["script", "-q", "/dev/null", "sh", "-c", "PAGER=agenttool-lobby git log"],
                     ["sh", "-c", "GIT_EXTERNAL_DIFF=agenttool-lobby git log -p --ext-diff"],
                     ["/x y/run", "grep", "agenttool-lobby", "hub", "init"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(refused_command(argv))
        for argv in (["sh", "-c", "LC_ALL=C grep -r agenttool-lobby ."], ["sh", "-c", "X=1 grep agenttool-lobby f"]):
            with self.subTest(argv=argv):
                self.assertIsNone(refused_command(argv))

    def test_pinned_package_specs_are_still_the_same_tool(self):
        for argv in (["npx", "-y", "@agenttool/collab-zerone@0.3.0", "anchor"], ["bunx", "collab-zerone@latest", "anchor"],
                     ["deno", "run", "-A", "npm:@agenttool/collab-courier@0.1.0"], ["npx", "agenttool-lobby@1.0.0", "room", "list"],
                     ["npx", "-y", "agenttool-collab-host@2", "end"], ["npx", "@agenttool/lobby", "room", "list"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(refused_command(argv))
        self.assertIsNone(refused_command(["npx", "-y", "collab-zerone@0.3.0", "status"]))


class SessionFileEdgeTests(unittest.TestCase):
    def test_session_file_options_and_receipts_are_locators(self):
        path = "/var/empty/run/peer-session.json"
        self.assertTrue(argv_names_collab_session_file(["agenttool-collab-host", "wait", "--session-file", path]))
        self.assertTrue(argv_names_collab_session_file(["agenttool-collab-host", "wait", "--session-file=" + path]))
        self.assertTrue(argv_names_collab_session_file(["sh", "-c", "agenttool-collab-host wait --session-file " + path]))
        self.assertFalse(argv_names_collab_session_file(["agenttool-collab-host", "wait", "--session-file", "$F"]))
        receipt = json.dumps({"protocol": "agenttool.collab-host/1", "status": "ended", "session_file": path})
        self.assertTrue(names_collab_session_file(receipt))
        self.assertNotIn(path, withhold_session_files(receipt))
        self.assertFalse(names_collab_session_file('{"session_file_name": "s1.json"}'))

    def test_quoted_paths_with_spaces_are_withheld_whole(self):
        text = 'ended "/var/empty/My Files/collab-sessions/s1.json" fine'
        self.assertEqual(withhold_session_files(text), f'ended "{WITHHELD}" fine')

    def test_detection_and_withholding_stay_fast_on_long_tokens(self):
        token = "a/" * 100_000
        started = time.monotonic()
        self.assertFalse(names_collab_session_file(token))
        self.assertEqual(withhold_session_files(token), token)
        self.assertLess(time.monotonic() - started, 2)

    def test_coloured_paths_are_withheld_after_ansi_codes_are_stripped(self):
        from agent_workbench.runner import redact_summary
        shown = redact_summary("done \x1b[31m/var/empty/x/collab-sessions/s1.json\x1b[0m")
        self.assertNotIn("collab-sessions/s1.json", shown)
        self.assertIn(WITHHELD, shown)

    def test_live_duplex_input_naming_a_session_file_is_refused_and_not_stored(self):
        from agent_workbench.bridge import request_native
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "state")
            state = store.create({"version": 1, "name": "n", "goal": "g", "tasks": [
                {"id": "work", "harness": "claude", "adapter": "duplex", "prompt": "p"}]}, Path(temp))

            def live(state):
                state["status"] = "running"
                task = state["tasks"][0]
                task["status"] = "running"
                task["attempts"] = [{"id": "attempt-1", "status": "running", "duplex_initialized": True, "input_closed": False}]
            store.update(state["id"], live)
            locator = "/var/empty/x/collab-sessions/s1.json"
            with self.assertRaises(ValueError) as caught:
                request_native(store, state["id"], "work", "input", "resume " + locator)
            self.assertNotIn(locator, str(caught.exception))
            self.assertNotIn(locator, json.dumps(store.read(state["id"])))


class WiringEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-workbench-wiring-edges-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {"PATH": "", "HERMES_MANAGED_DIR": str(self.root / "no-managed-scope")}

    def write(self, relative, text):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def report(self, **env):
        return wiring(home=self.home, project=None, env={**self.env, **env})

    def hermes(self, block):
        self.write(".hermes/config.yaml", "mcp_servers:\n" + block)
        return self.report()["harnesses"]["hermes"]

    def test_codex_and_hermes_tool_filters_follow_each_harness(self):
        url = 'url = "https://api.agenttool.dev/v1/mcp"\n'
        self.write(".codex/config.toml", '[mcp_servers.agenttool]\n' + url + 'disabled_tools = ["wake.platform", "canon.lookup"]\n')
        codex = self.report()["harnesses"]["codex"]
        self.assertEqual((codex["servers"][0]["tools"]["fit"], codex["summary"]["public_agenttool"]), ("subset", "partial"))
        self.assertEqual(codex["servers"][0]["tools"]["missing"], ["canon.lookup", "wake.platform"])
        canon = "  agenttool_canon:\n    url: https://api.agenttool.dev/v1/mcp/canon\n    tools:\n"
        cases = {"      include:\n        - search\n        - fetch\n      exclude:\n        - fetch\n": ("exact", "configured"),
                 "      include: []\n": ("unrestricted", "configured"),
                 "      include: search\n": ("subset", "partial"),
                 "      exclude:\n        - fetch\n": ("subset", "partial"),
                 "      exclude: fetch\n": ("subset", "partial")}
        for tools, expected in cases.items():
            with self.subTest(tools=tools):
                section = self.hermes(canon + tools)
                self.assertEqual((section["servers"][0]["tools"]["fit"], section["summary"]["public_canon"]), expected)

    def test_unreadable_places_make_sections_unknown_never_crash_doctor(self):
        locked = self.root / "locked"
        (locked / "hermes").mkdir(parents=True)
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        loop = self.home / ".codex/config.toml"
        loop.parent.mkdir(parents=True)
        loop.symlink_to(loop)
        dist = self.root / "pkg/dist"
        dist.mkdir(parents=True)
        (dist / "agenttool-collab-mcp.js").write_text("")
        self.write(".claude.json", json.dumps({"mcpServers": {
            "collab": {"command": "node", "args": [str(dist / "agenttool-collab-mcp.js")]},
            "odd": {"command": "node", "args": ["/opt/x\u0000y/dist/agenttool-collab-mcp.js"]}}}))
        dist.chmod(0)
        self.addCleanup(dist.chmod, 0o700)
        self.write(".hermes/profiles/astra-ultra/config.yaml",
                   "mcp_servers:\n  deep:\n" + "".join("  " * (i + 2) + f"k{i}:\n" for i in range(400)))
        report = self.report(HERMES_HOME=str(locked / "hermes"))
        self.assertIn("unknown", report["harnesses"]["hermes"]["summary"].values())
        self.assertEqual(report["harnesses"]["codex"]["summary"]["public_agenttool"], "unknown")
        collab = next(row for row in report["harnesses"]["claude"]["servers"] if row["name"] == "collab")
        self.assertEqual(collab["collab"]["host_cli"], "unknown")
        self.assertTrue(format_lines(report))

    def test_enabled_and_credential_words_read_the_way_hermes_reads_them(self):
        section = self.hermes("  agenttool:\n    url: https://api.agenttool.dev/v1/mcp\n    enabled: null\n"
                              "    auth: null\n    headers: ~\n    oauth: false\n"
                              "  agenttool_canon:\n    url: https://api.agenttool.dev/v1/mcp/canon\n    enabled: ~\n")
        self.assertEqual(section["summary"]["public_agenttool"], "configured")
        self.assertEqual(section["summary"]["public_canon"], "configured")
        self.assertFalse(section["servers"][0]["credentials_configured"])
        off = self.hermes("  agenttool:\n    url: https://api.agenttool.dev/v1/mcp\n    enabled: 'off'\n")
        self.assertEqual(off["summary"]["public_agenttool"], "disabled")

    def test_text_shows_the_row_the_summary_trusts(self):
        section = self.hermes("  narrow:\n    url: https://api.agenttool.dev/v1/mcp/canon\n    tools:\n      include: search\n"
                              "  wide:\n    url: https://api.agenttool.dev/v1/mcp/canon\n")
        self.assertEqual(section["summary"]["public_canon"], "configured")
        self.assertIn("public canon all tools (no tool filter)", "\n".join(format_lines(self.report())))

    def test_credentials_in_urls_and_helpers_are_flagged_without_values(self):
        self.write(".codex/config.toml", '[mcp_servers.agenttool]\nurl = "https://api.agenttool.dev/v1/mcp"\n'
                                         'http_headers_helper = "/var/empty/print-private-bearer"\n')
        self.write(".claude.json", json.dumps({"mcpServers": {
            "a": {"type": "http", "url": "https://api.agenttool.dev/v1/mcp/canon", "headersHelper": "/var/empty/print-private-bearer"},
            "b": {"type": "http", "url": "https://api.agenttool.dev/v1/mcp?token=private-query-value"},
            "c": {"type": "http", "url": "https://api.agenttool.dev/v1/private-path-name?x=1"}}}))
        report = self.report()
        for harness in ("codex", "claude"):
            for row in report["harnesses"][harness]["servers"]:
                if row["kind"] != "unrecognised_agenttool_endpoint":
                    with self.subTest(harness=harness, row=row["name"]):
                        self.assertTrue(row["credentials_configured"])
        rendered = json.dumps(report) + "\n".join(format_lines(report))
        for private in ("print-private-bearer", "private-query-value", "private-path-name"):
            self.assertNotIn(private, rendered)

    def test_env_values_never_appear_for_codex_or_claude(self):
        self.write(".codex/config.toml", '[mcp_servers.collab]\ncommand = "bun"\nargs = ["/var/empty/dist/agenttool-collab-mcp.js"]\n'
                                         'env = { AGENTOOL_COLLAB_DB = "/var/empty/codex-private-db" }\n')
        self.write(".claude.json", json.dumps({"mcpServers": {"collab": {
            "command": "bun", "args": ["/var/empty/dist/agenttool-collab-mcp.js"],
            "env": {"AGENTOOL_COLLAB_DB": "/var/empty/claude-private-db"}}}}))
        report = self.report()
        self.assertEqual(report["harnesses"]["codex"]["servers"][0]["environment_names"], ["AGENTOOL_COLLAB_DB"])
        rendered = json.dumps(report)
        self.assertNotIn("codex-private-db", rendered)
        self.assertNotIn("claude-private-db", rendered)

    def test_top_level_spellings_the_subset_cannot_read_are_unknown_not_absent(self):
        server = "{agenttool: {url: 'https://api.agenttool.dev/v1/mcp'}}"
        for text in (f"mcp_servers: {server}\n", '{"mcp_servers": {}}\n', '"mcp_servers":\n  a:\n    url: x\n',
                     "mcp_servers :\n  a:\n    url: x\n", "mcp_servers: &m\n  a:\n    url: x\n",
                     "mcp_servers:\n  a:\n    url: x\nmcp_servers:\n  b:\n    url: y\n",
                     "mcp_servers:\n  a:\n    url: x\n    note: see: docs\n", "mcp_servers:\n  a:\n    url: x\x0c\n"):
            with self.subTest(text=text):
                self.assertEqual(_hermes_servers(text), (None, "unparsed"))
        self.write(".hermes/config.yaml", f"mcp_servers: {server}\n")
        report = self.report()
        self.assertEqual(set(report["harnesses"]["hermes"]["summary"].values()), {"unknown"})
        self.assertNotIn("hermes", {item["harness"] for item in report["suggestions"]})
        servers, parse = _hermes_servers("\ufeffmcp_servers:\n  a:\n    url: https://api.agenttool.dev/v1/mcp\n")
        self.assertEqual((parse, servers["a"]["url"]), ("read", "https://api.agenttool.dev/v1/mcp"))
        servers, parse = _hermes_servers("mcp_servers:\n  a:\n    url:https://x\n  b:\n    url: y\n")
        self.assertEqual((parse, servers["a"], servers["b"]["url"]), ("partial", None, "y"))

    def test_flow_lists_of_arguments_are_read(self):
        dist = self.root / "collab/dist"
        dist.mkdir(parents=True)
        (dist / "agenttool-collab-mcp.js").write_text("")
        (dist.parent / "package.json").write_text(json.dumps({"name": "@agenttool/collab", "version": "0.4.0"}))
        section = self.hermes(f'  collab:\n    command: bun\n    args: ["{dist / "agenttool-collab-mcp.js"}"]\n')
        self.assertEqual(section["summary"]["collab"], "configured")
        self.assertEqual(section["servers"][0]["collab"]["package_version"], "0.4.0")
        self.assertEqual(_hermes_servers('mcp_servers:\n  a:\n    args: ["-y", \'pkg\', plain]\n')[0]["a"]["args"],
                         ["-y", "pkg", "plain"])

    def test_hermes_managed_scope_makes_absence_unknown(self):
        managed = self.root / "managed"
        managed.mkdir()
        self.write(".hermes/config.yaml", "mcp_servers:\n  other:\n    command: foo\n")
        report = self.report(HERMES_MANAGED_DIR=str(managed))
        hermes = report["harnesses"]["hermes"]
        self.assertEqual(hermes["summary"]["public_agenttool"], "unknown")
        self.assertEqual(hermes["sources"][-1], {"source": "$HERMES_MANAGED_DIR/config.yaml", "status": "not_read", "reader": "none"})
        self.assertNotIn("hermes", {item["harness"] for item in report["suggestions"]})
        self.assertIn("managed scope", " ".join(report["limitations"]))


if __name__ == "__main__":
    unittest.main()
