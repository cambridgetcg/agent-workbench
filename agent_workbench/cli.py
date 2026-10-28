"""Command-line interface for the shared local workbench."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .store import Store, add_event, now, MAX_DOCUMENT_BYTES, HARNESSES, EFFORTS, SANDBOXES


def read_input(path: Path, *, limit: int = MAX_DOCUMENT_BYTES) -> str:
    """Read bounded UTF-8 text from a file or stdin without requiring a temp file."""
    if str(path) == "-":
        if sys.stdin.isatty():
            raise ValueError("Pipe input to stdin or supply a file path.")
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        data = stream.read(limit + 1)
    else:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
    if isinstance(data, bytes):
        if len(data) > limit:
            raise ValueError("Input is too large.")
        return data.decode("utf-8")
    if len(data.encode("utf-8")) > limit:
        raise ValueError("Input is too large.")
    return data


def load_manifest(path: Path) -> dict:
    return json.loads(read_input(path))


def creation_manifest(args) -> dict:
    simple_options = (args.harness, args.prompt, args.prompt_file, args.name, args.goal, args.model, args.effort, args.adapter,
                      args.workspace, args.base_ref, args.sandbox, args.max_turns, args.skill, args.run_budget,
                      args.timeout_seconds)
    if args.manifest is not None:
        if any(value is not None for value in simple_options):
            raise ValueError("Use a manifest or prompt options, not both.")
        return load_manifest(args.manifest)
    if args.harness is None or (args.prompt is None and args.prompt_file is None):
        raise ValueError("Provide a manifest (or - for stdin), or --harness with --prompt/--prompt-file.")
    prompt = args.prompt if args.prompt is not None else read_input(args.prompt_file, limit=400_000)
    task = {"id": "work", "harness": args.harness, "prompt": prompt}
    for field, option in (("model", "model"), ("effort", "effort"), ("adapter", "adapter"), ("workspace", "workspace"),
                          ("base_ref", "base_ref"), ("sandbox", "sandbox"), ("max_turns", "max_turns"),
                          ("skills", "skill"), ("run_budget_seconds", "run_budget"),
                          ("timeout_seconds", "timeout_seconds")):
        if getattr(args, option) is not None:
            task[field] = getattr(args, option)
    name = args.name if args.name is not None else prompt.strip().split("\n")[0][:160]
    goal = args.goal if args.goal is not None else (prompt if len(prompt) <= 20_000 else name)
    return {"version": 1, "name": name, "goal": goal, "tasks": [task]}


def redacted_view(value):
    from .runner import redact_summary
    if isinstance(value, str):
        return redact_summary(value, limit=max(len(value) + 1, 16_000))
    if isinstance(value, dict):
        return {key: redacted_view(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redacted_view(item) for item in value]
    return value


def coordination_view(state: dict) -> dict:
    from .collab import coordination_view as view
    return view(state)


def status_view(state: dict) -> dict:
    from .views import status_details
    details = status_details(state)
    last_boundary = next((e for e in reversed(state.get("events", [])) if e.get("kind") == "run_" + state["status"]), {})
    return redacted_view({
        "id": state["id"], "name": state["name"], "goal": state["goal"],
        "project": state["project"], "status": state["status"],
        "revision": state["revision"], "updated_at": state["updated_at"],
        "control": {"stop_requested": state["control"].get("stop_requested", False),
                    "pause_requested": state["control"].get("pause_requested", False),
                    "queued_guidance_count": len(state["control"].get("guidance", [])),
                    "native_requests": [{key: value for key, value in request.items() if key != "text"}
                                        for request in state["control"].get("native_requests", [])]},
        "tasks": [{"id": t["id"], "harness": t["harness"], "status": t["status"],
                   "depends_on": t["depends_on"], "attempts": len(t["attempts"]),
                   "result": t.get("result"), **details["tasks"][t["id"]]} for t in state["tasks"]],
        "coordination": coordination_view(state),
        "notes_count": len(state["notes"]),
        "imported_history": bool(state.get("handoff")),
        "status_reason": last_boundary.get("reason"),
        "next_actions": details["next_actions"],
    })


def safe_terminal(value) -> str:
    return "".join(c if c.isprintable() else " " for c in str(value))


def print_status(state: dict, as_json=False) -> None:
    view = status_view(state)
    if as_json:
        print(json.dumps(view, indent=2))
        return
    print(f"{view['id']}  {view['status']}  {safe_terminal(view['name'])}")
    print(f"Goal: {safe_terminal(view['goal'])}")
    if view["status_reason"]:
        print(f"Reason: {safe_terminal(view['status_reason'])}")
    coordination = view["coordination"]
    if coordination["collab"]:
        parent = coordination["parent_session_id"]
        print("Collab: agent tasks get the Workbench board note" + (
            f"; declared parent {parent} (unverified lineage; create-time check {coordination['parent_check']})" if parent else ""))
    for task in view["tasks"]:
        deps = ", ".join(task["depends_on"]) or "none"
        print(f"  {task['id']}: {task['status']} ({task['harness']}, attempts {task['attempts']}, after {deps})")
        print(f"    {safe_terminal(task['status_reason'])}")
        if task.get("result"):
            from .runner import redact_summary
            print(f"    Result: {safe_terminal(redact_summary(task['result'].get('summary', ''), 400))}")
        attempt = task.get("latest_attempt") or {}
        if attempt.get("id"):
            print(f"    Attempt: {safe_terminal(attempt['id'])}")
        if attempt.get("native_session_id"):
            print(f"    Native session (local pointer): {safe_terminal(attempt['native_session_id'])}")
    for action in view["next_actions"]:
        target = " " + action["task_id"] if action.get("task_id") else ""
        print(f"Next: {action['action']}{target} — {safe_terminal(action['reason'])}")
    if view["imported_history"]:
        print("Imported history is retained separately; local tasks require new execution.")


def print_timeline(view: dict, as_json=False) -> None:
    if as_json:
        print(json.dumps(view, indent=2))
        return
    print(f"{view['id']}  {view['status']}  ({len(view['entries'])} of {view['total_entries']} events)")
    for entry in view["entries"]:
        fields = "  ".join(f"{key}={safe_terminal(value)}" for key, value in entry.items() if key not in {"at", "kind"} and value is not None)
        print(f"{safe_terminal(entry.get('at', ''))}  {safe_terminal(entry['kind'])}  {fields}")


def plan_view(state: dict) -> dict:
    from .handoff import TASK_FIELDS
    return redacted_view({"id": state["id"], "manifest": {
        "version": 1, "name": state["name"], "goal": state["goal"],
        "tasks": [{k: v for k, v in task.items() if k in TASK_FIELDS} for task in state["tasks"]]},
        "coordination": coordination_view(state),
        "notes": state["notes"], "handoff": state.get("handoff"),
        "display": "Credential-like strings are redacted in this view; the saved plan is unchanged."})


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-workbench", description="Local diagnostics, durable workflows and handoffs for Codex, Claude Code and Hermes.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--state-dir", type=Path, help="Override private local state directory")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="Read redacted local configuration and auth metadata")
    d.add_argument("--project", type=Path, default=Path.cwd())
    d.add_argument("--probe", action="store_true", help="Probe existing loopback gateways without inference")
    d.add_argument("--json", action="store_true")
    explain = sub.add_parser("explain", help="Explain partial configuration, instructions and launch capabilities")
    explain.add_argument("--project", type=Path, default=Path.cwd())
    explain.add_argument("--harness", choices=sorted(HARNESSES - {"command"}))
    explain.add_argument("--json", action="store_true")
    c = sub.add_parser("create", help="Save an unstarted workflow from a manifest or an agent prompt")
    c.add_argument("manifest", type=Path, nargs="?", help="JSON file, or - to read JSON from stdin")
    c.add_argument("--project", type=Path, default=Path.cwd(), help="Project directory (default: current directory)")
    c.add_argument("--harness", choices=sorted(HARNESSES - {"command"}), help="Explicit harness for a single task")
    prompt = c.add_mutually_exclusive_group()
    prompt.add_argument("--prompt", help="Single task prompt")
    prompt.add_argument("--prompt-file", type=Path, help="Prompt file, or - to read prompt text from stdin")
    c.add_argument("--name", help="Run name (default: first prompt line)")
    c.add_argument("--goal", help="Workflow goal (default: prompt, or run name for prompts over 20,000 characters)")
    c.add_argument("--model", help="Explicit native model override")
    c.add_argument("--effort", choices=sorted(EFFORTS), help="Explicit native effort override")
    c.add_argument("--adapter", choices=["process", "duplex"], help="Use explicit native duplex controls where supported")
    c.add_argument("--workspace", choices=["shared", "read-only", "worktree"], help="Checkout ownership (read-only also selects Codex's read-only sandbox)")
    c.add_argument("--base-ref", help="Worktree base revision, such as origin/main (default: the project's HEAD)")
    c.add_argument("--sandbox", choices=sorted(SANDBOXES), help="Codex sandbox for this task (default: native configuration)")
    c.add_argument("--max-turns", type=int, help="Hermes tool-calling iterations per turn")
    c.add_argument("--skill", action="append", help="Hermes skill to preload; repeat for several")
    c.add_argument("--run-budget", type=int, help="Hermes graceful wall-clock budget in seconds (below --timeout-seconds)")
    c.add_argument("--timeout-seconds", type=float, help="Hard time limit for this task in seconds")
    c.add_argument("--collab", action="store_true",
                   help="Give agent tasks a Workbench note on using the agenttool collab board (also with a manifest)")
    c.add_argument("--collab-parent", metavar="SESSION_ID",
                   help="Declare a collab parent session for the note's children (implies --collab; checked read-only)")
    ls = sub.add_parser("list", help="List saved local runs")
    ls.add_argument("--json", action="store_true")
    ls.add_argument("--project", type=Path, help="Only show runs for this project")
    s = sub.add_parser("status", help="Show tasks, attempts and run state")
    s.add_argument("run_id")
    s.add_argument("--json", action="store_true")
    s.add_argument("--metadata", action="store_true", help="Omit goal, prompts, summaries and project paths")
    launch = sub.add_parser("launch", help="Inspect a recorded attempt's launch evidence")
    launch.add_argument("run_id")
    launch.add_argument("task_id")
    launch.add_argument("--attempt", help="Attempt ID (default: latest for this task)")
    workspaces = sub.add_parser("workspaces", help="List per-attempt checkout ownership; path_exists and dirty are observed now")
    workspaces.add_argument("run_id")
    cleanup = sub.add_parser("workspace-remove", help="Explicitly remove a clean Workbench-owned worktree at its base commit or with HEAD on a remote-tracking ref")
    cleanup.add_argument("run_id")
    cleanup.add_argument("task_id")
    cleanup.add_argument("attempt_id")
    for action in ("send", "interrupt"):
        command = sub.add_parser(action, help="Address an active native duplex task; receipts do not imply applied effects")
        command.add_argument("run_id")
        command.add_argument("task_id")
        if action == "send":
            command.add_argument("text")
        command.add_argument("--request-id", help="Caller ID for deduplication; repeated IDs are never resent")
    plan = sub.add_parser("plan", help="Inspect the saved task definitions, notes and imported provenance")
    plan.add_argument("run_id")
    collab = sub.add_parser("collab", help="Attribute agenttool collab sessions to this run's attempts (read-only, computed live)")
    collab.add_argument("run_id")
    collab.add_argument("--json", action="store_true")
    timeline = sub.add_parser("timeline", help="Show recent run events and attempt outcomes without raw logs")
    timeline.add_argument("run_id")
    timeline.add_argument("--limit", type=int, default=20, help="Maximum events to show (1–200)")
    timeline.add_argument("--json", action="store_true")
    run = sub.add_parser("run", help="Execute pending workflow tasks with native harnesses")
    run.add_argument("run_id")
    run.add_argument("--timeout", type=float, default=1800, help="Overall wall-time limit in seconds")
    run.add_argument("--max-launches", type=int, default=20, help="Maximum task subprocess launches in this invocation")
    run.add_argument("--jobs", type=int, default=1, help="Concurrent tasks, 1–16; shared writers still serialize")
    for action, help_text in [("pause", "Pause between tasks"), ("stop", "Stop the owned active task and its ordinary descendants"), ("recover", "Mark a stale controller interrupted without rerunning work")]:
        sub.add_parser(action, help=help_text).add_argument("run_id")
    steer = sub.add_parser("steer", help="Queue guidance for the next agent task, not the active turn")
    steer.add_argument("run_id")
    steer.add_argument("text")
    retry = sub.add_parser("retry", help="Explicitly reset one unsuccessful task to pending")
    retry.add_argument("run_id")
    retry.add_argument("task_id")
    note = sub.add_parser("note", help="Record a decision, question, evidence reference or next step")
    note.add_argument("run_id")
    note.add_argument("--kind", choices=["decision", "question", "evidence", "next"], required=True)
    note.add_argument("text")
    ex = sub.add_parser("export", help="Write a portable JSON handoff and Markdown summary")
    ex.add_argument("run_id")
    ex.add_argument("output", type=Path)
    im = sub.add_parser("import", help="Import history as a new unstarted run")
    im.add_argument("source", type=Path)
    im.add_argument("--project", type=Path, required=True)
    ep = sub.add_parser("eval-plan", help="Inspect a bounded, local-only plugin baseline comparison without inference")
    ep.add_argument("plugin", type=Path)
    ep.add_argument("--executable", default="claude")
    ep.add_argument("--runs", type=int, default=3)
    ep.add_argument("--case")
    ep.add_argument("--max-cases", type=int, default=20)
    ep.add_argument("--timeout", type=int, default=900)
    ep.add_argument("--max-cost-usd", type=float, default=5)
    ep.add_argument("--model")
    ep.add_argument("--judge-model")
    ep.add_argument("--allow-tool", action="append", default=[])
    ep.add_argument("--trust-plugin", action="store_true", help="Explicit native trust of the selected evaluation directory")
    ep.add_argument("--output", type=Path, help="Save the plan privately; refuses an existing file")
    er = sub.add_parser("eval-run", help="Execute a saved plugin evaluation plan using the native evaluator")
    er.add_argument("plan", type=Path)
    report = sub.add_parser("eval-report", help="Summarize measured native evaluation outcomes without traces")
    report.add_argument("report", type=Path)
    inventory = sub.add_parser("plugin-cost", help="Inventory plugin context bytes and explicitly estimated tokens")
    inventory.add_argument("plugin", type=Path)
    mod = sub.add_parser("mod-check", help="Validate an optional mod against a selected native runtime without model calls")
    mod.add_argument("plugin", type=Path)
    mod.add_argument("--executable", default="claude")
    mod.add_argument("--enable-function-hooks", action="store_true", help="Opt in for this isolated checking process only")
    mod.add_argument("--test", action="store_true", help="Also run the mod's native host tests")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            from .doctor import diagnose, format_report
            report = diagnose(project=args.project, probe=args.probe)
            print(json.dumps(report, indent=2) if args.json else format_report(report))
            return 0
        if args.command == "explain":
            from .capabilities import explain_configuration, inspect_launch
            report = inspect_launch({"harness": args.harness}, args.project) if args.harness else explain_configuration(args.project)
            # JSON is also the readable default: evidence and limitations stay
            # adjacent to each value, without flattening uncertain provenance.
            print(json.dumps(redacted_view(report), indent=2))
            return 0
        if args.command == "mod-check":
            from .mods import check_mod
            report = check_mod(args.plugin, executable=args.executable,
                               enable_function_hooks=args.enable_function_hooks, run_tests=args.test)
            print(json.dumps(redacted_view(report), indent=2))
            return 0 if report.get("ok") else 1
        if args.command in {"eval-plan", "eval-run", "eval-report", "plugin-cost"}:
            from .evaluation import evaluation_plan, run_evaluation, evaluation_report, plugin_inventory
            if args.command == "eval-plan":
                report = evaluation_plan(args.plugin, executable=args.executable, runs=args.runs, case=args.case,
                    max_cases=args.max_cases, timeout_seconds=args.timeout, max_cost_usd=args.max_cost_usd,
                    model=args.model, judge_model=args.judge_model, allow_tools=args.allow_tool, trust_plugin=args.trust_plugin)
                if args.output:
                    from .handoff import _write_new
                    _write_new(args.output, (json.dumps(report, indent=2) + "\n").encode())
            elif args.command == "eval-run":
                report = run_evaluation(load_manifest(args.plan), state_root=Store(args.state_dir).root)
            elif args.command == "eval-report":
                report = evaluation_report(args.report)
            else:
                report = plugin_inventory(args.plugin)
            print(json.dumps(redacted_view(report), indent=2))
            return 1 if args.command == "eval-run" and (report.get("execution") != "completed" or report.get("partial")) else 0
        store = Store(args.state_dir)
        if hasattr(args, "run_id"):
            args.run_id = store.resolve_run_id(args.run_id)
        if args.command == "create":
            manifest = creation_manifest(args)
            coordination = None
            if args.collab or args.collab_parent is not None:
                coordination = {"collab": True, "parent_session_id": None, "parent_check": None}
            if args.collab_parent is not None:
                from .collab import check_parent
                from .store import validate_manifest
                project = args.project.expanduser().resolve()
                if not project.is_dir():
                    raise ValueError("Project must be an existing directory.")
                harnesses = [task["harness"] for task in validate_manifest(manifest)["tasks"]]
                check = check_parent(harnesses, project, args.collab_parent)
                coordination.update(parent_session_id=args.collab_parent, parent_check=check)
            state = store.create(manifest, args.project, coordination=coordination)
            print(state["id"])
        elif args.command == "list":
            states = store.list_runs()
            if args.project is not None:
                project = str(args.project.expanduser().resolve())
                states = [state for state in states if state["project"] == project]
            if args.json:
                print(json.dumps([status_view(s) for s in states], indent=2))
            else:
                for s in states:
                    print(f"{s['id']}  {s['status']}  {safe_terminal(redacted_view(s['name']))}")
                if not states:
                    print("No saved runs.")
        elif args.command == "status":
            state = store.read(args.run_id)
            if args.metadata:
                from .views import metadata_view
                print(json.dumps(metadata_view(state), indent=2))
            else:
                print_status(state, args.json)
        elif args.command == "launch":
            state = store.read(args.run_id)
            task = next((t for t in state["tasks"] if t["id"] == args.task_id), None)
            attempts = (task or {}).get("attempts", [])
            attempt = next((a for a in attempts if a["id"] == args.attempt), None) if args.attempt else (attempts[-1] if attempts else None)
            if not attempt or not attempt.get("launch_profile"):
                raise ValueError("No recorded launch profile for this attempt")
            path = store.directory(args.run_id) / attempt["launch_profile"]
            if not path.resolve().is_relative_to(store.directory(args.run_id).resolve()) or path.is_symlink() or not path.is_file():
                raise ValueError("Invalid launch profile path")
            print(json.dumps(redacted_view(load_manifest(path)), indent=2))
        elif args.command == "workspaces":
            from .workspaces import workspace_view
            print(json.dumps(redacted_view(workspace_view(store.read(args.run_id), observe=True)), indent=2))
        elif args.command == "workspace-remove":
            from .workspaces import remove_clean
            print_status(remove_clean(store, args.run_id, args.task_id, args.attempt_id))
        elif args.command in {"send", "interrupt"}:
            from .bridge import request_native
            report = request_native(store, args.run_id, args.task_id, "input" if args.command == "send" else "interrupt",
                                    getattr(args, "text", None), args.request_id)
            print(json.dumps(report, indent=2))
        elif args.command == "plan":
            print(json.dumps(plan_view(store.read(args.run_id)), indent=2))
        elif args.command == "collab":
            from .collab import attribution, format_attribution
            view = redacted_view(attribution(store.read(args.run_id)))
            print(json.dumps(view, indent=2) if args.json else format_attribution(view))
        elif args.command == "timeline":
            from .views import timeline_view
            print_timeline(timeline_view(store.read(args.run_id), limit=args.limit), args.json)
        elif args.command == "run":
            from .runner import budgets_beyond_run_timeout, run_workflow
            # run_workflow rejects a non-positive limit; warn only about a usable one.
            late_budgets = budgets_beyond_run_timeout(store.read(args.run_id), args.timeout) if args.timeout > 0 else []
            for late in late_budgets:
                print(f"agent-workbench: warning: task {safe_terminal(late['task_id'])} has a Hermes run budget of "
                      f"{late['run_budget_seconds']} s, but run --timeout {args.timeout:g} s stops the run first, "
                      "so Hermes cannot wrap up; raise --timeout above the budget.", file=sys.stderr)
            state = run_workflow(store, args.run_id, timeout_seconds=args.timeout, max_launches=args.max_launches, jobs=args.jobs)
            print_status(state)
            return 0 if state["status"] == "succeeded" else 1 if state["status"] == "failed" else 2
        elif args.command in {"pause", "stop", "steer"}:
            from .runner import request_control
            print_status(request_control(store, args.run_id, args.command, getattr(args, "text", None)))
        elif args.command == "retry":
            from .runner import retry_task
            print_status(retry_task(store, args.run_id, args.task_id))
        elif args.command == "recover":
            from .runner import recover_run
            print_status(recover_run(store, args.run_id))
        elif args.command == "note":
            if not args.text.strip() or len(args.text) > 10_000 or "\0" in args.text:
                raise ValueError("Note must contain 1–10,000 characters.")
            def append(state):
                state["notes"].append({"kind": args.kind, "text": args.text, "at": now()})
                add_event(state, "note_added", note_kind=args.kind)
            store.update(args.run_id, append)
            print(f"Recorded {args.kind} note.")
        elif args.command == "export":
            from .handoff import export_handoff
            export_handoff(store, args.run_id, args.output)
            print(f"Handoff: {args.output}")
            print(f"Summary: {args.output.with_suffix('.md')}")
        elif args.command == "import":
            from .handoff import import_handoff
            state = import_handoff(store, args.source, args.project)
            print(state["id"])
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        print(f"agent-workbench: {safe_terminal(error)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("agent-workbench: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
