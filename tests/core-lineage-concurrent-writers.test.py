#!/usr/bin/env python3
"""Concurrent record_run() callers must not lose a session or a run.

`record_run` performs two read-modify-write updates. `_appending()` is what
makes them safe, and it must stay a real inter-process lock on EVERY platform:
a POSIX-only lock silently degrades to nothing on Windows, where two heartbeat
or restart callers can read the same list and atomically replace each other.

The negative control is the point of this file. A concurrency test that would
also pass with no lock at all proves nothing, so `test_without_the_lock_updates_are_lost`
runs the identical workload with `_appending` neutered and asserts rows ARE lost.
If that control ever stops failing, the workload no longer contends and the
positive test has stopped testing anything.

Run: python3 tests/core-lineage-concurrent-writers.test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WRITERS = 8

_CHILD = r'''
import sys, contextlib
sys.path.insert(0, {src!r})
import core_lineage
if {neuter!r}:
    @contextlib.contextmanager
    def _no_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        yield
    core_lineage._appending = _no_lock
core_lineage.record_run({ws!r}, "h", sys.argv[1], runtime="test")
'''


def _run(workspace: str, neuter: bool) -> tuple[list, list]:
    src = str(REPO / "src")
    prog = _CHILD.format(src=src, ws=workspace, neuter=neuter)
    procs = [subprocess.Popen([sys.executable, "-c", prog, f"session-{i:02d}"])
             for i in range(WRITERS)]
    for p in procs:
        p.wait()
    sys.path.insert(0, str(REPO / "src"))
    import core_lineage
    base = core_lineage.lineage_dir(workspace, "h")
    def _rows(name, key):
        f = base / name
        if not f.exists():
            return []
        try:
            return json.loads(f.read_text()).get(key, [])
        except ValueError:
            return []
    return _rows("sessions.json", "sessions"), _rows("runs.json", "runs")


class ConcurrentWriters(unittest.TestCase):
    def test_every_session_and_run_survives(self):
        with tempfile.TemporaryDirectory() as d:
            sessions, runs = _run(d, neuter=False)
            got = {s.get("session_id") for s in sessions}
            want = {f"session-{i:02d}" for i in range(WRITERS)}
            self.assertEqual(got, want, f"lost sessions: {sorted(want - got)}")
            self.assertEqual(len(runs), WRITERS, f"lost runs: {len(runs)} of {WRITERS}")

    def test_without_the_lock_updates_are_lost(self):
        """Negative control: the workload must genuinely contend."""
        with tempfile.TemporaryDirectory() as d:
            sessions, runs = _run(d, neuter=True)
            self.assertTrue(
                len(sessions) < WRITERS or len(runs) < WRITERS,
                "no rows were lost without the lock — the workload does not "
                "contend, so the positive test above proves nothing",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
