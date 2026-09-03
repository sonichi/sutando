#!/usr/bin/env python3
"""PoolNotifier (L5): handoff notices fire only on a real handler change,
stall notices fire once past the threshold and never for a done task,
and the ledger stays bounded to live claims.

Run: python3 tests/pool-notify.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from pool_notify import PoolNotifier, read_routing  # noqa: E402

HEADER = "id: {stem}\nsource: {source}\nchannel_id: {channel}\ntask: hi\n"


class NotifyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.state = root / "state"
        self.tasks.mkdir()
        self.state.mkdir()
        self.sent = []
        self.clock = [1_000.0]
        self.n = PoolNotifier(
            self.tasks, self.state,
            send_fn=lambda s, c, m: self.sent.append((s, c, m)) or True,
            now_fn=lambda: self.clock[0], stall_after_s=600)

    def tearDown(self):
        self.tmp.cleanup()

    def _assigned(self, stem, inst, source="discord", channel="C1"):
        p = self.tasks / f"{stem}.assigned-{inst}.txt"
        p.write_text(HEADER.format(stem=stem, source=source, channel=channel))
        return p

    def _claimed(self, stem, inst, source="discord", channel="C1"):
        p = self.tasks / f"{stem}.claimed-{inst}.txt"
        p.write_text(HEADER.format(stem=stem, source=source, channel=channel))
        return p


class HandoffTests(NotifyBase):
    def test_first_assignment_is_silent_and_records_handler(self):
        self._assigned("task-1", "worker-2")
        self.assertFalse(self.n.on_assigned("task-1.txt", "worker-2"))
        self.assertEqual(self.sent, [])

    def test_handler_change_notifies_the_channel(self):
        self._assigned("task-1", "worker-2")
        self.n.on_assigned("task-1.txt", "worker-2")
        self._assigned("task-2", "worker-1")
        self.assertTrue(self.n.on_assigned("task-2.txt", "worker-1"))
        (source, channel, msg), = self.sent
        self.assertEqual((source, channel), ("discord", "C1"))
        self.assertIn("worker-1", msg)
        self.assertIn("worker-2", msg)
        self.assertNotIn("[", msg)  # ag2space team_result_guard withholds brackets

    def test_same_handler_stays_silent(self):
        self._assigned("task-1", "worker-2")
        self.n.on_assigned("task-1.txt", "worker-2")
        self._assigned("task-2", "worker-2")
        self.assertFalse(self.n.on_assigned("task-2.txt", "worker-2"))
        self.assertEqual(self.sent, [])

    def test_unroutable_task_is_silent(self):
        p = self.tasks / "task-9.assigned-worker-1.txt"
        p.write_text("id: task-9\ntask: voice thing, no channel\n")
        self.assertFalse(self.n.on_assigned("task-9.txt", "worker-1"))
        self.assertEqual(self.sent, [])


class StallTests(NotifyBase):
    def test_stall_fires_once_after_threshold(self):
        self._claimed("task-1", "worker-2")
        self.assertEqual(self.n.check_stalls(), [])  # first sight: records t0
        self.clock[0] += 601
        self.assertEqual(self.n.check_stalls(), ["task-1"])
        (source, channel, msg), = self.sent
        self.assertEqual((source, channel), ("discord", "C1"))
        self.assertIn("worker-2", msg)
        self.assertIn("10", msg)
        self.assertEqual(self.n.check_stalls(), [])  # at most once

    def test_no_stall_before_threshold(self):
        self._claimed("task-1", "worker-2")
        self.n.check_stalls()
        self.clock[0] += 599
        self.assertEqual(self.n.check_stalls(), [])
        self.assertEqual(self.sent, [])

    def test_done_flag_suppresses_stall(self):
        self._claimed("task-1", "worker-2")
        self.n.check_stalls()
        done = self.state / "cores" / "worker-2" / "done"
        done.mkdir(parents=True)
        (done / "task-1.flag").touch()
        self.clock[0] += 601
        self.assertEqual(self.n.check_stalls(), [])
        self.assertEqual(self.sent, [])

    def test_slack_is_excluded_its_bridge_owns_the_notice(self):
        self._claimed("task-1", "worker-2", source="slack", channel="D1")
        self.n.check_stalls()
        self.clock[0] += 601
        self.assertEqual(self.n.check_stalls(), [])
        self.assertEqual(self.sent, [])

    def test_failed_send_retries_next_pass(self):
        calls = []

        def flaky(source, channel, msg):
            calls.append(1)
            return len(calls) > 1

        n = PoolNotifier(self.tasks, self.state, send_fn=flaky,
                         now_fn=lambda: self.clock[0], stall_after_s=600)
        self._claimed("task-1", "worker-2")
        n.check_stalls()
        self.clock[0] += 601
        self.assertEqual(n.check_stalls(), [])       # send failed → not marked
        self.assertEqual(n.check_stalls(), ["task-1"])  # retried, succeeded

    def test_repool_keeps_the_at_most_once_marker(self):
        """A stall-notified task that is repooled and re-claimed by another
        core must not notify again — the marker lives in a row that used to
        be pruned the moment the file stopped being `.claimed-`."""
        p = self._claimed("task-1", "core-A")
        self.n.check_stalls()
        self.clock[0] += 601
        self.assertEqual(self.n.check_stalls(), ["task-1"])
        p.rename(self.tasks / "task-1.txt")        # lead repools it
        self.n.check_stalls()
        self._claimed("task-1", "core-B")          # another core claims it
        for _ in range(3):                         # past the re-armed clock
            self.clock[0] += 601
            self.n.check_stalls()
        self.assertEqual(len(self.sent), 1, [m for _, _, m in self.sent])
        self.assertIn("core-A", self.sent[0][2])

    def test_repool_control_same_claimer_also_notifies_once(self):
        self._claimed("task-1", "core-A")
        self.n.check_stalls()
        for _ in range(4):
            self.clock[0] += 601
            self.n.check_stalls()
        self.assertEqual(len(self.sent), 1, [m for _, _, m in self.sent])

    def test_presence_prune_does_not_alias_a_longer_stem(self):
        """task-1 must not be kept alive by task-12 — the prefix-collision
        that the same-shaped archive glob shipped elsewhere."""
        p = self._claimed("task-1", "core-A")
        self._claimed("task-12", "core-B")
        self.n.check_stalls()
        p.unlink()                                  # only task-1 goes away
        self.n.check_stalls()
        import json
        ledger = json.loads(
            (self.state / "pool" / "notify-ledger.json").read_text())
        self.assertEqual(sorted(ledger["tasks"]), ["task-12"])

    def test_ledger_drops_archived_claims(self):
        p = self._claimed("task-1", "worker-2")
        self.n.check_stalls()
        p.unlink()  # follower archived it
        self.n.check_stalls()
        import json
        ledger = json.loads(
            (self.state / "pool" / "notify-ledger.json").read_text())
        self.assertEqual(ledger["tasks"], {})


class RoutingTests(NotifyBase):
    def test_chat_id_is_a_channel_too(self):
        p = self.tasks / "t.txt"
        p.write_text("id: t\nsource: telegram\nchat_id: 555\ntask: x\n")
        self.assertEqual(read_routing(p), ("telegram", "555"))

    def test_missing_file_is_none(self):
        self.assertIsNone(read_routing(self.tasks / "absent.txt"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
