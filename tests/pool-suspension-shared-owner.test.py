#!/usr/bin/env python3
"""The `state/pool-suspended` marker has ONE reader, and all three callers delegate to it.

  * CONTRACT — pool_suspension decides whether the pool is suspended and normalises the record.
  * DELEGATION — health_snapshot, health-check and the pool's remedy go through it, each
    keeping its own rendering. The defect this guards is copies drifting, not one being wrong.

Run: python3 tests/pool-suspension-shared-owner.test.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import pool_suspension as susp  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        (self.ws / "state").mkdir()

    def mark(self, text):
        susp.path(self.ws).write_text(text)


class Contract(Base):
    def test_no_marker_is_not_suspended(self):
        self.assertIsNone(susp.read(self.ws))

    def test_the_record_reads_back_as_written(self):
        self.mark(json.dumps({"reason": "app-quit", "at": 5, "stopped": ["a" * 32]}))
        self.assertEqual(susp.read(self.ws), {"reason": "app-quit", "at": 5, "stopped": ["a" * 32]})

    def test_a_record_with_missing_or_bad_fields_is_normalised(self):
        self.mark(json.dumps({"reason": "", "at": True, "stopped": ["x", 3, None]}))
        self.assertEqual(susp.read(self.ws), {"reason": "suspended", "at": None, "stopped": ["x"]})
        self.mark(json.dumps({"at": "yesterday", "stopped": "x"}))
        self.assertEqual(susp.read(self.ws), {"reason": "suspended", "at": None, "stopped": []})

    def test_a_marker_that_is_not_a_record_still_suspends_and_names_no_workers(self):
        self.mark("app-quit 7\n")
        self.assertEqual(susp.read(self.ws), {"reason": "app-quit 7", "at": None, "stopped": []})
        self.mark("[1, 2]")
        self.assertEqual(susp.read(self.ws)["reason"], "[1, 2]")
        self.mark("")
        self.assertEqual(susp.read(self.ws), {"reason": "suspended", "at": None, "stopped": []})

    def test_no_workspace_resolves_the_sanctioned_one(self):
        with mock.patch.object(susp, "resolve_workspace", return_value=self.ws):
            self.assertEqual(susp.path(), self.ws / "state" / "pool-suspended")
            self.assertIsNone(susp.read())

    def test_an_unreadable_marker_raises_for_the_caller_to_judge(self):
        susp.path(self.ws).mkdir()
        with self.assertRaises(OSError):
            susp.read(self.ws)


class Delegation(Base):
    RECORD = {"reason": "from-the-reader", "at": 9, "stopped": []}

    def test_health_snapshot_renders_the_readers_record(self):
        hs = _load("hs_susp", REPO / "src" / "health_snapshot.py")
        with mock.patch.object(hs.pool_suspension, "read", return_value=self.RECORD) as read:
            self.assertEqual(hs.snapshot(self.ws, agent="core")["suspended"], {"reason": "from-the-reader", "at": 9})
        read.assert_called_once_with(self.ws)
        susp.path(self.ws).mkdir()
        self.assertIsNone(hs._suspension(self.ws))

    def test_health_check_renders_the_readers_record(self):
        hc = _load("hc_susp", REPO / "src" / "health-check.py")
        with mock.patch.object(hc, "WORKSPACE_DIR", self.ws), \
                mock.patch.object(hc, "_any_core_alive", return_value=True), \
                mock.patch.object(hc.pool_suspension, "read", return_value=self.RECORD) as read:
            self.assertIn("from-the-reader since 9", hc.check_pool_suspended()["detail"])
        read.assert_called_once_with(self.ws)

    def test_the_pools_remedy_reads_through_it(self):
        rem = _load("rem_susp", REPO / "skills" / "worker-pool" / "scripts" / "pool_remedy.py")
        with mock.patch.object(rem.pool_suspension, "read", return_value=self.RECORD) as read:
            self.assertEqual(rem.suspension(self.ws), "from-the-reader 9")
        read.assert_called_once_with(self.ws)
        self.assertEqual(rem.suspended_path(self.ws), susp.path(self.ws))
        susp.path(self.ws).mkdir()
        self.assertIsNone(rem.suspension(self.ws))


if __name__ == "__main__":
    sys.exit(unittest.main())
