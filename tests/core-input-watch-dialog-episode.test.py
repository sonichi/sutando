#!/usr/bin/env python3
"""One option dialog on a seat's screen is one requirement and one card for as long as it shows.

A seat's pane repaints around a dialog it is blocked on: a tool's elapsed timer, a status band,
a spinner, the footer. Each repaint used to key a new requirement, so one prompt posted a fresh
card every few polls. Each tick below is a fresh --once process on the same store.
"""
import importlib.util as u
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(HERE.parent / "skills" / "worker-pool" / "scripts"))
sys.path.insert(0, str(SRC))
spec = u.spec_from_file_location("ciw_dialog_episode", SRC / "core-input-watch.py")
M = u.module_from_spec(spec)
spec.loader.exec_module(M)

from hitl.projector import project  # noqa: E402

SESSION = "sutando-worker-1"
SEAT = "worker eng"
QUESTION = ("│ Enable hot reloading for this session?\n│\n"
            "│ Mods in this session's folder load when this turn ends and reload when their\n"
            "│ files change. They run with your permissions.\n")


def _pane(above, footer, question=QUESTION, caret=0):
    opts = ["How does this work?", "Enable for this session", "Not now", "Type something."]
    rows = "".join(f" {'❯' if i == caret else ' '} {i + 1}. {o}\n" for i, o in enumerate(opts))
    return (above + "─" * 64 + "\n Mods\n" + question + rows
            + " Enter to select · ↑/↓ to navigate · Esc to cancel\n"
            + f"  ⏵⏵ bypass permissions on · {footer}\n")


# The chrome a blocked seat repaints between polls; the dialog itself never changes.
FRAMES = [
    _pane("⏺ Running sleep 25; check the forwarders … · 12s\nSutando: using a tool      [-]\n",
          "1 monitor · ← for agents"),
    _pane("⏺ Running sleep 25; check the forwarders … · 22s\nSutando: thinking      [-]\n",
          "1 monitor · ← for agents · 2 …"),
    _pane("✢ Generating… (42s · ↓ 2.1k tokens)\n  ⎿  Tip: Use /statusline to set up a status line\n"
          "Sutando: waiting on you      [-]\n", "2 monitors · esc to interrupt"),
    _pane("  ⎿  Churned for 1m 3s\nSutando: waiting on you      [-]\n", "1 monitor"),
    _pane("Sutando: waiting on you      [-]\n", "1 monitor", caret=2),
]


class _RH:
    TMUX_SOCKET = SESSION = None

    def derive(self):
        return {"health": "working"}


def _tick(out, pane):
    argv = ["core-input-watch.py", "--socket", "/tmp/x.sock", "--session", SESSION,
            "--out", out, "--once", "--stable", "1", "--seat", SEAT]
    with patch.object(M, "capture_styled", lambda s, sess: pane), \
            patch.object(M, "_load_runtime_health", lambda: _RH()), \
            patch.object(M, "gateway_alive", lambda *a: True), \
            patch.object(M, "_ensure_tmux_on_path", lambda: None), \
            patch.object(M, "session_runtime", lambda *a: "claude"), \
            patch.object(sys, "argv", argv):
        M.main()
    with open(out) as f:
        return json.load(f)


class OneDialogOneCard(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.td.name, "state", f"core-supervisor.{SESSION}.json")
        os.makedirs(os.path.dirname(self.out))
        self.sent = []

    def tearDown(self):
        self.td.cleanup()

    def _send(self, payload):
        self.sent.append(payload)
        return {"ok": True, "event_id": f"$e{len(self.sent)}"}

    def _poll(self, pane):
        sig = _tick(self.out, pane)
        self.assertEqual(sig["state"], "blocked-human", pane)
        m = M._hitl_manager(self.out)
        project(m, self._send, "!room", clock=lambda: 1_000_000.0)
        return m.active()

    def test_repaints_around_one_dialog_keep_one_requirement_and_one_card(self):
        ids = {r.id for pane in FRAMES for r in self._poll(pane)}
        self.assertEqual(len(ids), 1, ids)
        self.assertEqual([p["op"] for p in self.sent], ["message"])

    def test_a_different_dialog_is_still_a_new_card(self):
        self._poll(FRAMES[0])
        other = QUESTION.replace("hot reloading", "auto-update")
        active = self._poll(_pane("Sutando: waiting on you      [-]\n", "1 monitor", question=other))
        self.assertEqual(len({r.id for r in active}), 2)
        self.assertEqual([p["op"] for p in self.sent], ["message", "message"])


if __name__ == "__main__":
    unittest.main()
