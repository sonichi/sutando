#!/usr/bin/env python3
"""quota-tier picks the MOST restrictive tier, and each window keeps its own rule.

The inversion this file exists to prevent (min instead of max) passes every
same-tier case, so the discriminating tests are the MIXED pairs.
"""
import importlib.util
import io
import pathlib
import sys
import unittest
from contextlib import redirect_stdout

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "skills" / "proactive-loop" / "scripts"
spec = importlib.util.spec_from_file_location("qt", SCRIPTS / "quota-tier.py")
qt = importlib.util.module_from_spec(spec); spec.loader.exec_module(qt)


class Selection(unittest.TestCase):
    def test_mixed_pairs_pick_the_more_restrictive(self):
        # THE regression test: min() returns the left column and is wrong on all 6.
        for a, b, want in [("FULL", "MEDIUM", "MEDIUM"), ("MEDIUM", "FULL", "MEDIUM"),
                           ("FULL", "LIGHT", "LIGHT"), ("LIGHT", "FULL", "LIGHT"),
                           ("MEDIUM", "LIGHT", "LIGHT"), ("LIGHT", "MEDIUM", "LIGHT")]:
            self.assertEqual(qt.most_restrictive(a, b), want, f"{a}+{b}")

    def test_same_tier_pairs_are_the_tier(self):
        # Control: these pass under BOTH min and max, so they cannot detect the bug.
        for t in ("FULL", "MEDIUM", "LIGHT"):
            self.assertEqual(qt.most_restrictive(t, t), t)

    def test_minimal_beats_everything(self):
        self.assertEqual(qt.most_restrictive("FULL", "MINIMAL"), "MINIMAL")


class PerWindowRules(unittest.TestCase):
    def test_5h_uses_retained_budget_not_headroom(self):
        self.assertEqual(qt.tier_5h(97, 141), "FULL")     # 3.44 %/pass
        self.assertEqual(qt.tier_5h(99, 262), "MEDIUM")
        self.assertEqual(qt.tier_5h(5, 300), "LIGHT")     # 0.08 %/pass

    def test_7d_uses_headroom_and_the_5h_bands_would_be_a_constant(self):
        self.assertEqual(qt.tier_7d(96, 0.0763), "MEDIUM")  # headroom 1.039
        self.assertEqual(qt.tier_7d(55, 0.197), "LIGHT")
        # 0.99/(1-0.20) = 1.2375 -> MEDIUM. I first wrote FULL here and the suite
        # caught it, which is the point of asserting the computed value.
        self.assertEqual(qt.tier_7d(99, 0.20), "MEDIUM")
    def test_7d_band_edges(self):
        self.assertEqual(qt.tier_7d(99, 0.20), "MEDIUM")
        self.assertEqual(qt.tier_7d(90, 0.40), "FULL")      # headroom 1.5 exactly

    def test_a_window_at_or_past_its_reset_is_FULL(self):
        self.assertEqual(qt.tier_5h(50, 0), "FULL")      # window just reset
        self.assertEqual(qt.tier_7d(50, 1.0), "FULL")

    def test_zero_remaining_is_MINIMAL_on_either_window(self):
        self.assertEqual(qt.tier_5h(0, 100), "MINIMAL")
        self.assertEqual(qt.tier_7d(0, 0.5), "MINIMAL")


class Parsing(unittest.TestCase):
    TEXT = ("Status: allowed\n5h window: 3% used, 97% remaining\n  Resets: 03:10 Sep 01\n"
            "7d window: 4% used, 96% remaining\n  Resets: 12:00 Sep 07\n")

    def test_parse_reads_both_windows(self):
        q = qt.parse(self.TEXT)
        self.assertEqual((q["used5"], q["rem5"], q["used7"], q["rem7"]), (3, 97, 4, 96))

    def test_a_missing_window_raises_rather_than_defaulting(self):
        with self.assertRaises(ValueError):
            qt.parse("5h window: 3% used, 97% remaining\n")


