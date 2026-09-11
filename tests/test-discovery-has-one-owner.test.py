#!/usr/bin/env python3
"""Both runners must DELEGATE test discovery to one helper, not re-implement it.

Run: python3 tests/test-discovery-has-one-owner.test.py

Four review rounds of parser edge cases came from two runners each writing their
own `find` and a guard reading that text back. The helper is now the single
owner; these pins stop a runner re-growing its own copy, which is what made the
drift invisible before.
"""
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DISCOVER = REPO / "scripts" / "discover-python-tests.sh"
RUNNERS = [REPO / ".github" / "workflows" / "ci.yml",
           REPO / "scripts" / "coverage-gate.sh"]
INLINE_FIND = re.compile(r"^[^#]*\bfind\b[^#\n]*-name\s+'\*\.test\.py'")


class TestDiscoveryHasOneOwner(unittest.TestCase):
    def test_the_helper_exists_and_is_executable(self):
        self.assertTrue(DISCOVER.is_file(), f"{DISCOVER} missing")
        self.assertTrue(DISCOVER.stat().st_mode & 0o111, f"{DISCOVER} is not executable")

    def test_every_runner_invokes_the_helper(self):
        for r in RUNNERS:
            self.assertIn(DISCOVER.name, r.read_text(),
                          f"{r.name} does not call {DISCOVER.name} — it is discovering "
                          "tests some other way, and the guard cannot see how")

    def test_no_runner_reimplements_discovery(self):
        for r in RUNNERS:
            offenders = [ln.strip() for ln in r.read_text().splitlines() if INLINE_FIND.match(ln)]
            self.assertEqual(offenders, [],
                             f"{r.name} has its own test-discovery find: {offenders}. "
                             "Two implementations drift, and the drift is silent.")

    def test_the_helper_actually_reaches_both_roots(self):
        out = subprocess.run(["bash", str(DISCOVER)], cwd=str(REPO),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, f"helper failed: {out.stderr.strip()}")
        paths = out.stdout.split()
        self.assertTrue(any(p.startswith("tests/") for p in paths), "no tests/ files discovered")
        if (REPO / "skills").is_dir():
            self.assertTrue(any(p.startswith("skills/") for p in paths),
                            "skills/ exists but the helper reached none of it")

    def test_the_helper_is_order_and_comment_proof_by_construction(self):
        """The property the parser could never hold: there is nothing to parse."""
        body = DISCOVER.read_text()
        self.assertNotIn("grep", body.split("find")[0],
                         "the helper should RUN find, not derive roots from text")


if __name__ == "__main__":
    unittest.main(verbosity=2)
