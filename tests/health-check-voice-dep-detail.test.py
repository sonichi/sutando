#!/usr/bin/env python3
"""A dependent voice probe must carry the dependency's detail, not just its
status word. Run: python3 tests/health-check-voice-dep-detail.test.py"""

import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hc)

STALE = {
    "name": "voice-agent",
    "status": "stale",
    "detail": "running but code is 740 min newer than process — restart needed",
}


class DependentProbeDetail(unittest.TestCase):
    def _both(self, voice_check):
        return {
            "voice-watchers": hc.check_voice_watchers(voice_check),
            "voice-transport": hc.check_voice_transport(voice_check),
        }

    def test_dependency_detail_is_carried_through(self):
        for name, c in self._both(STALE).items():
            with self.subTest(probe=name):
                self.assertEqual(c["status"], "warn")
                self.assertIn("stale", c["detail"])
                self.assertIn("740 min", c["detail"],
                              f"{name}: dropped the dependency's duration")

    def test_missing_dependency_detail_still_names_the_status(self):
        bare = {"name": "voice-agent", "status": "down"}
        for name, c in self._both(bare).items():
            with self.subTest(probe=name):
                self.assertIn("down", c["detail"])

    def test_unknown_status_is_unchanged(self):
        for name, c in self._both({"name": "voice-agent"}).items():
            with self.subTest(probe=name):
                self.assertIn("unknown", c["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
