#!/usr/bin/env python3
"""A scheduled job whose script was deleted must not report as healthy.

Freshness fall-through (this PR's other half) lets the task-cron record lane be
consulted when the output lanes are merely stale. That lane dates COMPLETION,
and a job whose script is gone still completes: its slot fires, the handler
finds no script, and writes a no-op result. So the lane cannot tell "ran and
delivered by DM" from "cannot run at all", and the job goes green.

Two fixes were proposed and both are refuted by measurement, which is why the
discriminator here is the schedule's own command:

  * filter `[no-send]` result bodies — refuted: on this host
    `posthog-usage-daily` writes `[no-send]` on a run that DID deliver
    ("delivered as owner DM"), because the marker means "do not send THIS body",
    not "produced nothing".
  * restrict fall-through to same-class lanes — refuted: it is a straight revert
    of the bug this PR fixes; the sibling test
    `punctuality-stale-lane-does-not-shadow` fails under it.

Script existence is orthogonal to lane order, so it separates the two cases
without touching either behaviour.

The control is load-bearing: with the script PRESENT the same fixture must go
green, or this is a detector that fires on everything.
"""
import datetime
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
_spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(hc)
except SystemExit:
    pass

failures = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def _at(days_ago, hh, mm):
    d = datetime.datetime.now() - datetime.timedelta(days=days_ago)
    return d.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()


def build(script_ref):
    """A job with NO output lane at all, whose only fresh evidence is lane 3."""
    ws = Path(tempfile.mkdtemp(prefix="dead-job-"))
    (ws / "hosts" / "H").mkdir(parents=True)
    (ws / "results").mkdir()
    (ws / "state").mkdir()
    (ws / "hosts" / "H" / "crons.json").write_text(json.dumps([
        {"name": "ghost-job", "cron": "12 7 * * *", "launchd": True,
         "artifact": "ghost-job",
         "prompt": f"Run: python3 {script_ref} and DM the result"}]))
    # lane 3 only: the slot fired and something finished, today and on time.
    for k in range(0, 5):
        f = ws / "results" / f"task-cron-ghost-job-{int(time.time() * 1000) + k}.txt"
        f.write_text("[no-send]\nnothing to report\n")
        os.utime(f, (_at(k, 7, 14), _at(k, 7, 14)))
    return ws


def verdict(ws):
    orig_ws, orig_host = hc.WORKSPACE_DIR, hc._host_label
    hc.WORKSPACE_DIR, hc._host_label = ws, (lambda: "H")
    try:
        return hc.check_daily_cron_punctuality()
    finally:
        hc.WORKSPACE_DIR, hc._host_label = orig_ws, orig_host


MISSING = "src/deleted-by-a-past-pr-and-never-unscheduled.py"
assert not (REPO / MISSING).exists(), "fixture path must not exist"
PRESENT = "src/health-check.py"
assert (REPO / PRESENT).exists(), "control path must exist"

r_dead = verdict(build(MISSING))
check("a job whose script is gone is not reported healthy",
      r_dead["status"] == "warn", f"got {r_dead['status']}: {r_dead['detail'][:120]}")
check("...and the detail names the missing script, not lateness",
      MISSING in r_dead["detail"], f"detail: {r_dead['detail'][:160]}")

r_live = verdict(build(PRESENT))
check("CONTROL: identical fixture with the script PRESENT stays green",
      r_live["status"] == "ok",
      f"got {r_live['status']} — the check fires regardless of the script: {r_live['detail'][:120]}")

# ---- an INVOKED path only: prose is not a dependency (qingyun-wu, #3672) ----
# The detector matched `scripts/x.sh` inside a longer ABSOLUTE path and judged the
# repo-relative remainder, so a working /tmp script read as missing.
_t = tempfile.mkdtemp()
os.makedirs(os.path.join(_t, "scripts"), exist_ok=True)
_live = os.path.join(_t, "scripts", "live.sh")
open(_live, "w").write("#!/bin/sh\necho job completed\n")
_gone = os.path.join(_t, "scripts", "gone.sh")

check("invoked: an absolute script that EXISTS is not missing",
      hc._cron_missing_script({"prompt": f"bash {_live}"}) is None)
check("invoked: prose naming a retired file is not an invocation",
      hc._cron_missing_script({"prompt": "do not use scripts/retired-thing.py any more"}) is None)
check("CONTROL: a genuinely missing invoked repo script still warns",
      hc._cron_missing_script({"prompt": "python3 scripts/does-not-exist.py"})
      == "scripts/does-not-exist.py")
check("CONTROL: a missing ABSOLUTE invoked script still warns",
      hc._cron_missing_script({"prompt": f"bash {_gone}"}) == _gone)

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("All dead-job checks passed.")
