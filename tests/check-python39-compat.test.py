#!/usr/bin/env python3
"""Tests for check-python39-compat.py.

The interesting property under test is that the gate CANNOT pass vacuously.
A syntax scan run on the wrong interpreter finds nothing and looks exactly
like a clean tree — so `self_test()` must go RED when the running interpreter
is newer than the 3.9 floor, and the real scan refuses to run in that state.

These tests are therefore version-aware by design: several assertions invert
above 3.10, and that inversion is itself the thing being verified.

Run: python3 tests/check-python39-compat.test.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

#: Lives in tests/ (not beside the script) because the coverage gate
#: discovers ONLY `find tests -name "*.test.py"` — a sibling test in
#: scripts/ never runs under coverage and reports the file as 0%.
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


chk = _load("check_python39_compat", "check-python39-compat.py")

ON_39 = sys.version_info < (3, 10)
MATCH_SRC = "match 1:\n    case 1: pass\n"

#: A control that fails to parse on EVERY interpreter. Substituting it lets the
#: post-self-test branches of run() be exercised on any python, instead of
#: skipping above the floor. Those branches (missing target, empty target,
#: failure reporting) contain no version-dependent logic — they were only
#: unreachable because run() refuses when the real control stops discriminating.
#: Without this the CI coverage job, which runs stock 3.12, skips them and
#: reports ~66% on lines that are fully tested locally under 3.9.
ALWAYS_INVALID = (("always-invalid", "$$$ not python $$$\n"),)


class _FloorShim:
    """Context manager: make self_test() pass on whatever interpreter is running."""

    def __enter__(self):
        self._saved = chk.CONTROL_MUST_FAIL
        chk.CONTROL_MUST_FAIL = ALWAYS_INVALID
        return self

    def __exit__(self, *exc):
        chk.CONTROL_MUST_FAIL = self._saved
        return False




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

    def test_scan_skips_a_missing_dir_but_run_refuses_it(self):
        """`scan()` is the raw walker and stays permissive; `run()` is the
        GATE and must not turn "directory absent" into a green.

        This assertion replaces `test_missing_target_dir_is_not_an_error`,
        which pinned the vacuous behaviour: `--target definitely-missing`
        printed "0 file(s) parse cleanly" and exited 0."""
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(chk.scan(("nope",), Path(td)), [])   # walker: quiet
            import io
            import contextlib
            err = io.StringIO()
            with _FloorShim(), contextlib.redirect_stderr(err):
                rc = chk.run(("nope",), Path(td))                 # gate: loud
            self.assertEqual(rc, 1)
            self.assertIn("do not exist", err.getvalue())

    def test_run_refuses_a_target_that_exists_but_holds_no_python(self):
        """Second vacuous shape: the directory is there and simply empty."""
        import io
        import contextlib
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "empty").mkdir()
            err = io.StringIO()
            with _FloorShim(), contextlib.redirect_stderr(err):
                rc = chk.run(("empty",), repo)
        self.assertEqual(rc, 1, "zero files scanned must never report clean")
        self.assertIn("no .py files", err.getvalue())

    def test_the_real_src_tree_parses_on_this_interpreter(self):
        """The regression this ships to protect."""
        repo = SCRIPTS.parent   # <repo> — scripts/ is one level down
        failures = chk.scan(("src",), repo)
        self.assertEqual(failures, [], "src/ must parse on %s"
                         % ".".join(str(v) for v in sys.version_info[:3]))


class TestRunAndMain(unittest.TestCase):
    """`run()` is the reporting path — the branch that tells a developer WHY
    the build stopped. Tested against a temp tree so the failure case does not
    need a broken file planted in the real src/."""

    def _tree(self, td: str, body: str) -> Path:
        repo = Path(td)
        (repo / "src").mkdir()
        (repo / "src" / "mod.py").write_text(body)
        return repo

    def test_run_returns_zero_on_a_clean_tree(self):
        with tempfile.TemporaryDirectory() as td, _FloorShim():
            self.assertEqual(chk.run(("src",), self._tree(td, "x = 1\n")), 0)

    def test_run_returns_one_and_names_the_file_on_a_bad_tree(self):
        import io
        import contextlib
        with tempfile.TemporaryDirectory() as td:
            repo = self._tree(td, ALWAYS_INVALID[0][1])
            err = io.StringIO()
            with _FloorShim(), contextlib.redirect_stderr(err):
                rc = chk.run(("src",), repo)
        self.assertEqual(rc, 1)
        self.assertIn("mod.py", err.getvalue())
        self.assertIn("do NOT parse", err.getvalue())

    def test_run_refuses_above_the_floor_instead_of_scanning(self):
        """The anti-vacuous property, at the run() level."""
        if ON_39:
            self.skipTest("only observable on a newer interpreter")
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(chk.run(("src",), self._tree(td, MATCH_SRC)), 1)

    def test_main_self_test_flag_matches_self_test(self):
        self.assertEqual(chk.main(["--self-test"]), chk.self_test())

    def test_main_accepts_repeatable_target(self):
        """--target is the documented remedy if the floor ever needs to cover
        skills/; assert it parses rather than leaving the claim untested."""
        ap_ok = chk.main(["--self-test", "--target", "src", "--target", "skills"])
        self.assertEqual(ap_ok, chk.self_test())


class TestSelfTestFailureReporting(unittest.TestCase):
    """The self-test's RED path. On 3.9 it never fires naturally, so it is
    forced by degrading the control lists — which is exactly the real-world
    regression it guards against: a control that stops discriminating."""

    def setUp(self):
        self._mf, self._mp = chk.CONTROL_MUST_FAIL, chk.CONTROL_MUST_PASS
        self.addCleanup(self._restore)

    def _restore(self):
        chk.CONTROL_MUST_FAIL, chk.CONTROL_MUST_PASS = self._mf, self._mp

    def _capture(self, fn):
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = fn()
        return rc, err.getvalue()

    def test_a_must_fail_control_that_stops_discriminating_goes_red(self):
        chk.CONTROL_MUST_FAIL = (("degraded", "x = 1\n"),)
        rc, err = self._capture(chk.self_test)
        self.assertEqual(rc, 1)
        self.assertIn("COMPILED but must not", err)
        self.assertIn("vacuously", err)

    def test_a_broken_harness_rejecting_valid_syntax_goes_red(self):
        chk.CONTROL_MUST_PASS = (("impossible", "def (\n"),)
        rc, err = self._capture(chk.self_test)
        self.assertEqual(rc, 1)
        self.assertIn("harness is broken", err)

    def test_run_aborts_before_scanning_when_the_self_test_is_red(self):
        chk.CONTROL_MUST_FAIL = (("degraded", "x = 1\n"),)
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "src").mkdir()
            (repo / "src" / "bad.py").write_text(MATCH_SRC)
            rc, _ = self._capture(lambda: chk.run(("src",), repo))
        self.assertEqual(rc, 1, "a red self-test must stop the run")


class TestEdgeCases(unittest.TestCase):
    def test_undecodable_file_is_reported_not_crashed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "src").mkdir()
            (repo / "src" / "binary.py").write_bytes(b"\xff\xfe\x00bad bytes\n")
            failures = chk.scan(("src",), repo)
        self.assertEqual(len(failures), 1)
        self.assertIn("undecodable", failures[0][1])

    def test_main_with_no_args_scans_the_real_repo(self):
        """Covers the default path main() takes in CI."""
        with _FloorShim():
            self.assertEqual(chk.main([]), 0)


if __name__ == "__main__":
    print("running under python %s (floor mode: %s)"
          % (".".join(str(v) for v in sys.version_info[:3]),
             "3.9" if ON_39 else "newer — inversion assertions active"))
    unittest.main(verbosity=2)
