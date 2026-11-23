#!/usr/bin/env python3
"""A delivery sentinel carries a lease, and every reader still reads the sentinel as held.

The router writes `{"delivered_at", "ttl_s"}` into the sentinel it creates (and into one it
renews). `pool_delivery.lease_verdict` judges it, `lease_mode` and `lease_ttl_s` read the
worker-pool manifest's config (env first), and `pool_holder.pool_delegation` is what
`connections.task_reply_agent` asks. A lease-filled sentinel must still name its holder to
every existence reader: pool_holder, the Stop hook's `task_delegation.py holder --deliveries`,
`task_dispatch.worker_holds`, scripts/unanswered-tasks.py and the task-orphan-check classifier.

Run: python3 tests/pool-delivery-lease.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))

import pool_delivery as pd
import pool_holder as ph
import pool_router as rt
from delivery import task_dispatch

A, B = "a" * 32, "b" * 32
HUGE = "1" + "0" * 400
# Each would raise or read as current if parsed naively; all must be unreadable.
MALFORMED_LEASES = (
    '{"delivered_at": Infinity, "ttl_s": 3600}', '{"delivered_at": 1000000, "ttl_s": Infinity}',
    '{"delivered_at": NaN, "ttl_s": 3600}', '{"delivered_at": 1000000, "ttl_s": NaN}',
    '{"delivered_at": 1e400, "ttl_s": 3600}', '{"delivered_at": 1000000, "ttl_s": 1e400}',
    '{"delivered_at": %s, "ttl_s": 3600}' % HUGE, '{"delivered_at": 1000000, "ttl_s": %s}' % HUGE,
    '{"delivered_at": 1000000, "ttl_s": -5}', '{"delivered_at": -1, "ttl_s": 3600}',
    '{"delivered_at": "1000000", "ttl_s": 3600}', '{"delivered_at": 1000000, "ttl_s": true}',
    '{"delivered_at": null, "ttl_s": 3600}', '{"delivered_at": [1], "ttl_s": 3600}', '[1, 2]', '7',
    '{"delivered_at": 1e308, "ttl_s": 1e308}',
)
ROOM = "!bound:example.test"
TID = "task-r"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name) / "ws"
        for d in ("tasks", "state", "deliveries", "results"):
            (self.ws / d).mkdir(parents=True)
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {A: {"state": "live"}, B: {"state": "live"}}, "bindings": {ROOM: A}}))
        (self.ws / "tasks" / f"{TID}.txt").write_text(
            f"id: {TID}\nchannel_id: {ROOM}\nsource: ag2space\naccess_tier: owner\ntask: body\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("WORKER_POOL_DELIVERY_")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sentinel(self, suffix=pd.PENDING_SUFFIX):
        return self.ws / "deliveries" / A / f"{TID}{suffix}"

    def lease(self, path=None):
        return json.loads((path or self.sentinel()).read_text())


class TheRouterWritesALease(Case):
    def test_a_new_delivery_carries_the_configured_ttl(self):
        before = time.time()
        self.assertEqual(rt.deliver_one(self.ws, A, TID), "delivered")
        lease = self.lease()
        self.assertEqual(lease["ttl_s"], pd.DEFAULT_LEASE_TTL_S)
        self.assertTrue(before - 1 <= lease["delivered_at"] <= time.time() + 1, lease)
        self.assertEqual(pd.lease_verdict(self.sentinel(), time.time())[0], pd.LEASE_LIVE)

    def test_the_env_overrides_the_manifest_ttl(self):
        with mock.patch.dict(os.environ, {pd.LEASE_TTL_KEY: "60"}):
            rt.deliver_one(self.ws, A, TID)
        self.assertEqual(self.lease()["ttl_s"], 60)

    def test_a_renewed_sentinel_gets_a_fresh_lease(self):
        rt.deliver_one(self.ws, A, TID)
        self.sentinel().write_bytes(pd.lease_body(1_000.0, 60))
        self.assertEqual(rt.deliver_one(self.ws, A, TID, renotify=True), "already")
        self.assertGreater(self.lease()["delivered_at"], 1_000.0)

    def test_accepting_keeps_the_lease(self):
        rt.deliver_one(self.ws, A, TID)
        lease = self.lease()
        accepted = pd.accept(self.sentinel())
        self.assertEqual(self.lease(accepted), lease)

    def test_a_lease_that_cannot_be_written_still_delivers(self):
        with mock.patch.object(rt.os, "write", side_effect=OSError(28, "no space")), \
                mock.patch("sys.stderr"):
            self.assertEqual(rt.deliver_one(self.ws, A, TID), "delivered")
        self.assertEqual(self.sentinel().read_bytes(), b"")
        self.assertEqual(ph.pool_holder(self.ws, TID), A)


class TheVerdict(Case):
    def verdict(self, body, now=1_000_100.0):
        p = self.ws / "s"
        p.write_bytes(body if isinstance(body, bytes) else body.encode())
        return pd.lease_verdict(p, now)[0]

    def test_each_shape(self):
        at = 1_000_000.0
        self.assertEqual(self.verdict(pd.lease_body(at, 3600)), pd.LEASE_LIVE)
        self.assertEqual(self.verdict(pd.lease_body(at, 3600), now=at + 3599.9), pd.LEASE_LIVE)
        self.assertEqual(self.verdict(pd.lease_body(at, 3600), now=at + 3600), pd.LEASE_EXPIRED)
        self.assertEqual(self.verdict(b""), pd.LEASE_LEGACY)
        self.assertEqual(self.verdict(b"\n"), pd.LEASE_LEGACY)
        for bad in ("{", "[]", '{"delivered_at": 1}', '{"delivered_at": "x", "ttl_s": 1}',
                    '{"delivered_at": 1, "ttl_s": 0}', '{"delivered_at": NaN, "ttl_s": 1}',
                    json.dumps({"delivered_at": at, "ttl_s": 1, "pad": "x" * 5000}), b"\xff\xfe"):
            self.assertEqual(self.verdict(bad), pd.LEASE_UNREADABLE, bad)
        self.assertEqual(self.verdict(pd.lease_body(at + 3600, 3600), now=at), pd.LEASE_UNREADABLE)

    def test_no_unrepresentable_or_mistyped_lease_raises_or_reads_as_current(self):
        for body in MALFORMED_LEASES:
            with self.subTest(body=body[:60]):
                self.assertEqual(self.verdict(body), pd.LEASE_UNREADABLE)

    def test_a_non_finite_number_is_named_as_such(self):
        for body in MALFORMED_LEASES[:6]:
            with self.subTest(body=body):
                p = self.ws / "s"
                p.write_text(body)
                verdict, detail = pd.lease_verdict(p, 1_000_100.0)
                self.assertEqual((verdict, "not finite" in detail), (pd.LEASE_UNREADABLE, True), detail)

    def test_a_missing_file_is_unreadable_not_live(self):
        self.assertEqual(pd.lease_verdict(self.ws / "absent", 0.0)[0], pd.LEASE_UNREADABLE)


class TheSwitch(Case):
    def test_the_mode_is_warn_unless_set_to_enforce(self):
        self.assertEqual(pd.lease_mode(), "warn")
        self.assertEqual(pd.lease_mode({pd.LEASE_MODE_KEY: "enforce"}), "enforce")
        self.assertEqual(pd.lease_mode({pd.LEASE_MODE_KEY: "ENFORCE"}), "enforce")
        self.assertEqual(pd.lease_mode({pd.LEASE_MODE_KEY: "strict"}), "warn")

    def test_the_manifest_declares_both_defaults(self):
        cfg = json.loads((SCRIPTS.parent / "manifest.json").read_text())["config"]
        self.assertEqual(cfg[pd.LEASE_MODE_KEY], "warn")
        self.assertEqual(int(cfg[pd.LEASE_TTL_KEY]), pd.DEFAULT_LEASE_TTL_S)

    def test_a_bad_ttl_is_the_default(self):
        for v in ("x", "0", "-5"):
            self.assertEqual(pd.lease_ttl_s({pd.LEASE_TTL_KEY: v}), pd.DEFAULT_LEASE_TTL_S, v)

    def test_only_enforce_refuses_and_only_what_is_not_live(self):
        for verdict in (pd.LEASE_LIVE, pd.LEASE_EXPIRED, pd.LEASE_LEGACY, pd.LEASE_UNREADABLE):
            self.assertTrue(pd.lease_allows(verdict, "warn"), verdict)
            self.assertEqual(pd.lease_allows(verdict, "enforce"), verdict == pd.LEASE_LIVE, verdict)


class PoolDelegation(Case):
    def test_held_by_this_worker_with_a_live_lease(self):
        rt.deliver_one(self.ws, A, TID)
        got = ph.pool_delegation(self.ws, TID, A)
        self.assertEqual((got["held"], got["verdict"], got["mode"], got["allowed"]), (True, "live", "warn", True))

    def test_expired_under_enforce_is_held_but_not_allowed(self):
        rt.deliver_one(self.ws, A, TID)
        with mock.patch.dict(os.environ, {pd.LEASE_MODE_KEY: "enforce"}):
            got = ph.pool_delegation(self.ws, TID, A, now=time.time() + pd.DEFAULT_LEASE_TTL_S + 1)
        self.assertEqual((got["held"], got["verdict"], got["allowed"]), (True, "expired", False))

    def test_an_accept_between_the_lookup_and_the_read_still_reads_the_lease(self):
        import worker_delivery
        rt.deliver_one(self.ws, A, TID)
        real = worker_delivery.sentinel_of
        calls = []

        def lookup_then_accept(ws, tid):
            found = real(ws, tid)
            if not calls:
                pd.accept(found)
            calls.append(found)
            return found
        with mock.patch.object(worker_delivery, "sentinel_of", lookup_then_accept):
            got = ph.pool_delegation(self.ws, TID, A)
        self.assertEqual(len(calls), 2, "the renamed sentinel was looked up again")
        self.assertEqual((got["held"], got["verdict"], got["allowed"]), (True, "live", True))
        self.assertTrue(self.sentinel(pd.ACCEPTED_SUFFIX).exists())

    def test_a_sentinel_withdrawn_between_the_lookup_and_the_read_is_not_held(self):
        import worker_delivery
        rt.deliver_one(self.ws, A, TID)
        real = worker_delivery.sentinel_of

        def lookup_then_withdraw(ws, tid):
            found = real(ws, tid)
            if found is not None:
                found.unlink()
            return found
        with mock.patch.object(worker_delivery, "sentinel_of", lookup_then_withdraw):
            self.assertFalse(ph.pool_delegation(self.ws, TID, A)["held"])

    def test_another_worker_or_no_pool_is_not_held(self):
        rt.deliver_one(self.ws, A, TID)
        self.assertFalse(ph.pool_delegation(self.ws, TID, B)["held"])
        self.assertFalse(ph.pool_delegation(self.ws, "task-none", A)["held"])
        self.assertFalse(ph.pool_delegation(self.ws, TID, A, pool_scripts=self.ws / "no-skill")["held"])


class EveryReaderStillSeesItHeld(Case):
    """Each existing reader keys on the sentinel's name; a lease inside it changes none of them."""

    def setUp(self):
        super().setUp()
        rt.deliver_one(self.ws, A, TID)
        self.assertTrue(self.lease()["ttl_s"] > 0)

    def each_stage(self):
        yield "pending"
        pd.accept(self.sentinel())
        self.assertTrue(self.sentinel(pd.ACCEPTED_SUFFIX).read_bytes())
        yield "accepted"

    def test_pool_holder(self):
        for stage in self.each_stage():
            self.assertEqual(ph.pool_holder(self.ws, TID), A, stage)

    def test_the_stop_hooks_holder_command(self):
        for stage in self.each_stage():
            p = subprocess.run([sys.executable, str(REPO / "src" / "task_delegation.py"), "holder", TID,
                                "--deliveries", str(self.ws / "deliveries"), "--workspace", str(self.ws)],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, (stage, p.stderr))

    def test_task_dispatch_worker_holds(self):
        for stage in self.each_stage():
            self.assertTrue(task_dispatch.worker_holds(self.ws / "deliveries", f"{TID}.txt"), stage)

    def test_unanswered_tasks(self):
        ut = _load("unanswered_tasks", REPO / "scripts" / "unanswered-tasks.py")
        for stage in self.each_stage():
            self.assertEqual(ut._holder(self.ws, TID), A, stage)

    def test_the_orphan_classifier(self):
        cl = _load("orphan_classify", REPO / "skills" / "task-orphan-check" / "scripts" / "classify.py")
        for stage in self.each_stage():
            self.assertEqual(cl._holder_of(self.ws, TID), A, stage)


if __name__ == "__main__":
    unittest.main()
