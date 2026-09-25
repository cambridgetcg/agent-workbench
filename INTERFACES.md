# Implementation contract: 0.3.0

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
Optional `adapter` is `process` (default) or `duplex`; duplex requires a Claude,
Astra or Astra Ultra task. Optional `workspace` is `shared` (default), `read-only`
or `worktree`. These fields survive export/import and single-task creation.

State: `{version:1, id, name, goal, project, created_at, updated_at, revision,
status:'ready', tasks:[...], events:[], notes:[], control:{stop_requested:false,
pause_requested:false, guidance:[]}, controller:null}`. Tasks retain their
manifest fields and add `status:'pending', attempts:[], result:null`.
Task states: pending, running, succeeded, failed, cancelled, interrupted.
Run states: ready, running, paused, stopped, succeeded, failed, interrupted.
Attempt metadata is extensible but must distinguish exit/failure/cancellation.
`notes` entries: `{kind:'decision'|'question'|'evidence'|'next', text, at}`.
`control.native_requests` is added on demand. Entries identify a run-local
request ID, task, attempt, action, status and timestamps; input text stays in
private state. Each attempt can record launch evidence, workspace ownership,
native session pointers, bounded public events and control receipts.

## Runner API (`runner.py`)

`run_workflow(store, run_id, *, timeout_seconds=1800, max_launches=20, jobs=1) -> dict`
executes dependency-ready tasks with 1–16 bounded workers, preserving
already-succeeded tasks. Own a per-run controller lock, never steal a live run.
The default is serial. `scheduler.schedule(store, run_id, deadline, interrupted,
max_launches, jobs) -> (status, reason)` runs inside that lock and signal scope.
It honors checkout compatibility, joins dependencies, drains workers on pause
or launch limit, and cancels active peers on failure without inventing an OS
signal. A busy checkout pauses with `workspace_busy`, without an attempt or retry.
Stopping terminates owned active subprocess groups. Guidance applies to the next
agent task that starts and is explicitly not in-flight native steering.
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
Claude print stream-json. Every attempt records selected executable/version,
capability/configuration evidence in private `launch.json`; launch uses the
selected absolute entry point. Astra wraps Claude. Do not force model/effort unless
manifest requests them. No token-budget enforcement claim: workflows bound elapsed
time and task launches. Agent task success requires native terminal success,
not just exit code zero with a JSON error. Command success is exit code zero.

## Checkout ownership (`workspaces.py`)

- `compatible(task, active_tasks)` permits read-only/read-only overlap and
  independent worktrees; shared writers serialize with shared readers/writers.
- `lease(store, project, mode)` holds a shared/exclusive checkout lock for a task.
  Coordination covers controllers sharing a state root, not unrelated processes.
- `prepare(store, run_id, task, attempt_id, project) -> dict` returns attempt
  workspace metadata. Worktrees are detached committed-HEAD snapshots; the
  relative project path is preserved and uncommitted files are not copied.
  Planned ownership is saved before checkout creation. Preparation failures
  retain the recorded location and a fixed failure code for inspection.
- `workspace_view(state) -> dict` lists recorded paths, ownership and retention.
- `remove_clean(store, run_id, task_id, attempt_id) -> dict` takes the controller
  lock and removes only an inactive, unchanged, owned worktree at its recorded
  base commit. It refuses dirty trees, new commits, incomplete preparation and
  ownership mismatches. Git helpers use bounded output and owned process groups.

Read-only is a declaration, not OS enforcement. Worktrees are retained by default.
Dependency completion never merges changes or copies predecessor worktrees.

## Native stream/control (`native.py`, `bridge.py`)

`NativeSession(send, emit=None, *, max_frame_bytes=2097152)` owns no subprocesses.
Runner supplies complete-byte writes and feeds incremental stdout bytes through
`feed`; `finish` records EOF and unresolved receipts. `initialize(request_id)`
must receive its matching success response before `send_user(text, request_id,
session_id=None)` or `interrupt(request_id)`. Request IDs are single-use within
the session, and user UUIDs correlate native echoes with durable caller IDs.

