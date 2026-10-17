#!/usr/bin/env python3
"""src/delivery/notifier_lock.py: one holder at a time, released by the kernel when it dies."""
import fcntl
import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "delivery"))
import notifier_lock  # noqa: E402

HELPER = REPO / "src" / "delivery" / "notifier_lock.py"


class NotifierLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.lock = str(self.dir / "state" / "codex-notifier.lock")

    def _holder(self, *cmd, timeout="30"):
        return subprocess.Popen([sys.executable, str(HELPER), "--lock", self.lock, "--timeout", timeout, "--", *cmd],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def _wait_for(self, path):
        deadline = time.monotonic() + 10
        while not path.exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

    def test_a_second_holder_waits_until_the_first_exits(self):
        log = self.dir / "log"
        first = self._holder("bash", "-c", f'echo first-in >> "{log}"; sleep 1; echo first-out >> "{log}"')
        self._wait_for(log)
        second = self._holder("bash", "-c", f'echo second-in >> "{log}"')
        first.communicate(timeout=30)
        second.communicate(timeout=30)
        self.assertEqual(log.read_text().split(), ["first-in", "first-out", "second-in"])

    def test_a_kill_9_holder_releases_at_once(self):
        ready = self.dir / "ready"
        first = self._holder("bash", "-c", f'touch "{ready}"; exec sleep 60')
        self._wait_for(ready)
        os.kill(first.pid, signal.SIGKILL)
        first.communicate(timeout=30)
        started = time.monotonic()
        second = self._holder("true")
        second.communicate(timeout=30)
        self.assertEqual(second.returncode, 0)
        self.assertLess(time.monotonic() - started, 3)

    def test_a_successor_waits_for_a_surviving_child_of_a_killed_holder(self):
        # The child inherited the lock and could still act for the dead holder: it is the fence.
        ready, child_pid = self.dir / "ready", self.dir / "child"
        first = self._holder("bash", "-c", f'sleep 2 & echo $! > "{child_pid}"; touch "{ready}"; exec sleep 60')
        self._wait_for(ready)
        child = int(child_pid.read_text())
        os.kill(first.pid, signal.SIGKILL)
        first.wait(timeout=30)
        self.addCleanup(lambda: (first.stdout.close(), first.stderr.close()))
        second = self._holder("bash", "-c", f'kill -0 {child} 2>/dev/null && echo CHILD-ALIVE || echo CHILD-GONE',
                              timeout="20")
        out, err = second.communicate(timeout=30)
        self.assertEqual((second.returncode, out.strip()), (0, "CHILD-GONE"), err)

    def test_check_held_accepts_only_the_description_that_holds_the_lock(self):
        held = notifier_lock.acquire(self.lock, 1, 1)
        self.addCleanup(os.close, held)
        self.assertTrue(notifier_lock.holds(held, self.lock))
        other = os.open(self.lock, os.O_RDWR)
        self.addCleanup(os.close, other)
        self.assertFalse(notifier_lock.holds(other, self.lock), "same file, but another description holds it")
        elsewhere = os.open(str(self.dir / "elsewhere"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, elsewhere)
        self.assertFalse(notifier_lock.holds(elsewhere, self.lock))
        self.assertFalse(notifier_lock.holds(9999, self.lock))
        self.assertEqual(notifier_lock.main(["--lock", self.lock, "--check-held", str(other)]), 1)
        self.assertEqual(notifier_lock.main(["--lock", self.lock, "--check-held", str(held)]), 0)

    def test_a_busy_lock_times_out_with_exit_75(self):
        ready = self.dir / "ready"
        first = self._holder("bash", "-c", f'touch "{ready}"; sleep 5')
        self._wait_for(ready)
        second = self._holder("true", timeout="0.3")
        _, err = second.communicate(timeout=30)
        self.assertEqual(second.returncode, notifier_lock.EXIT_BUSY)
        self.assertIn("still held", err)
        first.kill()
        first.communicate(timeout=30)

    def test_a_waiter_logs_to_its_file_and_publishes_then_clears_its_waiting_record(self):
        ready, logs = self.dir / "ready", self.dir / "logs"
        logs.mkdir()
        waiting = Path(self.lock + ".waiting")
        first = self._holder("bash", "-c", f'touch "{ready}"; sleep 2')
        self._wait_for(ready)
        second = subprocess.Popen([sys.executable, str(HELPER), "--lock", self.lock, "--log-every", "0.5",
                                   "--log-file", str(logs / "task-notifier.log"), "--", "true"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self._wait_for(waiting)
        pid, since = waiting.read_text().split()
        self.assertEqual(int(pid), second.pid)
        self.assertLessEqual(abs(int(since) - time.time()), 5)
        first.communicate(timeout=30)
        second.communicate(timeout=30)
        self.assertEqual(second.returncode, 0)
        self.assertFalse(waiting.exists(), "cleared once the lock is taken")
        self.assertIn("another notifier holds it", (logs / "task-notifier.log").read_text())

    def test_a_log_file_whose_directory_is_missing_or_unwritable_is_skipped(self):
        log = notifier_lock.stderr_and_file(str(self.dir / "missing" / "x.log"))
        with redirect_stderr(io.StringIO()) as err:
            log("hello")
        self.assertIn("hello", err.getvalue())
        with redirect_stderr(io.StringIO()):
            notifier_lock.stderr_and_file(str(self.dir / "ok.log"))("hello")
        self.assertTrue((self.dir / "ok.log").read_text().endswith(" hello\n"))
        (self.dir / "ro").mkdir()
        (self.dir / "ro").chmod(0o500)
        try:
            with redirect_stderr(io.StringIO()):
                notifier_lock.stderr_and_file(str(self.dir / "ro" / "x.log"))("hello")
                notifier_lock._write_waiting(str(self.dir / "ro" / "w"))
        finally:
            (self.dir / "ro").chmod(0o700)

    def test_the_command_inherits_the_lock_on_fd_9(self):
        r = subprocess.run([sys.executable, str(HELPER), "--lock", self.lock, "--", "bash", "-c",
                            'echo "$SUTANDO_NOTIFIER_LOCK_FD"; [ -e /dev/fd/9 ] && echo open'],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.split(), ["9", "open"], r.stderr)

    def test_a_lock_file_replaced_while_waiting_is_reopened_not_trusted(self):
        # A waits on the old file; the path is replaced and the old lock released. A lock on the
        # old inode would guard nothing, so acquire must end up holding the file now at the path.
        old = notifier_lock.acquire(self.lock, 1, 1)
        replaced = []

        def sleep(_):
            if not replaced:
                os.unlink(self.lock)
                Path(self.lock).write_text("")
                os.close(old)
                replaced.append(True)

        got = notifier_lock.acquire(self.lock, 5, 5, sleep=sleep)
        self.assertIsNotNone(got)
        self.addCleanup(os.close, got)
        self.assertEqual(replaced, [True])
        self.assertTrue(notifier_lock.holds(got, self.lock))
        rival = os.open(self.lock, os.O_RDWR)
        self.addCleanup(os.close, rival)
        with self.assertRaises(BlockingIOError):
            fcntl.flock(rival, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_acquire_logs_while_it_waits_then_gives_up(self):
        held = notifier_lock.acquire(self.lock, 1, 1)
        self.addCleanup(os.close, held)
        now = [0.0]
        logs = []
        got = notifier_lock.acquire(self.lock, 2.0, 0.5, clock=lambda: now[0],
                                    sleep=lambda s: now.__setitem__(0, now[0] + 0.3), log=logs.append)
        self.assertIsNone(got)
        self.assertTrue(logs and "waiting" in logs[0])

    def test_main_usage_errors_and_exec(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(notifier_lock.main(["--lock", self.lock]), 2)
            self.assertEqual(notifier_lock.main(["--lock", self.lock, "--"]), 2)
        calls = []
        rc = notifier_lock.main(["--lock", self.lock, "--", "true"],
                                execvpe=lambda f, a, e: calls.append((f, a, e["SUTANDO_NOTIFIER_LOCK_FD"])))
        self.assertEqual((rc, calls), (0, [("true", ["true"], "9")]))
        os.close(notifier_lock.LOCK_FD)
        held = notifier_lock.acquire(self.lock, 1, 1)
        self.addCleanup(os.close, held)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(notifier_lock.main(["--lock", self.lock, "--timeout", "0.2", "--", "true"]),
                             notifier_lock.EXIT_BUSY)


if __name__ == "__main__":
    unittest.main()
