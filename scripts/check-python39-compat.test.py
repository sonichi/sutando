#!/usr/bin/env python3
"""Tests for check-python39-compat.py.

The interesting property under test is that the gate CANNOT pass vacuously.
A syntax scan run on the wrong interpreter finds nothing and looks exactly
like a clean tree — so `self_test()` must go RED when the running interpreter
is newer than the 3.9 floor, and the real scan refuses to run in that state.

These tests are therefore version-aware by design: several assertions invert
above 3.10, and that inversion is itself the thing being verified.

Run: python3 scripts/check-python39-compat.test.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


chk = _load("check_python39_compat", "check-python39-compat.py")

ON_39 = sys.version_info < (3, 10)
MATCH_SRC = "match 1:\n    case 1: pass\n"


class TestDetector(unittest.TestCase):
    def test_plain_source_always_compiles(self):
        self.assertTrue(chk.compiles("x = 1\n", "<t>"))

    def test_match_statement_is_rejected_only_below_310(self):
        """The detector's whole value is this discrimination."""
        self.assertEqual(chk.compiles(MATCH_SRC, "<t>"), not ON_39)

    def test_future_annotations_make_310_hints_safe(self):
        """Why the current tree passes: annotations are not evaluated.

        This is the case that made a shipped comment wrong — cron-runner.py
        carries 3.10-style hints AND `from __future__ import annotations`,
        so it parses on 3.9 despite the hint syntax."""
        src = "from __future__ import annotations\ndef f(x: int | None) -> set[int]: ...\n"
        self.assertTrue(chk.compiles(src, "<t>"))


class TestSelfTestCannotPassVacuously(unittest.TestCase):
    def test_self_test_agrees_with_the_running_interpreter(self):
        rc = chk.self_test()
        if ON_39:
            self.assertEqual(rc, 0, "must pass on the 3.9 floor")
        else:
            self.assertEqual(rc, 1, "must FAIL loudly when run too new — "
                                    "otherwise the scan is vacuous")

    def test_every_must_fail_control_is_really_310_syntax(self):
        """Guards the control list itself: each entry has to be something a
        3.9 parser rejects, or it proves nothing."""
        if not ON_39:
            self.skipTest("controls only discriminate on 3.9")
        for label, src in chk.CONTROL_MUST_FAIL:
            self.assertFalse(chk.compiles(src, "<c>"),
                             "control %r must not compile on 3.9" % label)

    def test_must_pass_controls_hold_on_every_version(self):
        for label, src in chk.CONTROL_MUST_PASS:
            self.assertTrue(chk.compiles(src, "<c>"), label)


class TestScan(unittest.TestCase):
    def test_scan_flags_a_planted_310_only_file(self):
        if not ON_39:
            self.skipTest("a 3.10+ interpreter parses the planted file happily")
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "src").mkdir()
            (repo / "src" / "bad.py").write_text(MATCH_SRC)
            (repo / "src" / "good.py").write_text("x = 1\n")
            failures = chk.scan(("src",), repo)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0].name, "bad.py")

    def test_scan_is_clean_on_an_all_good_tree(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "src").mkdir()
            (repo / "src" / "ok.py").write_text(
                "from __future__ import annotations\nx = 1\n")
            self.assertEqual(chk.scan(("src",), repo), [])

    def test_missing_target_dir_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(chk.scan(("nope",), Path(td)), [])

    def test_the_real_src_tree_parses_on_this_interpreter(self):
        """The regression this ships to protect."""
        repo = SCRIPTS.parent
        failures = chk.scan(("src",), repo)
        self.assertEqual(failures, [], "src/ must parse on %s"
                         % ".".join(str(v) for v in sys.version_info[:3]))


if __name__ == "__main__":
    print("running under python %s (floor mode: %s)"
          % (".".join(str(v) for v in sys.version_info[:3]),
             "3.9" if ON_39 else "newer — inversion assertions active"))
    unittest.main(verbosity=2)
