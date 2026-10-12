"""Follow-up notices: a still-unresolved blocking requirement re-alerts as a NEW message
on a 30m / 2h / 6h backoff, announces recovery, and keeps its schedule across a restart."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hitl.manager import HitlManager, HitlStore  # noqa: E402
from hitl.projector import pending_ids, project  # noqa: E402

from hitl import renotify  # noqa: E402
from hitl.schema import (  # noqa: E402
    NOTICE_FIELD, STATUS_CANCELLED, Action, HumanRequirement, WIRE_FIELD)

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


class FlakySender(Sender):
    """Rejects a chosen requirement's send exactly once, then accepts it."""

    def __init__(self, reject_req_id):
        super().__init__()
        self._reject = reject_req_id
        self._rejected_once = False

    def __call__(self, payload):
        if not self._rejected_once and self._reject in payload.get("dedupe_key", ""):
            self._rejected_once = True
            self.sent.append(payload)
            return {"ok": False}
        return super().__call__(payload)


def seat_card(seat, guard, created=T0):
    """A worker seat's core-blocked card, as core-input-watch raises one per repaint."""
    return HumanRequirement(
        kind="core-blocked", runtime="claude", message=f"worker {seat}\n\nI am blocked.",
        title=f"worker {seat} · blocked-human", guard=guard,
        device={"id": f"sutando-worker-{seat}", "name": f"worker {seat}"},
        subject={"source": "tui", "session": f"sutando-worker-{seat}"},
        created_at=created, updated_at=created)


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
        self.assertNotIn(WIRE_FIELD, sent[0]["extra_content"])  # never a second card
        self.assertEqual(sent[0]["extra_content"][NOTICE_FIELD],
                         {"notice": "reminder", "ids": [req.id], "runtime": "claude",
                          "device": "core-1", "host": req.wire_device().get("host") or ""})
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
        self.assertIn("✓ Reconnected — Claude Code needs to sign in again; resuming 3 queued tasks.",
                      sent[1]["body"])
        self.clock.advance(7 * HOUR)
        self.assertEqual(self.drive(), [])  # once, and no reminder after resolution

    # -- one seat, many cards ---------------------------------------------------------

    def _seat_cards(self, seat, n, step=MIN):
        ids = []
        for i in range(n):
            ids.append(self.mgr.create(seat_card(seat, f"{seat}-g{i}", self.clock.t)).id)
            self.drive()
            self.clock.advance(step)
        return ids

    def test_many_cards_for_one_seat_resolved_at_once_send_one_recovery(self):
        ids = self._seat_cards("pro-dev", 58)
        for i in ids:
            self.mgr.resolve(i)
        sent = self.drive()
        messages = [p for p in sent if p["op"] == "message"]
        self.assertEqual(len([p for p in sent if p["op"] == "edit"]), 58)
        self.assertEqual(len(messages), 1, [m["body"] for m in messages])
        self.assertEqual(messages[0]["body"],
                         "✓ Cleared — worker pro-dev · blocked-human; Sutando has continued its work.")
        notice = messages[0]["extra_content"][NOTICE_FIELD]
        self.assertEqual((notice["notice"], sorted(notice["ids"])), ("recovery", sorted(ids)))
        self.assertEqual(notice["device"], "sutando-worker-pro-dev")  # the seat's device id
        self.assertTrue(all(self.mgr.store.projection(i).get("recovered") for i in ids))
        self.clock.advance(7 * HOUR)
        self.assertEqual(self.drive(), [])
        self.assertEqual(pending_ids(self.mgr, clock=self.clock), [])

    def test_a_rejected_resolve_edit_does_not_split_the_recovery(self):
        ids = self._seat_cards("pro-dev", 3)
        for i in ids:
            self.mgr.resolve(i)
        straggler = ids[0]
        flaky = FlakySender(straggler)

        first = project(self.mgr, flaky, ROOM, clock=self.clock)
        self.assertEqual([p["op"] for p in flaky.sent if p["op"] == "message"], [],
                         "no recovery yet: the straggler's edit hasn't landed")
        self.assertFalse(self.mgr.store.projection(straggler).get("recovered"))

        self.clock.advance(MIN)
        second_sent_before = len(flaky.sent)
        project(self.mgr, flaky, ROOM, clock=self.clock)
        second = flaky.sent[second_sent_before:]
        messages = [p for p in second if p["op"] == "message"]
        self.assertEqual(len(messages), 1, [m["body"] for m in messages])
        self.assertTrue(all(self.mgr.store.projection(i).get("recovered") for i in ids))

    def test_one_recovery_per_seat(self):
        seats = ("pro-dev", "pro-fast", "pro-comm")
        ids = [i for seat in seats for i in self._seat_cards(seat, 4)]
        for i in ids:
            self.mgr.resolve(i)
        bodies = sorted(p["body"] for p in self.drive() if p["op"] == "message")
        self.assertEqual(bodies, sorted(
            f"✓ Cleared — worker {s} · blocked-human; Sutando has continued its work." for s in seats))

    def test_a_seat_still_blocked_owes_no_recovery_yet(self):
        old, new = self._seat_cards("pro-dev", 2)
        self.mgr.resolve(old)
        self.assertEqual([p["op"] for p in self.drive()], ["edit"])
        self.mgr.resolve(new)
        sent = self.drive()
        self.assertEqual([p["op"] for p in sent], ["edit", "message"])
        self.assertIn("✓ Cleared — worker pro-dev", sent[1]["body"])

    def test_many_open_cards_for_one_seat_share_one_reminder_clock(self):
        self._seat_cards("pro-dev", 20, step=3 * MIN)
        reminders = []
        for _ in range(int(4 * HOUR // MIN)):
            self.clock.advance(MIN)
            reminders += [p["body"] for p in self.drive() if p["op"] == "message"]
        # 30m after the newest card, then 2h later: two notices, not two per card.
        self.assertEqual(len(reminders), 2, reminders)
        self.assertTrue(reminders[0].startswith("⚠ Still blocked after 1h 27m — worker pro-dev"),
                        reminders[0])

    def test_a_new_card_inherits_the_seats_backoff(self):
        self._seat_cards("pro-dev", 1)
        self.clock.advance(31 * MIN)
        self.assertEqual(len(self.drive()), 1)  # reminder #1, count -> 1
        self.clock.advance(2 * HOUR + MIN)
        self.assertEqual(len(self.drive()), 1)  # reminder #2, count -> 2 (next interval 6h)
        self.mgr.create(seat_card("pro-dev", "new-card", self.clock.t))
        self.drive()  # the new card's own creation message, not a renotify notice
        self.clock.advance(31 * MIN)  # past the new card's own 30m mark
        self.assertEqual(self.drive(), [], "the seat's clock, not the new card's, governs")
        self.clock.advance(6 * HOUR)
        sent = [p for p in self.drive() if p["op"] == "message"]
        self.assertEqual(len(sent), 1)

    def test_a_reminder_for_one_seat_does_not_silence_another(self):
        self._seat_cards("pro-dev", 3)
        self._seat_cards("pro-fast", 3)
        self.clock.advance(31 * MIN)
        bodies = [p["body"] for p in self.drive() if p["op"] == "message"]
        self.assertEqual(len(bodies), 2)
        self.assertTrue(any("pro-dev" in b for b in bodies) and any("pro-fast" in b for b in bodies))

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

    def _pre_upgrade(self, req, edited=True):
        """A record the old projector wrote: a bare {revision, event_id} ledger."""
        self.mgr.create(req)
        self.mgr.resolve(req.id)
        stored = self.mgr.store.load(req.id)
        rev = stored.revision if edited else 1
        self.mgr.store.save(stored, projection={"revision": rev, "event_id": "$old-" + req.id})
        return stored

    def test_a_card_resolved_before_the_upgrade_owes_no_recovery(self):
        self._pre_upgrade(blocking())
        self.clock.advance(7 * HOUR)
        self.assertEqual(self.drive(), [])

    def test_a_pre_upgrade_card_whose_resolve_edit_is_pending_gets_only_the_edit(self):
        self._pre_upgrade(blocking(), edited=False)
        self.assertEqual([p["op"] for p in self.drive()], ["edit"])
        self.assertEqual(self.drive(), [])

    def test_one_pulse_over_a_pre_upgrade_store_sends_no_recovery(self):
        for i in range(100):
            kind = ("auth", "core-blocked", "choice")[i % 3]
            self._pre_upgrade(blocking(kind=kind, guard=f"g{i}"), edited=i % 4 != 0)
        sent = self.drive()
        self.assertEqual([p for p in sent if p["op"] == "message"], [])
        self.assertTrue(all(p["op"] == "edit" for p in sent))

    def test_a_pre_upgrade_card_re_alerted_after_upgrade_announces_recovery(self):
        req = self.mgr.create(blocking())
        stored = self.mgr.store.load(req.id)
        self.mgr.store.save(stored, projection={"revision": stored.revision, "event_id": "$old"})
        self.clock.advance(31 * MIN)
        self.assertIn("Still blocked", self.drive()[0]["body"])
        self.mgr.resolve(req.id)
        self.assertEqual([p["op"] for p in self.drive()], ["edit", "message"])

    def test_a_block_longer_than_a_day_reads_in_days(self):
        req = self.mgr.create(blocking())
        self.assertIn("after 1d 2h", renotify.body(renotify.REMINDER, req, T0 + 26 * HOUR))

    def test_an_unprojected_revision_waits_for_its_edit(self):
        req = blocking()
        req.revision = 3
        self.assertIsNone(renotify.due(req, {"event_id": "$e", "revision": 2}, T0 + 7 * HOUR))

    def test_a_cancelled_card_gets_neither_reminder_nor_recovery(self):
        req = blocking()
        req.status = STATUS_CANCELLED
        self.assertIsNone(renotify.due(req, {"event_id": "$e", "revision": 1}, T0 + 7 * HOUR))

    def test_a_notice_for_a_deleted_record_is_a_no_op(self):
        self.mgr.record_notice("hitl-missing", {"recovered": True})
        self.assertIsNone(self.mgr.store.load("hitl-missing"))


if __name__ == "__main__":
    unittest.main()
