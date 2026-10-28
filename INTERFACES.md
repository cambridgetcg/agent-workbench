# Implementation contract: 0.5.1

Python package: `agent_workbench/`. Public CLI: `agent-workbench`.

## State API (`store.py`)

`Store(root: Path | None = None)` defaults to `AGENT_WORKBENCH_STATE_DIR` or
`~/.local/state/agent-workbench`.

- `create(manifest: dict, project: Path, *, coordination: dict | None = None) -> dict`
  validates the manifest and creates a private run. Project must exist. Returns
  full state. `coordination` (validated by `validate_coordination`) is the
  run-level collab opt-in `{collab: true, parent_session_id: str|null,
  parent_check: null|"matched"|"unavailable"}` (a check exactly when a parent is
  set), saved as `state["coordination"]` beside the manifest, never in it.
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
`harness` (`codex`, `claude`, `astra`, `astra-ultra`, `hermes`,
`hermes-astra-ultra`, or `command`),
`depends_on` (default []), and either `prompt` (agent) or `argv` (command array).
Optional model and effort are explicit user choices. Command tasks run argv
without a shell. Optional task `timeout_seconds` bounds one task.
Optional `adapter` is `process` (default) or `duplex`; duplex requires a Claude,
Astra or Astra Ultra task. Optional `workspace` is `shared` (default), `read-only`
or `worktree`. A worktree task may set `base_ref`, one Git revision without
option dashes, whitespace or ranges. A Codex task may set `sandbox`
(`read-only`, `workspace-write`, `danger-full-access`); a read-only workspace
implies and requires `read-only`. Hermes tasks may set `max_turns` (1–10000),
`skills` (1–32 unique names) and `run_budget_seconds` (an integer below any
`timeout_seconds`). These fields survive export/import and single-task creation;
`store.TASK_FIELDS` is the single field list.

State: `{version:1, id, name, goal, project, created_at, updated_at, revision,
status:'ready', tasks:[...], events:[], notes:[], control:{stop_requested:false,
pause_requested:false, guidance:[]}, controller:null}`. Tasks retain their
manifest fields and add `status:'pending', attempts:[], result:null`.
A run created with `--collab` adds `coordination` (see `create`); export copies
only manifest fields and `TASK_FIELDS`, and import calls `create` without it, so
the opt-in is never exported or imported.
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
already-succeeded tasks. Its `run_started` event lists
`budgets_beyond_run_timeout(state, timeout_seconds)`: pending tasks whose Hermes
`run_budget_seconds` is not shorter than the invocation's limit, which would end
the run before Hermes could wrap up; the CLI also warns about them before running
when the limit is positive. Own a per-run controller lock, never steal a live run.
The default is serial. `scheduler.schedule(store, run_id, deadline, interrupted,
max_launches, jobs) -> (status, reason)` runs inside that lock and signal scope.
It honors checkout compatibility, joins dependencies, drains workers on pause
or launch limit, and cancels active peers on failure without inventing an OS
signal. A busy checkout pauses with `workspace_busy`, without an attempt or retry.
Stopping terminates owned active subprocess groups. Guidance applies to the next
agent task that starts and is explicitly not in-flight native steering.
Agent prompts include the workflow goal only when it differs from the task
prompt. In a run with `coordination.collab`, every agent attempt's prompt then
carries Workbench's board note (`_board_note`, named for the attempt ID only, with
no run ID, path, journal or credential location) directly after the prompt and
goal, before predecessor records and results, continuity notes and guidance; a
declared parent adds its lineage-only clause. A successor's predecessor block adds Workbench's recorded worktree
evidence as a separate Workbench-labelled block before the untrusted predecessor
summary, or says that evidence is unavailable. `native_sandbox(task)` returns the
Codex sandbox flag value or None. `processes.native_environment()` omits the
launching host session's identity and messaging variables
(`HOST_SESSION_ENV`, which includes Codex's `CODEX_CI`, `CODEX_SESSION_ID`,
`CODEX_THREAD_ID`, `CODEX_VERSION`, `CODEX_TOOL_CALL_ID`,
`CODEX_INTERNAL_ORIGINATOR_OVERRIDE` and `CODEX_ESCALATE_SOCKET`, Hermes's `TERMINAL_CWD`,
plus every `CLAUDE_CODE_MESSAGING_` name and every `AGENTOOL_COLLAB_`/`AGENTTOOL_COLLAB_` name
except the pass-through locations `AGENTOOL_COLLAB_DB` and `AGENTOOL_COLLAB_ANCHOR_LEDGER`
(`HOST_SESSION_PREFIXES`, `HOST_SESSION_PASS_THROUGH`); `CODEX_HOME`,
credentials, `CODEX_SANDBOX*` and Codex's network proxy variables stay) for tasks, plugin
evaluations and mod checks; a task's launch record lists the removed names. The collab
strip matters for Claude and Astra children, whose MCP servers may inherit the
environment; Hermes gives MCP servers a safe baseline plus their own `env` block, and
Codex a whitelist.
Launch inspection passes `update_check=False`, so Hermes's update-checking
`--version` is not probed during runs. Every task launch (agent or command) sets
`AGENT_WORKBENCH_ATTEMPT=<attempt_id>` and `AGENT_WORKBENCH_STATE_ROOT=<store root>`,
overwriting inherited values; see the CLI's `inside_workbench_child` refusal.
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

Native CLI commands use prompt stdin and no bypass flags. The only sandbox flag
is a Codex task's explicit `sandbox` (`danger-full-access` turns Codex's sandbox
off) or the `read-only` implied by a read-only workspace. Codex exec JSON;
Claude print stream-json. Every attempt records selected executable/version,
capability/configuration evidence in private `launch.json`; launch uses the
selected absolute entry point. Astra wraps Claude. Do not force model/effort unless
manifest requests them. No token-budget enforcement claim: workflows bound elapsed
time and task launches. Agent task success requires native terminal success,
not just exit code zero with a JSON error. Command success is exit code zero.

