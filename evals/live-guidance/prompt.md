---
name: live-guidance
description: Distinguish next-task guidance, native live input, interruption, and delivery receipts.
plugins: ["../.."]
runs: 3
max_turns: 6
timeout_seconds: 120
allowed_tools: [Read, Glob, Grep, Skill]
tags: [smoke, controls]
---

I am coordinating Agent Workbench run 12345678. Its active task is `implement`,
using the Claude `duplex` adapter. I want to leave "Use small commits" for the
next agent task, send "Check the parser first" to the active native session,
and have the command ready to interrupt that native turn if needed.

Give me the exact Workbench CLI commands, without executing anything. Consult
the available Workbench guidance if present. Return one JSON object with these
fields, each command as a single string:

- `next_task_command`
- `live_task_command`
- `interrupt_command`
- `receipt_proves_applied`: a boolean answering whether a queued request or
  native acknowledgment proves the agent applied the requested change.
