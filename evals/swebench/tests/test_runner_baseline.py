"""Real-Git regressions for the image checkout integrity gate (no Docker or agent)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from evals.swebench.runner import PI_COMMAND, checkout_baseline, export_patch, run_one
from evals.swebench.tests.test_runner import FakeDocker, TASK


class LocalCheckoutDocker(FakeDocker):
    def __init__(self, repo, git, agent_edit=None):
        super().__init__()
        self.repo = repo
        self.git = git
        self.agent_edit = agent_edit

    def call(self, *args, **kwargs):
        if "git" in args:
            self.commands.append(args)
            return self.git(*args[args.index("git") + 1:])
        if "bash" in args and self.agent_edit:
            self.agent_edit()
        return super().call(*args, **kwargs)

    def text(self, *args):
        return self.call(*args, capture_output=True).stdout.decode().strip()


class CheckoutBaselineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Eval Test")
        self.git("config", "user.email", "eval@example.invalid")
        self.git("config", "core.fileMode", "true")
        self.names = ["normal.py", "space tab\tnewline\nfile.py", "-option.py", "binary.dat"]
        for name in self.names:
            (self.repo / name).write_bytes(b"before\x00\xff" if name == "binary.dat" else b"old = 1\n")
            (self.repo / name).chmod(0o644)
        self.base = self.commit("task base")
        self.docker = LocalCheckoutDocker(self.repo, self.git)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                              capture_output=True, check=True)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "--quiet", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.decode().strip()

    def mode_setup(self):
        for name in self.names:
            (self.repo / name).chmod(0o755)
        return self.commit("SWE-bench mode setup")

    def run_attempt(self):
        return run_one(self.docker, dict(TASK, base_commit=self.base),
                       {"image_id": "unused"}, self.root / "attempt", 1, "eval", "2", "4g")

    def assert_rejected_before_launch(self, expected):
        row = self.run_attempt()
        self.assertEqual(row["agent_status"], "setup_error")
        self.assertIn(expected, row["errors"][0])
        self.assertEqual(row["patch"], "")
        self.assertFalse(any("bash" in command for command in self.docker.commands))
        self.assertFalse(any("--cached" in command for command in self.docker.commands))

    def test_empty_setup_commit_uses_image_head(self):
        head = self.commit("empty setup")
        self.assertNotEqual(head, self.base)
        self.assertEqual(checkout_baseline(self.docker, "unused", self.base), head)
        row = self.run_attempt()
        self.assertEqual(row["agent_status"], "completed")
        self.assertEqual(row["baseline_commit"], head)
        self.assertEqual(row["patch"], "")

    def test_mode_only_setup_with_weird_paths_and_config_is_accepted(self):
        head = self.mode_setup()
        for key, value in [("core.abbrev", "4"), ("core.quotePath", "false"),
                           ("core.fileMode", "false"), ("diff.renames", "true"),
                           ("diff.external", "/does-not-exist"), ("diff.ignoreSubmodules", "all"),
                           ("status.showUntrackedFiles", "no")]:
            self.git("config", key, value)
        self.assertEqual(checkout_baseline(self.docker, "unused", self.base), head)
        self.assertEqual(export_patch(self.docker, "unused", head), b"")
        row = self.run_attempt()
        self.assertEqual(row["agent_status"], "completed")
        self.assertEqual(row["baseline_commit"], head)
        self.assertEqual(row["patch"], "")

    def test_mode_only_setup_export_contains_agent_content_not_setup_modes(self):
        head = self.mode_setup()
        self.docker.agent_edit = lambda: (self.repo / "normal.py").write_text("new = 2\n")
        row = self.run_attempt()
        self.assertEqual(row["agent_status"], "completed")
        self.assertEqual(row["baseline_commit"], head)
        self.assertIn("+new = 2", row["patch"])
        self.assertNotIn("old mode", row["patch"])
        self.assertNotIn("new mode", row["patch"])
        diff = next(command for command in self.docker.commands if "--cached" in command)
        self.assertIn(head, diff)
        self.assertTrue(any(PI_COMMAND in command for command in self.docker.commands))

    def test_reverse_regular_mode_change_is_accepted(self):
        self.base = self.mode_setup()
        for name in self.names:
            (self.repo / name).chmod(0o644)
        head = self.commit("reverse modes")
        self.assertEqual(checkout_baseline(self.docker, "unused", self.base), head)

    def test_committed_content_and_binary_changes_with_same_modes_are_rejected(self):
        for name in ("normal.py", "binary.dat", self.names[1]):
            with self.subTest(name=name):
                self.git("reset", "--hard", self.base)
                (self.repo / name).write_bytes(b"changed\x00\xfe")
                self.commit("content changed")
                with self.assertRaisesRegex(ValueError, "content/tree"):
                    checkout_baseline(self.docker, "unused", self.base)
        self.assert_rejected_before_launch("content/tree")

    def test_committed_content_change_plus_mode_change_is_rejected(self):
        self.mode_setup()
        (self.repo / "normal.py").write_text("changed\n")
        self.commit("content changed too")
        self.assert_rejected_before_launch("content/tree")

    def test_addition_deletion_and_same_blob_rename_are_rejected(self):
        for change in ("addition", "deletion", "rename"):
            with self.subTest(change=change):
                self.git("reset", "--hard", self.base)
                if change == "addition":
                    (self.repo / "new\nfile").write_text("old = 1\n")
                elif change == "deletion":
                    (self.repo / "normal.py").unlink()
                else:
                    (self.repo / "normal.py").rename(self.repo / "renamed\nfile")
                self.commit(change)
                with self.assertRaisesRegex(ValueError, "content/tree"):
                    checkout_baseline(self.docker, "unused", self.base)
        self.assert_rejected_before_launch("content/tree")

    def test_file_to_symlink_with_identical_blob_is_rejected(self):
        path = self.repo / "normal.py"
        path.unlink()
        path.symlink_to("old = 1\n")
        self.commit("type changed but blob unchanged")
        self.assert_rejected_before_launch("content/tree")

    def test_symlink_target_change_and_symlink_to_file_are_rejected(self):
        path = self.repo / "link"
        path.symlink_to("normal.py")
        self.base = self.commit("base with symlink")
        for change in ("target", "type"):
            with self.subTest(change=change):
                self.git("reset", "--hard", self.base)
                path.unlink()
                if change == "target":
                    path.symlink_to("binary.dat")
                else:
                    path.write_bytes(b"normal.py")
                self.commit(change)
                with self.assertRaisesRegex(ValueError, "content/tree"):
                    checkout_baseline(self.docker, "unused", self.base)
        self.assert_rejected_before_launch("content/tree")

    def test_submodule_addition_and_commit_change_are_rejected(self):
        # Gitlinks can be constructed locally; no fetch or external repository needed.
        self.git("update-index", "--add", "--cacheinfo", "160000", self.base, "module")
        self.git("commit", "--quiet", "-m", "added gitlink")
        with self.assertRaisesRegex(ValueError, "content/tree"):
            checkout_baseline(self.docker, "unused", self.base)
        previous = self.base
        self.base = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("commit", "--quiet", "--allow-empty", "-m", "empty next commit")
        next_commit = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("update-index", "--cacheinfo", "160000", next_commit, "module")
        self.git("commit", "--quiet", "-m", "changed gitlink")
        self.assertNotEqual(previous, next_commit)
        self.assert_rejected_before_launch("content/tree")

    def test_dirty_submodule_is_rejected_despite_ignore_config(self):
        module = self.repo / "module"
        module.mkdir()
        def subgit(*args):
            return subprocess.run(["git", *args], cwd=module, env=self.env,
                                  capture_output=True, check=True)
        subgit("init", "--quiet")
        subgit("config", "user.name", "Eval Test")
        subgit("config", "user.email", "eval@example.invalid")
        (module / "tracked").write_text("clean\n")
        subgit("add", "-A")
        subgit("commit", "--quiet", "-m", "module base")
        self.base = self.commit("task base with module")
        self.assertEqual(checkout_baseline(self.docker, "unused", self.base), self.base)
        self.git("config", "diff.ignoreSubmodules", "all")
        (module / "untracked").write_text("dirty\n")
        self.assert_rejected_before_launch("dirty")

    def test_ignored_setup_cache_preserves_previous_clean_checkout_policy(self):
        (self.repo / ".gitignore").write_text("setup-cache/\n")
        self.base = self.commit("ignore setup cache")
        cache = self.repo / "setup-cache"
        cache.mkdir()
        (cache / "ignored.dat").write_bytes(b"setup cache")
        self.assertEqual(checkout_baseline(self.docker, "unused", self.base), self.base)
        self.assertTrue((cache / "ignored.dat").exists())

    def test_dirty_index_worktree_untracked_and_modes_are_rejected(self):
        self.mode_setup()
        clean_head = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("config", "core.fileMode", "false")
        self.git("config", "status.showUntrackedFiles", "no")
        for change in ("worktree", "index", "untracked", "mode"):
            with self.subTest(change=change):
                self.git("reset", "--hard", clean_head)
                if change == "untracked":
                    (self.repo / "untracked\nfile").write_text("new\n")
                elif change == "mode":
                    (self.repo / "normal.py").chmod(0o644)
                else:
                    (self.repo / "normal.py").write_text("dirty\n")
                    if change == "index":
                        self.git("add", "normal.py")
                with self.assertRaisesRegex(ValueError, "dirty"):
                    checkout_baseline(self.docker, "unused", self.base)
                if change == "untracked":
                    (self.repo / "untracked\nfile").unlink()
        self.assert_rejected_before_launch("dirty")


if __name__ == "__main__":
    unittest.main()
