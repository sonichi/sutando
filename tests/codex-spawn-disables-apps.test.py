#!/usr/bin/env python3
"""Every argv that SPAWNS codex carries --disable=apps.

The audit this guards was done by grepping prompt strings -- text telling an
agent what to run -- and missed the one real `create_subprocess_exec` argv,
which is the only site where the flag actually constrains a process.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = [ROOT / "src", ROOT / "scripts", ROOT / "packages"]


def _argv_lists(tree):
    """List literals whose first two string elements are 'codex', 'exec'."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        head = [e.value for e in node.elts[:2]
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if head[:2] == ["codex", "exec"]:
            yield node


def _flags(node):
    return {e.value for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)}


def _offenders():
    bad = []
    for root in SCAN:
        if not root.exists():
            continue
        for f in root.rglob("*.py"):
            try:
                tree = ast.parse(f.read_text(encoding="utf8", errors="replace"))
            except SyntaxError:
                continue
            for node in _argv_lists(tree):
                if "--disable=apps" not in _flags(node):
                    bad.append(f"{f.relative_to(ROOT)}:{node.lineno}")
    return bad


class CodexSpawnsDisableApps(unittest.TestCase):
    def test_no_codex_argv_omits_the_flag(self):
        bad = _offenders()
        self.assertEqual(bad, [], "codex argv without --disable=apps: " + ", ".join(bad))

    def test_the_scan_finds_at_least_one_argv(self):
        """A scan that matches nothing would pass while every site regressed."""
        found = 0
        for root in SCAN:
            if not root.exists():
                continue
            for f in root.rglob("*.py"):
                try:
                    tree = ast.parse(f.read_text(encoding="utf8", errors="replace"))
                except SyntaxError:
                    continue
                found += sum(1 for _ in _argv_lists(tree))
        self.assertGreater(found, 0, "the argv matcher found nothing to check")


if __name__ == "__main__":
    unittest.main(verbosity=2)
