#!/usr/bin/env python3
"""A clicked decision card says what came of the answer: `outcome` on the space.ag2.hitl wire is
`running` while the click is applied, then `done` or `failed` with one short line, or the neutral
`closed` when the card closed before verification saw either. The producer's
own word wins, then a recovery receipt, then the status; a card still asking carries none. A
worker's card leads with the question, its seat line last."""
import importlib.util
import sys
import tempfile
import time
import types
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PQ = REPO / "skills" / "pending-questions" / "scripts"
for p in (REPO / "src", PQ):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pending_questions_clicks as cards
import pending_questions_lease as lease_mod
import pending_questions_reader as reader
from hitl import projector
from hitl.manager import HitlManager, HitlStore
from hitl.schema import (
    CLOSED_UNVERIFIED_TEXT, OUTCOME_TEXT_MAX, RECEIPT_FAILED_TEXT, SUPERSEDED_TEXT, Action, ActionReply, HumanRequirement,
    outcome_wire,
)


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Store(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="hitl-outcome-"))
        (self.ws / "state").mkdir()
        self.m = HitlManager(HitlStore(self.ws / "state" / "hitl" / "requirements"))

    def card(self, runtime="claude", subject=None, guard="g1"):
        req = HumanRequirement(kind="choice", runtime=runtime, message="Merge #12?", guard=guard,
                               actions=[Action(id="yes", kind="confirmation", label="Yes"),
                                        Action(id="no", kind="confirmation", label="No")],
                               subject=dict(subject or {}))
        return self.m.create(req).id

    def click(self, rid, action="yes"):
        req = self.m.get(rid)
        self.m.apply_action(ActionReply(hitl_id=rid, expected_revision=req.revision, action_id=action,
                                        guard=req.guard))

    def wire(self, rid):
        return self.m.get(rid).to_wire()


class WireOutcome(_Store):
    def test_a_card_still_asking_carries_no_outcome(self):
        self.assertNotIn("outcome", self.wire(self.card()))

    def test_a_clicked_card_runs_then_is_done(self):
        rid = self.card()
        self.click(rid)
        self.assertEqual(self.wire(rid)["outcome"], {"state": "running"})
        self.m.resolve(rid)
        self.assertEqual(self.wire(rid)["outcome"], {"state": "done"})

    def test_the_producers_word_wins_over_the_status_in_the_same_write(self):
        rid = self.card()
        self.click(rid)
        before = self.m.get(rid).revision
        self.m.resolve(rid, "failed", "  The merge was refused:\n the branch is protected. ")
        req = self.m.get(rid)
        self.assertEqual(req.revision, before + 1, "one write, one new revision to project")
        self.assertEqual(req.to_wire()["outcome"],
                         {"state": "failed", "text": "The merge was refused: the branch is protected."})

    def test_an_unknown_outcome_state_is_refused_and_nothing_is_saved(self):
        rid = self.card()
        self.click(rid)
        with self.assertRaises(ValueError):
            self.m.resolve(rid, "maybe")
        self.assertEqual(self.m.get(rid).status, "in_progress")

    def test_a_cancel_without_a_word_leaves_it_to_the_status(self):
        rid = self.card()
        self.click(rid)
        self.m.cancel(rid)
        self.assertNotIn("outcome", self.wire(rid))

    def test_a_long_text_is_cut_at_a_word(self):
        out = outcome_wire("done", "word " * 100)
        self.assertLessEqual(len(out["text"]), OUTCOME_TEXT_MAX)
        self.assertTrue(out["text"].endswith("word…"))


