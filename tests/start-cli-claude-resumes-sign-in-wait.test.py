#!/usr/bin/env python3
"""The Claude launcher, once the core is up, resumes a sign-in a runtime card is waiting for.

Real launcher on a private tmux socket (the notifier tests' Harness), with a stub runtime_card.py that
records how it was called. Run: python3 tests/start-cli-claude-resumes-sign-in-wait.test.py
"""
from __future__ import annotations

import importlib.util
import time
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "notifier_harness", Path(__file__).resolve().parent / "start-cli-claude-task-notifier.test.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

STUB = "import sys, pathlib\npathlib.Path(sys.argv[2], 'resumed.txt').write_text(' '.join(sys.argv[1:]))\nprint('resumed')\n"


class ResumesTheSignInWait(unittest.TestCase):
    def setUp(self):
        self.h = _mod.Harness()
        self.ws = self.h.td / "ws"
        card = self.h.root / "skills" / "runtime-switch" / "scripts"
        card.mkdir(parents=True)
        (card / "runtime_card.py").write_text(STUB)

    def tearDown(self):
        self.h.close()

    def seen(self, wait_s=8.0):
        end = time.time() + wait_s
        while time.time() < end and not (self.ws / "resumed.txt").exists():
            time.sleep(0.1)
        return (self.ws / "resumed.txt").read_text() if (self.ws / "resumed.txt").exists() else None

    def test_a_wait_on_record_is_resumed_after_the_core_is_up_and_its_output_is_logged(self):
        (self.ws / "state" / "runtime-card-signin.json").write_text("{}")
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.seen(), f"--workspace {self.ws} resume")
        self.assertIn("resumed", (self.ws / "logs" / "runtime-card.log").read_text())

    def test_with_nothing_waited_for_the_card_script_is_not_run(self):
        run = self.h.launch()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIsNone(self.seen(1.5))


if __name__ == "__main__":
    unittest.main()
