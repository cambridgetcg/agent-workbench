"""Read-only, redacted diagnostics; never refresh credentials or request inference."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

from .mcp import _hermes_home, mcp_inventory


_LIMIT = 2 * 1024 * 1024
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "ultracode"}
_MODEL = re.compile(r"(?:(?:gpt-[0-9]|claude-)[A-Za-z0-9._():-]*|(?:o[134]|opus|opusplan|sonnet|haiku|fable|default)(?:-[A-Za-z0-9._():-]+)?)(?:\[(?:1m|200k)\])?\Z")
_CODEX_KEYS = ("model", "model_reasoning_effort", "model_provider", "approval_policy", "sandbox_mode")
_MODEL_ENV = ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
              "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL")
_AUTH_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
# Inherited by Codex and Hermes tasks; presence only, never values.
_CODEX_AUTH_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY")


def _load(path: Path, *, toml: bool = False) -> tuple[dict | None, str]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None, "not_regular"
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(_LIMIT + 1)
        finally:
            os.close(fd)
        if len(data) > _LIMIT:
            return None, "too_large"
        parsed = tomllib.loads(data.decode("utf-8")) if toml else json.loads(data)
        return (parsed, "read") if isinstance(parsed, dict) else (None, "malformed")
    except FileNotFoundError:
        return None, "absent"
    except (ValueError, TypeError, UnicodeError):
        return None, "malformed"
    except OSError:
        return None, "unreadable"


def _model(value):
    if isinstance(value, str) and len(value) <= 96 and _MODEL.fullmatch(value):
        return value
    return "[custom or invalid model withheld]"


def _effort(value):
    return value if isinstance(value, str) and value in _EFFORTS else "[unrecognized effort withheld]"


def _safe_codex(key, value):
    if key == "model":
        return _model(value)
    if key == "model_reasoning_effort":
        return _effort(value)
    permitted = {
        "model_provider": {"openai", "ollama", "lmstudio"},
        "approval_policy": {"untrusted", "on-failure", "on-request", "never"},
        "sandbox_mode": {"read-only", "workspace-write", "danger-full-access"},
    }
    return value if isinstance(value, str) and value in permitted[key] else "[custom or invalid value withheld]"


def _configured_path(value, default, home):
    if not isinstance(value, str) or not value:
        return default
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    path = Path(value)
    return path if path.is_absolute() else home / path


def _token_metadata(token, now):
    result = {"evidence": "decoded locally; signature and server acceptance not verified", "status": "unavailable"}
    if not isinstance(token, str) or not token:
        return result
    result["status"] = "unknown_expiry"
    try:
        pieces = token.split(".")
        if len(pieces) != 3:
            return result
        encoded = pieces[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        expiry = claims["exp"]
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
            return result
        stamp = datetime.fromtimestamp(expiry, timezone.utc).isoformat()
        remaining = math.floor(expiry - now)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, OSError):
        return result
    result.update(expires_at=stamp, seconds_remaining=remaining,
                  status="expired" if remaining <= 0 else "expiring" if remaining < 3600 else "fresh")
    return result


def _version(name, env, home):
    executable = shutil.which(name, path=env.get("PATH", os.defpath))
    if executable is None:
        return {"evidence": "observed", "status": "not_found", "selected_executable": None}
    try:
        selected = "~/" + Path(executable).relative_to(home).as_posix()
    except ValueError:
        selected = str(executable)
    if any("@" in part or part.startswith(("sk-", "sk_", "eyJ")) or len(part) > 100 for part in Path(selected).parts):
        selected = "[custom executable path withheld]"
    provenance = {"evidence": "observed", "selected_executable": selected}
    try:
        completed = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                   timeout=3, env=env, cwd=home, check=False)
        output = (completed.stdout + "\n" + completed.stderr)[:4096]
        match = re.search(r"(?<![\w.])v?(\d{1,4}\.\d{1,4}\.\d{1,4}(?:\.\d{1,4})?)(?![\w.])", output)
        # A version-like string from a failed command is not a reported version.
        status = "version_unavailable" if not match else "reported" if completed.returncode == 0 else "unverified"
        return {**provenance, "status": status,
                "version": match.group(1) if match else None, "exit_code": completed.returncode}
    except subprocess.TimeoutExpired:
        return {**provenance, "status": "timed_out"}
    except (OSError, ValueError, UnicodeError):
        return {**provenance, "status": "check_failed"}


def _codex(home, project, env, now):
    root = _configured_path(env.get("CODEX_HOME"), home / ".codex", home)
    root_label = "$CODEX_HOME" if env.get("CODEX_HOME") else "~/.codex"
    sources = [(root / "config.toml", root_label + "/config.toml")]
    if project is not None:
        sources.append((project / ".codex/config.toml", "$PROJECT/.codex/config.toml"))
    selected, rows = {}, []
    for path, label in sources:
        data, status = _load(path, toml=True)
        values = {key: _safe_codex(key, data[key]) for key in _CODEX_KEYS if data is not None and key in data}
        rows.append({"source": label, "status": status, "configured": values})
        for key, value in values.items():
            selected[key] = {"value": value, "source": label}
    auth, auth_status = _load(root / "auth.json")
    auth = auth or {}
    tokens = auth.get("tokens")
    token = tokens.get("access_token") if isinstance(tokens, dict) else None
    mode = auth.get("auth_mode")
    mode = mode if mode in ("chatgpt", "apikey", "api_key") else "unknown"
    authentication = {"source": root_label + "/auth.json", "file_status": auth_status,
                      "configured_mode": mode, "access_token": _token_metadata(token, now),
                      "process_environment": {key: "present (redacted)" if env[key] else "empty"
                                              for key in _CODEX_AUTH_ENV if key in env}}
    return {
        "configuration": {"scope": "partial", "sources": rows, "inferred_selected": selected,
                          "precedence_reviewed": "project file over root file; project trust not established",
                          "limitations": ["System/managed config, ancestor layers, profiles, CLI overrides, and native session state are not evaluated.",
                                          "A project file may be ignored by Codex when the project is untrusted."]},
        "authentication": authentication,
    }, token if isinstance(token, str) else None


def _loopback(host, port):
    if not isinstance(host, str) or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    if "%" in host:
        return None
    if host.lower() == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not address.is_loopback:
        return None
    return str(address), port


def _endpoint(value):
    if not isinstance(value, str):
        return "[invalid endpoint withheld]"
    try:
        parsed = urllib.parse.urlsplit(value)
        pair = _loopback(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        if parsed.scheme in {"http", "https"} and pair:
            host, port = pair
            return f"{parsed.scheme}://[{host}]:{port}" if ":" in host else f"{parsed.scheme}://{host}:{port}"
    except ValueError:
        pass
    return "[external or invalid endpoint withheld]"


def _safe_env(env):
    result = {key: _model(env[key]) for key in _MODEL_ENV if key in env}
    if "CLAUDE_CODE_EFFORT_LEVEL" in env:
        result["CLAUDE_CODE_EFFORT_LEVEL"] = _effort(env["CLAUDE_CODE_EFFORT_LEVEL"])
    if "ANTHROPIC_BASE_URL" in env:
        result["ANTHROPIC_BASE_URL"] = _endpoint(env["ANTHROPIC_BASE_URL"])
    for key in _AUTH_ENV:
        if key in env:
            result[key] = "present (redacted)" if env[key] else "empty"
    return result


def _claude(home, project, env):
    root = _configured_path(env.get("CLAUDE_CONFIG_DIR"), home / ".claude", home)
    label = "$CLAUDE_CONFIG_DIR" if env.get("CLAUDE_CONFIG_DIR") else "~/.claude"
    sources = [(root / "settings.json", label + "/settings.json")]
    if project is not None:
        sources += [(project / ".claude/settings.json", "$PROJECT/.claude/settings.json"),
                    (project / ".claude/settings.local.json", "$PROJECT/.claude/settings.local.json")]
    rows, selected, selected_env = [], {}, {}
    for path, label in sources:
        data, status = _load(path)
        data = data or {}
        values = {}
        if "model" in data:
            values["model"] = _model(data["model"])
        if "effortLevel" in data:
            values["effortLevel"] = _effort(data["effortLevel"])
        settings_env = _safe_env(data["env"]) if isinstance(data.get("env"), dict) else {}
        rows.append({"source": label, "status": status, "configured": values, "configured_environment": settings_env})
        for key, value in values.items():
            selected[key] = {"value": value, "source": label}
        for key, value in settings_env.items():
            selected_env[key] = {"value": value, "source": label + " env"}
    return {"configuration": {
        "scope": "partial", "sources": rows, "inferred_selected_settings": selected,
        "inferred_selected_settings_environment": selected_env,
        "configured_process_environment": {key: {"value": value, "source": "doctor process environment"}
                                            for key, value in _safe_env(env).items()},
        "precedence_reviewed": "project local settings over project settings over user settings",
        "limitations": ["Managed policy, command-line --settings/--effort/--model, remote settings, and other processes' environments are not inspected.",
                        "Settings and inherited environment candidates are shown separately; final native precedence is not observed."]}}


def _listener(pair, env, home):
    if pair is None:
        return {"status": "not_checked", "reason": "endpoint is not configured as loopback"}
    executable = shutil.which("lsof", path=env.get("PATH", os.defpath))
    if executable is None:
        return {"status": "unknown", "reason": "lsof unavailable"}
    host, port = pair
    host = f"[{host}]" if ":" in host else host
    try:
        result = subprocess.run([executable, "-nP", "-a", f"-iTCP@{host}:{port}", "-sTCP:LISTEN", "-Fpc"],
                                capture_output=True, text=True, timeout=3, env=env, cwd=home, check=False)
        pids = {line[1:] for line in result.stdout.splitlines() if re.fullmatch(r"p[0-9]+", line)}
        if result.returncode not in (0, 1) or (result.stderr and not pids):
            return {"status": "unknown", "reason": "listener inspection failed"}
        return {"status": "listening" if pids else "not_listening", "process_count": len(pids),
                "cliproxyapi_process_seen": any(line.startswith("ccliproxyapi") for line in result.stdout.splitlines()),
                "limitation": "Listener ownership of this configuration is not established."}
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return {"status": "unknown", "reason": "listener inspection failed or timed out"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _probe(pair, config):
    if pair is None:
        return {"status": "blocked", "reason": "only numeric loopback endpoints are allowed"}
    keys = config.get("api-keys")
    key = keys[0] if isinstance(keys, list) and keys else None
    if not isinstance(key, str) or not key or len(key) > 8192 or "\n" in key or "\r" in key:
        return {"status": "not_probed", "reason": "no usable private client key"}
    host, port = pair
    host = f"[{host}]" if ":" in host else host
    url = f"http://{host}:{port}/v1/models"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    try:
        with opener.open(request, timeout=2) as response:
            body = response.read(65537)
            result = {"status": "responded", "http_status": response.status, "endpoint": "/v1/models"}
        if len(body) <= 65536:
            try:
                payload = json.loads(body)
                data = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(data, list):
                    models = [_model(item.get("id")) for item in data[:100] if isinstance(item, dict)]
                    result["advertised_models"] = sorted(set(models))[:12]
                    result["model_count"] = len(data)
            except (ValueError, TypeError, UnicodeError):
                pass
        return result
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        return {"status": "responded", "http_status": code, "endpoint": "/v1/models",
                "redirect_followed": False}
    except (OSError, ValueError, urllib.error.URLError):
        return {"status": "unreachable", "endpoint": "/v1/models"}


def _payload_effort(config, model):
    payload = config.get("payload")
    rules = payload.get("override") if isinstance(payload, dict) else None
    effort = None
    if isinstance(rules, list):
        for rule in rules[:100]:
            if not isinstance(rule, dict) or not isinstance(rule.get("models"), list):
                continue
            matches = any(isinstance(item, dict) and item.get("name") in (model, "*") for item in rule["models"])
            params = rule.get("params")
            if matches and isinstance(params, dict) and "reasoning.effort" in params:
                effort = _effort(params["reasoning.effort"])
    return effort


def _astra(home, env, token, now, probe):
    try:
        paths = sorted((home / ".local/state/claude-astra").glob("session-*/config.json"))
    except OSError:
        return {"scan_status": "unreadable", "sessions": []}
    rows = []
    for number, path in enumerate(paths[:50], 1):
        config, status = _load(path)
        row = {"source": f"Astra session config {number}", "file_status": status}
        rows.append(row)
        if config is None:
            continue
        pair = _loopback(config.get("host"), config.get("port"))
        providers = config.get("codex-api-key")
        provider = providers[0] if isinstance(providers, list) and providers and isinstance(providers[0], dict) else {}
        models = provider.get("models")
        first = models[0] if isinstance(models, list) and models and isinstance(models[0], dict) else {}
        model = first.get("name")
        effort = _payload_effort(config, model) if isinstance(model, str) else None
        settings, settings_status = _load(path.parent / "claude-settings.json")
        settings_env = settings.get("env") if settings else None
        ui = _effort(settings_env["CLAUDE_CODE_EFFORT_LEVEL"]) if isinstance(settings_env, dict) and "CLAUDE_CODE_EFFORT_LEVEL" in settings_env else None
        copied = provider.get("api-key")
        freshness = _token_metadata(copied, now)
        comparable = isinstance(copied, str) and bool(copied) and bool(token)
        same = copied == token if comparable else None
        row["configured"] = {"endpoint": f"loopback port {pair[1]}" if pair else "non-loopback or invalid (withheld)",
                             "model": _model(model), "payload_reasoning_effort_override": effort,
                             "claude_ui_effort": ui, "claude_settings_status": settings_status,
                             "access_token": freshness}
        row["inferred"] = {"token_copy": "matches_current_codex" if same else "differs_from_current_codex; may be stale" if comparable else "not_comparable",
                           "preset": "matches max reasoning + xhigh UI Ultra preset settings" if effort == "max" and ui == "xhigh" else "not identified",
                           "ultracode_workflow": "not observed; native command-line/session state not inspected"}
        row["observed"] = {"listener": _listener(pair, env, home),
                           "health": _probe(pair, config) if probe else {"status": "not_requested"},
                           "backend_reasoning_effort": "not observed; no inference requests made"}
    return {"scan_status": "read", "sessions": rows, "truncated": len(paths) > 50,
            "limitation": "Session files can outlive a process; listener/health evidence is separate. Only the first Codex provider/model and exact or wildcard payload model selectors are reviewed."}


def _hermes(home, env):
    # Hermes --version can perform an upstream update check. Default doctor
    # reports executable presence without invoking it or importing Hermes.
    executables = {}
    for name in ("hermes", "hermes-astra-ultra"):
        path = shutil.which(name, path=env.get("PATH", os.defpath))
        if path is None:
            executables[name] = {"status": "not_found"}
            continue
        try:
            selected = "~/" + Path(path).relative_to(home).as_posix()
        except ValueError:
            selected = path
        if len(selected) > 512 or any(ord(c) < 32 for c in selected) or any(
                "@" in part or part.startswith(("sk-", "sk_", "eyJ")) or len(part) > 100
                for part in Path(selected).parts):
            selected = "[custom executable path withheld]"
        executables[name] = {"status": "present", "selected_executable": selected}
    # HERMES_HOME can be either an installation root or an already-selected
    # named profile. Inspect Astra Ultra only within that same installation.
    _, root = _hermes_home(env, home)
    profile = root / "profiles/astra-ultra"
    binding = profile / "workbench-subscription.json"
    auth = {"status": "not_configured", "refresh_performed": False}
    if binding.exists():
        from .codex_token import borrow_token, TokenError
        try:
            auth = borrow_token(binding, check=True, env=env)
        except TokenError as error:
            auth = {"status": str(error), "refresh_performed": False}
    return {"executables": executables, "astra_ultra_profile_present": profile.is_dir(),
            "subscription_binding": auth,
            "limitations": ["Native version probes are opt-in through explain because Hermes can check for updates.",
                            "Profile configuration, tool readiness and actual request effort are not inspected here."]}


def diagnose(*, project: Path | None = None, home: Path | None = None,
             env: dict | None = None, probe: bool = False) -> dict:
    """Collect allowlisted metadata without authentication changes or inference."""
    environment = dict(os.environ if env is None else env)
    home = Path(home) if home is not None else Path(environment.get("HOME", Path.home()))
    project = Path(project) if project is not None else None
    environment["HOME"] = str(home)
    now = time.time()
    codex, token = _codex(home, project, environment, now)
    return {
        "version": 1,
        "scope": {"read_only": True, "project_supplied": project is not None,
                  "network": "optional loopback /v1/models only" if probe else "none",
                  "inference_requests": False, "auth_refresh": False},
        "versions": {name: _version(name, environment, home) for name in ("codex", "claude", "cliproxyapi")},
        "codex": codex,
        "claude": _claude(home, project, environment),
        "astra": _astra(home, environment, token, now, probe),
        "hermes": _hermes(home, environment),
        # Astra wrappers read Claude's configuration, so Claude's row covers them.
        "mcp_servers": {harness: mcp_inventory(harness, project, home=home, env=environment)
                        for harness in ("codex", "claude", "hermes", "hermes-astra-ultra")},
    }


def format_report(report) -> str:
    """Format only the already-redacted report returned by diagnose."""
    lines = ["Agent Workbench doctor — read-only; no inference or auth refresh"]
    for name, version in report.get("versions", {}).items():
        executable = version.get("selected_executable")
        label = version.get("version") or version.get("status", "unknown")
        if version.get("version") and version.get("status") != "reported":
            label += f" ({version.get('status')}, exit {version.get('exit_code')})"
        lines.append(f"Observed {name}: {label}" + (f" — {executable}" if executable else ""))
    hermes = report.get("hermes", {})
    for name, entry in hermes.get("executables", {}).items():
        lines.append(f"Selected {name}: {entry.get('selected_executable') or entry.get('status')}")
    if hermes.get("astra_ultra_profile_present"):
        lines.append("Hermes Astra Ultra subscription binding: " +
                     hermes.get("subscription_binding", {}).get("status", "unknown") +
                     " (local metadata only; no refresh)")
    auth = report.get("codex", {}).get("authentication", {})
    freshness = auth.get("access_token", {})
    expiry = f"; expires {freshness['expires_at']}" if "expires_at" in freshness else ""
    lines.append(f"Codex file auth: {auth.get('configured_mode', 'unknown')}; {freshness.get('status', 'unavailable')}{expiry} (locally decoded, not server-verified)")
    for key, presence in auth.get("process_environment", {}).items():
        lines.append(f"Codex auth environment: {key} {presence}")
    for harness, key in (("codex", "inferred_selected"), ("claude", "inferred_selected_settings")):
        config = report.get(harness, {}).get("configuration", {})
        lines.append(f"{harness.title()} configuration: partial view")
        for source in config.get("sources", []):
            if source.get("status") not in ("read", "absent"):
                lines.append(f"  {source['source']}: {source['status']}")
        for name, entry in config.get(key, {}).items():
            lines.append(f"  Inferred {name}: {entry['value']} — {entry['source']}")
        if harness == "claude":
            for collection in ("inferred_selected_settings_environment", "configured_process_environment"):
                for name, entry in config.get(collection, {}).items():
                    lines.append(f"  Configured {name}: {entry['value']} — {entry['source']}")
        for limitation in config.get("limitations", []):
            lines.append(f"  {limitation}")
    sessions = report.get("astra", {}).get("sessions", [])
    lines.append(f"Astra session configurations: {len(sessions)} (files alone do not prove an active session)")
    inactive = [row for row in sessions if row.get("configured") is not None
                and row.get("observed", {}).get("listener", {}).get("status") == "not_listening"
                and row.get("observed", {}).get("health", {}).get("status") != "responded"]
    if inactive:
        expired = sum(row["configured"]["access_token"]["status"] == "expired" for row in inactive)
        lines.append(f"  Inactive configurations: {len(inactive)} ({expired} with expired token copies); details available in JSON.")
    for row in sessions:
        if row in inactive:
            continue
        configured = row.get("configured")
        if configured is None:
            lines.append(f"  {row['source']}: {row['file_status']}")
            continue
        observed = row["observed"]
        lines.append(f"  {row['source']}: configured {configured['model']}, {configured['endpoint']}; token {configured['access_token']['status']}")
        lines.append(f"    Configured GPT effort override: {configured['payload_reasoning_effort_override'] or 'none'}; Claude UI effort: {configured['claude_ui_effort'] or 'not found'}")
        lines.append(f"    Inferred token copy: {row['inferred']['token_copy']}; {row['inferred']['preset']}")
        lines.append(f"    Observed listener: {observed['listener']['status']}; health: {observed['health']['status']}" +
                     (f" (HTTP {observed['health']['http_status']})" if "http_status" in observed["health"] else ""))
        lines.append("    Ultracode activation and actual backend reasoning effort: not observed.")
    for harness, inventory in report.get("mcp_servers", {}).items():
        names = dict.fromkeys(server["name"] + (" (disabled)" if server.get("enabled") is False else "")
                              for row in inventory.get("sources", []) for server in row.get("servers", []))
        line = f"MCP servers for {harness} (configured names only): {', '.join(names) or 'none'} [{inventory.get('status')}]"
        warnings = inventory.get("warnings", [])
        if warnings:
            line += f"; {warnings[0]}" + (f" (+{len(warnings) - 1} more in JSON)" if len(warnings) > 1 else "")
        lines.append(line)
    return "\n".join(lines)
