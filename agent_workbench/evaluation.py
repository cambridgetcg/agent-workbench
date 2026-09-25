"""Local plugin comparisons with explicit limits and content-free result summaries.

The native evaluator owns isolation, grading, authentication and permissions.
Workbench never publishes its reports, treats cost as an estimate, and keeps
native reports (which can contain prompts and traces) in private state.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import time
import uuid

from .handoff import contains_credential_text
from .store import now, write_private_json


MAX_FILE_BYTES = 2_000_000
MAX_REPORT_BYTES = 20_000_000
MAX_FILES = 2000
COMPONENTS = ("skills", "commands", "agents", "output-styles", "workflows", "hooks")
_VERSION = re.compile(r"(?<![\w.])(\d{1,4}\.\d{1,4}\.\d{1,4})(?![\w.])")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}\Z")


def _number(value, low=0, high=1e12):
    return value if type(value) in (int, float) and math.isfinite(value) and low <= value <= high else None


def _bounded_integer(value, name, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}.")
    return value


def _label(value):
    return value if isinstance(value, str) and _LABEL.fullmatch(value) and not contains_credential_text(value) else "[label withheld]"


def _read_regular(path, limit):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Evaluation input must be a bounded regular file.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Evaluation input is too large.")
        return data
    finally:
        os.close(descriptor)


def _files(directory):
    """Walk authoring inputs, never symlinked directories or files."""
    if directory.is_symlink():
        raise ValueError("Plugin component directories must not be symbolic links.")
    count = 0
    for base, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith(".") and name != "results")
        if any((Path(base) / name).is_symlink() for name in dirs):
            raise ValueError("Plugin component directories must not be symbolic links.")
        for name in sorted(files):
            path = Path(base) / name
            if path.is_symlink():
                raise ValueError("Plugin component files must not be symbolic links.")
            count += 1
            if count > MAX_FILES:
                raise ValueError("Plugin has too many component files.")
            yield path


def plugin_inventory(plugin):
    """Count authoring text, not actual model context or tokenizer output."""
    root = Path(plugin).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Plugin must be a directory.")
    manifest = root / ".claude-plugin/plugin.json"
    if not manifest.exists():
        manifest = root / "plugin.json"
    if manifest.parent.is_symlink():
        raise ValueError("Plugin manifest directory must not be a symbolic link.")
    try:
        manifest_bytes = _read_regular(manifest, MAX_FILE_BYTES)
        metadata = json.loads(manifest_bytes)
    except (OSError, ValueError, UnicodeError):
        raise ValueError("Plugin requires a readable JSON manifest.") from None
    if not isinstance(metadata, dict):
        raise ValueError("Plugin manifest must be an object.")
    experimental = metadata.get("experimental")
    eval_directory = experimental.get("evals", "evals") if isinstance(experimental, dict) else "evals"
    rows, digest, total = [], hashlib.sha256(), 0
    for component in COMPONENTS:
        row = {"component": component, "files": 0, "bytes": 0, "characters": 0}
        directory = root / component
        for path in _files(directory):
            if path.suffix.lower() not in {".md", ".json", ".js", ".ts", ".tsx", ".py", ".sh"}:
                continue
            data = _read_regular(path, MAX_FILE_BYTES)
            total += len(data)
            if total > MAX_REPORT_BYTES:
                raise ValueError("Plugin authoring inputs exceed the inventory limit.")
            relative = path.relative_to(root).as_posix().encode()
            digest.update(len(relative).to_bytes(4, "big") + relative + len(data).to_bytes(8, "big") + data)
            row["files"] += 1
            row["bytes"] += len(data)
            row["characters"] += len(data.decode("utf-8", errors="replace"))
        row["approximate_tokens"] = math.ceil(row["characters"] / 4)
        rows.append(row)
    return {"name": _label(metadata.get("name")), "version": _label(metadata.get("version")),
            "components": rows, "authoring_bytes": total,
            "approximate_tokens": sum(row["approximate_tokens"] for row in rows),
            "estimate_method": "ceil(Unicode characters / 4) per component; not tokenizer output or loaded context",
            "inventory_sha256": digest.hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "native_eval_directory": "evals" if eval_directory == "evals" else "custom_not_supported",
            "limitations": ["Authoring inventory omits external component paths, MCP schemas and dynamic content.",
                            "Skills load progressively; inventory size is not per-turn context cost."]}


def evaluation_capabilities(executable="claude"):
    selected = shutil.which(str(executable))
    if selected is None:
        raise ValueError("Claude evaluation executable was not found.")
    selected = str(Path(selected).resolve(strict=True))
    environment = {**os.environ, "DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}
    try:
        version_result = subprocess.run([selected, "--version"], capture_output=True, text=True, timeout=5,
                                        stdin=subprocess.DEVNULL, env=environment, check=False)
        help_result = subprocess.run([selected, "plugin", "eval", "--help"], capture_output=True, text=True,
                                     timeout=5, stdin=subprocess.DEVNULL, env=environment, check=False)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise ValueError("Cannot verify Claude evaluation capabilities.") from None
    version = _VERSION.search(version_result.stdout[:4096])
    help_text = help_result.stdout[:100_000]
    flags = set(re.findall(r"(?<!\S)--[a-z][a-z-]+\b", help_text))
    required = {"--ablation", "--runs", "--case", "--max-cost-usd", "--output-dir", "--json"}
    if help_result.returncode != 0 or "Usage: claude plugin eval" not in help_text or not required <= flags:
        raise ValueError("Selected executable does not expose the required native eval contract.")
    if "--no-publish" in flags:
        publication = "explicit_no_publish"
    elif ("--publish-report" in flags and version is not None
          and version.group(1) == "2.1.221"
          and "Publish the HTML report" in help_text):
        # This is an observed contract, not an assumption about all future releases.
        publication = "legacy_opt_in_not_requested"
    else:
        raise ValueError("Cannot establish local-only report behavior; evaluation was not started.")
    info = os.stat(selected)
    return {"executable": selected, "version": version.group(1) if version else None,
            "publication": publication, "flags": sorted(flags),
            "identity": {"size": info.st_size, "mtime_ns": info.st_mtime_ns,
                         "device": info.st_dev, "inode": info.st_ino},
            "evidence": "CLI help observed; account eligibility and runtime support unverified"}


def evaluation_plan(plugin, *, executable="claude", runs=3, case=None, max_cases=20,
                    timeout_seconds=900, max_cost_usd=5, model=None, judge_model=None,
                    allow_tools=(), trust_plugin=False):
    """Plan a bounded two-arm comparison; performs only metadata/help reads."""
    runs = _bounded_integer(runs, "Runs per arm", 10)
    max_cases = _bounded_integer(max_cases, "Case limit", 50)
    timeout_seconds = _bounded_integer(timeout_seconds, "Timeout seconds", 3600)
    if _number(max_cost_usd, 0.01, 100) is None:
        raise ValueError("Cost ceiling must be between 0.01 and 100 USD.")
    if case is not None and (not isinstance(case, str) or not re.fullmatch(r"[A-Za-z0-9_.*?/-]{1,160}", case)):
        raise ValueError("Invalid evaluation case filter.")
    for value in (model, judge_model):
        if value is not None and (_label(value) != value or value.startswith("-")):
            raise ValueError("Invalid evaluation model identifier.")
    if type(trust_plugin) is not bool or not isinstance(allow_tools, (list, tuple)) or len(allow_tools) > 50:
        raise ValueError("Invalid evaluation tool or trust options.")
    for tool in allow_tools:
        if (not isinstance(tool, str) or not 1 <= len(tool) <= 256 or tool.startswith("-")
                or any(ord(char) < 32 for char in tool) or contains_credential_text(tool)):
            raise ValueError("Invalid evaluation tool grant.")
    root = Path(plugin).expanduser().resolve(strict=True)
    inventory = plugin_inventory(root)
    if inventory["native_eval_directory"] != "evals":
        raise ValueError("Custom native eval directories are not supported by bounded evaluation plans.")
    suite_files = list(_files(root / "evals"))
    case_dirs = {path.parent for path in suite_files if path.name in {"case.yaml", "prompt.md"}}
    # Bound the entire suite because native case names may override directory names.
    if not 1 <= len(case_dirs) <= max_cases:
        raise ValueError(f"Evaluation suite must contain between 1 and {max_cases} case directories.")
    suite_digest, suite_bytes = hashlib.sha256(), 0
    for path in suite_files:
        data = _read_regular(path, MAX_FILE_BYTES)
        suite_bytes += len(data)
        if suite_bytes > MAX_REPORT_BYTES:
            raise ValueError("Evaluation suite exceeds the input size limit.")
        relative = path.relative_to(root).as_posix().encode()
        suite_digest.update(len(relative).to_bytes(4, "big") + relative + len(data).to_bytes(8, "big") + data)
    capabilities = evaluation_capabilities(executable)
    if trust_plugin and "--trust-plugin" not in capabilities["flags"]:
        raise ValueError("Selected evaluator does not support explicit directory trust.")
    if model and "--model" not in capabilities["flags"] or judge_model and "--judge-model" not in capabilities["flags"]:
        raise ValueError("Selected evaluator does not support the requested model override.")
    if allow_tools and "--allow-tools" not in capabilities["flags"]:
        raise ValueError("Selected evaluator does not support explicit tool grants.")
    return {"format": "agent-workbench-evaluation-plan", "version": 1, "plugin": str(root),
            "capabilities": capabilities, "inventory": inventory, "ablation": "with-without",
            "suite_case_count": len(case_dirs), "maximum_agent_runs": 2 * runs * len(case_dirs),
            "suite_sha256": suite_digest.hexdigest(),
            "options": {"runs": runs, "case": case, "max_cases": max_cases, "timeout_seconds": timeout_seconds,
                        "max_cost_usd": max_cost_usd, "model": model, "judge_model": judge_model,
                        "allow_tools": list(allow_tools), "trust_plugin": trust_plugin},
            "limits": {"timeout_scope": "entire evaluator process group",
                       "case_limits": "native case max_turns and timeout_seconds; bounded again by overall timeout",
                       "cost_scope": "native list-price ceiling; may overrun by one agent run",
                       "scaffolds": "not enabled", "publication": "local only"}}


def evaluation_report(source):
    """Allowlist numeric outcomes; never return prompts, traces or grader evidence."""
    if isinstance(source, (str, Path)):
        try:
            source = json.loads(_read_regular(Path(source), MAX_REPORT_BYTES))
        except (OSError, ValueError, UnicodeError):
            raise ValueError("Native evaluation report is unreadable or malformed.") from None
    if not isinstance(source, dict) or type(source.get("schemaVersion")) is not int or source["schemaVersion"] != 1:
        raise ValueError("Unsupported native evaluation report schema.")
    cases = source.get("cases")
    if not isinstance(cases, list) or len(cases) > 50:
        raise ValueError("Invalid native evaluation case results.")
    partial_reason = source.get("partialReason")
    result = {"format": "agent-workbench-evaluation-summary", "version": 1,
              "partial": source.get("partial") is True,
              "partial_reason": partial_reason if partial_reason in {"cost_ceiling", "interrupted", "auth_failed"} else None,
              "claude_version": source.get("claudeVersion") if isinstance(source.get("claudeVersion"), str)
              and _VERSION.fullmatch(source["claudeVersion"]) else None,
              "estimated_cost_usd": _number(source.get("costUsd")),
              "wall_seconds": _number(source.get("durationSeconds")), "cases": []}
    aggregates = source.get("aggregates") if isinstance(source.get("aggregates"), dict) else {}
    result["aggregates"] = {key: _number(aggregates.get(key), -1 if key == "meanDelta" else 0,
                                       50 if key in {"casesPassed", "casesTotal"} else 1)
                            for key in ("overallScore", "casesPassed", "casesTotal", "meanDelta")}
    eligible = not result["partial"]
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError("Invalid native evaluation case result.")
        totals = case.get("aggregates") if isinstance(case.get("aggregates"), dict) else {}
        row = {"index": index, "name": _label(case.get("name")), "score": _number(totals.get("score"), 0, 1),
               "delta": _number(totals.get("delta"), -1, 1), "arms": {}}
        arms = case.get("arms") if isinstance(case.get("arms"), dict) else {}
        for arm in ("with", "without"):
            runs = arms.get(arm, [])
            if not isinstance(runs, list) or len(runs) > 50:
                raise ValueError("Invalid native evaluation arm results.")
            metrics = []
            for run in runs:
                if not isinstance(run, dict):
                    raise ValueError("Invalid native evaluation run result.")
                errored = run.get("error") is not None or bool(run.get("aborted"))
                skipped = run.get("skippedPaidGraders") is True
                eligible = eligible and not errored and not skipped
                metrics.append({"score": _number(run.get("score"), 0, 1), "error": errored,
                                "skipped_paid_graders": skipped,
                                "duration_seconds": _number(run.get("durationSeconds")),
                                "estimated_cost_usd": _number(run.get("costUsd"))})
            row["arms"][arm] = metrics
        # An aggregate delta cannot prove that both arms actually ran.
        eligible = (eligible and bool(row["arms"]["with"])
                    and len(row["arms"]["with"]) == len(row["arms"]["without"])
                    and row["score"] is not None and row["delta"] is not None
                    and all(run["score"] is not None for arm in row["arms"].values() for run in arm))
        result["cases"].append(row)
    result["eligible_for_comparison"] = eligible and bool(cases)
    result["metric_notes"] = ["Costs are native list-price estimates, not account charges.",
                              "Wall time includes evaluator overhead and judge calls.",
                              "Missing native measurements remain null; no token counts are inferred.",
                              "Scores are grader outcomes, not independent verification or proof of causation."]
    return result


def _stop_group(process):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        if sig == signal.SIGTERM:
            time.sleep(0.2)
    process.wait(timeout=5)


def run_evaluation(plan, *, state_root=None):
    """Explicitly execute a prepared plan; never retry an interrupted evaluation."""
    if not isinstance(plan, dict) or plan.get("format") != "agent-workbench-evaluation-plan" or plan.get("version") != 1:
        raise ValueError("Invalid evaluation plan.")
    try:
        checked = evaluation_plan(plan["plugin"], executable=plan["capabilities"]["executable"], **plan["options"])
    except (KeyError, TypeError):
        raise ValueError("Invalid evaluation plan.") from None
    if any(checked.get(key) != plan.get(key) for key in ("capabilities", "inventory", "suite_case_count", "suite_sha256")):
        raise ValueError("Evaluation executable or plugin changed; create a fresh plan before running.")
    root = Path(state_root or os.environ.get("AGENT_WORKBENCH_STATE_DIR", Path.home() / ".local/state/agent-workbench")).expanduser()
    if root.is_symlink() or (root / "evaluations").is_symlink():
        raise ValueError("Evaluation state directories must not be symbolic links.")
    directory = root / "evaluations" / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    output = directory / "native"
    output.mkdir(mode=0o700)
    native_report = output / "result.json"
    options, capabilities = checked["options"], checked["capabilities"]
    argv = [capabilities["executable"], "plugin", "eval", checked["plugin"],
            "--ablation", "with-without", "--runs", str(options["runs"]),
            "--max-cost-usd", str(options["max_cost_usd"]), "--output-dir", str(output), "--json", str(native_report)]
    if capabilities["publication"] == "explicit_no_publish":
        argv.append("--no-publish")
    if "--no-scaffold" in capabilities["flags"]:
        argv.append("--no-scaffold")
    for key in ("case", "model", "judge_model"):
        if options[key] is not None:
            argv += ["--" + key.replace("_", "-"), options[key]]
    if options["trust_plugin"]:
        argv.append("--trust-plugin")
    if options["allow_tools"]:
        argv += ["--allow-tools", *options["allow_tools"]]
    write_private_json(directory / "plan.json", checked)
    started, elapsed_start, returncode, execution = now(), time.monotonic(), None, "launch_failed"
    environment = {**os.environ, "DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"}
    log_path = directory / "native-output.log"
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as log:
        process = None
        try:
            process = subprocess.Popen(argv, cwd=checked["plugin"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       env=environment, start_new_session=True)
            returncode = process.wait(timeout=options["timeout_seconds"])
            execution = "completed" if returncode == 0 else "native_failed"
        except subprocess.TimeoutExpired:
            execution = "timed_out"
        except KeyboardInterrupt:
            execution = "interrupted"
        except OSError:
            execution = "launch_failed"
        finally:
            if process is not None:
                # A grader or model child may outlive the evaluator even on exit.
                _stop_group(process)
                returncode = process.returncode
    try:
        summary = evaluation_report(native_report)
    except ValueError:
        summary = {"format": "agent-workbench-evaluation-summary", "version": 1,
                   "partial": True, "eligible_for_comparison": False, "report_status": "unavailable_or_invalid"}
    if execution != "completed":
        summary["eligible_for_comparison"] = False
    summary.update(id=directory.name, started_at=started, finished_at=now(), execution=execution,
                   exit_code=returncode, observed_wall_seconds=round(time.monotonic() - elapsed_start, 3),
                   inventory=checked["inventory"], publication="local only", state_directory=str(directory))
    write_private_json(directory / "summary.json", summary)
    return summary
