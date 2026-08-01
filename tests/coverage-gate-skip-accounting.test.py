#!/usr/bin/env python3
"""A fully-skipped test file must not look identical to a passing one.

`scripts/coverage-gate.sh` runs every `tests/**/*.test.py` under coverage and
captures each file's output into `$output`, echoing it **only on failure**:

    if ! output=$(python3 -m coverage run --rcfile=.coveragerc "$f" 2>&1); then
        echo "✖ test failed under instrumentation: $f"

So a file whose every case called `skipTest()` produced byte-for-byte the same
visible result as one whose every case passed: nothing. A suite that quietly
stopped asserting — a missing toolchain, an absent optional dependency, a
host-specific guard that is false on the runner — was indistinguishable from a
green one, and nothing in the gate reported skip counts.

Measured before the change, on two synthetic files run through the same parse:

    all_skip.py   ran=2 skipped=2   old gate showed: (nothing)
    all_pass.py   ran=2 skipped=0   old gate showed: (nothing)

That is the two-opposite-inputs-identical-output shape. The information was
already there — unittest prints `Ran N tests` and `OK (skipped=M)` — the gate
was discarding it.

This is reporting, NOT a new failure mode: optional deps and host toolchains
are legitimately absent, so a fully-skipped file is surfaced loudly and the
gate still passes. The point is that it stops being invisible.

Run: python3 tests/coverage-gate-skip-accounting.test.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "coverage-gate.sh"
GATE_SRC = GATE.read_text()

#: The gate's own extraction, mirrored so drift in either shows up here.
RAN_RE = re.compile(r"^Ran (\d+) tests?", re.M)
SKIP_RE = re.compile(r"skipped=(\d+)")

ALL_SKIP = """import unittest
class T(unittest.TestCase):
    def test_a(self): self.skipTest("no toolchain")
    def test_b(self): self.skipTest("no toolchain")
if __name__ == "__main__": unittest.main()
"""
ALL_PASS = """import unittest
class T(unittest.TestCase):
    def test_a(self): self.assertTrue(True)
    def test_b(self): self.assertTrue(True)
if __name__ == "__main__": unittest.main()
"""
MIXED = """import unittest
class T(unittest.TestCase):
    def test_a(self): self.skipTest("optional")
    def test_b(self): self.assertTrue(True)
if __name__ == "__main__": unittest.main()
"""


def run_and_parse(source: str) -> "tuple[int, int]":
    """Run a synthetic test file and apply the gate's parse to its output."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "probe.py"
        p.write_text(source)
        r = subprocess.run([sys.executable, str(p)],
                           capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    m, s = RAN_RE.search(out), SKIP_RE.search(out)
    return (int(m.group(1)) if m else 0, int(s.group(1)) if s else 0)


def fully_skipped(ran: int, skipped: int) -> bool:
    """The gate's condition."""
    return ran > 0 and skipped == ran


class TestUnittestActuallyReportsThis(unittest.TestCase):
    """Guards the rest — if unittest's summary format changed, every
    assertion below would pass or fail for an unrelated reason."""

    def test_ran_count_is_parseable(self):
        ran, _ = run_and_parse(ALL_PASS)
        self.assertEqual(ran, 2, "could not parse 'Ran N tests'")

    def test_skip_count_is_parseable(self):
        _, sk = run_and_parse(ALL_SKIP)
        self.assertEqual(sk, 2, "could not parse 'skipped=M'")


class TestTheDistinction(unittest.TestCase):
    def test_all_skipped_is_detected(self):
        ran, sk = run_and_parse(ALL_SKIP)
        self.assertTrue(fully_skipped(ran, sk), (ran, sk))

    def test_all_passed_is_not_flagged(self):
        ran, sk = run_and_parse(ALL_PASS)
        self.assertFalse(fully_skipped(ran, sk), (ran, sk))

    def test_partial_skip_is_not_flagged(self):
        """A file with one optional case is normal, not a dead file."""
        ran, sk = run_and_parse(MIXED)
        self.assertEqual((ran, sk), (2, 1))
        self.assertFalse(fully_skipped(ran, sk))

    def test_the_two_are_distinguishable_at_all(self):
        """The regression itself: pre-change both were invisible."""
        self.assertNotEqual(run_and_parse(ALL_SKIP), run_and_parse(ALL_PASS))


class TestGateWiring(unittest.TestCase):
    def test_gate_collects_skip_counts(self):
        self.assertIn("skipped_total", GATE_SRC)
        self.assertIn("fully_skipped", GATE_SRC)

    def test_gate_reports_rather_than_fails(self):
        """Optional deps are legitimately absent — surfacing must not turn a
        green suite red."""
        self.assertIn("not a failure", GATE_SRC)
        block = GATE_SRC[GATE_SRC.index("fully_skipped=()"):]
        block = block[:block.index("diff-cover")] if "diff-cover" in block else block
        self.assertNotIn("failed=1", block.split("continue", 1)[-1],
                         "surfacing skips must not set the failure flag")

    def test_gate_attributes_partial_skips_not_just_the_total(self):
        """A bare total says something skips but not WHAT, so it cannot answer
        the question it provokes: does CI skip different things than a laptop?
        Naming the files makes a platform difference visible in the log."""
        self.assertIn("partly_skipped", GATE_SRC)
        # ...and the list is actually printed, not merely collected.
        tail = GATE_SRC[GATE_SRC.index("skipped_total\" -gt 0"):]
        self.assertIn('for entry in "${partly_skipped[@]}"', tail)

    def test_partial_and_full_skips_are_reported_separately(self):
        """A file skipping 1 of 30 is normal; one skipping 30 of 30 asserts
        nothing. Collapsing them would hide the second in the first."""
        self.assertIn("fully_skipped+=", GATE_SRC)
        self.assertIn("partly_skipped+=", GATE_SRC)
        self.assertIn("elif", GATE_SRC[GATE_SRC.index("fully_skipped+="):])

    def test_gate_still_fails_on_a_real_failure(self):
        """The pre-existing behaviour must survive the change."""
        self.assertIn('echo "✖ test failed under instrumentation: $f"', GATE_SRC)
        self.assertIn("failed=1", GATE_SRC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
