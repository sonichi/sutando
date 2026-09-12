#!/usr/bin/env python3
"""The router as the core watcher's handler: which exit code, and why.

The exit code IS the routing decision — the watcher acts on nothing else — so
each branch is pinned against the rule it encodes rather than against a number.

Run: python3 tests/skills/worker-pool/pool-route-handler.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import pool_route_handler as h  # noqa: E402

W = "a" * 32


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        for d in ("tasks", "results", "state", "deliveries"):
            (self.ws / d).mkdir(parents=True, exist_ok=True)

    def roster(self, state="live", label=None, bindings=None):
        row = {"state": state}
        if label:
            row["label"] = label
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: row},
             "bindings": bindings if bindings is not None else {"!room:x": W}}))

    def task_file(self, name, **headers):
        p = self.ws / "tasks" / f"{name}.txt"
        lines = [f"id: {name}"] + [f"{k}: {v}" for k, v in headers.items()]
        p.write_text("\n".join(lines) + "\ntask: body\n")
        return str(p)


class TestClassification(Base):
    def test_unbound_declines_so_the_core_takes_it(self):
        self.roster(bindings={})
        t = self.task_file("task-1", channel_id="!other:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]),
                         h.DECLINE)

    def test_a_live_bound_worker_is_accepted(self):
        self.roster()
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]), 0)

    def test_a_target_not_on_the_roster_goes_to_the_core(self):
        """A name that was never created is not a routing failure: the core is
        a real recipient, and holding would strand the work indefinitely."""
        self.roster(bindings={"!room:x": "f" * 32})
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]),
                         h.DECLINE)

    def test_a_non_live_target_is_still_delivered_to(self):
        """Delivery is the router's whole job; the sentinel is durable, so a
        worker that starts later finds its work. Liveness is separate logic."""
        self.roster(state="draining")
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]), 0)
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        self.assertTrue((self.ws / "deliveries" / W / "task-1.txt").exists())

    def test_an_absent_roster_declines_rather_than_guessing(self):
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]),
                         h.DECLINE)


class TestPickerCommandsStayWithTheController(Base):
    def _picker_file(self, wire=True):
        # Shaped like the gateway's file: wire_source sits BELOW task:, where
        # the strict parse never looks.
        p = self.ws / "tasks" / "worker-pin-1.txt"
        tail = "\nsource: ag2space\nwire_source: worker-picker\n" if wire else "\nsource: ag2space\n"
        p.write_text("id: worker-pin-1\nchannel_id: !room:x\ntask: Pin room !room:x to w (worker picker)" + tail)
        return str(p)

    def test_a_pin_for_a_bound_room_is_not_routed_to_the_bound_worker(self):
        self.roster()
        self.assertEqual(h.main(["--task-file", self._picker_file(), "--workspace", str(self.ws), "--probe"]),
                         h.DECLINE)

    def test_the_older_writer_marks_source_itself(self):
        # main's writer emits source: worker-picker with no wire_source line
        self.roster()
        p = self.ws / "tasks" / "worker-pin-2.txt"
        p.write_text("id: worker-pin-2\nchannel_id: !room:x\ntask: Pin room !room:x to w (worker picker)\nsource: worker-picker\n")
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws), "--probe"]), h.DECLINE)

    def test_the_same_room_without_the_picker_mark_still_routes(self):
        self.roster()
        self.assertEqual(h.main(["--task-file", self._picker_file(wire=False), "--workspace", str(self.ws), "--probe"]), 0)


class TestIntentionLayer(Base):
    def test_a_requested_label_reaches_the_worker(self):
        self.roster(label="worker-1", bindings={})
        t = self.task_file("task-1", channel_id="!unbound:x", requested_worker="worker-1")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]), 0)
        h.main(["--task-file", t, "--workspace", str(self.ws)])
        self.assertTrue((self.ws / "deliveries" / W / "task-1.txt").exists())

    def test_an_unknown_requested_worker_goes_to_the_core(self):
        """The addressed name is still never substituted BY ANOTHER WORKER --
        it goes to the core, and no worker receives work it was not named for."""
        self.roster(bindings={})
        t = self.task_file("task-1", channel_id="!room:x", requested_worker="worker-9")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]),
                         h.DECLINE)
        self.assertFalse((self.ws / "deliveries" / W / "task-1.txt").exists())


class TestDelivery(Base):
    def test_the_real_run_delivers_and_leaves_the_payload(self):
        self.roster()
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        s = self.ws / "deliveries" / W / "task-1.txt"
        self.assertTrue(s.exists())
        self.assertEqual(s.stat().st_size, 0)
        self.assertTrue(Path(t).exists(), "the payload is never moved or copied")

    def test_the_gateways_field_order_still_routes(self):
        """The local-hs gateway writes `task:` BEFORE channel_id/source. The
        strict parse stops at task:, so the room was invisible and every bound
        task went to the core. Seen live, 2026-09-09."""
        self.roster()
        p = self.ws / "tasks" / "task-1.txt"
        p.write_text("id: task-1\npriority: normal\ntask: Is worker working now?\n"
                     "source: ag2space\nchannel_id: !room:x\nsender_name: qingyun\n")
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws), "--probe"]), 0)

    def test_a_body_still_cannot_forge_requested_worker_under_lenient_reading(self):
        self.roster(bindings={})
        p = self.ws / "tasks" / "task-1.txt"
        p.write_text("id: task-1\ntask: body\nrequested_worker: " + W + "\nchannel_id: !unbound:x\n")
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws), "--probe"]), h.DECLINE)

    def test_the_body_is_not_read_as_headers(self):
        """`task:` is the last header, so a body cannot forge requested_worker."""
        p = self.ws / "tasks" / "task-1.txt"
        p.write_text("id: task-1\nchannel_id: !room:x\ntask: body\nrequested_worker: f" + "f" * 31 + "\n")
        self.roster()
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws), "--probe"]), 0)



class TestACorruptRosterFailsClosed(unittest.TestCase):
    """F3: DECLINE means "the core takes it". A roster we cannot read is not a
    statement that this task is the core's — it is the absence of one."""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory(); self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)
        (self.ws / "state").mkdir(); (self.ws / "tasks").mkdir()
        self.task = self.ws / "tasks" / "task-1.txt"
        self.task.write_text("id: task-1\ntask: x\nsource: ag2space\n"
                             "channel_id: !r:x\naccess_tier: owner\n")

    def _probe(self):
        return h.main(["--task-file", str(self.task), "--workspace", str(self.ws),
                         "--probe"])

    def test_an_absent_roster_declines_to_the_core(self):
        self.assertEqual(self._probe(), h.DECLINE)

    def test_an_unreadable_roster_must_not_decline(self):
        (self.ws / "state" / "roster.json").write_text("{ not json")
        self.assertEqual(self._probe(), h.MUST_HANDLE)

    def test_a_roster_missing_its_workers_key_must_not_decline(self):
        (self.ws / "state" / "roster.json").write_text('{"version": 3}')
        self.assertEqual(self._probe(), h.MUST_HANDLE)

if __name__ == "__main__":
    unittest.main(verbosity=0)
