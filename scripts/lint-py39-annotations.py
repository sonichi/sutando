#!/usr/bin/env python3
"""
lint-py39-annotations.py — flag src/*.py files that use PEP-604 union syntax
(X | Y in type annotations) without `from __future__ import annotations`.

PEP-604 bare union syntax (e.g. `str | None`) requires Python 3.10+.
Adding `from __future__ import annotations` (PEP-563) makes it work on 3.9
because annotations become strings evaluated lazily.

Usage:
    python3 scripts/lint-py39-annotations.py [--fix] [files...]

    --fix   Add `from __future__ import annotations` to flagged files.
    files   Explicit list of .py files to check (default: src/*.py).

Exit codes:
    0  All files clean (or all fixed)
    1  One or more violations found (without --fix)
"""

import ast
import re
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DEFAULT_GLOB = "src/*.py"

# Regex: a bare `|` between two identifiers or bracketed types in annotation
# position. We use AST-level detection (more reliable than regex), but the
# regex is used as a fast pre-filter to skip files that can't possibly match.
_UNION_RE = re.compile(r"\w[\w\[\], ]*\s*\|\s*[\w\[\], ]*\w")


def has_future_annotations(source: str) -> bool:
    """Return True if the file already has `from __future__ import annotations`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__" and any(
                alias.name == "annotations" for alias in node.names
            ):
                return True
    return False


def uses_union_syntax(source: str) -> bool:
    """Return True if source uses `X | Y` type union syntax."""
    if not _UNION_RE.search(source):
        return False
    # AST walk: look for BinOp with BitOr operator in annotation contexts.
    # Python 3.10+ parses `X | Y` as ast.BinOp(op=ast.BitOr) in annotations;
    # on 3.9 this is only valid under `from __future__ import annotations`
    # (where annotations are strings, not evaluated). We detect via regex
    # on annotation source positions since 3.9's ast doesn't produce BinOp
    # for annotation unions.
    #
    # Strategy: use tokenize to find `|` tokens inside annotation positions.
    # Simpler approach: look for function/variable annotations with `|`.
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    lines = source.splitlines()

    def _annotation_contains_union(ann) -> bool:
        if ann is None:
            return False
        # On Python 3.10+, `X | Y` parses as BinOp(BitOr)
        if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
            return True
        # Recurse into subscripts, tuples, etc.
        for child in ast.walk(ann):
            if child is ann:
                continue
            if isinstance(child, ast.BinOp) and isinstance(child.op, ast.BitOr):
                return True
        # Fallback: check raw annotation line for `|` pattern
        # (handles cases where ast doesn't parse BinOp on older Pythons)
        start = getattr(ann, "lineno", None)
        end = getattr(ann, "end_lineno", None)
        if start and end:
            snippet = " ".join(lines[start - 1 : end])
            if _UNION_RE.search(snippet):
                return True
        return False

    for node in ast.walk(tree):
        # Function argument annotations and return annotation
        if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            if _annotation_contains_union(node.returns):
                return True
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                if _annotation_contains_union(arg.annotation):
                    return True
            if node.args.vararg and _annotation_contains_union(node.args.vararg.annotation):
                return True
            if node.args.kwarg and _annotation_contains_union(node.args.kwarg.annotation):
                return True
        # Variable annotations (x: int | None = ...)
        if isinstance(node, ast.AnnAssign):
            if _annotation_contains_union(node.annotation):
                return True
    return False


def _insert_future_annotations(source: str) -> str:
    """Insert `from __future__ import annotations` after any module docstring."""
    lines = source.splitlines(keepends=True)
    insert_at = 0
    # Skip shebang
    if lines and lines[0].startswith("#!"):
        insert_at = 1
    # Skip module docstring (string literal as first statement)
    try:
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                insert_at = node.end_lineno  # insert after docstring line
            break
    except SyntaxError:
        pass
    # Skip leading comment lines if no docstring consumed them
    while insert_at < len(lines) and lines[insert_at].startswith("#"):
        insert_at += 1
    future_line = "from __future__ import annotations\n"
    # Don't double-insert
    if future_line in lines:
        return source
    lines.insert(insert_at, future_line)
    return "".join(lines)


def lint_file(path: Path, fix: bool = False) -> bool:
    """
    Check a single file. Return True if clean (no violation or fixed),
    False if violation found (and not fixing).
    """
    source = path.read_text(encoding="utf-8")
    if has_future_annotations(source):
        return True
    if not uses_union_syntax(source):
        return True
    # Violation found
    if fix:
        new_source = _insert_future_annotations(source)
        path.write_text(new_source, encoding="utf-8")
        print(f"FIXED  {path}")
        return True
    print(f"FAIL   {path}  — uses X|Y union syntax but missing `from __future__ import annotations`")
    return False


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    fix = "--fix" in argv
    file_args = [a for a in argv if not a.startswith("--")]

    if file_args:
        paths = [Path(f) for f in file_args]
    else:
        paths = sorted((REPO_ROOT / "src").glob("*.py"))

    violations = 0
    for path in paths:
        if not path.exists():
            print(f"SKIP   {path}  (not found)", file=sys.stderr)
            continue
        ok = lint_file(path, fix=fix)
        if not ok:
            violations += 1

    if violations:
        print(f"\n{violations} file(s) need `from __future__ import annotations`")
        print("Re-run with --fix to add it automatically.")
        sys.exit(1)
    elif file_args or fix:
        print("All files clean.")


if __name__ == "__main__":
    main()
