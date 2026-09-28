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


class TestCardStep(unittest.TestCase):
    def test_a_failed_capture_is_no_evidence(self):
        self.assertEqual(M.card_step(False, "blocked-human", 0, 2), ("hold", 0))
        self.assertEqual(M.card_step(False, "running", 1, 2), ("hold", 1), "the idle count is kept, not advanced")

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
