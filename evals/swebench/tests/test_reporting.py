import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.swebench.common import DATASET, fingerprint, read_json, runtime_record, write_json
from evals.swebench.reporting import compare, distribution, file_hash, grade_run, predictions, summarize_run


class ReportingTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        run = Path(temporary.name).resolve() / "run-1"
        (run / "build").mkdir(parents=True)
        tasks = [{"instance_id": f"owner__repo-{i}", "repo": "owner/repo", "base_commit": "a" * 40,
                  "image": "example:fixed", "problem_statement": "Fix it",
                  "problem_sha256": hashlib.sha256(b"Fix it").hexdigest()}
                 for i in (1, 2)]
        versions = {"pi": "1.0.0", "pi-provider-litellm": "1.0.0", "pi-subagents": "1.0.0"}
        # read_manifest requires build/runtime.json to match the manifest and be part of the hash.
        write_json(run / "build/runtime.json", runtime_record(versions, "node:22-fixed", tasks))
        manifest = {"schema_version": 1, "run_id": "run-1", "config_id": "pepi-test", "dataset": DATASET,
                    "split": "test", "profile_sha256": fingerprint(run / "build"), "versions": versions,
                    "node_image": "node:22-fixed", "tasks": tasks}
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
        unselected = run / 'task-source/tasks/not__selected-1'
        unselected.mkdir()
        (unselected / 'test.patch').write_text('synthetic unselected input')
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
        self.assertEqual({path.name for path in (Path(args['task_repo']) / 'tasks').iterdir()},
                         {task['instance_id'] for task in manifest['tasks']})
        self.assertEqual(Path(execute.call_args.args[0][1]).name, 'grade_driver.py')
        self.assertFalse(args["rewrite_reports"])

    def test_pairing_refuses_mismatched_tasks_or_budgets(self):
        run, _ = self.fixture()
        baseline = summarize_run(run)
        for key in ("task_identity", "dataset"):
            candidate = copy.deepcopy(baseline)
            candidate[key] = "different"
            with self.assertRaisesRegex(ValueError, f"different {key}; do not silently intersect"):
                compare(baseline, candidate)
        candidate = copy.deepcopy(baseline)
        candidate["protocol"] = "different"
        with self.assertRaisesRegex(ValueError, "malformed execution protocol"):
            compare(baseline, candidate)

    def real_protocol(self, **changes):
        # Shape of execution.json from smoke-20261017-1109 (no forwarding, no auth.json).
        protocol = {"timeout_seconds": 300.0, "network": "pepi-eval", "network_policy": "operator-managed; not verified",
                    "cpus": 4.0, "memory": "8g", "concurrency": 1, "host": "macOS-26.7.1-arm64-arm-64bit",
                    "pids_limit": 512, "timeout_scope": "Pi invocation; setup and export are outside this deadline",
                    "timeout_patch_policy": "submit partial diff after stopping all agent processes",
                    "secrets": {"auth.json": {"present": False}, "mcp.json": {"present": False}},
                    "config_label": None, "mcp": {"enabled": True, "commands": []}}
        protocol.update(changes)
        return protocol

    def paired(self, baseline_protocol, candidate_protocol):
        run, _ = self.fixture()
        baseline = summarize_run(run)
        candidate = copy.deepcopy(baseline)
        candidate["run_id"] = "candidate"
        baseline["protocol"], candidate["protocol"] = baseline_protocol, candidate_protocol
        return baseline, candidate

    def test_pairing_reports_forwarded_env_and_auth_presence_as_configuration(self):
        candidate_protocol = self.real_protocol(
            secrets={"auth.json": {"present": True}, "mcp.json": {"present": False}},
            forwarded_env_names=["TYPESAFE_API_KEY"])
        result = compare(*self.paired(self.real_protocol(), candidate_protocol))
        self.assertEqual(result["configuration_differences"], {
            "forwarded_env_names": {"baseline": [], "candidate": ["TYPESAFE_API_KEY"]},
            "secrets": {"baseline": {"auth.json": {"present": False}, "mcp.json": {"present": False}},
                        "candidate": {"auth.json": {"present": True}, "mcp.json": {"present": False}}}})
        self.assertEqual(list(result["configuration_differences"]), ["forwarded_env_names", "secrets"])

    def test_pairing_rejects_measurement_protocol_mismatch_naming_keys(self):
        with self.assertRaisesRegex(ValueError, "different execution protocol: timeout_seconds, cpus$"):
            compare(*self.paired(self.real_protocol(), self.real_protocol(timeout_seconds=600.0, cpus=2.0)))
        legacy = self.real_protocol()
        del legacy["pids_limit"]
        with self.assertRaisesRegex(ValueError, "different execution protocol: pids_limit$"):
            compare(*self.paired(legacy, self.real_protocol()))
        both_legacy = copy.deepcopy(legacy)
        self.assertEqual(compare(*self.paired(legacy, both_legacy))["configuration_differences"], {})

    def test_pairing_treats_absent_forwarded_env_names_as_empty(self):
        result = compare(*self.paired(self.real_protocol(), self.real_protocol(forwarded_env_names=[])))
        self.assertEqual(result["configuration_differences"], {})
        result = compare(*self.paired(self.real_protocol(), self.real_protocol(forwarded_env_names=["ALPHA"])))
        self.assertEqual(result["configuration_differences"],
                         {"forwarded_env_names": {"baseline": [], "candidate": ["ALPHA"]}})

    def test_pairing_treats_absent_mcp_servers_as_empty(self):
        result = compare(*self.paired(self.real_protocol(), self.real_protocol(mcp_servers=[])))
        self.assertEqual(result["configuration_differences"], {})
        result = compare(*self.paired(self.real_protocol(), self.real_protocol(mcp_servers=["docs"])))
        self.assertEqual(result["configuration_differences"],
                         {"mcp_servers": {"baseline": [], "candidate": ["docs"]}})

    def test_pairing_reports_config_label_and_unknown_keys_safely(self):
        candidate_protocol = self.real_protocol(config_label="x" * 500, extra={"nested": [1, float("nan")]})
        result = compare(*self.paired(self.real_protocol(), candidate_protocol))
        differences = result["configuration_differences"]
        self.assertEqual(list(differences), ["config_label", "extra"])
        self.assertEqual(differences["config_label"], {"baseline": None, "candidate": "x" * 200})
        self.assertEqual(differences["extra"], {"baseline": None,
                                                "candidate": {"nested": [1, "<unsupported value>"]}})
        json.dumps(differences, allow_nan=False)

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

    def test_summary_schema_is_stable(self):
        run, _ = self.fixture()
        result = summarize_run(run)
        # Top-level keys that must always be present
        required_keys = {
            "schema_version", "run_id", "config_id", "dataset", "task_identity", "protocol",
            "selected", "resolved", "resolved_fraction", "unknown_grades",
            "grader_exit_code", "grading_seconds",
            "latency_all_seconds", "latency_resolved_seconds", "turns_observed",
            "available_accounting_tasks", "observed_estimated_cost_usd", "estimated_total_cost_usd",
            "estimated_cost_per_resolved", "cost_caveat", "instances"
        }
        self.assertEqual(set(result.keys()), required_keys)
        # Per-instance keys that must always be present
        required_instance_keys = {
            "instance_id", "resolved", "grading_status", "agent_status",
            "agent_wall_seconds", "setup_seconds", "turns",
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "estimated_cost_usd", "accounting_status"
        }
        for instance in result["instances"]:
            self.assertEqual(set(instance.keys()), required_instance_keys)
