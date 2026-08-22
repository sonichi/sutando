#!/usr/bin/env python3
"""Convergence gate ① (issue #3279): one production entry point per outbound
delivery direction.

AST-level (not substring — see #3278 for why substring anchors lie): every
call site of the outbound drains is enumerated from the parsed module, so a
bypass added anywhere — main loop, a helper, a new thread — fails this suite
by name rather than shipping silently. Absence fails loudly as its own
contract, never as a misleading downstream assertion.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py"

failures: list[str] = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


tree = ast.parse(MODULE.read_text(), filename=str(MODULE))

# Map every call of the drain functions to its enclosing top-level def.
DRAINS = ("_post_ready_results", "_post_proactive")
call_sites: dict[str, list[tuple[str, int]]] = {d: [] for d in DRAINS}


class Walker(ast.NodeVisitor):
    def __init__(self):
        self.scope: list[str] = []

    def visit_FunctionDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        f = node.func
        name = f.id if isinstance(f, ast.Name) else (
            f.attr if isinstance(f, ast.Attribute) else None)
        if name in call_sites:
            call_sites[name].append(
                (self.scope[-1] if self.scope else "<module>", node.lineno))
        self.generic_visit(node)


Walker().visit(tree)

defined = {n.name for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
for d in DRAINS:
    check(d in defined, f"{d} is still defined in the bridge module")

for d in DRAINS:
    sites = call_sites[d]
    check(len(sites) == 1,
          f"{d} has exactly ONE call site (found {len(sites)}: {sites})")
    if sites:
        caller, line = sites[0]
        check(caller == "_outbound_worker",
              f"{d}'s only caller is _outbound_worker (found {caller}:{line})")

# The worker itself must be started from main() and nowhere else — the
# lifecycle has one owner, so a second scheduler cannot appear silently.
starter_sites = []


class StartWalker(Walker):
    def visit_Call(self, node):
        f = node.func
        name = f.id if isinstance(f, ast.Name) else (
            f.attr if isinstance(f, ast.Attribute) else None)
        if name == "_start_outbound_worker":
            starter_sites.append(
                (self.scope[-1] if self.scope else "<module>", node.lineno))
        self.generic_visit(node)


StartWalker().visit(tree)
check("_start_outbound_worker" in defined,
      "_start_outbound_worker is still defined in the bridge module")
check(len(starter_sites) == 1 and starter_sites[0][0] == "main",
      f"_start_outbound_worker called exactly once, from main() "
      f"(found {starter_sites})")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
