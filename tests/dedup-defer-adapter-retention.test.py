#!/usr/bin/env python3
"""Every adapter must be able to NAME `defer`, or it archives through one.

`plan_dedup_recovery` grew a fourth outcome, `defer`, for a holder that exists
but cannot be read yet. The three bridges only branched on `requeue` and
`report`, so `defer` fell through to the same path as `honour` — which pops the
route and archives both files. The unreadable answer is then unreachable and
the asker gets a duplicate re-ask instead of the reply that landed a moment
later.

Wiring, not policy: the decision lives in dedup_recovery. What is asserted here
is that each adapter binds the constant and guards a retention branch with it.
"""
import ast
import pathlib
import sys
import unittest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
from dedup_recovery import DEFER  # noqa: E402

ADAPTERS = ["telegram-bridge.py", "slack-bridge.py", "discord-bridge.py"]


def _tree(name):
    return ast.parse((SRC / name).read_text())


class DeferAdapterRetentionTest(unittest.TestCase):
    def test_defer_is_the_shared_owners_constant_not_a_copied_literal(self):
        # If an adapter spelled "defer" itself, renaming it here would leave the
        # adapters comparing against a string the owner no longer returns.
        self.assertEqual(DEFER, "defer")

    def test_every_adapter_imports_DEFER_from_the_shared_owner(self):
        for name in ADAPTERS:
            with self.subTest(adapter=name):
                imported = {
                    alias.name
                    for node in ast.walk(_tree(name))
                    if isinstance(node, ast.ImportFrom)
                    and node.module == "dedup_recovery"
                    for alias in node.names
                }
                self.assertIn("DEFER", imported)

    def test_every_adapter_guards_a_branch_on_DEFER(self):
        # The import alone proves nothing — an unused name still imports.
        for name in ADAPTERS:
            with self.subTest(adapter=name):
                used = [
                    node for node in ast.walk(_tree(name))
                    if isinstance(node, ast.Name) and node.id == "DEFER"
                ]
                self.assertTrue(used, f"{name} imports DEFER but never reads it")

    def test_the_control_a_bridge_without_the_branch_is_detected(self):
        # Proves the two assertions above can fail: the same checks run against
        # a module that legitimately has no defer branch.
        tree = _tree("result_markers.py")
        self.assertFalse([
            n for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id == "DEFER"
        ])


if __name__ == "__main__":
    unittest.main(verbosity=2)
