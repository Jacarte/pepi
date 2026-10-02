import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.swebench.common import DATASET, identifier, positive, read_manifest, version
from evals.swebench.prepare import prepare_run, sanitize_tasks, selected_ids
from evals.swebench.runner import PI_COMMAND, export_patch, run_one, run_tasks

TASK = {"instance_id": "owner__repo-1", "repo": "owner/repo", "base_commit": "a" * 40,
        "image": "swebench/example@sha256:" + "b" * 64, "problem_statement": "Fix a bug.\n",
        "datasets": [DATASET], "split": "test", "patch": "SECRET GOLD",
        "test_patch": "SECRET TEST", "FAIL_TO_PASS": ["hidden_test"]}
VERSIONS = {"pi": "0.87.1", "pi-provider-litellm": "3.0.1", "pi-subagents": "0.68.0"}


def prepare_fixture(root):
    pepi = root / "pepi"
    pepi.mkdir()
    (pepi / "AGENTS.md").write_text("Delegate; verify; report honestly.\n")
    (pepi / "settings.json").write_text(json.dumps({
        "defaultProvider": "litellm", "defaultModel": "model-a",
        "subagents": {"agentOverrides": {"worker": {"model": "model-b"},
                                         "reviewer": {"skills": ["personal"]}}},
        "packages": ["npm:pi-memory-mem0"], "pi-memory-mem0": {"userId": "private"},
    }))
    (pepi / "auth.json").write_text("DO NOT COPY")
    run = root / "run-1"
    prepare_run(pepi, run, [TASK], [TASK["instance_id"]], VERSIONS, "node:22-bookworm-slim")
    return run


class FakeDocker:
    def __init__(self, failure=None):
        self.commands = []
        self.failure = failure

    def call(self, *args, **kwargs):
        self.commands.append(args)
        if "bash" in args and PI_COMMAND in args:
            if self.failure == "timeout":
                raise subprocess.TimeoutExpired(args, 1)
            if self.failure == "agent":
                raise subprocess.CalledProcessError(2, args)
            if self.failure == "interrupt":
                raise KeyboardInterrupt()
        if args[0] == "create" and self.failure == "setup":
            raise subprocess.CalledProcessError(125, args)
        if "pgrep" in args:
            raise subprocess.CalledProcessError(1, args)
        output = b"diff --git a/a b/a\nnew file mode 100644\n" if "--cached" in args else b""
        return SimpleNamespace(stdout=output, returncode=0)

    def text(self, *args):
        self.commands.append(args)
        if "rev-parse" in args:
            return "c" * 40
        if self.failure == "dirty" and "status" in args:
            return "?? dirty.py"
        return ""


