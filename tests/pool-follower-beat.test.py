#!/usr/bin/env python3
"""Contract tests for wrapper-owned follower liveness (L3 fix).

pool-follower-beat.sh must beat `<ws>/state/cores/<instance>.alive` while the
watched pid is alive, stop when it dies, and never unlink the file — the 90s
stale window is what absorbs KeepAlive restart gaps, so an unlink would make
the lead reclaim in-flight assignments on every clean follower restart.

pool-core-wrapper.sh must produce that beat for `core-$SUTANDO_CORE_ID` while
its claude child runs (the shipped path, exercised with a stub claude), and
propagate the child's exit code to launchd.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BEAT = REPO / "scripts" / "pool-follower-beat.sh"
WRAPPER = REPO / "scripts" / "pool-core-wrapper.sh"


def _wait_for(cond, timeout=10.0, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()


class PoolFollowerBeatTest(unittest.TestCase):
    def test_beats_while_watched_alive_then_stops_without_unlink(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            watched = subprocess.Popen(["sleep", "300"])
            env = dict(os.environ, SUTANDO_POOL_BEAT_INTERVAL="1")
            beater = subprocess.Popen(
                ["bash", str(BEAT), "core-7", str(ws), str(watched.pid)],
                env=env)
            alive = ws / "state" / "cores" / "core-7.alive"
            try:
                self.assertTrue(_wait_for(alive.exists),
                                "beat file never appeared")
                payload = json.loads(alive.read_text())
                self.assertEqual(payload["role"], "follower")
                self.assertEqual(payload["instance"], "core-7")
                self.assertEqual(payload["pid"], watched.pid)

                m1 = alive.stat().st_mtime
                self.assertTrue(
                    _wait_for(lambda: alive.stat().st_mtime > m1),
                    "mtime never advanced — a single write is not a beat")

                watched.terminate()
                watched.wait(timeout=10)
                self.assertIsNotNone(
                    beater.wait(timeout=10),
                    "beater must exit once the watched pid is gone")
                self.assertTrue(alive.exists(),
                                "beat file must survive follower exit "
                                "(no unlink — restart-gap contract)")
            finally:
                for p in (watched, beater):
                    if p.poll() is None:
                        p.kill()

    def test_wrapper_beats_core_id_and_propagates_exit_code(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            stub = ws / "stub-claude"
            stub.write_text("#!/bin/bash\nsleep 3\nexit 7\n")
            stub.chmod(0o755)
            env = dict(
                os.environ,
                POOL_REPO_DIR=str(REPO),
                POOL_CLAUDE_BIN=str(stub),
                POOL_WORKSPACE=str(ws),
                SUTANDO_CORE_ID="9",
                SUTANDO_POOL_BEAT_INTERVAL="1")
            wrapper = subprocess.Popen(["bash", str(WRAPPER)], env=env)
            alive = ws / "state" / "cores" / "core-9.alive"
            try:
                self.assertTrue(
                    _wait_for(alive.exists),
                    "wrapper never produced core-9.alive while claude ran")
                self.assertEqual(
                    wrapper.wait(timeout=15), 7,
                    "wrapper must propagate the claude child's exit code")
                self.assertTrue(alive.exists())
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()


if __name__ == "__main__":
    unittest.main()
