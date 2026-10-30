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
import pool_advertise as pa  # noqa: E402
import pool_router as router  # noqa: E402

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
        wid, old, roster = pr.set_worker_state(self.ws, "pool worker 1", "live")
        self.assertEqual((wid, old, roster), (WID, "live", None))

    def test_remove_worker_refuses_a_bound_retired_worker(self):
        pr.set_worker_state(self.ws, WID, "retired")
        (self.ws / "state" / "bindings.json").write_text(json.dumps({"bindings": {"!r:x": WID}}))
        with self.assertRaises(pr.WorkerBound):
            pr.remove_worker(self.ws, WID)


if __name__ == "__main__":
    unittest.main()
