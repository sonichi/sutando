#!/usr/bin/env python3
"""A delivered result is archived only as the publication that was sent.

The drain reads a result, sends it, and then retires the file. A producer may
publish a newer reply under the same name in between; retiring by pathname
archived that newer reply as if it had been sent, and nothing ever delivered
it. Every archive site (the ordinary send, a lease-closing marker, a dedup
decision and the orphan sweep) must retire only the generation it read and
leave a replacement live.

Run: python3 tests/gateway-archive-the-sent-generation.test.py
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'packages' / 'ag2-sparrow'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import assert_hermetic, isolate_then_import  # noqa: E402
_GW, _IMPORT_READS = isolate_then_import()   # before anything else imports the bridge
from ag2_sparrow import outbox, remote_gateway_bridge as gw, undelivered_quarantine
# The canonical module, so the coverage gate (source = src) sees the archive run
# under the bridge; the vendored copy is pinned byte-equal by the delegation test.
sys.path.insert(0, str(REPO / 'src'))
from delivery import disposal
from ag2_sparrow.delivery_core import DeliveryCore, DesignAClaimBackend, RetryPolicy
from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceResultProvider

ROOM = '!same:ag2.space'
TID = 'task-archive1'
SENT = 'BODY-A sent'
NEWER = 'BODY-B newer'


class Gateway:
    """Accepts every result; `during` runs inside the POST, after the read."""

    def __init__(self):
        self.calls = []
        self.during = None

    def request(self, method, path, payload):
        self.calls.append(dict(payload))
        if self.during:
            self.during()
        return {'ok': True}


class ArchiveTheSentGeneration(unittest.TestCase):
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
        self.lines: list[str] = []
        for seen in (undelivered_quarantine._REPORTED, disposal._REPORTED):
            kept = set(seen)
            self.addCleanup(lambda s=seen, k=kept: (s.clear(), s.update(k)))
            seen.clear()
        core = DeliveryCore(
            DesignAClaimBackend(self.results / '.outbox', retry_schedule=outbox.RetrySchedule(),
                                republish_delivered=False),
            AG2SpaceResultProvider(self.server.request),
            RetryPolicy(max_attempts=5, defer_idempotent_resend=True))
        values = dict(RESULTS_DIR=self.results, ARCHIVE_RESULTS_DIR=self.results / 'archive',
                      UNDELIVERABLE_RESULTS_DIR=self.results / 'undelivered', TASKS_DIR=self.tasks,
                      _STATE=self.root / 'state', DEDUP_ALIAS_FILE=self.root / 'state' / 'aliases.json',
                      TASK_ROOMS_FILE=self.root / 'state' / 'rooms.json',
                      TASK_MEDIA_FILE=self.root / 'state' / 'media.json',
                      INFLIGHT_FILE=self.root / 'state' / 'inflight.json',
                      GATEWAY_INSTANCE='', _INST_SUFFIX='', _DELIVERY_CORE=core,
                      _req=self.server.request, _log=self.lines.append, disposal=disposal,
                      _last_orphan_sweep=0.0, _orphan_quarantine_logged=set(),
                      _WITHHELD_TASK_OUTPUT={}, _SETTLE_PENDING=set(), _SETTLE_SCANNED=set(),
                      _SETTLE_LOADED=False)
        stack = contextlib.ExitStack()
        for name, value in values.items():
            stack.enter_context(patch.object(gw, name, value, create=True))
        self.addCleanup(stack.close)
        (self.tasks / f'{TID}.txt').write_text(
            f'id: {TID}\nsource: ag2space\nchannel_id: {ROOM}\nuser_id: owner\n'
            'access_tier: owner\ntask: Same question\n')
        gw._record_task_room(TID, ROOM)

    # ── helpers ──────────────────────────────────────────────────────────────

    def live(self):
        return self.results / f'{TID}.txt'

    def publish(self, body=SENT):
        self.live().write_text(body)

    def replace_atomically(self):
        """A producer publishing a newer reply: write aside, then rename over."""
        tmp = self.results / f'.{TID}.tmp'
        tmp.write_text(NEWER)
        os.replace(tmp, self.live())

    def rewrite_in_place(self):
        with open(self.live(), 'w') as f:
            f.write(NEWER)

    def archived(self):
        d = self.results / 'archive'
        return sorted(p.read_text() for p in d.glob('*.txt')) if d.is_dir() else []

    def kept(self):
        d = self.results / 'undelivered'
        return sorted(p.read_text() for p in d.glob('*.txt')) if d.is_dir() else []

    def sent_bodies(self):
        return [c.get('body') for c in self.server.calls]

    def drain(self, inflight=None):
        inflight = {TID} if inflight is None else inflight
        gw._post_ready_results(inflight)
        return inflight

    @contextlib.contextmanager
    def replaced_after_the_claim(self):
        """A producer publishes a newer reply the instant the owner has taken
        the sent one off its name, before it is placed anywhere."""
        real = os.rename
        fired = []

        def rename(src, dst, *a, **k):
            out = real(src, dst, *a, **k)
            if not fired and Path(src) == self.live() and '.disposing-' in Path(dst).name:
                fired.append(1)
                tmp = self.results / f'.{TID}.tmp'
                tmp.write_text(NEWER)
                real(tmp, self.live())
            return out
        with patch('os.rename', rename):
            yield
        self.assertEqual(fired, [1], 'the schedule never reached the claim')

    @contextlib.contextmanager
    def without_a_no_replace_rename(self):
        """Patch the quarantine module the active lifecycle owner calls, and
        prove the patch reached it."""
        uq = disposal.undelivered_quarantine
        with patch.object(uq, '_RENAME', None), patch.object(uq, 'RENAME_PRIMITIVE', 'none'):
            self.assertTrue(disposal._cannot_put_back(), 'the patch missed the module the owner uses')
            yield

    @contextlib.contextmanager
    def task_archive_fails(self, tid=TID):
        """The task file's move into tasks/archive/ raises, as on a full or read-only disk."""
        real = Path.rename

        def rename(path, target, *a, **k):
            if Path(path).parent == self.tasks and Path(path).name == f'{tid}.txt':
                raise OSError(5, 'EIO')
            return real(path, target, *a, **k)
        with patch.object(Path, 'rename', rename):
            yield

    def assert_nothing_half_cleared(self, inflight):
        self.assertTrue((self.tasks / f'{TID}.txt').exists())
        self.assertIn(TID, inflight, 'the id was forgotten while its task is still executable')
        self.assertIn(TID, self.durable_inflight(), 'a restart would lose the guard on a live task')
        self.assertEqual(gw._load_task_rooms().get(TID), ROOM, 'the room was forgotten early')
        self.assertFalse(any('the task is retired' in ln for ln in self.lines), 'retirement was claimed')

    def assert_task_retired(self, inflight):
        self.assertNotIn(TID, inflight, 'the id is still looked for')
        self.assertNotIn(TID, self.durable_inflight(), 'a restart would look for the id again')
        self.assertFalse((self.tasks / f'{TID}.txt').exists(), 'the task is still pending')
        self.assertTrue((self.tasks / 'archive' / f'{TID}.txt').exists())

    def reasks(self):
        return sorted(p.stem for p in self.tasks.glob('task-*.txt') if p.stem != TID)

    def aliases(self):
        f = self.root / 'state' / 'aliases.json'
        return json.loads(f.read_text()) if f.exists() else {}

    def durable_inflight(self):
        f = self.root / 'state' / 'inflight.json'
        return json.loads(f.read_text()) if f.exists() else []

    def sweep(self, inflight):
        gw._last_orphan_sweep = 0.0
        with patch.object(gw, 'ORPHAN_GRACE_S', 0.0), patch.object(gw, 'ORPHAN_MAX_AGE_S', 1e9):
            gw._reconcile_orphan_results(inflight)
        return inflight

    def assert_newer_stays_live(self, inflight=None):
        self.assertTrue(self.live().exists(), 'the newer reply lost its name')
        self.assertEqual(self.live().read_text(), NEWER)
        self.assertNotIn(NEWER, self.archived(), 'a reply never sent was archived as sent')
        if inflight is not None:
            self.assertIn(TID, inflight, 'the newer reply is no longer looked for')
        self.assertTrue((self.tasks / f'{TID}.txt').exists(),
                        'the task was archived while its newer reply waits')

    # ── the ordinary send ────────────────────────────────────────────────────

    def test_an_unraced_send_archives_the_sent_body_and_the_task(self):
        self.publish()
        inflight = self.drain()
        self.assertEqual(self.sent_bodies(), [SENT])
        self.assertEqual(self.archived(), [SENT])
        self.assertFalse(self.live().exists())
        self.assertNotIn(TID, inflight)
        self.assertTrue((self.tasks / 'archive' / f'{TID}.txt').exists())

    def test_a_reply_published_over_the_name_during_the_post_stays_live(self):
        self.publish()
        self.server.during = self.replace_atomically
        inflight = self.drain()
        self.assertEqual(self.sent_bodies(), [SENT])
        self.assert_newer_stays_live(inflight)
        self.assertEqual(self.archived(), [], 'only the sent body may be archived, and it is gone')
        self.assertEqual(len([ln for ln in self.lines if 'replaced at its name before it was archived' in ln]), 1)

    def test_a_reply_rewritten_in_place_during_the_post_stays_live(self):
        self.publish()
        self.server.during = self.rewrite_in_place
        inflight = self.drain()
        self.assertEqual(self.sent_bodies(), [SENT])
        self.assert_newer_stays_live(inflight)

    # ── a lease-closing marker ───────────────────────────────────────────────

    def test_a_lease_close_does_not_archive_a_replacement(self):
        self.publish('[no-send]\nhandled elsewhere')
        self.server.during = self.replace_atomically
        inflight = self.drain()
        self.assertEqual(len(self.server.calls), 1)
        self.assertTrue(self.server.calls[0].get('no_send'))
        self.assert_newer_stays_live(inflight)

    # ── a dedup decision ─────────────────────────────────────────────────────

    def test_a_dedup_decision_does_not_archive_a_replacement(self):
        self.publish('[deduped: task-holder1]')
        real = gw.plan_dedup_recovery

        def plan(*a, **k):
            out = real(*a, **k)
            self.replace_atomically()
            return out
        with patch.object(gw, 'plan_dedup_recovery', plan):
            inflight = self.drain()
        self.assertEqual(self.server.calls, [])
        self.assert_newer_stays_live(inflight)
        self.assertEqual(len(self.reasks()), 1)
        self.assertIn(self.reasks()[0], self.durable_inflight(), 'the published re-ask is not tracked')

    def test_a_failed_dedup_archive_retried_reuses_one_tracked_reask(self):
        self.publish('[deduped: task-holder1]')
        (self.results / 'archive').write_text('not a directory')
        inflight = self.drain()
        reask = self.reasks()
        self.assertEqual(len(reask), 1)
        self.assertIn(reask[0], self.durable_inflight(), 'the re-ask was published before it was tracked')
        self.drain(inflight)
        self.assertEqual(self.reasks(), reask, 'a retried decision published another re-ask')
        (self.tasks / 'archive').mkdir()                    # the core picked the re-ask up
        os.replace(self.tasks / f'{reask[0]}.txt', self.tasks / 'archive' / f'{reask[0]}.txt')
        self.drain(inflight)
        self.assertEqual(self.reasks(), [], 'a re-ask already taken was published again')
        self.assertEqual(sorted(self.aliases()), reask, 'a retried decision committed another alias')
        self.assertIn(TID, inflight)
        (self.results / 'archive').unlink()
        self.drain(inflight)
        self.assertNotIn(TID, inflight)
        self.assertEqual(self.reasks(), [])
        self.assertIn(reask[0], inflight)
        self.assertIn(reask[0], self.durable_inflight())

    def test_a_dedup_replaced_after_the_claim_twice_keeps_one_tracked_reask(self):
        self.publish('[deduped: task-holder1]')
        with self.replaced_after_the_claim():
            inflight = self.drain()
        reask = self.reasks()
        self.assertEqual(len(reask), 1)
        self.assert_newer_stays_live(inflight)
        self.publish('[deduped: task-holder1]')
        with self.replaced_after_the_claim():
            self.drain(inflight)
        self.assertEqual(self.reasks(), reask, 'a retried decision published another re-ask')
        self.assertEqual(sorted(self.aliases()), reask)
        self.assertIn(reask[0], self.durable_inflight())
        self.assert_newer_stays_live(inflight)

    def test_a_reask_whose_temp_cleanup_fails_is_still_published(self):
        self.publish('[deduped: task-holder1]')
        real = Path.unlink

        def unlink(path, *a, **k):
            if path.name.startswith('.task-') and path.name.endswith('.tmp'):
                raise OSError(5, 'EIO')
            return real(path, *a, **k)
        with patch.object(Path, 'unlink', unlink):
            inflight = self.drain()
        reask = self.reasks()
        self.assertEqual(len(reask), 1, 'the published re-ask is missing')
        self.assertEqual(self.server.calls, [], 'a re-ask that went out was reported as failed')
        self.assertFalse(self.live().exists(), 'the decision was not retired')
        self.assertIn(reask[0], inflight)
        self.assertIn(reask[0], self.durable_inflight())
        self.assertNotIn(TID, inflight)

    def test_two_planners_racing_the_watcher_publish_one_executable_reask(self):
        import sys
        import threading
        self.publish('[deduped: task-holder1]')
        reask = gw._reask_id(TID)
        planner = sys.modules[gw.plan_dedup_recovery.__module__]
        real_link, real_published = os.link, planner._published_as
        in_link, b_checked, a_done, first = threading.Event(), threading.Event(), threading.Event(), []

        def link(src, dst, *a, **k):
            if Path(dst).name == f'{reask}.txt' and not first:
                first.append(1)
                in_link.set()
                b_checked.wait(1.0)                     # unserialised, B checks right here
                out = real_link(src, dst, *a, **k)
                (self.tasks / 'archive').mkdir(exist_ok=True)
                os.replace(dst, self.tasks / 'archive' / f'{reask}.txt')   # the watcher takes it
                a_done.set()
                return out
            return real_link(src, dst, *a, **k)

        def published(*a, **k):
            seen = real_published(*a, **k)
            if threading.current_thread().name == 'B':
                b_checked.set()
                a_done.wait(1.0)                        # unserialised, A links and the watcher moves it now
            return seen
        with patch('os.link', link), patch.object(planner, '_published_as', published):
            a = threading.Thread(target=gw._dedup_plan, args=(TID, 'task-holder1', set()), name='A')
            a.start()
            self.assertTrue(in_link.wait(5))
            b = threading.Thread(target=gw._dedup_plan, args=(TID, 'task-holder1', set()), name='B')
            b.start()
            a.join(10)
            b.join(10)
        copies = [p for p in (self.tasks / f'{reask}.txt', self.tasks / 'archive' / f'{reask}.txt') if p.exists()]
        self.assertEqual(len(copies), 1, f'executable copies of one re-ask: {copies}')

    def test_a_busy_results_lock_defers_the_dedup_decision(self):
        self.publish('[deduped: task-holder1]')

        def busy(_d):
            raise disposal.DisposalBusy('held')
        with patch.object(disposal, 'locked', busy):
            inflight = self.drain()
        self.assertEqual(self.reasks(), [], 'a re-ask was published without the lock')
        self.assertEqual(self.live().read_text(), '[deduped: task-holder1]')
        self.assertEqual(inflight, {TID})
        self.assertTrue(any('deferred' in ln for ln in self.lines), self.lines)

    def test_a_reask_that_cannot_be_tracked_is_never_published(self):
        self.publish('[deduped: task-holder1]')
        with patch.object(gw, '_save_inflight', return_value=False):
            inflight = self.drain()
        self.assertEqual(self.reasks(), [], 'an untracked re-ask was published')
        self.assertEqual(self.live().read_text(), '[deduped: task-holder1]', 'the decision was retired')
        self.assertEqual(inflight, {TID})
        self.drain(inflight)
        self.assertEqual(len(self.reasks()), 1)
        self.assertFalse(self.live().exists())

    # ── the orphan sweep ─────────────────────────────────────────────────────

    def test_an_orphan_recovery_does_not_archive_a_replacement(self):
        self.publish()
        self.server.during = self.replace_atomically
        with patch.object(gw, 'ORPHAN_GRACE_S', 0.0), patch.object(gw, 'ORPHAN_MAX_AGE_S', 1e9):
            gw._reconcile_orphan_results(set())
        self.assertEqual(len(self.server.calls), 1)
        self.assert_newer_stays_live()

    def test_an_unraced_orphan_recovery_archives_it(self):
        self.publish()
        with patch.object(gw, 'ORPHAN_GRACE_S', 0.0), patch.object(gw, 'ORPHAN_MAX_AGE_S', 1e9):
            gw._reconcile_orphan_results(set())
        self.assertEqual(len(self.server.calls), 1)
        self.assertFalse(self.live().exists())
        self.assertEqual(len(self.archived()), 1)

    # ── a reply published after the claim, at every archive site ─────────────

    def test_the_ordinary_send_keeps_a_reply_published_after_the_claim(self):
        self.publish()
        with self.replaced_after_the_claim():
            inflight = self.drain()
        self.assertEqual(self.sent_bodies(), [SENT])
        self.assertEqual(self.archived(), [SENT], 'the sent generation itself is retired')
        self.assert_newer_stays_live(inflight)

    def test_a_lease_close_keeps_a_reply_published_after_the_claim(self):
        marker = '[no-send]\nhandled elsewhere'
        self.publish(marker)
        with self.replaced_after_the_claim():
            inflight = self.drain()
        self.assertEqual(self.archived(), [marker])
        self.assert_newer_stays_live(inflight)

    def test_a_dedup_decision_keeps_a_reply_published_after_the_claim(self):
        self.publish('[deduped: task-holder1]')
        with self.replaced_after_the_claim():
            inflight = self.drain()
        self.assert_newer_stays_live(inflight)
        self.assertEqual(len(self.reasks()), 1)
        self.assertIn(self.reasks()[0], self.durable_inflight())

    def test_an_orphan_recovery_tracks_a_reply_published_after_the_claim(self):
        self.publish()
        with self.replaced_after_the_claim():
            inflight = self.sweep(set())
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(self.archived(), [SENT])
        self.assert_newer_stays_live(inflight)
        self.assertIn(TID, json.loads((self.root / 'state' / 'inflight.json').read_text()),
                      'the id is looked for only until the process restarts')
        self.sweep(inflight)
        self.assert_newer_stays_live(inflight)

    def restart(self):
        """A fresh process: only the durable in-flight file survives."""
        gw._SETTLE_PENDING.clear()
        gw._SETTLE_SCANNED.clear()
        gw._SETTLE_LOADED = False
        gw._WITHHELD_TASK_OUTPUT.clear()
        return set(self.durable_inflight())

    def assert_kept_for_a_person(self, body):
        self.assertNotIn(body, self.archived(), 'a never-sent reply was archived as a late duplicate')
        self.assertIn(body, self.kept(), 'the never-sent reply has no surviving copy')
        self.assertFalse(self.live().exists())
        self.assertFalse(any('requeue' in ln and 'restores it' in ln for ln in self.lines),
                         'a requeue that cannot work for a delivered id was advertised')

    def test_a_team_reply_after_a_withheld_one_is_judged_as_itself(self):
        safe_c = 'BODY-C a safe reply'
        (self.tasks / f'{TID}.txt').write_text(
            f'id: {TID}\nsource: ag2space\nchannel_id: {ROOM}\nuser_id: @alice:ag2.space\n'
            'access_tier: team\ntask: Same question\n')
        routed = []
        with patch.object(gw, '_route_withheld_review',
                          side_effect=lambda p: routed.append(json.loads(p.read_text())['withheld_body']) or True):
            self.publish()
            self.server.during = lambda: (self.live().write_text('[file: /tmp/b.txt]\nBODY-B'))
            inflight = self.drain()
            self.server.during = None
            self.drain(inflight)                         # B: withheld for the owner's review
            self.assertEqual(routed, ['[file: /tmp/b.txt]\nBODY-B'])
            self.assert_task_retired(inflight)
            self.publish(safe_c)
            self.sweep(inflight)
        visible = [c.get('body') for c in self.server.calls if not c.get('no_send')]
        self.assertEqual(visible, [SENT], 'a reply other than A reached the room')
        self.assertNotIn(safe_c, [c.get('body') for c in self.server.calls])
        self.assertNotIn(safe_c, self.archived(), "C was archived on B's verdict")
        self.assertIn(safe_c, self.kept(), 'C was not kept for a person')
        self.assertEqual(routed, ['[file: /tmp/b.txt]\nBODY-B'], 'C was reviewed as B')

    # ── a placement that fails ───────────────────────────────────────────────

    def test_an_archive_directory_missing_at_placement_is_not_an_archive(self):
        self.publish()
        real = disposal._move_into_quarantine

        def vanish(src, dst, log):
            Path(dst).parent.rmdir()
            return real(src, dst, log)
        with patch.object(disposal, '_move_into_quarantine', vanish):
            inflight = self.drain()
        self.assertEqual(self.live().read_text(), SENT, 'the sent reply lost its name')
        self.assertIn(TID, inflight)
        self.assertTrue((self.tasks / f'{TID}.txt').exists(), 'the task was archived without its result')
        self.assertEqual(len([ln for ln in self.lines if 'could not be archived' in ln]), 1, self.lines)
        self.drain(inflight)
        self.assertEqual(self.sent_bodies(), [SENT], 'a delivered result was sent twice')
        self.assertEqual(self.archived(), [SENT])
        self.assertNotIn(TID, inflight)

    def test_a_placement_failure_after_the_name_is_retaken_keeps_both(self):
        self.publish()
        real = disposal._move_into_quarantine
        failed = []

        def fail_once(src, dst, log):
            if not failed:
                failed.append(1)
                self.replace_atomically()
                raise OSError(5, 'EIO')
            return real(src, dst, log)
        with patch.object(disposal, '_move_into_quarantine', fail_once):
            inflight = self.drain()
        self.assertEqual(self.archived(), [])
        self.assertEqual(self.kept(), [SENT], 'the sent reply has no surviving name')
        self.assert_newer_stays_live(inflight)
        said = [ln for ln in self.lines if 'kept outside the archive' in ln]
        self.assertEqual(len(said), 1, self.lines)
        self.assertIn('undelivered/', said[0], 'the log does not name where the reply is')

    def test_every_archive_name_taken_is_not_an_archive(self):
        self.publish()
        d = self.results / 'archive'
        d.mkdir()
        for n in ('one.txt', 'two.txt'):
            (d / n).write_text('earlier evidence')
        with patch.object(gw, '_names', lambda base: iter(['one.txt', 'two.txt'])):
            inflight = self.drain()
        self.assertEqual(self.live().read_text(), SENT)
        self.assertEqual(self.archived(), ['earlier evidence', 'earlier evidence'])
        self.assertIn(TID, inflight)
        self.assertTrue((self.tasks / f'{TID}.txt').exists())

    # ── the pass after a race ────────────────────────────────────────────────

    def test_the_next_pass_never_resends_the_sent_reply(self):
        self.publish()
        self.server.during = self.replace_atomically
        inflight = self.drain()
        self.server.during = None
        self.drain(inflight)
        self.assertEqual(self.sent_bodies().count(SENT), 1, 'the sent reply was sent twice')
        surviving = self.archived() + self.kept() + ([self.live().read_text()] if self.live().exists() else [])
        self.assertIn(NEWER, surviving, 'the newer reply has no surviving name')

    def test_a_replacement_kept_for_a_person_retires_its_task(self):
        self.publish()
        self.server.during = self.replace_atomically
        inflight = self.drain()
        self.server.during = None
        self.assertIn(TID, inflight)
        self.drain(inflight)
        self.assertEqual(self.kept(), [NEWER])
        self.assert_task_retired(inflight)
        for _ in range(3):
            self.drain(inflight)
            gw._reconcile_abandoned(inflight, set())
        self.assertEqual(self.sent_bodies(), [SENT], 'B was sent')
        self.assertEqual(self.kept(), [NEWER])
        self.assertTrue(any('the task is retired' in ln for ln in self.lines), self.lines)

    def test_a_reply_published_during_the_quarantine_keeps_the_task(self):
        self.publish()
        self.server.during = self.replace_atomically
        inflight = self.drain()
        self.server.during = None
        real = gw._quarantine_unsent
        newest = 'BODY-C newest'

        def then_publish(*a, **k):
            real(*a, **k)
            self.publish(newest)
        with patch.object(gw, '_quarantine_unsent', then_publish):
            self.drain(inflight)
        self.assertEqual(self.live().read_text(), newest)
        self.assertIn(TID, inflight, 'a live reply lost its tracking')
        self.assertTrue((self.tasks / f'{TID}.txt').exists(), 'the task was retired under a live reply')

    def test_an_id_that_was_never_delivered_is_not_settled(self):
        import urllib.error
        self.publish()

        def vanish_then_fail():
            self.live().unlink()
            raise urllib.error.URLError('down')
        self.server.during = vanish_then_fail
        inflight = self.drain()
        self.assertNotEqual(outbox.read_item(self.results / '.outbox', TID).get('status'), 'DELIVERED')
        self.assertIn(TID, inflight)
        self.assertTrue((self.tasks / f'{TID}.txt').exists(), 'an undelivered task was retired')

    def test_a_task_archive_that_fails_changes_nothing_and_is_retried(self):
        gw._save_inflight({TID})                         # as the poll loop does when the task arrives
        self.publish()
        self.server.during = self.replace_atomically
        inflight = self.drain()
        self.server.during = None
        with self.task_archive_fails():
            self.drain(inflight)
            self.drain(inflight)
        self.assertEqual(self.kept(), [NEWER])
        self.assert_nothing_half_cleared(inflight)
        self.drain(inflight)                             # the disk recovered: the retry settles it
        self.assert_task_retired(inflight)
        self.assertEqual(self.sent_bodies(), [SENT])

    def test_a_task_archive_that_fails_is_settled_after_a_restart(self):
        gw._save_inflight({TID})                         # as the poll loop does when the task arrives
        self.publish()
        self.server.during = self.replace_atomically
        inflight = self.drain()
        self.server.during = None
        with self.task_archive_fails():
            self.drain(inflight)
        self.assert_nothing_half_cleared(inflight)
        restarted = set(self.durable_inflight())         # a fresh process reloads only the ledger
        self.assertIn(TID, restarted)
        self.drain(restarted)
        self.assert_task_retired(restarted)
        self.assertEqual(self.sent_bodies(), [SENT], 'the served task ran again')

    def test_an_orphan_recovery_that_cannot_be_tracked_moves_nothing_until_a_restart(self):
        self.publish()
        with patch.object(gw, '_save_inflight', return_value=False), self.task_archive_fails():
            self.sweep(set())
        self.assertEqual(self.server.calls, [], 'a recovery was sent while its id could not be tracked')
        self.assertEqual(self.live().read_text(), SENT, 'the only restart-durable trace was moved')
        self.assertTrue((self.tasks / f'{TID}.txt').exists())
        restarted = self.restart()
        self.sweep(restarted)
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(self.archived(), [SENT])
        self.assert_task_retired(restarted)

    def test_an_id_that_could_not_be_made_durable_is_not_left_in_memory(self):
        self.publish()
        inflight = set()
        with patch.object(gw, '_save_inflight', return_value=False):
            self.sweep(inflight)
            self.assertNotIn(TID, inflight, 'an id that is not durable was left in memory')
            with self.task_archive_fails():
                self.drain(inflight)
        self.assertEqual(self.server.calls, [], 'a drain sent a result whose id was never durable')
        restarted = self.restart()
        for _ in range(2):
            self.sweep(restarted)
            self.drain(restarted)
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(self.archived(), [SENT])
        self.assert_task_retired(restarted)

    def test_an_id_is_invisible_to_the_outbound_drain_until_its_write_commits(self):
        import threading
        self.publish()
        inflight = set()
        real, entered, release = gw._durable_write, threading.Event(), threading.Event()

        def write(path, text):
            if Path(path) == gw.INFLIGHT_FILE and TID in text and not entered.is_set():
                entered.set()
                release.wait(5)
                return False                             # the write that held the id fails
            return real(path, text)
        with patch.object(gw, '_durable_write', write):
            sweep = threading.Thread(target=self.sweep, args=(inflight,))
            sweep.start()
            self.assertTrue(entered.wait(5), 'the sweep never wrote the id')
            self.assertNotIn(TID, inflight, 'the id is visible before it is durable')
            with self.task_archive_fails():
                self.drain(inflight)                     # the outbound thread runs in the window
            release.set()
            sweep.join(5)
        self.assertEqual(self.server.calls, [], 'a result was sent while its id was durable nowhere')
        self.assertEqual(self.live().read_text(), SENT)
        restarted = self.restart()
        self.sweep(restarted)
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(self.archived(), [SENT])
        self.assert_task_retired(restarted)

    def test_an_alias_whose_result_vanished_is_settled_after_a_restart(self):
        self.publish('[deduped: task-holder1]')
        inflight = self.drain()
        reask = gw._reask_id(TID)
        rfile = self.results / f'{reask}.txt'
        rfile.write_text('BODY-R the re-asked answer')
        self.server.during = lambda: rfile.unlink()     # the result is gone when it is archived
        with self.task_archive_fails(reask):
            self.drain(inflight)
        self.server.during = None
        self.assertEqual(len(self.server.calls), 1)
        self.assertFalse(rfile.exists())
        self.assertTrue((self.tasks / f'{reask}.txt').exists())
        self.assertIn(reask, self.durable_inflight())
        restarted = self.restart()
        for _ in range(2):
            self.drain(restarted)
        self.assertFalse((self.tasks / f'{reask}.txt').exists(), 'the alias was never settled after a restart')
        self.assertNotIn(reask, restarted)
        self.assertNotIn(reask, self.durable_inflight())
        self.assertNotIn(reask, gw._SETTLE_PENDING)
        self.assertEqual(len(self.server.calls), 1)

    @contextlib.contextmanager
    def settle_intent_write_fails(self):
        real = gw._durable_write

        def write(path, text):
            if Path(path) == gw._settle_pending_file():
                return False
            return real(path, text)
        with patch.object(gw, '_durable_write', write):
            yield

    def _alias_whose_result_vanished(self):
        self.publish('[deduped: task-holder1]')
        inflight = self.drain()
        reask = gw._reask_id(TID)
        rfile = self.results / f'{reask}.txt'
        rfile.write_text('BODY-R the re-asked answer')
        self.server.during = lambda: rfile.unlink()
        return reask, rfile, inflight

    def test_a_settle_intent_that_cannot_be_saved_moves_nothing_and_is_retried(self):
        self.publish()
        gw._save_inflight({TID})
        with self.settle_intent_write_fails():
            inflight = self.drain()
        self.assertEqual(self.sent_bodies(), [SENT])
        self.assertEqual(self.live().read_text(), SENT, 'the result moved with no durable settle intent')
        self.assertTrue((self.tasks / f'{TID}.txt').exists())
        self.assertIn(TID, self.durable_inflight())
        restarted = self.restart()
        self.drain(restarted)
        self.assertEqual(self.archived(), [SENT])
        self.assert_task_retired(restarted)
        self.assertEqual(self.sent_bodies(), [SENT], 'the served result was sent twice')

    def test_a_vanished_alias_whose_intent_save_fails_is_settled_once_the_save_recovers(self):
        reask, rfile, inflight = self._alias_whose_result_vanished()
        with self.settle_intent_write_fails(), self.task_archive_fails(reask):
            self.drain(inflight)
        self.server.during = None
        self.assertTrue((self.tasks / f'{reask}.txt').exists())
        self.drain(inflight)                                 # the disk recovered, same process
        self.assertFalse((self.tasks / f'{reask}.txt').exists(), 'the served alias stayed executable')
        self.assertNotIn(reask, self.durable_inflight())
        self.assertEqual(len(self.server.calls), 1)

    def test_a_vanished_alias_stranded_by_a_restart_is_never_sent_twice(self):
        reask, rfile, inflight = self._alias_whose_result_vanished()
        with self.settle_intent_write_fails(), self.task_archive_fails(reask):
            self.drain(inflight)
        self.server.during = None
        restarted = self.restart()                           # the save never recovered before it
        self.drain(restarted)
        self.assertTrue((self.tasks / f'{reask}.txt').exists(), 'no evidence is left, so it waits')
        rfile.write_text('BODY-R2 the core ran it again')    # the core re-runs the live task
        self.drain(restarted)
        self.assertEqual(len(self.server.calls), 1, 'a second answer was posted under a delivered id')
        self.assertIn('BODY-R2 the core ran it again', self.kept(), 'the rerun was not kept for review')
        self.assertFalse((self.tasks / f'{reask}.txt').exists())
        self.assertNotIn(reask, self.durable_inflight())

    def test_a_save_before_the_first_drain_keeps_a_previous_processs_ids(self):
        gw._durable_write(gw._settle_pending_file(), json.dumps({'ids': ['task-fromthelastprocess']}))
        gw._SETTLE_LOADED = False
        gw._task_left_executable('task-fromthisprocess')
        self.assertEqual(json.loads(gw._settle_pending_file().read_text())['ids'],
                         ['task-fromthelastprocess', 'task-fromthisprocess'])

    def _seed_ledger(self, ids):
        gw._durable_write(gw._settle_pending_file(), json.dumps({'ids': ids}))
        gw._SETTLE_LOADED = False
        gw._SETTLE_PENDING.clear()

    def ledger(self):
        return json.loads(gw._settle_pending_file().read_text())['ids']

    def test_a_ledger_that_cannot_be_read_is_never_written_over(self):
        for failure in ('transient read error', 'corrupt json', 'ids not a list'):
            with self.subTest(failure=failure):
                self._seed_ledger(['task-oldserved'])
                if failure == 'transient read error':
                    real, failed = gw._read_settle_ledger, []

                    def read(path):
                        if not failed:
                            failed.append(1)
                            raise OSError(5, 'EIO')
                        return real(path)
                    ctx = patch.object(gw, '_read_settle_ledger', read)
                else:
                    bad = '{not json' if failure == 'corrupt json' else '{"ids": "task-oldserved"}'
                    raw = gw._settle_pending_file().read_text()
                    gw._settle_pending_file().write_text(bad)
                    ctx = contextlib.nullcontext()
                with ctx:
                    gw._task_left_executable('task-new')
                    self.assertFalse(gw._SETTLE_LOADED, 'the ledger was marked read without being read')
                    if failure != 'transient read error':
                        self.assertEqual(gw._settle_pending_file().read_text(), bad,
                                         'an unreadable ledger was written over')
                        gw._settle_pending_file().write_text(raw)
                    gw._task_left_executable('task-newer')   # readable again: merged, not replaced
                self.assertEqual(self.ledger(), ['task-new', 'task-newer', 'task-oldserved'])

    def test_two_threads_saving_first_never_lose_a_previous_processs_ids(self):
        import threading
        self._seed_ledger(['task-oldserved'])
        real, entered, release = gw._read_settle_ledger, threading.Event(), threading.Event()

        def read(path):
            entered.set()
            release.wait(5)
            return real(path)
        with patch.object(gw, '_read_settle_ledger', read):
            a = threading.Thread(target=gw._task_left_executable, args=('task-newa',))
            a.start()
            self.assertTrue(entered.wait(5))
            b = threading.Thread(target=gw._task_left_executable, args=('task-newb',))
            b.start()
            b.join(0.3)
            self.assertTrue(b.is_alive(), 'a second save ran while the first load was unresolved')
            release.set()
            a.join(5)
            b.join(5)
        self.assertEqual(self.ledger(), ['task-newa', 'task-newb', 'task-oldserved'])
        self.assertEqual(gw._SETTLE_PENDING, {'task-newa', 'task-newb', 'task-oldserved'})

    def test_a_third_reply_after_a_restart_is_kept_and_its_task_retired(self):
        newest = 'BODY-C newest'
        self.publish()
        with self.replaced_after_the_claim():
            self.sweep(set())
        self.assertEqual(self.archived(), [SENT])
        self.assertIn(TID, self.durable_inflight(), 'the replacement is not tracked across a restart')
        tmp = self.results / f'.{TID}.tmp'
        tmp.write_text(newest)
        os.replace(tmp, self.live())
        restarted = self.restart()
        self.drain(restarted)
        self.assert_kept_for_a_person(newest)
        self.assertEqual(len(self.server.calls), 1, 'a restart posted under a delivered id')
        self.assert_task_retired(restarted)

    def _late_reply_with_its_task_back_in_the_queue(self):
        gw._save_inflight({TID})
        self.publish()
        self.drain()                                     # A sent, its task archived normally
        os.replace(self.tasks / 'archive' / f'{TID}.txt', self.tasks / f'{TID}.txt')
        self.publish(NEWER)

    def test_an_orphan_arm_failing_to_track_and_to_archive_moves_nothing_until_a_restart(self):
        self._late_reply_with_its_task_back_in_the_queue()
        with patch.object(gw, '_save_inflight', return_value=False), self.task_archive_fails():
            self.sweep(set())
        self.assertEqual(self.live().read_text(), NEWER, 'the only restart-durable trace was moved')
        self.assertTrue((self.tasks / f'{TID}.txt').exists())
        restarted = self.restart()
        self.sweep(restarted)
        self.assert_kept_for_a_person(NEWER)
        self.assert_task_retired(restarted)
        self.assertEqual(len(self.server.calls), 1)

    def test_an_orphan_arm_whose_task_archive_fails_is_settled_after_a_restart(self):
        self._late_reply_with_its_task_back_in_the_queue()
        with self.task_archive_fails():
            self.sweep(set())
        self.assertEqual(self.kept(), [NEWER])
        self.assertTrue((self.tasks / f'{TID}.txt').exists())
        self.assertIn(TID, self.durable_inflight(), 'the retry marker is not restart-durable')
        restarted = self.restart()
        self.drain(restarted)
        self.assert_task_retired(restarted)
        self.assertEqual(len(self.server.calls), 1)

    def test_a_normal_send_whose_task_archive_fails_keeps_every_guard(self):
        gw._save_inflight({TID})
        self.publish()
        with self.task_archive_fails():
            inflight = self.drain()
        self.assertEqual(self.sent_bodies(), [SENT])
        self.assertEqual(self.archived(), [SENT])
        self.assert_nothing_half_cleared(inflight)
        self.drain(inflight)
        self.assert_task_retired(inflight)
        self.assertEqual(self.sent_bodies(), [SENT])

    def test_a_normal_send_whose_task_archive_fails_is_settled_after_a_restart(self):
        gw._save_inflight({TID})
        self.publish()
        with self.task_archive_fails():
            inflight = self.drain()
        self.assert_nothing_half_cleared(inflight)
        restarted = self.restart()
        self.drain(restarted)
        self.assert_task_retired(restarted)
        self.assertEqual(self.sent_bodies(), [SENT], 'the served task ran again')

    def _reask_answered_with_its_task_archive_failing(self):
        self.publish('[deduped: task-holder1]')
        inflight = self.drain()
        reask = gw._reask_id(TID)
        self.assertIn(reask, inflight)
        (self.results / f'{reask}.txt').write_text('BODY-R the re-asked answer')
        with self.task_archive_fails(reask):
            self.drain(inflight)
        self.assertEqual(self.sent_bodies(), ['BODY-R the re-asked answer'])
        self.assertTrue((self.tasks / f'{reask}.txt').exists())
        self.assertIn(reask, inflight)
        self.assertIn(reask, self.durable_inflight())
        return reask, inflight

    def test_a_reask_whose_task_archive_fails_is_retried_by_its_own_id(self):
        reask, inflight = self._reask_answered_with_its_task_archive_failing()
        self.drain(inflight)
        self.assertFalse((self.tasks / f'{reask}.txt').exists(), 'the re-ask was never retried')
        self.assertNotIn(reask, inflight)
        self.assertEqual(len(self.server.calls), 1)

    def test_a_reask_whose_task_archive_fails_is_settled_after_a_restart(self):
        reask, _ = self._reask_answered_with_its_task_archive_failing()
        restarted = self.restart()
        self.drain(restarted)
        self.assertFalse((self.tasks / f'{reask}.txt').exists(), 'the re-ask was never retried')
        self.assertNotIn(reask, self.durable_inflight())
        self.assertEqual(len(self.server.calls), 1)

    def test_an_alias_is_never_settled_by_its_primarys_delivery(self):
        gw._save_inflight({TID})
        self.publish()
        inflight = self.drain()                          # the primary is delivered
        alias = 'task-alias1'
        aliases = gw._load_dedup_aliases() or {}
        aliases[alias] = TID
        gw._save_dedup_aliases(aliases)
        (self.tasks / f'{alias}.txt').write_text(
            f'id: {alias}\nsource: ag2space\nchannel_id: {ROOM}\nuser_id: owner\n'
            'access_tier: owner\ntask: Still waiting for its answer\n')
        inflight.add(alias)
        for restarted in (False, True):
            if restarted:
                inflight = self.restart() | {alias}
            self.drain(inflight)
            self.assertTrue((self.tasks / f'{alias}.txt').exists(), "an alias retired on its primary's record")
            self.assertIn(alias, inflight)

    def test_the_next_pass_keeps_the_replacement_unsent_for_a_person(self):
        self.publish()
        self.server.during = self.replace_atomically
        inflight = self.drain()
        self.server.during = None
        self.assertEqual(self.live().read_text(), NEWER)
        self.drain(inflight)
        self.assertEqual(self.sent_bodies(), [SENT])
        self.assertNotIn(NEWER, self.archived(), 'a reply never sent was archived as sent')
        self.assertEqual(self.kept(), [NEWER])

    # ── failure and platform edges ───────────────────────────────────────────

    def test_an_archive_that_cannot_move_leaves_the_result_and_never_resends(self):
        self.publish()
        (self.results / 'archive').write_text('not a directory')
        inflight = self.drain()
        self.drain(inflight)
        self.assertEqual(self.sent_bodies(), [SENT], 'a delivered result was sent twice')
        self.assertTrue(self.live().exists(), 'a result that could not be archived was dropped')
        self.assertEqual(self.live().read_text(), SENT)
        self.assertIn(TID, inflight)
        self.assertEqual(len([ln for ln in self.lines if 'could not be archived' in ln]), 1)

    def test_a_body_the_owner_kept_aside_keeps_its_task_looked_for(self):
        self.publish()

        kept = disposal.Retired(disposal.Retirement.FAILED, None, 'a newer reply was kept in undelivered/')
        with patch.object(disposal, 'retire_generation', return_value=kept):
            inflight = self.drain()
        self.assertIn(TID, inflight)
        self.assertTrue((self.tasks / f'{TID}.txt').exists())

    def test_an_archive_placed_with_an_unlock_warning_is_still_placed(self):
        self.publish()
        real = disposal.unlock_fd

        def unlock(fd):
            real(fd)
            raise OSError(5, 'EIO')
        with patch.object(disposal, 'unlock_fd', unlock):
            inflight = self.drain()
        self.assertEqual(self.archived(), [SENT])
        self.assertNotIn(TID, inflight, 'a placed archive kept the id looked for')
        self.assertTrue((self.tasks / 'archive' / f'{TID}.txt').exists())
        self.assertTrue(any('could not be released' in ln for ln in self.lines), self.lines)

    def test_a_result_already_gone_is_retired(self):
        self.publish()
        self.server.during = lambda: self.live().unlink()
        inflight = self.drain()
        self.assertNotIn(TID, inflight)
        self.assertTrue((self.tasks / 'archive' / f'{TID}.txt').exists())

    def test_without_a_no_replace_rename_the_sent_body_is_still_archived(self):
        self.publish()
        with self.without_a_no_replace_rename():
            inflight = self.drain()
        self.assertEqual(self.archived(), [SENT])
        self.assertNotIn(TID, inflight)

    def test_without_a_no_replace_rename_a_replacement_is_kept_not_archived(self):
        self.publish()
        self.server.during = self.replace_atomically
        with self.without_a_no_replace_rename():
            inflight = self.drain()
        self.assertNotIn(NEWER, self.archived(), 'a reply never sent was archived as sent')
        self.assertEqual(self.kept(), [NEWER], 'the newer reply has no surviving name')
        self.assertFalse(self.live().exists())
        self.assertIn(TID, inflight)
        self.assertFalse(any('stays live' in ln for ln in self.lines), 'a reply kept aside was called live')
        said = [ln for ln in self.lines if 'could not be archived' in ln]
        self.assertEqual(len(said), 1, self.lines)
        self.assertIn('undelivered/', said[0])

