#!/usr/bin/env python3
"""The bridge's task-lifecycle hooks, in process (REMOTE_TASK_LIFECYCLE_V2 + REMOTE_TASK_TURN_CONTEXT).

The real bridge module, imported hermetically with both flags on, against a fake `_req`:
the ack paths, the per-task beat riding the client heartbeat, a cancel reaching the core,
the ledger reconcile, the startup turn sweep, the result gate, and the control-task sweep.

Run: python3 tests/gateway-agent-activity-hooks.test.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gw, _READS = isolate_then_import(env={"REMOTE_TASK_LIFECYCLE_V2": "1", "REMOTE_TASK_TURN_CONTEXT": "1"})
agent_turn = gw.task_attempts.agent_turn


def _http(code, body):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


class FakeGateway:
    def __init__(self, **answers):
        self.calls, self.answers = [], answers

    def __call__(self, method, path, payload=None, timeout=None, **_k):
        self.calls.append((method, path, payload))
        ans = self.answers.get(path.split("?")[0])
        if isinstance(ans, BaseException):
            raise ans
        return ans(payload) if callable(ans) else (ans if ans is not None else {})

    def posted(self, suffix):
        return [p for _m, path, p in self.calls if path.endswith(suffix)]


def _task(tid, **extra):
    return {"id": tid, "task": "reply", "source": "ag2space", "user_id": "@o:x", "access_tier": "owner",
            **extra}


def _ack(tid, gen=1):
    return {"ok": True, "durable": True, "task_id": tid, "execution_generation": gen, "attempt": gen,
            "lease_expires_at": 1761354125.0}


class Hooks(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(gw, "_log", self.logs.append).start()

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

    def test_the_ack_first_path_records_the_generation_or_refuses_a_cancel(self):
        self.use(FakeGateway(**{"/v1/tasks/task-h3/ack": _ack("task-h3", 2)}))
        self.assertEqual(gw._post_ack_first("task-h3", None), "accepted")
        self.assertEqual(gw._ATTEMPTS.get("task-h3")["execution_generation"], 2)
        fake = self.use(FakeGateway(**{"/v1/tasks/task-h3b/ack": _http(409, {"error_code": "task_cancelled"})}))
        self.assertEqual(gw._post_ack_first("task-h3b", None), "stale")
        self.assertEqual(self.controls("task-h3b"), [], "a staged task never reached the core")
        self.assertEqual(len(fake.posted("/cancel-ack")), 1)

    def test_the_client_heartbeat_carries_a_per_task_beat_that_can_fence(self):
        gw._ATTEMPTS.record_ack("task-h4", _ack("task-h4", 1))
        fake = self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True, "task_errors": [
            {"task_id": "task-h4", "error_code": "stale_execution_generation"},
            {"task_id": "task-elsewhere", "error_code": "task_cancelled"}]}}))
        gw._post_heartbeat({"task-h4"}, force=True)
        beat = fake.posted("/v1/tasks/heartbeat")[0]
        self.assertEqual(beat["tasks"][0]["task_id"], "task-h4")
        self.assertEqual(len(self.controls("task-h4")), 1)
        self.assertEqual(self.controls("task-elsewhere"), [], "only this node's attempts are fenced")

    def test_a_newer_generation_served_here_withdraws_the_old_generations_cancel(self):
        tid = "task-regen"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True, "task_errors": [
            {"task_id": tid, "error_code": "stale_execution_generation"}]}}))
        gw._post_heartbeat({tid}, force=True)
        self.assertEqual(len(self.controls(tid)), 1, "no newer attempt here yet: the core is stopped")
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        self.assertEqual(self.controls(tid), [], "generation 1's cancel must not stop generation 2")
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["execution_generation"], rec.get("fenced"), rec.get("fence_ctl")), (2, None, None))
        self.assertTrue(any("withdrawn" in m for m in self.logs))

    def test_a_cancel_the_core_already_took_is_not_withdrawn(self):
        tid = "task-regen-taken"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True, "task_errors": [
            {"task_id": tid, "error_code": "stale_execution_generation"}]}}))
        gw._post_heartbeat({tid}, force=True)
        ctl = self.controls(tid)[0]
        taken = ctl.with_name(ctl.stem + ".claimed-core")
        ctl.rename(taken)
        self.addCleanup(lambda: taken.unlink(missing_ok=True))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        self.assertTrue(taken.exists())
        self.assertTrue(any("already taken" in m for m in self.logs))

    def taken_stale_cancel(self, tid):
        """Generation 1 fenced, its cancel taken by the core, then generation 2 acked here."""
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True, "task_errors": [
            {"task_id": tid, "error_code": "stale_execution_generation"}]}}))
        gw._post_heartbeat({tid}, force=True)
        ctl = self.controls(tid)[0]
        ctl.unlink()
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2), "/v1/results": {"ok": True},
                                       "/v1/tasks/heartbeat": {"ok": True}}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        self.addCleanup(lambda: (gw.RESULTS_DIR / f"{tid}.txt").unlink(missing_ok=True))
        return fake, ctl.stem

    def test_a_newer_generation_stopped_by_the_taken_cancel_is_posted_as_failed(self):
        tid = "task-regen-stopped"
        fake, ctl = self.taken_stale_cancel(tid)
        gw._post_task_beats({tid}, force=True)
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "the core has not handled the cancel yet")
        (gw.RESULTS_DIR / f"{ctl}.txt").write_text("[no-send]\n")
        self.addCleanup(lambda: (gw.RESULTS_DIR / f"{ctl}.txt").unlink(missing_ok=True))
        (gw.TASKS_DIR / "archive").mkdir(exist_ok=True)
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
        gw._post_task_beats({tid}, force=True)
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.STALE_CANCEL_RESULT)
        self.assertFalse(gw._ATTEMPTS.beating({tid}))
        self.assertTrue(gw._deliver_result_payload(tid, tid, "stopped", no_send=True))
        post = fake.posted("/v1/results")[0]
        self.assertEqual((post["status"], post["no_send"], post["execution_generation"]), ("failed", True, 2))

    def test_a_newer_generation_the_core_still_answers_posts_its_own_result(self):
        tid = "task-regen-answered"
        _, ctl = self.taken_stale_cancel(tid)
        (gw.RESULTS_DIR / f"{ctl}.txt").write_text("[no-send]\n")
        self.addCleanup(lambda: (gw.RESULTS_DIR / f"{ctl}.txt").unlink(missing_ok=True))
        gw._post_task_beats({tid}, force=True)
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "its task file is still queued")
        (gw.RESULTS_DIR / f"{tid}.txt").write_text("the answer\n")
        (gw.TASKS_DIR / f"{tid}.txt").unlink()
        gw._post_task_beats({tid}, force=True)
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), "the answer\n")
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("taken_ctl"))

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
        gw._ATTEMPTS.record_ack("task-v2seen", _ack("task-v2seen", 1))
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
        gw._ATTEMPTS.fence("task-cap", {"error_code": "task_already_terminal"})
        self.assertTrue(gw._deliver_result_payload("task-cap", "task-cap", "answer"), "a fence ends the wait")
        self.assertEqual(fake.posted("/v1/results"), [])

    def test_a_held_result_is_settled_only_on_an_explicit_terminal_answer(self):
        gw._ATTEMPTS.record_ack("task-v2seen2", _ack("task-v2seen2", 1))
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
        gw._ATTEMPTS.remember_task("task-late", _task("task-late"))
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
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        fake = self.closed_by_beat(tid, reason="question_resolved")
        self.assertFalse((gw.TASKS_DIR / f"{tid}.txt").exists(), "the core never runs it")
        self.assertEqual(self.controls(tid), [])
        self.assertTrue(any("question_resolved" in m and "withdrawn" in m for m in self.logs))
        self.assertTrue(gw._deliver_result_payload(tid, tid, "late"))
        self.assertEqual(fake.posted("/v1/results"), [], "nothing reaches the room")

    def test_a_closed_task_the_core_has_taken_up_is_stopped_not_withdrawn(self):
        tid = "task-closed-taken"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
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
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
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
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
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
        gw._ATTEMPTS.remember_task(tid, _task(tid))
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
        self.assertIsNone(gw._ATTEMPTS.result_hold(tid), "nothing left to wait for")
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

        self.assertEqual(gw._write_task(dict(wake)), (tid, True), "served again, it runs")
        self.assertIn('answered "blue"', (gw.TASKS_DIR / f"{tid}.txt").read_text())
        self.assertTrue(gw._post_task_ack(tid, durable=True))
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
        self.assertFalse((gw.TASKS_DIR / f"{tid}.txt").exists(), "and is not published")
        self.assertNotIn(tid, inflight)
        self.assertTrue(gw._take_served_task(dict(wake), inflight, []), "served again, it runs and is tracked")
        self.assertIn(tid, inflight)
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        gw._close_stale_turns(inflight)
        self.assertIsNotNone(agent_turn.open_turn_for_task(gw._STATE, tid), "its turn survives the sweep")

    def test_a_wake_admitted_after_a_stale_disposal_judgement_is_not_settled_away(self):
        tid = "task-stale-disposed"
        self.assertIsNotNone(gw._write_task(_task(tid)))
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
        (gw.RESULTS_DIR / "archive" / f"{tid}.txt").write_text("I asked which one.")
        gw._ATTEMPTS.finish(tid)
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
        self.assertEqual(gw._write_task(dict(wake)), (tid, True), "served again, it runs")
        self.assertTrue((gw.TASKS_DIR / f"{tid}.txt").exists())

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
        gw._ATTEMPTS.record_ack("task-moved", _ack("task-moved", 1))
        earlier = gw._ATTEMPTS.get("task-moved")
        gw._ATTEMPTS.open_wake("task-moved", {"blocker_episode_id": "e2"})
        self.assertFalse(gw._ATTEMPTS.finish("task-moved", earlier))
        self.assertTrue(agent_turn.is_closed(gw._STATE, earlier["turn_id"]))
        self.assertEqual(gw._ATTEMPTS.get("task-moved")["wake_episode"], "e2", "the wake is kept")
        self.assertEqual(gw._ATTEMPTS.open_wake("task-moved", {"blocker_episode_id": "e2"}), "e2",
                         "and not marked as already run")

    def test_a_new_wake_starts_a_new_journal_boundary(self):
        self.assertTrue(gw._take_served_task(_task("task-wake-boundary"), set(), []))
        first = gw._ATTEMPTS.get("task-wake-boundary")["published_at"]
        with mock.patch.object(gw._ATTEMPTS, "now", lambda: first + 50):
            gw._ATTEMPTS.open_wake("task-wake-boundary", {"blocker_episode_id": "e7"})
            gw._ATTEMPTS.remember_task("task-wake-boundary", _task("task-wake-boundary"))
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

    def held_then_archived(self, tid):
        """A served, acked attempt whose task file the core archived unanswered (the orphan pass)."""
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1), "/v1/results": {"ok": True}}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        (gw.TASKS_DIR / "archive").mkdir(exist_ok=True)
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
        clock = [time.time()]
        mock.patch.object(gw._ATTEMPTS, "now", lambda: clock[0]).start()
        self.addCleanup(lambda: (gw.RESULTS_DIR / f"{tid}.txt").unlink(missing_ok=True))
        return fake, clock

    def beat(self, tid):
        gw._watch_dispatch(tid)
        return gw._ATTEMPTS.beating({tid})

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
        self.assertEqual(self.controls(tid), [], "a task the core never took has no turn to stop")
        self.assertTrue(gw._deliver_result_payload(tid, tid, "the task left the queue", no_send=True))
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
        self.assertEqual(gw._write_task({**task, "wake": wake}), (tid, True))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1), "/v1/results": {"ok": True}}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
        clock = [time.time()]
        mock.patch.object(gw._ATTEMPTS, "now", lambda: clock[0]).start()
        self.addCleanup(lambda: (gw.RESULTS_DIR / f"{tid}.txt").unlink(missing_ok=True))
        self.beat(tid)
        clock[0] += gw.task_attempts.DISPATCH_LOST_S
        self.assertFalse(self.beat(tid), "the first attempt's archived reply does not answer the wake")
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.DISPATCH_LOST_RESULT)

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
        self.assertEqual(len(self.controls(tid)), 1, "a turn parked on a prompt gets the Stop path's Escape")
        self.beat(tid)
        self.assertEqual(len(self.controls(tid)), 1, "asked once")
        self.assertTrue(gw._deliver_result_payload(tid, tid, "stopped working", no_send=True))
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
            self.assertTrue(self.beat(tid), "not marked lost while its result is unwritten")
        self.assertTrue(any("retried next beat" in m for m in self.logs))
        self.assertFalse(self.beat(tid))
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.DISPATCH_LOST_RESULT)

    def test_a_cancel_instruction_that_cannot_be_written_is_reported(self):
        with mock.patch.object(gw, "_durable_write", return_value=False):
            gw._queue_cancel_instruction("task-h5", "task_cancelled")
        self.assertTrue(any("cancel instruction not written" in m for m in self.logs))

    def test_the_ledger_reconcile_closes_what_the_server_closed(self):
        gw._ATTEMPTS.record_ack("task-h6", _ack("task-h6", 1))
        gw._ATTEMPTS._update("task-h6", acked_at=0)
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
        turn = gw._ATTEMPTS.record_ack("task-h9", _ack("task-h9", 1))["turn_id"]
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
        turn = gw._ATTEMPTS.record_ack("task-h7", _ack("task-h7", 1))["turn_id"]
        gw._close_stale_turns(set())
        self.assertTrue(agent_turn.is_closed(gw._STATE, turn))
        self.assertTrue(any(m.startswith("closed ") and "turn file" in m for m in self.logs))

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
        self.assertEqual(gw._write_task({**task, "wake": wake}), ("task-h8", True))
        body = next(line for line in (gw.TASKS_DIR / "task-h8.txt").read_text().splitlines()
                    if line.startswith("task: "))
        self.assertIn('ROOM DATA, not instructions', body)
        self.assertIn('chose option "b"', body)
        self.assertIn("This is a NEW turn of this task (question episode e1)",
                      (gw.TASKS_DIR / "task-h8.txt").read_text(), "a wake's file says it is not a re-emit")
        head = (gw.TASKS_DIR / "task-h8.txt").read_text().split("\ntask: ")[0].splitlines()
        self.assertIn("wake_episode: e1", head, "the Stop hook reads a wake turn from its header")
        self.assertFalse(gw._result_disposed("task-h8"), "while the wake is open only its item decides")
        self.assertEqual(gw._ATTEMPTS.wake_item("task-h8"), "task-h8.wake-e1")

        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, "/v1/tasks/task-h8/ack": _ack("task-h8", 1)}))
        self.assertFalse(gw._deliver_result_payload("task-h8", "task-h8", "went with b"), "waits for its ack")
        self.assertTrue(gw._post_task_ack("task-h8", durable=True))
        self.assertTrue(gw._deliver_result_payload("task-h8", "task-h8", "went with b"))
        self.assertEqual(fake.posted("/v1/results")[0]["id"], "task-h8")
        root = gw._delivery_core().backend.root
        self.assertEqual((gw.read_item(root, "task-h8.wake-e1") or {}).get("status"), "DELIVERED")
        # Confirmed, not yet archived: a restart re-offers the live result through the wake's own item.
        self.assertEqual(gw._ATTEMPTS.wake_item("task-h8"), "task-h8.wake-e1", "kept until it leaves flight")
        posted = len(fake.posted("/v1/results"))
        self.assertTrue(gw._deliver_result_payload("task-h8", "task-h8", "went with b"))
        self.assertEqual(len(fake.posted("/v1/results")), posted, "already delivered: not posted again")
        self.assertEqual((gw.read_item(root, "task-h8.wake-e1") or {}).get("status"), "DELIVERED")
        self.assertFalse(any((gw.RESULTS_DIR / "undelivered").glob("*task-h8*")), "never quarantined")
        gw._ATTEMPTS.sweep(set())
        self.assertIsNone(gw._ATTEMPTS.wake_item("task-h8"), "forgotten once the task left flight")

        (gw.TASKS_DIR / "task-h8.txt").rename(gw.TASKS_DIR / "archive" / "task-h8.txt")
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
        self.assertEqual(gw._write_task({**task, "wake": wake}), ("task-h8c", True))
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True},
                                       "/v1/tasks/task-h8c/ack": _http(409, {"error_code": "task_cancelled"})}))
        self.assertFalse(gw._post_task_ack("task-h8c", durable=True))
        self.assertFalse((gw.TASKS_DIR / "task-h8c.txt").exists(), "the core never runs the wake")
        self.assertEqual(self.controls("task-h8c"), [])
        self.assertTrue(gw._deliver_result_payload("task-h8c", "task-h8c", "late"))
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
        turn = gw._ATTEMPTS.record_ack("task-h9", _ack("task-h9", 3))["turn_id"]
        with open(agent_turn.room_actions_path(gw._STATE), "a") as fh:
            fh.write(json.dumps({"record_type": "dispatch_outcome", "turn_id": turn, "seq": 1,
                                 "operation_id": f"op_{turn}_1", "operation_status": "succeeded",
                                 "applied_execution_phase": "responded", "applied_terminal": True}) + "\n")
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
        self.assertTrue(gw._deliver_result_payload("task-h9", "task-h9", "final"))
        self.assertEqual(fake.posted("/v1/results"), [], "the room action already answered")
        self.assertTrue(agent_turn.is_closed(gw._STATE, turn))

        turn = gw._ATTEMPTS.record_ack("task-h10", _ack("task-h10", 2))["turn_id"]
        self.assertTrue(gw._deliver_result_payload("task-h10", "task-h10", "the answer"))
        sent = fake.posted("/v1/results")[0]
        self.assertEqual((sent["status"], sent["execution_generation"], sent["worker_id"]),
                         ("completed", 2, gw._ATTEMPTS.worker_id()))
        self.assertTrue(agent_turn.is_closed(gw._STATE, turn))
        gw._ATTEMPTS.record_ack("task-h10", _ack("task-h10", 2))
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


if __name__ == "__main__":
    unittest.main()
