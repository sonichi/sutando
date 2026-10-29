#!/usr/bin/env python3
"""src/stop_hook_repeat.py: the Stop hook's same-queue repeat counter.

Run: python3 tests/stop-hook-repeat.test.py
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import stop_hook_repeat as m  # noqa: E402


class Counter(unittest.TestCase):
    def setUp(self):
        for k in ("SUTANDO_INSTANCE_ID", "SUTANDO_AGENT_ID", "AGENT_MXID", "AGENT_ID"):
            os.environ.pop(k, None)
        self.td = tempfile.TemporaryDirectory()
        self.state = Path(self.td.name) / "state"

    def tearDown(self):
        self.td.cleanup()

    def test_same_signature_counts_up_and_creates_the_dir(self):
        self.assertEqual(m.bump(self.state, "a.txt b.txt"), 1)
        self.assertEqual(m.bump(self.state, "a.txt b.txt"), 2)
        self.assertEqual(m.bump(self.state, "a.txt b.txt"), 3)
        self.assertTrue(m.counter_path(self.state).is_file())
        self.assertFalse(list(self.state.glob(".*")), "no temp file left behind")

    def test_a_different_signature_starts_over(self):
        m.bump(self.state, "a.txt"); m.bump(self.state, "a.txt")
        self.assertEqual(m.bump(self.state, "a.txt b.txt"), 1)
        self.assertEqual(m.bump(self.state, "a.txt b.txt"), 2)

    def test_clear_restarts_and_is_idempotent(self):
        m.bump(self.state, "x"); m.clear(self.state); m.clear(self.state)
        self.assertFalse(m.counter_path(self.state).exists())
        self.assertEqual(m.bump(self.state, "x"), 1)

    def test_garbage_in_the_file_restarts(self):
        m.counter_path(self.state).parent.mkdir(parents=True)
        m.counter_path(self.state).write_text("not a count\n")
        self.assertEqual(m.bump(self.state, "x"), 1)

    def test_a_signature_is_hashed_not_stored(self):
        m.bump(self.state, "task-secret-name.txt")
        self.assertNotIn("secret", m.counter_path(self.state).read_text())

    def test_a_failed_replace_unlinks_the_temp_file_and_raises(self):
        # The write reached the temp file; the rename into place failed: nothing may be
        # left behind and the failure surfaces (the hook then fails open).
        import unittest.mock as um
        m.bump(self.state, "x")
        with um.patch.object(m.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                m.bump(self.state, "x")
        self.assertFalse(list(self.state.glob(".*")), "temp file left behind")
        self.assertEqual(m.bump(self.state, "x"), 2, "the persisted count survived the failed write")

    def test_bump_raises_when_the_dir_cannot_be_written(self):
        self.state.mkdir(parents=True)
        os.chmod(self.state, 0o500)
        try:
            with self.assertRaises(OSError):
                m.bump(self.state, "x")
            self.assertFalse(list(self.state.glob(".*")))
        finally:
            os.chmod(self.state, 0o700)


class Cli(unittest.TestCase):
    def setUp(self):
        for k in ("SUTANDO_INSTANCE_ID", "SUTANDO_AGENT_ID", "AGENT_MXID", "AGENT_ID"):
            os.environ.pop(k, None)
        self.td = tempfile.TemporaryDirectory()
        self.state = str(Path(self.td.name) / "state")

    def tearDown(self):
        self.td.cleanup()

    def _main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = m.main(list(args))
        return rc, out.getvalue().strip()

    def test_bump_prints_the_count_and_path_and_clear_work(self):
        self.assertEqual(self._main("bump", "--state", self.state, "--sig", "a b"), (0, "1"))
        self.assertEqual(self._main("bump", "--state", self.state, "--sig", "a b"), (0, "2"))
        rc, path = self._main("path", "--state", self.state)
        self.assertEqual(rc, 0); self.assertTrue(Path(path).is_file())
        self.assertEqual(self._main("clear", "--state", self.state), (0, ""))
        self.assertFalse(Path(path).exists())

    def test_usage_errors_are_rc_2(self):
        for args in (("bump", "--state", self.state), ("bump", "--state", self.state, "--nope", "x"),
                     ("clear", "--state", self.state, "extra"), ("what", "--state", self.state), ("bump",)):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(m.main(list(args)), 2, args)

    def test_a_persistence_failure_is_rc_1_with_the_reason_on_stderr(self):
        Path(self.state).mkdir(parents=True); os.chmod(self.state, 0o500)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                rc = m.main(["bump", "--state", self.state, "--sig", "x"])
        finally:
            os.chmod(self.state, 0o700)
        self.assertEqual(rc, 1); self.assertIn("bump failed", err.getvalue())

    def test_runs_as_a_script(self):
        r = subprocess.run([sys.executable, str(REPO / "src" / "stop_hook_repeat.py"), "bump", "--state", self.state, "--sig", "q"],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "1"), r.stderr)


if __name__ == "__main__":
    unittest.main()
