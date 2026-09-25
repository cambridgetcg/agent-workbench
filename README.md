# Agent Workbench

Shared local tools for Codex and Claude Code: understand the active setup, save a
workflow, control its execution, and hand useful context to the other harness.
Python 3.11 or later on macOS or Linux; no runtime dependencies or service account.

## Four roadmaps

Each roadmap separates the first usable release from deeper native integration:

| Track | First release | Roadmap |
| --- | --- | --- |
| Claude-style workflows in Codex | Saved dependencies, task results, explicit retry, mixed harness tasks | [Workflows](docs/roadmaps/workflows.md) |
| Explicit run controls for Claude | Goal, status, stop, pause, queued guidance, time and launch limits | [Run control](docs/roadmaps/run-control.md) |
| Shared status and doctor | Redacted auth/config metadata, provenance, loopback health probes | [Doctor](docs/roadmaps/doctor.md) |
| Cross-harness continuity | Versioned JSON and Markdown handoffs, decisions, questions and evidence | [Handoffs](docs/roadmaps/handoffs.md) |

## Use

From this checkout, run `python3 scripts/agent-workbench`. A local installation
can expose the same script as `agent-workbench` on PATH. Both plugins share the
same implementation and private state directory.

```sh
agent-workbench doctor
agent-workbench doctor --project /path/to/project --probe --json
```

The default doctor reads selected metadata and checks installed versions. It
does not log in, refresh credentials, make inference requests, or inspect other
processes' environments. `--probe` sends authenticated health requests only to
existing loopback gateways. It never follows a redirect to another host.
Configuration shown is a partial local view: native session flags, managed
policies and another process's environment may change the effective settings.
Configured effort is labeled separately from observed request behavior.
Each version includes its selected executable. If multiple CLI installations
exist, check that path: a shell may select an older installation with different
effort support. The runner preserves the PATH it inherits.

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
agent-workbench create workflow.json --project /path/to/project
# The command prints RUN_ID. Substitute that value below.
agent-workbench status RUN_ID
agent-workbench plan RUN_ID
agent-workbench run RUN_ID --timeout 900 --max-launches 4
```

Supported harnesses: `codex`, `claude`, `astra`, `astra-ultra`, and `command`.
Agent tasks use a `prompt` and optional explicit `model` / `effort`. Astra
requires the separately installed `claude-astra` launcher. A command task uses
an `argv` array, for example `["python3", "-m", "unittest", "discover"]`, and
executes without a shell. Optional `timeout_seconds` bounds an individual task.

Tasks run **serially** in the declared project. A successor receives the goal
and bounded predecessor results. Existing native authentication, hooks and
permissions apply; the runner adds no permission-bypass flags. Starting a run
invokes real agents and uses the corresponding subscription or API allowance.
Explicit manifest model and effort choices override native defaults; otherwise
the defaults remain in force. Process success alone does not establish that an
agent's answer is correct—add a verification task for an observable check.

## Control and recover

In another terminal:

```sh
agent-workbench pause RUN_ID
agent-workbench steer RUN_ID 'Keep the public API unchanged.'
agent-workbench stop RUN_ID
agent-workbench status RUN_ID --json
```

- Pause takes effect between tasks. A later `run` continues pending tasks.
- Stop terminates the active subprocess group owned by this controller. It does
  not undo effects already performed or stop an unrelated native session.
- Guidance is queued for the **next agent task**. V1 does not inject it into an
  already-running native conversation.
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

Start a new session after plugin installation. Ask either agent to use Agent
Workbench for diagnostics, workflow management or a handoff. The plugin is not
selected for ordinary coding requests merely because it is installed.

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
