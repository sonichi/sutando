#!/usr/bin/env python3
"""health-check `sleep-guard`: detect and, with --fix, restart a missing caffeinate -s holder on macOS.

Startup alone starting the holder meant a holder that died later went unnoticed while the Mac slept.
ps is a fixture string (or None for "ps could not run"); spawns are stubs or short real children.

Run: python3 tests/health-check-sleep-guard.test.py
"""
from __future__ import annotations

import tempfile

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(CLAUDE_CONFIG_DIR=tempfile.mkdtemp(prefix="sleepguard-ccd-"))

import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import sleep_guard as sg  # noqa: E402
import sutando_config  # noqa: E402

_REAL_IS_ENABLED = sg.is_enabled


def setUpModule():
    # A host's own config must not decide the default-path tests; the switch is tested explicitly.
    sg.is_enabled = lambda: True


def tearDownModule():
    sg.is_enabled = _REAL_IS_ENABLED

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

    def poll(self):
        return None


class SpawnStub:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return FakeProc()


class Pattern(unittest.TestCase):
    def test_self_ending_caffeinate_does_not_match(self):
        for args in ("caffeinate -s -t 60", "caffeinate -s -w 99", "caffeinate -i -s make build",
                     "caffeinate make -s"):
            self.assertFalse(sg.holds(args), args)

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
        snaps = iter([SHORT_ONLY, WITH_HOLDER])
        pid = self.hc.apply_sleep_guard_fix([row], stream=out, spawn=spawn, snapshot=lambda: next(snaps))
        self.assertEqual(pid, 4242)
        self.assertEqual(len(spawn.calls), 1)
        argv, kw = spawn.calls[0]
        self.assertEqual(argv, ["caffeinate", "-d", "-i", "-s"])
        self.assertTrue(kw["start_new_session"])
        for stream in ("stdin", "stdout", "stderr"):
            self.assertEqual(os.path.basename(kw[stream].name), os.path.basename(os.devnull), stream)
        self.assertEqual(row["status"], "ok")
        self.assertIn("4242", out.getvalue())

    def test_fix_does_nothing_when_ok_or_unknown(self):
        for snap in (WITH_HOLDER, None):
            row = self.hc.check_sleep_guard(snapshot=lambda s=snap: s, platform="darwin")
            spawn = SpawnStub()
            self.assertIsNone(self.hc.apply_sleep_guard_fix([row], stream=io.StringIO(), spawn=spawn,
                                                            snapshot=lambda s=snap: s))
            self.assertEqual(spawn.calls, [])

    def test_fix_rechecks_before_starting(self):
        row = self.hc.check_sleep_guard(snapshot=lambda: SHORT_ONLY, platform="darwin")
        for later in (WITH_HOLDER, None):
            spawn = SpawnStub()
            self.assertIsNone(self.hc.apply_sleep_guard_fix([row], stream=io.StringIO(), spawn=spawn,
                                                            snapshot=lambda s=later: s))
            self.assertEqual(spawn.calls, [], "a holder that appeared, or an unreadable table, starts nothing")
            self.assertEqual(row["status"], "warn")

    def test_fix_keeps_warn_when_child_exits(self):
        """A real child that exits non-zero right away, as `caffeinate` missing behind a wrapper did."""
        row = self.hc.check_sleep_guard(snapshot=lambda: SHORT_ONLY, platform="darwin")

        def dying(argv, **kw):
            return subprocess.Popen([sys.executable, "-c", "raise SystemExit(127)"], **kw)
        out = io.StringIO()
        self.assertIsNone(self.hc.apply_sleep_guard_fix([row], stream=out, spawn=dying,
                                                        snapshot=lambda: SHORT_ONLY))
        self.assertEqual(row["status"], "warn")
        self.assertIn("127", out.getvalue())

    def test_fix_keeps_warn_when_holder_not_visible(self):
        row = self.hc.check_sleep_guard(snapshot=lambda: SHORT_ONLY, platform="darwin")
        out = io.StringIO()
        self.assertIsNone(self.hc.apply_sleep_guard_fix([row], stream=out, spawn=SpawnStub(),
                                                        snapshot=lambda: SHORT_ONLY))
        self.assertEqual(row["status"], "warn")
        self.assertIn("no holder is visible", out.getvalue())

    def test_fix_reports_spawn_failure(self):
        row = self.hc.check_sleep_guard(snapshot=lambda: SHORT_ONLY, platform="darwin")

        def boom(argv, **kw):
            raise FileNotFoundError("caffeinate")
        out = io.StringIO()
        self.assertIsNone(self.hc.apply_sleep_guard_fix([row], stream=out, spawn=boom, snapshot=lambda: SHORT_ONLY))
        self.assertEqual(row["status"], "warn")
        self.assertIn("could not start", out.getvalue())


