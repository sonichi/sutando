#!/usr/bin/env python3
"""health-check `system-load --fix`: the core's lever to run the installed orphan reaper once per episode.

A normal pass only warns. With --fix, a warning whose action is reap-once runs the reaper with
`--reap --spotlight` at most once per high-load episode; nothing is sent to anyone.

Hermetic: load and `ps` are fixtures, the reaper is a stub script (linked into a temp installed-skills
dir the way skills/install.sh links) that records its argv and signals nothing, and the workspace is a
temp dir.

Run: python3 tests/health-check-system-load-reap.test.py
"""
from __future__ import annotations

import tempfile

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(CLAUDE_CONFIG_DIR=tempfile.mkdtemp(prefix="sysload-ccd-"))

import importlib.util
import io
import json
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import system_load as sl  # noqa: E402

PS = textwrap.dedent("""\
      4211     1  97.0  3-02:11:04 /usr/bin/python3 /private/tmp/claude-501/p/s/scratchpad/spin.py
      4212     1  88.5  3-02:11:03 node /private/tmp/claude-501/p/s/scratchpad/fixture.js
       900     1  12.0    10:00:00 Foo
""")

STUB = textwrap.dedent("""\
    import json, os, sys
    log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.jsonl")
    with open(log, "a") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\\n")
    if "--reap" in sys.argv:
        if MODE == "usage":
            sys.exit(2)
        if MODE == "busy":
            print("process-hygiene: another run holds the lock; nothing done")
            sys.exit(0)
        print("stopping group=4211 pids=4211,4212 age=11000s cwd=/x cmd='spin'")
        print("process-hygiene: mode=reap roots=1 candidates=3 terminated=2 killed=0 aborted=1 spotlight_markers=1")
    else:
        print("would stop group=4211 pids=4211,4212 age=11000s cwd=/x cmd='spin'")
        print("process-hygiene: mode=report roots=1 candidates=2 terminated=0 killed=0 aborted=0 spotlight_markers=0")
""")


def load_hc():
    spec = importlib.util.spec_from_file_location("health_check_system_load_reap_test", REPO / "src" / "health-check.py")
    hc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hc)
    return hc


class Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if argv[0] == "ps":
            return 0, PS
        return sl.run_command(argv, timeout)


class Base(unittest.TestCase):
    hc = None

    @classmethod
    def setUpClass(cls):
        cls.hc = load_hc()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sysload-reap-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.ws = self.tmp / "ws"
        self.installed = self.tmp / "claude-home" / "skills"
        self.installed.mkdir(parents=True)
        p = mock.patch.object(self.hc, "installed_skills_dir", lambda: self.installed)
        p.start()
        self.addCleanup(p.stop)
        self.cfg = {sl.REAP_KEY: "1"}
        p = mock.patch.object(self.hc, "config_get", lambda k, d=None: self.cfg.get(k, d))
        p.start()
        self.addCleanup(p.stop)
        self.t = 1000.0
        self.log = io.StringIO()
        self.script = None

    def tearDown(self):
        self.assertFalse((self.ws / "results").exists(), "health-check must never queue a message")

    def install(self, mode="ok"):
        d = self.tmp / "shipped" / "hygiene"
        (d / "scripts").mkdir(parents=True)
        self.script = d / "scripts" / "reaper.py"
        self.script.write_text(STUB.replace("MODE", repr(mode)))
        (d / "manifest.json").write_text(json.dumps({"name": "hygiene", sl.DECLARATION: "scripts/reaper.py"}))
        (self.installed / "hygiene").symlink_to(d)

    def reaps(self):
        if self.script is None:
            return []
        log = self.script.parent / "calls.jsonl"
        calls = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        return [c for c in calls if "--reap" in c]

    def check(self, load=30.0):
        row = self.hc.check_system_load(self.ws, now=self.t, load_fn=lambda: (load, 0, 0), cores_fn=lambda: 8,
                                        runner=Runner())
        self.t += 300
        return row

    def fix(self, row, **kw):
        kw.setdefault("now", self.t)
        return self.hc.apply_system_load_reap([row], self.ws, stream=self.log, **kw)

    def warn(self):
        rows = [self.check() for _ in range(3)]
        self.assertEqual(rows[-1]["status"], "warn")
        return rows[-1]


