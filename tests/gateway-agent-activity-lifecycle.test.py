#!/usr/bin/env python3
"""The bridge's task lifecycle (REMOTE_TASK_LIFECYCLE_V2 + REMOTE_TASK_TURN_CONTEXT), on the real loop.

A bridge process polls a relay on a real socket: the ack names the lease generation, the
attempt's turn file appears, and the result carries that generation and the lease holder.
A result whose text the turn already sent as a room message is posted without a send, and
a cancelled ack stops the core through its cancel signal and is acknowledged.

Run: python3 tests/gateway-agent-activity-lifecycle.test.py
"""
from __future__ import annotations

import hashlib
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.gateway_relay import Bridges, Relay  # noqa: E402

TID = "task-aa1"
FLAGS = {"REMOTE_TASK_LIFECYCLE_V2": "1", "REMOTE_TASK_TURN_CONTEXT": "1"}


def _task(**extra):
    return {"id": TID, "task": "what is 2+2?", "source": "ag2space", "user_id": "@owner:ag2.space",
            "access_tier": "owner", "source_room_id": "!room:ag2.space", "thread_root": "$root",
            **extra}


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="agent-activity-"))
        self.relay = Relay()
        self.addCleanup(self.relay.close)
        self.bridges = Bridges(self, self.root, self.relay)
        self.bridges.env.update(FLAGS)
        self.state = self.bridges.ws / "state"

    def _ack_with_generation(self, gen=3):
        def on_post(path, payload):
            if path.endswith("/ack"):
                self.relay.acks.append(path.split("/")[-2])
                self.relay.ack_bodies.append(payload)
                return 200, {"ok": True, "durable": True, "task_id": TID, "execution_generation": gen,
                             "attempt": gen, "lease_expires_at": 1761354125.0}
            return None
        self.relay.on_post = on_post

    def _turn_files(self):
        return sorted((self.state / "turns").glob(f"{TID}_g*_*.json"))

    def test_the_ack_generation_rides_the_turn_file_and_the_result(self):
        self._ack_with_generation(3)
        self.relay.queue.append(_task())
        proc = self.bridges.start()
        self.bridges.wait_for(lambda: self.relay.acks, proc, "the ack")
        body = self.relay.ack_bodies[0]
        self.assertEqual(body["protocol_version"], 2)
        worker = body["worker_id"]
        self.assertRegex(worker, r"^[0-9a-f]{32}$")
        self.bridges.wait_for(self._turn_files, proc, "the turn file")
        turn = self._turn_files()[0]
        self.assertEqual(stat.S_IMODE(turn.stat().st_mode), 0o400)
        ctx = json.loads(turn.read_text())
        self.assertEqual((ctx["task_id"], ctx["execution_generation"], ctx["room_id"], ctx["worker_id"]),
                         (TID, 3, "!room:ag2.space", worker))

        (self.bridges.ws / "results" / f"{TID}.txt").write_text("4")
        self.bridges.wait_for(lambda: self.relay.results, proc, "the result")
        result = self.relay.results[0]
        self.assertEqual((result["id"], result["body"], result["execution_generation"],
                          result["worker_id"], result["status"]), (TID, "4", 3, worker, "completed"))
        self.assertNotIn("no_send", result)
        self.bridges.wait_for(lambda: (self.state / "turns" / f"{turn.stem}.closed").exists(),
                              proc, "the turn closed")
        self.assertFalse(turn.exists())

    def test_a_result_already_sent_as_a_room_message_is_posted_without_a_send(self):
        self._ack_with_generation(1)
        self.relay.queue.append(_task())
        proc = self.bridges.start()
        self.bridges.wait_for(self._turn_files, proc, "the turn file")
        turn_id = self._turn_files()[0].stem
        intent = {"v": 2, "record_type": "dispatch_intent", "ts": 4e9, "turn_id": turn_id, "seq": 1,
                  "operation_id": f"op_{turn_id}_1", "task_id": TID, "execution_generation": 1,
                  "action": "room.message.send", "requested_task_status": "working",
                  "body_sha256": hashlib.sha256(b"4").hexdigest()}
        with open(self.state / "room-actions.jsonl", "a") as fh:
            fh.write(json.dumps(intent) + "\n")
        (self.bridges.ws / "results" / f"{TID}.txt").write_text("4\n")
        self.bridges.wait_for(lambda: self.relay.results, proc, "the result")
        result = self.relay.results[0]
        self.assertEqual((result.get("no_send"), result["status"]), (True, "completed"))

    def test_a_woken_task_is_never_closed_as_a_redelivery(self):
        self._ack_with_generation(1)
        self.relay.queue.append(_task())
        proc = self.bridges.start()
        self.bridges.wait_for(self._turn_files, proc, "the turn file")
        (self.bridges.ws / "results" / f"{TID}.txt").write_text("asked the owner")
        self.bridges.wait_for(lambda: self.relay.results, proc, "the first result")
        acks = len(self.relay.acks)
        self.relay.queue.append(_task(wake={"blocker_episode_id": "ep1", "resolved_blockers": [
            {"blocker_id": "b1", "outcome": "answered", "answer": {"kind": "text", "text": "yes"}}]}))
        self.bridges.wait_logged(proc, f"wake for {TID}: not handled")
        self.assertEqual(len(self.relay.acks), acks, "a wake this client cannot run is not claimed")
        self.assertEqual(len(self.relay.results), 1, "and never answered with a no-send redelivery")

    def test_a_cancelled_ack_withdraws_the_untaken_task_and_is_acknowledged(self):
        def on_post(path, payload):
            if path.endswith("/ack"):
                return 409, {"error": "task cancelled", "error_code": "task_cancelled",
                             "cancel_requested_generation": 2, "cancel_requested_attempt": 2}
            if path.endswith("/cancel-ack"):
                return 200, {"ok": True, "task_id": TID, "cancellation_state": "cancel_confirmed",
                             "terminal": True}
            return None
        self.relay.on_post = on_post
        self.relay.queue.append(_task())
        proc = self.bridges.start()
        cancel_acks = lambda: [p for p, _ in self.relay.post_paths if p.endswith("/cancel-ack")]  # noqa: E731
        self.bridges.wait_for(cancel_acks, proc, "the cancel-ack")
        body = next(b for p, b in self.relay.post_paths if p.endswith("/cancel-ack"))
        self.assertEqual((body["cancel_requested_generation"], body["cancel_requested_attempt"]), (2, 2))
        self.assertRegex(body["worker_id"], r"^[0-9a-f]{32}$")
        self.bridges.wait_logged(proc, "its untaken task file was withdrawn")
        self.assertFalse((self.bridges.ws / "tasks" / f"{TID}.txt").exists(), "the core never starts it")
        self.assertEqual(list((self.bridges.ws / "tasks").glob("task-ctl-*.txt")), [])


if __name__ == "__main__":
    unittest.main()
