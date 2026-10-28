# Agent Workbench

Shared local tools for Codex, Claude Code and Hermes: understand the active setup, save a
workflow, control its execution, compare plugin usefulness, and hand useful
context to another harness. Version 0.5 lets worktree tasks start from a chosen
revision and records what each attempt left behind, enforces Codex's read-only
sandbox for read-only tasks, and passes Hermes turn, skill and budget options;
0.5.1 also keeps a Codex host session's markers out of native launches.
Native Hermes workflows, the ChatGPT subscription bridge, launch evidence, Claude
control receipts, parallel execution and the optional Claude pane remain available.
Python 3.11 or later on macOS or Linux; no runtime dependencies or service account.

## Start a task

From this checkout, run `python3 scripts/agent-workbench`. A local installation
can expose the same script as `agent-workbench` on PATH. All plugin formats share the
same implementation and private state directory.

```sh
agent-workbench create --harness codex \
  --prompt 'Read project instructions. Investigate the failing parser test and report a proposed fix.'
# Create prints a run ID. Use it, or a unique prefix of at least 8 hex characters.
agent-workbench plan RUN_ID
agent-workbench run RUN_ID --timeout 900 --max-launches 1
agent-workbench status RUN_ID
agent-workbench timeline RUN_ID --limit 20
```

`create` saves an **unstarted** run for the current project directory. Use
`--project PATH` to select another existing project. Choose the harness explicitly:
`codex`, `claude`, `astra`, `astra-ultra`, `hermes` or `hermes-astra-ultra`. `--prompt-file FILE` reads a prompt
file; `--prompt-file -` reads bounded UTF-8 text from stdin. Optional `--name`,
`--goal`, `--model`, `--effort`, `--adapter`, `--workspace`, `--base-ref`, `--sandbox`,
`--max-turns`, `--skill` (repeatable), `--run-budget` and `--timeout-seconds` customize this form. The default name
comes from the first prompt line, capped at 160 characters. The goal defaults to
the prompt, or to the run name for prompts longer than 20,000 characters; the task
prompt stays intact and is sent once when the goal repeats it. Native model and
effort defaults apply unless overridden.

Every command that takes `RUN_ID` accepts a full ID or an unambiguous lowercase
hexadecimal prefix of at least eight characters. Ambiguous prefixes fail without
acting. `list --project PATH` finds saved runs for a project; no command silently
selects the latest run.

## Check the setup

```sh
agent-workbench doctor
agent-workbench doctor --project /path/to/project --probe --json
agent-workbench explain --project /path/to/project
agent-workbench explain --harness claude --project /path/to/project
```

