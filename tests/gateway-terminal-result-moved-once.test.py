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
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'packages' / 'ag2-sparrow'))
from ag2_sparrow import outbox, remote_gateway_bridge as gw, undelivered_quarantine


def _park_in_quarantine(rfile, results, when=None):
    """Fixture: put a result where the quarantine reader lists it."""
    return undelivered_quarantine.place(Path(rfile), results, Path(rfile).stem, when=when)
try:
    # The canonical module, so the coverage gate (source = src) sees the
    # lifecycle run under the bridge; the vendored copy is pinned byte-equal.
    sys.path.insert(0, str(REPO / 'src'))
    from delivery import disposal
except ImportError:                                   # a head before the lifecycle owner existed
    class _Legacy:                                    # the pre-round-5 names, so the file runs there
        def __getattr__(self, name):
            return getattr(gw, {'identity_of': 'identity_of', 'self_token': '_self_token',
                                'CLAIM_MAX_S': 'DISPOSING_CLAIM_MAX_S', 'put_back': '_put_back'}[name])
    disposal = _Legacy()
from ag2_sparrow.delivery_core import DeliveryCore, DesignAClaimBackend, RetryPolicy, DrainStatus
from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceResultProvider

ROOM = '!same:ag2.space'
UNSENT = 'a different reply was published after this id was delivered'
TID = 'task-terminal1'
PASSES = 3


