# Connection and execution doctor: both harnesses

Status: v0.1 implemented and installed in both harnesses. Offline tests, a
command-only handoff check, and a live Codex → Astra workflow passed on 2026-09-25.

Explain which layer controls a run: installed CLI, authentication freshness,
selected settings, local adapter and actual model request. The recent Astra
integration showed why client effort, provider reasoning and orchestration must
be reported separately.

## v0.1: read-only, selected metadata

- Report installed versions, available launchers and selected configuration
  values with source/provenance. Do not dump configuration files or environment.
- Inspect authentication freshness locally without displaying tokens, account
  identifiers or email. Freshness is not proof that a remote login will succeed.
- Identify active Astra loopback proxies and distinguish requested settings,
  configured translation and observed results. Preserve uncertainty explicitly.
- Default diagnosis makes no inference or network calls. An explicit `--probe`
  may check configured loopback health endpoints only, never send a model prompt.
- Explain the Astra Ultra preset as native Ultracode orchestration plus the
  provider's supported per-request effort; do not equate the UI label with proof
  of the serialized upstream request.

Acceptance: fixture homes cover absent/stale/fresh auth and conflicting settings;
reports contain no sentinel credentials or account identifiers; default diagnosis
opens no network connection; opt-in probes cannot follow a redirect off loopback.
Unknown or unreadable state is reported rather than silently treated as healthy.

## Next: capture-backed capability checks

Add a credential-free local request capture for installed CLI versions, with
explicit consent for any later inference smoke. Compare serialized request
effort and workflow capability against the intended execution profile.

Acceptance: a changed client flag or adapter mapping yields a concrete mismatch,
and the report labels static configuration, local capture and provider response
as different evidence. Captures exclude raw prompts and credentials from reports.

## Later: supported capability negotiation

Prefer stable SDK/server capability metadata where available. A shared native
status display or consistent effort negotiation across products needs upstream
product/API support; local plugins can only explain observable behavior.

Acceptance: versioned capability metadata replaces a documented inference, with
a clear fallback when an installed version cannot expose it.
