#!/usr/bin/env python3
"""Regression test: `check_codex_presence` must key on PATH resolution, not on
an engine-tree location, and must distinguish a wiped binary from one that was
never installed.

Cases:
  a) codex on PATH                          -> ok, detail names the resolved path
  b) absent, ~/.codex present               -> warn, "wiped binary", remedy given
  c) absent, ~/.codex absent                -> warn, "never installed"
  d) resolved OUTSIDE the engine tree       -> still ok (the second known
                                               topology; a tree-keyed probe
                                               would be wrong on both hosts)
  e) the probe is registered in run_checks  -> a probe nobody calls reports
                                               nothing

Run: python3 tests/health-check-codex-presence.test.py
Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hc = _load_module()


class CodexPresence(unittest.TestCase):
    def test_present_is_ok_and_names_the_path(self):
        r = hc.check_codex_presence(which=lambda _: "/Users/x/.local/bin/codex")
        self.assertEqual(r["status"], "ok")
        self.assertIn("/Users/x/.local/bin/codex", r["detail"])

    def test_outside_the_engine_tree_is_still_ok(self):
        # Both known hosts resolve codex outside the engine tree; a probe keyed
        # to the tree would warn on a perfectly working install.
        r = hc.check_codex_presence(which=lambda _: "/opt/homebrew/bin/codex")
        self.assertEqual(r["status"], "ok")

    def test_absent_with_config_reads_as_wiped_and_gives_the_remedy(self):
        with patch.object(Path, "is_dir", return_value=True):
            r = hc.check_codex_presence(which=lambda _: None)
        self.assertEqual(r["status"], "warn")
        self.assertIn("wiped binary", r["detail"])
        self.assertIn("--prefix ~/.local", r["detail"])
        self.assertIn("non-owner", r["detail"])

    def test_absent_without_config_reads_as_never_installed(self):
        with patch.object(Path, "is_dir", return_value=False):
            r = hc.check_codex_presence(which=lambda _: None)
        self.assertEqual(r["status"], "warn")
        self.assertIn("never installed", r["detail"])
        self.assertNotIn("wiped binary", r["detail"])

    def test_probe_is_registered(self):
        src = (REPO / "src" / "health-check.py").read_text()
        self.assertIn("checks.append(check_codex_presence())", src,
                      "probe exists but nothing calls it")


if __name__ == "__main__":
    unittest.main(verbosity=2)
