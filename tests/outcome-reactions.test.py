#!/usr/bin/env python3
"""ag2_sparrow/outcome_reactions.py: a reaction on the agent's own message is an outcome signal.

What the handler claims (own-message reactions in the broker's shape, nothing else), the record it
writes (which message, who reacted and as whom, the key, the task), owner vs member evidence,
removals, and a fake feed through the real EventInbox, HandlerChain and EventConsumer: the reaction
is consumed and recorded, never taskified; an approval-card claimant still goes first; an ordinary
message still becomes a task. In process, so coverage sees it.

Run: python3 tests/outcome-reactions.test.py   (stdlib only)
"""
import io
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
from ag2_sparrow import correlation_log as cl, event_consumer, event_inbox, human_action, outcome_reactions  # noqa: E402

EventConsumer, TaskifyHandler = event_consumer.EventConsumer, event_consumer.TaskifyHandler
EventInbox, HandlerChain = event_inbox.EventInbox, human_action.HandlerChain
OutcomeReactionHandler, bucket = outcome_reactions.OutcomeReactionHandler, outcome_reactions.bucket

OWNER = "@owner:s.space"
TASK = "task-s_dspace~task-9905cc47dfa0f6b12e"


def reaction(n, key="👍", relation="owner", reactor=OWNER, kind="reaction.added", task=TASK, **kw):
    ev = {"type": kind, "event_id": f"$r{n}:s.space", "cursor": n, "room_id": "!dm:s.space", "ts": 1,
          "target_event_id": "$result1:s.space", "reactor_id": reactor, "reactor_relation": relation,
          "key": key, "task_id": task}
    ev.update(kw)
    return {k: v for k, v in ev.items() if v is not None}


class Handler(unittest.TestCase):
    def setUp(self):
        self.log = Path(tempfile.mkdtemp()) / "correlation.jsonl"
        self.h = OutcomeReactionHandler(self.log, log=lambda *a: None)

    def _signals(self):
        return cl.read("outcome_signal", path=self.log)

    def test_it_claims_own_message_reactions_only(self):
        self.assertTrue(self.h.claims(reaction(1)))
        self.assertTrue(self.h.claims(reaction(2, kind="reaction.removed")))
        legacy = {"type": "reaction.added", "event_id": "$x", "content": {"m.relates_to": {"event_id": "$y", "key": "👍"}}}
        for ev in (legacy, {"type": "message.created", "event_id": "$m"}, reaction(3, relation="stranger"),
                   reaction(4, reactor=None), reaction(5, target_event_id=None)):
            self.assertFalse(self.h.claims(ev), ev)

    def test_the_record_says_which_message_who_and_as_whom(self):
        self.assertEqual(self.h.offer(reaction(1)), ["$r1:s.space"])
        self.h.offer(reaction(2, key="👎", relation="member", reactor="@guest:s.space"))
        owner, member = self._signals()
        self.assertEqual({k: owner[k] for k in ("signal", "value", "reactor_id", "reactor_relation", "evidence",
                                                 "ref", "task_id", "room_id")},
                         {"signal": "reaction", "value": "👍", "reactor_id": OWNER, "reactor_relation": "owner",
                          "evidence": "direct", "ref": "$result1:s.space", "task_id": TASK, "room_id": "!dm:s.space"})
        self.assertEqual((member["reactor_id"], member["reactor_relation"], member["evidence"], member["value"]),
                         ("@guest:s.space", "member", "correlated", "👎"))
        self.assertNotIn("body", owner)
        self.assertNotIn("content", owner)

    def test_a_removal_is_its_own_signal(self):
        self.h.offer(reaction(1, kind="reaction.removed"))
        self.assertEqual(self._signals()[0]["signal"], "reaction_removed")

    def test_a_bad_task_id_is_dropped_not_the_reaction(self):
        self.h.offer(reaction(1, task="@someone:s.space"))
        self.h.offer(reaction(2, task=None))
        recs = self._signals()
        self.assertEqual(len(recs), 2)
        self.assertTrue(all("task_id" not in r for r in recs))

    def test_a_refused_record_is_still_consumed_and_logged(self):
        said = []
        h = OutcomeReactionHandler(self.log, log=said.append)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(h.offer(reaction(1, key="")), ["$r1:s.space"])
        self.assertEqual(self._signals(), [])
        self.assertIn("not recorded", said[0])

    def test_buckets_are_for_display(self):
        self.assertEqual((bucket("✅"), bucket("👎"), bucket("🤔")), ("positive", "negative", "neutral"))


class CardFirst:
    """Stands for the approval-card handler, which is ahead in the real chain."""

    def __init__(self):
        self.seen = []

    def claims(self, ev):
        return ev.get("target_event_id") == "$card:s.space"

    def offer(self, ev):
        self.seen.append(ev["event_id"])
        return [ev["event_id"]]


class FakeFeed(unittest.TestCase):
    def test_reactions_are_recorded_and_consumed_never_taskified(self):
        d = Path(tempfile.mkdtemp())
        inbox = EventInbox(str(d / "event-inbox.db"))
        tasks = d / "tasks"
        tasks.mkdir()
        log = d / "correlation.jsonl"
        card = CardFirst()
        chain = HandlerChain([card, OutcomeReactionHandler(log, log=lambda *a: None),
                              TaskifyHandler(str(tasks), "@agent:s.space", threshold=1, log=lambda *a: None)])
        for ev in (reaction(1), reaction(2, key="👎", relation="member", reactor="@guest:s.space"),
                   reaction(3, target_event_id="$card:s.space", key="1️⃣"),
                   {"type": "message.created", "event_id": "$m4:s.space", "cursor": 4, "room_id": "!dm:s.space",
                    "actor_id": "@guest:s.space", "content": {"body": "hello"}}):
            self.assertTrue(inbox.insert(ev))
        out = EventConsumer(inbox, chain).drain()
        self.assertEqual(out["consumed"], 4)
        self.assertEqual(inbox.unconsumed(), [])
        self.assertEqual(card.seen, ["$r3:s.space"], "the card claimant answers its own reaction first")
        sig = cl.read("outcome_signal", path=log)
        self.assertEqual([(s["reactor_relation"], s["value"]) for s in sig], [("owner", "👍"), ("member", "👎")])
        made = [p.read_text() for p in tasks.iterdir()]
        self.assertEqual(len(made), 1, "only the message became a task")
        self.assertNotIn("reaction", made[0])


class Wiring(unittest.TestCase):
    """STRUCTURAL, and labelled so: the bridge builds this chain inside its run loop."""

    def test_the_bridge_appends_it_as_the_last_claimant(self):
        src = (REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py").read_text()
        hitl = src.index("claimants.append(_HitlClicks(probe, log=_log))")
        ours = src.index('claimants.append(OutcomeReactionHandler(_STATE / "correlation.jsonl", log=_log))')
        chain = src.index("handler = HandlerChain(claimants + [handler])")
        self.assertLess(hitl, ours)
        self.assertLess(ours, chain)


if __name__ == "__main__":
    os.environ.pop("SUTANDO_INSTANCE_ID", None)
    unittest.main()
