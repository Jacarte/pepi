"""Private evaluator boundaries and target-neutral, checkout-owned CI configuration; offline."""
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.swebench import ci, narrate, preset
from evals.swebench.common import read_json, read_manifest, write_json
from evals.swebench.prepare import prepare_run
from evals.swebench.tests import test_ci_helpers as helpers, test_ci_workflow as workflow
from evals.swebench.tests.test_runner import TASK, VERSIONS, prepare_fixture

REPO = helpers.REPO
REVISION = "a" * 40


class WasmtimePresetTests(helpers.Temp):
    def test_frozen_order_dataset_defaults_and_existing_presets(self):
        ids = preset.preset_ids("wasmtime-safe-v1").read_text().splitlines()
        self.assertEqual(ids, [f"bytecodealliance__wasmtime-{n}-safe-v1-l8rcyy"
                               for n in (15027, 15028, 15033, 15043, 14979, 14961, 14902)])
        frozen = preset.preset_definition("wasmtime-safe-v1")
        self.assertEqual(frozen["ids_sha256"], preset.tasklist_hash(ids))
        self.assertEqual((frozen["dataset"], frozen["timeout_seconds"], frozen["concurrency"]),
                         ("pepi/upstream-safe-v1", 1800, 1))
        self.assertTrue(preset.requires_task_inputs(ids[:1]))
        self.assertEqual(preset.preset_ids("verified20-v1"), preset.DEFAULT_IDS)
        self.assertEqual(preset.preset_definition("verified20-v1"), preset.definition())
        self.assertEqual(preset.preset_definition("multilingual10-v1"), preset.multilingual_definition())
        for symbol in ("WASMTIME_IDS", "WASMTIME_DEFINITION"):
            path = getattr(preset, symbol)
            altered = self.write(path.name, path.read_text() + " ")
            with patch.object(preset, symbol, altered), self.assertRaisesRegex(ValueError, "hash mismatch"):
                preset.preset_definition("wasmtime-safe-v1")

    def test_manifest_records_source_preset_dataset_and_no_oracle(self):
        prepare_fixture(self.root)
        ids = preset.WASMTIME_IDS.read_text().splitlines()
        tasks = [dict(TASK, instance_id=i, repo="bytecodealliance/wasmtime",
                      datasets=[preset.WASMTIME_DATASET]) for i in ids]
        run = self.root / "wasmtime"
        prepare_run(self.root / "pepi", run, tasks, ids, VERSIONS, "node:22-fixed",
                    preset.WASMTIME_DATASET, REVISION, drop_mcp=True)
        manifest = read_manifest(run)
        self.assertEqual(manifest["task_repo_revision"], REVISION)
        self.assertEqual(manifest["dataset"], preset.WASMTIME_DATASET)
        self.assertEqual(manifest["selection"]["preset"]["id"], "custom-wasmtime-safe-v1")
        self.assertEqual(manifest["selection"]["task_repo_revision"], REVISION)
        self.assertNotIn("SECRET", json.dumps(manifest))
        # Subsets still carry defaults/provenance and cannot select other tasks.
        selection = preset.selection_record(ids[:1], tasks[:1], preset.WASMTIME_DATASET, REVISION)
        self.assertEqual(selection["preset"]["timeout_seconds"], 1800)
        for chosen, dataset, revision in ((ids, "wrong", REVISION), (ids, preset.WASMTIME_DATASET, "main"),
                                          (ids + ["other__task-1"], preset.WASMTIME_DATASET, REVISION)):
            with self.assertRaises(ValueError):
                preset.selection_record(chosen, tasks, dataset, revision)

    def test_shell_accepts_wasmtime_only_with_an_explicit_task_repo(self):
        script = (REPO / "evals/pepi-eval.sh").read_text()
        functions = script[script.index("preset_name() {"):script.index("default_ids_file() {")]
        env = {"PATH": os.environ["PATH"], "PEPI_EVAL_PRESET": "wasmtime-safe-v1"}
        command = 'die() { echo "$1" >&2; exit "$2"; };\n' + functions + "\ncheck_preset"
        result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("pinned evaluator checkout", result.stderr)
        env["PEPI_EVAL_TASK_REPO"] = "/private/tasks"
        self.assertEqual(subprocess.run(["bash", "-c", command], env=env, capture_output=True).returncode, 0)


