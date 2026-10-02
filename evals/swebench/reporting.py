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
from .telemetry import collect, number, records


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


def grade_run(run: Path, task_repo: Path, workers: int, timeout: int) -> int:
    manifest = read_manifest(run)
    predictions(run, manifest)
    if workers <= 0 or timeout <= 0:
        raise ValueError("Workers and grading timeout must be positive")
    grade_id = f"{manifest['run_id']}-{uuid.uuid4().hex[:12]}"
    work = run / "grading" / grade_id
    frozen = work / "task-repo"
    (frozen / "tasks").mkdir(parents=True, exist_ok=False)
    # Trusted host-only grading inputs, outside the agent build context.
    shutil.copyfile(task_repo / "sweb.yaml", frozen / "sweb.yaml")
    for task in manifest["tasks"]:
        name = identifier(task["instance_id"])
        shutil.copytree(task_repo / "tasks" / name, frozen / "tasks" / name, symlinks=True)
    grading_hash = fingerprint(frozen)
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
    started = time.monotonic()
    try:
        with (work / "grader.log").open("wb") as log:
            completed = subprocess.run([sys.executable, str(Path(__file__).with_name("grade_driver.py")),
                                        str(work / "arguments.json")], cwd=work, stdout=log,
                                       stderr=subprocess.STDOUT, check=False)
        record["exit_code"] = completed.returncode
    finally:
        record["elapsed_seconds"] = time.monotonic() - started
        write_json(work / "grading.json", record)
        write_json(run / "grading.json", record)
    return record["exit_code"]


def distribution(values: list) -> dict:
    known = sorted(value for value in values if number(value) is not None)
    return {"observations": len(known), "missing": len(values) - len(known),
            "median": statistics.median(known) if known else None,
            "p95": known[math.ceil(len(known) * 0.95) - 1] if known else None}


def summarize_run(run: Path) -> dict:
    manifest = read_manifest(run)
    submitted = predictions(run, manifest)
    grading = read_json(run / "grading.json")
    if grading["predictions_sha256"] != file_hash(run / "predictions.jsonl"):
        raise ValueError("Predictions changed after grading; grade them again")
    reports = (run / grading["reports"]).resolve()
    frozen = (run / grading["task_repo"]).resolve()
    if not reports.is_relative_to(run.resolve()) or not frozen.is_relative_to(run.resolve()):
        raise ValueError("Grading artifacts must be inside the run directory")
    if fingerprint(frozen) != grading["grader_sha256"]:
        raise ValueError("Frozen grading inputs changed")
    overall = read_json(reports / "results.json") if (reports / "results.json").is_file() else {}
    errors = set(overall.get("error_ids", []))
    rows = []
    for task in manifest["tasks"]:
        instance_id = task["instance_id"]
        directory = run / "attempts" / instance_id
        attempt = read_json(directory / "attempt.json") if (directory / "attempt.json").is_file() else {}
        telemetry = read_json(directory / "telemetry.json") if (directory / "telemetry.json").is_file() else collect(directory)
        report_path = reports / manifest["config_id"] / instance_id / "report.json"
        resolved, status = None, "not_graded"
        if report_path.is_file():
            official = read_json(report_path).get(instance_id, {})
            resolved = official.get("resolved")
            if not isinstance(resolved, bool):
                raise ValueError(f"Invalid official report: {instance_id}")
            status = "resolved" if resolved else "unresolved"
        elif not submitted[instance_id]["model_patch"]:
            resolved, status = False, "empty_patch"
        elif instance_id in errors:
            status = "grading_error"
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
        })
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
        "instances": rows,
    }
    write_json(run / "summary.json", summary)
    with (run / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (run / "summary.md").write_text(
        f"# {manifest['run_id']}\n\n"
        f"Resolved: **{solved}/{count} ({100 * solved / count:.1f}%)**. Unknown grades: {summary['unknown_grades']}.\n\n"
        f"Agent latency (all observed attempts): {summary['latency_all_seconds']}.\n\n"
        f"Resolved-task latency: {summary['latency_resolved_seconds']}.\n\n"
        f"Available accounting: {available}/{count}. Estimated cost per resolved task: "
        f"{summary['estimated_cost_per_resolved']}. `null`/`None` means unknown or undefined, not zero.\n\n"
        "See summary.csv for per-task time, turns, token categories, cost and failure states.\n"
        "Costs are Pi estimates, not invoices; reported coverage can omit unobserved work.\n", encoding="utf-8")
    return summary


def compare(left: dict, right: dict) -> dict:
    for key in ("dataset", "task_identity", "protocol"):
        if left[key] != right[key]:
            raise ValueError(f"Cannot pair runs with different {key}; do not silently intersect task sets")
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
    return {"schema_version": 1, "baseline": left["run_id"], "candidate": right["run_id"],
            "resolved_delta": right["resolved"] - left["resolved"], "wins": sorted(wins),
            "regressions": sorted(regressions), "inconclusive": sorted(unknown),
            "both_resolved_latency_pairs": len(deltas),
            "both_resolved_latency_delta_median_seconds": statistics.median(deltas) if deltas else None,
            "estimated_cost_delta_usd": y - x if x is not None and y is not None else None,
            "note": "Positive deltas mean more time/cost for the candidate; repeat runs before interpreting small differences."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    grade = commands.add_parser("grade")
    grade.add_argument("run", type=Path)
    grade.add_argument("--task-repo", type=Path, required=True)
    grade.add_argument("--workers", type=int, required=True)
    grade.add_argument("--test-timeout-seconds", type=int, required=True)
    summary = commands.add_parser("summarize")
    summary.add_argument("run", type=Path)
    paired = commands.add_parser("compare")
    paired.add_argument("baseline", type=Path)
    paired.add_argument("candidate", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "grade":
            return grade_run(args.run.resolve(), args.task_repo.resolve(), args.workers, args.test_timeout_seconds)
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
