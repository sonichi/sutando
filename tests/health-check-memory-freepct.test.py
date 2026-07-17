#!/usr/bin/env python3
"""
Tests for check_memory()'s free%-gating rewrite (PR #1949, issue #1485
follow-up): `memory_pressure`'s free% becomes the deciding vote over the
kernel pressure level / swap-in-use heuristic, because level can read
"warning" for a single transient tick and swap-in-use is sticky residue
from a past pressure event — both produced recurring false FAILs/WARNs
on hosts that were actually healthy.

Mocks all three subprocess calls (`sysctl kern.memorystatus_vm_pressure_level`,
`sysctl vm.swapusage`, `memory_pressure`) so this runs deterministically on
any machine, macOS or not, regardless of its actual live pressure state.

Covers (per reviewer request on #1949):
  a) high free% + sticky swap (level 1) => ok, with a "residue" detail
  b) low free% + level 2 => warn
  c) low free% + level 2 + swap >= fail threshold => fail
  d) level 4 (kernel-critical) => fail regardless of free%
  e) unparseable `memory_pressure` output => legacy level+swap fallback branch
     e1) fallback: level>=2 + swap>=fail => fail
     e2) fallback: level>=2, swap<fail => warn
     e3) fallback: level 1, swap>=warn => warn ("likely residue")
     e4) fallback: level 1, swap<warn => ok
  f) `memory_pressure` tool missing (FileNotFoundError) => same fallback path as (e)
  g) high free%, level 1, swap 0 => ok, plain "pressure normal" detail
  h) env var thresholds (SUTANDO_MEMORY_FREE_FAIL_PCT / _WARN_PCT) are honored

Run: python3 tests/health-check-memory-freepct.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class FakeCompleted:
    def __init__(self, stdout: str):
        self.stdout = stdout


def fake_run_factory(level: Optional[int], swap_out: str, mp_out):
    """Build a stand-in for subprocess.run that dispatches on argv[0].

    `mp_out` may be a string (memory_pressure stdout) or an exception
    instance/class to simulate the tool being missing/timing out.
    """
    def fake_run(cmd, **kwargs):
        if cmd[0] == "sysctl":
            if cmd[-1] == "kern.memorystatus_vm_pressure_level":
                if level is None:
                    raise FileNotFoundError("sysctl not found")
                return FakeCompleted(f"{level}\n")
            if cmd[-1] == "vm.swapusage":
                return FakeCompleted(swap_out)
            raise AssertionError(f"unexpected sysctl call: {cmd}")
        if cmd[0] == "memory_pressure":
            if isinstance(mp_out, Exception):
                raise mp_out
            if isinstance(mp_out, type) and issubclass(mp_out, Exception):
                raise mp_out("memory_pressure unavailable")
            return FakeCompleted(mp_out)
        raise AssertionError(f"unexpected subprocess call: {cmd}")
    return fake_run


def run_check_memory(level, swap_out, mp_out, env=None):
    orig_run = hc.subprocess.run
    orig_environ = dict(os.environ)
    try:
        hc.subprocess.run = fake_run_factory(level, swap_out, mp_out)
        if env:
            os.environ.update(env)
        return hc.check_memory()
    finally:
        hc.subprocess.run = orig_run
        os.environ.clear()
        os.environ.update(orig_environ)


SWAP_NONE = "total = 2048.00M  used = 0.00M  free = 2048.00M  (encrypted)"
SWAP_STICKY = "total = 30720.00M  used = 24487.00M  free = 6233.00M  (encrypted)"
SWAP_HUGE = "total = 30720.00M  used = 5000.00M  free = 25720.00M  (encrypted)"


def mp(free_pct: int) -> str:
    return (
        f"Pages free: 123456.\n"
        f"System-wide memory free percentage: {free_pct}%\n"
    )


def case_a_high_free_sticky_swap_ok() -> list[str]:
    fails = []
    r = run_check_memory(level=1, swap_out=SWAP_STICKY, mp_out=mp(47))
    if r["status"] != "ok":
        fails.append(f"a) high free% + sticky swap should be ok, got {r['status']} ({r['detail']})")
    if "residue" not in r["detail"]:
        fails.append(f"a) detail should call out swap as residue, got: {r['detail']}")
    return fails


def case_b_low_free_level2_warn() -> list[str]:
    fails = []
    r = run_check_memory(level=2, swap_out=SWAP_NONE, mp_out=mp(20))
    if r["status"] != "warn":
        fails.append(f"b) 20% free + level 2 should be warn, got {r['status']} ({r['detail']})")
    return fails


def case_c_low_free_level2_big_swap_fail() -> list[str]:
    fails = []
    r = run_check_memory(level=2, swap_out=SWAP_STICKY, mp_out=mp(10))
    if r["status"] != "fail":
        fails.append(f"c) 10% free + level 2 + swap>=fail_mb should be fail, got {r['status']} ({r['detail']})")
    return fails


def case_d_level4_always_fail() -> list[str]:
    fails = []
    # Even with abundant free% and no swap, level 4 (kernel-critical) fails outright.
    r = run_check_memory(level=4, swap_out=SWAP_NONE, mp_out=mp(90))
    if r["status"] != "fail":
        fails.append(f"d) level 4 should fail regardless of free%, got {r['status']} ({r['detail']})")
    return fails


def case_e1_fallback_fail() -> list[str]:
    fails = []
    r = run_check_memory(level=2, swap_out=SWAP_STICKY, mp_out="unparseable garbage, no percentage here")
    if r["status"] != "fail":
        fails.append(f"e1) unparseable memory_pressure, level 2 + big swap should fall back to fail, got {r['status']} ({r['detail']})")
    return fails


def case_e2_fallback_warn() -> list[str]:
    fails = []
    r = run_check_memory(level=2, swap_out=SWAP_NONE, mp_out="unparseable garbage, no percentage here")
    if r["status"] != "warn":
        fails.append(f"e2) unparseable memory_pressure, level 2 + small swap should fall back to warn, got {r['status']} ({r['detail']})")
    return fails


def case_e3_fallback_warn_residue() -> list[str]:
    fails = []
    r = run_check_memory(level=1, swap_out=SWAP_STICKY, mp_out="")
    if r["status"] != "warn":
        fails.append(f"e3) unparseable memory_pressure, level 1 + swap>=warn_mb should fall back to warn, got {r['status']} ({r['detail']})")
    if "residue" not in r["detail"]:
        fails.append(f"e3) fallback detail should mention residue, got: {r['detail']}")
    return fails


def case_e4_fallback_ok() -> list[str]:
    fails = []
    r = run_check_memory(level=1, swap_out=SWAP_NONE, mp_out="")
    if r["status"] != "ok":
        fails.append(f"e4) unparseable memory_pressure, level 1 + no swap should fall back to ok, got {r['status']} ({r['detail']})")
    return fails


def case_f_tool_missing_uses_fallback() -> list[str]:
    fails = []
    # memory_pressure binary missing entirely (FileNotFoundError) — same
    # fallback path as an unparseable/empty output, not a crash.
    r = run_check_memory(level=2, swap_out=SWAP_STICKY, mp_out=FileNotFoundError("no such tool"))
    if r["status"] != "fail":
        fails.append(f"f) memory_pressure missing, level 2 + big swap should fall back to fail, got {r['status']} ({r['detail']})")
    return fails


def case_g_healthy_plain_detail() -> list[str]:
    fails = []
    r = run_check_memory(level=1, swap_out=SWAP_NONE, mp_out=mp(80))
    if r["status"] != "ok":
        fails.append(f"g) 80% free, level 1, no swap should be ok, got {r['status']} ({r['detail']})")
    if "pressure normal" not in r["detail"]:
        fails.append(f"g) plain-healthy detail should read 'pressure normal', got: {r['detail']}")
    return fails


def case_h_env_thresholds_honored() -> list[str]:
    fails = []
    # Tight custom fail threshold (5%) — 10% free would normally fail at the
    # default (15%) but should pass through to warn-or-better once the env
    # var raises the bar for what counts as "critical".
    r = run_check_memory(
        level=2, swap_out=SWAP_HUGE, mp_out=mp(10),
        env={"SUTANDO_MEMORY_FREE_FAIL_PCT": "5", "SUTANDO_MEMORY_FREE_WARN_PCT": "12"},
    )
    if r["status"] == "fail":
        fails.append(f"h) 10% free should not fail once SUTANDO_MEMORY_FREE_FAIL_PCT=5, got {r['status']} ({r['detail']})")
    if r["status"] != "warn":
        fails.append(f"h) 10% free with warn_pct=12 should be warn, got {r['status']} ({r['detail']})")
    return fails


def main() -> int:
    cases = [
        ("a", case_a_high_free_sticky_swap_ok),
        ("b", case_b_low_free_level2_warn),
        ("c", case_c_low_free_level2_big_swap_fail),
        ("d", case_d_level4_always_fail),
        ("e1", case_e1_fallback_fail),
        ("e2", case_e2_fallback_warn),
        ("e3", case_e3_fallback_warn_residue),
        ("e4", case_e4_fallback_ok),
        ("f", case_f_tool_missing_uses_fallback),
        ("g", case_g_healthy_plain_detail),
        ("h", case_h_env_thresholds_honored),
    ]
    all_failures = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            all_failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    print("\nAll check_memory() free%-gating invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
