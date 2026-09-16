#!/usr/bin/env python3
"""Tests for src/core-input-watch.py — the core supervisor MONITOR (M1).

classify() fixtures are the ACTUAL tmux panes captured from the bundled core on
the Mac mini 2026-07-14 while driving the first-run gates by hand
(bypass-permissions, /login, paste-code, login-success) plus the two states that
must NEVER flag: the idle "ready for a task" prompt and normal agent output.

compose_state() tests exercise the full state machine (crashed / blocked-human /
blocked-known / logged-out / gateway-down / idle-ready / running / hung).

Run: python3 tests/core-input-watch.test.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src", "core-input-watch.py")
_spec = importlib.util.spec_from_file_location("core_input_watch", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify
compose_state = _mod.compose_state
auto_answer = _mod.auto_answer
main = _mod.main

_BYPASS = ("  in Bypass Permissions mode.\n  https://code.claude.com/docs/en/security\n"
           "  ❯ 1. No, exit\n    2. Yes, I accept\n  Enter to confirm · Esc to cancel")
_LOGIN_MENU = ("  Login\n  Select login method:\n  ❯ 1. Claude account with subscription\n"
               "    2. Anthropic Console account\n  Esc to cancel")
_PASTE = ("  Login\n  Browser didn't open? Use the url below to sign in\n"
          "https://claude.com/cai/oauth/authorize?code=true\n"
          "  Paste code here if prompted >\n  Esc to cancel")
_PRESS_ENTER = ("  Login\n  Logged in as x@example.com\n  Login successful. Press Enter to continue…")
_IDLE = ("──────── sutando-core ──\n❯ \n────────\n"
         "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents")
_IDLE_LOGGEDOUT = _IDLE + "\n     Not logged in · Run /login"
_WORKING = ("⏺ Bash(WS=... echo WORKSPACE=...)\n  ⎿  WORKSPACE=/Users/x\n     HOST=mini\n     … +39 lines")
# The usage-limit screen (owner screenshot 2026-09-02, Qingyuns-MacBook-Pro core):
# a wait/spend decision that the relay had been reporting as a LOGIN gate.
_SESSION_LIMIT = ("● Monitor event: \"Streaming task watcher\"\n"
                  "  ⎿  You've hit your session limit · resets 12:10pm\n"
                  "     (America/Los_Angeles)\n"
                  "     /usage-credits to finish what you're working on.\n"
                  "  Continuing automatically at 12:10pm · esc to cancel")
# A real mid-session permission prompt rendered ABOVE the persistent idle footer
# (review repro 2026-07-14). The footer's await-affordance must NOT suppress it.
_PERMISSION_WITH_FOOTER = (
    "  Do you want to proceed?\n  Allow this action\n  Esc to cancel\n"
    "  ────────\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents")
# Claude Code's Fable weekly-limit dialog, reconstructed from the CLI bundle (v2.1.258),
# not captured live: "Switch to <fallback> and continue" is focused by default.
_FABLE_LIMIT = ("  You've reached your Fable limit\n"
                "  You've used your included Fable usage for this week. Continuing on Fable 5.1 uses\n"
                "  usage credits — you have $0.00 in credits.\n"
                "  ❯ Switch to Opus 5 and continue\n"
                "    Continue with Fable 5.1\n"
                "  Esc to cancel")
# The same dialog under a select-list footer, in case the generic one is not rendered.
_FABLE_LIMIT_SELECT_FOOTER = _FABLE_LIMIT.rsplit("\n", 1)[0] + "\n  ↑/↓ to navigate · Enter to select"
# The caret on the PAYING option (rui, #3739): Enter here spends credits, so this
# must be a human gate however the text around it reads.
_FABLE_LIMIT_UNFOCUSED = _FABLE_LIMIT.replace(
    "  ❯ Switch to Opus 5 and continue\n    Continue with Fable 5.1",
    "    Switch to Opus 5 and continue\n  ❯ Continue with Fable 5.1")
assert _FABLE_LIMIT_UNFOCUSED != _FABLE_LIMIT
# The core's terminal on 2026-09-07 (#4015): every /startup was refused in 0-1s and the CLI
# went straight back to its idle footer, so no gate was ever on screen.
_REFUSAL_LINE = ("You're out of usage credits. Run /usage-credits to keep using Fable 5.1 "
                 "or /model to switch models.")
_IDLE_FOOTER = ("────────\n❯ \n────────\n"
                "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents")
_REFUSED_TURNS = ("❯ /startup\n"
                  f"  ⎿  {_REFUSAL_LINE}\n"
                  "✻ Cooked for 1s · done 12:32 PM\n"
                  "❯ /startup\n"
                  f"  ⎿  {_REFUSAL_LINE}\n"
                  "✻ Cogitated for 0s · done 12:42 PM\n")
# A /startup that ran: agent output, a real duration, the same footer.
_STARTUP_OK_TURN = ("❯ /startup\n"
                    "● /startup complete: watcher streaming, 3 crons registered.\n"
                    "✻ Worked for 2m 14s · done 12:55 PM\n")


class TestClassify(unittest.TestCase):
    def test_bypass_flags(self):
        self.assertEqual(classify(_BYPASS)[0], "bypass-permissions")

    def test_login_menu_flags(self):
        self.assertEqual(classify(_LOGIN_MENU)[0], "login")

    def test_paste_flags(self):
        self.assertEqual(classify(_PASTE)[0], "login")

    def test_press_enter_flags(self):
        self.assertEqual(classify(_PRESS_ENTER)[0], "press-enter")

    def test_idle_does_not_flag(self):
        self.assertIsNone(classify(_IDLE))

    def test_working_does_not_flag(self):
        self.assertIsNone(classify(_WORKING))

    def test_empty_does_not_flag(self):
        self.assertIsNone(classify(""))

    def test_permission_prompt_above_footer_is_not_suppressed(self):
        # Regression (review 2026-07-14): the idle-footer suppression must not hide a
        # real permission prompt just because the persistent footer is also present.
        hit = classify(_PERMISSION_WITH_FOOTER)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "permission")

    def test_permission_with_footer_composes_blocked_human(self):
        st, _d, prompt, kind = compose_state(_PERMISSION_WITH_FOOTER, "working", True)
        self.assertEqual(st, "blocked-human")
        self.assertEqual(kind, "permission")
        self.assertIsNotNone(prompt)

    def test_idle_with_await_token_still_does_not_flag(self):
        # The idle prompt can carry an await-hint-like token ("to accept") without
        # being a real gate — the _IDLE guard must still suppress it (never nag the
        # user on the ready-for-a-task prompt).
        idle_hint = ("  ⏵⏵ bypass permissions on (shift+tab to cycle) · "
                     "press tab to accept · ← for agents")
        self.assertIsNone(classify(idle_hint))

    def test_session_limit_flags_as_its_own_kind(self):
        self.assertEqual(classify(_SESSION_LIMIT)[0], "session-limit")

    def test_session_limit_wins_over_stale_login_text_above_it(self):
        # The live misfire: an earlier /login menu still in the pane scrollback
        # made the limit screen read as "login". The limit must win.
        pane = _LOGIN_MENU + "\n" + _SESSION_LIMIT
        self.assertEqual(classify(pane)[0], "session-limit")

    def test_stale_limit_text_does_not_outrank_a_live_login_gate(self):
        # The symmetric twin (rui + sonichi, #3730): a full limit screen left in
        # scrollback above a live login menu; the live gate is the lower one.
        pane = _SESSION_LIMIT + "\n" + _LOGIN_MENU
        self.assertEqual(classify(pane)[0], "login")

    def test_session_limit_is_a_human_gate_never_auto_answered(self):
        st, detail, _p, kind = compose_state(_SESSION_LIMIT, "working", True)
        self.assertEqual((st, kind), ("blocked-human", "session-limit"))
        self.assertIn("session-limit", detail)
        self.assertIsNone(auto_answer("session-limit"))

    def test_fable_limit_flags_as_its_own_kind(self):
        self.assertEqual(classify(_FABLE_LIMIT)[0], "fable-limit")
        self.assertEqual(classify(_FABLE_LIMIT_SELECT_FOOTER)[0], "fable-limit")

    def test_fable_limit_is_a_known_gate_answered_with_enter(self):
        # Enter takes the default-focused "Switch to <fallback> and continue": it
        # spends nothing and keeps the core working (owner 2026-09-02).
        st, _d, _p, kind = compose_state(_FABLE_LIMIT, "working", True)
        self.assertEqual((st, kind), ("blocked-known", "fable-limit"))
        self.assertEqual(auto_answer("fable-limit"), "Enter")

    def test_fable_limit_with_the_caret_elsewhere_is_a_human_gate(self):
        # Same dialog, caret on "Continue with Fable": Enter would spend credits.
        kind, _ = classify(_FABLE_LIMIT_UNFOCUSED)
        self.assertEqual(kind, "fable-limit-unfocused")
        st, _d, _p, k = compose_state(_FABLE_LIMIT_UNFOCUSED, "working", True)
        self.assertEqual((st, k), ("blocked-human", "fable-limit-unfocused"))
        self.assertIsNone(auto_answer("fable-limit-unfocused"))
        self.assertIsNone(_mod.answer_step("blocked-known", "fable-limit-unfocused", "p", None))

    def test_switch_text_without_the_caret_is_not_the_answerable_kind(self):
        # The switch phrase alone (scrollback, an unfocused row) must not read as focus.
        pane = "  Switch to Opus 5 and continue\n  Esc to cancel"
        self.assertNotEqual((classify(pane) or (None,))[0], "fable-limit")

    def test_a_focused_switch_line_on_some_other_dialog_is_never_typed_at(self):
        # sonichi (#3739): a dialog whose own text says it discards work carried the
        # same "Switch to … and continue" row; without the Fable text it is unknown.
        pane = ("  This will discard local changes\n"
                "  ❯ Switch to origin/main and continue\n    Cancel\n  Esc to cancel")
        kind, _ = classify(pane)
        self.assertNotIn(kind, ("fable-limit", "fable-limit-unfocused"))
        self.assertIsNone(auto_answer(kind))

    def test_a_resolved_fable_dialog_above_another_dialog_does_not_vouch_for_it(self):
        # sonichi's residual: the Fable text still inside the tail, then a NEW dialog
        # with its own focused switch row. Co-presence is not adjacency.
        pane = (_FABLE_LIMIT.replace("  ❯ Switch to Opus 5 and continue", "    Switch to Opus 5 and continue")
                .rsplit("\n", 1)[0]
                + "\n  [resolved]\n  This will discard local changes\n"
                  "  ❯ Switch to origin/main and continue\n    Cancel\n  Esc to cancel")
        kind, _ = classify(pane)
        self.assertEqual(kind, "unknown")
        self.assertIsNone(auto_answer(kind))
        # ...while the real dialog, whose body wraps onto a second line, still qualifies.
        wrapped = _FABLE_LIMIT.replace("uses\n  usage credits", "uses\n  usage\n  credits")
        self.assertEqual(classify(wrapped)[0], "fable-limit")

    def test_fable_limit_and_session_limit_stay_distinct(self):
        # One is a switch the monitor may take; the other is a wait/spend decision.
        self.assertEqual(classify(_FABLE_LIMIT)[0], "fable-limit")
        self.assertEqual(classify(_SESSION_LIMIT)[0], "session-limit")
        self.assertIsNone(auto_answer("session-limit"))

    def test_unforeseen_prompt_surfaces_as_unknown(self):
        # No matching signature, but an input affordance is present and it is NOT
        # the idle prompt → must surface as "unknown" (owner's no-dead-end rule),
        # never fall through silently.
        novel = "  Overwrite the existing config file?\n  (Enter to confirm · Esc to cancel)"
        hit = classify(novel)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "unknown")


class TestUnobservedProbeIsNotHung(unittest.TestCase):
    """An unobserved process probe (tmux refused the client, binary missing) must
    hold, never become the RECOVER-facing `hung`; only a SEEN session with stale
    status is `hung`, and a server that answered "no session" is `crashed`."""

    def test_unobserved_probe_holds(self):
        st, detail, _p, kind = compose_state("", "unknown", True, process=None)
        self.assertEqual(st, "unobserved")
        self.assertIn("unobserved", detail)
        self.assertNotEqual(st, "hung")

    def test_present_and_stale_is_still_hung(self):
        st, *_ = compose_state("Running step 3...\n(no prompt, no footer)", "unknown", True, process=True)
        self.assertEqual(st, "hung")

    def test_definitive_absence_is_crashed(self):
        st, *_ = compose_state("", "offline", True, process=False)
        self.assertEqual(st, "crashed")

    def test_refused_client_through_derive_and_compose(self):
        """Full path: refused tmux client → runtime_health.derive() → compose_state()."""
        import subprocess as _sp
        import sys as _sys
        import tempfile
        from unittest import mock
        _sys.path.insert(0, os.path.join(_HERE, "..", "src"))
        import tmux_probe  # noqa: E402
        rh_spec = importlib.util.spec_from_file_location(
            "runtime_health_pin", os.path.join(_HERE, "..", "src", "runtime-health.py"))
        rh = importlib.util.module_from_spec(rh_spec); rh_spec.loader.exec_module(rh)
        tmp = tempfile.mkdtemp()
        # Every unrelated probe held healthy so only the process signal varies.
        with mock.patch.object(rh, "_resolve_workspace", lambda repo: tmp), \
             mock.patch.object(rh, "_gateway_running", lambda: True), \
             mock.patch.object(rh, "_ag2space_app_running", lambda: True), \
             mock.patch.object(rh, "_station_cached", lambda ws: True), \
             mock.patch.object(rh, "_heartbeat_fresh", lambda ws: True):
            def refused(*a, **k):
                return _sp.CompletedProcess(["tmux"], 1, "", "server exited unexpectedly\n")
            with mock.patch.object(tmux_probe.subprocess, "run", refused):
                base = rh.derive()
            self.assertEqual(base["health"], "unknown")
            self.assertIsNone(base["signals"]["process"])
            st, *_ = compose_state("", base["health"], True, process=base["signals"]["process"])
            self.assertEqual(st, "unobserved")

            def gone(*a, **k):
                return _sp.CompletedProcess(["tmux"], 1, "", "can't find session: sutando-core\n")
            with mock.patch.object(tmux_probe.subprocess, "run", gone):
                base = rh.derive()
            self.assertEqual(base["health"], "offline")
            self.assertIs(base["signals"]["process"], False)
            st, *_ = compose_state("", base["health"], True, process=base["signals"]["process"])
            self.assertEqual(st, "crashed")


class TestComposeState(unittest.TestCase):
    """compose_state REFINES runtime-health's coarse `base_health` (one shared
    derivation, #2092) into the 8 supervisor states — signature is
    (pane, base_health, gateway_alive). base_health ∈
    {offline, needs_login, working, idle, unknown}."""

    def test_crashed_when_base_offline(self):
        # runtime-health "offline" (no session) dominates everything.
        st, *_ = compose_state(_WORKING, "offline", gateway_alive=True)
        self.assertEqual(st, "crashed")

    def test_blocked_human_on_login(self):
        # An ACTIVE /login menu is finer than the coarse health → carry the prompt.
        st, detail, prompt, kind = compose_state(_LOGIN_MENU, "working", True)
        self.assertEqual(st, "blocked-human")
        self.assertEqual(kind, "login")
        self.assertIsNotNone(prompt)

    def test_blocked_known_on_bypass(self):
        st, _d, _p, kind = compose_state(_BYPASS, "working", True)
        self.assertEqual(st, "blocked-known")
        self.assertEqual(kind, "bypass-permissions")

    def test_logged_out_when_base_needs_login_no_active_gate(self):
        # Passive "Not logged in · Run /login" banner (no active menu) → the
        # base needs_login maps straight to logged-out.
        st, *_ = compose_state(_IDLE_LOGGEDOUT, "needs_login", True)
        self.assertEqual(st, "logged-out")

    def test_gateway_down_when_base_ok_gateway_dead(self):
        st, *_ = compose_state(_IDLE, "idle", gateway_alive=False)
        self.assertEqual(st, "gateway-down")

    def test_idle_ready_when_base_idle(self):
        st, *_ = compose_state(_IDLE, "idle", True)
        self.assertEqual(st, "idle-ready")

    def test_running_when_base_working(self):
        st, *_ = compose_state(_WORKING, "working", True)
        self.assertEqual(st, "running")

    def test_hung_when_base_unknown(self):
        # runtime-health "unknown" = live session but stale/absent core-status
        # (wedged loop) → the supervisor's hung, carrying the pane tail. Uses a
        # WORKING (no-affordance) pane: no idle footer → genuinely wedged.
        st, _d, prompt, _k = compose_state(_WORKING, "unknown", True)
        self.assertEqual(st, "hung")
        self.assertIsNotNone(prompt)

    def test_idle_ready_overrides_stale_status_when_pane_is_idle(self):
        # #2112: a healthy core sitting at its idle prompt writes core-status
        # rarely, so runtime-health goes "unknown" (stale status) and WOULD be
        # falsely flagged hung (→ spurious ESCALATE / RECOVER). When the pane
        # POSITIVELY shows the idle-ready footer, trust it: idle, not wedged.
        idle_footer = ("prior output\n\n"
                       "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents")
        st, _d, _p, _k = compose_state(idle_footer, "unknown", True)
        self.assertEqual(st, "idle-ready")
        # A no-affordance pane (mid-work / frozen) with the same stale status must
        # STILL read hung — the override is positive-idle-only, so genuine wedge
        # detection is preserved.
        st2, *_ = compose_state("Running step 3...\n(no prompt, no footer)", "unknown", True)
        self.assertEqual(st2, "hung")


class TestRefusedTurn(unittest.TestCase):
    """#4015: a turn the CLI refuses is a FINISHED turn at the idle footer — no gate, no
    affordance — so classify() cannot see it and every base health reads idle-ready.
    The refused-turn detector must flag it as blocked-human/turn-rejected, carrying the
    refusal line, and must leave every other idle pane exactly as it was."""

    def test_the_pane_shows_no_gate(self):
        # The blind spot itself: nothing for the gate classifier to match.
        self.assertIsNone(classify(_REFUSED_TURNS + _IDLE_FOOTER))

    def test_refused_turns_at_the_idle_footer_are_blocked_human(self):
        for base in ("idle", "unknown", "working"):
            st, detail, prompt, kind = compose_state(_REFUSED_TURNS + _IDLE_FOOTER, base, True)
            self.assertEqual((st, kind), ("blocked-human", "turn-rejected"), base)
            self.assertEqual(prompt, _REFUSAL_LINE, base)
            self.assertIn("turn-rejected", detail)

    def test_a_bare_done_line_counts(self):
        # The captured shape without "· done HH:MM" (tests/runtime-health.test.py).
        pane = ("❯ /startup\n  ⎿  OAuth access token has expired · Please run /login\n"
                "✻ Worked for 0s\n" + _IDLE_FOOTER)
        st, _d, prompt, kind = compose_state(pane, "idle", True)
        self.assertEqual((st, kind), ("blocked-human", "turn-rejected"))
        self.assertEqual(prompt, "OAuth access token has expired · Please run /login")

    def test_a_successful_last_turn_stays_idle_ready(self):
        for base in ("idle", "unknown"):
            st, *_ = compose_state(_STARTUP_OK_TURN + _IDLE_FOOTER, base, True)
            self.assertEqual(st, "idle-ready", base)

    def test_a_refusal_in_scrollback_under_a_later_success_stays_idle_ready(self):
        st, *_ = compose_state(_REFUSED_TURNS + _STARTUP_OK_TURN + _IDLE_FOOTER, "idle", True)
        self.assertEqual(st, "idle-ready")

    def test_a_long_turn_that_mentions_the_words_is_not_a_refusal(self):
        for done in ("✻ Worked for 12s · done 1:00 PM", "✻ Worked for 2m 3s · done 1:00 PM"):
            pane = f"❯ /startup\n  ⎿  {_REFUSAL_LINE}\n{done}\n" + _IDLE_FOOTER
            st, *_ = compose_state(pane, "idle", True)
            self.assertEqual(st, "idle-ready", done)

    def test_agent_output_in_a_short_turn_is_not_a_refusal(self):
        # The words in the agent's own reply (`●`), not in a `⎿` result: the turn ran.
        pane = ("❯ usage?\n● You are not out of usage credits; /usage-credits shows $12.\n"
                "✻ Worked for 1s · done 1:00 PM\n" + _IDLE_FOOTER)
        st, *_ = compose_state(pane, "idle", True)
        self.assertEqual(st, "idle-ready")

    def test_a_tool_result_carrying_the_words_is_not_a_refusal(self):
        # `⎿` is also the tool-result marker. A short turn whose tool READ a refusal line
        # (and whose agent then answered) ran: the result belongs to the tool, not the CLI.
        pane = ("❯ check credits\n"
                "⏺ Bash(cat diagnostic.txt)\n"
                f"  ⎿  {_REFUSAL_LINE}\n"
                "● The diagnostic was read successfully.\n"
                "✻ Worked for 1s\n" + _IDLE_FOOTER)
        for base in ("idle", "unknown"):
            st, _d, prompt, kind = compose_state(pane, base, True)
            self.assertEqual((st, kind), ("idle-ready", None), (base, prompt))

    def test_not_logged_in_inside_ordinary_output_is_not_a_refusal(self):
        # Three common words: a slash-command result that merely contains them (no ⏺, ≤1s)
        # must not escalate. Only the CLI's own line-start form or /login adjacency counts.
        for line in ("gh: not logged in to github.com",
                     "src/auth.py:42: raise RuntimeError('not logged in')",
                     "shown to a visitor who is not logged in",
                     "ok test_errors_when_not_logged_in"):
            pane = f"❯ /somecommand\n  ⎿  {line}\n✻ Worked for 1s\n" + _IDLE_FOOTER
            st, _d, prompt, kind = compose_state(pane, "idle", True)
            self.assertEqual((st, kind), ("idle-ready", None), (line, prompt))

    def test_the_clis_own_not_logged_in_line_is_a_refusal(self):
        for line in ("Not logged in · Please run /login", "You are not logged in. Run /login",
                     "not logged in", "Run /login first: not logged in"):
            pane = f"❯ /startup\n  ⎿  {line}\n✻ Worked for 0s\n" + _IDLE_FOOTER
            st, _d, prompt, kind = compose_state(pane, "idle", True)
            self.assertEqual((st, kind), ("blocked-human", "turn-rejected"), line)
            self.assertEqual(prompt, line)

    def test_a_tool_result_alone_in_a_short_turn_is_not_a_refusal(self):
        # Same ownership, no trailing agent line: the `⏺` header already says the turn ran.
        pane = ("❯ check credits\n⏺ Bash(cat diagnostic.txt)\n"
                f"  ⎿  {_REFUSAL_LINE}\n✻ Worked for 0s\n" + _IDLE_FOOTER)
        st, *_ = compose_state(pane, "idle", True)
        self.assertEqual(st, "idle-ready")

    def test_a_refused_turn_under_a_newer_active_turn_stays_running(self):
        # History plus an active turn: the refusal completed, then a newer prompt started
        # and is still spinning. The old completion must not be reused.
        pane = (_REFUSED_TURNS
                + "❯ try again\n"
                + "● Checking the connection.\n"
                + "✻ Perambulating… (1m 46s · ↓ 5.9k tokens)\n" + _IDLE_FOOTER)
        st, _d, prompt, kind = compose_state(pane, "working", True)
        self.assertEqual((st, kind), ("running", None), prompt)

    def test_a_refused_turn_under_a_newer_typed_prompt_is_not_reused(self):
        # The owner has typed the next prompt but not sent it: the refusal is history.
        pane = _REFUSED_TURNS + _IDLE_FOOTER.replace("❯ \n", "❯ try again\n", 1)
        self.assertIn("❯ try again", pane)
        st, _d, prompt, kind = compose_state(pane, "idle", True)
        self.assertEqual((st, kind), ("idle-ready", None), prompt)

    def test_a_short_result_without_the_words_is_not_a_refusal(self):
        # The CLI's own `⎿` result, ended in 0s, but not one of the refusal lines.
        pane = ("❯ /nosuch\n  ⎿  Unknown slash command: /nosuch\n"
                "✻ Worked for 0s\n" + _IDLE_FOOTER)
        st, *_ = compose_state(pane, "idle", True)
        self.assertEqual(st, "idle-ready")

    def test_the_spinner_is_not_a_completed_turn(self):
        pane = (f"❯ /startup\n  ⎿  {_REFUSAL_LINE}\n"
                "✻ Perambulating… (1m 46s · ↓ 5.9k tokens)\n" + _IDLE_FOOTER)
        st, *_ = compose_state(pane, "idle", True)
        self.assertEqual(st, "idle-ready")

    def test_a_refusal_without_the_idle_footer_is_not_flagged(self):
        # Mid-render: no footer yet, so no evidence the core came back to rest.
        st, *_ = compose_state(_REFUSED_TURNS, "working", True)
        self.assertEqual(st, "running")

    def test_the_footer_alone_stays_idle_ready(self):
        st, *_ = compose_state(_IDLE, "idle", True)
        self.assertEqual(st, "idle-ready")

    def test_a_refused_turn_is_never_auto_answered(self):
        self.assertIsNone(auto_answer("turn-rejected"))
        self.assertIsNone(_mod.answer_step("blocked-human", "turn-rejected", _REFUSAL_LINE, None))


class TestAutoAnswer(unittest.TestCase):
    """M4 decision safety: only strictly-safe gates auto-answer; all else escalates."""

    def test_allowlist_is_exactly_press_enter_and_fable_limit(self):
        self.assertEqual(auto_answer("press-enter"), "Enter")
        self.assertEqual(auto_answer("fable-limit"), "Enter")
        self.assertEqual(set(_mod._AUTO_ANSWER), {"press-enter", "fable-limit"})

    def test_login_never_auto_answered(self):
        self.assertIsNone(auto_answer("login"))

    def test_unknown_never_auto_answered(self):
        # The no-dead-end catch-all must escalate, never guess a keystroke.
        self.assertIsNone(auto_answer("unknown"))

    def test_selection_and_permission_never_auto_answered(self):
        self.assertIsNone(auto_answer("selection"))
        self.assertIsNone(auto_answer("permission"))

    def test_trust_and_bypass_escalate_not_auto_accepted(self):
        # Handled by PREVENT seeds; if they surface at runtime we ESCALATE — never
        # auto-accept a trust / dangerous-mode prompt without explicit opt-in.
        self.assertIsNone(auto_answer("folder-trust"))
        self.assertIsNone(auto_answer("bypass-permissions"))


class TestAnswerStep(unittest.TestCase):
    """The actor's pure half: a settled, allowlisted gate gets one key per instance."""

    def test_sends_enter_for_a_settled_fable_limit(self):
        self.assertEqual(_mod.answer_step("blocked-known", "fable-limit", "p1", None), "Enter")

    def test_same_prompt_instance_is_answered_once(self):
        self.assertIsNone(_mod.answer_step("blocked-known", "fable-limit", "p1", "p1"))
        self.assertEqual(_mod.answer_step("blocked-known", "fable-limit", "p2", "p1"), "Enter")

    def test_human_gates_are_never_typed_at(self):
        for kind in ("login", "session-limit", "fable-limit-unfocused", "permission", "selection", "unknown"):
            self.assertIsNone(_mod.answer_step("blocked-known", kind, "p", None), kind)
        self.assertIsNone(_mod.answer_step("blocked-human", "fable-limit", "p", None))

    def test_a_settling_prompt_is_not_answered(self):
        self.assertIsNone(_mod.answer_step("running", "fable-limit", None, None))

    def test_disabled_flag_reports_only(self):
        self.assertIsNone(_mod.answer_step("blocked-known", "fable-limit", "p1", None, enabled=False))


