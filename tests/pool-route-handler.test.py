#!/usr/bin/env python3
"""The router as the core watcher's handler: which exit code, and why.

The exit code IS the routing decision — the watcher acts on nothing else — so
each branch is pinned against the rule it encodes rather than against a number.

Run: python3 tests/pool-route-handler.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

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
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]), h.TAKE)

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
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]), h.TAKE)
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        self.assertTrue((self.ws / "deliveries" / W / "task-1.txt").exists())

    def test_an_absent_roster_refuses_rather_than_declining(self):
        """Declining is the core. An unreadable file must not choose a recipient."""
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]),
                         h.TAKE)

    def test_a_corrupt_roster_refuses_too(self):
        (self.ws / "state" / "roster.json").write_text("{broken")
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]),
                         h.TAKE)


class TestIntentionLayer(Base):
    def test_a_requested_label_reaches_the_worker(self):
        self.roster(label="worker-1", bindings={})
        t = self.task_file("task-1", channel_id="!unbound:x", requested_worker="worker-1")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws), "--probe"]), h.TAKE)
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
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws), "--probe"]), h.TAKE)

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
        self.assertEqual(h.main(["--task-file", str(p), "--workspace", str(self.ws), "--probe"]), h.TAKE)



class TestFailureAfterTheProbe(Base):
    def log(self):
        p = self.ws / "logs" / "pool-route-handler.log"
        return p.read_text() if p.exists() else ""

    def test_a_refused_pass_exits_nonzero_and_says_why(self):
        """Probe 0 queued it as taken, so a non-zero run falls back to the live
        core, never to the owner. The reason goes to the log the watcher lacks."""
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {W: {"state": "live"}, "f" * 32: {"state": "live"}},
             "bindings": {"!room:x": [W, "f" * 32]}}))
        t = self.task_file("task-1", channel_id="!room:x")
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 1)
        self.assertIn("refused", self.log())
        self.assertFalse((self.ws / "deliveries" / W / "task-1.txt").exists())

    def test_a_task_archived_before_the_run_is_nothing_to_route(self):
        """Seen live: the worker finished and the bridge archived the payload
        between the probe and the run; exit 1 then sent the task to the core."""
        self.roster()
        t = self.task_file("task-1", channel_id="!room:x")
        Path(t).unlink()
        self.assertEqual(h.main(["--task-file", t, "--workspace", str(self.ws)]), 0)
        self.assertIn("gone before the run", self.log())


if __name__ == "__main__":
    unittest.main(verbosity=0)
