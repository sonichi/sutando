#!/usr/bin/env python3
"""The remedy recovers a dead worker under its OWN identity and inbox.

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
import plistlib
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

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
        self.loaded = set()
        self.runtime, self.launcher_fails = runtime, launcher_fails

    def __call__(self, argv, **kw):
        cp = subprocess.CompletedProcess
        self.calls.append(argv)
        self.envs.append(dict(kw.get("env") or {}))
        if argv[0] == "launchctl":
            if argv[1] == "print":
                return cp(argv, 0 if argv[2] in self.loaded else 113, "", "")
            if argv[1] == "bootout":
                self.loaded.discard(argv[2])
            if argv[1] == "bootstrap":
                with open(argv[3], "rb") as fh:
                    label = plistlib.load(fh)["Label"]
                self.loaded.add(f"gui/{os.getuid()}/{label}")
            return cp(argv, 0, "", "")
        if argv[0] == "bash" and argv[1].endswith("sutando-config.sh"):
            return cp(argv, 0, self.runtime + "\n", "")
        if len(argv) > 2 and argv[2] == "watcher-sentinel":
            # The spawner's per-instance-sentinel safety check stays ON in the code
            # under test, so the fake answers it: one path per instance identity.
            return cp(argv, 0, "sentinel-" + (kw.get("env") or {})["SUTANDO_INSTANCE_ID"], "")
        if argv[0] == "bash" and argv[1].endswith("launch-worker-session.sh"):
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
                if a[0] == "bash" and a[1].endswith("launch-worker-session.sh")]

    def probes(self):
        return [a for a in self.calls if len(a) > 3 and a[3] == "has-session"]


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.la = self.ws / "LaunchAgents"
        real_ensure = sw.ensure_remedy_timer
        patcher = mock.patch.object(
            sw, "ensure_remedy_timer",
            side_effect=lambda ws, repo, **kw: real_ensure(ws, repo, launch_agents=self.la, **kw))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.t = FakeTmux()
        first = sw.spawn(self.ws, REPO, cwd=str(REPO), socket=SOCK, label="alpha",
                         runner=self.t, require_sentinel=False)
        if sys.platform == "darwin":
            self.assertEqual(Path(first["remedy_timer"]["plist"]).parent, self.la)
        self.wid, self.session, self.inbox = (first["worker_id"], first["runtime_session_id"],
                                              first["delivery_dir"])
        sup.pr.register_worker(self.ws, self.wid, "alpha", runtime="claude")
        self.t.live.clear()                       # the worker died
        self.launched_before = len(self.t.launches())

    def recover(self, **kw):
        return rem.recover(self.ws, REPO, self.wid, runner=self.t, **kw)


class ItResumesTheSameWorker(Base):
    def test_shared_recovery_checks_suspension_before_probes_or_writes(self):
        rem.suspend(self.ws, "renewed-stop")
        before = {p: p.read_bytes() for p in wi.worker_dir(self.ws, self.wid).glob("*.json")}
        with mock.patch.object(sup, "is_paused", side_effect=AssertionError("past guard")), \
                mock.patch.object(wi, "sessions", side_effect=AssertionError("read lineage")), \
                mock.patch.object(sw, "session_state", side_effect=AssertionError("probed")), \
                mock.patch.object(rem, "close_dead_runs", side_effect=AssertionError("closed")), \
                mock.patch.object(sw, "spawn", side_effect=AssertionError("launched")):
            self.assertEqual(self.recover(), {"worker_id": self.wid, "outcome": rem.SUSPENDED})
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(len(self.t.launches()), self.launched_before)

    def test_another_workspaces_suspension_does_not_block_recovery(self):
        rem.suspend(self.ws / "other", "unrelated-stop")
        self.assertEqual(self.recover()["outcome"], rem.RECOVERED)
        self.assertEqual(len(self.t.launches()), self.launched_before + 1)

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


class RecoverySessionSelection(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        timer = mock.patch.object(sw, "ensure_remedy_timer", return_value={"installed": False})
        timer.start()
        self.addCleanup(timer.stop)
        self.t = FakeTmux()
        self.a_dir, self.b_dir = self.ws / "project-a", self.ws / "project-b"
        self.a_dir.mkdir()
        self.b_dir.mkdir()
        first = wi.create_worker(self.ws, runtime="claude", cwd=str(self.a_dir),
                                 tmux_socket=SOCK)
        self.wid, self.a = first["worker_id"], first["runtime_session_id"]
        wi.end_incarnation(self.ws, self.wid, first["incarnation_id"], "exited")
        self.b = "later-session-b"
        wi.record_session(self.ws, self.wid, self.b, runtime="claude",
                          relation=wi.RELATION_NEW, cwd=str(self.b_dir))
        b_run = wi.start_incarnation(self.ws, self.wid, self.b, tmux_socket=SOCK,
                                     tmux_session=wi.tmux_session_name(self.wid))
        wi.end_incarnation(self.ws, self.wid, b_run["incarnation_id"], "exited")
        sw.pr.register_worker(self.ws, self.wid, "alpha", room="test-room", runtime="claude")
        sw.spawn(self.ws, REPO, resume=self.a, socket=SOCK, runner=self.t)
        self.t.live.clear()
        other = wi.create_worker(self.ws, runtime="claude", cwd=str(self.b_dir))
        self.other, self.other_session = other["worker_id"], other["runtime_session_id"]
        sw.pr.register_worker(self.ws, self.other, "beta", runtime="claude")
        import pool_router as router
        self.router = router
        (self.ws / "tasks").mkdir()
        for task_id in ("task-pending", "task-accepted"):
            rem.pd.payload_path(self.ws, task_id).write_text(
                "source: test\nchannel_id: test-room\ntask: payload for " + task_id + "\n")
            self.assertEqual(router.route(self.ws, {"id": task_id, "channel_id": "test-room"})
                             ["delivered"], [self.wid])
        rem.pd.accept(rem.pd.deliveries_dir(self.ws, self.wid) / "task-accepted.txt")

    def snapshot(self, *, include_runs=False):
        mutable = {wi.incarnations_path(self.ws, self.wid), wi.current_path(self.ws, self.wid)}
        return {str(p.relative_to(self.ws)): p.read_bytes() for p in self.ws.rglob("*")
                if p.is_file() and (include_runs or p not in mutable)}

    def recover(self, *, reopened=False):
        if reopened:
            code = """
