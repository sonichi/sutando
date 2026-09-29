#!/usr/bin/env python3
"""Health snapshot — one read-only answer per agent (core and workers) from existing state files.

Each agent carries `alive` (true | false | null, from its beat file) and reads as motion (idle | moving | unknown) × condition (healthy | abnormal | unknown),
plus a reason when abnormal. Sources are the files the heartbeat, supervisors, cli_wedge, the pool
supervisor and the activity hooks already write; nothing here probes a process or a pane, and
nothing is written. A source past its freshness window gives no opinion rather than a stale one.

    python3 src/health_snapshot.py [--agent all|core|workers|<worker id>] [--view summary|full]

`summary` is safe to leave the Mac (no paths, no pane text); `full` adds every source with its age
and value, workspace-relative, for local debugging only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli_wedge
from util_paths import _host_label
from workspace_default import resolve_workspace, status_read_path

VIEWS = ("summary", "full")
IDLE, MOVING, UNKNOWN = "idle", "moving", "unknown"
HEALTHY, ABNORMAL = "healthy", "abnormal"

# Same windows the existing readers use: .alive and a "running" self-report go stale at 90 s,
# a cli_wedge reading at 180 s (agent_availability), a pool sample after 3 of its 300 s periods.
HEARTBEAT_STALE_S = 90.0
BEAT_FUTURE_TOLERANCE_S = 5.0
STATUS_STALE_S = 90.0
WEDGE_STALE_S = 180.0
# Motion changes by the second: an older "the pane was moving" says nothing about now.
WEDGE_MOTION_FRESH_S = 30.0
POOL_STALE_S = 900.0
ACTIVITY_LIVE_S = 120.0
ACTIVITY_TAIL_BYTES = 256 * 1024

# core-input-watch states → (motion, condition, reason). A blocked-human reason is refined from `kind`.
SUPERVISOR = {
    "running": (MOVING, HEALTHY, None),
    "idle-ready": (IDLE, HEALTHY, None),
    "blocked-known": (IDLE, HEALTHY, None),
    "blocked-human": (IDLE, ABNORMAL, "awaiting-input"),
    "logged-out": (IDLE, ABNORMAL, "needs-login"),
    "hung": (IDLE, ABNORMAL, "hung"),
    "crashed": (None, ABNORMAL, "crashed"),
    "gateway-down": (None, ABNORMAL, "gateway-down"),
    "unobserved": (None, None, None),
}


def _read_json(path: Path):
    """(value, mtime) — (None, None) when absent or unreadable."""
    try:
        mtime = path.stat().st_mtime
        return json.loads(path.read_text()), mtime
    except (OSError, ValueError):
        return None, None


def _age(mtime, now):
    return None if mtime is None else round(max(0.0, now - mtime), 1)


def _rel(path: Path, ws: Path) -> str:
    try:
        return str(path.relative_to(ws))
    except ValueError:
        return path.name


def _opinion(motion=None, condition=None, reason=None, since=None):
    return {"motion": motion, "condition": condition, "reason": reason, "since": since}


def _supervisor_source(path: Path, ws: Path, now: float) -> dict:
    value, mtime = _read_json(path)
    src = {"path": _rel(path, ws), "age_s": _age(mtime, now)}
    if not isinstance(value, dict):
        return {**src, "value": None, "opinion": None}
    state = value.get("state")
    src["value"] = {"state": state, "kind": value.get("kind")}
    if state not in SUPERVISOR:
        return {**src, "opinion": None}
    motion, condition, reason = SUPERVISOR[state]
    kind = value.get("kind")
    if state == "blocked-human" and isinstance(kind, str) and kind and kind != "unknown":
        reason = kind
    return {**src, "opinion": _opinion(motion, condition, reason, mtime if condition == ABNORMAL else None)}


def _heartbeat_source(ws: Path, now: float, path: Path | None = None) -> dict:
    """Liveness from a beat file's mtime: the core's .alive, or a worker watcher's beat."""
    path = path or ws / "state" / "cores" / f"{_host_label()}.alive"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    src = {"path": _rel(path, ws), "age_s": _age(mtime, now)}
    if mtime is None:
        # Absent is not dead: the desktop core runs for ~2 min before anything starts the heartbeat.
        return {**src, "value": "missing", "opinion": None}
    # A future-dated beat is as untrustworthy as an old one.
    if now - mtime > HEARTBEAT_STALE_S or mtime - now > BEAT_FUTURE_TOLERANCE_S:
        return {**src, "value": "stale", "opinion": _opinion(None, ABNORMAL, "offline", mtime)}
    return {**src, "value": "fresh", "opinion": None}


