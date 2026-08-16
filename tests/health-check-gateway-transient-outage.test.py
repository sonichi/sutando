#!/usr/bin/env python3
"""A minutes-old last successful poll is a retry, not proof of non-delivery.
Run: python3 tests/health-check-gateway-transient-outage.test.py"""
from __future__ import annotations
import importlib.util
import json
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
        # Measured live 2026-08-16: connected:false with a 148s-old last success,
        # and the next poll succeeded 39s later.
        res = _probe(148)
        self.assertEqual(res["status"], "ok", res["detail"])
        self.assertIn("148s ago", res["detail"])
        self.assertNotIn("not being delivered", res["detail"])

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
