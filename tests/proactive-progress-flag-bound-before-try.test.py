#!/usr/bin/env python3
"""The zero-progress flag must be bound BEFORE the try whose handler reads it.

A failure in `fetch_user`/`create_dm` reached the except clause with the flag
unbound, so the handler itself raised UnboundLocalError: the claim stayed
`.sending`, was never released or parked, and only orphan recovery on restart
found it.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"
FLAG = "_sent_any"


def _tree():
    return ast.parse(BRIDGE.read_text())


def _calls(node) -> set:
    """Attribute names of every call under `node` (e.g. fetch_user, create_dm)."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            out.add(n.func.attr)
    return out


def _reads_flag(node) -> bool:
    return any(isinstance(n, ast.Name) and n.id == FLAG and isinstance(n.ctx, ast.Load)
               for n in ast.walk(node))


def _binds_flag(node) -> bool:
    return any(isinstance(n, ast.Name) and n.id == FLAG and isinstance(n.ctx, ast.Store)
               for n in ast.walk(node))


def _target_trys():
    """Every Try whose handlers read the flag AND whose body resolves the recipient."""
    found = []
    for n in ast.walk(_tree()):
        if not isinstance(n, ast.Try):
            continue
        if not any(_reads_flag(h) for h in n.handlers):
            continue
        if not ({"fetch_user", "create_dm"} & _calls(ast.Module(body=n.body, type_ignores=[]))):
            continue
        found.append(n)
    return found


class TestProgressFlagBinding(unittest.TestCase):
    def test_the_control_finds_the_try_under_test(self):
        # Without this the assertions below pass vacuously on zero matches.
        self.assertEqual(len(_target_trys()), 1,
                         "expected exactly one recipient-resolving try whose handler "
                         "reads the flag")

    def test_the_flag_is_bound_before_that_try_opens(self):
        try_node = _target_trys()[0]
        # Any binding at or after the try's own line is inside it, so a failure in
        # fetch_user/create_dm would reach the handler with the name unbound.
        binds = [n.lineno for n in ast.walk(_tree())
                 if isinstance(n, ast.Name) and n.id == FLAG
                 and isinstance(n.ctx, ast.Store)]
        self.assertTrue(binds, f"{FLAG} is never assigned")
        self.assertTrue(min(binds) < try_node.lineno,
                        f"{FLAG} is first bound at line {min(binds)}, at or inside the "
                        f"try that opens at {try_node.lineno}; the handler reads it")

    def test_the_recipient_lookup_is_inside_the_guarded_try(self):
        # The point of the fix is that the lookup is covered, not moved out of it.
        body = ast.Module(body=_target_trys()[0].body, type_ignores=[])
        self.assertIn("fetch_user", _calls(body))

    def test_the_handler_still_passes_progress_to_the_policy(self):
        handlers = _target_trys()[0].handlers
        self.assertTrue(any(_reads_flag(h) for h in handlers))
        # Post-5b the handler delegates through the fence's fail(), whose own
        # consult of decide_failed_send is pinned in send-failure-delegation.
        self.assertTrue(any("fail" in _calls(h) for h in handlers),
                        "the handler no longer delegates to the policy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
