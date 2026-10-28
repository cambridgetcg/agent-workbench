"""Read-only attribution of agenttool collab sessions to Workbench attempts.

Workbench is not a collab client. It starts no MCP server or host command,
ends no session and opens no session credential file. Children reach collab
through their own harness registrations; this module reads the journal that
registration names, never collab's default path (on some machines a deliberate
non-SQLite placeholder), in one short read-only transaction.

Attribution is computed live and written nowhere. A worktree attempt owns its
checkout root, which the journal records for every session, so sessions match
by that root and time window; the label suffix is reported, not required, so
sessions from before the board note still attribute. Shared and read-only
attempts share a root, so their sessions match only by an actor label ending
with the attempt ID, within the window. Labels are self-declared routing hints,
not authentication.

Selected columns only: never token_hash, cursor columns or actor text in any
output. A read-only open can still create empty mode-0600 -wal/-shm sidecars
beside a WAL journal when none exist; it never uses immutable=1, which would
miss rows still in the write-ahead log.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tomllib

from .store import COLLAB_SESSION_ID


SESSION_ID = COLLAB_SESSION_ID
APPLICATION_ID = 1096041292  # 0x4154434C, collab's journal identity
SQLITE_HEADER = b"SQLite format 3\x00"
LABEL_SUFFIX = " · workbench job "
SESSION_LIMIT = 20
ROW_LIMIT = 100_000
WINDOW_GRACE = timedelta(seconds=5)
JOURNAL_COLUMNS = {
    "coordination_sessions": ("id", "workspace_id", "worktree_id", "actor", "parent_session_id", "status",
                              "joined_at", "last_seen_at", "ended_at"),
    "worktrees": ("id", "root_path"),
    "workspaces": ("id", "repository_key"),
}
# Collab 0.5.1's session-fit tables: any main build that opens a journal adds them.
NEWER_TABLES = ("repository_aliases", "repository_discovery", "session_lease_lapses")
COLLAB_PROGRAM = "agenttool-collab-mcp"
LOBBY_NAME = "agenttool-lobby"
HARNESS_HOSTS = {"claude": "claude", "astra": "claude", "astra-ultra": "claude", "codex": "codex",
                 "hermes": "hermes", "hermes-astra-ultra": "hermes"}
_CONFIG_LIMIT = 2 * 1024 * 1024
_SMALL_LIMIT = 262_144
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_HERMES_PROFILE = re.compile(r"(?:default|[a-z0-9][a-z0-9_-]{0,63})\Z")  # Hermes's PROFILE_ID_RE
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_STATUS = re.compile(r"[a-z_]{1,32}\Z")
_ATTEMPT_ID = re.compile(r"[a-f0-9]{32}\Z")
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_VERSION = re.compile(r"\d{1,4}\.\d{1,4}\.\d{1,4}(?:[-+][0-9A-Za-z.+-]{1,40})?\Z")
_COMMIT = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
_YAML_INDICATORS = ("&", "*", "!", "|", ">", "{", "[", "%", "@", "`")
LIMITATIONS = ["Registrations are read from host configuration files; a running process may have started "
               "with different configuration or environment.",
               "No MCP server, host CLI or network service is started or contacted.",
               "Claude Code project-scoped and plugin-provided MCP registrations are not read."]


def _home_env(home, env):
    environment = dict(os.environ if env is None else env)
    home = Path(home) if home is not None else Path(environment.get("HOME") or Path.home())
    return home, environment


def _configured(value, default, home):
    from .doctor import _configured_path
    return _configured_path(value, default, home)


def _read_bounded(path, limit=_CONFIG_LIMIT):
    """Bytes of a bounded regular file, for host configuration and metadata.

    A final symbolic link is followed, as the hosts and doctor's own reads
    follow dotfile-manager links; only the journal refuses links.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "unreadable"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "not_regular"
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
    except OSError:
        return None, "unreadable"
    finally:
        os.close(fd)
    return (None, "too_large") if len(data) > limit else (data, "read")


def _yaml_scalar(text):
    """A plain or quoted YAML scalar, or raise ValueError for anything richer."""
    text = text.strip()
    if text.startswith('"'):
        index = 1
        while index < len(text) and text[index] != '"':
            index += 2 if text[index] == "\\" else 1
        rest = text[index + 1:].strip()
        if index >= len(text) or rest and not rest.startswith("#"):
            raise ValueError("unparsed")
        value = json.loads(text[:index + 1])  # YAML-only escapes fail here and stay unparsed
        if not isinstance(value, str):
            raise ValueError("unparsed")
        return value
    if text.startswith("'"):
        index = 1
        while True:
            index = text.find("'", index)
            if index == -1:
                raise ValueError("unparsed")
            if text[index + 1:index + 2] != "'":
                break
            index += 2
        rest = text[index + 1:].strip()
        if rest and not rest.startswith("#"):
            raise ValueError("unparsed")
        return text[1:index].replace("''", "'")
    if text.startswith(_YAML_INDICATORS) or text.startswith(("- ", "? ", ": ")) or text in {"-", "?"}:
        raise ValueError("unparsed")
    comment = text.find(" #")
    value = "" if text.startswith("#") else (text[:comment] if comment != -1 else text).strip()
    if ": " in value or value.endswith(":"):
        raise ValueError("unparsed")  # a mapping, not a scalar
    return None if value in {"", "~", "null", "Null", "NULL"} else value


