"""AgentTool next door: refusals, session-file locators and a read-only wiring report.

Nothing here starts a process, opens a Collab journal or uses the network. Agents
use AgentTool through their own harness's MCP tools; Workbench holds no AgentTool
identity. The refusals are a tripwire at Workbench's own launch surface, not a
sandbox or a shell parser: an agent task's own tools stay governed by its harness.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePath
import re
import shutil
from urllib.parse import urlsplit


# --- Command tasks that Workbench refuses ------------------------------------

_LOBBY = ("runs the AgentTool lobby CLI. Workbench never runs lobby commands: signing and sending need a "
          "person typing yes at a real terminal, which Workbench launches never have, and other lobby commands "
          "open or create the desk's state. Type lobby commands yourself in a terminal.")
_LEASE = ("looks like a lobby room send under a seat lease. Workbench never makes, uses or sends with lobby "
          "seat leases; table talk is for a person at a terminal.")
_COURIER = ("runs Collab courier, which signs and sends through the hosted service with a project credential. "
            "Workbench uses only public AgentTool tools.")
_ANCHOR = ("runs collab-zerone anchor, which broadcasts a fee-paying chain transaction. Workbench never runs it; "
           "status and verify are allowed.")
_MISSION = ("runs an AgentTool hosted mission client or worker, which uses enrolled credentials and would be a "
            "second supervisor. Workbench does not run hosted missions.")
_HOST_END = ("runs agenttool-collab-host end, which needs a session credential file's path and ends, and fences, "
             "whichever process holds that session. End sessions yourself after the harness has exited.")
_WAKE = ("calls AgentTool's personal wake, which needs a project credential. Workbench uses only public "
         "AgentTool tools.")

# (stems, a later word that makes it consequential or None for always, reason)
REFUSED_STEMS = (
    ({"agenttool-lobby"}, None, _LOBBY),
    ({"agenttool-collab-courier", "collab-courier"}, None, _COURIER),
    ({"collab-zerone", "agenttool-collab-zerone"}, "anchor", _ANCHOR),
    ({"agenttool-collab-host"}, "end", _HOST_END),
)
# Prefixes rather than a list of bin names, so new mission entry points stay covered.
REFUSED_PREFIXES = (("agenttool-mission", "mission-chat", "mission-wake-", "mission-work-"), _MISSION)
SEARCH_COMMANDS = {"grep", "egrep", "fgrep"}
_EXTENSIONS = (".ts", ".js", ".mjs", ".cjs", ".py")
_SCRIPT_CHARS = re.compile(r"[\s;&|()<>`'\"$\\]")
_CONTROL = re.compile(r"\|\||&&|\$\(|[;&|()\n`]")
_WORD = re.compile(r"[^\s'\",\[\]{}<>]+")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_PERSONAL_WAKE = re.compile(r"(?i)\bapi\.agenttool\.dev/v1/wake(?![\w-])")


def _segments(argv) -> list[list[str]]:
    """The top-level words, then every shell-like part of any script argument."""
    top, scripts = [], []
    for element in argv:
        if _SCRIPT_CHARS.search(element):
            scripts.extend(_WORD.findall(part) for part in _CONTROL.split(element))
        else:
            top.append(element)
    return [top, *[words for words in scripts if words]]


def _is_search(words) -> bool:
    """A plain search treats every later word as data; pagers can run commands."""
    words = [word for word in words if not _ASSIGNMENT.match(word)]
    if not words or any(word.startswith(("-O", "--open-files-in-pager")) for word in words):
        return False
    name = PurePath(words[0]).name
    if name in SEARCH_COMMANDS:
        return True
    # Only `git grep` or `git log` with no global options: `git -c` can set a pager.
    return name == "git" and len(words) > 1 and words[1] in {"grep", "log"}


def _stems(word) -> list[str]:
    """The command name a word could invoke, and the value of NAME=value or --option=value."""
    candidates = [word]
    if "=" in word:
        candidates.append(word.split("=", 1)[1])
    stems = []
    for candidate in candidates:
        stem = PurePath(candidate).name.lower() if candidate else ""
        for extension in _EXTENSIONS:
            if stem.endswith(extension):
                stem = stem[:-len(extension)]
                break
        stems.append(stem)
    return stems


def _segment_reason(words) -> str | None:
    for index, word in enumerate(words):
        for stem in _stems(word):
            for names, later, reason in REFUSED_STEMS:
                if stem in names and (later is None or later in words[index + 1:]):
                    return reason
            if stem.startswith(REFUSED_PREFIXES[0]):
                return REFUSED_PREFIXES[1]
        if _PERSONAL_WAKE.search(word):
            return _WAKE
    if "room" in words and any(word == "--lease" or word.startswith("--lease=") for word in words):
        return _LEASE
    return None


def refused_command(argv) -> str | None:
    """Why Workbench must not run this command task, or None. Fails closed on mentions."""
    segments = _segments(argv)
    if _is_search(segments[0]):
        return None
    for words in segments:
        if words is segments[0] or not _is_search(words):
            if reason := _segment_reason(words):
                return reason
    return None


# --- Collab session credential-file locators ---------------------------------

# Collab writes each session's bearer to <journal dir>/collab-sessions/<id>.json.
SESSION_FILE_LOCATOR = re.compile(r"(?i)(?:[^\s\"'`<>]*[/\\])?collab-sessions[/\\][A-Za-z0-9_.:-]+\.json(?![A-Za-z0-9_])")
SESSION_FILE_ASSIGNMENT = re.compile(
    r"(?<![{\w])(AGENTOOL_COLLAB_SESSION_FILE[\"']?\s*[:=]\s*[\"']?)"
    r"(?=[^\s\"'$}]*(?:[/\\]|\.json(?!\w)))([^\s\"'$},;][^\s\"',;}]*)")
SESSION_FILE_PATTERNS = (SESSION_FILE_LOCATOR, SESSION_FILE_ASSIGNMENT)
SESSION_FILE_REFUSAL = ("names a Collab session credential file. Workbench keeps these paths out of prompts, "
                        "commands and run state; refer to the session by its ID and let the agent's own Collab "
                        "tools find it.")
WITHHELD = "[Collab session file withheld]"


def names_collab_session_file(text) -> bool:
    return isinstance(text, str) and any(pattern.search(text) for pattern in SESSION_FILE_PATTERNS)


def withhold_session_files(text: str) -> str:
    text = SESSION_FILE_LOCATOR.sub(WITHHELD, text)
    return SESSION_FILE_ASSIGNMENT.sub(lambda match: match.group(1) + WITHHELD, text)


def launch_refusal(task: dict, prompt: str) -> str | None:
    """The refusal for a saved task at launch, so runs saved before these rules are caught too."""
    if task["harness"] == "command":
        reason = refused_command(task.get("argv", []))
        if reason is None and any(names_collab_session_file(element) for element in task.get("argv", [])):
            reason = SESSION_FILE_REFUSAL
    else:
        reason = SESSION_FILE_REFUSAL if names_collab_session_file(prompt) else None
    return f"Task {task['id']} {reason}" if reason else None


# --- Read-only report: AgentTool tools by harness ----------------------------

AGENTTOOL_HOST = "api.agenttool.dev"
PUBLIC_ENDPOINTS = {
    "public_agenttool": ("/v1/mcp", ("canon.by_type", "canon.list_types", "canon.lookup", "canon.summary", "wake.platform")),
    "public_canon": ("/v1/mcp/canon", ("fetch", "search")),
}
PUBLIC_TOOLS_AS_OF = "agenttool build 038a54a4 (2026-10-28)"
LOCAL_COMMANDS = ("agenttool-wake", "agenttool-lobby", "agenttool-lobby-mcp", "agenttool-collab-host")
SURFACES = ("public_agenttool", "public_canon", "collab", "lobby_view")
_LABELS = {"public_agenttool": "public agenttool", "public_canon": "public canon", "collab": "Collab",
           "lobby_view": "lobby view"}
_SUGGESTED_NAMES = {"public_agenttool": "agenttool", "public_canon": "agenttool_canon"}
_CREDENTIAL_KEYS = {"headers", "http_headers", "env_http_headers", "bearer_token_env_var", "auth", "oauth"}
_VALUE_FREE_KEYS = {"env", "headers", "http_headers", "env_http_headers", "auth", "oauth"}
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}\Z")
_COLLAB_ENV = ("AGENTOOL_COLLAB_SESSION_FILE", "AGENTOOL_COLLAB_ALLOW_CURSOR_RECOVERY", "AGENTOOL_COLLAB_CLAUDE_PID",
               "AGENTOOL_COLLAB_DB", "AGENTOOL_COLLAB_ANCHOR_LEDGER")


class _Unparsed(ValueError):
    pass


def _strip_comment(content: str) -> str:
    quote = None
    for index, char in enumerate(content):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or content[index - 1] in " \t"):
            return content[:index].rstrip()
    return content.rstrip()


def _scalar(text: str):
    if text in ("{}", "[]"):
        return {} if text == "{}" else []
    if text[0] == '"':
        if len(text) < 2 or not text.endswith('"'):
            raise _Unparsed()
        try:
            return json.loads(text)
        except ValueError:
            raise _Unparsed() from None
    if text[0] == "'":
        if len(text) < 2 or not text.endswith("'"):
            raise _Unparsed()
        return text[1:-1].replace("''", "'")
    if text[0] in "{[&*!|>%@`?":
        raise _Unparsed()
    return text


def _is_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _yaml_list(items, index, indent):
    values = []
    while index < len(items) and items[index][0] == indent and _is_item(items[index][1]):
        item = items[index][1][1:].strip()
        if not item or ": " in item or item.endswith(":"):
            raise _Unparsed()  # sequences of mappings or nested blocks
        values.append(_scalar(item))
        index += 1
    if index < len(items) and items[index][0] > indent:
        raise _Unparsed()
    return values, index


def _yaml_mapping(items, index, indent):
    result = {}
    while index < len(items):
        level, content = items[index]
        if level < indent:
            break
        if level > indent or content.startswith("-"):
            raise _Unparsed()
        key, separator, rest = content.partition(":")
        if not separator or not key or key != key.strip() or key[0] in "?&*!|>'\"{[" or key in result:
            raise _Unparsed()
        rest = rest.strip()
        index += 1
        if rest:
            value = _scalar(rest)
        elif index < len(items) and items[index][0] > indent:
            value, index = (_yaml_list if _is_item(items[index][1]) else _yaml_mapping)(items, index, items[index][0])
        elif index < len(items) and items[index][0] == indent and _is_item(items[index][1]):
            value, index = _yaml_list(items, index, indent)
        else:
            value = None
        result[key] = value
    return result, index


def _hermes_servers(text: str) -> tuple[dict | None, str]:
    """Block-style subset of Hermes's column-0 ``mcp_servers:`` only; other YAML is unparsed."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"mcp_servers:\s*(?:\{\}\s*)?(?:#.*)?$", line)), None)
    if start is None:
        return {}, "no_mcp_servers"
    if "{}" in lines[start].split("#")[0]:
        return {}, "read"
    items = []
    for line in lines[start + 1:]:
        if line and line[0] not in " \t#":
            break
        if "\t" in line:
            return None, "unparsed"
        content = _strip_comment(line.lstrip(" "))
        if content:
            items.append((len(line) - len(line.lstrip(" ")), content))
    if not items:
        return {}, "read"
    indent = items[0][0]
    servers, index, unparsed = {}, 0, 0
    while index < len(items):
        level, content = items[index]
        if level != indent or content.startswith("-") or ":" not in content:
            return None, "unparsed"
        name, _, rest = content.partition(":")
        if not name or name in servers:
            return None, "unparsed"
        index += 1
        end = index
        while end < len(items) and items[end][0] > indent:
            end += 1
        try:
            if rest.strip():
                raise _Unparsed()
            entry, consumed = _yaml_mapping(items[index:end], 0, items[index][0]) if index < end else ({}, 0)
            if consumed != end - index:
                raise _Unparsed()
            for key in _VALUE_FREE_KEYS & entry.keys():
                entry[key] = {child: None for child in entry[key]} if isinstance(entry[key], dict) else bool(entry[key])
            servers[name] = entry
        except _Unparsed:
            servers[name] = None
            unparsed += 1
        index = end
    return servers, "partial" if unparsed else "read"


