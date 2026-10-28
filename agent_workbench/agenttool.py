"""AgentTool next door: refusals, session-file locators and a read-only wiring report.

Nothing here starts a process, opens a Collab journal or uses the network. Agents
use AgentTool through their own harness's MCP tools; Workbench holds no AgentTool
identity. The refusals are a tripwire at Workbench's own launch surface, not a
sandbox or a shell parser: an agent task's own tools stay governed by its harness.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePath
import re
import shutil
import stat
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
# git grep runs the value of -O<cmd>, bundled -nO<cmd> or -5O<cmd>, and --open-files-in-pager=<cmd> (any
# abbreviation from --op) through a shell with the matching paths appended, terminal or not.
_PAGER_OPTION = re.compile(r"-[A-Za-z0-9]*O(?P<short>.*)\Z|--op[^=]*(?:=(?P<long>.*))?\Z", re.S)
# A whole package spec a runner would install and run: @agenttool/lobby, npm:@agenttool/collab-courier@0.1.0.
_SCOPED_PACKAGE = re.compile(r"(?i)(?:npm:)?@agenttool/([a-z0-9][a-z0-9._-]*)(?:@[^/]*)?\Z")
# Options whose value chooses a package or workspace rather than running it.
_SELECTOR_OPTIONS = {"-p", "--package", "--filter", "-F", "-w", "--workspace"}


def _pager_value(word) -> str:
    match = _PAGER_OPTION.match(word)
    return (match.group("short") or match.group("long") or "") if match else ""


def _script_words(text) -> list[list[str]]:
    return [words for words in (_WORD.findall(part) for part in _CONTROL.split(text)) if words]


def _segments(argv) -> list[list[str]]:
    """The top-level words, then every shell-like part of any script argument or pager value."""
    top, scripts = [], []
    for element in argv:
        if pager := _pager_value(element):
            scripts.extend(_script_words(pager))
        if _SCRIPT_CHARS.search(element):
            scripts.extend(_script_words(element))
        else:
            top.append(element)
    return [top, *scripts]


def _leading_assignments(words) -> int:
    lead = 0
    while lead < len(words) and _ASSIGNMENT.match(words[lead]):
        lead += 1
    return lead


def _is_search(words) -> bool:
    """A plain search treats its later words as data; pagers and external diffs can run commands."""
    words = words[_leading_assignments(words):]
    if not words or any(_PAGER_OPTION.match(word) or word.startswith("--ext-diff") for word in words):
        return False
    name = PurePath(words[0]).name
    if name in SEARCH_COMMANDS:
        return True
    # Only `git grep` or `git log` with no global options: `git -c` can set a pager.
    return name == "git" and len(words) > 1 and words[1] in {"grep", "log"}


def _is_whole_search(argv, segments) -> bool:
    """Only argv[0] itself can make the whole command a search, and never with a pager option."""
    return (bool(argv) and segments[0][:1] == [argv[0]] and _is_search(segments[0])
            and not any(_PAGER_OPTION.match(element) for element in argv))


def _stems(word, selector=False) -> list[str]:
    """The command names a word could invoke: itself, a NAME=value or --option=value value, a pager value."""
    candidates = [word]
    if "=" in word:
        candidates.append(word.split("=", 1)[1])
    if pager := _pager_value(word):
        candidates.append(pager)
    if not selector and word.split("=", 1)[0] not in _SELECTOR_OPTIONS:
        candidates += [f"agenttool-{match.group(1)}" for match in map(_SCOPED_PACKAGE.match, candidates[:2]) if match]
    stems = []
    for candidate in candidates:
        stem = PurePath(candidate).name.lower() if candidate else ""
        # A package runner's spec pins a version: collab-zerone@0.3.0, agenttool-lobby@1.0.0.
        if (at := stem.find("@", 1)) > 0:
            stem = stem[:at]
        for extension in _EXTENSIONS:
            if stem.endswith(extension):
                stem = stem[:-len(extension)]
                break
        stems.append(stem)
    return stems


def _segment_reason(words) -> str | None:
    for index, word in enumerate(words):
        for stem in _stems(word, index > 0 and words[index - 1] in _SELECTOR_OPTIONS):
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
    if _is_whole_search(argv, segments):
        return None
    for index, words in enumerate(segments):
        if index and _is_search(words):
            # A search's words are data, but NAME=value in front of it can set a pager or helper.
            words = words[:_leading_assignments(words)]
        if reason := _segment_reason(words):
            return reason
    return None


# --- Collab session credential-file locators ---------------------------------

# Collab writes each session's bearer to <journal dir>/collab-sessions/<id>.json (also JSON-escaped). A host
# may name the file with AGENTOOL_COLLAB_SESSION_FILE (always a Collab name), or with --session-file or a
# session_file field, which count only next to a mention of Collab: session_file is an ordinary identifier.
SESSION_FILE_NAME = re.compile(r"(?i)collab-sessions(?:[/\\]|\\\\|\\/)[A-Za-z0-9_.:-]+\.json(?![A-Za-z0-9_])")
_VALUE = r"(\"[^\"\n]*\"|'[^'\n]*'|(?:\$\{\w+\}|[^\s\"',;}\\]|\\.)+)"
SESSION_FILE_ASSIGNMENT = re.compile(r"(?<!\w)(AGENTOOL_COLLAB_SESSION_FILE[\"']?[ \t]*(?::-|:=|[:=])[ \t]*)" + _VALUE)
SESSION_FILE_OPTION = re.compile(r"(?<![\w-])(--session-file(?:=|[ \t]+)|[\"']?session_file[\"']?[ \t]*[:=][ \t]*)" + _VALUE)
_LOOKS_LIKE_PATH = re.compile(r"[/\\]|\.json(?!\w)")
_BARE_VARIABLE = re.compile(r"\$(?:\w+|\{\w+\})")
_COLLAB_CONTEXT = re.compile(r"(?i)(?<![A-Za-z0-9])(?:agenttool[-_ .]?collab|collab)(?![A-Za-z0-9])")
SESSION_FILE_REFUSAL = ("names a Collab session credential file. Workbench keeps these paths out of prompts, "
                        "commands and run state; refer to the session by its ID and let the agent's own Collab "
                        "tools find it.")
WITHHELD = "[Collab session file withheld]"
_TOKEN_END = " \t\r\n\"'`<>"
_PATH_ROOT = re.compile(r"~?[/\\]|\.{1,2}[/\\]|[A-Za-z]:[/\\]")
# Where a path can begin inside a word: at its start or after = ( [ { , : with file://, ~, $NAME or a drive.
_ROOT_IN_WORD = re.compile(r"(?:^|(?<=[=(\[{,:]))(?:file://)?(?:(?:~|\$\w+|\$\{\w+\})?[/\\]|\.{1,2}[/\\]|[A-Za-z]:[/\\])")


def _is_path_value(value: str) -> bool:
    value = value.strip("\"'")
    return bool(_LOOKS_LIKE_PATH.search(value)) and not _BARE_VARIABLE.fullmatch(value)


def _names_path(pattern, text) -> bool:
    return any(_is_path_value(match.group(2)) for match in pattern.finditer(text))


def names_collab_session_file(text) -> bool:
    """Linear-time detection of a Collab session credential-file path in text."""
    if not isinstance(text, str):
        return False
    if SESSION_FILE_NAME.search(text) or _names_path(SESSION_FILE_ASSIGNMENT, text):
        return True
    # Workbench's own marker says "Collab"; it must not turn an ordinary session_file into a refusal.
    return bool(_COLLAB_CONTEXT.search(text.replace(WITHHELD, ""))) and _names_path(SESSION_FILE_OPTION, text)


def argv_names_collab_session_file(argv) -> bool:
    """Also the two-word form: --session-file followed by a path, for Collab's own host command."""
    if any(names_collab_session_file(element) for element in argv):
        return True
    segments = _segments(argv)
    if _is_whole_search(argv, segments):
        return False  # a search's later words are patterns and paths, never its options
    if not any(_COLLAB_CONTEXT.search(element) for element in argv):
        return False
    host = any("agenttool-collab-host" in _stems(word) for word in segments[0])
    values = [value for flag, value in zip(argv, argv[1:]) if flag == "--session-file"]
    values += [element.split("=", 1)[1] for element in argv if element.startswith("--session-file=")]
    return any(value and not _BARE_VARIABLE.fullmatch(value.strip("\"'")) and (host or _is_path_value(value))
               for value in values)


