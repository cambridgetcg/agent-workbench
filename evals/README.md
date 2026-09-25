# Workbench utility comparisons

These two native Claude plugin-eval cases test guidance about Workbench controls
and preserving effects after interruption. They ask for commands without
executing them, use only read-only tools, and need no shell scaffolds or real
MCP servers. Every scored grader is deterministic; there are no model judges.
The `skill-used` indicator is excluded from both arms' scores to avoid rewarding
plugin invocation itself. The shared outcome checks run against both arms.

Use Workbench's `eval-plan`, `eval-run`, and `eval-report` to keep the native
evaluator bounded and its reports private. Select `--case recovery --runs 1`
for a small end-to-end check. One pair checks the pipeline and provides an
observation; it cannot establish a reliable improvement. The default three
runs per arm give a less noisy starting point for comparison.

The narrow JSON contract makes grader outcomes reproducible, but remains a
proxy for operational usefulness. These cases do not establish that a real
agent executed a command correctly or that every control failure mode works.
Compare repeated results under the same executable, provider/model, suite and
plugin revision. Keep partial runs, skipped graders, errors and missing arms
out of improvement claims. Native cost is a list-price estimate, and source
character counts are not actual prompt-token usage.
