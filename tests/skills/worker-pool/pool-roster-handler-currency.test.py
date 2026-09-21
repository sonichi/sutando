#!/usr/bin/env python3
"""register_worker() is the only writer of task-event-handler.json -- a pool
that predates this file, or that nobody has re-registered into since an
upgrade, never gets it (re)declared, so its worker-bound tasks fall straight
to core instead of being routed. The regression starts with an existing
pool and requires no new worker registration: ensure_task_event_handler
backfills it, called every tick() so the sweep self-heals on its own
cadence, never core's.

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

    def test_an_empty_workers_dict_is_a_noop(self):
        """A roster file can exist (bindings compiled it) with zero workers --
        that is the real no-pool-yet case, distinct from a worker present but
        not currently live."""
        pr.roster_path(self.ws).parent.mkdir(parents=True, exist_ok=True)
        pr.roster_path(self.ws).write_text(json.dumps({"workers": {}}))
        self.assertIsNone(pr.ensure_task_event_handler(self.ws))
        self.assertFalse(cfg_path(self.ws).exists())

    def test_liveness_is_not_the_gate_recovering_abandoned_also_backfill(self):
        """The router never reads `state` (docs/worker-pool-design.md,
        pool_route_handler.py) -- a recovering or abandoned worker's
        deliveries still route to it, so ensure_task_event_handler must
        publish for them too, not only for a currently-live one."""
        for state in ("recovering", "abandoned"):
            with self.subTest(state=state):
                ws = Path(tempfile.mkdtemp())
                make_worker(ws)
                roster = json.loads(pr.roster_path(ws).read_text())
                for w in roster["workers"].values():
                    w["state"] = state
                pr.roster_path(ws).write_text(json.dumps(roster))
                cfg_path(ws).unlink()

                result = pr.ensure_task_event_handler(ws)

                self.assertIsNotNone(result, f"state={state} was not backfilled")
                self.assertTrue(cfg_path(ws).exists())

    def test_an_unreadable_roster_raises_rather_than_reading_as_no_pool(self):
        """Absent (no roster file) and UNREADABLE (a roster file that exists
        but load_roster refuses) are different failures -- collapsing the
        latter into the former hides an existing pool's ownership."""
        pr.roster_path(self.ws).parent.mkdir(parents=True, exist_ok=True)
        pr.roster_path(self.ws).write_text("not valid json {{{")

        with self.assertRaises(pr.HandlerPublishError):
            pr.ensure_task_event_handler(self.ws)

    def test_a_malformed_roster_missing_workers_key_also_raises(self):
        pr.roster_path(self.ws).parent.mkdir(parents=True, exist_ok=True)
        pr.roster_path(self.ws).write_text(json.dumps({"not_workers": {}}))

        with self.assertRaises(pr.HandlerPublishError):
            pr.ensure_task_event_handler(self.ws)

    def test_a_falsey_non_dict_workers_value_raises_instead_of_reading_as_no_pool(self):
        """`workers = roster.get("workers") or {}` let a falsey-but-invalid
        shape (an empty list) collapse into "no pool" without ever reaching
        validation. Exact repro: roster.json with a workers list."""
        pr.roster_path(self.ws).parent.mkdir(parents=True, exist_ok=True)
        for workers_val in ([], None, "bogus", ["w1"], 0, ""):
            with self.subTest(workers=workers_val):
                pr.roster_path(self.ws).write_text(json.dumps(
                    {"workers": workers_val, "bindings": {"room-a": "worker-a"}}))
                with self.assertRaises(pr.HandlerPublishError):
                    pr.ensure_task_event_handler(self.ws)

    def test_a_worker_row_missing_state_also_raises(self):
        pr.roster_path(self.ws).parent.mkdir(parents=True, exist_ok=True)
        pr.roster_path(self.ws).write_text(json.dumps(
            {"workers": {"w1": {"label": "w1"}}, "bindings": {}}))

        with self.assertRaises(pr.HandlerPublishError):
            pr.ensure_task_event_handler(self.ws)

    def test_a_dangling_binding_raises_with_a_valid_live_worker(self):
        """A valid live worker-a plus a binding to a nonexistent worker-b.
        validate_workers alone passes; without validate_bindings this
        published a handler anyway, and the router's own DECLINE for the
        dangling binding then fell through to the unrestricted core."""
        pr.roster_path(self.ws).parent.mkdir(parents=True, exist_ok=True)
        pr.roster_path(self.ws).write_text(json.dumps(
            {"workers": {"worker-a": {"state": "live"}},
             "bindings": {"room-a": "worker-b"}}))

        with self.assertRaises(pr.HandlerPublishError):
            pr.ensure_task_event_handler(self.ws)

    def test_a_dangling_binding_raises_even_with_empty_workers(self):
        """An empty workers dict does NOT make a dangling binding harmless
        -- it's still a corrupt roster, not the ordinary no-pool case."""
        pr.roster_path(self.ws).parent.mkdir(parents=True, exist_ok=True)
        pr.roster_path(self.ws).write_text(json.dumps(
            {"workers": {}, "bindings": {"room-a": "worker-b"}}))

        with self.assertRaises(pr.HandlerPublishError):
            pr.ensure_task_event_handler(self.ws)

    def test_a_valid_binding_to_an_existing_worker_still_publishes(self):
        """Negative control for the two tests above: a binding that DOES
        resolve to a real worker must not be caught by the new check."""
        pr.roster_path(self.ws).parent.mkdir(parents=True, exist_ok=True)
        pr.roster_path(self.ws).write_text(json.dumps(
            {"workers": {"worker-a": {"state": "live"}},
             "bindings": {"room-a": "worker-a"}}))

        self.assertIsNotNone(pr.ensure_task_event_handler(self.ws))
        self.assertTrue(cfg_path(self.ws).exists())

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
    core because the declaration hasn't been written yet. A sweep-timer-only
    backfill left exactly that gap open until the pool's own periodic sweep
    first fired."""

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


class FailClosedOnBackfillFailure(Base):
    """A live pool whose declaration cannot be published is a routing outage
    in the making, not the ordinary no-pool case: distinguish the benign
    no-skill/no-roster case from a failed required backfill, and fail closed
    instead of starting the watcher when backfill for an existing pool
    cannot complete."""

    def _break_state_dir(self):
        state = self.ws / "state"
        state.mkdir(parents=True, exist_ok=True)
        state.chmod(0o500)  # read+execute, no write

    def tearDown(self):
        (self.ws / "state").chmod(0o700)  # so tempfile cleanup can remove it
        super().tearDown()

    def test_tick_surfaces_the_error_instead_of_raising(self):
        make_worker(self.ws)
        cfg_path(self.ws).unlink()
        self._break_state_dir()

        out = sup.tick(self.ws, 1000.0, worker_ids=None, persist=False)

        self.assertIsNotNone(out["handler_backfill_error"])
        self.assertIn("task-event-handler.json", out["handler_backfill_error"])

    def test_a_successful_backfill_reports_no_error(self):
        make_worker(self.ws)
        cfg_path(self.ws).unlink()

        out = sup.tick(self.ws, 1000.0, worker_ids=None, persist=False)

        self.assertIsNone(out["handler_backfill_error"])
        self.assertTrue(cfg_path(self.ws).exists())

    def test_main_returns_a_distinct_code_and_does_not_crash(self):
        make_worker(self.ws)
        cfg_path(self.ws).unlink()
        self._break_state_dir()

        rc = sup.main(["--workspace", str(self.ws), "--sweep", "--no-persist"])

        self.assertEqual(rc, 3, "a failed backfill on an existing pool must fail "
                                 "closed with its own code, not the ordinary 0")

    def test_main_still_returns_0_when_there_is_no_pool_to_backfill(self):
        self._break_state_dir()

        rc = sup.main(["--workspace", str(self.ws), "--sweep", "--no-persist"])

        self.assertEqual(rc, 0, "an unwritable state dir with no live worker is "
                                 "not a backfill failure -- there is nothing to publish")

    def test_an_unreadable_roster_also_fails_closed_with_the_distinct_code(self):
        """A roster file that exists but cannot be read is an existing pool
        whose ownership can't be established -- the same failure class as a
        write error, not the benign no-pool case. A corrupt/unreadable
        roster must not leave sweep_rc==0 and config_written==no, silently."""
        pr.roster_path(self.ws).parent.mkdir(parents=True, exist_ok=True)
        pr.roster_path(self.ws).write_text("not valid json {{{")

        rc = sup.main(["--workspace", str(self.ws), "--sweep", "--no-persist"])

        self.assertEqual(rc, 3, "an unreadable roster on an existing pool must "
                                 "fail closed, not read as 'no pool'")

    def test_a_malformed_workers_type_fails_closed_end_to_end_not_a_crash(self):
        """ensure_task_event_handler() rejected workers="bogus" correctly,
        but tick() then continued into observe()/supervised_workers(), which
        raised a raw AttributeError (exit 1) rather than the intended
        handled exit 3 -- end to end through main(), not just the backfill
        call in isolation."""
        pr.roster_path(self.ws).parent.mkdir(parents=True, exist_ok=True)
        pr.roster_path(self.ws).write_text(json.dumps({"workers": "bogus", "bindings": {}}))

        rc = sup.main(["--workspace", str(self.ws), "--sweep", "--no-persist"])

        self.assertEqual(rc, 3, "a malformed workers type must fail closed "
                                 "through the whole sweep, not crash with exit 1")

    def test_main_still_returns_0_on_an_ordinary_successful_backfill(self):
        make_worker(self.ws)
        cfg_path(self.ws).unlink()

        rc = sup.main(["--workspace", str(self.ws), "--sweep", "--no-persist"])

        self.assertEqual(rc, 0)
        self.assertTrue(cfg_path(self.ws).exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