def _stem_of(value) -> str:
    stem = PurePath(value).name if isinstance(value, str) else ""
    for extension in _EXTENSIONS:
        if stem.endswith(extension):
            return stem[:-len(extension)]
    return stem


def _classify(entry: dict):
    url = entry.get("url")
    if isinstance(url, str):
        try:
            parts = urlsplit(url)
            host = parts.hostname
        except ValueError:
            return None, None
        if host != AGENTTOOL_HOST:
            return None, None
        path = parts.path.rstrip("/")
        for kind, (endpoint, _) in PUBLIC_ENDPOINTS.items():
            if path == endpoint:
                return kind, f"https://{AGENTTOOL_HOST}{endpoint}"
        if path.startswith("/v1/mcp/agents/"):
            return "agent_scoped", f"https://{AGENTTOOL_HOST}/v1/mcp/agents/[agent id withheld]"
        return "unrecognised_agenttool_endpoint", f"https://{AGENTTOOL_HOST}/[path withheld]"
    arguments = entry.get("args") if isinstance(entry.get("args"), list) else []
    stems = [_stem_of(word) for word in [entry.get("command"), *arguments]]
    if "agenttool-collab-mcp" in stems:
        return "collab", None
    if "agenttool-lobby-mcp" in stems:
        return "lobby_view", None
    return None, None


