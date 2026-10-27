"""evals.swebench.ci and evals/ci/*.sh: offline, temp dirs and fakes only (no network, Docker or gh)."""
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.swebench import ci
from evals.swebench.common import write_json
from evals.swebench.tests.test_narrate import summary

REPO = Path(__file__).resolve().parents[3]
CI_SCRIPTS = sorted((REPO / "evals/ci").glob("*.sh"))
AUTH_TOKEN = "sk-test-0123456789abcdefghij-SECRET"
ENV_TOKEN = "gateway-key-0123456789-SECRET"
PI_AUTH = json.dumps({"litellm": {"type": "api_key", "key": AUTH_TOKEN}, "short": "abc"})
EVAL_ENV = textwrap.dedent(f"""\
    # forwarded to the evals
    LITELLM_BASE_URL=https://gateway.invalid
    export LITELLM_API_KEY="{ENV_TOKEN}"
    TYPESAFE_API_KEY='typesafe-value-123'
    PERCENT='abc%0Adef'
    FLAG=1
    """)


def invoke(*arguments, env=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            patch.dict(os.environ, env or {}, clear=False):
        code = ci.main([str(argument) for argument in arguments])
    return code, out.getvalue(), err.getvalue()


class Temp(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write(self, relative, text="x", base=None):
        path = (base or self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


class UsageTests(unittest.TestCase):
    def test_missing_or_unknown_command_is_a_usage_error(self):
        self.assertEqual(invoke()[0], 2)
        self.assertEqual(invoke("bogus")[0], 2)
        self.assertEqual(invoke("cache-key")[0], 2)

    def test_help_exits_zero(self):
        self.assertEqual(invoke("--help")[0], 0)

    def test_shell_scripts_parse(self):
        self.assertEqual({path.name for path in CI_SCRIPTS},
                         {"free-disk.sh", "narrate.sh", "prune-images.sh", "run-evals.sh"})
        for path in (*CI_SCRIPTS, REPO / "profile.sh"):
            for shell in ("bash", "/bin/bash"):
                if shutil.which(shell):
                    result = subprocess.run([shell, "-n", str(path)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, f"{shell} -n {path.name}: {result.stderr}")


class ProfileScriptTests(Temp):
    def repo(self):
        repo = self.root / "repo"
        repo.mkdir()
        shutil.copyfile(REPO / "profile.sh", repo / "profile.sh")
        self.write("profiles/alpha/settings.json", '{"defaultModel": "a"}', repo)
        self.write("profiles/alpha/mcp.json", "{}", repo)
        self.write("profiles/beta/settings.json", '{"defaultModel": "b"}', repo)
        self.write("keep.txt", "untouched", repo)
        return repo

    def run_profile(self, repo, *arguments, stdin=""):
        return subprocess.run(["bash", str(repo / "profile.sh"), *arguments], input=stdin,
                              capture_output=True, text=True, cwd=self.root)

    def test_named_profile_applies_without_a_terminal(self):
        repo = self.repo()
        result = self.run_profile(repo, "beta")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Applied profile: beta", result.stdout)
        self.assertNotIn("Available profiles", result.stdout)
        self.assertEqual(json.loads((repo / "settings.json").read_text()), {"defaultModel": "b"})
        self.assertFalse((repo / "mcp.json").exists())
        self.assertEqual((repo / "keep.txt").read_text(), "untouched")

    def test_unknown_invalid_or_extra_arguments_fail(self):
        repo = self.repo()
        for arguments, message in ((("gamma",), "Unknown profile"), (("../alpha",), "Invalid profile name"),
                                   ((".hidden",), "Invalid profile name"), (("alpha", "beta"), "Usage")):
            result = self.run_profile(repo, *arguments)
            self.assertEqual(result.returncode, 1, arguments)
            self.assertIn(message, result.stderr)
        self.assertFalse((repo / "settings.json").exists())

    def test_menu_mode_is_unchanged_without_arguments(self):
        repo = self.repo()
        result = self.run_profile(repo, stdin="\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Available profiles", result.stdout)
        self.assertIn("Applied profile: alpha", result.stdout)


class AssembleConfigTests(Temp):
    def repo(self):
        repo = self.root / "repo"
        repo.mkdir()
        shutil.copyfile(REPO / "profile.sh", repo / "profile.sh")
        self.write("settings.json", '{"defaultModel": "root"}', repo)
        self.write("AGENTS.md", "rules\n", repo)
        self.write("workflows/a.ts", "export {}\n", repo)
        self.write("mcp.json", '{"secret": true}', repo)
        self.write("auth.json", "{}", repo)
        self.write("notes.txt", "not config", repo)
        self.write("profiles/bed/settings.json", '{"defaultModel": "bed"}', repo)
        self.write("profiles/bed/mcp.json", '{"servers": {}}', repo)
        self.write("profiles/bed/models-router.json", "{}", repo)
        return repo

    def test_profile_is_applied_in_a_scratch_copy_and_only_config_files_are_emitted(self):
        repo = self.repo()
        code, out, err = invoke("assemble-config", "--repo", repo, "--profile", "bed", "--out", self.root / "out",
                                "--fallback-root", repo)
        self.assertEqual(code, 0, err)
        files = sorted(path.relative_to(self.root / "out").as_posix()
                       for path in (self.root / "out").rglob("*") if path.is_file())
        self.assertEqual(files, ["AGENTS.md", "settings.json", "workflows/a.ts"])
        self.assertEqual(json.loads((self.root / "out/settings.json").read_text()), {"defaultModel": "bed"})
        self.assertEqual(json.loads((repo / "settings.json").read_text()), {"defaultModel": "root"})
        record = json.loads(out)
        self.assertEqual(record["profile"], "bed")
        self.assertEqual(record["harness_fallback_files"], [])
        code, out2, _ = invoke("assemble-config", "--repo", repo, "--profile", "bed", "--out", self.root / "out2")
        self.assertEqual(json.loads(out2)["sha256"], record["sha256"])

    def test_missing_config_file_that_the_harness_would_supply_is_fatal_unless_allowed(self):
        repo = self.repo()
        harness = self.root / "harness"
        self.write("models.json", "{}", harness)
        code, out, err = invoke("assemble-config", "--repo", repo, "--profile", "bed", "--out", self.root / "out",
                                "--fallback-root", harness)
        self.assertEqual(code, 1, err)
        self.assertEqual(json.loads(out)["harness_fallback_files"], ["models.json"])
        self.assertIn("error: config files absent here would come from the harness checkout: models.json", err)
        code, out, err = invoke("assemble-config", "--repo", repo, "--profile", "bed", "--out", self.root / "out2",
                                "--fallback-root", harness, "--allow-harness-fallback")
        self.assertEqual(code, 0, err)
        self.assertIn("warning: config files absent here", err)

    def test_refuses_while_secrets_are_in_the_environment(self):
        repo = self.repo()
        for name in ("PI_AUTH", "EVAL_ENV"):
            with self.subTest(name=name):
                code, out, err = invoke("assemble-config", "--repo", repo, "--profile", "bed",
                                        "--out", self.root / f"out-{name}", env={name: "secret-value-123456"})
                self.assertEqual(code, 2)
                self.assertIn(f"unset {name}", err)
                self.assertNotIn("secret-value-123456", out + err)
                self.assertFalse((self.root / f"out-{name}").exists())

    def test_profile_sh_runs_with_a_minimal_environment(self):
        repo = self.repo()
        record = self.root / "profile-env.txt"
        (repo / "profile.sh").write_text(f"#!/bin/bash\nenv | sort > {str(record)!r}\n")
        code, _, err = invoke("assemble-config", "--repo", repo, "--profile", "bed", "--out", self.root / "out",
                              env={"PEPI_CI_LEAK": "1", "GITHUB_TOKEN": "ghs_leak"})
        self.assertEqual(code, 0, err)
        names = {line.split("=", 1)[0] for line in record.read_text().splitlines()}
        names -= {"PWD", "SHLVL", "_", "OLDPWD"}  # set by bash itself
        self.assertEqual(names, {"HOME", "LC_ALL", "PATH"})
        home = next(line for line in record.read_text().splitlines() if line.startswith("HOME="))
        self.assertNotEqual(home.removeprefix("HOME="), str(Path.home()))

    def test_symlinked_parent_directories_are_refused(self):
        repo = self.repo()
        outside = self.root / "outside"
        self.write("subagent/config.json", '{"host": "file"}', outside)
        (repo / "extensions").symlink_to(outside, target_is_directory=True)
        code, _, err = invoke("assemble-config", "--repo", repo, "--profile", "bed", "--out", self.root / "out")
        self.assertEqual(code, 2)
        self.assertIn("Symlink in config sources: extensions/subagent/config.json (extensions)", err)

    def test_symlinks_secret_files_and_unknown_profiles_are_refused(self):
        repo = self.repo()
        (repo / "workflows/link.ts").symlink_to(repo / "notes.txt")
        code, _, err = invoke("assemble-config", "--repo", repo, "--profile", "bed", "--out", self.root / "o1")
        self.assertEqual(code, 2)
        self.assertIn("Symlink", err)
        (repo / "workflows/link.ts").unlink()
        self.write("workflows/.env", "A=1", repo)
        code, _, err = invoke("assemble-config", "--repo", repo, "--profile", "bed", "--out", self.root / "o2")
        self.assertEqual(code, 2)
        self.assertIn("Secret file", err)
        for profile in ("nope", "../bed"):
            self.assertEqual(invoke("assemble-config", "--repo", repo, "--profile", profile,
                                    "--out", self.root / "o3")[0], 2)
        self.write("o4/existing", "x")
        (repo / "workflows/.env").unlink()
        self.assertEqual(invoke("assemble-config", "--repo", repo, "--profile", "bed", "--out", self.root / "o4")[0], 2)


class CacheKeyTests(Temp):
    def setUp(self):
        super().setUp()
        self.harness = self.root / "harness"
        for relative in ("evals/pepi-eval.sh", "evals/__init__.py", "profile.sh", "evals/swebench/runner.py",
                         "evals/swebench/instructions.md", "evals/swebench/README.md", "evals/swebench/Dockerfile",
                         "evals/swebench/presets/p.json", "evals/swebench/tests/test_x.py",
                         "evals/swebench/runs/r/summary.json", "evals/swebench/__pycache__/x.pyc", "README.md",
                         "evals/ci/run-evals.sh"):
            self.write(relative, relative, self.harness)
        self.config = self.root / "config"
        self.write("settings.json", "{}", self.config)

    def key(self, *extra, config=None):
        code, out, err = invoke("cache-key", "--harness-root", self.harness, "--config-dir", config or self.config,
                                "--profile", "bedrock", "--preset", "verified20-v1", "--pi-version", "1.1.0",
                                "--week", "2026-W43", *extra)
        self.assertEqual(code, 0, err)
        return out.strip()

    def test_key_shape_and_determinism(self):
        key = self.key()
        self.assertRegex(key, r"^pepi-evals-v1-bedrock-verified20-v1-[0-9a-f]{24}-2026-W43$")
        self.assertEqual(key, self.key())
        code, out, _ = invoke("cache-key", "--harness-root", self.harness, "--config-dir", self.config,
                              "--profile", "bedrock", "--preset", "verified20-v1", "--week", "2026-W43", "--json")
        components = json.loads(out)["components"]
        self.assertEqual(set(components), {"harness", "config", "pins"})
        self.assertIn("timeout_seconds", components["pins"])

    def test_harness_config_pins_settings_and_week_change_the_key(self):
        base = self.key()
        changes = []
        for relative in ("evals/swebench/runner.py", "evals/swebench/instructions.md", "evals/pepi-eval.sh",
                         "profile.sh", "evals/swebench/presets/p.json", "evals/swebench/Dockerfile",
                         "evals/ci/run-evals.sh"):
            path = self.harness / relative
            original = path.read_text()
            path.write_text(original + "changed")
            changes.append(self.key())
            path.write_text(original)
        self.assertEqual(self.key(), base)
        (self.config / "settings.json").write_text('{"defaultModel": "b"}')
        changes.append(self.key())
        (self.config / "settings.json").write_text("{}")
        changes += [self.key("--node-image", "node:23"), self.key("--timeout-seconds", "600"),
                    self.key("--task-repo-revision", "a" * 40), self.key("--setting", "IDS=a,b"),
                    self.key("--week", "2026-W44")]
        self.assertNotIn(base, changes)
        self.assertEqual(len(set(changes)), len(changes))

    def test_ids_and_network_are_pins_with_the_run_evals_defaults(self):
        base = self.key()
        self.assertEqual(self.key("--network", "pepi-eval"), base)
        self.assertEqual(self.key("--ids", ""), base)
        other_network = self.key("--network", "other-net")
        ids_csv = self.key("--ids", "a__b-1,c__d-2")
        self.assertEqual(len({base, other_network, ids_csv}), 3)
        first, second = self.write("ids-1.txt", "a__b-1\n"), self.write("ids-2.txt", "a__b-1\n")
        self.assertEqual(self.key("--ids", first), self.key("--ids", second))
        second.write_text("c__d-2\n")
        self.assertNotEqual(self.key("--ids", first), self.key("--ids", second))

    def test_tests_docs_runs_and_bytecode_do_not_change_the_key(self):
        base = self.key()
        for relative in ("evals/swebench/README.md", "evals/swebench/tests/test_x.py",
                         "evals/swebench/runs/r/summary.json", "evals/swebench/__pycache__/x.pyc", "README.md",
                         "evals/swebench/runs/new/predictions.jsonl"):
            self.write(relative, "different", self.harness)
        self.assertEqual(self.key(), base)

    def test_bad_inputs_are_usage_errors(self):
        for extra in (("--week", "43"), ("--setting", "noequals"), ("--setting", "1BAD=x")):
            self.assertEqual(invoke("cache-key", "--harness-root", self.harness, "--config-dir", self.config,
                                    "--profile", "bedrock", "--preset", "verified20-v1", *extra)[0], 2, extra)
        self.assertEqual(invoke("cache-key", "--config-dir", self.config, "--profile", "../x", "--preset", "p")[0], 2)
        self.write("auth.json", "{}", self.config)
        self.assertEqual(invoke("cache-key", "--harness-root", self.harness, "--config-dir", self.config,
                                "--profile", "b", "--preset", "p")[0], 2)

    def test_real_harness_key_ignores_its_own_runs_directory(self):
        code, out, err = invoke("cache-key", "--config-dir", self.config, "--profile", "b", "--preset", "p",
                                "--week", "2026-W01", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["components"]["harness"], ci.harness_hash(REPO))


class SecretsTests(Temp):
    ENV = {"PI_AUTH": PI_AUTH, "EVAL_ENV": EVAL_ENV}

    def setup(self, env=None):
        launch = self.root / "launch"
        launch.mkdir(exist_ok=True)
        return invoke("secrets-setup", "--dir", self.root / "secrets", "--launch-dir", launch,
                      "--mask-file", self.root / "masks.txt", env=env or self.ENV)

    def test_files_modes_and_masks_without_printing_values(self):
        code, out, err = self.setup()
        self.assertEqual(code, 0, err)
        for value in (AUTH_TOKEN, ENV_TOKEN, "typesafe-value-123"):
            self.assertNotIn(value, out + err)
        self.assertEqual(stat.S_IMODE((self.root / "secrets").stat().st_mode), 0o700)
        for path in (self.root / "secrets/auth.json", self.root / "launch/.env", self.root / "masks.txt"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
        self.assertEqual(json.loads((self.root / "secrets/auth.json").read_text()), json.loads(PI_AUTH))
        self.assertEqual((self.root / "launch/.env").read_text(), EVAL_ENV)
        masks = (self.root / "masks.txt").read_text().splitlines()
        self.assertIn(f"::add-mask::{AUTH_TOKEN}", masks)
        self.assertIn(f"::add-mask::{ENV_TOKEN}", masks)
        self.assertIn("::add-mask::typesafe-value-123", masks)
        self.assertNotIn("::add-mask::abc", masks)
        self.assertNotIn("::add-mask::1", masks)
        self.assertIn("::add-mask::abc%250Adef", masks)
        self.assertEqual(json.loads(out)["env_names"],
                         ["LITELLM_BASE_URL", "LITELLM_API_KEY", "TYPESAFE_API_KEY", "PERCENT", "FLAG"])

    def test_missing_or_malformed_secrets_name_the_variable_only(self):
        code, _, err = self.setup({"PI_AUTH": "", "EVAL_ENV": EVAL_ENV})
        self.assertEqual(code, 2)
        self.assertIn("PI_AUTH is unset or empty", err)
        code, _, err = self.setup({"PI_AUTH": PI_AUTH, "EVAL_ENV": " "})
        self.assertIn("EVAL_ENV is unset or empty", err)
        self.assertEqual(self.setup({"PI_AUTH": '["list"]', "EVAL_ENV": EVAL_ENV})[0], 2)
        code, _, err = self.setup({"PI_AUTH": PI_AUTH, "EVAL_ENV": f"GOOD=1\nnot a pair {ENV_TOKEN}\n"})
        self.assertEqual(code, 2)
        self.assertIn("line 2", err)
        self.assertNotIn(ENV_TOKEN, err)
        code, _, err = self.setup({"PI_AUTH": PI_AUTH, "EVAL_ENV": f'A="{ENV_TOKEN}\n'})
        self.assertEqual(code, 2)
        self.assertNotIn(ENV_TOKEN, err)
        self.assertFalse((self.root / "secrets/auth.json").exists())

    def test_dotenv_parser_matches_the_harness_syntax(self):
        pairs = ci.parse_dotenv("A=plain value # comment\nB='single # kept'\nC=\"d\\\"q\\\\x\\n\"\n  export D=4\n")
        self.assertEqual(pairs, [("A", "plain value"), ("B", "single # kept"), ("C", 'd"q\\x\n'), ("D", "4")])
        for bad in ("A='x' y", "1A=x", "A=x\r"):
            with self.assertRaises(ci.UsageError):
                ci.parse_dotenv(bad)

    def test_cleanup_removes_everything_setup_wrote(self):
        self.assertEqual(self.setup()[0], 0)
        self.write("launch/other.txt", "keep")
        code, out, _ = invoke("secrets-cleanup", "--dir", self.root / "secrets", "--launch-dir", self.root / "launch",
                              "--mask-file", self.root / "masks.txt")
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "secrets").exists())
        self.assertFalse((self.root / "launch/.env").exists())
        self.assertFalse((self.root / "masks.txt").exists())
        self.assertEqual((self.root / "launch/other.txt").read_text(), "keep")
        self.assertEqual(json.loads(out)["kept"], [])
        self.assertEqual(invoke("secrets-cleanup", "--dir", self.root / "secrets")[0], 0)

    def test_setup_never_replaces_existing_files_or_directories(self):
        self.write("secrets/auth.json", "MINE")
        code, _, err = self.setup()
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual((self.root / "secrets/auth.json").read_text(), "MINE")
        shutil.rmtree(self.root / "secrets")
        self.write("launch/.env", "MINE=1\n")
        code, _, err = self.setup()
        self.assertEqual(code, 2)
        self.assertIn("already holds a .env", err)
        self.assertEqual((self.root / "launch/.env").read_text(), "MINE=1\n")
        self.assertFalse((self.root / "secrets").exists())
        (self.root / "launch/.env").unlink()
        self.write("masks.txt", "MINE")
        self.assertEqual(self.setup()[0], 2)
        self.assertEqual((self.root / "masks.txt").read_text(), "MINE")
        self.assertFalse((self.root / "secrets").exists())

    def test_eval_env_must_not_set_harness_settings(self):
        code, out, err = self.setup({"PI_AUTH": PI_AUTH,
                                     "EVAL_ENV": EVAL_ENV + "PEPI_EVAL_PRESET=other-preset-value\nPEPI_EVAL_IDS=x\n"})
        self.assertEqual(code, 2)
        self.assertIn("must not set PEPI_EVAL_*", err)
        self.assertIn("PEPI_EVAL_IDS, PEPI_EVAL_PRESET", err)
        self.assertNotIn("other-preset-value", out + err)
        self.assertFalse((self.root / "secrets").exists())
        self.assertFalse((self.root / "launch/.env").exists())

    def test_cleanup_without_a_marker_removes_nothing(self):
        # e.g. pointed at a real Pi agent dir and a project with its own .env by mistake
        agent = self.root / "agent"
        self.write("auth.json", "REAL AUTH", agent)
        self.write("settings.json", "{}", agent)
        self.write("project/.env", "REAL=1\n")
        code, out, _ = invoke("secrets-cleanup", "--dir", agent, "--launch-dir", self.root / "project")
        self.assertEqual(code, 0)
        self.assertEqual((agent / "auth.json").read_text(), "REAL AUTH")
        self.assertEqual((agent / "settings.json").read_text(), "{}")
        self.assertEqual((self.root / "project/.env").read_text(), "REAL=1\n")
        self.assertEqual(json.loads(out)["removed"], [])
        self.assertEqual(len(json.loads(out)["kept"]), 2)

    def test_cleanup_keeps_unrelated_files_changed_env_and_hard_link_content(self):
        self.assertEqual(self.setup()[0], 0)
        self.write("secrets/extra.txt", "not ours")
        os.link(self.root / "secrets/auth.json", self.root / "auth-hardlink.json")
        (self.root / "launch/.env").write_text("REPLACED=1\n")
        code, out, _ = invoke("secrets-cleanup", "--dir", self.root / "secrets", "--launch-dir", self.root / "launch",
                              "--mask-file", self.root / "masks.txt")
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "secrets/auth.json").exists())
        self.assertFalse((self.root / "secrets" / ci.SECRETS_MARKER).exists())
        self.assertEqual((self.root / "secrets/extra.txt").read_text(), "not ours")
        self.assertEqual(json.loads((self.root / "auth-hardlink.json").read_text()), json.loads(PI_AUTH))
        self.assertEqual((self.root / "launch/.env").read_text(), "REPLACED=1\n")
        kept = json.loads(out)["kept"]
        self.assertTrue(any("not the file secrets-setup wrote" in item for item in kept), kept)
        self.assertTrue(any("not empty" in item for item in kept), kept)


def fake_run(root: Path, name: str, statuses=("resolved", "unresolved", "resolved"), **options) -> Path:
    run = root / name
    if run.exists():
        return run
    write_json(run / "summary.json", summary(name, list(statuses), **options))
    for i in (1, 2, 3):
        attempt = run / "attempts" / f"owner__repo-{i}"
        write_json(attempt / "attempt.json", {"agent_status": "completed", "errors": []})
        write_json(attempt / "telemetry.json", {"parent_tool_timing": {"by_tool": {"bash": 2}}})
        (attempt / "patch.diff").write_text("diff --git a/x b/x\n")
        (attempt / "parent.events.jsonl").write_text("EVENTS\n")
        (attempt / "parent.stderr.log").write_text("STDERR\n")
        (attempt / "issue.md").write_text("ISSUE\n")
        (attempt / "artifacts/sessions").mkdir(parents=True)
        (attempt / "artifacts/sessions/s.jsonl").write_text("SESSION\n")
    for top in ("summary.csv", "summary.md", "manifest.json", "execution.json", "predictions.jsonl", "grading.json",
                "grading-strict.json", "builds.json", "narrative.md", "build-owner__repo-1.log", "builds.partial.json"):
        (run / top).write_text(top)
    reports = run / "grading" / f"{name}-abc" / "logs/evaluation" / f"{name}-abc" / "model" / "owner__repo-1"
    reports.mkdir(parents=True)
    for leaf in ("report.json", "test_output.txt", "run_instance.log", "eval.sh", "patch.diff"):
        (reports / leaf).write_text(leaf)
    (run / "grading" / f"{name}-abc" / "grader.log").write_text("grader")
    (run / "grading" / f"{name}-abc" / "task-repo/tasks/owner__repo-1").mkdir(parents=True)
    (run / "grading" / f"{name}-abc" / "task-repo/tasks/owner__repo-1/gold.patch").write_text("GOLD")
    (run / "build/profile").mkdir(parents=True)
    (run / "build/profile/settings.json").write_text("{}")
    return run


class SlimRunTests(Temp):
    def files(self, directory):
        return sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file())

    def test_cache_and_artifact_sets(self):
        run = fake_run(self.root, "run-a")
        (run / "attempts/owner__repo-1/linked.json").symlink_to(run / "summary.json")
        (run / "attempt-link").symlink_to(run / "attempts", target_is_directory=True)
        code, out, err = invoke("slim-run", "--run", run, "--out", self.root / "cache", "--kind", "cache")
        self.assertEqual(code, 0, err)
        cache = self.files(self.root / "cache")
        self.assertEqual(cache, sorted(
            ["execution.json", "grading-strict.json", "grading.json", "manifest.json", "predictions.jsonl",
             "summary.csv", "summary.json", "summary.md"]
            + [f"attempts/owner__repo-{i}/{leaf}" for i in (1, 2, 3) for leaf in ("attempt.json", "telemetry.json")]))
        code, out, err = invoke("slim-run", "--run", run, "--out", self.root / "handoff", "--kind", "handoff")
        self.assertEqual(code, 0, err)
        handoff = self.files(self.root / "handoff")
        # Uploaded artifacts are public on a public repository: the manifest and execution record stay in the cache.
        self.assertEqual(handoff, sorted(set(cache) - {"manifest.json", "execution.json"}))
        code, out, err = invoke("slim-run", "--run", run, "--out", self.root / "art", "--kind", "artifact")
        self.assertEqual(code, 0, err)
        artifact = self.files(self.root / "art")
        self.assertNotIn("manifest.json", artifact)
        self.assertNotIn("execution.json", artifact)
        reports = "grading/run-a-abc/logs/evaluation/run-a-abc/model/owner__repo-1/"
        self.assertEqual(sorted(set(artifact) - set(handoff)), sorted(
            ["builds.json", "narrative.md", "build-owner__repo-1.log", "grading/run-a-abc/grader.log",
             reports + "report.json", reports + "run_instance.log", reports + "test_output.txt"]
            + [f"attempts/owner__repo-{i}/patch.diff" for i in (1, 2, 3)]))
        joined = "".join((self.root / "art" / path).read_text() for path in artifact)
        for marker in ("EVENTS", "STDERR", "ISSUE", "SESSION", "GOLD"):
            self.assertNotIn(marker, joined)
        self.assertFalse(any((self.root / "art").rglob("linked.json")))

    def test_refuses_bad_destinations_and_runs_without_summary(self):
        run = fake_run(self.root, "run-a")
        self.assertEqual(invoke("slim-run", "--run", run, "--out", run / "inside", "--kind", "cache")[0], 2)
        self.write("busy/file", "x")
        self.assertEqual(invoke("slim-run", "--run", run, "--out", self.root / "busy", "--kind", "cache")[0], 2)
        (self.root / "empty").mkdir()
        self.assertEqual(invoke("slim-run", "--run", self.root / "empty", "--out", self.root / "o", "--kind", "cache")[0], 2)
        self.assertEqual(invoke("slim-run", "--run", run, "--out", self.root / "o", "--kind", "all")[0], 2)

    def test_narrate_reads_the_same_facts_from_a_cache_or_handoff_copy(self):
        from evals.swebench import narrate
        runs = [fake_run(self.root, "run-a"), fake_run(self.root, "run-b", ("resolved", "resolved", "resolved"))]
        for kind in ("cache", "handoff"):
            slim = []
            for run in runs:
                self.assertEqual(invoke("slim-run", "--run", run, "--out", self.root / f"{kind}-{run.name}",
                                        "--kind", kind)[0], 0)
                slim.append(self.root / f"{kind}-{run.name}")
            facts = [narrate.canonical(narrate.build_facts([narrate.load_summary(run) for run in pair], pair))
                     for pair in (runs, slim)]
            self.assertEqual(facts[0], facts[1], kind)


