"""Reduce canonical Pi events and versioned pi-subagents accounting snapshots.

Never add RPC totals to parent event totals: they overlap. Missing full-tree
accounting remains unknown, with separately useful parent-only observations.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

FIELDS = ("input", "output", "cacheRead", "cacheWrite", "cost", "turns")


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


def parent_stats(path: Path) -> dict:
    problems = []
    result = {"session_id": None, "turns": 0, "assistant_messages": 0,
              "tool_calls": 0, "tool_errors": 0, "retries": 0, "compactions": 0,
              "usage": dict.fromkeys(FIELDS, 0), "compaction_usage": dict.fromkeys(FIELDS, 0),
              "by_model": {}, "problems": problems}
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


if __name__ == "__main__":
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
