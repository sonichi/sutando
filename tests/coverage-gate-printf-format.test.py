#!/usr/bin/env python3
"""A `printf` format that starts with `-` is parsed as an option, not a format.

`scripts/coverage-gate.sh` renders the NOT-MEASURED list as a markdown bullet:

    printf '-   `%s`\\n' $unmeasured

bash's builtin printf reads the leading `-` as an option flag, fails with
`printf: - : invalid option`, and under the script's `set -euo pipefail` that
exits 2 — before `publish_summary` runs, so no `coverage-summary.md` is even
produced. The gate reports FAILURE on a PR whose diff coverage was 100%.

Observed on sonichi/sutando#2967 (diff coverage 100%, 6 lines, 0 missing):

    coverage-gate: NOT MEASURED — changed, but absent from coverage.xml:
      src/runtime-cli/sutando-runtime.py
    scripts/coverage-gate.sh: line 182: printf: - : invalid option
    ##[error]Process completed with exit code 2.

The branch is reached by any PR that changes a Python file outside
`[run] source` in `.coveragerc`, so the crash is a property of the gate, not
of the PR under test. Line 172 in the same block survives only because its
format happens to start with spaces.

The fix is `printf --`, which ends option parsing.

This test executes the format literals taken FROM the script rather than
copies of them, so a future format that leads with `-` fails here too.

Run: python3 tests/coverage-gate-printf-format.test.py
"""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "coverage-gate.sh"

#: Every `printf` invocation in the gate, captured as (leading `--`?, format).
PRINTF_RE = re.compile(r"printf\s+(--\s+)?'((?:[^'\\]|\\.)*)'")


def _run(snippet: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)


class PrintfFormatsAreSafe(unittest.TestCase):
    def setUp(self):
        self.src = GATE.read_text()
        self.printfs = PRINTF_RE.findall(self.src)
        self.assertTrue(self.printfs, "no printf calls parsed out of the gate")

    def test_every_printf_in_the_gate_executes(self):
        """Run each real format through bash under the gate's own shell flags."""
        for dashdash, fmt in self.printfs:
            with self.subTest(fmt=fmt):
                dd = "-- " if dashdash else ""
                snippet = (
                    "set -euo pipefail\n"
                    "u='a.py b.py'\n"
                    f"printf {dd}'{fmt}' $u\n"
                )
                r = _run(snippet)
                self.assertEqual(
                    r.returncode, 0,
                    f"printf {dd}'{fmt}' exited {r.returncode}: {r.stderr.strip()}")
                self.assertNotIn("invalid option", r.stderr)

    def test_a_leading_dash_format_without_dashdash_is_the_bug(self):
        """Control: the pre-fix form must still fail, or this test proves nothing."""
        r = _run("set -euo pipefail\nu='a.py b.py'\nprintf '-   `%s`\\n' $u\n")
        self.assertNotEqual(r.returncode, 0, "expected the unguarded form to fail")
        self.assertIn("invalid option", r.stderr)

    def test_the_not_measured_bullet_is_guarded(self):
        """The specific line that crashed CI carries `--`."""
        bullet = [(d, f) for d, f in self.printfs if f.startswith("-")]
        self.assertTrue(bullet, "the NOT-MEASURED bullet format is gone; update this test")
        for dashdash, fmt in bullet:
            self.assertTrue(dashdash, f"printf '{fmt}' needs `--` before the format")

    def test_the_bullet_still_renders_markdown(self):
        """`--` must not leak into the output the PR comment shows."""
        r = _run("set -euo pipefail\nu='a.py b.py'\nprintf -- '-   `%s`\\n' $u\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "-   `a.py`\n-   `b.py`\n")


if __name__ == "__main__":
    unittest.main()
