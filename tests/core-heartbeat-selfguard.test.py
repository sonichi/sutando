"""core_heartbeat self-guard: double-starts resolve to exactly one writer.

The guard yields ONLY to a fresh .alive naming a live pid that isn't us;
missing/stale/malformed files and dead pids all mean take over. Consumers
that use the .alive pid as a control target (pause/stop-core, #2198) depend
on this determinism; the schedule-crons step-5.5 backstop (#2199) is the
double-start source it defuses.

Run: python3 tests/core-heartbeat-selfguard.test.py
"""

import fcntl
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

    def test_eperm_pid_counts_as_live(self):
        # pid 1 (launchd/init) exists but os.kill(1, 0) raises EPERM for
        # non-root — the Codex-found case: EXISTS-but-unsignalable must yield,
        # not take over (a second writer would reintroduce the pid flap).
        if os.geteuid() == 0:
            self.skipTest("running as root: kill(1,0) would succeed, fixture invalid")
        self._write_alive(1)
        self.assertEqual(hb.another_heartbeat_alive(), 1)

    def test_unknown_oserror_takes_over(self):
        # non-ESRCH/EPERM OSError (e.g. EINVAL) — conservative take-over path;
        # covers the final except arm the real-pid fixtures can't reach.
        self._write_alive(os.getppid())
        orig = hb.os.kill

        def fake_kill(pid, sig):
            raise OSError(22, "Invalid argument")

        hb.os.kill = fake_kill
        self.addCleanup(lambda: setattr(hb.os, "kill", orig))
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

    def test_flock_makes_acquisition_atomic_within_process_pair(self):
        # Sequential sanity: first acquire wins, and after the winner beats,
        # a second acquire attempt in ANOTHER process yields to the winner.
        self.assertIsNone(hb.try_acquire_ownership())
        hb.write_beat()
        code = (
            "import importlib.util, sys, json, os\n"
            f"spec = importlib.util.spec_from_file_location('hb', {str(REPO / 'src' / 'core_heartbeat.py')!r})\n"
            "m = importlib.util.module_from_spec(spec); sys.modules['hb'] = m\n"
            "spec.loader.exec_module(m)\n"
            f"m.CORES_DIR = __import__('pathlib').Path({str(hb.CORES_DIR)!r})\n"
            "print(m.try_acquire_ownership())\n"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), str(os.getpid()), out.stderr)
        self._release_lock()

    def test_concurrent_start_exactly_one_owner(self):
        """The #2201 review repro, inverted into a regression test: N processes
        pass a barrier and race try_acquire_ownership + first beat. Exactly one
        must win; the final .alive must be valid JSON naming the winner."""
        import multiprocessing as mp
        ctx = mp.get_context("fork")  # fork so the CORES_DIR monkeypatch propagates
        n = 5
        barrier = ctx.Barrier(n)
        q = ctx.Queue()

        cores_dir = str(hb.CORES_DIR)

        def racer(barrier, q, cores_dir):
            import importlib.util as ilu
            from pathlib import Path
            spec = ilu.spec_from_file_location("hb_child", str(REPO / "src" / "core_heartbeat.py"))
            m = ilu.module_from_spec(spec)
            sys.modules["hb_child"] = m
            spec.loader.exec_module(m)
            m.CORES_DIR = Path(cores_dir)
            barrier.wait()  # two-phase: everyone checks at the same instant
            got = m.try_acquire_ownership()
            err = ""
            if got is None:
                try:
                    m.write_beat()
                except Exception as e:  # the shared-tmp collision class
                    err = f"{type(e).__name__}: {e}"
            q.put((os.getpid(), got is None, err))
            if got is None:
                time.sleep(0.5)  # hold the flock while siblings finish checking

        procs = [ctx.Process(target=racer, args=(barrier, q, cores_dir)) for _ in range(n)]
        for p in procs:
            p.start()
        results = [q.get(timeout=15) for _ in range(n)]
        for p in procs:
            p.join(timeout=15)

        owners = [pid for (pid, won, _err) in results if won]
        errors = [err for (_pid, _won, err) in results if err]
        self.assertEqual(len(owners), 1, f"expected exactly one owner, got {owners}; results={results}")
        self.assertEqual(errors, [], f"owner's first beat must not collide: {errors}")
        payload = json.loads(hb._alive_path().read_text())  # valid JSON or this raises
        self.assertEqual(payload["pid"], owners[0])
        # no stray shared-name staging file left behind
        leftovers = [p.name for p in hb.CORES_DIR.iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [], f"staging files left behind: {leftovers}")

    def test_lock_contention_returns_holder_pid_in_process(self):
        # flock conflicts even between two fds of the SAME process, so the
        # contention branch is coverable in-process: first acquire holds the
        # lock, second attempt must hit the OSError arm. With a beat on disk
        # the holder's pid is readable; without one it's the -1 sentinel.
        self.assertIsNone(hb.try_acquire_ownership())
        try:
            # no beat yet → holder unknown
            self.assertEqual(hb.try_acquire_ownership(), -1)
            hb.write_beat()
            # after a beat → holder pid comes from the .alive payload
            self.assertEqual(hb.try_acquire_ownership(), os.getpid())
        finally:
            self._release_lock()

    def test_flock_free_but_live_prebeat_owner_yields(self):
        # A pre-flock-build beater doesn't hold the lock but IS a live owner:
        # flock succeeds, the freshness/pid guard still yields — and the lock
        # must be RELEASED on that path (a second acquire attempt after the
        # yield must not hit the contention arm).
        self._write_alive(os.getppid())
        self.assertEqual(hb.try_acquire_ownership(), os.getppid())
        self.assertIsNone(hb._LOCK_FD, "yield path must not retain the lock fd")
        # lock was released → the next acquisition conflict-checks cleanly
        self._write_alive(os.getppid())
        self.assertEqual(hb.try_acquire_ownership(), os.getppid())

    def test_usurped_writer_exits_instead_of_flapping(self):
        # A different live pid legitimately owns the file → run_forever must
        # exit 0 before writing a single beat (the pre-beat recheck).
        self._write_alive(os.getppid())
        target = hb._alive_path()
        before = target.read_text()
        self.assertEqual(hb.run_forever(interval=0.05), 0)
        self.assertEqual(target.read_text(), before, "usurped writer flapped the file")

    def _release_lock(self):
        # tests share one process; drop the module-held flock between cases
        if hb._LOCK_FD is not None:
            try:
                fcntl.flock(hb._LOCK_FD, fcntl.LOCK_UN)
                os.close(hb._LOCK_FD)
            except OSError:
                pass
            hb._LOCK_FD = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
