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

rem_to = _with_run(_mod.check_overdue_reminders, _raise(subprocess.TimeoutExpired("osa", 10)))
ok("reminders probe: timeout reports COULD NOT CHECK",
   len(rem_to) == 1 and rem_to[0].startswith(U), f"got {rem_to!r}")

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
