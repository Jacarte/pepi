"""Host-auth adapter regressions use synthetic credentials only, never live HOME."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.swebench.host_auth import required_host_auth, run_with_host_auth
from evals.swebench.tests.test_profiles import PINS, oauth, prepare, repository


class HostAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = repository(base)
        self.run = base / 'run'
        prepare(self.root, self.run, profile_name='codex')
        self.home = base / 'home'
        self.source = self.home / '.pi/agent/auth.json'
        self.source.parent.mkdir(parents=True)
        self.source.write_text(json.dumps(oauth()))
        self.source.chmod(0o600)

    def test_frozen_plan_rejected_before_source_open(self):
        (self.run / 'build/provider-plan.json').write_text('{}')
        with patch('evals.swebench.host_auth.os.open', side_effect=AssertionError('must not open auth')):
            with self.assertRaises(ValueError):
                required_host_auth(self.run, self.source)

    def test_oauth_pin_and_schema_reused_before_temp_copy(self):
        prepare(self.root, self.run.parent / 'wrong-pin', profile_name='codex',
                versions={**PINS, 'pi': '1.2.0'})
        with self.assertRaisesRegex(ValueError, '1.1.0'):
            required_host_auth(self.run.parent / 'wrong-pin', self.source)
        for entry in ({'type': 'oauth', 'access': 'SYNTHETIC-ACCESS'},
                      {'type': 'oauth', 'access': 'SYNTHETIC-ACCESS',
                       'refresh': 'SYNTHETIC-REFRESH', 'expires': '1'},
                      {'type': 'api_key', 'key': '!SYNTHETIC-COMMAND'}):
            self.source.write_text(json.dumps({'openai-codex': entry}))
            with self.assertRaises(ValueError): required_host_auth(self.run, self.source)

    def test_explicit_child_providers_are_required_and_unrelated_entries_filtered(self):
        settings_path = self.root / 'profiles/codex/settings.json'
        settings = json.loads(settings_path.read_text())
        settings['subagents']['agentOverrides']['reviewer']['model'] = 'anthropic/test-model'
        settings_path.write_text(json.dumps(settings))
        child_run = self.run.parent / 'child-run'
        prepare(self.root, child_run, profile_name='codex')
        with self.assertRaisesRegex(ValueError, 'missing'):
            required_host_auth(child_run, self.source)
        auth = {**oauth(), 'anthropic': {'type': 'api_key', 'key': 'SYNTHETIC-CHILD'},
                'unrelated': {'type': 'invalid'}}
        self.source.write_text(json.dumps(auth))
        self.assertEqual(set(json.loads(required_host_auth(child_run, self.source))),
                         {'openai-codex', 'anthropic'})

    def test_oversize_and_unreadable_source_rejected(self):
        self.source.write_bytes(b' ' * 1048577)
        with self.assertRaisesRegex(ValueError, 'too large'):
            required_host_auth(self.run, self.source)
        self.source.chmod(0o000)
        with self.assertRaises((ValueError, PermissionError)):
            required_host_auth(self.run, self.source)

    def test_temp_cleanup_for_write_and_launch_failure(self):
        real_open = os.open
        real_temp = tempfile.TemporaryDirectory
        created = []

        def track_temp(*args, **kwargs):
            directory = real_temp(*args, **kwargs)
            created.append(directory.name)
            return directory

        def fail_write(path, *args, **kwargs):
            if Path(path).parent.name.startswith('pepi-eval-auth-'):
                raise OSError('SYNTHETIC-SENSITIVE-error')
            return real_open(path, *args, **kwargs)

        with patch.dict(os.environ, {'HOME': str(self.home)}), \
                patch('evals.swebench.host_auth.tempfile.TemporaryDirectory', side_effect=track_temp):
            with patch('evals.swebench.host_auth.os.open', side_effect=fail_write):
                self.assertEqual(run_with_host_auth(self.run, ['/no-such-executable']), 2)
            self.assertEqual(run_with_host_auth(self.run, ['/no-such-executable']), 2)
        self.assertEqual(len(created), 2)
        self.assertTrue(all(not Path(path).exists() for path in created))

    def test_pending_signal_during_temp_setup_cleans_directory_and_child(self):
        real_temp = tempfile.TemporaryDirectory
        real_popen = subprocess.Popen
        created, children = [], []
        def interrupt_setup(*args, **kwargs):
            directory = real_temp(*args, **kwargs)
            created.append(directory.name)
            os.kill(os.getpid(), signal.SIGTERM)
            return directory
        def track_child(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        with patch.dict(os.environ, {'HOME': str(self.home)}), \
                patch('evals.swebench.host_auth.tempfile.TemporaryDirectory', side_effect=interrupt_setup), \
                patch('evals.swebench.host_auth.subprocess.Popen', side_effect=track_child):
            self.assertEqual(run_with_host_auth(self.run, [sys.executable, '-c',
                                                         'import time; time.sleep(60)']), 130)
        self.assertEqual(len(created), 1)
        self.assertFalse(Path(created[0]).exists())
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        with self.assertRaises(ProcessLookupError): os.kill(children[0].pid, 0)

    def test_interruption_during_source_validation_restores_handlers_without_temp(self):
        previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        def interrupt(*_args):
            os.kill(os.getpid(), signal.SIGTERM)
        with patch('evals.swebench.host_auth.required_host_auth', side_effect=interrupt), \
                patch('evals.swebench.host_auth.tempfile.TemporaryDirectory') as create:
            self.assertEqual(run_with_host_auth(self.run, ['/no-such-executable']), 130)
            create.assert_not_called()
        self.assertEqual(previous, {s: signal.getsignal(s) for s in previous})


if __name__ == '__main__':
    unittest.main()
