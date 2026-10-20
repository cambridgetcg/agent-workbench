# Workflow orchestration across harnesses

Status: v0.3.0 implements bounded parallel scheduling with declared checkout
ownership and retained per-attempt Git worktrees. Serial execution remains the
default.

## Implemented: inspectable task graphs

Version 1 manifests declare a goal, task IDs, dependencies and explicit harnesses.
Agent tasks use prompts and optional model/effort choices; commands use argv
without a shell. `create --harness ... --prompt ...` saves a single task, while
file/stdin manifests define graphs. Creation and import do not execute anything.
Unique run prefixes and project-filtered listing avoid implicit latest-run
selection. `plan` exposes definitions, notes and imported provenance.

The runner validates dependencies/cycles, preserves succeeded tasks, and requires
explicit retry of failed/cancelled/interrupted work. Success needs a successful
native terminal event as well as process exit for agent tasks. It does not certify
an answer. Elapsed-time and launch limits are not token or spend caps.

Agent prompts receive bounded predecessor summaries and recent historical notes.
Imported claims remain unverified, stored notes remain intact, and credential-like
notes are withheld. Native authentication, hooks and permission controls remain
in force; no model/effort override is inferred from task difficulty.

## Implemented: bounded concurrency and checkout ownership

`run --jobs N` accepts 1–16 workers. A successor starts only after every dependency
succeeds. Ready tasks must also fit their declared checkout ownership:

- `shared` is the default project checkout and serializes with its shared readers
  and writers, regardless of graph independence.
- `read-only` tasks may overlap other readers. This declaration is not a sandbox,
  except that Codex tasks receive Codex's native read-only sandbox.
- `worktree` tasks get independent detached Git worktrees per attempt and may
  overlap either mode. Their source is committed HEAD or the task's `base_ref`;
  uncommitted/untracked files are not copied. The project's repository-relative
  path is preserved. After each attempt Workbench records final HEAD, branch and
  change counts, and dependent prompts receive that record.

Checkout leases coordinate controllers sharing a Workbench state root. They do
not exclude editors or processes outside Workbench. A busy lease creates no
attempt: the run drains active work, pauses with `workspace_busy`, and waits for
an explicit later run. There is no automatic retry loop.

A failure stops new launches and interrupts active peers. Stop/deadline/signal
boundaries clean owned process groups. Pause and launch limits let already-started
work finish. The controller joins all workers before releasing ownership.
A graph join never merges worktrees or copies predecessor files: integration is
an explicit task with its own verification.

`workspaces RUN` lists retained ownership and checkout metadata, with
`path_exists` and `dirty` measured when it runs.
`workspace-remove RUN TASK ATTEMPT` removes only a recorded owned tree after
execution, while clean, free of ignored files, and either still at its recorded
base commit or with HEAD contained in a remote-tracking ref. Dirty work, ignored
files or commits no remote-tracking ref contains are preserved; there is no
automatic cleanup or force removal.

Acceptance evidence: real synthetic commands and temporary Git repositories
verify overlap and worker bounds, serial shared writers, dependency joins,
separate outputs, retained dirty work, owned clean removal, peer cancellation,
multiple descendant groups, signal/deadline cleanup, lease contention and
controller-error cleanup. Crash recovery never infers permission to repeat work.

## Next: explicit integration and reusable packs

Add inspectable artifact-transfer/integration steps that state which checkout and
commit they consume, rather than treating dependency completion as a merge.
Recorded worktree evidence (0.5.0) supplies the commit; consuming it remains a
task-authored effect.
Named manifest templates can package repeatable review/verification work while
preserving an ordinary expanded graph, user model choices and execution bounds.

Acceptance: expanded graphs are deterministic and reviewable; transferred
artifacts have explicit provenance; applying or retrying changes remains an
explicit effect. No hidden template execution or automatic merge is introduced.
