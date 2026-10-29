import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evals.swebench.common import DATASET, fingerprint, read_json, runtime_record, write_json
from evals.swebench.reporting import (RESTORED_DIRECTORY, RESTORED_RECORD, STRICT_DIRECTORY, STRICT_RECORD, compare,
                                      distribution, file_hash, grade_run, main, predictions, summarize_run)
from evals.swebench.tests.test_testpaths import EXISTING_TEST, NEW_TEST, SOURCE, block as test_block


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

    def test_history_pruning_outcome_is_listed_and_unpruned_tasks_are_noted(self):
        run, _ = self.fixture()
        result = summarize_run(run)
        self.assertEqual([row["history_pruning"] for row in result["instances"]], [None, None])  # older attempts
        self.assertEqual(result["tasks_history_not_pruned"], 0)
        self.assertNotIn("git history", (run / "summary.md").read_text())
        for instance_id, status in (("owner__repo-1", "pruned"), ("owner__repo-2", "refs_only")):
            path = run / "attempts" / instance_id / "attempt.json"
            write_json(path, {**read_json(path), "history_pruning": {"status": status}})
        result = summarize_run(run)
        self.assertEqual([row["history_pruning"] for row in result["instances"]], ["pruned", "refs_only"])
        self.assertEqual(result["tasks_history_not_pruned"], 1)
        self.assertIn("Note: 1 task(s) ran with git history after the task base not fully removed",
                      (run / "summary.md").read_text())
        self.assertIn("history_pruning", (run / "summary.csv").read_text().splitlines()[0])

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

    def write_prediction_model(self, run, manifest, model_name):
        with (run / "predictions.jsonl").open("w") as stream:
            for task in manifest["tasks"]:
                stream.write(json.dumps({"instance_id": task["instance_id"], "model_name_or_path": model_name,
                                         "model_patch": ""}) + "\n")

    def test_predictions_accept_exact_labeled_model_name(self):
        run, manifest = self.fixture()
        for label in ("main", "candidate", "variant-b"):
            with self.subTest(label=label):
                manifest["config"] = {"label": label}
                model_name = f"{label}-{manifest['config_id']}"
                self.write_prediction_model(run, manifest, model_name)
                submitted = predictions(run, manifest)
                self.assertEqual(set(submitted), {task["instance_id"] for task in manifest["tasks"]})
                self.assertTrue(all(row["model_name_or_path"] == model_name for row in submitted.values()))

    def test_predictions_preserve_unlabeled_model_name(self):
        run, manifest = self.fixture()
        self.assertEqual(len(predictions(run, manifest)), len(manifest["tasks"]))  # Legacy: no config key.
        for config in ({}, {"label": None}, {"label": ""}):
            with self.subTest(config=config):
                manifest["config"] = config
                self.assertEqual(len(predictions(run, manifest)), len(manifest["tasks"]))

    def test_predictions_reject_wrong_label_or_config_id(self):
        run, manifest = self.fixture()
        for label in (None, "main", "candidate"):
            if label is None:
                manifest.pop("config", None)
            else:
                manifest["config"] = {"label": label}
            expected = f"{label}-{manifest['config_id']}" if label else manifest["config_id"]
            for model_name in (manifest["config_id"], "main-pepi-test", "candidate-pepi-test",
                               "other-pepi-test", "pepi-other", f"{label}-pepi-other",
                               f"prefix-{expected}", f"{expected}-suffix", "", None, 123):
                if model_name == expected:
                    continue
                with self.subTest(label=label, model_name=model_name):
                    self.write_prediction_model(run, manifest, model_name)
                    with self.assertRaisesRegex(ValueError, "wrong config ID"):
                        predictions(run, manifest)

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

    def pin_task_inputs(self, run, manifest):
        """A preset run (Multilingual10) records per-task grading-input fingerprints at prepare."""
        from evals.swebench.reporting import task_input_fingerprints
        ids = [task["instance_id"] for task in manifest["tasks"]]
        manifest = dict(manifest, task_input_fingerprints=task_input_fingerprints(run / "task-source", ids))
        write_json(run / "manifest.json", manifest)
        return manifest

    def test_grade_accepts_the_pinned_task_inputs_and_refuses_changed_ones_before_grading(self):
        run, manifest = self.fixture()
        self.pin_task_inputs(run, manifest)
        with patch("evals.swebench.reporting.subprocess.run", return_value=SimpleNamespace(returncode=0)) as execute:
            self.assertEqual(grade_run(run, run / "task-source", 1, 60), 0)
        self.assertEqual(execute.call_count, 1)
        official = read_json(run / "grading.json")
        grading_dirs = sorted((run / "grading").iterdir())
        (run / "task-source/tasks/owner__repo-2/test.patch").write_text("SECRET-ISH CHANGED GRADER CONTENT")
        for options in ({}, {"strip_test_edits": True}, {"restore_test_files": True}):
            with self.subTest(options=options), \
                    patch("evals.swebench.reporting.subprocess.run") as execute, \
                    self.assertRaisesRegex(ValueError, r"prepared with for: owner__repo-2;") as raised:
                grade_run(run, run / "task-source", 1, 60, **options)
            execute.assert_not_called()
            self.assertNotIn("CHANGED", str(raised.exception))
        self.assertEqual(read_json(run / "grading.json"), official)
        self.assertEqual(sorted((run / "grading").iterdir()), grading_dirs)
        self.assertFalse((run / STRICT_DIRECTORY).exists())
        self.assertFalse((run / RESTORED_DIRECTORY).exists())

    def test_grade_refuses_a_task_changed_while_it_is_copied(self):
        run, manifest = self.fixture()
        self.pin_task_inputs(run, manifest)
        from evals.swebench import reporting
        freeze = reporting.freeze_task_repo

        def racing(task_repo, frozen, tasks):
            result = freeze(task_repo, frozen, tasks)
            (frozen / "tasks/owner__repo-1/test.patch").write_text("swapped during the copy")
            return result

        with patch.object(reporting, "freeze_task_repo", side_effect=racing), \
                patch("evals.swebench.reporting.subprocess.run") as execute, \
                self.assertRaisesRegex(ValueError, "frozen task copy.*owner__repo-1"):
            grade_run(run, run / "task-source", 1, 60)
        execute.assert_not_called()
        self.assertEqual([path.name for path in (run / "grading").iterdir()], ["test"])  # the copy is removed

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

    def with_patches(self, run, patches):
        """Rewrite predictions with real diffs and keep the official grading record matching them."""
        with (run / "predictions.jsonl").open("w") as stream:
            for instance_id, patch_text in patches.items():
                stream.write(json.dumps({"instance_id": instance_id, "model_name_or_path": "pepi-test",
                                         "model_patch": patch_text}) + "\n")
        grading = read_json(run / "grading.json")
        grading["predictions_sha256"] = file_hash(run / "predictions.jsonl")
        write_json(run / "grading.json", grading)

    def strict_fixture(self, patches):
        run, manifest = self.fixture()
        self.with_patches(run, patches)
        official = {path.relative_to(run): path.read_bytes() for path in run.rglob("*")
                    if path.is_file() and path.is_relative_to(run / "grading") or path == run / "grading.json"}
        return run, manifest, official

    def fake_strict_report(self, run, instance_id, resolved):
        record = read_json(run / STRICT_RECORD)
        write_json(run / record["reports"] / "pepi-test" / instance_id / "report.json",
                   {instance_id: {"resolved": resolved}})

    def test_summary_flags_patches_that_modify_existing_tests(self):
        run, _ = self.fixture()
        self.with_patches(run, {"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": SOURCE + NEW_TEST})
        result = summarize_run(run)
        first, second = result["instances"]
        self.assertEqual((first["test_edit_kind"], first["test_files_touched"], first["patch_touches_tests"]),
                         ("modified_existing", ["tests/migrations/test_loader.py"], True))
        self.assertEqual((second["test_edit_kind"], second["test_files_added"]), ("added_only", 1))
        self.assertEqual((result["tasks_modifying_existing_tests"], result["resolved_with_test_edits"]), (1, 1))
        self.assertNotIn("resolved_strict", result)
        self.assertNotIn("resolved_strict", first)
        markdown = (run / "summary.md").read_text()
        self.assertIn("Note: 1 of 1 resolved tasks modified existing test files; regrade with "
                      "`reporting grade --strip-test-edits` for a strict score.", markdown)
        self.assertNotIn("strict, test edits removed", markdown)
        self.assertIn("tests/migrations/test_loader.py", (run / "summary.csv").read_text())

    def test_summary_without_test_edits_has_no_warning(self):
        run, _ = self.fixture()
        self.with_patches(run, {"owner__repo-1": SOURCE, "owner__repo-2": ""})
        result = summarize_run(run)
        self.assertEqual([row["test_edit_kind"] for row in result["instances"]], ["none", "none"])
        self.assertEqual(result["instances"][1]["grading_status"], "empty_patch")
        self.assertNotIn("Note:", (run / "summary.md").read_text())

    def test_strict_regrade_grades_only_modified_existing_tasks_without_touching_official_grading(self):
        run, manifest, official = self.strict_fixture(
            {"owner__repo-1": SOURCE + EXISTING_TEST + NEW_TEST, "owner__repo-2": SOURCE + NEW_TEST})
        with patch("evals.swebench.reporting.subprocess.run", return_value=SimpleNamespace(returncode=0)) as execute:
            self.assertEqual(grade_run(run, run / "task-source", 2, 60, strip_test_edits=True), 0)
        arguments = read_json(Path(execute.call_args.args[0][-1]))
        record = read_json(run / STRICT_RECORD)
        self.assertIn("-strict-", arguments["run_id"])
        self.assertEqual(arguments["run_id"], record["run_id"])
        self.assertEqual((arguments["instance_ids"], record["selected_ids"], record["graded_ids"]),
                         (["owner__repo-1"], ["owner__repo-1"], ["owner__repo-1"]))
        self.assertEqual(arguments["expected_tasks"], manifest["tasks"][:1])
        stripped = [json.loads(line) for line in Path(arguments["predictions_path"]).read_text().splitlines()]
        self.assertEqual(stripped, [{"instance_id": "owner__repo-1", "model_name_or_path": "pepi-test",
                                     "model_patch": SOURCE}])
        self.assertTrue(Path(arguments["predictions_path"]).is_relative_to(run / STRICT_DIRECTORY))
        self.assertEqual({path.name for path in (Path(arguments["task_repo"]) / "tasks").iterdir()},
                         {"owner__repo-1"})
        self.assertEqual(record["removed_test_file_blocks"], {"owner__repo-1": 2})
        self.assertEqual({path.relative_to(run): path.read_bytes() for path in run.rglob("*")
                          if path.is_file() and path.is_relative_to(run / "grading") or path == run / "grading.json"},
                         official)

        self.fake_strict_report(run, "owner__repo-1", False)
        result = summarize_run(run)
        first, second = result["instances"]
        self.assertEqual((first["resolved"], first["resolved_strict"], first["grading_status_strict"]),
                         (True, False, "unresolved"))
        # Outside the strict selection the official outcome is reused (here an evaluator error).
        self.assertEqual((second["resolved_strict"], second["grading_status_strict"]), (None, "grading_error"))
        self.assertEqual((result["resolved"], result["resolved_strict"], result["unknown_grades_strict"]), (1, 0, 1))
        self.assertEqual(result["strict_grading"]["selected_tasks"], 1)
        markdown = (run / "summary.md").read_text()
        self.assertIn("Resolved: **1/2 (50.0%)** (strict, test edits removed: 0/2; 1 unknown).", markdown)
        self.assertIn("Note: 1 of 1 resolved tasks modified existing test files; see strict score.", markdown)

    def test_strict_regrade_of_a_test_only_patch_is_an_empty_patch_without_grading(self):
        run, _, _ = self.strict_fixture({"owner__repo-1": EXISTING_TEST, "owner__repo-2": SOURCE})
        with patch("evals.swebench.reporting.subprocess.run") as execute:
            self.assertEqual(grade_run(run, run / "task-source", 2, 60, strip_test_edits=True), 0)
        execute.assert_not_called()
        record = read_json(run / STRICT_RECORD)
        self.assertEqual((record["graded_ids"], record["empty_after_strip_ids"]), ([], ["owner__repo-1"]))
        first = summarize_run(run)["instances"][0]
        self.assertEqual((first["resolved_strict"], first["grading_status_strict"]), (False, "empty_patch"))

    def test_strict_regrade_without_test_edits_equals_the_official_score(self):
        run, _, _ = self.strict_fixture({"owner__repo-1": SOURCE, "owner__repo-2": SOURCE + NEW_TEST})
        with patch("evals.swebench.reporting.subprocess.run") as execute:
            grade_run(run, run / "task-source", 2, 60, strip_test_edits=True)
        execute.assert_not_called()
        result = summarize_run(run)
        self.assertEqual((result["resolved"], result["resolved_strict"]), (1, 1))

    def test_stale_strict_regrade_is_refused(self):
        run, _, _ = self.strict_fixture({"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": SOURCE})
        with patch("evals.swebench.reporting.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            grade_run(run, run / "task-source", 2, 60, strip_test_edits=True)
        self.with_patches(run, {"owner__repo-1": SOURCE + EXISTING_TEST + "\n", "owner__repo-2": SOURCE})
        with self.assertRaisesRegex(ValueError, "strict regrade"):
            summarize_run(run)
        self.with_patches(run, {"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": SOURCE})
        record = read_json(run / STRICT_RECORD)
        (run / record["predictions"]).write_text("{}\n")
        with self.assertRaisesRegex(ValueError, "Strict regrade predictions changed"):
            summarize_run(run)

    def test_strict_regrade_refuses_task_inputs_that_differ_from_the_official_grading(self):
        run, _, _ = self.strict_fixture({"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": SOURCE})
        changed = run / "changed-source"
        changed.mkdir()
        (changed / "sweb.yaml").write_text("datasets: []")
        for name, text in (("owner__repo-1", "another grader"), ("owner__repo-2", "synthetic grader")):
            (changed / "tasks" / name).mkdir(parents=True)
            (changed / "tasks" / name / "test.patch").write_text(text)
        with patch("evals.swebench.reporting.subprocess.run") as execute, \
                self.assertRaisesRegex(ValueError, "differ from the official grading for: owner__repo-1;"):
            grade_run(run, changed, 2, 60, strip_test_edits=True)
        execute.assert_not_called()
        self.assertFalse((run / STRICT_DIRECTORY).exists())
        self.assertFalse((run / STRICT_RECORD).exists())

    def test_summary_refuses_a_strict_regrade_mixed_with_a_later_official_grading(self):
        run, _, _ = self.strict_fixture({"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": SOURCE})
        with patch("evals.swebench.reporting.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            grade_run(run, run / "task-source", 2, 60, strip_test_edits=True)
        self.fake_strict_report(run, "owner__repo-1", True)
        self.assertEqual(summarize_run(run)["resolved_strict"], 1)
        record = read_json(run / STRICT_RECORD)
        self.assertEqual(set(record["task_input_fingerprints"]), {"owner__repo-1"})
        # The official grading is redone from a changed task repo after the strict regrade.
        (run / "task-source/tasks/owner__repo-1/test.patch").write_text("another grader")
        grading = read_json(run / "grading.json")
        grading["grader_sha256"] = fingerprint(run / "task-source")
        write_json(run / "grading.json", grading)
        with self.assertRaisesRegex(ValueError, "strict regrade used other grading inputs .*owner__repo-1"):
            summarize_run(run)

    LISTED = ("cd /testbed\ngit checkout " + "b" * 40 + " tests/migrations/test_loader.py tests/new/0001_initial.py\n"
              "git apply -v -\n: '>>>>> Start Test Output'\n./runtests.py\n: '>>>>> End Test Output'\n"
              "git checkout " + "b" * 40 + " tests/migrations/test_loader.py tests/new/0001_initial.py\n")
    NOT_LISTED = "git checkout " + "b" * 40 + " tests/other/test_x.py\n./runtests.py\n"

    def restored_fixture(self, patches, scripts):
        """strict_fixture plus eval.sh per task (None: no eval.sh); the official grading record covers them."""
        run, manifest, _ = self.strict_fixture(patches)
        for instance_id, script in scripts.items():
            if script is not None:
                (run / "task-source/tasks" / instance_id / "eval.sh").write_bytes(
                    script if isinstance(script, bytes) else script.encode())
        grading = read_json(run / "grading.json")
        grading["grader_sha256"] = fingerprint(run / "task-source")
        write_json(run / "grading.json", grading)
        official = {path.relative_to(run): path.read_bytes() for path in run.rglob("*")
                    if path.is_file() and (path.is_relative_to(run / "grading") or path == run / "grading.json"
                                           or path.is_relative_to(run / "task-source"))}
        return run, manifest, official

    def official_bytes(self, run):
        return {path.relative_to(run): path.read_bytes() for path in run.rglob("*")
                if path.is_file() and (path.is_relative_to(run / "grading") or path == run / "grading.json"
                                       or path.is_relative_to(run / "task-source"))}

    def fake_restored_report(self, run, instance_id, resolved):
        record = read_json(run / RESTORED_RECORD)
        write_json(run / record["reports"] / "pepi-test" / instance_id / "report.json",
                   {instance_id: {"resolved": resolved}})

    def test_restored_regrade_repairs_only_its_own_eval_script_copy_and_keeps_the_patch(self):
        patches = {"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": SOURCE + EXISTING_TEST}
        run, manifest, official = self.restored_fixture(
            patches, {"owner__repo-1": self.LISTED, "owner__repo-2": self.NOT_LISTED})
        with patch("evals.swebench.reporting.subprocess.run", return_value=SimpleNamespace(returncode=0)) as execute:
            self.assertEqual(grade_run(run, run / "task-source", 2, 60, restore_test_files=True), 0)
        arguments = read_json(Path(execute.call_args.args[0][-1]))
        record = read_json(run / RESTORED_RECORD)
        self.assertIn("-restored-", arguments["run_id"])
        self.assertEqual(arguments["run_id"], record["run_id"])
        self.assertEqual((arguments["instance_ids"], record["selected_ids"], record["graded_ids"],
                          record["not_listed_ids"], record["unrewritable_ids"]),
                         (["owner__repo-1"], ["owner__repo-1"], ["owner__repo-1"], ["owner__repo-2"], []))
        self.assertEqual(arguments["expected_tasks"], manifest["tasks"][:1])
        graded = [json.loads(line) for line in Path(arguments["predictions_path"]).read_text().splitlines()]
        self.assertEqual(graded, [{"instance_id": "owner__repo-1", "model_name_or_path": "pepi-test",
                                   "model_patch": patches["owner__repo-1"]}])  # unchanged, unlike strict
        frozen = Path(arguments["task_repo"])
        self.assertTrue(frozen.is_relative_to(run / RESTORED_DIRECTORY))
        script = (frozen / "tasks/owner__repo-1/eval.sh").read_text()
        self.assertNotIn("\ngit checkout ", "\n" + script)
        self.assertEqual(script.count("then git checkout " + "b" * 40 + " -- tests/migrations/test_loader.py; fi"), 2)
        self.assertEqual(script.split("\n")[2:5], self.LISTED.split("\n")[2:5])
        self.assertEqual(record["eval_scripts"]["owner__repo-1"]["rewritten_lines"], 2)
        self.assertEqual(record["eval_scripts"]["owner__repo-1"]["original_sha256"],
                         hashlib.sha256(self.LISTED.encode()).hexdigest())
        self.assertEqual(record["eval_scripts"]["owner__repo-1"]["rewritten_sha256"],
                         hashlib.sha256(script.encode()).hexdigest())
        self.assertEqual(record["restore_policy_version"], 2)
        # The task repo it read and the official grading (incl. its frozen copy) are byte-identical.
        self.assertEqual(self.official_bytes(run), official)

        self.fake_restored_report(run, "owner__repo-1", False)
        result = summarize_run(run)
        first, second = result["instances"]
        self.assertEqual((first["resolved"], first["resolved_restored"], first["grading_status_restored"]),
                         (True, False, "unresolved"))
        # not_listed keeps the official outcome (here an evaluator error).
        self.assertEqual((second["resolved_restored"], second["grading_status_restored"]), (None, "grading_error"))
        self.assertEqual((result["resolved_restored"], result["unknown_grades_restored"]), (0, 1))
        self.assertEqual((result["restored_grading"]["regraded_tasks"], result["restored_grading"]["unrewritable_tasks"]),
                         (1, 0))
        self.assertNotIn("resolved_strict", result)
        markdown = (run / "summary.md").read_text()
        self.assertIn("Resolved: **1/2 (50.0%)** (test-checkout repaired: 0/2, 1 unknown).", markdown)
        self.assertIn("modified existing test files; see the test-checkout repaired score.", markdown)

    def test_restored_regrade_marks_unrewritable_or_missing_eval_scripts_unknown_without_grading(self):
        quoted = self.LISTED.replace("tests/new/0001_initial.py", "'tests/new/0001 initial.py'", 1)
        run, _, _ = self.restored_fixture({"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": EXISTING_TEST},
                                          {"owner__repo-1": quoted, "owner__repo-2": None})
        with patch("evals.swebench.reporting.subprocess.run") as execute:
            self.assertEqual(grade_run(run, run / "task-source", 2, 60, restore_test_files=True), 0)
        execute.assert_not_called()
        record = read_json(run / RESTORED_RECORD)
        self.assertEqual((record["graded_ids"], record["unrewritable_ids"]), ([], ["owner__repo-1", "owner__repo-2"]))
        self.assertEqual(list((run / RESTORED_DIRECTORY / record["run_id"]).iterdir()),
                         [run / RESTORED_DIRECTORY / record["run_id"] / "grading.json"])
        result = summarize_run(run)
        self.assertEqual([(row["resolved_restored"], row["grading_status_restored"]) for row in result["instances"]],
                         [(None, "not_graded"), (None, "not_graded")])
        self.assertEqual((result["resolved"], result["resolved_restored"], result["unknown_grades_restored"]), (1, 0, 2))

    def test_restored_regrade_without_affected_tasks_equals_the_official_score(self):
        run, _, _ = self.restored_fixture({"owner__repo-1": SOURCE, "owner__repo-2": SOURCE + NEW_TEST},
                                          {"owner__repo-1": self.LISTED, "owner__repo-2": self.LISTED})
        with patch("evals.swebench.reporting.subprocess.run") as execute:
            grade_run(run, run / "task-source", 2, 60, restore_test_files=True)
        execute.assert_not_called()
        result = summarize_run(run)
        self.assertEqual((result["resolved"], result["resolved_restored"]), (1, 1))
        self.assertEqual([row["grading_status_restored"] for row in result["instances"]],
                         [row["grading_status"] for row in result["instances"]])

    def test_stale_or_mixed_restored_regrade_is_refused(self):
        patches = {"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": SOURCE}
        run, _, _ = self.restored_fixture(patches, {"owner__repo-1": self.LISTED, "owner__repo-2": self.LISTED})
        with patch("evals.swebench.reporting.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            grade_run(run, run / "task-source", 2, 60, restore_test_files=True)
        self.fake_restored_report(run, "owner__repo-1", True)
        self.assertEqual(summarize_run(run)["resolved_restored"], 1)
        self.with_patches(run, {**patches, "owner__repo-1": patches["owner__repo-1"] + "\n"})
        with self.assertRaisesRegex(ValueError, "Predictions changed after the restored regrade"):
            summarize_run(run)
        self.with_patches(run, patches)
        record = read_json(run / RESTORED_RECORD)
        write_json(run / RESTORED_RECORD, {**record, "restore_policy_version": 0})
        with self.assertRaisesRegex(ValueError, "restored regrade used another test-file policy"):
            summarize_run(run)
        write_json(run / RESTORED_RECORD, record)
        # The official grading is redone from a changed task repo after the restored regrade.
        (run / "task-source/tasks/owner__repo-1/test.patch").write_text("another grader")
        grading = read_json(run / "grading.json")
        grading["grader_sha256"] = fingerprint(run / "task-source")
        write_json(run / "grading.json", grading)
        with self.assertRaisesRegex(ValueError, "restored regrade used other grading inputs .*owner__repo-1"):
            summarize_run(run)

    def test_restored_regrade_refuses_a_task_repo_that_differs_from_the_official_grading(self):
        run, _, _ = self.restored_fixture({"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": SOURCE},
                                          {"owner__repo-1": self.LISTED, "owner__repo-2": self.LISTED})
        changed = run / "changed-source"
        shutil.copytree(run / "task-source", changed)
        (changed / "tasks/owner__repo-1/eval.sh").write_text(self.NOT_LISTED + "# edited\n")
        with patch("evals.swebench.reporting.subprocess.run") as execute, \
                self.assertRaisesRegex(ValueError, "differ from the official grading for: owner__repo-1;"):
            grade_run(run, changed, 2, 60, restore_test_files=True)
        execute.assert_not_called()
        self.assertFalse((run / RESTORED_DIRECTORY).exists())
        self.assertFalse((run / RESTORED_RECORD).exists())
        with self.assertRaisesRegex(ValueError, "Choose one of"):
            grade_run(run, run / "task-source", 2, 60, strip_test_edits=True, restore_test_files=True)

    def test_strict_and_restored_scores_are_reported_together(self):
        run, _, _ = self.restored_fixture({"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": SOURCE},
                                          {"owner__repo-1": self.LISTED, "owner__repo-2": self.LISTED})
        with patch("evals.swebench.reporting.subprocess.run", return_value=SimpleNamespace(returncode=0)):
            grade_run(run, run / "task-source", 2, 60, strip_test_edits=True)
            grade_run(run, run / "task-source", 2, 60, restore_test_files=True)
        self.fake_strict_report(run, "owner__repo-1", False)
        self.fake_restored_report(run, "owner__repo-1", True)
        result = summarize_run(run)
        self.assertEqual((result["resolved"], result["resolved_strict"], result["resolved_restored"]), (1, 0, 1))
        markdown = (run / "summary.md").read_text()
        self.assertIn("(strict, test edits removed: 0/2; 1 unknown; test-checkout repaired: 1/2, 1 unknown).",
                      markdown)
        self.assertIn("see strict and test-checkout repaired scores.", markdown)

    def test_labeled_summary_uses_official_model_name_but_bare_variant_config_id(self):
        for label in ("main", "candidate"):
            with self.subTest(label=label):
                run, manifest, _ = self.restored_fixture(
                    {"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": SOURCE},
                    {"owner__repo-1": self.LISTED, "owner__repo-2": self.LISTED})
                manifest["config"] = {"label": label}
                write_json(run / "manifest.json", manifest)
                model_name = f"{label}-{manifest['config_id']}"
                submitted = [json.loads(line) for line in (run / "predictions.jsonl").read_text().splitlines()]
                with (run / "predictions.jsonl").open("w") as stream:
                    for row in submitted:
                        stream.write(json.dumps({**row, "model_name_or_path": model_name}) + "\n")
                grading = read_json(run / "grading.json")
                grading["predictions_sha256"] = file_hash(run / "predictions.jsonl")
                write_json(run / "grading.json", grading)
                reports = run / grading["reports"]
                (reports / manifest["config_id"]).rename(reports / model_name)
                write_json(reports / model_name / "owner__repo-2/report.json",
                           {"owner__repo-2": {"resolved": True}})
                write_json(reports / "results.json", {"error_ids": []})

                with patch("evals.swebench.reporting.subprocess.run", return_value=SimpleNamespace(returncode=0)):
                    self.assertEqual(grade_run(run, run / "task-source", 2, 60, strip_test_edits=True), 0)
                    self.assertEqual(grade_run(run, run / "task-source", 2, 60, restore_test_files=True), 0)
                for record_path in (STRICT_RECORD, RESTORED_RECORD):
                    record = read_json(run / record_path)
                    rows = [json.loads(line) for line in (run / record["predictions"]).read_text().splitlines()]
                    self.assertEqual([row["model_name_or_path"] for row in rows], [manifest["config_id"]])
                self.fake_strict_report(run, "owner__repo-1", False)
                self.fake_restored_report(run, "owner__repo-1", True)

                result = summarize_run(run)
                self.assertEqual((result["resolved"], result["resolved_strict"], result["resolved_restored"]), (2, 1, 2))
                self.assertEqual((result["unknown_grades"], result["unknown_grades_strict"],
                                  result["unknown_grades_restored"]), (0, 0, 0))
                self.assertEqual(result["resolved_fraction"], 1)
                first, second = result["instances"]
                self.assertEqual((first["grading_status"], first["grading_status_strict"],
                                  first["grading_status_restored"]), ("resolved", "unresolved", "resolved"))
                self.assertEqual((second["grading_status"], second["grading_status_strict"],
                                  second["grading_status_restored"]), ("resolved", "resolved", "resolved"))

    def test_test_checkout_command_prints_regraded_and_unrewritable_counts(self):
        run, _, _ = self.restored_fixture({"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": SOURCE + EXISTING_TEST},
                                          {"owner__repo-1": self.LISTED, "owner__repo-2": self.NOT_LISTED})
        for extra in ([], ["--task-repo", str(run / "task-source")]):
            with self.subTest(extra=extra), patch("sys.argv", ["reporting", "test-checkout", str(run), *extra]), \
                    patch("builtins.print") as printed:
                self.assertEqual(main(), 0)
            printed.assert_called_once_with("1 0")
        # An eval script that cannot be repaired is counted separately (its restored grade is unknown).
        unrepairable = run / "unrepairable-source"
        shutil.copytree(run / "task-source", unrepairable)
        (unrepairable / "tasks/owner__repo-2/eval.sh").write_text("cd /testbed && git checkout " + "b" * 40 + " x.py\n")
        with patch("sys.argv", ["reporting", "test-checkout", str(run), "--task-repo", str(unrepairable)]), \
                patch("builtins.print") as printed:
            self.assertEqual(main(), 0)
        printed.assert_called_once_with("1 1")

    def test_restored_selection_uses_every_existing_file_the_checkout_lists(self):
        # The checkout line is authoritative: a listed fixture the test-path heuristic does not call a test
        # file is still reset by the repair, so a patch editing it is regraded (strict ignores it).
        listed_fixture = ("git checkout " + "b" * 40 + " data/fixture.json tests/new/0001_initial.py\n./runtests.py\n"
                          "git checkout " + "b" * 40 + " data/fixture.json tests/new/0001_initial.py\n")
        fixture = test_block("data/fixture.json")
        run, _, _ = self.restored_fixture({"owner__repo-1": SOURCE + fixture, "owner__repo-2": SOURCE},
                                          {"owner__repo-1": listed_fixture, "owner__repo-2": listed_fixture})
        with patch("evals.swebench.reporting.subprocess.run", return_value=SimpleNamespace(returncode=0)) as execute:
            self.assertEqual(grade_run(run, run / "task-source", 2, 60, restore_test_files=True), 0)
        record = read_json(run / RESTORED_RECORD)
        self.assertEqual((record["selected_ids"], record["not_listed_ids"], record["unrewritable_ids"]),
                         (["owner__repo-1"], ["owner__repo-2"], []))
        self.assertEqual(read_json(Path(execute.call_args.args[0][-1]))["instance_ids"], ["owner__repo-1"])
        self.fake_restored_report(run, "owner__repo-1", False)
        result = summarize_run(run)
        self.assertEqual((result["instances"][0]["test_edit_kind"], result["instances"][0]["grading_status_restored"]),
                         ("none", "unresolved"))
        self.assertEqual(result["tasks_modifying_existing_tests"], 0)

    def test_pairing_reports_restored_scores(self):
        run, _ = self.fixture()
        baseline = summarize_run(run)
        candidate = copy.deepcopy(baseline)
        candidate["run_id"] = "candidate"
        self.assertNotIn("baseline_resolved_restored", compare(baseline, candidate))
        candidate["resolved_restored"] = 0
        for row, value in zip(candidate["instances"], (False, None)):
            row["resolved_restored"] = value
        one_sided = compare(baseline, candidate)
        self.assertEqual((one_sided["baseline_resolved_restored"], one_sided["candidate_resolved_restored"]), (None, 0))
        self.assertNotIn("restored_wins", one_sided)
        baseline["resolved_restored"] = 1
        for row, value in zip(baseline["instances"], (True, False)):
            row["resolved_restored"] = value
        both = compare(baseline, candidate)
        self.assertEqual((both["restored_wins"], both["restored_regressions"], both["restored_inconclusive"]),
                         ([], ["owner__repo-1"], ["owner__repo-2"]))
        self.assertNotIn("strict_wins", both)

    def test_test_edits_command_prints_the_count(self):
        run, _ = self.fixture()
        self.with_patches(run, {"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": SOURCE + NEW_TEST})
        with patch("sys.argv", ["reporting", "test-edits", str(run)]), \
                patch("builtins.print") as printed:
            self.assertEqual(main(), 0)
        printed.assert_called_once_with(1)

    def test_pairing_reports_strict_scores_and_test_edit_differences(self):
        run, _ = self.fixture()
        self.with_patches(run, {"owner__repo-1": SOURCE + EXISTING_TEST, "owner__repo-2": SOURCE})
        baseline = summarize_run(run)
        candidate = copy.deepcopy(baseline)
        candidate["run_id"] = "candidate"
        candidate["instances"][1]["test_edit_kind"] = "added_only"
        result = compare(baseline, candidate)
        self.assertEqual(result["test_edit_kind_differences"],
                         [{"instance_id": "owner__repo-2", "baseline": "none", "candidate": "added_only"}])
        self.assertNotIn("baseline_resolved_strict", result)
        candidate["resolved_strict"] = 1
        for row, value in zip(candidate["instances"], (False, True)):
            row["resolved_strict"] = value
        one_sided = compare(baseline, candidate)
        self.assertEqual((one_sided["baseline_resolved_strict"], one_sided["candidate_resolved_strict"]), (None, 1))
        self.assertNotIn("strict_wins", one_sided)
        baseline["resolved_strict"] = 1
        for row, value in zip(baseline["instances"], (True, None)):
            row["resolved_strict"] = value
        both = compare(baseline, candidate)
        self.assertEqual((both["strict_wins"], both["strict_regressions"], both["strict_inconclusive"]),
                         ([], ["owner__repo-1"], ["owner__repo-2"]))
        legacy = copy.deepcopy(baseline)
        for row in legacy["instances"]:
            del row["test_edit_kind"]
        self.assertNotIn("test_edit_kind_differences", compare(legacy, candidate))

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
            "estimated_cost_per_resolved", "cost_caveat", "instances",
            "tasks_modifying_existing_tests", "resolved_with_test_edits", "tasks_history_not_pruned",
        }
        self.assertEqual(set(result.keys()), required_keys)
        # Per-instance keys that must always be present
        required_instance_keys = {
            "instance_id", "resolved", "grading_status", "agent_status",
            "agent_wall_seconds", "setup_seconds", "turns",
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "estimated_cost_usd", "accounting_status",
            "patch_touches_tests", "test_files_touched", "test_files_touched_count",
            "test_files_modified_existing", "test_files_added", "test_edit_kind", "history_pruning",
        }
        for instance in result["instances"]:
            self.assertEqual(set(instance.keys()), required_instance_keys)