def _token_start(text, index, floor):
    while index > floor and text[index - 1] not in _TOKEN_END:
        if text[index - 2:index] == "](":
            break  # a Markdown link target starts here
        index -= 1
    return index


def _root_in(text, start, end):
    """The offset where a path begins in text[start:end], or None."""
    found = _ROOT_IN_WORD.search(text[start:end])
    return start + found.start() if found else None


def _withhold_locators(text: str) -> str:
    pieces, last = [], 0
    for match in SESSION_FILE_NAME.finditer(text):
        start = _token_start(text, match.start(), last)
        line = max(last, text.rfind("\n", last, start) + 1)
        close = text[match.end():match.end() + 1]
        if close and close in "\"'`" and start > line and text[start - 1] != close:
            # A quoted path with spaces: withhold from its opening quote when a path begins there.
            opening = text.rfind(close, line, start)
            if opening >= 0 and _ROOT_IN_WORD.match(text[opening + 1:match.start()]):
                start = opening + 1
        root = _root_in(text, start, match.start())
        if root is not None:
            start = root
        elif start < match.start():
            # The tail of a longer path with spaces ("Application Support/...", "My\ Files/..."):
            # walk back word by word on this line, never past the previous match, to where the path begins.
            probe = start
            while probe > line and text[probe - 1] in " \t":
                gap = probe
                while gap > line and text[gap - 1] in " \t":
                    gap -= 1
                previous = _token_start(text, gap, line)
                if previous == gap:
                    break
                root = _root_in(text, previous, gap)
                if root is not None:
                    start = root
                    break
                probe = previous
        pieces += [text[last:start], WITHHELD]
        last = match.end()
    return "".join(pieces + [text[last:]])


