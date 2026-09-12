#!/usr/bin/env python3
"""Recovery identity persistence, concurrency, recurrence, and failure contracts."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import recovery_issues as ri


class Issues(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'issues.json'
        self.events = []

    def emit(self, event, **props):
        self.events.append((event, props))

    def health(self, status, start=False):
        ri.track_health_issues(self.path, [{'name': 'private-check', 'status': status}],
                               start=start, emit=self.emit)

    def test_retries_survive_module_reload_and_recurrence_gets_new_id(self):
        self.health('down', True)
        first = self.events[0][1]['issue_id']
        importlib.reload(ri)
        self.health('down', True)
        self.health('ok')
        self.health('ok')
        self.assertEqual(len({p['issue_id'] for _, p in self.events}), 1)
        self.assertEqual(sum(e.endswith('_recovered') for e, _ in self.events), 1)
        self.health('down', True)
        self.assertNotEqual(self.events[-1][1]['issue_id'], first)

    def test_missing_check_and_warning_leave_issue_open(self):
        self.health('down', True)
        ri.track_health_issues(self.path, [], start=False, emit=self.emit)
        self.health('warn')
        self.assertEqual(len(self.events), 2)
        self.assertTrue(json.loads(self.path.read_text()))

    def test_concurrent_writers_only_detect_and_recover_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.health('down', True), range(20)))
            list(pool.map(lambda _: self.health('ok'), range(20)))
        self.assertEqual(sum(e.endswith('_detected') for e, _ in self.events), 1)
        self.assertEqual(sum(e.endswith('_recovered') for e, _ in self.events), 1)
        self.assertEqual(len({p['issue_id'] for _, p in self.events}), 1)

    def test_atomic_failure_preserves_identity_and_emits_nothing(self):
        self.health('down', True)
        before = self.path.read_text()
        self.events.clear()
        with patch.object(ri.os, 'replace', side_effect=OSError('disk full')):
            self.health('ok')
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(self.events, [])
        self.assertEqual(list(self.path.parent.glob('.recovery-issues-*')), [])
        self.health('ok')
        self.assertEqual(self.events[0][1]['issue_id'], json.loads(before)['private-check']['issue_id'])

    def test_corrupt_state_does_not_invent_new_identity(self):
        self.path.write_text('{broken')
        self.health('down', True)
        self.assertEqual(self.events, [])
        self.assertEqual(self.path.read_text(), '{broken')

    def test_sender_failure_and_unsupported_lock_are_nonfatal(self):
        with patch.object(self, 'emit', side_effect=RuntimeError('offline')):
            self.health('down', True)
        self.assertTrue(self.path.exists())
        with patch.object(ri, 'fcntl', None):
            self.health('ok')
        self.assertTrue(json.loads(self.path.read_text()))

    def test_core_unknown_dead_and_retry_keep_identity_until_progress(self):
        def core(alive, status=10, start=False):
            ri.track_core_issue(self.path, alive=alive, task='private-task',
                                status_ts=status, start=start, emit=self.emit)
        core(False, start=True)
        core(None, 11)
        core(True)
        core(False, start=True)
        self.assertFalse(any(e.endswith('_recovered') for e, _ in self.events))
        core(True, 11)
        core(True, 12)
        self.assertEqual(sum(e.endswith('_recovered') for e, _ in self.events), 1)
        self.assertEqual(len({p['issue_id'] for _, p in self.events}), 1)
        self.assertNotIn('private-task', json.dumps(self.events))


if __name__ == '__main__':
    unittest.main()
