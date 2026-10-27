"""Real-Git tests for HISTORY_PRUNE_SCRIPT: no ref, reflog or object after the task base survives (no Docker)."""
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.swebench import __main__ as cli
from evals.swebench.common import read_json, write_json
from evals.swebench.runner import (HISTORY_PRUNE_CLEANUP, HISTORY_PRUNE_POLICY, HISTORY_PRUNE_SCRIPT,
                                   HISTORY_PRUNE_TIMEOUT, PI_COMMAND, PKILL, prune_history, run_one, run_tasks)
from evals.swebench.tests.test_ci import ENV
from evals.swebench.tests.test_runner import FakeDocker, TASK, prepare_fixture


class ShellDocker(FakeDocker):
    """`docker exec ... sh -c SCRIPT` runs SCRIPT in the local repository; everything else is FakeDocker."""

    def __init__(self, repo, env, prune=None):
        super().__init__()
        self.repo, self.env, self.prune = repo, env, prune

    def call(self, *args, **kwargs):
        if "sh" in args and args[-2] == "-c":
            self.commands.append(args)
            if args[-1] == HISTORY_PRUNE_SCRIPT and self.prune:
                return self.prune(args, kwargs)
            return subprocess.run(["sh", "-c", args[-1]], cwd=self.repo, env=self.env, capture_output=True,
                                  check=True, timeout=kwargs.get("timeout"))
        if "git" in args:
            self.commands.append(args)
            return subprocess.run(["git", *args[args.index("git") + 1:]], cwd=self.repo, env=self.env,
                                  capture_output=True, check=True)
        return super().call(*args, **kwargs)

    def text(self, *args):
        return self.call(*args, capture_output=True).stdout.decode().strip()


class HistoryPruneScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        self.git("init", "--quiet", "--initial-branch=main")
        (self.repo / "lib.py").write_text("old = 1\n")
        self.past = self.commit("past")
        (self.repo / "lib.py").write_text("base = 1\n")
        self.base = self.commit("task base")
        # Later upstream history, as a task image keeps it: the reference fix and beyond.
        self.git("checkout", "--quiet", "-b", "upstream")
        (self.repo / "lib.py").write_text("fixed = 1\n")
        (self.repo / "fix_only.txt").write_text("reference fix blob\n")
        self.fix = self.commit("reference fix")
        (self.repo / "lib.py").write_text("later = 1\n")
        self.later = self.commit("later")
        self.future = [self.fix, self.later]
        self.fix_blob = self.git("rev-parse", f"{self.fix}:fix_only.txt")
        self.git("tag", "v-light", self.fix)
        self.git("tag", "-a", "-m", "annotated", "v-annotated", self.later)
        self.git("tag", "blob-tag", self.fix_blob)  # a non-commit tag: no ^{commit}, so it goes
        # Release tags on the base and its ancestors stay: `git describe` (versioneer, setuptools_scm) keeps working.
        self.git("tag", "v-past", self.past)
        self.git("tag", "-a", "-m", "release", "v-base", self.base)
        self.kept_tags = ["refs/tags/v-base", "refs/tags/v-past"]
        self.git("remote", "add", "origin", "https://example.invalid/repo.git")
        self.git("update-ref", "refs/remotes/origin/main", self.later)
        self.git("update-ref", "refs/original/refs/heads/main", self.later)
        self.git("notes", "add", "-m", "note", self.later)
        self.git("checkout", "--quiet", "main")
        (self.repo / "lib.py").write_text("stashed = 1\n")
        self.git("stash", "--quiet")
        git_dir = self.repo / ".git"
        (git_dir / "FETCH_HEAD").write_text(f"{self.later}\t\tbranch 'main' of example\n")
        (git_dir / "ORIG_HEAD").write_text(self.later + "\n")
        self.git("pack-refs", "--all")
        self.git("repack", "-a", "-d", "--quiet")
        for pack in (git_dir / "objects/pack").glob("*.pack"):
            pack.with_suffix(".keep").write_text("")  # `git repack -a` would otherwise keep these objects
        self.git("commit-graph", "write", "--reachable")

    def git(self, *args, check=True):
        result = subprocess.run(["git", *args], cwd=self.repo, env=self.env, capture_output=True, check=check)
        return result.stdout.decode().strip() if check else result

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def prune(self):
        return subprocess.run(["sh", "-c", HISTORY_PRUNE_SCRIPT], cwd=self.repo, env=self.env,
                              capture_output=True, check=True).stdout.decode()

    def assert_only_head_history(self, head):
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        for sha in (*self.future, self.fix_blob):
            self.assertNotEqual(self.git("cat-file", "-e", sha, check=False).returncode, 0, sha)
        self.assertEqual(self.git("rev-list", "--all", "--reflog"), self.git("rev-list", "HEAD"))
        self.assertEqual(self.git("rev-list", "HEAD").split(), [self.base, self.past])
        self.assertEqual(self.git("fsck", "--unreachable", "--no-reflogs", "--no-progress"), "")
        self.assertEqual(self.git("remote"), "")
        self.assertEqual(self.git("status", "--porcelain=v1", "--untracked-files=all"), "")
        self.assertEqual((self.repo / "lib.py").read_text(), "base = 1\n")
        for name in ("FETCH_HEAD", "ORIG_HEAD"):
            self.assertFalse((self.repo / ".git" / name).exists())

    def test_only_the_checked_out_branch_its_ancestors_and_their_tags_survive(self):
        refs_before = len(self.git("for-each-ref", "--format=%(refname)").split())
        describe = self.git("describe", "--tags")
        output = self.prune()
        self.assertIn(f"pepi-history refs_removed={refs_before - 3} remotes_removed=1\n", output)
        self.assertTrue(output.endswith("pepi-history gc_done\n"))
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)").split(), ["refs/heads/main", *self.kept_tags])
        self.assertEqual(self.git("describe", "--tags"), describe)
        self.assertEqual(self.git("describe", "--tags", self.past), "v-past")
        self.assert_only_head_history(self.base)

    def test_detached_head_keeps_only_tags_on_its_history(self):
        self.git("checkout", "--quiet", "--detach", self.base)
        refs_before = len(self.git("for-each-ref", "--format=%(refname)").split())
        self.assertIn(f"refs_removed={refs_before - 2} ", self.prune())
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)").split(), self.kept_tags)
        self.assert_only_head_history(self.base)

    def test_a_tag_after_the_base_is_removed_even_when_it_names_an_old_release(self):
        # Ancestry, not tag names or dates, decides: a later commit tagged with an older-looking name goes.
        self.git("tag", "v0.0.1", self.fix)
        self.prune()
        self.assertNotIn("v0.0.1", self.git("tag"))
        self.assertEqual(self.git("tag").split(), ["v-base", "v-past"])
        self.assert_only_head_history(self.base)

    def test_exported_patch_is_unchanged_by_pruning(self):
        untouched = self.root / "untouched"
        shutil.copytree(self.repo, untouched, symlinks=True)
        self.prune()

        def export(repo):
            (repo / "lib.py").write_text("agent = 2\n")
            (repo / "new.bin").write_bytes(b"\x00\xff")
            run = lambda *args: subprocess.run(["git", *args], cwd=repo, env=self.env, capture_output=True,
                                               check=True).stdout
            run("add", "-A")
            return run("diff", "--cached", "--binary", "--no-color", "--src-prefix=a/", "--dst-prefix=b/",
                       self.base, "--")

        self.assertEqual(export(self.repo), export(untouched))

    def test_alternates_and_linked_worktrees_are_refused(self):
        alternates = self.repo / ".git/objects/info/alternates"
        alternates.write_text("/elsewhere/objects\n")
        result = subprocess.run(["sh", "-c", HISTORY_PRUNE_SCRIPT], cwd=self.repo, env=self.env, capture_output=True)
        self.assertEqual((result.returncode, result.stdout), (3, b""))
        alternates.unlink()
        self.git("worktree", "add", "--quiet", str(self.root / "linked"), self.later)
        result = subprocess.run(["sh", "-c", HISTORY_PRUNE_SCRIPT], cwd=self.repo, env=self.env, capture_output=True)
        self.assertEqual((result.returncode, result.stdout), (3, b""))
        self.assertIn(b"linked worktrees", result.stderr)

    def test_cleanup_removes_stale_git_locks_after_a_killed_gc(self):
        git_dir = self.repo / ".git"
        for name in ("index.lock", "gc.pid", "packed-refs.lock", "refs/heads/main.lock"):
            (git_dir / name).write_text("")
        subprocess.run(["sh", "-c", HISTORY_PRUNE_CLEANUP], cwd=self.repo, env=self.env, check=True)
        for name in ("index.lock", "gc.pid", "packed-refs.lock", "refs/heads/main.lock"):
            self.assertFalse((git_dir / name).exists(), name)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)


