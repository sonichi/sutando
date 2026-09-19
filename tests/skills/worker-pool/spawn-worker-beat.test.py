#!/usr/bin/env python3
"""A successful spawn starts the worker's own beat, watching the tmux pane's
pid (the agent process itself, with the core's launch command) — not the
launcher's own pid, and not by parentage: see docs in pool_beat.py and the
live proof in tests/worker-writes-its-beat.test.sh for why parentage cannot
work for anything this skill spawns from inside a running session.

tmux is injected here too, same as spawn-worker-launcher.test.py: these never
touch a real socket or a real pool_beat.py process. What's under test is the
WIRING — spawn() reads the pane pid and hands it to start_worker_beat with the
right worker id — not pool_beat.py's own mechanics (covered by
tests/pool-beat-watch-pid.test.py) or Popen's (nothing meaningful to fake).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import spawn_worker as sw  # noqa: E402


class FakeTmuxWithPane:
    """Like spawn-worker-launcher's FakeTmux, plus a `list-panes` answer."""

    def __init__(self, pane_pid="54321"):
        self.calls = []
        self.pane_pid = pane_pid

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] == "bash" and argv[1].endswith("sutando-config.sh"):
            return subprocess.CompletedProcess(argv, 0, "claude\n", "")
        if argv[0] == "bash" and argv[1].endswith("start-cli.sh"):
            return subprocess.CompletedProcess(argv, 0, "Started detached.", "")
        if argv[0] == "tmux" and "list-panes" in argv:
            out = f"{self.pane_pid}\n" if self.pane_pid is not None else ""
            return subprocess.CompletedProcess(argv, 0, out, "")
        if argv[0] == "tmux" and "has-session" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "can't find session: x")
        return subprocess.CompletedProcess(argv, 0, "", "")


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)


# --- pane_pid() --------------------------------------------------------------

class TestPanePid(unittest.TestCase):
    def test_parses_the_pid_tmux_reports(self):
        got = sw.pane_pid("sess", "/tmp/x.sock", runner=FakeTmuxWithPane("777"))
        self.assertEqual(got, 777)

    def test_none_when_tmux_reports_nothing_usable(self):
        got = sw.pane_pid("sess", "/tmp/x.sock", runner=FakeTmuxWithPane(""))
        self.assertIsNone(got)

    def test_none_on_non_numeric_output_rather_than_raising(self):
        got = sw.pane_pid("sess", "/tmp/x.sock", runner=FakeTmuxWithPane("not-a-pid"))
        self.assertIsNone(got)


# --- start_worker_beat() ------------------------------------------------------

class TestStartWorkerBeat(unittest.TestCase):
    def test_launches_pool_beat_with_watch_pid_not_parent_pid(self):
        calls = []

        def fake_popen(argv, **kw):
            calls.append((argv, kw))
            return object()

        ok = sw.start_worker_beat("/ws", "w123", 999, repo=REPO, popen=fake_popen)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        argv, kw = calls[0]
        self.assertIn("--watch-pid", argv)
        self.assertEqual(argv[argv.index("--watch-pid") + 1], "999")
        self.assertNotIn("--parent-pid", argv)
        self.assertIn("--kind", argv)
        self.assertEqual(argv[argv.index("--kind") + 1], "worker")
        self.assertEqual(argv[argv.index("--id") + 1], "w123")
        self.assertEqual(argv[argv.index("--workspace") + 1], "/ws")
        # Detached: must not block spawn_worker's own exit waiting on it.
        self.assertTrue(kw.get("start_new_session"))

    def test_false_when_the_beat_script_is_missing_never_raises(self):
        ok = sw.start_worker_beat("/ws", "w1", 1, repo="/nonexistent-repo-xyz",
                                  popen=lambda *a, **k: (_ for _ in ()).throw(
                                      AssertionError("must not even try to spawn")))
        self.assertFalse(ok)

    def test_false_not_raise_when_popen_itself_fails(self):
        def boom(*a, **k):
            raise OSError("no such thing")
        ok = sw.start_worker_beat("/ws", "w1", 1, repo=REPO, popen=boom)
        self.assertFalse(ok)


# --- wired into spawn() -------------------------------------------------------

class TestSpawnStartsTheBeat(Base):
    def test_a_successful_spawn_starts_the_beat_on_the_panes_pid(self):
        t = FakeTmuxWithPane("54321")
        started = {}

        def fake_start(workspace, worker_id, watch_pid, **kw):
            started.update(workspace=workspace, worker_id=worker_id, watch_pid=watch_pid)
            return True
        real = sw.start_worker_beat
        sw.start_worker_beat = fake_start
        try:
            got = sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        finally:
            sw.start_worker_beat = real
        self.assertEqual(started["worker_id"], got["worker_id"])
        self.assertEqual(started["watch_pid"], 54321)
        self.assertEqual(str(started["workspace"]), str(self.ws))
        self.assertTrue(got["beat_started"])

    def test_no_pane_pid_no_beat_attempt_and_spawn_still_succeeds(self):
        """A tmux that cannot answer list-panes must not fail an otherwise
        successful spawn: the roster's own state/beat split already
        tolerates `state: live, beat: absent`."""
        t = FakeTmuxWithPane(pane_pid=None)
        calls = {"n": 0}

        def fake_start(*a, **kw):
            calls["n"] += 1
            return True
        real = sw.start_worker_beat
        sw.start_worker_beat = fake_start
        try:
            got = sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        finally:
            sw.start_worker_beat = real
        self.assertEqual(calls["n"], 0)
        self.assertTrue(got["started"])
        self.assertFalse(got["beat_started"])

    def test_a_resume_also_starts_a_fresh_beat(self):
        """The old beat, if any, already exited on its own once its watched
        (now-dead) pid disappeared — a resume just needs a new one for the
        new pid, through the SAME code path a fresh spawn uses."""
        real = sw.start_worker_beat
        sw.start_worker_beat = lambda *a, **kw: True   # no real subprocess in setup
        try:
            t = FakeTmuxWithPane("11111")
            first = sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        finally:
            sw.start_worker_beat = real
        session_id = first["runtime_session_id"]

        t2 = FakeTmuxWithPane("22222")
        started = {}

        def fake_start(workspace, worker_id, watch_pid, **kw):
            started.update(worker_id=worker_id, watch_pid=watch_pid)
            return True
        sw.start_worker_beat = fake_start
        try:
            second = sw.spawn(self.ws, REPO, runner=t2, require_sentinel=False,
                             resume=session_id)
        finally:
            sw.start_worker_beat = real
        self.assertEqual(second["worker_id"], first["worker_id"])
        self.assertEqual(started["worker_id"], first["worker_id"])
        self.assertEqual(started["watch_pid"], 22222)


if __name__ == "__main__":
    unittest.main()
