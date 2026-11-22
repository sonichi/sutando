#!/usr/bin/env python3
"""A stuck seat's card shows the options the runtime itself offers, by its own names, and only
those can be chosen: the runtime's model list (read off its own picker, on the owner's Refresh
options tap), and the on-screen menu's choices, delivered into the same menu as keystrokes.

Run: python3 tests/runtime-models.test.py
"""
import importlib.util as u
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))
spec = u.spec_from_file_location("ciw_runtime_models", SRC / "core-input-watch.py")
M = u.module_from_spec(spec)
spec.loader.exec_module(M)

import core_limit_hold
import core_recovery_state
import recovery_actions as RA
import runtime_models as RM
from hitl import recovery, tui_gate
from hitl.manager import HitlManager, HitlStore
from hitl.replies import REPLY_FIELD, HitlReplyHandler
from hitl.schema import STATUS_PENDING, STATUS_RESOLVED

PICKER = (REPO / "tests" / "fixtures" / "pane-claude-model-picker.txt").read_text()
MENU = (REPO / "tests" / "fixtures" / "core_recovery" / "seat-rate-limit-options.txt").read_text().rstrip("\n")
CODEX_PICKER = ("  Select Model and Effort\n  1. gpt-6-sol  Frontier\n› 4. gpt-5.5 (current)  Proven model\n\n"
                "  GPT-6-Sol ultra · ~/x")
LIMIT = "  ⎿  You've hit your usage limit · resets 3pm\n✻ Cooked for 1s"
CORE, SEAT_SESSION, SEAT = "sutando-core", "sutando-worker-b35461a4", "worker Mira (b35461a4)"
OWNER, DM = "@owner:s.space", "!dm:s.space"


class Parse(unittest.TestCase):
    def test_the_claude_picker_rows_are_the_runtimes_own_list(self):
        rows = RM.parse_picker(PICKER, "claude")
        self.assertEqual([(r["key"], r["label"], r["arg"]) for r in rows], [
            ("default-recommended", "Default (Opus 5.5)", "default"),
            ("opus-5-5", "Opus 5.5", "claude-opus-5-5"),
            ("sonnet-5-1m-context", "Sonnet 5 (1M context)", "claude-sonnet-5[1m]"),
            ("fable-5-1", "Fable 5.1", "claude-fable-5-1"),
            ("haiku", "Haiku 4.5", "claude-haiku-4-5")])

    def test_the_codex_picker_rows_carry_the_current_mark(self):
        rows = RM.parse_picker(CODEX_PICKER, "codex")
        self.assertEqual([(r["label"], r["current"], r["arg"]) for r in rows],
                         [("gpt-6-sol", False, None), ("gpt-5.5", True, None)])

    def test_no_picker_is_no_list(self):
        for pane in (MENU, LIMIT, "", None):
            self.assertEqual(RM.parse_picker(pane, "claude"), [])

    def test_offer_and_summary_mark_the_model_in_use_and_the_capped_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            RM.record(ws, "claude", RM.parse_picker(PICKER, "claude"))
            self.assertEqual([e["label"] for e in RM.offer(ws, "claude", "claude-opus-5-5", "fable")],
                             ["Sonnet 5 (1M context)", "Haiku 4.5"])
            self.assertEqual(RM.summary(ws, "claude", "claude-opus-5-5", "fable"),
                             "This runtime offers: Default (Opus 5.5) · Opus 5.5 (in use) · Sonnet 5 (1M context) · "
                             "Fable 5.1 (hit its limit) · Haiku 4.5.")


class Probe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def step(self, pane, state="blocked-human", refused=True, seat=False, runtime="claude"):
        return RM.probe_step(self.ws, CORE, runtime, seat, state, refused, pane,
                             lambda k: self.sent.append(k) or True)

    def test_nothing_is_typed_unless_the_owner_asked(self):
        self.assertEqual(self.step(LIMIT), "")
        self.assertEqual(self.sent, [], "the stuck pane is never typed into on the monitor's own initiative")

    def test_on_the_owners_request_the_picker_is_opened_read_and_closed_once(self):
        RM.request_probe(self.ws, CORE)
        self.assertEqual(self.step(LIMIT), "probing")
        self.assertEqual(self.sent, [("/model", "Enter")])
        self.assertEqual(self.step(PICKER), "ours")
        self.assertEqual(self.sent[-1], "Escape", "nothing is chosen: the picker is dismissed")
        self.assertEqual(len(RM.entries(self.ws, "claude")), 5)
        self.assertEqual(self.step(LIMIT), "")
        self.assertEqual(len(self.sent), 2, "one read per request")

    def test_a_picker_someone_else_opened_is_read_and_left_alone(self):
        self.assertEqual(self.step(PICKER), "read")
        self.assertEqual(self.sent, [])
        self.assertEqual(len(RM.entries(self.ws, "claude")), 5)

    def test_a_seat_a_codex_core_or_a_core_that_is_not_idle_at_its_limit_is_never_probed(self):
        for kw in ({"seat": True}, {"runtime": "codex"}, {"refused": False}, {"state": "running"}):
            with self.subTest(**kw):
                RM.request_probe(self.ws, CORE)
                self.assertEqual(self.step(LIMIT, **kw), "")
                self.assertEqual(self.sent, [])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        for d in ("state", "tasks", "results"):
            (self.ws / d).mkdir()
        self.mgr = HitlManager(HitlStore(self.ws / "state" / "hitl" / "requirements"))
        self.handler = HitlReplyHandler(self.mgr, OWNER, workspace=self.ws, log=lambda m: None)
        for p in (mock.patch.object(RA.shutil, "which", return_value=None),
                  mock.patch.object(core_limit_hold, "launch_identity",
                                    lambda ws, config_dir=None: ("claude-opus-5-5", None))):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def card(self, kind="usage-limit", prompt=LIMIT, seat=None):
        return M.escalate(self.mgr, "blocked-human", f"awaiting user: {kind}", kind, prompt,
                          SEAT_SESSION if seat else CORE, seat=seat, runtime="claude", workspace=self.ws)

    def click(self, req, aid):
        self.handler.offer({"type": "message.created", "event_id": "$c", "actor_id": OWNER, "room_id": DM,
                            "content": {REPLY_FIELD: {"hitl_id": req.id, "expected_revision": req.revision,
                                                      "action_id": aid, "guard": req.guard}}})