class Gateway:
    """A relay that refuses permanently (400) so the first attempt parks the item."""

    def __init__(self):
        self.now = 1000.0
        self.calls = []

    accepting = False

    def request(self, method, path, payload, **_kw):
        self.calls.append(dict(payload))
        if self.accepting:
            return {'ok': True}
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
                      _last_orphan_sweep=0.0, _orphan_quarantine_logged=set(),
                      _WITHHELD_TASK_OUTPUT={})
        if hasattr(gw, 'disposal'):
            values['disposal'] = disposal
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

    def quarantined_bodies(self):
        return sorted(p.read_text() for p in (self.results / 'undelivered').glob('*.txt'))

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
        _park_in_quarantine(result, self.results, when=1)   # ancient epoch
        self.deliver(body, result, gen)
        self.assertEqual(len(self.quarantined()), 1)
        self.assertEqual(self.about(), [], '\n'.join(self.about()))

    def claim_name(self, pid, start, nonce='deadbeef', acquired=None, restore=False,
                   body='Existing answer', ino=None, mtime=None):
        # When the live result holds `body` that file IS the generation; otherwise
        # the name describes another publication and the claim's body is unverified.
        acquired = int(time.time() if acquired is None else acquired)
        digest = hashlib.sha256(body.encode()).hexdigest()
        live = self.results / f'{TID}.txt'
        if ino is None or mtime is None:
            # The generation is the file holding `body`: the live result, or a
            # claim/quarantined copy it was already moved to.
            same = False
            for cand in [live] + sorted(live.parent.glob(f'.{live.stem}.disposing-*')) \
                    + sorted((live.parent / 'undelivered').glob(f'{live.stem}-*.txt')):
                try:
                    if cand.read_bytes() == body.encode():
                        st = os.stat(cand)
                        same = True
                        break
                except OSError:
                    continue
            ino = ino if ino is not None else (st.st_ino if same else 0)
            mtime = mtime if mtime is not None else (st.st_mtime_ns if same else 0)
        return self.results / (f'.{TID}.disposing-{pid}-{start}-{acquired}-{ino}-{mtime}-{digest}-{nonce}'
                               + ('.restore' if restore else ''))

    def live_other_owner(self):
        """A process that is alive and is not this one: the parent."""
        ident = outbox.process_identity(os.getppid())
        self.assertIs(ident.state, outbox.OwnerState.ALIVE)
        return os.getppid(), int(ident.start_usec or 0)

    def test_a_copy_still_held_in_a_live_owners_claim_silences_the_loser(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        body, gen = self.read(result)
        pid, start = self.live_other_owner()
        claim = self.claim_name(pid, start)
        result.rename(claim)                             # the winner, mid-move
        self.deliver(body, result, gen)
        self.assertEqual(self.about(), [], '\n'.join(self.about()))
        self.assertTrue(claim.exists(), 'a live owner must keep its claim')
        self.assertEqual(self.quarantined(), [])

    # ---- crash safety of the private claim

    def die_after_claim_rename(self):
        """Kewei's recipe: the owner dies right after the first rename (its
        verification of the claimed file is the first thing after it)."""
        def dying(fd, generation):
            raise SystemExit('owner died mid-disposal')
        return patch.object(disposal, '_verify_fd', dying)

    def claims(self):
        return sorted(p.name for p in self.results.glob(f'.{TID}.disposing-*'))

    def test_a_death_after_the_claim_rename_is_recovered_by_the_next_passes(self):
        self.bridge()
        self.task()
        result = self.result()
        body, gen = self.read(result)
        with self.die_after_claim_rename():
            with self.assertRaises(SystemExit):
                self.deliver(body, result, gen)
        self.assertFalse(result.exists())
        self.assertEqual(len(self.claims()), 1, 'the body sits at the claim path')
        self.assertEqual(self.quarantined(), [])
        for _ in range(PASSES):
            gw._post_ready_results({TID})
            gw._last_orphan_sweep = 0.0
            gw._reconcile_orphan_results({TID})
        self.assertEqual(self.claims(), [], 'the claim must not be stranded')
        self.assertEqual(len(self.quarantined()), 1, self.quarantined())
        self.assertEqual((self.results / 'undelivered' / self.quarantined()[0]).read_text(),
                         'Existing answer')
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('recovered', self.about()[0])
        self.assertEqual(len(self.server.calls), 1)

    def test_a_death_mid_disposal_keeps_a_distinct_publication_of_the_same_bytes(self):
        # Equal bytes under another inode are another reply: both stay visible.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        body, gen = self.read(result)
        pid, start = disposal.self_token()
        claim = self.claim_name(pid, start, 'aba0d001')      # ours, but not active
        result.rename(claim)
        _park_in_quarantine(self.result(), self.results)
        gw._post_ready_results({TID})
        self.assertEqual(self.claims(), [])
        self.assertEqual(len(self.quarantined()), 2, 'a distinct publication is never deleted')
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('recovered', self.about()[0])

    def test_a_death_mid_disposal_of_the_very_file_already_quarantined_keeps_both_names(self):
        # The same inode under two names (an interrupted move): the second name
        # is kept where the operator looks, never unlinked after a check.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        pid, start = disposal.self_token()
        claim = self.claim_name(pid, start, 'aba0d002')
        os.link(result, claim)
        _park_in_quarantine(result, self.results)
        gw._post_ready_results({TID})
        self.assertEqual(self.claims(), [])
        copies = [self.results / 'undelivered' / n for n in self.quarantined()]
        self.assertEqual(len({os.stat(c).st_ino for c in copies}), 1, 'one body, every name visible')
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('second name', self.about()[0])

    def test_a_quarantine_setup_failure_after_the_claim_rename_puts_the_body_back(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        (self.results / 'undelivered').write_text('not a directory')
        result = self.result()
        body, gen = self.read(result)
        self.deliver(body, result, gen)                  # winner: setup fails
        self.assertTrue(result.exists(), 'the body must be back at its canonical name')
        self.assertEqual(result.read_text(), 'Existing answer')
        self.assertEqual(self.claims(), [], 'no stranded claim')
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('leaving it in place', self.about()[0])
        self.deliver(body, result, gen)                  # loser: not silent either
        self.assertEqual(len(self.about()), 2, '\n'.join(self.about()))
        self.assertTrue(result.exists())

    def test_a_setup_failure_with_the_canonical_name_retaken_keeps_the_body(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result('OLD BODY')
        body, gen = self.read(result)
        real_mkdir = Path.mkdir
        failed = []

        def retake_then_fail(path, *a, **kw):
            if path.name == 'undelivered' and not failed:
                failed.append(True)
                self.result('NEWER BODY')                # a producer reuses the name
                raise PermissionError(13, 'no quarantine dir')
            return real_mkdir(path, *a, **kw)
        with patch.object(Path, 'mkdir', retake_then_fail):
            self.deliver(body, result, gen)
        self.assertEqual(result.read_text(), 'NEWER BODY')
        self.assertEqual(self.claims(), [], 'no stranded claim')
        self.assertEqual(len(self.quarantined()), 1, self.quarantined())
        self.assertEqual((self.results / 'undelivered' / self.quarantined()[0]).read_text(),
                         'OLD BODY')
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))

    def test_a_stale_claim_under_a_reused_pid_is_recovered(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        pid, start = disposal.self_token()
        claim = self.claim_name(pid, start + 7, 'ce05ed01')  # same pid, another birth
        result.rename(claim)
        gw._post_ready_results({TID})
        self.assertEqual(self.claims(), [])
        self.assertEqual(len(self.quarantined()), 1, self.quarantined())
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('recovered', self.about()[0])

    def test_another_live_pid_with_a_different_birth_is_a_reused_pid(self):
        # The pid is alive but was born at another time: not the claimant.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        pid, start = self.live_other_owner()
        result.rename(self.claim_name(pid, start + 7, 'ce05ed02'))
        gw._post_ready_results({TID})
        self.assertEqual(self.claims(), [])
        self.assertEqual(len(self.quarantined()), 1, self.quarantined())
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))

    # ---- the sweep's defensive edges

    def dead_pid(self):
        dead = subprocess.Popen(['true']); dead.wait()
        return dead.pid

    def test_a_claim_of_an_unknown_shape_is_never_touched(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        odd = self.results / f'.{TID}.disposing-123-1-abcd'    # an older, three-field name
        self.result().rename(odd)
        for _ in range(PASSES):
            gw._post_ready_results({TID})
        self.assertTrue(odd.exists())
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('unknown shape', self.about()[0])

    def test_a_claim_gone_between_listing_and_stat_is_left_alone(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        os.symlink('nowhere', self.claim_name(self.dead_pid(), 1, 'da0611e0'))
        gw._post_ready_results({TID})                   # stat fails: treated as held
        self.assertEqual(self.about(), [])

    def test_an_unreadable_claim_is_skipped_by_recovery_and_by_the_loser(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        body, gen = self.read(result)
        self.claim_name(self.dead_pid(), 1, 'd1ec0000').mkdir()   # a claim nobody can read
        result.unlink()
        gw._post_ready_results({TID})                   # recovery: nothing it can move
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(self.about(), [])
        self.deliver(body, result, gen)                  # loser: skips it, reports the loss
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('vanished', self.about()[0])

    def test_a_dead_owners_claim_with_other_bytes_does_not_silence_the_loser(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        body, gen = self.read(result)
        self.result('OTHER BODY').rename(self.claim_name(self.dead_pid(), 1, '07e40000'))
        self.deliver(body, result, gen)                  # its own bytes are gone
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('vanished', self.about()[0])

    def test_recovery_skips_an_unreadable_quarantined_copy(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        (self.results / 'undelivered').mkdir()
        (self.results / 'undelivered' / undelivered_quarantine.quarantine_name(TID, 5)).mkdir()
        self.result().rename(self.claim_name(self.dead_pid(), 1, 'c0de0001'))
        gw._post_ready_results({TID})
        self.assertEqual(self.claims(), [])
        self.assertEqual(len(self.quarantined()), 2)      # the dir and the recovered body
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))

    def test_recovery_losing_the_final_rename_to_another_observer_is_quiet(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        self.result()
        claim = self.claim_name(self.dead_pid(), 1, 'ace00001')
        (self.results / f'{TID}.txt').rename(claim)
        real_move = disposal._move_into_quarantine

        def taken_first(src, dst, log):
            if Path(src) == claim:
                os.rename(src, self.results / 'elsewhere.txt')    # the other observer
                raise FileNotFoundError(2, 'gone', str(src))
            return real_move(src, dst, log)
        with patch.object(disposal, '_move_into_quarantine', taken_first):
            gw._post_ready_results({TID})
        self.assertEqual(self.about(), [])
        self.assertEqual(self.quarantined(), [])

    def test_recovery_survives_an_unlistable_results_dir(self):
        self.bridge(self.park_without_disposing())
        with patch.object(Path, 'glob', side_effect=PermissionError(13, 'no listing')):
            gw._recover_disposing_claims()
        self.assertEqual(self.about(), [])

    # ---- round 5: acquisition time, isolation, put-back duplicates, identity

    def test_a_live_owners_fresh_claim_on_an_old_reply_is_kept(self):
        # A rename keeps the reply's mtime; the claim is young all the same.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        old = time.time() - 700
        os.utime(result, (old, old))
        pid, start = self.live_other_owner()
        claim = self.claim_name(pid, start, '01d00001')
        result.rename(claim)
        for _ in range(PASSES):
            gw._post_ready_results({TID})
            gw._last_orphan_sweep = 0.0
            gw._reconcile_orphan_results({TID})
        self.assertTrue(claim.exists(), 'a live owner keeps the claim it just made')
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(self.about(), [], '\n'.join(self.about()))

    def test_a_live_owner_putting_back_a_newer_reply_is_not_robbed(self):
        # The owner found NEWER under its claim and recorded the restore intent;
        # a sweep in that window must leave it, and the owner then restores it.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result('NEWER BODY')
        old = time.time() - 700
        os.utime(result, (old, old))
        pid, start = self.live_other_owner()
        claim = self.claim_name(pid, start, '01d00002', restore=True)
        result.rename(claim)
        gw._post_ready_results({TID})
        gw._last_orphan_sweep = 0.0
        gw._reconcile_orphan_results({TID})
        self.assertTrue(claim.exists())
        self.assertEqual(self.quarantined(), [])
        self.assertTrue(disposal.put_back(claim, result))      # the owner finishes
        self.assertEqual(result.read_text(), 'NEWER BODY')
        self.assertEqual(self.about(), [], '\n'.join(self.about()))

    def test_an_abandoned_restore_intent_is_finished_not_quarantined(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result('NEWER BODY')
        result.rename(self.claim_name(self.dead_pid(), 1, '01d00003', restore=True))
        gw._last_orphan_sweep = 0.0
        gw._reconcile_orphan_results(set())             # the sweep, not a delivering drain
        self.assertEqual(result.read_text(), 'NEWER BODY', 'the newer reply goes back live')
        self.assertEqual(self.claims(), [])
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('restored', self.about()[0])

    def test_a_crash_between_the_put_back_link_and_unlink_strands_nothing_and_unlinks_nothing(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        claim = self.claim_name(self.dead_pid(), 1, '01d00004', restore=True)
        os.link(result, claim)                            # died after the link
        for _ in range(PASSES):
            gw._post_ready_results({TID})
        self.assertEqual(self.claims(), [])
        copies = [self.results / 'undelivered' / n for n in self.quarantined()]
        self.assertEqual(len({os.stat(c).st_ino for c in copies}), 1, 'one body, every name visible')
        self.assertEqual(len(self.about()), 2, '\n'.join(self.about()))
        self.assertTrue(any('second name' in l for l in self.about()))
        self.assertTrue(any('terminal' in l for l in self.about()))

    def test_without_a_no_replace_rename_the_printed_requeue_still_delivers(self):
        # A host with no RENAME_NOREPLACE: the reply parked by a refusal must come
        # back through the requeue the bridge prints, and then be delivered.
        sys.path.insert(0, str(REPO / 'src'))
        import outbox_cli
        mods = {id(m): m for m in (undelivered_quarantine, getattr(disposal, 'undelivered_quarantine', None),
                                   outbox_cli.undelivered_quarantine) if m is not None}
        stack = contextlib.ExitStack()
        for m in mods.values():
            stack.enter_context(patch.object(m, 'RENAME_PRIMITIVE', 'none'))
            stack.enter_context(patch.object(m, '_RENAME', None))
        self.addCleanup(stack.close)
        self.bridge()
        self.task()
        result = self.result('the parked answer')
        gw._post_ready_results({TID})
        self.assertFalse(result.exists())
        self.assertEqual(len(self.quarantined()), 1)

        def accept(method, path, payload):
            self.server.calls.append(dict(payload))
            return {'ok': True}
        self.server.request = accept
        rc = outbox_cli.main(['--root', str(self.outbox), 'requeue', TID, '--reset-attempts',
                              '--results-dir', str(self.results), '--body-id', TID])
        self.assertEqual(rc, 0)
        self.assertTrue(result.exists(), 'the requeue did not put the body back')
        self.assertEqual(self.quarantined(), [], 'the restored body is still where the drain cannot see it')
        gw._post_ready_results({TID})
        self.assertEqual([c.get('body') for c in self.server.calls][-1:], ['the parked answer'])
        self.assertFalse(result.exists(), 'a delivered result stays rescannable')

    def _parked_on_a_host_that_cannot_restore(self):
        """A refusal parks the item and quarantines its body, on a host with
        neither a no-replace rename nor hard links, so a requeue hits NO_SAFE_MOVE."""
        sys.path.insert(0, str(REPO / 'src'))
        import outbox_cli
        mods = {id(m): m for m in (undelivered_quarantine, getattr(disposal, 'undelivered_quarantine', None),
                                   outbox_cli.undelivered_quarantine) if m is not None}
        stack = contextlib.ExitStack()
        for m in mods.values():
            stack.enter_context(patch.object(m, 'RENAME_PRIMITIVE', 'none'))
            stack.enter_context(patch.object(m, '_RENAME', None))
        self.addCleanup(stack.close)
        self.bridge()
        self.task()
        result = self.result('BODY-A parked answer')
        gw._post_ready_results({TID})
        self.assertEqual(outbox.item_status(self.outbox, TID), 'PARKED')
        return outbox_cli, result

    def _requeue_with(self, outbox_cli, after_restore):
        real = outbox_cli.undelivered_quarantine.restore
        out = []

        def restore_then(results_dir, body_id):
            got = real(results_dir, body_id)
            self.assertIs(got[0], outbox_cli.undelivered_quarantine.RestoreOutcome.NO_SAFE_MOVE)
            after_restore()
            return got
        with patch.object(outbox_cli.undelivered_quarantine, 'restore', restore_then), \
                patch('os.link', side_effect=PermissionError(1, 'no hard links')), \
                patch.object(outbox_cli, '_emit', lambda rec, as_json, **k: out.append(dict(rec))):
            rc = outbox_cli.main(['--root', str(self.outbox), 'requeue', TID, '--reset-attempts',
                                  '--results-dir', str(self.results), '--body-id', TID])
        return rc, out[-1]

    def test_a_failed_restore_never_overwrites_a_peers_delivered(self):
        outbox_cli, _ = self._parked_on_a_host_that_cannot_restore()
        rc, out = self._requeue_with(outbox_cli, lambda: outbox.record_delivered(self.outbox, TID))
        self.assertEqual(rc, 4)
        self.assertEqual(outbox.item_status(self.outbox, TID), 'DELIVERED', 'the peer\'s transition was overwritten')
        self.assertEqual(out['result'], 'requeued')
        self.assertEqual(self.quarantined_bodies(), ['BODY-A parked answer'])

    def _publish_b(self, result):
        tmp = result.with_name('.producer.tmp')
        tmp.write_text('BODY-B newer reply')
        os.replace(tmp, result)

    def _drain_once_accepting(self):
        """One real drain pass against a relay that now accepts."""
        before = len(self.server.calls)
        self.server.accepting = True
        gw._post_ready_results({TID})
        return self.server.calls[before:]

    def _assert_a_sent_and_b_kept_visible(self, result):
        """A requeue sends the stored body: A goes out; B, a different reply at an id
        now delivered, is quarantined with its cause, never archived or lost."""
        sent = self._drain_once_accepting()
        archived = sorted(p.read_text() for p in (self.results / 'archive').rglob('*.txt'))
        self.assertEqual(
            {'provider_bodies': [c.get('body') for c in sent],
             'status': outbox.item_status(self.outbox, TID), 'live': result.exists(),
             'undelivered': self.quarantined_bodies(), 'b_archived': 'BODY-B newer reply' in archived},
            {'provider_bodies': ['BODY-A parked answer'], 'status': 'DELIVERED', 'live': False,
             'undelivered': ['BODY-A parked answer', 'BODY-B newer reply'], 'b_archived': False})
        said = [l for l in self.lines if TID in l and UNSENT in l]
        self.assertEqual(len(said), 1, '\n'.join(self.lines))
        self._assert_review_line(said[0], delivered='BODY-A parked answer', this='BODY-B newer reply')

    def _assert_review_line(self, line, delivered=None, this=None):
        """The one operator line for a reply at a delivered id: review it, never send it,
        with references to the delivered wire body and this reply."""
        self.assertIn('review it; it may already have been sent', line)
        self.assertNotIn('by hand', line)
        self.assertIn('delivered wire body ', line)
        for text in (delivered, this):
            if text is not None:
                self.assertIn(hashlib.sha256(text.encode()).hexdigest()[:12], line)

    def test_a_reply_published_before_the_failed_restore_returns_is_kept_visible(self):
        outbox_cli, result = self._parked_on_a_host_that_cannot_restore()
        rc, out = self._requeue_with(outbox_cli, lambda: self._publish_b(result))
        self.assertEqual(rc, 4)
        self.assertEqual(outbox.item_status(self.outbox, TID), 'QUEUED', 'B was parked unsent')
        self.assertEqual(result.read_text(), 'BODY-B newer reply')
        self.assertEqual(self.quarantined_bodies(), ['BODY-A parked answer'])
        self._assert_a_sent_and_b_kept_visible(result)

    def test_a_reply_published_after_the_live_check_is_never_parked(self):
        """B lands just before any PARKED write the requeue makes (between a
        live-result check and its write); with no such write, after it returns."""
        outbox_cli, result = self._parked_on_a_host_that_cannot_restore()
        real_write, fired = outbox_cli.outbox._write_item, []

        def write(root, item_id, d):
            if d.get('status') == 'PARKED' and not fired:
                fired.append(1)
                self._publish_b(result)
            return real_write(root, item_id, d)
        with patch.object(outbox_cli.outbox, '_write_item', write):
            rc, out = self._requeue_with(outbox_cli, lambda: None)
        if not fired:
            self._publish_b(result)
        self.assertEqual(rc, 4)
        self.assertEqual(outbox.item_status(self.outbox, TID), 'QUEUED', 'B was parked unsent')
        self._assert_a_sent_and_b_kept_visible(result)

    def _requeue_as_main_writes_it(self):
        """The record exactly as main's requeue_item writes it: no newer marker."""
        d = dict(outbox._read_item(self.outbox, TID))
        outbox._release_locked(self.outbox, TID, force=True)
        d.update(resend_epoch=int(d.get('resend_epoch', 0) or 0) + 1, status='QUEUED', reason=None,
                 attempts=0, requeued_at=time.time(), requeued_by='old-cli', requeue_reason='')
        d.pop('retry', None)
        outbox._write_item(self.outbox, TID, d)

    def _legacy_epoch_used_then_c(self):
        """main requeues A, the gateway takes A but the response is lost; after
        the upgrade C is published and the relay dedupes on the id."""
        relay = [self.server.request]
        backend = DesignAClaimBackend(self.outbox, retry_schedule=outbox.RetrySchedule(),
                                      clock=lambda: self.server.now, republish_delivered=False)
        self.bridge(DeliveryCore(backend, AG2SpaceResultProvider(lambda *a: relay[0](*a)),
                                 RetryPolicy(max_attempts=5, defer_idempotent_resend=True)))
        self.task()
        result = self.result('BODY-A parked answer')
        gw._post_ready_results({TID})                      # 400: parked, A quarantined
        self._requeue_as_main_writes_it()
        result.write_text('BODY-A parked answer')          # main's restore put A back
        held = []

        def take_then_lose_the_response(method, path, payload):
            self.server.calls.append(dict(payload))
            held.append(payload.get('body'))
            raise TimeoutError('response lost after send')
        relay[0] = take_then_lose_the_response
        self.server.now += 3600
        gw._post_ready_results({TID})
        rec = outbox.read_item(self.outbox, TID) or {}
        self.assertEqual((rec.get('status'), rec.get('resend_epoch'), rec.get('attempts'),
                          (rec.get('retry') or {}).get('failures')),
                         ('QUEUED', 1, 1, 1), 'not the persisted shape main leaves')
        self._publish_c(result)

        def dedupe(method, path, payload):
            self.server.calls.append(dict(payload))
            return {'ok': True, 'duplicate': True}       # it already holds A for this id
        relay[0] = dedupe
        self.server.now += 3600
        return result, held

    def test_a_legacy_epoch_already_used_never_carries_a_new_body(self):
        """C must not be recorded DELIVERED under A's epoch, nor archived as sent."""
        self._assert_c_never_rides_a_used_epoch(*self._legacy_epoch_used_then_c())

    def _assert_c_never_rides_a_used_epoch(self, result, held):
        before = len(self.server.calls)
        gw._post_ready_results({TID})
        rec = outbox.read_item(self.outbox, TID) or {}
        archived = sorted(p.read_text() for p in (self.results / 'archive').rglob('*.txt'))
        self.assertEqual(
            {'relay_holds': held, 'sent': [c.get('body') for c in self.server.calls[before:]],
             'status': rec.get('status'), 'recorded_body': json.loads(rec.get('payload', '{}')).get('body'),
             'c_live': result.exists(), 'c_listed': 'BODY-C newest reply' in self.quarantined_bodies(),
             'c_archived_as_sent': 'BODY-C newest reply' in archived},
            {'relay_holds': ['BODY-A parked answer'], 'sent': ['BODY-A parked answer'],
             'status': 'DELIVERED', 'recorded_body': 'BODY-A parked answer',
             'c_live': False, 'c_listed': True, 'c_archived_as_sent': False})
        said = [l for l in self.lines if TID in l and UNSENT in l]
        self.assertEqual(len(said), 1, '\n'.join(self.lines))
        self._assert_review_line(said[0], delivered='BODY-A parked answer', this='BODY-C newest reply')
        self.assertNotIn('restores it', said[0], 'a requeue of a delivered id restores nothing')

    def test_a_caller_without_a_result_file_is_told_the_stored_body_went(self):
        self._legacy_epoch_used_then_c()
        self.assertFalse(gw._deliver_result_payload(TID, TID, 'BODY-C newest reply'))
        self.assertTrue(any(UNSENT in l and 'not retrying' in l for l in self.lines), self.lines)

    def _c_after_a_delivered(self):
        """The lost-response schedule run up to A being confirmed and recorded
        DELIVERED; returns before anything rules on the live C."""
        result, held = self._legacy_epoch_used_then_c()
        return result, len(self.server.calls)

    def _archived_bodies(self):
        return sorted(p.read_text() for p in (self.results / 'archive').rglob('*.txt'))

    def _assert_c_quarantined_never_archived(self, result, posts_from):
        self.assertEqual(
            {'posts': [c.get('body') for c in self.server.calls[posts_from:]],
             'status': outbox.item_status(self.outbox, TID), 'c_live': result.exists(),
             'c_listed': 'BODY-C newest reply' in self.quarantined_bodies(),
             'c_archived': 'BODY-C newest reply' in self._archived_bodies()},
            {'posts': ['BODY-A parked answer'], 'status': 'DELIVERED', 'c_live': False,
             'c_listed': True, 'c_archived': False})

    def test_a_failed_quarantine_is_retried_by_the_next_pass_never_archived(self):
        result, posts_from = self._c_after_a_delivered()
        with patch.object(gw.disposal, 'quarantine_generation', side_effect=OSError(5, 'EIO')):
            gw._post_ready_results({TID})
        self.assertEqual(result.read_text(), 'BODY-C newest reply', 'a failed move leaves C live')
        self.assertNotIn('BODY-C newest reply', self._archived_bodies())
        gw._post_ready_results({TID})
        self._assert_c_quarantined_never_archived(result, posts_from)

    def _crash_after_the_confirm(self):
        class Crash(BaseException):
            pass
        result, posts_from = self._c_after_a_delivered()
        # Whichever ruling the bridge makes first after the confirm.
        ruling = 'delivered_body_differs' if hasattr(gw, 'delivered_body_differs') else '_record_sent_this_body'
        with patch.object(gw, ruling, side_effect=Crash):
            with self.assertRaises(Crash):
                gw._post_ready_results({TID})
        self.assertEqual(outbox.item_status(self.outbox, TID), 'DELIVERED')
        self.assertEqual(result.read_text(), 'BODY-C newest reply')
        self.bridge(self.core())                         # a restarted process: a fresh core
        return result, posts_from

    def test_a_crash_between_the_confirm_and_the_ruling_then_the_drain(self):
        result, posts_from = self._crash_after_the_confirm()
        gw._post_ready_results({TID})
        self._assert_c_quarantined_never_archived(result, posts_from)

    def test_a_crash_between_the_confirm_and_the_ruling_then_the_sweep(self):
        result, posts_from = self._crash_after_the_confirm()
        os.utime(result, (time.time() - gw.ORPHAN_GRACE_S - 60,) * 2)
        gw._reconcile_orphan_results(set())
        self._assert_c_quarantined_never_archived(result, posts_from)

    def _delivered_then_late(self, late, first='BODY-A the reply', before_late=None):
        self.bridge()
        self.task()
        self.server.accepting = True
        result = self.result(first)
        gw._post_ready_results({TID})
        self.assertEqual(outbox.item_status(self.outbox, TID), 'DELIVERED')
        if before_late:
            before_late()
        posts = len(self.server.calls)
        result.write_text(late)
        os.utime(result, (time.time() - gw.ORPHAN_GRACE_S - 60,) * 2)
        gw._reconcile_orphan_results(set())
        return result, posts

    def test_a_late_note_at_a_delivered_id_is_quarantined_not_archived(self):
        result, posts = self._delivered_then_late('Replied in the room; the result was archived.')
        self.assertFalse(result.exists())
        self.assertIn('Replied in the room; the result was archived.', self.quarantined_bodies())
        self.assertNotIn('Replied in the room; the result was archived.', self._archived_bodies())
        self.assertEqual(len(self.server.calls), posts, 'nothing is posted')
        self.assertTrue(any(UNSENT in l for l in self.lines), self.lines)

    def test_a_late_copy_not_yet_readable_is_left_for_a_later_sweep(self):
        result, posts = self._delivered_then_late('  \n')
        self.assertEqual(result.read_text(), '  \n')
        self.assertEqual((self.quarantined_bodies(), len(self.server.calls)), ([], posts))
        self.assertEqual(list((self.results / 'archive').rglob(f'{TID}-*-late-duplicate*.txt')), [])

    MARKED = ('[REPLIED] Posted it in the room.', '[no-send]\ninternal, nothing to say',
              '[dm-only]\nBODY-A the reply', '[channel: !other:ag2.space]\nBODY-A the reply')

    def _forget_the_source(self):
        """The record as a drain from before source digests left it."""
        rec = outbox.read_item(self.outbox, TID)
        rec.pop('source_ready_sha256', None)
        outbox._write_item(self.outbox, TID, rec)

    def test_an_identical_late_copy_of_a_marked_reply_is_archived_as_a_duplicate(self):
        for body in self.MARKED:
            with self.subTest(body=body):
                self.setUp()
                result, posts = self._delivered_then_late(body, first=body)
                self.assertFalse(result.exists())
                self.assertEqual(self.quarantined_bodies(), [])
                self.assertEqual(len(list((self.results / 'archive').rglob(f'{TID}-*-late-duplicate*.txt'))), 1)
                self.assertEqual(len(self.server.calls), posts, 'no second POST')
                self.assertFalse(any('by hand' in l for l in self.lines), self.lines)

    def test_a_record_without_a_source_proves_only_an_unmarked_identical_copy(self):
        """Fail closed: a digest-less record cannot prove a marked source; a skip
        marker still owes nothing, an action marker goes to review."""
        cases = (('BODY-A the reply', 'archived'),
                 ('[REPLIED] Posted it in the room.', 'archived'),
                 ('[no-send]\ninternal, nothing to say', 'archived'),
                 ('[dm-only]\nBODY-A the reply', 'review'),
                 # The redirect rides the wire body unchanged, so its source is provable.
                 ('[channel: !other:ag2.space]\nBODY-A the reply', 'archived'))
        for body, outcome in cases:
            with self.subTest(body=body):
                self.setUp()
                result, posts = self._delivered_then_late(body, first=body,
                                                          before_late=self._forget_the_source)
                self.assertFalse(result.exists())
                self.assertEqual(len(self.server.calls), posts, 'no second POST')
                self.assertFalse(any('by hand' in l for l in self.lines), self.lines)
                if outcome == 'archived':
                    self.assertIn(body, self._archived_bodies())
                    self.assertEqual(self.quarantined_bodies(), [])
                else:
                    self.assertIn(body, self.quarantined_bodies())
                    self.assertTrue(any('review it' in l for l in self.lines), self.lines)

    def test_a_record_without_a_source_never_archives_a_new_action_around_the_old_body(self):
        """The old wire body under a new [dm-only], [channel:] or [file:]: the action
        was never performed, so it is not a duplicate."""
        for late in ('[dm-only]\nBODY-A the reply', '[channel: !other:ag2.space]\nBODY-A the reply',
                     '[file: /tmp/new-report.txt]\nBODY-A the reply'):
            with self.subTest(late=late):
                self.setUp()
                result, posts = self._delivered_then_late(late, before_late=self._forget_the_source)
                self.assertFalse(result.exists())
                self.assertIn(late, self.quarantined_bodies())
                self.assertNotIn(late, self._archived_bodies())
                self.assertEqual(len(self.server.calls), posts)
                self.assertFalse(any('moved aside' in l for l in self.lines), self.lines)

    def test_a_different_suppressed_reply_is_archived_never_handed_over(self):
        result, posts = self._delivered_then_late('[no-send]\nsomething internal')
        self.assertFalse(result.exists())
        self.assertEqual(self.quarantined_bodies(), [])
        self.assertIn('[no-send]\nsomething internal', self._archived_bodies())
        self.assertEqual(len(self.server.calls), posts)
        self.assertFalse(any('by hand' in l for l in self.lines), self.lines)
        self.assertTrue(any('suppressed' in l and 'owes no room delivery' in l for l in self.lines), self.lines)

    def test_a_suppressed_reply_that_cannot_be_archived_stays_for_the_next_pass(self):
        failed = disposal.Retired(disposal.Retirement.FAILED, None, 'the disposal lock is busy')
        real = disposal.retire_generation

        def fail_only_the_suppressed_archive(results_dir, rfile, generation, log, directory, names):
            names = list(names)
            if names and '-suppressed' in names[0]:
                return failed
            return real(results_dir, rfile, generation, log, directory, names)
        with patch.object(disposal, 'retire_generation', fail_only_the_suppressed_archive):
            result, posts = self._delivered_then_late('[no-send]\nsomething internal')
        self.assertTrue(result.exists(), 'a failed archive leaves it live')
        self.assertTrue(any('could not be archived' in l for l in self.lines), self.lines)
        gw._last_orphan_sweep = 0.0
        gw._reconcile_orphan_results(set())
        self.assertIn('[no-send]\nsomething internal', self._archived_bodies())
        self.assertEqual((self.quarantined_bodies(), len(self.server.calls)), ([], posts))

    def test_a_late_copy_differing_only_in_surrounding_whitespace_is_a_duplicate(self):
        for first, late in (('BODY-A the reply', 'BODY-A the reply\n'),
                            ('BODY-A the reply\n', 'BODY-A the reply'),
                            ('BODY-A the reply', '\n  BODY-A the reply  \n\n')):
            with self.subTest(first=first, late=late):
                self.setUp()
                result, posts = self._delivered_then_late(late, first=first)
                self.assertFalse(result.exists())
                self.assertEqual(self.quarantined_bodies(), [])
                self.assertEqual(len(list((self.results / 'archive').rglob(f'{TID}-*-late-duplicate*.txt'))), 1)
                self.assertEqual(len(self.server.calls), posts)
                self.assertFalse(any('by hand' in l for l in self.lines), self.lines)

    def test_a_whitespace_rewrite_after_a_crash_before_the_archive_is_archived_by_the_drain(self):
        class Crash(BaseException):
            pass
        self.bridge()
        self.task()
        self.server.accepting = True
        result = self.result('BODY-A the reply')
        with patch.object(gw, '_archive_result', side_effect=Crash):
            with self.assertRaises(Crash):
                gw._post_ready_results({TID})
        posts = len(self.server.calls)
        result.write_text('BODY-A the reply\n')
        self.bridge(self.core())
        gw._post_ready_results({TID})
        self.assertFalse(result.exists())
        self.assertEqual(self.quarantined_bodies(), [])
        self.assertIn('BODY-A the reply\n', self._archived_bodies())
        self.assertEqual(len(self.server.calls), posts)

    def test_the_stored_source_is_what_tells_a_restricted_rewrite_from_a_duplicate(self):
        """Same text plus a restriction marker: only the digest the drain stored at
        publish shows it is another source (without it, the stripped text matches)."""
        for late in ('[dm-only]\nBODY-A the reply', '[channel: !other:ag2.space]\nBODY-A the reply'):
            with self.subTest(late=late):
                self.setUp()
                result, posts = self._delivered_then_late(late)
                rec = outbox.read_item(self.outbox, TID)
                self.assertEqual(rec.get('source_ready_sha256'),
                                 hashlib.sha256('BODY-A the reply'.encode()).hexdigest())
                self.assertIn(late, self.quarantined_bodies())
                self.assertNotIn(late, self._archived_bodies())
                self.assertTrue(any('review it' in l for l in self.lines), self.lines)
                self.assertEqual(len(self.server.calls), posts)

    def _team_task(self, **extra):
        lines = [f'id: {TID}', 'source: ag2space', f'channel_id: {ROOM}', 'user_id: @alice:ag2.space',
                 'access_tier: team'] + [f'{k}: {v}' for k, v in extra.items()] + ['task: Same question']
        (self.tasks / f'{TID}.txt').write_text('\n'.join(lines) + '\n')

    def _team_delivered_then_late(self, late, **extra):
        self.bridge()
        self._team_task(**extra)
        gw._record_task_room(TID, ROOM)
        self.server.accepting = True
        result = self.result('BODY-A the reply')
        gw._post_ready_results({TID})
        self.assertEqual(outbox.item_status(self.outbox, TID), 'DELIVERED')
        posts = len(self.server.calls)
        result.write_text(late)
        os.utime(result, (time.time() - gw.ORPHAN_GRACE_S - 60,) * 2)
        gw._reconcile_orphan_results(set())
        return result, posts

    def test_a_team_suppression_at_a_delivered_id_is_journalled_by_the_guard(self):
        from ag2_sparrow import team_result_guard as trg
        result, posts = self._team_delivered_then_late('[no-send]')
        self.assertIn('[no-send]', self._archived_bodies())
        self.assertEqual(len(self.server.calls), posts)
        journal = self.root / 'state' / trg.SUPPRESSED_RESULT_DIR
        self.assertTrue(journal.is_dir() and any(journal.iterdir()), 'no suppression journal')
        self.assertFalse(any('by hand' in l for l in self.lines), self.lines)

    def test_a_team_attachment_at_a_delivered_id_is_withheld_for_owner_review(self):
        from ag2_sparrow import team_result_guard as trg
        routed = []
        with patch.object(gw, '_route_withheld_review', side_effect=lambda p: routed.append(p) or True):
            result, posts = self._team_delivered_then_late('[file: /tmp/private-report.txt]\nBODY-C')
        self.assertFalse(result.exists())
        self.assertEqual(len(self.server.calls), posts)
        self.assertTrue(routed, 'the withheld review never reached the owner')
        self.assertTrue((self.root / 'state' / trg.WITHHELD_RESULT_DIR).is_dir())
        said = [l for l in self.lines if UNSENT in l]
        self.assertEqual(len(said), 1, self.lines)
        self.assertIn('withheld by the result guard', said[0])
        self.assertNotIn('by hand', said[0])

    def _late(self, result, body):
        result.write_text(body)
        os.utime(result, (time.time() - gw.ORPHAN_GRACE_S - 60,) * 2)
        gw._last_orphan_sweep = 0.0
        gw._reconcile_orphan_results(set())

    def _records(self, directory, field):
        d = self.root / 'state' / directory
        return sorted(json.loads(p.read_text())[field] for p in d.glob('*.json')) if d.is_dir() else []

    def test_each_later_team_attachment_gets_its_own_owner_review(self):
        """B then C under one task, warm and across a restart: C is reviewed as C,
        never archived on B's record."""
        from ag2_sparrow import team_result_guard as trg
        for restart in (False, True):
            with self.subTest(restart=restart):
                self.setUp()
                routed = []
                route = patch.object(gw, '_route_withheld_review',
                                     side_effect=lambda p: routed.append(json.loads(p.read_text())['withheld_body']) or True)
                with route:
                    result, posts = self._team_delivered_then_late('[file: /tmp/b.txt]\nBODY-B')
                    if restart:
                        self.bridge(self.core())             # a fresh process: no in-memory verdicts
                    self._late(result, '[file: /tmp/c.txt]\nBODY-C')
                self.assertEqual(self._records(trg.WITHHELD_RESULT_DIR, 'withheld_body'),
                                 ['[file: /tmp/b.txt]\nBODY-B', '[file: /tmp/c.txt]\nBODY-C'])
                self.assertIn('[file: /tmp/c.txt]\nBODY-C', routed)
                self.assertFalse(result.exists())
                self.assertEqual(len(self.server.calls), posts)

    def test_a_resolved_review_id_is_never_reused_for_a_later_body(self):
        """B is reviewed, resolved and archived; C gets its own review id, its own
        messages (the gateway dedupes on that id), and B's archive is untouched."""
        from ag2_sparrow import team_result_guard as trg
        with patch.object(gw, '_gateway_owner', return_value='@owner:ag2.space'), \
                patch.object(gw, '_owner_review_dm', return_value='!ownerdm:ag2.space'):
            result, posts = self._team_delivered_then_late('[file: /tmp/b.txt]\nBODY-B')
            hot = self.root / 'state' / trg.WITHHELD_RESULT_DIR
            (b_path,) = hot.glob('wr_*.json')
            b = json.loads(b_path.read_text())
            self.assertEqual(b['status'], 'awaiting_owner')
            b['status'] = 'kept_private'
            b_path.write_text(json.dumps(b))
            self.assertTrue(gw._archive_resolved_review(b_path, b))
            self._late(result, '[file: /tmp/c.txt]\nBODY-C')
            (c_path,) = hot.glob('wr_*.json')
        c = json.loads(c_path.read_text())
        self.assertNotEqual(c['review_id'], b['review_id'])
        self.assertEqual((c['withheld_body'], c['status']), ('[file: /tmp/c.txt]\nBODY-C', 'awaiting_owner'))
        keys = [p.get('dedupe_key', '') for p in self.server.calls]
        self.assertTrue(any(c['review_id'] in k for k in keys), keys)
        self.assertTrue(any('BODY-C' in str(p.get('body')) for p in self.server.calls))
        archived = json.loads((hot / 'archive' / b_path.name).read_text())
        self.assertEqual(archived['withheld_body'], '[file: /tmp/b.txt]\nBODY-B')

    def test_a_reply_replaced_between_its_ruling_and_its_disposal_is_judged_again(self):
        """C is ruled different; the delivered A is put back before the disposal
        rereads the file: A is not disposed of on C's ruling."""
        real, fired = gw.delivered_body_differs, []

        def rule_then_put_a_back(root, item_id, ready_body):
            out = real(root, item_id, ready_body)
            if out and not fired:
                fired.append(1)
                tmp = self.results / '.producer.tmp'
                tmp.write_text('BODY-A the reply')
                os.replace(tmp, self.results / f'{TID}.txt')
            return out
        with patch.object(gw, 'delivered_body_differs', rule_then_put_a_back):
            result, posts = self._delivered_then_late('BODY-C a different reply')
        self.assertEqual(fired, [1])
        self.assertEqual(result.read_text(), 'BODY-A the reply', 'A was disposed of on C\'s ruling')
        self.assertEqual(self.quarantined_bodies(), [])
        self._late(result, 'BODY-A the reply')
        self.assertFalse(result.exists())
        self.assertEqual(self.quarantined_bodies(), [])
        self.assertEqual(len(list((self.results / 'archive').rglob(f'{TID}-*-late-duplicate*.txt'))), 1)
        self.assertEqual(len(self.server.calls), posts)
        self.assertFalse(any('by hand' in l for l in self.lines), self.lines)

    def test_a_payload_adopted_by_an_earlier_writer_never_keeps_this_heads_proof(self):
        """This head parks A with a proof; an earlier writer adopts B (payload and its
        own source_sha256, keeping fields it does not know); B is delivered. The proof
        of A no longer describes the payload, so the unchanged B is a duplicate."""
        self.bridge()
        self.task()
        result = self.result('BODY-A first answer')
        gw._post_ready_results({TID})                            # 400: parked with A's proof
        self.assertTrue(outbox.read_item(self.outbox, TID).get('source_ready_sha256'))
        outbox.requeue_item(self.outbox, TID, reset_attempts=True)
        rec = outbox.read_item(self.outbox, TID)
        rec.update(payload=json.dumps({'id': TID, 'body': 'BODY-B newer answer'}),
                   resend_adopted_epoch=rec['resend_epoch'],
                   source_sha256=hashlib.sha256(b'BODY-B newer answer').hexdigest())
        outbox._write_item(self.outbox, TID, rec)                # what the earlier adopt wrote
        result.write_text('BODY-B newer answer')
        self.server.accepting = True
        gw._post_ready_results({TID})
        self.assertEqual([c.get('body') for c in self.server.calls][-1], 'BODY-B newer answer')
        self.assertEqual(outbox.item_status(self.outbox, TID), 'DELIVERED')
        self.assertEqual(self.quarantined_bodies(), ['BODY-A first answer'], 'only the refused A')
        self.assertIn('BODY-B newer answer', self._archived_bodies())
        self.assertFalse(any('by hand' in l for l in self.lines), self.lines)

    def test_an_identical_body_after_its_review_was_archived_reuses_no_consumed_id(self):
        """B is reviewed, resolved and archived; after a restart identical B arrives
        again: no new record under the consumed id, no new review messages, and B
        is retired against the decision already made."""
        from ag2_sparrow import team_result_guard as trg
        with patch.object(gw, '_gateway_owner', return_value='@owner:ag2.space'), \
                patch.object(gw, '_owner_review_dm', return_value='!ownerdm:ag2.space'):
            result, posts = self._team_delivered_then_late('[file: /tmp/b.txt]\nBODY-B')
            hot = self.root / 'state' / trg.WITHHELD_RESULT_DIR
            (b_path,) = hot.glob('wr_*.json')
            b = json.loads(b_path.read_text())
            b['status'] = 'kept_private'
            b_path.write_text(json.dumps(b))
            self.assertTrue(gw._archive_resolved_review(b_path, b))
            room_messages = len([c for c in self.server.calls if c.get('op') == 'message'])
            self.bridge(self.core())                             # a restart: no in-memory verdicts
            self._late(result, '[file: /tmp/b.txt]\nBODY-B')
        self.assertEqual(list(hot.glob('wr_*.json')), [], 'a consumed review id was reissued')
        self.assertEqual(json.loads((hot / 'archive' / b_path.name).read_text())['status'], 'kept_private')
        self.assertEqual(len([c for c in self.server.calls if c.get('op') == 'message']), room_messages)
        self.assertFalse(result.exists())
        self.assertTrue(any('already decided in owner review' in l for l in self.lines), self.lines)
        self.assertEqual(self.quarantined_bodies(), [])

    ERA_RECORDS = json.loads((REPO / 'tests' / 'fixtures' / 'adoption-era-outbox-records.json')
                             .read_text())['records']

    def _install(self, record):
        def install():
            outbox._write_item(self.outbox, TID, dict(record))
        return install

    def test_invariant_conflicting_legacy_state_fails_closed(self):
        """Records generated by rounds 14-21's real writers (each adopted a marked B
        composing to A's payload and delivered it), and an unknown writer's shape: no
        trusted proof, so an unchanged body is a duplicate and a marked one goes to
        review, never "send it by hand"."""
        records = dict(self.ERA_RECORDS)
        # The same adoption with the clock repeating: nothing in the record moved.
        records['round 14, adopted in the same millisecond'] = dict(
            self.ERA_RECORDS['round 14 (d2c5493f0)'], published_at=1000.0, requeued_at=1000.0)
        for name in list(records) + ['unknown writer']:
            for late in ('BODY-A the reply', '[file: /tmp/report.txt]\nBODY-A the reply'):
                with self.subTest(writer=name, late=late):
                    self.setUp()
                    if name == 'unknown writer':
                        def install():
                            rec = outbox.read_item(self.outbox, TID)
                            rec.update(proof_version=2, a_field_no_writer_here_knows=1)
                            outbox._write_item(self.outbox, TID, rec)
                    else:
                        install = self._install(records[name])
                    result, posts = self._delivered_then_late(late, first='BODY-A the reply',
                                                              before_late=install)
                    self.assertFalse(result.exists())
                    self.assertEqual(len(self.server.calls), posts)
                    self.assertFalse(any('by hand' in l for l in self.lines), self.lines)
                    # A stale proof may tip archive versus review either way; neither is a send.
                    archived = len(list((self.results / 'archive').rglob(f'{TID}-*-late-duplicate*.txt')))
                    reviewed = (late in self.quarantined_bodies()
                                and any('may already have been sent' in l for l in self.lines))
                    self.assertTrue(archived == 1 or reviewed, (archived, self.lines))

    def test_a_reply_at_a_delivered_id_is_never_handed_over_for_sending(self):
        """No stored state can prove a reply was not already sent (delivery adds
        attachment notes and recovery labels), so every such reply gets the review
        line, its markers and both body references, whatever the proof says."""
        for late in ('[file: /tmp/report.txt]\nBODY-A the reply', '[file: /tmp/report.txt]\nBODY-C a new answer',
                     'BODY-C a new answer'):
            with self.subTest(late=late):
                self.setUp()
                self._delivered_then_late(late)
                said = [l for l in self.lines if UNSENT in l]
                self.assertEqual(len(said), 1, self.lines)
                self._assert_review_line(said[0], delivered='BODY-A the reply', this=late)
                if late.startswith('[file:'):
                    self.assertIn('marked [file: /tmp/report.txt]', said[0])

    def test_delivery_transforms_never_turn_a_delivered_source_into_a_manual_send(self):
        """Kewei's schedules: an attachment delivered as "(file attached)", and an
        orphan recovery delivered with its label, then a crash before the archive and
        a restarted sweep. Same source each time; never a send instruction."""
        class Crash(BaseException):
            pass
        for name, record_body in (('attachment placeholder', '(file attached)'),
                                  ('recovery label', '(recovered result \u2014 original delivery was lost)\nBODY-A the reply')):
            with self.subTest(transform=name):
                self.setUp()
                self.bridge()
                self.task()
                self.server.accepting = True
                source = '[file: /tmp/report.txt]\nBODY-A the reply' if name.startswith('attach') else 'BODY-A the reply'
                result = self.result(source)
                with patch.object(gw, '_archive_result', side_effect=Crash):
                    with self.assertRaises(Crash):
                        gw._post_ready_results({TID})
                rec = outbox.read_item(self.outbox, TID)
                for field in ('proof_version', 'source_ready_sha256', 'source_payload_sha256', 'publication_id'):
                    rec.pop(field, None)                          # a record no trusted proof covers
                rec['payload'] = json.dumps({'id': TID, 'body': record_body})
                outbox._write_item(self.outbox, TID, rec)
                posts = len(self.server.calls)
                self.bridge(self.core())
                if name.startswith('attach'):
                    gw._post_ready_results({TID})                 # the drain still holds the id
                else:
                    os.utime(result, (time.time() - gw.ORPHAN_GRACE_S - 60,) * 2)
                    gw._reconcile_orphan_results(set())           # the in-flight id was lost
                self.assertEqual(len(self.server.calls), posts, 'no second POST')
                self.assertFalse(any('by hand' in l for l in self.lines), self.lines)
                said = [l for l in self.lines if UNSENT in l]
                self.assertTrue(said, self.lines)
                self._assert_review_line(said[-1], delivered=record_body, this=source)

    def _reviewed(self, body):
        """B delivered as a Team result's withheld review through real routing."""
        from ag2_sparrow import team_result_guard as trg
        result, posts = self._team_delivered_then_late(body)
        hot = self.root / 'state' / trg.WITHHELD_RESULT_DIR
        (path,) = hot.glob('wr_*.json')
        return result, hot, path

    def _messages(self):
        return len([c for c in self.server.calls if c.get('op') == 'message'])

    def test_invariant_archive_and_issue_are_one_serialized_transition(self):
        from ag2_sparrow import team_result_guard as trg
        with patch.object(gw, '_gateway_owner', return_value='@owner:ag2.space'), \
                patch.object(gw, '_owner_review_dm', return_value='!ownerdm:ag2.space'):
            result, hot, b_path = self._reviewed('[file: /tmp/b.txt]\nBODY-B')
            sent = self._messages()
            self.bridge(self.core())
            # The resolver archives B between the replay choosing B's id and acting on it
            # (a head without the reservation ledger acts by writing the record).
            hook = '_issued_to' if hasattr(trg, '_issued_to') else '_write_artifact'
            real, fired = getattr(trg, hook), []

            threads = []

            def resolve_and_archive():
                b = json.loads(b_path.read_text())
                b['status'] = 'kept_private'
                b_path.write_text(json.dumps(b))
                gw._archive_resolved_review(b_path, b)

            def archive_mid_issue(path, *rest):
                if not fired and path.name == b_path.name and b_path.exists():
                    fired.append(1)                         # a concurrent resolver, not this thread
                    threads.append(threading.Thread(target=resolve_and_archive))
                    threads[0].start()
                    threads[0].join(0.2)                    # it finishes now unless it must wait
                return real(path, *rest)
            with patch.object(trg, hook, archive_mid_issue):
                self._late(result, '[file: /tmp/b.txt]\nBODY-B')
            for t in threads:
                t.join(5)
        self.assertEqual(fired, [1])
        self.assertEqual(list(hot.glob('wr_*.json')), [], 'the consumed id was recreated')
        self.assertEqual(json.loads((hot / 'archive' / b_path.name).read_text())['status'], 'kept_private')
        self.assertEqual(self._messages(), sent)

    def test_a_delivered_record_whose_wire_body_is_unknown_never_yields_a_manual_send(self):
        def garble():
            rec = outbox.read_item(self.outbox, TID)
            outbox._write_item(self.outbox, TID, dict(rec, payload='{not json'))
        result, posts = self._delivered_then_late('BODY-C a different reply', before_late=garble)
        said = [l for l in self.lines if UNSENT in l]
        self.assertEqual(len(said), 1, self.lines)
        self.assertNotIn('by hand', said[0])
        self.assertIn('may already have been sent', said[0])

    def test_invariant_record_ownership_stays_bound_to_its_body(self):
        """Legacy state: B's decision archived and a stale live C under the same id,
        no reservations. B replays against its own decision; C is a new body with
        its own id and its own owner prompt, never B's decision."""
        from ag2_sparrow import team_result_guard as trg
        with patch.object(gw, '_gateway_owner', return_value='@owner:ag2.space'), \
                patch.object(gw, '_owner_review_dm', return_value='!ownerdm:ag2.space'):
            result, hot, b_path = self._reviewed('[file: /tmp/b.txt]\nBODY-B')
            b = json.loads(b_path.read_text())
            (hot / 'archive').mkdir()
            (hot / 'archive' / b_path.name).write_text(json.dumps(dict(b, status='kept_private')))
            b_path.write_text(json.dumps(dict(b, withheld_body='[file: /tmp/c.txt]\nBODY-C',
                                              status='awaiting_owner')))
            shutil.rmtree(hot / 'issued', ignore_errors=True)
            self.bridge(self.core())
            sent = self._messages()
            self._late(result, '[file: /tmp/c.txt]\nBODY-C')
            c_records = [p for p in hot.glob('wr_*.json') if p.name != b_path.name]
            self.assertEqual(len(c_records), 1, 'C got no review of its own')
            c = json.loads(c_records[0].read_text())
            self.assertEqual((c['withheld_body'], c['status']), ('[file: /tmp/c.txt]\nBODY-C', 'awaiting_owner'))
            self.assertGreater(self._messages(), sent, 'the owner was never prompted for C')
            self.assertFalse(any('already decided' in l and 'BODY-C' in l for l in self.lines))
            sent = self._messages()
            self._late(result, '[file: /tmp/b.txt]\nBODY-B')
        self.assertEqual(self._messages(), sent, 'B was prompted again despite its decision')
        self.assertTrue(any('already decided in owner review' in l for l in self.lines), self.lines)

    def test_invariant_an_archived_decision_is_immutable_and_a_conflicting_live_record_frozen(self):
        """Legacy state: B's decision archived and a stale live C under B's id (no
        ledger). An owner reply to the old id never reaches C, C is frozen for the
        owner, and B's archived decision survives every later archive."""
        from ag2_sparrow import team_result_guard as trg
        hot = self.root / 'state' / trg.WITHHELD_RESULT_DIR
        old = trg.withheld_review_path(self.root / 'state', TID)
        (hot / 'archive').mkdir(parents=True)
        b = {'review_id': old.stem, 'status': 'kept_private', 'withheld_body': 'BODY-B',
             'owner': '@owner:ag2.space', 'dm_room_id': '!ownerdm:ag2.space', 'dm_event_id': '$b'}
        (hot / 'archive' / old.name).write_text(json.dumps(b))
        old.write_text(json.dumps(dict(b, status='awaiting_owner', withheld_body='BODY-C', dm_event_id='$c')))
        self.bridge()
        task = {'user_id': '@owner:ag2.space', 'channel_id': '!ownerdm:ag2.space', 'task': f'Yes {old.stem}'}
        with patch.object(gw, '_tier_for', return_value='owner'):
            self.assertIsNone(gw._match_review_decision(task), 'an owner reply to the old id matched C')
        self.assertEqual(gw._pending_review_records(), [])
        frozen = list((hot / 'conflicts').glob('*.json'))
        self.assertEqual([json.loads(p.read_text())['withheld_body'] for p in frozen], ['BODY-C'])
        self.assertFalse(old.exists())
        self.assertFalse(trg.archive_record(old))
        self.assertFalse(trg.update_record(old, dict(b, status='published')))
        self.assertEqual(json.loads((hot / 'archive' / old.name).read_text()), b, "B's decision changed")
        self.assertTrue(any('frozen' in l for l in self.lines), self.lines)
        with patch.object(gw.team_result_guard, 'actionable_records', side_effect=OSError(5, 'EIO')):
            self.assertEqual(gw._pending_review_records(), [], 'an unreadable store lists nothing to act on')

    def test_an_archive_inside_a_lookup_waits_for_it(self):
        """The resolver's archive cannot land between a lookup's archive and live
        checks: it waits for the ledger lock, so a decided replay is never re-issued."""
        from ag2_sparrow import team_result_guard as trg
        with patch.object(gw, '_gateway_owner', return_value='@owner:ag2.space'), \
                patch.object(gw, '_owner_review_dm', return_value='!ownerdm:ag2.space'):
            result, hot, b_path = self._reviewed('[file: /tmp/b.txt]\nBODY-B')
            self.bridge(self.core())
            # Between the lookup's archive check and its read of the live record.
            hook = '_record_digest' if hasattr(trg, '_record_digest') else '_record_of'
            real, threads = getattr(trg, hook), []

            decided = dict(json.loads(b_path.read_text()), status='kept_private')
            b_path.write_text(json.dumps(decided))           # the owner's decision, already recorded

            def resolve_and_archive():
                gw._archive_resolved_review(b_path, decided)

            def archive_inside_lookup(path, *rest):
                if not threads and path == b_path:
                    threads.append(threading.Thread(target=resolve_and_archive))
                    threads[0].start()
                    threads[0].join(0.2)
                return real(path, *rest)
            with patch.object(trg, hook, archive_inside_lookup):
                self._late(result, '[file: /tmp/b.txt]\nBODY-B')
            threads[0].join(5)
        self.assertEqual(list(hot.glob('wr_*.json')), [], 'a fresh id was issued for a decided body')
        self.assertEqual(len(list((hot / 'archive').glob('wr_*.json'))), 1)

    def test_an_upgrade_with_a_stale_live_copy_of_a_decided_review_never_reroutes_it(self):
        """A host left an archived decision and a live same-body record under the same
        id, and no reservations: the decision wins, nothing is routed again."""
        from ag2_sparrow import team_result_guard as trg
        with patch.object(gw, '_gateway_owner', return_value='@owner:ag2.space'), \
                patch.object(gw, '_owner_review_dm', return_value='!ownerdm:ag2.space'):
            result, hot, b_path = self._reviewed('[file: /tmp/b.txt]\nBODY-B')
            b = json.loads(b_path.read_text())
            (hot / 'archive').mkdir()
            (hot / 'archive' / b_path.name).write_text(json.dumps(dict(b, status='kept_private')))
            shutil.rmtree(hot / 'issued', ignore_errors=True)      # state from before reservations
            sent = self._messages()
            self.bridge(self.core())
            self._late(result, '[file: /tmp/b.txt]\nBODY-B')
        self.assertEqual(self._messages(), sent)
        self.assertFalse(result.exists())
        self.assertTrue(any('already decided in owner review' in l for l in self.lines), self.lines)

    def test_a_replacement_before_retirement_is_judged_on_its_own(self):
        from ag2_sparrow import team_result_guard as trg
        real, fired = disposal.retire_generation, []

        def replace_then_retire(results_dir, rfile, generation, log, directory, names):
            names = list(names)
            if not fired and names and '-suppressed' in names[0]:
                fired.append(1)
                tmp = Path(rfile).with_name('.producer.tmp')
                tmp.write_text('[file: /tmp/d.txt]\nBODY-D')
                os.replace(tmp, rfile)
            return real(results_dir, rfile, generation, log, directory, names)
        with patch.object(gw, '_route_withheld_review', return_value=True), \
                patch.object(disposal, 'retire_generation', replace_then_retire):
            result, posts = self._team_delivered_then_late('[file: /tmp/c.txt]\nBODY-C')
            self.assertEqual(fired, [1])
            self.assertEqual(result.read_text(), '[file: /tmp/d.txt]\nBODY-D', 'D was retired on C\'s decision')
            self._late(result, '[file: /tmp/d.txt]\nBODY-D')
        self.assertEqual(self._records(trg.WITHHELD_RESULT_DIR, 'withheld_body'),
                         ['[file: /tmp/c.txt]\nBODY-C', '[file: /tmp/d.txt]\nBODY-D'])
        self.assertFalse(result.exists())
        self.assertEqual(len(self.server.calls), posts)

    def test_two_distinct_team_suppressions_are_both_journalled(self):
        from ag2_sparrow import team_result_guard as trg
        for restart in (False, True):
            with self.subTest(restart=restart):
                self.setUp()
                result, posts = self._team_delivered_then_late('[no-send]\ninternal note one')
                if restart:
                    self.bridge(self.core())
                self._late(result, '[no-send]\ninternal note two')
                self.assertEqual(self._records(trg.SUPPRESSED_RESULT_DIR, 'suppressed_body'),
                                 ['[no-send]\ninternal note one', '[no-send]\ninternal note two'])
                self.assertFalse(result.exists())
                self.assertEqual(len(self.server.calls), posts)

    def test_a_reply_gone_before_its_ruling_is_left_for_the_next_pass(self):
        self.bridge()
        gw._quarantine_unsent(self.results / f'{TID}.txt', TID, TID)
        self.assertTrue(any('not readable now' in l for l in self.lines), self.lines)
        self.assertEqual(self.quarantined_bodies(), [])

    def test_an_earlier_digest_field_is_never_read_as_this_one(self):
        """A record from an earlier writer of `source_sha256` (raw bytes, or the ready
        body): an unchanged plain body is a duplicate, never "send it by hand"."""
        for name, value in (('raw-bytes writer', lambda text: hashlib.sha256(text.encode()).hexdigest()),
                            ('wrong value', lambda text: '0' * 64)):
            with self.subTest(writer=name):
                self.setUp()

                def earlier_writer():
                    rec = outbox.read_item(self.outbox, TID)
                    rec.pop('source_ready_sha256', None)
                    rec['source_sha256'] = value('BODY-A the reply\n')
                    outbox._write_item(self.outbox, TID, rec)
                result, posts = self._delivered_then_late('BODY-A the reply\n', first='BODY-A the reply\n',
                                                          before_late=earlier_writer)
                self.assertFalse(result.exists())
                self.assertEqual(self.quarantined_bodies(), [])
                self.assertEqual(len(list((self.results / 'archive').rglob(f'{TID}-*-late-duplicate*.txt'))), 1)
                self.assertEqual(len(self.server.calls), posts)
                self.assertFalse(any('by hand' in l for l in self.lines), self.lines)

    def test_an_owner_mention_result_at_a_delivered_id_goes_to_the_owner_dm(self):
        with patch.object(gw, 'resolve_destination', lambda audience, **kw: '!ownerdm:ag2.space'):
            result, posts = self._team_delivered_then_late('BODY-C a later answer', owner_mentioned='true')
        self.assertFalse(result.exists())
        self.assertEqual(len(self.server.calls), posts, 'nothing reaches the room')
        self.assertIn('BODY-C a later answer', self.quarantined_bodies())
        self.assertNotIn('BODY-C a later answer', self._archived_bodies())
        said = [l for l in self.lines if UNSENT in l]
        self.assertEqual(len(said), 1, self.lines)
        self.assertIn("owner's DM", said[0])
        self.assertNotIn('by hand', said[0])

    def test_a_guard_that_cannot_decide_leaves_the_reply_live_and_a_withheld_one_is_never_handed_over(self):
        for name, patches, outcome in (
                ('mention routing unavailable', {'_owner_mention_disposition': None}, 'live'),
                ('guard unavailable', {'_guarded_result_body': (None, 'guard unavailable')}, 'live'),
                ('guard withheld a redaction', {'_guarded_result_body': ('BODY-C [redacted]', 'secret redacted')},
                 'review')):
            with self.subTest(case=name):
                self.setUp()
                stack = contextlib.ExitStack()
                with stack:
                    result, posts = self._delivered_then_late(
                        'BODY-C secret', before_late=lambda: [stack.enter_context(
                            patch.object(gw, fn, return_value=value)) for fn, value in patches.items()])
                self.assertEqual(len(self.server.calls), posts)
                self.assertFalse(any('cannot resend it: send it by hand' in l for l in self.lines), self.lines)
                if outcome == 'live':
                    self.assertEqual(result.read_text(), 'BODY-C secret')
                    self.assertEqual(self.quarantined_bodies(), [])
                else:
                    self.assertIn('BODY-C secret', self.quarantined_bodies())
                    self.assertTrue(any('withheld it (secret redacted)' in l for l in self.lines), self.lines)

    def test_a_different_restricted_reply_is_quarantined_for_review_not_sending(self):
        for body, says in (('[dm-only]\nprivate detail for the owner', "marked [dm-only]"),
                           ('[channel: !other:ag2.space]\nfor that room', '[channel: !other:ag2.space]')):
            with self.subTest(body=body):
                self.setUp()
                result, posts = self._delivered_then_late(body)
                self.assertIn(body, self.quarantined_bodies())
                self.assertNotIn(body, self._archived_bodies())
                said = [l for l in self.lines if UNSENT in l]
                self.assertEqual(len(said), 1, self.lines)
                self.assertIn('review it', said[0])
                self.assertIn(says, said[0])
                self.assertNotIn('by hand', said[0])

    def test_a_drain_delivered_reply_recovered_by_the_sweep_after_a_crash_is_archived(self):
        """The sweep recomposes it with its recovery label; the source decides, not the label."""
        class Crash(BaseException):
            pass
        cases = [(b, False, True) for b in ('BODY-A the reply', '[dm-only]\nBODY-A the reply',
                                            '[channel: !other:ag2.space]\nBODY-A the reply')]
        cases += [('BODY-A the reply', True, True), ('[dm-only]\nBODY-A the reply', True, False)]
        for body, legacy, archived in cases:
            with self.subTest(body=body, legacy_record=legacy):
                self.setUp()
                self.bridge()
                self.task()
                self.server.accepting = True
                result = self.result(body)
                with patch.object(gw, '_archive_result', side_effect=Crash):
                    with self.assertRaises(Crash):
                        gw._post_ready_results({TID})
                self.assertEqual(outbox.item_status(self.outbox, TID), 'DELIVERED')
                if legacy:
                    self._forget_the_source()
                posts = len(self.server.calls)
                self.bridge(self.core())
                os.utime(result, (time.time() - gw.ORPHAN_GRACE_S - 60,) * 2)
                gw._reconcile_orphan_results(set())
                self.assertFalse(result.exists())
                self.assertEqual(len(self.server.calls), posts, 'no second POST')
                if archived:
                    self.assertEqual(self.quarantined_bodies(), [])
                    self.assertIn(body, self._archived_bodies())
                else:                                    # fail closed: a marked source is unprovable
                    self.assertIn(body, self.quarantined_bodies())

    def test_a_byte_identical_late_copy_is_archived_as_a_duplicate(self):
        result, posts = self._delivered_then_late('BODY-A the reply')
        self.assertFalse(result.exists())
        self.assertEqual(self.quarantined_bodies(), [])
        self.assertEqual(len(list((self.results / 'archive').rglob(f'{TID}-*-late-duplicate*.txt'))), 1)
        self.assertEqual(len(self.server.calls), posts)

    def _publish_c(self, result):
        tmp = result.with_name('.producer.tmp')
        tmp.write_text('BODY-C newest reply')
        os.replace(tmp, result)

    def test_an_unrestorable_body_leaves_an_inert_queued_record(self):
        """No live result, no send: the drain and the sweep act only on a
        result file, and the body waits in undelivered/ for the operator."""
        outbox_cli, result = self._parked_on_a_host_that_cannot_restore()
        rc, out = self._requeue_with(outbox_cli, lambda: None)
        self.assertEqual(rc, 4)
        self.assertEqual(out['result'], 'requeued')
        self.assertEqual(self._drain_once_accepting(), [])
        gw._reconcile_orphan_results(set())
        self.assertEqual(len(self.server.calls), 1, 'only the refused first attempt')
        self.assertEqual(outbox.item_status(self.outbox, TID), 'QUEUED')
        self.assertEqual(self.quarantined_bodies(), ['BODY-A parked answer'])

    def test_a_recovery_precheck_error_never_blocks_delivery(self):
        # EIO from the results-dir precheck, through the real drain entry:
        # recovery skips and says so once; the ordinary result is still posted.
        def accept(method, path, payload):
            self.server.calls.append(dict(payload))
            return {'ok': True}
        self.server.request = accept
        self.bridge()
        self.task()
        other = self.result('fresh answer')
        real = Path.is_dir
        results = self.results

        def eio(self_):
            if self_ == results:
                raise OSError(5, 'Input/output error')
            return real(self_)
        with patch.object(Path, 'is_dir', eio):
            for _ in range(PASSES):
                gw._post_ready_results({TID})
        self.assertFalse(other.exists(), 'the ordinary result must still be delivered')
        self.assertEqual(len([c for c in self.server.calls if c.get('id') == TID]), 1)
        skipped = [l for l in self.lines if 'recovery skipped this pass' in l]
        self.assertEqual(len(skipped), 1, '\n'.join(self.lines))

    def test_the_adapter_boundary_fails_open_when_recovery_itself_raises(self):
        def accept(method, path, payload):
            self.server.calls.append(dict(payload))
            return {'ok': True}
        self.server.request = accept
        self.bridge()
        self.task()
        other = self.result('fresh answer')
        with patch.object(gw.disposal, 'recover_abandoned_claims',
                          side_effect=RuntimeError('recovery exploded')):
            for _ in range(PASSES):
                gw._post_ready_results({TID})
        self.assertFalse(other.exists(), 'the ordinary result must still be delivered')
        self.assertEqual(len([c for c in self.server.calls if c.get('id') == TID]), 1)
        failed = [l for l in self.lines if 'recovery failed' in l and 'delivery continues' in l]
        self.assertEqual(len(failed), 1, '\n'.join(self.lines))

    def test_one_damaged_claim_never_blocks_ordinary_delivery(self):
        def accept(method, path, payload):
            self.server.calls.append(dict(payload))
            return {'ok': True}
        self.server.request = accept
        self.bridge()
        self.task()
        self.task('task-other')
        self.result().rename(self.claim_name(self.dead_pid(), 1, '01d00005'))
        (self.results / 'undelivered').write_text('not a directory')
        other = self.result('fresh answer', 'task-other')
        for _ in range(PASSES):
            gw._post_ready_results({TID, 'task-other'})
        self.assertFalse(other.exists(), 'the ordinary result must still be delivered')
        self.assertTrue(any(c.get('id') == 'task-other' for c in self.server.calls))
        failures = [l for l in self.lines if 'could not recover' in l]
        self.assertEqual(len(failures), 1, '\n'.join(self.lines))
        self.assertEqual(len(self.claims()), 1, 'the body stays visible for the operator')

    def test_the_sweep_alone_recovers_an_abandoned_claim(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        self.result().rename(self.claim_name(self.dead_pid(), 1, '01d00006'))
        gw._last_orphan_sweep = 0.0
        gw._reconcile_orphan_results(set())
        self.assertEqual(self.claims(), [])
        self.assertEqual(len(self.quarantined()), 1)
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))

    def test_the_sweep_does_not_take_the_drains_live_claim(self):
        # While this process is between its two renames, a concurrent sweep
        # runs: only the active-claim register tells it the claim is held.
        self.bridge()
        self.task()
        result = self.result()
        body, gen = self.read(result)
        real = disposal._verify_fd
        swept = []

        def sweep_mid_move(fd, generation):
            if not swept:
                swept.append(True)
                claim = disposal.find_claims(self.results)
                self.assertEqual(len(claim), 1)
                gw._last_orphan_sweep = 0.0
                gw._reconcile_orphan_results(set())
                self.assertTrue(claim[0].exists(), 'the sweep took a live claim')
            return real(fd, generation)
        with patch.object(disposal, '_verify_fd', sweep_mid_move):
            self.deliver(body, result, gen)
        self.assertTrue(swept)
        self.assert_once(result, 'sweep during a live move')
        self.assertEqual(len(self.server.calls), 1)

    def test_equal_bytes_in_another_publication_do_not_silence_the_loser(self):
        # Simulated inode reuse: the same dev/ino and bytes under a new write
        # time is a later, distinct publication, so the loser's reply is lost.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        body, gen = self.read(result)
        os.utime(result, ns=(gen.mtime_ns + 1_000, gen.mtime_ns + 1_000))
        _park_in_quarantine(result, self.results)
        self.deliver(body, result, gen)
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('vanished', self.about()[0])
        self.assertEqual(len(self.quarantined()), 1, 'the other publication is untouched')

    def test_a_dead_owners_unverified_claim_goes_back_live(self):
        # The owner died after claiming but before checking: the claim names
        # OLD's digest while it holds NEWER, so NEWER is restored, not quarantined.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        self.result('NEWER BODY').rename(self.claim_name(self.dead_pid(), 1, '01d00007', body='OLD BODY'))
        gw._post_ready_results({TID})
        # Restored live first; the item is terminal, so the same pass then
        # disposes of NEWER under its own identity, once, with its own line.
        self.assertEqual(self.claims(), [])
        self.assertEqual(len(self.server.calls), 1, 'the park itself; a terminal item is never re-POSTed')
        self.assertEqual(len(self.quarantined()), 1, self.quarantined())
        self.assertEqual((self.results / 'undelivered' / self.quarantined()[0]).read_text(), 'NEWER BODY')
        self.assertEqual(len(self.about()), 2, '\n'.join(self.about()))
        self.assertIn('never verified', self.about()[0])
        self.assertIn('terminal', self.about()[1])

    def test_a_lock_the_loser_cannot_take_is_no_verdict_and_it_reports(self):
        # Both loser branches ask the owner for a verdict; a lock it cannot
        # take is reported, and the loser still says what it saw.
        self.bridge(self.park_without_disposing())
        self.task()
        result = self.result()
        _, gen = self.read(result)

        def no_lock(*_a, **_k):
            raise OSError(77, 'No locks available')
        with patch.object(disposal, 'disposed_copy_exists', no_lock), \
                patch.object(disposal, 'quarantine_generation',
                             lambda *_a, **_k: (_ for _ in ()).throw(disposal.GenerationReplaced('replaced'))):
            gw._quarantine_undelivered(result, TID, 'terminal', generation=gen)
        result.unlink()
        with patch.object(disposal, 'disposed_copy_exists', no_lock):
            gw._quarantine_undelivered(result, TID, 'terminal', generation=gen)
        self.assertEqual(len(self.about()), 4, '\n'.join(self.about()))
        self.assertEqual(sum('could not check for a disposed copy' in ln for ln in self.about()), 2)
        self.assertTrue(any('stays live' in ln for ln in self.about()))
        self.assertTrue(any('vanished' in ln for ln in self.about()))

    def test_a_claim_that_cannot_be_read_is_reported_and_the_body_stays_live(self):
        # EMFILE while checking the claim is not a replacement: the bridge says
        # the quarantine failed and the reply is back at its name.
        self.bridge()
        self.task()
        result = self.result()
        body, gen = self.read(result)
        def emfile(fd, generation):
            raise OSError(24, 'Too many open files')
        with patch.object(disposal, '_verify_fd', emfile):
            self.deliver(body, result, gen)
        self.assertTrue(result.exists(), 'the body must be back at its name')
        self.assertEqual(self.claims(), [])
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('leaving it in place', self.about()[0])
        self.assertNotIn('replaced', self.about()[0])

    def test_a_sweep_during_the_restore_rename_cannot_take_the_claim(self):
        # The .restore path is registered before it exists; a sweep that runs
        # at the rename sees it held and leaves it, and the owner finishes.
        self.bridge()
        self.task()
        result = self.result('OLD BODY')
        body, gen = self.read(result)
        result.unlink()
        self.result('NEWER BODY')
        real = os.rename
        swept = []

        def sweep_at_restore(src, dst, *a, **kw):
            out = real(src, dst, *a, **kw)
            if str(dst).endswith('.restore') and not swept:
                swept.append(disposal.owner_holds(Path(dst)))
                gw._last_orphan_sweep = 0.0
                with patch.object(os, 'rename', real):
                    gw._reconcile_orphan_results(set())
                self.assertTrue(Path(dst).exists(), 'the sweep took a live restore claim')
            return out
        with patch.object(os, 'rename', sweep_at_restore):
            self.deliver(body, result, gen)
        self.assertEqual(swept, [True])
        self.assertEqual(result.read_text(), 'NEWER BODY')
        self.assertEqual(self.claims(), [])
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('replaced', self.about()[0])

    def test_a_dead_owners_claim_is_recovered(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        dead = subprocess.Popen(['true']); dead.wait()  # a pid that has exited
        self.assertIs(outbox.process_identity(dead.pid).state, outbox.OwnerState.DEAD)
        result.rename(self.claim_name(dead.pid, 1, 'dead0001'))
        gw._post_ready_results({TID})
        self.assertEqual(self.claims(), [])
        self.assertEqual(len(self.quarantined()), 1, self.quarantined())
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))

    def test_a_live_owners_claim_is_left_alone_by_the_sweep(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        pid, start = self.live_other_owner()
        claim = self.claim_name(pid, start, '11e00001')
        result.rename(claim)
        for _ in range(PASSES):
            gw._post_ready_results({TID})
            gw._last_orphan_sweep = 0.0
            gw._reconcile_orphan_results({TID})
        self.assertTrue(claim.exists())
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(self.about(), [], '\n'.join(self.about()))

    def test_a_live_owners_claim_is_never_taken_on_age_alone(self):
        # A live owner's moves run under the lock, so a claim it still holds is
        # one it is still making: age says nothing about it.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        pid, start = self.live_other_owner()
        claim = self.claim_name(pid, start, '5fc00001',
                                acquired=time.time() - disposal.CLAIM_MAX_S - 1)
        result.rename(claim)
        for _ in range(PASSES):
            gw._post_ready_results({TID})
            gw._last_orphan_sweep = 0.0
            gw._reconcile_orphan_results({TID})
        self.assertTrue(claim.exists(), 'a live owner keeps its claim whatever its age')
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(self.about(), [], '\n'.join(self.about()))

    def test_an_owner_whose_state_cannot_be_read_ages_out(self):
        # pid 1 answers EPERM: neither alive nor dead to us, so the bound applies.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        # pid 1 reads UNKNOWN (EPERM) on macOS but ALIVE on the Linux runners: stub the probe.
        unknown = outbox.ProcessIdentity(1, outbox.OwnerState.UNKNOWN)
        probe = patch.object(disposal, 'process_identity', lambda pid: unknown)
        probe.start(); self.addCleanup(probe.stop)
        result = self.result()
        fresh = self.claim_name(1, 0, '5fc00002')
        result.rename(fresh)
        gw._post_ready_results({TID})
        self.assertTrue(fresh.exists(), 'within the bound an opaque owner is trusted')
        stale = self.claim_name(1, 0, '5fc00003', acquired=time.time() - disposal.CLAIM_MAX_S - 1)
        fresh.rename(stale)
        gw._post_ready_results({TID})
        self.assertEqual(self.claims(), [])
        self.assertEqual(len(self.quarantined()), 1, self.quarantined())
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))

    def test_a_dead_owners_claim_holding_the_losers_bytes_is_recovered_once(self):
        # The loser finds its bytes in a claim nobody holds: it recovers them
        # with one line instead of trusting a stranded copy.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        body, gen = self.read(result)
        dead = subprocess.Popen(['true']); dead.wait()
        result.rename(self.claim_name(dead.pid, 1, 'dead0002'))
        self.deliver(body, result, gen)
        self.assertEqual(self.claims(), [])
        self.assertEqual(len(self.quarantined()), 1, self.quarantined())
        self.assertEqual(len(self.about()), 1, '\n'.join(self.about()))
        self.assertIn('recovered', self.about()[0])

    def test_an_unrelated_copy_with_a_later_stamp_does_not_hide_a_lost_reply(self):
        # A quarantined OTHER body carries a timestamp after this attempt; the
        # reply this pass read is gone and must be reported, not assumed moved.
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        other = self.result('OTHER BODY')
        _park_in_quarantine(other, self.results, when=time.time_ns() + 10 ** 12)
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
        real = disposal.rename_noreplace

        def producer_first(src, dst, *a, **kw):
            if Path(dst) == result:
                self.result('NEWEST BODY')
            return real(src, dst, *a, **kw)
        with patch.object(disposal, 'rename_noreplace', producer_first):
            self.deliver(body, result, gen)
        self.assertEqual(result.read_text(), 'NEWEST BODY')
        bodies = sorted((self.results / 'undelivered' / n).read_text() for n in self.quarantined())
        self.assertEqual(bodies, ['NEWER BODY'])
        self.assertEqual(len(self.about()), 2, '\n'.join(self.about()))
        self.assertTrue(any('superseded' in l for l in self.about()))
        self.assertTrue(any('replaced' in l for l in self.about()))

    def test_an_unreadable_copy_is_skipped_while_matching(self):
        core = self.park_without_disposing()
        self.bridge(core)
        self.task()
        result = self.result()
        body, gen = self.read(result)
        q = self.results / 'undelivered'
        q.mkdir()
        (q / f'{TID}-5.txt').symlink_to(q / 'gone.txt')     # listed, not readable
        _park_in_quarantine(result, self.results, when=7)
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

    def test_without_a_primitive_a_newer_reply_kept_in_quarantine_is_not_called_live(self):
        # No no-replace rename: the newer reply found at the name cannot go back,
        # waits in undelivered/, and nothing claims it is live or superseded.
        self.bridge(self.park_without_disposing())
        self.task()
        result = self.result('OLD BODY')
        _, gen = self.read(result)
        os.unlink(result)
        result.write_text('NEWER BODY')
        with patch.object(disposal.undelivered_quarantine, '_RENAME', None), \
                patch.object(disposal.undelivered_quarantine, 'RENAME_PRIMITIVE', 'none'):
            gw._quarantine_undelivered(result, TID, 'terminal', generation=gen)
        self.assertFalse(result.exists(), '\n'.join(self.lines))
        self.assertEqual(self.quarantined_bodies(), ['NEWER BODY'])
        said = '\n'.join(self.lines)
        self.assertIn('requeue it to deliver', said)
        self.assertNotIn('stays live', said)
        self.assertNotIn('superseded', said)

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
        self.assertIn('replaced', self.about()[0])

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
