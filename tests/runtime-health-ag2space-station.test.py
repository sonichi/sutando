#!/usr/bin/env python3
"""AG2 Space -> Station availability in src/runtime-health.py.

AG2 Space's engine (`space.ag2.app/engine`) serves the Station connector
gateway, so `station_available` tracks whether AG2 Space is running. Verifies
the detection and that derive() surfaces both fields. Run:
  python3 tests/runtime-health-ag2space-station.test.py
Exit 0 on pass, 1 on fail.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "runtime_health_ag2space_under_test", REPO / "src" / "runtime-health.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAg2SpaceStationAwareness(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def test_running_true_when_engine_present(self):
        seen = []

        def fake_run(cmd):
            seen.append(cmd)
            if "pgrep" in cmd and self.mod._AG2SPACE_ENGINE_MARKER in cmd:
                return 0, "83111\n83113\n"
            return 127, ""
        self.mod._run = fake_run
        self.assertTrue(self.mod._ag2space_running())
        # matched on the ENGINE marker (what serves Station), not the UI process
        self.assertTrue(any(self.mod._AG2SPACE_ENGINE_MARKER in c for c in seen))

    def test_running_false_when_absent(self):
        self.mod._run = lambda cmd: (1, "")
        self.assertFalse(self.mod._ag2space_running())

    def test_running_false_when_pgrep_missing(self):
        # _run yields 127 when the binary is absent; must degrade to False, not raise.
        self.mod._run = lambda cmd: (127, "")
        self.assertFalse(self.mod._ag2space_running())

    def test_derive_station_available_tracks_ag2space_up(self):
        self.mod._run = lambda cmd: (
            (0, "") if ("pgrep" in cmd and self.mod._AG2SPACE_ENGINE_MARKER in cmd) else (127, ""))
        out = self.mod.derive()
        self.assertIn("ag2space_running", out)
        self.assertIn("station_available", out)
        self.assertTrue(out["ag2space_running"])
        self.assertEqual(out["station_available"], out["ag2space_running"])

    def test_derive_station_unavailable_when_ag2space_down(self):
        self.mod._run = lambda cmd: (127, "")
        out = self.mod.derive()
        self.assertFalse(out["ag2space_running"])
        self.assertFalse(out["station_available"])


if __name__ == "__main__":
    res = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if res.wasSuccessful() else 1)