def _yaml_key(line):
    """Split ``key: rest`` into the key and its unparsed remainder."""
    if line.startswith(("'", '"')):
        quote = line[0]
        end = line.find(quote, 1)
        if end == -1 or line[end + 1:end + 2] != ":":
            raise ValueError("unparsed")
        return line[1:end], line[end + 2:]
    if line.startswith(_YAML_INDICATORS) or line.startswith(("- ", "? ", "<<")) or line == "-":
        raise ValueError("unparsed")
    match = re.match(r"([^\s:#][^:#]*?)\s*:(?:\s+|\Z)", line)
    if not match:
        raise ValueError("unparsed")
    return match.group(1), line[match.end():]


def _yaml_lines(block):
    """(indent, content) for meaningful lines; None when indentation uses tabs."""
    result = []
    for raw in block:
        content = raw.lstrip(" ")
        if not content.strip() or content.startswith("#"):
            continue
        if content[0] == "\t":
            return None
        result.append((len(raw) - len(content), content.rstrip()))
    return result


def hermes_mcp_servers(text):
    """Scan only a Hermes config's column-0 ``mcp_servers:`` block.

    No YAML parser runs, and no line outside that block is interpreted, so
    ``providers`` (which holds key_cmd and account headers) is never read into
    any structure. Flow style, anchors, aliases, tags, merge keys and tabs are
    reported as ``unparsed`` rather than guessed: for the whole block when they
    appear at its top, otherwise for the server they appear in. Only each
    server's ``command``, ``args`` (a block list) and ``env`` (a block mapping)
    are kept.
    """
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if re.match(r"mcp_servers\s*:", line)]
    if not starts:
        return "no_mcp_servers", {}
    if len(starts) > 1:
        return "unparsed", {}
    remainder = lines[starts[0]].split(":", 1)[1].strip()
    if remainder and not remainder.startswith("#"):
        return "unparsed", {}
    block = []
    for line in lines[starts[0] + 1:]:
        if line and not line[0].isspace() and not line.startswith("#"):
            break  # the next column-0 key ends the block
        block.append(line)
    entries = _yaml_lines(block)
    if entries is None:
        return "unparsed", {}
    servers = {}
    if not entries:
        return "read", servers
    server_indent = entries[0][0]
    index = 0
    while index < len(entries):
        indent, content = entries[index]
        if indent != server_indent:
            return "unparsed", {}
        try:
            name, rest = _yaml_key(content)
        except ValueError:
            return "unparsed", {}
        body = []
        index += 1
        while index < len(entries) and entries[index][0] > server_indent:
            body.append(entries[index])
            index += 1
        server = {"command": None, "args": None, "env": None, "unparsed": name in servers}
        servers[name] = server
        if rest.strip() and not rest.strip().startswith("#"):
            server["unparsed"] = True
            continue
        try:
            _hermes_server(body, server)
        except ValueError:
            server.update(command=None, args=None, env=None, unparsed=True)
    return "read", servers


def _hermes_server(body, server):
    if not body:
        return
    key_indent = body[0][0]
    index = 0
    while index < len(body):
        indent, content = body[index]
        if indent != key_indent:
            raise ValueError("unparsed")
        key, rest = _yaml_key(content)
        value = rest.strip()
        index += 1
        nested = []
        # A block list may sit at its key's own indentation.
        while index < len(body) and (body[index][0] > key_indent
                                     or key == "args" and body[index][0] == key_indent and body[index][1].startswith("- ")):
            nested.append(body[index])
            index += 1
        if key == "<<" or value.startswith(("&", "*", "!")):
            raise ValueError("unparsed")
        if key == "command":
            if nested:
                raise ValueError("unparsed")
            server["command"] = _yaml_scalar(rest)
        elif key in {"args", "env"}:
            if value and not value.startswith("#") or server[key] is not None:
                raise ValueError("unparsed")  # flow style, an inline scalar or a repeated key
            item_indent = nested[0][0] if nested else None
            server[key] = [] if key == "args" else {}
            for item, line in nested:
                if item != item_indent:
                    raise ValueError("unparsed")  # nested structure or a continued scalar
                if key == "args":
                    if not line.startswith("- "):
                        raise ValueError("unparsed")
                    server["args"].append(_yaml_scalar(line[2:]))
                else:
                    name, remainder = _yaml_key(line)
                    if name in server["env"]:
                        raise ValueError("unparsed")
                    server["env"][name] = _yaml_scalar(remainder)


