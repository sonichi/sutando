#!/usr/bin/env python3
"""The briefing's health line must not understate the failure set.

get_health_issues() returned issues[:3] and synthesize() then rendered
health_issues[:2], so a morning with nine failures produced
`System note: A; B.` — two of nine, presented as the system state, with the
count destroyed at the first cap and never recoverable at the second. The
`  health issues: N` console line printed the CAPPED length for the same
reason. The health-check notifiers in this repo already solve this
(`{len(failures)} health check failure(s): ... (+N more)`); the briefing was
the surface that did not.

Run: python3 tests/morning-briefing-health-truncation.test.py
"""
import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("mb", REPO / "src" / "morning-briefing.py")
mb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mb)

FAILS = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def render(issues):
    return mb.synthesize(None, None, None, None, None, issues)


# --- the prose must carry the true count and the omission ---
nine = [f"probe-{i}: down (detail {i})" for i in range(9)]
out = render(nine)
check("9 health failures" in out, "names the true count, not the rendered count")
check("(+7 more)" in out, "names how many it did not show")
check("probe-0" in out and "probe-1" in out, "still shows the leading failures")

# --- one failure: no plural, no "+0 more" noise ---
one = render(["disk: full (0 GiB)"])
check("1 health failure —" in one, "singular for a single failure")
check("more)" not in one, "no remainder clause when nothing was omitted")

# --- two failures: complete, so no remainder clause ---
check("more)" not in render(nine[:2]), "no remainder clause when the list fits")

# --- the gather must stop capping, or the count above is a lie ---
FAKE = "\n".join(f"  ✗ probe-{i}   fail   detail {i}" for i in range(9))


class _R:
    returncode, stdout, stderr = 1, FAKE, ""


with patch.object(subprocess, "run", return_value=_R()):
    got = mb.get_health_issues()
check(got is not None and len(got) == 9,
      f"get_health_issues returns every failure, not a capped 3 (got {got and len(got)})")

# --- contract the rest of the module depends on is unchanged ---
with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("health", 5)):
    check(mb.get_health_issues() is None, "a check that did not run is still None, not []")

print(f"\n{len(FAILS)} failure(s)")
sys.exit(1 if FAILS else 0)
