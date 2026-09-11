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
        # Positive assertion keys on test:ts — the script CI invokes. Joining every
        # test* script would let an unrelated script satisfy it while CI skips tests.
        scripts = self._scripts()
        joined = " ".join(v for k, v in scripts.items() if k.startswith("test"))
        self.assertIn(
            "tests/**/*.test.ts",
            scripts.get("test:ts", ""),
            "test:ts glob must be recursive (tests/**/*.test.ts) so nested tests run",
        )
        self.assertNotIn(
            "tests/*.test.ts",
            joined,
            "non-recursive tests/*.test.ts would skip every relocated test",
        )

    def test_py_find_is_recursive(self) -> None:
        """`find` over a ROOT, never a flat glob. Asserted on the behaviour, not
        the exact root list, which grew a second (optional) root for skills/."""
        py_script = self._scripts().get("test:py", "")
        self.assertRegex(
            py_script, r"find [^|;]*-name '\*\.test\.py'",
            "Python runner must use a recursive `find`, not a flat glob")
        self.assertNotIn(
            "tests/*.test.py", py_script,
            "a flat glob would skip every relocated test")

    def test_py_discovery_includes_the_skills_root(self) -> None:
        """A skill owns its own tests/ dir, so discovery must reach skills/ —
        otherwise a suite that moves into a skill silently stops running."""
        py_script = self._scripts().get("test:py", "")
        self.assertIn(
            "skills", py_script,
            "test:py must discover skills/**/*.test.py, not just tests/")

    def test_recursive_find_discovers_a_skill_owned_test(self) -> None:
        out = subprocess.run(
            ["find", "skills", "-name", "*.test.py"],
            cwd=REPO, capture_output=True, text=True, check=True).stdout
        self.assertTrue(
            [p for p in out.splitlines() if p.strip()],
            "expected at least one *.test.py under skills/ (e.g. "
            "skills/worker-pool/tests/); the skills root would be untested otherwise")

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
