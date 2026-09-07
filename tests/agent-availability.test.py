#!/usr/bin/env python3
"""The room availability contract is narrow and never leaks the private numbers; the task projection
says who is on it and since when, never what they are doing inside."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import agent_availability as av  # noqa: E402
from agent_availability import AgentRuntimeState as S  # noqa: E402


class Contract(unittest.TestCase):
    def test_the_five_values_and_what_maps_to_them(self):
        self.assertEqual(av.availability(S(runtime_healthy=None)), "unknown")
        self.assertEqual(av.availability(S(runtime_healthy=False, active_runs=0)), "offline")
        self.assertEqual(av.availability(S(runtime_healthy=True, active_runs=0)), "available")
        self.assertEqual(av.availability(S(runtime_healthy=True, active_runs=2, max_concurrency=4)), "busy_accepting")
        self.assertEqual(av.availability(S(runtime_healthy=True, active_runs=4, max_concurrency=4)), "busy_unavailable")
        self.assertEqual(av.availability(S(runtime_healthy=True, active_runs=0, accepting_work=False)), "busy_unavailable")
        self.assertEqual(av.availability(S(runtime_healthy=True, active_runs=0, queue_depth=2, max_concurrency=1)), "busy_accepting")

    def test_a_running_task_alone_does_not_mean_busy(self):
        # The owner's rule: never derive availability from "has a running task".
        self.assertEqual(av.availability(S(runtime_healthy=True, active_runs=1, max_concurrency=2)), "busy_accepting")

    def test_the_room_projection_carries_no_numbers_and_no_reasons(self):
        p = av.availability_projection(S(runtime_healthy=True, active_runs=3, max_concurrency=4, queue_depth=7), worker="air")
        self.assertEqual(set(p), {"worker", "room", "availability", "audience", "projection", "ts"})
        self.assertEqual((p["availability"], p["audience"], p["projection"]), ("busy_accepting", "room", "AVAILABILITY"))
        blob = json.dumps({k: v for k, v in p.items() if k != "ts"})
        self.assertNotIn("3", blob); self.assertNotIn("7", blob)

    def test_the_task_projection_says_who_and_since_when_but_not_what(self):
        snap = {"task_id": "t", "message_event_id": "$m", "worker": "air", "phase": "RUNNING", "started_at": 1000.0,
                "summary": "reviewing the acquisition documents", "seq": 9}
        p = av.task_projection(snap, now=1360.0)
        self.assertEqual((p["worker"], p["phase"], p["since_s"], p["audience"], p["projection"]),
                         ("air", "RUNNING", 360.0, "room", "TASK_STATUS"))
        self.assertNotIn("summary", p); self.assertNotIn("seq", p)
        self.assertNotIn("acquisition", json.dumps(p))


class PerRoom(unittest.TestCase):
    def test_a_room_policy_can_narrow_what_this_room_learns_but_never_widen_it(self):
        s = S(runtime_healthy=True, active_runs=1, max_concurrency=4)
        narrow = lambda room, v: "busy_unavailable" if room == "!engineering:s" and v == "busy_accepting" else v
        self.assertEqual(av.availability_projection(s, "air", "!engineering:s", narrow)["availability"], "busy_unavailable")
        self.assertEqual(av.availability_projection(s, "air", "!board:s", narrow)["availability"], "busy_accepting")
        self.assertEqual(av.availability_projection(s, "air", "!x:s", lambda r, v: "reviewing acquisition docs")["availability"],
                         "busy_accepting", "an off-contract value is ignored, never leaked")
        # Widening is refused, not merely kept inside the enum: the true value stands.
        self.assertEqual(av.availability_projection(s, "air", "!x:s", lambda r, v: "available")["availability"], "busy_accepting")
        full = S(runtime_healthy=True, active_runs=4, max_concurrency=4)
        self.assertEqual(av.availability_projection(full, "air", "!x:s", lambda r, v: "busy_accepting")["availability"], "busy_unavailable")
        self.assertEqual(av.availability_projection(full, "air", "!x:s", lambda r, v: "unknown")["availability"], "unknown", "less is allowed")
        self.assertEqual(av.availability_projection(S(runtime_healthy=False), "air", "!x:s", lambda r, v: "available")["availability"], "offline",
                         "a known fact is never relabelled available")
        for true, allowed in av.NARROWING.items():
            self.assertIn(true, allowed); self.assertIn("unknown", allowed)
            if true != "available":
                self.assertNotIn("available", allowed)

    def test_a_restricted_snapshot_never_widens_to_room(self):
        for aud in ("owner", "selected_members", "system"):
            self.assertEqual(av.task_projection({"task_id": "t", "phase": "RUNNING", "audience": aud})["audience"], aud)
        self.assertEqual(av.task_projection({"task_id": "t", "phase": "RUNNING", "audience": "room"})["audience"], "room")
        self.assertEqual(av.task_projection({"task_id": "t", "phase": "RUNNING"})["audience"], "room", "no label: room, the lifecycle default")


class ThisHost(unittest.TestCase):
    """state/cores/ is synced across hosts, so a glob lets a PEER's heartbeat answer for this agent.
    Only this host's file counts."""

    def test_a_peers_fresh_heartbeat_never_makes_a_dead_host_look_alive(self):
        ws = Path(tempfile.mkdtemp()); (ws / "state" / "cores").mkdir(parents=True)
        (ws / "state" / "cores" / "peer.alive").write_text("{}")
        mine = ws / "state" / "cores" / "mac.alive"; mine.write_text("{}")
        os.utime(mine, (time.time() - 4000, time.time() - 4000))
        s = av.read_runtime_state(ws, host="mac", now=time.time())
        self.assertNotEqual(av.availability(s), "available", "my own heartbeat is stale; the peer's does not count")
        self.assertNotEqual(av.availability(av.read_runtime_state(ws, host="nobody", now=time.time())), "available")
        self.assertIsInstance(av.this_host(), str); self.assertTrue(av.this_host())