class WarnOnly(Base):
    def test_no_reap_without_fix(self):
        self.install()
        rows = [self.check() for _ in range(6)]
        self.assertEqual(rows[-1]["action"], sl.ACTION_REAP)
        self.assertEqual(self.reaps(), [])

    def test_the_warning_says_what_fix_will_do(self):
        self.install()
        row = self.warn()
        self.assertTrue(row["detail"].endswith(
            "; --fix runs the hygiene reaper once for this episode; cause=leftover-test-processes action=reap-once"),
            row["detail"])

    def test_reaping_is_off_unless_exactly_1(self):
        self.install()
        cases = {None: False, "0": False, "O": False, "disable": False, "true": False, "True": False,
                 "TRUE": False, " 1": False, "1 ": False, "yes": False, "": False, "1": True}
        self.cfg.pop(sl.REAP_KEY)
        self.warn()
        for value, on in cases.items():
            with self.subTest(value=value):
                self.cfg.pop(sl.REAP_KEY, None)
                if value is not None:
                    self.cfg[sl.REAP_KEY] = value
                self.assertIs(sl.reap_enabled(value), on)
                row = self.check()
                self.assertEqual((row["status"], row["cause"]), ("warn", sl.CAUSE_LEFTOVER))
                if on:
                    self.assertEqual(row["action"], sl.ACTION_REAP)
                    self.assertIn("--fix runs the hygiene reaper once for this episode", row["detail"])
                    continue
                self.assertEqual(row["action"], sl.ACTION_NONE)
                self.assertTrue(row["detail"].endswith(
                    f"; reaping is off ({sl.REAP_KEY}=1 enables it); cause=leftover-test-processes action=none"),
                    row["detail"])
                self.assertIsNone(self.fix(row))
                self.assertEqual(self.reaps(), [])

    def test_a_reap_once_row_from_before_reaping_was_turned_off_does_not_reap(self):
        self.install()
        row = self.warn()
        self.assertEqual(row["action"], sl.ACTION_REAP)
        self.cfg[sl.REAP_KEY] = "true"
        self.assertEqual(self.fix(row), {"action": "disabled"})
        self.assertIn(f"not reaped: reaping is off ({sl.REAP_KEY}=1 enables it)", self.log.getvalue())
        self.assertEqual(self.reaps(), [])

    def test_the_default_is_documented_as_off(self):
        env = json.loads((REPO / "docs" / "sutando-config.schema.json").read_text())["properties"]["env"]
        self.assertEqual(env["properties"][sl.REAP_KEY]["default"], "0")
        self.assertIn(f"| `{sl.REAP_KEY}` | `0` (unset is off) |", (REPO / "docs" / "workspace-config.md").read_text())


class FixOncePerEpisode(Base):
    def test_fix_reaps_once_and_the_warning_then_says_it_already_ran(self):
        self.install()
        row = self.warn()
        self.assertEqual(self.fix(row)["action"], "reaped")
        self.assertEqual(self.reaps(), [["--reap", "--spotlight"]])
        self.assertEqual(row["action"], sl.ACTION_NONE)
        self.assertRegex(row["detail"], r"; the hygiene reaper already ran this episode at \d{4}-\d\d-\d\d \d\d:\d\d: "
                                        r"stopped 2 of 3 \(2 terminated, 0 killed, 1 changed before stopping\); "
                                        r"cause=leftover-test-processes action=none$")
        self.assertIn("--fix ran the reaper once this episode: stopped 2 of 3", self.log.getvalue())
        later = self.check()
        self.assertEqual((later["status"], later["action"]), ("warn", sl.ACTION_NONE))
        self.assertIn("already ran this episode", later["detail"])

    def test_a_repeated_fix_in_the_same_episode_stops_nothing(self):
        self.install()
        row = self.warn()
        self.fix(row)
        self.assertEqual(len(self.reaps()), 1)
        for _ in range(3):
            again = self.check()
            self.assertIsNone(self.fix(again))
        stale = {"name": sl.NAME, "status": "warn", "cause": sl.CAUSE_LEFTOVER, "action": sl.ACTION_REAP,
                 "_base": "b", "_reaper": "hygiene", "_reap_on": True}
        self.assertEqual(self.fix(stale)["action"], "already")
        self.assertEqual(len(self.reaps()), 1, "a repeat --fix in one episode must invoke the reaper zero more times")

    def test_rise_fall_rise_reaps_twice(self):
        self.install()
        self.fix(self.warn())
        self.fix(self.check())
        normal = self.check(load=2.0)
        self.assertIsNone(self.fix(normal))
        self.assertIsNone(sl.read_state(self.ws / "state" / sl.STATE_NAME)["episode"])
        row = self.warn()
        self.assertEqual(row["action"], sl.ACTION_REAP)
        self.assertEqual(self.fix(row)["action"], "reaped")
        self.assertEqual(len(self.reaps()), 2)

    def test_reaper_invoked_with_exactly_these_args(self):
        self.install()
        runner = Runner()
        self.fix(self.warn(), runner=runner)
        self.assertEqual(runner.calls, [[sys.executable, str(self.script.resolve()), "--reap", "--spotlight"]])


