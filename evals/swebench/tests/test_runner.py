import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.swebench.common import DATASET, identifier, positive, read_manifest, version
from evals.swebench.prepare import prepare_run, sanitize_tasks, selected_ids
from evals.swebench.runner import (CONTROLLER_ENV, HISTORY_PRUNE_SCRIPT, PGREP, PI_COMMAND, PI_NODE, PKILL, export_patch,
                                   run_one, run_tasks)

TASK = {"instance_id": "owner__repo-1", "repo": "owner/repo", "base_commit": "a" * 40,
        "image": "swebench/example@sha256:" + "b" * 64, "problem_statement": "Fix a bug.\n",
        "datasets": [DATASET], "split": "test", "patch": "SECRET GOLD",
        "test_patch": "SECRET TEST", "FAIL_TO_PASS": ["hidden_test"]}
VERSIONS = {"pi": "1.1.0", "pi-provider-litellm": "3.0.1", "pi-subagents": "0.68.0"}


def prepare_fixture(root, tasks=None):
    pepi = root / "pepi"
    pepi.mkdir()
    (pepi / "agents").mkdir()
    (pepi / "agents/deep-worker.md").write_text("---\nname: deep-worker\n---\nImplement the assigned task.\n")
    (pepi / "AGENTS.md").write_text("Delegate; verify; report honestly.\n")
    (pepi / "settings.json").write_text(json.dumps({
        "defaultProvider": "litellm", "defaultModel": "model-a",
        "subagents": {"agentOverrides": {"worker": {"model": "model-b"},
                                         "reviewer": {"skills": ["personal"]}}},
        "packages": ["npm:pi-memory-mem0"], "pi-memory-mem0": {"userId": "private"},
    }))
    (pepi / "auth.json").write_text("DO NOT COPY")
    run = root / "run-1"
    tasks = [TASK] if tasks is None else tasks
    prepare_run(pepi, run, tasks, [task["instance_id"] for task in tasks], VERSIONS, "node:22-bookworm-slim")
    return run


class FakeDocker:
    def __init__(self, failure=None):
        self.commands = []
        self.failure = failure

    def call(self, *args, **kwargs):
        self.commands.append(args)
        if "bash" in args and any(a.startswith(PI_COMMAND) for a in args):
            if self.failure == "timeout":
                raise subprocess.TimeoutExpired(args, 1)
            if self.failure == "agent":
                raise subprocess.CalledProcessError(2, args)
            if self.failure == "interrupt":
                raise KeyboardInterrupt()
        if args[0] == "create" and self.failure == "setup":
            raise subprocess.CalledProcessError(125, args)
        if PGREP in args:
            raise subprocess.CalledProcessError(1, args)
        if self.failure == "dirty" and "status" in args:
            return SimpleNamespace(stdout=b"?? dirty.py\0", returncode=0)
        if HISTORY_PRUNE_SCRIPT in args:
            return SimpleNamespace(stdout=b"pepi-history refs_removed=0 remotes_removed=0\npepi-history gc_done\n",
                                   returncode=0)
        output = b"diff --git a/a b/a\nnew file mode 100644\n" if "--cached" in args else b""
        return SimpleNamespace(stdout=output, returncode=0)

    def text(self, *args):
        self.commands.append(args)
        if "rev-parse" in args:
            return "c" * 40
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
            self.assertEqual(profile["subagents"]["agentOverrides"]["worker"]["model"], "litellm/model-b")
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