class TestMainAutoAnswerWiring(unittest.TestCase):
    """One --once tick against a Fable-limit pane: the key is typed through send_keys
    and the signal file records it; --no-auto-answer only reports."""

    def _tick(self, extra_args, pane=None):
        import sys
        import tempfile
        from unittest.mock import patch
        sent = []
        pane = _FABLE_LIMIT if pane is None else pane
        out = os.path.join(tempfile.mkdtemp(), "core-supervisor.json")

        class _RH:
            TMUX_SOCKET = SESSION = None

            def derive(self):
                return {"health": "working"}
        argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--out", out,
                "--once", "--stable", "1"] + extra_args
        with patch.object(_mod, "capture", lambda s, sess: pane), \
                patch.object(_mod, "_load_runtime_health", lambda: _RH()), \
                patch.object(_mod, "gateway_alive", lambda *a: True), \
                patch.object(_mod, "_ensure_tmux_on_path", lambda: None), \
                patch.object(_mod, "send_keys", lambda s, sess, k: sent.append((s, sess, k)) or True), \
                patch.object(sys, "argv", argv):
            main()
        with open(out) as f:
            return sent, json.load(f)

    def test_fable_limit_is_typed_at_and_recorded(self):
        sent, payload = self._tick([])
        self.assertEqual(sent, [("/tmp/x.sock", "sutando-core", "Enter")])
        self.assertEqual(payload["kind"], "fable-limit")
        self.assertEqual(payload["auto_answered"]["kind"], "fable-limit")
        self.assertEqual(payload["auto_answered"]["key"], "Enter")

    def test_caret_on_the_paying_option_is_escalated_never_typed(self):
        sent, payload = self._tick([], pane=_FABLE_LIMIT_UNFOCUSED)
        self.assertEqual(sent, [])
        self.assertEqual((payload["state"], payload["kind"]), ("blocked-human", "fable-limit-unfocused"))
        self.assertNotIn("auto_answered", payload)

    def test_an_expired_answer_record_is_dropped_from_the_signal(self):
        # The record rides along for AUTO_ANSWER_CARRY_S; past that it is gone.
        from unittest.mock import patch
        with patch.object(_mod, "AUTO_ANSWER_CARRY_S", -1.0):
            sent, payload = self._tick([])
        self.assertEqual(len(sent), 1)
        self.assertNotIn("auto_answered", payload)

    def test_no_auto_answer_flag_reports_only(self):
        sent, payload = self._tick(["--no-auto-answer"])
        self.assertEqual(sent, [])
        self.assertEqual(payload["kind"], "fable-limit")
        self.assertNotIn("auto_answered", payload)