class PruneHistoryOutcomeTests(unittest.TestCase):
    MARKER = b"pepi-history refs_removed=3 remotes_removed=1\n"

    def outcome(self, prune, timeout=120):
        docker = ShellDocker(Path("."), dict(os.environ), prune)
        return prune_history(docker, "container", timeout), docker

    def test_timeout_kills_gc_cleans_locks_and_records_refs_only(self):
        def prune(args, kwargs):
            raise subprocess.TimeoutExpired(args, kwargs["timeout"], output=self.MARKER)

        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "--quiet", directory], check=True)
            (Path(directory) / ".git/index.lock").write_text("")
            docker = ShellDocker(Path(directory), dict(os.environ), prune)
            result = prune_history(docker, "container", 5)
            self.assertFalse((Path(directory) / ".git/index.lock").exists())
        self.assertEqual({k: result[k] for k in ("status", "refs_removed", "remotes_removed", "error")},
                         {"status": "refs_only", "refs_removed": 3, "remotes_removed": 1,
                          "error": "timed out after 5s"})
        killed = [index for index, command in enumerate(docker.commands) if PKILL in command]
        cleanup = [index for index, command in enumerate(docker.commands) if HISTORY_PRUNE_CLEANUP in command]
        self.assertTrue(killed and cleanup and killed[0] < cleanup[0])

    def test_script_failure_and_unexpected_output(self):
        def failing(args, kwargs):
            raise subprocess.CalledProcessError(3, args, output=b"")

        result, _ = self.outcome(failing)
        self.assertEqual((result["status"], result["refs_removed"], result["error"]), ("failed", None, "exit code 3"))
        result, _ = self.outcome(lambda args, kwargs: SimpleNamespace(stdout=b"", returncode=0))
        self.assertEqual((result["status"], result["error"]), ("failed", "unexpected output"))
        result, _ = self.outcome(lambda args, kwargs: SimpleNamespace(stdout=self.MARKER, returncode=0))
        self.assertEqual((result["status"], result["refs_removed"]), ("refs_only", 3))

    def test_zero_timeout_skips_without_a_container_command(self):
        result, docker = self.outcome(None, timeout=0)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(docker.commands, [])


