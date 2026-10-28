"""Read-only collab attribution against synthetic journals and fake homes."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_workbench import collab
from agent_workbench.cli import main
from agent_workbench.store import Store


ATTEMPT = "0123456789abcdef0123456789abcdef"
OTHER_ATTEMPT = "fedcba9876543210fedcba9876543210"
TOKEN_HASH = "TOKEN_HASH_SENTINEL_d2a1c0"
CURSOR = "CURSOR_SENTINEL_77"
ACTOR = "PRIVATE_ACTOR_SENTINEL@studio"
LABEL = " \u00b7 workbench job "
# Variables that would point resolution at a real home; tests always clear them.
HOST_VARIABLES = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HERMES_HOME", "AGENTOOL_COLLAB_DB")


def session_id(number):
    return f"session_{number:08x}-0000-4000-8000-{number:012x}"


def build_journal(path, *, application_id=collab.APPLICATION_ID, newer=False, drop_column=None):
    """A WAL-mode journal with collab 0.5.0's selected columns plus secret ones."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(f"PRAGMA application_id={application_id}")
    connection.execute("PRAGMA user_version=3")
    connection.execute("CREATE TABLE workspaces (id TEXT PRIMARY KEY, repository_key TEXT NOT NULL, created_at TEXT)")
    connection.execute("CREATE TABLE worktrees (id TEXT PRIMARY KEY, workspace_id TEXT, root_path TEXT NOT NULL)")
    columns = ["id TEXT PRIMARY KEY", "workspace_id TEXT", "worktree_id TEXT", "actor TEXT", "parent_session_id TEXT",
               "status TEXT", "joined_at TEXT", "last_seen_at TEXT", "ended_at TEXT", "token_hash TEXT",
               "cursor_event_id TEXT", "cursor_anchor TEXT"]
    columns = [column for column in columns if column.split()[0] != drop_column]
    connection.execute(f"CREATE TABLE coordination_sessions ({', '.join(columns)})")
    if newer:
        connection.execute("CREATE TABLE session_lease_lapses (id TEXT)")
    connection.commit()
    return connection


def add_session(connection, number, *, root, actor=ACTOR, joined="2026-10-28T10:01:00.000Z", status="ended",
                parent=None, ended="2026-10-28T10:02:00.000Z", workspace="ws_1", repository_key="local-git:" + "0" * 64,
                raw_id=None):
    connection.execute("INSERT OR IGNORE INTO workspaces (id, repository_key) VALUES (?, ?)", (workspace, repository_key))
    worktree = "wt_" + str(abs(hash(root)))
    connection.execute("INSERT OR IGNORE INTO worktrees (id, workspace_id, root_path) VALUES (?, ?, ?)", (worktree, workspace, root))
    connection.execute("INSERT INTO coordination_sessions (id, workspace_id, worktree_id, actor, parent_session_id, status, "
                       "joined_at, last_seen_at, ended_at, token_hash, cursor_event_id, cursor_anchor) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (raw_id or session_id(number), workspace, worktree, actor, parent, status, joined, joined, ended,
                        TOKEN_HASH, CURSOR, CURSOR))
    connection.commit()


class CollabTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = Store(self.root / "state")
        self.journal = self.root / "share" / "collab-v3.sqlite"
        bundle = self.root / "pkg" / "dist" / "agenttool-collab-mcp.js"
        bundle.parent.mkdir(parents=True)
        bundle.write_text("// synthetic bundle")
        (self.root / "pkg" / "package.json").write_text(json.dumps({"name": "@agenttool/collab", "version": "0.5.0"}))
        self.bundle = bundle
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}

    def register_claude(self, db=None, *, name="agenttool-collab", config=None, extra=None):
        path = config or self.home / ".claude.json"
        data = json.loads(path.read_text()) if path.exists() else {"oauthAccount": {"emailAddress": "PRIVATE@example.invalid"}}
        env = {"GIT_OPTIONAL_LOCKS": "0"}
        if db is not False:
            env["AGENTOOL_COLLAB_DB"] = str(db or self.journal)
        data.setdefault("mcpServers", {})[name] = {"type": "stdio", "command": "bun", "args": ["run", str(self.bundle)], "env": env,
                                                   **(extra or {})}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def register_codex(self, db=None, root=None):
        root = root or self.home / ".codex"
        root.mkdir(parents=True, exist_ok=True)
        (root / "config.toml").write_text(
            'model = "gpt-6-astra"\n[mcp_servers.agenttool-collab]\ncommand = "bun"\n'
            f'args = ["run", {json.dumps(str(self.bundle))}]\n'
            f'[mcp_servers.agenttool-collab.env]\nAGENTOOL_COLLAB_DB = {json.dumps(str(db or self.journal))}\n')

    def register_hermes(self, db=None, profile="astra-ultra", active="astra-ultra", body=None):
        hermes = self.home / ".hermes"
        directory = hermes if profile == "default" else hermes / "profiles" / profile
        directory.mkdir(parents=True, exist_ok=True)
        if active is not None:
            (hermes / "active_profile").write_text(active + "\n")
        body = body if body is not None else (
            "mcp_servers:\n  agenttool:\n    command: bun\n    args:\n    - run\n"
            f"    - {json.dumps(str(self.bundle))}\n    env:\n      AGENTOOL_COLLAB_DB: {json.dumps(str(db or self.journal))}\n")
        (directory / "config.yaml").write_text(
            "providers:\n  chatgpt-subscription:\n    key_cmd: SENTINEL_KEY_CMD\n    extra_headers:\n"
            "      ChatGPT-Account-ID: SENTINEL_ACCOUNT_ID\n" + body + "approvals:\n  mode: smart\n")

    def run_with(self, attempts, *, harness="claude", workspace=None, coordination=None):
        task = {"id": "work", "harness": harness, "prompt": "Work."}
        if workspace:
            task["workspace"] = workspace
        state = self.store.create({"version": 1, "name": "Collab", "goal": "Work.", "tasks": [task]}, self.project)
        def add(current):
            current["tasks"][0]["attempts"].extend(attempts)
            current["tasks"][0]["status"] = "succeeded"
            if coordination is not None:
                current["coordination"] = coordination
        return self.store.update(state["id"], add)

    def attempt(self, ident=ATTEMPT, *, started="2026-10-28T10:00:00.123456+00:00", finished="2026-10-28T10:05:00+00:00",
                workspace=None):
        attempt = {"id": ident, "status": "succeeded", "started_at": started, "finished_at": finished}
        if workspace is not None:
            attempt["workspace"] = workspace
        return attempt

    def worktree(self, run_root, ident=ATTEMPT):
        return {"mode": "worktree", "owned": True, "root": str(run_root / f"work-{ident}"), "path": str(run_root / f"work-{ident}")}

    def assert_private(self, rendered):
        for private in (TOKEN_HASH, CURSOR, ACTOR, "PRIVATE@example", "SENTINEL_KEY_CMD", "SENTINEL_ACCOUNT_ID",
                        str(self.journal), str(self.journal.parent), str(self.bundle), str(self.home)):
            self.assertNotIn(private, rendered)


