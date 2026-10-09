#!/usr/bin/env python3
"""A terminal outbox item moves its live result aside ONCE and is logged ONCE.

Every outbound pass that finds a result file for an item the outbox has
already decided must not log the refusal again or leave the file where the
next pass rescans it: the drain, the orphan sweep and a fresh process that
re-reads the terminal record from the store all have to converge on one
quarantined copy and one log line.

Run: python3 tests/gateway-terminal-result-moved-once.test.py
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'packages' / 'ag2-sparrow'))
from ag2_sparrow import outbox, remote_gateway_bridge as gw, undelivered_quarantine
from ag2_sparrow.delivery_core import DeliveryCore, DesignAClaimBackend, RetryPolicy, DrainStatus
from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceResultProvider

ROOM = '!same:ag2.space'
TID = 'task-terminal1'
PASSES = 3


class Gateway:
    """A relay that refuses permanently (400) so the first attempt parks the item."""

    def __init__(self):
        self.now = 1000.0
        self.calls = []

    def request(self, method, path, payload):
        self.calls.append(dict(payload))
        raise urllib.error.HTTPError('https://gateway.invalid', 400, 'bad request', None, None)


class TerminalResultMovedOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server = Gateway()
        observer = patch.object(outbox, '_activity_completed', return_value=None)
        observer.start()
        self.addCleanup(observer.stop)
        self.results = self.root / 'results'
        self.tasks = self.root / 'tasks'
        self.results.mkdir()
        self.tasks.mkdir()
        self.outbox = self.results / '.outbox'
        self.lines: list[str] = []

    def core(self):
        backend = DesignAClaimBackend(self.outbox, retry_schedule=outbox.RetrySchedule(),
                                      clock=lambda: self.server.now, republish_delivered=False)
        return DeliveryCore(backend, AG2SpaceResultProvider(self.server.request),
                            RetryPolicy(max_attempts=5, defer_idempotent_resend=True))

    def bridge(self, core=None):
        core = core or self.core()
        values = dict(RESULTS_DIR=self.results, ARCHIVE_RESULTS_DIR=self.results / 'archive',
                      UNDELIVERABLE_RESULTS_DIR=self.results / 'undelivered', TASKS_DIR=self.tasks,
                      _STATE=self.root / 'state', DEDUP_ALIAS_FILE=self.root / 'state' / 'aliases.json',
                      TASK_ROOMS_FILE=self.root / 'state' / 'rooms.json',
                      TASK_MEDIA_FILE=self.root / 'state' / 'media.json',
                      INFLIGHT_FILE=self.root / 'state' / 'inflight.json',
                      GATEWAY_INSTANCE='', _INST_SUFFIX='', _DELIVERY_CORE=core,
                      _req=self.server.request, _log=self.lines.append,
                      _last_orphan_sweep=0.0, _orphan_quarantine_logged=set())
        stack = contextlib.ExitStack()
        for name, value in values.items():
            stack.enter_context(patch.object(gw, name, value))
        self.addCleanup(stack.close)
        return core

    def task(self, tid=TID):
        (self.tasks / f'{tid}.txt').write_text(
            f'id: {tid}\nsource: ag2space\nchannel_id: {ROOM}\nuser_id: owner\n'
            'access_tier: owner\ntask: Same question\n')
        gw._record_task_room(tid, ROOM)

    def result(self, body='Existing answer', tid=TID):
        p = self.results / f'{tid}.txt'
        p.write_text(body)
        return p

    def about(self):
        return [ln for ln in self.lines if TID in ln]

    def quarantined(self):
        return sorted(p.name for p in (self.results / 'undelivered').glob(f'{TID}*'))

    def assert_once(self, result, where):
        self.assertFalse(result.exists(), f'{where}: live result still rescannable')
        self.assertEqual(len(self.quarantined()), 1, f'{where}: {self.quarantined()}')
        self.assertEqual(len(self.about()), 1, f'{where}: logged {len(self.about())}x\n'
                         + '\n'.join(self.about()))
        self.assertIn('terminal', self.about()[0])

    def test_live_drain_moves_and_logs_once(self):
        self.bridge()
        self.task()
        result = self.result()
        inflight = {TID}
        for _ in range(PASSES):
            gw._post_ready_results(inflight)
        self.assertEqual(len(self.server.calls), 1, 'a parked item must not be re-POSTed')
        self.assert_once(result, 'live drain')
        self.assertTrue(self.core().backend.is_terminal(TID))

    def test_fresh_process_reading_a_terminal_record_moves_and_logs_once(self):
        # The record went terminal under an earlier process; this one starts
        # with the live file still present and the decision already on disk.
        first = self.core()
        payload = b'{"id": "%s", "body": "Existing answer"}' % TID.encode()
        first.backend.publish(TID, payload)
        first.deliver_one(TID, payload)
        self.assertTrue(first.backend.is_terminal(TID))
        self.bridge(self.core())
        self.task()
        result = self.result()
        inflight = {TID}
        for _ in range(PASSES):
            gw._post_ready_results(inflight)
        self.assertEqual(len(self.server.calls), 1, 'the stored decision must not be re-tried')
        self.assert_once(result, 'fresh process')

    def test_lease_close_marker_moves_and_logs_once(self):
        self.bridge()
        self.task()
        result = self.result('[no-send]\ninternal, nothing to say')
        inflight = {TID}
        for _ in range(PASSES):
            gw._post_ready_results(inflight)
        self.assert_once(result, 'lease close')

    def test_competing_observers_dispose_once(self):
        # The parking pass pauses after its record reads PARKED while a second
        # observer claims TERMINAL and quarantines first; the loser stays quiet.
        import threading
        self.bridge()
        self.task()
        result = self.result()
        payload = f'{{"id": "{TID}", "body": "Existing answer"}}'
        parked = threading.Event()
        released = threading.Event()
        first = threading.current_thread()
        real_read = gw.read_item

        def paused_read(root, item_id):
            rec = real_read(root, item_id)
            if (threading.current_thread() is not first and not parked.is_set()
                    and (rec or {}).get('status') == 'PARKED'):
                parked.set()
                released.wait(5)
            return rec

        with patch.object(gw, 'read_item', paused_read):
            loser = threading.Thread(target=gw._deliver_result_payload,
                                     args=(TID, TID, 'Existing answer'),
                                     kwargs=dict(result_file=result))
            loser.start()
            self.assertTrue(parked.wait(5), 'the first observer never parked the item')
            gw._deliver_result_payload(TID, TID, 'Existing answer', result_file=result)
            released.set()
            loser.join(5)
        self.assertFalse(loser.is_alive())
        self.assertEqual(len(self.server.calls), 1)
        self.assert_once(result, 'competing observers')
        self.assertFalse(any('vanished' in l or 'leaving it in place' in l for l in self.about()),
                         '\n'.join(self.about()))

    def test_lease_close_retry_is_not_silenced_by_a_delivered_base_item(self):
        # A [no-send] close for a delivered item is its own record: while it
        # retries, the sweep must say so instead of reading the base as terminal.
        self.server.request = lambda method, path, payload: {'ok': True}
        core = self.core()
        payload = b'{"id": "%s", "body": "Existing answer"}' % TID.encode()
        core.backend.publish(TID, payload)
        core.deliver_one(TID, payload)
        self.assertEqual(outbox.read_item(core.backend.root, TID)['status'], 'DELIVERED')
        self.bridge(core)
        self.task()
        result = self.result('[no-send]\ninternal, nothing to say')
        import os
        old = self.server.now
        os.utime(result, (old, old))

        def flaky(method, path, payload):
            self.server.calls.append(dict(payload))
            raise urllib.error.HTTPError('https://gateway.invalid', 503, 'busy', None, None)
        with patch.object(core.provider, '_request', flaky), patch.object(gw, '_req', flaky), \
                patch.object(gw.time, 'time', lambda: old + gw.ORPHAN_GRACE_S + 60):
            gw._last_orphan_sweep = 0.0
            gw._reconcile_orphan_results(set())
        self.assertTrue(result.exists(), 'a retryable close must keep its result')
        self.assertFalse(core.backend.is_terminal(f'{TID}.lease-close'))
        self.assertTrue(any('will retry' in l for l in self.about()),
                        'sweep stayed quiet for a retryable lease-close:\n' + '\n'.join(self.about()))

    def test_orphan_sweep_moves_and_logs_once(self):
        self.bridge()
        self.task()
        result = self.result()
        import os
        old = self.server.now
        os.utime(result, (old, old))  # past the sweep's grace, inside its max age
        with patch.object(gw.time, 'time', lambda: old + gw.ORPHAN_GRACE_S + 60):
            for _ in range(PASSES):
                gw._last_orphan_sweep = 0.0
                gw._reconcile_orphan_results(set())
        self.assertEqual(len(self.server.calls), 1, 'a parked item must not be re-POSTed')
        self.assert_once(result, 'orphan sweep')


if __name__ == '__main__':
    unittest.main(verbosity=2)
