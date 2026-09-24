#!/usr/bin/env python3
"""`src/quota_record.py`: the proxy's quota record read as one fact.

The contract a delivery gate depends on: `provider_allows_now` is True ONLY for
a fresh record that says allowed. Every other shape -- absent, unreadable,
malformed, stale, rejected on any window, or silent -- is False, because a
stale limit banner may be overridden by the provider's word and never by the
absence of one.

Run: python3 tests/quota-record.test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import quota_record as qr  # noqa: E402

NOW = 1_790_000_000.0


def _iso(epoch: float, millis: bool = True) -> str:
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z" if millis else dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _headers(status="allowed", h5="allowed", h7="allowed") -> dict:
    return {
        "anthropic-ratelimit-unified-status": status,
        "anthropic-ratelimit-unified-5h-status": h5,
        "anthropic-ratelimit-unified-7d-status": h7,
        "anthropic-ratelimit-unified-7d-reset": "1790499600",
    }


class RecordFixture(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name) / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.path = self.ws / "state" / "quota-state.json"

    def write(self, data) -> None:
        self.path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


class TestReadQuotaRecord(RecordFixture):
    def test_a_fresh_allowed_record_reads_allowed_and_fresh(self):
        self.write({"available": True, "last_checked": _iso(NOW - 30), "headers": _headers()})
        rec = qr.read_quota_record(self.ws, now=NOW)
        self.assertIs(rec.allowed, True)
        self.assertAlmostEqual(rec.age_s, 30, delta=0.01)
        self.assertTrue(rec.fresh())

    def test_any_window_rejected_reads_not_allowed(self):
        # The unified status can say allowed while the 7d window is the one that bit.
        for kw in ({"status": "rejected"}, {"h5": "rejected"}, {"h7": "rejected"}):
            self.write({"last_checked": _iso(NOW - 30), "headers": _headers(**kw)})
            self.assertIs(qr.read_quota_record(self.ws, now=NOW).allowed, False, kw)

    def test_headers_outrank_the_available_flag(self):
        self.write({"available": True, "last_checked": _iso(NOW - 30), "headers": _headers(h7="rejected")})
        self.assertIs(qr.read_quota_record(self.ws, now=NOW).allowed, False)

    def test_without_headers_the_available_flag_is_the_answer(self):
        self.write({"available": False, "last_checked": _iso(NOW - 30)})
        self.assertIs(qr.read_quota_record(self.ws, now=NOW).allowed, False)
        self.write({"available": True, "last_checked": _iso(NOW - 30)})
        self.assertIs(qr.read_quota_record(self.ws, now=NOW).allowed, True)

    def test_a_record_that_says_nothing_reads_allowed_none(self):
        self.write({"last_checked": _iso(NOW - 30), "headers": {"x-other": "1"}})
        self.assertIsNone(qr.read_quota_record(self.ws, now=NOW).allowed)

    def test_last_checked_is_read_in_both_shapes_the_proxy_writes(self):
        self.write({"headers": _headers(), "last_checked": _iso(NOW - 45, millis=True)})
        self.assertAlmostEqual(qr.read_quota_record(self.ws, now=NOW).age_s, 45, delta=0.01)
        self.write({"headers": _headers(), "last_checked": _iso(NOW - 45, millis=False)})
        self.assertAlmostEqual(qr.read_quota_record(self.ws, now=NOW).age_s, 45, delta=0.01)

    def test_no_timestamp_falls_back_to_the_file_mtime(self):
        self.write({"headers": _headers()})
        os.utime(self.path, (NOW - 100, NOW - 100))
        self.assertAlmostEqual(qr.read_quota_record(self.ws, now=NOW).age_s, 100, delta=0.01)

    def test_an_unparseable_timestamp_falls_back_to_the_file_mtime(self):
        self.write({"headers": _headers(), "last_checked": "yesterday-ish"})
        os.utime(self.path, (NOW - 100, NOW - 100))
        self.assertAlmostEqual(qr.read_quota_record(self.ws, now=NOW).age_s, 100, delta=0.01)

    def test_absent_and_malformed_records_read_as_none_not_as_an_error(self):
        self.assertIsNone(qr.read_quota_record(self.ws, now=NOW))
        self.write("{not json")
        self.assertIsNone(qr.read_quota_record(self.ws, now=NOW))
        self.write("[1, 2, 3]")
        self.assertIsNone(qr.read_quota_record(self.ws, now=NOW))

    def test_a_record_dated_in_the_future_is_not_fresh(self):
        # A clock skew that puts the observation ahead of now vouches for nothing.
        self.write({"headers": _headers(), "last_checked": _iso(NOW + 300)})
        self.assertFalse(qr.read_quota_record(self.ws, now=NOW).fresh())


class TestProviderAllowsNow(RecordFixture):
    def test_true_only_for_fresh_and_allowed(self):
        self.write({"last_checked": _iso(NOW - 30), "headers": _headers()})
        self.assertTrue(qr.provider_allows_now(self.ws, now=NOW))

    def test_exactly_at_the_freshness_bound_still_counts(self):
        self.write({"last_checked": _iso(NOW - qr.FRESH_SEC), "headers": _headers()})
        self.assertTrue(qr.provider_allows_now(self.ws, now=NOW))
        self.write({"last_checked": _iso(NOW - qr.FRESH_SEC - 1), "headers": _headers()})
        self.assertFalse(qr.provider_allows_now(self.ws, now=NOW))

    def test_false_for_every_other_shape(self):
        cases = {
            "stale": {"last_checked": _iso(NOW - 3600), "headers": _headers()},
            "rejected": {"last_checked": _iso(NOW - 30), "headers": _headers(h7="rejected")},
            "silent": {"last_checked": _iso(NOW - 30), "headers": {}},
            "no-timestamp-old-file": {"headers": _headers()},
        }
        for name, data in cases.items():
            self.write(data)
            if name == "no-timestamp-old-file":
                os.utime(self.path, (NOW - 3600, NOW - 3600))
            self.assertFalse(qr.provider_allows_now(self.ws, now=NOW), name)
        self.path.unlink()
        self.assertFalse(qr.provider_allows_now(self.ws, now=NOW), "absent")
        self.write("{broken")
        self.assertFalse(qr.provider_allows_now(self.ws, now=NOW), "malformed")

    def test_the_freshness_window_is_a_parameter(self):
        self.write({"last_checked": _iso(NOW - 3000), "headers": _headers()})
        self.assertFalse(qr.provider_allows_now(self.ws, now=NOW))
        self.assertTrue(qr.provider_allows_now(self.ws, now=NOW, fresh_sec=3600))

    def test_default_freshness_is_minutes_not_hours(self):
        # health-check's six-hour horizon asks "is the proxy wired"; this asks "is
        # the limit lifted NOW", and a limit can begin at any moment in between.
        self.assertLessEqual(qr.FRESH_SEC, 15 * 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
