"""Names-only inventory of the MCP servers a harness child would inherit.

Native configuration files are read, never executed. Besides display labels of
the files read, only server names, enabled/trust flags and tool-filter counts
leave this module: commands, arguments, environment, URLs, headers and tokens
are at most delimited in memory and discarded. A listed server is configured,
not observed running.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from .capabilities import _MAX_ROWS, _display, _name, _read, _read_bytes, _root, _setting_value


_SCOPE = "Configured server names and selected flags only; commands, arguments, environment, URLs, headers and tokens are omitted."
_HERMES_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_YAML_TRUE = {"true", "1", "yes", "on"}
_YAML_FALSE = {"false", "0", "no", "off"}
_YAML_NULL = {"", "~", "null", "Null", "NULL"}


def _label(path: Path, home: Path, project: Path | None = None) -> str:
    # Resolved projects and working directories can spell home differently
    # (/private/var for /var); never let that print an absolute home path.
    label = _display(path, home, project)
    if label.startswith("/"):
        try:
            label = _display(path, home.resolve(), project)
        except (OSError, RuntimeError):
            pass
    return label


def _hermes_home(env: dict, home: Path, cwd: Path | None = None) -> tuple[Path, Path]:
    """Return the home HERMES_HOME selects and that installation's root.

    HERMES_HOME can be either an installation root or an already-selected named
    profile. Unset or blank keeps ~/.hermes; a relative value follows ``cwd``.
    """
    configured = env.get("HERMES_HOME")
    if not isinstance(configured, str) or not configured.strip():
        selected = home / ".hermes"
    elif configured == "~":
        selected = home
    elif configured.startswith("~/"):
        selected = home / configured[2:]
    else:
        selected = Path(configured)
        if not selected.is_absolute():
            selected = (Path.cwd() if cwd is None else cwd) / selected
    return selected, selected.parent.parent if selected.parent.name == "profiles" else selected


class _Unparsed(ValueError):
    """The minimal YAML reader met a construct it does not interpret."""


# Only these parts of mcp_servers are decoded ("*": any server name). Other
# values are delimited and discarded, so their quoting or anchors never matter.
_HERMES_MCP = {"*": {"enabled": True, "trust": True, "tools": {"include": True}}}


def _yaml_child(schema, key):
    return schema if schema is None or schema is True else schema.get(key, schema.get("*"))


def _yaml_space(text: str, at: int) -> bool:
    return at >= len(text) or text[at] in " \t\r\n"


def _yaml_skip(text: str, at: int) -> int:
    while at < len(text):
        if text[at] in " \t\r\n":
            at += 1
        elif text[at] == "#" and (at == 0 or text[at - 1] in " \t\r\n"):
            newline = text.find("\n", at)
            at = len(text) if newline < 0 else newline
        else:
            break
    return at


def _yaml_quoted(text: str, at: int, decode: bool) -> tuple[object, int]:
    quote, end = text[at], at + 1
    while end < len(text):
        if quote == '"' and text[end] == "\\":
            end += 2
        elif quote == "'" and text.startswith("''", end):
            end += 2
        elif text[end] == quote:
            body = text[at + 1:end]
            if not decode:
                return None, end + 1
            if "\n" in body:
                raise _Unparsed
            if quote == "'":
                return body.replace("''", "'"), end + 1
            try:
                return json.loads('"' + body + '"'), end + 1
            except ValueError:
                raise _Unparsed from None
        else:
            end += 1
    raise _Unparsed


def _yaml_flow(text: str, at: int, schema=True) -> tuple[object, int]:
    """Parse one flow node; with ``schema`` None it is only delimited."""
    at = _yaml_skip(text, at)
    char = text[at:at + 1]
    if schema is None and (char in ("&", "!") or char == "?" and _yaml_space(text, at + 1)):
        # Anchors, tags and explicit-key markers only decorate a delimited node.
        while not _yaml_space(text, at) and text[at] not in ",[]{}":
            at += 1
        return _yaml_flow(text, at, schema)
    if not char or (char in "&*!|>%@`?" and (schema is not None or char != "*")) or (char == "-" and _yaml_space(text, at + 1)):
        raise _Unparsed
    if char in "\"'":
        return _yaml_quoted(text, at, schema is not None)
    if char not in "[{":
        end = at
        while not (end >= len(text) or text[end] in ",[]{}" or (text[end] == "#" and text[end - 1] in " \t\n")
                   or (text[end] == ":" and (_yaml_space(text, end + 1) or text[end + 1] in ",[]{}"))):
            end += 1
        value = " ".join(text[at:end].split())
        return (None if schema is None or value in _YAML_NULL else value), end
    close, items, mapping, at = "]" if char == "[" else "}", [], {}, at + 1
    while True:
        at = _yaml_skip(text, at)
        if text.startswith(close, at):
            return (None if schema is None else items if char == "[" else mapping), at + 1
        if char == "[":
            item, at = _yaml_flow(text, at, True if schema is True else None)
            if text.startswith(":", _yaml_skip(text, at)):
                raise _Unparsed
            items.append(item)
        else:
            start = _yaml_skip(text, at)
            key, at = _yaml_flow(text, start, None if schema is None else True)
            # As in block style, a plain merge key could inject decoded settings.
            if schema is not None and (key is not None and not isinstance(key, str)
                                       or key == "<<" and text[start] not in "\"'"):
                raise _Unparsed
            child, value, at = _yaml_child(schema, key), None, _yaml_skip(text, at)
            if text.startswith(":", at):
                at = _yaml_skip(text, at + 1)
                if not text.startswith((",", close), at):
                    value, at = _yaml_flow(text, at, child)
            if child is not None:
                if key in mapping:
                    raise _Unparsed
                mapping[key] = value
        at = _yaml_skip(text, at)
        if text.startswith(",", at):
            at += 1
        elif not text.startswith(close, at):
            raise _Unparsed


def _yaml_rows(text: str) -> list[tuple[int, int, int]]:
    """Indent, start and end offsets of each line that is not blank or a comment."""
    rows, start = [], 0
    for line in text.split("\n"):
        content = line.lstrip(" ")
        if content.strip() and not content.startswith("#"):
            rows.append((len(line) - len(content), start, start + len(line)))
        start += len(line) + 1
    return rows


def _yaml_uncomment(value: str) -> str:
    return re.split(r"[ \t]#", value, maxsplit=1)[0].strip()


def _yaml_item(text: str, row: tuple[int, int, int]) -> bool:
    at = row[1] + row[0]
    return text[at] == "-" and _yaml_space(text, at + 1)


def _yaml_key(text: str, start: int, end: int, decode: bool = True) -> tuple[str | None, int]:
    """A block mapping key (None unless decoded) and the offset after its colon."""
    if text[start] in "\"'":
        key, at = _yaml_quoted(text, start, decode)
        while at < end and text[at] in " \t":
            at += 1
    else:
        # A merge key could inject decoded settings; elsewhere it is harmless.
        if text[start] in "-?:,[]{}#&*!|>%@`\t" or decode and text.startswith("<<", start):
            raise _Unparsed
        at = text.find(":", start, end)
        while 0 <= at < end - 1 and text[at + 1] not in " \t":
            at = text.find(":", at + 1, end)
        if at < 0 or re.search(r"[ \t]#", text[start:at]):
            raise _Unparsed
        key = text[start:at].rstrip(" \t")
        key = None if not decode or key in _YAML_NULL else key
    if at >= end or text[at] != ":" or at + 1 < end and text[at + 1] not in " \t":
        raise _Unparsed
    return key, at + 1


def _yaml_block(text: str, rows: list, index: int, indent: int, at: int, key: bool, schema) -> tuple[object, int]:
    """The value starting at ``at`` on rows[index], owned by a key or item at ``indent``."""
    end, following = rows[index][2], index + 1
    while at < end:
        if text[at] in " \t":
            at += 1
        elif schema is None and text[at] in "&!":
            # Anchors and tags only decorate a value that is merely delimited.
            while at < end and text[at] not in " \t":
                at += 1
        else:
            break
    def owned(position):
        # Deeper rows belong to this value, as do a key's same-indent "- " items.
        return position < len(rows) and (rows[position][0] > indent or key and rows[position][0] == indent
                                         and _yaml_item(text, rows[position]))
    if not key and at < end and (text[at] == "-" and _yaml_space(text, at + 1) or _yaml_compact(text, at, end, schema)):
        # "- - x" or "- key: value": the item is a node starting at this column.
        rows[index] = (at - rows[index][1], rows[index][1], end)
        return _yaml_node(text, rows, index, schema)
    if text[at:at + 1] in ("[", "{", '"', "'"):
        value, stop = _yaml_flow(text, at, schema)
        newline = text.find("\n", stop)
        newline = len(text) if newline < 0 else newline
        if _yaml_uncomment(" " + text[stop:newline]):
            raise _Unparsed
        while following < len(rows) and rows[following][1] < newline:
            following += 1
        if owned(following):
            raise _Unparsed
        return value, following
    if at == end or text[at] == "#":
        if not owned(following):
            return None, following
        row = rows[following]
        if _yaml_item(text, row) or _yaml_compact(text, row[1] + row[0], row[2], schema):
            return _yaml_node(text, rows, following, schema)
        # A scalar or flow value may also start on the next, deeper line.
        return _yaml_block(text, rows, following, indent, row[1] + row[0], True, schema)
    if schema is None:
        # Plain or block scalar text continues only on more-indented rows.
        while following < len(rows) and rows[following][0] > indent:
            following += 1
        return None, following
    if text[at] in "&*!|>%@`?" or text[at] == "-" and _yaml_space(text, at + 1):
        raise _Unparsed
    # A plain scalar may continue on more-indented lines; fold them with spaces.
    parts = [_yaml_uncomment(text[at:end])]
    while following < len(rows) and rows[following][0] > indent:
        parts.append(_yaml_uncomment(text[rows[following][1] + rows[following][0]:rows[following][2]]))
        following += 1
    value = " ".join(parts)
    if re.search(r":(?:[ \t]|\Z)", value):
        raise _Unparsed
    return (None if value in _YAML_NULL else value), following


def _yaml_compact(text: str, at: int, end: int, schema) -> bool:
    try:
        _yaml_key(text, at, end, schema is not None)
        return True
    except _Unparsed:
        return False


def _yaml_node(text: str, rows: list, index: int, schema) -> tuple[object, int]:
    indent, sequence = rows[index][0], _yaml_item(text, rows[index])
    result = [] if sequence else {}
    while index < len(rows) and rows[index][0] == indent and _yaml_item(text, rows[index]) == sequence:
        if text[rows[index][1] + indent] == "\t":
            raise _Unparsed
        if sequence:
            value, index = _yaml_block(text, rows, index, indent, rows[index][1] + indent + 1, False,
                                       True if schema is True else None)
            result.append(value)
            continue
        start = rows[index][1] + indent
        if schema is None and text[start] == "?" and _yaml_space(text, start + 1):
            # PyYAML writes empty or complex keys explicitly: "? key", then ": value".
            _, index = _yaml_block(text, rows, index, indent, start + 1, False, None)
            if index < len(rows) and rows[index][0] == indent and text[rows[index][1] + indent] == ":":
                _, index = _yaml_block(text, rows, index, indent, rows[index][1] + indent + 1, False, None)
            continue
        key, at = _yaml_key(text, start, rows[index][2], schema is not None)
        child = _yaml_child(schema, key)
        value, index = _yaml_block(text, rows, index, indent, at, True, child)
        if child is not None:
            if key in result:
                raise _Unparsed
            result[key] = value
    if index < len(rows) and rows[index][0] > indent:
        raise _Unparsed
    return result, index


def _hermes_servers(text: str) -> object:
    """Return the top-level ``mcp_servers`` value of a Hermes config.yaml.

    A deliberately minimal reader: block mappings and sequences, flow
    collections and plain or single-line quoted scalars. Only server names,
    enabled, trust and tools.include are decoded; other values are walked
    just enough to delimit them. Anything else among the decoded parts
    (anchors, aliases, tags, merge keys, block scalars), multiple documents
    or characters PyYAML-based Hermes rejects raise _Unparsed instead.
    """
    if "mcp_servers" not in text:
        return None
    text = re.sub("\r\n?|[\x85\u2028\u2029]", "\n", text)
    if re.search("[^\t\n\x20-\x7e\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", text):
        raise _Unparsed
    rows = _yaml_rows(text)
    first = 1 if rows and text[rows[0][1]:rows[0][2]].rstrip() == "---" else 0
    if first >= len(rows):
        return None
    schema = {"mcp_servers": _HERMES_MCP}
    if text.startswith("{", rows[first][1] + rows[first][0]):
        document, end = _yaml_flow(text, rows[first][1], schema)
        if _yaml_skip(text, end) != len(text):
            raise _Unparsed
    else:
        document, index = _yaml_node(text, rows, first, schema)
        if index < len(rows):
            raise _Unparsed
    return document.get("mcp_servers") if isinstance(document, dict) else None


def _codex_server(entry: dict) -> dict:
    shape = {"enabled": _setting_value("enabled", entry["enabled"])} if "enabled" in entry else {}
    if isinstance(entry.get("enabled_tools"), list):
        shape["enabled_tools_count"] = len(entry["enabled_tools"])
    return shape


def _hermes_server(entry: dict) -> dict:
    # Hermes's own readings: absent enabled means on, absent trust means full,
    # and tools.include (a name/glob or a list of them) is a whitelist. Other
    # enabled words, and an include that looks like a number or boolean, depend
    # on how Hermes's YAML loader types them (0.0 is off; n is off under ruamel
    # but on under PyYAML; an include of 5 is ignored), so they are not guessed.
    shape = {}
    if entry.get("enabled") is not None:
        word = entry["enabled"].strip().lower() if isinstance(entry["enabled"], str) else None
        if word not in _YAML_TRUE | _YAML_FALSE:
            return {"status": "unparsed"}
        shape["enabled"] = word in _YAML_TRUE
    if entry.get("trust") is not None:
        word = entry["trust"].strip().lower() if isinstance(entry["trust"], str) else None
        shape["trust"] = word if word in {"full", "untrusted"} else "[unrecognized trust withheld]"
    include = entry["tools"].get("include") if isinstance(entry.get("tools"), dict) else None
    if isinstance(include, str) and (re.match(r"[-+.0-9]", include) or include.lower() in _YAML_TRUE | _YAML_FALSE | {"y", "n"}):
        return {"status": "unparsed"}
    if isinstance(include, (list, str)):
        shape["tools_include_count"] = len(include) if isinstance(include, list) else 1
    return shape


def _source(label: str, scope: str, status: str, servers: object, shape=None) -> dict:
    row = {"source": label, "scope": scope, "status": status, "server_count": 0, "servers": []}
    if status == "read" and servers is not None and not isinstance(servers, dict):
        row["status"] = "malformed"
    elif isinstance(servers, dict):
        row["server_count"] = len(servers)
        for name, entry in list(servers.items())[:_MAX_ROWS]:
            server = {"name": _name(name)}
            server.update((shape(entry) if shape else {}) if isinstance(entry, dict) else {"status": "malformed"})
            row["servers"].append(server)
    return row


def _report(sources: list, limitations: list, warnings: list, *, incomplete: bool = False, **extra) -> dict:
    for row in sources:
        failed = sum("status" in server for server in row["servers"])
        if row["status"] not in {"read", "absent"}:
            warnings.append(f"MCP servers in {row['source']} could not be inspected ({row['status']}); unlisted servers may be inherited.")
        if failed:
            warnings.append(f"{failed} MCP server entr{'y' if failed == 1 else 'ies'} in {row['source']} could not be inspected.")
        if row["server_count"] > len(row["servers"]):
            warnings.append(f"Only the first {_MAX_ROWS} MCP servers in {row['source']} are listed.")
        incomplete = incomplete or row["status"] not in {"read", "absent"} or failed or row["server_count"] > len(row["servers"])
    # Rows listed across sources; each source's server_count is what it declares.
    listed = sum(len(row["servers"]) for row in sources)
    return {"status": "incomplete" if incomplete else "unverified" if listed else "not_configured",
            "scope": _SCOPE, **extra, "sources": sources, "listed_server_count": listed,
            "warnings": warnings, "limitations": limitations}


def mcp_inventory(harness: str, project: Path | None, *, home: Path | None = None, env: dict | None = None,
                  cwd: Path | None = None) -> dict:
    """List the MCP servers a harness child would read from native configuration.

    Plugins, managed policy and wrappers can change the native set, so a
    listed server is a configured candidate. ``cwd`` resolves a relative
    HERMES_HOME (default: this process's working directory).
    """
    env = dict(os.environ if env is None else env)
    home = Path.home() if home is None else Path(home)
    project = None if project is None else Path(project).resolve()
    if harness in {"claude", "astra", "astra-ultra"}:
        configured = env.get("CLAUDE_CONFIG_DIR")
        label = "$CLAUDE_CONFIG_DIR/.claude.json" if configured else "~/.claude.json"
        # Unlike explain_configuration, this opens Claude Code's account state,
        # which can also hold a saved API key. It is parsed in memory, and only
        # MCP server names leave it; account_state_file_read reports the read.
        data, status = _read(_root(configured, home, home) / ".claude.json")
        opened = status in {"read", "malformed", "too_large"}
        data = data or {}
        sources, warnings = [_source(label, "user", status, data.get("mcpServers"))], []
        # Claude Code prefers a legacy global .config.json when one exists.
        legacy = _root(configured, home / ".claude", home) / ".config.json"
        if legacy.is_file():
            warnings.append(f"{'$CLAUDE_CONFIG_DIR' if configured else '~/.claude'}/.config.json exists; "
                            f"Claude Code may read user MCP servers from it instead of {label}.")
        if project is not None:
            # Local scope is keyed by the Git root containing the project, else the project.
            key = next((path for path in (project, *project.parents) if (path / ".git").exists()), project)
            projects = data.get("projects")
            entry = projects.get(key.as_posix()) if isinstance(projects, dict) else None
            if isinstance(entry, dict) and "mcpServers" in entry:
                sources.append(_source(f"{label} projects[{_label(key, home, project)}]", "local", "read", entry["mcpServers"]))
            # Like Claude Code, walk down from below the filesystem root to the project.
            for directory in reversed([project, *project.parents][:-1]):
                shared, shared_status = _read(directory / ".mcp.json")
                if shared_status != "absent":
                    sources.append(_source(_label(directory / ".mcp.json", home, project), "project",
                                               shared_status, (shared or {}).get("mcpServers")))
        limitations = ["Plugins, managed MCP policy, claude.ai connectors and --mcp-config can add servers; per-project disables and .mcp.json approvals are not resolved."]
        if harness != "claude":
            limitations.append("The Astra wrapper was not executed; it may select another configuration directory or MCP flags.")
        return _report(sources, limitations, warnings, incomplete=bool(warnings), account_state_file_read=opened)
    if harness == "codex":
        root = _root(env.get("CODEX_HOME"), home / ".codex", home)
        label = "$CODEX_HOME" if env.get("CODEX_HOME") else "~/.codex"
        paths = [(root / "config.toml", label + "/config.toml", "user")]
        if project is not None:
            paths.append((project / ".codex/config.toml", "$PROJECT/.codex/config.toml", "project"))
        sources = []
        for path, source, scope in paths:
            data, status = _read(path, toml=True)
            sources.append(_source(source, scope, status, (data or {}).get("mcp_servers"), _codex_server))
        return _report(sources, ["A project file applies only when Codex trusts the project. Workbench does not pass --ignore-user-config, which would drop the user file's servers.",
                                     "Only the project's own .codex/config.toml is read; Codex may also layer those in directories between the project and its Git root.",
                                     "Plugins, profiles, managed configuration and -c overrides can change the set."], [])
    if harness not in {"hermes", "hermes-astra-ultra"}:
        raise ValueError("MCP inventory requires an agent harness")
    selected, root = _hermes_home(env, home, cwd)
    configured = env.get("HERMES_HOME")
    warnings, selection = [], "hermes_home" if isinstance(configured, str) and configured.strip() else "default_home"
    if harness == "hermes-astra-ultra":
        selected, selection = root / "profiles/astra-ultra", "astra_ultra_wrapper_candidate"
    elif selected.parent.name == "profiles":
        selection = "hermes_home_profile"
    else:
        # Without an explicit profile, native Hermes follows the root's sticky active_profile.
        raw, status = _read_bytes(root / "active_profile", 256)
        name = raw.decode("utf-8-sig", "replace").strip() if raw is not None else ""
        name = "default" if name.casefold() == "default" else name.lower()
        if status not in {"read", "absent"} or name and not _HERMES_PROFILE.match(name):
            selection = "active_profile_uninterpretable"
            warnings.append("Hermes's sticky active_profile could not be interpreted; the unprofiled home was inspected.")
        elif name and name != "default":
            selected, selection = root / "profiles" / name, "sticky_active_profile"
    path = selected / "config.yaml"
    raw, status = _read_bytes(path)
    servers = None
    if raw is not None:
        try:
            servers = _hermes_servers(raw.decode("utf-8-sig"))
        except (_Unparsed, UnicodeError, RecursionError):
            status = "unparsed"
    label = _label(path, home)
    limitations = ["Plugin-provided, managed-scope and last-known-good fallback configuration are not inspected; Hermes also skips entries it judges suspicious.",
                   "A minimal reader takes names, enabled, trust and tools.include counts; constructs among those fields that it does not interpret are reported as unparsed.",
                   "YAML that Hermes rejects (some tab or flow-indicator placements, for example) can still be read here; Hermes then falls back to its last-known-good configuration."]
    wrapper_unseen = harness == "hermes-astra-ultra" and status == "absent"
    if harness == "hermes-astra-ultra":
        limitations.append("The hermes-astra-ultra wrapper was not executed; it may set its own HERMES_HOME, so this profile is a candidate.")
    if wrapper_unseen:
        warnings.append(f"No Hermes configuration at {label}; the hermes-astra-ultra wrapper may use another installation.")
    if str(env.get("HERMES_SAFE_MODE", "")).strip().lower() in _YAML_TRUE:
        warnings.append("HERMES_SAFE_MODE is set; Hermes loads no MCP servers in safe mode.")
    return _report([_source(label, "profile", status, servers, _hermes_server)], limitations, warnings,
                       incomplete=wrapper_unseen, profile=_label(selected, home), profile_selection=selection)