def _tool_fit(configured, expected):
    if expected is None:
        return {"configured": None, "expected": None, "fit": "not_compared", "missing": [], "unexpected": []}
    if configured is None:
        return {"configured": None, "expected": sorted(expected), "fit": "unrestricted", "missing": [], "unexpected": []}
    have, want = set(configured), set(expected)
    fit = "exact" if have == want else "subset" if have < want else "superset" if have > want else "different"
    return {"configured": sorted(have), "expected": sorted(want), "fit": fit,
            "missing": sorted(want - have), "unexpected": sorted(have - want)}


def _names(values) -> list[str]:
    from .capabilities import _name
    return sorted({_name(value) for value in values if isinstance(value, str)})


def _collab_package(entry: dict) -> dict:
    from .capabilities import _read, _version
    evidence = "files beside the configured entry, not the running process"
    arguments = entry.get("args") if isinstance(entry.get("args"), list) else []
    for argument in arguments:
        if (isinstance(argument, str) and argument.startswith("/") and "$" not in argument
                and argument.endswith("dist/agenttool-collab-mcp.js")):
            entry_file = Path(argument)
            package, status = _read(entry_file.parent.parent / "package.json")
            version = (_version(package.get("version"))
                       if status == "read" and package.get("name") == "@agenttool/collab" else None)
            host = entry_file.parent / "agenttool-collab-host.js"
            return {"package_version": version,
                    "host_cli": "present" if host.is_file() and not host.is_symlink() else "absent",
                    "evidence": evidence}
    return {"package_version": None, "host_cli": "unknown", "evidence": evidence}