def _withhold_value(match) -> str:
    value = match.group(2)
    if not _is_path_value(value):
        return match.group(0)
    quote = value[0] if len(value) > 1 and value[0] in "\"'" and value[-1] == value[0] else ""
    return match.group(1) + quote + WITHHELD + quote


def withhold_session_files(text: str) -> str:
    """Replace session-file paths, a path with spaces whole; broader than detection, so it errs toward hiding."""
    text = _withhold_locators(text)
    for pattern in (SESSION_FILE_ASSIGNMENT, SESSION_FILE_OPTION):
        text = pattern.sub(_withhold_value, text)
    return text


def launch_refusal(task: dict, prompt: str) -> str | None:
    """The refusal for a saved task at launch, so runs saved before these rules are caught too."""
    if task["harness"] == "command":
        argv = task.get("argv", [])
        reason = refused_command(argv) or (SESSION_FILE_REFUSAL if argv_names_collab_session_file(argv) else None)
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
# Each harness's own config keys that carry or produce credentials (Codex snake_case, Claude camelCase).
_CREDENTIAL_KEYS = {"headers", "http_headers", "env_http_headers", "bearer_token_env_var", "bearer_token",
                    "http_headers_helper", "headersHelper", "auth", "oauth"}
_VALUE_FREE_KEYS = {"env", "headers", "http_headers", "env_http_headers", "auth", "oauth"}
_FALSE_WORDS = {"", "null", "~", "false", "no", "off"}
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}\Z")
_COLLAB_ENV = ("AGENTOOL_COLLAB_SESSION_FILE", "AGENTOOL_COLLAB_ALLOW_CURSOR_RECOVERY", "AGENTOOL_COLLAB_CLAUDE_PID",
               "AGENTOOL_COLLAB_DB", "AGENTOOL_COLLAB_ANCHOR_LEDGER")
_MAX_DEPTH = 24
# PyYAML's printable set plus tab and newlines; anything else makes Hermes reject the whole file.
_NOT_YAML_PRINTABLE = re.compile("[^\t\n\r\x20-\x7e\x85\xa0-퟿-�\U00010000-\U0010ffff]|[\x85  ]")
_TOP_KEY = re.compile(r"""["']?mcp_servers["']?\s*:""")
# Top-level lines that cannot hide an mcp_servers key: plain keys, list items, document markers, directives.
_PLAIN_TOP = re.compile(r"(?:[A-Za-z_][\w.-]*\s*:(?:\s|$)|- |-$|---\s*(?:#.*)?$|\.\.\.\s*(?:#.*)?$|%)")
# PyYAML's YAML 1.1 resolvers for plain scalars.
_YAML_NULL = re.compile(r"~|null|Null|NULL")
_YAML_BOOL = re.compile(r"yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF")
_YAML_NUMBER = re.compile(
    r"[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+"
    r"|[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?"
    r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)")


# Escapes PyYAML accepts in double-quoted scalars; any other backslash makes the file invalid.
_BAD_ESCAPE = re.compile(r'\\(?![0abt\tnvfre "/\\N_LP]|x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|\n)')


class _Unparsed(ValueError):
    """YAML the subset cannot read (valid or not): only the server it is in is unknown."""


class _Invalid(_Unparsed):
    """YAML that PyYAML rejects: Hermes then loads no servers from the file at all."""


class _PlainNumber(str):
    """A plain scalar PyYAML resolves to a number: Hermes sees a number, never a name."""


_NO_TOOLS = object()  # Hermes fails a server whose `tools` is not a mapping and registers none of its tools


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


def _flow_list(text: str) -> list:
    """A flow sequence of scalars, such as args: ["-y", "pkg"]; anything nested is outside the subset."""
    items, current, quote = [], "", None
    for char in text[1:-1]:
        if quote:
            current += char
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
            current += char
        elif char == ",":
            items.append(current.strip())
            current = ""
        elif char in "[]{}":
            raise _Unparsed()
        else:
            current += char
    if quote:
        raise _Invalid()
    if current.strip():
        items.append(current.strip())  # a trailing comma, as in [a,], is valid
    if any(not item for item in items):
        raise _Invalid()  # [a,,b] or [,a]
    return [_scalar(item) for item in items]


