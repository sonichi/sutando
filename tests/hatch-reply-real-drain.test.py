#!/usr/bin/env python3
"""`ag2-sparrow-hatch reply` reports posted only when Sparrow's real drain sent it.

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

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / 'packages' / 'ag2-sparrow'
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402
_GW, _IMPORT_READS = isolate_then_import()
from ag2_sparrow import gateway_instance, outbox, remote_gateway_bridge as gw
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
    def start(self, refuse=False, instance=''):
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
            'access_tier: owner\ntask: what is 2+2?\n')
        gw._record_task_room(self.tid, '!r:ag2.space')

    def reply(self, body):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('REMOTE_', 'AG2_', 'AGENT_CONNECT_', 'SPARROW_'))
               and k != 'CLAUDE_CONFIG_DIR'}
        env.update(HOME=str(self.home), PYTHONPATH=str(PKG),
                   AGENT_CONNECT_RESULT_DIR=str(self.results),
                   AGENT_CONNECT_TASK_DIR=str(self.tasks),
                   AGENT_CONNECT_STATE_DIR=str(self.home / 'state'),
                   GATEWAY_INSTANCE=self.instance)
        proc = subprocess.Popen(
            [sys.executable, '-m', 'ag2_sparrow.adapters.hatch.cli', 'reply',
             str(self.results / f'{self.tid}.txt'), '--wait', '20'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, cwd=str(self.home))
        done = {}
        waiter = threading.Thread(target=lambda: done.update(
            out=proc.communicate(input=body, timeout=30)))
        waiter.start()
        while waiter.is_alive():
            if (self.results / f'{self.tid}.txt').exists():
                gw._post_ready_results({self.tid})
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
