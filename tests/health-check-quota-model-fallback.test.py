#!/usr/bin/env python3
"""`check_quota_model_fallback` reports the credential proxy's model-fallback
tier from the `fallback` block of quota-state.json:

  - no file / no block        -> ok (absence is quota-telemetry's job)
  - tier 1                    -> ok, names the reason
  - tier 2 or 3, fresh        -> warn, names the model map and the since-stamp
  - runtime_switch to codex   -> fail, reaches the remote owner DM, says the
                                 switch is manual (the proxy only signals)
  - stale record              -> ok: only Claude traffic through the proxy
                                 refreshes it, so an old warn/fail says nothing
  - window reset passed       -> ok: the tier is over whatever the record says
  - unreadable                -> warn
  - registered in the main check list

Run: python3 tests/health-check-quota-model-fallback.test.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_health_check():
    spec = importlib.util.spec_from_file_location(
        "health_check_model_fallback_test", REPO / "src" / "health-check.py"
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class TestQuotaModelFallback(unittest.TestCase):
    def setUp(self):
        self.hc = _load_health_check()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.hc.WORKSPACE_DIR = self.root
        self.qpath = self.hc.status_read_path("quota-state.json", self.root)
        self.qpath.parent.mkdir(parents=True, exist_ok=True)
        self.now = time.time()

    def tearDown(self):
        self._tmp.cleanup()

    def _windows(self, tier, reset_in_sec=3600):
        """Both windows at `tier`, resetting `reset_in_sec` from now (negative = already passed)."""
        return {w: {"tier": tier, "reset": str(int(self.now + reset_in_sec))} for w in ("5h", "7d")}

    def _write(self, fallback=None, age_sec=0, **top):
        payload = {"available": True, "last_checked": _iso(self.now - age_sec),
                   "headers": {"anthropic-ratelimit-unified-status": "allowed"}, **top}
        if fallback is not None:
            payload["fallback"] = fallback
        self.qpath.write_text(json.dumps(payload))

    def _tier(self, tier, **extra):
        fb = {"tier": tier, "low_priority_tier": tier, "since": "2026-10-09T01:02:03Z",
              "active_model_map": {"fable": "claude-opus-5-5", "mythos": "claude-opus-5-5"} if tier == 2
              else {"fable": "claude-sonnet-5", "opus": "claude-sonnet-5"} if tier == 3 else {},
              "reason": "7d window 86% > level1 threshold 85%" if tier > 1 else "primary (5h 20%, 7d 50%)",
              "windows": self._windows(tier)}
        fb.update(extra)
        return fb

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
        self._write(fallback=self._tier(1))
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("tier 1", c["detail"])
        self.assertIn("7d 50%", c["detail"])

    def test_fresh_tier_2_warns_and_names_the_model_map(self):
        self._write(fallback=self._tier(2))
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "warn", c)
        self.assertIn("tier 2", c["detail"])
        self.assertIn("fable→claude-opus-5-5", c["detail"])
        self.assertIn("2026-10-09T01:02:03Z", c["detail"])
        self.assertIn("7d window 86%", c["detail"])

    def test_fresh_tier_3_warns(self):
        self._write(fallback=self._tier(3))
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "warn", c)
        self.assertIn("opus→claude-sonnet-5", c["detail"])

    def test_stale_tier_record_does_not_warn(self):
        # An idle night: the last Claude response was 8h ago; the tier it set says nothing about now.
        self._write(fallback=self._tier(2), age_sec=8 * 3600)
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("480m old", c["detail"])
        self.assertIn("stale", c["detail"])
        self.assertNotIn("quota-model-fallback", [f["name"] for f in self.hc._slack_failures([c])])

    def test_unparsable_last_checked_counts_as_stale(self):
        self._write(fallback=self._tier(2), last_checked="not a time")
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("unknown age", c["detail"])

    def test_tier_whose_window_reset_has_passed_is_cleared(self):
        fb = self._tier(2)
        fb["windows"] = {"5h": {"tier": 1, "reset": str(int(self.now + 3600))},
                         "7d": {"tier": 2, "reset": str(int(self.now - 120))}}
        self._write(fallback=fb)  # fresh file, but the 7d window that set tier 2 reset 2 minutes ago
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("reset at", c["detail"])
        self.assertIn("cleared", c["detail"])

    def test_tier_still_warns_while_one_of_its_windows_has_not_reset(self):
        fb = self._tier(3)
        fb["windows"] = {"5h": {"tier": 3, "reset": str(int(self.now - 60))},
                         "7d": {"tier": 3, "reset": str(int(self.now + 86400))}}
        self._write(fallback=fb)
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "warn", c)

    def test_low_priority_only_downgrade_stays_ok_but_is_named(self):
        self._write(fallback=self._tier(1, low_priority_tier=2, reason="primary (5h 10%, 7d 61%)"))
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("low-priority traffic at tier 2", c["detail"])

    def test_fresh_codex_switch_request_fails_and_reaches_the_owner_dm(self):
        self._write(fallback=self._tier(1, runtime_switch={"to": "codex", "reason": "rejected", "at": "2026-10-09T02:00:00Z"}))
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "fail", c)
        self.assertIn("rejected", c["detail"])
        self.assertIn("Codex", c["detail"])
        self.assertIn("2026-10-09T02:00:00Z", c["detail"])
        # It must say the switch is NOT automatic, and how to do it.
        self.assertIn("manually", c["detail"])
        self.assertIn("start-cli.sh --restart", c["detail"])
        self.assertIn("quota-model-fallback", [f["name"] for f in self.hc._slack_failures([c])])

    def test_stale_codex_request_stops_failing(self):
        # After the owner switched to Codex no Claude traffic refreshes the record: it must not stay red.
        self._write(fallback=self._tier(1, runtime_switch={"to": "codex", "reason": "rejected", "at": "x"}), age_sec=2 * 3600)
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("not alerting", c["detail"])

    def test_codex_request_is_cleared_once_the_5h_window_reset_has_passed(self):
        fb = self._tier(1, runtime_switch={"to": "codex", "reason": "rejected", "at": "x"})
        fb["windows"]["5h"]["reset"] = str(int(self.now - 30))
        self._write(fallback=fb)
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "ok", c)
        self.assertIn("has passed since", c["detail"])

    def test_withdrawn_switch_is_ok(self):
        self._write(fallback=self._tier(1, runtime_switch={"to": "claude", "reason": "window reset", "at": "y"}))
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

    def test_malformed_windows_do_not_crash_the_probe(self):
        self._write(fallback=self._tier(2, windows={"5h": "bad", "7d": {"tier": 2, "reset": "soon"}}))
        c = self.hc.check_quota_model_fallback()
        self.assertEqual(c["status"], "warn", c)

    def test_registered_in_the_main_check_list(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertRegex(src, re.compile(r"^\s*checks\.append\(check_quota_model_fallback\(\)\)", re.M))


if __name__ == "__main__":
    unittest.main()
