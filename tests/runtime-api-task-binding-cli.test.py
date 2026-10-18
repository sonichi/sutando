"""Task binding through the real CLI, Unix handler and durable request owner."""
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'task_binding_transport_fixture', ROOT / 'tests/github-status-runtime-cli.test.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
from dispatcher import _fingerprint


class TaskBindingTests(fixture.TransportTests):
    def cli(self, *args):
        env = {'PATH': os.environ['PATH'], 'HOME': str(self.path),
               **self.harness.extra_env}
        proc = subprocess.run(
            [sys.executable, str(ROOT / 'src/runtime-cli/sutando-runtime.py'), *args],
            env=env, capture_output=True, text=True, timeout=15)
        return proc.returncode, json.loads(proc.stdout or proc.stderr)

    def effect(self):
        return ['--action', 'message.send', '--resource',
                json.dumps({'roomId': '!isolated:fixture'}), '--input',
                json.dumps({'body': 'isolated canonical message'})]

    def approved(self, task=None):
        scoped = ['--task-id', task] if task is not None else []
        rc, row = self.cli('approval', 'request', *scoped, *self.effect())
        self.assertEqual(rc, 0, row)
        rc, wait = self.cli('request', 'wait', row['requestId'], '--timeout', '5')
        self.assertEqual(rc, 0, wait)
        self.assertEqual(wait['status'], 'approved')
        return row['requestId']

    def execute(self, approval, task=None, key='fixture-key'):
        scoped = ['--task-id', task] if task is not None else []
        return self.cli('capability', 'execute', *scoped, *self.effect(),
                        '--approval', approval, '--idempotency-key', key)

    def test_different_or_removed_task_refuses_without_consumption(self):
        rid = self.approved('fixture-task-A')
        for task in ('fixture-task-B', None):
            rc, row = self.execute(rid, task)
            self.assertNotEqual(rc, 0, row)
            self.assertIn('resource/input or task', row['error'])
            self.assertFalse(self.srv.store.get(rid)['consumedAt'])
            self.assertEqual(self.sent, [])
        rc, row = self.execute(rid, 'fixture-task-A')
        self.assertEqual(rc, 0, row)
        self.assertTrue(self.srv.store.get(rid)['consumedAt'])
        self.assertEqual(len(self.sent), 1)
        rc, replay = self.execute(rid, 'fixture-task-B')
        self.assertNotEqual(rc, 0, replay)
        rc, replay = self.execute(rid, 'fixture-task-A')
        self.assertEqual(rc, 0, replay)
        self.assertTrue(replay['idempotentReplay'])
        self.assertEqual(len(self.sent), 1)

    def test_unscoped_approval_cannot_acquire_task_context(self):
        rid = self.approved()
        rc, row = self.execute(rid, 'fixture-task-A')
        self.assertNotEqual(rc, 0, row)
        self.assertFalse(self.srv.store.get(rid)['consumedAt'])
        rc, row = self.execute(rid)
        self.assertEqual(rc, 0, row)
        self.assertEqual(len(self.sent), 1)

    def test_completed_record_with_missing_or_invalid_fingerprint_cannot_replay(self):
        params = {'taskId': 'fixture-task-A', 'action': 'message.send',
                  'resource': {'roomId': '!isolated:fixture'},
                  'input': {'body': 'isolated canonical message'}}
        for index, fingerprint in enumerate((None, 'invalid-persisted-fingerprint')):
            key = 'invalid-fingerprint-' + str(index)
            record = self.srv.store.create(
                'capability', 'capability.execute', 'fixture-daemon', params,
                task_id=params['taskId'], idempotency_key=key, fingerprint=fingerprint)
            self.srv.store.transition(record['requestId'], 'completed',
                                      result={'executed': True, 'eventId': '$old-fixture'})
            rc, row = self.execute('unused', 'fixture-task-A', key=key)
            self.assertNotEqual(rc, 0, row)
            self.assertIn('per-execution', row['error'])
            self.assertEqual(self.srv.store.get(record['requestId'])['status'], 'completed')
        self.assertEqual(self.sent, [])

    def test_legacy_completed_record_replays_only_its_original_task(self):
        params = {'taskId': 'fixture-task-A', 'action': 'message.send',
                  'resource': {'roomId': '!isolated:fixture'},
                  'input': {'body': 'isolated canonical message'}}
        record = self.srv.store.create(
            'capability', 'capability.execute', 'fixture-daemon', params,
            task_id=params['taskId'], idempotency_key='fixture-key',
            fingerprint=_fingerprint({k: v for k, v in params.items() if k != 'taskId'}))
        self.srv.store.transition(record['requestId'], 'completed',
                                  result={'executed': True, 'eventId': '$old-fixture'})
        rc, row = self.execute('unused', 'fixture-task-A')
        self.assertEqual(rc, 0, row)
        self.assertTrue(row['idempotentReplay'])
        self.assertEqual(row['requestId'], record['requestId'])
        for task in ('fixture-task-B', None):
            rc, row = self.execute('unused', task)
            self.assertNotEqual(rc, 0, row)
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
