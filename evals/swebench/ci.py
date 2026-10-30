"""GitHub Actions helpers for label-triggered pepi evals (stdlib only).
Only build-wasmtime-images uses Docker/network; the other helpers are offline (except narrate).

Usage: python -m evals.swebench.ci <command> --help

  assemble-config  apply `profile.sh NAME` in a scratch copy and emit a clean PEPI_EVAL_CONFIG_DIR
  cache-key        baseline cache key: pepi-evals-v1-<profile>-<preset>-<hash>-<ISO year-week>
  build-wasmtime-images  build pinned base images with recipe-only contexts and private host receipts
  secrets-setup    write auth.json (from PI_AUTH) into a NEW dir and .env (from EVAL_ENV); masks go to a file
  secrets-cleanup  remove only what secrets-setup wrote (its marker file proves it; always exit 0)
  slim-run         copy the cache, handoff or artifact subset of a run directory
  scan-artifacts   refuse uploads that contain secret values (any encoding) or common token shapes
  verdict          infrastructure-only pass/fail of a baseline/candidate pair, or of one run (--single)
  comment          markdown body for one new PR comment per run
  annotate         `::warning::` (counts only) when the candidate regresses beyond the noise threshold
  narrate          run `evals.swebench.narrate` with a private Pi agent dir; never fails the job

Exit codes: 0 ok, 1 check failed (scan findings, red verdict), 2 usage/configuration error.
Secret values are never printed; errors name variables and files only.

secrets-setup writes `::add-mask::` lines to --mask-file, never to stdout. The workflow must replay
them in its own step (`cat "$MASK_FILE"; rm -f "$MASK_FILE"`) before any later step can print a
value; GitHub only masks values announced on a step's stdout.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .common import read_json
from .prepare import CONFIG_FILES, COOPERATIVE_FILES, REQUIRED_AGENT_FILES
from .task_labels import display_metadata
from .telemetry import JEV_CRITERIA

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
HARNESS_ROOT = Path(__file__).resolve().parents[2]
KEY_PREFIX = "pepi-evals-v1"
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
WEEK_PATTERN = re.compile(r"\d{4}-W\d{2}")
ENV_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
INSTANCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,149}")
SECRET_NAMES = frozenset({"auth.json", "mcp.json", ".env"})
# Values shorter than this are not exact-matched or masked: "1" or "true" would hit every log line.
MIN_SECRET_LENGTH = 8
MIN_MASK_LENGTH = 4

# ---------------------------------------------------------------------------- shared helpers


class UsageError(ValueError):
    pass


def checked_name(value: str, what: str) -> str:
    if not NAME_PATTERN.fullmatch(value or ""):
        raise UsageError(f"Unsafe {what}: use letters, digits, '.', '_' or '-'")
    return value


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def framed(digest, data: bytes) -> None:
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)


def walk_files(root: Path, include) -> list[tuple[str, Path]]:
    """Sorted (relative posix path, path) of regular files; directories pruned by `include(rel, is_dir)`."""
    result = []
    for directory, dirnames, filenames in os.walk(root):
        base = Path(directory)
        relative_dir = base.relative_to(root).as_posix()
        prefix = "" if relative_dir == "." else relative_dir + "/"
        # Symlinked directories are never descended; they are reported as entries instead.
        links = [d for d in dirnames if (base / d).is_symlink()]
        dirnames[:] = sorted(d for d in dirnames if d not in links and include(prefix + d, True))
        for filename in sorted(filenames + links):
            relative = prefix + filename
            if include(relative, False):
                result.append((relative, base / filename))
    return sorted(result)


def hash_files(root: Path, include) -> str:
    """Length-framed SHA-256 of relative paths, file type and bytes (symlinks hash their target text)."""
    digest = hashlib.sha256()
    for relative, path in walk_files(root, include):
        framed(digest, relative.encode())
        if path.is_symlink():
            framed(digest, b"symlink")
            framed(digest, os.readlink(path).encode())
        else:
            mode = path.lstat().st_mode
            framed(digest, b"x" if mode & stat.S_IXUSR else b"f")
            framed(digest, path.read_bytes())
    return digest.hexdigest()


def write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    """New file only (never replaces or follows anything already at the destination)."""
    if path.exists() or path.is_symlink():
        raise UsageError(f"Refusing to write {path}: it already exists")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.chmod(path, mode)


def new_private_dir(path: Path) -> Path:
    """Create a fresh 0700 directory; refuses one that already exists (or a symlink)."""
    if path.exists() or path.is_symlink():
        raise UsageError(f"Refusing {path}: it already exists (secrets-setup needs a new directory)")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkdir(path, 0o700)
    os.chmod(path, 0o700)
    return path


def wipe(path: Path) -> bool:
    """Overwrite then unlink a regular file; best effort. Hard-linked files are only unlinked, so the
    other names keep their content; symlinks are unlinked without touching their target."""
    try:
        if path.is_symlink():
            path.unlink()
            return True
        if not path.is_file():
            return False
        if path.stat().st_nlink > 1:
            path.unlink()
            return True
        size = path.stat().st_size
        with open(path, "r+b") as handle:
            handle.write(b"\0" * size)
            handle.flush()
            os.fsync(handle.fileno())
        path.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------- .env parsing


def dotenv_value(raw: str) -> str:
    """Mirror of pepi-eval.sh dotenv_parse_value; raises ValueError on a malformed value."""
    rest = raw.lstrip()
    if rest.startswith("'"):
        body = rest[1:]
        if "'" not in body:
            raise ValueError("unterminated quote")
        value, rest = body.split("'", 1)
    elif rest.startswith('"'):
        rest, out, closed = rest[1:], [], False
        while rest:
            char, rest = rest[0], rest[1:]
            if char == "\\":
                escapes = {'"': '"', "\\": "\\", "n": "\n"}
                if rest[:1] in escapes:
                    out.append(escapes[rest[0]])
                    rest = rest[1:]
                else:
                    out.append("\\")
            elif char == '"':
                closed = True
                break
            else:
                out.append(char)
        if not closed:
            raise ValueError("unterminated quote")
        value = "".join(out)
    else:
        return re.split(r"\s#", raw, maxsplit=1)[0].strip()
    if not re.fullmatch(r"\s*(#.*)?", rest):
        raise ValueError("text after the closing quote")
    return value


def parse_dotenv(text: str) -> list[tuple[str, str]]:
    """NAME=VALUE pairs with the harness' syntax; errors name the line number only."""
    pairs = []
    for number, line in enumerate(text.split("\n"), 1):
        line = line.lstrip()
        if not line or line.startswith("#"):
            continue
        if "\r" in line or "\0" in line:
            raise UsageError(f"EVAL_ENV line {number}: CR or NUL character (use Unix line endings)")
        if re.match(r"export\s+", line):
            line = line[len("export"):].lstrip()
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)=", line)
        if not match:
            raise UsageError(f"EVAL_ENV line {number}: expected NAME=VALUE")
        try:
            pairs.append((match.group(1), dotenv_value(line[match.end():])))
        except ValueError as error:
            raise UsageError(f"EVAL_ENV line {number}: {error}") from None
    return pairs


def string_leaves(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in string_leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in string_leaves(item)]
    return []


def required_env(variable: str) -> str:
    value = os.environ.get(variable, "")
    if not value.strip():
        raise UsageError(f"{variable} is unset or empty")
    return value


