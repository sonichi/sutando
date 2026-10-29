import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InboxRoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name)
        for name in ('tasks', 'results', 'state'):
            (self.workspace / name).mkdir()
        (self.workspace / 'tasks/task-core.txt').write_text('id: task-core\ntask: core-owned work\n')
        self.env = dict(os.environ)
        for key in ('SUTANDO_CORE_SESSION', 'SUTANDO_INSTANCE_ID', 'CLAUDE_CODE_SESSION_ID'):
            self.env.pop(key, None)
        self.env.update(SUTANDO_TEST_MODE='1', SUTANDO_WORKSPACE=str(self.workspace),
                        SUTANDO_STOP_HOOK_WATCHER_GATE='0')
        resolved = subprocess.run(['bash', str(ROOT / 'scripts/sutando-config.sh'), 'workspace'],
                                  env=self.env, capture_output=True, text=True, check=True)
        self.assertEqual(Path(resolved.stdout.strip()).resolve(), self.workspace.resolve())

    def verdict(self, **role):
        result = subprocess.run(['bash', str(ROOT / 'src/check-pending-tasks.sh')], env=self.env | role,
                                input='{}', capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_explicit_detached_session_ignores_core_queue_without_heartbeat(self):
        self.assertEqual(self.verdict(SUTANDO_CORE_SESSION='0'), {})
        self.assertFalse(list((self.workspace / 'state').iterdir()))

    def test_core_marker_still_blocks(self):
        self.assertEqual(self.verdict(SUTANDO_CORE_SESSION='1')['decision'], 'block')

    def test_unmarked_or_invalid_role_still_fails_closed(self):
        for role in ({}, {'SUTANDO_CORE_SESSION':'invalid'}):
            with self.subTest(role=role):
                self.assertEqual(self.verdict(**role)['decision'], 'block')

    def test_enrolled_worker_still_owns_deliveries(self):
        inbox = self.workspace / 'deliveries/fixture-worker'
        inbox.mkdir(parents=True)
        (inbox / 'task-worker.txt').touch()
        (self.workspace / 'tasks/task-worker.txt').write_text('id: task-worker\ntask: worker-owned work\n')
        self.assertEqual(self.verdict(SUTANDO_CORE_SESSION='0', SUTANDO_INSTANCE_ID='fixture-worker')['decision'], 'block')


if __name__ == '__main__':
    unittest.main()
