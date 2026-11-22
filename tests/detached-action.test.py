#!/usr/bin/env python3
"""A restart that closes its caller's session runs outside it, and its outcome is known by a deadline.

The core's Restart core / Switch runtime / a live core's Retry now go through
`detached_action.spawn`: the launcher's --restart kills the tmux session the monitor runs in.
Reproduced with real processes and a real kill: a "monitor" process group stands for the core's
tmux session; the monitor runs the production executor (`recovery_actions.run_pending`) on a
tapped card, with only the launcher replaced by a script that kills that process group the way
`tmux kill-session` does, then starts "the new core". The restart must still finish and report.

Run: python3 tests/detached-action.test.py
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

import detached_action as D  # noqa: E402
import recovery_gate  # noqa: E402

# The fake launcher: it takes the monitor's whole session down first, as --restart does, then
# finishes the restart. A launcher that is a child of the monitor dies at the first line.
LAUNCHER = textwrap.dedent('''\
    #!/bin/bash
    kill -TERM -"$MONITOR_PGID" 2>/dev/null
    sleep 1
    echo up > "$MARKER"
''')

# The monitor: the production executor on a card the test already tapped, in its own group.
MONITOR = textwrap.dedent('''
    import os, sys, time
    from pathlib import Path
    sys.path.insert(0, {src!r})
    os.environ["MONITOR_PGID"] = str(os.getpgrp())
    import recovery_actions as RA
    from hitl.manager import HitlManager, HitlStore
    ws = Path(sys.argv[1])
    RA.REPO = Path(sys.argv[2])  # only the launcher is a stand-in
    mgr = HitlManager(HitlStore(ws / "state" / "hitl" / "requirements"))
    RA.run_pending(mgr, RA.Target(ws, "/nonexistent.sock", "sutando-core"))
    (ws / "monitor-finished").write_text("yes")
    time.sleep(30)  # a live monitor keeps ticking until its session is closed
''')


class SurvivesItsCallersSession(unittest.TestCase):
    def test_a_tapped_restart_finishes_although_it_closes_the_monitor_that_ran_it(self):
        from hitl.manager import HitlManager, HitlStore
        from hitl.replies import REPLY_FIELD, HitlReplyHandler
        import importlib.util as u
        spec = u.spec_from_file_location("ciw_detached", SRC / "core-input-watch.py")
        M = u.module_from_spec(spec)
        spec.loader.exec_module(M)
        with tempfile.TemporaryDirectory() as tmp:
            ws, fake = Path(tmp) / "ws", Path(tmp) / "repo"
            for d in ("state", "tasks", "results", "logs"):
                (ws / d).mkdir(parents=True)
            launcher = fake / "src" / "agent" / "start-cli.sh"
            launcher.parent.mkdir(parents=True)
            launcher.write_text(LAUNCHER)
            launcher.chmod(0o755)
            mgr = HitlManager(HitlStore(ws / "state" / "hitl" / "requirements"))
            req = M.escalate(mgr, "blocked-human", "awaiting user: unknown", "unknown",
                             "  Something unexpected\n  ❯ do the thing?\n  Esc to cancel", "sutando-core",
                             runtime="claude", workspace=ws)
            mgr.record_projection(req.id, req.revision, "$card", {"room_id": "!dm:s.space"})
            handler = HitlReplyHandler(mgr, "@owner:s.space", workspace=ws, log=lambda m: None)
            handler.offer({"type": "message.created", "event_id": "$c", "actor_id": "@owner:s.space",
                           "room_id": "!dm:s.space", "content": {REPLY_FIELD: {
                               "hitl_id": req.id, "expected_revision": req.revision,
                               "action_id": "restart_core", "guard": req.guard}}})
            self.assertEqual(handler.last_outcome, "applied")
            marker = Path(tmp) / "new-core-up"
            script = Path(tmp) / "monitor.py"
            script.write_text(MONITOR.format(src=str(SRC)))
            env = dict(os.environ, MARKER=str(marker))
            monitor = subprocess.Popen([sys.executable, str(script), str(ws), str(fake)], env=env,
                                       start_new_session=True, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            try:
                deadline = time.time() + 20
                while time.time() < deadline and not marker.exists():
                    time.sleep(0.1)
                self.assertTrue(marker.exists(), "the restart died with the monitor's session")
                self.assertIsNotNone(monitor.wait(timeout=5), "the monitor's session was closed")
                self.assertTrue((ws / "monitor-finished").exists(), "the monitor recorded the tap first")
                key = f"restart:{req.id}:{mgr.get(req.id).revision - 1}"
                for _ in range(50):
                    if D.status(ws, key).state != D.RUNNING:
                        break
                    time.sleep(0.1)
                self.assertEqual(D.status(ws, key).state, D.DONE, "the restart reported its own outcome")
                receipt = json.loads((ws / "state" / "recovery" / "receipts" / f"{req.id}.json").read_text())
                self.assertEqual(receipt["outcome"], "done")
            finally:
                for p in (monitor,):
                    if p.poll() is None:
                        os.killpg(p.pid, signal.SIGKILL)
                subprocess.run(["pkill", "-f", str(launcher)], check=False)


class Records(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_running_until_reported_first_report_wins_and_silence_expires(self):
        self.assertEqual(D.status(self.ws, "k"), (None, None, None))
        D.begin(self.ws, "k", now=100.0, deadline_s=10)
        self.assertEqual(D.status(self.ws, "k", now=105).state, D.RUNNING)
        self.assertEqual(D.status(self.ws, "k", now=111).state, D.EXPIRED)
        self.assertTrue(D.report(self.ws, "k", D.FAILED, "why", 3, now=106))
        self.assertFalse(D.report(self.ws, "k", D.DONE, now=107), "the first report stands")
        self.assertEqual(D.status(self.ws, "k", now=200), (D.FAILED, "why", 3))
        self.assertFalse(D.report(self.ws, "nobody", D.DONE), "nothing was begun under that key")
        D.begin(self.ws, "j")
        self.assertFalse(D.report(self.ws, "j", "maybe"))

    def test_an_unreadable_record_is_no_action(self):
        D._path(self.ws, "k").parent.mkdir(parents=True)
        D._path(self.ws, "k").write_text("[")
        self.assertEqual(D.status(self.ws, "k").state, None)
        D._path(self.ws, "k").write_text("[1]")
        self.assertEqual(D.status(self.ws, "k").state, None)

    def test_spawn_starts_the_wrapper_in_its_own_session_and_a_failed_start_reports_failed(self):
        calls = []
        self.assertTrue(D.spawn(self.ws, "k", ["bash", "x"], env={"A": "1"}, log=self.ws / "logs" / "l.log",
                                popen=lambda argv, **kw: calls.append((argv, kw)), after_gate="g"))
        argv, kw = calls[0]
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["env"][D.KEY_ENV], "k")
        self.assertEqual(argv[argv.index("--after-gate") + 1], "g")
        self.assertEqual(argv[argv.index("--") + 1:], ["bash", "x"])
        self.assertEqual(D.status(self.ws, "k").state, D.RUNNING)

        def boom(argv, **kw):
            raise OSError("no")
        self.assertFalse(D.spawn(self.ws, "j", ["bash", "x"], popen=boom))
        self.assertEqual(D.status(self.ws, "j"), (D.FAILED, "could not start", None))

    def test_the_wrapper_reports_the_exit_code_unless_the_action_reported_first(self):
        D.begin(self.ws, "ok")
        self.assertEqual(D._run(self.ws, "ok", ["true"], runner=lambda a, **k: subprocess.CompletedProcess(a, 0)), 0)
        self.assertEqual(D.status(self.ws, "ok").state, D.DONE)
        D.begin(self.ws, "bad")
        self.assertEqual(D._run(self.ws, "bad", ["x"], runner=lambda a, **k: subprocess.CompletedProcess(a, 4)), 1)
        self.assertEqual(D.status(self.ws, "bad"), (D.FAILED, "exit", 4))

        def gone(a, **k):
            raise OSError("no such file")
        D.begin(self.ws, "gone")
        self.assertEqual(D._run(self.ws, "gone", ["x"], runner=gone), 1)
        self.assertEqual(D.status(self.ws, "gone").state, D.FAILED)

    def test_a_late_or_orphaned_child_never_acts(self):
        ran = []
        runner = lambda a, **k: ran.append(a) or subprocess.CompletedProcess(a, 0)  # noqa: E731
        D.begin(self.ws, "done-already")
        D.report(self.ws, "done-already", D.FAILED, "x")
        self.assertEqual(D._run(self.ws, "done-already", ["x"], runner=runner), 1)
        D.begin(self.ws, "expired", now=1.0, deadline_s=1)
        self.assertEqual(D._run(self.ws, "expired", ["x"], runner=runner), 1)
        self.assertEqual(ran, [], "a child whose action already ended ran its command")

    def test_the_wrapper_waits_for_its_callers_record_then_acts(self):
        waited = []
        D.begin(self.ws, "k")
        D._run(self.ws, "k", ["x"], runner=lambda a, **k: subprocess.CompletedProcess(a, 0), after_gate="g",
               wait=lambda ws, gate: waited.append(gate))
        self.assertEqual(waited, ["g"])
        recovery_gate.admit(self.ws, "g", 10, now=10)
        clock = iter([0.0, 0.1, 0.2, 99.0])
        D._wait_for_gate(self.ws, "g", wait_s=5, sleep=lambda s: None, clock=lambda: next(clock))
        recovery_gate.finish(self.ws, "g", "done", now=11)
        D._wait_for_gate(self.ws, "g", wait_s=5, sleep=lambda s: self.fail("waited on a finished claim"))

    def test_main_runs_the_command_after_the_separator(self):
        D.begin(self.ws, "m")
        with mock.patch.object(D, "_run", return_value=0) as run:
            self.assertEqual(D.main(["run", "--workspace", str(self.ws), "--key", "m", "--after-gate", "g",
                                     "--", "echo", "hi"]), 0)
        self.assertEqual(run.call_args[0][2], ["echo", "hi"])
        self.assertEqual(run.call_args[1]["after_gate"], "g")

    def test_detach_with_a_log_opens_and_closes_it(self):
        log = self.ws / "logs" / "x.log"
        self.assertTrue(D.detach(["true"], log=log, popen=lambda argv, **kw: kw["stdout"].write(b"")))
        self.assertTrue(log.exists())

        def boom(argv, **kw):
            raise OSError("no")
        self.assertFalse(D.detach(["true"], log=log, popen=boom))


if __name__ == "__main__":
    unittest.main()
