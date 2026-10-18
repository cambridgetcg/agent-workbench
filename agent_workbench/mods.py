"""Isolated, bounded native validation of optional Claude function-hook plugins."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import tempfile

from .handoff import contains_credential_text
from .processes import native_environment, run_owned


OUTPUT_LIMIT = 1_048_576
PROBE_TIMEOUT_SECONDS = 10
VALIDATE_TIMEOUT_SECONDS = 20
TEST_TIMEOUT_SECONDS = 60
_VERSION = re.compile(r"(?<![\w.])(\d{1,4}\.\d{1,4}\.\d{1,4})(?![\w.])")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _path_label(path):
    value = str(path)
    if len(value) > 4096 or any(ord(char) < 32 for char in value) or contains_credential_text(value):
        return "[path withheld]"
    return value


def _probe(argv, *, environment, cwd, timeout):
    """Private file-backed output; only callers' selected facts leave this module."""
    result = run_owned(argv, cwd=cwd, env=environment, timeout=timeout, output_limit=OUTPUT_LIMIT)
    return {"outcome": result["outcome"], "exit_code": result["exit_code"],
            "text": _ANSI.sub("", "\n".join(text for text in (result["stdout"], result["stderr"]) if text)),
            "signal": result["signal"]}


def _stage(status, diagnostic, probe=None, **fields):
    return {"status": status, "diagnostic": diagnostic,
            "exit_code": probe.get("exit_code") if probe else None, **fields}


def _problem(probe):
    if probe["outcome"] != "exited":
        return _stage("unverified", "native_" + probe["outcome"], probe)
    return None


def _function_hooks_gate(text):
    lower = text.lower()
    return ("hooks modules are not turned on" in lower
            or "function hooks are not enabled" in lower
            or "set claude_code_enable_function_hooks=1" in lower)


