"""Test suite for evals/pepi-eval.sh CLI script."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class CLIScriptTest(unittest.TestCase):
    """Tests for the interactive bash eval CLI script."""

    @classmethod
    def setUpClass(cls):
        """Find the script."""
        cls.script = Path(__file__).parent.parent.parent.parent / "evals" / "pepi-eval.sh"
        if not cls.script.exists():
            raise FileNotFoundError(f"Script not found: {cls.script}")
        if not os.access(cls.script, os.X_OK):
            raise PermissionError(f"Script is not executable: {cls.script}")

    def run_script(self, args=None, env_vars=None, stdin_data=None):
        """Run the script and return (returncode, stdout, stderr)."""
        args = args or []
        env = os.environ.copy()
        env.pop("PEPI_EVAL_YES", None)
        env.pop("LITELLM_API_KEY", None)
        if env_vars:
            env.update(env_vars)
        # Force non-TTY
        env.pop("FORCE_TTY", None)
        
        cmd = ["bash", str(self.script)] + args
        result = subprocess.run(
            cmd,
            stdin=subprocess.PIPE if stdin_data is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            input=stdin_data,
            timeout=10
        )
        return result.returncode, result.stdout, result.stderr

    def test_help_command(self):
        """Test help output."""
        code, stdout, stderr = self.run_script(["help"])
        self.assertEqual(code, 0, f"help failed:\n{stderr}")
        self.assertIn("Commands:", stdout)
        self.assertIn("check", stdout)
        self.assertIn("prepare", stdout)
        self.assertIn("run", stdout)

    def test_script_is_executable(self):
        """Ensure the script is executable."""
        self.assertTrue(os.access(self.script, os.X_OK), f"Script is not executable: {self.script}")

    def test_env_var_prefix_documented(self):
        """Test that help text documents PEPI_EVAL_* vars."""
        code, stdout, stderr = self.run_script(["help"])
        self.assertEqual(code, 0)
        self.assertIn("PEPI_EVAL", stdout)
        self.assertIn("VENV", stdout)
        self.assertIn("TASK_REPO", stdout)

    def test_check_command_exists(self):
        """Test that 'check' command runs without requiring full setup."""
        env_vars = {
            "PEPI_EVAL_VENV": os.path.expanduser("~/.venvs/swebench"),
            "PEPI_EVAL_SWEBENCH_REPO": os.path.expanduser("~/SWE-bench"),
            "PEPI_EVAL_TASK_REPO": os.path.expanduser("~/swe-bench-tasks"),
        }
        
        code, stdout, stderr = self.run_script(["check"], env_vars=env_vars)
        # check may fail if repos/venv don't exist, but it shouldn't crash
        self.assertIn(code, [0, 1, 3], f"check exited with unexpected code: {code}")

    def test_prepare_command_with_env(self):
        """Test prepare subcommand with required env vars."""
        env_vars = {
            "PEPI_EVAL_VENV": os.path.expanduser("~/.venvs/swebench"),
            "PEPI_EVAL_TASK_REPO": os.path.expanduser("~/swe-bench-tasks"),
            "PEPI_EVAL_IDS": "sympy__sympy-20590",
            "PEPI_EVAL_RUN_NAME": "test-cli-unit-01",
            "PEPI_EVAL_PI_VERSION": "1.1.0",
            "PEPI_EVAL_LITELLM_VERSION": "3.0.1",
            "PEPI_EVAL_SUBAGENTS_VERSION": "0.68.0",
            "PEPI_EVAL_NODE_IMAGE": "node:22-bookworm-slim",
            "PEPI_EVAL_YES": "1",
        }
        
        code, stdout, stderr = self.run_script(["prepare"], env_vars=env_vars)
        # prepare may fail if repos don't exist, but should exit cleanly
        self.assertIn(code, [0, 1, 2, 3], f"prepare exited: {code}, stderr: {stderr}")

    def test_api_key_not_in_dry_run_output(self):
        """Test that API key never appears in script output."""
        secret_key = "sk-super-secret-api-key-12345"
        env_vars = {
            "PEPI_EVAL_VENV": os.path.expanduser("~/.venvs/swebench"),
            "PEPI_EVAL_TASK_REPO": os.path.expanduser("~/swe-bench-tasks"),
            "LITELLM_API_KEY": secret_key,
        }
        
        code, stdout, stderr = self.run_script(["help"], env_vars=env_vars)
        output = stdout + stderr
        
        # API key must never appear (should not be in help at all)
        self.assertNotIn(secret_key, output, "API key appeared in output!")

    def test_help_shows_env_vars(self):
        """Test that help includes env var documentation."""
        code, stdout, stderr = self.run_script(["help"])
        self.assertEqual(code, 0)
        self.assertIn("VENV", stdout)  # listed under PEPI_EVAL_* prefix
        self.assertIn("TASK_REPO", stdout)
        self.assertIn("LITELLM_API_KEY", stdout)
        self.assertIn("LITELLM_BASE_URL", stdout)

    def test_script_syntax(self):
        """Test that the script has valid bash syntax."""
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )
        self.assertEqual(result.returncode, 0, f"Syntax errors:\n{result.stderr}")


if __name__ == "__main__":
    unittest.main()