class ScanTests(Temp):
    ENV = {"PI_AUTH": PI_AUTH, "EVAL_ENV": EVAL_ENV, "EXTRA_SECRET": "extra-secret-value-42"}

    def scan(self, *extra):
        return invoke("scan-artifacts", "--dir", self.root / "art", *extra, env=self.ENV)

    def test_clean_tree_and_prose_pass(self):
        self.write("art/test_output.txt", "token: x\nsecret = None\napi_key: placeholder\nBearer short\n")
        code, out, _ = self.scan()
        self.assertEqual((code, json.loads(out)["findings"]), (0, []))

    def test_exact_values_and_token_shapes_fail_without_printing_content(self):
        self.write("art/a.log", f"leak {AUTH_TOKEN}\n")
        self.write("art/b.json", json.dumps({"v": ENV_TOKEN + '"quoted'}))
        self.write("art/c.txt", "extra-secret-value-42")
        self.write("art/d.txt", "-----BEGIN OPENSSH PRIVATE KEY-----\n")
        self.write("art/e.txt", "AKIAABCDEFGHIJKLMNOP")
        self.write("art/f.txt", "ghp_" + "a" * 36)
        self.write("art/g.txt", "xoxb-1234567890-abc")
        self.write("art/h.txt", "Authorization: Bearer " + "x" * 30)
        self.write("art/i.txt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig")
        (self.root / "art/j").symlink_to(self.root / "art/a.log")
        code, out, err = self.scan("--extra-env", "EXTRA_SECRET")
        self.assertEqual(code, 1)
        rules = {finding["path"]: finding["rules"] for finding in json.loads(out)["findings"]}
        self.assertEqual(rules["a.log"][0], "secret-value")
        self.assertIn("secret-value", rules["b.json"])
        self.assertEqual(rules["c.txt"], ["secret-value"])
        self.assertEqual(rules["d.txt"], ["private-key"])
        self.assertEqual(rules["e.txt"], ["aws-access-key"])
        self.assertEqual(rules["f.txt"], ["github-token"])
        self.assertEqual(rules["g.txt"], ["slack-token"])
        self.assertEqual(rules["h.txt"], ["bearer-token"])
        self.assertEqual(rules["i.txt"], ["jwt"])
        self.assertEqual(rules["j"], ["symlink"])
        for value in (AUTH_TOKEN, ENV_TOKEN, "extra-secret-value-42", "AKIAABCDEFGHIJKLMNOP"):
            self.assertNotIn(value, out + err)
        self.assertEqual(self.scan("--extra-env", "bad name")[0], 2)

    def test_encoded_values_and_new_token_shapes(self):
        import base64
        from urllib.parse import quote
        self.write("art/b64.txt", base64.b64encode(AUTH_TOKEN.encode()).decode())
        self.write("art/b64url.txt", base64.urlsafe_b64encode(b"key/with+chars?" + ENV_TOKEN.encode()).decode())
        self.write("art/url.txt", "https://x.invalid/?k=" + quote("typesafe-value-123/x", safe=""))
        self.write("art/json.txt", json.dumps({"v": "line\none-" + ENV_TOKEN}))
        self.write("art/google.txt", "key=AIza" + "B" * 35)
        self.write("art/basic.txt", "Authorization: Basic " + base64.b64encode(b"username:password123").decode())
        self.write("art/prose.txt", "Basic internationalization works\n")
        env = {**self.ENV, "EVAL_ENV": EVAL_ENV + "SLASHED=typesafe-value-123/x\n"
                                       + 'ODD="key/with+chars?' + ENV_TOKEN + '"\n'}
        code, out, _ = invoke("scan-artifacts", "--dir", self.root / "art", env=env)
        self.assertEqual(code, 1)
        rules = {finding["path"]: finding["rules"] for finding in json.loads(out)["findings"]}
        for path in ("b64.txt", "b64url.txt", "url.txt", "json.txt"):
            self.assertIn("secret-value", rules.get(path, []), path)
        self.assertEqual(rules["google.txt"], ["google-api-key"])
        self.assertEqual(rules["basic.txt"], ["basic-auth"])
        self.assertNotIn("prose.txt", rules)

    def test_values_from_the_secrets_setup_files_and_no_silent_empty_scan(self):
        self.write("art/a.log", f"leak {AUTH_TOKEN}\n")
        self.write("art/b.log", f"leak {ENV_TOKEN}\n")
        secrets, env_file = self.root / "secrets", self.write("launch/.env", EVAL_ENV)
        self.write("auth.json", PI_AUTH, secrets)
        clean = {"PI_AUTH": "", "EVAL_ENV": ""}
        code, out, err = invoke("scan-artifacts", "--dir", self.root / "art", env=clean)
        self.assertEqual(code, 2)
        self.assertIn("no secret values to scan for", err)
        code, out, _ = invoke("scan-artifacts", "--dir", self.root / "art", "--secrets-dir", secrets,
                              "--env-file", env_file, env=clean)
        self.assertEqual(code, 1)
        self.assertEqual(sorted(finding["path"] for finding in json.loads(out)["findings"]), ["a.log", "b.log"])
        self.assertNotIn(AUTH_TOKEN, out)
        # Token shapes only: the sk- token is still caught, the gateway key (no known shape) is not.
        code, out, _ = invoke("scan-artifacts", "--dir", self.root / "art", "--no-secret-values", env=clean)
        self.assertEqual((code, json.loads(out)["findings"]), (1, [{"path": "a.log", "rules": ["sk-key"]}]))
        self.assertEqual(invoke("scan-artifacts", "--dir", self.root / "art", "--secrets-dir", self.root / "nope",
                                env=clean)[0], 2)


