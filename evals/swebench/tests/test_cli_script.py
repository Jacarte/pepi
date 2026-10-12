"""Tests for evals/pepi-eval.sh: subprocess-based core functionality."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class PepiEvalTest(unittest.TestCase):
    """Test evals/pepi-eval.sh command line interface."""
    
    @classmethod
    def setUpClass(cls):
        cls.script_path = Path(__file__).resolve().parents[2] / "pepi-eval.sh"
        if not cls.script_path.exists():
            raise RuntimeError(f"Script not found: {cls.script_path}")
    
    def setUp(self):
        self.runs_tmpdir = tempfile.TemporaryDirectory()
    
    def tearDown(self):
        self.runs_tmpdir.cleanup()
    
    def run_script(self, args, env_extra=None, stdin=None):
        """Run the script."""
        env = os.environ.copy()
        env['PEPI_EVAL_RUNS_DIR'] = self.runs_tmpdir.name
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


if __name__ == '__main__':
    unittest.main()
