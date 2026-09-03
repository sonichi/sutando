#!/usr/bin/env python3
"""`check_core_request_rejections` must surface a proxy-recorded upstream
rejection that `check_core_quota_exhausted` cannot see (#3790): two scheduled
fires dropped with "out of usage credits" while every unified-status header
read "allowed".

  - fresh rejection (< window)       -> warn, names status + snippet + remedy,
                                        survives the _slack_failures filter
  - sustained (>= 5 in the hour)     -> fail
  - only old rejections              -> ok
  - absent file / empty / foreign    -> ok (never pages on nothing)
  - unreadable file                  -> warn (bounded)
  - the probe is registered          -> appears in the assembled check list

Run: python3 tests/health-check-core-request-rejections.test.py
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc_rej_test", REPO / "src" / "health-check.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _iso(age_sec: float) -> str:
    return datetime.fromtimestamp(time.time() - age_sec, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class TestCoreRequestRejections(unittest.TestCase):
    def setUp(self):
        self.hc = _load()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.hc.WORKSPACE_DIR = self.root
        self.q = self.hc.status_read_path("quota-state.json", self.root)
        self.q.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, ledger):
        self.q.write_text(json.dumps({"available": True, "headers": {
            "anthropic-ratelimit-unified-status": "allowed"}, "recent_rejections": ledger}))

    def _rej(self, age, status=429, snippet="You're out of usage credits. Run /usage-credits"):
        return {"ts": _iso(age), "status": status, "path": "/v1/messages", "snippet": snippet}

    def test_fresh_rejection_warns_with_remedy_and_reaches_owner_filter(self):
        self._write([self._rej(120)])
        c = self.hc.check_core_request_rejections()
        self.assertEqual(c["status"], "warn", c)
        self.assertIn("HTTP 429", c["detail"])
        self.assertIn("out of usage credits", c["detail"])
        self.assertIn("/usage-credits", c["detail"])
        self.assertEqual([x["name"] for x in self.hc._slack_failures([c])], ["core-request-rejections"])

    def test_sustained_run_fails(self):
        self._write([self._rej(60 * k) for k in range(1, 6)])
        c = self.hc.check_core_request_rejections()
        self.assertEqual(c["status"], "fail", c)
        self.assertIn("5 upstream rejections", c["detail"])

    def test_four_in_hour_none_in_window_is_ok_but_states_it(self):
        self._write([self._rej(1200 + 60 * k) for k in range(4)])
        c = self.hc.check_core_request_rejections()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("none in the last 15m", c["detail"])

    def test_only_old_rejections_ok(self):
        self._write([self._rej(7200), self._rej(86400)])
        c = self.hc.check_core_request_rejections()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("120m ago", c["detail"])

    def test_control_the_fresh_case_is_what_flips_it(self):
        old = [self._rej(7200)]
        self._write(old)
        self.assertEqual(self.hc.check_core_request_rejections()["status"], "ok")
        self._write(old + [self._rej(30)])
        self.assertEqual(self.hc.check_core_request_rejections()["status"], "warn")

    def test_absent_empty_foreign_never_page(self):
        self.assertEqual(self.hc.check_core_request_rejections()["status"], "ok")
        self._write([])
        self.assertEqual(self.hc.check_core_request_rejections()["status"], "ok")
        self._write("not-a-list")
        self.assertEqual(self.hc.check_core_request_rejections()["status"], "ok")
        self._write([{"ts": 5}, "junk", None, {"status": 429}])
        c = self.hc.check_core_request_rejections()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("none carry a parsable ts", c["detail"])
        self.q.write_text("[1,2]")
        self.assertEqual(self.hc.check_core_request_rejections()["status"], "ok")

    def test_unreadable_is_bounded_warn(self):
        self.q.write_text("{not json")
        c = self.hc.check_core_request_rejections()
        self.assertEqual(c["status"], "warn")
        self.assertIn("unreadable", c["detail"])

    def test_probe_is_registered_next_to_core_quota(self):
        src = (REPO / "src" / "health-check.py").read_text()
        i = src.index("checks.append(check_core_quota_exhausted())")
        j = src.index("checks.append(check_core_request_rejections())")
        self.assertLess(i, j)
        self.assertLess(j - i, 400, "registered right after the core-quota probe")


if __name__ == "__main__":
    unittest.main(verbosity=1)
