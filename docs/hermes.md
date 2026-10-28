# Hermes with Astra Ultra

Hermes is a native agent harness with its own terminal interface, tools, skills,
memory and session database. Agent Workbench is the shared local workflow layer:
you can talk to Astra in Hermes, invoke Workbench from that conversation, or launch
Hermes tasks from Workbench. Installing the plugin does not replace Hermes's loop.

## Use the configured profile

```sh
hermes-astra-ultra
# Continue a specific native conversation:
hermes-astra-ultra --resume SESSION_ID
# Safe auth metadata, with no token output or refresh:
hermes-codex-token --binding "$HOME/.hermes/profiles/astra-ultra/workbench-subscription.json" --check
agent-workbench doctor
```

The launcher selects the isolated `astra-ultra` profile, `gpt-6-astra`, the named
`chatgpt-subscription` provider and Hermes's `ultra` reasoning setting. Existing
default-profile memory and configuration are separate. Hermes configuration
changes should be checked in a new session; an existing conversation retains
its history and cached prompt assumptions.

The installed macOS profile also sets a supported UTF-8 locale before shell
startup and loads a profile-local `terminal.shell_init_files` script that puts
Homebrew Python first on PATH. This fixes the system Python 3.9 selection and
startup warnings that otherwise contaminated native file-tool output. Global
shell files are unchanged. The fresh native terminal, filename/content search,
write, patch and checksum paths were verified after this correction.

Hermes maps `ultra` to the supported provider effort **`max`** for this model.
This was verified with an actual native CLI request captured against a local
synthetic server. It is the highest effort in this tested path, not a distinct
provider level above `max`. Hermes keeps its own delegation and tools; Claude's
Ultracode behavior does not transfer. Auxiliary title generation is a separate
request and may omit reasoning effort.

## Subscription and credential ownership

The named custom provider uses the official Codex Responses endpoint through
Hermes's `codex_responses` transport. Its `key_cmd` calls the bundled
`scripts/hermes-codex-token` with a private binding file. The binding contains an
expected account identifier and Codex home path, not refresh credentials. An
account change fails explicitly instead of silently switching subscriptions.

The helper reads Codex's existing ChatGPT login and returns only an access token
and a bounded cache lifetime through Hermes's private subprocess pipe. Near
expiry, it uses the official Codex app-server `account/read` refresh operation,
then rereads the result. It shares the Claude Astra refresh lock and bounds the
operation below Hermes's token-command timeout. Hermes does not receive a copied
refresh token. Initial login remains `codex login`; `--check` never logs in or
refreshes. This bridge requires Codex's file-backed ChatGPT auth cache.

The helper's normal stdout is a **credential protocol**. Never invoke it without
`--check` in a human-facing terminal, diagnostic log, prompt or tool transcript.
It refuses a terminal stdout as an additional guard, but redirected output is
still sensitive. Provider configuration contains private account metadata and
stays outside this repository. Do not print or commit it.

The installed profile uses native local tools and normal approvals, disables
external-login adoption and shared telemetry, and explicitly routes title
generation through the same named custom provider. No API-key fallback is
configured by Workbench. A profile's own `fallback_providers` stay in force, and
Workbench evidence records only the configured model label, not which model or
provider answered a turn. Subscription availability and quotas still apply.

## Workbench workflows

```sh
agent-workbench create --harness hermes-astra-ultra \
  --prompt 'Read project instructions. Inspect the parser and explain the failing test.'
agent-workbench plan RUN_ID
agent-workbench run RUN_ID --timeout 900 --max-launches 1
agent-workbench status RUN_ID
agent-workbench timeline RUN_ID
agent-workbench explain --harness hermes-astra-ultra --json
```

Long tasks can raise the profile's turn limit, preload skills and leave Hermes
time to wrap up before Workbench's hard task timeout:

```json
{"id": "lane", "harness": "hermes-astra-ultra", "prompt": "...",
 "max_turns": 150, "skills": ["zerone-development"],
 "run_budget_seconds": 3000, "timeout_seconds": 3300}
```

These become `chat --max-turns`, `--skills` (comma-joined) and `--run-budget`.
`create` offers `--max-turns`, repeatable `--skill`, `--run-budget` and
`--timeout-seconds`. The invocation's `run --timeout` is also a hard stop, so run
this example with `run RUN_ID --timeout 3600`; the default 1800 seconds would end
the run first, and `run` warns before starting when that is certain. A profile's `agent.max_turns`
otherwise applies; when it runs out, Hermes reports an incomplete turn and the
task fails.

Use `hermes` to retain a normal Hermes installation's configured profile/model;
use `hermes-astra-ultra` for the explicit subscription launcher. Both use the
native `chat --query-file - --format stream-json` process protocol. Prompts travel
on stdin. Explicit model and effort options are forwarded; absent overrides
retain native defaults. Workbench rejects the Claude-specific `ultracode` effort
for Hermes and currently exposes `low`, `medium`, `high`, `xhigh`, `max`, `ultra`.
Hermes's additional `none` and `minimal` settings remain available in Hermes.

