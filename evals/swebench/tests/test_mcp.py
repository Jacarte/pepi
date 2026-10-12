"""Native MCP preparation, bounded provisioning and tmpfs injection; no network/model calls."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.swebench.common import read_json, read_manifest, write_json
from evals.swebench.mcp import MCP_BIN, native_mcp_available, validate_mcp_secret
from evals.swebench.prepare import prepare_run
from evals.swebench.runner import PI_COMMAND, run_tasks
from evals.swebench.tests.test_ci import ENV, SENTINEL, RecordingDocker, write_settings
from evals.swebench.tests.test_runner import TASK, VERSIONS

PACKAGES = {"dependencies": {"@example/server": "1.2.3"}, "commands": ["example-mcp"]}
LOCK = {"lockfileVersion": 3, "packages": {
    "": {"dependencies": PACKAGES["dependencies"]},
    "node_modules/@example/server": {
        "version": "1.2.3", "resolved": "https://registry.npmjs.org/@example/server/-/server-1.2.3.tgz",
        "integrity": "sha512-" + "A" * 86 + "==", "bin": {"example-mcp": "server.js"},
    },
}}
POLICY = {"enabled": True, "commands": ["example-mcp"]}
STDIO = {"mcpServers": {"local": {"command": MCP_BIN + "example-mcp", "args": ["/testbed"]}}}
HTTP = {"mcpServers": {"remote": {"url": "https://mcp.example.invalid/mcp",
                                         "headers": {"Authorization": SENTINEL}}}}


class McpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "repo"
        write_settings(self.root, packages=["npm:pi-mcp-adapter"])
        self.run = self.base / "run-1"

    def provision(self, packages=None, lock=None, directory=None):
        target = directory or self.root
        write_json(target / "mcp-packages.json", PACKAGES if packages is None else packages)
        write_json(target / "mcp-package-lock.json", LOCK if lock is None else lock)

    def prepare(self, **kwargs):
        return prepare_run(self.root, self.run, [TASK], [TASK["instance_id"]],
                           kwargs.pop("versions", VERSIONS), "node:22", **kwargs)

    def secrets(self, config):
        directory = self.base / "secrets"
        directory.mkdir(mode=0o700, exist_ok=True)
        write_json(directory / "mcp.json", config)
        (directory / "mcp.json").chmod(0o600)
        return directory

    def execute(self, docker, secrets):
        write_json(self.run / "builds.json", {TASK["instance_id"]: {"image_id": "sha256:frozen"}})
        with patch.dict(os.environ, ENV), patch("evals.swebench.runner.Docker", return_value=docker):
            return run_tasks(self.run, 1, "eval", "2", "4g", secrets)

    def test_native_runtime_baseline_and_legacy_opt_out(self):
        self.assertTrue(native_mcp_available("1.1.0"))
        for value in ("0.87.1", "0.99.2", "1.1.0-rc.1"):
            self.assertFalse(native_mcp_available(value))
        with self.assertRaisesRegex(ValueError, "Pi >= 1.1.0"):
            self.prepare(versions=dict(VERSIONS, pi="0.87.1"))
        self.assertFalse(self.run.exists())
        self.prepare(versions=dict(VERSIONS, pi="0.87.1"), drop_mcp=True)

    def test_provisioning_is_frozen_hashed_and_does_not_touch_source(self):
        self.provision()
        (self.root / "mcp.json").write_text("HOST CONFIG MUST NOT BE READ OR COPIED")
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        manifest = self.prepare()
        self.assertEqual(manifest["config"]["mcp"], POLICY)
        self.assertIn("mcp-package-lock.json", manifest["config"]["files"])
        package = read_json(self.run / "build/mcp/package.json")
        self.assertEqual(package, {"private": True, "dependencies": PACKAGES["dependencies"]})
        self.assertEqual(read_json(self.run / "build/mcp/package-lock.json"), LOCK)
        self.assertFalse((self.run / "build/profile/mcp.json").exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        read_manifest(self.run)
        write_json(self.run / "build/mcp/policy.json", {"enabled": False})
        with self.assertRaisesRegex(ValueError, "modified"):
            read_manifest(self.run)

    def test_injected_provisioning_has_provenance_and_never_mixes_locks(self):
        injected = self.base / "injected"
        self.provision(directory=injected)
        manifest = self.prepare(config_dir=injected)
        self.assertEqual(manifest["config"]["source"], "injected")
        self.run = self.base / "run-2"
        (injected / "mcp-package-lock.json").unlink()
        self.provision()  # a repo lock must not fill in an injected manifest's missing lock
        with self.assertRaises(FileNotFoundError):
            self.prepare(config_dir=injected)

    def test_missing_lock_and_unpinned_or_unprovisioned_dependencies_fail(self):
        cases = [dict(PACKAGES, dependencies={"@example/server": "latest"}),
                 dict(PACKAGES, commands=["npx"]), dict(PACKAGES, commands=["../example-mcp"])]
        for packages in cases:
            self.provision(packages=packages)
            with self.subTest(packages=packages), self.assertRaises(ValueError):
                self.prepare()
            self.assertFalse(self.run.exists())
        self.provision()
        (self.root / "mcp-package-lock.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.prepare()

    def test_lock_rejects_missing_integrity_private_urls_links_and_mismatches(self):
        for field, value in (("integrity", ""), ("resolved", "file:../server"),
                             ("resolved", "https://token@registry.npmjs.org/server.tgz"),
                             ("resolved", "https://private.invalid/server.tgz"),
                             ("version", "2.0.0"), ("link", True)):
            lock = copy.deepcopy(LOCK)
            lock["packages"]["node_modules/@example/server"][field] = value
            self.provision(lock=lock)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.prepare()
        self.provision(lock={"lockfileVersion": 2, "packages": {}})
        with self.assertRaisesRegex(ValueError, "lockfile v3"):
            self.prepare()

    def test_opt_out_does_not_provision_and_changes_config_id(self):
        self.provision()
        enabled = self.prepare()
        self.run = self.base / "run-2"
        disabled = self.prepare(drop_mcp=True)
        self.assertNotEqual(enabled["config_id"], disabled["config_id"])
        self.assertEqual(disabled["config"]["mcp"], {"enabled": False, "commands": []})
        self.assertFalse((self.run / "build/mcp/package-lock.json").exists())
        settings = read_json(self.run / "build/profile/settings.json")
        self.assertEqual(settings["extensions"], ["-builtin:mcp"])
        self.assertFalse(settings["litellm"]["mcp"]["enabled"])
        docker = RecordingDocker()
        self.assertEqual(self.execute(docker, None), 0)
        launch = next(c for c in docker.commands if any(a.startswith(PI_COMMAND) for a in c))
        self.assertIn("PEPI_EVAL_MCP_FLAG=--no-mcp", launch)

    def test_stdio_and_http_are_injected_over_stdin_only_and_redacted(self):
        self.provision()
        self.prepare()
        config = {"mcpServers": {**STDIO["mcpServers"], **HTTP["mcpServers"]}}
        secrets = self.secrets(config)
        before = (secrets / "mcp.json").read_bytes()
        docker = RecordingDocker(patch_bytes=f"+{SENTINEL}\n".encode())
        self.assertEqual(self.execute(docker, secrets), 0)
        create = next(c for c in docker.commands if c[0] == "create")
        self.assertIn("--tmpfs", create)
        for flag in ("--volume", "--mount", "--privileged", "--network=host"):
            self.assertNotIn(flag, create)
        stdin_call = next(kwargs for kwargs in docker.kwargs if "input" in kwargs)
        self.assertEqual(stdin_call["input"], before)
        self.assertTrue(any("/opt/pepi-agent/mcp.json" in c for c in docker.commands))
        self.assertFalse(any(SENTINEL in arg for c in docker.commands for arg in c))
        execution = read_json(self.run / "execution.json")
        self.assertEqual(execution["secrets"]["mcp.json"], {"present": True})
        for path in self.run.rglob("*"):
            if path.is_file():
                self.assertNotIn(SENTINEL.encode(), path.read_bytes())
        self.assertEqual((secrets / "mcp.json").read_bytes(), before)

    def test_opt_out_rejects_injection_before_container_creation(self):
        self.prepare(drop_mcp=True)
        docker = RecordingDocker()
        with self.assertRaisesRegex(ValueError, "--drop-mcp"):
            self.execute(docker, self.secrets(HTTP))
        self.assertFalse(any(c[0] == "create" for c in docker.commands))
        self.assertFalse((self.run / "execution.json").exists())

    def test_runtime_config_rejects_unprovisioned_launchers_and_unsupported_transports(self):
        bad = [{"command": c} for c in ("npx", "uvx", "/usr/bin/node", MCP_BIN + "unknown")]
        bad += [{"url": "https://example.invalid/sse", "type": "sse"},
                {"url": "file:///host"}, {"url": "https://example.invalid", "headers": {"x": "!secret"}},
                {"command": MCP_BIN + "example-mcp", "env": {"TOKEN": "!secret"}}]
        for server in bad:
            with self.subTest(server=server), self.assertRaises(ValueError):
                validate_mcp_secret(json.dumps({"mcpServers": {"test": server}}).encode(), POLICY, "1.1.0")
        validate_mcp_secret(json.dumps(STDIO).encode(), POLICY, "1.1.0")
        validate_mcp_secret(json.dumps(HTTP).encode(), POLICY, "1.1.0")
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            validate_mcp_secret(b"not json", POLICY, "1.1.0")
        with self.assertRaisesRegex(ValueError, "adapter"):
            validate_mcp_secret(b'{"imports": [], "mcpServers": {}}', POLICY, "1.1.0")
        with self.assertRaisesRegex(ValueError, "Pi >= 1.1.0"):
            validate_mcp_secret(json.dumps(HTTP).encode(), POLICY, "0.87.1")


if __name__ == "__main__":
    unittest.main()
