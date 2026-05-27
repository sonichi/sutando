#!/usr/bin/env python3
"""Lint: flag src/*.py files that use PEP-604 union syntax (X | Y) in type
annotations without `from __future__ import annotations`.

Python 3.9 (macOS stock interpreter via Command Line Tools) predates PEP-604
runtime support. Without the __future__ guard, `str | None` in a function
signature or variable annotation crashes the file on import.

This is Option A of issue #961 — a lint that prevents the #960-class bug
(telegram-bridge outage caused by a missing __future__ import) without
requiring a baseline bump in startup.sh.

Run: python3 tests/py39-compat.test.py
Exit: 0 on pass, 1 on fail.
"""

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

# Regex that matches PEP-604 X|Y patterns that only work at runtime under 3.10+
# Captures things like: str | None, int | float, Path | None, list[str] | None
# We look for these in annotation positions (not inside strings/comments).
# The approach: parse the AST and inspect annotation nodes for BinOp(op=BitOr).
_FUTURE_ANNOTATIONS = "from __future__ import annotations"


def has_future_annotations(source: str) -> bool:
    return _FUTURE_ANNOTATIONS in source


def _binop_bitor_in_annotation(node: ast.AST) -> bool:
    """True if 'node' or any descendant is a BinOp with BitOr operator."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return True
    for child in ast.iter_child_nodes(node):
        if _binop_bitor_in_annotation(child):
            return True
    return False


def find_pep604_annotations(source: str, filepath: str) -> list[int]:
    """Return list of line numbers where PEP-604 union syntax appears in
    annotation positions (function args, returns, variable annotations)."""
    try:
        tree = ast.parse(source, filename=filepath)
    except SyntaxError:
        return []

    lines = []

    for node in ast.walk(tree):
        # Function argument annotations
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                if arg.annotation and _binop_bitor_in_annotation(arg.annotation):
                    lines.append(arg.annotation.lineno)
            if node.args.vararg and node.args.vararg.annotation:
                if _binop_bitor_in_annotation(node.args.vararg.annotation):
                    lines.append(node.args.vararg.annotation.lineno)
            if node.args.kwarg and node.args.kwarg.annotation:
                if _binop_bitor_in_annotation(node.args.kwarg.annotation):
                    lines.append(node.args.kwarg.annotation.lineno)
            # Return annotation
            if node.returns and _binop_bitor_in_annotation(node.returns):
                lines.append(node.returns.lineno)

        # Variable annotations (AnnAssign)
        elif isinstance(node, ast.AnnAssign):
            if _binop_bitor_in_annotation(node.annotation):
                lines.append(node.annotation.lineno)

    return sorted(set(lines))


def main() -> int:
    fails: list[str] = []
    checked = 0

    for pyfile in sorted(SRC.glob("*.py")):
        source = pyfile.read_text(encoding="utf-8", errors="replace")
        checked += 1

        if has_future_annotations(source):
            # File is guarded — no check needed.
            continue

        offending = find_pep604_annotations(source, str(pyfile))
        if offending:
            fails.append(
                f"{pyfile.name}: PEP-604 union syntax at line(s) "
                f"{offending[:5]} — add `from __future__ import annotations` "
                f"(Python 3.9 compatibility, issue #961 Option A)"
            )

    if fails:
        print(f"FAIL: {len(fails)} file(s) use PEP-604 unions without __future__ guard:")
        for f in fails:
            print(f"  - {f}")
        print()
        print("Fix: add `from __future__ import annotations` near the top of each file.")
        return 1

    print(f"PASS: {checked} src/*.py files checked — all PEP-604 unions have __future__ guard.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
