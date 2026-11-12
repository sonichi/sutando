#!/usr/bin/env python3
"""A seat parked on a usage limit restarts once the owner signs in to another account.

On 2026-11-11 the Claude account hit its limit; the core and six Claude seats stopped. The owner
signed in to another account, the core resumed, and five seats stayed parked on "Usage limit
reached · Continuing automatically at 8:40pm · esc to cancel" until each was restarted by hand.

Through the production sweep (`pool_remedy --sweep`: supervision tick, policy, apply), with fake
tmux panes carrying that banner and the one a worker pane showed live:
- a parked seat's limit is bound to the account it was seen under; the same account and a future
  reset keep it waiting;
- another account on disk restarts it at once: one note to the owner first, the restart, a
  closing note only for a failure;
- a seat mid-turn under the banner is never touched, nor one whose turn starts between the tick
  and the restart;
- a seat that owes work keeps it across the restart;
- retry_limit accepts the footer form it refused that night;
- the core coming back (hold released, account switched, cleared, signed in) asks for exactly one
  sweep per event.

Run: python3 tests/skills/worker-pool/pool-new-login-recovery.test.py
"""
from __future__ import annotations

import importlib.util as u
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

SRC, SCRIPTS = REPO / "src", REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SRC))


def _load(name, path):
    if name not in sys.modules:
        spec = u.spec_from_file_location(name, path)
        mod = u.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


rem = _load("pool_remedy", SCRIPTS / "pool_remedy.py")
sup, ps, wi = rem.sup, rem.ps, rem.wi
import core_limit_hold as clh  # noqa: E402
import pool_sweep_trigger as trig  # noqa: E402

RULE = "─" * 40 + "\n"
FOOTER = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
# The banner the parked seats showed on 2026-11-11, as the core read it off their panes.
TONIGHT = "  ⚠ Usage limit reached · Continuing automatically at 8:40pm · esc to cancel\n"
# A worker pane captured live (tests/core-input-watch-limit-episode.test.py, _BANNER).
LIVE = ("  ⚠ Usage limit reached · limit resets Oct 13 at 11pm\n"
        "    Continuing automatically at Oct 13 at 11pm · esc to cancel\n")
DONE = "● Sent a cross-session message.\n✻ Worked for 27s · done 6:54 PM\n"
THINKING = "● Reading the task.\n✳ Thinking… (12s · esc to interrupt)\n"


def pane(body, banner=TONIGHT):
    return f"{body}{RULE}❯ \n{RULE}{banner}{FOOTER}"


PARKED, PARKED_LIVE, MID_TURN = pane(DONE), pane(DONE, LIVE), pane(THINKING)
IDLE = f"{DONE}{RULE}❯ \n{RULE}{FOOTER}"
A, B = "acct-aaaa", "acct-bbbb"


class Pool(unittest.TestCase):
    """Workers on one fake tmux server; each sweep runs the real `pool_remedy.main --sweep`."""

    WORKERS = {"a" * 32: "Mars", "b" * 32: "Baxter"}

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.ws = Path(self.td.name)
        (self.ws / "state").mkdir()
        (self.ws / "results").mkdir()
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"workers": {w: {"state": "live", "runtime": "claude", "label": n} for w, n in self.WORKERS.items()}}))
        self.socket = str(self.ws / "tmux.sock")
        self.alive, self.panes, self.spawned, self.account = set(), {}, [], A
        for i, w in enumerate(self.WORKERS):
            wi.record_session(self.ws, w, f"s{i}", runtime="claude", relation=wi.RELATION_NEW)
            wi.start_incarnation(self.ws, w, f"s{i}", tmux_socket=self.socket,
                                 tmux_session=wi.tmux_session_name(w))
            rem.pd.deliveries_dir(self.ws, w).mkdir(parents=True)
            self.alive.add(wi.tmux_session_name(w))
            self.panes[w] = [IDLE]
        self.now = 1_000_000.0

    def seat(self, wid):
        return wi.tmux_session_name(wid)

    def show(self, wid, *frames):
        """The pane's next captures, in order; the last one repeats."""
        self.panes[wid] = list(frames)

    def runner(self, argv, **kwargs):
        name = argv[argv.index("-t") + 1].lstrip("=").split(":")[0] if "-t" in argv else ""
        if "capture-pane" in argv:
            wid = next(w for w in self.WORKERS if self.seat(w) == name)
            frames = self.panes[wid]
            return subprocess.CompletedProcess(argv, 0, frames.pop(0) if len(frames) > 1 else frames[0], "")
        if "has-session" in argv:
            if name in self.alive:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 1, "", f"can't find session: {name}")
        if "kill-session" in argv:
            self.alive.discard(name)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "inbox-holders" in argv:
            return subprocess.CompletedProcess(argv, 0, "4242 session\n", "")
        raise AssertionError(f"unexpected process operation: {argv}")

    def spawn(self, workspace, repo, **kw):
        wid = kw.get("existing_worker_id") or next(w for w in self.WORKERS if self.seat(w) not in self.alive)
        self.spawned.append((wid, kw.get("resume")))
        self.alive.add(self.seat(wid))
        self.panes[wid] = [IDLE]
        return {"runtime_session_id": f"{kw.get('resume')}-again"}

    def owe(self, wid, task):
        tasks = self.ws / "tasks"
        tasks.mkdir(exist_ok=True)
        (rem.pd.deliveries_dir(self.ws, wid) / f"{task}.txt").write_text("pending")
        (tasks / f"{task}.txt").write_text(f"id: {task}\ntask: x\n")

    def sweep(self):
        self.now += 300.0
        real_tick, real_apply = sup.tick, rem.apply
        with patch.object(sup, "tick", side_effect=lambda ws, now, **kw: real_tick(ws, now, runner=self.runner, **kw)), \
                patch.object(rem, "apply", side_effect=lambda ws, repo, d, **kw:
                             real_apply(ws, repo, d, runner=self.runner, spawn=self.spawn)), \
                patch.object(sup, "current_account", side_effect=lambda: self.account), \
                patch.object(rem.wc, "raise_card", return_value={"outcome": "carded"}), \
                patch.object(rem.wc, "resolve_cleared", return_value=[]), \
                patch.object(rem.wc, "drive_escapes", return_value={}), \
                patch.object(rem, "_repair_human_cards", return_value=None), \
                patch.object(rem, "retry_limit", return_value={"outcome": "rearm-sent"}), \
                patch.object(rem, "ensure_supervisors", return_value={}), \
                patch.object(rem, "ensure_input_watches", return_value={}), \
                patch.object(rem.time, "time", return_value=self.now), \
                patch.object(rem.subprocess, "run", side_effect=AssertionError("unexpected subprocess")), \
                patch("sys.stdout", new=io.StringIO()) as out:
            rc = rem.main(["--workspace", str(self.ws), "--repo", str(REPO), "--sweep"], runner=self.runner)
        return rc, json.loads(out.getvalue())

    def notes(self):
        return sorted(p.read_text() for p in (self.ws / "results").glob("proactive-*.txt"))


