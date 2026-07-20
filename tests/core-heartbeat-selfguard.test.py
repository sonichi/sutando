"""core_heartbeat self-guard: double-starts resolve to exactly one writer.

The guard yields ONLY to a fresh .alive naming a live pid that isn't us;
missing/stale/malformed files and dead pids all mean take over. Consumers
that use the .alive pid as a control target (pause/stop-core, #2198) depend
on this determinism; the schedule-crons step-5.5 backstop (#2199) is the
double-start source it defuses.

Run: python3 tests/core-heartbeat-selfguard.test.py
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("core_heartbeat", REPO / "src" / "core_heartbeat.py")
hb = importlib.util.module_from_spec(spec)
sys.modules["core_heartbeat"] = hb
spec.loader.exec_module(hb)


def dead_pid() -> int:
    """A pid guaranteed dead: spawn a child that exits immediately, reap it."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class SelfGuardTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self._orig = hb.CORES_DIR
        hb.CORES_DIR = Path(td.name)
        self.addCleanup(lambda: setattr(hb, "CORES_DIR", self._orig))

    def _write_alive(self, pid, age_s=0.0):
        target = hb._alive_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"pid": pid, "host": "t", "schema_version": 2}))
        if age_s:
            past = time.time() - age_s
            os.utime(target, (past, past))
        return target

    def test_no_alive_file_takes_over(self):
        self.assertIsNone(hb.another_heartbeat_alive())

    def test_fresh_alive_with_live_other_pid_yields(self):
        # our parent is a live process that isn't us
        self._write_alive(os.getppid())
        self.assertEqual(hb.another_heartbeat_alive(), os.getppid())

    def test_own_pid_takes_over(self):
        # restart racing its own leftover file must not deadlock against itself
        self._write_alive(os.getpid())
        self.assertIsNone(hb.another_heartbeat_alive())

    def test_dead_pid_takes_over(self):
        self._write_alive(dead_pid())
        self.assertIsNone(hb.another_heartbeat_alive())

    def test_stale_file_takes_over_even_with_live_pid(self):
        self._write_alive(os.getppid(), age_s=120.0)
        self.assertIsNone(hb.another_heartbeat_alive())

    def test_malformed_payload_takes_over(self):
        target = hb._alive_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{not json")
        self.assertIsNone(hb.another_heartbeat_alive())

    def test_missing_pid_field_takes_over(self):
        target = hb._alive_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"host": "t"}))
        self.assertIsNone(hb.another_heartbeat_alive())

    def test_main_exits_zero_without_looping_when_guarded(self):
        self._write_alive(os.getppid())
        # would loop forever if the guard failed; guard-hit returns 0 instantly
        self.assertEqual(hb.main([]), 0)

    def test_once_bypasses_guard(self):
        self._write_alive(os.getppid())
        self.assertEqual(hb.main(["--once"]), 0)
        # --once overwrote the file with OUR beat (forced single write)
        self.assertEqual(json.loads(hb._alive_path().read_text())["pid"], os.getpid())


if __name__ == "__main__":
    unittest.main(verbosity=2)
