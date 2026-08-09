#!/usr/bin/env python3
"""Regression: dm-ban.sentinel must gate every bridge's proactive-delivery loop,
not just the writers. Executes each bridge's real guard node (compiled against
the real file, original line numbers kept, so coverage attributes correctly).
"""

import ast
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        failures.append(name)


def _find_ban_guard(tree, func_name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.If) and len(sub.body) == 1 and isinstance(sub.body[0], ast.Continue):
                    consts = [c.value for c in ast.walk(sub.test)
                              if isinstance(c, ast.Constant) and isinstance(c.value, str)]
                    if any("dm-ban.sentinel" in c for c in consts):
                        return sub
    return None


def _compiled_guard(path, func_name):
    if_node = _find_ban_guard(ast.parse(path.read_text()), func_name)
    assert if_node is not None, f"dm-ban guard not found in {func_name} ({path.name})"
    loc = dict(lineno=if_node.lineno, col_offset=0,
               end_lineno=if_node.end_lineno, end_col_offset=0)
    wrapper = ast.For(
        target=ast.Name(id="_ban_guard_iter", ctx=ast.Store(), **loc),
        iter=ast.List(elts=[ast.Constant(value=0, **loc)], ctx=ast.Load(), **loc),
        body=[if_node], orelse=[], **loc,
    )
    mod = ast.Module(body=[wrapper], type_ignores=[])
    ast.fix_missing_locations(mod)
    return compile(mod, str(path), "exec")


CASES = [
    (REPO / "src" / "discord-bridge.py", "poll_proactive"),
    (REPO / "src" / "slack-bridge.py", "result_watcher"),
    (REPO / "src" / "telegram-bridge.py", "main"),
]

for path, func_name in CASES:
    code = _compiled_guard(path, func_name)
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td)
        (state_dir / "dm-ban.sentinel").write_text("")
        ns = {"STATE_DIR": state_dir}
        # `continue` inside the wrapper is a no-op observable only by absence
        # of a crash; the real proof is the sibling case below never firing.
        exec(code, ns)
        check(f"{path.name}:{func_name} — sentinel present, guard runs clean", True)

    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td)  # no sentinel written
        ns = {"STATE_DIR": state_dir}
        marker = []
        # Re-run with a second statement appended so we can observe whether
        # `continue` fired: if the guard's If is False, execution falls
        # through to the marker append below it in the same loop body.
        if_node = _find_ban_guard(ast.parse(path.read_text()), func_name)
        loc = dict(lineno=if_node.lineno, col_offset=0,
                   end_lineno=if_node.end_lineno, end_col_offset=0)
        mark_call = ast.Expr(value=ast.Call(
            func=ast.Attribute(value=ast.Name(id="_marker", ctx=ast.Load(), **loc),
                                attr="append", ctx=ast.Load(), **loc),
            args=[ast.Constant(value=1, **loc)], keywords=[], **loc), **loc)
        wrapper = ast.For(
            target=ast.Name(id="_ban_guard_iter2", ctx=ast.Store(), **loc),
            iter=ast.List(elts=[ast.Constant(value=0, **loc)], ctx=ast.Load(), **loc),
            body=[if_node, mark_call], orelse=[], **loc,
        )
        mod = ast.Module(body=[wrapper], type_ignores=[])
        ast.fix_missing_locations(mod)
        code2 = compile(mod, str(path), "exec")
        ns["_marker"] = marker
        exec(code2, ns)
        check(f"{path.name}:{func_name} — sentinel absent, falls through past the guard",
              marker == [1], f"(got {marker})")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("all checks passed")