class AgentsOverrideTests(helpers.Temp):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.write("profile.sh", "#!/bin/bash\nexit 0\n", self.repo)
        self.write("profiles/direct/settings.json", "{}", self.repo)
        self.write("settings.json", "{}", self.repo)
        self.write("AGENTS.md", "global delegation rules", self.repo)
        self.write("agents/deep-worker.md", "arm-owned worker", self.repo)

    def assemble(self, name, *args):
        return helpers.invoke("assemble-config", "--repo", self.repo, "--profile", "direct",
                              "--out", self.root / name, *args)

    def test_override_is_hashed_without_changing_global_agents(self):
        original = self.assemble("original")
        override = self.write("direct.md", "implement directly")
        code, output, err = self.assemble("direct", "--agents-override", override)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.root / "direct/AGENTS.md").read_text(), "implement directly")
        self.assertEqual((self.repo / "AGENTS.md").read_text(), "global delegation rules")
        self.assertNotEqual(json.loads(original[1])["sha256"], json.loads(output)["sha256"])
        override.write_text("implement and test directly")
        changed = self.assemble("changed", "--agents-override", override)
        self.assertNotEqual(json.loads(changed[1])["sha256"], json.loads(output)["sha256"])
        (self.repo / "AGENTS.md").unlink()
        absent = self.assemble("absent", "--agents-override", override, "--fallback-root", REPO)
        self.assertIn("AGENTS.md", json.loads(absent[1])["files"])
        self.assertNotIn("AGENTS.md", json.loads(absent[1])["harness_fallback_files"])

    def test_override_rejects_links_missing_directories_and_special_files_before_profile(self):
        target = self.write("target.md", "direct")
        link = self.root / "link.md"
        link.symlink_to(target)
        parent_link = self.root / "alias"
        parent_link.symlink_to(self.repo, target_is_directory=True)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        for i, path in enumerate((link, parent_link / "AGENTS.md", self.root / "missing", self.repo, fifo)):
            with self.subTest(path=path):
                code, _, _ = self.assemble(f"bad-{i}", "--agents-override", path)
                self.assertEqual(code, 2)
                self.assertFalse((self.root / f"bad-{i}").exists())


