# Implementation contract

Python package: `agent_workbench/`. Public CLI: `agent-workbench`.

## State API (root owns store.py)

`Store(root: Path | None = None)` defaults to `AGENT_WORKBENCH_STATE_DIR` or
`~/.local/state/agent-workbench`.

- `create(manifest: dict, project: Path) -> dict` validates the manifest and
  creates a private run. Project must exist. Returns full state.
- `read(run_id: str) -> dict` reads an atomic snapshot.
- `update(run_id: str, callback) -> dict` locks, reads state, lets callback mutate
  it, increments revision, and atomically saves. Callback exceptions do not save.
- `directory(run_id: str) -> Path` returns the private run directory.
- `list_runs() -> list[dict]` returns snapshots.
- `validate_manifest(manifest: dict) -> dict` returns normalized copy; validates
  task IDs, dependencies/cycles, harness settings, and command argv.
- `now() -> str` UTC ISO timestamp.
- `add_event(state, kind, **fields)` appends a timestamped event.

Manifest version 1: `{version, name, goal, tasks}`. Each task has `id`,
`harness` (`codex`, `claude`, `astra`, `astra-ultra`, or `command`),
`depends_on` (default []), and either `prompt` (agent) or `argv` (command array).
Optional model and effort are explicit user choices. Command tasks run argv
without a shell. Optional task `timeout_seconds` bounds one task.

State: `{version:1, id, name, goal, project, created_at, updated_at, revision,
status:'ready', tasks:[...], events:[], notes:[], control:{stop_requested:false,
pause_requested:false, guidance:[]}, controller:null}`. Tasks retain their
manifest fields and add `status:'pending', attempts:[], result:null`.
Task states: pending, running, succeeded, failed, cancelled, interrupted.
Run states: ready, running, paused, stopped, succeeded, failed, interrupted.
Attempt metadata is extensible but must distinguish exit/failure/cancellation.
`notes` entries: `{kind:'decision'|'question'|'evidence'|'next', text, at}`.

## Runner API (runner agent owns runner.py)

`run_workflow(store, run_id, *, timeout_seconds=1800, max_launches=20) -> dict`
executes dependency-ready tasks serially in the declared project, preserving
already-succeeded tasks. Own a per-run controller lock, never steal a live run.
Check durable control flags and enforce wall-time / launch count bounds. Pausing
stops between tasks; stopping terminates owned active subprocess groups. Guidance
applies to the next agent task and is explicitly not in-flight native steering.
Retain private stdout/stderr logs and final text; record only bounded redacted
summaries in state. Handle SIGINT/SIGTERM, crash recovery, and cancelled tasks.
`request_control(store, run_id, action, text=None) -> dict` actions stop/pause/steer.
`retry_task(store, run_id, task_id) -> dict` explicitly resets failed/cancelled/
interrupted task for retry; do not rerun succeeded descendants silently.
`recover_run(store, run_id) -> dict` marks stale running attempts interrupted,
only after proving controller lock is available; does not run them.

Native CLI commands use prompt stdin and no bypass flags. Codex exec JSON;
Claude print stream-json. Astra wraps Claude. Do not force model/effort unless
manifest requests them. No token-budget enforcement claim: V1 bounds elapsed
time and task launches. Agent task success requires native terminal success,
not just exit code zero with a JSON error. Command success is exit code zero.

## Doctor API (doctor agent owns doctor.py)

`diagnose(*, project: Path | None = None, home: Path | None = None,
env: dict | None = None, probe: bool = False) -> dict`
read-only allowlisted metadata: versions, auth freshness, selected config values
with provenance, actual-vs-requested effort, active Astra local proxies. Default
does not make inference or network calls. Optional probe only configured loopback
health endpoints, no model calls. Distinguish inferred, configured and observed.
`format_report(report) -> str`. No token/account-ID/email output.

## Handoff API (handoff agent owns handoff.py)

`export_handoff(store, run_id, output: Path) -> dict` writes portable versioned
JSON plus sibling Markdown containing goal, decisions, questions, task statuses,
explicit evidence and next steps. No credentials, raw traces or hidden reasoning.
Include digest to detect accidental modifications, not a signature/trust claim.
`import_handoff(store, source: Path, project: Path) -> dict` validates and creates
a new local run with provenance. Imported completion claims remain historical;
new runnable tasks are pending and require explicit execution. No automatic
subprocess/network/external mutation. Preserve original artifact separately.

## CLI (root owns cli.py)

`doctor [--project PATH] [--probe] [--json]`
`create MANIFEST --project PATH`
`list`, `status RUN [--json]`, `plan RUN`, `run RUN [--timeout N] [--max-launches N]`
`pause RUN`, `stop RUN`, `steer RUN TEXT`, `retry RUN TASK`, `recover RUN`
`note RUN --kind decision|question|evidence|next TEXT`
`export RUN OUTPUT`, `import INPUT --project PATH`

Tests use temporary state/home/project and fake executable harnesses. One
bounded synthetic live smoke may use an actual subscription after offline tests.
