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
import os
import contextlib
import unittest
import unittest.mock
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
        # The probe reads this core's model from the HOST, so an uncontrolled
        # default makes every arm pass or fail by which machine runs it.
        os.environ.pop("SUTANDO_CORE_MODEL", None)
        pins = unittest.mock.patch.object(self.hc, "_settings_model_pins", lambda *a, **k: [])
        pins.start()
        self.addCleanup(pins.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, ledger):
        self.q.write_text(json.dumps({"available": True, "headers": {
            "anthropic-ratelimit-unified-status": "allowed"}, "recent_rejections": ledger}))

    def _rej(self, age, status=429, snippet="You're out of usage credits. Run /usage-credits", model="claude-fable-5-1"):
        return {"ts": _iso(age), "status": status, "path": "/v1/messages", "snippet": snippet, "model": model}

    @contextlib.contextmanager
    def _own(self, model):
        """Declare (or un-declare) this core's model for the duration.

        Both sources must be controlled: leaving the settings fallback live
        would read the HOST's own pin, so the "unknown model" arm would pass
        on a machine with no pin and fail on the developer's.
        """
        env = {"SUTANDO_CORE_MODEL": model} if model else {}
        with unittest.mock.patch.dict("os.environ", env, clear=False):
            if not model:
                os.environ.pop("SUTANDO_CORE_MODEL", None)
            with unittest.mock.patch.object(
                self.hc, "_settings_model_pins",
                lambda *a, **k: [("test", model)] if model else []
            ):
                yield

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

    def test_shared_proxy_other_seats_rejections_do_not_count_when_own_model_known(self):
        # Five rejections from another seat's model inside the hour; one fresh from mine.
        self._write([self._rej(60 * k, model="claude-opus-5") for k in range(1, 6)] + [self._rej(30, model="claude-fable-5-1")])
        with self._own("claude-fable-5-1"):
            c = self.hc.check_core_request_rejections()
        self.assertEqual(c["status"], "warn", c)
        self.assertIn("counting model=claude-fable-5-1", c["detail"])
        self.assertIn("5 from other client(s) [claude-opus-5] not counted", c["detail"])

    def test_only_other_seats_rejected_is_ok_for_this_core(self):
        self._write([self._rej(60 * k, model="claude-opus-5") for k in range(1, 6)])
        with self._own("claude-fable-5-1"):
            c = self.hc.check_core_request_rejections()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("none for this core's model", c["detail"])

    def test_unknown_own_model_counts_every_client_and_says_so(self):
        self._write([self._rej(60 * k, model="claude-opus-5") for k in range(1, 6)])
        with self._own(None):
            c = self.hc.check_core_request_rejections()
        self.assertEqual(c["status"], "fail", c)
        self.assertIn("unattributed", c["detail"])

    def test_control_attribution_is_what_flips_the_verdict(self):
        ledger = [self._rej(60 * k, model="claude-opus-5") for k in range(1, 6)]
        self._write(ledger)
        with self._own("claude-fable-5-1"):
            self.assertEqual(self.hc.check_core_request_rejections()["status"], "ok")
        with self._own("claude-opus-5"):
            self.assertEqual(self.hc.check_core_request_rejections()["status"], "fail")

    def test_probe_is_registered_next_to_core_quota(self):
        src = (REPO / "src" / "health-check.py").read_text()
        i = src.index("checks.append(check_core_quota_exhausted())")
        j = src.index("checks.append(check_core_request_rejections())")
        self.assertLess(i, j)
        self.assertLess(j - i, 400, "registered right after the core-quota probe")


    def test_a_model_stamped_in_core_runtime_json_is_ignored(self):
        """The launch marker records runtime/session, not model. A model there
        would be a launch-time copy of a value the supervisor can change
        mid-session (#3739), so reading it would attribute confidently and
        wrongly. This is the control for that: the file says fable, the probe
        must still report unattributed."""
        marker = self.hc.status_read_path("core-runtime.json", self.root)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(
            {"runtime": "claude", "session": "sutando-core", "model": "claude-fable-5-1"}))
        self._write([self._rej(120, model="claude-fable-5-1")])
        c = self.hc.check_core_request_rejections()
        self.assertIn("unattributed", c["detail"], c)
        self.assertNotIn("counting model=", c["detail"], c)

    def test_a_single_settings_pin_declares_the_model(self):
        with unittest.mock.patch.object(
            self.hc, "_settings_model_pins", lambda *a, **k: [("user", "claude-opus-5")]
        ):
            self._write([self._rej(120, model="claude-opus-5")])
            c = self.hc.check_core_request_rejections()
        self.assertEqual(c["status"], "warn", c)
        self.assertIn("counting model=claude-opus-5", c["detail"], c)

    def test_two_settings_pins_that_disagree_are_not_a_claim(self):
        """Two files naming different models is not a model this core can claim;
        guessing one would attribute every rejection to a coin flip."""
        with unittest.mock.patch.object(
            self.hc, "_settings_model_pins",
            lambda *a, **k: [("user", "claude-opus-5"), ("project", "claude-fable-5-1")]
        ):
            self._write([self._rej(120, model="claude-opus-5")])
            c = self.hc.check_core_request_rejections()
        self.assertIn("unattributed", c["detail"], c)


if __name__ == "__main__":
    unittest.main(verbosity=1)