def _host_config(harness, home, env):
    """Where the harness's own MCP registrations live, with a path-free label."""
    host = HARNESS_HOSTS.get(harness)
    if host == "claude":
        if harness == "claude" and env.get("CLAUDE_CONFIG_DIR"):
            return {"host": host, "path": _configured(env["CLAUDE_CONFIG_DIR"], home / ".claude", home) / ".claude.json",
                    "label": "$CLAUDE_CONFIG_DIR/.claude.json"}
        return {"host": host, "path": home / ".claude.json", "label": "~/.claude.json"}
    if host == "codex":
        root = _configured(env.get("CODEX_HOME"), home / ".codex", home)
        return {"host": host, "path": root / "config.toml",
                "label": ("$CODEX_HOME" if env.get("CODEX_HOME") else "~/.codex") + "/config.toml"}
    if host == "hermes":
        # Hermes's profile mode exports HERMES_HOME=<root>/profiles/<name> and
        # its terminal children inherit it. As Hermes does, take the root from
        # two levels up and trust that directory as the active profile.
        selected = _configured(env.get("HERMES_HOME"), home / ".hermes", home)
        in_profile = bool(env.get("HERMES_HOME")) and selected.parent.name == "profiles"
        root = selected.parent.parent if in_profile else selected
        base = ("~/.hermes" if root == home / ".hermes" else "$HERMES_HOME/../.." if in_profile else "$HERMES_HOME")
        if harness == "hermes-astra-ultra":
            profile = "astra-ultra"
        elif in_profile:
            return {"host": host, "path": selected / "config.yaml", "label": "$HERMES_HOME/config.yaml",
                    "profile": selected.name}
        else:
            data, status = _read_bounded(root / "active_profile", 256)
            try:
                profile = "default" if status == "absent" else data.decode("utf-8-sig").strip() if data is not None else None
            except UnicodeDecodeError:
                profile = None
            # Hermes's reading: empty or any case of "default" is the root, other names are lowercased.
            if profile is not None:
                profile = "default" if not profile or profile.casefold() == "default" else profile.lower()
            if profile is None or not _HERMES_PROFILE.fullmatch(profile):
                return {"host": host, "path": None, "label": base + "/active_profile", "profile": None,
                        "failure": "hermes_profile_invalid"}
        if profile == "default":
            return {"host": host, "path": root / "config.yaml", "label": base + "/config.yaml", "profile": profile}
        return {"host": host, "path": root / "profiles" / profile / "config.yaml",
                "label": f"{base}/profiles/{profile}/config.yaml", "profile": profile}
    return None


def _programs(entry):
    values = [entry.get("command"), *(entry.get("args") or [])]
    return [value for value in values if isinstance(value, str)]


def _kind(host, name, entry):
    names = [os.path.basename(value) for value in _programs(entry)]
    # Collab's README registers Hermes's collab server under the exact name "agenttool".
    if any(base.startswith(COLLAB_PROGRAM) for base in names) or host == "hermes" and name == "agenttool":
        return "collab"
    if name == LOBBY_NAME or any(base.startswith(LOBBY_NAME) and not base.startswith(LOBBY_NAME + "-doorbell")
                                 for base in names):
        return "lobby"
    return None


def registrations(harness, *, home=None, env=None):
    """The harness's collab and lobby MCP registrations, private values included.

    The result keeps command, args and env values in memory for the journal and
    version checks; callers must expose only names, key names and booleans.
    """
    home, env = _home_env(home, env)
    config = _host_config(harness, home, env)
    if config is None:
        return {"harness": harness, "config": None, "config_status": "not_applicable", "entries": []}
    result = {"harness": harness, "config": config["label"], "profile": config.get("profile"), "entries": [],
              "unclassified_unparsed": 0}
    if config.get("failure"):
        result["config_status"] = config["failure"]
        return result
    result["source"] = os.path.realpath(config["path"])  # private: which configuration file this is
    data, status = _read_bounded(config["path"])
    result["config_status"] = status
    if data is None:
        return result
    try:
        text = data.decode("utf-8")
        if config["host"] == "hermes":
            status, servers = hermes_mcp_servers(text)
            result["config_status"] = "read" if status == "no_mcp_servers" else status
        else:
            parsed = json.loads(text) if config["host"] == "claude" else tomllib.loads(text)
            servers = parsed.get("mcpServers" if config["host"] == "claude" else "mcp_servers") if isinstance(parsed, dict) else None
            servers = servers if isinstance(servers, dict) else {}
    except (ValueError, UnicodeError):
        result["config_status"] = "malformed"
        return result
    for name, spec in list(servers.items())[:200]:
        if not isinstance(name, str) or not isinstance(spec, dict):
            continue
        entry = {"name": name, "command": spec.get("command") if isinstance(spec.get("command"), str) else None,
                 "args": [item for item in spec["args"] if isinstance(item, str)] if isinstance(spec.get("args"), list) else None,
                 "env": {key: value for key, value in spec["env"].items() if isinstance(key, str)}
                        if isinstance(spec.get("env"), dict) else None,
                 "unparsed": bool(spec.get("unparsed"))}
        kind = _kind(config["host"], name, entry)
        if kind is not None:
            entry["kind"] = kind
            result["entries"].append(entry)
        elif entry["unparsed"]:
            # Its command and args were not kept, so it may be a collab
            # registration under another name: unknown, not absent.
            result["unclassified_unparsed"] += 1
    return result


