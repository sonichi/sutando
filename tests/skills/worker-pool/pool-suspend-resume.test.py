#!/usr/bin/env python3
"""The pool can be suspended by the app that owns it, and resumed on its next start.

`pool_remedy --suspend <reason>` writes `state/pool-suspended`, recording which workers the
stop takes down (those not already in a death episode); while it exists a sweep observes
only (no recover, rearm, card or supervisor start, the ladder does not advance, escalations
are still reported), and `apply()` re-reads it before every action so a quit that lands
mid-sweep wins. `--resume` lifts it and recovers, outside the death ladder, only the workers
the stop took down; a worker already dead or escalated keeps its ladder. Without a marker,
--resume is a sweep.

Run: python3 tests/skills/worker-pool/pool-suspend-resume.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


rem = _load("pool_remedy")
ps = rem.ps
DEAD, ALIVE, PAUSED, ESCALATED = "a" * 32, "b" * 32, "c" * 32, "e" * 32


def obs(session_alive, paused=False):
    return ps.Observation(beat=ps.STALE if session_alive is False else ps.LIVE,
                          session_alive=session_alive, paused=paused)


def tick_result(decisions=None):
    return {"decisions": decisions or {}, "observations": {}, "wedged": [], "auth_expired": []}


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "ws"
        (self.ws / "state").mkdir(parents=True)

    def roster(self, *ids):
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"workers": {w: {"runtime": "claude", "state": "live"} for w in ids}}))

    def tearDown(self):
        self.td.cleanup()

    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = rem.main(["--workspace", str(self.ws), "--repo", str(REPO), *args])
        return rc, json.loads(out.getvalue())


class Marker(Base):
    def test_suspend_records_reason_time_and_the_workers_the_stop_takes_down(self):
        self.assertIsNone(rem.suspension(self.ws))
        self.roster(DEAD, ALIVE, ESCALATED)
        rem.sup.save_state(self.ws, ps.SupervisionState(last_sample_at=1.0, workers={
            ESCALATED: ps.WorkerEvidence(consecutive=9, escalated=True)}))
        self.assertEqual(rem.suspend(self.ws, "app-quit", now=1790630000), "app-quit 1790630000")
        rec = json.loads((self.ws / "state" / "pool-suspended").read_text())
        self.assertEqual(rec, {"reason": "app-quit", "at": 1790630000, "stopped": sorted([DEAD, ALIVE])})
        self.assertEqual(rem.suspension(self.ws), "app-quit 1790630000")

    def test_an_unreadable_pool_still_suspends_and_names_no_workers(self):
        with mock.patch.object(rem.sup, "load_state", side_effect=ValueError("corrupt ladder")):
            self.assertTrue(rem.suspend(self.ws, "app-quit", now=3).startswith("app-quit"))
        rec = json.loads((self.ws / "state" / "pool-suspended").read_text())
        self.assertEqual(rec["stopped"], [])

    def test_a_bad_worker_id_on_resume_exits_2_not_a_traceback(self):
        rem.suspend(self.ws, "app-quit", now=3)
        with mock.patch.object(rem, "resume", side_effect=rem.wi.IdentityError("bad id")), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            rc = rem.main(["--workspace", str(self.ws), "--repo", str(REPO), "--resume"])
        self.assertEqual(rc, 2)
        self.assertIn("refused", err.getvalue())

    def test_a_marker_that_is_not_a_record_still_suspends_and_names_no_workers(self):
        (self.ws / "state" / "pool-suspended").write_text("app-quit 5\n")
        self.assertEqual(rem.suspension(self.ws), "app-quit 5")
        with mock.patch.object(rem.sup, "observe", return_value={DEAD: obs(False)}), \
                mock.patch.object(rem, "recover") as rec:
            self.assertEqual(rem.resume(self.ws, REPO)["restarted"], {})
        rec.assert_not_called()

    def test_the_cli_suspends_and_exits_zero(self):
        rc, out = self.run_main("--suspend", "app-quit")
        self.assertEqual(rc, 0)
        self.assertTrue(out["suspended"].startswith("app-quit "))

    def test_exactly_one_mode(self):
        for args in ([], ["--sweep", "--resume"], ["--suspend", "x", "--sweep"]):
            with self.subTest(args=args), self.assertRaises(SystemExit), \
                    mock.patch("sys.stderr", new_callable=io.StringIO):
                rem.main(["--workspace", str(self.ws), "--repo", str(REPO), *args])


class Suspended(Base):
    def test_apply_takes_no_action_while_suspended_but_still_reports_escalations(self):
        rem.suspend(self.ws, "app-quit")
        with mock.patch.object(rem, "recover") as rec, mock.patch.object(rem, "ensure_supervisor") as sup, \
                mock.patch.object(rem.wc, "raise_card") as card:
            out = rem.apply(self.ws, REPO, {DEAD: ps.RECOVER, ALIVE: ps.REARM_WATCHER,
                                            PAUSED: ps.CARD_CAUSE, "d" * 32: ps.ESCALATE})
        rec.assert_not_called(), sup.assert_not_called(), card.assert_not_called()
        self.assertEqual(out["recoveries"][DEAD]["outcome"], rem.SUSPENDED)
        self.assertEqual(out["escalations"], ["d" * 32])

    def test_a_quit_landing_mid_sweep_stops_the_remaining_actions(self):
        calls = []

        def recover(ws, repo, wid, **kw):
            calls.append(wid)
            rem.suspend(ws, "app-quit")
            return {"worker_id": wid, "outcome": rem.RECOVERED}
        with mock.patch.object(rem, "recover", side_effect=recover):
            out = rem.apply(self.ws, REPO, {DEAD: ps.RECOVER, ALIVE: ps.RECOVER})
        self.assertEqual(calls, [DEAD])
        self.assertEqual(out["recoveries"][ALIVE]["outcome"], rem.SUSPENDED)

    def test_a_suspended_sweep_observes_without_advancing_the_ladder_or_starting_anything(self):
        rem.suspend(self.ws, "app-quit", now=1)
        with mock.patch.object(rem.sup, "tick", return_value=tick_result({DEAD: ps.RECOVER})) as tick, \
                mock.patch.object(rem, "apply") as apply, \
                mock.patch.object(rem, "ensure_supervisors") as ens:
            rc, out = self.run_main("--sweep")
        self.assertEqual(rc, 0)
        self.assertFalse(tick.call_args.kwargs["persist"])
        apply.assert_not_called(), ens.assert_not_called()
        self.assertEqual(out["suspended"], "app-quit 1")
        self.assertEqual(out["decisions"], {DEAD: ps.RECOVER})

    def test_a_suspended_sweep_still_reports_escalations(self):
        rem.suspend(self.ws, "app-quit", now=1)
        with mock.patch.object(rem.sup, "tick", return_value=tick_result({ESCALATED: ps.ESCALATE})), \
                mock.patch.object(rem, "apply") as apply:
            rc, out = self.run_main("--sweep")
        apply.assert_not_called()
        self.assertEqual(out["escalations"], [ESCALATED])


class Resume(Base):
    def test_resume_without_a_marker_is_just_a_sweep(self):
        with mock.patch.object(rem.sup, "observe") as observe, mock.patch.object(rem, "recover") as rec:
            self.assertEqual(rem.resume(self.ws, REPO), {"was_suspended": None, "restarted": {}})
        observe.assert_not_called(), rec.assert_not_called()

    def test_resume_restarts_only_what_the_stop_took_down_and_keeps_an_escalation(self):
        self.roster(DEAD, ALIVE, PAUSED, ESCALATED)
        (self.ws / "state" / "workers" / PAUSED).mkdir(parents=True)
        (self.ws / "state" / "workers" / PAUSED / "paused").write_text("")
        escalated = ps.WorkerEvidence(consecutive=9, escalated=True, recover_issued_at=5.0)
        rem.sup.save_state(self.ws, ps.SupervisionState(last_sample_at=1.0, workers={ESCALATED: escalated}))
        rem.suspend(self.ws, "app-quit")
        with mock.patch.object(rem.sup, "observe", return_value={
                    DEAD: obs(False), ALIVE: obs(True), PAUSED: obs(False, paused=True),
                    ESCALATED: obs(False)}), \
                mock.patch.object(rem, "recover", side_effect=lambda ws, repo, w, **kw: {
                    "worker_id": w, "outcome": rem.RECOVERED}) as rec:
            out = rem.resume(self.ws, REPO)
        self.assertIsNone(rem.suspension(self.ws))
        self.assertEqual([c.args[2] for c in rec.call_args_list], [DEAD])
        self.assertEqual(out["was_suspended"].split()[0], "app-quit")
        self.assertEqual(list(out["restarted"]), [DEAD])
        self.assertEqual(rem.sup.load_state(self.ws).workers[ESCALATED], escalated)

    def test_the_cli_resumes_then_sweeps_and_a_failed_restart_exits_nonzero(self):
        self.roster(DEAD)
        rem.suspend(self.ws, "app-quit")
        with mock.patch.object(rem.sup, "observe", return_value={DEAD: obs(False)}), \
                mock.patch.object(rem, "recover", return_value={"worker_id": DEAD, "outcome": rem.FAILED}), \
                mock.patch.object(rem.sup, "tick", return_value=tick_result()) as tick, \
                mock.patch.object(rem, "ensure_supervisors", return_value={}) as ens, \
                mock.patch.object(rem, "ensure_input_watches", return_value={}), \
                mock.patch.object(rem.wc, "drive_escapes", return_value={}), \
                mock.patch.object(rem.wc, "resolve_cleared", return_value=[]):
            rc, out = self.run_main("--resume")
        self.assertEqual(rc, 1)
        self.assertTrue(tick.call_args.kwargs["persist"])
        ens.assert_called_once()
        self.assertEqual(out["resume"]["restarted"][DEAD]["outcome"], rem.FAILED)


if __name__ == "__main__":
    sys.exit(unittest.main())
