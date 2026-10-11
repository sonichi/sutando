#!/usr/bin/env python3
"""MC1 slice-2: the gateway bridge's per-workspace singleton glue.

Verifies the thin wiring around workspace_lock (the primitive itself is tested in
tests/workspace-lock.test.py, incl. the O_EXCL concurrency + heartbeat-P1 cases):
acquire/release round-trip, the SUTANDO_BRIDGE_LOCK kill-switch, the deferred
path (a live holder → the bridge must NOT poll), and fail-open (a lock-layer
error must never stop the bridge from polling — task delivery must not wedge).
"""
from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Point the bridge's dirs at a temp tree + pin the host label BEFORE importing,
# so _STATE (and thus _LOCK_WS = _STATE.parent) resolve into the sandbox.
_TMP = tempfile.mkdtemp()
os.environ["AGENT_CONNECT_STATE_DIR"] = str(Path(_TMP) / "state")
os.environ["AGENT_CONNECT_TASK_DIR"] = str(Path(_TMP) / "tasks")
os.environ["AGENT_CONNECT_RESULT_DIR"] = str(Path(_TMP) / "results")
os.environ["SUTANDO_HOST_LABEL"] = "testhost"
os.environ.setdefault("REMOTE_TASK_TOKEN", "https://example|secret")

sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))
import ag2_sparrow.remote_gateway_bridge as rgb  # noqa: E402