def resolve_journal(harness, *, home=None, env=None):
    """Select the collab journal a harness's registration names, or a failure code.

    Several collab registrations in one host configuration must agree, and
    Workbench's own AGENTOOL_COLLAB_DB, when set, must agree with them: any
    disagreement is ``journal_ambiguous``. A registration without a journal
    location would use collab's default, which is never read. ``path`` is
    private; never include it in output.
    """
    home, env = _home_env(home, env)
    found = registrations(harness, home=home, env=env)
    result = {"harness": harness, "config": found["config"], "source": found.get("source"), "failure": None,
              "path": None, "real": None}
    if found["config_status"] not in {"read", "absent"}:
        result["failure"] = {"hermes_profile_invalid": "hermes_profile_invalid",
                             "unparsed": "registration_unparsed"}.get(found["config_status"], "registration_unreadable")
        return result
    entries = [entry for entry in found["entries"] if entry["kind"] == "collab"]
    if not entries:
        result["failure"] = "registration_unparsed" if found.get("unclassified_unparsed") else "collab_not_registered"
        return result
    if any(entry["unparsed"] for entry in entries):
        result["failure"] = "registration_unparsed"
        return result
    values = [(entry["env"] or {}).get("AGENTOOL_COLLAB_DB") for entry in entries]
    own = env.get("AGENTOOL_COLLAB_DB")
    if all(value is None for value in values):
        result["failure"] = "journal_ambiguous" if own else "journal_unconfigured"
        return result
    if any(not isinstance(value, str) for value in values):
        result["failure"] = "journal_ambiguous"
        return result
    if any(not os.path.isabs(value) for value in values) or own and not os.path.isabs(own):
        result["failure"] = "journal_path_not_absolute"
        return result
    reals = {os.path.realpath(value) for value in values}
    if len(reals) != 1 or own and os.path.realpath(own) not in reals:
        result["failure"] = "journal_ambiguous"
        return result
    result.update(path=values[0], real=reals.pop())
    return result


