"""AgentTool next door: refusals, session-file locators and a read-only wiring report.

Nothing here starts a process, opens a Collab journal or uses the network. Agents
use AgentTool through their own harness's MCP tools; Workbench holds no AgentTool
identity. The refusals are a tripwire at Workbench's own launch surface, not a
sandbox or a shell parser: an agent task's own tools stay governed by its harness.
"""
from __future__ import annotations

from pathlib import PurePath
import re


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
