#!/usr/bin/env python3
"""register_worker() is the only writer of task-event-handler.json -- a pool
that predates this file, or that nobody has re-registered into since an
upgrade, never gets it (re)declared, so its worker-bound tasks fall straight
to core instead of being routed. The regression starts with an existing
pool and requires no new worker registration, per keweichen's review on
PR #4503: ensure_task_event_handler backfills it, called every tick() so
the sweep self-heals on its own cadence, never core's.

Run: python3 tests/skills/worker-pool/pool-roster-handler-currency.test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import pool_supervise as sup  # noqa: E402

pr, wi = sup.pr, sup.wi
SOCK = "/recorded/app/run/tmux.sock"


def make_worker(ws, label="w"):
    wid = wi.new_worker_id()
    wi.create_worker(ws, runtime="claude", cwd=str(ws), host="h",
                     session_id="11111111-1111-4111-8111-111111111111",
                     tmux_socket=SOCK, worker_id=wid)
    pr.register_worker(ws, wid, label)
    return wid


def cfg_path(ws) -> Path:
    return ws / "state" / "task-event-handler.json"


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())


class EnsureTaskEventHandler(Base):
    def test_no_roster_is_a_noop(self):
        self.assertIsNone(pr.ensure_task_event_handler(self.ws))
        self.assertFalse(cfg_path(self.ws).exists())

    def test_no_live_workers_is_a_noop(self):
        make_worker(self.ws)
        roster = json.loads(pr.roster_path(self.ws).read_text())
        for w in roster["workers"].values():
            w["state"] = "retired"
        pr.roster_path(self.ws).write_text(json.dumps(roster))
        cfg_path(self.ws).unlink()
        self.assertIsNone(pr.ensure_task_event_handler(self.ws))
        self.assertFalse(cfg_path(self.ws).exists())

    def test_an_existing_pool_that_predates_the_file_is_backfilled(self):
        """register_worker() already wrote it once (this skill's normal path);
        delete it to simulate a pool registered before publish_task_event_handler
        existed at all -- the exact scenario the review names."""
        make_worker(self.ws)
        cfg_path(self.ws).unlink()
        self.assertFalse(cfg_path(self.ws).exists())

        result = pr.ensure_task_event_handler(self.ws)

        self.assertIsNotNone(result)
        self.assertTrue(cfg_path(self.ws).exists())
        cfg = json.loads(cfg_path(self.ws).read_text())
        self.assertEqual(cfg["handler"], str(Path(pr.__file__).resolve().parent / "pool_route_handler.py"))

    def test_a_stale_declaration_is_republished(self):
        make_worker(self.ws)
        cfg_path(self.ws).write_text(json.dumps({"handler": "/old/no-longer-real.py"}))
        pr.ensure_task_event_handler(self.ws)
        cfg = json.loads(cfg_path(self.ws).read_text())
        self.assertTrue(cfg["handler"].endswith("pool_route_handler.py"))

    def test_an_already_current_declaration_is_not_rewritten(self):
        make_worker(self.ws)
        before = cfg_path(self.ws).stat().st_mtime_ns
        result = pr.ensure_task_event_handler(self.ws)
        self.assertIsNotNone(result)
        self.assertEqual(cfg_path(self.ws).stat().st_mtime_ns, before,
                          "an already-current declaration must not be rewritten")


class TickBackfillsOnItsOwnSweep(Base):
    def test_a_sweep_tick_backfills_an_existing_pools_missing_declaration(self):
        """The full path a real cron sweep exercises: --sweep passes no
        worker_ids, exactly the periodic call this fix rides on."""
        make_worker(self.ws)
        cfg_path(self.ws).unlink()

        sup.tick(self.ws, 1000.0, worker_ids=None)

        self.assertTrue(cfg_path(self.ws).exists(),
                         "an existing pool's declaration was not backfilled by the sweep")

    def test_a_recipient_tick_also_backfills(self):
        """The delivery-time path (--recipient) shares the same call, not a
        second copy: a task arriving before the next sweep still self-heals."""
        wid = make_worker(self.ws)
        cfg_path(self.ws).unlink()

        sup.tick(self.ws, 1000.0, worker_ids=[wid])

        self.assertTrue(cfg_path(self.ws).exists())


class BootTimeSweepBackfillsBeforeDispatch(Base):
    """The exact command skills/startup/SKILL.md step 1.5 runs, synchronously,
    before the watcher starts -- an existing pool that upgraded without a new
    worker registration must not have a window where a bound task can reach
    core because the declaration hasn't been written yet. Reviewed by
    qingyun-wu on PR #4503: the sweep-timer-only backfill left exactly that
    gap open until the worker-pool skill's own five-minute sweep first fired."""

    def test_the_startup_command_backfills_an_existing_pool(self):
        make_worker(self.ws)
        cfg_path(self.ws).unlink()
        self.assertFalse(cfg_path(self.ws).exists())

        rc = sup.main(["--workspace", str(self.ws), "--sweep", "--no-persist"])

        self.assertEqual(rc, 0)
        self.assertTrue(cfg_path(self.ws).exists(),
                         "the boot-time sweep did not backfill the declaration "
                         "before a task could be dispatched")

    def test_no_persist_still_backfills_but_does_not_advance_the_ladder(self):
        make_worker(self.ws)
        cfg_path(self.ws).unlink()
        before = sup.state_path(self.ws).exists()

        sup.main(["--workspace", str(self.ws), "--sweep", "--no-persist"])

        self.assertTrue(cfg_path(self.ws).exists())
        self.assertEqual(sup.state_path(self.ws).exists(), before,
                          "--no-persist must not be what makes the backfill run "
                          "-- it must run regardless, only the ladder is skipped")

    def test_an_empty_workspace_with_no_roster_is_a_clean_noop(self):
        rc = sup.main(["--workspace", str(self.ws), "--sweep", "--no-persist"])
        self.assertEqual(rc, 0)
        self.assertFalse(cfg_path(self.ws).exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
