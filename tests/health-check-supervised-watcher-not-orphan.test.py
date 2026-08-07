#!/usr/bin/env python3
"""check_task_watcher(): a SUPERVISED watcher with no sentinel is not an orphan to kill.

Observed 2026-08-07 on Chis-Mac-mini: one watcher tree (pid 12631), ancestor chain
12631 -> 12626 -> 8832 (this core) -> 8829 -> 1, i.e. running under the live session
that spawned it via the Monitor tool. `state/watch-tasks-stream.pid` was absent, so
the probe reported:

    1 orphaned watcher(s) running with no PID sentinel (pids 12631) —
    draining tasks/ unsupervised; stop them and restart one cleanly

Following that advice kills the only working watcher and opens exactly the gap this
check exists to detect. The duplicate-processing rationale in the docstring applies
to MULTIPLE trees; with one supervised tree there is nothing to duplicate.

`#2733` fixed the same inference ("a missing sentinel is UNKNOWN, not DEAD") in
`proactive-loop/SKILL.md` and `schedule-crons/SKILL.md`. This is the third copy of
that policy, in code, still giving the destructive remedy.

A KNOWN parent that is not init (1) is the discriminator, rather than an ancestry test
against the core's `.alive` pid — that file records the HEARTBEAT process on some
versions, so an ancestry test against it can be wrong in a way this one cannot.
Unknown parentage stays an orphan: it cannot support a "supervised" claim.

Run: python3 tests/health-check-supervised-watcher-not-orphan.test.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

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


def _verdict(trees, parents):
    """check_task_watcher() with no sentinel, given tree roots and their ppids."""
    with tempfile.TemporaryDirectory() as ws:
        with patch.object(hc, "WORKSPACE_DIR", Path(ws)), \
             patch.object(hc, "_any_core_alive", return_value=True), \
             patch.object(hc, "_watcher_trees", return_value=trees), \
             patch.object(hc, "_ps_snapshot", return_value=None), \
             patch.object(hc, "_pid_parent", side_effect=lambda pid, ps=None: parents.get(str(pid))):
            return hc.check_task_watcher()


print("single SUPERVISED watcher (the live case):")
v = _verdict({"12631": {"12631"}}, {"12631": "12626"})
check("still warns (the sentinel gap is real)", v["status"] == "warn", str(v))
check("does NOT say 'orphaned'", "orphaned" not in v["detail"], v["detail"])
check("does NOT tell you to stop it", "stop them" not in v["detail"], v["detail"])
check("says do NOT stop it", "Do NOT stop it" in v["detail"], v["detail"])
check("names the live parent", "ppid 12626" in v["detail"], v["detail"])

print("single REPARENTED watcher (a true orphan):")
v2 = _verdict({"555": {"555"}}, {"555": "1"})
check("keeps the orphan verdict", "orphaned" in v2["detail"], v2["detail"])
check("keeps the stop remedy", "stop them" in v2["detail"], v2["detail"])

print("single watcher with UNKNOWN parent (must stay an orphan):")
# ps could not report a ppid. Unknown parentage cannot support "runs under a live
# session" — the first version of this fix printed exactly that with "(ppid None)".
# tests/health-check-task-watcher.test.py case (m) is the fixture that caught it.
vU = _verdict({"9000": {"9000"}}, {})
check("keeps the orphan verdict", "orphaned" in vU["detail"], vU["detail"])
check("does not claim a live session", "live session" not in vU["detail"], vU["detail"])

print("TWO supervised watchers (duplicates are still a real problem):")
v3 = _verdict({"100": {"100"}, "200": {"200"}}, {"100": "99", "200": "98"})
check("keeps the orphan/stop verdict for 2 trees", "stop them" in v3["detail"], v3["detail"])
check("counts both", "2 orphaned" in v3["detail"], v3["detail"])

print("no watchers at all (unchanged):")
v4 = _verdict({}, {})
check("reports not running", "watcher not running" in v4["detail"], v4["detail"])

if failures:
    print(f"\nFAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("\nPASS — supervised watcher is not reported as an orphan")
