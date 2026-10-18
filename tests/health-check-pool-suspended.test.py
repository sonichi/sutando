#!/usr/bin/env python3
"""The pool-suspended probe warns only when a suspension outlives the host's stop:
the marker is still present while a core is running, so no worker is being healed.

Run: python3 tests/health-check-pool-suspended.test.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(hc)
except SystemExit:
    pass


class Probe(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        (self.ws / "state").mkdir()
        self._old = hc.WORKSPACE_DIR
        hc.WORKSPACE_DIR = self.ws
        self.addCleanup(lambda: setattr(hc, "WORKSPACE_DIR", self._old))
        self.marker = self.ws / "state" / "pool-suspended"

    def check(self, core_alive):
        with mock.patch.object(hc, "_any_core_alive", return_value=core_alive):
            return hc.check_pool_suspended()

    def test_no_marker_is_ok(self):
        self.assertEqual(self.check(True)["status"], "ok")

    def test_a_marker_while_no_core_runs_is_the_expected_quit(self):
        self.marker.write_text(json.dumps({"reason": "app-quit", "at": 5, "stopped": []}))
        c = self.check(False)
        self.assertEqual(c["status"], "ok")
        self.assertIn("app-quit since 5", c["detail"])

    def test_a_marker_while_a_core_runs_warns_with_the_repair(self):
        self.marker.write_text(json.dumps({"reason": "app-quit", "at": 5, "stopped": []}))
        c = self.check(True)
        self.assertEqual(c["status"], "warn")
        self.assertIn("repair: resume the worker pool", c["detail"])

    def test_a_hand_written_marker_is_quoted(self):
        self.marker.write_text("app-quit 7\n")
        self.assertIn("app-quit 7", self.check(True)["detail"])

    def test_an_unreadable_marker_warns(self):
        self.marker.mkdir()
        self.assertEqual(self.check(True)["status"], "warn")

    def test_the_probe_is_registered(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_pool_suspended())", src)


if __name__ == "__main__":
    sys.exit(unittest.main())
