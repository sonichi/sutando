#!/usr/bin/env python3
"""
Regression tests for PR #1027's check_discord_voice() probe.

Guards:
  a) pgrep returns no pids → warn "not running (on-demand)"
  b) pgrep returns a pid   → ok "running"
  c) pgrep exits non-zero  → treated as no pids → warn
  d) pgrep raises OSError  → treated as no pids → warn
  e) run_all_checks includes a "discord-voice" entry
  f) the check never raises into run_all_checks on timeout

Run: python3 tests/health-check-discord-voice.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  FAIL: {msg}")


def ok(msg: str) -> None:
    print(f"  ok:   {msg}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_pgrep(pids: list[str], returncode: int = 0):
    result = MagicMock()
    result.stdout = "\n".join(pids) + ("\n" if pids else "")
    result.returncode = returncode
    return result


# ---------------------------------------------------------------------------
# Case a — no pids → warn
# ---------------------------------------------------------------------------

def case_a_no_pids_warn():
    with patch.object(subprocess, "run", return_value=_mock_pgrep([], returncode=1)):
        check = hc.check_discord_voice()
    if check["status"] != "warn":
        fail(f"a: expected warn, got {check['status']}")
        return
    if "not running" not in check["detail"]:
        fail(f"a: detail should contain 'not running', got {check['detail']!r}")
        return
    ok("no pids → warn 'not running (on-demand)'")


# ---------------------------------------------------------------------------
# Case b — pid returned → ok
# ---------------------------------------------------------------------------

def case_b_pid_ok():
    with patch.object(subprocess, "run", return_value=_mock_pgrep(["12345"], returncode=0)):
        # Also mock mark_stale_if_outdated since the ts file won't exist in tests
        with patch.object(hc, "mark_stale_if_outdated", return_value=None):
            check = hc.check_discord_voice()
    if check["status"] != "ok":
        fail(f"b: expected ok, got {check['status']}")
        return
    if check["detail"] != "running":
        fail(f"b: detail should be 'running', got {check['detail']!r}")
        return
    ok("pid present → ok 'running'")


# ---------------------------------------------------------------------------
# Case c — pgrep exits non-zero but no pids → warn (not error)
# ---------------------------------------------------------------------------

def case_c_pgrep_nonzero_warn():
    with patch.object(subprocess, "run", return_value=_mock_pgrep([], returncode=2)):
        check = hc.check_discord_voice()
    if check["status"] != "warn":
        fail(f"c: expected warn on pgrep non-zero, got {check['status']}")
        return
    ok("pgrep exit-2 (no match) → warn, not error")


# ---------------------------------------------------------------------------
# Case d — OSError during pgrep → warn, not crash
# ---------------------------------------------------------------------------

def case_d_oserror_warn():
    with patch.object(subprocess, "run", side_effect=OSError("no such file")):
        try:
            check = hc.check_discord_voice()
        except Exception as exc:
            fail(f"d: should not raise, got {exc!r}")
            return
    if check["status"] != "warn":
        fail(f"d: expected warn on OSError, got {check['status']}")
        return
    ok("OSError during pgrep → warn, no crash")


# ---------------------------------------------------------------------------
# Case e — run_all_checks includes discord-voice entry
# ---------------------------------------------------------------------------

def case_e_wired_into_run_all_checks():
    checks = hc.run_all_checks()
    names = [c["name"] for c in checks]
    if "discord-voice" not in names:
        fail(f"e: 'discord-voice' not in run_all_checks output; names={names}")
        return
    ok("discord-voice wired into run_all_checks()")


# ---------------------------------------------------------------------------
# Case f — TimeoutExpired → warn, not crash
# ---------------------------------------------------------------------------

def case_f_timeout_warn():
    with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=5)):
        try:
            check = hc.check_discord_voice()
        except Exception as exc:
            fail(f"f: should not raise on timeout, got {exc!r}")
            return
    if check["status"] != "warn":
        fail(f"f: expected warn on timeout, got {check['status']}")
        return
    ok("TimeoutExpired → warn, no crash")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

CASES = [
    case_a_no_pids_warn,
    case_b_pid_ok,
    case_c_pgrep_nonzero_warn,
    case_d_oserror_warn,
    case_e_wired_into_run_all_checks,
    case_f_timeout_warn,
]

if __name__ == "__main__":
    for case in CASES:
        print(f"\n{case.__name__}")
        case()
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s):")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print(f"\nAll {len(CASES)} tests passed.")
    sys.exit(0)
