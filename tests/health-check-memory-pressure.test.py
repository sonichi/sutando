#!/usr/bin/env python3
"""
Tests for health-check.py's check_memory() — the macOS memory pressure gate.

Motivated by the history of false FAILs (issue #1485) where the old top-based
unused-pages check fired on a healthy machine because macOS keeps unused pages
near zero deliberately. The current check uses kern.memorystatus_vm_pressure_level
(the kernel's own OOM-proximity signal) plus swap-in-use.

Covers check_memory():
  a) sysctl unavailable (non-macOS / VM) → ok, "pressure level unavailable"
  b) level=1, no swap → ok, "pressure normal"
  c) level=2, no swap, memory_pressure unavailable → warn (fallback path)
  d) level=2, swap above fail threshold, memory_pressure unavailable → fail (fallback)
  e) level=4 → fail regardless of swap (kernel-declared critical)
  f) level=1, swap above warn threshold, memory_pressure unavailable → warn (swap residue)
  g) swap sysctl fails (OSError) → ok (swap treated as 0, level=1)
  h) custom thresholds via SUTANDO_MEMORY_SWAP_WARN_MB / _FAIL_MB env vars

  free%-path tests (when free_pct signals agree with level/swap conviction):
  i) level=4, free%=36 → fail (kernel-critical overrides free%)
  j) free%=5 (< 15 fail threshold), level=2, swap>fail → fail
  k) level=2, swap>warn, free%=20 → warn (free% in warn band)
  l) custom free% thresholds via SUTANDO_MEMORY_FREE_FAIL_PCT / _WARN_PCT

Note: test cases where healthy free% (e.g. 36%) should override a convicting
level+swap signal (the "residue" ok-path from PR #1949) belong in a follow-up
once that PR merges — those branches don't exist in the current implementation.

Run: python3 tests/health-check-memory-pressure.test.py
Exit code: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

FAILURES = []


def check(name, cond, detail=""):
    ok = cond
    print(f"{'  ok  ' if ok else '  FAIL '}{name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def _mock_run(level_str="1", swap_str="total = 0.00M  used = 0.00M  free = 0.00M",
              raise_level=None, raise_swap=None, free_pct=None):
    """Return a side_effect function for subprocess.run that:
    - Returns level_str for kern.memorystatus_vm_pressure_level
    - Returns swap_str for vm.swapusage
    - Returns fake memory_pressure output if free_pct is given, else raises FileNotFoundError
    - Optionally raises exceptions for level/swap sysctls
    """
    def _side_effect(cmd, **kwargs):
        if "kern.memorystatus_vm_pressure_level" in cmd:
            if raise_level is not None:
                raise raise_level
            r = unittest.mock.MagicMock()
            r.stdout = level_str
            return r
        if "vm.swapusage" in cmd:
            if raise_swap is not None:
                raise raise_swap
            r = unittest.mock.MagicMock()
            r.stdout = swap_str
            return r
        if "memory_pressure" in cmd:
            if free_pct is None:
                raise FileNotFoundError("memory_pressure unavailable")
            r = unittest.mock.MagicMock()
            r.stdout = f"System-wide memory free percentage: {free_pct}%\n"
            return r
        raise ValueError(f"unexpected cmd: {cmd}")

    return _side_effect


# ── a) sysctl unavailable → ok ──────────────────────────────────────────────
with unittest.mock.patch.object(hc.subprocess, "run", side_effect=FileNotFoundError):
    result = hc.check_memory()
check("a) sysctl unavailable → ok", result["status"] == "ok")
check("a) detail says unavailable", "unavailable" in result["detail"])


# ── b) level=1, no swap → ok ────────────────────────────────────────────────
with unittest.mock.patch.object(hc.subprocess, "run", side_effect=_mock_run("1", "total = 0.00M  used = 0.00M  free = 0.00M")):
    result = hc.check_memory()
check("b) level=1, no swap → ok", result["status"] == "ok")
check("b) detail says pressure normal", "pressure normal" in result["detail"] or "normal" in result["detail"])


# ── c) level=2, no swap, memory_pressure unavailable → warn (fallback) ──────
with unittest.mock.patch.object(hc.subprocess, "run", side_effect=_mock_run("2", "total = 0.00M  used = 0.00M  free = 0.00M")):
    result = hc.check_memory()
check("c) level=2, no swap, no free% → warn (fallback)", result["status"] == "warn")
check("c) detail mentions level 2", "level 2" in result["detail"] or "2" in result["detail"])


# ── d) level=2, swap above fail threshold, memory_pressure unavailable → fail
# Default fail threshold is 2048M; use 3000M swap to trigger
swap_fail = "total = 4096.00M  used = 3000.00M  free = 1096.00M"
with unittest.mock.patch.object(hc.subprocess, "run", side_effect=_mock_run("2", swap_fail)):
    result = hc.check_memory()
check("d) level=2, swap>fail, no free% → fail (fallback)", result["status"] == "fail")
check("d) detail says critical", "critical" in result["detail"].lower())


# ── e) level=4 → fail regardless of swap ────────────────────────────────────
with unittest.mock.patch.object(hc.subprocess, "run", side_effect=_mock_run("4", "total = 0.00M  used = 0.00M  free = 0.00M")):
    result = hc.check_memory()
check("e) level=4 → fail", result["status"] == "fail")


# ── f) level=1, swap above warn threshold, memory_pressure unavailable → warn
# Default warn threshold is 512M; use 600M swap
swap_warn = "total = 4096.00M  used = 600.00M  free = 3496.00M"
with unittest.mock.patch.object(hc.subprocess, "run", side_effect=_mock_run("1", swap_warn)):
    result = hc.check_memory()
check("f) level=1, swap>warn, no free% → warn (fallback)", result["status"] == "warn")
check("f) detail mentions swap", "swap" in result["detail"].lower())


# ── g) swap sysctl fails → ok (treated as swap=0, level=1) ─────────────────
with unittest.mock.patch.object(hc.subprocess, "run",
                                side_effect=_mock_run("1", raise_swap=OSError("no swap info"))):
    result = hc.check_memory()
check("g) swap OSError → ok (no crash)", result["status"] == "ok")


# ── h) custom thresholds via env vars ───────────────────────────────────────
saved_warn = os.environ.get("SUTANDO_MEMORY_SWAP_WARN_MB")
saved_fail = os.environ.get("SUTANDO_MEMORY_SWAP_FAIL_MB")
try:
    os.environ["SUTANDO_MEMORY_SWAP_WARN_MB"] = "100"
    os.environ["SUTANDO_MEMORY_SWAP_FAIL_MB"] = "200"
    # 150M swap, level=2, no free% → warn (150 < 200 fail, level=2)
    swap_custom = "total = 4096.00M  used = 150.00M  free = 3946.00M"
    with unittest.mock.patch.object(hc.subprocess, "run", side_effect=_mock_run("2", swap_custom)):
        result = hc.check_memory()
    check("h) level=2, swap=150M < custom fail 200M, no free% → warn not fail", result["status"] == "warn")

    # 250M swap, level=2, no free% → fail (250 >= 200)
    swap_above_fail = "total = 4096.00M  used = 250.00M  free = 3846.00M"
    with unittest.mock.patch.object(hc.subprocess, "run", side_effect=_mock_run("2", swap_above_fail)):
        result = hc.check_memory()
    check("h) level=2, swap=250M >= custom fail 200M, no free% → fail", result["status"] == "fail")
finally:
    if saved_warn is not None:
        os.environ["SUTANDO_MEMORY_SWAP_WARN_MB"] = saved_warn
    else:
        os.environ.pop("SUTANDO_MEMORY_SWAP_WARN_MB", None)
    if saved_fail is not None:
        os.environ["SUTANDO_MEMORY_SWAP_FAIL_MB"] = saved_fail
    else:
        os.environ.pop("SUTANDO_MEMORY_SWAP_FAIL_MB", None)


# ── i) level=4, free%=36 → fail (kernel-critical overrides free%) ───────────
# Both old and new code: level=4 always fails regardless of free%.
with unittest.mock.patch.object(hc.subprocess, "run",
                                side_effect=_mock_run("4", swap_fail, free_pct=36)):
    result = hc.check_memory()
check("i) level=4, free%=36 → fail (kernel-critical)", result["status"] == "fail")


# ── j) free%=5, level=2, swap>fail → fail ───────────────────────────────────
# Both old and new code: 5% free with level=2+swap>fail is critical.
with unittest.mock.patch.object(hc.subprocess, "run",
                                side_effect=_mock_run("2", swap_fail, free_pct=5)):
    result = hc.check_memory()
check("j) free%=5, level=2, swap>fail → fail", result["status"] == "fail")
check("j) detail says critical", "critical" in result["detail"].lower())


# ── k) level=2, swap>warn, free%=20 → warn ──────────────────────────────────
# Old code: level=2 → warn. New code: 20 < 25 warn threshold, swap>warn → warn.
with unittest.mock.patch.object(hc.subprocess, "run",
                                side_effect=_mock_run("2", swap_warn, free_pct=20)):
    result = hc.check_memory()
check("k) level=2, swap>warn, free%=20 → warn", result["status"] == "warn")


# ── l) custom free% thresholds via env vars ──────────────────────────────────
saved_free_fail = os.environ.get("SUTANDO_MEMORY_FREE_FAIL_PCT")
saved_free_warn = os.environ.get("SUTANDO_MEMORY_FREE_WARN_PCT")
try:
    os.environ["SUTANDO_MEMORY_FREE_FAIL_PCT"] = "30"
    os.environ["SUTANDO_MEMORY_FREE_WARN_PCT"] = "40"
    # free%=25, custom fail=30%, level=2, swap>fail → fail in new code; also fail in old (level+swap).
    with unittest.mock.patch.object(hc.subprocess, "run",
                                    side_effect=_mock_run("2", swap_fail, free_pct=25)):
        result = hc.check_memory()
    check("l) free%=25, custom fail=30%, level=2, swap>fail → fail", result["status"] == "fail")
finally:
    if saved_free_fail is not None:
        os.environ["SUTANDO_MEMORY_FREE_FAIL_PCT"] = saved_free_fail
    else:
        os.environ.pop("SUTANDO_MEMORY_FREE_FAIL_PCT", None)
    if saved_free_warn is not None:
        os.environ["SUTANDO_MEMORY_FREE_WARN_PCT"] = saved_free_warn
    else:
        os.environ.pop("SUTANDO_MEMORY_FREE_WARN_PCT", None)


# ── Summary ──────────────────────────────────────────────────────────────────
if FAILURES:
    print(f"\nFAIL — {len(FAILURES)} check(s) failed:")
    for f in FAILURES:
        print(f"  {f}")
    sys.exit(1)
else:
    print("\nPASS — check_memory coverage: all branches exercised")
