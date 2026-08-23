#!/usr/bin/env python3
"""The launchd lane is observable through its completion sentinel (#2754).

`check_daily_cron_punctuality` scored only jobs that publish a dated file into
`results/`. The two jobs that record completion — `state/<job>-<date>.sentinel`,
written after publish — are launchd-owned and were skipped by the collector, so
on a host where every session-owned daily job is artifact-less the probe reports
"0 of N observable" and nothing asserts that the morning briefing ran at all.

The load-bearing property: a sentinel history that stops TODAY must surface as a
named miss, and a history that is consistently late must surface as late. Both
were unreachable before, because the lane never entered the scored population.
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


CRONS = [
    {"name": "morning-briefing", "cron": "57 6 * * *", "launchd": True},
    {"name": "daily-insight", "cron": "50 6 * * *", "launchd": True},
    # launchd but never stamps: must stay UNCHECKED, never a standing warning.
    {"name": "agent-landscape-digest", "cron": "30 7 * * *", "launchd": True,
     "artifact": "landscape"},
    {"name": "example-digest", "cron": "0 8 * * *"},
    {"name": "codex-owned", "cron": "40 8 * * *", "execution": "codex-task"},
]


def build(finish=(6, 59), days=5, include_today=True):
    ws = Path(tempfile.mkdtemp(prefix="punct-"))
    (ws / "hosts" / "H").mkdir(parents=True)
    (ws / "state").mkdir()
    (ws / "results").mkdir()
    (ws / "hosts" / "H" / "crons.json").write_text(json.dumps(CRONS))
    today = datetime.date.today()
    for i in range(days):
        if i == 0 and not include_today:
            continue
        d = today - datetime.timedelta(days=i)
        (ws / "state" / f"morning-briefing-{d}.sentinel").write_text(
            f"{d}T{finish[0]:02d}:{finish[1]:02d}:00.000000")
        # daily-insight stores its payload, not a timestamp: mtime is the only
        # finish signal, which is why the reader falls back to it.
        p = ws / "state" / f"daily-insight-{d}.sentinel"
        p.write_text("payload text, no timestamp")
        os.utime(p, (time.time(),
                     datetime.datetime.combine(d, datetime.time(6, 53)).timestamp()))
    return ws


def run(ws):
    hc.WORKSPACE_DIR = ws
    hc._host_label = lambda: "H"
    return hc.check_daily_cron_punctuality()


# ── the lane enters the scored population at all ─────────────────────────────
r = run(build())
check("on-time sentinels are observable and on schedule",
      "2 of 4 daily job(s) observable" in r["detail"] and "all on schedule" in r["detail"],
      r["detail"])
check("a launchd job that never stamps stays UNCHECKED, not a warning",
      "agent-landscape-digest" in r["detail"] and "median" not in r["detail"],
      r["detail"])
check("codex-task entries stay out of the population",
      "codex-owned" not in r["detail"], r["detail"])

# ── broken-must-FAIL: a history that stops today ─────────────────────────────
r = run(build(include_today=False))
check("a sentinel history that stops today reports a named miss",
      "morning-briefing: no output today" in r["detail"], r["detail"])
check("the mtime-only job is missed too",
      "daily-insight: no output today" in r["detail"], r["detail"])

# ── broken-must-FAIL: sustained lateness ─────────────────────────────────────
r = run(build(finish=(8, 29)))
check("sustained lateness is scored from the sentinel body",
      "morning-briefing" in r["detail"] and "median +92 min late" in r["detail"],
      r["detail"])

# ── the body is preferred over mtime, and mtime is the documented fallback ───
ws = build()
d = datetime.date.today()
f = ws / "state" / f"morning-briefing-{d}.sentinel"
os.utime(f, (time.time(), datetime.datetime.combine(d, datetime.time(23, 30)).timestamp()))
mins = hc._daily_completion_minutes(ws / "state", "morning-briefing")
check("ISO body wins over a touched mtime",
      mins and mins[-1][1] == 6 * 60 + 59, f"got {mins[-1] if mins else None}")
f.write_text("not a timestamp")          # write first: it resets mtime
os.utime(f, (time.time(), datetime.datetime.combine(d, datetime.time(23, 30)).timestamp()))
mins = hc._daily_completion_minutes(ws / "state", "morning-briefing")
check("unparseable body falls back to mtime",
      mins and mins[-1][1] == 23 * 60 + 30, f"got {mins[-1] if mins else None}")

# `-extra-` never matches the glob; a trailing suffix does and is refused by
# the anchor — only the second actually reaches the regex branch.
(ws / "state" / f"morning-briefing-extra-{d}.sentinel").write_text(f"{d}T05:00:00")
mins = hc._daily_completion_minutes(ws / "state", "morning-briefing")
check("a differently-prefixed job is excluded",
      all(m[1] != 5 * 60 for m in mins), f"got {mins}")

(ws / "state" / f"morning-briefing-{d}-old.sentinel").write_text(f"{d}T04:00:00")
mins = hc._daily_completion_minutes(ws / "state", "morning-briefing")
check("a trailing suffix is refused by the anchored date pattern",
      all(m[1] != 4 * 60 for m in mins), f"got {mins}")

# ── absent state/ dir is empty, not an exception ─────────────────────────────
check("missing state dir returns empty",
      hc._daily_completion_minutes(Path(tempfile.mkdtemp()) / "nope", "x") == [])

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