def _status_source(ws: Path, now: float) -> dict:
    path = Path(status_read_path("core-status.json", ws))
    value, mtime = _read_json(path)
    src = {"path": _rel(path, ws), "age_s": _age(mtime, now)}
    status = value.get("status") if isinstance(value, dict) else None
    ts = value.get("ts") if isinstance(value, dict) else None
    src["value"] = {"status": status}
    fresh = isinstance(ts, (int, float)) and 0 <= now - ts <= STATUS_STALE_S
    return {**src, "opinion": _opinion(MOVING) if status == "running" and fresh else None}


def _wedge_source(ws: Path, now: float) -> dict:
    path = cli_wedge.window_path(ws)
    src = {"path": _rel(path, ws)}
    try:
        entries = cli_wedge.load_window(path)
        if not entries:
            return {**src, "age_s": None, "value": None, "opinion": None}
        verdict = cli_wedge.classify_window(entries, cli_wedge.work_outstanding(ws, now), now)
    except Exception:  # noqa: BLE001 — an unreadable window is no reading, never a verdict
        return {**src, "age_s": None, "value": None, "opinion": None}
    age = verdict.get("last_sample_age_s")
    kind = verdict.get("kind")
    src.update(age_s=age, value={"kind": kind, "abnormal": verdict.get("current_abnormal") or []})
    if not isinstance(age, (int, float)) or age > WEDGE_STALE_S:
        return {**src, "opinion": None}
    motion = IDLE if verdict.get("raw_static") else MOVING
    names = list(verdict.get("current_abnormal") or [])
    if kind == "working":
        op = _opinion(MOVING if age <= WEDGE_MOTION_FRESH_S else None, HEALTHY)
    elif kind == "idle":
        op = _opinion(IDLE, HEALTHY)
    elif kind == "retry-loop":
        op = _opinion(MOVING, ABNORMAL, "retry-loop", verdict.get("run_started"))
    elif kind == "provider-limit":
        limit = next((n for n in names if n in cli_wedge.PROVIDER_LIMIT_PATTERNS), "provider-limit")
        op = _opinion(motion, ABNORMAL, limit, verdict.get("run_started"))
    elif kind == "abnormal":
        op = _opinion(motion, ABNORMAL, names[0] if names else "abnormal", verdict.get("run_started"))
    else:
        op = None
    return {**src, "opinion": op}


def _has_result(ws: Path, tid: str) -> bool:
    """A result file is the task's last write, so its presence ends the task. Archivers move it
    within seconds, flat or into a month folder: archive/[<YYYY-MM>/]<id>[-<ts>].txt."""
    results = ws / "results"
    if (results / f"{tid}.txt").exists():
        return True
    name = re.compile(re.escape(tid) + r"(?:-\d+)?\.txt")
    return any(name.fullmatch(f.name) for f in (results / "archive").rglob(f"{tid}*.txt"))


