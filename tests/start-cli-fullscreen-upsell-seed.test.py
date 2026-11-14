#!/usr/bin/env python3
"""The launcher seeds Claude Code's fullscreen upsell as already dismissed (P1-24).

Claude Code shows its "Flicker-free output" dialog in a fresh scoped config
until fullscreenUpsellSeenCount reaches 3; a headless core cannot take the
trial, the pane watcher raised it to the owner as a decision card, and the
answer keys never settled it. The seed program in session-launch.sh writes the
counter "Not now" would have written. Same harness as the dangerous-mode seed.

Run: python3 tests/start-cli-fullscreen-upsell-seed.test.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh"


def _seed_program() -> str:
    m = re.search(r"<<'PY'.*?\n(.*?)\nPY\b", SCRIPT.read_text(), re.S)
    assert m, "seed PY heredoc not found in session-launch.sh"
    prog = m.group(1)
    assert "fullscreenUpsellSeenCount" in prog, "the seed program no longer seeds the upsell counter"
    return prog


def _run(ccd: Path, home: Path) -> dict:
    env = dict(os.environ, _ccd=str(ccd), _cwd="", _accept_bypass="", HOME=str(home))
    r = subprocess.run([sys.executable, "-c", _seed_program()], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads((ccd / ".claude.json").read_text()), r.stdout


class TestUpsellSeed(unittest.TestCase):
    def _case(self, before):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ccd = td / "cfg"
            ccd.mkdir()
            if before is not None:
                (ccd / ".claude.json").write_text(json.dumps(before))
            return _run(ccd, td)

    def test_a_fresh_config_is_seeded_to_three(self):
        cfg, out = self._case(None)
        self.assertEqual(cfg["fullscreenUpsellSeenCount"], 3)
        self.assertIn("upsell-seed", out)

    def test_a_partial_count_is_raised_to_three(self):
        cfg, out = self._case({"fullscreenUpsellSeenCount": 1, "theme": "dark"})
        self.assertEqual(cfg["fullscreenUpsellSeenCount"], 3)
        self.assertEqual(cfg["theme"], "dark", "other keys are preserved")

    def test_a_dismissed_or_higher_count_is_left_alone(self):
        for n in (3, 7):
            cfg, out = self._case({"fullscreenUpsellSeenCount": n, "hasCompletedOnboarding": True,
                                   "hasCompletedClaudeInChromeOnboarding": True})
            self.assertEqual(cfg["fullscreenUpsellSeenCount"], n)
            self.assertNotIn("upsell-seed", out)


if __name__ == "__main__":
    unittest.main()
