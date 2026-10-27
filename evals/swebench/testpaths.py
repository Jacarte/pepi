"""Find the test files a graded patch touches, and strip them for the strict regrade.

Some task eval scripts fail to restore the agent's test edits before applying the reference
test patch (django__django-11141's `git checkout` names a file that does not exist yet, so git
restores nothing), so an edited existing test can decide the grade. Only paths from the
per-file `diff --git` headers are read here; diff content is never returned or logged.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

# `tests`, `__tests__` (Jest) and `testdata` (Go fixtures; ignored by `go build`) mark tests at any depth.
# `test`/`testing`/`spec`/`specs` only at the top level (`test/`, pytest's `testing/`, RSpec's `spec/`), as
# Maven/Gradle's `src/test/` or as a JS monorepo's `packages/<name>/test/`: other nested ones are often library
# code, e.g. django/test/testcases.py, sympy/testing/runtests.py, druid's .../query/spec/*.java. Nested RSpec
# files are still caught by their `*_spec.rb` basename.
TEST_DIRECTORIES = frozenset({"tests", "__tests__", "testdata"})
TOP_LEVEL_TEST_DIRECTORIES = frozenset({"test", "testing", "spec", "specs"})
# Basenames: Python test_*.py/*_test(s).py/conftest.py; Go *_test.go (a Go test_*.go is compiled into the package,
# so it is library code); Ruby *_spec.rb, test_*.rb, *_test.rb and the RSpec spec_helper.rb/rails_helper.rb
# support files; JS/TS *.test.* and *.spec.*; C/C++ test_*, *_test, *-test (so jq's test runner src/jq_test.c is
# test-like, while jq's and redis's other src/*.c stay source); Rust test_*.rs/*_test.rs (Rust unit tests inside
# source files cannot be told apart by path).
TEST_BASENAME = re.compile(
    r"test_.*\.(?:py|rb|rs|c|cc|cpp|cxx|js)"
    r"|.*_tests?\.(?:py|rb|rs|c|cc|cpp|cxx)"
    r"|.*_test\.go"
    r"|.*_spec\.rb|spec_helper\.rb|rails_helper\.rb"
    r"|.*-test\.(?:c|cc|cpp|cxx)"
    r"|.*\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs|cjs)"
    r"|conftest\.py")
TEST_FILES_LISTED = 10
# Bumped when the heuristic or the stripping changes, so strict results from an older policy are refused.
STRIP_POLICY_VERSION = 3
STRIP_POLICY = "remove every per-file diff block whose old or new path is test-like (added files too)"
# One pass over git's C-style escapes, so an escaped backslash followed by digits stays literal.
_QUOTED_ESCAPE = re.compile(rb'\\([0-7]{3}|[\\"tnrabfv])')
_ESCAPES = {b"\\": b"\\", b'"': b'"', b"t": b"\t", b"n": b"\n", b"r": b"\r",
            b"a": b"\a", b"b": b"\b", b"f": b"\f", b"v": b"\v"}


def is_test_path(path: str) -> bool:
    """Test-like: a tests/__tests__/testdata directory component at any depth, a top-level test/testing/spec/specs
    directory, `src/test/`, `packages/<name>/test/`, or a test-file basename (see TEST_BASENAME)."""
    parts = PurePosixPath(path).parts
    directories = parts[:-1]
    return bool(parts) and (any(part in TEST_DIRECTORIES for part in directories)
                            or (bool(directories) and directories[0] in TOP_LEVEL_TEST_DIRECTORIES)
                            or any(part == "test" and parent == "src"
                                   for parent, part in zip(directories, directories[1:]))
                            or (len(directories) >= 3 and directories[0] == "packages" and directories[2] == "test")
                            or bool(TEST_BASENAME.fullmatch(parts[-1])))


def _unquote(token: str) -> str:
    """Git's C-style quoted path ("a/x\\303\\251") back to text."""
    raw = token[1:-1].encode("utf-8", "surrogateescape")
    raw = _QUOTED_ESCAPE.sub(lambda match: (_ESCAPES.get(match.group(1))
                                            or bytes([int(match.group(1), 8) & 0xFF])), raw)
    return raw.decode("utf-8", "replace")


def _strip_prefix(path: str | None) -> str | None:
    if path is None or path == "/dev/null":
        return None
    return path[2:] if path[:2] in ("a/", "b/") else path