def _scalar(text: str):
    if text in ("{}", "[]"):
        return {} if text == "{}" else []
    if text[0] == "[" and text.endswith("]"):
        return _flow_list(text)
    if text[0] == '"':
        if not re.fullmatch(r'"(?:[^"\\]|\\.)*"', text):
            raise _Invalid()
        if _BAD_ESCAPE.search(text):
            raise _Invalid()  # an escape YAML does not have, such as \\w or \\q
        try:
            return json.loads(text)
        except ValueError:
            raise _Unparsed() from None  # a YAML-only escape such as \\x41 or \\N
    if text[0] == "'":
        if not re.fullmatch(r"'(?:[^']|'')*'", text):
            raise _Invalid()
        return text[1:-1].replace("''", "'")
    if text[0] in "{[&*!|>":
        raise _Unparsed()
    if text[0] in ",]}%@`" or text == "-" or text.startswith(("- ", "? ")):
        raise _Invalid()
    if ": " in text or text.endswith(":") or " #" in text:
        raise _Invalid()  # PyYAML rejects a mapping indicator inside a plain value
    if _YAML_NULL.fullmatch(text):
        return None
    if _YAML_BOOL.fullmatch(text):
        return text.lower() in ("yes", "true", "on")
    if _YAML_NUMBER.fullmatch(text):
        return _PlainNumber(text)
    return text


def _is_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _yaml_list(items, index, indent, parent=-1):
    values = []
    while index < len(items) and items[index][0] == indent and _is_item(items[index][1]):
        item = items[index][1][1:].strip()
        if not item or ": " in item or item.endswith(":") or _is_item(item):
            raise _Unparsed()  # sequences of mappings, nested sequences or nested blocks
        values.append(_scalar(item))
        index += 1
    if index < len(items) and items[index][0] > indent:
        raise _Unparsed()
    if index < len(items) and parent < items[index][0] < indent:
        raise _Invalid()  # a dedent to a column no open block uses
    return values, index


def _yaml_mapping(items, index, indent, depth=0):
    if depth > _MAX_DEPTH:
        raise _Unparsed()
    result = {}
    while index < len(items):
        level, content = items[index]
        if level < indent:
            break
        if level > indent:
            if result and (": " in content or content.endswith(":")):
                raise _Invalid()  # a continuation holding a mapping indicator, or a mis-indented key
            raise _Unparsed()
        if content.startswith("-"):
            raise _Unparsed()
        match = re.fullmatch(r"([^:]+):(?: +(.*))?", content)
        if not match:
            if result or any(level == indent and re.match(r"[^:]+:(?: |$)", rest_content)
                             for level, rest_content in items[index + 1:]):
                raise _Invalid()  # "key:value" among sibling keys
            raise _Unparsed()  # e.g. a lone "url:https://..." is one plain scalar to PyYAML, not a key
        key, rest = match.group(1), (match.group(2) or "").strip()
        if key[0] in ",]}%@`":
            raise _Invalid()
        if key != key.strip() or key[0] in "?&*!|>'\"{[" or key in result:
            raise _Unparsed()
        index += 1
        if rest:
            value = _scalar(rest)
        elif index < len(items) and items[index][0] > indent:
            if _is_item(items[index][1]):
                value, index = _yaml_list(items, index, items[index][0], parent=indent)
            else:
                value, index = _yaml_mapping(items, index, items[index][0], depth + 1)
            if index < len(items) and indent < items[index][0]:
                raise _Invalid()  # a dedent to a column no open block uses
        elif index < len(items) and items[index][0] == indent and _is_item(items[index][1]):
            value, index = _yaml_list(items, index, indent, parent=indent - 1)
        else:
            value = None
        result[key] = value
    return result, index


def _invalid_elsewhere(lines, start) -> bool:
    """Cheap whole-file checks for errors that make PyYAML, and so Hermes, reject every server."""
    for number, line in enumerate(lines):
        indent = line[:len(line) - len(line.lstrip(" \t"))]
        if "\t" in indent and line.strip():
            return True
        if number != start and line[:1] not in " \t#" and line.strip():
            match = re.match(r"[A-Za-z_][\w.-]*\s*:\s+(.*)$", line)
            value = _strip_comment(match.group(1)).strip() if match else ""
            if value and value[0] not in "\"'{[&*!|>" and (
                    ": " in value or value.endswith(":") or value == "-" or value.startswith("- ")):
                return True
    return False


