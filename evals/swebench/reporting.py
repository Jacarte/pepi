"""Official grading, fixed-denominator summaries and paired comparisons."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .common import fingerprint, identifier, read_json, read_manifest, write_json
from .evalscript import RESTORE_POLICY, RESTORE_POLICY_VERSION, checkout_paths, rewrite_eval_script
from .telemetry import collect, number, records
from .testpaths import STRIP_POLICY, STRIP_POLICY_VERSION, existing_paths, patch_test_edits, strip_test_files


# execution.json keys that change what is measured; paired runs must match on all of them.
# Every other execution.json key is configuration: reported by compare(), never a blocker.
COMPARABLE_PROTOCOL_KEYS = ("timeout_seconds", "cpus", "memory", "concurrency", "host", "network",
                            "network_policy", "pids_limit", "timeout_scope", "timeout_patch_policy")
# Runs omit forwarded_env_names/mcp_servers when nothing is forwarded/copied, so absent equals [].
HISTORY_NOT_PRUNED = ("refs_only", "failed")
CONFIGURATION_DEFAULTS = {"forwarded_env_names": [], "mcp_servers": [], "skills": [], "network_requirements": []}
CONFIGURATION_STRING_LIMIT = 200
MISSING = object()
# Strict regrade (`grade --strip-test-edits`): separate tree and record, never the official grading's.
STRICT_DIRECTORY, STRICT_RECORD = "grading-strict", "grading-strict.json"
# Restored regrade (`grade --restore-test-files`): eval.sh test checkout repaired, patch unchanged.
RESTORED_DIRECTORY, RESTORED_RECORD = "grading-restored", "grading-restored.json"
VARIANTS = {"strict": {"label": "strict regrade", "flag": "--strip-test-edits",
                       "directory": STRICT_DIRECTORY, "record": STRICT_RECORD},
            "restored": {"label": "restored regrade", "flag": "--restore-test-files",
                         "directory": RESTORED_DIRECTORY, "record": RESTORED_RECORD}}
EVAL_SCRIPT = "eval.sh"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def predictions(run: Path, manifest: dict) -> dict:
    errors, result = [], {}
    for row in records(run / "predictions.jsonl", errors):
        instance_id = row.get("instance_id")
        if not isinstance(instance_id, str) or instance_id in result \
                or row.get("model_name_or_path") != manifest["config_id"] \
                or not isinstance(row.get("model_patch"), str):
            raise ValueError("Duplicate or malformed prediction, or wrong config ID")
        result[instance_id] = row
    if errors or set(result) != {task["instance_id"] for task in manifest["tasks"]}:
        raise ValueError("Predictions must contain exactly the selected task IDs")
    return result


def modifying_existing_tests(submitted: dict, manifest: dict) -> list:
    """Selected task IDs (manifest order) whose graded patch modifies existing test-like files."""
    return [task["instance_id"] for task in manifest["tasks"]
            if patch_test_edits(submitted[task["instance_id"]]["model_patch"])["test_edit_kind"] == "modified_existing"]


def freeze_task_repo(task_repo: Path, frozen: Path, tasks: list) -> str:
    (frozen / "tasks").mkdir(parents=True, exist_ok=False)
    # Trusted host-only grading inputs, outside the agent build context.
    shutil.copyfile(task_repo / "sweb.yaml", frozen / "sweb.yaml")
    for task in tasks:
        name = identifier(task["instance_id"])
        shutil.copytree(task_repo / "tasks" / name, frozen / "tasks" / name, symlinks=True)
    return fingerprint(frozen)


def task_input_fingerprints(task_repo: Path, instance_ids: list, transform=None) -> dict:
    """Per-task fingerprint of grading inputs (tasks/<id> plus the shared sweb.yaml); None when absent.

    `transform(relative, data)` fingerprints the inputs as a regrade would change them (see fingerprint)."""
    shared = file_hash(task_repo / "sweb.yaml")
    result = {}
    for instance_id in instance_ids:
        directory = task_repo / "tasks" / identifier(instance_id)
        result[instance_id] = (hashlib.sha256(f"{shared}\0{fingerprint(directory, transform)}".encode()).hexdigest()
                               if directory.is_dir() else None)
    return result


def restored_eval_script(relative: str, data: bytes) -> bytes:
    """Fingerprint transform: a task directory's files as the restored regrade grades them."""
    if relative != EVAL_SCRIPT:
        return data
    rewritten, _ = rewrite_eval_script(data)
    return data if rewritten is None else rewritten


