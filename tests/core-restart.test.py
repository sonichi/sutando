#!/usr/bin/env python3
"""core_restart: the one route for a core restart, and its callers outside the recovery cards.

In the desktop bundle (`<app>/engine/<checkout>`) a restart is the app's sutando-ctl.sh
restart-core between an intent and an outcome line in state/recovery/audit.jsonl, refused when the
ctl is missing or takes no launch lock; elsewhere it is start-cli.sh --restart.
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
import subprocess
import tempfile
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
        self.assertEqual(argv, ["bash", str(self.clone / "src" / "agent" / "start-cli.sh"),
                                "--runtime", "codex", "--restart", "--visible"])
        self.assertEqual(via, CR.VIA_START_CLI)

    def test_a_given_launcher_is_kept_outside_the_app(self):
        argv, _via = self.route(self.clone, start_cli="/x/L")
        self.assertEqual(argv, ["bash", "/x/L", "--restart"])

    def test_a_clone_outside_the_bundle_falls_back_to_start_cli(self):
        self.desktop()
        run = Run()
        argv, via = self.route(self.clone, run)
        self.assertEqual((argv[-1], via, run.calls), ("--restart", CR.VIA_FALLBACK, []))

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
        base = {"event": "ctl_restart", "via": "ctl", "caller": "recovery-card", "hitl_id": "hitl_1",
                "revision": 4, "at": 100}
        self.assertEqual(intent, dict(base, phase="intent"))
        self.assertEqual(outcome, dict(base, phase="outcome", outcome="done", rc=0))

    def test_a_busy_lock_is_a_failed_outcome_with_its_code(self):
        rc = CR.run_ctl(self.ctl, self.ws, "graceful-restart", runner=Run(rc=CR.CTL_LAUNCH_LOCK_BUSY), env={})
        self.assertEqual(rc, CR.CTL_LAUNCH_LOCK_BUSY)
        intent, outcome = self.audit()
        self.assertNotIn("hitl_id", intent, "no card, so only the caller's name")
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
        real = sutando_config.persist_desktop_runtime_selection
        calls = []

        def persist(runtime, repo=None, check=False):
            calls.append(runtime)
            if len(calls) > 1:
                raise OSError("read-only")
            return real(runtime, repo, check)
        with mock.patch.object(sutando_config, "persist_desktop_runtime_selection", side_effect=persist):
            rc = CR.run_ctl(self.ctl, self.ws, "x", runtime="codex", runner=Run(rc=1), env={}, repo=self.bundle)
        self.assertEqual((rc, calls, self.audit()[-1]["outcome"]), (1, ["codex", "claude"], "failed"))

    def test_an_unselectable_runtime_is_refused_before_the_ctl_runs(self):
        self.desktop()
        (self.app / "runtime.json").write_text("[]")  # not an object: never replaced
        run = Run()
        rc = CR.run_ctl(self.ctl, self.ws, "x", runtime="codex", runner=run, env={}, repo=self.bundle)
        self.assertEqual((rc, run.calls), (CR.REFUSED_EXIT, []))
        self.assertEqual((self.audit()[-1]["outcome"], self.audit()[-1]["reason"]), ("refused", "runtime-not-selectable"))


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

    def test_outside_the_bundle_start_cli_replaces_the_process(self):
        execv = mock.Mock()
        CR.restart(self.ws, "graceful-restart", repo=self.clone, extra=["--visible"], start_cli="/x/stub",
                   runner=Run(), execv=execv)
        execv.assert_called_once()
        self.assertEqual(execv.call_args[0][:2], ("bash", ["bash", "/x/stub", "--restart", "--visible"]))

    def test_in_the_bundle_the_ctl_runs_here_between_its_lines(self):
        self.desktop()
        run, execv = Run(rc=0), mock.Mock()
        self.assertEqual(CR.restart(self.ws, "graceful-restart", repo=self.bundle, runner=run, execv=execv), 0)
        execv.assert_not_called()
        self.assertEqual(run.argvs()[-1], ["bash", str(self.ctl), "restart-core"])
        self.assertEqual([r["phase"] for r in self.audit()], ["intent", "outcome"])

    def test_the_cli_dispatches_both_verbs(self):
        with mock.patch.object(CR, "run_ctl", return_value=12) as rc_:
            self.assertEqual(CR.main(["ctl", "--ctl", "/c", "--workspace", "/w", "--caller", "c",
                                      "--hitl-id", "h", "--revision", "2", "--runtime", "codex"]), 12)
        rc_.assert_called_once_with(Path("/c"), Path("/w"), "c", hitl_id="h", revision=2, runtime="codex")
        with mock.patch.object(CR, "restart", return_value=0) as r:
            self.assertEqual(CR.main(["restart", "--workspace", "/w", "--caller", "g", "--repo", "/r",
                                      "--start-cli", "/s", "--", "--visible"]), 0)
        r.assert_called_once_with(Path("/w"), "g", runtime=None, extra=["--visible"], start_cli="/s", repo=Path("/r"))


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

    def test_elsewhere_the_given_launcher_restarts(self):
        run = Run(rc=0)
        with mock.patch.object(clh, "_REPO", self.clone):
            self.assertTrue(clh.relaunch_core(self.ws, "L", run=run))
        self.assertEqual(run.argvs(), [["bash", "L", "--restart"]])


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

    def test_elsewhere_it_is_start_cli(self):
        self.patched(self.clone)
        run = Run(rc=1)
        self.assertFalse(HC._default_core_restart(run=run))
        self.assertEqual(run.argvs(), [["bash", str(self.clone / "src" / "agent" / "start-cli.sh"), "--restart"]])

    def test_without_a_launcher_it_does_nothing(self):
        self.patched(self.root / "nowhere")
        run = Run()
        self.assertFalse(HC._default_core_restart(run=run))
        self.assertEqual(run.calls, [])


if __name__ == "__main__":
    unittest.main()
