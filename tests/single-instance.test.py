#!/usr/bin/env python3
"""Tests for src/single_instance.py — flock-based bridge single-instance enforcement.

Run: python3 tests/single-instance.test.py
Exit: 0 on pass, 1 on fail.
"""
import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _acquire_in_subprocess(name: str, workspace: str, result_queue: multiprocessing.Queue) -> None:
    """Worker: call acquire() and report success or exit code."""
    os.environ["SUTANDO_WORKSPACE"] = workspace
    sys.modules.pop("single_instance", None)
    sys.modules.pop("workspace_default", None)
    import single_instance  # noqa: F401 (re-import with patched env)
    try:
        single_instance.acquire(name)
        result_queue.put("acquired")
    except SystemExit as e:
        result_queue.put(f"exit:{e.code}")


class TestSingleInstance(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("SUTANDO_WORKSPACE")
        self.tmp = Path(tempfile.mkdtemp(prefix="single-instance-test-"))
        os.environ["SUTANDO_WORKSPACE"] = str(self.tmp)
        # Force fresh import each test.
        sys.modules.pop("single_instance", None)
        sys.modules.pop("workspace_default", None)

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["SUTANDO_WORKSPACE"] = self._saved_env
        elif "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        sys.modules.pop("single_instance", None)
        sys.modules.pop("workspace_default", None)

    def test_lock_dir_created(self):
        import single_instance
        single_instance.acquire("test-bridge")
        lock_dir = self.tmp / "state" / "locks"
        self.assertTrue(lock_dir.is_dir())

    def test_lock_file_created(self):
        import single_instance
        single_instance.acquire("test-bridge")
        lock_file = self.tmp / "state" / "locks" / "test-bridge.lock"
        self.assertTrue(lock_file.exists())

    def test_pid_written_to_lock_file(self):
        import single_instance
        single_instance.acquire("test-bridge")
        lock_file = self.tmp / "state" / "locks" / "test-bridge.lock"
        content = lock_file.read_text().strip()
        self.assertEqual(content, str(os.getpid()))

    def test_acquire_twice_same_process_ok(self):
        """Second acquire in the same process should succeed (fd already held)."""
        import single_instance
        single_instance.acquire("test-bridge-a")
        # A second call with a different name must also work.
        single_instance.acquire("test-bridge-b")
        self.assertGreaterEqual(len(single_instance._held_fds), 2)

    def test_second_process_exits_cleanly(self):
        """A second process trying to acquire the same lock must exit(0)."""
        import single_instance
        # Hold the lock in the parent process first.
        single_instance.acquire("test-exclusive")

        # Spawn a child that tries to acquire the same lock.
        q: multiprocessing.Queue = multiprocessing.Queue()
        p = multiprocessing.Process(
            target=_acquire_in_subprocess,
            args=("test-exclusive", str(self.tmp), q),
        )
        p.start()
        p.join(timeout=5)
        self.assertFalse(p.is_alive(), "subprocess hung — flock call did not return")
        # os._exit(0) means exitcode 0.
        self.assertEqual(p.exitcode, 0, f"Expected exit 0, got {p.exitcode}")

    def test_different_names_dont_conflict(self):
        """Two different bridge names must not conflict with each other."""
        import single_instance
        single_instance.acquire("bridge-alpha")

        q: multiprocessing.Queue = multiprocessing.Queue()
        p = multiprocessing.Process(
            target=_acquire_in_subprocess,
            args=("bridge-beta", str(self.tmp), q),
        )
        p.start()
        p.join(timeout=5)
        self.assertFalse(p.is_alive(), "subprocess hung")
        # Different names → child should acquire successfully, not exit 0 via _exit.
        # exitcode None means normal exit after acquire (process finished function).
        self.assertEqual(p.exitcode, 0)
        result = q.get_nowait()
        self.assertEqual(result, "acquired")


if __name__ == "__main__":
    unittest.main()
