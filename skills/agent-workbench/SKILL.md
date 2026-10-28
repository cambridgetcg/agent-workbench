---
name: agent-workbench
description: Diagnose Codex, Claude Code, Hermes or Astra configuration; run and control saved workflows; compare local plugin evaluations; or transfer explicit work handoffs. Use for these operations, not ordinary coding tasks.
---

Use the shared `agent-workbench` CLI. If it is not on PATH, execute the bundled
[script](../../scripts/agent-workbench) with Python 3.11+ using its resolved path.
For command syntax and a workflow example, read [the guide](../../README.md).

- Start connection investigations with `doctor --json`; use `explain` for partial
  settings provenance, instruction-file presence and plugin version drift, or
  `explain --harness HARNESS` for executable/version/help evidence. These are not
  full native effective configuration or proof that instructions were loaded.
  `doctor` and `explain --harness` also name the MCP servers (for example
  AgentTool Collab) a harness child would inherit, without commands or
  environment. `incomplete` or `unparsed` means some configuration could not
  be read: check the warnings rather than assuming there are none.
  `--probe` checks only existing loopback gateways; it does not call a model.
  Never read raw auth files or request logs merely to elaborate a doctor report.
  Each attempt records launch evidence; inspect it with `launch RUN TASK` when
  PATH selection, native version or wrapper behavior matters.
- For one agent task, use `create --harness HARNESS --prompt TEXT` (or
  `--prompt-file FILE`, with `-` for stdin). For dependencies, use `create FILE`
  or pipe JSON to `create -`. Project defaults to the current directory;
  `--project PATH` selects another. `create` saves an unstarted run;
  `run` invokes real native agents or argv commands in the declared project.
  Preserve the user's chosen harness, model, effort and task scope. Creation or
  import alone is not a request to execute. The runner adds no bypass flags; its
  only sandbox flag is a Codex task's `sandbox` or the read-only one it implies.
  Use `plan RUN_ID` to inspect task definitions, notes and imported provenance.
  Run references accept unique prefixes of at least eight characters;
  `list --project PATH` finds runs for a project.
- Hermes tasks use `hermes` or `hermes-astra-ultra` with the native process
  adapter. They require a Hermes version advertising `chat --query-file` and
  `--format stream-json`. The Ultra profile uses native Hermes tools and sends
  Astra's supported `max` wire effort. Hermes does not inherit Claude Ultracode.
  Optional `max_turns`, `skills` and `run_budget_seconds` (below `timeout_seconds`,
  with `run --timeout`, default 1800 seconds, longer still; `run` warns when it
  cannot reach a budget) map to Hermes chat flags; failed native file edits
  surface as result warnings.
  Check the native tool workspace, not just subprocess cwd: Hermes prefers
  `TERMINAL_CWD`, so the runner binds it to the prepared task path, including
  worktrees. Reproduce workspace issues with relative file access rather than
  masking them with an explicit tool workdir. Keep native approval safeguards.
  Use the token helper's `--check` interface for auth diagnostics; its normal
  stdout is a private credential protocol and must never be run in a terminal
  or included in logs, prompts or reports. See the [Hermes integration guide](../../docs/hermes.md).
- Runs are serial by default; `run --jobs N` allows 1–16 workers subject to
  dependencies and checkout ownership. `shared` writers serialize; declared
  `read-only` tasks may overlap. Codex read-only tasks get Codex's read-only
  sandbox; other Codex tasks may choose one with `sandbox`. Other harnesses are
  not sandboxed.
  `worktree` gives each attempt a retained snapshot of committed HEAD, or of an
  explicit `base_ref` such as `origin/main`, without uncommitted source files.
  Workbench records each attempt's final HEAD, branch and change counts and gives
  dependent prompts that record as a separate block. Dependencies do not merge worktrees. Use
  `workspaces RUN` to inspect ownership (it measures `path_exists` and `dirty` now);
  `workspace-remove RUN TASK ATTEMPT` removes only an inactive, clean owned tree
  without ignored files whose HEAD is its base or is on a remote-tracking ref.
  Never use `git worktree remove` on Workbench trees: run state would go stale.
  Checkout leases cover Workbench controllers sharing the same state root.
- Time and launch limits bound one invocation, not tokens or spend. `pause`
  drains active tasks; `stop` terminates owned active process groups. Failure
  interrupts active peers. `steer` queues guidance for the next agent task that
  starts, not a particular currently running task.
- Opt into `--adapter duplex` only for Claude/Astra-compatible tasks needing live
  controls. A runtime handshake must succeed before the prompt is sent. Use
  `send RUN TASK TEXT --request-id ID` or `interrupt RUN TASK --request-id ID`
  only while that attempt is initialized and input remains open. Acknowledged
  input/interrupt means native receipt, never applied guidance or stopped effects.
  Identical request IDs deduplicate; uncertain dispatches never auto-resend.
  Input closes through a bounded drain heuristic, not an indefinite conversation.
  Unresolved native permission decisions are denied with interruption; inspect
  the native session for an explicit decision rather than broadening permissions.
- `status` explains blockers, results and continuation actions; `timeline RUN_ID`
  shows live public event metadata and attempt outcomes without prompt/tool text
  or reasoning. `status --metadata` is the smaller view without goals, paths,
  summaries or notes. Native session IDs are local pointers,
  not portable conversations. Use explicit verification tasks and evidence notes
  for checks that matter. Process success alone is not proof of correctness.
  After a crash, `recover` records interruption; `retry` is an explicit decision
  to repeat possibly executed work.
- Before `export`, record useful decisions, open questions, evidence references
  and next steps with `note`. Exports omit raw logs and account data. Imports
  preserve past results as historical claims and create pending local tasks.
  Review them against the current project before explicitly starting work.
  Agent tasks receive bounded recent notes as context; notes matching the handoff
  credential checks are withheld with a notice. Imported notes remain
  historical claims; inspect `plan` for the complete notes when needed.
- `plugin-cost DIR` estimates authoring size, not loaded context or billed tokens.
  `eval-plan DIR --output FILE` inspects a conventional eval suite without
  inference; `eval-run FILE` explicitly executes the native with/without test.
  Preserve explicit tool/trust/model choices. Native cost ceilings are list-price
  estimates and may overrun by one run. Use `eval-report` for numeric outcomes;
  incomplete arms or graders cannot establish a comparison. Do not auto-remove
  plugins from scores. Read the guide for evaluator bounds and local-report checks.
- The separate [Claude frontend](../../extensions/claude-mod/README.md) is an
  optional early-access status pane, not enabled by ordinary plugin installation.
  `mod-check DIR --executable PATH` validates in isolated configuration; add
  `--enable-function-hooks --test` for explicit native host tests, without a
  global opt-in. Use its compatibility/test instructions when working on that
  frontend; do not change global Claude settings or upgrade the user's runtime
  merely to display Workbench status.

Durable state is shared locally across the harnesses. Native conversation
histories and hidden reasoning are not transferred. Rest and stopped runs need
no justification; preserve a practical handoff when useful.
