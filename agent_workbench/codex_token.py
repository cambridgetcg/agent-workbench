"""Access-only bridge for Hermes key_cmd; official Codex owns OAuth refresh.

The private binding pins the ChatGPT account and optionally CODEX_HOME. The
normal CLI's stdout is a credential protocol intended for Hermes's private pipe;
``--check`` is the read-only, metadata-only diagnostic interface.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time


TIMEOUT_SECONDS = 12.0
REFRESH_BEFORE_SECONDS = 3600
MIN_VALID_SECONDS = 600
MAX_CACHE_SECONDS = 300
MAX_AUTH_BYTES = 1_048_576
MAX_RPC_BYTES = 1_048_576


class TokenError(RuntimeError):
    """Fixed diagnostic codes only; never attach untrusted exception text."""


def _json_file(path: Path, limit: int, *, private: bool = False) -> dict:
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ValueError()
        if private and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077):
            raise ValueError()
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, RecursionError):
        raise TokenError("binding_invalid" if private else "auth_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def _text(value, limit=512):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 33 for c in value)


def _binding(path: Path, env: dict) -> tuple[str, Path]:
    data = _json_file(path, 16384, private=True)
    account = data.get("expected_account_id")
    codex_home = data.get("codex_home", env.get("CODEX_HOME", str(Path.home() / ".codex")))
    if not _text(account) or not isinstance(codex_home, str) or not codex_home or "\x00" in codex_home:
        raise TokenError("binding_invalid")
    home = Path(codex_home).expanduser()
    if not home.is_absolute():
        raise TokenError("binding_invalid")
    return account, home


def _read_token(home: Path, expected_account: str) -> tuple[str, float]:
    data = _json_file(home / "auth.json", MAX_AUTH_BYTES)
    try:
        if data.get("auth_mode") != "chatgpt":
            raise ValueError()
        tokens = data["tokens"]
        token = tokens["access_token"]
        if not _text(token, 262144) or len(token.split(".")) != 3:
            raise ValueError()
        encoded = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        expiry = claims["exp"]
        if type(expiry) not in (int, float) or not math.isfinite(expiry):
            raise ValueError()
        token_account = claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        file_account = tokens.get("account_id")
        account = token_account or file_account
        if not _text(account):
            raise ValueError()
        if account != expected_account or (file_account is not None and file_account != expected_account):
            raise TokenError("account_changed")
        return token, float(expiry)
    except TokenError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise TokenError("auth_unavailable") from None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TokenError("deadline_exceeded")
    return remaining


@contextmanager
def _refresh_lock(home: Path, deadline: float):
    fd = None
    try:
        fd = os.open(home / ".claude-astra-refresh.lock",
                     os.O_CREAT | os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise TokenError("refresh_unavailable")
        while True:
            _remaining(deadline)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.05, _remaining(deadline)))
        yield
    except OSError:
        raise TokenError("refresh_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def _stop(process):
    """Terminate the owned group even when its leader exited before a child."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            raise TokenError("refresh_cleanup_failed") from None
        if sig == signal.SIGTERM:
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
    try:
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        raise TokenError("refresh_cleanup_failed") from None


def _official_refresh(home: Path, env: dict, deadline: float):
    executable = shutil.which("codex", path=env.get("PATH", os.defpath))
    if executable is None:
        raise TokenError("codex_unavailable")
    child_env = dict(env, CODEX_HOME=str(home))
    process = None
    selector = selectors.DefaultSelector()
    pending = b""
    total = 0
    try:
        _remaining(deadline)
        process = subprocess.Popen([executable, "app-server"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env=child_env, start_new_session=True, close_fds=True)
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)

        def send(request):
            payload = (json.dumps(request) + "\n").encode()
            _remaining(deadline)
            # Three small protocol frames fit within an empty pipe's atomic
            # write capacity. Never block if a broken peer refuses input.
            if os.write(process.stdin.fileno(), payload) != len(payload):
                raise TokenError("refresh_failed")

        def rpc(request):
            nonlocal pending, total
            send(request)
            while True:
                while b"\n" in pending:
                    _remaining(deadline)
                    line, pending = pending.split(b"\n", 1)
                    response = json.loads(line)
                    if not isinstance(response, dict):
                        raise TokenError("refresh_failed")
                    if response.get("id") == request["id"]:
                        if "error" in response or not isinstance(response.get("result"), dict):
                            raise TokenError("refresh_failed")
                        return response["result"]
                if not selector.select(min(0.1, _remaining(deadline))):
                    if process.poll() is not None:
                        raise TokenError("refresh_failed")
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise TokenError("refresh_failed")
                total += len(chunk)
                if total > MAX_RPC_BYTES:
                    raise TokenError("refresh_failed")
                pending += chunk

        rpc({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "hermes_codex_token", "version": "1.0.0"}}})
        send({"method": "initialized"})
        result = rpc({"id": 2, "method": "account/read", "params": {"refreshToken": True}})
        account = result.get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            raise TokenError("chatgpt_required")
    except TokenError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise TokenError("refresh_failed") from None
    finally:
        selector.close()
        if process is not None:
            try:
                _stop(process)
            finally:
                process.stdin.close()
                process.stdout.close()


def borrow_token(binding: Path, *, check=False, env=None, timeout=TIMEOUT_SECONDS) -> dict:
    """Return a token protocol object, or redacted read-only check metadata.

    The caller must keep normal results private. A binding change must be an
    explicit setup action; this function never rewrites it or copies auth files.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= TIMEOUT_SECONDS:
        raise TokenError("invalid_deadline")
    deadline = time.monotonic() + timeout
    environment = dict(os.environ if env is None else env)
    expected_account, home = _binding(Path(binding), environment)
    token, expiry = _read_token(home, expected_account)
    if check:
        return {"status": "refresh_needed" if expiry < time.time() + REFRESH_BEFORE_SECONDS else "ready",
                "auth_method": "chatgpt", "account_bound": True,
                "expires_in": max(0, int(expiry - time.time())), "refresh_performed": False}
    if expiry < time.time() + REFRESH_BEFORE_SECONDS:
        with _refresh_lock(home, deadline):
            # Another launcher may have refreshed while this process waited.
            token, expiry = _read_token(home, expected_account)
            if expiry < time.time() + REFRESH_BEFORE_SECONDS:
                _official_refresh(home, environment, deadline)
                token, expiry = _read_token(home, expected_account)
    _remaining(deadline)
    if expiry < time.time() + MIN_VALID_SECONDS:
        raise TokenError("token_expiring")
    return {"access_token": token, "expires_in": min(MAX_CACHE_SECONDS, int(expiry - time.time()))}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise TokenError("invalid_arguments")


def main(argv=None) -> int:
    parser = _Parser(description="Private Hermes token bridge; use --check for safe diagnostics.")
    parser.add_argument("--binding", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="Print metadata only, without refreshing.")
    previous = {}
    try:
        args = parser.parse_args(argv)
        if not args.check and sys.stdout.isatty():
            raise TokenError("terminal_output_refused")
        def interrupted(_signum, _frame):
            raise TokenError("interrupted")
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, interrupted)
        result = borrow_token(args.binding, check=args.check)
        sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        return 0
    except TokenError as error:
        sys.stderr.write("Hermes Codex token bridge: " + str(error) + ".\n")
        return 1
    except (OSError, ValueError, TypeError, KeyboardInterrupt):
        sys.stderr.write("Hermes Codex token bridge: unavailable.\n")
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
