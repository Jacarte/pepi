"""Consented host mcp.json copy: per-server filtering, staging, cleanup and the script flow.

Synthetic HOME and values only; the live ~/.pi/agent/mcp.json is never read.
"""
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.swebench.host_auth import (HOST_MCP_SHA256_ENV, filter_host_mcp, host_mcp, host_mcp_sha256,
                                      run_with_host_auth)
from evals.swebench.mcp import MCP_BIN, validate_mcp_secret
from evals.swebench.tests.test_cli_script import ScriptHarness
from evals.swebench.tests.test_profiles import oauth, prepare, repository

POLICY = {"enabled": True, "commands": ["example-mcp"]}
HEADER = "SYNTHETIC-MCP-HEADER-0123456789"
SPEAKER_KEY = "SYNTHETIC-MCP-SPEAKER-0123456789"
GITHUB_TOKEN = "SYNTHETIC-MCP-GITHUB-0123456789"
VALUES = (HEADER, SPEAKER_KEY, GITHUB_TOKEN, "SYNTHETIC-MCP-CONTEXT7", "/Users/someone/tts2mic-mcp")


def desktop_mcp():
    """Shaped like a real desktop ~/.pi/agent/mcp.json (adapter `disabled`, npx, host paths)."""
    return {"mcpServers": {
        "context7": {"url": "https://mcp.context7.com/mcp", "disabled": True,
                     "headers": {"CONTEXT7_API_KEY": "SYNTHETIC-MCP-CONTEXT7"}},
        "browsermcp": {"command": "npx", "args": ["@browsermcp/mcp@latest"]},
        "playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]},
        "speaker": {"command": "/Users/someone/tts2mic-mcp",
                    "env": {"ELEVENLABS_API_KEY": SPEAKER_KEY, "TTS_LANG": "en"}},
        "voice-devtools": {"command": "bash", "args": ["-c", "run"]},
        "github": {"url": "https://api.githubcopilot.com/mcp", "oauth": False,
                   "headers": {"Authorization": "Bearer " + GITHUB_TOKEN}},
        "neo4j": {"command": "neo4j-mcp", "args": ["--a", "b", "c"]},
        "Chrome": {"command": "npx", "args": ["chrome-devtools-mcp@latest"], "disabled": True},
        "codegraph": {"command": "codegraph", "args": ["serve", "--mcp"]},
    }}


DESKTOP_REASONS = {
    "context7": "disabled in host config", "browsermcp": "npx not provisioned",
    "playwright": "npx not provisioned", "speaker": "host path command",
    "voice-devtools": "bash not provisioned", "github": "oauth is not an object",
    "neo4j": "neo4j-mcp not provisioned", "Chrome": "disabled in host config",
    "codegraph": "codegraph not provisioned",
}


def with_survivors():
    config = desktop_mcp()
    config["mcpServers"]["docs"] = {"url": "https://docs.example.invalid/mcp",
                                    "headers": {"Authorization": HEADER}}
    return config