## Checkout ownership (`workspaces.py`)

- `compatible(task, active_tasks)` permits read-only/read-only overlap and
  independent worktrees; shared writers serialize with shared readers/writers.
- `lease(store, project, mode, *, run_id=None, task_id=None)` yields checkout
  ownership with `attach_attempt`, `spawn` and `release_process` operations.
  Workflow claims persist beyond controller death. A private startup gate waits
  for durable process identity before executing the native command.
  Coordination covers controllers sharing a state root, not unrelated processes.
- `reconcile_claims(store, run_id, *, terminate=False)` runs under that run's
  controller lock. Explicit recovery may stop matching recorded process groups;
  unverified surviving groups keep the checkout unavailable. No work is replayed.
- `prepare(store, run_id, task, attempt_id, project) -> dict` returns attempt
  workspace metadata. Worktrees are detached snapshots of the committed HEAD or
  of `base_ref` (resolved with `rev-parse --verify --end-of-options REF^{commit}`);
  the relative project path is preserved and uncommitted files are not copied.
  Planned ownership is saved before checkout creation. Preparation failures
  retain the recorded location and a fixed failure code for inspection.
- `record_worktree_evidence(store, run_id, task_id, attempt_id, workspace, *, settled=True) -> dict`
  runs after the runner has terminated the attempt's process group and records
  `final_head`, `branch`, `commits_ahead`/`commits_behind`, `dirty`, entry counts
  and tracked diff counts against the base. It stores no file names or contents
  and never raises for Git, ownership or helper failures: those, and
  `settled=False` (`processes_still_running`), record `evidence_status:
  "unavailable"` with an `evidence_failure` code, and the task outcome stands.
  Status calls pass `--untracked-files=normal` so local Git configuration cannot
  hide untracked files.
