#!/usr/bin/env python3
"""The remedy resumes a dead worker on its OWN session, and does nothing else.

What is pinned here is what a timer with nobody watching must get right: it uses
the socket the worker last ran on rather than an environment it does not have,
it leaves honest run records behind, it never touches a paused or a running
worker, and an `escalate` decision passes through it untouched.

Run: python3 tests/skills/worker-pool/pool-remedy.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import pool_remedy as rem  # noqa: E402

sup, sw, ps, wi = rem.sup, rem.sw, rem.ps, rem.wi
SOCK = "/recorded/app/run/tmux.sock"


class FakeTmux:
    """has-session from a set of live names; the launcher creates the session,
    unless told to fail. Records every argv with its env."""

    def __init__(self, runtime="claude", launcher_fails=False):
        self.calls, self.envs, self.live = [], [], set()
        self.runtime, self.launcher_fails = runtime, launcher_fails

    def __call__(self, argv, **kw):
        cp = subprocess.CompletedProcess
        self.calls.append(argv)
        self.envs.append(dict(kw.get("env") or {}))
        if argv[0] == "bash" and argv[1].endswith("sutando-config.sh"):
            return cp(argv, 0, self.runtime + "\n", "")
        if len(argv) > 2 and argv[2] == "watcher-sentinel":
            # The spawner's per-instance-sentinel safety check stays ON in the code
            # under test, so the fake answers it: one path per instance identity.
            return cp(argv, 0, "sentinel-" + (kw.get("env") or {})["SUTANDO_INSTANCE_ID"], "")
        if argv[0] == "bash" and argv[1].endswith("start-cli.sh"):
            if self.launcher_fails:
                return cp(argv, 1, "", "claude: not logged in")
            self.live.add((kw.get("env") or {}).get("SUTANDO_TMUX_SESSION", ""))
            return cp(argv, 0, "Started detached.", "")
        if len(argv) > 3 and argv[3] == "has-session":
            name = argv[-1].lstrip("=")
            return (cp(argv, 0, "", "") if name in self.live
                    else cp(argv, 1, "", f"can't find session: {name}"))
        return cp(argv, 0, "", "")

    def launches(self):
        return [(a, e) for a, e in zip(self.calls, self.envs)
                if a[0] == "bash" and a[1].endswith("start-cli.sh")]

    def probes(self):
        return [a for a in self.calls if len(a) > 3 and a[3] == "has-session"]


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.t = FakeTmux()
        first = sw.spawn(self.ws, REPO, cwd=str(REPO), socket=SOCK, label="alpha",
                         runner=self.t, require_sentinel=False)
        self.wid, self.session, self.inbox = (first["worker_id"], first["runtime_session_id"],
                                              first["delivery_dir"])
        self.t.live.clear()                       # the worker died
        self.launched_before = len(self.t.launches())

    def recover(self, **kw):
        return rem.recover(self.ws, REPO, self.wid, runner=self.t, **kw)


class ItResumesTheSameWorker(Base):
    def test_a_dead_worker_comes_back_on_its_own_session(self):
        out = self.recover()
        self.assertEqual(out["outcome"], rem.RECOVERED)
        self.assertEqual(out["session_id"], self.session)
        _, env = self.t.launches()[-1]
        self.assertEqual(env.get("SUTANDO_CLAUDE_RESUME"), self.session,
                         "the runtime was not told to RESUME the recorded conversation")

    def test_identity_label_and_inbox_are_untouched(self):
        self.recover()
        self.assertEqual([d.name for d in (self.ws / "state/workers").iterdir()], [self.wid],
                         "recovery minted a second worker")
        self.assertTrue(Path(self.inbox).is_dir())
        self.assertEqual(len(wi.sessions(self.ws, self.wid)), 1,
                         "recovery recorded a new session; it is the same conversation")

    def test_it_uses_the_RECORDED_socket_not_the_environments(self):
        old = os.environ.get("SUTANDO_TMUX_SOCKET")
        os.environ["SUTANDO_TMUX_SOCKET"] = "/wrong/env/default.sock"
        try:
            self.recover()
        finally:
            os.environ.pop("SUTANDO_TMUX_SOCKET") if old is None else os.environ.__setitem__(
                "SUTANDO_TMUX_SOCKET", old)
        sockets = {a[2] for a in self.t.probes()}
        self.assertEqual(sockets, {SOCK},
                         "a timer has no SUTANDO_TMUX_SOCKET; falling back to it probes and "
                         "launches on a tmux server the worker never ran on")


class ItLeavesHonestRunRecords(Base):
    def test_every_dead_run_is_closed_and_exactly_one_is_open_after(self):
        wi.start_incarnation(self.ws, self.wid, self.session, tmux_socket=SOCK,
                             tmux_session=wi.tmux_session_name(self.wid))   # a second stale run
        out = self.recover()
        runs = wi.incarnations(self.ws, self.wid)
        self.assertEqual(len(out["closed_runs"]), 2)
        self.assertEqual([r["end_reason"] for r in runs[:-1]], ["crashed", "crashed"])
        self.assertEqual([r["ended_at"] is None for r in runs], [False, False, True],
                         "the only open run must be the one that was just started")

    def test_a_failed_launch_leaves_NO_open_run(self):
        self.t.launcher_fails = True
        out = self.recover()
        self.assertEqual(out["outcome"], rem.FAILED)
        self.assertIn("not logged in", out["why"])
        self.assertEqual([r for r in wi.incarnations(self.ws, self.wid) if r["ended_at"] is None],
                         [], "a recovery that failed left a run open; the next probe reads "
                             "it as this worker's live run")


class WhatItRefusesToTouch(Base):
    def test_a_paused_worker_is_never_relaunched(self):
        (wi.worker_dir(self.ws, self.wid) / sup.PAUSED_MARKER).touch()
        self.assertEqual(self.recover()["outcome"], rem.PAUSED)
        self.assertEqual(len(self.t.launches()), self.launched_before)
        self.assertTrue((wi.worker_dir(self.ws, self.wid) / sup.PAUSED_MARKER).exists(),
                        "the remedy cleared an owner's pause")

    def test_a_worker_that_is_already_running_is_left_alone(self):
        self.t.live.add(wi.tmux_session_name(self.wid))
        out = self.recover()
        self.assertEqual(out["outcome"], rem.ALREADY_RUNNING)
        self.assertEqual(len(self.t.launches()), self.launched_before)
        self.assertIsNone(wi.incarnations(self.ws, self.wid)[-1]["ended_at"],
                          "it closed the run of a worker that is alive")

    def test_a_worker_with_no_recorded_session_cannot_be_resumed(self):
        wi.sessions_path(self.ws, self.wid).write_text(json.dumps({"sessions": []}))
        self.assertEqual(self.recover()["outcome"], rem.NO_SESSION)
        self.assertEqual(len(self.t.launches()), self.launched_before)


class ApplyingATick(Base):
    def test_only_recover_acts_and_escalate_passes_through(self):
        other = wi.new_worker_id()
        seen = []
        out = rem.apply(self.ws, REPO,
                        {self.wid: ps.RECOVER, other: ps.ESCALATE, "third": ps.NOTHING},
                        spawn=lambda *a, **k: seen.append(k) or {"runtime_session_id": "s"},
                        runner=self.t)
        self.assertEqual(list(out["recoveries"]), [self.wid])
        self.assertEqual(out["escalations"], [other],
                         "asking the owner is the core's; a timer must not swallow it")
        self.assertEqual(len(seen), 1)

    def test_the_whole_loop_observe_decide_remedy_observe(self):
        pr = sup.pr
        pr.register_worker(self.ws, self.wid, "alpha")
        for now in (1000.0, 1030.0, 1060.0, 1095.0):
            tick = sup.tick(self.ws, now, runner=self.t)
        self.assertEqual(tick["decisions"][self.wid], ps.RECOVER)
        done = rem.apply(self.ws, REPO, tick["decisions"], runner=self.t)
        self.assertEqual(done["recoveries"][self.wid]["outcome"], rem.RECOVERED)
        after = sup.observe(self.ws, 1100.0, runner=self.t)[self.wid]
        self.assertIs(after.session_alive, True, "the worker is not back after a 'recovered'")


class OneCopyOfEachModule(unittest.TestCase):
    def test_a_module_already_loaded_is_reused_not_loaded_twice(self):
        self.assertIs(rem._sibling("spawn_worker"), rem.sw,
                      "a second copy of spawn_worker has its own SpawnRefused, so "
                      "`except sw.SpawnRefused` would stop catching the real one")


class TheCommandLine(Base):
    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = rem.main(["--workspace", str(self.ws), "--repo", str(REPO), *argv])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_exactly_one_mode_is_required(self):
        self.assertEqual(self._run()[0], 2)

    def test_a_dry_run_neither_remedies_nor_advances_the_ladder(self):
        sup.pr.register_worker(self.ws, self.wid, "alpha")
        rc, out, _ = self._run("--sweep", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["dry_run"])
        self.assertFalse(sup.state_path(self.ws).exists())

    def test_a_malformed_id_is_refused(self):
        rc, _, err = self._run("--recipient", "../../etc")
        self.assertEqual(rc, 2)
        self.assertIn("refused", err)

    def test_a_failed_recovery_is_a_nonzero_exit(self):
        real = rem.apply
        rem.apply = lambda *a, **k: {"recoveries": {"w": {"outcome": rem.FAILED}},
                                     "escalations": []}
        try:
            rc, _, _ = self._run("--sweep")
        finally:
            rem.apply = real
        self.assertEqual(rc, 1, "a timer's log is its only witness: failure must be visible")


if __name__ == "__main__":
    unittest.main(verbosity=2)