def _activity_by_agent(ws: Path, now: float, worker_ids) -> dict:
    """{agent id: last row ts} for tasks with no `done` row whose newest row is recent."""
    path = ws / "state" / "agent-activity.jsonl"
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            start = max(0, fh.tell() - ACTIVITY_TAIL_BYTES)
            fh.seek(start)
            # A tail that starts mid-file starts mid-line; drop that partial row.
            lines = fh.read().decode("utf-8", "replace").splitlines()[1 if start else 0:]
    except OSError:
        return {}
    last, done = {}, set()
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        task = row.get("task") if isinstance(row, dict) else None
        tid = task.get("id") if isinstance(task, dict) else None
        ts = row.get("ts")
        if not isinstance(tid, str) or not isinstance(ts, (int, float)):
            continue
        last[tid] = max(ts, last.get(tid, 0))
        if row.get("done") or row.get("kind") == "done":
            done.add(tid)
    out = {}
    for tid, ts in last.items():
        if tid in done or now - ts > ACTIVITY_LIVE_S or _has_result(ws, tid):
            continue
        owner = next((w for w in worker_ids if (ws / "deliveries" / w / f"{tid}.txt").exists()), "core")
        out[owner] = max(ts, out.get(owner, 0))
    return out


def _activity_source(agent_id: str, activity: dict, now: float) -> dict:
    ts = activity.get(agent_id)
    src = {"path": "state/agent-activity.jsonl", "age_s": _age(ts, now),
           "value": "live task" if ts else "no live task"}
    return {**src, "opinion": _opinion(MOVING) if ts else None}


def _pool_source(entry, sampled_at, now: float) -> dict:
    src = {"path": "state/pool-supervision.json", "age_s": _age(sampled_at, now)}
    if not isinstance(entry, dict):
        return {**src, "value": None, "opinion": None}
    src["value"] = {k: entry.get(k) for k in ("consecutive", "escalated", "watcher_consecutive",
                                               "watcher_escalated", "wedge_consecutive", "wedge_escalated")}
    if sampled_at is None or now - sampled_at > POOL_STALE_S:
        return {**src, "opinion": None}
    for flag, reason, since in (("escalated", "not-answering", "first_detected_at"),
                                ("wedge_escalated", "wedged", "wedge_first_detected_at"),
                                ("watcher_escalated", "watcher-down", "watcher_first_detected_at")):
        if entry.get(flag):
            return {**src, "opinion": _opinion(None, ABNORMAL, reason, entry.get(since))}
    return {**src, "opinion": None}


def _alive(beat: dict):
    """True / False from a beat source, None when there is no beat to judge."""
    return {"fresh": True, "stale": False}.get(beat.get("value"))


def _verdict(agent: dict, sources: dict) -> dict:
    """Abnormal from any source wins; moving from any source wins. The first abnormal source,
    in the order given, names the reason."""
    ops = [s["opinion"] for s in sources.values() if s.get("opinion")]
    # A dead agent's last words (e.g. a supervisor file left at idle-ready) say nothing now.
    offline = next((o for o in ops if o["reason"] == "offline"), None)
    if offline:
        return {**agent, "motion": UNKNOWN, "condition": ABNORMAL, "reason": "offline", "since": offline["since"]}
    motions = {o["motion"] for o in ops if o["motion"]}
    bad = [o for o in ops if o["condition"] == ABNORMAL]
    good = [o for o in ops if o["condition"] == HEALTHY]
    return {
        **agent,
        "motion": MOVING if MOVING in motions else IDLE if IDLE in motions else UNKNOWN,
        "condition": ABNORMAL if bad else HEALTHY if good else UNKNOWN,
        "reason": bad[0]["reason"] if bad else None,
        "since": bad[0]["since"] if bad else None,
    }


def _workers(ws: Path):
    """[(id, label, roster state)] for non-retired workers; [] with no pool."""
    roster, _ = _read_json(ws / "state" / "roster.json")
    rows = roster.get("workers") if isinstance(roster, dict) else None
    if not isinstance(rows, dict):
        return []
    out = []
    for wid, row in sorted(rows.items()):
        row = row if isinstance(row, dict) else {}
        if row.get("state") == "retired":
            continue
        label = row.get("label")
        out.append((wid, label if isinstance(label, str) and label != wid else None, row.get("state")))
    return out


def _worker_started_at(ws: Path, wid: str):
    """Epoch start of the worker's current incarnation, or None when its records are unreadable."""
    base = ws / "state" / "workers" / wid
    current, _ = _read_json(base / "current.json")
    records, _ = _read_json(base / "incarnations.json")
    inc = current.get("incarnation_id") if isinstance(current, dict) else None
    rows = records.get("incarnations") if isinstance(records, dict) else None
    row = next((r for r in rows or [] if isinstance(r, dict) and r.get("incarnation_id") == inc), None)
    try:
        return datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00")).timestamp()
    except (TypeError, KeyError, ValueError):
        return None


