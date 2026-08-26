#!/usr/bin/env python3
"""terminal_open pure core: detection from env + plan/applescript building.

open_instance (the actual spawner) is pragma-excluded; everything decision-
shaped is these pure helpers, driven for every supported terminal.

Run: python3 tests/runtime-cli-terminal-open.test.py   (stdlib only)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-cli"))

import terminal_open as to  # noqa: E402


class DetectTerminal(unittest.TestCase):
    def _with(self, env):
        base = {"TERM_PROGRAM": "", "WEZTERM_PANE": None, "KITTY_WINDOW_ID": None}
        clean = {k: v for k, v in {**base, **env}.items() if v is not None}
        drop = [k for k, v in {**base, **env}.items() if v is None]
        with mock.patch.dict("os.environ", clean, clear=False):
            for k in drop:
                mock.patch.dict("os.environ", {}, clear=False)
            import os
            for k in drop:
                os.environ.pop(k, None)
            return to.detect_terminal()

    def test_each_supported_terminal_detected(self):
        self.assertEqual(self._with({"TERM_PROGRAM": "Apple_Terminal"}),
                         "apple_terminal")
        self.assertEqual(self._with({"TERM_PROGRAM": "iTerm.app"}), "iterm2")
        self.assertEqual(self._with({"WEZTERM_PANE": "1"}), "wezterm")
        self.assertEqual(self._with({"KITTY_WINDOW_ID": "1"}), "kitty")

    def test_ghostty_detected(self):
        self.assertEqual(self._with({"TERM_PROGRAM": "ghostty"}), "ghostty")

    def test_wezterm_and_kitty_argv_when_binary_present(self):
        with mock.patch.object(to.shutil, "which", lambda _b: "/usr/bin/fake"):
            wz = to.build_open_plan("@a:x", "wezterm")
            self.assertEqual(wz["method"], "exec")
            self.assertIn("wezterm", wz["argv"][0])
            kt = to.build_open_plan("@a:x", "kitty")
            self.assertEqual(kt["method"], "exec")
            self.assertIn("kitty", kt["argv"][0])

    def test_unknown_terminal_falls_back(self):
        out = self._with({"TERM_PROGRAM": "MysteryTerm"})
        self.assertIsInstance(out, str)
        self.assertNotIn(out, ("apple_terminal", "iterm2", "wezterm", "kitty"))


class PlanBuilding(unittest.TestCase):
    def test_plan_for_every_terminal_names_the_attach_command(self):
        for term in ("apple_terminal", "iterm2", "wezterm", "kitty", "unknown"):
            plan = to.build_open_plan("@a:x", term)
            self.assertIn("@a:x", str(plan), term)

    def test_non_default_instance_lands_in_the_attach_command(self):
        plan = to.build_open_plan("@a:x", "unknown", instance="work")
        self.assertEqual(plan["command"], "sutando attach @a:x --instance work")
        # the default instance keeps the bare command (single-instance world)
        plan = to.build_open_plan("@a:x", "unknown", instance="default")
        self.assertEqual(plan["command"], "sutando attach @a:x")

    def test_applescript_tab_vs_window(self):
        tab = to.applescript_for("echo hi", window=False)
        win = to.applescript_for("echo hi", window=True)
        self.assertIn("echo hi", tab)
        self.assertIn("echo hi", win)
        self.assertNotEqual(tab, win)


if __name__ == "__main__":
    unittest.main(verbosity=2)