class TestSendKeys(unittest.TestCase):
    """send_keys reports what tmux did: True only on a zero exit, False on a
    non-zero exit or when tmux cannot be run at all — never an exception."""

    def _with_fake_tmux(self, script):
        import stat
        import tempfile
        d = tempfile.mkdtemp()
        log = os.path.join(d, "argv.log")
        p = os.path.join(d, "tmux")
        with open(p, "w") as f:
            f.write("#!/bin/sh\nprintf '%s\\n' \"$@\" > " + json.dumps(log) + "\n" + script + "\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        return d, log

    def test_zero_exit_is_true_and_the_key_reaches_the_session_pane(self):
        from unittest.mock import patch
        d, log = self._with_fake_tmux("exit 0")
        with patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ.get("PATH", "")}):
            self.assertTrue(_mod.send_keys("/tmp/x.sock", "sutando-core", "Enter"))
        with open(log) as f:
            self.assertEqual(f.read().split("\n")[:6],
                             ["-S", "/tmp/x.sock", "send-keys", "-t", "sutando-core:0", "Enter"])

    def test_non_zero_exit_is_false(self):
        from unittest.mock import patch
        d, _ = self._with_fake_tmux("exit 1")
        with patch.dict(os.environ, {"PATH": d + os.pathsep + os.environ.get("PATH", "")}):
            self.assertFalse(_mod.send_keys("/tmp/x.sock", "sutando-core", "Enter"))

    def test_an_unrunnable_tmux_is_false_not_an_exception(self):
        from unittest.mock import patch
        with patch.object(_mod.subprocess, "run", side_effect=OSError("no tmux")):
            self.assertFalse(_mod.send_keys("/tmp/x.sock", "sutando-core", "Enter"))


