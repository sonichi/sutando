#!/usr/bin/env python3
"""Tests for memory-index-budget.py. Run: python3 skills/proactive-loop/scripts/memory-index-budget.test.py

Fixtures are built against the REAL health-check limit rather than a mocked one,
because the thing under test is the delegation. A stubbed limit would pass with a
private copy of 25,000 hardcoded — the exact defect the guard refuses to have.
"""
import contextlib
import importlib.util
import io
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parents[0]
SCRIPT = REPO / "skills" / "proactive-loop" / "scripts" / "memory-index-budget.py"

spec = importlib.util.spec_from_file_location("mib", SCRIPT)
mib = importlib.util.module_from_spec(spec); spec.loader.exec_module(mib)

MOD = mib._health_check(REPO)
assert MOD is not None, f"health-check.py not importable from {REPO} — cannot run these tests"
LIMIT = MOD.MEMORY_INDEX_LOAD_BYTES

fails = []
ran = 0
def check(name, cond, detail=""):
    global ran; ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(name)

def row(i, pad=200):
    return f"- [row {i:04d}](feedback_row_{i:04d}.md) — {'x' * pad}\n"

def index_of(nbytes):
    """An index whose effective text is just under `nbytes`."""
    out, tot, i = [], 0, 0
    while tot + len(row(i).encode()) <= nbytes:
        out.append(row(i)); tot += len(row(i).encode()); i += 1
    return "".join(out)

print("memory-index-budget")

# --- the report mode -------------------------------------------------------
small = index_of(LIMIT // 2)
r = mib.evaluate(MOD, small)
check("report: a small index drops nothing", r["already_dropped"] == [])
check("report: bytes match the delegated measurement",
      r["bytes"] == mib.loaded_lines(MOD, small)[1])
check("report: the limit comes from health-check, not a local constant",
      r["limit"] == MOD.MEMORY_INDEX_LOAD_BYTES)

over = index_of(LIMIT) + row(9998) + row(9999)
r = mib.evaluate(MOD, over)
check("report: an over-budget index NAMES the rows already past the cut",
      len(r["already_dropped"]) >= 2, f"got {len(r['already_dropped'])}")

# --- adding: the two distinct failure modes --------------------------------
full = index_of(LIMIT)
r = mib.evaluate(MOD, full, row(5000))
check("append to a full index: the ADDITION is what fails to load",
      r["addition_loads"] is False and r["dropped"] == [],
      f"loads={r['addition_loads']} dropped={len(r['dropped'])}")

r = mib.evaluate(MOD, full, row(5000), at_top=True)
check("insert at top of a full index: an EXISTING row is dropped",
      len(r["dropped"]) >= 1 and r["addition_loads"] is True,
      f"dropped={len(r['dropped'])} loads={r['addition_loads']}")
check("the dropped row is named, not just counted",
      r["dropped"] and r["dropped"][0].startswith("- [row"), str(r["dropped"][:1]))

r = mib.evaluate(MOD, small, row(5000))
check("adding to a half-full index is safe",
      r["dropped"] == [] and r["addition_loads"] is True)

# --- strip delegation ------------------------------------------------------
commented = "<!-- " + "y" * (LIMIT // 2) + " -->\n" + small
r = mib.evaluate(MOD, commented, row(5000))
check("a huge HTML comment costs no budget (strip is delegated, not re-implemented)",
      r["dropped"] == [] and r["addition_loads"] is True,
      "raw-text measurement would blow the budget here")

# --- refusal ---------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    check("no health-check.py -> None, so main() can refuse", mib._health_check(pathlib.Path(d)) is None)
    rc = mib.main(["--repo", d, "--index", str(SCRIPT)])
    check("exit 2 when the authority is unreadable (never a hardcoded fallback)", rc == 2, f"rc={rc}")

with tempfile.TemporaryDirectory() as d:
    rc = mib.main(["--repo", str(REPO), "--index", str(pathlib.Path(d) / "nope.md")])
    check("exit 2 when the index is absent", rc == 2, f"rc={rc}")

# --- exit codes end to end -------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    p = pathlib.Path(d) / "MEMORY.md"; p.write_text(full)
    with contextlib.redirect_stdout(io.StringIO()):
        rc_bad = mib.main(["--repo", str(REPO), "--index", str(p), "--adding", row(5000).strip()])
    p.write_text(small)
    with contextlib.redirect_stdout(io.StringIO()):
        rc_ok = mib.main(["--repo", str(REPO), "--index", str(p), "--adding", row(5000).strip()])
    check("exit 1 refuses, exit 0 permits", (rc_bad, rc_ok) == (1, 0), f"got {(rc_bad, rc_ok)}")

print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'} "
      f"({ran - len(fails)}/{ran} assertions)")
sys.exit(1 if fails else 0)