class TheOwnerRetiresOnlyTheGenerationRead(unittest.TestCase):
    """The lifecycle owner's own contract, below the bridge."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.results = Path(self.tmp.name) / 'results'
        self.results.mkdir()
        self.archive = self.results / 'archive'
        self.lines: list[str] = []

    def test_a_matching_generation_moves_under_the_first_free_name(self):
        rfile = self.results / f'{TID}.txt'
        rfile.write_text(SENT)
        (self.archive).mkdir()
        (self.archive / f'{TID}-1.txt').write_text('earlier evidence')
        _, gen = disposal.identity_of(rfile)
        done = disposal.retire_generation(self.results, rfile, gen, self.lines.append,
                                          self.archive, [f'{TID}-1.txt', f'{TID}-1.2.txt'])
        self.assertIs(done.outcome, disposal.Retirement.PLACED)
        self.assertTrue(done.retired)
        moved = done.path
        self.assertEqual(moved.name, f'{TID}-1.2.txt')
        self.assertEqual((self.archive / f'{TID}-1.txt').read_text(), 'earlier evidence')
        self.assertEqual(moved.read_text(), SENT)

    def test_another_generation_is_left_live(self):
        rfile = self.results / f'{TID}.txt'
        rfile.write_text(SENT)
        _, gen = disposal.identity_of(rfile)
        tmp = self.results / '.tmp'
        tmp.write_text(NEWER)
        os.replace(tmp, rfile)
        done = disposal.retire_generation(self.results, rfile, gen, self.lines.append,
                                          self.archive, [f'{TID}-1.txt'])
        self.assertEqual(done, disposal.Retired(disposal.Retirement.REPLACEMENT_LIVE))
        self.assertFalse(done.retired)
        self.assertEqual(rfile.read_text(), NEWER)
        self.assertFalse(any(self.archive.glob('*.txt')) if self.archive.is_dir() else False)


class BridgeImportIsHermetic(unittest.TestCase):
    def test_the_bridge_import_reads_no_host_config_token_or_vault(self):
        assert_hermetic(self, _IMPORT_READS)

    def test_the_checker_flags_a_host_read_and_a_vault_lookup(self):
        from _helpers.hermetic_gateway import host_reads
        flagged = host_reads([('file', str(Path.home() / '.claude' / 'channels' / 'x.env')),
                              ('proc', 'security find-generic-password -s REMOTE_TASK_TOKEN -w')])
        self.assertEqual(len(flagged), 2, flagged)


if __name__ == '__main__':
    unittest.main(verbosity=2)