class TestEnsureTmuxOnPath(unittest.TestCase):
    """Mini-verified 2026-07-14: a detached spawn without Homebrew on PATH made bare
    `tmux` fail → healthy core mis-read as crashed. The monitor must self-heal PATH."""

    def test_prepends_tmux_dir_when_not_on_path(self):
        import sys
        orig_path = os.environ.get("PATH", "")
        orig_which = _mod.shutil.which
        orig_exists = _mod.os.path.exists
        try:
            _mod.shutil.which = lambda _n: None            # tmux not on PATH
            _mod.os.path.exists = lambda p: p == "/opt/homebrew/bin/tmux"
            os.environ["PATH"] = "/usr/bin"
            _mod._ensure_tmux_on_path()
            self.assertIn("/opt/homebrew/bin", os.environ["PATH"].split(os.pathsep))
        finally:
            _mod.shutil.which = orig_which
            _mod.os.path.exists = orig_exists
            os.environ["PATH"] = orig_path

    def test_noop_when_tmux_already_resolvable(self):
        orig_path = os.environ.get("PATH", "")
        orig_which = _mod.shutil.which
        try:
            _mod.shutil.which = lambda _n: "/usr/bin/tmux"  # already found
            os.environ["PATH"] = "/usr/bin"
            _mod._ensure_tmux_on_path()
            self.assertEqual(os.environ["PATH"], "/usr/bin")  # untouched
        finally:
            _mod.shutil.which = orig_which
            os.environ["PATH"] = orig_path


