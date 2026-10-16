"""./.env loading in evals/pepi-eval.sh and `run --pass-env` forwarding. Fake Docker/venv only."""
import json
import os
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.swebench import __main__ as cli
from evals.swebench.common import read_json, write_json
from evals.swebench.runner import PI_COMMAND, RESERVED_ENV_NAMES, Secrets, forwardable_env_name, run_one, run_tasks
from evals.swebench.tests.test_ci import ENV, RecordingDocker
from evals.swebench.tests.test_cli_script import ScriptHarness, write_executable
from evals.swebench.tests.test_runner import TASK, FakeDocker, prepare_fixture

TOKEN = "DOTENV-SECRET-0123456789"


class PassEnvRunnerTests(unittest.TestCase):
    def run_fixture(self, base):
        run = prepare_fixture(base)
        write_json(run / "builds.json", {TASK["instance_id"]: {"image_id": "sha256:frozen"}})
        return run

    def test_pass_env_forwards_names_only_and_redacts_values(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            docker = RecordingDocker(patch_bytes=f"+token = '{TOKEN}'\n".encode())
            original = docker.call
            def call(*args, **kwargs):
                if args[0] == "cp" and args[1].endswith("/output/."):
                    target = Path(args[2]); target.mkdir(exist_ok=True)
                    (target / "events.jsonl").write_text(json.dumps({"env": TOKEN}))
                return original(*args, **kwargs)
            docker.call = call
            secrets = Secrets()
            with patch.dict(os.environ, {**ENV, "GITHUB_TOKEN": TOKEN, "http_proxy": "http://proxy:3128"}):
                self.assertEqual(secrets.forward_environment(["GITHUB_TOKEN", "http_proxy"]), [])
                row = run_one(docker, TASK, {"image_id": "sha256:frozen"}, base / "attempt", 1,
                              "eval", "2", "4g", secrets, "run-1")
            agent = next(c for c in docker.commands if any(a.startswith(PI_COMMAND) for a in c))
            pairs = [agent[i + 1] for i, part in enumerate(agent) if part == "--env"]
            self.assertIn("GITHUB_TOKEN", pairs)
            self.assertIn("http_proxy", pairs)
            self.assertFalse(any(TOKEN in part for c in docker.commands for part in c))
            self.assertNotIn(TOKEN, row["patch"])
            self.assertGreaterEqual(row["secret_redactions"], 2)
            for path in (base / "attempt").rglob("*"):
                if path.is_file():
                    self.assertNotIn(TOKEN.encode(), path.read_bytes(), path)

    def test_invalid_reserved_duplicate_and_missing_names_are_refused(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": TOKEN}):
            for names, message in ((["PATH"], "reserved"), (["LD_PRELOAD"], "reserved"),
                                   (["DOCKER_HOST"], "reserved"), (["PYTHONPATH"], "reserved"),
                                   (["PEPI_EVAL_IDS"], "reserved"), (["LITELLM_API_KEY"], "reserved"),
                                   (["NODE_OPTIONS"], "reserved"), (["BAD-NAME"], "invalid"),
                                   (["X=1"], "invalid"), (["GITHUB_TOKEN", "GITHUB_TOKEN"], "more than once"),
                                   (["ABSENT_VARIABLE_FOR_TEST"], "not set")):
                with self.subTest(names=names), self.assertRaisesRegex(ValueError, message):
                    Secrets().forward_environment(names)

    def test_short_values_are_forwarded_but_reported_unredacted(self):
        with patch.dict(os.environ, {"SHORT": "abc", "EMPTY": ""}):
            secrets = Secrets()
            self.assertEqual(secrets.forward_environment(["SHORT", "EMPTY"]), ["SHORT"])
            self.assertEqual(secrets.forwarded, ["SHORT", "EMPTY"])
            self.assertNotIn(b"abc", secrets.values)

    def test_execution_records_sorted_names_only_and_omits_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.run_fixture(Path(directory))
            with patch.dict(os.environ, {**ENV, "ZED": TOKEN, "ALPHA": TOKEN + "-2"}), \
                 patch("evals.swebench.runner.Docker", return_value=FakeDocker()):
                self.assertEqual(run_tasks(run, 1, "eval", "2", "4g", None, ["ZED", "ALPHA"]), 0)
            execution = (run / "execution.json").read_text()
            self.assertEqual(json.loads(execution)["forwarded_env_names"], ["ALPHA", "ZED"])
            self.assertNotIn(TOKEN, execution)
        with tempfile.TemporaryDirectory() as directory:
            run = self.run_fixture(Path(directory))
            with patch.dict(os.environ, ENV), patch("evals.swebench.runner.Docker", return_value=FakeDocker()):
                self.assertEqual(run_tasks(run, 1, "eval", "2", "4g"), 0)
            # Unchanged protocol for runs without forwarding keeps them comparable with earlier runs.
            self.assertNotIn("forwarded_env_names", read_json(run / "execution.json"))

    def test_json_escaped_and_multiline_values_are_redacted(self):
        value = 'first "quoted" line \\ end\nsecond-line-secret-xyz\n'
        lines = [line.encode() for line in value.splitlines()]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            docker = RecordingDocker(patch_bytes=b"".join(b"+" + line + b"\n" for line in lines))
            original = docker.call
            def call(*args, **kwargs):
                if args[0] == "cp" and args[1].endswith("/output/."):
                    target = Path(args[2]); target.mkdir(exist_ok=True)
                    (target / "events.jsonl").write_text(json.dumps({"env": value}) + "\n")
                    (target / "session.jsonl").write_text(json.dumps({"env": value}, ensure_ascii=False))
                return original(*args, **kwargs)
            docker.call = call
            secrets = Secrets()
            with patch.dict(os.environ, {**ENV, "SERVICE_ACCOUNT": value}):
                secrets.forward_environment(["SERVICE_ACCOUNT"])
                row = run_one(docker, TASK, {"image_id": "sha256:frozen"}, base / "attempt", 1,
                              "eval", "2", "4g", secrets, "run-1")
            escaped = json.dumps(value)[1:-1].encode()
            forms = [value.encode(), escaped, *lines, *(json.dumps(l.decode())[1:-1].encode() for l in lines)]
            for form in forms:
                self.assertNotIn(form.decode(), row["patch"])
            artifacts = [p for p in (base / "attempt").rglob("*") if p.is_file()]
            self.assertTrue(any(p.name == "events.jsonl" for p in artifacts))
            for path in artifacts:
                data = path.read_bytes()
                for form in forms:
                    self.assertNotIn(form, data, path)

    def test_non_utf8_values_are_forwarded_and_redacted(self):
        with patch.dict(os.environ, {"BINARY": "abc\udcffdefghij"}):
            secrets = Secrets()
            self.assertEqual(secrets.forward_environment(["BINARY"]), [])
        self.assertIn(b"abc\xffdefghij", secrets.values)
        self.assertEqual(secrets.redact(b"x abc\xffdefghij y"), (b"x <redacted> y", 1))

    def test_cli_wiring_and_exit_code_for_missing_variable(self):
        args = cli.build_parser().parse_args(["run", "r", "--network", "n", "--pass-env", "A",
                                              "--pass-env", "B"])
        self.assertEqual(args.pass_env, ["A", "B"])
        with tempfile.TemporaryDirectory() as directory:
            run = self.run_fixture(Path(directory))
            with patch.dict(os.environ, ENV), patch("evals.swebench.runner.Docker", return_value=FakeDocker()):
                self.assertEqual(cli.main(["run", str(run), "--network", "eval",
                                           "--pass-env", "ABSENT_VARIABLE_FOR_TEST"]), 2)
                self.assertEqual(cli.main(["run", str(run), "--network", "eval", "--pass-env", "HOME"]), 2)
            self.assertFalse((run / "attempts").exists())


class DotenvScriptTests(ScriptHarness):
    def write_dotenv(self, text, directory=None, mode=0o600):
        path = (directory or self.launch) / ".env"
        path.write_text(text)
        path.chmod(mode)
        return path

    def run_env(self, extra=None):
        """A fresh prepared run plus shell settings; `None` in `extra` unsets a default."""
        self.dump = Path(self.fake.name) / "env.json"
        self.log.unlink(missing_ok=True)
        root = Path(self.runs_tmpdir.name)
        for name in ("run-1", "pepi"):
            shutil.rmtree(root / name, ignore_errors=True)
        run = prepare_fixture(root)
        env = {"PEPI_EVAL_RUN_NAME": run.name, "PEPI_EVAL_USE_HOST_AUTH": "0",
               "LITELLM_BASE_URL": "https://gateway.example", "LITELLM_API_KEY": "SHELL-KEY-0123456789",
               "FAKE_ENV_DUMP": str(self.dump), **(extra or {})}
        return {key: value for key, value in env.items() if value is not None}

    def runner_call(self):
        calls = [line for line in self.log.read_text().splitlines() if " evals.swebench run " in line]
        self.assertEqual(len(calls), 1)
        return calls[0]

    def passed(self, call):
        parts = call.split()
        return [parts[i + 1] for i, part in enumerate(parts) if part == "--pass-env"]

    def tty(self, args, env, answers):
        """Run on a pty, answering each prompt substring once; return (status, output)."""
        master, slave = pty.openpty()
        process = subprocess.Popen(["bash", str(self.script_path), *args], stdin=slave, stdout=slave,
                                   stderr=slave, cwd=self.launch, env=self.script_env(env))
        os.close(slave)
        output, pending = b"", dict(answers)
        try:
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                    for prompt in [p for p in pending if p.encode() in output]:
                        os.write(master, pending.pop(prompt).encode() + b"\n")
                if process.poll() is not None:
                    break
            process.wait(timeout=3)
            return process.returncode, output.decode()
        finally:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=3)
            os.close(master)

    def test_parser_values_override_shell_and_reach_runner_by_name(self):
        self.write_dotenv(
            "# comment\n\n"
            "export GITHUB_TOKEN=" + TOKEN + "\n"
            "SINGLE='lit $HOME `x` \"q\"'  # trailing\n"
            'DOUBLE="a \\"b\\" \\\\ c\\nd $(echo hi)"\n'
            "UNQUOTED=  hello world   # comment\n"
            "HASH=a#b\n"
            "EMPTY=\n"
            "OVERRIDE=from-dotenv\n"
            "OVERRIDE=last-wins\n")
        result = self.run_script(["-y", "run"], self.run_env({"PEPI_EVAL_USE_DOTENV": "1",
                                                             "OVERRIDE": "from-shell"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        env = json.loads(self.dump.read_text())
        self.assertEqual(env["GITHUB_TOKEN"], TOKEN)
        self.assertEqual(env["SINGLE"], 'lit $HOME `x` "q"')
        self.assertEqual(env["DOUBLE"], 'a "b" \\ c\nd $(echo hi)')
        self.assertEqual(env["UNQUOTED"], "hello world")
        self.assertEqual(env["HASH"], "a#b")
        self.assertEqual(env["EMPTY"], "")
        self.assertEqual(env["OVERRIDE"], "last-wins")
        call = self.runner_call()
        self.assertEqual(self.passed(call),
                         ["GITHUB_TOKEN", "SINGLE", "DOUBLE", "UNQUOTED", "HASH", "EMPTY", "OVERRIDE"])
        self.assertNotIn(TOKEN, call + result.stdout + result.stderr)
        self.assertIn(f"loaded 7 variable(s) from {self.launch.resolve()}/.env", result.stderr)

    def test_bad_lines_fail_with_line_number_only(self):
        for text, line in (("A=1\nthis is LEAKY-CONTENT\n", 2), ("A='LEAKY-CONTENT\n", 1),
                           ('A="LEAKY-CONTENT" trailing\n', 1), ("NAME = LEAKY-CONTENT\n", 1),
                           ("A=1\nB=LEAKY-CONTENT\r\n", 2), ("B=LEAKY\x00CONTENT\n", 1)):
            with self.subTest(text=text):
                self.write_dotenv(text)
                result = self.run_script(["--dry-run", "check"])
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(f".env line {line}:", result.stderr)
                self.assertNotIn("LEAKY", result.stdout + result.stderr)
                self.assertFalse(self.log.exists())

    def test_unsafe_files_are_refused_and_readable_files_warned(self):
        target = Path(self.fake.name) / "real.env"
        target.write_text("A=1\n"); target.chmod(0o600)
        (self.launch / ".env").symlink_to(target)
        result = self.run_script(["--dry-run", "check"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("symlink", result.stderr)
        (self.launch / ".env").unlink()
        for mode in (0o620, 0o602):
            with self.subTest(mode=oct(mode)):
                self.write_dotenv("A=1\n", mode=mode)
                result = self.run_script(["--dry-run", "check"])
                self.assertEqual(result.returncode, 2)
                self.assertIn("chmod 600", result.stderr)
        self.write_dotenv("A=1\n", mode=0o644)
        result = self.run_script(["--dry-run", "check"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("readable by group/others", result.stderr)
        (self.launch / ".env").unlink()
        (self.launch / ".env").mkdir()
        self.assertEqual(self.run_script(["--dry-run", "check"]).returncode, 2)

    def test_reserved_names_are_not_loaded_and_script_state_is_protected(self):
        self.write_dotenv("PATH=/evil\nHOME=/evil\nLD_PRELOAD=/evil.so\nDYLD_INSERT_LIBRARIES=x\n"
                          "DOCKER_HOST=tcp://evil\nPYTHONPATH=/evil\nNODE_OPTIONS=--require=/evil\n"
                          "BASH_ENV=/evil\nCDPATH=/evil\nGLOBIGNORE=*\nPOSIXLY_CORRECT=1\n"
                          "script_dir=/evil\nruns_dir=/evil\n_de_index=9\nrun_cmd=/evil\nKEPT=yes\n")
        result = self.run_script(["-y", "run"], self.run_env({"PEPI_EVAL_USE_DOTENV": "1"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        env = json.loads(self.dump.read_text())
        shell = self.script_env()
        self.assertEqual((env["PATH"], env["HOME"]), (shell["PATH"], shell["HOME"]))
        for name in ("LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "DOCKER_HOST", "NODE_OPTIONS", "BASH_ENV",
                     "CDPATH", "GLOBIGNORE", "POSIXLY_CORRECT"):
            self.assertNotIn(name, env)
            self.assertIn(f"Ignoring reserved {name}", result.stderr)
        self.assertNotIn("/evil", env["PYTHONPATH"])
        self.assertIn("Ignoring shell/script variable name(s) from", result.stderr)
        self.assertIn("script_dir runs_dir _de_index", result.stderr)
        self.assertNotIn("_de_index", env)
        # A forwarded name that shadows the script's command array cannot redirect the run.
        self.assertEqual(env["run_cmd"], "/evil")
        self.assertEqual(self.passed(self.runner_call()), ["run_cmd", "KEPT"])
        self.assertIn(str(Path(self.runs_tmpdir.name)), self.runner_call())

    def test_script_settings_load_but_are_never_forwarded(self):
        self.write_dotenv("PEPI_EVAL_CPUS=3\nLITELLM_BASE_URL=https://dotenv.example\n"
                          "LITELLM_API_KEY=DOTENV-KEY-0123456789\nLITELLM_EXTRA=x\nKEPT=yes\n")
        result = self.run_script(["-y", "run"], self.run_env({"PEPI_EVAL_USE_DOTENV": "1"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        call = self.runner_call()
        self.assertIn("--cpus 3", call)
        self.assertIn("Ignoring LITELLM_EXTRA", result.stderr)
        self.assertIn("script settings: PEPI_EVAL_CPUS LITELLM_BASE_URL LITELLM_API_KEY;", result.stderr)
        self.assertEqual(self.passed(call), ["KEPT"])
        env = json.loads(self.dump.read_text())
        self.assertEqual((env["LITELLM_BASE_URL"], env["LITELLM_API_KEY"]),
                         ("https://dotenv.example", "DOTENV-KEY-0123456789"))
        self.assertNotIn("LITELLM_EXTRA", env)
        self.assertIn("would be sent", result.stderr)  # a differing gateway is always consented to, even with a .env key

    def test_container_candidates_never_reach_host_steps(self):
        git_hook = "touch /tmp/pepi-dotenv-should-never-run-" + TOKEN
        self.write_dotenv("GIT_CONFIG_COUNT=1\nGIT_CONFIG_KEY_0=core.fsmonitor\n"
                          f"GIT_CONFIG_VALUE_0='{git_hook}'\nKEPT={TOKEN}\n")
        candidates = ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "KEPT")
        env_log = Path(self.fake.name) / "env.jsonl"
        for repo in ("SWE-bench", "swe-bench-tasks"):
            (self.home / repo).mkdir(exist_ok=True)

        def calls(extra, args):
            env_log.unlink(missing_ok=True)
            result = self.run_script(args, self.run_env({"FAKE_ENV_LOG": str(env_log), **extra}))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(TOKEN, self.log.read_text() + result.stdout + result.stderr)  # never argv
            return {" ".join(c["argv"][1:3]): c["env"] for c in map(json.loads, env_log.read_text().splitlines())}

        # Every step of `all` (host git, pip, docker build, grading in a real run) sees none of
        # them, even with consent; only the runner gets them.
        steps = calls({"PEPI_EVAL_USE_DOTENV": "1", "PEPI_EVAL_RUN_NAME": "fresh",
                       "FAKE_PREPARE_FIXTURE": str(Path(self.runs_tmpdir.name) / "run-1")}, ["-y", "all"])
        self.assertEqual(sorted(steps), sorted([
            "evals.swebench prepare", "evals.swebench build", "evals.swebench run",
            "evals.swebench.telemetry " + str(Path(self.runs_tmpdir.name) / "fresh"),
            "evals.swebench.reporting grade", "evals.swebench.reporting summarize"]))
        for step, env in steps.items():
            for name in candidates:
                self.assertEqual(name in env, step == "evals.swebench run", (step, name))
        self.assertEqual(steps["evals.swebench run"]["GIT_CONFIG_VALUE_0"], git_hook)
        # Without consent the runner does not get them either.
        run = calls({}, ["-y", "run"])["evals.swebench run"]
        for name in candidates:
            self.assertNotIn(name, run)

    def test_shell_only_settings_in_dotenv_are_ignored(self):
        evil = Path(self.fake.name) / "evil-venv"
        write_executable(evil / "bin/python", '#!/bin/sh\n: > "$FAKE_STATE/evil-python"\nexit 0\n')
        (evil / "pyvenv.cfg").write_text("home = evil\n")
        secrets = Path(self.fake.name) / "dotenv-secrets"
        secrets.mkdir(mode=0o700)
        (secrets / "auth.json").write_text("{}")
        self.write_dotenv(f"PEPI_EVAL_VENV={evil}\nPEPI_EVAL_SWEBENCH_REPO=/evil\n"
                          f"PEPI_EVAL_SECRETS_DIR={secrets}\nPEPI_EVAL_NETWORK=dotenv-net\nKEPT=yes\n")
        result = self.run_script(["-y", "run"], self.run_env({"PEPI_EVAL_USE_DOTENV": "1"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        warning = next(l for l in result.stderr.splitlines() if "Ignoring shell-only setting(s)" in l)
        for name in ("PEPI_EVAL_VENV", "PEPI_EVAL_SWEBENCH_REPO", "PEPI_EVAL_SECRETS_DIR", "PEPI_EVAL_NETWORK"):
            self.assertIn(name, warning)
        self.assertFalse((Path(self.fake.name) / "state/evil-python").exists())
        call = self.runner_call()
        self.assertIn("--network pepi-eval", call)
        self.assertNotIn("--secrets-dir", call)
        self.assertEqual(self.passed(call), ["KEPT"])

    def test_consent_knobs_in_dotenv_are_ignored(self):
        self.write_dotenv("PEPI_EVAL_YES=1\nPEPI_EVAL_USE_DOTENV=1\nPEPI_EVAL_USE_HOST_AUTH=1\n"
                          "PEPI_EVAL_AUTO_FIX=1\nKEPT=yes\n")
        result = self.run_script(["run"], self.run_env({"PEPI_EVAL_USE_HOST_AUTH": None}))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Ignoring shell-only setting(s)", result.stderr)
        for name in ("PEPI_EVAL_YES", "PEPI_EVAL_USE_DOTENV", "PEPI_EVAL_USE_HOST_AUTH", "PEPI_EVAL_AUTO_FIX"):
            self.assertIn(name, result.stderr)
        self.assertIn("not forwarded without consent", result.stderr)
        self.assertIn("not confirmed", result.stderr)
        self.assertNotIn("Use " + str(self.home), result.stderr)
        self.assertFalse(self.log.exists() and " evals.swebench run " in self.log.read_text())

    def test_non_tty_consent_matrix(self):
        self.write_dotenv("KEPT=yes\n")
        for extra, forwarded in (({}, []), ({"PEPI_EVAL_YES": "1"}, []),
                                 ({"PEPI_EVAL_USE_DOTENV": "0"}, []), ({"PEPI_EVAL_USE_DOTENV": "1"}, ["KEPT"])):
            with self.subTest(extra=extra):
                result = self.run_script(["-y", "run"], self.run_env(extra))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.passed(self.runner_call()), forwarded)
                if not extra or "PEPI_EVAL_YES" in extra:
                    self.assertIn("set PEPI_EVAL_USE_DOTENV=1 in the shell", result.stderr)
        result = self.run_script(["-y", "run"], self.run_env({"PEPI_EVAL_USE_DOTENV": "yes"}))
        self.assertEqual(result.returncode, 2)
        self.assertIn("PEPI_EVAL_USE_DOTENV must be 0 or 1", result.stderr)

    def test_tty_consent_with_yes_still_asks(self):
        self.write_dotenv("KEPT=" + TOKEN + "\n")
        for answer, forwarded in (("y", ["KEPT"]), ("n", []), ("", [])):
            with self.subTest(answer=answer):
                status, output = self.tty(["-y", "run"], self.run_env(),
                                          {"Forward these .env variables into the eval container? [y/N]": answer})
                self.assertEqual(status, 0, output)
                self.assertIn("(names only): KEPT", output)
                self.assertEqual(self.passed(self.runner_call()), forwarded)
                self.assertNotIn(TOKEN, output)

    def test_dry_run_shows_names_never_values_or_prompts(self):
        self.write_dotenv("KEPT=" + TOKEN + "\n")
        result = self.run_script(["--dry-run", "-y", "run"], {
            "LITELLM_BASE_URL": "https://x", "LITELLM_API_KEY": "sk-dummy-0123456789"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--pass-env KEPT", result.stdout)
        self.assertIn("a real run asks before forwarding", result.stderr)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertNotIn("[y/N]", result.stderr)

    def test_dotenv_is_read_from_the_launch_directory_only(self):
        repo = Path(self.fake.name) / "repo"
        (repo / "evals").mkdir(parents=True)
        shutil.copy(self.script_path, repo / "evals/pepi-eval.sh")
        for directory in (repo, repo / "evals"):
            self.write_dotenv("FROM_CHECKOUT=1\n", directory)
        env = self.script_env()
        result = subprocess.run(["bash", str(repo / "evals/pepi-eval.sh"), "--dry-run", "check"], env=env,
                                cwd=self.launch, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("FROM_CHECKOUT", result.stderr)
        result = subprocess.run(["bash", str(repo / "evals/pepi-eval.sh"), "--dry-run", "check"], env=env,
                                cwd=repo, capture_output=True, text=True, timeout=5)
        self.assertIn(f"loaded 1 variable(s) from {repo.resolve()}/.env: script settings: (none); "
                      "container candidates", result.stderr)
        self.assertIn("never set on the host): FROM_CHECKOUT", result.stderr)

    def test_dry_run_flag_beats_dotenv(self):
        self.write_dotenv("DRY_RUN=0\n")
        result = self.run_script(["--dry-run", "-y", "run"], {
            "LITELLM_BASE_URL": "https://x", "LITELLM_API_KEY": "sk-dummy-0123456789"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("+ ", result.stdout)
        self.assertFalse(self.log.exists())

    def test_dotenv_gateway_without_key_needs_consent_for_the_shell_key(self):
        self.write_dotenv("LITELLM_BASE_URL=https://user:pw@other.example/v1\n")
        result = self.run_script(["-y", "run"], self.run_env())
        self.assertEqual(result.returncode, 2)
        self.assertIn("would be sent to that gateway", result.stderr)
        self.assertIn("host other.example", result.stderr)
        self.assertNotIn("pw@", result.stderr)
        self.assertFalse(self.log.exists())
        for value, status in (("0", 2), ("1", 0)):
            with self.subTest(value=value):
                result = self.run_script(["-y", "run"], self.run_env({"PEPI_EVAL_USE_DOTENV": value}))
                self.assertEqual(result.returncode, status, result.stderr)
        for answer, status in (("n", 2), ("y", 0)):
            with self.subTest(answer=answer):
                status_seen, output = self.tty(["-y", "run"], self.run_env(),
                                               {"Send your LiteLLM key (shell or auth.json) to this gateway? [y/N]": answer})
                self.assertEqual(status_seen, status, output)
        # Same URL as the shell: nothing is redirected.
        self.write_dotenv("LITELLM_BASE_URL=https://gateway.example\n")
        result = self.run_script(["-y", "run"], self.run_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("would be sent", result.stderr)

    def test_dotenv_gateway_guard_covers_auth_json_and_prompted_keys(self):
        secrets = Path(self.fake.name) / "secrets"
        secrets.mkdir(mode=0o700)
        (secrets / "auth.json").write_text(json.dumps({"litellm": {"type": "api_key", "key": "AUTH-KEY-0123"}}))
        (secrets / "auth.json").chmod(0o600)
        self.write_dotenv("LITELLM_BASE_URL=https://other.example\n")
        no_shell_key = {"LITELLM_BASE_URL": None, "LITELLM_API_KEY": None}
        for extra in ({"PEPI_EVAL_SECRETS_DIR": str(secrets)}, {}):  # auth.json key, or a prompted key
            with self.subTest(extra=extra):
                result = self.run_script(["-y", "run"], self.run_env({**no_shell_key, **extra}))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("your LiteLLM key (shell, .env or auth.json) would be sent to that gateway",
                              result.stderr)
                self.assertFalse(self.log.exists())
        result = self.run_script(["-y", "run"], self.run_env({
            **no_shell_key, "PEPI_EVAL_SECRETS_DIR": str(secrets), "PEPI_EVAL_USE_DOTENV": "1"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--secrets-dir {secrets}", self.runner_call())

    def test_dotenv_url_and_key_still_guarded_when_auth_json_key_wins(self):
        secrets = Path(self.fake.name) / "secrets"
        secrets.mkdir(mode=0o700)
        (secrets / "auth.json").write_text(json.dumps({"litellm": {"type": "api_key", "key": "AUTH-KEY-0123"}}))
        (secrets / "auth.json").chmod(0o600)
        self.write_dotenv("LITELLM_BASE_URL=https://other.example\nLITELLM_API_KEY=DOTENV-KEY-0123456789\n")
        result = self.run_script(["-y", "run"], self.run_env({
            "LITELLM_BASE_URL": None, "LITELLM_API_KEY": None, "PEPI_EVAL_SECRETS_DIR": str(secrets)}))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("would be sent to that gateway", result.stderr)
        self.assertFalse(self.log.exists())

    def test_host_auth_wrapper_keeps_pass_env_and_environment(self):
        from evals.swebench.tests.test_cli_script import PepiEvalTest
        _, capture, env = PepiEvalTest.host_auth_fixture(self)
        self.write_dotenv("KEPT=" + TOKEN + "\n")
        result = self.run_script(["-y", "run"], {**env, "PEPI_EVAL_USE_HOST_AUTH": "1",
                                                "PEPI_EVAL_USE_DOTENV": "1", "FAKE_CAPTURE_ENV": "KEPT"})
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads(capture.read_text())
        self.assertEqual(captured["env"], {"KEPT": TOKEN})  # the adapter's child inherits it
        args = captured["args"]
        self.assertEqual(args[args.index("--pass-env") + 1], "KEPT")
        self.assertLess(args.index("--pass-env"), args.index("--secrets-dir"))
        self.assertNotIn(TOKEN, json.dumps(args) + result.stdout + result.stderr)

    def test_bash_forwarding_matches_python_policy(self):
        names = sorted(RESERVED_ENV_NAMES | {
            "LD_PRELOAD", "DYLD_X", "BASH_FUNC_X", "DOCKER_CONFIG", "PYTHONHOME", "PEPI_EVAL_IDS",
            "PEPI_EVAL_YES", "LITELLM_BASE_URL", "LITELLM_EXTRA", "GITHUB_TOKEN", "AWS_REGION",
            "http_proxy", "NO_PROXY", "My_Var", "_UNDERSCORE"})
        self.write_dotenv("".join(f"{name}=v\n" for name in names))
        result = self.run_script(["--dry-run", "-y", "run"], {
            "PEPI_EVAL_USE_DOTENV": "1", "LITELLM_BASE_URL": "v", "LITELLM_API_KEY": "sk-dummy-0123456789"})
        self.assertEqual(result.returncode, 0, result.stderr)
        line = next(l for l in result.stdout.splitlines() if " evals.swebench run " in l)
        self.assertEqual(sorted(self.passed(line)), sorted(n for n in names if forwardable_env_name(n)))


if __name__ == "__main__":
    unittest.main()
