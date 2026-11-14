"""Bounded, durable asks using disposable workspaces and a failing child adapter."""
import importlib
import contextlib
import io
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
        out = adapter.ask_owner('new question', urgency='durable', workspace=self.ws,
                                host='fake-host', store=self.store)
        first = json.loads(self.seen.read_text().splitlines()[0])
        self.assertIn('new question', first, 'the new ask must be held before the first database child')
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
        out = adapter.ask_owner('new question', urgency='durable', workspace=self.ws,
                                host='fake-host', store=self.store)
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


    def test_nested_budget_keeps_parent_deadline_and_resets_after_exception(self):
        clock = [100.0]
        self.client.timeout = 10
        with mock.patch.object(store_mod.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(RuntimeError, 'synthetic'):
                with self.client.operation():
                    clock[0] = 104.0
                    with self.client.operation():
                        self.assertEqual(self.client._deadline, 110.0)
                    self.assertEqual(self.client._deadline, 110.0)
                    raise RuntimeError('synthetic')
            self.assertIsNone(self.client._deadline)
            with self.client.operation():
                self.assertEqual(self.client._deadline, 114.0)

    def test_an_unconfirmed_new_row_keeps_its_record_without_reconciling_backlog(self):
        old = store_mod.Question('ask-old-held', 'older held question')
        store_mod.Outbox(self.ws).save(old, '**Sent:** synthetic', 1)
        with mock.patch.object(self.client, 'rows', return_value=[]), \
                mock.patch.object(self.client, 'add_row', return_value={'created': True}), \
                mock.patch.object(self.client, 'row', return_value=None), \
                mock.patch.object(adapter, 'reconcile_pending') as reconcile:
            out = adapter.ask_owner('unconfirmed new question', urgency='durable',
                                    workspace=self.ws, host='fake-host', store=self.store)
        self.assertEqual(out['db_error'], 'the row was not confirmed complete')
        self.assertTrue(Path(out['outbox']).is_file())
        self.assertEqual(len(store_mod.Outbox(self.ws).entries()), 2)
        self.assertTrue((self.ws / 'results' / out['proactive_file']).is_file())
        reconcile.assert_not_called()

    def test_an_incomplete_host_row_is_not_cached_as_confirmed(self):
        cells = {'ask_id': 'ask-incomplete', 'host': 'fake-host',
                 'recovery': self.store._tag(store_mod.INCOMPLETE)}
        with mock.patch.object(self.client, 'rows', return_value=[]), \
                mock.patch.object(self.client, 'row', return_value={'body': 'question', 'cells': cells}):
            self.assertFalse(self.store.complete('ask-incomplete'))
        self.assertNotIn('ask-incomplete', self.store._keys)


    def test_expired_operation_refuses_before_waiting_for_a_write_lock(self):
        self.store.lock = self.ws / 'state/db.lock'
        with mock.patch.object(store_mod.time, 'monotonic', return_value=2.0), \
                mock.patch.object(store_mod.ledger, 'under_lock') as lock:
            self.client._deadline = 1.0
            with self.assertRaisesRegex(store_mod.StoreError, 'deadline exceeded'):
                self.store.close('ask-expired', 'Answered')
        lock.assert_not_called()

    def test_timeout_error_reports_the_remaining_child_budget(self):
        self.client.timeout = 90
        self.client._deadline = 2.25
        with mock.patch.object(store_mod.time, 'monotonic', return_value=2.0), \
                mock.patch.object(store_mod.subprocess, 'run',
                                  side_effect=store_mod.subprocess.TimeoutExpired('synthetic', 0.25)):
            with self.assertRaisesRegex(store_mod.StoreError, 'no reply within 0.25s'):
                self.client.rows(store_mod.DB_SCHEMA)


class ReconcileBudget(unittest.TestCase):
    setUp = AskBudget.setUp

    def _run_entry(self, entry, backlog):
        self.client.timeout = 0.1
        self.store.lock = self.ws / 'state/db.lock'
        self.child.write_text("import time; time.sleep(0.3)\n")
        for n in range(backlog):
            store_mod.Outbox(self.ws).save(
                store_mod.Question('ask-old-' + str(n), 'old question'), '**Sent:** synthetic', 1)
        store_mod.mark_store_used(self.ws, 'ask-previous')
        real_run = store_mod.subprocess.run
        with mock.patch.object(adapter, 'room_store', return_value=(self.store, 'synthetic')), \
                mock.patch.object(store_mod.subprocess, 'run', wraps=real_run) as children, \
                contextlib.redirect_stdout(io.StringIO()):
            if entry == 'shared':
                out = store_mod.reconcile_pending(self.store, self.ws, 'fake-host')
                self.assertTrue(out['errors'])
            elif entry == 'public':
                self.assertTrue(adapter.reconcile_pass(self.ws)['errors'])
            elif entry == 'cli':
                pq = importlib.import_module('pq')
                self.assertEqual(pq.cmd_reconcile(['--workspace', str(self.ws)]), 1)
            elif entry == 'reminder':
                reminder = importlib.import_module('pending_questions_remind')
                with mock.patch.object(reminder, 'WORKSPACE', self.ws):
                    out = reminder.gather(adapter)
                self.assertTrue(out['unavailable'])
            elif entry == 'resolve':
                ok, message = adapter.resolve(self.ws, 'ask-target', 'Answered')
                self.assertTrue(ok, message)
                self.assertIn('recorded locally', message)
                self.assertIn('ask-target', store_mod.HeldRecords(self.ws).closes())
            else:
                self.fail(entry)
        room_calls = [call for call in children.call_args_list if 'input' in call.kwargs]
        self.assertEqual(len(room_calls), 2 if entry == 'reminder' else 1,
                             'expired operations must not spawn a child for each held question')
        self.assertEqual(len(store_mod.Outbox(self.ws).entries()), backlog)
        self.assertIsNone(self.client._deadline, 'the entry must restore the previous budget')

    def test_shared_reconcile_bounds_zero_and_eight_held_questions(self):
        for n in (0, 8):
            with self.subTest(backlog=n):
                self._run_entry('shared', n)

    def test_public_reconcile_bounds_zero_and_eight_held_questions(self):
        for n in (0, 8):
            with self.subTest(backlog=n):
                self._run_entry('public', n)

    def test_pq_reconcile_bounds_zero_and_eight_held_questions(self):
        for n in (0, 8):
            with self.subTest(backlog=n):
                self._run_entry('cli', n)

    def test_reminder_reconcile_and_read_bound_zero_and_eight_held_questions(self):
        for n in (0, 8):
            with self.subTest(backlog=n):
                self._run_entry('reminder', n)

    def test_owner_resolve_bounds_reconcile_and_close_with_zero_and_eight_held_questions(self):
        for n in (0, 8):
            with self.subTest(backlog=n):
                self._run_entry('resolve', n)


if __name__ == '__main__':
    unittest.main()