def _hermes_servers(text: str) -> tuple[dict | None, str]:
    """Block-style subset of Hermes's column-0 ``mcp_servers:`` only; other YAML is unparsed, never absent."""
    text = text.removeprefix("﻿")
    if _NOT_YAML_PRINTABLE.search(text):
        return None, "unparsed"
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    live = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    body = [line for line in live if not re.match(r"(?:---|\.\.\.)\s*(?:#.*)?$|%", line)]
    if body and body[0].lstrip()[:1] in "{[":
        return None, "unparsed"  # a flow-style or JSON document
    starts = [i for i, line in enumerate(lines) if _TOP_KEY.match(line)]
    if not starts:
        certain = (not any("mcp_servers" in line or "\\" in line for line in live)
                   and not (body and body[0][:1] in " \t")  # a document indented as a whole
                   and all(_PLAIN_TOP.match(line) for line in live if line[:1] not in " \t"))
        return ({}, "no_mcp_servers") if certain else (None, "unparsed")
    start = starts[0]
    others = [line for i, line in enumerate(lines) if i != start and line[:1] not in " \t#" and "mcp_servers" in line]
    if others or not re.match(r"mcp_servers:(?:\s+(?:(?:\{\}|null|~)\s*)?(?:#.*)?)?$", lines[start]):
        return None, "unparsed"  # quoted key, anchor, tag, inline value, or a repeated key (PyYAML keeps the last)
    if _invalid_elsewhere(lines, start):
        return None, "unparsed"
    if _strip_comment(lines[start].split(":", 1)[1].strip()):
        return {}, "read"
    items = []
    for line in lines[start + 1:]:
        if line and line[0] not in " \t#":
            break
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
        raw_name, _, rest = content.partition(":")
        name = raw_name.rstrip(" ")
        if not name or name[0] in "?&*!|>%@`{[,]}":
            return None, "unparsed"  # an anchored, tagged or complex key, or a stray indicator
        if name[0] in "'\"":
            try:
                name = _scalar(name)
            except _Unparsed:
                return None, "unparsed"
        if not isinstance(name, str) or not name or name in servers:
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
                raise _Invalid()  # a dedent to a column no open block uses
            for key in _VALUE_FREE_KEYS & entry.keys():
                value = entry[key]
                entry[key] = ({child: None for child in value} if isinstance(value, dict)
                              else bool(value) and str(value).strip().lower() not in _FALSE_WORDS)
            servers[name] = entry
        except _Invalid:
            return None, "unparsed"
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
    """(kind, display endpoint, credentials carried in the URL). A url key means HTTP, as in Hermes."""
    arguments = entry.get("args") if isinstance(entry.get("args"), list) else []
    words = [entry.get("command"), *arguments]
    stems = [_stem_of(word) for word in words]
    local = "collab" if "agenttool-collab-mcp" in stems else "lobby_view" if "agenttool-lobby-mcp" in stems else None
    if "url" in entry:
        url = entry.get("url")
        try:
            parts = urlsplit(url.strip()) if isinstance(url, str) else None
            host = parts.hostname if parts else None
            in_url = bool(parts and (parts.username or parts.password or parts.query))
        except ValueError:
            parts, host, in_url = None, None, False
        usable = parts is not None and parts.scheme in {"http", "https"} and host
        if not usable:
            if (isinstance(url, str) and "${" in url) or any(isinstance(w, str) and "${" in w for w in words):
                return "unresolved", None, False
            mentions = local or (isinstance(url, str) and AGENTTOOL_HOST in url.lower())
            return ("unusable_entry", None, False) if mentions else (None, None, False)
        if host != AGENTTOOL_HOST:
            return ("unresolved" if "${" in url else None), None, False
        path = parts.path.rstrip("/")
        for kind, (endpoint, _) in PUBLIC_ENDPOINTS.items():
            if path == endpoint:
                return kind, f"{parts.scheme}://{AGENTTOOL_HOST}{endpoint}", in_url
        if path.startswith("/v1/mcp/agents/"):
            return "agent_scoped", f"{parts.scheme}://{AGENTTOOL_HOST}/v1/mcp/agents/[agent id withheld]", in_url
        return "unrecognised_agenttool_endpoint", f"{parts.scheme}://{AGENTTOOL_HOST}/[path withheld]", in_url
    if local:
        return local, None, False
    if any(isinstance(word, str) and "${" in word for word in words):
        return "unresolved", None, False
    return None, None, False


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
    return sorted({_name(value) for value in values if isinstance(value, str) and not isinstance(value, _PlainNumber)})


def _presence(path, *, want, follow=True) -> str:
    """present, absent or unknown. Path.exists() and is_file() hide permission errors on Python 3.14."""
    try:
        mode = (os.stat if follow else os.lstat)(path).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except (OSError, ValueError):
        return "unknown"
    return "present" if want(mode) else "absent"


