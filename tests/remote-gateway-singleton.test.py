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

    def test_heartbeat_thread_records_a_lost_lock_and_stops(self):
        # The lock is refreshed off the poll loop; a loss it sees must reach the
        # loop as _LOCK_LOST, and a fresh acquire must clear it.
        self.assertTrue(rgb._acquire_singleton())
        rgb._OWNERSHIP_RELINQUISHED.clear()
        orig_hb, orig_stale = rgb._ws_heartbeat, rgb._LOCK_STALE_S
        calls = []

        def lost(*a, **k):
            calls.append(a)
            return False
        rgb._ws_heartbeat, rgb._LOCK_STALE_S = lost, 1
        try:
            rgb._start_lock_heartbeat()
            deadline = time.time() + 5
            while not rgb._LOCK_LOST.is_set() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(rgb._LOCK_LOST.is_set(), "a lost heartbeat must set the flag")
            time.sleep(1.5)
            self.assertEqual(len(calls), 1, "the thread must stop after the loss")
        finally:
            rgb._ws_heartbeat, rgb._LOCK_STALE_S = orig_hb, orig_stale
        self._lockfile().unlink(missing_ok=True)
        self.assertTrue(rgb._acquire_singleton())
        self.assertFalse(rgb._LOCK_LOST.is_set(), "a fresh acquire starts un-lost")
        rgb._release_singleton()

    def test_heartbeat_thread_keeps_the_lock_fresh_until_relinquished(self):
        self.assertTrue(rgb._acquire_singleton())
        rgb._OWNERSHIP_RELINQUISHED.clear()
        orig_hb, orig_stale = rgb._ws_heartbeat, rgb._LOCK_STALE_S
        calls = []

        def held(*a, **k):
            calls.append(a)
            return True
        rgb._ws_heartbeat, rgb._LOCK_STALE_S = held, 1
        try:
            rgb._start_lock_heartbeat()
            deadline = time.time() + 5
            while len(calls) < 2 and time.time() < deadline:
                time.sleep(0.05)
            self.assertGreaterEqual(len(calls), 2, "the thread must keep refreshing")
            rgb._OWNERSHIP_RELINQUISHED.set()
            time.sleep(0.7)
            n = len(calls)
            time.sleep(1.2)
            self.assertEqual(len(calls), n, "relinquishing ownership must stop the thread")
            self.assertFalse(rgb._LOCK_LOST.is_set())
        finally:
            rgb._ws_heartbeat, rgb._LOCK_STALE_S = orig_hb, orig_stale
        rgb._release_singleton()

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


if __name__ == "__main__":
    unittest.main()