def pi_auth(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError:
        raise UsageError("PI_AUTH is not valid JSON") from None
    if not isinstance(value, dict) or not value:
        raise UsageError("PI_AUTH must be a non-empty JSON object (auth.json content)")
    return value


def auth_values(text: str) -> list[str]:
    try:
        return string_leaves(json.loads(text))
    except ValueError:
        return [text]


def dotenv_values(text: str) -> list[str]:
    try:
        return [value for _, value in parse_dotenv(text)]
    except UsageError:
        return [line.split("=", 1)[1] for line in text.splitlines() if "=" in line]


def secret_values(extra_names=(), secrets_dir: Path | None = None, env_file: Path | None = None,
                  require_source: bool = True) -> list[str]:
    """Exact values worth matching: PI_AUTH/EVAL_ENV from the environment and/or the files secrets-setup
    wrote (--secrets-dir/auth.json, --env-file), plus named variables.

    With require_source, having none of those four sources is a usage error: a scan without the secret
    values would only run the token-shape rules and pass silently."""
    values, sources = [], 0
    if os.environ.get("PI_AUTH", "").strip():
        values += auth_values(os.environ["PI_AUTH"])
        sources += 1
    if os.environ.get("EVAL_ENV", "").strip():
        values += dotenv_values(os.environ["EVAL_ENV"])
        sources += 1
    if secrets_dir is not None:
        auth = secrets_dir / "auth.json"
        if auth.is_symlink() or not auth.is_file():
            raise UsageError("--secrets-dir has no auth.json")
        values += auth_values(auth.read_text(encoding="utf-8", errors="replace"))
        sources += 1
    if env_file is not None:
        if env_file.is_symlink() or not env_file.is_file():
            raise UsageError("--env-file not found")
        values += dotenv_values(env_file.read_text(encoding="utf-8", errors="replace"))
        sources += 1
    if require_source and not sources:
        raise UsageError("no secret values to scan for: set PI_AUTH/EVAL_ENV or pass --secrets-dir/--env-file "
                         "(or --no-secret-values to run only the token-shape rules)")
    values += [os.environ.get(variable, "") for variable in extra_names]
    return sorted({value for value in values if len(value) >= MIN_SECRET_LENGTH})


# ---------------------------------------------------------------------------- assemble-config

SOURCE_FILES = tuple(CONFIG_FILES)


def reject_unsafe_tree(root: Path, label: str) -> None:
    """Mirror of pepi-eval.sh reject_secrets_in_config_dir, plus no symlinks anywhere."""
    for directory, dirnames, filenames in os.walk(root):
        for entry in (*dirnames, *filenames):
            path = Path(directory) / entry
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise UsageError(f"Symlink in {label}: {relative}")
            if entry in SECRET_NAMES:
                raise UsageError(f"Secret file in {label}: {relative}")


def reject_symlinked_path(root: Path, relative: str, label: str) -> None:
    """Every component of ROOT/RELATIVE, parents included: a committed `extensions -> /etc` symlink must
    not make extensions/subagent/config.json read a host file."""
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise UsageError(f"Symlink in {label}: {relative} ({current.relative_to(root).as_posix()})")


def copy_tree_entry(root: Path, relative: str, destination_root: Path, label: str) -> bool:
    """Copy ROOT/RELATIVE (file or directory, no symlinks anywhere) if it exists; True when copied."""
    reject_symlinked_path(root, relative, label)
    source, destination = root / relative, destination_root / relative
    if relative in REQUIRED_AGENT_FILES and not source.is_file():
        raise UsageError(f"Required agent must be an arm-owned regular file: {relative}")
    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        reject_unsafe_tree(source, label)
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copyfile(source, destination)
    return True


# Secrets that must not exist while profile.sh (code from a checkout) runs.
SECRET_ENV_NAMES = ("PI_AUTH", "EVAL_ENV", "WASMTIME_TASKS_TOKEN")


def assemble_config(repo: Path, profile: str, out: Path, fallback_root: Path | None = None,
                    agents_override: Path | None = None) -> dict:
    """Clean config dir for `prepare --config-dir`: the repo's config after `profile.sh PROFILE`.

    profile.sh is the checkout's own code, so it runs with a minimal environment (PATH, a scratch HOME,
    LC_ALL=C) and refuses to run while provider or checkout secrets are in the environment."""
    present = [name for name in SECRET_ENV_NAMES if name in os.environ]
    if present:
        raise UsageError("assemble-config must run before secrets exist; unset " + ", ".join(present))
    checked_name(profile, "profile name")
    override_bytes = None
    if agents_override is not None:
        from .profile import safe_path
        override = agents_override.absolute()
        safe_path(override.parent, override.name)
        if not override.is_file():
            raise UsageError("--agents-override must be an existing regular, non-symlink file")
        override_bytes = override.read_bytes()
    repo = repo.resolve()
    if not (repo / "profile.sh").is_file() or (repo / "profile.sh").is_symlink():
        raise UsageError("profile.sh not found (or a symlink) in --repo")
    profiles = repo / "profiles"
    if not profiles.is_dir() or profiles.is_symlink() or not (profiles / profile).is_dir():
        raise UsageError(f"Unknown profile: {profile}")
    if out.exists() and (out.is_symlink() or not out.is_dir() or any(out.iterdir())):
        raise UsageError("--out must be a new or empty directory")
    with tempfile.TemporaryDirectory(prefix="pepi-ci-config-") as scratch:
        work, home = Path(scratch) / "work", Path(scratch) / "home"
        work.mkdir()
        home.mkdir()
        shutil.copyfile(repo / "profile.sh", work / "profile.sh")
        for relative, path in walk_files(profiles, lambda rel, is_dir: True):
            if path.is_symlink():
                raise UsageError(f"Symlink in profiles/: {relative}")
            (work / "profiles" / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, work / "profiles" / relative)
        for relative in SOURCE_FILES:
            copy_tree_entry(repo, relative, work, "config sources")
        env = {"PATH": os.environ.get("PATH") or os.defpath, "HOME": str(home), "LC_ALL": "C"}
        result = subprocess.run(["bash", str(work / "profile.sh"), profile], cwd=work, env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise UsageError(f"profile.sh {profile} failed (exit {result.returncode}): "
                             f"{result.stderr.strip()[-300:]}")
        out.mkdir(parents=True, exist_ok=True)
        copied = [relative for relative in SOURCE_FILES if copy_tree_entry(work, relative, out, "assembled config")]
    if override_bytes is not None:
        (out / "AGENTS.md").write_bytes(override_bytes)
        if "AGENTS.md" not in copied:
            copied.append("AGENTS.md")
    reject_unsafe_tree(out, "assembled config")
    record = {"profile": profile, "files": copied, "sha256": config_hash(out)}
    if fallback_root is not None:
        # prepare falls back to --pepi-root for files absent from --config-dir; for the baseline that is
        # the PR checkout, so such a file would silently come from the candidate's side.
        record["harness_fallback_files"] = [relative for relative in SOURCE_FILES if relative not in copied
                                            and relative not in COOPERATIVE_FILES
                                            and (fallback_root / relative).exists()]
    return record


def config_hash(directory: Path) -> str:
    return hash_files(directory, lambda rel, is_dir: True)


# ---------------------------------------------------------------------------- Wasmtime base images


def build_wasmtime_images(task_repo: Path, revision: str, out: Path) -> dict:
    """Build only frozen public recipes, never send the evaluator directory to Docker."""
    from .preset import wasmtime_definition
    from .profile import safe_path
    if any(name in os.environ for name in SECRET_ENV_NAMES):
        raise UsageError("build-wasmtime-images must run before secrets exist")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise UsageError("--revision must be the pinned 40-hex WASMTIME_TASKS_REVISION")
    task_repo = task_repo.absolute()
    if not safe_path(task_repo, "sweb.yaml").is_file():
        raise UsageError("evaluator checkout has no regular sweb.yaml")
    try:
        head = subprocess.run(["git", "-C", str(task_repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(task_repo), "status", "--porcelain"],
                               capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError:
        raise UsageError("cannot verify the pinned evaluator Git checkout") from None
    if head != revision or dirty:
        raise UsageError("evaluator checkout must be clean and match WASMTIME_TASKS_REVISION")
    frozen = wasmtime_definition()
    recipes = []
    for instance_id, expected in frozen["tasks"].items():
        directory = task_repo / "tasks" / instance_id
        for name in ("task.yaml", "Dockerfile", ".dockerignore"):
            if not safe_path(directory, name).is_file():
                raise UsageError(f"Wasmtime {instance_id}: missing regular {name}")
        # The prepared task bundle's task.yaml is JSON (a YAML subset).
        metadata = read_json(directory / "task.yaml")
        if not isinstance(metadata, dict):
            raise UsageError(f"Wasmtime {instance_id}: task.yaml must be a JSON object")
        if any(metadata.get(key) != expected[key] for key in ("base_commit", "image")) or (
                metadata.get("instance_id") != instance_id or metadata.get("repo") != frozen["repo"]
                or metadata.get("datasets") != [frozen["dataset"]] or metadata.get("split") != "test"):
            raise UsageError(f"Wasmtime {instance_id}: task metadata differs from the validated preset")
        dockerfile = (directory / "Dockerfile").read_bytes()
        ignore = (directory / ".dockerignore").read_bytes()
        if ignore != b"*\n!Dockerfile\n!.dockerignore\n":
            raise UsageError(f"Wasmtime {instance_id}: .dockerignore must exclude everything except Dockerfile/.dockerignore")
        if re.search(rb"(?im)^\s*(?:ONBUILD\s+)?(?:COPY|ADD)\b", dockerfile):
            raise UsageError(f"Wasmtime {instance_id}: COPY/ADD is forbidden in the base-image recipe")
        if sha256_hex(dockerfile) != expected["dockerfile_sha256"]:
            raise UsageError(f"Wasmtime {instance_id}: Dockerfile differs from the validated recipe")
        recipes.append((instance_id, expected, dockerfile, ignore))
    # Raw build output stays in a private host directory, never stdout or public artifacts.
    new_private_dir(out)
    receipt = {"task_repo_revision": revision, "preset": frozen["id"], "dataset": frozen["dataset"], "images": {}}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pepi-wasmtime-build-") as temporary:
        scratch = Path(temporary)
        env = {"PATH": os.environ.get("PATH") or os.defpath, "HOME": temporary, "LC_ALL": "C"}
        for instance_id, expected, dockerfile, ignore in recipes:
            context = scratch / instance_id
            context.mkdir()
            (context / "Dockerfile").write_bytes(dockerfile)
            (context / ".dockerignore").write_bytes(ignore)
            iid_file = scratch / "image-id"
            iid_file.unlink(missing_ok=True)
            task_started = time.monotonic()
            log_path = out / f"{instance_id}.log"
            write_private(log_path, b"")
            try:
                with log_path.open("ab") as log:
                    subprocess.run(["docker", "build", "--platform", "linux/amd64", "--iidfile", str(iid_file),
                                    "--tag", expected["image"], str(context)], env=env, stdout=log,
                                   stderr=subprocess.STDOUT, check=True)
                    image_id = iid_file.read_text().strip()
                    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                        raise UsageError("Docker did not return an immutable Wasmtime image ID")
                    build_seconds = time.monotonic() - task_started
                    # No provider, mounts, evaluator files or network in the checkout verification container.
                    verify = ('cd /testbed && head=$(git rev-parse HEAD) && test "$head" = "$1" '
                              '&& state=$(git status --porcelain --untracked-files=all) && test -z "$state" '
                              '&& git submodule status --recursive > /tmp/submodule-status '
                              '&& ! grep -q "^[+U-]" /tmp/submodule-status')
                    subprocess.run(["docker", "run", "--rm", "--network", "none", "--platform", "linux/amd64",
                                    "--entrypoint", "/bin/sh", image_id, "-c", verify, "--", expected["base_commit"]],
                                   env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
            except subprocess.CalledProcessError:
                raise UsageError(f"Wasmtime {instance_id}: base-image build/checkout verification failed; see private host log") from None
            receipt["images"][instance_id] = {**expected, "image_id": image_id, "build_seconds": build_seconds,
                                               "verification_seconds": time.monotonic() - task_started - build_seconds}
    receipt["elapsed_seconds"] = time.monotonic() - started
    write_private(out / "receipt.json", (json.dumps(receipt, indent=2) + "\n").encode())
    return {"images_built": len(recipes), "private_receipt": str(out / "receipt.json")}


# ---------------------------------------------------------------------------- cache-key

HARNESS_DIRS = ("evals/swebench", "evals/ci")
HARNESS_FILES = ("evals/pepi-eval.sh", "evals/__init__.py", "profile.sh")
EXCLUDED_DIRS = frozenset({"tests", "runs", "__pycache__"})
# instructions.md is copied into every agent image, so it is harness, not documentation.
HARNESS_MARKDOWN = frozenset({"instructions.md", "AGENTS.md"})


def harness_include(relative: str, is_dir: bool) -> bool:
    leaf = relative.rsplit("/", 1)[-1]
    if is_dir:
        return leaf not in EXCLUDED_DIRS
    # Public display titles do not affect execution or baseline cache identity.
    if leaf == "task-labels.json":
        return False
    if leaf.endswith((".pyc", ".pyo")) or leaf == ".gitignore" or leaf == ".DS_Store":
        return False
    return not leaf.endswith(".md") or leaf in HARNESS_MARKDOWN


def harness_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in HARNESS_FILES:
        path = root / relative
        framed(digest, relative.encode())
        framed(digest, path.read_bytes() if path.is_file() else b"<absent>")
    for relative in HARNESS_DIRS:
        framed(digest, relative.encode())
        directory = root / relative
        framed(digest, hash_files(directory, harness_include).encode() if directory.is_dir() else b"<absent>")
    return digest.hexdigest()


PIN_FIELDS = ("pi_version", "subagents_version", "litellm_version", "node_image", "task_repo_revision",
              "swebench_revision", "python_version", "timeout_seconds", "cpus", "memory", "concurrency",
              "dataset", "ids", "network")
# What evals/ci/run-evals.sh uses when the value is not given, so omitting a flag and passing the
# default give the same key. ids "" means the preset's .ids file, which the harness hash already covers.
PIN_DEFAULTS = {"network": "pepi-eval"}


def pin_value(field: str, value: str) -> str:
    value = value or PIN_DEFAULTS.get(field, "")
    if field == "ids" and value and Path(value).is_file():
        # PEPI_EVAL_IDS may name a file: key on its content, not its path.
        return "file-sha256:" + sha256_hex(Path(value).read_bytes())
    return value


def iso_week(value: str | None) -> str:
    if value is None:
        year, week, _ = datetime.date.today().isocalendar()
        return f"{year}-W{week:02d}"
    if not WEEK_PATTERN.fullmatch(value):
        raise UsageError("--week must look like 2026-W43")
    return value


def cache_key(harness_root: Path, config_dir: Path, profile: str, preset: str, pins: dict,
              settings: dict, week: str | None = None) -> dict:
    checked_name(profile, "profile name")
    checked_name(preset, "preset name")
    if not config_dir.is_dir():
        raise UsageError("--config-dir not found")
    reject_unsafe_tree(config_dir, "config dir")
    components = {"harness": harness_hash(harness_root), "config": config_hash(config_dir),
                  "pins": {**{field: pin_value(field, pins.get(field) or "") for field in PIN_FIELDS}, "preset": preset,
                           "profile": profile, **{f"setting:{k}": v for k, v in sorted(settings.items())}}}
    digest = hashlib.sha256()
    framed(digest, KEY_PREFIX.encode())
    framed(digest, components["harness"].encode())
    framed(digest, components["config"].encode())
    for key in sorted(components["pins"]):
        framed(digest, key.encode())
        framed(digest, str(components["pins"][key]).encode())
    week = iso_week(week)
    combined = digest.hexdigest()
    return {"key": f"{KEY_PREFIX}-{profile}-{preset}-{combined[:24]}-{week}", "week": week,
            "sha256": combined, "components": components}


# ---------------------------------------------------------------------------- secrets


# Sibling suffix: keep cleanup metadata outside the runner's strict auth.json/mcp.json directory.
SECRETS_MARKER = ".pepi-ci-secrets.json"
# The workflow sets the harness' PEPI_EVAL_* settings; in EVAL_ENV they would override them (.env wins
# over the shell in pepi-eval.sh) and make the run differ from its cache key.
RESERVED_ENV_PREFIX = "PEPI_EVAL_"


def secrets_marker_path(directory: Path) -> Path:
    return directory.parent / (directory.name + SECRETS_MARKER)


def secrets_setup(directory: Path, launch_dir: Path, mask_file: Path) -> dict:
    auth_text = required_env("PI_AUTH")
    env_text = required_env("EVAL_ENV")
    auth = pi_auth(auth_text)
    pairs = parse_dotenv(env_text)
    if any(key == "WASMTIME_TASKS_TOKEN" for key, _ in pairs):
        raise UsageError("EVAL_ENV must not contain WASMTIME_TASKS_TOKEN; it belongs only in the evaluator checkout")
    reserved = sorted({key for key, _ in pairs if key.startswith(RESERVED_ENV_PREFIX)})
    if reserved:
        raise UsageError("EVAL_ENV must not set PEPI_EVAL_* (the workflow owns them): " + ", ".join(reserved))
    if not launch_dir.is_dir() or launch_dir.is_symlink():
        raise UsageError("--launch-dir must be an existing directory")
    env_file = launch_dir / ".env"
    marker_file = secrets_marker_path(directory)
    for path, flag in ((env_file, "--launch-dir already holds a .env"), (mask_file, "--mask-file already exists"),
                       (marker_file, "secrets-setup marker already exists")):
        if path.exists() or path.is_symlink():
            raise UsageError(f"Refusing to overwrite: {flag} ({path})")
    env_bytes = (env_text if env_text.endswith("\n") else env_text + "\n").encode()
    auth_bytes = auth_text.encode()
    new_private_dir(directory)
    masks = sorted({value for value in string_leaves(auth) if len(value) >= MIN_SECRET_LENGTH}
                   | {value for _, value in pairs if len(value) >= MIN_MASK_LENGTH})
    # Workflow-command data is percent-decoded by the runner, so encode '%' (and CR) to mask the literal.
    lines = [f"::add-mask::{line.replace('%', '%25').replace(chr(13), '%0D')}"
             for value in masks for line in value.split("\n") if line.strip()]
    # The marker comes first, so cleanup can remove a partial setup.
    info = directory.stat()
    marker = {"version": 2, "secrets_dir": str(directory.resolve()),
              "secrets_dev": info.st_dev, "secrets_ino": info.st_ino,
              "env_file": str(env_file.resolve()), "env_sha256": sha256_hex(env_bytes)}
    write_private(marker_file, (json.dumps(marker, indent=2) + "\n").encode())
    write_private(mask_file, ("\n".join(lines) + "\n").encode() if lines else b"")
    write_private(directory / "auth.json", auth_bytes)
    write_private(env_file, env_bytes)
    return {"auth_json": str(directory / "auth.json"), "env_file": str(env_file),
            "env_names": [key for key, _ in pairs], "masks": len(lines), "mask_file": str(mask_file)}


def read_marker(directory: Path) -> dict | None:
    path = secrets_marker_path(directory)
    if directory.is_symlink() or not directory.is_dir() or path.is_symlink() or not path.is_file():
        return None
    try:
        info, marker_info = directory.stat(), path.stat()
        if (info.st_uid != os.geteuid() or marker_info.st_uid != info.st_uid
                or stat.S_IMODE(marker_info.st_mode) & 0o077):
            return None
        marker = json.loads(path.read_text(encoding="utf-8"))
        # A sibling can outlive its directory; never use it to clean a replacement at the same path.
        if (isinstance(marker, dict) and marker.get("version") == 2
                and marker.get("secrets_dir") == str(directory.resolve())
                and marker.get("secrets_dev") == info.st_dev and marker.get("secrets_ino") == info.st_ino):
            return marker
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    return None


def secrets_cleanup(directory: Path | None, launch_dir: Path | None, mask_file: Path | None) -> dict:
    """Remove only what secrets-setup wrote, as recorded by its marker; the mask file is always wiped."""
    removed, kept = [], []
    if mask_file is not None and wipe(mask_file):
        removed.append(str(mask_file))
    marker = read_marker(directory) if directory is not None else None
    if marker is None:
        if directory is not None and directory.exists():
            kept.append(f"{directory} (no secrets-setup marker; nothing removed)")
        if launch_dir is not None and (launch_dir / ".env").exists():
            kept.append(f"{launch_dir / '.env'} (no secrets-setup marker)")
        return {"removed": removed, "kept": kept}
    env_file = launch_dir / ".env" if launch_dir is not None else None
    if env_file is not None and (env_file.exists() or env_file.is_symlink()):
        written = (not env_file.is_symlink() and env_file.is_file()
                   and str(env_file.resolve()) == marker.get("env_file")
                   and sha256_hex(env_file.read_bytes()) == marker.get("env_sha256"))
        if written and wipe(env_file):
            removed.append(str(env_file))
        elif not written:
            kept.append(f"{env_file} (not the file secrets-setup wrote)")
    for path in (directory / "auth.json", secrets_marker_path(directory)):
        if wipe(path):
            removed.append(str(path))
    try:
        directory.rmdir()
        removed.append(str(directory))
    except OSError:
        kept.append(f"{directory} (not empty)")
    return {"removed": removed, "kept": kept}


# ---------------------------------------------------------------------------- slim-run

CACHE_TOP = frozenset({"summary.json", "summary.csv", "summary.md", "manifest.json", "execution.json",
                       "predictions.jsonl", "grading.json", "grading-strict.json", "grading-restored.json"})
# Kept only in the actions/cache entry (not publicly downloadable). Every uploaded artifact is world-readable
# on a public repository, so the `handoff` (1-day, report job input) and `artifact` (30-day) kinds drop them:
# the manifest carries the frozen config, dropped packages, provider plan and pepi revision; execution.json
# duplicates summary.json's `protocol`. compare/narrate/verdict/comment read only summary.json and attempts.
PRIVATE_TOP = frozenset({"manifest.json", "execution.json"})
CACHE_ATTEMPT = frozenset({"attempt.json", "telemetry.json"})
SLIM_KINDS = ("cache", "handoff", "artifact")
ARTIFACT_TOP = frozenset({"narrative.md", "builds.json"})
ARTIFACT_ATTEMPT = frozenset({"patch.diff"})
GRADING_DIRS = frozenset({"grading", "grading-strict", "grading-restored"})
GRADING_REPORT_FILES = frozenset({"report.json", "test_output.txt", "run_instance.log"})


def slim_include(kind: str):
    artifact = kind == "artifact"
    top = CACHE_TOP if kind == "cache" else CACHE_TOP - PRIVATE_TOP

    def include(relative: str, is_dir: bool) -> bool:
        parts = relative.split("/")
        if is_dir:
            if parts[0] == "attempts":
                # attempts/<id>; never attempts/<id>/artifacts (sessions) or deeper.
                return len(parts) == 1 or len(parts) == 2 and bool(INSTANCE_PATTERN.fullmatch(parts[1]))
            if artifact and parts[0] in GRADING_DIRS:
                # grading*/<run>/logs/evaluation/**; task-repo copies are never entered.
                return len(parts) <= 2 or parts[2] == "logs" and (len(parts) == 3 or parts[3] == "evaluation")
            return False
        if len(parts) == 1:
            return parts[0] in top or artifact and (parts[0] in ARTIFACT_TOP or re.fullmatch(
                r"build-[A-Za-z0-9][A-Za-z0-9_.-]*\.log", parts[0]) is not None)
        if parts[0] == "attempts":
            return len(parts) == 3 and (parts[2] in CACHE_ATTEMPT or artifact and parts[2] in ARTIFACT_ATTEMPT)
        if artifact and parts[0] in GRADING_DIRS:
            if len(parts) == 3:
                return parts[2] == "grader.log"
            return parts[-1] in GRADING_REPORT_FILES
        return False
    return include


def slim_run(run: Path, out: Path, kind: str) -> dict:
    if kind not in SLIM_KINDS:
        raise UsageError("--kind must be one of: " + ", ".join(SLIM_KINDS))
    if not (run / "summary.json").is_file():
        raise UsageError(f"{run} has no summary.json")
    root = run.resolve()
    target = out.resolve()
    if target == root or root in target.parents:
        raise UsageError("--out must be outside the run directory")
    if out.exists() and (out.is_symlink() or not out.is_dir() or any(out.iterdir())):
        raise UsageError("--out must be a new or empty directory")
    # Evaluator logs can echo hidden test patches. Never publish or cache them for Wasmtime.
    from .preset import WASMTIME_DATASET
    private_evaluator = read_json(run / "summary.json").get("dataset") == WASMTIME_DATASET
    include = slim_include(kind)
    if private_evaluator:
        public_top = {"summary.json", "summary.csv", "summary.md", "predictions.jsonl"}
        def include(relative, is_dir):
            parts = relative.split("/")
            if is_dir:
                return parts[0] == "attempts" and (len(parts) == 1 or len(parts) == 2
                                                        and bool(INSTANCE_PATTERN.fullmatch(parts[1])))
            if len(parts) == 1:
                return parts[0] in public_top or kind == "cache" and parts[0] in PRIVATE_TOP
            return (len(parts) == 3 and parts[0] == "attempts"
                    and (parts[2] in CACHE_ATTEMPT or kind == "artifact" and parts[2] == "patch.diff"))
    copied, skipped, size = [], [], 0
    for relative, path in walk_files(root, include):
        if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
            skipped.append(relative)
            continue
        destination = out / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        copied.append(relative)
        size += path.stat().st_size
    return {"kind": kind, "files": len(copied), "bytes": size, "skipped_symlinks": skipped}


# ---------------------------------------------------------------------------- scan-artifacts

# Narrow on purpose: common.SECRET_PATTERN also matches `token: x` / `secret = ...` prose, which SWE-bench
# test logs and patches contain routinely, so it would block every upload.
TOKEN_RULES = (
    ("private-key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY[A-Z ]*-----")),
    ("aws-access-key", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{22,}")),
    ("slack-token", re.compile(rb"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("sk-key", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}")),
    ("bearer-token", re.compile(rb"\b[Bb]earer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("jwt", re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}")),
    ("google-api-key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}")),
    # Mixed case plus a digit or +/ so prose such as "Basic internationalization" does not match.
    ("basic-auth", re.compile(rb"\bBasic\s+(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])"
                              rb"(?=[A-Za-z0-9+/]*[0-9+/])[A-Za-z0-9+/]{20,}={0,2}")),
)


def value_forms(value: str) -> list[bytes]:
    """The value as it may appear in a log: raw, JSON-escaped, percent-encoded or base64 (whole value)."""
    from urllib.parse import quote, quote_plus
    import base64
    raw = value.encode()
    escaped = json.dumps(value)[1:-1]
    forms = {raw, escaped.encode(), json.dumps(value, ensure_ascii=False)[1:-1].encode(),
             escaped.replace("/", "\\/").encode(), quote(value, safe="").encode(), quote_plus(value).encode(),
             base64.b64encode(raw).rstrip(b"="), base64.urlsafe_b64encode(raw).rstrip(b"=")}
    return sorted(form for form in forms if len(form) >= MIN_SECRET_LENGTH)


def scan_artifacts(directory: Path, extra_names=(), secrets_dir: Path | None = None, env_file: Path | None = None,
                   require_source: bool = True) -> list[dict]:
    values = [form for value in secret_values(extra_names, secrets_dir, env_file, require_source)
              for form in value_forms(value)]
    findings = []
    for relative, path in walk_files(directory, lambda rel, is_dir: True):
        if path.is_symlink():
            findings.append({"path": relative, "rules": ["symlink"]})
            continue
        data = path.read_bytes()
        rules = [rule for rule, pattern in TOKEN_RULES if pattern.search(data)]
        if any(value in data for value in values):
            rules.insert(0, "secret-value")
        if rules:
            findings.append({"path": relative, "rules": rules})
    return findings


# ---------------------------------------------------------------------------- verdict / comment

# agent_error counts as infrastructure on purpose, although the harness files it as an agent outcome:
# it means Pi itself exited non-zero (e.g. `Unknown provider "litellm"`, a bad config or credentials),
# so the task got no real attempt and its "unresolved" says nothing about the model. narrate treats it
# the same way. A PR config that breaks Pi therefore turns the job red, which is the intent.
INFRASTRUCTURE_STATUSES = frozenset({"agent_error", "setup_error", "runner_error", "interrupted",
                                     "not_started", "unfinished_children"})
UNKNOWN_GRADES = frozenset({"grading_error", "not_graded"})
NOISE_THRESHOLD = 2


def load_summary(run: Path) -> dict | None:
    try:
        summary = read_json(run / "summary.json")
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(summary, dict) or not isinstance(summary.get("instances"), list):
        return None
    return summary


def instance_ids(rows: list, predicate) -> list[str]:
    return sorted(str(row.get("instance_id")) for row in rows if isinstance(row, dict) and predicate(row))


def verdict(runs: dict, exit_codes: dict, fail_on_timeout: bool = False) -> dict:
    """Infrastructure-only pass/fail for each named run (baseline/candidate, or a single "run")."""
    statuses = INFRASTRUCTURE_STATUSES | ({"timeout"} if fail_on_timeout else set())
    result = {"ok": True, "reasons": [], "infra_tasks": {}, "unknown_grades": {}, "timeouts": {}}
    for side, run in runs.items():
        code = exit_codes.get(side)
        if code not in (None, 0):
            result["reasons"].append(f"{side}: pepi-eval.sh exited {code}")
        summary = load_summary(run)
        if summary is None:
            result["reasons"].append(f"{side}: summary.json missing or invalid")
            result["infra_tasks"][side] = result["unknown_grades"][side] = result["timeouts"][side] = []
            continue
        rows = summary["instances"]
        result["infra_tasks"][side] = instance_ids(rows, lambda row: row.get("agent_status") in statuses)
        result["unknown_grades"][side] = instance_ids(
            rows, lambda row: row.get("resolved") is None or row.get("grading_status") in UNKNOWN_GRADES)
        result["timeouts"][side] = instance_ids(rows, lambda row: row.get("agent_status") == "timeout")
        if result["infra_tasks"][side]:
            result["reasons"].append(f"{side}: {len(result['infra_tasks'][side])} task(s) with infrastructure failures")
        if result["unknown_grades"][side]:
            result["reasons"].append(f"{side}: {len(result['unknown_grades'][side])} task(s) with unknown grades")
    result["ok"] = not result["reasons"]
    return result


ZERO_WIDTH = "\u200b"
COMMENT_LIMIT = 60000
URL_PATTERN = re.compile(r"https://[A-Za-z0-9._~:/?#\[\]@!$&'*+,;=%-]+")
SHA_PATTERN = re.compile(r"[0-9a-f]{7,40}")


def neutralise(text: str) -> str:
    """Model output as inert markdown: no HTML, no @mentions, no issue cross-links, no links or images
    (inline, reference-style, or GitHub's bare http(s)://, ftp:// and www. autolinks)."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"@(?=[A-Za-z0-9_-])", "@" + ZERO_WIDTH, text)
    text = re.sub(r"#(?=\d)", "#" + ZERO_WIDTH, text)
    text = re.sub(r"(?i)\b(https?|ftp):(?=//)", lambda match: match.group(1) + ":" + ZERO_WIDTH, text)
    text = re.sub(r"(?i)\b(www)\.", lambda match: match.group(1) + ZERO_WIDTH + ".", text)
    for pair in ("](", "][", "]:"):
        text = text.replace(pair, pair[0] + ZERO_WIDTH + pair[1])
    return text


def code(value) -> str:
    text = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_.:/@+-]{1,200}", text):
        return "`<invalid>`"
    return f"`{text}`"


def count_cell(summary: dict | None, key: str) -> str:
    if summary is None or key not in summary or summary.get(key) is None:
        return "n/a"
    selected = summary.get("selected")
    return f"{summary[key]}/{selected}" if isinstance(selected, int) else str(summary[key])


def metric_cell(summary: dict | None, key: str, field: str | None = None) -> str:
    from .telemetry import number
    value = summary.get(key) if summary else None
    if field is not None:
        value = value.get(field) if isinstance(value, dict) else None
    value = number(value)
    return f"{value:.4g}" if value is not None else "unavailable"


def comparison(baseline: dict | None, candidate: dict | None) -> tuple[dict | None, str | None]:
    if baseline is None or candidate is None:
        return None, "a summary.json is missing"
    from .reporting import compare
    try:
        result = compare(baseline, candidate)
    except (ValueError, KeyError, TypeError) as error:
        return None, re.sub(r"[^A-Za-z0-9 ,.;:_/()-]", "", str(error))[:200] or "not comparable"
    result["exceeds_noise_threshold"] = (not result["inconclusive"]
                                         and abs(len(result["wins"]) - len(result["regressions"])) > NOISE_THRESHOLD)
    return result, None


ID_LIST_CAP, NAME_LIST_CAP = 30, 20
MAX_REASONS = 10
# Room kept for the narrative wrapper and notes; the head is shrunk (shorter lists) to stay under it.
HEAD_LIMIT = COMMENT_LIMIT - 2000
MIN_NARRATIVE = 200


def task_name(instance_id: str, row: dict | None = None) -> str:
    metadata = display_metadata(instance_id, row)
    label, url = metadata["display_label"], metadata["source_url"]
    if label is None:
        return code(instance_id)
    # Titles are plain text, not Markdown; neutralise HTML, mentions and automatic links too.
    label = re.sub(r"([\\`*_{}\[\]()#+.!|~\-])", r"\\\1", neutralise(label))
    return f"[{label}]({url})" if url else label


def id_list(ids: list, cap: int = ID_LIST_CAP, tasks: dict | None = None) -> str:
    if not ids:
        return "none"
    shown = ", ".join(task_name(item, (tasks or {}).get(item)) for item in ids[:cap])
    more = len(ids) - min(cap, len(ids))
    return (shown + f" (+{more} more)" if shown else f"{more} task(s); see the run artifacts") if more else shown


def jev_score_pct(value) -> str | None:
    if type(value) not in (int, float) or value < 0 or value > 1 \
            or isinstance(value, float) and not math.isfinite(value):
        return None
    return f"{math.floor(value * 1000) / 10:g}%"


def jev_score_table(baseline: dict | None, candidate: dict | None) -> list[str]:
    columns = (("task_coverage", "Coverage"), ("slice_independence", "Slice independence"),
               ("ownership_conflicts", "Ownership conflicts"),
               ("integration_feasibility", "Integration feasibility"))
    rows, omitted = [], 0
    for side, summary in (("baseline", baseline), ("candidate", candidate)):
        for task in (summary or {}).get("instances", []):
            jev = task.get("external_jev") if isinstance(task, dict) else None
            if not isinstance(jev, dict):
                continue
            best = jev.get("best_score")
            attempts = jev.get("score_attempts")
            if not isinstance(attempts, list) and isinstance(best, dict):
                attempts = [best]  # Legacy telemetry only retained the best attempt.
            if not isinstance(attempts, list):
                continue
            instance_id = task.get("instance_id")
            if not isinstance(instance_id, str) or not INSTANCE_PATTERN.fullmatch(instance_id):
                continue
            for attempt in attempts:
                scores = attempt.get("criteria") if isinstance(attempt, dict) else None
                if not isinstance(scores, dict) or set(scores) != JEV_CRITERIA:
                    continue
                attempt_no, outcome = attempt.get("attempt"), attempt.get("outcome")
                if type(attempt_no) is not int or attempt_no < 1 or outcome not in ("accepted", "rejected"):
                    continue
                percentages = [jev_score_pct(scores[key]) for key, _ in columns]
                minimum = jev_score_pct(min(scores[key] for key, _ in columns))
                if any(value is None for value in percentages) or minimum is None:
                    continue
                is_best = isinstance(best, dict) and attempt_no == best.get("attempt") \
                    and scores == best.get("criteria")
                cells = [side, task_name(instance_id, task), str(attempt_no), "yes" if is_best else "", outcome]
                cells.extend(percentages)
                cells.append(minimum)
                if len(rows) < 60:
                    rows.append("| " + " | ".join(cells) + " |")
                else:
                    omitted += 1
    if not rows:
        return []
    headers = ["Arm", "Task", "Attempt", "Best?", "Outcome", *(label for _, label in columns), "Minimum"]
    lines = ["", "<details><summary>Jev criteria scores by attempt</summary>", "",
             "The best attempt is marked; dispatch still requires at least 80% on every criterion.", "",
             "| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|", *rows]
    if omitted:
        lines.append(f"(+{omitted} more score attempt(s) omitted; see the run artifacts.)")
    return [*lines, "", "</details>"]


def name_list(names: list, cap: int = NAME_LIST_CAP) -> str:
    if not names:
        return "none"
    shown = ", ".join(code(name) for name in names[:cap])
    more = len(names) - min(cap, len(names))
    return (shown + f" … and {more} more" if shown else f"{more} name(s); see the run artifacts") if more else shown


def comment_head(baseline: dict | None, candidate: dict | None, result: dict | None, problem: str | None,
                 verdict_result: dict, sha: str, pr: str, run_id: str, run_url: str, profile: str, preset: str,
                 baseline_source: str, id_cap: int, name_cap: int) -> str:
    lines = [f"<!-- pepi-evals pr={pr} sha={sha} run={run_id} -->",
             f"## pepi evals for `{sha[:7]}`",
             "",
             f"Profile {code(profile)}, preset {code(preset)}; baseline = `main` config"
             + (f" ({baseline_source})" if baseline_source in ("cached", "fresh") else "")
             + f", candidate = `pr-{pr}` config, same harness.",
             "",
             "| | baseline (main) | candidate (PR) |",
             "|---|---|---|"]
    for label, key in (("resolved (official)", "resolved"), ("resolved (strict: test edits removed)", "resolved_strict"),
                       ("resolved (test checkout repaired)", "resolved_restored"), ("unknown grades", "unknown_grades")):
        lines.append(f"| {label} | {count_cell(baseline, key)} | {count_cell(candidate, key)} |")
    for label, key, field in (("agent seconds (observed median)", "latency_all_seconds", "median"),
                              ("agent seconds (observed p95)", "latency_all_seconds", "p95"),
                              ("estimated total cost (USD)", "estimated_total_cost_usd", None),
                              ("observed cost (USD, may be partial)", "observed_estimated_cost_usd", None)):
        lines.append(f"| {label} | {metric_cell(baseline, key, field)} | {metric_cell(candidate, key, field)} |")
    lines.append(f"| tasks with full accounting | {count_cell(baseline, 'available_accounting_tasks')} | "
                 f"{count_cell(candidate, 'available_accounting_tasks')} |")
    lines += ["", "Tasks solved is primary; time and cost are secondary. Costs are Pi estimates, not invoices; "
              "unavailable totals are not zero.", ""]
    lines += ["Arms use their own configuration; tool and child counts are context only and do not prove concurrency or causality.", ""]
    tasks = {row["instance_id"]: row for summary in (baseline, candidate)
             for row in (summary or {}).get("instances", [])}
    if result is not None:
        noise = ("**exceeds** the noise threshold" if result["exceeds_noise_threshold"]
                 else f"within noise (needs |wins - regressions| > {NOISE_THRESHOLD} and no inconclusive tasks)")
        lines += [f"- Wins: {id_list(result['wins'], id_cap, tasks)}",
                  f"- Regressions: {id_list(result['regressions'], id_cap, tasks)}",
                  f"- Inconclusive: {id_list(result['inconclusive'], id_cap, tasks)}",
                  f"- Difference: {noise}",
                  f"- Configuration differences (names only): "
                  f"{name_list(sorted(result['configuration_differences']), name_cap)}"]
    else:
        lines.append(f"- Comparison unavailable: {problem}")
    lines.extend(jev_score_table(baseline, candidate))
    for side in ("baseline", "candidate"):
        lines.append(f"- Infrastructure failures ({side}): "
                     f"{id_list(verdict_result.get('infra_tasks', {}).get(side, []), id_cap, tasks)}")
        if verdict_result.get("timeouts", {}).get(side):
            lines.append(f"- Timeouts ({side}, not counted as infrastructure): "
                         f"{id_list(verdict_result['timeouts'][side], id_cap, tasks)}")
    reasons = [re.sub(r"[^A-Za-z0-9 ,.:_()-]", "", str(reason))[:200] for reason in verdict_result["reasons"]]
    if len(reasons) > MAX_REASONS:
        reasons = reasons[:MAX_REASONS] + [f"{len(reasons) - MAX_REASONS} more"]
    lines += [f"- Verdict: {'ok' if verdict_result['ok'] else 'infrastructure problem'}"
              + "".join(f"; {reason}" for reason in reasons),
              ("- Network: preprovisioned self-hosted `pepi-eval`; operator must enforce egress allowlist (deny GitHub)"
               if preset == "wasmtime-safe-v1" else "- Network: unrestricted (plain docker bridge)"),
              f"- Run and artifacts: {run_url}",
              ""]
    return "\n".join(lines)


def comment_body(baseline_run: Path, candidate_run: Path, narrative: str | None, sha: str, pr: str,
                 run_id: str, run_url: str, profile: str, preset: str, baseline_source: str = "",
                 verdict_result: dict | None = None) -> str:
    """Never longer than COMMENT_LIMIT: lists shrink first, then the narrative is truncated or omitted."""
    if not SHA_PATTERN.fullmatch(sha):
        raise UsageError("--sha must be a lowercase hex commit sha")
    if not pr.isdigit() or not run_id.isdigit():
        raise UsageError("--pr and --run-id must be numbers")
    if not URL_PATTERN.fullmatch(run_url):
        raise UsageError("--run-url must be an https URL without spaces or markup")
    checked_name(profile, "profile name")
    checked_name(preset, "preset name")
    baseline, candidate = load_summary(baseline_run), load_summary(candidate_run)
    result, problem = comparison(baseline, candidate)
    verdict_result = verdict_result or verdict({"baseline": baseline_run, "candidate": candidate_run}, {})
    for id_cap, name_cap in ((ID_LIST_CAP, NAME_LIST_CAP), (5, 5), (0, 0)):
        head = comment_head(baseline, candidate, result, problem, verdict_result, sha, pr, run_id, run_url,
                            profile, preset, baseline_source, id_cap, name_cap)
        if len(head) <= HEAD_LIMIT:
            break
    if len(head) > HEAD_LIMIT:
        # Only reachable with pathological reasons/ids; keep the marker line and the run link.
        note = f"\n- _… report truncated (comment size limit); see {run_url}_\n"
        head = head[:HEAD_LIMIT - len(note)].rsplit("\n", 1)[0] + note
    tail = "\n\n_Narrative: AI interpretation of the summary data, not a grade._\n"
    if narrative is None or not narrative.strip():
        return head + "\n**Narrative unavailable.**\n"
    quoted = "\n".join("> " + line if line else ">" for line in neutralise(narrative.strip()).split("\n"))
    opening, closing = "<details open><summary>Narrative</summary>\n\n", "\n\n</details>"
    truncation = "\n>\n> _… narrative truncated ({} characters omitted); see the run artifacts._"
    budget = COMMENT_LIMIT - len(head) - 1 - len(opening) - len(closing) - len(tail) - len(truncation) - 20
    if budget < MIN_NARRATIVE:
        return head + "\n**Narrative omitted (comment size limit); see the run artifacts.**\n"
    if len(quoted) > budget:
        omitted = len(quoted) - budget
        quoted = quoted[:budget].rsplit("\n", 1)[0] + truncation.format(omitted)
    return head + "\n" + opening + quoted + closing + tail


def regression_annotation(baseline_run: Path, candidate_run: Path) -> str | None:
    """`::warning::` line (counts only, no task ids) when the official score regresses beyond the noise
    threshold, else None. Never raises: an incomparable pair simply gets no annotation."""
    try:
        baseline, candidate = load_summary(baseline_run), load_summary(candidate_run)
        result, _ = comparison(baseline, candidate)
        if result is None or not result["exceeds_noise_threshold"]:
            return None
        wins, regressions = len(result["wins"]), len(result["regressions"])
        if regressions <= wins:
            return None
        return (f"::warning title=pepi evals::candidate regressions beyond the noise threshold: "
                f"{regressions} regression(s), {wins} win(s) (|wins - regressions| > {NOISE_THRESHOLD}); "
                f"official resolved {int(baseline['resolved'])} -> {int(candidate['resolved'])}. See the PR comment.")
    except Exception:  # noqa: BLE001 - an annotation must never fail the job
        return None


# ---------------------------------------------------------------------------- narrate

NARRATE_SETTINGS = ("defaultProvider", "defaultModel", "defaultThinkingLevel")


def narrate_settings(config_dir: Path, litellm_version: str | None) -> dict:
    settings = read_json(config_dir / "settings.json")
    if not isinstance(settings, dict):
        raise UsageError("settings.json in --config-dir is not an object")
    result = {key: settings[key] for key in NARRATE_SETTINGS if key in settings}
    packages = []
    for package in settings.get("packages", []) if isinstance(settings.get("packages"), list) else []:
        # Only model providers: narrate runs with --no-tools/--no-skills, so tool packages are dead weight.
        if isinstance(package, str) and "pi-provider-" in package:
            if package == "npm:pi-provider-litellm" and litellm_version:
                package = f"npm:pi-provider-litellm@{litellm_version}"
            packages.append(package)
    if packages:
        result["packages"] = packages
    return result


def run_narrate(args) -> dict:
    """Never raises for narrate problems: any failure is reported as {"narrative": "unavailable"}."""
    output = args.output
    status, detail = None, ""
    try:
        if output.exists() or output.is_symlink():
            output.unlink()
        env_values = dict(parse_dotenv(args.env_file.read_text(encoding="utf-8"))) if args.env_file else {}
        with tempfile.TemporaryDirectory(prefix="pepi-ci-narrate-") as scratch:
            agent_dir = new_private_dir(Path(scratch) / "agent")
            try:
                auth = args.secrets_dir / "auth.json"
                if not auth.is_file() or auth.is_symlink():
                    raise UsageError("auth.json not found in --secrets-dir")
                write_private(agent_dir / "auth.json", auth.read_bytes())
                settings = narrate_settings(args.config_dir, args.litellm_version)
                write_private(agent_dir / "settings.json", (json.dumps(settings, indent=2) + "\n").encode())
                if (args.config_dir / "models.json").is_file():
                    from .profile import safe_models
                    models = safe_models(args.config_dir / "models.json")
                    write_private(agent_dir / "models.json", (json.dumps(models, indent=2) + "\n").encode())
                env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
                env.update(HOME=scratch, PI_CODING_AGENT_DIR=str(agent_dir), PYTHONDONTWRITEBYTECODE="1",
                           PYTHONPATH=str(HARNESS_ROOT))
                for key in ("LITELLM_BASE_URL", "LITELLM_API_KEY"):
                    value = env_values.get(key) or os.environ.get(key)
                    if value:
                        env[key] = value
                command = [sys.executable, "-m", "evals.swebench.narrate", str(args.baseline.resolve()),
                           str(args.candidate.resolve()), "--yes", "--force", "--output", str(output.resolve()),
                           "--pi-command", args.pi_command, "--timeout-seconds", str(args.timeout_seconds)]
                if args.model:
                    command += ["--model", args.model]
                completed = subprocess.run(command, env=env, cwd=HARNESS_ROOT, stdin=subprocess.DEVNULL,
                                           capture_output=True, text=True,
                                           timeout=args.timeout_seconds + 120, check=False)
                status = completed.returncode
                lines = completed.stderr.strip().splitlines()
                detail = lines[-1][:200] if lines else ""
            finally:
                for child in agent_dir.glob("*"):
                    wipe(child)
    except subprocess.TimeoutExpired:
        detail = "timed out"
    except (UsageError, ValueError, OSError) as error:
        detail = str(error)[:200]
    if status != 0 or not output.is_file():
        try:
            if output.is_file():
                output.unlink()
        except OSError:
            pass
        return {"narrative": "unavailable", "exit_code": status, "detail": detail}
    return {"narrative": str(output), "exit_code": 0}


# ---------------------------------------------------------------------------- CLI


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m evals.swebench.ci", description=__doc__,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = root.add_subparsers(dest="command", required=True)

    command = commands.add_parser("assemble-config", help="clean PEPI_EVAL_CONFIG_DIR for one checkout")
    command.add_argument("--repo", type=Path, required=True, help="checkout containing profile.sh and profiles/")
    command.add_argument("--profile", required=True)
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--fallback-root", type=Path,
                         help="harness checkout (--pepi-root): config files it would supply instead are an error")
    command.add_argument("--agents-override", type=Path,
                         help="regular non-symlink file replacing AGENTS.md after profile assembly; included in hash")
    command.add_argument("--allow-harness-fallback", action="store_true",
                         help="only warn when --fallback-root would supply missing config files")

    command = commands.add_parser("build-wasmtime-images", help="build pinned evaluator base images without oracle inputs")
    command.add_argument("--task-repo", type=Path, required=True)
    command.add_argument("--revision", required=True)
    command.add_argument("--out", type=Path, required=True, help="new private host directory for logs and receipt")

    command = commands.add_parser("cache-key", help="baseline cache key")
    command.add_argument("--harness-root", type=Path, default=HARNESS_ROOT)
    command.add_argument("--config-dir", type=Path, required=True, help="assembled baseline (main) config dir")
    command.add_argument("--profile", required=True)
    command.add_argument("--preset", required=True)
    for field in PIN_FIELDS:
        command.add_argument("--" + field.replace("_", "-"), dest=field, default="")
    command.add_argument("--setting", action="append", default=[], metavar="NAME=VALUE",
                         help="extra measurement setting folded into the key (repeatable)")
    command.add_argument("--week", help="ISO year-week, e.g. 2026-W43 (default: current week)")
    command.add_argument("--json", action="store_true", help="print the components and sub-hashes")

    command = commands.add_parser("secrets-setup", help="auth.json from PI_AUTH, .env from EVAL_ENV")
    command.add_argument("--dir", type=Path, required=True, help="private secrets dir (PEPI_EVAL_SECRETS_DIR)")
    command.add_argument("--launch-dir", type=Path, required=True, help="directory pepi-eval.sh is started from")
    command.add_argument("--mask-file", type=Path, required=True, help="receives ::add-mask:: lines (not stdout)")

    command = commands.add_parser("secrets-cleanup", help="remove secrets-setup output")
    command.add_argument("--dir", type=Path)
    command.add_argument("--launch-dir", type=Path)
    command.add_argument("--mask-file", type=Path)

    command = commands.add_parser("slim-run", help="copy the cache, handoff or artifact subset of a run")
    command.add_argument("--run", type=Path, required=True)
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--kind", choices=SLIM_KINDS, required=True,
                         help="cache: actions/cache entry; handoff: report-job input (public 1-day artifact, "
                              "no manifest/execution); artifact: public 30-day artifact")

    command = commands.add_parser("scan-artifacts", help="refuse secret values and token shapes")
    command.add_argument("--dir", type=Path, required=True)
    command.add_argument("--secrets-dir", type=Path, help="read secret values from DIR/auth.json (secrets-setup --dir)")
    command.add_argument("--env-file", type=Path, help="read secret values from this .env (secrets-setup output)")
    command.add_argument("--extra-env", action="append", default=[], metavar="NAME",
                         help="also match this variable's value exactly (repeatable)")
    command.add_argument("--no-secret-values", action="store_true",
                         help="allow a scan with no PI_AUTH/EVAL_ENV/--secrets-dir/--env-file (token shapes only)")

    command = commands.add_parser("verdict", help="infrastructure-only pass/fail (a pair, or --single RUN)")
    command.add_argument("--baseline", type=Path)
    command.add_argument("--candidate", type=Path)
    command.add_argument("--exit-code-baseline", type=int)
    command.add_argument("--exit-code-candidate", type=int)
    command.add_argument("--single", type=Path, metavar="RUN", help="verdict of one run (e.g. may it be cached?)")
    command.add_argument("--exit-code", type=int, help="with --single: that run's pepi-eval.sh exit code")
    command.add_argument("--fail-on-timeout", action="store_true", help="also fail on agent timeouts")
    command.add_argument("--out", type=Path, help="also write the JSON here")

    command = commands.add_parser("comment", help="PR comment markdown")
    command.add_argument("--baseline", type=Path, required=True)
    command.add_argument("--candidate", type=Path, required=True)
    command.add_argument("--narrative", required=True, help="narrative.md path, or 'none'")
    command.add_argument("--sha", required=True)
    command.add_argument("--pr", required=True)
    command.add_argument("--run-url", required=True)
    command.add_argument("--run-id", help="default: the last path component of --run-url")
    command.add_argument("--profile", required=True)
    command.add_argument("--preset", required=True)
    command.add_argument("--baseline-source", choices=("cached", "fresh"), default="")
    command.add_argument("--verdict", type=Path, help="JSON written by `verdict --out`")
    command.add_argument("--out", type=Path, required=True)

    command = commands.add_parser("annotate", help="::warning:: (counts only) on regressions beyond noise; exit 0")
    command.add_argument("--baseline", type=Path, required=True)
    command.add_argument("--candidate", type=Path, required=True)

    command = commands.add_parser("narrate", help="narrate with a private Pi agent dir; exit 0 even on failure")
    command.add_argument("--baseline", type=Path, required=True)
    command.add_argument("--candidate", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--secrets-dir", type=Path, required=True)
    command.add_argument("--config-dir", type=Path, required=True, help="candidate config dir (provider/model)")
    command.add_argument("--env-file", type=Path, help=".env holding LITELLM_BASE_URL/LITELLM_API_KEY")
    command.add_argument("--model")
    command.add_argument("--pi-command", default="pi")
    command.add_argument("--litellm-version")
    command.add_argument("--timeout-seconds", type=float, default=300)
    return root


def emit(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def main(argv: list | None = None) -> int:
    try:
        args = parser().parse_args(argv)
    except SystemExit as exit_:
        return EXIT_OK if exit_.code in (0, None) else EXIT_USAGE
    try:
        if args.command == "assemble-config":
            record = assemble_config(args.repo, args.profile, args.out, args.fallback_root, args.agents_override)
            fallback = record.get("harness_fallback_files")
            if fallback:
                # For the baseline this means main's run would silently use the PR's copy of these files.
                level = "warning" if args.allow_harness_fallback else "error"
                print(f"pepi-ci: {level}: config files absent here would come from the harness checkout: "
                      + ", ".join(fallback), file=sys.stderr)
            emit(record)
            if fallback and not args.allow_harness_fallback:
                return EXIT_FAILED
        elif args.command == "build-wasmtime-images":
            emit(build_wasmtime_images(args.task_repo, args.revision, args.out))
        elif args.command == "cache-key":
            settings = {}
            for item in args.setting:
                key, separator, value = item.partition("=")
                if not separator or not ENV_NAME_PATTERN.fullmatch(key):
                    raise UsageError("--setting must be NAME=VALUE")
                settings[key] = value
            result = cache_key(args.harness_root, args.config_dir, args.profile, args.preset,
                               {field: getattr(args, field) for field in PIN_FIELDS}, settings, args.week)
            emit(result) if args.json else print(result["key"])
        elif args.command == "secrets-setup":
            emit(secrets_setup(args.dir, args.launch_dir, args.mask_file))
        elif args.command == "secrets-cleanup":
            emit(secrets_cleanup(args.dir, args.launch_dir, args.mask_file))
        elif args.command == "slim-run":
            emit(slim_run(args.run, args.out, args.kind))
        elif args.command == "scan-artifacts":
            for variable in args.extra_env:
                if not ENV_NAME_PATTERN.fullmatch(variable):
                    raise UsageError("--extra-env takes variable names")
            if not args.dir.is_dir():
                raise UsageError("--dir not found")
            findings = scan_artifacts(args.dir, args.extra_env, args.secrets_dir, args.env_file,
                                      require_source=not args.no_secret_values)
            for finding in findings:
                print(f"pepi-ci: secret scan: {finding['path']}: {', '.join(finding['rules'])}", file=sys.stderr)
            emit({"findings": findings})
            return EXIT_FAILED if findings else EXIT_OK
        elif args.command == "verdict":
            pair = (args.baseline, args.candidate, args.exit_code_baseline, args.exit_code_candidate)
            if args.single is not None:
                if any(value is not None for value in pair):
                    raise UsageError("--single excludes --baseline/--candidate/--exit-code-baseline/--exit-code-candidate")
                runs, codes = {"run": args.single}, {"run": args.exit_code}
            else:
                if args.baseline is None or args.candidate is None or args.exit_code is not None:
                    raise UsageError("verdict needs --baseline and --candidate, or --single RUN [--exit-code N]")
                runs = {"baseline": args.baseline, "candidate": args.candidate}
                codes = {"baseline": args.exit_code_baseline, "candidate": args.exit_code_candidate}
            result = verdict(runs, codes, args.fail_on_timeout)
            if args.out:
                args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            emit(result)
            return EXIT_OK if result["ok"] else EXIT_FAILED
        elif args.command == "comment":
            narrative = None
            if args.narrative != "none":
                path = Path(args.narrative)
                narrative = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
            run_id = args.run_id or args.run_url.rstrip("/").rsplit("/", 1)[-1]
            given = read_json(args.verdict) if args.verdict else None
            body = comment_body(args.baseline, args.candidate, narrative, args.sha, args.pr, run_id, args.run_url,
                                args.profile, args.preset, args.baseline_source, given)
            args.out.write_text(body, encoding="utf-8")
            print(f"pepi-ci: wrote {len(body)} characters to {args.out}", file=sys.stderr)
        elif args.command == "annotate":
            line = regression_annotation(args.baseline, args.candidate)
            if line:
                print(line)
        elif args.command == "narrate":
            result = run_narrate(args)
            if result["narrative"] == "unavailable":
                print(f"pepi-ci: narrative unavailable (exit {result['exit_code']}): {result.get('detail', '')}",
                      file=sys.stderr)
            emit(result)
    except (UsageError, ValueError, OSError) as error:
        print(f"pepi-ci: error: {error}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