def _collab_package(entry: dict) -> dict:
    from .capabilities import _read, _version
    evidence = "files beside the configured entry, not the running process"
    arguments = entry.get("args") if isinstance(entry.get("args"), list) else []
    for argument in arguments:
        if (isinstance(argument, str) and argument.startswith("/") and "$" not in argument and "\x00" not in argument
                and argument.endswith("dist/agenttool-collab-mcp.js")):
            entry_file = Path(argument)
            package, status = _read(entry_file.parent.parent / "package.json")
            version = (_version(package.get("version"))
                       if status == "read" and package.get("name") == "@agenttool/collab" else None)
            host_cli = _presence(entry_file.parent / "agenttool-collab-host.js", want=stat.S_ISREG, follow=False)
            return {"package_version": version, "host_cli": host_cli, "evidence": evidence}
    return {"package_version": None, "host_cli": "unknown", "evidence": evidence}


def _enabled(value) -> bool:
    """Hermes's _parse_boolish with default True: only an explicit no turns a server off."""
    if value is None or isinstance(value, _PlainNumber):
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off"}
    return True


def _server_row(name, entry, source, scope, tool_filter):
    from .capabilities import _name
    kind, endpoint, credentials_in_url = _classify(entry)
    if kind is None or kind == "unresolved":
        return kind
    environment = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    env_names = [key for key in environment if isinstance(key, str) and _ENV_NAME.fullmatch(key)]
    if isinstance(entry.get("env_vars"), list):
        env_names += [key for key in entry["env_vars"] if isinstance(key, str) and _ENV_NAME.fullmatch(key)]
    expected = PUBLIC_ENDPOINTS[kind][1] if kind in PUBLIC_ENDPOINTS else None
    if tool_filter is _NO_TOOLS:
        tools = _tool_fit(set(), expected) if expected else {**_tool_fit(None, None), "fit": "none_registered"}
    else:
        allow, deny = tool_filter if tool_filter else (None, set())
        configured = None if allow is None and not deny else (set(allow) if allow is not None else set(expected or ())) - deny
        tools = _tool_fit(configured if expected else None, expected)
    row = {"name": _name(name), "source": source, "scope": scope, "kind": kind,
           "transport": "http" if "url" in entry else "stdio" if entry.get("command") else "unknown",
           "enabled": _enabled(entry.get("enabled", True)), "tools": tools,
           "environment_names": sorted(set(env_names)),
           "credentials_configured": credentials_in_url or any(
               bool(entry.get(key)) and str(entry.get(key)).strip().lower() not in _FALSE_WORDS for key in _CREDENTIAL_KEYS),
           "warnings": []}
    if endpoint:
        row["endpoint"] = endpoint
    elif kind in {"collab", "lobby_view"}:
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
    if tool_filter is _NO_TOOLS:
        row["warnings"].append({"code": "tools_not_mapping", "message":
                                "tools is not a mapping (include:/exclude:); Hermes fails this server and registers none of its tools"})
    if kind == "unusable_entry":
        row["warnings"].append({"code": "unusable_url", "message":
                                "has a url key the harness will use, but it is not an http(s) URL; this server will not start"})
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


def _collect(rows, taken, servers, source, scope, filter_of) -> tuple[int, int]:
    """Append AgentTool rows; return (other servers, servers whose ${...} placeholders hide what they are).

    ``taken`` gathers every server name, only so a suggestion never reuses one;
    it never enters the report. Other servers are never named.
    """
    other = unresolved = 0
    taken.update(name for name in servers if isinstance(name, str))
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        row = _server_row(name, entry, source, scope, filter_of(entry))
        if row is None:
            other += 1
        elif row == "unresolved":
            unresolved += 1
        else:
            rows.append(row)
    return other, unresolved


def _codex_filter(entry):
    """Codex: an enabled_tools list allows; disabled_tools always removes (codex-mcp ToolFilter)."""
    enabled, disabled = entry.get("enabled_tools"), entry.get("disabled_tools")
    return (set(_names(enabled)) if isinstance(enabled, list) else None,
            set(_names(disabled)) if isinstance(disabled, list) else set())


def _yaml_falsy(value) -> bool:
    if isinstance(value, _PlainNumber):
        try:
            return float(value.replace("_", "")) == 0
        except ValueError:
            return False
    return not value


def _hermes_filter(entry):
    """Hermes: `tools or {}`; a non-empty include wins and exclude is ignored; a string is one name."""
    tools = entry.get("tools")
    if not isinstance(tools, dict):
        if not _yaml_falsy(tools):
            return _NO_TOOLS
        tools = {}

    def names(value):
        # Hermes's _normalize_name_filter: a string is one name; a list keeps str() of every item.
        if isinstance(value, str) and not isinstance(value, _PlainNumber):
            return set(_names([value]))
        if isinstance(value, list):
            return set(_names(["None" if item is None else str(item) for item in value]))
        return set()

    include, exclude = names(tools.get("include")), names(tools.get("exclude"))
    return (include, set()) if include else (None, exclude)


