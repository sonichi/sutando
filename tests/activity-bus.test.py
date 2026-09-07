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
import unittest.mock
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


class IdempotentProjection(unittest.TestCase):
    """Reviewer's case: the real row writer appends the row, then raises while saving its index. The
    row stays owed; the replay must not publish it twice, and the summary must land exactly once."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp()); (self.ws / "state").mkdir()

    def rows(self):
        return [json.loads(l)["line"] for l in card.log_path(self.ws).read_text().splitlines()]

    def test_a_failure_after_the_append_is_replayed_exactly_once(self):
        import activity_rows
        store = ActivityStore(self.ws)
        store.apply(T("task-i1", "RUNNING", ts=1, message_event_id="$m"))
        self.assertEqual(self.rows(), ["picked up"])
        real = activity_rows._save_index
        calls = {"n": 0}
        def flaky(ip, idx):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("index disk full")  # AFTER the log append and the summary write
            real(ip, idx)
        with unittest.mock.patch.object(activity_rows, "_save_index", flaky):
            st = store.apply(T("task-i1", "COMPLETED", ts=2))
        self.assertEqual((st.phase, len(st.pending)), ("COMPLETED", 1), "committed, row still owed")
        self.assertEqual(self.rows(), ["picked up", "replied"], "the row DID land before the failure")
        self.assertTrue(all("pid" in json.loads(l) for l in card.log_path(self.ws).read_text().splitlines()))
        fresh = ActivityStore(self.ws)  # a restart: the drain replays the owed row
        fresh._drain(fresh.load("task-i1"))
        self.assertEqual(self.rows(), ["picked up", "replied"], "replayed, not duplicated")
        self.assertEqual(len(fresh.load("task-i1").pending), 0)
        sums = [json.loads(l) for l in card.summaries_path(self.ws).read_text().splitlines()]
        self.assertEqual([(x["task"]["id"], x["rows"]) for x in sums], [("task-i1", 2)], "one summary, exact")
        self.assertNotIn("task-i1", card.open_task_index(self.ws))

    def test_a_replay_after_rotation_is_still_applied_once(self):
        # Reviewer's case: the owed row rotates into the day archive before recovery. The writer's
        # acknowledgement ledger, not the rotating live log, is what recognises the replay.
        import activity_rows
        store = ActivityStore(self.ws)
        store.apply(T("task-i3", "RUNNING", ts=1, message_event_id="$m"))
        real = activity_rows._save_index; calls = {"n": 0}
        def flaky(ip, idx):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("index disk full")
            real(ip, idx)
        with unittest.mock.patch.object(activity_rows, "_save_index", flaky):
            store.apply(T("task-i3", "COMPLETED", ts=2))
        for i in range(card.LIVE_ROWS + 1):
            card.append(f"noise {i}", kind="notice", room=None, workspace=self.ws)
        live = card.log_path(self.ws).read_text()
        self.assertNotIn("task-i3", live, "precondition: the task's rows rotated into the archive")
        fresh = ActivityStore(self.ws)
        fresh.apply(T("task-i3", "COMPLETED", ts=2))  # the public entry point performs the recovery
        history = "".join(p.read_text() for p in (self.ws / "state").glob("agent-activity*.jsonl") if "summaries" not in p.name)
        self.assertEqual(history.count('"pid": "task-i3:1:2"'), 1, "the done row appears once across live + archive")
        self.assertEqual(len(fresh.load("task-i3").pending), 0)
        self.assertEqual(len(card.summaries_path(self.ws).read_text().splitlines()), 1)

    def test_unrelated_pid_bearing_traffic_never_evicts_an_owed_rows_ack(self):
        # yixuan's finding: the filler must carry pids of OTHER tasks, the memory a global ledger
        # would evict. Per-task acknowledgement cannot be evicted by anyone else's rows.
        import activity_rows
        store = ActivityStore(self.ws)
        store.apply(T("task-i4", "RUNNING", ts=1, message_event_id="$m"))
        real = activity_rows._save_index; calls = {"n": 0}
        def flaky(ip, idx):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("index disk full")
            real(ip, idx)
        with unittest.mock.patch.object(activity_rows, "_save_index", flaky):
            store.apply(T("task-i4", "COMPLETED", ts=2))
        for i in range(6100):
            card.append(f"noise {i}", kind="notice", room=None, task={"id": f"task-n{i % 50}"}, workspace=self.ws, pid=f"pid-noise-{i}")
        self.assertNotIn("task-i4", card.log_path(self.ws).read_text(), "precondition: rotated out")
        fresh = ActivityStore(self.ws)
        fresh.apply(T("task-i4", "COMPLETED", ts=2))
        history = "".join(p.read_text() for p in (self.ws / "state").glob("agent-activity*.jsonl") if "summaries" not in p.name)
        self.assertEqual(history.count('"pid": "task-i4:1:2"'), 1, "still exactly once after 6100 pid-bearing rows")
        self.assertEqual(len(fresh.load("task-i4").pending), 0)
        # positive control: a brand-new pid still appends exactly one row
        card.append("fresh", kind="notice", room=None, task={"id": "task-i4"}, workspace=self.ws, pid="task-i4:1:99")
        self.assertEqual(card.log_path(self.ws).read_text().count('"pid": "task-i4:1:99"'), 1)

    def test_a_closed_task_leaves_no_ack_file_behind(self):
        import activity_rows
        t = {"id": "task-i5"}
        card.append("picked up", kind="processing", room="!r:s", task=t, workspace=self.ws, pid="task-i5:1:1")
        self.assertTrue((activity_rows.acks_dir(self.ws) / "task-i5").exists())
        card.append("replied", kind="done", room="!r:s", task=t, done=True, workspace=self.ws, pid="task-i5:1:2")
        self.assertFalse((activity_rows.acks_dir(self.ws) / "task-i5").exists(), "the summary carries the done pid; the file goes")
        card.append("replied", kind="done", room="!r:s", task=t, done=True, workspace=self.ws, pid="task-i5:1:2")
        self.assertEqual(len(card.summaries_path(self.ws).read_text().splitlines()), 1, "a replayed done row is still one summary")

    def test_a_row_whose_ack_never_landed_is_still_found_after_rotation(self):
        # The log append completes, then _ack raises: once the row rotates no memory of it remains, so
        # the replay must find it in the archive day file its own (event, not replay) timestamp names.
        import activity_rows
        for fault in (False, True):
            ws = Path(tempfile.mkdtemp()); (ws / "state").mkdir()
            real = activity_rows._ack; calls = {"n": 0}
            def flaky(w, tid, pid):
                calls["n"] += 1
                if fault and calls["n"] == 1:
                    raise OSError("ack disk full")  # AFTER the log append
                real(w, tid, pid)
            store = ActivityStore(ws)
            with unittest.mock.patch.object(activity_rows, "_ack", flaky):
                st = store.apply(T("task-g1", "RUNNING", ts=1_757_000_000.0, message_event_id="$m"))
            self.assertEqual(len(st.pending), 1 if fault else 0, "a faulted ack leaves the row owed")
            for i in range(card.LIVE_ROWS + 1):
                card.append(f"noise {i}", kind="notice", room=None, workspace=ws)
            self.assertNotIn("task-g1", card.log_path(ws).read_text(), "precondition: rotated out")
            fresh = ActivityStore(ws); fresh.apply(T("task-g1", "COMPLETED", ts=1_757_000_002.0))
            history = [json.loads(l) for p in (ws / "state").glob("agent-activity*.jsonl") if "summaries" not in p.name for l in p.read_text().splitlines()]
            mine = [r for r in history if r.get("task", {}).get("id") == "task-g1"]
            self.assertEqual([r["line"] for r in sorted(mine, key=lambda r: r["ts"])], ["picked up", "replied"], f"fault={fault}")
            self.assertEqual([r["ts"] for r in sorted(mine, key=lambda r: r["ts"])], [1_757_000_000.0, 1_757_000_002.0], "event timestamps, not a replay clock")
            self.assertEqual(len(fresh.load("task-g1").pending), 0)
            sums = [json.loads(l) for l in card.summaries_path(ws).read_text().splitlines()]
            self.assertEqual([x["rows"] for x in sums], [2], f"fault={fault}: the summary counts two rows, once")

    def test_an_archived_unacked_row_is_found_whatever_the_live_windows_order(self):
        # yixuan's residual: after a replayed old-ts row lands in the live log, any "older than the
        # window" reading lies. The lookup must not depend on live order at all.
        import activity_rows
        t = {"id": "task-h1"}
        with unittest.mock.patch.object(activity_rows, "_ack", lambda w, tid, pid: None):  # the ack never lands
            card.append("picked up", kind="processing", room="!r:s", task=t, workspace=self.ws, pid="task-h1:1:1", ts=1_757_000_000.0)
        for i in range(card.LIVE_ROWS + 1):
            card.append(f"noise {i}", kind="notice", room=None, workspace=self.ws)
        self.assertNotIn("task-h1", card.log_path(self.ws).read_text(), "precondition: the victim is archived")
        # a replayed row with an ancient event ts now sits in the live window, both older and newer rows around it
        card.append("old replay", kind="notice", room=None, task={"id": "task-o"}, workspace=self.ws, pid="task-o:1:1", ts=1.0)
        card.append("picked up", kind="processing", room="!r:s", task=t, workspace=self.ws, pid="task-h1:1:1", ts=1_757_000_000.0, replay=True)
        history = "".join(p.read_text() for p in (self.ws / "state").glob("agent-activity*.jsonl") if "summaries" not in p.name)
        self.assertEqual(history.count('"pid": "task-h1:1:1"'), 1, "found in the archive; not appended again")
        card.append("fresh", kind="notice", room=None, task=t, workspace=self.ws, pid="task-h1:1:9")
        self.assertEqual(card.log_path(self.ws).read_text().count('"pid": "task-h1:1:9"'), 1, "control: a new pid appends once")

    def test_the_index_never_counts_a_replayed_pid_twice_whatever_landed_between(self):
        # The drained-snapshot save fails once AFTER row, ack and index landed; a same-task hook row
        # lands through the shared writer; the replay must not count the pickup again (4 arms).
        import activity_rows
        for fault, hook in ((False, False), (True, False), (False, True), (True, True)):
            ws = Path(tempfile.mkdtemp()); (ws / "state").mkdir()
            store = ActivityStore(ws); real_save = store.save; hit = {"n": 0}
            def faulty_save(state):
                if fault and not state.pending and hit["n"] == 0:
                    hit["n"] += 1
                    raise OSError("snapshot disk full")  # the drained snapshot never publishes
                real_save(state)
            store.save = faulty_save
            if fault:
                with self.assertRaises(OSError):
                    store.apply(T("task-x1", "RUNNING", ts=1_757_000_000.0, message_event_id="$m"))
                self.assertEqual(len(store.load("task-x1").pending), 1, "the pickup row is still owed")
            else:
                store.apply(T("task-x1", "RUNNING", ts=1_757_000_000.0, message_event_id="$m"))
            if hook:
                card.append("hook detail", kind="working", room="!r:s", task={"id": "task-x1"}, workspace=ws)
            fresh = ActivityStore(ws); fresh.apply(T("task-x1", "COMPLETED", ts=1_757_000_002.0))
            self.assertEqual(len(fresh.load("task-x1").pending), 0)
            rows = [json.loads(l) for p in (ws / "state").glob("agent-activity*.jsonl") if "summaries" not in p.name for l in p.read_text().splitlines()]
            mine = sorted((r for r in rows if r.get("task", {}).get("id") == "task-x1"), key=lambda r: r["ts"])
            sums = [json.loads(l) for l in card.summaries_path(ws).read_text().splitlines()]
            expect = 3 if hook else 2
            self.assertEqual(len(mine), expect, f"fault={fault} hook={hook}: actual rows")
            self.assertEqual([x["rows"] for x in sums], [expect], f"fault={fault} hook={hook}: the summary counts each row once")

    def test_a_lower_counter_landing_late_still_counts_and_its_replay_still_does_not(self):
        # yixuan's axis: the watermark alone would under-count a lower counter arriving after a
        # higher one. A row that lands now is new whatever its counter; only a replay defers to it.
        t = {"id": "task-o1"}
        card.append("second", kind="working", room="!r:s", task=t, workspace=self.ws, pid="task-o1:1:2", ts=2.0)
        card.append("first, late", kind="processing", room="!r:s", task=t, workspace=self.ws, pid="task-o1:1:1", ts=1.0)
        card.append("first, late", kind="processing", room="!r:s", task=t, workspace=self.ws, pid="task-o1:1:1", ts=1.0)  # a replay
        card.append("second", kind="working", room="!r:s", task=t, workspace=self.ws, pid="task-o1:1:2", ts=2.0)  # a replay
        idx = json.loads(card.index_path(self.ws).read_text())["task-o1"]
        self.assertEqual((idx["rows"], idx["applied"]), (2, {"1": 2}))
        card.append("replied", kind="done", room="!r:s", task=t, done=True, workspace=self.ws, pid="task-o1:1:3", ts=3.0)
        sums = [json.loads(l) for l in card.summaries_path(self.ws).read_text().splitlines()]
        self.assertEqual([x["rows"] for x in sums], [3])

    def test_recovery_across_the_index_format_change_counts_the_old_pickup_once(self):
        # The previous writer left rows=1 and last_pid (no applied map) with the pickup row still
        # owed; the new writer's replay must carry that evidence over, not count the pickup again.
        store = ActivityStore(self.ws)
        store.apply(T("task-up", "RUNNING", ts=1_757_000_000.0, message_event_id="$m"))  # row, ack, index landed
        ip = card.index_path(self.ws); idx = json.loads(ip.read_text()); e = idx["task-up"]
        self.assertEqual(e["rows"], 1); e.pop("applied"); e["last_pid"] = "task-up:1:1"  # the old format on disk
        ip.write_text(json.dumps(idx))
        st = store.load("task-up"); st.pending = [{"kind": "processing", "line": "picked up", "ts": 1_757_000_000.0, "room": "!r:s",
                                                   "task": {"id": "task-up"}, "done": False, "pid": "task-up:1:1", "attempts": 1}]
        store.save(st)  # the drained snapshot never published: the pickup is still owed
        fresh = ActivityStore(self.ws); fresh.apply(T("task-up", "COMPLETED", ts=1_757_000_002.0))
        self.assertEqual(len(fresh.load("task-up").pending), 0)
        sums = [json.loads(l) for l in card.summaries_path(self.ws).read_text().splitlines()]
        self.assertEqual([x["rows"] for x in sums], [2], "pickup + replied, the replayed pickup counted once")

    def test_recovery_across_the_format_change_survives_a_pidless_hook_that_nulled_last_pid(self):
        # The previous writer counted pickup + hook (rows=2) and its slot reads None after the hook;
        # the owed pickup's replay under the new writer must not count a third time.
        store = ActivityStore(self.ws)
        store.apply(T("task-up2", "RUNNING", ts=1_757_000_000.0, message_event_id="$m"))
        card.append("hook detail", kind="working", room="!r:s", task={"id": "task-up2"}, workspace=self.ws)
        ip = card.index_path(self.ws); idx = json.loads(ip.read_text()); e = idx["task-up2"]
        self.assertEqual(e["rows"], 2); e.pop("applied"); e["last_pid"] = None  # the old format after a pidless row
        ip.write_text(json.dumps(idx))
        st = store.load("task-up2"); st.pending = [{"kind": "processing", "line": "picked up", "ts": 1_757_000_000.0, "room": "!r:s",
                                                    "task": {"id": "task-up2"}, "done": False, "pid": "task-up2:1:1", "attempts": 1}]
        store.save(st)
        fresh = ActivityStore(self.ws); fresh.apply(T("task-up2", "COMPLETED", ts=1_757_000_002.0))
        self.assertEqual(len(fresh.load("task-up2").pending), 0)
        sums = [json.loads(l) for l in card.summaries_path(self.ws).read_text().splitlines()]
        self.assertEqual([x["rows"] for x in sums], [3], "pickup + hook + replied; the replayed pickup counted once")

    def test_recovery_across_the_format_change_adds_the_count_an_old_index_save_lost(self):
        # The previous writer appended row + ack, then its index save failed: the entry (rows=1,
        # last_pid=:1:1) does not include the owed WAITING row that is on disk. Both arms: rows live, rows rotated.
        import activity_rows
        for rotate in (False, True):
            ws = Path(tempfile.mkdtemp()); (ws / "state").mkdir(); store = ActivityStore(ws)
            store.apply(T("task-ix", "RUNNING", ts=1_757_000_000.0, message_event_id="$m"))
            real = activity_rows._save_index; calls = []
            def fail_once(path, data):
                if not calls:
                    calls.append(1)
                    raise OSError("one index publication fault")
                return real(path, data)
            with unittest.mock.patch.object(activity_rows, "_save_index", fail_once):
                store.apply(T("task-ix", "WAITING", ts=1_757_000_001.0, reason="approval"))
            self.assertEqual(len(store.load("task-ix").pending), 1, "the WAITING row is owed; it landed with its ack")
            ip = card.index_path(ws); idx = json.loads(ip.read_text()); e = idx["task-ix"]
            self.assertEqual(e["rows"], 1); e.pop("applied"); e["last_pid"] = "task-ix:1:1"; ip.write_text(json.dumps(idx))  # the old format
            if rotate:
                for i in range(card.LIVE_ROWS + 1):
                    card.append(f"noise {i}", kind="notice", room=None, workspace=ws)
                self.assertNotIn("task-ix", card.log_path(ws).read_text(), "precondition: rotated into the day archive")
            fresh = ActivityStore(ws); fresh.apply(T("task-ix", "COMPLETED", ts=1_757_000_002.0))
            self.assertEqual(len(fresh.load("task-ix").pending), 0)
            sums = [json.loads(l) for l in card.summaries_path(ws).read_text().splitlines()]
            self.assertEqual([x["rows"] for x in sums], [3], f"rotate={rotate}: pickup + waiting + replied, the lost count restored")

    def test_a_fresh_row_never_reads_the_archive(self):
        # Bounded cost: the archive is consulted only on a replay. Fresh bus rows and hook rows pay
        # the live-log read they always paid, and nothing more, however large the day file grows.
        import activity_rows
        calls = {"n": 0}; real = activity_rows._pid_in_archive
        def counting(ws, pid, ts):
            calls["n"] += 1
            return real(ws, pid, ts)
        with unittest.mock.patch.object(activity_rows, "_pid_in_archive", counting):
            store = ActivityStore(self.ws)
            for i in range(200):
                store.apply(T(f"task-p{i}", "RUNNING", ts=1 + i, message_event_id="$m"))
            for i in range(50):
                card.append(f"noise {i}", kind="notice", room=None, task={"id": f"task-n{i}"}, workspace=self.ws, pid=f"n:{i}")
            self.assertEqual(calls["n"], 0, "no fresh row reads the archive")
            with unittest.mock.patch.object(activity_rows, "_ack", lambda w, tid, pid: (_ for _ in ()).throw(OSError("ack disk full"))):
                store.apply(T("task-q1", "RUNNING", ts=400, message_event_id="$m"))  # landed, ack lost, row owed
            for i in range(card.LIVE_ROWS + 1):
                card.append(f"more {i}", kind="notice", room=None, task={"id": f"task-m{i}"}, workspace=self.ws, pid=f"m:{i}")
            self.assertEqual(calls["n"], 0, "rotation traffic does not read the archive either")
            ActivityStore(self.ws).apply(T("task-q1", "COMPLETED", ts=401))  # the replay of the owed row
            self.assertEqual(calls["n"], 1, "exactly the replay consulted the archive")
            rows = [json.loads(l) for p in (self.ws / "state").glob("agent-activity*.jsonl") if "summaries" not in p.name for l in p.read_text().splitlines() if "task-q1" in l]
            self.assertEqual(sorted(r["line"] for r in rows), ["picked up", "replied"], "found in the archive: applied once")

    def test_a_replay_of_a_landed_row_leaves_the_index_count_exact(self):
        # The other half: index saved, then the drained snapshot save lost (a crash) — the same pid
        # projected again must not count the row twice.
        t = {"id": "task-i2"}
        card.append("picked up", kind="processing", room="!r:s", task=t, workspace=self.ws, pid="task-i2:1:1")
        card.append("picked up", kind="processing", room="!r:s", task=t, workspace=self.ws, pid="task-i2:1:1")
        self.assertEqual(self.rows(), ["picked up"])
        self.assertEqual(card.open_task_index(self.ws)["task-i2"]["rows"], 1)
        card.append("replied", kind="done", room="!r:s", task=t, done=True, workspace=self.ws, pid="task-i2:1:2")
        card.append("replied", kind="done", room="!r:s", task=t, done=True, workspace=self.ws, pid="task-i2:1:2")
        self.assertEqual(len(card.summaries_path(self.ws).read_text().splitlines()), 1, "one summary")
        self.assertEqual(self.rows(), ["picked up", "replied"])
class Wiring(unittest.TestCase):
    """The scheduler's emit points reach the bus: the CLI from shell, the manager and the outbox in-process."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "state").mkdir(); (self.ws / "tasks").mkdir()
        (self.ws / "tasks" / "task-w1.txt").write_text(
            "id: task-w1\nchannel_id: !r:s\nuser_id: @q:s\nsender_name: qingyun\ntask: Fix it\nsource_message_id: $m1\nsource: ag2space\n")

    def rows(self):
        return [json.loads(l) for l in card.log_path(self.ws).read_text().splitlines()]

    def test_cli_transitions_from_a_task_file_carry_room_sender_text_and_event(self):
        for to in ("QUEUED", "RUNNING"):
            self.assertEqual(bus.main(["transition", to, "--task-file", str(self.ws / "tasks" / "task-w1.txt"), "--workspace", str(self.ws)]), 0)
        st = ActivityStore(self.ws).load("task-w1")
        self.assertEqual((st.phase, st.room, st.sender, st.text, st.message_event_id), ("RUNNING", "!r:s", "@q:s", "Fix it", "$m1"))
        self.assertEqual([(r["kind"], r["line"], r["task"]["event"]) for r in self.rows()],
                         [("notice", "queued", "$m1"), ("processing", "picked up", "$m1")])

    def test_cli_task_id_and_event_paths_and_a_parse_error_all_return_zero(self):
        self.assertEqual(bus.main(["transition", "RUNNING", "--task-id", "task-c1", "--workspace", str(self.ws)]), 0)
        self.assertEqual(ActivityStore(self.ws).load("task-c1").phase, "RUNNING")
        kind = bus.EVENT_KINDS[0]
        self.assertEqual(bus.main(["event", "task-c1", kind, "--session", "S1", "--seq", "1", "--text", "hi", "--workspace", str(self.ws)]), 0)
        self.assertEqual(ActivityStore(self.ws).load("task-c1").seq, 1)
        with unittest.mock.patch("sys.stderr", new=__import__("io").StringIO()):
            self.assertEqual(bus.main(["transition", "NOT_A_PHASE", "--workspace", str(self.ws)]), 0)

    def test_the_hitl_and_outbox_callers_survive_a_broken_bus(self):
        # The delivery and HITL paths must never fail because the card could not be updated.
        import hitl.manager as manager
        import outbox
        with unittest.mock.patch.object(bus, "ActivityStore", side_effect=RuntimeError("bus down")):
            manager._activity(["task-h1"], "WAITING", "approval")
            outbox._activity_completed("task-o9")
        manager._activity([], "WAITING", "approval")
        outbox._activity_completed("not-a-task")

    def test_cli_never_fails_the_caller(self):
        self.assertEqual(bus.main(["transition", "RUNNING", "--task-file", "/nonexistent/task-x.txt", "--workspace", str(self.ws)]), 0)
        self.assertEqual(bus.main(["transition", "RUNNING", "--workspace", str(self.ws)]), 0)

    def test_a_cancel_instruction_names_its_target(self):
        self.assertEqual(bus.cancel_target("CANCEL_INSTRUCTION: stop processing task-abc12 if still in flight."), "task-abc12")
        self.assertIsNone(bus.cancel_target("please cancel my subscription"))

    def test_a_consolidated_completion_resolves_the_holder_event_from_its_task_file(self):
        (self.ws / "tasks" / "task-h1.txt").write_text("id: task-h1\nchannel_id: !r:s\ntask: holder\nsource_message_id: $holder\n")
        bus.main(["transition", "RUNNING", "--task-file", str(self.ws / "tasks" / "task-w1.txt"), "--workspace", str(self.ws)])
        bus.main(["transition", "COMPLETED", "--task-file", str(self.ws / "tasks" / "task-w1.txt"), "--into-task", "task-h1", "--workspace", str(self.ws)])
        self.assertEqual((self.rows()[-1]["line"], self.rows()[-1]["task"]["into"]), ("consolidated", "$holder"))

    def test_a_queued_that_lands_after_its_running_is_history_not_a_regression(self):
        # The emitter's QUEUED and RUNNING are independent processes: RUNNING (stamped later) can take
        # the lock first. The earlier-stamped QUEUED still writes its row and never regresses the phase.
        store = ActivityStore(self.ws)
        st = store.apply(T("task-w1", "RUNNING", ts=10.0, message_event_id="$m1", room="!r:s"))
        st = store.apply(T("task-w1", "QUEUED", ts=9.0))
        self.assertEqual((st.phase, st.generation), ("RUNNING", 1))
        self.assertEqual([(r["kind"], r["line"]) for r in self.rows()], [("processing", "picked up"), ("notice", "queued")])
        st = store.apply(T("task-w1", "QUEUED", ts=9.0))  # replay: once
        self.assertEqual(len(self.rows()), 2)
        st = store.apply(T("task-w1", "QUEUED", ts=11.0))  # a later QUEUED is a replay by key: no row, no regression
        self.assertEqual((st.phase, len(self.rows())), ("RUNNING", 2))

    def test_the_hitl_manager_stays_silent_for_a_policy_answered_requirement(self):
        import hitl.manager as hm
        calls = []
        class FakeStore:
            def apply(self, item): calls.append((item.task_id, item.to_phase))
        class Req:
            blocked_task_ids = ["task-w1"]; kind = "permission"; status = "in_progress"; decided_by = hm.POLICY_DECIDER
        with unittest.mock.patch.object(bus, "ActivityStore", lambda *a, **k: FakeStore()):
            if Req.decided_by != hm.POLICY_DECIDER and Req.status not in hm.TERMINAL_STATUSES:
                hm._activity(Req.blocked_task_ids, "WAITING", Req.kind)
        self.assertEqual(calls, [], "policy answered it: nobody is waiting")
        src = open(os.path.join(_SRC, "hitl", "manager.py")).read()
        self.assertIn("if req.decided_by != POLICY_DECIDER and req.status not in TERMINAL_STATUSES:", src)

    def test_the_emitter_and_the_watcher_route_through_the_bus(self):
        emit = open(os.path.join(_SRC, "task-emit.sh")).read()
        watcher = open(os.path.join(_SRC, "watch-tasks-stream.sh")).read()
        for phase in ("QUEUED", "RUNNING", "CANCELLED"):
            self.assertIn(phase, emit, f"task-emit.sh does not transition {phase}")
        self.assertIn("activity_bus.py", emit)
        self.assertIn("transition FAILED", watcher)
        self.assertNotIn("( python3 ", emit + watcher, "the watcher's resolved interpreter, never PATH's python3")
        self.assertIn("--ts", emit, "each transition is stamped so ordering survives independent processes")
        self.assertNotIn("activity.py", emit, "the emitter must not write rows around the bus")

    def test_the_hitl_manager_marks_waiting_and_running(self):
        import hitl.manager as hm
        calls = []
        class FakeStore:
            def apply(self, item): calls.append((item.task_id, item.to_phase, item.reason))
        with unittest.mock.patch.object(bus, "ActivityStore", lambda *a, **k: FakeStore()):
            hm._activity(["task-w1"], "WAITING", "selection")
            hm._activity(["task-w1"], "RUNNING", "resolved")
            hm._activity([], "WAITING", "none")
        self.assertEqual(calls, [("task-w1", "WAITING", "selection"), ("task-w1", "RUNNING", "resolved")])

    def test_the_outbox_marks_completed_for_task_results_only(self):
        import outbox
        calls = []
        class FakeStore:
            def apply(self, item): calls.append((item.task_id, item.to_phase))
        with unittest.mock.patch.object(bus, "ActivityStore", lambda *a, **k: FakeStore()):
            outbox._activity_completed("task-w1")
            outbox._activity_completed("proactive-123")
        self.assertEqual(calls, [("task-w1", "COMPLETED")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
