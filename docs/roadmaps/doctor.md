# Connection and execution evidence

Status: v0.3.0 implements launch profiles, partial configuration explanations,
plugin inventory and bounded native evaluations. No global settings or native
runtime upgrade is required for these interfaces.

## Implemented: explain the actual launch

- `doctor` retains local version/auth-freshness metadata and optional loopback
  health probes. Default diagnosis makes no inference request; a health response
  is not proof that a model call or login will succeed.
- `explain` selects local settings with contributing sources, hook/permission
  counts, model-related environment candidates, instruction-file presence and
  plugin registration/cache/source versions. Instruction bodies and auth files
  are not read. Scalar candidates are not complete native effective settings.
- `explain --harness HARNESS` records PATH candidates, selected executable,
  version and advertised interfaces. Native help evidence is kept separate from
  protocol acknowledgments. Missing, unreadable and unverified data stay explicit.
- Each attempt stores private `launch.json` before execution and uses the selected
  absolute entry point. `launch RUN TASK [--attempt ID]` inspects that evidence.
- Astra wrappers are not executed during discovery. Their Claude PATH candidate
  is reported separately because wrapper forwarding, reserved settings and proxy
  lifetime cannot be established from native help alone.
- Plugin drift checks compare manifest versions. Equal versions do not prove
  that source and cache contain identical bytes.

Acceptance evidence: fixture homes cover PATH drift, wrapper boundaries, malformed
configuration, instruction/auth non-reading, old-version AGENTS.md limits, plugin
version drift and selected-metadata privacy. Integration tests verify private
launch evidence and pre-prompt initialization failure without sending task input.

## Implemented: measure a plugin against its baseline

`plugin-cost` counts bounded authoring files and estimates tokens as characters
per four. It does not measure loaded context, dynamic tool schemas or account
charges. `eval-plan` inspects a conventional `evals/` suite and evaluator help,
then pins executable metadata and plugin/suite hashes without model calls.

Explicit `eval-run` uses the native with/without evaluator, bounded repetitions,
case count, process wall time and native list-price cost ceiling. Report behavior
must be verified as local-only. Tool grants, directory trust and model overrides
are explicit plan inputs. A native cost ceiling may overrun by one agent run.

`eval-report` retains numeric grader outcomes and missing-measurement uncertainty.
Errors, partial results, unbalanced arms and skipped graders cannot support a
comparison. Raw prompts/traces remain private, and a score does not prove
correctness or causation. Nothing removes a plugin automatically.

Acceptance evidence: synthetic native evaluators cover local-only behavior,
changed-input refusal, bounded cases, missing/malformed reports, incomplete arms,
private files and report redaction. Live evaluation is separate from offline CI.

## Next: stronger native evidence

Prefer an upstream effective-configuration/context API and explicit capability
negotiation when available. Local discovery cannot infer managed policy, trust,
all setting merges, what a wrapper actually forwards, or what reached a provider.
A future request capture must keep requested, observed serialized and provider
accepted values separate without exposing credentials, prompts or reasoning.

Acceptance: a new observation replaces a named uncertainty, and unsupported
versions retain an honest fallback. No version check alone activates early-access
features or changes authentication and permissions.
