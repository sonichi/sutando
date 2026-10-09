#!/usr/bin/env python3
"""Upgrade legacy supervision and HITL state through the real remedy sweep.

Only process I/O and unrelated watcher provisioning are stubbed. The pane
classifier, state reader/writer, decision ladder, card deduplication and cleanup
all run against a temporary workspace; no live worker is inspected or changed.
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills" / "worker-pool" / "scripts"))

import pool_remedy as rem  # noqa: E402

sup, ps, wc, wi = rem.sup, rem.ps, rem.wc, rem.wi
WORKERS = ("7c54b230a8d94ea9b86f52d70134ac68", "8d65c341b9ea4fbac97f63e81245bd79")
FOOTER = "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
LOGIN = f"❯ /startup\n  ⎿  Login expired · Please run /login\n\n❯ \n{FOOTER}\n"
IDLE = f"❯ \n{FOOTER}\n"
ERROR = f"  ⎿  API Error: 500 internal server error\n❯ \n{FOOTER}\n"


class LoginCardMigration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="pool-login-migration-")
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        (self.ws / "state").mkdir()
        self.now = 1000.0
        self.panes = {}
        self.calls = []
        self.add_worker(WORKERS[0])

    def add_worker(self, wid):
        path = self.ws / "state" / "roster.json"
        roster = json.loads(path.read_text()) if path.exists() else {"workers": {}}
        roster["workers"][wid] = {"state": "live", "runtime": "claude", "label": wid}
        path.write_text(json.dumps(roster))
        wi.record_session(self.ws, wid, "s1", runtime="claude", relation=wi.RELATION_NEW)
        wi.start_incarnation(self.ws, wid, "s1", tmux_socket=str(self.ws / "tmux.sock"),
                             tmux_session=wi.tmux_session_name(wid))
        self.panes[wid] = LOGIN

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        if "capture-pane" in argv:
            wid = argv[-1].removeprefix("=sutando-worker-").removesuffix(":0")
            return subprocess.CompletedProcess(argv, 0, self.panes[wid], "")
        if "has-session" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "inbox-holders" in argv:
            return subprocess.CompletedProcess(argv, 0, "4242 session\n", "")
        raise AssertionError(f"unexpected process operation: {argv}")

    def seed_legacy_state(self):
        sup.state_path(self.ws).write_text(json.dumps({
            "last_sample_at": self.now,
            "workers": {wid: {"wedge_first_detected_at": 400.0,
                               "wedge_consecutive": 3, "wedge_escalated": True}
                        for wid in self.panes},
        }))

    def seed_card(self, wid, which=ps.CARD_CAUSE, pane=LOGIN):
        previous = self.panes[wid]
        self.panes[wid] = pane
        try:
            out = wc.raise_card(self.ws, wid, which, runner=self.runner,
                                routed=lambda *_: False)
        finally:
            self.panes[wid] = previous
        return wc.manager_for(self.ws).get(out["hitl_id"])

    def sweep(self):
        self.now += 300.0
        real_tick, real_raise = sup.tick, wc.raise_card
        output = io.StringIO()
        with mock.patch.object(sup, "tick", side_effect=lambda ws, now, **kw:
                               real_tick(ws, now, runner=self.runner, **kw)), \
                mock.patch.object(wc, "raise_card", side_effect=lambda ws, wid, which, **kw:
                                  real_raise(ws, wid, which, runner=self.runner,
                                             routed=lambda *_: False)), \
                mock.patch.object(rem, "ensure_supervisors", return_value={}), \
                mock.patch.object(rem, "ensure_input_watches", return_value={}), \
                mock.patch.object(wc, "drive_escapes", return_value={}), \
                mock.patch.object(rem.time, "time", return_value=self.now), \
                mock.patch.object(rem.subprocess, "run",
                                  side_effect=AssertionError("unexpected subprocess")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(rem.main(["--workspace", str(self.ws), "--repo", str(REPO),
                                       "--sweep"]), 0)
        return json.loads(output.getvalue())

    def assert_login_cards(self, count):
        cards = wc.manager_for(self.ws).active()
        self.assertEqual(len(cards), count)
        for card in cards:
            self.assertIn("needs-login", card.subject["cause"])
            self.assertIn("Please run /login", card.message)
            self.assertEqual([action.id for action in card.actions], ["open_terminal"])
        return cards

    def test_legacy_login_card_survives_dedup_cleanup_and_subsequent_ticks(self):
        wid = WORKERS[0]
        old = self.seed_card(wid)
        self.seed_legacy_state()
        first = self.sweep()
        cards = self.assert_login_cards(1)
        self.assertEqual((cards[0].id, cards[0].revision), (old.id, old.revision))
        self.assertEqual(first["decisions"][wid], ps.CARD_LOGIN)
        self.assertEqual(first["cards"][wid]["hitl_id"], old.id)
        self.assertEqual(first["cards_closed"], [])
        for _ in range(2):
            self.assertEqual(self.sweep()["decisions"][wid], ps.NOTHING)
            self.assertEqual(self.assert_login_cards(1)[0].id, old.id)
        state = sup.load_state(self.ws).workers[wid]
        self.assertEqual((state.wedge_kind, state.wedge_escalated), ("login", True))

    def test_legacy_ack_without_a_card_does_not_silence_login(self):
        self.seed_legacy_state()
        self.sweep()
        card = self.assert_login_cards(1)[0]
        self.assertEqual(card.subject["wedge"], ps.CARD_LOGIN)
        self.sweep()
        self.assertEqual(self.assert_login_cards(1)[0].id, card.id)

    def test_an_unrelated_legacy_cause_is_replaced_not_preserved(self):
        old = self.seed_card(WORKERS[0], pane=ERROR)
        self.seed_legacy_state()
        result = self.sweep()
        card = self.assert_login_cards(1)[0]
        self.assertNotEqual(card.id, old.id)
        self.assertEqual(card.subject["wedge"], ps.CARD_LOGIN)
        self.assertEqual(result["cards_closed"], [old.id])

    def test_recovery_closes_the_compatible_legacy_card_and_reexpiry_creates_one(self):
        wid = WORKERS[0]
        old = self.seed_card(wid)
        self.seed_legacy_state()
        self.sweep()
        self.panes[wid] = IDLE
        self.assertEqual(self.sweep()["cards_closed"], [old.id])
        self.assert_login_cards(0)
        self.panes[wid] = LOGIN
        self.sweep()
        new = self.assert_login_cards(1)[0]
        self.assertNotEqual(new.id, old.id)
        self.sweep()
        self.assertEqual(self.assert_login_cards(1)[0].id, new.id)

    def test_migration_preserves_one_card_per_worker_and_existing_login_cards(self):
        self.add_worker(WORKERS[1])
        old = self.seed_card(WORKERS[0])
        current = self.seed_card(WORKERS[1], which=ps.CARD_LOGIN)
        self.seed_legacy_state()
        for _ in range(3):
            self.sweep()
            self.assertEqual({card.id for card in self.assert_login_cards(2)},
                             {old.id, current.id})

    def test_other_legacy_kinds_keep_their_acknowledgments(self):
        for pane in (ps.PANE_ABNORMAL, ps.PANE_WORKING, ps.PANE_GATE):
            with self.subTest(pane=pane):
                legacy = ps.SupervisionState(last_sample_at=1000.0, workers={"w":
                    ps.WorkerEvidence(wedge_first_detected_at=400.0, wedge_consecutive=3,
                                      wedge_escalated=True, last_pane_id="same")})
                obs = ps.Observation(beat=ps.LIVE, session_alive=True, watcher_held=True,
                                     work_outstanding=True, pane=pane, pane_id="same")
                state, decisions = ps.evaluate(legacy, {"w": obs}, 1300.0)
                self.assertEqual(decisions["w"], ps.NOTHING)
                self.assertTrue(state.workers["w"].wedge_escalated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
