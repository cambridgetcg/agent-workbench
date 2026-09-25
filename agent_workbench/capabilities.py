"""Bounded, read-only launch evidence and partial configuration explanations.

Help is evidence that a CLI advertises an interface, never proof that a provider,
account, or running process supports its protocol. Authentication files, session
transcripts, instruction contents, hook commands and permission patterns are not
read or returned here. A launch profile's absolute paths are private run metadata;
the corresponding display fields are suitable for diagnostic output.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tomllib

from .processes import run_owned


_LIMIT = 2_097_152
_PROBE_LIMIT = 262_144
_PROBE_TIMEOUT_SECONDS = 3
_MAX_ROWS = 100
_EXECUTABLES = {"codex": "codex", "claude": "claude", "astra": "claude-astra",
                "astra-ultra": "claude-astra-ultra"}
_SEMVER = re.compile(r"(?<![\w.])v?(\d{1,4}\.\d{1,4}\.\d{1,4}(?:\.\d{1,4})?)(?![\w.])")
_MODEL = re.compile(r"(?:(?:gpt-[0-9]|claude-)[A-Za-z0-9._():-]*|(?:o[134]|opus|opusplan|sonnet|haiku|fable|default)(?:-[A-Za-z0-9._():-]+)?)(?:\[(?:1m|200k)\])?\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@/-]{0,119}\Z")
_SECRET = re.compile(r"(?i)(?:\b(?:sk|pk|ghp|gho|github_pat|xox[baprs])[-_]|\beyJ[A-Za-z0-9_-]{10}|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})")
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "ultracode"}
_PERMISSION_MODES = {"default", "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}
_HOOK_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "Notification",
                "UserPromptSubmit", "SessionStart", "SessionEnd", "Stop", "SubagentStart", "SubagentStop",
                "PreCompact", "PostCompact", "InstructionsLoaded", "ConfigChange", "WorktreeCreate",
                "WorktreeRemove", "TaskCompleted", "TeammateIdle", "Elicitation", "ElicitationResult"}
_FLAGS = {"--output-format", "--input-format", "--resume", "--continue", "--worktree", "--bg",
          "--background", "--include-hook-events", "--forward-subagent-text", "--safe-mode", "--bare",
          "--setting-sources", "--settings", "--plugin-dir", "--effort", "--model", "--json",
          "--no-daemon", "--sandbox", "--ask-for-approval", "--replay-user-messages"}


def _read(path: Path, *, toml: bool = False) -> tuple[dict | None, str]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None, "not_regular"
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(_LIMIT + 1)
        finally:
            os.close(descriptor)
        if len(raw) > _LIMIT:
            return None, "too_large"
        value = tomllib.loads(raw.decode("utf-8")) if toml else json.loads(raw)
        return (value, "read") if isinstance(value, dict) else (None, "malformed")
    except FileNotFoundError:
        return None, "absent"
    except (ValueError, TypeError, UnicodeError):
        return None, "malformed"
    except OSError:
        return None, "unreadable"


def _name(value: object) -> str:
    if isinstance(value, str) and _NAME.fullmatch(value) and not _SECRET.search(value):
        return value
    return "[custom identifier withheld]"


def _version(value: object) -> str | None:
    if isinstance(value, str) and len(value) <= 80 and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.+-][A-Za-z0-9.-]+)?", value):
        return value
    return None


def _display(path: Path, home: Path, project: Path | None = None) -> str:
    label = str(path)
    for root, prefix in ((project, "$PROJECT"), (home, "~")):
        if root is not None:
            try:
                relative = path.relative_to(root)
                label = prefix + ("/" + relative.as_posix() if relative.parts else "")
                break
            except ValueError:
                pass
    if len(label) > 512 or _SECRET.search(label) or any(ord(c) < 32 for c in label):
        return "[custom path withheld]"
    return label


def _root(value: object, default: Path, home: Path) -> Path:
    if not isinstance(value, str) or not value:
        return default
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    path = Path(value)
    return path if path.is_absolute() else home / path


def _paths(name: str, project: Path, env: dict) -> list[Path]:
    if "/" in name:
        path = Path(name)
        candidates = [path if path.is_absolute() else project / path]
    else:
        candidates = []
        for part in env.get("PATH", os.defpath).split(os.pathsep):
            directory = Path(part) if part else project
            if not directory.is_absolute():
                directory = project / directory
            candidates.append(directory / name)
    result, seen = [], set()
    for path in candidates:
        path = Path(os.path.abspath(path))
        if str(path) in seen:
            continue
        seen.add(str(path))
        try:
            if path.is_file() and os.access(path, os.X_OK):
                result.append(path)
        except OSError:
            continue
    return result


def _probe(executable: Path, args: list[str], project: Path, env: dict) -> tuple[dict, str]:
    # Discovery owns helpers too: a successful --help leader can leave a child
    # behind, just as a timed-out probe can. Raw output stays in private bounded
    # files and only successfully completed probes reach the metadata parser.
    try:
        result = run_owned([str(executable), *args], cwd=project, env=env,
                           timeout=_PROBE_TIMEOUT_SECONDS, output_limit=_PROBE_LIMIT)
    except (OSError, ValueError):
        return {"status": "failed"}, ""
    outcome = result["outcome"]
    if outcome == "exited":
        return {"status": "reported" if result["exit_code"] == 0 else "failed",
                "exit_code": result["exit_code"], "output_truncated": False}, result["stdout"] + result["stderr"]
    return {"status": outcome if outcome in {"timed_out", "interrupted", "output_limit"} else "failed",
            "failure_kind": outcome, "exit_code": result["exit_code"],
            "output_truncated": outcome == "output_limit"}, ""


def _capability(status: str, evidence: str, **extra) -> dict:
    return {"status": status, "evidence": evidence, "runtime_verified": False, **extra}


def _help_capability(help_info: dict, flags: set[str], *required: str) -> dict:
    if help_info["status"] != "reported":
        return _capability("unverified", "help unavailable")
    if all(flag in flags for flag in required):
        return _capability("supported", "advertised by CLI help; runtime protocol not verified")
    return _capability("unverified", "not advertised by inspected help; absence is not a runtime test")


def inspect_launch(task: dict, project: Path, *, env: dict | None = None, home: Path | None = None) -> dict:
    """Resolve an executable and record selected, sanitized capability evidence.

    No command task or Astra wrapper is invoked during inspection. Known native
    CLIs receive only --version and --help (Codex also exec --help). The returned
    ``executable`` is the absolute selected entry point for a later launch.
    Wrappers still resolve their own children; the nested ``native`` record
    describes the current PATH candidate, not a guaranteed wrapper launch.
    """
    env = dict(os.environ if env is None else env)
    home = Path.home() if home is None else Path(home)
    project = Path(project).resolve()
    harness = task.get("harness")
    if harness not in {*_EXECUTABLES, "command"}:
        raise ValueError("Unknown harness for launch inspection")
    name = task.get("argv", [None])[0] if harness == "command" and task.get("argv") else _EXECUTABLES.get(harness)
    if not isinstance(name, str) or not name:
        raise ValueError("A command launch requires a nonempty executable")
    candidates = _paths(name, project, env)
    selected = candidates[0] if candidates else None
    warnings = []
    identities = {str(path.resolve()) for path in candidates}
    if len(identities) > 1:
        warnings.append("Multiple distinct installations are available on PATH; selection follows this launch environment.")
    result = {"schema_version": 1, "harness": harness,
              "executable": str(selected) if selected else None,
              "resolved_executable": str(selected.resolve()) if selected else None,
              "display_executable": _display(selected, home, project) if selected else None,
              "status": "resolved" if selected else "unavailable",
              "candidates": [{"path": _display(path, home, project),
                              "resolved_path": _display(path.resolve(), home, project)} for path in candidates[:_MAX_ROWS]],
              "candidate_count": len(candidates), "warnings": warnings,
              "protocol_evidence": "not runtime verified", "capabilities": {}}
    if harness == "command":
        result.update(version={"status": "not_probed", "version": None}, help={"status": "not_probed", "flags": []})
        result["configuration"] = {"scope": "not_applicable", "reason": "Command environment and arguments are not inspected."}
        return result
    native = selected
    wrapper = harness in {"astra", "astra-ultra"}
    if wrapper:
        native_candidates = _paths("claude", project, env)
        native = native_candidates[0] if native_candidates else None
        result["native"] = {"executable": str(native) if native else None,
                            "display_executable": _display(native, home, project) if native else None,
                            "evidence": "PATH child candidate; wrapper forwarding not runtime verified",
                            "candidate_count": len(native_candidates)}
        if len({str(path.resolve()) for path in native_candidates}) > 1:
            warnings.append("Multiple Claude installations are on PATH; the wrapper's native child can vary between launch environments.")
        warnings.append("Astra wrapper was not executed during discovery; settings composition and proxy lifetime remain adapter boundaries.")
    version_info, help_info, help_text = {"status": "unavailable", "version": None}, {"status": "unavailable"}, ""
    if selected is not None and native is not None:
        version_info, text = _probe(native, ["--version"], project, env)
        match = _SEMVER.search(text)
        version_info["version"] = match.group(1) if match and version_info["status"] == "reported" else None
        if version_info["status"] == "interrupted":
            # A captured signal ends discovery; do not start a second native
            # probe after the helper has cleaned the interrupted process group.
            help_info = {"status": "interrupted"}
        else:
            help_info, help_text = _probe(native, ["--help"], project, env)
        if harness == "codex" and help_info["status"] != "interrupted":
            exec_info, exec_text = _probe(native, ["exec", "--help"], project, env)
            help_info["exec_status"] = exec_info["status"]
            if exec_info["status"] == "reported":
                help_text += "\n" + exec_text
    flags = {flag for flag in _FLAGS if re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", help_text)}
    help_info["flags"] = sorted(flags)
    help_info["stream_json_advertised"] = "stream-json" in help_text
    result.update(version=version_info, help=help_info)
    version_info["subject"] = "native_child_candidate" if wrapper else "selected_executable"
    result["requested"] = {key: _setting_value("effortLevel" if key == "effort" else key, task[key])
                           for key in ("model", "effort") if key in task}
    caps = result["capabilities"]
    if harness == "codex":
        caps["structured_events"] = _help_capability(help_info, flags, "--json")
        caps["live_input"] = _capability("unavailable", "Workbench exec adapter is single-input; no duplex protocol configured")
        caps["interrupt"] = _capability("unavailable", "Workbench exec adapter has process stop only, not acknowledged turn interruption")
        caps["native_agents_md"] = _capability("unverified", "Native instruction discovery is not runtime observed")
    else:
        caps["structured_events"] = _help_capability(help_info, flags, "--output-format")
        caps["stream_input"] = _help_capability(help_info, flags, "--input-format")
        if not help_info["stream_json_advertised"]:
            for key in ("structured_events", "stream_input"):
                caps[key] = _capability("unverified", "stream-json format not advertised by inspected help")
        for key, flag in (("resume", "--resume"), ("worktree", "--worktree"),
                          ("hook_events", "--include-hook-events"), ("safe_mode", "--safe-mode")):
            caps[key] = _help_capability(help_info, flags, flag)
        caps["live_input"] = _capability("unverified", "Stream input help is not evidence of acknowledged interactive input")
        caps["interrupt"] = _capability("unverified", "Control acknowledgments require a runtime protocol handshake")
        caps["function_hooks"] = _capability("unverified", "Early-access function hooks require a compatible enabled runtime; source declarations are not runtime evidence")
        version = version_info["version"]
        old = version is not None and tuple(map(int, version.split(".")[:3])) < (2, 1, 277)
        caps["native_agents_md"] = _capability("unavailable" if old else "unverified",
            "Official source documents native AGENTS.md from 2.1.277; provider support differs and loaded instructions are not observed",
            documented_minimum="2.1.277")
        caps["background"] = _help_capability(help_info, flags, "--background" if "--background" in flags else "--bg")
        if wrapper:
            for key in ("structured_events", "stream_input", "resume", "worktree", "hook_events", "safe_mode", "background"):
                if caps[key]["status"] == "supported":
                    caps[key] = _capability("unverified", "Native child advertises the interface; wrapper forwarding and lifecycle are not verified")
    result["configuration"] = explain_configuration(project, home=home, env=env)
    if harness != "codex" and caps["native_agents_md"]["status"] == "unavailable":
        if any(row["name"] == "AGENTS.md" and row["status"] == "present"
               for row in result["configuration"]["instructions"]["files"]):
            warnings.append("AGENTS.md was discovered but this native version predates documented automatic loading; imports were not evaluated.")
    return result


def _setting_value(key: str, value: object) -> object:
    if key in {"model", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
               "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL"}:
        return value if isinstance(value, str) and len(value) <= 96 and _MODEL.fullmatch(value) and not _SECRET.search(value) else "[custom or invalid model withheld]"
    if key in {"effortLevel", "model_reasoning_effort", "CLAUDE_CODE_EFFORT_LEVEL"}:
        return value if isinstance(value, str) and value in _EFFORTS else "[unrecognized effort withheld]"
    options = {"approval_policy": {"untrusted", "on-failure", "on-request", "never"},
               "sandbox_mode": {"read-only", "workspace-write", "danger-full-access"},
               "model_provider": {"openai", "ollama", "lmstudio"}}
    if key in options:
        return value if isinstance(value, str) and value in options[key] else "[custom or invalid value withheld]"
    return value if isinstance(value, bool) else "[invalid boolean withheld]"


def _configuration(sources: list[tuple[Path, str]], keys: tuple[str, ...], *, toml: bool = False) -> dict:
    rows, contributing = [], {}
    for path, label in sources:
        data, status = _read(path, toml=toml)
        data = data or {}
        values = {key: _setting_value(key, data[key]) for key in keys if key in data}
        row = {"source": label, "status": status, "configured": values}
        for key, value in values.items():
            contributing.setdefault(key, []).append({"source": label, "value": value})
        if not toml:
            permissions = data.get("permissions")
            if isinstance(permissions, dict):
                row["permissions"] = {key + "_count": len(permissions[key]) if isinstance(permissions.get(key), list) else 0
                                      for key in ("allow", "ask", "deny", "additionalDirectories")}
                if "defaultMode" in permissions:
                    mode = permissions["defaultMode"]
                    row["permissions"]["defaultMode"] = mode if isinstance(mode, str) and mode in _PERMISSION_MODES else "[unrecognized mode withheld]"
            hooks = data.get("hooks")
            if isinstance(hooks, dict):
                row["hooks"] = {"events": sorted(key for key in hooks if key in _HOOK_EVENTS),
                                "unknown_event_count": sum(key not in _HOOK_EVENTS for key in hooks),
                                "entry_count": sum(len(value) if isinstance(value, list) else 0 for value in hooks.values())}
            enabled = data.get("enabledPlugins")
            if isinstance(enabled, dict):
                row["plugins"] = [{"id": _name(key), "enabled": value if isinstance(value, bool) else None}
                                  for key, value in list(enabled.items())[:_MAX_ROWS]]
            configured_env = data.get("env")
            if isinstance(configured_env, dict):
                row["model_environment"] = {key: _setting_value(key, configured_env[key]) for key in _MODEL_ENV if key in configured_env}
        rows.append(row)
    return {"sources": rows, "contributing_sources": contributing,
            "inferred_selected": {key: values[-1] for key, values in contributing.items()},
            "precedence": "Later inspected sources supply scalar candidates; native policy, trust, merges and CLI overrides are not resolved."}


_MODEL_ENV = ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
              "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
              "CLAUDE_CODE_EFFORT_LEVEL")


def _file_status(path: Path) -> str:
    try:
        mode = path.stat().st_mode
        return "present" if stat.S_ISREG(mode) else "not_regular"
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"


def _instruction_preview(project: Path, home: Path, claude_root: Path, codex_root: Path) -> dict:
    candidates = [(claude_root / "CLAUDE.md", "user"), (codex_root / "AGENTS.md", "codex_user"),
                  (codex_root / "AGENTS.override.md", "codex_user")]
    # Ancestor metadata is relevant even above a repository root. Do not open any
    # instruction bodies, follow imports, or scan descendants recursively.
    for directory in reversed([project, *project.parents]):
        scope = "project" if directory == project else "ancestor"
        candidates.extend((directory / relative, scope) for relative in
                          ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENTS.override.md"))
    files = []
    for path, scope in candidates:
        status = _file_status(path)
        if status != "absent":
            files.append({"path": _display(path, home, project), "name": path.name, "scope": scope, "status": status})
    rules = []
    for path, scope in ((claude_root / "rules", "user"), (project / ".claude/rules", "project")):
        count, scanned = 0, 0
        try:
            # Top-level only: a precise recursive/native discovery claim would
            # require trust, path filters, nested contexts and import processing.
            with os.scandir(path) as entries:
                for entry in entries:
                    scanned += 1
                    if entry.name.endswith(".md") and entry.is_file(follow_symlinks=False):
                        count += 1
                    if scanned >= _MAX_ROWS:
                        break
            status = "present"
        except FileNotFoundError:
            status = "absent"
        except OSError:
            status = "unreadable"
        rules.append({"path": _display(path, home, project), "scope": scope,
                      "status": status, "top_level_markdown_count": count, "scan_capped": scanned >= _MAX_ROWS})
    return {"scope": "file-presence preview, not native loaded context", "files": files, "rules": rules,
            "contents_read": False, "limitations": ["Imports, path-scoped rules, auto-memory, nested attachments and native project trust are not evaluated.",
            "AGENTS.md support and CLAUDE.md fallback depend on native version, provider and instruction mode."]}


def _plugin_inventory(claude_root: Path, project: Path, home: Path) -> dict:
    registry, status = _read(claude_root / "plugins/installed_plugins.json")
    marketplaces, market_status = _read(claude_root / "plugins/known_marketplaces.json")
    installed = (registry or {}).get("plugins", {})
    installed = installed if isinstance(installed, dict) else {}
    rows = []
    for identifier, registrations in installed.items():
        if not isinstance(registrations, list):
            continue
        for registration in registrations:
            if len(rows) >= _MAX_ROWS:
                break
            if not isinstance(registration, dict):
                continue
            row = {"id": _name(identifier), "registered_version": _version(registration.get("version")),
                   "scope": registration.get("scope") if registration.get("scope") in {"user", "project", "local", "managed"} else "unknown"}
            install = registration.get("installPath")
            manifest, manifest_status = (None, "unknown_path")
            if isinstance(install, str) and install and Path(install).is_absolute():
                directory = Path(install)
                row["cache_path"] = _display(directory, home, project)
                manifest, manifest_status = _read(directory / ".claude-plugin/plugin.json")
            row["manifest_status"] = manifest_status
            row["manifest_version"] = _version((manifest or {}).get("version"))
            row["registered_cache_consistency"] = "unverified"
            if row["registered_version"] and row["manifest_version"]:
                row["registered_cache_consistency"] = "match" if row["registered_version"] == row["manifest_version"] else "mismatch"
            row["components"] = sorted(key for key in (manifest or {}) if key in
                {"skills", "agents", "commands", "hooks", "mcpServers", "lspServers", "outputStyles", "workflows"})
            source_manifest, source_status = _plugin_source(identifier, marketplaces or {})
            row["source_manifest_status"] = source_status
            row["source_version"] = _version((source_manifest or {}).get("version"))
            row["source_cache_consistency"] = "unverified"
            if row["source_version"] and row["manifest_version"]:
                row["source_cache_consistency"] = "match" if row["source_version"] == row["manifest_version"] else "mismatch"
            rows.append(row)
    return {"registry_status": status, "marketplace_registry_status": market_status, "entries": rows,
            "entries_capped": len(rows) >= _MAX_ROWS,
            "comparison": "Manifest version metadata only; matching versions do not prove identical cached bytes.",
            "limitations": ["No plugin commands, hooks, MCP definitions or source code are executed or displayed.",
                            "Source comparison covers registered local directory marketplaces only."]}


def _plugin_source(identifier: str, marketplaces: dict) -> tuple[dict | None, str]:
    if not isinstance(identifier, str) or "@" not in identifier:
        return None, "unavailable"
    name, market = identifier.rsplit("@", 1)
    entry = marketplaces.get(market)
    if not isinstance(entry, dict):
        return None, "unavailable"
    source = entry.get("source")
    if not isinstance(source, dict) or source.get("source") != "directory":
        return None, "not_local_directory"
    location = source.get("path")
    if not isinstance(location, str) or not Path(location).is_absolute():
        return None, "unknown_path"
    root = Path(location)
    market_manifest, status = _read(root / ".claude-plugin/marketplace.json")
    plugins = (market_manifest or {}).get("plugins")
    if not isinstance(plugins, list):
        return None, status if status != "read" else "malformed"
    for plugin in plugins[:_MAX_ROWS]:
        if not isinstance(plugin, dict) or plugin.get("name") != name:
            continue
        relative = plugin.get("source")
        if not isinstance(relative, str) or not relative.startswith("./"):
            return None, "not_relative_directory"
        try:
            path = (root / relative).resolve()
        except (OSError, RuntimeError):
            return None, "unreadable"
        # A local marketplace may intentionally point to a sibling source tree;
        # read only that source's fixed plugin manifest, never its configuration.
        return _read(path / ".claude-plugin/plugin.json")
    return None, "not_registered"


def explain_configuration(project: Path, *, home: Path | None = None, env: dict | None = None) -> dict:
    """Return allowlisted candidate sources without claiming full native parity."""
    project = Path(project).resolve()
    home = Path.home() if home is None else Path(home)
    env = dict(os.environ if env is None else env)
    claude_root = _root(env.get("CLAUDE_CONFIG_DIR"), home / ".claude", home)
    codex_root = _root(env.get("CODEX_HOME"), home / ".codex", home)
    claude_label = "$CLAUDE_CONFIG_DIR" if env.get("CLAUDE_CONFIG_DIR") else "~/.claude"
    codex_label = "$CODEX_HOME" if env.get("CODEX_HOME") else "~/.codex"
    claude = _configuration([(claude_root / "settings.json", claude_label + "/settings.json"),
                             (project / ".claude/settings.json", "$PROJECT/.claude/settings.json"),
                             (project / ".claude/settings.local.json", "$PROJECT/.claude/settings.local.json")],
                            ("model", "effortLevel", "disableAllHooks", "allowManagedHooksOnly", "disableAutoMemory"))
    claude["process_model_environment"] = {key: _setting_value(key, env[key]) for key in _MODEL_ENV if key in env}
    codex = _configuration([(codex_root / "config.toml", codex_label + "/config.toml"),
                            (project / ".codex/config.toml", "$PROJECT/.codex/config.toml")],
                           ("model", "model_reasoning_effort", "approval_policy", "sandbox_mode", "model_provider"), toml=True)
    return {"scope": "partial", "network": "none", "authentication_files_read": False,
            "claude": claude, "codex": codex,
            "instructions": _instruction_preview(project, home, claude_root, codex_root),
            "plugins": _plugin_inventory(claude_root, project, home),
            "limitations": ["Managed/remote policy, native trust, CLI overrides, profiles and complete merge rules are not inspected.",
                            "Scalar selections are inferred candidates, not observed effective native settings.",
                            "Environment and settings candidates remain separate; authentication and provider endpoints are not inspected."]}