`duplex_arguments()` adds stream input, replayed-user receipts and stdio permission
decisions. It injects neither `--settings` nor a system-prompt override. Advertised
input flags are checked before launch; initialization then verifies the actual
protocol exchange. There is no automatic fallback after possibly executed input.
Unknown native decisions fail explicitly: tool permission callbacks receive deny
plus interruption, other unregistered callbacks receive an error. Native rules
that settle a decision first remain authoritative; no callback defaults to allow.

`normalize_event(frame, harness='claude') -> list[dict]` handles Claude-compatible
NDJSON and Codex JSON. It allowlists session/tool/task pointers, fixed event/status
values and bounded numeric usage. It excludes text, prompts, tool inputs, thinking,
raw errors, provider configuration and arbitrary paths/account values.

`PublicJournal` batches selected events into atomic state; it keeps the latest
2,000 native events and tracks dropped counts. `EventTail` reads growing private
process-adapter logs incrementally. Neither publishes raw transcript content.

`request_native(store, run_id, task_id, action, text=None, request_id=None) -> dict`
addresses an initialized running duplex attempt whose input remains open. Actions
are `input` and `interrupt`; input is 1–16,000 characters. The queue allows at most
128 retained requests and 64 queued requests, with at most 63 additional inputs
per attempt so a coalesced result stays within the 64-prompt UUID receipt bound.
Caller IDs accept 1–80 letters,
digits, underscores or hyphens, starting with an alphanumeric character.
Identical repeated IDs return the existing receipt; conflicting reuse fails.

`claim_requests` durably moves queued requests to `dispatching` before writing.
Receipts distinguish `sent`, `acknowledged`, `rejected` and `unacknowledged`.
`mark_closed`/`close_requests` mark still-queued input `not_sent` and uncertain
dispatches `unacknowledged`. Recovery closes the same attempt queue without
resending. Acknowledgment is receipt, not applied guidance or completed effects.

The runner closes input using observed turn completion, tracked local-agent and
workflow state, queued controls and a 0.5-second settling interval. This bounded
drain heuristic is not a native run-boundary guarantee or a promise of one
result per input. Native process exit, terminal success and invocation deadlines
still govern the attempt. Native session resume remains a separate explicit act.
An early native exit with unresolved input completion or tracked active tasks
fails the attempt even if an earlier turn produced a successful result.
`input_drained` and `input_drain_evidence` distinguish result UUID receipts from
the narrow legacy single-input fallback. Old overlapping-input streams without
completion receipts remain unverified rather than counting results as inputs.
The duplex child receives `CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1`; observed
native idle/running state and queue counts constrain draining. This per-process
observation setting does not replace prompts, policy or global configuration.

## Doctor API (`doctor.py`)

`diagnose(*, project: Path | None = None, home: Path | None = None,
env: dict | None = None, probe: bool = False) -> dict`
read-only allowlisted metadata: versions, auth freshness, selected config values
with provenance, actual-vs-requested effort, active Astra local proxies. Default
does not make inference or network calls. Optional probe only configured loopback
health endpoints, no model calls. Distinguish inferred, configured and observed.
`format_report(report) -> str`. No token/account-ID/email output.

## Launch/configuration evidence (`capabilities.py`)

`inspect_launch(task, project, *, env=None, home=None) -> dict` resolves PATH
candidates and selected absolute entry point. Native CLIs receive version/help
probes only (Codex also exec help); command tasks and Astra wrappers are not
executed during discovery. A wrapper's native record is its PATH child candidate,
not proof of what the wrapper launches. Capability evidence distinguishes
advertised interfaces from runtime control acknowledgments and unavailable data.

`explain_configuration(project, *, home=None, env=None) -> dict` reads selected
local settings, scalar candidates with contributing sources, permission/hook
counts, model-related environment candidates and plugin manifest versions. It
checks instruction-file presence without reading bodies/imports. It does not
resolve complete native policy/trust/merge behavior or read authentication files.
Plugin source/cache consistency compares version metadata, not byte identity.

## Plugin evaluation (`evaluation.py`)

`plugin_inventory(plugin) -> dict` counts bounded local component authoring files
and hashes their bytes. Approximate tokens use ceil(characters/4), not a tokenizer
or measured loaded context; external components and dynamic context are omitted.

