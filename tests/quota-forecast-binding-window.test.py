#!/usr/bin/env python3
"""Tests for read-quota's binding-window forecast.

The forecast answers "how many passes until the loop is stopped". Before this,
it answered a narrower question — "until the 5h window is exhausted" — and
printed the answer under the broader label. Two ways that misleads, one test
class each:

  * the 7d window can be the scarcer pool, and was not projected at all;
  * a 5h projection longer than the time to the 5h reset is unreachable,
    because the window refills first.

Running these against the pre-fix script is a weak control and should not be
quoted as a strong one: `_update_burn_rate` gained three parameters, so every
case dies on `TypeError` before an assertion runs. That proves coupling to the
signature, not that a logic regression would be caught.

The real control is mutation, at the new signature — each mutation must fail the
cases that guard it, on a VALUE, and leave the rest untouched:

  remove the reset clamp in `_window_horizon`
      -> 3 failures, all AssertionError, all in the two clamp classes
  never forecast 7d (`if False` on its horizon branch)
      -> 3 failures, all AssertionError, all in TestSevenDayCanBind

`seed()` asserts the result is non-None precisely so those land on values rather
than on a downstream `NoneType` TypeError. A control that dies on a type error
proves less than one that dies on a value.

Module loading mirrors tests/quota-burn-rate.test.py.

Run: python3 tests/quota-forecast-binding-window.test.py
Exit: 0 on pass, 1 on fail.

Python 3.9 compatible (CI floor).
"""
from __future__ import annotations
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
_SCRIPT = REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py"

HOUR = 3600.0
PASS_S = 300.0


