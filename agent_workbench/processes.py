"""Bounded subprocess groups with private output and scoped signal handling.

Main-thread calls capture SIGINT/SIGTERM until group cleanup completes and
restore the previous handlers. Worker-thread calls cannot install handlers:
their owner must handle process signals and keep the process alive through
worker cleanup. Workers may additionally supply a cancellation callback.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import math
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time


@contextmanager
def scoped_signals():
    interrupted = {"signal": None}
    previous = {}
    if threading.current_thread() is threading.main_thread():
        def receive(signum, _frame):
            if interrupted["signal"] is None:
                interrupted["signal"] = signum
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, receive)
    try:
        yield interrupted
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _terminate(process, grace_seconds):
    def send(signum):
        try:
            os.killpg(process.pid, signum)
            return True
        except ProcessLookupError:
            return True
        except OSError:
            # macOS can report EPERM for a just-exited, unreaped group leader.
            # Reap it and establish absence before treating that race as clean.
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                pass
            return False
    process.poll()
    success = send(signal.SIGTERM)
    if process.poll() is None:
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
    # The leader may have exited while ordinary descendants still own its group.
    success = send(signal.SIGKILL) and success
    try:
        process.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        success = False
    return success


def _output(stack, path):
    if path is None:
        return stack.enter_context(tempfile.TemporaryFile(mode="w+b"))
    descriptor = os.open(Path(path), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    return stack.enter_context(os.fdopen(descriptor, "w+b"))


def run_owned(argv, *, cwd=None, env=None, timeout=10, output_limit=1_048_576,
              cancelled=None, stdout_path=None, stderr_path=None, capture_output=True,
              termination_grace=1):
    """Own a new process group, including descendants after its leader exits.

    Output is file-backed and monitored against a combined byte budget. Reads
    are bounded even if a child writes beyond that budget between polls. Output
    from failed, cancelled, or oversized probes is withheld. Raw files persist
    only when explicit private paths were supplied by the caller.
    """
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
            or type(output_limit) is not int or output_limit <= 0
            or type(termination_grace) not in (int, float) or not math.isfinite(termination_grace)
            or not 0 <= termination_grace <= 15):
        raise ValueError("Process timeout and output limit must be positive.")
    result = {"outcome": "launch_failed", "exit_code": None, "stdout": "", "stderr": "", "signal": None}
    with scoped_signals() as interrupted, ExitStack() as stack:
        stdout = _output(stack, stdout_path)
        stderr = _output(stack, stderr_path)
        process = None
        # Scheduler workers publish probe ownership before execution too, so a
        # controller crash cannot free a checkout while a helper survives.
        from .workspaces import current_lease
        owner = current_lease()
        spawn = owner.spawn if owner is not None else subprocess.Popen
        try:
            if interrupted["signal"] is not None or cancelled is not None and cancelled():
                result["outcome"] = "interrupted"
            else:
                process = spawn(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=stdout, stderr=stderr, start_new_session=True, close_fds=True)
                deadline = time.monotonic() + timeout
                while True:
                    if interrupted["signal"] is not None or cancelled is not None and cancelled():
                        result["outcome"] = "interrupted"
                        break
                    if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > output_limit:
                        result["outcome"] = "output_limit"
                        break
                    if process.poll() is not None:
                        result["outcome"] = "exited"
                        break
                    if time.monotonic() >= deadline:
                        result["outcome"] = "timed_out"
                        break
                    time.sleep(0.01)
        except (OSError, ValueError):
            result["outcome"] = "launch_failed"
        except KeyboardInterrupt:
            result["outcome"] = "interrupted"
        finally:
            if process is not None:
                if not _terminate(process, termination_grace):
                    result["outcome"] = "cleanup_failed"
                result["exit_code"] = process.returncode
                if owner is not None:
                    try:
                        owner.release_process(process.pid)
                    except (OSError, ValueError, RuntimeError):
                        result["outcome"] = "cleanup_failed"
            result["signal"] = interrupted["signal"]
            if interrupted["signal"] is not None and result["outcome"] == "exited":
                result["outcome"] = "interrupted"
        if result["outcome"] == "exited" and capture_output:
            stdout.seek(0)
            stdout_bytes = stdout.read(output_limit)
            stderr.seek(0)
            stderr_bytes = stderr.read(max(0, output_limit - len(stdout_bytes)))
            result["stdout"] = stdout_bytes.decode("utf-8", errors="replace")
            result["stderr"] = stderr_bytes.decode("utf-8", errors="replace")
        if interrupted["signal"] is not None:
            result["signal"] = interrupted["signal"]
            if result["outcome"] == "exited":
                result.update(outcome="interrupted", stdout="", stderr="")
    return result
