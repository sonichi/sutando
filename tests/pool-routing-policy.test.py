#!/usr/bin/env python3
"""Routing policy seam: the choice is pluggable, the lead's enforcement is
not, and a broken policy degrades to the historical default instead of
stranding the queue. Single-core is the same policy over a pool of one.

Exercises the production sweep() and acquire_work() paths — real task
files, real renames. Run: python3 tests/pool-routing-policy.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

from pool_lead import PoolLead  # noqa: E402
from pool_routing import (  # noqa: E402
    CORE_ID, Router, RoutingConfig, TaskMeta, WorkerView, build_router,
    load_config, solo_pick)
import pool_follower  # noqa: E402


def task(**hdr):
    base = {"source": "slack", "channel_id": "C1", "access_tier": "owner",
            "priority": "normal", "user_id": "U1"}
    base.update({k: v for k, v in hdr.items() if v is not None})
    lines = "".join(f"{k}: {v}\n" for k, v in base.items())
    return f"id: x\n{lines}task: hi\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks, self.state = root / "tasks", root / "state"
        self.tasks.mkdir()
        self.state.mkdir()
        (root / "results").mkdir()
        self.pool = ["core-1", "core-2"]
        self.core = None

    def tearDown(self):
        self.tmp.cleanup()

    def lead(self):
        return PoolLead(self.tasks, self.state,
                        followers_fn=lambda: list(self.pool),
                        alive_fn=lambda i: True, now_fn=lambda: 1_000.0,
                        core_fn=lambda: self.core)

    def routing(self, **cfg):
        (self.state / "pool").mkdir(exist_ok=True)
        (self.state / "pool" / "routing.json").write_text(json.dumps(cfg))

    def write(self, name, body):
        (self.tasks / name).write_text(body)

    def assigned(self):
        return {f.name.split(".assigned-")[0] + ".txt": f.name.split(".assigned-")[1][:-4]
                for f in self.tasks.iterdir() if ".assigned-" in f.name}

    def trace(self, event):
        p = self.state / "pool" / "liveness-trace.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines()
                if json.loads(l).get("event") == event]


class NoConfigIsHistoricalBehaviour(Base):
    def test_no_config_matches_pick(self):
        # CONTROL: with no routing.json the policy IS _pick — same answer.
        self.write("task-1.txt", task())
        lead = self.lead()
        expect = lead._pick("C1", self.pool, {}, "owner")
        lead.sweep()
        self.assertEqual(self.assigned()["task-1.txt"], expect)
        r = self.trace("routed")
        self.assertEqual(r[0]["policy"], "affinity-first")
        self.assertFalse(r[0]["fallback"])

    def test_core_not_picked_unless_policy_names_it(self):
        self.core = CORE_ID
        self.write("task-1.txt", task())
        self.lead().sweep()
        self.assertIn(self.assigned()["task-1.txt"], self.pool)


class CoreFirst(Base):
    def test_unaddressed_goes_to_core(self):
        self.core = CORE_ID
        self.routing(policy="core-first")
        self.write("task-1.txt", task())
        self.lead().sweep()
        self.assertEqual(self.assigned()["task-1.txt"], CORE_ID)

    def test_addressed_goes_to_that_worker(self):
        self.core = CORE_ID
        self.routing(policy="core-first")
        self.write("task-1.txt", task(target_worker="core-2"))
        self.lead().sweep()
        self.assertEqual(self.assigned()["task-1.txt"], "core-2")

    def test_no_core_falls_to_least_loaded(self):
        self.routing(policy="core-first")
        self.write("task-1.txt", task())
        self.lead().sweep()
        self.assertIn(self.assigned()["task-1.txt"], self.pool)
        self.assertEqual(self.trace("routed")[0]["policy"], "core-first")


class Rules(Base):
    def test_first_match_wins_and_narrows(self):
        self.routing(policy="least-loaded", rules=[
            {"match": {"room_name": "Pro-Main"}, "to": ["core-1"]},
            {"match": {"access_tier": "team"}, "policy": "least-loaded",
             "exclude": ["core-1"]},
        ])
        self.write("task-a.txt", task(room_name="Pro-Main"))
        self.write("task-b.txt", task(access_tier="team"))
        self.lead().sweep()
        got = self.assigned()
        self.assertEqual(got["task-a.txt"], "core-1")
        self.assertEqual(got["task-b.txt"], "core-2")
        rules = {t["task"]: t["rule"] for t in self.trace("routed")}
        self.assertEqual(rules["task-a.txt"], 0)
        self.assertEqual(rules["task-b.txt"], 1)

    def test_rule_narrowed_to_nothing_falls_back(self):
        self.routing(rules=[{"match": {"source": "slack"}, "only": ["ghost"]}])
        self.write("task-1.txt", task())
        self.lead().sweep()
        self.assertIn(self.assigned()["task-1.txt"], self.pool)
        t = self.trace("routed")[0]
        self.assertTrue(t["fallback"])
        self.assertIn("no live worker", t["reason"])


class BrokenPolicyDegrades(Base):
    def test_custom_that_raises_falls_back(self):
        mod = Path(self.tmp.name) / "bad.py"
        mod.write_text("def pick(task, workers, affinity, state):\n    raise RuntimeError('boom')\n")
        self.routing(policy=f"custom:{mod}:pick")
        self.write("task-1.txt", task())
        self.lead().sweep()
        self.assertIn(self.assigned()["task-1.txt"], self.pool)
        t = self.trace("routed")[0]
        self.assertTrue(t["fallback"])
        self.assertIn("RuntimeError", t["reason"])

    def test_custom_naming_dead_worker_falls_back(self):
        mod = Path(self.tmp.name) / "dead.py"
        mod.write_text("def pick(task, workers, affinity, state):\n    return 'core-9'\n")
        self.routing(policy=f"custom:{mod}:pick")
        self.write("task-1.txt", task())
        self.lead().sweep()
        self.assertIn(self.assigned()["task-1.txt"], self.pool)
        self.assertTrue(self.trace("routed")[0]["fallback"])

    def test_custom_that_works_is_used(self):
        mod = Path(self.tmp.name) / "ok.py"
        mod.write_text("def pick(task, workers, affinity, state):\n    return 'core-2'\n")
        self.routing(policy=f"custom:{mod}:pick")
        self.write("task-1.txt", task())
        self.lead().sweep()
        self.assertEqual(self.assigned()["task-1.txt"], "core-2")
        self.assertFalse(self.trace("routed")[0]["fallback"])

    def test_unknown_policy_name_falls_back(self):
        self.routing(policy="no-such-policy")
        self.write("task-1.txt", task())
        self.lead().sweep()
        self.assertIn(self.assigned()["task-1.txt"], self.pool)
        self.assertIn("KeyError", self.trace("routed")[0]["reason"])

    def test_malformed_config_is_default(self):
        (self.state / "pool").mkdir()
        (self.state / "pool" / "routing.json").write_text("{not json")
        self.assertEqual(load_config(self.state).policy, "affinity-first")


class BuiltinsUnit(unittest.TestCase):
    def w(self, *ids, core=None):
        return [WorkerView(i, load=0, claiming=True) for i in ids] + (
            [WorkerView(core, is_core=True)] if core else [])

    def test_round_robin_cycles(self):
        r = Router(RoutingConfig(policy="round-robin"), lambda *a: None)
        picks = [r.pick(TaskMeta("t"), self.w("a", "b"), {}).worker for _ in range(4)]
        self.assertEqual(picks, ["a", "b", "a", "b"])

    def test_sticky_sender_keeps_worker_while_live(self):
        r = Router(RoutingConfig(policy="sticky-sender"), lambda *a: None)
        first = r.pick(TaskMeta("t", sender="U1"), self.w("a", "b"), {}).worker
        again = r.pick(TaskMeta("t", sender="U1"), self.w("a", "b"), {}).worker
        self.assertEqual(first, again)
        moved = r.pick(TaskMeta("t", sender="U1"), self.w("b"), {}).worker
        self.assertEqual(moved, "b")

    def test_least_loaded_prefers_lowest_load(self):
        r = Router(RoutingConfig(policy="least-loaded"), lambda *a: None)
        ws = [WorkerView("a", load=3), WorkerView("b", load=1)]
        self.assertEqual(r.pick(TaskMeta("t"), ws, {}).worker, "b")


class SingleCoreIsNEqualsOne(Base):
    """N=0 workers: the same policy over [core] — no solo code path."""

    def stale_lead(self):
        (self.state / "cores").mkdir(exist_ok=True)

    def test_default_policy_lets_the_lone_member_claim(self):
        self.stale_lead()
        self.write("task-1.txt", task())
        got = pool_follower.acquire_work(self.tasks, self.state, CORE_ID,
                                         "pool-lead", now_fn=lambda: 1e9)
        self.assertIsNotNone(got)
        self.assertTrue(got.name.endswith(f".claimed-{CORE_ID}.txt"))

    def test_rule_excluding_me_leaves_the_file(self):
        self.stale_lead()
        self.routing(policy="core-first", rules=[
            {"match": {"access_tier": "team"}, "exclude": [CORE_ID]}])
        self.write("task-team.txt", task(access_tier="team"))
        self.write("task-own.txt", task())
        got = pool_follower.acquire_work(self.tasks, self.state, CORE_ID,
                                         "pool-lead", now_fn=lambda: 1e9)
        self.assertEqual(got.name, f"task-own.claimed-{CORE_ID}.txt")
        self.assertTrue((self.tasks / "task-team.txt").exists())

    def test_solo_pick_is_the_same_router(self):
        router = build_router(self.state)
        self.assertTrue(solo_pick(router, TaskMeta("t"), CORE_ID))
        self.assertTrue(solo_pick(router, TaskMeta("t"), "core-1", is_core=False))


class Delegation(Base):
    def held(self):
        p = self.tasks / f"task-1.claimed-{CORE_ID}.txt"
        p.write_text(task())
        return p

    def test_disabled_by_default(self):
        with self.assertRaises(ValueError):
            pool_follower.delegate(self.tasks, self.state, self.held(), CORE_ID, "core-2")
        self.assertTrue((self.tasks / f"task-1.claimed-{CORE_ID}.txt").exists())

    def test_enabled_hands_off_as_assignment(self):
        self.routing(policy="core-first", allow_delegation=True)
        out = pool_follower.delegate(self.tasks, self.state, self.held(), CORE_ID, "core-2")
        self.assertEqual(out.name, "task-1.assigned-core-2.txt")
        self.assertEqual(self.assigned()["task-1.txt"], "core-2")

    def test_refuses_foreign_or_self_target(self):
        self.routing(allow_delegation=True)
        for bad in (CORE_ID, "", "../x"):
            with self.assertRaises(ValueError):
                pool_follower.delegate(self.tasks, self.state, self.held(), CORE_ID, bad)

    def test_only_a_file_i_hold(self):
        self.routing(allow_delegation=True)
        other = self.tasks / "task-2.claimed-core-1.txt"
        other.write_text(task())
        with self.assertRaises(ValueError):
            pool_follower.delegate(self.tasks, self.state, other, CORE_ID, "core-2")


if __name__ == "__main__":
    unittest.main(verbosity=1)
