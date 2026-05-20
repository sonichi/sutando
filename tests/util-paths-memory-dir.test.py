#!/usr/bin/env python3
"""Tests for the SUTANDO_PRIVATE_DIR -> SUTANDO_MEMORY_DIR rename (#870).

Verifies:
  1. SUTANDO_MEMORY_DIR is honored as the canonical env var.
  2. SUTANDO_PRIVATE_DIR is honored as a legacy fallback.
  3. SUTANDO_MEMORY_DIR wins when both are set.
  4. A deprecation warning is emitted on every read of the legacy alias.
  5. Neither set -> None (no warning).

Run: python3 tests/util-paths-memory-dir.test.py
Exit: 0 on pass, 1 on fail.
"""
import io
import os
import socket
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Import after sys.path setup
from util_paths import (  # noqa: E402
    _memory_dir_env,
    _private_machine_dir,
    shared_personal_path,
)


def clear_env():
    for k in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR"):
        os.environ.pop(k, None)


class MemoryDirEnvTests(unittest.TestCase):
    def setUp(self):
        clear_env()

    def tearDown(self):
        clear_env()

    def test_unset_returns_none(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertIsNone(_memory_dir_env())
        self.assertEqual(buf.getvalue(), "")  # no warning when unset

    def test_new_var_returned(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/new-memory"
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertEqual(_memory_dir_env(), "/tmp/new-memory")
        self.assertEqual(buf.getvalue(), "")  # new var: no warning

    def test_legacy_var_returned_with_warning(self):
        os.environ["SUTANDO_PRIVATE_DIR"] = "/tmp/legacy-private"
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertEqual(_memory_dir_env(), "/tmp/legacy-private")
        self.assertIn("DEPRECATION", buf.getvalue())
        self.assertIn("SUTANDO_PRIVATE_DIR", buf.getvalue())
        self.assertIn("SUTANDO_MEMORY_DIR", buf.getvalue())

    def test_new_wins_when_both_set(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/new"
        os.environ["SUTANDO_PRIVATE_DIR"] = "/tmp/legacy"
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertEqual(_memory_dir_env(), "/tmp/new")
        # No warning when new wins — legacy path not consulted.
        self.assertEqual(buf.getvalue(), "")

    def test_every_read_warns(self):
        # Cron environments miss startup-only warnings, so the warning must
        # fire on every call, not just first. Regression guard for #870.
        os.environ["SUTANDO_PRIVATE_DIR"] = "/tmp/legacy"
        for _ in range(3):
            buf = io.StringIO()
            with redirect_stderr(buf):
                _memory_dir_env()
            self.assertIn("DEPRECATION", buf.getvalue())


class PrivateMachineDirTests(unittest.TestCase):
    def setUp(self):
        clear_env()

    def tearDown(self):
        clear_env()

    def test_returns_none_when_unset(self):
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(_private_machine_dir())

    def test_new_var_drives_machine_dir(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/memdir"
        host = socket.gethostname().split(".")[0]
        with redirect_stderr(io.StringIO()):
            p = _private_machine_dir()
        self.assertEqual(p, Path(f"/tmp/memdir/machine-{host}"))

    def test_legacy_var_drives_machine_dir(self):
        os.environ["SUTANDO_PRIVATE_DIR"] = "/tmp/legacy-dir"
        host = socket.gethostname().split(".")[0]
        with redirect_stderr(io.StringIO()):
            p = _private_machine_dir()
        self.assertEqual(p, Path(f"/tmp/legacy-dir/machine-{host}"))


class SharedPersonalPathTests(unittest.TestCase):
    def setUp(self):
        clear_env()

    def tearDown(self):
        clear_env()

    def test_resolves_via_new_var(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/mem"
        with redirect_stderr(io.StringIO()):
            p = shared_personal_path("MEMORY.md", workspace=Path("/tmp/ws"))
        # Neither path exists -> returns preferred memory-dir path.
        self.assertEqual(p, Path("/tmp/mem/MEMORY.md"))

    def test_resolves_via_legacy_var(self):
        os.environ["SUTANDO_PRIVATE_DIR"] = "/tmp/legacy-mem"
        with redirect_stderr(io.StringIO()):
            p = shared_personal_path("MEMORY.md", workspace=Path("/tmp/ws"))
        self.assertEqual(p, Path("/tmp/legacy-mem/MEMORY.md"))


if __name__ == "__main__":
    unittest.main()