class AttributionTests(CollabTestCase):
    def test_worktree_attempts_attribute_by_root_and_window_without_a_label(self):
        connection = build_journal(self.journal)
        self.addCleanup(connection.close)
        self.register_claude()
        trees = self.root / "state" / "trees"
        root = os.path.realpath(trees / f"work-{ATTEMPT}")
        add_session(connection, 1, root=root, actor="hermes-astra-ultra-invited@studio")
        add_session(connection, 2, root=root, joined="2026-10-28T10:05:05.001Z")
        add_session(connection, 3, root=os.path.realpath(trees / "elsewhere"), actor=ACTOR + LABEL + ATTEMPT)
        add_session(connection, 4, root=root, actor=ACTOR + LABEL + ATTEMPT, status="active", ended=None)
        # Written while the writer stays open: the row lives only in the -wal file.
        self.assertTrue(Path(str(self.journal) + "-wal").exists())
        state = self.run_with([self.attempt(workspace=self.worktree(trees))], workspace="worktree")
        view = collab.attribution(state, home=self.home, env=self.env)
        attempt = view["attempts"][0]
        self.assertEqual(attempt["status"], "recorded")
        self.assertEqual([session["id"] for session in attempt["sessions"]], [session_id(1), session_id(4)])
        self.assertEqual([(s["label_match"], s["label_exact"]) for s in attempt["sessions"]], [(False, False), (True, True)])
        self.assertEqual(attempt["excluded"], {"outside_window": 1, "time_unparseable": 0, "invalid_session_id": 0, "root_mismatch": 1})
        self.assertEqual((attempt["left_active"], view["left_active"]), (1, 1))
        self.assertEqual(view["journals"], [{"harnesses": ["claude"], "status": "read", "failure": None,
                                             "basename": "collab-v3.sqlite", "user_version": 3, "newer_tables": False}])
        self.assertEqual(view["coordination"], {"collab": False, "parent_session_id": None, "parent_check": None})
        text = collab.format_attribution(view)
        self.assertIn("1 collab session(s) still active; Workbench does not end sessions. The launching host may end one "
                      "with collab's own host end command (see collab's README).", text)
        self.assertIn(f"{session_id(4)}  active", text)
        self.assert_private(json.dumps(view) + text)

    def test_journal_identity_is_checked_before_any_session_is_read(self):
        state = self.run_with([self.attempt()])
        def failure():
            return collab.attribution(state, home=self.home, env=self.env)["attempts"][0]["failure"]
        self.register_claude()
        self.assertEqual(failure(), "journal_missing")
        self.journal.parent.mkdir(parents=True)
        self.journal.write_text("This file is a deliberate placeholder, not a database.\n" * 12)
        self.assertEqual(failure(), "journal_not_sqlite")
        self.journal.unlink()
        real = self.root / "share" / "real.sqlite"
        build_journal(real).close()
        self.journal.symlink_to(real)
        self.assertEqual(failure(), "journal_not_regular")
        self.journal.unlink()
        build_journal(self.journal, application_id=1234).close()
        self.assertEqual(failure(), "journal_not_collab")
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.journal) + suffix).unlink(missing_ok=True)
        build_journal(self.journal, drop_column="ended_at").close()
        self.assertEqual(failure(), "journal_schema_unknown")
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.journal) + suffix).unlink(missing_ok=True)
        connection = build_journal(self.journal, newer=True)
        add_session(connection, 1, root="/elsewhere", actor="x" + LABEL + ATTEMPT)
        connection.close()
        view = collab.attribution(state, home=self.home, env=self.env)
        self.assertEqual(view["journals"][0]["newer_tables"], True)
        self.assertEqual(view["attempts"][0]["session_count"], 1)
        self.assertIn("newer tables yes", collab.format_attribution(view))

    def test_milliseconds_z_and_microsecond_offsets_are_compared_as_times(self):
        connection = build_journal(self.journal)
        self.addCleanup(connection.close)
        self.register_claude()
        labelled = "agent" + LABEL
        for number, joined in enumerate(("2026-10-28T10:00:00.123Z", "2026-10-28T10:00:00.122Z",
                                         "2026-10-28T10:05:05.000Z", "2026-10-28T10:05:05.001Z",
                                         "not a time", "2026-10-28T10:01:00"), 1):
            add_session(connection, number, root="/shared", actor=labelled + ATTEMPT, joined=joined)
        # Workbench writes no fraction when microseconds are zero.
        for number, joined in enumerate(("2026-10-28T11:00:00.000Z", "2026-10-28T10:59:59.999Z"), 20):
            add_session(connection, number, root="/shared", actor=labelled + OTHER_ATTEMPT, joined=joined)
        state = self.run_with([self.attempt(), self.attempt(OTHER_ATTEMPT, started="2026-10-28T11:00:00+00:00",
                                                            finished="2026-10-28T11:00:30+00:00")])
        first, second = collab.attribution(state, home=self.home, env=self.env)["attempts"]
        self.assertEqual([s["id"] for s in first["sessions"]], [session_id(1), session_id(3)])
        self.assertEqual(first["excluded"]["outside_window"], 2)
        self.assertEqual(first["excluded"]["time_unparseable"], 2)
        self.assertEqual([s["id"] for s in second["sessions"]], [session_id(20)])
        self.assertEqual(second["excluded"]["outside_window"], 1)
        self.assertEqual(collab.parse_time("2026-10-28T10:00:00+00:00").microsecond, 0)
        self.assertIsNone(collab.parse_time("2026-10-28T10:00:00"))

    def test_running_attempt_window_ends_now_and_bad_attempt_times_are_reported(self):
        connection = build_journal(self.journal)
        self.addCleanup(connection.close)
        self.register_claude()
        add_session(connection, 1, root="/shared", actor="a" + LABEL + ATTEMPT, joined="2026-10-28T10:01:00.000Z")
        add_session(connection, 2, root="/shared", actor="a" + LABEL + ATTEMPT, joined="2999-01-01T00:00:00.000Z")
        state = self.run_with([self.attempt(finished=None), self.attempt(OTHER_ATTEMPT, started="yesterday")])
        running, broken = collab.attribution(state, home=self.home, env=self.env)["attempts"]
        self.assertEqual([s["id"] for s in running["sessions"]], [session_id(1)])
        self.assertEqual(running["excluded"]["outside_window"], 1)
        self.assertEqual((broken["status"], broken["failure"]), ("unavailable", "attempt_time_unparseable"))

    def test_shared_mode_needs_the_exact_attempt_suffix_matched_in_python(self):
        connection = build_journal(self.journal)
        self.addCleanup(connection.close)
        self.register_claude()
        actors = {
            1: "astra" + LABEL + ATTEMPT,                 # exact suffix
            2: "astra workbench job " + ATTEMPT,          # paraphrased separator: suffix only
            3: "astra" + LABEL + ATTEMPT.upper(),         # LIKE is case-insensitive; endswith is not
            4: "astra" + LABEL + ATTEMPT + ".",           # trailing punctuation
            5: "astra" + LABEL + ATTEMPT[:-1] + "_",      # LIKE wildcard where a character should be
            6: "astra" + LABEL + "%",
            7: "%" + ATTEMPT[1:],
            8: "_" * 32,
            9: ACTOR,                                     # same root, no label
        }
        for number, actor in actors.items():
            add_session(connection, number, root=str(self.project), actor=actor)
        add_session(connection, 10, root=str(self.project), actor="x" + LABEL + ATTEMPT, raw_id="session_NOT-VALID")
        for workspace in ("shared", "read-only"):
            with self.subTest(workspace=workspace):
                state = self.run_with([self.attempt()], workspace=workspace)
                attempt = collab.attribution(state, home=self.home, env=self.env)["attempts"][0]
                self.assertEqual(attempt["mode"], workspace)
                self.assertEqual([(s["id"], s["label_exact"]) for s in attempt["sessions"]],
                                 [(session_id(1), True), (session_id(2), False)])
                self.assertEqual(attempt["excluded"]["invalid_session_id"], 1)
                self.assertNotIn("NOT-VALID", json.dumps(attempt))

    def test_session_cap_parent_matches_and_still_active_count(self):
        connection = build_journal(self.journal)
        self.addCleanup(connection.close)
        self.register_claude()
        parent = session_id(999)
        for number in range(1, 26):
            add_session(connection, number, root="/shared", actor="a" + LABEL + ATTEMPT,
                        joined=f"2026-10-28T10:01:{number:02d}.000Z", status="active" if number % 5 == 0 else "ended",
                        parent=parent if number == 1 else session_id(998) if number == 2 else None)
        state = self.run_with([self.attempt()], coordination={"collab": True, "parent_session_id": parent, "parent_check": "matched"})
        view = collab.attribution(state, home=self.home, env=self.env)
        attempt = view["attempts"][0]
        self.assertEqual((len(attempt["sessions"]), attempt["session_count"], attempt["truncated"]), (20, 25, True))
        self.assertEqual(attempt["left_active"], 5)
        self.assertEqual([s["parent_matches"] for s in attempt["sessions"][:3]], [True, False, False])
        self.assertEqual(view["coordination"], {"collab": True, "parent_session_id": parent, "parent_check": "matched"})
        text = collab.format_attribution(view)
        self.assertIn("showing the first 20", text)
        self.assertIn("5 collab session(s) still active", text)
        self.assertIn(f"declared parent {parent} (unverified lineage, check matched)", text)
        undeclared = collab.attribution(self.run_with([self.attempt()]), home=self.home, env=self.env)
        self.assertEqual({s["parent_matches"] for s in undeclared["attempts"][0]["sessions"]}, {None})

    @unittest.skipUnless(sys.platform == "darwin" and os.path.islink("/tmp"), "macOS /tmp symbolic link")
    def test_worktree_root_is_compared_after_realpath(self):
        connection = build_journal(self.journal)
        self.addCleanup(connection.close)
        self.register_claude()
        # Strings only: nothing is created under /tmp.
        recorded = "/tmp/agent-workbench-collab-test-absent/runs/x/worktrees"
        add_session(connection, 1, root=f"/private/tmp/agent-workbench-collab-test-absent/runs/x/worktrees/work-{ATTEMPT}")
        state = self.run_with([self.attempt(workspace=self.worktree(Path(recorded)))], workspace="worktree")
        attempt = collab.attribution(state, home=self.home, env=self.env)["attempts"][0]
        self.assertEqual([s["id"] for s in attempt["sessions"]], [session_id(1)])

    def test_command_tasks_and_unrecorded_worktrees_are_unavailable(self):
        connection = build_journal(self.journal)
        self.addCleanup(connection.close)
        self.register_claude()
        state = self.run_with([self.attempt(workspace={"mode": "worktree", "owned": True})], workspace="worktree")
        self.assertEqual(collab.attribution(state, home=self.home, env=self.env)["attempts"][0]["failure"], "workspace_unrecorded")
        command = self.store.create({"version": 1, "name": "c", "goal": "c", "tasks": [
            {"id": "check", "harness": "command", "argv": ["true"]}]}, self.project)
        self.store.update(command["id"], lambda current: current["tasks"][0]["attempts"].append(self.attempt()))
        view = collab.attribution(self.store.read(command["id"]), home=self.home, env=self.env)
        self.assertEqual((view["attempts"][0]["failure"], view["journals"]), ("command_task", []))


