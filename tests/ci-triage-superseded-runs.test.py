#!/usr/bin/env python3
"""Regression pin: a superseded run's FAILURE is not a live failure.

One head can carry several CI runs — a concurrency cancellation leaves the
loser's jobs in the rollup beside the winner's, and a job that starts while its
run is being cancelled exits non-zero and records FAILURE. Counting every
rollup entry then reports a red on a PR whose every required context is green.
Observed on sonichi/sutando#3777: `tsc + tests (clean install)` FAILURE at
09:04:42Z (cancelled run) and SUCCESS at 09:17:30Z (live run), same sha; the
PR merged CLEAN while `failing_checks` still named the job.

Run: python3 tests/ci-triage-superseded-runs.test.py
Exit code: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ci_triage", REPO / "scripts" / "ci-triage.py")
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

failures: "list[str]" = []
checked = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global checked
    checked += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        if detail:
            print(f"       {detail}")
        failures.append(label)


def run_for(rollup):
    """Stand in for `gh`, returning one canned rollup."""
    def _run(args, **kw):
        class R:
            returncode = 0
            stdout = json.dumps({"statusCheckRollup": rollup})
        return R()
    return _run


def cr(name, conclusion, at, url=""):
    return {"name": name, "conclusion": conclusion, "completedAt": at, "detailsUrl": url}


# --- the defect: superseded FAILURE, later SUCCESS, same name ----------------
SUPERSEDED = [
    cr("tsc + tests (clean install)", "FAILURE", "2026-09-03T09:04:42Z"),
    cr("tsc + tests (clean install)", "SUCCESS", "2026-09-03T09:17:30Z"),
    cr("eslint", "SUCCESS", "2026-09-03T09:05:00Z"),
]
got = ct.failing_checks("1", run_for(SUPERSEDED), "o/r")
check("a) a superseded FAILURE with a later SUCCESS is NOT reported", got == [], f"got {got}")

# --- the arm: the fix must not become a machine that only says "none" -------
STILL_FAILING = [
    cr("tsc + tests (clean install)", "SUCCESS", "2026-09-03T09:04:42Z"),
    cr("tsc + tests (clean install)", "FAILURE", "2026-09-03T09:17:30Z"),
]
got = ct.failing_checks("1", run_for(STILL_FAILING), "o/r")
check("b) SUCCESS superseded by a later FAILURE IS reported",
      got == ["tsc + tests (clean install)"], f"got {got}")

# --- a genuine single failure still reports ---------------------------------
got = ct.failing_checks("1", run_for([cr("ruff", "FAILURE", "2026-09-03T09:00:00Z")]), "o/r")
check("c) an undisputed FAILURE still reports", got == ["ruff"], f"got {got}")

# --- CANCELLED as the latest is still not a defect (pre-existing contract) ---
got = ct.failing_checks("1", run_for([
    cr("bundle-smoke", "FAILURE", "2026-09-03T09:00:00Z"),
    cr("bundle-smoke", "CANCELLED", "2026-09-03T09:10:00Z"),
]), "o/r")
check("d) CANCELLED as the latest is capacity, not a failure", got == [], f"got {got}")

# --- equal stamps: keep the failing entry rather than forgive on a coin flip -
got = ct.failing_checks("1", run_for([
    cr("shellcheck", "SUCCESS", "2026-09-03T09:00:00Z"),
    cr("shellcheck", "FAILURE", "2026-09-03T09:00:00Z"),
]), "o/r")
check("e) on an identical stamp the FAILURE wins (no silent forgiveness)",
      got == ["shellcheck"], f"got {got}")
got = ct.failing_checks("1", run_for([
    cr("shellcheck", "FAILURE", "2026-09-03T09:00:00Z"),
    cr("shellcheck", "SUCCESS", "2026-09-03T09:00:00Z"),
]), "o/r")
check("e2) ...regardless of rollup order", got == ["shellcheck"], f"got {got}")

# --- StatusContext entries carry `context`, not `name` -----------------------
got = ct.failing_checks("1", run_for([
    {"context": "license/cla", "state": "FAILURE", "completedAt": "2026-09-03T09:00:00Z"},
    {"context": "license/cla", "state": "SUCCESS", "completedAt": "2026-09-03T09:10:00Z"},
]), "o/r")
check("f) a StatusContext dedupes on `context` too", got == [], f"got {got}")

# --- _run_ids must not send the reader to a superseded run's log ------------
ids = ct._run_ids("1", run_for([
    cr("tsc", "FAILURE", "2026-09-03T09:04:42Z", "https://x/runs/111/job/1"),
    cr("tsc", "SUCCESS", "2026-09-03T09:17:30Z", "https://x/runs/222/job/2"),
]), "o/r")
check("g) _run_ids skips the superseded run's id", ids == [], f"got {ids}")

ids = ct._run_ids("1", run_for([
    cr("tsc", "SUCCESS", "2026-09-03T09:04:42Z", "https://x/runs/111/job/1"),
    cr("tsc", "FAILURE", "2026-09-03T09:17:30Z", "https://x/runs/222/job/2"),
]), "o/r")
check("h) _run_ids returns the LIVE failing run's id", ids == ["222"], f"got {ids}")

print(f"\nci-triage superseded runs: {checked - len(failures)} passed, {len(failures)} failed")
raise SystemExit(1 if failures else 0)
