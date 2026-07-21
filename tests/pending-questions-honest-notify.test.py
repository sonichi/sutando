#!/usr/bin/env python3
"""check-pending-questions must not claim delivery it did not achieve.

2026-07-21: the cron printed "Notified: 16 pending questions" on a host where no
bridge was draining results/proactive-*.txt. The DM never reached the owner; only
a local macOS notification did. A notifier that reports success regardless of
outcome is how a blocked decision sits unseen for a day.
"""
import importlib.util
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(results_dir):
    spec = importlib.util.spec_from_file_location("cpq", REPO / "src" / "check-pending-questions.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.RESULTS_DIR = Path(results_dir)
    return m


class TestUndrainedDetection(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = Path(tempfile.mkdtemp(prefix="cpq-"))
        self.m = _load(self.d)

    def _write(self, name, age_s):
        p = self.d / name
        p.write_text("x")
        t = time.time() - age_s
        import os
        os.utime(p, (t, t))
        return p

    def test_a_fresh_file_is_not_undrained(self):
        self._write("proactive-pending-q-abc.txt", 5)
        self.assertEqual(self.m.undrained_proactive_files(), [])

    def test_b_old_file_is_undrained(self):
        self._write("proactive-pending-q-abc.txt", self.m.UNDRAINED_AGE_S + 60)
        self.assertEqual(self.m.undrained_proactive_files(), ["proactive-pending-q-abc.txt"])

    def test_c_only_proactive_files_count(self):
        self._write("task-123.txt", self.m.UNDRAINED_AGE_S + 60)
        self.assertEqual(self.m.undrained_proactive_files(), [],
                         "a stale task result is a different thing entirely")

    def test_d_missing_dir_does_not_raise(self):
        self.m.RESULTS_DIR = Path("/nonexistent/definitely/not/here")
        self.assertEqual(self.m.undrained_proactive_files(), [])

    def test_e_notify_macos_reports_failure(self):
        import subprocess
        real = subprocess.run
        try:
            subprocess.run = lambda *a, **k: type("R", (), {"returncode": 1})()
            self.assertFalse(self.m.notify_macos(1, ["t"]), "a failed osascript must not read as delivered")
            subprocess.run = lambda *a, **k: type("R", (), {"returncode": 0})()
            self.assertTrue(self.m.notify_macos(1, ["t"]))
        finally:
            subprocess.run = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
