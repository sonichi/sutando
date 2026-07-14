#!/usr/bin/env python3
"""Tests for src/core-input-watch.py classify() — the core-needs-input detector.

Fixtures are the ACTUAL tmux panes captured from the bundled core on the Mac mini
2026-07-14 while driving the first-run gates by hand (bypass-permissions, /login,
paste-code, login-success) plus the two states that must NEVER flag: the idle
"ready for a task" prompt and normal agent output.

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


class TestClassify(unittest.TestCase):
    def test_bypass_permissions_prompt_flags(self) -> None:
        pane = (
            "  in Bypass Permissions mode.\n"
            "  https://code.claude.com/docs/en/security\n"
            "  ❯ 1. No, exit\n    2. Yes, I accept\n"
            "  Enter to confirm · Esc to cancel"
        )
        self.assertEqual(classify(pane)[0], "bypass-permissions")

    def test_login_method_menu_flags(self) -> None:
        pane = (
            "  Login\n  Select login method:\n"
            "  ❯ 1. Claude account with subscription\n"
            "    2. Anthropic Console account\n  Esc to cancel"
        )
        self.assertEqual(classify(pane)[0], "login")

    def test_paste_code_prompt_flags(self) -> None:
        pane = (
            "  Login\n  Browser didn't open? Use the url below to sign in\n"
            "https://claude.com/cai/oauth/authorize?code=true\n"
            "  Paste code here if prompted >\n  Esc to cancel"
        )
        self.assertEqual(classify(pane)[0], "login")

    def test_press_enter_prompt_flags(self) -> None:
        pane = (
            "  Login\n  Logged in as qingyun0327@gmail.com\n"
            "  Login successful. Press Enter to continue…"
        )
        self.assertEqual(classify(pane)[0], "press-enter")

    def test_idle_ready_prompt_does_not_flag(self) -> None:
        # The normal "ready for a task" state — a bare prompt with the bypass
        # footer. Must NOT be surfaced as needing input (would false-alarm forever).
        pane = (
            "──────── sutando-core ──\n❯ \n────────\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
            "     Not logged in · Run /login"
        )
        self.assertIsNone(classify(pane))

    def test_normal_agent_output_does_not_flag(self) -> None:
        pane = (
            "⏺ Bash(WS=... echo WORKSPACE=...)\n"
            "  ⎿  WORKSPACE=/Users/qingyun-mini/Library/Application Support\n"
            "     HOST=qingyun-mac-mini\n     … +39 lines"
        )
        self.assertIsNone(classify(pane))

    def test_empty_pane_does_not_flag(self) -> None:
        self.assertIsNone(classify(""))


if __name__ == "__main__":
    unittest.main()
