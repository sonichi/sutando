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
import json
import os
import pathlib
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
UNBLOCK = ("To unblock it, open its terminal (tmux session sutando-worker-1) and run "
           "/usage-credits to use extra credits, or /login to switch to another account.")
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


def _tick(out, pane, health="idle"):
    argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--session", SESSION,
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
            # A delivery typed into the limited seat: the old code read this as recovery.
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

    def test_other_gates_keep_their_wording(self):
        body = M.escalation_message("blocked-human", "d", "turn-rejected", "not logged in", SESSION)
        self.assertNotIn("/usage-credits", body)
        self.assertIn("Open the Runtime panel", body)


class PoolAndMonitorShareOneCard(unittest.TestCase):
    def test_the_pool_limit_card_is_the_monitors_card(self):
        spec_ = u.spec_from_file_location("pool_remedy", SCRIPTS / "pool_remedy.py")
        rem = sys.modules.get("pool_remedy") or u.module_from_spec(spec_)
        if "pool_remedy" not in sys.modules:
            sys.modules["pool_remedy"] = rem
            spec_.loader.exec_module(rem)
        wc, wi, ps = rem.wc, rem.wi, rem.ps
        wid = "7c54b230a8d94ea9b86f52d70134ac68"
        name = f"sutando-worker-{wid}"
        with tempfile.TemporaryDirectory() as td:
            ws = pathlib.Path(td)
            (ws / "state").mkdir()
            (ws / "state" / "roster.json").write_text(json.dumps(
                {"workers": {wid: {"state": "live", "label": "comm"}}}))
            wi.record_session(ws, wid, "s1", runtime="claude", relation=wi.RELATION_NEW)
            wi.start_incarnation(ws, wid, "s1", tmux_socket="/tmp/p.sock", tmux_session=name)
            m = HitlManager(HitlStore(ws / "state" / "hitl" / "requirements"))
            pane = ("  You've hit your session limit · resets 4pm\n"
                    "  ❯ 1. Stop and wait for limit to reset\n  Enter to confirm · Esc to cancel\n")

            class _Done:
                returncode, stderr = 0, ""
                stdout = pane

            out = wc.raise_card(ws, wid, ps.CARD_HUMAN, runner=lambda *a, **k: _Done(),
                                routed=lambda *a: False, manager=m)
            pool_card = m.get(out["hitl_id"])
            self.assertEqual(pool_card.guard, tui_gate.limit_guard(name))
            self.assertIn("/usage-credits to use extra credits, or /login to switch to another account",
                          pool_card.message)
            self.assertIn(f"tmux session {name}", pool_card.message)
            watched = M.escalate(m, "blocked-human", "d", "session-limit", pane, name,
                                 seat="worker comm", runtime="claude")
            self.assertEqual(watched.id, pool_card.id)
            self.assertEqual(len(m.active()), 1)


if __name__ == "__main__":
    unittest.main()
