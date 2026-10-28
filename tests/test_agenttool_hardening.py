"""Second-round review: the refusal, withholding and report edges that remained after the first fixes."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_workbench.agenttool import (LOCAL_COMMANDS, WITHHELD, _hermes_servers, argv_names_collab_session_file,
                                       format_lines, names_collab_session_file, refused_command, wiring,
                                       withhold_session_files)


class RefusalHardeningTests(unittest.TestCase):
    def test_pager_values_bundled_after_digits_are_commands(self):
        for argv in (["git", "grep", "-5Oagenttool-lobby room say r --lease L", "-e", "x"],
                     ["git", "grep", "-5Ocollab-zerone anchor --workspace W", "-e", "x"],
                     ["git", "grep", "-l3O:; agenttool-collab-host end --session S --reason r", "-e", "x"],
                     ["git", "grep", "-i5Oagenttool-lobby", "-e", "x"], ["sh", "-c", "git grep -5Oagenttool-lobby -e x"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(refused_command(argv))
        self.assertIsNone(refused_command(["git", "grep", "-n5", "-e", "collab-zerone anchor"]))

    def test_search_exemption_ends_at_every_separator(self):
        for script in ("grep -q x f; agenttool-lobby room list", "grep -q x f\nagenttool-lobby room list",
                       "grep x f | agenttool-lobby room list", "grep x f || agenttool-lobby room list",
                       "grep x f & agenttool-lobby room list"):
            with self.subTest(script=script):
                self.assertIsNotNone(refused_command(["sh", "-c", script]))

    def test_names_and_script_extensions_match_whatever_the_case_or_runtime(self):
        for argv in (["AGENTTOOL-LOBBY", "room", "list"], ["node", "/opt/agenttool-lobby.mjs", "room", "list"],
                     ["node", "x/agenttool-lobby.cjs"], ["python3", "collab-courier.py", "send"],
                     ["curl", "https://API.AGENTTOOL.DEV/v1/wake"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(refused_command(argv))

    def test_package_selectors_choose_rather_than_run(self):
        for argv in (["bunx", "-p", "@agenttool/lobby", "agenttool-lobby-mcp"],
                     ["npx", "--package=@agenttool/lobby", "agenttool-lobby-mcp"], ["bun", "--filter", "@agenttool/lobby", "test"]):
            with self.subTest(argv=argv):
                self.assertIsNone(refused_command(argv))
        self.assertIsNotNone(refused_command(["npx", "@agenttool/lobby", "room", "list"]))


class SessionFileHardeningTests(unittest.TestCase):
    def test_paths_with_spaces_are_withheld_whole_in_every_spelling(self):
        cases = ["see /Users/jane/Library/Application Support/agenttool/collab-sessions/s1.json now",
                 "see /Users/jane/Library/Application\\ Support/agenttool/collab-sessions/s1.json now",
                 "see C:\\Users\\Jane Doe\\AppData\\agenttool\\collab-sessions\\s1.json now",
                 "see `/Users/jane/My Files/agenttool/collab-sessions/s1.json` now",
                 "see AGENTOOL_COLLAB_SESSION_FILE='/Users/jane/My Journal/peer.json' now",
                 'see {"session_file": "/Users/jane/My Journal/peer.json"} now',
                 "see --session-file '/Users/jane/My Runs/peer.json' now"]
        for text in cases:
            with self.subTest(text=text):
                shown = withhold_session_files(text)
                self.assertIn(WITHHELD, shown)
                for private in ("jane", "Support", "Doe", "Files", "Journal", "Runs"):
                    self.assertNotIn(private, shown)
                self.assertTrue(shown.startswith("see ") and shown.endswith(" now"))

    def test_withholding_keeps_unrelated_words(self):
        self.assertEqual(withhold_session_files("'It works', said Jane, removing /x/collab-sessions/s1.json'"),
                         f"'It works', said Jane, removing {WITHHELD}'")
        self.assertEqual(withhold_session_files("Yu's desk: /x/collab-sessions/s1.json's bearer"),
                         f"Yu's desk: {WITHHELD}'s bearer")
        self.assertEqual(withhold_session_files("See [the file](/x/collab-sessions/s1.json)"), f"See [the file]({WITHHELD})")
        text = "Use --session-file\ndocs/agenttool.md explains it"
        self.assertEqual(withhold_session_files(text), text)

    def test_repeated_keys_stay_linear(self):
        for text in ("x.session_file=null," * 50_000, "session_file=" * 80_000, "AGENTOOL_COLLAB_SESSION_FILE=" * 50_000):
            started = time.monotonic()
            names_collab_session_file(text)
            withhold_session_files(text)
            from agent_workbench.runner import redact_summary
            redact_summary(text, limit=None)
            self.assertLess(time.monotonic() - started, 3, text[:30])

    def test_dollar_paths_are_paths_and_bare_variables_are_not(self):
        path = "$HOME/runs/peer-session.json"
        for argv in (["agenttool-collab-host", "wait", "--session-file", path], ["agenttool-collab-host", "wait", "--session-file=" + path],
                     ["sh", "-c", "agenttool-collab-host wait --session-file " + path],
                     ["agenttool-collab-host", "wait", "--session-file", "${AGENTOOL_COLLAB_SESSION_FILE:-/x/peer.json}"]):
            with self.subTest(argv=argv):
                self.assertTrue(argv_names_collab_session_file(argv))
        self.assertTrue(names_collab_session_file("Resume with AGENTOOL_COLLAB_SESSION_FILE=$HOME/x.json"))
        for value in ("$F", '"$F"', "${F}"):
            with self.subTest(value=value):
                self.assertFalse(names_collab_session_file(f"AGENTOOL_COLLAB_SESSION_FILE={value}"))
                self.assertFalse(argv_names_collab_session_file(["agenttool-collab-host", "wait", "--session-file", value]))
        self.assertFalse(names_collab_session_file('"${AGENTOOL_COLLAB_SESSION_FILE:-}"'))

    def test_session_file_is_an_ordinary_word_away_from_collab(self):
        for text in ("In bot.py, change session_file = 'sessions/bot.json' to read from an env var.",
                     'Fix the parser for config like {"session_file": "./state/run.json"}',
                     "The Telethon client stores its login in session_file: ~/.config/app/user.session"):
            with self.subTest(text=text):
                self.assertFalse(names_collab_session_file(text))
        self.assertTrue(names_collab_session_file("Collab host printed session_file: /x/p.json"))
        for argv in (["python3", "-c", "session_file = '/tmp/app/session.json'; print(session_file)"],
                     ["grep", "-rn", "-e", "--session-file", "docs/"], ["git", "grep", "-n", "-e", "--session-file", "--", "docs"],
                     ["mytool", "--session-file", "bot.session"]):
            with self.subTest(argv=argv):
                self.assertFalse(argv_names_collab_session_file(argv))
        self.assertTrue(argv_names_collab_session_file(["agenttool-collab-host", "wait", "--session-file=peer.json"]))

    def test_terminal_escapes_inside_a_file_name_do_not_hide_it(self):
        from agent_workbench.runner import redact_summary
        for text in ("/x/collab-sessions/\x1b[1mabc\x1b(B\x1b[m.json", "/x/collab-sessions/abc\x1b[m\x0f.json",
                     "/x/collab-sessions\x1b(B/abc.json"):
            with self.subTest(text=repr(text)):
                self.assertIn(WITHHELD, redact_summary(text))
        self.assertEqual(redact_summary("plain text with \x1b[31mred\x1b(B\x1b[m words"), "plain text with red words")

    def test_case_does_not_hide_a_locator(self):
        text = "/x/Collab-Sessions/s.json"
        self.assertTrue(names_collab_session_file(text))
        self.assertTrue(argv_names_collab_session_file(["cat", text]))
        self.assertEqual(withhold_session_files(text), WITHHELD)


class ReportHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-workbench-hardening-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {"PATH": "", "HERMES_MANAGED_DIR": str(self.root / "no-managed-scope")}

    def write(self, relative, text, base=None):
        path = (base or self.home) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def report(self, **env):
        return wiring(home=self.home, project=None, env={**self.env, **env})

    def hermes(self, text):
        self.write(".hermes/config.yaml", text)
        return self.report()["harnesses"]["hermes"]

    def test_tools_that_are_not_a_mapping_register_nothing(self):
        server = "mcp_servers:\n  agenttool:\n    url: https://api.agenttool.dev/v1/mcp\n"
        for tools in ("    tools: [canon.lookup]\n", "    tools:\n      - canon.lookup\n", "    tools: canon.lookup\n", "    tools: true\n"):
            with self.subTest(tools=tools):
                section = self.hermes(server + tools)
                self.assertEqual(section["summary"]["public_agenttool"], "partial")
                self.assertIn("tools_not_mapping", [w["code"] for w in section["servers"][0]["warnings"]])
        for tools in ("    tools:\n", "    tools: false\n", "    tools: []\n"):
            with self.subTest(tools=tools):
                self.assertEqual(self.hermes(server + tools)["summary"]["public_agenttool"], "configured")

    def test_plain_scalars_resolve_like_pyyaml(self):
        canon = "mcp_servers:\n  agenttool_canon:\n    url: https://api.agenttool.dev/v1/mcp/canon\n"
        for include in ("null", "~", "true", "5"):
            with self.subTest(include=include):
                section = self.hermes(canon + f"    tools:\n      include: {include}\n")
                self.assertEqual(section["servers"][0]["tools"]["fit"], "unrestricted")
        for enabled, state in (('"0"', "disabled"), ("'0'", "disabled"), ("0", "configured"), ("no", "disabled")):
            with self.subTest(enabled=enabled):
                self.assertEqual(self.hermes(canon + f"    enabled: {enabled}\n")["summary"]["public_canon"], state)

    def test_server_names_are_unquoted_and_duplicates_spelled_differently_are_unparsed(self):
        block = "  agenttool:\n    url: https://api.agenttool.dev/v1/mcp\n"
        for later in ("'agenttool'", '"agenttool"', "agenttool ", "&a agenttool"):
            with self.subTest(later=later):
                text = "mcp_servers:\n" + block + f"  {later}:\n    enabled: false\n"
                self.assertEqual(_hermes_servers(text), (None, "unparsed"))
        servers, parse = _hermes_servers('mcp_servers:\n  "agenttool":\n    url: x\n')
        self.assertEqual((parse, list(servers)), ("read", ["agenttool"]))

    def test_placeholders_are_unresolved_not_absent(self):
        section = self.hermes("mcp_servers:\n  agenttool:\n    url: ${AT_URL}\n")
        self.assertEqual(section["summary"]["public_agenttool"], "unknown")
        self.assertEqual((section["other_server_count"], section["unresolved_server_count"]), (0, 1))
        self.write(".claude.json", json.dumps({"mcpServers": {"a": {"type": "http", "url": "${AT_URL:-https://api.agenttool.dev/v1/mcp}"}}}))
        report = self.report()
        self.assertEqual(report["harnesses"]["claude"]["summary"]["public_agenttool"], "unknown")
        self.assertEqual(report["suggestions"], [])

    def test_a_url_key_means_http_even_when_unusable(self):
        cases = ("    url:\n    command: bun\n    args: [/x/dist/agenttool-collab-mcp.js]\n",
                 "    url: ''\n    command: agenttool-lobby-mcp\n", "    url: ws://api.agenttool.dev/v1/mcp\n",
                 "    url: //api.agenttool.dev/v1/mcp\n")
        for body in cases:
            with self.subTest(body=body):
                section = self.hermes("mcp_servers:\n  x:\n" + body)
                self.assertNotIn("configured", section["summary"].values())
                self.assertIn("unusable_url", [w["code"] for row in section["servers"] for w in row["warnings"]])

    def test_absence_is_claimed_only_when_no_key_could_hide(self):
        hidden = ("&servers mcp_servers:\n  a:\n    url: x\n", "!!str mcp_servers:\n  a:\n    url: x\n",
                  " mcp_servers:\n  a:\n    url: x\n", "--- {mcp_servers: {a: {url: x}}}\n", "? mcp_servers\n: {a: {url: x}}\n",
                  "base: &b\n  mcp_servers: {}\n<<: *b\n", '"mcp\\x5fservers":\n  a:\n    url: x\n')
        for text in hidden:
            with self.subTest(text=text):
                self.assertNotEqual(_hermes_servers(text)[1], "no_mcp_servers")
        for text in ("model:\n  default: x\nproviders: {}\n", "model: x\n# mcp_servers:\n#   time:\n", ""):
            with self.subTest(text=text):
                self.assertEqual(_hermes_servers(text), ({}, "no_mcp_servers"))
        for ending in ("\r", "\n\r"):
            with self.subTest(ending=repr(ending)):
                text = ending.join(["mcp_servers:", "  a:", "    url: https://api.agenttool.dev/v1/mcp", ""])
                servers, parse = _hermes_servers(text)
                self.assertEqual((parse, servers["a"]["url"]), ("read", "https://api.agenttool.dev/v1/mcp"))

    def test_common_whole_file_errors_make_the_block_unparsed(self):
        block = "mcp_servers:\n  a:\n    url: https://api.agenttool.dev/v1/mcp\n"
        for text in ("model: x: y\n" + block, "model:\n\tdefault: x\n" + block, block + "    note: - x\n"):
            with self.subTest(text=text):
                self.assertEqual(_hermes_servers(text), (None, "unparsed"))
        self.assertIn("YAML error elsewhere", " ".join(self.report()["limitations"]))

    def test_exact_host_bearer_flag_collab_warnings_overrides_and_absent_config(self):
        self.write(".claude.json", json.dumps({"mcpServers": {
            "a": {"type": "http", "url": "https://evil-agenttool.dev/v1/mcp"},
            "b": {"type": "http", "url": "https://api.agenttool.dev.example/v1/mcp/canon"},
            "c": {"type": "http", "url": "https://agenttool.dev/v1/mcp"}}}))
        claude = self.report()["harnesses"]["claude"]
        self.assertEqual((claude["servers"], claude["other_server_count"]), ([], 3))
        self.write(".codex/config.toml", '[mcp_servers.agenttool]\nurl = "https://api.agenttool.dev/v1/mcp"\n'
                                         'bearer_token_env_var = "SOME_PRIVATE_NAME"\n'
                                         '[mcp_servers.collab]\ncommand = "bun"\nargs = ["/var/empty/dist/agenttool-collab-mcp.js"]\n'
                                         'env = { AGENTOOL_COLLAB_SESSION_FILE = "x", AGENTTOOL_COLLAB_DB = "y" }\n')
        codex = self.report()["harnesses"]["codex"]
        rows = {row["kind"]: row for row in codex["servers"]}
        self.assertIn("credentials", [w["code"] for w in rows["public_agenttool"]["warnings"]])
        self.assertNotIn("SOME_PRIVATE_NAME", json.dumps(codex))
        self.assertEqual([w["code"] for w in rows["collab"]["warnings"]], ["fixed_session", "misspelled_journal"])
        elsewhere = self.root / "elsewhere"
        self.write("config.toml", '[mcp_servers.agenttool]\nurl = "https://api.agenttool.dev/v1/mcp"\n', base=elsewhere / "codex")
        self.write(".claude.json", json.dumps({"mcpServers": {"x": {"type": "http", "url": "https://api.agenttool.dev/v1/mcp"}}}),
                   base=elsewhere / "claude")
        (self.home / ".codex/config.toml").unlink()
        (self.home / ".claude.json").unlink()
        self.assertEqual(self.report()["harnesses"]["codex"]["summary"]["public_agenttool"], "not_configured")
        moved = self.report(CODEX_HOME=str(elsewhere / "codex"), CLAUDE_CONFIG_DIR=str(elsewhere / "claude"))
        self.assertEqual(moved["harnesses"]["codex"]["sources"][0]["status"], "read")
        self.assertEqual(moved["harnesses"]["codex"]["summary"]["public_agenttool"], "configured")
        self.assertEqual(moved["harnesses"]["claude"]["summary"]["public_agenttool"], "configured")
        empty = wiring(home=self.root / "empty-home", project=None, env=self.env)
        self.assertEqual(empty["suggestions"], [])

    def test_report_runs_no_command_even_one_it_finds(self):
        bin_dir = self.root / "bin"
        ran = self.root / "ran"
        bin_dir.mkdir()
        ran.mkdir()
        for name in LOCAL_COMMANDS:
            script = bin_dir / name
            script.write_text(f"#!/bin/sh\ntouch {ran / name}\n")
            script.chmod(0o755)
        events = []
        active = {"on": True}

        def audit(event, args):
            if active["on"] and event in {"subprocess.Popen", "os.posix_spawn", "os.exec", "os.system", "os.spawn",
                                          "os.fork", "socket.connect", "socket.getaddrinfo", "sqlite3.connect"}:
                events.append(event)

        sys.addaudithook(audit)
        try:
            with patch("subprocess.Popen", side_effect=AssertionError("no process")) as popen:
                report = self.report(PATH=str(bin_dir))
        finally:
            active["on"] = False
        popen.assert_not_called()
        self.assertEqual(events, [])
        self.assertEqual(set(report["commands"].values()), {"present"})
        self.assertEqual(sorted(path.name for path in ran.iterdir()), [])
        self.assertTrue(format_lines(report))


class RoundThreeTests(unittest.TestCase):
    def test_long_paths_with_spaces_are_withheld_whole(self):
        long_path = "/Users/Jane Doe/Library/Application Support/My Agent Work Space/collab-sessions/s1.json"
        for text in (f'see "{long_path}" now', f"see `{long_path}` now", f"see {long_path} now",
                     "see C:\\Users\\Jane Doe\\OneDrive - Contoso Holdings Ltd\\Collab\\collab-sessions\\s1.json now",
                     "see (/Users/jane/Library/Application Support/agenttool/collab-sessions/s1.json) now",
                     "see path=/Users/jane/Library/Application Support/agenttool/collab-sessions/s1.json now",
                     "see file:///Users/jane/Library/Application Support/agenttool/collab-sessions/s1.json now",
                     "see $HOME/Library/Application Support/agenttool/collab-sessions/s1.json now",
                     "see /Users/jane/Library/Application  Support/agenttool/collab-sessions/s1.json now"):
            with self.subTest(text=text):
                shown = withhold_session_files(text)
                for private in ("Jane", "jane", "Library", "Application", "Contoso", "HOME"):
                    self.assertNotIn(private, shown)
                self.assertTrue(shown.startswith("see ") and shown.endswith(" now"))
        self.assertEqual(withhold_session_files("see (/x/collab-sessions/s.json)"), f"see ({WITHHELD})")
        self.assertEqual(withhold_session_files("see path=/x/collab-sessions/s.json"), f"see path={WITHHELD}")

    def test_include_lists_are_built_the_way_hermes_builds_them(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            config = home / ".hermes/config.yaml"
            config.parent.mkdir(parents=True)
            env = {"PATH": "", "HERMES_MANAGED_DIR": str(home / "none")}
            server = "mcp_servers:\n  agenttool:\n    url: https://api.agenttool.dev/v1/mcp\n    tools:\n"
            for include in ("      include: [null]\n", "      include: [~]\n", "      include: [true]\n", "      include: [5]\n",
                            "      include:\n        - null\n"):
                with self.subTest(include=include):
                    config.write_text(server + include)
                    section = wiring(home=home, project=None, env=env)["harnesses"]["hermes"]
                    self.assertEqual((section["servers"][0]["tools"]["fit"], section["summary"]["public_agenttool"]),
                                     ("different", "partial"))
            config.write_text(server + "      include: ghp_0123456789abcdefghijklmnopqrstuvwxyz\n")
            report = wiring(home=home, project=None, env=env)
            self.assertNotIn("ghp_0123456789", json.dumps(report))

    def test_workbench_marker_is_not_a_mention_of_collab(self):
        self.assertFalse(names_collab_session_file(f"earlier: --session-file {WITHHELD}\nsession_file: ./state/session.json"))
        self.assertTrue(names_collab_session_file("Use collab; session_file: ./x.json"))

    def test_goal_and_prompt_are_checked_together_when_saved(self):
        from agent_workbench.store import validate_manifest
        with self.assertRaises(ValueError):
            validate_manifest({"version": 1, "name": "n", "goal": "Finish the collab mode before Friday.", "tasks": [
                {"id": "w", "harness": "codex", "prompt": "Make the CLI read session_file: ./state/session.json."}]})

    def test_errors_inside_the_block_make_the_whole_block_unparsed(self):
        good = "  agenttool:\n    url: https://api.agenttool.dev/v1/mcp\n"
        for sibling in ("  canon:\n    url: x\n    tools:\n      include:\n        - search\n       - fetch\n",
                        "  github:\n    command: npx\n      args: [x]\n",
                        "  github:\n    command: npx\n    args: [a,,b]\n",
                        '  github:\n    command: npx\n    args: ["^\\\\w+$"]\n',
                        "  github:\n    command: npx\n    args:x\n",
                        "  github:\n    command: npx\n    @note: hi\n",
                        "  ]github:\n    command: npx\n",
                        "  github:\n    env:\n      A: b\n     B: c\n"):
            with self.subTest(sibling=sibling):
                self.assertEqual(_hermes_servers("mcp_servers:\n" + good + sibling), (None, "unparsed"))
        servers, parse = _hermes_servers("mcp_servers:\n" + good + '  ok:\n    command: npx\n    args: [a, "\\x41"]\n')
        self.assertEqual(parse, "partial")  # a YAML-only escape is valid but outside the subset
        servers, parse = _hermes_servers("mcp_servers:\n" + good + "  ok:\n    command: npx\n    args: [a,]\n")
        self.assertEqual((parse, servers["ok"]["args"]), ("read", ["a"]))

    def test_a_partly_read_file_says_hermes_may_load_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            config = home / ".hermes/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text("mcp_servers:\n  agenttool:\n    url: https://api.agenttool.dev/v1/mcp\n"
                              "  multi:\n    command: npx\n    args: [\"-y\",\n      \"pkg\"]\n")
            report = wiring(home=home, project=None, env={"PATH": "", "HERMES_MANAGED_DIR": str(home / "none")})
            self.assertIn("Hermes loads no servers", "\n".join(format_lines(report)))

    def test_redaction_stays_fast_on_a_megabyte_of_hostile_output(self):
        from agent_workbench.runner import redact_summary
        for text in ("password" + " " * 1_000_000, "%7B%22k%22" * 100_000, "a." * 500_000, "0-" * 500_000):
            started = time.monotonic()
            redact_summary(text, limit=2400)
            self.assertLess(time.monotonic() - started, 3, text[:12])


if __name__ == "__main__":
    unittest.main()
