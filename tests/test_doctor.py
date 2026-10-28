import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from agent_workbench import doctor


def jwt(expiry, **extra):
    claims = {"exp": expiry, "email": "private-person@example.invalid",
              "https://api.openai.com/auth": {"chatgpt_account_id": "private-account-id"}, **extra}
    return "eyJhbGciOiJub25lIn0." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".private-signature"


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.project = self.home / "project"
        self.project.mkdir()
        self.env = {"PATH": "/synthetic/bin"}
        self.calls = []

        def fake_run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            if argv[1] == "--version":
                return subprocess.CompletedProcess(argv, 0, "client 1.2.3 private-version-secret", "")
            return subprocess.CompletedProcess(argv, 1, "", "")

        self.which = patch.object(doctor.shutil, "which", side_effect=lambda name, **_kw: "/synthetic/bin/" + name)
        self.run = patch.object(doctor.subprocess, "run", side_effect=fake_run)
        self.which.start()
        self.run.start()
        self.addCleanup(self.which.stop)
        self.addCleanup(self.run.stop)

    def write(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def auth(self, token=None):
        token = token or jwt(time.time() + 86400)
        self.write(".codex/auth.json", {"auth_mode": "chatgpt", "tokens": {"access_token": token,
                   "refresh_token": "private-refresh-token", "account_id": "private-account-id"}})
        return token

    def session(self, token=None, host="127.0.0.1", port=45678):
        token = token or jwt(time.time() + 7200)
        config = {"host": host, "port": port, "api-keys": ["synthetic-private-client-key"],
                  "codex-api-key": [{"api-key": token, "headers": {"ChatGPT-Account-ID": "private-account-id"},
                                     "models": [{"name": "gpt-6-astra"}]}],
                  "payload": {"override": [{"models": [{"name": "gpt-6-astra"}],
                                             "params": {"reasoning.effort": "max"}}]}}
        self.write(".local/state/claude-astra/session-test/config.json", config)
        self.write(".local/state/claude-astra/session-test/claude-settings.json",
                   {"env": {"CLAUDE_CODE_EFFORT_LEVEL": "xhigh", "ANTHROPIC_AUTH_TOKEN": "synthetic-private-client-key"}})
        return config

    def diagnose(self, **kwargs):
        return doctor.diagnose(home=self.home, project=self.project, env=self.env, **kwargs)

    def test_read_only_default_and_redaction(self):
        token = self.auth()
        self.session(token)
        self.env.update(ANTHROPIC_API_KEY="private-env-api-key", PRIVATE_OTHER="private-arbitrary-environment")
        before = {str(path.relative_to(self.home)): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        with patch.object(doctor.urllib.request, "build_opener", side_effect=AssertionError("No default network")):
            report = self.diagnose()
        after = {str(path.relative_to(self.home)): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        rendered = json.dumps(report) + doctor.format_report(report)
        for secret in (token, "private-refresh-token", "private-account-id", "private-person@example.invalid",
                       "private-env-api-key", "private-arbitrary-environment", "synthetic-private-client-key", "private-version-secret"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(report["scope"]["network"], "none")
        self.assertEqual(report["codex"]["authentication"]["access_token"]["status"], "fresh")
        self.assertTrue(all(kwargs["timeout"] == 3 and isinstance(argv, list) and not kwargs.get("shell")
                            for argv, kwargs in self.calls))

    def test_text_report_lists_agenttool_tools_by_harness(self):
        self.write(".codex/config.toml", '[mcp_servers.agenttool]\nurl = "https://api.agenttool.dev/v1/mcp"\n')
        self.write(".hermes/config.yaml", "mcp_servers:\n  agenttool_canon:\n    url: https://api.agenttool.dev/v1/mcp/canon\n")
        before = {str(path.relative_to(self.home)): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        report = self.diagnose()
        after = {str(path.relative_to(self.home)): path.read_bytes() for path in self.home.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        section = report["agenttool"]
        self.assertEqual(section["harnesses"]["codex"]["summary"]["public_agenttool"], "configured")
        self.assertEqual(section["harnesses"]["hermes"]["summary"]["public_canon"], "configured")
        text = doctor.format_report(report)
        self.assertIn("AgentTool tools by harness", text)
        for harness in ("codex", "claude", "hermes", "hermes-astra-ultra"):
            self.assertIn(f"  {harness}: ", text)
        self.assertIn("public agenttool all tools (no allowlist)", text)
        self.assertTrue(all(argv[1] == "--version" for argv, _ in self.calls))

    def test_codex_partial_config_precedence_and_provenance(self):
        self.write(".codex/config.toml", 'model = "gpt-6-astra"\nmodel_reasoning_effort = "xhigh"\napproval_policy = "on-request"\n')
        self.write("project/.codex/config.toml", 'model = "gpt-6-sol"\nmodel_reasoning_effort = "max"\n')
        config = self.diagnose()["codex"]["configuration"]
        self.assertEqual(config["scope"], "partial")
        self.assertEqual(config["inferred_selected"]["model"], {"value": "gpt-6-sol", "source": "$PROJECT/.codex/config.toml"})
        self.assertEqual(config["inferred_selected"]["approval_policy"]["source"], "~/.codex/config.toml")
        self.assertIn("untrusted", " ".join(config["limitations"]))

    def test_hermes_binding_diagnostics_never_launch_or_refresh(self):
        from agent_workbench import codex_token
        token = self.auth(jwt(time.time() - 100))
        binding = self.write(".hermes/profiles/astra-ultra/workbench-subscription.json",
                             {"expected_account_id": "private-account-id",
                              "codex_home": str(self.home / ".codex")})
        binding.chmod(0o600)
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        with patch.object(codex_token, "_official_refresh", side_effect=AssertionError("No refresh")):
            report = self.diagnose()
        after = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        hermes = report["hermes"]
        self.assertTrue(hermes["astra_ultra_profile_present"])
        self.assertEqual(hermes["subscription_binding"]["status"], "refresh_needed")
        self.assertFalse(hermes["subscription_binding"]["refresh_performed"])
        self.assertEqual(hermes["executables"]["hermes"]["status"], "present")
        self.assertFalse(any("hermes" in argv[0] for argv, _ in self.calls))
        rendered = json.dumps(report) + doctor.format_report(report)
        for secret in (token, "private-account-id", "private-refresh-token", str(binding)):
            self.assertNotIn(secret, rendered)

    def test_hermes_changed_account_and_insecure_binding_are_metadata_only(self):
        self.auth()
        binding = self.write(".hermes/profiles/astra-ultra/workbench-subscription.json",
                             {"expected_account_id": "different-private-account",
                              "codex_home": str(self.home / ".codex")})
        binding.chmod(0o600)
        report = self.diagnose()
        self.assertEqual(report["hermes"]["subscription_binding"]["status"], "account_changed")
        self.assertNotIn("different-private-account", json.dumps(report))
        binding.chmod(0o644)
        self.assertEqual(self.diagnose()["hermes"]["subscription_binding"]["status"], "binding_invalid")

    def test_config_root_overrides_and_withheld_custom_values(self):
        self.env.update(CODEX_HOME=str(self.home / "other"), CLAUDE_CONFIG_DIR=str(self.home / "claude-other"))
        self.write("other/config.toml", 'model = "private-person@example.invalid"\nmodel_provider = "private-provider-id"\n')
        self.write("claude-other/settings.json", {"model": "claude-opus-4-6"})
        report = self.diagnose()
        self.assertEqual(report["codex"]["configuration"]["sources"][0]["source"], "$CODEX_HOME/config.toml")
        self.assertEqual(report["claude"]["configuration"]["sources"][0]["source"], "$CLAUDE_CONFIG_DIR/settings.json")
        self.assertNotIn("private-provider-id", json.dumps(report))
        self.assertNotIn("private-person@example.invalid", json.dumps(report))

    def test_claude_sources_and_environment_remain_distinct(self):
        self.write(".claude/settings.json", {"model": "claude-sonnet-4-6", "effortLevel": "low",
                                              "env": {"ANTHROPIC_AUTH_TOKEN": "private-settings-token"}})
        self.write("project/.claude/settings.json", {"model": "gpt-6-astra", "effortLevel": "high"})
        self.write("project/.claude/settings.local.json", {"effortLevel": "max", "env": {"CLAUDE_CODE_EFFORT_LEVEL": "xhigh"}})
        self.env.update(CLAUDE_CODE_EFFORT_LEVEL="low", ANTHROPIC_BASE_URL="https://user:private-password@private.example.invalid/path")
        config = self.diagnose()["claude"]["configuration"]
        self.assertEqual(config["inferred_selected_settings"]["effortLevel"]["value"], "max")
        self.assertEqual(config["inferred_selected_settings_environment"]["CLAUDE_CODE_EFFORT_LEVEL"]["value"], "xhigh")
        self.assertEqual(config["configured_process_environment"]["CLAUDE_CODE_EFFORT_LEVEL"]["value"], "low")
        self.assertNotIn("private-password", json.dumps(config))
        self.assertNotIn("private.example.invalid", json.dumps(config))
        self.assertIn("not observed", " ".join(config["limitations"]))

    def test_recognized_context_alias_is_not_redacted_as_a_secret(self):
        self.write(".claude/settings.json", {"model": "opus[1m]"})
        report = self.diagnose()
        self.assertEqual(report["claude"]["configuration"]["inferred_selected_settings"]["model"]["value"], "opus[1m]")

    def test_expired_and_different_proxy_token_copy(self):
        self.auth(jwt(time.time() + 86400))
        self.session(jwt(time.time() - 3600))
        row = self.diagnose()["astra"]["sessions"][0]
        self.assertEqual(row["configured"]["access_token"]["status"], "expired")
        self.assertIn("may be stale", row["inferred"]["token_copy"])
        self.assertEqual(row["configured"]["payload_reasoning_effort_override"], "max")
        self.assertEqual(row["configured"]["claude_ui_effort"], "xhigh")
        self.assertIn("not observed", row["observed"]["backend_reasoning_effort"])
        self.assertIn("not observed", row["inferred"]["ultracode_workflow"])

    def test_malformed_files_and_auth_shapes_are_reported(self):
        self.write(".codex/config.toml", 'model = [broken')
        self.write(".claude/settings.json", '["not an object"]')
        self.write(".local/state/claude-astra/session-bad/config.json", "not json")
        for auth in ([1], {"tokens": None}, {"tokens": {"access_token": 42}}, {"tokens": {"access_token": jwt(None)}},
                     {"tokens": {"access_token": jwt("private-expiry-secret")}}, {"tokens": {"access_token": jwt(float("nan"))}}):
            with self.subTest(auth_type=type(auth).__name__):
                self.write(".codex/auth.json", auth)
                report = self.diagnose()
                self.assertIn(report["codex"]["authentication"]["access_token"]["status"], ("unavailable", "unknown_expiry"))
                self.assertEqual(report["codex"]["configuration"]["sources"][0]["status"], "malformed")
                self.assertEqual(report["astra"]["sessions"][0]["file_status"], "malformed")
                self.assertNotIn("private-expiry-secret", doctor.format_report(report))

    def test_malformed_nested_proxy_shapes_do_not_crash(self):
        path = self.write(".local/state/claude-astra/session-bad/config.json", {})
        for config in ({"codex-api-key": [None]}, {"codex-api-key": {"private": "secret"}},
                       {"codex-api-key": [{"models": [None], "api-key": {"unexpected": "secret"}}], "payload": [1]},
                       {"host": [1], "port": "private-port", "payload": {"override": [None]}}):
            path.write_text(json.dumps(config))
            report = self.diagnose(probe=True)
            self.assertEqual(report["astra"]["sessions"][0]["observed"]["health"]["status"], "blocked")

    def test_versions_missing_timeout_and_listener_failure(self):
        with patch.object(doctor.shutil, "which", return_value=None):
            report = self.diagnose()
            self.assertEqual(report["versions"]["codex"]["status"], "not_found")
        with patch.object(doctor.subprocess, "run", side_effect=subprocess.TimeoutExpired(["synthetic"], 3)):
            self.session()
            report = self.diagnose()
            self.assertEqual(report["versions"]["codex"]["status"], "timed_out")
            self.assertEqual(report["astra"]["sessions"][0]["observed"]["listener"]["status"], "unknown")

    def test_version_from_failed_command_is_unverified_and_codex_keys_are_presence_only(self):
        def failing(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 2, "cliproxyapi 7.2.145", "unknown flag")
        with patch.object(doctor.subprocess, "run", side_effect=failing):
            report = doctor.diagnose(home=self.home, project=self.project,
                                     env={**self.env, "OPENAI_API_KEY": "sk-private-openai-key", "CODEX_API_KEY": ""})
        self.assertEqual(report["versions"]["cliproxyapi"]["status"], "unverified")
        self.assertEqual(report["versions"]["cliproxyapi"]["exit_code"], 2)
        self.assertEqual(report["codex"]["authentication"]["process_environment"],
                         {"OPENAI_API_KEY": "present (redacted)", "CODEX_API_KEY": "empty"})
        self.assertNotIn("sk-private-openai-key", json.dumps(report))
        self.assertNotIn("sk-private-openai-key", doctor.format_report(report))

    def test_text_report_shows_unverified_versions_and_codex_key_presence(self):
        def failing(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 2, "cliproxyapi 7.2.145", "unknown flag")
        with patch.object(doctor.subprocess, "run", side_effect=failing):
            text = doctor.format_report(doctor.diagnose(home=self.home, project=self.project,
                                                        env={**self.env, "OPENAI_API_KEY": "sk-private-openai-key"}))
        self.assertIn("Observed cliproxyapi: 7.2.145 (unverified, exit 2)", text)
        self.assertIn("Codex auth environment: OPENAI_API_KEY present (redacted)", text)
        self.assertNotIn("sk-private-openai-key", text)

    def test_version_provenance_identifies_selected_binary_without_home_path(self):
        with patch.object(doctor.shutil, "which", side_effect=lambda name, **_kw: str(self.home / ".local/bin" / name)):
            report = self.diagnose()
        self.assertEqual(report["versions"]["claude"]["selected_executable"], "~/.local/bin/claude")
        self.assertIn("Observed claude: 1.2.3 — ~/.local/bin/claude", doctor.format_report(report))
        self.assertNotIn(str(self.home), doctor.format_report(report))
        external = self.diagnose()["versions"]["claude"]
        self.assertEqual(external["selected_executable"], "/synthetic/bin/claude")

    def test_listener_metadata_is_observed_without_process_environment(self):
        self.session()
        def fake(argv, **kwargs):
            if argv[1] == "--version":
                return subprocess.CompletedProcess(argv, 0, "1.2.3", "")
            return subprocess.CompletedProcess(argv, 0, "p1234\nccliproxyapi\n", "")
        with patch.object(doctor.subprocess, "run", side_effect=fake):
            report = self.diagnose()
        listener = report["astra"]["sessions"][0]["observed"]["listener"]
        self.assertEqual(listener["status"], "listening")
        self.assertTrue(listener["cliproxyapi_process_seen"])
        self.assertNotIn("1234", json.dumps(report))

    def server(self, *, redirect=None):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path, self.headers.get("Authorization")))
                if redirect:
                    self.send_response(302)
                    self.send_header("Location", redirect)
                    self.end_headers()
                    return
                body = json.dumps({"data": [{"id": "gpt-6-astra"}, {"id": "private-person@example.invalid"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(close)
        return server, received

    def test_optional_probe_uses_only_authenticated_models_endpoint(self):
        server, received = self.server()
        self.session(port=server.server_port)
        self.env["HTTP_PROXY"] = "http://private-proxy.invalid:1234"
        report = self.diagnose(probe=True)
        health = report["astra"]["sessions"][0]["observed"]["health"]
        self.assertEqual(received, [("/v1/models", "Bearer synthetic-private-client-key")])
        self.assertEqual(health["http_status"], 200)
        self.assertIn("gpt-6-astra", health["advertised_models"])
        self.assertNotIn("private-person@example.invalid", json.dumps(report))
        self.assertNotIn("synthetic-private-client-key", json.dumps(report))

    def test_probe_never_follows_redirects_even_to_loopback(self):
        target, target_received = self.server()
        redirect, received = self.server(redirect=f"http://127.0.0.1:{target.server_port}/capture")
        self.session(port=redirect.server_port)
        report = self.diagnose(probe=True)
        health = report["astra"]["sessions"][0]["observed"]["health"]
        self.assertEqual(health["http_status"], 302)
        self.assertFalse(health["redirect_followed"])
        self.assertEqual(len(received), 1)
        self.assertEqual(target_received, [])

    def test_external_or_malformed_hosts_are_never_probed(self):
        for host in ("example.invalid", "0.0.0.0", "192.168.1.1", "127.0.0.1@example.invalid", "http://127.0.0.1"):
            with self.subTest(host=host):
                self.session(host=host)
                with patch.object(doctor.urllib.request, "build_opener", side_effect=AssertionError("Blocked network")):
                    report = self.diagnose(probe=True)
                self.assertEqual(report["astra"]["sessions"][0]["observed"]["health"]["status"], "blocked")

    def test_large_or_nonobject_files_are_bounded(self):
        self.write(".claude/settings.json", " " * (doctor._LIMIT + 1))
        config = self.diagnose()["claude"]["configuration"]
        self.assertEqual(config["sources"][0]["status"], "too_large")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFOs")
    def test_nonregular_config_is_rejected_without_waiting_for_a_writer(self):
        fifo = self.home / "config-fifo"
        os.mkfifo(fifo)
        result = []
        reader = threading.Thread(target=lambda: result.append(doctor._load(fifo)), daemon=True)
        reader.start()
        reader.join(timeout=1)
        self.assertFalse(reader.is_alive(), "Config read blocked waiting for a FIFO writer")
        self.assertEqual(result, [(None, "not_regular")])
        self.assertEqual(doctor._load(self.project), (None, "not_regular"))

    def test_text_compacts_inactive_files_but_keeps_active_unknown_and_malformed(self):
        self.session(jwt(time.time() - 3600))
        report = self.diagnose()
        expired = report["astra"]["sessions"][0]
        expired["source"] = "inactive-expired"
        fresh = json.loads(json.dumps(expired))
        fresh["source"] = "inactive-fresh"
        fresh["configured"]["access_token"]["status"] = "fresh"
        active = json.loads(json.dumps(fresh))
        active["source"] = "active-session"
        active["observed"]["listener"]["status"] = "listening"
        unknown = json.loads(json.dumps(fresh))
        unknown["source"] = "unknown-session"
        unknown["observed"]["listener"]["status"] = "unknown"
        malformed = {"source": "malformed-session", "file_status": "malformed"}
        report["astra"]["sessions"] = [expired, fresh, active, unknown, malformed]
        before = json.dumps(report, sort_keys=True)
        text = doctor.format_report(report)
        self.assertIn("Inactive configurations: 2 (1 with expired token copies)", text)
        self.assertNotIn("inactive-expired", text)
        self.assertNotIn("inactive-fresh", text)
        for source in ("active-session", "unknown-session", "malformed-session"):
            self.assertIn(source, text)
        self.assertEqual(json.dumps(report, sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
