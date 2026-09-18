#!/usr/bin/env python3
"""Exercise worker setup without changing real launchd jobs or user paths."""
import importlib.util
import io
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('feedback_worker', ROOT / 'skills/report-feedback/install-worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.ws = self.home / 'workspace'
        self.destination = self.home / 'Library/LaunchAgents/com.sutando.feedback-recovery.plist'
        self.patches = [
            mock.patch.object(worker, 'resolve_workspace', return_value=self.ws),
            mock.patch.object(worker.Path, 'home', return_value=self.home),
            mock.patch.object(worker.platform, 'system', return_value='Darwin'),
            mock.patch.object(worker.os, 'getuid', return_value=123, create=True),
            mock.patch.object(worker.subprocess, 'run', return_value=types.SimpleNamespace(returncode=1)),
        ]
        self.workspace, self.home_mock, self.platform, self.uid, self.run = [p.start() for p in self.patches]
        for patch in self.patches:
            self.addCleanup(patch.stop)

    def cli(self, *args):
        with mock.patch.object(sys, 'argv', ['install-worker.py', *args]):
            worker.main()

    def test_install_writes_job_for_persistent_script_and_current_interpreter(self):
        self.cli()
        config = plistlib.loads(self.destination.read_bytes())
        self.assertEqual(config['ProgramArguments'], [sys.executable, str(worker.SCRIPT), '--apply'])
        self.assertEqual(config['StartInterval'], 60)
        self.assertEqual(config['WorkingDirectory'], str(ROOT))
        self.assertEqual(config['StandardOutPath'], str(self.ws / 'logs/feedback-recovery.log'))
        self.assertTrue((self.ws / 'logs').is_dir())
        self.assertEqual(self.run.call_args_list[1].args[0],
                         ['launchctl', 'bootstrap', 'gui/123', str(self.destination)])
        self.assertEqual(self.run.call_args_list[2].args[0],
                         ['launchctl', 'print', 'gui/123/com.sutando.feedback-recovery'])

    def test_reinstall_stops_existing_job_before_bootstrap(self):
        self.run.return_value.returncode = 0
        self.cli()
        self.assertEqual([c.args[0][1] for c in self.run.call_args_list],
                         ['print', 'bootout', 'bootstrap', 'print'])
        self.assertTrue(self.run.call_args_list[1].kwargs['check'])

    def test_uninstall_only_removes_our_job(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.write_bytes(b'old job')
        neighbour = self.destination.with_name('another-job.plist')
        neighbour.write_bytes(b'keep')
        self.run.return_value.returncode = 0
        self.cli('--uninstall')
        self.assertFalse(self.destination.exists())
        self.assertEqual(neighbour.read_bytes(), b'keep')
        self.assertEqual([c.args[0][1] for c in self.run.call_args_list], ['print', 'bootout'])

    def test_uninstall_already_absent_is_idempotent(self):
        self.cli('--uninstall')
        self.assertEqual(self.run.call_count, 1)
        self.assertFalse(self.destination.exists())

    def test_render_never_installs_or_writes(self):
        output = io.BytesIO()
        with mock.patch.object(sys, 'stdout', types.SimpleNamespace(buffer=output)):
            self.cli('--render')
        self.assertTrue(plistlib.loads(output.getvalue())['RunAtLoad'])
        self.run.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_other_platform_gets_scheduler_instruction_before_mutation(self):
        self.platform.return_value = 'Linux'
        with self.assertRaises(SystemExit) as result:
            self.cli()
        self.assertEqual(result.exception.code, 2)
        self.run.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_bootstrap_failure_is_not_reported_as_installed(self):
        def command(args, **kwargs):
            if args[1] == 'bootstrap':
                raise subprocess.CalledProcessError(1, args)
            return types.SimpleNamespace(returncode=1)
        self.run.side_effect = command
        with self.assertRaises(subprocess.CalledProcessError):
            self.cli()
        self.assertEqual(self.run.call_count, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
