"""Tests for src/single_instance.py.

Covers:
  (a) acquire() succeeds when no other holder — lock file written with PID.
  (b) Second acquire() from a new process exits 75 (EXIT_STANDDOWN).
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
    os.environ["SUTANDO_TEST_MODE"] = "1"  # v0.8: opt-in env-honor
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
        os.environ["SUTANDO_TEST_MODE"] = "1"  # v0.8: opt-in env-honor

    def tearDown(self):
        os.environ.pop("SUTANDO_WORKSPACE", None)
        os.environ.pop("SUTANDO_TEST_MODE", None)
        self.tmp.cleanup()

    # (a) First acquire writes PID to lock file and returns normally.
    def test_first_acquire_writes_pid(self):
        mod = _load_single_instance(self.workspace)
        mod.acquire("test-bridge")
        lock_path = self.workspace / "state" / "locks" / "test-bridge.lock"
        self.assertTrue(lock_path.exists(), "lock file should be created")
        pid_in_file = int(lock_path.read_text().strip())
        self.assertEqual(pid_in_file, os.getpid())

    # (b) Second process attempting the same lock stands down with 75.
    def test_second_process_exits_standdown_code(self):
        mod = _load_single_instance(self.workspace)
        mod.acquire("test-second")
        # Spawn a child process that tries to acquire the same lock.
        # 75, not 0: a bridge whose main loop merely returns also exits 0, so a
        # supervisor cannot tell that from a deliberate stand-down.
        child = subprocess.run(
            [
                sys.executable, "-c",
                f"import sys; sys.path.insert(0, '{ROOT}/src');"
                f"import os; os.environ['SUTANDO_WORKSPACE']='{self.workspace}'; os.environ['SUTANDO_TEST_MODE']='1';"
                f"from single_instance import acquire; acquire('test-second')",
            ],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(child.returncode, 75,
                         "contending process should exit EXIT_STANDDOWN (75)")
        self.assertIn(b"already holds the lock", child.stderr)

    # (c) Lock releases after holder dies — next acquire wins.
    def test_lock_releases_after_holder_dies(self):
        # Start a subprocess that acquires the lock and then waits.
        holder = subprocess.Popen(
            [
                sys.executable, "-c",
                f"import sys, time; sys.path.insert(0, '{ROOT}/src');"
                f"import os; os.environ['SUTANDO_WORKSPACE']='{self.workspace}'; os.environ['SUTANDO_TEST_MODE']='1';"
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
        # If lock still held, acquire() would call os._exit(75) — but since
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


    # In-process so coverage records the stand-down line: the sibling test's
    # child runs in another interpreter, where coverage cannot see it.
    def test_standdown_exits_with_the_declared_constant(self):
        import os as _os
        mod = _load_single_instance(self.workspace)
        calls = []
        real_exit = _os._exit
        _os._exit = lambda code: (calls.append(code), (_ for _ in ()).throw(SystemExit(code)))[0]
        try:
            mod.acquire("coverage-standdown-probe")      # first holder: returns
            with self.assertRaises(SystemExit):
                mod.acquire("coverage-standdown-probe")  # contender: stands down
        finally:
            _os._exit = real_exit
        self.assertEqual(calls, [mod.EXIT_STANDDOWN],
                         "acquire() must stand down with EXIT_STANDDOWN, not a bare 0")
        self.assertEqual(mod.EXIT_STANDDOWN, 75)


if __name__ == "__main__":
    unittest.main()
