#!/usr/bin/env python3
"""sparrowd: a worker exit code in `WorkerSpec.hold_rc` is held, not restarted, until the spec under
its name changes; any other code keeps the restart backoff. A restarted worker is told the previous
run's exit code in SPARROWD_LAST_RC; a first start, and a start after a spec change, is not.

Run: python3 tests/sparrowd-hold-rc.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow.sparrowd import LAST_RC_ENV, ControlServer, Supervisor, WorkerSpec

# Appends the SPARROWD_LAST_RC it was given (or "-") to argv[1], then exits with argv[2].
CHILD = ("import os, sys\n"
         "with open(sys.argv[1], 'a') as f: f.write(os.environ.get('SPARROWD_LAST_RC', '-') + '\\n')\n"
         "raise SystemExit(int(sys.argv[2]))\n")


def wait_for(pred, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


class HoldRc(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.child = self.dir / "child.py"
        self.child.write_text(CHILD)
        self.sups = []

    def tearDown(self):
        for sup in self.sups:
            sup.stop(grace_s=2)

    def spec(self, rc, log="runs", **kw):
        return WorkerSpec("core", [sys.executable, str(self.child), str(self.dir / log), str(rc)], **kw)

    def runs(self, log="runs"):
        try:
            return (self.dir / log).read_text().split()
        except FileNotFoundError:
            return []

    def supervise(self, specs):
        self.sleeps = []
        sup = Supervisor(specs, backoff_initial_s=0.5, sleep=lambda s: (self.sleeps.append(s), time.sleep(0.01)))
        self.sups.append(sup)
        sup.start()
        return sup

    def test_a_held_code_is_not_restarted(self):
        sup = self.supervise([self.spec(75, hold_rc=frozenset({75}))])
        self.assertTrue(wait_for(lambda: sup.status()["workers"]["core"]["state"] == "held"))
        time.sleep(0.3)
        st = sup.status()["workers"]["core"]
        self.assertEqual((self.runs(), st["last_exit"], st["restarts"], st["pid"]), (["-"], 75, 0, None))
        self.assertEqual(self.sleeps, [], "no backoff is waited: nothing restarts")

    def test_a_changed_spec_restarts_a_held_worker_as_a_first_start(self):
        sup = self.supervise([self.spec(75, hold_rc=frozenset({75}), revision="a")])
        self.assertTrue(wait_for(lambda: sup.status()["workers"]["core"]["state"] == "held"))
        sup.sync([self.spec(75, hold_rc=frozenset({75}), revision="a")])
        time.sleep(0.2)
        self.assertEqual(self.runs(), ["-"], "the same spec stays held")
        added, removed = sup.sync([self.spec(75, hold_rc=frozenset({75}), revision="b")])
        self.assertEqual((added, removed), (["core"], ["core"]))
        self.assertTrue(wait_for(lambda: len(self.runs()) == 2))
        self.assertTrue(wait_for(lambda: sup.status()["workers"]["core"]["state"] == "held"))
        self.assertEqual(self.runs(), ["-", "-"], "a new spec's first run is told no previous exit")

    def test_other_codes_keep_the_backoff_and_are_told_the_last_exit(self):
        sup = self.supervise([self.spec(3, hold_rc=frozenset({75}))])
        self.assertTrue(wait_for(lambda: len(self.runs()) >= 3))
        sup.stop(grace_s=2)
        self.assertEqual(self.runs()[:3], ["-", "3", "3"])
        self.assertGreaterEqual(len(self.sleeps), 2)
        self.assertGreater(self.sleeps[1], self.sleeps[0], "rapid crashes still climb the backoff")

    def test_no_hold_codes_is_the_old_restart_for_every_code(self):
        sup = self.supervise([self.spec(75)])
        self.assertTrue(wait_for(lambda: len(self.runs()) >= 2))
        sup.stop(grace_s=2)
        self.assertEqual(self.runs()[:2], ["-", "75"])

    def test_an_inherited_last_rc_never_reaches_a_first_start(self):
        with mock.patch.dict("os.environ", {LAST_RC_ENV: "9"}):
            sup = self.supervise([self.spec(75, hold_rc=frozenset({75}))])
            self.assertTrue(wait_for(lambda: sup.status()["workers"]["core"]["state"] == "held"))
        self.assertEqual(self.runs(), ["-"])

    def test_a_held_worker_reads_as_healthy(self):
        sup = self.supervise([self.spec(75, hold_rc=frozenset({75}))])
        self.assertTrue(wait_for(lambda: sup.status()["workers"]["core"]["state"] == "held"))
        sock = self.dir / "ctl.sock"
        ctl = ControlServer(sock, sup, threading.Event().set)
        ctl.start()
        self.addCleanup(ctl.close)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(sock))
        c.sendall(b'{"op": "health"}\n')
        resp = json.loads(c.makefile("r").readline())
        c.close()
        self.assertEqual(resp, {"ok": True, "healthy": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)
