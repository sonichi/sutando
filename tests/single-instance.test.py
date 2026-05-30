"""Tests for src/single_instance.py.

Covers:
  (a) acquire() succeeds when no other holder — lock file written with PID.
  (b) Second acquire() from a new process exits 0 (launchd-safe).
  (c) Lock releases when the holder process dies — next acquire() wins.
  (d) acquire() on two different names is independent (no cross-lock).

Run: `python3 tests/single-instance.test.py`
"""
import importlib.util
import os
import sys
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _load_single_instance(workspace_dir: Path):
    """Load single_instance with SUTANDO_WORKSPACE pointing at a temp dir."""
    os.environ["SUTANDO_WORKSPACE"] = str(workspace_dir)
    # Reload to pick up new env — module caches resolve_workspace() at call time.
    spec = importlib.util.spec_from_file_location(
        "single_instance", ROOT / "src" / "single_instance.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSingleInstance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        os.environ["SUTANDO_WORKSPACE"] = str(self.workspace)

    def tearDown(self):
        os.environ.pop("SUTANDO_WORKSPACE", None)
        self.tmp.cleanup()

    # (a) First acquire writes PID to lock file and returns normally.
    def test_first_acquire_writes_pid(self):
        mod = _load_single_instance(self.workspace)
        mod.acquire("test-bridge")
        lock_path = self.workspace / "state" / "locks" / "test-bridge.lock"
        self.assertTrue(lock_path.exists(), "lock file should be created")
        pid_in_file = int(lock_path.read_text().strip())
        self.assertEqual(pid_in_file, os.getpid())

    # (b) Second process attempting the same lock exits 0.
    def test_second_process_exits_zero(self):
        mod = _load_single_instance(self.workspace)
        mod.acquire("test-second")
        # Spawn a child process that tries to acquire the same lock.
        # It should exit 0 (not 1) because we hold it in this process.
        child = subprocess.run(
            [
                sys.executable, "-c",
                f"import sys; sys.path.insert(0, '{ROOT}/src');"
                f"import os; os.environ['SUTANDO_WORKSPACE']='{self.workspace}';"
                f"from single_instance import acquire; acquire('test-second')",
            ],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(child.returncode, 0, "contending process should exit 0")
        self.assertIn(b"already holds the lock", child.stderr)

    # (c) Lock releases after holder dies — next acquire wins.
    def test_lock_releases_after_holder_dies(self):
        # Start a subprocess that acquires the lock and then waits.
        holder = subprocess.Popen(
            [
                sys.executable, "-c",
                f"import sys, time; sys.path.insert(0, '{ROOT}/src');"
                f"import os; os.environ['SUTANDO_WORKSPACE']='{self.workspace}';"
                f"from single_instance import acquire; acquire('test-release');"
                f"time.sleep(30)",  # hold forever — we'll kill it
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Give it a moment to acquire the lock.
        time.sleep(0.3)
        holder.terminate()
        holder.wait(timeout=5)
        # Now this process should be able to acquire.
        mod = _load_single_instance(self.workspace)
        # If lock still held, acquire() would call os._exit(0) — but since
        # holder died, the OS released the flock and we should proceed normally.
        try:
            mod.acquire("test-release")
        except SystemExit as e:
            self.fail(f"acquire() exited after holder died: {e}")
        lock_path = self.workspace / "state" / "locks" / "test-release.lock"
        self.assertEqual(int(lock_path.read_text().strip()), os.getpid())

    # (d) Different names are independent — acquiring name-A doesn't block name-B.
    def test_different_names_are_independent(self):
        mod = _load_single_instance(self.workspace)
        mod.acquire("bridge-alpha")
        # Acquiring a different name in the same process should also succeed.
        try:
            mod.acquire("bridge-beta")
        except SystemExit as e:
            self.fail(f"acquire('bridge-beta') exited unexpectedly: {e}")
        for name in ("bridge-alpha", "bridge-beta"):
            lock_path = self.workspace / "state" / "locks" / f"{name}.lock"
            self.assertTrue(lock_path.exists(), f"{name} lock file missing")


if __name__ == "__main__":
    unittest.main()