def _header_name(line: str, marker: str) -> str | None:
    """Path from a `--- a/x`, `+++ b/x`, `rename from x` style line (git adds a TAB after names with spaces)."""
    value = line[len(marker):]
    if value.startswith('"'):
        end = re.match(r'"(?:[^"\\]|\\.)*"', value)
        return _unquote(end.group(0)) if end else None
    return value.split("\t", 1)[0]


def _git_header_paths(rest: str) -> tuple[str | None, str | None]:
    """Paths from `diff --git <a> <b>` when no other header names them (mode-only or binary changes)."""
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"|\S+', rest)
    if len(tokens) == 2 and all(token.startswith('"') for token in tokens):
        return _strip_prefix(_unquote(tokens[0])), _strip_prefix(_unquote(tokens[1]))
    # Unquoted names may contain spaces; git writes the same name twice, so split in the middle.
    length = (len(rest) - 5) // 2
    if length > 0 and rest[:2] == "a/" and rest[2 + length:] == " b/" + rest[2:2 + length]:
        name = rest[2:2 + length]
        return name, name
    if " b/" in rest:
        left, right = rest.rsplit(" b/", 1)
        return _strip_prefix(left), right
    return None, None


def file_blocks(patch: str) -> list[dict]:
    """One entry per `diff --git` block: start/end line indexes, old/new path and status."""
    lines = patch.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    blocks = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        old = new = None
        added = deleted = renamed = False
        for line in lines[start + 1:end]:
            text = line.rstrip("\r\n")
            if text.startswith("@@") or text.startswith("GIT binary patch"):
                break
            if text.startswith("new file mode"):
                added = True
            elif text.startswith("deleted file mode"):
                deleted = True
            elif text.startswith("rename from "):
                renamed, old = True, _header_name(text, "rename from ")
            elif text.startswith("rename to "):
                renamed, new = True, _header_name(text, "rename to ")
            elif text.startswith("--- "):
                old = old if renamed else _strip_prefix(_header_name(text, "--- "))
            elif text.startswith("+++ "):
                new = new if renamed else _strip_prefix(_header_name(text, "+++ "))
        if (old is None and not added) or (new is None and not deleted):
            fallback_old, fallback_new = _git_header_paths(lines[start].rstrip("\r\n")[len("diff --git "):])
            old = old if old is not None or added else fallback_old
            new = new if new is not None or deleted else fallback_new
        status = "added" if added else "deleted" if deleted else "renamed" if renamed else "modified"
        blocks.append({"start": start, "end": end, "old_path": None if added else old,
                       "new_path": None if deleted else new, "status": status})
    return blocks


def patch_test_edits(patch: str) -> dict:
    """Test-like files a patch touches; `kind` is none, added_only, modified_existing or unknown."""
    touched, existing, added = set(), 0, 0
    blocks = file_blocks(patch or "")
    for block in blocks:
        paths = [path for path in (block["old_path"], block["new_path"]) if path]
        if not any(is_test_path(path) for path in paths):
            continue
        touched.update(path for path in paths if is_test_path(path))
        if block["status"] == "added":
            added += 1
        else:
            existing += 1
    if patch and patch.strip() and not blocks:
        kind = "unknown"  # not a git diff: nothing can be said about test files
    elif existing:
        kind = "modified_existing"
    elif added:
        kind = "added_only"
    else:
        kind = "none"
    ordered = sorted(touched)
    return {"patch_touches_tests": bool(touched), "test_files_touched": ordered[:TEST_FILES_LISTED],
            "test_files_touched_count": len(ordered), "test_files_modified_existing": existing,
            "test_files_added": added, "test_edit_kind": kind}


def existing_paths(patch: str) -> set:
    """Base-commit paths of every existing file a patch modifies, renames or deletes (test-like or not).

    Selection only (`grade --restore-test-files`): an eval script's checkout line is the authoritative list
    of the files it resets, so it is intersected with all of these, not with the is_test_path heuristic.
    """
    return {block["old_path"] for block in file_blocks(patch or "")
            if block["status"] != "added" and block["old_path"]}


def strip_test_files(patch: str) -> tuple[str, int]:
    """The patch without test-like per-file blocks, and how many blocks were removed."""
    lines = patch.splitlines(keepends=True)
    blocks = file_blocks(patch)
    removed = [block for block in blocks
               if any(is_test_path(path) for path in (block["old_path"], block["new_path"]) if path)]
    drop = {index for block in removed for index in range(block["start"], block["end"])}
    kept = "".join(line for index, line in enumerate(lines) if index not in drop)
    return ("" if not kept.strip() else kept), len(removed)
