"""Small, dependency-free file and validation helpers."""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

DATASET = "SWE-bench/SWE-bench_Verified"
UID = "10101"


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,149}", value):
        raise ValueError(f"Unsafe identifier: {value!r}")
    return value


def version(value: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", value):
        raise ValueError(f"Use an exact package version, not a range: {value!r}")
    return value


def positive(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Expected a finite positive number")
    return result


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symlinks are not allowed in a profile: {path}")
        if path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def git_revision(directory: Path) -> str | None:
    result = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def read_manifest(run: Path) -> dict:
    manifest = read_json(run / "manifest.json")
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported manifest schema")
    if fingerprint(run / "build") != manifest["profile_sha256"]:
        raise ValueError("Prepared profile was modified; prepare a new run")
    return manifest