def _journal_file(path):
    """Check the registered path is a regular SQLite file without following links."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return "journal_not_regular"
        fd = os.open(os.path.realpath(path), os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return "journal_missing"
    except OSError:
        return "journal_unreadable"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "journal_not_regular"
        header = os.read(fd, len(SQLITE_HEADER))
    except OSError:
        return "journal_unreadable"
    finally:
        os.close(fd)
    return None if header == SQLITE_HEADER else "journal_not_sqlite"


def read_journal(path, reader=None):
    """Open a collab journal read-only, check its identity and run ``reader``.

    Returns ``{failure, user_version, newer_tables, result}``. One short read
    transaction covers the identity checks and the reader, then the connection
    closes. ``reader`` receives the connection and must select named columns.
    """
    outcome = {"failure": None, "user_version": None, "newer_tables": None, "result": None}
    failure = _journal_file(path)
    if failure:
        outcome["failure"] = failure
        return outcome
    connection = None
    try:
        connection = sqlite3.connect(Path(os.path.realpath(path)).as_uri() + "?mode=ro", uri=True, timeout=2,
                                     isolation_level=None)
        connection.execute("PRAGMA query_only=1")
        connection.execute("BEGIN")
        try:
            if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                outcome["failure"] = "journal_not_collab"
                return outcome
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            outcome["user_version"] = version if type(version) is int else None
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            outcome["newer_tables"] = any(table in tables for table in NEWER_TABLES)
            for table, columns in JOURNAL_COLUMNS.items():
                present = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not set(columns) <= present:
                    outcome["failure"] = "journal_schema_unknown"
                    return outcome
            if reader is not None:
                outcome["result"] = reader(connection)
        finally:
            connection.execute("COMMIT")
    except _JournalTooLarge:
        outcome["failure"] = "journal_too_large"
    except sqlite3.Error:
        outcome["failure"] = "journal_unreadable"
    finally:
        if connection is not None:
            connection.close()
    return outcome


class _JournalTooLarge(Exception):
    pass


def _session_rows(connection):
    cursor = connection.execute(
        "SELECT s.id, s.actor, s.parent_session_id, s.status, s.joined_at, s.last_seen_at, s.ended_at, w.root_path "
        "FROM coordination_sessions AS s LEFT JOIN worktrees AS w ON w.id = s.worktree_id")
    rows = []
    while batch := cursor.fetchmany(1000):
        rows.extend(batch)
        if len(rows) > ROW_LIMIT:
            raise _JournalTooLarge()
    keys = ("id", "actor", "parent_session_id", "status", "joined_at", "last_seen_at", "ended_at", "root_path")
    return [dict(zip(keys, row)) for row in rows]


def parse_time(value):
    """An aware datetime from collab's ``...Z`` or Workbench's ``+00:00`` ISO text."""
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _time_text(value):
    return value if parse_time(value) is not None else None


def repository_key(project):
    """Collab's local repository key for a project, or None when Git cannot say.

    Collab keys a workspace as ``local-git:`` plus the SHA-256 of the realpath
    of the repository's Git common directory; worktrees share it.
    """
    from .workspaces import GitWorkspaceError, _git
    try:
        common = _git(project, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except GitWorkspaceError:
        return None
    if not common or not os.path.isabs(common):
        return None
    return "local-git:" + hashlib.sha256(os.path.realpath(common).encode()).hexdigest()


def check_parent(harnesses, project, parent_session_id, *, home=None, env=None):
    """Check a declared parent at create time; return ``matched`` or ``unavailable``.

    Read-only. Where a run harness's journal is readable, the parent must exist
    there and belong to this project's repository workspace, or collab would
    refuse it; a definite miss raises ``collab_parent_not_found`` or
    ``collab_parent_other_repository``. When no journal can be read, or a
    repository cannot be compared, the parent is accepted as ``unavailable``.
    """
    if not isinstance(parent_session_id, str) or not SESSION_ID.fullmatch(parent_session_id):
        raise ValueError("Collab parent must be a collab session ID (session_ followed by a lowercase UUID).")
    home, env = _home_env(home, env)
    key = repository_key(project)
    journals = {}
    for harness in sorted(set(harnesses) & set(HARNESS_HOSTS)):
        resolved = resolve_journal(harness, home=home, env=env)
        journals.setdefault(resolved["real"], resolved)
    if not journals:
        return "unavailable"

    def parent(connection):
        return connection.execute(
            "SELECT s.id, w.repository_key FROM coordination_sessions AS s "
            "LEFT JOIN workspaces AS w ON w.id = s.workspace_id WHERE s.id = ?", (parent_session_id,)).fetchone()

    verified = True
    other = False
    for real, resolved in journals.items():
        if real is None:
            verified = False
            continue
        outcome = read_journal(resolved["path"], parent)
        if outcome["failure"]:
            verified = False
            continue
        row = outcome["result"]
        if row is None:
            raise ValueError("collab_parent_not_found: the declared parent session is not in the collab journal "
                             "this run's harnesses use, so collab would refuse it (session_not_found).")
        found = row[1]
        if (key is None or not isinstance(found, str) or not found.startswith("local-git:")):
            verified = False
        elif found != key:
            other = True
    if other:
        raise ValueError("collab_parent_other_repository: the declared parent session belongs to another "
                         "repository's collab workspace, so collab would refuse it (parent_session_mismatch).")
    return "matched" if verified else "unavailable"


def coordination_view(state):
    coordination = state.get("coordination") if isinstance(state.get("coordination"), dict) else {}
    parent = coordination.get("parent_session_id")
    return {"collab": coordination.get("collab") is True,
            "parent_session_id": parent if isinstance(parent, str) and SESSION_ID.fullmatch(parent) else None,
            "parent_check": coordination.get("parent_check") if coordination.get("parent_check") in {"matched", "unavailable"} else None}


def _match_attempt(task, attempt, rows, declared_parent, observed):
    attempt_id = attempt.get("id")
    workspace = attempt.get("workspace") if isinstance(attempt.get("workspace"), dict) else {}
    mode = workspace.get("mode") or task.get("workspace", "shared")
    view = {"task_id": task["id"], "attempt_id": attempt_id, "harness": task["harness"], "mode": mode,
            "status": "unavailable", "failure": None}
    if not isinstance(attempt_id, str) or not _ATTEMPT_ID.fullmatch(attempt_id):
        # An empty or altered ID would make every actor "end with" it.
        view["failure"] = "attempt_id_invalid"
        return view
    started = parse_time(attempt.get("started_at"))
    finished = parse_time(attempt.get("finished_at")) if attempt.get("finished_at") else None
    if started is None or attempt.get("finished_at") and finished is None:
        view["failure"] = "attempt_time_unparseable"
        return view
    # Collab stores milliseconds; Workbench records microseconds.
    started = started.replace(microsecond=started.microsecond // 1000 * 1000)
    upper = finished + WINDOW_GRACE if finished is not None else observed
    root = None
    if mode == "worktree":
        if not workspace.get("root"):
            view["failure"] = "workspace_unrecorded"
            return view
        root = os.path.realpath(workspace["root"])
    excluded = {"outside_window": 0, "time_unparseable": 0, "invalid_session_id": 0, "root_mismatch": 0}
    matched = []
    for row in rows:
        actor = row["actor"] if isinstance(row["actor"], str) else ""
        label_match = actor.endswith(attempt_id)
        label_exact = actor.endswith(LABEL_SUFFIX + attempt_id)
        if root is not None:
            if row["root_path"] != root:
                if label_match:
                    excluded["root_mismatch"] += 1
                continue
        elif not label_match:
            continue
        parent = row["parent_session_id"]
        if (not isinstance(row["id"], str) or not SESSION_ID.fullmatch(row["id"])
                or parent is not None and (not isinstance(parent, str) or not SESSION_ID.fullmatch(parent))):
            excluded["invalid_session_id"] += 1
            continue
        joined = parse_time(row["joined_at"])
        if joined is None:
            excluded["time_unparseable"] += 1
            continue
        if not started <= joined <= upper:
            excluded["outside_window"] += 1
            continue
        status = row["status"] if isinstance(row["status"], str) and _STATUS.fullmatch(row["status"]) else "unknown"
        matched.append((joined, {
            "id": row["id"], "status": status, "parent_session_id": parent,
            "parent_matches": None if declared_parent is None else parent == declared_parent,
            "joined_at": row["joined_at"], "ended_at": _time_text(row["ended_at"]),
            "last_seen_at": _time_text(row["last_seen_at"]),
            "label_match": label_match, "label_exact": label_exact}))
    matched.sort(key=lambda item: item[0])
    view.update(status="recorded", sessions=[session for _, session in matched[:SESSION_LIMIT]],
                session_count=len(matched), truncated=len(matched) > SESSION_LIMIT,
                left_active=sum(session["status"] == "active" for _, session in matched), excluded=excluded)
    return view


def attribution(state, *, home=None, env=None):
    """Live collab sessions per recorded attempt; read-only, nothing is saved.

    Works for any run, opted in or not. Each harness's journal comes from its
    own registration; harnesses using different journals is a warning.
    """
    home, env = _home_env(home, env)
    observed = datetime.now(timezone.utc)
    coordination = coordination_view(state)
    resolved = {}
    for task in state.get("tasks", []):
        if task.get("attempts") and task.get("harness") in HARNESS_HOSTS and task["harness"] not in resolved:
            resolved[task["harness"]] = resolve_journal(task["harness"], home=home, env=env)
    journals = {}
    for harness, entry in resolved.items():
        key = entry["real"] or "failure:" + harness
        journals.setdefault(key, {"harnesses": [], "resolved": entry})["harnesses"].append(harness)
    for key, journal in journals.items():
        entry = journal["resolved"]
        journal["outcome"] = (read_journal(entry["path"], _session_rows) if entry["real"] is not None
                              else {"failure": entry["failure"], "user_version": None, "newer_tables": None, "result": None})
    by_harness = {harness: journal for journal in journals.values() for harness in journal["harnesses"]}
    attempts = []
    for task in state.get("tasks", []):
        for attempt in task.get("attempts", []):
            if task.get("harness") not in HARNESS_HOSTS:
                attempts.append({"task_id": task["id"], "attempt_id": attempt.get("id"), "harness": task.get("harness"),
                                 "mode": task.get("workspace", "shared"), "status": "unavailable", "failure": "command_task"})
                continue
            outcome = by_harness[task["harness"]]["outcome"]
            if outcome["failure"]:
                attempts.append({"task_id": task["id"], "attempt_id": attempt.get("id"), "harness": task["harness"],
                                 "mode": ((attempt.get("workspace") or {}).get("mode") or task.get("workspace", "shared")),
                                 "status": "unavailable", "failure": outcome["failure"]})
                continue
            attempts.append(_match_attempt(task, attempt, outcome["result"], coordination["parent_session_id"], observed))
    rows = []
    for key, journal in journals.items():
        outcome, entry = journal["outcome"], journal["resolved"]
        name = os.path.basename(entry["path"]) if entry["path"] else None
        rows.append({"harnesses": sorted(journal["harnesses"]), "status": "unavailable" if outcome["failure"] else "read",
                     "failure": outcome["failure"],
                     "basename": (name if name and _FILE_NAME.fullmatch(name) else "[name withheld]") if name else None,
                     "user_version": outcome["user_version"], "newer_tables": outcome["newer_tables"]})
    readable = {key for key, journal in journals.items() if not key.startswith("failure:")}
    return {"run_id": state["id"], "observed_at": observed.isoformat(), "coordination": coordination,
            "journals": rows, "warnings": ["journals_differ"] if len(readable) > 1 else [],
            "attempts": attempts, "left_active": sum(item.get("left_active", 0) for item in attempts),
            "matching": {"worktree": "worktree root and time window; label reported, not required",
                         "shared_and_read_only": "actor label ending with the attempt ID and time window",
                         "window": "attempt start (to milliseconds) through finish plus 5 seconds, or now while running"},
            "limitations": LIMITATIONS + ["Labels are self-declared routing hints, not authentication.",
                                          "Workbench never ends collab sessions."]}


_JOURNAL_KINDS = {None: "sqlite_collab", "journal_schema_unknown": "sqlite_collab", "journal_not_collab": "sqlite_other",
                  "journal_not_sqlite": "not_sqlite", "journal_missing": "missing", "journal_not_regular": "not_regular",
                  "journal_unreadable": "unreadable", "journal_too_large": "sqlite_collab"}


def _safe_name(name):
    return name if isinstance(name, str) and _NAME.fullmatch(name) else "[name withheld]"


def _env_keys(env):
    return sorted({key if _ENV_KEY.fullmatch(key) else "[key withheld]" for key in (env or {})})


def _nearby(program, filename, levels):
    """Parse the first ``filename`` in a program's directory or its parents."""
    if not isinstance(program, str) or not os.path.isabs(program):
        return None
    directory = os.path.dirname(program)
    for _ in range(levels):
        data, status = _read_bounded(os.path.join(directory, filename), _SMALL_LIMIT)
        if status != "absent":
            try:
                parsed = json.loads(data) if data is not None else None
            except (ValueError, UnicodeError):
                return None
            return parsed if isinstance(parsed, dict) else None
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return None