class TestMainOnce(unittest.TestCase):
    """End-to-end --once through the SHARED runtime-health derivation: with no
    live sutando-core, runtime_health.derive() → 'offline' → the supervisor
    writes state 'crashed'. Exercises main()/_load_runtime_health()/capture()/
    gateway_alive()/_atomic_write() in-process (not just classify/compose)."""

    def test_once_writes_crashed_with_no_core(self):
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "state", "core-supervisor.json")
            argv = ["core-input-watch.py", "--socket",
                    os.path.join(td, "nope.sock"), "--out", out, "--once"]
            old = sys.argv
            sys.argv = argv
            try:
                main()
            finally:
                sys.argv = old
            with open(out) as f:
                sig = json.load(f)
        self.assertEqual(sig["state"], "crashed")
        self.assertEqual(sig["session"], "sutando-core")

    def test_once_blocked_prompt_debounces_on_first_tick(self):
        """A fresh gate with --stable 2 must NOT escalate on the first tick — the
        debounce holds it as 'running (settling)' until the prompt persists. Drives
        the settling branch + the --app-data gateway probe in-process."""
        import sys
        import tempfile

        class _FakeRH:  # stand in for runtime-health: session alive + working
            TMUX_SOCKET = None
            SESSION = None

            def derive(self):
                return {"health": "working"}

        orig_cap, orig_load = _mod.capture, _mod._load_runtime_health
        _mod.capture = lambda sock, sess: _BYPASS          # a recognized gate
        _mod._load_runtime_health = lambda: _FakeRH()
        try:
            with tempfile.TemporaryDirectory() as td:
                out = os.path.join(td, "state", "core-supervisor.json")
                argv = ["core-input-watch.py", "--socket", "/x", "--out", out,
                        "--app-data", td, "--stable", "2", "--once"]
                old = sys.argv
                sys.argv = argv
                try:
                    main()
                finally:
                    sys.argv = old
                with open(out) as f:
                    sig = json.load(f)
        finally:
            _mod.capture, _mod._load_runtime_health = orig_cap, orig_load
        # stable=2, first tick → prompt seen once (< 2) → debounced to running.
        self.assertEqual(sig["state"], "running")



