#!/usr/bin/env python3
"""A stale lane must not shadow a later lane that holds today.

`check_daily_cron_punctuality` consults its lanes in preference order and fell
through only on `if not arts:` — EMPTINESS. A job that once published dated
artifacts and stopped keeps a non-empty, permanently stale lane 2, so lane 3
(the task-cron record, which needs no per-job config) was never consulted and
the job warned forever while finishing on time every day. It worsens with age
instead of self-correcting, because the stale lane never becomes empty.

Measured on a live host: lane2 newest 2026-08-29, lane3 newest 2026-09-01, warn.

The control is the load-bearing half: with lane 3 also empty the probe must
still warn, or the fix is a deletion of the detector wearing a green result.
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
    d = (datetime.datetime.now() - datetime.timedelta(days=days_ago))
    return d.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()


def build(lane3_today):
    ws = Path(tempfile.mkdtemp(prefix="lane-shadow-"))
    (ws / "hosts" / "H").mkdir(parents=True)
    (ws / "results").mkdir()
    (ws / "state").mkdir()
    (ws / "hosts" / "H" / "crons.json").write_text(json.dumps([
        {"name": "ghost-job", "cron": "12 7 * * *", "launchd": True,
         "artifact": "ghost-job"}]))
    # lane 2: on-time dated artifacts that STOPPED two days ago.
    for k in range(2, 7):
        stamp = (datetime.datetime.now() - datetime.timedelta(days=k)).strftime("%Y%m%d")
        f = ws / "results" / f"ghost-job-{stamp}.txt"
        f.write_text("x")
        os.utime(f, (_at(k, 7, 14), _at(k, 7, 14)))
    if lane3_today:
        f = ws / "results" / f"task-cron-ghost-job-{int(time.time() * 1000)}.txt"
        f.write_text("done")
        os.utime(f, (_at(0, 7, 14), _at(0, 7, 14)))
    return ws


def verdict(ws):
    orig_ws, orig_host = hc.WORKSPACE_DIR, hc._host_label
    hc.WORKSPACE_DIR, hc._host_label = ws, (lambda: "H")
    try:
        return hc.check_daily_cron_punctuality()
    finally:
        hc.WORKSPACE_DIR, hc._host_label = orig_ws, orig_host


r_fresh = verdict(build(lane3_today=True))
check("a fresh lane 3 is consulted even though lane 2 is stale-but-non-empty",
      r_fresh["status"] == "ok", f"got {r_fresh['status']}: {r_fresh['detail'][:90]}")

r_missing = verdict(build(lane3_today=False))
check("CONTROL: with no lane holding today the probe still warns",
      r_missing["status"] == "warn",
      f"got {r_missing['status']} — the fix silenced the detector")

print()
if failures:
    print(f"{len(failures)} failure(s): {', '.join(failures)}")
    sys.exit(1)
print("All stale-lane-shadowing checks passed.")
