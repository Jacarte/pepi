"""Review fixes and CI config/secret injection. Synthetic tasks, fake Docker, no model spend."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.swebench import __main__ as cli
from evals.swebench import runner
from evals.swebench.common import read_json, read_manifest, write_json
from evals.swebench.prepare import prepare_run
from evals.swebench.runner import (PI_COMMAND, SECRET_DIR, Secrets, build_images, run_one, run_tasks,
                                   stop_agents)
from evals.swebench.tests.test_runner import TASK, VERSIONS, FakeDocker, prepare_fixture

SENTINEL = "SENTINEL-SECRET-0123456789"
ENV = {"LITELLM_BASE_URL": "https://example.invalid", "LITELLM_API_KEY": SENTINEL + "-env"}


def write_settings(directory: Path, **extra):
    directory.mkdir(parents=True, exist_ok=True)
    settings = {"defaultProvider": "litellm", "defaultModel": "model-a"}
    settings.update(extra)
    (directory / "settings.json").write_text(json.dumps(settings))
    (directory / "AGENTS.md").write_text("rules\n")


class RecordingDocker(FakeDocker):
    """FakeDocker that also keeps kwargs (stdin) and can leak a secret into the patch."""
    def __init__(self, failure=None, patch_bytes=None):
        super().__init__(failure)
        self.kwargs = []
        self.patch_bytes = patch_bytes

    def call(self, *args, **kwargs):
        self.kwargs.append(kwargs)
        result = super().call(*args, **kwargs)
        if self.patch_bytes is not None and "--cached" in args:
            return SimpleNamespace(stdout=self.patch_bytes, returncode=0)
        return result


class PrepareConfigTests(unittest.TestCase):
    def prepare(self, root, run, **kwargs):
        return prepare_run(root, run, [TASK], [TASK["instance_id"]], VERSIONS, "node:22", **kwargs)

    def test_config_dir_overrides_with_per_file_fallback_and_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            write_settings(base / "repo")
            (base / "repo/settings.json").write_text(json.dumps(
                {"defaultProvider": "litellm", "defaultModel": "repo-model"}))
            write_settings(base / "injected", defaultModel="injected-model")
            (base / "injected/AGENTS.md").unlink()  # falls back to the repo copy
            manifest = self.prepare(base / "repo", base / "run-1", config_dir=base / "injected",
                                    config_label="variant-b")
            profile = read_json(base / "run-1/build/profile/settings.json")
            self.assertEqual(profile["defaultModel"], "injected-model")
            self.assertEqual(manifest["config"]["label"], "variant-b")
            self.assertEqual(manifest["config"]["source"], "injected")
            self.assertEqual(set(manifest["config"]["files"]), {"settings.json", "AGENTS.md"})
            self.assertEqual((base / "run-1/build/profile/AGENTS.md").read_text(), "rules\n")

    def test_different_configs_get_different_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            write_settings(base / "repo")
            write_settings(base / "a", defaultModel="a")
            write_settings(base / "b", defaultModel="b")
            first = self.prepare(base / "repo", base / "r1", config_dir=base / "a")
            second = self.prepare(base / "repo", base / "r2", config_dir=base / "b")
            self.assertNotEqual(first["config_id"], second["config_id"])

    def test_secret_files_in_config_dir_fail_prepare(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            write_settings(base / "repo")
            write_settings(base / "injected")
            (base / "injected/auth.json").write_text(SENTINEL)
            with self.assertRaisesRegex(ValueError, "secrets-dir"):
                self.prepare(base / "repo", base / "run-1", config_dir=base / "injected")
            self.assertFalse((base / "run-1").exists())  # failed prepare cleans up

    def test_mcp_requires_explicit_drop(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            write_settings(base / "repo", packages=["npm:pi-mcp-adapter"])
            with self.assertRaisesRegex(ValueError, "--drop-mcp"):
                self.prepare(base / "repo", base / "run-1")
            manifest = self.prepare(base / "repo", base / "run-2", drop_mcp=True)
            dropped = manifest["config"]["dropped_features"]
            self.assertTrue(dropped["mcp"])
            self.assertIn("npm:pi-mcp-adapter", dropped["packages"])
            self.assertNotIn("mcp-adapter", json.dumps(read_json(base / "run-2/build/profile/settings.json")))

    def test_secret_file_planted_in_build_is_rejected_and_tamper_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            (run / "build/profile/auth.json").write_text(SENTINEL)
            with self.assertRaisesRegex(ValueError, "Secret file"):
                read_manifest(run)

    def test_manifest_detects_edited_issue_text_and_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            manifest = read_json(run / "manifest.json")
            manifest["tasks"][0]["problem_statement"] = "Something else"
            write_json(run / "manifest.json", manifest)
            with self.assertRaisesRegex(ValueError, "Issue text"):
                read_manifest(run)
            manifest = read_json(run / "manifest.json")
            manifest["tasks"][0]["problem_statement"] = TASK["problem_statement"]
            manifest["node_image"] = "attacker/image"
            write_json(run / "manifest.json", manifest)
            with self.assertRaisesRegex(ValueError, "node image"):
                read_manifest(run)


class SecretTests(unittest.TestCase):
    def secrets_dir(self, base, mode=0o600, name="auth.json"):
        directory = base / "secrets"
        directory.mkdir()
        path = directory / name
        path.write_text(json.dumps({"litellm": {"apiKey": SENTINEL}}))
        path.chmod(mode)
        return directory

    def run_with_secrets(self, docker, base):
        secrets = Secrets.load(self.secrets_dir(base))
        output = base / "attempt"
        with patch.dict(os.environ, ENV):
            row = run_one(docker, TASK, {"image_id": "sha256:frozen"}, output, 1, "eval", "2", "4g",
                          secrets, "run-1")
        return row, output, secrets

    def test_secrets_are_delivered_over_stdin_to_a_tmpfs_only(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            docker = RecordingDocker()
            row, output, _ = self.run_with_secrets(docker, base)
            create = next(c for c in docker.commands if c[0] == "create")
            self.assertTrue(any(a.startswith(f"{SECRET_DIR}:") and "uid=10101" in a for a in create))
            for forbidden in ("-v", "--volume", "--mount"):
                self.assertNotIn(forbidden, create)
            self.assertIn("pepi-eval.run=run-1", create)
            index = next(i for i, c in enumerate(docker.commands) if "cat >" in " ".join(c))
            self.assertIn(SENTINEL.encode(), docker.kwargs[index]["input"])
            self.assertIn("--user", docker.commands[index])
            self.assertIn("10101", docker.commands[index])
            # The value appears in no argv (docker cp into tmpfs is unsupported, so none is used).
            self.assertFalse(any(SENTINEL in part for c in docker.commands for part in c))
            self.assertEqual(row["agent_status"], "completed")

    def test_sentinel_absent_from_run_files_and_patch_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            leaky = f"+key = '{SENTINEL}'\n".encode()
            docker = RecordingDocker(patch_bytes=leaky)
            original = docker.call
            def call(*args, **kwargs):
                if args[0] == "cp" and args[1].endswith("/output/."):
                    target = Path(args[2]); target.mkdir(exist_ok=True)
                    (target / "sessions.jsonl").write_text(f'{{"env": "{SENTINEL}"}}')
                return original(*args, **kwargs)
            docker.call = call
            row, output, _ = self.run_with_secrets(docker, base)
            self.assertGreaterEqual(row["secret_redactions"], 2)
            self.assertNotIn(SENTINEL, row["patch"])
            for path in output.rglob("*"):
                if path.is_file():
                    self.assertNotIn(SENTINEL.encode(), path.read_bytes(), path)
            self.assertIn("<redacted>", (output / "patch.diff").read_text())

    def test_exception_text_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            docker = RecordingDocker()
            original = docker.call
            def call(*args, **kwargs):
                if args[0] == "start":
                    raise subprocess.CalledProcessError(1, ["docker", "x", SENTINEL])
                return original(*args, **kwargs)
            docker.call = call
            row, output, _ = self.run_with_secrets(docker, base)
            self.assertEqual(row["agent_status"], "setup_error")
            self.assertNotIn(SENTINEL, json.dumps(row))
            self.assertNotIn(SENTINEL, (output / "attempt.json").read_text())

    def test_loose_permissions_and_unexpected_files_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            old = os.umask(0o077)  # the mode must come from chmod, not from the umask
            try:
                with self.assertRaisesRegex(ValueError, "chmod 600"):
                    Secrets.load(self.secrets_dir(base, mode=0o644))
            finally:
                os.umask(old)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Unexpected"):
                Secrets.load(self.secrets_dir(Path(directory), name="mcp.json"))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Unexpected"):
                Secrets.load(self.secrets_dir(Path(directory), name="notes.txt"))

    def test_redaction_fails_closed_on_symlinks_and_unreadable_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            secrets = Secrets.load(self.secrets_dir(base))
            tree = base / "tree"
            (tree / "sub").mkdir(parents=True)
            (tree / "sub/ok.txt").write_text(f"key={SENTINEL}")
            outside = base / "host-secret"
            outside.write_text(SENTINEL)
            (tree / "link").symlink_to(outside)
            (tree / "sub/dir-link").symlink_to(base)
            locked = tree / "locked.txt"
            locked.write_text(SENTINEL)
            locked.chmod(0)
            count, removed = secrets.redact_tree(tree)
            self.assertEqual(count, 2)  # ok.txt and the chmod-000 file we could repair
            self.assertEqual(sorted(removed), ["link", "sub/dir-link"])
            self.assertNotIn(SENTINEL, (tree / "sub/ok.txt").read_text())
            self.assertFalse((tree / "link").is_symlink() or (tree / "link").exists())
            self.assertTrue(outside.exists())  # the target itself is untouched
            # chmod 000 is repaired then redacted (we own the file); it must not stay raw.
            self.assertNotIn(SENTINEL.encode(), locked.read_bytes())

    def test_bookkeeping_failure_removes_raw_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            docker = RecordingDocker()
            original = docker.call
            def call(*args, **kwargs):
                if args[0] == "cp" and args[1].endswith("/output/."):
                    target = Path(args[2]); target.mkdir(exist_ok=True)
                    (target / "raw.txt").write_text(SENTINEL)
                return original(*args, **kwargs)
            docker.call = call
            secrets = Secrets.load(self.secrets_dir(base))
            with patch.object(Secrets, "redact_tree", side_effect=OSError("boom")), \
                 patch.dict(os.environ, ENV):
                row = run_one(docker, TASK, {"image_id": "sha256:frozen"}, base / "attempt", 1,
                              "eval", "2", "4g", secrets)
            self.assertEqual(row["agent_status"], "runner_error")
            self.assertFalse((base / "attempt/artifacts").exists())
            self.assertFalse((base / "attempt/patch.diff").exists())
            self.assertEqual(row["patch"], "")

    def test_failed_create_does_not_collect_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = RecordingDocker("setup")
            row = run_one(docker, TASK, {"image_id": "sha256:frozen"}, Path(directory) / "a", 1, "eval", "2", "4g")
            self.assertEqual(row["errors"][0][:19], "CalledProcessError:")
            self.assertEqual(len(row["errors"]), 1)  # no spurious "Artifact collection failed"
            self.assertFalse(any(c[0] == "cp" for c in docker.commands))
            self.assertEqual(docker.commands[-1][0], "rm")

    def test_no_secrets_dir_means_no_tmpfs(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = RecordingDocker()
            run_one(docker, TASK, {"image_id": "sha256:frozen"}, Path(directory) / "a", 1, "eval", "2", "4g")
            create = next(c for c in docker.commands if c[0] == "create")
            self.assertNotIn("--tmpfs", create)

    def test_secrets_recorded_by_name_only_and_label_in_predictions(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            write_settings(base / "repo")
            run = base / "run-1"
            prepare_run(base / "repo", run, [TASK], [TASK["instance_id"]], VERSIONS, "node:22",
                        config_label="variant-b")
            write_json(run / "builds.json", {TASK["instance_id"]: {"image_id": "sha256:frozen"}})
            secrets = self.secrets_dir(base)
            with patch.dict(os.environ, ENV), patch("evals.swebench.runner.Docker", return_value=RecordingDocker()):
                self.assertEqual(run_tasks(run, 1, "eval", "2", "4g", secrets), 0)
            execution = (run / "execution.json").read_text()
            self.assertNotIn(SENTINEL, execution)
            self.assertEqual(json.loads(execution)["secrets"]["auth.json"], {"present": True})
            prediction = json.loads((run / "predictions.jsonl").read_text().strip())
            self.assertTrue(prediction["model_name_or_path"].startswith("variant-b-pepi-"))
            for path in run.rglob("*"):
                if path.is_file():
                    self.assertNotIn(SENTINEL.encode(), path.read_bytes(), path)


class RunnerFixTests(unittest.TestCase):
    def test_stop_agents_kills_again_until_processes_are_gone(self):
        class Docker:
            def __init__(self): self.commands, self.alive = [], 2
            def call(self, *args, **kwargs):
                self.commands.append(args[2])
                if args[2] == "pgrep":
                    if self.alive:
                        self.alive -= 1
                        return SimpleNamespace()
                    raise subprocess.CalledProcessError(1, args)
                if args[2] == "pkill":
                    raise subprocess.CalledProcessError(1, args)
        docker = Docker()
        stop_agents(docker, "c")
        self.assertEqual(docker.commands.count("pkill"), 3)

    def test_stop_agents_gives_up_and_surfaces_other_errors(self):
        class Survivors:
            def call(self, *args, **kwargs):
                if args[2] == "pkill":
                    raise subprocess.CalledProcessError(1, args)
                return SimpleNamespace()
        with patch("evals.swebench.runner.time.monotonic", side_effect=[0, 0, 11]), \
             patch("evals.swebench.runner.time.sleep"), self.assertRaises(RuntimeError):
            stop_agents(Survivors(), "c")
        class Broken:
            def call(self, *args, **kwargs):
                raise subprocess.CalledProcessError(2, args)
        with self.assertRaises(subprocess.CalledProcessError):
            stop_agents(Broken(), "c")

    def test_non_utf8_patch_is_kept_byte_for_byte(self):
        raw = b"diff --git a/x b/x\n+caf\xe9\n"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "attempt"
            row = run_one(RecordingDocker(patch_bytes=raw), TASK, {"image_id": "sha256:frozen"},
                          output, 1, "eval", "2", "4g")
            self.assertEqual((output / "patch.diff").read_bytes(), raw)
            self.assertEqual(row["patch_encoding"], "non-utf8")
            self.assertEqual(row["agent_status"], "completed")

    def test_patch_export_pins_output_format_and_issue_is_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = RecordingDocker()
            output = Path(directory) / "attempt"
            run_one(docker, TASK, {"image_id": "sha256:frozen"}, output, 1, "eval", "2", "4g")
            diff = next(c for c in docker.commands if "--cached" in c)
            for flag in ("--no-ext-diff", "--no-textconv", "--no-color", "--src-prefix=a/", "--dst-prefix=b/"):
                self.assertIn(flag, diff)
            self.assertEqual((output / "issue.md").stat().st_mode & 0o044, 0o044)

    def test_half_created_container_is_removed_when_create_times_out(self):
        with tempfile.TemporaryDirectory() as directory:
            docker = RecordingDocker()
            original = docker.call
            def call(*args, **kwargs):
                if args[0] == "create":
                    raise subprocess.TimeoutExpired(args, 120)
                return original(*args, **kwargs)
            docker.call = call
            row = run_one(docker, TASK, {"image_id": "sha256:frozen"}, Path(directory) / "a", 1, "eval", "2", "4g")
            self.assertEqual(row["agent_status"], "setup_error")
            self.assertEqual(docker.commands[-1][0], "rm")

    def test_bookkeeping_failure_does_not_lose_the_row(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("evals.swebench.runner.write_json", side_effect=OSError("disk full")):
                row = run_one(RecordingDocker(), TASK, {"image_id": "sha256:frozen"},
                              Path(directory) / "a", 1, "eval", "2", "4g")
            self.assertEqual(row["agent_status"], "runner_error")

    def test_network_ids_resolving_to_default_networks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            write_json(run / "builds.json", {TASK["instance_id"]: {"image_id": "sha256:frozen"}})
            class Resolver:
                def __init__(self, name): self.name = name
                def text(self, *args):
                    return self.name
            with patch.dict(os.environ, ENV):
                with patch("evals.swebench.runner.Docker", return_value=Resolver("bridge")), \
                     self.assertRaisesRegex(ValueError, "resolves to bridge"):
                    run_tasks(run, 1, "0123456789ab", "2", "4g")
                class Missing:
                    def text(self, *args):
                        raise subprocess.CalledProcessError(1, args)
                with patch("evals.swebench.runner.Docker", return_value=Missing()), \
                     self.assertRaisesRegex(ValueError, "not found"):
                    run_tasks(run, 1, "nope", "2", "4g")

    def test_rejected_networks_and_missing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            write_json(run / "builds.json", {TASK["instance_id"]: {"image_id": "sha256:frozen"}})
            with patch.dict(os.environ, ENV):
                for network in ("host", "bridge", "default", "container:abc"):
                    with self.subTest(network=network), self.assertRaises(ValueError):
                        run_tasks(run, 1, network, "2", "4g")
            with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "LITELLM"):
                run_tasks(run, 1, "eval", "2", "4g")
            with patch.dict(os.environ, ENV):
                write_json(run / "builds.json", {"other": {"image_id": "x"}})
                with self.assertRaisesRegex(ValueError, "cover"):
                    run_tasks(run, 1, "eval", "2", "4g")

    def test_interrupt_marks_remaining_tasks_not_started_and_exit_130(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            write_settings(base / "repo")
            tasks = [dict(TASK, instance_id=f"owner__repo-{i}") for i in (1, 2, 3)]
            run = base / "run-1"
            prepare_run(base / "repo", run, tasks, [t["instance_id"] for t in tasks], VERSIONS, "node:22")
            write_json(run / "builds.json", {t["instance_id"]: {"image_id": "sha256:frozen"} for t in tasks})
            with patch.dict(os.environ, ENV), patch("evals.swebench.runner.Docker", return_value=FakeDocker("interrupt")):
                self.assertEqual(run_tasks(run, 1, "eval", "2", "4g"), 130)
            rows = [json.loads(line) for line in (run / "predictions.jsonl").read_text().splitlines()]
            self.assertEqual([r["instance_id"] for r in rows], [t["instance_id"] for t in tasks])
            self.assertFalse((run / "attempts/owner__repo-2").exists())

    def test_agent_failures_are_benchmark_outcomes_not_ci_failures(self):
        for failure, code in (("agent", 0), ("timeout", 0), ("setup", 3)):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                run = prepare_fixture(Path(directory))
                write_json(run / "builds.json", {TASK["instance_id"]: {"image_id": "sha256:frozen"}})
                with patch.dict(os.environ, ENV), patch("evals.swebench.runner.Docker", return_value=FakeDocker(failure)):
                    self.assertEqual(run_tasks(run, 1, "eval", "2", "4g"), code)

    def test_run_exception_in_run_one_still_writes_a_prediction(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            write_json(run / "builds.json", {TASK["instance_id"]: {"image_id": "sha256:frozen"}})
            with patch.dict(os.environ, ENV), patch("evals.swebench.runner.run_one", side_effect=OSError("boom")), \
                 patch("evals.swebench.runner.Docker", return_value=FakeDocker()):
                self.assertEqual(run_tasks(run, 1, "eval", "2", "4g"), 3)
            self.assertEqual(len((run / "predictions.jsonl").read_text().splitlines()), 1)


class ProfileHardeningTests(unittest.TestCase):
    def test_declares_mcp_tokens_keys_and_malformed_values(self):
        from evals.swebench.prepare import declares_mcp
        self.assertTrue(declares_mcp({"packages": ["npm:pi-mcp-adapter@1.0.0"]}))
        self.assertTrue(declares_mcp({"mcpServers": {}}))
        self.assertTrue(declares_mcp({"litellm": {"mcp": {"enabled": True}}}))
        self.assertFalse(declares_mcp({"packages": ["npm:pi-subagents"], "litellm": "weird"}))
        self.assertFalse(declares_mcp({"packages": "not-a-list"}))

    def test_dropped_package_specs_do_not_carry_url_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            write_settings(base / "repo", packages=["git:https://x-access-token:" + SENTINEL + "@github.com/o/r"])
            manifest = prepare_run(base / "repo", base / "run-1", [TASK], [TASK["instance_id"]], VERSIONS, "node:22")
            self.assertNotIn(SENTINEL, (base / "run-1/manifest.json").read_text())
            self.assertIn("<redacted>@github.com", json.dumps(manifest))

    def test_task_fields_used_by_build_and_run_are_tamper_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            for field, value in (("image", "attacker/image"), ("base_commit", "f" * 40)):
                manifest = read_json(run / "manifest.json")
                manifest["tasks"][0][field] = value
                write_json(run / "manifest.json", manifest)
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "do not match"):
                    read_manifest(run)
                manifest["tasks"][0][field] = TASK[field]
                write_json(run / "manifest.json", manifest)

    def test_git_output_is_stable_under_hostile_agent_git_config(self):
        from evals.swebench.runner import export_patch
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            def git(*args):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)
            git("init", "--quiet")
            git("config", "user.name", "T"); git("config", "user.email", "t@example.invalid")
            (cwd / "f.py").write_text("a = 1\n")
            git("add", "-A"); git("commit", "--quiet", "-m", "base")
            baseline = git("rev-parse", "HEAD").stdout.decode().strip()
            (cwd / "f.py").write_text("a = 2\n")
            for key, value in (("color.ui", "always"), ("diff.noprefix", "true"),
                               ("diff.external", "/bin/false")):
                git("config", key, value)
            class LocalGitDocker:
                def call(self, *args, **kwargs):
                    return git(*args[args.index("git") + 1:])
            patch_bytes = export_patch(LocalGitDocker(), "unused", baseline)
            self.assertIn(b"diff --git a/f.py b/f.py", patch_bytes)
            self.assertNotIn(b"\x1b[", patch_bytes)


class BuildTests(unittest.TestCase):
    def test_build_images_records_tags_args_and_refuses_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            calls = []
            class Docker:
                def call(self, *args, **kwargs):
                    calls.append(args)
                def text(self, *args):
                    return "sha256:image"
            with patch("evals.swebench.runner.Docker", return_value=Docker()):
                build_images(run, "linux/amd64")
                builds = read_json(run / "builds.json")
                self.assertTrue(builds[TASK["instance_id"]]["image"].startswith("pepi-eval:pepi-"))
                self.assertIn("TASK_IMAGE=" + TASK["image"], calls[0])
                self.assertIn("NODE_IMAGE=node:22-bookworm-slim", calls[0])
                self.assertTrue((run / "builds.partial.json").exists())
                with self.assertRaisesRegex(ValueError, "already exist"):
                    build_images(run, "linux/amd64")

    def test_failed_build_names_the_log(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            class Docker:
                def call(self, *args, **kwargs):
                    raise subprocess.CalledProcessError(1, args)
            with patch("evals.swebench.runner.Docker", return_value=Docker()):
                with self.assertRaisesRegex(RuntimeError, "build-owner__repo-1.log"):
                    build_images(run, "linux/amd64")


class CliTests(unittest.TestCase):
    def test_parser_wiring_and_validation(self):
        parser = cli.build_parser()
        args = parser.parse_args(["run", "r", "--timeout-seconds", "5", "--network", "n",
                                  "--secrets-dir", "s"])
        self.assertEqual(args.secrets_dir, Path("s"))
        for bad in (["run", "r", "--timeout-seconds", "0", "--network", "n"],
                    ["run", "r", "--timeout-seconds", "5", "--network", "n", "--cpus", "nan"]):
            with self.subTest(bad=bad), self.assertRaises((SystemExit, ValueError)):
                parser.parse_args(bad)

    def test_exit_codes_and_messages(self):
        base = ["build", "/nonexistent-run"]
        self.assertEqual(cli.main(base), 2)  # missing manifest -> OSError -> usage/config
        for exception, code in ((subprocess.CalledProcessError(1, "docker"), 3), (RuntimeError("x"), 3),
                                (KeyboardInterrupt(), 130)):
            with self.subTest(exception=exception), patch.object(cli, "build_images", side_effect=exception):
                self.assertEqual(cli.main(base), code)

    def test_main_installs_sigterm_handler_that_unwinds(self):
        import signal
        previous = signal.getsignal(signal.SIGTERM)
        try:
            with patch.object(cli, "build_images", side_effect=lambda *a: signal.raise_signal(signal.SIGTERM)):
                self.assertEqual(cli.main(["build", "/x"]), 130)
        finally:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    unittest.main()
