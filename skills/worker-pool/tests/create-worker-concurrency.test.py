#!/usr/bin/env python3
"""Two create-worker registrations at once must not lose one.

Reviewers reproduced a lost update in `create_worker.compile_with()`: read the
roster, read bindings, mutate both in memory, then write each — three reads
and two writes with nothing serializing them. Two processes that both read
before either writes each merge from the same stale snapshot; only the last
write survives, and rc is 0 for both.

`pool_roster.register_worker()` is the fix under test: the whole
read-merge-write runs under one exclusive lock (`_locked`), so a second
caller's read cannot start until the first caller's write has finished.

The CONTROL below proves the lock is what serializes, not process-scheduling
luck: it disables `_locked` and stretches the window between read and write on
THIS repo's current `register_worker` (no copied-out old code), and shows the
same lost update the reviewers found.

Run: python3 skills/worker-pool/tests/create-worker-concurrency.test.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pool_roster as pr  # noqa: E402

# A separate process per registration: two Python-level callers of the same
# function share globals and cannot race each other's read/write halves.
CHILD = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    scripts, workspace, worker_id, room, barrier_dir, mode = sys.argv[1:7]
    sys.path.insert(0, scripts)
    import pool_roster as pr

    barrier = Path(barrier_dir)
    (barrier / (worker_id + ".ready")).touch()
    while len(list(barrier.glob("*.ready"))) < 2:
        time.sleep(0.01)

    if mode == "unlocked":
        import contextlib
        pr._locked = lambda ws: contextlib.nullcontext()
        real_load_bindings = pr.load_bindings

        def slow_load_bindings(ws, _real=real_load_bindings):
            got = _real(ws)
            time.sleep(0.4)
            return got

        pr.load_bindings = slow_load_bindings

    pr.register_worker(workspace, worker_id, worker_id, room)
""")


def race(workspace, barrier_dir, mode):
    """Run two `register_worker` calls concurrently in separate processes,
    released together once both have reached the barrier."""
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", CHILD, str(SCRIPTS), str(workspace),
             wid, room, str(barrier_dir), mode],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for wid, room in (("w1", "room-a"), ("w2", "room-b"))
    ]
    results = [p.communicate(timeout=15) for p in procs]
    for p, (out, err) in zip(procs, results):
        assert p.returncode == 0, f"child failed rc={p.returncode}\nstdout={out}\nstderr={err}"


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name) / "workspace"
        self.barrier_dir = Path(self._t.name) / "barrier"
        self.barrier_dir.mkdir()


class TestConcurrentRegistrationThroughTheProductionWriter(Base):
    def test_two_processes_registering_at_once_keep_both(self):
        race(self.ws, self.barrier_dir, "locked")
        roster = pr.load_roster(self.ws)
        self.assertEqual(set(roster["workers"]), {"w1", "w2"})
        want = {"room-a": "w1", "room-b": "w2"}
        self.assertEqual(pr.load_bindings(self.ws), want)
        self.assertEqual(roster["bindings"], want)


class TestTheLockIsWhatSerializes(Base):
    """Control: the same two processes, calling THIS repo's current
    `register_worker`, with its lock disabled and its read/write window
    stretched. If this still kept both workers, the passing test above would
    prove nothing about the lock."""

    def test_without_the_lock_the_race_drops_one_worker(self):
        race(self.ws, self.barrier_dir, "unlocked")
        roster = pr.load_roster(self.ws)
        bindings = pr.load_bindings(self.ws)
        self.assertLess(len(roster["workers"]), 2,
                        "expected the lost-update bug to reproduce with the lock disabled")
        self.assertLess(len(bindings), 2,
                        "expected the lost-update bug to reproduce with the lock disabled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