def _server_row(name, entry, source, scope, configured_tools):
    from .capabilities import _name
    kind, endpoint = _classify(entry)
    if kind is None:
        return None
    environment = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    env_names = [key for key in environment if isinstance(key, str) and _ENV_NAME.fullmatch(key)]
    if isinstance(entry.get("env_vars"), list):
        env_names += [key for key in entry["env_vars"] if isinstance(key, str) and _ENV_NAME.fullmatch(key)]
    enabled = entry.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.lower() in {"true", "yes", "on", "1"}
    expected = PUBLIC_ENDPOINTS[kind][1] if kind in PUBLIC_ENDPOINTS else None
    row = {"name": _name(name), "source": source, "scope": scope, "kind": kind,
           "transport": "http" if isinstance(entry.get("url"), str) else "stdio" if entry.get("command") else "unknown",
           "enabled": enabled is not False,
           "tools": _tool_fit(configured_tools if expected else None, expected),
           "environment_names": sorted(set(env_names)),
           "credentials_configured": any(bool(entry.get(key)) for key in _CREDENTIAL_KEYS), "warnings": []}
    if endpoint:
        row["endpoint"] = endpoint
    else:
        command = entry.get("command")
        row["command"] = _name(PurePath(command).name) if isinstance(command, str) else None
        row["entry"] = "agenttool-collab-mcp.js" if kind == "collab" else "agenttool-lobby-mcp.js"
    if kind == "collab":
        row["collab"] = _collab_package(entry)
        if "AGENTOOL_COLLAB_SESSION_FILE" in env_names:
            row["warnings"].append({"code": "fixed_session", "message":
                                    "resumes a fixed session; a second process resuming it fences the first"})
        if "AGENTTOOL_COLLAB_DB" in env_names and "AGENTOOL_COLLAB_DB" not in env_names:
            row["warnings"].append({"code": "misspelled_journal", "message":
                                    "Collab reads AGENTOOL_COLLAB_DB (one T); this server uses the default journal"})
    if row["credentials_configured"] and kind not in {"collab", "lobby_view"}:
        row["warnings"].append({"code": "credentials", "message":
                                "configured with credentials: outside the public-tools boundary"})
    if kind == "agent_scoped":
        row["warnings"].append({"code": "agent_scoped", "message":
                                "per-agent endpoint: its write tools need a bearer, outside the public-tools boundary"})
    if kind == "unrecognised_agenttool_endpoint":
        row["warnings"].append({"code": "unrecognised_endpoint", "message":
                                "an AgentTool URL that is not a public MCP endpoint"})
    return row


