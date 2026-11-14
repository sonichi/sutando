#!/usr/bin/env python3
"""A test that starts the task watcher or a task notifier in its own session must bind it.

`subprocess.Popen(..., start_new_session=True)` puts the fixture outside the
test's process group, so when the test is SIGTERMed or SIGKILLed before its
teardown runs, the fixture is re-parented to init and runs forever with the
repo as its cwd, where the process-hygiene reaper deliberately cannot reach it.
`tests/_helpers/process_group.py` ties such a group to the test (`popen_bound`,
or `bind_to_test` in the same function as the Popen).

This is a structural scan, not a behaviour test (REVIEW.md lesson 14's
exception): the defect is a call shape that only shows when a test is killed.
`tests/process-group-binding.test.py` pins the behaviour of the binding itself.

Run: python3 tests/watcher-fixtures-are-bound.test.py
"""
import ast
import textwrap
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
LONG_RUNNING = ("watch-tasks-stream.sh", "task-notifier.sh")


def _is_session_popen(call: ast.Call) -> bool:
    func = call.func
    named = getattr(func, "attr", None) == "Popen" or getattr(func, "id", None) == "Popen"
    return named and any(k.arg == "start_new_session" and getattr(k.value, "value", None) is True
                         for k in call.keywords)


def _binds(scope) -> bool:
    return any(isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "bind_to_test"
               for n in ast.walk(scope))


def unbound_popens(source: str, name: str = "<src>") -> list:
    """Lines of Popen(start_new_session=True) calls with no bind_to_test in their innermost function."""
    if not any(script in source for script in LONG_RUNNING):
        return []
    tree = ast.parse(source, name)
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_session_popen(node):
            scope = parent.get(node)
            while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = parent.get(scope)
            if not _binds(scope if scope is not None else tree):
                found.append(node.lineno)
    return sorted(found)


class EveryWatcherFixtureIsBound(unittest.TestCase):
    maxDiff = None

    def test_no_test_starts_a_watcher_in_its_own_session_unbound(self):
        files = [p for p in sorted(TESTS.rglob("*.py"))
                 if "_helpers" not in p.parts and "node_modules" not in p.parts and p != Path(__file__).resolve()]
        offenders = {}
        for path in files:
            lines = unbound_popens(path.read_text(errors="replace"), str(path))
            if lines:
                offenders[str(path.relative_to(TESTS.parent))] = lines
        self.assertEqual(offenders, {}, "start these through tests/_helpers/process_group.py popen_bound, "
                                        "or call bind_to_test in the same function")
        self.assertGreater(sum("popen_bound" in p.read_text(errors="replace") for p in files), 10,
                           "the scan saw no converted callers; it is not reading the test tree")


class TheScanFires(unittest.TestCase):
    """Positive controls: an instrument that cannot fire proves nothing."""

    def test_an_unbound_watcher_popen_is_flagged(self):
        src = textwrap.dedent('''
            import subprocess
            def start(inbox):
                return subprocess.Popen(["bash", "src/watch-tasks-stream.sh", inbox],
                                        start_new_session=True)
            p = subprocess.Popen(["bash", "src/claude-task-notifier.sh"], start_new_session=True)
        ''')
        self.assertEqual(unbound_popens(src), [4, 6])

    def test_bound_and_unrelated_shapes_pass(self):
        src = textwrap.dedent('''
            import subprocess
            from _helpers.process_group import bind_to_test, popen_bound
            WATCHER = "src/watch-tasks-stream.sh"
            def a(inbox):
                return popen_bound(["bash", WATCHER, inbox])
            def b(inbox):
                p = subprocess.Popen(["bash", WATCHER, inbox], start_new_session=True)
                bind_to_test(p.pid)
                return p
            def c():
                return subprocess.Popen(["sleep", "1"])
        ''')
        self.assertEqual(unbound_popens(src), [])

    def test_a_file_that_never_names_a_long_running_script_is_out_of_scope(self):
        src = "import subprocess\nsubprocess.Popen(['sleep', '9'], start_new_session=True)\n"
        self.assertEqual(unbound_popens(src), [])


if __name__ == "__main__":
    unittest.main()
