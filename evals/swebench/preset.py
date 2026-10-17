"""Frozen metadata-only custom Verified 20-task preset (not an official score)."""
from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

from .common import DATASET, read_json, sha256_file

PRESETS = Path(__file__).parent / "presets"
DEFAULT_IDS = PRESETS / "verified20-v1.ids"
DEFINITION = PRESETS / "verified20-v1.json"
METADATA = PRESETS / "verified20-v1.metadata.json"
IDS_SHA256 = "11fa9243c458222ee0c2dadd3fde52092862bcd927a0895c6551af2de186a3b6"
DEFINITION_SHA256 = "3c03dae52600481ab1e74448c701393685dd57f5002120bc053a53eec3bdb2db"


def tasklist_hash(ids: list[str]) -> str:
    """Canonical ordered IDs: UTF-8, one per line, with a final newline."""
    return hashlib.sha256(("\n".join(ids) + "\n").encode()).hexdigest()


def select_metadata(records: list[dict]) -> list[str]:
    """Reproduce v1 using only the four selection fields; fail on universe drift."""
    rows = [{key: row[key] for key in ("instance_id", "repo", "datasets", "split")}
            for row in records if DATASET in row.get("datasets", []) and row.get("split") == "test"]
    unique = {row["instance_id"] for row in rows}
    if len(rows) != 500 or len(unique) != 500:
        raise ValueError(f"Verified/test universe drift: expected 500 unique IDs; "
                         f"got {len(rows)} records / {len(unique)} unique IDs")
    counts = Counter(row["repo"] for row in rows)
    repos = sorted((repo for repo, count in counts.items() if count >= 4),
                   key=lambda repo: (-counts[repo], repo))[:5]
    if len(repos) != 5:
        raise ValueError("Expected at least five repositories with >=4 tasks")
    result = []
    for repo in repos:
        ids = [row["instance_id"] for row in rows if row["repo"] == repo]
        result.extend(sorted(ids, key=lambda instance_id: (
            hashlib.sha256(f"42\n{repo}\n{instance_id}".encode()).hexdigest(), instance_id))[:4])
    return result


def definition() -> dict:
    if sha256_file(DEFINITION) != DEFINITION_SHA256:
        raise ValueError("Frozen Verified20 definition hash mismatch")
    frozen = read_json(DEFINITION)
    if sha256_file(DEFAULT_IDS) != IDS_SHA256 or frozen["ids_sha256"] != IDS_SHA256:
        raise ValueError("Frozen Verified20 IDs hash mismatch")
    if sha256_file(METADATA) != frozen["metadata_sha256"]:
        raise ValueError("Frozen Verified20 metadata hash mismatch")
    if select_metadata(read_json(METADATA)) != DEFAULT_IDS.read_text().splitlines():
        raise ValueError("Frozen Verified20 selection does not reproduce")
    return frozen


def selection_record(ids: list[str], tasks: list[dict], dataset: str,
                     task_revision: str | None) -> dict:
    record = {"tasklist_sha256": tasklist_hash(ids), "task_repo_revision": task_revision}
    if tasklist_hash(ids) == IDS_SHA256:
        frozen = definition()
        if dataset != DATASET or task_revision != frozen["task_repo_revision"]:
            raise ValueError("Verified20 requires its frozen dataset and task-source revision")
        repos = {row["instance_id"]: row["repo"] for row in read_json(METADATA)}
        if any(task["repo"] != repos[task["instance_id"]] for task in tasks):
            raise ValueError("Verified20 task repository metadata mismatch")
        record["preset"] = frozen
        record["definition_sha256"] = sha256_file(DEFINITION)
    return record


def validate_selection(run: Path, manifest: dict) -> None:
    """Classification must agree with hashed build inputs; old runs remain valid."""
    path = run / "build/selection.json"
    if "selection" not in manifest and not path.exists():
        return
    frozen = read_json(path)
    if manifest.get("selection") != frozen:
        raise ValueError("Manifest selection does not match the prepared build")
    expected = selection_record([task["instance_id"] for task in manifest["tasks"]],
                                manifest["tasks"], manifest["dataset"],
                                manifest.get("task_repo_revision"))
    if frozen != expected or manifest.get("split") != "test":
        raise ValueError("Selection provenance does not match the frozen tasks/source")
