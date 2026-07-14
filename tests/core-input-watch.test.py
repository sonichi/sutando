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

    def test_unforeseen_prompt_surfaces_as_unknown(self):
        # No matching signature, but an input affordance is present and it is NOT
        # the idle prompt → must surface as "unknown" (owner's no-dead-end rule),
        # never fall through silently.
        novel = "  Overwrite the existing config file?\n  (Enter to confirm · Esc to cancel)"
        hit = classify(novel)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "unknown")


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
        # (wedged loop) → the supervisor's hung, carrying the pane tail.
        st, _d, prompt, _k = compose_state(_WORKING, "unknown", True)
        self.assertEqual(st, "hung")
        self.assertIsNotNone(prompt)


class TestAutoAnswer(unittest.TestCase):
    """M4 decision safety: only strictly-safe gates auto-answer; all else escalates."""

    def test_press_enter_is_the_only_auto_answer(self):
        self.assertEqual(auto_answer("press-enter"), "Enter")

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


if __name__ == "__main__":
    unittest.main()
