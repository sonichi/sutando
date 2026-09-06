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
        widen = lambda room, v: "available"  # a policy cannot invent a value outside the contract either
        self.assertEqual(av.availability_projection(s, "air", "!x:s", lambda r, v: "reviewing acquisition docs")["availability"],
                         "busy_accepting", "an off-contract value is ignored, never leaked")
        self.assertIn(av.availability_projection(s, "air", "!x:s", widen)["availability"], av.AVAILABILITY)


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
