#!/usr/bin/env python3
"""A refusal must carry the roster's REASON, or the obvious repair overrides it.

Issue #3468: a blank `stand`/`room` can be missing data OR a deliberate
DO-NOT-ROUTE. `resolve()` refused correctly and explained it mechanically
("needs both 'stand' and 'room'"), which reads as a data gap — so the natural
repair is to populate the fields, silently converting the refusal into a
routable entry. The roster held the reason; the one code path that could
surface it discarded it.

Every assertion is paired with its negative: a note must appear when present
AND must not be invented when absent. A suite that only checks the first
proves the string was added, not that it came from the entry.

Run: python3 tests/sci-notify-reviewers-refusal-basis.test.py   (stdlib only)
"""
import importlib.util
import io
import contextlib
import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent / "skills"
          / "collaboration-intelligence" / "scripts" / "notify_reviewers.py")


def _load():
    spec = importlib.util.spec_from_file_location("_nr_basis", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASIS = "DO NOT ROUTE. Owner instruction; the empty stand is not missing data."


class StatedReason(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_refusal_basis_wins_over_note(self):
        r = self.mod.stated_reason({"refusal_basis": "the basis", "note": "the note"})
        self.assertEqual(r, "the basis")

    def test_note_is_used_when_there_is_no_basis(self):
        self.assertEqual(self.mod.stated_reason({"note": "the note"}), "the note")

    def test_absent_blank_and_non_string_all_yield_empty(self):
        for entry in ({}, {"note": ""}, {"note": "   "}, {"note": None}, {"note": ["x"]}):
            self.assertEqual(self.mod.stated_reason(entry), "",
                             f"{entry!r} must not produce a reason")

    def test_newlines_are_folded_so_the_reason_stays_one_line(self):
        self.assertEqual(self.mod.stated_reason({"note": "a\n  b\nc"}), "a b c")


class RefusalOutput(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _resolve(self, roster, names=("x",)):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            targets, rc = self.mod.resolve(list(names), roster)
        return targets, rc, err.getvalue()

    # --- blank stand/room ------------------------------------------------
    def test_unusable_entry_prints_the_roster_reason(self):
        _, rc, err = self._resolve({"x": {"stand": "", "room": "", "note": BASIS}})
        self.assertEqual(rc, 3)
        self.assertIn("DO NOT ROUTE", err)

    def test_CONTROL_unusable_without_a_note_invents_nothing(self):
        _, rc, err = self._resolve({"x": {"stand": "", "room": ""}})
        self.assertEqual(rc, 3)
        self.assertIn("UNUSABLE", err)
        self.assertNotIn("roster says", err,
                         "a reason must come from the entry, never from the code")

    # --- off-allowlist ---------------------------------------------------
    def test_off_allowlist_states_the_roster_reason_instead_of_asserting_a_bounce(self):
        _, rc, err = self._resolve(
            {"x": {"stand": "@s:x", "room": "!r:x", "allowlisted": False, "note": BASIS}})
        self.assertEqual(rc, 4)
        self.assertIn("DO NOT ROUTE", err)
        self.assertNotIn("bounced", err,
                         "the code must not assert a history it never checked")

    def test_CONTROL_off_allowlist_without_a_note_claims_no_history_either(self):
        # The old wording asserted "bounced a mention before". Nothing sets
        # allowlisted=False after a bounce, so that was a guess, not a fallback.
        _, rc, err = self._resolve({"x": {"stand": "@s:x", "room": "!r:x", "allowlisted": False}})
        self.assertEqual(rc, 4)
        self.assertIn("not allowlisted for mentions", err)
        self.assertNotIn("bounced", err,
                         "with no stated reason the code must still not invent a cause")

    # --- the batch must not be starved, and rc must not drift ------------
    def test_CONTROL_a_usable_entry_still_resolves_alongside_a_refused_one(self):
        roster = {"bad": {"stand": "", "room": "", "note": BASIS},
                  "good": {"stand": "@g:x", "room": "!r:x", "human": "@h:x"}}
        targets, rc, _ = self._resolve(roster, names=("bad", "good"))
        self.assertEqual([t["name"] for t in targets], ["good"])
        self.assertEqual(rc, 3, "the worst refusal code must still reach the caller")


if __name__ == "__main__":
    unittest.main(verbosity=2)