def _bundle_version(entry):
    for program in _programs(entry):
        if os.path.basename(program).startswith(COLLAB_PROGRAM):
            package = _nearby(program, "package.json", 3)
            version = (package or {}).get("version")
            return version if isinstance(version, str) and _VERSION.fullmatch(version) else None
    return None


def _installed_commit(entry):
    candidates = [arg for arg in entry.get("args") or [] if os.path.isabs(arg)]
    if isinstance(entry.get("command"), str) and os.path.basename(entry["command"]).startswith(LOBBY_NAME):
        candidates.append(entry["command"])
    for program in candidates[:16]:
        receipt = _nearby(program, "INSTALL-RECEIPT.json", 5)
        if receipt is not None:
            match = _COMMIT.search(json.dumps(receipt.get("source")))
            return match.group(0) if match else "unknown"
    return None


def _desk_home(entry):
    value = (entry.get("env") or {}).get("AGENTTOOL_LOBBY_HOME")
    if not isinstance(value, str) or not os.path.isabs(value):
        return None
    try:
        return stat.S_ISDIR(os.stat(value).st_mode)  # stat only; nothing inside is opened
    except OSError:
        return False


def agenttool_report(*, home=None, env=None):
    """Doctor's agenttool section: registrations and journal identity, offline.

    Names (sanitized), environment key names, booleans, versions and a lobby
    install commit only: never a command, argument, path or environment value.
    No MCP server, host CLI or lobby command is started, and nothing inside a
    lobby desk home is opened.
    """
    home, env = _home_env(home, env)
    rows = {}
    journals = {}
    resolved_paths = {}
    versions = set()
    for harness in ("claude", "astra", "astra-ultra", "codex", "hermes", "hermes-astra-ultra"):
        found = registrations(harness, home=home, env=env)
        row = {"config": found["config"], "config_status": found["config_status"]}
        if found.get("profile"):
            row["profile"] = _safe_name(found["profile"])
        collab_entries = [entry for entry in found["entries"] if entry["kind"] == "collab"]
        listed = []
        for entry in collab_entries:
            version = None if entry["unparsed"] else _bundle_version(entry)
            if version:
                versions.add(version)
            listed.append({"name": _safe_name(entry["name"]), "parsed": not entry["unparsed"],
                           "env_keys": _env_keys(entry["env"]),
                           "db_configured": isinstance((entry["env"] or {}).get("AGENTOOL_COLLAB_DB"), str),
                           "bundle_version": version})
        resolved = resolve_journal(harness, home=home, env=env)
        if resolved["real"] is None:
            journal = {"kind": "unresolved", "failure": resolved["failure"], "application_id_ok": None,
                       "user_version": None, "newer_tables": None}
        else:
            if resolved["real"] not in journals:
                journals[resolved["real"]] = read_journal(resolved["path"])
            outcome = journals[resolved["real"]]
            kind = _JOURNAL_KINDS.get(outcome["failure"], "unreadable")
            journal = {"kind": kind, "failure": outcome["failure"],
                       "application_id_ok": True if kind == "sqlite_collab" else False if kind == "sqlite_other" else None,
                       "user_version": outcome["user_version"], "newer_tables": outcome["newer_tables"]}
            # Keyed by configuration file: harnesses sharing one prove nothing about agreement.
            resolved_paths[resolved["source"]] = resolved["real"]
        unparsed = found.get("unclassified_unparsed", 0)
        # A configuration that could not be read says nothing about absence.
        known = found["config_status"] in {"read", "absent"}
        row["collab"] = {"registered": True if collab_entries else None if unparsed or not known else False,
                         "unclassified_unparsed": unparsed, "registrations": listed, "journal": journal}
        if HARNESS_HOSTS[harness] in {"claude", "codex"}:
            lobby = [entry for entry in found["entries"] if entry["kind"] == "lobby"]
            row["lobby"] = {"registered": True if lobby else False if known else None, "registrations": [
                {"name": _safe_name(entry["name"]), "env_keys": _env_keys(entry["env"]),
                 "desk_home_present": _desk_home(entry), "installed_commit": _installed_commit(entry)}
                for entry in lobby]}
        rows[harness] = row
    return {"scope": {"offline": True, "read_only": True, "mcp_servers_started": False},
            "harnesses": rows,
            "journals_equal": len(set(resolved_paths.values())) == 1 if len(resolved_paths) > 1 else None,
            "versions_differ": len(versions) > 1,
            "limitations": LIMITATIONS + ["A read-only journal open can create empty -wal/-shm files beside a WAL journal.",
                                          "Lobby desk homes are checked with stat only; lobby.sqlite is never opened."]}


