#!/usr/bin/env python3
"""The report must name the workspace it measured.

Every probe resolves against `WORKSPACE_DIR`, which `resolve_workspace()`
derives from the working directory. On a host with two checkouts the same
command produces two different verdicts and says nothing about which tree it
read — measured: 10 warnings from one, 19 from the other, seconds apart, and
NOT a superset (nine names appear, two disappear). The dangerous cell was
`task-watcher`: `⚠ ... wrote no PID sentinel` against one workspace and
`✓ streaming watcher alive` against the other, for the SAME pid. The proactive
loop reads that probe to decide whether to stop or start a watcher.
"""
import importlib.util
import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(ROOT, "src", "health-check.py")

_spec = importlib.util.spec_from_file_location("health_check_ws_mod", SRC)
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)


class WorkspaceNamedTests(unittest.TestCase):
    def test_module_resolves_a_concrete_workspace(self):
        """WORKSPACE_DIR is what every probe reads; it must be a real path."""
        self.assertTrue(str(hc.WORKSPACE_DIR))
        self.assertTrue(os.path.isabs(str(hc.WORKSPACE_DIR)),
                        f"not absolute: {hc.WORKSPACE_DIR!r}")

    def test_header_prints_the_resolved_workspace(self):
        """Structural, matching this file's sibling test for main()-glue: the
        header must interpolate WORKSPACE_DIR, not a literal or a re-derivation."""
        with open(SRC) as fh:
            src = fh.read()
        needle = 'print(f"workspace: {WORKSPACE_DIR}")'
        self.assertTrue(needle in src, f"header does not print {needle}")

    def test_header_line_precedes_the_rule(self):
        """It has to be inside the human-readable block, above the divider —
        printed after it, a reader scanning the top of the report still misses it."""
        with open(SRC) as fh:
            src = fh.read()
        ws = src.find('print(f"workspace: {WORKSPACE_DIR}")')
        title = src.find('print("Sutando Health Check")')
        self.assertNotEqual(ws, -1, "no workspace line to position")
        self.assertNotEqual(title, -1, "no title line to position against")
        rule = src.find('print("=" * 40)', title)
        self.assertNotEqual(rule, -1, "no divider after the title")
        self.assertLess(title, ws, "workspace line must follow the title")
        self.assertLess(ws, rule, "workspace line must precede the divider")

    def test_control_the_probe_can_fail(self):
        """A structural assertion that matches nothing scores 0 by construction."""
        with open(SRC) as fh:
            src = fh.read()
        self.assertFalse('print(f"workspace: {NO_SUCH_NAME}")' in src,
                         "the negative control matched — the probe is too loose")
        self.assertTrue('print("Sutando Health Check")' in src,
                        "the positive control missed — the probe cannot hit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
