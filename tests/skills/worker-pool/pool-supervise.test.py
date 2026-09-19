#!/usr/bin/env python3
"""The supervision caller: it observes the pool truthfully, and it never remedies.

`pool_supervision` decides; this module is the I/O around it, so what is under
test is that each observation says what is actually on disk and in tmux, that
the ladder's state survives between ticks, and that nothing here ever acts.

Run: python3 tests/skills/worker-pool/pool-supervise.test.py
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import pool_supervise as sup  # noqa: E402

ps, pb, pr, wi = sup.ps, sup.pb, sup.pr, sup.wi
SOCK = "/recorded/app/run/tmux.sock"


class Tmux:
    """Answers `has-session` from a set of live session names; records argv."""

    def __init__(self, live=(), stderr_absent="can't find session: x", raises=None,
                 odd=None):
        self.live, self.calls = set(live), []
        self.stderr_absent, self.raises, self.odd = stderr_absent, raises, odd

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.raises:
            raise self.raises
        if self.odd is not None:
            return subprocess.CompletedProcess(argv, *self.odd)
        name = argv[-1].lstrip("=")
        if name in self.live:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", self.stderr_absent)


def make_worker(ws, label="w", socket=SOCK):
    wid = wi.new_worker_id()
    wi.create_worker(ws, runtime="claude", cwd=str(ws), host="h",
                     session_id="11111111-1111-4111-8111-111111111111",
                     tmux_socket=socket, worker_id=wid)
    pr.register_worker(ws, wid, label)
    return wid


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())


class TheSessionProbe(Base):
    def test_it_asks_the_RECORDED_socket_for_the_exact_session(self):
        wid = make_worker(self.ws)
        t = Tmux(live={wi.tmux_session_name(wid)})
        self.assertIs(sup.probe_session(self.ws, wid, runner=t), True)
        argv = t.calls[-1]
        self.assertEqual(argv[:3], ["tmux", "-S", SOCK],
                         "probed a socket other than the one the run recorded; a wrong "
                         "socket answers 'no server' for a host whose real one is fine")
        self.assertEqual(argv[-1], "=" + wi.tmux_session_name(wid),
                         "a bare name prefix-matches another worker's session")

    def test_a_missing_session_is_gone(self):
        wid = make_worker(self.ws)
        self.assertIs(sup.probe_session(self.ws, wid, runner=Tmux()), False)

    def test_no_tmux_server_is_gone(self):
        wid = make_worker(self.ws)
        t = Tmux(stderr_absent="no server running on /recorded/app/run/tmux.sock")
        self.assertIs(sup.probe_session(self.ws, wid, runner=t), False)

    def test_an_unexpected_tmux_failure_is_UNKNOWN_not_death(self):
        wid = make_worker(self.ws)
        t = Tmux(odd=(1, "", "error connecting: permission denied"))
        self.assertIsNone(sup.probe_session(self.ws, wid, runner=t))

    def test_tmux_that_cannot_run_is_unknown(self):
        wid = make_worker(self.ws)
        self.assertIsNone(sup.probe_session(self.ws, wid, runner=Tmux(raises=OSError("no tmux"))))

    def test_a_worker_with_no_open_run_is_unknown(self):
        wid = make_worker(self.ws)
        for run in wi.incarnations(self.ws, wid):
            wi.end_incarnation(self.ws, wid, run["incarnation_id"], "exited")
        t = Tmux(live={wi.tmux_session_name(wid)})
        self.assertIsNone(sup.probe_session(self.ws, wid, runner=t))
        self.assertEqual(t.calls, [], "probed tmux for a worker that has no open run")

    def test_an_open_run_with_no_recorded_socket_is_unknown(self):
        wid = make_worker(self.ws, socket="")
        self.assertIsNone(sup.probe_session(self.ws, wid, runner=Tmux()))


class WhoIsSupervised(Base):
    def test_no_roster_means_nobody_not_an_invented_pool(self):
        self.assertEqual(sup.supervised_workers(self.ws), {})

    def test_the_core_and_retired_workers_are_not_supervised(self):
        keep, gone = make_worker(self.ws, "keep"), make_worker(self.ws, "gone")
        roster = json.loads(pr.roster_path(self.ws).read_text())
        roster["workers"][gone]["state"] = "retired"
        roster["workers"][pr.CORE] = {"state": "live", "label": "core"}
        roster["workers"]["junk"] = "not-a-row"
        pr.roster_path(self.ws).write_text(json.dumps(roster))
        self.assertEqual(sorted(sup.supervised_workers(self.ws)), [keep])


class Observing(Base):
    def test_an_observation_reports_beat_session_and_pause(self):
        wid = make_worker(self.ws)
        pb.touch(pb.beat_path(self.ws, "worker", wid))
        obs = sup.observe(self.ws, __import__("time").time(),
                          runner=Tmux(live={wi.tmux_session_name(wid)}))
        self.assertEqual(obs[wid], ps.Observation(beat=pb.LIVE, session_alive=True, paused=False))

    def test_the_owners_marker_is_what_pauses_a_worker(self):
        wid = make_worker(self.ws)
        (wi.worker_dir(self.ws, wid) / sup.PAUSED_MARKER).touch()
        self.assertTrue(sup.observe(self.ws, 100.0, runner=Tmux())[wid].paused)

    def test_a_recompiled_roster_does_not_lift_a_pause(self):
        wid = make_worker(self.ws)
        (wi.worker_dir(self.ws, wid) / sup.PAUSED_MARKER).touch()
        make_worker(self.ws, "another")          # register_worker recompiles the roster
        self.assertTrue(sup.is_paused(self.ws, wid))

    def test_a_delivery_time_check_observes_only_its_recipient(self):
        a, _b = make_worker(self.ws, "a"), make_worker(self.ws, "b")
        self.assertEqual(list(sup.observe(self.ws, 100.0, worker_ids=[a], runner=Tmux())), [a])


class TheLadderSurvivesBetweenTicks(Base):
    def test_state_round_trips(self):
        st = ps.SupervisionState(last_sample_at=5.0, workers={
            "w": ps.WorkerEvidence(first_detected_at=1.0, consecutive=2,
                                   recover_issued_at=4.0, escalated=True)})
        sup.save_state(self.ws, st)
        self.assertEqual(sup.load_state(self.ws), st)

    def test_a_missing_corrupt_or_wrong_shaped_file_is_a_fresh_ladder(self):
        self.assertEqual(sup.load_state(self.ws), ps.SupervisionState())
        sup.state_path(self.ws).parent.mkdir(parents=True, exist_ok=True)
        for junk in ("{not json", "[1, 2]", json.dumps({"workers": {"w": "nope"}})):
            sup.state_path(self.ws).write_text(junk)
            self.assertEqual(sup.load_state(self.ws), ps.SupervisionState(), junk)

    def test_a_failed_write_leaves_no_temp_file_and_no_half_state(self):
        sup.save_state(self.ws, ps.SupervisionState(last_sample_at=1.0))
        real = sup.os.replace
        sup.os.replace = lambda *_a: (_ for _ in ()).throw(OSError("disk full"))
        try:
            with self.assertRaises(OSError):
                sup.save_state(self.ws, ps.SupervisionState(last_sample_at=2.0))
        finally:
            sup.os.replace = real
        self.assertEqual(sup.load_state(self.ws).last_sample_at, 1.0)
        self.assertEqual([f.name for f in sup.state_path(self.ws).parent.iterdir()
                          if f.name.startswith(".pool-supervision.")], [])

    def test_sustained_death_across_persisted_ticks_reaches_recover(self):
        wid = make_worker(self.ws)
        seen = [sup.tick(self.ws, t, runner=Tmux())["decisions"][wid]
                for t in (1000.0, 1030.0, 1060.0, 1095.0)]
        self.assertEqual(seen, [ps.NOTHING, ps.NOTHING, ps.NOTHING, ps.RECOVER],
                         "each tick is a separate process in production: the ladder "
                         "only advances if the state really persisted between them")

    def test_no_persist_decides_without_advancing_the_ladder(self):
        wid = make_worker(self.ws)
        for t in (1000.0, 1030.0, 1060.0, 1095.0):
            out = sup.tick(self.ws, t, runner=Tmux(), persist=False)
        self.assertEqual(out["decisions"][wid], ps.NOTHING)
        self.assertFalse(sup.state_path(self.ws).exists())

    def test_a_fast_check_right_after_a_sweep_is_not_a_resume(self):
        wid = make_worker(self.ws)
        sup.tick(self.ws, 1000.0, runner=Tmux())
        out = sup.tick(self.ws, 1300.0, worker_ids=[wid], runner=Tmux())
        self.assertFalse(out["resumed"])
        self.assertEqual(sup.load_state(self.ws).workers[wid].consecutive, 2,
                         "both samplers share last_sample_at, so a gap the SWEEP explains "
                         "must not discard a delivery-time check's evidence")

    def test_a_host_sleep_is_reported_and_recovers_nobody(self):
        wid = make_worker(self.ws)
        sup.tick(self.ws, 1000.0, runner=Tmux())
        out = sup.tick(self.ws, 1000.0 + 7200.0, runner=Tmux())
        self.assertTrue(out["resumed"])
        self.assertEqual(out["decisions"][wid], ps.NOTHING)


class ItNeverRemedies(Base):
    def test_a_recover_decision_runs_nothing_but_the_probe(self):
        wid = make_worker(self.ws)
        t = Tmux()
        for now in (1000.0, 1030.0, 1060.0, 1095.0):
            out = sup.tick(self.ws, now, runner=t)
        self.assertEqual(out["decisions"][wid], ps.RECOVER)
        self.assertEqual({a[3] for a in t.calls}, {"has-session"},
                         "the caller ran something other than a read-only probe")
        self.assertEqual(len(wi.incarnations(self.ws, wid)), 1, "it started a run")


class TheCommandLine(Base):
    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = sup.main(["--workspace", str(self.ws), *argv])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_exactly_one_mode_is_required(self):
        self.assertEqual(self._run()[0], 2)
        self.assertEqual(self._run("--sweep", "--recipient", "x")[0], 2)

    def test_a_sweep_prints_one_line_per_worker(self):
        wid = make_worker(self.ws)
        rc, out, _ = self._run("--sweep", "--no-persist")
        self.assertEqual(rc, 0)
        self.assertIn(wid[:8], out)
        self.assertIn("beat=absent", out)

    def test_json_carries_decisions_and_observations(self):
        wid = make_worker(self.ws)
        rc, out, _ = self._run("--recipient", wid, "--json", "--no-persist")
        self.assertEqual(rc, 0)
        self.assertEqual(set(json.loads(out)), {"decisions", "observations", "resumed"})

    def test_a_resume_sample_says_so(self):
        make_worker(self.ws)
        sup.save_state(self.ws, ps.SupervisionState(last_sample_at=1.0))
        _, out, _ = self._run("--sweep", "--no-persist")
        self.assertIn("discarded as evidence", out)

    def test_a_malformed_worker_id_is_refused_not_a_traceback(self):
        rc, _, err = self._run("--recipient", "../../etc", "--no-persist")
        self.assertEqual(rc, 2)
        self.assertIn("refused", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
