import json
import tempfile
import unittest
from pathlib import Path

from evals.swebench.telemetry import collect, number, parent_stats

USAGE = {"input": 100, "output": 10, "cacheRead": 20, "cacheWrite": 5,
         "cost": {"total": 0.2}, "reasoningTokens": 4}
MESSAGE = {"role": "assistant", "provider": "litellm", "model": "a", "usage": USAGE,
           "content": [{"type": "text", "text": "Unicode\u2028separator"}]}
TOTAL = {"input": 400, "output": 40, "cacheRead": 80, "cacheWrite": 20, "cost": 0.8, "turns": 4}


class TelemetryTests(unittest.TestCase):
    def fixture(self, events=None, snapshots=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        with (root / "parent.events.jsonl").open("w", encoding="utf-8") as stream:
            for event in events or [{"type": "session", "id": "parent"},
                                    {"type": "message_end", "message": MESSAGE},
                                    {"type": "turn_end", "message": MESSAGE}]:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        directory = root / "artifacts/telemetry"
        directory.mkdir(parents=True)
        if snapshots:
            (directory / "parent.jsonl").write_text("".join(json.dumps(row) + "\n" for row in snapshots))
        return root

    def snapshot(self, **updates):
        row = {"schema_version": 1, "session_id": "parent", "sequence": 1, "phase": "shutdown",
               "active_children": 0, "accounting": {"version": 1, "total": TOTAL,
               "parent": dict(TOTAL, input=100), "unresolvedAsyncChildren": 0,
               "children": [{"agent": "worker", "runId": "child", "usage": TOTAL}]}}
        row.update(updates)
        return row

    def test_count_only_canonical_final_message_not_stream_snapshots(self):
        root = self.fixture([
            {"type": "session", "id": "parent"},
            {"type": "message_update", "usage": USAGE},
            {"type": "message_end", "message": MESSAGE},
            {"type": "turn_end", "message": MESSAGE},
            {"type": "agent_end", "messages": [MESSAGE]},
        ])
        row = parent_stats(root / "parent.events.jsonl")
        self.assertEqual(row["usage"]["input"], 100)
        self.assertEqual(row["usage"]["output"], 10)
        self.assertEqual(row["turns"], 1)
        self.assertEqual(row["problems"], [])

    def test_rpc_total_is_not_added_to_parent_or_child_totals(self):
        row = collect(self.fixture(snapshots=[self.snapshot()]))
        self.assertEqual(row["total"], TOTAL)
        self.assertEqual(row["accounting_status"], "available")
        self.assertEqual(row["parent_stream"]["usage"]["input"], 100)

    def test_missing_child_accounting_does_not_become_zero(self):
        row = collect(self.fixture())
        self.assertIsNone(row["total"])
        self.assertEqual(row["accounting_status"], "unavailable")

    def test_incomplete_children_and_active_runs_are_partial(self):
        for active, missing in ((1, 0), (0, 1), (None, 0)):
            snapshot = self.snapshot(active_children=active)
            snapshot["accounting"]["unresolvedAsyncChildren"] = missing
            self.assertEqual(collect(self.fixture(snapshots=[snapshot]))["accounting_status"], "partial")

    def test_failed_shutdown_does_not_erase_previous_accounting(self):
        first = self.snapshot(phase="settled")
        last = self.snapshot(sequence=2, accounting=None, active_children=None)
        row = collect(self.fixture(snapshots=[first, last]))
        self.assertEqual(row["total"], TOTAL)
        self.assertEqual(row["accounting_status"], "partial")

    def test_child_session_snapshots_cannot_replace_parent_report(self):
        child = self.snapshot(session_id="child", sequence=100)
        child["accounting"]["total"] = dict(TOTAL, input=99999)
        row = collect(self.fixture(snapshots=[self.snapshot(), child]))
        self.assertEqual(row["total"]["input"], 400)

    def test_truncated_lines_mark_partial_without_losing_good_events(self):
        root = self.fixture(snapshots=[self.snapshot()])
        with (root / "parent.events.jsonl").open("a") as stream:
            stream.write('{"type":')
        row = collect(root)
        self.assertEqual(row["accounting_status"], "partial")
        self.assertEqual(row["parent_stream"]["assistant_messages"], 1)
        self.assertTrue(row["problems"])

    def test_missing_or_invalid_usage_is_unknown(self):
        for invalid in (-1, float("nan"), float("inf"), True, "10", None):
            self.assertIsNone(number(invalid))
        event = {"type": "message_end", "message": {"role": "assistant"}}
        row = parent_stats(self.fixture([event]) / "parent.events.jsonl")
        self.assertIsNone(row["usage"]["input"])
        self.assertIsNone(row["usage"]["cost"])

    def test_tools_retries_and_compaction_have_separate_counts(self):
        events = [
            {"type": "tool_execution_start", "toolCallId": "one"},
            {"type": "tool_execution_end", "toolCallId": "one", "isError": True},
            {"type": "auto_retry_start"},
            {"type": "compaction_end", "result": {"usage": USAGE}},
        ]
        row = parent_stats(self.fixture(events) / "parent.events.jsonl")
        self.assertEqual((row["tool_calls"], row["tool_errors"], row["retries"], row["compactions"]), (1, 1, 1, 1))
        self.assertEqual(row["usage"]["input"], 0)
        self.assertEqual(row["compaction_usage"]["input"], 100)