def restore_selection(submitted: dict, manifest: dict, task_repo: Path) -> dict:
    """Tasks whose patch modifies existing files (manifest order), by what the checkout repair does.

    The eval.sh checkout lines are the authoritative list of the files grading resets, so they are
    intersected with every existing file the patch changes, not only test-like ones (testpaths.existing_paths).
    selected: a checkout line lists one of those files (regraded with the repair); unrewritable: eval.sh
    missing, not UTF-8, or a checkout line outside the rewritable form, so which files it resets is not known
    (restored outcome unknown); not_listed: the checkout never resets those files, so the repair changes
    nothing (official outcome kept). Patches that change no existing file keep the official outcome.
    """
    result = {"selected": [], "unrewritable": [], "not_listed": []}
    for task in manifest["tasks"]:
        instance_id = task["instance_id"]
        changed = existing_paths(submitted[instance_id]["model_patch"])
        if not changed:
            continue
        script = task_repo / "tasks" / identifier(instance_id) / EVAL_SCRIPT
        try:
            paths = checkout_paths(script.read_bytes().decode("utf-8")) if script.is_file() else None
        except UnicodeDecodeError:
            paths = None
        if paths is None:
            result["unrewritable"].append(instance_id)
        elif paths & changed:
            result["selected"].append(instance_id)
        else:
            result["not_listed"].append(instance_id)
    return result


def frozen_task_repo(run: Path, grading: dict) -> Path:
    frozen = (run / grading["task_repo"]).resolve()
    if not frozen.is_relative_to(run.resolve()):
        raise ValueError("Grading artifacts must be inside the run directory")
    return frozen


def mismatched_task_inputs(strict: dict, official: dict) -> list:
    return sorted(instance_id for instance_id, value in strict.items()
                  if value is None or official.get(instance_id) != value)


def run_grader(work: Path, record: dict, record_paths: list) -> int:
    started = time.monotonic()
    try:
        with (work / "grader.log").open("wb") as log:
            completed = subprocess.run([sys.executable, str(Path(__file__).with_name("grade_driver.py")),
                                        str(work / "arguments.json")], cwd=work, stdout=log,
                                       stderr=subprocess.STDOUT, check=False)
        record["exit_code"] = completed.returncode
        if completed.returncode != 0:
            print(f"grader failed (exit {completed.returncode}); see {work / 'grader.log'}. The grader needs the "
                  f"swebench environment: run this with ~/.venvs/swebench/bin/python (or via evals/pepi-eval.sh).",
                  file=sys.stderr)
    finally:
        record["elapsed_seconds"] = time.monotonic() - started
        for path in record_paths:
            write_json(path, record)
    return record["exit_code"]


def refuse_changed_task_inputs(run: Path, task_repo: Path, instance_ids: list) -> None:
    """Before a regrade: tasks keeping or deciding by official results must use the official grading inputs."""
    if instance_ids and (run / "grading.json").is_file():
        official = task_input_fingerprints(frozen_task_repo(run, read_json(run / "grading.json")), instance_ids)
        changed = mismatched_task_inputs(task_input_fingerprints(task_repo, instance_ids), official)
        if changed:
            raise ValueError("The task repo's grading inputs differ from the official grading for: "
                             + ", ".join(changed) + "; use the task repo revision of the official grading")


def refuse_changed_pinned_inputs(manifest: dict, task_repo: Path, label: str) -> None:
    """Preset runs (Multilingual10) pin per-task grading inputs at prepare: grade only exactly those inputs."""
    pinned = manifest.get("task_input_fingerprints")
    if pinned is None:
        return
    ids = [task["instance_id"] for task in manifest["tasks"]]
    changed = mismatched_task_inputs(task_input_fingerprints(task_repo, ids), pinned)
    if changed:
        raise ValueError(f"The {label}'s grading inputs differ from the ones this run was prepared with for: "
                         + ", ".join(changed) + "; pass the task repo this run was prepared from")


