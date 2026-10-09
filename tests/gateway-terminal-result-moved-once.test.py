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
import inspect
import sys
import tempfile
import time
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

    # ---- competing observers: every schedule runs in the main thread so the
    # coverage gate (multiprocessing tracer, no thread tracing) sees the lines.

    def read(self, path):
        """The body and its identity, the way the drain reads them."""
        if hasattr(gw, '_read_ready_generation'):
            return gw._read_ready_generation(path)
        return gw.read_ready_result(path), None

    def deliver(self, body, path, generation):
        kw = dict(result_file=path)
        if 'generation' in inspect.signature(gw._deliver_result_payload).parameters:
            kw['generation'] = generation
        return gw._deliver_result_payload(TID, TID, body, **kw)

    def interleave(self, at_parked):
        """Run the drain as observer A and call `at_parked()` the first time A
        reads its record as PARKED, i.e. after it persisted the park and
        before it disposes: the window a second observer slips into."""
        real_read = gw.read_item
        fired = []

        def paused_read(root, item_id):
            rec = real_read(root, item_id)
            if not fired and (rec or {}).get('status') == 'PARKED':
                fired.append(True)
                with patch.object(gw, 'read_item', real_read):
                    at_parked()
            return rec
        with patch.object(gw, 'read_item', paused_read):
            gw._post_ready_results({TID})
        self.assertTrue(fired, 'observer A never parked the item')

    def test_competing_observers_dispose_once(self):
        # Observer B claims TERMINAL and quarantines while A is paused after
        # its park; A then finds the bytes it read already in quarantine.
        self.bridge()
        self.task()
        result = self.result()
        body, gen = self.read(result)
        self.interleave(lambda: self.deliver(body, result, gen))
        self.assertEqual(len(self.server.calls), 1)
        self.assert_once(result, 'competing observers')
        self.assertFalse(any('vanished' in l or 'leaving it in place' in l for l in self.about()),
                         '\n'.join(self.about()))

    def test_a_newer_reply_landing_after_the_winner_is_preserved(self):
        # Same race, but a producer publishes a newer reply at the canonical
        # path after B's quarantine: A must not dispose of a file it never read.
        self.bridge()
        self.task()
        result = self.result('OLD BODY')
        body, gen = self.read(result)

        def b_then_producer():
            self.deliver(body, result, gen)
            self.result('NEWER BODY')
        self.interleave(b_then_producer)
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(result.read_text(), 'NEWER BODY', 'the newer reply must stay live')
        copies = self.quarantined()
        self.assertEqual(len(copies), 1, copies)
        self.assertEqual((self.results / 'undelivered' / copies[0]).read_text(), 'OLD BODY')
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))

    def test_a_late_loser_that_read_the_old_body_stays_quiet(self):
        # Both observers read the old body; the winner finishes before the
        # loser even enters delivery, so its copy predates the loser's attempt.
        self.bridge()
        self.task()
        result = self.result()
        body, gen = self.read(result)
        self.deliver(body, result, gen)           # winner: parks and quarantines
        self.assertFalse(result.exists())
        self.deliver(body, result, gen)           # loser, with the same bytes in hand
        self.assertEqual(len(self.server.calls), 1)
        self.assert_once(result, 'late loser')

    def park_without_disposing(self):
        """A terminal record whose live file no observer has disposed of yet."""
        core = self.core()
        payload = b'{"id": "%s", "body": "Existing answer"}' % TID.encode()
        core.backend.publish(TID, payload)
        core.deliver_one(TID, payload)
        self.assertTrue(core.backend.is_terminal(TID))
        return core

    def test_an_older_copy_of_the_same_bytes_silences_the_loser(self):
        # The winner named its copy before the loser started; wall-clock order
        # says nothing, the bytes do.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        body, gen = self.read(result)
        undelivered_quarantine.quarantine(result, self.results, when=1)   # ancient epoch
        self.deliver(body, result, gen)
        self.assertEqual(len(self.quarantined()), 1)
        self.assertEqual(self.about(), [], '\n'.join(self.about()))

    def test_a_copy_still_held_in_a_claim_silences_the_loser(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        body, gen = self.read(result)
        result.rename(self.results / f'.{TID}.disposing-999-deadbeef')   # the winner, mid-move
        self.deliver(body, result, gen)
        self.assertEqual(self.about(), [], '\n'.join(self.about()))

    def test_an_unrelated_copy_with_a_later_stamp_does_not_hide_a_lost_reply(self):
        # A quarantined OTHER body carries a timestamp after this attempt; the
        # reply this pass read is gone and must be reported, not assumed moved.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        other = self.result('OTHER BODY')
        undelivered_quarantine.quarantine(other, self.results, when=time.time_ns() + 10 ** 12)
        result = self.result('NEW BODY')
        body, gen = self.read(result)
        result.unlink()                           # lost before disposal
        self.deliver(body, result, gen)
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('vanished', self.about()[0])
        self.assertIn('no quarantined copy', self.about()[0])

    def test_a_vanished_reply_with_no_copy_at_all_is_reported(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        body, gen = self.read(result)
        result.unlink()
        self.deliver(body, result, gen)
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('vanished', self.about()[0])
        self.assertEqual(self.quarantined(), [])

    def test_a_newer_reply_superseded_during_the_put_back_is_kept(self):
        # A holds NEWER in its claim when NEWEST lands at the canonical path:
        # NEWER cannot go back, so it is kept in quarantine, named once.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result('OLD BODY')
        body, gen = self.read(result)
        result.unlink()
        self.result('NEWER BODY')
        import os
        real_link = os.link

        def link_after_producer(src, dst, *a, **kw):
            self.result('NEWEST BODY')
            return real_link(src, dst, *a, **kw)
        with patch.object(os, 'link', link_after_producer):
            self.deliver(body, result, gen)
        self.assertEqual(result.read_text(), 'NEWEST BODY')
        bodies = sorted((self.results / 'undelivered' / n).read_text() for n in self.quarantined())
        self.assertEqual(bodies, ['NEWER BODY'])
        self.assertEqual(len(self.about()), 2, '\n'.join(self.about()))
        self.assertTrue(any('superseded' in l for l in self.about()))
        self.assertTrue(any('vanished' in l for l in self.about()))

    def test_an_unreadable_copy_is_skipped_while_matching(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        body, gen = self.read(result)
        q = self.results / 'undelivered'
        q.mkdir()
        (q / f'{TID}-5.txt').symlink_to(q / 'gone.txt')     # listed, not readable
        undelivered_quarantine.quarantine(result, self.results, when=7)
        self.deliver(body, result, gen)
        self.assertEqual(self.about(), [], '\n'.join(self.about()))

    def test_a_refused_attribution_quarantines_the_generation_it_read(self):
        self.bridge()
        self.task()
        result = self.result()
        body, gen = self.read(result)
        with patch.object(gw, '_attribution', return_value=('', True)):
            self.assertFalse(self.deliver(body, result, gen))
        self.assertEqual(self.server.calls, [])
        self.assertEqual(len(self.quarantined()), 1)
        self.assertIn('attribution refused', self.about()[0])

    def test_a_caller_without_an_identity_still_moves_the_file(self):
        # Legacy shape: nothing captured at read time, so the path is moved as is.
        self.bridge()
        result = self.result()
        gw._quarantine_undelivered(result, TID, 'refused')
        self.assertFalse(result.exists())
        self.assertEqual(len(self.quarantined()), 1)

    def test_a_reply_rewritten_in_place_is_a_new_generation(self):
        # Same inode, new bytes: still not the file this pass read.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result('OLD BODY')
        body, gen = self.read(result)
        with result.open('r+') as f:
            f.seek(0); f.write('NEW BODY'); f.truncate()
        self.deliver(body, result, gen)
        self.assertEqual(result.read_text(), 'NEW BODY')
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(len(self.about()), 1)
        self.assertIn('vanished', self.about()[0])

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
