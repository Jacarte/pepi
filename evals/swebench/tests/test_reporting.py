import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.swebench.common import DATASET, fingerprint, read_json, write_json
from evals.swebench.reporting import compare, distribution, file_hash, grade_run, predictions, summarize_run


class ReportingTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        run = Path(temporary.name).resolve() / "run-1"
        (run / "build").mkdir(parents=True)
        tasks = [{"instance_id": f"owner__repo-{i}", "repo": "owner/repo", "base_commit": "a" * 40,
                  "image": "example:fixed", "problem_statement": "Fix it", "problem_sha256": "b" * 64}
                 for i in (1, 2)]
        manifest = {"schema_version": 1, "run_id": "run-1", "config_id": "pepi-test", "dataset": DATASET,
                    "split": "test", "profile_sha256": fingerprint(run / "build"), "tasks": tasks}
        write_json(run / "manifest.json", manifest)
        source = run / "task-source"
        source.mkdir()
        (source / "sweb.yaml").write_text("datasets: []")
        for task in tasks:
            path = source / "tasks" / task["instance_id"]
            path.mkdir(parents=True)
            (path / "test.patch").write_text("synthetic grader")
        with (run / "predictions.jsonl").open("w") as stream:
            for task in tasks:
                stream.write(json.dumps({"instance_id": task["instance_id"], "model_name_or_path": "pepi-test",
                                         "model_patch": "diff\n"}) + "\n")
        write_json(run / "execution.json", {"timeout_seconds": 100, "concurrency": 1})
        write_json(run / "grading.json", {"predictions_sha256": file_hash(run / "predictions.jsonl"),
            "reports": "grading/test/logs/evaluation/test", "exit_code": 0, "elapsed_seconds": 3,
            "task_repo": "task-source", "grader_sha256": fingerprint(source)})
        reports = run / "grading/test/logs/evaluation/test"
        write_json(reports / "results.json", {"error_ids": ["owner__repo-2"]})
        write_json(reports / "pepi-test/owner__repo-1/report.json", {"owner__repo-1": {"resolved": True}})
        for i, task in enumerate(tasks):
            directory = run / "attempts" / task["instance_id"]
            write_json(directory / "attempt.json", {"agent_status": "completed" if i == 0 else "timeout",
                "timing_seconds": {"agent_wall": 10 + i * 90, "setup": 2}})
            write_json(directory / "telemetry.json", {"accounting_status": "available",
                "total": {"cost": 1 + i, "turns": 5, "input": 100, "output": 20, "cacheRead": 10, "cacheWrite": 0}})
        return run, manifest

    def test_fixed_denominator_and_failed_attempt_cost(self):
        run, _ = self.fixture()
        result = summarize_run(run)
        self.assertEqual((result["resolved"], result["selected"], result["unknown_grades"]), (1, 2, 1))
        self.assertEqual(result["resolved_fraction"], 0.5)
        self.assertEqual(result["estimated_cost_per_resolved"], 3)
        self.assertEqual(result["latency_all_seconds"]["median"], 55)
        self.assertEqual(result["latency_resolved_seconds"]["median"], 10)
        self.assertEqual(result["instances"][1]["grading_status"], "grading_error")
        self.assertTrue((run / "summary.csv").is_file())
        self.assertTrue((run / "summary.md").is_file())

    def test_missing_telemetry_is_unknown_not_free(self):
        run, _ = self.fixture()
        (run / "attempts/owner__repo-2/telemetry.json").unlink()
        result = summarize_run(run)
        self.assertIsNone(result["estimated_total_cost_usd"])
        self.assertIsNone(result["estimated_cost_per_resolved"])
        self.assertEqual(result["observed_estimated_cost_usd"], 1)

    def test_partial_accounting_does_not_produce_a_full_cost_claim(self):
        run, _ = self.fixture()
        path = run / "attempts/owner__repo-2/telemetry.json"
        row = read_json(path)
        row["accounting_status"] = "partial"
        write_json(path, row)
        self.assertIsNone(summarize_run(run)["estimated_total_cost_usd"])

    def test_zero_successes_cost_per_resolved_is_undefined(self):
        run, _ = self.fixture()
        report = run / "grading/test/logs/evaluation/test/pepi-test/owner__repo-1/report.json"
        write_json(report, {"owner__repo-1": {"resolved": False}})
        self.assertIsNone(summarize_run(run)["estimated_cost_per_resolved"])

    def test_modified_predictions_or_grading_inputs_are_rejected(self):
        run, _ = self.fixture()
        path = run / "predictions.jsonl"
        path.write_text(path.read_text().replace("diff", "changed"))
        with self.assertRaises(ValueError):
            summarize_run(run)
        receipt = read_json(run / "grading.json")
        receipt["predictions_sha256"] = file_hash(path)
        write_json(run / "grading.json", receipt)
        (run / "task-source/sweb.yaml").write_text("modified")
        with self.assertRaises(ValueError):
            summarize_run(run)

    def test_duplicate_prediction_ids_are_rejected(self):
        run, manifest = self.fixture()
        path = run / "predictions.jsonl"
        with path.open("a") as stream:
            stream.write(path.read_text().split("\n")[0] + "\n")
        with self.assertRaises(ValueError):
            predictions(run, manifest)

    def test_grade_uses_new_ids_and_host_only_task_snapshots(self):
        run, manifest = self.fixture()
        with patch("evals.swebench.reporting.subprocess.run", return_value=SimpleNamespace(returncode=0)) as execute:
            self.assertEqual(grade_run(run, run / "task-source", 2, 60), 0)
            first = read_json(run / "grading.json")
            self.assertEqual(grade_run(run, run / "task-source", 2, 60), 0)
            second = read_json(run / "grading.json")
        self.assertNotEqual(first["run_id"], second["run_id"])
        args = read_json(Path(execute.call_args.args[0][-1]))
        self.assertTrue(Path(args["task_repo"]).is_relative_to(run / "grading"))
        self.assertEqual(args["instance_ids"], [task["instance_id"] for task in manifest["tasks"]])
        self.assertEqual(args["expected_tasks"], manifest["tasks"])
        self.assertFalse(args["rewrite_reports"])

    def test_pairing_refuses_mismatched_tasks_or_budgets(self):
        run, _ = self.fixture()
        baseline = summarize_run(run)
        for key in ("task_identity", "protocol", "dataset"):
            candidate = copy.deepcopy(baseline)
            candidate[key] = "different"
            with self.assertRaises(ValueError):
                compare(baseline, candidate)

    def test_pairing_does_not_label_unknown_grade_a_regression(self):
        run, _ = self.fixture()
        baseline = summarize_run(run)
        candidate = copy.deepcopy(baseline)
        candidate["run_id"] = "candidate"
        candidate["instances"][0]["agent_wall_seconds"] = 8
        candidate["instances"][1]["resolved"] = True
        candidate["resolved"] = 2
        result = compare(baseline, candidate)
        self.assertEqual(result["both_resolved_latency_delta_median_seconds"], -2)
        self.assertEqual(result["wins"], [])
        self.assertEqual(result["inconclusive"], ["owner__repo-2"])

    def test_small_sample_percentile_and_missing_counts(self):
        self.assertEqual(distribution([1, 2, 3, None])["p95"], 3)
        self.assertEqual(distribution([1, None])["missing"], 1)
        self.assertIsNone(distribution([])["median"])