- `workspace_view(state, *, observe=False) -> dict` lists recorded paths, ownership and retention.
  With `observe=True` (the `workspaces` command) each owned worktree also gets
  `observe_workspace(workspace)`: live `path_exists` (the recorded path is a
  directory) and `dirty`, the recorded values as
  `recorded_path_exists`/`recorded_dirty`, and an `observed_at` time on the
  view. `dirty` is None, with an `observation_failure` code, unless the path is
  the recorded checkout: `checkout_missing`, `not_checkout_root`,
  `ownership_path_mismatch` (the root or its parent is a symbolic link) or
  `git_ownership_mismatch` (its Git common directory is not the source
  repository's, the same check `remove_clean` makes). Read-only; no lease is taken.
- `remove_clean(store, run_id, task_id, attempt_id) -> dict` takes the controller
  lock and removes only an inactive, clean, owned worktree whose HEAD is its
  recorded base commit or is contained in a local remote-tracking ref, while
  also holding the checkout's exclusive lease, including against other runs
  using a retained worktree. It records `removal_basis` (`unchanged_base` or
  `head_on_remote_ref`) and `removed_head`. Automatic cleanup, if any is ever
  built, may remove only a clean tree whose HEAD a remote-tracking ref
  contains. It refuses dirty trees, commits
  no remote-tracking ref contains, ignored files,
  incomplete preparation and ownership mismatches. Git helpers use bounded output, owned
  process groups and `GIT_OPTIONAL_LOCKS=0`.

Read-only is a declaration, not OS enforcement, except that Codex receives its
native read-only sandbox. Worktrees are retained by default.
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
NDJSON, Codex JSON and native Hermes JSONL. It allowlists session/tool/task pointers, fixed event/status
values and bounded numeric usage. It excludes text, prompts, tool inputs, thinking,
raw errors, provider configuration and arbitrary paths/account values.

Hermes process tasks use `chat --query-file - --format stream-json`, never the
top-level oneshot shortcut that changes native approval behavior. They require
one terminal result with a valid session ID, string text, an integer (not boolean)
exit code of zero, no error and process exit zero. Tool errors are not terminal
results. Hermes has no Workbench duplex handshake; stop owns the process group
and grants native detached-tool cleanup time before escalation.

`PublicJournal` batches selected events into atomic state; it keeps the latest
2,000 native events and tracks dropped counts. `flush(force=False)` writes only
after 100 pending events or 0.25 seconds; lifecycle boundaries call the forced
default. `EventTail` reads growing private process-adapter logs incrementally and
keeps one Hermes `native_message` per contiguous run of text deltas. Neither
publishes raw transcript content. A Hermes result whose text carries Hermes's own
"File-mutation verifier: N file edit(s) FAILED" footer (English wording) adds a
result warning; it does not change the native outcome.

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
Writes multiplex stdin readiness with stdout draining and control checks. A
bounded receive backlog is parsed between complete frames so callback responses
cannot interleave with an outgoing JSON frame. Final process-event draining
stops inherited writers first and continues checking run boundaries.

## Collab attribution (`collab.py`)

Read-only; Workbench is not a collab client. No MCP server, host command or lobby
command is started, no session is ended, and no credential file is opened.

- `registrations(harness, *, home=None, env=None)` and `resolve_journal(harness,
  *, home=None, env=None)` read the harness's own registration, never collab's
  default path: `claude`, `astra` and `astra-ultra` read `mcpServers` in
  `$CLAUDE_CONFIG_DIR/.claude.json` (claude only, when set) or `~/.claude.json`;
  `codex` reads `mcp_servers` in `$CODEX_HOME/config.toml` (default `~/.codex`);
  the Hermes root is found as Hermes finds it: `$HERMES_HOME` (default
  `~/.hermes`), or two levels up when `$HERMES_HOME` is a profile directory
  `<root>/profiles/<name>`, which Hermes's profile mode exports to its terminals.
  `hermes` reads such a profile directory's `config.yaml` without reading
  `active_profile`; otherwise it reads the profile named in `<root>/active_profile`
  (UTF-8 with an optional BOM; absent, empty or any case of `default` means the
  root `config.yaml`; other names are lowercased and must be Hermes profile IDs).
  `hermes-astra-ultra` always reads `<root>/profiles/astra-ultra`. Both go through `hermes_mcp_servers`, a
  scanner of the column-0 `mcp_servers:` block only (stopping at the next column-0
  key; `providers` is never read) that keeps `command`, block-list `args` and
  block-mapping `env` and reports flow style, anchors, aliases, tags, merge keys
  and tabs as unparsed. A collab entry's args name `agenttool-collab-mcp` (or, for
  Hermes, its name is `agenttool`); the lobby's name is `agenttool-lobby`. The
  journal is the entries' absolute `env.AGENTOOL_COLLAB_DB`. Entries that disagree,
  a missing location beside a set one, or a Workbench `AGENTOOL_COLLAB_DB` that
  differs (after `os.path.realpath`) give `journal_ambiguous`; no location gives
  `journal_unconfigured`. Other failures: `collab_not_registered`,
  `registration_unreadable`, `registration_unparsed`, `journal_path_not_absolute`,
  `hermes_profile_invalid`. Host configuration files follow a final symbolic link,
  as the hosts do; only the journal refuses links. Results carry private paths;
  callers expose none.
- `read_journal(path, reader=None)` requires `lstat` to show a regular file,
  a 16-byte `O_RDONLY|O_NOFOLLOW` header read of `SQLite format 3\0`
  (`journal_not_sqlite`), then opens `Path(realpath).as_uri() + "?mode=ro"` with
  `uri=True`, `timeout=2` and `PRAGMA query_only=1`, never `immutable=1`. One short
  read transaction checks `PRAGMA application_id` = 1096041292
  (`journal_not_collab`), records `user_version`, requires the columns it selects
  (`coordination_sessions`: id, workspace_id, worktree_id, actor,
  parent_session_id, status, joined_at, last_seen_at, ended_at; `worktrees`: id,
  root_path; `workspaces`: id, repository_key; else `journal_schema_unknown`),
  reports `newer_tables` when collab 0.5.1's `repository_aliases`,
  `repository_discovery` or `session_lease_lapses` exist, runs the reader, and
  closes. It never selects `token_hash` or cursor columns. A read-only open can
  still create empty mode-0600 `-wal`/`-shm` sidecars beside a WAL journal.
