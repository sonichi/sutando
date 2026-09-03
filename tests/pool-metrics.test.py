#!/usr/bin/env python3
"""PoolMetrics (L4): append-only recording, fail-open on IO error, and a
summary that actually computes the quality-bar quantities.

Run: python3 tests/pool-metrics.test.py   (stdlib only)
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

import pool_metrics  # noqa: E402
from pool_metrics import PoolMetrics  # noqa: E402


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = PoolMetrics(self.tmp.name, now_fn=lambda: 1_700_000_000.0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_summary_computes_quality_bar_quantities(self):
        self.m.assigned("task-1", "a", "C1", wait_s=1.0)
        self.m.assigned("task-2", "a", "C1", wait_s=3.0)
        self.m.assigned("task-3", "b", None, wait_s=200.0)  # head-of-line
        self.m.claimed("task-4", "b", fallback=True)
        self.m.reclaimed("task-5", "a")
        s = self.m.summarize()
        self.assertEqual(s["assignment_distribution"], {"a": 2, "b": 1})
        self.assertEqual(s["head_of_line_incidents"], 1)
        self.assertEqual(s["fallback_claims"], 1)
        self.assertEqual(s["reclaims"], 1)
        self.assertEqual(s["mean_wait_by_channel"], {"C1": 2.0})

    def test_corrupt_line_counted_not_fatal(self):
        self.m.assigned("task-1", "a", None, wait_s=0.5)
        with open(self.m._path(), "a") as f:
            f.write("{not json\n")
        self.m.assigned("task-2", "b", None, wait_s=0.5)
        s = self.m.summarize()
        self.assertEqual(s["rows"], 2)
        self.assertEqual(s["bad_lines"], 1)

    def test_record_is_fail_open_on_unwritable_dir(self):
        bad = PoolMetrics("/dev/null/nope", now_fn=lambda: 0.0)
        bad.assigned("task-1", "a", None, wait_s=0.1)  # must not raise
        self.assertIn("note", bad.summarize())

    def test_missing_day_reports_note_not_crash(self):
        self.assertIn("note", self.m.summarize(day="1999-01-01"))


    def test_continuity_breaks_count_same_channel_core_switches(self):
        # asymmetric (2 switches, 1 same-core pair) so an inverted
        # comparison cannot yield the same counts
        self.m.assigned("t1", "a", "C1", wait_s=0.0)
        self.m.assigned("t2", "b", "C1", wait_s=0.0)
        self.m.assigned("t3", "a", "C1", wait_s=0.0)
        self.m.assigned("t4", "a", "C1", wait_s=0.0)
        s = self.m.summarize()
        self.assertEqual(s["continuity_breaks"], 2)
        self.assertEqual(s["continuity_pairs"], 3)
        self.assertEqual(s["continuity_breaks_by_channel"], {"C1": 2})

    def test_switch_outside_window_is_not_a_break(self):
        self.m.assigned("t1", "a", "C1", wait_s=0.0)
        far = PoolMetrics(self.tmp.name, now_fn=lambda: 1_700_000_000.0 + 4000)
        far.assigned("t2", "b", "C1", wait_s=0.0)
        s = self.m.summarize()
        self.assertEqual(s["continuity_breaks"], 0)
        self.assertEqual(s["continuity_pairs"], 0)

    def test_cli_prints_the_days_summary_as_json(self):
        self.m.claimed("task-4", "b", fallback=True)
        self.m.reclaimed("task-5", "a", reason="stuck")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = pool_metrics.summarize_cli([self.tmp.name, "2023-11-14"])
        self.assertEqual(rc, 0)
        s = json.loads(out.getvalue())
        self.assertEqual((s["fallback_claims"], s["reclaims"]), (1, 1))

    def test_cli_usage_exits_2(self):
        err = io.StringIO()
        with __import__("contextlib").redirect_stderr(err):
            self.assertEqual(pool_metrics.summarize_cli([]), 2)
        self.assertIn("usage", err.getvalue())


class PoolStatusScriptTest(unittest.TestCase):
    """scripts/pool-status.sh is the summary's consumer: a fake repo with the
    real script, the real resolver and the real module, a stub config."""

    def test_status_view_prints_the_summary(self):
        with tempfile.TemporaryDirectory() as t:
            td = Path(t)
            repo = td / "repo"
            (repo / "scripts").mkdir(parents=True)
            (repo / "src" / "runtime-api").mkdir(parents=True)
            for rel in ("scripts/pool-status.sh", "scripts/python-binary.sh",
                        "src/runtime-api/pool_metrics.py"):
                shutil.copy(REPO / rel, repo / rel)
            cfg = repo / "scripts" / "sutando-config.sh"
            cfg.write_text('#!/bin/bash\n[ "$1" = workspace ] && printf %s "$STUB_WS"\n')
            cfg.chmod(0o755)
            ws = td / "ws"
            (ws / "state").mkdir(parents=True)
            PoolMetrics(ws / "state").claimed("task-1", "worker-2", fallback=True)
            r = subprocess.run(["bash", str(repo / "scripts" / "pool-status.sh")],
                               env=dict(os.environ, STUB_WS=str(ws)),
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("6. lead metrics", r.stdout)
            self.assertIn('"fallback_claims": 1', r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