class FixCannotReap(Base):
    def test_skill_absent_keeps_warning_and_sends_nothing(self):
        self.assertTrue((REPO / "skills" / "process-hygiene" / "manifest.json").is_file())
        row = self.warn()
        self.assertEqual((row["cause"], row["action"]), (sl.CAUSE_UNCLASSIFIED, sl.ACTION_NONE))
        self.assertIsNone(self.fix(row))
        self.assertEqual(row["status"], "warn")
        self.assertIn("no installed skill declares an orphan reaper", row["detail"])

    def test_skill_unlinked_between_the_pass_and_the_fix(self):
        self.install()
        row = self.warn()
        (self.installed / "hygiene").unlink()
        self.assertEqual(self.fix(row)["action"], "no-reaper")
        self.assertEqual((row["status"], row["action"]), ("warn", sl.ACTION_NONE))
        self.assertIn("--fix could not reap: no installed skill declares an orphan reaper", row["detail"])
        self.assertEqual(self.reaps(), [])

    def test_unclassified_other_or_ok_rows_are_left_alone(self):
        self.install()
        for row in ({"name": sl.NAME, "status": "warn", "action": sl.ACTION_NONE},
                    {"name": sl.NAME, "status": "ok"}, {"name": "x", "status": "warn"}):
            self.assertIsNone(self.hc.apply_system_load_reap([row], self.ws, stream=self.log))
        self.assertEqual(self.reaps(), [])

    def test_usage_error_warns_and_is_not_retried(self):
        self.install(mode="usage")
        row = self.warn()
        self.assertEqual(self.fix(row)["action"], "usage-error")
        self.assertIn("WARN reaper refused its arguments (exit 2)", self.log.getvalue())
        self.assertEqual(row["status"], "warn")
        self.assertIn("failed this episode", row["detail"])
        self.assertIn("(usage error, exit 2); not retried until load returns to normal", row["detail"])
        self.assertIsNone(self.fix(self.check()))
        self.assertEqual(len(self.reaps()), 1)

    def test_a_busy_reaper_is_retried_a_bounded_number_of_times(self):
        self.install(mode="busy")
        row = self.warn()
        for i in range(sl.REAP_MAX_ATTEMPTS):
            self.assertEqual(self.fix(row)["action"], "nothing")
            if i + 1 < sl.REAP_MAX_ATTEMPTS:
                self.assertIn(f"(it did nothing on {i + 1} of {sl.REAP_MAX_ATTEMPTS} tries)", row["detail"])
            row = self.check()
        self.assertEqual(row["action"], sl.ACTION_NONE)
        self.assertIn(f"did nothing in {sl.REAP_MAX_ATTEMPTS} tries", row["detail"])
        self.assertIsNone(self.fix(row))
        self.assertEqual(len(self.reaps()), sl.REAP_MAX_ATTEMPTS)

    def test_a_reaper_that_cannot_run_is_not_retried(self):
        self.install()

        def boom(argv, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)
        row = self.warn()
        self.assertEqual(self.fix(row, runner=boom)["action"], "failed")
        self.assertIn("(could not run: TimeoutExpired)", row["detail"])
        self.assertEqual(self.fix(dict(row, action=sl.ACTION_REAP), runner=boom)["action"], "already")

    def test_another_fix_holding_the_reap_lock(self):
        self.install()
        row = self.warn()
        before = dict(row)
        with sl.locked(Path(str(self.ws / "state" / sl.STATE_NAME) + ".reap")):
            self.assertEqual(self.fix(row)["action"], "busy")
        self.assertEqual(row, before)
        self.assertEqual(self.reaps(), [])