def _collect(rows, taken, servers, source, scope, tools_of) -> int:
    """Append AgentTool rows; return how many other servers were seen (never named).

    ``taken`` gathers every server name, only so a suggestion never reuses one;
    it never enters the report.
    """
    other = 0
    taken.update(name for name in servers if isinstance(name, str))
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        row = _server_row(name, entry, source, scope, tools_of(entry))
        if row is None:
            other += 1
        else:
            rows.append(row)
    return other


def _codex_tools(entry):
    enabled = entry.get("enabled_tools")
    if not isinstance(enabled, list):
        return None
    disabled = entry.get("disabled_tools") if isinstance(entry.get("disabled_tools"), list) else []
    return _names(tool for tool in enabled if tool not in disabled)


def _hermes_tools(entry):
    tools = entry.get("tools") if isinstance(entry.get("tools"), dict) else {}
    include = tools.get("include")
    if not isinstance(include, list):
        return None
    exclude = tools.get("exclude") if isinstance(tools.get("exclude"), list) else []
    return _names(tool for tool in include if tool not in exclude)


def _summary(rows, sources, unparsed):
    unknown = bool(unparsed) or any(source["status"] not in ("read", "absent") or source.get("parse") == "unparsed"
                                    for source in sources)
    summary = {}
    for surface in SURFACES:
        found = [row for row in rows if row["kind"] == surface]
        enabled = [row for row in found if row["enabled"]]
        if enabled:
            complete = {"exact", "superset", "unrestricted", "not_compared"}
            summary[surface] = "configured" if any(row["tools"]["fit"] in complete for row in enabled) else "partial"
        elif found:
            summary[surface] = "disabled"
        else:
            summary[surface] = "unknown" if unknown else "not_configured"
    return summary


