#!/usr/bin/env python3
"""`check_quota_model_fallback` reports the credential proxy's model-fallback
tier from the `fallback` block of quota-state.json:

  - no file / no block        -> ok (absence is quota-telemetry's job)
  - tier 1                    -> ok, names the reason
  - tier 2 or 3               -> warn, names the model map and the since-stamp
  - runtime_switch to codex   -> fail, reaches the remote owner DM, says the
                                 switch is manual (the proxy only signals)
  - unreadable                -> warn
  - registered in the main check list

Run: python3 tests/health-check-quota-model-fallback.test.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_health_check():
    spec = importlib.util.spec_from_file_location(
        "health_check_model_fallback_test", REPO / "src" / "health-check.py"
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class TestQuotaModelFallback(unittest.TestCase):
    def setUp(self):
        self.hc = _load_health_check()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.hc.WORKSPACE_DIR = self.root
        self.qpath = self.hc.status_read_path("quota-state.json", self.root)
        self.qpath.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, fallback=None, **top):
        payload = {"available": True, "headers": {"anthropic-ratelimit-unified-status": "allowed"}, **top}
        if fallback is not None:
            payload["fallback"] = fallback
        self.qpath.write_text(json.dumps(payload))

    def test_absent_file_is_ok(self):
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("no quota-state.json", c["detail"])

    def test_no_fallback_block_is_ok(self):
        self._write()
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("primary", c["detail"])

    def test_tier_1_is_ok_with_reason(self):
        self._write(fallback={"tier": 1, "low_priority_tier": 1, "active_model_map": {},
                              "reason": "primary (5h 20%, 7d 50%)", "since": "2026-10-09T00:00:00Z"})
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("tier 1", c["detail"])
        self.assertIn("7d 50%", c["detail"])

    def test_tier_2_warns_and_names_the_model_map(self):
        self._write(fallback={"tier": 2, "low_priority_tier": 2,
                              "active_model_map": {"fable": "claude-opus-5-5", "mythos": "claude-opus-5-5"},
                              "reason": "7d window 86% > level1 threshold 85%", "since": "2026-10-09T01:02:03Z"})
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "warn", c)
        self.assertIn("tier 2", c["detail"])
        self.assertIn("fable→claude-opus-5-5", c["detail"])
        self.assertIn("2026-10-09T01:02:03Z", c["detail"])
        self.assertIn("7d window 86%", c["detail"])

    def test_tier_3_warns(self):
        self._write(fallback={"tier": 3, "low_priority_tier": 3,
                              "active_model_map": {"fable": "claude-sonnet-5", "opus": "claude-sonnet-5"},
                              "reason": "5h window 98% > level2 threshold 97%", "since": "x"})
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "warn", c)
        self.assertIn("opus→claude-sonnet-5", c["detail"])

    def test_low_priority_only_downgrade_stays_ok_but_is_named(self):
        self._write(fallback={"tier": 1, "low_priority_tier": 2, "active_model_map": {},
                              "reason": "primary (5h 10%, 7d 61%)", "since": "x"})
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("low-priority traffic at tier 2", c["detail"])

    def test_codex_switch_request_fails_and_reaches_the_owner_dm(self):
        self._write(fallback={"tier": 1, "low_priority_tier": 1, "active_model_map": {}, "reason": "primary",
                              "since": "x", "runtime_switch": {"to": "codex", "reason": "rejected", "at": "2026-10-09T02:00:00Z"}})
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "fail", c)
        self.assertIn("rejected", c["detail"])
        self.assertIn("Codex", c["detail"])
        self.assertIn("2026-10-09T02:00:00Z", c["detail"])
        # It must say the switch is NOT automatic, and how to do it.
        self.assertIn("manually", c["detail"])
        self.assertIn("start-cli.sh --restart", c["detail"])
        self.assertIn("quota-model-fallback", [f["name"] for f in self.hc._slack_failures([c])])

    def test_withdrawn_switch_is_ok(self):
        self._write(fallback={"tier": 1, "low_priority_tier": 1, "active_model_map": {}, "reason": "primary (5h 5%, 7d 40%)",
                              "since": "x", "runtime_switch": {"to": "claude", "reason": "window reset", "at": "y"}})
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)

    def test_unreadable_is_warn_and_non_object_fallback_is_ok(self):
        self.qpath.write_text("{not json")
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "warn", c)
        self._write(fallback="tier 2")
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)
        self.qpath.write_text("[1, 2]")
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)

    def test_registered_in_the_main_check_list(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertRegex(src, re.compile(r"^\s*checks\.append\(check_quota_model_fallback\(\)\)", re.M))


if __name__ == "__main__":
    unittest.main()