class VerdictTests(Temp):
    def verdict(self, baseline, candidate, *extra):
        code, out, _ = invoke("verdict", "--baseline", baseline, "--candidate", candidate, *extra)
        return code, json.loads(out)

    def test_score_differences_never_fail(self):
        base = fake_run(self.root, "base", ("resolved", "resolved", "resolved"))
        cand = fake_run(self.root, "cand", ("unresolved", "empty_patch", "unresolved"))
        code, result = self.verdict(base, cand, "--exit-code-baseline", "0", "--exit-code-candidate", "0")
        self.assertEqual((code, result["ok"], result["reasons"]), (0, True, []))

    def test_infrastructure_problems_fail(self):
        base = fake_run(self.root, "base")
        cand = fake_run(self.root, "cand", agent_statuses=("completed", "agent_error", "runner_error"))
        code, result = self.verdict(base, cand)
        self.assertEqual(code, 1)
        self.assertEqual(result["infra_tasks"]["candidate"], ["owner__repo-2", "owner__repo-3"])
        unknown = fake_run(self.root, "unknown", ("resolved", "not_graded", "resolved"))
        code, result = self.verdict(base, unknown)
        self.assertEqual((code, result["unknown_grades"]["candidate"]), (1, ["owner__repo-2"]))
        for exit_code in ("3", "130", "2"):
            self.assertEqual(self.verdict(base, base, "--exit-code-candidate", exit_code)[0], 1)
        code, result = self.verdict(base, self.root / "missing")
        self.assertEqual(code, 1)
        self.assertIn("candidate: summary.json missing or invalid", result["reasons"])

    def test_single_run_mode(self):
        clean = fake_run(self.root, "clean")
        broken = fake_run(self.root, "broken", agent_statuses=("completed", "agent_error", "completed"))
        code, out, _ = invoke("verdict", "--single", clean, "--exit-code", "0")
        self.assertEqual((code, json.loads(out)["ok"], json.loads(out)["infra_tasks"]), (0, True, {"run": []}))
        code, out, _ = invoke("verdict", "--single", broken)
        self.assertEqual((code, json.loads(out)["infra_tasks"]["run"]), (1, ["owner__repo-2"]))
        self.assertEqual(invoke("verdict", "--single", clean, "--exit-code", "3")[0], 1)
        self.assertEqual(invoke("verdict", "--single", self.root / "missing")[0], 1)
        for extra in (("--baseline", clean), ("--exit-code-candidate", "0")):
            self.assertEqual(invoke("verdict", "--single", clean, *extra)[0], 2, extra)
        self.assertEqual(invoke("verdict", "--baseline", clean)[0], 2)
        self.assertEqual(invoke("verdict", "--baseline", clean, "--candidate", clean, "--exit-code", "0")[0], 2)

    def test_timeouts_are_reported_and_fail_only_on_request(self):
        base = fake_run(self.root, "base")
        cand = fake_run(self.root, "cand", ("resolved", "unresolved", "resolved"),
                        agent_statuses=("completed", "timeout", "completed"))
        code, result = self.verdict(base, cand, "--out", self.root / "verdict.json")
        self.assertEqual((code, result["timeouts"]["candidate"]), (0, ["owner__repo-2"]))
        self.assertEqual(json.loads((self.root / "verdict.json").read_text()), result)
        self.assertEqual(self.verdict(base, cand, "--fail-on-timeout")[0], 1)


