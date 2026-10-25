#!/usr/bin/env python3
"""A usage limit reaches the owner once per episode, and the card says how to lift it.

The owner kept getting the same usage-limit card, and its notification, for one
ongoing limit on one seat. A limit repaints its screen (a refused delivery, a new
reset time, the menu after the banner), and each repaint keyed a new card; a
delivery typed into the limited seat read as recovery and closed the card, so the
next refusal opened another; and the reminder backoff re-alerted on top. Now:

- one card per seat while the limit stands, whatever the screen shows, across
  watcher restarts (each --once tick below is a fresh process on the same store);
- it closes only when a turn did work after the limit line, or no limit text is
  left; a limit after that is a new episode and a new card;
- the card is announced once: no reminder notices, and its edits are edits;
- the seat monitor's card and the pool sweep's card for the same limit are one card;
- the card names /usage-credits and /login in that seat's terminal.
"""
import importlib.util as u
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
SRC = REPO / "src"
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SRC))
spec = u.spec_from_file_location("ciw_limit_episode", SRC / "core-input-watch.py")
M = u.module_from_spec(spec)
spec.loader.exec_module(M)

from hitl import renotify, tui_gate  # noqa: E402
from hitl.manager import HitlManager, HitlStore  # noqa: E402

from hitl.projector import project  # noqa: E402
from hitl.schema import STATUS_RESOLVED  # noqa: E402

SESSION = "sutando-worker-1"
SEAT = "worker comm"
UNBLOCK = ("It continues when the limit resets. To unblock it sooner, open its terminal (tmux session "
           "sutando-worker-1) and run /usage-credits to use extra credits, /login to switch to another "
           "account, or /model to switch to a model with usage left.")
CODEX_UNBLOCK = ("It continues when the limit resets. To unblock it sooner, open its terminal (tmux session "
                 "sutando-worker-1) and run `codex login` to switch to another account.")
_FOOTER = ("────────\n❯ \n────────\n"
           "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents")


def _refused(task, resets="3pm"):
    return f"❯ {task}\n  ⎿  You've hit your usage limit · resets {resets}\n✻ Cooked for 1s\n"


_REAL_TURN = ("❯ task-9\n● Done: the summary is in the room.\n"
              "✻ Worked for 2m 14s · done 4:05 PM\n")


class _RH:
    TMUX_SOCKET = SESSION = None

    def __init__(self, health):
        self.health = health

    def derive(self):
        return {"health": self.health}


def _tick(out, pane, health="idle", session=SESSION):
    argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--session", session,
            "--out", out, "--once", "--stable", "1", "--seat", SEAT]
    with patch.object(M, "capture_styled", lambda s, sess: pane), \
            patch.object(M, "_load_runtime_health", lambda: _RH(health)), \
            patch.object(M, "gateway_alive", lambda *a: True), \
            patch.object(M, "_ensure_tmux_on_path", lambda: None), \
            patch.object(M, "session_runtime", lambda *a: "claude"), \
            patch.object(sys, "argv", argv):
        M.main()
    with open(out) as f:
        return json.load(f)


