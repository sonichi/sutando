#!/usr/bin/env python3
"""Tests for src/agent/codex/cli/trust-seed.py and its call in the Codex launcher.

A fresh Codex core parks at "Trust this folder?" until answered; the launcher seeds
the answer into $CODEX_HOME/config.toml. The seed must add the entry Codex writes on
"Trust and continue", keep an existing decision, and never corrupt the config.

Run: python3 tests/codex-trust-seed.test.py
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.normpath(os.path.join(_HERE, ".."))
_SEED = os.path.join(_REPO, "src", "agent", "codex", "cli", "trust-seed.py")
_LAUNCHER = os.path.join(_REPO, "src", "agent", "codex", "cli", "start-cli.sh")
_spec = importlib.util.spec_from_file_location("codex_trust_seed", _SEED)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
seed = _mod.seed

# The engine path from a real macOS install: spaces must survive the TOML key.
_DIR = "/Users/admin/Library/Application Support/space.ag2.app/engine/sutando"


def _projects(path):
    with open(path, "rb") as f:
        return tomllib.load(f).get("projects", {})


class SeedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = os.path.join(self._tmp.name, "codex", "config.toml")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text):
        os.makedirs(os.path.dirname(self.cfg), exist_ok=True)
        with open(self.cfg, "w", encoding="utf-8") as f:
            f.write(text)

    def test_missing_config_gets_the_trusted_entry(self):
        self.assertEqual(seed(self.cfg, _DIR), "added")
        self.assertEqual(_projects(self.cfg)[_DIR], {"trust_level": "trusted"})

    def test_existing_config_keeps_its_content(self):
        self._write('model = "gpt-5"\n\n[mcp_servers.x]\ncommand = "y"\n')
        self.assertEqual(seed(self.cfg, _DIR), "added")
        with open(self.cfg, "rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["model"], "gpt-5")
        self.assertEqual(data["mcp_servers"]["x"]["command"], "y")
        self.assertEqual(data["projects"][_DIR]["trust_level"], "trusted")

    def test_second_run_is_a_no_op(self):
        seed(self.cfg, _DIR)
        with open(self.cfg) as f:
            before = f.read()
        self.assertEqual(seed(self.cfg, _DIR), "present")
        with open(self.cfg) as f:
            self.assertEqual(f.read(), before)

    def test_an_existing_decision_is_left_alone(self):
        self._write(f'[projects."{_DIR}"]\ntrust_level = "untrusted"\n')
        self.assertEqual(seed(self.cfg, _DIR), "present")
        self.assertEqual(_projects(self.cfg)[_DIR]["trust_level"], "untrusted")

    def test_other_projects_do_not_count_as_this_one(self):
        self._write('[projects."/elsewhere"]\ntrust_level = "trusted"\n')
        self.assertEqual(seed(self.cfg, _DIR), "added")
        self.assertEqual(set(_projects(self.cfg)), {"/elsewhere", _DIR})

    def test_quotes_and_backslashes_in_the_path_stay_valid_toml(self):
        odd = '/tmp/a "quoted" \\ dir'
        self.assertEqual(seed(self.cfg, odd), "added")
        self.assertEqual(_projects(self.cfg)[odd]["trust_level"], "trusted")

    def test_unparseable_config_is_not_touched(self):
        self._write("this is = = not toml\n")
        self.assertTrue(seed(self.cfg, _DIR).startswith("skipped: unreadable"))
        with open(self.cfg) as f:
            self.assertEqual(f.read(), "this is = = not toml\n")

    def test_an_unwritable_config_is_skipped_not_raised(self):
        blocker = os.path.join(self._tmp.name, "a-file")
        with open(blocker, "w") as f:
            f.write("x")
        # A path under a regular file cannot be created: a real OSError, no mocking.
        self.assertTrue(seed(os.path.join(blocker, "codex", "config.toml"), _DIR)
                        .startswith("skipped: not writable"))

    def test_main_reports_the_status_and_never_fails(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(_mod.main(["trust-seed.py", self.cfg, _DIR]), 0)
            self.assertEqual(_mod.main(["trust-seed.py"]), 0)
        self.assertEqual(out.getvalue().splitlines()[0], "added")
        self.assertTrue(out.getvalue().splitlines()[1].startswith("skipped: usage"))

    def test_cli_prints_one_status_and_exits_zero(self):
        r = subprocess.run([sys.executable, _SEED, self.cfg, _DIR], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "added"))
        r = subprocess.run([sys.executable, _SEED], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith("skipped:"))


class LauncherWiringTest(unittest.TestCase):
    """Source-tied: a genuine boot seeds with the launcher's one resolved interpreter."""

    def setUp(self):
        with open(_LAUNCHER, encoding="utf-8") as f:
            self.src = f.read()

    def test_seed_reuses_the_single_resolved_interpreter(self):
        self.assertRegex(self.src, r'"\$_HB_PY" "\$REPO/src/agent/codex/cli/trust-seed\.py"')
        self.assertEqual(self.src.count("resolve_python "), 1, "one interpreter resolution per launch")

    def test_seed_targets_codex_home_and_the_working_dir(self):
        self.assertIn('"${CODEX_HOME:-$HOME/.codex}/config.toml" "$WORKING_DIR"', self.src)

    def test_seed_runs_after_resolution_and_before_every_codex_launch(self):
        resolved = self.src.index("\nresolve_heartbeat_python\n")
        reuse_exit = self.src.index('echo "$SESSION already running (codex)."')
        seeded = self.src.index("trust-seed.py")
        launches = [m.start() for m in re.finditer(r'codex "\$\{CODEX_ARGS\[@\]\}"', self.src)]
        self.assertTrue(launches)
        self.assertLess(resolved, seeded)
        self.assertLess(reuse_exit, seeded, "an attached, already-running core is past the dialog")
        self.assertTrue(all(seeded < at for at in launches))


if __name__ == "__main__":
    unittest.main()