class HostMcpFilterTests(unittest.TestCase):
    def test_desktop_config_keeps_nothing_and_explains_each_drop(self):
        content, kept, dropped, ignored = filter_host_mcp(json.dumps(desktop_mcp()).encode(),
                                                          {"enabled": True, "commands": []}, "1.1.0")
        self.assertIsNone(content)
        self.assertEqual(kept, [])
        self.assertEqual(dict(dropped), DESKTOP_REASONS)
        self.assertEqual(ignored, [])
        self.assertFalse(any(value in json.dumps(dropped) for value in VALUES))

    def test_survivors_are_copied_verbatim_and_summarized_by_name_only(self):
        config = with_survivors()
        config["mcpServers"]["local"] = {"command": MCP_BIN + "example-mcp", "args": ["/testbed"],
                                         "env": {"TOKEN": SPEAKER_KEY}}
        config["mcpServers"]["bare"] = {"command": "example-mcp"}
        config["mcpServers"]["unlisted"] = {"command": MCP_BIN + "other-mcp"}
        config["mcpServers"]["lookup"] = {"url": "https://x.example.invalid", "headers": {"A": "!gh auth token"}}
        config["mcpServers"]["sse"] = {"url": "https://x.example.invalid/sse", "type": "sse"}
        config["mcpServers"]["broken"] = {"url": "https://[::1/mcp"}
        config["autoEnableCodemode"] = False
        config["imports"] = ["cursor"]
        content, kept, dropped, ignored = filter_host_mcp(json.dumps(config).encode(), POLICY, "1.1.0")
        copied = json.loads(content)
        self.assertEqual(copied, {"mcpServers": {"docs": config["mcpServers"]["docs"],
                                                 "local": config["mcpServers"]["local"]},
                                  "autoEnableCodemode": False})
        validate_mcp_secret(content, POLICY, "1.1.0")
        self.assertEqual(kept, [
            {"name": "docs", "type": "http", "host": "docs.example.invalid", "headers": ["Authorization"]},
            {"name": "local", "type": "stdio", "env": ["TOKEN"]}])
        reasons = dict(dropped)
        self.assertEqual(reasons["bare"], f"example-mcp must be spelled {MCP_BIN}example-mcp")
        self.assertEqual(reasons["unlisted"], "other-mcp is not in the frozen mcp-packages.json")
        self.assertEqual(reasons["lookup"], "headers uses !command")
        self.assertEqual(reasons["sse"], "url is not a streamable HTTP URL")
        self.assertEqual(reasons["broken"], "url is not a streamable HTTP URL")
        self.assertEqual(ignored, ["imports"])
        self.assertFalse(any(value in json.dumps([kept, dropped]) for value in VALUES))

    def test_unprintable_names_and_normalized_collisions(self):
        config = {"mcpServers": {"a-b": {"url": "https://one.example.invalid"},
                                 "a_b": {"url": "https://two.example.invalid"},
                                 "bad name\x1b[31m": {"url": "https://three.example.invalid"}}}
        content, kept, dropped, _ = filter_host_mcp(json.dumps(config).encode(), POLICY, "1.1.0")
        self.assertEqual(list(json.loads(content)["mcpServers"]), ["a-b"])
        self.assertEqual(dropped, [("a_b", "name collides with another server"),
                                   ("<server #3>", "name is not a native Pi identifier")])

    def test_invalid_documents_fail_without_echoing_content(self):
        for content in (b"SYNTHETIC-MCP-not-json", b"[]", b'{"servers": {}}'):
            with self.subTest(content=content), self.assertRaises(ValueError) as caught:
                filter_host_mcp(content, POLICY, "1.1.0")
            self.assertNotIn("SYNTHETIC", str(caught.exception))


class HostMcpAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = repository(base)
        self.run = base / "run"
        prepare(self.root, self.run, profile_name="codex")
        self.home = base / "home"
        self.agent = self.home / ".pi/agent"
        self.agent.mkdir(parents=True)
        (self.agent / "auth.json").write_text(json.dumps(oauth()))
        (self.agent / "auth.json").chmod(0o600)
        self.source = self.agent / "mcp.json"
        self.source.write_text(json.dumps(with_survivors()))
        self.source.chmod(0o600)
        self.capture = base / "capture.json"
        self.inspector = [sys.executable, "-c", (
            "import json, os, pathlib, stat, sys; d = pathlib.Path(sys.argv[sys.argv.index('--secrets-dir') + 1]);"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({'directory': str(d),"
            " 'modes': {p.name: stat.S_IMODE(p.stat().st_mode) for p in d.iterdir()},"
            " 'dir_mode': stat.S_IMODE(d.stat().st_mode), 'digest_env': os.environ.get('PEPI_EVAL_HOST_MCP_SHA256'),"
            " 'mcp': json.loads((d / 'mcp.json').read_text()) if (d / 'mcp.json').exists() else None}))"),
            str(self.capture)]

    def stage(self, env=None, **flags):
        with patch.dict(os.environ, {"HOME": str(self.home), **(env or {})}):
            status = run_with_host_auth(self.run, self.inspector, **flags)
        return status, json.loads(self.capture.read_text()) if self.capture.exists() else None

    def filtered_sha256(self):
        content, *_ = host_mcp(self.run, self.source)
        return host_mcp_sha256(content)

    def test_approved_digest_unchanged_stages_and_is_not_inherited(self):
        approved = self.filtered_sha256()
        status, data = self.stage(env={HOST_MCP_SHA256_ENV: approved}, auth=False, mcp=True,
                                  approved_mcp_sha256=approved)
        self.assertEqual(status, 0)
        self.assertEqual(list(data["mcp"]["mcpServers"]), ["docs"])
        self.assertIsNone(data["digest_env"])

    def test_source_edited_after_approval_exits_2_and_stages_nothing(self):
        approved = self.filtered_sha256()
        edited = with_survivors()
        edited["mcpServers"]["docs"]["headers"]["Authorization"] = "SYNTHETIC-MCP-EDITED-0123456789"
        before = set(Path("/tmp").glob("pepi-eval-auth-*"))
        for name, config in (("edited", edited), ("no survivor", desktop_mcp())):
            with self.subTest(case=name):
                self.source.write_text(json.dumps(config))
                self.capture.unlink(missing_ok=True)
                with patch("sys.stderr") as stderr:
                    status, data = self.stage(auth=True, mcp=True, approved_mcp_sha256=approved)
                self.assertEqual((status, data), (2, None))
                printed = "".join(call.args[0] for call in stderr.write.call_args_list)
                self.assertIn("host mcp.json changed since approval; re-run", printed)
                self.assertEqual(before, set(Path("/tmp").glob("pepi-eval-auth-*")))

    def test_no_survivor_digest_is_defined(self):
        self.assertEqual(host_mcp_sha256(None), host_mcp_sha256(b""))
        self.source.write_text(json.dumps(desktop_mcp()))
        status, data = self.stage(auth=False, mcp=True, approved_mcp_sha256=host_mcp_sha256(None))
        self.assertEqual((status, data["mcp"]), (0, None))

    def test_mcp_only_and_with_auth_share_one_private_directory_then_clean_up(self):
        before = (self.source.read_bytes(), self.source.stat().st_mtime_ns)
        for flags, files in (({"auth": False, "mcp": True}, {"mcp.json": 0o600}),
                             ({"auth": True, "mcp": True}, {"auth.json": 0o600, "mcp.json": 0o600})):
            with self.subTest(flags=flags):
                self.capture.unlink(missing_ok=True)
                status, data = self.stage(**flags)
                self.assertEqual(status, 0)
                self.assertEqual((data["modes"], data["dir_mode"]), (files, 0o700))
                self.assertEqual(list(data["mcp"]["mcpServers"]), ["docs"])
                self.assertFalse(Path(data["directory"]).exists())
                self.assertEqual(before, (self.source.read_bytes(), self.source.stat().st_mtime_ns))

    def test_no_survivor_runs_without_mcp_json(self):
        self.source.write_text(json.dumps(desktop_mcp()))
        status, data = self.stage(auth=False, mcp=True)
        self.assertEqual(status, 0)
        self.assertEqual(data["modes"], {})
        self.assertIsNone(data["mcp"])

    def test_unsafe_sources_and_disabled_policy_fail_closed_before_staging(self):
        target = self.agent / "target.json"
        target.write_text(json.dumps(with_survivors()))
        target.chmod(0o600)
        cases = {"group-writable": lambda: self.source.chmod(0o620),
                 "symlink": lambda: (self.source.unlink(), self.source.symlink_to(target)),
                 "too large": lambda: self.source.write_bytes(b" " * 1048577),
                 "fifo": lambda: (self.source.unlink(), os.mkfifo(self.source, 0o600))}
        for name, mutate in cases.items():
            with self.subTest(case=name):
                if self.source.exists() or self.source.is_symlink():
                    self.source.unlink()
                self.source.write_text(json.dumps(with_survivors()))
                self.source.chmod(0o600)
                mutate()
                with self.assertRaises((ValueError, OSError)):
                    host_mcp(self.run, self.source)
                self.capture.unlink(missing_ok=True)
                self.assertEqual(self.stage(auth=False, mcp=True), (2, None))
        dropped = Path(self.tmp.name) / "dropped"
        prepare(self.root, dropped, profile_name="codex", drop_mcp=True)
        self.source.unlink()
        self.source.write_text(json.dumps(with_survivors()))
        self.source.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "disabled"):
            host_mcp(dropped, self.source)

    def test_readable_by_others_is_reported_not_refused(self):
        self.source.chmod(0o644)
        *_, shared = host_mcp(self.run, self.source)
        self.assertTrue(shared)

    def test_preview_prints_names_and_count_never_values(self):
        self.source.chmod(0o644)
        result = subprocess.run([sys.executable, "-m", "evals.swebench.host_auth", str(self.run), "--mcp-preview"],
                                env={**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"1 {self.filtered_sha256()}")
        self.assertIn("copy docs (http; host docs.example.invalid; headers keys: Authorization)", result.stderr)
        self.assertIn("drop speaker (host path command)", result.stderr)
        self.assertIn("readable by group/others", result.stderr)
        self.assertIn("mode 700, file 600", result.stderr)
        for value in VALUES:
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_adapter_arguments(self):
        from evals.swebench import host_auth
        for argv in (["run"], ["run", "--"], ["run", "--bogus", "--", "x"], ["run", "--mcp", "--mcp", "--", "x"]):
            with self.subTest(argv=argv), patch.object(sys, "argv", ["host_auth", *argv]):
                self.assertEqual(host_auth.main(), 2)
        with patch.object(sys, "argv", ["host_auth", "R", "--", "cmd"]), \
                patch.object(host_auth, "run_with_host_auth", return_value=0) as legacy:
            host_auth.main()
        legacy.assert_called_once_with(Path("R"), ["cmd"], auth=True, mcp=False, approved_mcp_sha256=None,
                                       skills=False, approved_skills_sha256=None)
        for env, approved in (({}, None), ({HOST_MCP_SHA256_ENV: "abc"}, "abc")):
            with self.subTest(env=env), patch.dict(os.environ, env), \
                    patch.object(sys, "argv", ["host_auth", "R", "--mcp", "--", "cmd", "--", "x"]), \
                    patch.object(host_auth, "run_with_host_auth", return_value=0) as mcp_only:
                if not env:
                    os.environ.pop(HOST_MCP_SHA256_ENV, None)
                host_auth.main()
            mcp_only.assert_called_once_with(Path("R"), ["cmd", "--", "x"], auth=False, mcp=True,
                                             approved_mcp_sha256=approved, skills=False,
                                             approved_skills_sha256=None)


INSPECTOR = '''import json, os, pathlib, stat, sys, time
args = sys.argv[1:]
d = pathlib.Path(args[args.index('--secrets-dir') + 1]) if '--secrets-dir' in args else None
pathlib.Path(os.environ['FAKE_CAPTURE']).write_text(json.dumps({
    'directory': str(d) if d else None, 'args': args, 'pid': os.getpid(),
    'dir_mode': stat.S_IMODE(d.stat().st_mode) if d else None,
    'digest_env': os.environ.get('PEPI_EVAL_HOST_MCP_SHA256'),
    'modes': {p.name: stat.S_IMODE(p.stat().st_mode) for p in d.iterdir()} if d else {},
    'mcp': json.loads((d / 'mcp.json').read_text()) if d and (d / 'mcp.json').exists() else None}))
if os.environ.get('FAKE_SLEEP') == '1':
    time.sleep(60)
sys.exit(int(os.environ.get('FAKE_STATUS', '0')))
'''


class HostMcpScriptTests(ScriptHarness):
    def fixture(self, config=None, drop_mcp=False, mode=0o600):
        base = Path(self.runs_tmpdir.name)
        root = repository(base)
        prepare(root, base / "host-run", profile_name="codex", drop_mcp=drop_mcp)
        agent = self.home / ".pi/agent"
        agent.mkdir(parents=True)
        (agent / "auth.json").write_text(json.dumps(oauth()))
        (agent / "auth.json").chmod(0o600)
        self.source = agent / "mcp.json"
        self.source.write_text(json.dumps(with_survivors() if config is None else config))
        self.source.chmod(mode)
        inspector = Path(self.fake.name) / "inspect.py"
        inspector.write_text(INSPECTOR)
        (Path(self.fake.name) / "venv/bin/python").write_text(
            '#!/bin/sh\n[ "$1" = "-m" ] || exit 0\nprintf \'%s\\n\' "$*" >> "$FAKE_LOG"\n'
            'exec python3 "$FAKE_INSPECTOR" "$@"\n')
        self.capture = Path(self.fake.name) / "capture.json"
        # Logs every host python3 argv (preview and adapter) so tests can check what argv carries.
        self.python3_log = Path(self.fake.name) / "python3.log"
        python3 = Path(self.fake.name) / "bin/python3"
        python3.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "{self.python3_log}"\n'
                           f'exec "{shutil.which("python3")}" "$@"\n')
        python3.chmod(0o755)
        return {"PEPI_EVAL_RUN_NAME": "host-run", "PEPI_EVAL_USE_HOST_AUTH": "0",
                "FAKE_INSPECTOR": str(inspector), "FAKE_CAPTURE": str(self.capture)}

    def filtered_sha256(self):
        content, *_ = host_mcp(Path(self.runs_tmpdir.name) / "host-run", self.source)
        return host_mcp_sha256(content)

    def edit_docs_header(self):
        edited = with_survivors()
        edited["mcpServers"]["docs"]["headers"]["Authorization"] = "SYNTHETIC-MCP-EDITED-0123456789"
        self.source.write_text(json.dumps(edited))

    def captured(self):
        return json.loads(self.capture.read_text())

    def assert_no_values(self, *texts):
        joined = "".join(texts) + (self.log.read_text() if self.log.exists() else "")
        for value in VALUES:
            self.assertNotIn(value, joined)

    def tty(self, env, answers):
        master, slave = pty.openpty()
        process = subprocess.Popen(["bash", str(self.script_path), "-y", "run"], stdin=slave, stdout=slave,
                                   stderr=slave, cwd=self.launch, env=self.script_env(env))
        os.close(slave)
        output, pending = b"", dict(answers)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                    for prompt in [p for p in pending if p.encode() in output]:
                        self.assertFalse(self.capture.exists(), "no copy before consent")
                        answer = pending.pop(prompt)
                        answer = answer() if callable(answer) else answer  # may edit files first
                        os.write(master, answer.encode() + b"\n")
                if process.poll() is not None:
                    break
            process.wait(timeout=3)
            return process.returncode, output.decode()
        finally:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=3)
            os.close(master)

    def test_explicit_opt_in_copies_only_survivors_privately_and_cleans_up(self):
        env = self.fixture()
        original = (self.source.read_bytes(), self.source.stat().st_mode, self.source.stat().st_mtime_ns)
        result = self.run_script(["-y", "run"], {**env, "PEPI_EVAL_USE_HOST_MCP": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.captured()
        self.assertEqual((data["modes"], data["dir_mode"]), ({"mcp.json": 0o600}, 0o700))
        self.assertEqual(data["mcp"], {"mcpServers": {"docs": with_survivors()["mcpServers"]["docs"]}})
        self.assertEqual(data["args"][-2], "--secrets-dir")
        self.assertFalse(Path(data["directory"]).exists())
        self.assertIn("copy docs (http; host docs.example.invalid; headers keys: Authorization)", result.stderr)
        for name, reason in DESKTOP_REASONS.items():
            self.assertIn(f"drop {name} ({reason})", result.stderr)
        self.assertNotIn("Allow temporary host mcp.json copy?", result.stderr)
        self.assert_no_values(result.stdout, result.stderr)
        self.assertEqual(original, (self.source.read_bytes(), self.source.stat().st_mode, self.source.stat().st_mtime_ns))

    def test_with_host_auth_both_files_share_the_directory(self):
        env = self.fixture()
        result = self.run_script(["-y", "run"], {**env, "PEPI_EVAL_USE_HOST_AUTH": "1", "PEPI_EVAL_USE_HOST_MCP": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.captured()
        self.assertEqual(data["modes"], {"auth.json": 0o600, "mcp.json": 0o600})
        self.assertFalse(Path(data["directory"]).exists())
        self.assertLess(result.stderr.index(str(self.home / ".pi/agent/auth.json")),
                        result.stderr.index(str(self.source)))

    def test_without_consent_nothing_is_read_or_copied(self):
        env = self.fixture()
        self.source.unlink()
        os.mkfifo(self.source, 0o600)  # any open/read attempt would fail loudly
        for extra in ({}, {"PEPI_EVAL_YES": "1"}, {"PEPI_EVAL_USE_HOST_MCP": "0"}):
            with self.subTest(extra=extra):
                self.capture.unlink(missing_ok=True)
                result = self.run_script(["-y", "run"], {**env, **extra})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIsNone(self.captured()["directory"])
                self.assertNotIn("Cannot use host mcp.json", result.stderr)
                self.assertNotIn("Use " + str(self.source), result.stderr)
                hint = "not copied (no terminal); set PEPI_EVAL_USE_HOST_MCP=1"
                (self.assertNotIn if extra.get("PEPI_EVAL_USE_HOST_MCP") == "0" else self.assertIn)(hint, result.stderr)

    def test_bad_option_values_are_rejected(self):
        env = self.fixture()
        for option in ("", "yes", "2"):
            with self.subTest(option=option):
                result = self.run_script(["-y", "run"], {**env, "PEPI_EVAL_USE_HOST_MCP": option})
                self.assertEqual(result.returncode, 2)
                self.assertIn("PEPI_EVAL_USE_HOST_MCP must be 0 or 1", result.stderr)

    def test_dry_run_never_reads_or_creates_a_directory(self):
        env = self.fixture()
        self.source.unlink()
        os.mkfifo(self.source, 0o600)
        before = set(Path("/tmp").glob("pepi-eval-auth-*"))
        result = self.run_script(["--dry-run", "-y", "run"], {**env, "PEPI_EVAL_USE_HOST_MCP": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("this run can launch to a private temporary directory; nothing read or created", result.stderr)
        self.assertNotIn("Allow temporary", result.stderr)
        self.assertFalse(self.capture.exists())
        self.assertEqual(before, set(Path("/tmp").glob("pepi-eval-auth-*")))

    def test_dotenv_cannot_consent(self):
        env = self.fixture()
        dotenv = self.launch / ".env"
        dotenv.write_text(f"PEPI_EVAL_USE_HOST_MCP=1\nPEPI_EVAL_HOST_MCP_SHA256={'0' * 64}\n")
        dotenv.chmod(0o600)
        result = self.run_script(["-y", "run"], env)
        self.assertEqual(result.returncode, 0, result.stderr)
        warning = next(line for line in result.stderr.splitlines() if "Ignoring shell-only setting(s)" in line)
        self.assertIn("PEPI_EVAL_USE_HOST_MCP", warning)
        self.assertIn("PEPI_EVAL_HOST_MCP_SHA256", warning)
        self.assertIsNone(self.captured()["directory"])
        self.assertIn("not copied (no terminal)", result.stderr)

    def test_no_survivor_continues_without_host_mcp(self):
        env = self.fixture(config=desktop_mcp())
        result = self.run_script(["-y", "run"], {**env, "PEPI_EVAL_USE_HOST_MCP": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("continuing without host MCP servers", result.stderr)
        self.assertIsNone(self.captured()["directory"])  # no adapter: the runner runs directly
        self.assertNotIn("host_auth", self.log.read_text())
        self.assert_no_values(result.stdout, result.stderr)

    def test_drop_mcp_prevents_the_proposal(self):
        env = self.fixture(drop_mcp=True)
        result = self.run_script(["-y", "run"], {**env, "PEPI_EVAL_USE_HOST_MCP": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Native MCP is disabled for this run", result.stderr)
        self.assertIsNone(self.captured()["directory"])

    def test_secrets_dir_wins_without_reading_the_host_file(self):
        env = self.fixture()
        self.source.unlink()
        os.mkfifo(self.source, 0o600)
        secrets = Path(self.fake.name) / "provided"
        secrets.mkdir(mode=0o700)
        result = self.run_script(["-y", "run"], {**env, "PEPI_EVAL_USE_HOST_MCP": "1",
                                                "PEPI_EVAL_SECRETS_DIR": str(secrets)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(str(self.source), result.stderr)
        self.assertEqual(self.captured()["directory"], str(secrets))
        self.assertNotIn("host_auth", self.log.read_text())

    def test_explicit_opt_in_with_unusable_source_fails_closed(self):
        env = self.fixture()
        for kind in ("missing", "invalid", "group-writable", "symlink"):
            with self.subTest(kind=kind):
                for path in (self.source, self.source.with_name("target.json")):
                    if path.exists() or path.is_symlink():
                        path.unlink()
                if kind == "invalid":
                    self.source.write_text("SYNTHETIC-MCP-not-json"); self.source.chmod(0o600)
                elif kind == "group-writable":
                    self.source.write_text(json.dumps(with_survivors())); self.source.chmod(0o620)
                elif kind == "symlink":
                    target = self.source.with_name("target.json")
                    target.write_text(json.dumps(with_survivors())); target.chmod(0o600)
                    self.source.symlink_to(target)
                self.capture.unlink(missing_ok=True)
                result = self.run_script(["-y", "run"], {**env, "PEPI_EVAL_USE_HOST_MCP": "1"})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.capture.exists())
                self.assertNotIn("SYNTHETIC", result.stdout + result.stderr)

    def test_terminal_consent_defaults_to_no_and_yes_flag_does_not_consent(self):
        env = self.fixture(mode=0o644)
        approved = self.filtered_sha256()
        status, output = self.tty(env, {"Allow temporary host mcp.json copy? [y/N]": "y"})
        self.assertEqual(status, 0, output)
        self.assertEqual(list(self.captured()["mcp"]["mcpServers"]), ["docs"])
        self.assertIsNone(self.captured()["digest_env"])  # the runner child never inherits it
        self.assertNotIn(approved, self.python3_log.read_text() + self.log.read_text())  # never argv
        self.assertNotIn(approved, json.dumps(self.captured()["args"]))
        self.assertIn("readable by group/others", output)
        self.assertIn("the agent and its MCP tools can read", output.replace("\r\n", "\n").replace("\n", " "))
        self.assert_no_values(output)
        for answer in ("", "n"):
            with self.subTest(answer=answer):
                self.capture.unlink()
                status, output = self.tty(env, {"Allow temporary host mcp.json copy? [y/N]": answer})
                self.assertEqual(status, 0, output)
                self.assertIsNone(self.captured()["directory"])

    def test_terminal_approval_refuses_a_source_edited_after_the_preview(self):
        env = self.fixture()
        before = set(Path("/tmp").glob("pepi-eval-auth-*"))

        def edit_then_consent():
            self.edit_docs_header()
            return "y"

        self.edit_docs_header()
        edited_sha256 = self.filtered_sha256()
        # A digest pre-set in the caller's shell (here: of the edited file) cannot bypass the check.
        for preset in ({}, {"PEPI_EVAL_HOST_MCP_SHA256": edited_sha256}):
            with self.subTest(preset=bool(preset)):
                self.source.write_text(json.dumps(with_survivors()))
                self.capture.unlink(missing_ok=True)
                status, output = self.tty({**env, **preset},
                                          {"Allow temporary host mcp.json copy? [y/N]": edit_then_consent})
                self.assertEqual(status, 2, output)
                self.assertIn("host mcp.json changed since approval; re-run", output)
                self.assertFalse(self.capture.exists())
                self.assertEqual(before, set(Path("/tmp").glob("pepi-eval-auth-*")))
                self.assert_no_values(output)
                self.assertNotIn("SYNTHETIC-MCP-EDITED", output)

    def test_preset_digest_is_dropped_for_explicit_opt_in(self):
        env = self.fixture()
        result = self.run_script(["-y", "run"], {**env, "PEPI_EVAL_USE_HOST_MCP": "1",
                                                "PEPI_EVAL_HOST_MCP_SHA256": "0" * 64})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.captured()["mcp"]["mcpServers"]), ["docs"])
        self.assertIsNone(self.captured()["digest_env"])

    def test_interrupt_cleans_the_directory_and_reaps_the_runner(self):
        env = self.fixture()
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                self.capture.unlink(missing_ok=True)
                process = subprocess.Popen(["bash", str(self.script_path), "-y", "run"],
                                           env=self.script_env({**env, "PEPI_EVAL_USE_HOST_MCP": "1", "FAKE_SLEEP": "1"}),
                                           cwd=self.launch, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                child = None
                try:
                    deadline = time.monotonic() + 8
                    while not self.capture.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    data = self.captured()
                    child = data["pid"]
                    process.send_signal(signum)
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 130, stderr)
                    self.assertFalse(Path(data["directory"]).exists())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child, 0)
                    self.assert_no_values(stdout, stderr)
                finally:
                    if process.poll() is None:
                        process.terminate(); process.communicate(timeout=5)
                    if child:
                        try:
                            os.kill(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()