class AnnotateTests(Temp):
    def annotate(self, base, cand):
        code, out, err = invoke("annotate", "--baseline", base, "--candidate", cand)
        self.assertEqual(code, 0, err)
        return out

    def test_warns_with_counts_only_on_regressions_beyond_noise(self):
        base = fake_run(self.root, "base", ("resolved", "resolved", "resolved"))
        worse = fake_run(self.root, "worse", ("unresolved", "unresolved", "empty_patch"))
        out = self.annotate(base, worse)
        self.assertTrue(out.startswith("::warning title=pepi evals::"), out)
        self.assertIn("3 regression(s), 0 win(s)", out)
        self.assertIn("official resolved 3 -> 0", out)
        self.assertEqual(out.count("\n"), 1)
        self.assertNotIn("owner__repo", out)

    def test_silent_within_noise_on_wins_and_on_incomparable_runs(self):
        base = fake_run(self.root, "base", ("resolved", "resolved", "resolved"))
        slightly = fake_run(self.root, "slightly", ("resolved", "unresolved", "unresolved"))
        better = fake_run(self.root, "better-base", ("unresolved", "unresolved", "unresolved"))
        self.assertEqual(self.annotate(base, slightly), "")
        self.assertEqual(self.annotate(better, base), "")
        self.assertEqual(self.annotate(base, self.root / "missing"), "")
        (self.root / "broken").mkdir()
        (self.root / "broken/summary.json").write_text("{not json")
        self.assertEqual(self.annotate(base, self.root / "broken"), "")


