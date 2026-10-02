"""Freeze selected tasks and a local-only Pepi profile, without model calls."""
from __future__ import annotations

import copy
import hashlib
import re
import shutil
from pathlib import Path

from .common import DATASET, fingerprint, git_revision, identifier, read_json, version, write_json

HERE = Path(__file__).parent


def selected_ids(path: Path) -> list[str]:
    ids = [identifier(line.strip()) for line in path.read_text().split("\n")
           if line.strip() and not line.lstrip().startswith("#")]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Select at least one task; duplicate IDs are not allowed")
    return ids


def sanitize_tasks(tasks: list[dict], ids: list[str], dataset: str = DATASET) -> list[dict]:
    by_id = {task["instance_id"]: task for task in tasks}
    if len(by_id) != len(tasks) or set(by_id) != set(ids):
        raise ValueError("Loaded tasks do not match the selection exactly")
    result = []
    for instance_id in ids:
        identifier(instance_id)
        task = by_id[instance_id]
        if not re.fullmatch(r"[0-9a-f]{40}", str(task.get("base_commit", ""))):
            raise ValueError(f"Invalid task base commit: {instance_id}")
        if dataset not in task.get("datasets", []) or task.get("split") != "test":
            raise ValueError(f"{instance_id} is not in the selected dataset's test split")
        # Explicit allowlist: never serialize gold patches or grading tests.
        row = {key: task[key] for key in
               ("instance_id", "repo", "base_commit", "image", "problem_statement")}
        if not all(isinstance(value, str) and value.strip() for value in row.values()):
            raise ValueError(f"Incomplete task: {instance_id}")
        row["problem_sha256"] = hashlib.sha256(row["problem_statement"].encode()).hexdigest()
        result.append(row)
    return result


def make_profile(root: Path, build: Path, versions: dict) -> None:
    settings = read_json(root / "settings.json")
    # Copy model/role choices, not personal servers, memory, auth or package caches.
    profile = {key: copy.deepcopy(settings[key]) for key in
               ("defaultModel", "defaultProvider", "defaultThinkingLevel", "subagents")
               if key in settings}
    if profile.get("defaultProvider") != "litellm":
        raise ValueError("This first runtime supports Pepi's LiteLLM provider only")
    for override in profile.get("subagents", {}).get("agentOverrides", {}).values():
        override.pop("skills", None)  # Personal skills are not in the snapshot.
    profile["packages"] = [f"npm:{name}@{version(versions[name])}" for name in
                           ("pi-provider-litellm", "pi-subagents")]
    profile["litellm"] = {"skills": {"enabled": False}, "mcp": {"enabled": False}}
    destination = build / "profile"
    destination.mkdir(parents=True)
    write_json(destination / "settings.json", profile)
    shutil.copyfile(root / "AGENTS.md", destination / "AGENTS.md")
    if (root / "workflows").is_dir():
        shutil.copytree(root / "workflows", destination / "workflows", symlinks=True)
    config = root / "extensions/subagent/config.json"
    if config.is_file():
        target = destination / "extensions/subagent/config.json"
        target.parent.mkdir(parents=True)
        shutil.copyfile(config, target)
    shutil.copyfile(HERE / "Dockerfile", build / "Dockerfile")
    shutil.copyfile(HERE / "instructions.md", build / "instructions.md")


def prepare_run(root: Path, run: Path, tasks: list[dict], ids: list[str], versions: dict,
                node_image: str, dataset: str = DATASET, task_revision: str | None = None) -> dict:
    rows = sanitize_tasks(tasks, ids, dataset)
    for value in versions.values():
        version(value)
    if not node_image.strip() or node_image.startswith("-"):
        raise ValueError("A Node runtime image is required")
    identifier(run.name)
    run.mkdir(parents=True, exist_ok=False)
    try:
        make_profile(root, run / "build", versions)
        write_json(run / "build/runtime.json", {"versions": versions, "node_image": node_image})
        digest = fingerprint(run / "build")
        manifest = {
            "schema_version": 1, "run_id": run.name, "config_id": f"pepi-{digest[:12]}",
            "dataset": dataset, "split": "test", "pepi_revision": git_revision(root),
            "task_repo_revision": task_revision, "profile_sha256": digest,
            "versions": versions, "node_image": node_image, "tasks": rows,
            "profile_policy": "local-only; no personal memory, MCP, web, router or skills",
        }
        write_json(run / "manifest.json", manifest)
        return manifest
    except BaseException:
        # Only remove the new directory this function created.
        shutil.rmtree(run)
        raise
