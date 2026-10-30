#!/usr/bin/env python3
"""`pool_manage.py`: the agent can stop, retire, re-enable and remove a worker, and the
pool honours it.

A worker whose tmux session is merely killed is respawned by the next sweep on its own
session id; that is the defect. `stop` pauses it first, so recover, sweep, `--resume`
and `--restart` all leave it down; `retire` also takes it out of supervision, the
advertisement and routing (traffic addressed to it goes to the core); `enable` lifts
both and restarts it; `remove` drops a retired row. Each prints one JSON line.

Run: python3 tests/skills/worker-pool/pool-manage.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


pm = _load("pool_manage")
rem, pr, wi, sup = pm.rem, pm.pr, pm.wi, pm.sup
pdm = rem.pd
import pool_advertise as pa  # noqa: E402
import pool_attribution as pattr  # noqa: E402

import pool_route_handler as handler  # noqa: E402
import pool_router as router  # noqa: E402

sys.path.insert(0, str(REPO / "src"))
from delivery import task_dispatch as td  # noqa: E402

WID, OTHER = "a" * 32, "b" * 32
SEAT = f"sutando-worker-{WID}"


class FakeTmux:
    """has-session / kill-session against a set of live session names."""

    def __init__(self, alive=()):
        self.alive, self.calls = set(alive), []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:1] == ["tmux"] and "has-session" in argv:
            name = argv[argv.index("-t") + 1].lstrip("=")
            if name in self.alive:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 1, "", f"can't find session: {name}\n")
        if argv[:1] == ["tmux"] and "kill-session" in argv:
            self.alive.discard(argv[argv.index("-t") + 1].lstrip("="))
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def killed(self):
        return [c[c.index("-t") + 1] for c in self.calls if "kill-session" in c]


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.ws = Path(td.name) / "ws"
        (self.ws / "state").mkdir(parents=True)
        self.sock = str(Path(td.name) / "tmux.sock")
        for wid, label in ((WID, "pool worker 1"), (OTHER, "other")):
            wi.create_worker(self.ws, runtime="claude", tmux_socket=self.sock, worker_id=wid,
                             cwd=str(REPO))
            pr.register_worker(self.ws, wid, label, runtime="claude")
        self.tmux = FakeTmux({SEAT, SEAT + "-watcher", SEAT + "-input",
                              f"sutando-worker-{OTHER}"})
        self.spawned = []

    def spawn(self, workspace, repo, **kw):
        self.spawned.append(kw)
        return {"runtime_session_id": kw.get("resume")}

    def main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = pm.main(["--workspace", str(self.ws), "--repo", str(REPO), *args],
                         runner=self.tmux)
        lines = out.getvalue().strip().splitlines()
        return rc, (json.loads(lines[-1]) if lines and lines[-1].startswith(("{", "[")) else lines)

    def row(self, wid=WID):
        return (pr.load_roster(self.ws)["workers"]).get(wid)


class KillAloneIsUndone(Base):
    def test_a_killed_session_is_resumed_by_the_remedy_but_a_stopped_one_is_not(self):
        self.tmux.alive.discard(SEAT)  # what killing the tmux session by hand leaves
        out = rem.recover(self.ws, REPO, WID, runner=self.tmux, spawn=self.spawn)
        self.assertEqual(out["outcome"], rem.RECOVERED)
        self.assertEqual(len(self.spawned), 1)

        rc, out = self.main("stop", WID)
        self.assertEqual((rc, out["result"]), (0, "stopped"))
        self.assertEqual(rem.recover(self.ws, REPO, WID, runner=self.tmux,
                                     spawn=self.spawn)["outcome"], rem.PAUSED)
        self.assertEqual(rem.restart(self.ws, REPO, WID, runner=self.tmux,
                                     spawn=self.spawn)["result"], "paused")
        self.assertEqual(len(self.spawned), 1)


class Stop(Base):
    def test_stop_pauses_kills_seat_and_companions_and_closes_the_run(self):
        rc, out = self.main("stop", "pool worker 1")
        self.assertEqual(rc, 0)
        self.assertEqual(out["worker_id"], WID)
        self.assertTrue(sup.is_paused(self.ws, WID))
        self.assertEqual(self.tmux.killed(), [f"={SEAT}-watcher", f"={SEAT}-input", f"={SEAT}"])
        self.assertTrue(all(c[1:3] == ["-S", self.sock] for c in self.tmux.calls))
        self.assertEqual(out["sessions"], {SEAT + "-watcher": "killed", SEAT + "-input": "killed",
                                           SEAT: "killed"})
        runs = wi.incarnations(self.ws, WID)
        self.assertEqual([r["end_reason"] for r in runs], ["killed"])
        self.assertIn(f"sutando-worker-{OTHER}", self.tmux.alive)
        self.assertEqual(self.row()["state"], "live")

    def test_a_sweep_after_stop_neither_recovers_nor_re_arms_it(self):
        self.main("stop", WID)
        obs = sup.observe(self.ws, time.time(), runner=self.tmux)
        self.assertTrue(obs[WID].paused)
        state = rem.ps.SupervisionState(last_sample_at=time.time() - 300)
        _, decisions = rem.ps.evaluate(state, obs, time.time(),
                                       expected_period_s=rem.ps.SAMPLE_PERIOD_S)
        self.assertEqual(decisions[WID], rem.ps.NOTHING)

    def test_a_seat_tmux_cannot_confirm_gone_fails_and_keeps_its_run_open(self):
        def stuck(argv, **kw):
            if "kill-session" in argv:
                return subprocess.CompletedProcess(argv, 1, "", "server exited unexpectedly")
            return subprocess.CompletedProcess(argv, 1, "", "server exited unexpectedly")
        self.tmux = stuck
        rc, out = self.main("stop", WID)
        self.assertEqual((rc, out["result"]), (1, "failed"))
        self.assertTrue(sup.is_paused(self.ws, WID))
        self.assertIsNone(wi.incarnations(self.ws, WID)[-1]["ended_at"])

    def test_unknown_and_core_targets_are_refused(self):
        for target in ("ghost", "core"):
            rc, out = self.main("stop", target)
            self.assertEqual((rc, out["result"]), (2, "refused"))
        self.assertEqual(self.tmux.killed(), [])


class Retire(Base):
    def test_retire_leaves_supervision_advertisement_and_routing(self):
        rc, out = self.main("retire", WID)
        self.assertEqual((rc, out["result"], out["previous_state"]), (0, "retired", "live"))
        self.assertEqual(self.row()["state"], "retired")
        self.assertNotIn(WID, sup.supervised_workers(self.ws))
        self.assertNotIn(WID, pa.profile_workers(pr.load_roster(self.ws)))
        roster = pr.load_roster(self.ws)
        self.assertEqual(pr.unknown_targets(roster, [WID]), [WID])
        self.assertEqual(pr.targets_for(roster, "", "pool worker 1"), [WID])
        (self.ws / "tasks").mkdir()
        (self.ws / "tasks" / "task-1.txt").write_text("id: task-1\ntask: hi\n")
        routed = router.route(self.ws, {"id": "task-1", "requested_worker": "pool worker 1"})
        self.assertEqual((routed["targets"], routed["redirected"]), ([pr.CORE], [WID]))
        self.assertFalse((self.ws / "deliveries" / WID / "task-1.txt").exists())

    def test_a_bound_worker_is_refused_unless_unbind_is_passed(self):
        pr.bind_room(self.ws, "!room:example", WID)
        rc, out = self.main("retire", WID)
        self.assertEqual((rc, out["result"]), (2, "refused"))
        self.assertIn("!room:example", out["why"])
        self.assertEqual(self.row()["state"], "live")
        self.assertEqual(self.tmux.killed(), [])
        rc, out = self.main("retire", WID, "--unbind")
        self.assertEqual((rc, out["unbound"]), (0, ["!room:example"]))
        self.assertEqual(pr.load_bindings(self.ws), {})


class Enable(Base):
    def test_enable_lifts_retire_and_pause_then_restarts_on_the_same_session(self):
        self.main("retire", WID)
        with mock.patch.object(rem, "ensure_supervisor", return_value={"outcome": rem.SUPERVISED}), \
                mock.patch.object(rem, "ensure_input_watch", return_value={"outcome": rem.WATCHING}):
            out = pm.enable(self.ws, REPO, WID, runner=self.tmux, spawn=self.spawn)
        self.assertEqual((out["result"], out["previous_state"]), ("enabled", "retired"))
        self.assertEqual(out["start"]["result"], "restarted")
        self.assertEqual(self.row()["state"], "live")
        self.assertFalse(sup.is_paused(self.ws, WID))
        self.assertEqual(self.spawned[-1]["resume"], wi.sessions(self.ws, WID)[-1]["session_id"])

    def test_no_start_only_lifts(self):
        self.main("stop", WID)
        rc, out = self.main("enable", WID, "--no-start")
        self.assertEqual((rc, out["result"]), (0, "enabled"))
        self.assertNotIn("start", out)
        self.assertFalse(sup.is_paused(self.ws, WID))
        self.assertEqual(self.spawned, [])


class Remove(Base):
    def test_remove_refuses_a_live_worker_and_drops_a_retired_one(self):
        rc, out = self.main("remove", WID)
        self.assertEqual((rc, out["result"]), (2, "refused"))
        self.assertIsNotNone(self.row())
        self.main("retire", WID)
        rc, out = self.main("remove", WID)
        self.assertEqual((rc, out["result"]), (0, "removed"))
        self.assertIsNone(self.row())
        self.assertIsNotNone(self.row(OTHER))
        pr.validate_current_roster(self.ws)
        self.assertTrue(wi.worker_dir(self.ws, WID).is_dir())


class Listing(Base):
    def test_list_reports_state_pause_and_session(self):
        self.main("stop", OTHER)
        rc, rows = self.main("list", "--json")
        self.assertEqual(rc, 0)
        by = {r["worker_id"]: r for r in rows}
        self.assertEqual((by[WID]["paused"], by[WID]["session_alive"]), (False, True))
        self.assertEqual((by[OTHER]["paused"], by[OTHER]["session_alive"]), (True, False))
        self.assertEqual(by[WID]["name"], "pool worker 1")


class RosterWriters(Base):
    def test_set_worker_state_validates_and_is_a_no_op_when_unchanged(self):
        with self.assertRaises(pr.RosterError):
            pr.set_worker_state(self.ws, WID, "asleep")
        out = pr.set_worker_state(self.ws, "pool worker 1", "live")
        self.assertEqual(out, (WID, "live", None, []))

    def test_remove_worker_refuses_a_bound_retired_worker(self):
        pr.set_worker_state(self.ws, WID, "retired")
        (self.ws / "state" / "bindings.json").write_text(json.dumps({"bindings": {"!r:x": WID}}))
        with self.assertRaises(pr.WorkerBound):
            pr.remove_worker(self.ws, WID)


class OwedBase(Base):
    """A worker that owes a pending AND an accepted delivery. Retire/remove either
    refuse or hand both to the core; a replay after either never targets the worker;
    a late result from the old worker is neither run twice nor lost."""

    def setUp(self):
        super().setUp()
        (self.ws / "tasks").mkdir()
        (self.ws / "results").mkdir()
        for t in ("task-p", "task-a"):
            (self.ws / "tasks" / f"{t}.txt").write_text(f"id: {t}\ntask: hi\n")
            self.assertEqual(router.route(self.ws, {"id": t, "requested_worker": WID})["delivered"], [WID])
        pdm.accept(self.ws / "deliveries" / WID / "task-a.txt")
        pdm.mark_done(self.ws, WID, "task-a", published=False)

    def core_queue(self):
        return list(td.pending_candidates(self.ws / "tasks", self.ws / "results",
                                          deliveries_dir=self.ws / "deliveries"))

    def replay(self, t):
        """The core watcher's replay decision: DECLINE hands the task to the core."""
        code, targets, _ = handler.classify(self.ws, {"id": t, "requested_worker": WID})
        return {"code": code, "targets": targets}

    def assert_core_owns(self, t):
        self.assertIsNone(router.committed_recipient(self.ws, t))
        self.assertEqual(self.replay(t)["code"], handler.DECLINE)
        self.assertEqual(pdm.find(self.ws, WID, t), None)
        self.assertIsNone(pattr.worker_for_task(self.ws, t))
        self.assertIn(f"{t}.txt", self.core_queue())