def assert_controller_execs_trust_no_task_path(case, commands):
    """Every `docker exec` but the Pi launch pins CONTROLLER_ENV; root ones also name the binary by path."""
    execs = [command for command in commands if command[0] == "exec"]
    case.assertTrue(execs)
    for command in execs:
        container = next(index for index, arg in enumerate(command) if arg.startswith("pepi-eval-"))
        if any(arg.startswith(PI_COMMAND) for arg in command):
            case.assertNotIn(CONTROLLER_ENV[1], command, "the agent keeps the task image's PATH")
            continue
        case.assertEqual(command[container - 2:container], CONTROLLER_ENV, command)
        if "--user" not in command[:container]:
            case.assertTrue(command[container + 1].startswith("/"), command)


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
        kill = next(i for i, command in enumerate(docker.commands) if PKILL in command)
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
        assert_controller_execs_trust_no_task_path(self, docker.commands)
        root = [command for command in docker.commands if command[0] == "exec" and "--user" not in command]
        self.assertEqual({command[4] for command in root}, {PKILL, PGREP})

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
                self.assertEqual(run_tasks(run, 1, "eval", "2", "4g"), 3)
            rows = [json.loads(line) for line in (run / "predictions.jsonl").read_text().split("\n") if line]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["model_patch"], "")
            with self.assertRaises(ValueError):
                run_tasks(run, 1, "eval", "2", "4g")


