#!/usr/bin/env python3
"""check_disk_space: a full volume must be reported, not silently ignored.

Regression for 2026-07-21: the volume hit 100%, task/result writes failed with
ENOSPC, and health-check reported "All systems operational" because no check
looked at free space. A health check that stays green through the failure it
should catch is worse than no check — it actively certifies a broken system.
"""
import importlib.util
import os
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeStat:
    """statvfs_result stand-in: free bytes = f_bavail * f_frsize."""
    def __init__(self, free_gib, fsid=1):
        self.f_frsize = 4096
        self.f_bavail = int(free_gib * (1024 ** 3) / 4096)
        self.f_fsid = fsid


class TestDiskSpace(unittest.TestCase):
    def setUp(self):
        self.hc = _load()
        self._real = os.statvfs

    def tearDown(self):
        os.statvfs = self._real

    def _status(self, free_gib):
        os.statvfs = lambda p: _FakeStat(free_gib)
        return self.hc.check_disk_space()

    def test_a_full_volume_fails(self):
        r = self._status(0.1)
        self.assertEqual(r["status"], "fail", r)
        self.assertIn("ENOSPC", r["detail"])

    def test_b_low_volume_warns(self):
        self.assertEqual(self._status(5.0)["status"], "warn")

    def test_c_healthy_volume_ok(self):
        self.assertEqual(self._status(200.0)["status"], "ok")

    def test_d_boundaries(self):
        self.assertEqual(self._status(self.hc.DISK_FAIL_GIB - 0.01)["status"], "fail")
        self.assertEqual(self._status(self.hc.DISK_FAIL_GIB + 0.01)["status"], "warn")
        self.assertEqual(self._status(self.hc.DISK_WARN_GIB + 0.01)["status"], "ok")

    def test_e_reports_the_worse_of_two_distinct_volumes(self):
        """A roomy /tmp must not mask a full workspace."""
        seen = {"n": 0}
        def fake(_p):
            seen["n"] += 1
            return _FakeStat(500.0, fsid=1) if seen["n"] == 1 else _FakeStat(0.2, fsid=2)
        os.statvfs = fake
        r = self.hc.check_disk_space()
        self.assertEqual(r["status"], "fail", r)

    def test_f_stat_failure_is_reported_not_swallowed(self):
        def boom(_p):
            raise OSError("nope")
        os.statvfs = boom
        self.assertEqual(self.hc.check_disk_space()["status"], "error")

    def test_g_check_is_actually_registered(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_disk_space())", src,
                      "check exists but never runs — the exact 2026-07-21 failure shape")


if __name__ == "__main__":
    unittest.main(verbosity=2)
