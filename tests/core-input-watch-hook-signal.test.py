#!/usr/bin/env python3
"""core-input-watch.py with the CLI's hook record (src/core_block_signal.py): a fresh hook
"clear" voids only an ambiguous pane reading (unknown, or a selection with no focused row),
a dialog the screen names stays blocked-human, a hook "blocked" is blocked-human, and with no
record the pane alone decides exactly as before.

Run: python3 tests/core-input-watch-hook-signal.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
_spec = importlib.util.spec_from_file_location(
    "core_input_watch_hook_signal", os.path.join(_HERE, "..", "src", "core-input-watch.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
import core_block_signal as cbs  # noqa: E402

with open(os.path.join(_HERE, "fixtures", "pane-claude-slash-command-menu.txt"), encoding="utf-8") as _f:
    SLASH_MENU = _f.read()

_COMPOSER = "─" * 80 + "\n❯ \n" + "─" * 80 + "\n  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
# A finished turn whose completion verb has an accented letter, under a reply quoting a hint.
SAUTEED = ("⏺ The diff shows the dialog text: \"Press Enter to accept the change\" — that is the\n"
           "  CLI's own hint, quoted here, not a prompt.\n\n"
           "✻ Sautéed for 1m 4s · done 9:32 AM · 1 shell still running\n" + _COMPOSER)
# The agent's own reply quoting a whole permission dialog: no pattern can tell it from a real one.
QUOTED_DIALOG = ("⏺ Earlier the CLI showed this, and I answered it:\n"
                 "  Do you want to proceed?\n  ❯ 1. Yes\n    2. No\n  Esc to cancel\n\n" + _COMPOSER)
# "Select" and a key hint in the agent's own reply, with no focused row: a selection guess.
AMBIGUOUS_SELECT = ("⏺ In the picker, Select the build you want and press Enter to confirm.\n\n"
                    + _COMPOSER)
LOGIN_MENU = ("  Login\n  Select login method:\n  ❯ 1. Claude account with subscription\n"
              "    2. Anthropic Console account\n  Esc to cancel")
IDLE_LOGGED_OUT = ("──────── sutando-core ──\n❯ \n────────\n"
                   "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
                   "     Not logged in · Run /login")
SESSION_LIMIT = ("⏺ Working on it.\n\n  You've hit your session limit · resets 3pm\n"
                 "  ❯ 1. Stop and wait for limit to reset\n    2. Add funds to continue with extra usage\n"
                 "  Enter to confirm · Esc to cancel")
_IDLE_FOOTER = ("────────\n❯ \n────────\n"
                "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents")
_REFUSAL = "You're out of usage credits. Run /usage-credits to keep using Fable 5.1 or /model to switch models."
REFUSED_TURNS = (f"❯ /startup\n  ⎿  {_REFUSAL}\n✻ Cooked for 1s · done 12:32 PM\n"
                 f"❯ /startup\n  ⎿  {_REFUSAL}\n✻ Cogitated for 0s · done 12:42 PM\n" + _IDLE_FOOTER)
LOGIN_EXPIRED = ("⏺ Bash(ls)\n  ⎿  done\n\n❯ /startup\n  ⎿  Login expired · Please run /login\n"
                 "✻ Worked for 0s\n" + _IDLE_FOOTER)
# The same screens before the CLI repaints its idle footer: nothing reads them as idle-ready.
LOGIN_EXPIRED_BARE = LOGIN_EXPIRED.replace(_IDLE_FOOTER, "")
# No gate, no idle footer, no motion: what a live permission dialog the pane cannot parse looks like.
STILL_PANE = "⏺ Bash(rm -rf build/)\n  ⎿  Running…\n"

CLEAR = ("clear", None)


def state(pane, base="idle", hook=None, **kw):
    return _mod.compose_state(pane, base, True, hook=hook, **kw)


class TestHookClearVoidsAPaneGuess(unittest.TestCase):
    def test_positive_control_the_quoted_dialog_flags_without_hooks(self):
        # Without this the clear case below would pass for the wrong reason.
        self.assertEqual(state(QUOTED_DIALOG)[0], "blocked-human")

    def test_a_fully_quoted_dialog_still_escalates_under_a_clear(self):
        # It carries a named gate (signature, focused row, key hint): the screen stays authoritative.
        self.assertEqual(state(QUOTED_DIALOG, hook=CLEAR)[0], "blocked-human")

    def test_an_ambiguous_reading_under_a_clear_is_voided(self):
        self.assertEqual(state(AMBIGUOUS_SELECT)[0], "blocked-human", "positive control")
        self.assertEqual(_mod.classify(AMBIGUOUS_SELECT)[0], "selection")
        self.assertNotEqual(state(AMBIGUOUS_SELECT, hook=CLEAR)[0], "blocked-human")

    def test_the_accented_turn_line_under_a_quoted_hint_is_not_blocked(self):
        self.assertNotEqual(state(SAUTEED, hook=CLEAR)[0], "blocked-human")
        # base "unknown" (stale core-status) must read idle, not hung: a hung core is restarted.
        self.assertEqual(state(SAUTEED, base="unknown", hook=CLEAR)[0], "idle-ready")

    def test_the_real_slash_menu_is_not_blocked(self):
        self.assertNotEqual(state(SLASH_MENU, hook=CLEAR)[0], "blocked-human")
        self.assertEqual(state(SLASH_MENU, base="unknown", hook=CLEAR)[0], "idle-ready")


class TestMenuGateUnderAClear(unittest.TestCase):
    """Gate text under a completion-menu row is sent to a human on purpose; a recent unrelated
    clear must not void it, and nothing may type at it."""

    ROW = "plus optional rule tweaks\n"

    def panes(self):
        for hint in ("Press Enter to continue", "Do you want to proceed? Enter to confirm"):
            yield hint, SLASH_MENU.replace(self.ROW, self.ROW + " " * 41 + hint + "\n", 1)
        block = " " * 41 + "Do you want to proceed?\n" + " " * 41 + "Enter to confirm · Esc to cancel\n"
        yield "split", SLASH_MENU.replace(self.ROW, self.ROW + block, 1)

    def test_it_alerts_under_an_unrelated_clear_and_no_key_is_sent(self):
        for label, pane in self.panes():
            with self.subTest(label):
                self.assertNotEqual(pane, SLASH_MENU)
                self.assertEqual(_mod.classify(pane)[0], "unknown")
                self.assertTrue(_mod.slash_menu_gate_rows(*_mod._live_tails(pane, pane), _mod.CLAUDE))
                st, _d, prompt, kind = _mod.compose_state(pane, "idle", True, styled_pane=pane, hook=CLEAR)
                self.assertEqual(st, "blocked-human")
                self.assertIsNone(_mod.answer_step(st, kind, prompt, None))
                self.assertIsNone(_mod.auto_answer(kind))

    def test_the_menu_alone_stays_quiet_under_a_clear(self):
        self.assertFalse(_mod.slash_menu_gate_rows(*_mod._live_tails(SLASH_MENU, SLASH_MENU), _mod.CLAUDE))
        self.assertNotEqual(state(SLASH_MENU, hook=CLEAR, styled_pane=SLASH_MENU)[0], "blocked-human")


class TestScreensHooksNeverReport(unittest.TestCase):
    def test_a_login_menu_stands_under_a_clear(self):
        self.assertEqual(state(LOGIN_MENU, hook=CLEAR)[3], "login")

    def test_signed_out_stands_under_a_clear(self):
        self.assertEqual(state(IDLE_LOGGED_OUT, base="needs_login", hook=CLEAR)[0], "logged-out")

    def test_a_usage_limit_menu_stands_under_a_clear(self):
        self.assertEqual(state(SESSION_LIMIT, hook=CLEAR), state(SESSION_LIMIT))
        self.assertEqual(state(SESSION_LIMIT)[0], "blocked-human")

    def test_a_crashed_core_stays_crashed(self):
        self.assertEqual(state("", base="offline", hook=("blocked", "permission"))[0], "crashed")


class TestHookBlocked(unittest.TestCase):
    def test_a_blocked_record_is_blocked_human(self):
        s, detail, prompt, kind = state(STILL_PANE, hook=("blocked", "permission"))
        self.assertEqual((s, kind), ("blocked-human", "permission"))
        self.assertIn("hook", detail)
        self.assertIn("Bash(rm -rf build/)", prompt)

    def test_a_question_dialog_is_a_selection_gate(self):
        self.assertEqual(state(STILL_PANE, hook=("blocked", "question"))[3], "selection")

    def test_a_pane_back_at_its_idle_prompt_outweighs_a_blocked_record(self):
        # A dialog dismissed with Esc fires no clearing hook; the idle footer is the proof.
        idle = "● Done.\n✻ Worked for 2m 14s · done 11:20 PM\n" + _COMPOSER
        self.assertEqual(state(idle, hook=("blocked", "permission"))[0], "idle-ready")

    def test_the_pane_kind_wins_when_the_pane_names_the_gate(self):
        self.assertEqual(state(LOGIN_MENU, hook=("blocked", "permission"))[3], "login")


class TestScreenEvidenceOutranksAHeldDialog(unittest.TestCase):
    """A blocked record is sticky; a screen that proves a sign-in, refusal or limit now wins."""

    HELD = ("blocked", "permission")

    def test_positive_controls_each_screen_reads_without_a_record(self):
        self.assertEqual(state(IDLE_LOGGED_OUT, base="needs_login")[0], "logged-out")
        self.assertEqual(state(LOGIN_EXPIRED)[0], "logged-out")
        self.assertEqual(state(REFUSED_TURNS)[3], "turn-rejected")

    def test_signed_out_outranks_a_held_dialog(self):
        self.assertEqual(state(IDLE_LOGGED_OUT, base="needs_login", hook=self.HELD)[0], "logged-out")
        self.assertEqual(state(STILL_PANE, base="needs_login", hook=self.HELD)[0], "logged-out")
        for pane in (LOGIN_EXPIRED, LOGIN_EXPIRED_BARE):
            self.assertEqual(state(pane)[0], "logged-out")
            self.assertEqual(state(pane, hook=self.HELD)[0], "logged-out")

    def test_a_refused_turn_outranks_a_held_dialog(self):
        # A refused turn is only read under the idle footer, so the footer case is the one to hold.
        self.assertEqual(state(REFUSED_TURNS, hook=self.HELD)[3], "turn-rejected")

    def test_a_limit_screen_and_a_paused_queue_outrank_a_held_dialog(self):
        self.assertEqual(state(SESSION_LIMIT, hook=self.HELD), state(SESSION_LIMIT))
        self.assertEqual(state(STILL_PANE, base="paused", hook=self.HELD), state(STILL_PANE, base="paused"))
        self.assertEqual(state(STILL_PANE, base="paused")[0], "blocked-known")

    def test_each_screen_is_the_same_with_or_without_the_record(self):
        for pane, base in ((IDLE_LOGGED_OUT, "needs_login"), (LOGIN_EXPIRED, "idle"),
                           (REFUSED_TURNS, "idle"), (SESSION_LIMIT, "idle"), (LOGIN_MENU, "idle")):
            self.assertEqual(state(pane, base=base, hook=self.HELD), state(pane, base=base), pane[:30])


class TestAnOwnerQuestionReachesTheOwner(unittest.TestCase):
    """AskUserQuestion's own PreToolUse is a blocked record, so the screen's question stands."""

    QUESTION = ("⏺ AskUserQuestion\n\n  Ship the release now?\n  ❯ 1. Yes\n    2. Not yet\n"
                "  Enter to select · Esc to cancel")

    def test_positive_control_the_question_is_a_selection_without_hooks(self):
        self.assertEqual(state(self.QUESTION)[::3], ("blocked-human", "selection"))

    def test_the_question_tools_record_keeps_it_blocked(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        payload = {"hook_event_name": "PreToolUse", "tool_use_id": "q1", "tool_name": "AskUserQuestion",
                   "tool_input": {"questions": [{"question": "Ship the release now?"}]}}
        cbs.record_event(tmp.name, "/tmp/s.sock", "%1", 9, {"hook_event_name": "Stop"}, "claude", "s")
        cbs.record_event(tmp.name, "/tmp/s.sock", "%1", 9, payload, "claude", "s")
        hook = cbs.verdict(cbs.read(tmp.name, "/tmp/s.sock", "%1"), 9, cbs.read(tmp.name, "/tmp/s.sock", "%1")["ts"])
        self.assertEqual(hook, ("blocked", "question"))
        self.assertEqual(state(self.QUESTION, hook=hook)[::3], ("blocked-human", "selection"))

    def test_an_unrelated_clear_does_not_hide_a_named_question(self):
        # A focused numbered row under a key hint is a dialog the screen names; no clear voids it.
        self.assertEqual(state(self.QUESTION, hook=CLEAR)[::3], ("blocked-human", "selection"))


class TestNoRecordIsTheOldBehaviour(unittest.TestCase):
    def test_every_pane_reads_as_without_the_parameter(self):
        for pane in (SLASH_MENU, SAUTEED, QUOTED_DIALOG, LOGIN_MENU, IDLE_LOGGED_OUT, SESSION_LIMIT, STILL_PANE):
            for base in ("idle", "unknown", "needs_login", "working"):
                self.assertEqual(_mod.compose_state(pane, base, True, hook=None),
                                 _mod.compose_state(pane, base, True), (base, pane[:30]))


class TestHookVerdictFromDisk(unittest.TestCase):
    """The loop's reader: the record must belong to the core pane's current process and,
    when clear, be fresh; anything else is no verdict, so the screen decides."""

    SOCK = "/tmp/test-core.sock"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = self.tmp.name
        self.patches = [mock.patch.object(_mod.cli_wedge, "core_target", return_value="=sutando-core:0"),
                        mock.patch.object(cbs, "pane_identity", return_value=("sutando-core", "%4", 321))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def put(self, st, ts, pid=321):
        cbs.write(self.state, self.SOCK, "%4", pid, st, "permission" if st == "blocked" else None,
                  "Stop", "claude", "sutando-core", now=ts)

    def verdict(self, now):
        return _mod.hook_verdict(self.SOCK, "sutando-core", self.state, now)

    def test_no_record_makes_no_tmux_call(self):
        # The loop's harnesses count time.sleep; a real tmux call polls it and spends their ticks.
        with mock.patch.object(_mod.cli_wedge, "core_target", side_effect=AssertionError("tmux called")), \
                mock.patch.object(cbs, "pane_identity", side_effect=AssertionError("tmux called")), \
                mock.patch("subprocess.run", side_effect=AssertionError("subprocess called")):
            self.assertIsNone(self.verdict(1000.0))
            cbs.write(self.state, "/tmp/another.sock", "%4", 321, "clear", None, "Stop", "claude", "s")
            self.assertIsNone(self.verdict(1000.0))

    def test_no_record_falls_back(self):
        self.assertIsNone(self.verdict(1000.0))
        self.assertEqual(state(QUOTED_DIALOG, hook=self.verdict(1000.0))[0], "blocked-human")

    def test_a_fresh_clear_is_read(self):
        self.put("clear", 1000.0)
        self.assertEqual(self.verdict(1010.0), CLEAR)

    def test_a_clear_past_its_ttl_falls_back_to_the_screen(self):
        self.put("clear", 1000.0)
        v = self.verdict(1000.0 + cbs.CLEAR_TTL_S + 5)
        self.assertIsNone(v)
        self.assertEqual(state(QUOTED_DIALOG, hook=v)[0], "blocked-human")

    def test_a_record_from_the_previous_cli_falls_back(self):
        self.put("clear", 1000.0, pid=111)
        self.assertIsNone(self.verdict(1001.0))

    def test_a_blocked_record_is_read(self):
        self.put("blocked", 1000.0)
        self.assertEqual(self.verdict(99999.0), ("blocked", "permission"))

    def test_an_unreadable_pane_falls_back(self):
        self.put("clear", 1000.0)
        with mock.patch.object(cbs, "pane_identity", return_value=None):
            self.assertIsNone(self.verdict(1001.0))


if __name__ == "__main__":
    unittest.main()
