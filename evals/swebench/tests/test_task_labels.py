"""Public reporting labels, never hidden task metadata or model calls."""
import json
import unittest
from pathlib import Path

from evals.swebench import task_labels
from evals.swebench.prepare import sanitize_tasks
from evals.swebench.tests.test_runner import TASK


class TaskLabelsTests(unittest.TestCase):
    def test_catalog_is_exactly_public_issue_titles_with_provenance(self):
        expected = {
            15027: "wasi-http: Ensure buffered body reads aren't dropped when guests pass a host-provided body",
            15028: "wasi-http: Report error when guests trap while streaming body",
            15033: "Fix a debug-only assert linking assert",
            15043: "CM+GC: Add support for flat scalars",
            14979: "Add Global::get_numeric taking AsContext",
            14961: "wasi-http: Add benchmarks for individual wasi:http p3 APIs",
            14902: "serve: reject --addr with --systemd-listenfd",
        }
        preset = json.loads((Path(__file__).resolve().parents[1] / "presets/wasmtime-safe-v1.json").read_text())
        self.assertEqual(set(task_labels.CATALOG), set(preset["tasks"]))
        for issue, title in expected.items():
            instance_id = f"bytecodealliance__wasmtime-{issue}-safe-v1-l8rcyy"
            metadata = task_labels.display_metadata(instance_id)
            self.assertEqual(metadata, {"display_label": title,
                "source_url": f"https://github.com/bytecodealliance/wasmtime/issues/{issue}"})
            self.assertEqual(task_labels.CATALOG[instance_id], metadata)
            self.assertNotIn("display_label", preset["tasks"][instance_id])

    def test_saved_metadata_and_legacy_unknown_fallback(self):
        instance_id = next(iter(task_labels.CATALOG))
        saved = {"display_label": "Saved public title", "source_url": "https://github.com/o/r/issues/1"}
        self.assertEqual(task_labels.display_metadata(instance_id, saved), saved)
        self.assertEqual(task_labels.display_metadata(instance_id, {}), task_labels.CATALOG[instance_id])
        self.assertEqual(task_labels.display_metadata("unknown"), {"display_label": None, "source_url": None})

    def test_rejects_malformed_control_and_secret_labels(self):
        for label in (None, [], 4, "", "  ", "x" * 301, "a\nb", "a\tb", "a\0b", "a\u202eb", "a\u2028b",
                      "token: private-label-value", "api_key=private-label-value"):
            with self.subTest(label=label):
                self.assertIsNone(task_labels.display_metadata("unknown", {"display_label": label})["display_label"])
        for url in ("javascript:alert(1)", "https://github.com/o/r/issues/1)bad", "https://user:pass@github.com/o/r/issues/1",
                    "https://evil.invalid/o/r/issues/1", "https://github.com/o/r/issues/1#token=secret", [], None):
            with self.subTest(url=url):
                self.assertEqual(task_labels.display_metadata("unknown", {"display_label": "Public title", "source_url": url}),
                                 {"display_label": "Public title", "source_url": None})

    def test_csv_formulas_are_inert_without_changing_normal_titles(self):
        for label in ("=1+1", "+SUM(A1)", "-1+2", "@SUM(A1)", "  =1+1"):
            self.assertEqual(task_labels.csv_label(label), "'" + label)
        for label in (None, "", "CM+GC: Add support for flat scalars", 'Title, with "quotes"'):
            self.assertEqual(task_labels.csv_label(label), label)

    def test_reporting_fields_do_not_enter_task_inputs(self):
        task = {**TASK, "display_label": "Never feed this title to a task", "source_url": "https://github.com/o/r/issues/1"}
        self.assertEqual(sanitize_tasks([task], [task["instance_id"]]), sanitize_tasks([TASK], [TASK["instance_id"]]))


if __name__ == "__main__":
    unittest.main()
