#!/usr/bin/env python3
"""The hermetic-bridge lint must not report `ok` when its own discovery failed.

`scripts/lint-hermetic-bridge-tests.py` is a required CI gate. Both of its
discovery paths read `subprocess.run(...).stdout` with no returncode check, so a
git failure returned `[]` — and an empty target list is indistinguishable from
"scanned everything, found nothing". Measured before the fix, with git forced to
exit 128:

    tracked_tests()   310 files  ->  0 files
    main()            exit 0
    printed           "lint-hermetic-bridge-tests: ok (0 bridge-importing tests scanned…)"

A gate that reports clean because it could not look is worse than no gate: it
occupies the slot where a real check would go and answers with the same word.
That is the exact shape this lint exists to catch, one level up from the tests it
scans.

Two different guards, because the two paths have different truths:

  * `tracked_tests()`  — zero is NEVER legitimate, so it gets a floor as well as
    a returncode check. `MIN_TRACKED_TESTS` is a tripwire far below any plausible
    shrink, not a census.
  * `changed_tests()`  — zero IS legitimate (a PR may touch no tests), so it gets
    the returncode check ONLY. Giving it a floor would break every PR that
    doesn't touch tests, which is how a guard becomes a thing people disable.

Run:  python3 tests/lint-hermetic-discovery-floor.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "lint_hermetic", REPO / "scripts" / "lint-hermetic-bridge-tests.py")
lh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lh)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)
        if detail:
            print(f"        {detail[:300]}")


class _FailedGit:
    returncode = 128
    stdout = ""
    stderr = "fatal: not a git repository (or any of the parent directories): .git"


def with_broken_git(fn):
    """Run fn with every `git` subprocess failing; everything else untouched."""
    real = subprocess.run

    def fake(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and cmd[:1] and cmd[0] == "git":
            return _FailedGit()
        return real(cmd, *a, **k)

    subprocess.run = fake
    try:
        return fn()
    finally:
        subprocess.run = real


print("lint-hermetic discovery guards")

# --- positive control ---------------------------------------------------------
# Everything below asserts a FAILURE mode. Without this, a module that raised
# unconditionally would pass every one of them.
# `getattr` with a default, NOT a bare attribute read: against the pre-fix module
# the constant does not exist, and an AttributeError here would abort the file —
# a control that fails for the wrong reason is not a control. Every assertion
# below must fail BEHAVIOURALLY on the old source, not by crashing on import.
FLOOR = getattr(lh, "MIN_TRACKED_TESTS", 0)
check("the module declares a discovery floor", FLOOR > 0,
      "MIN_TRACKED_TESTS absent — nothing bounds a collapsed discovery")
real_tracked = lh.tracked_tests()
check("positive control: discovery works normally and finds a real suite",
      len(real_tracked) >= FLOOR, f"found {len(real_tracked)}, floor {FLOOR}")
check("positive control: the floor is below the real count, not above it",
      0 < FLOOR < len(real_tracked), f"floor {FLOOR} vs actual {len(real_tracked)}")

# --- 1. THE REGRESSION: a failed git must not read as a clean scan ------------
def _tracked():
    try:
        lh.tracked_tests()
        return None
    except SystemExit as e:
        return str(e)

msg = with_broken_git(_tracked)
check("tracked_tests() RAISES when git fails — never returns []",
      msg is not None,
      "it returned normally; a broken discovery would report `ok`")
if msg:
    check("...and the message says discovery failed, not that the tree is clean",
          "FAILED to discover" in msg, msg)
    check("...and it surfaces git's stderr rather than swallowing it",
          "not a git repository" in msg, msg)

# --- 2. changed_tests(): returncode checked, but NO floor --------------------
def _changed():
    try:
        lh.changed_tests("origin/main")
        return None
    except SystemExit as e:
        return str(e)

msg2 = with_broken_git(_changed)
check("changed_tests() also RAISES when git fails", msg2 is not None, str(msg2))

# A PR touching no test files is legitimate and must stay a normal, quiet pass.
# This is the assertion that stops the fix from becoming a nuisance guard.
real = subprocess.run
def fake_empty(cmd, *a, **k):
    if isinstance(cmd, (list, tuple)) and cmd[:1] and cmd[0] == "git":
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()
    return real(cmd, *a, **k)
subprocess.run = fake_empty
try:
    empty_ok = lh.changed_tests("origin/main") == []
finally:
    subprocess.run = real
check("changed_tests() returns [] quietly when git SUCCEEDS with no changes",
      empty_ok,
      "zero changed tests is a real answer and must not raise")

# --- 3. The floor fires on a collapsed-but-successful discovery --------------
# The subtler case: git exits 0 but returns almost nothing (a broken pathspec, a
# shallow checkout). The returncode check cannot see this; the floor can.
def fake_thin(cmd, *a, **k):
    if isinstance(cmd, (list, tuple)) and cmd[:1] and cmd[0] == "git":
        class R:
            returncode = 0
            stdout = "tests/one.py\n"
            stderr = ""
        return R()
    return real(cmd, *a, **k)
subprocess.run = fake_thin
try:
    thin = None
    try:
        lh.tracked_tests()
    except SystemExit as e:
        thin = str(e)
finally:
    subprocess.run = real
check("a git that SUCCEEDS but returns 1 file trips the floor",
      thin is not None and "floor is" in thin,
      f"got {thin!r} — returncode alone cannot catch this")
if thin:
    check("...and the message tells you how to lower it deliberately",
          "MIN_TRACKED_TESTS" in thin, thin)

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — a discovery that did not run cannot report `ok`")
