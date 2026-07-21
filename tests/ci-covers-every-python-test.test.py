#!/usr/bin/env python3
"""Every test-looking Python file must actually be executed by CI.

CI discovers Python tests with `find tests -name '*.test.py'`. Anything outside
that root or suffix runs only if ci.yml names it explicitly. A fixed list is
maintenance the next author will not know they owe: a test added under
packages/*/tests/ is silently never run, and reads as coverage anyway.

This guard makes that gap self-detecting instead of silent. It failed to exist
when five suites -- including the transport gate and the src/-vs-package drift
guard -- had never run in CI.
"""
import glob
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CI = REPO / ".github" / "workflows" / "ci.yml"


def discovered_by_find():
    """What `find tests -name '*.test.py'` reaches."""
    return {str(Path(p)) for p in glob.glob("tests/**/*.test.py", recursive=True)}


def named_in_workflows():
    """Files any workflow invokes explicitly, e.g. `python3 path/to/x.py`."""
    named = set()
    for wf in (REPO / ".github" / "workflows").glob("*.yml"):
        for m in re.finditer(r"python3?\s+(\S+\.py)", wf.read_text()):
            named.add(m.group(1))
    return named


def test_looking_files():
    out = set()
    for pat in ("**/*.test.py", "**/test_*.py", "**/*_test.py"):
        for p in glob.glob(pat, recursive=True):
            if "node_modules" in p or p.startswith(".git/"):
                continue
            out.add(str(Path(p)))
    return out


class TestCICoversEveryPythonTest(unittest.TestCase):
    def test_no_python_test_is_invisible_to_ci(self):
        import os
        os.chdir(REPO)
        orphans = sorted(test_looking_files() - discovered_by_find() - named_in_workflows())
        self.assertEqual(
            orphans, [],
            "these test files are never executed by CI — either move them to "
            "tests/<name>.test.py (auto-discovered) or name them explicitly in "
            f"a workflow:\n  " + "\n  ".join(orphans),
        )

    def test_the_guard_can_actually_fail(self):
        """A guard that cannot fire is the bug it exists to catch."""
        fake = {"packages/somewhere/tests/test_invented.py"}
        self.assertTrue(fake - discovered_by_find() - named_in_workflows(),
                        "an out-of-tree file must register as an orphan")


if __name__ == "__main__":
    unittest.main(verbosity=2)
