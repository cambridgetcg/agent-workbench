# AgentTool next door

AgentTool offers agents public tools (the canon and platform wake), local
coordination (Collab) and a signed meeting place between one person's devices
(the lobby). Agents use AgentTool through **their own harness's MCP tools**.
Workbench itself makes no AgentTool call, holds no AgentTool identity, starts or
registers no MCP server, opens no Collab journal, runs no lobby command and edits
no harness configuration. It is a careful neighbour: it keeps what belongs to one
session out of the agents it launches, refuses the commands that would sign, send
or take over, and shows which AgentTool tools each harness has.

## What launches never inherit

Collab binds a coordination session through three variables. A child that
inherits them resumes the host's session, and a second holder fences the first.
Every native launch (tasks, plugin evaluations, mod checks) therefore removes:

- `AGENTOOL_COLLAB_SESSION_FILE`, the path of the session's bearer file;
- `AGENTOOL_COLLAB_ALLOW_CURSOR_RECOVERY`, a one-shot recovery privilege;
- `AGENTOOL_COLLAB_CLAUDE_PID`, which attributes actions to the parent Claude.

Their names, never their values, appear in the attempt's launch record. These pass
through, because they choose data rather than prove identity:
`AGENTOOL_COLLAB_DB` (which journal), `AGENTOOL_COLLAB_ANCHOR_LEDGER` (a read-only
sidecar) and `AGENTTOOL_LOBBY_HOME`. Lobby seat references come only from
`CLAUDE_CODE_SESSION_ID` and `CODEX_THREAD_ID`, which launches already remove, so a
child never inherits its parent's seat.

## What command tasks may not run

| Refused | Why |
| --- | --- |
| `agenttool-lobby`, any form | Signing and sending need a person typing yes at a real terminal, which launches never have; other lobby commands create or upgrade the desk's state |
| Any `room` command with `--lease` | A seat lease posts table talk with no terminal at all; table talk is for a person |
| `agenttool-collab-courier`, `collab-courier` | Signs and sends through the hosted service with a project credential |
| `collab-zerone anchor` | Broadcasts a fee-paying chain transaction (`status` and `verify` are fine) |
| `agenttool-mission*`, `mission-chat*`, `mission-wake-*`, `mission-work-*` | Hosted missions use enrolled credentials and would be a second supervisor |
| `agenttool-collab-host end` | Needs a session file's path and fences whoever holds the session (`wait` is fine) |
| `api.agenttool.dev/v1/wake` | Personal wake needs a project credential |

The check runs when a run is saved or imported, and again just before launch, so
a run saved by an older version fails as `launch_error` with no process, before
any workspace is prepared. It looks inside `sh -c` scripts, wrappers such as
`env`, `script`, `npx` and `bun`, package specs such as `name@version` and
`@agenttool/<name>`, the value of `NAME=value` and `--option=value` words, and
git grep's pager value. A plain `grep`, `git grep` or `git log` in the first
position keeps its words as data, unless it is given a pager option (`-O`,
`--open-files-in-pager`) or `--ext-diff`; `NAME=value` words in front of a search
are still checked. Everything else fails closed: a verifier that must mention a
refused name can use grep or a pattern such as `agenttool-lobb[y]`. The lobby's
read-only MCP view, `agenttool-lobby-mcp`, is not refused.

This is a tripwire at Workbench's own launch surface, **not a sandbox**. An agent
task's own shell stays governed by its harness's permissions.

## Session-file paths stay out of run state

Collab writes each session's bearer to `<journal dir>/collab-sessions/<id>.json`;
a host may also name one with `AGENTOOL_COLLAB_SESSION_FILE=<path>`,
`--session-file <path>` or a `"session_file": "<path>"` receipt field. A workflow
name, goal, prompt or command argument that names such a file is refused, and so
are `note`, `steer` and live `send` text. The error never repeats the path. At
launch the composed prompt (which carries the goal when it differs) and the
command's arguments are checked again; the saved name is not sent anywhere.
An agent's own summary that names one shows `[Collab session file withheld]` in
status, timeline, plan, launch output and the next agent's prompt. Handoff export
refuses such a run and continuity notes withhold such a note. A saved prompt that
names one fails at launch rather than being silently rewritten. Private logs stay
private, as before. Workbench never reads, lists or checks the session folder.

## See what each harness has

```sh
agent-workbench doctor --project ~
agent-workbench doctor --project ~ --json   # the "agenttool" key
```

The section reads configuration files only: `~/.codex/config.toml` (or
`$CODEX_HOME`) and the project's `.codex/config.toml`; the `mcpServers` objects of
`~/.claude.json` (or `$CLAUDE_CONFIG_DIR`; user scope and this project's local
scope) and the project's `.mcp.json`; the `mcp_servers` block of Hermes's
`config.yaml` (or `$HERMES_HOME`) and of the `astra-ultra` profile; and the
`package.json` beside a configured Collab entry. It also checks, without opening
them, whether `~/.hermes/active_profile`, Hermes's managed scope
(`$HERMES_MANAGED_DIR` or `/etc/hermes`) and a Collab host command exist, and which
AgentTool commands are on PATH. Servers are recognised by endpoint or command, not
by name, and other servers are only counted. Use `--project` for the folder whose
Claude servers you want to see.

