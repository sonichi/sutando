#!/usr/bin/env python3
"""Tests for SUTANDO_HOST_LABEL (#871).

Verifies:
  1. Unset → falls back to hostname.
  2. Set → override returned (trimmed).
  3. Empty / whitespace-only → treated as unset (don't collapse to `machine-/`).
  4. `_private_machine_dir()` composes `machine-<label>/` with the override.

Run: python3 tests/util-paths-host-label.test.py
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

from util_paths import _host_label, _private_machine_dir  # noqa: E402


def clear_env():
    for k in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR", "SUTANDO_HOST_LABEL"):
        os.environ.pop(k, None)


class HostLabelTests(unittest.TestCase):
    def setUp(self):
        clear_env()

    def tearDown(self):
        clear_env()

    def test_unset_falls_back_to_hostname(self):
        expected = socket.gethostname().split(".")[0]
        self.assertEqual(_host_label(), expected)

    def test_override_used_verbatim(self):
        os.environ["SUTANDO_HOST_LABEL"] = "my-stable-mac"
        self.assertEqual(_host_label(), "my-stable-mac")

    def test_override_is_trimmed(self):
        os.environ["SUTANDO_HOST_LABEL"] = "  studio-2  "
        self.assertEqual(_host_label(), "studio-2")

    def test_empty_override_falls_back(self):
        # Regression guard: an empty env var must NOT collapse the path to
        # `machine-/`. A user setting `export SUTANDO_HOST_LABEL=` should get
        # default hostname behavior, not a broken path.
        os.environ["SUTANDO_HOST_LABEL"] = ""
        self.assertEqual(_host_label(), socket.gethostname().split(".")[0])

    def test_whitespace_only_override_falls_back(self):
        os.environ["SUTANDO_HOST_LABEL"] = "   "
        self.assertEqual(_host_label(), socket.gethostname().split(".")[0])

    def test_private_machine_dir_uses_override(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/mem"
        os.environ["SUTANDO_HOST_LABEL"] = "pinned"
        with redirect_stderr(io.StringIO()):
            p = _private_machine_dir()
        self.assertEqual(p, Path("/tmp/mem/machine-pinned"))

    def test_private_machine_dir_uses_hostname_when_unset(self):
        os.environ["SUTANDO_MEMORY_DIR"] = "/tmp/mem"
        expected_host = socket.gethostname().split(".")[0]
        with redirect_stderr(io.StringIO()):
            p = _private_machine_dir()
        self.assertEqual(p, Path(f"/tmp/mem/machine-{expected_host}"))


if __name__ == "__main__":
    unittest.main()
