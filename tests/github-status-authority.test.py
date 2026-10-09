"""Publication adapter delegation to the production approval owner over isolated stores."""
import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src/runtime-api'), str(ROOT / 'skills/review-preflight/scripts')]
from dispatcher import RuntimeDispatcher
from request_store import RequestStore
from ha_adapter import HumanActionAdapter
from protocol import ProtocolError
spec = importlib.util.spec_from_file_location('status_authority', ROOT / 'skills/review-preflight/scripts/github-status.py')
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.store = RequestStore(str(self.path / 'state.sqlite'))
        self.addCleanup(self.store.close)
        self.sent = []
        def send(params):
            self.sent.append(params)
            return {'executed': True, 'eventId': '$fixture', 'roomId': params['resource']['roomId']}
        self.domain = RuntimeDispatcher(self.store, HumanActionAdapter(str(self.path / 'ha')), 'fixture-daemon', {'message.send': send})
        self.resolution = 'approved'
        self.change_body = False
        self.lose_response = False
        self.calls = []
        self.effect = {'errors': [], 'repository': 'o/r', 'pr': 1, 'head_sha': 'abc',
                       'observed_at': '2026-10-05T06:10:00Z', 'merge_outcome': 'not_merged',
                       'checks_status': 'passed', 'overall_readiness': 'unknown'}
        self.now = 1791180600

    def runner(self, args):
        args = args[2:]
        self.calls.append(args)
        try:
            if args[:2] == ['approval', 'request']:
                params = {'action': args[args.index('--action')+1],
                          'resource': json.loads(args[args.index('--resource')+1]),
                          'input': json.loads(args[args.index('--input')+1]), 'expiresInS': 30}
                value = asyncio.run(self.domain.handle('approval.request', params))
                self.approval_id = value['requestId']
            elif args[:2] == ['request', 'wait']:
                rid = args[2]
                if self.resolution != 'pending':
                    self.store.transition(rid, self.resolution, resolved_by='fixture-human')
                value = asyncio.run(self.domain.handle('request.get', {'requestId': rid}))
            elif args[:2] == ['capability', 'execute']:
                params = {'action': args[args.index('--action')+1],
                          'resource': json.loads(args[args.index('--resource')+1]),
                          'input': json.loads(args[args.index('--input')+1]),
                          'approvalRequestId': args[args.index('--approval')+1],
                          'idempotencyKey': args[args.index('--idempotency-key')+1]}
                if self.change_body: params['input']['body'] += ' altered'
                value = asyncio.run(self.domain.handle('capability.execute', params))
                self.execution_id = value['requestId']
                if self.lose_response: raise subprocess.TimeoutExpired(args, 20)
            else: raise AssertionError('unexpected CLI command')
            return SimpleNamespace(returncode=0, stdout=json.dumps(value))
        except ProtocolError:
            return SimpleNamespace(returncode=1, stdout='{}')

    def publish(self):
        return status.publish(self.effect, 'o/r', 1, '!room:fixture', '/fixture/runtime.py', runner=self.runner, now=self.now)

    def test_approval_owner_consumes_exact_effect_once_durably(self):
        result = self.publish()
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.sent), 1)
        approval = self.store.get(self.approval_id)
        execution = self.store.get(self.execution_id)
        self.assertTrue(approval['consumedAt'])
        self.assertEqual(execution['status'], 'completed')
        self.assertEqual(execution['actorId'], 'fixture-daemon')
        self.assertEqual(approval['params']['input'], execution['params']['input'])

    def test_pending_and_denied_never_contact_executor(self):
        for value in ('pending', 'denied'):
            self.resolution = value
            self.assertFalse(self.publish()['ok'])
            self.assertEqual(self.sent, [])
            self.assertFalse(self.store.get(self.approval_id).get('consumedAt'))

    def test_changed_approved_body_refused_by_owner_without_consumption(self):
        self.change_body = True
        self.assertFalse(self.publish()['ok'])
        self.assertEqual(self.sent, [])
        self.assertFalse(self.store.get(self.approval_id).get('consumedAt'))

    def test_lost_execution_response_does_not_retry_or_respend(self):
        self.lose_response = True
        self.assertEqual(self.publish()['state'], 'OUTCOME_UNKNOWN')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.store.get(self.execution_id)['status'], 'completed')
        self.assertTrue(self.store.get(self.approval_id)['consumedAt'])


if __name__ == '__main__':
    unittest.main()
