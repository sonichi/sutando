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
PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={Path(__file__).resolve().parents[1] / '.coveragerc'}"]
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
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws),
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
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(repo)],
                       capture_output=True, text=True)
    check("second run is fresh", "fresh" in p.stdout, True)
    os.utime(repo / "tests" / "extra.test.py", None)  # touch ONLY the extra
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(repo)],
                       capture_output=True, text=True)
    check("touching the extra re-triggers", "running" in p.stdout, True)

def watched_dir_case(td):
    """A workspace whose tools live in tools/, with an EMPTY scripts/ beside it.

    The empty dir is the trap: it exists, so the `no scripts/` refusal does not
    fire, and it holds no .py, so the newest-mtime trigger has nothing to watch.
    """
    ws = Path(td) / "ws3"
    (ws / "scripts").mkdir(parents=True)          # exists, deliberately empty
    (ws / "tools").mkdir()
    (ws / "state").mkdir()
    (ws / "tools" / "thing.py").write_text("def verdict():\n    return 1\n")
    (ws / "tools" / "thing.test.py").write_text("import sys\nprint('ok')\nsys.exit(0)\n")
    return ws


with tempfile.TemporaryDirectory() as td:
    ws = watched_dir_case(td)
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                       capture_output=True, text=True)
    check("tools/ is watched when scripts/ is empty", "running" in p.stdout, True)
    check("and the suite under tools/ actually ran", "thing.test.py" in p.stdout, True)
    # The trigger must still key on an edit, not merely on the first run.
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                       capture_output=True, text=True)
    check("second run is fresh", "fresh" in p.stdout, True)
    os.utime(ws / "tools" / "thing.py", None)
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                       capture_output=True, text=True)
    check("touching a tool under tools/ re-triggers", "running" in p.stdout, True)


def both_populated_case(td):
    """BOTH scripts/ and tools/ hold .py — the ordinary mid-migration state.

    `next(...)` returned scripts/ and stopped, so tools/ was neither watched nor
    DISCOVERED: its suites never ran at all (3852-r1). Healthy from inside,
    because the selected dir still yields a non-empty runnable set.
    """
    ws = Path(td) / "ws5"
    for d in ("scripts", "tools", "state"):
        (ws / d).mkdir(parents=True)
    (ws / "scripts" / "legacy.py").write_text("def verdict():\n    return 1\n")
    (ws / "scripts" / "legacy.test.py").write_text("import sys\nprint('ok')\nsys.exit(0)\n")
    (ws / "tools" / "newer.py").write_text("def verdict():\n    return 2\n")
    (ws / "tools" / "newer.test.py").write_text("import sys\nprint('ok')\nsys.exit(0)\n")
    return ws


with tempfile.TemporaryDirectory() as td:
    ws = both_populated_case(td)
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                       capture_output=True, text=True)
    check("both dirs populated: the scripts/ suite runs", "legacy.test.py" in p.stdout, True)
    check("...and the tools/ suite runs too (next() dropped it)", "newer.test.py" in p.stdout, True)
    check("...counted as 2, so neither is silently outside the set",
          "2 of 2 suites pass" in p.stdout, True)
    # The trigger must key on an edit in EITHER dir, not only the first one.
    subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                   capture_output=True, text=True)
    os.utime(ws / "tools" / "newer.py", None)
    p = subprocess.run([*PYBASE, str(TOOL), "--workspace", str(ws), "--repo", str(Path(td))],
                       capture_output=True, text=True)
    check("touching a tool in the UNSELECTED dir re-triggers", "running" in p.stdout, True)


print("7. in-process: the trigger rule and the two scope refusals")
check("no recorded run -> run", tsc.should_run({}, 0.0, 3600, 100.0)[0], True)
check("unchanged and young -> fresh", tsc.should_run({"tools_mtime": 5.0, "ran_at": 90.0}, 5.0, 3600, 100.0)[0], False)
go, why = tsc.should_run({"tools_mtime": 5.0, "ran_at": 0.0}, 5.0, 3600, 7200.0)
check("unchanged but older than --max-age -> run", (go, "last run was" in why), (True, True))
with tempfile.TemporaryDirectory() as td:
    check("no scripts/ dir -> exit 2", tsc.main(["--workspace", td]), 2)
    (Path(td) / "scripts").mkdir()
    check("zero suites -> exit 2 (a scope result, not a clean bill)", tsc.main(["--workspace", td]), 2)

def stale_bytecode_case(td):
    """A tool the suite imports, run once, then edited to the same length.

    A same-size edit in the same second is served the pre-edit bytecode, so the
    suite asserts against code that is not on disk and still exits 0.
    """
    ws = Path(td) / "ws2"
    (ws / "scripts").mkdir(parents=True)
    (ws / "state").mkdir()
    (ws / "scripts" / "thing.py").write_text('def verdict():\n    return "AAAA"\n')
    (ws / "scripts" / "thing.test.py").write_text(
        "import importlib.util, sys\n"
        "from pathlib import Path\n"
        "p = Path(__file__).with_name('thing.py')\n"
        "s = importlib.util.spec_from_file_location('thing', p)\n"
        "m = importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
        "print('verdict', m.verdict())\n"
        "sys.exit(0 if m.verdict() == 'AAAA' else 1)\n")
    return ws


with tempfile.TemporaryDirectory() as td:
    ws = stale_bytecode_case(td)
    args = ["--workspace", str(ws), "--repo", str(Path(td))]
    r1 = subprocess.run(PYBASE + [str(TOOL)] + args, capture_output=True, text=True)
    check("stale-bytecode: first run passes (the tool really does say AAAA)", r1.returncode, 0)

    tool = ws / "scripts" / "thing.py"
    tool.write_text(tool.read_text().replace('"AAAA"', '"BBBB"'))
    check("stale-bytecode: the edit is on disk", '"BBBB"' in tool.read_text(), True)

    r2 = subprocess.run(PYBASE + [str(TOOL)] + args, capture_output=True, text=True)
    # Without a fresh cache the suite imports the PRE-edit module, still sees
    # AAAA, and this run reports 0 — green on code that no longer exists.
    check("stale-bytecode: the edited tool is seen, so the suite FAILS",
          r2.returncode, 1)
    check("stale-bytecode: and the failure names the suite",
          "thing.test.py" in (r2.stdout + r2.stderr), True)

if FAILURES:
    print(f"\nFAIL — {len(FAILURES)} check(s):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("\nPASS — tool-suites-check tests")
