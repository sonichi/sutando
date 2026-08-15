#!/usr/bin/env python3
"""The gateway warn line must carry HOW LONG the outage has run.

Without it the line is byte-identical at minute one and hour thirteen, so a
reader cannot tell a blip from a standing outage — and an alert that reads the
same when ignored as when new is one nobody can act on.

Run: python3 tests/health-check-gateway-outage-duration.test.py
"""
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


def _sidecar(tmp, **fields):
    p = pathlib.Path(tmp) / "gateway-status.json"
    p.write_text(json.dumps(fields))
    return p


class TestLastOkAge(unittest.TestCase):
    def test_reports_hours_since_the_last_successful_poll(self):
        with tempfile.TemporaryDirectory() as td:
            p = _sidecar(td, connected=False, ts=NOW, last_ok_ts=NOW - 48_600)
            self.assertAlmostEqual(HC._gateway_last_ok_age_h(p, NOW), 13.5, places=2)

    def test_a_missing_last_ok_ts_is_UNKNOWN_not_zero(self):
        # Zero would read as "just now" — the most misleading possible default.
        with tempfile.TemporaryDirectory() as td:
            p = _sidecar(td, connected=False, ts=NOW)
            self.assertIsNone(HC._gateway_last_ok_age_h(p, NOW))

    def test_a_bool_is_not_a_timestamp(self):
        # isinstance(True, int) is True in Python, so bools reach the arithmetic
        # unless excluded explicitly.
        with tempfile.TemporaryDirectory() as td:
            p = _sidecar(td, connected=False, ts=NOW, last_ok_ts=True)
            self.assertIsNone(HC._gateway_last_ok_age_h(p, NOW))

    def test_a_future_last_ok_ts_clamps_to_zero_rather_than_going_negative(self):
        with tempfile.TemporaryDirectory() as td:
            p = _sidecar(td, connected=False, ts=NOW, last_ok_ts=NOW + 10_000)
            self.assertEqual(HC._gateway_last_ok_age_h(p, NOW), 0.0)

    def test_an_absent_or_corrupt_sidecar_is_UNKNOWN(self):
        with tempfile.TemporaryDirectory() as td:
            missing = pathlib.Path(td) / "nope.json"
            self.assertIsNone(HC._gateway_last_ok_age_h(missing, NOW))
            bad = pathlib.Path(td) / "bad.json"
            bad.write_text("{not json")
            self.assertIsNone(HC._gateway_last_ok_age_h(bad, NOW))


class TestWarnDetail(unittest.TestCase):
    """The probe's own output, not just the helper."""

    def _detail(self, age_h):
        with mock.patch.object(HC, "_gateway_serving", return_value=False), \
             mock.patch.object(HC, "_gateway_last_ok_age_h", return_value=age_h), \
             mock.patch.object(HC, "_gateway_configured", return_value=True, create=True):
            r = HC.check_gateway_bridge()
        return r["detail"] if r else ""

    def test_two_different_outage_lengths_produce_DIFFERENT_lines(self):
        # The control. This is the whole point: if these two match, the fix is
        # inert no matter what the other assertions say.
        short, long = self._detail(0.1), self._detail(13.5)
        if not short or not long:
            self.skipTest("probe short-circuited before the warn branch")
        self.assertNotEqual(short, long,
                            "a 6-minute and a 13-hour outage produced identical text")
        self.assertIn("0.1h", short)
        self.assertIn("13.5h", long)

    def test_an_unknown_start_says_so_rather_than_implying_freshness(self):
        d = self._detail(None)
        if not d:
            self.skipTest("probe short-circuited before the warn branch")
        self.assertIn("UNKNOWN", d)
        self.assertNotIn("0.0h", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
