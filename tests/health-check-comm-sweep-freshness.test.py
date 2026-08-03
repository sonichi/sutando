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
import json
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

    def _schedule_comm_sweep(self, entry: dict | None = None) -> None:
        """Wire a comm-sweep cron into THIS fake host's crons.json."""
        host = self.hc._host_label()
        d = self.ws / "hosts" / host
        d.mkdir(parents=True, exist_ok=True)
        (d / "crons.json").write_text(json.dumps([
            {"name": "main-loop", "cron": "*/3 * * * *", "prompt_skill": "proactive-loop"},
            entry if entry is not None
            else {"name": "comm-sweep", "cron": "26 * * * *", "prompt_skill": "comm-sweep"},
        ]))

    def test_missing_stamp_on_non_owning_host_is_ok_not_a_permanent_warn(self):
        # Comm handling is a SINGLE-OWNER lane: the driver runs on one host by
        # design (a second cron would duplicate sweeps over the owner's comms).
        # A host that does not schedule it has nothing to adopt, so warning here
        # warns forever — and a permanent warn is how a health output gets
        # ignored, taking this probe's real alarms down with it.
        out = self.hc.check_comm_sweep_freshness()
        self.assertEqual(out["name"], "comm-sweep")
        self.assertEqual(out["status"], "ok")
        self.assertIn("N/A", out["detail"])

    def test_missing_stamp_on_the_OWNING_host_still_warns(self):
        # The alarm must survive on the host that actually schedules the driver:
        # cron present + never stamped = wired but not producing. If this went
        # quiet too, the lane-awareness fix would have silenced the whole probe.
        self._schedule_comm_sweep()
        out = self.hc.check_comm_sweep_freshness()
        self.assertEqual(out["status"], "warn")
        self.assertIn("not producing", out["detail"])

    def test_prompt_body_scheduling_counts_as_wired(self):
        # The driver may be scheduled as a prompt body rather than prompt_skill.
        # Keying only on prompt_skill would read the owning host as non-owning
        # and silence its alarm — the exact failure direction to avoid.
        self._schedule_comm_sweep(
            {"name": "sweep", "cron": "26 * * * *",
             "prompt": "Run bash skills/comm-sweep/scripts/comm-sweep.sh collect"}
        )
        self.assertEqual(self.hc.check_comm_sweep_freshness()["status"], "warn")

    def test_stalled_stays_down_even_when_no_cron_is_configured(self):
        # THE DANGEROUS DIRECTION. Lane-gating is applied to the ABSENT branch
        # only. If the age thresholds were gated on config too, deleting the
        # cron entry would silently disarm a real stall — turning a "comm
        # handling stopped" page into silence. A stamp that EXISTS proves the
        # driver ran here, so its staleness is judged unconditionally.
        self._stamp(7.0)
        out = self.hc.check_comm_sweep_freshness()
        self.assertEqual(out["status"], "down")
        self.assertIn("silently stopped", out["detail"])

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
