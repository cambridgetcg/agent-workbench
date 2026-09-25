# Optional Claude Code status frontend

This original Workbench plugin implements `/workbench-status RUN_ID` using
Claude Code's early-access function hooks. It is separate from the ordinary
Agent Workbench plugin and is not enabled by installing that plugin.

The command runs the Workbench CLI's read-only status and timeline views with
fixed argument arrays and five-second timeouts. It projects those responses to
run state, task numbers, harnesses, attempt counts, queued-guidance counts and
selected event kinds. Task prompts, names, summaries, notes, process output and
credentials are not displayed or added to model context. The terminal pane
offers Refresh and Close. Other surfaces receive the text summary.

It reads a recorded snapshot. Neither the pane nor a successful CLI process
proves that a task is correct or that a recorded process is alive. It performs
no automatic polling, task execution, retry, interruption, permission change,
network request or session-message inspection.

## Try it in a compatible process

The native host tests and strict validation passed with Claude Code 2.1.282 and
the explicit, per-process early-access flag below. This is an observed tested
version, not a guarantee that every account or later version exposes the same
contract. The installed 2.1.221 help did not expose `plugin test`.

```sh
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 /path/to/compatible/claude \
  --plugin-dir ./extensions/claude-mod
```

Then type `/workbench-status RUN_ID`, supplying a full run ID or an unambiguous
prefix of at least eight hexadecimal characters. `/workbench-status close`
closes the pane. `agent-workbench` must be on that process's PATH. To pin the
Workbench executable, set this plugin's `options.executable` through Claude's
supported `pluginConfigs` mechanism for the loaded plugin identity. No setting
is changed automatically.

Function hooks execute host effects through Claude's `$` interface, with the
host user's access. The frontend deliberately uses only command registration,
fixed read-only CLI calls, and its own pane. It supplies no enforcement policy;
native hooks may be skipped when they fail. Keep default Claude settings and
runtime updates separate from this optional experiment.

## Verify without model calls

Use an isolated `CLAUDE_CONFIG_DIR` for verification, then run:

```sh
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 /path/to/compatible/claude \
  plugin validate extensions/claude-mod --strict
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 /path/to/compatible/claude \
  plugin test extensions/claude-mod
```

These tests load the real native plugin host with mocked command responses.
They exercise projection, command registration, argument rejection, subprocess
failures, pane rendering, refresh and close. They do not make model requests or
claim to test terminal painting. For TypeScript checks, generate declarations
with the compatible runtime's `/plugin-types`, place them in `.claude/types`,
and run `tsc -p extensions/claude-mod/tsconfig.json`. Generated declarations are
not vendored here.

The interface was researched in Anthropic's
[public function-hook declarations](https://github.com/anthropics/claude-code/blob/e1bb7b065bc29117ab5923f8772fee16a4630a0d/mods/types/claude-code.d.ts).
The module and tests were written for Workbench; no upstream mod implementation
is included.