import json, runpy, sys
from unittest import mock
m = runpy.run_path(sys.argv[1], run_name='recovery_fixture')
rem, t = m['rem'], m['FakeTmux']()
with mock.patch.object(rem.sw, 'ensure_remedy_timer', return_value={'installed': False}):
    out = rem.recover(sys.argv[2], sys.argv[3], sys.argv[4], runner=t)
keys = ('SUTANDO_CLAUDE_RESUME', 'SUTANDO_CLAUDE_WORKING_DIR', 'SUTANDO_TMUX_SOCKET',
        'SUTANDO_WORKER_ID', 'SUTANDO_INSTANCE_ID', 'SUTANDO_TASKS_DIR')
print(json.dumps({'out': out, 'launches': [{k: e.get(k) for k in keys}
                                         for _, e in t.launches()]}))
"""
            cp = subprocess.run([sys.executable, "-c", code, __file__, str(self.ws),
                                 str(REPO), self.wid], env=dict(os.environ),
                                capture_output=True, text=True, timeout=20)
            self.assertEqual(cp.returncode, 0, cp.stderr)
            result = json.loads(cp.stdout)
            self.assertEqual(len(result["launches"]), 1)
            return result["out"], result["launches"][0]
        before = len(self.t.launches())
        out = rem.recover(self.ws, REPO, self.wid, runner=self.t)
        self.assertEqual(len(self.t.launches()), before + 1)
        return out, self.t.launches()[-1][1]

    def assert_selected(self, out, env, sid, folder):
        self.assertEqual(out["outcome"], rem.RECOVERED)
        self.assertEqual(out["session_id"], sid)
        for key, value in {"SUTANDO_CLAUDE_RESUME": sid,
                           "SUTANDO_CLAUDE_WORKING_DIR": str(folder),
                           "SUTANDO_TMUX_SOCKET": SOCK, "SUTANDO_WORKER_ID": self.wid,
                           "SUTANDO_INSTANCE_ID": self.wid,
                           "SUTANDO_TASKS_DIR": str(rem.pd.deliveries_dir(self.ws, self.wid))}.items():
            self.assertEqual(env[key], value, key)

    def test_latest_incarnation_selects_a_and_survives_reopening(self):
        for reopened in (False, True):
            with self.subTest(reopened=reopened):
                self.t.live.clear()
                before, runs = self.snapshot(), wi.incarnations(self.ws, self.wid)
                with mock.patch.dict(os.environ, {"SUTANDO_TMUX_SOCKET": "/wrong/default.sock"}):
                    out, env = self.recover(reopened=reopened)
                self.assert_selected(out, env, self.a, self.a_dir)
                self.assertEqual(self.snapshot(), before)
                after = wi.incarnations(self.ws, self.wid)
                self.assertEqual(len(after), len(runs) + 1)
                self.assertEqual(after[:-2], runs[:-1])
                self.assertEqual(after[-2], {**runs[-1], "ended_at": after[-2]["ended_at"],
                                             "end_reason": "crashed"})
                self.assertIsNotNone(after[-2]["ended_at"])
                self.assertEqual(out["closed_runs"], [runs[-1]["incarnation_id"]])
                self.assertEqual(after[-1]["session_id"], self.a)
                self.assertIsNone(after[-1]["ended_at"])
                self.assertEqual(wi.current(self.ws, self.wid), {
                    "session_id": self.a, "incarnation_id": after[-1]["incarnation_id"]})
                for task_id in ("task-pending", "task-accepted"):
                    self.assertEqual(self.router.committed_recipient(self.ws, task_id), self.wid)
                self.assertEqual([p.name for p in rem.pd.pending(self.ws, self.wid)],
                                 ["task-pending.txt"])
                self.assertEqual([p.name for p in rem.pd.accepted(self.ws, self.wid)],
                                 ["task-accepted.accepted"])

    def test_failed_recovery_retries_a_even_after_current_is_cleared(self):
        before, runs = self.snapshot(), wi.incarnations(self.ws, self.wid)
        self.t.launcher_fails = True
        out, env = self.recover()
        self.assertEqual(out["outcome"], rem.FAILED)
        self.assertIn("not logged in", out["why"])
        self.assertEqual(env["SUTANDO_CLAUDE_RESUME"], self.a)
        self.assertEqual(env["SUTANDO_CLAUDE_WORKING_DIR"], str(self.a_dir))
        failed = wi.incarnations(self.ws, self.wid)
        self.assertEqual(len(failed), len(runs) + 1)
        self.assertEqual(failed[-1]["session_id"], self.a)
        self.assertEqual(failed[-1]["end_reason"], "crashed")
        self.assertTrue(all(r["ended_at"] is not None for r in failed))
        self.assertEqual(wi.current(self.ws, self.wid), {"session_id": None, "incarnation_id": None})
        self.assertEqual(self.snapshot(), before)
        out, env = self.recover(reopened=True)
        self.assert_selected(out, env, self.a, self.a_dir)
        self.assertEqual(out["closed_runs"], [])
        retried = wi.incarnations(self.ws, self.wid)
        self.assertEqual(retried[:-1], failed)
        self.assertEqual(retried[-1]["session_id"], self.a)
        self.assertIsNone(retried[-1]["ended_at"])
        self.assertEqual(self.snapshot(), before)

    def test_inconsistent_reference_refuses_without_mutation_or_launch(self):
        path = wi.incarnations_path(self.ws, self.wid)
        for missing in ("unrecorded-session", self.other_session):
            with self.subTest(reference=missing):
                runs = wi.incarnations(self.ws, self.wid)
                runs[-1]["session_id"] = missing
                path.write_text(json.dumps({"incarnations": runs}))
                before, launches = self.snapshot(include_runs=True), len(self.t.launches())
                out = rem.recover(self.ws, REPO, self.wid, runner=self.t)
                self.assertEqual(out["outcome"], rem.NO_SESSION)
                self.assertIn(missing, out["why"])
                self.assertEqual(len(self.t.launches()), launches)
                self.assertEqual(self.snapshot(include_runs=True), before)

    def test_legacy_missing_or_empty_reference_uses_latest_lineage(self):
        path = wi.incarnations_path(self.ws, self.wid)
        for reference in (None, "", "absent"):
            with self.subTest(reference=reference):
                self.t.live.clear()
                runs = wi.incarnations(self.ws, self.wid)
                if reference == "absent":
                    runs[-1].pop("session_id")
                else:
                    runs[-1]["session_id"] = reference
                path.write_text(json.dumps({"incarnations": runs}))
                out, env = self.recover()
                self.assert_selected(out, env, self.b, self.b_dir)

    def test_legacy_without_incarnations_uses_latest_lineage(self):
        wi.incarnations_path(self.ws, self.wid).write_text(json.dumps({"incarnations": []}))
        with mock.patch.dict(os.environ, {"SUTANDO_TMUX_SOCKET": SOCK}):
            out, env = self.recover()
        self.assert_selected(out, env, self.b, self.b_dir)

    def test_selected_session_with_empty_folder_uses_repository(self):
        rows = wi.sessions(self.ws, self.wid)
        rows[0]["transcript"]["cwd"] = ""
        wi.sessions_path(self.ws, self.wid).write_text(json.dumps({"sessions": rows}))
        out, env = self.recover()
        self.assert_selected(out, env, self.a, REPO)

    def test_pause_and_suspension_preserve_the_multi_session_worker(self):
        pause = wi.worker_dir(self.ws, self.wid) / sup.PAUSED_MARKER
        pause.touch()
        before, launches = self.snapshot(include_runs=True), len(self.t.launches())
        out = rem.recover(self.ws, REPO, self.wid, runner=self.t)
        self.assertEqual(out["outcome"], rem.PAUSED)
        self.assertEqual(self.snapshot(include_runs=True), before)
        pause.unlink()
        rem.suspend(self.ws, "test suspension")
        with rem.pool_lock(self.ws):
            pass
        before = self.snapshot(include_runs=True)
        out = rem.restart(self.ws, REPO, self.wid, runner=self.t)
        self.assertEqual(out["result"], rem.SUSPENDED)
        self.assertEqual(self.snapshot(include_runs=True), before)
        self.assertEqual(len(self.t.launches()), launches)


class CodexRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)
        self.la = self.ws / "LaunchAgents"
        real_ensure = sw.ensure_remedy_timer
        patcher = mock.patch.object(
            sw, "ensure_remedy_timer",
            side_effect=lambda ws, repo, **kw: real_ensure(ws, repo, launch_agents=self.la, **kw))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cwd = self.ws / "project"
        self.cwd.mkdir()
        self.t = FakeTmux(runtime="codex")
        first = sw.spawn(self.ws, REPO, runtime="codex", cwd=str(self.cwd),
                         socket=SOCK, label="Codex reviewer", runner=self.t,
                         require_sentinel=False)
        if sys.platform == "darwin":
            self.assertEqual(Path(first["remedy_timer"]["plist"]).parent, self.la)
        self.wid = first["worker_id"]
        self.inbox = first["delivery_dir"]
        sup.pr.register_worker(self.ws, self.wid, "Codex reviewer", runtime="codex")
        self.t.live.clear()

    def test_dead_codex_worker_gets_fresh_run_with_same_identity_and_inbox(self):
        self.assertEqual(wi.sessions(self.ws, self.wid), [])
        self.assertIsNone(wi.current(self.ws, self.wid)["session_id"])
        before = len(self.t.launches())
        out = rem.recover(self.ws, REPO, self.wid, runner=self.t)
        self.assertEqual(out["outcome"], rem.RECOVERED)
        self.assertIsNone(out["session_id"])
        self.assertEqual(len(self.t.launches()), before + 1)
        self.assertEqual([p.name for p in (self.ws / "state/workers").iterdir()
                          if p.is_dir()], [self.wid])
        self.assertTrue(Path(self.inbox).is_dir())
        self.assertEqual(wi.sessions(self.ws, self.wid), [])
        runs = wi.incarnations(self.ws, self.wid)
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["end_reason"], "crashed")
        self.assertIsNone(runs[1]["ended_at"])
        self.assertIsNone(runs[1]["session_id"])
        self.assertEqual(runs[1]["cwd"], str(self.cwd))
        _, env = self.t.launches()[-1]
        self.assertEqual(env["SUTANDO_WORKER_RUNTIME"], "codex")
        self.assertEqual(env["SUTANDO_TMUX_SOCKET"], SOCK)
        self.assertEqual(env["SUTANDO_TASKS_DIR"], self.inbox)
        self.assertEqual(env["SUTANDO_CODEX_WORKING_DIR"], str(self.cwd))
        self.assertNotIn("SUTANDO_CLAUDE_RESUME", env)

    def test_a_live_codex_worker_is_not_relaunched(self):
        self.t.live.add(wi.tmux_session_name(self.wid))
        before_runs = wi.incarnations(self.ws, self.wid)
        before_launches = len(self.t.launches())
        out = rem.recover(self.ws, REPO, self.wid, runner=self.t)
        self.assertEqual(out["outcome"], rem.ALREADY_RUNNING)
        self.assertEqual(wi.incarnations(self.ws, self.wid), before_runs)
        self.assertEqual(len(self.t.launches()), before_launches)

    def test_a_codex_worker_without_a_recorded_run_is_not_relaunched(self):
        # The last incarnation identifies the tmux socket and folder;
        # recovery cannot choose a target without it.
        wi.incarnations_path(self.ws, self.wid).write_text(
            json.dumps({"incarnations": []}))
        before = len(self.t.launches())
        out = rem.recover(self.ws, REPO, self.wid, runner=self.t)
        self.assertEqual(out["outcome"], rem.NO_SESSION)
        self.assertEqual(len(self.t.launches()), before)

    def test_a_failed_codex_relaunch_leaves_no_open_incarnation(self):
        self.t.launcher_fails = True
        out = rem.recover(self.ws, REPO, self.wid, runner=self.t)
        self.assertEqual(out["outcome"], rem.FAILED)
        self.assertEqual([r for r in wi.incarnations(self.ws, self.wid)
                          if r["ended_at"] is None], [])

    def test_watcher_rearm_carries_codex_runtime_and_clears_core_handler(self):
        seen = []

        def run(argv, **kw):
            seen.append((argv, kw["env"]))
            return subprocess.CompletedProcess(argv, 0, "", "")

        out = rem.ensure_supervisor(self.ws, REPO, self.wid, runner=run)
        self.assertEqual(out["outcome"], rem.SUPERVISED)
        env = seen[0][1]
        self.assertEqual(env["SUTANDO_WORKER_RUNTIME"], "codex")
        self.assertEqual(env["SUTANDO_TASK_EVENT_HANDLER"], "")
        self.assertEqual(env["SUTANDO_TMUX_SOCKET"], SOCK)


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
    def test_an_unreadable_roster_blocks_recovery_and_supervisor_rearm(self):
        before = wi.incarnations_path(self.ws, self.wid).read_bytes()
        launched = len(self.t.launches())
        with mock.patch.object(sw.pr, "load_roster", return_value=None):
            recovered = self.recover()
            rearmed = rem.ensure_supervisor(self.ws, REPO, self.wid, runner=self.t)
        self.assertEqual(recovered["outcome"], rem.INDETERMINATE)
        self.assertIn("absent from the readable roster", recovered["why"])
        self.assertEqual(rearmed["outcome"], rem.INDETERMINATE)
        self.assertIn("absent from the readable roster", rearmed["why"])
        self.assertEqual(wi.incarnations_path(self.ws, self.wid).read_bytes(), before)
        self.assertEqual(len(self.t.launches()), launched)
        self.assertFalse(any(c[0] == "bash" and c[1].endswith("worker-watcher-supervisor.sh")
                             for c in self.t.calls))

    def test_an_unknown_roster_runtime_blocks_recovery_and_supervisor_rearm(self):
        roster = {"workers": {self.wid: {"runtime": "unrecognised"}}}
        before = wi.incarnations_path(self.ws, self.wid).read_bytes()
        launched = len(self.t.launches())
        with mock.patch.object(sw.pr, "load_roster", return_value=roster):
            recovered = self.recover()
            rearmed = rem.ensure_supervisor(self.ws, REPO, self.wid, runner=self.t)
        self.assertEqual(recovered["outcome"], rem.INDETERMINATE)
        self.assertIn("unknown worker runtime", recovered["why"])
        self.assertEqual(rearmed["outcome"], rem.INDETERMINATE)
        self.assertIn("unknown worker runtime", rearmed["why"])
        self.assertEqual(wi.incarnations_path(self.ws, self.wid).read_bytes(), before)
        self.assertEqual(len(self.t.launches()), launched)
        self.assertFalse(any(c[0] == "bash" and c[1].endswith("worker-watcher-supervisor.sh")
                             for c in self.t.calls))

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

    def test_an_indeterminate_probe_writes_nothing_and_is_not_already_running(self):
        # tmux exits 3 with a message the shared classifier does not recognise:
        # `session_probe` answers "unknown". The worker MAY be alive.
        wi.start_incarnation(self.ws, self.wid, self.session, tmux_socket=SOCK,
                             tmux_session=wi.tmux_session_name(self.wid))   # a second open run
        runs_path = wi.incarnations_path(self.ws, self.wid)
        before = runs_path.read_bytes()
        real = self.t.__call__

        def cannot_answer(argv, **kw):
            if len(argv) > 3 and argv[3] == "has-session":
                return subprocess.CompletedProcess(argv, 3, "", "error connecting to server")
            return real(argv, **kw)
        self.t.__call__ = cannot_answer
        out = rem.recover(self.ws, REPO, self.wid, runner=cannot_answer)
        self.assertEqual(out["outcome"], rem.INDETERMINATE)
        self.assertNotEqual(out["outcome"], rem.ALREADY_RUNNING,
                            "'could not check' was recorded as 'checked and running'")
        self.assertEqual(len(self.t.launches()), self.launched_before)
        self.assertEqual(runs_path.read_bytes(), before,
                         "a probe that could not answer closed runs of a worker that may be "
                         "alive; with zero open runs the supervisor returns None for it forever")
        self.assertEqual(len([r for r in wi.incarnations(self.ws, self.wid)
                              if r["ended_at"] is None]), 2)


class APartialCloseIsBounded(Base):
    def test_an_io_failure_mid_close_is_a_recorded_outcome_not_an_exception(self):
        wi.start_incarnation(self.ws, self.wid, self.session, tmux_socket=SOCK,
                             tmux_session=wi.tmux_session_name(self.wid))   # two open runs
        real, calls = wi.end_incarnation, []

        def flaky(*a, **k):
            calls.append(a)
            if len(calls) == 2:
                raise OSError(28, "No space left on device")
            return real(*a, **k)
        wi.end_incarnation = flaky
        try:
            out = self.recover()          # the docstring promises: never raises
        finally:
            wi.end_incarnation = real
        self.assertEqual(out["outcome"], rem.FAILED)
        self.assertIn("No space left", out["why"])
        self.assertEqual(len(out["closed_runs"]), 1,
                         "the runs that WERE closed must be reported, or the partial close "
                         "looks like a completed one")
        self.assertEqual(len(self.t.launches()), self.launched_before,
                         "it launched over records it had not finished settling")


class ApplyingATick(Base):
    def test_human_dispatch_rechecks_pause_and_suspension(self):
        marker = wi.worker_dir(self.ws, self.wid) / sup.PAUSED_MARKER
        with mock.patch.object(rem.wc, "raise_card") as raised:
            marker.touch()
            rem.apply(self.ws, REPO, {self.wid: ps.CARD_HUMAN}, runner=self.t)
            marker.unlink()
            rem.suspend(self.ws, "test")
            rem.apply(self.ws, REPO, {self.wid: ps.CARD_HUMAN}, runner=self.t)
            raised.assert_not_called()

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


LOGIN_PANE = ("❯ /startup\n  ⎿  Login expired · Please run /login\n\n❯ \n"
              "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n")
API_PANE = ("  ⎿  API Error: 500 internal server error\n❯ \n"
            "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n")


class TheSweepOnALoggedOutWorker(Base):
    """The user's report (2026-09-29): `--sweep --dry-run` said "nothing" for four
    workers whose panes read "Login expired · Please run /login". The session is
    alive, so the death ladder is silent; the finding has to come from the pane."""

    def setUp(self):
        super().setUp()
        sup.pr.register_worker(self.ws, self.wid, "alpha")
        for name, value in (("probe_session", True), ("session_watcher_holds", False),
                            ("observe_pane", (ps.PANE_LOGGED_OUT, "f1"))):
            p = mock.patch.object(sup, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(rem.wc, "capture", return_value=LOGIN_PANE)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = rem.main(["--workspace", str(self.ws), "--repo", str(REPO), *argv])
        return rc, json.loads(out.getvalue())

    def test_a_dry_run_reports_the_login_card_it_would_raise(self):
        rc, out = self._run("--sweep", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertEqual(out["decisions"][self.wid], ps.CARD_LOGIN)
        self.assertEqual(out["auth_expired"], [self.wid])
        self.assertEqual(out["cards"], {}, "a dry run raises nothing")
        self.assertFalse(sup.state_path(self.ws).exists())

    def test_the_real_run_cards_the_owner_and_restarts_nothing(self):
        rc, out = self._run("--sweep")
        self.assertEqual(rc, 0)
        self.assertEqual(out["recoveries"], {}, "a logged-out session is never respawned")
        self.assertEqual(out["escalations"], [])
        self.assertEqual(out["cards"][self.wid]["outcome"], "carded")
        self.assertEqual(len(self.t.launches()), self.launched_before)
        req = rem.wc.manager_for(self.ws).get(out["cards"][self.wid]["hitl_id"])
        self.assertIn("Login expired · Please run /login", req.message)
        self.assertIn("Run /login", req.message)
        self.assertEqual(req.subject["wedge"], ps.CARD_LOGIN)
        self.assertNotIn(req.id, out["cards_closed"])
        # Raised once: the next sweep still reports the finding, without a second card.
        rc, again = self._run("--sweep")
        self.assertEqual((rc, again["decisions"][self.wid], again["cards"], again["auth_expired"]),
                         (0, ps.NOTHING, {}, [self.wid]))
        self.assertEqual(len(rem.wc.manager_for(self.ws).active()), 1)

    def test_a_pane_that_moves_on_to_another_wedge_retires_the_login_card(self):
        """One decision per (episode, kind): the acknowledged login card neither
        silences the API error that follows it nor stays up once the pane stops
        showing the expired login."""
        clock = [5000.0]
        with mock.patch.object(rem, "time", types.SimpleNamespace(time=lambda: clock[0])):
            _, first = self._run("--sweep")
            login_id = first["cards"][self.wid]["hitl_id"]
            with mock.patch.object(sup, "observe_pane", return_value=(ps.PANE_ABNORMAL, "f2")), \
                    mock.patch.object(sup, "work_outstanding", return_value=True), \
                    mock.patch.object(rem.wc, "capture", return_value=API_PANE), \
                    mock.patch.object(rem.wc.qa, "seat_env_base_url",
                                      return_value=rem.wc.qa.SeatEnv(False, None)):
                clock[0] += 300.0
                _, second = self._run("--sweep")
                clock[0] += 300.0
                _, third = self._run("--sweep")
        self.assertEqual((second["decisions"][self.wid], second["cards_closed"]),
                         (ps.NOTHING, [login_id]), "the stale login card is retired at once")
        self.assertEqual((third["decisions"][self.wid], third["cards"][self.wid]["outcome"]),
                         (ps.CARD_CAUSE, "carded"))
        active = rem.wc.manager_for(self.ws).active()
        self.assertEqual([r.subject["wedge"] for r in active], [ps.CARD_CAUSE])
        self.assertIn("API Error: 500", active[0].message)



class PressedProxyRecovery(Base):
    def sweep(self, pane, frame, pending, *, previous="old", alive=True,
              sample_at=1000.0, dry_run=False, held=None, paused=False):
        sup.save_state(self.ws, ps.SupervisionState(
            last_sample_at=sample_at, workers={self.wid: ps.WorkerEvidence(
                wedge_consecutive=3, wedge_first_detected_at=100.0,
                wedge_escalated=True, wedge_kind="abnormal", last_pane_id=previous)}))
        args = types.SimpleNamespace(workspace=self.ws, repo=REPO, resume=False,
                                     recipient=None, dry_run=dry_run)
        with contextlib.ExitStack() as stack:
            for obj, name, value in (
                (sup, "probe_session", alive), (sup, "session_watcher_holds", True),
                (sup, "observe_pane", (pane, frame)), (sup, "work_outstanding", pending),
                (sup, "is_paused", paused), (rem, "suspension", held),
                (rem, "ensure_supervisors", {}), (rem, "ensure_input_watches", {}),
                (rem.wc, "raise_card", {"outcome": "refused"})):
                stack.enter_context(mock.patch.object(obj, name, return_value=value))
            stack.enter_context(mock.patch.object(rem, "time", types.SimpleNamespace(time=lambda: 1300.0)))
            stack.enter_context(mock.patch.object(rem.subprocess, "run",
                                                  side_effect=AssertionError("no external action")))
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(rem._sweep(args), 0)
        out = json.loads(output.getvalue())
        self.assertEqual(out["recoveries"], {})
        return out

    def pressed_card(self):
        from hitl.schema import ActionReply
        def capture(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, API_PANE, "")
        out = rem.wc.raise_card(self.ws, self.wid, ps.CARD_CAUSE,
                                 runner=capture, routed=lambda *a: True)
        manager = rem.wc.manager_for(self.ws)
        req = manager.get(out["hitl_id"])
        manager.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision,
                                         action_id=rem.wc.PROXY_ACTION, guard=req.guard))
        return req.id

    def test_uncertain_or_wedged_observations_retain_pressed_proxy(self):
        cases = [(None, None, True, "old", True),
                 (ps.PANE_UNKNOWN, "new", True, "old", True),
                 (ps.PANE_IDLE, "new", None, "old", True),
                 (ps.PANE_ABNORMAL, "new", True, "old", True),
                 (ps.PANE_LOGGED_OUT, "new", True, "old", True),
                 (ps.PANE_GATE, "new", True, "old", True),
                 (ps.PANE_LIMIT, "new", True, "old", True),
                 (ps.PANE_WORKING, "old", True, "old", True),
                 (ps.PANE_WORKING, "new", True, None, True),
                 (ps.PANE_WORKING, None, False, "old", True),
                 (ps.PANE_IDLE, None, False, "old", True),
                 (ps.PANE_IDLE, "new", False, "old", None)]
        for pane, frame, pending, previous, alive in cases:
            with self.subTest(pane=pane, frame=frame, pending=pending, previous=previous, alive=alive):
                rid = self.pressed_card()
                out = self.sweep(pane, frame, pending, previous=previous, alive=alive)
                self.assertNotIn(rid, out["cards_closed"])
                manager = rem.wc.manager_for(self.ws)
                self.assertEqual(manager.get(rid).status, "in_progress")
                manager.expire(rid)

    def test_healthy_recovery_persists_and_closes_only_once(self):
        for pane, pending in ((ps.PANE_IDLE, False), (ps.PANE_IDLE, True),
                              (ps.PANE_WORKING, True), (ps.PANE_WORKING, False)):
            with self.subTest(pane=pane, pending=pending):
                rid = self.pressed_card()
                self.assertEqual(self.sweep(pane, "new", pending)["cards_closed"], [rid])
                self.assertEqual(rem.wc.manager_for(self.ws).get(rid).status, "resolved")
                self.assertEqual(self.sweep(pane, "newer", pending)["cards_closed"], [])

    def test_ineligible_sweeps_retain_pressed_proxy(self):
        for options in ({"sample_at": 1.0}, {"dry_run": True},
                        {"held": "held"}, {"paused": True}):
            with self.subTest(options=options):
                rid = self.pressed_card()
                self.assertNotIn(rid, self.sweep(ps.PANE_IDLE, "new", False,
                                               **options).get("cards_closed", []))
                manager = rem.wc.manager_for(self.ws)
                self.assertEqual(manager.get(rid).status, "in_progress")
                manager.expire(rid)

    def test_failed_and_empty_capture_retain_pressed_proxy(self):
        real_observe = sup.observe_pane
        for result in (subprocess.CompletedProcess([], 1, "", "failed"),
                       subprocess.CompletedProcess([], 0, "", "")):
            with self.subTest(returncode=result.returncode):
                with mock.patch.object(sup, "_open_tmux", return_value=(SOCK, "seat")):
                    pane, frame = real_observe(self.ws, self.wid,
                                               runner=lambda *a, **k: result)
                self.assertEqual((pane, frame), (None, None))
                rid = self.pressed_card()
                self.assertEqual(self.sweep(pane, frame, True)["cards_closed"], [])
                manager = rem.wc.manager_for(self.ws)
                self.assertEqual(manager.get(rid).status, "in_progress")
                manager.expire(rid)


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
