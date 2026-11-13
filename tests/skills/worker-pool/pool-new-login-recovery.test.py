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
        # As spawn_worker does: the worker is the one whose session it resumes.
        wid = (kw.get("existing_worker_id") or wi.worker_for_session(self.ws, kw.get("resume") or "")
               or next(w for w in self.WORKERS if self.seat(w) not in self.alive))
        self.spawned.append((wid, kw.get("resume")))
        # As spawn_worker does: a new run, carrying the account it launched under.
        wi.start_incarnation(self.ws, wid, kw.get("resume"), tmux_socket=self.socket,
                             tmux_session=self.seat(wid), account=self.account)
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
                patch.object(rem.wc, "_account_label", return_value=None), \
                patch.object(rem, "_repair_human_cards", return_value=None), \
                patch.object(rem, "retry_limit", return_value={"outcome": "rearm-sent"}), \
                patch.object(rem, "ensure_supervisors", return_value={}), \
                patch.object(rem, "ensure_input_watches", return_value={}), \
                patch.object(rem.time, "time", return_value=self.now), \
                patch.object(rem.subprocess, "run", side_effect=AssertionError("unexpected subprocess")), \
                patch("sys.stdout", new=io.StringIO()) as out:
            rc = rem.main(["--workspace", str(self.ws), "--repo", str(REPO), "--sweep"], runner=self.runner)
        return rc, json.loads(out.getvalue())

    def sweep_until(self, wid, *, most=8):
        """Sweep at the normal cadence until `wid` is acted on (its backoff allows a try); its outcome."""
        for _ in range(most):
            rc, out = self.sweep()
            if wid in out["recoveries"]:
                return rc, out["recoveries"][wid]
        return None, None

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

    def test_a_turn_that_starts_after_the_tick_is_left_alone_and_said_so(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        self.show(mars, PARKED, MID_TURN)  # the tick reads it parked, the restart's re-read sees a turn
        _, out = self.sweep()
        self.assertEqual(out["recoveries"][mars]["outcome"], "pane-changed")
        self.assertEqual(self.spawned, [])
        self.assertIn(self.seat(mars), self.alive)
        self.assertIn("Not restarted on the new login: Mars (it was busy again", self.notes()[0] + self.notes()[1])
        self.assertNotIn("The rest are back", "".join(self.notes()), "none came back")
        self.show(mars, PARKED)  # it parked again on the old account's limit: a later sweep tries again
        self.sweep_until(mars)
        self.assertEqual(self.spawned, [(mars, "s0")], "the binding stood, so the try came again")

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

    def test_a_failed_restart_is_told_and_tried_again_up_to_the_cap(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        real_runner = self.runner

        def kill_unconfirmed(argv, **kw):  # tmux cannot confirm the parked seat gone, so it stays up
            if "kill-session" in argv and self.seat(mars) in argv[-1]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return real_runner(argv, **kw)
        self.runner = kill_unconfirmed
        for attempt in range(1, ps.RELOGIN_MAX_ATTEMPTS + 1):
            rc, got = self.sweep_until(mars)
            self.assertEqual((rc, got["outcome"]), (1, rem.FAILED), attempt)
        self.assertEqual(self.sweep_until(mars), (None, None), "past the cap it is not tried again")
        joined = "".join(self.notes())
        self.assertIn("Not restarted on the new login: Mars (tmux could not confirm the parked seat gone; "
                      "I'll try again", joined)
        self.assertIn("no more automatic tries; restart it with: pool_manage.py reseat Mars", joined)
        self.assertEqual(sup.load_state(self.ws).workers[mars].limit_account, A, "never bound to B: not restarted")
        self.assertFalse(sup.is_paused(self.ws, mars))
        self.runner = real_runner
        self.show(mars, IDLE)  # the owner restarted it by hand: the episode ends
        self.sweep()
        self.assertEqual(sup.load_state(self.ws).workers[mars].relogin_attempts, 0)

    def test_the_note_comes_before_any_restart(self):
        mars, baxter = list(self.WORKERS)
        self.show(mars, PARKED)
        self.show(baxter, PARKED)
        self.sweep()
        self.account = B
        events, real_tell, real_spawn = [], rem.tell_owner, self.spawn
        self.spawn = lambda ws, repo, **kw: (events.append("restart"), real_spawn(ws, repo, **kw))[1]
        with patch.object(rem, "tell_owner", side_effect=lambda *a: (events.append("note"), real_tell(*a))[1]):
            self.sweep()
        self.assertEqual(events, ["note", "restart", "restart"])

    def test_a_note_that_cannot_be_queued_restarts_nothing_and_the_sweep_goes_on(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        with patch.object(rem, "tell_owner", side_effect=OSError("disk full")):
            rc, out = self.sweep()
        self.assertEqual((rc, out["recoveries"][mars]["outcome"]), (0, "note-failed"))
        self.assertEqual(self.spawned, [])
        self.assertIn("cards_closed", out, "the rest of the sweep still ran")
        self.assertEqual(self.sweep_until(mars)[1]["outcome"], rem.RECOVERED, "a later sweep tries again")
        self.assertEqual(self.spawned, [(mars, "s0")])

    def test_a_failing_take_down_never_leaves_the_seat_paused(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        with patch.object(rem, "close_dead_runs", side_effect=OSError("runs unreadable")):
            _, out = self.sweep()
        self.assertEqual(out["recoveries"][mars]["outcome"], rem.FAILED)
        self.assertIn("OSError: runs unreadable", out["recoveries"][mars]["why"])
        self.assertFalse(sup.is_paused(self.ws, mars), "supervision still sees it")

    def test_a_suspension_written_during_the_sweep_stops_the_kill(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        real_tell = rem.tell_owner

        def quit_then_tell(*a):
            rem.suspend(self.ws, "the app is quitting")  # lands after the decision, before the take-down
            return real_tell(*a)
        with patch.object(rem, "tell_owner", side_effect=quit_then_tell):
            _, out = self.sweep()
        self.assertEqual(out["recoveries"][mars]["outcome"], rem.SUSPENDED)
        self.assertIn(self.seat(mars), self.alive, "nothing was killed")
        self.assertEqual(self.spawned, [])

    def test_a_seat_first_seen_parked_after_the_sign_in_is_still_restarted(self):
        mars = next(iter(self.WORKERS))
        self.sweep()  # running on A: the pool reads A
        self.account = B  # the owner signs in, and the seat had parked on A's limit in between
        self.show(mars, PARKED)
        _, out = self.sweep()
        self.assertEqual(out["decisions"][mars], ps.RESTART_NEW_LOGIN)
        self.assertEqual(self.spawned, [(mars, "s0")])

    def test_an_accepted_task_in_flight_is_still_the_seats_after_the_restart(self):
        mars = next(iter(self.WORKERS))
        self.owe(mars, "task-in-flight")
        inbox = rem.pd.deliveries_dir(self.ws, mars)
        rem.pd.accept(inbox / "task-in-flight.txt")
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        _, out = self.sweep()
        self.assertEqual(out["recoveries"][mars]["outcome"], rem.RECOVERED)
        kept = sorted(p.name for p in inbox.glob("task-in-flight.*"))
        self.assertTrue(kept, "the accepted task is neither dropped nor handed away")
        self.assertIs(sup.work_outstanding(self.ws, mars), True)


    def test_a_spawn_that_fails_after_the_kill_is_started_again_until_it_starts(self):
        mars = next(iter(self.WORKERS))
        self.owe(mars, "task-owed-3")
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        real_spawn, tries = self.spawn, []

        def fail_twice(workspace, repo, **kw):
            tries.append(kw.get("resume"))
            if len(tries) <= 2:
                raise rem.sw.SpawnRefused("the seat could not start")
            return real_spawn(workspace, repo, **kw)
        self.spawn = fail_twice
        rc, out = self.sweep()
        self.assertEqual((rc, out["recoveries"][mars]["outcome"]), (1, rem.FAILED))
        self.assertNotIn(self.seat(mars), self.alive, "the kill landed: the seat is down")
        self.assertIn("it is down and not coming back on its own", "".join(self.notes()))
        self.assertIn("pool_manage.py retire Mars", "".join(self.notes()), "it names the way out")
        self.assertIs(sup.probe_session(self.ws, mars, runner=self.runner), False, "supervision still sees it")
        self.assertEqual(self.sweep_until(mars)[1]["outcome"], rem.FAILED, "tried again while down")
        self.assertEqual(self.sweep_until(mars)[1]["outcome"], rem.RECOVERED, "and it comes back")
        self.assertEqual(tries, ["s0", "s0", "s0"], "each try resumes its own session")
        self.assertIn(self.seat(mars), self.alive)
        self.assertIs(sup.work_outstanding(self.ws, mars), True, "its owed work is still its own")
        state = sup.load_state(self.ws).workers[mars]
        self.assertEqual((state.limit_account, state.relogin_down), (B, False))
        openers = [n for n in self.notes() if "I'm back on a new login" in n]
        self.assertEqual(len(openers), 1, "one opening note for the sign-in, whatever the tries")

    def test_a_seat_that_never_starts_is_told_once_and_tried_hourly_for_good(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B

        def never(workspace, repo, **kw):
            raise rem.sw.SpawnRefused("its folder is gone")
        self.spawn = never
        tried_at = []
        for _ in range(288):  # a day at the timer's cadence
            _, out = self.sweep()
            if out["decisions"].get(mars) == ps.RESTART_NEW_LOGIN:
                tried_at.append(self.now)
        gaps = [b - a for a, b in zip(tried_at, tried_at[1:])]
        cap = ps.RELOGIN_DOWN_MAX_RETRY_S
        self.assertTrue(all(b >= a for a, b in zip(gaps, gaps[1:])), f"the wait only grows: {gaps}")
        self.assertEqual(max(gaps), cap, "and stops growing at the ceiling")
        self.assertGreater(gaps.count(cap), 15, "an hour apart, for good")
        self.assertNotIn(self.seat(mars), self.alive)
        notes = self.notes()
        self.assertEqual(len(notes), 2, f"one opener and one note that it is not coming back: {notes}")
        self.assertEqual(sum("not coming back" in n for n in notes), 1)

    def test_the_down_wait_doubles_from_the_first_failed_start(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B

        def never(workspace, repo, **kw):
            raise rem.sw.SpawnRefused("broken CLI")
        self.spawn = never
        tried_at = []
        for _ in range(60):
            _, out = self.sweep()
            if out["decisions"].get(mars) == ps.RESTART_NEW_LOGIN:  # not the dead-seat ladder's own tries
                tried_at.append(self.now)
        gaps = [b - a for a, b in zip(tried_at, tried_at[1:])]
        self.assertEqual(gaps[:6], [300.0, 600.0, 1200.0, 2400.0, 3600.0, 3600.0],
                         "doubled per try from 300 s, then an hour apart")

    def test_a_down_seat_seen_alive_drops_its_down_mark(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B

        def never(workspace, repo, **kw):
            raise rem.sw.SpawnRefused("broken CLI")
        real = self.spawn
        self.spawn = never
        self.sweep()
        ev = sup.load_state(self.ws).workers[mars]
        self.assertEqual((ev.relogin_down, ev.relogin_down_told), (True, True))
        self.spawn = real
        real(self.ws, REPO, resume="s0")  # the owner starts it by hand
        self.show(mars, MID_TURN)
        self.sweep()
        ev = sup.load_state(self.ws).workers[mars]
        self.assertEqual((ev.relogin_down, ev.relogin_down_told), (False, False))
        before = len(self.spawned)
        for _ in range(4):
            self.sweep()
        self.assertEqual(len(self.spawned), before, "an alive seat is never restarted by the down rule")

    def test_one_seats_unexpected_error_never_stops_the_others_or_the_note(self):
        mars, baxter = list(self.WORKERS)
        for w in (mars, baxter):
            self.show(w, PARKED)
        self.sweep()
        self.account = B
        real_recover = rem.recover

        def mars_breaks(ws, repo, w, **kw):
            if w == mars:
                raise RuntimeError("boom")  # not SpawnRefused/OSError: an error nobody expected
            return real_recover(ws, repo, w, **kw)
        with patch.object(rem, "recover", side_effect=mars_breaks):
            rc, out = self.sweep()
        self.assertEqual(out["recoveries"][mars]["outcome"], rem.FAILED)
        self.assertIn("RuntimeError", out["recoveries"][mars]["why"])
        self.assertEqual(out["recoveries"][baxter]["outcome"], rem.RECOVERED, "the other seat still restarts")
        self.assertTrue(any("Not restarted on the new login: Mars" in n for n in self.notes()))
        self.assertNotIn(self.seat(mars), self.alive)
        self.assertTrue(sup.load_state(self.ws).workers[mars].relogin_down, "and it is started again later")
        self.assertEqual(self.sweep_until(mars)[1]["outcome"], rem.RECOVERED)

    def test_a_down_mark_that_cannot_be_saved_kills_nothing(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        with patch.object(sup, "mark_relogin_down", side_effect=OSError("read-only")):
            _, out = self.sweep()
        self.assertEqual(out["recoveries"][mars]["outcome"], rem.FAILED)
        self.assertIn("left running", out["recoveries"][mars]["why"])
        self.assertIn(self.seat(mars), self.alive, "unmarked, it is never taken down")
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.sweep_until(mars)[1]["outcome"], rem.RECOVERED, "a later sweep restarts it")

    def test_a_confirmation_that_cannot_be_saved_still_restarts_the_rest(self):
        mars, baxter = list(self.WORKERS)
        for w in (mars, baxter):
            self.show(w, PARKED)
        self.sweep()
        self.account = B
        with patch.object(sup, "confirm_new_login", side_effect=OSError("read-only")):
            _, out = self.sweep()
        self.assertEqual({w: r["outcome"] for w, r in out["recoveries"].items()},
                         {mars: rem.RECOVERED, baxter: rem.RECOVERED})
        self.assertIn(self.seat(mars), self.alive)
        self.assertIn(self.seat(baxter), self.alive)

    def test_a_suspension_written_during_the_capture_stops_the_kill(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        real_runner, real_restart, restarting = self.runner, rem.restart_on_new_login, []

        def quit_while_capturing(argv, **kw):
            # The restart's own capture is where the app quits: the marker lands mid-capture.
            if restarting and "capture-pane" in argv:
                rem.suspend(self.ws, "the app is quitting")
            return real_runner(argv, **kw)

        def restart(*a, **kw):
            restarting.append(1)
            return real_restart(*a, **kw)
        self.runner = quit_while_capturing
        with patch.object(rem, "restart_on_new_login", side_effect=restart):
            _, out = self.sweep()
        got = out["recoveries"][mars]
        self.assertEqual(got["outcome"], rem.SUSPENDED)
        self.assertNotIn("taken_down", got, "nothing was taken down")
        self.assertIn(self.seat(mars), self.alive, "the seat is still running")
        self.assertEqual(self.spawned, [])
        self.runner = real_runner
        rem.resume(self.ws, REPO, runner=self.runner, spawn=self.spawn)
        self.assertEqual(sup.load_state(self.ws).workers[mars].relogin_attempts, 0, "the skipped try is not spent")
        self.assertEqual(self.sweep_until(mars)[1]["outcome"], rem.RECOVERED, "restarted once the quit is over")

    def test_a_suspension_written_as_the_seat_is_marked_down_still_stops_the_kill(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        real_mark = sup.mark_relogin_down

        def quit_while_marking(ws, w):
            real_mark(ws, w)
            rem.suspend(self.ws, "the app is quitting")  # after the capture's check, before the kill
        with patch.object(sup, "mark_relogin_down", side_effect=quit_while_marking):
            _, out = self.sweep()
        self.assertEqual(out["recoveries"][mars]["outcome"], rem.SUSPENDED)
        self.assertIn(self.seat(mars), self.alive, "the last check before the kill held")
        rem.resume(self.ws, REPO, runner=self.runner, spawn=self.spawn)
        self.sweep()
        self.assertFalse(sup.load_state(self.ws).workers[mars].relogin_down, "seen alive, the mark is gone")

    def test_a_restart_whose_capture_fails_is_done_by_a_healthy_later_sweep(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        real_runner, real_restart, restarting = self.runner, rem.restart_on_new_login, []

        def capture_times_out(argv, **kw):
            if restarting and "capture-pane" in argv:
                raise subprocess.TimeoutExpired(argv, 10)
            return real_runner(argv, **kw)

        def restart(*a, **kw):
            restarting.append(1)
            return real_restart(*a, **kw)
        self.runner = capture_times_out
        with patch.object(rem, "restart_on_new_login", side_effect=restart):
            _, out = self.sweep()
        self.assertEqual(out["recoveries"][mars]["outcome"], rem.INDETERMINATE)
        self.assertEqual(sup.load_state(self.ws).workers[mars].limit_account, A, "the old binding stands")
        self.runner = real_runner
        self.assertEqual(self.sweep_until(mars)[1]["outcome"], rem.RECOVERED, "the next healthy sweep restarts it")

    def test_a_down_note_that_could_not_be_queued_is_written_on_the_next_try(self):
        mars = next(iter(self.WORKERS))
        self.owe(mars, "task-owed-9")
        self.show(mars, PARKED)
        self.sweep()
        self.account = B

        def never(workspace, repo, **kw):
            raise rem.sw.SpawnRefused("its folder is gone")
        self.spawn = never
        real_tell, failed = rem.tell_owner, []

        def first_failure_note_fails(workspace, prefix, text):
            if prefix == "pool-new-login-failed" and not failed:
                failed.append(text)
                raise OSError("results is full")
            return real_tell(workspace, prefix, text)
        with patch.object(rem, "tell_owner", side_effect=first_failure_note_fails):
            for _ in range(30):
                self.sweep()
        self.assertEqual(len(failed), 1)
        down = [n for n in self.notes() if "not coming back" in n]
        self.assertEqual(len(down), 1, f"told once, after the failed write: {self.notes()}")
        self.assertIn("pool_manage.py stop Mars --force", down[0], "the flag a seat that owes work needs")
        self.assertIn("pool_manage.py retire Mars --hand-off", down[0])
        self.assertTrue(sup.load_state(self.ws).workers[mars].relogin_down_told)

    def test_a_down_seat_retried_with_the_account_unreadable_gets_no_new_opener(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B

        def never(workspace, repo, **kw):
            raise rem.sw.SpawnRefused("broken CLI")
        self.spawn = never
        self.sweep()
        self.account = None  # the config cannot be read for the rest of the run
        for _ in range(48):
            self.sweep()
        openers = [n for n in self.notes() if "I'm back on a new login" in n]
        self.assertEqual(len(openers), 1, "retrying a seat already down says nothing again")

    def test_a_sign_in_that_cannot_be_recorded_never_aborts_the_sweep(self):
        mars, baxter = list(self.WORKERS)
        for w in (mars, baxter):
            self.show(w, PARKED)
        self.sweep()
        self.account = B
        with patch.object(sup, "announce_relogin", side_effect=OSError("read-only")):
            rc, out = self.sweep()
        self.assertEqual({w: r["outcome"] for w, r in out["recoveries"].items()},
                         {mars: rem.RECOVERED, baxter: rem.RECOVERED})

    def test_a_take_down_that_breaks_after_the_kill_still_comes_back(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        with patch.object(rem, "close_dead_runs", side_effect=OSError("runs unreadable")):
            _, out = self.sweep()
        self.assertEqual(out["recoveries"][mars]["outcome"], rem.FAILED)
        self.assertNotIn(self.seat(mars), self.alive)
        self.assertFalse(sup.is_paused(self.ws, mars))
        self.assertEqual(self.sweep_until(mars)[1]["outcome"], rem.RECOVERED)

    def test_a_suspension_between_the_kill_and_the_start_is_started_after_it_lifts(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        manage = rem._sibling("pool_manage")
        real = manage.take_down_held

        def kill_then_quit(*a, **kw):
            out = real(*a, **kw)
            rem.suspend(self.ws, "the app is quitting")  # after the kill, before the start
            return out
        with patch.object(manage, "take_down_held", side_effect=kill_then_quit):
            _, out = self.sweep()
        self.assertEqual(out["recoveries"][mars]["outcome"], rem.SUSPENDED)
        self.assertNotIn(self.seat(mars), self.alive)
        lifted = rem.resume(self.ws, REPO, runner=self.runner, spawn=self.spawn)
        self.assertNotIn(mars, lifted["restarted"], "the quit did not stop it: resume leaves it to its rule")
        self.assertFalse(rem.suspension(self.ws))
        self.assertEqual(self.sweep_until(mars)[1]["outcome"], rem.RECOVERED, "a seat this rule killed comes back")

    def test_the_tries_are_spaced_by_a_doubling_backoff(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        real_runner = self.runner

        def kill_unconfirmed(argv, **kw):
            if "kill-session" in argv and self.seat(mars) in argv[-1]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return real_runner(argv, **kw)
        self.runner = kill_unconfirmed
        tried_at = []
        for _ in range(8):
            _, out = self.sweep()
            if mars in out["recoveries"]:
                tried_at.append(self.now)
        gaps = [b - a for a, b in zip(tried_at, tried_at[1:])]
        self.assertEqual(len(tried_at), ps.RELOGIN_MAX_ATTEMPTS)
        self.assertEqual(gaps, [ps.RELOGIN_RETRY_S, 2 * ps.RELOGIN_RETRY_S], "300 s, then 600 s")

    def test_a_switch_to_a_third_account_starts_the_count_again(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        real_runner = self.runner

        def kill_unconfirmed(argv, **kw):
            if "kill-session" in argv and self.seat(mars) in argv[-1]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return real_runner(argv, **kw)
        self.runner = kill_unconfirmed
        for _ in range(ps.RELOGIN_MAX_ATTEMPTS):
            self.sweep_until(mars)
        self.assertEqual(self.sweep_until(mars), (None, None), "capped on B")
        self.runner = real_runner
        self.account = "acct-cccc"
        self.assertEqual(self.sweep_until(mars)[1]["outcome"], rem.RECOVERED, "a new sign-in is a new count")

    def test_a_seat_bound_to_its_launch_account_however_late_it_parks(self):
        mars = next(iter(self.WORKERS))
        self.show(mars, PARKED)
        self.sweep()
        self.account = B
        self.sweep()  # restarted on B: its new run is launched under B
        self.assertEqual(sup.launch_account(self.ws, mars), B)
        self.account = "acct-cccc"  # the owner signs in again; the seat keeps working a while
        self.show(mars, MID_TURN)
        for _ in range(3):
            self.sweep()
        self.show(mars, PARKED)  # it parks only now, on B's limit
        before = len(self.spawned)
        self.sweep()
        self.assertEqual(len(self.spawned), before + 1, "bound to B, the account its process launched under")

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


class CodexIsNotRestarted(Pool):
    WORKERS = {"c" * 32: "Reviewer"}

    def setUp(self):
        super().setUp()
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"workers": {w: {"state": "live", "runtime": "codex", "label": n} for w, n in self.WORKERS.items()}}))

    def test_a_codex_seat_under_a_limit_banner_is_left_to_its_own_reset(self):
        w = next(iter(self.WORKERS))
        self.show(w, PARKED)
        self.sweep()
        self.account = B
        _, out = self.sweep()
        self.assertNotEqual(out["decisions"].get(w), ps.RESTART_NEW_LOGIN)
        self.assertEqual(self.spawned, [])


class ErrorBranches(Pool):
    """Every way the new paths fail answers a value, and none of them raises out of a sweep."""

    def test_a_seat_with_no_open_run_or_an_unreadable_pane_is_not_restarted(self):
        mars = next(iter(self.WORKERS))
        rem.close_dead_runs(self.ws, mars, end_reason="killed")
        self.assertEqual(rem.restart_on_new_login(self.ws, REPO, mars, runner=self.runner)["outcome"], rem.NO_SESSION)
        baxter = list(self.WORKERS)[1]

        def unreadable(argv, **kw):
            raise OSError("tmux is gone")
        got = rem.restart_on_new_login(self.ws, REPO, baxter, runner=unreadable)
        self.assertEqual((got["outcome"], got["why"]), (rem.INDETERMINATE, "its pane could not be read"))
        self.assertEqual(self.spawned, [])

    def test_an_unreadable_account_is_none(self):
        with patch.object(clh, "current_account", side_effect=RuntimeError("config gone")):
            self.assertIsNone(sup.current_account())
        import spawn_worker
        with patch.object(clh, "current_account", side_effect=RuntimeError("config gone")):
            self.assertIsNone(spawn_worker.launch_account())
        with patch.object(clh, "current_account", return_value=A):
            self.assertEqual(spawn_worker.launch_account(), A)

    def test_the_account_is_a_digest_of_the_signed_in_identity(self):
        cfg = self.ws / "cfg"
        cfg.mkdir()
        (cfg / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "u-1", "emailAddress": "x@y"}}))
        with patch.object(clh, "_launch_config_dir", return_value=str(cfg)):
            got = clh.current_account()
        self.assertEqual(len(got), 16)
        self.assertNotIn("x@y", got)

    def test_a_failed_sweep_nudge_never_reaches_the_core(self):
        watch = _load("core_input_watch", SRC / "core-input-watch.py")
        with patch.object(trig, "request_sweep", side_effect=RuntimeError("no pool")):
            self.assertFalse(watch.pool_sweep_after_sign_in(self.ws, 1.0))
            clh._pool_sweep(self.ws, "core-limit-cleared", "k")  # swallowed: the timer still sweeps

    def test_a_spawn_that_cannot_start_is_no_sweep(self):
        def broken(argv, **kw):
            raise OSError("no python")
        self.assertFalse(trig.request_sweep(self.ws, "core-signed-in", "k-1", spawn=broken))
        self.assertFalse(trig.request_sweep(self.ws, "core-signed-in", "", spawn=broken), "no key, no sweep")

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
        (self.ws / "state" / "roster.json").write_text(json.dumps({"workers": {}}))
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

    def test_no_pool_installed_or_no_roster_is_no_sweep(self):
        self.assertFalse(trig.request_sweep(self.ws, "x", "k", repo=self.ws, spawn=self.spawn))
        (self.ws / "state" / "roster.json").unlink()
        self.assertFalse(trig.request_sweep(self.ws, "x", "k2", spawn=self.spawn))
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
