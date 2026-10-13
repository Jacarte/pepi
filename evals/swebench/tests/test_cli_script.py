"""Tests for evals/pepi-eval.sh: subprocess-based core functionality."""
import json
import os
import pty
import select
import signal
import shlex
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from evals.swebench.__main__ import build_parser
from evals.swebench.preset import DEFAULT_IDS, IDS_SHA256, tasklist_hash
from evals.swebench.tests.test_runner import prepare_fixture
from evals.swebench.tests.test_profiles import repository, prepare

# Stand-in for the swebench venv's python: records `-m` invocations, exits with $FAKE_STATUS.
FAKE_PYTHON = """#!/bin/sh
[ "$1" = "-m" ] || exit 0
printf '%s\\n' "$*" >> "$FAKE_LOG"
if [ -n "$FAKE_IDS_LOG" ]; then
    while [ "$#" -gt 0 ]; do
        if [ "$1" = "--ids" ]; then cat "$2" > "$FAKE_IDS_LOG"; break; fi
        shift
    done
fi
exit "${FAKE_STATUS:-0}"
"""


class PepiEvalTest(unittest.TestCase):
    """Test evals/pepi-eval.sh command line interface."""
    
    @classmethod
    def setUpClass(cls):
        cls.script_path = Path(__file__).resolve().parents[2] / "pepi-eval.sh"
        if not cls.script_path.exists():
            raise RuntimeError(f"Script not found: {cls.script_path}")
    
    def setUp(self):
        self.runs_tmpdir = tempfile.TemporaryDirectory()
        self.fake = tempfile.TemporaryDirectory()
        fake = Path(self.fake.name)
        (fake / 'venv/bin').mkdir(parents=True)
        python = fake / 'venv/bin/python'
        python.write_text(FAKE_PYTHON)
        python.chmod(python.stat().st_mode | stat.S_IXUSR)
        (fake / 'bin').mkdir()
        docker = fake / 'bin/docker'
        docker.write_text('#!/bin/sh\nexit 0\n')
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        pi = fake / 'bin/pi'
        pi.write_text('#!/bin/sh\necho 1.1.0\n')
        pi.chmod(pi.stat().st_mode | stat.S_IXUSR)
        self.log = fake / 'calls.log'
        self.home = fake / 'home'
        self.home.mkdir()
    
    def tearDown(self):
        self.runs_tmpdir.cleanup()
        self.fake.cleanup()
    
    def script_env(self, env_extra=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PEPI_EVAL_")
               and not k.startswith("LITELLM_")}
        env['HOME'] = str(self.home)
        env['PEPI_EVAL_RUNS_DIR'] = self.runs_tmpdir.name
        env['PEPI_EVAL_VENV'] = str(Path(self.fake.name) / 'venv')
        env['PATH'] = f"{Path(self.fake.name) / 'bin'}{os.pathsep}{env['PATH']}"
        env['FAKE_LOG'] = str(self.log)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        
        if env_extra:
            env.update(env_extra)
        
        return env

    def run_script(self, args, env_extra=None, stdin=None):
        """Run the script against a synthetic HOME and fake venv/docker."""
        result = subprocess.run(
            ['bash', str(self.script_path)] + args,
            env=self.script_env(env_extra),
            capture_output=True,
            text=True,
            input=stdin,
            timeout=5
        )
        return result
    
    def test_dry_run_prints_commands_to_stdout(self):
        """Dry-run prints commands to stdout, not stderr."""
        env = {
            'LITELLM_API_KEY': 'sk-test',
            'LITELLM_BASE_URL': 'https://x',
            'PEPI_EVAL_YES': '1',
        }
        result = self.run_script(['--dry-run', '-y', 'all'], env_extra=env)
        
        stdout_lines = [l for l in result.stdout.split('\n') if l.startswith('+ ')]
        self.assertGreater(len(stdout_lines), 0, "Dry-run should print commands to stdout")
    
    def test_api_key_masked_in_dry_run(self):
        """API key masked in dry-run output when reaching run step."""
        env = {
            'LITELLM_API_KEY': 'sk-SECRET-KEY-12345',
            'LITELLM_BASE_URL': 'https://x',
            'PEPI_EVAL_YES': '1',
        }
        result = self.run_script(['--dry-run', '-y', 'run'], env_extra=env)
        
        # Even if it fails, the key should not appear
        self.assertNotIn('sk-SECRET-KEY-12345', result.stdout)
        self.assertNotIn('sk-SECRET-KEY-12345', result.stderr)
    
    def test_help_output_goes_to_stderr(self):
        """Help text goes to stderr (not captured as stdout)."""
        result = self.run_script(['--help'])
        
        self.assertIn('Usage:', result.stderr)
        self.assertIn('--dry-run', result.stderr)
        self.assertEqual(result.returncode, 0)
    
    def test_error_messages_on_stderr(self):
        """Error messages go to stderr, not stdout."""
        result = self.run_script(['unknown-command'])
        
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('error:', result.stderr)
        self.assertEqual(result.stdout.strip(), '')
    
    def test_step_headers_on_stderr(self):
        """Step headers printed to stderr (not stdout)."""
        env = {'PEPI_EVAL_YES': '1'}
        result = self.run_script(['--dry-run', '-y', 'all'], env_extra=env)
        
        self.assertIn('step', result.stderr)
        stdout_step_lines = [l for l in result.stdout.split('\n') if 'step' in l and not l.startswith('+ ')]
        for line in stdout_step_lines:
            self.assertTrue(line.startswith('+ ') or not line, f"Non-command line on stdout: {line}")
    
    def test_bash_syntax(self):
        """Script is valid bash."""
        result = subprocess.run(['bash', '-n', str(self.script_path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"bash -n failed: {result.stderr}")
    
    def test_step_count_all(self):
        """Step count for all command is 7."""
        env = {'PEPI_EVAL_YES': '1'}
        result = self.run_script(['--dry-run', '-y', 'all'], env_extra=env)
        
        step_headers = [l for l in result.stderr.split('\n') if ' step ' in l and '/' in l]
        self.assertTrue(any('/7' in h for h in step_headers), f"Expected step X/7 in: {step_headers}")
    
    def test_step_count_report(self):
        """Step count for report command is 3."""
        run_name = 'test-report'
        run_dir = Path(self.runs_tmpdir.name) / run_name
        run_dir.mkdir()
        (run_dir / 'manifest.json').write_text('{"tasks": []}')
        (run_dir / 'predictions.jsonl').write_text('{}\n')
        
        env = {
            'PEPI_EVAL_YES': '1',
            'PEPI_EVAL_RUN_NAME': run_name,
        }
        result = self.run_script(['--dry-run', '-y', 'report'], env_extra=env)
        
        self.assertIn('/3', result.stderr)
        self.assertIn('step 1/3', result.stderr)
        self.assertIn('step 3/3', result.stderr)
    
    def test_existing_run_dir_check_before_side_effects(self):
        """Existing run dir exits 2 before creating anything."""
        run_name = 'test-dup'
        run_dir = Path(self.runs_tmpdir.name) / run_name
        run_dir.mkdir()
        
        env = {
            'PEPI_EVAL_YES': '1',
            'PEPI_EVAL_RUN_NAME': run_name,
        }
        result = self.run_script(['--dry-run', '-y', 'prepare'], env_extra=env, stdin='')
        
        self.assertIn('already exists', result.stderr)
    
    def test_help_lists_secret_env_vars(self):
        """Help text documents secret env vars correctly."""
        result = self.run_script(['--help'])
        
        self.assertIn('LITELLM_BASE_URL', result.stderr)
        self.assertIn('LITELLM_API_KEY', result.stderr)
        self.assertIn('secret', result.stderr.lower())
    
    def test_dry_run_all_no_run_dir_created(self):
        """Dry-run all does not create run directories."""
        env = {
            'LITELLM_API_KEY': 'sk-test',
            'LITELLM_BASE_URL': 'https://x',
            'PEPI_EVAL_YES': '1',
        }
        result = self.run_script(['--dry-run', '-y', 'all'], env_extra=env)
        
        # Should not have created any run directories
        self.assertEqual(len(os.listdir(self.runs_tmpdir.name)), 0)
    
    def test_dry_run_all_exits_zero(self):
        """Dry-run all exits 0 and prints 6+ commands to stdout."""
        env = {
            'LITELLM_API_KEY': 'sk-SECRET-KEY',
            'LITELLM_BASE_URL': 'https://gateway.example',
            'PEPI_EVAL_YES': '1',
        }
        result = self.run_script(['--dry-run', '-y', 'all'], env_extra=env)
        
        self.assertEqual(result.returncode, 0, f"Expected exit 0, got {result.returncode}. stderr: {result.stderr}")
        
        # Count commands (lines starting with '+ ')
        stdout_lines = [l for l in result.stdout.split('\n') if l.startswith('+ ')]
        self.assertGreaterEqual(len(stdout_lines), 6, f"Expected at least 6 commands, got {len(stdout_lines)}")
        
        # API key should not leak in output
        self.assertNotIn('sk-SECRET-KEY', result.stdout)
        self.assertNotIn('sk-SECRET-KEY', result.stderr)
    
    def test_non_tty_run_without_litellm_base_url_exits_2_naming_var(self):
        """Non-TTY run without LITELLM_BASE_URL exits 2 and names the var."""
        run = prepare_fixture(Path(self.runs_tmpdir.name))
        result = self.run_script(['run'], env_extra={'PEPI_EVAL_RUN_NAME': run.name}, stdin='')
        
        self.assertEqual(result.returncode, 2)
        self.assertIn('LITELLM_BASE_URL', result.stderr)
        self.assertIn('error:', result.stderr)
    
    def test_non_tty_run_with_base_url_without_api_key_exits_2(self):
        """Non-TTY run with LITELLM_BASE_URL but no key exits 2 naming LITELLM_API_KEY."""
        run = prepare_fixture(Path(self.runs_tmpdir.name))
        env = {'LITELLM_BASE_URL': 'https://gateway.example', 'PEPI_EVAL_RUN_NAME': run.name}
        result = self.run_script(['run'], env_extra=env, stdin='')
        
        self.assertEqual(result.returncode, 2)
        self.assertIn('LITELLM_API_KEY', result.stderr)
        self.assertIn('error:', result.stderr)
    
    def test_non_tty_build_without_pepi_eval_run_name_exits_2(self):
        """Non-TTY build without PEPI_EVAL_RUN_NAME exits 2 naming PEPI_EVAL_RUN_NAME."""
        result = self.run_script(['build'], env_extra={}, stdin='')
        
        self.assertEqual(result.returncode, 2)
        self.assertIn('PEPI_EVAL_RUN_NAME', result.stderr)
        self.assertIn('error:', result.stderr)
    
    def test_dry_run_check_does_not_run_setup(self):
        """Dry-run check does not call setup_venv or check repos."""
        # This test verifies check doesn't have side effects in dry-run
        # by checking it doesn't fail even if venv is missing
        env = {'PEPI_EVAL_VENV': '/nonexistent'}
        result = self.run_script(['--dry-run', '-y', 'check'], env_extra=env)
        
        # In dry-run, should not fail due to missing venv
        self.assertEqual(result.returncode, 0)
        self.assertIn('✓', result.stderr)  # checkmark printed


    # --- integration with the runner's real CLI (flags, exit codes, secrets) ---

    def printed_commands(self, result):
        """Commands printed by --dry-run, as argv lists after `python -m evals.swebench`."""
        commands = []
        for line in result.stdout.splitlines():
            if line.startswith('+ '):
                argv = shlex.split(line[2:])
                if argv[1:3] == ['-m', 'evals.swebench']:
                    commands.append(argv[3:])
        return commands

    def test_dry_run_commands_parse_with_the_real_runner_cli(self):
        """Every runner command the script prints is accepted by evals.swebench's argparse."""
        config = Path(self.fake.name) / 'config'
        config.mkdir()
        secrets = Path(self.fake.name) / 'secrets'
        secrets.mkdir()
        env = {
            'LITELLM_API_KEY': 'sk-SECRET-KEY-12345', 'LITELLM_BASE_URL': 'https://x',
            'PEPI_EVAL_RUN_NAME': 'flags-run', 'PEPI_EVAL_CONFIG_DIR': str(config),
            'PEPI_EVAL_CONFIG_LABEL': 'variant-b', 'PEPI_EVAL_DROP_MCP': '1',
            'PEPI_EVAL_SECRETS_DIR': str(secrets), 'PEPI_EVAL_IDS': 'a__b-1,c__d-2',
        }
        result = self.run_script(['--dry-run', '-y', 'all'], env_extra=env, stdin='')
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = {argv[0]: build_parser().parse_args(argv) for argv in self.printed_commands(result)}
        self.assertEqual(set(parsed), {'prepare', 'build', 'run'})
        self.assertEqual(parsed['prepare'].config_dir, config)
        self.assertEqual(parsed['prepare'].config_label, 'variant-b')
        self.assertTrue(parsed['prepare'].drop_mcp)
        self.assertEqual(parsed['run'].secrets_dir, secrets)
        self.assertNotIn('sk-SECRET-KEY-12345', result.stdout + result.stderr)

    def test_default_preset_and_300_seconds_forwarded_for_each_profile(self):
        for profile in ('codex', 'bedrock'):
            result = self.run_script(['--dry-run', '-y', 'all'], stdin='', env_extra={
                'PEPI_EVAL_PROFILE': profile, 'PEPI_EVAL_RUN_NAME': f'{profile}-20',
                'PEPI_EVAL_LITELLM_VERSION': '4.2.0',
                'LITELLM_BASE_URL': 'https://x', 'LITELLM_API_KEY': 'fake'})
            self.assertEqual(result.returncode, 0, result.stderr)
            args = {argv[0]: build_parser().parse_args(argv) for argv in self.printed_commands(result)}
            self.assertEqual(args['prepare'].ids, DEFAULT_IDS)
            self.assertEqual(args['run'].timeout_seconds, 300)
            self.assertIsNone(args['prepare'].config_label)
            self.assertEqual(tasklist_hash(DEFAULT_IDS.read_text().splitlines()), IDS_SHA256)
            self.assertTrue(DEFAULT_IDS.is_file(), 'default IDs must not be deleted during cleanup')

    def test_explicit_ids_file_and_timeout_override_remain_supported(self):
        ids = Path(self.fake.name) / 'custom.ids'
        ids.write_text('sympy__sympy-20590\\n')
        result = self.run_script(['--dry-run', '-y', 'all'], stdin='', env_extra={
            'PEPI_EVAL_PROFILE': 'codex', 'PEPI_EVAL_RUN_NAME': 'smoke',
            'PEPI_EVAL_IDS': str(ids), 'PEPI_EVAL_TIMEOUT_SECONDS': '1200'})
        self.assertEqual(result.returncode, 0, result.stderr)
        args = {argv[0]: build_parser().parse_args(argv) for argv in self.printed_commands(result)}
        self.assertEqual(args['prepare'].ids, ids)
        self.assertEqual(args['run'].timeout_seconds, 1200)
        self.assertTrue(ids.exists())

    def test_comma_ids_and_one_task_smoke_forward_exact_ids_then_remove_temp(self):
        captured = Path(self.fake.name) / 'captured.ids'
        for ids in ('sympy__sympy-20590', 'a__b-1,c__d-2'):
            result = self.run_script(['-y', 'prepare'], stdin='', env_extra={
                'PEPI_EVAL_RUN_NAME': 'smoke', 'PEPI_EVAL_IDS': ids,
                'FAKE_IDS_LOG': str(captured)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(captured.read_text().splitlines(), ids.split(','))
            argv = shlex.split(self.log.read_text().splitlines()[-1])
            self.assertFalse(Path(argv[argv.index('--ids') + 1]).exists())

    def test_direct_cli_defaults_and_explicit_flags(self):
        prepare_args = ['prepare', '--task-repo', '/tasks', '--out', '/fresh',
                        '--pi-version', '1.1.0', '--subagents-version', '0.68.0', '--node-image', 'node:22']
        self.assertEqual(build_parser().parse_args(prepare_args).ids, DEFAULT_IDS)
        self.assertEqual(build_parser().parse_args(prepare_args + ['--ids', '/custom.ids']).ids,
                         Path('/custom.ids'))
        run_args = ['run', '/fresh', '--network', 'pepi-eval']
        self.assertEqual(build_parser().parse_args(run_args).timeout_seconds, 300)
        self.assertEqual(build_parser().parse_args(run_args + ['--timeout-seconds', '1200']).timeout_seconds, 1200)

    def test_optional_flags_are_omitted_when_unset(self):
        env = {'PEPI_EVAL_RUN_NAME': 'plain-run'}
        result = self.run_script(['--dry-run', '-y', 'prepare'], env_extra=env, stdin='')
        (argv,) = self.printed_commands(result)
        args = build_parser().parse_args(argv)
        self.assertIsNone(args.config_dir)
        self.assertIsNone(args.config_label)
        self.assertFalse(args.drop_mcp)

    def test_config_dir_with_secret_files_is_rejected_with_exit_2(self):
        for name in ('auth.json', 'mcp.json', '.env'):
            with self.subTest(name=name):
                config = Path(self.fake.name) / f'config-{name}'
                config.mkdir()
                (config / name).write_text('{}')
                env = {'PEPI_EVAL_RUN_NAME': 'secret-in-config', 'PEPI_EVAL_CONFIG_DIR': str(config)}
                result = self.run_script(['-y', 'prepare'], env_extra=env, stdin='')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(name, result.stderr)
                self.assertFalse(self.log.exists(), 'the runner must not be invoked')

    def test_prepare_leaves_run_directory_creation_to_the_runner(self):
        """`prepare` refuses an existing directory, so the script must not pre-create it."""
        result = self.run_script(['-y', 'prepare'], env_extra={'PEPI_EVAL_RUN_NAME': 'fresh'}, stdin='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((Path(self.runs_tmpdir.name) / 'fresh').exists())
        self.assertIn('prepare', self.log.read_text())

    def test_all_carries_prepared_run_without_name_env(self):
        template_root = Path(self.fake.name) / 'template'
        template_root.mkdir()
        template = prepare_fixture(template_root)
        for name in ('source', 'tasks'):
            (Path(self.fake.name) / name).mkdir()
        python = Path(self.fake.name) / 'venv/bin/python'
        python.write_text('''#!/bin/sh
[ "$1" = "-m" ] || exit 0
printf '%s\\n' "$*" >> "$FAKE_LOG"
if [ "$2" = "evals.swebench" ]; then
    case "$3" in
        prepare)
            while [ "$#" -gt 0 ]; do
                if [ "$1" = "--out" ]; then cp -R "$FAKE_TEMPLATE" "$2"; break; fi
                shift
            done ;;
        build) printf '{}' > "$4/builds.json" ;;
        run) printf '{}\\n' > "$4/predictions.jsonl" ;;
    esac
fi
''')
        result = self.run_script(['-y', 'all'], stdin='', env_extra={
            'FAKE_TEMPLATE': str(template), 'PEPI_EVAL_PROFILE': 'bedrock',
            'PEPI_EVAL_SWEBENCH_REPO': str(Path(self.fake.name) / 'source'),
            'PEPI_EVAL_TASK_REPO': str(Path(self.fake.name) / 'tasks'),
            'LITELLM_BASE_URL': 'https://gateway.example', 'LITELLM_API_KEY': 'SYNTHETIC',
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [shlex.split(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(calls), 6, calls)
        prepared = calls[0][calls[0].index('--out') + 1]
        for call in calls[1:]:
            target = call[2] if call[1] == 'evals.swebench.telemetry' else call[3]
            self.assertEqual(target, prepared, call)
        self.assertNotIn('Run name (from runs/)', result.stderr)
        self.assertNotIn('Available runs:', result.stderr)

    def test_blank_tty_run_name_fails_before_runner_for_each_step(self):
        for command in ('build', 'run', 'report'):
            with self.subTest(command=command):
                master, slave = pty.openpty()
                process = subprocess.Popen(['bash', str(self.script_path), '-y', command],
                                           stdin=slave, stdout=slave, stderr=slave,
                                           env=self.script_env())
                os.close(slave)
                output = b''
                try:
                    os.write(master, b'\n')
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if select.select([master], [], [], 0.1)[0]:
                            try:
                                chunk = os.read(master, 65536)
                            except OSError:
                                break
                            if not chunk:
                                break
                            output += chunk
                    process.wait(timeout=2)
                    self.assertEqual(process.returncode, 2, output.decode())
                    self.assertIn('Run name cannot be empty', output.decode())
                    self.assertFalse(self.log.exists(), 'no runner for an empty selection')
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=2)
                    os.close(master)

    def test_invalid_existing_run_names_fail_before_runner(self):
        for command in ('build', 'run', 'report'):
            for name in ('../other-run', ' ', 'bad/name', '.', '..'):
                with self.subTest(command=command, name=name):
                    result = self.run_script(['--dry-run', '-y', command], env_extra={
                        'PEPI_EVAL_RUN_NAME': name})
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('Invalid name', result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertFalse(self.log.exists())

    def test_runner_exit_codes_pass_through_as_2_and_3(self):
        for status, expected in (('2', 2), ('3', 3), ('1', 3), ('130', 130)):
            with self.subTest(status=status):
                result = self.run_script(['-y', 'prepare'], stdin='', env_extra={
                    'PEPI_EVAL_RUN_NAME': f'status-{status}', 'FAKE_STATUS': status})
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_prepare_failure_does_not_suggest_disabling_mcp(self):
        result = self.run_script(['-y', 'prepare'], stdin='', env_extra={
            'PEPI_EVAL_RUN_NAME': 'mcp-tip', 'FAKE_STATUS': '2'})
        self.assertNotIn('PEPI_EVAL_DROP_MCP=1', result.stderr)

    def test_paid_run_needs_confirmation_when_not_interactive(self):
        run = prepare_fixture(Path(self.runs_tmpdir.name))
        env = {'LITELLM_API_KEY': 'sk-SECRET-KEY-12345', 'LITELLM_BASE_URL': 'https://x',
               'PEPI_EVAL_RUN_NAME': run.name}
        result = self.run_script(['run'], env_extra=env, stdin='')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.log.exists(), 'no paid run without confirmation')
        confirmed = self.run_script(['-y', 'run'], env_extra=env, stdin='')
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        call = self.log.read_text()
        self.assertIn('evals.swebench run', call)
        self.assertNotIn('sk-SECRET-KEY-12345', call + confirmed.stdout + confirmed.stderr)

    def test_named_codex_prepare_and_dry_run_require_no_litellm_pin_or_prompt(self):
        result = self.run_script(['--dry-run', '-y', 'all'], stdin='', env_extra={
            'PEPI_EVAL_PROFILE': 'codex', 'PEPI_EVAL_RUN_NAME': 'codex',
            'PEPI_EVAL_LITELLM_VERSION': 'should-not-be-used'})
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = {argv[0]: build_parser().parse_args(argv) for argv in self.printed_commands(result)}
        self.assertEqual(parsed['prepare'].profile, 'codex')
        self.assertIsNone(parsed['prepare'].litellm_version)
        self.assertNotIn('LiteLLM', result.stderr)
        self.assertNotIn('should-not-be-used', result.stdout)

    def test_named_bedrock_prepare_requires_litellm_pin(self):
        result = self.run_script(['--dry-run', '-y', 'prepare'], stdin='', env_extra={
            'PEPI_EVAL_PROFILE': 'bedrock', 'PEPI_EVAL_RUN_NAME': 'bedrock',
            'PEPI_EVAL_LITELLM_VERSION': '4.2.0'})
        self.assertEqual(result.returncode, 0, result.stderr)
        args = build_parser().parse_args(self.printed_commands(result)[0])
        self.assertEqual(args.profile, 'bedrock')
        self.assertEqual(args.litellm_version, '4.2.0')

    def test_run_uses_frozen_codex_even_when_host_selector_is_bedrock(self):
        base = Path(self.runs_tmpdir.name)
        root = repository(base)
        prepare(root, base / 'codex-run', profile_name='codex')
        result = self.run_script(['-y', 'run'], stdin='', env_extra={
            'PEPI_EVAL_RUN_NAME': 'codex-run', 'PEPI_EVAL_PROFILE': 'bedrock'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('LITELLM', result.stderr)
        self.assertIn('evals.swebench run', self.log.read_text())

    def test_run_uses_frozen_litellm_even_when_host_selector_is_codex(self):
        run = prepare_fixture(Path(self.runs_tmpdir.name))
        result = self.run_script(['run'], stdin='', env_extra={
            'PEPI_EVAL_RUN_NAME': run.name, 'PEPI_EVAL_PROFILE': 'codex'})
        self.assertEqual(result.returncode, 2)
        self.assertIn('LITELLM_BASE_URL', result.stderr)
        self.assertFalse(self.log.exists())

    def test_tampered_run_rejected_before_secret_prompts(self):
        run = prepare_fixture(Path(self.runs_tmpdir.name))
        (run / 'build/provider-plan.json').write_text('{}')
        result = self.run_script(['run'], stdin='', env_extra={'PEPI_EVAL_RUN_NAME': run.name})
        self.assertEqual(result.returncode, 2)
        self.assertIn('Invalid frozen run', result.stderr)
        self.assertNotIn('Missing required env var: LITELLM', result.stderr)
        self.assertFalse(self.log.exists())

    def test_works_from_any_working_directory(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('PEPI_EVAL_')
               and not k.startswith('LITELLM_')}
        env['PATH'] = f"{Path(self.fake.name) / 'bin'}{os.pathsep}{env['PATH']}"
        env['HOME'] = str(self.home)
        env.update({'PEPI_EVAL_RUNS_DIR': self.runs_tmpdir.name, 'PEPI_EVAL_RUN_NAME': 'cwd',
                    'PEPI_EVAL_VENV': str(Path(self.fake.name) / 'venv'), 'FAKE_LOG': str(self.log)})
        with tempfile.TemporaryDirectory() as elsewhere:
            subprocess.run(['bash', str(self.script_path), '-y', 'prepare'], cwd=elsewhere, env=env,
                           capture_output=True, text=True, input='', timeout=5, check=True)
        # PYTHONPATH makes `-m evals.swebench` resolvable; the fake python only logs it.
        self.assertIn('evals.swebench prepare', self.log.read_text())

    def host_auth_fixture(self):
        from evals.swebench.tests.test_profiles import oauth
        base = Path(self.runs_tmpdir.name)
        root = repository(base)
        prepare(root, base / 'host-run', profile_name='codex')
        auth = self.home / '.pi/agent/auth.json'
        auth.parent.mkdir(parents=True)
        data = oauth()
        data['unrelated'] = {'type': 'api_key', 'key': 'SYNTHETIC-UNRELATED-TOKEN'}
        auth.write_text(json.dumps(data))
        auth.chmod(0o600)
        (auth.parent / 'mcp.json').write_text('SYNTHETIC-MCP-NEVER-COPY')
        inspector = Path(self.fake.name) / 'inspect.py'
        inspector.write_text('''import json, os, pathlib, stat, sys, time
args = sys.argv[1:]
d = pathlib.Path(args[args.index('--secrets-dir') + 1])
a = d / 'auth.json'
pathlib.Path(os.environ['FAKE_CAPTURE']).write_text(json.dumps({
    'directory': str(d), 'dir_mode': stat.S_IMODE(d.stat().st_mode),
    'mode': stat.S_IMODE(a.stat().st_mode), 'files': sorted(p.name for p in d.iterdir()),
    'auth': json.loads(a.read_bytes()), 'args': args, 'pid': os.getpid()}))
if os.environ.get('FAKE_SLEEP') == '1':
    time.sleep(60)
sys.exit(int(os.environ.get('FAKE_STATUS', '0')))
''')
        python = Path(self.fake.name) / 'venv/bin/python'
        python.write_text('''#!/bin/sh
[ "$1" = "-m" ] || exit 0
printf '%s\\n' "$*" >> "$FAKE_LOG"
exec python3 "$FAKE_INSPECTOR" "$@"
''')
        capture = Path(self.fake.name) / 'capture.json'
        env = {'PEPI_EVAL_RUN_NAME': 'host-run', 'FAKE_INSPECTOR': str(inspector),
               'FAKE_CAPTURE': str(capture)}
        return auth, capture, env

    def test_explicit_host_auth_filtered_private_unchanged_and_cleaned(self):
        auth, capture, env = self.host_auth_fixture()
        original = (auth.read_bytes(), auth.stat().st_mode, auth.stat().st_mtime_ns)
        for status in ('0', '2', '3', '130'):
            with self.subTest(status=status):
                result = self.run_script(['-y', 'run'], {**env, 'PEPI_EVAL_USE_HOST_AUTH': '1',
                                                          'FAKE_STATUS': status})
                self.assertEqual(result.returncode, int(status), result.stderr)
                data = json.loads(capture.read_text())
                self.assertEqual(set(data['auth']), {'openai-codex'})
                self.assertEqual(data['files'], ['auth.json'])
                self.assertEqual((data['dir_mode'], data['mode']), (0o700, 0o600))
                self.assertFalse(Path(data['directory']).exists())
                self.assertIn('invalidate', result.stderr)
                self.assertNotIn('$20', result.stderr)
                for token in ('SYNTHETIC-access', 'SYNTHETIC-refresh', 'SYNTHETIC-UNRELATED'):
                    self.assertNotIn(token, result.stdout + result.stderr + self.log.read_text())
                self.assertEqual(original, (auth.read_bytes(), auth.stat().st_mode, auth.stat().st_mtime_ns))

    def test_yes_and_non_tty_unset_never_consent_and_zero_disables(self):
        auth, capture, env = self.host_auth_fixture()
        # A FIFO would hang if opened: absence of consent must mean no read, not just no copy.
        auth.unlink()
        os.mkfifo(auth, 0o600)
        for extra in ({}, {'PEPI_EVAL_USE_HOST_AUTH': '0'}, {'PEPI_EVAL_YES': '1'}):
            result = self.run_script(['-y', 'run'], {**env, **extra})
            self.assertFalse(capture.exists())
            self.assertNotIn('Use ' + str(auth), result.stderr)

    def test_explicit_secrets_dir_wins_without_host_proposal(self):
        auth, capture, env = self.host_auth_fixture()
        auth.unlink()
        secrets = Path(self.fake.name) / 'provided'
        secrets.mkdir(mode=0o700)
        (secrets / 'auth.json').write_text('{}')
        # Use the ordinary mock: only the user directory is forwarded.
        (Path(self.fake.name) / 'venv/bin/python').write_text(FAKE_PYTHON)
        result = self.run_script(['-y', 'run'], {**env, 'PEPI_EVAL_USE_HOST_AUTH': '1',
                                                'PEPI_EVAL_SECRETS_DIR': str(secrets)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('host login', result.stderr)
        self.assertNotIn('Allow temporary', result.stderr)
        self.assertIn(str(secrets), self.log.read_text())
        self.assertTrue(secrets.exists())
        self.assertFalse(capture.exists())

    def test_host_auth_dry_run_has_no_read_or_credential_directory(self):
        auth, capture, env = self.host_auth_fixture()
        auth.unlink()
        os.mkfifo(auth, 0o600)
        before = set(Path('/tmp').glob('pepi-eval-auth-*'))
        result = self.run_script(['--dry-run', '-y', 'run'], {**env, 'PEPI_EVAL_USE_HOST_AUTH': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('no credentials read or directory created', result.stderr)
        self.assertFalse(capture.exists())
        self.assertEqual(before, set(Path('/tmp').glob('pepi-eval-auth-*')))

    def test_host_auth_bad_options_and_sources_fail_closed(self):
        auth, capture, env = self.host_auth_fixture()
        for option in ('', 'yes', '2', '01'):
            result = self.run_script(['-y', 'run'], {**env, 'PEPI_EVAL_USE_HOST_AUTH': option})
            self.assertEqual(result.returncode, 2)
            self.assertIn('must be 0 or 1', result.stderr)
        for content in ('SYNTHETIC-SECRET-invalid', '[]', '{}',
                        '{"openai-codex":{"type":"api_key","key":"SYNTHETIC-SECRET"}}'):
            auth.write_text(content)
            result = self.run_script(['-y', 'run'], {**env, 'PEPI_EVAL_USE_HOST_AUTH': '1'})
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn('SYNTHETIC-SECRET', result.stdout + result.stderr)
            self.assertFalse(capture.exists())
        auth.unlink()
        for kind in ('missing', 'symlink', 'public', 'directory', 'fifo'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    target = auth.parent / 'target'
                    target.write_text('{}'); target.chmod(0o600)
                    auth.symlink_to(target)
                elif kind == 'public':
                    auth.write_text('{}'); auth.chmod(0o644)
                elif kind == 'directory':
                    auth.mkdir()
                elif kind == 'fifo':
                    os.mkfifo(auth, 0o600)
                before = set(Path('/tmp').glob('pepi-eval-auth-*'))
                result = self.run_script(['-y', 'run'], {**env, 'PEPI_EVAL_USE_HOST_AUTH': '1'})
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(capture.exists())
                self.assertEqual(before, set(Path('/tmp').glob('pepi-eval-auth-*')))
                if auth.is_dir(): auth.rmdir()
                elif kind != 'missing': auth.unlink()

    def tty_host_auth(self, env, answer):
        master, slave = pty.openpty()
        process = subprocess.Popen(['bash', str(self.script_path), '-y', 'run'],
                                   stdin=slave, stdout=slave, stderr=slave, env=self.script_env(env))
        os.close(slave)
        output = b''
        try:
            deadline = time.monotonic() + 8
            answered = False
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try: chunk = os.read(master, 65536)
                    except OSError: break
                    if not chunk: break
                    output += chunk
                    if b'Allow temporary host-auth copy?' in output and not answered:
                        self.assertFalse(Path(env['FAKE_CAPTURE']).exists(), 'no copy before consent')
                        os.write(master, answer.encode() + b'\n')
                        answered = True
                if process.poll() is not None: break
            process.wait(timeout=3)
            return process.returncode, output.decode()
        finally:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=3)
            os.close(master)

    def test_tty_affirmative_and_default_decline_with_yes(self):
        auth, capture, env = self.host_auth_fixture()
        status, output = self.tty_host_auth(env, 'yes')
        self.assertEqual(status, 0, output)
        self.assertTrue(capture.exists())
        self.assertFalse(Path(json.loads(capture.read_text())['directory']).exists())
        capture.unlink()
        auth.unlink(); os.mkfifo(auth, 0o600)
        for answer in ('', 'n'):
            status, output = self.tty_host_auth(env, answer)
            self.assertFalse(capture.exists())
            self.assertIn('[y/N]', output)

    def test_declined_host_auth_preserves_litellm_env_flow(self):
        run = prepare_fixture(Path(self.runs_tmpdir.name))
        result = self.run_script(['-y', 'run'], {
            'PEPI_EVAL_RUN_NAME': run.name, 'PEPI_EVAL_USE_HOST_AUTH': '0',
            'LITELLM_BASE_URL': 'https://gateway.example', 'LITELLM_API_KEY': 'SYNTHETIC-TOKEN'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('--secrets-dir', self.log.read_text())

    def test_host_opt_in_still_requires_usage_confirmation_before_read(self):
        auth, capture, env = self.host_auth_fixture()
        auth.unlink(); os.mkfifo(auth, 0o600)
        result = self.run_script(['run'], {**env, 'PEPI_EVAL_USE_HOST_AUTH': '1'})
        self.assertEqual(result.returncode, 2)
        self.assertIn('not confirmed', result.stderr)
        self.assertFalse(capture.exists())
        self.assertFalse(self.log.exists())

    def test_litellm_host_auth_opt_in_skips_env_prompts(self):
        run = prepare_fixture(Path(self.runs_tmpdir.name))
        auth = self.home / '.pi/agent/auth.json'
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({'litellm': {'type': 'api_key', 'key': 'SYNTHETIC-LITELLM',
                                              'env': {'LITELLM_BASE_URL': 'https://gateway.example'}}}))
        auth.chmod(0o600)
        result = self.run_script(['-y', 'run'], {'PEPI_EVAL_RUN_NAME': run.name,
                                                'PEPI_EVAL_USE_HOST_AUTH': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Missing required', result.stderr)
        call = self.log.read_text()
        directory = Path(shlex.split(call)[-1])
        self.assertFalse(directory.exists())
        self.assertNotIn('SYNTHETIC-LITELLM', call + result.stdout + result.stderr)

    def test_host_auth_cleanup_on_sigint_and_sigterm_reaps_child(self):
        auth, capture, env = self.host_auth_fixture()
        original = (auth.read_bytes(), auth.stat().st_mode, auth.stat().st_mtime_ns)
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                capture.unlink(missing_ok=True)
                process = subprocess.Popen(['bash', str(self.script_path), '-y', 'run'],
                    env=self.script_env({**env, 'PEPI_EVAL_USE_HOST_AUTH': '1', 'FAKE_SLEEP': '1'}),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                child_pid = None
                try:
                    deadline = time.monotonic() + 5
                    while not capture.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(capture.exists())
                    data = json.loads(capture.read_text())
                    child_pid = data['pid']
                    process.send_signal(signum)
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 130, stderr)
                    self.assertFalse(Path(data['directory']).exists())
                    with self.assertRaises(ProcessLookupError): os.kill(child_pid, 0)
                    self.assertNotIn('SYNTHETIC-access', stdout + stderr)
                    self.assertEqual(original, (auth.read_bytes(), auth.stat().st_mode, auth.stat().st_mtime_ns))
                finally:
                    if process.poll() is None:
                        process.terminate(); process.communicate(timeout=5)
                    if child_pid:
                        try: os.kill(child_pid, signal.SIGKILL)
                        except ProcessLookupError: pass


if __name__ == '__main__':
    unittest.main()
