#!/usr/bin/env python3
"""A blocked core raises a HumanRequirement, so it reaches the owner as a card.

ESCALATE was a banner in the Runtime window, so learning the core was stuck
required already looking at that window (owner report 2026-09-03, after a
weekly model-limit prompt held the core all morning).

`src/hitl` already owns requirement dedup and card projection; the monitor is
the driver it never had. These tests pin what the monitor contributes: the
right states, an episode key the Manager can dedup on, resolution when the
core moves on, and never dying of its own escalation.
"""
import importlib.util as u
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))
spec = u.spec_from_file_location("ciw", SRC / "core-input-watch.py")
M = u.module_from_spec(spec)
spec.loader.exec_module(M)

from hitl.manager import HitlManager, HitlStore  # noqa: E402
from hitl.schema import STATUS_RESOLVED  # noqa: E402


def _mgr():
    return HitlManager(HitlStore(pathlib.Path(tempfile.mkdtemp())))


class TestMessage(unittest.TestCase):
    def test_it_names_the_gate_and_quotes_the_terminal(self):
        b = M.escalation_message("blocked-human", "awaiting user: selection",
                                 "selection", "Pick one:\n> a\n  b")
        self.assertIn("cannot continue without you", b)
        self.assertIn("selection", b)
        self.assertIn("Pick one:", b)

    def test_an_unknown_gate_is_not_rendered_as_a_gate_name(self):
        """`unknown` is the classifier saying it could not tell, so `Gate:
        unknown` reads as a fact about the prompt rather than about us."""
        b = M.escalation_message("blocked-human", "d", "unknown", "x")
        self.assertNotIn("Gate: unknown", b)

    def test_logged_out_says_signed_out_not_blocked(self):
        self.assertIn("signed out",
                      M.escalation_message("logged-out", "d", None, None))


class TestEscalate(unittest.TestCase):
    def test_it_raises_one_requirement_with_an_actionable_card(self):
        m = _mgr()
        req = M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "sutando-core")
        self.assertIsNotNone(req)
        self.assertEqual(req.kind, M.HITL_KIND)
        self.assertTrue(req.actions, "a card with no action cannot be answered")
        self.assertIn("Pick one:", req.message)

    def test_the_same_prompt_held_for_many_ticks_raises_one_card(self):
        m = _mgr()
        ids = {M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "s").id
               for _ in range(40)}
        self.assertEqual(len(ids), 1)

    def test_a_different_prompt_is_a_different_card(self):
        """Two things needing the owner are two cards, even back to back —
        the second is not a redraw of the first."""
        m = _mgr()
        a = M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "s")
        b = M.escalate(m, "blocked-human", "d", "login", "Log in:", "s")
        self.assertNotEqual(a.id, b.id)

    def test_the_prompt_is_the_episode_key_not_the_state(self):
        """Guard is derived from the prompt: keying on state alone would
        collapse two unrelated blocks into one card."""
        m = _mgr()
        a = M.escalate(m, "blocked-human", "d", "selection", "A", "s")
        b = M.escalate(m, "blocked-human", "d", "selection", "B", "s")
        self.assertNotEqual(a.guard, b.guard)

    def test_no_manager_is_survivable(self):
        """The monitor must outlive its own escalation: if this raised, the
        layer that DETECTS the block would die with the block unreported."""
        self.assertIsNone(M.escalate(None, "blocked-human", "d", "k", "p", "s"))


class TestResolve(unittest.TestCase):
    def test_leaving_the_blocked_set_resolves_the_card(self):
        m = _mgr()
        req = M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "s")
        self.assertEqual(M.resolve_escalations(m, "s"), [req.id])
        self.assertEqual(m.get(req.id).status, STATUS_RESOLVED)

    def test_it_resolves_only_its_own_kind(self):
        """A permission card belongs to the hook driver; clearing it because
        the CORE unblocked would dismiss a question nobody answered."""
        m = _mgr()
        from hitl.schema import Action, HumanRequirement
        other = m.create(HumanRequirement(kind="permission", runtime="claude",
                                          message="may I", guard="g1",
                                          actions=[Action("allow", "allow", "Allow")]))
        M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "s")
        self.assertNotIn(other.id, M.resolve_escalations(m, "s"))
        self.assertNotEqual(m.get(other.id).status, STATUS_RESOLVED)

    def test_it_resolves_only_its_own_session(self):
        """A pool shares one login, so a weekly limit blocks every worker on
        byte-identical prompt text. One worker recovering must not clear a
        sibling's card, and two blocked workers are two cards, not one."""
        m = _mgr()
        P = "You've reached your Fable limit"
        a = M.escalate(m, "blocked-human", "d", "fable-limit-unfocused", P, "worker-1")
        b = M.escalate(m, "blocked-human", "d", "fable-limit-unfocused", P, "worker-2")
        self.assertNotEqual(a.id, b.id, "two blocked workers collapsed to one card")
        self.assertEqual(len(m.active()), 2)
        self.assertEqual(M.resolve_escalations(m, "worker-1"), [a.id])
        self.assertEqual([r.id for r in m.active()], [b.id],
                         "worker-1 recovering cleared worker-2's card")

    def test_a_second_block_after_recovery_raises_again(self):
        m = _mgr()
        first = M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "s")
        M.resolve_escalations(m, "s")
        again = M.escalate(m, "blocked-human", "d", "selection", "Pick one:", "s")
        self.assertNotEqual(first.id, again.id)


class TestScope(unittest.TestCase):
    def test_states_with_an_automatic_remedy_are_not_escalated(self):
        """`blocked-known` is auto-answered and `hung` is RECOVER's job; both
        resolve without the owner, so neither is theirs to be woken for."""
        for s in ("blocked-known", "hung", "crashed", "running", "idle-ready"):
            self.assertNotIn(s, M._CHAT_ESCALATE_STATES, s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
