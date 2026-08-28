#!/usr/bin/env python3
"""The lead is a singleton, and only its own writer clears its heartbeat.

Before this, scripts/pool-lead-wrapper.sh decided by pgrep-then-exec, so two
concurrent starts both crossed into the daemon; and the daemon unlinked the
shared beat unconditionally on exit, so a losing instance deleted the LIVE
lead's heartbeat and every follower degraded to leaderless claiming.

Run: python3 tests/pool-lead-singleton.test.py   (stdlib only)
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "pool_lead_daemon", REPO / "scripts" / "pool-lead-daemon.py")
dm = importlib.util.module_from_spec(_spec)
sys.modules["pool_lead_daemon"] = dm
_spec.loader.exec_module(dm)


class SingletonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cores = Path(self.tmp.name) / "cores"

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_holder_in_this_process_is_refused(self):
        first = dm.acquire_singleton(self.cores)
        self.assertIsNotNone(first)
        # Same process, a DIFFERENT open file description — flock is per-OFD, so
        # this is a real second holder, not the same one re-entering.
        self.assertIsNone(dm.acquire_singleton(self.cores))
        first.close()

    def test_concurrent_starts_yield_exactly_one_winner(self):
        # The real race: two independent processes, no shared handle. Each holds
        # the lock ~1s so their attempts genuinely overlap.
        prog = (
            "import importlib.util,sys,time,pathlib\n"
            f"s=importlib.util.spec_from_file_location('d', {str(REPO / 'scripts' / 'pool-lead-daemon.py')!r})\n"
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
            "h=m.acquire_singleton(pathlib.Path(sys.argv[1]))\n"
            "print('WON' if h else 'LOST')\n"
            "sys.stdout.flush()\n"
            "time.sleep(1.0)\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", prog, str(self.cores)],
                                  stdout=subprocess.PIPE, text=True)
                 for _ in range(2)]
        outs = [p.communicate(timeout=60)[0].strip() for p in procs]
        self.assertEqual(outs.count("WON"), 1,
                         f"expected exactly one winner, got {outs}")

    def test_lock_frees_when_the_holder_exits(self):
        first = dm.acquire_singleton(self.cores)
        self.assertIsNotNone(first)
        first.close()
        second = dm.acquire_singleton(self.cores)
        self.assertIsNotNone(second, "a released lock must be re-acquirable")
        second.close()


class BeatOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cores = Path(self.tmp.name) / "cores"
        self.cores.mkdir(parents=True)
        self.beat = self.cores / f"{dm.LEAD_LABEL}.alive"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, pid):
        self.beat.write_text(json.dumps({"role": "pool-lead", "pid": pid, "ts": 1.0}))

    def test_writer_clears_its_own_beat(self):
        self._write(os.getpid())
        self.assertTrue(dm.release_beat(self.beat, os.getpid()))
        self.assertFalse(self.beat.exists())

    def test_a_different_instance_must_not_clear_the_live_beat(self):
        self._write(os.getpid() + 1)          # the LIVE lead
        self.assertFalse(dm.release_beat(self.beat, os.getpid()))
        self.assertTrue(self.beat.exists(), "a loser deleted the live lead's beat")

    def test_absent_or_corrupt_beat_is_not_an_error(self):
        self.assertFalse(dm.release_beat(self.beat, os.getpid()))
        self.beat.write_text("{not json")
        self.assertFalse(dm.release_beat(self.beat, os.getpid()))
        self.assertTrue(self.beat.exists())


class WrapperTests(unittest.TestCase):
    def test_wrapper_does_not_claim_to_be_the_boundary(self):
        s = (REPO / "scripts" / "pool-lead-wrapper.sh").read_text()
        self.assertIn("NOT the singleton boundary", s)

    def test_wrapper_is_valid_shell(self):
        r = subprocess.run(["bash", "-n", str(REPO / "scripts" / "pool-lead-wrapper.sh")],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


class DaemonMainTickTests(unittest.TestCase):
    """One full tick of main(), driven end to end. The daemon is the process
    the whole pool depends on, and none of its loop body was measured."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "tasks").mkdir()
        (self.ws / "state" / "cores").mkdir(parents=True)
        self.cores = self.ws / "state" / "cores"

    def tearDown(self):
        self.tmp.cleanup()

    def _run_one_tick(self, argv=("pool-lead-daemon.py",)):
        """Stop after a single iteration by firing the daemon's OWN SIGTERM
        handler from the sleep it makes at the end of each pass."""
        captured = {}

        def capture_signal(sig, handler):
            captured[sig] = handler

        def sleep_once(_seconds):
            captured[__import__("signal").SIGTERM](None, None)

        with (mock.patch.object(dm, "_workspace", return_value=self.ws),
              mock.patch.object(dm.signal, "signal", side_effect=capture_signal),
              mock.patch.object(dm.time, "sleep", side_effect=sleep_once),
              mock.patch.object(sys, "argv", list(argv))):
            return dm.main()

    def test_a_tick_assigns_beats_and_releases_on_the_way_out(self):
        (self.cores / "core-1.alive").write_text("{}")
        (self.ws / "tasks" / "task-d1.txt").write_text("id: task-d1\n")

        rc = self._run_one_tick()

        self.assertEqual(rc, 0)
        names = sorted(p.name for p in (self.ws / "tasks").iterdir())
        self.assertEqual(names, ["task-d1.assigned-core-1.txt"],
                         "the tick did not assign the queued task")
        self.assertFalse((self.cores / f"{dm.LEAD_LABEL}.alive").exists(),
                         "the lead beat outlived the daemon; followers would "
                         "keep deferring to a lead that is gone")
        self.assertTrue((self.ws / "state" / "pool-status.json").is_file())

    def test_a_second_lead_stands_down_instead_of_double_assigning(self):
        held = dm.acquire_singleton(self.cores)
        self.assertIsNotNone(held)
        (self.cores / "core-1.alive").write_text("{}")
        (self.ws / "tasks" / "task-d2.txt").write_text("id: task-d2\n")
        try:
            with mock.patch.object(dm, "_workspace", return_value=self.ws), \
                 mock.patch.object(sys, "argv", ["pool-lead-daemon.py"]):
                rc = dm.main()
        finally:
            held.close()
        self.assertEqual(rc, 0, "standing down is a clean exit, not a failure")
        self.assertTrue((self.ws / "tasks" / "task-d2.txt").is_file(),
                        "the stood-down lead assigned work anyway")



if __name__ == "__main__":
    unittest.main(verbosity=2)
