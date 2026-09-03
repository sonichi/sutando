#!/usr/bin/env python3
"""pool-lead-daemon composition root: main() sweeps assign real tasks,
prunes, runs recovery, autoscales through the ledger, stamps and unlinks
the beat file, and the subprocess helpers survive transport failure.

Run: python3 tests/pool-lead-daemon.test.py   (stdlib only)
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "pool_lead_daemon", REPO / "scripts" / "pool-lead-daemon.py")
daemon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(daemon)


class _FakeCompleted:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


class HelperTests(unittest.TestCase):
    def test_run_recovery_returns_stdout_and_survives_oserror(self):
        real = daemon.subprocess
        daemon.subprocess = types.SimpleNamespace(
            run=lambda *a, **k: _FakeCompleted(0, "worker-1 ok\n"),
            TimeoutExpired=real.TimeoutExpired)
        try:
            self.assertEqual(daemon._run_recovery(), "worker-1 ok")
            def boom(*a, **k):
                raise OSError("no bash")
            daemon.subprocess = types.SimpleNamespace(
                run=boom, TimeoutExpired=real.TimeoutExpired)
            self.assertIn("recovery sweep failed", daemon._run_recovery())
        finally:
            daemon.subprocess = real

    def test_send_notice_maps_rc_and_failure(self):
        real = daemon.subprocess
        seen = {}
        def fake_run(cmd, **k):
            seen["cmd"] = cmd
            return _FakeCompleted(0)
        daemon.subprocess = types.SimpleNamespace(
            run=fake_run, TimeoutExpired=real.TimeoutExpired)
        try:
            self.assertTrue(daemon._send_notice("telegram", "42", "hi"))
            self.assertIn("--chat-id", seen["cmd"])
            self.assertTrue(daemon._send_notice("discord", "c", "hi"))
            def boom(*a, **k):
                raise OSError("gone")
            daemon.subprocess = types.SimpleNamespace(
                run=boom, TimeoutExpired=real.TimeoutExpired)
            self.assertFalse(daemon._send_notice("discord", "c", "hi"))
        finally:
            daemon.subprocess = real


class MainLoopTests(unittest.TestCase):
    def test_two_sweeps_assign_scale_and_shut_down_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            tasks, cores = ws / "tasks", ws / "state" / "cores"
            tasks.mkdir()
            cores.mkdir(parents=True)
            (cores / "worker-1.alive").write_text("beat")
            (tasks / "task-d1.txt").write_text(
                "id: task-d1\nsource: chat\ntask: t\n")

            real_ws, real_rec, real_send = (
                daemon._workspace, daemon._run_recovery, daemon._send_notice)
            real_sub, real_time, real_argv = (
                daemon.subprocess, daemon.time, sys.argv)
            installs = []

            def fake_sub_run(cmd, **k):
                installs.append([str(c) for c in cmd])
                return _FakeCompleted(0, "staged worker-2\n")

            sleeps = {"n": 0}

            def fake_sleep(_s):
                sleeps["n"] += 1
                if sleeps["n"] >= 2:
                    signal.raise_signal(signal.SIGTERM)

            daemon._workspace = lambda: ws
            daemon._run_recovery = lambda: "worker-1 healthy\nkickstart worker-2"
            daemon._send_notice = lambda *a: True
            daemon.subprocess = types.SimpleNamespace(
                run=fake_sub_run, TimeoutExpired=real_sub.TimeoutExpired)
            daemon.time = types.SimpleNamespace(
                time=real_time.time, sleep=fake_sleep)
            # saturate autoscale: force a grow decision every pass
            real_decide = daemon.scale_decide
            daemon.scale_decide = (
                lambda *a, **k: 2)
            sys.argv = ["pool-lead-daemon.py", "--interval", "0.01",
                        "--pool-max", "2"]
            out = io.StringIO()
            try:
                with redirect_stdout(out):
                    rc = daemon.main()
            finally:
                daemon._workspace, daemon._run_recovery = real_ws, real_rec
                daemon._send_notice, daemon.subprocess = real_send, real_sub
                daemon.time, sys.argv = real_time, real_argv
                daemon.scale_decide = real_decide
            log = out.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("assigned task-d1.txt -> worker-1", log)
            self.assertIn("recovery: kickstart worker-2", log)
            self.assertIn("scaled-up pool 1 -> 2", log)
            self.assertIn("pool-lead: stopped", log)
            self.assertTrue(
                any("install-worker-pool.sh" in " ".join(c) for c in installs))
            self.assertFalse((cores / "pool-lead.alive").exists(),
                             "beat must be unlinked on clean shutdown")
            self.assertTrue(
                (tasks / "task-d1.assigned-worker-1.txt").exists())

    def test_runtime_of_reads_the_workers_plist_under_home(self):
        """A codex plist under $HOME/Library/LaunchAgents must steer owner
        work to the claude seat; unread, both seats look claude and the
        lane rule sends it to worker-1."""
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            home = Path(td) / "home"
            tasks, cores = ws / "tasks", ws / "state" / "cores"
            tasks.mkdir(parents=True)
            cores.mkdir(parents=True)
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            (agents / "com.sutando.worker-1.plist").write_text(
                "<plist><dict><key>POOL_RUNTIME</key><string>codex</string>"
                "</dict></plist>")
            for w in ("worker-1", "worker-2"):
                (cores / f"{w}.alive").write_text("beat")
            (tasks / "task-r1.txt").write_text(
                "id: task-r1\nsource: chat\ntask: t\n")

            real_ws, real_rec, real_send = (
                daemon._workspace, daemon._run_recovery, daemon._send_notice)
            real_time, real_argv = daemon.time, sys.argv
            real_home = os.environ.get("HOME")

            def fake_sleep(_s):
                signal.raise_signal(signal.SIGTERM)

            daemon._workspace = lambda: ws
            daemon._run_recovery = lambda: ""
            daemon._send_notice = lambda *a: True
            daemon.time = types.SimpleNamespace(
                time=real_time.time, sleep=fake_sleep)
            sys.argv = ["pool-lead-daemon.py", "--interval", "0.01"]
            os.environ["HOME"] = str(home)
            out = io.StringIO()
            try:
                with redirect_stdout(out):
                    rc = daemon.main()
            finally:
                daemon._workspace, daemon._run_recovery = real_ws, real_rec
                daemon._send_notice, daemon.time = real_send, real_time
                sys.argv = real_argv
                os.environ["HOME"] = real_home
            self.assertEqual(rc, 0)
            self.assertIn("assigned task-r1.txt -> worker-2", out.getvalue())
            self.assertTrue(
                (tasks / "task-r1.assigned-worker-2.txt").exists())

    def test_scale_up_failure_is_reported_not_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "tasks").mkdir()
            cores = ws / "state" / "cores"
            cores.mkdir(parents=True)
            (cores / "worker-1.alive").write_text("beat")

            real_ws, real_rec = daemon._workspace, daemon._run_recovery
            real_sub, real_time, real_argv = (
                daemon.subprocess, daemon.time, sys.argv)
            real_decide = daemon.scale_decide

            def fake_sleep(_s):
                signal.raise_signal(signal.SIGTERM)

            daemon._workspace = lambda: ws
            daemon._run_recovery = lambda: "worker-1 healthy"
            daemon.subprocess = types.SimpleNamespace(
                run=lambda *a, **k: _FakeCompleted(3, "", "boom"),
                TimeoutExpired=real_sub.TimeoutExpired)
            daemon.time = types.SimpleNamespace(
                time=real_time.time, sleep=fake_sleep)
            daemon.scale_decide = lambda *a, **k: 2
            sys.argv = ["pool-lead-daemon.py", "--interval", "0.01"]
            out = io.StringIO()
            try:
                with redirect_stdout(out):
                    rc = daemon.main()
            finally:
                daemon._workspace, daemon._run_recovery = real_ws, real_rec
                daemon.subprocess, daemon.time = real_sub, real_time
                sys.argv, daemon.scale_decide = real_argv, real_decide
            self.assertEqual(rc, 0)
            self.assertIn("scale-up failed rc=3", out.getvalue())
            self.assertIn("recovery: ok", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=1)