def format_agenttool(report):
    """Doctor text lines for ``agenttool_report``; names and booleans only."""
    lines = ["agenttool coordination registrations (offline; no MCP server started):"]
    for harness, row in report.get("harnesses", {}).items():
        collab_row = row["collab"]
        journal = collab_row["journal"]
        if collab_row["registered"]:
            versions = sorted({item["bundle_version"] for item in collab_row["registrations"] if item["bundle_version"]})
            text = (f"collab registered ({len(collab_row['registrations'])}), journal {journal['kind']}"
                    + (f" ({journal['failure']})" if journal["failure"] else "")
                    + (f", user_version {journal['user_version']}" if journal["user_version"] is not None else "")
                    + (", newer tables present" if journal["newer_tables"] else "")
                    + (f", bundle {', '.join(versions)}" if versions else ", bundle version unknown"))
        elif row["config_status"] not in {"read", "absent"}:
            text = f"configuration {row['config_status']}" + ("; collab and lobby unknown" if "lobby" in row else "; collab unknown")
            lines.append(f"  {harness} ({row['config']}): {text}")
            continue
        elif collab_row["registered"] is None:
            count = collab_row["unclassified_unparsed"]
            text = f"collab registration unknown ({count} server{'' if count == 1 else 's'} unparsed)"
        else:
            text = "collab not registered"
        if "lobby" in row:
            lobby = row["lobby"]["registrations"]
            if lobby:
                desk = {True: "desk home present", False: "desk home absent", None: "desk home not configured"}[lobby[0]["desk_home_present"]]
                commit = lobby[0]["installed_commit"]
                text += f"; lobby registered, {desk}, " + (f"commit {commit[:12]}" if commit and commit != "unknown" else
                                                          "commit unknown" if commit else "no install receipt")
            else:
                text += "; lobby not registered"
        lines.append(f"  {harness} ({row['config']}): {text}")
    equal = report.get("journals_equal")
    lines.append("  Collab journals equal across host configurations: " + ("yes" if equal else "no" if equal is False else "not comparable")
                 + "; bundle versions differ: " + ("yes" if report.get("versions_differ") else "no"))
    lines.extend("  " + limitation for limitation in report.get("limitations", []))
    return lines


