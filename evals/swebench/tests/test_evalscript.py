import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from evals.swebench.evalscript import checkout_paths, rewrite_checkouts, rewrite_eval_script
from evals.swebench.reporting import restore_selection

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


# Checkout lines of the 10 Multilingual (Niffler) eval scripts; tokio's resets its test file with `rm -f` instead.
MULTILINGUAL_CHECKOUTS = {
    "axios__axios-4731": ["test/unit/adapters/http.js"],
    "caddyserver__caddy-6115": ["modules/caddyhttp/reverseproxy/selectionpolicies_test.go"],
    "facebook__docusaurus-10130": ["packages/docusaurus/src/server/__tests__/brokenLinks.test.ts"],
    "fmtlib__fmt-1683": ["test/printf-test.cc"],
    "gin-gonic__gin-1805": ["routes_test.go"],
    "jqlang__jq-2235": ["tests/shtest"],
    "nushell__nushell-12901": ["tests/shell/environment/env.rs"],
    "redis__redis-10068": ["tests/unit/type/stream.tcl"],
    "rubocop__rubocop-13362": ["spec/rubocop/cop/style/redundant_freeze_spec.rb",
                               "spec/rubocop/cop/style/redundant_interpolation_unfreeze_spec.rb"],
}
TOKIO_SCRIPT = ("#!/bin/bash\nset -uxo pipefail\ncd /testbed\nrm -f tokio/tests/net_types_unwind.rs\n"
                "git apply -v - <<'EOF_1'\nnew file mode 100644\nEOF_1\n: '>>>>> Start Test Output'\n"
                "(cargo test --package tokio --test net_types_unwind) | cat\n: '>>>>> End Test Output'\n"
                "rm -f tokio/tests/net_types_unwind.rs\n")


def multilingual_script(paths):
    line = f"git checkout {SHA} " + " ".join(paths)
    return SCRIPT.replace(BEFORE, line)


class MultilingualCheckoutTests(unittest.TestCase):
    def test_each_multilingual_checkout_line_is_rewritten_per_path(self):
        for instance_id, paths in MULTILINGUAL_CHECKOUTS.items():
            with self.subTest(instance_id=instance_id):
                script = multilingual_script(paths)
                self.assertEqual(checkout_paths(script), set(paths))
                rewritten, count = rewrite_checkouts(script)
                self.assertEqual(count, 2)
                self.assertEqual(rewritten, script.replace(f"git checkout {SHA} " + " ".join(paths),
                                                           "; ".join(per_path(path) for path in paths)))
                self.assertEqual(rewrite_checkouts(rewritten), (rewritten, 0))

    def test_script_without_checkout_line_is_never_selected_for_restore(self):
        self.assertEqual(rewrite_checkouts(TOKIO_SCRIPT), (TOKIO_SCRIPT, 0))
        self.assertEqual(checkout_paths(TOKIO_SCRIPT), set())
        source = "diff --git a/tokio/src/net/udp.rs b/tokio/src/net/udp.rs\n--- a/tokio/src/net/udp.rs\n" \
                 "+++ b/tokio/src/net/udp.rs\n@@ -1 +1 @@\n-a\n+b\n"
        test_edit = source.replace("tokio/src/net/udp.rs", "tokio/tests/net_types_unwind.rs")
        rubocop = "spec/rubocop/cop/style/redundant_freeze_spec.rb"
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            scripts = {"tokio-rs__tokio-4384": TOKIO_SCRIPT,
                       "rubocop__rubocop-13362": multilingual_script(MULTILINGUAL_CHECKOUTS["rubocop__rubocop-13362"])}
            for instance_id, script in scripts.items():
                (repo / "tasks" / instance_id).mkdir(parents=True)
                (repo / "tasks" / instance_id / "eval.sh").write_text(script)
            manifest = {"tasks": [{"instance_id": instance_id} for instance_id in scripts]}
            submitted = {"tokio-rs__tokio-4384": {"model_patch": source + test_edit},
                         "rubocop__rubocop-13362": {"model_patch": source.replace("tokio/src/net/udp.rs", rubocop)}}
            self.assertEqual(restore_selection(submitted, manifest, repo),
                             {"selected": ["rubocop__rubocop-13362"], "unrewritable": [],
                              "not_listed": ["tokio-rs__tokio-4384"]})


if __name__ == "__main__":
    unittest.main()
