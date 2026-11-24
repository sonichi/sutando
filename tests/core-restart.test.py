#!/usr/bin/env python3
"""core_restart: the one route for a core restart, and its callers outside the recovery cards.

In the desktop bundle (`<app>/engine/<checkout>`) a restart is the app's sutando-ctl.sh
restart-core, refused when the ctl is missing or takes no launch lock; elsewhere it is
start-cli.sh --restart. Both run between intent and outcome lines in state/recovery/audit.jsonl.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import io
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import core_limit_hold as clh
import core_restart as CR
import sutando_config

_spec = importlib.util.spec_from_file_location("health_check_core_restart", REPO / "src" / "health-check.py")
HC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(HC)

LOCKING = "status\nrestart-core\nlaunch-lock\n"


class Run:
    """Stands in for subprocess.run: records each call, answers `capabilities` with `caps`."""

    def __init__(self, caps=LOCKING, caps_rc=0, rc=0, raises=None):
        self.calls, self.caps, self.caps_rc, self.rc, self.raises = [], caps, caps_rc, rc, raises

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if argv[-1] == "capabilities":
            if self.raises is not None:
                raise self.raises
            return subprocess.CompletedProcess(argv, self.caps_rc, stdout=self.caps)
        return subprocess.CompletedProcess(argv, self.rc, stdout="")

    def argvs(self):
        return [a for a, _kw in self.calls]


#: What the recorder says on stderr when both of an attempt's audit lines could not be written.
LOST = ["core_restart: audit intent line not written (attempt att_0123456789ab)",
        "core_restart: audit outcome line not written (attempt att_0123456789ab)"]


class LostAudit(Run):
    """A restart whose recorder lost both audit lines, with the launcher's own output around them."""

    def __call__(self, argv, **kw):
        done = super().__call__(argv, **kw)
        if argv[-1] != "capabilities":
            done.stderr = "launcher noise\n" + "\n".join(LOST) + "\nmore launcher noise\n"
        return done


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.app = self.root / "app"
        self.bundle = self.app / "engine" / "sutando"
        (self.bundle / "src" / "agent").mkdir(parents=True)
        (self.bundle / "src" / "agent" / "start-cli.sh").write_text("#!/bin/bash\n")
        self.ctl = self.app / "engine" / "sutando-ctl.sh"
        self.ctl.write_text("#!/bin/bash\n")
        self.clone = self.root / "clone" / "sutando"
        (self.clone / "src" / "agent").mkdir(parents=True)
        (self.clone / "src" / "agent" / "start-cli.sh").write_text("#!/bin/bash\n")
        self.app_dir = None
        p = mock.patch.object(sutando_config, "desktop_app_support", side_effect=lambda _repo=None: self.app_dir)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def desktop(self):
        self.app_dir = self.app

    def audit(self):
        path = self.ws / "state" / "recovery" / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class Route(Base):
    def route(self, repo, run=None, env=None, **kw):
        env = {} if env is None else env
        return CR.route(repo, env, run or Run(), workspace=self.ws, caller="test", **kw)

    def test_without_the_app_it_is_start_cli(self):
        argv, via = self.route(self.clone, runtime="codex", extra=["--visible"])
        self.assertEqual(argv[:3], [sys.executable, CR.RECORDER, "start-cli"])
        self.assertEqual(argv[argv.index("--start-cli") + 1:],
                         [str(self.clone / "src" / "agent" / "start-cli.sh"), "--runtime", "codex",
                          "--", "--visible"])
        self.assertEqual(via, CR.VIA_START_CLI)

    def test_a_start_runs_the_launcher_with_no_restart_flag_outside_the_bundle(self):
        argv, via = self.route(self.clone, start=True, start_cli="/x/L")
        self.assertEqual((argv[:3], via),
                         ([sys.executable, CR.RECORDER, "start-cli"],
                          CR.VIA_START_CLI))
        self.assertEqual(argv[argv.index("--start-cli") + 1:], ["/x/L", "--start"])

    def test_a_start_in_the_bundle_is_the_ctl_like_a_restart(self):
        self.desktop()
        argv, via = self.route(self.bundle, start=True, start_cli="/x/L")
        self.assertEqual((via, argv[2], argv[-1]), (CR.VIA_CTL, "ctl", "--start"))
        self.assertNotIn("--start", self.route(self.bundle)[0])

    def test_a_given_launcher_is_kept_outside_the_app(self):
        argv, _via = self.route(self.clone, start_cli="/x/L")
        self.assertEqual(argv[argv.index("--start-cli") + 1], "/x/L")

    def test_a_clone_outside_the_bundle_falls_back_to_start_cli(self):
        self.desktop()
        run = Run()
        argv, via = self.route(self.clone, run)
        self.assertEqual((argv[2], via, run.calls), ("start-cli", CR.VIA_FALLBACK, []))

    def test_the_bundle_without_its_ctl_is_refused(self):
        self.desktop()
        self.ctl.unlink()
        run = Run()
        self.assertEqual(self.route(self.bundle, run), (None, CR.VIA_NO_CTL))
        self.assertEqual(run.calls, [], "nothing ran")

    def test_a_ctl_without_the_launch_lock_is_refused(self):
        self.desktop()
        for name, run in (("usage", Run(caps="", caps_rc=2)), ("no token", Run(caps="status\n")),
                          ("hangs", Run(raises=subprocess.TimeoutExpired("x", 5))), ("oserror", Run(raises=OSError()))):
            with self.subTest(name):
                self.assertEqual(self.route(self.bundle, run), (None, CR.VIA_NO_LOCK))
                self.assertEqual([a[-1] for a in run.argvs()], ["capabilities"], "only the read-only probe")

    def test_a_locking_ctl_runs_through_the_recorder_with_the_cards_ids(self):
        self.desktop()
        run = Run()
        env = {CR.LOCK_MARKER: "/elsewhere/engine-launch.lock"}
        argv, via = self.route(self.bundle, run, env, hitl_id="hitl_1", revision=3, runtime="claude")
        self.assertEqual(via, CR.VIA_CTL)
        self.assertEqual(argv, [sys.executable, str(self.bundle / "src" / "core_restart.py"), "ctl",
                                "--ctl", str(self.ctl), "--workspace", str(self.ws), "--caller", "test",
                                "--hitl-id", "hitl_1", "--revision", "3", "--runtime", "claude"])
        self.assertEqual(env["SUTANDO_APP_SUPPORT"], str(self.app))
        self.assertNotIn(CR.LOCK_MARKER, env, "a lock marker is never inherited")
        [(_probe, kw)] = run.calls
        self.assertEqual(kw["timeout"], CR.CTL_CAPABILITIES_TIMEOUT_S)

    def test_without_a_card_no_ids_are_passed(self):
        self.desktop()
        argv, _via = self.route(self.bundle)
        self.assertNotIn("--hitl-id", argv)
        self.assertNotIn("--runtime", argv)


