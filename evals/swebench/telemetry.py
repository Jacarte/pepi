"""Reduce canonical Pi events and versioned pi-subagents accounting snapshots.

Never add RPC totals to parent event totals: they overlap. Missing full-tree
accounting remains unknown, with separately useful parent-only observations.
"""
from __future__ import annotations

import json
import math
import posixpath
import re
from pathlib import Path

FIELDS = ("input", "output", "cacheRead", "cacheWrite", "cost", "turns")
# Consented host skills live here in the task container (runner.SKILLS_DIR).
SKILLS_ROOT = "/opt/pepi-agent/skills"
SKILL_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value >= 0 else None


def usage(raw: dict, *, message: bool = False) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    values = {key: number(raw.get(key)) for key in FIELDS}
    if message:
        cost = raw.get("cost")
        values["cost"] = number(cost.get("total")) if isinstance(cost, dict) else None
        values["turns"] = 0
    return values


def add(left: dict, right: dict) -> dict:
    return {key: left[key] + right[key] if left[key] is not None and right[key] is not None else None
            for key in FIELDS}


def records(path: Path, problems: list[str]):
    if not path.is_file():
        problems.append(f"Missing {path.name}")
        return
    with path.open("rb") as stream:
        for index, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("Record is not an object")
                yield value
            except (ValueError, UnicodeDecodeError) as error:
                problems.append(f"{path.name}:{index}: {error}")


def skill_read(args) -> tuple[str, bool] | None:
    """(skill name, valid) for a `read` call of a file below SKILLS_ROOT/<name>/, else None.

    Only absolute paths count (a leading `@` is stripped, as Pi does); `..` is resolved first, so a
    path that escapes the skills root is ignored. A lower bound: relative paths and reads through
    bash/cat are not counted."""
    path = args.get("path") if isinstance(args, dict) else None
    if not isinstance(path, str):
        return None
    path = path[1:] if path.startswith("@") else path
    if not path.startswith("/"):
        return None
    parts = ("/" + posixpath.normpath(path).lstrip("/")).split("/")
    root = SKILLS_ROOT.split("/")
    if len(parts) < len(root) + 2 or parts[:len(root)] != root:
        return None
    name = parts[len(root)]
    return name, bool(SKILL_NAME.fullmatch(name))


def parent_stats(path: Path) -> dict:
    problems = []
    result = {"session_id": None, "turns": 0, "assistant_messages": 0,
              "tool_calls": 0, "tool_errors": 0, "retries": 0, "compactions": 0,
              "usage": dict.fromkeys(FIELDS, 0), "compaction_usage": dict.fromkeys(FIELDS, 0),
              "by_model": {}, "skill_reads": {}, "skill_reads_invalid_names": 0, "problems": problems}
    seen_tools = set()
    ended_tools = set()
    for event in records(path, problems):
        kind = event.get("type")
        if kind == "message_end" and not isinstance(event.get("message"), dict):
            problems.append("message_end has no valid message object")
            continue
        if kind == "session":
            result["session_id"] = event.get("id")
        elif kind == "turn_end":
            result["turns"] += 1
        elif kind == "message_end" and event.get("message", {}).get("role") == "assistant":
            message = event["message"]
            current = usage(message.get("usage"), message=True)
            result["usage"] = add(result["usage"], current)
            model = f"{message.get('provider', 'unknown')}/{message.get('model', 'unknown')}"
            result["by_model"][model] = add(result["by_model"].get(model, dict.fromkeys(FIELDS, 0)), current)
            result["assistant_messages"] += 1
        elif kind == "tool_execution_start":
            identity = event.get("toolCallId")
            if identity not in seen_tools:
                result["tool_calls"] += 1
                seen_tools.add(identity)
                read = skill_read(event.get("args")) if event.get("toolName") == "read" else None
                if read is not None and read[1]:
                    result["skill_reads"][read[0]] = result["skill_reads"].get(read[0], 0) + 1
                elif read is not None:
                    result["skill_reads_invalid_names"] += 1
        elif kind == "tool_execution_end":
            identity = event.get("toolCallId")
            if identity not in ended_tools:
                result["tool_errors"] += event.get("isError") is True
                ended_tools.add(identity)
        elif kind in ("auto_retry_start", "summarization_retry_attempt_start"):
            result["retries"] += 1
        elif kind == "compaction_end":
            result["compactions"] += 1
            details = event.get("result")
            raw = details.get("usage") if isinstance(details, dict) else None
            result["compaction_usage"] = add(result["compaction_usage"], usage(raw, message=True))
    result["usage"]["turns"] = result["turns"]
    return result