def _load_module(workspace: Path):
    """Load read-quota.py with a controlled workspace and a dummy quota-state.json."""
    os.environ["SUTANDO_WORKSPACE"] = str(workspace)
    os.environ["SUTANDO_TEST_MODE"] = "1"  # v0.8: opt-in env-honor
    sys.modules.pop("read_quota_binding_under_test", None)

    state_dir = workspace / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    quota_file = state_dir / "quota-state.json"
    if not quota_file.exists():
        quota_file.write_text(json.dumps({"headers": {
            "anthropic-ratelimit-unified-status": "allowed",
            "anthropic-ratelimit-unified-5h-utilization": "0.1",
            "anthropic-ratelimit-unified-7d-utilization": "0.7",
        }}))

    spec = importlib.util.spec_from_file_location(
        "read_quota_binding_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO / "src"))
    spec.loader.exec_module(mod)
    return mod


class BindingWindowBase(unittest.TestCase):
    # Far enough out that neither window refills first; overridden per class.
    RESET_5H_S = 4 * HOUR
    RESET_7D_S = 90 * HOUR

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quota-binding-test-"))
        self.mod = _load_module(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed(self, samples):
        """Feed (util_5h, util_7d) pairs one pass apart, back-dating history.

        Three samples satisfy the >= 2 sample gate for both windows. The result
        is asserted non-None here so that a regression which drops the forecast
        entirely fails on the assertion below rather than on a downstream
        `NoneType` TypeError — a control that dies on a type error proves less
        than one that dies on a value.
        """
        result = None
        for i, (u5, u7) in enumerate(samples):
            if i:
                h = json.loads(self.mod.BURN_HISTORY_FILE.read_text())
                h["last_read_ts"] = time.time() - PASS_S
                self.mod.BURN_HISTORY_FILE.write_text(json.dumps(h))
            result = self.mod._update_burn_rate(
                u5, u7,
                time.time() + self.RESET_5H_S,
                time.time() + self.RESET_7D_S,
            )
        self.assertIsNotNone(result, "no forecast produced at all")
        return result


class TestSevenDayCanBind(BindingWindowBase):
    """The 7d window is projected, and wins when it is the scarcer pool."""

    SAMPLES = [(0.04, 0.92), (0.05, 0.93), (0.06, 0.94)]

    def test_7d_is_named_as_the_binding_window(self):
        r = self.seed(self.SAMPLES)
        self.assertEqual(r["binding_window"], "7d")

    def test_reported_horizon_is_the_smaller_of_the_two(self):
        r = self.seed(self.SAMPLES)
        self.assertIsNotNone(r["estimated_passes_left"], "no horizon reported")
        five_h_only = ((1 - 0.06) * 100) / r["burn_rate_pct_per_pass"]
        self.assertLess(r["estimated_passes_left"], five_h_only)
        self.assertLessEqual(r["estimated_passes_left"], 7.0)

    def test_7d_burn_rate_is_reported_separately(self):
        r = self.seed(self.SAMPLES)
        self.assertIn("burn_rate_7d_pct_per_pass", r)
        self.assertGreater(r["burn_rate_7d_pct_per_pass"], 0)

    def test_a_7d_sample_survives_a_5h_reset(self):
        # util_5h drops (window reset) while util_7d keeps climbing. The 5h
        # delta is skipped; the 7d reading must still be folded in.
        r = self.seed([(0.80, 0.90), (0.90, 0.91), (0.02, 0.92)])
        self.assertIn("burn_rate_7d_pct_per_pass", r)


class TestPastResetSuppressesTheForecast(BindingWindowBase):
    """A reset already in the past means the state file is stale.

    Found by Sutando-Pro on a node whose `quota-state.json` had not been
    refreshed for 56.8 h (the core was not routed through the credential proxy,
    sonichi/sutando#2417). Both reset epochs were then in the past, and the
    pre-fix script forecast ~9,836 passes — 34 days — from three-day-old data.

    The clamp suppresses this for free: `passes_until_reset` floors at 0.0 for a
    past reset, so no projection can be less than it and every window returns
    None. Pinned here because it is a second, independently-reachable defect
    that this guard closes, and nothing else in the suite covers it.
    """

    RESET_5H_S = -3 * HOUR
    RESET_7D_S = -56 * HOUR

    def test_no_window_forecasts_from_a_past_reset(self):
        r = self.seed([(0.09, 0.71), (0.10, 0.72), (0.11, 0.73)])
        self.assertIsNone(r["binding_window"])
        self.assertIsNone(r["estimated_passes_left"])
        self.assertIsNone(r["estimated_minutes_left"])


class TestForecastCannotOutrunItsOwnReset(BindingWindowBase):
    """A window that refills before it empties does not constrain anything."""

    RESET_5H_S = 10 * PASS_S      # refills in 10 passes
    # 7d held flat so it contributes no rate — this class is about the 5h clamp.
    SAMPLES = [(0.04, 0.10), (0.05, 0.10), (0.06, 0.10)]

    def test_unreachable_5h_projection_does_not_bind(self):
        # ~1pp/pass with 94% left projects ~94 passes, but the window refills
        # in 10 — so the 5h window is not what will stop the loop.
        r = self.seed(self.SAMPLES)
        self.assertNotEqual(r["binding_window"], "5h")

    def test_no_binding_window_reports_none_rather_than_a_number(self):
        r = self.seed(self.SAMPLES)
        self.assertIsNone(r["binding_window"])
        self.assertIsNone(r["estimated_passes_left"])
        self.assertIsNone(r["estimated_minutes_left"])


class TestObservedReading(BindingWindowBase):
    """The 2026-08-05 reading that motivated the change.

    5h 89% remaining resetting in 212 min; 7d 27% remaining resetting in 5702.
    The old code printed 615 minutes left — 403 past the 5h reset. Whatever is
    reported now may not exceed the time until the window it came from resets.
    """

    RESET_5H_S = 212 * 60
    RESET_7D_S = 5702 * 60

    def test_horizon_never_exceeds_its_own_windows_reset(self):
        r = self.seed([(0.09, 0.71), (0.10, 0.72), (0.11, 0.73)])
        cap = {"5h": 212, "7d": 5702}.get(r["binding_window"])
        if cap is not None:
            self.assertLessEqual(r["estimated_minutes_left"], cap)


class TestV1HistoryWarmUp(BindingWindowBase):
    """An expected window with no history of its own is INCOMPLETE, not clear.

    Blocking review finding (qingyun-wu, at `c326df8a`). Every pre-existing v1
    history already satisfies the 5h sample gate and carries no `burn_samples_7d`
    at all, so for the first reads after the upgrade the 7d window is
    expected-but-unforecast. Folding that into `binding_window: null` made the
    human path print "no window runs out before its own reset" over a 7d window
    sitting at 95% — an all-clear on a window nobody measured, which is the exact
    failure this whole change removes, one layer up.
    """

    def v1_history(self):
        """A real pre-upgrade history: warm 5h EWMA, no 7d fields whatsoever."""
        self.mod.BURN_HISTORY_FILE.write_text(json.dumps({
            "last_read_ts": time.time() - PASS_S,
            "last_util_5h": 0.09,
            "schema_version": 1,
            "burn_rate_5h_ewma": 0.0037,
            "burn_samples": 99,
        }))

    def test_7d_without_history_is_reported_unforecast(self):
        self.v1_history()
        r = self.mod._update_burn_rate(
            0.10, 0.95, time.time() + self.RESET_5H_S, time.time() + self.RESET_7D_S)
        self.assertEqual(r["unforecast_windows"], ["7d"])

    def test_incomplete_is_not_reported_as_a_binding_number(self):
        self.v1_history()
        r = self.mod._update_burn_rate(
            0.10, 0.95, time.time() + self.RESET_5H_S, time.time() + self.RESET_7D_S)
        self.assertIsNone(r["binding_window"])
        self.assertIsNone(r["estimated_passes_left"])

    def test_a_7d_stream_that_never_matures_stays_unforecast(self):
        # One 7d sample is not two. Omission must never age into safety.
        self.v1_history()
        r = self.mod._update_burn_rate(
            0.10, 0.95, time.time() + self.RESET_5H_S, time.time() + self.RESET_7D_S)
        h = json.loads(self.mod.BURN_HISTORY_FILE.read_text())
        h["last_read_ts"] = time.time() - PASS_S
        self.mod.BURN_HISTORY_FILE.write_text(json.dumps(h))
        r = self.mod._update_burn_rate(
            0.11, 0.96, time.time() + self.RESET_5H_S, time.time() + self.RESET_7D_S)
        self.assertEqual(r["unforecast_windows"], ["7d"])

    def test_human_output_does_not_state_an_all_clear(self):
        # End-to-end through the real script: the printed line is what a human
        # acts on, and it is the thing that was wrong.
        self.v1_history()
        (self.tmp / "state" / "quota-state.json").write_text(json.dumps({"headers": {
            "anthropic-ratelimit-unified-status": "allowed",
            "anthropic-ratelimit-unified-5h-utilization": "0.10",
            "anthropic-ratelimit-unified-7d-utilization": "0.95",
        }}))
        # Routed: unrouted, the forecast this case asserts on is suppressed as
        # traffic belonging to another session.
        env = dict(os.environ, SUTANDO_WORKSPACE=str(self.tmp), SUTANDO_TEST_MODE="1",
                   ANTHROPIC_BASE_URL="http://localhost:7846")
        out = subprocess.run([sys.executable, str(_SCRIPT)], env=env,
                             capture_output=True, text=True).stdout
        self.assertNotIn("no window runs out", out)
        self.assertIn("INCOMPLETE", out)
        self.assertIn("7d", out.split("Est. passes left:")[-1])


class TestHumanOutputBranches(BindingWindowBase):
    """All three `Est. passes left:` renderings, in-process.

    The subprocess test above is the honest end-to-end proof — it asserts on
    exactly the bytes a human reads — but a subprocess is invisible to the
    parent's coverage run, so `main()`'s print branches counted as untested and
    CI's diff-coverage gate failed at 88.1% (missing 288-299). Both forms earn
    their place: the subprocess one proves the real path, these prove each
    branch. Deleting either would leave a gap the other does not cover.
    """

    def render(self, burn):
        """Run main()'s output path with a stubbed burn result, capture stdout."""
        (self.tmp / "state" / "quota-state.json").write_text(json.dumps({"headers": {
            "anthropic-ratelimit-unified-status": "allowed",
            "anthropic-ratelimit-unified-5h-utilization": "0.10",
            "anthropic-ratelimit-unified-7d-utilization": "0.73",
        }}))
        buf = io.StringIO()
        # Routed: the forecast is only rendered for a session that goes through
        # the proxy; unrouted it is suppressed as another session's traffic.
        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "http://localhost:7846"}), \
                patch.object(self.mod, "_update_burn_rate", return_value=burn), \
                patch.object(sys, "argv", ["read-quota.py"]), \
                contextlib.redirect_stdout(buf):
            self.mod.main()
        return buf.getvalue()

    BASE = {"burn_rate_pct_per_pass": 0.5, "burn_samples": 99}

    def test_binding_window_renders_the_number(self):
        out = self.render(dict(self.BASE, binding_window="7d", estimated_passes_left=19.3,
                               estimated_minutes_left=96, unforecast_windows=[]))
        self.assertIn("19.3", out)
        self.assertIn("7d window binds", out)
        self.assertNotIn("INCOMPLETE", out)

    def test_binding_window_still_carries_the_caveat(self):
        # The second instance of the bug: a number is only the minimum over the
        # windows measured, so it may not be stated bare while one is unforecast.
        out = self.render(dict(self.BASE, binding_window="5h", estimated_passes_left=161.0,
                               estimated_minutes_left=805, unforecast_windows=["7d"]))
        self.assertIn("161.0", out)
        self.assertIn("INCOMPLETE", out)
        self.assertIn("7d", out)

    def test_incomplete_with_no_binding_window(self):
        out = self.render(dict(self.BASE, binding_window=None, estimated_passes_left=None,
                               estimated_minutes_left=None, unforecast_windows=["7d"]))
        self.assertIn("INCOMPLETE", out)
        self.assertNotIn("no window runs out", out)

    def test_all_measured_and_nothing_binds_is_the_only_all_clear(self):
        out = self.render(dict(self.BASE, binding_window=None, estimated_passes_left=None,
                               estimated_minutes_left=None, unforecast_windows=[]))
        self.assertIn("no window runs out before its own reset", out)
        self.assertNotIn("INCOMPLETE", out)