class ResetParsing(unittest.TestCase):
    NOW = __import__("datetime").datetime(2026, 9, 1, 1, 0)

    def test_parses_a_reset_later_today(self):
        self.assertEqual(qt.parse_reset("03:10 Sep 01", self.NOW, 6).hour, 3)

    def test_rolls_the_year_for_a_dec_to_jan_reset(self):
        import datetime as dt
        got = qt.parse_reset("00:30 Jan 02", dt.datetime(2026, 12, 31, 23, 0), 8*24)
        self.assertEqual((got.year, got.month, got.day), (2027, 1, 2))

    def test_refuses_a_reset_outside_the_window_horizon(self):
        # THE guard: a 5h window whose "reset" is months away is a mis-parse, not a fact.
        with self.assertRaises(ValueError):
            qt.parse_reset("03:10 Dec 25", self.NOW, 6)

    def test_refuses_an_unparseable_string_rather_than_defaulting(self):
        with self.assertRaises(ValueError):
            qt.parse_reset("sometime soon", self.NOW, 6)

    def test_an_impossible_calendar_date_is_refused_in_both_candidate_years(self):
        with self.assertRaises(ValueError):
            qt.parse_reset("00:30 Feb 30", self.NOW, 8 * 24)


class ResetFallback(unittest.TestCase):
    """Without --reset5/--reset7 the printed `Resets:` lines are parsed, bounds-checked."""

    def _run(self, text, argv=()):
        out = io.StringIO()
        real = sys.stdin
        sys.stdin = io.StringIO(text)
        try:
            with redirect_stdout(out), __import__("contextlib").redirect_stderr(out):
                rc = qt.main(list(argv))
        finally:
            sys.stdin = real
        return rc, out.getvalue()

    def test_printed_resets_within_horizon_are_used(self):
        import datetime
        now = datetime.datetime.now()
        r5 = (now + datetime.timedelta(hours=4)).strftime("%H:%M %b %d")
        r7 = (now + datetime.timedelta(days=6)).strftime("%H:%M %b %d")
        text = (f"5h window: 10% used, 90% remaining\n  Resets: {r5}\n"
                f"7d window: 20% used, 80% remaining\n  Resets: {r7}\n")
        rc, out = self._run(text)
        self.assertEqual(rc, 0, out)
        self.assertIn("TIER", out)

    def test_missing_reset_lines_are_cannot_answer_not_guessed(self):
        text = "5h window: 10% used, 90% remaining\n7d window: 20% used, 80% remaining\n"
        rc, out = self._run(text)
        self.assertEqual(rc, 2)
        self.assertIn("cannot resolve reset times", out)


class FreshWindowZeroBurn(unittest.TestCase):
    """A just-reset 7d window has used7 == 0, so `burn` is 0 and then divides.

    `burn` guards its OWN denominator (`el`) and one line later BECOMES a
    denominator with no guard. Measured live 2026-09-01: 100%/100% remaining
    crashed the helper with ZeroDivisionError AFTER both windows had already
    tiered MEDIUM — so the loop lost the tier it is forbidden to hand-select,
    precisely when the budget was at its most permissive.
    """

    def _run(self, used7):
        import datetime
        now = datetime.datetime.now()
        r5 = (now + datetime.timedelta(hours=4)).isoformat(timespec="seconds")
        r7 = (now + datetime.timedelta(days=6)).isoformat(timespec="seconds")
        text = (f"5h window: 0% used, 100% remaining\n"
                f"7d window: {used7}% used, {100-used7}% remaining\n")
        out = io.StringIO()
        real = sys.stdin
        sys.stdin = io.StringIO(text)
        try:
            with redirect_stdout(out):
                rc = qt.main(["--reset5", r5, "--reset7", r7])
        finally:
            sys.stdin = real
        return rc, out.getvalue()

    def test_zero_usage_does_not_crash_and_says_so(self):
        rc, text = self._run(0)
        self.assertEqual(rc, 0, text)
        self.assertIn("TIER", text)
        # The ratio is unbounded, not a number: SAY so rather than print a
        # fabricated figure or omit the line.
        self.assertRegex(text, r"sustainable .*(unbounded|no usage).*even pace",
                         f"zero-burn must report the ratio in words; got: {text!r}")

    def test_nonzero_burn_still_prints_a_ratio(self):
        # Control: the fix must not swallow the normal case.
        rc, text = self._run(20)
        self.assertEqual(rc, 0, text)
        self.assertRegex(text, r"sustainable \d+\.\d+x CURRENT pace")

if __name__ == "__main__":
    unittest.main(verbosity=1)


