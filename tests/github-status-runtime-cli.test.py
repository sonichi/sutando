"""Real status CLI → real runtime CLI → production Unix transport and approval stores."""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/runtime-api'))
import server
from ha_adapter import ha_action_id
spec = importlib.util.spec_from_file_location('status_cli_fixture', ROOT / 'tests/github-status-cli.test.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.harness = fixture.CliTests('test_default_runs_collectors_without_publication')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.tmp = tempfile.TemporaryDirectory(prefix='status-owner-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.sent = []
        self.denied = False
        self.omit_event = False
        self.errors = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        self.assertTrue(self.ready.wait(5))
        self.addCleanup(self.shutdown)
        self.harness.extra_env = {'SUTANDO_RUNTIME_SOCKET': str(self.path / 's.sock'),
                                  'SUTANDO_SCP_WSS_URL': '', 'SUTANDO_RUN_DIR': str(self.path / 'run'),
                                  'REMOTE_TASK_URL': '', 'REMOTE_TASK_TOKEN': ''}

    def shutdown(self):
        self.stop.set()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [])

    def serve(self):
        try:
            asyncio.run(self.serve_async())
        except Exception as exc:
            self.errors.append(type(exc).__name__)
            self.ready.set()

    async def serve_async(self):
        with patch.object(server, 'resolve_actor_id', return_value='fixture-daemon'):
            self.srv = server.RuntimeServer(str(self.path / 's.sock'), str(self.path / 'state.sqlite'), str(self.path / 'ha'))
        def send(params):
            self.sent.append(params)
            result = {'executed': True, 'roomId': params['resource']['roomId']}
            if not self.omit_event: result['eventId'] = '$isolated-fixture'
            return result
        self.srv.dispatcher.executors = {'message.send': send}
        transport = await asyncio.start_unix_server(self.srv.client, path=str(self.path / 's.sock'))
        self.ready.set()
        answered = set()
        try:
            async with transport:
                while not self.stop.is_set():
                    for rec in self.srv.store.pending():
                        if rec['requestType'] == 'approval' and rec['requestId'] not in answered:
                            self.srv.ha.resolve(ha_action_id(rec['requestId']), {'1': [2 if self.denied else 1]}, 'fixture-human')
                            answered.add(rec['requestId'])
                    await asyncio.sleep(.02)
        finally:
            self.srv.store.close()

    def run_status(self):
        return self.harness.run_cli('--room', '!isolated:fixture', '--runtime-tool', str(ROOT / 'src/runtime-cli/sutando-runtime.py'))

    def test_real_cli_approved_exact_effect_has_one_durable_execution(self):
        proc = self.run_status()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        publication = json.loads(proc.stdout)['publication']
        self.assertEqual(publication['event_id'], '$isolated-fixture')
        self.assertEqual(len(self.sent), 1)
        approval = self.srv.store.get(publication['approval_request_id'])
        execution = self.srv.store.get(publication['execution_request_id'])
        self.assertTrue(approval['consumedAt'])
        self.assertEqual(execution['actorId'], 'fixture-daemon')
        self.assertEqual(execution['status'], 'completed')
        self.assertEqual(approval['params']['input'], execution['params']['input'])
        self.assertIn('Overall readiness: unknown', self.sent[0]['input']['body'])

    def test_real_cli_denied_approval_has_no_execution(self):
        self.denied = True
        proc = self.run_status()
        self.assertEqual(proc.returncode, 2, proc.stderr)
        publication = json.loads(proc.stdout)['publication']
        self.assertEqual(publication['state'], 'NOT_APPROVED')
        self.assertEqual(self.sent, [])
        approval = self.srv.store.get(publication['approval_request_id'])
        self.assertEqual(approval['status'], 'denied')
        self.assertFalse(approval['consumedAt'])

    def test_real_cli_unconfirmed_execution_is_not_retried(self):
        self.omit_event = True
        proc = self.run_status()
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['publication']['state'], 'OUTCOME_UNKNOWN')
        self.assertEqual(len(self.sent), 1)


if __name__ == '__main__':
    unittest.main()
