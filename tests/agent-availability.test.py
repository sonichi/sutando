#!/usr/bin/env python3
"""The room availability contract is narrow and never leaks the private numbers; the task projection
says who is on it and since when, never what they are doing inside."""
from __future__ import annotations

import json
import subprocess
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import agent_availability as av  # noqa: E402
import activity_policy as pol  # noqa: E402
S = av.AgentRuntimeState


class Contract(unittest.TestCase):
    def test_the_five_values_and_what_maps_to_them(self):
        now = 1000.0
        fresh = dict(runtime_healthy=True, last_heartbeat_at=now - 10)
        self.assertEqual(av.availability(S(runtime_healthy=None), now), "unknown")
        self.assertEqual(av.availability(S(disconnected=True, active_runs=0), now), "offline")
        self.assertEqual(av.availability(S(active_runs=0, **fresh), now), "available")
        self.assertEqual(av.availability(S(active_runs=2, max_concurrency=4, **fresh), now), "busy_accepting")
        self.assertEqual(av.availability(S(active_runs=4, max_concurrency=4, **fresh), now), "busy_unavailable")
        self.assertEqual(av.availability(S(active_runs=0, accepting_work=False, **fresh), now), "busy_unavailable")
        self.assertEqual(av.availability(S(active_runs=0, queue_depth=2, max_concurrency=1, **fresh), now), "busy_accepting")

    def test_stale_is_unknown_and_only_an_explicit_disconnect_is_offline(self):
        # Invariant 2: offline is a known fact; unknown is missing telemetry. They never merge.
        now = 1000.0
        stale = S(runtime_healthy=True, active_runs=1, max_concurrency=4, last_heartbeat_at=now - 600)
        self.assertEqual(av.availability(stale, now), "unknown", "a dead runtime's last busy_accepting must not linger")
        self.assertEqual(av.availability(S(runtime_healthy=True, active_runs=1, last_heartbeat_at=None), now), "unknown")
        self.assertEqual(av.availability(S(disconnected=True, runtime_healthy=True, last_heartbeat_at=now - 1), now), "offline")

    def test_a_running_task_alone_does_not_mean_busy(self):
        # The owner's rule: never derive availability from "has a running task".
        self.assertEqual(av.availability(S(runtime_healthy=True, active_runs=1, max_concurrency=2, last_heartbeat_at=1000.0), 1001.0), "busy_accepting")

    def test_the_room_projection_carries_no_numbers_and_no_reasons(self):
        p = av.availability_projection(S(runtime_healthy=True, active_runs=3, max_concurrency=4, queue_depth=7, last_heartbeat_at=999.0), worker="air", now=1000.0)
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

    def test_the_room_guard_refuses_rather_than_trims_and_survives_python_O(self):
        with self.assertRaises(ValueError):
            av.room_payload({"worker": "air", "summary": "the acquisition"}, av.ROOM_AVAILABILITY_FIELDS)
        with self.assertRaises(ValueError):
            av.room_payload({"worker": "air", "queue_depth": 7}, av.ROOM_TASK_STATUS_FIELDS)
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); import agent_availability as av\n"
                 "try:\n    av.room_payload({'worker': 'a', 'thinking': 'x'}, av.ROOM_AVAILABILITY_FIELDS)\n"
                 "except ValueError:\n    print('refused')\nelse:\n    print('LEAKED')")
        out = subprocess.run([sys.executable, "-O", "-c", probe, src], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "refused", "an assert would be compiled out under -O; this guard is not")

    def test_the_task_projection_keeps_a_restricted_audience(self):
        for aud in ("owner", "selected_members", "system"):
            p = av.task_projection({"task_id": "t", "phase": "RUNNING", "audience": aud}, now=1.0)
            self.assertEqual(p["audience"], aud, "a restricted snapshot never becomes room-visible by projection")
        self.assertEqual(av.task_projection({"task_id": "t", "phase": "RUNNING", "audience": "room"}, now=1.0)["audience"], "room")

    def test_one_contract_the_wire_shape_derives_from_the_bus_snapshot(self):
        import activity_bus as bus
        st = bus.TaskActivityState(task_id="t9", phase="RUNNING", worker="air", message_event_id="$m", started_at=1.0,
                                   last_activity_at=5.0, summary="the acquisition", seq=3, activity_session_id="a1")
        snap = bus.shared_projection(st)
        self.assertFalse(set(snap) & av.FORBIDDEN_IN_ROOM, "the snapshot carries no private key")
        wire = av.task_projection(snap, now=10.0)
        self.assertEqual(set(wire), set(av.ROOM_TASK_STATUS_FIELDS))
        self.assertEqual((wire["phase"], wire["since_s"], wire["last_status_at"], wire["audience"]), ("RUNNING", 9.0, 5.0, snap["audience"]))

    def test_invariant_1_no_forbidden_field_ever_reaches_a_room_payload(self):
        # Privacy happens before projection and transport: the forbidden keys are absent by construction,
        # and no private text survives by value either.
        snap = {"task_id": "t", "message_event_id": "$m", "worker": "air", "phase": "RUNNING", "started_at": 1.0,
                "last_activity_at": 5.0, "summary": "reviewing the acquisition documents", "thinking": "hmm",
                "tool": "pytest", "command": "rm -rf", "private_room_id": "!board:s", "active_runs": 3,
                "capacity": 4, "queue_depth": 7, "reason": "confidential", "seq": 9, "activity_session_id": "a1"}
        state = S(runtime_healthy=True, active_runs=3, max_concurrency=4, queue_depth=7, last_heartbeat_at=999.0)
        for payload in (av.task_projection(snap, now=10.0), av.availability_projection(state, "air", "!eng:s", now=1000.0)):
            self.assertFalse(set(payload) & av.FORBIDDEN_IN_ROOM, payload)
            blob = json.dumps({k: v for k, v in payload.items() if k != "ts"})
            for secret in ("acquisition", "hmm", "pytest", "rm -rf", "!board:s", "confidential"):
                self.assertNotIn(secret, blob)
            for n in ("3", "4", "7", "9"):
                self.assertNotIn(f": {n}", blob.replace('"since_s": 9.0', ""))

    def test_a_stale_runtime_reads_unknown_from_the_engines_own_files(self):
        ws = Path(tempfile.mkdtemp()); (ws / "state" / "cores").mkdir(parents=True)
        beat = ws / "state" / "cores" / "mac.alive"; beat.write_text("{}")
        os.utime(beat, (time.time() - 600, time.time() - 600))
        self.assertEqual(av.availability(av.read_runtime_state(ws, host="mac", now=time.time()), time.time()), "unknown")
        beat.unlink(); (ws / "state" / "core-status.json").write_text(json.dumps({"status": "stopped"}))
        self.assertEqual(av.availability(av.read_runtime_state(ws, host="mac", now=time.time()), time.time()), "offline")


