#!/usr/bin/env python3
"""`coverage_unmeasured`: name the changed files the gate never looked at.

diff-cover reports only on files present in coverage.xml. A changed file
outside `[run] source` is therefore neither covered nor uncovered — the gate
reports success without having examined it, which is indistinguishable from
success on a well-tested file.
"""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _mod():
    spec = importlib.util.spec_from_file_location(
        "cov_unmeasured", REPO / "scripts" / "coverage_unmeasured.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


XML = """<?xml version="1.0" ?>
<coverage><packages><package><classes>
  <class filename="health-check.py"/>
  <class filename="task_priority.py"/>
</classes></package></packages></coverage>
"""


class UnmeasuredFiles(unittest.TestCase):
    def setUp(self):
        self.m = _mod()

    def test_a_file_outside_source_is_reported(self):
        # The live case: packages/ is not in `[run] source`, so a bridge change
        # passed the gate without a single one of its lines being examined.
        out = self.m.unmeasured(
            ["src/health-check.py", "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py"],
            {"health-check.py", "task_priority.py"}, [])
        self.assertEqual(out, ["packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py"])

    def test_a_measured_file_is_not_reported(self):
        # coverage.xml filenames are relative to a <source> root, so the match
        # is by suffix — `src/health-check.py` vs `health-check.py`.
        self.assertEqual(
            self.m.unmeasured(["src/health-check.py"], {"health-check.py"}, []), [])

    def test_a_deliberately_omitted_file_is_not_reported(self):
        # tests/ is in `[run] omit` on purpose; reporting it every run would
        # train the reader to skip the section that matters.
        self.assertEqual(
            self.m.unmeasured(["tests/foo.test.py"], {"health-check.py"}, ["tests/*"]), [])

    def test_a_suffix_collision_does_not_mask_an_unmeasured_file(self):
        # "endswith" alone would let a measured `a/util.py` vouch for an
        # unmeasured `elsewhere/xutil.py`; the separator is what prevents it.
        self.assertEqual(
            self.m.unmeasured(["pkg/xutil.py"], {"util.py"}, []), ["pkg/xutil.py"])

    def test_nothing_changed_reports_nothing(self):
        self.assertEqual(self.m.unmeasured([], {"health-check.py"}, []), [])


class OmitGlobsComeFromCoveragerc(unittest.TestCase):
    """Read coverage's own config — a restated copy drifts the moment it is edited."""

    def setUp(self):
        self.m = _mod()

    def test_the_repo_rcfile_is_parsed(self):
        globs = self.m.omit_globs(REPO / ".coveragerc")
        self.assertIn("tests/*", globs)

    def test_a_missing_rcfile_yields_no_globs_rather_than_raising(self):
        self.assertEqual(self.m.omit_globs(REPO / "does-not-exist.cfg"), [])

    def test_omitted_matching_handles_a_leading_slash_pattern(self):
        self.assertTrue(self.m.is_omitted("a/node_modules/x.py", ["*/node_modules/*"]))
        self.assertFalse(self.m.is_omitted("src/x.py", ["*/node_modules/*"]))


class MeasuredFilesParsing(unittest.TestCase):
    def setUp(self):
        self.m = _mod()

    def test_filenames_are_read_from_the_report(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "coverage.xml"
            p.write_text(XML)
            self.assertEqual(self.m.measured_files(p),
                             {"health-check.py", "task_priority.py"})

    def test_an_unreadable_report_yields_an_empty_set_not_an_exception(self):
        # Fail-open: the gate's verdict must never depend on this helper.
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "coverage.xml"
            bad.write_text("not xml at all")
            self.assertEqual(self.m.measured_files(bad), set())
            self.assertEqual(self.m.measured_files(Path(td) / "absent.xml"), set())


class MalformedConfig(unittest.TestCase):
    def setUp(self):
        self.m = _mod()

    def test_an_unparsable_rcfile_yields_no_globs_rather_than_raising(self):
        # Same fail-open contract as the report: the gate's verdict must never
        # depend on this helper, so a broken config loses the exemptions rather
        # than the run.
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / ".coveragerc"
            bad.write_text("[run\nomit = tests/*\n")   # unclosed section header
            self.assertEqual(self.m.omit_globs(bad), [])


class ChangedFilesComeFromGit(unittest.TestCase):
    """`changed_py` runs git in the CWD, so drive it against a real repo."""

    def setUp(self):
        self.m = _mod()

    def _repo(self, td: Path) -> Path:
        import os
        import subprocess
        r = td / "repo"
        r.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
        run = lambda *a: subprocess.run(["git", "-C", str(r), *a],  # noqa: E731
                                        check=True, capture_output=True, env=env)
        subprocess.run(["git", "init", "-q", "-b", "base", str(r)],
                       check=True, capture_output=True)
        (r / "kept.py").write_text("x = 1\n")
        run("add", "-A"); run("commit", "-qm", "base")
        run("switch", "-qc", "topic")
        (r / "added.py").write_text("y = 2\n")
        (r / "notpython.md").write_text("doc\n")
        run("add", "-A"); run("commit", "-qm", "topic")
        return r

    def test_only_changed_python_files_are_returned(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            r = self._repo(Path(td))
            cwd = os.getcwd()
            try:
                os.chdir(r)
                self.assertEqual(self.m.changed_py("base"), ["added.py"])
            finally:
                os.chdir(cwd)

    def test_a_failing_git_yields_no_files_rather_than_raising(self):
        # An unknown base ref (or a non-repo cwd) must not take the gate down.
        import os
        with tempfile.TemporaryDirectory() as td:
            r = self._repo(Path(td))
            cwd = os.getcwd()
            try:
                os.chdir(r)
                self.assertEqual(self.m.changed_py("no-such-ref"), [])
            finally:
                os.chdir(cwd)


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.m = _mod()

    def test_too_few_arguments_exits_nonzero_without_printing_paths(self):
        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self.m.main(["coverage_unmeasured.py"])
        self.assertEqual(rc, 2)
        self.assertIn("Usage", err.getvalue())

    def test_it_prints_one_unmeasured_path_per_line(self):
        import contextlib
        import io
        import os
        with tempfile.TemporaryDirectory() as td:
            xml = Path(td) / "coverage.xml"
            xml.write_text(XML)
            rc_file = Path(td) / ".coveragerc"
            rc_file.write_text("[run]\nomit =\n    tests/*\n")
            self.m.changed_py = lambda base: ["packages/x/bridge.py", "tests/a.test.py",
                                              "src/health-check.py"]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = self.m.main(["prog", "origin/main", str(xml), "--rcfile", str(rc_file)])
        self.assertEqual(rc, 0)
        # tests/ omitted on purpose; health-check.py is in the report by suffix.
        self.assertEqual(out.getvalue().split(), ["packages/x/bridge.py"])
        self.assertNotIn("tests/a.test.py", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
