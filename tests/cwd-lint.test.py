#!/usr/bin/env python3
"""Tests for scripts/lint-cwd-writes.py (issue #863 — ban bare cwd() outside resolvers)."""

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "lint-cwd-writes.py"

spec = importlib.util.spec_from_file_location("lint_cwd", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

check_py = mod.check_py
check_ts = mod.check_ts

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
# T1 — script exists
# ---------------------------------------------------------------------------
if SCRIPT.exists():
    ok("T1: lint script exists at scripts/lint-cwd-writes.py")
else:
    fail("T1: lint script exists at scripts/lint-cwd-writes.py")

# ---------------------------------------------------------------------------
# T2 — detects Path.cwd() in Python file
# ---------------------------------------------------------------------------
with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False,
                                  dir=REPO_ROOT / "src") as tf:
    tf.write("from pathlib import Path\ncwd = Path.cwd()\n")
    viol_py = Path(tf.name)
try:
    hits = check_py(viol_py)
    if hits and hits[0][0] == 2:
        ok("T2: check_py detects Path.cwd() at correct line")
    else:
        fail("T2: check_py detects Path.cwd() at correct line", f"hits={hits}")
finally:
    viol_py.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# T3 — detects os.getcwd() in Python file
# ---------------------------------------------------------------------------
with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False,
                                  dir=REPO_ROOT / "src") as tf:
    tf.write("import os\np = os.getcwd()\n")
    viol_py2 = Path(tf.name)
try:
    hits = check_py(viol_py2)
    if hits and hits[0][0] == 2:
        ok("T3: check_py detects os.getcwd()")
    else:
        fail("T3: check_py detects os.getcwd()", f"hits={hits}")
finally:
    viol_py2.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# T4 — ignores Path.cwd() in a Python comment
# ---------------------------------------------------------------------------
with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False,
                                  dir=REPO_ROOT / "src") as tf:
    tf.write("# Previously used Path.cwd() here — now uses resolve_workspace()\n")
    comment_py = Path(tf.name)
try:
    hits = check_py(comment_py)
    if not hits:
        ok("T4: check_py ignores Path.cwd() in a comment line")
    else:
        fail("T4: check_py ignores Path.cwd() in a comment line", f"hits={hits}")
finally:
    comment_py.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# T5 — allows Path.cwd() in workspace_default.py (canonical resolver)
# ---------------------------------------------------------------------------
workspace_py = REPO_ROOT / "src" / "workspace_default.py"
if workspace_py.exists():
    hits = check_py(workspace_py)
    if not hits:
        ok("T5: check_py allows Path.cwd() in src/workspace_default.py")
    else:
        fail("T5: check_py allows Path.cwd() in src/workspace_default.py",
             f"{len(hits)} violation(s) flagged in allowed file")
else:
    ok("T5: workspace_default.py not present — skip (no violations possible)")

# ---------------------------------------------------------------------------
# T6 — detects process.cwd() in TS file
# ---------------------------------------------------------------------------
with tempfile.NamedTemporaryFile(suffix=".ts", mode="w", delete=False,
                                  dir=REPO_ROOT / "src") as tf:
    tf.write("const d = process.cwd();\n")
    viol_ts = Path(tf.name)
try:
    hits = check_ts(viol_ts)
    if hits and hits[0][0] == 1:
        ok("T6: check_ts detects process.cwd() in TS file")
    else:
        fail("T6: check_ts detects process.cwd() in TS file", f"hits={hits}")
finally:
    viol_ts.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# T7 — ignores process.cwd() in a TS comment
# ---------------------------------------------------------------------------
with tempfile.NamedTemporaryFile(suffix=".ts", mode="w", delete=False,
                                  dir=REPO_ROOT / "src") as tf:
    tf.write("// was: process.cwd() — now uses resolveWorkspace()\n")
    comment_ts = Path(tf.name)
try:
    hits = check_ts(comment_ts)
    if not hits:
        ok("T7: check_ts ignores process.cwd() in a TS comment line")
    else:
        fail("T7: check_ts ignores process.cwd() in a TS comment line", f"hits={hits}")
finally:
    comment_ts.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# T8 — allows process.cwd() in workspace_default.ts
# ---------------------------------------------------------------------------
workspace_ts = REPO_ROOT / "src" / "workspace_default.ts"
if workspace_ts.exists():
    hits = check_ts(workspace_ts)
    if not hits:
        ok("T8: check_ts allows process.cwd() in src/workspace_default.ts")
    else:
        fail("T8: check_ts allows process.cwd() in src/workspace_default.ts",
             f"{len(hits)} violation(s) flagged in allowed file")
else:
    ok("T8: workspace_default.ts not present — skip")

# ---------------------------------------------------------------------------
# T9 — allows process.cwd() in approved inline-tools.ts
# ---------------------------------------------------------------------------
inline_ts = REPO_ROOT / "src" / "inline-tools.ts"
if inline_ts.exists():
    hits = check_ts(inline_ts)
    if not hits:
        ok("T9: check_ts allows process.cwd() in approved src/inline-tools.ts")
    else:
        fail("T9: check_ts allows process.cwd() in approved src/inline-tools.ts",
             f"{len(hits)} violation(s)")
else:
    ok("T9: inline-tools.ts not present — skip")

# ---------------------------------------------------------------------------
# T10 — CLI exits 0 for clean src/ directory
# ---------------------------------------------------------------------------
proc = subprocess.run(
    [sys.executable, str(SCRIPT)],
    capture_output=True, text=True, cwd=str(REPO_ROOT)
)
if proc.returncode == 0:
    ok("T10: CLI exits 0 — all src/ files currently clean")
else:
    fail("T10: CLI exits 0 — all src/ files currently clean",
         proc.stdout.strip()[:200])

# ---------------------------------------------------------------------------
# T11 — CLI exits 1 for a violating file
# ---------------------------------------------------------------------------
with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False,
                                  dir=REPO_ROOT / "src") as tf:
    tf.write("import os\nbase = os.getcwd()\n")
    viol3 = Path(tf.name)
try:
    proc2 = subprocess.run(
        [sys.executable, str(SCRIPT), str(viol3)],
        capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    if proc2.returncode == 1:
        ok("T11: CLI exits 1 for a file with os.getcwd() outside allowed paths")
    else:
        fail("T11: CLI exits 1 for a violating file", f"rc={proc2.returncode}")
finally:
    viol3.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# T12 — script has module docstring
# ---------------------------------------------------------------------------
content = SCRIPT.read_text()
lines = content.splitlines()
body = "\n".join(l for l in lines if not l.startswith("#!")).lstrip()
if body.startswith('"""') or body.startswith("'''"):
    ok("T12: lint script has module docstring")
else:
    fail("T12: lint script has module docstring")

# ---------------------------------------------------------------------------
# T13 — ignores process.cwd() in inline trailing TS comment
# ---------------------------------------------------------------------------
with tempfile.NamedTemporaryFile(suffix=".ts", mode="w", delete=False,
                                  dir=REPO_ROOT / "src") as tf:
    tf.write("const ws = resolveWorkspace(); // previously process.cwd()\n")
    trailing_ts = Path(tf.name)
try:
    hits = check_ts(trailing_ts)
    if not hits:
        ok("T13: check_ts ignores process.cwd() in trailing inline comment")
    else:
        fail("T13: check_ts ignores process.cwd() in trailing inline comment", f"hits={hits}")
finally:
    trailing_ts.unlink(missing_ok=True)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{PASS}/{PASS+FAIL} tests passed")
if __name__ == "__main__":
    pass
