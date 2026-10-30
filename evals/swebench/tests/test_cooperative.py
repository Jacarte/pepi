"""Generic profile enablement, arm provenance and external accounting; no model/Docker/network calls."""
import json
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from evals.swebench import ci
from evals.swebench.common import read_json, sha256_file, write_json
from evals.swebench.prepare import COOPERATIVE_FILES, REQUIRED_AGENT_FILES
from evals.swebench.reporting import external_jev_summary
from evals.swebench.telemetry import JEV_CRITERIA, external_jev
from evals.swebench.tests.test_profiles import repository, prepare, REPO

PINS = {"pi": "1.1.0", "pi-subagents": "0.78.0"}


def bundle(root):
    for name in (*REQUIRED_AGENT_FILES, *COOPERATIVE_FILES):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((REPO / name).read_bytes())


def enable(root):
    write_json(root / "settings.json", {"defaultProvider": "anthropic", "defaultModel": "claude-test", "cooperativePlanner": True})


class DeepWorkerProfileTests(unittest.TestCase):
    def test_deep_worker_requires_same_arm_regular_file_without_fallback(self):
        for injected in (False, True):
            for unsafe in ("missing", "symlink", "parent-symlink", "directory", "fifo"):
                with self.subTest(injected=injected, unsafe=unsafe), tempfile.TemporaryDirectory() as directory:
                    base = Path(directory); root = repository(base)
                    arm = base / "arm" if injected else root
                    if injected:
                        arm.mkdir()
                        (arm / "agents").mkdir()
                        (arm / "agents/deep-worker.md").write_text("arm worker")
                    role = arm / "agents/deep-worker.md"
                    role.unlink()
                    if unsafe == "symlink":
                        role.symlink_to(REPO / "agents/deep-worker.md")
                    elif unsafe == "parent-symlink":
                        role.parent.rmdir()
                        role.parent.symlink_to(REPO / "agents", target_is_directory=True)
                    elif unsafe == "directory":
                        role.mkdir()
                    elif unsafe == "fifo":
                        os.mkfifo(role)
                    with self.assertRaisesRegex(ValueError, "regular file|Symlinks"):
                        prepare(root, base / "run", config_dir=arm if injected else None, versions=PINS)
                    self.assertFalse((base / "run").exists())

    def test_assembly_rejects_missing_or_unsafe_deep_worker_without_candidate_fallback(self):
        for unsafe in ("missing", "symlink", "parent-symlink", "directory", "fifo"):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as directory:
                base = Path(directory); root = repository(base)
                shutil.copyfile(REPO / "profile.sh", root / "profile.sh")
                role = root / "agents/deep-worker.md"
                role.unlink()
                if unsafe == "symlink":
                    role.symlink_to(REPO / "agents/deep-worker.md")
                elif unsafe == "parent-symlink":
                    role.parent.rmdir()
                    role.parent.symlink_to(REPO / "agents", target_is_directory=True)
                elif unsafe == "directory":
                    role.mkdir()
                elif unsafe == "fifo":
                    os.mkfifo(role)
                with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
                        ci.UsageError, "regular file|Symlink"):
                    ci.assemble_config(root, "codex", base / "config", fallback_root=REPO)

    def test_deep_worker_bytes_hash_and_identity_are_owned_by_each_arm(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            roots = {}
            for label in ("main", "pr"):
                (base / label).mkdir()
                root = roots[label] = repository(base / label)
                shutil.copyfile(REPO / "profile.sh", root / "profile.sh")
                role = root / "agents/deep-worker.md"
                role.write_text(role.read_text() + f"\n{label}-owned worker instructions.\n")
            records, manifests = {}, {}
            def freeze(label, suffix):
                assembled = base / f"{label}-{suffix}-config"
                with patch.dict(os.environ, {}, clear=True):
                    record = ci.assemble_config(roots[label], "codex", assembled, fallback_root=roots["pr"])
                run = base / f"{label}-{suffix}-run"
                manifest = prepare(roots["pr"], run, config_dir=assembled, versions=PINS)
                name = "agents/deep-worker.md"
                self.assertEqual((assembled / name).read_bytes(), (roots[label] / name).read_bytes())
                self.assertEqual((run / "build/profile" / name).read_bytes(), (roots[label] / name).read_bytes())
                self.assertEqual(manifest["config"]["files"][name], sha256_file(roots[label] / name))
                self.assertIn(name, record["files"])
                self.assertEqual(record["harness_fallback_files"], [])
                return record, manifest
            for label in roots:
                records[label], manifests[label] = freeze(label, "first")
            role = roots["pr"] / "agents/deep-worker.md"
            role.write_text(role.read_text() + "\nCandidate-only role change.\n")
            for label in roots:
                record, manifest = freeze(label, "second")
                compare = self.assertEqual if label == "main" else self.assertNotEqual
                compare(record["sha256"], records[label]["sha256"])
                compare(manifest["config_id"], manifests[label]["config_id"])
                compare(manifest["config"]["files"]["agents/deep-worker.md"],
                        manifests[label]["config"]["files"]["agents/deep-worker.md"])


class CooperativeProfileTests(unittest.TestCase):
    def test_opt_in_exact_bundle_hashes_and_no_baseline_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = repository(base); bundle(root); enable(root)
            candidate = prepare(root, base / "candidate", versions=PINS)
            frozen = base / "candidate/build/profile"
            self.assertIn("./extensions/cooperative-planner/extension.ts", read_json(frozen / "settings.json")["extensions"])
            for name in (*REQUIRED_AGENT_FILES, *COOPERATIVE_FILES):
                self.assertEqual((frozen / name).read_bytes(), (root / name).read_bytes())
                self.assertEqual(candidate["config"]["files"][name], sha256_file(root / name))
            baseline = base / "baseline-config"; baseline.mkdir()
            (baseline / "agents").mkdir()
            (baseline / "agents/deep-worker.md").write_bytes((root / "agents/deep-worker.md").read_bytes())
            write_json(baseline / "settings.json", {"defaultProvider": "anthropic", "defaultModel": "claude-test"})
            old = prepare(root, base / "baseline", config_dir=baseline, versions=PINS)
            self.assertNotIn("cooperative_planner", old["config"])
            self.assertFalse((base / "baseline/build/profile/extensions/cooperative-planner").exists())
            self.assertFalse((base / "baseline/build/profile/agents/planner.md").exists())
            enable(baseline)
            with self.assertRaisesRegex(ValueError, "bundle is incomplete"):
                prepare(root, base / "incomplete", config_dir=baseline, versions=PINS)
            for i, name in enumerate(COOPERATIVE_FILES):
                with (root / name).open("a") as stream: stream.write("\n// identity test\n")
                changed = prepare(root, base / f"changed-{i}", versions=PINS)
                self.assertNotEqual(candidate["config_id"], changed["config_id"])
                candidate = changed

    def test_static_planner_is_required_from_the_same_arm_and_fingerprinted(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = repository(base); bundle(root)
            arm = base / "arm"; arm.mkdir(); bundle(arm); enable(arm)
            planner = arm / "agents/planner.md"
            planner.unlink()
            with self.assertRaisesRegex(ValueError, "incomplete: agents/planner.md"):
                prepare(root, base / "missing-planner", config_dir=arm, versions=PINS)
            planner.symlink_to(root / "agents/planner.md")
            with self.assertRaisesRegex(ValueError, "Symlinks"):
                prepare(root, base / "symlink-planner", config_dir=arm, versions=PINS)
            planner.unlink()
            planner.write_text((root / "agents/planner.md").read_text() + "\nArm-owned planner instructions.\n")
            first = prepare(root, base / "first", config_dir=arm, versions=PINS)
            self.assertEqual((base / "first/build/profile/agents/planner.md").read_text(), planner.read_text())
            self.assertIn("agents/planner.md", first["config"]["cooperative_planner"]["files"])
            planner.write_text(planner.read_text() + "\nChanged role.\n")
            second = prepare(root, base / "second", config_dir=arm, versions=PINS)
            self.assertNotEqual(first["config"]["files"]["agents/planner.md"], second["config"]["files"]["agents/planner.md"])
            self.assertNotEqual(first["config_id"], second["config_id"])

    def test_daily_profiles_freeze_only_the_exact_enabled_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = repository(base, cooperative=True); bundle(root)
            for name in ("extensions/cooperative-planner/unapproved.ts", "extensions/router.ts", "agents/other.md"):
                (root / name).write_text("must not provision arbitrary code or roles")
            self.assertEqual(set(COOPERATIVE_FILES), {
                "extensions/cooperative-planner/extension.ts", "extensions/cooperative-planner/controller.mjs",
                "extensions/cooperative-planner/scorer.mjs", "agents/planner.md"})
            for profile in ("codex", "bedrock"):
                versions = {**PINS, **({"pi-provider-litellm": "4.2.0"} if profile == "bedrock" else {})}
                original = read_json(REPO / f"profiles/{profile}/settings.json")
                self.assertIs(original["cooperativePlanner"], True)
                manifest = prepare(root, base / profile, profile_name=profile, versions=versions)
                frozen = base / profile / "build/profile"
                settings = read_json(frozen / "settings.json")
                self.assertEqual(settings["extensions"], ["./extensions/cooperative-planner/extension.ts"])
                self.assertNotIn("cooperativePlanner", settings)  # freezer input, not a runtime setting
                self.assertEqual(set(manifest["config"]["cooperative_planner"]["files"]), set(COOPERATIVE_FILES))
                for name in (*REQUIRED_AGENT_FILES, *COOPERATIVE_FILES):
                    self.assertEqual((frozen / name).read_bytes(), (root / name).read_bytes())
                    self.assertEqual(manifest["config"]["files"][name], sha256_file(root / name))
                executable_files = {p.relative_to(frozen).as_posix() for folder in ("extensions", "agents")
                                    for p in (frozen / folder).rglob("*") if p.is_file()}
                self.assertEqual(executable_files, {*REQUIRED_AGENT_FILES, *COOPERATIVE_FILES, "extensions/eval-telemetry.ts"})
                self.assertNotIn("subagentOnlyExtensions", json.dumps(settings))
                self.assertNotIn("pi-memory-mem0", settings)
                # An explicit false wins even if the ordinary extension path is present.
                original["cooperativePlanner"] = False
                write_json(root / f"profiles/{profile}/settings.json", original)
                disabled = prepare(root, base / f"{profile}-disabled", profile_name=profile, versions=versions)
                disabled_profile = base / f"{profile}-disabled/build/profile"
                self.assertNotIn("cooperative_planner", disabled["config"])
                self.assertFalse(set(COOPERATIVE_FILES) & set(disabled["config"]["files"]))
                self.assertFalse(any((disabled_profile / name).exists() for name in COOPERATIVE_FILES))
                self.assertNotIn("extensions", read_json(disabled_profile / "settings.json"))
                self.assertEqual((disabled_profile / "agents/deep-worker.md").read_bytes(),
                                 (root / "agents/deep-worker.md").read_bytes())

    def test_symlink_missing_and_wrong_api_pins_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = repository(base); bundle(root); enable(root)
            with self.assertRaisesRegex(ValueError, "inspected"):
                prepare(root, base / "pins")
            source = root / COOPERATIVE_FILES[0]
            source.unlink(); source.symlink_to(root / COOPERATIVE_FILES[1])
            with self.assertRaisesRegex(ValueError, "Symlinks"):
                prepare(root, base / "symlink", versions=PINS)
            source.unlink()
            with self.assertRaisesRegex(ValueError, "incomplete"):
                prepare(root, base / "missing", versions=PINS)

    def test_assembled_old_main_stays_disabled_with_enabled_candidate_harness(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for arm in ("main", "pr"):
                (base / arm).mkdir()
            main = repository(base / "main")
            candidate = repository(base / "pr", cooperative=True)
            for root, rules in ((main, "main generic rules"), (candidate, "candidate generic rules")):
                shutil.copyfile(REPO / "profile.sh", root / "profile.sh")
                (root / "AGENTS.md").write_text(rules)
            bundle(candidate)
            # Model an old main checkout with no knowledge of the feature or extension.
            for profile in ("codex", "bedrock"):
                path = main / f"profiles/{profile}/settings.json"
                settings = read_json(path)
                settings.pop("cooperativePlanner")
                settings.pop("extensions")
                write_json(path, settings)
                versions = {**PINS, **({"pi-provider-litellm": "4.2.0"} if profile == "bedrock" else {})}
                old_dir, new_dir = base / f"{profile}-main-config", base / f"{profile}-pr-config"
                with patch.dict(os.environ, {}, clear=True):
                    old_record = ci.assemble_config(main, profile, old_dir, fallback_root=candidate)
                    ci.assemble_config(candidate, profile, new_dir)
                self.assertEqual(old_record["harness_fallback_files"], [])
                old = prepare(candidate, base / f"{profile}-main-run", config_dir=old_dir, versions=versions)
                new = prepare(candidate, base / f"{profile}-pr-run", config_dir=new_dir, versions=versions)
                for label, config, source in (("main", old, main), ("pr", new, candidate)):
                    frozen = base / f"{profile}-{label}-run/build/profile"
                    self.assertEqual((frozen / "AGENTS.md").read_bytes(), (source / "AGENTS.md").read_bytes())
                    self.assertEqual(config["config"]["source"], "injected")
                    if label == "main":
                        self.assertNotIn("cooperative_planner", config["config"])
                        self.assertNotIn("extensions", read_json(frozen / "settings.json"))
                        self.assertFalse(any((frozen / name).exists() for name in COOPERATIVE_FILES))
                    else:
                        self.assertTrue(config["config"]["cooperative_planner"]["enabled"])
                        for name in COOPERATIVE_FILES:
                            self.assertEqual((frozen / name).read_bytes(), (candidate / name).read_bytes())
                with (candidate / "agents/planner.md").open("a") as stream:
                    stream.write("\nCandidate-only planner change.\n")
                (candidate / "AGENTS.md").write_text("Changed candidate-only rules")
                with patch.dict(os.environ, {}, clear=True):
                    unchanged = ci.assemble_config(main, profile, base / f"{profile}-main-again", fallback_root=candidate)
                self.assertEqual(unchanged["sha256"], old_record["sha256"])
                again = prepare(candidate, base / f"{profile}-main-rerun", config_dir=old_dir, versions=versions)
                self.assertEqual(again["config_id"], old["config_id"])
                # Even an enabled baseline cannot borrow a candidate bundle.
                settings = read_json(old_dir / "settings.json")
                settings["cooperativePlanner"] = True
                write_json(old_dir / "settings.json", settings)
                with self.assertRaisesRegex(ValueError, "bundle is incomplete"):
                    prepare(candidate, base / f"{profile}-missing-bundle", config_dir=old_dir, versions=versions)

    def test_assembly_copies_arm_owned_exact_files_without_optional_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); root = repository(base); bundle(root)
            shutil.copyfile(REPO / "profile.sh", root / "profile.sh")
            candidate = base / "candidate"
            with patch.dict(os.environ, {}, clear=True):
                ci.assemble_config(root, "codex", candidate)
            self.assertTrue(all((candidate / name).is_file() for name in COOPERATIVE_FILES))
            for name in COOPERATIVE_FILES: (root / name).unlink()
            with patch.dict(os.environ, {}, clear=True):
                record = ci.assemble_config(root, "codex", base / "baseline", fallback_root=candidate)
            self.assertFalse(any((base / "baseline" / name).exists() for name in COOPERATIVE_FILES))
            self.assertFalse(set(COOPERATIVE_FILES) & set(record["harness_fallback_files"]))


class RuntimeInstructionsTests(unittest.TestCase):
    def test_common_contract_preserves_safety_without_collaboration_policy(self):
        text = " ".join((REPO / "evals/swebench/instructions.md").read_text().split())
        for clause in ("/input/issue.md", "/testbed", "no human approval loop",
                       "Do not bypass configured tool approval gates", "existing task container",
                       "Do not escape it", "start other agent CLIs", "access secrets",
                       "provision host tools or extensions", "explicitly authorized model/service endpoints",
                       "do not change network policy", "Network reachability is not permission",
                       "hidden evaluator data", "upstream fixes or later history", "benchmark answers",
                       "reference patches or hidden tests", "Do not weaken grading",
                       "alter tests merely to make them pass", "prepared dependency environment",
                       "Do not push, publish, deploy", "change Git metadata", "worktrees",
                       "all final source changes, including new files", "/output or your isolated home",
                       "terminal state", "report failed/skipped checks honestly", "controller exports a diff"):
            self.assertIn(clause, text)
        for forbidden in ("wasmtime", "safe-v1", "worker", "planner", "cooperative", "jev",
                          "subagent", "needs_approval", "approvedplan", "mcp is disabled"):
            self.assertNotIn(forbidden, text.lower())
        self.assertFalse((REPO / "evals/swebench/presets/wasmtime-safe-v1/AGENTS.md").exists())

    def test_common_runtime_limits_apply_without_optional_host_capabilities(self):
        text = " ".join((REPO / "evals/swebench/instructions.md").read_text().split())
        for clause in ("runtime limits take precedence over conflicting host working rules",
                       "Missing optional host routers or extensions do not block use of the configured provider/model choices",
                       "do not provision replacements", "without claiming a routing recommendation",
                       "Do not bypass configured tool approval gates",
                       "change Git metadata (including branches, commits and worktrees)",
                       "If delegation is available and used", "shared /testbed checkout",
                       'shared-tree isolation (`isolation: "none"` where supported)',
                       "never automatic or worktree isolation", "explicit, disjoint file ownership",
                       "do not edit or run integration checks while children are active",
                       "If safe shared-checkout delegation is unavailable, work sequentially within these limits",
                       "runtime safety and isolation constraints in every child brief",
                       "do not assume they are inherited"):
            self.assertIn(clause, text)

    def test_generic_working_rules_respect_missing_optional_router_and_runtime_isolation(self):
        text = " ".join((REPO / "AGENTS.md").read_text().split())
        for clause in ("optional router is absent or prohibited by runtime policy",
                       "retain the configured role/provider/model choices",
                       "Do not install host extensions or widen network access",
                       "never waives required scoring or approval gates",
                       "If routing fails, report the failure",
                       "runtime network, Git metadata and isolation restrictions",
                       'shared-tree isolation (`isolation: "none"`)', "never `auto` or worktree creation",
                       "Require exclusive file ownership", "If ownership cannot be enforced, execute sequentially",
                       "API errors never grant approval", "Failure, cancellation, and branch navigation stay fail-closed"):
            self.assertIn(clause, text)


def receipt(phase="finished", outcome="accepted", identity=None, *, attempt=1, scores=None):
    if outcome in ("accepted", "rejected"):
        scores = scores or {key: (0.9 if outcome == "accepted" else 0.7) for key in JEV_CRITERIA}
        passed = {key: value >= 0.8 for key, value in scores.items()}
    else:
        scores = passed = None
    return {"schema_version": 1, "receipt_id": identity or str(uuid.uuid4()),
            **{key: "a" * 64 for key in ("session_digest", "tool_call_digest", "plan_digest", "policy_digest")},
            "attempt": attempt, "model": "jev-latest", "phase": phase, "outcome": outcome, "duration_ms": 10,
            "usage": {"input_tokens": 123, "output_tokens": 4} if outcome in ("accepted", "rejected") else None,
            "cost_usd": None, "scores": scores, "passed": passed}


class ExternalAccountingTests(unittest.TestCase):
    def test_deduplicate_phases_across_sessions_and_never_add_to_pi_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            end = receipt(); start = receipt("started", "unfinished", end["receipt_id"])
            (base / "a.jsonl").write_text("\n".join(map(json.dumps, [start, end, end])) + "\n")
            (base / "b.jsonl").write_text(json.dumps(end) + "\n")
            report = external_jev(base)
            self.assertEqual(report["http_attempts"], 1)
            self.assertEqual(report["usage"], {"input_tokens": 123, "output_tokens": 4})
            self.assertIsNone(report["cost_usd"])
            summary = external_jev_summary([{"external_jev": report}])
            self.assertEqual(summary["http_attempts_observed"], 1)
            self.assertEqual(summary["scored_attempts_observed"], 1)
            self.assertIsNone(summary["cost_usd"])
            public = base / "public"; public.mkdir()
            write_json(public / "telemetry.json", {"external_jev": report})
            secret = "synthetic-typesafe-secret-123456789"
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": secret,
                                         "EVAL_ENV": f"TYPESAFE_API_KEY={secret}"}, clear=True):
                self.assertEqual(ci.scan_artifacts(public, extra_names=("TYPESAFE_API_KEY",)), [])

    def test_best_criterion_scores_are_validated_and_best_attempt_is_maximin(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = {"task_coverage": 0.7, "slice_independence": 0.9,
                     "ownership_conflicts": 0.8, "integration_feasibility": 0.88}
            second = {"task_coverage": 0.76, "slice_independence": 0.85,
                      "ownership_conflicts": 0.84, "integration_feasibility": 0.9}
            rows = [receipt(outcome="rejected", attempt=1, scores=first),
                    receipt(outcome="rejected", attempt=2, scores=second)]
            (base / "scores.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n")
            report = external_jev(base)
            self.assertEqual(report["scored_attempts"], 2)
            self.assertEqual(report["score_attempts"], [
                {"attempt": 1, "outcome": "rejected", "minimum_score": 0.7, "criteria": first},
                {"attempt": 2, "outcome": "rejected", "minimum_score": 0.76, "criteria": second},
            ])
            self.assertEqual(report["best_score"], {"attempt": 2, "outcome": "rejected",
                                                     "minimum_score": 0.76, "criteria": second})
            rows[1]["passed"]["task_coverage"] = True
            (base / "scores.jsonl").write_text(json.dumps(rows[1]) + "\n")
            report = external_jev(base)
            self.assertEqual(report["invalid_receipts"], 1)
            self.assertIsNone(report["best_score"])

    def test_unfinished_cancel_errors_malformed_and_conflicting_receipts_remain_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self.assertEqual(external_jev(base)["status"], "unavailable")
            rows = [receipt("started", "unfinished"), receipt(outcome="cancelled"), receipt(outcome="timeout")]
            (base / "a.jsonl").write_text("\n".join(map(json.dumps, rows)) + '\n{"SECRET malformed\n')
            report = external_jev(base)
            self.assertEqual(report["http_attempts"], 3)
            self.assertEqual(report["status"], "partial")
            self.assertIsNone(report["usage"])
            self.assertEqual(report["invalid_receipts"], 1)
            self.assertNotIn("SECRET", json.dumps(report))
            end = receipt(); conflicting = {**end, "usage": {"input_tokens": 999, "output_tokens": 4}}
            (base / "a.jsonl").write_text("\n".join(map(json.dumps, [end, conflicting])) + "\n")
            report = external_jev(base)
            self.assertEqual(report["http_attempts"], 1)
            self.assertIsNone(report["usage"])
            self.assertEqual(report["observed_usage"]["input_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
