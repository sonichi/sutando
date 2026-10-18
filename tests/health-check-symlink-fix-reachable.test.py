#!/usr/bin/env python3
"""`--fix` must repair broken skill symlinks without an unrelated failure present.

`skill-symlinks` reports `warn` when links are missing or broken, and `warn` is
excluded from `issues` by construction. Its fix pass was written as a separate
loop precisely for that reason — and then placed inside `else:` of
`if not issues:`, i.e. inside the branch that only runs when `issues` is
NON-empty. A second gate compounded it: in `--quiet` mode
`elif codex_notifier is None: sys.exit(0)` returns before any fix runs.

Net effect: the repair fired only when some UNRELATED check was failing. On a
host whose only problem was broken symlinks — the exact case the fixer exists
for — `--fix` printed nothing and repaired nothing.

The load-bearing assertion is the FIRST one. The second (warn + unrelated
failure) passed before the fix too, so on its own it proves nothing; it is here
to show the fix did not simply move the breakage to the other branch.
"""
import importlib.util
import io
import sys
from pathlib import Path

MOD = Path(__file__).resolve().parent.parent / "src" / "health-check.py"

fails = []
def check(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(name)

def run_capturing(checks, argv):
    """Same, but return (fired, stdout, stderr) so the --json contract is testable."""
    import contextlib
    import io
    spec = importlib.util.spec_from_file_location("hc_under_test", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    called = []
    m.fix_skill_symlinks = lambda c: (
        called.append(c["name"]),
        {"name": "skill-symlinks", "status": "ok", "detail": "relinked"},
    )[1]
    # The re-run is what decides the reported verdict now, so it is stubbed with
    # a DISTINGUISHABLE detail. If the payload ever carries "relinked" again the
    # code went back to trusting the fixer's self-report.
    m.check_skill_symlinks = lambda: {
        "name": "skill-symlinks", "status": "ok", "detail": "re-measured: all linked",
    }
    m.run_all_checks = lambda: [dict(c) for c in checks]
    saved, sys.argv = sys.argv, ["health-check.py"] + argv
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            m.main()
    except SystemExit:
        pass
    finally:
        sys.argv = saved
    return bool(called), out.getvalue(), err.getvalue()


def run(checks, argv):
    """Load health-check fresh, stub the repair, run main(), report if it fired."""
    spec = importlib.util.spec_from_file_location("hc_under_test", MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    called = []
    m.fix_skill_symlinks = lambda c: (
        called.append(c["name"]),
        {"name": "skill-symlinks", "status": "ok", "detail": "relinked"},
    )[1]
    m.run_all_checks = lambda: [dict(c) for c in checks]
    saved, sys.argv = sys.argv, ["health-check.py"] + argv
    try:
        m.main()
    except SystemExit:
        pass
    finally:
        sys.argv = saved
    return bool(called)

BROKEN = {"name": "skill-symlinks", "status": "warn",
          "detail": "2 skill(s) not linked", "_unlinked": ["a", "b"]}
OK = {"name": "other", "status": "ok", "detail": ""}
FAILING = {"name": "bridge", "status": "fail", "detail": "down"}
LINKED = {"name": "skill-symlinks", "status": "ok", "detail": "all 58 linked"}

for mode in (["--fix", "--quiet"], ["--fix"]):
    tag = " ".join(mode)
    # THE regression: broken symlinks and nothing else wrong.
    check(f"[{tag}] warn-only host still gets the repair", run([BROKEN, OK], mode), True)
    # Control: the pre-fix world passed this one, so it cannot carry the test.
    check(f"[{tag}] warn + unrelated failure still repairs", run([BROKEN, FAILING], mode), True)
    # Control: nothing to fix -> the pass must not invent work.
    check(f"[{tag}] healthy symlinks -> no repair attempted", run([LINKED, OK], mode), False)

# Control: without --fix, a broken warn must never be silently repaired.
check("no --fix flag -> repair never runs", run([BROKEN, OK], ["--quiet"]), False)

# --- mixed fixable + ORPHANED (blocking review of #2663) --------------------
# `fix_skill_symlinks` repairs only _unlinked/_broken and its status is computed
# from what it repaired — it never looks at _orphaned, which it deliberately
# leaves alone. Copying that status onto the check reported `ok` while a dangling
# link survived. The fix re-runs the check instead; a repair's self-report is not
# evidence of the resulting state.
def _apply_with(fix_result, recheck):
    spec_m = importlib.util.spec_from_file_location("hc_mixed", MOD)
    mm = importlib.util.module_from_spec(spec_m)
    spec_m.loader.exec_module(mm)
    mm.fix_skill_symlinks = lambda c: fix_result
    mm.check_skill_symlinks = lambda: recheck
    chk = {"name": "skill-symlinks", "status": "warn", "detail": "1 unlinked, 1 orphaned",
           "_unlinked": ["good"], "_orphaned": ["foreign"]}
    mm.apply_skill_symlink_fixes([chk], stream=io.StringIO())
    return chk

_mixed = _apply_with(
    {"name": "skill-symlinks", "status": "ok", "detail": "linked 1 (fixable)"},
    {"name": "skill-symlinks", "status": "warn", "detail": "1 orphaned link remains",
     "_orphaned": ["foreign"]})
check("fixable+orphaned -> still warn, not a false clean", _mixed["status"], "warn")
check("...and the surviving orphan is still named", _mixed.get("_orphaned"), ["foreign"])
check("...and the detail is the re-measure, not the fixer's 'linked 1 (fixable)'",
      _mixed["detail"], "1 orphaned link remains")

# --- the --json contract (regression caught in review of #2663) -------------
# The first version of this hoist printed the repair line to stdout
# unconditionally, so `--json --fix` emitted prose ahead of the payload and
# json.loads(stdout) failed at line 1. Parsing the ENTIRE stdout is the point:
# asserting "no prose" by substring would still pass on a truncated payload.
import json as _json

fired, out, err = run_capturing([BROKEN, OK], ["--json", "--fix"])
check("[--json --fix] repair still runs", fired, True)

parsed, perr = None, None
try:
    parsed = _json.loads(out)
except Exception as e:                                  # noqa: BLE001
    perr = str(e)
check("[--json --fix] the ENTIRE stdout parses as JSON", perr, None)
# NB: assert on the prose LINE, not the substring "relinked" — the correct JSON
# payload contains that word as data (`"detail": "relinked"`), so a substring
# assertion would fail on right output.
_prose = "  skill-symlinks: relinked"
check("[--json --fix] the repair prose line is on stderr", _prose in err, True)
check("[--json --fix] ...and NOT on stdout",
      any(l.strip() == _prose.strip() for l in out.splitlines()), False)

ss = [c for c in (parsed or {}).get("checks", []) if c["name"] == "skill-symlinks"]
check("[--json --fix] payload reports the POST-fix status", (ss or [{}])[0].get("status"), "ok")
check("[--json --fix] payload carries the RE-MEASURED detail, not the fixer's self-report",
      (ss or [{}])[0].get("detail"), "re-measured: all linked")

# Control: the stderr routing must be --json-only. Without --json the operator
# still sees the repair on stdout, where it has always been.
_f, out2, err2 = run_capturing([BROKEN, OK], ["--fix"])
check("[--fix] prose still on stdout when NOT --json", "skill-symlinks: relinked" in out2, True)

# Control: nothing to repair -> --json stays parseable and untouched.
_f3, out3, _e3 = run_capturing([LINKED, OK], ["--json", "--fix"])
try:
    _json.loads(out3); ok3 = True
except Exception:                                       # noqa: BLE001
    ok3 = False
check("[--json --fix] healthy host still emits parseable JSON", ok3, True)

print(("FAILED: " + ", ".join(fails)) if fails else "symlink-fix-reachable: all checks passed")
sys.exit(1 if fails else 0)
