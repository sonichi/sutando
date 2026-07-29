#!/usr/bin/env python3
"""Tests for src/result_audit.py — the Result Router §7 audit ledger sink.

`record()` appends one `<iso_ts>\\t<task_id>\\t<disposition>\\t<surface>` line to
`<workspace>/state/result-audit.log`, and must NEVER raise (observability can't
block delivery). Runs against a hermetic temp workspace (SUTANDO_TEST_MODE=1).

Run: python3 tests/result-audit.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Hermetic workspace before importing the module (it resolves the path lazily,
# but set it up front so _audit_path() lands in the temp dir).
_WS = tempfile.mkdtemp()
os.environ["SUTANDO_WORKSPACE"] = _WS
os.environ["SUTANDO_TEST_MODE"] = "1"

spec = importlib.util.spec_from_file_location("result_audit", REPO / "src" / "result_audit.py")
ra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ra)

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


AUDIT = Path(_WS) / "state" / "result-audit.log"

# 1. A basic record writes exactly one tab-separated §7 line.
ra.record("task-abc-1", "delivered", "discord", ts="2026-07-07T06:00:00Z")
lines = AUDIT.read_text().splitlines()
check("writes one line", len(lines) == 1, str(lines))
check("line is the §7 tab format",
      lines[0] == "2026-07-07T06:00:00Z\ttask-abc-1\tdelivered\tdiscord", repr(lines[0]))

# 2. Appends (does not truncate).
ra.record("task-def-2", "redirected", "slack", ts="2026-07-07T06:01:00Z")
ra.record("task-ghi-3", "failed", "telegram", ts="2026-07-07T06:02:00Z")
lines = AUDIT.read_text().splitlines()
check("appends across calls", len(lines) == 3)
check("fields cut cleanly on tab",
      [l.split("\t")[2] for l in lines] == ["delivered", "redirected", "failed"],
      str([l.split("\t")[2] for l in lines]))

# 3. ts defaults to an ISO-8601 UTC stamp when omitted.
ra.record("task-jkl-4", "no_send", "voice")
last = AUDIT.read_text().splitlines()[-1]
ts0 = last.split("\t")[0]
check("default ts is ISO-8601 UTC", ts0.endswith("Z") and "T" in ts0 and len(ts0) == 20, repr(ts0))

# 4. Never raises — empty task_id falls back, weird disposition/surface still logged.
try:
    ra.record("", "delivered", "discord", ts="2026-07-07T06:03:00Z")
    ra.record("task-x", "some-unknown-disposition", "", ts="2026-07-07T06:04:00Z")
    check("empty task_id falls back to 'unknown'",
          "\tunknown\t" in AUDIT.read_text())
    check("does not raise on odd inputs", True)
except Exception as e:
    check("does not raise on odd inputs", False, str(e))

# 5. Never raises even when the sink path can't be written (unresolvable ws).
#    Point resolve at a file-as-directory so mkdir/open fail; record must no-op.
_saved = ra._audit_path
try:
    bad = Path(_WS) / "not-a-dir-file"
    bad.write_text("x")  # a file where a dir would need to be
    ra._audit_path = lambda: bad / "state" / "result-audit.log"  # parent is a file → mkdir raises
    try:
        ra.record("task-boom", "delivered", "discord")
        check("never raises when path unwritable", True)
    except Exception as e:
        check("never raises when path unwritable", False, str(e))
finally:
    ra._audit_path = _saved

if failures:
    print(f"\nFAIL — {len(failures)} check(s) failed: {failures}")
    raise SystemExit(1)
print("\nPASS — result_audit sink tests")
