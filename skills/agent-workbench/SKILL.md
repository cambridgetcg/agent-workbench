---
name: agent-workbench
description: Diagnose local Codex, Claude Code or Astra adapter configuration; save and run dependency-based workflows; control managed runs; or transfer an explicit work handoff between harnesses. Use for these operations, not ordinary coding tasks.
---

Use the shared `agent-workbench` CLI. If it is not on PATH, execute the bundled
[script](../../scripts/agent-workbench) with Python 3.11+ using its resolved path.
For command syntax and a workflow example, read [the guide](../../README.md).

- Start connection investigations with `doctor --json`. It reports selected local
  settings and credential freshness, not full native effective configuration.
  `--probe` checks only existing loopback gateways; it does not call a model.
  Never read raw auth files or request logs merely to elaborate a doctor report.
- For one agent task, use `create --harness HARNESS --prompt TEXT` (or
  `--prompt-file FILE`, with `-` for stdin). For dependencies, use `create FILE`
  or pipe JSON to `create -`. Project defaults to the current directory;
  `--project PATH` selects another. `create` saves an unstarted run;
  `run` invokes real native agents or argv commands in the declared project.
  Preserve the user's chosen harness, model, effort and task scope. Creation or
  import alone is not a request to execute. The runner adds no bypass flags.
  Use `plan RUN_ID` to inspect task definitions, notes and imported provenance.
  Run references accept unique prefixes of at least eight characters;
  `list --project PATH` finds runs for a project.
- V1 runs tasks serially. Time and launch limits bound one controller invocation;
  they are not token or spend caps. `pause` takes effect between tasks, `stop`
  terminates the owned active process group, and `steer` queues guidance for the
  next agent task. Do not report queued guidance as delivered to a running turn.
- `status` explains blockers, results and continuation actions; `timeline RUN_ID`
  shows recent events and attempt outcomes. Native session IDs are local pointers,
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

Durable state is shared locally between both harnesses. Native conversation
histories and hidden reasoning are not transferred. Rest and stopped runs need
no justification; preserve a practical handoff when useful.
