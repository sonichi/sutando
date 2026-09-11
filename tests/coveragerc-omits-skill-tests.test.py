#!/usr/bin/env python3
"""`.coveragerc` must exclude skill TEST dirs while still measuring skill SCRIPTS.

Run: python3 tests/coveragerc-omits-skill-tests.test.py

The omit lines this pins were added because `source = skills` with `omit = tests/*`
measured `skills/<name>/tests/*.test.py` as production: assertion lines score 100%
and dilute the diff gate. Nothing failed when they were removed, so the fix was
unpinned -- a contract with no control is a comment.

It asserts the PATTERNS decide correctly rather than reading a generated XML, so it
needs no coverage run and still flips the moment an omit line goes.
"""
import configparser
import fnmatch
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RC = REPO / ".coveragerc"

MUST_BE_OMITTED = [
    "skills/worker-pool/tests/pool-delivery.test.py",
    "skills/worker-pool/tests/pool-bindings-roster.test.py",
    "skills/any-skill/tests/whatever.test.py",
]
MUST_BE_MEASURED = [
    "skills/worker-pool/scripts/pool_delivery.py",
    "skills/worker-pool/scripts/pool_roster.py",
    "src/task_priority.py",
]


def _omit_patterns():
    cp = configparser.ConfigParser()
    cp.read(RC)
    raw = cp.get("run", "omit", fallback="")
    return [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _omitted(path, patterns):
    return any(fnmatch.fnmatch(path, p) for p in patterns)


class TestCoveragercOmitsSkillTests(unittest.TestCase):
    def setUp(self):
        self.patterns = _omit_patterns()
        self.assertTrue(self.patterns, ".coveragerc declares no omit patterns at all")

    def test_skill_test_files_are_omitted(self):
        for p in MUST_BE_OMITTED:
            self.assertTrue(_omitted(p, self.patterns),
                            f"{p} is MEASURED as production source; assertion lines will "
                            f"dilute the diff gate. omit={self.patterns}")

    def test_skill_scripts_are_still_measured(self):
        for p in MUST_BE_MEASURED:
            self.assertFalse(_omitted(p, self.patterns),
                             f"{p} is omitted — the fix over-reached and stopped "
                             f"measuring production code. omit={self.patterns}")

    def test_skills_is_still_a_source_root(self):
        cp = configparser.ConfigParser(); cp.read(RC)
        src = cp.get("run", "source", fallback="")
        self.assertIn("skills", src.split(),
                      "skills dropped from source — omitting its tests is meaningless "
                      "if nothing under skills is measured at all")


if __name__ == "__main__":
    unittest.main(verbosity=2)