def _harness(rows, sources, other, unparsed=0, **extra):
    return {"sources": sources, "servers": rows, "summary": _summary(rows, sources, unparsed),
            "other_server_count": other, "unparsed_server_count": unparsed, **extra}


def _codex_section(home, project, env):
    from .capabilities import _read, _root
    root = _root(env.get("CODEX_HOME"), home / ".codex", home)
    candidates = [(root / "config.toml", "$CODEX_HOME/config.toml" if env.get("CODEX_HOME") else "~/.codex/config.toml", "user")]
    if project is not None:
        candidates.append((project / ".codex/config.toml", "$PROJECT/.codex/config.toml", "project"))
    rows, sources, other, seen, taken = [], [], 0, set(), set()
    for path, label, scope in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue  # --project at the home folder names the user file twice
        seen.add(resolved)
        data, status = _read(path, toml=True)
        sources.append({"source": label, "status": status, "reader": "toml"})
        servers = data.get("mcp_servers") if data else None
        if isinstance(servers, dict):
            other += _collect(rows, taken, servers, label, scope, _codex_tools)
    return _harness(rows, sources, other), taken


def _claude_section(home, project, env):
    from .capabilities import _read, _root
    root = _root(env.get("CLAUDE_CONFIG_DIR"), home, home)
    label = "$CLAUDE_CONFIG_DIR/.claude.json" if env.get("CLAUDE_CONFIG_DIR") else "~/.claude.json"
    rows, sources, other, taken = [], [], 0, set()
    data, status = _read(root / ".claude.json")
    sources.append({"source": label, "status": status, "reader": "json", "kept": "mcpServers objects only"})
    if data:
        scoped = [(data.get("mcpServers"), "user")]
        projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
        if project is not None and isinstance(projects.get(str(project.resolve())), dict):
            scoped.append((projects[str(project.resolve())].get("mcpServers"), "local"))
        data = None  # Account state is never kept past this point.
        for servers, scope in scoped:
            if isinstance(servers, dict):
                other += _collect(rows, taken, servers, label, scope, lambda entry: None)
    if project is not None:
        shared, status = _read(project / ".mcp.json")
        sources.append({"source": "$PROJECT/.mcp.json", "status": status, "reader": "json"})
        if shared and isinstance(shared.get("mcpServers"), dict):
            other += _collect(rows, taken, shared["mcpServers"], "$PROJECT/.mcp.json", "project", lambda entry: None)
    return _harness(rows, sources, other), taken


def _hermes_section(path, label, active_profile=None):
    from .capabilities import _read_bytes
    raw, status = _read_bytes(path)
    source = {"source": label, "status": status, "reader": "yaml-subset"}
    rows, other, unparsed, taken = [], 0, 0, set()
    if raw is not None:
        try:
            servers, parse = _hermes_servers(raw.decode("utf-8"))
        except UnicodeError:
            servers, parse = None, "unparsed"
        source["parse"] = parse
        if servers:
            unparsed = sum(entry is None for entry in servers.values())
            other = _collect(rows, taken, servers, label, "profile", _hermes_tools)
    extra = {"active_profile_file": active_profile} if active_profile is not None else {}
    return _harness(rows, [source], other, unparsed, **extra), taken


def _launch_environment(env):
    from .processes import COLLAB_SESSION_ENV
    report = {}
    for name in _COLLAB_ENV:
        value = env.get(name)
        status = "absent" if value is None else "empty" if value == "" else "present"
        report[name] = {"status": status, "launches": "withheld" if name in COLLAB_SESSION_ENV else "passed"}
    return report


