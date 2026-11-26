#!/usr/bin/env python3
"""Per-lane archive health and a retiring worker's card withdrawal, on the core's `src/hitl`.

- A lane's report counts toward archive health while it names failures; a stale lane report that
  names failures marks health stale; one with none is ignored. A malformed scope is refused.
- Withdrawing a worker's cards skips a card answered meanwhile, and leaves other workers' cards alone.
"""
# ruff: noqa: E402
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hitl import cards as hc, manager as hm, schema as hs


class ArchiveLanes(unittest.TestCase):
    def setUp(self):
        self.root = hm.default_store(Path(tempfile.mkdtemp(prefix="hitl-lanes-")))
        self.root.mkdir(parents=True)

    def write(self, scope, **status):
        hm.archive_status_path(self.root, scope).write_text(json.dumps(status))

    def test_a_malformed_scope_is_refused(self):
        for bad in ("../x", "Upper", "a b", "-lead", "x" * 65):
            with self.subTest(scope=bad), self.assertRaises(ValueError):
                hm.archive_status_path(self.root, bad)
        self.assertTrue(hm.archive_status_path(self.root, "lane-1").name.endswith("-archive-status.lane-1.json"))

    def test_lane_reports_with_failures_add_up_and_a_stale_one_marks_health_stale(self):
        now = time.time()
        self.write("", failures=0, error="", updated_at=now)
        self.write("lane-a", failures=2, error="OSError: disk full", updated_at=now)
        self.write("lane-b", failures=1, error="OSError: other", updated_at=now - hm.ARCHIVE_STATUS_STALE_S - 5)
        self.write("lane-c", failures=0, error="", updated_at=1)
        health = hm.archive_health(self.root, now)
        self.assertEqual(health["failures"], 3)
        self.assertEqual(health["error"], "OSError: disk full")
        self.assertTrue(health["stale"])

    def test_a_fresh_lane_failure_alone_is_reported_and_not_stale(self):
        now = time.time()
        self.write("lane-a", failures=1, error="OSError: x", updated_at=now)
        health = hm.archive_health(self.root, now)
        self.assertEqual((health["failures"], health["stale"], health["reported"]), (1, False, False))


class WithdrawWorkerCards(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="hitl-withdraw-"))
        self.manager = hc.manager_for(self.ws)

    def card(self, guard, worker):
        return self.manager.create(hc.choice_request(
            f"Q {guard}?", [{"id": "ok", "label": "OK"}], guard, {"by_worker": worker}, runtime="pending-questions"))

    def test_a_card_answered_mid_withdrawal_is_not_withdrawn(self):
        answered, open_ = self.card("g1", "w1"), self.card("g2", "w1")
        other = self.card("g3", "w2")
        real_cancel = self.manager.cancel

        def cancel(req_id):
            if req_id == answered.id:  # the owner's click lands first
                self.manager.resolve(req_id)
                return []
            return real_cancel(req_id)
        with mock.patch.object(self.manager, "cancel", side_effect=cancel):
            withdrawn = hc.withdraw_worker_cards("w1", self.ws, manager=self.manager)
        self.assertEqual(withdrawn, ["Q g2?"])
        self.assertEqual(self.manager.store.load(answered.id).status, hs.STATUS_RESOLVED)
        self.assertEqual(self.manager.store.load(open_.id).status, hs.STATUS_CANCELLED)
        self.assertEqual(self.manager.store.load(other.id).status, hs.STATUS_PENDING)


if __name__ == "__main__":
    unittest.main(verbosity=2)
