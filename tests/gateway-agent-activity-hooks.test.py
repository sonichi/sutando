#!/usr/bin/env python3
"""The bridge's task-lifecycle hooks, in process (REMOTE_TASK_LIFECYCLE_V2 + REMOTE_TASK_TURN_CONTEXT).

The real bridge module, imported hermetically with both flags on, against a fake `_req`:
the ack paths, the per-task beat riding the client heartbeat, a cancel reaching the core,
the ledger reconcile, the startup turn sweep, the result gate, and the control-task sweep.

Run: python3 tests/gateway-agent-activity-hooks.test.py
"""
from __future__ import annotations

import ast
import io
import json
import contextlib
import os
import sys
import textwrap
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gw, _READS = isolate_then_import(env={"REMOTE_TASK_LIFECYCLE_V2": "1", "REMOTE_TASK_TURN_CONTEXT": "1"})
assert gw._ATTEMPTS.take_ownership()
agent_turn = gw.task_attempts.agent_turn


def _http(code, body):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


class FakeGateway:
    def __init__(self, **answers):
        self.calls, self.answers, self.ops = [], answers, []

    def __call__(self, method, path, payload=None, timeout=None, **k):
        gw.task_attempts.assert_unlocked(f"{method} {path}")      # no provider call inside a transition
        self.calls.append((method, path, payload))
        self.ops.append(k.get("operation_id"))
        ans = self.answers.get(path.split("?")[0])
        if isinstance(ans, BaseException):
            raise ans
        return ans(payload) if callable(ans) else (ans if ans is not None else {})

    def posted(self, suffix):
        return [p for _m, path, p in self.calls if path.endswith(suffix)]


def _task(tid, **extra):
    return {"id": tid, "task": "reply", "source": "ag2space", "user_id": "@o:x", "access_tier": "owner",
            **extra}


def seed_ack(tid, response, open_turn=True):
    """An ack recorded as the bridge records one: claimed, then committed in the task's transition."""
    with gw._ATTEMPTS.transition(tid) as t:
        serve = t.rec.get("lease_id") if isinstance(t.rec, dict) else None
        rec = t.record_ack(t.claim("ack", serve=serve), response, open_turn=open_turn)
        assert isinstance(rec, dict), rec
        return rec


def seed(tid, **fields):
    """Fields written into tid's record through its transition."""
    with gw._ATTEMPTS.transition(tid) as t:
        assert t.put(**fields)


def txn(tid):
    return gw._ATTEMPTS.transition(tid)


def remember(tid, task, lease=None):
    with txn(tid) as t:
        return t.remember_task(task, lease=lease)


def wakes_seen(tid) -> list:
    return (ledger().get("wakes") or {}).get(tid, [])


def settling(tid, episode="e1") -> bool:
    """Whether a wake of `tid` would wait for an earlier run still settling here."""
    with txn(tid) as t:
        return gw._earlier_run_settling(t, tid, episode)


def ledger() -> dict:
    return json.loads(gw._ATTEMPTS.path.read_text())


def write_ledger(data: dict) -> None:
    gw._ATTEMPTS.path.write_text(json.dumps(data))


def failing_commit(*whys):
    """Every ledger write whose purpose names one of `whys` fails; the rest commit."""
    real = gw._ATTEMPTS._commit

    def commit(apply, owner=False, why=""):
        return (False, None) if any(w in why for w in whys) else real(apply, owner=owner, why=why)
    return mock.patch.object(gw._ATTEMPTS, "_commit", side_effect=commit)


def _ack(tid, gen=1):
    return {"ok": True, "durable": True, "task_id": tid, "execution_generation": gen, "attempt": gen,
            "lease_expires_at": 1761354125.0}