def _summary(rows, sources, unparsed):
    unknown = bool(unparsed) or any(source["status"] not in ("read", "absent") or source.get("parse") == "unparsed"
                                    for source in sources)
    summary = {}
    for surface in SURFACES:
        found = [row for row in rows if row["kind"] == surface]
        enabled = [row for row in found if row["enabled"]]
        if enabled:
            summary[surface] = "configured" if any(_complete(row) for row in enabled) else "partial"
        elif found:
            summary[surface] = "disabled"
        else:
            summary[surface] = "unknown" if unknown else "not_configured"
    return summary


def _complete(row) -> bool:
    return row["tools"]["fit"] in {"exact", "superset", "unrestricted", "not_compared"}


def _harness(rows, sources, other, unparsed=0, unresolved=0, **extra):
    return {"sources": sources, "servers": rows, "summary": _summary(rows, sources, unparsed + unresolved),
            "other_server_count": other, "unparsed_server_count": unparsed, "unresolved_server_count": unresolved,
            **extra}


def _codex_section(home, project, env):
    from .capabilities import _read, _root
    root = _root(env.get("CODEX_HOME"), home / ".codex", home)
    candidates = [(root / "config.toml", "$CODEX_HOME/config.toml" if env.get("CODEX_HOME") else "~/.codex/config.toml", "user")]
    if project is not None:
        candidates.append((project / ".codex/config.toml", "$PROJECT/.codex/config.toml", "project"))
    rows, sources, other, unresolved, seen, taken = [], [], 0, 0, set(), set()
    for path, label, scope in candidates:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError, ValueError):
            resolved = path
        if resolved in seen:
            continue  # --project at the home folder names the user file twice
        seen.add(resolved)
        data, status = _read(path, toml=True)
        sources.append({"source": label, "status": status, "reader": "toml"})
        servers = data.get("mcp_servers") if data else None
        if isinstance(servers, dict):
            counts = _collect(rows, taken, servers, label, scope, _codex_filter)
            other, unresolved = other + counts[0], unresolved + counts[1]
    return _harness(rows, sources, other, unresolved=unresolved), taken


def _claude_section(home, project, env):
    from .capabilities import _read, _root
    root = _root(env.get("CLAUDE_CONFIG_DIR"), home, home)
    label = "$CLAUDE_CONFIG_DIR/.claude.json" if env.get("CLAUDE_CONFIG_DIR") else "~/.claude.json"
    rows, sources, other, unresolved, taken = [], [], 0, 0, set()
    data, status = _read(root / ".claude.json")
    sources.append({"source": label, "status": status, "reader": "json", "kept": "mcpServers objects only"})
    scoped = []
    if data:
        scoped.append((data.get("mcpServers"), label, "user"))
        projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
        if project is not None and isinstance(projects.get(str(project.resolve())), dict):
            scoped.append((projects[str(project.resolve())].get("mcpServers"), label, "local"))
        data = None  # Account state is never kept past this point.
    if project is not None:
        shared, status = _read(project / ".mcp.json")
        sources.append({"source": "$PROJECT/.mcp.json", "status": status, "reader": "json"})
        if shared:
            scoped.append((shared.get("mcpServers"), "$PROJECT/.mcp.json", "project"))
    for servers, source, scope in scoped:
        if isinstance(servers, dict):
            counts = _collect(rows, taken, servers, source, scope, lambda entry: None)
            other, unresolved = other + counts[0], unresolved + counts[1]
    return _harness(rows, sources, other, unresolved=unresolved), taken


def _hermes_section(path, label, active_profile=None, managed=None):
    from .capabilities import _read_bytes
    raw, status = _read_bytes(path)
    source = {"source": label, "status": status, "reader": "yaml-subset"}
    sources = [source]
    rows, other, unparsed, unresolved, taken = [], 0, 0, 0, set()
    if raw is not None:
        try:
            servers, parse = _hermes_servers(raw.decode("utf-8"))
        except UnicodeError:
            servers, parse = None, "unparsed"
        source["parse"] = parse
        if servers:
            unparsed = sum(entry is None for entry in servers.values())
            other, unresolved = _collect(rows, taken, servers, label, "profile", _hermes_filter)
    if managed:
        # Hermes merges its managed scope over the user file; it is not read here, so absence is unknown.
        sources.append({"source": managed, "status": "not_read", "reader": "none"})
    extra = {"active_profile_file": active_profile} if active_profile is not None else {}
    return _harness(rows, sources, other, unparsed, unresolved, **extra), taken


