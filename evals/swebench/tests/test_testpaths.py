import unittest

from evals.swebench.testpaths import existing_paths, file_blocks, is_test_path, patch_test_edits, strip_test_files


def block(path, body="@@ -1 +1 @@\n-a\n+b\n", old=None, header=None):
    old = old or path
    return (header or f"diff --git a/{old} b/{path}\n") + f"index 1..2 100644\n--- a/{old}\n+++ b/{path}\n" + body


SOURCE = block("django/db/migrations/loader.py")
EXISTING_TEST = block("tests/migrations/test_loader.py")
NEW_TEST = ("diff --git a/pkg/tests/test_new.py b/pkg/tests/test_new.py\nnew file mode 100644\n"
            "index 0000000..1\n--- /dev/null\n+++ b/pkg/tests/test_new.py\n@@ -0,0 +1 @@\n+x\n")


class TestPathHeuristicTests(unittest.TestCase):
    def test_directories_and_basenames(self):
        for path in ("tests/migrations/test_loader.py", "lib/matplotlib/tests/helpers.py", "test/x.c",
                     "test/foo.py", "testing/test_foo.py", "testing/helpers.py", "tests/x.py", "pkg/tests/y.py",
                     "sympy/testing/tests/test_a.py", "web/__tests__/a.js", "sympy/core/test_basic.py",
                     "a/b_test.py", "a_test.py", "a/b_tests.py", "conftest.py", "src/conftest.py",
                     "ui/button.test.ts", "ui/x.test.js"):
            self.assertTrue(is_test_path(path), path)
        # Nested test/testing packages are library code, not tests.
        for path in ("django/test/testcases.py", "django/test/runner.py", "lib/matplotlib/testing/decorators.py",
                     "sympy/testing/runtests.py", "sphinx/testing/fixtures.py", "xarray/testing/assertions.py",
                     "pkg/testing/util.py", "django/db/migrations/loader.py", "tests", "test", "testing",
                     "src/testsuite.py", "src/latest.py", "docs/testing.rst", "contest.py", "src/test.py",
                     "ui/x.spec.ts", ""):
            self.assertFalse(is_test_path(path), path)


class PatchTestEditsTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(patch_test_edits(SOURCE)["test_edit_kind"], "none")
        self.assertEqual(patch_test_edits("")["test_edit_kind"], "none")
        self.assertEqual(patch_test_edits(SOURCE + NEW_TEST)["test_edit_kind"], "added_only")
        edits = patch_test_edits(SOURCE + EXISTING_TEST + NEW_TEST)
        self.assertEqual(edits, {"patch_touches_tests": True,
                                 "test_files_touched": ["pkg/tests/test_new.py", "tests/migrations/test_loader.py"],
                                 "test_files_touched_count": 2, "test_files_modified_existing": 1,
                                 "test_files_added": 1, "test_edit_kind": "modified_existing"})
        self.assertEqual(patch_test_edits("not a diff\n")["test_edit_kind"], "unknown")

    def test_deletion_rename_quoted_and_mode_only_headers(self):
        deleted = ("diff --git a/tests/test_gone.py b/tests/test_gone.py\ndeleted file mode 100644\n"
                   "index 1..0\n--- a/tests/test_gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
        renamed = ("diff --git a/old_test.py b/src/helper.py\nsimilarity index 100%\n"
                   "rename from old_test.py\nrename to src/helper.py\n")
        quoted = ('diff --git "a/tests/sp\\303\\251 c.py" "b/tests/sp\\303\\251 c.py"\n'
                  'index 1..2 100644\n--- "a/tests/sp\\303\\251 c.py"\n+++ "b/tests/sp\\303\\251 c.py"\n'
                  "@@ -1 +1 @@\n-a\n+b\n")
        spaced_mode = "diff --git a/my tests/x.py b/my tests/x.py\nold mode 100644\nnew mode 100755\n"
        mode_only = "diff --git a/tests/x y.py b/tests/x y.py\nold mode 100644\nnew mode 100755\n"
        blocks = file_blocks(deleted + renamed + quoted + spaced_mode + mode_only)
        self.assertEqual([(row["old_path"], row["new_path"], row["status"]) for row in blocks], [
            ("tests/test_gone.py", None, "deleted"), ("old_test.py", "src/helper.py", "renamed"),
            ("tests/spé c.py", "tests/spé c.py", "modified"), ("my tests/x.py", "my tests/x.py", "modified"),
            ("tests/x y.py", "tests/x y.py", "modified")])
        edits = patch_test_edits(deleted + renamed + quoted + mode_only)
        self.assertEqual(edits["test_files_touched"], ["old_test.py", "tests/spé c.py", "tests/test_gone.py",
                                                       "tests/x y.py"])
        self.assertEqual((edits["test_files_modified_existing"], edits["test_edit_kind"]), (4, "modified_existing"))
        # Base-commit paths of every existing file, test-like or not (the restore selection intersects them
        # with the checkout line); a renamed file counts at its old path; added files are not existing.
        fixture = block("data/fixtures.json")
        self.assertEqual(existing_paths(deleted + renamed + quoted + mode_only + NEW_TEST + SOURCE + fixture),
                         {"tests/test_gone.py", "old_test.py", "tests/spé c.py", "tests/x y.py",
                          "django/db/migrations/loader.py", "data/fixtures.json"})
        self.assertEqual(existing_paths(""), set())
        self.assertEqual(existing_paths("not a diff\n"), set())

    def test_quoted_names_are_decoded_in_one_pass(self):
        def quoted(name):
            return (f'diff --git "a/{name}" "b/{name}"\nindex 1..2 100644\n--- "a/{name}"\n+++ "b/{name}"\n'
                    "@@ -1 +1 @@\n-a\n+b\n")

        # An escaped backslash followed by digits is a literal backslash, not an octal byte.
        self.assertEqual(file_blocks(quoted("a/x\\\\303.py"))[0]["new_path"], "a/x\\303.py")
        self.assertEqual(file_blocks(quoted("tests/caf\\303\\251.py"))[0]["new_path"], "tests/café.py")
        self.assertEqual(file_blocks(quoted('tests/q\\"t\\tx.py'))[0]["new_path"], 'tests/q"t\tx.py')

    def test_lists_at_most_ten_paths_but_counts_all(self):
        patch = "".join(block(f"tests/test_{index:02d}.py") for index in range(12))
        edits = patch_test_edits(patch)
        self.assertEqual((len(edits["test_files_touched"]), edits["test_files_touched_count"]), (10, 12))

    def test_hunk_lines_that_look_like_headers_are_content(self):
        body = "@@ -1,2 +1,2 @@\n-diff --git a/tests/x.py b/tests/x.py\n+--- a/tests/x.py\n"
        edits = patch_test_edits(block("src/a.py", body=body))
        self.assertEqual(edits["test_edit_kind"], "none")


class StripTestFilesTests(unittest.TestCase):
    def test_removes_every_test_like_block_and_keeps_source_blocks_verbatim(self):
        stripped, removed = strip_test_files(EXISTING_TEST + SOURCE + NEW_TEST)
        self.assertEqual((stripped, removed), (SOURCE, 2))
        self.assertEqual(strip_test_files(SOURCE), (SOURCE, 0))

    def test_only_test_blocks_leave_an_empty_patch(self):
        self.assertEqual(strip_test_files(EXISTING_TEST + NEW_TEST), ("", 2))
        self.assertEqual(strip_test_files(""), ("", 0))


if __name__ == "__main__":
    unittest.main()
