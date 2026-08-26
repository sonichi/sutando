#!/usr/bin/env python3
"""core-status.sh no-ops for pool FOLLOWERS and writes for everything else.

A follower running the stock loop writes core-status.json every pass, but that
file is the MAIN core's owner-facing record: the Discord bridge renders `step`
live and graceful-restart gates busy() on "running + fresh ts" (#3156). The rule
was prose in a SKILL.md an LLM session must remember across compactions.

The predicate is the VALUE, not the key's presence. The pool plist assigns 1..N
(scripts/install-core-pool.sh); a main core carries something else ('legacy' on
this host) or nothing. Gating on "is set" would silence a main core, and a core
that never writes reads as idle — which authorises a kill. So unrecognised
values WRITE: silence is the dangerous direction, and it must be unreachable by
accident.

This drives the SHIPPED script, not a copy: the write target is resolved
globally (`SUTANDO_WORKSPACE` is retired per v0.8/#1440 and cannot redirect it),
so the suite snapshots that file and restores it in a finally.

Run: python3 tests/core-status-follower-guard.test.py
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "core-status.sh"

# (value, should_write, why). None = absent from the environment.
CASES = [
    (None,     True,  "unset — a main core outside the pool"),
    ("legacy", True,  "the value this repo's own main core carries"),
    ("",       True,  "empty — indistinguishable from unset, must not silence"),
    ("1",      False, "install-core-pool.sh assigns 1..N"),
    ("3",      False, "install-core-pool.sh assigns 1..N"),
    ("12",     False, "multi-digit ids stay followers"),
    ("core-2", True,  "session NAME, not the id — unrecognised, fail open"),
    ("weird",  True,  "unrecognised, fail open"),
    ("2x",     True,  "not purely numeric — fail open"),
]

failures = []


def check(cond, label):
    print(f"{'ok' if cond else 'FAIL'}: {label}")
    if not cond:
        failures.append(label)


def status_file() -> Path:
    spec = importlib.util.spec_from_file_location(
        "wd", str(REPO / "src" / "workspace_default.py"))
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    return Path(wd.status_path("core-status.json"))


def run(value):
    env = dict(os.environ)
    env.pop("SUTANDO_CORE_ID", None)
    if value is not None:
        env["SUTANDO_CORE_ID"] = value
    r = subprocess.run(["bash", str(SCRIPT), "running", "guard-probe"],
                       capture_output=True, text=True, env=env, cwd=str(REPO))
    return r.returncode, r.stdout.strip()


target = status_file()
before = target.read_bytes() if target.exists() else None
try:
    for value, should_write, why in CASES:
        rc, out = run(value)
        shown = "<unset>" if value is None else (repr(value) if value == "" else value)
        check(rc == 0,
              f"rc=0 for SUTANDO_CORE_ID={shown} (non-zero breaks `cmd && ...` callers)")
        check(bool(out) == should_write,
              f"SUTANDO_CORE_ID={shown} {'writes' if should_write else 'no-ops'} — {why}")

    # Control: the suite must be able to FAIL. Keyed on PRESENCE, 'legacy' and
    # '2' would both no-op; assert they are actually discriminated.
    _, main_out = run("legacy")
    _, follower_out = run("2")
    check(bool(main_out) and not bool(follower_out),
          "control: 'legacy' and '2' are DISCRIMINATED, not both-set-so-both-skip")
finally:
    # The probes above wrote a bogus `step` to the live record. Put it back.
    if before is not None:
        target.write_bytes(before)
    elif target.exists():
        target.unlink()

check(
    (target.read_bytes() if target.exists() else None) == before,
    "the live status file is byte-identical to before this suite ran")

print(f"\n{'OK' if not failures else 'FAILED'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