class TestBackCompat(BindingWindowBase):
    """The pre-existing single-argument call keeps working."""

    def test_a_5h_only_caller_does_not_expect_7d(self):
        # No 7d utilization supplied -> 7d is not an expected window, so it
        # cannot be "unforecast". Otherwise every legacy caller reads INCOMPLETE
        # forever over a window it never asked about.
        r = None
        for i, u5 in enumerate((0.10, 0.20, 0.30)):
            if i:
                h = json.loads(self.mod.BURN_HISTORY_FILE.read_text())
                h["last_read_ts"] = time.time() - PASS_S
                self.mod.BURN_HISTORY_FILE.write_text(json.dumps(h))
            r = self.mod._update_burn_rate(u5)
        self.assertEqual(r["unforecast_windows"], [])

    def test_5h_only_call_still_forecasts(self):
        r = None
        for i, u5 in enumerate((0.10, 0.20, 0.30)):
            if i:
                h = json.loads(self.mod.BURN_HISTORY_FILE.read_text())
                h["last_read_ts"] = time.time() - PASS_S
                self.mod.BURN_HISTORY_FILE.write_text(json.dumps(h))
            r = self.mod._update_burn_rate(u5)
        self.assertEqual(r["binding_window"], "5h")
        self.assertGreater(r["estimated_passes_left"], 0)
        self.assertEqual(r["estimated_minutes_left"],
                         round(r["estimated_passes_left"] * 5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
