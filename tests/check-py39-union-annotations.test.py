#!/usr/bin/env python3
"""
Structural tests for scripts/check-py39-union-annotations.py.

Verifies the script's logic by feeding synthetic AST fixtures and running
the full CLI against temp directories — no dependency on the live src/ tree.
"""

import ast
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-py39-union-annotations.py"

# ---------------------------------------------------------------------------
# Import the helpers directly (annotation_union_lines, check_file)
# ---------------------------------------------------------------------------
import importlib.util

spec = importlib.util.spec_from_file_location("lint", SCRIPT)
lint = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
spec.loader.exec_module(lint)  # type: ignore[union-attr]


def _parse(src: str) -> ast.Module:
    return ast.parse(textwrap.dedent(src))


class TestAnnotationUnionLines(unittest.TestCase):
    """annotation_union_lines() — pure AST detection."""

    def test_clean_file_no_hits(self) -> None:
        tree = _parse(
            """
            def foo(x: int, y: str) -> bool:
                z: int = 1
            """
        )
        self.assertEqual(lint.annotation_union_lines(tree), [])

    def test_variable_annotation_flagged(self) -> None:
        tree = _parse("x: int | None = 1\n")
        lines = lint.annotation_union_lines(tree)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0], 1)

    def test_return_type_flagged(self) -> None:
        tree = _parse(
            """
            def foo() -> int | None:
                return None
            """
        )
        lines = lint.annotation_union_lines(tree)
        self.assertIn(2, lines)

    def test_arg_annotation_flagged(self) -> None:
        tree = _parse(
            """
            def bar(x: str | int) -> None:
                pass
            """
        )
        lines = lint.annotation_union_lines(tree)
        self.assertTrue(len(lines) >= 1)

    def test_async_def_flagged(self) -> None:
        tree = _parse(
            """
            async def baz(x: str | None) -> int | None:
                pass
            """
        )
        lines = lint.annotation_union_lines(tree)
        self.assertGreaterEqual(len(lines), 2)

    def test_bit_or_in_expression_not_flagged(self) -> None:
        tree = _parse(
            """
            x = 1 | 2
            y = {'a': 1} | {'b': 2}
            """
        )
        self.assertEqual(lint.annotation_union_lines(tree), [])

    def test_kwonly_arg_annotation_flagged(self) -> None:
        tree = _parse(
            """
            def fn(*, key: str | int) -> None:
                pass
            """
        )
        lines = lint.annotation_union_lines(tree)
        self.assertTrue(len(lines) >= 1)


class TestCheckFile(unittest.TestCase):
    """check_file() — file-level guard and integration."""

    def _write(self, content: str, suffix: str = ".py") -> Path:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, delete=False, encoding="utf-8"
        )
        f.write(textwrap.dedent(content))
        f.flush()
        return Path(f.name)

    def test_future_annotations_suppresses_flag(self) -> None:
        p = self._write(
            """
            from __future__ import annotations
            def foo(x: str | None) -> int | None:
                pass
            """
        )
        self.assertEqual(lint.check_file(p), [])

    def test_missing_future_triggers_flag(self) -> None:
        p = self._write(
            """
            def foo(x: str | None) -> None:
                pass
            """
        )
        result = lint.check_file(p)
        self.assertTrue(len(result) >= 1)
        self.assertIn("PEP-604", result[0])

    def test_syntax_error_reported(self) -> None:
        p = self._write("def (broken:\n")
        result = lint.check_file(p)
        self.assertTrue(len(result) == 1)
        self.assertIn("SyntaxError", result[0])

    def test_clean_file_returns_empty(self) -> None:
        p = self._write(
            """
            def foo(x: int, y: str) -> bool:
                return True
            """
        )
        self.assertEqual(lint.check_file(p), [])


class TestCLI(unittest.TestCase):
    """End-to-end CLI exit codes."""

    def _run(self, scan_dirs: dict[str, str]) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as td:
            for rel, content in scan_dirs.items():
                p = Path(td) / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(textwrap.dedent(content), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT)],
                capture_output=True,
                text=True,
                cwd=td,
            )

    def test_exit_0_on_clean_codebase(self) -> None:
        result = self._run(
            {
                "src/clean.py": """\
                from __future__ import annotations
                def foo(x: str | None) -> int | None:
                    pass
                """,
            }
        )
        self.assertEqual(result.returncode, 0)

    def test_exit_1_on_violation(self) -> None:
        result = self._run(
            {
                "src/bad.py": """\
                def foo(x: str | None) -> None:
                    pass
                """,
            }
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("PEP-604", result.stdout)

    def test_exit_0_when_scan_dirs_absent(self) -> None:
        result = self._run({})
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
