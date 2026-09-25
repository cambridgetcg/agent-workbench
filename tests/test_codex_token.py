import base64
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import io
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

from agent_workbench import codex_token


ACCOUNT = "synthetic-private-account"
REFRESH = "synthetic-private-refresh-credential"


def jwt(expiry, account=ACCOUNT):
    claims = {"exp": expiry, "email": "private@example.invalid",
              "https://api.openai.com/auth": {"chatgpt_account_id": account}}
    return "e30." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".private-signature"


class CodexTokenTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.binding = self.root / "binding.json"
        self.binding.write_text(json.dumps({"expected_account_id": ACCOUNT, "codex_home": str(self.home)}))
        self.binding.chmod(0o600)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {"PATH": str(self.bin), "HOME": str(self.root)}
        self.token = self.auth(time.time() + 86400)

    def auth(self, expiry, account=ACCOUNT, **extra):
        token = jwt(expiry, account)
        data = {"auth_mode": "chatgpt", "tokens": {"access_token": token,
                "refresh_token": REFRESH, "account_id": account}, **extra}
        (self.home / "auth.json").write_text(json.dumps(data))
        return token

    def fake_codex(self, mode="success"):
        executable = self.bin / "codex"
        renewed = {"auth_mode": "chatgpt", "tokens": {"access_token": jwt(time.time() + 7200),
                   "refresh_token": "synthetic-rotated-by-official-owner", "account_id": ACCOUNT}}
        self.renewed = renewed["tokens"]["access_token"]
        if mode == "switch":
            renewed["tokens"]["account_id"] = "another-private-account"
            renewed["tokens"]["access_token"] = jwt(time.time() + 7200, "another-private-account")
        source = f'''#!{sys.executable}
import json, os, pathlib, subprocess, sys, time
root = pathlib.Path({str(self.root)!r})
home = pathlib.Path(os.environ["CODEX_HOME"])
mode = {mode!r}
(root / "argv.json").write_text(json.dumps(sys.argv[1:]))
if mode == "stall":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    (root / "pids.json").write_text(json.dumps({{"leader": os.getpid(), "child": child.pid}}))
    time.sleep(30)
if mode == "flood":
    sys.stdout.write("PRIVATE-RAW-ERROR" * 100000)
    sys.stdout.flush()
    time.sleep(30)
for line in sys.stdin:
    request = json.loads(line)
    with (root / "rpc.jsonl").open("a") as log:
        log.write(json.dumps(request) + "\\n")
    if request["method"] == "initialize":
        print(json.dumps({{"id": request["id"], "result": {{}}}}), flush=True)
    elif request["method"] == "account/read":
        if mode == "failure":
            print("PRIVATE-STDERR-TOKEN " + {REFRESH!r}, file=sys.stderr, flush=True)
            print(json.dumps({{"id": request["id"], "error": {{"message": {REFRESH!r}}}}}), flush=True)
        elif mode == "api_key":
            print(json.dumps({{"id": request["id"], "result": {{"account": {{"type": "apiKey"}}}}}}), flush=True)
        else:
            if mode != "unchanged":
                (home / "auth.json").write_text(json.dumps({renewed!r}))
            print(json.dumps({{"id": request["id"], "result": {{"account": {{"type": "chatgpt", "email": "private@example.invalid"}}}}}}), flush=True)
'''
        executable.write_text(source)
        executable.chmod(0o700)

    def test_fresh_access_only_without_refresh_or_auth_copy(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(codex_token, "_official_refresh", side_effect=AssertionError("No refresh")):
            result = codex_token.borrow_token(self.binding, env=self.env)
        self.assertEqual(result, {"access_token": self.token, "expires_in": 300})
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertNotIn(REFRESH, json.dumps(result))

    def test_check_is_redacted_read_only_even_if_expired(self):
        expired = self.auth(time.time() - 1)
        with patch.object(codex_token, "_official_refresh", side_effect=AssertionError("No refresh")):
            result = codex_token.borrow_token(self.binding, check=True, env=self.env)
        self.assertEqual(result["status"], "refresh_needed")
        self.assertEqual(result["expires_in"], 0)
        self.assertFalse(result["refresh_performed"])
        for secret in (expired, REFRESH, ACCOUNT, "private@example.invalid", str(self.home)):
            self.assertNotIn(secret, json.dumps(result))
        self.assertFalse((self.home / ".claude-astra-refresh.lock").exists())

    def test_expired_token_uses_official_rpc_and_pinned_codex_home(self):
        self.auth(time.time() - 5)
        self.fake_codex()
        result = codex_token.borrow_token(self.binding, env=dict(self.env, CODEX_HOME="/wrong-home"))
        self.assertEqual(result["access_token"], self.renewed)
        self.assertEqual(json.loads((self.root / "argv.json").read_text()), ["app-server"])
        requests = [json.loads(line) for line in (self.root / "rpc.jsonl").read_text().splitlines()]
        self.assertEqual([r["method"] for r in requests], ["initialize", "initialized", "account/read"])
        self.assertEqual(requests[-1]["params"], {"refreshToken": True})
        self.assertNotIn(REFRESH, json.dumps(requests))
        self.assertEqual((self.home / ".claude-astra-refresh.lock").stat().st_mode & 0o777, 0o600)

    def test_wrong_account_fails_before_refresh(self):
        self.auth(time.time() - 5, account="other-account")
        with patch.object(codex_token, "_official_refresh", side_effect=AssertionError("No refresh")):
            with self.assertRaisesRegex(codex_token.TokenError, "^account_changed$"):
                codex_token.borrow_token(self.binding, env=self.env)

    def test_inconsistent_file_and_jwt_account_fails(self):
        data = json.loads((self.home / "auth.json").read_text())
        data["tokens"]["account_id"] = "other-account"
        (self.home / "auth.json").write_text(json.dumps(data))
        with self.assertRaisesRegex(codex_token.TokenError, "^account_changed$"):
            codex_token.borrow_token(self.binding, env=self.env)

    def test_account_change_during_refresh_is_rejected(self):
        self.auth(time.time() - 1)
        self.fake_codex("switch")
        with self.assertRaisesRegex(codex_token.TokenError, "^account_changed$"):
            codex_token.borrow_token(self.binding, env=self.env)

    def test_unknown_auth_method_and_unknown_expiry_fail_closed(self):
        for expiry, auth_mode in [(None, "chatgpt"), (float("nan"), "chatgpt"),
                                  (True, "chatgpt"), (time.time() + 7200, "apikey"),
                                  (time.time() + 7200, "chatgptAuthTokens")]:
            with self.subTest(expiry=expiry, auth_mode=auth_mode):
                self.auth(expiry, auth_mode=auth_mode)
                with self.assertRaisesRegex(codex_token.TokenError, "^auth_unavailable$"):
                    codex_token.borrow_token(self.binding, env=self.env)

    def test_missing_auth_and_malformed_nested_shape_fail_closed(self):
        for data in ({}, {"auth_mode": "chatgpt", "tokens": []},
                     {"auth_mode": "chatgpt", "tokens": {"access_token": "private-not-jwt"}}, []):
            (self.home / "auth.json").write_text(json.dumps(data))
            with self.assertRaisesRegex(codex_token.TokenError, "^auth_unavailable$"):
                codex_token.borrow_token(self.binding, env=self.env)

    def test_fifo_and_oversized_auth_do_not_block(self):
        auth_path = self.home / "auth.json"
        auth_path.unlink()
        os.mkfifo(auth_path)
        start = time.monotonic()
        with self.assertRaisesRegex(codex_token.TokenError, "^auth_unavailable$"):
            codex_token.borrow_token(self.binding, env=self.env)
        self.assertLess(time.monotonic() - start, 0.5)
        auth_path.unlink()
        auth_path.write_bytes(b" " * (codex_token.MAX_AUTH_BYTES + 1))
        with self.assertRaisesRegex(codex_token.TokenError, "^auth_unavailable$"):
            codex_token.borrow_token(self.binding, env=self.env)

    def test_private_binding_required(self):
        self.binding.chmod(0o644)
        with self.assertRaisesRegex(codex_token.TokenError, "^binding_invalid$"):
            codex_token.borrow_token(self.binding, env=self.env)

    def test_refresh_lock_wait_is_bounded_without_second_refresher(self):
        self.auth(time.time() - 1)
        path = self.home / ".claude-astra-refresh.lock"
        with path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            start = time.monotonic()
            with patch.object(codex_token, "_official_refresh", side_effect=AssertionError("No refresh")):
                with self.assertRaisesRegex(codex_token.TokenError, "^deadline_exceeded$"):
                    codex_token.borrow_token(self.binding, env=self.env, timeout=0.15)
            self.assertLess(time.monotonic() - start, 0.7)

    def test_rereads_token_under_shared_lock(self):
        self.auth(time.time() - 1)
        original = codex_token._refresh_lock
        def refreshed_while_waiting(home, deadline):
            self.auth(time.time() + 7200)
            return original(home, deadline)
        with patch.object(codex_token, "_refresh_lock", side_effect=refreshed_while_waiting), \
                patch.object(codex_token, "_official_refresh", side_effect=AssertionError("No duplicate refresh")):
            result = codex_token.borrow_token(self.binding, env=self.env)
        self.assertEqual(result["expires_in"], 300)

    def test_refresh_errors_never_echo_secrets(self):
        self.auth(time.time() - 1)
        self.fake_codex("failure")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
            result = codex_token.main(["--binding", str(self.binding)])
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Hermes Codex token bridge: refresh_failed.\n")

    def test_refresh_must_return_chatgpt_and_fresh_file_token(self):
        for mode, error in (("api_key", "chatgpt_required"), ("unchanged", "token_expiring")):
            with self.subTest(mode=mode):
                self.auth(time.time() - 1)
                self.fake_codex(mode)
                with self.assertRaisesRegex(codex_token.TokenError, "^" + error + "$"):
                    codex_token.borrow_token(self.binding, env=self.env)

    def test_deadline_kills_owned_children(self):
        self.auth(time.time() - 1)
        self.fake_codex("stall")
        start = time.monotonic()
        with self.assertRaisesRegex(codex_token.TokenError, "^deadline_exceeded$"):
            codex_token.borrow_token(self.binding, env=self.env, timeout=1.5)
        self.assertLess(time.monotonic() - start, 2.5)
        pids = json.loads((self.root / "pids.json").read_text())
        for pid in pids.values():
            state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), state)

    def test_unbounded_rpc_output_fails_without_echo(self):
        self.auth(time.time() - 1)
        self.fake_codex("flood")
        with self.assertRaisesRegex(codex_token.TokenError, "^refresh_failed$"):
            codex_token.borrow_token(self.binding, env=self.env, timeout=2)

    def test_cli_sigterm_cleans_refresh_group_without_token_output(self):
        self.auth(time.time() - 1)
        self.fake_codex("stall")
        script = Path(__file__).resolve().parents[1] / "scripts/hermes-codex-token"
        process = subprocess.Popen([sys.executable, str(script), "--binding", str(self.binding)],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        pids = None
        try:
            deadline = time.monotonic() + 5
            ready = self.root / "pids.json"
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            pids = json.loads(ready.read_text())
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "Hermes Codex token bridge: interrupted.\n")
            for pid in pids.values():
                state = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)
            if pids:
                try:
                    os.killpg(pids["leader"], signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_unsafe_refresh_lock_target_is_rejected(self):
        self.auth(time.time() - 1)
        lock = self.home / ".claude-astra-refresh.lock"
        lock.symlink_to(self.binding)
        before = self.binding.read_bytes()
        with self.assertRaisesRegex(codex_token.TokenError, "^refresh_unavailable$"):
            codex_token.borrow_token(self.binding, env=self.env)
        self.assertEqual(self.binding.read_bytes(), before)

    def test_terminal_secret_output_refused_before_reading_auth(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        stdout.isatty = lambda: True
        with redirect_stdout(stdout), redirect_stderr(stderr), \
                patch.object(codex_token, "borrow_token", side_effect=AssertionError("No token read")):
            result = codex_token.main(["--binding", str(self.binding)])
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("terminal_output_refused", stderr.getvalue())

    def test_cli_check_and_protocol_output_are_distinct(self):
        for check in (False, True):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, self.env, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
                result = codex_token.main(["--binding", str(self.binding)] + (["--check"] if check else []))
            self.assertEqual(result, 0, stderr.getvalue())
            output = json.loads(stdout.getvalue())
            self.assertEqual("access_token" in output, not check)
            self.assertNotIn(REFRESH, stdout.getvalue() + stderr.getvalue())

    def test_invalid_arguments_do_not_echo_input(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = codex_token.main(["--secret=" + REFRESH])
        self.assertEqual(result, 1)
        self.assertNotIn(REFRESH, stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
