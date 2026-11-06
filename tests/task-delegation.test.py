#!/usr/bin/env python3
"""src/task_delegation.py: the background-delegation marker's writer and reader.

Run: python3 tests/task-delegation.test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import task_delegation as m  # noqa: E402

TID = "task-cron-deal-sweep-1792841385408"


class Marker(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_mark_then_live_and_no_temp_left(self):
        p = m.mark(self.ws, TID, 600)
        self.assertTrue(m.is_delegated(self.ws, TID))
        d = json.loads(p.read_text())
        self.assertEqual((d["task_id"], d["task_file"], d["ttl_s"]), (TID, f"tasks/{TID}.txt", 600.0))
        self.assertEqual([x.name for x in p.parent.iterdir()], [p.name])

    def test_expired_is_not_delegated(self):
        m.mark(self.ws, TID, 60, now=time.time() - 61)
        self.assertFalse(m.is_delegated(self.ws, TID))

    def test_future_dated_is_not_delegated(self):
        m.mark(self.ws, TID, 600, now=time.time() + 3600)
        self.assertFalse(m.is_delegated(self.ws, TID))

    def test_absent_is_not_delegated(self):
        self.assertFalse(m.is_delegated(self.ws, TID))

    def test_malformed_markers_are_not_delegated(self):
        p = m.marker_path(self.ws, TID)
        p.parent.mkdir(parents=True)
        now = time.time()
        for body in ("not json", "[]", "{}",
                     json.dumps({"task_id": "other", "delegated_at": now, "ttl_s": 600}),
                     json.dumps({"task_id": TID, "delegated_at": "now", "ttl_s": 600}),
                     json.dumps({"task_id": TID, "delegated_at": now, "ttl_s": True}),
                     json.dumps({"task_id": TID, "delegated_at": now, "ttl_s": 10 ** 9}),
                     '{"task_id": "%s", "delegated_at": NaN, "ttl_s": 600}' % TID,
                     json.dumps({"task_id": TID, "delegated_at": now, "ttl_s": 600, "pad": "x" * 5000})):
            p.write_text(body)
            self.assertFalse(m.is_delegated(self.ws, TID), body[:80])

    def test_bounds_are_refused_before_any_write(self):
        for ttl in (0, -1, m.MAX_TTL_S + 1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                m.mark(self.ws, TID, ttl)
        for bad in ("../x", "a/b", "..", ""):
            with self.assertRaises(ValueError):
                m.mark(self.ws, bad, 60)
        self.assertFalse((self.ws / m.SUBDIR).exists())

    def test_clear_removes_it(self):
        m.mark(self.ws, TID, 600)
        self.assertTrue(m.clear(self.ws, TID))
        self.assertFalse(m.is_delegated(self.ws, TID))
        self.assertFalse(m.clear(self.ws, TID))

    def test_long_expired_markers_are_pruned_on_write(self):
        old = m.mark(self.ws, "task-old-1", 60)
        t = time.time() - m.MAX_TTL_S - 86400 - 10
        os.utime(old, (t, t))
        m.mark(self.ws, TID, 60)
        self.assertFalse(old.exists())

    def test_cli_rcs(self):
        def run(*a):
            return subprocess.run([sys.executable, str(REPO / "src" / "task_delegation.py"), *a,
                                   "--workspace", str(self.ws)], capture_output=True, text=True).returncode
        task_file = f"/somewhere/tasks/{TID}.txt"
        self.assertEqual(run("check", task_file), 1)
        self.assertEqual(run("mark", task_file, "--ttl", "1800"), 0)
        self.assertEqual(run("check", TID), 0)
        self.assertEqual(run("mark", TID, "--ttl", "0"), 2)
        self.assertEqual(run("clear", TID), 0)
        self.assertEqual(run("check", TID), 1)

    def test_unwritable_store_fails_with_rc_1(self):
        (self.ws / "state").mkdir()
        (self.ws / "state" / "task-delegations").write_text("a file where the dir goes")
        r = subprocess.run([sys.executable, str(REPO / "src" / "task_delegation.py"), "mark", TID,
                            "--workspace", str(self.ws)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("keeps blocking", r.stderr)


if __name__ == "__main__":
    unittest.main()
