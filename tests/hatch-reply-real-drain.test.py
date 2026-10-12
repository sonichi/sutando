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
from ag2_sparrow import outbox, remote_gateway_bridge as gw
from ag2_sparrow.delivery_core import DeliveryCore, DesignAClaimBackend, RetryPolicy
from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceResultProvider

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
    def start(self, refuse=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.home, self.results, tasks = root / 'home', root / 'results', root / 'tasks'
        for d in (self.home, self.results, tasks):
            d.mkdir()
        self.server = Gateway(refuse)
        core = DeliveryCore(
            DesignAClaimBackend(self.results / '.outbox', retry_schedule=outbox.RetrySchedule(),
                                republish_delivered=False),
            AG2SpaceResultProvider(self.server.request),
            RetryPolicy(max_attempts=5, defer_idempotent_resend=True))
        values = dict(RESULTS_DIR=self.results, ARCHIVE_RESULTS_DIR=self.results / 'archive',
                      UNDELIVERABLE_RESULTS_DIR=self.results / 'undelivered', TASKS_DIR=tasks,
                      _STATE=root / 'state', DEDUP_ALIAS_FILE=root / 'state' / 'aliases.json',
                      TASK_ROOMS_FILE=root / 'state' / 'rooms.json',
                      TASK_MEDIA_FILE=root / 'state' / 'media.json',
                      INFLIGHT_FILE=root / 'state' / 'inflight.json',
                      GATEWAY_INSTANCE='', _INST_SUFFIX='', _DELIVERY_CORE=core,
                      _req=self.server.request, _log=lambda *_: None,
                      _last_orphan_sweep=0.0, _orphan_quarantine_logged=set(),
                      _WITHHELD_TASK_OUTPUT={}, _SETTLE_PENDING=set(), _SETTLE_SCANNED=set(),
                      _SETTLE_LOADED=False)
        stack = contextlib.ExitStack()
        for name, value in values.items():
            stack.enter_context(patch.object(gw, name, value, create=True))
        stack.enter_context(patch.object(outbox, '_activity_completed', return_value=None))
        self.addCleanup(stack.close)
        (tasks / f'{TID}.txt').write_text(
            f'id: {TID}\nsource: ag2space\nchannel_id: !r:ag2.space\nuser_id: @bob:ag2.space\n'
            'access_tier: owner\ntask: what is 2+2?\n')
        gw._record_task_room(TID, '!r:ag2.space')

    def reply(self, body):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('REMOTE_', 'AG2_', 'AGENT_CONNECT_', 'SPARROW_'))
               and k != 'CLAUDE_CONFIG_DIR'}
        env.update(HOME=str(self.home), PYTHONPATH=str(PKG),
                   AGENT_CONNECT_RESULT_DIR=str(self.results),
                   AGENT_CONNECT_STATE_DIR=str(self.home / 'state'))
        proc = subprocess.Popen(
            [sys.executable, '-m', 'ag2_sparrow.adapters.hatch.cli', 'reply',
             str(self.results / f'{TID}.txt'), '--wait', '20'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, cwd=str(self.home))
        proc.stdin.write(body)
        proc.stdin.close()
        deadline = time.monotonic() + 20
        while proc.poll() is None and time.monotonic() < deadline:
            if (self.results / f'{TID}.txt').exists():
                gw._post_ready_results({TID})
            time.sleep(0.05)
        out, err = proc.communicate(timeout=30)
        return proc.returncode, out, err

    def test_posted_after_the_gateway_accepted_it(self):
        self.start()
        rc, out, err = self.reply('4\n')
        self.assertEqual(rc, 0, err)
        self.assertIn(f'posted: {TID}.txt', out)
        self.assertEqual([c['body'] for c in self.server.calls], ['4'])

    def test_not_posted_when_the_gateway_refused_it(self):
        self.start(refuse=True)
        rc, out, err = self.reply('4\n')
        self.assertEqual(rc, 1, out + err)
        self.assertIn('NOT posted', err)
        self.assertNotIn('posted:', out)
        self.assertEqual(len(self.server.calls), 1)


if __name__ == '__main__':
    unittest.main()