class Hooks(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.addCleanup(self.ledger_left_intact)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(gw, "_log", self.logs.append).start()

    def ledger_left_intact(self):
        """Every test leaves the shared ledger readable and this process its owner."""
        if gw._ATTEMPTS._read() is None or gw._ATTEMPTS.revoked:
            if gw._ATTEMPTS.path.is_dir():
                gw._ATTEMPTS.path.rmdir()
            gw._ATTEMPTS.path.unlink(missing_ok=True)
            gw._ATTEMPTS.revoked = False
            gw._ATTEMPTS.take_ownership()
            self.fail("the test left the attempt ledger unreadable or this process revoked")

    def use(self, fake):
        mock.patch.object(gw, "_req", fake).start()
        return fake

    def controls(self, tid):
        return [p for p in gw.TASKS_DIR.glob("task-ctl-*.txt") if f"stop processing {tid} " in p.read_text()]

    def test_the_ack_carries_the_lease_holder_and_records_the_generation(self):
        self.assertIsNotNone(gw._write_task(_task("task-h1")))
        fake = self.use(FakeGateway(**{"/v1/tasks/task-h1/ack": _ack("task-h1", 4)}))
        self.assertTrue(gw._post_task_ack("task-h1", durable=True))
        body = fake.posted("/ack")[0]
        self.assertEqual((body["protocol_version"], body["worker_id"]), (2, gw._ATTEMPTS.worker_id()))
        rec = gw._ATTEMPTS.get("task-h1")
        self.assertEqual(rec["execution_generation"], 4)
        self.assertEqual(agent_turn.open_turn_for_task(gw._STATE, "task-h1").stem, rec["turn_id"])

    def test_a_cancelled_ack_withdraws_the_untaken_task_and_is_acknowledged(self):
        cancel = _http(409, {"error_code": "task_cancelled", "cancel_requested_generation": 2,
                             "cancel_requested_attempt": 2})
        fake = self.use(FakeGateway(**{"/v1/tasks/task-h2/ack": cancel}))
        self.assertTrue(gw._take_served_task(_task("task-h2"), set(), []))
        self.assertFalse(gw._post_task_ack("task-h2", durable=True))
        self.assertIsNone(agent_turn.open_turn_for_task(gw._STATE, "task-h2"), "no turn")
        self.assertFalse((gw.TASKS_DIR / "task-h2.txt").exists(), "the published task is not started")
        self.assertEqual(self.controls("task-h2"), [])
        self.assertEqual(fake.posted("/cancel-ack")[0]["cancel_requested_generation"], 2)

    def test_a_cancelled_ack_stops_a_task_the_core_has_taken_up(self):
        fake = self.use(FakeGateway(**{"/v1/tasks/task-h2t/ack": _http(409, {"error_code": "task_cancelled"}),
                                       "/v1/results": {"ok": True}}))
        self.assertTrue(gw._take_served_task(_task("task-h2t"), set(), []))
        with open(gw._STATE / "agent-activity.jsonl", "a") as fh:
            fh.write(json.dumps({"ts": time.time() + 5, "kind": "processing", "task": {"id": "task-h2t"}}) + "\n")
        self.assertFalse(gw._post_task_ack("task-h2t", durable=True))
        self.assertTrue((gw.TASKS_DIR / "task-h2t.txt").exists())
        self.assertEqual(len(self.controls("task-h2t")), 1, "the core is asked to stop")
        self.assertTrue(gw._deliver_result_payload("task-h2t", "task-h2t", "late"))
        self.assertEqual(fake.posted("/v1/results"), [], "its result is settled, never posted")

    def test_a_broker_without_per_task_beats_is_left_alone(self):
        seed_ack("task-nobeat", _ack("task-nobeat", 1))
        fake = self.use(FakeGateway(**{"/v1/tasks/heartbeat": _http(404, {})}))
        self.addCleanup(setattr, gw._ATTEMPTS, "task_beat_disabled", False)
        self.assertEqual(gw._send_task_beats({"task-nobeat"}), [])
        self.assertEqual(gw._send_task_beats({"task-nobeat"}), [])
        self.assertEqual(len(fake.posted("/v1/tasks/heartbeat")), 1, "not asked again")

    def test_a_beat_that_fails_or_answers_no_fence_moves_nothing(self):
        seed_ack("task-beat-errors", _ack("task-beat-errors", 1))
        for answer in (_http(500, {}), OSError("down"),
                       {"task_errors": [{"task_id": "task-beat-errors", "error_code": "other"}, "x"]}):
            self.use(FakeGateway(**{"/v1/tasks/heartbeat": answer}))
            self.assertEqual(gw._send_task_beats({"task-beat-errors"}), [])

    def test_a_cancel_ack_is_retried_under_its_operation_id_and_a_settled_answer_ends_it(self):
        tid = "task-cancel-ack-retry"
        seed_ack(tid, _ack(tid, 2))
        with txn(tid) as t:
            t.fence({"error_code": "task_cancelled", "cancel_requested_generation": 2, "cancel_requested_attempt": 2})
            gw._want_cancel_ack(t)
        keys = []
        real = FakeGateway.__call__

        def keyed(fake, method, path, payload=None, timeout=None, **k):
            keys.append(k.get("operation_id"))
            return real(fake, method, path, payload, timeout, **k)
        mock.patch.object(FakeGateway, "__call__", keyed).start()
        for answer in (_http(503, {"error": "x"}), OSError("down")):
            self.use(FakeGateway(**{f"/v1/tasks/{tid}/cancel-ack": answer}))
            gw._drive_cancel_acks(tid)
            self.assertNotEqual(gw._ATTEMPTS.get(tid)["fenced"], "cancel_confirmed")
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/cancel-ack": _http(409, {"error_code": "no_cancel_pending"})}))
        gw._drive_cancel_acks(tid)
        self.assertEqual(gw._ATTEMPTS.get(tid)["fenced"], "cancel_confirmed")
        self.assertEqual(fake.posted("/cancel-ack")[0], {"worker_id": gw._ATTEMPTS.worker_id(),
                                                          "cancel_requested_generation": 2,
                                                          "cancel_requested_attempt": 2})
        self.assertEqual(len(set(keys)), 1, "every send of one cancel-ack carries the same operation id")
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("claims"))

    def test_the_server_listing_is_one_complete_page_or_nothing(self):
        fake = self.use(FakeGateway(**{"/v1/tasks/active": {"tasks": [{"task_id": "a"}, {"x": 1}], "next_cursor": None}}))
        self.assertEqual(gw._active_tasks(), [{"task_id": "a"}])
        self.assertIn("limit=200", fake.calls[-1][1])
        for answer in ({"tasks": [], "next_cursor": "c1"}, {"tasks": "x"}, _http(503, {"error_code": "x"}),
                       _http(404, {}), OSError("down")):
            self.use(FakeGateway(**{"/v1/tasks/active": answer}))
            self.assertIsNone(gw._active_tasks())
        self.assertTrue(any("more than 200 outstanding" in m for m in self.logs))

    def test_the_ack_first_path_records_the_generation_or_refuses_a_cancel(self):
        self.use(FakeGateway(**{"/v1/tasks/task-h3/ack": _ack("task-h3", 2)}))
        self.assertIsNotNone(gw._write_task(_task("task-h3"), hold=True))     # staged before its ack
        self.assertEqual(gw._post_ack_first("task-h3", None), "accepted")
        self.assertEqual(gw._ATTEMPTS.get("task-h3")["execution_generation"], 2)
        self.assertIsNotNone(gw._write_task(_task("task-h3b"), hold=True))
        fake = self.use(FakeGateway(**{"/v1/tasks/task-h3b/ack": _http(409, {"error_code": "task_cancelled"})}))
        self.assertEqual(gw._post_ack_first("task-h3b", None), "stale")
        self.assertEqual(self.controls("task-h3b"), [], "a staged task never reached the core")
        self.assertEqual(len(fake.posted("/cancel-ack")), 1)

    def test_the_client_heartbeat_carries_a_per_task_beat_that_can_fence(self):
        seed_ack("task-h4", _ack("task-h4", 1))
        fake = self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True, "task_errors": [
            {"task_id": "task-h4", "error_code": "stale_execution_generation"},
            {"task_id": "task-elsewhere", "error_code": "task_cancelled"}]}}))
        gw._post_heartbeat({"task-h4"}, force=True)
        beat = fake.posted("/v1/tasks/heartbeat")[0]
        self.assertEqual(beat["tasks"][0]["task_id"], "task-h4")
        self.assertEqual(len(self.controls("task-h4")), 1)
        self.assertEqual(self.controls("task-elsewhere"), [], "only this node's attempts are fenced")

    # ── a run's generation is fixed once it can execute (round 14, adoption ruling) ──
    def served_acked(self, tid, gen=1):
        """Served under L1 and acked as `gen`; its task file queued. The broker double for the task."""
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1", source_event_id="$e"), set(), []))
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, gen), "/v1/results": {"ok": True},
                                       "/v1/tasks/heartbeat": {"ok": True}}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        return fake

    def reserved(self, tid, inflight):
        """Served again under L2: held, nothing published, the run unchanged."""
        before = sorted(p.name for p in gw.TASKS_DIR.glob(f"{tid}*"))
        self.assertEqual(gw._write_task(_task(tid, lease_id="L2", source_event_id="$e")), (tid, True))
        self.assertEqual(sorted(p.name for p in gw.TASKS_DIR.glob(f"{tid}*")), before, "nothing is published")
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["run"], rec["lease_id"], rec["pending_serve"]["lease"]), (tid, "L1", "L2"))
        self.assertIn(tid, inflight)

    def test_a_lost_ack_re_served_under_its_own_generation_keeps_one_run(self):
        tid = "task-lost-ack-same-gen"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1", source_event_id="$e"), set(), []))
        self.assertTrue(gw._record_pending_acks([(tid, True, "L1")]))
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": TimeoutError("lost"), "/v1/results": {"ok": True}}))
        self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L1"))          # its ack is lost
        inflight = {tid}
        self.reserved(tid, inflight)
        with txn(tid) as t:
            self.assertIsNotNone(t.result_hold(), "nothing is posted while the generation is unknown")
        fake.answers[f"/v1/tasks/{tid}/ack"] = lambda p: _ack(tid, 1)
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["run"], rec["lease_id"], rec["execution_generation"], rec.get("pending_serve")),
                         (tid, "L2", 1, None), "the same run: the lease moved onto it")
        self.assertEqual(sorted(p.name for p in gw.TASKS_DIR.glob(f"{tid}*")), [f"{tid}.txt"], "one run, never re-run")
        self.assertFalse(gw._reserve_path(tid).exists())
        posted = len(fake.posted("/ack"))
        gw._retry_pending_acks(inflight)
        self.assertEqual(len(fake.posted("/ack")), posted, "L1's ack, superseded, is not sent")
        self.ran(tid)
        gw._post_ready_results(inflight)
        self.assertEqual([p["execution_generation"] for p in fake.posted("/v1/results")], [1])

    def test_a_newer_generation_gets_its_own_run_whatever_the_earlier_run_was_doing(self):
        for state in ("queued", "claimed", "running", "answered"):
            with self.subTest(state=state):
                tid = f"task-g1-g2-{state}"
                fake = self.served_acked(tid)
                inflight = {tid}
                if state == "claimed":
                    (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.claimed-core1")
                    self.addCleanup(lambda t=tid: (gw.TASKS_DIR / f"{t}.claimed-core1").unlink(missing_ok=True))
                elif state == "running":
                    with open(gw._STATE / "agent-activity.jsonl", "a") as fh:
                        fh.write(json.dumps({"ts": time.time() + 5, "kind": "working", "task": {"id": tid}}) + "\n")
                elif state == "answered":
                    self.core_writes(tid, "generation 1's answer")
                self.reserved(tid, inflight)
                fake.answers[f"/v1/tasks/{tid}/ack"] = lambda p, t=tid: _ack(t, 2)
                self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
                run = f"{tid}~~2"
                rec = gw._ATTEMPTS.get(tid)
                self.assertEqual((rec["run"], rec["execution_generation"], rec["lease_id"]), (run, 2, "L2"))
                stops = self.all_controls(tid)
                if state == "queued":
                    self.assertFalse((gw.TASKS_DIR / f"{tid}.txt").exists(), "the untaken run is withdrawn")
                    self.assertEqual(stops, [])
                elif state in ("claimed", "running"):
                    self.assertEqual(len(stops), 1, "the core is asked to stop the earlier run")
                else:
                    self.assertEqual(stops, [], "an answered run has nothing to stop")
                self.assertEqual(self.all_controls(run), [], "nothing ever stops the newer run")
                gw._post_task_beats(inflight, force=True)           # the newer run's publication
                self.assertIn(run, inflight)
                self.assertIn(f"id: {run}", (gw.TASKS_DIR / f"{run}.txt").read_text())
                self.assertIsNone(gw._ATTEMPTS.get(tid).get("owed_publish"))
                gw._post_ready_results(inflight)
                self.assertEqual([p for p in fake.posted("/v1/results") if p["id"] == tid], [],
                                 "the earlier run's answer is never posted, under either generation")
                if state == "answered":
                    self.assertEqual(len(list((gw.RESULTS_DIR / "undelivered").glob(f"{tid}.*"))), 1, "quarantined")
                    self.assertNotIn(tid, inflight)
                self.core_writes(run, "generation 2's answer")
                old = time.time() - 5
                os.utime(gw.RESULTS_DIR / f"{run}.txt", (old, old))
                gw._post_ready_results(inflight)
                posts = [(p["execution_generation"], p["body"]) for p in fake.posted("/v1/results") if p["id"] == tid]
                self.assertEqual(posts, [(2, "generation 2's answer")])
                self.assertEqual(gw._ATTEMPTS.get(tid), {})

    def test_an_earlier_runs_effect_of_unknown_outcome_is_settled_before_the_newer_run(self):
        tid = "task-g1-unknown"
        fake = self.served_acked(tid)
        inflight = {tid}
        self.core_writes(tid, "generation 1's answer")
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
        fake.answers["/v1/results"] = _http(503, {"error": "busy"})
        gw._post_ready_results(inflight)                  # published, not delivered: its claim stays open
        self.assertEqual([c["kind"] for c in gw._ATTEMPTS.get(tid)["claims"].values()], ["result"])
        self.reserved(tid, inflight)
        root = gw._delivery_core().backend.root
        outbox = sys.modules[gw.read_item.__module__]
        self.assertTrue(outbox.acquire_delivery_claim(root, tid, "another-worker"))   # it may be sending now
        fake.answers[f"/v1/tasks/{tid}/ack"] = lambda p: _ack(tid, 2)
        self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L2"), "the newer run waits")
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["run"], rec["execution_generation"]), (tid, 1))
        self.assertIsNotNone(gw.read_item(root, tid))
        outbox.release_delivery_claim(root, tid, "another-worker")
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        self.assertIsNone(gw.read_item(root, tid), "the unsent answer is taken back under its own claim")
        self.assertEqual(gw._ATTEMPTS.get(tid)["run"], f"{tid}~~2")
        fake.answers["/v1/results"] = {"ok": True}
        mock.patch.object(gw.time, "time", return_value=time.time() + 3600).start()     # past any retry wait
        gw._post_task_beats(inflight, force=True)
        gw._post_ready_results(inflight)
        self.assertEqual([p for p in fake.posted("/v1/results") if p.get("execution_generation") == 1 and
                          p["id"] == tid and fake.posted("/v1/results").index(p) > 0], [],
                         "generation 1's answer is never sent again")

    def test_two_cancels_in_one_millisecond_both_reach_the_core(self):
        with mock.patch.object(gw.time, "time", return_value=1761500000.0):
            gw._queue_cancel_instruction("task-h5a", "task_cancelled")
            gw._queue_cancel_instruction("task-h5b", "task_cancelled")
        self.assertEqual((len(self.controls("task-h5a")), len(self.controls("task-h5b"))), (1, 1))

    def test_a_result_written_before_the_ack_waits_for_the_lease(self):
        self.assertTrue(gw._take_served_task(_task("task-race"), set(), []))
        fake = self.use(FakeGateway(**{"/v1/tasks/task-race/ack": _ack("task-race", 3),
                                       "/v1/results": {"ok": True}}))
        self.assertFalse(gw._deliver_result_payload("task-race", "task-race", "fast answer"))
        self.assertEqual(fake.posted("/v1/results"), [], "nothing goes before the ack names the lease")
        self.assertTrue(gw._post_task_ack("task-race", durable=True))
        turn = agent_turn.open_turn_for_task(gw._STATE, "task-race").stem
        self.assertTrue(gw._deliver_result_payload("task-race", "task-race", "fast answer"))
        sent = fake.posted("/v1/results")[0]
        self.assertEqual((sent["execution_generation"], sent["status"]), (3, "completed"))
        self.assertTrue(agent_turn.is_closed(gw._STATE, turn))

    def test_room_actions_before_the_ack_still_gate_the_result(self):
        self.assertTrue(gw._take_served_task(_task("task-early"), set(), []))
        with open(agent_turn.room_actions_path(gw._STATE), "a") as fh:
            fh.write(json.dumps({"record_type": "dispatch_intent", "turn_id": None, "task_id": "task-early",
                                 "ts": time.time(), "action": "room.message.send",
                                 "body_sha256": agent_turn.body_sha256("the answer")}) + "\n")
        fake = self.use(FakeGateway(**{"/v1/tasks/task-early/ack": _ack("task-early", 1),
                                       "/v1/results": {"ok": True}}))
        self.assertTrue(gw._post_task_ack("task-early", durable=True))
        self.assertTrue(gw._deliver_result_payload("task-early", "task-early", "the answer"))
        self.assertEqual(fake.posted("/v1/results")[0].get("no_send"), True, "the early send is not repeated")

    def early(self, tid, kind):
        """A room action the proxy journalled unbound, naming the task, before any ack."""
        rec = {"turn_id": None, "task_id": tid, "ts": time.time(), "seq": 1, "operation_id": f"op_{tid}"}
        if kind == "send":
            rec.update(record_type="dispatch_intent", action="room.message.send",
                       body_sha256=agent_turn.body_sha256("answer"))
        elif kind == "terminal":
            rec.update(record_type="dispatch_outcome", operation_status="succeeded",
                       applied_execution_phase="responded", applied_terminal=True)
        else:
            rec.update(record_type="dispatch_outcome", operation_status="succeeded",
                       applied_execution_phase="waiting_input", applied_terminal=False,
                       active_blocker_count=1)
        with open(agent_turn.room_actions_path(gw._STATE), "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def test_on_a_broker_that_names_leases_an_unanswered_ack_never_releases_the_result(self):
        seed_ack("task-v2seen", _ack("task-v2seen", 1))
        self.assertTrue(gw._ATTEMPTS.broker_names_leases())
        self.assertTrue(gw._take_served_task(_task("task-cap"), set(), []))
        self.early("task-cap", "send")
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
        later = time.time() + gw.task_attempts.ACK_HOLD_MAX_S + 1
        with mock.patch.object(gw._ATTEMPTS, "now", lambda: later):
            self.assertFalse(gw._deliver_result_payload("task-cap", "task-cap", "answer"))
            self.assertFalse(gw._deliver_result_payload("task-cap", "task-cap", "answer"))
        self.assertEqual(fake.posted("/v1/results"), [], "never posted ungated")
        self.assertEqual(sum("waits for the ack or a fence" in m for m in self.logs), 1, "said once")
        with txn("task-cap") as t:
            t.fence({"error_code": "task_already_terminal"})
        self.assertTrue(gw._deliver_result_payload("task-cap", "task-cap", "answer"), "a fence ends the wait")
        self.assertEqual(fake.posted("/v1/results"), [])

    def test_a_held_result_is_settled_only_on_an_explicit_terminal_answer(self):
        seed_ack("task-v2seen2", _ack("task-v2seen2", 1))
        tid = "task-held-closed"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        later = time.time() + 10_000
        fake = self.use(FakeGateway(**{"/v1/tasks/active": {"tasks": [], "next_cursor": None},
                                       "/v1/results": {"ok": True}}))
        mock.patch.object(gw._ATTEMPTS, "now", lambda: later).start()
        self.assertFalse(gw._deliver_result_payload(tid, tid, "answer"), "held: its ack never answered")
        mock.patch.object(gw, "_last_ledger_reconcile", 0.0).start()
        gw._reconcile_ledger({tid})
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("fenced"), "absent from the outstanding list is not terminal")
        self.assertTrue((gw.TASKS_DIR / f"{tid}.txt").exists(), "a reply the core may be writing is kept")
        self.assertFalse(gw._deliver_result_payload(tid, tid, "answer"), "still held")
        self.assertTrue(any(f"{tid} is not listed as outstanding" in m for m in self.logs))
        fake.answers[f"/v1/tasks/{tid}/ack"] = _http(409, {"error_code": "task_already_terminal"})
        self.assertFalse(gw._post_task_ack(tid, durable=True))
        self.assertEqual(gw._ATTEMPTS.get(tid)["fenced"], "task_already_terminal", "the broker said so")
        self.assertFalse((gw.TASKS_DIR / f"{tid}.txt").exists(), "its untaken file is withdrawn")
        self.assertEqual(self.controls(tid), [], "nothing for the core to stop")
        self.assertTrue(gw._deliver_result_payload(tid, tid, "answer"), "settled")
        self.assertEqual(fake.posted("/v1/results"), [], "never sent ungated")

    def test_on_a_broker_never_seen_to_name_leases_the_fallback_is_still_gated(self):
        cases = {"none": {}, "send": {"status": "completed", "no_send": True},
                 "terminal": None, "blocker": {"status": "waiting_input", "no_send": True}}
        with mock.patch.object(gw._ATTEMPTS, "broker_names_leases", lambda: False):
            for kind, want in cases.items():
                with self.subTest(kind=kind):
                    tid = f"task-legacy-{kind}"
                    self.assertTrue(gw._take_served_task(_task(tid), set(), []))
                    if kind != "none":
                        self.early(tid, kind)
                    fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
                    self.assertFalse(gw._deliver_result_payload(tid, tid, "answer"), "waits first")
                    later = time.time() + gw.task_attempts.ACK_HOLD_MAX_S + 1
                    with mock.patch.object(gw._ATTEMPTS, "now", lambda: later):
                        self.assertTrue(gw._deliver_result_payload(tid, tid, "answer"))
                    posted = fake.posted("/v1/results")
                    if want is None:
                        self.assertEqual(posted, [], "closed through MCP: settled, not posted")
                        continue
                    sent = posted[0]
                    self.assertNotIn("execution_generation", sent)
                    self.assertEqual({k: sent[k] for k in want}, want)
                    if kind == "none":
                        self.assertNotIn("status", sent, "no journal record: today's post")

    def test_a_redelivery_before_the_ack_keeps_the_first_publication_boundary(self):
        for kind, want in (("send", {"status": "completed", "no_send": True}), ("terminal", None),
                           ("blocker", {"status": "waiting_input", "no_send": True})):
            with self.subTest(kind=kind):
                tid = f"task-redeliver-{kind}"
                self.assertTrue(gw._take_served_task(_task(tid), set(), []))
                first = gw._ATTEMPTS.get(tid)["published_at"]
                self.early(tid, kind)
                with mock.patch.object(gw._ATTEMPTS, "now", lambda: first + 5):
                    self.assertEqual(gw._write_task(_task(tid)), (tid, True), "served again before the ack")
                self.assertEqual(gw._ATTEMPTS.get(tid)["published_at"], first, "the boundary does not move")
                fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1), "/v1/results": {"ok": True}}))
                with mock.patch.object(agent_turn.time, "time", lambda: first + 10):
                    self.assertTrue(gw._post_task_ack(tid, durable=True))
                self.assertTrue(gw._deliver_result_payload(tid, tid, "answer"))
                posted = fake.posted("/v1/results")
                if want is None:
                    self.assertEqual(posted, [])
                else:
                    self.assertEqual({k: posted[0][k] for k in want}, want)

    def test_a_late_ack_for_a_finished_task_opens_no_turn(self):
        remember("task-late", _task("task-late"))
        self.use(FakeGateway(**{"/v1/tasks/task-late/ack": _ack("task-late", 1)}))
        self.assertTrue(gw._post_task_ack("task-late", durable=True))
        self.assertIsNone(agent_turn.open_turn_for_task(gw._STATE, "task-late"))
        self.assertEqual(gw._ATTEMPTS.get("task-late")["execution_generation"], 1)

    def test_every_fencing_ack_answer_fences_retires_and_stops_or_withdraws(self):
        cases = [(409, "stale_execution_generation"), (404, "stale_execution_generation"),
                 (409, "task_not_found"), (409, "task_already_terminal")]
        for status, code in cases:
            with self.subTest(status=status, code=code):
                tid = f"task-fence-{status}-{code}"
                inflight, pending = set(), []
                self.assertTrue(gw._take_served_task(_task(tid), inflight, pending))
                self.assertTrue(gw._record_pending_acks(pending))
                fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(status, {
                    "error": "not leased to you", "reason": "stale lease generation", "error_code": code}),
                    "/v1/results": {"ok": True}}))
                self.assertFalse(gw._post_task_ack(tid, durable=True))
                self.assertEqual(gw._ATTEMPTS.get(tid)["fenced"], code)
                self.assertNotIn(tid, gw._load_pending_acks(), "its pending ack is retired")
                closed = code in gw.CLOSED_CODES
                self.assertEqual(len(self.controls(tid)), 0 if closed else 1, "stopped, or withdrawn untaken")
                self.assertEqual((gw.TASKS_DIR / f"{tid}.txt").exists(), not closed)
                self.assertTrue(gw._deliver_result_payload(tid, tid, "late"))
                self.assertEqual(fake.posted("/v1/results"), [], "its result is settled, never posted")

    def closed_by_beat(self, tid, **err):
        fake = self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True, "task_errors": [
            {"task_id": tid, "error_code": "task_already_terminal", **err}]}, "/v1/results": {"ok": True}}))
        gw._post_heartbeat({tid}, force=True)
        return fake

    def test_a_closed_task_the_core_has_not_taken_is_withdrawn_from_the_inbox(self):
        tid = "task-closed-untaken"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        seed_ack(tid, _ack(tid, 1))
        fake = self.closed_by_beat(tid, reason="question_resolved")
        self.assertFalse((gw.TASKS_DIR / f"{tid}.txt").exists(), "the core never runs it")
        self.assertEqual(self.controls(tid), [])
        self.assertTrue(any("question_resolved" in m and "withdrawn" in m for m in self.logs))
        self.assertTrue(gw._deliver_result_payload(tid, tid, "late"))
        self.assertEqual(fake.posted("/v1/results"), [], "nothing reaches the room")

    def test_a_closed_task_the_core_has_taken_up_is_stopped_not_withdrawn(self):
        tid = "task-closed-taken"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        seed_ack(tid, _ack(tid, 1))
        with open(gw._STATE / "agent-activity.jsonl", "a") as fh:
            fh.write(json.dumps({"ts": time.time() + 5, "kind": "processing", "task": {"id": tid}}) + "\n")
        fake = self.closed_by_beat(tid, reason="max_queued_age_exceeded")
        self.assertTrue((gw.TASKS_DIR / f"{tid}.txt").exists(), "a file the core is on stays")
        self.assertEqual(len(self.controls(tid)), 1, "the core is asked to stop")
        self.closed_by_beat(tid)
        self.assertEqual(len(self.controls(tid)), 1, "asked once")
        self.assertTrue(gw._deliver_result_payload(tid, tid, "late"))
        self.assertEqual(fake.posted("/v1/results"), [])

    def test_a_closed_task_the_core_claimed_first_is_stopped(self):
        tid = "task-closed-claimed"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        seed_ack(tid, _ack(tid, 1))
        queued = gw.TASKS_DIR / f"{tid}.txt"
        claimed = queued.with_name(f"{tid}.claimed-core")
        queued.rename(claimed)
        self.addCleanup(lambda: claimed.unlink(missing_ok=True))
        self.closed_by_beat(tid)
        self.assertTrue(claimed.exists())
        self.assertEqual(len(self.controls(tid)), 1)

    def test_a_closed_task_already_answered_is_left_to_its_settled_result(self):
        tid = "task-closed-answered"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        seed_ack(tid, _ack(tid, 1))
        result = gw.RESULTS_DIR / f"{tid}.txt"
        result.parent.mkdir(parents=True, exist_ok=True)
        result.write_text("answer")
        self.addCleanup(lambda: result.unlink(missing_ok=True))
        self.closed_by_beat(tid)
        self.assertEqual(self.controls(tid), [])

    def test_a_stale_older_lease_with_a_newer_serve_here_is_left_to_the_newer_ack(self):
        tid = "task-reserved"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L2"), set(), []))
        self.assertTrue(gw._record_pending_acks([(tid, True, "L2")]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(404, {"error_code": "stale_execution_generation"})}))
        self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("fenced"))
        self.assertEqual(self.controls(tid), [])
        self.assertIn(tid, gw._load_pending_acks(), "the newer serve's ack is still due")

    def test_a_fenced_ack_first_task_is_refused_without_reaching_the_core(self):
        tid = "task-af-stale"
        remember(tid, _task(tid))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(404, {"error_code": "stale_execution_generation"})}))
        self.assertEqual(gw._post_ack_first(tid, None), "stale")
        self.assertEqual(gw._ATTEMPTS.get(tid)["fenced"], "stale_execution_generation")
        self.assertEqual(self.controls(tid), [])

    def test_with_the_lifecycle_off_a_wake_is_an_ordinary_task(self):
        wake = {"blocker_episode_id": "ep1", "resolved_blockers": []}
        with mock.patch.object(gw, "LIFECYCLE_V2", False):
            self.assertIsNotNone(gw._write_task(_task("task-plain-wake", wake=wake)))
        self.assertTrue((gw.TASKS_DIR / "task-plain-wake.txt").exists(), "written and acked as on main")
        self.assertFalse(any("task-plain-wake: not run here" in m for m in self.logs))

    def test_a_wake_seen_first_here_runs_with_its_answer_or_not_at_all(self):
        wake = {"blocker_episode_id": "ep1", "resolved_blockers": [
            {"outcome": "answered", "answer": {"kind": "text", "text": "blue"}}]}
        with mock.patch.object(gw, "TURN_CONTEXT", False):
            self.assertIsNone(gw._write_task(_task("task-fresh-off", wake=wake)))
        self.assertFalse((gw.TASKS_DIR / "task-fresh-off.txt").exists(), "never run without its answer")
        self.assertIsNone(gw._write_task(_task("task-fresh-af", ack_first=True, wake=wake), hold=True))
        self.assertIsNone(gw._write_task(_task("task-fresh-bad", wake={"resolved_blockers": []})))
        self.assertEqual(gw._write_task(_task("task-fresh-wake", wake=wake)), ("task-fresh-wake", True))
        self.assertIn('answered "blue"', (gw.TASKS_DIR / "task-fresh-wake.txt").read_text())

    def test_a_legacy_not_leased_refusal_releases_the_result_without_fencing(self):
        tid = "task-legacy"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        refusal = _http(404, {"error": "not leased to you"})
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": refusal}))
        self.assertFalse(gw._post_task_ack(tid, durable=True))
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec.get("fenced"), rec.get("awaiting_ack")), (None, False))
        with txn(tid) as t:
            self.assertIsNone(t.result_hold(), "nothing left to wait for")
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(404, {"error": "not leased to you"})}))
        self.assertEqual(gw._post_ack_first(tid, None), "gone")

    def test_a_journal_line_still_being_written_defers_the_result(self):
        self.assertTrue(gw._take_served_task(_task("task-defer"), set(), []))
        fake = self.use(FakeGateway(**{"/v1/tasks/task-defer/ack": _ack("task-defer", 1),
                                       "/v1/results": {"ok": True}}))
        self.assertTrue(gw._post_task_ack("task-defer", durable=True))
        with open(agent_turn.room_actions_path(gw._STATE), "a") as fh:
            fh.write('{"record_type": "dispatch_intent", "turn_id": nul')
        self.addCleanup(lambda: agent_turn.room_actions_path(gw._STATE).write_text(""))
        self.assertFalse(gw._deliver_result_payload("task-defer", "task-defer", "x"))
        self.assertEqual(fake.posted("/v1/results"), [])
        self.assertTrue(any("still being written" in m for m in self.logs))

    def test_a_wake_served_during_the_waiting_post_is_not_lost(self):
        tid = "task-race-wake"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        first = gw._ATTEMPTS.get(tid)["turn_id"]
        with open(agent_turn.room_actions_path(gw._STATE), "a") as fh:
            fh.write(json.dumps({"record_type": "dispatch_outcome", "turn_id": first, "seq": 1,
                                 "operation_id": f"op_{first}_1", "operation_status": "succeeded",
                                 "applied_execution_phase": "waiting_input", "active_blocker_count": 1,
                                 "applied_terminal": False, "ts": time.time()}) + "\n")
        (gw.TASKS_DIR / "archive").mkdir(exist_ok=True)
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
        (gw.RESULTS_DIR / f"{tid}.txt").write_text("I asked which one.")
        wake = _task(tid, wake={"blocker_episode_id": "e1", "resolved_blockers": [
            {"question_id": "q1", "outcome": "answered", "answer": {"kind": "text", "text": "blue"}}]})
        during = {}

        def on_result(payload):
            during["status"] = payload.get("status")
            during["wake"] = gw._write_task(dict(wake))
            return {"ok": True}
        self.use(FakeGateway(**{"/v1/results": on_result, f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        inflight = {tid}
        gw._post_ready_results(inflight)
        self.assertEqual(during, {"status": "waiting_input", "wake": None},
                         "a wake served mid-settle waits for the broker to serve it again")
        self.assertNotIn(tid, inflight, "the first attempt is settled")
        self.assertTrue(agent_turn.is_closed(gw._STATE, first))

        run = f"{tid}~~2"
        self.assertEqual(gw._write_task(dict(wake)), (run, True), "served again, it runs as its own run")
        self.assertIn('answered "blue"', (gw.TASKS_DIR / f"{run}.txt").read_text())
        self.assertTrue(gw._post_task_ack(run, durable=True))
        self.assertNotEqual(gw._ATTEMPTS.get(tid)["turn_id"], first, "a new turn at the same generation")

    def test_a_running_wake_served_again_is_a_redelivery_not_a_conflict(self):
        wake = {"blocker_episode_id": "e3", "resolved_blockers": []}
        self.assertEqual(gw._write_task(_task("task-rerun", wake=wake)), ("task-rerun", True))
        self.assertEqual(gw._write_task(_task("task-rerun", wake=wake)), ("task-rerun", True),
                         "the same episode while it runs is the same task")
        self.assertIsNone(gw._write_task(_task("task-rerun", wake={**wake, "blocker_episode_id": "e4"})),
                          "a different episode waits while this one is queued")

    def test_a_wake_admitted_between_the_old_archive_and_its_retirement_waits(self):
        tid = "task-retire-race"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1), "/v1/results": {"ok": True}}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        first = gw._ATTEMPTS.get(tid)["turn_id"]
        with open(agent_turn.room_actions_path(gw._STATE), "a") as fh:
            fh.write(json.dumps({"record_type": "dispatch_outcome", "turn_id": first, "seq": 1,
                                 "operation_id": f"op_{first}_9", "operation_status": "succeeded",
                                 "applied_execution_phase": "waiting_input", "active_blocker_count": 1,
                                 "applied_terminal": False, "ts": time.time()}) + "\n")
        (gw.RESULTS_DIR / f"{tid}.txt").write_text("I asked which one.")
        wake = _task(tid, wake={"blocker_episode_id": "e1", "resolved_blockers": [
            {"outcome": "answered", "answer": {"kind": "text", "text": "blue"}}]})
        inflight, admitted = {tid}, {}
        real_archive = gw._archive_result

        def archive_then_admit(*a, **k):
            done = real_archive(*a, **k)
            if done:
                admitted["wake"] = gw._take_served_task(dict(wake), inflight, [])
            return done
        with mock.patch.object(gw, "_archive_result", archive_then_admit):
            gw._post_ready_results(inflight)
        self.assertEqual(admitted, {"wake": False}, "admitted mid-retirement, the wake waits")
        self.assertFalse((gw.TASKS_DIR / f"{tid}~~2.txt").exists(), "and is not published")
        self.assertNotIn(tid, inflight)
        self.assertTrue(gw._take_served_task(dict(wake), inflight, []), "served again, it runs and is tracked")
        self.assertIn(f"{tid}~~2", inflight, "as its own run")
        self.assertTrue(gw._post_task_ack(f"{tid}~~2", durable=True))
        gw._close_stale_turns(inflight)
        self.assertIsNotNone(agent_turn.open_turn_for_task(gw._STATE, tid), "its turn survives the sweep")

    def test_a_wake_admitted_after_a_stale_disposal_judgement_is_not_settled_away(self):
        tid = "task-stale-disposed"
        self.assertIsNotNone(gw._write_task(_task(tid)))
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
        (gw.RESULTS_DIR / "archive" / f"{tid}.txt").write_text("I asked which one.")
        with txn(tid) as t:
            t.finish()
        inflight, seen = {tid}, {}
        wake = _task(tid, wake={"blocker_episode_id": "e1", "resolved_blockers": [
            {"outcome": "answered", "answer": {"kind": "text", "text": "blue"}}]})
        real = gw._result_disposed

        def judge_then_admit(t):
            seen["disposed"] = real(t)
            seen["wake"] = gw._take_served_task(dict(wake), inflight, [])
            return seen["disposed"]
        with mock.patch.object(gw, "_result_disposed", judge_then_admit):
            gw._post_ready_results(inflight)
        self.assertEqual(seen, {"disposed": True, "wake": False}, "the wake waits for the judgement's settle")
        self.assertEqual(gw._write_task(dict(wake)), (f"{tid}~~2", True), "served again, it runs as its own run")
        self.assertTrue((gw.TASKS_DIR / f"{tid}~~2.txt").exists())

    def test_a_settle_never_archives_a_wake_that_opened_before_it_took_the_lock(self):
        tid = "task-open-wake"
        wake = _task(tid, wake={"blocker_episode_id": "e5", "resolved_blockers": []})
        self.assertEqual(gw._write_task(dict(wake)), (tid, True))
        inflight = {tid}
        self.assertFalse(gw._settle_task(tid, inflight), "a stale caller's settle is refused")
        self.assertTrue((gw.TASKS_DIR / f"{tid}.txt").exists())
        self.assertIn(tid, inflight)
        with mock.patch.object(gw, "read_item", lambda _root, _item: {"status": "DELIVERED"}):
            self.assertTrue(gw._settle_task(tid, inflight), "once the wake's own result is delivered it settles")
        self.assertFalse((gw.TASKS_DIR / f"{tid}.txt").exists())
        self.assertNotIn(tid, inflight)

    def test_settling_an_earlier_attempt_never_finishes_a_later_one(self):
        seed_ack("task-moved", _ack("task-moved", 1))
        with txn("task-moved") as t:
            earlier = t.claim("result", sole=True)
        with txn("task-moved") as t:
            self.assertEqual(t.open_wake({"blocker_episode_id": "e2"}, "task-moved"), ("e2", "task-moved"))
        with txn("task-moved") as t:
            self.assertEqual(t.finish(earlier), "kept", "nothing of the earlier attempt is left")
        self.assertTrue(agent_turn.is_closed(gw._STATE, earlier["turn_id"]))
        self.assertEqual(gw._ATTEMPTS.get("task-moved")["wake_episode"], "e2", "the wake is kept")
        self.assertIsNone(gw._ATTEMPTS.get("task-moved").get("claims"), "the earlier claim is released")
        with txn("task-moved") as t:
            self.assertEqual(t.open_wake({"blocker_episode_id": "e2"}, "task-moved"), ("e2", "task-moved"),
                             "and not marked as already run")

    def test_a_new_wake_starts_a_new_journal_boundary(self):
        self.assertTrue(gw._take_served_task(_task("task-wake-boundary"), set(), []))
        first = gw._ATTEMPTS.get("task-wake-boundary")["published_at"]
        with mock.patch.object(gw._ATTEMPTS, "now", lambda: first + 50), txn("task-wake-boundary") as t:
            t.open_wake({"blocker_episode_id": "e7"}, "task-wake-boundary")
            t.remember_task(_task("task-wake-boundary"))
        self.assertEqual(gw._ATTEMPTS.get("task-wake-boundary")["published_at"], first + 50)
    def stopped_by_a_beat(self, tid):
        """A running attempt; the next per-task beat answers task_cancelled (a cooperative Stop)."""
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        stop = {"ok": True, "task_errors": [{"task_id": tid, "error_code": "task_cancelled",
                                              "cancel_requested_generation": 1, "cancel_requested_attempt": 1}]}
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1), "/v1/tasks/heartbeat": stop,
                                       "/v1/results": {"ok": True}}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        gw._post_task_beats({tid}, force=True)
        self.assertEqual(len(self.controls(tid)), 1, "the core is asked to stop")
        gw._post_task_beats({tid}, force=True)
        self.assertEqual(len(self.controls(tid)), 1, "asked once, however many beats repeat it")
        self.assertEqual(fake.posted("/cancel-ack"), [], "nothing is confirmed yet")
        return fake, self.controls(tid)[0].stem

    def test_stop_then_finish_posts_the_result_and_sends_no_cancel_ack(self):
        tid = "task-stop-finish"
        fake, ctl = self.stopped_by_a_beat(tid)
        self.assertTrue(gw._deliver_result_payload(tid, tid, "done anyway"))
        self.assertEqual(fake.posted("/v1/results")[0]["status"], "completed", "a finished reply is delivered")
        (gw.RESULTS_DIR / f"{ctl}.txt").write_text("[no-send]")
        gw._post_task_beats({tid}, force=True)
        self.assertEqual(fake.posted("/cancel-ack"), [])

    def test_stop_then_finish_seen_by_the_control_task_first_still_sends_no_cancel_ack(self):
        tid = "task-stop-finish-2"
        fake, ctl = self.stopped_by_a_beat(tid)
        (gw.RESULTS_DIR / f"{tid}.txt").write_text("done anyway")
        (gw.RESULTS_DIR / f"{ctl}.txt").write_text("[no-send]")
        gw._post_task_beats({tid}, force=True)
        self.assertEqual(fake.posted("/cancel-ack"), [])
        self.assertTrue(gw._deliver_result_payload(tid, tid, "done anyway"))
        self.assertEqual(len(fake.posted("/v1/results")), 1)
        (gw.RESULTS_DIR / f"{tid}.txt").unlink()

    def test_stop_then_stop_sends_only_the_cancel_ack(self):
        tid = "task-stop-stop"
        fake, ctl = self.stopped_by_a_beat(tid)
        (gw.RESULTS_DIR / f"{ctl}.txt").write_text("[no-send]")
        gw._post_task_beats({tid}, force=True)
        self.assertEqual(fake.posted("/cancel-ack")[0]["cancel_requested_generation"], 1)
        self.assertTrue(gw._deliver_result_payload(tid, tid, "late"), "a later result settles")
        self.assertEqual(fake.posted("/v1/results"), [], "and is never posted")

    def test_stop_then_a_no_send_result_confirms_the_stop_instead_of_no_action_needed(self):
        for i, (body, no_send) in enumerate([("[no-send]", True), ("  \n", False)]):
            tid = f"task-stop-nosend-{i}"
            fake, ctl = self.stopped_by_a_beat(tid)
            (gw.RESULTS_DIR / f"{tid}.txt").write_text(body)
            (gw.RESULTS_DIR / f"{ctl}.txt").write_text("[no-send]")
            gw._post_task_beats({tid}, force=True)
            self.assertEqual(fake.posted("/cancel-ack"), [], "an undelivered result is ruled by delivery")
            self.assertTrue(gw._deliver_result_payload(tid, tid, body, no_send=no_send))
            self.assertEqual(fake.posted("/v1/results"), [], "never posted as no_action_needed")
            self.assertEqual(fake.posted("/cancel-ack")[0]["cancel_requested_generation"], 1)
            (gw.RESULTS_DIR / f"{tid}.txt").unlink()

    def test_a_stop_confirmed_by_a_result_is_retried_until_its_cancel_ack_lands(self):
        tid = "task-stop-nosend-retry"
        fake, _ = self.stopped_by_a_beat(tid)
        fake.answers[f"/v1/tasks/{tid}/cancel-ack"] = _http(503, {})
        self.assertFalse(gw._deliver_result_payload(tid, tid, "", no_send=True), "held, not settled")
        fake.answers[f"/v1/tasks/{tid}/cancel-ack"] = {"ok": True}
        self.assertTrue(gw._deliver_result_payload(tid, tid, "", no_send=True))
        self.assertEqual(fake.posted("/v1/results"), [])
        self.assertEqual(len(fake.posted("/cancel-ack")), 2)

    def test_every_held_task_beats_each_interval_even_while_the_client_heartbeat_backs_off(self):
        tid = "task-long-turn"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1), "/v1/heartbeat": _http(404, {})}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        mock.patch.object(gw, "_heartbeat_retry_at", time.time() + 3600).start()
        clock = [time.time()]
        mock.patch.object(gw.time, "time", lambda: clock[0]).start()
        mock.patch.object(gw, "_last_task_beat_at", 0.0).start()
        for _ in range(3):
            gw._post_heartbeat({tid})
            clock[0] += gw.task_attempts.TASK_HEARTBEAT_INTERVAL_S
        beats = [b for b in fake.posted("/v1/tasks/heartbeat") if any(t["task_id"] == tid for t in b["tasks"])]
        self.assertEqual(len(beats), 3, "one per interval through a long turn")
        self.assertEqual(fake.posted("/v1/heartbeat"), [], "the client beat stayed in its backoff")

    def test_a_held_task_beats_every_15_s_and_the_poll_returns_when_the_beat_is_due(self):
        self.assertEqual(gw.task_attempts.TASK_HEARTBEAT_INTERVAL_S, 15)
        self.assertEqual(gw.HEARTBEAT_INTERVAL, 60, "the client heartbeat keeps its cadence")
        tid = "task-beat-cadence"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        self.assertEqual(gw._poll_wait({tid}), gw.POLL_WAIT, "no generation yet: no beat to wait for")
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        clock = [time.time()]
        mock.patch.object(gw.time, "time", lambda: clock[0]).start()
        mock.patch.object(gw, "_last_task_beat_at", 0.0).start()
        gw._post_task_beats({tid})
        self.assertEqual(gw._poll_wait({tid}), 15)
        clock[0] += 14
        gw._post_task_beats({tid})
        self.assertEqual(gw._poll_wait({tid}), 1, "the poll returns as the next beat falls due")
        clock[0] += 1
        gw._post_task_beats({tid})
        self.assertEqual(len(fake.posted("/v1/tasks/heartbeat")), 2, "one beat per 15 s, none sooner")
        self.assertEqual(gw._poll_wait(set()), gw.POLL_WAIT, "nothing held: the full long poll")

    def held_then_archived(self, tid, lease=None):
        """A served, acked attempt whose task file the core archived unanswered (the orphan pass)."""
        self.assertTrue(gw._take_served_task(_task(tid, **({"lease_id": lease} if lease else {})), set(), []))
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1), "/v1/results": {"ok": True}}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id=lease))
        (gw.TASKS_DIR / "archive").mkdir(exist_ok=True)
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
        clock = [time.time()]
        mock.patch.object(gw._ATTEMPTS, "now", lambda: clock[0]).start()
        self.addCleanup(lambda: (gw.RESULTS_DIR / f"{tid}.txt").unlink(missing_ok=True))
        return fake, clock

    def beat(self, tid):
        gw._post_task_beats({tid}, force=True)
        return gw._ATTEMPTS.beating({gw._broker_tid(tid)})

    def test_an_attempt_archived_unanswered_is_failed_after_ten_minutes_and_stops_beating(self):
        tid = "task-lost"
        fake, clock = self.held_then_archived(tid)
        self.assertTrue(self.beat(tid), "the first miss only starts the clock")
        clock[0] += gw.task_attempts.DISPATCH_LOST_S - 1
        self.assertTrue(self.beat(tid))
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists())
        clock[0] += 1
        self.assertFalse(self.beat(tid), "no beat holds an attempt nobody will answer")
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.DISPATCH_LOST_RESULT)
        self.assertTrue(any("posting it as failed" in m for m in self.logs))
        gw._post_ready_results({tid})                # the drain reads the file it posts
        post = fake.posted("/v1/results")[0]
        self.assertEqual((post["status"], post["no_send"], post["execution_generation"]), ("failed", True, 1))

    def test_a_wake_archived_unanswered_is_failed_despite_the_first_attempts_reply(self):
        tid = "task-lost-wake"
        task = {"id": tid, "task": "hi", "source": "ag2space", "user_id": "@o:x"}
        self.assertIsNotNone(gw._write_task(dict(task)))
        for d in (gw.TASKS_DIR, gw.RESULTS_DIR):
            (d / "archive").mkdir(exist_ok=True)
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
        (gw.RESULTS_DIR / "archive" / f"{tid}.txt").write_text("I asked.")
        wake = {"blocker_episode_id": "e1", "resolved_blockers": [
            {"question_id": "q1", "outcome": "answered", "answer": {"kind": "text", "text": "yes"}}]}
        run = f"{tid}~~2"
        self.assertEqual(gw._write_task({**task, "wake": wake}), (run, True))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1), "/v1/results": {"ok": True}}))
        self.assertTrue(gw._post_task_ack(run, durable=True))
        (gw.TASKS_DIR / f"{run}.txt").rename(gw.TASKS_DIR / "archive" / f"{run}.txt")
        clock = [time.time()]
        mock.patch.object(gw._ATTEMPTS, "now", lambda: clock[0]).start()
        self.addCleanup(lambda: (gw.RESULTS_DIR / f"{run}.txt").unlink(missing_ok=True))
        self.beat(run)
        clock[0] += gw.task_attempts.DISPATCH_LOST_S
        self.assertFalse(self.beat(run), "the first run's archived reply does not answer the wake")
        self.assertEqual((gw.RESULTS_DIR / f"{run}.txt").read_text(), gw.DISPATCH_LOST_RESULT)

    def test_an_attempt_the_broker_still_holds_is_failed_not_dropped_from_flight(self):
        tid = "task-archived-long-ago"
        fake, clock = self.held_then_archived(tid)
        old = time.time() - 2 * gw.ARCHIVE_COMPLETION_GRACE_S
        os.utime(gw.TASKS_DIR / "archive" / f"{tid}.txt", (old, old))
        inflight = {tid}
        gw._reconcile_abandoned(inflight, gw._reconcile_abandoned(inflight, set()))
        self.assertEqual(inflight, {tid}, "a live attempt is not completed elsewhere")
        self.assertFalse(any("dropped abandoned" in m for m in self.logs))
        self.beat(tid)
        clock[0] += gw.task_attempts.DISPATCH_LOST_S
        self.assertFalse(self.beat(tid))
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.DISPATCH_LOST_RESULT)

    def test_a_task_put_back_in_the_queue_resets_the_clock(self):
        tid = "task-requeued"
        _, clock = self.held_then_archived(tid)
        self.beat(tid)
        clock[0] += 300
        (gw.TASKS_DIR / "archive" / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.txt")
        self.beat(tid)
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
        clock[0] += 300
        self.assertTrue(self.beat(tid), "the clock restarted when the file came back")
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists())

    def test_turn_activity_on_the_task_keeps_it_alive(self):
        tid = "task-busy-elsewhere"
        _, clock = self.held_then_archived(tid)
        self.beat(tid)
        log = gw._STATE / "agent-activity.jsonl"
        with open(log, "a") as fh:
            fh.write(json.dumps({"ts": clock[0] + 500, "kind": "working", "task": {"id": tid}}) + "\n")
            fh.write(json.dumps({"ts": clock[0] + 590, "kind": "done", "task": {"id": tid},
                                 "projection": "TASK_STATUS", "audience": "room"}) + "\n")
        clock[0] += gw.task_attempts.DISPATCH_LOST_S + 10
        self.assertTrue(self.beat(tid), "a tool call 110 s ago is turn activity; a delivery row is not")
        clock[0] += 500
        self.assertFalse(self.beat(tid))

    def held_in_queue(self, tid, motion="idle"):
        """A served, acked attempt whose task file is still queued; the core reads `motion`."""
        self.motion = mock.patch.object(gw, "_core_motion", return_value=motion).start()
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1), "/v1/results": {"ok": True}}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        clock = [(gw.TASKS_DIR / f"{tid}.txt").stat().st_mtime]
        mock.patch.object(gw._ATTEMPTS, "now", lambda: clock[0]).start()
        self.addCleanup(lambda: (gw.RESULTS_DIR / f"{tid}.txt").unlink(missing_ok=True))
        return fake, clock

    def row(self, tid, ts, kind="working"):
        with open(gw._STATE / "agent-activity.jsonl", "a") as fh:
            fh.write(json.dumps({"ts": ts, "kind": kind, "task": {"id": tid}}) + "\n")

    def test_a_queued_task_the_core_took_up_then_left_is_failed_after_thirty_minutes(self):
        tid = "task-stalled"
        fake, clock = self.held_in_queue(tid)
        self.row(tid, clock[0] + 12, "processing")
        clock[0] += 12 + gw.task_attempts.STALLED_S - 1
        self.assertTrue(self.beat(tid))
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists())
        clock[0] += 1
        self.assertFalse(self.beat(tid), "no beat holds a task the core walked away from")
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.STALLED_RESULT)
        self.assertTrue(any("then wrote no turn activity or result for 30 min and is not mid-turn" in m
                            for m in self.logs))
        gw._post_ready_results({tid})                # the drain reads the file it posts
        post = fake.posted("/v1/results")[0]
        self.assertEqual((post["status"], post["no_send"]), ("failed", True))

    def test_a_core_mid_turn_on_a_long_tool_call_is_never_stalled(self):
        tid = "task-long-tool"
        _, clock = self.held_in_queue(tid, motion="moving")
        self.row(tid, clock[0] + 5, "working")
        clock[0] += 4 * gw.task_attempts.STALLED_UNOBSERVED_S
        self.assertTrue(self.beat(tid), "still beating: the core reads mid-turn")
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "no failure posted over a live turn")
        self.motion.return_value = "idle"
        self.assertFalse(self.beat(tid), "once the core reads idle, the silence is an abandoned turn")

    def test_with_no_reading_of_the_core_the_stall_waits_the_long_window(self):
        tid = "task-unobserved"
        _, clock = self.held_in_queue(tid, motion=None)
        self.row(tid, clock[0] + 5, "working")
        clock[0] += 5 + gw.task_attempts.STALLED_UNOBSERVED_S - 1
        self.assertTrue(self.beat(tid))
        clock[0] += 1
        self.assertFalse(self.beat(tid))
        self.assertTrue(any("for 120 min and is not mid-turn" in m for m in self.logs))

    def test_the_core_motion_is_the_cores_own_reading(self):
        cmd = mock.Mock()
        cmd.row.return_value = {"motion": "unknown"}
        with mock.patch.object(gw, "_HEALTH_COMMAND", cmd):
            self.assertIsNone(gw._core_motion(), "Sparrow's own fill-in never counts as the core moving")
            cmd.row.return_value = {"motion": "moving"}
            self.assertEqual(gw._core_motion(), "moving")
            for wedged in ({"condition": "abnormal"}, {"alive": False}):
                cmd.row.return_value = {"motion": "moving", **wedged}
                self.assertEqual(gw._core_motion(), "idle", f"{wedged}: a wedged core is not working")
        with mock.patch.object(gw, "_HEALTH_COMMAND", None), \
                mock.patch.object(gw, "_health_snapshot", return_value={"agents": [
                    {"role": "core", "motion": "idle", "alive": True}]}):
            self.assertEqual(gw._core_motion(), "idle")
        with mock.patch.object(gw, "_HEALTH_COMMAND", None), mock.patch.object(gw, "_health_snapshot", return_value=None):
            self.assertIsNone(gw._core_motion())

    def test_a_queued_task_the_core_never_took_up_keeps_beating(self):
        tid = "task-waiting-its-turn"
        _, clock = self.held_in_queue(tid)
        self.row(tid, clock[0] - 60, "done")
        clock[0] += 4 * gw.task_attempts.STALLED_S
        self.assertTrue(self.beat(tid), "a row from before this file was queued is an earlier turn's")
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists())

    def test_turn_activity_on_a_queued_task_keeps_it_alive(self):
        tid = "task-long-turn"
        _, clock = self.held_in_queue(tid)
        self.row(tid, clock[0] + 5, "processing")
        self.row(tid, clock[0] + gw.task_attempts.STALLED_S, "working")
        clock[0] += gw.task_attempts.STALLED_S + 600
        self.assertTrue(self.beat(tid))
        clock[0] += gw.task_attempts.STALLED_S
        self.assertFalse(self.beat(tid))

    def test_a_real_result_written_first_is_never_replaced(self):
        tid = "task-raced"
        rfile = gw.RESULTS_DIR / f"{tid}.txt"
        rfile.write_text("the real answer")
        self.addCleanup(lambda: rfile.unlink(missing_ok=True))
        self.assertIsNone(gw._write_if_absent(rfile, gw.DISPATCH_LOST_RESULT))
        self.assertEqual(rfile.read_text(), "the real answer")
        self.assertFalse(gw._write_if_absent(gw.RESULTS_DIR / "no-such-dir" / "task-x.txt", "x"))
        target = gw.RESULTS_DIR / "task-y.txt"
        for fault in ("fsync", "link"):
            with self.subTest(fault=fault), \
                    mock.patch.object(gw.task_attempts.os, fault, side_effect=OSError("disk full")):
                self.assertFalse(gw._write_if_absent(target, "x"), "a failed write is a failure")
            self.assertFalse(target.exists(), "no empty placeholder blocks the retry")
        self.assertEqual([p.name for p in gw.RESULTS_DIR.iterdir() if p.name.startswith(".task-y")], [],
                         "the temp file is gone")
        self.assertTrue(gw._write_if_absent(target, "x"), "the retry writes it")
        self.assertEqual(target.read_text(), "x")
        target.unlink()

    def test_a_failure_result_that_cannot_be_written_is_retried_next_beat(self):
        tid = "task-lost-retry"
        _, clock = self.held_then_archived(tid)
        self.beat(tid)
        clock[0] += gw.task_attempts.DISPATCH_LOST_S
        with mock.patch.object(gw, "_write_if_absent", return_value=False):
            self.assertFalse(self.beat(tid), "recorded as lost; its result is owed")
        self.assertTrue(any("retried next beat" in m for m in self.logs))
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists())
        self.assertFalse(self.beat(tid))
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.DISPATCH_LOST_RESULT)

    def test_a_cancel_instruction_that_cannot_be_written_is_reported(self):
        with mock.patch.object(gw, "_durable_write", return_value=False):
            gw._queue_cancel_instruction("task-h5", "task_cancelled")
        self.assertTrue(any("cancel instruction not written" in m for m in self.logs))

    def test_the_ledger_reconcile_closes_what_the_server_closed(self):
        seed_ack("task-h6", _ack("task-h6", 1))
        seed("task-h6", acked_at=0)
        self.use(FakeGateway(**{"/v1/tasks/active": {"tasks": [{"task_id": "task-srv"}], "next_cursor": None}}))
        mock.patch.object(gw, "_last_ledger_reconcile", 0.0).start()
        gw._reconcile_ledger({"task-h6"})
        self.assertEqual(self.controls("task-h6"), [], "not listed is not closed: the core is not stopped")
        self.assertIsNone(gw._ATTEMPTS.get("task-h6").get("fenced"))
        listed = json.loads(gw.OUTSTANDING_TASKS_FILE.read_text())
        self.assertEqual([t["task_id"] for t in listed["tasks"]], ["task-srv"])
        self.assertTrue(any("not held here" in m for m in self.logs))
        logged = sum("task-h6 is not listed" in m for m in self.logs)
        gw._reconcile_ledger({"task-h6"})
        self.assertEqual(sum("task-h6 is not listed" in m for m in self.logs), logged, "the cadence holds the next pass")
        mock.patch.object(gw, "_last_ledger_reconcile", 0.0).start()
        self.use(FakeGateway(**{"/v1/tasks/active": _http(503, {"error_code": "task_api_unavailable"})}))
        gw._reconcile_ledger({"task-h6"})

    def test_the_close_retry_runs_on_the_beat_cadence(self):
        turn = seed_ack("task-h9", _ack("task-h9", 1))["turn_id"]
        mock.patch.object(gw, "_last_turn_close_pass", 0.0).start()
        gw._retry_turn_closes({"task-h9"})
        self.assertFalse(agent_turn.is_closed(gw._STATE, turn), "in flight: left open")
        gw._retry_turn_closes(set())
        self.assertFalse(agent_turn.is_closed(gw._STATE, turn), "the cadence holds the next pass")
        mock.patch.object(gw, "_last_turn_close_pass", 0.0).start()
        gw._retry_turn_closes(set())
        self.assertTrue(agent_turn.is_closed(gw._STATE, turn))
        self.assertTrue(any("finished turn file(s)" in m for m in self.logs))

    def test_turns_whose_task_left_flight_are_closed_at_start(self):
        turn = seed_ack("task-h7", _ack("task-h7", 1))["turn_id"]
        gw._close_stale_turns(set())
        self.assertTrue(agent_turn.is_closed(gw._STATE, turn))
        self.assertTrue(any(m.startswith("closed ") and "turn file" in m for m in self.logs))

    def answered_task(self, tid):
        """A first attempt that asked and settled: its task and result archived."""
        task = {"id": tid, "task": "hi", "source": "ag2space", "user_id": "@o:x"}
        self.assertIsNotNone(gw._write_task(dict(task)))
        for d in (gw.TASKS_DIR / "archive", gw.RESULTS_DIR / "archive"):
            d.mkdir(exist_ok=True)
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
        (gw.RESULTS_DIR / "archive" / f"{tid}.txt").write_text("I asked.")
        return {**task, "wake": {"blocker_episode_id": "e1", "resolved_blockers": [
            {"question_id": "q1", "outcome": "answered", "answer": {"kind": "text", "text": "b"}}]}}

    def failing_save(self, prefix):
        """The ledger writes a file of the base bridge's held fail: one part's, or every one."""
        return failing_commit({"relay-broker-contract": "lease contract", "relay-wake-episodes": "retirement",
                               "relay-wake-items": "retirement"}.get(prefix, ""))

    def test_a_wake_that_cannot_be_saved_is_not_queued_or_acked(self):
        wake = self.answered_task("task-wopen")
        with self.failing_save("relay-task-attempts-v2"):
            self.assertIsNone(gw._write_task(dict(wake)), "left unacked for a re-serve")
        self.assertEqual(list(gw.TASKS_DIR.glob("task-wopen*.txt")), [], "nothing queued")
        self.assertIsNone(gw._ATTEMPTS.get("task-wopen").get("wake_episode"))
        self.assertTrue(any("wake e1 not saved" in m for m in self.logs))
        gw._WAKES_LEFT_UNACKED.discard(("task-wopen", "e1"))
        self.assertEqual(gw._write_task(dict(wake)), ("task-wopen~~2", True), "the re-serve runs it as its own run")

    def test_a_wake_result_whose_retirement_fails_stays_live_and_is_retried(self):
        tid = "task-wret"
        run = f"{tid}~~2"
        self.assertEqual(gw._write_task(dict(self.answered_task(tid))), (run, True))
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        self.assertTrue(gw._post_task_ack(run, durable=True))
        (gw.TASKS_DIR / "archive").mkdir(exist_ok=True)
        (gw.TASKS_DIR / f"{run}.txt").rename(gw.TASKS_DIR / "archive" / f"{run}.txt")
        (gw.RESULTS_DIR / f"{run}.txt").write_text("went with b")
        inflight = {run}
        with self.failing_save("relay-wake-episodes"):
            gw._post_ready_results(inflight)
        self.assertTrue((gw.RESULTS_DIR / f"{run}.txt").exists(), "not archived: still live")
        self.assertIn(run, inflight, "still in flight")
        self.assertEqual(gw._ATTEMPTS.get(tid).get("wake_episode"), "e1", "the attempt is kept")
        self.assertTrue(any(f"result {run}: settled, but its attempt could not be retired" in m
                            for m in self.logs))
        gw._post_ready_results(inflight)
        self.assertFalse((gw.RESULTS_DIR / f"{run}.txt").exists(), "the retry archives it")
        self.assertNotIn(run, inflight)
        self.assertEqual(len(fake.posted("/v1/results")), 1, "posted once across both passes")
        self.assertIn("e1", wakes_seen(tid))
        self.assertEqual(gw._ATTEMPTS.run_item(run), f"{tid}.wake-e1")

    def test_a_wake_result_whose_records_cannot_be_read_at_retirement_stays_live(self):
        for broken in ("malformed", "unreadable"):
            with self.subTest(broken=broken):
                tid = f"task-wrd-{broken}"
                run = f"{tid}~~2"
                wake = self.answered_task(tid)
                self.assertEqual(gw._write_task(dict(wake)), (run, True))
                path, good = gw._ATTEMPTS.path, []

                def break_it(_payload, path=path, broken=broken, good=good):
                    good.append(path.read_bytes())
                    path.unlink()
                    path.write_text("{not json") if broken == "malformed" else path.mkdir()
                    return {"ok": True}

                fake = self.use(FakeGateway(**{"/v1/results": break_it, f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
                self.assertTrue(gw._post_task_ack(run, durable=True))
                (gw.TASKS_DIR / f"{run}.txt").rename(gw.TASKS_DIR / "archive" / f"{run}.txt")
                (gw.RESULTS_DIR / f"{run}.txt").write_text("went with b")
                inflight = {run}
                gw._post_ready_results(inflight)
                self.assertEqual(len(fake.posted("/v1/results")), 1)
                self.assertTrue((gw.RESULTS_DIR / f"{run}.txt").exists(), "not archived: still live")
                self.assertIn(run, inflight, "still in flight")

                path.rmdir() if path.is_dir() else path.unlink()
                path.write_bytes(good[0])
                gw._post_ready_results(inflight)
                self.assertFalse((gw.RESULTS_DIR / f"{run}.txt").exists(), "the retry archives it")
                self.assertNotIn(run, inflight)
                self.assertEqual(len(fake.posted("/v1/results")), 1, "never posted a second time")
                self.assertIn("e1", wakes_seen(tid))
                self.assertIsNone(gw._write_task(dict(wake)), "the replay is refused")

    def test_an_unreadable_ledger_is_kept_through_other_tasks_and_repeated_drains(self):
        for broken in ("malformed", "unreadable"):
            with self.subTest(broken=broken):
                tid = f"task-wkeep-{broken}"
                run = f"{tid}~~2"
                wake = self.answered_task(tid)
                self.assertEqual(gw._write_task(dict(wake)), (run, True))
                path, good = gw._ATTEMPTS.path, []

                def break_it(_payload, broken=broken):
                    good.append(path.read_bytes())
                    path.unlink()
                    path.write_text("{not json") if broken == "malformed" else path.mkdir()
                    return {"ok": True}

                fake = self.use(FakeGateway(**{"/v1/results": break_it, f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
                self.assertTrue(gw._post_task_ack(run, durable=True))
                (gw.TASKS_DIR / f"{run}.txt").rename(gw.TASKS_DIR / "archive" / f"{run}.txt")
                (gw.RESULTS_DIR / f"{run}.txt").write_text("went with b")
                inflight = {run}
                gw._post_ready_results(inflight)
                broken_bytes = None if path.is_dir() else path.read_bytes()

                self.assertIsNone(gw._write_task(_task(f"{tid}-other")), "another task is left for a re-serve")
                self.assertEqual(None if path.is_dir() else path.read_bytes(), broken_bytes,
                                 "the unreadable ledger is left as it was")
                root = gw._delivery_core().backend.root
                for _ in range(3):
                    gw._post_ready_results(inflight)
                self.assertEqual(len(fake.posted("/v1/results")), 1, "never posted a second time")
                self.assertTrue((gw.RESULTS_DIR / f"{run}.txt").exists(), "still live")
                self.assertIn(run, inflight)
                self.assertIsNone(gw.read_item(root, tid), "no delivery under the first attempt's item")
                self.assertEqual((gw.read_item(root, f"{tid}.wake-e1") or {}).get("status"), "DELIVERED")
                self.assertIsNone(gw._write_task(dict(wake)), "nor queued again while unknown")

                path.rmdir() if path.is_dir() else path.unlink()
                path.write_bytes(good[0])
                gw._post_ready_results(inflight)
                self.assertFalse((gw.RESULTS_DIR / f"{run}.txt").exists(), "recovered: archived")
                self.assertNotIn(run, inflight)
                self.assertEqual(len(fake.posted("/v1/results")), 1)
                self.assertIn("e1", wakes_seen(tid))
                self.assertEqual(gw._ATTEMPTS.run_item(run), f"{tid}.wake-e1")
                self.assertIsNone(gw._write_task(dict(wake)), "the replay is refused")

    def ran(self, tid):
        (gw.TASKS_DIR / "archive").mkdir(exist_ok=True)
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
        (gw.RESULTS_DIR / f"{tid}.txt").write_text("done")

    def posted_once_under_its_generation(self, fake, tid, inflight):
        gw._post_ready_results(inflight)
        posted = fake.posted("/v1/results")
        self.assertEqual(len(posted), 1)
        self.assertEqual((posted[0]["execution_generation"], posted[0]["worker_id"]),
                         (1, gw._ATTEMPTS.worker_id()), "posted under its generation, not as legacy")
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists())

    def test_a_task_whose_attempt_cannot_be_recorded_is_not_queued_or_acked(self):
        tid, path = "task-adm1", gw._ATTEMPTS.path
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        good = path.read_bytes() if path.exists() else b"{}"
        path.write_text("{not json")
        inflight, fates = set(), {}
        gw._take_served_round([_task(tid)], inflight, [], fates, [])
        self.assertFalse((gw.TASKS_DIR / f"{tid}.txt").exists(), "not queued")
        self.assertEqual(fake.posted("/ack"), [], "not acked: the broker serves it again")
        self.assertNotIn(tid, inflight)
        self.assertEqual(path.read_text(), "{not json", "left as it was")
        self.assertTrue(any(f"task {tid}: its attempt could not be recorded" in m for m in self.logs))

        path.write_bytes(good)
        gw._take_served_round([_task(tid)], inflight, [], fates, [])
        self.assertEqual(len(fake.posted("/ack")), 1)
        self.assertIn(tid, inflight)
        self.ran(tid)
        self.posted_once_under_its_generation(fake, tid, inflight)

    def test_an_ack_whose_generation_cannot_be_recorded_keeps_its_pending_ack(self):
        tid, path, good = "task-adm2", gw._ATTEMPTS.path, []

        def ack(_payload):
            good.append(path.read_bytes())
            path.write_text("{not json")
            return _ack(tid, 1)

        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, f"/v1/tasks/{tid}/ack": ack}))
        inflight, fates = set(), {}
        gw._take_served_round([_task(tid)], inflight, [], fates, [])
        self.assertTrue((gw.TASKS_DIR / f"{tid}.txt").exists())
        self.assertIn(tid, gw._load_pending_acks(), "the pending ack is kept to retry from")
        self.assertNotEqual(fates.get(tid), "acked")
        self.assertTrue(any(f"task ack for {tid}: accepted, but its generation" in m for m in self.logs))
        self.ran(tid)
        gw._post_ready_results(inflight)
        self.assertEqual(fake.posted("/v1/results"), [], "unknown: nothing posted")

        path.write_bytes(good[0])
        gw._post_ready_results(inflight)
        self.assertEqual(fake.posted("/v1/results"), [], "still waiting for its generation")
        fake.answers[f"/v1/tasks/{tid}/ack"] = _ack(tid, 1)
        gw._retry_pending_acks(inflight)
        self.assertNotIn(tid, gw._load_pending_acks(), "retired once the generation is on disk")
        self.assertEqual(gw._ATTEMPTS.get(tid)["execution_generation"], 1)
        self.posted_once_under_its_generation(fake, tid, inflight)

    def test_an_ack_first_task_whose_generation_cannot_be_recorded_stays_staged(self):
        tid, path, good = "task-af-unrec", gw._ATTEMPTS.path, []

        def ack(_payload):
            good.append(path.read_bytes())
            path.write_text("{not json")
            return _ack(tid, 1)

        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, f"/v1/tasks/{tid}/ack": ack}))
        inflight, fates = set(), {}
        self.assertFalse(gw._take_ack_first(_task(tid, ack_first=True), inflight, fates))
        self.assertEqual(len(fake.posted("/ack")), 1)
        self.assertFalse((gw.TASKS_DIR / f"{tid}.txt").exists(), "not published")
        entry = (gw._load_held() or {}).get(tid)
        self.assertIsNotNone(entry, "the manifest entry is live")
        self.assertIsNot(entry.get("accepted"), True, "not marked accepted")
        self.assertTrue((gw._held_dir() / entry["staged"]).exists(), "still staged")
        self.assertTrue(any(f"ack_first task {tid}: accepted, but its generation" in m for m in self.logs))

        path.write_bytes(good[0])
        fake.answers[f"/v1/tasks/{tid}/ack"] = _ack(tid, 1)
        self.assertTrue(gw._advance_held_tasks(inflight), "the next pass re-acks and publishes")
        self.assertEqual(len(fake.posted("/ack")), 2)
        self.assertNotIn(tid, gw._load_held() or {})
        self.assertEqual(gw._ATTEMPTS.get(tid)["execution_generation"], 1)
        self.ran(tid)
        self.posted_once_under_its_generation(fake, tid, inflight)

    def no_ctl_write(self):
        real = gw._durable_write
        return mock.patch.object(gw, "_durable_write", side_effect=lambda path, text, *a, **k: (
            False if path.name.startswith("task-ctl-") else real(path, text, *a, **k)))

    def beat_error(self, tid, code):
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True, "task_errors": [
            {"task_id": tid, "error_code": code}]}}))
        gw._post_task_beats({tid}, force=True)

    def test_an_ack_first_task_recovers_from_one_failed_contract_write(self):
        tid = "task-af-contract"
        data = ledger()
        data.pop("contract", None)
        write_ledger(data)
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        inflight = set()
        with self.failing_save("relay-broker-contract"):
            self.assertFalse(gw._take_ack_first(_task(tid, ack_first=True), inflight, {}))
        self.assertNotIn("contract", ledger())
        self.assertEqual(gw._ATTEMPTS.get(tid)["execution_generation"], 1, "the generation is saved")
        self.assertIsNot((gw._load_held() or {})[tid].get("accepted"), True, "kept staged")
        self.assertTrue(gw._advance_held_tasks(inflight), "the same-generation re-ack repairs the contract")
        self.assertIn("contract", ledger())
        self.assertNotIn(tid, gw._load_held() or {})
        self.ran(tid)
        self.posted_once_under_its_generation(fake, tid, inflight)

    def test_a_leased_ack_first_task_ends_its_ack_wait_and_its_reply_is_posted(self):
        tid = "task-af-leased"
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        inflight = set()
        self.assertTrue(gw._take_ack_first(_task(tid, ack_first=True, lease_id="LA"), inflight, {}))
        self.assertEqual(fake.posted("/ack")[0]["lease_id"], "LA")
        rec = gw._ATTEMPTS.get(tid)
        self.assertFalse(rec.get("awaiting_ack"), "the ack of the serve that holds the lease is recorded")
        self.assertEqual(rec["execution_generation"], 1)
        self.ran(tid)
        self.posted_once_under_its_generation(fake, tid, inflight)

    def test_beat_fences_and_stops_act_only_once_recorded(self):
        for code in ("stale_execution_generation", "task_cancelled"):
            with self.subTest(code=code):
                tid = f"task-unrec-{code.split('_')[0]}"
                seed_ack(tid, _ack(tid, 1))
                with self.failing_save("relay-task-attempts-v2"):
                    self.beat_error(tid, code)
                self.assertEqual(self.controls(tid), [], "nothing acted on before it is recorded")
                self.assertFalse(gw._ATTEMPTS.get(tid).get("fenced") or gw._ATTEMPTS.get(tid).get("stop_requested"))
                self.beat_error(tid, code)
                self.beat_error(tid, code)
                self.assertEqual(len(self.controls(tid)), 1, "acted on once, when the beat repeats it")
                rec = gw._ATTEMPTS.get(tid)
                ctl = rec.get("fence_ctl") or rec.get("stop_ctl")
                self.assertEqual(self.controls(tid)[0].name, f"{ctl}.txt", "the recorded id is the queued one")

    def test_a_cancel_owed_by_a_fence_or_stop_survives_a_storage_outage(self):
        for code in ("stale_execution_generation", "task_cancelled"):
            with self.subTest(code=code):
                tid = f"task-owed-{code.split('_')[0]}"
                seed_ack(tid, _ack(tid, 1))
                with self.no_ctl_write():
                    self.beat_error(tid, code)
                    self.assertEqual(self.controls(tid), [])
                    with self.failing_save("relay-task-attempts-v2"):
                        self.beat_error(tid, code)
                self.assertEqual(self.controls(tid), [], "nothing written during the outage")
                self.beat_error(tid, code)
                self.beat_error(tid, code)
                self.assertEqual(len(self.controls(tid)), 1, "storage back: exactly one control")
                rec = gw._ATTEMPTS.get(tid)
                self.assertEqual(self.controls(tid)[0].name, f"{rec.get('fence_ctl') or rec.get('stop_ctl')}.txt")
                self.assertIsNone(rec.get("owed_ctl"))

    def test_a_closed_task_taken_up_is_stopped_after_a_storage_outage(self):
        tid = "task-owed-closed"
        self.assertIsNotNone(gw._write_task(_task(tid)))
        seed_ack(tid, _ack(tid, 1))
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.claimed-core1")
        with self.no_ctl_write():
            self.beat_error(tid, "task_already_terminal")
        self.assertEqual(self.controls(tid), [])
        self.assertEqual(gw._ATTEMPTS.get(tid)["fenced"], "task_already_terminal")
        self.beat_error(tid, "task_already_terminal")
        self.assertEqual(len(self.controls(tid)), 1, "the owed cancel is written once storage is back")
        self.beat_error(tid, "task_already_terminal")
        self.assertEqual(len(self.controls(tid)), 1)

    def test_a_failure_result_owed_through_a_storage_outage_is_written_once(self):
        tid = "task-owed-lost"
        _, clock = self.held_then_archived(tid)
        self.beat(tid)
        clock[0] += gw.task_attempts.DISPATCH_LOST_S
        with mock.patch.object(gw, "_write_if_absent", return_value=False), \
                self.failing_save("relay-task-attempts-v2"):
            self.beat(tid)
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists())
        with mock.patch.object(gw, "_write_if_absent", return_value=False):
            self.assertFalse(self.beat(tid), "recorded as lost, its result owed")
            with self.failing_save("relay-task-attempts-v2"):
                self.beat(tid)
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists())
        self.assertFalse(self.beat(tid))
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.DISPATCH_LOST_RESULT)
        self.beat(tid)
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.DISPATCH_LOST_RESULT)

    def test_a_dispatch_lost_that_cannot_be_recorded_writes_no_failure(self):
        tid = "task-lost-unrec"
        seed_ack(tid, _ack(tid, 1))
        with self.failing_save("relay-task-attempts-v2"), txn(tid) as t:
            gw._fail_unanswered(t, tid, "failed", "test")
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "retried next beat")
        self.assertFalse(gw._ATTEMPTS.get(tid).get("dispatch_lost"))
        with txn(tid) as t:
            gw._fail_unanswered(t, tid, "failed", "test")
        self.assertTrue((gw.RESULTS_DIR / f"{tid}.txt").exists())
        self.assertTrue(gw._ATTEMPTS.get(tid)["dispatch_lost"])

    def owed_failure(self, tid, lease=None):
        """An attempt failed as lost whose failure result could not be written yet."""
        fake, clock = self.held_then_archived(tid, lease)
        self.beat(tid)
        clock[0] += gw.task_attempts.DISPATCH_LOST_S
        with mock.patch.object(gw, "_write_if_absent", return_value=False):
            self.assertFalse(self.beat(tid))
        self.assertTrue(gw._ATTEMPTS.get(tid).get("dispatch_lost"))
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists())
        return fake

    def ledger_fails_once_stopped(self, tid):
        """The ledger write that records `tid`'s Stop lands; every ledger write after it fails."""
        real, landed = gw._ATTEMPTS._write, [False]

        def write(data):
            if landed[0]:
                raise OSError("disk full")
            landed[0] = bool(((data.get("tasks") or {}).get(tid) or {}).get("stop_requested"))
            return real(data)
        return mock.patch.object(gw._ATTEMPTS, "_write", side_effect=write)

    def seed(self, tid, rec):
        """A record written straight into the ledger, as a crash would leave it."""
        data = ledger()
        data.setdefault("tasks", {})[tid] = rec
        write_ledger(data)

    def undamage(self, tid):
        data = ledger()
        data.get("tasks", {}).pop(tid, None)
        write_ledger(data)

    def test_owed_work_keeps_its_task_in_flight_until_it_is_done(self):
        tid = "task-owed-held"
        fake = self.owed_failure(tid)
        old = time.time() - 2 * gw.ARCHIVE_COMPLETION_GRACE_S
        os.utime(gw.TASKS_DIR / "archive" / f"{tid}.txt", (old, old))
        inflight = {tid}
        gw._reconcile_abandoned(inflight, gw._reconcile_abandoned(inflight, set()))
        self.assertEqual(inflight, {tid}, "a record that owes a result is not abandoned")
        gw._post_task_beats(inflight, force=True)
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.DISPATCH_LOST_RESULT)
        gw._post_ready_results(inflight)
        post = fake.posted("/v1/results")
        self.assertEqual([(p["status"], p["no_send"]) for p in post], [("failed", True)])

    def test_owed_work_is_settled_from_the_ledger_when_its_id_left_flight(self):
        tid = "task-owed-adopted"
        fake = self.owed_failure(tid)
        inflight: set = set()
        gw._post_task_beats(inflight, force=True)
        self.assertIn(tid, inflight, "the ledger, not the in-flight set, owns the retry")
        self.assertIn(tid, json.loads(gw.INFLIGHT_FILE.read_text()))
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.DISPATCH_LOST_RESULT)
        gw._post_ready_results(inflight)
        self.assertEqual([(p.get("status"), p.get("no_send")) for p in fake.posted("/v1/results")
                          if p["id"] == tid], [("failed", True)])
        self.assertNotIn(tid, gw._ATTEMPTS.owing())

    def test_an_answer_the_core_writes_before_the_owed_failure_posts_as_its_answer(self):
        tid = "task-owed-raced"
        fake = self.owed_failure(tid)
        (gw.RESULTS_DIR / f"{tid}.txt").write_text("the real answer")
        gw._post_task_beats({tid}, force=True)
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), "the real answer")
        gw._post_ready_results({tid})
        post = fake.posted("/v1/results")
        self.assertEqual(len(post), 1)
        self.assertEqual((post[0]["body"], post[0].get("status"), post[0].get("no_send")),
                         ("the real answer", "completed", None), "posted as the core's answer, not as a failure")

    def test_an_unreadable_in_flight_set_at_start_deletes_no_owed_work(self):
        tid = "task-owed-restart"
        self.assertIsNotNone(gw._write_task(_task(tid)))
        seed_ack(tid, _ack(tid, 1))
        with self.no_ctl_write():
            self.beat_error(tid, "stale_execution_generation")
        self.assertEqual(self.controls(tid), [])
        saved = gw.INFLIGHT_FILE.read_bytes() if gw.INFLIGHT_FILE.exists() else None
        self.addCleanup(lambda: gw.INFLIGHT_FILE.write_bytes(saved) if saved is not None
                        else gw.INFLIGHT_FILE.unlink(missing_ok=True))
        mock.patch.object(gw, "_INFLIGHT_DEGRADED", False).start()
        gw.INFLIGHT_FILE.write_text("{not json")
        inflight = gw._load_inflight()
        self.assertTrue(gw._INFLIGHT_DEGRADED)
        gw._close_stale_turns(inflight)
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual(rec.get("fenced"), "stale_execution_generation", "the record is kept")
        self.assertEqual(len(self.controls(tid)), 1, "its owed cancel is written at start")
        self.assertIn(tid, inflight)
        gw._post_task_beats(inflight, force=True)
        self.assertEqual(len(self.controls(tid)), 1, "exactly once")

    def test_a_stop_whose_control_and_rollback_both_fail_still_stops_the_core(self):
        tid = "task-stop-double"
        seed_ack(tid, _ack(tid, 1))
        with self.no_ctl_write(), self.ledger_fails_once_stopped(tid):
            self.beat_error(tid, "task_cancelled")
        self.assertTrue(gw._ATTEMPTS.get(tid).get("stop_requested"), "the Stop itself was recorded")
        self.assertEqual(self.controls(tid), [], "its control write failed, and every write after it")
        self.beat_error(tid, "task_cancelled")
        self.beat_error(tid, "task_cancelled")
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual([p.name for p in self.controls(tid)], [f"{rec['stop_ctl']}.txt"],
                         "storage back: the core is asked to stop, once")

    # ── round 11: one writer per transition, unknown is held, startup order ──
    def all_controls(self, tid):
        return [p for p in gw.TASKS_DIR.glob("task-ctl-*") if f"stop processing {tid} " in p.read_text()]

    def keep_inflight_file(self, content):
        saved = gw.INFLIGHT_FILE.read_bytes() if gw.INFLIGHT_FILE.exists() else None
        self.addCleanup(lambda: gw.INFLIGHT_FILE.write_bytes(saved) if saved is not None
                        else gw.INFLIGHT_FILE.unlink(missing_ok=True))
        gw.INFLIGHT_FILE.write_text(content)
        mock.patch.object(gw, "_INFLIGHT_DEGRADED", False).start()

    def paused(self, name):
        """Patch `gw.<name>` to stop at its first call until released; (reached, release)."""
        real, reached, release = getattr(gw, name), threading.Event(), threading.Event()

        def stop(*a, **k):
            if not reached.is_set():
                reached.set()
                release.wait(10)
            return real(*a, **k)
        mock.patch.object(gw, name, side_effect=stop).start()
        return reached, release

    def test_a_cancel_owed_by_a_refused_serve_stops_only_its_own_run(self):
        tid = "task-preack-fence"
        pending: list = []
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), pending))
        self.assertTrue(gw._record_pending_acks(pending))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(409, {
            "error": "not leased to you", "reason": "stale lease generation",
            "error_code": "stale_execution_generation"})}))
        self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        self.assertEqual(len(self.controls(tid)), 1, "the refused lease's run is asked to stop")
        inflight = {tid}
        self.assertEqual(gw._write_task(_task(tid, lease_id="L2")), (tid, True), "served again: held")
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2), "/v1/tasks/heartbeat": {"ok": True}}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["run"], rec["execution_generation"], rec.get("fenced")), (f"{tid}~~2", 2, None),
                         "a stopped run is never adopted: the generation gets its own")
        gw._post_task_beats(inflight, force=True)
        self.assertTrue((gw.TASKS_DIR / f"{tid}~~2.txt").exists())
        self.assertEqual(len(self.controls(tid)), 1, "the earlier run's cancel stands")
        self.assertEqual(self.all_controls(f"{tid}~~2"), [], "and never names the newer run")
        del fake

    def test_owed_work_is_settled_only_once_its_task_is_held_in_flight_on_disk(self):
        tid = "task-owed-unheld"
        self.held_then_archived(tid)
        with self.no_ctl_write():
            self.beat_error(tid, "stale_execution_generation")
        self.keep_inflight_file("[]")
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True}}))
        inflight: set = set()
        with mock.patch.object(gw, "_save_inflight", return_value=False):
            gw._post_task_beats(inflight, force=True)
        self.assertNotIn(tid, inflight)
        self.assertTrue(gw._ATTEMPTS.get(tid).get("owed_ctl"), "not settled while nothing durable holds it")
        self.assertEqual(self.controls(tid), [])
        inflight = gw._load_inflight()                                          # restart
        gw._close_stale_turns(inflight)
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual(rec.get("fenced"), "stale_execution_generation", "the record survives the restart")
        self.assertEqual([p.name for p in self.controls(tid)], [f"{rec['fence_ctl']}.txt"])

    def test_an_unreadable_attempt_ledger_abandons_nothing(self):
        tid = "task-held-unknown"
        self.held_then_archived(tid)
        old = time.time() - 2 * gw.ARCHIVE_COMPLETION_GRACE_S
        os.utime(gw.TASKS_DIR / "archive" / f"{tid}.txt", (old, old))
        saved, broken = gw._ATTEMPTS.path.read_bytes(), [True]
        self.addCleanup(lambda: broken[0] and gw._ATTEMPTS.path.write_bytes(saved))
        gw._ATTEMPTS.path.write_text("{not json")
        inflight = {tid}
        gw._reconcile_abandoned(inflight, gw._reconcile_abandoned(inflight, set()))
        self.assertEqual(inflight, {tid}, "unknown is held, not abandoned")
        gw._ATTEMPTS.path.write_bytes(saved)
        broken[0] = False
        gw._close_stale_turns(inflight)
        self.assertEqual(gw._ATTEMPTS.get(tid).get("execution_generation"), 1)

    def test_an_unreadable_in_flight_set_is_rebuilt_whole_never_from_the_debts_alone(self):
        other, owed = "task-unrelated-live", "task-owed-degraded"
        self.held_then_archived(other)                  # acked, its task taken; its answer not written yet
        self.held_then_archived(owed)
        with self.no_ctl_write():
            self.beat_error(owed, "stale_execution_generation")
        self.keep_inflight_file("{not json")
        inflight = gw._load_inflight()
        self.assertTrue(gw._INFLIGHT_DEGRADED)
        gw._close_stale_turns(inflight)
        mock.patch.object(gw, "_INFLIGHT_DEGRADED", False).start()
        inflight = gw._load_inflight()                                          # the next restart
        self.assertIn(other, inflight)
        gw._close_stale_turns(inflight)
        self.assertEqual(gw._ATTEMPTS.get(other).get("execution_generation"), 1, "the live attempt is kept")

    def test_a_wake_result_live_at_restart_posts_under_its_own_item(self):
        tid = "task-wake-restart"
        task = {"id": tid, "task": "hi", "source": "ag2space", "user_id": "@o:x"}
        gens = iter((1, 2))
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True},
                                       f"/v1/tasks/{tid}/ack": lambda p: _ack(tid, next(gens))}))
        inflight: set = set()
        self.assertTrue(gw._take_served_task(dict(task), inflight, []))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        self.ran(tid)
        (gw.RESULTS_DIR / f"{tid}.txt").write_text("I asked.")
        gw._post_ready_results(inflight)
        wake = {"blocker_episode_id": "e1", "resolved_blockers": [
            {"question_id": "q1", "outcome": "answered", "answer": {"kind": "text", "text": "b"}}]}
        run = f"{tid}~~2"
        self.assertEqual(gw._write_task({**task, "wake": wake}), (run, True))   # its in-flight save is lost
        self.assertTrue(gw._post_task_ack(run, durable=True))
        self.ran(run)
        (gw.RESULTS_DIR / f"{run}.txt").write_text("went with b")
        inflight = set()                                                        # restart
        gw._close_stale_turns(inflight)
        gw._post_ready_results(inflight)
        gw._reconcile_orphan_results(inflight)
        bodies = [p["body"] for p in fake.posted("/v1/results") if p["id"] == tid]
        self.assertEqual((len(bodies), "went with b" in bodies[-1]), (2, True), "the resumed answer is posted")
        root = gw._delivery_core().backend.root
        self.assertEqual((gw.read_item(root, f"{tid}.wake-e1") or {}).get("status"), "DELIVERED")
        self.assertFalse(any((gw.RESULTS_DIR / "undelivered").glob(f"*{tid}*")), "never quarantined")
        self.assertNotIn(run, inflight)

    def test_an_old_generations_result_never_retires_the_newer_without_turn_context(self):
        tid = "task-finish-regen"
        mock.patch.object(gw, "TURN_CONTEXT", False).start()
        mock.patch.object(gw._ATTEMPTS, "turn_context", False).start()
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        self.ran(tid)
        entered, gate = threading.Event(), threading.Event()

        def result(payload):
            entered.set()
            gate.wait(10)
            return {"ok": True}
        self.use(FakeGateway(**{"/v1/results": result, f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        drain = threading.Thread(target=gw._post_ready_results, args=({tid},))
        drain.start()
        self.assertTrue(entered.wait(5), "generation 1's result is being posted")
        self.assertEqual(gw._write_task(_task(tid, lease_id="L2")), (tid, True))      # generation 2 meanwhile
        acked = []
        acker = threading.Thread(target=lambda: acked.append(gw._post_task_ack(tid, durable=True, lease_id="L2")))
        acker.start()
        acker.join(1)
        self.assertEqual(acked, [False], "generation 2 waits while generation 1's send may be under way")
        gate.set()
        drain.join(10)
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec.get("finished"), rec["pending_serve"]["lease"]), (True, "L2"),
                         "generation 1 settled; generation 2 is still held, not erased")
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        self.assertEqual((gw._ATTEMPTS.get(tid)["run"], gw._ATTEMPTS.get(tid)["execution_generation"]),
                         (f"{tid}~~2", 2), "generation 2 runs as its own run")

    def test_a_core_answer_in_the_failure_results_own_words_is_the_cores(self):
        tid = "task-owed-collision"
        fake = self.owed_failure(tid)
        (gw.RESULTS_DIR / f"{tid}.txt").write_text(gw.DISPATCH_LOST_RESULT)   # the core wins, same words
        gw._post_task_beats({tid}, force=True)
        gw._post_ready_results({tid})
        post = [p for p in fake.posted("/v1/results") if p["id"] == tid]
        self.assertEqual(len(post), 1)
        self.assertNotEqual(post[0].get("status"), "failed", "the bridge did not write this file")

    def test_a_core_answer_that_replaced_the_failure_survives_a_newer_generation(self):
        tid = "task-owed-replaced"
        self.owed_failure(tid)
        gw._post_task_beats({tid}, force=True)
        tmp = gw.RESULTS_DIR / f".{tid}.core"
        tmp.write_text("the core's answer")
        os.replace(tmp, gw.RESULTS_DIR / f"{tid}.txt")                        # the core's own file, same name
        (gw.TASKS_DIR / "archive" / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.txt")
        self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), "the core's answer",
                         "only the file the bridge created is withdrawn")

    def test_a_start_that_cannot_read_or_hold_its_attempts_retires_none(self):
        tid = "task-start-unknown"
        self.held_then_archived(tid)
        (gw.TASKS_DIR / "archive" / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.txt")
        saved = gw._ATTEMPTS.path.read_bytes()
        self.addCleanup(lambda: gw._ATTEMPTS.path.write_bytes(saved))
        gw._ATTEMPTS.path.write_text("{not json")
        gw._close_stale_turns(set())
        self.assertEqual(gw._ATTEMPTS.path.read_text(), "{not json", "an unreadable ledger is not touched")
        self.assertTrue(any("nothing settled or retired at start" in m for m in self.logs))
        gw._ATTEMPTS.path.write_bytes(saved)
        with mock.patch.object(gw, "_save_inflight", return_value=False), \
                mock.patch.object(gw._ATTEMPTS, "sweep") as sweep:
            gw._close_stale_turns(set())
        sweep.assert_not_called()
        self.assertEqual(gw._ATTEMPTS.get(tid).get("execution_generation"), 1)

    # ── round 12: the file ruled is the file published; serves, beats and start are transitions ──
    def test_a_result_file_that_moves_after_the_drain_read_it_is_never_published(self):
        for race in ("withdrawn by a newer generation",):
            with self.subTest(race=race):
                tid = f"task-drain-race-{len(race)}"
                fake = self.owed_failure(tid)
                gw._post_task_beats({tid}, force=True)
                self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.DISPATCH_LOST_RESULT)
                fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2), "/v1/results": {"ok": True}}))
                reached, release = self.paused("_relay_result")            # read done, ruling not begun
                drain = threading.Thread(target=gw._post_ready_results, args=({tid},))
                drain.start()
                self.assertTrue(reached.wait(5))
                if race.startswith("withdrawn"):
                    self.assertEqual(gw._write_task(_task(tid, lease_id="L2")), (tid, True))
                    self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L2"),
                                     "the earlier run's claimed result may still be sent: the switch waits")
                else:
                    tmp = gw.RESULTS_DIR / f".{tid}.core"
                    tmp.write_text("the core's answer")
                    os.replace(tmp, gw.RESULTS_DIR / f"{tid}.txt")
                release.set()
                drain.join(10)
                self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"), "held, it let go: the switch goes")
                self.assertEqual(gw._ATTEMPTS.get(tid)["run"], f"{tid}~~2")
                mock.patch.object(gw, "_relay_result", side_effect=None).stop()
                mock.patch.stopall()
                mock.patch.object(gw, "_log", self.logs.append).start()
                self.use(fake)
                self.assertEqual([p for p in fake.posted("/v1/results") if p["id"] == tid], [],
                                 "the bytes it read are gone: nothing of them is published")
                gw._post_ready_results({tid})
                posts = [(p["body"], p.get("status")) for p in fake.posted("/v1/results") if p["id"] == tid]
                self.assertEqual(posts, [], "the earlier run's failure is never posted for the newer run")
                self.assertEqual(len(list((gw.RESULTS_DIR / "undelivered").glob(f"{tid}.*"))), 1, "quarantined")

    def test_a_serve_waits_for_a_settlement_already_under_way(self):
        tid = "task-serve-vs-settle"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        seed_ack(tid, _ack(tid, 1))
        with self.no_ctl_write():
            self.beat_error(tid, "stale_execution_generation")
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True}}))
        reached, release = self.paused("_queue_cancel_instruction")
        settler = threading.Thread(target=gw._post_task_beats, args=({tid},), kwargs={"force": True})
        settler.start()
        self.assertTrue(reached.wait(5), "the settlement read generation 1's owed cancel")
        served = threading.Thread(target=gw._write_task, args=(_task(tid, lease_id="L2"),))
        served.start()
        served.join(0.3)
        blocked = served.is_alive()
        release.set()
        settler.join(10)
        served.join(10)
        self.assertTrue(blocked, "the re-serve waits until the settlement's transition is done")
        self.assertEqual(gw._ATTEMPTS.get(tid)["pending_serve"]["lease"], "L2", "held, nothing published")
        mock.patch.stopall()
        mock.patch.object(gw, "_log", self.logs.append).start()
        self.assertTrue(gw._record_pending_acks([(tid, True, "L2")]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        self.assertEqual(len(self.all_controls(tid)), 1, "generation 1's run is stopped by its own cancel")
        self.assertEqual(self.all_controls(f"{tid}~~2"), [], "which never names generation 2's run")

    def test_a_beat_answered_for_an_older_generation_never_fences_the_newer(self):
        tid = "task-beat-regen"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        seed_ack(tid, _ack(tid, 1))
        entered, gate = threading.Event(), threading.Event()

        def beat(payload):
            entered.set()
            gate.wait(10)
            return {"ok": True, "task_errors": [{"task_id": tid, "error_code": "task_cancelled"}]}
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": beat, f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        beating = threading.Thread(target=gw._post_task_beats, args=({tid},), kwargs={"force": True})
        beating.start()
        self.assertTrue(entered.wait(5), "generation 1's beat is on the wire")
        self.assertEqual(gw._write_task(_task(tid, lease_id="L2")), (tid, True))      # generation 2 meanwhile
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        gate.set()
        beating.join(10)
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["run"], rec["execution_generation"], rec.get("stop_requested"), rec.get("fenced")),
                         (f"{tid}~~2", 2, None, None))
        self.assertEqual(self.all_controls(f"{tid}~~2"), [], "generation 1's Stop never reaches generation 2")

    def test_a_start_that_cannot_read_a_live_result_keeps_its_wake_identity(self):
        tid = "task-wake-unreadable"
        task = {"id": tid, "task": "hi", "source": "ag2space", "user_id": "@o:x"}
        run = f"{tid}~~2"
        self.answered = self.answered_task(tid)
        self.assertEqual(gw._write_task(self.answered), (run, True))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        self.assertTrue(gw._post_task_ack(run, durable=True))
        self.ran(run)
        (gw.RESULTS_DIR / f"{run}.txt").write_text("went with b")
        mode = gw.RESULTS_DIR.stat().st_mode
        self.addCleanup(os.chmod, gw.RESULTS_DIR, mode)
        os.chmod(gw.RESULTS_DIR, 0)
        try:
            gw._close_stale_turns(set())
        finally:
            os.chmod(gw.RESULTS_DIR, mode)
        self.assertEqual(gw._ATTEMPTS.wake_item(tid), f"{tid}.wake-e1", "unknown is not absent")
        self.assertTrue(any("nothing settled or retired at start" in m for m in self.logs))
        del task

    def test_a_wake_left_unacked_never_receives_the_earlier_attempts_failure(self):
        tid = "task-wake-owed-failure"
        self.owed_failure(tid)                                  # generation 1 owes its failure result
        wake = {"id": tid, "task": "hi", "source": "ag2space", "user_id": "@o:x",
                "wake": {"blocker_episode_id": "e1", "resolved_blockers": []}}
        with mock.patch.object(gw.task_attempts.Transition, "remember_task", return_value=False):
            self.assertIsNone(gw._write_task(wake), "opened, but its serve was not recorded: left unacked")
        self.assertEqual(gw._ATTEMPTS.get(tid).get("wake_episode"), "e1")
        gw._post_task_beats({tid}, force=True)
        self.assertEqual(list(gw.RESULTS_DIR.glob(f"{tid}*.txt")), [], "generation 1's debt is not the wake's")

    # ── round 12, independent review: acks and refusals for older leases, unknown entries ──
    def test_a_late_ack_for_an_older_serve_or_a_retired_attempt_moves_nothing(self):
        tid = "task-late-ack"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        self.assertIsNotNone(gw._write_task(_task(tid, lease_id="L2")))           # served again: held
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))    # its own generation: adopted
        turn = gw._ATTEMPTS.get(tid)["turn_id"]
        late = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))   # L1's ack, late
        self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        self.assertEqual(late.posted("/ack"), [], "an ack for a serve the attempt no longer holds is not sent")
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["lease_id"], rec["turn_id"]), ("L2", turn), "the lease moved on and stays")
        self.assertFalse(agent_turn.is_closed(gw._STATE, turn))
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        self.ran(tid)
        gw._post_ready_results({tid})
        self.assertEqual(gw._ATTEMPTS.get(tid), {}, "settled and retired")
        self.assertTrue(gw._record_pending_acks([(tid, True, "L2")]))
        self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        self.assertEqual(gw._ATTEMPTS.get(tid), {}, "a late ack does not bring a retired attempt back")
        self.assertNotIn(tid, gw._load_pending_acks())
        self.assertEqual(len(fake.posted("/v1/results")), 1)

    def test_an_ack_refusal_about_an_older_lease_never_fences_the_newer_generation(self):
        tid = "task-ack-refused-old"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        self.assertIsNotNone(gw._write_task(_task(tid, lease_id="L2")))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))    # the lease moves onto the run
        self.assertTrue(gw._record_pending_acks([(tid, True, "L1")]))
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(409, {
            "error_code": "task_cancelled", "cancel_requested_generation": 1})}))
        gw._post_task_ack(tid, durable=True, lease_id="L1")
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["lease_id"], rec.get("fenced")), ("L2", None))
        self.assertTrue((gw.TASKS_DIR / f"{tid}.txt").exists(), "the run is not withdrawn")
        self.assertEqual(fake.posted("/cancel-ack"), [])
        self.assertNotIn(tid, gw._load_pending_acks())

    def test_a_beat_fence_while_a_newer_serve_awaits_its_ack_is_left_to_that_ack(self):
        tid = "task-beat-awaiting"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        seed_ack(tid, _ack(tid, 1))
        self.assertIsNotNone(gw._write_task(_task(tid, lease_id="L2")))           # served again: held
        self.beat_error(tid, "stale_execution_generation")                        # generation 1 is over
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec.get("fenced"), rec["pending_serve"]["lease"]), ("stale_execution_generation", "L2"))
        self.assertEqual(len(self.all_controls(tid)), 1, "generation 1's run is asked to stop")
        self.assertTrue(gw._record_pending_acks([(tid, True, "L2")]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["run"], rec["execution_generation"], rec.get("fenced")), (f"{tid}~~2", 2, None))
        self.assertEqual(self.all_controls(f"{tid}~~2"), [], "generation 2 runs as its own run, never stopped")

    def test_a_ledger_that_turns_unreadable_mid_delivery_holds_the_result(self):
        tid = "task-ledger-mid-delivery"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        seed_ack(tid, _ack(tid, 1))
        self.ran(tid)
        saved = []
        self.addCleanup(lambda: saved and gw._ATTEMPTS.path.write_bytes(saved[0]))

        def result(_payload):
            saved.append(gw._ATTEMPTS.path.read_bytes())
            gw._ATTEMPTS.path.write_text("{not json")                # the ledger breaks after the claim
            return {"ok": True}
        fake = self.use(FakeGateway(**{"/v1/results": result}))
        inflight = {tid}
        gw._post_ready_results(inflight)
        self.assertEqual(len(fake.posted("/v1/results")), 1, "the claimed post went")
        self.assertTrue((gw.RESULTS_DIR / f"{tid}.txt").exists(), "held: its commit cannot be read")
        self.assertIn(tid, inflight)
        gw._post_ready_results(inflight)
        self.assertEqual(len(fake.posted("/v1/results")), 1, "never posted again while unknown")
        gw._ATTEMPTS.path.write_bytes(saved.pop())
        gw._post_ready_results(inflight)
        self.assertEqual(len(fake.posted("/v1/results")), 1, "the recovered pass commits, never re-sends")
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists())
        self.assertNotIn(tid, inflight)

    def test_a_damaged_ledger_entry_is_kept_and_held(self):
        tid = "task-entry-damaged"
        self.seed(tid, "damaged")
        self.addCleanup(self.undamage, tid)
        self.assertTrue(gw._broker_holds(tid), "an entry that is not a record is unknown")
        gw._close_stale_turns(set())
        self.assertEqual(ledger()["tasks"].get(tid), "damaged", "never swept")

    def test_the_earlier_runs_cancel_never_names_the_wakes_run(self):
        tid = "task-wake-retracts"
        wake = self.answered_task(tid)
        seed_ack(tid, _ack(tid, 1))
        self.beat_error(tid, "stale_execution_generation")
        self.assertEqual(len(self.controls(tid)), 1, "the earlier run is asked to stop")
        self.assertEqual(gw._write_task(wake), (f"{tid}~~2", True))
        self.assertEqual(self.all_controls(f"{tid}~~2"), [], "its cancel names only that run, never the wake's")

    def test_a_start_counts_a_claimed_task_live_and_an_unreadable_queue_unknown(self):
        for state in ("claimed", "queue unreadable"):
            with self.subTest(state=state):
                tid = f"task-start-{len(state)}"
                self.assertTrue(gw._take_served_task(_task(tid), set(), []))
                seed_ack(tid, _ack(tid, 1))
                claimed = gw.TASKS_DIR / f"{tid}.claimed-core1"
                (gw.TASKS_DIR / f"{tid}.txt").rename(claimed)
                self.addCleanup(lambda c=claimed: c.unlink(missing_ok=True))
                mode = gw.TASKS_DIR.stat().st_mode
                self.addCleanup(os.chmod, gw.TASKS_DIR, mode)
                if state == "queue unreadable":
                    os.chmod(gw.TASKS_DIR, 0)
                try:
                    inflight: set = set()
                    gw._close_stale_turns(inflight)
                finally:
                    os.chmod(gw.TASKS_DIR, mode)
                self.assertEqual(gw._ATTEMPTS.get(tid).get("execution_generation"), 1, "kept through the start")
                self.assertEqual(tid in inflight, state == "claimed")

    def test_an_ack_fence_that_cannot_be_recorded_keeps_its_pending_ack(self):
        tid = "task-ack-fence-unsaved"
        pending: list = []
        self.assertTrue(gw._take_served_task(_task(tid), set(), pending))
        self.assertTrue(gw._record_pending_acks(pending))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(409, {"error_code": "task_already_terminal"})}))
        with mock.patch.object(gw.task_attempts.Transition, "fence",
                               return_value={"new": False, "cancel": False, "unsaved": True}):
            gw._post_task_ack(tid, durable=True)
        self.assertIn(tid, gw._load_pending_acks(), "retried, so the fence is heard again")

    def test_a_legacy_ack_whose_wait_cannot_be_ended_keeps_its_pending_ack(self):
        tid = "task-legacy-ack-unsaved"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": {"ok": True, "durable": True}}))   # no generation
        with failing_commit("commit of ack-"):
            self.assertFalse(gw._post_task_ack(tid, durable=True))
        self.assertIn(tid, gw._load_pending_acks())

    def test_a_stop_is_not_judged_while_the_wake_records_cannot_be_read(self):
        tid = "task-stop-wake-unknown"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        seed_ack(tid, _ack(tid, 1))
        self.beat_error(tid, "task_cancelled")
        ctl = gw._ATTEMPTS.get(tid)["stop_ctl"]
        (gw.TASKS_DIR / f"{ctl}.txt").unlink()
        (gw.RESULTS_DIR / f"{ctl}.txt").write_text("[no-send]")               # the core ran the Stop
        self.addCleanup(lambda: (gw.RESULTS_DIR / f"{ctl}.txt").unlink(missing_ok=True))
        saved = gw._ATTEMPTS.path.read_bytes()
        self.addCleanup(lambda: gw._ATTEMPTS.path.write_bytes(saved))
        fake = self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True}}))
        with mock.patch.object(gw._ATTEMPTS, "run_item", return_value=gw.task_attempts.UNKNOWN):
            gw._post_task_beats({tid}, force=True)
        self.assertEqual(fake.posted("/cancel-ack"), [], "unknown is never read as stopped with no reply")

    def test_an_ack_waits_for_a_beat_check_already_under_way(self):
        tid = "task-beat-check-vs-ack"
        self.held_then_archived(tid)
        self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1), "/v1/tasks/heartbeat": {"ok": True}}))
        real, reached, release = gw.task_attempts.last_activity, threading.Event(), threading.Event()

        def activity(state, task_id):
            if task_id == tid:                  # another task's transition never holds this one
                reached.set()
                release.wait(10)
            return real(state, task_id)
        with mock.patch.object(gw.task_attempts, "last_activity", side_effect=activity):
            beat = threading.Thread(target=gw._post_task_beats, args=({tid},), kwargs={"force": True})
            beat.start()
            self.assertTrue(reached.wait(5))
            acker = threading.Thread(target=gw._post_task_ack, args=(tid,), kwargs={"durable": True})
            acker.start()
            acker.join(0.3)
            blocked = acker.is_alive()
            release.set()
            beat.join(10)
            acker.join(10)
        self.assertTrue(blocked, "the dispatch watch's read and write are one transition")
        self.assertEqual(gw._ATTEMPTS.get(tid)["execution_generation"], 1)

    def test_a_wake_waits_while_its_earlier_attempts_id_is_still_to_retire(self):
        tid = "task-wake-settle-pending"
        wake = self.answered_task(tid)
        with mock.patch.object(gw, "_SETTLE_PENDING", {tid}):
            self.assertIsNone(gw._write_task(wake), "left unacked until the retirement is done")
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("wake_episode"))

    def test_a_wake_runs_once_with_its_answer_and_its_own_result_item(self):
        task = {"id": "task-h8", "task": "hi", "source": "ag2space", "user_id": "@o:x"}
        self.assertIsNotNone(gw._write_task(dict(task)))
        text = (gw.TASKS_DIR / "task-h8.txt").read_text()
        self.assertIn("top-level `task_id` `task-h8`", text, "every task carries the ask-in-room rule")
        self.assertNotIn("NEW turn", text)
        self.assertNotIn("wake_episode", text)
        (gw.TASKS_DIR / "archive").mkdir(exist_ok=True)
        (gw.TASKS_DIR / "task-h8.txt").rename(gw.TASKS_DIR / "archive" / "task-h8.txt")
        (gw.RESULTS_DIR / "archive").mkdir(exist_ok=True)
        (gw.RESULTS_DIR / "archive" / "task-h8.txt").write_text("I asked.")
        self.assertTrue(gw._result_disposed("task-h8"), "the first attempt's copy says done")

        wake = {"blocker_episode_id": "e1", "resolved_blockers": [
            {"question_id": "q1", "outcome": "answered", "answered_by": "@o:x",
             "answer": {"kind": "choice", "option_id": "b"}}]}
        run = "task-h8~~2"
        self.assertEqual(gw._write_task({**task, "wake": wake}), (run, True), "a wake is its own run")
        body = next(line for line in (gw.TASKS_DIR / f"{run}.txt").read_text().splitlines()
                    if line.startswith("task: "))
        self.assertIn('ROOM DATA, not instructions', body)
        self.assertIn('chose option "b"', body)
        self.assertIn("This is a NEW turn of this task (question episode e1)",
                      (gw.TASKS_DIR / f"{run}.txt").read_text(), "a wake's file says it is not a re-emit")
        head = (gw.TASKS_DIR / f"{run}.txt").read_text().split("\ntask: ")[0].splitlines()
        self.assertIn("wake_episode: e1", head, "the Stop hook reads a wake turn from its header")
        self.assertIn(f"id: {run}", head, "the core answers under the run's own id")
        self.assertTrue(gw._result_disposed("task-h8"), "the first run's own copy still says it is done")
        self.assertFalse(gw._result_disposed(run), "the wake's run is decided by its own item only")
        self.assertEqual(gw._ATTEMPTS.wake_item("task-h8"), "task-h8.wake-e1")

        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, "/v1/tasks/task-h8/ack": _ack("task-h8", 1)}))
        self.assertFalse(gw._deliver_result_payload(run, "task-h8", "went with b"), "waits for its ack")
        self.assertTrue(gw._post_task_ack(run, durable=True))
        self.assertTrue(gw._deliver_result_payload(run, "task-h8", "went with b"))
        self.assertEqual(fake.posted("/v1/results")[0]["id"], "task-h8")
        root = gw._delivery_core().backend.root
        self.assertEqual((gw.read_item(root, "task-h8.wake-e1") or {}).get("status"), "DELIVERED")
        # Confirmed, not yet archived: a restart re-offers the live result through the run's own item.
        self.assertEqual(gw._ATTEMPTS.run_item(run), "task-h8.wake-e1", "kept with its run")
        self.assertTrue(gw._result_disposed(run))
        posted = len(fake.posted("/v1/results"))
        self.assertTrue(gw._deliver_result_payload(run, "task-h8", "went with b"))
        self.assertEqual(len(fake.posted("/v1/results")), posted, "already delivered: not posted again")
        self.assertEqual((gw.read_item(root, "task-h8.wake-e1") or {}).get("status"), "DELIVERED")
        self.assertFalse(any((gw.RESULTS_DIR / "undelivered").glob("*task-h8*")), "never quarantined")
        gw._ATTEMPTS.sweep(set())
        self.assertIsNone(gw._ATTEMPTS.wake_item("task-h8"), "forgotten once the task left flight")

        (gw.TASKS_DIR / f"{run}.txt").rename(gw.TASKS_DIR / "archive" / f"{run}.txt")
        self.assertIsNone(gw._write_task({**task, "wake": wake}), "a wake already run is not run again")
        self.assertIsNone(gw._write_task({**task, "wake": wake}))
        self.assertEqual(sum("wake for task-h8: not run here" in m for m in self.logs), 1,
                         "logged once per episode, not per re-serve")

    def test_a_wake_for_a_task_stopped_meanwhile_is_withdrawn_unrun(self):
        task = {"id": "task-h8c", "task": "hi", "source": "ag2space", "user_id": "@o:x"}
        self.assertIsNotNone(gw._write_task(dict(task)))
        (gw.TASKS_DIR / "archive").mkdir(exist_ok=True)
        (gw.TASKS_DIR / "task-h8c.txt").rename(gw.TASKS_DIR / "archive" / "task-h8c.txt")
        (gw.RESULTS_DIR / "archive").mkdir(exist_ok=True)
        (gw.RESULTS_DIR / "archive" / "task-h8c.txt").write_text("I asked.")
        wake = {"blocker_episode_id": "e1", "resolved_blockers": [
            {"question_id": "q1", "outcome": "answered", "answer": {"kind": "text", "text": "yes"}}]}
        run = "task-h8c~~2"
        self.assertEqual(gw._write_task({**task, "wake": wake}), (run, True))
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True},
                                       "/v1/tasks/task-h8c/ack": _http(409, {"error_code": "task_cancelled"})}))
        self.assertFalse(gw._post_task_ack(run, durable=True))
        self.assertFalse((gw.TASKS_DIR / f"{run}.txt").exists(), "the core never runs the wake")
        self.assertEqual(self.all_controls("task-h8c") + self.all_controls(run), [])
        self.assertTrue(gw._deliver_result_payload(run, "task-h8c", "late"))
        self.assertEqual(fake.posted("/v1/results"), [], "nothing reaches the room")

    def test_an_ack_first_wake_is_left_unacked_and_logged_once(self):
        task = {"id": "task-h11", "task": "hi", "source": "ag2space", "user_id": "@o:x"}
        self.assertIsNotNone(gw._write_task(dict(task)))
        (gw.TASKS_DIR / "task-h11.txt").rename(gw.TASKS_DIR / "archive" / "task-h11.txt")
        woken = {**task, "ack_first": True, "wake": {"blocker_episode_id": "e9"}}
        self.assertIsNone(gw._write_task(dict(woken), hold=True))
        self.assertIsNone(gw._write_task(dict(woken), hold=True))
        lines = [m for m in self.logs if "wake for task-h11" in m]
        self.assertEqual(len(lines), 1)
        self.assertIn("an ack_first wake is not run", lines[0])

    def test_the_result_gate_settles_a_closed_turn_and_posts_the_rest_with_lease_fields(self):
        turn = seed_ack("task-h9g", _ack("task-h9g", 3))["turn_id"]
        with open(agent_turn.room_actions_path(gw._STATE), "a") as fh:
            fh.write(json.dumps({"record_type": "dispatch_outcome", "turn_id": turn, "seq": 1,
                                 "operation_id": f"op_{turn}_1", "operation_status": "succeeded",
                                 "applied_execution_phase": "responded", "applied_terminal": True}) + "\n")
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
        self.assertTrue(gw._deliver_result_payload("task-h9g", "task-h9g", "final"))
        self.assertEqual(fake.posted("/v1/results"), [], "the room action already answered")
        self.assertTrue(agent_turn.is_closed(gw._STATE, turn))

        turn = seed_ack("task-h10", _ack("task-h10", 2))["turn_id"]
        self.assertTrue(gw._deliver_result_payload("task-h10", "task-h10", "the answer"))
        sent = fake.posted("/v1/results")[0]
        self.assertEqual((sent["status"], sent["execution_generation"], sent["worker_id"]),
                         ("completed", 2, gw._ATTEMPTS.worker_id()))
        self.assertTrue(agent_turn.is_closed(gw._STATE, turn))
        seed_ack("task-h10", _ack("task-h10", 2))
        self.assertTrue(gw._deliver_result_payload("task-h10", "task-h10", "the answer"),
                        "the delivered item answers a repeat")
        self.assertEqual(gw._ATTEMPTS.get("task-h10"), {}, "and the attempt is finished")

    def test_a_control_tasks_result_is_retired_locally_never_posted(self):
        tid = "task-ctl-1"
        (gw.TASKS_DIR / f"{tid}.txt").write_text(gw.task_attempts.cancel_task_text(
            "task-x", tid, "2026-10-26T00:00:00Z", "test"))
        result = gw.RESULTS_DIR / f"{tid}.txt"
        result.write_text("[no-send]")
        old = time.time() - gw.ORPHAN_GRACE_S - 60
        os.utime(result, (old, old))
        fake = self.use(FakeGateway())
        mock.patch.object(gw, "_last_orphan_sweep", 0.0).start()
        gw._reconcile_orphan_results(set())
        self.assertFalse(result.exists())
        self.assertEqual(fake.posted("/v1/results"), [])

    # ── round 13: a pinned result, a serve's own lease, one reader, nothing minted ──
    def pin_checked(self):
        """Pause the ruling right after its first check of the result's bytes passes, inside its lock:
        (reached, release)."""
        real, reached, release = gw._live_source, threading.Event(), threading.Event()

        def held(*a):
            body = real(*a)
            if body is not None and not reached.is_set():
                reached.set()
                release.wait(10)
            return body
        mock.patch.object(gw, "_live_source", side_effect=held).start()
        return reached, release

    def core_writes(self, tid, text):
        tmp = gw.RESULTS_DIR / f".{tid}.core"
        tmp.write_text(text)
        os.replace(tmp, gw.RESULTS_DIR / f"{tid}.txt")

    def unpatched(self):
        mock.patch.stopall()
        mock.patch.object(gw, "_log", self.logs.append).start()

    def test_a_reply_written_after_the_pin_is_a_new_result_never_the_one_ruled(self):
        tid = "task-pin-replaced"
        self.owed_failure(tid)
        gw._post_task_beats({tid}, force=True)
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
        reached, release = self.pin_checked()
        drain = threading.Thread(target=gw._post_ready_results, args=({tid},))
        drain.start()
        self.assertTrue(reached.wait(5))
        self.core_writes(tid, "the core's answer")          # inside the check-to-publish window
        release.set()
        drain.join(10)
        self.unpatched()
        self.use(fake)
        posted = [p for p in fake.posted("/v1/results") if p["id"] == tid]
        self.assertEqual([(p.get("status"), p.get("no_send")) for p in posted], [("failed", True)],
                         "what was published is what was pinned and ruled")
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), "the core's answer", "left live")
        self.assertEqual(list(gw.RESULTS_DIR.glob(f".delivering/{tid}*")), [], "the pin ended with its generation")
        gw._post_ready_results({tid})
        self.assertEqual(len([p for p in fake.posted("/v1/results") if p["id"] == tid]), 1,
                         "the lease is closed: the new result is never sent in its place")
        kept = list((gw.RESULTS_DIR / "undelivered").glob(f"{tid}*"))
        self.assertEqual([p.read_text() for p in kept], ["the core's answer"], "kept for review, not lost")
        self.assertEqual(list(gw.RESULTS_DIR.glob(f".delivering/{tid}*")), [], "and its pin ended with it")

    def test_an_upload_and_its_post_come_from_the_same_pinned_result(self):
        tid = "task-pin-attach"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        seed_ack(tid, _ack(tid, 1))
        self.ran(tid)
        self.core_writes(tid, "first answer\n[file: /tmp/first.png]")
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
        uploads, reached, release = [], threading.Event(), threading.Event()

        def upload(room, fp):
            uploads.append(fp)
            reached.set()
            release.wait(10)
            return True, ""
        mock.patch.object(gw, "_upload_attachment", side_effect=upload).start()
        mock.patch.object(gw, "_load_task_rooms", return_value={tid: "!room:x"}).start()
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
        drain = threading.Thread(target=gw._post_ready_results, args=({tid},))
        drain.start()
        self.assertTrue(reached.wait(5))
        self.core_writes(tid, "second answer\n[file: /tmp/second.png]")
        release.set()
        drain.join(10)
        posted = [p["body"] for p in fake.posted("/v1/results") if p["id"] == tid]
        self.assertEqual((uploads, posted), (["/tmp/first.png"], ["first answer"]),
                         "the upload and the post are of one result: the one pinned")
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), "second answer\n[file: /tmp/second.png]")

    def test_a_stop_is_settled_by_the_pinned_result_and_a_later_answer_stays_live(self):
        tid = "task-pin-stop"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        seed_ack(tid, _ack(tid, 1))
        self.beat_error(tid, "task_cancelled")
        self.ran(tid)
        self.core_writes(tid, "[no-send]")
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
        reached, release = self.paused("_relay_result")
        drain = threading.Thread(target=gw._post_ready_results, args=({tid},))
        drain.start()
        self.assertTrue(reached.wait(5))
        self.core_writes(tid, "the answer after all")
        release.set()
        drain.join(10)
        self.unpatched()
        self.assertEqual(len(fake.posted("/cancel-ack")), 1, "the pinned [no-send] confirmed the Stop")
        self.assertEqual(fake.posted("/v1/results"), [])
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), "the answer after all",
                         "written after the pin: a new result, left live")

    def test_a_newer_serve_waits_for_a_ruling_already_past_its_pin_check(self):
        tid = "task-pin-vs-ack"
        self.owed_failure(tid, lease="L1")
        gw._post_task_beats({tid}, force=True)
        served = []

        def results(p):
            if served and p.get("execution_generation") == 1:
                raise _http(409, {"error_code": "stale_execution_generation"})
            return {"ok": True}
        fake = self.use(FakeGateway(**{"/v1/results": results, f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        reached, release = self.pin_checked()
        drain = threading.Thread(target=gw._post_ready_results, args=({tid},))
        drain.start()
        self.assertTrue(reached.wait(5))
        (gw.TASKS_DIR / "archive" / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.txt")

        def reserve():
            served.append(gw._write_task(_task(tid, lease_id="L2")))
            self.assertTrue(gw._record_pending_acks([(tid, True, "L2")]))
            gw._post_task_ack(tid, durable=True, lease_id="L2")
        server = threading.Thread(target=reserve)
        server.start()
        server.join(0.3)
        blocked = server.is_alive()
        release.set()
        drain.join(10)
        server.join(10)
        self.unpatched()
        self.use(fake)
        gw._retry_pending_acks({tid})               # an ack held back while the failure was sending
        self.assertTrue(blocked, "the serve waits until the ruling has published")
        posts = [(p["execution_generation"], p.get("status")) for p in fake.posted("/v1/results") if p["id"] == tid]
        self.assertIn(posts, ([], [(1, "failed")]), "only under the generation it was ruled for, or withdrawn")
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec.get("execution_generation"), rec.get("lease_id"), rec.get("run")), (2, "L2", f"{tid}~~2"),
                         "the newer serve stands, as its own run")
        gw._post_task_beats({tid}, force=True)
        self.assertTrue((gw.TASKS_DIR / f"{tid}~~2.txt").exists(), "generation 2's run is queued")

    def test_a_pin_that_cannot_be_released_holds_its_id_until_it_is(self):
        tid = "task-pin-stuck"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        seed_ack(tid, _ack(tid, 1))
        self.ran(tid)
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
        inflight = {tid}
        real = os.unlink

        def unlink(path, *a, **k):
            if str(path).endswith(f"{tid}.pin"):
                raise PermissionError("denied")
            return real(path, *a, **k)
        with mock.patch.object(gw.disposal.os, "unlink", side_effect=unlink):
            gw._post_ready_results(inflight)
            self.assertIn(tid, inflight, "its pin still holds the id")
            self.assertTrue(settling(tid), "a wake waits while the pin holds")
        gw._post_ready_results(inflight)
        self.assertNotIn(tid, inflight)
        self.assertEqual(list(gw.RESULTS_DIR.glob(f".delivering/{tid}*")), [])
        self.assertEqual(len(fake.posted("/v1/results")), 1, "posted once")

    def test_an_older_leases_answer_before_the_newer_serve_is_on_the_ack_ledger_moves_nothing(self):
        tid = "task-lease-window"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        self.assertTrue(gw._record_pending_acks([(tid, True, "L1")]))       # L1's ack retry is held
        self.assertIsNotNone(gw._write_task(_task(tid, lease_id="L2")))     # re-served: held
        for answer in (_http(404, {"error": "not leased", "reason": "stale lease generation"}),
                       _http(409, {"error_code": "stale_execution_generation"})):
            self.assertTrue(gw._record_pending_acks([(tid, True, "L1")]))
            self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": answer}))
            gw._post_task_ack(tid, durable=True, lease_id="L1")
            rec = gw._ATTEMPTS.get(tid)
            self.assertEqual((rec.get("fenced"), rec["pending_serve"]["lease"], rec["run"]), (None, "L2", tid),
                             "an answer about the replaced lease leaves the run to the held serve's ack")
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True}}))
        gw._post_task_beats({tid}, force=True)
        self.assertEqual(self.all_controls(tid), [], "no cancel of generation 1 against the waiting serve")
        self.assertTrue(gw._record_pending_acks([(tid, True, "L2")]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["run"], rec["execution_generation"]), (f"{tid}~~2", 2))

    def test_a_refusal_of_the_current_lease_after_a_restart_fences_it(self):
        tid = "task-lease-current"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L2"), set(), []))
        self.assertTrue(gw._record_pending_acks([(tid, True, "L2")]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        with mock.patch.object(gw, "_forget_pending_ack"):                  # the crash: L2 stays on the ledger
            self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        self.assertEqual(gw._ATTEMPTS.get(tid)["awaiting_ack"], False)
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(409, {
            "error_code": "stale_execution_generation"})}))
        gw._retry_pending_acks({tid})
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual(rec["fenced"], "stale_execution_generation", "the running attempt's own lease")
        self.assertEqual([p.name for p in self.controls(tid)], [f"{rec['fence_ctl']}.txt"], "the core is stopped")
        self.assertNotIn(tid, gw._load_pending_acks())
        self.assertEqual(fake.posted("/cancel-ack"), [])

    def test_a_damaged_entry_holds_only_its_own_task(self):
        for damage in ("damaged", None, [], "", 0, False):
            with self.subTest(damage=damage):
                tag = f"{len(repr(damage))}{type(damage).__name__}"
                tid, other = f"task-damaged-{tag}", f"task-damage-bystander-{tag}"
                for t in (tid, other):
                    self.assertTrue(gw._take_served_task(_task(t), set(), []))
                    seed_ack(t, _ack(t, 1))
                    self.ran(t)
                    old = time.time() - 5
                    os.utime(gw.RESULTS_DIR / f"{t}.txt", (old, old))
                self.seed(tid, damage)
                fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, "/v1/tasks/heartbeat": {"ok": True}}))
                for _pass in range(2):
                    gw._post_ready_results({tid, other})
                    gw._post_task_beats({tid, other}, force=True)
                posted = {p["id"]: p for p in fake.posted("/v1/results")}
                self.assertNotIn(tid, posted, "unknown is held: never posted, with or without lease fields")
                self.assertEqual(posted[other]["execution_generation"], 1, "the rest of the pass goes on")
                self.assertTrue((gw.RESULTS_DIR / f"{tid}.txt").exists())
                self.assertEqual(ledger()["tasks"][tid], damage, "never rewritten")
                self.assertFalse(remember(tid, _task(tid)), "a serve is not recorded over it")
                self.undamage(tid)
                (gw.RESULTS_DIR / f"{tid}.txt").unlink()
                gw.disposal.pin_path(gw.RESULTS_DIR, tid).unlink(missing_ok=True)

    # ── each lock and guard of round 13, deleted, fails one of these ──
    def served_then(self, tid, lease="L1"):
        self.assertTrue(gw._take_served_task(_task(tid, lease_id=lease), set(), []))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id=lease))

    def reserve_during(self, tid, reached, release, answer):
        """With `answer` (a thread) paused inside its transition, re-serve the task under L2 on another
        thread; True when that serve had to wait for the transition."""
        answer.start()
        self.assertTrue(reached.wait(5))
        serve = threading.Thread(target=gw._write_task, args=(_task(tid, lease_id="L2"),))
        serve.start()
        serve.join(0.3)
        blocked = serve.is_alive()
        release.set()
        answer.join(10)
        serve.join(10)
        self.unpatched()
        return blocked

    def assert_serve_waits(self, tid, blocked):
        rec = gw._ATTEMPTS.get(tid)
        self.assertTrue(blocked, "the serve waits for the transition under way")
        self.assertEqual((rec.get("lease_id"), (rec.get("pending_serve") or {}).get("lease")), ("L1", "L2"),
                         "the newer serve is held for its ack")

    def test_a_refusal_and_a_serve_never_interleave(self):
        tid = "task-lock-ackfenced"
        self.served_then(tid)
        self.assertTrue(gw._record_pending_acks([(tid, True, "L1")]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(409, {"error_code": "stale_execution_generation"})}))
        reached, release = self.paused("_fence_owes")
        answer = threading.Thread(target=gw._post_task_ack, args=(tid,), kwargs={"durable": True, "lease_id": "L1"})
        self.assert_serve_waits(tid, self.reserve_during(tid, reached, release, answer))

    def test_a_beat_fence_and_a_serve_never_interleave(self):
        tid = "task-lock-beatfence"
        self.served_then(tid)
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True, "task_errors": [
            {"task_id": tid, "error_code": "stale_execution_generation"}]}}))
        reached, release = self.paused("_fence_current")
        answer = threading.Thread(target=gw._post_task_beats, args=({tid},), kwargs={"force": True})
        self.assert_serve_waits(tid, self.reserve_during(tid, reached, release, answer))

    def test_a_not_leased_answer_and_a_serve_never_interleave(self):
        tid = "task-lock-acksettled"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(404, {"error": "not leased"})}))
        real, reached, release = gw.task_attempts.Transition.ack_settled, threading.Event(), threading.Event()

        def held(t, *a):
            ok = real(t, *a)
            if not reached.is_set():
                reached.set()
                release.wait(10)
            return ok
        mock.patch.object(gw.task_attempts.Transition, "ack_settled", autospec=True, side_effect=held).start()
        answer = threading.Thread(target=gw._post_task_ack, args=(tid,), kwargs={"durable": True, "lease_id": "L1"})
        self.assert_serve_waits(tid, self.reserve_during(tid, reached, release, answer))

    def test_a_not_leased_answer_ends_the_wait_of_its_own_serve(self):
        tid = "task-not-leased-own"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(404, {"error": "not leased"})}))
        gw._post_task_ack(tid, durable=True, lease_id="L1")
        self.assertIs(gw._ATTEMPTS.get(tid)["awaiting_ack"], False)

    def test_a_beat_answered_after_a_same_generation_serve_is_left_to_that_serve(self):
        tid = "task-beat-same-gen"
        self.served_then(tid)
        reached, release = threading.Event(), threading.Event()

        def beat(payload):
            reached.set()
            release.wait(10)
            return {"ok": True, "task_errors": [{"task_id": tid, "error_code": "stale_execution_generation"}]}
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": beat, f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        beats = threading.Thread(target=gw._post_task_beats, args=({tid},), kwargs={"force": True})
        beats.start()
        self.assertTrue(reached.wait(5))
        self.assertIsNotNone(gw._write_task(_task(tid, lease_id="L2")))       # served again, same generation
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        release.set()
        beats.join(10)
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec.get("fenced"), rec.get("lease_id"), self.all_controls(tid)), (None, "L2", []),
                         "the beat was for L1's serve")

    def test_a_wake_opening_and_a_beat_fence_never_interleave(self):
        tid = "task-lock-wake"
        self.served_then(tid)
        (gw.TASKS_DIR / "archive").mkdir(exist_ok=True)
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True, "task_errors": [
            {"task_id": tid, "error_code": "task_cancelled"}]}}))
        real, reached, release = gw.task_attempts.Transition.open_wake, threading.Event(), threading.Event()

        def opening(t, *a, **k):
            reached.set()
            release.wait(10)
            return real(t, *a, **k)
        mock.patch.object(gw.task_attempts.Transition, "open_wake", autospec=True, side_effect=opening).start()
        wake = _task(tid, lease_id="L2", wake={"blocker_episode_id": "e1", "resolved_blockers": []})
        serve = threading.Thread(target=gw._write_task, args=(wake,))
        serve.start()
        self.assertTrue(reached.wait(5))
        beats = threading.Thread(target=gw._post_task_beats, args=({tid},), kwargs={"force": True})
        beats.start()
        beats.join(0.3)
        blocked = beats.is_alive()
        release.set()
        serve.join(10)
        beats.join(10)
        self.unpatched()
        self.assertTrue(blocked, "the beat's checks wait for the wake to open")
        self.assertEqual(self.all_controls(tid), [], "no Stop of the earlier attempt against the wake")
        self.assertEqual(gw._ATTEMPTS.get(tid).get("wake_episode"), "e1")

    def test_a_refusal_for_a_retired_attempt_records_nothing(self):
        tid = "task-refused-retired"
        self.served_then(tid)
        self.ran(tid)
        self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
        gw._post_ready_results({tid})
        self.assertEqual(gw._ATTEMPTS.get(tid), {})
        self.assertTrue(gw._record_pending_acks([(tid, True, "L1")]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(409, {"error_code": "stale_execution_generation"})}))
        gw._post_task_ack(tid, durable=True, lease_id="L1")
        self.assertEqual((gw._ATTEMPTS.get(tid), self.all_controls(tid)), ({}, []))

    def test_an_unreadable_entry_is_held_in_flight(self):
        tid = "task-unknown-held"
        self.seed(tid, ["damaged"])
        self.addCleanup(self.undamage, tid)
        inflight = set()
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True}}))
        gw._post_task_beats(inflight, force=True)
        self.assertIn(tid, inflight, "unknown is never released from flight")
        with txn(tid) as t:
            self.assertFalse(t.mark_dispatch_lost("x"), "no writer writes over it")
        self.assertEqual(ledger()["tasks"][tid], ["damaged"])

    def test_an_older_result_settled_after_a_newer_serve_retires_only_itself(self):
        tid = "task-newer-holds"
        self.served_then(tid)
        self.ran(tid)
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
        real, served = gw.task_attempts.Transition.finish, []

        def finish(t, *a, **k):
            if not served:
                (gw.TASKS_DIR / "archive" / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.txt")
                served.append(gw._write_task(_task(tid, lease_id="L2")))     # served again meanwhile
            return real(t, *a, **k)
        mock.patch.object(gw.task_attempts.Transition, "finish", autospec=True, side_effect=finish).start()
        self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
        inflight = {tid}
        gw._post_ready_results(inflight)
        self.assertIsNotNone(served[0])
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "the settled result is retired")
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec.get("finished"), rec["pending_serve"]["lease"]), (True, "L2"),
                         "the held serve outlives the settled run: its ack decides")
        self.assertEqual(gw._ATTEMPTS.beating({tid}), False, "a settled run beats no more")
        gw._post_ready_results(inflight)
        self.assertEqual(list(gw.RESULTS_DIR.glob(f".delivering/{tid}*")), [])
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        self.assertEqual(gw._ATTEMPTS.get(tid), {}, "its own generation, already answered: nothing to run")
        self.assertEqual(list(gw.TASKS_DIR.glob(f"{tid}~~*")), [])
        del fake

    def test_a_refused_result_whose_pin_cannot_be_released_holds_its_id_until_it_is(self):
        tid = "task-pin-stuck-refused"
        self.served_then(tid)
        self.ran(tid)
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
        self.use(FakeGateway(**{"/v1/results": _http(400, {"error_code": "invalid_request"})}))
        real = os.unlink

        def unlink(path, *a, **k):
            if str(path).endswith(f"{tid}.pin"):
                raise PermissionError("denied")
            return real(path, *a, **k)
        inflight = {tid}
        with mock.patch.object(gw.disposal.os, "unlink", side_effect=unlink):
            for _pass in range(2):
                gw._post_ready_results(inflight)
                self.assertIn(tid, inflight, "its pin still holds the id")
                self.assertTrue(settling(tid), "a wake of the same id waits while the pin holds")
        self.assertEqual(len(list((gw.RESULTS_DIR / "undelivered").glob(f"{tid}*"))), 1, "quarantined once")
        for _pass in range(2):                                  # the pin ends, then the settled id leaves
            gw._post_ready_results(inflight)
        self.assertNotIn(tid, inflight)
        self.assertEqual(list(gw.RESULTS_DIR.glob(f".delivering/{tid}*")), [])
        self.assertFalse(settling(tid))
    # ── round 14: claim, effect with no lock held, commit; a Stop and a newer serve at each boundary ──
    def broker(self, tid):
        """A broker that acks L1 as generation 1 and L2 as generation 2, accepts results and cancel-acks,
        and answers beats with `beat`; every call is recorded with its operation id."""
        fake = FakeGateway(**{"/v1/results": {"ok": True}, f"/v1/tasks/{tid}/cancel-ack": {"ok": True}})
        fake.beat = {"ok": True}
        fake.answers["/v1/tasks/heartbeat"] = lambda _p: fake.beat
        fake.answers[f"/v1/tasks/{tid}/ack"] = lambda p: _ack(tid, 2 if p.get("lease_id") == "L2" else 1)
        return self.use(fake)

    def answered_running(self, tid):
        """Generation 1 served under L1 and acked; the core answered and its task file is still queued."""
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        fake = self.broker(tid)
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        self.core_writes(tid, "the answer")
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
        return fake

    def at(self, boundary):
        """Pause the delivery at `boundary` of its claim: "claim" (ruled and claimed, nothing sent),
        "accepted" (the broker accepted it, nothing committed). (reached, release)."""
        name = {"claim": "_rule_result", "accepted": "_committed"}[boundary]
        real, reached, release = getattr(gw, name), threading.Event(), threading.Event()

        def paused(*a, **k):
            if boundary == "claim":
                out = real(*a, **k)
                reached.set()
                release.wait(10)
                return out
            reached.set()
            release.wait(10)
            return real(*a, **k)
        mock.patch.object(gw, name, side_effect=paused).start()
        return reached, release

    def stop_now(self, fake, tid):
        """The broker's Stop arrives by the next beat; True when it was recorded without waiting."""
        fake.beat = {"ok": True, "task_errors": [{"task_id": tid, "error_code": "task_cancelled",
                                                  "cancel_requested_generation": 1, "cancel_requested_attempt": 1}]}
        th = threading.Thread(target=gw._post_task_beats, args=({tid},), kwargs={"force": True})
        th.start()
        th.join(3)
        fake.beat = {"ok": True}
        return not th.is_alive() and bool(gw._ATTEMPTS.get(tid).get("stop_requested"))

    def serve_newer_now(self, tid):
        """The broker serves the task again under L2 and acks generation 2; True when both landed without waiting."""
        done = []
        th = threading.Thread(target=lambda: done.append(
            gw._write_task(_task(tid, lease_id="L2")) is not None and gw._post_task_ack(tid, durable=True, lease_id="L2")))
        th.start()
        th.join(3)
        return done == [True]

    def assert_settled_once(self, fake, tid, gen=1):
        posts = [p for p in fake.posted("/v1/results") if p["id"] == tid]
        self.assertEqual([p["execution_generation"] for p in posts], [gen], "posted once, under the generation ruled")
        rec = gw._ATTEMPTS.get(tid)
        self.assertIsNone(rec.get("claims"), "no claim is left open")
        return rec

    def test_a_stop_at_each_boundary_of_a_delivery_is_recorded_at_once_and_the_result_still_settles(self):
        for boundary in ("claim", "accepted"):
            with self.subTest(boundary=boundary):
                tid = f"task-sched-stop-{boundary}"
                fake = self.answered_running(tid)
                reached, release = self.at(boundary)
                inflight = {tid}
                drain = threading.Thread(target=gw._post_ready_results, args=(inflight,))
                drain.start()
                self.assertTrue(reached.wait(5))
                self.assertTrue(self.stop_now(fake, tid), "a Stop is a short transition of its own")
                release.set()
                drain.join(10)
                self.unpatched()
                self.use(fake)
                self.assertEqual(self.assert_settled_once(fake, tid), {}, "the cooperative Stop delivers the answer")
                self.assertNotIn(tid, inflight)
                self.assertEqual(fake.posted("/cancel-ack"), [], "the core finished: no cancel-ack")
                self.assertEqual(len(self.all_controls(tid)), 1, "the core was asked to stop once")

    def test_a_newer_serve_at_each_boundary_of_a_delivery_keeps_the_task_and_is_never_credited(self):
        for boundary in ("claim", "accepted"):
            with self.subTest(boundary=boundary):
                tid = f"task-sched-serve-{boundary}"
                fake = self.answered_running(tid)
                reached, release = self.at(boundary)
                inflight = {tid}
                drain = threading.Thread(target=gw._post_ready_results, args=(inflight,))
                drain.start()
                self.assertTrue(reached.wait(5))
                served = threading.Thread(target=gw._write_task, args=(_task(tid, lease_id="L2"),))
                served.start()
                served.join(3)
                self.assertFalse(served.is_alive(), "the newer serve never waits for the send: it is held")
                acked = []
                acker = threading.Thread(target=lambda: acked.append(
                    gw._post_task_ack(tid, durable=True, lease_id="L2")))
                acker.start()
                acker.join(3)
                self.assertFalse(acker.is_alive(), "nor does its ack: it records nothing while the send may be under way")
                if boundary == "claim":
                    self.assertEqual(acked, [False], "a claimed send not yet in the outbox may still go: the switch waits")
                release.set()
                drain.join(10)
                self.unpatched()
                self.use(fake)
                if gw._ATTEMPTS.get(tid).get("pending_serve"):
                    self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"), "settled: the switch goes")
                rec = self.assert_settled_once(fake, tid)
                self.assertEqual((rec["execution_generation"], rec["lease_id"]), (2, "L2"), "generation 2 stands")
                self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "only the delivered result is retired")
                self.assertFalse(agent_turn.is_closed(gw._STATE, rec["turn_id"]), "generation 2's turn is open")
                self.answered_by_its_own_run(fake, tid, inflight)
                self.assertIn(gw.task_attempts.RERUN_RULES["answered"], gw._archived_task_file(f"{tid}~~2").read_text(),
                              "generation 1's answer went out before the switch: generation 2 is told so")

    def answered_by_its_own_run(self, fake, tid, inflight):
        """Generation 2's run is published and its own answer posts under generation 2, on its own item."""
        run = f"{tid}~~2"
        gw._post_task_beats(inflight, force=True)
        self.assertTrue((gw.TASKS_DIR / f"{run}.txt").exists(), "generation 2's run is published")
        self.core_writes(run, "generation 2's answer")
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{run}.txt", (old, old))
        gw._post_ready_results(inflight)
        posts = [(p["execution_generation"], p["body"]) for p in fake.posted("/v1/results") if p["id"] == tid]
        self.assertEqual(posts[-1], (2, "generation 2's answer"), "generation 2's own answer is posted")
        self.assertLessEqual(len([p for p in posts if p[0] == 1]), 1, "generation 1 posted at most once")
        self.assertEqual(list((gw.RESULTS_DIR / "undelivered").glob(f"{run}*")), [], "never quarantined")
        root = gw._delivery_core().backend.root
        self.assertEqual((gw.read_item(root, f"{tid}.run-2") or {}).get("status"), "DELIVERED", "its own item")
        self.assertEqual(gw._ATTEMPTS.get(tid), {})

    def crash_after_acceptance(self, tid):
        """The drain dies after the broker accepted the result, before its commit; then a restart."""
        class Crash(BaseException):
            pass
        real = gw._committed
        mock.patch.object(gw, "_committed", side_effect=lambda *a, **k: (_ for _ in ()).throw(Crash())).start()
        inflight = {tid}
        drain = threading.Thread(target=lambda: self.assertRaises(Crash, gw._deliver_ready, tid,
                                                                  gw.RESULTS_DIR / f"{tid}.txt",
                                                                  *gw._pin_and_claim(tid), inflight))
        drain.start()
        drain.join(10)
        mock.patch.object(gw, "_committed", real).start()
        claim = gw._ATTEMPTS.get(tid)["claims"]
        self.assertEqual([c["kind"] for c in claim.values()], ["result"], "its claim survives the crash")
        gw._ATTEMPTS.take_ownership()                      # the restarted bridge owns the ledger
        return inflight, next(iter(claim))

    def test_a_crash_after_the_broker_accepted_a_result_reconciles_it_once_under_its_operation(self):
        for event in ("none", "stop", "newer serve"):
            with self.subTest(event=event):
                tid = f"task-sched-crash-{event.replace(' ', '-')}"
                fake = self.answered_running(tid)
                inflight, op = self.crash_after_acceptance(tid)
                self.unpatched()
                self.use(fake)
                if event == "stop":
                    self.assertTrue(self.stop_now(fake, tid))
                elif event == "newer serve":
                    self.assertTrue(self.serve_newer_now(tid))
                gw._close_stale_turns(inflight)
                gw._post_ready_results(inflight)
                root = gw._delivery_core().backend.root
                self.assertEqual((gw.read_item(root, tid) or {}).get("lifecycle", {}).get("op"), op,
                                 "the item carries the claim it was published under")
                rec = self.assert_settled_once(fake, tid)
                if event == "newer serve":
                    self.assertEqual((rec["execution_generation"], rec["lease_id"]), (2, "L2"))
                    self.answered_by_its_own_run(fake, tid, inflight)
                else:
                    self.assertEqual(rec, {}, "settled once after the restart")
                    self.assertNotIn(tid, inflight)
                self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists())

    def test_an_ack_at_each_boundary_meets_a_newer_serve_and_commits_nothing_for_it(self):
        for boundary in ("claim", "accepted"):
            with self.subTest(boundary=boundary):
                tid = f"task-sched-ack-{boundary}"
                self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
                fake = self.broker(tid)
                name = {"claim": "_claim_ack", "accepted": "_commit_ack"}[boundary]
                real, reached, release = getattr(gw, name), threading.Event(), threading.Event()

                def paused(*a, real=real, reached=reached, release=release, boundary=boundary, **k):
                    if boundary == "claim":
                        out = real(*a, **k)
                    if not reached.is_set():
                        reached.set()
                        release.wait(10)
                    return out if boundary == "claim" else real(*a, **k)
                mock.patch.object(gw, name, side_effect=paused).start()
                acker = threading.Thread(target=gw._post_task_ack, args=(tid,), kwargs={"durable": True, "lease_id": "L1"})
                acker.start()
                self.assertTrue(reached.wait(5))
                served = gw._write_task(_task(tid, lease_id="L2"))
                release.set()
                acker.join(10)
                self.unpatched()
                self.use(fake)
                self.assertIsNotNone(served, "the newer serve never waited for the ack's send")
                rec = gw._ATTEMPTS.get(tid)
                self.assertEqual((rec["lease_id"], rec.get("execution_generation"), rec["pending_serve"]["lease"]),
                                 ("L1", 1, "L2"), "L1's ack binds its own run; L2 is held for its own")
                self.assertIsNone(rec.get("claims"))
                self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
                rec = gw._ATTEMPTS.get(tid)
                self.assertEqual((rec["run"], rec["execution_generation"]), (f"{tid}~~2", 2),
                                 "generation 2 gets its own run")

    def test_an_ack_whose_outcome_is_unknown_is_retried_under_the_same_operation(self):
        for fault in ("timeout", "crash after acceptance"):
            with self.subTest(fault=fault):
                tid = f"task-sched-unknown-{len(fault)}"
                pending: list = []
                self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), pending))
                self.assertTrue(gw._record_pending_acks(pending))
                fake = self.broker(tid)
                if fault == "timeout":
                    fake.answers[f"/v1/tasks/{tid}/ack"] = TimeoutError("timed out")
                    self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L1"))
                else:
                    class Crash(BaseException):
                        pass
                    with mock.patch.object(gw, "_commit_ack", side_effect=Crash()):
                        with self.assertRaises(Crash):
                            gw._post_task_ack(tid, durable=True, lease_id="L1")
                    gw._ATTEMPTS.take_ownership()           # the restarted bridge
                claims = gw._ATTEMPTS.get(tid)["claims"]
                self.assertEqual(len(claims), 1, "the claim is kept: its outcome is unknown, not failed")
                fake.answers[f"/v1/tasks/{tid}/ack"] = lambda p: _ack(tid, 1)
                gw._retry_pending_acks({tid})
                ops = [o for (m, path, _p), o in zip(fake.calls, fake.ops) if path.endswith("/ack")]
                self.assertEqual(set(ops), set(claims), "every send of it carries the one operation id")
                rec = gw._ATTEMPTS.get(tid)
                self.assertEqual((rec["execution_generation"], rec.get("claims")), (1, None))
                self.assertNotIn(tid, gw._load_pending_acks())

    def test_one_tasks_stalled_send_never_holds_another_tasks_beat_stop_or_result(self):
        a, b = "task-stall-a", "task-stall-b"
        stalled, release = threading.Event(), threading.Event()
        fakes = {t: None for t in (a, b)}
        for t in (a, b):
            self.assertTrue(gw._take_served_task(_task(t, lease_id="L1"), set(), []))
        fake = FakeGateway(**{f"/v1/tasks/{a}/ack": _ack(a, 1), f"/v1/tasks/{b}/ack": _ack(b, 1),
                              f"/v1/tasks/{b}/cancel-ack": {"ok": True}})
        fake.beat = {"ok": True}

        def results(p):
            if p["id"] == a:
                stalled.set()
                release.wait(60)                        # task A's send hangs in transport
            return {"ok": True}
        fake.answers["/v1/results"] = results
        fake.answers["/v1/tasks/heartbeat"] = lambda _p: fake.beat
        self.use(fake)
        for t in (a, b):
            self.assertTrue(gw._post_task_ack(t, durable=True, lease_id="L1"))
        self.core_writes(a, "A's answer")
        inflight = {a, b}
        mock.patch.object(gw, "DELIVERY_PASS_WAIT_S", 0.5).start()
        drain = threading.Thread(target=gw._post_ready_results, args=(inflight,))
        drain.start()
        self.assertTrue(stalled.wait(5))
        try:
            fake.beat = {"ok": True, "task_errors": [{"task_id": b, "error_code": "task_cancelled",
                                                      "cancel_requested_generation": 1, "cancel_requested_attempt": 1}]}
            beats = threading.Thread(target=gw._post_task_beats, args=({a, b},), kwargs={"force": True})
            beats.start()
            beats.join(20)
            self.assertFalse(beats.is_alive(), "beats go on while A's send hangs")
            self.assertTrue(gw._ATTEMPTS.get(b).get("stop_requested"), "B's Stop is recorded")
            self.assertTrue(gw._ATTEMPTS.get(a).get("claims"), "A's claim is open, its send under way")
            self.core_writes(b, "[no-send] stopped")
            second = threading.Thread(target=gw._post_ready_results, args=(inflight,))
            second.start()
            second.join(20)
            self.assertFalse(second.is_alive(), "a pass never waits on A's send")
            self.assertEqual(gw._ATTEMPTS.get(b), {}, "B's result settled its Stop")
            self.assertNotIn(b, inflight)
            self.assertIn(a, inflight)
        finally:
            release.set()
        drain.join(60)
        self.assertEqual(gw._ATTEMPTS.get(a), {}, "A settles once its send returns")
        del fakes

    def test_a_late_ack_and_a_finish_never_interleave_and_nothing_is_resurrected(self):
        tid = "task-late-ack-finish"
        fake = self.answered_running(tid)
        turn = gw._ATTEMPTS.get(tid)["turn_id"]
        self.assertTrue(gw._record_pending_acks([(tid, True, "L1")]))   # L1's ack, still to be confirmed
        real, reached, release = gw.task_attempts.Transition.finish, threading.Event(), threading.Event()

        def finishing(t, *a, **k):
            reached.set()
            release.wait(10)                    # the settle is inside its transition
            return real(t, *a, **k)
        mock.patch.object(gw.task_attempts.Transition, "finish", autospec=True, side_effect=finishing).start()
        inflight = {tid}
        drain = threading.Thread(target=gw._post_ready_results, args=(inflight,))
        drain.start()
        self.assertTrue(reached.wait(5))
        late = threading.Thread(target=gw._post_task_ack, args=(tid,), kwargs={"durable": True, "lease_id": "L1"})
        late.start()
        late.join(0.3)
        waited = late.is_alive()
        release.set()
        drain.join(10)
        late.join(10)
        self.unpatched()
        self.assertTrue(waited, "the late ack waits for the finish under way")
        self.assertEqual(gw._ATTEMPTS.get(tid), {}, "the settled attempt is not brought back")
        self.assertTrue(agent_turn.is_closed(gw._STATE, turn), "nor its turn reopened")
        self.assertIsNone(agent_turn.open_turn_for_task(gw._STATE, tid))
        self.assertEqual(len([p for p in fake.posted("/v1/results") if p["id"] == tid]), 1)

    def test_a_late_ack_held_past_its_check_while_the_finish_runs_resurrects_nothing(self):
        tid = "task-late-ack-held"
        fake = self.answered_running(tid)
        real_ret, at_retire, go_retire = gw._retired, threading.Event(), threading.Event()

        def retire(*a, **k):
            at_retire.set()
            go_retire.wait(10)
            return real_ret(*a, **k)
        mock.patch.object(gw, "_retired", side_effect=retire).start()       # posted, not yet committed
        inflight = {tid}
        drain = threading.Thread(target=gw._post_ready_results, args=(inflight,))
        drain.start()
        self.assertTrue(at_retire.wait(5))
        real, reached, release = gw._commit_ack, threading.Event(), threading.Event()

        def held(*a, **k):
            reached.set()
            release.wait(10)
            return real(*a, **k)
        mock.patch.object(gw, "_commit_ack", side_effect=held).start()      # the late ack, accepted, not committed
        late = threading.Thread(target=gw._post_task_ack, args=(tid,), kwargs={"durable": True, "lease_id": "L1"})
        late.start()
        self.assertTrue(reached.wait(5))
        go_retire.set()
        drain.join(10)                                                       # the finish runs beside the held ack
        release.set()
        late.join(10)
        self.unpatched()
        self.assertEqual(gw._ATTEMPTS.get(tid), {}, "the finished attempt is not brought back")
        self.assertIsNone(agent_turn.open_turn_for_task(gw._STATE, tid))
        self.assertEqual(len([p for p in fake.posted("/v1/results") if p["id"] == tid]), 1)

    def test_a_bridge_killed_after_the_broker_accepted_its_ack_is_reconciled_once_by_the_next(self):
        import subprocess
        from _helpers import hermetic_gateway
        tid, effects = "task-killed-mid-ack", gw._STATE / "killed-mid-ack.effects"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        self.assertTrue(gw._record_pending_acks([(tid, True, "L1")]))
        child = textwrap.dedent(f"""
            import json, os, sys
            sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
            from _helpers.hermetic_gateway import isolate_then_import
            gw, _ = isolate_then_import(root={str(hermetic_gateway.ROOT)!r}, env={{
                "REMOTE_TASK_LIFECYCLE_V2": "1", "REMOTE_TASK_TURN_CONTEXT": "1"}})
            assert gw._ATTEMPTS.take_ownership()
            def req(method, path, payload=None, timeout=None, **k):
                if path.endswith("/ack"):
                    with open({str(effects)!r}, "a") as fh:
                        fh.write(json.dumps({{"op": k.get("operation_id"), "lease": payload.get("lease_id")}}) + "\\n")
                    os._exit(9)                 # accepted by the broker; killed before the commit
                return {{}}
            gw._req = req
            gw._post_task_ack({tid!r}, durable=True, lease_id="L1")
            os._exit(0)
        """)
        proc = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 9, proc.stderr[-2000:])
        accepted = [json.loads(line) for line in effects.read_text().splitlines()]
        self.assertEqual(len(accepted), 1)
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual(list(rec["claims"]), [accepted[0]["op"]], "the dead bridge's claim is on disk")
        self.assertTrue(gw._ATTEMPTS.take_ownership())          # the restarted bridge
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        gw._close_stale_turns({tid})
        self.assertTrue(any("outcome unknown" in m and accepted[0]["op"] in m for m in self.logs))
        gw._retry_pending_acks({tid})
        acks = [o for (_m, path, _p), o in zip(fake.calls, fake.ops) if path.endswith("/ack")]
        self.assertEqual(acks, [accepted[0]["op"]], "re-sent once, under the same operation id")
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["execution_generation"], rec.get("awaiting_ack"), rec.get("claims")), (1, False, None))
        self.assertNotIn(tid, gw._load_pending_acks())
        turns = [p for p in agent_turn.open_turns(gw._STATE)
                 if (agent_turn.read_turn_context(p) or {}).get("task_id") == tid]
        self.assertEqual(len(turns), 1, "one turn, opened once")

    def test_a_ledger_not_owned_at_start_is_taken_on_the_next_pass(self):
        epoch = gw._ATTEMPTS.epoch
        self.addCleanup(lambda: gw._ATTEMPTS.epoch is None and gw._ATTEMPTS.take_ownership())
        gw._ATTEMPTS.epoch = None                       # the start's take failed
        with txn("task-unowned") as t:
            self.assertFalse(t.put(x=1), "nothing is written while it is not owned")
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True}}))
        gw._post_task_beats(set(), force=True)
        self.assertEqual(gw._ATTEMPTS.epoch, epoch + 1, "the next pass takes it")
        with txn("task-unowned") as t:
            self.assertTrue(t.put(x=1))

    # ── round 14: every way a held serve's ack, the switch and its reconcile can fail ──
    def held(self, tid, gen_answer=2):
        """Served and acked as generation 1 under L1, then served again under L2 and held."""
        fake = self.served_acked(tid)
        inflight = {tid}
        self.reserved(tid, inflight)
        fake.answers[f"/v1/tasks/{tid}/ack"] = lambda p, t=tid: _ack(t, gen_answer)
        return fake, inflight

    def test_a_held_serves_ack_refused_ends_it_and_only_a_task_wide_answer_fences_the_run(self):
        for code, fenced in (("stale_execution_generation", None), ("task_cancelled", "cancel_confirmed")):
            with self.subTest(code=code):
                tid = f"task-held-refused-{code.split('_')[0]}"
                fake, _ = self.held(tid)
                fake.answers[f"/v1/tasks/{tid}/ack"] = _http(409, {"error_code": code})
                self.assertTrue(gw._record_pending_acks([(tid, True, "L2")]))
                self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L2"))
                rec = gw._ATTEMPTS.get(tid)
                self.assertEqual((rec.get("pending_serve"), rec["run"], rec.get("fenced")), (None, tid, fenced))
                self.assertFalse(gw._reserve_path(tid).exists())
                self.assertNotIn(tid, gw._load_pending_acks())

    def test_a_held_serves_ack_that_cannot_be_read_or_no_longer_holds_moves_nothing(self):
        tid = "task-held-unread"
        self.held(tid)
        with txn(tid) as t:
            claim = t.claim("ack", serve="L2")
        with mock.patch.object(gw.task_attempts.Transition, "rec", new_callable=mock.PropertyMock,
                               return_value=gw.task_attempts.UNKNOWN):
            self.assertEqual(gw._decide_held_serve(tid, claim, _ack(tid, 2), 2, True), "unrecorded")
        with txn(tid) as t:
            t.release_claim(claim)
        self.assertEqual(gw._decide_held_serve(tid, claim, _ack(tid, 2), 2, True), "stale")
        self.assertEqual(gw._ATTEMPTS.get(tid)["pending_serve"]["lease"], "L2", "still held")

    def test_a_switch_that_cannot_stop_the_earlier_run_or_be_saved_waits(self):
        for fault in ("stop", "switch"):
            with self.subTest(fault=fault):
                tid = f"task-switch-fails-{fault}"
                self.held(tid)
                (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.claimed-core1")
                self.addCleanup(lambda t=tid: (gw.TASKS_DIR / f"{t}.claimed-core1").unlink(missing_ok=True))
                stuck = (self.no_ctl_write() if fault == "stop"
                         else failing_commit("the switch to run"))
                with stuck:
                    self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L2"))
                rec = gw._ATTEMPTS.get(tid)
                self.assertEqual((rec["run"], rec["pending_serve"]["lease"]), (tid, "L2"), "nothing switched")
                self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"), "the retry switches")
                self.assertEqual(gw._ATTEMPTS.get(tid)["run"], f"{tid}~~2")

    def test_a_held_serve_whose_saved_task_is_lost_fails_its_generation_rather_than_run_nothing(self):
        tid = "task-held-lost"
        fake, inflight = self.held(tid)
        gw._reserve_path(tid).write_text("{not json")
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        run = f"{tid}~~2"
        self.assertEqual(gw._ATTEMPTS.get(tid)["owed_publish"], run)
        gw._post_task_beats(inflight, force=True)
        self.assertFalse((gw.TASKS_DIR / f"{run}.txt").exists(), "nothing to publish")
        self.assertEqual((gw.RESULTS_DIR / f"{run}.txt").read_text(), gw.DISPATCH_LOST_RESULT)
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("owed_publish"))
        del fake

    def test_an_owed_run_that_cannot_be_published_yet_is_retried(self):
        tid = "task-owed-publish-retry"
        _, inflight = self.held(tid)
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        with mock.patch.object(gw, "_write_task", return_value=None):
            gw._post_task_beats(inflight, force=True)
        self.assertEqual(gw._ATTEMPTS.get(tid)["owed_publish"], f"{tid}~~2", "still owed")
        gw._post_task_beats(inflight, force=True)
        self.assertTrue((gw.TASKS_DIR / f"{tid}~~2.txt").exists())

    def test_the_reconcile_sends_the_earlier_runs_cancel_ack_first_and_reads_its_items(self):
        tid = "task-reconcile-cancel-ack"
        fake, _ = self.held(tid)
        with txn(tid) as t:
            t.put(stop_confirmed=True, cancel_requested_generation=1)
            gw._want_cancel_ack(t)
        fake.answers[f"/v1/tasks/{tid}/cancel-ack"] = OSError("down")
        self.assertFalse(gw._reconcile_earlier_run(tid), "its cancel-ack may not have landed")
        fake.answers[f"/v1/tasks/{tid}/cancel-ack"] = {"ok": True}
        self.assertTrue(gw._reconcile_earlier_run(tid))
        self.assertFalse(gw._reconcile_earlier_run("task-never-recorded"), "no record: nothing to settle against")
        with txn(tid) as t:
            claim = t.claim("result", file=[1])
        root = gw._delivery_core().backend.root
        self.publish_claimed(tid, claim["op"])
        with mock.patch.object(gw, "withdraw_unsent", side_effect=OSError("disk")):
            self.assertFalse(gw._reconcile_earlier_run(tid), "its item cannot be checked")
        for status in ("DELIVERED", "PARKED"):
            with mock.patch.object(gw, "read_item", return_value={"lifecycle": {"op": claim["op"]}, "status": status}):
                self.assertTrue(gw._reconcile_earlier_run(tid), f"{status} is final")
        self.assertTrue(gw._reconcile_earlier_run(tid), "unsent: taken back")
        self.assertIsNone(gw.read_item(root, tid))

    def test_stopping_an_earlier_run_withdraws_stops_or_reports_why_it_could_not(self):
        tid = "task-stop-run"
        self.served_acked(tid)
        with txn(tid) as t:
            with mock.patch.object(gw.Path, "unlink", side_effect=PermissionError("denied")):
                self.assertFalse(gw._stop_run(t, tid, "test"), "an untaken file that cannot be withdrawn")
            (gw.TASKS_DIR / f"{tid}.txt").unlink()
            self.assertTrue(gw._stop_run(t, tid, "test"), "never taken and gone: nothing runs")
            self.assertTrue(gw._stop_run(t, None, "test"))
        self.assertTrue(any("could not be withdrawn" in m for m in self.logs))

    def test_a_held_serve_delivered_again_or_not_recordable_is_left_for_the_broker(self):
        tid = "task-held-again"
        self.held(tid)
        self.assertEqual(gw._write_task(_task(tid, lease_id="L2", source_event_id="$e")), (tid, True), "the same held serve")
        self.assertEqual(sorted(p.name for p in gw.TASKS_DIR.glob(f"{tid}*")), [f"{tid}.txt"], "nothing published")
        other = "task-held-unrecordable"
        self.served_acked(other)
        with failing_commit("a new run"):
            self.assertIsNone(gw._write_task(_task(other, lease_id="L2")), "its run cannot be allocated")
        with mock.patch.object(gw, "_durable_write", return_value=False):
            self.assertIsNone(gw._write_task(_task(other, lease_id="L2")), "its task cannot be saved")
        self.assertIsNone(gw._ATTEMPTS.get(other).get("pending_serve"))

    def test_acks_that_cannot_be_claimed_or_settled_keep_their_pending_ack(self):
        tid = "task-ack-unclaimable"
        pending: list = []
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), pending))
        self.assertTrue(gw._record_pending_acks(pending))
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(404, {"error": "not leased to you"})}))
        with failing_commit("claim ack-"):
            self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L1"))
            self.assertEqual(gw._post_ack_first(tid, "L1"), "retry")
        self.assertEqual(fake.posted("/ack"), [], "nothing is sent unclaimed")
        with failing_commit("awaiting_ack"):
            self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        self.assertIn(tid, gw._load_pending_acks(), "the wait could not be ended: the ack is kept")
        self.assertEqual(gw._post_ack_first(tid, "L9"), "stale", "an ack_first for a serve not held here")

    def test_a_ruling_holds_whatever_it_cannot_read_bind_or_record(self):
        tid = "task-ruling-holds"
        fake = self.served_acked(tid)
        self.core_writes(tid, "the answer")
        raw, generation = gw._pin_and_claim(tid)
        with mock.patch.object(gw, "_run_item", return_value=gw.task_attempts.UNKNOWN):
            self.assertEqual(gw._rule_result(tid, tid, raw, False, True, generation).why, "its run's records cannot be read")
        with mock.patch.object(gw, "_pin_holds", return_value=False):
            self.assertIn("withdrawn", gw._rule_result(tid, tid, raw, False, True, generation).why)
        with mock.patch.object(gw.task_attempts.Transition, "stop_by_result", return_value=False):
            self.assertIn("could not be recorded", gw._rule_result(tid, tid, raw, False, True, generation).why)
        with txn(tid) as t:
            for c in t.claims("result"):
                t.release_claim(c)
        self.assertIn("moved on", gw._rule_result(tid, tid, raw, False, True, generation).why)
        with failing_commit("claim result-"):
            self.assertIn("could not be claimed", gw._rule_result(tid, tid, raw, False, False, generation).why)
        with mock.patch.object(gw.task_attempts.Transition, "rec", new_callable=mock.PropertyMock,
                               return_value=gw.task_attempts.UNKNOWN):
            self.assertIn("cannot be read", gw._rule_result(tid, tid, raw, False, True, generation).why)
        del fake

    def test_a_ruling_held_for_a_pending_serve_lets_its_claim_go(self):
        tid = "task-ruling-pending"
        fake = self.served_acked(tid)
        self.core_writes(tid, "the answer")
        raw, generation = gw._pin_and_claim(tid)
        self.assertEqual([c["kind"] for c in gw._ATTEMPTS.get(tid)["claims"].values()], ["result"])
        self.reserved(tid, {tid})
        self.assertIn("re-serve waits", gw._rule_result(tid, tid, raw, False, True, generation).why)
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("claims"), "nothing was sent: the claim goes, so the switch can")
        self.assertEqual(gw._pin_and_claim(tid)[0], raw)
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("claims"), "and no new one is taken while the serve is held")
        del fake

    def disposed_unpublished(self, tid, body, fault):
        """Served, acked as generation 1; its result claimed at its pin, then disposed of unpublished."""
        fake = self.served_acked(tid)
        self.core_writes(tid, body)
        inflight = {tid}
        with fault:
            gw._post_ready_results(inflight)
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "the drain disposed of it")
        self.assertIsNone(gw.read_item(gw._delivery_core().backend.root, tid), "nothing was published")
        self.assertEqual(gw._ATTEMPTS.get(tid).get("claims"), None, "its claim ended with it")
        return fake, inflight

    def switches(self, tid, fake, inflight):
        """Served again under L2: its ack, retried up to three times, switches to generation 2, whose
        run is published and whose own answer posts; no claim is left behind."""
        inflight.add(tid)
        self.reserved(tid, inflight)
        fake.answers[f"/v1/tasks/{tid}/ack"] = _ack(tid, 2)
        acks = []
        for _ in range(3):
            acks.append(gw._post_task_ack(tid, durable=True, lease_id="L2"))
            if acks[-1]:
                break
        self.assertEqual(acks, [True], "the newer generation is not held")
        self.assertEqual(gw._ATTEMPTS.get(tid)["run"], f"{tid}~~2")
        self.answered_by_its_own_run(fake, tid, inflight)

    def test_a_result_refused_attribution_ends_its_claim_and_never_holds_a_newer_generation(self):
        tid = "task-unpublished-attribution"
        fake, inflight = self.disposed_unpublished(
            tid, "the answer", mock.patch.object(gw, "_attribution", return_value=("", True)))
        self.assertTrue(list((gw.RESULTS_DIR / "undelivered").glob(f"{tid}*")), "quarantined")
        self.switches(tid, fake, inflight)

    def test_a_dedup_requeue_ends_the_originals_claim_without_waiting_on_the_re_ask(self):
        tid = "task-unpublished-requeue"
        fake, inflight = self.disposed_unpublished(
            tid, "[deduped: task-holder-x]",
            mock.patch.object(gw, "_dedup_plan", return_value=("requeue", f"{tid}-reask", "")))
        self.switches(tid, fake, inflight)

    def test_a_dedup_report_refused_attribution_ends_its_claim(self):
        tid = "task-unpublished-report"
        with mock.patch.object(gw, "_owner_mention_disposition", return_value=""):
            fake, inflight = self.disposed_unpublished(
                tid, "[deduped: task-holder-y]",
                mock.patch.multiple(gw, _dedup_plan=mock.Mock(return_value=("report", "ask again", "")),
                                    _attribution=mock.Mock(return_value=("", True))))
        self.switches(tid, fake, inflight)

    def test_any_drain_exit_that_disposes_of_a_result_unpublished_ends_its_claim(self):
        tid = "task-unpublished-any"

        def disposes(t, rfile, *_a):
            rfile.rename(gw.RESULTS_DIR / f".{t}.elsewhere")
            disposal.pin_path(gw.RESULTS_DIR, t).unlink(missing_ok=True)
            return False
        disposal = gw.disposal
        fake, inflight = self.disposed_unpublished(tid, "the answer",
                                                   mock.patch.object(gw, "_deliver_ready", side_effect=disposes))
        self.switches(tid, fake, inflight)

    def test_a_send_really_in_progress_still_holds_the_newer_generation(self):
        for stage in ("pinned", "in the outbox"):
            with self.subTest(stage=stage):
                tid = f"task-in-progress-{stage.split()[-1]}"
                fake = self.served_acked(tid)
                self.core_writes(tid, "generation 1's answer")
                self.assertIsNotNone(gw._pin_and_claim(tid)[0])
                inflight = {tid}
                self.reserved(tid, inflight)
                fake.answers[f"/v1/tasks/{tid}/ack"] = _ack(tid, 2)
                if stage == "in the outbox":
                    op = next(iter(gw._ATTEMPTS.get(tid)["claims"]))
                    self.publish_claimed(tid, op)
                    gw.disposal.pin_path(gw.RESULTS_DIR, tid).unlink()
                    (gw.RESULTS_DIR / f"{tid}.txt").unlink()
                    sending = mock.patch.object(gw, "withdraw_unsent", return_value=False)
                else:
                    sending = contextlib.nullcontext()
                with sending:
                    acks = [gw._post_task_ack(tid, durable=True, lease_id="L2") for _ in range(3)]
                self.assertEqual(acks, [False, False, False], "its send may be under way: L2 waits")
                rec = gw._ATTEMPTS.get(tid)
                self.assertEqual((rec["run"], rec["pending_serve"]["lease"]), (tid, "L2"))
                self.assertEqual([c["kind"] for c in rec["claims"].values()].count("result"), 1, "its claim is kept")

    def publish_claimed(self, tid, op, item=None):
        """Publish an item as delivery does: its id recorded on the claim first."""
        item = item or tid
        with txn(tid) as t:
            claim = next(c for c in t.claims("result") if c["op"] == op)
            self.assertIsNotNone(t.note_item(claim, item))
        gw._delivery_core().backend.publish(item, b"{}", lifecycle={"op": op})
        return item

    def test_every_item_a_claim_publishes_is_recorded_and_a_close_on_a_later_runs_item_is_settled_before_a_switch(self):
        tid = "task-run-close"
        fake, inflight = self.held(tid)
        root = gw._delivery_core().backend.root
        published, unrecorded = [], []
        real_publish = type(gw._delivery_core().backend).publish

        def spy(backend, item_id, payload, *a, lifecycle=None, **k):
            if lifecycle:
                claim = (gw._ATTEMPTS.get(lifecycle["task"]).get("claims") or {}).get(lifecycle["op"]) or {}
                if item_id not in (claim.get("items") or []):
                    unrecorded.append(item_id)
                published.append(item_id)
            return real_publish(backend, item_id, payload, *a, lifecycle=lifecycle, **k)
        mock.patch.object(type(gw._delivery_core().backend), "publish", spy).start()
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        run = f"{tid}~~2"
        gw._post_task_beats(inflight, force=True)
        self.core_writes(run, "generation 2's answer")
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{run}.txt", (old, old))
        gw._post_ready_results(inflight)
        self.assertEqual((gw.read_item(root, f"{tid}.run-2") or {}).get("status"), "DELIVERED")
        with txn(tid) as t:                     # its attempt is held open, as a crash before the finish leaves it
            t.put(run=run, execution_generation=2, lease_id="L2")
        self.core_writes(run, "[no-send]")
        os.utime(gw.RESULTS_DIR / f"{run}.txt", (old, old))

        class Crash(BaseException):
            pass
        mock.patch.object(type(gw._delivery_core()), "deliver_one", side_effect=Crash()).start()
        drain = threading.Thread(target=lambda: self.assertRaises(
            Crash, gw._deliver_ready, run, gw.RESULTS_DIR / f"{run}.txt", *gw._pin_and_claim(run), inflight))
        drain.start()
        drain.join(10)
        mock.patch.stopall()
        mock.patch.object(gw, "_log", self.logs.append).start()
        self.use(fake)
        close = f"{tid}.run-2.lease-close"
        self.assertEqual(published[-1], close, "a close of a delivered run item rides that item's own close")
        self.assertIn((gw.read_item(root, close) or {}).get("status"), ("READY", "QUEUED"), "published, never sent")
        gw.disposal.pin_path(gw.RESULTS_DIR, run).unlink()
        (gw.RESULTS_DIR / f"{run}.txt").unlink()        # its reply retired: only the outbox item is left
        self.assertEqual(gw._write_task(_task(tid, lease_id="L3", source_event_id="$e")), (run, True))
        fake.answers[f"/v1/tasks/{tid}/ack"] = _ack(tid, 3)
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L3"))
        self.assertEqual(gw._ATTEMPTS.get(tid)["run"], f"{tid}~~3")
        self.assertIsNone(gw.read_item(root, close), "generation 2's close was taken back before the switch")
        gw._post_task_beats(inflight, force=True)
        self.assertTrue((gw.TASKS_DIR / f"{tid}~~3.txt").exists(), "generation 3's run is published")
        self.assertEqual(unrecorded, [], "every item is recorded on its claim before it is published")

    def stale_refused_then_served_again(self, tid):
        """Generation 1 runs and answers; the broker refuses the answer as stale (settled, archived);
        then L2 is served and acked as generation 2."""
        fake = self.served_acked(tid)
        fake.answers["/v1/results"] = _http(409, {"error_code": "stale_execution_generation"})
        self.core_writes(tid, "generation 1's answer")
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
        inflight = {tid}
        gw._post_ready_results(inflight)
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "settled and archived")
        self.assertIsNone(gw._ATTEMPTS.entry(tid), "generation 1's attempt is finished")
        fake.answers["/v1/results"] = {"ok": True}
        fake.answers[f"/v1/tasks/{tid}/ack"] = _ack(tid, 2)
        return fake, inflight

    def test_a_generation_served_after_the_earlier_was_refused_as_stale_runs_on_its_own_run_and_item(self):
        tid = "task-stale-then-l2"
        fake, inflight = self.stale_refused_then_served_again(tid)
        self.assertEqual(gw._write_task(_task(tid, lease_id="L2", source_event_id="$e")), (tid, True),
                         "held for its ack, never swallowed as already handled")
        self.assertFalse(any(f"dedup: {tid} already handled" in m for m in self.logs))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        run = f"{tid}~~2"
        self.assertEqual(gw._ATTEMPTS.get(tid)["run"], run)
        self.assertEqual(gw._ATTEMPTS.run_item(run), f"{tid}.run-2")
        def mine():
            return [p for p in fake.posted("/v1/results") if p["id"] == tid]
        before = len(mine())
        self.answered_by_its_own_run(fake, tid, inflight)
        self.assertEqual(len(mine()), before + 1, "generation 2 makes its own POST")

    def newer_run_file(self, tid, fake, inflight):
        """L2 served and acked as generation 2; its run published. The text of its task file."""
        self.assertEqual(gw._write_task(_task(tid, lease_id="L2", source_event_id="$e")), (tid, True))
        fake.answers[f"/v1/tasks/{tid}/ack"] = _ack(tid, 2)
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        gw._post_task_beats(inflight, force=True)
        return (gw.TASKS_DIR / f"{tid}~~2.txt").read_text()

    def test_a_newer_generations_task_says_what_became_of_the_earlier_answer(self):
        rules = gw.task_attempts.RERUN_RULES
        cases = {"refused": "not_delivered", "delivered": "answered", "sent in the room": "posted_in_room",
                 "journal unreadable": "unknown"}
        for case, verdict in cases.items():
            with self.subTest(case=case):
                tid = f"task-rerun-{case.split()[0]}"
                fake = self.served_acked(tid)
                first = (gw.TASKS_DIR / f"{tid}.txt").read_text()
                self.assertFalse(any(r in first for r in rules.values()), "a first run carries none")
                if case == "sent in the room":
                    with open(gw._STATE / "room-actions.jsonl", "a") as fh:
                        fh.write(json.dumps({"v": 2, "record_type": "dispatch_intent", "ts": 4e9, "seq": 1,
                                             "turn_id": f"{tid}_g1_01X", "task_id": tid, "execution_generation": 1,
                                             "operation_id": "op1", "action": "room.message.send"}) + "\n")
                if case != "delivered":
                    fake.answers["/v1/results"] = _http(409, {"error_code": "stale_execution_generation"})
                self.core_writes(tid, "generation 1's answer")
                old = time.time() - 5
                os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
                inflight = {tid}
                gw._post_ready_results(inflight)
                self.assertIsNone(gw._ATTEMPTS.entry(tid), "generation 1 settled")
                fake.answers["/v1/results"] = {"ok": True}
                broken = (mock.patch.object(gw.task_attempts.agent_turn, "read_turn_records",
                                            return_value=gw.task_attempts.agent_turn.TurnRecords([], [], False))
                          if case == "journal unreadable" else contextlib.nullcontext())
                with broken:
                    text = self.newer_run_file(tid, fake, inflight)
                self.assertIn(rules[verdict], text)
                self.assertEqual([r for r in rules.values() if r in text], [rules[verdict]], "exactly one")
                (gw._STATE / "room-actions.jsonl").unlink(missing_ok=True)

    def test_a_newer_run_is_published_only_once_what_became_of_the_earlier_answer_can_be_read(self):
        rules = gw.task_attempts.RERUN_RULES
        for case, verdict in (("refused", "not_delivered"), ("delivered", "answered")):
            for fault in ("ledger", "reserve"):
                with self.subTest(case=case, fault=fault):
                    tid = f"task-unread-{case}-{fault}"
                    fake = self.served_acked(tid)
                    if case == "refused":
                        fake.answers["/v1/results"] = _http(409, {"error_code": "stale_execution_generation"})
                    self.core_writes(tid, "generation 1's answer")
                    old = time.time() - 5
                    os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
                    inflight = {tid}
                    gw._post_ready_results(inflight)
                    fake.answers["/v1/results"] = {"ok": True}
                    self.assertEqual(gw._write_task(_task(tid, lease_id="L2", source_event_id="$e")), (tid, True))
                    fake.answers[f"/v1/tasks/{tid}/ack"] = _ack(tid, 2)
                    self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
                    run, reserve = f"{tid}~~2", gw._reserve_path(tid)
                    real_read = gw.Path.read_text
                    real_view = gw._ATTEMPTS.owed_view

                    def failed_read(wire):     # one failed read of the ledger, by the production reader
                        with mock.patch.object(gw._ATTEMPTS, "_read", return_value=None):
                            return real_view(wire)
                    stuck = (mock.patch.object(gw._ATTEMPTS, "owed_view", side_effect=failed_read)
                             if fault == "ledger" else mock.patch.object(
                                 gw.Path, "read_text", autospec=True, side_effect=lambda p, *a, **k: (
                                     (_ for _ in ()).throw(PermissionError(13, "denied")) if p == reserve
                                     else real_read(p, *a, **k))))
                    with stuck:
                        gw._post_task_beats(inflight, force=True)
                    self.assertFalse((gw.TASKS_DIR / f"{run}.txt").exists(), "withheld while it cannot be read")
                    self.assertEqual((gw._ATTEMPTS.get(tid)["owed_publish"], reserve.exists()), (run, True),
                                     "still owed, its serve kept")
                    self.assertIsNone(gw._ATTEMPTS.get(tid).get("dispatch_lost"), "never failed for a read error")
                    gw._post_task_beats(inflight, force=True)
                    text = (gw.TASKS_DIR / f"{run}.txt").read_text()
                    self.assertEqual([r for r in rules.values() if r in text], [rules[verdict]], "the right line, once")
                    self.assertEqual(text.count("task: "), 1)
                    self.assertIsNone(gw._ATTEMPTS.get(tid).get("owed_publish"))
                    self.assertFalse(reserve.exists())

    def test_the_same_lease_served_again_after_its_run_finished_is_still_a_redelivery(self):
        tid = "task-stale-then-l1"
        fake, _ = self.stale_refused_then_served_again(tid)
        self.assertEqual(gw._write_task(_task(tid, lease_id="L1", source_event_id="$e")), (tid, True))
        self.assertTrue(any(f"dedup: {tid} already handled" in m for m in self.logs), "the same serve")
        self.assertFalse((gw.TASKS_DIR / f"{tid}~~2.txt").exists())
        del fake

    def test_a_served_again_generation_the_finished_run_already_settled_runs_nothing(self):
        tid = "task-stale-then-same-gen"
        fake, inflight = self.stale_refused_then_served_again(tid)
        fake.answers[f"/v1/tasks/{tid}/ack"] = _ack(tid, 1)
        self.assertEqual(gw._write_task(_task(tid, lease_id="L2", source_event_id="$e")), (tid, True))
        gw._post_task_ack(tid, durable=True, lease_id="L2")
        gw._post_task_beats(inflight, force=True)
        self.assertEqual(sorted(p.name for p in gw.TASKS_DIR.glob(f"{tid}*")), [], "nothing re-runs")
        self.assertIsNone(gw._ATTEMPTS.entry(tid))

    def test_every_run_after_the_first_has_its_own_item_even_when_its_task_left_no_trace(self):
        tid = "task-pruned-run"
        fake, inflight = self.stale_refused_then_served_again(tid)
        with gw._ATTEMPTS.transition(tid) as t:      # retained state pruned: no lease to judge by
            t._change(lambda d, r: (d.get("runs", {}).pop(tid, None), r)[1], "prune")
        for p in (gw.TASKS_DIR / "archive").rglob(f"{tid}.txt"):
            p.unlink()
        for p in gw.ARCHIVE_RESULTS_DIR.rglob(f"{tid}*.txt"):
            p.unlink()
        self.assertEqual(gw._write_task(_task(tid, lease_id="L2", source_event_id="$e")), (tid, True),
                         "a finished run it knows nothing of: held for its ack")
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        self.assertEqual(gw._ATTEMPTS.run_item(f"{tid}~~2"), f"{tid}.run-2", "never the task's own item")
        self.answered_by_its_own_run(fake, tid, inflight)
        with txn("task-fresh") as t:
            first = t.new_run("task-fresh")
            second = t.new_run("task-fresh", current=False)
        self.assertEqual((gw._ATTEMPTS.run_item(first), gw._ATTEMPTS.run_item(second)),
                         (None, "task-fresh.run-2"), "the first run has the task's item, every later one its own")

    def test_an_accepted_ack_its_generation_cannot_reach_returns_no_success_and_is_retried(self):
        tid = "task-ack-unlanded"
        fake, inflight = self.held(tid)
        self.assertTrue(gw._record_pending_acks([(tid, True, "L2")]))
        with mock.patch.object(gw, "_commit_ack", return_value="stale"):
            self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L2"),
                             "the serve is still held for it: not done")
        self.assertIn(tid, gw._load_pending_acks(), "kept for the retry")
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"), "the retry lands it")
        self.assertEqual(gw._ATTEMPTS.get(tid)["owed_publish"], f"{tid}~~2")
        self.answered_by_its_own_run(fake, tid, inflight)

    def test_finishing_the_earlier_generation_keeps_a_newer_serves_accepted_ack(self):
        tid = "task-finish-vs-ack"
        fake = self.served_acked(tid)
        fake.answers["/v1/results"] = _http(409, {"error_code": "stale_execution_generation"})
        self.core_writes(tid, "generation 1's answer")
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
        at_retire, go_retire, at_commit, go_commit = (threading.Event() for _ in range(4))
        real_retired, real_commit = gw._retired, gw._commit_ack

        def retired(*a, **k):
            at_retire.set()
            go_retire.wait(10)
            return real_retired(*a, **k)

        def commit(*a, **k):
            at_commit.set()
            go_commit.wait(10)
            return real_commit(*a, **k)
        mock.patch.object(gw, "_retired", side_effect=retired).start()
        mock.patch.object(gw, "_commit_ack", side_effect=commit).start()
        inflight = {tid}
        drain = threading.Thread(target=gw._post_ready_results, args=(inflight,))
        drain.start()
        self.assertTrue(at_retire.wait(10), "generation 1 refused as stale, paused before its retirement")
        self.assertEqual(gw._write_task(_task(tid, lease_id="L2", source_event_id="$e")), (tid, True))
        fake.answers[f"/v1/tasks/{tid}/ack"] = _ack(tid, 2)
        acked = []
        ack = threading.Thread(target=lambda: acked.append(gw._post_task_ack(tid, durable=True, lease_id="L2")))
        ack.start()
        self.assertTrue(at_commit.wait(10), "L2's ack accepted as generation 2, paused before its commit")
        go_retire.set()
        drain.join(10)
        self.assertEqual(gw._ATTEMPTS.get(tid)["pending_serve"]["lease"], "L2")
        go_commit.set()
        ack.join(10)
        mock.patch.stopall()
        mock.patch.object(gw, "_log", self.logs.append).start()
        self.use(fake)
        if not acked[0]:
            self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"), "kept and retried")
        self.assertEqual(gw._ATTEMPTS.get(tid)["run"], f"{tid}~~2")
        fake.answers["/v1/results"] = {"ok": True}
        self.answered_by_its_own_run(fake, tid, inflight)

    def test_a_failed_publication_marker_never_rebinds_a_newer_lease_to_the_earlier_run(self):
        for finished in (False, True):
            with self.subTest(finished=finished):
                tid = f"task-marker-{'finished' if finished else 'running'}"
                with failing_commit("the run's publication"):
                    self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1", source_event_id="$e"), set(), []))
                self.assertTrue((gw.TASKS_DIR / f"{tid}.txt").exists())
                self.assertFalse(gw._ATTEMPTS.run_published(tid) if hasattr(gw._ATTEMPTS, "run_published")
                                 else ((ledger().get("runs") or {}).get(tid) or {}).get("published"))
                fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1), "/v1/results": {"ok": True},
                                               "/v1/tasks/heartbeat": {"ok": True}}))
                self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L1"))
                inflight = {tid}
                if finished:
                    fake.answers["/v1/results"] = _http(409, {"error_code": "stale_execution_generation"})
                    self.core_writes(tid, "generation 1's answer")
                    old = time.time() - 5
                    os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
                    gw._post_ready_results(inflight)
                    self.assertIsNone(gw._ATTEMPTS.entry(tid))
                    fake.answers["/v1/results"] = {"ok": True}
                self.assertEqual(gw._write_task(_task(tid, lease_id="L2", source_event_id="$e")), (tid, True))
                self.assertEqual(gw._ATTEMPTS.get(tid)["pending_serve"]["lease"], "L2", "held for its ack")
                self.assertFalse(any(f"dedup: {tid} already handled" in m for m in self.logs))
                fake.answers[f"/v1/tasks/{tid}/ack"] = _ack(tid, 2)
                self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
                self.assertEqual(gw._ATTEMPTS.get(tid)["run"], f"{tid}~~2", "never the earlier run")
                self.answered_by_its_own_run(fake, tid, inflight)

    def paused_before_publish(self, tid):
        """Served, acked as generation 1; its result pinned and claimed by a worker paused before its
        publish; then served again under L2."""
        fake = self.served_acked(tid)
        self.core_writes(tid, "generation 1's answer")
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
        raw, generation = gw._pin_and_claim(tid)
        inflight = {tid}
        self.reserved(tid, inflight)
        fake.answers[f"/v1/tasks/{tid}/ack"] = _ack(tid, 2)
        return fake, inflight, raw, generation

    def held_three_times(self, tid):
        acks = [gw._post_task_ack(tid, durable=True, lease_id="L2") for _ in range(3)]
        self.assertEqual(acks, [False, False, False], "not known to be unsent: L2 waits")
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["run"], rec["execution_generation"], rec["pending_serve"]["lease"]), (tid, 1, "L2"))
        self.assertEqual([c["kind"] for c in rec["claims"].values()].count("result"), 1, "its claim is kept")
        self.assertTrue(any(f"task {tid}: result claim" in m and "newer generation waits" in m for m in self.logs),
                        "the hold is logged with what unsticks it")

    def test_an_unreadable_reply_keeps_its_claim_and_the_paused_send_posts_before_any_switch(self):
        for fault in ("unreadable", "unstattable"):
            with self.subTest(fault=fault):
                tid = f"task-unreadable-{fault}"
                fake, inflight, raw, generation = self.paused_before_publish(tid)
                names = {str(gw.RESULTS_DIR / f"{tid}.txt"), str(gw.disposal.pin_path(gw.RESULTS_DIR, tid))}
                real_lstat = os.lstat
                if fault == "unreadable":
                    os.chmod(gw.RESULTS_DIR / f"{tid}.txt", 0)
                    stuck = contextlib.nullcontext()
                else:
                    stuck = mock.patch.object(gw.os, "lstat", side_effect=lambda p, *a, **k: (
                        (_ for _ in ()).throw(PermissionError(13, "denied")) if str(p) in names
                        else real_lstat(p, *a, **k)))
                with stuck:
                    self.held_three_times(tid)
                os.chmod(gw.RESULTS_DIR / f"{tid}.txt", 0o644)
                gw._deliver_ready(tid, gw.RESULTS_DIR / f"{tid}.txt", raw, generation, inflight)
                self.assertEqual(fake.posted("/v1/results"), [], "the paused send resumes into the hold: nothing posts")
                self.assertNotIn("result", [c["kind"] for c in (gw._ATTEMPTS.get(tid).get("claims") or {}).values()],
                                 "and nothing was sent, so its result claim goes")
                self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"), "settled: the switch follows")
                gw._post_ready_results(inflight)
                self.assertEqual(fake.posted("/v1/results"), [], "generation 1's reply is never posted after it")
                self.answered_by_its_own_run(fake, tid, inflight)

    def test_an_unreadable_outbox_item_keeps_its_claim_until_the_operator_clears_it(self):
        tid = "task-unreadable-item"
        fake, inflight, _, _ = self.paused_before_publish(tid)
        op = next(iter(gw._ATTEMPTS.get(tid)["claims"]))
        root = gw._delivery_core().backend.root
        self.publish_claimed(tid, op)
        item = sys.modules[gw.read_item.__module__]._item_path(root, tid)
        item.write_text("{not json")
        gw.disposal.pin_path(gw.RESULTS_DIR, tid).unlink()
        (gw.RESULTS_DIR / f"{tid}.txt").unlink()
        self.held_three_times(tid)
        item.unlink()                           # the operator's removal: read as no item
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        self.answered_by_its_own_run(fake, tid, inflight)

    def test_a_name_or_item_that_cannot_be_read_is_never_taken_as_absent(self):
        tid = "task-cannot-tell"
        fake = self.served_acked(tid)
        name = gw.RESULTS_DIR / f"{tid}.txt"
        real_lstat = os.lstat
        unknown = mock.patch.object(gw.os, "lstat", side_effect=lambda p, *a, **k: (
            (_ for _ in ()).throw(PermissionError(13, "denied")) if str(p).startswith(str(gw.RESULTS_DIR / tid))
            or str(p).startswith(str(gw.RESULTS_DIR / f".{tid}")) else real_lstat(p, *a, **k)))
        self.assertIs(gw._on_disk(name), False)
        with unknown:
            self.assertIsNone(gw._on_disk(name))
            self.assertTrue(gw._run_id_used(tid), "a run id that cannot be told is never reused")
            with txn(tid) as t:
                self.assertFalse(gw._stop_run(t, tid, "test"), "nor stopped as unanswered")
                t.put(owed_close={"gen": 1, "why": "closed"})
                gw._withdraw_closed(t, "closed")
                self.assertTrue(t.rec.get("owed_close"), "its close stays owed")
                t.put(stop_ctl="task-ctl-x", stop_requested=True)
            self.assertIsNone(gw._ctl_written(f"{tid}-ctl"), "a control task that cannot be told is neither written nor cleared")
            self.assertTrue((gw.TASKS_DIR / f"{tid}.txt").exists(), "nothing was withdrawn")
        root = gw._delivery_core().backend.root
        gw._delivery_core().backend.publish(tid, b"{}")
        with mock.patch.object(gw, "read_item", side_effect=PermissionError(13, "denied")):
            self.assertEqual(gw._outbox_record(root, tid), gw.task_attempts.UNKNOWN)
        sys.modules[gw.read_item.__module__]._item_path(root, tid).write_text("{not json")
        self.assertEqual(gw._outbox_record(root, tid), gw.task_attempts.UNKNOWN)
        self.assertFalse(gw._result_disposed(tid))
        del fake

    def test_a_different_reply_at_the_name_does_not_keep_a_disposed_replys_claim(self):
        tid = "task-other-reply"
        self.served_acked(tid)
        self.core_writes(tid, "the first reply")
        self.assertIsNotNone(gw._pin_and_claim(tid)[0])
        gw.disposal.pin_path(gw.RESULTS_DIR, tid).unlink()
        (gw.RESULTS_DIR / f"{tid}.txt").unlink()
        self.core_writes(tid, "a later reply")
        self.assertTrue(gw._end_unpublished_claims(tid), "the claimed reply itself is gone, unpublished")
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("claims"))

    def test_a_claim_left_open_by_a_crash_is_ended_by_the_reconcile_only_once_its_file_is_gone(self):
        tid = "task-unpublished-crash"
        self.served_acked(tid)
        self.core_writes(tid, "the answer")
        self.assertIsNotNone(gw._pin_and_claim(tid)[0])
        self.assertFalse(gw._reconcile_earlier_run(tid), "its file is still there: it may be about to send")
        self.assertEqual(len(gw._ATTEMPTS.get(tid)["claims"]), 1)
        gw.disposal.pin_path(gw.RESULTS_DIR, tid).unlink()
        self.assertFalse(gw._reconcile_earlier_run(tid), "its pin is gone, its file is not")
        (gw.RESULTS_DIR / f"{tid}.txt").unlink()
        self.assertTrue(gw._reconcile_earlier_run(tid), "gone and never in the outbox: nothing was sent")
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("claims"))

    def test_a_pin_that_cannot_be_made_or_finds_no_file_is_retried(self):
        tid = "task-pin-fails"
        self.served_acked(tid)
        self.core_writes(tid, "the answer")
        with mock.patch.object(gw.disposal, "pin", side_effect=FileNotFoundError()):
            self.assertEqual(gw._pin_and_claim(tid), (None, None))
        with mock.patch.object(gw.disposal, "pin", side_effect=PermissionError("denied")):
            self.assertEqual(gw._pin_and_claim(tid), (None, None))
        self.assertTrue(any("not pinned" in m for m in self.logs))
        self.assertIsNotNone(gw._pin_and_claim(tid)[0])

    def test_a_delivery_for_another_attempt_is_kept_and_settles_nothing_of_the_current_one(self):
        tid = "task-delivered-elsewhere"
        self.served_acked(tid)
        with txn(tid) as t:
            claim = t.claim("result", file=[1])
        record = {"lifecycle": {"op": "result-other", "task": tid, "run": "elsewhere", "execution_generation": 9}}
        self.assertEqual(gw._committed(tid, tid, claim, record), "kept")
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("claims"), "its claim is let go")
        self.assertFalse(gw._delivered_for_current_attempt(record), "the current attempt is another")
        self.assertTrue(gw._delivered_for_current_attempt({"lifecycle": {**record["lifecycle"], "run": tid,
                                                                        "execution_generation": 1}}))
        with mock.patch.object(gw._ATTEMPTS, "entry", return_value=gw.task_attempts.UNKNOWN):
            self.assertFalse(gw._delivered_for_current_attempt(record), "unknown settles nothing")
        self.assertTrue(gw._delivered_for_current_attempt({}), "an item with no claim is the outbox's own")

    def test_a_closed_run_holds_nothing_in_flight_and_a_result_worker_isolates_its_failure(self):
        tid = "task-closed-run"
        _, inflight = self.held(tid)
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        self.assertFalse(gw._broker_holds(tid), "the earlier run's id holds nothing")
        self.assertTrue(gw._broker_holds(f"{tid}~~2"))
        with mock.patch.object(gw, "_deliver_ready", side_effect=RuntimeError("boom")):
            gw._delivery_worker(tid, gw.RESULTS_DIR / f"{tid}.txt", "x", None, inflight)
        self.assertTrue(any("delivery error (isolated)" in m for m in self.logs))
        self.assertFalse(gw._delivering(tid))

    def test_the_idempotency_key_rides_the_real_request(self):
        seen = {}

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

            def getheader(self, *_a):
                return "application/json"
            headers = {}

        def urlopen(req, timeout=None):
            seen.update(req.header_items())
            return Resp()
        with mock.patch.object(gw.urllib.request, "urlopen", side_effect=urlopen):
            try:
                gw._req("POST", "/v1/tasks/task-key/ack", {"id": "task-key"}, operation_id="ack-0123")
            except Exception:  # noqa: BLE001 — only the request it built is under test
                pass
        self.assertEqual(seen.get("Idempotency-key"), "ack-0123")

    # ── enforcement: one writer, provider calls with no transition held ──
    READERS = {"transition", "entry", "records", "owing", "holds", "beating", "wake_item", "run_item", "owed_view", "run_of",
               "ack_fields", "worker_id", "broker_names_leases", "report_superseded", "sweep",
               "close_finished_turns", "beat_snapshot", "now", "take_ownership", "task_beat_disabled", "unowned"}

    def attempts_misuse(self, src):
        """Every `_ATTEMPTS.<name>` the bridge source uses that is not a reader or the transition."""
        tree = ast.parse(src)
        return sorted({n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                       and isinstance(n.value, ast.Name) and n.value.id == "_ATTEMPTS"} - self.READERS)

    def test_the_bridge_changes_an_attempt_only_through_its_transition(self):
        src = Path(gw.__file__).read_text()
        self.assertEqual(self.attempts_misuse(src), [])
        self.assertEqual(self.attempts_misuse("_ATTEMPTS._commit(f)\n_ATTEMPTS.put(x=1)\n"), ["_commit", "put"],
                         "positive control: a write that skips the transition is caught")
        public = {n for n in dir(gw.task_attempts.TaskAttempts) if not n.startswith("_")}
        self.assertEqual(public - self.READERS - {"get", "result_fields"}, set(),
                         "the owner itself exposes no mutator but the transition")
        self.assertEqual(sorted({n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
                                & {"_set", "_update", "_merge"}), [])

    def test_a_provider_call_inside_a_transition_is_refused(self):
        with gw._ATTEMPTS.transition("task-under-lock"):
            with self.assertRaises(gw.task_attempts.UnderLock):
                gw._req("POST", "/v1/tasks/task-under-lock/ack", {})
            with self.assertRaises(gw.task_attempts.UnderLock):
                FakeGateway()("POST", "/v1/results", {})
        self.assertEqual(FakeGateway()("GET", "/v1/x"), {}, "positive control: outside it, calls go")


if __name__ == "__main__":
    unittest.main()
