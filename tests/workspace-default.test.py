#!/usr/bin/env python3
"""Tests for src/workspace_default.py — workspace dir resolution contract.

Run: python3 tests/workspace-default.test.py
Exit: 0 on pass, 1 on fail.
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from workspace_default import default_workspace_dir, resolve_workspace  # noqa: E402


class TestWorkspaceDefault(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("SUTANDO_WORKSPACE")
        if "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["SUTANDO_WORKSPACE"] = self._saved_env
        elif "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]

    def test_default_is_application_support_workspace_subdir(self):
        d = default_workspace_dir()
        self.assertEqual(d.name, "workspace")
        self.assertEqual(d.parent.name, "sutando")
        self.assertEqual(d.parent.parent.name, "Application Support")
        self.assertEqual(d.parent.parent.parent.name, "Library")
        self.assertEqual(d, Path.home() / "Library" / "Application Support" / "sutando" / "workspace")

    def test_resolve_uses_env_when_set(self):
        os.environ["SUTANDO_WORKSPACE"] = "/tmp/test-ws"
        self.assertEqual(resolve_workspace(), Path("/tmp/test-ws"))

    def test_resolve_expanduser_on_tilde(self):
        os.environ["SUTANDO_WORKSPACE"] = "~/custom-ws"
        self.assertEqual(resolve_workspace(), Path.home() / "custom-ws")

    def test_resolve_falls_back_to_default_when_env_unset(self):
        self.assertEqual(resolve_workspace(), default_workspace_dir())

    def test_resolve_falls_back_when_env_empty_string(self):
        os.environ["SUTANDO_WORKSPACE"] = ""
        self.assertEqual(resolve_workspace(), default_workspace_dir())

    def test_resolve_falls_back_when_env_whitespace_only(self):
        os.environ["SUTANDO_WORKSPACE"] = "   "
        self.assertEqual(resolve_workspace(), default_workspace_dir())

    def test_resolve_never_returns_repo_root(self):
        """Anti-regression: the historical fallback was the script's repo root
        (`Path(__file__).resolve().parent.parent`), which polluted git status
        with runtime artifacts. The default must NOT be a Sutando repo path."""
        d = resolve_workspace()
        self.assertNotEqual(d, ROOT)
        self.assertFalse(str(d).endswith("/sutando"))
        self.assertFalse(str(d).endswith("/sutando/"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