def grade_variant(run: Path, manifest: dict, variant: str, record: dict, patches: dict, task_repo: Path,
                  workers: int, timeout: int, adjust=None) -> int:
    """Grade `patches` ({instance_id: patch}, manifest order) under a fresh <run>-<variant>-<uuid> ID.

    Results go to the variant's directory and record; grading/ and grading.json are never touched.
    Without patches only the record is written (no task-repo copy, no grader). `adjust(frozen, ids)` may
    change the regrade's own task-repo copy and returns extra record fields."""
    spec = VARIANTS[variant]
    grade_id = f"{manifest['run_id']}-{variant}-{uuid.uuid4().hex[:12]}"
    work = run / spec["directory"] / grade_id
    work.mkdir(parents=True, exist_ok=False)
    graded = list(patches)
    record = {"schema_version": 1, "run_id": grade_id, **record,
              "source_predictions_sha256": file_hash(run / "predictions.jsonl"), "graded_ids": graded,
              "predictions": None, "predictions_sha256": None, "reports": None, "task_repo": None,
              "grader_sha256": None, "task_input_fingerprints": {},
              "workers": workers, "test_timeout_seconds": timeout, "exit_code": None}
    record_paths = [work / "grading.json", run / spec["record"]]
    if not graded:
        record.update(exit_code=0, elapsed_seconds=0)
        for path in record_paths:
            write_json(path, record)
        return 0
    with (work / "predictions.jsonl").open("x", encoding="utf-8") as stream:
        for instance_id, patch_text in patches.items():
            stream.write(json.dumps({"instance_id": instance_id, "model_name_or_path": manifest["config_id"],
                                     "model_patch": patch_text}) + "\n")
    tasks = [task for task in manifest["tasks"] if task["instance_id"] in patches]
    frozen = work / "task-repo"
    freeze_task_repo(task_repo, frozen, tasks)
    if adjust is not None:
        record.update(adjust(frozen, graded))
    record.update(predictions=str((work / "predictions.jsonl").relative_to(run)),
                  predictions_sha256=file_hash(work / "predictions.jsonl"),
                  reports=str((work / "logs/evaluation" / grade_id).relative_to(run)),
                  task_repo=str(frozen.relative_to(run)), grader_sha256=fingerprint(frozen),
                  task_input_fingerprints=task_input_fingerprints(frozen, graded))
    write_json(work / "arguments.json", {
        "dataset_name": manifest["dataset"], "split": manifest["split"], "instance_ids": graded,
        "predictions_path": str(work / "predictions.jsonl"), "max_workers": workers,
        "open_file_limit": 4096, "run_id": grade_id, "timeout": timeout,
        "rewrite_reports": False, "modal": False, "task_repo": str(frozen), "expected_tasks": tasks,
    })
    return run_grader(work, record, record_paths)


def grade_strict(run: Path, task_repo: Path, workers: int, timeout: int) -> int:
    """Regrade only tasks whose patch modifies existing test files, with every test-like file block removed.

    Results go to grading-strict/<id>/ and grading-strict.json; grading/ and grading.json are never touched.
    A patch that is empty once stripped is not graded (summarize reports it as empty_patch).
    """
    manifest = read_manifest(run)
    submitted = predictions(run, manifest)
    refuse_changed_pinned_inputs(manifest, task_repo, "task repo")
    selected = modifying_existing_tests(submitted, manifest)
    stripped = {instance_id: strip_test_files(submitted[instance_id]["model_patch"]) for instance_id in selected}
    graded = [instance_id for instance_id in selected if stripped[instance_id][0]]
    # Untouched tasks keep their official outcome, so regraded ones must use the same grader inputs.
    refuse_changed_task_inputs(run, task_repo, graded)
    return grade_variant(run, manifest, "strict", {
        "strip_policy_version": STRIP_POLICY_VERSION, "strip_policy": STRIP_POLICY, "selected_ids": selected,
        "empty_after_strip_ids": [instance_id for instance_id in selected if not stripped[instance_id][0]],
        "removed_test_file_blocks": {instance_id: stripped[instance_id][1] for instance_id in selected},
    }, {instance_id: stripped[instance_id][0] for instance_id in graded}, task_repo, workers, timeout)


def repair_test_checkouts(frozen: Path, instance_ids: list) -> dict:
    """Rewrite eval.sh of each task in the restored regrade's own task-repo copy (evalscript.py)."""
    scripts = {}
    for instance_id in instance_ids:
        path = frozen / "tasks" / identifier(instance_id) / EVAL_SCRIPT
        original = path.read_bytes()
        rewritten, lines = rewrite_eval_script(original)
        if rewritten is None or not lines:
            raise ValueError(f"eval.sh of {instance_id} changed during the restored regrade; run it again")
        path.write_bytes(rewritten)
        scripts[instance_id] = {"original_sha256": hashlib.sha256(original).hexdigest(),
                                "rewritten_sha256": hashlib.sha256(rewritten).hexdigest(),
                                "rewritten_lines": lines}
    return {"eval_scripts": scripts}


def grade_restored(run: Path, task_repo: Path, workers: int, timeout: int) -> int:
    """Regrade tasks whose patch modifies an existing file that their eval.sh checkout resets, with the
    checkout repaired in this regrade's own task-repo copy and the patch unchanged (see evalscript.py).

    Results go to grading-restored/<id>/ and grading-restored.json; grading/, grading.json and the
    --task-repo are never touched.
    """
    manifest = read_manifest(run)
    submitted = predictions(run, manifest)
    refuse_changed_pinned_inputs(manifest, task_repo, "task repo")
    selection = restore_selection(submitted, manifest, task_repo)
    # The selection reads these tasks' eval.sh, and untouched tasks keep their official outcome.
    refuse_changed_task_inputs(run, task_repo, [instance_id for ids in selection.values() for instance_id in ids])
    return grade_variant(run, manifest, "restored", {
        "restore_policy_version": RESTORE_POLICY_VERSION, "restore_policy": RESTORE_POLICY,
        "selected_ids": selection["selected"], "unrewritable_ids": selection["unrewritable"],
        "not_listed_ids": selection["not_listed"], "eval_scripts": {},
    }, {instance_id: submitted[instance_id]["model_patch"] for instance_id in selection["selected"]},
        task_repo, workers, timeout, repair_test_checkouts)


