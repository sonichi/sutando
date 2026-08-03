#!/usr/bin/env python3
""""No friction detected today. Everything is clean." must mean every probe RAN.

`check_github_issues()` and `check_overdue_reminders()` swallowed
TimeoutExpired / FileNotFoundError into `pass` and returned `[]` — the same
value they return when the probe ran and found nothing. `main()` then reports
"No friction detected today. Everything is clean." on `not all_issues`, and the
bridges DM that file to the owner (`FALLBACK_PREFIXES` includes `friction-`;
today's file was consumed and archived, so the delivery path is live).

Same class as the morning-briefing all-clear (#2528): a claim about state,
asserted over a question nobody answered. Fixed the same way — the distinction
between "checked, nothing found" and "could not check" is made visible, here by
reporting the failure AS a friction item so the all-clear is withheld.

Run: python3 tests/friction-unchecked-probes.test.py
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("fd", REPO / "src" / "friction-detector.py")
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


if not hasattr(_mod, "UNCHECKED"):
    print("  FAIL: friction-detector has no UNCHECKED marker — a failed probe is "
          "still indistinguishable from a clean one")
    print("friction-unchecked-probes: 0/1 passed — 1 FAILED")
    raise SystemExit(1)


class _Result:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def _with_run(fn, impl):
    real = _mod.subprocess.run
    try:
        _mod.subprocess.run = impl
        return fn()
    finally:
        _mod.subprocess.run = real


def _raise(exc):
    return lambda *a, **k: (_ for _ in ()).throw(exc)


U = _mod.UNCHECKED

# --- a probe that could not run reports itself -------------------------------
gh_to = _with_run(_mod.check_github_issues, _raise(subprocess.TimeoutExpired("gh", 10)))
ok("github probe: timeout reports COULD NOT CHECK, not []",
   len(gh_to) == 1 and gh_to[0].startswith(U), f"got {gh_to!r}")

gh_missing = _with_run(_mod.check_github_issues, _raise(FileNotFoundError("gh")))
ok("github probe: missing binary reports COULD NOT CHECK",
   len(gh_missing) == 1 and gh_missing[0].startswith(U), f"got {gh_missing!r}")

gh_rc = _with_run(_mod.check_github_issues, lambda *a, **k: _Result(1, ""))
ok("github probe: non-zero exit reports COULD NOT CHECK",
   len(gh_rc) == 1 and gh_rc[0].startswith(U), f"got {gh_rc!r}")

# The reminders probe returns early when reminders.py is absent, which is the
# case on a clean-install CI runner (macos-tools is not installed there). That
# early return used to skip the exception handler entirely, so asserting on the
# timeout alone passed locally and FAILED in CI — the test was measuring the
# developer's machine. Pin the script as present, then exercise each path.
_real_home = _mod.claude_home_path


class _Present(type(Path())):
    def exists(self):  # noqa: D102
        return True


def _with_script_present(fn, impl):
    real_run = _mod.subprocess.run
    try:
        _mod.claude_home_path = lambda *a: _Present(Path("/nonexistent/reminders.py"))
        _mod.subprocess.run = impl
        return fn()
    finally:
        _mod.subprocess.run = real_run
        _mod.claude_home_path = _real_home


rem_to = _with_script_present(_mod.check_overdue_reminders,
                              _raise(subprocess.TimeoutExpired("osa", 10)))
ok("reminders probe: timeout reports COULD NOT CHECK",
   len(rem_to) == 1 and rem_to[0].startswith(U), f"got {rem_to!r}")

rem_rc = _with_script_present(_mod.check_overdue_reminders, lambda *a, **k: _Result(1, ""))
ok("reminders probe: non-zero exit reports COULD NOT CHECK",
   len(rem_rc) == 1 and rem_rc[0].startswith(U), f"got {rem_rc!r}")

rem_absent = _mod.check_overdue_reminders.__wrapped__() if hasattr(
    _mod.check_overdue_reminders, "__wrapped__") else None
_real_run2 = _mod.subprocess.run
try:
    _mod.claude_home_path = lambda *a: Path("/nonexistent/definitely-not-here.py")
    rem_absent = _mod.check_overdue_reminders()
finally:
    _mod.claude_home_path = _real_home
    _mod.subprocess.run = _real_run2
ok("reminders probe: missing reminders.py reports COULD NOT CHECK (the CI case)",
   len(rem_absent) == 1 and rem_absent[0].startswith(U), f"got {rem_absent!r}")

# NB: the fixture must not contain "overdue"/"past due" — the detector matches
# those substrings, so "no overdue items" is parsed AS an overdue reminder. My
# first fixture said exactly that and the control failed for the right reason.
rem_clean = _with_script_present(_mod.check_overdue_reminders,
                                 lambda *a, **k: _Result(0, "Reminders: 0 due\n"))
ok("CONTROL: reminders probe that RAN and found nothing returns []",
   rem_clean == [], f"got {rem_clean!r}")

rem_hit = _with_script_present(_mod.check_overdue_reminders,
                               lambda *a, **k: _Result(0, "Overdue: call the dentist\n"))
ok("CONTROL: a genuinely overdue reminder is still reported unmarked",
   len(rem_hit) == 1 and not rem_hit[0].startswith(U), f"got {rem_hit!r}")

# --- the CONTROL: a probe that ran and found nothing stays empty -------------
# Without this the fix could pass by marking everything unchecked, which would
# make the all-clear unreachable and the report useless.
gh_clean = _with_run(_mod.check_github_issues, lambda *a, **k: _Result(0, json.dumps([])))
ok("CONTROL: github probe that RAN and found nothing returns []",
   gh_clean == [], f"got {gh_clean!r}")

# and a real finding is still a real finding
_recent = "2020-01-01T00:00:00Z"
gh_stale = _with_run(
    _mod.check_github_issues,
    lambda *a, **k: _Result(0, json.dumps(
        [{"number": 7, "title": "old thing", "updatedAt": _recent}])),
)
ok("CONTROL: a genuinely stale issue is still reported",
   len(gh_stale) == 1 and not gh_stale[0].startswith(U), f"got {gh_stale!r}")

# --- the owner-visible consequence ------------------------------------------
# The all-clear is `if not all_issues`, so an UNCHECKED item withholds it purely
# by being non-empty. Assert that relationship rather than assuming it.
ok("an unchecked probe makes all_issues non-empty (withholds the all-clear)",
   bool(gh_to + rem_to), "both empty — the all-clear would still fire")
ok("a fully-clean run leaves all_issues empty (all-clear still reachable)",
   not (gh_clean + []), "the all-clear became unreachable")


print(f"friction-unchecked-probes: {_passed}/{_passed + _failed} passed"
      + (f" — {_failed} FAILED" if _failed else ""))
raise SystemExit(1 if _failed else 0)