class RunCtl(Base):
    def test_one_intent_and_one_outcome_line_carry_the_caller_and_the_cards_ids(self):
        run = Run(rc=0)
        rc = CR.run_ctl(self.ctl, self.ws, "recovery-card", hitl_id="hitl_1", revision=4, runner=run,
                        env={CR.LOCK_MARKER: "x", "A": "1"}, now=lambda: 100.0)
        self.assertEqual(rc, 0)
        [(argv, kw)] = run.calls
        self.assertEqual(argv, ["bash", str(self.ctl), "restart-core"])
        self.assertNotIn(CR.LOCK_MARKER, kw["env"])
        intent, outcome = self.audit()
        base = {"event": "core_restart", "via": "ctl", "caller": "recovery-card", "hitl_id": "hitl_1",
                "revision": 4, "at": 100, "attempt": intent["attempt"]}
        self.assertEqual(intent, dict(base, phase="intent"))
        self.assertEqual(outcome, dict(base, phase="outcome", outcome="done", rc=0))

    def test_a_busy_lock_is_a_failed_outcome_with_its_code(self):
        rc = CR.run_ctl(self.ctl, self.ws, "graceful-restart", runner=Run(rc=CR.CTL_LAUNCH_LOCK_BUSY), env={})
        self.assertEqual(rc, CR.CTL_LAUNCH_LOCK_BUSY)
        intent, outcome = self.audit()
        self.assertNotIn("hitl_id", intent, "no card, so only the automatic actor and caller are named")
        self.assertEqual(intent["actor"], "host-supervisor")
        self.assertEqual((outcome["outcome"], outcome["rc"], outcome["caller"]), ("failed", 12, "graceful-restart"))

    def test_a_ctl_that_cannot_run_is_failed_with_no_code(self):
        def boom(argv, **kw):
            raise OSError("no bash")
        self.assertEqual(CR.run_ctl(self.ctl, self.ws, "x", runner=boom, env={}), 1)
        self.assertNotIn("rc", self.audit()[-1])
        self.assertEqual(self.audit()[-1]["outcome"], "failed")

    def test_a_runtime_switch_selects_it_first_and_puts_the_old_one_back_on_failure(self):
        self.desktop()
        selection = self.app / "runtime.json"
        selection.write_text(json.dumps({"coreRuntime": "claude", "other": 1}))
        seen = []

        def run(argv, **kw):
            seen.append(json.loads(selection.read_text())["coreRuntime"])
            return subprocess.CompletedProcess(argv, 1)
        rc = CR.run_ctl(self.ctl, self.ws, "recovery-card", runtime="codex", runner=run, env={}, repo=self.bundle)
        self.assertEqual((rc, seen), (1, ["codex"]), "the app's launcher read the new runtime")
        self.assertEqual(json.loads(selection.read_text()), {"coreRuntime": "claude", "other": 1})
        self.assertEqual(self.audit()[-1]["runtime"], "codex")

    def test_a_runtime_switch_that_succeeds_keeps_the_new_selection(self):
        self.desktop()
        selection = self.app / "runtime.json"
        selection.write_text(json.dumps({"coreRuntime": "claude"}))
        CR.run_ctl(self.ctl, self.ws, "x", runtime="codex", runner=Run(rc=0), env={}, repo=self.bundle)
        self.assertEqual(json.loads(selection.read_text())["coreRuntime"], "codex")

    def test_a_failed_restore_still_records_the_failure(self):
        self.desktop()
        (self.app / "runtime.json").write_text(json.dumps({"coreRuntime": "claude"}))
        with mock.patch.object(sutando_config, "restore_desktop_runtime_selection", side_effect=OSError("ro")) as r:
            rc = CR.run_ctl(self.ctl, self.ws, "x", runtime="codex", runner=Run(rc=1), env={}, repo=self.bundle)
        self.assertEqual((rc, self.audit()[-1]["outcome"]), (1, "failed"))
        r.assert_called_once_with("codex", "claude", self.bundle)

    def test_with_no_prior_selection_a_failed_switch_leaves_none(self):
        self.desktop()
        (self.app / "runtime.json").write_text(json.dumps({"other": 1}))
        CR.run_ctl(self.ctl, self.ws, "x", runtime="codex", runner=Run(rc=1), env={}, repo=self.bundle)
        self.assertEqual(json.loads((self.app / "runtime.json").read_text()), {"other": 1})

    def test_a_newer_choice_made_during_the_restart_is_not_rolled_back(self):
        self.desktop()
        selection = self.app / "runtime.json"
        selection.write_text(json.dumps({"coreRuntime": "claude"}))

        def run(argv, **kw):
            selection.write_text(json.dumps({"coreRuntime": "claude-next"}))  # the app's own writer
            return subprocess.CompletedProcess(argv, 1)
        CR.run_ctl(self.ctl, self.ws, "x", runtime="codex", runner=run, env={}, repo=self.bundle)
        self.assertEqual(json.loads(selection.read_text())["coreRuntime"], "claude-next")

    def test_an_unchanged_selection_is_never_rolled_back(self):
        self.desktop()
        (self.app / "runtime.json").write_text(json.dumps({"coreRuntime": "codex"}))
        with mock.patch.object(sutando_config, "restore_desktop_runtime_selection") as r:
            CR.run_ctl(self.ctl, self.ws, "x", runtime="codex", runner=Run(rc=1), env={}, repo=self.bundle)
        r.assert_not_called()

    def test_select_restart_and_rollback_run_under_one_hold_of_the_launch_lock(self):
        import fcntl
        self.desktop()
        (self.app / "runtime.json").write_text(json.dumps({"coreRuntime": "claude"}))
        lock = self.app / "run" / CR.LAUNCH_LOCK_FILE
        seen = {}

        def run(argv, **kw):
            with open(lock, "a") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen["free"] = True
                except OSError:
                    seen["free"] = False
            seen["marker"] = kw["env"].get(CR.LOCK_MARKER)
            return subprocess.CompletedProcess(argv, 0)
        CR.run_ctl(self.ctl, self.ws, "x", runtime="codex", runner=run, env={}, repo=self.bundle)
        self.assertEqual(seen, {"free": False, "marker": str(lock)}, "the ctl runs inside this process's hold")
        with open(lock, "a") as after:
            fcntl.flock(after, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released once the switch is over

    def test_a_busy_launch_lock_changes_nothing_and_runs_nothing(self):
        import fcntl
        self.desktop()
        selection = self.app / "runtime.json"
        selection.write_text(json.dumps({"coreRuntime": "claude"}))
        lock = self.app / "run" / CR.LAUNCH_LOCK_FILE
        lock.parent.mkdir(parents=True)
        run = Run()
        with open(lock, "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rc = CR.run_ctl(self.ctl, self.ws, "x", runtime="codex", runner=run, env={}, repo=self.bundle,
                            lock_wait_s=0)
        self.assertEqual((rc, run.calls), (CR.CTL_LAUNCH_LOCK_BUSY, []))
        self.assertEqual(json.loads(selection.read_text())["coreRuntime"], "claude")
        self.assertEqual((self.audit()[-1]["outcome"], self.audit()[-1]["rc"]), ("failed", CR.CTL_LAUNCH_LOCK_BUSY))

    def test_the_lock_wait_follows_the_ctls_setting_and_waits_before_giving_up(self):
        lock = self.root / "l"
        held, state = CR.take_launch_lock(lock, 1)
        self.assertEqual(state, CR.LOCK_LOCKED)
        ticks, slept = iter([0.0, 0.5, 2.0]), []
        try:
            self.assertEqual(CR.take_launch_lock(lock, 1, sleep=slept.append, clock=lambda: next(ticks)),
                             (None, CR.LOCK_BUSY))
        finally:
            held.close()
        self.assertEqual(slept, [0.2])
        self.desktop()
        (self.app / "runtime.json").write_text(json.dumps({"coreRuntime": "claude"}))
        with mock.patch.object(CR, "take_launch_lock", return_value=(None, CR.LOCK_BUSY)) as take:
            CR.run_ctl(self.ctl, self.ws, "x", runtime="codex", runner=Run(), env={CR.LOCK_WAIT_ENV: "7"},
                       repo=self.bundle)
        self.assertEqual(take.call_args[0][1], 7.0)

    def test_a_lock_that_cannot_be_opened_refuses_before_the_selection_or_the_ctl_changes(self):
        self.desktop()
        selection = self.app / "runtime.json"
        before = json.dumps({"coreRuntime": "claude", "other": 1})
        selection.write_text(before)
        (self.app / "run").write_text("a file where the run dir would be")
        run = Run(rc=0)
        with mock.patch.object(sutando_config, "persist_desktop_runtime_selection") as persist:
            rc = CR.run_ctl(self.ctl, self.ws, "x", runtime="codex", runner=run, env={}, repo=self.bundle)
        self.assertEqual((rc, run.calls), (CR.REFUSED_EXIT, []), "the controller never ran")
        persist.assert_not_called()
        self.assertEqual(selection.read_text(), before, "the selection is byte-for-byte what it was")
        last = self.audit()[-1]
        self.assertEqual((last["outcome"], last["reason"]), ("refused", "launch-lock-unavailable"))


    def test_an_unselectable_runtime_is_refused_before_the_ctl_runs(self):
        self.desktop()
        (self.app / "runtime.json").write_text("[]")  # not an object: never replaced
        run = Run()
        rc = CR.run_ctl(self.ctl, self.ws, "x", runtime="codex", runner=run, env={}, repo=self.bundle)
        self.assertEqual((rc, run.calls), (CR.REFUSED_EXIT, []))
        self.assertEqual((self.audit()[-1]["outcome"], self.audit()[-1]["reason"]), ("refused", "runtime-not-selectable"))


class StartIfDown(Base):
    """A start (a paused core started again) never stops anything: under the launch lock it reads the
    ctl's status, and only a core that is down is started, by the app's launcher."""

    def setUp(self):
        super().setUp()
        self.desktop()
        self.launcher = self.app / "engine" / CR.DESKTOP_LAUNCHER
        self.launcher.write_text("#!/bin/bash\n")
        self.core = "stopped"
        self.calls = []
        self.launch_rc = 0

    def fake(self, argv, **kw):
        self.calls.append((list(argv), kw))
        if argv[-1] == "status":
            return subprocess.CompletedProcess(argv, 0, stdout=f"core={self.core} gateway=running availability=x\n")
        return subprocess.CompletedProcess(argv, self.launch_rc, stdout="")

    def start(self, **kw):
        return CR.run_ctl(self.ctl, self.ws, "switch-model", start=True, runner=lambda argv, **kw: self.fake(argv, **kw), env={},
                          repo=self.bundle, **kw)

    def test_a_core_that_is_down_is_started_by_the_launcher_under_the_lock(self):
        import fcntl
        lock = self.app / "run" / CR.LAUNCH_LOCK_FILE
        held = []
        real = self.fake

        def run(argv, **kw):
            with open(lock, "a") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(False)
                except OSError:
                    held.append(True)
            return real(argv, **kw)
        self.fake = run
        self.assertEqual(self.start(), 0)
        self.assertEqual([a for a, _kw in self.calls],
                         [["bash", str(self.ctl), "status"], ["bash", str(self.launcher)]])
        self.assertEqual(held, [True, True], "the status read and the launch are inside one hold")
        self.assertNotIn(CR.LOCK_MARKER, self.calls[-1][1]["env"], "the launcher's children never inherit the marker")
        self.assertEqual([(r["phase"], r.get("outcome")) for r in self.audit()], [("intent", None), ("outcome", "done")])

    def test_a_core_that_came_up_while_the_lock_was_awaited_is_left_running(self):
        # The caller saw no core; one starts while this waits for the launch lock.
        real = CR.take_launch_lock

        def take(lock, wait):
            self.core = "running"
            return real(lock, wait)
        with mock.patch.object(CR, "take_launch_lock", side_effect=take):
            rc = self.start()
        self.assertEqual(rc, CR.ALREADY_RUNNING_EXIT)
        self.assertEqual([a[-1] for a, _kw in self.calls], ["status"], "nothing was stopped or started")
        last = self.audit()[-1]
        self.assertEqual((last["outcome"], last["reason"]), ("skipped", "core-already-running"))

    def test_the_launchers_exit_code_is_the_outcome(self):
        self.launch_rc = 75
        self.assertEqual(self.start(), 75)
        self.assertEqual((self.audit()[-1]["outcome"], self.audit()[-1]["rc"]), ("failed", 75))

    def test_an_unreadable_status_starts_nothing(self):
        for name, runner in (("no token", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="?\n")),
                             ("fails", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="core=stopped")),
                             ("hangs", mock.Mock(side_effect=subprocess.TimeoutExpired("x", 5)))):
            with self.subTest(name):
                calls = []

                def run(argv, _r=runner, **kw):
                    calls.append(argv[-1])
                    return _r(argv, **kw)
                rc = CR.run_ctl(self.ctl, self.ws, "x", start=True, runner=run, env={}, repo=self.bundle)
                self.assertEqual((rc, calls), (CR.REFUSED_EXIT, ["status"]))
                self.assertEqual(self.audit()[-1]["reason"], "core-state-unknown")

    def test_without_the_apps_launcher_nothing_runs_after_the_status_read(self):
        self.launcher.unlink()
        self.assertEqual(self.start(), CR.REFUSED_EXIT)
        self.assertEqual([a[-1] for a, _kw in self.calls], ["status"])
        self.assertEqual(self.audit()[-1]["reason"], "launcher-missing")

    def test_a_busy_or_unopenable_lock_reads_and_starts_nothing(self):
        import fcntl
        lock = self.app / "run" / CR.LAUNCH_LOCK_FILE
        lock.parent.mkdir(parents=True)
        with open(lock, "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.start(lock_wait_s=0), CR.CTL_LAUNCH_LOCK_BUSY)
        lock.unlink()
        lock.parent.rmdir()
        (self.app / "run").write_text("a file where the run dir would be")
        self.assertEqual(self.start(), CR.REFUSED_EXIT)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.audit()[-1]["reason"], "launch-lock-unavailable")