class JournalResolutionTests(CollabTestCase):
    def resolve(self, harness, **env):
        return collab.resolve_journal(harness, home=self.home, env={**self.env, **env})

    def test_each_harness_reads_its_own_registration_never_collab_default(self):
        default = self.home / ".local/share/agenttool/collab.sqlite"
        build_journal(default).close()
        self.assertEqual(self.resolve("claude")["failure"], "collab_not_registered")
        self.register_claude(db=False)
        self.assertEqual(self.resolve("claude")["failure"], "journal_unconfigured")
        self.assertEqual(self.resolve("claude", AGENTOOL_COLLAB_DB=str(default))["failure"], "journal_ambiguous")
        self.register_claude(db="relative/collab.sqlite")
        self.assertEqual(self.resolve("claude")["failure"], "journal_path_not_absolute")
        self.register_claude()
        self.assertEqual(self.resolve("astra")["real"], str(self.journal))
        self.assertIsNone(self.resolve("claude", AGENTOOL_COLLAB_DB=str(self.journal))["failure"])
        self.assertEqual(self.resolve("claude", AGENTOOL_COLLAB_DB=str(default))["failure"], "journal_ambiguous")
        self.register_claude(db=default, name="second-collab")
        self.assertEqual(self.resolve("claude")["failure"], "journal_ambiguous")

    def test_claude_config_dir_applies_to_claude_only_and_codex_home_is_honored(self):
        other = self.root / "other.sqlite"
        self.register_claude()
        self.register_claude(db=other, config=self.root / "claude-config" / ".claude.json")
        configured = {"CLAUDE_CONFIG_DIR": str(self.root / "claude-config")}
        self.assertEqual(self.resolve("claude", **configured)["real"], str(other))
        self.assertEqual(self.resolve("astra-ultra", **configured)["real"], str(self.journal))
        self.assertEqual(self.resolve("codex")["failure"], "collab_not_registered")
        self.register_codex(db=other, root=self.root / "codex-home")
        self.assertEqual(self.resolve("codex", CODEX_HOME=str(self.root / "codex-home"))["real"], str(other))

    def test_hermes_profiles_and_unparsed_blocks(self):
        self.register_hermes()
        self.assertEqual(self.resolve("hermes")["real"], str(self.journal))
        self.assertEqual(self.resolve("hermes-astra-ultra")["real"], str(self.journal))
        (self.home / ".hermes/active_profile").write_text("default\n")
        self.assertEqual(self.resolve("hermes")["failure"], "collab_not_registered")
        self.register_hermes(profile="default", active=None, db=self.root / "default.sqlite")
        self.assertEqual(self.resolve("hermes")["real"], str(self.root / "default.sqlite"))
        (self.home / ".hermes/active_profile").write_text("../escape\n")
        self.assertEqual(self.resolve("hermes")["failure"], "hermes_profile_invalid")
        self.register_hermes(body="mcp_servers:\n  agenttool: {command: bun}\n")
        self.assertEqual(self.resolve("hermes-astra-ultra")["failure"], "registration_unparsed")
        self.register_hermes(body="mcp_servers: &servers\n  agenttool:\n    command: bun\n")
        self.assertEqual(self.resolve("hermes-astra-ultra")["failure"], "registration_unparsed")
        self.register_hermes(body="mcp_servers:\n  agenttool:\n    <<: *base\n")
        self.assertEqual(self.resolve("hermes-astra-ultra")["failure"], "registration_unparsed")

    def test_scanner_reads_only_the_column_zero_mcp_servers_block(self):
        text = ("providers:\n  p:\n    key_cmd: SENTINEL_KEY_CMD\nmcp_servers:\n  agenttool:\n    command: bun\n"
                "    args:\n      - run\n      - 'it''s.js'\n    env:\n      AGENTOOL_COLLAB_DB: \"/j.sqlite\"  # journal\n"
                "    timeout: 30\n  tagged:\n    command: !env X\n  flow:\n    args: [a, b]\n"
                "toolsets:\n  mcp_servers:\n    command: not-this-block\n")
        status, servers = collab.hermes_mcp_servers(text)
        self.assertEqual(status, "read")
        self.assertEqual(servers["agenttool"], {"command": "bun", "args": ["run", "it's.js"],
                                                "env": {"AGENTOOL_COLLAB_DB": "/j.sqlite"}, "unparsed": False})
        self.assertTrue(servers["tagged"]["unparsed"] and servers["flow"]["unparsed"])
        self.assertEqual(set(servers), {"agenttool", "tagged", "flow"})
        self.assertNotIn("SENTINEL", json.dumps(servers))
        self.assertEqual(collab.hermes_mcp_servers("mcp_servers:\n\tagenttool:\n")[0], "unparsed")
        self.assertEqual(collab.hermes_mcp_servers("model: x\n"), ("no_mcp_servers", {}))

    def test_harnesses_on_different_journals_warn(self):
        other = self.root / "share" / "other.sqlite"
        for path in (self.journal, other):
            build_journal(path).close()
        self.register_claude()
        self.register_codex(db=other)
        state = self.store.create({"version": 1, "name": "two", "goal": "two", "tasks": [
            {"id": "a", "harness": "claude", "prompt": "a"}, {"id": "b", "harness": "codex", "prompt": "b"}]}, self.project)
        def add(current):
            for task in current["tasks"]:
                task["attempts"].append({"id": ATTEMPT if task["id"] == "a" else OTHER_ATTEMPT, "status": "succeeded",
                                         "started_at": "2026-10-28T10:00:00+00:00", "finished_at": "2026-10-28T10:01:00+00:00"})
        view = collab.attribution(self.store.update(state["id"], add), home=self.home, env=self.env)
        self.assertEqual(view["warnings"], ["journals_differ"])
        self.assertIn("different collab journals", collab.format_attribution(view))


class CollabCommandTests(CollabTestCase):
    def test_collab_command_is_read_only_and_prints_no_paths_or_actor_text(self):
        connection = build_journal(self.journal)
        self.addCleanup(connection.close)
        self.register_claude()
        add_session(connection, 1, root=str(self.project), actor=ACTOR + LABEL + ATTEMPT)
        state = self.run_with([self.attempt()])
        before = (self.store.directory(state["id"]) / "state.json").read_bytes()
        environment = {key: value for key, value in os.environ.items() if key not in HOST_VARIABLES}
        outputs = []
        with patch.dict(os.environ, {**environment, "HOME": str(self.home)}, clear=True):
            for extra in ([], ["--json"]):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["--state-dir", str(self.store.root), "collab", state["id"][:8], *extra]), 0)
                outputs.append(output.getvalue())
        self.assertEqual((self.store.directory(state["id"]) / "state.json").read_bytes(), before)
        self.assertIn(f"{session_id(1)}  ended", outputs[0])
        self.assertEqual(json.loads(outputs[1])["attempts"][0]["sessions"][0]["label_exact"], True)
        self.assert_private("".join(outputs))


if __name__ == "__main__":
    unittest.main()
