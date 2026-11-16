#!/usr/bin/env python3
"""health-check `sleep-guard`: detect and, with --fix, restart a missing caffeinate -s holder on macOS.

Startup alone starting the holder meant a holder that died later went unnoticed while the Mac slept.
Hermetic: ps is a fixture string (or None for "ps could not run") and the spawn is a stub.

Run: python3 tests/health-check-sleep-guard.test.py
"""
from __future__ import annotations

import tempfile

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(CLAUDE_CONFIG_DIR=tempfile.mkdtemp(prefix="sleepguard-ccd-"))

import importlib.util
import io
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import sleep_guard as sg  # noqa: E402

HEADER = "  PID  PPID ARGS\n"
WITH_HOLDER = HEADER + "  101     1 /usr/sbin/cfprefsd agent\n  202     1 caffeinate -d -i -s\n"
SHORT_ONLY = HEADER + "  101     1 /usr/sbin/cfprefsd agent\n  303   300 caffeinate -i -t 300\n"


def load_hc():
    spec = importlib.util.spec_from_file_location("health_check_sleep_guard_test", REPO / "src" / "health-check.py")
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class FakeProc:
    pid = 4242


class SpawnStub:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return FakeProc()


class Pattern(unittest.TestCase):
    def test_holders_match(self):
        for args in ("caffeinate -d -i -s", "/usr/bin/caffeinate -s", "caffeinate -dis"):
            self.assertTrue(sg.holds(args), args)

    def test_short_lived_does_not_match(self):
        for args in ("caffeinate -i -t 300", "caffeinate -d", "mycaffeinate -s", "grep caffeinate -s"):
            self.assertFalse(sg.holds(args), args)


class Probe(unittest.TestCase):
    hc = load_hc()

    def test_ok_when_holder_runs(self):
        row = self.hc.check_sleep_guard(snapshot=lambda: WITH_HOLDER, platform="darwin")
        self.assertEqual(row["status"], "ok")

    def test_warn_names_fix_when_only_short_lived(self):
        row = self.hc.check_sleep_guard(snapshot=lambda: SHORT_ONLY, platform="darwin")
        self.assertEqual(row["status"], "warn")
        self.assertIn("--fix", row["detail"])

    def test_unknown_when_ps_unavailable(self):
        row = self.hc.check_sleep_guard(snapshot=lambda: None, platform="darwin")
        self.assertEqual(row["status"], "unknown")

    def test_skipped_off_macos(self):
        called = []
        row = self.hc.check_sleep_guard(snapshot=lambda: called.append(1), platform="linux")
        self.assertIsNone(row)
        self.assertEqual(called, [], "ps must not run off macOS")


class Fix(unittest.TestCase):
    hc = load_hc()

    def test_fix_spawns_detached_and_rewrites_row(self):
        row = self.hc.check_sleep_guard(snapshot=lambda: SHORT_ONLY, platform="darwin")
        spawn, out = SpawnStub(), io.StringIO()
        pid = self.hc.apply_sleep_guard_fix([row], stream=out, spawn=spawn)
        self.assertEqual(pid, 4242)
        self.assertEqual(len(spawn.calls), 1)
        argv, kw = spawn.calls[0]
        self.assertEqual(argv, ["nohup", "caffeinate", "-d", "-i", "-s"])
        self.assertTrue(kw["start_new_session"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(os.path.basename(kw[stream].name), os.path.basename(os.devnull), stream)
        self.assertEqual(row["status"], "ok")
        self.assertIn("4242", out.getvalue())

    def test_fix_does_nothing_when_ok_or_unknown(self):
        for snap in (WITH_HOLDER, None):
            row = self.hc.check_sleep_guard(snapshot=lambda s=snap: s, platform="darwin")
            spawn = SpawnStub()
            self.assertIsNone(self.hc.apply_sleep_guard_fix([row], stream=io.StringIO(), spawn=spawn))
            self.assertEqual(spawn.calls, [])

    def test_fix_reports_spawn_failure(self):
        row = self.hc.check_sleep_guard(snapshot=lambda: SHORT_ONLY, platform="darwin")

        def boom(argv, **kw):
            raise FileNotFoundError("caffeinate")
        out = io.StringIO()
        self.assertIsNone(self.hc.apply_sleep_guard_fix([row], stream=out, spawn=boom))
        self.assertEqual(row["status"], "warn")
        self.assertIn("could not start", out.getvalue())


class Ensure(unittest.TestCase):
    def test_starts_only_when_absent(self):
        spawn = SpawnStub()
        self.assertIn("already running", sg.ensure(lambda: WITH_HOLDER, platform="darwin", spawn=spawn))
        self.assertEqual(spawn.calls, [])
        self.assertIn("started", sg.ensure(lambda: SHORT_ONLY, platform="darwin", spawn=spawn))
        self.assertEqual(len(spawn.calls), 1)

    def test_skips_off_macos(self):
        spawn = SpawnStub()
        self.assertIn("not macOS", sg.ensure(lambda: "", platform="linux", spawn=spawn))
        self.assertEqual(spawn.calls, [])


if __name__ == "__main__":
    unittest.main()
