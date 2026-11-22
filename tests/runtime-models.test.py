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


class ProbeOwnership(unittest.TestCase):
    """A Refresh options probe owns one picker, briefly: never a later one the owner opens."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.sent, self.escape_lands = [], True

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, keys):
        self.sent.append(keys)
        return self.escape_lands if keys == "Escape" else True

    def step(self, pane, now, seat=False):
        return RM.probe_step(self.ws, CORE, "claude", seat, "blocked-human", True, pane, self.send, now=now)

    def test_a_missing_picker_followed_by_an_owner_opened_one_is_read_not_dismissed(self):
        RM.request_probe(self.ws, CORE, now=1000.0)
        self.assertEqual(self.step(LIMIT, 1001.0), "probing")
        self.assertEqual(self.step(LIMIT, 1003.0), "", "the picker never came up")
        later = 1001.0 + RM.PROBE_TTL_S + 1
        self.assertEqual(self.step(PICKER, later), "read", "the owner's own picker is theirs")
        self.assertNotIn("Escape", self.sent)
        self.assertFalse(RM._probe_path(self.ws, CORE).exists(), "the abandoned probe was cleared")

    def test_a_request_nobody_acted_on_expires(self):
        RM.request_probe(self.ws, CORE, now=1000.0)
        self.assertEqual(self.step(LIMIT, 1000.0 + RM.REQUEST_TTL_S + 1), "")
        self.assertEqual(self.sent, [])
        self.assertFalse(RM._probe_path(self.ws, CORE).exists())

    def test_a_failed_escape_is_not_recorded_as_dismissed(self):
        RM.request_probe(self.ws, CORE, now=1000.0)
        self.step(LIMIT, 1001.0)
        self.escape_lands = False
        self.assertEqual(self.step(PICKER, 1002.0), "ours")
        self.assertTrue(RM._probe_path(self.ws, CORE).exists(), "not dismissed: the key did not land")
        self.escape_lands = True
        self.assertEqual(self.step(PICKER, 1003.0), "ours")
        self.assertEqual(self.sent.count("Escape"), 2)
        self.assertFalse(RM._probe_path(self.ws, CORE).exists(), "dismissed once the key landed")

    def test_a_seats_pane_never_writes_the_cores_list(self):
        self.assertEqual(self.step(PICKER, 1000.0, seat=True), "")
        self.assertEqual(RM.entries(self.ws, "claude"), [])


class OpaqueTokens(unittest.TestCase):
    def test_each_read_issues_new_tokens_so_a_tap_on_an_older_list_runs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            rows = RM.parse_picker(PICKER, "claude")
            RM.record(ws, "claude", rows, now=1.0)
            old = [e["key"] for e in RM.entries(ws, "claude")]
            RM.record(ws, "claude", rows, now=2.0)
            new = [e["key"] for e in RM.entries(ws, "claude")]
            self.assertTrue(all(k.startswith("o") and len(k) == 1 + RM.TOKEN_HEX for k in old + new))
            self.assertFalse(set(old) & set(new), "the same list read again gets new tokens")
            self.assertIsNone(RM.entry(ws, "claude", old[0]))
            labels = {e["label"] for e in RM.entries(ws, "claude")}
            self.assertFalse(labels & set(new), "a token is never a label")

    def test_a_racing_writer_reads_the_key_the_first_one_wrote(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            path = ws / "state" / "recovery" / "option-key"
            real_link = RM.os.link

            def lose_the_race(src, dst):
                real_link(src, dst)  # the other writer got there first
                raise FileExistsError(dst)
            with mock.patch.object(RM.os, "link", lose_the_race):
                got = RM._secret(ws)
            self.assertEqual(got, path.read_bytes())
            RM._drop(ws / "no-such-file")  # dropping what is already gone is quiet

    def test_the_host_key_is_private_and_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            first = RM._secret(ws)
            path = ws / "state" / "recovery" / "option-key"
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            self.assertEqual(RM._secret(ws), first)


class Edges(unittest.TestCase):
    def test_a_row_that_names_no_model_has_nothing_to_send(self):
        rows = RM.parse_picker(" Select model\n   1. Custom thing\n\n Enter to confirm · Esc to cancel\n", "claude")
        self.assertEqual([(r["label"], r["arg"]) for r in rows], [("Custom thing", None)])

    def test_an_unreadable_or_foreign_record_is_no_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "state").mkdir()
            path = ws / "state" / "runtime-models.claude.json"
            for body in ("{", json.dumps({"v": 2, "entries": []}), json.dumps([1])):
                path.write_text(body)
                self.assertIsNone(RM.read(ws, "claude"), body)
            self.assertIsNone(RM.read(ws, "agy"))

    def test_a_row_without_a_model_matches_only_its_own_key(self):
        self.assertTrue(RM._same({"key": "custom", "model": None}, "custom"))
        self.assertFalse(RM._same({"key": "custom", "model": None}, "opus"))

    def test_a_broken_request_file_is_no_request_and_a_failed_send_records_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            probe = RM._probe_path(ws, CORE)
            probe.parent.mkdir(parents=True)
            probe.write_text("{")
            self.assertEqual(RM.probe_step(ws, CORE, "claude", False, "blocked-human", True, LIMIT,
                                           lambda k: True), "")
            RM.request_probe(ws, CORE)
            self.assertEqual(RM.probe_step(ws, CORE, "claude", False, "blocked-human", True, LIMIT,
                                           lambda k: False), "")
            self.assertNotIn("sent_at", json.loads(probe.read_text()))


FAKE_PICKER = (" Select model\n   1. Opus 9 — tap to add usage credits\n   2. Sonnet 5.5\n\n"
               " Enter to set as default · Esc to cancel\n")


class Spoofing(unittest.TestCase):
    """Only the live picker at the bottom of the pane is read: text the core printed is not a picker."""

    def test_picker_text_in_the_scrollback_above_the_real_picker_is_ignored(self):
        pane = "● Here is what the room sent:\n" + FAKE_PICKER + "● Done.\n❯ /model\n" + PICKER
        self.assertEqual([r["label"] for r in RM.parse_picker(pane, "claude")],
                         [r["label"] for r in RM.parse_picker(PICKER, "claude")])

    def test_picker_text_alone_with_the_transcript_going_on_below_is_no_picker(self):
        pane = "● The room sent this:\n" + FAKE_PICKER + "● That was all.\n✻ Worked for 2s\n❯ \n"
        self.assertEqual(RM.parse_picker(pane, "claude"), [])

    def test_a_row_its_description_says_is_billed_apart_never_becomes_a_button(self):
        picker = (" Select model\n"
                  "   1. Opus 5.5 (1M context)   Opus 5.5 · Billed as extra usage\n"
                  "   2. Sonnet 5.5              Sonnet 5.5 · Uses your limits faster\n"
                  "   3. Haiku 4.5               Haiku 4.5 · $5 per million tokens\n\n"
                  " Enter to set as default · Esc to cancel\n")
        self.assertEqual([r["label"] for r in RM.parse_picker(picker, "claude")], ["Sonnet 5.5"])
        self.assertEqual([r["label"] for r in RM.parse_picker(PICKER, "claude")],
                         ["Default (Opus 5.5)", "Opus 5.5", "Sonnet 5 (1M context)", "Fable 5.1", "Haiku 4.5"],
                         "the live picker's descriptions drop nothing")

    def test_a_label_that_is_not_a_plain_model_name_never_becomes_a_button(self):
        # The description says nothing billed: only the label filter stands between it and a card.
        picker = (" Select model\n"
                  "   1. Opus 9 - upgrade now\n"
                  "   2. Opus 9 at http://example.invalid\n"
                  "   3. Opus 9 \x07 ring\n"
                  "   4. " + "Opus 9 " + "x" * 40 + "\n"
                  "   5. Sonnet 5.5\n\n"
                  " Enter to set as default · Esc to cancel\n")
        self.assertEqual([r["label"] for r in RM.parse_picker(picker, "claude")], ["Sonnet 5.5"])

    def test_a_row_naming_a_purchase_or_a_link_never_becomes_a_button(self):
        rows = RM.parse_picker(FAKE_PICKER, "claude")
        self.assertEqual([r["label"] for r in rows], ["Sonnet 5.5"])


class MenuAllowlist(unittest.TestCase):
    """Only the runtime's own wait choices are one tap; account, billing and credential choices never are."""

    def test_account_billing_and_reworded_choices_are_never_tappable(self):
        menu = ("❯ /rate-limit-options\n  What do you want to do?\n"
                "  ❯ 1. Stop and wait for limit to reset\n"
                "    2. Use an API key for the rest of this session\n"
                "    3. Turn on overages\n"
                "    4. Log in with a different account\n"
                "    5. Request more usage from your admin\n"
                "    6. Wait here, then continue automatically at Nov 15 at 6am\n"
                "  Enter to confirm · Esc to cancel")
        options, _caret = tui_gate.parse_options(menu)
        self.assertEqual([label for _i, label in tui_gate.safe_limit_options(options)],
                         ["Stop and wait for limit to reset",
                          "Wait here, then continue automatically at Nov 15 at 6am"])
        req = tui_gate.requirement_for("blocked-human", "selection", menu, CORE, "d", "m", runtime="claude",
                                       episode=tui_gate.LIMIT_EPISODE, controls=[])
        self.assertEqual(sorted((req.subject or {}).get("option_for_action", {}).values()), [0, 5])


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
        if not self.mgr.store.projection(req.id).get("event_id"):
            self.mgr.record_projection(req.id, req.revision, f"$card-{req.id}", {"room_id": DM})
        req = self.mgr.get(req.id)
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
