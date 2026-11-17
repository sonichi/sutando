#!/usr/bin/env python3
"""Attribution gate: replay a day of agent-activity rows and check each row's task and room.

    python3 scripts/attribution-check.py [--day 2026-10-31] [--grace 120] [--workspace W] [--max-wrong-pct N]

A row is CORRECT when its task id resolves to a task file, the row's time falls inside that task's
processing window (picked up .. result time + grace), and its room is the task's channel_id. Picked up
is the task's first `processing` row that day, else the task file's timestamp. Task files and results
are found through src/local_task_protocol.py (live, flat and month-partitioned archives).
UNLINKED: the row names no task, or a task id that no task file has. WRONG_ROOM: the room is not the
task's channel. WRONG_TASK: outside the window while another task was in flight (the row belongs to
that one). STALE: outside the window with no other task in flight (late, but not misattributed). Deterministic, no model.
Output is redacted: counts, and examples carry only a hash of the task id, the row kind and the offset.
Report-only by default (exit 0). With --max-wrong-pct it is a gate: exit 1 when the wrong share is
above it. Rows carry no seat id yet, so "another task in flight" is host-wide.
"""
import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import local_task_protocol as ltp  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402


def h(s):
    return hashlib.sha256(str(s).encode()).hexdigest()[:8]


def _start_from_file(path):
    """The task file's own `timestamp:` header, else its mtime."""
    try:
        hd = ltp.parse_task_headers_lenient(Path(path).read_text(errors="replace")).headers
    except OSError:
        return None, None
    try:
        start = dt.datetime.strptime(hd.get("timestamp", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        start = os.path.getmtime(path)
    return start, hd.get("channel_id") or hd.get("source_room_id")


def task_index(W, ids):
    """id -> (file start, channel_id), through the shared lookup (live, flat and month archives)."""
    out = {}
    for tid in ids:
        p = ltp.find_archived_task(Path(W) / "tasks", tid)
        if p is not None:
            out[tid] = _start_from_file(p)
    return out


def _result_time(path):
    m = re.match(r"^.*-(\d{10})\.txt$", path.name)
    return int(m.group(1)) if m else os.path.getmtime(path)


def result_index(W, ids):
    """id -> latest result time, from every candidate the shared owner knows (live and all archives).
    index_result_candidates is the bulk form of find_result: the same files, one listing per layout."""
    return {tid: max(_result_time(p) for p in ps)
            for tid, ps in ltp.index_result_candidates(Path(W) / "results", ids).items() if ps}


def rows_for(W, day):
    files = [f"{W}/state/agent-activity.archive.{day}.jsonl"]
    if not os.path.exists(files[0]):
        files = [f"{W}/state/agent-activity.jsonl"]
    for f in files:
        for line in open(f, errors="replace"):
            try:
                yield json.loads(line)
            except ValueError:
                continue


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=None, help="default: the resolved Sutando workspace")
    ap.add_argument("--day", default=(dt.date.today() - dt.timedelta(days=1)).isoformat())
    ap.add_argument("--grace", type=float, default=120.0, help="seconds after the result a row may still land")
    ap.add_argument("--max-wrong-pct", type=float, default=None, help="gate: exit 1 above this share")
    a = ap.parse_args(argv)
    W = a.workspace or str(resolve_workspace(migrate=False))
    try:
        rows = list(rows_for(W, a.day))
    except OSError as e:
        print(json.dumps({"day": a.day, "error": f"no activity log: {type(e).__name__}"}))
        return 2

    def tid_of(r):
        t = r.get("task")
        return t.get("id") if isinstance(t, dict) else t

    ids = {tid_of(r) for r in rows if tid_of(r)}
    tasks, results = task_index(W, ids), result_index(W, ids)
    # The window opens when the task was picked up (its first processing row), not when the message came.
    picked = {}
    for r in rows:
        if r.get("kind") == "processing" and tid_of(r):
            picked[tid_of(r)] = min(picked.get(tid_of(r), float("inf")), float(r.get("ts") or 0))
    tasks = {k: (picked.get(k, st), room) for k, (st, room) in tasks.items()}
    c, by_kind, ex = collections.Counter(), collections.defaultdict(collections.Counter), collections.defaultdict(list)
    windows = sorted((st, results[k] + a.grace, k) for k, (st, _) in tasks.items() if k in results)

    def other_live(ts, tid):
        return any(st <= ts <= en and k != tid for st, en, k in windows if st <= ts)
    for r in rows:
        tid = tid_of(r)
        kind = r.get("kind")
        ts = float(r.get("ts") or 0)
        if not tid:
            v = "unlinked"
        elif tid not in tasks:
            v = "unlinked_unknown_task"
        else:
            start, room = tasks[tid]
            if start is None:
                start = ts
            end = results.get(tid)
            if r.get("room") and room and r["room"] != room:
                v = "wrong_room"
            elif ts < start - a.grace or (end is not None and ts > end + a.grace):
                # Late or early with no other task in flight is stale, not misattributed.
                v = "wrong_task" if other_live(ts, tid) else "stale_no_other_task"
                ex[v].append({"task": h(tid), "kind": kind,
                              "offset_s": round(ts - (end if ts > (end or ts) else start)),
                              "side": "after_result" if end is not None and ts > end else "before_start"})
            elif not r.get("room"):
                v = "correct_no_room"
            else:
                v = "correct"
        if v == "wrong_room":
            ex[v].append({"task": h(tid), "kind": kind})
        c[v] += 1
        by_kind[kind][v] += 1
    n = sum(c.values()) or 1
    ok = c["correct"] + c["correct_no_room"]
    unl = c["unlinked"] + c["unlinked_unknown_task"]
    wrong = c["wrong_room"] + c["wrong_task"]
    stale = c["stale_no_other_task"]
    report = {"day": a.day, "rows": sum(c.values()), "grace_s": a.grace,
              "correct_pct": round(100 * ok / n, 1), "unlinked_pct": round(100 * unl / n, 1),
              "wrong_pct": round(100 * wrong / n, 1), "stale_pct": round(100 * stale / n, 1), "counts": dict(c),
              "by_kind": {k: dict(v) for k, v in by_kind.items()},
              "examples": {k: v[:8] for k, v in ex.items()}}
    print(json.dumps(report, indent=1))
    return 1 if a.max_wrong_pct is not None and 100 * wrong / n > a.max_wrong_pct else 0


if __name__ == "__main__":
    sys.exit(main())