class ReceiptOutcome(_Store):
    def receipted(self, outcome, text=None, superseded=False, click=True, verify=None):
        rid = self.card(subject={"recovery": True}, guard=f"g-{outcome}-{verify}")
        if click:
            self.click(rid)
        with self.m.store.locked():
            req = self.m.get(rid)
            req.subject = {**req.subject, "receipt": {"outcome": outcome, "at": 1,
                                                      **({"text": text} if text else {})}}
            if verify is not None:
                req.subject["verify"] = verify
            if superseded:
                req.subject["superseded"] = True
            req.revision += 1
            self.m.store.save(req)
        return rid

    def test_a_done_receipt_is_done_with_its_line(self):
        self.assertEqual(self.wire(self.receipted("done", "It is working again."))["outcome"],
                         {"state": "done", "text": "It is working again."})

    def test_a_refused_or_failed_receipt_fails_in_plain_words(self):
        for outcome in ("refused", "stale", "failed"):
            with self.subTest(outcome=outcome):
                self.assertEqual(self.wire(self.receipted(outcome))["outcome"],
                                 {"state": "failed", "text": RECEIPT_FAILED_TEXT[outcome]})

    def test_a_superseded_done_says_it_is_still_blocked(self):
        self.assertEqual(self.wire(self.receipted("done", superseded=True))["outcome"],
                         {"state": "failed", "text": SUPERSEDED_TEXT})

    def test_a_done_receipt_still_verifying_runs(self):
        rid = self.receipted("done", "Sent Escape.", verify={"outcome": "done", "at": 1})
        self.assertEqual(self.wire(rid)["outcome"], {"state": "running"})

    def test_a_done_receipt_closed_unverified_is_neutral(self):
        for close in ("resolve", "cancel"):
            with self.subTest(close=close):
                rid = self.receipted("done", "Sent Escape.", verify={"outcome": "done", "at": 1})
                getattr(self.m, close)(rid)
                self.assertEqual(self.wire(rid)["outcome"], {"state": "closed", "text": CLOSED_UNVERIFIED_TEXT},
                                 "closed before verification: never done, never failed")

    def test_a_stalled_verification_keeps_running(self):
        rid = self.receipted("done", "Sent Escape.", verify={"outcome": "done", "at": 1})
        with mock.patch("time.time", return_value=time.time() + 7 * 86400):
            self.assertEqual(self.wire(rid)["outcome"], {"state": "running"}, "no timeout turns it into a verdict")

    def test_an_unanswered_restart_is_neutral_not_failed(self):
        rid = self.receipted("failed", "I couldn't confirm it came back.",
                             verify={"outcome": "done", "seen": "unknown"})
        self.m.resolve(rid)
        self.assertEqual(self.wire(rid)["outcome"], {"state": "closed", "text": "I couldn't confirm it came back."})

    def test_what_verification_saw_decides_a_done_receipt(self):
        for seen, state in (("working", "done"), ("up", "closed"), ("blocked", "failed"), ("failed", "failed")):
            with self.subTest(seen=seen):
                rid = self.receipted("done", "what was seen", verify={"outcome": "done", "seen": seen})
                self.assertEqual(self.wire(rid)["outcome"], {"state": state, "text": "what was seen"})

    def test_a_failed_action_keeps_its_own_line_when_the_block_clears(self):
        ra = _load("recovery_actions", REPO / "src" / "recovery_actions.py")
        rid = self.card(subject={"recovery": True, "session": "s1"})
        self.click(rid)
        ra._answer_card(self.m, self.m.get(rid), "It could not be switched.", "failed", time.time())
        ra.verify_step(self.m, self.ws, "s1", blocked=False)
        self.assertIn(ra.CLEARED, self.m.get(rid).message)
        self.assertEqual(self.wire(rid)["outcome"], {"state": "failed", "text": "It could not be switched."})

    def test_a_stale_tap_on_a_card_still_asking_keeps_its_buttons(self):
        self.assertNotIn("outcome", self.wire(self.receipted("stale", click=False)))

    def test_the_recovery_card_receipt_carries_the_line_the_card_shows(self):
        ra = _load("recovery_actions", REPO / "src" / "recovery_actions.py")
        rid = self.card(subject={"recovery": True})
        self.click(rid)
        ra._answer_card(self.m, self.m.get(rid), "The restart did not complete.", "failed", time.time())
        self.assertEqual(self.wire(rid)["outcome"], {"state": "failed", "text": "The restart did not complete."})


