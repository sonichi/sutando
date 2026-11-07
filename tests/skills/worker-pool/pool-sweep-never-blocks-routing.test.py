#!/usr/bin/env python3
"""A sweep never holds a recipient's delivery lock across its scan, so routing is never blocked by it.

The sweep's spent-sentinel archiving (pool_delivery.archive_finished, reached from
pool_supervise.tick) re-checks each task under the folder lock the router delivers under. That
re-check walks tasks/ and its archive, so with a big inbox on a loaded host one locked pass ran
for half an hour and every route waited behind it. What is pinned here:

  * a route delivers into the folder while a sweep is mid-pass, within a short bound;
  * the pass stops at its deadline, reports what it deferred, and the next pass finishes it with
    the same outcome an unbounded pass has;
  * each task is re-checked under the lock: one re-queued between scan and move keeps its hold.

Run: python3 tests/skills/worker-pool/pool-sweep-never-blocks-routing.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))
sys.path.insert(0, str(REPO / "src"))

import pool_delivery as pd  # noqa: E402
import pool_roster as pr  # noqa: E402
import pool_router as rt  # noqa: E402
import pool_supervise as sup  # noqa: E402

W = "a3f91c2d4e5b6a7c8d9e0f1a2b3c4d5e"
SRC = "room:!abc:ag2.space"
# Each spent task's locked re-check is made to cost this long, as a loaded host's directory walks do.
SLOW_S = 0.02
SPENT = 300
ROUTE_BOUND_S = 2.0


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        for d in ("tasks/archive", "results/archive", "state"):
            (self.ws / d).mkdir(parents=True, exist_ok=True)
        self.inbox = pd.deliveries_dir(self.ws, W)
        self.inbox.mkdir(parents=True)

    def spent(self, n, prefix="task-spent"):
        ids = [f"{prefix}{i:04d}" for i in range(n)]
        for t in ids:
            (self.ws / "tasks/archive" / f"{t}.txt").write_text(f"id: {t}\ntask: x\n")
            (self.inbox / f"{t}.txt").write_bytes(b"")
        return ids

    def live_names(self):
        return sorted(n for n in os.listdir(self.inbox) if pd.parse_sentinel(n))

    def archived_names(self):
        a = self.inbox / pd.ARCHIVE_DIR
        return sorted(os.listdir(a)) if a.is_dir() else []


class TestRouteDuringSweep(Base):
    def test_a_route_delivers_while_the_sweep_is_mid_pass(self):
        ids = self.spent(SPENT)
        roster = pr.compile_roster(self.ws, {W: {"state": "live"}}, {SRC: W})
        (self.ws / "tasks" / "task-new.txt").write_text("task: body\n")
        real, calls, started = pd.sentinel_retirable, [], threading.Event()

        def slow(ws, recipient, task_id):
            calls.append(task_id)
            if len(calls) == 5:
                started.set()
            time.sleep(SLOW_S)
            return real(ws, recipient, task_id)

        sweep_out = {}
        with mock.patch.object(pd, "sentinel_retirable", slow):
            sweeper = threading.Thread(target=lambda: sweep_out.update(
                pd.archive_finished(self.ws, W, task_ids=ids)))
            sweeper.start()
            self.assertTrue(started.wait(10), "the sweep never reached its locked re-checks")
            routed = {}
            router = threading.Thread(target=lambda: routed.update(
                rt.route(self.ws, {"id": "task-new", "channel_id": SRC}, roster)))
            t0 = time.monotonic()
            router.start()
            router.join(ROUTE_BOUND_S)
            waited = time.monotonic() - t0
            mid_pass = sweeper.is_alive()
            self.assertFalse(router.is_alive(),
                             f"the route waited {waited:.1f}s behind the sweep's lock")
            sweeper.join(60)
        self.assertEqual(routed.get("delivered"), [W])
        self.assertTrue(mid_pass, "the route must land while the sweep is still running")
        self.assertEqual(sorted(sweep_out["archived"]), ids)
        self.assertEqual(self.live_names(), ["task-new.txt"])


class FakeClock:
    def __init__(self, step):
        self.now, self.step = 1000.0, step

    def monotonic(self):
        self.now += self.step
        return self.now


class TestBudget(Base):
    def test_the_pass_stops_at_its_deadline_and_the_next_one_finishes(self):
        ids = self.spent(200)
        self.spent(1, prefix="task-open")
        (self.ws / "tasks" / "task-open0000.txt").write_text("id: task-open0000\ntask: x\n")
        clock = FakeClock(step=1.0)
        with mock.patch.object(pd, "time", types.SimpleNamespace(monotonic=clock.monotonic, time=time.time)):
            first = pd.archive_finished(self.ws, W, deadline=clock.now + 20)
        self.assertGreater(len(first["archived"]), 0)
        self.assertLess(len(first["archived"]), len(ids))
        self.assertGreater(first["deferred"], 0)
        self.assertEqual(len(first["archived"]) + first["deferred"] + sum(first["kept"].values()), 201)
        self.assertEqual(len(self.live_names()), 201 - len(first["archived"]))
        second = pd.archive_finished(self.ws, W)
        self.assertEqual(second["deferred"], 0)
        self.assertEqual(sorted(first["archived"] + second["archived"]), ids)
        self.assertEqual(self.live_names(), ["task-open0000.txt"])
        self.assertEqual(second["kept"], {"pending": 1})

    def test_a_scan_cut_short_decides_nothing_it_did_not_read(self):
        ids = self.spent(200)
        clock = FakeClock(step=1.0)
        with mock.patch.object(pd, "time", types.SimpleNamespace(monotonic=clock.monotonic, time=time.time)):
            got = pd.archive_finished(self.ws, W, task_ids=ids, deadline=clock.now + 1.5)
        self.assertEqual(got["archived"], [])
        self.assertEqual(got["kept"], {})
        self.assertEqual(got["deferred"], 200, "one chunk scanned, then the deadline: every id is left")
        self.assertEqual(len(self.live_names()), 200)

    def test_without_a_deadline_nothing_is_deferred(self):
        ids = self.spent(150)
        got = pd.archive_finished(self.ws, W, task_ids=ids)
        self.assertEqual((sorted(got["archived"]), got["deferred"]), (ids, 0))

    def test_the_sweep_reports_what_it_left_and_retires_it_next_time(self):
        for t in self.spent(40):
            os.utime(self.inbox / f"{t}.txt", (1.0, 1.0))
        progress = {}
        moved = sup.retire_spent_sentinels(self.ws, W, time.time(), deadline=time.monotonic() - 1,
                                           progress=progress)
        self.assertEqual((moved, progress), ([], {"deferred": 40}))
        self.assertEqual(len(sup.retire_spent_sentinels(self.ws, W, time.time())), 40)
        self.assertEqual(self.live_names(), [])

    def test_the_budget_numbers_fit_inside_one_sweep_period(self):
        self.assertLessEqual(sup.SENTINEL_RETIRE_BUDGET_S, sup.SENTINEL_RETIRE_TOTAL_BUDGET_S)
        self.assertLess(sup.SENTINEL_RETIRE_TOTAL_BUDGET_S, sup.ps.SAMPLE_PERIOD_S / 2)


class TestRecheckUnderTheLock(Base):
    def test_a_task_re_queued_after_the_scan_keeps_its_hold(self):
        ids = self.spent(3)
        real, entered = pd.arbitration, []

        def requeue_second(ws, recipient, **kw):
            entered.append(1)
            if len(entered) == 2:  # between the scan and this entry's move
                (self.ws / "tasks" / f"{ids[1]}.txt").write_text(f"id: {ids[1]}\ntask: x\n")
            return real(ws, recipient, **kw)

        with mock.patch.object(pd, "arbitration", requeue_second):
            got = pd.archive_finished(self.ws, W, task_ids=ids)
        self.assertEqual(got["archived"], [ids[0], ids[2]])
        self.assertEqual(got["kept"], {"re-queued": 1})
        self.assertEqual(self.live_names(), [f"{ids[1]}.txt"])

    def test_one_lock_per_entry_never_one_across_the_pass(self):
        ids = self.spent(5)
        real, depth, peak, taken = pd.arbitration, [0], [0], []

        class Counted:
            def __init__(self, cm):
                self.cm = cm

            def __enter__(self):
                depth[0] += 1
                peak[0] = max(peak[0], depth[0])
                taken.append(1)
                return self.cm.__enter__()

            def __exit__(self, *exc):
                depth[0] -= 1
                return self.cm.__exit__(*exc)

        with mock.patch.object(pd, "arbitration", lambda ws, r, **kw: Counted(real(ws, r, **kw))):
            pd.archive_finished(self.ws, W, task_ids=ids)
        self.assertEqual((len(taken), peak[0]), (5, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
