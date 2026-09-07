#!/usr/bin/env python3
"""Contract tests for the production cross-platform advisory lock."""

from __future__ import annotations

import multiprocessing
import errno
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from file_lock import locked_file  # noqa: E402
import file_lock


def long_contender(path, started, acquired, release):
    started.set()
    with locked_file(Path(path)):
        acquired.set()
        release.wait(30)


class LockContract(unittest.TestCase):
    def contend(self, kill=False):
        ctx = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "lock")
            events = [(ctx.Event(), ctx.Event(), ctx.Event()) for _ in range(2)]
            children = [ctx.Process(target=long_contender, args=(path, *e)) for e in events]
            try:
                children[0].start()
                self.assertTrue(events[0][1].wait(10))
                children[1].start()
                self.assertTrue(events[1][0].wait(10))
                time.sleep(0.5 if kill else 12)
                self.assertTrue(children[1].is_alive())
                self.assertFalse(events[1][1].is_set())
                if kill:
                    children[0].kill()
                    children[0].join(10)
                else:
                    events[0][2].set()
                self.assertTrue(events[1][1].wait(10))
                events[1][2].set()
                for child in children:
                    child.join(10)
                self.assertEqual(children[1].exitcode, 0)
                if not kill:
                    self.assertEqual(children[0].exitcode, 0)
            finally:
                for child in children:
                    if child.pid is not None:
                        if child.is_alive():
                            child.kill()
                        child.join(10)
                        child.close()

    def test_prolonged_contention(self):
        self.contend()

    def test_killed_holder(self):
        self.contend(kill=True)

    def test_failed_acquisition_closes_without_unlock(self):
        failure = OSError(errno.EBADF, "acquisition failed")
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(file_lock, "lock_fd", side_effect=failure), \
                    patch.object(file_lock, "unlock_fd", side_effect=AssertionError) as unlock, \
                    patch.object(file_lock.os, "close", wraps=os.close) as close:
                with self.assertRaises(OSError) as raised:
                    with locked_file(Path(tmp) / "lock"):
                        self.fail("entered without acquiring")
                self.assertIs(raised.exception, failure)
                unlock.assert_not_called()
                close.assert_called_once()
                with self.assertRaises(OSError):
                    os.fstat(close.call_args.args[0])

    @unittest.skipUnless(os.name == "nt", "Windows contention errno")
    def test_windows_retry_classification(self):
        with tempfile.TemporaryFile() as fh:
            for error in (errno.EBADF, errno.EINVAL, errno.EDEADLK):
                with self.subTest(error=error), \
                        patch.object(file_lock.msvcrt, "locking", side_effect=OSError(error, "fault")) as lock, \
                        patch.object(file_lock.time, "sleep") as sleep:
                    with self.assertRaises(OSError):
                        file_lock.lock_fd(fh.fileno())
                    lock.assert_called_once()
                    sleep.assert_not_called()
            with patch.object(file_lock.msvcrt, "locking", side_effect=[OSError(errno.EACCES, "busy"), None]) as lock, \
                    patch.object(file_lock.time, "sleep") as sleep:
                file_lock.lock_fd(fh.fileno())
                self.assertEqual(lock.call_count, 2)
                sleep.assert_called_once_with(0.05)
            with patch.object(file_lock.msvcrt, "locking", side_effect=OSError(errno.EACCES, "busy")) as lock, \
                    patch.object(file_lock.time, "sleep") as sleep:
                with self.assertRaises(OSError):
                    file_lock.lock_fd(fh.fileno(), blocking=False)
                lock.assert_called_once_with(fh.fileno(), file_lock.msvcrt.LK_NBLCK, 1)
                sleep.assert_not_called()
            with patch.object(file_lock.msvcrt, "locking", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    file_lock.lock_fd(fh.fileno())

    def test_posix_delegation(self):
        fake = Mock(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        with patch.object(file_lock, "fcntl", fake):
            file_lock.lock_fd(42)
            fake.flock.assert_called_with(42, 2)
            file_lock.lock_fd(42, blocking=False)
            fake.flock.assert_called_with(42, 6)
            file_lock.unlock_fd(42)
            fake.flock.assert_called_with(42, 8)

    @unittest.skipUnless(os.name == "nt", "Windows handle accounting")
    def test_repeated_operations_do_not_leak(self):
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetCurrentProcess.argtypes = []
        api.GetCurrentProcess.restype = wintypes.HANDLE
        api.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetProcessHandleCount.restype = wintypes.BOOL
        def count():
            n = wintypes.DWORD()
            self.assertTrue(api.GetProcessHandleCount(api.GetCurrentProcess(), ctypes.byref(n)))
            return n.value
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            path.write_bytes(b"contents stay intact")
            before = count()
            for _ in range(1000):
                with locked_file(path):
                    pass
            self.assertEqual(count(), before)
            self.assertEqual(path.read_bytes(), b"contents stay intact")


def contender(path: str, ready, release, acquired) -> None:
    with locked_file(Path(path)):
        acquired.set()
        ready.set()
        release.wait(10)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sutando-file-lock-") as raw:
        path = str(Path(raw) / "state.lock")
        ready1 = multiprocessing.Event()
        release1 = multiprocessing.Event()
        acquired1 = multiprocessing.Event()
        first = multiprocessing.Process(
            target=contender, args=(path, ready1, release1, acquired1)
        )
        first.start()
        assert ready1.wait(10), "first process never acquired"

        ready2 = multiprocessing.Event()
        release2 = multiprocessing.Event()
        acquired2 = multiprocessing.Event()
        second = multiprocessing.Process(
            target=contender, args=(path, ready2, release2, acquired2)
        )
        second.start()
        time.sleep(0.5)
        assert not acquired2.is_set(), "second process acquired while first held"

        release1.set()
        assert ready2.wait(10), "second process did not acquire after release"
        release2.set()
        first.join(10)
        second.join(10)
        assert first.exitcode == 0 and second.exitcode == 0

    print("PASS: cross-platform file lock serializes contenders")


if __name__ == "__main__":
    main()
    unittest.main()