def grade_run(run: Path, task_repo: Path, workers: int, timeout: int, strip_test_edits: bool = False,
              restore_test_files: bool = False) -> int:
    if workers <= 0 or timeout <= 0:
        raise ValueError("Workers and grading timeout must be positive")
    if strip_test_edits and restore_test_files:
        raise ValueError("Choose one of --strip-test-edits and --restore-test-files per grade")
    if strip_test_edits:
        return grade_strict(run, task_repo, workers, timeout)
    if restore_test_files:
        return grade_restored(run, task_repo, workers, timeout)
    manifest = read_manifest(run)
    predictions(run, manifest)
    refuse_changed_pinned_inputs(manifest, task_repo, "task repo")
    grade_id = f"{manifest['run_id']}-{uuid.uuid4().hex[:12]}"
    work = run / "grading" / grade_id
    frozen = work / "task-repo"
    grading_hash = freeze_task_repo(task_repo, frozen, manifest["tasks"])
    try:
        refuse_changed_pinned_inputs(manifest, frozen, "frozen task copy")  # changed while copying
    except ValueError:
        shutil.rmtree(work)
        raise
    arguments = {
        "dataset_name": manifest["dataset"], "split": manifest["split"],
        "instance_ids": [task["instance_id"] for task in manifest["tasks"]],
        "predictions_path": str(run / "predictions.jsonl"), "max_workers": workers,
        "open_file_limit": 4096, "run_id": grade_id, "timeout": timeout,
        "rewrite_reports": False, "modal": False, "task_repo": str(frozen),
        "expected_tasks": manifest["tasks"],
    }
    write_json(work / "arguments.json", arguments)
    record = {"schema_version": 1, "run_id": grade_id,
              "predictions_sha256": file_hash(run / "predictions.jsonl"),
              "reports": str((work / "logs/evaluation" / grade_id).relative_to(run)),
              "task_repo": str(frozen.relative_to(run)), "grader_sha256": grading_hash,
              "workers": workers, "test_timeout_seconds": timeout, "exit_code": None}
    return run_grader(work, record, [work / "grading.json", run / "grading.json"])


def distribution(values: list) -> dict:
    known = sorted(value for value in values if number(value) is not None)
    return {"observations": len(known), "missing": len(values) - len(known),
            "median": statistics.median(known) if known else None,
            "p95": known[math.ceil(len(known) * 0.95) - 1] if known else None}


def graded_artifacts(run: Path, grading: dict) -> tuple[Path, set]:
    """Validated report directory and evaluator error IDs of one grading record."""
    reports = (run / grading["reports"]).resolve()
    frozen = frozen_task_repo(run, grading)
    if not reports.is_relative_to(run.resolve()):
        raise ValueError("Grading artifacts must be inside the run directory")
    if fingerprint(frozen) != grading["grader_sha256"]:
        raise ValueError("Frozen grading inputs changed")
    overall = read_json(reports / "results.json") if (reports / "results.json").is_file() else {}
    return reports, set(overall.get("error_ids", []))


def report_outcome(report_path: Path, instance_id: str, empty_patch: bool, errors: set) -> tuple:
    if report_path.is_file():
        resolved = read_json(report_path).get(instance_id, {}).get("resolved")
        if not isinstance(resolved, bool):
            raise ValueError(f"Invalid official report: {instance_id}")
        return resolved, "resolved" if resolved else "unresolved"
    if empty_patch:
        return False, "empty_patch"
    return None, "grading_error" if instance_id in errors else "not_graded"


