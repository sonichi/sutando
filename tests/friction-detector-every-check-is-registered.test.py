#!/usr/bin/env python3
"""The set of checks friction-detector DEFINES must equal the set it RUNS.

Two failures live in the gap between those sets, and this file exists because
both were real in `src/friction-detector.py` at the same time:

  * a check defined and never called — `check_stale_results()` carried the
    docstring "Find undelivered results (no corresponding task completion)"
    and a body of `return []`. It was never in `main()`. On 2026-08-04 it cost
    a real investigation: asking "does anything cover undelivered results?"
    finds that name, reads as coverage, and is dead.
  * a check dropped from `main()` — measured, not assumed: deleting
    `all_issues.extend(check_pending_questions())` broke **zero** of the two
    existing friction suites. A live check can be silently unregistered and
    the detector keeps reporting a clean run.

The second is why "the suites still pass" was not evidence that removing the
stub was safe. A negative from a set of tests never shown able to produce a
positive says nothing, so the equality below is asserted structurally rather
than trusted to the existing suites.

Deliberately an equality, not two one-way checks: either direction alone is
satisfiable by the other's bug.

Run: python3 tests/friction-detector-every-check-is-registered.test.py
"""
from __future__ import annotations

import ast
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src", "friction-detector.py")


def _module_tree() -> ast.Module:
    with open(_SRC) as fh:
        return ast.parse(fh.read())


def _defined_checks(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in tree.body                      # module level only
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("check_")
    }


def _checks_called_in_main(tree: ast.Module) -> set[str]:
    main = next(
        (n for n in tree.body
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "main"),
        None,
    )
    if main is None:
        raise AssertionError("friction-detector.py has no main() — the registration site moved")
    called: set[str] = set()
    for node in ast.walk(main):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id.startswith("check_"):
            called.add(node.func.id)
    return called


class TestEveryCheckIsRegistered(unittest.TestCase):
    def test_defined_and_run_sets_are_equal(self):
        tree = _module_tree()
        defined = _defined_checks(tree)
        called = _checks_called_in_main(tree)

        # Guard the instrument itself: an empty set on either side would make
        # the equality trivially true and this test permanently green.
        self.assertTrue(defined, "no check_* functions found — the parse is broken, not the file")
        self.assertTrue(called, "main() calls no check_* function — the parse is broken")

        dead = sorted(defined - called)
        self.assertFalse(
            dead,
            f"defined but never run by main(): {dead}. A check that is never called reads as "
            f"coverage to anyone grepping for its name. Register it or delete it.",
        )
        missing = sorted(called - defined)
        self.assertFalse(
            missing,
            f"called by main() but not defined at module level: {missing}",
        )

    def test_the_known_dead_stub_has_not_come_back(self):
        """Named explicitly: this one shipped, sat unused, and misled a search
        for 'undelivered results'. The equality above would catch it anyway;
        this asserts the specific regression by name so the reason survives."""
        self.assertNotIn("check_stale_results", _defined_checks(_module_tree()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
