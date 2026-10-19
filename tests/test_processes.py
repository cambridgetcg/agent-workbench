from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_workbench.processes import run_owned


class OwnedProcessTests(unittest.TestCase):
    def test_active_checkout_lease_owns_probe_before_execution_and_releases_after(self):
        calls = []
        class Owner:
            def spawn(self, argv, **kwargs):
                calls.append(("spawn", kwargs["start_new_session"], kwargs["close_fds"]))
                return subprocess.Popen(argv, **kwargs)
            def release_process(self, pid):
                calls.append(("release", pid))
        with patch("agent_workbench.workspaces.current_lease", return_value=Owner()):
            result = run_owned([sys.executable, "-c", "print('owned')"])
        self.assertEqual(result["outcome"], "exited")
        self.assertEqual(calls[0], ("spawn", True, True))
        self.assertEqual(calls[1][0], "release")

    def test_main_thread_handlers_are_restored_and_worker_calls_remain_supported(self):
        before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        result = run_owned([sys.executable, "-c", "print('metadata')"])
        self.assertEqual(result["stdout"].strip(), "metadata")
        self.assertEqual(before, {sig: signal.getsignal(sig) for sig in before})
        with ThreadPoolExecutor(max_workers=1) as workers:
            result = workers.submit(run_owned, [sys.executable, "-c", "print('worker')"]).result(timeout=5)
        self.assertEqual(result["stdout"].strip(), "worker")

    def test_parent_sigterm_cleans_mod_probe_children_and_restores_handlers(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready.json"
            child = "import time; time.sleep(30)"
            target = "import subprocess,sys,time,json,os; from pathlib import Path; "
            target += "p=subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); "
            target += "Path(" + repr(str(ready)) + ").write_text(json.dumps({'child':p.pid,'group':os.getpid()})); time.sleep(30)"
            controller_code = "from agent_workbench.mods import _probe; import json,sys,signal,os; old=signal.getsignal(signal.SIGTERM); "
            controller_code += "r=_probe([sys.executable,'-c'," + repr(target) + "],environment=os.environ.copy(),cwd=" + repr(directory) + ",timeout=30); "
            controller_code += "r['handler_restored']=signal.getsignal(signal.SIGTERM)==old; print(json.dumps(r))"
            controller = subprocess.Popen([sys.executable, "-c", controller_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            owned = None
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and controller.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                owned = json.loads(ready.read_text())
                controller.send_signal(signal.SIGTERM)
                stdout, stderr = controller.communicate(timeout=5)
                self.assertEqual(controller.returncode, 0, stderr)
                result = json.loads(stdout)
                self.assertEqual(result["outcome"], "interrupted")
                self.assertTrue(result["handler_restored"])
                state = subprocess.run(["ps", "-p", str(owned["child"]), "-o", "stat="], capture_output=True, text=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
            finally:
                if controller.poll() is None:
                    controller.kill()
                    controller.wait()
                if owned:
                    try:
                        os.killpg(owned["group"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_successful_leader_exit_still_ends_owned_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "child.json"
            code = "import subprocess,sys,json; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); Path(" + repr(str(path)) + ").write_text(json.dumps(p.pid)); print('done')"
            result = run_owned([sys.executable, "-c", code], timeout=3)
            self.assertEqual(result["outcome"], "exited")
            pid = json.loads(path.read_text())
            state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), state)


class NativeEnvironmentTests(unittest.TestCase):
    def test_host_session_variables_are_removed_by_name_and_messaging_prefix(self):
        from agent_workbench.processes import host_session_variables, native_environment
        host = {"CLAUDECODE": "1", "CLAUDE_PID": "1", "CLAUDE_CODE_MESSAGING_TOKEN": "private",
                "CLAUDE_CODE_MESSAGING_FUTURE": "private", "CLAUDE_CODE_USE_BEDROCK": "1",
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000", "OPENAI_API_KEY": "kept", "PATH": "/bin"}
        self.assertEqual(native_environment(host), {"CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000",
                                                    "OPENAI_API_KEY": "kept", "PATH": "/bin"})
        self.assertEqual(host_session_variables(host), ["CLAUDECODE", "CLAUDE_CODE_MESSAGING_FUTURE",
                                                        "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID"])

    def test_codex_host_session_markers_are_removed_but_sandbox_and_credentials_stay(self):
        from agent_workbench.processes import host_session_variables, native_environment
        host = {"CODEX_CI": "1", "CODEX_SESSION_ID": "s", "CODEX_THREAD_ID": "t", "CODEX_VERSION": "0.156.1",
                "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "codex_vscode", "CODEX_ESCALATE_SOCKET": "/tmp/parent.sock",
                "CODEX_HOME": "/codex", "CODEX_API_KEY": "kept", "CODEX_SANDBOX": "seatbelt",
                "CODEX_SANDBOX_NETWORK_DISABLED": "1", "CODEX_NETWORK_PROXY_ACTIVE": "1", "PATH": "/bin"}
        self.assertEqual(native_environment(host), {"CODEX_HOME": "/codex", "CODEX_API_KEY": "kept",
                                                    "CODEX_SANDBOX": "seatbelt", "CODEX_SANDBOX_NETWORK_DISABLED": "1",
                                                    "CODEX_NETWORK_PROXY_ACTIVE": "1", "PATH": "/bin"})
        self.assertEqual(host_session_variables(host), ["CODEX_CI", "CODEX_ESCALATE_SOCKET", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
                                                        "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_VERSION"])


if __name__ == "__main__":
    unittest.main()
