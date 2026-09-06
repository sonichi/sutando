#!/usr/bin/env python3
"""poll_dm_fallback must archive (never DM) question-/insight- digests past
the grace window; notify_voice must supersede older undelivered digests."""

import ast
import re
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE_PATH = REPO / "src" / "discord-bridge.py"
CPQ_PATH = REPO / "src" / "check-pending-questions.py"
BRIDGE_SRC = BRIDGE_PATH.read_text()
CPQ_SRC = CPQ_PATH.read_text()

failures = []


def _compile_function(path: Path, src: str, func_name: str):
    """Compile against the real file path with original line numbers kept,
    so coverage.py attributes the run to the file, not a detached string."""
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            mod = ast.Module(body=[node], type_ignores=[])
            return compile(mod, str(path), "exec")
    raise AssertionError(f"{func_name} not found in {path}")


def _find_digest_if(tree):
    """Matched structurally, not by text, so a reformat can't stop finding it."""
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "poll_dm_fallback":
            for node in ast.walk(fn):
                if not isinstance(node, ast.If):
                    continue
                test = node.test
                if not (isinstance(test, ast.Call)
                        and isinstance(test.func, ast.Attribute)
                        and test.func.attr == "startswith"
                        and test.args
                        and isinstance(test.args[0], ast.Tuple)):
                    continue
                consts = {c.value for c in test.args[0].elts if isinstance(c, ast.Constant)}
                if consts == {"question-", "insight-"}:
                    return node
    return None


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        print(f"  FAIL: {name} {detail}")
        failures.append(name)


def poll_dm_fallback_body() -> str:
    m = re.search(r"async def poll_dm_fallback\(\):.*?(?=^(?:async )?def |\Z)",
                  BRIDGE_SRC, re.MULTILINE | re.DOTALL)
    assert m, "poll_dm_fallback not found"
    return m.group(0)


# --- Half 1: bridge never DMs digest artifacts --------------------------------
body = poll_dm_fallback_body()

digest_branch = re.search(
    r'if f\.name\.startswith\(\("question-", "insight-"\)\):'
    r'.*?archive_file\(f, "results", f\.stem\).*?continue',
    body, re.DOTALL)
check("digest branch exists (question-/insight- -> archive, no DM)",
      digest_branch is not None)

if digest_branch:
    grace_pos = body.find("if age < GRACE_SECONDS:")
    send_pos = body.rfind("dm-result.py")  # rfind: first hit is the docstring
    check("digest branch sits AFTER the voice first-dibs grace check",
          grace_pos != -1 and digest_branch.start() > grace_pos,
          f"(grace at {grace_pos}, branch at {digest_branch.start()})")
    check("digest branch sits BEFORE the dm-result send",
          send_pos != -1 and digest_branch.start() < send_pos,
          f"(branch at {digest_branch.start()}, send at {send_pos})")

check("briefing-/friction- delivery NOT swept into the digest branch",
      'startswith(("question-", "insight-"))' in body
      and 'startswith(("question-", "insight-", "briefing-"' not in body)


# --- Half 1b: exercise the digest branch for real (not just structurally) -----
# Wrapped in a throwaway one-iteration `for` so `continue` stays valid;
# original line numbers are kept so coverage.py attributes the run correctly.
digest_if = _find_digest_if(ast.parse(BRIDGE_SRC))
check("digest branch If node found structurally (for real execution)", digest_if is not None)

if digest_if is not None:
    _loc = dict(lineno=digest_if.lineno, col_offset=0,
                end_lineno=digest_if.end_lineno, end_col_offset=0)
    _wrapper = ast.For(
        target=ast.Name(id="_dm_digest_iter", ctx=ast.Store(), **_loc),
        iter=ast.List(elts=[ast.Constant(value=0, **_loc)], ctx=ast.Load(), **_loc),
        body=[digest_if], orelse=[],
        **_loc,
    )
    _mod = ast.Module(body=[_wrapper], type_ignores=[])
    ast.fix_missing_locations(_mod)
    digest_code = compile(_mod, str(BRIDGE_PATH), "exec")

    class _FakeResultFile:
        def __init__(self, name):
            self.name = name
            self.stem = name.rsplit(".", 1)[0]

    archived = []
    fobj = _FakeResultFile("question-999.txt")
    digest_ns = {
        "f": fobj,
        "archive_file": lambda f, kind, stem: archived.append((f.name, kind, stem)),
        "print": lambda *a, **kw: None,
    }
    exec(digest_code, digest_ns)
    check("executing the digest branch archives (not DM) with the right args",
          archived == [("question-999.txt", "results", "question-999")],
          f"(got {archived})")


# --- Half 2: notify_voice supersedes older digests ----------------------------
m = re.search(r"def notify_voice\(questions\):.*?(?=^def |\Z)",
              CPQ_SRC, re.MULTILINE | re.DOTALL)
assert m, "notify_voice not found"
nv_src = m.group(0)

check("notify_voice unlinks older question-*.txt before writing",
      re.search(r'glob\("question-\*\.txt"\).*?unlink', nv_src, re.DOTALL) is not None)

# Exercise it for real: two stale digests + one write -> exactly one file left.
nv_code = _compile_function(CPQ_PATH, CPQ_SRC, "notify_voice")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    (tmp / "question-111.txt").write_text("old wall 1")
    (tmp / "question-222.txt").write_text("old wall 2")
    (tmp / "task-333.txt").write_text("unrelated result")
    ns = {"RESULTS_DIR": tmp, "time": __import__("time"), "Path": Path}
    exec(nv_code, ns)
    ns["notify_voice"]([{"title": "q1"}, {"title": "q2"}])
    remaining = sorted(p.name for p in tmp.glob("question-*.txt"))
    check("exactly one digest remains after a new write",
          len(remaining) == 1, f"(got {remaining})")
    check("the survivor is the NEW digest",
          bool(remaining) and remaining[0] not in ("question-111.txt", "question-222.txt"))
    check("non-digest results untouched", (tmp / "task-333.txt").exists())

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("all checks passed")