class CommentTests(Temp):
    SHA = "0123456789abcdef0123456789abcdef01234567"

    def comment(self, narrative, base=None, cand=None, **overrides):
        base = base or fake_run(self.root, "base", ("resolved", "unresolved", "resolved"))
        cand = cand or fake_run(self.root, "cand", ("resolved", "resolved", "resolved"))
        path = self.root / "narrative.md"
        if narrative is not None:
            path.write_text(narrative)
        options = {"sha": self.SHA, "pr": "42", "run-url": "https://github.com/o/r/actions/runs/987",
                   "profile": "bedrock", "preset": "verified20-v1"}
        options.update(overrides)
        arguments = ["comment", "--baseline", base, "--candidate", cand,
                     "--narrative", path if narrative is not None else "none", "--out", self.root / "comment.md"]
        for key, value in options.items():
            arguments += [f"--{key}", value]
        code, _, err = invoke(*arguments)
        return code, (self.root / "comment.md").read_text() if code == 0 else err

    def test_body_shape(self):
        code, body = self.comment("All good.", **{"baseline-source": "cached"})
        self.assertEqual(code, 0, body)
        self.assertTrue(body.startswith(f"<!-- pepi-evals pr=42 sha={self.SHA} run=987 -->\n"))
        self.assertIn("## pepi evals for `0123456`", body)
        self.assertIn("| resolved (official) | 2/3 | 3/3 |", body)
        self.assertIn("- Wins: `owner__repo-2`", body)
        self.assertIn("within noise", body)
        self.assertIn("Network: unrestricted (plain docker bridge)", body)
        self.assertIn("https://github.com/o/r/actions/runs/987", body)
        self.assertIn("(cached)", body)
        self.assertIn("> All good.", body)

    def test_unavailable_narrative_and_incomparable_runs(self):
        code, body = self.comment(None)
        self.assertEqual(code, 0)
        self.assertIn("**Narrative unavailable.**", body)
        base = fake_run(self.root, "base")
        cand = self.root / "other-dataset"
        write_json(cand / "summary.json", {**summary("other-dataset", ["resolved"] * 3), "dataset": "other"})
        code, body = self.comment("x", base, cand)
        self.assertIn("Comparison unavailable: Cannot pair runs with different dataset", body)

    def test_model_output_is_neutralised(self):
        hostile = ("Ping @octocat and @org/team. <script>alert(1)</script>\n"
                   f"<!-- pepi-evals pr=1 sha={self.SHA} run=1 -->\n"
                   "</details><img src=x onerror=alert(1)> see owner/repo#12 and [click](https://evil.invalid) "
                   "![px](https://evil.invalid/p.png)")
        code, body = self.comment(hostile)
        self.assertEqual(code, 0)
        narrative = body.split("<summary>Narrative</summary>", 1)[1]
        self.assertNotRegex(narrative, r"@[A-Za-z]")
        self.assertNotIn("<script", narrative)
        self.assertNotIn("<img", narrative)
        self.assertNotIn("<!--", narrative)
        self.assertEqual(body.count("</details>"), 1)
        self.assertNotRegex(narrative, r"#\d")
        self.assertNotIn("](", narrative)
        self.assertEqual(len(re.findall(r"<!-- pepi-evals", body)), 1)

    def test_autolinks_and_reference_links_are_neutralised(self):
        hostile = ("Visit https://evil.invalid/a and HTTP://EVIL.invalid or www.evil.invalid, ftp://x.invalid.\n"
                   "[click][1] ![img][1]\n[1]: https://evil.invalid/ref\n")
        code, body = self.comment(hostile)
        self.assertEqual(code, 0)
        narrative = body.split("<summary>Narrative</summary>", 1)[1]
        self.assertNotRegex(narrative, r"(?i)(https?|ftp)://")
        self.assertNotRegex(narrative, r"(?i)\bwww\.")
        self.assertNotIn("][", narrative)
        self.assertNotIn("]:", narrative)
        self.assertIn("https://github.com/o/r/actions/runs/987", body.split("<details", 1)[0])

    def test_size_budget_covers_the_header_and_long_lists(self):
        many_ids = [f"owner__repo-{i}-" + "x" * 120 for i in range(400)]
        crafted = {"wins": many_ids, "regressions": many_ids, "inconclusive": many_ids,
                   "configuration_differences": {f"setting_{i:03d}_" + "y" * 150: {} for i in range(300)},
                   "exceeds_noise_threshold": False}
        verdict_result = {"ok": False, "reasons": ["candidate: x" * 50] * 40,
                          "infra_tasks": {"baseline": many_ids, "candidate": many_ids},
                          "timeouts": {"baseline": many_ids, "candidate": many_ids}}
        with patch.object(ci, "comparison", return_value=(crafted, None)), \
                patch.object(ci, "verdict", return_value=verdict_result):
            for narrative in ("short narrative", "long narrative line\n" * 20000):
                code, body = self.comment(narrative)
                self.assertEqual(code, 0)
                self.assertLessEqual(len(body), ci.COMMENT_LIMIT)
                self.assertIn("more", body)
                self.assertTrue(body.startswith("<!-- pepi-evals pr=42"))
                self.assertIn("https://github.com/o/r/actions/runs/987", body)
        head = ci.comment_head(None, None, crafted, None, verdict_result, self.SHA, "42", "987",
                               "https://github.com/o/r/actions/runs/987", "bedrock", "p", "", 30, 20)
        self.assertIn("… and 280 more", head)
        self.assertIn("(+370 more)", head)
        # Tight limits exercise the fallbacks: shorter lists, then a truncated head; never over the limit.
        for head_limit, comment_limit in ((9000, 11000), (3000, 5000)):
            with patch.object(ci, "comparison", return_value=(crafted, None)), \
                    patch.object(ci, "verdict", return_value=verdict_result), \
                    patch.object(ci, "HEAD_LIMIT", head_limit), patch.object(ci, "COMMENT_LIMIT", comment_limit):
                code, body = self.comment("narrative\n" * 5000)
            with self.subTest(head_limit=head_limit):
                self.assertEqual(code, 0)
                self.assertLessEqual(len(body), comment_limit)
                self.assertTrue(body.startswith("<!-- pepi-evals pr=42"))
                self.assertIn("https://github.com/o/r/actions/runs/987", body)

    def test_long_narrative_is_truncated_under_the_limit(self):
        code, body = self.comment("line of narrative text\n" * 10000)
        self.assertEqual(code, 0)
        self.assertLessEqual(len(body), ci.COMMENT_LIMIT)
        self.assertIn("narrative truncated", body)

    def test_invalid_inputs_are_usage_errors(self):
        for override in ({"sha": "XYZ"}, {"pr": "4 2"}, {"run-url": "javascript:alert(1)"},
                         {"profile": "a b"}, {"run-url": "https://x/ y"}):
            self.assertEqual(self.comment("x", **override)[0], 2, override)


