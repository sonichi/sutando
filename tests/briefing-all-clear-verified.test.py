#!/usr/bin/env python3
""""Everything looks clean" must mean every query ran, not that every query was falsy.

`get_reminders()` and `get_health_issues()` both returned `[]` on
TimeoutExpired/OSError — the same value they return when the query ran and found
nothing. `synthesize()` closed on `not reminders and not health_issues`, so a
timed-out reminders fetch plus a timed-out health check produced a confident
"Everything looks clean. Good day for deep work." over two questions nobody had
answered. Spoken to the owner every morning.

`get_calendar_events()` already drew this line (None vs []) after the 2026-07-21
falsely-clear bug, #2256; the other two gathers never did.

Test 1 is the discriminator: the SAME synthesize() inputs except failure-vs-empty
must produce different closings. Tests 4-5 are the regression for a crash this
fix could have introduced — main() calls len() on both values unguarded.

Run: python3 tests/briefing-all-clear-verified.test.py
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("mb", REPO / "src" / "morning-briefing.py")
_mod = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_mod)
except SystemExit:
    pass

_passed = 0
_failed = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def _with_failing_subprocess(fn, exc):
    real = _mod.subprocess.run
    try:
        _mod.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(exc)
        return fn()
    finally:
        _mod.subprocess.run = real


CLEAN = "Everything looks clean"

# --- the gathers must report unavailability, not emptiness -------------------
rem = _with_failing_subprocess(_mod.get_reminders, subprocess.TimeoutExpired("osascript", 5))
ok("get_reminders() returns None on timeout, not []", rem is None, f"got {rem!r}")

hl = _with_failing_subprocess(_mod.get_health_issues, subprocess.TimeoutExpired("health", 5))
ok("get_health_issues() returns None on timeout, not []", hl is None, f"got {hl!r}")

rem_os = _with_failing_subprocess(_mod.get_reminders, OSError("no such binary"))
ok("get_reminders() returns None on OSError too", rem_os is None, f"got {rem_os!r}")

# --- the closing distinguishes verified-empty from unanswered ---------------
verified = _mod.synthesize(None, [], [], [], [], [])
ok("a genuinely clean day STILL gets the all-clear",
   CLEAN in verified, f"got {verified!r}")

degraded = _mod.synthesize(None, [], None, [], [], None)
ok("failed reminders + failed health check get NO all-clear",
   CLEAN not in degraded, f"got {degraded!r}")

ok("the two are distinguishable (the whole point)",
   (CLEAN in verified) != (CLEAN in degraded))

# each alone is sufficient to withhold it
ok("failed reminders alone withholds the all-clear",
   CLEAN not in _mod.synthesize(None, [], None, [], [], []))
ok("failed health check alone withholds the all-clear",
   CLEAN not in _mod.synthesize(None, [], [], [], [], None))

# and the pre-existing calendar guard is untouched
ok("unreadable calendar still withholds the all-clear (#2256, unchanged)",
   CLEAN not in _mod.synthesize(None, None, [], [], [], []))

# --- None must not crash the reporting lines --------------------------------
# main() calls len() on both values. Returning None without fixing those would
# raise TypeError and take the whole briefing down — worse than the bug.
try:
    _ = f"  reminders: {'unavailable' if rem is None else f'{len(rem)} due'}"
    _ = f"  health issues: {'unavailable' if hl is None else len(hl)}"
    ok("main()'s progress lines survive None (no len(None))", True)
except TypeError as e:
    ok("main()'s progress lines survive None (no len(None))", False, str(e))

# Substring-matching `len(reminders)` was too crude: the CORRECT guarded form
# contains it too (`'unavailable' if reminders is None else f'{len(reminders)}'`).
# Assert the guard is present on the same line instead.
src = (REPO / "src" / "morning-briefing.py").read_text()
_len_lines = [ln for ln in src.splitlines()
              if "len(reminders)" in ln or "len(health_issues)" in ln]
ok("every len() on a possibly-None gather is guarded by an `is None` check",
   all("is None" in ln for ln in _len_lines),
   f"unguarded: {[ln.strip() for ln in _len_lines if 'is None' not in ln]}")

# --- content lines still render when data IS present ------------------------
withdata = _mod.synthesize(None, [], ["Call the dentist"], [], [], ["disk: 91% full"])
ok("reminders still render when present", "Call the dentist" in withdata)
ok("health issues still render when present", "disk: 91% full" in withdata)
ok("a day with real items gets no all-clear", CLEAN not in withdata)


# --- every "did not run" path returns None, not [] --------------------------
# There are three ways each gather can fail to answer, and all three must be
# distinguishable from a verified-empty result. Timeout and OSError are covered
# above; these are the other two.


class _Result:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


_real_run = _mod.subprocess.run
_real_path = _mod.Path
try:
    # non-zero exit: the script ran but failed -> unknown, not empty
    _mod.subprocess.run = lambda *a, **k: _Result(1, "")
    ok("get_reminders() returns None on non-zero exit", _mod.get_reminders() is None,
       f"got {_mod.get_reminders()!r}")

    # exit 0 with no rows IS a verified-empty answer, and must stay []
    _mod.subprocess.run = lambda *a, **k: _Result(0, "No reminders.\n")
    got = _mod.get_reminders()
    ok("get_reminders() returns [] when the script ran and found nothing",
       got == [], f"got {got!r}")
finally:
    _mod.subprocess.run = _real_run

# missing script / missing health-check binary -> cannot answer
class _NoSuchPath(type(_real_path())):
    def exists(self):  # noqa: D102
        return False


# `_SRC_DIR` is resolved ONCE at import, so stubbing `_mod.Path` here no longer
# reaches these path builds — point the directory itself at nothing instead.
# Stubbing Path alone left both cases running the REAL scripts and passing for
# an unrelated reason (a Reminders.app timeout also yields None).
_real_src_dir = _mod._SRC_DIR
try:
    _mod.Path = lambda *a, **k: _NoSuchPath(_real_path(*a, **k))
    _mod._SRC_DIR = _real_path("/nonexistent-sutando-src")
    ok("get_reminders() returns None when reminders.py is absent",
       _mod.get_reminders() is None)
    ok("get_health_issues() returns None when health-check.py is absent",
       _mod.get_health_issues() is None)
finally:
    _mod.Path = _real_path
    _mod._SRC_DIR = _real_src_dir

# a briefing built entirely from unavailable gathers says nothing false
allgone = _mod.synthesize(None, None, None, [], [], None)
ok("all-unavailable briefing makes no all-clear claim", CLEAN not in allgone,
   f"got {allgone!r}")
ok("all-unavailable briefing still reports the calendar honestly",
   "couldn't read your calendar" in allgone, f"got {allgone!r}")


# --- a CRASHED health check is not a clean system (review round 2) ---------
# health-check.py ends in `sys.exit(1 if issues else 0)`, so a non-zero exit is
# its normal way of saying "I found problems" — a blanket `returncode != 0 ->
# None` would throw away real findings. The crash case is non-zero WITH nothing
# parseable: import error, traceback on stderr, empty stdout. Both reviewers
# reproduced the false all-clear on that path independently.


def _health_with(rc, out, err=""):
    real = _mod.subprocess.run
    try:
        _mod.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(
            args=[], returncode=rc, stdout=out, stderr=err)
        return _mod.get_health_issues()
    finally:
        _mod.subprocess.run = real


_crash = _health_with(1, "", "Traceback (most recent call last): boom")
ok("crashed health check (rc!=0, nothing parseable) returns None",
   _crash is None, f"got {_crash!r}")
ok("crashed health check withholds the all-clear",
   CLEAN not in _mod.synthesize(None, [], [], [], [], _crash))

# the discriminator: rc!=0 is AMBIGUOUS, so real findings must survive it
_found = _health_with(1, "  \u2717 disk-space    fail    91% full on /\n")
ok("rc!=0 WITH parseable issues still returns them (not None)",
   _found == ["disk-space: 91% full on /"], f"got {_found!r}")
_n = _mod.synthesize(None, [], [], [], [], _found)
ok("real health issues still reach the briefing", "91% full" in _n, f"got {_n!r}")
ok("a day with real health issues gets no all-clear", CLEAN not in _n)

# and a clean run is still verified-empty, not unknown
_ok0 = _health_with(0, "  \u2713 everything fine\n")
ok("rc==0 with no failures is verified-empty []", _ok0 == [], f"got {_ok0!r}")
ok("verified-clean health STILL yields the all-clear",
   CLEAN in _mod.synthesize(None, [], [], [], [], _ok0))


print(f"briefing-all-clear-verified: {_passed}/{_passed + _failed} passed"
      + (f" — {_failed} FAILED" if _failed else ""))
raise SystemExit(1 if _failed else 0)
