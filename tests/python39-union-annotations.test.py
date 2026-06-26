#!/usr/bin/env python3
"""CI lint: flag PEP-604 union annotations without `from __future__ import annotations`.

## Why this test exists

Python 3.9 (EOL Oct 2025, still the stock macOS interpreter as of 2026) evaluates
annotations at *definition time*. A bare `str | None` in a function signature is a
`BinOp(BitOr)` expression that raises `TypeError: unsupported operand type(s) for |:
'type' and 'NoneType'` on import.

`from __future__ import annotations` (PEP 563) turns ALL annotations into lazy strings,
making `str | None` and `X | Y` safe on 3.9. Every `src/*.py` file that uses PEP-604
union syntax MUST include that import.

Incident: PR #960 — `telegram-bridge.py:313` used `task_id: str | None = None` without
the future import. The bridge crashed silently on every Mac running stock python3 (3.9).

This test is the CI gate that prevents the same class of regression.

Scan scope: `src/*.py` + `skills/**/*.py`. Tests and third-party vendored code
(__pycache__, node_modules) are excluded. Skills are included because cron-path
scripts in skills/ can also crash on Python 3.9 hosts — #1385 fixed two such
instances in quota-tracker and deal-finder; this broader scope prevents the
class from recurring (closes #1386).

Run: python3 tests/python39-union-annotations.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
SKILLS = REPO / "skills"

_EXCLUDE = ("/__pycache__/", "/node_modules/", "/personal-")


def _contains_bitor(node: ast.expr) -> bool:
    """True if `node` or any descendant is a BitOr BinOp (PEP-604 union)."""
    for child in ast.walk(node):
        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.BitOr):
            return True
    return False


def _file_uses_pep604_unions(tree: ast.Module) -> bool:
    """True if the AST contains PEP-604 union syntax in annotation context."""
    for node in ast.walk(tree):
        # Function / async-function parameter and return annotations.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_args = (
                node.args.args
                + node.args.posonlyargs
                + node.args.kwonlyargs
                + ([node.args.vararg] if node.args.vararg else [])
                + ([node.args.kwarg] if node.args.kwarg else [])
            )
            for arg in all_args:
                if arg.annotation and _contains_bitor(arg.annotation):
                    return True
            if node.returns and _contains_bitor(node.returns):
                return True
        # Module / class / function-body variable annotations.
        if isinstance(node, ast.AnnAssign):
            if _contains_bitor(node.annotation):
                return True
    return False


def _has_future_annotations(source: str) -> bool:
    return "from __future__ import annotations" in source


def check_file(path: Path) -> str | None:
    """Return a failure message for `path`, or None if the file is clean."""
    try:
        source = path.read_text(encoding="utf-8")
    except Exception:
        return None  # unreadable; let other tools handle it

    if _has_future_annotations(source):
        return None  # already guarded

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return None  # parse errors are a different gate

    if _file_uses_pep604_unions(tree):
        rel = path.relative_to(REPO)
        return (
            f"{rel}: uses PEP-604 `X | Y` union annotation without "
            f"`from __future__ import annotations` — crashes on Python 3.9 "
            f"(the stock macOS interpreter). Add `from __future__ import annotations` "
            f"at the top of the file (see issue #961 and the #960 incident)."
        )
    return None


def test_no_bare_union_annotations() -> None:
    failures = []
    paths = list(SRC.glob("*.py")) + list(SKILLS.rglob("*.py") if SKILLS.exists() else [])
    for path in sorted(paths):
        if any(ex in str(path) for ex in _EXCLUDE):
            continue
        msg = check_file(path)
        if msg:
            failures.append(msg)
    if failures:
        raise AssertionError(
            "PEP-604 union annotations without `from __future__ import annotations`:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )


def test_detection_self_check() -> None:
    """Self-test: the AST scan must match known PEP-604 union shapes."""
    cases = [
        "def f(x: str | None): pass",
        "def f(x: int | str | None): pass",
        "def f() -> str | None: pass",
        "x: str | None",
        "x: list[str] | None",
    ]
    for case in cases:
        tree = ast.parse(case)
        assert _file_uses_pep604_unions(tree), (
            f"detection missed PEP-604 union shape: {case!r}"
        )


def test_negative_cases() -> None:
    """Self-test: must NOT match non-annotation uses of `|`."""
    cases = [
        # Bitwise OR in expressions
        "x = a | b",
        "result = flags | 0x01",
        # Regex pattern strings
        r'RE = re.compile(r"foo|bar")',
        # String concatenation
        "'foo' or 'bar'",
        # Guarded file (has __future__)
        "from __future__ import annotations\ndef f(x: str | None): pass",
    ]
    negative_trees = [
        ast.parse("x = a | b"),
        ast.parse("result = flags | 0x01"),
        ast.parse(r're.compile(r"foo|bar")'),
    ]
    for tree in negative_trees:
        assert not _file_uses_pep604_unions(tree), (
            "false positive: matched non-annotation BitOr"
        )
    # Guarded file: has __future__, so check_file returns None
    src = "from __future__ import annotations\ndef f(x: str | None): pass"
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
        f.write(src)
        fname = f.name
    try:
        result = check_file(Path(fname))
        assert result is None, f"false positive on guarded file: {result}"
    finally:
        os.unlink(fname)


def main() -> int:
    failures = []
    for fn in (
        test_detection_self_check,
        test_negative_cases,
        test_no_bare_union_annotations,
    ):
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        return 1
    print("All python39-union-annotations tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
