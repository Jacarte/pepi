""".github/workflows/pepi-evals.yml: offline structure and security checks (no network, Docker or gh).

Structural tests need a YAML parser: PyYAML in this interpreter, else a local interpreter that has it
(the swebench venv); without one they skip locally and fail when CI is set. Text-level checks (run
blocks, runs-on lines, xtrace, pins) always run.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from evals.swebench import ci

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github/workflows/pepi-evals.yml"
RUN_EVALS = REPO / "evals/ci/run-evals.sh"
SELF_HOSTED_LABELS = ["self-hosted", "pepi-evals"]
HEAVY_RUNS_ON = "${{ fromJSON(vars.EVAL_RUNS_ON || '[\"self-hosted\",\"pepi-evals\"]') }}"
HOSTED_ALTERNATIVE = "# EVAL_RUNS_ON='[\"ubuntu-latest\"]'  (hosted: see free-disk caveats)"
ENVIRONMENT = "pepi-evals"
SECRET_JOBS = ("baseline", "candidate", "report")
LABEL_GUARD = "github.event.label.name == 'run-evals'"
FORK_GUARD = "github.event.pull_request.head.repo.full_name == github.repository"
CACHE_PATH = "pepi-baseline-cache"
PRESENCE_CHECK = re.compile(r"\$\{\{ secrets\.[A-Z_]+ != '' \}\}")
CONVERT = ("import json, sys, yaml\n"
           "data = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))\n"
           "if True in data: data['on'] = data.pop(True)\n"
           "print(json.dumps(data))\n")


def load_workflow():
    """Parsed workflow (the YAML 1.1 `on` key normalised), or None when no YAML parser is available."""
    try:
        import yaml
    except ImportError:
        candidates = [os.environ.get("PEPI_EVAL_VENV", ""), str(Path.home() / ".venvs/swebench")]
        for venv in candidates:
            python = Path(venv) / "bin/python" if venv else None
            if python is None or not python.is_file():
                continue
            result = subprocess.run([str(python), "-c", CONVERT, str(WORKFLOW)], capture_output=True,
                                    text=True, check=False)
            if result.returncode == 0:
                return json.loads(result.stdout)
        return None
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    if True in data:
        data["on"] = data.pop(True)
    return data


def require_workflow(loader=load_workflow, environ=os.environ):
    """The parsed workflow; without a YAML parser skip locally, but fail under CI (a skip would hide every
    structural check)."""
    workflow = loader()
    if workflow is None:
        if environ.get("CI"):
            raise RuntimeError("no YAML parser available (PyYAML or ~/.venvs/swebench) and CI is set")
        raise unittest.SkipTest("no YAML parser available (PyYAML or ~/.venvs/swebench)")
    return workflow


def run_blocks(text: str) -> list:
    """(line number, script) of every `run: |` literal block, without a YAML parser."""
    lines = text.splitlines()
    blocks = []
    for index, line in enumerate(lines):
        match = re.fullmatch(r"(\s*)(?:- )?run: \|\s*", line)
        if not match:
            continue
        key_indent = len(match.group(1))
        body = []
        for following in lines[index + 1:]:
            if following.strip() and len(following) - len(following.lstrip()) <= key_indent:
                break
            body.append(following)
        while body and not body[-1].strip():
            body.pop()
        indent = min(len(item) - len(item.lstrip()) for item in body if item.strip())
        blocks.append((index + 1, "\n".join(item[indent:] for item in body) + "\n"))
    return blocks


def all_steps(workflow):
    for job_name, job in workflow["jobs"].items():
        for index, step in enumerate(job["steps"]):
            yield job_name, index, step


def step_index(steps, predicate) -> list:
    return [index for index, step in enumerate(steps) if predicate(step)]


def runs(step, needle) -> bool:
    return needle in (step.get("run") or "")


def uses(step, action) -> bool:
    return (step.get("uses") or "").split("@")[0] == action


class WorkflowTextTests(unittest.TestCase):
    """Checks that need no YAML parser."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.blocks = run_blocks(cls.text)

    def test_every_run_block_is_strict_bash_without_expressions(self):
        self.assertGreater(len(self.blocks), 20)
        for line, script in self.blocks:
            with self.subTest(line=line):
                self.assertTrue(script.startswith("set -euo pipefail\n"))
                # Attacker-controlled strings must reach scripts through env:, never ${{ }}.
                self.assertNotIn("${{", script)
                result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_xtrace_anywhere(self):
        self.assertIsNone(re.search(r"\bset\s+-[a-z]*x|\bset\s+-o\s+xtrace|\bbash\s+-[a-z]*x", self.text))

    def test_eval_jobs_default_to_self_hosted_and_lookup_report_stay_hosted(self):
        lines = self.text.splitlines()
        positions = [index for index, line in enumerate(lines) if re.match(r"\s*runs-on:", line)]
        self.assertEqual(len(positions), 4)
        seen = {}
        for index in positions:
            job = next(re.fullmatch(r"  ([a-z]+):", line).group(1) for line in reversed(lines[:index])
                       if re.fullmatch(r"  ([a-z]+):", line))
            value = lines[index].split("runs-on:", 1)[1].strip()
            comment = lines[index + 1].strip()
            seen[job] = value
            with self.subTest(job=job):
                if job in ("baseline", "candidate"):
                    self.assertEqual(value, HEAVY_RUNS_ON)
                    self.assertEqual(comment, HOSTED_ALTERNATIVE)
                else:
                    self.assertEqual(value, "ubuntu-latest")
                    self.assertTrue(comment.startswith("# Always GitHub-hosted"), comment)
        self.assertEqual(set(seen), {"lookup", "baseline", "candidate", "report"})
        default = re.search(r"\|\| '(\[.*\])'", HEAVY_RUNS_ON).group(1)
        self.assertEqual(json.loads(default), SELF_HOSTED_LABELS)

    def test_task_subset_variable_feeds_run_evals_and_the_cache_key(self):
        self.assertIn("  PEPI_CI_IDS: ${{ vars.EVAL_IDS }}\n", self.text)
        script = next(script for _, script in self.blocks if "ci cache-key" in script)
        self.assertIn('--ids "${PEPI_CI_IDS:-}"', script)
        self.assertIn('[[ -z "${PEPI_CI_IDS:-}" ]] || export PEPI_EVAL_IDS="$PEPI_CI_IDS"',
                      RUN_EVALS.read_text(encoding="utf-8"))

    def test_no_privileged_triggers_or_set_env_commands(self):
        code = "\n".join(line for line in self.text.splitlines() if not line.lstrip().startswith("#"))
        for forbidden in ("pull_request_target", "workflow_run", "::set-env", "::add-path", "ACTIONS_ALLOW_UNSECURE"):
            self.assertNotIn(forbidden, code)

    def test_pin_defaults_match_run_evals(self):
        script = RUN_EVALS.read_text(encoding="utf-8")
        for variable, name in (("PEPI_CI_PI_VERSION", "EVAL_PI_VERSION"), ("PEPI_CI_SUBAGENTS_VERSION", "EVAL_SUBAGENTS_VERSION"),
                               ("PEPI_CI_LITELLM_VERSION", "EVAL_LITELLM_VERSION"), ("PEPI_CI_NODE_IMAGE", "EVAL_NODE_IMAGE"),
                               ("PEPI_CI_PRESET", "EVAL_PRESET")):
            with self.subTest(variable=variable):
                default = re.search(r"\$\{" + variable + r":-([^}]+)\}", script).group(1)
                self.assertIn(f"{variable}: ${{{{ vars.{name} || '{default}' }}}}", self.text)

    def test_cache_key_covers_every_pin_and_this_workflow(self):
        script = next(script for _, script in self.blocks if "ci cache-key" in script)
        for flag in ("--pi-version", "--subagents-version", "--litellm-version", "--node-image", "--task-repo-revision",
                     "--swebench-revision", "--python-version", "--timeout-seconds", "--cpus", "--memory",
                     "--concurrency", "--dataset", "--profile", "--preset", "--config-dir", "--harness-root ."):
            self.assertIn(flag, script)
        # ids/network exactly as run-evals.sh resolves them (same defaults).
        run_evals = RUN_EVALS.read_text(encoding="utf-8")
        for variable, flag in (("PEPI_CI_IDS", "--ids"), ("PEPI_CI_NETWORK", "--network")):
            default = re.search(r"\$\{" + variable + r":-([^}]*)\}", run_evals)
            expected = default.group(1) if default else ""
            self.assertIn(f'{flag} "${{{variable}:-{expected}}}"', script)
        self.assertEqual(ci.PIN_DEFAULTS["network"], "pepi-eval")
        self.assertIn('--setting "workflow_sha256=$workflow_sha"', script)
        self.assertIn(".github/workflows/pepi-evals.yml", script)

    def test_swebench_fallback_revision_matches_the_verified_preset(self):
        preset = json.loads((REPO / "evals/swebench/presets/verified20-v1.json").read_text(encoding="utf-8"))
        self.assertIn(f"PEPI_CI_SWEBENCH_REVISION: {preset['swebench_revision']}", self.text)

    def test_preset_step_resolves_the_pinned_revisions_from_the_preset_files(self):
        script = next(script for _, script in self.blocks if "task_repo_revision" in script)
        expectations = {
            "verified20-v1": ("verified", "task_repo_revision"),
            "multilingual10-v1": ("multilingual", "dataset_revision"),
        }
        for preset, (source, field) in expectations.items():
            with self.subTest(preset=preset), tempfile.TemporaryDirectory() as scratch:
                output = Path(scratch) / "out"
                output.write_text("")
                env = {key: value for key, value in os.environ.items() if not key.startswith("PEPI_CI_")}
                env.update(GITHUB_OUTPUT=str(output), PEPI_CI_PRESET=preset, PYTHONDONTWRITEBYTECODE="1",
                           PEPI_CI_SWEBENCH_REVISION="02e7a74ffd0b707aab73d203fe87bdc7c76afc8e")
                result = subprocess.run(["bash", "-c", script], cwd=REPO, env=env, capture_output=True,
                                        text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                values = dict(line.split("=", 1) for line in output.read_text().splitlines())
                data = json.loads((REPO / f"evals/swebench/presets/{preset}.json").read_text(encoding="utf-8"))
                self.assertEqual(values["task_source"], source)
                self.assertEqual(values["task_revision"], data[field])
                self.assertEqual(values["dataset"], data["dataset"])
                self.assertRegex(values["swebench_revision"], r"^[0-9a-f]{40}$")

    def test_configuration_check_fails_fast_without_printing_values(self):
        script = next(script for _, script in self.blocks if "Repository variable PROFILE" in script)
        self.assertNotIn("HAS_PI_AUTH", script)
        base = {key: value for key, value in os.environ.items() if not key.startswith("PEPI_CI_")}
        base.pop("EVAL_RUNS_ON", None)
        good = dict(base, PEPI_CI_PROFILE="bedrock", PEPI_CI_PRESET="verified20-v1", PEPI_CI_PI_VERSION="1.1.0",
                    PEPI_CI_SUBAGENTS_VERSION="0.78.0", PEPI_CI_LITELLM_VERSION="4.2.0", PEPI_CI_TIMEOUT_SECONDS="",
                    PEPI_CI_CPUS="4", PEPI_CI_MEMORY="8g")
        smoke = "sympy__sympy-16886,django__django-11095,sphinx-doc__sphinx-8551,scikit-learn__scikit-learn-25931"
        for extra in ({}, dict(PEPI_CI_IDS=smoke), dict(PEPI_CI_IDS="django__django-11095"),
                      dict(EVAL_RUNS_ON='["ubuntu-latest"]'), dict(EVAL_RUNS_ON='["self-hosted","pepi-evals"]')):
            with self.subTest(good=extra):
                result = subprocess.run(["bash", "-c", script], env=dict(good, **extra), capture_output=True,
                                        text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for change, message in ((dict(PEPI_CI_PROFILE=""), "PROFILE is not set"),
                                (dict(PEPI_CI_PROFILE="../x"), "unsafe characters"),
                                (dict(PEPI_CI_PRESET="other"), "EVAL_PRESET"),
                                (dict(PEPI_CI_PI_VERSION="1.1.0; rm -rf /"), "PEPI_CI_PI_VERSION"),
                                (dict(PEPI_CI_TIMEOUT_SECONDS="ten"), "EVAL_TIMEOUT_SECONDS"),
                                (dict(PEPI_CI_IDS="sympy__sympy-16886, django__django-11095"), "EVAL_IDS"),
                                (dict(PEPI_CI_IDS="/etc/passwd"), "EVAL_IDS"),
                                (dict(PEPI_CI_IDS="profile.sh"), "EVAL_IDS"),
                                (dict(PEPI_CI_IDS="sympy__sympy-16886,"), "EVAL_IDS"),
                                (dict(EVAL_RUNS_ON="ubuntu-latest"), "EVAL_RUNS_ON"),
                                (dict(EVAL_RUNS_ON="[]"), "EVAL_RUNS_ON"),
                                (dict(EVAL_RUNS_ON='{"group": "x"}'), "EVAL_RUNS_ON"),
                                (dict(EVAL_RUNS_ON='["self hosted; rm -rf /"]'), "EVAL_RUNS_ON")):
            with self.subTest(change=change):
                result = subprocess.run(["bash", "-c", script], env=dict(good, **change), capture_output=True,
                                        text=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stdout)
                self.assertNotIn("rm -rf", result.stdout)

    def test_secret_presence_checks_run_in_each_environment_job_without_printing_values(self):
        scripts = [script for _, script in self.blocks if "HAS_PI_AUTH" in script]
        self.assertEqual(len(scripts), len(SECRET_JOBS))
        self.assertEqual(len(set(scripts)), 1)
        base = {key: value for key, value in os.environ.items() if not key.startswith("HAS_")}
        result = subprocess.run(["bash", "-c", scripts[0]], env=dict(base, HAS_PI_AUTH="true", HAS_EVAL_ENV="true"),
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for change, message in ((dict(HAS_PI_AUTH="false", HAS_EVAL_ENV="true"), "Secret PI_AUTH is not set"),
                                (dict(HAS_PI_AUTH="true", HAS_EVAL_ENV="false"), "Secret EVAL_ENV is not set")):
            with self.subTest(change=change):
                result = subprocess.run(["bash", "-c", scripts[0]], env=dict(base, **change), capture_output=True,
                                        text=True, check=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message + " in the pepi-evals environment", result.stdout)

    def test_head_must_carry_the_executing_workflow(self):
        script = next(script for _, script in self.blocks if "executing/.github/workflows/pepi-evals.yml" in script)
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            executing = root / "executing/.github/workflows/pepi-evals.yml"
            head = root / "harness/.github/workflows/pepi-evals.yml"
            executing.parent.mkdir(parents=True)
            executing.write_text("name: new\n")
            for content, expected in ((None, 1), ("name: old\n", 1), ("name: new\n", 0)):
                with self.subTest(head=content):
                    if content is None:
                        head.unlink(missing_ok=True)
                    else:
                        head.parent.mkdir(parents=True, exist_ok=True)
                        head.write_text(content)
                    result = subprocess.run(["bash", "-c", script], cwd=root, capture_output=True, text=True,
                                            check=False)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    if expected:
                        self.assertIn("Rebase the PR on main", result.stdout)

    def test_missing_yaml_parser_skips_locally_but_fails_under_ci(self):
        with self.assertRaises(unittest.SkipTest):
            require_workflow(loader=lambda: None, environ={})
        with self.assertRaises(RuntimeError):
            require_workflow(loader=lambda: None, environ={"CI": "true"})
        self.assertEqual(require_workflow(loader=lambda: {"jobs": {}}, environ={"CI": "true"}), {"jobs": {}})

    @unittest.skipUnless(shutil.which("actionlint"), "actionlint not installed")
    def test_actionlint(self):
        result = subprocess.run(["actionlint", str(WORKFLOW)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class WorkflowStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = require_workflow()
        cls.jobs = cls.workflow["jobs"]

    def test_triggers_are_label_events_and_pushes_to_main_only(self):
        self.assertEqual(self.workflow["on"], {"pull_request": {"types": ["labeled"]}, "push": {"branches": ["main"]}})

    def test_pr_jobs_check_the_label_and_refuse_forks(self):
        self.assertEqual(set(self.jobs), {"lookup", "baseline", "candidate", "report"})
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                condition = job["if"]
                self.assertIn(LABEL_GUARD, condition)
                self.assertIn(FORK_GUARD, condition)
        for name in ("candidate", "report"):
            self.assertIn("github.event_name == 'pull_request'", self.jobs[name]["if"])

    def test_permissions_are_read_only_except_comments_in_report(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                expected = {"contents": "read", "pull-requests": "write"} if name == "report" else {"contents": "read"}
                self.assertEqual(job["permissions"], expected)

    def test_concurrency_is_per_pr_and_ignores_unrelated_label_events(self):
        group = self.workflow["concurrency"]["group"]
        for fragment in ("github.event.pull_request.number", "'run-evals'", "github.run_id", "'pepi-evals-main'"):
            self.assertIn(fragment, group)
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"],
                         "${{ github.event_name == 'pull_request' }}")

    def test_job_timeouts(self):
        self.assertEqual({name: job["timeout-minutes"] for name, job in self.jobs.items()},
                         {"lookup": 15, "baseline": 350, "candidate": 350, "report": 30})

    def test_checkouts_never_persist_credentials(self):
        checkouts = [(job, step) for job, _, step in all_steps(self.workflow) if uses(step, "actions/checkout")]
        self.assertGreaterEqual(len(checkouts), 9)
        for job, step in checkouts:
            with self.subTest(job=job, step=step.get("name")):
                self.assertIs(step["with"]["persist-credentials"], False)

    def test_external_repositories_are_checked_out_at_lookup_resolved_pins(self):
        for job in ("baseline", "candidate"):
            steps = self.jobs[job]["steps"]
            by_repo = {step["with"].get("repository"): step for step in steps
                       if uses(step, "actions/checkout") and step["with"].get("repository")}
            self.assertEqual(set(by_repo), {"SWE-bench/SWE-bench", "SWE-bench/swe-bench-tasks"})
            self.assertEqual(by_repo["SWE-bench/SWE-bench"]["with"]["ref"], "${{ needs.lookup.outputs.swebench_revision }}")
            self.assertEqual(by_repo["SWE-bench/swe-bench-tasks"]["with"]["ref"], "${{ needs.lookup.outputs.task_revision }}")

    def test_secrets_appear_only_in_the_steps_that_need_them(self):
        self.assertNotIn("secrets.", json.dumps(self.workflow.get("env", {})))
        for name, job in self.jobs.items():
            self.assertNotIn("secrets.", json.dumps(job.get("env", {})), name)
            self.assertNotIn("secrets.", json.dumps(job.get("outputs", {})), name)
        for job, _, step in all_steps(self.workflow):
            for key, value in (step.get("env") or {}).items():
                value = str(value)
                if "secrets." in value:
                    with self.subTest(job=job, step=step.get("name"), key=key):
                        allowed = PRESENCE_CHECK.fullmatch(value) or runs(step, "ci secrets-setup")
                        self.assertTrue(allowed)
                if "github.token" in value:
                    self.assertTrue(runs(step, "gh pr comment") or runs(step, "gh api -X DELETE"), step.get("name"))
            self.assertNotIn("secrets.", json.dumps(step.get("with") or {}), step.get("name"))

    def test_jobs_that_write_secrets_mask_them_and_always_clean_up(self):
        writers = 0
        for name, job in self.jobs.items():
            steps = job["steps"]
            setups = step_index(steps, lambda step: runs(step, "ci secrets-setup"))
            if not setups:
                continue
            writers += 1
            with self.subTest(job=name):
                self.assertEqual(len(setups), 1)
                mask = steps[setups[0] + 1]
                self.assertIn('cat "$RUNNER_TEMP/pepi-masks"', mask["run"])
                self.assertIn('rm -f "$RUNNER_TEMP/pepi-masks"', mask["run"])
                cleanups = step_index(steps, lambda step: runs(step, "ci secrets-cleanup"))
                self.assertEqual(len(cleanups), 1)
                self.assertGreater(cleanups[0], setups[0])
                self.assertIn("always()", steps[cleanups[0]]["if"])
                self.assertIn("rm -rf", steps[cleanups[0]]["run"])
                # Config assembly (which runs profile.sh from a checkout) happens before secrets exist.
                for index in step_index(steps, lambda step: runs(step, "ci assemble-config")):
                    self.assertLess(index, setups[0])
        self.assertEqual(writers, 3)
        self.assertEqual(step_index(self.jobs["lookup"]["steps"], lambda step: runs(step, "secrets-setup")), [])

    def test_secret_scans_read_the_values_from_the_secrets_setup_files(self):
        scans = [(job, step) for job, _, step in all_steps(self.workflow) if runs(step, "ci scan-artifacts")]
        self.assertEqual(len(scans), 3)
        for job, step in scans:
            with self.subTest(job=job, step=step.get("name")):
                self.assertNotIn("env", step)
                self.assertIn('--secrets-dir "$RUNNER_TEMP/pepi-secrets"', step["run"])
                self.assertIn('--env-file "$RUNNER_TEMP/pepi-launch/.env"', step["run"])
                self.assertNotIn("--no-secret-values", step["run"])
                # Each invocation (backslash continuations joined) carries the two sources.
                for command in step["run"].replace("\\\n", " ").splitlines():
                    if "ci scan-artifacts" in command:
                        self.assertTrue('"${sources[@]}"' in command or "--secrets-dir" in command, command)
            cleanup = step_index(self.jobs[job]["steps"], lambda other: runs(other, "ci secrets-cleanup"))
            self.assertLess(self.jobs[job]["steps"].index(step), cleanup[0])

    def test_baseline_cache_decision_uses_a_single_run_verdict_and_fallback_is_fatal(self):
        step = next(step for step in self.jobs["baseline"]["steps"] if step.get("id") == "cacheable")
        self.assertIn('ci verdict --single "$GITHUB_WORKSPACE/pepi-baseline-cache" --exit-code 0', step["run"])
        self.assertNotIn("--candidate", step["run"])
        scripts = "\n".join(step.get("run") or "" for _, _, step in all_steps(self.workflow))
        self.assertNotIn("--allow-harness-fallback", scripts)
        fallback = [job for job, _, step in all_steps(self.workflow) if runs(step, "--fallback-root")]
        self.assertEqual(sorted(fallback), ["baseline", "lookup"])

    def test_only_existing_helper_commands_and_scripts_are_used(self):
        scripts = "\n".join(step.get("run") or "" for _, _, step in all_steps(self.workflow))
        used = set(re.findall(r"evals\.swebench\.ci\s+([a-z][a-z-]*)", scripts))
        subparsers = next(action for action in ci.parser()._actions if action.dest == "command")
        self.assertTrue(used)
        self.assertLessEqual(used, set(subparsers.choices))
        for script in set(re.findall(r"harness/(evals/ci/[a-z-]+\.sh)", scripts)):
            self.assertTrue((REPO / script).is_file(), script)
        self.assertIn("-m evals.swebench.multilingual import", scripts)

    def test_secret_scan_precedes_every_upload_cache_save_and_comment(self):
        for name, job in self.jobs.items():
            steps = job["steps"]
            scans = step_index(steps, lambda step: runs(step, "ci scan-artifacts"))
            publishing = step_index(steps, lambda step: uses(step, "actions/upload-artifact")
                                    or uses(step, "actions/cache/save") or runs(step, "gh pr comment"))
            with self.subTest(job=name):
                if publishing:
                    self.assertTrue(scans)
                    self.assertLess(min(scans), min(publishing))
                    for index in publishing:
                        self.assertNotIn("always()", steps[index].get("if", ""))
        uploads = [step for _, _, step in all_steps(self.workflow) if uses(step, "actions/upload-artifact")]
        long_lived = {step["with"]["name"] for step in uploads if step["with"]["retention-days"] == 30}
        self.assertEqual(long_lived, {"pepi-evals-baseline", "pepi-evals-candidate", "pepi-evals-report"})
        for step in uploads:
            self.assertEqual(step["with"]["if-no-files-found"], "error")

    def test_baseline_rechecks_the_cache_key_with_its_own_variables_before_saving(self):
        lookup_step = next(step for step in self.jobs["lookup"]["steps"] if step.get("id") == "key")
        steps = self.jobs["baseline"]["steps"]
        recheck = step_index(steps, lambda step: step.get("id") == "recheck")
        self.assertEqual(len(recheck), 1)
        step = steps[recheck[0]]

        def invocation(script):
            start = script.index("python3 -m evals.swebench.ci cache-key")
            return script[start:script.index('--json > "$key_json"', start)]

        # Same command and the same env names as lookup, fed with lookup's preset outputs.
        self.assertEqual(invocation(step["run"]), invocation(lookup_step["run"]))
        self.assertIn('config_dir="$RUNNER_TEMP/pepi-config-baseline"', lookup_step["run"])
        self.assertIn('config_dir="$RUNNER_TEMP/pepi-config"', step["run"])
        workflow_sha = lookup_step["run"][lookup_step["run"].index("workflow_sha=$("):].split(")\n", 1)[0]
        self.assertIn(workflow_sha, step["run"])
        expected_env = {name: value.replace("steps.preset.outputs.", "needs.lookup.outputs.")
                        for name, value in lookup_step["env"].items()}
        expected_env["LOOKUP_KEY"] = "${{ needs.lookup.outputs.key }}"
        self.assertEqual(step["env"], expected_env)
        self.assertNotIn("secrets.", json.dumps(step))
        self.assertIn('"${key%$week_suffix}" == "${LOOKUP_KEY%$week_suffix}"', step["run"])
        self.assertIn("::warning::", step["run"])
        # After the config is checked, before the secrets exist and before the cache is saved.
        assemble = step_index(steps, lambda other: runs(other, "EXPECTED_SHA256"))
        secrets = step_index(steps, lambda other: runs(other, "ci secrets-setup"))
        save = step_index(steps, lambda other: uses(other, "actions/cache/save"))
        self.assertLess(assemble[0], recheck[0])
        self.assertLess(recheck[0], secrets[0])
        self.assertLess(recheck[0], save[0])
        cacheable = next(other for other in steps if other.get("id") == "cacheable")
        self.assertEqual(cacheable["env"]["KEY_MATCHES"], "${{ steps.recheck.outputs.matches }}")
        self.assertIn('if [[ "$KEY_MATCHES" != true ]]; then', cacheable["run"])

    def test_baseline_key_recheck_matches_lookup_and_flags_overridden_variables(self):
        lookup_step = next(step for step in self.jobs["lookup"]["steps"] if step.get("id") == "key")
        recheck_step = next(step for step in self.jobs["baseline"]["steps"] if step.get("id") == "recheck")
        revisions = {"TASK_REVISION": "a" * 40, "SWEBENCH_REVISION": "b" * 40, "DATASET": "princeton-nlp/SWE-bench_Verified"}
        with tempfile.TemporaryDirectory() as scratch:
            runner_temp = Path(scratch)
            for name in ("pepi-config-baseline", "pepi-config"):
                (runner_temp / name).mkdir()
                (runner_temp / name / "settings.json").write_text("{}\n")
            base = {key: value for key, value in os.environ.items() if not key.startswith("PEPI_CI_")}
            base.update(revisions, RUNNER_TEMP=str(runner_temp), PYTHONDONTWRITEBYTECODE="1",
                        PEPI_CI_PROFILE="bedrock", PEPI_CI_PRESET="verified20-v1", PEPI_CI_PI_VERSION="1.1.0",
                        PEPI_CI_SUBAGENTS_VERSION="0.78.0", PEPI_CI_LITELLM_VERSION="4.2.0",
                        PEPI_CI_NODE_IMAGE="node:22-bookworm-slim", PEPI_CI_PYTHON_VERSION="3.12",
                        PEPI_CI_TIMEOUT_SECONDS="", PEPI_CI_CPUS="4", PEPI_CI_MEMORY="8g", PEPI_CI_IDS="")

            def outputs(script, env):
                output = runner_temp / "github-output"
                output.write_text("")
                result = subprocess.run(["bash", "-c", script], cwd=REPO, env=dict(env, GITHUB_OUTPUT=str(output)),
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                return dict(line.split("=", 1) for line in output.read_text().splitlines()), result.stdout

            lookup_key = outputs(lookup_step["run"], base)[0]["key"]
            values, stdout = outputs(recheck_step["run"], dict(base, LOOKUP_KEY=lookup_key))
            self.assertEqual(values, {"matches": "true"})
            self.assertNotIn("::warning::", stdout)
            # Approved after a week boundary: the week suffix alone does not block the save.
            other_week = re.sub(r"-[0-9]{4}-W[0-9]{2}$", "-1999-W01", lookup_key)
            self.assertEqual(outputs(recheck_step["run"], dict(base, LOOKUP_KEY=other_week))[0], {"matches": "true"})
            # An environment-level variable changed a setting only in this job.
            for name, value in (("PEPI_CI_CPUS", "8"), ("PEPI_CI_IDS", "sympy__sympy-16886"),
                                ("PEPI_CI_PRESET", "multilingual10-v1"), ("PEPI_CI_TIMEOUT_SECONDS", "900")):
                with self.subTest(variable=name):
                    values, stdout = outputs(recheck_step["run"], dict(base, LOOKUP_KEY=lookup_key, **{name: value}))
                    self.assertEqual(values, {"matches": "false"})
                    self.assertIn("::warning::", stdout)
            # A cache-key failure (e.g. an unsafe overridden profile) is not cacheable, never a job failure.
            values, stdout = outputs(recheck_step["run"], dict(base, LOOKUP_KEY=lookup_key, PEPI_CI_PROFILE="../x"))
            self.assertEqual(values, {"matches": "false"})

    def test_cache_is_saved_only_by_a_clean_baseline_and_paths_match(self):
        locations = [(job, step) for job, _, step in all_steps(self.workflow) if uses(step, "actions/cache/save")
                     or uses(step, "actions/cache/restore") or uses(step, "actions/cache")]
        self.assertNotIn("actions/cache", [step["uses"].split("@")[0] for _, step in locations])
        saves = [(job, step) for job, step in locations if uses(step, "actions/cache/save")]
        self.assertEqual([job for job, _ in saves], ["baseline"])
        self.assertEqual(saves[0][1]["if"], "steps.cacheable.outputs.cacheable == 'true'")
        self.assertEqual(saves[0][1]["with"]["key"], "${{ needs.lookup.outputs.key }}")
        restores = {job: step for job, step in locations if uses(step, "actions/cache/restore")}
        self.assertEqual(set(restores), {"lookup", "report"})
        self.assertIs(restores["lookup"]["with"]["lookup-only"], True)
        self.assertIs(restores["report"]["with"]["fail-on-cache-miss"], True)
        for _, step in locations:
            self.assertEqual(step["with"]["path"], CACHE_PATH)

    def test_report_runs_after_candidate_and_a_fresh_or_cached_baseline(self):
        report = self.jobs["report"]
        self.assertEqual(report["needs"], ["lookup", "baseline", "candidate"])
        condition = report["if"]
        for fragment in ("!cancelled()", "needs.lookup.result == 'success'", "needs.candidate.result == 'success'",
                         "needs.baseline.result == 'success'",
                         "needs.baseline.result == 'skipped' && needs.lookup.outputs.hit == 'true'"):
            self.assertIn(fragment, condition)
        # always() would keep a stale report running (with the secrets) after a re-label cancelled the run.
        self.assertNotIn("always()", condition)
        self.assertFalse(condition.lstrip().startswith("!"))
        self.assertIn("needs.lookup.outputs.hit != 'true'", self.jobs["baseline"]["if"])
        self.assertEqual(self.jobs["candidate"]["needs"], "lookup")
        steps = report["steps"]
        verdict = step_index(steps, lambda step: runs(step, "ci verdict"))
        final = steps[-1]
        self.assertEqual(final["if"], "steps.verdict.outputs.ok == 'false'")
        self.assertIn("exit 1", final["run"])
        self.assertLess(verdict[0], len(steps) - 1)
        narrate = step_index(steps, lambda step: runs(step, "evals/ci/narrate.sh"))
        self.assertEqual(len(narrate), 1)
        self.assertNotIn("continue-on-error", steps[narrate[0]])

    def test_only_the_secret_jobs_use_the_environment_and_check_it_first(self):
        self.assertEqual({name: job.get("environment") for name, job in self.jobs.items()},
                         {"lookup": None, "baseline": ENVIRONMENT, "candidate": ENVIRONMENT, "report": ENVIRONMENT})
        # lookup runs before any approval: no secrets at all (not even presence checks).
        self.assertNotIn("secrets.", json.dumps(self.jobs["lookup"]))
        for name in SECRET_JOBS:
            with self.subTest(job=name):
                first = self.jobs[name]["steps"][0]
                self.assertIn("HAS_PI_AUTH", first["env"])
                self.assertIn("HAS_EVAL_ENV", first["env"])
                self.assertIn("pepi-evals environment", first["run"])
        self.assertNotIn("schedule", self.workflow["on"])
        self.assertNotIn("workflow_dispatch", self.workflow["on"])

    def test_report_consumes_the_label_and_flags_regressions_without_failing(self):
        steps = self.jobs["report"]["steps"]
        comment = step_index(steps, lambda step: runs(step, "gh pr comment"))
        removal = step_index(steps, lambda step: runs(step, "labels/run-evals"))
        annotate = step_index(steps, lambda step: runs(step, "ci annotate"))
        self.assertEqual((len(comment), len(removal), len(annotate)), (1, 1, 1))
        self.assertLess(comment[0], removal[0])
        self.assertLess(comment[0], annotate[0])
        for index in removal + annotate:
            self.assertEqual(steps[index]["if"], "${{ !cancelled() }}")
            self.assertNotIn("continue-on-error", steps[index])
        removal_step = steps[removal[0]]
        self.assertIn('gh api -X DELETE "repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/labels/run-evals"', removal_step["run"])
        self.assertEqual(removal_step["env"]["PR_NUMBER"], "${{ github.event.pull_request.number }}")
        self.assertNotIn("${{", removal_step["run"])
        # Never fails the job: the failure is captured, and only a non-404 failure warns.
        self.assertIn(") || status=$?", removal_step["run"])
        self.assertIn('*"HTTP 404"*', removal_step["run"])
        self.assertIn("::warning::", removal_step["run"])
        self.assertIn("|| true", steps[annotate[0]]["run"])
        for name in ("lookup", "baseline", "candidate"):
            blob = json.dumps(self.jobs[name])
            self.assertNotIn("gh pr", blob, name)
            self.assertNotIn("gh api", blob, name)
            self.assertNotIn("ci annotate", blob, name)

    def test_handoffs_are_public_safe_and_match_between_jobs(self):
        uploads = {step["with"]["name"]: (job, step) for job, _, step in all_steps(self.workflow)
                   if uses(step, "actions/upload-artifact")}
        downloads = [step["with"]["name"] for _, _, step in all_steps(self.workflow)
                     if uses(step, "actions/download-artifact")]
        self.assertEqual(sorted(downloads), ["pepi-evals-baseline-handoff", "pepi-evals-candidate-handoff"])
        for name in downloads:
            job, step = uploads[name]
            with self.subTest(name=name):
                self.assertEqual(step["with"]["retention-days"], 1)
                self.assertTrue(step["with"]["path"].endswith(name.replace("pepi-evals-", "pepi-out/")), step["with"]["path"])
                slim = "\n".join(other.get("run") or "" for other in self.jobs[job]["steps"])
                self.assertIn('--out "$handoff_dir" --kind handoff', slim)
        # The cache kind (manifest.json/execution.json included) only goes to actions/cache, never to an upload.
        cache_kind = [job for job, _, step in all_steps(self.workflow) if runs(step, "--kind cache")]
        self.assertEqual(cache_kind, ["baseline"])
        self.assertIn('--out "$cache_dir" --kind cache', next(step["run"] for step in self.jobs["baseline"]["steps"]
                                                            if runs(step, "--kind cache")))
        for name, (job, step) in uploads.items():
            self.assertNotEqual(step["with"]["path"], CACHE_PATH, name)

    def test_lookup_checks_the_executing_workflow_before_using_the_harness(self):
        steps = self.jobs["lookup"]["steps"]
        sparse = [index for index, step in enumerate(steps) if uses(step, "actions/checkout")
                  and step["with"].get("path") == "executing"]
        self.assertEqual(len(sparse), 1)
        checkout = steps[sparse[0]]["with"]
        self.assertEqual(checkout["ref"], "${{ github.sha }}")
        self.assertEqual(checkout["sparse-checkout"].strip(), ".github/workflows/pepi-evals.yml")
        self.assertIs(checkout["sparse-checkout-cone-mode"], False)
        check = step_index(steps, lambda step: runs(step, "executing/.github/workflows/pepi-evals.yml"))
        uses_harness = step_index(steps, lambda step: runs(step, "evals.swebench.ci") or runs(step, "presets"))
        self.assertEqual(len(check), 1)
        self.assertLess(sparse[0], check[0])
        self.assertLess(check[0], min(uses_harness))

    def test_offline_ci_smoke_gates_the_expensive_eval_jobs(self):
        smoke_steps = [(job, index, step) for job, index, step in all_steps(self.workflow)
                       if runs(step, "python -m unittest")]
        self.assertEqual(len(smoke_steps), 1)
        job, smoke_index, smoke = smoke_steps[0]
        self.assertEqual(job, "lookup")
        lookup = self.jobs[job]
        self.assertEqual(lookup["runs-on"], "ubuntu-latest")
        self.assertNotIn("environment", lookup)
        self.assertNotIn("secrets.", json.dumps(lookup))
        for scope in (lookup, smoke):
            self.assertNotIn("continue-on-error", scope)
        self.assertNotIn("if", smoke)
        self.assertEqual(smoke["working-directory"], "harness")
        command = smoke["run"].replace("\\\n", " ").splitlines()
        self.assertEqual(command[0], "set -euo pipefail")
        self.assertEqual(" ".join(" ".join(command[1:]).split()),
                         "PYTHONDONTWRITEBYTECODE=1 python -m unittest "
                         "evals.swebench.tests.test_ci_workflow evals.swebench.tests.test_ci_helpers "
                         "evals.swebench.tests.test_ci evals.swebench.tests.test_reporting")

        steps = lookup["steps"]
        python_setup = step_index(steps, lambda step: uses(step, "actions/setup-python"))
        self.assertEqual(len(python_setup), 1)
        self.assertEqual(steps[python_setup[0]]["with"]["python-version"], "${{ env.PEPI_CI_PYTHON_VERSION }}")
        parser_setup = step_index(steps, lambda step: runs(step, "pip install"))
        self.assertEqual(len(parser_setup), 1)
        parser = steps[parser_setup[0]]
        self.assertEqual(parser["working-directory"], "harness")
        self.assertIn("python -c 'import yaml' 2>/dev/null || python -m pip install", parser["run"])
        self.assertIn("PyYAML==6.0.2", parser["run"])
        self.assertNotIn("if", parser)
        self.assertNotIn("continue-on-error", parser)
        self.assertLess(python_setup[0], parser_setup[0])
        self.assertLess(parser_setup[0], smoke_index)
        harness_checkout = step_index(steps, lambda step: uses(step, "actions/checkout")
                                      and step["with"].get("path") == "harness")
        workflow_check = step_index(steps, lambda step: runs(step, "executing/.github/workflows/pepi-evals.yml"))
        self.assertEqual(len(harness_checkout), 1)
        self.assertEqual(len(workflow_check), 1)
        self.assertLess(harness_checkout[0], smoke_index)
        self.assertLess(workflow_check[0], smoke_index)
        # Fail before profile/config work too, not just before Docker builds in dependent jobs.
        config = step_index(steps, lambda step: runs(step, "ci assemble-config"))
        self.assertEqual(len(config), 1)
        self.assertLess(smoke_index, config[0])
        for name in ("baseline", "candidate"):
            with self.subTest(job=name):
                heavy = self.jobs[name]
                self.assertEqual(heavy["needs"], "lookup")
                self.assertNotIn("always()", heavy["if"])
                self.assertNotIn("!cancelled()", heavy["if"])
                self.assertTrue(any(runs(step, "run-evals.sh") for step in heavy["steps"]))

    def test_disk_scripts_run_only_on_the_runner_kind_they_are_for(self):
        for job, _, step in all_steps(self.workflow):
            if runs(step, "free-disk.sh"):
                self.assertEqual(step["if"], "runner.environment == 'github-hosted'", job)
            if runs(step, "prune-images.sh"):
                self.assertEqual(step["if"], "always() && runner.environment == 'self-hosted'", job)

    def test_baseline_and_candidate_use_their_own_configs_and_labels(self):
        baseline = "\n".join(step.get("run") or "" for step in self.jobs["baseline"]["steps"])
        candidate = "\n".join(step.get("run") or "" for step in self.jobs["candidate"]["steps"])
        self.assertIn('--repo "$GITHUB_WORKSPACE/config-src"', baseline)
        self.assertIn('run-evals.sh baseline "$RUNNER_TEMP/pepi-config" main', baseline)
        self.assertIn("EXPECTED_SHA256", baseline)
        self.assertIn('--repo "$GITHUB_WORKSPACE/harness"', candidate)
        self.assertIn('run-evals.sh candidate "$RUNNER_TEMP/pepi-config" "$CONFIG_LABEL"', candidate)
        self.assertIn('echo "label=pr-$PR_NUMBER"', candidate)
        for job in ("baseline", "candidate", "report"):
            refs = [step["with"]["ref"] for step in self.jobs[job]["steps"]
                    if uses(step, "actions/checkout") and step["with"].get("path") == "harness"]
            self.assertEqual(refs, ["${{ needs.lookup.outputs.harness_sha }}"], job)

    def test_referenced_outputs_exist(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        for job in ("lookup", "baseline", "candidate"):
            declared = set(self.jobs[job].get("outputs", {}))
            for name in set(re.findall(r"needs\." + job + r"\.outputs\.([a-z0-9_]+)", text)):
                self.assertIn(name, declared, f"needs.{job}.outputs.{name}")
        for name, job in self.jobs.items():
            ids = {step.get("id") for step in job["steps"]}
            blob = json.dumps(job)
            for step_id in set(re.findall(r"steps\.([a-z0-9_]+)\.outputs", blob)):
                self.assertIn(step_id, ids, f"{name}: steps.{step_id}")


if __name__ == "__main__":
    unittest.main()
