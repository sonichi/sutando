#!/usr/bin/env python3
"""A headless install must stay healthy; a host that asked for the app via
launchd must not go quiet when the binary is gone."""
from __future__ import annotations

import ast
import inspect
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src" / "health-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("hc", SRC)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


hc = _load()


class MenubarAppStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.dev = root / "dev" / "Sutando"
        self.app = root / "app" / "Sutando"
        self.plist = root / "com.sutando.menubar.plist"
        self.chips = root / "contextual-chips.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, p: Path):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")

    # --- the control the review asked for --------------------------------

    def test_a_HEADLESS_host_is_not_unhealthy(self):
        # Linux/cloud core: no binaries, no plist, not macOS. The OSS core is
        # deliberately headless, so this is a supported install, not a fault.
        self.assertEqual(
            hc.menubar_app_state(self.dev, self.app, self.plist, is_macos=False),
            "not-applicable")

    def test_macos_without_the_optional_app_is_not_unhealthy(self):
        # An operator who simply never installed the menu-bar app.
        self.assertEqual(
            hc.menubar_app_state(self.dev, self.app, self.plist, is_macos=True),
            "not-applicable")

    # --- but a configured host must stay visible --------------------------

    def test_a_host_that_ASKED_for_the_app_and_lacks_it_is_flagged(self):
        # launchd job installed, binary gone: that is a broken install, and the
        # bug this file exists for is that it used to emit no row at all.
        self._touch(self.plist)
        self.assertEqual(
            hc.menubar_app_state(self.dev, self.app, self.plist, is_macos=True),
            "expected-missing")

    def test_a_plist_on_a_non_macos_host_is_still_not_applicable(self):
        self._touch(self.plist)
        self.assertEqual(
            hc.menubar_app_state(self.dev, self.app, self.plist, is_macos=False),
            "not-applicable")

    # --- present in either location -> the existing probe runs -------------

    def test_dev_build_present_is_installed(self):
        self._touch(self.dev)
        self.assertEqual(
            hc.menubar_app_state(self.dev, self.app, self.plist, is_macos=True),
            "installed")

    def test_installed_bundle_present_is_installed(self):
        self._touch(self.app)
        self.assertEqual(
            hc.menubar_app_state(self.dev, self.app, self.plist, is_macos=False),
            "installed")

    def test_installed_wins_over_a_stale_plist(self):
        self._touch(self.plist)
        self._touch(self.dev)
        self.assertEqual(
            hc.menubar_app_state(self.dev, self.app, self.plist, is_macos=True),
            "installed")


    # --- the control: a FRESHLY INITIALIZED workspace ---------------------

    def test_an_initialized_workspace_with_seeded_chips_stays_silent(self):
        # src/init.sh seeds state/contextual-chips.json with a CURRENT
        # timestamp, so chips recency cannot mean "the app ran here".
        self._touch(self.chips)
        self.assertEqual(
            hc.menubar_app_state(self.dev, self.app, self.plist, is_macos=True),
            "not-applicable")

    def test_the_probe_never_consults_contextual_chips(self):
        # Guards the reintroduction, not just the current return value: any
        # chips-derived signal warns on every newly initialized macOS install.
        body = inspect.getsource(hc.menubar_app_state)
        self.assertNotIn("chips", body)
        self.assertNotIn("contextual", body)


class CallSiteTest(unittest.TestCase):
    """The pure function is useless if run_all_checks stops consulting it."""

    src = SRC.read_text(encoding="utf-8")

    def _guard(self) -> ast.If:
        for node in ast.walk(ast.parse(self.src)):
            if isinstance(node, ast.If):
                names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
                if "_menubar" in names:
                    return node
        raise AssertionError("run_all_checks no longer branches on _menubar")

    def test_the_call_site_branches_on_the_state(self):
        self.assertTrue(self._guard().orelse, "no expected-missing branch")

    def test_not_applicable_emits_NO_row(self):
        # The whole point of the fix: silence is correct here and only here.
        tail = self.src.split("_menubar ==")[-1]
        block = tail.split("# Battery and memory health checks")[0]
        self.assertNotIn("not-applicable", block,
                         "not-applicable must fall through without appending a check")

    def test_expected_missing_emits_a_non_ok_row(self):
        self.assertIn('"expected-missing"', self.src)
        seg = self.src.split('elif _menubar == "expected-missing":')[1][:600]
        self.assertIn('"sutando-app"', seg)
        self.assertIn('"warn"', seg)


class RunAllChecksActuallyEmitsTheAbsentRow(unittest.TestCase):
    """The AST guard above proves run_all_checks BRANCHES on _menubar; it cannot
    prove the branch emits anything. This drives run_all_checks for real."""

    def _rows(self, state):
        with patch.object(hc, "menubar_app_state", return_value=state):
            checks = hc.run_all_checks()
        return [c for c in checks if isinstance(c, dict) and c.get("name") == "sutando-app"]

    def test_expected_missing_emits_a_warn_row_naming_both_paths(self):
        rows = self._rows("expected-missing")
        self.assertEqual(len(rows), 1, "a host that ASKED for the app must get exactly one row")
        self.assertEqual(rows[0]["status"], "warn")
        detail = rows[0]["detail"]
        self.assertIn("no binary exists", detail)
        self.assertIn(hc.MENUBAR_LABEL, detail, "the row must name the launchd job that asked")
        self.assertIn("UNKNOWN", detail, "silence and 'ok' are the two things this must not say")

    def test_CONTROL_not_applicable_emits_NO_row(self):
        """Without this the assertion above passes for a probe that emits a row
        unconditionally — the headless case is the whole point of the PR."""
        self.assertEqual(self._rows("not-applicable"), [],
                         "a headless host must get no sutando-app row at all")


if __name__ == "__main__":
    unittest.main(verbosity=2)
