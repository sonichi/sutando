#!/usr/bin/env python3
"""Regression test: check_quota_telemetry must surface a proxy that is up but
producing no quota state.

The gap it covers: quota-state.json is written by the credential proxy from
upstream response headers, so it only appears if a core actually ROUTES
through the proxy. src/startup.sh is the only thing exporting
ANTHROPIC_BASE_URL=http://localhost:7846, and a supervisor-launched core
never runs startup.sh. On such a host the proxy is healthy and listening,
every check is green, and quota telemetry is silently absent forever — the
proactive loop's budget check reads "unknown" every pass with no explanation.

The pre-existing credential-proxy check cannot catch this: it is a plain
TCP-listening probe (correct for a forwarding proxy with no liveness
endpoint), so "listening" is the most it can ever assert.

Run: python3 tests/health-check-quota-telemetry.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_health_check():
    spec = importlib.util.spec_from_file_location(
        "health_check_quota_test", REPO / "src" / "health-check.py"
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class TestQuotaTelemetryCheck(unittest.TestCase):
    def setUp(self):
        self.hc = _load_health_check()
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "state").mkdir(parents=True, exist_ok=True)
        self.hc.WORKSPACE_DIR = self.ws

    def tearDown(self):
        self._tmp.cleanup()

    def _write_quota(self, mtime_age_sec: float = 0.0) -> Path:
        p = self.ws / "state" / "quota-state.json"
        p.write_text('{"remaining_pct": 42}')
        if mtime_age_sec:
            past = time.time() - mtime_age_sec
            os.utime(p, (past, past))
        return p

    def test_proxy_up_but_no_quota_state_warns(self):
        """The actual bug: green everywhere, telemetry silently dead."""
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "warn")
        self.assertIn("never written quota-state.json", r["detail"])
        # The detail must name the cause, not just the symptom — otherwise the
        # reader has no idea why an up proxy produces nothing.
        self.assertIn("ANTHROPIC_BASE_URL", r["detail"])

    def test_proxy_down_stays_silent(self):
        """Not every host routes through the proxy, and its own check already
        reports it as down. Warning twice would be noise."""
        for status in ("warn", "down"):
            r = self.hc.check_quota_telemetry(status)
            self.assertEqual(r["status"], "ok", f"status={status}")
            self.assertIn("not expected", r["detail"])

    def test_quota_state_present_is_ok_with_age(self):
        self._write_quota()
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok")
        self.assertIn("present", r["detail"])

    def test_old_quota_state_does_not_warn(self):
        """Deliberate: a quiet core legitimately writes nothing for a long
        time. An age threshold would fire on healthy idle hosts, so absence —
        not staleness — is the signal. Pin it so nobody 'improves' this into
        a flaky check later."""
        self._write_quota(mtime_age_sec=60 * 60 * 24 * 3)
        r = self.hc.check_quota_telemetry("ok")
        self.assertEqual(r["status"], "ok")
        self.assertIn("4320m ago", r["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
