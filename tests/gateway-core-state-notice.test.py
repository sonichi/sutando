#!/usr/bin/env python3
"""_maybe_core_state_notices — the bridge binder for core-state notices.

The sweep itself (dedup, cooldown, recovery — tests/test_core_state_notice.py
in the package) is policy; this covers only what the BINDER owns: which rooms
it hands the sweep (in-flight tasks without a ready result, mapped through the
task→room sidecar, valid Matrix ids only), and its exception contract (a
broken sweep never breaks the poll loop; the send's 401/403 must escape to the
loop's auth-recovery path).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PKG = _REPO / "packages" / "ag2-sparrow"
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))

_TMP = tempfile.TemporaryDirectory()
for _kind in ("task", "result", "state"):
    _d = Path(_TMP.name) / _kind
    _d.mkdir()
    os.environ[f"AGENT_CONNECT_{_kind.upper()}_DIR"] = str(_d)
os.environ.setdefault("REMOTE_TASK_URL", "https://gw.example/relay")
os.environ.setdefault("REMOTE_TASK_TOKEN", "dummy-secret")

import ag2_sparrow.remote_gateway_bridge as gw  # noqa: E402


class RoomSelection(unittest.TestCase):
    def setUp(self):
        self.captured = []
        self._saved = gw.sweep_core_state_notices
        gw.sweep_core_state_notices = (
            lambda state_dir, rooms, send, **kw: self.captured.append(set(rooms)))
        self.addCleanup(setattr, gw, "sweep_core_state_notices", self._saved)
        self.addCleanup(lambda: gw.TASK_ROOMS_FILE.unlink(missing_ok=True))

    def _rooms_for(self, rooms_map, inflight, results=()):
        gw.TASK_ROOMS_FILE.parent.mkdir(parents=True, exist_ok=True)
        gw.TASK_ROOMS_FILE.write_text(json.dumps(rooms_map))
        for tid in results:
            (gw.RESULTS_DIR / f"{tid}.txt").write_text("done\n")
            self.addCleanup((gw.RESULTS_DIR / f"{tid}.txt").unlink)
        gw._maybe_core_state_notices(set(inflight))
        return self.captured[-1]

    def test_only_pending_matrix_rooms_reach_the_sweep(self):
        # t3 empty / t4 non-Matrix id → no room op; t5 has a ready result;
        # task-dev~t6 is a valid named-instance id. Only real pending rooms pass.
        rooms = self._rooms_for(
            {"t1": "!a:server", "t2": "!b:server", "t3": "",
             "t4": "C0FOREIGN", "t5": "!c:server", "task-dev~t6": "!d:server"},
            ["t1", "t2", "t3", "t4", "t5", "task-dev~t6",
             "../evil", "t-unmapped"],
            results=["t5"])
        self.assertEqual(rooms, {"!a:server", "!b:server", "!d:server"})

    def test_empty_inflight_hands_empty_rooms(self):
        # Still called: the sweep owns recovery notices, which need no rooms.
        self.assertEqual(self._rooms_for({}, []), set())


class RoomSidecarOrdering(unittest.TestCase):
    def test_room_recorded_before_task_publish(self):
        # #2: the room sidecar must commit BEFORE the task publishes, else a
        # crash between them queues a task the notice sweep can't route.
        order = []
        saved = {n: getattr(gw, n) for n in
                 ("_record_task_room", "_publish_staged", "_record_task_media",
                  "_write_owner_activity", "_task_pending")}
        gw._record_task_room = lambda tid, room: order.append("room")
        gw._publish_staged = lambda tmp, dest: (order.append("publish"), True)[1]
        gw._record_task_media = lambda tid, task: True
        gw._write_owner_activity = lambda *a, **k: None
        gw._task_pending = lambda tid: False
        try:
            out = gw._write_task({"id": "task-order-1", "task": "hi",
                                  "user_id": "@u:s", "channel_id": "!a:server",
                                  "access_tier": "guest",
                                  "timestamp": "2026-09-07T00:00:00Z"})
        finally:
            for n, fn in saved.items():
                setattr(gw, n, fn)
            for f in gw.TASKS_DIR.glob("task-order-1*"):
                f.unlink()
        self.assertEqual(out, ("task-order-1", True))
        self.assertEqual(order, ["room", "publish"])


class ExceptionContract(unittest.TestCase):
    def test_sweep_errors_are_swallowed(self):
        saved = gw.sweep_core_state_notices
        gw.sweep_core_state_notices = lambda *a, **k: (_ for _ in ()).throw(
            ValueError("boom"))
        try:
            gw._maybe_core_state_notices(set())  # must not raise
        finally:
            gw.sweep_core_state_notices = saved

    def test_auth_rejection_escapes_to_the_loop(self):
        saved = gw.sweep_core_state_notices
        gw.sweep_core_state_notices = lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.HTTPError("u", 401, "nope", {}, None))
        try:
            with self.assertRaises(urllib.error.HTTPError):
                gw._maybe_core_state_notices(set())
        finally:
            gw.sweep_core_state_notices = saved


if __name__ == "__main__":
    unittest.main()
