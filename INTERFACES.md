# Implementation contract

Python package: `agent_workbench/`. Public CLI: `agent-workbench`.

## State API (`store.py`)

`Store(root: Path | None = None)` defaults to `AGENT_WORKBENCH_STATE_DIR` or
`~/.local/state/agent-workbench`.

- `create(manifest: dict, project: Path) -> dict` validates the manifest and
  creates a private run. Project must exist. Returns full state.
- `read(run_id: str) -> dict` reads an atomic snapshot.
- `resolve_run_id(reference: str) -> str` resolves a full run ID or unique
  lowercase hexadecimal prefix of at least eight characters. Reject missing or
  ambiguous matches; never choose the latest run.
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

## Runner API (`runner.py`)

`run_workflow(store, run_id, *, timeout_seconds=1800, max_launches=20) -> dict`
executes dependency-ready tasks serially in the declared project, preserving
already-succeeded tasks. Own a per-run controller lock, never steal a live run.
Check durable control flags and enforce wall-time / launch count bounds. Pausing
stops between tasks; stopping terminates owned active subprocess groups. Guidance
applies to the next agent task and is explicitly not in-flight native steering.
Each agent prompt receives at most eight recent notes within an 8,000-character
continuity section, including framing and omission/truncation notices. Notes are
redacted and quoted as historical data; imported notes are unverified claims.
Notes matching the shared handoff credential check are withheld with a notice.
Task instructions and queued guidance take precedence. Stored notes are unchanged;
referenced artifacts are not opened and notes do not change task status.
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

## Doctor API (`doctor.py`)

`diagnose(*, project: Path | None = None, home: Path | None = None,
env: dict | None = None, probe: bool = False) -> dict`
read-only allowlisted metadata: versions, auth freshness, selected config values
with provenance, actual-vs-requested effort, active Astra local proxies. Default
does not make inference or network calls. Optional probe only configured loopback
health endpoints, no model calls. Distinguish inferred, configured and observed.
`format_report(report) -> str`. No token/account-ID/email output.

## Handoff API (`handoff.py`)

`export_handoff(store, run_id, output: Path) -> dict` writes portable versioned
JSON plus sibling Markdown containing goal, decisions, questions, task statuses,
explicit evidence and next steps. No credentials, raw traces or hidden reasoning.
Include digest to detect accidental modifications, not a signature/trust claim.
`import_handoff(store, source: Path, project: Path) -> dict` validates and creates
a new local run with provenance. Imported completion claims remain historical;
new runnable tasks are pending and require explicit execution. No automatic
subprocess/network/external mutation. Preserve original artifact separately.

## Read-only views (`views.py`)

`status_details(state: dict) -> dict` selects the latest attempt for each task,
explains dependency waits and returns explicit next actions. Recorded controller
metadata does not prove process liveness; only recovery checks the controller lock.

`timeline_view(state: dict, limit: int = 20) -> dict` returns the latest 1–200
entries in chronological order, a total count and a truncation flag. Selected
event fields are enriched with matching attempt metadata; attempts missing event
records still appear. Allowlisted scalar fields exclude prompts, note/guidance
text, log paths and raw output. Neither view runs tasks or resumes native sessions.

## CLI (`cli.py`)

```text
doctor [--project PATH] [--probe] [--json]
create [MANIFEST] [--project PATH]
create --harness codex|claude|astra|astra-ultra (--prompt TEXT | --prompt-file FILE|-)
       [--project PATH] [--name TEXT] [--goal TEXT] [--model MODEL] [--effort EFFORT]
list [--project PATH] [--json]
status RUN [--json]
timeline RUN [--limit N] [--json]
plan RUN
run RUN [--timeout N] [--max-launches N]
pause RUN
stop RUN
steer RUN TEXT
retry RUN TASK
recover RUN
note RUN --kind decision|question|evidence|next TEXT
export RUN OUTPUT
import INPUT --project PATH
```

Creation takes a JSON manifest file (`-` for stdin) or single-task options, never
both. The single-task form requires an explicit harness and prompt, assigns task
ID `work`, and derives the run name from its first line, capped at 160 characters.
The goal defaults to the prompt up to 20,000 characters, or to the run name for
longer prompts. Explicit `--goal` wins; the task prompt is preserved. All creation
paths use the shared manifest validator and save an
unstarted run. `--project` defaults to the current directory for create/doctor;
import still requires it explicitly. Manifest input is bounded at 2,000,000 bytes;
prompt-file input at 400,000 bytes, followed by normal text validation.

The CLI resolves every `RUN` reference before read or mutation. `list --project`
matches resolved project paths. Status selects results, attempt/session pointers,
dependency blockers and explicit next actions. Timeline exposes bounded selected
event fields and attempt metadata, excluding raw logs and note/guidance text.
These views neither run tasks nor resume native sessions.

Tests use temporary state/home/project and fake executable harnesses. One
bounded synthetic live smoke may use an actual subscription after offline tests.
