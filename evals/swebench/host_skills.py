"""Consented host skills: filter, hash and stage the host's global skills for one run.

Sources follow Pi's own user discovery order (<agent-dir>/skills, then ~/.agents/skills); the
first skill of a name wins, as on the host. A skill is copied whole or not at all. Python is
authoritative: the shell only asks for consent and pins the approved digest.
"""
from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

from .common import MIN_REDACTED_LENGTH, SECRET_PATTERN

# (relative to HOME, label printed in the preview); order = Pi's discovery order.
HOST_SKILL_SOURCES = ((".pi/agent/skills", "~/.pi/agent/skills"), (".agents/skills", "~/.agents/skills"))
# Digest of the skill list the user approved from the preview; set by the shell only, via env.
HOST_SKILLS_SHA256_ENV = "PEPI_EVAL_HOST_SKILLS_SHA256"
SKILL_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
# File/directory names inside a skill: printable, no path separators.
ENTRY_NAME = re.compile(r"[^\x00-\x1f\x7f/\\]{1,255}")
MAX_FILE_BYTES = 1048576
MAX_SKILL_BYTES = 5 * 1048576
MAX_SKILL_FILES = 200
MAX_SKILLS = 50
# Credential-like file names (compared lower-case): exact names, prefixes and suffixes.
FORBIDDEN_FILES = frozenset({"auth.json", "mcp.json", ".env", ".netrc", ".npmrc", ".pypirc"})
FORBIDDEN_PREFIXES = (".env.", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials")
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")
FORBIDDEN_REASON = ("contains a credential-like file (.env*, auth.json, mcp.json, .netrc, .npmrc, .pypirc, "
                    "credentials*, SSH keys, *.pem/*.key/*.p12/*.pfx/*.keystore/*.jks) or a .git entry")
# Skill-only content checks beyond SECRET_PATTERN: PEM private keys and quoted (JSON/YAML) credential
# assignments with a value of 8+ token characters, so prose such as "password policy" or "password: ask"
# passes this rule. SECRET_PATTERN (shared with narrate's scrubbing) is unchanged.
SKILL_SECRET_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY[A-Z ]*-----"
    r"|\"?(?:api[_-]?key|token|secret|private_key|password|passwd|access[_-]?key)[\w-]*\"?\s*[:=]\s*"
    r"(?:\"[^\"\s]{8,}\"|'[^'\s]{8,}'|[A-Za-z0-9_\-./+=]{8,})",
    re.IGNORECASE)
# A skill tree deeper than this is refused (bounds open directory descriptors during the walk).
MAX_SKILL_DEPTH = 16
SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".ps1")
# Credentials whose values a skill must not contain (checked when present in the environment).
CREDENTIAL_ENV = ("LITELLM_API_KEY",)


