"""Frozen custom task presets (not official scores): metadata-only Verified20 v1 and the
imported-task Multilingual10 v1 (Niffler pilot)."""
from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

from .common import DATASET, fingerprint, identifier, read_json, sha256_file

PRESETS = Path(__file__).parent / "presets"
DEFAULT_IDS = PRESETS / "verified20-v1.ids"
DEFINITION = PRESETS / "verified20-v1.json"
METADATA = PRESETS / "verified20-v1.metadata.json"
IDS_SHA256 = "11fa9243c458222ee0c2dadd3fde52092862bcd927a0895c6551af2de186a3b6"
DEFINITION_SHA256 = "3c03dae52600481ab1e74448c701393685dd57f5002120bc053a53eec3bdb2db"
MULTILINGUAL_IDS = PRESETS / "multilingual10-v1.ids"
MULTILINGUAL_DEFINITION = PRESETS / "multilingual10-v1.json"
MULTILINGUAL_IDS_SHA256 = "d40e0a3d723e96d5bab7f6844ca70930fa9c2ace79095642c6e30a3a92b143ac"
MULTILINGUAL_DEFINITION_SHA256 = "a9339399f67a28eb704ef6e0686af1a8ab35796279bc0285f072cd13212eead8"
DEFAULT_PRESET = "verified20-v1"
PRESET_NAMES = (DEFAULT_PRESET, "multilingual10-v1")


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


def multilingual_definition() -> dict:
    """Multilingual10 pins its task source by per-task input fingerprints of an import of the pinned
    dataset revision (the analogue of Verified20's task_repo_revision; no metadata universe)."""
    if sha256_file(MULTILINGUAL_DEFINITION) != MULTILINGUAL_DEFINITION_SHA256:
        raise ValueError("Frozen Multilingual10 definition hash mismatch")
    frozen = read_json(MULTILINGUAL_DEFINITION)
    if sha256_file(MULTILINGUAL_IDS) != MULTILINGUAL_IDS_SHA256 or frozen["ids_sha256"] != MULTILINGUAL_IDS_SHA256:
        raise ValueError("Frozen Multilingual10 IDs hash mismatch")
    if list(frozen["task_fingerprints"]) != MULTILINGUAL_IDS.read_text().splitlines():
        raise ValueError("Frozen Multilingual10 task fingerprints do not match its IDs")
    return frozen


def preset_ids(name: str) -> Path:
    if name not in PRESET_NAMES:
        raise ValueError(f"Unknown preset: {name!r}; choose one of {', '.join(PRESET_NAMES)}")
    return DEFAULT_IDS if name == DEFAULT_PRESET else MULTILINGUAL_IDS


def preset_definition(name: str) -> dict:
    preset_ids(name)
    return definition() if name == DEFAULT_PRESET else multilingual_definition()


def preset_dataset(name: str) -> str:
    return preset_definition(name)["dataset"]


def requires_task_inputs(ids: list[str]) -> bool:
    """Selections whose preset pins imported task inputs (computed only for them)."""
    return tasklist_hash(ids) == MULTILINGUAL_IDS_SHA256


def task_input_fingerprints(task_repo: Path, ids: list[str]) -> dict:
    """Per task: sha256 of sweb.yaml's hash and the tasks/<id> fingerprint (as reporting computes)."""
    shared = sha256_file(task_repo / "sweb.yaml")
    return {instance_id: hashlib.sha256(
        f"{shared}\0{fingerprint(task_repo / 'tasks' / identifier(instance_id))}".encode()).hexdigest()
        for instance_id in ids}


def selection_record(ids: list[str], tasks: list[dict], dataset: str,
                     task_revision: str | None, task_inputs: dict | None = None) -> dict:
    record = {"tasklist_sha256": tasklist_hash(ids), "task_repo_revision": task_revision}
    if tasklist_hash(ids) == MULTILINGUAL_IDS_SHA256:
        frozen = multilingual_definition()
        if dataset != frozen["dataset"]:
            raise ValueError("Multilingual10 requires its frozen dataset")
        changed = [instance_id for instance_id in ids
                   if (task_inputs or {}).get(instance_id) != frozen["task_fingerprints"][instance_id]]
        if changed:
            raise ValueError("Multilingual10 task inputs differ from the frozen import "
                             f"(re-import revision {frozen['dataset_revision']}): {' '.join(changed)}")
        record["preset"] = frozen
        record["definition_sha256"] = sha256_file(MULTILINGUAL_DEFINITION)
    elif tasklist_hash(ids) == IDS_SHA256:
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
                                manifest.get("task_repo_revision"), manifest.get("task_input_fingerprints"))
    if frozen != expected or manifest.get("split") != "test":
        raise ValueError("Selection provenance does not match the frozen tasks/source")
    # The manifest's copy of the preset's host names must be the hashed selection's.
    if manifest.get("network_requirements", []) != (frozen.get("preset") or {}).get("network_requirements", []):
        raise ValueError("Manifest network requirements do not match the frozen preset")
