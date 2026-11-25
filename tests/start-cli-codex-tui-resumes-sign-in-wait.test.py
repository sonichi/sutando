#!/usr/bin/env python3
"""The Codex launcher, once a TUI core is up, resumes a sign-in a runtime card is waiting for; an
app-server seat resumes it itself (seat.py), so the launcher leaves that path alone.

Real launcher in the fixture of tests/codex-app-server-core-launcher.test.py, with a stub
runtime_card.py that records each call. Run: python3 tests/start-cli-codex-tui-resumes-sign-in-wait.test.py
"""
from __future__ import annotations

import importlib.util
import time
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "codex_seat_fixture", Path(__file__).resolve().parent / "codex-app-server-core-launcher.test.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

STUB = ("import sys, pathlib\n"
        "with open(pathlib.Path(sys.argv[2], 'resumed.txt'), 'a') as f:\n"
        "    f.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "print('resumed')\n")
# The seat fixture's tmux, except that a new window holds a TUI core unless it runs the seat.
OLD_WINDOW = '    echo seat > "$S.core"; seat_ready "$@"\n'
assert OLD_WINDOW in _mod.SEAT_TMUX
HEAL_TMUX = _mod.SEAT_TMUX.replace(OLD_WINDOW, (
    '    kind=codex; for a in "$@"; do case "$a" in */codex-seat) kind=seat ;; esac; done\n'
    '    echo "$kind" > "$S.core"; [ "$kind" = seat ] && seat_ready "$@"\n'))
TUI, SEAT = {"SUTANDO_CODEX_APP_SERVER": "0"}, {"SUTANDO_CODEX_APP_SERVER": "1"}


class ResumesTheSignInWait(unittest.TestCase):
    def fixture(self, waiting=True, seat_tmux=False):
        f = _mod.Fixture(self, seat_tmux=seat_tmux)
        if seat_tmux:
            (f.h.bin / "tmux").write_text(HEAL_TMUX)
        card = f.root / "skills" / "runtime-switch" / "scripts"
        card.mkdir(parents=True)
        (card / "runtime_card.py").write_text(STUB)
        if waiting:
            (f.workspace / "state" / "runtime-card-signin.json").write_text("{}")
        return f

    @staticmethod
    def resumed(f):
        return [f"--workspace {f.workspace.resolve()} resume"]

    def calls(self, f, wait_s=8.0):
        out = f.workspace / "resumed.txt"
        end = time.time() + wait_s
        while time.time() < end and not out.exists():
            time.sleep(0.1)
        time.sleep(0.3)   # a second, duplicate call would land within this
        lines = out.read_text().splitlines() if out.exists() else []
        # The workspace argument is compared resolved: the launcher may name it through /private.
        return [" ".join(str(Path(w).resolve()) if i == 1 else w for i, w in enumerate(ln.split())) for ln in lines]

    def test_a_tui_boot_resumes_a_wait_on_record_once_and_logs_its_output(self):
        f = self.fixture()
        run = f.run(**TUI)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.calls(f), self.resumed(f))
        self.assertIn("resumed", (f.workspace / "logs" / "runtime-card.log").read_text())

    def test_a_tui_core_window_healed_in_place_resumes_it_too(self):
        f = self.fixture(seat_tmux=True)
        f.core("gone")
        run = f.run(**TUI)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("healing core window", run.stderr)
        self.assertEqual(self.calls(f), self.resumed(f))

    def test_with_nothing_waited_for_the_card_script_is_not_run(self):
        f = self.fixture(waiting=False)
        run = f.run(**TUI)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.calls(f, 1.5), [])

    def test_a_seat_boot_is_left_to_the_seat(self):
        for core in (None, "gone"):
            with self.subTest(core=core):
                f = self.fixture(seat_tmux=True)
                if core:
                    f.core(core)
                run = f.run(**SEAT)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertIn("codex-seat", f.calls())
                self.assertEqual(self.calls(f, 1.5), [], "the launcher would arm a second waiter beside the seat's")


if __name__ == "__main__":
    unittest.main()
