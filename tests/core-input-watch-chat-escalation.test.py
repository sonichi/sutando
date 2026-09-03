#!/usr/bin/env python3
"""A blocked core must reach the owner where they already are.

ESCALATE was a banner in the Runtime window, so the owner had to be looking at
that window to learn the core was stuck (owner report 2026-09-03, after a
weekly model-limit prompt held the core all morning). The monitor is a separate
process and the bridge claims `results/proactive-*.txt` independently of the
core, so the announcement can be made by the one layer that is not blocked.

The whole design risk is repetition: a stuck core stays stuck for as long as it
takes someone to act, and a per-tick announcement would turn one problem into a
room full of them. These tests pin once-per-episode.
"""
import importlib.util as u
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src" / "core-input-watch.py"
spec = u.spec_from_file_location("ciw", SRC)
M = u.module_from_spec(spec)
spec.loader.exec_module(M)


def _ws():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "state"), exist_ok=True)
    return d, os.path.join(d, "state", "core-supervisor.json")


class TestEscalationBody(unittest.TestCase):
    def test_it_names_the_gate_and_quotes_the_terminal(self):
        b = M.escalation_body("blocked-human", "awaiting user: selection",
                              "selection", "Pick one:\n> a\n  b")
        self.assertIn("cannot continue without you", b)
        self.assertIn("selection", b)
        self.assertIn("Pick one:", b)

    def test_an_unknown_gate_is_not_rendered_as_a_gate_name(self):
        """`unknown` is the classifier saying it could not tell, so printing
        `Gate: unknown` reads as a fact about the prompt rather than about us."""
        b = M.escalation_body("blocked-human", "awaiting user: unknown", "unknown", "x")
        self.assertNotIn("Gate: unknown", b)

    def test_logged_out_says_signed_out_not_blocked(self):
        b = M.escalation_body("logged-out", "core not authenticated", None, None)
        self.assertIn("signed out", b)


class TestWriter(unittest.TestCase):
    def test_it_writes_into_the_workspace_results_dir(self):
        d, out = _ws()
        p = M.escalate_to_chat(out, "blocked-human", "awaiting user: selection", "selection", "x")
        self.assertIsNotNone(p)
        self.assertEqual(os.path.basename(os.path.dirname(p)), "results")
        self.assertTrue(os.path.basename(p).startswith("proactive-"))
        self.assertIn("cannot continue", open(p).read())

    def test_an_unwritable_results_dir_does_not_raise(self):
        """The monitor must outlive its own escalation: if this raised, the
        layer that DETECTS the block would die with the block unreported."""
        d, out = _ws()
        os.makedirs(os.path.join(d, "results"))
        os.chmod(os.path.join(d, "results"), 0o500)
        try:
            self.assertIsNone(M.escalate_to_chat(out, "blocked-human", "d", "k", "p"))
        finally:
            os.chmod(os.path.join(d, "results"), 0o700)


class TestOncePerEpisode(unittest.TestCase):
    """The loop's own dedup, exercised through the same key it uses."""

    def _run(self, states):
        d, out = _ws()
        episode = None
        written = []
        for state, kind, prompt in states:
            if state in M._CHAT_ESCALATE_STATES:
                ep = (state, kind, prompt)
                if ep != episode:
                    p = M.escalate_to_chat(out, state, "d", kind, prompt)
                    if p:
                        episode = ep
                        written.append(p)
            elif state not in M._CHAT_ESCALATE_STATES:
                episode = None
        return written

    def test_a_block_held_for_many_ticks_announces_once(self):
        ticks = [("blocked-human", "selection", "Pick one:")] * 40
        self.assertEqual(len(self._run(ticks)), 1)

    def test_a_second_block_after_recovery_announces_again(self):
        ticks = ([("blocked-human", "selection", "Pick one:")] * 5
                 + [("running", None, None)] * 5
                 + [("blocked-human", "selection", "Pick one:")] * 5)
        self.assertEqual(len(self._run(ticks)), 2)

    def test_a_different_gate_in_the_same_stretch_announces_again(self):
        """Two different things needing the owner are two announcements, even
        back to back — the second is not a redraw of the first."""
        ticks = ([("blocked-human", "selection", "Pick one:")] * 3
                 + [("blocked-human", "login", "Log in:")] * 3)
        self.assertEqual(len(self._run(ticks)), 2)

    def test_states_with_an_automatic_remedy_are_not_announced(self):
        """`blocked-known` is auto-answered and `hung` is RECOVER's job; both
        would resolve without the owner, so neither is theirs to be woken for."""
        for s in ("blocked-known", "hung", "crashed", "running", "idle-ready"):
            self.assertNotIn(s, M._CHAT_ESCALATE_STATES, s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