class OneCardPerEpisode(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.td.name, "state", f"core-supervisor.{SESSION}.json")
        os.makedirs(os.path.dirname(self.out))

    def tearDown(self):
        self.td.cleanup()

    def _active(self):
        # A fresh manager per read: what a restarted watcher would load from disk.
        return M._hitl_manager(self.out).active()

    def test_repaints_and_refused_deliveries_keep_one_card_until_work_runs(self):
        sig = _tick(self.out, _refused("task-1") + _FOOTER)
        self.assertEqual((sig["state"], sig["kind"]), ("blocked-human", "turn-rejected"))
        [card] = self._active()
        self.assertEqual(card.guard, tui_gate.limit_guard(SESSION))
        self.assertIs(card.subject["remind"], False)

        frames = [
            # A delivery typed into the limited seat.
            _refused("task-1") + "❯ task-2\n✻ Pondering… (2s · esc to interrupt)\n" + _FOOTER,
            # Its refusal, with a reset time the CLI repainted.
            _refused("task-1") + _refused("task-2", resets="3:59pm") + _FOOTER,
            # The limit menu after it.
            _refused("task-2") + "  You've hit your session limit · resets 4pm\n"
                                 "  ❯ 1. Stop and wait for limit to reset\n"
                                 "    2. Switch to extra usage\n  Enter to confirm · Esc to cancel\n",
        ]
        for pane in frames:
            _tick(self.out, pane)
            self.assertEqual([r.id for r in self._active()], [card.id], pane)

        sig = _tick(self.out, _refused("task-2") + _REAL_TURN + _FOOTER)
        self.assertEqual(sig["state"], "idle-ready")
        self.assertEqual(self._active(), [])
        self.assertEqual(M._hitl_manager(self.out).get(card.id).status, STATUS_RESOLVED)

        _tick(self.out, _REAL_TURN + _refused("task-10") + _FOOTER)
        [again] = self._active()
        self.assertNotEqual(again.id, card.id, "a limit after it cleared is a new episode")

    def test_a_pane_with_no_limit_text_left_ends_the_episode(self):
        _tick(self.out, _refused("task-1") + _FOOTER)
        self.assertEqual(len(self._active()), 1)
        _tick(self.out, _FOOTER)
        self.assertEqual(self._active(), [])

    def test_other_gates_still_key_on_their_prompt(self):
        m = HitlManager(HitlStore(pathlib.Path(self.td.name) / "h"))
        a = M.escalate(m, "blocked-human", "d", "selection", "Pick A:", SESSION)
        b = M.escalate(m, "blocked-human", "d", "selection", "Pick B:", SESSION)
        self.assertNotEqual(a.id, b.id)
        self.assertNotIn("remind", a.subject)


class WhatIsALimit(unittest.TestCase):
    def test_the_limit_menu_reads_as_a_limit_though_classified_selection(self):
        from delivery.pane_gate import is_usage_limit
        menu = ("  You've hit your session limit · resets 4pm\n\n"
                "  ❯ 1. Stop and wait for limit to reset\n    2. Switch to extra usage\n")
        self.assertTrue(is_usage_limit("selection", menu))

    def test_a_dialog_under_a_refused_turn_is_that_dialog_not_the_limit(self):
        from delivery.pane_gate import is_usage_limit
        pane = (_refused("task-1") + "  Do you want to proceed?\n  ❯ 1. Yes\n    2. No\n")
        self.assertFalse(is_usage_limit("selection", pane))
        self.assertFalse(is_usage_limit("turn-rejected", "not logged in"))


class AnnouncedOnce(unittest.TestCase):
    def _card(self, m, episode):
        req = tui_gate.requirement_for("blocked-human", "turn-rejected",
                                       "You've hit your usage limit", SESSION, "d", "body",
                                       episode=episode)
        return m.create(req)

    def test_a_limit_card_sends_no_reminders_and_its_edits_are_edits(self):
        with tempfile.TemporaryDirectory() as td:
            m = HitlManager(HitlStore(pathlib.Path(td)))
            card = self._card(m, tui_gate.LIMIT_EPISODE)
            sent, now = [], [1_000_000.0]

            def send(p):
                sent.append(p)
                return {"ok": True, "event_id": f"$e{len(sent)}"}

            project(m, send, "!room", clock=lambda: now[0])
            self.assertEqual([p["op"] for p in sent], ["message"])
            for _ in range(5):
                now[0] += 7 * 3600
                project(m, send, "!room", clock=lambda: now[0])
            self.assertEqual(len(sent), 1, "a limit card re-alerted")
            with m.store.locked():
                cur = m.get(card.id)
                cur.message += "\nmore"
                cur.revision += 1
                m.store.save(cur)
            project(m, send, "!room", clock=lambda: now[0])
            self.assertEqual([p["op"] for p in sent], ["message", "edit"])

    def test_other_blocking_cards_keep_their_reminders(self):
        with tempfile.TemporaryDirectory() as td:
            m = HitlManager(HitlStore(pathlib.Path(td)))
            self._card(m, None)
            sent = []
            send = lambda p: (sent.append(p), {"ok": True, "event_id": f"$e{len(sent)}"})[1]  # noqa: E731
            project(m, send, "!room", clock=lambda: 1_000_000.0)
            project(m, send, "!room", clock=lambda: 1_000_000.0 + renotify.REMIND_INTERVALS_S[0])
            self.assertEqual(len(sent), 2)


