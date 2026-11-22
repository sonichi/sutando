#!/usr/bin/env python3
"""A slash command typed into the composer opens Claude Code's completion list ABOVE it:
one row per command, its description beside it, wrapped onto indented lines. A description
can quote a key hint ("wait for the CLI to accept it"), which AWAIT_HINT matched, and with
no gate signature classify() returned "unknown": a blocked-on-you alert for an idle core.

The fixture is a real capture (Claude Code 2.1.322, a 120-column tmux pane, "/mod" typed,
nothing submitted); only the banner's working directory is replaced.

Run: python3 tests/core-input-watch-slash-menu.test.py
"""
from __future__ import annotations

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))
_spec = importlib.util.spec_from_file_location(
    "core_input_watch_slash_menu", os.path.join(_HERE, "..", "src", "core-input-watch.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

with open(os.path.join(_HERE, "fixtures", "pane-claude-slash-command-menu.txt"), encoding="utf-8") as _f:
    MENU = _f.read()

# The finished turn the live core showed above the list: the alert's excerpt quoted its first line.
_TURN = ("  Ran 1 shell command\n\n"
         "⏺ Nothing new to act on this pass.\n\n")
_BANNER_END = "/…/projects/demo\n"
MENU_UNDER_A_TURN = MENU.replace(_BANNER_END, _BANNER_END + "\n" + _TURN, 1)
assert MENU_UNDER_A_TURN != MENU


def _swap_composer(pane: str, text: str) -> str:
    return pane.replace("❯ /mod", f"❯ {text}", 1)


class TestSlashMenuIsNotAGate(unittest.TestCase):
    def test_the_captured_menu_carries_the_hint(self):
        # Positive control: without this the tests below would pass for the wrong reason.
        self.assertRegex(MENU, _mod._AWAIT_HINT)

    def test_the_menu_is_not_awaiting_the_user(self):
        for name, pane in (("menu", MENU), ("under a finished turn", MENU_UNDER_A_TURN)):
            with self.subTest(name):
                self.assertIsNone(_mod.classify(pane))
                self.assertIsNone(_mod.classify(pane, pane))

    def test_an_idle_core_with_the_menu_open_is_not_blocked_human(self):
        state = _mod.compose_state(MENU_UNDER_A_TURN, "idle", True, styled_pane=MENU_UNDER_A_TURN)
        self.assertEqual(state[0], "idle-ready")

    def test_other_quoted_hints_in_a_description_are_dropped_too(self):
        for hint in ("Press Enter to send", "Esc to cancel", "Enter to select", "↑/↓ to navigate"):
            with self.subTest(hint):
                pane = MENU.replace("plus optional rule tweaks", hint, 1)
                self.assertIn(hint, pane)
                self.assertIsNone(_mod.classify(pane))


class TestWhatStillCounts(unittest.TestCase):
    def test_menu_rows_are_dropped_only_while_the_composer_holds_a_slash_command(self):
        # The same rows over a composer holding prose are not a completion list.
        self.assertEqual(_mod.classify(_swap_composer(MENU, "check the mod"))[0], "unknown")

    def test_a_real_unrecognized_dialog_beside_the_menu_still_flags(self):
        pane = MENU.replace("────────────────", "Overwrite the existing config file?\n"
                            "Enter to confirm · Esc to cancel\n────────────────", 1)
        kind, tail = _mod.classify(pane)
        self.assertEqual(kind, "unknown")
        self.assertIn("Overwrite the existing config file?", tail)
        self.assertNotIn("/model-switch", tail)

    def test_a_gate_line_indented_under_a_menu_row_still_flags(self):
        # Indented at/past the description column it is shaped like a wrapped description;
        # its gate signature ends the run, so it stays evidence.
        row = "plus optional rule tweaks\n"
        for hint, kind in (("Press Enter to continue", "press-enter"),
                           ("Do you want to proceed? Enter to confirm", "permission")):
            with self.subTest(hint):
                pane = MENU.replace(row, row + " " * 41 + hint + "\n", 1)
                self.assertIn(" " * 41 + hint, pane)
                got = _mod.classify(pane)
                self.assertIsNotNone(got)
                self.assertEqual(got[0], kind)
                self.assertIn(hint, got[1])

    def test_known_gap_accept_hint_indented_under_menu_row_is_not_flagged(self):
        # A real dialog drawn this way would be missed; this pins it so a change is noticed.
        row = "plus optional rule tweaks\n"
        line = " " * 41 + "Press Enter to accept the change"
        pane = MENU.replace(row, row + line + "\n", 1)
        self.assertIn(line, pane)
        self.assertIn("❯ /mod", pane)
        self.assertIsNone(_mod.classify(pane))

    def test_a_novel_prompt_with_no_menu_still_classifies_unknown(self):
        pane = ("⏺ Done.\n"
                "Overwrite the existing config file?\n"
                "Enter to confirm · Esc to cancel\n"
                "❯ \n")
        self.assertEqual(_mod.classify(pane)[0], "unknown")


class TestFinishedToolSummariesAreNotGates(unittest.TestCase):
    """Collapsed tool-run summaries in a finished turn hold no key hint and must stay that way."""

    def test_summary_lines_over_an_idle_composer(self):
        for summary in ("Ran 1 shell command", "Ran 3 shell commands", "Read 2 files",
                        "Ran 5 shell commands, wrote 1 memory",
                        "Ran 2 shell commands, read 1 file (ctrl+o to expand)"):
            with self.subTest(summary):
                pane = (f"⏺ Checking.\n\n  {summary}\n\n⏺ Nothing to do.\n"
                        + "─" * 40 + "\n❯ \n" + "─" * 40 + "\n"
                        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n")
                self.assertIsNone(_mod.classify(pane))
                self.assertEqual(_mod.compose_state(pane, "idle", True)[0], "idle-ready")


if __name__ == "__main__":
    unittest.main()