class Fallbacks(unittest.TestCase):
    def test_the_host_label_falls_back_to_the_node_name_when_the_resolver_is_missing(self):
        import platform
        with unittest.mock.patch.dict(sys.modules, {"util_paths": None}):  # the import raises
            self.assertEqual(av.this_host(), platform.node().split(".")[0])

    def test_an_unreadable_task_snapshot_is_skipped_not_counted(self):
        ws = Path(tempfile.mkdtemp()); (ws / "state" / "activity").mkdir(parents=True)
        (ws / "state" / "activity" / "task-ok.json").write_text(json.dumps({"task_id": "task-ok", "phase": "RUNNING"}))
        (ws / "state" / "activity" / "task-bad.json").write_text("{not json")
        self.assertEqual(av.read_runtime_state(ws, host="h", now=1.0).active_runs, 1)


class StaleSnapshots(unittest.TestCase):
    """yixuan's gap: a core that dies between RUNNING and a terminal phase leaves the snapshot behind and a
    fresh heartbeat cannot mask it. The boundary that shares the task lifecycle is the task watcher (it
    dispatches every task and dies with the core session), never the heartbeat writer."""

    def _ws(self, watcher_started_at=None):
        ws = Path(tempfile.mkdtemp()); (ws / "state" / "activity").mkdir(parents=True); (ws / "state" / "cores").mkdir(); (ws / "tasks").mkdir()
        (ws / "state" / "cores" / "h.alive").write_text("{}")
        if watcher_started_at is not None:
            pf = ws / "state" / "watch-tasks-stream.pid"; pf.write_text("1"); os.utime(pf, (watcher_started_at, watcher_started_at))
        return ws

    def _snapshot(self, ws, written_at, task_file=False):
        p = ws / "state" / "activity" / "task-s1.json"; p.write_text(json.dumps({"task_id": "task-s1", "phase": "RUNNING"}))
        os.utime(p, (written_at, written_at))
        if task_file:
            (ws / "tasks" / "task-s1.txt").write_text("id: task-s1\n")

    def _runs(self, ws, now):
        os.utime(ws / "state" / "cores" / "h.alive", (now, now))
        return av.read_runtime_state(ws, host="h", now=now)

    def test_a_stale_running_snapshot_beside_a_fresh_heartbeat_is_not_work(self):
        now = time.time()
        for age in (24 * 3600.0, 365 * 24 * 3600.0):
            ws = self._ws(); self._snapshot(ws, now - age)
            s = self._runs(ws, now)
            self.assertEqual((s.active_runs, s.runtime_healthy), (0, True), f"age {age}")
            self.assertEqual(av.availability(s), "available")
        ws = self._ws(); self._snapshot(ws, now - 60.0)
        self.assertEqual(self._runs(ws, now).active_runs, 1, "control: a live run still counts")

    def test_a_previous_sessions_snapshot_is_a_leftover_however_the_heartbeat_writer_lived(self):
        # Retained heartbeat writer + core restart: the watcher is new, the task file is gone, the
        # snapshot is 5 minutes old — a leftover, not work.
        now = time.time()
        ws = self._ws(watcher_started_at=now - 120.0); self._snapshot(ws, now - 300.0)
        self.assertEqual(self._runs(ws, now).active_runs, 0)
        # Heartbeat writer restarted under a still-running core: the watcher predates the snapshot and
        # the task is still in the queue — a live, silent run keeps counting.
        ws = self._ws(watcher_started_at=now - 3600.0); self._snapshot(ws, now - 2400.0, task_file=True)
        self.assertEqual(self._runs(ws, now).active_runs, 1)
        # A task re-dispatched after a restart is in the queue again: live, whatever its snapshot's age.
        ws = self._ws(watcher_started_at=now - 120.0); self._snapshot(ws, now - 300.0, task_file=True)
        self.assertEqual(self._runs(ws, now).active_runs, 1)
        self.assertFalse(av.snapshot_is_live(now - 10.0, now, False, now - 5.0)); self.assertTrue(av.snapshot_is_live(now - 10.0, now, False, None))


class Reading(unittest.TestCase):
    def test_the_private_numbers_come_from_what_the_engine_already_writes(self):
        ws = Path(tempfile.mkdtemp())
        (ws / "state" / "activity").mkdir(parents=True); (ws / "state" / "cores").mkdir(); (ws / "tasks").mkdir()
        for tid, phase in (("task-a", "RUNNING"), ("task-b", "WAITING"), ("task-c", "COMPLETED")):
            (ws / "state" / "activity" / f"{tid}.json").write_text(json.dumps({"task_id": tid, "phase": phase}))
        (ws / "tasks" / "task-q1.txt").write_text("id: task-q1\n"); (ws / "tasks" / "task-cron-x.txt").write_text("id: task-cron-x\n")
        beat = ws / "state" / "cores" / "mac.alive"; beat.write_text("{}")
        s = av.read_runtime_state(ws, host="mac", max_concurrency=2, now=time.time())
        self.assertEqual((s.active_runs, s.queue_depth, s.runtime_healthy, s.max_concurrency), (2, 1, True, 2))
        self.assertEqual(av.availability(s), "busy_unavailable")
        os.utime(beat, (time.time() - 600, time.time() - 600))
        self.assertEqual(av.availability(av.read_runtime_state(ws, host="mac", now=time.time())), "offline")
        self.assertEqual(av.availability(av.read_runtime_state(Path(tempfile.mkdtemp()))), "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