class OwedDeliveries(OwedBase):
    def test_before_the_fix_shape_replay_follows_the_committed_worker(self):
        self.assertEqual(self.replay("task-a"), {"code": 0, "targets": [WID]})
        self.assertEqual(self.core_queue(), [])

    def test_stop_refuses_an_owing_worker_unless_forced_and_force_keeps_the_work(self):
        rc, out = self.main("stop", WID)
        self.assertEqual((rc, out["result"]), (2, "refused"))
        self.assertIn("task-a", out["why"])
        self.assertFalse(sup.is_paused(self.ws, WID))
        self.assertEqual(self.tmux.killed(), [])
        rc, out = self.main("stop", WID, "--force")
        self.assertEqual((rc, out["owed_kept"]), (0, ["task-a", "task-p"]))
        self.assertEqual(self.replay("task-a"), {"code": 0, "targets": [WID]})

    def test_retire_refuses_without_hand_off_and_changes_nothing(self):
        rc, out = self.main("retire", WID)
        self.assertEqual((rc, out["result"]), (2, "refused"))
        self.assertEqual(self.row()["state"], "live")
        self.assertFalse(sup.is_paused(self.ws, WID))
        self.assertEqual(self.tmux.killed(), [])
        self.assertIsNotNone(pdm.find(self.ws, WID, "task-a"))

    def test_retire_hand_off_moves_pending_and_accepted_to_the_core(self):
        rc, out = self.main("retire", WID, "--hand-off")
        self.assertEqual((rc, out["result"]), (0, "retired"))
        self.assertEqual(sorted(out["handed_off"]), ["task-a", "task-p"])
        self.assertFalse(pdm.pending_flag(self.ws, WID, "task-a").exists())
        for t in ("task-a", "task-p"):
            self.assert_core_owns(t)

    def test_replay_after_remove_targets_the_core(self):
        self.main("retire", WID, "--hand-off")
        rc, out = self.main("remove", WID)
        self.assertEqual((rc, out["result"]), (0, "removed"))
        for t in ("task-a", "task-p"):
            self.assert_core_owns(t)
            self.assertFalse((self.ws / "deliveries" / WID / f"{t}.accepted").exists())

    def test_a_result_landing_before_the_hand_off_stays_the_workers_and_is_not_rerun(self):
        real = pm._take_down

        def late(ws, wid, runner):
            out = real(ws, wid, runner)
            (self.ws / "results" / "task-a.txt").write_text("task: task-a\nthe worker's reply\n")
            return out
        with mock.patch.object(pm, "_take_down", late):
            rc, out = self.main("retire", WID, "--hand-off")
        self.assertEqual((rc, out["handed_off"], out["kept"]), (0, ["task-p"], {"task-a": "completed"}))
        self.assertEqual(pattr.worker_for_task(self.ws, "task-a"), WID)
        self.assertNotIn("task-a.txt", self.core_queue())
        self.assertTrue(td.has_ready_result(self.ws / "results", "task-a.txt"))

    def test_a_late_result_after_the_hand_off_is_delivered_once_not_rerun(self):
        self.main("retire", WID, "--hand-off")
        self.assertIn("task-a.txt", self.core_queue())
        (self.ws / "results" / "task-a.txt").write_text("task: task-a\nthe worker's reply\n")
        self.assertNotIn("task-a.txt", self.core_queue())
        self.assertTrue(td.has_ready_result(self.ws / "results", "task-a.txt"))
        self.assertEqual(self.replay("task-a")["code"], handler.DECLINE)

    def test_hand_off_never_runs_while_a_session_cannot_be_confirmed_gone(self):
        self.tmux = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "server exited unexpectedly")
        rc, out = self.main("retire", WID, "--hand-off")
        self.assertEqual((rc, out["result"]), (1, "failed"))
        self.assertEqual(self.row()["state"], "live")
        self.assertEqual(router.committed_recipient(self.ws, "task-a"), WID)