class PerRoom(unittest.TestCase):
    def test_a_room_policy_can_narrow_what_this_room_learns_but_never_widen_it(self):
        now = 1000.0
        s = S(runtime_healthy=True, active_runs=1, max_concurrency=4, last_heartbeat_at=now - 5)
        narrow = lambda room, v: "busy_unavailable" if room == "!engineering:s" and v == "busy_accepting" else v
        self.assertEqual(av.availability_projection(s, "air", "!engineering:s", narrow, now=now)["availability"], "busy_unavailable")
        self.assertEqual(av.availability_projection(s, "air", "!board:s", narrow, now=now)["availability"], "busy_accepting")
        widen = lambda room, v: "available"  # a policy cannot invent a value outside the contract either
        self.assertEqual(av.availability_projection(s, "air", "!x:s", lambda r, v: "reviewing acquisition docs", now=now)["availability"],
                         "busy_accepting", "an off-contract value is ignored, never leaked")
        self.assertIn(av.availability_projection(s, "air", "!x:s", widen, now=now)["availability"], av.AVAILABILITY)


class Policy(unittest.TestCase):
    """Invariant 3 and the tier mapping: TASK_STATUS follows the task's room, RUNTIME_DETAIL follows
    ownership, AVAILABILITY is room policy-filtered; tiers resolve to capabilities, never to branches."""

    def test_the_matrix_owner_same_room_other_room(self):
        def P(tier, viewer_room, task_room):
            return pol.projections_for(tier, viewer_room, task_room, viewer_is_room_member=True)
        self.assertEqual(P("owner", "!eng:s", "!eng:s"), {"RUNTIME_DETAIL", "TASK_STATUS", "AVAILABILITY"})
        self.assertEqual(P("owner", "!other:s", "!eng:s"), {"RUNTIME_DETAIL", "TASK_STATUS", "AVAILABILITY"})
        for tier in ("team", "guest", "none"):
            self.assertEqual(P(tier, "!eng:s", "!eng:s"), {"TASK_STATUS", "AVAILABILITY"}, tier)
            self.assertEqual(P(tier, "!other:s", "!eng:s"), {"AVAILABILITY"}, f"{tier}: another room never sees the task")
            self.assertNotIn("RUNTIME_DETAIL", P(tier, "!eng:s", "!eng:s"))

    def test_a_no_access_room_member_still_sees_the_rooms_own_task_but_nothing_of_the_agent(self):
        self.assertIn("TASK_STATUS", pol.projections_for("none", "!eng:s", "!eng:s", viewer_is_room_member=True))
        self.assertEqual(pol.projections_for("none", "!eng:s", "!eng:s"), set(), "membership is claimed, never assumed")
        self.assertNotIn("agent.invoke", pol.capabilities("none", room_member=True))
        self.assertEqual(pol.projections_for("none", "!eng:s", "!eng:s", viewer_is_room_member=False), set())

    def test_tiers_resolve_to_capabilities(self):
        self.assertIn("agent.configure", pol.capabilities("owner"))
        self.assertIn("agent.invoke", pol.capabilities("team")); self.assertNotIn("agent.invoke", pol.capabilities("guest"))
        self.assertEqual(pol.capabilities("nonsense"), frozenset())


