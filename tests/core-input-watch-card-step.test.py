#!/usr/bin/env python3
"""The pane watcher's per-tick card verdict (P1-24: a decision card resolved on a
single idle or failed capture and the identical dialog was raised again).

Run: python3 tests/core-input-watch-card-step.test.py
"""
import importlib.util as u
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))
spec = u.spec_from_file_location("ciw_cs", SRC / "core-input-watch.py")
M = u.module_from_spec(spec)
spec.loader.exec_module(M)


class _Done(Exception):
    pass


_DIALOG_A = (
    "Flicker-free output, mouse support and auto-copy are available.\n"
    "❯ 1. Yes, try it\n"
    "  2. Not now\n"
    "Enter to confirm · Esc to cancel\n"
)
_DIALOG_B = _DIALOG_A.replace("❯ 1. Yes, try it\n  2. Not now", "  1. Yes, try it\n❯ 2. Not now")


class TestCardStep(unittest.TestCase):
    def test_a_failed_capture_is_no_evidence(self):
        self.assertEqual(M.card_step(False, "blocked-human", 0, 2), ("hold", 0))
        self.assertEqual(M.card_step(False, "running", 1, 2), ("hold", 1), "the idle count is kept, not advanced")

    def test_a_settling_prompt_holds(self):
        # Review of #4868 (Rui): a blocked tick under the entry debounce was rewritten to
        # "running" and counted as idle, so a re-rendering dialog lost its card.
        self.assertEqual(M.card_step(True, "running", 1, 2, settling=True), ("hold", 1))
        self.assertEqual(M.card_step(True, "running", 0, 1, settling=True), ("hold", 0))

    def test_five_ticks_of_an_alternating_prompt_never_resolve(self):
        """main() over five ticks whose dialog text alternates (a re-rendering dialog):
        no tick may resolve the session's cards."""
        out = os.path.join(tempfile.mkdtemp(), "core-supervisor.json")
        panes = [_DIALOG_A, _DIALOG_B, _DIALOG_A, _DIALOG_B, _DIALOG_A]

        class _RH:
            TMUX_SOCKET = SESSION = None

            def derive(self):
                return {"health": "working"}
        calls = []
        ticks = {"n": 0}

        def capture(s, sess):
            i = min(ticks["n"], len(panes) - 1)
            return panes[i]

        def sleep(_secs):
            ticks["n"] += 1
            if ticks["n"] >= len(panes):
                raise _Done()
        argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--out", out, "--stable", "2", "--interval", "0"]
        with patch.object(M, "capture", capture), \
                patch.object(M, "_load_runtime_health", lambda: _RH()), \
                patch.object(M, "gateway_alive", lambda *a: True), \
                patch.object(M, "session_runtime", lambda *a: None), \
                patch.object(M, "_ensure_tmux_on_path", lambda: None), \
                patch.object(M, "_hitl_manager", lambda out_path: object()), \
                patch.object(M, "escalate", lambda *a, **k: calls.append("escalate")), \
                patch.object(M, "drive_escalations", lambda *a, **k: calls.append("drive")), \
                patch.object(M, "resolve_escalations", lambda *a, **k: calls.append("resolve")), \
                patch.object(M.time, "sleep", sleep), \
                patch.object(sys, "argv", argv):
            with self.assertRaises(_Done):
                M.main()
        self.assertEqual(ticks["n"], 5)
        self.assertNotIn("resolve", calls, f"an alternating dialog resolved a card: {calls}")

    def test_blocked_escalates_and_resets_the_idle_count(self):
        self.assertEqual(M.card_step(True, "blocked-human", 1, 2), ("escalate", 0))

    def test_leaving_the_blocked_set_resolves_only_after_stable_ticks(self):
        v, n = M.card_step(True, "running", 0, 2)
        self.assertEqual((v, n), ("hold", 1), "one idle tick: the dialog may only be re-rendering")
        self.assertEqual(M.card_step(True, "running", n, 2), ("resolve", 2))
        self.assertEqual(M.card_step(True, "idle-ready", 0, 1), ("resolve", 1), "--stable 1 keeps the old one-tick behaviour")
        self.assertEqual(M.card_step(True, "running", 0, 0), ("resolve", 1), "a zero stable is one")

    def test_the_loop_holds_cards_on_a_failed_capture(self):
        """One --once tick with tmux returning nothing: neither escalate nor resolve runs."""
        for pane in (None, "", "   \n\n"):
            with self.subTest(pane=repr(pane)):
                self._tick_with_pane(pane)

    def _tick_with_pane(self, pane):
        out = os.path.join(tempfile.mkdtemp(), "core-supervisor.json")

        class _RH:
            TMUX_SOCKET = SESSION = None

            def derive(self):
                return {"health": "working"}
        calls = []
        argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--out", out, "--once", "--stable", "1"]
        with patch.object(M, "capture", lambda s, sess: pane), \
                patch.object(M, "_load_runtime_health", lambda: _RH()), \
                patch.object(M, "gateway_alive", lambda *a: True), \
                patch.object(M, "_ensure_tmux_on_path", lambda: None), \
                patch.object(M, "_hitl_manager", lambda out_path: object()), \
                patch.object(M, "escalate", lambda *a, **k: calls.append("escalate")), \
                patch.object(M, "drive_escalations", lambda *a, **k: calls.append("drive")), \
                patch.object(M, "resolve_escalations", lambda *a, **k: calls.append("resolve")), \
                patch.object(sys, "argv", argv):
            M.main()
        self.assertEqual(calls, [], f"a failed or blank capture ({pane!r}) must neither raise nor resolve a card")


if __name__ == "__main__":
    unittest.main()
