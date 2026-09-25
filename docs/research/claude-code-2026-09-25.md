# Claude Code: features, source, and customization

Research date: 2026-09-25. Purpose: identify useful Claude Code mechanisms for
Agent Workbench and the integration friction that Workbench can remove.

## Findings

Claude Code has several distinct extension layers. Settings and Markdown shape
ordinary sessions; plugins package reusable components; the Agent SDK controls
the native process; and newly published TypeScript mods reach deeper into the
engine's prompts, tools, agents and UI. The public repository contains real mod
implementations, but not a complete buildable implementation of the native core.

The immediate Workbench opportunity is reliable orchestration: explain the
selected executable and configuration, preserve native session identity, consume
structured events, and deliver input through an actual control protocol. Deeper
engine customization is a separate experimental track.

This is research, not a runtime migration. No Claude installation, live settings,
permission rules, instructions or plugin configuration were changed for the
inventory. Proposed capabilities below are not implemented Workbench features.

## Evidence and version boundaries

| Evidence | Snapshot | What it establishes |
| --- | --- | --- |
| Installed native CLI | 2.1.221; another shell resolves 2.1.52 | Executable identity, help and selected configuration metadata; not every feature's runtime availability |
| Official documentation | Retrieved 2026-09-25 | Current documented behavior, sometimes newer than the installed CLI |
| Official Claude Code repository | [`e1bb7b065bc29117ab5923f8772fee16a4630a0d`](https://github.com/anthropics/claude-code/tree/e1bb7b065bc29117ab5923f8772fee16a4630a0d) | Published mods, declarations, examples, changelog and repository terms; changelog starts at 2.1.282 |
| Python Agent SDK | [`6c3df983c4d7b51ee6ffbd5443fcfa01a8088b0d`](https://github.com/anthropics/claude-agent-sdk-python/tree/6c3df983c4d7b51ee6ffbd5443fcfa01a8088b0d), package 0.2.159 | Actual Python transport/control implementation; bundled CLI version declaration is 2.1.282 |
| TypeScript Agent SDK repository | [`9e477a178c370991ed87ca65b4c8631d390aea35`](https://github.com/anthropics/claude-agent-sdk-typescript/tree/9e477a178c370991ed87ca65b4c8631d390aea35) | Public examples, release information and supporting files; not the main TypeScript implementation |
| Official TypeScript SDK distribution | [`@anthropic-ai/claude-agent-sdk@0.3.282`](https://registry.npmjs.org/@anthropic-ai/claude-agent-sdk/-/claude-agent-sdk-0.3.282.tgz) | Compiled JavaScript implementation and declarations; downloaded and integrity-checked without execution |

Local executable inspection found native Mach-O binaries, not an editable
JavaScript distribution at the checked installation locations. Login/nonlogin
shells select different versions. The adapter also resolves its child from PATH,
so this difference can change an otherwise identical Workbench launch.

Documentation, help, source declarations and successful runtime probes are
different evidence. A help entry does not establish account/provider eligibility;
an API declaration does not establish that a particular binary enables it.
Machine-specific inventory and raw research artifacts remain outside Git.

## What is actually modifiable

| Layer | Editable surface | Useful changes | Boundary |
| --- | --- | --- | --- |
| Session configuration | Settings files, CLI flags, selected environment controls | Models, effort, discovery, tools, permissions, compaction and display | Precedence and version-specific semantics apply |
| Instructions | `CLAUDE.md`, rules, memory, skills and agent definitions | Project knowledge, task recipes, delegation and context selection | Model instructions do not enforce access controls |
| Ordinary extensions | Plugins, classic hooks, MCP/LSP, commands, output styles | Tool integration, lifecycle automation and reusable behavior | Each component has its own discovery, approval and lifecycle rules |
| Host integration | Agent SDK and native NDJSON/control protocol | Streaming, input, interruption, resume and permission callbacks | Runs the native engine; defaults may differ from interactive Claude |
| Engine-facing mods | Published TypeScript function-hook modules | Prompt/tool/agent/UI behavior at supported event sites | Early access, enabled-build dependent, unstable API |
| Native core | Installed executable | Select, update or wrap a supported release | No complete public core source/build was found in the audited official repositories |

The Claude Code repository's [license file](https://github.com/anthropics/claude-code/blob/e1bb7b065bc29117ab5923f8772fee16a4630a0d/LICENSE.md)
states all rights reserved and refers to Anthropic's commercial terms. Publicly
readable source should not be described as an unrestricted open-source fork.
This report proposes integrations, not copying proprietary implementation into
Workbench.

## Published mods: the deepest customization surface found

The official [mods directory](https://github.com/anthropics/claude-code/tree/e1bb7b065bc29117ab5923f8772fee16a4630a0d/mods)
publishes four built-in modules as shipped: `agents-md`, `diff`, `telemetry`, and
`sec-default`. A mod is a plugin with a hooks module, entered through
`register(on, options)`. Handlers receive `($, event, next)` and participate in an
ordered event chain. Source loading and engine-backed tests are documented as
`claude --plugin-dir mods/diff` and `claude plugin test mods/diff`.

These modules load only where function hooks are enabled. The README explicitly
marks the API early access and subject to unannounced changes. Their existence
does not prove that installed 2.1.221 can run a custom mod.

The [generated type contract](https://github.com/anthropics/claude-code/blob/e1bb7b065bc29117ab5923f8772fee16a4630a0d/mods/types/claude-code.d.ts)
is stamped 2.1.277. It exposes prompt context/sections/attachments, tool calls and
descriptions, agent offering/spawning, model and effort controls, compaction,
commands, configuration and UI events. Modules execute in a separate JavaScript
environment without Node or DOM; host effects use `$` APIs. `/plugin-types`
generates declarations, and plugin validation inspects the module's interface.

[Failure behavior](https://github.com/anthropics/claude-code/blob/e1bb7b065bc29117ab5923f8772fee16a4630a0d/mods/types/claude-code.d.ts#L3223)
matters: an invalid, throwing or over-budget hook is skipped;
lower hooks/core continue, or an already-called `next` result is retained.
Optional catch handling has a bounded grace period. Consequently, a failed
custom hook must not be assumed to stop an operation. Workbench should treat
mods first as observability/context/UI integration, with separately verified
contracts for anything intended to enforce policy.

### Instruction loading: `agents-md`

The [implementation and README](https://github.com/anthropics/claude-code/tree/e1bb7b065bc29117ab5923f8772fee16a4630a0d/mods/agents-md)
support four modes: CLAUDE only, CLAUDE with AGENTS fallback, both, and managed
instructions only. The default fallback depends on whether the project already
has a qualifying ancestor/current CLAUDE instruction file. A local CLAUDE file
can therefore suppress AGENTS loading; this is not a simple filename preference.

The published configuration for loading both is:

```json
{
  "pluginConfigs": {
    "agents-md@builtin": {
      "options": { "instructionFiles": "claude-md-and-agents-md" }
    }
  }
}
```

This is a newer-runtime example, not a setting applied by this research. The mod
reads its options from user, CLI or managed configuration, not project settings.
Its `managed-only` description notes an incomplete boundary: nested native
CLAUDE attachments are outside the context event and can still arrive. Do not
interpret that option as complete instruction isolation.

### UI and authority examples

The [`diff` mod](https://github.com/anthropics/claude-code/tree/e1bb7b065bc29117ab5923f8772fee16a4630a0d/mods/diff)
implements a live changes pane using commands, tool events, rendering, focus,
scrolling and selected-diff prompt attachments. It demonstrates that supported
mods can change interaction design, beyond adding a slash-command prompt.

[`sec-default`](https://github.com/anthropics/claude-code/tree/e1bb7b065bc29117ab5923f8772fee16a4630a0d/mods/sec-default)
separates organizational hooks, prompt content, managed settings and tool policy
from installed plugins; it supplies no policy of its own.
[`telemetry`](https://github.com/anthropics/claude-code/tree/e1bb7b065bc29117ab5923f8772fee16a4630a0d/mods/telemetry)
provides a typed engine interface for built-in analytics and refuses installed
plugins. These illustrate extension tiers and shared type contracts, not blanket
authority for an installed Workbench plugin.

For a focused implementation walkthrough, start with these entry points:

| Source | What to trace |
| --- | --- |
| [`agents-md/hooks/register.ts`](https://github.com/anthropics/claude-code/blob/e1bb7b065bc29117ab5923f8772fee16a4630a0d/mods/agents-md/hooks/register.ts#L40) | Mode selection, instruction filtering/discovery, deduplication, fork state and nested Read attachments |
| [`diff/hooks/register.ts`](https://github.com/anthropics/claude-code/blob/e1bb7b065bc29117ab5923f8772fee16a4630a0d/mods/diff/hooks/register.ts#L45) | Pane state, repository backend, refresh coordination and UI/tool event registration |
| [`sec-default/hooks/register.ts`](https://github.com/anthropics/claude-code/blob/e1bb7b065bc29117ab5923f8772fee16a4630a0d/mods/sec-default/hooks/register.ts#L17) | Protected settings/prompt paths and managed tool/plugin constraints |
| [`telemetry/hooks/register.ts`](https://github.com/anthropics/claude-code/blob/e1bb7b065bc29117ab5923f8772fee16a4630a0d/mods/telemetry/hooks/register.ts#L23) | Engine-interface construction, event queueing and flush lifecycle |

## Agent SDK: programmable sessions, with consequential defaults

The Python SDK contains real transport and control code. Its
[`ClaudeSDKClient`](https://github.com/anthropics/claude-agent-sdk-python/blob/6c3df983c4d7b51ee6ffbd5443fcfa01a8088b0d/src/claude_agent_sdk/client.py)
supports an ongoing conversation, additional input, interruption and dynamic
model/permission controls. The simpler
[`query()`](https://github.com/anthropics/claude-agent-sdk-python/blob/6c3df983c4d7b51ee6ffbd5443fcfa01a8088b0d/src/claude_agent_sdk/query.py)
interface does not provide the same interactive control lifecycle. The SDK
controls the Claude process; it does not replace the underlying agent engine.

At the pinned Python version, the
[subprocess transport](https://github.com/anthropics/claude-agent-sdk-python/blob/6c3df983c4d7b51ee6ffbd5443fcfa01a8088b0d/src/claude_agent_sdk/_internal/transport/subprocess_cli.py)
has defaults that an adapter must make explicit:

| Option | Observed source behavior | Workbench implication |
| --- | --- | --- |
| `cli_path=None` | Prefers bundled CLI before PATH | Installing the SDK can select a different engine from the user's CLI |
| `system_prompt=None` | Emits an empty system-prompt override | Specify the `claude_code` preset when preserving native coding instructions |
| `setting_sources=None` | Omits the flag, inheriting CLI defaults | Do not claim settings discovery is disabled |
| `setting_sources=[]` | Emits an empty source selection | Different behavior from omission |
| Explicit SDK skills | Default sources become user/project when unspecified | Skills can change discovery defaults |
| SDK settings or sandbox options | Emits composed `--settings` | Collides with wrappers that reserve this option |

The local Astra wrapper reserves `--settings` for its connection configuration
and rejects native background mode because its proxy lifetime follows the
foreground child. Merely setting SDK `cli_path` to that wrapper does not resolve
either contract. A deliberate composition/lifetime interface is needed.

Workbench currently uses Python's standard library only. Adopting the SDK would
introduce a dependency decision. A separate optional bridge process could keep
the core small; implementing a narrow protocol client would instead require
maintaining protocol compatibility. Either choice needs fixture tests and a
bounded live check before becoming the default.

The [Python control implementation](https://github.com/anthropics/claude-agent-sdk-python/blob/6c3df983c4d7b51ee6ffbd5443fcfa01a8088b0d/src/claude_agent_sdk/_internal/query.py)
correlates request IDs with acknowledgments and routes tool-permission, hook and
MCP callbacks. For example, turn interruption uses this control-frame shape:

```json
{"type":"control_request","request_id":"example","request":{"subtype":"interrupt"}}
```

A result frame ends a turn; background work may still need an open input channel
for callbacks. Closing stdin at the first result is therefore not a general
managed-session design. Permission callbacks are also not every-call monitors:
[earlier permission rules can settle the call first](https://github.com/anthropics/claude-agent-sdk-python/blob/6c3df983c4d7b51ee6ffbd5443fcfa01a8088b0d/src/claude_agent_sdk/types.py#L1863).

The inspected TypeScript package contains compiled implementation plus a richer
declared control surface: interrupt queue receipts when supported, dynamic MCP,
task backgrounding, reinitialization and runtime settings. Python/TypeScript
parity must not be assumed. Queue receipts are especially useful: interrupting
the current turn need not cancel pending prompts. See the
[exact published package](https://registry.npmjs.org/@anthropic-ai/claude-agent-sdk/-/claude-agent-sdk-0.3.282.tgz).

License facts differ across layers: the
[Python SDK is MIT](https://github.com/anthropics/claude-agent-sdk-python/blob/6c3df983c4d7b51ee6ffbd5443fcfa01a8088b0d/LICENSE);
the [TypeScript repository](https://github.com/anthropics/claude-agent-sdk-typescript/blob/9e477a178c370991ed87ca65b4c8631d390aea35/LICENSE.md)
and inspected distribution state all rights reserved and reference commercial
terms, as does the Claude Code repository.

## Feature map and configuration details

### Where configuration and extensions live

These are documented authoring locations, not an instruction to edit every
scope. Installed plugin caches are deployment artifacts; change their owning
source and reinstall/update through the native mechanism.

| Surface | Typical authoring location | Main controls |
| --- | --- | --- |
| User settings | `~/.claude/settings.json` | User defaults, enabled plugins, hooks, status line and permission rules |
| Shared project settings | `.claude/settings.json` | Repository configuration, subject to native trust/policy |
| Local project settings | `.claude/settings.local.json` | Machine-specific project overrides |
| Project instructions | `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md` | Shared or local instruction context and imports |
| Scoped rules | `.claude/rules/`, `~/.claude/rules/` | Reusable instructions, optionally path-scoped |
| Skills | `.claude/skills/<name>/SKILL.md`, `~/.claude/skills/` | Invocation, supporting scripts, task-specific context |
| Custom agents | `.claude/agents/`, `~/.claude/agents/` | Agent definitions and delegation contracts |
| Shared MCP servers | `.mcp.json` | Project server definitions |
| User/local MCP servers | `~/.claude.json` | Native-managed user and per-project server state; mixed state, not a safe diagnostic dump |
| Plugin source | `.claude-plugin/plugin.json` plus component directories | Packaged skills, agents, hooks, servers and supported extra components |
| Output styles | `.claude/output-styles/`, `~/.claude/output-styles/` | Response role/style instructions |
| Saved workflows | `.claude/workflows/`, `~/.claude/workflows/`, plugin workflows | Agent orchestration scripts on compatible releases |

The linked settings, memory, skills, subagents, MCP, plugin, style and workflow
references below define the distinct discovery and precedence rules. Managed
policy and explicit launch flags form additional configuration layers.

### Effective settings

Current [settings documentation](https://code.claude.com/docs/en/settings)
describes managed policy, CLI, project-local, project, then user precedence.
Many lists merge; some model lists behave differently, and environment behavior
is setting-specific. `/status` identifies loaded sources, not per-key effective
provenance. Invalid settings may be skipped, particularly awkward in headless
runs. Project/local bypass-mode behavior changes at 2.1.257. For `modelPicker`,
the settings docs give 2.1.242, while the
[pinned changelog](https://github.com/anthropics/claude-code/blob/e1bb7b065bc29117ab5923f8772fee16a4630a0d/CHANGELOG.md#L1720)
announces it in 2.1.243. The exact minimum remains unverified; do not encode either
number as an established runtime gate from this evidence alone.

Workbench should explain a selected value's contributing sources, ignored
entries, applicability and reload requirements. Until version-specific rules
are implemented, call the result a partial explanation rather than a complete
effective configuration.

### Instructions, memory and skills

[Instruction memory](https://code.claude.com/docs/en/memory) includes ancestor
and nested CLAUDE files, path-scoped rules, imports and auto-memory. Auto-memory
is repository-scoped across worktrees; its startup index is bounded, with topic
files loaded as needed. Native AGENTS loading begins at 2.1.277. For an older
runtime, an intentional CLAUDE import is a possible bridge; copying whole homes
or assuming AGENTS is already loaded is not.

The [pinned changelog](https://github.com/anthropics/claude-code/blob/e1bb7b065bc29117ab5923f8772fee16a4630a0d/CHANGELOG.md#L231)
records expanded AGENTS support for gateways and additional providers in
2.1.281. Provider compatibility belongs in the capability check too.

[Skills](https://code.claude.com/docs/en/skills) provide progressive context
loading, invocation controls, scripts/references and optional forked execution.
`allowed-tools` grants permission temporarily; it does not restrict the tool
set, and the grant clears at the next user message. Name precedence differs
from settings precedence. Invalid or misspelled frontmatter may lose its
intended semantics without making the skill visibly disappear. A useful
Workbench validator should show actual discovery, collisions, dynamic command
execution and permission effects.

### Agents, teams and workflows

[Subagents](https://code.claude.com/docs/en/sub-agents) can specify tools, models,
memory, hooks, MCP, context policy and worktrees. Some fields are ignored for
plugin-provided agents. Foreground/background capabilities differ. Later
releases change fork defaults (2.1.232), model precedence (2.1.251), and add
instruction omission (2.1.271). An adapter must distinguish requested settings
from those actually honored.

[Teams](https://code.claude.com/docs/en/agent-teams) add independent sessions,
messaging and shared tasks, currently experimental. Resume does not restore
in-process teammates; nested teams and leadership transfer have limitations.
These are distinct from a durable cross-harness dependency graph.

[Workflows](https://code.claude.com/docs/en/workflows) orchestrate agents through
JavaScript with parallel/pipeline primitives. Intermediate results can remain
outside the main conversation. Saved scripts live at project/user/plugin scope;
the script delegates filesystem and shell effects to agents. The bundled
`/workflow-authoring` reference skill requires 2.1.248; newer concurrency controls
require 2.1.269. Relaunch replays in
launch order: the first changed/failed call invalidates the following suffix,
including previously successful calls. Workbench should retain explicit retry
for potentially executed effects. Ultracode is orchestration plus effort, not
merely a larger reasoning label; native flag support dates to 2.1.203. Automated
SDK/print input no longer gains that behavior just by containing the keyword
since 2.1.210.

### Hooks, tools and plugins

[Classic hooks](https://code.claude.com/docs/en/hooks) support lifecycle
automation, but every event has its own decision contract. Post-action hooks
cannot undo actions. Async hooks cannot block, start separate processes, and
may be killed at headless teardown. Exit-code semantics and timeouts must be
tested for the particular event; installing a hook does not prove enforcement.

[Plugins](https://code.claude.com/docs/en/plugins-reference) package components
with a manifest. A plugin-root CLAUDE file is not automatically project context,
and plugin default settings accept only selected keys. Authoring source,
registration metadata and cached installed bytes are separate states. Local
inspection found a disabled plugin with mismatched registration/source versions.

[MCP](https://code.claude.com/docs/en/mcp) has its own scopes and precedence;
same-name server entries replace rather than field-merge. Project-MCP approval
differs in interactive and headless sessions. `--strict-mcp-config` narrows
discovery, but exclusion/prompt behavior changed in 2.1.246. A launch profile
should name the intended tools and sources instead of inferring that absence of
an approval dialog means absence of discovered tools.

### Permissions and isolation

[Permission rules](https://code.claude.com/docs/en/permissions) evaluate deny
before ask before allow. `dontAsk` denies unresolved requests; it is not a grant
of unrestricted access. Tool selection, persistent permission rules, temporary
skill grants and instruction text have different roles. Explaining why an
operation is allowed, denied or awaiting a decision would remove more friction
than silently broadening permissions.

[Bash sandboxing](https://code.claude.com/docs/en/sandboxing) provides OS-level
filesystem/network restrictions where supported, with explicit exceptions. A
Git worktree prevents some checkout collisions but is not a process/network
sandbox. Workbench should report these properties independently.

### Native session lifecycle and recovery

[Headless execution](https://code.claude.com/docs/en/headless) supplies structured
results, NDJSON events and native session identity. Subagent parent tool IDs
support nested timelines. Forwarding subagent output can include thinking;
Workbench should retain only explicitly allowed public event fields. SIGTERM
stops the process; turn interruption is a different operation. Partial output,
native success and independently verified completion need separate states.

[Agent view](https://code.claude.com/docs/en/agent-view) manages durable native
background sessions through a supervisor. Those sessions are distinct from
in-session workers. Later fixes address worktree cleanup and resumed identity,
so installed help alone cannot establish the latest lifecycle guarantees. An
integration should map one logical task to its native identity and avoid two
controllers independently restarting it.

[Checkpointing](https://code.claude.com/docs/en/checkpointing) does not restore
Bash changes, external effects or most subagent edits. Conversation rewind,
file checkpoints and Git history solve different recovery problems. A status
view must say what recovery covered.

### Other surfaces worth distinguishing

The installed CLI exposes plugin component/token-cost inspection and plugin
evaluations, including baseline comparisons. These are promising ways to
measure whether Workbench instructions improve task outcomes rather than merely
adding context. They were inspected through help; evaluations were not run.

The [CLI reference](https://code.claude.com/docs/en/cli-reference) distinguishes
prompt replacement from append, resume from fork, and discovery controls from
permission controls. Locally, `--bare` changes authentication/discovery behavior;
`--safe-mode` is a customization troubleshooting profile. Neither should become
a generic performance shortcut without a declared launch contract.

[Output styles](https://code.claude.com/docs/en/output-styles) adjust role and
tone; retaining coding instructions is a separate choice. Newer concise styles
and mid-session reload behavior cannot be assumed on older engines.

[/goal](https://code.claude.com/docs/en/goal) uses an evaluator to decide whether
to continue. Its evaluator sees conversation evidence, not an independent
verification environment. Natural-language time/turn conditions are not hard
controller budgets. Workbench should keep concrete verification and explicit
stop/rest states even if native continuation is used.

[Channels](https://code.claude.com/docs/en/channels) allow MCP-delivered incoming
events, currently with preview/authentication restrictions. They are not a
provider-neutral replacement for the SDK control channel, and their existence
does not establish compatibility with a local gateway adapter.

## Recommended Workbench implementation sequence

These are design inferences from the research, not upstream promises.

| Order | Change | Completion criterion |
| --- | --- | --- |
| 1 | Persist executable, version and adapter capability evidence per attempt | Shell/PATH drift is visible; supported, unavailable and unverified are distinct |
| 2 | Explain selected configuration and instruction discovery | A conflict names contributing sources without exposing secrets; uncertain resolution is labeled |
| 3 | Normalize public native events into the durable timeline | Native session/agent/parent IDs, partial results and terminal outcomes survive restart; reasoning is excluded |
| 4 | Add an optional duplex native control adapter | New input and interruption have acknowledgments; queued, sent and applied are distinct; native permissions remain authoritative |
| 5 | Add bounded parallelism with explicit checkout ownership | Independent tasks overlap; shared edits, worktree cleanup and effect retries have defined ownership |
| 6 | Measure plugin utility and context cost | Representative tasks compare with/without Workbench; results include interventions, completion, latency and context cost |
| 7 | Prototype one mod on a verified compatible runtime | A context or run-status pane passes native validation/tests and degrades cleanly when unavailable |

The first useful delivery should combine capability evidence with a launch
explanation. The local shell-version mismatch and wrapper restrictions supply
real acceptance cases. Full effective-configuration emulation should follow in
small, tested subsets rather than claiming complete parity immediately.

Keep a cross-harness core: durable objectives, dependencies, explicit handoffs,
verified artifacts and honest stopped/interrupted states. Use native features
where their contract is known. Avoid duplicate supervisors, automatic replay of
possibly executed work, mandatory repeated continuation, copying entire
conversation histories, and treating process exit zero as review approval.

## Verification boundary

Research used official documentation, exact official repository revisions and
read-only local help/configuration metadata. Public SDK/mod source was inspected;
no proprietary binary was patched or unofficial source mirror used. The feature
inventory itself made no model calls. A separate Workbench-managed Astra review
checked the high-impact source claims and found one overly broad version gate:
2.1.248 applies to the bundled workflow-authoring skill, not authoring in general.
That wording is corrected, and the review's modelPicker source discrepancy is
recorded above. This was evidence assessment, not a live compatibility test of
every described feature.