def _safe_section(label, build):
    """A section that cannot be read is unknown; it never stops the rest of doctor."""
    try:
        return build()
    except (OSError, RuntimeError, ValueError, RecursionError):
        return _harness([], [{"source": label, "status": "unreadable"}], 0), set()


def _managed_hermes_dir(env):
    override = (env.get("HERMES_MANAGED_DIR") or "").strip()
    path = Path(override) if override else Path("/etc/hermes")
    if _presence(path, want=stat.S_ISDIR) == "absent":  # unknown counts as there: absence cannot be claimed
        return None
    return "$HERMES_MANAGED_DIR/config.yaml" if override else "/etc/hermes/config.yaml"


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
    active = _presence(hermes_home / "active_profile", want=lambda mode: True)
    managed = _managed_hermes_dir(env)
    sections = {
        "codex": _safe_section("~/.codex/config.toml", lambda: _codex_section(home, project, env)),
        "claude": _safe_section("~/.claude.json", lambda: _claude_section(home, project, env)),
        "hermes": _safe_section(hermes_label, lambda: _hermes_section(
            hermes_home / "config.yaml", hermes_label, active_profile=active, managed=managed)),
        "hermes-astra-ultra": _safe_section("~/.hermes/profiles/astra-ultra/config.yaml", lambda: _hermes_section(
            home / ".hermes/profiles/astra-ultra/config.yaml", "~/.hermes/profiles/astra-ultra/config.yaml",
            managed=managed)),
    }
    harnesses = {harness: section for harness, (section, _) in sections.items()}
    taken = {harness: names for harness, (_, names) in sections.items()}
    limitations = [
        "Configured, not observed: a running server may differ from its files.",
        "~/.claude.json holds account state as well as MCP servers; it is parsed in memory and only mcpServers objects are kept.",
        "Claude managed or remote MCP, plugin-provided servers, project-server approval, --mcp-config and "
        "--strict-mcp-config are not evaluated.",
        "Hermes config.yaml is read with a block-style subset reader of the mcp_servers block plus a few common "
        "whole-file errors; a YAML error elsewhere, or in a server counted unparsed, makes Hermes load no MCP "
        "servers at all, which may go unnoticed here.",
        "Hermes's managed scope ($HERMES_MANAGED_DIR or /etc/hermes) can add servers and is not read; when it exists, "
        "Hermes surfaces read unknown. A HERMES_MANAGED_DIR set only in Hermes's own launch environment is not seen.",
        "Servers whose url or command uses ${...} placeholders are counted as unresolved; their surfaces read unknown.",
        "Whether Claude Code accepts the dotted tool names of /v1/mcp was not verified; check /mcp in a new session.",
    ]
    if active == "present":
        limitations.append("~/.hermes/active_profile exists and may select another profile; that selection is not resolved.")
    elif active == "unknown":
        limitations.append("Whether ~/.hermes/active_profile exists could not be checked.")
    try:
        commands = {name: "present" if shutil.which(name, path=env.get("PATH", "")) else "absent"
                    for name in LOCAL_COMMANDS}
    except (OSError, ValueError):
        commands = {name: "unknown" for name in LOCAL_COMMANDS}
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
        "commands": commands,
        "suggestions": _suggestions(harnesses, taken),
        "limitations": limitations,
    }


def _surface_text(surface, state, rows):
    if state in {"configured", "partial"} and rows:
        row = next((row for row in rows if _complete(row)), rows[0])  # the same row the summary trusts
        if surface == "collab":
            collab = row.get("collab", {})
            host = {"present": "host command present", "absent": "host command absent"}.get(collab.get("host_cli"),
                                                                                             "host command unknown")
            registered = "" if _complete(row) else ", no tools registered"
            return f"Collab {collab.get('package_version') or 'version unknown'} ({host}{registered})"
        if surface == "lobby_view":
            return "lobby view configured" if _complete(row) else "lobby view configured, no tools registered"
        tools = row["tools"]
        if tools["fit"] == "unrestricted":
            return f"{_LABELS[surface]} all tools (no tool filter)"
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
        for key, label in (("other_server_count", "other server"), ("unparsed_server_count", "unparsed"),
                           ("unresolved_server_count", "unresolved (${...} placeholder)")):
            if count := data.get(key):
                parts.append(f"{count} {label}{'s' if key == 'other_server_count' and count != 1 else ''}")
        unread = [source["source"] for source in data.get("sources", []) if source["status"] not in ("read", "absent")]
        if unread:
            parts.append("not read: " + ", ".join(unread))
        if any(source.get("parse") == "partial" for source in data.get("sources", [])):
            parts.append("file only partly read: if the unread part is invalid YAML, Hermes loads no servers")
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
