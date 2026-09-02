#!/usr/bin/env python3
"""src/task_output_retention.py — the steady-state task-output sweep (R3).

  * a dir whose lease is younger than 26 h is kept — including across the
    daemon job's longest legitimate gap (24 h horizon + backoff, under 26 h);
  * a dir whose lease is older than 26 h is removed;
  * a crash-abandoned dir (no lease ever touched) ages by its newest mtime;
  * sweep-vs-consuming-job: a lease renewed at the moment of the sweep wins;
  * the lock keeps two sweeps apart, and a stale lock is broken;
  * only `task-signal-*` REAL directories are candidates — symlinks, files and
    the archive dirs are untouched;
  * run_hourly reclaims WITHOUT a restart (injected clock, short interval) and
    re-runs the startup archiver's logic each pass.

Run: python3 tests/task-output-retention.test.py
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import task_output_retention as ret  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


H = 3600
T0 = 1_800_000_000.0


def fresh():
    results = Path(tempfile.mkdtemp(prefix="retention-")) / "results"
    results.mkdir()
    return results


def make_dir(results, name, *, lease_at=None, mtime=None):
    d = results / name
    d.mkdir()
    (d / "img.png").write_bytes(b"x")
    if mtime is not None:
        os.utime(d / "img.png", (mtime, mtime))
        os.utime(d, (mtime, mtime))
    if lease_at is not None:
        ret.touch_lease(d, now=lease_at)
    return d


check("lease threshold is 26h", ret.LEASE_STALE_SEC == 26 * H)
check("sweep interval is hourly", ret.SWEEP_INTERVAL_SEC == H)

print("== lease age decides ==")
results = fresh()
young = make_dir(results, "task-signal-1-young", lease_at=T0)
backoff = make_dir(results, "task-signal-2-backoff", lease_at=T0)
old = make_dir(results, "task-signal-3-old", lease_at=T0 - 26 * H - 1)
report = ret.sweep_task_outputs(results, now=T0 + 1)
check("fresh lease kept", young.exists() and "task-signal-1-young" in report["kept"])
check("26h-old lease removed", not old.exists() and "task-signal-3-old" in report["removed"])
report = ret.sweep_task_outputs(results, now=T0 + 25 * H)
check("lease survives the 24h horizon + longest backoff (25h)", backoff.exists(), str(report))
report = ret.sweep_task_outputs(results, now=T0 + 26 * H + 1)
check("same dir aged out once past 26h", not backoff.exists() and not young.exists())
check("lock released after each sweep", not (results / ret.LOCK_NAME).exists())

print("== crash-abandoned dirs age by mtime ==")
results = fresh()
abandoned = make_dir(results, "task-signal-4-abandoned", mtime=T0 - 30 * H)
recent = make_dir(results, "task-signal-5-recent", mtime=T0 - 1 * H)
touched = make_dir(results, "task-signal-6-touched", mtime=T0 - 30 * H)
(touched / "late.png").write_bytes(b"y")
os.utime(touched / "late.png", (T0 - 2 * H, T0 - 2 * H))
report = ret.sweep_task_outputs(results, now=T0)
check("no-lease dir older than 26h removed", not abandoned.exists())
check("no-lease dir with a recent write kept", recent.exists())
check("newest file mtime counts as liveness", touched.exists(), str(report))

print("== sweep vs consuming job ==")
results = fresh()
consumed = make_dir(results, "task-signal-7-consumed", mtime=T0 - 40 * H)
ret.touch_lease(consumed, now=T0 - 1)
report = ret.sweep_task_outputs(results, now=T0)
check("an old dir whose lease was just renewed by a serve is kept", consumed.exists())
# A renewal landing after the scan but before the delete still wins: simulate by
# making the scan read a stale lease and the final re-read a fresh one.
racing = make_dir(results, "task-signal-8-racing", lease_at=T0 - 27 * H)
real_liveness = ret._liveness


def liveness_then_renew(task_dir):
    out = real_liveness(task_dir)
    if task_dir.name == "task-signal-8-racing":
        ret.touch_lease(task_dir, now=T0)
    return out


ret._liveness = liveness_then_renew
try:
    report = ret.sweep_task_outputs(results, now=T0)
finally:
    ret._liveness = real_liveness
check("lease renewed between scan and delete wins", racing.exists() and "task-signal-8-racing" in report["kept"])

print("== lock ==")
results = fresh()
victim = make_dir(results, "task-signal-9-victim", lease_at=T0 - 30 * H)
lock = results / ret.LOCK_NAME
lock.write_text("other")
os.utime(lock, (T0 - 60, T0 - 60))
report = ret.sweep_task_outputs(results, now=T0)
check("a live lock skips the sweep", report["skipped"] == "locked" and victim.exists())
os.utime(lock, (T0 - 2 * H, T0 - 2 * H))
report = ret.sweep_task_outputs(results, now=T0)
check("a stale lock is broken and the sweep runs", not victim.exists() and report["skipped"] is None)

print("== only real task-signal-* directories are candidates ==")
results = fresh()
elsewhere = Path(tempfile.mkdtemp(prefix="retention-target-"))
(elsewhere / "keep.png").write_bytes(b"z")
(results / "task-signal-10-link").symlink_to(elsewhere)
os.utime(elsewhere, (T0 - 40 * H, T0 - 40 * H))
archive = results / "archive-2026-01-01"
archive.mkdir()
(archive / "task-signal-11.txt").write_text("old")
os.utime(archive, (T0 - 40 * H, T0 - 40 * H))
owner = make_dir(results, "task-123", mtime=T0 - 40 * H)
stray = results / "task-signal-12.txt"
stray.write_text("result text")
os.utime(stray, (T0 - 40 * H, T0 - 40 * H))
report = ret.sweep_task_outputs(results, now=T0)
check("a symlinked task-signal dir is ignored and its target intact",
      (elsewhere / "keep.png").exists() and (results / "task-signal-10-link").is_symlink())
check("archive dirs untouched", archive.exists())
check("non-signal dirs untouched", owner.exists())
check("result text files untouched", stray.exists())
check("nothing reported removed", report["removed"] == [], str(report))
check("missing results dir is a no-op", ret.sweep_task_outputs(results / "nope")["skipped"] == "no results dir")

print("== hourly loop reclaims without a restart ==")
results = fresh()
doomed = make_dir(results, "task-signal-13-doomed", lease_at=T0)
archived = []
stop = threading.Event()
thread = threading.Thread(target=ret.run_hourly, args=(results, stop),
                          kwargs={"interval": 0.02, "archive_results": lambda: archived.append(1),
                                  "clock": lambda: T0 + 27 * H}, daemon=True)
thread.start()
deadline = time.time() + 5
while doomed.exists() and time.time() < deadline:
    time.sleep(0.01)
check("the loop removed the stale dir with no restart", not doomed.exists())
check("the archiver hook ran alongside", len(archived) >= 1)
stop.set()
thread.join(timeout=2)
check("the loop stops on the event", not thread.is_alive())


def boom():
    raise RuntimeError("archiver down")


stop = threading.Event()
survivor = make_dir(results, "task-signal-14-survivor", lease_at=T0)
thread = threading.Thread(target=ret.run_hourly, args=(results, stop),
                          kwargs={"interval": 0.02, "archive_results": boom,
                                  "clock": lambda: T0 + 27 * H}, daemon=True)
thread.start()
deadline = time.time() + 5
while survivor.exists() and time.time() < deadline:
    time.sleep(0.01)
check("an archiver failure does not stop the sweep", not survivor.exists())
stop.set()
thread.join(timeout=2)

print()
if failures:
    print(f"  {len(failures)} FAILURE(S): {', '.join(failures)}")
    sys.exit(1)
print("PASS — task-output retention")
