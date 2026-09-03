#!/usr/bin/env python3
"""The briefing must say how many pending questions reach no surface.

`check-pending-questions.py` notifies `questions[:VISIBLE_PREFIX]`, so waiting
order IS priority order: everything past the prefix counts as open and renders
nowhere the owner reads. On 2026-09-03 there were 38 waiting and 33 of them were
invisible, while the briefing said only "38 pending questions. Top item: ...".

The audit used to be prose in the cron prompt telling the agent to name
below-fold items *after* running this script — but the script publishes its
result immediately and the bridge claims it, so the append had nowhere to land
and the step silently no-opped. Counting it here is a mechanism instead: it runs
whether or not anyone remembers.

Run: python3 tests/morning-briefing-below-fold.test.py
"""
import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MB_PATH = REPO / "src" / "morning-briefing.py"


def _load_mb():
    spec = importlib.util.spec_from_file_location("morning_briefing", MB_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBelowFoldCount(unittest.TestCase):
    def setUp(self):
        if not MB_PATH.exists():
            self.skipTest("morning-briefing.py not found")
        self.mb = _load_mb()
        self.orig = getattr(self.mb._CPQ, "VISIBLE_PREFIX", None)

    def tearDown(self):
        if self.orig is None:
            if hasattr(self.mb._CPQ, "VISIBLE_PREFIX"):
                del self.mb._CPQ.VISIBLE_PREFIX
        else:
            self.mb._CPQ.VISIBLE_PREFIX = self.orig

    def test_prefix_is_read_from_the_notifier_not_hardcoded_here(self):
        # A local copy of the number is the bug this whole module family keeps
        # having: the two would drift and the briefing would misreport.
        self.assertIsInstance(self.orig, int)
        self.mb._CPQ.VISIBLE_PREFIX = 7
        self.assertEqual(self.mb.below_fold_count(10), 3)

    def test_the_live_shape(self):
        self.mb._CPQ.VISIBLE_PREFIX = 5
        self.assertEqual(self.mb.below_fold_count(38), 33)

    def test_nothing_hidden_when_the_list_fits(self):
        self.mb._CPQ.VISIBLE_PREFIX = 5
        self.assertEqual(self.mb.below_fold_count(5), 0)
        self.assertEqual(self.mb.below_fold_count(3), 0)
        self.assertEqual(self.mb.below_fold_count(0), 0)

    def test_an_unreadable_prefix_yields_zero_not_a_guess(self):
        # Absent, wrong-typed, or negative: the cutoff is unknown, and the
        # briefing must not state a number it cannot derive.
        del self.mb._CPQ.VISIBLE_PREFIX
        self.assertEqual(self.mb.below_fold_count(38), 0)
        self.mb._CPQ.VISIBLE_PREFIX = "five"
        self.assertEqual(self.mb.below_fold_count(38), 0)
        self.mb._CPQ.VISIBLE_PREFIX = -1
        self.assertEqual(self.mb.below_fold_count(38), 0)

    def test_bool_is_not_an_int_here(self):
        # True == 1 under `isinstance(x, int)`, which would silently claim a
        # 1-item fold. The guard exists so the assertion below can hold.
        self.mb._CPQ.VISIBLE_PREFIX = True
        self.assertEqual(self.mb.below_fold_count(38), 0)


class TestNarrativeSaysIt(unittest.TestCase):
    def setUp(self):
        if not MB_PATH.exists():
            self.skipTest("morning-briefing.py not found")
        self.mb = _load_mb()
        self.orig = getattr(self.mb._CPQ, "VISIBLE_PREFIX", None)
        self.mb._CPQ.VISIBLE_PREFIX = 5

    def tearDown(self):
        if self.orig is not None:
            self.mb._CPQ.VISIBLE_PREFIX = self.orig

    def _narrate(self, n):
        return self.mb.synthesize(
            weather=None, events=[], reminders=[], discord_msgs=[],
            pending_qs=[f"q{i}" for i in range(n)], health_issues=[])

    def test_the_hidden_count_is_spoken(self):
        text = self._narrate(38)
        self.assertIn("38 pending questions", text)
        self.assertIn("33 of them render below the fold", text)

    def test_silent_when_everything_is_visible(self):
        text = self._narrate(4)
        self.assertIn("4 pending questions", text)
        self.assertNotIn("below the fold", text)

    def test_silent_on_a_single_question(self):
        text = self._narrate(1)
        self.assertNotIn("below the fold", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
