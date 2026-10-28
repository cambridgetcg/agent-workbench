# Coordination with agenttool

Status: v0.6.0 adds attribution and observation of agenttool coordination, built
against the installed collab 0.5.0 and lobby `ac1830bc`. Workbench is not an MCP
client: children reach the collab board and the read-only lobby tools through
their own harness registrations, as before.

## Implemented: opt-in note, live attribution, offline checks

- `create --collab` gives an opted-in run's agent attempts a Workbench note,
  directly after the task prompt and goal, on how to use the board: their own
  session rooted at their working directory, the ` · workbench job ATTEMPT_ID`
  label suffix, no other session's credential file, and an end with leases
  released. `--collab-parent SESSION_ID` adds declared lineage, checked read-only
  at create against the journal the run's harnesses use and collab's own
  repository key. The opt-in is run-level state, never exported or imported.
- `collab RUN` attributes sessions to attempts live and read-only, for any run:
  by owned worktree root and time window in worktree mode (so sessions from
  before 0.6.0 attribute), by the attempt-ID label suffix and window in shared
  and read-only modes. It reports sessions left active and never ends them.
- `doctor` checks collab and lobby registrations per harness offline: journal
  identity (`application_id`, `user_version`, collab 0.5.1's newer tables),
  bundle versions and whether they differ, whether harnesses share one journal,
  and the lobby desk home (stat only) and installed commit.
- Native launches strip the launching host's collab session binding by prefix
  (`AGENTOOL_COLLAB_`, `AGENTTOOL_COLLAB_`; the journal and anchor-ledger
  locations pass through) and Hermes's `TERMINAL_CWD`. Children carry
  `AGENT_WORKBENCH_ATTEMPT` and `AGENT_WORKBENCH_STATE_ROOT`, and run-control
  commands refuse inside a child of the same state root (a speed bump, not a
  security boundary). Timelines label collab and lobby tool calls.
- Guard tests over Workbench's own code: no pseudo-terminal, no terminal
  driver, no agenttool host, lobby, doorbell, courier, ferry or mission argv,
  and native, command and checkout spawns in their own sessions.

Acceptance evidence: a synthetic WAL-mode journal with every selected column and a
filled `token_hash`, fake homes for all three hosts, the placeholder, symlink,
wrong-identity, schema and newer-table cases, the millisecond `Z` against
microsecond `+00:00` boundary, look-alike and wildcard actors, the session cap,
the create-time parent check, the board note's position, the child refusal and
its temporary-state-root exemption, and sentinel strings that never reach doctor
output.

## Waits, and why

- **Collab 0.5.1.** Its README still says "DEVELOPMENT BUILD 0.5.1-dev.0" and
  unreleased. Any main build that opens a journal adds the session-fit tables
  (`repository_aliases`, `repository_discovery`, `session_lease_lapses`) while
  `user_version` stays 3, so every serving MCP process has to upgrade together:
  an install act across hosts, not a Workbench change. `doctor` reports
  `newer_tables` and `versions_differ` so a mixed journal is visible.
- **The doorbell's `wait --addressed`.** Workbench has no timer scheduler; its
  scheduler runs dependency-ready tasks and never wakes on time, so nothing in
  Workbench would drive a doorbell.
- **A host-side session end.** Deferred. Collab's host CLI ends whatever
  session a credential file's contents name, not the one its filename names, so
  a copied file could end another session, such as the parent's. Shared-mode
  matching cannot tell sessions apart: one live root held 23 sessions. As of
  2026-10-28, none of the 71 Workbench sessions in the journal had been left
  open. Draft #491's
  `end --seat-ref` would replace label matching altogether. Conditions to build
  it: an end bound to the session (such as a released seat-ref end, or a check
  of the file's session ID before and of the receipt's after); the host CLI's
  version pinned to every serving collab bundle and the journal's schema;
  automatic selection only in worktree mode, with strict window and last-seen
  rules; and an explicit act by the person or the launching host session, never
  a child, recorded as an agent's decision when an agent takes it.
- **Execution seats** (drafts #488, #489, #491). The ledger CLI writes when it
  opens, so even reading seats would be an effect. `AGENTOOL_COLLAB_SEAT_REF` is
  #491's draft name only; its spelling is still under review, and the prefix
  strip already covers it.
- **Lobby display, netcheck and draft handoffs; leased table talk.** These wait
  for a concrete Workbench need. The lobby CLI's consequential acts need a person
  to type `yes` on the controlling terminal, by design.
- **The lobby MCP for Hermes.** Not added. Yu's 2026-10-27 word named Claude Code
  and Codex; Astra's Hermes terminal has a pty mode, and its deny floor had no
  lobby or pty rules, while room text is peer-authored data from other devices.
  Conditions: Yu names Hermes; Astra's deny floor first covers the lobby CLI's
  consequential subcommands and the pty drivers (`script`, `expect`,
  `unbuffer`), with block and pass cases; then a registration mirroring the
  Claude entry, confirmed by one read-only lobby call in a fresh session.

## Not wired by Workbench

Each of these stays outside Workbench's code, with the reason:

- Lobby consequential acts: they exist only in the lobby CLI, behind a typed
  `yes` that keeps a model from approving its own act.
- Doorbell resume: it would resume sessions on a signal, which Workbench never
  does; native session resume stays an explicit separate act.
- Courier `select`, `run` and `watch`: cross-device delivery is a separately
  configured, opt-in channel with its own disclosure and revocation rules.
- Ferry: moving work between devices is an outward act Workbench has no
  authority for.
- Mission `accept`, `send`, `retry` and `chat`: each is an outward act or an
  automatic retry of a possibly executed effect.
- Collab-zerone `anchor` and `resolve`: chain acts need Yu's word for each one.
- A model running `agenttool-collab-host`: the host end is a host-side
  command for the person or launching session, not for a model.
- Any pty, `expect` or `script` automation: it would answer the terminal
  prompts that guard consequential acts.
