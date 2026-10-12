"""Frozen profile/provider/auth regression tests: synthetic secrets, no Docker or model calls."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.swebench.common import fingerprint, read_json, read_manifest, write_json
from evals.swebench.prepare import prepare_run
from evals.swebench.profile import isolated_settings, safe_models
from evals.swebench.runner import (PI_COMMAND, Secrets, build_images, frozen_provider_plan,
                                   run_one, run_tasks)
from evals.swebench.tests.test_runner import TASK, VERSIONS, FakeDocker, prepare_fixture

REPO = Path(__file__).resolve().parents[3]
PINS = {k: v for k, v in VERSIONS.items() if k != "pi-provider-litellm"}
TOKENS = [f"SYNTHETIC-{kind}-{i}-0123456789" for i in range(3) for kind in ("access", "refresh")]


def oauth(i=0):
    return {"openai-codex": {"type": "oauth", "access": TOKENS[i * 2],
                              "refresh": TOKENS[i * 2 + 1], "expires": 1, "accountId": "test-account"}}


def repository(base):
    root = base / "repo"
    root.mkdir()
    write_json(root / "settings.json", {"defaultProvider": "anthropic", "defaultModel": "claude-test"})
    (root / "AGENTS.md").write_text("safe rules")
    for name in ("bedrock", "codex"):
        selected = root / "profiles" / name
        selected.mkdir(parents=True)
        for filename in ("settings.json", "models.json"):
            source = REPO / "profiles" / name / filename
            if source.is_file():
                (selected / filename).write_bytes(source.read_bytes())
        # Named secrets are never opened, even though their syntax is deliberately invalid.
        for filename in ("auth.json", "mcp.json"):
            (selected / filename).write_text("SECRET SENTINEL invalid JSON")
    return root


def prepare(root, run, **kwargs):
    return prepare_run(root, run, [TASK], [TASK["instance_id"]], kwargs.pop("versions", PINS), "node:22", **kwargs)


class ProfileTests(unittest.TestCase):
    def test_real_codex_shape_has_models_roles_and_no_litellm_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = repository(base)
            original = fingerprint(root)
            opened = []
            real_open = Path.open
            def watch(path, *args, **kwargs):
                opened.append(path)
                if path.name in ("auth.json", "mcp.json"):
                    self.fail("prepare opened named secrets")
                return real_open(path, *args, **kwargs)
            with patch.object(Path, "open", watch):
                manifest = prepare(root, base / "run", profile_name="codex")
            self.assertEqual(fingerprint(root), original)
            settings = read_json(base / "run/build/profile/settings.json")
            self.assertEqual(settings["defaultProvider"], "openai-codex")
            self.assertEqual(settings["defaultModel"], "gpt-6-luna")
            self.assertEqual(settings["subagents"]["agentOverrides"]["oracle"]["model"], "openai-codex/gpt-6-astra")
            self.assertTrue(settings["subagents"]["agentOverrides"]["claude-code"]["disabled"])
            self.assertNotIn("skills", settings["subagents"]["agentOverrides"]["reviewer"])
            self.assertNotIn("litellm", settings)
            self.assertEqual(settings["packages"], ["npm:pi-subagents@0.68.0"])
            self.assertEqual(manifest["versions"], PINS)
            self.assertEqual(read_json(base / "run/build/profile/models.json")["providers"]["openai-codex"]
                             ["modelOverrides"]["gpt-6-luna"]["contextWindow"], 500000)
            self.assertNotIn("SECRET SENTINEL", json.dumps(manifest))
            docker = FakeDocker()
            with patch("evals.swebench.runner.Docker", return_value=docker):
                build_images(base / "run", "linux/amd64")
            self.assertFalse(any("LITELLM_VERSION=" in a for c in docker.commands for a in c))
            self.assertTrue(manifest["config"]["mcp"]["enabled"])

    def test_real_bedrock_and_no_stale_root_models(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = repository(base)
            (root / "models.json").write_text("unsafe stale root models")
            with self.assertRaisesRegex(ValueError, "litellm-version"):
                prepare(root, base / "missing", profile_name="bedrock")
            manifest = prepare(root, base / "run", profile_name="bedrock", versions=VERSIONS)
            settings = read_json(base / "run/build/profile/settings.json")
            self.assertEqual(settings["defaultModel"], "bedrock-claude-sonnet-5-5")
            self.assertFalse((base / "run/build/profile/models.json").exists())
            self.assertFalse(settings["litellm"]["budget"]["enabled"])
            self.assertEqual(frozen_provider_plan(base / "run")["required_providers"], ["litellm"])
            self.assertIn("pi-provider-litellm", manifest["versions"])

    def test_injected_per_file_precedence_and_hash_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = repository(base)
            injected = base / "injected"; injected.mkdir()
            write_json(injected / "settings.json", {"defaultProvider": "anthropic", "defaultModel": "third"})
            manifest = prepare(root, base / "run", profile_name="codex", config_dir=injected)
            self.assertEqual(manifest["config"]["source"], "injected")
            settings = read_json(base / "run/build/profile/settings.json")
            self.assertEqual(settings["defaultModel"], "third")
            self.assertNotIn("defaultThinkingLevel", settings)  # whole-file, not deep merge
            self.assertTrue((base / "run/build/profile/models.json").is_file())
            plan = read_json(base / "run/build/provider-plan.json")
            plan["required_providers"] = ["litellm"]
            write_json(base / "run/build/provider-plan.json", plan)
            with self.assertRaisesRegex(ValueError, "modified"):
                frozen_provider_plan(base / "run")

    def test_selectors_symlinks_malformed_and_secret_config_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = repository(base)
            (root / "profiles/link").symlink_to(root / "profiles/codex")
            for name in ("../codex", "", "missing", "link"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    prepare(root, base / "run", profile_name=name)
            path = root / "profiles/codex/settings.json"
            path.unlink(); path.symlink_to(root / "settings.json")
            with self.assertRaisesRegex(ValueError, "Symlinks"):
                prepare(root, base / "run", profile_name="codex")
            injected = base / "injected"; injected.mkdir()
            (injected / "auth.json").write_text("secret")
            with self.assertRaisesRegex(ValueError, "Secret file"):
                prepare(root, base / "run", config_dir=injected)
            (injected / "auth.json").unlink()
            (injected / "settings.json").write_text("[]")
            with self.assertRaisesRegex(ValueError, "object"):
                prepare(root, base / "run", config_dir=injected)

    def test_safe_model_definitions_and_unsafe_values_without_disclosure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            good = {"providers": {"anthropic": {"models": [{"id": "test", "name": "Test", "reasoning": True,
                    "contextWindow": 500000, "maxTokens": 8192, "input": ["text"],
                    "cost": {"input": 0}}]}}}
            write_json(path, good); self.assertEqual(safe_models(path), good)
            for bad in ({"apiKey": "SENSITIVE-SENTINEL"}, {"headers": {"Authorization": "SENSITIVE-SENTINEL"}},
                        {"baseUrl": "https://user:SENSITIVE-SENTINEL@example.com"},
                        {"modelOverrides": {"test": {"contextWindow": "!SENSITIVE-SENTINEL"}}}):
                write_json(path, {"providers": {"anthropic": bad}})
                with self.assertRaises(ValueError) as caught:
                    safe_models(path)
                self.assertNotIn("SENSITIVE-SENTINEL", str(caught.exception))
            path.unlink(); path.symlink_to(Path(directory) / "missing")
            root = repository(Path(directory))
            (root / "models.json").symlink_to(path)
            with self.assertRaisesRegex(ValueError, "Symlinks"):
                prepare(root, Path(directory) / "run")

    def test_explicit_child_provider_dependencies_and_ambiguous_routing(self):
        settings = {"defaultProvider": "openai-codex", "defaultModel": "gpt-test", "subagents": {
            "defaultProvider": "openai-codex", "agentOverrides": {"worker": {"model": "litellm/child"}},
            "agentOverridesByProvider": {"openai-codex": {"reviewer": {"model": "anthropic/third"}}}}}
        with self.assertRaisesRegex(ValueError, "litellm-version"):
            isolated_settings(settings, PINS)
        frozen, plan = isolated_settings(settings, VERSIONS)
        self.assertEqual(plan["required_providers"], ["anthropic", "litellm", "openai-codex"])
        self.assertIn("litellm", frozen)
        ambiguous = json.loads(json.dumps(settings))
        del ambiguous["subagents"]["defaultProvider"]
        ambiguous["subagents"]["agentOverrides"]["scout"] = {"model": "bare-child"}
        with self.assertRaisesRegex(ValueError, "mixed-provider"):
            isolated_settings(ambiguous, VERSIONS)
        for value in ("plugin/model", "anthropic:model", "anthropic.model"):
            settings["subagents"]["agentOverrides"]["worker"]["model"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Ambiguous"):
                isolated_settings(settings, VERSIONS)
        settings["defaultProvider"] = "custom-extension"
        with self.assertRaisesRegex(ValueError, "dependency"):
            isolated_settings(settings, VERSIONS)

    def test_scoped_provider_only_override_uses_raw_ordinary_bare_model(self):
        settings = {"defaultProvider": "openai", "defaultModel": "gpt-4o", "subagents": {
            "defaultProvider": "openai", "agentOverrides": {"worker": {"model": "gpt-4o"}},
            "agentOverridesByProvider": {"openai": {"worker": {"defaultProvider": "litellm"}}}}}
        frozen, plan = isolated_settings(settings, VERSIONS)
        ordinary = frozen["subagents"]["agentOverrides"]["worker"]
        scoped = frozen["subagents"]["agentOverridesByProvider"]["openai"]["worker"]
        self.assertEqual(ordinary["model"], "openai/gpt-4o")
        self.assertEqual(scoped, {"defaultProvider": "litellm", "model": "litellm/gpt-4o"})
        self.assertEqual(plan["required_providers"], ["litellm", "openai"])
        self.assertIn({"provider": "litellm", "model": "gpt-4o"}, plan["models"])
        self.assertIn("pi-provider-litellm", plan["versions"])
        with self.assertRaisesRegex(ValueError, "litellm-version"):
            isolated_settings(settings, PINS)

    def test_scoped_provider_only_override_uses_raw_bare_subagent_default_model(self):
        settings = {"defaultProvider": "openai", "defaultModel": "gpt-4o", "subagents": {
            "defaultProvider": "openai", "defaultModel": "gpt-4o", "agentOverrides": {"worker": {}},
            "agentOverridesByProvider": {"openai": {"worker": {"defaultProvider": "litellm"}}}}}
        frozen, plan = isolated_settings(settings, VERSIONS)
        self.assertEqual(frozen["subagents"]["defaultModel"], "openai/gpt-4o")
        self.assertEqual(frozen["subagents"]["agentOverrides"]["worker"]["model"], "openai/gpt-4o")
        self.assertEqual(frozen["subagents"]["agentOverridesByProvider"]["openai"]["worker"],
                         {"defaultProvider": "litellm", "model": "litellm/gpt-4o"})
        self.assertEqual(plan["required_providers"], ["litellm", "openai"])
        self.assertIn({"provider": "litellm", "model": "gpt-4o"}, plan["models"])
        with self.assertRaisesRegex(ValueError, "litellm-version"):
            isolated_settings(settings, PINS)

    def test_scoped_provider_only_override_does_not_replace_explicit_qualification(self):
        for sub in ({"agentOverrides": {"worker": {"model": "openai/gpt-4o"}}},
                    {"defaultModel": "openai/gpt-4o", "agentOverrides": {"worker": {}}}):
            with self.subTest(sub=sub):
                settings = {"defaultProvider": "openai", "defaultModel": "gpt-4o", "subagents": {
                    "defaultProvider": "openai", **sub,
                    "agentOverridesByProvider": {"openai": {"worker": {"defaultProvider": "litellm"}}}}}
                frozen, plan = isolated_settings(settings, PINS)
                self.assertEqual(frozen["subagents"]["agentOverridesByProvider"]["openai"]["worker"],
                                 {"defaultProvider": "openai", "model": "openai/gpt-4o"})
                self.assertEqual(plan["required_providers"], ["openai"])
                self.assertNotIn("pi-provider-litellm", plan["versions"])
                self.assertNotIn("litellm", frozen)

    def test_inherit_and_provider_preference_are_preserved_for_nested_roles(self):
        frozen, plan = isolated_settings({"defaultProvider": "openai-codex", "defaultModel": "parent",
            "subagents": {"defaultProvider": "openai-codex", "defaultModel": "litellm/child",
                          "agentOverrides": {"worker": {"model": "inherit"}}}}, VERSIONS)
        self.assertEqual(frozen["subagents"]["defaultProvider"], "openai-codex")
        self.assertEqual(frozen["subagents"]["agentOverrides"]["worker"]["model"], "inherit")
        self.assertEqual(plan["required_providers"], ["litellm", "openai-codex"])

    def test_legacy_litellm_plan_remains_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            (run / "build/provider-plan.json").unlink()
            manifest = read_json(run / "manifest.json")
            manifest["profile_sha256"] = fingerprint(run / "build")
            write_json(run / "manifest.json", manifest)
            self.assertEqual(frozen_provider_plan(run)["required_providers"], ["litellm"])
            self.assertEqual(read_manifest(run)["versions"], VERSIONS)


class AuthTests(unittest.TestCase):
    def test_codex_schema_no_litellm_or_ambient_env_and_third_builtin(self):
        plan = {"required_providers": ["openai-codex"]}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "UNFORWARDED", "NODE_OPTIONS": "evil"}, clear=True):
            secrets = Secrets({"auth.json": json.dumps(oauth()).encode()})
            secrets.validate(plan, "1.1.0")
            self.assertEqual(secrets.environment, [])
            self.assertTrue(secrets.oauth)
            third = Secrets({"auth.json": b'{"anthropic":{"type":"api_key","key":"synthetic-key"}}'})
            third.validate({"required_providers": ["anthropic"]}, "1.1.0")
            self.assertEqual(third.environment, [])
            for data in ({}, {"openai": {"type": "api_key", "key": "key"}},
                         {"openai-codex": {"type": "api_key", "key": "key"}},
                         {"openai-codex": {"type": "oauth", "access": "a", "refresh": "b", "expires": "123"}}):
                with self.subTest(data=data), self.assertRaises(ValueError):
                    Secrets({"auth.json": json.dumps(data).encode()}).validate(plan, "1.1.0")
            with self.assertRaisesRegex(ValueError, "1.1.0"):
                secrets.validate(plan, "1.2.0")

    def test_saved_litellm_and_secure_env_policy(self):
        plan = {"required_providers": ["litellm"]}
        with patch.dict(os.environ, {}, clear=True):
            saved = {"litellm": {"type": "api_key", "key": "dedicated", "env": {"LITELLM_BASE_URL": "https://test.example"}}}
            secrets = Secrets({"auth.json": json.dumps(saved).encode()}); secrets.validate(plan, "1.1.0")
            self.assertEqual(secrets.environment, [])
            for field, value in (("key", "!host-key-command"), ("key", "$HOST_KEY"),
                                 ("env", {"NODE_OPTIONS": "--require=evil"}),
                                 ("env", {"LITELLM_BASE_URL": "https://u:secret@example.com"})):
                data = json.loads(json.dumps(saved)); data["litellm"][field] = value
                with self.assertRaises(ValueError):
                    Secrets({"auth.json": json.dumps(data).encode()}).validate(plan, "1.1.0")

    def test_auth_failures_precede_docker_and_third_builtin_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = repository(base); run = base / "run"
            prepare(root, run)
            write_json(run / "builds.json", {TASK["instance_id"]: {"image_id": "frozen"}})
            with patch.dict(os.environ, {}, clear=True), patch("evals.swebench.runner.Docker") as docker:
                with self.assertRaisesRegex(ValueError, "anthropic"):
                    run_tasks(run, 1, "eval", "2", "4g")
                docker.assert_not_called()
            directory = base / "secrets"; directory.mkdir(mode=0o700)
            write_json(directory / "auth.json", {"anthropic": {"type": "api_key", "key": "synthetic"}})
            (directory / "auth.json").chmod(0o600)
            docker = FakeDocker()
            with patch.dict(os.environ, {}, clear=True), patch("evals.swebench.runner.Docker", return_value=docker):
                self.assertEqual(run_tasks(run, 1, "eval", "2", "4g", directory), 0)
            launch = next(c for c in docker.commands if any(a.startswith(PI_COMMAND) for a in c))
            self.assertFalse(any(a.startswith("LITELLM_") for a in launch))
            self.assertIn("--provider anthropic --model claude-test", launch[-1])

    def test_oauth_pretty_auth_journal_stdin_and_private_directory_checks(self):
        secrets = Secrets({"auth.json": json.dumps(oauth(), indent=2).encode()})
        secrets.validate({"required_providers": ["openai-codex"]}, "1.1.0")
        inputs = []
        class Docker:
            def call(self, *args, **kwargs):
                if "input" in kwargs:
                    inputs.append((args, kwargs["input"]))
        secrets.inject(Docker(), "unused")
        journal = next(data for args, data in inputs if "auth-journal.jsonl" in args[-1])
        self.assertEqual(len(journal.splitlines()), 1)
        self.assertEqual(json.loads(journal), oauth())
        self.assertFalse(any(token in part for args, _ in inputs for part in args for token in TOKENS))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            public = base / "public"; public.mkdir(); public.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "chmod 700"):
                Secrets.load(public)
            link = base / "link"; link.symlink_to(public)
            with self.assertRaisesRegex(ValueError, "symlink"):
                Secrets.load(link)
            from evals.swebench import __main__ as cli
            run = prepare_fixture(base)
            write_json(run / "builds.json", {TASK["instance_id"]: {"image_id": "frozen"}})
            with patch.dict(os.environ, {}, clear=True), patch("evals.swebench.runner.Docker") as docker:
                self.assertEqual(cli.main(["run", str(run), "--secrets-dir", str(link),
                                           "--timeout-seconds", "1", "--network", "eval"]), 2)
                docker.assert_not_called()

    def test_transient_journal_failure_still_discards_and_stops_remaining_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = repository(base); run = base / "run"
            tasks = [dict(TASK, instance_id=f"owner__repo-{i}") for i in (1, 2)]
            prepare_run(root, run, tasks, [t["instance_id"] for t in tasks], PINS, "node:22", profile_name="codex")
            write_json(run / "builds.json", {t["instance_id"]: {"image_id": "frozen"} for t in tasks})
            secret = base / "secrets"; secret.mkdir(mode=0o700)
            write_json(secret / "auth.json", oauth()); (secret / "auth.json").chmod(0o600)
            class Docker(FakeDocker):
                def __init__(self):
                    super().__init__(); self.collections = 0
                def call(self, *args, **kwargs):
                    if "node" in args and "-e" in args:
                        self.commands.append(args); self.collections += 1
                        if self.collections == 1:
                            raise subprocess.CalledProcessError(2, args)
                        return SimpleNamespace(stdout=json.dumps({"auth": oauth(1), "history": [oauth(), oauth(1)]}).encode())
                    return super().call(*args, **kwargs)
            docker = Docker()
            with patch.dict(os.environ, {}, clear=True), patch("evals.swebench.runner.Docker", return_value=docker):
                self.assertEqual(run_tasks(run, 1, "eval", "2", "4g", secret), 3)
            self.assertEqual(sum(c[0] == "create" for c in docker.commands), 1)
            self.assertFalse(any("--cached" in c or c[0] == "cp" and "/output/." in c[1] for c in docker.commands))
            self.assertFalse((run / "attempts/owner__repo-2").exists())
            self.assertEqual(read_json(secret / "auth.json"), oauth())

    def test_successive_rotations_redacted_and_collection_failure_discards(self):
        for failure in (None, "agent", "timeout", "interrupt", "journal"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                secrets = Secrets({"auth.json": json.dumps(oauth()).encode()})
                secrets.validate({"required_providers": ["openai-codex"]}, "1.1.0")
                class Docker(FakeDocker):
                    def call(self, *args, **kwargs):
                        if "node" in args and "-e" in args:
                            self.commands.append(args)
                            if failure == "journal":
                                raise subprocess.CalledProcessError(2, args)
                            return SimpleNamespace(stdout=json.dumps({"auth": oauth(2),
                                "history": [oauth(0), oauth(1), oauth(2)]}).encode())
                        result = super().call(*args, **kwargs)
                        if "--cached" in args:
                            result.stdout = ("+" + " ".join(TOKENS)).encode()
                        return result
                docker = Docker(failure)
                output = Path(directory) / "attempt"
                original_auth = secrets.files["auth.json"]
                row = run_one(docker, TASK, {"image_id": "frozen"}, output, 1, "eval", "2", "4g", secrets)
                self.assertFalse(any(token in row["patch"] for token in TOKENS))
                if failure == "journal":
                    self.assertEqual(row["agent_status"], "runner_error")
                    self.assertFalse(any("--cached" in c for c in docker.commands))
                    self.assertFalse((output / "parent.events.jsonl").exists())
                else:
                    self.assertEqual(json.loads(secrets.files["auth.json"]), oauth(2))
                    self.assertEqual(json.loads(original_auth), oauth(0))
                self.assertEqual(docker.commands[-1][0], "rm")
                for path in output.rglob("*"):
                    if path.is_file():
                        self.assertFalse(any(token.encode() in path.read_bytes() for token in TOKENS))


class AuthAuditExecutableTests(unittest.TestCase):
    def test_actual_preload_esm_child_successive_writes_and_collector(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve(); agent = base / "agent"; secret = base / "secret"
            agent.mkdir(); secret.mkdir()
            write_json(secret / "auth.json", oauth(0))
            (secret / "auth-journal.jsonl").write_text(json.dumps(oauth(0)) + "\n")
            (agent / "auth.json").symlink_to(secret / "auth.json")
            preload = REPO / "evals/swebench/auth-audit.cjs"
            boot = base / "boot.cjs"
            boot.write_text(f"require({json.dumps(str(preload))}).installAuthAudit({json.dumps(str(agent))},{json.dumps(str(secret))});")
            script = base / "refresh.mjs"
            script.write_text("import {writeFileSync} from 'node:fs'; import {spawnSync} from 'node:child_process';\n"
                + f"writeFileSync({json.dumps(str(agent / 'auth.json'))},{json.dumps(json.dumps(oauth(1)))});\n"
                + "const child=spawnSync(process.execPath,['-e'," + json.dumps(
                    f"require('fs').writeFileSync({json.dumps(str(agent / 'auth.json'))},{json.dumps(json.dumps(oauth(2)))})")
                + "],{env:process.env}); if(child.status!==0) throw Error('child failed');")
            env = {**os.environ, "NODE_OPTIONS": f"--require={boot}"}
            result = subprocess.run(["node", "--require", str(preload), str(script)], env=env, capture_output=True, check=True)
            self.assertEqual(result.stdout, b""); self.assertEqual(result.stderr, b"")
            history = [json.loads(line) for line in (secret / "auth-journal.jsonl").read_text().splitlines()]
            self.assertEqual(history, [oauth(0), oauth(1), oauth(2)])
            self.assertEqual(read_json(secret / "auth.json"), oauth(2))
            self.assertTrue((agent / "auth.json").is_symlink())
            class LocalCollector:
                def call(self, *args, **kwargs):
                    script = args[-1].replace("/run/pepi-secrets", str(secret)).replace("/opt/pepi-agent", str(agent))
                    return subprocess.run(["node", "-e", script], capture_output=True, check=True)
            secrets = Secrets({"auth.json": json.dumps(oauth(0)).encode()})
            secrets.validate({"required_providers": ["openai-codex"]}, "1.1.0")
            secrets.collect_auth(LocalCollector(), "unused")
            self.assertNotIn(TOKENS[2], secrets.redact_text(TOKENS[2]))
            self.assertEqual(json.loads(secrets.files["auth.json"]), oauth(2))
            (secret / "auth-journal.jsonl").write_text("malformed secret payload")
            with self.assertRaisesRegex(RuntimeError, "discarded"):
                secrets.collect_auth(LocalCollector(), "unused")

    def test_journal_failure_prevents_auth_write_and_unrelated_writes_unchanged(self):
        for failure in ("symlink", "directory", "malformed", "auth-replaced"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve(); agent = base / "agent"; secret = base / "secret"
                agent.mkdir(); secret.mkdir()
                write_json(secret / "auth.json", oauth(0))
                (agent / "auth.json").symlink_to(secret / "auth.json")
                journal = secret / "auth-journal.jsonl"
                if failure == "symlink": journal.symlink_to(base / "outside")
                elif failure == "directory": journal.mkdir()
                elif failure == "auth-replaced":
                    (agent / "auth.json").unlink(); write_json(agent / "auth.json", oauth(0))
                preload = REPO / "evals/swebench/auth-audit.cjs"
                script = f"const f=require('fs'); require({json.dumps(str(preload))}).installAuthAudit({json.dumps(str(agent))},{json.dumps(str(secret))});"
                script += f"f.writeFileSync({json.dumps(str(base / 'unrelated'))},'unchanged');"
                content = "malformed" if failure == "malformed" else json.dumps(oauth(1))
                script += f"f.writeFileSync({json.dumps(str(agent / 'auth.json'))},{json.dumps(content)});"
                result = subprocess.run(["node", "--require", str(preload), "-e", script], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"PEPI_EVAL_AUTH_AUDIT_FAILED", result.stderr)
                self.assertEqual(read_json(secret / "auth.json"), oauth(0))
                self.assertEqual((base / "unrelated").read_text(), "unchanged")
                self.assertTrue(list(secret.glob("auth-pending-*")))
                self.assertFalse((base / "outside").exists())
                self.assertFalse(any(token.encode() in result.stderr for token in TOKENS))


if __name__ == "__main__":
    unittest.main()
