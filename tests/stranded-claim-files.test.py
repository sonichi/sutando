#!/usr/bin/env python3
"""Tests for find_task_file() fix — stranded .claimed-core-N.txt files (issue #933).

Verifies that each bridge's find_task_file() helper correctly resolves the
actual on-disk path for a task, whether bare or claimed by claim_task.py.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

PASS = 0
FAIL = 0

def ok(label):
    global PASS
    PASS += 1
    print(f"PASS  {label}")

def fail(label, detail=""):
    global FAIL
    FAIL += 1
    print(f"FAIL  {label}" + (f": {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Load find_task_file from each bridge
# ---------------------------------------------------------------------------
def _load_fn(bridge_path: Path, fn_name: str):
    spec = importlib.util.spec_from_file_location("_bridge_mod", bridge_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    except Exception:
        pass
    return getattr(mod, fn_name, None)


slack_ftf = _load_fn(REPO_ROOT / "src" / "slack-bridge.py", "find_task_file")
telegram_ftf = _load_fn(REPO_ROOT / "src" / "telegram-bridge.py", "find_task_file")
discord_ftf = _load_fn(REPO_ROOT / "src" / "discord-bridge.py", "find_task_file")


# ---------------------------------------------------------------------------
# T1 — slack-bridge has find_task_file (structural AST check — bridge exits
#      early without tokens so runtime import is not possible)
# ---------------------------------------------------------------------------
import ast as _ast
def _has_fn(path: Path, name: str) -> bool:
    try:
        tree = _ast.parse(path.read_text())
        return any(isinstance(n, _ast.FunctionDef) and n.name == name for n in _ast.walk(tree))
    except Exception:
        return False

if _has_fn(REPO_ROOT / "src" / "slack-bridge.py", "find_task_file"):
    ok("T1: slack-bridge.py defines find_task_file")
else:
    fail("T1: slack-bridge.py defines find_task_file")

# ---------------------------------------------------------------------------
# T2 — telegram-bridge has find_task_file
# ---------------------------------------------------------------------------
if _has_fn(REPO_ROOT / "src" / "telegram-bridge.py", "find_task_file"):
    ok("T2: telegram-bridge.py defines find_task_file")
else:
    fail("T2: telegram-bridge.py defines find_task_file")

# ---------------------------------------------------------------------------
# T3 — discord-bridge has find_task_file
# ---------------------------------------------------------------------------
if callable(discord_ftf):
    ok("T3: discord-bridge.py exports find_task_file (runtime import)")
else:
    fail("T3: discord-bridge.py exports find_task_file (runtime import)")

# ---------------------------------------------------------------------------
# Shared helper for actual logic tests
# ---------------------------------------------------------------------------
def import_ftf_from_source():
    """Inline find_task_file implementation extracted from slack-bridge.py source."""
    src = (REPO_ROOT / "src" / "slack-bridge.py").read_text()
    # Extract just the helper function
    import ast, textwrap
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "find_task_file":
            start = node.lineno - 1
            end = node.end_lineno
            lines = src.splitlines()[start:end]
            code = textwrap.dedent("\n".join(lines))
            ns = {"Path": Path}
            exec(compile(code, "<test>", "exec"), ns)
            return ns["find_task_file"]
    return None

ftf = import_ftf_from_source()

# ---------------------------------------------------------------------------
# T4 — finds bare task file when it exists
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    td = Path(d)
    task_id = "task-1234567890000"
    bare = td / f"{task_id}.txt"
    bare.write_text("id: task-1234567890000\n")
    result = ftf(td, task_id)
    if result == bare:
        ok("T4: find_task_file returns bare .txt when it exists")
    else:
        fail("T4: find_task_file returns bare .txt when it exists", f"got {result}")

# ---------------------------------------------------------------------------
# T5 — finds claimed file when bare doesn't exist
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    td = Path(d)
    task_id = "task-9999999990000"
    claimed = td / f"{task_id}.claimed-core-2.txt"
    claimed.write_text("id: task-9999999990000\n")
    result = ftf(td, task_id)
    if result == claimed:
        ok("T5: find_task_file returns .claimed-core-N.txt when bare absent")
    else:
        fail("T5: find_task_file returns .claimed-core-N.txt when bare absent", f"got {result}")

# ---------------------------------------------------------------------------
# T6 — returns None when neither bare nor claimed exists
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    td = Path(d)
    result = ftf(td, "task-0000000000000")
    if result is None:
        ok("T6: find_task_file returns None when no matching file exists")
    else:
        fail("T6: find_task_file returns None when no matching file exists", f"got {result}")

# ---------------------------------------------------------------------------
# T7 — prefers bare over claimed when both exist (shouldn't happen, but safe)
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    td = Path(d)
    task_id = "task-1111111110000"
    bare = td / f"{task_id}.txt"
    bare.write_text("id: task-1111111110000\n")
    claimed = td / f"{task_id}.claimed-core-1.txt"
    claimed.write_text("id: task-1111111110000\n")
    result = ftf(td, task_id)
    if result == bare:
        ok("T7: find_task_file prefers bare .txt when both bare and claimed exist")
    else:
        fail("T7: find_task_file prefers bare .txt when both bare and claimed exist", f"got {result}")

# ---------------------------------------------------------------------------
# T8 — slack-bridge.py: archive call uses find_task_file (no bare literal)
# ---------------------------------------------------------------------------
content = (REPO_ROOT / "src" / "slack-bridge.py").read_text()
# The single archive call for tasks should no longer use the bare string
bare_call = 'archive_file(TASKS_DIR / f"{task_id}.txt", "tasks"'
if bare_call not in content:
    ok("T8: slack-bridge.py archive path uses find_task_file, not bare literal")
else:
    fail("T8: slack-bridge.py archive path uses find_task_file, not bare literal",
         "bare literal still present")

# ---------------------------------------------------------------------------
# T9 — telegram-bridge.py: archive calls use find_task_file
# ---------------------------------------------------------------------------
content_tg = (REPO_ROOT / "src" / "telegram-bridge.py").read_text()
bare_tg = 'archive_file(task_file, "tasks"'
if bare_tg not in content_tg:
    ok("T9: telegram-bridge.py archive path uses find_task_file, not bare literal")
else:
    fail("T9: telegram-bridge.py archive path uses find_task_file, not bare literal")

# ---------------------------------------------------------------------------
# T10 — discord-bridge.py: main poll_results archive uses find_task_file
# ---------------------------------------------------------------------------
content_dc = (REPO_ROOT / "src" / "discord-bridge.py").read_text()
# Count remaining bare archive patterns vs find_task_file usages
import re
bare_archive = re.findall(r'archive_file\(TASKS_DIR / f"\{task_id\}\.txt"', content_dc)
bare_archive += re.findall(r'archive_file\(TASKS_DIR / f"\{_task_id\}\.txt"', content_dc)
ftf_archive = re.findall(r'find_task_file\(TASKS_DIR', content_dc)
if len(bare_archive) == 0 and len(ftf_archive) >= 5:
    ok(f"T10: discord-bridge.py has {len(ftf_archive)} find_task_file calls, 0 bare archive paths")
else:
    fail("T10: discord-bridge.py bare archive literal check",
         f"bare={len(bare_archive)}, ftf_calls={len(ftf_archive)}")

# ---------------------------------------------------------------------------
# T11 — discord-bridge.py: tier-read sites also use find_task_file
# ---------------------------------------------------------------------------
tier_bare = re.findall(r'TASKS_DIR / f"\{task_id\}\.txt"\)\.read_text\(\)', content_dc)
tier_bare += re.findall(r'TASKS_DIR / f"\{_task_id\}\.txt"\)\.read_text\(\)', content_dc)
if len(tier_bare) == 0:
    ok("T11: discord-bridge.py tier-read sites use find_task_file (no bare .read_text())")
else:
    fail("T11: discord-bridge.py tier-read sites use find_task_file",
         f"{len(tier_bare)} bare read_text calls remain")

# ---------------------------------------------------------------------------
# T12 — find_task_file docstring mentions #933
# ---------------------------------------------------------------------------
dc_src = (REPO_ROOT / "src" / "discord-bridge.py").read_text()
if "#933" in dc_src:
    ok("T12: discord-bridge.py find_task_file docstring references issue #933")
else:
    fail("T12: discord-bridge.py find_task_file docstring references issue #933")

# ---------------------------------------------------------------------------
# T13 — write sites (task creation) unchanged — still use bare path
# ---------------------------------------------------------------------------
slack_src = (REPO_ROOT / "src" / "slack-bridge.py").read_text()
if 'task_file = TASKS_DIR / f"{task_id}.txt"' in slack_src:
    ok("T13: slack-bridge.py task-write site still uses bare path (write, not archive)")
else:
    fail("T13: slack-bridge.py task-write site still uses bare path")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{PASS}/{PASS+FAIL} tests passed")
if __name__ == "__main__":
    pass
