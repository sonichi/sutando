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

    def test_d_glob_raising_oserror_is_swallowed(self):
        """The outer handler. A missing dir does NOT raise from glob() — it
        yields nothing — so the earlier version of this test passed without ever
        reaching the except it was named for."""
        class Boom:
            def glob(self, _pat):
                raise OSError("boom")
        self.m.RESULTS_DIR = Boom()
        self.assertEqual(self.m.undrained_proactive_files(), [])

    def test_d2_stat_raising_oserror_skips_that_file(self):
        """The inner handler: one unstattable entry must not lose the others."""
        good = self._write("proactive-good.txt", self.m.UNDRAINED_AGE_S + 60)

        class Bad:
            name = "proactive-bad.txt"
            def stat(self):
                raise OSError("nope")

        class Dir:
            def glob(self, _pat):
                return [Bad(), good]
        self.m.RESULTS_DIR = Dir()
        self.assertEqual(self.m.undrained_proactive_files(), ["proactive-good.txt"])

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



class TestNotifySummary(unittest.TestCase):
    """The summary line is the claim. It must not assert delivery that failed."""

    def setUp(self):
        import tempfile
        self.m = _load(tempfile.mkdtemp(prefix="cpq-sum-"))

    def test_a_all_paths_healthy(self):
        s, w = self.m.notify_summary(3, True, True, [])
        self.assertIn("macos=ok", s)
        self.assertIn("voice=ok", s)
        self.assertIn("proactive-file=written", s)
        self.assertIsNone(w, "no warning when nothing is undrained")

    def test_b_macos_failure_is_not_reported_as_ok(self):
        s, _ = self.m.notify_summary(3, False, True, [])
        self.assertIn("macos=FAILED", s)
        self.assertNotIn("macos=ok", s)

    def test_c_voice_offline_says_skipped_not_ok(self):
        s, _ = self.m.notify_summary(3, True, False, [])
        self.assertIn("voice=skipped(not connected)", s)

    def test_d_undrained_produces_an_explicit_warning(self):
        s, w = self.m.notify_summary(16, True, False, ["proactive-pending-q-old.txt"])
        self.assertIn("UNDRAINED", s)
        self.assertIsNotNone(w)
        self.assertIn("NOT reaching the owner", w)
        self.assertIn("proactive-pending-q-old.txt", w)

    def test_e_count_is_carried(self):
        s, _ = self.m.notify_summary(16, True, True, [])
        self.assertIn("16 pending questions", s)


class TestDeliver(unittest.TestCase):
    """deliver() must report per-path truth, not a blanket success."""

    def setUp(self):
        import tempfile
        self.m = _load(tempfile.mkdtemp(prefix="cpq-del-"))
        self.m.notify_discord_dm = lambda q: None
        self.m.notify_voice = lambda q: None

    def test_a_voice_connected_reports_ok(self):
        self.m.notify_macos = lambda c, t: True
        self.m.voice_client_connected = lambda: True
        s = self.m.deliver([{"title": "q"}], 1, ["q"])
        self.assertIn("voice=ok", s)

    def test_b_voice_offline_reports_skipped(self):
        self.m.notify_macos = lambda c, t: True
        self.m.voice_client_connected = lambda: False
        s = self.m.deliver([{"title": "q"}], 1, ["q"])
        self.assertIn("voice=skipped", s)

    def test_c_macos_failure_surfaces(self):
        self.m.notify_macos = lambda c, t: False
        self.m.voice_client_connected = lambda: False
        s = self.m.deliver([{"title": "q"}], 1, ["q"])
        self.assertIn("macos=FAILED", s)

    def test_d_undrained_backlog_produces_warning(self):
        self.m.notify_macos = lambda c, t: True
        self.m.voice_client_connected = lambda: False
        self.m.undrained_proactive_files = lambda: ["proactive-old.txt"]
        import io, contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            s = self.m.deliver([{"title": "q"}], 1, ["q"])
        self.assertIn("UNDRAINED", s)
        self.assertIn("NOT reaching the owner", err.getvalue(),
                      "the warning must reach stderr, not just be returned")


if __name__ == "__main__":
    unittest.main(verbosity=2)