class WasmtimeWorkflowTests(helpers.Temp):
    def setUp(self):
        super().setUp()
        self.jobs = workflow.require_workflow()["jobs"]
        self.env = {"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1",
                    "PEPI_CI_PROFILE": "bedrock", "PEPI_CI_PRESET": "wasmtime-safe-v1",
                    "PEPI_CI_PI_VERSION": "1.1.0", "PEPI_CI_SUBAGENTS_VERSION": "0.78.0",
                    "PEPI_CI_LITELLM_VERSION": "4.2.0", "PEPI_CI_TIMEOUT_SECONDS": "",
                    "PEPI_CI_CPUS": "4", "PEPI_CI_MEMORY": "8g",
                    "WASMTIME_TASKS_REPOSITORY": "operator/private-evaluator", "WASMTIME_TASKS_REVISION": REVISION,
                    "PEPI_CI_SWEBENCH_REVISION": "b" * 40, "GITHUB_REPOSITORY": "operator/public-agent"}

    def run_step(self, step, **env):
        output = self.root / "output"
        output.write_text("")
        result = subprocess.run(["bash", "-c", step["run"]], cwd=REPO,
                                env={**self.env, "GITHUB_OUTPUT": str(output), **env},
                                capture_output=True, text=True)
        return result, dict(line.split("=", 1) for line in output.read_text().splitlines())

    def test_lookup_validates_pin_repository_ids_and_self_hosted_labels_without_secrets(self):
        steps = self.jobs["lookup"]["steps"]
        config = steps[0]
        resolve = next(s for s in steps if s.get("id") == "preset")
        self.assertNotIn("secrets.", json.dumps(self.jobs["lookup"]))
        self.assertNotIn("environment", self.jobs["lookup"])
        self.assertEqual(self.run_step(config)[0].returncode, 0)
        result, outputs = self.run_step(resolve)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs["task_source"], "wasmtime")
        self.assertEqual(outputs["task_repository"], self.env["WASMTIME_TASKS_REPOSITORY"])
        self.assertEqual(outputs["task_revision"], REVISION)
        self.assertEqual(outputs["dataset"], preset.WASMTIME_DATASET)
        for change in ({"WASMTIME_TASKS_REPOSITORY": ""}, {"WASMTIME_TASKS_REPOSITORY": "../bad"},
                       {"WASMTIME_TASKS_REVISION": "main"}, {"WASMTIME_TASKS_REVISION": ""},
                       {"EVAL_RUNS_ON": '["ubuntu-latest"]'}, {"EVAL_RUNS_ON": '["self-hosted"]'}):
            with self.subTest(change=change):
                self.assertNotEqual(self.run_step(config, **change)[0].returncode, 0)
        ids = preset.WASMTIME_IDS.read_text().splitlines()
        self.assertEqual(self.run_step(config, PEPI_CI_IDS=ids[0])[0].returncode, 0)
        for change in ({"WASMTIME_TASKS_REPOSITORY": "OPERATOR/PUBLIC-AGENT"},
                       {"WASMTIME_TASKS_REVISION": "tag"}, {"PEPI_CI_IDS": "other__task-1"},
                       {"PEPI_CI_IDS": f"{ids[0]},{ids[0]}"}):
            self.assertNotEqual(self.run_step(resolve, **change)[0].returncode, 0)

    def test_checkout_guard_pins_cleanup_and_token_scoping(self):
        for job_name in ("baseline", "candidate"):
            job = self.jobs[job_name]
            self.assertIn("task_source == 'wasmtime' && '[\"self-hosted\",\"pepi-evals\"]'", job["runs-on"])
            self.assertEqual(job["environment"], "pepi-evals")
            steps = job["steps"]
            checkout = next(s for s in steps if s.get("with", {}).get("path") == "ext/wasmtime-tasks")
            self.assertEqual(checkout["if"], "needs.lookup.outputs.task_source == 'wasmtime'")
            self.assertEqual(checkout["with"]["repository"], "${{ needs.lookup.outputs.task_repository }}")
            self.assertEqual(checkout["with"]["ref"], "${{ needs.lookup.outputs.task_revision }}")
            self.assertEqual(checkout["with"]["token"], "${{ secrets.WASMTIME_TASKS_TOKEN || github.token }}")
            self.assertIs(checkout["with"]["persist-credentials"], False)
            guard = steps[steps.index(checkout) - 1]
            self.assertEqual(guard["if"], checkout["if"])
            self.assertNotIn("secrets.", json.dumps(guard))
            self.assertNotIn("WASMTIME_TASKS_TOKEN", json.dumps(guard))
            # Public repos need no task token; the runner/network gate is independent of credentials.
            docker = self.write("bin/docker", '#!/bin/sh\nexit "${NETWORK_STATUS:-0}"\n')
            docker.chmod(0o755)
            env = {"PATH": f"{docker.parent}:{self.env['PATH']}", "RUNNER_ENVIRONMENT": "self-hosted"}
            result, _ = self.run_step(guard, **env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for change, error in (({"RUNNER_ENVIRONMENT": "github-hosted"}, "self-hosted pepi-evals runner"),
                                  ({"NETWORK_STATUS": "1"}, "Preprovision the operator-managed pepi-eval network")):
                result, _ = self.run_step(guard, **{**env, **change})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stdout)
            build = next(s for s in steps if workflow.runs(s, "ci build-wasmtime-images"))
            self.assertEqual(build["if"], checkout["if"])
            self.assertLess(steps.index(checkout), steps.index(build))
            self.assertLess(steps.index(build), next(i for i, s in enumerate(steps) if workflow.runs(s, "ci secrets-setup")))
            self.assertNotIn("secrets.", json.dumps(build))
            run = next(s for s in steps if s.get("id") == "run")
            self.assertNotIn("TOKEN", json.dumps(run))
            self.assertIn('export PEPI_CI_TASK_REPO="$TASK_REPO"', run["run"])
            self.assertEqual(run["env"]["PEPI_CI_TASK_REVISION"], "${{ needs.lookup.outputs.task_revision }}")
            names = next(s for s in steps if s.get("id") == "names")
            self.assertIn('wasmtime) task_repo="$GITHUB_WORKSPACE/ext/wasmtime-tasks"', names["run"])
            clean = next(s for s in steps if s.get("name") == "Remove evaluator checkout")
            self.assertIn("always()", clean["if"])
        text = workflow.WORKFLOW.read_text()
        # The value is only provided to checkout, never env, scripts, outputs or containers.
        self.assertEqual(text.count("${{ secrets.WASMTIME_TASKS_TOKEN || github.token }}"), 2)

    def test_public_checkout_falls_back_to_job_token_and_private_checkout_prefers_task_token(self):
        # Check the Actions expression offline; no GitHub API or real token is used.
        for job_name in ("baseline", "candidate"):
            checkout = next(s for s in self.jobs[job_name]["steps"]
                            if s.get("with", {}).get("path") == "ext/wasmtime-tasks")
            expression = checkout["with"]["token"]
            self.assertEqual(expression, "${{ secrets.WASMTIME_TASKS_TOKEN || github.token }}")
            choices = [choice.strip() for choice in expression[3:-3].split("||")]
            for visibility, task_token, expected in (("public", "", "synthetic-job-token"),
                                                      ("private", "synthetic-readonly-task-token",
                                                       "synthetic-readonly-task-token")):
                with self.subTest(job=job_name, visibility=visibility):
                    values = {"secrets.WASMTIME_TASKS_TOKEN": task_token, "github.token": "synthetic-job-token"}
                    self.assertEqual(next(values[choice] for choice in choices if values[choice]), expected)
            self.assertIs(checkout["with"]["persist-credentials"], False)
            self.assertNotIn("env", checkout)
            self.assertNotIn("run", checkout)
            self.assertNotIn("continue-on-error", checkout)
            steps = self.jobs[job_name]["steps"]
            self.assertLess(steps.index(checkout), next(i for i, s in enumerate(steps) if s.get("id") == "run"))

    def test_all_assembly_steps_use_arm_owned_config_without_preset_overrides(self):
        for name in ("lookup", "baseline", "candidate"):
            step = next(s for s in self.jobs[name]["steps"] if workflow.runs(s, "ci assemble-config"))
            self.assertNotIn("PEPI_CI_PRESET", step["run"])
            self.assertNotIn("agents_args", step["run"])
            self.assertNotIn("agents-override", step["run"])
            checkout = "harness" if name == "candidate" else "config-src"
            self.assertIn(f'assemble-config --repo "$GITHUB_WORKSPACE/{checkout}"', step["run"])
            if name != "candidate":
                self.assertIn('--fallback-root "$GITHUB_WORKSPACE/harness"', step["run"])
        for job, ref in (("lookup", "${{ github.event_name == 'push' && github.sha || 'main' }}"),
                         ("baseline", "${{ needs.lookup.outputs.main_sha }}")):
            checkout = next(s for s in self.jobs[job]["steps"] if s.get("with", {}).get("path") == "config-src")
            self.assertEqual(checkout["with"]["ref"], ref)
        report_config = next(s for s in self.jobs["report"]["steps"] if workflow.runs(s, "ci assemble-config"))
        self.assertNotIn("agents-override", report_config["run"])
        self.write("config/settings.json", "{}")
        pins = dict(task_repo_revision=REVISION, dataset=preset.WASMTIME_DATASET, concurrency="1")
        def key(pins=pins, name="wasmtime-safe-v1", repository="operator/private-evaluator"):
            return ci.cache_key(REPO, self.root / "config", "bedrock", name, pins,
                                {"task_repository": repository}, "2026-W01")
        base = key()
        self.assertEqual(base["components"]["pins"]["task_repo_revision"], REVISION)
        self.assertEqual(base["components"]["pins"]["preset"], "wasmtime-safe-v1")
        for other in (key({**pins, "task_repo_revision": "b" * 40}), key({**pins, "dataset": "other"}),
                      key(name="verified20-v1"), key(repository="operator/other-evaluator")):
            self.assertNotEqual(base["key"], other["key"])

    def config_checkouts(self):
        workspace = self.root / "workspace"
        for checkout in ("config-src", "harness"):
            repo = workspace / checkout
            self.write("profile.sh", (REPO / "profile.sh").read_text(), repo)
            self.write("profiles/bedrock/settings.json", "{}", repo)
            self.write("settings.json", "{}", repo)
            self.write("AGENTS.md", f"{checkout} ordinary instructions", repo)
            self.write("agents/deep-worker.md", f"{checkout} worker instructions", repo)
        return workspace, "AGENTS.md"

    def assemble_arm(self, job, workspace, name, **env):
        step = next(s for s in self.jobs[job]["steps"] if workflow.runs(s, "ci assemble-config"))
        temp = self.root / name
        temp.mkdir()
        result, outputs = self.run_step(step, GITHUB_WORKSPACE=str(workspace), RUNNER_TEMP=str(temp), **env)
        config = temp / ("pepi-config-baseline" if job == "lookup" else "pepi-config")
        return result, outputs, config

    def test_executed_assemblies_keep_each_arms_agents_and_hash_identity(self):
        workspace, relative = self.config_checkouts()
        result, outputs, base = self.assemble_arm("lookup", workspace, "lookup")
        self.assertEqual(result.returncode, 0, result.stderr)
        base_hash = outputs["sha256"]
        self.assertEqual(base_hash, ci.config_hash(base))
        result, _, fresh = self.assemble_arm("baseline", workspace, "baseline", EXPECTED_SHA256=base_hash)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(ci.config_hash(fresh), base_hash)
        result, _, candidate = self.assemble_arm("candidate", workspace, "candidate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((base / "AGENTS.md").read_bytes(), (workspace / "config-src" / relative).read_bytes())
        self.assertEqual((candidate / "AGENTS.md").read_bytes(), (workspace / "harness" / relative).read_bytes())
        candidate_hash = ci.config_hash(candidate)
        self.assertNotEqual(candidate_hash, base_hash)
        pins = dict(task_repo_revision=REVISION, dataset=preset.WASMTIME_DATASET, concurrency="1")
        def key(config):
            return ci.cache_key(REPO, config, "bedrock", "wasmtime-safe-v1", pins, {}, "2026-W01")["key"]
        self.assertEqual(key(base), key(fresh))
        self.assertNotEqual(key(base), key(candidate))
        # Candidate-only prompt edits must never affect baseline config identity.
        (workspace / "harness" / relative).write_text("changed candidate instructions")
        result, outputs, unchanged = self.assemble_arm("lookup", workspace, "lookup-again")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs["sha256"], base_hash)
        self.assertEqual(key(unchanged), key(base))
        result, _, changed = self.assemble_arm("candidate", workspace, "candidate-again")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(ci.config_hash(changed), candidate_hash)
        # Main edits invalidate the baseline and the lookup/execution consistency check.
        (workspace / "config-src" / relative).write_text("changed main instructions")
        result, outputs, changed = self.assemble_arm("lookup", workspace, "lookup-main-change")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(outputs["sha256"], base_hash)
        self.assertNotEqual(key(changed), key(base))
        result, _, _ = self.assemble_arm("baseline", workspace, "stale-baseline", EXPECTED_SHA256=base_hash)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("baseline config differs", result.stdout)

    def test_missing_main_agents_never_falls_back_to_candidate(self):
        workspace, relative = self.config_checkouts()
        (workspace / "config-src" / relative).unlink()
        for job in ("lookup", "baseline"):
            result, _, config = self.assemble_arm(job, workspace, job, EXPECTED_SHA256="unused")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("config files absent here would come from the harness checkout: AGENTS.md", result.stderr)
            self.assertFalse((config / "AGENTS.md").exists())

    def test_every_preset_uses_the_same_arm_owned_assembly(self):
        workspace, relative = self.config_checkouts()
        hashes = {}
        for preset_name in ("verified20-v1", "multilingual10-v1", "wasmtime-safe-v1"):
            base_hash = ""
            for job in ("lookup", "baseline", "candidate"):
                result, outputs, config = self.assemble_arm(job, workspace, f"{preset_name}-{job}",
                    PEPI_CI_PRESET=preset_name, EXPECTED_SHA256=base_hash)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                owner = "harness" if job == "candidate" else "config-src"
                self.assertEqual((config / relative).read_bytes(), (workspace / owner / relative).read_bytes())
                if job == "lookup":
                    base_hash = outputs["sha256"]
                hashes.setdefault(job, ci.config_hash(config))
                self.assertEqual(ci.config_hash(config), hashes[job])

    def test_profile_agents_overlay_is_owned_by_each_checkout(self):
        workspace, relative = self.config_checkouts()
        for checkout in ("config-src", "harness"):
            self.write("profiles/bedrock/AGENTS.md", f"{checkout} profile instructions", workspace / checkout)
        base_hash = ""
        for job in ("lookup", "baseline", "candidate"):
            result, outputs, config = self.assemble_arm(job, workspace, job, EXPECTED_SHA256=base_hash)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            owner = "harness" if job == "candidate" else "config-src"
            self.assertEqual((config / relative).read_text(), f"{owner} profile instructions")
            if job == "lookup":
                base_hash = outputs["sha256"]

    def test_report_narrates_baseline_then_candidate_before_building_comment(self):
        steps = self.jobs["report"]["steps"]
        step = next(s for s in steps if workflow.runs(s, "evals/ci/narrate.sh"))
        self.assertIn('narrate.sh "$GITHUB_WORKSPACE/pepi-baseline-cache" "$GITHUB_WORKSPACE/pepi-candidate"',
                      step["run"])
        self.assertNotIn("if", step)
        comment = next(s for s in steps if workflow.runs(s, "ci comment"))
        self.assertLess(steps.index(step), steps.index(comment))
        self.assertIn('--narrative "$RUNNER_TEMP/pepi-report/narrative.md"', comment["run"])


