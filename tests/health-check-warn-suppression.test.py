#!/usr/bin/env python3
"""Pin `health.suppress_alerts`: a host can quiet a chronic WARN, never a failure.

The only suppression before this was a hardcoded per-probe `"alerting": False`,
used once in the whole file, so an operator had no supported way to say "this
warn does not apply to this machine" without editing upstream code -- and a
local edit there is wiped by the next engine update.

Run: python3 tests/health-check-warn-suppression.test.py
Exit code: 0 on pass, 1 on fail.
"""
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("hc", ROOT / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(hc)
except SystemExit:
    pass


class WarnSuppression(unittest.TestCase):
    def _with_config(self, payload):
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "sutando.config.local.json").write_text(json.dumps(payload))
        orig_root, orig_cache = hc.REPO_DIR, hc._WARN_SUPPRESS_CACHE
        hc.REPO_DIR, hc._WARN_SUPPRESS_CACHE = d, None
        self.addCleanup(lambda: setattr(hc, "REPO_DIR", orig_root))
        self.addCleanup(lambda: setattr(hc, "_WARN_SUPPRESS_CACHE", orig_cache))
        return d

    def test_explicit_alerting_false_still_suppresses(self):
        """The pre-existing mechanism must keep working, config or not."""
        self._with_config({})
        self.assertTrue(hc._alerts_suppressed(
            {"name": "x", "status": "warn", "alerting": False}))

    def test_listed_warn_is_suppressed(self):
        self._with_config({"health_check": {"suppress_alerts": ["sutando-app"]}})
        self.assertTrue(hc._alerts_suppressed({"name": "sutando-app", "status": "warn"}))

    def test_unlisted_warn_still_alerts(self):
        self._with_config({"health_check": {"suppress_alerts": ["sutando-app"]}})
        self.assertFalse(hc._alerts_suppressed({"name": "slack-bridge", "status": "warn"}))

    def test_a_listed_FAILURE_still_alerts(self):
        """The load-bearing one: a list must never be able to hide an outage."""
        self._with_config({"health_check": {"suppress_alerts": ["slack-bridge"]}})
        for status in ("fail", "down", "missing", "stale", "not_loaded"):
            with self.subTest(status=status):
                self.assertFalse(hc._alerts_suppressed(
                    {"name": "slack-bridge", "status": status}))

    def test_malformed_config_fails_open(self):
        """An unreadable or wrong-typed config alerts; it never silences."""
        for payload in ({"health_check": {"suppress_alerts": "sutando-app"}},
                        {"health_check": "nonsense"},
                        {"health_check": {"suppress_alerts": [None, 5]}}):
            with self.subTest(payload=payload):
                self._with_config(payload)
                self.assertFalse(hc._alerts_suppressed(
                    {"name": "sutando-app", "status": "warn"}))

    def test_absent_config_changes_nothing(self):
        self._with_config({})
        self.assertFalse(hc._alerts_suppressed({"name": "sutando-app", "status": "warn"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