- `attribution(state, *, home=None, env=None) -> dict` is computed live for any
  run and writes nothing. Times are parsed with `datetime.fromisoformat` after
  mapping a trailing `Z` to `+00:00` (collab writes milliseconds and `Z`;
  Workbench microseconds, `+00:00`, and no fraction at zero), never compared as
  strings; the window runs from `started_at` floored to milliseconds through
  `finished_at` plus 5 seconds, or now while unfinished. A worktree attempt matches
  sessions whose `worktrees.root_path` equals `os.path.realpath` of its recorded
  owned root and whose `joined_at` is in the window; `label_match` (the actor ends
  with the attempt ID) and `label_exact` (it ends with ` · workbench job
  <attempt_id>`) are reported, not required. Shared and read-only attempts need
  the actor to end with the attempt ID, checked in Python, never SQL `LIKE`.
  Session IDs are validated; at most 20 sessions per attempt (`truncated`,
  `session_count`). Each session reports id, status, parent_session_id,
  parent_matches (against the run's declared parent, null if none), joined_at,
  ended_at, last_seen_at, label_match and label_exact; each attempt `left_active`
  and `excluded` counts (`outside_window`, `time_unparseable`,
  `invalid_session_id`, `root_mismatch`). An attempt is `unavailable` with
  `attempt_id_invalid`, `attempt_time_unparseable`, `workspace_unrecorded`,
  `command_task` or its journal's failure. The run view adds the coordination
  block, each journal's basename, `user_version` and `newer_tables`, a
  `journals_differ` warning and `observed_at`. Actor text never appears.
- `check_parent(harnesses, project, parent_session_id, *, home=None, env=None)`
  returns `matched` or `unavailable`, or refuses with `collab_parent_not_found` or
  `collab_parent_other_repository`. `repository_key(project)` is collab's rule:
  `"local-git:" + sha256(realpath(git rev-parse --path-format=absolute
  --git-common-dir))`; a key in another scheme is not a definite mismatch.
- `agenttool_report(*, home=None, env=None)` is doctor's section; `format_attribution`
  and `format_agenttool` render text without paths, arguments, env values or actor text.

## Doctor API (`doctor.py`)

`diagnose(*, project: Path | None = None, home: Path | None = None,
env: dict | None = None, probe: bool = False) -> dict`
read-only allowlisted metadata: versions, auth freshness, selected config values
with provenance, actual-vs-requested effort, active Astra local proxies. Default
does not make inference or network calls. Optional probe only configured loopback
health endpoints, no model calls. Distinguish inferred, configured and observed.
`format_report(report) -> str`. No token/account-ID/email output. Version and
listener probes pass `stdin=DEVNULL`. The `agenttool` section reports, per
harness, from `collab.py`'s resolver: collab registered, sanitized registration
names, env key names, whether `AGENTOOL_COLLAB_DB` is configured, the journal kind
(`sqlite_collab`, `sqlite_other`, `not_sqlite`, `missing`, `not_regular`,
`unreadable`, or `unresolved` with its failure), `application_id_ok`,
`user_version`, `newer_tables`, each bundle's version from the `package.json`
beside it, `versions_differ` and `journals_equal` (booleans); for Claude and Codex
the lobby registration, whether its `AGENTTOOL_LOBBY_HOME` exists (stat only;
nothing inside is opened) and the source commit from an `INSTALL-RECEIPT.json`
beside the registered package. Never a command, argument, path or env value.

## Launch/configuration evidence (`capabilities.py`)

`inspect_launch(task, project, *, env=None, home=None) -> dict` resolves PATH
candidates and selected absolute entry point. Native CLIs receive version/help
probes only (Codex also exec help, Hermes chat help); command tasks and Astra wrappers are not
executed during discovery. A wrapper's native record is its PATH child candidate,
not proof of what the wrapper launches. Capability evidence distinguishes
advertised interfaces from runtime control acknowledgments and unavailable data.
Hermes version probes may check upstream for updates; the default doctor skips
that probe and only reports executable presence plus read-only binding metadata.
Hermes profile/provider configuration is not resolved by this inspection.

## Subscription bridge (`codex_token.py`)

`borrow_token(binding, check=False, env=None, timeout=12)` is the private
credential interface for Hermes `key_cmd`. The binding pins an account and
Codex home. Normal results contain an access token and bounded cache lifetime;
they must never enter public diagnostics. Only official Codex app-server refresh
updates credentials, under the shared refresh lock. `check=True` returns selected
metadata and never refreshes. CLI `--check` is the human-facing diagnostic path.

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

`plugin_payload(plugin) -> dict` separately fingerprints local code, guides,
fixtures and dot-configuration, including executable mode bits. It excludes
listed VCS/cache/result paths and rejects symlinks; external dependencies and
runtime configuration remain unverified. Code or guide changes invalidate a
saved evaluation plan even when its estimated authoring-token inventory matches.

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

## Owned helper processes (`processes.py`)

`run_owned` captures bounded file-backed output and cleans process groups on
normal exit, timeout and interruption. Main-thread calls temporarily capture
SIGINT/SIGTERM and restore previous handlers after cleanup. Worker calls depend
on their controller's signal handling. Inside a checkout lease, helper launches
also use its durable process gate; no helper silently weakens checkout ownership.

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
JSON plus sibling Markdown containing goal, decisions, questions, task statuses
with their execution settings (`handoff.SUMMARY_SETTINGS`, escaped, with control
and format characters replaced; prompts and argv stay in JSON), explicit evidence and next steps. No credentials, raw traces or hidden reasoning.
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
Selected numeric usage is retained. Claude tool names starting
`mcp__agenttool-collab__collab_` and `mcp__agenttool-lobby__`, and Hermes names
starting `mcp__agenttool__collab_`, become `Collab` and `Lobby` (fixed constants,
no server name or tool suffix); Codex MCP calls stay `MCP`.
`metadata_view(state) -> dict` is the optional
frontend's smaller contract: run/task states, task IDs/harnesses, attempt counts,
queue counts and `coordination` (collab, parent_set and the parent session ID, a
local pointer), without goals, summaries, notes, paths or logs.

## CLI (`cli.py`)

```text
doctor [--project PATH] [--probe] [--json]
explain [--project PATH] [--harness HARNESS] [--json]
create [MANIFEST] [--project PATH]
create --harness codex|claude|astra|astra-ultra|hermes|hermes-astra-ultra (--prompt TEXT | --prompt-file FILE|-)
       [--project PATH] [--name TEXT] [--goal TEXT] [--model MODEL] [--effort EFFORT]
       [--adapter process|duplex] [--workspace shared|read-only|worktree] [--base-ref REF]
       [--sandbox read-only|workspace-write|danger-full-access] [--max-turns N]
       [--skill NAME]... [--run-budget SECONDS] [--timeout-seconds SECONDS]
create ... [--collab | --collab-parent SESSION_ID]
list [--project PATH] [--json]
status RUN [--json] [--metadata]
launch RUN TASK [--attempt ATTEMPT]
workspaces RUN
workspace-remove RUN TASK ATTEMPT
timeline RUN [--limit N] [--json]
collab RUN [--json]
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
both. `--collab` and `--collab-parent SESSION_ID` (which implies `--collab`; the ID
must fullmatch `session_` plus a lowercase UUID) combine with either and set the
run's `coordination`; a parent is checked with `collab.check_parent` before
anything is saved. The single-task form requires an explicit harness and prompt, assigns task
ID `work`, and derives the run name from its first line, capped at 160 characters.
The goal defaults to the prompt up to 20,000 characters, or to the run name for
longer prompts. Explicit `--goal` wins; the task prompt is preserved. All creation
paths use the shared manifest validator and save an
unstarted run. `--project` defaults to the current directory for create/doctor;
import still requires it explicitly. Manifest input is bounded at 2,000,000 bytes;
prompt-file input at 400,000 bytes, followed by normal text validation.

`run`, `retry`, `recover`, `stop`, `pause`, `steer`, `send`, `interrupt`,
`workspace-remove`, `eval-run` and `create --collab-parent` refuse with
`inside_workbench_child` when `AGENT_WORKBENCH_ATTEMPT` and
`AGENT_WORKBENCH_STATE_ROOT` are both set and the latter's realpath is the state
root this invocation would use. This is a speed bump against a Workbench-launched
agent steering its own run, not a security boundary: a process can unset the
variables. Read-only commands, `note` and other creates stay allowed, and a child
using another state root is unaffected. `plan` and `status` include the
`coordination` block; `collab RUN` prints `collab.attribution` (text, or
`--json` through `redacted_view`).

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