class RunTasksProgressTests(unittest.TestCase):
    @patch.dict("os.environ", {"LITELLM_BASE_URL": "https://example.invalid",
                              "LITELLM_API_KEY": "private-credential-sentinel"})
    def run_progress(self, outcomes, audit_failed=False):
        with tempfile.TemporaryDirectory(prefix="private-path-sentinel") as directory:
            tasks = [dict(TASK, instance_id=f"owner__repo-{i}",
                          problem_statement="private-description-sentinel")
                     for i in range(1, len(outcomes) + 1)]
            run = prepare_fixture(Path(directory), tasks)
            (run / "builds.json").write_text(json.dumps({
                task["instance_id"]: {"image_id": "sha256:frozen"} for task in tasks}))
            stderr = io.StringIO()
            calls = []

            def attempt(*args):
                ordinal = len(calls) + 1
                self.assertTrue(stderr.getvalue().endswith(
                    f"pepi-eval: task {ordinal}/{len(tasks)} started\n"))
                self.assertEqual(flush.call_count, 2 * ordinal - 1)
                calls.append(args)
                if audit_failed:
                    args[8].audit_failed = True
                outcome = outcomes[ordinal - 1]
                if isinstance(outcome, Exception):
                    raise outcome
                return {"instance_id": args[1]["instance_id"], "agent_status": outcome,
                        "patch": "private-patch-sentinel", "errors": ["private-error-sentinel"],
                        "timing_seconds": {"agent_wall": 0.01}}

            ticks = [tick for i in range(len(tasks)) for tick in (100 + i * 10, 104.25 + i * 10)]
            with patch("evals.swebench.runner.Docker", return_value=FakeDocker()), \
                 patch("evals.swebench.runner.run_one", side_effect=attempt) as run_one_mock, \
                 patch("evals.swebench.runner.time.monotonic", side_effect=ticks) as clock, \
                 patch("evals.swebench.runner.sys.stderr", stderr), \
                 patch.object(stderr, "flush", wraps=stderr.flush) as flush:
                code = run_tasks(run, 1, "eval", "2", "4g")
                self.assertEqual(flush.call_count, 2 * run_one_mock.call_count)
                self.assertEqual(clock.call_count, 2 * run_one_mock.call_count)
            predictions = [json.loads(line) for line in (run / "predictions.jsonl").read_text().splitlines()]
            self.assertEqual([row["instance_id"] for row in predictions],
                             [task["instance_id"] for task in tasks])
            return code, stderr.getvalue(), run_one_mock.call_count

    def test_progress_format_status_elapsed_and_safe_metadata(self):
        statuses = ["completed", "timeout", "agent_error", "setup_error", "runner_error", "interrupted"]
        code, output, count = self.run_progress(statuses)
        self.assertEqual(code, 130)
        self.assertEqual(count, len(statuses))
        expected = "".join(
            f"pepi-eval: task {i}/{len(statuses)} started\n"
            f"pepi-eval: task {i}/{len(statuses)} completed agent_status={status} elapsed_seconds=4.2\n"
            for i, status in enumerate(statuses, 1))
        self.assertEqual(output, expected)
        self.assertNotIn("private-", output)
        self.assertNotIn("owner__repo", output)

    def test_progress_normalizes_runner_exception_without_printing_it(self):
        code, output, count = self.run_progress([OSError("private-exception-sentinel")])
        self.assertEqual((code, count), (3, 1))
        self.assertEqual(output, "pepi-eval: task 1/1 started\n"
                         "pepi-eval: task 1/1 completed agent_status=runner_error elapsed_seconds=4.2\n")

    @patch.dict("os.environ", {"LITELLM_BASE_URL": "https://example.invalid",
                              "LITELLM_API_KEY": "private-credential-sentinel"})
    def test_progress_io_failures_preserve_execution_predictions_cleanup_and_exit_code(self):
        class FailingStderr(io.StringIO):
            phase = ""
            failures = 0

            def write(self, text):
                if text.startswith("pepi-eval: task "):
                    self.phase = "started" if text.endswith("started") else "completed"
                if operation == "write" and self.phase == phase:
                    self.failures += 1
                    raise BrokenPipeError("private-stderr-error")
                return super().write(text)

            def flush(self):
                if operation == "flush" and self.phase == phase:
                    self.failures += 1
                    raise OSError("private-stderr-error")
                return super().flush()

        for operation in ("write", "flush"):
            for phase in ("started", "completed"):
                for failure, status, code in ((None, "completed", 0), ("setup", "setup_error", 3),
                                              ("interrupt", "interrupted", 130)):
                    with self.subTest(operation=operation, phase=phase, failure=failure), \
                         tempfile.TemporaryDirectory() as directory:
                        tasks = [dict(TASK, instance_id=f"owner__repo-{i}") for i in (1, 2)]
                        run = prepare_fixture(Path(directory), tasks)
                        (run / "builds.json").write_text(json.dumps({
                            task["instance_id"]: {"image_id": "sha256:frozen"} for task in tasks}))
                        docker, stderr = FakeDocker(failure), FailingStderr()
                        with patch("evals.swebench.runner.Docker", return_value=docker), \
                             patch("evals.swebench.runner.sys.stderr", stderr):
                            self.assertEqual(run_tasks(run, 1, "eval", "2", "4g"), code)
                        attempts = 1 if failure == "interrupt" else 2
                        self.assertEqual(stderr.failures, attempts)
                        for action in ("create", "rm"):
                            self.assertEqual(sum(cmd[0] == action for cmd in docker.commands), attempts)
                        rows = [json.loads(line) for line in (run / "predictions.jsonl").read_text().splitlines()]
                        self.assertEqual([row["instance_id"] for row in rows],
                                         [task["instance_id"] for task in tasks])
                        expected_patch = b"diff --git a/a b/a\nnew file mode 100644\n" if failure is None else b""
                        for task, row in zip(tasks[:attempts], rows[:attempts]):
                            output = run / "attempts" / task["instance_id"]
                            self.assertEqual(json.loads((output / "attempt.json").read_text())["agent_status"], status)
                            self.assertEqual((output / "patch.diff").read_bytes(), expected_patch)
                            self.assertEqual(row["model_patch"], expected_patch.decode())
                        if failure == "interrupt":
                            self.assertEqual(rows[1]["model_patch"], "")
                            self.assertFalse((run / "attempts" / tasks[1]["instance_id"]).exists())

    def test_progress_does_not_claim_interrupt_or_audit_skipped_tasks_started(self):
        for status, audit_failed, code in (("interrupted", False, 130), ("runner_error", True, 3)):
            with self.subTest(status=status):
                actual_code, output, count = self.run_progress(
                    [status, "completed", "completed"], audit_failed=audit_failed)
                self.assertEqual((actual_code, count), (code, 1))
                self.assertEqual(output, "pepi-eval: task 1/3 started\n"
                                 f"pepi-eval: task 1/3 completed agent_status={status} elapsed_seconds=4.2\n")


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
            self.assertIn(b"+new = 2", result)
            self.assertIn(b"new file.py", result)
            self.assertIn(b"+added = True", result)
            self.assertTrue(result.endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()
