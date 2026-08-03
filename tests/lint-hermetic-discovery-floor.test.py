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
import os
import re
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

# --- 4. An UNRESOLVABLE base ref must WIDEN, not crash and not go quiet ------
# CI caught this on the first version of the fix. `actions/checkout@v4` fetches
# depth 1, so `origin/main` is often absent and `origin/main...HEAD` exits 128
# with "bad revision". Three distinguishable behaviours, and only the third is
# right:
#
#   before this PR   returned [] -> "no test files changed" -> 0 scanned, exit 0
#   first fix        SystemExit  -> broke the repo's own --diff test in CI
#   now              warns, falls back to the full tree -> 63 scanned, exit 0
#
# Crashing here is the "a guard becomes a thing people disable" outcome this
# change's own rationale warns about, committed one layer up.
class _BadRef:
    returncode = 128
    stdout = ""
    stderr = "fatal: bad revision 'origin/main...HEAD'"

real2 = subprocess.run


def fake_bad_ref(cmd, *a, **k):
    if isinstance(cmd, (list, tuple)) and cmd[:2] == ["git", "diff"]:
        return _BadRef()
    return real2(cmd, *a, **k)


subprocess.run = fake_bad_ref
try:
    widened = None
    try:
        widened = lh.changed_tests("origin/main")
    except SystemExit as e:
        widened = f"RAISED: {e}"
finally:
    subprocess.run = real2

check("an unresolvable base ref does NOT raise",
      not isinstance(widened, str),
      str(widened)[:200])
check("...and returns the widen sentinel (None), not an empty list",
      widened is None,
      f"got {widened!r} — [] would silently scan nothing, which is the original bug")

# --- 5. `no merge base` is the SAME failure, in git's other wording ------------
# Section 4 above tests one spelling. Git has (at least) two for "this comparison
# cannot be made here", and the first cut of the fallback recognised only the
# first — so the shallow-checkout case it was written for still hard-failed.
# Reproduced against real git, not asserted from the manual:
#
#     $ git init; git commit -m a; git checkout --orphan other; git commit -m b
#     $ git diff --name-only --diff-filter=AM other...HEAD -- '*.txt'
#     fatal: other...HEAD: no merge base
#     rc=128
#
# `actions/checkout@v4` fetches depth 1, so a base ref that EXISTS can still share
# no history with HEAD in the grafted shallow graph — which is this wording, not
# "bad revision". Same class, same required response: widen, never crash.
for stderr_wording, label in (
    ("fatal: origin/main...HEAD: no merge base", "no merge base"),
    ("fatal: bad revision 'origin/main...HEAD'", "bad revision"),
    ("fatal: ambiguous argument 'origin/main...HEAD': unknown revision", "unknown revision"),
):
    class _Wording:
        returncode = 128
        stdout = ""
        stderr = stderr_wording

    real3 = subprocess.run

    def fake_wording(cmd, *a, _w=_Wording, **k):
        if isinstance(cmd, (list, tuple)) and cmd[:2] == ["git", "diff"]:
            return _w()
        return real3(cmd, *a, **k)

    subprocess.run = fake_wording
    try:
        got = None
        try:
            got = lh.changed_tests("origin/main")
        except SystemExit as e:
            got = f"RAISED: {e}"
    finally:
        subprocess.run = real3
    check(f"git's {label!r} wording widens (sentinel None), never raises",
          got is None, f"got {str(got)[:180]!r}")

# --- 6. END-TO-END through main(): the widen branch actually SCANS ------------
# Everything above stops at `changed_tests()`. The branch that consumes the
# sentinel — `if targets is None: targets = tracked_tests()` in main() — was
# uncovered, which is both a coverage-gate failure and a real gap: a sentinel
# nobody acts on is the original "0 scanned, exit 0" bug wearing a warning.
#
# This case uses REAL git in the REAL repo with a base ref that genuinely does
# not exist, so nothing here is mocked: git fails for real, main() widens for
# real, and the scanned count proves it read the whole tree rather than nothing.
import contextlib  # noqa: E402
import io  # noqa: E402

saved_argv, saved_base = sys.argv[:], os.environ.get("BASE_REF")
buf = io.StringIO()
try:
    sys.argv = ["lint-hermetic-bridge-tests.py", "--diff"]
    os.environ["BASE_REF"] = "refs/heads/no-such-base-ref-for-this-test"
    with contextlib.redirect_stdout(buf):
        rc = lh.main()
except SystemExit as e:
    rc, = (f"RAISED: {e}",)
finally:
    sys.argv = saved_argv
    if saved_base is None:
        os.environ.pop("BASE_REF", None)
    else:
        os.environ["BASE_REF"] = saved_base
out = buf.getvalue()

check("main('--diff') with an unresolvable BASE_REF exits 0, not 128", rc == 0, repr(rc))
check("...and SAYS it fell back, rather than falling back silently",
      "Falling back to the full tracked-test scan" in out, out[:300])
m = re.search(r"ok \((\d+) bridge-importing tests scanned", out)
check("...and reports a scan, not 'no test files changed'",
      m is not None and "no test files changed" not in out, out[-400:])
if m:
    # The number is the whole point: the pre-fix behaviour and a broken widen both
    # print a verdict, and only the count tells them apart.
    check("...and the count proves it scanned the FULL tree, not an empty diff",
          int(m.group(1)) >= 10, f"scanned {m.group(1)} — too few to be the full tree")

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — a discovery that did not run cannot report `ok`")
