"""Guard-preservation checks over Workbench's own source, by syntax tree.

These tests keep properties that review relies on from eroding silently:
Workbench allocates no pseudo-terminal, drives no terminal-automation tool,
spawns no agenttool host, lobby, doorbell, courier, ferry or mission program,
and keeps its native, command and checkout spawns in their own sessions. They
inspect Workbench's own code only (``agent_workbench/*.py``). A child's own
pseudo-terminal tools, such as Codex's unified_exec_tty or Hermes's terminal
pty mode, and whatever a user's command task runs as written, are outside
Workbench's reach and outside these checks.

The scan is a syntax-tree walk, not a text grep, so ``scripts/`` paths,
prose and docstrings cannot produce false positives.
"""
import ast
import os
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parent.parent / "agent_workbench"
TERMINAL_MODULES = {"pty", "tty", "termios"}
TERMINAL_CALLS = {("os", "openpty"), ("os", "forkpty"), ("pty", "spawn"), ("pty", "fork"), ("pty", "openpty")}
TERMINAL_DRIVERS = {"script", "expect", "unbuffer", "osascript", "tmux", "screen"}
AGENTTOOL_PROGRAMS = {"bun", "agenttool-collab-host", "agenttool-lobby", "agenttool-lobby-doorbell",
                      "lobby-netcheck", "collab-zerone", "collab-courier", "ferry", "agenttool-mission"}
# CheckoutLease.spawn forwards its caller's keyword arguments, and refuses a
# claim-owned launch without start_new_session=True; every caller passes it.
PASS_THROUGH_SPAWNS = {("workspaces.py", "spawn")}


def _modules():
    for path in sorted(SOURCE.glob("*.py")):
        yield path, ast.parse(path.read_text(), str(path))


def _program(value):
    return os.path.basename(value) if isinstance(value, str) else None


def _callee(node):
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _enclosing_functions(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    def function_of(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return None
    return function_of


class GuardPreservationTests(unittest.TestCase):
    def test_source_tree_is_scanned(self):
        names = {path.name for path, _ in _modules()}
        self.assertTrue({"runner.py", "processes.py", "workspaces.py", "doctor.py", "cli.py"} <= names)

    def test_no_pseudo_terminal_modules_or_calls(self):
        for path, tree in _modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], TERMINAL_MODULES, f"{path.name}:{node.lineno}")
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or "").split(".")[0], TERMINAL_MODULES, f"{path.name}:{node.lineno}")
                    if node.module == "os":
                        self.assertFalse({alias.name for alias in node.names} & {"openpty", "forkpty"}, f"{path.name}:{node.lineno}")
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    self.assertNotIn((node.value.id, node.attr), TERMINAL_CALLS, f"{path.name}:{node.lineno}")

    def test_no_argv_literal_starts_a_terminal_driver_or_agenttool_program(self):
        forbidden = TERMINAL_DRIVERS | AGENTTOOL_PROGRAMS
        for path, tree in _modules():
            for node in ast.walk(tree):
                if isinstance(node, (ast.List, ast.Tuple)) and node.elts and isinstance(node.elts[0], ast.Constant):
                    self.assertNotIn(_program(node.elts[0].value), forbidden, f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Call) and _callee(node) == "which" and node.args and isinstance(node.args[0], ast.Constant):
                    self.assertNotIn(_program(node.args[0].value), forbidden, f"{path.name}:{node.lineno}")
        from agent_workbench.runner import _EXECUTABLES
        self.assertFalse({_program(name) for name in _EXECUTABLES.values()} & forbidden)

    def test_native_command_and_checkout_spawns_start_their_own_session(self):
        checked = 0
        for path, tree in _modules():
            function_of = _enclosing_functions(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _callee(node) not in {"Popen", "spawn"}:
                    continue
                checked += 1
                keywords = {keyword.arg: keyword.value for keyword in node.keywords}
                if (path.name, function_of(node)) in PASS_THROUGH_SPAWNS and None in keywords:
                    continue
                value = keywords.get("start_new_session")
                self.assertTrue(isinstance(value, ast.Constant) and value.value is True,
                                f"{path.name}:{node.lineno} spawns without start_new_session=True")
        self.assertGreaterEqual(checked, 5)

    def test_doctor_probes_pass_devnull_stdin(self):
        tree = ast.parse((SOURCE / "doctor.py").read_text())
        runs = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _callee(node) == "run"
                and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"]
        self.assertGreaterEqual(len(runs), 2)
        for node in runs:
            stdin = next((keyword.value for keyword in node.keywords if keyword.arg == "stdin"), None)
            self.assertTrue(isinstance(stdin, ast.Attribute) and stdin.attr == "DEVNULL", f"doctor.py:{node.lineno}")


if __name__ == "__main__":
    unittest.main()