class PreparationTests(unittest.TestCase):
    def test_task_allowlist_and_order(self):
        rows = sanitize_tasks([TASK], [TASK["instance_id"]])
        self.assertNotIn("SECRET", json.dumps(rows))
        self.assertNotIn("FAIL_TO_PASS", rows[0])
        self.assertEqual(rows[0]["problem_statement"], TASK["problem_statement"])

    def test_unknown_wrong_dataset_and_duplicate_tasks(self):
        for tasks, ids in [([TASK], ["missing-1"]), ([TASK, TASK], [TASK["instance_id"]]),
                           ([dict(TASK, datasets=[])], [TASK["instance_id"]])]:
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                sanitize_tasks(tasks, ids)

    def test_selection_rejects_duplicate_or_unsafe_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ids"
            for contents in ("", "a\na\n", "../../outside\n"):
                path.write_text(contents)
                with self.assertRaises(ValueError):
                    selected_ids(path)
            path.write_text("# comment\nowner__repo-1\n")
            self.assertEqual(selected_ids(path), ["owner__repo-1"])

    def test_profile_is_isolated_and_tamper_evident(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            manifest = read_manifest(run)
            profile = json.loads((run / "build/profile/settings.json").read_text())
            self.assertEqual(profile["subagents"]["agentOverrides"]["worker"]["model"], "model-b")
            self.assertNotIn("skills", profile["subagents"]["agentOverrides"]["reviewer"])
            self.assertFalse((run / "build/profile/auth.json").exists())
            self.assertNotIn("SECRET", json.dumps(manifest))
            self.assertFalse(profile["litellm"]["mcp"]["enabled"])
            (run / "build/profile/AGENTS.md").write_text("changed")
            with self.assertRaises(ValueError):
                read_manifest(run)

    def test_invalid_base_commit_is_rejected(self):
        with self.assertRaises(ValueError):
            sanitize_tasks([dict(TASK, base_commit="--output=/tmp/bad")], [TASK["instance_id"]])

    def test_package_versions_and_limits(self):
        for value in ("latest", "^1.0.0", "1", "1.0.0 && bad"):
            with self.assertRaises(ValueError):
                version(value)
        for value in ("nan", "inf", "0", "-1"):
            with self.assertRaises(ValueError):
                positive(value)
        self.assertEqual(version("1.0.0-rc.1"), "1.0.0-rc.1")
        self.assertEqual(identifier("sympy__sympy-20590"), "sympy__sympy-20590")


class RunnerTests(unittest.TestCase):
    def run_fake(self, failure=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "attempt"
        docker = FakeDocker(failure)
        row = run_one(docker, TASK, {"image_id": "sha256:frozen"}, output, 1, "eval", "2", "4g")
        return docker, row, output

    def test_success_stops_children_before_staging_and_includes_new_files(self):
        docker, row, output = self.run_fake()
        self.assertEqual(row["agent_status"], "completed")
        self.assertTrue(row["patch"].endswith("\n"))
        kill = next(i for i, command in enumerate(docker.commands) if "pkill" in command)
        stage = next(i for i, command in enumerate(docker.commands) if "add" in command)
        self.assertLess(kill, stage)
        self.assertIn("-A", docker.commands[stage])
        diff = next(command for command in docker.commands if "--cached" in command)
        self.assertIn("c" * 40, diff)
        self.assertIn("--user", diff)
        self.assertEqual(docker.commands[-1][0], "rm")
        self.assertTrue((output / "attempt.json").exists())
        launch = next(command for command in docker.commands if PI_COMMAND in command)
        self.assertIn("LITELLM_API_KEY", launch)
        self.assertNotIn("-v", docker.commands[0])

    def test_timeout_keeps_partial_patch_and_cleans_up(self):
        docker, row, _ = self.run_fake("timeout")
        self.assertEqual(row["agent_status"], "timeout")
        self.assertTrue(row["patch"])
        self.assertEqual(docker.commands[-1][0], "rm")
        self.assertIn("agent_wall", row["timing_seconds"])

    def test_agent_error_still_extracts_patch(self):
        _, row, _ = self.run_fake("agent")
        self.assertEqual(row["agent_status"], "agent_error")
        self.assertTrue(row["patch"])

    def test_setup_error_empty_patch(self):
        docker, row, _ = self.run_fake("setup")
        self.assertEqual(row["agent_status"], "setup_error")
        self.assertEqual(row["patch"], "")
        self.assertFalse(any("bash" in command for command in docker.commands))

    def test_dirty_checkout_does_not_launch_agent(self):
        docker, row, _ = self.run_fake("dirty")
        self.assertEqual(row["agent_status"], "setup_error")
        self.assertFalse(any("bash" in command for command in docker.commands))
        self.assertEqual(docker.commands[-1][0], "rm")

    def test_interrupt_cleans_up(self):
        docker, row, _ = self.run_fake("interrupt")
        self.assertEqual(row["agent_status"], "interrupted")
        self.assertEqual(docker.commands[-1][0], "rm")

    @patch.dict("os.environ", {"LITELLM_BASE_URL": "https://example.invalid", "LITELLM_API_KEY": "test-only"})
    def test_failed_run_preserves_prediction_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            run = prepare_fixture(Path(directory))
            (run / "builds.json").write_text(json.dumps({TASK["instance_id"]: {"image_id": "sha256:frozen"}}))
            with patch("evals.swebench.runner.Docker", return_value=FakeDocker("setup")):
                self.assertEqual(run_tasks(run, 1, "eval", "2", "4g"), 1)
            rows = [json.loads(line) for line in (run / "predictions.jsonl").read_text().split("\n") if line]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["model_patch"], "")
            with self.assertRaises(ValueError):
                run_tasks(run, 1, "eval", "2", "4g")


class RealGitTests(unittest.TestCase):
    def test_export_includes_committed_work_and_untracked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            def git(*args):
                return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)
            git("init", "--quiet")
            git("config", "user.name", "Eval Test")
            git("config", "user.email", "eval@example.invalid")
            (cwd / "tracked.py").write_text("old = 1\n")
            git("add", "-A")
            git("commit", "--quiet", "-m", "baseline")
            baseline = git("rev-parse", "HEAD").stdout.decode().strip()
            (cwd / "tracked.py").write_text("new = 2\n")
            git("commit", "--quiet", "-am", "agent committed a change")
            (cwd / "new file.py").write_text("added = True\n")
            class LocalGitDocker:
                def call(self, *args, **kwargs):
                    return git(*args[args.index("git") + 1:])
            result = export_patch(LocalGitDocker(), "unused", baseline)
            self.assertIn("+new = 2", result)
            self.assertIn("new file.py", result)
            self.assertIn("+added = True", result)
            self.assertTrue(result.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
