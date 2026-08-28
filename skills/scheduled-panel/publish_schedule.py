#!/usr/bin/env python3
"""Publish the agent's durable schedule (crons.json + last-run state) as a
Room Context doc the AG2 Space "Scheduled" Activity tab reads. Owner-greenlit
2026-08-21: source is durable crons.json, cross-session stable."""
from __future__ import annotations
import json, os, sys, subprocess, datetime, importlib.util

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _cfg(arg: str) -> str:
    return subprocess.run(["bash", os.path.join(REPO, "scripts/sutando-config.sh"), arg],
                          capture_output=True, text=True).stdout.strip()

def _field_set(field: str, lo: int, hi: int) -> set[int]:
    # One cron field -> the set of matching ints. Handles *, */n, a,b, x-y, x-y/n.
    vals: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1); step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
        vals.update(range(a, b + 1, step))
    return vals

def _human_cron(expr: str) -> str:
    p = expr.split()
    if len(p) != 5:
        return expr
    m, h, dom, mon, dow = p
    if m.startswith("*/") and h == "*":
        return f"every {m[2:]} min"
    if ("," in m or "-" in m) and h == "*":
        return f"~every few min ({m})"
    if m.isdigit() and h.isdigit() and dom == "*" and mon == "*":
        return (f"daily {int(h):02d}:{int(m):02d}" if dow == "*"
                else f"{dow} {int(h):02d}:{int(m):02d}")
    return expr

def _next_fire(expr: str, now: datetime.datetime) -> str:
    p = expr.split()
    if len(p) != 5:
        return "—"
    try:
        mins, hrs = _field_set(p[0], 0, 59), _field_set(p[1], 0, 23)
        dows = _field_set(p[3 + 1], 0, 6)  # cron dow 0-6 (Sun=0); mon field p[3] ignored (always * here)
    except Exception:
        return "—"
    t = now.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
    for _ in range(8 * 24 * 60):  # minute scan, 8-day bound
        # python weekday(): Mon=0..Sun=6 -> cron dow Sun=0..Sat=6
        if t.minute in mins and t.hour in hrs and ((t.weekday() + 1) % 7) in dows:
            return t.strftime("%Y-%m-%dT%H:%MZ")
        t += datetime.timedelta(minutes=1)
    return "—"

def _iso(ts) -> str:
    try:
        if isinstance(ts, (int, float)):
            return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%dT%H:%MZ")
        return str(ts)[:16].replace(" ", "T")
    except Exception:
        return "—"

def _last_fired(job: dict, ws: str, host: str) -> str:
    # Best-effort per-cron last-run signal; heterogeneous by design.
    name = job.get("name", "")
    state = os.path.join(ws, "state")
    def _read(path, key):
        try:
            d = json.load(open(path)); return d.get(key)
        except Exception:
            return None
    if name == "main-loop":
        v = _read(os.path.join(state, "core-status.json"), "ts")
        if v:
            return _iso(v)
    # Crons that stamp their own state file expose last_pass (e.g. my-pr-shepherd).
    v = _read(os.path.join(state, f"{name}.json"), "last_pass")
    if v:
        return _iso(v)
    alive = os.path.join(state, f"dynamic-loop-{name}.alive")
    if os.path.exists(alive):
        return _iso(os.path.getmtime(alive))
    return "—"

def build_rows() -> list[dict]:
    ws, host = _cfg("workspace"), _cfg("host-label")
    cf = os.path.join(ws, "hosts", host, "crons.json")
    raw = json.load(open(cf))
    jobs = raw if isinstance(raw, list) else raw.get("jobs", raw.get("crons", []))
    now = datetime.datetime.now(datetime.UTC)
    rows = []
    for j in jobs:
        expr = j.get("cron", "")
        dyn = j.get("loop") == "dynamic"
        rows.append({
            "name": j.get("name", "?"),
            "schedule": "adaptive (self-paced)" if dyn else _human_cron(expr),
            "next_fire": "—" if dyn else _next_fire(expr, now),
            "last_fired": _last_fired(j, ws, host),
            "does": j.get("prompt_skill") or (j.get("prompt", "")[:48]) or "—",
        })
    return rows

def render_md(rows: list[dict]) -> str:
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%MZ")
    out = [f"# Scheduled jobs", f"*updated {now} — source: durable crons.json*", "",
           "| Job | Schedule | Last fired | Next fire | Does |",
           "|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['name']} | {r['schedule']} | {r['last_fired']} | {r['next_fire']} | {r['does']} |")
    return "\n".join(out) + "\n"

SCHED_FOLDER = "activity"   # client "Scheduled" tab reads folder=activity name=SCHEDULE.md
SCHED_NAME = "SCHEDULE.md"

def publish(room: str, content: str) -> dict:
    # Reuse the gateway room-ops doc client; room is caller-supplied (no id baked in).
    ops = os.path.join(REPO, "skills/agent-room-ops")
    if ops not in sys.path:
        sys.path.insert(0, ops)   # doc.py does a sibling-relative `from _gateway import ...`
    spec = importlib.util.spec_from_file_location("_ops_doc", os.path.join(ops, "doc.py"))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod.doc_put(room, content, folder=SCHED_FOLDER, name=SCHED_NAME,
                       message="scheduled-jobs refresh")

def _arg(flag: str):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None

if __name__ == "__main__":
    rows = build_rows()
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=2)); sys.exit(0)
    md = render_md(rows)
    room = _arg("--room")
    if "--publish" in sys.argv and room:
        r = publish(room, md)
        print("published" if r.get("ok") else f"FAILED: {r.get('reason')}")
    else:
        print(md)
