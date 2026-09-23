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
from unittest.mock import patch
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
        # No watcher beat was written and the tmux stub answers no holder scan,
        # so the watcher reads absent and its holder stays unknown.
        self.assertEqual(obs[wid], ps.Observation(beat=pb.LIVE, session_alive=True, paused=False,
                                                  watcher_beat=pb.ABSENT, watcher_held=None))

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


class AnIneligibleRecipientIsNotObserved(Base):
    """Naming a worker must not smuggle it past the filter the sweep applies: once
    something remedies, a ladder entry for a RETIRED worker is a recovery attempt
    on one the roster deliberately retired."""

    def _retire(self, wid):
        roster = json.loads(pr.roster_path(self.ws).read_text())
        roster["workers"][wid]["state"] = "retired"
        pr.roster_path(self.ws).write_text(json.dumps(roster))

    def test_a_retired_recipient_is_not_observed(self):
        gone = make_worker(self.ws, "gone")
        self._retire(gone)
        self.assertEqual(sup.observe(self.ws, 100.0, worker_ids=[gone], runner=Tmux()), {})

    def test_an_unknown_recipient_is_not_observed(self):
        make_worker(self.ws, "real")
        never = wi.new_worker_id()
        self.assertEqual(sup.observe(self.ws, 100.0, worker_ids=[never], runner=Tmux()), {})

    def test_neither_reaches_the_persisted_ladder(self):
        gone, never = make_worker(self.ws, "gone"), wi.new_worker_id()
        self._retire(gone)
        for now in (1000.0, 1030.0, 1060.0, 1095.0):
            out = sup.tick(self.ws, now, worker_ids=[gone, never], runner=Tmux())
        self.assertEqual(out["decisions"], {})
        self.assertEqual(sup.load_state(self.ws).workers, {},
                         "a retired or unknown worker acquired a ladder entry")
        self.assertEqual(sorted(out["not_supervised"]), sorted([gone, never]))

    def test_the_operator_is_told_rather_than_shown_nothing(self):
        gone = make_worker(self.ws, "gone")
        self._retire(gone)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sup.main(["--workspace", str(self.ws), "--recipient", gone, "--no-persist"])
        self.assertEqual(rc, 0)
        self.assertIn("not supervised", out.getvalue())

    def test_control_a_live_recipient_is_still_observed(self):
        live = make_worker(self.ws, "live")
        self.assertEqual(list(sup.observe(self.ws, 100.0, worker_ids=[live], runner=Tmux())), [live])


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


