#!/usr/bin/env python3
"""A watcher with no PID sentinel is an orphan only when its parent is unknown or
init; a known live parent means supervised, so the remedy must not say to stop it.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "_helpers"))
from os_probes import PS_SKIP_REASON, ps_available  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules["health_check"] = hc
spec.loader.exec_module(hc)

failures = []
skipped = []


def check(label, cond, extra=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}  {extra}")


def skip(label, reason=PS_SKIP_REASON):
    """Loud, never silent: a skipped precondition must be visible in the log."""
    skipped.append(label)
    print(f"  SKIP {label}  — {reason}")


def _verdict(trees, parents):
    """check_task_watcher() with no sentinel, given tree roots and their ppids."""
    # "" is a scan that RAN and found nothing; None means ps is unavailable,
    # which would contradict this scenario's premise that watchers exist.
    with tempfile.TemporaryDirectory() as ws:
        with patch.object(hc, "WORKSPACE_DIR", Path(ws)), \
             patch.object(hc, "_fresh_local_core_record", return_value={"ts": 1}), \
             patch.object(hc, "_watcher_trees", return_value=trees), \
             patch.object(hc, "_ps_snapshot", return_value=""), \
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
# An unknown ppid cannot support "runs under a live session" — saying so would
# print a self-contradicting "(ppid None)".
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

print("the ps helpers, unpatched (their real bodies, which the fixtures above stub out):")

if ps_available():
    _snap = hc._ps_snapshot()
    check("_ps_snapshot returns a ps table", isinstance(_snap, str) and "PID" in _snap.upper())
    check("_ps_snapshot lists this process", _snap is not None and str(os.getpid()) in _snap)
else:
    skip("_ps_snapshot live-OS probes")
with patch.object(hc.subprocess, "run", side_effect=OSError("boom")):
    check("_ps_snapshot returns None when ps cannot run", hc._ps_snapshot() is None)

if ps_available():
    _real_ppid = str(os.getppid())
    check("_pid_parent reads a real ppid via ps", hc._pid_parent(os.getpid()) == _real_ppid,
          f"got {hc._pid_parent(os.getpid())!r} want {_real_ppid!r}")
    check("_pid_parent returns None for a pid ps does not know", hc._pid_parent(999999) is None)
else:
    # Without ps this asserts None-is-None and would pass for the wrong reason.
    skip("_pid_parent live-OS probes")
with patch.object(hc.subprocess, "run", side_effect=OSError("boom")):
    check("_pid_parent returns None when ps cannot run", hc._pid_parent(os.getpid()) is None)

_table = "  PID  PPID ARGS\n  100    99 /bin/foo --x\n  200     1 /bin/bar\n"
check("_pid_parent parses a supplied table", hc._pid_parent("100", _table) == "99")
check("_pid_parent finds an init parent in a table", hc._pid_parent("200", _table) == "1")
check("_pid_parent returns None when the pid is absent from the table",
      hc._pid_parent("777", _table) is None)
check("_pid_parent tolerates a malformed row",
      hc._pid_parent("100", "garbage\n  100    99 ok\n") == "99")

if failures:
    print(f"\nFAILED ({len(failures)}): {failures}")
    sys.exit(1)
_note = f" ({len(skipped)} live-OS probe(s) skipped: {skipped})" if skipped else ""
print(f"\nPASS — supervised watcher is not reported as an orphan{_note}")
