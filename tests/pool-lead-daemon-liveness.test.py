#!/usr/bin/env python3
"""The lead daemon delegates follower freshness to the pool heartbeat bounds."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))
SPEC = importlib.util.spec_from_file_location(
    "pool_lead_daemon", REPO / "scripts" / "pool-lead-daemon.py")
DAEMON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DAEMON)


class LeadDaemonLivenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cores = Path(self.tmp.name)
        self.now = 10_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def _alive(self, age):
        beat = self.cores / "core-1.alive"
        beat.write_text("{}")
        timestamp = self.now - age
        os.utime(beat, (timestamp, timestamp))
        return DAEMON._heartbeat_alive(
            self.cores, "core-1", now_fn=lambda: self.now)

    def test_small_future_skew_is_alive(self):
        self.assertTrue(self._alive(-0.5))

    def test_future_tolerance_is_bounded(self):
        self.assertFalse(
            self._alive(-DAEMON.HEARTBEAT_FUTURE_TOLERANCE_S - 0.1))

    def test_stale_beat_is_dead(self):
        self.assertFalse(self._alive(DAEMON.LEAD_STALE_S))

    def test_missing_beat_is_dead(self):
        self.assertFalse(DAEMON._heartbeat_alive(
            self.cores, "missing", now_fn=lambda: self.now))


if __name__ == "__main__":
    unittest.main(verbosity=2)
