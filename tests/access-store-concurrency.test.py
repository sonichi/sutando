#!/usr/bin/env python3
"""Concurrency regression for src/access_store.py — the single locked writer
every access.json mutator shares (#3318, qingyun-wu review).

Before this PR, tier-map seeding (`ensure_tier_map_seeded`), thread-engage
seeding, and pairing-code issuance each did their own unlocked
read-json / mutate-dict / write-json in `discord-bridge.py`. Two of those
racing — e.g. an owner approving via `/discord:access` while a new thread's
first message seeds its group — is a classic lost update: both readers see
the same on-disk snapshot, both write back a snapshot missing the other's
change, and whichever write lands second silently erases the first.

This test calls `access_store.mutate_access_file` directly — the actual
production writer, not a copied recipe of its locking — with two real OS
threads racing a tier-map-shaped mutation against a thread-group-shaped
mutation, and asserts BOTH survive. Each mutator sleeps mid-transaction to
force the threads' critical sections to overlap in time; if `mutate_access_file`
did not serialize them via `_locked`, the two would race and one mutation
would be lost. `fcntl.flock` locks are scoped to the *open file description*,
not the process, so two threads in one process each doing their own
`os.open()` (which `_locked` always does) are genuinely exclusded from each
other — this is real mutual exclusion, not merely the GIL serializing Python
bytecode.

Run: python3 tests/access-store-concurrency.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from access_store import mutate_access_file  # noqa: E402


def _owner_tier_mutator(data):
    """Mirrors ensure_tier_map_seeded()'s _mutator: grandfather every
    existing allowFrom member into tierMap, once."""
    time.sleep(0.05)  # widen the critical section to force thread overlap
    if "tierMap" in data:
        return None, True
    data["tierMap"] = {uid: "owner" for uid in data.get("allowFrom", [])}
    return data, True


def _thread_group_mutator(data):
    """Mirrors the thread-engage seed mutator: add one new thread group."""
    time.sleep(0.05)
    groups = data.setdefault("groups", {})
    groups["thread-1"] = {"requireMention": False, "allowFrom": ["sender-x"]}
    return data, True


class TestConcurrentOwnerAndThreadWritesBothSurvive(unittest.TestCase):
    def test_racing_tier_seed_and_thread_seed_both_persist(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            p.write_text(json.dumps({
                "dmPolicy": "pairing", "allowFrom": ["owner-1"], "pending": {},
            }))

            results = {}

            def _run_tier():
                results["tier"] = mutate_access_file(p, _owner_tier_mutator)

            def _run_thread():
                results["thread"] = mutate_access_file(p, _thread_group_mutator)

            t1 = threading.Thread(target=_run_tier)
            t2 = threading.Thread(target=_run_thread)
            # Start together so both threads' critical sections genuinely
            # overlap in time (each sleeps 50ms mid-transaction).
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

            self.assertFalse(t1.is_alive(), "tier-seed thread did not finish — deadlock?")
            self.assertFalse(t2.is_alive(), "thread-seed thread did not finish — deadlock?")
            self.assertTrue(results.get("tier"), "tier-map mutation reported failure")
            self.assertTrue(results.get("thread"), "thread-group mutation reported failure")

            final = json.loads(p.read_text())
            self.assertEqual(
                final.get("tierMap"), {"owner-1": "owner"},
                "the tier-map update was lost — a concurrent thread-group write clobbered it",
            )
            self.assertEqual(
                final.get("groups", {}).get("thread-1"),
                {"requireMention": False, "allowFrom": ["sender-x"]},
                "the thread-group seed was lost — a concurrent tier-map write clobbered it",
            )
            # Nothing from the original doc should have been dropped either.
            self.assertEqual(final.get("allowFrom"), ["owner-1"])
            self.assertEqual(final.get("dmPolicy"), "pairing")

    def test_many_concurrent_distinct_thread_seeds_all_persist(self):
        """Wider fan-out: N threads each seeding a DIFFERENT thread group
        concurrently must all land — no lost updates under real contention,
        not just a two-way race."""
        N = 8
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            p.write_text(json.dumps({"dmPolicy": "pairing", "allowFrom": [], "pending": {}}))

            def _make_mutator(i):
                def _mutator(data):
                    time.sleep(0.02)
                    groups = data.setdefault("groups", {})
                    groups[f"thread-{i}"] = {"requireMention": False}
                    return data, True
                return _mutator

            threads = []
            outcomes = [None] * N
            for i in range(N):
                def _run(i=i):
                    outcomes[i] = mutate_access_file(p, _make_mutator(i))
                th = threading.Thread(target=_run)
                threads.append(th)
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=10)

            self.assertTrue(all(outcomes), f"some mutations reported failure: {outcomes}")
            final = json.loads(p.read_text())
            for i in range(N):
                self.assertIn(
                    f"thread-{i}", final.get("groups", {}),
                    f"thread-{i}'s seed was lost under concurrent contention",
                )


if __name__ == "__main__":
    _r = unittest.main(exit=False)
    try:
        import coverage

        _cov = coverage.Coverage.current()
        if _cov is not None:
            _cov.save()
    except Exception:
        pass
    sys.exit(0 if _r.result.wasSuccessful() else 1)