class SelfTarget(Base):
    def test_a_worker_cannot_stop_or_retire_itself(self):
        with mock.patch.dict("os.environ", {"SUTANDO_INSTANCE_ID": WID}):
            for verb in ("stop", "retire"):
                rc, out = self.main(verb, WID)
                self.assertEqual((rc, out["result"]), (2, "refused"))
        self.assertEqual(self.tmux.killed(), [])


class RetireRollback(Base):
    def test_a_failed_take_down_leaves_the_worker_live_and_bound(self):
        pr.bind_room(self.ws, "!room:example", WID)
        self.tmux = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "server exited unexpectedly")
        rc, out = self.main("retire", WID, "--unbind")
        self.assertEqual((rc, out["result"]), (1, "failed"))
        self.assertEqual(self.row()["state"], "live")
        self.assertEqual(pr.load_bindings(self.ws), {"!room:example": WID})


class EdgePaths(Base):
    def test_refusals_for_names_rosters_and_unreadable_owed_work(self):
        pr.rename_worker(self.ws, OTHER, "dup")
        roster = json.loads((self.ws / "state" / "roster.json").read_text())
        roster["workers"][WID]["display_label"] = "dup"
        (self.ws / "state" / "roster.json").write_text(json.dumps(roster))
        self.assertEqual(self.main("stop", "dup")[0], 2)
        with mock.patch.object(sup, "work_outstanding", return_value=None):
            rc, out = self.main("stop", WID)
        self.assertEqual((rc, out["result"]), (2, "refused"))
        self.assertIn("cannot tell", out["why"])
        for path in ("roster.json",):
            (self.ws / "state" / path).unlink()
        self.assertEqual(self.main("stop", WID)[0], 2)
        self.assertEqual(self.main("list")[0], 2)
        with self.assertRaises(pr.RosterError):
            pr.set_worker_state(self.ws, WID, "retired")

    def test_roster_writer_refusals(self):
        with self.assertRaises(pr.RosterError):
            pr.set_worker_state(self.ws, "ghost", "retired")
        pr.bind_room(self.ws, "!r:x", WID)
        with self.assertRaises(pr.WorkerBound):
            pr.set_worker_state(self.ws, WID, "retired")
        with self.assertRaises(pr.RosterError):
            pr.remove_worker(self.ws, OTHER)

    def test_a_worker_with_no_recorded_run_has_no_session_to_kill(self):
        self.assertIsNone(sup.run_socket(self.ws, "c" * 32))
        with mock.patch.object(sup, "run_socket", return_value=None):
            rc, out = self.main("stop", WID)
        self.assertEqual((rc, out["sessions"]), (0, {}))

    def test_a_kill_that_raises_still_reports_by_the_re_probe(self):
        real = self.tmux

        def flaky(argv, **kw):
            if "kill-session" in argv:
                real(argv)
                raise OSError("tmux vanished")
            return real(argv, **kw)
        self.tmux = flaky
        rc, out = self.main("stop", WID)
        self.assertEqual((rc, out["result"]), (0, "stopped"))

    def test_enable_reports_a_failed_restart(self):
        with mock.patch.object(rem, "restart", return_value={"result": "failed", "detail": {"why": "x"}}):
            out = pm.enable(self.ws, REPO, WID)
        self.assertEqual(out["result"], "failed")

    def test_list_text_and_a_malformed_row(self):
        roster = json.loads((self.ws / "state" / "roster.json").read_text())
        roster["workers"]["d" * 32] = "not-a-row"
        (self.ws / "state" / "roster.json").write_text(json.dumps(roster))
        rc, lines = self.main("list")
        self.assertEqual(rc, 0)
        self.assertTrue(any("pool worker 1" in ln for ln in lines))
        self.assertFalse(any("d" * 32 in ln for ln in lines))


class EdgeOwed(OwedBase):
    def test_remove_refuses_owed_work_and_fails_on_an_unconfirmed_take_down(self):
        pr.set_worker_state(self.ws, WID, "retired")
        rc, out = self.main("remove", WID)
        self.assertEqual((rc, out["result"]), (2, "refused"))
        self.tmux = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "server exited unexpectedly")
        rc, out = self.main("remove", WID, "--hand-off")
        self.assertEqual((rc, out["result"]), (1, "failed"))
        self.assertIsNotNone(self.row())

    def test_conflicting_evidence_fails_the_hand_off_and_moves_nothing(self):
        pattr.attribution_path(self.ws, "task-a").write_text("not-a-worker-id")
        rc, out = self.main("retire", WID, "--hand-off")
        self.assertEqual((rc, out["result"]), (1, "failed"))
        self.assertIsNotNone(pdm.find(self.ws, WID, "task-a"))

    def test_revoke_removes_only_a_record_naming_that_worker(self):
        self.assertFalse(pattr.revoke(self.ws, "task-a", OTHER))
        self.assertFalse(pattr.revoke(self.ws, "task-none", WID))
        self.assertTrue(pattr.revoke(self.ws, "task-a", WID))


if __name__ == "__main__":
    unittest.main()
