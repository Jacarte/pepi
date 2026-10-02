"""Freeze selected tasks and a local-only Pepi profile, without model calls."""
from __future__ import annotations

import copy
import hashlib
import re
import shutil
from pathlib import Path

from .common import (DATASET, fingerprint, git_dirty, git_revision, identifier, read_json,
                     redact_url_userinfo, reject_secret_files, runtime_record, sha256_file,
                     version, write_json)

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


KEPT_SETTINGS = ("defaultModel", "defaultProvider", "defaultThinkingLevel", "subagents")
CONFIG_FILES = ("settings.json", "AGENTS.md", "workflows", "extensions/subagent/config.json")


def declares_mcp(settings: dict) -> bool:
    packages = settings.get("packages", [])
    packages = [str(package).lower() for package in packages] if isinstance(packages, list) else []
    litellm = settings.get("litellm")
    litellm_mcp = litellm.get("mcp") if isinstance(litellm, dict) else None
    return (any(re.search(r"(?<![a-z0-9])mcp(?![a-z0-9])", package) for package in packages)
            or any(str(key).lower().startswith("mcp") for key in settings)
            or (isinstance(litellm_mcp, dict) and bool(litellm_mcp.get("enabled"))))


def make_profile(root: Path, build: Path, versions: dict, config_dir: Path | None = None,
                 drop_mcp: bool = False) -> dict:
    """Write the frozen profile; return non-secret provenance for the manifest.

    Each config file comes from `config_dir` when present there, else from `root`.
    Secret files (auth.json, mcp.json, .env) are never read: pass them to `run`.
    """
    if config_dir is not None:
        reject_secret_files(config_dir)
    def source(name: str) -> Path:
        if config_dir is not None and (config_dir / name).exists():
            return config_dir / name
        return root / name
    settings = read_json(source("settings.json"))
    dropped = {"mcp": declares_mcp(settings),
               "packages": [redact_url_userinfo(str(package)) for package in
                            (settings.get("packages") if isinstance(settings.get("packages"), list) else [])],
               "settings_keys": sorted(key for key in settings if key not in KEPT_SETTINGS),
               "agent_skill_overrides": []}
    if dropped["mcp"] and not drop_mcp:
        raise ValueError("The configuration declares MCP, which this runtime does not support "
                         "(stdio servers need npm/network access). Pass --drop-mcp to run without it.")
    # Copy model/role choices, not personal servers, memory, auth or package caches.
    profile = {key: copy.deepcopy(settings[key]) for key in KEPT_SETTINGS if key in settings}
    if profile.get("defaultProvider") != "litellm":
        raise ValueError("This first runtime supports Pepi's LiteLLM provider only")
    for name, override in profile.get("subagents", {}).get("agentOverrides", {}).items():
        if override.pop("skills", None) is not None:  # Personal skills are not in the snapshot.
            dropped["agent_skill_overrides"].append(name)
    profile["packages"] = [f"npm:{name}@{version(versions[name])}" for name in
                           ("pi-provider-litellm", "pi-subagents")]
    profile["litellm"] = {"skills": {"enabled": False}, "mcp": {"enabled": False}}
    destination = build / "profile"
    destination.mkdir(parents=True)
    write_json(destination / "settings.json", profile)
    files = {"settings.json": sha256_file(source("settings.json"))}
    used_injected = source("settings.json").parent == config_dir
    agents = source("AGENTS.md")
    shutil.copyfile(agents, destination / "AGENTS.md")
    files["AGENTS.md"] = sha256_file(agents)
    used_injected = used_injected or agents.parent == config_dir
    workflows = source("workflows")
    if workflows.is_dir():
        shutil.copytree(workflows, destination / "workflows", symlinks=True)
        files["workflows"] = fingerprint(destination / "workflows")
        used_injected = used_injected or workflows.parent == config_dir
    config = source("extensions/subagent/config.json")
    if config.is_file():
        target = destination / "extensions/subagent/config.json"
        target.parent.mkdir(parents=True)
        shutil.copyfile(config, target)
        files["extensions/subagent/config.json"] = sha256_file(config)
        used_injected = used_injected or config_dir is not None and config.is_relative_to(config_dir)
    shutil.copyfile(HERE / "Dockerfile", build / "Dockerfile")
    shutil.copyfile(HERE / "instructions.md", build / "instructions.md")
    return {"source": "injected" if used_injected else "repo", "files": files,
            "dropped_features": dropped}


def prepare_run(root: Path, run: Path, tasks: list[dict], ids: list[str], versions: dict,
                node_image: str, dataset: str = DATASET, task_revision: str | None = None,
                config_dir: Path | None = None, config_label: str | None = None,
                drop_mcp: bool = False) -> dict:
    rows = sanitize_tasks(tasks, ids, dataset)
    for value in versions.values():
        version(value)
    if not node_image.strip() or node_image.startswith("-"):
        raise ValueError("A Node runtime image is required")
    if config_label is not None:
        identifier(config_label)
    identifier(run.name)
    run.mkdir(parents=True, exist_ok=False)
    try:
        config = make_profile(root, run / "build", versions, config_dir, drop_mcp)
        config["label"] = config_label
        write_json(run / "build/runtime.json", runtime_record(versions, node_image, rows))
        digest = fingerprint(run / "build")
        manifest = {
            "schema_version": 1, "run_id": run.name, "config_id": f"pepi-{digest[:12]}",
            "config": config, "dataset": dataset, "split": "test",
            # Revision of the runner code; injected config is described by `config`.
            "pepi_revision": git_revision(HERE), "pepi_dirty": git_dirty(HERE),
            "task_repo_revision": task_revision, "profile_sha256": digest,
            "versions": versions, "node_image": node_image, "tasks": rows,
            "profile_policy": "local-only; keeps model/role config, AGENTS.md and workflows; "
                              "drops MCP, gateway and personal skills, memory and router packages",
        }
        write_json(run / "manifest.json", manifest)
        return manifest
    except BaseException:
        # Only remove the new directory this function created.
        shutil.rmtree(run)
        raise