def _worker_supervisor_paths(ws: Path) -> dict:
    """{session name: path} for per-seat supervisor files (core-supervisor.<session>.json)."""
    out = {}
    for path in (ws / "state").glob("core-supervisor.*.json"):
        value, _ = _read_json(path)
        session = value.get("session") if isinstance(value, dict) else None
        if isinstance(session, str):
            out[session] = path
    return out


def snapshot(workspace=None, *, agent: str = "all", view: str = "summary", now=None) -> dict:
    """The health snapshot. `agent`: all | core | workers | <worker id>. `view`: summary | full."""
    if view not in VIEWS:
        raise ValueError(f"view must be one of {', '.join(VIEWS)}")
    ws = Path(workspace) if workspace is not None else Path(resolve_workspace())
    now = time.time() if now is None else now
    workers = _workers(ws)
    activity = _activity_by_agent(ws, now, [w[0] for w in workers])
    agents = []

    if agent in ("all", "core"):
        sources = {
            "supervisor": _supervisor_source(Path(status_read_path("core-supervisor.json", ws)), ws, now),
            "cli_wedge": _wedge_source(ws, now),
            "heartbeat": _heartbeat_source(ws, now),
            "activity": _activity_source("core", activity, now),
            "self_report": _status_source(ws, now),
        }
        agents.append((_verdict({"id": "core", "role": "core", "label": None,
                                 "alive": _alive(sources["heartbeat"])}, sources), sources))

    if agent != "core":
        pool, pool_mtime = _read_json(ws / "state" / "pool-supervision.json")
        pool_rows = pool.get("workers") if isinstance(pool, dict) else {}
        sampled = pool.get("last_sample_at") if isinstance(pool, dict) else None
        sampled = sampled if isinstance(sampled, (int, float)) else pool_mtime
        seats = _worker_supervisor_paths(ws)
        for wid, label, state in workers:
            if agent not in ("all", "workers", wid):
                continue
            seat = next((p for s, p in seats.items() if s.rsplit("-", 1)[-1] == wid), None)
            supervisor = (_supervisor_source(seat, ws, now) if seat else
                          {"path": None, "age_s": None, "value": None, "opinion": None})
            started = _worker_started_at(ws, wid)
            # A verdict written before this incarnation began is the previous run's, not this one's.
            if seat and started is not None and supervisor["age_s"] is not None and now - supervisor["age_s"] < started:
                supervisor = {**supervisor, "value": {**(supervisor["value"] or {}), "previous_run": True},
                              "opinion": None}
            sources = {
                "supervisor": supervisor,
                "watcher_beat": _heartbeat_source(ws, now, ws / "state" / "watchers" / f"{wid}.alive"),
                "pool": _pool_source((pool_rows or {}).get(wid), sampled, now),
                "roster": {"path": "state/roster.json", "age_s": None, "value": {"state": state},
                           "opinion": (None if state in (None, "live") else
                                       _opinion(None, ABNORMAL, str(state)))},
                "activity": _activity_source(wid, activity, now),
            }
            agents.append((_verdict({"id": wid, "role": "worker", "label": label,
                                     "alive": _alive(sources["watcher_beat"])}, sources), sources))

    conditions = {a["condition"] for a, _ in agents}
    overall = ("attention" if ABNORMAL in conditions else
               "ok" if agents and conditions == {HEALTHY} else "unknown")
    out = {"checked_at": round(now, 1), "overall": overall,
           "agents": [a if view == "summary" else {**a, "sources": s} for a, s in agents]}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agent", default="all")
    ap.add_argument("--view", default="summary", choices=VIEWS)
    ap.add_argument("--workspace", default=None)
    a = ap.parse_args(argv)
    print(json.dumps(snapshot(a.workspace, agent=a.agent, view=a.view), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
