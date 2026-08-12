#!/usr/bin/env python3
"""A probe that emits nothing when it cannot run is indistinguishable from a pass.

`run_all_checks()` guards the sutando-app probe on `dev_bin.exists() or
app_bin.exists()`. When neither exists the block was skipped with no `else`, so
no `sutando-app` row reached the report — and the summary line reads
"No failures", which is what a healthy probe also produces.

Structural rather than behavioural on purpose: exercising the branch means
calling `run_all_checks()`, which runs ~100 probes with subprocesses and
network. So this parses the guard's `else` out of the source and asserts what it
emits. Validated against the parent — see the negative-control note in the PR.

Run: python3 tests/health-check-sutando-app-absent.test.py
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "health-check.py"


def _guard_node() -> ast.If:
    """The `if dev_bin.exists() or app_bin.exists():` statement."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if {"dev_bin", "app_bin"} <= names:
            return node
    raise AssertionError("guard `dev_bin.exists() or app_bin.exists()` not found")


def _appended_check_names(body: list[ast.stmt]) -> list[str]:
    """Literal `name` values of every `checks.append({...})` dict in `body`."""
    out: list[str] = []
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "append":
            continue
        for arg in node.args:
            if not isinstance(arg, ast.Dict):
                continue
            for k, v in zip(arg.keys, arg.values):
                if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant):
                    out.append(v.value)
    return out


def _statuses(body: list[ast.stmt], for_name: str) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if not isinstance(node, ast.Dict):
            continue
        pairs = {k.value: v for k, v in zip(node.keys, node.values)
                 if isinstance(k, ast.Constant)}
        nm, st = pairs.get("name"), pairs.get("status")
        if isinstance(nm, ast.Constant) and nm.value == for_name and isinstance(st, ast.Constant):
            out.append(st.value)
    return out


class SutandoAppAbsentTest(unittest.TestCase):
    def test_the_guard_has_an_else_branch(self):
        # Without it the probe is simply absent from the report, and absence
        # is the one outcome a "No failures" summary cannot distinguish.
        self.assertTrue(_guard_node().orelse,
                        "no else on the sutando-app guard: when neither binary "
                        "exists the report omits the row entirely")

    def test_the_else_emits_a_sutando_app_row(self):
        names = _appended_check_names(_guard_node().orelse)
        self.assertIn("sutando-app", names,
                      f"else branch appends {names or 'nothing'} — the row must exist "
                      "so 'not checked' is visible")

    def test_that_row_is_NOT_ok(self):
        # An 'ok' here would be worse than silence: it asserts health for a
        # thing that was never looked at.
        sts = _statuses(_guard_node().orelse, "sutando-app")
        self.assertTrue(sts, "no status literal for the sutando-app row")
        for s in sts:
            self.assertNotEqual(s, "ok", "unchecked state must not report ok")

    def test_the_detail_names_both_paths_it_looked_for(self):
        # A warn that doesn't say where it looked sends the reader to grep.
        body = ast.get_source_segment(SRC.read_text(encoding="utf-8"),
                                      _guard_node()) or ""
        _, _, tail = body.partition("\n    else:")
        self.assertIn("dev_bin", tail, "detail should name the dev-build path")
        self.assertIn("app_bin", tail, "detail should name the installed-bundle path")


if __name__ == "__main__":
    unittest.main(verbosity=2)
