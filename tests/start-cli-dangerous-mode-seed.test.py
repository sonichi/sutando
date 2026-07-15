#!/usr/bin/env python3
"""Hermetic test for the dangerous-mode seed in start-cli.sh (#2098).

On a working-dir launch, start-cli.sh seeds `skipDangerousModePermissionPrompt`
into `<ccd>/settings.json` ONLY when `SUTANDO_ACCEPT_BYPASS_PERMISSIONS` is set —
the dedicated opt-in the bundled desktop's launch-sutando.sh exports for the
detached, no-TTY core. `--dangerously-skip-permissions` does NOT bypass Claude
Code's "Bypass Permissions mode / Yes, I accept" acknowledgement, so a headless
core hangs on it forever unless the flag is pre-seeded (owner-hit 2026-07-14).

This extracts the ACTUAL embedded seed program from start-cli.sh (source-tied —
fails if the block is renamed/removed) and drives it with controlled env,
asserting the four behaviors the review asked for:
  - env set   ⇒ settings.json gains exactly `skipDangerousModePermissionPrompt: true`
  - env unset ⇒ settings.json is never created / untouched
  - an existing settings.json is MERGED, not clobbered
  - a second run is idempotent (stays true, no error)

Run: python3 tests/start-cli-dangerous-mode-seed.test.py
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
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"


def _seed_program() -> str:
    """Extract the embedded `python3 - <<'PY' … PY` seed program verbatim."""
    txt = SCRIPT.read_text()
    m = re.search(r"<<'PY'.*?\n(.*?)\nPY\b", txt, re.S)
    assert m, "seed PY heredoc not found in start-cli.sh — did the seed block move?"
    prog = m.group(1)
    assert "skipDangerousModePermissionPrompt" in prog, \
        "extracted seed program no longer references skipDangerousModePermissionPrompt"
    return prog


def _run(program: str, ccd: Path, accept_bypass: bool, home: Path):
    """Run the seed program with controlled env; return the parsed settings.json or None."""
    env = dict(os.environ)
    env["_ccd"] = str(ccd)
    env["_cwd"] = ""  # no trust-seed dir — isolate the dangerous-mode path
    env["_accept_bypass"] = "1" if accept_bypass else ""
    env["HOME"] = str(home)  # so the ~/.claude.json read is hermetic (absent)
    r = subprocess.run([sys.executable, "-c", program],
                       env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"seed program errored: {r.stderr}"
    sp = ccd / "settings.json"
    return json.loads(sp.read_text()) if sp.exists() else None


class TestDangerousModeSeed(unittest.TestCase):
    def setUp(self):
        self.prog = _seed_program()

    def test_env_set_seeds_the_key(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ccd = td / "cfg"
            ccd.mkdir()
            st = _run(self.prog, ccd, accept_bypass=True, home=td)
            self.assertIsNotNone(st, "settings.json should be created when opted in")
            self.assertIs(st.get("skipDangerousModePermissionPrompt"), True)

    def test_env_unset_leaves_settings_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ccd = td / "cfg"
            ccd.mkdir()
            st = _run(self.prog, ccd, accept_bypass=False, home=td)
            # No opt-in → the seed must not create settings.json at all.
            self.assertIsNone(st, "settings.json must not be created without the opt-in")

    def test_existing_settings_merged_not_clobbered(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ccd = td / "cfg"
            ccd.mkdir()
            (ccd / "settings.json").write_text(json.dumps(
                {"env": {"FOO": "bar"}, "permissions": {"allow": ["x"]}}))
            st = _run(self.prog, ccd, accept_bypass=True, home=td)
            self.assertIs(st.get("skipDangerousModePermissionPrompt"), True)
            # Pre-existing keys survive.
            self.assertEqual(st.get("env"), {"FOO": "bar"})
            self.assertEqual(st.get("permissions"), {"allow": ["x"]})

    def test_idempotent_second_run(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ccd = td / "cfg"
            ccd.mkdir()
            _run(self.prog, ccd, accept_bypass=True, home=td)
            st = _run(self.prog, ccd, accept_bypass=True, home=td)
            self.assertEqual(st, {"skipDangerousModePermissionPrompt": True})


if __name__ == "__main__":
    unittest.main()
