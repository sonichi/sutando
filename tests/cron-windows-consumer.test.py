"""Windows cron fires require the exact dispatcher named by this workspace."""
import importlib.util
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
spec = importlib.util.spec_from_file_location('cron_windows_consumer', ROOT / 'src/cron-runner.py')
cron = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cron)


class WindowsConsumer(unittest.TestCase):
    def setUp(self):
        # Patch only the runner's OS view; pathlib and file locking use the real host.
        windows = SimpleNamespace(**{**vars(os), 'name': 'nt', 'path': ntpath})
        for name, value in [('os', windows), ('REPO_ROOT', PureWindowsPath('C:/cron fixture/checkout'))]:
            patcher = patch.object(cron, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_exact_pid_executable_and_script_are_required(self):
        with tempfile.TemporaryDirectory(prefix='cron consumer ') as folder:
            workspace = Path(folder)
            (workspace / 'state').mkdir()
            pid_file = workspace / 'state/task-dispatcher.pid'
            pid_file.write_text('123')
            script = cron.REPO_ROOT / 'src/task-dispatcher.ps1'
            good = f'123 1 "C:\\Program Files\\PowerShell\\7\\pwsh.exe" -NoProfile -File "{script}" -Background\n'
            cases = [
                (good, True), (None, False), ('', False),
                (good.replace('123 1', '124 1'), False),
                (good.replace('pwsh.exe', 'other.exe'), False),
                (good.replace(str(script), str(script) + '.bak'), False),
                (good.replace('-File', '-Command'), False),
                (good.replace('pwsh.exe', 'powershell.exe'), True),
                (good.replace('-File', '-fIlE').replace(str(script), str(script).upper()), True),
                ('123 1 pwsh.exe -File', False),
                ('123 1 pwsh.exe -File "unterminated', False),
                ('123\n' + good, True),
            ]
            with patch.object(cron, 'WORKSPACE', workspace):
                for snapshot, expected in cases:
                    with self.subTest(snapshot=snapshot), patch('sutando_platform.process_snapshot', return_value=snapshot):
                        self.assertEqual(cron.local_core_alive(), expected)
                pid_file.write_text('not-a-pid')
                self.assertFalse(cron.local_core_alive())
                pid_file.unlink()
                self.assertFalse(cron.local_core_alive())
                self.assertEqual(cron._core_alive_reason(1700000040),
                                 'this installation has no verified Windows task dispatcher')

    def test_verified_dispatcher_releases_due_prompt_without_tmux_heartbeat(self):
        with tempfile.TemporaryDirectory(prefix='cron prompt ') as folder:
            workspace = Path(folder)
            (workspace / 'state').mkdir()
            (workspace / 'state/task-dispatcher.pid').write_text('123')
            config = workspace / 'crons.json'
            config.write_text(json.dumps([{
                'name': 'proof', 'cron': '* * * * *', 'launchd': True, 'prompt': 'Return OK',
            }]))
            snapshot = f'123 1 pwsh.exe -File "{cron.REPO_ROOT / "src/task-dispatcher.ps1"}"\n'
            with patch.multiple(cron, WORKSPACE=workspace, CRONS_FILE=config,
                                STATE_FILE=workspace / 'state/cron.json', TASKS_DIR=workspace / 'tasks'), \
                    patch('sutando_platform.process_snapshot', return_value=snapshot), \
                    patch.object(cron, '_emit_cron_telemetry'):
                with patch('sutando_platform.process_snapshot', return_value=None):
                    self.assertEqual(cron.run(now_epoch=1700000040), [])
                self.assertFalse((workspace / 'tasks').exists())
                held = workspace / 'state' / cron.HOLD_FILE.name
                self.assertEqual(json.loads(held.read_text())['reason'],
                                 'this installation has no verified Windows task dispatcher')
                self.assertEqual(cron.run(now_epoch=1700000040), ['proof'])
                self.assertFalse(held.exists())
                tasks = list((workspace / 'tasks').glob('task-cron-proof-*.txt'))
                self.assertEqual(len(tasks), 1)
                self.assertIn('task: Return OK', tasks[0].read_text())
                self.assertEqual(cron.run(now_epoch=1700000040), [])


if __name__ == '__main__':
    unittest.main()