class WorkSignal(unittest.TestCase):
    """Chi's rule: core-status and the heartbeat are liveness, not work — a wedged core keeps both
    fresh. The CLI wedge detector's reading is the primary input; the heartbeat is the fallback."""

    def test_verdict_kinds_fold_to_the_three_signals(self):
        for kind, sig in (("working", "working"), ("clock-only", "working"), ("idle", "idle"),
                          ("static-with-work", "wedged"), ("retry-loop", "wedged"), ("provider-limit", "wedged"),
                          ("low-novelty", "wedged"), ("unknown", "unknown"), ("cadence-too-sparse", "unknown")):
            self.assertEqual(av.work_signal_from_verdict({"kind": kind, "confidence": "high"}), sig, kind)
        self.assertEqual(av.work_signal_from_verdict(None), "unknown")
        self.assertEqual(av.work_signal_from_verdict("garbage"), "unknown")

    def test_a_wedged_core_is_busy_unavailable_even_with_a_fresh_heartbeat(self):
        s = S(runtime_healthy=True, active_runs=0, last_heartbeat_at=1000.0, work_signal="wedged")
        self.assertEqual(av.availability(s, 1001.0), "busy_unavailable", "fresh heartbeat, no work: not available")
        self.assertEqual(av.availability(S(runtime_healthy=True, last_heartbeat_at=1.0, work_signal="wedged"), 1000.0),
                         "busy_unavailable", "a stale heartbeat does not demote a pane reading to unknown")
        self.assertEqual(av.availability(S(disconnected=True, work_signal="wedged")), "offline", "a known disconnect wins")

    def test_idle_and_working_readings_outrank_the_heartbeat(self):
        self.assertEqual(av.availability(S(runtime_healthy=True, last_heartbeat_at=1.0, work_signal="idle"), 1000.0), "available")
        self.assertEqual(av.availability(S(runtime_healthy=True, last_heartbeat_at=1.0, work_signal="idle", queue_depth=2), 1000.0),
                         "busy_accepting", "idle at the prompt with a queue is about to be busy")
        self.assertEqual(av.availability(S(runtime_healthy=True, active_runs=1, max_concurrency=2, work_signal="working"), 5.0),
                         "busy_accepting")
        self.assertEqual(av.availability(S(runtime_healthy=True, active_runs=1, max_concurrency=1, work_signal="working"), 5.0),
                         "busy_unavailable")
        self.assertEqual(av.availability(S(accepting_work=False, work_signal="idle")), "busy_unavailable")

    def test_a_stale_pane_reading_is_no_reading(self):
        self.assertEqual(av.work_signal_from_verdict({"kind": "idle", "last_sample_age_s": 30.0}), "idle")
        self.assertEqual(av.work_signal_from_verdict({"kind": "idle", "last_sample_age_s": 600.0}), "unknown")
        self.assertEqual(av.work_signal_from_verdict({"kind": "working", "last_sample_age_s": av.WORK_SIGNAL_MAX_AGE_S + 1}), "unknown")

    def test_the_persisted_window_never_keeps_a_dead_core_available(self):
        # The reviewer's control, through the production window writer and the default probe: two
        # idle samples at 1000/1060, a heartbeat last touched at 1060, then the clock moves on.
        try:
            import cli_wedge
        except ImportError:
            self.skipTest("the detector is not on this tree")
        ws = Path(tempfile.mkdtemp()); (ws / "state" / "cores").mkdir(parents=True); (ws / "tasks").mkdir()
        cli_wedge.append_window(ws, "> idle prompt\n", 1000.0); cli_wedge.append_window(ws, "> idle prompt\n", 1060.0)
        beat = ws / "state" / "cores" / "h.alive"; beat.write_text("{}"); os.utime(beat, (1060.0, 1060.0))
        reading = lambda now: av.availability(av.read_runtime_state(ws, host="h", now=now), now)
        self.assertEqual(reading(1061.0), "available", "a fresh pane reading counts")
        self.assertEqual(reading(1660.0), "unknown", "600 s old: the window is no reading, the heartbeat is stale")
        self.assertEqual(reading(2260.0), "unknown")
        beat.unlink()
        self.assertEqual(reading(1060.0 + 1200.0), "unknown", "no heartbeat and a 20-minute-old window")

    def test_no_reading_falls_back_to_the_heartbeat_rule(self):
        self.assertEqual(av.availability(S(runtime_healthy=True, last_heartbeat_at=1000.0), 1001.0), "available")
        self.assertEqual(av.availability(S(runtime_healthy=True, last_heartbeat_at=1.0), 1000.0), "unknown", "stale is unknown")

    def test_read_runtime_state_takes_the_probe_and_survives_its_failure(self):
        ws = Path(tempfile.mkdtemp()); (ws / "state").mkdir()
        self.assertEqual(av.read_runtime_state(ws, host="h", work_probe=lambda w, n: {"kind": "static-with-work"}).work_signal, "wedged")
        self.assertEqual(av.read_runtime_state(ws, host="h", work_probe=lambda w, n: None).work_signal, "unknown")
        def boom(w, n):
            raise RuntimeError("tmux gone")
        self.assertEqual(av.read_runtime_state(ws, host="h", work_probe=boom).work_signal, "unknown")
        self.assertEqual(av.read_runtime_state(ws, host="h").work_signal, "unknown", "no detector window here: unknown, never a crash")

    def test_the_work_signal_never_reaches_a_room_payload(self):
        s = S(runtime_healthy=True, last_heartbeat_at=1000.0, work_signal="wedged")
        p = av.availability_projection(s, "air", "!eng:s", now=1000.0)
        self.assertNotIn("work_signal", p); self.assertIn("work_signal", av.FORBIDDEN_IN_ROOM)
        self.assertEqual(p["availability"], "busy_unavailable")


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
        self.assertEqual(av.availability(s, time.time()), "busy_unavailable")
        self.assertEqual(av.availability(av.read_runtime_state(Path(tempfile.mkdtemp())), time.time()), "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