JEV_CRITERIA = {"task_coverage", "slice_independence", "ownership_conflicts", "integration_feasibility"}
JEV_THRESHOLD = 0.8  # Mirrors the immutable controller/scorer policy.
JEV_OUTCOMES = {"accepted", "rejected", "transport_error", "http_error", "invalid_response", "cancelled", "timeout"}


def external_jev(directory: Path) -> dict:
    """Separate HTTP accounting; receipts are not Pi model usage and never supply approval."""
    calls, invalid = {}, 0
    for path in sorted(directory.glob("*.jsonl")):
        if path.is_symlink() or not path.is_file():
            invalid += 1
            continue
        with path.open("rb") as stream:
            for line in stream:
                try:
                    if len(line) > 4096:
                        raise ValueError()
                    row = json.loads(line)
                    if not isinstance(row, dict) or set(row) - {
                        "schema_version", "receipt_id", "session_digest", "tool_call_digest", "attempt",
                        "plan_digest", "policy_digest", "model", "phase", "outcome", "duration_ms", "usage", "cost_usd", "scores", "passed"
                    } or row.get("schema_version") != 1 or row.get("model") != "jev-latest":
                        raise ValueError()
                    identity = row.get("receipt_id")
                    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9-]{36}", identity):
                        raise ValueError()
                    if any(not isinstance(row.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", row[key])
                           for key in ("session_digest", "tool_call_digest", "plan_digest", "policy_digest")):
                        raise ValueError()
                    if type(row.get("attempt")) is not int or row["attempt"] < 1 or number(row.get("duration_ms")) is None:
                        raise ValueError()
                    phase, outcome = row.get("phase"), row.get("outcome")
                    if not (phase == "started" and outcome == "unfinished" or phase == "finished" and outcome in JEV_OUTCOMES):
                        raise ValueError()
                    usage_row = row.get("usage")
                    if usage_row is not None and (not isinstance(usage_row, dict) or set(usage_row) != {"input_tokens", "output_tokens"}
                            or any(type(value) is not int or value < 0 for value in usage_row.values())):
                        raise ValueError()
                    if row.get("cost_usd") is not None:
                        raise ValueError()  # This receipt version has no verified pricing contract.
                    passed, scores = row.get("passed"), row.get("scores")
                    if outcome in ("accepted", "rejected"):
                        if usage_row is None or not isinstance(passed, dict) or set(passed) != JEV_CRITERIA \
                                or any(type(value) is not bool for value in passed.values()) \
                                or (outcome == "accepted") != all(passed.values()):
                            raise ValueError()
                        # Older receipts omit scores; new ones validate the exact values behind `passed`.
                        if scores is not None and (not isinstance(scores, dict) or set(scores) != JEV_CRITERIA
                                or any(type(value) not in (int, float) or value < 0 or value > 1
                                       or isinstance(value, float) and not math.isfinite(value)
                                       for value in scores.values())
                                or any(passed[key] != (scores[key] >= JEV_THRESHOLD) for key in JEV_CRITERIA)):
                            raise ValueError()
                    elif usage_row is not None or passed is not None or scores is not None:
                        raise ValueError()
                    phases = calls.setdefault(identity, {})
                    if phase in phases and phases[phase] != row:
                        phases["conflict"] = True
                        raise ValueError()
                    phases[phase] = row
                except (ValueError, TypeError, KeyError, UnicodeDecodeError):
                    invalid += 1  # Never echo an input line, key, file name or parser message.
    rows = []
    for phases in calls.values():
        start, end = phases.get("started"), phases.get("finished")
        binding = ("session_digest", "tool_call_digest", "plan_digest", "policy_digest", "attempt", "model")
        if phases.get("conflict") or start and end and any(start[key] != end[key] for key in binding):
            invalid += 1
            rows.append({"outcome": "unknown", "usage": None})
        else:
            rows.append(end or start)
    outcomes = {}
    for row in rows:
        outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
    scored = sorted((row for row in rows if isinstance(row.get("scores"), dict)), key=lambda row: row["attempt"])
    score_attempts = [{"attempt": row["attempt"], "outcome": row["outcome"],
                       "minimum_score": min(row["scores"].values()), "criteria": row["scores"]}
                      for row in scored]
    best_score = None
    for attempt in score_attempts:
        if best_score is None or attempt["minimum_score"] > best_score["minimum_score"]:
            best_score = attempt
    known = [row["usage"] for row in rows if row.get("usage") is not None]
    complete = bool(rows) and len(known) == len(rows) and invalid == 0
    observed = {key: sum(row[key] for row in known) for key in ("input_tokens", "output_tokens")}
    return {"status": "available" if complete else "partial" if rows or invalid else "unavailable",
            "http_attempts": len(rows), "outcomes": outcomes, "invalid_receipts": invalid,
            "scored_attempts": len(scored), "score_attempts": score_attempts, "best_score": best_score,
            "usage": observed if complete else None, "observed_usage": observed,
            "cost_usd": None, "duration_ms_sum": sum(row.get("duration_ms", 0) for row in rows),
            "cost_note": "External Jev HTTP calls, separate from Pi totals; pricing unavailable, not zero"}


def collect(attempt: Path) -> dict:
    parent = parent_stats(attempt / "parent.events.jsonl")
    problems = list(parent["problems"])
    snapshots = []
    for path in sorted((attempt / "artifacts/telemetry").glob("*.jsonl")):
        for event in records(path, problems):
            if event.get("schema_version") == 1 and event.get("session_id") == parent["session_id"] \
                    and parent["session_id"] is not None:
                snapshots.append(event)
    snapshots.sort(key=lambda row: row.get("sequence", 0))
    last = snapshots[-1] if snapshots else {}
    available = [row for row in snapshots if isinstance(row.get("accounting"), dict)
                 and row["accounting"].get("version") == 1]
    report = available[-1]["accounting"] if available else None
    active = number(last.get("active_children"))
    result = {
        "schema_version": 1, "parent_stream": parent, "accounting_status": "unavailable",
        "total": None, "parent": None, "children": [], "unresolved_async_children": None,
        "reported_active_children": active, "snapshot_phase": last.get("phase"),
        "cost_source": "pi model-price estimate; not gateway billing",
        "coverage": "session-accounted usage; external calls and hidden provider retries are not guaranteed",
        "parent_tool_timing": last.get("tools"), "problems": problems,
        "external_jev": external_jev(attempt / "artifacts/jev-receipts"),
        # Optional (absent in older telemetry.json): skill names and counts only, never paths/content.
        "skill_reads": parent.pop("skill_reads"), "skill_reads_invalid_names": parent.pop("skill_reads_invalid_names"),
    }
    if report:
        unresolved = number(report.get("unresolvedAsyncChildren"))
        result.update(total=usage(report.get("total")), parent=usage(report.get("parent")),
                      unresolved_async_children=unresolved)
        for child in report.get("children", []):
            result["children"].append({key: child.get(key) for key in ("agent", "label", "runId")}
                                      | {"usage": usage(child.get("usage"))})
        complete_fields = all(value is not None for value in result["total"].values())
        result["accounting_status"] = "available" if (
            complete_fields and unresolved == 0 and active == 0 and not problems
            and last.get("accounting") is not None and last.get("phase") == "shutdown"
            and not last.get("child_session_copy_errors")
        ) else "partial"
    return result


def collect_run(run: Path) -> list[dict]:
    from .common import read_manifest, write_json
    manifest = read_manifest(run)
    results = []
    for task in manifest["tasks"]:
        attempt = run / "attempts" / task["instance_id"]
        row = collect(attempt)
        row["instance_id"] = task["instance_id"]
        write_json(attempt / "telemetry.json", row)
        results.append(row)
    return results


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Reduce archived parent/child usage without model calls")
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    try:
        rows = collect_run(args.run.resolve())
        print(f"Collected {len(rows)} task records; "
              f"{sum(row['accounting_status'] == 'available' for row in rows)} with available accounting")
    except (OSError, ValueError, KeyError) as error:
        parser.exit(2, f"pepi-eval telemetry: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
