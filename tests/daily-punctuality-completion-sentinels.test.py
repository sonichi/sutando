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


# The probe reads the wall clock, and `due` is combined with TODAY's date, so
# for the first hour after local midnight no miss can be reported at all.
class _FixedNow(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        d = datetime.date.today()
        return cls(d.year, d.month, d.day, 12, 0, 0)


def run(ws):
    hc.WORKSPACE_DIR = ws
    hc._host_label = lambda: "H"
    real = datetime.datetime
    datetime.datetime = _FixedNow
    try:
        return hc.check_daily_cron_punctuality()
    finally:
        datetime.datetime = real


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

# ── an aware body must localise, or its minute-of-day is UTC while cron times
# and the mtime fallback are local — a whole-offset error, not a rounding one ──
ws = build()
d = datetime.date.today()
aware = datetime.datetime(d.year, d.month, d.day, 6, 59, tzinfo=datetime.timezone.utc)
local = aware.astimezone()
for nm, body in (("z", f"{aware:%Y-%m-%dT%H:%M:%S}Z"),
                 ("us", f"{aware:%Y-%m-%dT%H:%M:%S}.433037Z"),
                 ("off", f"{aware:%Y-%m-%dT%H:%M:%S}+00:00")):
    (ws / "state" / f"{nm}-{d}.sentinel").write_text(body)
    got = hc._daily_completion_minutes(ws / "state", nm)
    check(f"{nm}: aware body localises to the same minute-of-day as cron/mtime",
          got and got[-1][1] == local.hour * 60 + local.minute,
          f"got {got[-1] if got else None}, want {local.hour * 60 + local.minute}")

# ── absent state/ dir is empty, not an exception ─────────────────────────────
check("missing state dir returns empty",
      hc._daily_completion_minutes(Path(tempfile.mkdtemp()) / "nope", "x") == [])

# ── a launchd job that publishes a dated ARTIFACT but stamps no sentinel ─────

# `launchd` says how a job is SCHEDULED, not what dated evidence it leaves; without
# the fallback such a job reports "no dated artifact" while writing one every day.
def _launchd_artifact_ws(days=5, include_today=True, name="digest-job", stem="digest-job"):
    ws = Path(tempfile.mkdtemp(prefix="punct-la-"))
    (ws / "hosts" / "H").mkdir(parents=True)
    (ws / "state").mkdir()
    (ws / "results").mkdir()
    (ws / "hosts" / "H" / "crons.json").write_text(json.dumps(
        [{"name": name, "cron": "0 6 * * *", "launchd": True, "artifact": stem}]))
    today = datetime.date.today()
    for i in range(days):
        if i == 0 and not include_today:
            continue
        d = today - datetime.timedelta(days=i)
        f = ws / "results" / f"{stem}-{d:%Y%m%d}.txt"
        f.write_text("x")
        os.utime(f, (time.time(),
                     datetime.datetime.combine(d, datetime.time(6, 4)).timestamp()))
    return ws


r = run(_launchd_artifact_ws())
check("launchd + dated artifact is OBSERVABLE, not UNCHECKED",
      "digest-job" not in (r.get("detail") or ""), f"got {r.get('detail')!r}")
check("...and a punctual artifact history is not reported late",
      r.get("status") == "ok", f"got {r.get('status')}: {r.get('detail')}")

r = run(_launchd_artifact_ws(include_today=False))
check("a launchd artifact history that stops TODAY surfaces as a named miss",
      "digest-job" in (r.get("detail") or "") and "no output today" in (r.get("detail") or ""),
      f"got {r.get('detail')!r}")

# The control that can fail: with NO evidence in either lane the job must stay
# UNCHECKED, so the fallback cannot manufacture observability out of nothing.
r = run(_launchd_artifact_ws(days=0))
check("no sentinel and no artifact stays UNCHECKED",
      "digest-job" in (r.get("detail") or "")
      and ("UNCHECKED" in (r.get("detail") or "") or "unverifiable" in (r.get("detail") or "")),
      f"got {r.get('detail')!r}")


# ── declared artifact, zero evidence in EITHER lane (yixuan-ag2 on #3440) ────

# `used_artifact_lane` flips here vs the old `not launchd`, but the interpret
# layer `continue`s on empty artifacts before the stem_declared gate is read.
ws = Path(tempfile.mkdtemp(prefix="punct-none-"))
(ws / "hosts" / "H").mkdir(parents=True)
(ws / "state").mkdir()
(ws / "results").mkdir()
(ws / "hosts" / "H" / "crons.json").write_text(json.dumps(
    [{"name": "never-ran", "cron": "0 6 * * *", "artifact": "never-ran"}]))
r = run(ws)
_d = r.get("detail") or ""
check("a job with no evidence in either lane is UNCHECKED, not a miss",
      "never-ran" in _d and "past due" not in _d and "no output today" not in _d,
      f"got {_d!r}")

# The same property at the interpret layer, holding everything but the flag
# fixed — it must not matter which value stem_declared carries.
_base = dict(name="j", hour=6, minute=0, artifacts=[], today_seen=False,
             minutes_since_due=999, conditional=False)
_outs = [hc._interpret_daily_punctuality([dict(_base, stem_declared=sd)]).get("detail") or ""
         for sd in (True, False)]
check("stem_declared cannot change the verdict when artifacts is empty",
      _outs[0] == _outs[1], f"got {_outs}")

# ── the task-cron lane: the only completion record needing no per-job config ──

# Every cron job leaves results/task-cron-<name>-<epoch>.txt when its result is
# written, whatever else it publishes; mtime is the finish, as with sentinels.
def _task_record_ws(days=5, include_today=True, name="ghost-job", finish=(6, 3),
                    file_name=None):
    ws = Path(tempfile.mkdtemp(prefix="punct-tc-"))
    (ws / "hosts" / "H").mkdir(parents=True)
    (ws / "state").mkdir()
    (ws / "results").mkdir()
    (ws / "hosts" / "H" / "crons.json").write_text(json.dumps(
        [{"name": name, "cron": "0 6 * * *"}]))
    today = datetime.date.today()
    for i in range(days):
        if i == 0 and not include_today:
            continue
        d = today - datetime.timedelta(days=i)
        f = ws / "results" / f"task-cron-{file_name or name}-{1787000000000 + i}.txt"
        f.write_text("x")
        os.utime(f, (time.time(),
                     datetime.datetime.combine(d, datetime.time(*finish)).timestamp()))
    return ws


r = run(_task_record_ws())
check("a job with only task-cron records is OBSERVABLE",
      "ghost-job" not in (r.get("detail") or ""), f"got {r.get('detail')!r}")
check("...and a punctual record history is not reported late",
      r.get("status") == "ok", f"got {r.get('status')}: {r.get('detail')}")

r = run(_task_record_ws(include_today=False))
check("a task-cron history that stops TODAY surfaces as a named miss",
      "ghost-job" in (r.get("detail") or "") and "no output today" in (r.get("detail") or ""),
      f"got {r.get('detail')!r}")

# The control that can fail: no records in ANY lane must stay UNCHECKED, so the
# fallback cannot manufacture observability out of nothing.
r = run(_task_record_ws(days=0))
_d = r.get("detail") or ""
check("no evidence in any of the three lanes stays UNCHECKED",
      "ghost-job" in _d and ("UNCHECKED" in _d or "unverifiable" in _d), f"got {_d!r}")

# Records for a DIFFERENT job must not vouch for this one — the glob is anchored
# on the full job name, so a prefix-sharing neighbour cannot bleed across.
ws = _task_record_ws(days=0)
for i in range(5):
    d = datetime.date.today() - datetime.timedelta(days=i)
    f = ws / "results" / f"task-cron-ghost-job-extra-{1787000000000 + i}.txt"
    f.write_text("x")
    os.utime(f, (time.time(),
                 datetime.datetime.combine(d, datetime.time(6, 3)).timestamp()))
r = run(ws)
_d = r.get("detail") or ""
check("another job's records do NOT make this one observable",
      "ghost-job" in _d and ("UNCHECKED" in _d or "unverifiable" in _d), f"got {_d!r}")


# `cron-runner` writes task-cron-<SLUG>-<stamp>; globbing the RAW name finds nothing.
import cron_task_id  # noqa: E402

for _raw, _slug in [("Money scan", "Money-scan"), ("a/b", "a-b"), ("...", "unnamed"),
                    ("daily.report.job", "daily-report-job"), ("mo*ney", "mo-ney")]:
    check(f"slug contract: {_raw!r} -> {_slug}", cron_task_id.sanitize_name(_raw) == _slug,
          f"got {cron_task_id.sanitize_name(_raw)!r}")

    # Configured under the RAW name, recorded under the SLUG: the regression.
    _r = run(_task_record_ws(name=_raw, file_name=_slug))
    _det = _r.get("detail") or ""
    check(f"a job named {_raw!r} is found under its written slug",
          _raw not in _det and _slug not in _det, f"got {_det!r}")

# The decoys must still be refused after the contract change.
_ws = _task_record_ws(name="Money scan", file_name="Money-scan")
for _i in range(5):
    _d0 = datetime.date.today() - datetime.timedelta(days=_i)
    for _decoy in (f"task-cron-Money-scan-extra-{1787000000000 + _i}.txt",
                   f"task-cron-Money-scanner-{1787000000000 + _i}.txt"):
        _f = _ws / "results" / _decoy
        _f.write_text("x")
        os.utime(_f, (time.time(),
                      datetime.datetime.combine(_d0, datetime.time(6, 3)).timestamp()))
_matcher = cron_task_id.record_matcher("Money scan")
check("neighbour suffix is refused", not _matcher.match("task-cron-Money-scan-extra-123.txt"))
check("bare-prefix neighbour is refused", not _matcher.match("task-cron-Money-scanner-123.txt"))
check("the job's own record is accepted", bool(_matcher.match("task-cron-Money-scan-123.txt")))
# Numeric-prefix neighbour: `ghost-job-2` is its own configured job, so its
# record must not vouch for `ghost-job`. The stamp has to END the slug.
_gj = cron_task_id.record_matcher("ghost-job")
check("numeric-suffix neighbour is refused",
      not _gj.match("task-cron-ghost-job-2-1787000000000.txt"))
check("numeric-suffix neighbour is refused with a trailing marker too",
      not _gj.match("task-cron-ghost-job-2-1787000000000-late-duplicate.txt"))
check("control: the neighbour still claims its own record",
      bool(cron_task_id.record_matcher("ghost-job-2")
           .match("task-cron-ghost-job-2-1787000000000.txt")))
# Real filename shapes that MUST keep matching — measured against 2050 live
# task-cron-* files: 2041 `.txt`, 8 `-late-duplicate.txt`, 1 `.no-task.<n>.txt`.
check("own record, plain", bool(_gj.match("task-cron-ghost-job-1787000000000.txt")))
check("own record, -late-duplicate marker",
      bool(_gj.match("task-cron-ghost-job-1787000000000-late-duplicate.txt")))
check("own record, .no-task.<stamp> marker",
      bool(_gj.match("task-cron-ghost-job-1787801434542.no-task.1787803712.txt")))
# Numeric-plus-text neighbour, built with BOTH production helpers so the writer
# and the matcher are exercised against each other rather than a hand-typed name.
for _nb in ("ghost-job-2", "ghost-job-2-late", "ghost-job-2-text",
            "ghost-job-42-backfill"):
    _fn = cron_task_id.task_id(_nb, 1787000000000) + ".txt"
    check(f"neighbour {_nb!r} does not vouch for ghost-job",
          not cron_task_id.record_matcher("ghost-job").match(_fn))
    check(f"control: {_nb!r} still claims its own record",
          bool(cron_task_id.record_matcher(_nb).match(_fn)))
# The three suffixes records actually carry must keep matching.
for _sfx in ("", "-late-duplicate", ".no-task.1787803712"):
    _own = cron_task_id.task_id("ghost-job", 1787000000000) + _sfx + ".txt"
    check(f"own record with suffix {_sfx!r} is accepted",
          bool(cron_task_id.record_matcher("ghost-job").match(_own)))
# An archived record is `task-cron-<job>-<emit-ms>-<archive-s>.txt`; 54 of one
# host's 1331 records carried that shape and matched no job at all.
check("archived record (emit stamp + archive stamp) is accepted",
      bool(_gj.match("task-cron-ghost-job-1788172659933-1788173709.txt")))
check("archived record plus -late-duplicate is accepted",
      bool(_gj.match(
          "task-cron-ghost-job-1788172659933-1788173709-late-duplicate.txt")))
# The neighbour guard must survive the widening: `2` is not a stamp, so a
# 13-digit number after it cannot be read as an archive stamp.
check("numeric-suffix neighbour still refused against the archived shape",
      not _gj.match("task-cron-ghost-job-2-1788172659933.txt"))
check("control: a two-stamp neighbour still claims its own record",
      bool(cron_task_id.record_matcher("ghost-job-2")
           .match("task-cron-ghost-job-2-1788172659933-1788173709.txt")))
check("a short second field is not an archive stamp",
      not _gj.match("task-cron-ghost-job-1788172659933-7.txt"))

check("task_id spells the writer's filename",
      cron_task_id.task_id("Money scan", 123) == "task-cron-Money-scan-123")
check("discovery glob carries no job name", "*" == cron_task_id.DISCOVERY_GLOB[-1]
      and "task-cron-" == cron_task_id.DISCOVERY_GLOB[:-1])

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
