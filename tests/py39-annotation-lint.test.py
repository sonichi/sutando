#!/usr/bin/env python3
"""Tests for scripts/lint-py39-annotations.py (issue #961 — py3.9 baseline)."""

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "lint-py39-annotations.py"

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
spec = importlib.util.spec_from_file_location("lint_py39", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

has_future_annotations = mod.has_future_annotations
uses_union_syntax = mod.uses_union_syntax
lint_file = mod.lint_file
_insert_future_annotations = mod._insert_future_annotations

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
# T1 — script file exists
# ---------------------------------------------------------------------------
if SCRIPT.exists():
    ok("T1: lint script exists at scripts/lint-py39-annotations.py")
else:
    fail("T1: lint script exists at scripts/lint-py39-annotations.py")

# ---------------------------------------------------------------------------
# T2 — has_future_annotations: detects present
# ---------------------------------------------------------------------------
SRC_WITH = "from __future__ import annotations\nimport os\n"
if has_future_annotations(SRC_WITH):
    ok("T2: has_future_annotations detects present import")
else:
    fail("T2: has_future_annotations detects present import")

# ---------------------------------------------------------------------------
# T3 — has_future_annotations: detects absent
# ---------------------------------------------------------------------------
SRC_WITHOUT = "import os\nimport sys\n"
if not has_future_annotations(SRC_WITHOUT):
    ok("T3: has_future_annotations returns False when absent")
else:
    fail("T3: has_future_annotations returns False when absent")

# ---------------------------------------------------------------------------
# T4 — uses_union_syntax: detects X | Y in function arg annotation
# ---------------------------------------------------------------------------
SRC_ARG_UNION = "def f(x: str | None): pass\n"
if uses_union_syntax(SRC_ARG_UNION):
    ok("T4: uses_union_syntax detects str|None in arg annotation")
else:
    fail("T4: uses_union_syntax detects str|None in arg annotation")

# ---------------------------------------------------------------------------
# T5 — uses_union_syntax: detects X | Y in return annotation
# ---------------------------------------------------------------------------
SRC_RET_UNION = "def f() -> int | None: return None\n"
if uses_union_syntax(SRC_RET_UNION):
    ok("T5: uses_union_syntax detects int|None in return annotation")
else:
    fail("T5: uses_union_syntax detects int|None in return annotation")

# ---------------------------------------------------------------------------
# T6 — uses_union_syntax: detects X | Y in variable annotation
# ---------------------------------------------------------------------------
SRC_VAR_UNION = "x: str | None = None\n"
if uses_union_syntax(SRC_VAR_UNION):
    ok("T6: uses_union_syntax detects str|None in variable annotation")
else:
    fail("T6: uses_union_syntax detects str|None in variable annotation")

# ---------------------------------------------------------------------------
# T7 — uses_union_syntax: does NOT flag X | Y inside a docstring
# ---------------------------------------------------------------------------
SRC_DOC_UNION = '''def f():
    """Returns dict with keys:
       "foo": str | None,
    """
    pass
'''
if not uses_union_syntax(SRC_DOC_UNION):
    ok("T7: uses_union_syntax ignores X|Y inside docstrings")
else:
    fail("T7: uses_union_syntax ignores X|Y inside docstrings")

# ---------------------------------------------------------------------------
# T8 — uses_union_syntax: no false positive on bitwise OR in expressions
# ---------------------------------------------------------------------------
SRC_BITWISE = "def f(x, y):\n    return x | y\n"
if not uses_union_syntax(SRC_BITWISE):
    ok("T8: uses_union_syntax no false positive on bitwise OR in expressions")
else:
    fail("T8: uses_union_syntax no false positive on bitwise OR in expressions")

# ---------------------------------------------------------------------------
# T9 — lint_file: clean file (has future + union) → returns True
# ---------------------------------------------------------------------------
SRC_CLEAN = "from __future__ import annotations\ndef f(x: str | None): pass\n"
with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as tf:
    tf.write(SRC_CLEAN)
    clean_path = Path(tf.name)
try:
    result = lint_file(clean_path, fix=False)
    if result:
        ok("T9: lint_file returns True for file with future import + union syntax")
    else:
        fail("T9: lint_file returns True for file with future import + union syntax")
finally:
    clean_path.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# T10 — lint_file: violating file → returns False
# ---------------------------------------------------------------------------
SRC_VIOL = "import os\ndef f(x: str | None): pass\n"
with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as tf:
    tf.write(SRC_VIOL)
    viol_path = Path(tf.name)
try:
    result = lint_file(viol_path, fix=False)
    if not result:
        ok("T10: lint_file returns False for violating file (union without future)")
    else:
        fail("T10: lint_file returns False for violating file (union without future)")
finally:
    viol_path.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# T11 — lint_file --fix: adds from __future__ import annotations
# ---------------------------------------------------------------------------
SRC_VIOL2 = "import os\ndef f(x: str | None): pass\n"
with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as tf:
    tf.write(SRC_VIOL2)
    fix_path = Path(tf.name)
try:
    result = lint_file(fix_path, fix=True)
    fixed_content = fix_path.read_text()
    if result and "from __future__ import annotations" in fixed_content:
        ok("T11: lint_file --fix inserts from __future__ import annotations")
    else:
        fail("T11: lint_file --fix inserts from __future__ import annotations",
             f"result={result}, has_future={'from __future__' in fixed_content}")
finally:
    fix_path.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# T12 — _insert_future_annotations: doesn't double-insert
# ---------------------------------------------------------------------------
SRC_ALREADY = "from __future__ import annotations\nimport os\ndef f(x: str | None): pass\n"
new_src = _insert_future_annotations(SRC_ALREADY)
count = new_src.count("from __future__ import annotations")
if count == 1:
    ok("T12: _insert_future_annotations doesn't double-insert")
else:
    fail("T12: _insert_future_annotations doesn't double-insert", f"count={count}")

# ---------------------------------------------------------------------------
# T13 — CLI: exits 0 for clean src/ directory (no current violations)
# ---------------------------------------------------------------------------
proc = subprocess.run(
    [sys.executable, str(SCRIPT)],
    capture_output=True, text=True,
    cwd=str(REPO_ROOT)
)
if proc.returncode == 0:
    ok("T13: CLI exits 0 — all src/*.py currently clean (no py3.9 violations)")
else:
    fail("T13: CLI exits 0 — all src/*.py currently clean", proc.stdout.strip())

# ---------------------------------------------------------------------------
# T14 — CLI: exits 1 for a temp violating file
# ---------------------------------------------------------------------------
SRC_VIOL3 = "import os\ndef bad(x: dict | None): pass\n"
with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as tf:
    tf.write(SRC_VIOL3)
    viol_path2 = Path(tf.name)
try:
    proc2 = subprocess.run(
        [sys.executable, str(SCRIPT), str(viol_path2)],
        capture_output=True, text=True,
        cwd=str(REPO_ROOT)
    )
    if proc2.returncode == 1:
        ok("T14: CLI exits 1 for a file with union syntax missing future import")
    else:
        fail("T14: CLI exits 1 for violating file", f"rc={proc2.returncode} stdout={proc2.stdout[:80]}")
finally:
    viol_path2.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# T15 — script has module docstring (describes what it does)
# ---------------------------------------------------------------------------
content = SCRIPT.read_text()
# Skip shebang line before checking for docstring
lines = content.splitlines()
body = "\n".join(l for l in lines if not l.startswith("#!")).lstrip()
if body.startswith('"""') or body.startswith("'''"):
    ok("T15: lint script has module docstring")
else:
    fail("T15: lint script has module docstring")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{PASS}/{PASS+FAIL} tests passed")
if __name__ == "__main__":
    pass
