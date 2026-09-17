#!/usr/bin/env python3
"""Pins the read predicate in codex-core-launcher's _wait_for_heartbeat_exit.

The heartbeat stub records its pid with `Path(...).write_text(str(os.getpid()))`.
`write_text` opens with mode "w", which CREATES AND TRUNCATES before any bytes are
written, so a reader waiting on `pid_file.exists()` can observe a zero-length file
and raise `ValueError: invalid literal for int() with base 10: ''`.

Observed in CI (run 12df9ee0, job "diff coverage >= 95% (python)"), where the
suite -- not the coverage threshold -- is what failed.

This drives the REAL method off the launcher suite rather than a copied recipe, so
the assertion cannot pass against a surrogate while production keeps the defect.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "tests" / "codex-core-launcher.test.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("_codex_core_launcher", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dead_pid():
    """A pid guaranteed to have exited, so the method's kill-loop returns."""
    p = subprocess.Popen([sys.executable, "-c", ""])
    p.wait()
    return p.pid


class HeartbeatPidReadRaceTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_launcher()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pid_file = Path(self.tmp.name) / "heartbeat.pid"

    def _subject(self):
        """The real _wait_for_heartbeat_exit, bound to a minimal host object."""
        cls = self.mod.CodexCoreLauncherTests
        inst = cls.__new__(cls)
        inst.tmp = self.tmp
        return types.MethodType(cls._wait_for_heartbeat_exit, inst)

    def _write_racily(self, pid, hold_s=0.30):
        """Reproduce write_text()'s create-then-write window deterministically."""
        def run():
            fh = open(self.pid_file, "w")   # file now EXISTS and is EMPTY
            time.sleep(hold_s)
            fh.write(str(pid))
            fh.close()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join)

    # --- the defect, stated as a control -------------------------------------

    def test_existence_predicate_raises_on_the_truncate_window(self):
        """NEGATIVE CONTROL: proves the window is real, reachable, and fatal.

        If this ever stops raising, the reproduction has gone inert and the
        test below would pass for the wrong reason.
        """
        self._write_racily(_dead_pid())
        deadline = time.monotonic() + 5
        while not self.pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.pid_file.exists())
        with self.assertRaises(ValueError):
            int(self.pid_file.read_text())

    # --- the fix --------------------------------------------------------------

    def test_real_method_survives_the_truncate_window(self):
        self._write_racily(_dead_pid())
        self._subject()()          # must not raise ValueError

    def test_real_method_survives_a_late_create(self):
        """The file may not exist at all yet -- FileNotFoundError, not ValueError."""
        pid = _dead_pid()

        def run():
            time.sleep(0.30)
            self.pid_file.write_text(str(pid))
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join)
        self._subject()()

    # --- negative controls: the fix must not over-correct ---------------------

    def test_still_fails_when_the_pid_is_never_written(self):
        """An empty file that never fills must still be a failure, not a hang."""
        self.pid_file.write_text("")
        with self.assertRaises(AssertionError):
            self._subject()()

    def test_still_fails_when_the_pid_never_exits(self):
        """A live pid must still fail -- the method's second half must survive."""
        self.pid_file.write_text(str(os.getpid()))
        with self.assertRaises(AssertionError):
            self._subject()()


if __name__ == "__main__":
    unittest.main(verbosity=2)