The default doctor reads selected metadata and checks installed versions, except
Hermes: its version command can check for updates, so doctor reports presence only. It
does not log in, refresh credentials, make inference requests, or inspect other
processes' environments. `--probe` sends authenticated health requests only to
existing loopback gateways. It never follows a redirect to another host.
Configuration shown is a partial local view: native session flags, managed
policies and another process's environment may change the effective settings.
Configured effort is labeled separately from observed request behavior.
Each version includes its selected executable. If multiple CLI installations
exist, check that path: a shell may select an older installation with different
effort support. Its `agenttool` section checks collab and lobby registrations
offline (see [Coordinate through agenttool](#coordinate-through-agenttool)).

`explain` shows selected configuration sources, scalar candidates, instruction
file presence, hook/permission counts and plugin registration/cache/source
versions. It does not open instruction bodies or authentication files, execute
plugins, resolve every native merge rule, or claim a file was loaded by an agent.
`explain --harness` also resolves the executable and probes native version/help.
Help evidence and observed runtime acknowledgments remain separate. Astra
wrappers are not executed during discovery; their native PATH child is a candidate
whose forwarding and lifecycle must still be verified.

Before each attempt, the runner records a private `launch.json` and uses the
selected absolute entry point. This explains PATH drift between shells without
changing the user's PATH or settings. Inspect recorded evidence with:

```sh
agent-workbench launch RUN_ID TASK_ID
agent-workbench launch RUN_ID TASK_ID --attempt ATTEMPT_ID
```

Launch/configuration views include selected local paths and settings. Use
`status RUN_ID --metadata` for the smaller interface without project paths,
goals, prompts, summaries or notes.

## Save a workflow

Create a workflow JSON file like this, changing the goal and prompts to the work
you actually want performed:

```json
{
  "version": 1,
  "name": "Investigate a failing test",
  "goal": "Explain the failing parser test and independently check the proposed fix.",
  "tasks": [
    {
      "id": "investigate",
      "harness": "codex",
      "prompt": "Read project instructions. Investigate the parser test failure and propose a minimal fix. Do not edit files."
    },
    {
      "id": "review",
      "harness": "astra",
      "depends_on": ["investigate"],
      "prompt": "Check the predecessor's proposed fix against the source and tests. Report agreement, disagreement and evidence. Do not edit files."
    }
  ]
}
```

```sh
agent-workbench create workflow.json
# Or: agent-workbench create - < workflow.json
agent-workbench plan RUN_ID
agent-workbench run RUN_ID --timeout 900 --max-launches 4
```

Manifest files and stdin use the same validation and 2,000,000-byte input limit.
The manifest form cannot be combined with single-task prompt options. Both forms
create a saved run without launching anything.

Supported harnesses: `codex`, `claude`, `astra`, `astra-ultra`, `hermes`,
`hermes-astra-ultra`, and `command`.
Agent tasks use a `prompt` and optional explicit `model` / `effort`. Astra
requires the separately installed `claude-astra` launcher. A command task uses
an `argv` array, for example `["python3", "-m", "unittest", "discover"]`, and
executes without a shell. Optional `timeout_seconds` bounds an individual task.

Tasks run serially by default. A successor receives the goal
and bounded predecessor results. Agent tasks also receive recent recorded notes
as historical context. Existing native authentication, hooks and
permissions apply; the runner adds no permission-bypass flags. The one sandbox
flag it passes is a Codex task's: an explicit `sandbox` (`read-only`,
`workspace-write` or `danger-full-access`, which turns Codex's sandbox off), or
`read-only` implied by a read-only workspace. Without either, Codex's own
configuration applies. Hermes tasks may set `max_turns`, `skills` (a list to
preload) and `run_budget_seconds`, a graceful wrap-up budget that must be shorter
than the task's `timeout_seconds`; `run --timeout` is also a hard stop, so give
it more time than both (`run` warns when it cannot reach a budget). Native
launches (tasks, plugin evaluations and mod checks) do not inherit the launching
host session's identity and messaging variables: `CLAUDECODE`, `CLAUDE_PID`,
`CLAUDE_EFFORT`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION`,
`CLAUDE_CODE_SESSION_ATTENDED`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_EXECPATH`,
every `CLAUDE_CODE_MESSAGING_` variable, Codex's `CODEX_CI`,
`CODEX_SESSION_ID`, `CODEX_THREAD_ID`, `CODEX_VERSION`, `CODEX_TOOL_CALL_ID`,
`CODEX_INTERNAL_ORIGINATOR_OVERRIDE` and `CODEX_ESCALATE_SOCKET`, Hermes's
`TERMINAL_CWD`, and every agenttool collab session-binding variable
(`AGENTOOL_COLLAB_` and `AGENTTOOL_COLLAB_` names other than the journal and
anchor-ledger locations `AGENTOOL_COLLAB_DB` and `AGENTOOL_COLLAB_ANCHOR_LEDGER`,
which pass through). Other environment variables, including API
keys, are inherited; a task's launch record lists the removed names. Starting a run
invokes real agents and uses the corresponding subscription or API allowance.
Explicit manifest model and effort choices override native defaults; otherwise
the defaults remain in force. Process success alone does not establish that an
agent's answer is correct—add a verification task for an observable check.

## Run independent tasks in parallel

Use `run RUN_ID --jobs 2` through `--jobs 16` to allow bounded overlap. Every
dependency must succeed before its successor starts. Set each task's `workspace`
in its manifest, or use `create --workspace MODE` for a single task:

| Workspace | Ownership and overlap |
| --- | --- |
| `shared` (default) | Uses the project checkout; serializes against other shared readers and writers |
| `read-only` | Uses the project checkout; may overlap other declared readers. Codex runs in its read-only sandbox |
| `worktree` | Creates a separate detached Git worktree for each attempt; may overlap either mode |

For Claude, Astra, Hermes and command tasks, read-only is a declaration, **not a
sandbox**. Codex tasks declared read-only launch with `--sandbox read-only`, and
a writable `sandbox` choice is refused for them. Checkout leases coordinate only
Workbench controllers using the same state root; they do not lock out editors
or unrelated processes. Lease contention pauses the run with `workspace_busy`
after existing work drains, without creating an attempt for the blocked task.
Checkout claims survive a controller crash. Use `list --project PATH` to find
the owning run, inspect its state, and explicitly `recover` it before reuse.
Recovery checks recorded process identities; unresolved survivors keep the
checkout unavailable, and recovery does not retry their work.

Worktrees start from the project's committed HEAD, or from the task's
`base_ref` (for example `origin/main`; fetch it first). They preserve the
project's path relative to the repository root. Uncommitted and untracked
source files are not copied. Every attempt, including a retry, gets a fresh
worktree; the ref is resolved again each time and recorded as `base_commit`, so
use a commit SHA as `base_ref` when retries must start identically. To continue
an attempt's commits, branch at its recorded final HEAD and use that branch as
the `base_ref` of a new run.

After each attempt, once its processes have ended, Workbench records what the
worktree holds: final HEAD, branch, commits ahead of and behind the base, whether
it is dirty, modified and untracked entry counts, and file, insertion and deletion
counts for tracked files against the base (untracked files are counted, not
diffed). No file names or contents enter run state. If Git or checkout ownership
fails, the evidence is marked unavailable and the task's outcome stands. A
dependent agent task's prompt carries this record as a separate block, labelled
as written by Workbench rather than the predecessor, with the worktree path.
Dependency joins neither merge changes nor copy worktrees. Choose an explicit
integration task when results need to be combined.

Codex's `workspace-write` sandbox lets a worktree task edit files, but Git refuses
to stage or commit there: a linked worktree keeps its Git metadata in the source
repository's `.git`, outside the sandbox's writable roots. Workbench does not add
that directory, because doing so would also expose the repository's hooks and
configuration. The recorded evidence shows the uncommitted result for an explicit
integration step.

```sh
agent-workbench workspaces RUN_ID
agent-workbench workspace-remove RUN_ID TASK_ID ATTEMPT_ID
```

`workspaces` measures each owned worktree when it runs: `path_exists` and
`dirty` say what is on disk now, and `recorded_path_exists` and
`recorded_dirty` keep what preparation and the attempt's end recorded. A tree
removed outside Workbench shows `path_exists: false` and `dirty: null`.
`dirty` is measured only for the recorded checkout itself: a directory,
symbolic link or other repository found at the recorded path shows
`dirty: null` with an `observation_failure` code (`not_checkout_root`,
`ownership_path_mismatch` or `git_ownership_mismatch`).

Removal is explicit and accepts only the recorded Workbench-owned worktree when
execution has ended, the tree is clean, and HEAD either still matches its base
commit or is contained in a remote-tracking ref (pushed, as of the last fetch;
no remote is contacted). Changed files, commits no remote-tracking ref contains,
or ignored files (which Git would otherwise delete silently) cause refusal;
nothing is force-removed or merged. The run records `removal_basis`
(`unchanged_base` or `head_on_remote_ref`). Use this command rather than
`git worktree remove`, which leaves run state claiming the tree still exists.
Workbench never removes a worktree on its own; any automatic cleanup built on
it should accept exactly a clean tree with HEAD on a remote-tracking ref.
Incomplete checkout preparation also remains recorded for inspection and is
refused by this cleanup command.

## Control and recover

In another terminal:

```sh
agent-workbench pause RUN_ID
agent-workbench steer RUN_ID 'Keep the public API unchanged.'
agent-workbench stop RUN_ID
agent-workbench status RUN_ID --json
agent-workbench status RUN_ID --metadata
agent-workbench timeline RUN_ID --limit 20 --json
```

`status` shows task results, attempt and native session identifiers when recorded,
dependency blockers, launch/workspace metadata, receipts and explicit next
actions. `timeline` includes public native events while a task runs: session,
turn, tool and delegated-task metadata where emitted. It returns the most recent
20 entries by default, with `--limit` accepting 1–200. The journal retains up to
2,000 native events and reports dropped counts. Hermes's streamed text deltas
appear as one message event per contiguous run of text. It excludes prompts, text output,
tool arguments, reasoning and note/guidance bodies. Native session pointers
identify a separate native resume action; they
do not turn `run` into native conversation resumption.

- Pause stops new launches and lets active tasks finish. A later `run` continues pending tasks.
- Stop terminates active subprocess groups owned by this controller. It does
  not undo effects already performed or stop an unrelated native session.
- `steer` queues guidance for the **next agent task**; it does not address a
  running turn. With parallel agents, the next task that starts consumes it.
- A failed task stops new launches and interrupts active peers. Their possibly
  executed effects remain recorded; neither peers nor failed tasks auto-retry.
- Time and launch limits apply to one controller invocation. They are not hard
  token or monetary budgets. Native agents can make several model calls inside
  one launch.
- `run` exits 0 on success, 1 on failure, and 2 for a paused, stopped or
  interrupted result. The status includes the boundary reason, such as
  `launch_limit`; a planned pause is not a failed task.
- Detached descendants that create a separate process session are outside the
  controller's process-group boundary. Do not use this as OS sandboxing.

After a crash, inspect the work before repeating a task:

```sh
agent-workbench recover RUN_ID
agent-workbench retry RUN_ID TASK_ID
agent-workbench run RUN_ID
```

Recovery records stale work as interrupted and does not execute it. Retry is an
explicit reset of one unsuccessful task. Completed tasks are retained. A lock
prevents two controllers from operating on the same run simultaneously.

## Send input to an active Claude-compatible task

Duplex control is explicit and available for `claude`, `astra` and `astra-ultra`:

```sh
agent-workbench create --harness claude --adapter duplex \
  --prompt 'Investigate the parser failure and explain the evidence.'
agent-workbench run RUN_ID --timeout 900
# From another terminal, while task "work" has initialized and input is open:
agent-workbench send RUN_ID work 'Also check empty-array handling.' --request-id followup-1
agent-workbench interrupt RUN_ID work --request-id interrupt-1
```

The runner checks advertised stream-input flags, then requires a correlated
initialize acknowledgment before sending the initial prompt. It preserves the
native system prompt and adds no `--settings` or permission-bypass flags. Astra's
wrapper remains responsible for its connection configuration and proxy lifetime.
Codex retains the process adapter and process-group stop behavior.

Requests are persisted against a particular attempt before dispatch. Statuses
distinguish `queued`, `dispatching`, `sent`, `acknowledged`, `rejected`,
`unacknowledged` and `not_sent`. An echoed input UUID or control response is a
**receipt**, never proof that guidance was applied or that effects have stopped.
Reusing a request ID returns its existing record; conflicting reuse fails.
Crashes and lost receipts never cause automatic resend.

Native permission rules remain authoritative. If an unresolved permission
decision reaches the adapter, it sends deny with interruption and reports
`native_decision_required`; inspect the native session before explicitly resuming
there. Unregistered hook/MCP callbacks fail explicitly. No decision defaults to
allow, and no replacement SDK hooks are installed.

Input closes using observed completion evidence, tracked local agents/workflows,
queued requests and a 0.5-second settling interval. This is a shutdown heuristic:
native inputs can coalesce and background continuations can race with it. There
is no one-result-per-input guarantee. Workbench
still waits for native process exit and a successful terminal result, and applies
the invocation deadline. It does not promise an indefinitely open conversation.
Where emitted, result UUID receipts identify the inputs consumed by a turn.
Older producers get a narrow single-input fallback; overlapping inputs without
completion receipts remain unverified and may wait for native exit, an explicit
stop or a deadline. Selecting a newer executable for a run does not upgrade the
user's default Claude installation.

## Compare a plugin with its baseline

`plugin-cost` inventories local authoring files. Its character-count token
estimate is not tokenizer output, actual loaded context or a per-turn bill.

```sh
agent-workbench plugin-cost /path/to/plugin
agent-workbench eval-plan /path/to/plugin --runs 3 --max-cases 20 \
  --timeout 900 --max-cost-usd 5 --output ./evaluation-plan.json
agent-workbench eval-run ./evaluation-plan.json
agent-workbench eval-report /path/to/native/result.json
```

Planning reads a conventional `evals/` suite and native evaluator help without
inference. The saved plan pins plugin/suite hashes and executable metadata;
`eval-run` refuses changed inputs and invokes the native with/without comparison.
This executes real agents and graders using native authentication. Tools and
plugin-directory trust are granted only through explicit `--allow-tool` and
`--trust-plugin` plan options. Model and judge-model overrides are also explicit.

Plans bound repetitions (1–10 per arm), the entire suite (up to 50 cases), and
wall time (up to 3,600 seconds). The native cost ceiling is a list-price estimate
and may overrun by one agent run; it is not a hard account-charge budget. Only
verified local-report contracts are accepted, and publication is not requested.
Private native output stays under the evaluation's state directory.
Saved plans also fingerprint local plugin code and guides separately from the
context estimate. Changes require a fresh plan. Evaluations and mod checks clean
up their owned process groups on SIGINT or SIGTERM.

Reports keep numeric score, cost and timing measurements without prompts,
traces or grader evidence. Partial runs, errors, missing/unbalanced arms and
skipped paid graders are not eligible for comparison. Scores are grader outcomes,
not proof of correctness or causation. No plugin is removed automatically.

## Optional Claude status pane

The separate [Claude mod](extensions/claude-mod/README.md) adds
`/workbench-status RUN_ID`, with Refresh and Close. It reads metadata snapshots
and selected timeline events; it does not execute tasks, steer agents, alter
permissions or add task content to model context.

The native host tests passed on Claude Code 2.1.282 with the explicit per-process
`CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` flag. This early-access frontend is loaded
separately with `--plugin-dir`; installing the ordinary Workbench plugin does
not enable it or upgrade Claude. Follow its guide for compatibility and isolated
host tests. A recorded status snapshot does not prove process liveness.

```sh
agent-workbench mod-check ./extensions/claude-mod --executable /path/to/compatible/claude
agent-workbench mod-check ./extensions/claude-mod --executable /path/to/compatible/claude \
  --enable-function-hooks --test
```

`mod-check` uses a temporary Claude configuration and child-process-only settings.
It performs strict validation by default; `--test` explicitly runs the selected
plugin's native host tests. `--enable-function-hooks` opts in only for those check
processes. Validation and recognized positive test results are reported separately;
unknown output or an unavailable runtime cannot establish compatibility. No model
request, global installation or persistent settings change is performed by the
check. Host tests execute plugin test code and do not prove live terminal painting.

## Handoff

Record information the next agent should know:

```sh
agent-workbench note RUN_ID --kind decision 'Keep the existing request format.'
agent-workbench note RUN_ID --kind question 'Does the oldest supported client send empty arrays?'
agent-workbench note RUN_ID --kind evidence 'Parser tests passed; see the recorded verification attempt.'
agent-workbench note RUN_ID --kind next 'Check compatibility before changing validation.'
agent-workbench export RUN_ID ./handoff.json
agent-workbench import ./handoff.json --project /path/to/receiving/project
```

Each agent task receives the eight most recent notes within an 8,000-character
context limit, with omitted/truncated content identified. Stored notes remain
unchanged. Notes matching the handoff credential checks are withheld from prompts
with a visible notice. Other notes are redacted, quoted historical data; imported
notes are labeled as
unverified claims. They do not override the task prompt or queued guidance, and
the runner does not open referenced files or mark tasks complete from a note.
Use `steer` for instructions to the next agent task.

Export creates JSON plus a readable Markdown sibling and refuses existing
output files. It includes the plan, selected history and explicit notes; it
omits raw logs, environment variables, auth data, and private reasoning.
Credential-like content is rejected. Review exported text before sharing it.
The digest detects accidental modification; it is not a signature or an
independent verification of the claims.

Import preserves the source artifact and historical claims, then creates a new
local run with pending tasks. It does not launch anything. `plan RUN_ID` displays
the stored task definitions, notes and imported provenance with recognizable
credentials redacted. Inspect the imported plan before explicitly running it in
the receiving project. This transfers
explicit context, not a native conversation or internal model state.

Private state defaults to `~/.local/state/agent-workbench`. Override it with
`--state-dir PATH` or `AGENT_WORKBENCH_STATE_DIR` for isolated tests. Run logs
contain task content and stay local with owner-only permissions.

## Coordinate through agenttool

Workbench children already reach agenttool's collab board and read-only lobby
MCP tools through their own harness registrations (Claude Code's
`~/.claude.json`, Codex's `config.toml`, a Hermes profile's `mcp_servers`).
Workbench adds attribution and observation, not an MCP client:

```sh
agent-workbench create --harness hermes-astra-ultra --workspace worktree --collab \
  --prompt 'Read project instructions. Claim the parser files on the board, then fix the failing test.'
agent-workbench create workflow.json --collab-parent SESSION_ID
agent-workbench collab RUN_ID
agent-workbench collab RUN_ID --json
```

`--collab` gives every agent attempt a short Workbench note, placed directly
after the task prompt and goal and before any predecessor or note data. If the
child uses the board, it starts its own session rooted at the absolute path of
its working directory, appends ` · workbench job ATTEMPT_ID` to its usual actor
label, keeps a session its process already has, never opens another session's
credential file, and ends its session with its leases released. The note
authorizes nothing beyond the task. `--collab-parent SESSION_ID` (implies
`--collab`) also asks children to pass that session as `parent_session_id`:
declared lineage, sharing no lease, cursor or credential. Workbench checks it
read-only at create: where a run harness's journal is readable, the parent must
be there and belong to this project's repository, or the run is refused
(`collab_parent_not_found`, `collab_parent_other_repository`) because collab would
refuse it too; when no journal can be read it is accepted as `parent_check:
unavailable`. Both flags work with a manifest. The opt-in is run-level state,
shown by `plan` and `status`, and never exported or imported.

`collab RUN` reads, live and read-only, the journal that each attempt's harness
registration names (never collab's default path), and lists each attempt's
sessions. A worktree attempt's are those rooted at its owned checkout that joined
between its start and five seconds after its end, labelled or not, so runs from
before this release attribute too. Shared and read-only attempts share a root, so
only sessions whose actor label ends with the attempt ID count. Labels are
self-declared routing hints, not authentication. It works for any run, opted in
or not, saves nothing, and prints no journal path, credential location or actor
text. When a session was left active it says so: Workbench does not end
sessions. The launching host can end one with collab's own host end command
(see collab's README).

`doctor` adds an offline `agenttool` section per harness: whether collab and the
lobby are registered, environment key names (never values), the journal's kind,
`application_id`, `user_version` and whether collab 0.5.1's newer tables are
present, each collab bundle's version and whether they differ, whether harnesses
share one journal, and for the lobby whether its desk home exists (stat only) and
the installed source commit. It starts no MCP server or CLI.

Children never inherit the launching host's collab session binding, and each
carries `AGENT_WORKBENCH_ATTEMPT` and `AGENT_WORKBENCH_STATE_ROOT`. Inside a child
of the same state root, `run`, `retry`, `recover`, `stop`, `pause`, `steer`,
`send`, `interrupt`, `workspace-remove`, `eval-run` and `create --collab-parent`
refuse with `inside_workbench_child`: they are for the person or the host session
that launched the run. This is a speed bump against accidental self-steering,
not a security boundary. Timelines label collab and lobby tool calls `Collab`
and `Lobby`.

Workbench never ends collab sessions, performs lobby acts, runs an agenttool
host, lobby, doorbell, courier, ferry or mission program, or allocates a
pseudo-terminal; tests hold its own code to that. A child's own terminal tools
are outside its reach. The [coordination roadmap](docs/roadmaps/coordination.md)
lists what waits and why.

## Install the plugins

Claude Code can use this checkout directly for a session:

```sh
claude --plugin-dir /path/to/agent-workbench
```

For a persistent Claude installation:

```sh
claude plugin marketplace add /path/to/agent-workbench
claude plugin install agent-workbench@agent-workbench-local
```

The Codex package uses `.codex-plugin/plugin.json`; register this directory in a
local marketplace and install `agent-workbench` from that marketplace. On the
original installation, the Plugin Creator scaffold registers it in the personal
marketplace and `codex plugin add agent-workbench@personal` installs it.

Hermes loads the portable root `plugin.json` and the same skill. See the
[Hermes guide](docs/hermes.md) for the Astra Ultra subscription profile, native
session commands and local plugin installation.

Start a new session after plugin installation. Ask an agent to use Agent
Workbench for diagnostics, workflow management or a handoff. The plugin is not
selected for ordinary coding requests merely because it is installed.

## Roadmaps

The [Hermes source and customization map](docs/research/hermes-agent-2026-09-25.md)
and [module/tool inventory](docs/research/hermes-agent-2026-09-25.inventory.json)
cover the native runtime, extension surfaces and declared tool prerequisites.

The [Claude Code source and customization deep dive](docs/research/claude-code-2026-09-25.md)
maps native configuration, SDK controls and early-access mods to future Workbench
work, with exact source revisions and installed-version boundaries.

| Track | Implemented | Next |
| --- | --- | --- |
| [Workflows](docs/roadmaps/workflows.md) | Mixed harnesses, bounded concurrency, explicit checkout ownership and retained worktrees | Explicit artifact integration and reusable workflow packs |
| [Run control](docs/roadmaps/run-control.md) | Live event metadata, Claude duplex receipts, pause/stop/recovery and optional status pane | Native run-boundary signals and interactive permission bridging |
| [Doctor](docs/roadmaps/doctor.md) | Configuration explanations, executable/capability evidence and plugin evaluation | Stronger native effective-configuration evidence |
| [Handoffs](docs/roadmaps/handoffs.md) | Versioned artifacts, inspectable history and bounded note continuity | Project comparison and evidence reconciliation |
| [Coordination](docs/roadmaps/coordination.md) | Collab board note, live read-only session attribution and doctor registration checks | Collab 0.5.1 session fit, a bound host-side end and execution seats |

## Development

```sh
python3 -m unittest discover -s tests
python3 scripts/agent-workbench --help
```

Tests use isolated state and synthetic harnesses. Live subscription smoke tests
are separate, explicit checks and are not run by the test suite or CI. See
[INTERFACES.md](INTERFACES.md) for module contracts.

Native interfaces used: [Codex exec](https://learn.chatgpt.com/docs/developer-commands#codex-exec),
[Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Claude headless execution](https://code.claude.com/docs/en/headless),
and [Claude plugins](https://code.claude.com/docs/en/plugins).
