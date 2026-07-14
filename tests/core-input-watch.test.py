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
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src", "core-input-watch.py")
_spec = importlib.util.spec_from_file_location("core_input_watch", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify
compose_state = _mod.compose_state

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
    def test_crashed_when_core_dead(self):
        # core_alive=False dominates everything.
        st, *_ = compose_state(_WORKING, core_alive=False, gateway_alive=True, progressing=True)
        self.assertEqual(st, "crashed")

    def test_blocked_human_on_login(self):
        st, detail, prompt, kind = compose_state(_LOGIN_MENU, True, True, False)
        self.assertEqual(st, "blocked-human")
        self.assertEqual(kind, "login")
        self.assertIsNotNone(prompt)

    def test_blocked_known_on_bypass(self):
        st, _d, _p, kind = compose_state(_BYPASS, True, True, False)
        self.assertEqual(st, "blocked-known")
        self.assertEqual(kind, "bypass-permissions")

    def test_logged_out_when_idle_but_not_authed(self):
        st, *_ = compose_state(_IDLE_LOGGEDOUT, True, True, False)
        self.assertEqual(st, "logged-out")

    def test_gateway_down_when_core_ok_gateway_dead(self):
        st, *_ = compose_state(_IDLE, True, gateway_alive=False, progressing=False)
        self.assertEqual(st, "gateway-down")

    def test_idle_ready_when_healthy(self):
        st, *_ = compose_state(_IDLE, True, True, False)
        self.assertEqual(st, "idle-ready")

    def test_running_when_progressing(self):
        st, *_ = compose_state(_WORKING, True, True, progressing=True)
        self.assertEqual(st, "running")

    def test_hung_when_stalled_not_prompt_not_idle(self):
        st, *_ = compose_state(_WORKING, True, True, progressing=False)
        self.assertEqual(st, "hung")


if __name__ == "__main__":
    unittest.main()