def variant_outcomes(run: Path, manifest: dict, submitted: dict, official_inputs: Path, variant: str) -> dict | None:
    """Per-task (resolved, status) of a strict or restored regrade, or None when there is none.

    Tasks outside its selection keep their official outcome, filled in by the caller, so every regraded
    task must have used the official grading inputs (`official_inputs`), changed only as the variant does."""
    spec = VARIANTS[variant]
    if not (run / spec["record"]).is_file():
        return None
    record = read_json(run / spec["record"])
    label, again = spec["label"], f"run grade {spec['flag']} again"
    if record.get("source_predictions_sha256") != file_hash(run / "predictions.jsonl"):
        raise ValueError(f"Predictions changed after the {label}; {again} or remove {spec['record']}")
    if variant == "strict":
        current = record.get("strip_policy_version") == STRIP_POLICY_VERSION \
            and record.get("selected_ids") == modifying_existing_tests(submitted, manifest)
        outcomes = {instance_id: (False, "empty_patch") for instance_id in record["empty_after_strip_ids"]}
        transform = None
    else:
        selection = restore_selection(submitted, manifest, official_inputs)
        current = record.get("restore_policy_version") == RESTORE_POLICY_VERSION \
            and [record.get(key) for key in ("selected_ids", "unrewritable_ids", "not_listed_ids")] \
            == [selection[key] for key in ("selected", "unrewritable", "not_listed")] \
            and record.get("graded_ids") == selection["selected"]
        # Unrewritable checkouts could not be repaired: no restored outcome, not the official one.
        outcomes = {instance_id: (None, "not_graded") for instance_id in record["unrewritable_ids"]}
        transform = restored_eval_script
    if not current:
        raise ValueError(f"The {label} used another test-file policy; {again}")
    if record["graded_ids"]:
        graded_predictions = (run / record["predictions"]).resolve()
        if not graded_predictions.is_relative_to(run.resolve()) \
                or record.get("predictions_sha256") != file_hash(graded_predictions):
            raise ValueError(f"{label.capitalize()} predictions changed; {again}")
        reports, errors = graded_artifacts(run, record)
        used = task_input_fingerprints(frozen_task_repo(run, record), record["graded_ids"])
        expected = task_input_fingerprints(official_inputs, record["graded_ids"], transform)
        changed = mismatched_task_inputs(used, expected)
        if changed or record.get("task_input_fingerprints") != used:
            raise ValueError(f"The {label} used other grading inputs than the official grading for: "
                             + ", ".join(changed or record["graded_ids"]) + f"; {again}")
        for instance_id in record["graded_ids"]:
            outcomes[instance_id] = report_outcome(reports / manifest["config_id"] / instance_id / "report.json",
                                                   instance_id, False, errors)
    return {"outcomes": outcomes, "exit_code": record["exit_code"], "run_id": record["run_id"], "record": record}


