#!/usr/bin/env python3
"""`ag2-sparrow-hatch reply` reports posted, and `no-reply` settled, only when Sparrow's real drain
sent it.

The worker's command writes the result file and waits; the bridge's own
`_post_ready_results` drains it through the delivery core to a stand-in gateway.
Run: python3 tests/hatch-reply-real-drain.test.py
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from _helpers.hermetic_env import hermetic_env  # noqa: E402

from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402
_GW, _IMPORT_READS = isolate_then_import()

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / 'packages' / 'ag2-sparrow'
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ag2_sparrow import agent_identity, delivery_destination, gateway_instance, outbox, remote_gateway_bridge as gw
from ag2_sparrow.adapters.hatch import task_fields

TID = 'task-1784700000001'


class Gateway:
    def __init__(self, refuse=False):
        self.calls, self.refuse = [], refuse

    def request(self, method, path, payload):
        self.calls.append(dict(payload))
        if self.refuse:
            raise urllib.error.HTTPError(path, 400, 'bad request', {}, None)
        return {'ok': True}


class ReplyThroughTheRealDrain(unittest.TestCase):
    def start(self, refuse=False, instance='', tier='owner'):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.home, self.results = root / 'home', root / 'results'
        self.tasks = tasks = root / 'tasks'
        for d in (self.home, self.results, tasks):
            d.mkdir()
        self.server = Gateway(refuse)
        self.instance = instance
        values = dict(RESULTS_DIR=self.results, ARCHIVE_RESULTS_DIR=self.results / 'archive',
                      UNDELIVERABLE_RESULTS_DIR=self.results / 'undelivered', TASKS_DIR=tasks,
                      _STATE=root / 'state', DEDUP_ALIAS_FILE=root / 'state' / 'aliases.json',
                      TASK_ROOMS_FILE=root / 'state' / 'rooms.json',
                      TASK_MEDIA_FILE=root / 'state' / 'media.json',
                      INFLIGHT_FILE=root / 'state' / 'inflight.json',
                      # The bridge builds its own outbox, at the root it derives itself.
                      GATEWAY_INSTANCE=instance, _INST_SUFFIX=f'.{instance}' if instance else '',
                      _DELIVERY_CORE=None,
                      _req=self.server.request, _log=lambda *_: None,
                      _last_orphan_sweep=0.0, _orphan_quarantine_logged=set(),
                      _WITHHELD_TASK_OUTPUT={}, _SETTLE_PENDING=set(), _SETTLE_SCANNED=set(),
                      _SETTLE_LOADED=False)
        stack = contextlib.ExitStack()
        for name, value in values.items():
            stack.enter_context(patch.object(gw, name, value, create=True))
        stack.enter_context(patch.object(outbox, '_activity_completed', return_value=None))
        self.addCleanup(stack.close)
        self.tid = gw._local_tid(TID)
        (tasks / f'{self.tid}.txt').write_text(
            f'id: {self.tid}\nsource: ag2space\nchannel_id: !r:ag2.space\nuser_id: @bob:ag2.space\n'
            f'access_tier: {tier}\ntask: what is 2+2?\n')
        gw._record_task_room(self.tid, '!r:ag2.space')

    def reply(self, body, *cmd):
        env = hermetic_env()
        env.update(HOME=str(self.home), PYTHONPATH=str(PKG),
                   AGENT_CONNECT_RESULT_DIR=str(self.results),
                   AGENT_CONNECT_TASK_DIR=str(self.tasks),
                   AGENT_CONNECT_STATE_DIR=str(self.home / 'state'),
                   GATEWAY_INSTANCE=self.instance)
        proc = subprocess.Popen(
            [sys.executable, '-m', 'ag2_sparrow.adapters.hatch.cli', *(cmd or ('reply',)),
             str(self.results / f'{self.tid}.txt'), '--wait', '20'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, cwd=str(self.home))
        done = {}
        self.inflight = {self.tid}
        waiter = threading.Thread(target=lambda: done.update(
            out=proc.communicate(input=body, timeout=30)))
        waiter.start()
        while waiter.is_alive():
            if (self.results / f'{self.tid}.txt').exists():
                gw._post_ready_results(self.inflight)
            time.sleep(0.05)
        waiter.join()
        out, err = done['out']
        return proc.returncode, out, err

    def test_posted_after_the_gateway_accepted_it(self):
        self.start()
        rc, out, err = self.reply('4\n')
        self.assertEqual(rc, 0, err)
        self.assertIn(f'posted: {self.tid}.txt', out)
        self.assertEqual([c['body'] for c in self.server.calls], ['4'])

    def test_posted_through_a_named_gateway_instance(self):
        self.start(instance='blue')
        self.assertEqual(self.tid, f'task-blue~{TID}')
        rc, out, err = self.reply('4\n')
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f'posted: {self.tid}.txt', out)
        self.assertEqual([(c['id'], c['body']) for c in self.server.calls], [(TID, '4')])
        self.assertEqual(outbox.item_status(self.results / '.outbox.blue', TID), 'DELIVERED')

    def test_not_posted_when_the_gateway_refused_it(self):
        self.start(refuse=True)
        rc, out, err = self.reply('4\n')
        self.assertEqual(rc, 1, out + err)
        self.assertIn('NOT posted', err)
        self.assertNotIn('posted:', out)
        self.assertEqual(len(self.server.calls), 1)

    def test_no_reply_posts_nothing_and_settles_the_task(self):
        self.start()
        rc, out, err = self.reply('', 'no-reply', '--reason', 'an acknowledgement')
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f'settled: {self.tid}.txt, nothing posted', out)
        self.assertEqual([(c['body'], c.get('no_send')) for c in self.server.calls],
                         [('[no-send]', True)])
        self.assertEqual(self.inflight, set())
        self.assertFalse((self.results / f'{self.tid}.txt').exists())
        self.assertEqual(outbox.item_status(self.results / '.outbox', TID), 'DELIVERED')

    def side(self):
        return self.results / f'{self.tid}.meta.json'

    def archived_sides(self):
        return sorted((self.results / 'archive').glob(f'{self.tid}-*.meta.json'))

    def test_reply_carries_its_title_and_outcome_and_archives_them(self):
        self.start()
        rc, out, err = self.reply('4\n', 'reply', '--title', 'Add two and two',
                                  '--outcome', 'Answered 4.')
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([(c['body'], c.get('title'), c.get('outcome')) for c in self.server.calls],
                         [('4', 'Add two and two', 'Answered 4.')])
        self.assertFalse(self.side().exists())
        self.assertEqual(len(self.archived_sides()), 1)

    def test_without_labels_the_result_is_posted_as_before(self):
        self.start()
        rc, out, err = self.reply('4\n')
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn('title', self.server.calls[0])
        self.assertNotIn('outcome', self.server.calls[0])
        self.assertFalse(self.side().exists())

    def test_a_reply_without_labels_removes_a_side_file_left_by_an_earlier_attempt(self):
        self.start()
        self.side().write_text('{"title": "Stale", "outcome": "From a crashed attempt."}')
        rc, out, err = self.reply('4\n')
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([(c.get('title'), c.get('outcome')) for c in self.server.calls], [(None, None)])

    def test_no_reply_outcome_defaults_to_its_reason(self):
        self.start()
        rc, out, err = self.reply('', 'no-reply', '--reason', 'an acknowledgement')
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([(c.get('no_send'), c.get('title'), c.get('outcome'))
                          for c in self.server.calls], [(True, None, 'an acknowledgement')])

    def test_no_reply_takes_an_explicit_title_and_outcome(self):
        self.start()
        rc, out, err = self.reply('', 'no-reply', '--reason', 'ack', '--title', 'Acknowledge',
                                  '--outcome', 'Acknowledged the thanks.')
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([(c.get('title'), c.get('outcome')) for c in self.server.calls],
                         [('Acknowledge', 'Acknowledged the thanks.')])

    def test_labels_are_one_line_without_control_characters(self):
        self.start()
        rc, out, err = self.reply('4\n', 'reply', '--title', 'Add\x07 two\n\tand two\u2028now',
                                  '--outcome', '  Answered\x1b[31m 4. ')
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([(c.get('title'), c.get('outcome')) for c in self.server.calls],
                         [('Add two and two now', 'Answered [31m 4.')])

    def test_an_over_long_label_is_refused_before_anything_is_written(self):
        for flag, n in (('--title', 61), ('--outcome', 151)):
            with self.subTest(flag=flag):
                self.start()
                rc, out, err = self.reply('4\n', 'reply', flag, 'x' * n)
                self.assertEqual(rc, 2, out + err)
                self.assertIn(f'{flag} over {n - 1} characters', err)
                self.assertFalse((self.results / f'{self.tid}.txt').exists())
                self.assertFalse(self.side().exists())
                self.assertEqual(self.server.calls, [])

    def test_a_long_reason_is_cut_to_the_outcome_limit(self):
        self.start()
        rc, out, err = self.reply('', 'no-reply', '--reason', 'r' * 180)
        self.assertEqual(rc, 0, out + err)
        outcome = self.server.calls[0]['outcome']
        self.assertEqual((len(outcome), outcome[-1]), (150, '…'))

    def test_labels_ride_a_dm_only_result(self):
        # A core writes a marked result itself; the hatch reply refuses markers.
        self.start()
        self.side().write_text('{"title": "Add two and two"}')
        (self.results / f'{self.tid}.txt').write_text('[dm-only]\n4\n')
        gw._post_ready_results({self.tid})
        self.assertEqual([(c['dm_only'], c.get('title')) for c in self.server.calls],
                         [(True, 'Add two and two')])

    def test_a_non_owner_label_passes_the_result_guard_or_is_left_out(self):
        self.start(tier='team')
        rc, out, err = self.reply('4\n', 'reply', '--title', 'Add two and two',
                                  '--outcome', 'Sent ghp_' + 'a' * 36)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([(c['body'], c.get('title'), c.get('outcome')) for c in self.server.calls],
                         [('4', 'Add two and two', None)])

    def test_an_answered_item_keeps_the_side_file_it_was_answered_with(self):
        self.start()
        (self.results / f'{self.tid}.txt').write_text('first\n')
        self.side().write_text('{"title": "First"}')
        env = hermetic_env()
        env.update(HOME=str(self.home), PYTHONPATH=str(PKG),
                   AGENT_CONNECT_RESULT_DIR=str(self.results), AGENT_CONNECT_TASK_DIR=str(self.tasks),
                   AGENT_CONNECT_STATE_DIR=str(self.home / 'state'))
        proc = subprocess.run(
            [sys.executable, '-m', 'ag2_sparrow.adapters.hatch.cli', 'reply',
             str(self.results / f'{self.tid}.txt'), '--title', 'Second', '--wait', '0'],
            input='second\n', capture_output=True, text=True, env=env, cwd=str(self.home))
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.side().read_text(), '{"title": "First"}')

    def test_an_orphaned_side_file_is_archived_once_it_is_old(self):
        self.start()
        young, old = self.results / 'task-young.meta.json', self.results / 'task-old.meta.json'
        for f in (young, old):
            f.write_text('{"title": "Orphan"}')
        stale = time.time() - gw.RESULT_LABELS_ORPHAN_S - 1
        os.utime(old, (stale, stale))
        live = self.results / 'task-live.meta.json'
        live.write_text('{"title": "Live"}')
        os.utime(live, (stale, stale))
        (self.results / 'task-live.txt').write_text('pending\n')
        gw._sweep_orphan_result_labels(time.time())
        self.assertTrue(young.exists())
        self.assertTrue(live.exists())
        self.assertFalse(old.exists())
        self.assertEqual(len(list((self.results / 'archive').glob('task-old-*.meta.json'))), 1)

    def test_no_reply_is_not_settled_when_the_gateway_refused_it(self):
        self.start(refuse=True)
        rc, out, err = self.reply('', 'no-reply')
        self.assertEqual(rc, 1, out + err)
        self.assertIn('NOT settled', err)
        self.assertEqual(self.inflight, {self.tid})

    def lane(self, lifecycle=None):
        """The running drain's lane record, as the bridge publishes it, naming its lifecycle."""
        agent_identity.publish_lane(self.home / 'state', self.instance, '@agent:ag2.space',
                                    send_lock=delivery_destination.SEND_LOCK_PROTOCOL, lifecycle=lifecycle)

    def wake_run(self, base_delivered=False, bridge_keeps_ledger=True):
        """A wake opened in a lifecycle-2 ledger: its run's own task file, and the episode's item."""
        from ag2_sparrow import task_attempts
        att = task_attempts.TaskAttempts(self.home / 'state', log=lambda *_: None)  # where the CLI reads it
        att.take_ownership()
        if bridge_keeps_ledger:
            stack = contextlib.ExitStack()
            stack.enter_context(patch.object(gw, '_ATTEMPTS', att))
            self.addCleanup(stack.close)
        if base_delivered:
            outbox.record_delivered(self.results / '.outbox', TID)
        with att.transition(TID) as t:
            episode, run = t.open_wake({'blocker_episode_id': 'e1'}, self.tid,
                                       used=lambda local: (self.tasks / f'{local}.txt').exists())
        (self.tasks / f'{run}.txt').write_text((self.tasks / f'{self.tid}.txt').read_text())
        self.tid, item = run, att.run_item(run)
        self.assertEqual(item, f'{TID}.wake-{episode}')
        return item

    def test_no_reply_settles_a_wake_run_under_its_own_item(self):
        for base_delivered in (False, True):
            with self.subTest(base_delivered=base_delivered):
                self.start()
                self.lane(lifecycle=2)
                item = self.wake_run(base_delivered)
                rc, out, err = self.reply('', 'no-reply')
                self.assertEqual(rc, 0, out + err)
                self.assertIn(f'settled: {self.tid}.txt, nothing posted', out)
                self.assertEqual(outbox.item_status(self.results / '.outbox', item), 'DELIVERED')
                self.assertEqual([(c['body'], c.get('no_send')) for c in self.server.calls], [('[no-send]', True)])
                self.assertEqual(self.inflight, set())

    def test_with_lifecycle_2_off_a_ledger_left_behind_is_never_read(self):
        for ledger, base_delivered in (('valid', False), ('damaged', False), ('valid', True)):
            with self.subTest(ledger=ledger, base_delivered=base_delivered):
                self.start()
                self.lane()                                   # the running drain keeps no ledger
                self.wake_run(base_delivered, bridge_keeps_ledger=False)
                if ledger == 'damaged':
                    (self.home / 'state' / 'relay-task-ledger.json').write_text('{not json')
                rc, out, err = self.reply('', 'no-reply')
                self.assertEqual(rc, 0, out + err)
                self.assertIn(f'settled: {self.tid}.txt, nothing posted', out)
                # Under the task's own item; one already delivered closes on its own record.
                item = f'{TID}.lease-close' if base_delivered else TID
                self.assertEqual(outbox.item_status(self.results / '.outbox', item), 'DELIVERED')
                self.assertEqual([(c['body'], c.get('no_send')) for c in self.server.calls], [('[no-send]', True)])
                self.assertEqual(self.inflight, set())

    def test_with_no_running_drain_a_run_the_ledger_names_is_not_guessed(self):
        self.start()
        self.wake_run()
        rc, out, err = self.reply('', 'no-reply')
        self.assertEqual(rc, 2, out + err)
        self.assertIn('Sparrow is not running', err)
        self.assertFalse((self.results / f'{self.tid}.txt').exists(), 'nothing written')
        self.assertEqual(self.server.calls, [])

    def test_a_skip_behind_a_redirect_is_refused_before_anything_is_sent(self):
        self.start()
        rc, out, err = self.reply('[channel: !r:ag2.space]\n[REPLIED]\n')
        self.assertEqual(rc, 2, out + err)
        self.assertNotIn('posted:', out)
        self.assertFalse((self.results / f'{self.tid}.txt').exists())
        self.assertEqual(self.server.calls, [])

    def test_a_redirect_to_another_room_is_refused(self):
        self.start()
        rc, out, err = self.reply('[channel: !other:ag2.space]\nhello\n')
        self.assertEqual(rc, 2, out + err)
        self.assertEqual(self.server.calls, [])


class SharedInstanceMapping(unittest.TestCase):
    """The adapter reads ids and the outbox root through the helper the bridge uses."""

    def test_item_ids_and_outbox_root_follow_the_bridge(self):
        with tempfile.TemporaryDirectory() as d:
            for inst in ('', 'blue'):
                with patch.object(gw, 'GATEWAY_INSTANCE', inst), \
                        patch.object(gw, '_DELIVERY_CORE', None), \
                        patch.object(gw, 'RESULTS_DIR', Path(d)):
                    local = gw._local_tid(TID)
                    self.assertEqual(task_fields.item_id(local, inst), gw._broker_tid(local))
                    self.assertEqual(gw._delivery_core().backend.root,
                                     gateway_instance.outbox_root(Path(d), inst))
        self.assertIsNone(task_fields.item_id('task-blue~task-1', ''))
        self.assertIsNone(task_fields.item_id(TID, 'blue'))
        self.assertIsNone(task_fields.item_id('task-blue~../x', 'blue'))


if __name__ == '__main__':
    unittest.main()
