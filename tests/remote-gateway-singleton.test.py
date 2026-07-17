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