class Episodes(unittest.TestCase):
    def tmpdir(self):
        tmp = Path(tempfile.mkdtemp(prefix="sysload-ep-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        return tmp

    def test_tracking(self):
        hi, lo = {"ts": 2.0, "load1": 30, "cores": 8}, {"ts": 2.0, "load1": 1, "cores": 8}
        state = {}
        self.assertEqual(sl.track_episode(state, None, hi, 1.5)["start"], 2.0)
        self.assertEqual(sl.track_episode(state, hi, dict(hi, ts=3.0), 1.5)["start"], 2.0)
        self.assertIsNone(sl.track_episode(state, hi, lo, 1.5))
        self.assertEqual(sl.track_episode(state, lo, dict(hi, ts=9.0), 1.5)["start"], 9.0)
        state["episode"] = "junk"
        self.assertEqual(sl.track_episode(state, hi, dict(hi, ts=10.0), 1.5)["start"], 10.0)

    def test_a_gap_starts_a_new_episode(self):
        state = self.tmpdir() / sl.STATE_NAME
        kw = dict(load_fn=lambda: (30.0, 0, 0), cores_fn=lambda: 8, runner=Runner())
        sl.check(state, now=100.0, **kw)
        sl.check(state, now=130.0, **kw)
        self.assertEqual(sl.read_state(state)["episode"]["start"], 100.0)
        sl.check(state, now=130.0 + sl.MAX_GAP_S + 1, **kw)
        self.assertEqual(sl.read_state(state)["episode"]["start"], 130.0 + sl.MAX_GAP_S + 1)

    def test_state_writes_are_fsynced_and_whole(self):
        state = self.tmpdir() / "state" / sl.STATE_NAME
        with mock.patch("os.fsync", wraps=__import__("os").fsync) as fsync:
            sl.write_state(state, {"episode": None})
        self.assertGreaterEqual(fsync.call_count, 2, "the file and its directory are fsynced")
        self.assertEqual(sl.read_state(state), {"episode": None})
        self.assertEqual([p.name for p in state.parent.iterdir()], [sl.STATE_NAME])

    def test_a_stale_episode_record_is_not_overwritten(self):
        state = self.tmpdir() / sl.STATE_NAME
        sl.write_state(state, {"episode": {"start": 5.0, "reaped": None}})
        sl._save_episode(state, {"start": 4.0, "reaped": {"outcome": "reaped"}})
        self.assertEqual(sl.read_state(state)["episode"], {"start": 5.0, "reaped": None})

    def test_no_open_episode_and_counts(self):
        state = self.tmpdir() / sl.STATE_NAME
        self.assertEqual(sl.reap_once(state, Path("r.py"), log=lambda m: None)["action"], "no-episode")
        self.assertIsNone(sl.parse_counts("x: mode=report candidates=2\n"))
        self.assertEqual(sl.parse_counts("x: mode=reap roots=1 candidates=2 terminated=1 killed=1 aborted=0\n"),
                         {"candidates": 2, "terminated": 1, "killed": 1, "aborted": 0})
        self.assertEqual(sl._stamp(None), "an unknown time")


class MainWiring(Base):
    """main() runs the reap step only under --fix; the core decides when to pass it."""

    def run_main(self, argv):
        row = {"name": sl.NAME, "status": "warn", "detail": "d", "cause": sl.CAUSE_LEFTOVER,
               "action": sl.ACTION_REAP}
        with mock.patch.object(self.hc, "run_all_checks", return_value=[row]), \
                mock.patch.object(self.hc, "publish_health_report"), \
                mock.patch.object(self.hc, "track_health_fix"), \
                mock.patch.object(self.hc, "apply_skill_symlink_fixes"), \
                mock.patch.object(self.hc, "apply_task_watcher_sentinel_fix"), \
                mock.patch.object(self.hc, "apply_claude_hooks_fix"), \
                mock.patch.object(self.hc, "fix_down_bridges", return_value=[]), \
                mock.patch.object(self.hc, "apply_system_load_reap") as reap, \
                mock.patch.object(sys, "argv", ["health-check.py"] + argv), \
                mock.patch("builtins.print"):
            try:
                self.hc.main()
            except SystemExit:
                pass
        return reap

    def test_only_fix_reaps(self):
        self.assertEqual(self.run_main([]).call_count, 0)
        self.assertEqual(self.run_main(["--quiet"]).call_count, 0)
        self.assertEqual(self.run_main(["--json"]).call_count, 0)
        self.assertEqual(self.run_main(["--fix"]).call_args.kwargs["stream"], sys.stdout)
        self.assertEqual(self.run_main(["--fix", "--json"]).call_args.kwargs["stream"], sys.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