The runner supplies the prepared task workspace as both process cwd and
`TERMINAL_CWD`. Hermes tools and project-instruction discovery prefer that
environment value, so an inherited controller directory must not override the
task's project or retained worktree. Read-only mode remains a declaration, not
a sandbox. Native unattended approval rules remain unchanged.

`doctor` resolves Astra Ultra within the installation selected by `HERMES_HOME`,
whether it names the installation root or an already-selected profile. A
`subscription_binding: not_configured` result describes only the optional
Workbench binding file; it does not prove native provider authentication is
missing or broken. Verify authentication through the intended native workflow.

`doctor`, `explain --harness` and launch records list the profile's MCP servers
by name, for example an AgentTool Collab server, with `enabled`, `trust` and the
`tools.include` count. `hermes` uses the profile named by `HERMES_HOME`, else
the sticky `active_profile`, else the home. `hermes-astra-ultra` uses
`profiles/astra-ultra` in that installation. Block- and flow-style `config.yaml`
are both read by a minimal stdlib parser. Constructs it does not interpret among
those fields (anchors or merge keys, in block or flow style, for example) and
`enabled` or `tools.include` values whose meaning depends on the YAML loader
(such as `0.0` or `n`) are reported as `unparsed`, never guessed. YAML that
Hermes rejects can still be read here, while Hermes falls back to its
last-known-good configuration. Plugin-provided and managed-scope servers are not
listed, and `HERMES_SAFE_MODE` disables them all natively. A custom wrapper that
sets its own `HERMES_HOME` is not run, so an absent profile there is reported as
`incomplete`, not as having no servers.

`run RUN_ID --collab-context` gives each Hermes prompt in that invocation its
run, task and attempt IDs and a suggested Collab actor label such as
`hermes-astra-ultra · workbench 3f9c2a7b/review#1`; Workbench itself writes
nothing to Collab. Hermes `delegate_task` children run inside the parent's
process and share its MCP server process, which binds one Collab session: a
child's session start is refused once the parent holds one, and would otherwise
bind the shared process under the child's label. Their Collab calls count as the
parent's. That is why the block tells in-process sub-agents not to start a
session, while a separate agent process the task launches may start its own,
with the parent's session ID as `parent_session_id`. Whether a task can reach
Collab at all depends on the profile's MCP servers, not on this option.

Success requires process exit zero and exactly one valid native terminal result
with exit code zero, text and a session identifier. Recoverable individual tool
errors do not prematurely end a task. When Hermes appends its file-mutation
verifier footer (failed `write_file`/`patch` calls), the result carries a warning;
add a command verifier task when file effects matter. Streamed text deltas
appear in the timeline as one message event per contiguous run. Session IDs, selected usage metadata and
tool names can appear in public workflow events; prompts, tool arguments,
tool outputs and reasoning remain in private native logs. Stop, deadlines and
recovery preserve Workbench's existing explicit-retry rules.

Hermes currently supports the process adapter. Workbench's live `send` and
acknowledged turn interrupt remain Claude-compatible features. Process-group
stop is available for Hermes. Hermes plugin evaluation is not implemented.
`explain` can run native version/help checks; unlike the default doctor, that
may cause Hermes's upstream update check. Run launches inspect `--help` and
`chat --help` only and record the version as `not_probed`.

## Install the skill in Hermes

The repository's root `plugin.json` is an Agent Plugins v1 manifest. Hermes can
discover a local checkout under the selected profile's `plugins/` directory:

```sh
mkdir -p "$HOME/.hermes/profiles/astra-ultra/plugins"
ln -s /path/to/agent-workbench "$HOME/.hermes/profiles/astra-ultra/plugins/agent-workbench"
hermes --profile astra-ultra plugins enable agent-workbench
```

Use a stable checkout when enabling it: native publication verifies dependency
inputs and refuses a tree that changes during preparation. Start a new Hermes
session afterwards, call `skills_list`, and select the namespaced Workbench
skill. Its exact qualified name is generated by Hermes. This plugin contributes
instructions and CLI scripts; it does not register an unrestricted MCP server,
auto-run hooks, or change native permissions.

## Explore and customize

Read the [source/module map](research/hermes-agent-2026-09-25.md) and its
[machine-readable inventory](research/hermes-agent-2026-09-25.inventory.json).
The inventory describes source declarations, not installed-service readiness.
Profiles, project instructions, skills, plugins, MCP servers, toolsets, terminal
backends and auxiliary models are the main customization surfaces. Keep core
source patches for behavior that cannot be expressed through those interfaces.

The tested native main request exposed 18 tool schemas: `clarify`,
`delegate_task`, `execute_code`, `hermes_tool_search`, `memory`, `patch`,
`read_file`, `search_files`, `skill_view`, `skills_list`, `terminal`,
`text_to_speech`, `tool_call`, `tool_describe`, `vision_analyze`, `web_extract`,
`web_search`, `write_file`. A schema in a request is not proof of credentials,
service readiness or successful execution. Some optional tools are discovered
and loaded on demand. Inspect descriptions and prerequisites before use.

The source audit and capture used upstream commit
`59004a62356f3a4697ab0fe8ad5086d2b405e2a6` (2026-09-25 local checkout).
The previous installed checkout was retained. The Git pin is authoritative;
an unstamped upstream build can display a placeholder package version.
