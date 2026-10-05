"""Follow-up notices: a still-unresolved blocking requirement re-alerts as a NEW message
on a 30m / 2h / 6h backoff, announces recovery, and keeps its schedule across a restart."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hitl.manager import HitlManager, HitlStore  # noqa: E402
from hitl.projector import pending_ids, project  # noqa: E402

from hitl.schema import Action, HumanRequirement, WIRE_FIELD  # noqa: E402

ROOM = "!room:ag2.space"
T0 = 1_000_000.0
MIN = 60.0
HOUR = 60 * MIN


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Sender:
    def __init__(self):
        self.sent = []

    def __call__(self, payload):
        self.sent.append(payload)
        return {"ok": True, "event_id": f"$ev{len(self.sent)}"}


def blocking(kind="auth", guard="g1"):
    return HumanRequirement(
        kind=kind, runtime="claude", message="Claude Code needs to sign in again",
        guard=guard, device={"id": "core-1", "name": "core-1"},
        actions=[Action(id="reauth", kind="authenticate", label="Re-authenticate")],
        created_at=T0, updated_at=T0)


class RenotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mgr = HitlManager(HitlStore(self.root))
        self.send = Sender()
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def drive(self):
        before = len(self.send.sent)
        project(self.mgr, self.send, ROOM, clock=self.clock)
        return self.send.sent[before:]

    def test_still_blocked_after_30m_is_one_new_message(self):
        req = self.mgr.create(blocking())
        self.mgr.link_blocked_task(req.id, "task-1")
        self.mgr.link_blocked_task(req.id, "task-2")
        self.assertEqual([p["op"] for p in self.drive()], ["message"])
        self.clock.advance(31 * MIN)
        sent = self.drive()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["op"], "message")
        self.assertNotIn("event_id", sent[0])  # not an edit of the card
        self.assertNotIn("extra_content", sent[0])
        self.assertIn("Still blocked after 31m", sent[0]["body"])
        self.assertIn("2 tasks are waiting", sent[0]["body"])

    def test_a_little_later_sends_nothing(self):
        self.mgr.create(blocking())
        self.drive()
        self.clock.advance(31 * MIN)
        self.assertEqual(len(self.drive()), 1)
        self.clock.advance(10 * MIN)
        self.assertEqual(self.drive(), [])
        self.assertEqual(pending_ids(self.mgr, clock=self.clock), [])

    def test_backoff_30m_then_2h_then_every_6h(self):
        self.mgr.create(blocking())
        self.drive()
        times = []
        for _ in range(int(24 * HOUR // (5 * MIN))):
            self.clock.advance(5 * MIN)
            if self.drive():
                times.append(round((self.clock.t - T0) / MIN))
        # 30m, +2h, then +6h repeating, sampled on a 5-minute pulse.
        self.assertEqual(times, [30, 150, 510, 870, 1230])

    def test_inside_the_interval_sends_nothing(self):
        req = self.mgr.create(blocking(kind="core-blocked"))
        self.drive()
        self.clock.advance(29 * MIN)
        self.assertEqual(self.drive(), [])
        self.assertNotIn(req.id, pending_ids(self.mgr, clock=self.clock))

    def test_resolve_is_the_edit_plus_one_recovery_message(self):
        req = self.mgr.create(blocking())
        self.mgr.link_blocked_task(req.id, "task-1")
        self.mgr.link_blocked_task(req.id, "task-2")
        self.mgr.link_blocked_task(req.id, "task-3")
        self.drive()
        self.mgr.resolve(req.id)
        sent = self.drive()
        self.assertEqual([p["op"] for p in sent], ["edit", "message"])
        self.assertEqual(sent[0]["extra_content"][WIRE_FIELD]["status"], "resolved")
        self.assertIn("Reconnected — resuming 3 queued tasks", sent[1]["body"])
        self.clock.advance(7 * HOUR)
        self.assertEqual(self.drive(), [])  # once, and no reminder after resolution

    def test_schedule_survives_a_restart(self):
        self.mgr.create(blocking())
        self.drive()
        self.clock.advance(31 * MIN)
        self.assertEqual(len(self.drive()), 1)
        restarted = HitlManager(HitlStore(self.root))
        self.clock.advance(MIN)
        self.assertEqual(pending_ids(restarted, clock=self.clock), [])
        self.clock.advance(2 * HOUR)
        before = len(self.send.sent)
        project(restarted, self.send, ROOM, clock=self.clock)
        self.assertEqual(len(self.send.sent) - before, 1)

    def test_a_rejected_reminder_is_retried_and_not_counted(self):
        self.mgr.create(blocking())
        self.drive()
        self.clock.advance(31 * MIN)
        refused = []
        project(self.mgr, lambda p: refused.append(p) or {"ok": False}, ROOM, clock=self.clock)
        self.assertEqual(len(refused), 1)
        sent = self.drive()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["dedupe_key"], refused[0]["dedupe_key"])

    def test_a_decision_card_is_never_re_alerted(self):
        req = blocking(kind="choice")
        self.mgr.create(req)
        self.drive()
        self.clock.advance(7 * HOUR)
        self.assertEqual(self.drive(), [])
        self.mgr.resolve(req.id)
        self.assertEqual([p["op"] for p in self.drive()], ["edit"])

    def test_a_card_already_over_when_first_sent_gets_no_recovery(self):
        req = self.mgr.create(blocking())
        self.mgr.resolve(req.id)
        self.assertEqual([p["op"] for p in self.drive()], ["message"])
        self.assertEqual(self.drive(), [])


if __name__ == "__main__":
    unittest.main()