FAKE_PI = """\
import json, os, sys
from pathlib import Path
agent = Path(os.environ["PI_CODING_AGENT_DIR"])
record = {"settings": json.loads((agent / "settings.json").read_text()),
          "auth": (agent / "auth.json").read_text(), "argv": sys.argv[1:],
          "env": sorted(os.environ), "litellm_key": os.environ.get("LITELLM_API_KEY"),
          "mode": oct((agent / "auth.json").stat().st_mode & 0o777)}
Path(RECORD).write_text(json.dumps(record))
sys.stdin.read()
if os.environ.get("LITELLM_BASE_URL") == "https://fail.invalid":
    print("boom", file=sys.stderr)
    raise SystemExit(1)
print("FAKE NARRATIVE")
"""


class NarrateWrapperTests(Temp):
    def setUp(self):
        super().setUp()
        self.base, self.cand = fake_run(self.root, "base"), fake_run(self.root, "cand")
        self.secrets = self.root / "secrets"
        self.secrets.mkdir(mode=0o700)
        (self.secrets / "auth.json").write_text(PI_AUTH)
        self.config = self.root / "config"
        write_json(self.config / "settings.json", {
            "defaultProvider": "litellm", "defaultModel": "m", "theme": "dark", "pi-memory-mem0": {"k": "v"},
            "packages": ["npm:pi-provider-litellm", "npm:pi-subagents@0.78.0", "npm:pi-web-access"]})
        self.record = self.root / "record.json"
        script = self.root / "fake_pi.py"
        script.write_text(f"RECORD = {str(self.record)!r}\n" + FAKE_PI)
        self.pi = f"{sys.executable} {script}"

    def narrate(self, env_text):
        env_file = self.root / ".env"
        env_file.write_text(env_text)
        return invoke("narrate", "--baseline", self.base, "--candidate", self.cand, "--output", self.root / "n.md",
                      "--secrets-dir", self.secrets, "--config-dir", self.config, "--env-file", env_file,
                      "--pi-command", self.pi, "--litellm-version", "4.2.0", "--timeout-seconds", "60")

    def test_private_agent_dir_minimal_settings_and_gateway_env(self):
        code, out, err = self.narrate(EVAL_ENV)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["exit_code"], 0)
        self.assertIn("FAKE NARRATIVE", (self.root / "n.md").read_text())
        record = json.loads(self.record.read_text())
        self.assertEqual(record["settings"], {"defaultProvider": "litellm", "defaultModel": "m",
                                              "packages": ["npm:pi-provider-litellm@4.2.0"]})
        self.assertEqual((record["auth"], record["litellm_key"], record["mode"]), (PI_AUTH, ENV_TOKEN, "0o600"))
        self.assertNotIn("TYPESAFE_API_KEY", record["env"])
        self.assertNotIn(AUTH_TOKEN, out + err)

    def test_failure_is_reported_as_unavailable_with_exit_zero(self):
        code, out, err = self.narrate("LITELLM_BASE_URL=https://fail.invalid\n")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["narrative"], "unavailable")
        self.assertFalse((self.root / "n.md").exists())
        self.assertIn("narrative unavailable", err)
        (self.secrets / "auth.json").unlink()
        code, out, _ = self.narrate(EVAL_ENV)
        self.assertEqual((code, json.loads(out)["narrative"]), (0, "unavailable"))