class RunOneHistoryTests(unittest.TestCase):
    def attempt(self, prune=None, timeout=120):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
            git = lambda *args: subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True,
                                               check=True).stdout.decode().strip()
            git("init", "--quiet")
            (repo / "a.py").write_text("a = 1\n")
            git("add", "-A")
            git("commit", "--quiet", "-m", "base")
            base = git("rev-parse", "HEAD")
            git("tag", "base-tag")
            git("checkout", "--quiet", "-b", "upstream")
            (repo / "a.py").write_text("a = 2\n")
            git("commit", "--quiet", "-am", "later fix")
            git("tag", "future-tag")
            git("checkout", "--quiet", "-")
            docker = ShellDocker(repo, env, prune)
            row = run_one(docker, dict(TASK, base_commit=base), {"image_id": "unused"}, root / "attempt", 1,
                          "eval", "2", "4g", history_prune_timeout=timeout)
            return row, docker, git("for-each-ref")

    def test_history_is_pruned_after_the_baseline_check_and_before_the_agent(self):
        row, docker, refs = self.attempt()
        self.assertEqual(row["agent_status"], "completed")
        self.assertEqual(row["history_pruning"]["status"], "pruned")
        self.assertEqual(row["history_pruning"]["refs_removed"], 2)  # the upstream branch and future-tag
        self.assertEqual(row["errors"], [])
        self.assertNotIn("future-tag", refs)
        self.assertNotIn("upstream", refs)
        self.assertIn("refs/tags/base-tag", refs)
        order = [index for index, command in enumerate(docker.commands)
                 if HISTORY_PRUNE_SCRIPT in command or any(PI_COMMAND in part for part in command)
                 or "status" in command]
        kinds = ["status" if "status" in docker.commands[index] else
                 "prune" if HISTORY_PRUNE_SCRIPT in docker.commands[index] else "agent" for index in order]
        self.assertEqual(kinds, ["status", "prune", "agent"])

    def test_a_failed_prune_is_recorded_and_the_attempt_still_runs(self):
        def failing(args, kwargs):
            raise subprocess.CalledProcessError(3, args, output=b"")

        row, _, refs = self.attempt(failing)
        self.assertEqual(row["agent_status"], "completed")
        self.assertEqual(row["history_pruning"]["status"], "failed")
        self.assertEqual(row["errors"], ["history_not_pruned: exit code 3"])
        self.assertIn("future-tag", refs)

    def test_disabled_pruning_is_recorded_as_skipped(self):
        row, docker, refs = self.attempt(timeout=0)
        self.assertEqual((row["history_pruning"]["status"], row["errors"]), ("skipped", []))
        self.assertIn("future-tag", refs)
        self.assertFalse(any(HISTORY_PRUNE_SCRIPT in command for command in docker.commands))


class HistoryPruneConfigurationTests(unittest.TestCase):
    def test_execution_records_the_policy_and_timeout(self):
        for timeout, expected in ((HISTORY_PRUNE_TIMEOUT, {"policy": HISTORY_PRUNE_POLICY, "timeout_seconds": 120}),
                                  (0, {"policy": "disabled"})):
            with self.subTest(timeout=timeout), tempfile.TemporaryDirectory() as directory:
                run = prepare_fixture(Path(directory))
                write_json(run / "builds.json", {TASK["instance_id"]: {"image_id": "sha256:frozen"}})
                with patch.dict(os.environ, ENV), patch("evals.swebench.runner.Docker", return_value=FakeDocker()):
                    self.assertEqual(run_tasks(run, 1, "eval", "2", "4g", history_prune_timeout=timeout), 0)
                self.assertEqual(read_json(run / "execution.json")["history_pruning"], expected)
                attempt = read_json(run / "attempts" / TASK["instance_id"] / "attempt.json")
                self.assertEqual(attempt["history_pruning"]["status"], "pruned" if timeout else "skipped")

    def test_cli_default_override_and_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            for extra, expected in (([], HISTORY_PRUNE_TIMEOUT), (["--history-prune-timeout", "0"], 0),
                                    (["--history-prune-timeout", "30"], 30)):
                with patch.object(cli, "run_tasks", return_value=0) as call:
                    self.assertEqual(cli.main(["run", directory, "--network", "eval", *extra]), 0)
                self.assertEqual(call.call_args.kwargs["history_prune_timeout"], expected)
            with patch.object(cli, "run_tasks", return_value=0) as call, self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                cli.main(["run", directory, "--network", "eval", "--history-prune-timeout", "-1"])
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
