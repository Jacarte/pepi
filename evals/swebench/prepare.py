"""Freeze selected tasks and an isolated Pepi profile, without model calls."""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

from .common import (DATASET, fingerprint, git_dirty, git_revision, identifier, read_json,
                     redact_url_userinfo, runtime_record, sha256_file,
                     version, write_json)

from .mcp import freeze_mcp
from .profile import config_source, isolated_settings, safe_models
from .preset import selection_record

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
        row = {key: task[key] for key in
               ("instance_id", "repo", "base_commit", "image", "problem_statement")}
        if not all(isinstance(value, str) and value.strip() for value in row.values()):
            raise ValueError(f"Incomplete task: {instance_id}")
        row["problem_sha256"] = hashlib.sha256(row["problem_statement"].encode()).hexdigest()
        result.append(row)
    return result


KEPT_SETTINGS = ("defaultModel", "defaultProvider", "defaultThinkingLevel", "subagents")
CONFIG_FILES = ("settings.json", "models.json", "AGENTS.md", "workflows", "extensions/subagent/config.json",
                "mcp-packages.json", "mcp-package-lock.json")


def make_profile(root: Path, build: Path, versions: dict, config_dir: Path | None = None,
                 drop_mcp: bool = False, profile_name: str | None = None) -> dict:
    """Write the frozen profile; return non-secret provenance for the manifest.

    Files use injected > named profile > repo precedence; named models never use repo fallback.
    Secret files (auth.json, mcp.json, .env) are never read: pass them to `run`.
    """
    source = config_source(root, config_dir, profile_name)
    settings = read_json(source("settings.json"))
    profile, plan = isolated_settings(settings, versions)
    mcp_policy, mcp_files = freeze_mcp(source, build, versions["pi"], drop_mcp)
    dropped = {"mcp": drop_mcp,
               "packages": [redact_url_userinfo(str(package)) for package in
                            (settings.get("packages") if isinstance(settings.get("packages"), list) else [])],
               "settings_keys": sorted(key for key in settings if key not in KEPT_SETTINGS),
               "agent_skill_overrides": []}
    # Copy model/role choices, not personal servers, memory, auth or package caches.
    for name, override in settings.get("subagents", {}).get("agentOverrides", {}).items():
        if "skills" in override:
            dropped["agent_skill_overrides"].append(name)
    if drop_mcp:
        profile["extensions"] = ["-builtin:mcp"]
    destination = build / "profile"
    destination.mkdir(parents=True)
    write_json(destination / "settings.json", profile)
    write_json(build / "provider-plan.json", plan)
    models = source("models.json")
    if models.is_file():
        write_json(destination / "models.json", safe_models(models))
    files = {"settings.json": sha256_file(source("settings.json")), **mcp_files}
    if models.is_file():
        files["models.json"] = sha256_file(models)
    used_injected = (
        models.is_file() and config_dir is not None and models.is_relative_to(config_dir)
        or source("settings.json").parent == config_dir
        or bool(mcp_files) and source("mcp-packages.json").parent == config_dir)
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
    extension = destination / "extensions/eval-telemetry.ts"
    extension.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / "eval-telemetry.ts", extension)
    shutil.copyfile(HERE / "Dockerfile", build / "Dockerfile")
    shutil.copyfile(HERE / "auth-audit.cjs", build / "auth-audit.cjs")
    shutil.copyfile(HERE / "install-mcp.mjs", build / "install-mcp.mjs")
    shutil.copyfile(HERE / "instructions.md", build / "instructions.md")
    return {"source": "injected" if used_injected else "profile" if profile_name else "repo",
            "profile": profile_name, "files": files,
            "dropped_features": dropped, "mcp": mcp_policy}


def prepare_run(root: Path, run: Path, tasks: list[dict], ids: list[str], versions: dict,
                node_image: str, dataset: str = DATASET, task_revision: str | None = None,
                config_dir: Path | None = None, config_label: str | None = None,
                drop_mcp: bool = False, profile_name: str | None = None) -> dict:
    rows = sanitize_tasks(tasks, ids, dataset)
    selection = selection_record(ids, rows, dataset, task_revision)
    for value in versions.values():
        if value is not None:
            version(value)
    if not node_image.strip() or node_image.startswith("-"):
        raise ValueError("A Node runtime image is required")
    if config_label is not None:
        identifier(config_label)
    identifier(run.name)
    run.mkdir(parents=True, exist_ok=False)
    try:
        config = make_profile(root, run / "build", versions, config_dir, drop_mcp, profile_name)
        versions = read_json(run / "build/provider-plan.json")["versions"]
        config["label"] = config_label
        write_json(run / "build/runtime.json", runtime_record(versions, node_image, rows))
        write_json(run / "build/selection.json", selection)
        digest = fingerprint(run / "build")
        manifest = {
            "schema_version": 1, "run_id": run.name, "config_id": f"pepi-{digest[:12]}",
            "config": config, "dataset": dataset, "split": "test",
            # Revision of the runner code; injected config is described by `config`.
            "pepi_revision": git_revision(HERE), "pepi_dirty": git_dirty(HERE),
            "task_repo_revision": task_revision, "selection": selection, "profile_sha256": digest,
            "versions": versions, "node_image": node_image, "tasks": rows,
            "profile_policy": "isolated; keeps model/role config, AGENTS.md and workflows; "
                              + ("native MCP disabled; " if drop_mcp else "native MCP enabled; ")
                              + "drops gateway tools, personal skills, memory and router packages",
        }
        write_json(run / "manifest.json", manifest)
        return manifest
    except BaseException:
        shutil.rmtree(run)
        raise
