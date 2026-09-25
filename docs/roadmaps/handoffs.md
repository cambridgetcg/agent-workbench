# Portable handoffs: both harnesses

Status: v0.1 implemented and installed in both harnesses. Offline tests, a
command-only handoff check, and a live Codex → Astra workflow passed on 2026-09-25.

Move task intent and explicit evidence between harnesses without pretending a
chat transcript, process exit or checksum proves that the work is correct.
Existing native session resumption remains available within each harness.

## v0.1: versioned local artifact

- Export canonical JSON plus a human-readable Markdown sibling: goal, manifest,
  historical task statuses, decisions, questions, evidence and next steps.
- Include a SHA-256 digest of canonical JSON content, excluding the digest field.
  It detects accidental changes; it is not a signature or claim attestation.
- Export selected fields only. Exclude project paths, raw logs, result traces,
  environment, provider configuration and hidden reasoning. Reject recognizable
  credential-like text; accept explicit manual `[REDACTED]` placeholders without
  silently rewriting runnable prompts. Pattern checks are not comprehensive DLP.
- Refuse to overwrite either output. Use private files. Limit JSON to 2,000,000
  bytes, 200 tasks and 500 notes; each note is at most 16,384 characters. Task
  settings still obey the shared manifest validator.
- Import into an explicitly selected existing project as a new ready run. Every
  runnable task starts pending with no attempts/result. Preserve source statuses
  separately as historical claims and retain the exact source JSON privately.
- Import does not execute a subprocess or contact a service. JSON is the import
  format; Markdown is a summary, not another source of executable instructions.
- `plan RUN` shows the imported manifest, notes and provenance so a receiving
  session can inspect the context and executable tasks before explicitly running.

Acceptance: round trips preserve manifests/dependencies and all four note kinds;
digest edits, unsupported schema and path fields fail before a run is created;
historical success never skips work; recognizable credentials fail without being
echoed; original artifacts and existing destination files remain intact.

## Implemented: notes reach the next agent

- Every agent task receives the newest eight recorded notes within an
  8,000-character continuity section. Omission and truncation notices describe
  what was left out; the saved notes remain unchanged.
- All note kinds are quoted as historical data. Imported notes are labeled
  unverified claims, with task instructions and queued guidance taking precedence.
- Notes matching the same credential patterns as handoff validation are withheld
  with a notice rather than automatically forwarded to an agent.
- The runner reads recorded text only. It does not open referenced artifacts,
  promote claims into task results, or resume the source native conversation.

Acceptance: note delivery preserves ordering and bounds, exposes omissions,
redacts recognizable credentials, and leaves both saved notes and imported
completion claims unchanged. Command tasks receive no agent prompt context.

## Next: review and reconciliation

Extend the existing plan view with a comparison against the selected project: repository
identity/revision when explicitly recorded, available harnesses and unresolved
questions. Keep a user decision to adopt prior evidence separate from process
success and automatic task execution.

Acceptance: different projects/revisions are visible, the preview runs no task,
and accepted evidence records who/what supplied the claim without changing prior
attempt records or silently marking fresh tasks complete.

## Later: richer portable references

Add bounded, explicitly selected artifact references and schema migrations.
Native session pointers can aid a local continuation but must not be mistaken for
portable session contents. Automatic deep session transfer needs product/API
support beyond this plugin.

Acceptance: migrations retain the original bytes, unknown versions fail clearly,
and referenced files cannot traverse outside the explicitly selected bundle.
