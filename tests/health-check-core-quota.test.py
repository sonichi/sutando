#!/usr/bin/env python3
"""`check_core_quota_exhausted` must FAIL loudly (to the remote owner surface)
when the core's model quota is exhausted — the 'stuck silently' condition.

Owner-reported 2026-08-01: the core ran over quota and every task stalled with
no report. `check_quota_telemetry` only warns on ABSENCE of quota-state.json and
never reads the values, so an exhausted quota read as "ok". This suite pins the
new behavior:

  - fresh + not-available  -> fail, actionable detail, forwarded to the owner DM
  - fresh + available      -> ok
  - stale + not-available  -> ok (fail-safe: don't page on ambiguous old data)
  - absent                 -> ok (absence is quota-telemetry's job)
  - unreadable             -> warn
  - integration            -> a fail flows through notify_slack_for_failures
                              (the core-independent owner DM) and dedups.

Run: python3 tests/health-check-core-quota.test.py
"""
from __future__ import annotations

import importlib.util
import json
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load_health_check():
    spec = importlib.util.spec_from_file_location(
        "health_check_core_quota_test", REPO / "src" / "health-check.py"
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class TestCoreQuotaExhausted(unittest.TestCase):
    def setUp(self):
        self.hc = _load_health_check()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.hc.WORKSPACE_DIR = self.root
        self.qpath = self.hc.status_read_path("quota-state.json", self.root)
        self.qpath.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, *, available, status, age_sec=0, reset=None, util=None, extra=None):
        payload = {"available": available, "headers": {
            "anthropic-ratelimit-unified-status": status,
        }}
        if reset is not None:
            payload["headers"]["anthropic-ratelimit-unified-5h-reset"] = str(reset)
        if util is not None:
            payload["headers"]["anthropic-ratelimit-unified-5h-utilization"] = str(util[0])
            payload["headers"]["anthropic-ratelimit-unified-7d-utilization"] = str(util[1])
        for k, v in (extra or {}).items():
            payload["headers"][k] = v
        self.qpath.write_text(json.dumps(payload))
        if age_sec:
            old = time.time() - age_sec
            import os
            os.utime(self.qpath, (old, old))

    def _write_raw(self, text: str):
        self.qpath.write_text(text)

    # --- the core signal --------------------------------------------------

    def test_fresh_exhausted_fails_with_actionable_detail(self):
        self._write(available=False, status="rejected", reset=int(time.time()) + 3600)
        c = self.hc.check_core_quota_exhausted()
        self.assertEqual(c["status"], "fail", c)
        self.assertIn("OVER QUOTA", c["detail"])
        self.assertIn("/model", c["detail"])  # tells the owner how to recover
        # It must survive the remote-DM filter (this is the whole point).
        self.assertIn("core-quota", [f["name"] for f in self.hc._slack_failures([c])])

    def test_fresh_exhausted_without_reset_header_still_fails(self):
        # No reset header -> _fmt_quota_reset returns "" and the message omits
        # the reset clause, but it must still fail with the actionable core text.
        self._write(available=False, status="rejected")  # reset=None
        c = self.hc.check_core_quota_exhausted()
        self.assertEqual(c["status"], "fail", c)
        self.assertIn("OVER QUOTA", c["detail"])
        self.assertNotIn("window resets", c["detail"])  # no reset-time clause

    def test_fmt_quota_reset_bad_input(self):
        self.assertEqual(self.hc._fmt_quota_reset(None), "")
        self.assertEqual(self.hc._fmt_quota_reset("not-an-epoch"), "")

    def test_available_is_ok(self):
        self._write(available=True, status="allowed")
        c = self.hc.check_core_quota_exhausted()
        self.assertEqual(c["status"], "ok")
        # Control for the staleness case below: a FRESH reading carries no
        # hedge, so the hedge there cannot be an unconditional suffix.
        self.assertNotIn("old", c["detail"])

    def test_stale_available_is_ok_but_says_it_is_stale(self):
        # Status must stay ok — a stale allowed is not a warning, quota-telemetry
        # owns that — but the detail may not state it as current.
        self._write(available=True, status="allowed", age_sec=4000)
        c = self.hc.check_core_quota_exhausted()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("66m old", c["detail"])
        self.assertNotIn("core-quota", [f["name"] for f in self.hc._slack_failures([c])])

    def test_available_age_unreadable_says_currency_unknown(self):
        # Same FakePath shape as test_unreadable_age_does_not_page below: a
        # blanket Path.stat mock breaks status_read_path's own exists() first.
        payload = json.dumps({"available": True, "headers": {
            "anthropic-ratelimit-unified-status": "allowed"}})

        class FakePath:
            def exists(self_):
                return True

            def read_text(self_):
                return payload

            def stat(self_):
                raise OSError("stat failed")

        orig = self.hc.status_read_path
        self.hc.status_read_path = lambda *a, **k: FakePath()
        try:
            c = self.hc.check_core_quota_exhausted()
        finally:
            self.hc.status_read_path = orig
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("currency is unknown", c["detail"])

    def test_status_not_allowed_even_if_available_flag_true(self):
        # Defensive: trust the unified status header, not just the bool.
        self._write(available=True, status="rejected", reset=int(time.time()) + 60)
        self.assertEqual(self.hc.check_core_quota_exhausted()["status"], "fail")

    def test_rejected_with_both_windows_low_is_a_warn_naming_the_shared_proxy(self):
        # Another client's credit gate through the shared proxy, not this core's window.
        self._write(available=False, status="rejected", util=(0.13, 0.52))
        c = self.hc.check_core_quota_exhausted()
        self.assertEqual(c["status"], "warn")
        self.assertIn("shared credential proxy", c["detail"])
        self.assertIn("5h 13%", c["detail"])
        self.assertIn("7d 52%", c["detail"])

    def test_rejected_per_model_window_at_full_still_fails_and_names_it(self):
        # Live shape 2026-09-03: 5h 13% / 7d 52%, but 7d_oi rejected at 100%.
        self._write(available=False, status="rejected", util=(0.13, 0.52), extra={
            "anthropic-ratelimit-unified-7d_oi-utilization": "1.0",
            "anthropic-ratelimit-unified-7d_oi-status": "rejected"})
        c = self.hc.check_core_quota_exhausted()
        self.assertEqual(c["status"], "fail")
        self.assertIn("7d_oi (100%, rejected)", c["detail"])

    def test_unparseable_window_utilization_with_rejected_status_still_fails(self):
        # A rejected window whose utilization does not parse is named as n/a, not crashed on.
        self._write(available=False, status="rejected", util=(0.13, 0.52), extra={
            "anthropic-ratelimit-unified-7d_oi-utilization": "n/a",
            "anthropic-ratelimit-unified-7d_oi-status": "rejected"})
        c = self.hc.check_core_quota_exhausted()
        self.assertEqual(c["status"], "fail")
        self.assertIn("7d_oi (n/a, rejected)", c["detail"])

    def test_per_window_rejected_status_counts_even_below_the_utilization_bar(self):
        self._write(available=False, status="rejected", util=(0.13, 0.52), extra={
            "anthropic-ratelimit-unified-overage-utilization": "0.5",
            "anthropic-ratelimit-unified-overage-status": "rejected"})
        c = self.hc.check_core_quota_exhausted()
        self.assertEqual(c["status"], "fail")
        self.assertIn("overage (50%, rejected)", c["detail"])

    def test_rejected_with_a_window_near_full_still_fails(self):
        self._write(available=False, status="rejected", util=(0.97, 0.52))
        c = self.hc.check_core_quota_exhausted()
        self.assertEqual(c["status"], "fail")
        self.assertIn("OVER QUOTA", c["detail"])

    def test_rejected_without_utilization_headers_still_fails(self):
        # An unknown reading corroborates nothing: the original page stays loud.
        self._write(available=False, status="rejected")
        self.assertEqual(self.hc.check_core_quota_exhausted()["status"], "fail")

    def test_stale_exhausted_does_not_alert(self):
        self._write(available=False, status="rejected", age_sec=4000)
        c = self.hc.check_core_quota_exhausted()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("stale", c["detail"])
        self.assertNotIn("core-quota", [f["name"] for f in self.hc._slack_failures([c])])

    def test_absent_file_is_ok(self):
        self.qpath.unlink(missing_ok=True)
        self.assertEqual(self.hc.check_core_quota_exhausted()["status"], "ok")

    def test_unreadable_is_warn(self):
        self.qpath.write_text("{not json")
        self.assertEqual(self.hc.check_core_quota_exhausted()["status"], "warn")

    # --- fail-safe: ambiguous / corrupt / age-unknown must NOT page --------

    def test_available_true_with_unknown_status_does_not_page(self):
        # A fresh partial response: available:true, status header absent.
        # Ambiguous -> must not raise a false OVER QUOTA page (qingyun P1).
        self._write_raw(json.dumps({"available": True, "headers": {}}))
        c = self.hc.check_core_quota_exhausted()
        self.assertEqual(c["status"], "ok", c)
        self.assertNotIn("core-quota", [f["name"] for f in self.hc._slack_failures([c])])

    def test_missing_available_and_status_does_not_page(self):
        self._write_raw(json.dumps({"headers": {}}))
        self.assertEqual(self.hc.check_core_quota_exhausted()["status"], "ok")

    def test_non_dict_json_is_bounded_warn_not_crash(self):
        # A list or null payload must not AttributeError-crash the health run.
        for raw in ("[]", "null", '"a string"'):
            self._write_raw(raw)
            c = self.hc.check_core_quota_exhausted()
            self.assertEqual(c["status"], "warn", f"{raw!r} -> {c}")

    def test_non_dict_headers_does_not_crash(self):
        # headers as a list must degrade to {} (no .get crash); available:false
        # is still an explicit exhaustion so this fails cleanly, not by raising.
        self._write_raw(json.dumps({"available": False, "headers": []}))
        c = self.hc.check_core_quota_exhausted()
        self.assertEqual(c["status"], "fail", c)

    def test_unreadable_age_does_not_page(self):
        # Explicit exhaustion but the file age can't be read -> fail-safe: no page.
        # Inject a path whose exists()/read_text() work but stat() raises, so the
        # early exists() check still passes and only the age read fails.
        payload = json.dumps({"available": False, "headers": {
            "anthropic-ratelimit-unified-status": "rejected"}})

        class FakePath:
            def exists(self_):
                return True

            def read_text(self_):
                return payload

            def stat(self_):
                raise OSError("stat failed")

        orig = self.hc.status_read_path
        self.hc.status_read_path = lambda *a, **k: FakePath()
        try:
            c = self.hc.check_core_quota_exhausted()
        finally:
            self.hc.status_read_path = orig
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("unreadable", c["detail"])

    # --- delivery: reaches the owner, core-independent, deduped ------------

    def test_fail_reaches_owner_dm_once_per_episode(self):
        self._write(available=False, status="rejected", reset=int(time.time()) + 1800)
        c = self.hc.check_core_quota_exhausted()
        sent = []
        state = self.root / "state" / "last-slacked.json"

        def fake_sender(text):
            sent.append(text)
            return True

        self.hc.notify_slack_for_failures([c], state_file=state, sender=fake_sender)
        self.hc.notify_slack_for_failures([c], state_file=state, sender=fake_sender)
        self.assertEqual(len(sent), 1, "over-quota must DM the owner exactly once per unchanged episode")
        self.assertIn("core-quota", sent[0])

    def test_run_all_checks_wires_core_quota_probe(self):
        sentinel = {
            "name": "core-quota",
            "status": "ok",
            "detail": "integration sentinel",
        }
        with mock.patch.object(
            self.hc,
            "check_core_quota_exhausted",
            return_value=sentinel,
        ) as probe:
            checks = self.hc.run_all_checks()

        probe.assert_called_once_with()
        self.assertIn(sentinel, checks)


if __name__ == "__main__":
    unittest.main(verbosity=2)
