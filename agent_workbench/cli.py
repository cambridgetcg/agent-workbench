"""Command-line interface for the shared local workbench."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .store import Store, add_event, now, MAX_DOCUMENT_BYTES


def load_manifest(path: Path) -> dict:
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("Workflow manifest is too large.")
    return json.loads(path.read_text())


def status_view(state: dict) -> dict:
    last_boundary = next((e for e in reversed(state.get("events", [])) if e.get("kind") == "run_" + state["status"]), {})
    return {
        "id": state["id"], "name": state["name"], "goal": state["goal"],
        "project": state["project"], "status": state["status"],
        "revision": state["revision"], "updated_at": state["updated_at"],
        "control": {"stop_requested": state["control"].get("stop_requested", False),
                    "pause_requested": state["control"].get("pause_requested", False),
                    "queued_guidance_count": len(state["control"].get("guidance", []))},
        "tasks": [{"id": t["id"], "harness": t["harness"], "status": t["status"],
                   "depends_on": t["depends_on"], "attempts": len(t["attempts"]),
                   "result": t.get("result")} for t in state["tasks"]],
        "notes_count": len(state["notes"]),
        "imported_history": bool(state.get("handoff")),
        "status_reason": last_boundary.get("reason"),
    }


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
    for task in view["tasks"]:
        deps = ", ".join(task["depends_on"]) or "none"
        print(f"  {task['id']}: {task['status']} ({task['harness']}, attempts {task['attempts']}, after {deps})")
    if view["imported_history"]:
        print("Imported history is retained separately; local tasks require new execution.")


def plan_view(state: dict) -> dict:
    from .handoff import TASK_FIELDS
    from .runner import redact_summary
    def redacted(value):
        if isinstance(value, str):
            return redact_summary(value, limit=max(len(value) + 1, 16_000))
        if isinstance(value, dict):
            return {k: redacted(v) for k, v in value.items()}
        if isinstance(value, list):
            return [redacted(v) for v in value]
        return value
    return redacted({"id": state["id"], "manifest": {
        "version": 1, "name": state["name"], "goal": state["goal"],
        "tasks": [{k: v for k, v in task.items() if k in TASK_FIELDS} for task in state["tasks"]]},
        "notes": state["notes"], "handoff": state.get("handoff"),
        "display": "Credential-like strings are redacted in this view; the saved plan is unchanged."})


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-workbench", description="Local diagnostics, durable workflows and handoffs for Codex and Claude Code.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--state-dir", type=Path, help="Override private local state directory")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="Read redacted local configuration and auth metadata")
    d.add_argument("--project", type=Path, default=Path.cwd())
    d.add_argument("--probe", action="store_true", help="Probe existing loopback gateways without inference")
    d.add_argument("--json", action="store_true")
    c = sub.add_parser("create", help="Validate a workflow manifest and save an unstarted run")
    c.add_argument("manifest", type=Path)
    c.add_argument("--project", type=Path, required=True)
    ls = sub.add_parser("list", help="List saved local runs")
    ls.add_argument("--json", action="store_true")
    s = sub.add_parser("status", help="Show tasks, attempts and run state")
    s.add_argument("run_id")
    s.add_argument("--json", action="store_true")
    plan = sub.add_parser("plan", help="Inspect the saved task definitions, notes and imported provenance")
    plan.add_argument("run_id")
    run = sub.add_parser("run", help="Execute pending workflow tasks with native harnesses")
    run.add_argument("run_id")
    run.add_argument("--timeout", type=float, default=1800, help="Overall wall-time limit in seconds")
    run.add_argument("--max-launches", type=int, default=20, help="Maximum task subprocess launches in this invocation")
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
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            from .doctor import diagnose, format_report
            report = diagnose(project=args.project, probe=args.probe)
            print(json.dumps(report, indent=2) if args.json else format_report(report))
            return 0
        store = Store(args.state_dir)
        if args.command == "create":
            state = store.create(load_manifest(args.manifest), args.project)
            print(state["id"])
        elif args.command == "list":
            states = store.list_runs()
            if args.json:
                print(json.dumps([status_view(s) for s in states], indent=2))
            else:
                for s in states:
                    print(f"{s['id']}  {s['status']}  {safe_terminal(s['name'])}")
                if not states:
                    print("No saved runs.")
        elif args.command == "status":
            print_status(store.read(args.run_id), args.json)
        elif args.command == "plan":
            print(json.dumps(plan_view(store.read(args.run_id)), indent=2))
        elif args.command == "run":
            from .runner import run_workflow
            state = run_workflow(store, args.run_id, timeout_seconds=args.timeout, max_launches=args.max_launches)
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
