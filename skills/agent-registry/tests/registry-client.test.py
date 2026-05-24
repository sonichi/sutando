#!/usr/bin/env python3
"""
Tests for skills/agent-registry/scripts/registry-client.py

Coverage:
  resolve_workspace — env var override, default path
  pid_alive         — pid=0, pid=None, own PID (alive), nonexistent PID (dead),
                      PermissionError → True
  read_discovery    — valid JSON file, missing file, malformed JSON

Run: python3 skills/agent-registry/tests/registry-client.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent.parent.parent

# Must set SUTANDO_WORKSPACE before module loads (it's read at module level)
_tmpdir = tempfile.mkdtemp()
os.makedirs(os.path.join(_tmpdir, "state"), exist_ok=True)
with patch.dict(os.environ, {"SUTANDO_WORKSPACE": _tmpdir}):
    _spec = importlib.util.spec_from_file_location(
        "registry_client",
        REPO / "skills" / "agent-registry" / "scripts" / "registry-client.py",
    )
    rc = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(rc)


# ── resolve_workspace ─────────────────────────────────────────────────────────

class TestResolveWorkspace(unittest.TestCase):
    def test_env_var_used_when_set(self):
        with patch.dict(os.environ, {"SUTANDO_WORKSPACE": "/tmp/my-workspace"}):
            result = rc.resolve_workspace()
        self.assertEqual(result, "/tmp/my-workspace")

    def test_default_when_no_env(self):
        env = {k: v for k, v in os.environ.items() if k != "SUTANDO_WORKSPACE"}
        with patch.dict(os.environ, env, clear=True):
            result = rc.resolve_workspace()
        self.assertIn(".sutando/workspace", result)

    def test_tilde_expanded(self):
        with patch.dict(os.environ, {"SUTANDO_WORKSPACE": "~/.sutando/workspace"}):
            result = rc.resolve_workspace()
        self.assertNotIn("~", result)


# ── pid_alive ─────────────────────────────────────────────────────────────────

class TestPidAlive(unittest.TestCase):
    def test_zero_pid_always_true(self):
        self.assertTrue(rc.pid_alive(0))

    def test_none_pid_always_true(self):
        self.assertTrue(rc.pid_alive(None))

    def test_own_pid_alive(self):
        self.assertTrue(rc.pid_alive(os.getpid()))

    def test_nonexistent_pid_false(self):
        # PID 999999 very unlikely to be alive
        with patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(rc.pid_alive(999999))

    def test_permission_error_treated_as_alive(self):
        # Process exists but owned by another user
        with patch("os.kill", side_effect=PermissionError):
            self.assertTrue(rc.pid_alive(12345))

    def test_oserror_treated_as_dead(self):
        with patch("os.kill", side_effect=OSError):
            self.assertFalse(rc.pid_alive(12345))

    def test_negative_pid_always_true(self):
        # Negative or zero means nothing to watch
        self.assertTrue(rc.pid_alive(-1))


# ── read_discovery ────────────────────────────────────────────────────────────

class TestReadDiscovery(unittest.TestCase):
    def setUp(self):
        self._orig_path = rc.DISCOVERY_PATH
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        rc.DISCOVERY_PATH = self._orig_path

    def test_valid_json_returned(self):
        path = os.path.join(self._tmpdir, "agent-registry.json")
        data = {"url": "http://127.0.0.1:7847"}
        with open(path, "w") as f:
            json.dump(data, f)
        rc.DISCOVERY_PATH = path
        result = rc.read_discovery()
        self.assertEqual(result["url"], "http://127.0.0.1:7847")

    def test_missing_file_returns_none(self):
        rc.DISCOVERY_PATH = os.path.join(self._tmpdir, "nonexistent.json")
        result = rc.read_discovery()
        self.assertIsNone(result)

    def test_malformed_json_returns_none(self):
        path = os.path.join(self._tmpdir, "bad.json")
        with open(path, "w") as f:
            f.write("{not valid json}")
        rc.DISCOVERY_PATH = path
        result = rc.read_discovery()
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