class Ensure(unittest.TestCase):
    def test_starts_only_when_absent(self):
        spawn = SpawnStub()
        self.assertEqual(sg.ensure(lambda: WITH_HOLDER, platform="darwin", spawn=spawn)[0], True)
        self.assertEqual(spawn.calls, [])
        ok, msg = sg.ensure(lambda: SHORT_ONLY, platform="darwin", spawn=spawn)
        self.assertTrue(ok)
        self.assertIn("started", msg)
        self.assertEqual(len(spawn.calls), 1)

    def test_unreadable_table_starts_nothing(self):
        spawn = SpawnStub()
        ok, msg = sg.ensure(lambda: None, platform="darwin", spawn=spawn)
        self.assertFalse(ok)
        self.assertIn("unknown", msg)
        self.assertEqual(spawn.calls, [])

    def test_failed_child_is_not_reported_started(self):
        def dying(argv, **kw):
            return subprocess.Popen([sys.executable, "-c", "raise SystemExit(127)"], **kw)
        ok, msg = sg.ensure(lambda: SHORT_ONLY, platform="darwin", spawn=dying)
        self.assertFalse(ok)
        self.assertIn("did not start", msg)

    def test_missing_binary_is_not_reported_started(self):
        def missing(argv, **kw):
            return subprocess.Popen(["/nonexistent/caffeinate", *argv[1:]], **kw)
        ok, msg = sg.ensure(lambda: SHORT_ONLY, platform="darwin", spawn=missing)
        self.assertFalse(ok)
        self.assertNotIn("started (pid", msg)

    def test_skips_off_macos(self):
        spawn = SpawnStub()
        self.assertTrue(sg.ensure(lambda: "", platform="linux", spawn=spawn)[0])
        self.assertEqual(spawn.calls, [])


class Disabled(unittest.TestCase):
    """`sleep_guard.enabled: false`: nothing is started, the row is ok, a running holder is left alone."""
    hc = load_hc()

    def test_check_row_is_ok_and_names_key(self):
        for snap in (SHORT_ONLY, WITH_HOLDER, None):
            row = sg.check(snap, platform="darwin", enabled=False)
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["detail"], "disabled by config (sleep_guard.enabled)")

    def test_health_row_skips_ps_when_off(self):
        called = []
        row = self.hc.check_sleep_guard(snapshot=lambda: called.append(1), platform="darwin", enabled=False)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(called, [])

    def test_health_row_reads_switch_by_default(self):
        with unittest.mock.patch.object(sg, "is_enabled", return_value=False):
            row = self.hc.check_sleep_guard(snapshot=lambda: SHORT_ONLY, platform="darwin")
        self.assertEqual(row["status"], "ok")

    def test_ensure_starts_nothing(self):
        spawn, snaps = SpawnStub(), []
        ok, msg = sg.ensure(lambda: snaps.append(1), platform="darwin", spawn=spawn, enabled=False)
        self.assertTrue(ok)
        self.assertIn("off by config (sleep_guard.enabled)", msg)
        self.assertEqual((spawn.calls, snaps), ([], []))

    def test_fix_starts_nothing_even_on_a_warn_row(self):
        row = self.hc.check_sleep_guard(snapshot=lambda: SHORT_ONLY, platform="darwin", enabled=True)
        spawn = SpawnStub()
        self.assertIsNone(self.hc.apply_sleep_guard_fix([row], stream=io.StringIO(), spawn=spawn,
                                                        snapshot=lambda: SHORT_ONLY, enabled=False))
        self.assertEqual(spawn.calls, [])

    def test_warn_row_names_the_off_switch(self):
        row = sg.check(SHORT_ONLY, platform="darwin", enabled=True)
        self.assertEqual(row["status"], "warn")
        self.assertIn('"sleep_guard": {"enabled": false}', row["detail"])

    def test_is_enabled_delegates_to_config_and_fails_on(self):
        with unittest.mock.patch.object(sutando_config, "resolve_sleep_guard_enabled", return_value=False):
            self.assertFalse(_REAL_IS_ENABLED())

        def boom():
            raise RuntimeError("bad config")
        with unittest.mock.patch.object(sutando_config, "resolve_sleep_guard_enabled", boom):
            self.assertTrue(_REAL_IS_ENABLED())


class ConfigKey(unittest.TestCase):
    """`sleep_guard.enabled` as the canonical loader reads it from a tmp checkout."""

    def resolve(self, cfg):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "sutando.config.json").write_text(json.dumps(cfg))
            sutando_config._reset_cache_for_tests()
            try:
                with unittest.mock.patch("sys.stderr", io.StringIO()):
                    return sutando_config.resolve_sleep_guard_enabled(repo_root=root)
            finally:
                sutando_config._reset_cache_for_tests()

    def test_default_is_on(self):
        self.assertTrue(self.resolve({}))
        self.assertTrue(self.resolve({"sleep_guard": {}}))

    def test_only_json_false_turns_it_off(self):
        self.assertFalse(self.resolve({"sleep_guard": {"enabled": False}}))
        self.assertTrue(self.resolve({"sleep_guard": {"enabled": True}}))
        for junk in ("false", 0, None):
            self.assertTrue(self.resolve({"sleep_guard": {"enabled": junk}}), junk)


class Cli(unittest.TestCase):
    """`sleep_guard.py ensure` is what startup.sh runs; its exit code picks the tick or the warning."""

    def run_main(self, argv, result=None):
        real, out = sg.ensure, io.StringIO()
        if result is not None:
            sg.ensure = lambda snapshot: result
        try:
            with unittest.mock.patch("sys.stdout", out), unittest.mock.patch("sys.stderr", io.StringIO()):
                return sg.main(argv), out.getvalue()
        finally:
            sg.ensure = real

    def test_exit_code_follows_ensure(self):
        self.assertEqual(self.run_main(["sleep_guard.py", "ensure"], (True, "caffeinate -s already running")),
                         (0, "caffeinate -s already running\n"))
        self.assertEqual(self.run_main(["sleep_guard.py", "ensure"], (False, "caffeinate did not start (x)"))[0], 1)

    def test_bad_usage(self):
        self.assertEqual(self.run_main(["sleep_guard.py"])[0], 2)
        self.assertEqual(self.run_main(["sleep_guard.py", "start"])[0], 2)


if __name__ == "__main__":
    unittest.main()