class Menu(Base):
    """The live /rate-limit-options menu: its choices, in its words, delivered into that menu only."""

    def test_the_card_options_are_the_menus_choices_minus_the_purchase(self):
        options, _caret = tui_gate.parse_options(MENU)
        self.assertEqual(options[2], "Add funds to continue with usage credits")
        req = self.card(kind="selection", prompt=MENU, seat=SEAT)
        shown = [a.label for a in req.actions if a.kind == "select"]
        self.assertEqual(shown, options[:2], "displayed == the runtime's menu, purchase left off")
        self.assertEqual(core_recovery_state.derive(
            {"state": "blocked-human", "kind": "selection", "prompt": MENU}, None, None, None, None,
            now=time.time(), runtime="claude")["menu"], shown, "the record publishes the same list")

    def test_a_tap_delivers_that_choice_and_a_changed_pane_refuses(self):
        req = self.card(kind="selection", prompt=MENU, seat=SEAT)
        self.click(req, "opt2")
        sent = []
        M.drive_escalations(self.mgr, SEAT_SESSION, MENU, "blocked-human", lambda k: sent.append(k) or True)
        self.assertEqual(sent, ["Down", "Enter"], "the caret on row 1 moves to row 2, then confirms")
        other = MENU.replace("Nov 15 at 6am", "Nov 16 at 6am")
        self.assertIsNone(tui_gate.keys_for(self.mgr.get(req.id), other, "blocked-human"),
                          "never typed into a screen that changed since the tap")

    def test_a_purchase_row_is_never_deliverable(self):
        req = self.card(kind="selection", prompt=MENU, seat=SEAT)
        self.assertNotIn(2, (req.subject or {}).get("option_for_action", {}).values())


class Refresh(Base):
    def test_refresh_closes_the_card_and_the_next_card_carries_the_runtimes_list(self):
        req = self.card()
        self.assertFalse([a for a in req.actions if a.id.startswith("switch_model:")], "no list read yet")
        self.assertIn("Tap Refresh options", req.message)
        self.click(req, recovery.REFRESH_OPTIONS)
        target = RA.Target(self.ws, "/tmp/x.sock", CORE)
        [receipt] = RA.run_pending(self.mgr, target)
        self.assertEqual(receipt["outcome"], "done")
        self.assertEqual(self.mgr.get(req.id).status, STATUS_RESOLVED, "the owner asked: this card gives way")
        sent = []
        send = lambda k: sent.append(k) or True  # noqa: E731
        self.assertEqual(RM.probe_step(self.ws, CORE, "claude", False, "blocked-human", True, LIMIT, send), "probing")
        fresh = self.card()
        self.assertNotEqual(fresh.id, req.id)
        self.assertEqual(RM.probe_step(self.ws, CORE, "claude", False, "blocked-human", True, PICKER, send), "ours")
        RA.refresh_limit_card(self.mgr, self.ws, CORE, "claude")
        card = self.mgr.get(fresh.id)
        self.assertEqual(card.status, STATUS_PENDING)
        self.assertEqual([a.label for a in card.actions if a.id.startswith("switch_model:")],
                         ["Switch to Sonnet 5 (1M context)", "Switch to Fable 5.1", "Switch to Haiku 4.5"])
        self.assertIn("Opus 5.5 (in use)", card.message)
        self.assertEqual(sent, [("/model", "Enter"), "Escape"])

    def test_the_monitor_tick_that_reads_its_own_picker_raises_no_picker_card(self):
        RM.request_probe(self.ws, CORE)
        pending = json.loads(RM._probe_path(self.ws, CORE).read_text())
        RM.write_status(RM._probe_path(self.ws, CORE).name, dict(pending, sent_at=time.time()), workspace=self.ws)
        out = self.ws / "state" / "core-supervisor.json"

        class _RH:
            TMUX_SOCKET = SESSION = None

            def derive(self):
                return {"health": "idle", "signals": {"process": True}}
        sent = []
        argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--session", CORE, "--out", str(out),
                "--once", "--stable", "1"]
        with mock.patch.object(M, "capture_styled", lambda s, sess: PICKER), \
                mock.patch.object(M, "_load_runtime_health", lambda: _RH()), \
                mock.patch.object(M, "gateway_alive", lambda *a: True), \
                mock.patch.object(M, "_ensure_tmux_on_path", lambda: None), \
                mock.patch.object(M, "session_runtime", lambda *a: "claude"), \
                mock.patch.object(M, "_hitl_manager", lambda out_path: self.mgr), \
                mock.patch.object(M, "send_keys", lambda s, sess, k: sent.append(k) or True), \
                mock.patch.object(sys, "argv", argv):
            M.main()
        self.assertEqual(sent, ["Escape"])
        self.assertEqual(self.mgr.active(), [], "the picker the monitor opened is no gate to card")
        self.assertEqual(len(RM.entries(self.ws, "claude")), 5)


if __name__ == "__main__":
    unittest.main()
