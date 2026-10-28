"""Names-only inventory of the MCP servers a harness child would inherit.

Native configuration files are read, never executed. Besides display labels of
the files read, only server names, Codex enabled flags and tool-filter counts
leave this module: commands, arguments, environment, URLs, headers and tokens
are discarded in memory. A listed server is configured, not observed running.
"""
from __future__ import annotations

import os
from pathlib import Path
import re

from .capabilities import _MAX_ROWS, _display, _name, _read, _read_bytes, _root, _setting_value


_SCOPE = "Configured server names and selected flags only; commands, arguments, environment, URLs, headers and tokens are omitted."
_HERMES_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


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


def _codex_server(entry: dict) -> dict:
    shape = {"enabled": _setting_value("enabled", entry["enabled"])} if "enabled" in entry else {}
    if isinstance(entry.get("enabled_tools"), list):
        shape["enabled_tools_count"] = len(entry["enabled_tools"])
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
        failed = sum(server.get("status") == "malformed" for server in row["servers"])
        if row["status"] not in {"read", "absent"}:
            warnings.append(f"MCP servers in {row['source']} could not be inspected ({row['status']}); unlisted servers may be inherited.")
        if failed:
            warnings.append(f"{failed} MCP server entr{'y' if failed == 1 else 'ies'} in {row['source']} could not be inspected.")
        if row["server_count"] > len(row["servers"]):
            warnings.append(f"Only the first {_MAX_ROWS} MCP servers in {row['source']} are listed.")
        incomplete = incomplete or row["status"] not in {"read", "absent"} or failed or row["server_count"] > len(row["servers"])
    count = sum(len(row["servers"]) for row in sources)
    return {"status": "incomplete" if incomplete else "unverified" if count else "not_configured",
            "scope": _SCOPE, **extra, "sources": sources, "server_count": count,
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
        # This file also holds account and project state; only MCP names are taken.
        data, status = _read(_root(configured, home, home) / ".claude.json")
        data = data or {}
        sources = [_source(label, "user", status, data.get("mcpServers"))]
        if project is not None:
            # Local scope is keyed by the Git root containing the project, else the project.
            key = next((path for path in (project, *project.parents) if (path / ".git").exists()), project)
            projects = data.get("projects")
            entry = projects.get(key.as_posix()) if isinstance(projects, dict) else None
            if isinstance(entry, dict) and "mcpServers" in entry:
                sources.append(_source(f"{label} projects[{_label(key, home, project)}]", "local", "read", entry["mcpServers"]))
            for directory in reversed([project, *project.parents]):
                shared, shared_status = _read(directory / ".mcp.json")
                if shared_status != "absent":
                    sources.append(_source(_label(directory / ".mcp.json", home, project), "project",
                                               shared_status, (shared or {}).get("mcpServers")))
        limitations = ["Plugins, managed MCP policy, claude.ai connectors and --mcp-config can add servers; per-project disables and .mcp.json approvals are not resolved."]
        if harness != "claude":
            limitations.append("The Astra wrapper was not executed; it may select another configuration directory or MCP flags.")
        return _report(sources, limitations, [])
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
            selection = "unverified"
            warnings.append("Hermes's sticky active_profile could not be interpreted; the unprofiled home was inspected.")
        elif name and name != "default":
            selected, selection = root / "profiles" / name, "sticky_active_profile"
    path = selected / "config.yaml"
    # The standard library has no YAML parser, so the profile is located, not read.
    status = "not_inspected" if os.path.lexists(path) else "absent"
    label = _label(path, home)
    limitations = ["Hermes config.yaml is YAML, which the standard library cannot parse; its MCP servers are not listed.",
                   "Plugin-provided, managed-scope and last-known-good fallback configuration are not inspected either."]
    wrapper_unseen = harness == "hermes-astra-ultra" and status == "absent"
    if harness == "hermes-astra-ultra":
        limitations.append("The hermes-astra-ultra wrapper was not executed; it may set its own HERMES_HOME, so this profile is a candidate.")
    if wrapper_unseen:
        warnings.append(f"No Hermes configuration at {label}; the hermes-astra-ultra wrapper may use another installation.")
    if str(env.get("HERMES_SAFE_MODE", "")).strip().lower() in {"true", "1", "yes", "on"}:
        warnings.append("HERMES_SAFE_MODE is set; Hermes loads no MCP servers in safe mode.")
    return _report([_source(label, "profile", status, None)], limitations, warnings,
                       incomplete=wrapper_unseen, profile=_label(selected, home), profile_selection=selection)
