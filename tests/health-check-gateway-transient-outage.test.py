#!/usr/bin/env python3
"""A minutes-old last successful poll is a retry, not proof of non-delivery.
Run: python3 tests/health-check-gateway-transient-outage.test.py"""
from __future__ import annotations
import importlib.util
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


HC = _load()
NOW = 1_800_000_000.0


class _Pgrep:
    """Stand-in for the pgrep subprocess: one live bridge instance."""
    returncode = 0
    stdout = "4242\n"


def _probe(age_s, *, last_ok=True):
    """check_gateway_bridge with `connected: false` and a last good poll `age_s` old."""
    with mock.patch.object(HC, "_gateway_configured", return_value=True), \
         mock.patch.object(HC.subprocess, "run", return_value=_Pgrep()), \
         mock.patch.object(HC, "_gateway_serving", return_value=False), \
         mock.patch.object(HC, "_gateway_last_ok_age_h",
                           return_value=(age_s / 3600.0 if last_ok else None)):
        return HC.check_gateway_bridge()


class TestTransientIsNotAnOutage(unittest.TestCase):
    def test_the_line_no_longer_contradicts_itself(self):
        # Measured live 2026-08-16: connected:false while last_ok_age never
        # exceeded 35s across three samples, and the flag returned to True.
        res = _probe(35)
        self.assertEqual(res["status"], "ok", res["detail"])
        self.assertIn("35s ago", res["detail"])
        self.assertNotIn("not being delivered", res["detail"])

    def test_the_bound_is_the_bridge_s_own_grace_not_a_local_guess(self):
        """Review finding: a literal cannot track an env-tuned bridge constant."""
        self.assertEqual(HC.GATEWAY_TRANSIENT_OUTAGE_S, 3 * (25 + 10))
        with mock.patch.dict(os.environ, {"REMOTE_TASK_POLL_WAIT": "50"}):
            reloaded = _load()
        self.assertEqual(reloaded.GATEWAY_TRANSIENT_OUTAGE_S, 3 * (50 + 10))

    def test_past_the_grace_the_bridge_and_the_probe_agree_it_is_an_outage(self):
        # Between the old 300s literal and the bridge's 105s the two disagreed:
        # the bridge called it an outage and this probe still said "transient".
        self.assertEqual(_probe(106)["status"], "warn")
        self.assertEqual(_probe(299)["status"], "warn")

    def test_a_negative_age_is_not_freshness(self):
        """Review finding: a clock step must not read as a 'transient' ok.

        `assertNotIn` alone is satisfiable by an implementation that renders
        nothing at all, so the line is pinned positively too.
        """
        res = _probe(-600)
        self.assertEqual(res["status"], "warn", res["detail"])
        self.assertNotIn("-600s", res["detail"])
        self.assertIn("last successful poll UNKNOWN", res["detail"])

    def test_the_warn_line_reads_in_seconds_below_an_hour(self):
        """`{age_h:.1f}h` rendered a 106s age as "0.0h ago" — the same
        self-refuting shape, surviving on the correct side of the verdict."""
        for age_s in (106, 299, 3599):
            detail = _probe(age_s)["detail"]
            self.assertIn(f"{age_s}s ago", detail)
            self.assertNotIn("0.0h", detail)
        # An hour and over still reads in hours; the boundary belongs to hours.
        self.assertIn("1.0h ago", _probe(3600)["detail"])
        self.assertIn("13.5h ago", _probe(48600)["detail"])
        # UNKNOWN is untouched by the unit change.
        self.assertIn("UNKNOWN", _probe(0, last_ok=False)["detail"])

    def test_a_real_outage_still_warns(self):
        res = _probe(13.5 * 3600)
        self.assertEqual(res["status"], "warn")
        self.assertIn("not being delivered", res["detail"])
        self.assertIn("13.5h ago", res["detail"])

    def test_the_boundary_belongs_to_the_warn_side(self):
        self.assertEqual(_probe(HC.GATEWAY_TRANSIENT_OUTAGE_S)["status"], "warn")
        self.assertEqual(_probe(HC.GATEWAY_TRANSIENT_OUTAGE_S - 1)["status"], "ok")

    def test_an_unknown_last_ok_stays_a_warning(self):
        # Nothing proves it transient, so it must not be downgraded on a guess.
        res = _probe(0, last_ok=False)
        self.assertEqual(res["status"], "warn")
        self.assertIn("UNKNOWN", res["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
