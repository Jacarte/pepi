import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from evals.swebench.evalscript import checkout_paths, rewrite_checkouts, rewrite_eval_script

SHA = "5d9cf79baf07fc4aed7ad1b06990532a65378155"
BEFORE = f"git checkout {SHA} tests/migrations/test_loader.py tests/migrations/new/0001_initial.py"
SCRIPT = ("#!/bin/bash\nset -uxo pipefail\ncd /testbed\n" + BEFORE + "\n"
          "git apply -v - <<'EOF_1'\ndiff --git a/x b/x\nEOF_1\n: '>>>>> Start Test Output'\n./runtests.py\n"
          ": '>>>>> End Test Output'\n" + BEFORE + "\n")


def per_path(path):
    return f"if git cat-file -e {SHA}:{path} 2>/dev/null; then git checkout {SHA} -- {path}; fi"


class RewriteCheckoutsTests(unittest.TestCase):
    def test_both_checkout_lines_become_per_path_and_everything_else_is_kept(self):
        rewritten, count = rewrite_checkouts(SCRIPT)
        expected_line = per_path("tests/migrations/test_loader.py") + "; " + per_path("tests/migrations/new/0001_initial.py")
        self.assertEqual(count, 2)
        self.assertEqual(rewritten, SCRIPT.replace(BEFORE, expected_line))
        self.assertEqual(rewrite_checkouts(rewritten), (rewritten, 0))  # idempotent
        # A rewritten line is recognised, so its paths stay known (and it is not "unrewritable").
        self.assertEqual(checkout_paths(rewritten), {"tests/migrations/test_loader.py", "tests/migrations/new/0001_initial.py"})
        # A hand-edited look-alike of a rewritten line is not trusted.
        tampered = rewritten.replace("2>/dev/null; then", "2>/dev/null || true; then", 1)
        self.assertEqual((rewrite_checkouts(tampered), checkout_paths(tampered)), ((None, 0), None))

    def test_single_path_extra_spaces_and_crlf(self):
        self.assertEqual(rewrite_checkouts(f"git checkout {SHA}   a/test_x.py  \r\nrest\r\n"),
                         (per_path("a/test_x.py") + "\r\nrest\r\n", 1))
        self.assertEqual(checkout_paths(f"git checkout {SHA} a b c\n"), {"a", "b", "c"})

    def test_unsafe_or_unusual_checkout_lines_are_unrewritable(self):
        for line in (f"git checkout {SHA} 'tests/a b.py'", f'git checkout {SHA} "tests/x.py"',
                     f"git checkout {SHA} tests/x.py; rm -rf /", f"git checkout {SHA} tests/$X.py",
                     f"git checkout {SHA} tests/*.py", f"git checkout {SHA} -- tests/x.py",
                     f"git checkout {SHA} /etc/passwd", f"git checkout {SHA} ../x.py",
                     f"git checkout {SHA} tests/x.py # note", f"git checkout {SHA}", "git checkout main tests/x.py",
                     f"git checkout {SHA.upper()} tests/x.py", f"  git checkout {SHA} tests/x.py",
                     f"git checkout {SHA}\ttests/x.py",
                     # '.', empty or './' components and a trailing '/': cat-file and checkout read them differently.
                     f"git checkout {SHA} .", f"git checkout {SHA} ./tests/x.py", f"git checkout {SHA} tests/./x.py",
                     f"git checkout {SHA} tests//x.py", f"git checkout {SHA} tests/",
                     # Checkouts not at the start of the line, or via other git forms, cannot be rewritten either.
                     f"cd /testbed && git checkout {SHA} tests/x.py", f"git -c core.x=1 checkout {SHA} tests/x.py",
                     f"(git checkout {SHA} tests/x.py)", "git checkout-index -a -f", "# git checkout runs below"):
            with self.subTest(line=line):
                script = SCRIPT.replace(BEFORE, line, 1)
                self.assertEqual(rewrite_checkouts(script), (None, 0))
                self.assertIsNone(checkout_paths(script))

    def test_scripts_without_checkout_and_other_git_commands_are_untouched(self):
        script = "cd /testbed\ngit status\ngit apply -v -\necho checkout done\n"
        self.assertEqual(rewrite_checkouts(script), (script, 0))
        self.assertEqual(checkout_paths(script), set())

    def test_eval_script_bytes_must_be_utf8(self):
        self.assertEqual(rewrite_eval_script(b"\xff\xfe" + SCRIPT.encode()), (None, 0))
        self.assertEqual(rewrite_eval_script(SCRIPT.encode())[1], 2)

    @unittest.skipUnless(os.path.exists("/bin/bash"), "needs /bin/bash")
    def test_rewritten_line_restores_existing_files_and_skips_missing_ones_in_bash(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

            def git(*args):
                return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True,
                                      text=True).stdout.strip()

            git("init", "-q")
            (repo / "tests").mkdir()
            (repo / "tests/test_loader.py").write_text("original\n")
            git("add", ".")
            git("commit", "-q", "-m", "base")
            base = git("rev-parse", "HEAD")
            (repo / "tests/test_loader.py").write_text("agent edit\n")
            original = f"git checkout {base} tests/test_loader.py tests/new/0001_initial.py\n"
            # The official line aborts on the missing path and keeps the agent's edit.
            subprocess.run(["/bin/bash", "-c", "set -uxo pipefail\n" + original], cwd=repo, env=env,
                           capture_output=True)
            self.assertEqual((repo / "tests/test_loader.py").read_text(), "agent edit\n")
            rewritten, count = rewrite_checkouts(original)
            self.assertEqual(count, 1)
            subprocess.run(["/bin/bash", "-c", "set -uxo pipefail\n" + rewritten], cwd=repo, env=env,
                           capture_output=True, check=True)
            self.assertEqual((repo / "tests/test_loader.py").read_text(), "original\n")
            self.assertFalse((repo / "tests/new").exists())


if __name__ == "__main__":
    unittest.main()
