#!/usr/bin/env python3
"""Attribution gate: replay a day of agent-activity rows and check each row's task and room.

    python3 scripts/attribution-check.py [--day 2026-10-31] [--grace 120] [--workspace W] [--max-wrong-pct N]

A row is CORRECT when its task id resolves to a task file, the row's time falls inside that task's
processing window (task timestamp .. result time + grace), and its room is the task's channel_id.
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
import glob
import hashlib
import json
import os
import re
import sys


def h(s):
    return hashlib.sha256(str(s).encode()).hexdigest()[:8]


def headers(path):
    out = {}
    try:
        with open(path, errors="replace") as f:
            for line in f:
                m = re.match(r"^([a-z_]+):\s?(.*)$", line.rstrip("\n"))
                if not m:
                    break
                out.setdefault(m.group(1), m.group(2))
    except OSError:
        pass
    return out


def task_index(W):
    """id -> (start_epoch, channel_id) from every task file, live or archived."""
    idx = {}
    for p in glob.glob(f"{W}/tasks/*.txt") + glob.glob(f"{W}/tasks/archive/*.txt"):
        tid = os.path.basename(p)[:-4]
        hd = headers(p)
        ts = hd.get("timestamp")
        try:
            start = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp() if ts else os.path.getmtime(p)
        except ValueError:
            start = os.path.getmtime(p)
        idx[hd.get("id") or tid] = (start, hd.get("channel_id") or hd.get("source_room_id"))
    return idx


def result_index(W):
    """id -> result time: the epoch suffix of an archived result, else the live result's mtime."""
    out = {}
    for p in glob.glob(f"{W}/results/archive/*.txt"):
        m = re.match(r"^(.*)-(\d{10})\.txt$", os.path.basename(p))
        if m:
            out[m.group(1)] = max(out.get(m.group(1), 0), int(m.group(2)))
    for p in glob.glob(f"{W}/results/*.txt"):
        out[os.path.basename(p)[:-4]] = max(out.get(os.path.basename(p)[:-4], 0), os.path.getmtime(p))
    return out


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
    ap.add_argument("--workspace", default=os.environ.get("SUTANDO_WORKSPACE_DIR"))
    ap.add_argument("--day", default=(dt.date.today() - dt.timedelta(days=1)).isoformat())
    ap.add_argument("--grace", type=float, default=120.0, help="seconds after the result a row may still land")
    ap.add_argument("--max-wrong-pct", type=float, default=None, help="gate: exit 1 above this share")
    a = ap.parse_args(argv)
    W = a.workspace
    tasks, results = task_index(W), result_index(W)
    c, by_kind, ex = collections.Counter(), collections.defaultdict(collections.Counter), collections.defaultdict(list)
    windows = sorted((st, results[k] + a.grace, k) for k, (st, _) in tasks.items() if k in results)

    def other_live(ts, tid):
        return any(st <= ts <= en and k != tid for st, en, k in windows if st <= ts)
    for r in rows_for(W, a.day):
        t = r.get("task")
        tid = t.get("id") if isinstance(t, dict) else t
        kind = r.get("kind")
        ts = float(r.get("ts") or 0)
        if not tid:
            v = "unlinked"
        elif tid not in tasks:
            v = "unlinked_unknown_task"
        else:
            start, room = tasks[tid]
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
