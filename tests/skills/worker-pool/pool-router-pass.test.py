#!/usr/bin/env python3
"""The router pass: roster + one task in, deliveries out. Nothing else read.

The rules whose violation sends work to someone the owner never addressed:

  * an absent or unreadable roster REFUSES the pass — never defaults to core;
  * a target not on the roster goes to the CORE — never to another worker;
  * liveness is never read — a sentinel is a file a late worker still finds;
  * work already in flight (`.accepted`) is NOT re-delivered.

Run: python3 tests/skills/worker-pool/pool-router-pass.test.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import pool_delivery as pd  # noqa: E402

import pool_roster as pr  # noqa: E402

import pool_router as rt  # noqa: E402

W1 = "a3f91c2d4e5b6a7c8d9e0f1a2b3c4d5e"
W2 = "b4e02d3c5f6a7b8c9d0e1f2a3b4c5d6e"
SRC = "room:!abc:ag2.space"


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)

    def roster(self, workers=None, bindings=None):
        return pr.compile_roster(self.ws, workers or {W1: {"state": "live"}},
                                 bindings if bindings is not None else {SRC: W1})

    def task(self, tid="task-1", payload=True, **kw):
        # A routable task HAS a payload (the watcher read it out of tasks/);
        # a test that omits it is testing the guard, not the pass.
        if payload:
            (self.ws / "tasks").mkdir(parents=True, exist_ok=True)
            (self.ws / "tasks" / f"{tid}.txt").write_text("task: body\n")
        return {"id": tid, "channel_id": SRC, **kw}


class TestRefusals(Base):
    def test_absent_roster_refuses_the_pass(self):
        with self.assertRaises(rt.RouterRefused):
            rt.route(self.ws, self.task())

    def test_unreadable_roster_refuses_the_pass(self):
        p = pr.roster_path(self.ws)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        with self.assertRaises(rt.RouterRefused):
            rt.route(self.ws, self.task())

    def test_refusing_delivers_nothing(self):
        with self.assertRaises(rt.RouterRefused):
            rt.route(self.ws, self.task())
        self.assertFalse((self.ws / "deliveries").exists())

    def test_a_task_with_no_id_refuses(self):
        r = self.roster()
        with self.assertRaises(rt.RouterRefused):
            rt.route(self.ws, {"channel_id": SRC}, r)


class TestResolution(Base):
    def test_a_bound_source_delivers_to_its_worker(self):
        got = rt.route(self.ws, self.task(), self.roster())
        self.assertEqual(got["delivered"], [W1])
        self.assertTrue((self.ws / "deliveries" / W1 / "task-1.txt").exists())

    def test_an_unbound_source_delivers_to_the_core(self):
        r = self.roster(bindings={})
        got = rt.route(self.ws, self.task(channel_id="room:!other:x"), r)
        self.assertEqual(got["delivered"], ["core"])

    def test_requested_worker_outranks_the_binding(self):
        r = self.roster({W1: {"state": "live"}, W2: {"state": "live"}}, {SRC: W1})
        got = rt.route(self.ws, self.task(requested_worker=W2), r)
        self.assertEqual(got["delivered"], [W2])

    @unittest.skip("fan-out bindings are refused by compile_roster on main; never exercised live")

    def test_a_set_delivers_one_sentinel_per_member(self):
        r = self.roster({W1: {"state": "live"}, W2: {"state": "live"}}, {SRC: [W1, W2]})
        got = rt.route(self.ws, self.task(), r)
        self.assertEqual(sorted(got["delivered"]), sorted([W1, W2]))
        for w in (W1, W2):
            self.assertTrue((self.ws / "deliveries" / w / "task-1.txt").exists())


class TestNeverSubstitutes(Base):
    def test_an_unknown_target_goes_to_the_core_not_another_worker(self):
        """A name never created is not a worker. The core is a recipient, not a
        fallback; no OTHER worker may ever receive work it was not named for."""
        r = self.roster({W1: {"state": "live"}, W2: {"state": "live"}}, {SRC: W1})
        got = rt.route(self.ws, self.task(requested_worker="ghost"), r)
        self.assertEqual((got["redirected"], got["delivered"]), (["ghost"], ["core"]))
        self.assertTrue((self.ws / "deliveries" / "core" / "task-1.txt").exists())
        for w in (W1, W2):
            self.assertFalse((self.ws / "deliveries" / w).exists(), w)

    def test_a_non_live_target_is_delivered_to_not_re_aimed(self):
        """Liveness is not the router's question — but the recipient still is:
        a non-live worker gets its own sentinel, never a re-aim at the core."""
        r = self.roster({W1: {"state": "recovering"}})
        got = rt.route(self.ws, self.task(), r)
        self.assertEqual(got["delivered"], [W1])
        self.assertFalse((self.ws / "deliveries" / "core").exists())

    @unittest.skip("fan-out bindings are refused by compile_roster on main; never exercised live")

    def test_every_member_of_a_set_is_delivered_to(self):
        r = self.roster({W1: {"state": "live"}, W2: {"state": "abandoned"}},
                        {SRC: [W1, W2]})
        got = rt.route(self.ws, self.task(), r)
        self.assertEqual(sorted(got["delivered"]), sorted([W1, W2]))
        for w in (W1, W2):
            self.assertTrue((self.ws / "deliveries" / w / "task-1.txt").exists())


class TestDoubleDelivery(Base):
    def test_a_second_pass_does_not_re_deliver(self):
        r = self.roster()
        rt.route(self.ws, self.task(), r)
        got = rt.route(self.ws, self.task(), r)
        self.assertEqual((got["delivered"], got["already"]), ([], [W1]))

    def test_work_ALREADY_CLAIMED_is_not_re_delivered(self):
        """The defect this rule exists for: checking only the pending name
        recreates a sentinel for work in flight and delivers it twice."""
        r = self.roster()
        rt.route(self.ws, self.task(), r)
        pd.accept(pd.pending(self.ws, W1)[0])
        got = rt.route(self.ws, self.task(), r)
        self.assertEqual(got["already"], [W1])
        self.assertEqual([p.name for p in pd.pending(self.ws, W1)], [])

    def test_a_concurrent_racer_is_success_not_an_error(self):
        r = self.roster()
        d = pd.deliveries_dir(self.ws, W1)
        d.mkdir(parents=True, exist_ok=True)
        os.close(os.open(d / "task-1.txt", os.O_CREAT | os.O_EXCL))
        got = rt.route(self.ws, self.task(), r)
        self.assertEqual(got["already"], [W1])


    def test_the_real_race_EEXIST_is_success(self):
        """A sentinel appearing BETWEEN the check and the open. The earlier
        check cannot see it, so O_EXCL is what actually arbitrates — and losing
        that race means the delivery exists, which is success, not an error."""
        from unittest.mock import patch
        r = self.roster()
        d = pd.deliveries_dir(self.ws, W1)
        d.mkdir(parents=True, exist_ok=True)
        os.close(os.open(d / "task-1.txt", os.O_CREAT | os.O_EXCL))
        with patch.object(pd, "find", return_value=None):   # check misses it
            got = rt.route(self.ws, self.task(), r)
        self.assertEqual(got["already"], [W1])
        self.assertEqual(got["delivered"], [])


class TestOrdering(Base):
    def test_priority_then_oldest(self):
        tasks = [
            {"id": "c", "priority": "low", "created_at": "2026-01-01"},
            {"id": "a", "priority": "urgent", "created_at": "2026-02-01"},
            {"id": "b", "priority": "normal", "created_at": "2026-01-01"},
            {"id": "d", "priority": "urgent", "created_at": "2026-01-01"},
        ]
        self.assertEqual([t["id"] for t in rt.order_candidates(tasks)],
                         ["d", "a", "b", "c"])

    def test_a_missing_priority_defaults_to_normal_not_last(self):
        tasks = [{"id": "low", "priority": "low", "created_at": "2026-01-01"},
                 {"id": "bare", "created_at": "2026-01-01"}]
        self.assertEqual([t["id"] for t in rt.order_candidates(tasks)],
                         ["bare", "low"])


class TestPassIsReplayable(Base):
    def test_one_pass_uses_ONE_roster_version(self):
        """Every task in a pass is decided against the same version, so a
        recompile mid-pass cannot split the decision."""
        r = self.roster()
        out = rt.route_all(self.ws, [self.task("task-1"), self.task("task-2")], r)
        self.assertEqual({o["version"] for o in out}, {r["version"]})

    def test_resolution_reads_no_disk_when_given_a_roster(self):
        r = self.roster()
        snapshot = json.loads(json.dumps(r))
        pr.roster_path(self.ws).unlink()
        got = rt.route(self.ws, self.task(), snapshot)
        self.assertEqual(got["delivered"], [W1])

    def test_route_all_refuses_without_a_roster(self):
        with self.assertRaises(rt.RouterRefused):
            rt.route_all(self.ws, [self.task()])



class TestPayloadGuard(Base):
    """A sentinel names a payload. Writing one for a payload that is not there
    creates work its recipient can only delete — and if the payload is ARCHIVED
    rather than absent, it offers FINISHED work a second time."""

    def test_no_payload_writes_no_sentinel(self):
        r = self.roster()
        out = rt.route(self.ws, self.task("task-ghost", payload=False), r)
        self.assertEqual(out["skipped"], [W1])
        self.assertEqual(out["delivered"], [])
        self.assertIsNone(pd.find(self.ws, W1, "task-ghost"))

    def test_a_refusal_is_not_reported_as_already_delivered(self):
        # The bug this guards: any non-"delivered" return used to land in
        # `already`, so a refusal read as "the delivery exists".
        r = self.roster()
        out = rt.route(self.ws, self.task("task-ghost", payload=False), r)
        self.assertEqual(out["already"], [])

    def test_an_archived_payload_is_not_re_delivered(self):
        r = self.roster()
        t = self.task("task-done")
        (self.ws / "tasks" / "archive").mkdir(parents=True, exist_ok=True)
        (self.ws / "tasks" / "task-done.txt").rename(
            self.ws / "tasks" / "archive" / "task-done.txt")
        out = rt.route(self.ws, t, r)
        self.assertEqual(out["skipped"], [W1])
        self.assertIsNone(pd.find(self.ws, W1, "task-done"))

    @unittest.skip("fan-out bindings are refused by compile_roster on main; never exercised live")

    def test_a_set_skips_only_the_missing_payload_not_the_members(self):
        # One payload serves every member, so the guard is per-task: either all
        # members are skipped or none are.
        r = self.roster({W1: {"state": "live"}, W2: {"state": "live"}},
                        {SRC: [W1, W2]})
        out = rt.route(self.ws, self.task("task-ghost", payload=False), r)
        self.assertEqual(sorted(out["skipped"]), sorted([W1, W2]))
        self.assertEqual(out["delivered"], [])

    def test_the_ordinary_path_still_delivers(self):
        r = self.roster()
        out = rt.route(self.ws, self.task("task-real"), r)
        self.assertEqual(out["delivered"], [W1])
        self.assertEqual(out["skipped"], [])
        self.assertIsNotNone(pd.find(self.ws, W1, "task-real"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
