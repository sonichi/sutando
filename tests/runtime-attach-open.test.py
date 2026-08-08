#!/usr/bin/env python3
"""Tests for v1 attach (tmux) + open (terminal adapter).

Contract (owner v1, taxonomy part 10): attach resolves the tmux argv FROM the
manifest, never hand-built; a manifest lacking tmux coords fails clearly; the
terminal adapter picks per-terminal and falls back to a printable command for
an unknown terminal — it never silently no-ops.

Run: python3 tests/runtime-attach-open.test.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))
sys.path.insert(0, str(ROOT / "src" / "runtime-cli"))

import instance_registry as reg  # noqa: E402
import terminal_open  # noqa: E402


class AttachTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SUTANDO_INSTANCE_REGISTRY"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("SUTANDO_INSTANCE_REGISTRY", None)
        self.tmp.cleanup()

    def test_attach_argv_resolved_from_manifest(self):
        reg.write_manifest("q-001", backend="tmux",
                           tmux_socket="/run/q-001/tmux.sock",
                           session="sutando-core")
        out = reg.attach("q-001")
        self.assertTrue(out["ok"])
        self.assertEqual(out["argv"],
                         ["tmux", "-S", "/run/q-001/tmux.sock",
                          "attach-session", "-t", "sutando-core"])

    def test_attach_missing_tmux_coords_fails_clearly(self):
        reg.write_manifest("q-002", backend="tmux")  # no socket/session
        self.assertFalse(reg.attach("q-002")["ok"])
        self.assertFalse(reg.attach("ghost")["ok"])  # not registered

    def test_attach_rejects_non_tmux_backend(self):
        self.assertFalse(reg.attach_command(
            {"runtime": {"backend": "docker", "tmux_socket": "x",
                         "session": "y"}})["ok"])


class OpenAdapterTests(unittest.TestCase):
    def test_apple_terminal_builds_tab_applescript(self):
        plan = terminal_open.build_open_plan("q-001", "apple_terminal")
        self.assertEqual(plan["method"], "applescript")
        self.assertIn("sutando attach q-001", plan["script"])
        self.assertIn("in front window", plan["script"])  # tab, not window
        win = terminal_open.build_open_plan("q-001", "apple_terminal", window=True)
        self.assertNotIn("in front window", win["script"])

    def test_unknown_terminal_falls_back_to_manual(self):
        plan = terminal_open.build_open_plan("q-001", "unknown")
        self.assertEqual(plan["method"], "manual")
        self.assertEqual(plan["command"], "sutando attach q-001")


if __name__ == "__main__":
    unittest.main(verbosity=2)
