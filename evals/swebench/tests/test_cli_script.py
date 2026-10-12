"""Tests for evals/pepi-eval.sh: subprocess-based core functionality."""
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from evals.swebench.__main__ import build_parser

# Stand-in for the swebench venv's python: records `-m` invocations, exits with $FAKE_STATUS.
FAKE_PYTHON = """#!/bin/sh
[ "$1" = "-m" ] || exit 0
printf '%s\\n' "$*" >> "$FAKE_LOG"
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
        self.log = fake / 'calls.log'
    
    def tearDown(self):
        self.runs_tmpdir.cleanup()
        self.fake.cleanup()
    
    def run_script(self, args, env_extra=None, stdin=None):
        """Run the script against a fake venv/docker, so tests need neither."""
        env = os.environ.copy()
        env['PEPI_EVAL_RUNS_DIR'] = self.runs_tmpdir.name
        env['PEPI_EVAL_VENV'] = str(Path(self.fake.name) / 'venv')
        env['PATH'] = f"{Path(self.fake.name) / 'bin'}{os.pathsep}{env['PATH']}"
        env['FAKE_LOG'] = str(self.log)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        
        if env_extra:
            env.update(env_extra)
        
        result = subprocess.run(
            ['bash', str(self.script_path)] + args,
            env=env,
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
        result = self.run_script(['run'], env_extra={}, stdin='')
        
        self.assertEqual(result.returncode, 2)
        self.assertIn('LITELLM_BASE_URL', result.stderr)
        self.assertIn('error:', result.stderr)
    
    def test_non_tty_run_with_base_url_without_api_key_exits_2(self):
        """Non-TTY run with LITELLM_BASE_URL but no key exits 2 naming LITELLM_API_KEY."""
        env = {'LITELLM_BASE_URL': 'https://gateway.example'}
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

    def test_runner_exit_codes_pass_through_as_2_and_3(self):
        for status, expected in (('2', 2), ('3', 3), ('1', 3), ('130', 130)):
            with self.subTest(status=status):
                result = self.run_script(['-y', 'prepare'], stdin='', env_extra={
                    'PEPI_EVAL_RUN_NAME': f'status-{status}', 'FAKE_STATUS': status})
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_prepare_exit_2_without_drop_mcp_suggests_the_flag(self):
        result = self.run_script(['-y', 'prepare'], stdin='', env_extra={
            'PEPI_EVAL_RUN_NAME': 'mcp-tip', 'FAKE_STATUS': '2'})
        self.assertIn('PEPI_EVAL_DROP_MCP=1', result.stderr)

    def test_paid_run_needs_confirmation_when_not_interactive(self):
        run = Path(self.runs_tmpdir.name) / 'paid'
        run.mkdir()
        (run / 'manifest.json').write_text('{"tasks": []}')
        env = {'LITELLM_API_KEY': 'sk-SECRET-KEY-12345', 'LITELLM_BASE_URL': 'https://x',
               'PEPI_EVAL_RUN_NAME': 'paid'}
        result = self.run_script(['run'], env_extra=env, stdin='')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.log.exists(), 'no paid run without confirmation')
        confirmed = self.run_script(['-y', 'run'], env_extra=env, stdin='')
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        call = self.log.read_text()
        self.assertIn('evals.swebench run', call)
        self.assertNotIn('sk-SECRET-KEY-12345', call + confirmed.stdout + confirmed.stderr)

    def test_works_from_any_working_directory(self):
        env = os.environ.copy()
        env.update({'PEPI_EVAL_RUNS_DIR': self.runs_tmpdir.name, 'PEPI_EVAL_RUN_NAME': 'cwd',
                    'PEPI_EVAL_VENV': str(Path(self.fake.name) / 'venv'), 'FAKE_LOG': str(self.log)})
        with tempfile.TemporaryDirectory() as elsewhere:
            subprocess.run(['bash', str(self.script_path), '-y', 'prepare'], cwd=elsewhere, env=env,
                           capture_output=True, text=True, input='', timeout=5, check=True)
        # PYTHONPATH makes `-m evals.swebench` resolvable; the fake python only logs it.
        self.assertIn('evals.swebench prepare', self.log.read_text())


if __name__ == '__main__':
    unittest.main()