class UnblockGuidance(unittest.TestCase):
    LIMITS = [
        ("usage-limit", "Usage limit reached · resets 3pm\nContinuing automatically at 3pm"),
        ("session-limit", "You've hit your session limit · resets 4pm\n❯ 1. Stop and wait"),
        ("turn-rejected", "You've hit your weekly limit · resets Oct 20"),
    ]

    def test_a_limit_card_says_how_to_unblock_it(self):
        for kind, prompt in self.LIMITS:
            with self.subTest(kind=kind):
                body = M.escalation_message("blocked-human", f"awaiting user: {kind}", kind,
                                            prompt, SESSION)
                self.assertEqual(body.splitlines()[-1], UNBLOCK)
                self.assertNotIn("Nothing else I do can clear this one", body)

    def test_the_raised_card_carries_it(self):
        with tempfile.TemporaryDirectory() as td:
            m = HitlManager(HitlStore(pathlib.Path(td)))
            req = M.escalate(m, "blocked-human", "d", "turn-rejected",
                             "You've hit your usage limit", SESSION, seat=SEAT)
            self.assertIn(UNBLOCK, req.message)

    def test_a_codex_seat_gets_codex_steps(self):
        body = M.escalation_message("blocked-human", "d", "turn-rejected",
                                    "You've hit your usage limit", SESSION, runtime="codex")
        self.assertEqual(body.splitlines()[-1], CODEX_UNBLOCK)
        self.assertNotIn("/usage-credits", body)
        with tempfile.TemporaryDirectory() as td:
            m = HitlManager(HitlStore(pathlib.Path(td)))
            req = M.escalate(m, "blocked-human", "d", "turn-rejected",
                             "You've hit your usage limit", SESSION, seat=SEAT, runtime="codex")
            self.assertIn(CODEX_UNBLOCK, req.message)

    def test_other_gates_keep_their_wording(self):
        body = M.escalation_message("blocked-human", "d", "turn-rejected", "not logged in", SESSION)
        self.assertNotIn("/usage-credits", body)
        self.assertIn("Open the Runtime panel", body)


def _pool():
    spec_ = u.spec_from_file_location("pool_remedy", SCRIPTS / "pool_remedy.py")
    rem = sys.modules.get("pool_remedy") or u.module_from_spec(spec_)
    if "pool_remedy" not in sys.modules:
        sys.modules["pool_remedy"] = rem
        spec_.loader.exec_module(rem)
    return rem


def _shows(pane):
    class _Done:
        returncode, stderr, stdout = 0, "", pane
    return lambda *a, **k: _Done()


WID = "7c54b230a8d94ea9b86f52d70134ac68"
POOL_SESSION = f"sutando-worker-{WID}"
_MENU = ("  You've hit your session limit · resets 4pm\n"
         "  ❯ 1. Stop and wait for limit to reset\n  Enter to confirm · Esc to cancel\n")
# "limit reached" escapes the one-click deny list, so this menu parses as a two-option choice.
_WEEKLY_MENU = ("  Weekly limit reached · resets Oct 20, 9am\n\n"
                "  ❯ 1. Stop and wait for limit to reset\n"
                "    2. Switch to extra usage\n  Enter to confirm · Esc to cancel\n")