def check_mod(plugin, *, executable="claude", enable_function_hooks=False, run_tests=False):
    """Validate and optionally run native-host tests, without changing installation.

    Test output may contain arbitrary plugin text; public results contain only
    recognized capability facts, exit codes and numeric test counts. A passed
    validator alone does not establish function-hook runtime availability.
    """
    if type(enable_function_hooks) is not bool or type(run_tests) is not bool:
        raise ValueError("Function-hook and test options must be booleans.")
    root = Path(plugin).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Mod plugin must be an existing directory.")
    manifest = root / ".claude-plugin/plugin.json"
    if not manifest.is_file() and not (root / "plugin.json").is_file():
        raise ValueError("Mod plugin requires a plugin manifest.")
    resolved = shutil.which(str(executable))
    report = {"format": "agent-workbench-mod-check", "version": 1, "plugin": _path_label(root),
              "executable": None, "claude_version": None, "support": "unverified", "ok": False,
              "function_hooks": {"enabled_for_checks": enable_function_hooks, "scope": "child processes only"},
              "isolation": {"configuration": "temporary CLAUDE_CONFIG_DIR", "updates": "disabled",
                            "nonessential_traffic": "disabled", "global_settings_changed": False},
              "validation": _stage("unverified", "not_checked"),
              "tests": _stage("unverified", "not_requested", requested=run_tests),
              "limitations": ["Native host tests execute the selected plugin's test code against its mocked host.",
                              "A test pass does not establish live session behavior or terminal painting.",
                              "Early-access function-hook APIs may change between native versions."]}
    if resolved is None:
        report.update(support="unavailable", validation=_stage("unavailable", "executable_not_found"))
        return report
    selected = str(Path(resolved).resolve(strict=True))
    report["executable"] = _path_label(selected)
    with tempfile.TemporaryDirectory(prefix="agent-workbench-mod-check-") as temporary:
        config = Path(temporary) / "claude-config"
        config.mkdir(mode=0o700)
        environment = {**native_environment(), "CLAUDE_CONFIG_DIR": str(config), "DISABLE_AUTOUPDATER": "1",
                       "DISABLE_UPDATES": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        environment.pop("CLAUDE_CODE_ENABLE_FUNCTION_HOOKS", None)
        if enable_function_hooks:
            environment["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"] = "1"

        def probe(arguments, timeout=PROBE_TIMEOUT_SECONDS):
            return _probe([selected, *arguments], environment=environment, cwd=root, timeout=timeout)

        version = probe(["--version"])
        if version["outcome"] in {"interrupted", "cleanup_failed"}:
            report["validation"] = _problem(version)
            return report
        if version["outcome"] == "exited" and version["exit_code"] == 0:
            match = _VERSION.search(version["text"][:4096])
            report["claude_version"] = match.group(1) if match else None
        help_result = probe(["plugin", "validate", "--help"])
        problem = _problem(help_result)
        if problem:
            report["validation"] = problem
            return report
        if (help_result["exit_code"] != 0 or "Usage: claude plugin validate" not in help_result["text"]
                or not re.search(r"(?<!\S)--strict\b", help_result["text"])):
            report.update(support="unavailable", validation=_stage("unavailable", "strict_validation_not_advertised", help_result))
            return report

        validation = probe(["plugin", "validate", str(root), "--strict"], VALIDATE_TIMEOUT_SECONDS)
        problem = _problem(validation)
        if problem:
            report["validation"] = problem
            return report
        if _function_hooks_gate(validation["text"]):
            report["validation"] = _stage("unavailable", "function_hooks_disabled", validation)
        elif validation["exit_code"] == 0 and "Validation passed" in validation["text"]:
            report["validation"] = _stage("passed", "strict_validation_passed", validation)
        elif validation["exit_code"] != 0:
            report["validation"] = _stage("failed", "native_validation_failed", validation)
        else:
            report["validation"] = _stage("unverified", "validation_result_not_recognized", validation)

        test_help = probe(["plugin", "test", "--help"])
        problem = _problem(test_help)
        if problem:
            report["tests"] = {**problem, "requested": run_tests}
            return report
        if _function_hooks_gate(test_help["text"]):
            report.update(support="unavailable", tests=_stage("unavailable", "function_hooks_disabled", test_help, requested=run_tests))
            return report
        if test_help["exit_code"] != 0 or "Usage: claude plugin test" not in test_help["text"]:
            report.update(support="unavailable", tests=_stage("unavailable", "native_host_tests_not_advertised", test_help, requested=run_tests))
            return report
        if not run_tests:
            report["tests"] = _stage("unverified", "advertised_not_run", requested=False)
            report["ok"] = report["validation"]["status"] == "passed"
            return report
        if report["validation"]["status"] != "passed":
            report["tests"] = _stage("unverified", "validation_required_before_tests", requested=True)
            return report

        tests = probe(["plugin", "test", str(root)], TEST_TIMEOUT_SECONDS)
        problem = _problem(tests)
        if problem:
            report["tests"] = {**problem, "requested": True}
            return report
        if _function_hooks_gate(tests["text"]):
            report.update(support="unavailable", tests=_stage("unavailable", "function_hooks_disabled", tests, requested=True))
            return report
        passed = re.search(r"(?m)^\s*(\d{1,6}) pass\s*$", tests["text"])
        failed = re.search(r"(?m)^\s*(\d{1,6}) fail\s*$", tests["text"])
        total = re.search(r"(?m)^Ran (\d{1,6}) tests? across ", tests["text"])
        if not passed or not failed or not total or int(passed[1]) + int(failed[1]) != int(total[1]) or int(total[1]) == 0:
            report["tests"] = _stage("unverified", "test_result_not_recognized", tests, requested=True)
            return report
        counts = {"passed": int(passed[1]), "failed": int(failed[1]), "total": int(total[1])}
        did_pass = tests["exit_code"] == 0 and counts["failed"] == 0
        report["support"] = "supported"
        report["tests"] = _stage("passed" if did_pass else "failed",
                                 "native_host_tests_passed" if did_pass else "native_host_tests_failed",
                                 tests, requested=True, counts=counts)
        report["ok"] = did_pass
    return report