class SingletonGlueTest(unittest.TestCase):
    def _lockfile(self) -> Path:
        return rgb._LOCK_WS / "state" / "locks" / "gateway-bridge.lock"

    def setUp(self):
        os.environ.pop("SUTANDO_BRIDGE_LOCK", None)
        self._lockfile().unlink(missing_ok=True)   # force-clear (foreign or ours)

    def tearDown(self):
        os.environ.pop("SUTANDO_BRIDGE_LOCK", None)
        self._lockfile().unlink(missing_ok=True)

    def test_acquire_release_roundtrip(self):
        self.assertTrue(rgb._acquire_singleton())
        self.assertTrue(self._lockfile().exists())
        rgb._release_singleton()
        self.assertFalse(self._lockfile().exists())

    def test_kill_switch_disables(self):
        os.environ["SUTANDO_BRIDGE_LOCK"] = "0"
        self.assertTrue(rgb._acquire_singleton())        # proceeds
        self.assertFalse(self._lockfile().exists())      # but never touches the lock
        self.assertTrue(rgb._heartbeat_singleton())      # lock disabled → fail-open True
        rgb._release_singleton()

    def test_heartbeat_lost_ownership_stops_poll(self):
        # Regression (Codex review on #2153): after a stale takeover a replacement
        # reaps our lock, so workspace_lock.heartbeat() returns False. The bridge
        # MUST treat that as lost ownership and stop polling — else the reaped
        # process and the new owner dual-poll the relay bearer. Force the False
        # and prove _heartbeat_singleton propagates it (the main loop's
        # `if not _heartbeat_singleton(): return` then exits before the next poll).
        self.assertTrue(rgb._acquire_singleton())        # we hold it
        orig = rgb._ws_heartbeat
        rgb._ws_heartbeat = lambda *a, **k: False        # reaped by a replacement
        try:
            self.assertFalse(rgb._heartbeat_singleton(), "lost lock must signal stop-poll")
        finally:
            rgb._ws_heartbeat = orig
        self.assertTrue(rgb._heartbeat_singleton())      # still-held → keep polling
        rgb._release_singleton()

    def test_heartbeat_fail_open_on_error(self):
        # A heartbeat backend error must NOT be read as lost ownership (fail-open):
        # a lock bug can't be allowed to wedge task delivery.
        self.assertTrue(rgb._acquire_singleton())
        orig = rgb._ws_heartbeat

        def boom(*a, **k):
            raise RuntimeError("heartbeat backend exploded")
        rgb._ws_heartbeat = boom
        try:
            self.assertTrue(rgb._heartbeat_singleton())  # error → keep polling
        finally:
            rgb._ws_heartbeat = orig
        rgb._release_singleton()

    def _with_heartbeat(self, fake):
        orig_hb, orig_stale = rgb._ws_heartbeat, rgb._LOCK_STALE_S
        rgb._ws_heartbeat, rgb._LOCK_STALE_S = fake, 1
        rgb._OWNERSHIP_RELINQUISHED.clear()
        self.addCleanup(setattr, rgb, "_ws_heartbeat", orig_hb)
        self.addCleanup(setattr, rgb, "_LOCK_STALE_S", orig_stale)

    def test_heartbeat_loop_records_a_lost_lock_and_stops(self):
        # Run in the calling thread: coverage does not trace other threads here.
        self.assertTrue(rgb._acquire_singleton())
        calls = []
        self._with_heartbeat(lambda *a, **k: calls.append(a) or False)
        rgb._lock_heartbeat_loop()
        self.assertEqual(len(calls), 1, "the loop must stop at the first loss")
        self.assertTrue(rgb._LOCK_LOST.is_set(), "a lost heartbeat must set the flag")
        self._lockfile().unlink(missing_ok=True)
        self.assertTrue(rgb._acquire_singleton())
        self.assertFalse(rgb._LOCK_LOST.is_set(), "a fresh acquire starts un-lost")
        rgb._release_singleton()

    def test_heartbeat_loop_refreshes_until_relinquished(self):
        calls = []

        def held(*a, **k):
            calls.append(a)
            if len(calls) == 2:
                rgb._OWNERSHIP_RELINQUISHED.set()
            return True
        self._with_heartbeat(held)
        rgb._lock_heartbeat_loop()
        self.assertEqual(len(calls), 2, "relinquishing ownership must stop the loop")
        self.assertFalse(rgb._LOCK_LOST.is_set())

    def test_heartbeat_loop_stops_refreshing_while_the_main_loop_stalls(self):
        import threading
        calls, logged = [], []
        saved = (rgb._LOCK_PASS_MAX_S, rgb._LOOP_TICK["at"], rgb._log)
        self.addCleanup(setattr, rgb, "_LOCK_PASS_MAX_S", saved[0])
        self.addCleanup(rgb._LOOP_TICK.__setitem__, "at", saved[1])
        self.addCleanup(setattr, rgb, "_log", saved[2])
        rgb._log = logged.append

        def held(*a, **k):
            calls.append(a)
            rgb._OWNERSHIP_RELINQUISHED.set()
            return True
        self._with_heartbeat(held)
        rgb._LOCK_PASS_MAX_S = 10
        rgb._LOOP_TICK["at"] = time.monotonic() - 100   # the loop has been stuck
        # Progress resumes after a few idle intervals; the refresh resumes with it.
        threading.Timer(1.3, rgb._stamp_loop_progress).start()
        t0 = time.monotonic()
        rgb._lock_heartbeat_loop()
        self.assertGreaterEqual(time.monotonic() - t0, 1.3, "the loop must wait for progress")
        self.assertEqual(len(calls), 1, "no refresh while stalled; one once progress resumes")
        self.assertFalse(rgb._LOCK_LOST.is_set())
        self.assertEqual(sum("made no progress" in m for m in logged), 1, logged)

    def test_pass_bound_covers_a_full_pass_with_margin(self):
        # Data pin: the bound is twice one pass, and it exceeds the stale window.
        one_pass = rgb.POLL_WAIT + 10 + rgb._POLL_BACKOFF_MAX_S + 2 * rgb._REQ_TIMEOUT_S
        self.assertEqual(rgb._LOCK_PASS_MAX_S, 2 * one_pass)
        self.assertGreater(rgb._LOCK_PASS_MAX_S, rgb._LOCK_STALE_S)

    def test_start_lock_heartbeat_runs_the_loop_on_a_daemon_thread(self):
        import threading
        calls = []
        self._with_heartbeat(lambda *a, **k: calls.append(a) or True)
        rgb._start_lock_heartbeat()
        th = next(t for t in threading.enumerate() if t.name == "sparrow-lock-heartbeat")
        self.assertTrue(th.daemon)
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(calls, "the thread must refresh the lock")
        rgb._OWNERSHIP_RELINQUISHED.set()
        th.join(5)
        self.assertFalse(th.is_alive())

    def test_main_loop_stops_on_a_lost_flag_before_polling(self):
        import threading
        reached = []

        def must_not_run(name):
            def _stub(*a, **k):
                reached.append(name)
                raise KeyboardInterrupt(name)   # escapes the loop's catch-all
            return _stub

        def finished_thread(_inflight):
            t = threading.Thread(target=lambda: None)
            t.start()
            return t
        stubs = {"_acquire_singleton": lambda: True, "_start_lock_heartbeat": lambda: None,
                 "_load_inflight": set, "_recover_orphan_proactive": lambda: None,
                 "_emit_gateway_status": lambda *a, **k: None,
                 "refresh_routing": lambda **k: None,
                 "_maybe_start_event_channel": lambda: None,
                 "_start_results_watcher": lambda: None,
                 "_start_outbound_worker": finished_thread,
                 "_heartbeat_singleton": must_not_run("_heartbeat_singleton"),
                 "_req": must_not_run("_req")}
        saved = {k: getattr(rgb, k) for k in stubs}
        logged = []
        saved["_log"] = rgb._log
        for k, v in stubs.items():
            setattr(rgb, k, v)
        rgb._log = logged.append
        rgb._LOCK_LOST.set()
        try:
            th = threading.Thread(target=rgb.main, daemon=True)
            th.start()
            th.join(10)
            self.assertFalse(th.is_alive(), "main() must return once the lock is lost")
        finally:
            for k, v in saved.items():
                setattr(rgb, k, v)
            rgb._LOCK_LOST.clear()
            rgb._OUTBOUND_STOP.clear()
            rgb._OUTBOUND_WAKE.clear()
        self.assertEqual(reached, [], "the flag must stop the loop before any poll")
        self.assertTrue(any("lost workspace poller lock" in m for m in logged), logged)

    def test_main_stands_down_with_exit_75_when_deferred(self):
        # A supervising wrapper (gateway-bridge-wrapper.sh) relaunches any child
        # that exits, except rc 75: so a deferral must exit 75, not return 0.
        orig = rgb._acquire_singleton
        rgb._acquire_singleton = lambda: False
        try:
            with self.assertRaises(SystemExit) as ctx:
                rgb.main()
        finally:
            rgb._acquire_singleton = orig
        self.assertEqual(ctx.exception.code, 75)

    def test_deferred_when_live_holder(self):
        lf = self._lockfile()
        lf.parent.mkdir(parents=True, exist_ok=True)
        lf.write_text(json.dumps({"role": "gateway-bridge", "pid": 999999,
                                  "host": "testhost", "heartbeat_at": int(time.time()),
                                  "schema_version": 1}))
        self.assertFalse(rgb._acquire_singleton())       # live holder → must defer (no poll)
        self.assertEqual(json.loads(lf.read_text())["pid"], 999999)  # holder untouched

    def test_fail_open_on_acquire_error(self):
        orig = rgb._ws_acquire

        def boom(*a, **k):
            raise RuntimeError("lock backend exploded")
        rgb._ws_acquire = boom
        try:
            self.assertTrue(rgb._acquire_singleton())    # error → proceed to poll (fail-open)
        finally:
            rgb._ws_acquire = orig

    def _with_signal_sinks(self, stdout):
        """Route the handler's two sinks: stdout to `stdout`, the file line to a
        list. Returns the list."""
        lines = []
        saved = (sys.stdout, rgb._append_log_file)
        sys.stdout, rgb._append_log_file = stdout, lines.append
        self.addCleanup(lambda: setattr(sys, "stdout", saved[0]))
        self.addCleanup(setattr, rgb, "_append_log_file", saved[1])
        return lines

    def test_signal_exit_names_the_signal_and_exits_0(self):
        import io
        out = io.StringIO()
        lines = self._with_signal_sinks(out)
        with self.assertRaises(SystemExit) as ctx:
            rgb._exit_on_signal(signal.SIGTERM, None)
        self.assertEqual(ctx.exception.code, 0)
        want = "[remote-gateway-bridge] received SIGTERM — exiting"
        self.assertEqual(lines, [want])
        self.assertEqual(out.getvalue(), want + "\n")

    def test_signal_exit_logs_the_file_line_when_print_is_reentrant(self):
        # The signal landed inside print: the next print raises, as the
        # BufferedWriter does on re-entry. The file line must still be written.
        class Reentrant:
            def write(self, _data):
                raise RuntimeError("reentrant call inside <_io.BufferedWriter>")

            def flush(self):
                raise RuntimeError("reentrant call inside <_io.BufferedWriter>")
        lines = self._with_signal_sinks(Reentrant())
        with self.assertRaises(SystemExit) as ctx:
            rgb._exit_on_signal(signal.SIGINT, None)
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(lines, ["[remote-gateway-bridge] received SIGINT — exiting"])

    def test_signal_exit_survives_a_closed_stdout_fd(self):
        # Both stdout sinks refuse (stream re-entry, then the raw fd): the file
        # line is still written and the exit is still 0.
        class Reentrant:
            def write(self, _data):
                raise RuntimeError("reentrant call inside <_io.BufferedWriter>")

            def flush(self):
                raise RuntimeError("reentrant call inside <_io.BufferedWriter>")

        class NoFd:
            def __getattr__(self, name):
                return getattr(os, name)

            @staticmethod
            def write(_fd, _data):
                raise OSError(9, "Bad file descriptor")
        lines = self._with_signal_sinks(Reentrant())
        rgb.os = NoFd()
        self.addCleanup(setattr, rgb, "os", os)
        with self.assertRaises(SystemExit) as ctx:
            rgb._exit_on_signal(signal.SIGTERM, None)
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(lines, ["[remote-gateway-bridge] received SIGTERM — exiting"])

    def test_signal_exit_survives_a_failing_log(self):
        def boom(_line):
            raise OSError("disk gone")
        import io
        out = io.StringIO()
        self._with_signal_sinks(out)
        rgb._append_log_file = boom
        with self.assertRaises(SystemExit) as ctx:
            rgb._exit_on_signal(signal.SIGINT, None)
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("received SIGINT — exiting", out.getvalue())


if __name__ == "__main__":
    unittest.main()