def format_attribution(view):
    """Human text from an attribution view: no paths, credentials or actor text."""
    from .cli import safe_terminal
    lines = [f"{view['run_id']}  collab attribution (read-only, observed {safe_terminal(view['observed_at'])})"]
    coordination = view["coordination"]
    if coordination["collab"]:
        parent = coordination["parent_session_id"]
        lines.append("Opt-in: --collab" + (f"; declared parent {parent} (unverified lineage, check "
                                           f"{coordination['parent_check'] or 'not recorded'})" if parent else ""))
    else:
        lines.append("Opt-in: none (no board note was sent; worktree attempts still attribute by root)")
    for journal in view["journals"]:
        harnesses = ", ".join(journal["harnesses"])
        if journal["status"] == "read":
            lines.append(f"Journal {safe_terminal(journal['basename'])} for {harnesses}: user_version "
                         f"{journal['user_version']}, newer tables {'yes' if journal['newer_tables'] else 'no'}")
        else:
            lines.append(f"Journal unavailable for {harnesses}: {journal['failure']}")
    if "journals_differ" in view["warnings"]:
        lines.append("Warning: this run's harnesses use different collab journals.")
    for attempt in view["attempts"]:
        head = (f"  {safe_terminal(attempt['task_id'])} attempt {safe_terminal(attempt['attempt_id'])} "
                f"({safe_terminal(attempt['harness'])}, {safe_terminal(attempt['mode'])})")
        if attempt["status"] != "recorded":
            lines.append(f"{head}: unavailable ({attempt['failure']})")
            continue
        excluded = ", ".join(f"{count} {reason.replace('_', ' ')}" for reason, count in attempt["excluded"].items() if count)
        lines.append(f"{head}: {attempt['session_count']} session(s), {attempt['left_active']} active"
                     + ("; showing the first 20" if attempt["truncated"] else "") + (f"; excluded {excluded}" if excluded else ""))
        for session in attempt["sessions"]:
            label = "exact" if session["label_exact"] else "suffix" if session["label_match"] else "none"
            if session["parent_session_id"] is None:
                parent = "none"
            elif session["parent_matches"] is None:
                parent = "declared by the child"
            else:
                parent = "matches" if session["parent_matches"] else "differs"
            lines.append(f"    {session['id']}  {session['status']}  joined {session['joined_at']}"
                         + (f"  ended {session['ended_at']}" if session["ended_at"] else "")
                         + f"  label {label}  parent {parent}")
    if view["left_active"]:
        lines.append(f"{view['left_active']} collab session(s) still active; Workbench does not end sessions. "
                     "The launching host may end one with collab's own host end command (see collab's README).")
    return "\n".join(lines)
