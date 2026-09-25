# Workflow orchestration: Claude Code → Codex

Status: v0.1 implemented and installed in both harnesses. Offline tests, a
command-only handoff check, and a live Codex → Astra workflow passed on 2026-09-25.

Carry Claude Code's explicit workflow organization into a portable local runner.
Use both harnesses' existing tools, skills and agents; do not claim Codex lacks
delegation or introduce another inference/authentication service.

## v0.1: inspectable task graph

- A version 1 JSON manifest declares a goal, task IDs, dependencies and each
  task's harness. Agent tasks use prompts; command tasks use explicit argv.
- Validate dependencies and cycles before creating a run. Execute ready tasks
  serially in the selected existing project; retain observed outcomes privately.
- `plan RUN` exposes the manifest and recorded notes for review, with recognizable
  credential-like strings redacted in the display and the saved plan unchanged.
- Invoke installed Codex, Claude, Astra or Astra Ultra launchers with their native
  authentication and permission behavior. Model/effort overrides are explicit
  manifest choices, not inferred from task difficulty.
- Reopening a run preserves succeeded tasks. Failed or interrupted work requires
  an explicit retry; a process result is not a claim of review approval.
- Bound elapsed time and launches. These are not token/spend limits.

Acceptance: a synthetic mixed-harness graph executes in dependency order; a
failed dependency blocks descendants; invalid graphs launch nothing; a completed
task does not repeat when the run resumes. Fake native error terminal events
cannot be reported as successful just because the process exited zero.

## Implemented: less setup for ordinary tasks

- `create --harness HARNESS --prompt TEXT` saves one task directly. Prompt files
  and stdin are supported; JSON manifests remain available for task graphs.
- Creation defaults to the current project and still validates and saves an
  unstarted run. Harness selection is explicit, and native model/effort defaults
  remain in force unless overridden.
- Unique run ID prefixes of at least eight hexadecimal characters work across
  CLI commands; project-filtered listing helps locate the intended run. No
  implicit latest-run selection.
- The next agent receives bounded recorded notes as well as predecessor results.
  Notes remain historical context; imported claims do not establish fresh success.

Acceptance: each input form produces a normal pending run, malformed or oversized
input fails before creation, conflicting forms fail clearly, and ambiguous run
prefixes perform no action.

## Next: independent work in parallel

Add a configurable concurrency cap and explicit per-task working directories or
worktrees. Scheduling must account for tasks that edit the same project rather
than infer independence from graph shape alone.

Acceptance: independent read-only tasks overlap, shared-write tasks remain
serialized, cancellation owns only this run's processes, and a join observes
every dependency's recorded outcome before launching its next task.

## Later: reusable workflow packs

Package named manifest templates and review checkpoints in local skills/plugins.
Allow users to inspect the expanded graph before execution. A native workflow UI
or deep in-flight agent integration would be a product/source change, not a
capability supplied by the local plugin.

Acceptance: templates expand deterministically, preserve explicit model choices,
and can be exported as ordinary handoff manifests with no hidden execution code.
