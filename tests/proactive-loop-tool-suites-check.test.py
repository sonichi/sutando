#!/usr/bin/env python3
"""tool-suites-check: the checker that guards every other instrument had no
suite of its own. This is it — focused on the `extras` mechanism, whose whole
purpose is that a MISSING declared suite must be loud rather than silent.

Run:  python3 skills/proactive-loop/scripts/tool-suites-check.test.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "skills" / "proactive-loop" / "scripts" / "tool-suites-check.py"
spec = importlib.util.spec_from_file_location("tsc", TOOL)
tsc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tsc)

FAILURES = []


def check(label, got, want):
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r} want {want!r}")
    else:
        print(f"  ok   {label}")


def scaffold(td, extras=None, suite_body="print('PASS')\n", make_suite=True):
    """A minimal workspace: scripts/ with one passing suite, plus optional extras."""
    ws = Path(td) / "ws"
    (ws / "scripts").mkdir(parents=True)
    (ws / "state").mkdir()
    (ws / "scripts" / "dummy.test.py").write_text("print('PASS — dummy')\n")
    repo = Path(td) / "repo"
    (repo / "tests").mkdir(parents=True)
    if make_suite:
        (repo / "tests" / "extra.test.py").write_text(suite_body)
    if extras is not None:
        (ws / "state" / "tool-suites-extra.json").write_text(json.dumps({"suites": extras}))
    return ws, repo


def run(ws, repo, *extra_argv):
    p = subprocess.run([sys.executable, str(TOOL), "--workspace", str(ws),
                        "--repo", str(repo), "--force", *extra_argv],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


print("1. with no extras file the checker behaves exactly as before")
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td)
    rc, out = run(ws, repo)
    check("exit 0", rc, 0)
    check("ran the scripts/ suite", "dummy.test.py" in out, True)

print("2. a declared extra suite IS run")
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td, extras=["tests/extra.test.py"])
    rc, out = run(ws, repo)
    check("exit 0", rc, 0)
    check("extra suite ran", "extra.test.py" in out, True)
    check("counted in the total", "2 of 2 suites pass" in out, True)

print("3. a declared extra that FAILS fails the whole check")
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td, extras=["tests/extra.test.py"],
                        suite_body="import sys\nprint('boom')\nsys.exit(1)\n")
    rc, out = run(ws, repo)
    check("exit 1", rc, 1)
    check("names the failing suite", "extra.test.py" in out, True)

print("4. ⚠ THE DISCRIMINATING CASE — a declared suite that is MISSING must be LOUD.")
print("   Skipping it silently would report a clean bill for a suite that never ran,")
print("   which is the exact failure this checker exists to prevent.")
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td, extras=["tests/extra.test.py"], make_suite=False)
    rc, out = run(ws, repo)
    check("exit 2 (cannot answer), NOT 0", rc, 2)
    check("names the missing path", "extra.test.py" in out, True)
    check("does not claim suites passed", "suites pass" in out, False)

print("5. a malformed extras file cannot pass as 'no extras'")
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td)
    (ws / "state" / "tool-suites-extra.json").write_text("{not json")
    check("unreadable -> exit 2", run(ws, repo)[0], 2)
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td)
    (ws / "state" / "tool-suites-extra.json").write_text(json.dumps({"suites": "tests/x.py"}))
    check("bare string -> exit 2", run(ws, repo)[0], 2)
with tempfile.TemporaryDirectory() as td:
    # A dict whose KEY is a real suite path: without the isinstance(list) guard it
    # iterates to that key and runs. Only this input exercises the type guard.
    ws, repo = scaffold(td)
    (ws / "state" / "tool-suites-extra.json").write_text(
        json.dumps({"suites": {"tests/extra.test.py": True}}))
    check("dict-with-valid-key -> exit 2, not a silent run", run(ws, repo)[0], 2)
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td, extras=[""])
    check("empty entry -> exit 2", run(ws, repo)[0], 2)

print("6. an extra suite joins the CHANGE trigger, not just the run set")
with tempfile.TemporaryDirectory() as td:
    ws, repo = scaffold(td, extras=["tests/extra.test.py"])
    run(ws, repo)                                    # seed the sentinel (--force)
    p = subprocess.run([sys.executable, str(TOOL), "--workspace", str(ws), "--repo", str(repo)],
                       capture_output=True, text=True)
    check("second run is fresh", "fresh" in p.stdout, True)
    os.utime(repo / "tests" / "extra.test.py", None)  # touch ONLY the extra
    p = subprocess.run([sys.executable, str(TOOL), "--workspace", str(ws), "--repo", str(repo)],
                       capture_output=True, text=True)
    check("touching the extra re-triggers", "running" in p.stdout, True)

if FAILURES:
    print(f"\nFAIL — {len(FAILURES)} check(s):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("\nPASS — tool-suites-check tests")
