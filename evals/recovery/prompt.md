---
name: recovery
description: Preserve existing effects after a lost controller or a deliberately stopped task.
plugins: ["../.."]
runs: 3
max_turns: 6
timeout_seconds: 120
allowed_tools: [Read, Glob, Grep, Skill]
tags: [smoke, recovery]
---

Help me assess two Agent Workbench runs. Do not execute any command.

Run 12345678 lost its controller while task `publish` was running. That task
may already have written an external artifact. Run abcdef12 was deliberately
stopped, and its task `build` is cancelled. I want to inspect what happened
before deciding whether either task should execute again.

Consult available Workbench guidance if present. Return one JSON object with:

- `inspect_command`: the CLI command to inspect status of 12345678.
- `recovery_command`: the command to reconcile the lost controller's state.
- `recover_reexecutes`: whether that recovery command repeats the task.
- `stopped_run_auto_retries`: whether a stopped run automatically retries its
  cancelled task when a new session starts.
- `retry_requires_explicit_decision`: whether repeating possibly executed work
  requires a separate, explicit decision after inspecting existing effects.
- `authorized_retry_command`: the command that would reset `publish` after
  that decision, without launching it yet.
