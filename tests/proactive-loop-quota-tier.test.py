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


class TopTierLane(unittest.TestCase):
    """The API meters a third window, `7d_oi` (top-tier models). A pinned Fable/Opus
    core can exhaust it while 5h and 7d read fine, so it must be able to BIND."""

    def _run(self, text, argv=()):
        out = io.StringIO(); real = sys.stdin; sys.stdin = io.StringIO(text)
        try:
            with redirect_stdout(out), __import__("contextlib").redirect_stderr(out):
                rc = qt.main(list(argv))
        finally:
            sys.stdin = real
        return rc, out.getvalue()

    def _argv(self):
        import datetime
        now = datetime.datetime.now()
        r7 = (now + datetime.timedelta(days=3, hours=12)).isoformat()   # elapsed 0.5
        return ["--reset5", (now + datetime.timedelta(hours=1)).isoformat(), "--reset7", r7, "--reset7oi", r7]

    BASE = "5h window: 1% used, 99% remaining\n7d window: 4% used, 96% remaining\n"

    def test_parse_reads_the_oi_line_and_tolerates_its_absence(self):
        q = qt.parse(self.BASE + "7d-oi window (top-tier models): 80% used, 20% remaining\n")
        self.assertEqual((q["used7oi"], q["rem7oi"]), (80, 20))
        self.assertNotIn("rem7oi", qt.parse(self.BASE))

    def test_an_exhausted_oi_lane_binds_while_5h_and_7d_are_FULL(self):
        # THE discriminating pair: identical 5h/7d, the oi line flips the verdict.
        rc, out = self._run(self.BASE, self._argv())
        self.assertEqual(rc, 0, out); self.assertIn("TIER FULL (bound by both)", out)
        rc, out = self._run(self.BASE + "7d-oi window (top-tier models): 80% used, 20% remaining\n", self._argv())
        self.assertEqual(rc, 0, out)
        self.assertIn("7d-oi (top-tier models) 20% rem", out)
        self.assertIn("TIER LIGHT (bound by 7d-oi)", out)   # headroom 0.2/0.5 = 0.4

    def test_the_oi_lane_is_tiered_against_ITS_OWN_reset_not_the_ordinary_week(self):
        # Hold the oi lane fixed and move ONLY the ordinary 7d reset: its verdict must not
        # change. Then move only the oi reset: it must.
        import datetime
        now = datetime.datetime.now(); iso = lambda d: (now + datetime.timedelta(days=d)).isoformat()
        text = self.BASE + "7d-oi window (top-tier models): 80% used, 20% remaining\n"
        r5 = (now + datetime.timedelta(hours=1)).isoformat()
        far_7d, near_7d = iso(6), iso(1)
        a = self._run(text, ["--reset5", r5, "--reset7", near_7d, "--reset7oi", iso(6)])
        b = self._run(text, ["--reset5", r5, "--reset7", far_7d, "--reset7oi", iso(6)])
        self.assertEqual(a[0], 0, a[1]); self.assertEqual(b[0], 0, b[1])
        oi = lambda out: [l for l in out.splitlines() if l.startswith("7d-oi")][0]
        self.assertEqual(oi(a[1]), oi(b[1]), "moving the ORDINARY reset changed the top-tier line")
        self.assertIn("-> LIGHT", oi(a[1]))                       # 0.2 / (1 - 1/7) = 0.233
        c = self._run(text, ["--reset5", r5, "--reset7", far_7d, "--reset7oi", iso(1)])
        self.assertIn("-> MEDIUM", oi(c[1]))                      # 0.2 / (1 - 6/7) = 1.4

    def test_oi_utilization_without_its_reset_is_a_refusal_not_a_borrowed_week(self):
        import datetime
        now = datetime.datetime.now()
        text = self.BASE + "7d-oi window (top-tier models): 80% used, 20% remaining\n"
        rc, out = self._run(text, ["--reset5", (now + datetime.timedelta(hours=1)).isoformat(),
                                   "--reset7", (now + datetime.timedelta(days=3)).isoformat()])
        self.assertEqual(rc, 2, out); self.assertIn("its reset is missing", out)

    def test_printed_resets_bind_to_their_own_window_lines(self):
        import datetime
        now = datetime.datetime.now(); fmt = lambda d: (now + datetime.timedelta(days=d)).strftime("%H:%M %b %d")
        text = ("5h window: 1% used, 99% remaining\n  Resets: " + (now + datetime.timedelta(hours=1)).strftime("%H:%M %b %d") +
                "\n7d window: 4% used, 96% remaining\n  Resets: " + fmt(6) +
                "\n7d-oi window (top-tier models): 80% used, 20% remaining\n  Resets: " + fmt(1) + "\n")
        q = qt.parse(text)
        self.assertEqual(q["resets_by_window"]["7d-oi"], fmt(1))
        rc, out = self._run(text)
        self.assertEqual(rc, 0, out); self.assertIn("-> MEDIUM", [l for l in out.splitlines() if l.startswith("7d-oi")][0])

    def test_a_healthy_oi_lane_changes_nothing(self):
        rc, out = self._run(self.BASE + "7d-oi window (top-tier models): 5% used, 95% remaining\n", self._argv())
        self.assertEqual(rc, 0, out); self.assertIn("TIER FULL", out)

if __name__ == "__main__":
    unittest.main(verbosity=1)
