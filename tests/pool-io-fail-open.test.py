#!/usr/bin/env python3
"""Pool modules fail OPEN on filesystem trouble: a missing or unreadable
dir yields empty results and a blocked write is swallowed — the sweep must
never die on IO. One concern, every module's guard exercised.

Run: python3 tests/pool-io-fail-open.test.py   (stdlib only)
"""
from __future__ import annotations

# flake8: noqa: E402 — imports follow the sys.path bootstrap

import importlib.util
import io
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

import pool_follower
from pool_lead import PoolLead, _read_channel
from pool_notify import PoolNotifier
from pool_status import PoolStatusWriter

GONE = Path("/nonexistent-pool-io-fail-open")


class LeadScanGuards(unittest.TestCase):
    def _lead(self):
        return PoolLead(GONE / "tasks", GONE / "state",
                        followers_fn=lambda: ["worker-1"],
                        alive_fn=lambda _i: True)

    def test_every_scan_is_empty_not_fatal_on_missing_dirs(self):
        lead = self._lead()
        self.assertEqual(lead.sweep(), [])
        self.assertEqual(lead.reclaim_dead(), [])
        self.assertEqual(lead.reclaim_claimed(), [])
        self.assertEqual(lead.reclaim_stuck_assignments(), [])
        self.assertEqual(lead.prune_done_flags(), 0)
        self.assertEqual(lead._load("worker-1"), 0)

    def test_read_channel_unreadable_task_is_none(self):
        self.assertIsNone(_read_channel(GONE / "task-x.txt"))

    def test_trace_write_is_swallowed_when_blocked(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "tasks").mkdir()
            (ws / "state").mkdir()
            lead = PoolLead(ws / "tasks", ws / "state",
                            followers_fn=lambda: [], alive_fn=lambda _i: False)
            # the trace's parent dir is a FILE: every append must fail-open
            (ws / "state" / "pool").write_text("blocks the dir")
            lead._trace({"event": "test"})  # must not raise


class NotifierGuards(unittest.TestCase):
    def test_missing_tasks_dir_yields_no_stalls(self):
        n = PoolNotifier(GONE / "tasks", GONE / "state", lambda *a: True)
        self.assertEqual(n.check_stalls(), [])

    def test_broken_sender_is_false_not_fatal(self):
        def boom(*_a):
            raise RuntimeError("transport gone")
        n = PoolNotifier(GONE / "tasks", GONE / "state", boom)
        self.assertFalse(n._try_send("discord", "c", "m"))

    def test_blocked_ledger_write_is_swallowed(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "tasks").mkdir()
            (ws / "state").mkdir()
            (ws / "state" / "pool").write_text("blocks the dir")
            n = PoolNotifier(ws / "tasks", ws / "state", lambda *a: True)
            n._save({"seen": {}})  # must not raise


class StatusWriterGuards(unittest.TestCase):
    def test_missing_tasks_dir_counts_nothing(self):
        w = PoolStatusWriter(GONE / "tasks", GONE / "state",
                             followers_fn=lambda: [], alive_fn=lambda _i: False)
        self.assertEqual(w._in_flight(), {})


class FollowerFallbackGuards(unittest.TestCase):
    def test_raced_fallback_rename_moves_to_next_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "tasks").mkdir()
            (ws / "state").mkdir()
            (ws / "tasks" / "task-a.txt").write_text("x")
            (ws / "tasks" / "task-b.txt").write_text("x")
            real_rename = pool_follower.os.rename
            calls = {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("raced")
                return real_rename(src, dst)

            pool_follower.os.rename = flaky
            try:
                got = pool_follower.acquire_work(
                    ws / "tasks", ws / "state", "worker-1", "pool-lead")
            finally:
                pool_follower.os.rename = real_rename
            self.assertIsNotNone(got, "second candidate must still claim")
            self.assertEqual(calls["n"], 2)


class BindCliUsage(unittest.TestCase):
    def test_unpin_without_channel_is_usage_error(self):
        spec = importlib.util.spec_from_file_location(
            "pool_bind_usage", REPO / "scripts" / "pool-bind.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "tasks").mkdir()
            (ws / "state").mkdir()
            err = io.StringIO()
            with redirect_stderr(err):
                rc = mod.main(["unpin"], workspace=ws)
            self.assertEqual(rc, 2)
            self.assertIn("usage", err.getvalue())


class DaemonWorkspaceResolution(unittest.TestCase):
    def test_workspace_helper_parses_config_stdout(self):
        spec = importlib.util.spec_from_file_location(
            "pool_lead_daemon_ws", REPO / "scripts" / "pool-lead-daemon.py")
        daemon = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(daemon)
        real = daemon.subprocess
        daemon.subprocess = types.SimpleNamespace(
            run=lambda *a, **k: types.SimpleNamespace(stdout="/tmp/ws-x\n"),
            TimeoutExpired=real.TimeoutExpired)
        try:
            self.assertEqual(daemon._workspace(), Path("/tmp/ws-x"))
        finally:
            daemon.subprocess = real


if __name__ == "__main__":
    unittest.main(verbosity=1)