class WasmtimeRunBoundaryTests(helpers.Temp):
    def setUp(self):
        super().setUp()
        self.tree = self.root / "tree"
        self.write("evals/ci/run-evals.sh", (REPO / "evals/ci/run-evals.sh").read_text(), self.tree)
        self.write("evals/pepi-eval.sh", '#!/bin/bash\nenv | sort > "$RECORD"\n', self.tree).chmod(0o755)
        self.write("config/settings.json", "{}")
        self.write("secrets/auth.json", "{}")
        (self.root / "launch").mkdir()
        self.write("bin/git", '#!/bin/bash\n[[ "$*" != *rev-parse* ]] || echo "$FAKE_REVISION"\n').chmod(0o755)
        self.write("bin/docker", '#!/bin/bash\necho "$*" >> "$DOCKER_CALLS"\nexit "${NETWORK_STATUS:-0}"\n').chmod(0o755)
        self.env = {"PATH": f"{self.root / 'bin'}:{os.environ['PATH']}", "PEPI_CI_PRESET": "wasmtime-safe-v1",
                    "PEPI_CI_SECRETS_DIR": str(self.root / "secrets"), "PEPI_CI_LAUNCH_DIR": str(self.root / "launch"),
                    "PEPI_CI_TASK_REPO": "/private/tasks", "PEPI_CI_TASK_REVISION": REVISION,
                    "FAKE_REVISION": REVISION, "RUNNER_ENVIRONMENT": "self-hosted",
                    "RECORD": str(self.root / "record"), "DOCKER_CALLS": str(self.root / "docker.log"),
                    "WASMTIME_TASKS_TOKEN": "must-not-reach-harness"}

    def run_script(self, **env):
        return subprocess.run(["bash", str(self.tree / "evals/ci/run-evals.sh"), "baseline",
                               str(self.root / "config"), "main", "wasmtime-run"],
                              env={**self.env, **env}, capture_output=True, text=True)

    def test_preconfigured_network_mcp_timeout_and_no_checkout_token_in_agent_launcher(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        env = dict(line.split("=", 1) for line in (self.root / "record").read_text().splitlines())
        self.assertEqual(env["PEPI_EVAL_DROP_MCP"], "1")
        self.assertEqual(env["PEPI_EVAL_TIMEOUT_SECONDS"], "1800")
        self.assertEqual(env["PEPI_EVAL_TASK_REPO"], "/private/tasks")
        self.assertNotIn("WASMTIME_TASKS_TOKEN", env)
        self.assertEqual((self.root / "docker.log").read_text(), "network inspect pepi-eval\n")

    def test_missing_network_hosted_runner_or_wrong_checkout_fail_closed(self):
        for env, error in (({"NETWORK_STATUS": "1"}, "pre-existing operator-managed"),
                           ({"RUNNER_ENVIRONMENT": "github-hosted"}, "self-hosted pepi-evals"),
                           ({"PEPI_CI_NETWORK": "bridge"}, "preconfigured pepi-eval"),
                           ({"PEPI_CI_TASK_REVISION": "main"}, "pinned PEPI_CI_TASK_REVISION"),
                           ({"FAKE_REVISION": "b" * 40}, "does not match WASMTIME_TASKS_REVISION")):
            with self.subTest(env=env):
                result = self.run_script(**env)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(error, result.stderr)
                self.assertFalse((self.root / "record").exists())
        self.assertNotIn("create", (self.root / "docker.log").read_text())

    def test_checkout_token_cannot_be_forwarded_via_eval_env(self):
        with patch.dict(os.environ, {"PI_AUTH": helpers.PI_AUTH,
                                    "EVAL_ENV": "WASMTIME_TASKS_TOKEN=not-for-agents"}, clear=True):
            with self.assertRaisesRegex(ci.UsageError, "belongs only in the evaluator checkout"):
                ci.secrets_setup(self.root / "new-secrets", self.root / "launch", self.root / "mask")

    def test_public_outputs_exclude_hidden_evaluator_logs_and_narrative(self):
        run = helpers.fake_run(self.root, "run")
        summary = read_json(run / "summary.json")
        summary["dataset"] = preset.WASMTIME_DATASET
        write_json(run / "summary.json", summary)
        for kind in ci.SLIM_KINDS:
            out = self.root / kind
            ci.slim_run(run, out, kind)
            files = [p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()]
            self.assertIn("summary.json", files)
            self.assertIn("attempts/owner__repo-1/attempt.json", files)
            for path in files:
                self.assertFalse(path.startswith(("grading", "build")), path)
                self.assertNotIn("narrative", path)
                self.assertFalse(path.endswith(("gold.patch", "test.patch", "eval.sh", "test_output.txt")))
            if kind == "artifact":
                self.assertIn("attempts/owner__repo-1/patch.diff", files)
            if kind != "cache":
                self.assertNotIn("manifest.json", files)
        body = ci.comment_body(run, run, None, REVISION, "1", "2", "https://github.com/o/r/actions/runs/2",
                               "bedrock", "wasmtime-safe-v1")
        self.assertIn("deny GitHub", body)
        self.assertNotIn("unrestricted", body)


class WasmtimeNarrationTests(helpers.Temp):
    def test_two_arm_facts_keep_solved_time_cost_and_child_usage_without_oracle_data(self):
        base = helpers.fake_run(self.root, "main", ("resolved", "unresolved", "unresolved"))
        candidate = helpers.fake_run(self.root, "pr-42", ("resolved", "resolved", "unresolved"))
        summaries = []
        for run, children, cost, seconds in ((base, [], 3.0, 60),
                (candidate, [{"agent": "worker", "runId": "a"}, {"agent": "worker", "runId": "b"}], 6.0, 40)):
            data = read_json(run / "summary.json")
            data.update(dataset=preset.WASMTIME_DATASET, config_id=run.name,
                        estimated_total_cost_usd=cost, observed_estimated_cost_usd=cost)
            data["latency_all_seconds"].update(median=seconds, p95=seconds + 10)
            data["protocol"]["mcp"] = {"enabled": False, "commands": []}
            write_json(run / "summary.json", data)
            summaries.append(data)
            for row in data["instances"]:
                write_json(run / "attempts" / row["instance_id"] / "telemetry.json", {
                    "accounting_status": "available", "children": children,
                    "parent_tool_timing": {"calls": 3 if children else 2, "errors": 0,
                                           "by_tool": {"bash": 2, **({"subagent": 1} if children else {})}}})
        facts = narrate.build_facts(summaries, [base, candidate])
        self.assertEqual(facts["baseline_run_id"], "main")
        self.assertEqual([r["resolved"] for r in facts["runs"]], [1, 2])
        self.assertEqual([r["latency_all_median_seconds"] for r in facts["runs"]], [60, 40])
        self.assertEqual([r["estimated_total_cost_usd"] for r in facts["runs"]], [3, 6])
        self.assertEqual(facts["comparisons"][0]["wins"], ["owner__repo-2"])
        self.assertEqual(facts["comparisons"][0]["subagent_invocation_differences"], [
            {"agent": "worker", "baseline": 0, "candidate": 6, "difference": 6}])
        self.assertEqual(facts["runs"][0]["subagent_usage"]["total_children"], 0)
        self.assertEqual(facts["runs"][1]["subagent_usage"]["subagent_count_cross_check"],
                         "children_more_than_tool_calls")
        prompt = narrate.build_prompt(narrate.canonical(facts))
        for rule in ("Tasks solved is the primary outcome", "Time and cost are secondary",
                     "never describe missing child usage as free", "Child counts do not prove concurrent execution"):
            self.assertIn(rule, prompt)
        for forbidden in ("SECRET-PROBLEM-STATEMENT", "gold.patch", "test_output.txt", "SESSION", "STDERR"):
            self.assertNotIn(forbidden, prompt)
        # The deterministic PR report retains metrics even if the optional narrator fails.
        summaries[1].update(estimated_total_cost_usd=None, available_accounting_tasks=2)
        write_json(candidate / "summary.json", summaries[1])
        body = ci.comment_body(base, candidate, None, REVISION, "42", "2", "https://github.com/o/r/actions/runs/2",
                               "bedrock", "wasmtime-safe-v1")
        self.assertIn("| resolved (official) | 1/3 | 2/3 |", body)
        self.assertIn("| agent seconds (observed median) | 60 | 40 |", body)
        self.assertIn("| agent seconds (observed p95) | 70 | 50 |", body)
        self.assertIn("| estimated total cost (USD) | 3 | unavailable |", body)
        self.assertIn("| observed cost (USD, may be partial) | 3 | 6 |", body)
        self.assertIn("| tasks with full accounting | 3/3 | 2/3 |", body)
        self.assertIn("Arms use their own configuration", body)
        self.assertIn("tool and child counts are context only", body)
        self.assertIn("Tasks solved is primary", body)
        self.assertIn("Narrative unavailable", body)


class WasmtimeBaseImageTests(helpers.Temp):
    def setUp(self):
        super().setUp()
        self.tasks = self.root / "private-tasks"
        self.write("sweb.yaml", "{}", self.tasks)
        self.definition = preset.wasmtime_definition()
        self.recipe = b"FROM --platform=linux/amd64 debian:12\nWORKDIR /testbed\n"
        for instance_id, metadata in self.definition["tasks"].items():
            metadata["dockerfile_sha256"] = ci.sha256_hex(self.recipe)
            prefix = f"tasks/{instance_id}"
            self.write(f"{prefix}/task.yaml", json.dumps({**metadata, "instance_id": instance_id,
                       "repo": self.definition["repo"], "datasets": [self.definition["dataset"]], "split": "test"}), self.tasks)
            self.write(f"{prefix}/Dockerfile", self.recipe.decode(), self.tasks)
            self.write(f"{prefix}/.dockerignore", "*\n!Dockerfile\n!.dockerignore\n", self.tasks)
            # Synthetic sentinels only: no real oracle data in these tests.
            self.write(f"{prefix}/gold.patch", "DO NOT SEND TO DOCKER", self.tasks)
            self.write(f"{prefix}/test.patch", "DO NOT SEND TO DOCKER", self.tasks)
            self.write(f"{prefix}/eval.sh", "DO NOT SEND TO DOCKER", self.tasks)
        self.commands = []

    def process(self, command, **kwargs):
        self.commands.append(command)
        stdout = ""
        if command[0] == "git":
            stdout = REVISION if command[-1] == "HEAD" else ""
        else:
            self.assertNotIn("WASMTIME_TASKS_TOKEN", kwargs["env"])
            self.assertNotIn("PI_AUTH", kwargs["env"])
            self.assertEqual(set(kwargs["env"]), {"PATH", "HOME", "LC_ALL"})
            self.assertIn("linux/amd64", command)
            self.assertEqual(Path(kwargs["stdout"].name).parent, self.root / "receipt")
            if command[1] == "build":
                context = Path(command[-1])
                self.assertEqual({p.name for p in context.iterdir()}, {"Dockerfile", ".dockerignore"})
                self.assertEqual((context / "Dockerfile").read_bytes(), self.recipe)
                self.assertNotIn(str(self.tasks), str(context))
                Path(command[command.index("--iidfile") + 1]).write_text("sha256:" + "c" * 64)
            else:
                self.assertEqual(command[command.index("--network") + 1], "none")
                self.assertIn("git rev-parse HEAD", command[-3])
                self.assertIn("git status --porcelain", command[-3])
                self.assertNotIn("--volume", command)
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    def build(self):
        with patch.object(preset, "wasmtime_definition", return_value=self.definition), \
                patch.object(ci.subprocess, "run", side_effect=self.process):
            return ci.build_wasmtime_images(self.tasks, REVISION, self.root / "receipt")

    def test_builds_all_seven_with_only_recipes_then_verifies_and_records_private_timings(self):
        result = self.build()
        self.assertEqual(result["images_built"], 7)
        receipt = read_json(self.root / "receipt/receipt.json")
        self.assertEqual(receipt["task_repo_revision"], REVISION)
        self.assertEqual(receipt["dataset"], preset.WASMTIME_DATASET)
        self.assertEqual(len([c for c in self.commands if c[:2] == ["docker", "build"]]), 7)
        self.assertEqual(len([c for c in self.commands if c[:2] == ["docker", "run"]]), 7)
        for record in receipt["images"].values():
            self.assertEqual(record["image_id"], "sha256:" + "c" * 64)
            self.assertGreaterEqual(record["build_seconds"], 0)
            self.assertGreaterEqual(record["verification_seconds"], 0)
        self.assertEqual((self.root / "receipt").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / "receipt/receipt.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse(any("prune" in c for c in self.commands))

    def test_unsafe_context_recipe_or_metadata_is_rejected_before_docker(self):
        directory = self.tasks / "tasks" / next(iter(self.definition["tasks"]))
        for name, value, error in ((".dockerignore", "", ".dockerignore"),
                                   ("Dockerfile", "FROM debian:12\nCOPY gold.patch /oracle\n", "COPY/ADD"),
                                   ("Dockerfile", "FROM debian:12\nONBUILD ADD . /oracle\n", "COPY/ADD"),
                                   ("Dockerfile", "FROM different\n", "validated recipe"),
                                   ("task.yaml", "{}", "metadata")):
            path = directory / name
            original = path.read_text()
            path.write_text(value)
            with self.subTest(name=name, value=value), self.assertRaisesRegex(ci.UsageError, error):
                self.build()
            path.write_text(original)
            self.assertFalse(any(c[0] == "docker" for c in self.commands))
            self.assertFalse((self.root / "receipt").exists())
        (directory / "Dockerfile").unlink()
        (directory / "Dockerfile").symlink_to(directory / "gold.patch")
        with self.assertRaisesRegex(ValueError, "Symlinks"):
            self.build()

    def test_mismatched_revision_or_secret_environment_fails_before_docker(self):
        with patch.object(ci.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="b" * 40)):
            with self.assertRaisesRegex(ci.UsageError, "clean and match"):
                ci.build_wasmtime_images(self.tasks, REVISION, self.root / "receipt")
        with patch.dict(os.environ, {"WASMTIME_TASKS_TOKEN": "never-build-with-token"}):
            with self.assertRaisesRegex(ci.UsageError, "before secrets exist"):
                self.build()

    def test_failed_verification_never_writes_a_success_receipt(self):
        process = self.process
        def fail(command, **kwargs):
            if command[:2] == ["docker", "run"]:
                raise subprocess.CalledProcessError(1, command)
            return process(command, **kwargs)
        with patch.object(preset, "wasmtime_definition", return_value=self.definition), \
                patch.object(ci.subprocess, "run", side_effect=fail), \
                self.assertRaisesRegex(ci.UsageError, "checkout verification failed"):
            ci.build_wasmtime_images(self.tasks, REVISION, self.root / "receipt")
        self.assertFalse((self.root / "receipt/receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
