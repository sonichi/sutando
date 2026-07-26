#!/usr/bin/env python3
"""Regression test: check_comm_sweep_freshness must make a stalled owner-comm
sweep LOUD instead of silent (P1 of the comm-handling overhaul).

The gap it covers: comm handling ran as a discipline ("remember to sweep"),
so when its driver died (inbox-score loop, 2026-07-21) the owner-comm sweeps
lapsed for days and NOTHING alerted. The fix is a mechanism: the comm-sweep
driver stamps state/last-comm-sweep.json every run, and this probe pages when
that stamp goes stale (warn >2h, down >6h) or is absent (warn — not wired yet).

Run: python3 tests/health-check-comm-sweep-freshness.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load_health_check():
    spec = importlib.util.spec_from_file_location(
        "health_check_commsweep_test", REPO / "src" / "health-check.py"
    )
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class TestCommSweepFreshness(unittest.TestCase):
    def setUp(self):
        self.hc = _load_health_check()
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        (self.ws / "state").mkdir(parents=True, exist_ok=True)
        self.hc.WORKSPACE_DIR = self.ws

    def tearDown(self):
        self._tmp.cleanup()

    def _stamp(self, age_hours: float) -> Path:
        p = self.ws / "state" / "last-comm-sweep.json"
        p.write_text('{"last_sweep_ts": 0}')
        if age_hours:
            t = time.time() - age_hours * 3600
            os.utime(p, (t, t))
        return p

    def test_missing_stamp_warns_not_down(self):
        out = self.hc.check_comm_sweep_freshness()
        self.assertEqual(out["name"], "comm-sweep")
        self.assertEqual(out["status"], "warn")
        self.assertIn("not wired", out["detail"])

    def test_fresh_is_ok(self):
        self._stamp(0.1)
        self.assertEqual(self.hc.check_comm_sweep_freshness()["status"], "ok")

    def test_lagging_warns(self):
        self._stamp(3.0)
        out = self.hc.check_comm_sweep_freshness()
        self.assertEqual(out["status"], "warn")
        self.assertIn("lagging", out["detail"])

    def test_stalled_is_down(self):
        self._stamp(7.0)
        out = self.hc.check_comm_sweep_freshness()
        self.assertEqual(out["status"], "down")
        self.assertIn("silently stopped", out["detail"])

    def test_stat_failure_warns_not_crashes(self):
        # A stamp that exists() but whose stat() raises (races, permission, a
        # broken mount) must degrade to warn, never propagate an OSError that
        # would crash the whole health check.
        class _StatFails:
            def exists(self):
                return True

            def stat(self):
                raise OSError("simulated stat failure")

        with mock.patch.object(self.hc, "status_read_path", return_value=_StatFails()):
            out = self.hc.check_comm_sweep_freshness()
        self.assertEqual(out["name"], "comm-sweep")
        self.assertEqual(out["status"], "warn")
        self.assertIn("stat failed", out["detail"])

    def test_run_all_checks_emits_comm_sweep(self):
        # Reachability guard (mirrors PR #1898): the probe is useless if it's
        # defined but never wired into run_all_checks(). Exercise the actual
        # call site and assert a "comm-sweep" check is emitted.
        try:
            checks = self.hc.run_all_checks()
        except Exception as e:  # pragma: no cover - failure path
            self.fail(f"run_all_checks() raised: {e!r}")
        names = [c.get("name") for c in checks if isinstance(c, dict)]
        self.assertIn("comm-sweep", names,
                      "run_all_checks() emitted no comm-sweep check (branch unreachable)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