class NewLogin(Pool):
    def test_the_same_account_keeps_waiting_for_the_reset(self):
        w = next(iter(self.WORKERS))
        self.show(w, PARKED)
        for _ in range(3):
            rc, out = self.sweep()
            self.assertEqual(rc, 0)
            self.assertNotEqual(out["decisions"].get(w), ps.RESTART_NEW_LOGIN)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.notes(), [])
        self.assertEqual(sup.load_state(self.ws).workers[w].limit_account, A, "bound to the account it was seen under")

    def test_another_account_restarts_every_parked_seat_at_once(self):
        mars, baxter = list(self.WORKERS)
        self.show(mars, PARKED)
        self.show(baxter, PARKED_LIVE)
        self.sweep()
        self.account = B
        rc, out = self.sweep()
        self.assertEqual(rc, 0)
        self.assertEqual(out["decisions"], {mars: ps.RESTART_NEW_LOGIN, baxter: ps.RESTART_NEW_LOGIN})
        self.assertEqual(sorted(self.spawned), [(mars, "s0"), (baxter, "s1")], "each resumes its own session")
        self.assertEqual(self.notes(), ["[dm-only]\nI'm back on a new login; restarting 2 workers parked on the "
                                        "old limit: Mars, Baxter.\n"], "one note first, none for a success")
        self.assertEqual(sup.load_state(self.ws).workers[mars].limit_account, B)
        _, out = self.sweep()
        self.assertEqual(len(self.spawned), 2, "a restarted seat is not restarted again")

    def test_a_seat_mid_turn_under_the_banner_is_never_touched(self):
        mars, baxter = list(self.WORKERS)
        self.show(mars, MID_TURN)
        self.show(baxter, PARKED)
        self.sweep()
        self.account = B
        _, out = self.sweep()
        self.assertNotEqual(out["decisions"].get(mars), ps.RESTART_NEW_LOGIN)
        self.assertEqual(self.spawned, [(baxter, "s1")])
        self.assertIn(self.seat(mars), self.alive)
        self.assertIn("1 worker parked on the old limit: Baxter.", self.notes()[0])

    def test_a_turn_that_starts_after_the_tick_is_left_alone(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        self.show(mars, PARKED, MID_TURN)  # the tick reads it parked, the restart's re-read sees a turn
        _, out = self.sweep()
        self.assertEqual(out["recoveries"][mars]["outcome"], "pane-changed")
        self.assertEqual(self.spawned, [])
        self.assertIn(self.seat(mars), self.alive)

    def test_owed_work_stays_the_seats_across_the_restart(self):
        mars = next(iter(self.WORKERS))
        self.owe(mars, "task-owed-1")
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        _, out = self.sweep()
        self.assertEqual(out["recoveries"][mars]["outcome"], rem.RECOVERED)
        self.assertEqual(self.spawned, [(mars, "s0")])
        self.assertTrue((rem.pd.deliveries_dir(self.ws, mars) / "task-owed-1.txt").exists(), "still the seat's")
        self.assertIs(sup.work_outstanding(self.ws, mars), True)
        self.assertFalse(sup.is_paused(self.ws, mars), "the take-down's pause is lifted for the new seat")

    def test_a_failed_restart_is_told_after_the_note(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B

        def refuse(*a, **kw):
            raise rem.sw.SpawnRefused("the seat could not start")
        self.spawn = refuse
        rc, out = self.sweep()
        self.assertEqual(rc, 1)
        self.assertEqual(out["recoveries"][mars]["outcome"], rem.FAILED)
        notes = self.notes()
        self.assertEqual(len(notes), 2)
        self.assertIn("Could not restart on the new login: Mars (the seat could not start)", "".join(notes))

    def test_an_unreadable_account_binds_nothing_and_ends_nothing(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.account = None
        self.sweep()
        self.account = B
        self.sweep()
        self.account = None
        _, out = self.sweep()
        self.assertEqual(self.spawned, [])
        self.assertNotEqual(out["decisions"].get(mars), ps.RESTART_NEW_LOGIN)

    def test_a_paused_seat_is_left_paused(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        sup.set_paused(self.ws, mars, True)
        self.account = B
        self.sweep()
        self.assertEqual(self.spawned, [])


class RetryLimitReadsTheFooterForm(Pool):
    def test_the_footer_banner_is_a_limit_wait_not_a_changed_pane(self):
        mars = next(iter(self.WORKERS))
        self.owe(mars, "task-owed-2")
        self.show(mars, PARKED)

        class Ask:
            @staticmethod
            def ask(ws, wid, text):
                return {"task_id": "task-recovery-1"}

        class Sent:
            code = 0
        with patch.object(rem, "_sibling", return_value=Ask), \
                patch.object(sup.pane_gate, "deliver", return_value=Sent()):
            got = rem.retry_limit(self.ws, REPO, mars, runner=self.runner)
        self.assertEqual(got["outcome"], "rearm-sent", "the 2026-11-11 refusal was pane-changed-or-pending")
        self.show(mars, MID_TURN)
        with patch.object(rem, "_sibling", return_value=Ask):
            self.assertEqual(rem.retry_limit(self.ws, REPO, mars, runner=self.runner)["outcome"],
                             "pane-changed-or-pending", "a running turn is still refused")


class CoreRecoveredTrigger(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.ws = Path(self.td.name)
        (self.ws / "state").mkdir()
        self.spawns = []

    def spawn(self, argv, **kw):
        self.spawns.append(argv)

    def test_one_event_asks_for_exactly_one_sweep(self):
        for _ in range(3):
            trig.request_sweep(self.ws, "core-signed-in", "signed-in:100", spawn=self.spawn)
        self.assertEqual(len(self.spawns), 1)
        argv = self.spawns[0]
        self.assertEqual(argv[1:], [str(SCRIPTS / "pool_remedy.py"), "--workspace", str(self.ws),
                                    "--repo", str(REPO), "--sweep"])
        trig.request_sweep(self.ws, "core-limit-released", "core-launch:200", spawn=self.spawn)
        self.assertEqual(len(self.spawns), 2, "a new event is a new sweep")

    def test_no_pool_installed_is_no_sweep(self):
        self.assertFalse(trig.request_sweep(self.ws, "x", "k", repo=self.ws, spawn=self.spawn))
        self.assertEqual(self.spawns, [])

    def test_the_hold_asks_once_when_it_releases_and_when_cleared(self):
        asked = []
        hold = {"status": "held", "until": 50.0, "account": A, "set_at": 10.0, "reason": "limit"}
        (self.ws / "state" / clh.HOLD_FILE).write_text(json.dumps(hold))
        with patch.object(trig, "request_sweep", side_effect=lambda ws, cause, key, **kw: asked.append((cause, key))):
            clh.decide(self.ws, 100.0, quota={}, launch_account=A)
            clh.decide(self.ws, 400.0, quota={}, launch_account=A)
            self.assertEqual(asked, [("core-limit-released", "core-launch:100")],
                             "released once; a later plain launch is not a recovery")
            clh.decide(self.ws, 700.0, quota={}, launch_account=B)
            self.assertEqual(asked[-1], ("core-account-switched", "core-launch:700"))
            (self.ws / "state" / clh.HOLD_FILE).write_text(json.dumps(dict(hold, until=10_000.0)))
            clh.clear(self.ws, now=800.0)
            self.assertEqual(asked[-1], ("core-limit-cleared", "core-clear:800"))
        self.assertEqual(len(asked), 3)

    def test_the_core_signing_in_asks_once(self):
        watch = _load("core_input_watch", SRC / "core-input-watch.py")
        with patch.object(trig.subprocess, "Popen", side_effect=self.spawn):
            self.assertTrue(watch.pool_sweep_after_sign_in(self.ws, 1234.5))
            self.assertFalse(watch.pool_sweep_after_sign_in(self.ws, 1234.9), "the same sign-in")
        self.assertEqual(len(self.spawns), 1)


if __name__ == "__main__":
    unittest.main()