class SkillRejected(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Skill:
    name: str
    # (POSIX path relative to the skill directory, content, executable), sorted by path.
    files: tuple[tuple[str, bytes, bool], ...]

    @property
    def size(self) -> int:
        return sum(len(content) for _, content, _ in self.files)

    @property
    def scripts(self) -> int:
        return sum(executable or path.endswith(SCRIPT_SUFFIXES) or content.startswith(b"#!")
                   for path, content, executable in self.files)

    @property
    def sha256(self) -> str:
        """Over sorted relative paths, executable bits and bytes, length-framed."""
        digest = hashlib.sha256()
        for path, content, executable in self.files:
            encoded = path.encode()
            digest.update(len(encoded).to_bytes(8, "big") + encoded + bytes([executable])
                          + len(content).to_bytes(8, "big") + content)
        return digest.hexdigest()

    def record(self) -> dict:
        return {"name": self.name, "sha256": self.sha256, "files": len(self.files)}


def _forbidden(name: str, directory: bool) -> bool:
    name = name.lower()
    if name == ".git":
        return True
    return not directory and (name in FORBIDDEN_FILES or name.startswith(FORBIDDEN_PREFIXES)
                              or name.endswith(FORBIDDEN_SUFFIXES))


def _open_directory(name, dir_fd: int | None = None) -> int:
    """Open a directory without following a symlink in its last component."""
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SkillRejected("contains a symlink") from None
        raise SkillRejected("cannot read a directory") from None


def _walk(fd: int, relative: str, depth: int, state: dict) -> None:
    """Read one directory through its descriptor; sub-directories and files open relative to it, so a
    path swapped for a symlink during the scan is refused instead of followed."""
    if depth > MAX_SKILL_DEPTH:
        raise SkillRejected(f"nested deeper than {MAX_SKILL_DEPTH} directories")
    try:
        with os.scandir(fd) as iterator:
            names = sorted(entry.name for entry in iterator)
    except OSError:
        raise SkillRejected("cannot read a directory") from None
    for name in names:
        if not ENTRY_NAME.fullmatch(name):
            raise SkillRejected("contains an unsupported file name")
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            raise SkillRejected("contains an unsupported file name") from None
        path = f"{relative}/{name}" if relative else name
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError:
            raise SkillRejected("cannot read a file") from None
        if stat.S_ISLNK(info.st_mode):
            raise SkillRejected("contains a symlink")
        is_directory = stat.S_ISDIR(info.st_mode)
        if _forbidden(name, is_directory):
            raise SkillRejected(FORBIDDEN_REASON)
        if is_directory:
            state["directories"] += 1
            if state["directories"] > MAX_SKILL_FILES:
                raise SkillRejected(f"more than {MAX_SKILL_FILES} directories")
            child = _open_directory(name, fd)
            try:
                _walk(child, path, depth + 1, state)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise SkillRejected("contains a special file")
        if len(state["files"]) >= MAX_SKILL_FILES:
            raise SkillRejected(f"more than {MAX_SKILL_FILES} files")
        try:
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except OSError as error:
            raise SkillRejected("contains a symlink" if error.errno == errno.ELOOP else "cannot read a file") \
                from None
        try:
            with os.fdopen(file_fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise SkillRejected("contains a special file")
                # Another name for the same bytes can live outside the skill (e.g. a hard-linked key).
                if info.st_nlink > 1:
                    raise SkillRejected("contains a hard-linked file")
                content = stream.read(MAX_FILE_BYTES + 1)
        except OSError:
            raise SkillRejected("cannot read a file") from None
        if len(content) > MAX_FILE_BYTES:
            raise SkillRejected("a file is larger than 1 MiB")
        state["total"] += len(content)
        if state["total"] > MAX_SKILL_BYTES:
            raise SkillRejected("larger than 5 MiB")
        text = content.decode("utf-8", "replace")
        if SECRET_PATTERN.search(text) or SKILL_SECRET_PATTERN.search(text):
            raise SkillRejected("a file matches a secret/token pattern")
        if any(value in content for value in state["values"]):
            raise SkillRejected("a file contains a credential value of this run")
        state["files"].append((path, content, bool(info.st_mode & stat.S_IXUSR)))


def read_skill(directory: Path | str, name: str, secret_values=(), dir_fd: int | None = None) -> Skill:
    """Read one skill directory without following any symlink; raise SkillRejected (names-only reasons).

    With `dir_fd`, `directory` is a name relative to that open directory."""
    if not SKILL_NAME.fullmatch(name) or name.startswith("."):
        raise SkillRejected("invalid skill name")
    state = {"files": [], "total": 0, "directories": 0,
             "values": [value for value in secret_values if len(value) >= MIN_REDACTED_LENGTH]}
    root = _open_directory(directory, dir_fd)
    try:
        _walk(root, "", 0, state)
    finally:
        os.close(root)
    files = state["files"]
    if not any(path == "SKILL.md" for path, _, _ in files):
        raise SkillRejected("SKILL.md missing")
    return Skill(name, tuple(sorted(files)))


def skill_holding(skills: list[Skill], values) -> str | None:
    """Name of the first skill with a file containing one of `values` (8+ bytes), else None."""
    values = [value for value in values if len(value) >= MIN_REDACTED_LENGTH]
    for skill in skills:
        if any(value in content for _, content, _ in skill.files for value in values):
            return skill.name
    return None


def _label(name: str, index: int) -> str:
    return name if SKILL_NAME.fullmatch(name) else f"<entry #{index}>"


def collect_host_skills(home: Path, secret_values=()) -> tuple[list[Skill], list[tuple[str, str]]]:
    """Every usable host skill, plus (name, reason) for each entry that is not copied."""
    skills, dropped, seen = [], [], {}
    for relative, source in HOST_SKILL_SOURCES:
        base = home / relative
        try:
            info = os.lstat(base)
        except FileNotFoundError:
            continue
        except OSError:
            dropped.append((source, "cannot read the skills directory"))
            continue
        if stat.S_ISLNK(info.st_mode):
            dropped.append((source, "skills directory is a symlink; not followed"))
            continue
        if not stat.S_ISDIR(info.st_mode):
            dropped.append((source, "not a directory"))
            continue
        try:
            base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            dropped.append((source, "cannot read the skills directory"))
            continue
        try:
            _collect_source(base_fd, source, skills, dropped, seen, secret_values)
        finally:
            os.close(base_fd)
    return skills, dropped


def _collect_source(base_fd: int, source: str, skills: list[Skill], dropped: list[tuple[str, str]],
                    seen: dict, secret_values) -> None:
    try:
        with os.scandir(base_fd) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
    except OSError:
        dropped.append((source, "cannot read the skills directory"))
        return
    for index, entry in enumerate(entries, 1):
        label = _label(entry.name, index)
        if entry.name.startswith("."):
            dropped.append((label, "hidden entry; not a skill"))
        elif entry.is_symlink():
            dropped.append((label, "symlink; not followed"))
        elif not entry.is_dir(follow_symlinks=False):
            dropped.append((label, "not a skill directory"))
        elif not SKILL_NAME.fullmatch(entry.name):
            dropped.append((label, "invalid skill name"))
        elif entry.name in seen:
            dropped.append((label, f"duplicate name; {seen[entry.name]} comes first"))
        elif len(skills) >= MAX_SKILLS:
            dropped.append((label, f"more than {MAX_SKILLS} skills"))
        else:
            # Seen even when rejected: Pi on the host would load this one, never the later duplicate.
            seen[entry.name] = source
            try:
                skills.append(read_skill(entry.name, entry.name, secret_values, dir_fd=base_fd))
            except SkillRejected as error:
                dropped.append((label, error.reason))


def load_staged_skills(directory: Path, secret_values=()) -> list[Skill]:
    """Re-validate a staged `run --skills-dir` directory; any problem fails the run."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Skills directory must be a real directory, not a symlink")
    if stat.S_IMODE(directory.stat().st_mode) & 0o077:
        raise ValueError("Skills directory is accessible by group/others; run chmod 700")
    skills = []
    for index, path in enumerate(sorted(directory.iterdir()), 1):
        label = _label(path.name, index)
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"Unexpected entry in the skills directory: {label}")
        try:
            skills.append(read_skill(path, path.name, secret_values))
        except SkillRejected as error:
            raise ValueError(f"Skill {label} rejected: {error.reason}") from None
    if len(skills) > MAX_SKILLS:
        raise ValueError(f"More than {MAX_SKILLS} skills in the skills directory")
    return skills


def skills_sha256(skills: list[Skill]) -> str:
    """Digest of the approved list (names, per-skill digests and file counts)."""
    records = sorted((skill.record() for skill in skills), key=lambda record: record["name"])
    return hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stage_skills(skills: list[Skill], directory: Path) -> None:
    """Write skills privately (directories 700, files 600; executables 700), never following links."""
    for skill in skills:
        for path, content, executable in skill.files:
            parts = [skill.name, *path.split("/")]
            for depth in range(1, len(parts)):
                folder = directory.joinpath(*parts[:depth])
                if not folder.exists():
                    os.mkdir(folder, 0o700)
                    os.chmod(folder, 0o700)
            target = directory.joinpath(*parts)
            mode = 0o700 if executable else 0o600
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(content)


def _directories(skills: list[Skill]) -> set[str]:
    """Every directory of the merged tree (skill roots included), as paths relative to its root."""
    directories = set()
    for skill in skills:
        for path, _, _ in skill.files:
            parts = f"{skill.name}/{path}".split("/")[:-1]
            directories.update("/".join(parts[:index]) for index in range(1, len(parts) + 1))
    return directories


def skills_archive(skills: list[Skill]) -> bytes:
    """Deterministic root-owned tar for the container: directories 755, files 644 (executables 755)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        entries = [(name, None, True) for name in _directories(skills)]
        entries += [(f"{skill.name}/{path}", content, executable)
                    for skill in skills for path, content, executable in skill.files]
        for name, content, executable in sorted(entries, key=lambda entry: entry[0]):
            info = tarfile.TarInfo(name)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            if content is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(info)
            else:
                info.mode, info.size = (0o755 if executable else 0o644), len(content)
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def skills_tmpfs_kib(skills: list[Skill]) -> int:
    """tmpfs size: page-rounded files plus one page per inode, and 1 MiB of headroom."""
    pages = sum(-(-len(content) // 4096) + 1 for skill in skills for _, content, _ in skill.files)
    return (pages + len(_directories(skills))) * 4 + 1024


def credential_values() -> list[bytes]:
    return [os.environ[name].encode("utf-8", "surrogateescape") for name in CREDENTIAL_ENV
            if len(os.environ.get(name, "")) >= MIN_REDACTED_LENGTH]


def _size(count: int) -> str:
    return f"{count} B" if count < 1024 else f"{count / 1024:.1f} KiB" if count < 1048576 else \
        f"{count / 1048576:.1f} MiB"


def describe_host_skills(skills: list[Skill], dropped: list[tuple[str, str]]) -> list[str]:
    lines = ["pepi-eval: warning: Copy host skills for this run? Usable skills from "
             + " and ".join(label for _, label in HOST_SKILL_SOURCES)
             + " are copied to a private temporary directory (mode 700), mounted read-only for the agent "
               "in the task container, then deleted.",
             "pepi-eval: warning: Each skill's name and description go into the agent's system prompt; any "
             "skill file the agent reads (including scripts) is sent to the model provider and kept, unredacted, "
             "in the run's logs (attempts/*/parent.events.jsonl, attempts/*/artifacts/sessions/).",
             "pepi-eval: warning: A skill holding a value this run redacts (gateway URL/key, auth.json, mcp.json "
             "or forwarded .env values) stops the run before any container starts."]
    for skill in skills:
        scripts = f"scripts: {skill.scripts}" if skill.scripts else "no scripts"
        files = f"{len(skill.files)} file{'s' if len(skill.files) != 1 else ''}"
        lines.append(f"pepi-eval:   copy {skill.name} ({files}, {_size(skill.size)}; {scripts})")
    lines += [f"pepi-eval:   drop {name} ({reason})" for name, reason in dropped]
    return lines


def preview_host_skills() -> int:
    """Print what a host skills copy would contain (names only); stdout: `<count> <sha256>`."""
    try:
        skills, dropped = collect_host_skills(Path.home(), credential_values())
    except (OSError, ValueError) as error:
        print(f"pepi-eval: error: Cannot use host skills ({type(error).__name__}; check the skills directories "
              "and permissions)", file=sys.stderr)
        return 2
    for line in describe_host_skills(skills, dropped):
        print(line, file=sys.stderr)
    print(len(skills), skills_sha256(skills))
    return 0


def main() -> int:
    # Internal adapter for evals/pepi-eval.sh: `--preview` lists names on stderr, `<count> <sha256>` on stdout.
    if sys.argv[1:] != ["--preview"]:
        print("pepi-eval: error: Invalid host-skills adapter arguments", file=sys.stderr)
        return 2
    return preview_host_skills()


if __name__ == "__main__":
    sys.exit(main())
