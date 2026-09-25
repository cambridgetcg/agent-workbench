# Durable run control: Codex → Claude Code

Status: v0.1 implemented and installed in both harnesses. Offline tests, a
command-only handoff check, and a live Codex → Astra workflow passed on 2026-09-25.

Carry explicit run state and bounded supervision across harnesses. Claude Code
already has resumable sessions; this work adds a shared controller for a sequence
of native CLI tasks, not a replacement session engine.

## v0.1: explicit state and stop points

- Private atomic snapshots track goals, task attempts and observed results.
  A per-run controller lock prevents two controllers from launching the same run.
- Native session identifiers are retained when the harness emits them. Status
  includes the pause/stop reason; exit 2 means the invocation did not finish the
  run, including an intentional launch-limit pause.
- `pause` requests a stop between tasks. `stop` terminates owned active process
  groups and records cancellation; it must not stop an unrelated CLI session.
- `steer` records guidance for the next agent task. It does not claim to inject
  instructions into a currently running native conversation.
- Wall-time and launch-count bounds apply to this controller. Recovery marks
  abandoned attempts interrupted only after proving the controller lock is free.
- Retry is explicit for failed, cancelled or interrupted tasks. Never repeat an
  effect automatically merely because its completion record was lost in a crash.

Acceptance: control survives a controller restart; pause launches no following
task; stop leaves no owned child process; a second controller cannot steal a live
run; a simulated crash records interrupted work without automatically retrying
it. An ordinary success record remains an observed process outcome.

## Implemented: clearer continuation decisions

- `status` shows bounded result summaries, attempt/native session identifiers,
  dependency blockers and explicit next actions.
- `timeline RUN [--limit N] [--json]` presents selected recorded event fields and
  attempt metadata: the latest 20 entries by default, with a limit of 1–200.
  It omits raw logs, note text and guidance text.
- Retry, continuation of pending tasks, next-task guidance and native session
  pointers retain separate meanings. Neither view executes any of them.

Acceptance: the view explains why each task is pending/blocked/interrupted, shows
which attempt produced each result, and never represents next-task guidance as
in-flight steering. A user can locate the native session without exporting logs.

## Next: native control adapters

If a supported SDK exposes cancellation or in-flight steering, add a capability
checked adapter while retaining the subprocess fallback. This integration uses
documented native controls; inventing new product controls requires source/API
changes and is outside a wrapper's guarantees.

Acceptance: each adapter reports supported controls explicitly, confirms observed
delivery where possible, and does not silently emulate stronger semantics.