def summarize_run(run: Path) -> dict:
    manifest = read_manifest(run)
    submitted = predictions(run, manifest)
    grading = read_json(run / "grading.json")
    if grading["predictions_sha256"] != file_hash(run / "predictions.jsonl"):
        raise ValueError("Predictions changed after grading; grade them again")
    reports, errors = graded_artifacts(run, grading)
    official_inputs = frozen_task_repo(run, grading)
    variants = {variant: variant_outcomes(run, manifest, submitted, official_inputs, variant) for variant in VARIANTS}
    variants = {variant: result for variant, result in variants.items() if result is not None}
    strict, restored = variants.get("strict"), variants.get("restored")
    rows = []
    for task in manifest["tasks"]:
        instance_id = task["instance_id"]
        directory = run / "attempts" / instance_id
        attempt = read_json(directory / "attempt.json") if (directory / "attempt.json").is_file() else {}
        telemetry = read_json(directory / "telemetry.json") if (directory / "telemetry.json").is_file() else collect(directory)
        patch = submitted[instance_id]["model_patch"]
        resolved, status = report_outcome(reports / manifest["config_id"] / instance_id / "report.json",
                                          instance_id, not patch, errors)
        total = telemetry.get("total") or {}
        agent_status = attempt.get("agent_status", "not_started")
        if agent_status == "completed" and (telemetry.get("reported_active_children") or 0) > 0:
            agent_status = "unfinished_children"
        rows.append({
            "instance_id": instance_id, "resolved": resolved, "grading_status": status,
            "agent_status": agent_status,
            "agent_wall_seconds": attempt.get("timing_seconds", {}).get("agent_wall"),
            "setup_seconds": attempt.get("timing_seconds", {}).get("setup"),
            "turns": total.get("turns"), "input_tokens": total.get("input"),
            "output_tokens": total.get("output"), "cache_read_tokens": total.get("cacheRead"),
            "cache_write_tokens": total.get("cacheWrite"), "estimated_cost_usd": total.get("cost"),
            "accounting_status": telemetry.get("accounting_status", "unavailable"),
            **patch_test_edits(patch),
            # runner.HISTORY_PRUNE_SCRIPT outcome (pruned/refs_only/failed/skipped); None for older attempts.
            "history_pruning": (attempt.get("history_pruning") or {}).get("status"),
        })
        for variant, result in variants.items():
            rows[-1][f"resolved_{variant}"], rows[-1][f"grading_status_{variant}"] = \
                result["outcomes"].get(instance_id, (resolved, status))
    count = len(rows)
    if not count:
        raise ValueError("Cannot summarize an empty task selection")
    solved = sum(row["resolved"] is True for row in rows)
    available = sum(row["accounting_status"] == "available" for row in rows)
    costs = [number(row["estimated_cost_usd"]) for row in rows]
    observed_cost = sum(cost for cost in costs if cost is not None)
    full_cost = available == count and all(cost is not None for cost in costs)
    task_identity = [{key: task[key] for key in
                      ("instance_id", "repo", "base_commit", "image", "problem_sha256")}
                     for task in manifest["tasks"]]
    summary = {
        "schema_version": 1, "run_id": manifest["run_id"], "config_id": manifest["config_id"],
        "dataset": manifest["dataset"], "task_identity": sorted(task_identity, key=lambda row: row["instance_id"]),
        "protocol": read_json(run / "execution.json"), "selected": count, "resolved": solved,
        "resolved_fraction": solved / count, "unknown_grades": sum(row["resolved"] is None for row in rows),
        "grader_exit_code": grading["exit_code"], "grading_seconds": grading["elapsed_seconds"],
        "latency_all_seconds": distribution([row["agent_wall_seconds"] for row in rows]),
        "latency_resolved_seconds": distribution([row["agent_wall_seconds"] for row in rows if row["resolved"] is True]),
        "turns_observed": distribution([row["turns"] for row in rows]),
        "available_accounting_tasks": available, "observed_estimated_cost_usd": observed_cost,
        "estimated_total_cost_usd": observed_cost if full_cost else None,
        "estimated_cost_per_resolved": observed_cost / solved if full_cost and solved else None,
        "cost_caveat": "Pi estimates within reported coverage; includes failed attempts; not gateway billing",
        "tasks_modifying_existing_tests": sum(row["test_edit_kind"] == "modified_existing" for row in rows),
        "resolved_with_test_edits": sum(row["resolved"] is True and row["test_edit_kind"] == "modified_existing"
                                        for row in rows),
        # The agent ran with later git history (possibly the reference fix) still reachable or present.
        "tasks_history_not_pruned": sum(row["history_pruning"] in HISTORY_NOT_PRUNED for row in rows),
        "instances": rows,
    }
    if strict is not None:
        summary["resolved_strict"] = sum(row["resolved_strict"] is True for row in rows)
        summary["unknown_grades_strict"] = sum(row["resolved_strict"] is None for row in rows)
        summary["strict_grading"] = {"run_id": strict["run_id"], "grader_exit_code": strict["exit_code"],
                                     "strip_policy": STRIP_POLICY,
                                     "selected_tasks": len(strict["outcomes"])}
    if restored is not None:
        summary["resolved_restored"] = sum(row["resolved_restored"] is True for row in rows)
        summary["unknown_grades_restored"] = sum(row["resolved_restored"] is None for row in rows)
        summary["restored_grading"] = {"run_id": restored["run_id"], "grader_exit_code": restored["exit_code"],
                                       "restore_policy": RESTORE_POLICY,
                                       "regraded_tasks": len(restored["record"]["graded_ids"]),
                                       "unrewritable_tasks": len(restored["record"]["unrewritable_ids"]),
                                       "unrewritable_ids": restored["record"]["unrewritable_ids"]}
    suite_note = ""
    if "selection" in manifest:
        summary["selection"] = manifest["selection"]
        preset = manifest["selection"].get("preset")
        if preset:
            builds = read_json(run / "builds.json")
            if set(builds) != {task["instance_id"] for task in manifest["tasks"]}:
                raise ValueError("Build records must cover exactly the selected task IDs")
            summary["comparison_runtime"] = {
                "versions": manifest["versions"], "node_image": manifest["node_image"],
                "platforms": {instance_id: row["platform"] for instance_id, row in builds.items()},
            }
            suite_note = f"{preset['label']}. Preset: `{preset['id']}`.\n\n"
    write_json(run / "summary.json", summary)
    with (run / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({**row, "test_files_touched": ";".join(row["test_files_touched"])} for row in rows)
    scores = []
    if strict is not None:
        unknown_strict = (f"; {summary['unknown_grades_strict']} unknown"
                          if summary["unknown_grades_strict"] else "")
        scores.append(f"strict, test edits removed: {summary['resolved_strict']}/{count}{unknown_strict}")
    if restored is not None:
        unknown_restored = (f", {summary['unknown_grades_restored']} unknown"
                            if summary["unknown_grades_restored"] else "")
        scores.append(f"test-checkout repaired: {summary['resolved_restored']}/{count}{unknown_restored}")
    variant_note = f" ({'; '.join(scores)})" if scores else ""
    test_edit_note = ""
    if summary["resolved_with_test_edits"]:
        follow_up = ("see strict and test-checkout repaired scores" if strict is not None and restored is not None
                     else "see strict score" if strict is not None
                     else "see the test-checkout repaired score" if restored is not None
                     else "regrade with `reporting grade --strip-test-edits` for a strict score")
        test_edit_note = (f"Note: {summary['resolved_with_test_edits']} of {solved} resolved tasks modified "
                          f"existing test files; {follow_up}.\n\n")
    history_note = ""
    if summary["tasks_history_not_pruned"]:
        history_note = (f"Note: {summary['tasks_history_not_pruned']} task(s) ran with git history after the task "
                        "base not fully removed (`history_pruning` in summary.csv; see attempts/<id>/attempt.json).\n\n")
    (run / "summary.md").write_text(
        f"# {manifest['run_id']}\n\n"
        f"{suite_note}"
        f"Resolved: **{solved}/{count} ({100 * solved / count:.1f}%)**{variant_note}. "
        f"Unknown grades: {summary['unknown_grades']}.\n\n"
        f"{test_edit_note}"
        f"{history_note}"
        f"Agent latency (all observed attempts): {summary['latency_all_seconds']}.\n\n"
        f"Resolved-task latency: {summary['latency_resolved_seconds']}.\n\n"
        f"Available accounting: {available}/{count}. Estimated cost per resolved task: "
        f"{summary['estimated_cost_per_resolved']}. `null`/`None` means unknown or undefined, not zero.\n\n"
        "See summary.csv for per-task time, turns, token categories, cost and failure states.\n"
        "Costs are Pi estimates, not invoices; reported coverage can omit unobserved work.\n", encoding="utf-8")
    return summary


def configuration_value(value):
    """Keep only JSON-plain values with capped strings, so configuration differences are safe to display."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else "<unsupported value>"
    if isinstance(value, str):
        return value[:CONFIGURATION_STRING_LIMIT]
    if isinstance(value, list):
        return [configuration_value(item) for item in value]
    if isinstance(value, dict):
        return {key[:CONFIGURATION_STRING_LIMIT]: configuration_value(item)
                for key, item in value.items() if isinstance(key, str)}
    return "<unsupported value>"


def configuration_differences(left: dict, right: dict) -> dict:
    result = {}
    for key in sorted((set(left) | set(right)) - set(COMPARABLE_PROTOCOL_KEYS)):
        default = CONFIGURATION_DEFAULTS.get(key)
        baseline, candidate = left.get(key, default), right.get(key, default)
        if baseline != candidate:
            result[key] = {"baseline": configuration_value(baseline), "candidate": configuration_value(candidate)}
    return result


def compare(left: dict, right: dict) -> dict:
    for key in ("dataset", "task_identity"):
        if left[key] != right[key]:
            raise ValueError(f"Cannot pair runs with different {key}; do not silently intersect task sets")
    protocols = left["protocol"], right["protocol"]
    if not all(isinstance(protocol, dict) for protocol in protocols):
        raise ValueError("Cannot pair runs with a malformed execution protocol; summarize them again")
    different = [key for key in COMPARABLE_PROTOCOL_KEYS
                 if protocols[0].get(key, MISSING) != protocols[1].get(key, MISSING)]
    if different:
        raise ValueError(f"Cannot pair runs with different execution protocol: {', '.join(different)}")
    if left.get("selection") != right.get("selection"):
        raise ValueError("Cannot pair runs with different frozen selection/source provenance")
    if left.get("selection", {}).get("preset"):
        x, y = left["comparison_runtime"], right["comparison_runtime"]
        for key in ("node_image", "platforms"):
            if x[key] != y[key]:
                raise ValueError(f"Cannot pair runs with different {key}")
        shared_versions = {"pi", "pi-subagents"} | (set(x["versions"]) & set(y["versions"]))
        if any(x["versions"].get(key) != y["versions"].get(key) for key in shared_versions):
            raise ValueError("Cannot pair runs with different shared runtime versions")
    a = {row["instance_id"]: row for row in left["instances"]}
    b = {row["instance_id"]: row for row in right["instances"]}
    if set(a) != set(b):
        raise ValueError("Task IDs differ")
    wins, regressions, unknown, deltas = [], [], [], []
    for instance_id in a:
        before, after = a[instance_id], b[instance_id]
        if before["resolved"] is None or after["resolved"] is None:
            unknown.append(instance_id)
        elif before["resolved"] is False and after["resolved"] is True:
            wins.append(instance_id)
        elif before["resolved"] is True and after["resolved"] is False:
            regressions.append(instance_id)
        if before["resolved"] is True and after["resolved"] is True:
            x, y = number(before["agent_wall_seconds"]), number(after["agent_wall_seconds"])
            if x is not None and y is not None:
                deltas.append(y - x)
    x, y = left["estimated_total_cost_usd"], right["estimated_total_cost_usd"]
    result = {"schema_version": 1, "baseline": left["run_id"], "candidate": right["run_id"],
            "resolved_delta": right["resolved"] - left["resolved"], "wins": sorted(wins),
            "regressions": sorted(regressions), "inconclusive": sorted(unknown),
            "both_resolved_latency_pairs": len(deltas),
            "both_resolved_latency_delta_median_seconds": statistics.median(deltas) if deltas else None,
            "estimated_cost_delta_usd": y - x if x is not None and y is not None else None,
            "configuration_differences": configuration_differences(*protocols),
            "note": "Positive deltas mean more time/cost for the candidate; repeat runs before interpreting small differences."}
    for variant in VARIANTS:
        key = f"resolved_{variant}"
        if key in left or key in right:
            result[f"baseline_{key}"], result[f"candidate_{key}"] = left.get(key), right.get(key)
        if key in left and key in right:
            flips = {"wins": [], "regressions": [], "inconclusive": []}
            for instance_id in sorted(a):
                before, after = a[instance_id].get(key), b[instance_id].get(key)
                if before is None or after is None:
                    flips["inconclusive"].append(instance_id)
                elif before is False and after is True:
                    flips["wins"].append(instance_id)
                elif before is True and after is False:
                    flips["regressions"].append(instance_id)
            for name, ids in flips.items():
                result[f"{variant}_{name}"] = ids
    if all("test_edit_kind" in row for row in (*a.values(), *b.values())):
        result["test_edit_kind_differences"] = [
            {"instance_id": instance_id, "baseline": a[instance_id]["test_edit_kind"],
             "candidate": b[instance_id]["test_edit_kind"]}
            for instance_id in sorted(a) if a[instance_id]["test_edit_kind"] != b[instance_id]["test_edit_kind"]]
    if left.get("selection", {}).get("preset"):
        result.update({"selection": left["selection"], "selected": left["selected"],
                       "baseline_resolved": left["resolved"], "candidate_resolved": right["resolved"],
                       "baseline_unknown_grades": left["unknown_grades"],
                       "candidate_unknown_grades": right["unknown_grades"]})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    grade = commands.add_parser("grade")
    grade.add_argument("run", type=Path)
    grade.add_argument("--task-repo", type=Path, required=True)
    grade.add_argument("--workers", type=int, required=True)
    grade.add_argument("--test-timeout-seconds", type=int, required=True)
    variant = grade.add_mutually_exclusive_group()
    variant.add_argument("--strip-test-edits", action="store_true",
                         help="regrade only tasks whose patch modifies existing test files, with test files removed, "
                              "into grading-strict/ (the official grading is kept)")
    variant.add_argument("--restore-test-files", action="store_true",
                         help="regrade only tasks whose patch modifies an existing file their eval.sh checkout "
                              "resets, with that checkout repaired and the patch unchanged, into grading-restored/ "
                              "(the official grading is kept)")
    edits = commands.add_parser("test-edits", help="print how many task patches modify existing test files")
    edits.add_argument("run", type=Path)
    checkout = commands.add_parser("test-checkout",
                                   help="print '<regraded> <unrewritable>': how many tasks grade --restore-test-files "
                                        "would regrade, and how many it could not repair (restored grade unknown)")
    checkout.add_argument("run", type=Path)
    checkout.add_argument("--task-repo", type=Path, help="default: the official grading's frozen task repo")
    summary = commands.add_parser("summarize")
    summary.add_argument("run", type=Path)
    paired = commands.add_parser("compare")
    paired.add_argument("baseline", type=Path)
    paired.add_argument("candidate", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "grade":
            return grade_run(args.run.resolve(), args.task_repo.resolve(), args.workers, args.test_timeout_seconds,
                             strip_test_edits=args.strip_test_edits, restore_test_files=args.restore_test_files)
        if args.command == "test-checkout":
            run = args.run.resolve()
            manifest = read_manifest(run)
            task_repo = args.task_repo.resolve() if args.task_repo else frozen_task_repo(run, read_json(run / "grading.json"))
            selection = restore_selection(predictions(run, manifest), manifest, task_repo)
            print(f"{len(selection['selected'])} {len(selection['unrewritable'])}")
            return 0
        if args.command == "test-edits":
            manifest = read_manifest(args.run.resolve())
            print(len(modifying_existing_tests(predictions(args.run.resolve(), manifest), manifest)))
            return 0
        if args.command == "summarize":
            result = summarize_run(args.run.resolve())
            print(f"{result['resolved']}/{result['selected']} resolved; reports written to {args.run}")
        else:
            print(json.dumps(compare(read_json(args.baseline / "summary.json"),
                                     read_json(args.candidate / "summary.json")), indent=2))
    except (ValueError, OSError, KeyError) as error:
        parser.exit(2, f"pepi-eval reporting: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