class IsThisHostRouting(Base):
    """Bindings say tasks must reach workers; only the handler's receipt says the
    watcher ever consults it. The gap between the two is a host whose core answers
    for its workers, silently."""

    def _bind(self):
        wid = make_worker(self.ws)
        pr.bind_room(self.ws, "!room:ag2.space", wid)
        return wid

    def _archived_task(self, mtime, task_id="task-abc"):
        d = self.ws / "tasks" / "archive"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{task_id}.txt"
        f.write_text(f"id: {task_id}\ntask: x\n")
        import os
        os.utime(f, (mtime, mtime))
        return f

    def test_no_bindings_is_never_an_alarm(self):
        make_worker(self.ws)
        self.assertIsNone(sup.routing_status(self.ws)["alarm"])

    def test_a_room_bound_to_the_core_is_not_a_worker_binding(self):
        # An explicit {room: "core"} pin means the core answers there; nothing is
        # owed to a worker, so a never-consulted handler is not an alarm.
        make_worker(self.ws)
        pr.bind_room(self.ws, "!mine:ag2.space", pr.CORE)
        self._archived_task(1000.0)
        st = sup.routing_status(self.ws)
        self.assertEqual(st["bound_rooms"], [])
        self.assertIsNone(st["alarm"])

    def test_the_consulted_tasks_own_archive_is_not_the_alarm(self):
        # Real causality: the handler stamps the receipt BEFORE routing task-abc, and
        # task-abc is archived after it was processed — strictly later than its consult.
        self._bind()
        sup.prr.record(self.ws, mode="run", task_id="task-abc", now=1000.0)
        self._archived_task(1002.0)
        st = sup.routing_status(self.ws)
        self.assertEqual(st["newest_task_id"], "task-abc")
        self.assertIsNone(st["alarm"], "a task the handler routed must not read as unrouted")

    def test_a_different_task_archived_after_the_consult_is_still_the_alarm(self):
        self._bind()
        sup.prr.record(self.ws, mode="run", task_id="task-abc", now=1000.0)
        self._archived_task(1002.0)
        self._archived_task(1003.0, task_id="task-xyz")
        alarm = sup.routing_status(self.ws)["alarm"] or ""
        self.assertIn("task-xyz", alarm)
        self.assertIn("processed it unrouted", alarm)

    def test_a_different_task_tied_with_the_consulted_one_on_mtime_is_still_the_alarm(self):
        # Same clock tick for both archives is routine on coarse filesystems; the
        # consulted task's exemption must not shadow the other task, whatever the glob order.
        self._bind()
        sup.prr.record(self.ws, mode="run", task_id="task-abc", now=1000.0)
        for tid in ("task-abc", "task-xyz", "task-aaa"):
            self._archived_task(1002.0, task_id=tid)
        alarm = sup.routing_status(self.ws)["alarm"] or ""
        self.assertIn("processed it unrouted", alarm)
        self.assertNotIn("task-abc", alarm, "the exempt task must not be the one named")

    def test_an_unrouted_task_older_than_the_newest_routed_one_is_still_the_alarm(self):
        # task-xyz got past the handler, then the handler was consulted for task-abc and
        # task-abc archived last: the newest archive is exempt, task-xyz is not.
        self._bind()
        self._archived_task(999.0, task_id="task-old")          # before the consult: routed
        sup.prr.record(self.ws, mode="run", task_id="task-abc", now=1000.0)
        self._archived_task(1001.0, task_id="task-xyz")
        self._archived_task(1002.0)                            # task-abc, the exempt one
        alarm = sup.routing_status(self.ws)["alarm"] or ""
        self.assertIn("task-xyz", alarm)

    def test_bound_rooms_but_a_handler_never_consulted_is_the_alarm(self):
        self._bind()
        self._archived_task(1000.0)
        st = sup.routing_status(self.ws)
        self.assertEqual(st["bound_rooms"], ["!room:ag2.space"])
        self.assertIsNone(st["handler_consulted_at"])
        self.assertIn("never been consulted", st["alarm"] or "")

    def test_a_task_that_arrived_after_the_last_consult_is_the_alarm(self):
        self._bind()
        sup.prr.record(self.ws, mode="probe", task_id="task-old", now=1000.0)
        self._archived_task(2000.0)
        self.assertIn("processed it unrouted", sup.routing_status(self.ws)["alarm"] or "")

    def test_a_consult_newer_than_every_task_is_quiet(self):
        self._bind()
        self._archived_task(1000.0)
        sup.prr.record(self.ws, mode="run", task_id="task-abc", now=1001.0)
        st = sup.routing_status(self.ws)
        self.assertIsNone(st["alarm"])
        self.assertEqual(st["handler_consulted_at"], 1001.0)

    def test_a_corrupt_receipt_is_no_evidence(self):
        self._bind()
        sup.prr.receipt_path(self.ws).parent.mkdir(parents=True, exist_ok=True)
        sup.prr.receipt_path(self.ws).write_text("{not json")
        self.assertIn("never been consulted", sup.routing_status(self.ws)["alarm"] or "")

    def test_a_well_formed_receipt_of_the_wrong_shape_is_no_evidence_either(self):
        # The shape a half-written or schema-drifted receipt actually has: it parses,
        # but `consulted_at` is not a number (or the document is not an object).
        self._bind()
        path = sup.prr.receipt_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        for bad in ('{"consulted_at": "07:20"}', "[]", '{"mode": "probe"}'):
            path.write_text(bad)
            self.assertIsNone(sup.prr.read(self.ws), bad)
            self.assertIn("never been consulted", sup.routing_status(self.ws)["alarm"] or "", bad)

    def test_an_archived_task_that_cannot_be_stated_is_skipped_not_fatal(self):
        self._bind()
        self._archived_task(1000.0)
        sup.prr.record(self.ws, mode="run", task_id="task-abc", now=2000.0)
        real = Path.stat

        def flaky(self_, *a, **k):
            if self_.name == "task-abc.txt":
                raise OSError(5, "Input/output error")
            return real(self_, *a, **k)
        with patch.object(Path, "stat", flaky):
            st = sup.routing_status(self.ws)
        self.assertIsNone(st["newest_task_at"], "an unreadable task must not become a timestamp")
        self.assertIsNone(st["alarm"])

    def test_the_sweep_prints_the_alarm_and_carries_it_in_json(self):
        self._bind()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            sup.main(["--workspace", str(self.ws), "--sweep", "--no-persist"])
        self.assertIn("unrouted:", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sup.main(["--workspace", str(self.ws), "--sweep", "--no-persist", "--json"])
        self.assertIn("never been consulted", json.loads(out.getvalue())["routing"]["alarm"])


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
        self.assertEqual(set(json.loads(out)),
                         {"decisions", "observations", "resumed", "not_supervised", "routing"})

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
