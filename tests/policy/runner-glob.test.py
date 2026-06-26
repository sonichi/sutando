"""Guard: the test runner globs must be RECURSIVE so relocated tests run.

The migration moves tests out of the flat `tests/` dir into a tree that mirrors
`src/` (`tests/kernel/...`, `tests/adapters/...`). A non-recursive glob
(`tests/*.test.ts`) would silently skip every relocated test and still report
green — the "green-but-blind" failure mode. This test fails loudly if the
package.json scripts regress to a non-recursive pattern, and proves the
recursive Python `find` actually discovers a nested test.

POLICY test (test-inventory.md §5, Phase 0/4).
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class RunnerGlobTest(unittest.TestCase):
    def _scripts(self) -> dict[str, str]:
        pkg = json.loads((REPO / "package.json").read_text())
        return pkg.get("scripts", {})

    def test_ts_glob_is_recursive(self) -> None:
        test_script = self._scripts().get("test", "")
        self.assertIn(
            "tests/**/*.test.ts",
            test_script,
            "TS test glob must be recursive (tests/**/*.test.ts) so nested tests run",
        )
        self.assertNotIn(
            "tests/*.test.ts",
            test_script,
            "non-recursive tests/*.test.ts would skip every relocated test",
        )

    def test_py_find_is_recursive(self) -> None:
        py_script = self._scripts().get("test:py", "")
        self.assertIn(
            "find tests -name '*.test.py'",
            py_script,
            "Python runner must use a recursive `find`, not a flat glob",
        )

    def test_recursive_find_discovers_nested_tests(self) -> None:
        """The recursive find must return at least one test under a SUBDIRECTORY
        of tests/ (depth >= 2) — i.e. exactly what a flat glob would miss."""
        out = subprocess.run(
            ["find", "tests", "-name", "*.test.py"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        paths = [p for p in out.splitlines() if p.strip()]
        nested = [p for p in paths if Path(p).parent != Path("tests")]
        self.assertTrue(
            nested,
            "expected at least one *.test.py under a tests/ subdirectory "
            "(e.g. tests/kernel/...); recursion would be untested otherwise",
        )


if __name__ == "__main__":
    unittest.main()
