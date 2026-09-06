#!/usr/bin/env python3
"""The owner's acceptance tests for the activity bus (2026-09-06): the card stays truthful with no
hook at all, a dead provider is not a failure, replays never regress a phase, and a restart
reconstructs the run. Plus the state machine itself and the two dedup keys."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src")
sys.path.insert(0, _SRC)
import activity_bus as bus  # noqa: E402
from activity_bus import ActivityStore, LifecycleTransition as T, RuntimeEvent as E, TaskActivityState, reduce  # noqa: E402

_spec = importlib.util.spec_from_file_location("card", os.path.join(_HERE, "..", "skills", "agent-activity", "scripts", "activity.py"))
card = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(card)


def phases(rows):
    return [(r["kind"], r["line"]) for r in rows]


class StateMachine(unittest.TestCase):
    def test_the_valid_transitions_and_only_those(self):
        s = TaskActivityState("t")
        for to in ("QUEUED", "RUNNING", "WAITING", "RUNNING", "COMPLETED"):
            s, _ = reduce(s, T("t", to, ts=1))
            self.assertEqual(s.phase, to)
        self.assertEqual(s.generation, 2, "each entry into RUNNING is a generation")
        s, rows = reduce(s, T("t", "RUNNING", ts=2))
        self.assertEqual((s.phase, rows, s.telemetry), ("COMPLETED", [], 1), "a terminal phase is sticky")
        s2, rows = reduce(TaskActivityState("u"), T("u", "WAITING", ts=1))
        self.assertEqual((s2.phase, rows), ("RECEIVED", []), "RECEIVED cannot jump to WAITING")

    def test_lifecycle_dedup_is_by_task_generation_from_to(self):
        s = TaskActivityState("t")
        s, r1 = reduce(s, T("t", "RUNNING", from_phase="RECEIVED", generation=0, ts=1))
        s, r2 = reduce(s, T("t", "RUNNING", from_phase="RECEIVED", generation=0, ts=1))
        self.assertEqual((len(r1), len(r2), s.generation), (1, 0, 1))
        # RUNNING → WAITING → RUNNING again is legitimate: a new generation, a new row
        s, _ = reduce(s, T("t", "WAITING", ts=2))
        s, r3 = reduce(s, T("t", "RUNNING", ts=3))
        self.assertEqual((len(r3), s.generation), (1, 2))

    def test_runtime_dedup_is_by_session_and_seq(self):
        s = TaskActivityState("t", phase="RUNNING", generation=1)
        s, r1 = reduce(s, E("t", "S1", 1, "Working", text="Bash: tests", ts=1))
        s, r2 = reduce(s, E("t", "S1", 1, "Working", text="Bash: tests", ts=1))  # duplicate
        s, r3 = reduce(s, E("t", "S1", 3, "Working", text="Edit: x", ts=3))
        s, r4 = reduce(s, E("t", "S1", 2, "Working", text="late", ts=2))  # out of order: dropped
        self.assertEqual((len(r1), len(r2), len(r3), len(r4), s.seq), (1, 0, 1, 0, 3))
        s, r5 = reduce(s, E("t", "S2", 1, "Working", text="other session", ts=4))
        self.assertEqual(len(r5), 1, "a second session has its own sequence")


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "state").mkdir()
        self.rows = []
        self.store = ActivityStore(self.ws, project=self.rows.append)

    def test_1_delete_every_hook_and_the_lifecycle_alone_is_truthful(self):
        # No RuntimeEvent at all: the scheduler's transitions carry the card from start to finish.
        self.store.apply(T("t1", "QUEUED", ts=1, message_event_id="$m", room="!r:s", sender="@q:s", text="Review PR"))
        self.store.apply(T("t1", "RUNNING", ts=2))
        st = self.store.apply(T("t1", "COMPLETED", ts=3))
        self.assertEqual(phases(self.rows), [("notice", "queued"), ("processing", "picked up"), ("done", "replied")])
        self.assertTrue(all(r["task"]["event"] == "$m" and r["room"] == "!r:s" for r in self.rows), "every row mounts under the message")
        self.assertEqual((st.phase, st.generation, st.started_at), ("COMPLETED", 1, 1))

    def test_2_a_provider_that_dies_halfway_leaves_the_task_running_not_failed(self):
        self.store.apply(T("t2", "RUNNING", ts=1, message_event_id="$m"))
        self.store.apply(E("t2", "S1", 1, "Working", text="Bash: build", ts=2))
        st = self.store.apply(E("t2", "S1", 2, "RuntimeStopped", ts=3))
        self.assertEqual(st.phase, "RUNNING", "an observation's absence is not a failure")
        self.assertEqual(phases(self.rows)[-1], ("working", "Bash: build"))
        st = self.store.apply(E("t2", "S1", 3, "Heartbeat", ts=4))
        self.assertEqual((st.phase, st.last_activity_at), ("RUNNING", 4), "heartbeats keep it alive without a row")
        self.assertEqual(len(self.rows), 2)

    def test_3_duplicate_and_out_of_order_events_never_regress_the_phase(self):
        self.store.apply(T("t3", "RUNNING", ts=1))
        self.store.apply(E("t3", "S1", 5, "InteractionRequired", text="pick one", ts=2))
        self.assertEqual(self.store.load("t3").phase, "WAITING")
        self.store.apply(E("t3", "S1", 6, "Working", text="Edit: y", ts=3))
        self.assertEqual(self.store.load("t3").phase, "RUNNING")
        # a replay of the old InteractionRequired (seq 5) must not put it back into WAITING
        self.store.apply(E("t3", "S1", 5, "InteractionRequired", text="pick one", ts=2))
        st = self.store.apply(T("t3", "COMPLETED", ts=4))
        self.assertEqual(st.phase, "COMPLETED")
        st = self.store.apply(E("t3", "S1", 7, "Working", text="late hook", ts=5))
        self.assertEqual((st.phase, st.telemetry), ("COMPLETED", 1), "a late event is telemetry, never a reopen")
        first = json.dumps(bus.asdict(st), sort_keys=True)
        st = self.store.apply(E("t3", "S1", 7, "Working", text="late hook", ts=5))
        self.assertEqual(json.dumps(bus.asdict(st), sort_keys=True), first, "deterministic under replay")

    def test_4_restart_during_running_reconstructs_the_task_without_a_second_run(self):
        self.store.apply(T("t4", "QUEUED", ts=1, message_event_id="$m", room="!r:s"))
        self.store.apply(E("t4", "S1", 1, "RuntimeStarted", ts=2))
        self.store.apply(E("t4", "S1", 2, "Working", text="Read: file", ts=3))
        before = self.store.load("t4")
        # "restart": a fresh store over the same workspace knows the same run
        fresh_rows = []
        fresh = ActivityStore(self.ws, project=fresh_rows.append)
        after = fresh.load("t4")
        self.assertEqual((after.phase, after.generation, after.seq, after.message_event_id), ("RUNNING", 1, 2, "$m"))
        self.assertEqual(bus.asdict(after), bus.asdict(before))
        st = fresh.apply(E("t4", "S1", 3, "RuntimeStarted", ts=4))  # the resumed provider announces itself again
        self.assertEqual((st.phase, st.generation, fresh_rows), ("RUNNING", 1, []), "no second run, no second row")

    def test_5_the_default_projection_is_the_real_row_writer_and_leaves_a_summary(self):
        real = ActivityStore(self.ws)
        real.apply(T("t5", "QUEUED", ts=1, message_event_id="$m", room="!r:s", sender="@q:s", text="hi"))
        real.apply(T("t5", "RUNNING", ts=2))
        real.apply(T("t5", "COMPLETED", ts=3))
        rows = [json.loads(l) for l in card.log_path(self.ws).read_text().splitlines()]
        self.assertEqual([(r["kind"], r["line"], r.get("done", False)) for r in rows],
                         [("notice", "queued", False), ("processing", "picked up", False), ("done", "replied", True)])
        self.assertTrue(all(r["task"]["event"] == "$m" for r in rows))
        summary = json.loads(card.summaries_path(self.ws).read_text().splitlines()[-1])
        self.assertEqual((summary["task"]["id"], summary["rows"], summary["line"]), ("t5", 3, "replied"))

    def test_a_failed_projection_is_retried_later_and_projected_exactly_once(self):
        # Reviewer's case: the snapshot advanced but the row was lost when projecting raised.
        projected = []
        calls = {"n": 0}
        def flaky(row):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            projected.append(row["line"])
        store = ActivityStore(self.ws, project=flaky)
        st = store.apply(T("t7", "RUNNING", ts=1, message_event_id="$m"))
        self.assertEqual((st.phase, projected, len(st.pending)), ("RUNNING", [], 1), "committed, row still owed")
        st = store.apply(T("t7", "COMPLETED", ts=2))
        self.assertEqual((st.phase, projected, st.pending), ("COMPLETED", ["picked up", "replied"], []))
        # a restart with a healthy projector drains what an earlier process left owed
        store.project = flaky
        calls["n"] = 0; projected.clear()
        st = store.apply(T("t8", "RUNNING", ts=1)); self.assertEqual(len(st.pending), 1)
        fresh = ActivityStore(self.ws, project=lambda r: projected.append(r["line"]))
        st = fresh.apply(E("t8", "S1", 1, "Heartbeat", ts=2))
        self.assertEqual((projected, st.pending), (["picked up"], []), "exactly once, on the next apply")

    def test_rows_are_projected_inside_the_task_lock(self):
        import inspect
        src = inspect.getsource(bus.ActivityStore.apply)
        self.assertLess(src.index("self._drain(state)", src.index("self.save(state)")), src.index("return state"))
        self.assertNotIn("\n        for row in rows:", src, "no projection after the lock block")

    def test_a_consolidated_completion_names_the_holder(self):
        st = self.store.apply(T("t6", "RUNNING", ts=1, message_event_id="$m"))
        st = self.store.apply(T("t6", "COMPLETED", ts=2, into="$holder"))
        self.assertEqual((self.rows[-1]["line"], self.rows[-1]["task"]["into"]), ("consolidated", "$holder"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