class _PoolSeat(unittest.TestCase):
    def setUp(self):
        rem = _pool()
        self.wc, wi, self.ps = rem.wc, rem.wi, rem.ps
        self.td = tempfile.TemporaryDirectory()
        self.ws = pathlib.Path(self.td.name)
        (self.ws / "state").mkdir()
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"workers": {WID: {"state": "live", "label": "comm"}}}))
        wi.record_session(self.ws, WID, "s1", runtime="claude", relation=wi.RELATION_NEW)
        wi.start_incarnation(self.ws, WID, "s1", tmux_socket="/tmp/p.sock", tmux_session=POOL_SESSION)
        self.m = HitlManager(HitlStore(self.ws / "state" / "hitl" / "requirements"))

    def tearDown(self):
        self.td.cleanup()

    def _pool_card(self, pane=_MENU):
        out = self.wc.raise_card(self.ws, WID, self.ps.CARD_HUMAN, runner=_shows(pane),
                                 routed=lambda *a: False, manager=self.m)
        return self.m.get(out["hitl_id"])

    def _sweep(self, pane):
        return self.wc.resolve_cleared(self.ws, {WID}, manager=self.m, runner=_shows(pane))


class PoolAndMonitorShareOneCard(_PoolSeat):
    def test_the_pool_limit_card_is_the_monitors_card(self):
        m, name = self.m, POOL_SESSION
        pool_card = self._pool_card()
        self.assertEqual(pool_card.guard, tui_gate.limit_guard(name))
        self.assertIn("/usage-credits to use extra credits, /login to switch to another account",
                      pool_card.message)
        self.assertIn(f"tmux session {name}", pool_card.message)
        watched = M.escalate(m, "blocked-human", "d", "session-limit", _MENU, name,
                             seat="worker comm", runtime="claude")
        self.assertEqual(watched.id, pool_card.id)
        self.assertEqual(len(m.active()), 1)

    def test_a_limit_menu_with_choices_is_still_the_one_card(self):
        for pool_first in (True, False):
            with self.subTest(pool_first=pool_first):
                for r in self.m.active():
                    self.m.resolve(r.id)
                first = self._pool_card(_WEEKLY_MENU) if pool_first else None
                watched = M.escalate(self.m, "blocked-human", "d", "selection", _WEEKLY_MENU,
                                     POOL_SESSION, seat="worker comm", runtime="claude")
                self.assertEqual(watched.kind, tui_gate.FALLBACK_KIND)
                self.assertEqual([a.kind for a in watched.actions], ["open_terminal"])
                pooled = first or self._pool_card(_WEEKLY_MENU)
                self.assertEqual(watched.id, pooled.id)
                self.assertEqual(len(self.m.active()), 1)


class PoolSweepKeepsTheEpisode(_PoolSeat):
    """The pool's sweep closes a seat's card once the seat is not wedged; a limit card
    closes by the seat monitor's rule instead, so an unwedged but limited seat keeps it."""

    def test_the_sweep_keeps_a_limit_card_while_the_limit_stands(self):
        card = self._pool_card()
        watched = M.escalate(self.m, "blocked-human", "d", "turn-rejected", _refused("task-1"),
                             POOL_SESSION, runtime="claude")
        self.assertEqual(watched.id, card.id)
        for pane in (_refused("task-1") + "❯ task-2\n✻ Pondering… (2s · esc to interrupt)\n" + _FOOTER,
                     _refused("task-1") + _refused("task-2", resets="3:59pm") + _FOOTER,
                     _MENU):
            self.assertEqual(self._sweep(pane), [], pane)
            self.assertEqual([r.id for r in self.m.active()], [card.id])
        again = self._pool_card(_refused("task-2") + _MENU)
        self.assertEqual(again.id, card.id)

    def test_the_sweep_closes_it_once_work_ran_and_a_new_limit_is_a_new_card(self):
        card = self._pool_card()
        self.assertEqual(self._sweep(_refused("task-2") + _REAL_TURN + _FOOTER), [card.id])
        self.assertEqual(self.m.get(card.id).status, STATUS_RESOLVED)
        self.assertNotEqual(self._pool_card().id, card.id)

    def test_an_unread_pane_keeps_it(self):
        card = self._pool_card()
        self.assertEqual(self._sweep(""), [])
        self.assertEqual([r.id for r in self.m.active()], [card.id])

    def test_other_pool_cards_still_close_on_the_sweep(self):
        card = self._pool_card("  Do you want to proceed?\n  ❯ 1. Yes\n    2. No\n"
                               "  Enter to confirm · Esc to cancel\n")
        self.assertNotIn("episode", card.subject)
        self.assertEqual(self._sweep(_refused("task-1") + _FOOTER), [card.id])


