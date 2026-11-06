#!/usr/bin/env python3
"""Ready results go out in intake order, one at a time per room, rooms in parallel.

The real bridge module, imported hermetically with the attempt ledger on, against a fake `_req`:
  1. ten results that are ready in one pass are posted in the order their tasks were taken,
     whatever the in-flight set's hash order (re-run under several PYTHONHASHSEEDs);
  2. a reply whose room is still sending an earlier one waits for the next pass, then goes out;
  3. a slow send in one room never holds another room's reply;
  4. a result that posts nothing ([no-send]) is never held behind its room's send;
  5. with the ledger off, the serial drain also posts in intake order;
  6. intake order survives a restart: the task-room sidecar keeps insertion order;
  7. a reply still in flight after its turn (a deferred upload, an unreadable alias ledger, a held
     result, an unconfirmed post, a run not ready to offer it) holds its room's next reply until it is out, in this pass and the
     next; past ROOM_ORDER_MAX_WAIT_S a stuck reply stops holding them.

Run: python3 tests/gateway-ordered-result-delivery.test.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.paired_result import publish_result
from _helpers.hermetic_env import isolated_child_env  # noqa: E402
from _helpers.hermetic_gateway import isolate_then_import  # noqa: E402

gw, _READS = isolate_then_import(env={"REMOTE_TASK_LIFECYCLE_V2": "1", "REMOTE_TASK_TURN_CONTEXT": "1"})
assert gw._ATTEMPTS.take_ownership()

BURST_ONLY = "--burst-only"
ROOM_A, ROOM_B = "!room-a:example.org", "!room-b:example.org"


class FakeGateway:
    def __init__(self):
        self.calls, self.hold = [], {}
        self.lock = threading.Lock()

    def __call__(self, method, path, payload=None, timeout=None, **k):
        path = path.split("?")[0]
        if path.endswith("/ack"):
            tid = payload["id"]
            return {"ok": True, "durable": True, "task_id": tid, "execution_generation": 1,
                    "attempt": 1, "lease_expires_at": 1761354125.0}
        if path == "/v1/results":
            gate = self.hold.get(payload["id"])
            if gate is not None:
                gate[0].set()
                gate[1].wait(30)
            with self.lock:
                self.calls.append(payload["id"])
        return {"ok": True}


def _until(cond, timeout=30.0) -> bool:
    done = threading.Event()
    end = 0.0
    while end < timeout:
        if cond():
            return True
        done.wait(0.05)
        end += 0.05
    return bool(cond())


class Harness(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(gw, "_log", self.logs.append).start()
        self.fake = FakeGateway()
        mock.patch.object(gw, "_req", self.fake).start()

    def take(self, tid, room):
        task = {"id": tid, "task": "reply", "source": "ag2space", "user_id": "@o:x",
                "access_tier": "owner", "channel_id": room, "lease_id": "L1"}
        self.assertTrue(gw._take_served_task(task, set(), []))
        self.assertTrue(gw._post_task_ack(tid, durable=True, lease_id="L1"))

    def core_writes(self, tid, text):
        publish_result(gw.RESULTS_DIR / f"{tid}.txt", text)

    def drain(self, inflight):
        th = threading.Thread(target=gw._post_ready_results, args=(inflight,))
        th.start()
        return th



class Ordered(Harness):
    def test_room_order_uses_only_the_requested_tasks_verified_result(self):
        tid = "task-room-proof"
        path = gw.RESULTS_DIR / f"{tid}.txt"
        rooms = {tid: ROOM_A, "task-other-proof": ROOM_A}
        path.write_text("unpaired")
        self.assertIsNone(gw._reply_room(tid, path, rooms))
        self.core_writes(tid, "verified answer")
        self.assertEqual(gw._reply_room(tid, path, rooms), ROOM_A)
        self.assertIsNone(gw._reply_room("task-other-proof", path, rooms))
        path.write_text("substituted")
        self.assertIsNone(gw._reply_room(tid, path, rooms))

    def test_a_burst_ready_in_one_pass_is_posted_in_intake_order(self):
        # Taken in descending id order, so neither id order nor set order can pass by chance.
        tids = [f"task-burst-{n:02d}" for n in range(9, -1, -1)]
        for tid in tids:
            self.take(tid, ROOM_A)
        for tid in tids:
            self.core_writes(tid, f"answer {tid}")
        inflight = set(tids)
        self.drain(inflight).join(60)
        self.assertTrue(_until(lambda: not inflight), repr(self.fake.calls))
        self.assertEqual(self.fake.calls, tids)

    def test_a_reply_waits_for_its_rooms_earlier_send_and_another_room_does_not(self):
        a1, a2, b1 = "task-slow-a1", "task-slow-a2", "task-slow-b1"
        for tid, room in ((a1, ROOM_A), (a2, ROOM_A), (b1, ROOM_B)):
            self.take(tid, room)
        stalled, release = threading.Event(), threading.Event()
        self.fake.hold[a1] = (stalled, release)
        mock.patch.object(gw, "DELIVERY_PASS_WAIT_S", 0.2).start()
        inflight = {a1, a2, b1}
        self.core_writes(a1, "first in room A")
        first = self.drain(inflight)
        self.assertTrue(stalled.wait(10))
        try:
            first.join(10)
            self.core_writes(a2, "second in room A")
            self.core_writes(b1, "only in room B")
            second = self.drain(inflight)
            second.join(20)
            self.assertFalse(second.is_alive(), "a pass never waits on room A's send")
            self.assertEqual(self.fake.calls, [b1], "room B is answered while room A is sending")
            self.assertIn(a2, inflight, "room A's second reply waits for its first")
            gw._record_task_room("task-slow-nofile", ROOM_A)
            self.assertIsNone(gw._reply_room("task-slow-nofile", gw.RESULTS_DIR / "task-slow-nofile.txt",
                                             gw._load_task_rooms()), "no result yet: the ordinary path")
        finally:
            release.set()
        self.assertTrue(_until(lambda: not gw._room_busy(ROOM_A)))
        self.drain(inflight).join(20)
        self.assertTrue(_until(lambda: len(self.fake.calls) == 3), repr(self.fake.calls))
        self.assertEqual(self.fake.calls, [b1, a1, a2])

    def test_a_result_that_posts_nothing_is_not_held_behind_its_rooms_send(self):
        a1, a2 = "task-skip-a1", "task-skip-a2"
        for tid in (a1, a2):
            self.take(tid, ROOM_A)
        stalled, release = threading.Event(), threading.Event()
        self.fake.hold[a1] = (stalled, release)
        mock.patch.object(gw, "DELIVERY_PASS_WAIT_S", 0.2).start()
        inflight = {a1, a2}
        self.core_writes(a1, "a reply")
        first = self.drain(inflight)
        self.assertTrue(stalled.wait(10))
        try:
            first.join(10)
            self.core_writes(a2, "[no-send] nothing to show")
            self.drain(inflight).join(20)
            _until(lambda: a2 not in inflight, 10)
            self.assertNotIn(a2, inflight, "a skip result settles while its room is sending")
        finally:
            release.set()
        self.assertTrue(_until(lambda: a1 not in inflight))
        self.assertEqual(self.fake.calls, [a2, a1], "the skip closed its task before the held reply")

    def test_without_the_ledger_the_serial_drain_also_posts_in_intake_order(self):
        tids = [f"task-serial-{n:02d}" for n in range(9, -1, -1)]
        for tid in tids:
            gw._record_task_room(tid, ROOM_A)
            self.core_writes(tid, f"answer {tid}")
        order = []
        mock.patch.object(gw, "_ATTEMPTS", None).start()

        def delivered(tid, rfile, raw, generation, inflight):
            order.append(tid)
            inflight.discard(tid)
            return True
        mock.patch.object(gw, "_deliver_ready", side_effect=delivered).start()
        gw._post_ready_results(set(tids))
        self.assertEqual(order, tids)

    def test_the_task_room_sidecar_keeps_intake_order_across_a_restart(self):
        tids = ["task-zz", "task-aa", "task-mm"]
        for tid in tids:
            gw._record_task_room(tid, ROOM_B)
        on_disk = list(json.loads(gw.TASK_ROOMS_FILE.read_text()))
        self.assertEqual([t for t in on_disk if t in tids], tids)
        self.assertEqual(gw._intake_ordered(set(tids) | {"task-unknown"}, gw._load_task_rooms())[-4:],
                         tids + ["task-unknown"])


class InFlight(Harness):
    """The room's first reply does not go out on its first turn; the second must not overtake it."""

    def first_stays_in_flight(self, cause):
        tag = self._testMethodName.split("_")[-1]
        a1, a2 = f"task-inf-{tag}-a1", f"task-inf-{tag}-a2"
        for tid in (a1, a2):
            self.take(tid, ROOM_A)
        gate = {"block": True}
        cause(a1, gate)
        self.core_writes(a1, self.first_body)
        self.core_writes(a2, "second")
        inflight = {a1, a2}
        self.drain(inflight).join(30)
        self.assertTrue(_until(lambda: not gw._room_busy(ROOM_A)))
        self.assertNotIn(a2, self.fake.calls, "the second reply never overtakes the first")
        self.assertIn(a2, inflight, "\n".join(m for m in self.logs if a2 in m or a1 in m))
        self.drain(inflight).join(30)                       # still in flight: the room still waits
        self.assertTrue(_until(lambda: not gw._room_busy(ROOM_A)))
        self.assertNotIn(a2, self.fake.calls, "and not on the next pass either")
        gate["block"] = False
        for _ in range(40):                                 # an unconfirmed post retries on its own back-off
            if not inflight:
                break
            self.drain(inflight).join(30)
            _until(lambda: not gw._room_busy(ROOM_A), 10)
            threading.Event().wait(0.25)
        self.assertEqual([t for t in self.fake.calls if t in (a1, a2)], [a1, a2],
                         "\n".join(m for m in self.logs if a2 in m or a1 in m))
        self.assertEqual(inflight, set())

    first_body = "first"

    def test_a_deferred_upload(self):
        (gw.RESULTS_DIR / "inf-a1.txt").write_text("media")
        self.first_body = f"first\n[file: {gw.RESULTS_DIR / 'inf-a1.txt'}]"

        def cause(a1, gate):
            mock.patch.object(gw, "_load_task_media",
                              return_value={gw._broker_tid(a1): {"mode": "task-media"}}).start()
            mock.patch.object(gw, "_upload_task_attachment",
                              side_effect=lambda *a, **k: ("defer", "busy") if gate["block"] else ("ok", "")).start()
        self.first_stays_in_flight(cause)

    def test_an_unreadable_alias_ledger(self):
        real = gw._delivery_tid

        def cause(a1, gate):
            mock.patch.object(gw, "_delivery_tid",
                              side_effect=lambda t: None if gate["block"] and t == a1 else real(t)).start()
        self.first_stays_in_flight(cause)

    def test_a_held_result(self):
        real = gw._rule_result

        def cause(a1, gate):
            mock.patch.object(gw, "_rule_result", side_effect=lambda tid, *a: gw._Ruling(
                "hold", why="waiting on a cancel-ack") if gate["block"] and tid == a1 else real(tid, *a)).start()
        self.first_stays_in_flight(cause)

    def test_an_unconfirmed_post(self):
        def cause(a1, gate):
            real_call = self.fake.__call__

            def flaky(method, path, payload=None, timeout=None, **k):
                if gate["block"] and path.split("?")[0] == "/v1/results" and payload.get("id") == a1:
                    raise ConnectionResetError("reset before the answer")
                return real_call(method, path, payload, timeout, **k)
            mock.patch.object(gw, "_req", flaky).start()
        self.first_stays_in_flight(cause)

    def test_a_reply_its_run_cannot_offer_yet(self):
        real = gw._run_holds_result

        def cause(a1, gate):
            mock.patch.object(gw, "_run_holds_result",
                              side_effect=lambda t, f: None if gate["block"] and t == a1 else real(t, f)).start()
        self.first_stays_in_flight(cause)

    def test_a_stuck_reply_stops_holding_its_room_after_the_bound(self):
        mock.patch.object(gw, "ROOM_ORDER_MAX_WAIT_S", 0.0).start()
        a1, a2 = "task-stuck-a1", "task-stuck-a2"
        for tid in (a1, a2):
            self.take(tid, ROOM_A)
        real = gw._delivery_tid
        mock.patch.object(gw, "_delivery_tid", side_effect=lambda t: None if t == a1 else real(t)).start()
        self.core_writes(a1, "stuck")
        self.core_writes(a2, "second")
        os.utime(gw.RESULTS_DIR / f"{a1}.txt", (1, 1))
        inflight = {a1, a2}
        self.drain(inflight).join(30)
        self.assertTrue(_until(lambda: a2 in self.fake.calls), "the room is not silenced for good")
        self.assertIn(a1, inflight)
        self.assertTrue(any("go on without it" in m for m in self.logs), self.logs)


class HashSeeds(unittest.TestCase):
    def test_the_burst_order_holds_under_several_hash_seeds(self):
        for seed in ("1", "2", "3", "4"):
            env = isolated_child_env(PYTHONHASHSEED=seed)
            proc = subprocess.run([sys.executable, __file__, BURST_ONLY], env=env, capture_output=True, text=True,
                                  timeout=300)
            self.assertEqual(proc.returncode, 0, f"seed {seed}:\n{proc.stdout}\n{proc.stderr}")


if __name__ == "__main__":
    # The bridge import replaces the environment, so the child's mode rides argv.
    if BURST_ONLY in sys.argv:
        sys.argv[1:] = ["Ordered.test_a_burst_ready_in_one_pass_is_posted_in_intake_order"]
    unittest.main()