def _suggestions(harnesses, taken_names):
    suggestions = []
    for harness in ("codex", "claude", "hermes"):
        section = harnesses[harness]
        user_source = section["sources"][0]
        if user_source["status"] != "read":
            continue
        taken = taken_names[harness]
        for surface in ("public_agenttool", "public_canon"):
            if section["summary"][surface] != "not_configured":
                continue
            endpoint, tools = PUBLIC_ENDPOINTS[surface]
            url = f"https://{AGENTTOOL_HOST}{endpoint}"
            name = _SUGGESTED_NAMES[surface]
            name = "<choose-a-name>" if name in taken else name
            item = {"harness": harness, "surface": surface, "target": user_source["source"], "commands": [],
                    "snippet": None, "see": None,
                    "then": f"Start a new {harness} session; running sessions keep their old tool list."}
            if harness == "codex":
                listed = ", ".join(f'"{tool}"' for tool in (("search", "fetch") if surface == "public_canon" else tools))
                item["snippet"] = f'[mcp_servers.{name}]\nurl = "{url}"\nenabled_tools = [{listed}]\n'
            elif harness == "claude":
                item["target"] = "claude mcp (user scope)"
                item["commands"] = [f"claude mcp add --transport http --scope user {name} {url}"]
            else:
                item["see"] = "docs/agenttool.md#hermes"
            suggestions.append(item)
    return suggestions


def wiring(*, home, project, env) -> dict:
    """Which AgentTool MCP servers each harness has configured. Configured, not observed.

    Reads bounded config files only. Starts no process, opens no Collab journal or
    session file, runs no lobby command, uses no network and changes nothing.
    Names are reported, never values; other servers are only counted.
    """
    home = Path(home)
    project = Path(project) if project is not None else None
    hermes_home = Path(env["HERMES_HOME"]) if env.get("HERMES_HOME") else home / ".hermes"
    hermes_label = "$HERMES_HOME/config.yaml" if env.get("HERMES_HOME") else "~/.hermes/config.yaml"
    active = "present" if (hermes_home / "active_profile").exists() else "absent"
    sections = {
        "codex": _codex_section(home, project, env),
        "claude": _claude_section(home, project, env),
        "hermes": _hermes_section(hermes_home / "config.yaml", hermes_label, active_profile=active),
        "hermes-astra-ultra": _hermes_section(home / ".hermes/profiles/astra-ultra/config.yaml",
                                              "~/.hermes/profiles/astra-ultra/config.yaml"),
    }
    harnesses = {harness: section for harness, (section, _) in sections.items()}
    taken = {harness: names for harness, (_, names) in sections.items()}
    limitations = [
        "Configured, not observed: a running server may differ from its files.",
        "~/.claude.json holds account state as well as MCP servers; it is parsed in memory and only mcpServers objects are kept.",
        "Claude managed or remote MCP, plugin-provided servers, project-server approval, --mcp-config and "
        "--strict-mcp-config are not evaluated.",
        "Hermes config.yaml is read with a block-style subset reader; other YAML is reported unparsed and its tools as unknown.",
        "Whether Claude Code accepts the dotted tool names of /v1/mcp was not verified; check /mcp in a new session.",
    ]
    if active == "present":
        limitations.append("~/.hermes/active_profile exists and may select another profile; that selection is not resolved.")
    return {
        "version": 1,
        "scope": {"read_only": True, "network": "none", "processes_started": False, "collab_journal_opened": False,
                  "collab_session_files_opened": False, "lobby_commands_run": False, "values_reported": False,
                  "configuration_changed": False},
        "expected_public_tools": {**{kind: {"endpoint": f"https://{AGENTTOOL_HOST}{endpoint}", "tools": list(tools)}
                                     for kind, (endpoint, tools) in PUBLIC_ENDPOINTS.items()},
                                  "as_of": PUBLIC_TOOLS_AS_OF},
        "harnesses": harnesses,
        "launch_environment": _launch_environment(env),
        "commands": {name: "present" if shutil.which(name, path=env.get("PATH", "")) else "absent"
                     for name in LOCAL_COMMANDS},
        "suggestions": _suggestions(harnesses, taken),
        "limitations": limitations,
    }