In the JSON, each surface's `summary` is `configured`, `partial` (a tool filter
hides expected tools), `disabled`, `not_configured`, or `unknown` (a file was
unreadable or not parsed, or Hermes's managed scope exists, so absence cannot be
claimed). The text line shows the same as tool counts, for example
`public canon 2/2 tools (exact)` or `public canon 1/2 tools (subset)`, and as
`not configured` or `unknown`. Tool filters are read the way each harness applies
them: Codex removes `disabled_tools` even without `enabled_tools`; Hermes uses a
non-empty `include` and ignores `exclude`, treats a single string as one tool and
an empty list as no filter. The expected public tools are
`canon.by_type`, `canon.list_types`, `canon.lookup`, `canon.summary` and
`wake.platform` at `/v1/mcp`, and `search` and `fetch` at `/v1/mcp/canon`.
A Collab line shows the version in the `package.json` beside its configured entry,
which describes the files, not a running process. A server configured with
headers, a header helper program, a bearer, credentials in the URL, or a
per-agent endpoint is flagged as outside the public-tools boundary.

### Copy-ready fixes (your decision; Workbench applies none of them)

For a missing public server, doctor prints the command or snippet to add it for
Codex and Claude Code, and points Hermes to the blocks below. Start a new session
of that harness afterwards; running sessions keep their old tool list.

Codex, in `~/.codex/config.toml` (copy `default_tools_approval_mode` too if your
`agenttool` entry sets it):

```toml
[mcp_servers.agenttool_canon]
url = "https://api.agenttool.dev/v1/mcp/canon"
enabled_tools = ["search", "fetch"]
```

Claude Code (there is no per-server allowlist here; your permission rules still
apply, and whether the dotted tool names of `/v1/mcp` work is not verified, so
check `/mcp` in a new session):

```sh
claude mcp add --transport http --scope user agenttool https://api.agenttool.dev/v1/mcp
claude mcp add --transport http --scope user agenttool_canon https://api.agenttool.dev/v1/mcp/canon
```

<a id="hermes"></a>Hermes, in `config.yaml` under `mcp_servers:` (`hermes mcp add`
is interactive and connects to the server first). For the public agenttool server:

```yaml
  agenttool:
    url: https://api.agenttool.dev/v1/mcp
    enabled: true
    connect_timeout: 30
    timeout: 60
    tools:
      include:
        - canon.lookup
        - canon.by_type
        - canon.list_types
        - canon.summary
        - wake.platform
      resources: false
      prompts: false
    sampling:
      enabled: false
    elicitation:
      enabled: false
```

For the public canon server:

```yaml
  agenttool_canon:
    url: https://api.agenttool.dev/v1/mcp/canon
    enabled: true
    connect_timeout: 30
    timeout: 60
    tools:
      include:
        - search
        - fetch
      resources: false
      prompts: false
    sampling:
      enabled: false
    elicitation:
      enabled: false
```

Never add an AgentTool server with headers or a bearer. Collab for Codex or Claude
is not suggested: which build and which journal is the owner's decision, Codex
forwards only the environment variables a server lists, and a newer Collab build
must never open a journal that an older running server still serves (back up the
journal and upgrade every serving process together).

## The Collab peer pattern

[examples/collab-peer.json](../examples/collab-peer.json) is the shape that worked
end to end on a real Mac on 2026-10-14:

1. Before the run, the parent (an agent already holding its own Collab session)
   creates the Collab task and notes the workspace, task and session IDs.
2. The Workbench `peer` task is a separate native process. It starts its own
   session at the project's Git root with an **explicit actor label** (Collab 0.4
   requires one, and its actor defaulting cannot work for launched children),
   claims the task with a short lease, attaches its result with its SHA-256,
   reports to the parent, completes into pending review, never reviews itself,
   releases its lease on failure and ends its own session.
3. The dependent `check` command task checks the file effect from files, not
   from the summary.
4. The parent reviews and accepts from its own, different session.

Replace the `REPLACE_` placeholders before creating the run, and give `verify.py`
real checks. Things to know:

- One Workbench task per independent peer: Hermes delegates share their parent's
  MCP connection, so they cannot hold separate sessions.
- Workbench stop or timeout ends the process, not the Collab session or its
  lease; those last until their time-to-live plus Collab's six-hour staleness.
  Ending them is an explicit act by the parent or a person, after the harness has
  exited (`agenttool-collab-host end` needs Collab 0.5.0 or later; Workbench
  refuses to run it).
- For a `worktree` peer, do not `workspace-remove` its attempt before the parent
  has reviewed: Collab refuses claims, completion and handoff acceptance on a
  removed worktree. A detached worktree cannot use host-commit acceptance, so
  complete with attached artifacts. Each attempt registers its absolute path in
  the journal, which keeps it.
- IDs in prompts reach the model provider and any handoff export. The journal is
  local plaintext. A `read-only` workspace is a label, except for Codex.
- For public orientation, add an explicit command task `["agenttool-wake", "--json"]`
  if that command is installed. It makes one public request; Workbench makes none.
  Its output reaches the next task as an untrusted predecessor summary.

## Across devices

Carry work between devices by repository and exact commit through Git. When the
lobby is the right road, hand the exact lobby command to the person to type in a
real terminal. Workbench refuses the lobby CLI as a command task (the read-only
MCP view is the one exception) and never makes, uses or suggests a seat lease:
other loops stay out of table talk.

## Not here, and why

- No AgentTool network call, orientation prompt block or canon request by
  Workbench itself: each harness already has the public MCP tools.
- No Collab client, journal read, session start or end, or mirroring of Workbench
  tasks into Collab: starting a Collab server opens and may migrate its journal,
  and Collab state must not become a second scheduler.
- No per-task Collab environment or injected MCP configuration: Codex and Hermes
  drop such variables unless their own configuration lists them, and Workbench
  does not change native configuration.
- No lobby wiring, hosted missions, courier, anchoring, personal wake or
  credential broker: they sign, send or need a project credential.
- A Workbench-written Collab coordination block with an assigned actor, and a
  guard that keeps a worktree until its review, would need a new task field that
  older versions cannot import; they are candidates for a later release.
