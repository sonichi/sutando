#!/usr/bin/env python3
"""The pool-advertisement probe compares two files by roster version: a roster
the advertisement does not carry is a pin the broker was never told about.

Run: python3 tests/health-check-pool-advertisement.test.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(hc)
except SystemExit:
    pass


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        (self.ws / "state").mkdir()
        self._old = hc.WORKSPACE_DIR
        hc.WORKSPACE_DIR = self.ws
        self.addCleanup(lambda: setattr(hc, "WORKSPACE_DIR", self._old))

    def roster(self, version, bindings=None):
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": version, "workers": {"w1": {"state": "live"}}, "bindings": bindings or {}}))

    def advertisement(self, roster_version):
        (self.ws / "state" / "pool-advertisement.json").write_text(json.dumps(
            {"ts": 1, "report": {"roster_version": roster_version}, "profile_workers": {},
             "workers": {"ts": 1, "live_cores": ["w1"], "dead_cores": [], "bindings": {},
                         "roster_version": roster_version}}))


class TestProbe(Base):
    def test_no_roster_is_not_a_pool(self):
        c = hc.check_pool_advertisement()
        self.assertEqual((c["name"], c["status"]), ("pool-advertisement", "ok"))

    def test_roster_without_advertisement_warns(self):
        self.roster(3, {"!r:hs": "w1"})
        c = hc.check_pool_advertisement()
        self.assertEqual(c["status"], "warn")
        self.assertIn("no advertisement", c["detail"])
        self.assertIn("v3", c["detail"])

    def test_advertisement_behind_the_roster_warns_as_unpublished(self):
        self.roster(4, {"!r:hs": "w1"})
        self.advertisement(3)
        c = hc.check_pool_advertisement()
        self.assertEqual(c["status"], "warn")
        self.assertIn("binding unpublished", c["detail"])
        self.assertIn("v3", c["detail"])
        self.assertIn("v4", c["detail"])

    def test_matching_versions_are_ok(self):
        self.roster(4, {"!r:hs": "w1"})
        self.advertisement(4)
        c = hc.check_pool_advertisement()
        self.assertEqual(c["status"], "ok")
        self.assertIn("v4", c["detail"])

    def test_unreadable_advertisement_warns(self):
        self.roster(2)
        (self.ws / "state" / "pool-advertisement.json").write_text("{not json")
        c = hc.check_pool_advertisement()
        self.assertEqual(c["status"], "warn")
        self.assertIn("unreadable", c["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
