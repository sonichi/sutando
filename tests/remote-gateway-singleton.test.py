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
import subprocess
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
        live = os.getppid()                              # a running process that is not us
        lf.write_text(json.dumps({"role": "gateway-bridge", "pid": live,
                                  "host": rgb._stable_host_label(),
                                  "heartbeat_at": int(time.time()),
                                  "schema_version": 1}))
        self.assertFalse(rgb._acquire_singleton())       # live holder → must defer (no poll)
        self.assertEqual(json.loads(lf.read_text())["pid"], live)  # holder untouched

    @unittest.skipIf(os.name == "nt", "no pid probe on Windows: heartbeat rule only")
    def test_dead_holder_on_this_host_is_taken_over_at_once(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"]); child.wait()
        lf = self._lockfile()
        lf.parent.mkdir(parents=True, exist_ok=True)
        lf.write_text(json.dumps({"role": "gateway-bridge", "pid": child.pid,
                                  "host": rgb._stable_host_label(),   # the label the lock compares
                                  "heartbeat_at": int(time.time()),
                                  "schema_version": 1}))
        self.assertTrue(rgb._acquire_singleton())        # no stale-window wait for a corpse
        self.assertEqual(json.loads(lf.read_text())["pid"], os.getpid())
        rgb._release_singleton()

    def test_a_held_exit_retains_the_lock_past_its_own_pid(self):
        self.assertTrue(rgb._acquire_singleton())
        orig = rgb._join_push_thread
        rgb._join_push_thread = lambda *a, **k: False    # a publication still in flight
        try:
            rgb._release_singleton()
        finally:
            rgb._join_push_thread = orig
        held = json.loads(self._lockfile().read_text())
        self.assertEqual(held["pid"], os.getpid())       # not released
        self.assertTrue(held.get("retained"))            # so a dead-pid probe will not reap it

    def test_a_held_exit_keeps_the_lock_when_retain_fails(self):
        self.assertTrue(rgb._acquire_singleton())
        orig_join, orig_retain = rgb._join_push_thread, rgb._ws_retain
        rgb._join_push_thread = lambda *a, **k: False

        def boom(*a, **k):
            raise OSError("locks dir unwritable")
        rgb._ws_retain = boom
        try:
            rgb._release_singleton()                     # an exit hook must not raise
        finally:
            rgb._join_push_thread, rgb._ws_retain = orig_join, orig_retain
        held = json.loads(self._lockfile().read_text())
        self.assertEqual(held["pid"], os.getpid())       # still held, never released
        self.assertNotIn("retained", held)

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
