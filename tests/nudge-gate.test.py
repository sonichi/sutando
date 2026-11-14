#!/usr/bin/env python3
"""Contract for src/delivery/nudge_gate.py — the supervisor's nudge/alert/arm decision.

Run: python3 tests/nudge-gate.test.py
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from delivery import nudge_gate as ng  # noqa: E402


class DecideTest(unittest.TestCase):
    def test_idle_ready_and_live_nudges(self):
        self.assertEqual(ng.decide("idle-ready", "live"), "nudge")

    def test_idle_ready_but_stale_alerts(self):
        # An idle-looking frame over a dead agent: injection is futile, surface it.
        self.assertEqual(ng.decide("idle-ready", "stale"), "alert")

    def test_idle_ready_but_absent_alerts(self):
        self.assertEqual(ng.decide("idle-ready", "absent"), "alert")

    def test_idle_ready_but_unknown_health_arms(self):
        # Uncertainty about liveness must not withhold coverage.
        self.assertEqual(ng.decide("idle-ready", "unknown"), "arm")

    def test_pending_composer_arms_even_when_live(self):
        # A dirty composer is the existing arm path; the notifier's own gate carries it.
        self.assertEqual(ng.decide("pending", "live"), "arm")

    def test_busy_arms(self):
        # A working turn needs no help; the supervisor's next poll re-evaluates.
        self.assertEqual(ng.decide("busy", "live"), "arm")

    def test_abnormal_arms(self):
        self.assertEqual(ng.decide("abnormal", "live"), "arm")

    def test_unknown_pane_arms(self):
        # A failed capture is unknown, never idle; fail toward existing coverage.
        self.assertEqual(ng.decide("unknown", "live"), "arm")

    def test_every_non_idle_pane_arms_regardless_of_health(self):
        for pane in ("busy", "pending", "abnormal", "unknown"):
            for health in ("live", "stale", "absent", "unknown"):
                self.assertEqual(ng.decide(pane, health), "arm", (pane, health))


class CliHealthDirectTest(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ng._cli(argv)
        return rc, buf.getvalue().strip()

    def test_direct_health_flag(self):
        rc, out = self._run(["--pane-state", "idle-ready", "--health", "live"])
        self.assertEqual((rc, out), (0, "nudge"))

    def test_direct_health_alert(self):
        rc, out = self._run(["--pane-state", "idle-ready", "--health", "absent"])
        self.assertEqual((rc, out), (0, "alert"))


class CliBeatPathTest(unittest.TestCase):
    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ng._cli(argv)
        return rc, buf.getvalue().strip()

    def test_fresh_beat_is_live_and_nudges(self):
        with tempfile.TemporaryDirectory() as d:
            beat = Path(d) / "core.alive"
            beat.write_text("{}")
            rc, out = self._run(["--pane-state", "idle-ready", "--beat-path", str(beat)])
            self.assertEqual((rc, out), (0, "nudge"))

    def test_old_beat_is_stale_and_alerts(self):
        with tempfile.TemporaryDirectory() as d:
            beat = Path(d) / "core.alive"
            beat.write_text("{}")
            old = time.time() - 3600
            os.utime(beat, (old, old))
            rc, out = self._run(["--pane-state", "idle-ready", "--beat-path", str(beat)])
            self.assertEqual((rc, out), (0, "alert"))

    def test_missing_beat_is_absent_and_alerts(self):
        with tempfile.TemporaryDirectory() as d:
            beat = Path(d) / "nope.alive"
            rc, out = self._run(["--pane-state", "idle-ready", "--beat-path", str(beat)])
            self.assertEqual((rc, out), (0, "alert"))

    def test_future_beat_is_stale_not_live(self):
        # A clock-skewed future beat must not read as fresh (matches pool_beat).
        with tempfile.TemporaryDirectory() as d:
            beat = Path(d) / "core.alive"
            beat.write_text("{}")
            future = time.time() + 3600
            os.utime(beat, (future, future))
            rc, out = self._run(["--pane-state", "idle-ready", "--beat-path", str(beat)])
            self.assertEqual((rc, out), (0, "alert"))

    def test_missing_beat_but_busy_pane_still_arms(self):
        with tempfile.TemporaryDirectory() as d:
            beat = Path(d) / "nope.alive"
            rc, out = self._run(["--pane-state", "busy", "--beat-path", str(beat)])
            self.assertEqual((rc, out), (0, "arm"))

    def test_stat_error_other_than_missing_is_unknown_and_arms(self):
        # An OSError other than FileNotFoundError must fall through to
        # "unknown" (arms), never be swallowed as absent.
        with tempfile.TemporaryDirectory() as d:
            beat = Path(d) / "core.alive"
            beat.write_text("{}")
            with patch("os.stat", side_effect=PermissionError("denied")):
                rc, out = self._run(["--pane-state", "idle-ready", "--beat-path", str(beat)])
            self.assertEqual((rc, out), (0, "arm"))


class CliInvocationTest(unittest.TestCase):
    def test_runs_as_a_script(self):
        proc = subprocess.run(
            [sys.executable, str(REPO / "src" / "delivery" / "nudge_gate.py"),
             "--pane-state", "idle-ready", "--health", "live"],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "nudge")


if __name__ == "__main__":
    unittest.main(verbosity=2)