class RestoreSelection(Base):
    def test_restore_is_compare_and_swap_and_keeps_other_keys(self):
        self.desktop()
        path = self.app / "runtime.json"
        path.write_text(json.dumps({"coreRuntime": "codex", "k": 1}))
        self.assertEqual(sutando_config.restore_desktop_runtime_selection("codex", "claude", self.bundle), "restored")
        self.assertEqual(json.loads(path.read_text()), {"coreRuntime": "claude", "k": 1})
        self.assertEqual(sutando_config.restore_desktop_runtime_selection("codex", "", self.bundle), "superseded")
        path.unlink()
        self.assertEqual(sutando_config.restore_desktop_runtime_selection("codex", "", self.bundle), "superseded")

    def test_restore_refuses_what_it_cannot_read_and_needs_a_desktop(self):
        self.assertEqual(sutando_config.restore_desktop_runtime_selection("codex", "", self.clone), "no-desktop")
        self.desktop()
        for body in ("[]", "{not json"):
            with self.subTest(body):
                (self.app / "runtime.json").write_text(body)
                with self.assertRaises(ValueError):
                    sutando_config.restore_desktop_runtime_selection("codex", "", self.bundle)


class Restart(Base):
    def test_a_refusal_says_why_and_exits_refused(self):
        self.desktop()
        for name, caps, want in (("no lock", "status\n", "no launch lock"), ("no ctl", None, "is missing")):
            with self.subTest(name):
                if caps is None:
                    self.ctl.unlink()
                err, execv = io.StringIO(), mock.Mock()
                rc = CR.restart(self.ws, "graceful-restart", repo=self.bundle, runner=Run(caps=caps or ""),
                                execv=execv, err=err)
                self.assertEqual(rc, CR.REFUSED_EXIT)
                self.assertIn(want, err.getvalue())
                execv.assert_not_called()

    def test_outside_the_bundle_the_recording_wrapper_replaces_the_process(self):
        execv = mock.Mock()
        CR.restart(self.ws, "graceful-restart", repo=self.clone, extra=["--visible"], start_cli="/x/stub",
                   runner=Run(), execv=execv)
        execv.assert_called_once()
        argv = execv.call_args[0][1]
        self.assertEqual(argv[:3], [sys.executable, CR.RECORDER, "start-cli"])
        self.assertEqual(argv[argv.index("--start-cli") + 1:], ["/x/stub", "--foreground", "--", "--visible"])

    def test_start_cli_writes_one_intent_and_one_outcome_for_an_automatic_restart(self):
        run = Run(rc=0)
        rc = CR.run_start_cli(self.ws, "health-check --recover-core", start_cli="/x/stub",
                              runner=run, now=lambda: 100.0)
        self.assertEqual(rc, 0)
        self.assertEqual(run.argvs(), [["bash", "/x/stub", "--restart"]])
        intent, outcome = self.audit()
        base = {"event": "core_restart", "via": "start-cli", "caller": "health-check --recover-core",
                "actor": "host-supervisor", "at": 100, "attempt": intent["attempt"]}
        self.assertRegex(intent["attempt"], r"^att_[0-9a-f]{12}$")
        self.assertEqual(intent, dict(base, phase="intent"))
        self.assertEqual(outcome, dict(base, phase="outcome", outcome="done", rc=0))

    def test_start_cli_records_the_card_identity_instead_of_an_automatic_actor(self):
        CR.run_start_cli(self.ws, "recovery-card", start_cli="/x/stub", hitl_id="hitl_9", revision=6,
                         runner=Run(rc=1), now=lambda: 100.0)
        intent, outcome = self.audit()
        self.assertEqual((intent["hitl_id"], intent["revision"]), ("hitl_9", 6))
        self.assertNotIn("actor", intent)
        self.assertEqual((outcome["outcome"], outcome["rc"]), ("failed", 1))

    def _launcher(self, body):
        """A real launcher that records its own pid before running `body`."""
        script = self.root / "launcher.sh"
        script.write_text(f'#!/bin/bash\necho $$ > "{self.root}/launcher.pid"\n{body}\n')
        return script

    def _launcher_gone(self):
        pid = int((self.root / "launcher.pid").read_text())
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    def test_a_launcher_past_the_recorders_deadline_is_killed_reaped_and_recorded(self):
        script = self._launcher("exec sleep 60")
        began = time.monotonic()
        rc = CR.run_start_cli(self.ws, "health-check --recover-core", start_cli=str(script), deadline_s=4)
        self.assertLess(time.monotonic() - began, 20)
        self.assertEqual(rc, 1)
        self.assertTrue(self._launcher_gone(), "the launcher outlived the recorder's deadline")
        intent, outcome = self.audit()
        self.assertEqual((outcome["outcome"], outcome["reason"]), ("failed", "deadline"))
        self.assertNotIn("rc", outcome)
        self.assertEqual(outcome["attempt"], intent["attempt"])

    def test_a_cancelled_recorder_ends_its_launcher_and_still_writes_the_outcome(self):
        script = self._launcher("exec sleep 60")
        argv = CR.recorded_start_cli_argv(REPO, self.ws, "health-check --recover-core", via=CR.VIA_START_CLI,
                                          start_cli=str(script))
        recorder = subprocess.Popen(argv, stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not (self.root / "launcher.pid").exists():
                time.sleep(0.05)
            self.assertTrue((self.root / "launcher.pid").exists(), "the launcher never started")
            recorder.send_signal(signal.SIGTERM)
            self.assertEqual(recorder.wait(timeout=20), 1)
        finally:
            if recorder.poll() is None:
                recorder.kill()
                recorder.wait()
        self.assertTrue(self._launcher_gone(), "the launcher outlived its cancelled recorder")
        intent, outcome = self.audit()
        self.assertEqual((outcome["outcome"], outcome["reason"]), ("failed", "cancelled"))
        self.assertEqual(outcome["attempt"], intent["attempt"])

    def test_a_stop_signal_is_turned_into_a_cancel(self):
        installed = {}
        CR._cancel_on_signal(install=lambda sig, handler: installed.__setitem__(sig, handler))
        self.assertEqual(set(installed), set(CR.CANCEL_SIGNALS))

        def cancelled(argv, **kw):
            installed[signal.SIGTERM](signal.SIGTERM, None)
        self.assertEqual(CR.run_start_cli(self.ws, "automatic", start_cli="/x/stub", runner=cancelled), 1)
        self.assertEqual(self.audit()[-1]["reason"], "cancelled")

    def test_the_recorder_is_bounded_only_when_the_caller_names_its_own_deadline(self):
        argv, _via = CR.route(self.clone, {}, Run(), workspace=self.ws, caller="c", caller_deadline_s=120)
        self.assertEqual(argv[argv.index("--deadline-s") + 1], str(120 - CR.DEADLINE_MARGIN_S))
        argv, _via = CR.route(self.clone, {}, Run(), workspace=self.ws, caller="c")
        self.assertNotIn("--deadline-s", argv)
        run = Run(rc=0)
        CR.run_start_cli(self.ws, "c", start_cli="/x/stub", runner=run, deadline_s=8, clock=lambda: 50.0)
        CR.run_start_cli(self.ws, "c", start_cli="/x/stub", runner=run)
        self.assertEqual([kw["timeout"] for _argv, kw in run.calls], [6, None],
                         "the launcher leaves a quarter of a short budget for the outcome line")
        with mock.patch.object(CR, "run_start_cli", return_value=0) as sc, mock.patch.object(CR, "_cancel_on_signal"):
            CR.main(["start-cli", "--workspace", "/w", "--caller", "c", "--via", "start-cli",
                     "--start-cli", "/s", "--deadline-s", "100"])
        self.assertEqual(sc.call_args.kwargs["deadline_s"], 100)

    def test_a_held_audit_lock_cannot_outlast_the_recorders_deadline(self):
        from file_lock import locked_file
        from hitl import recovery
        lock = recovery.recovery_dir(self.ws) / recovery.AUDIT_LOCK
        run, err, done = Run(rc=0), io.StringIO(), []

        def record():
            with mock.patch.object(sys, "stderr", err):
                done.append(CR.run_start_cli(self.ws, "health-check --recover-core", start_cli="/x/stub",
                                             runner=run, deadline_s=2))
        with locked_file(lock):  # another process appending, stalled
            began = time.monotonic()
            worker = threading.Thread(target=record, daemon=True)
            worker.start()
            worker.join(15)
            waited = time.monotonic() - began
            self.assertFalse(worker.is_alive(), "the recorder waited on the audit lock past its deadline")
        self.assertLess(waited, 2 + 1, "both lines and the launcher fit one budget")
        self.assertEqual(done, [0], "the restart itself still ran and its code is returned")
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(self.audit(), [])
        self.assertIn("audit intent line not written", err.getvalue())
        self.assertIn("audit outcome line not written", err.getvalue())
        self.assertEqual(len(CR.audit_diagnostics(err.getvalue())), 2, "a caller can pick both reports out")
        self.assertTrue(recovery.audit(self.ws, {"event": "after"}, wait_s=1), "the lock was left free")

    def test_an_unbounded_recorder_still_bounds_each_audit_line(self):
        run = Run(rc=0)
        with mock.patch.object(CR, "_audit", return_value=True) as audit:
            CR.run_start_cli(self.ws, "graceful-restart", start_cli="/x/stub", runner=run)
        self.assertEqual([c.kwargs["wait_s"] for c in audit.call_args_list], [CR.AUDIT_LOCK_WAIT_S] * 2)
        from hitl import recovery
        with mock.patch.object(recovery, "audit", return_value=True) as written:
            CR.run_ctl(self.ctl, self.ws, "recovery-card", runner=run)
        self.assertEqual([c.kwargs["wait_s"] for c in written.call_args_list], [CR.AUDIT_LOCK_WAIT_S] * 2)

    def test_a_foreground_restart_keeps_the_terminal_and_an_unattended_one_does_not(self):
        import pty
        script = self._launcher('if [ -t 0 ]; then echo terminal; else echo detached; fi > "$0.stdin"')
        seen = Path(str(script) + ".stdin")
        for foreground, want in ((True, "terminal"), (False, "detached")):
            argv = CR.recorded_start_cli_argv(REPO, self.ws, "graceful-restart", via=CR.VIA_START_CLI,
                                              start_cli=str(script), foreground=foreground)
            master, slave = pty.openpty()
            try:
                self.assertEqual(subprocess.run(argv, stdin=slave, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL, timeout=30).returncode, 0)
            finally:
                os.close(master)
                os.close(slave)
            self.assertEqual(seen.read_text().strip(), want)
        run = Run(rc=0)
        CR.run_start_cli(self.ws, "c", start_cli="/x/stub", runner=run, foreground=True)
        CR.run_start_cli(self.ws, "c", start_cli="/x/stub", runner=run)
        self.assertEqual([kw["stdin"] for _argv, kw in run.calls], [None, subprocess.DEVNULL])
        execv = mock.Mock()
        CR.restart(self.ws, "graceful-restart", repo=self.clone, start_cli="/x/stub", runner=Run(), execv=execv)
        self.assertIn("--foreground", execv.call_args[0][1], "only the foreground verb asks for the terminal")
        argv, _via = CR.route(self.clone, {}, Run(), workspace=self.ws, caller="health-check --recover-core")
        self.assertNotIn("--foreground", argv)

    def test_overlapping_attempts_pair_their_own_intent_and_outcome(self):
        def nested(argv, **kw):
            if not self.audit()[1:]:
                CR.run_start_cli(self.ws, "second", start_cli="/x/stub", runner=Run(rc=3))
            return subprocess.CompletedProcess(argv, 0)
        CR.run_start_cli(self.ws, "first", start_cli="/x/stub", runner=nested)
        lines = self.audit()
        self.assertEqual([(r["caller"], r["phase"]) for r in lines],
                         [("first", "intent"), ("second", "intent"), ("second", "outcome"), ("first", "outcome")])
        self.assertEqual(lines[0]["attempt"], lines[3]["attempt"])
        self.assertEqual(lines[1]["attempt"], lines[2]["attempt"])
        self.assertNotEqual(lines[0]["attempt"], lines[1]["attempt"])

    def test_an_audit_line_that_cannot_be_written_is_said_and_marks_the_outcome(self):
        from hitl import recovery
        real, err = recovery.audit, io.StringIO()

        def intent_fails(workspace, entry, **kw):
            return False if entry["phase"] == "intent" else real(workspace, entry, **kw)
        with mock.patch.object(recovery, "audit", side_effect=intent_fails), mock.patch.object(sys, "stderr", err):
            self.assertEqual(CR.run_start_cli(self.ws, "c", start_cli="/x/stub", runner=Run(rc=0)), 0)
            self.assertEqual(CR.run_ctl(self.ctl, self.ws, "c", runner=Run(rc=0)), 0)
        self.assertEqual(err.getvalue().count("core_restart: audit intent line not written (attempt att_"), 2)
        self.assertEqual([(r["phase"], r["intent_recorded"]) for r in self.audit()],
                         [("outcome", False), ("outcome", False)])
        with mock.patch.object(recovery, "audit", return_value=False), mock.patch.object(sys, "stderr", err):
            self.assertEqual(CR.run_start_cli(self.ws, "c", start_cli="/x/stub", runner=Run(rc=0)), 0)
        self.assertIn("audit outcome line not written", err.getvalue())

    def test_start_cli_that_cannot_run_is_failed_with_no_code(self):
        def boom(argv, **kw):
            raise OSError("no bash")
        self.assertEqual(CR.run_start_cli(self.ws, "automatic", start_cli="/x/stub", runner=boom), 1)
        self.assertEqual(self.audit()[-1]["outcome"], "failed")
        self.assertNotIn("rc", self.audit()[-1])

    def test_a_start_outside_the_bundle_execs_the_launcher_without_the_flag(self):
        execv = mock.Mock()
        CR.restart(self.ws, "switch-model", repo=self.clone, start_cli="/x/L", runner=Run(), execv=execv, start=True)
        argv = execv.call_args[0][1]
        self.assertEqual(argv[:3], [sys.executable, CR.RECORDER, "start-cli"])
        self.assertEqual(argv[argv.index("--start-cli") + 1:], ["/x/L", "--start", "--foreground"])
        run = Run(rc=0)
        CR.run_start_cli(self.ws, "switch-model", start_cli="/x/L", runner=run, start=True)
        self.assertEqual(run.argvs(), [["bash", "/x/L"]])

    def test_in_the_bundle_the_ctl_runs_here_between_its_lines(self):
        self.desktop()
        run, execv = Run(rc=0), mock.Mock()
        self.assertEqual(CR.restart(self.ws, "graceful-restart", repo=self.bundle, runner=run, execv=execv), 0)
        execv.assert_not_called()
        self.assertEqual(run.argvs()[-1], ["bash", str(self.ctl), "restart-core"])
        self.assertEqual([r["phase"] for r in self.audit()], ["intent", "outcome"])

    def test_a_start_in_the_bundle_is_a_start_if_down_not_a_restart(self):
        self.desktop()
        with mock.patch.object(CR, "run_ctl", return_value=CR.ALREADY_RUNNING_EXIT) as rc_:
            rc = CR.restart(self.ws, "switch-model", repo=self.bundle, runner=Run(), execv=mock.Mock(), start=True)
        self.assertEqual(rc, CR.ALREADY_RUNNING_EXIT)
        self.assertTrue(rc_.call_args.kwargs["start"])

    def test_the_cli_dispatches_each_verb(self):
        with mock.patch.object(CR, "run_ctl", return_value=12) as rc_:
            self.assertEqual(CR.main(["ctl", "--ctl", "/c", "--workspace", "/w", "--caller", "c",
                                      "--hitl-id", "h", "--revision", "2", "--runtime", "codex"]), 12)
            CR.main(["ctl", "--ctl", "/c", "--workspace", "/w", "--caller", "c", "--start"])
        rc_.assert_any_call(Path("/c"), Path("/w"), "c", hitl_id="h", revision=2, runtime="codex", start=False)
        self.assertTrue(rc_.call_args.kwargs["start"])
        with mock.patch.object(CR, "run_start_cli", return_value=7) as sc:
            self.assertEqual(CR.main(["start-cli", "--workspace", "/w", "--caller", "c", "--via",
                                      "start-cli-fallback", "--start-cli", "/s", "--hitl-id", "h",
                                      "--revision", "2", "--runtime", "codex", "--", "--visible"]), 7)
        sc.assert_called_once_with(Path("/w"), "c", via="start-cli-fallback", hitl_id="h", revision=2,
                                   runtime="codex", extra=["--visible"], start_cli="/s", start=False,
                                   deadline_s=None, foreground=False)
        with mock.patch.object(CR, "restart", return_value=0) as r:
            self.assertEqual(CR.main(["restart", "--workspace", "/w", "--caller", "g", "--repo", "/r",
                                      "--start-cli", "/s", "--", "--visible"]), 0)
            CR.main(["restart", "--start", "--workspace", "/w", "--caller", "switch-model"])
        r.assert_any_call(Path("/w"), "g", runtime=None, extra=["--visible"], start_cli="/s", repo=Path("/r"),
                          start=False)
        self.assertTrue(r.call_args.kwargs["start"])


class LimitHoldRetry(Base):
    """core_limit_hold's retry restart takes the same route."""

    def setUp(self):
        super().setUp()
        (self.ws / "results").mkdir()

    def told(self):
        return [p.read_text() for p in (self.ws / "results").glob("proactive-*.txt")]

    def test_in_the_bundle_the_retry_is_the_ctl_recorded_under_its_caller(self):
        self.desktop()
        run = Run(rc=0)
        with mock.patch.object(clh, "_REPO", self.bundle):
            self.assertTrue(clh.relaunch_core(self.ws, str(self.bundle / "src" / "agent" / "start-cli.sh"), run=run))
        restart = run.argvs()[-1]
        self.assertEqual(restart[2:4], ["ctl", "--ctl"])
        self.assertEqual(restart[restart.index("--caller") + 1], clh.RELAUNCH_CALLER)
        self.assertNotIn("--restart", restart)

    def test_in_the_bundle_without_the_lock_nothing_runs_and_the_owner_is_told(self):
        self.desktop()
        run = Run(caps="status\n")
        with mock.patch.object(clh, "_REPO", self.bundle):
            self.assertFalse(clh.relaunch_core(self.ws, "L", run=run))
        self.assertEqual([a[-1] for a in run.argvs()], ["capabilities"])
        [body] = self.told()
        self.assertIn("Restart the core from the desktop app", body)

    def test_a_relaunch_that_succeeds_with_its_audit_lines_lost_says_so(self):
        run = LostAudit(rc=0)
        err = io.StringIO()
        with mock.patch.object(clh, "_REPO", self.clone), mock.patch.object(sys, "stderr", err):
            self.assertTrue(clh.relaunch_core(self.ws, "L", run=run))
        self.assertEqual(err.getvalue().splitlines(), [f"{clh.RELAUNCH_CALLER}: {line}" for line in LOST])
        trace = (self.ws / "logs" / CR.RESTART_TRACE).read_text().splitlines()
        self.assertEqual([line.split(" ", 1)[1] for line in trace],
                         [f"[audit] {clh.RELAUNCH_CALLER}: {line}" for line in LOST])

    def test_elsewhere_the_given_launcher_restarts(self):
        run = Run(rc=0)
        with mock.patch.object(clh, "_REPO", self.clone):
            self.assertTrue(clh.relaunch_core(self.ws, "L", run=run))
        [argv] = run.argvs()
        self.assertEqual(argv[:3], [sys.executable, CR.RECORDER, "start-cli"])
        self.assertEqual(argv[argv.index("--start-cli") + 1], "L")


class RecoverCore(Base):
    """health-check --recover-core's restart takes the same route."""

    def patched(self, repo):
        for name, value in (("REPO_DIR", repo), ("WORKSPACE_DIR", self.ws)):
            p = mock.patch.object(HC, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_in_the_bundle_it_is_the_ctl(self):
        self.desktop()
        self.patched(self.bundle)
        run = Run(rc=0)
        self.assertTrue(HC._default_core_restart(run=run))
        restart = run.argvs()[-1]
        self.assertEqual(restart[restart.index("--caller") + 1], HC.RECOVER_CALLER)

    def test_in_the_bundle_without_its_ctl_it_restarts_nothing(self):
        self.desktop()
        self.ctl.unlink()
        self.patched(self.bundle)
        run = Run()
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertFalse(HC._default_core_restart(run=run))
        self.assertEqual(run.calls, [])

    def test_a_recovery_that_succeeds_with_its_audit_lines_lost_says_so(self):
        self.patched(self.clone)
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertTrue(HC._default_core_restart(run=LostAudit(rc=0)))
        self.assertEqual(err.getvalue().splitlines(), [f"{HC.RECOVER_CALLER}: {line}" for line in LOST])
        trace = (self.ws / "logs" / CR.RESTART_TRACE).read_text()
        self.assertEqual(trace.count("[audit] "), 2)
        self.assertNotIn("launcher noise", trace + err.getvalue())

    def test_only_the_recorders_own_reports_are_relayed_and_at_most_a_few(self):
        many = "\n".join(f"core_restart: audit intent line not written (attempt att_{i:012x})" for i in range(9))
        self.assertEqual(len(CR.audit_diagnostics(many)), CR.MAX_AUDIT_DIAGNOSTICS)
        for other in (None, "", "core_restart: audit intent line not written (attempt att_1) and more",
                      "x core_restart: audit outcome line not written (attempt att_0123456789ab)"):
            self.assertEqual(CR.audit_diagnostics(other), [])
        self.assertEqual(CR.report_audit_diagnostics(self.ws, "c", "launcher noise"), [])
        self.assertFalse((self.ws / "logs").exists(), "nothing is written when there is nothing to relay")
        (self.ws / "logs").write_text("not a directory")
        err = io.StringIO()
        self.assertEqual(CR.report_audit_diagnostics(self.ws, "c", LOST[0], err=err), [LOST[0]])
        self.assertEqual(err.getvalue(), f"c: {LOST[0]}\n", "an unwritable trace still leaves the stderr report")

    def test_elsewhere_it_is_start_cli(self):
        self.patched(self.clone)
        run = Run(rc=1)
        self.assertFalse(HC._default_core_restart(run=run))
        [argv] = run.argvs()
        self.assertEqual(argv[:3], [sys.executable, CR.RECORDER, "start-cli"])
        self.assertEqual(argv[argv.index("--start-cli") + 1],
                         str(self.clone / "src" / "agent" / "start-cli.sh"))

    def test_without_a_launcher_it_does_nothing(self):
        self.patched(self.root / "nowhere")
        run = Run()
        self.assertFalse(HC._default_core_restart(run=run))
        self.assertEqual(run.calls, [])


if __name__ == "__main__":
    unittest.main()
