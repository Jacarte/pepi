"""Repair the test-file checkout of task eval scripts for `grade --restore-test-files`.

Task eval scripts reset the test files with one `git checkout <base> <path>...` before applying the
reference test patch (and again at the end). git refuses the whole command when any listed path does
not exist at <base>, e.g. a file the test patch creates, so nothing is reset and the agent's edits to
existing test files are graded (django__django-11141). The repair checks each path out on its own and
skips paths absent from <base>. Only checkout lines of the exact form below are rewritten; any other
line that mentions `git ... checkout` (anywhere in the line, e.g. `cd x && git checkout`, `git -c k=v
checkout`, `git checkout-index`, a comment) makes the script unrewritable and it is left untouched;
lines this module wrote are recognised, so rewriting is idempotent.
"""
from __future__ import annotations

import re

# Bumped when the rewrite or the task selection changes, so older restored regrades are refused.
RESTORE_POLICY_VERSION = 2
RESTORE_POLICY = ("eval.sh `git checkout <base> <paths>` lines become per-path `git checkout <base> -- <path>`, "
                  "skipping paths absent from <base>; regrades tasks whose patch modifies an existing file such a "
                  "line lists; the model patch is unchanged")
CHECKOUT_LINE = re.compile(r"git checkout ([0-9a-f]{40})((?: +[^ ]+)+) *")
# Any other line that could run a checkout; such a script is not rewritten (restored outcome unknown).
CHECKOUT_MENTION = re.compile(r"\bgit\b.*\bcheckout\b")
# Plain relative paths only: no shell metacharacters, quotes, globs, leading '-' or '/'.
SAFE_PATH = re.compile(r"[A-Za-z0-9_.@+,=][A-Za-z0-9_./@+,=-]*")


def _safe_path(path: str) -> bool:
    # git cat-file resolves <sha>:<path> literally while git checkout normalises pathspecs, so only paths both
    # read the same way: no '.', '..' or empty components (no './x', 'a//b' or trailing '/').
    return bool(SAFE_PATH.fullmatch(path)) and all(part not in ("", ".", "..") for part in path.split("/"))


def _checkout(text: str) -> tuple[str, list] | None:
    """(sha, paths) of a rewritable checkout line, else None."""
    match = CHECKOUT_LINE.fullmatch(text)
    if not match:
        return None
    paths = match.group(2).split()
    return (match.group(1), paths) if all(_safe_path(path) for path in paths) else None


def _is_checkout(text: str) -> bool:
    return bool(CHECKOUT_MENTION.search(text))


def _per_path_line(sha: str, paths: list) -> str:
    # `if` keeps a skipped path from leaving a failing status behind; only a real checkout can fail.
    return "; ".join(f"if git cat-file -e {sha}:{path} 2>/dev/null; then git checkout {sha} -- {path}; fi"
                     for path in paths)


def _rewritten_paths(text: str) -> list | None:
    """Paths of a line this module already wrote (kept so rewriting is idempotent), else None."""
    found = re.findall(r"then git checkout ([0-9a-f]{40}) -- ([^ ;]+); fi", text)
    if not found or len({sha for sha, _ in found}) != 1:
        return None
    paths = [path for _, path in found]
    if not all(_safe_path(path) for path in paths):
        return None
    return paths if text == _per_path_line(found[0][0], paths) else None


def checkout_paths(script: str) -> set | None:
    """Paths the script's checkout lines reset, or None when a checkout line is not rewritable."""
    paths = set()
    for line in script.split("\n"):
        text = line[:-1] if line.endswith("\r") else line
        if not _is_checkout(text):
            continue
        parsed = _checkout(text)
        if parsed is None:
            rewritten = _rewritten_paths(text)
            if rewritten is None:
                return None
            paths.update(rewritten)
            continue
        paths.update(parsed[1])
    return paths


def rewrite_checkouts(script: str) -> tuple[str | None, int]:
    """The script with each checkout line made per-path and existence-checked, and how many lines changed.

    None when a checkout line is not of the rewritable form. Every other byte is kept, and lines this
    function wrote are recognised and kept, so rewriting again changes nothing (idempotent). POSIX sh and bash 3.2.
    """
    lines, rewritten = script.split("\n"), 0
    for index, line in enumerate(lines):
        ending = "\r" if line.endswith("\r") else ""
        text = line[:len(line) - len(ending)]
        if not _is_checkout(text):
            continue
        parsed = _checkout(text)
        if parsed is None:
            if _rewritten_paths(text) is None:
                return None, 0
            continue
        lines[index] = _per_path_line(*parsed) + ending
        rewritten += 1
    return "\n".join(lines), rewritten


def rewrite_eval_script(data: bytes) -> tuple[bytes | None, int]:
    """rewrite_checkouts on eval.sh bytes; None when they are not UTF-8 or a checkout line is unrewritable."""
    try:
        script = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, 0
    result, count = rewrite_checkouts(script)
    return (None, 0) if result is None else (result.encode("utf-8"), count)