_POOL_FOOTER = "❯ \n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
# A worker pane captured live: the CLI keeps this banner drawn under the composer after turns finish.
_BANNER = ("──────── seat-1 ─\n❯ \n────────\n"
           "  ⚠ Usage limit reached · limit resets Oct 13 at 11pm\n"
           "    Continuing automatically at Oct 13 at 11pm · esc to cancel\n"
           "  ⏵⏵ bypass permissions on · 1 monitor · ← for agents · ↓ to manage")
_EARLIER = ("⏺ The task watcher on my inbox hit its 30-minute limit, so I restarted it.\n"
            "✻ Churned for 27s · done 1:55 PM · 1 monitor still running\n")


class PersistedSweepKeepsTheEpisode(unittest.TestCase):
    """The real `pool_remedy --sweep` (supervision writer, card raise, resolve_cleared) and the
    real seat monitor tick on one on-disk store, projected; either producer raises first."""

    def setUp(self):
        rem = self.rem = _pool()
        self.sup, self.wc, wi = rem.sup, rem.wc, rem.wi
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.ws = pathlib.Path(self.td.name)
        (self.ws / "state").mkdir()
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"workers": {WID: {"state": "live", "runtime": "claude", "label": "comm"}}}))
        wi.record_session(self.ws, WID, "s1", runtime="claude", relation=wi.RELATION_NEW)
        wi.start_incarnation(self.ws, WID, "s1", tmux_socket=str(self.ws / "tmux.sock"),
                             tmux_session=wi.tmux_session_name(WID))
        self.inbox = rem.pd.deliveries_dir(self.ws, WID)
        self.inbox.mkdir(parents=True)
        self.out = str(self.ws / "state" / f"core-supervisor.{POOL_SESSION}.json")
        self.now, self.pane, self.sent, self.watching = 1000.0, _POOL_FOOTER, [], False

    def runner(self, argv, **kwargs):
        if "capture-pane" in argv:
            return subprocess.CompletedProcess(argv, 0, self.pane, "")
        if "has-session" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "inbox-holders" in argv:
            return subprocess.CompletedProcess(argv, 0, "4242 session\n", "")
        raise AssertionError(f"unexpected process operation: {argv}")

    def owe(self, task):
        for f in self.inbox.glob("task-*.txt"):
            f.unlink()
        if task:
            (self.inbox / f"{task}.txt").write_text("pending")

    def manager(self):
        return self.wc.manager_for(self.ws)  # fresh from disk, as a restarted producer loads it

    def watch(self):
        self.watching = True
        _tick(self.out, self.pane, session=POOL_SESSION)

    def sweep(self):
        rem, sup, wc = self.rem, self.sup, self.wc
        self.now += 300.0
        real_tick, real_raise = sup.tick, wc.raise_card
        with patch.object(sup, "tick", side_effect=lambda ws, now, **kw:
                          real_tick(ws, now, runner=self.runner, **kw)), \
                patch.object(wc, "raise_card", side_effect=lambda ws, wid, which, **kw:
                             real_raise(ws, wid, which, runner=self.runner, routed=lambda *_: False)), \
                patch.object(rem, "ensure_supervisors", return_value={}), \
                patch.object(rem, "ensure_input_watches", return_value={}), \
                patch.object(wc, "drive_escapes", return_value={}), \
                patch.object(rem.time, "time", return_value=self.now), \
                patch.object(rem.subprocess, "run", side_effect=AssertionError("unexpected subprocess")), \
                patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(rem.main(["--workspace", str(self.ws), "--repo", str(REPO), "--sweep"],
                                      runner=self.runner), 0)
        if self.watching:
            _tick(self.out, self.pane, session=POOL_SESSION)
        project(self.manager(), lambda p: (self.sent.append(p), {"ok": True, "event_id": "$e"})[1],
                "!room", clock=lambda: self.now)

    def card_ids(self):
        return [r.id for r in self.manager().active()]

    def alerts(self):
        return sum(p.get("op") == "message" and "needs your attention" in p["body"] for p in self.sent)

    def limit_begins(self, pane, task, seat_first):
        self.pane = pane
        self.owe(task)
        if seat_first:
            self.watch()
        for _ in range(6):
            self.sweep()
        self.watch()
        [card] = self.manager().active()
        self.assertTrue(tui_gate.limit_card(card))
        self.assertEqual(card.subject["source"], "tui" if seat_first else self.wc.SOURCE)
        self.assertEqual(self.alerts(), 1)
        return card.id

    def test_refused_deliveries_keep_the_card_until_work_runs_then_a_new_limit_is_new(self):
        for seat_first in (False, True):
            with self.subTest(seat_first=seat_first):
                self.setUp()
                card = self.limit_begins(_refused("task-1") + _POOL_FOOTER, "task-1", seat_first)
                for pane in (_refused("task-1") + "❯ task-2\n✻ Pondering… (2s · esc to interrupt)\n",
                             _refused("task-1") + _refused("task-2", resets="3:59pm") + _POOL_FOOTER,
                             _refused("task-2") + _MENU):
                    self.pane = pane
                    self.owe("task-2")
                    for _ in range(2):
                        self.sweep()
                        self.assertEqual(self.card_ids(), [card], pane)
                        self.assertEqual(self.alerts(), 1)
                self.pane = _refused("task-2") + _REAL_TURN + _POOL_FOOTER
                self.owe(None)
                self.sweep()
                self.assertEqual(self.card_ids(), [], "real work did not clear")
                self.assertEqual(self.manager().get(card).status, STATUS_RESOLVED)
                self.pane = _REAL_TURN + _refused("task-10") + _POOL_FOOTER
                self.owe("task-10")
                for _ in range(6):
                    self.sweep()
                [again] = self.card_ids()
                self.assertNotEqual(again, card, "a limit after recovery is a new episode")
                self.assertEqual(self.alerts(), 2)

    def test_a_banner_left_drawn_after_a_finished_turn_ends_the_episode(self):
        # A worker's monitor leaves banners to the pool, so only the pool raises this card.
        later = "⏺ Done: the summary is in the room.\n✻ Brewed for 3m 2s · done 1:58 PM\n"
        card = self.limit_begins(_EARLIER + _BANNER, "task-1", seat_first=False)
        for pane in (_EARLIER + _BANNER, _EARLIER + "❯ task-2\n✻ Pondering… (2s · esc to interrupt)\n" + _BANNER):
            self.pane = pane
            self.owe("task-2")
            self.sweep()
            self.assertEqual(self.card_ids(), [card], "a standing banner closed the card")
        self.pane = _EARLIER + later + _BANNER
        self.owe(None)
        self.sweep()
        self.assertEqual(self.card_ids(), [], "a recovered seat kept its limit card")
        self.pane = _EARLIER + later + _BANNER + "\n" + _refused("task-3") + _POOL_FOOTER
        self.owe("task-3")
        for _ in range(6):
            self.sweep()
        [again] = self.card_ids()
        self.assertNotEqual(again, card, "the next limit folded into the old card")
        self.assertEqual(self.alerts(), 2)

if __name__ == "__main__":
    unittest.main()
