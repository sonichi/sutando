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
import threading
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
        self.assertIsNotNone(gw._write_task(_task("task-h3"), hold=True))     # staged before its ack
        self.assertEqual(gw._post_ack_first("task-h3", None), "accepted")
        self.assertEqual(gw._ATTEMPTS.get("task-h3")["execution_generation"], 2)
        self.assertIsNotNone(gw._write_task(_task("task-h3b"), hold=True))
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

    def test_an_old_cancel_the_core_took_is_recorded_before_the_ack_settles(self):
        tid = "task-regen-unrec"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True, "task_errors": [
            {"task_id": tid, "error_code": "stale_execution_generation"}]}}))
        gw._post_heartbeat({tid}, force=True)
        self.assertEqual(len(self.controls(tid)), 1)
        ctl = self.controls(tid)[0]
        (gw.TASKS_DIR / "archive").mkdir(exist_ok=True)
        ctl.rename(gw.TASKS_DIR / "archive" / ctl.name)     # the core took and archived it
        self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        real = gw._ATTEMPTS._merge
        with mock.patch.object(gw._ATTEMPTS, "_merge", side_effect=lambda w, f: (
                ({}, False) if "taken_ctl" in f else real(w, f))):
            self.assertFalse(gw._post_task_ack(tid, durable=True), "the taken cancel is not recorded")
        self.assertIn(tid, gw._load_pending_acks())
        self.assertEqual(gw._ATTEMPTS.get(tid)["execution_generation"], 1)
        gw._retry_pending_acks({tid})
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["execution_generation"], rec.get("taken_ctl")), (2, [ctl.stem]),
                         "the retry records that the core ran it")
        self.assertNotIn(tid, gw._load_pending_acks())

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
        gw._post_ready_results({tid})                # the drain reads the file it posts
        post = fake.posted("/v1/results")[0]
        self.assertEqual((post["status"], post["no_send"], post["execution_generation"]), ("failed", True, 2))

    def test_a_taken_cancel_keeps_its_marker_until_the_failure_is_recorded(self):
        tid = "task-regen-unrec"
        _, ctl = self.taken_stale_cancel(tid)
        (gw.RESULTS_DIR / f"{ctl}.txt").write_text("[no-send]\n")
        self.addCleanup(lambda: (gw.RESULTS_DIR / f"{ctl}.txt").unlink(missing_ok=True))
        (gw.TASKS_DIR / "archive").mkdir(exist_ok=True)
        (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
        real = gw._ATTEMPTS._merge
        with mock.patch.object(gw._ATTEMPTS, "_merge", side_effect=lambda w, f: (
                ({}, False) if "dispatch_lost" in f else real(w, f))):
            gw._post_task_beats({tid}, force=True)
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists())
        self.assertEqual(gw._ATTEMPTS.get(tid).get("taken_ctl"), [ctl], "kept: the next beat retries")
        gw._post_task_beats({tid}, force=True)
        self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.STALE_CANCEL_RESULT)
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("taken_ctl"))

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
        self.assertTrue(gw._ATTEMPTS.finish("task-moved", earlier), "nothing of the earlier attempt is left")
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
        real = gw._ATTEMPTS._save_json
        return mock.patch.object(gw._ATTEMPTS, "_save_json",
                                 side_effect=lambda p, d: False if p.name.startswith(prefix) else real(p, d))

    def test_a_wake_that_cannot_be_saved_is_not_queued_or_acked(self):
        wake = self.answered_task("task-wopen")
        with self.failing_save("relay-task-attempts-v2"):
            self.assertIsNone(gw._write_task(dict(wake)), "left unacked for a re-serve")
        self.assertFalse((gw.TASKS_DIR / "task-wopen.txt").exists(), "nothing queued")
        self.assertIsNone(gw._ATTEMPTS.get("task-wopen").get("wake_episode"))
        self.assertTrue(any("wake e1 not saved" in m for m in self.logs))
        gw._WAKES_LEFT_UNACKED.discard(("task-wopen", "e1"))
        self.assertEqual(gw._write_task(dict(wake)), ("task-wopen", True), "the re-serve runs it")

    def test_a_wake_result_whose_retirement_fails_stays_live_and_is_retried(self):
        for failing in ("relay-wake-items", "relay-wake-episodes"):
            with self.subTest(failing=failing):
                tid = f"task-wret-{failing.rsplit('-', 1)[-1]}"
                self.assertEqual(gw._write_task(dict(self.answered_task(tid))), (tid, True))
                fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
                self.assertTrue(gw._post_task_ack(tid, durable=True))
                (gw.TASKS_DIR / "archive").mkdir(exist_ok=True)
                (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
                (gw.RESULTS_DIR / f"{tid}.txt").write_text("went with b")
                inflight = {tid}
                with self.failing_save(failing):
                    gw._post_ready_results(inflight)
                self.assertTrue((gw.RESULTS_DIR / f"{tid}.txt").exists(), "not archived: still live")
                self.assertIn(tid, inflight, "still in flight")
                self.assertEqual(gw._ATTEMPTS.get(tid).get("wake_episode"), "e1", "the attempt is kept")
                self.assertTrue(any(f"result {tid}: settled, but its attempt could not be retired" in m
                                    for m in self.logs))
                gw._post_ready_results(inflight)
                self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "the retry archives it")
                self.assertNotIn(tid, inflight)
                self.assertEqual(len(fake.posted("/v1/results")), 1, "posted once across both passes")
                self.assertEqual(gw._ATTEMPTS.wake_item(tid), f"{tid}.wake-e1")

    def test_a_wake_result_whose_records_cannot_be_read_at_retirement_stays_live(self):
        for record in ("relay-task-attempts-v2", "relay-wake-episodes"):
            for broken in ("malformed", "unreadable"):
                with self.subTest(record=record, broken=broken):
                    tid = f"task-wrd-{record.rsplit('-', 1)[-1]}-{broken}"
                    wake = self.answered_task(tid)
                    self.assertEqual(gw._write_task(dict(wake)), (tid, True))
                    path, good = gw._ATTEMPTS.state_dir / f"{record}.json", []

                    def break_it(_payload, path=path, broken=broken, good=good):
                        good.append(path.read_bytes() if path.exists() else b"{}")
                        path.unlink(missing_ok=True)
                        path.write_text("{not json") if broken == "malformed" else path.mkdir()
                        return {"ok": True}

                    fake = self.use(FakeGateway(**{"/v1/results": break_it, f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
                    self.assertTrue(gw._post_task_ack(tid, durable=True))
                    (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
                    (gw.RESULTS_DIR / f"{tid}.txt").write_text("went with b")
                    inflight = {tid}
                    gw._post_ready_results(inflight)
                    self.assertEqual(len(fake.posted("/v1/results")), 1)
                    self.assertTrue((gw.RESULTS_DIR / f"{tid}.txt").exists(), "not archived: still live")
                    self.assertIn(tid, inflight, "still in flight")
                    self.assertTrue(any(f"result {tid}: settled, but its attempt could not be retired" in m
                                        for m in self.logs))

                    path.rmdir() if path.is_dir() else path.unlink()
                    path.write_bytes(good[0])
                    gw._post_ready_results(inflight)
                    self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "the retry archives it")
                    self.assertNotIn(tid, inflight)
                    self.assertEqual(len(fake.posted("/v1/results")), 1, "never posted a second time")
                    self.assertIn("e1", gw._ATTEMPTS._load_json(gw._ATTEMPTS._wakes_path)[tid])
                    self.assertEqual(gw._ATTEMPTS.wake_item(tid), f"{tid}.wake-e1")
                    self.assertIsNone(gw._write_task(dict(wake)), "the replay is refused")

    def test_an_unreadable_ledger_is_kept_through_other_tasks_and_repeated_drains(self):
        for broken in ("malformed", "unreadable"):
            with self.subTest(broken=broken):
                tid = f"task-wkeep-{broken}"
                wake = self.answered_task(tid)
                self.assertEqual(gw._write_task(dict(wake)), (tid, True))
                path, good = gw._ATTEMPTS.path, []

                def break_it(_payload, broken=broken):
                    good.append(path.read_bytes())
                    path.unlink()
                    path.write_text("{not json") if broken == "malformed" else path.mkdir()
                    return {"ok": True}

                fake = self.use(FakeGateway(**{"/v1/results": break_it, f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
                self.assertTrue(gw._post_task_ack(tid, durable=True))
                (gw.TASKS_DIR / f"{tid}.txt").rename(gw.TASKS_DIR / "archive" / f"{tid}.txt")
                (gw.RESULTS_DIR / f"{tid}.txt").write_text("went with b")
                inflight = {tid}
                gw._post_ready_results(inflight)
                broken_bytes = None if path.is_dir() else path.read_bytes()

                self.assertIsNone(gw._write_task(_task(f"{tid}-other")), "another task is left for a re-serve")
                self.assertEqual(None if path.is_dir() else path.read_bytes(), broken_bytes,
                                 "the unreadable ledger is left as it was")
                root = gw._delivery_core().backend.root
                for _ in range(3):
                    gw._post_ready_results(inflight)
                self.assertEqual(len(fake.posted("/v1/results")), 1, "never posted a second time")
                self.assertTrue((gw.RESULTS_DIR / f"{tid}.txt").exists(), "still live")
                self.assertIn(tid, inflight)
                self.assertIsNone(gw.read_item(root, tid), "no delivery under the first attempt's item")
                self.assertEqual((gw.read_item(root, f"{tid}.wake-e1") or {}).get("status"), "DELIVERED")
                self.assertIsNone(gw._write_task(dict(wake)), "nor queued again while unknown")

                path.rmdir() if path.is_dir() else path.unlink()
                path.write_bytes(good[0])
                gw._post_ready_results(inflight)
                self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "recovered: archived")
                self.assertNotIn(tid, inflight)
                self.assertEqual(len(fake.posted("/v1/results")), 1)
                self.assertIn("e1", gw._ATTEMPTS._load_json(gw._ATTEMPTS._wakes_path)[tid])
                self.assertEqual(gw._ATTEMPTS.wake_item(tid), f"{tid}.wake-e1")
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
        contract = gw._ATTEMPTS._contract_path
        contract.unlink(missing_ok=True)
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        inflight = set()
        with self.failing_save("relay-broker-contract"):
            self.assertFalse(gw._take_ack_first(_task(tid, ack_first=True), inflight, {}))
        self.assertFalse(contract.exists())
        self.assertEqual(gw._ATTEMPTS.get(tid)["execution_generation"], 1, "the generation is saved")
        self.assertIsNot((gw._load_held() or {})[tid].get("accepted"), True, "kept staged")
        self.assertTrue(gw._advance_held_tasks(inflight), "the same-generation re-ack repairs the contract")
        self.assertTrue(contract.exists())
        self.assertNotIn(tid, gw._load_held() or {})
        self.ran(tid)
        self.posted_once_under_its_generation(fake, tid, inflight)

    def test_beat_fences_and_stops_act_only_once_recorded(self):
        for code in ("stale_execution_generation", "task_cancelled"):
            with self.subTest(code=code):
                tid = f"task-unrec-{code.split('_')[0]}"
                gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
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
                gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
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
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
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
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        with self.failing_save("relay-task-attempts-v2"):
            gw._fail_unanswered(tid, tid, "failed", "test")
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "retried next beat")
        self.assertFalse(gw._ATTEMPTS.get(tid).get("dispatch_lost"))
        gw._fail_unanswered(tid, tid, "failed", "test")
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
        real, landed = gw._ATTEMPTS._save_json, [False]

        def save(path, data):
            if path != gw._ATTEMPTS.path:
                return real(path, data)
            if landed[0]:
                return False
            landed[0] = bool((data.get(tid) or {}).get("stop_requested"))
            return real(path, data)
        return mock.patch.object(gw._ATTEMPTS, "_save_json", side_effect=save)

    def seed(self, tid, rec):
        """A record exactly as the base bridge wrote it: no schema mark."""
        data = gw._ATTEMPTS._load_json(gw._ATTEMPTS.path)
        data[tid] = rec
        gw._ATTEMPTS.path.write_text(json.dumps(data))

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

    def test_a_failure_owed_by_an_older_generation_never_posts_under_the_newer(self):
        for written in (False, True):
            with self.subTest(written=written):
                tid = f"task-owed-regen-{int(written)}"
                self.owed_failure(tid)
                if written:
                    gw._post_task_beats({tid}, force=True)
                    self.assertEqual((gw.RESULTS_DIR / f"{tid}.txt").read_text(), gw.DISPATCH_LOST_RESULT)
                archived = gw.TASKS_DIR / "archive" / f"{tid}.txt"
                archived.rename(gw.TASKS_DIR / f"{tid}.txt")       # served again: back in the queue
                fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2), "/v1/results": {"ok": True},
                                               "/v1/tasks/heartbeat": {"ok": True}}))
                self.assertTrue(gw._post_task_ack(tid, durable=True))
                rec = gw._ATTEMPTS.get(tid)
                self.assertEqual((rec["execution_generation"], rec.get("dispatch_lost")), (2, None))
                self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "generation 1's failure is gone")
                gw._post_task_beats({tid}, force=True)
                self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "and is not written again")
                self.ran(tid)
                gw._post_ready_results({tid})
                post = fake.posted("/v1/results")
                self.assertEqual(len(post), 1)
                self.assertEqual((post[0]["body"], post[0].get("status"), post[0]["execution_generation"]),
                                 ("done", "completed", 2), "generation 2's own answer")

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
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
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
        self.assertEqual([p.name for p in self.controls(tid)], [f"{rec['fence_ctl']}.txt"],
                         "its owed cancel is written at start")
        self.assertIn(tid, inflight)
        gw._post_task_beats(inflight, force=True)
        self.assertEqual(len(self.controls(tid)), 1, "exactly once")

    def test_a_crash_while_withdrawing_an_old_cancel_never_leaves_it_queued_for_the_newer(self):
        class Crash(BaseException):
            pass
        real_rename, real_unlink, real_merge = os.rename, Path.unlink, gw._ATTEMPTS._merge

        def rename(src, dst, *a, **k):
            if Path(src).name.startswith("task-ctl-"):
                raise Crash()
            return real_rename(src, dst, *a, **k)

        def unlink(path, *a, **k):
            if path.name.startswith("task-ctl-") and path.parent == gw.TASKS_DIR:
                raise Crash()
            return real_unlink(path, *a, **k)

        def merge(w, f):
            if "fence_ctl" in f and f["fence_ctl"] is None:
                raise Crash()
            return real_merge(w, f)

        windows = {"before the cancel moves": [mock.patch.object(gw.os, "rename", rename),
                                               mock.patch.object(Path, "unlink", unlink)],
                   "after it moved, before the ledger write": [mock.patch.object(gw._ATTEMPTS, "_merge", merge)]}
        for window, faults in windows.items():
            with self.subTest(window=window):
                tid = f"task-regen-crash-{len(window)}"
                self.assertTrue(gw._take_served_task(_task(tid), set(), []))
                gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
                self.beat_error(tid, "stale_execution_generation")
                self.assertEqual(len(self.controls(tid)), 1)
                self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
                self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
                for f in faults:
                    f.start()
                with self.assertRaises(Crash):
                    gw._post_task_ack(tid, durable=True)
                for f in faults:
                    f.stop()
                gw._retry_pending_acks({tid})            # the restarted bridge retries the ack
                rec = gw._ATTEMPTS.get(tid)
                self.assertEqual(rec["execution_generation"], 2)
                self.assertEqual(self.controls(tid), [], "generation 1's cancel must not stop generation 2")
                self.assertIsNone(rec.get("taken_ctl"), "withdrawn here, not taken by the core")
                self.assertEqual([p for p in gw.TASKS_DIR.glob(".task-ctl-*")
                                  if f"stop processing {tid} " in p.read_text()], [])

    def test_a_stop_whose_control_and_rollback_both_fail_still_stops_the_core(self):
        tid = "task-stop-double"
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
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

    def test_a_settlement_and_a_newer_ack_on_two_threads_never_interleave(self):
        for debt in ("failure", "cancel"):
            with self.subTest(debt=debt):
                tid = f"task-race-{debt}"
                if debt == "failure":
                    self.owed_failure(tid)
                    (gw.TASKS_DIR / "archive" / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.txt")
                else:
                    self.assertTrue(gw._take_served_task(_task(tid), set(), []))
                    gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
                    with self.no_ctl_write():
                        self.beat_error(tid, "stale_execution_generation")
                    self.assertEqual(self.all_controls(tid), [])
                self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
                fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2), "/v1/results": {"ok": True},
                                               "/v1/tasks/heartbeat": {"ok": True}}))
                reached, release = self.paused("_write_if_absent" if debt == "failure"
                                               else "_queue_cancel_instruction")
                settler = threading.Thread(target=gw._post_task_beats, args=({tid},), kwargs={"force": True})
                settler.start()
                self.assertTrue(reached.wait(5), "the settlement read generation 1's debt")
                acked = []
                acker = threading.Thread(target=lambda: acked.append(gw._post_task_ack(tid, durable=True)))
                acker.start()
                acker.join(0.3)
                release.set()
                settler.join(10)
                acker.join(10)
                mock.patch.stopall()
                mock.patch.object(gw, "_log", self.logs.append).start()
                self.assertEqual(acked, [True])
                rec = gw._ATTEMPTS.get(tid)
                self.assertEqual((rec["execution_generation"], rec.get("dispatch_lost"), rec.get("taken_ctl")),
                                 (2, None, None))
                self.assertEqual(self.all_controls(tid), [], "generation 1's cancel never reaches generation 2")
                self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "nor its failure result")
                gw._post_ready_results({tid})
                self.assertEqual([p for p in fake.posted("/v1/results") if p["id"] == tid], [])

    def test_a_cancel_queued_before_the_first_ack_is_withdrawn_by_it(self):
        tid = "task-preack-fence"
        pending: list = []
        self.assertTrue(gw._take_served_task(_task(tid), set(), pending))
        self.assertTrue(gw._record_pending_acks(pending))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(409, {
            "error": "not leased to you", "reason": "stale lease generation",
            "error_code": "stale_execution_generation"})}))
        self.assertFalse(gw._post_task_ack(tid, durable=True))
        self.assertEqual(len(self.controls(tid)), 1, "the refused lease's attempt is asked to stop")
        pending = []
        self.assertTrue(gw._take_served_task(_task(tid), set(), pending))     # served again, newer lease
        self.assertTrue(gw._record_pending_acks(pending))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["execution_generation"], rec.get("fenced"), rec.get("taken_ctl")), (2, None, None))
        self.assertEqual(self.all_controls(tid), [], "the first generation is never stopped by it")

    def test_an_old_generation_is_not_settled_while_a_newer_serve_awaits_its_ack(self):
        tid = "task-reserve-awaiting"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        with self.no_ctl_write():
            self.beat_error(tid, "stale_execution_generation")
        pending: list = []
        self.assertTrue(gw._take_served_task(_task(tid), set(), pending))     # served again, not acked
        self.assertTrue(gw._record_pending_acks(pending))
        inflight = {tid}
        gw._close_stale_turns(inflight)                                         # a restart settles first
        for p in self.all_controls(tid):
            p.rename(p.with_name(p.name.replace(".txt", ".claimed-core1")))     # the core takes what is queued
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        gw._retry_pending_acks(inflight)
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual(rec["execution_generation"], 2)
        self.assertEqual((self.all_controls(tid), rec.get("taken_ctl")), ([], None),
                         "generation 1's cancel was never handed to the core")

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
        self.assertEqual(gw._write_task({**task, "wake": wake}), (tid, True))   # its in-flight save is lost
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        self.ran(tid)
        (gw.RESULTS_DIR / f"{tid}.txt").write_text("went with b")
        inflight = set()                                                        # restart
        gw._close_stale_turns(inflight)
        gw._post_ready_results(inflight)
        gw._reconcile_orphan_results(inflight)
        bodies = [p["body"] for p in fake.posted("/v1/results") if p["id"] == tid]
        self.assertEqual((len(bodies), "went with b" in bodies[-1]), (2, True), "the resumed answer is posted")
        root = gw._delivery_core().backend.root
        self.assertEqual((gw.read_item(root, f"{tid}.wake-e1") or {}).get("status"), "DELIVERED")
        self.assertFalse(any((gw.RESULTS_DIR / "undelivered").glob(f"*{tid}*")), "never quarantined")

    def test_an_old_generations_result_never_retires_the_newer_without_turn_context(self):
        tid = "task-finish-regen"
        mock.patch.object(gw, "TURN_CONTEXT", False).start()
        mock.patch.object(gw._ATTEMPTS, "turn_context", False).start()
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        self.ran(tid)
        entered, gate = threading.Event(), threading.Event()

        def result(payload):
            entered.set()
            gate.wait(10)
            return {"ok": True}
        self.use(FakeGateway(**{"/v1/results": result, f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
        drain = threading.Thread(target=gw._post_ready_results, args=({tid},))
        drain.start()
        self.assertTrue(entered.wait(5), "generation 1's result is being posted")
        self.assertTrue(gw._post_task_ack(tid, durable=True))                   # generation 2 meanwhile
        gate.set()
        drain.join(10)
        self.assertEqual(gw._ATTEMPTS.get(tid).get("execution_generation"), 2, "generation 2 is not erased")

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
                    (gw.TASKS_DIR / "archive" / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.txt")
                    self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
                    self.assertTrue(gw._post_task_ack(tid, durable=True))
                else:
                    tmp = gw.RESULTS_DIR / f".{tid}.core"
                    tmp.write_text("the core's answer")
                    os.replace(tmp, gw.RESULTS_DIR / f"{tid}.txt")
                release.set()
                drain.join(10)
                mock.patch.object(gw, "_relay_result", side_effect=None).stop()
                mock.patch.stopall()
                mock.patch.object(gw, "_log", self.logs.append).start()
                self.use(fake)
                self.assertEqual([p for p in fake.posted("/v1/results") if p["id"] == tid], [],
                                 "the bytes it read are gone: nothing of them is published")
                gw._post_ready_results({tid})
                posts = [(p["body"], p.get("status")) for p in fake.posted("/v1/results") if p["id"] == tid]
                self.assertEqual(posts, [] if race.startswith("withdrawn") else [("the core's answer", "completed")])
                self.assertFalse(any((gw.RESULTS_DIR / "undelivered").glob(f"*{tid}*")), "never quarantined")

    def test_a_serve_waits_for_a_settlement_already_under_way(self):
        tid = "task-serve-vs-settle"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        with self.no_ctl_write():
            self.beat_error(tid, "stale_execution_generation")
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True}}))
        reached, release = self.paused("_queue_cancel_instruction")
        settler = threading.Thread(target=gw._post_task_beats, args=({tid},), kwargs={"force": True})
        settler.start()
        self.assertTrue(reached.wait(5), "the settlement read generation 1's owed cancel")
        served = threading.Thread(target=gw._write_task, args=(_task(tid),))
        served.start()
        served.join(0.3)
        blocked = served.is_alive()
        release.set()
        settler.join(10)
        served.join(10)
        self.assertTrue(blocked, "the re-serve waits until the settlement's transition is done")
        self.assertTrue(gw._ATTEMPTS.get(tid).get("awaiting_ack"))
        mock.patch.stopall()
        mock.patch.object(gw, "_log", self.logs.append).start()
        self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        self.assertEqual((self.all_controls(tid), gw._ATTEMPTS.get(tid).get("taken_ctl")), ([], None))

    def test_a_beat_answered_for_an_older_generation_never_fences_the_newer(self):
        tid = "task-beat-regen"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        entered, gate = threading.Event(), threading.Event()

        def beat(payload):
            entered.set()
            gate.wait(10)
            return {"ok": True, "task_errors": [{"task_id": tid, "error_code": "task_cancelled"}]}
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": beat, f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        beating = threading.Thread(target=gw._post_task_beats, args=({tid},), kwargs={"force": True})
        beating.start()
        self.assertTrue(entered.wait(5), "generation 1's beat is on the wire")
        self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        gate.set()
        beating.join(10)
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["execution_generation"], rec.get("stop_requested"), rec.get("fenced")), (2, None, None))
        self.assertEqual(self.all_controls(tid), [], "generation 1's Stop never reaches generation 2")

    def test_a_start_that_cannot_read_a_live_result_keeps_its_wake_identity(self):
        tid = "task-wake-unreadable"
        task = {"id": tid, "task": "hi", "source": "ag2space", "user_id": "@o:x"}
        self.answered = self.answered_task(tid)
        self.assertEqual(gw._write_task(self.answered), (tid, True))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        self.ran(tid)
        (gw.RESULTS_DIR / f"{tid}.txt").write_text("went with b")
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
        with mock.patch.object(gw._ATTEMPTS, "remember_task", return_value=False):
            self.assertIsNone(gw._write_task(wake), "opened, but its serve was not recorded: left unacked")
        self.assertEqual(gw._ATTEMPTS.get(tid).get("wake_episode"), "e1")
        gw._post_task_beats({tid}, force=True)
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "generation 1's debt is not the wake's")

    # ── round 12, independent review: acks and refusals for older leases, unknown entries ──
    def test_a_late_ack_for_an_older_serve_or_a_retired_attempt_moves_nothing(self):
        tid = "task-late-ack"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        self.assertIsNotNone(gw._write_task(_task(tid, lease_id="L2")))           # served again
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        turn = gw._ATTEMPTS.get(tid)["turn_id"]
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))   # L1's answer, late, same generation
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 1)}))   # and naming generation 1
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L1"))
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["execution_generation"], rec["turn_id"]), (2, turn), "generation 2 stands")
        self.assertFalse(agent_turn.is_closed(gw._STATE, turn))
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.ran(tid)
        gw._post_ready_results({tid})
        self.assertEqual(gw._ATTEMPTS.get(tid), {}, "settled and retired")
        self.assertTrue(gw._record_pending_acks([(tid, True, "L2")]))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        self.assertEqual(gw._ATTEMPTS.get(tid), {}, "a late ack does not bring a retired attempt back")
        self.assertEqual(len(fake.posted("/v1/results")), 1)

    def test_an_ack_refusal_about_an_older_lease_never_fences_the_newer_generation(self):
        tid = "task-ack-refused-old"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        self.assertIsNotNone(gw._write_task(_task(tid, lease_id="L2")))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        self.assertTrue(gw._record_pending_acks([(tid, True, "L1")]))
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _http(409, {
            "error_code": "task_cancelled", "cancel_requested_generation": 1})}))
        gw._post_task_ack(tid, durable=True, lease_id="L1")
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["execution_generation"], rec.get("fenced")), (2, None))
        self.assertTrue((gw.TASKS_DIR / f"{tid}.txt").exists(), "generation 2's task is not withdrawn")
        self.assertEqual(fake.posted("/cancel-ack"), [])
        self.assertNotIn(tid, gw._load_pending_acks())

    def test_a_beat_fence_while_a_newer_serve_awaits_its_ack_is_left_to_that_ack(self):
        tid = "task-beat-awaiting"
        self.assertTrue(gw._take_served_task(_task(tid, lease_id="L1"), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        self.assertIsNotNone(gw._write_task(_task(tid, lease_id="L2")))           # served again, unacked
        self.assertTrue(gw._ATTEMPTS.get(tid).get("awaiting_ack"))
        self.beat_error(tid, "stale_execution_generation")
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec.get("fenced"), rec.get("awaiting_ack")), (None, True))
        self.assertEqual(self.all_controls(tid), [], "no cancel queued against the waiting serve")
        self.assertTrue(gw._record_pending_acks([(tid, True, "L2")]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        self.assertEqual(gw._ATTEMPTS.get(tid)["execution_generation"], 2)

    def test_a_ledger_that_turns_unreadable_mid_delivery_holds_the_result(self):
        tid = "task-ledger-mid-delivery"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        self.beat_error(tid, "task_cancelled")
        self.ran(tid)
        saved, real = gw._ATTEMPTS.path.read_bytes(), gw._ATTEMPTS.wake_item
        self.addCleanup(lambda: gw._ATTEMPTS.path.write_bytes(saved))

        def wake_item(w):
            item = real(w)
            gw._ATTEMPTS.path.write_text("{not json")                # the ledger breaks right after
            return item
        fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
        with mock.patch.object(gw._ATTEMPTS, "wake_item", side_effect=wake_item):
            gw._post_ready_results({tid})
        self.assertEqual(fake.posted("/v1/results"), [], "held, never posted without its lease")
        self.assertTrue((gw.RESULTS_DIR / f"{tid}.txt").exists())

    def test_a_damaged_ledger_entry_is_kept_and_held(self):
        tid = "task-entry-damaged"
        data = gw._ATTEMPTS._load_json(gw._ATTEMPTS.path)
        data[tid] = "damaged"
        gw._ATTEMPTS.path.write_text(json.dumps(data))
        self.assertTrue(gw._broker_holds(tid), "an entry that is not a record is unknown")
        gw._close_stale_turns(set())
        self.assertEqual(gw._ATTEMPTS._load_json(gw._ATTEMPTS.path).get(tid), "damaged", "never swept")
        data = gw._ATTEMPTS._load_json(gw._ATTEMPTS.path)
        data.pop(tid)
        gw._ATTEMPTS.path.write_text(json.dumps(data))

    def test_every_older_cancel_the_core_took_is_watched(self):
        tid = "task-two-taken"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        self.beat_error(tid, "task_cancelled")
        gw._ATTEMPTS._set(tid, stop_requested=None)
        self.beat_error(tid, "stale_execution_generation")
        ctls = self.controls(tid)
        self.assertEqual(len(ctls), 2)
        for p in ctls:
            p.rename(p.with_name(p.name.replace(".txt", ".claimed-core1")))   # the core takes both
        self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        self.assertEqual(sorted(gw._ATTEMPTS.get(tid)["taken_ctl"]), sorted(p.stem for p in ctls))

    def test_a_wake_withdraws_the_earlier_attempts_queued_cancel_before_it_runs(self):
        tid = "task-wake-retracts"
        wake = self.answered_task(tid)
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        self.beat_error(tid, "stale_execution_generation")
        self.assertEqual(len(self.controls(tid)), 1)
        self.assertEqual(gw._write_task(wake), (tid, True))
        self.assertEqual(self.all_controls(tid), [], "the earlier attempt's cancel never stops the wake")

    def test_an_ack_whose_older_generation_cannot_be_undone_is_retried_not_recorded(self):
        for fault in ("failure result", "cancel", "unreadable failure"):
            with self.subTest(fault=fault):
                tid = f"task-retract-fault-{len(fault)}"
                if fault != "cancel":
                    self.owed_failure(tid)
                    gw._post_task_beats({tid}, force=True)
                    (gw.TASKS_DIR / "archive" / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.txt")
                    stuck = (mock.patch.object(gw.disposal, "retire_generation", return_value=mock.Mock(
                        retired=False, outcome=None, cause="injected")) if fault == "failure result" else
                        mock.patch.object(gw.task_attempts, "file_identity", side_effect=PermissionError("x")))
                else:
                    self.assertTrue(gw._take_served_task(_task(tid), set(), []))
                    gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
                    self.beat_error(tid, "stale_execution_generation")
                    real = os.rename
                    stuck = mock.patch.object(gw.os, "rename", side_effect=lambda a, b, *x, **k: (
                        (_ for _ in ()).throw(PermissionError(a)) if "task-ctl-" in str(a) else real(a, b, *x, **k)))
                self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
                self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
                with stuck:
                    self.assertFalse(gw._post_task_ack(tid, durable=True))
                self.assertEqual(gw._ATTEMPTS.get(tid)["execution_generation"], 1, "not recorded over it")
                self.assertIn(tid, gw._load_pending_acks(), "the ack is retried")
                self.assertTrue(gw._post_task_ack(tid, durable=True))
                self.assertEqual((gw._ATTEMPTS.get(tid)["execution_generation"], self.all_controls(tid)), (2, []))
                self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists())

    def test_a_start_counts_a_claimed_task_live_and_an_unreadable_queue_unknown(self):
        for state in ("claimed", "queue unreadable"):
            with self.subTest(state=state):
                tid = f"task-start-{len(state)}"
                self.assertTrue(gw._take_served_task(_task(tid), set(), []))
                gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
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
        with mock.patch.object(gw._ATTEMPTS, "fence", return_value={"new": False, "cancel": False, "unsaved": True}):
            gw._post_task_ack(tid, durable=True)
        self.assertIn(tid, gw._load_pending_acks(), "retried, so the fence is heard again")

    def test_a_legacy_ack_whose_wait_cannot_be_ended_keeps_its_pending_ack(self):
        tid = "task-legacy-ack-unsaved"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": {"ok": True, "durable": True}}))   # no generation
        with mock.patch.object(gw._ATTEMPTS, "ack_settled", return_value=False):
            self.assertFalse(gw._post_task_ack(tid, durable=True))
        self.assertIn(tid, gw._load_pending_acks())

    def test_a_stop_is_not_judged_while_the_wake_records_cannot_be_read(self):
        tid = "task-stop-wake-unknown"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        self.beat_error(tid, "task_cancelled")
        ctl = gw._ATTEMPTS.get(tid)["stop_ctl"]
        (gw.TASKS_DIR / f"{ctl}.txt").unlink()
        (gw.RESULTS_DIR / f"{ctl}.txt").write_text("[no-send]")               # the core ran the Stop
        self.addCleanup(lambda: (gw.RESULTS_DIR / f"{ctl}.txt").unlink(missing_ok=True))
        items = gw._ATTEMPTS._wake_items_path
        saved = items.read_bytes() if items.exists() else None
        self.addCleanup(lambda: items.write_bytes(saved) if saved is not None else items.unlink(missing_ok=True))
        items.write_text("{not json")
        fake = self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True}}))
        gw._post_task_beats({tid}, force=True)
        self.assertEqual(fake.posted("/cancel-ack"), [], "unknown is never read as stopped with no reply")

    def test_an_ack_waits_for_a_beat_check_already_under_way(self):
        tid = "task-beat-check-vs-ack"
        self.held_then_archived(tid)
        self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2), "/v1/tasks/heartbeat": {"ok": True}}))
        real, reached, release = gw.task_attempts.last_activity, threading.Event(), threading.Event()

        def activity(*a):
            reached.set()
            release.wait(10)
            return real(*a)
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
        self.assertEqual(gw._ATTEMPTS.get(tid)["execution_generation"], 2)

    def test_a_cancel_written_but_not_cleared_then_taken_is_watched_by_the_newer(self):
        tid = "task-owed-written-taken"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        real = gw._ATTEMPTS._set
        with mock.patch.object(gw._ATTEMPTS, "_set", side_effect=lambda w, **f: (
                False if f == {"owed_ctl": None} else real(w, **f))):
            self.beat_error(tid, "stale_execution_generation")          # written; its marker not cleared
        ctl = gw._ATTEMPTS.get(tid)["owed_ctl"]["id"]
        (gw.TASKS_DIR / f"{ctl}.txt").rename(gw.TASKS_DIR / f"{ctl}.claimed-core1")
        self.addCleanup(lambda: (gw.TASKS_DIR / f"{ctl}.claimed-core1").unlink(missing_ok=True))
        self.assertTrue(gw._record_pending_acks([(tid, True, None)]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True))
        self.assertEqual(gw._ATTEMPTS.get(tid).get("taken_ctl"), [ctl], "the core took it: watched")

    def test_a_wake_waits_while_its_earlier_attempts_id_is_still_to_retire(self):
        tid = "task-wake-settle-pending"
        wake = self.answered_task(tid)
        with mock.patch.object(gw, "_SETTLE_PENDING", {tid}):
            self.assertIsNone(gw._write_task(wake), "left unacked until the retirement is done")
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("wake_episode"))

    def test_a_wake_whose_earlier_cancel_cannot_be_withdrawn_is_not_opened(self):
        tid = "task-wake-retract-fails"
        wake = self.answered_task(tid)
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
        self.beat_error(tid, "stale_execution_generation")
        real = os.rename
        with mock.patch.object(gw.os, "rename", side_effect=lambda a, b, *x, **k: (
                (_ for _ in ()).throw(PermissionError(a)) if "task-ctl-" in str(a) else real(a, b, *x, **k))):
            self.assertIsNone(gw._write_task(wake))
        self.assertIsNone(gw._ATTEMPTS.get(tid).get("wake_episode"), "not opened over a live cancel")
        self.assertEqual(gw._write_task(wake), (tid, True), "opened once the cancel is withdrawn")
        self.assertEqual(self.all_controls(tid), [])

    def test_a_record_watching_a_taken_cancel_is_never_swept(self):
        tid = "task-taken-only"
        self.seed(tid, {"execution_generation": 2, "fenced": "task_not_found",
                        "taken_ctl": ["task-ctl-1-watched"]})
        gw._close_stale_turns(set())
        self.assertEqual(gw._ATTEMPTS.get(tid)["taken_ctl"], ["task-ctl-1-watched"])

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
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
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
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
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
        self.assertEqual((rec.get("execution_generation"), rec.get("lease_id")), (2, "L2"), "the newer serve stands")
        self.assertTrue((gw.TASKS_DIR / f"{tid}.txt").exists(), "generation 2's task stays queued")

    def test_a_failure_published_but_unsent_is_taken_back_by_a_newer_generation(self):
        tid = "task-failure-unsent"
        self.owed_failure(tid, lease="L1")
        gw._post_task_beats({tid}, force=True)
        fake = self.use(FakeGateway(**{"/v1/results": _http(503, {"error": "busy"})}))
        gw._post_ready_results({tid})
        self.assertEqual(len(fake.posted("/v1/results")), 1, "published, refused for now: pending a retry")
        (gw.TASKS_DIR / "archive" / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.txt")
        self.assertIsNotNone(gw._write_task(_task(tid, lease_id="L2")))
        fake = self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2), "/v1/results": {"ok": True}}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        self.core_writes(tid, "the real answer")
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
        mock.patch.object(gw.time, "time", return_value=time.time() + 3600).start()   # past any retry wait
        gw._post_ready_results({tid})
        posts = [(p["execution_generation"], p.get("status"), p["body"]) for p in fake.posted("/v1/results")]
        self.assertEqual(posts, [(2, "completed", "the real answer")], "the stale failure is never sent")
        self.assertFalse(any((gw.RESULTS_DIR / "undelivered").glob(f"{tid}*")))

    def test_a_pin_that_cannot_be_released_holds_its_id_until_it_is(self):
        tid = "task-pin-stuck"
        self.assertTrue(gw._take_served_task(_task(tid), set(), []))
        gw._ATTEMPTS.record_ack(tid, _ack(tid, 1))
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
            self.assertTrue(gw._earlier_attempt_settling(tid, {"blocker_episode_id": "e1"}),
                            "a wake waits while the pin holds")
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
        self.assertIsNotNone(gw._write_task(_task(tid, lease_id="L2")))     # re-served, not yet on the ledger
        for answer in (_http(404, {"error": "not leased", "reason": "stale lease generation"}),
                       _http(409, {"error_code": "stale_execution_generation"})):
            self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": answer}))
            gw._post_task_ack(tid, durable=True, lease_id="L1")
            rec = gw._ATTEMPTS.get(tid)
            self.assertEqual((rec["awaiting_ack"], rec.get("fenced")), (True, None))
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True}}))
        gw._post_task_beats({tid}, force=True)
        self.assertEqual(self.all_controls(tid), [], "no cancel of generation 1 against the waiting serve")
        self.assertTrue(gw._record_pending_acks([(tid, True, "L2")]))
        self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
        rec = gw._ATTEMPTS.get(tid)
        self.assertEqual((rec["execution_generation"], rec.get("taken_ctl")), (2, None))

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
                    gw._ATTEMPTS.record_ack(t, _ack(t, 1))
                    self.ran(t)
                    old = time.time() - 5
                    os.utime(gw.RESULTS_DIR / f"{t}.txt", (old, old))
                data = gw._ATTEMPTS._load_json(gw._ATTEMPTS.path)
                data[tid] = damage
                gw._ATTEMPTS.path.write_text(json.dumps(data))
                fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, "/v1/tasks/heartbeat": {"ok": True}}))
                for _pass in range(2):
                    gw._post_ready_results({tid, other})
                    gw._post_task_beats({tid, other}, force=True)
                posted = {p["id"]: p for p in fake.posted("/v1/results")}
                self.assertNotIn(tid, posted, "unknown is held: never posted, with or without lease fields")
                self.assertEqual(posted[other]["execution_generation"], 1, "the rest of the pass goes on")
                self.assertTrue((gw.RESULTS_DIR / f"{tid}.txt").exists())
                self.assertEqual(gw._ATTEMPTS._load_json(gw._ATTEMPTS.path)[tid], damage, "never rewritten")
                self.assertFalse(gw._ATTEMPTS.remember_task(tid, _task(tid)), "a serve is not recorded over it")
                data = gw._ATTEMPTS._load_json(gw._ATTEMPTS.path)
                data.pop(tid)
                gw._ATTEMPTS.path.write_text(json.dumps(data))
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
        self.assertEqual((rec.get("awaiting_ack"), rec.get("lease_id")), (True, "L2"), "the newer serve stands")

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
        real, reached, release = gw.task_attempts.same_serve, threading.Event(), threading.Event()

        def held(*a):
            ok = real(*a)
            if not reached.is_set():
                reached.set()
                release.wait(10)
            return ok
        mock.patch.object(gw.task_attempts, "same_serve", side_effect=held).start()
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
        reached, release = self.paused("_retract_generation")
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
        data = gw._ATTEMPTS._load_json(gw._ATTEMPTS.path)
        data[tid] = ["damaged"]
        gw._ATTEMPTS.path.write_text(json.dumps(data))
        self.addCleanup(lambda: gw._ATTEMPTS.path.write_text(json.dumps(
            {k: v for k, v in gw._ATTEMPTS._load_json(gw._ATTEMPTS.path).items() if k != tid})))
        inflight = set()
        self.use(FakeGateway(**{"/v1/tasks/heartbeat": {"ok": True}}))
        gw._post_task_beats(inflight, force=True)
        self.assertIn(tid, inflight, "unknown is never released from flight")
        self.assertFalse(gw._ATTEMPTS.mark_dispatch_lost(tid, "x"), "no writer writes over it")
        self.assertEqual(gw._ATTEMPTS._load_json(gw._ATTEMPTS.path)[tid], ["damaged"])

    def test_an_older_result_settled_after_a_newer_serve_retires_only_itself(self):
        tid = "task-newer-holds"
        self.served_then(tid)
        self.ran(tid)
        old = time.time() - 5
        os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
        real = gw._ATTEMPTS.finish

        def finish(*a, **k):
            (gw.TASKS_DIR / "archive" / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.txt")
            self.assertIsNotNone(gw._write_task(_task(tid, lease_id="L2")))   # served again meanwhile
            return real(*a, **k)
        mock.patch.object(gw._ATTEMPTS, "finish", side_effect=finish).start()
        self.use(FakeGateway(**{"/v1/results": {"ok": True}}))
        inflight = {tid}
        gw._post_ready_results(inflight)
        self.assertIn(tid, inflight)
        self.assertTrue((gw.TASKS_DIR / f"{tid}.txt").exists(), "the newer serve's task stays queued")
        self.assertFalse((gw.RESULTS_DIR / f"{tid}.txt").exists(), "the settled result is retired")
        self.assertEqual(gw._ATTEMPTS.get(tid).get("lease_id"), "L2")
        gw._post_ready_results(inflight)
        self.assertEqual(gw._ATTEMPTS.get(tid).get("lease_id"), "L2", "nothing of the settled result is ruled again")
        self.assertEqual(list(gw.RESULTS_DIR.glob(f".delivering/{tid}*")), [])

    def test_a_retraction_waits_while_the_failure_may_be_sending(self):
        for blocker in ("sending", "records unreadable"):
            with self.subTest(blocker=blocker):
                tid = f"task-retract-{len(blocker)}"
                self.owed_failure(tid, lease="L1")
                gw._post_task_beats({tid}, force=True)
                self.use(FakeGateway(**{"/v1/results": _http(503, {"error": "busy"})}))
                gw._post_ready_results({tid})
                (gw.TASKS_DIR / "archive" / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.txt")
                self.assertIsNotNone(gw._write_task(_task(tid, lease_id="L2")))
                root = gw._delivery_core().backend.root
                items = gw._ATTEMPTS._wake_items_path
                if blocker == "sending":
                    self.assertTrue(sys.modules[gw.read_item.__module__].acquire_delivery_claim(root, tid, "another-worker"))
                else:
                    saved = items.read_bytes() if items.exists() else None
                    items.write_text("{torn")
                self.use(FakeGateway(**{f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
                self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L2"))
                self.assertEqual(gw._ATTEMPTS.get(tid)["execution_generation"], 1, "not moved while it may send")
                self.assertIsNotNone(gw.read_item(root, tid))
                if blocker == "sending":
                    sys.modules[gw.read_item.__module__].release_delivery_claim(root, tid, "another-worker")
                elif saved is None:
                    items.unlink()
                else:
                    items.write_bytes(saved)
                self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
                self.assertIsNone(gw.read_item(root, tid), "then taken back")
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
                self.assertTrue(gw._earlier_attempt_settling(tid, {"blocker_episode_id": "e1"}),
                                "a wake of the same id waits while the pin holds")
        self.assertEqual(len(list((gw.RESULTS_DIR / "undelivered").glob(f"{tid}*"))), 1, "quarantined once")
        for _pass in range(2):                                  # the pin ends, then the settled id leaves
            gw._post_ready_results(inflight)
        self.assertNotIn(tid, inflight)
        self.assertEqual(list(gw.RESULTS_DIR.glob(f".delivering/{tid}*")), [])
        self.assertFalse(gw._earlier_attempt_settling(tid, {"blocker_episode_id": "e1"}))
    def test_a_pinned_failure_replaced_at_its_name_is_still_withdrawn_by_a_newer_generation(self):
        for unpin in ("works", "fails, then works"):
            with self.subTest(unpin=unpin):
                tid = f"task-pinned-failure-{len(unpin)}"
                self.owed_failure(tid, lease="L1")
                gw._post_task_beats({tid}, force=True)
                fake = self.use(FakeGateway(**{"/v1/results": {"ok": True}, f"/v1/tasks/{tid}/ack": _ack(tid, 2)}))
                reached, release = self.paused("_relay_result")             # pinned, not yet ruled
                drain = threading.Thread(target=gw._post_ready_results, args=({tid},))
                drain.start()
                self.assertTrue(reached.wait(5))
                self.core_writes(tid, "the core's answer")
                (gw.TASKS_DIR / "archive" / f"{tid}.txt").rename(gw.TASKS_DIR / f"{tid}.txt")
                self.assertIsNotNone(gw._write_task(_task(tid, lease_id="L2")))
                if unpin.startswith("fails"):
                    real = os.unlink

                    def unlink(path, *a, **k):
                        if str(path).endswith(f"{tid}.pin"):
                            raise PermissionError("denied")
                        return real(path, *a, **k)
                    with mock.patch.object(gw.disposal.os, "unlink", side_effect=unlink):
                        self.assertFalse(gw._post_task_ack(tid, durable=True, lease_id="L2"))
                    self.assertEqual(gw._ATTEMPTS.get(tid)["execution_generation"], 1, "not moved: the pin holds")
                self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L2"))
                release.set()
                drain.join(10)
                self.unpatched()
                self.use(fake)
                self.assertEqual([p for p in fake.posted("/v1/results") if p["id"] == tid], [],
                                 "the withdrawn failure is never posted, under either generation")
                old = time.time() - 5
                os.utime(gw.RESULTS_DIR / f"{tid}.txt", (old, old))
                gw._post_ready_results({tid})
                self.assertEqual([(p["execution_generation"], p["body"]) for p in fake.posted("/v1/results")
                                  if p["id"] == tid], [(2, "the core's answer")])

if __name__ == "__main__":
    unittest.main()