class TestCoreStateEmitter(unittest.TestCase):
    """Every supervisor state TRANSITION is POSTed to the obs collector as a raw
    `core.state` record; no endpoint means no POST; the prompt text never rides."""

    def test_payload_shape_carries_gate_not_prompt(self):
        p = _mod.core_state_payload("running", "blocked-human", "awaiting user: login",
                                    "login", "sutando-core", ts=1700000000.1234)
        self.assertEqual(p, {"kind": "core.state", "ts": 1700000000.123,
                             "session": "sutando-core", "from": "running",
                             "to": "blocked-human", "detail": "awaiting user: login",
                             "gate": "login"})
        self.assertNotIn("prompt", p)

    def test_payload_marks_gateway_auth_rejection_only_when_true(self):
        p = _mod.core_state_payload("running", "gateway-down", "d", None, "s",
                                    gateway_auth_rejected=True, ts=1.0)
        self.assertTrue(p["gateway_auth_rejected"])
        self.assertNotIn("gate", p)
        q = _mod.core_state_payload("running", "gateway-down", "d", None, "s", ts=1.0)
        self.assertNotIn("gateway_auth_rejected", q)

    def test_post_is_skipped_without_endpoint(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"SUTANDO_OBS_ENDPOINT": ""}):
            self.assertIsNone(_mod.obs_endpoint())
            self.assertFalse(_mod.post_core_state({"kind": "core.state"}))

    def test_post_targets_ingest_core_state_and_never_raises(self):
        from unittest.mock import patch
        seen = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode("utf-8"))
            seen["timeout"] = timeout
            return _Resp()
        with patch.object(_mod.urllib.request, "urlopen", fake_urlopen):
            ok = _mod.post_core_state({"kind": "core.state", "to": "running"},
                                      endpoint="http://localhost:4000/")
        self.assertTrue(ok)
        self.assertEqual(seen["url"], "http://localhost:4000/ingest/core-state")
        self.assertEqual(seen["body"]["to"], "running")
        self.assertEqual(seen["timeout"], _mod.CORE_STATE_POST_TIMEOUT_S)

        def boom(req, timeout=None):
            raise OSError("collector down")
        with patch.object(_mod.urllib.request, "urlopen", boom):
            self.assertFalse(_mod.post_core_state({"kind": "core.state"},
                                                  endpoint="http://localhost:4000"))

    def test_gateway_auth_rejected_reads_zero_backoff_disconnect(self):
        import tempfile
        import time
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "gateway-status.json")
            now = time.time()
            with open(path, "w") as f:
                json.dump({"ts": now, "connected": False, "last_ok_ts": None,
                           "backoff_s": 0}, f)
            self.assertTrue(_mod.gateway_auth_rejected(td))
            with open(path, "w") as f:
                json.dump({"ts": now, "connected": False, "last_ok_ts": now - 5,
                           "backoff_s": 8}, f)
            self.assertFalse(_mod.gateway_auth_rejected(td), "transport retry is not an auth rejection")
            with open(path, "w") as f:
                json.dump({"ts": now, "connected": True, "last_ok_ts": now,
                           "backoff_s": None}, f)
            self.assertFalse(_mod.gateway_auth_rejected(td))
        self.assertFalse(_mod.gateway_auth_rejected(None))
        self.assertFalse(_mod.gateway_auth_rejected("/nonexistent/dir"))

    def test_transition_is_retried_until_the_collector_acknowledges(self):
        calls = []

        def flaky(payload, endpoint=None):
            calls.append(payload)
            return len(calls) >= 3  # collector comes up on the third tick
        ep = "http://localhost:4000"
        # tick 1: first observation, collector down -> not acknowledged
        last = _mod.note_transition(None, "logged-out", "d", None, "s", None, post=flaky, endpoint=ep)
        self.assertIsNone(last)
        # tick 2: same state, still down -> retried with the same from
        last = _mod.note_transition(last, "logged-out", "d", None, "s", None, post=flaky, endpoint=ep)
        self.assertIsNone(last)
        # tick 3: accepted -> acknowledged
        last = _mod.note_transition(last, "logged-out", "d", None, "s", None, post=flaky, endpoint=ep)
        self.assertEqual(last, "logged-out")
        self.assertEqual([(c["from"], c["to"]) for c in calls], [(None, "logged-out")] * 3)
        # tick 4: no change -> no POST
        last = _mod.note_transition(last, "logged-out", "d", None, "s", None, post=flaky, endpoint=ep)
        self.assertEqual(len(calls), 3)

    def test_transition_skipped_while_down_collapses_into_one_record(self):
        calls = []
        ep = "http://localhost:4000"
        last = _mod.note_transition("running", "logged-out", "d", None, "s", None,
                                    post=lambda p, endpoint=None: calls.append(p) or False, endpoint=ep)
        self.assertEqual(last, "running")
        last = _mod.note_transition(last, "idle-ready", "d", None, "s", None,
                                    post=lambda p, endpoint=None: calls.append(p) or True, endpoint=ep)
        self.assertEqual(last, "idle-ready")
        self.assertEqual((calls[-1]["from"], calls[-1]["to"]), ("running", "idle-ready"))

    def test_transition_without_endpoint_advances_without_posting(self):
        from unittest.mock import patch
        calls = []
        with patch.dict(os.environ, {"SUTANDO_OBS_ENDPOINT": ""}):
            last = _mod.note_transition(None, "running", "d", None, "s", None,
                                        post=lambda p, endpoint=None: calls.append(p) or True)
        self.assertEqual(last, "running")
        self.assertEqual(calls, [])

    _REFUSED_PANE = "\n".join([
        "  ⎿ \xa0Not logged in · Please run /login", "✻ Baked for 0s · done 3:36 PM",
        "❯ /startup", "  ⎿ \xa03 skills available", "  ⎿ \xa0Not logged in · Please run /login",
        "✻ Cooked for 0s · done 3:50 PM", "❯ /startup", "  ⎿ \xa0Not logged in · Please run /login",
        "✻ Worked for 0s · done 3:52 PM", "                          Not logged in · Run /login",
        "─────────────────────────────────────── sutando-core ─", "❯\xa0",
        "──────────────────────────────────────────────────────",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ←…"])

    def test_last_refused_turn_reads_the_real_pane(self):
        got = _mod.last_refused_turn(self._REFUSED_PANE)
        self.assertEqual(got, (("✻ Worked for 0s · done 3:52 PM", 3), "Not logged in · Please run /login"))

    def test_last_refused_turn_identity_changes_with_a_new_turn(self):
        before = _mod.last_refused_turn(self._REFUSED_PANE)[0]
        nxt = self._REFUSED_PANE.replace("✻ Worked for 0s · done 3:52 PM",
                                         "✻ Worked for 0s · done 3:52 PM\n❯ /startup\n  ⎿ \xa0Not logged in · Please run /login\n✻ Worked for 0s · done 3:52 PM")
        after = _mod.last_refused_turn(nxt)[0]
        self.assertNotEqual(before, after, "same stamp text, one more completed turn → new identity")

    def test_last_refused_turn_controls(self):
        pane = self._REFUSED_PANE
        # a newer prompt typed below the completion → the refusal is history
        self.assertIsNone(_mod.last_refused_turn(pane.replace("❯\xa0", "❯ hello there")))
        # agent output in the turn → it ran; a tool result quoting the words is the tool's
        ran = pane.replace("❯ /startup\n  ⎿ \xa0Not logged in · Please run /login\n✻ Worked",
                           "❯ /startup\n● Checking…\n  ⎿ \xa0Not logged in · Please run /login\n✻ Worked")
        self.assertIsNone(_mod.last_refused_turn(ran))
        # not at the idle footer → None
        self.assertIsNone(_mod.last_refused_turn(_WORKING))
        self.assertIsNone(_mod.last_refused_turn(""))
        # a completed turn without a refusal line → None
        ok = pane.replace("  ⎿ \xa0Not logged in · Please run /login\n✻ Worked", "  ⎿ \xa0Done.\n✻ Worked")
        self.assertIsNone(_mod.last_refused_turn(ok))

    def test_core_turn_payload_shape(self):
        p = _mod.core_turn_payload("logged-out", "Not logged in · Please run /login",
                                   "✻ Worked for 0s · done 3:52 PM", "sutando-core", ts=1.5)
        self.assertEqual(p, {"kind": "core.turn_refused", "ts": 1.5, "session": "sutando-core",
                             "state": "logged-out", "line": "Not logged in · Please run /login",
                             "turn": "✻ Worked for 0s · done 3:52 PM"})

    def test_main_once_seeds_turn_identity_without_posting_a_refusal(self):
        import sys
        import tempfile
        from unittest.mock import patch
        posted = []
        out = os.path.join(tempfile.mkdtemp(), "core-supervisor.json")

        class _RH:
            TMUX_SOCKET = SESSION = None

            def derive(self):
                return {"health": "needs_login"}
        argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--out", out, "--once"]
        with patch.object(_mod, "capture", lambda s, sess: self._REFUSED_PANE), \
                patch.object(_mod, "_load_runtime_health", lambda: _RH()), \
                patch.object(_mod, "gateway_alive", lambda *a: True), \
                patch.object(_mod, "_ensure_tmux_on_path", lambda: None), \
                patch.object(_mod, "post_core_state", lambda p, **kw: posted.append(p) or True), \
                patch.dict(os.environ, {"SUTANDO_OBS_ENDPOINT": "http://localhost:4000"}), \
                patch.object(sys, "argv", argv):
            main()
        # The refusal already on screen predates the monitor: only the state record ships.
        self.assertEqual([p["kind"] for p in posted], ["core.state"])

    def test_main_once_posts_the_first_observed_state_from_none(self):
        import sys
        import tempfile
        from unittest.mock import patch
        posted = []
        out = os.path.join(tempfile.mkdtemp(), "core-supervisor.json")

        class _RH:
            TMUX_SOCKET = SESSION = None

            def derive(self):
                return {"health": "needs_login"}
        argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--out", out, "--once"]
        with patch.object(_mod, "capture", lambda s, sess: _IDLE_LOGGEDOUT), \
                patch.object(_mod, "_load_runtime_health", lambda: _RH()), \
                patch.object(_mod, "gateway_alive", lambda *a: True), \
                patch.object(_mod, "_ensure_tmux_on_path", lambda: None), \
                patch.object(_mod, "post_core_state", lambda p, **kw: posted.append(p) or True), \
                patch.dict(os.environ, {"SUTANDO_OBS_ENDPOINT": "http://localhost:4000"}), \
                patch.object(sys, "argv", argv):
            main()
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["kind"], "core.state")
        self.assertIsNone(posted[0]["from"])
        self.assertEqual(posted[0]["to"], "logged-out")
        self.assertEqual(posted[0]["session"], "sutando-core")
        self.assertNotIn("prompt", posted[0])


if __name__ == "__main__":
    unittest.main()
