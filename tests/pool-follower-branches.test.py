#!/usr/bin/env python3
"""pool_follower IO branches: raced claims, unreadable dirs, source
sniffing, and the finish CLI's dispatch + refusal paths.

Run: python3 tests/pool-follower-branches.test.py   (stdlib only)
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import pool_follower  # noqa: E402


class ClaimRaceTests(unittest.TestCase):
    def test_raced_assignment_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            tasks = Path(td)
            ghost = tasks / "task-x.assigned-core-1.txt"  # never created
            self.assertIsNone(
                pool_follower._claim_assignment(tasks, ghost, "core-1"))

    def test_missing_tasks_dir_is_idle(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state"
            state.mkdir()
            got = pool_follower.acquire_work(
                Path(td) / "no-such-tasks", state, "core-1", "pool-lead")
            self.assertIsNone(got)

    def test_live_lead_blocks_unassigned_pool(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "tasks").mkdir()
            (ws / "state" / "cores").mkdir(parents=True)
            (ws / "tasks" / "task-q1.txt").write_text("x")
            import json
            import time
            (ws / "state" / "cores" / "pool-lead.alive").write_text(
                json.dumps({"ts": time.time()}))
            got = pool_follower.acquire_work(
                ws / "tasks", ws / "state", "core-1", "pool-lead")
            self.assertIsNone(got)
            self.assertTrue((ws / "tasks" / "task-q1.txt").exists())

    def test_dead_lead_falls_back_to_unassigned(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "tasks").mkdir()
            (ws / "state" / "cores").mkdir(parents=True)
            (ws / "tasks" / "task-q2.txt").write_text("x")
            got = pool_follower.acquire_work(
                ws / "tasks", ws / "state", "core-1", "pool-lead")
            self.assertIsNotNone(got)
            self.assertTrue(got.name.endswith(".claimed-core-1.txt"))


class SourceSniffTests(unittest.TestCase):
    def test_source_header_is_read(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "task-s.claimed-core-1.txt"
            f.write_text("id: s\nsource: discord\ntask: hi\n")
            self.assertEqual(pool_follower._source_of(f), "discord")

    def test_blank_line_ends_the_header_scan(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "task-s2.claimed-core-1.txt"
            f.write_text("id: s2\n\nsource: too-late\n")
            self.assertEqual(pool_follower._source_of(f), "")

    def test_unreadable_file_reads_as_empty(self):
        self.assertEqual(
            pool_follower._source_of(Path("/nonexistent/task-x.txt")), "")


class FinishCliTests(unittest.TestCase):
    def _ws(self, td):
        ws = Path(td)
        for d in ("tasks", "results", "state", "data"):
            (ws / d).mkdir()
        return ws

    def test_wrong_argc_is_usage_error(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = pool_follower._finish_cli(["only-one-arg"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err.getvalue())

    def test_finish_via_cli_writes_result_and_archives(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            claimed = ws / "tasks" / "task-f1.claimed-core-1.txt"
            claimed.write_text("id: task-f1\nsource: chat\ntask: t\n")
            out = io.StringIO()
            old_stdin = sys.stdin
            sys.stdin = io.StringIO("task: f1\nall done\n")
            try:
                with redirect_stdout(out):
                    rc = pool_follower._finish_cli([str(claimed), "core-1"])
            finally:
                sys.stdin = old_stdin
            self.assertEqual(rc, 0)
            self.assertEqual((ws / "results" / "task-f1.txt").read_text(),
                             "all done\n")
            self.assertTrue((ws / "tasks" / "archive" / "task-f1.txt").exists())

    def test_mismatched_pairing_line_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._ws(td)
            claimed = ws / "tasks" / "task-f2.claimed-core-1.txt"
            claimed.write_text("id: task-f2\ntask: t\n")
            err = io.StringIO()
            old_stdin = sys.stdin
            sys.stdin = io.StringIO("task: OTHER\nbody\n")
            try:
                with redirect_stderr(err):
                    rc = pool_follower._finish_cli([str(claimed), "core-1"])
            finally:
                sys.stdin = old_stdin
            self.assertEqual(rc, 2)
            self.assertIn("refused", err.getvalue())
            self.assertTrue(claimed.exists(), "refusal must not consume the claim")


if __name__ == "__main__":
    unittest.main(verbosity=1)
