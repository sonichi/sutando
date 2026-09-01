#!/usr/bin/env python3
"""Every source-backed restart in main()'s --fix loop consults the canonical guard.

`stale_restart_allowed()` says the guard "belongs to every auto-restart path",
but it was called from ONE branch. The siblings — the `com.sutando.*` and
LAUNCHD_BACKED_CHECKS launchd kickstarts, the stuck-CONNECTING voice-agent
relaunch, and the conversation-server kill+respawn — all boot whatever is
checked out HERE and reached that code with no guard at all.

Testing the policy in isolation cannot catch this: a correct predicate wired to
nothing passes every predicate test. So this drives main() and asserts on the
SIDE EFFECTS each branch would produce.

Run: python3 tests/health-check-every-restart-path-guarded.test.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules["health_check"] = hc
spec.loader.exec_module(hc)

failures = []


def check(label, cond, extra=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}  {extra}")


def _chk(name, status="stale", detail="stale code"):
    return {"name": name, "status": status, "detail": detail}


def drive(checks, *, allowed):
    """Run main() --fix over `checks`; return (launchd_calls, spawns, stdout).

    Only the guard varies between the two calls a case makes — every other stub
    is identical, so a difference in side effects can only come from the guard.
    """
    launchd, spawns, out = [], [], []

    def fake_run(argv, *a, **k):
        # pgrep in the conversation-server branch must yield no pids, so a
        # difference in `spawns` is never explained by kill bookkeeping.
        return mock.MagicMock(stdout="", returncode=0)

    with mock.patch.object(hc, "run_all_checks", return_value=checks), \
         mock.patch.object(hc, "stale_restart_allowed",
                           return_value=(allowed, "test-guard: noncanonical")), \
         mock.patch.object(hc, "fix_launchd",
                           side_effect=lambda job: launchd.append(job) or "kickstarted"), \
         mock.patch.object(hc, "fix_down_bridges", return_value=[]), \
         mock.patch.object(hc.subprocess, "Popen",
                           side_effect=lambda argv, **k: spawns.append(argv) or mock.MagicMock()), \
         mock.patch.object(hc.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(sys, "argv", ["health-check.py", "--fix", "--quiet"]), \
         mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(str(x) for x in a))):
        try:
            hc.main()
        except SystemExit:
            pass
    return launchd, spawns, "\n".join(out)


CASES = [
    ("com.sutando.* launchd job", [_chk("com.sutando.voice-agent")]),
    ("LAUNCHD_BACKED_CHECKS service", [_chk(next(iter(hc.LAUNCHD_BACKED_CHECKS)))]),
    ("conversation-server kill+respawn", [_chk("conversation-server")]),
]

for label, checks in CASES:
    print(f"\n{label}:")
    l_ref, s_ref, out_ref = drive(checks, allowed=False)
    check("noncanonical: nothing kickstarted", l_ref == [], f"launchd={l_ref}")
    check("noncanonical: nothing spawned", s_ref == [], f"spawns={s_ref}")
    check("noncanonical: says it refused, and why",
          "refused" in out_ref and "noncanonical" in out_ref, out_ref[-200:])
    # POSITIVE CONTROL — without it, a branch that never runs at all would pass
    # every assertion above by construction.
    l_ok, s_ok, _ = drive(checks, allowed=True)
    check("canonical control: the branch DOES restart",
          bool(l_ok or s_ok), f"launchd={l_ok} spawns={s_ok}")

print("\nstuck-CONNECTING voice-agent:")
# status MUST be "fail", matching the producer at :4141 — is_issue() treats warn as
# benign, so a warn fixture never enters issues[] and the branch is never reached.
vt = [{"name": "voice-transport", "status": "fail", "detail": "stuck CONNECTING",
       "_stuck_connecting": True}]
l_ref, s_ref, out_ref = drive(vt, allowed=False)
l_ok, s_ok, _ = drive(vt, allowed=True)
check("noncanonical: voice-agent not kickstarted", l_ref == [], f"launchd={l_ref}")
check("noncanonical: says it refused, and why",
      "refused" in out_ref and "noncanonical" in out_ref, out_ref[-200:])
check("canonical control: it IS kickstarted", l_ok == ["com.sutando.voice-agent"], f"launchd={l_ok}")

print("\nthe guard is LAZY and memoized:")
src = (REPO / "src" / "health-check.py").read_text()
check("one call site, reached through the memo", src.count("_gate[\"v\"] = stale_restart_allowed(") == 1)
calls = []
with mock.patch.object(hc, "run_all_checks", return_value=[_chk("some-unrelated-check", "warn", "x")]), \
     mock.patch.object(hc, "stale_restart_allowed",
                       side_effect=lambda r: calls.append(r) or (True, "ok")), \
     mock.patch.object(hc, "fix_down_bridges", return_value=[]), \
     mock.patch.object(sys, "argv", ["health-check.py", "--fix", "--quiet"]), \
     mock.patch("builtins.print", side_effect=lambda *a, **k: None):
    try:
        hc.main()
    except SystemExit:
        pass
check("NOT evaluated when no source-backed branch is reached", calls == [], f"calls={calls}")

if failures:
    print(f"\nFAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("\nPASS — every source-backed restart path consults the canonical guard")