class Projection(_Store):
    def test_the_card_edit_carries_the_outcome_and_the_plain_body_says_it(self):
        rid = self.card()
        sent = []
        send = lambda payload: (sent.append(payload), {"ok": True, "event_id": f"$e{len(sent)}"})[1]  # noqa: E731
        projector.project(self.m, send, "!dm:x")
        self.click(rid)
        self.m.resolve(rid, "failed", "The branch is protected.")
        projector.project(self.m, send, "!dm:x")
        edit = sent[-1]
        self.assertEqual(edit["op"], "edit")
        self.assertEqual(edit["extra_content"]["space.ag2.hitl"]["outcome"],
                         {"state": "failed", "text": "The branch is protected."})
        self.assertTrue(edit["body"].endswith("✕ The branch is protected."), edit["body"])
        self.assertNotIn("Resolved", edit["body"])

    def test_the_plain_body_of_a_card_closed_unverified_is_neither_tick_nor_cross(self):
        rid = self.card(subject={"recovery": True})
        sent = []
        send = lambda payload: (sent.append(payload), {"ok": True, "event_id": f"$e{len(sent)}"})[1]  # noqa: E731
        projector.project(self.m, send, "!dm:x")
        self.click(rid)
        with self.m.store.locked():
            req = self.m.get(rid)
            req.subject = {**req.subject, "receipt": {"outcome": "done", "at": 1},
                           "verify": {"outcome": "done", "at": 1}}
            req.revision += 1
            self.m.store.save(req)
        self.m.resolve(rid)
        projector.project(self.m, send, "!dm:x")
        edit = sent[-1]
        self.assertEqual(edit["extra_content"]["space.ag2.hitl"]["outcome"]["state"], "closed")
        self.assertTrue(edit["body"].endswith(f"— {CLOSED_UNVERIFIED_TEXT}"), edit["body"])
        self.assertNotIn("✓", edit["body"])


class PendingQuestionAck(_Store):
    def handed(self):
        rid = self.card(runtime=cards.CARD_RUNTIME, subject={"ask_id": "q1", "choices": {"yes": "merge"}},
                        guard="q1")
        self.click(rid)
        cards.stamp_claim(self.m, rid, {"token": "t", "state": cards.HANDED, "lease": lease_mod.new(time.time())})
        return rid

    def test_a_click_runs_until_the_ack_says_done(self):
        rid = self.handed()
        self.assertEqual(self.wire(rid)["outcome"], {"state": "running"})
        self.assertTrue(cards.ack_click(self.ws, "q1", "t", self.m)[0])
        self.assertEqual(self.wire(rid)["outcome"], {"state": "done"})

    def test_an_ack_can_say_it_failed_with_a_line(self):
        rid = self.handed()
        self.assertTrue(cards.ack_click(self.ws, "q1", "t", self.m, "failed", "CI is red, so it was not merged.")[0])
        self.assertEqual(self.wire(rid)["outcome"], {"state": "failed", "text": "CI is red, so it was not merged."})

    def test_the_reader_passes_the_outcome_only_to_an_adapter_that_takes_it(self):
        got = []
        new = types.SimpleNamespace(gather=lambda ws: {}, ack_click=lambda ws, a, t, outcome=None, text="":
                                    (got.append((outcome, text)), (True, "acknowledged"))[1])
        old = types.SimpleNamespace(gather=lambda ws: {}, ack_click=lambda ws, a, t: (True, "acknowledged"))
        self.assertEqual(reader.ack(self.ws, "q1", "t", new, "failed", "no"), (True, "acknowledged"))
        self.assertEqual(got, [("failed", "no")])
        ok, msg = reader.ack(self.ws, "q1", "t", old, None, "done it")
        self.assertTrue(ok)
        self.assertIn("shows no outcome", msg)
        self.assertEqual(reader.ack(self.ws, "q1", "t", old), (True, "acknowledged"))


class SeatLine(unittest.TestCase):
    def test_a_workers_card_leads_with_the_question(self):
        ciw = _load("core_input_watch", REPO / "src" / "core-input-watch.py")
        for subject in ({}, {"recovery": True}):
            with self.subTest(subject=subject):
                req = HumanRequirement(kind="choice", runtime="claude", message="Allow the edit?",
                                       title="Agent needs a decision", subject=dict(subject))
                ciw._name_seat(req, "sutando-worker-ab12", "worker Mars")
                lines = req.message.split("\n")
                self.assertTrue(lines[0].startswith("Allow the edit?"))
                self.assertEqual(lines[-1], "worker Mars (tmux session sutando-worker-ab12)")
                self.assertEqual(req.device, {"id": "sutando-worker-ab12", "name": "worker Mars"})


if __name__ == "__main__":
    unittest.main()