def _surface_text(surface, state, rows):
    if state in {"configured", "partial"} and rows:
        row = rows[0]
        if surface == "collab":
            collab = row.get("collab", {})
            host = {"present": "host command present", "absent": "host command absent"}.get(collab.get("host_cli"),
                                                                                             "host command unknown")
            return f"Collab {collab.get('package_version') or 'version unknown'} ({host})"
        if surface == "lobby_view":
            return "lobby view configured"
        tools = row["tools"]
        if tools["fit"] == "unrestricted":
            return f"{_LABELS[surface]} all tools (no allowlist)"
        found = len(tools["configured"]) - len(tools["unexpected"])
        return f"{_LABELS[surface]} {found}/{len(tools['expected'])} tools ({tools['fit']})"
    if surface in PUBLIC_ENDPOINTS:
        return f"{_LABELS[surface]} {state.replace('_', ' ')}"
    return f"{_LABELS[surface]} disabled" if state == "disabled" else None


def format_lines(section: dict) -> list[str]:
    """Text lines for doctor, built only from the report itself."""
    if not section:
        return []
    lines = ["AgentTool tools by harness (configured files, not running servers; nothing started, opened or changed):"]
    for harness, data in section.get("harnesses", {}).items():
        if all(source["status"] == "absent" for source in data.get("sources", [])):
            lines.append(f"  {harness}: configuration absent")
            continue
        parts = []
        for surface in SURFACES:
            rows = [row for row in data["servers"] if row["kind"] == surface and row["enabled"]]
            text = _surface_text(surface, data["summary"][surface], rows)
            if text:
                parts.append(text)
        if data.get("other_server_count"):
            count = data["other_server_count"]
            parts.append(f"{count} other server{'s' if count != 1 else ''}")
        if data.get("unparsed_server_count"):
            parts.append(f"{data['unparsed_server_count']} unparsed")
        lines.append(f"  {harness}: " + "; ".join(parts))
        for row in data["servers"]:
            for warning in row.get("warnings", []):
                lines.append(f"    {row['name']}: {warning['message']}")
    environment = section.get("launch_environment", {})
    withheld = [name for name, entry in environment.items() if entry["launches"] == "withheld"]
    present = [name for name in withheld if environment[name]["status"] != "absent"]
    journal = environment.get("AGENTOOL_COLLAB_DB", {}).get("status", "absent")
    lines.append(f"  Launches withhold {', '.join(withheld)} "
                 f"({'set here: ' + ', '.join(present) if present else 'none set here'}) and pass AGENTOOL_COLLAB_DB "
                 f"through ({'not set here' if journal == 'absent' else journal + ' here'})")
    commands = section.get("commands", {})
    shown = [f"{name} (Workbench never runs it)" if name == "agenttool-lobby" else name
             for name, state in commands.items() if state == "present"]
    missing = [name for name, state in commands.items() if state != "present"]
    lines.append("  On PATH: " + (", ".join(shown) or "none") + ("; absent: " + ", ".join(missing) if missing else ""))
    suggestions = section.get("suggestions", [])
    if suggestions:
        lines.append("  Yours to add if you want them (printed only; Workbench changes nothing; start a new session afterwards):")
        for item in suggestions:
            label = f"    {item['harness']}, {_LABELS[item['surface']]}: "
            if item["snippet"]:
                lines.append(label + f"add to {item['target']}")
                lines.extend("      " + line for line in item["snippet"].rstrip("\n").split("\n"))
            lines.extend(label + command for command in item["commands"])
            if item["see"]:
                lines.append(label + f"see {item['see']}")
    return lines