`evaluation_plan(plugin, *, executable='claude', runs=3, case=None, max_cases=20,
timeout_seconds=900, max_cost_usd=5, model=None, judge_model=None, allow_tools=(),
trust_plugin=False) -> dict` inspects a conventional `evals/` suite and native
evaluator help without inference. It pins plugin/suite hashes and executable
metadata. Runs are 1–10 per arm, suites at most 50 cases, timeout at most 3,600
seconds and native list-price cost ceiling USD 0.01–100. The case bound covers
the complete suite even when a filter is supplied. Only established local-report
contracts pass; no publish operation or automatic tool/trust grant is added.

`run_evaluation(plan, *, state_root=None) -> dict` rechecks the plan and explicitly
launches the native with/without evaluator. It records private output, stops its
process group on timeout/interruption and never retries. Native cost limits may
overrun by one agent run and are not hard account-charge guarantees.

`evaluation_report(source) -> dict` accepts supported native report schema 1 and
returns allowlisted numeric grader outcomes without prompts/traces/evidence text.
Partial, errored, unbalanced or incompletely graded runs are ineligible for
comparison. Missing measurements stay null; scores do not prove causation.

## Optional native mod checks (`mods.py`)

`check_mod(plugin, *, executable='claude', enable_function_hooks=False,
run_tests=False) -> dict` inspects a selected executable, performs strict plugin
validation and optionally runs native host tests. Every process uses temporary
`CLAUDE_CONFIG_DIR`, disabled updates/nonessential traffic, bounded output and
process-group cleanup. Function hooks are enabled only by explicit per-process
opt-in. Test execution is explicit; validation alone does not prove runtime
availability. Unknown output, zero tests and incompatible hosts remain unverified
or unavailable rather than passing. No global installation or model invocation
is requested; tests execute the selected plugin's test code with its mocked host.

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
Selected numeric usage is retained. `metadata_view(state) -> dict` is the optional
frontend's smaller contract: run/task states, task IDs/harnesses, attempt counts
and queue counts, without goals, summaries, notes, paths or logs.

## CLI (`cli.py`)

```text
doctor [--project PATH] [--probe] [--json]
explain [--project PATH] [--harness HARNESS] [--json]
create [MANIFEST] [--project PATH]
create --harness codex|claude|astra|astra-ultra (--prompt TEXT | --prompt-file FILE|-)
       [--project PATH] [--name TEXT] [--goal TEXT] [--model MODEL] [--effort EFFORT]
       [--adapter process|duplex] [--workspace shared|read-only|worktree]
list [--project PATH] [--json]
status RUN [--json] [--metadata]
launch RUN TASK [--attempt ATTEMPT]
workspaces RUN
workspace-remove RUN TASK ATTEMPT
timeline RUN [--limit N] [--json]
plan RUN
run RUN [--timeout N] [--max-launches N] [--jobs N]
send RUN TASK TEXT [--request-id ID]
interrupt RUN TASK [--request-id ID]
pause RUN
stop RUN
steer RUN TEXT
retry RUN TASK
recover RUN
note RUN --kind decision|question|evidence|next TEXT
export RUN OUTPUT
import INPUT --project PATH
plugin-cost PLUGIN
eval-plan PLUGIN [--executable PATH] [--runs N] [--case FILTER] [--max-cases N]
          [--timeout N] [--max-cost-usd N] [--model MODEL] [--judge-model MODEL]
          [--allow-tool RULE] [--trust-plugin] [--output FILE]
eval-run PLAN
eval-report NATIVE_REPORT
mod-check PLUGIN [--executable PATH] [--enable-function-hooks] [--test]
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

The separate `extensions/claude-mod` frontend registers `/workbench-status RUN`.
It calls fixed read-only CLI argument arrays and renders selected metadata with
Refresh/Close. It is not activated by ordinary Workbench installation. Native
host tests passed on Claude Code 2.1.282 with the explicit per-process function
hooks flag; early-access availability is runtime-specific. No configuration,
permissions, execution or model context is changed by displaying the pane.

Tests use temporary state/home/project and fake executable harnesses. One
bounded synthetic live smoke may use an actual subscription after offline tests.
