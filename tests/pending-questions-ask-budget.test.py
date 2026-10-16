"""Bounded, durable asks using disposable workspaces and a failing child adapter."""
import importlib
import json
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / 'src'), str(REPO / 'skills/pending-questions/scripts')]
adapter = importlib.import_module('pending_questions_room_db')
store_mod = importlib.import_module('pending_questions_store')


class AskBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pq-budget-')
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        (self.ws / 'state').mkdir()
        (self.ws / 'results').mkdir()
        self.seen = self.ws / 'seen.jsonl'
        self.child = self.ws / 'adapter.py'
        self.child.write_text('''import json, pathlib, sys, time
ws = pathlib.Path(sys.argv[1])
held = list((ws / 'state/pending-questions-outbox').glob('*.json'))
questions = [json.loads(p.read_text())['question']['question'] for p in held]
with (ws / 'seen.jsonl').open('a') as out:
    out.write(json.dumps(questions) + '\\n')
time.sleep(0.04)
print(json.dumps({'ok': False, 'error': 'synthetic unavailable'}))
''')
        self.client = store_mod.ScriptDbClient([sys.executable, str(self.child), str(self.ws)], timeout=0.15)
        self.store = store_mod.RoomDbStore(self.client, host='fake-host')

    def test_new_ask_is_durable_before_any_child_and_failures_do_not_scale_with_backlog(self):
        for n in range(8):
            q = store_mod.Question('ask-old-' + str(n), 'old question', asked_at=1)
            store_mod.Outbox(self.ws).save(q, '**Sent:** synthetic', 1)
        started = time.monotonic()
        out = adapter.ask_owner('new question', urgency='durable', workspace=self.ws,
                                host='fake-host', store=self.store)
        elapsed = time.monotonic() - started
        first = json.loads(self.seen.read_text().splitlines()[0])
        self.assertIn('new question', first, 'the new ask must be held before the first database child')
        self.assertLess(elapsed, 0.35, 'one ask must not spend a fresh child timeout per backlog entry')
        self.assertTrue(Path(out['outbox']).is_file())
        self.assertEqual(len(store_mod.Outbox(self.ws).entries()), 9)
        self.assertTrue((self.ws / 'results' / out['proactive_file']).is_file())

    def test_a_confirmed_new_row_does_not_wait_for_every_failing_backlog_child(self):
        for n in range(8):
            store_mod.Outbox(self.ws).save(store_mod.Question('ask-old-' + str(n), 'old question'),
                                          '**Sent:** synthetic', 1)
        self.child.write_text('''import json, pathlib, sys, time
ws = pathlib.Path(sys.argv[1])
req = json.load(sys.stdin)
state = ws / 'row.json'
with (ws / 'seen.jsonl').open('a') as out:
    out.write(req['op'] + '\\n')
if req['op'] == 'add_row' and req['cells']['name'] == 'new question':
    state.write_text(json.dumps({'id': req['row'], 'cells': req['cells'], 'body': req['body']}))
    print(json.dumps({'ok': True, 'result': {'created': True}}))
elif req['op'] == 'row' and state.exists():
    print(json.dumps({'ok': True, 'result': json.loads(state.read_text())}))
elif req['op'] == 'rows':
    print(json.dumps({'ok': True, 'result': [json.loads(state.read_text())] if state.exists() else []}))
else:
    time.sleep(0.04)
    print(json.dumps({'ok': False, 'error': 'synthetic unavailable'}))
''')
        self.client.timeout = 0.25
        self.store.host = None
        started = time.monotonic()
        out = adapter.ask_owner('new question', urgency='durable', workspace=self.ws,
                                host='fake-host', store=self.store)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIsNotNone(out['record'])
        self.assertIsNone(out['outbox'])
        self.assertEqual(len(store_mod.Outbox(self.ws).entries()), 8)
        self.assertTrue(out['reconcile']['errors'])
        self.assertLess(len(self.seen.read_text().splitlines()), 10)

    def test_host_row_confirmation_at_deadline_still_reports_and_notifies(self):
        clock = [0.0]
        row = {}
        operations = []

        def child(_argv, **kwargs):
            req = json.loads(kwargs['input'])
            operations.append(req['op'])
            if req['op'] == 'rows':
                result = []
            elif req['op'] == 'add_row':
                row.update(id=req['row'], cells=req['cells'], body=req['body'])
                result = {'created': True}
            elif req['op'] == 'row':
                result = dict(row)
                clock[0] = 1.001
            else:
                self.fail(req['op'])
            return SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'result': result}), stderr='')

        self.client.timeout = 1
        with mock.patch.object(store_mod.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(store_mod.subprocess, 'run', side_effect=child), \
                mock.patch.object(adapter.core_ask, 'notify_macos', return_value=(True, None)) as notify:
            out = adapter.ask_owner('new question', urgency='live', workspace=self.ws,
                                    host='fake-host', store=self.store)
        self.assertIsNotNone(out['record'])
        self.assertIsNone(out['outbox'])
        self.assertTrue((self.ws / 'state/pending-questions-store-history').is_file())
        self.assertTrue((self.ws / 'results' / out['proactive_file']).is_file())
        self.assertTrue(out['reconcile']['errors'])
        self.assertEqual(operations, ['rows', 'add_row', 'rows', 'row'])
        notify.assert_called_once_with('Question: new question')

    def test_one_operation_shares_its_deadline_and_a_later_operation_gets_a_fresh_budget(self):
        with self.client.operation():
            for _ in range(8):
                with self.assertRaises(store_mod.StoreError):
                    self.client.rows(store_mod.DB_SCHEMA)
        calls = len(self.seen.read_text().splitlines())
        self.assertLess(calls, 8, 'expired operations must stop spawning children')
        with self.client.operation():
            with self.assertRaises(store_mod.StoreError):
                self.client.rows(store_mod.DB_SCHEMA)
        self.assertEqual(len(self.seen.read_text().splitlines()), calls + 1)


if __name__ == '__main__':
    unittest.main()
