#!/usr/bin/env python3
"""Tests for instance-scoped runtime resources (isolation spec V1).

Contract: each instance gets its own run dir, socket and lock so two
instances can never collide; the default instance keeps honoring a
pre-existing legacy flat socket; SUTANDO_RUNTIME_SOCKET still overrides.

Run: python3 tests/runtime-api-rundir-instances.test.py
Exit: 0 on pass, 1 on fail.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

import rundir  # noqa: E402


class RundirInstanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SUTANDO_RUN_DIR"] = self.tmp.name
        for k in ("SUTANDO_RUNTIME_SOCKET", "SUTANDO_INSTANCE_ID"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k in ("SUTANDO_RUN_DIR", "SUTANDO_RUNTIME_SOCKET",
                  "SUTANDO_INSTANCE_ID"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_instances_get_disjoint_sockets_and_locks(self):
        a = rundir.socket_path("qingyun-001")
        b = rundir.socket_path("research-001")
        self.assertNotEqual(a, b)
        self.assertIn("/qingyun-001/", a)
        self.assertNotEqual(rundir.lock_path("qingyun-001"),
                            rundir.lock_path("research-001"))

    def test_env_instance_id_scopes_the_default(self):
        os.environ["SUTANDO_INSTANCE_ID"] = "research-001"
        self.assertIn("/research-001/", rundir.socket_path())

    def test_default_honors_preexisting_legacy_flat_socket(self):
        legacy = Path(self.tmp.name) / "sutando-runtime.sock"
        self.assertNotEqual(rundir.socket_path(), str(legacy))  # absent -> scoped
        legacy.touch()
        self.assertEqual(rundir.socket_path(), str(legacy))     # present -> honored
        # non-default instances NEVER fall back to the shared legacy socket
        self.assertNotEqual(rundir.socket_path("research-001"), str(legacy))

    def test_explicit_env_socket_still_wins(self):
        os.environ["SUTANDO_RUNTIME_SOCKET"] = "/x/y.sock"
        self.assertEqual(rundir.socket_path("anything"), "/x/y.sock")


if __name__ == "__main__":
    unittest.main(verbosity=2)
