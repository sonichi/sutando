#!/usr/bin/env python3
"""
CI gate: catch PEP-604 `X | Y` union syntax in annotation positions
without `from __future__ import annotations` (Python 3.9 incompatible).

Background (#961 / #960): The telegram-bridge outage on 2026-05-?? was caused
by a `str | None` annotation that crashed the bridge on import under Python 3.9
(stock macOS python3 = 3.9.x on Apple CLT). PEP-604 `X | Y` union syntax in
annotations requires Python 3.10+ OR `from __future__ import annotations` as
the first non-docstring import.

FLAGGED — a file in SCAN_DIRS that:
  1. uses `X | Y` syntax in an annotation position (variable, arg, return type), AND
  2. does NOT have `from __future__ import annotations`

NOT flagged:
  - Files with `from __future__ import annotations` present (runtime-safe on 3.9)
  - BitOr in non-annotation contexts (bit operations, dict unions {**a} | {**b}, etc.)
  - Files that don't parse cleanly (syntax errors reported separately)

Exit 1 on any hit, 0 otherwise. Run from the repo root.
"""

import ast
import sys
from pathlib import Path

SCAN_DIRS = ("src", "skills")
SCAN_GLOBS = ("*.py",)


def _has_bitor(node: ast.expr) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
            return True
    return False


def annotation_union_lines(tree: ast.Module) -> list[int]:
    """Return line numbers of PEP-604 union annotations in the tree."""
    hits: list[int] = []
    for node in ast.walk(tree):
        # Variable annotations: x: int | None = ...
        if isinstance(node, ast.AnnAssign) and node.annotation:
            if _has_bitor(node.annotation):
                hits.append(node.col_offset and node.lineno or node.lineno)
        # Function signatures
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Return type
            if node.returns and _has_bitor(node.returns):
                hits.append(node.lineno)
            # All argument annotations
            all_args = (
                node.args.args
                + node.args.posonlyargs
                + node.args.kwonlyargs
                + ([node.args.vararg] if node.args.vararg else [])
                + ([node.args.kwarg] if node.args.kwarg else [])
            )
            for arg in all_args:
                if arg.annotation and _has_bitor(arg.annotation):
                    hits.append(arg.lineno)
    return hits


def check_file(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8", errors="replace")
    if "from __future__ import annotations" in src:
        return []
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: SyntaxError — {exc.msg}"]
    lines = annotation_union_lines(tree)
    return [f"{path}:{ln}: PEP-604 X|Y annotation without __future__.annotations" for ln in lines]


def main() -> int:
    violations: list[str] = []
    for scan_dir in SCAN_DIRS:
        root = Path(scan_dir)
        if not root.exists():
            continue
        for glob in SCAN_GLOBS:
            for f in sorted(root.rglob(glob)):
                violations.extend(check_file(f))

    if violations:
        print(f"FAIL: {len(violations)} file(s) use PEP-604 X|Y union annotations")
        print("      without `from __future__ import annotations` (Python 3.9 incompatible).")
        print()
        for v in violations:
            print(f"  {v}")
        print()
        print("Fix: add `from __future__ import annotations` as the first non-docstring import,")
        print("     or upgrade the baseline to Python 3.10+.")
        return 1

    total = sum(
        len(list(Path(d).rglob(g)))
        for d in SCAN_DIRS
        for g in SCAN_GLOBS
        if Path(d).exists()
    )
    print(f"Results: {total} Python files checked, 0 violations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
