# Durable run control across harnesses

Status: v0.3.0 implements public native event timelines and explicit Claude-
compatible duplex control, alongside existing pause/stop/guidance/recovery.

## Implemented: durable attempts and public events

Private atomic snapshots and a per-run controller lock prevent competing
controllers. Each attempt retains process ownership, native session pointers,
launch evidence, checkout metadata and observed outcomes. `status` explains
blockers and explicit continuation actions; `status --metadata` omits goals,
paths, summaries and notes for a smaller frontend interface.

`timeline` includes live session, turn, tool and delegated-task metadata from
Codex and Claude-compatible streams. It excludes output text, prompts, arguments,
reasoning and raw logs. The journal caps retained native events at 2,000 and
reports omissions. The view shows the latest 20 entries by default, up to 200.
Recorded running state is not independent proof of process liveness.

Pause drains active tasks and stops new launches. Stop terminates this run's
owned process groups; it does not undo effects or control unrelated sessions.
`steer` queues guidance for the next agent task that starts, including under
parallel scheduling. It never claims delivery to an already-running turn.

## Implemented: acknowledged native control

Set `adapter: "duplex"` or `create --adapter duplex` for Claude, Astra or Astra
Ultra. The runner verifies advertised input flags, then requires a correlated
initialize response before sending the prompt. It preserves the native system
prompt and authentication, injects no settings, and adds no bypass flags.
Codex retains its single-input exec adapter and process-group stop control.

`send RUN TASK TEXT --request-id ID` and `interrupt RUN TASK --request-id ID`
address a particular initialized active attempt. Durable records distinguish
queued, dispatching, sent, acknowledged, rejected, unacknowledged and not-sent
states. Identical caller IDs are deduplicated; conflicting reuse fails. Echoed
input UUIDs and interrupt responses prove receipt, never applied instructions
or completed effects. Missing receipts are not automatically resent.

Native permission rules remain authoritative. Unresolved tool decisions receive
deny with interruption and an explicit blocker; continue only through a deliberate
native-session decision. Unknown hook/MCP callbacks fail rather than granting
access. Workbench installs no replacement native hooks or permission policy.

Input closure uses observed turn completion, tracked local agents/workflows,
queued requests and a 0.5-second settling interval. Native input can coalesce and
background continuations can race with this bounded drain heuristic; it is not
a one-result-per-input guarantee. The runner still waits for process exit and
terminal success, subject to its deadline.
Result UUID receipts support coalesced input batches where emitted. Older
producers have only a narrow single-input fallback; ambiguous overlapping inputs
remain unverified and may wait for native exit or the invocation boundary.
An explicit stop can end that wait. Using a separately selected compatible
executable does not upgrade the user's default native installation.

Acceptance evidence: protocol and fake-process tests cover incremental bounded
frames, correlated acknowledgments, refusal before initial input, late interrupt
receipts, lost-echo uncertainty, permission denial, unknown callbacks, task
lifecycle tracking and privacy filters. Process success is still an outcome,
not proof of correctness.

## Implemented: optional native frontend

The separate Claude mod renders `/workbench-status RUN` using metadata snapshots
and selected events, with Refresh and Close. It executes only fixed read-only
CLI calls and adds no task content to model context. Native host tests passed
on Claude Code 2.1.282 with per-process function hooks enabled. Ordinary plugin
installation does not enable the mod or replace the native runtime.
`mod-check` verifies a selected runtime with temporary configuration and bounded
native processes. Strict validation is separate from explicit `--test` host
execution; `--enable-function-hooks` applies only to checking processes. Unknown
output never establishes compatibility, and no model request is made by the check.

## Recovery and next work

Recovery takes the controller lock before marking stale attempts interrupted.
It closes uncertain request queues without resend. Retry remains an explicit
choice to repeat possibly executed work; succeeded tasks are retained.

Next improvements need native support for a definitive run-boundary signal and
an explicit interactive permission bridge. Any bridge must retain native policy,
distinguish pending decisions from granted authority, and keep acknowledgment
separate from execution. Resume/fork semantics and native histories remain
separate from Workbench's task continuation and portable handoff.