FAKE_HARNESS = """#!/bin/bash
{ printf 'cwd=%s\\n' "$(pwd)"; env | grep -E '^(PEPI_EVAL_|PI_AUTH=|EVAL_ENV=)' | sort; } > "$PEPI_TEST_RECORD"
exit "${PEPI_TEST_EXIT:-0}"
"""


class DestructiveScriptGuardTests(Temp):
    """free-disk.sh / prune-images.sh must refuse outside the CI runner kind they are for, before running
    anything. Every command they could use is a stub first on PATH that records the call and fails."""
    STUBS = ("docker", "sudo", "rm", "systemctl", "tee", "mkdir", "xargs", "df", "id", "uname", "awk", "python3")

    def setUp(self):
        super().setUp()
        self.calls = self.root / "calls.log"
        stubs = self.root / "stubs"
        for name in self.STUBS:
            self.write(name, f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\nexit 97\n', stubs).chmod(0o755)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("GITHUB_", "RUNNER_"))}
        self.env["PATH"] = f"{stubs}:/usr/bin:/bin"

    def run_script(self, name, *arguments, **env):
        return subprocess.run(["/bin/bash", str(REPO / "evals/ci" / name), *arguments], env={**self.env, **env},
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)

    def assert_refused(self, result, needle):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(needle, result.stderr)
        self.assertFalse(self.calls.exists(), self.calls.read_text() if self.calls.exists() else "")

    def test_free_disk_needs_a_github_hosted_runner(self):
        for env in ({}, {"GITHUB_ACTIONS": "true"}, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"},
                    {"GITHUB_ACTIONS": "false", "RUNNER_ENVIRONMENT": "github-hosted"},
                    {"RUNNER_ENVIRONMENT": "github-hosted"}):
            with self.subTest(env=env):
                self.assert_refused(self.run_script("free-disk.sh", **env), "refusing: only for GitHub-hosted runners")
        self.assert_refused(self.run_script("free-disk.sh", "--now", GITHUB_ACTIONS="true",
                                            RUNNER_ENVIRONMENT="github-hosted"), "takes no arguments")

    def test_prune_images_needs_github_actions_and_takes_no_flags(self):
        for env in ({}, {"GITHUB_ACTIONS": "false"}, {"RUNNER_ENVIRONMENT": "self-hosted"}):
            with self.subTest(env=env):
                self.assert_refused(self.run_script("prune-images.sh", **env), "refusing: only for GitHub Actions")
        # The build-cache wipe is gone: an argument is a usage error before docker is touched.
        self.assert_refused(self.run_script("prune-images.sh", "--build-cache", GITHUB_ACTIONS="true"),
                            "takes no arguments")


class RunEvalsScriptTests(Temp):
    def setUp(self):
        super().setUp()
        self.tree = self.root / "tree"
        (self.tree / "evals/ci").mkdir(parents=True)
        shutil.copyfile(REPO / "evals/ci/run-evals.sh", self.tree / "evals/ci/run-evals.sh")
        self.write("evals/pepi-eval.sh", FAKE_HARNESS, self.tree).chmod(0o755)
        bin_dir = self.root / "bin"
        self.write("docker", "#!/bin/bash\necho \"$@\" >> \"$PEPI_TEST_DOCKER\"\n"
                   "[[ $1 == network && $2 == inspect ]] && exit 1\nexit 0\n", bin_dir).chmod(0o755)
        self.secrets = self.root / "secrets"
        self.write("auth.json", "{}", self.secrets)
        self.launch = self.root / "launch"
        self.launch.mkdir()
        self.config = self.root / "config"
        self.write("settings.json", "{}", self.config)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("PEPI_", "GITHUB_"))}
        self.env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", PEPI_CI_SECRETS_DIR=str(self.secrets),
                        PEPI_CI_LAUNCH_DIR=str(self.launch), PEPI_TEST_RECORD=str(self.root / "record"),
                        PEPI_TEST_DOCKER=str(self.root / "docker.log"), PEPI_EVAL_PROFILE="leak",
                        PEPI_EVAL_IDS="leak", GITHUB_OUTPUT=str(self.root / "gh-output"),
                        PI_AUTH='{"k": "raw-secret"}', EVAL_ENV="A=raw-secret")

    def run_script(self, *arguments, **env):
        return subprocess.run(["bash", str(self.tree / "evals/ci/run-evals.sh"), *map(str, arguments)],
                              env={**self.env, **env}, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def test_sets_every_answer_and_forwards_the_exit_code(self):
        result = self.run_script("candidate", self.config, "pr-42", "pr-42-run", PEPI_TEST_EXIT="3",
                                 PEPI_CI_PRESET="multilingual10-v1", PEPI_CI_TASK_REPO="/tasks")
        self.assertEqual(result.returncode, 3, result.stderr)
        lines = (self.root / "record").read_text().splitlines()
        self.assertEqual(os.path.realpath(lines[0].removeprefix("cwd=")), os.path.realpath(self.launch))
        env = dict(line.split("=", 1) for line in lines[1:])
        self.assertEqual(os.path.realpath(env.pop("PEPI_EVAL_CONFIG_DIR")), os.path.realpath(self.config))
        self.assertEqual(env, {
            "PEPI_EVAL_AUTO_FIX": "0",
            "PEPI_EVAL_CONFIG_LABEL": "pr-42", "PEPI_EVAL_DROP_MCP": "0", "PEPI_EVAL_LITELLM_VERSION": "4.2.0",
            "PEPI_EVAL_NETWORK": "pepi-eval", "PEPI_EVAL_NODE_IMAGE": "node:22-bookworm-slim",
            "PEPI_EVAL_PI_VERSION": "1.1.0", "PEPI_EVAL_PRESET": "multilingual10-v1",
            "PEPI_EVAL_RESTORE_TEST_FILES": "1", "PEPI_EVAL_RUN_NAME": "pr-42-run",
            "PEPI_EVAL_SECRETS_DIR": str(self.secrets), "PEPI_EVAL_STRIP_TEST_EDITS": "1",
            "PEPI_EVAL_SUBAGENTS_VERSION": "0.78.0", "PEPI_EVAL_TASK_REPO": "/tasks",
            "PEPI_EVAL_USE_DOTENV": "1", "PEPI_EVAL_USE_HOST_AUTH": "0", "PEPI_EVAL_USE_HOST_MCP": "0",
            "PEPI_EVAL_USE_HOST_SKILLS": "0", "PEPI_EVAL_YES": "1"})
        self.assertNotIn("PI_AUTH", env)
        self.assertNotIn("EVAL_ENV", env)
        self.assertIn("network create pepi-eval", (self.root / "docker.log").read_text())
        output = (self.root / "gh-output").read_text()
        self.assertIn("exit_code=3", output)
        run_dir = re.search(r"^run_dir=(.*)$", output, re.M).group(1)
        self.assertEqual(os.path.realpath(run_dir), os.path.realpath(self.tree / "evals/swebench/runs/pr-42-run"))

    def test_usage_errors(self):
        self.assertEqual(self.run_script("other", self.config, "main", "r").returncode, 2)
        self.assertEqual(self.run_script("baseline", self.root / "missing", "main", "r").returncode, 2)
        self.assertEqual(self.run_script("baseline", self.config, "bad label", "r").returncode, 2)
        self.assertEqual(self.run_script("baseline", self.config, "main", "r",
                                         PEPI_CI_SECRETS_DIR=str(self.root)).returncode, 2)
        self.assertFalse((self.root / "record").exists())


if __name__ == "__main__":
    unittest.main()
