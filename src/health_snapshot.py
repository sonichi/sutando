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
import gateway_serving
import pool_suspension
import runtime_observation
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
# health-check's window for a gateway-status sidecar; the bridge rewrites it on every poll.
GATEWAY_STALE_S = 180.0

# Pane-derived claims a later completed model request disproves; every other reason stands.
PANE_SUPERSEDABLE = frozenset({"needs-login", "login", "quota-limit", "out-of-credits", "session-limit",
                               "api-error", "network-error"})

# Observation phases with no model request in flight.
RETRY_DISPROVING_PHASES = frozenset({"idle", "waiting", "failed"})

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


def _core_seen_since(ws: Path, supervisor: dict, heartbeat: dict) -> bool:
    """True when a fresh beat written after a `crashed` verdict saw a live core pane: the
    beat records the core's pid only when tmux shows one, else its own pid."""
    if ((supervisor.get("opinion") or {}).get("reason") != "crashed" or heartbeat.get("value") != "fresh"
            or supervisor["age_s"] is None or supervisor["age_s"] <= heartbeat["age_s"]):
        return False
    beat, _ = _read_json(ws / heartbeat["path"])
    return (isinstance(beat, dict) and isinstance(beat.get("pid"), int)
            and beat.get("pid") != beat.get("heartbeat_pid"))


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


def _observation(ws: Path, now: float, seat: str, session, started=None):
    """(source, record): the record only when it counts as evidence about this seat's current run."""
    path = runtime_observation.record_path(ws, seat)
    src = {"path": _rel(path, ws), "age_s": None, "value": None, "opinion": None}
    rec, _why = runtime_observation.load(ws, seat, now)
    if rec is None:
        return src, None
    src["age_s"] = _age(rec["heartbeat_at"], now)
    # A record from another tmux session, or from before this worker's current incarnation, is not about this run.
    if session and rec["session"] != session:
        return src, None
    if started is not None and rec["observer_started_at"] < started:
        return {**src, "value": {"previous_run": True}}, None
    abnormal = rec["condition"] == ABNORMAL
    last_success = rec["last_success_at"]
    value = {"phase": rec["phase"], "observer": rec["observer"], "observer_version": rec["observer_version"],
             "seq": rec["seq"], "heartbeat_age_s": src["age_s"],
             "last_success_age_s": None if last_success is None else _age(last_success, now)}
    op = _opinion(None if rec["motion"] == UNKNOWN else rec["motion"],
                  None if rec["condition"] == UNKNOWN else rec["condition"],
                  rec["reason"] if abnormal else None,
                  rec["condition_since"] if abnormal else None)
    return {**src, "value": value, "opinion": op}, rec


def _supersede(sources: dict, rec, now: float) -> dict:
    """A completed model request after a pane-derived claim disproves the claim."""
    if rec is None:
        return sources
    out = dict(sources)
    for name in ("supervisor", "cli_wedge"):
        src = out.get(name)
        op = src.get("opinion") if src else None
        if not op or op["condition"] != ABNORMAL:
            continue
        claimed = op["since"] if op["since"] is not None else (
            None if src.get("age_s") is None else now - src["age_s"])
        newer_success = (rec["last_success_at"] is not None and claimed is not None
                         and claimed < rec["last_success_at"])
        # A retry loop needs a request in flight. Its claim time is the window's run start, not
        # when retry text appeared, so a newer success proves nothing about it.
        if name == "cli_wedge" and op["reason"] == "retry-loop":
            drop = rec["phase"] in RETRY_DISPROVING_PHASES
        else:
            drop = op["reason"] in PANE_SUPERSEDABLE and newer_success
        if drop:
            out[name] = {**src, "value": {**(src["value"] if isinstance(src["value"], dict) else {}),
                                          "superseded_by": "observation"}, "opinion": None}
    return out


def _alive(beat: dict, supervisor: dict | None = None):
    """True / False from a beat source, None when there is no beat to judge. A current
    `crashed` verdict is False: the beat's writer can outlive the session it vouches for."""
    if ((supervisor or {}).get("opinion") or {}).get("reason") == "crashed":
        return False
    return {"fresh": True, "stale": False}.get(beat.get("value"))


def _verdict(agent: dict, sources: dict) -> dict:
    """Abnormal from any source wins; moving from any source wins. The first abnormal source,
    in the order given, names the reason."""
    ops = [s["opinion"] for s in sources.values() if s.get("opinion")]
    # A dead agent's last words (e.g. a supervisor file left at idle-ready) say nothing now.
    offline = next((o for o in ops if o["reason"] == "offline"), None)
    if offline:
        # The pool giving up is fresher news about the same death, and it needs a person.
        dead = next((o for o in ops if o["reason"] == "not-answering"), offline)
        return {**agent, "motion": UNKNOWN, "condition": ABNORMAL, "reason": dead["reason"],
                "since": dead["since"] if dead["since"] is not None else offline["since"]}
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


def _instance(ws: Path, now: float):
    """The identity a serving gateway lane signed in as; None when no lane is serving or serving
    lanes disagree, since a wrong identity would show this Mac twice."""
    ids = set()
    for path in (ws / "state").glob("gateway-status*.json"):
        value, _ = _read_json(path)
        verdict = gateway_serving.verdict_from_record(value, now=now, max_age=GATEWAY_STALE_S)
        agent_id = value.get("agent_id") if isinstance(value, dict) else None
        if verdict and verdict.serving and isinstance(agent_id, str) and agent_id:
            ids.add(agent_id)
    return ids.pop() if len(ids) == 1 else None


def _suspension(ws: Path):
    try:
        return pool_suspension.read(ws)
    except OSError:
        return None


def _suspended(rec):
    """{reason, at} while the worker pool is suspended, else None."""
    return {"reason": rec["reason"], "at": rec["at"]} if rec else None


def _stopped_by_suspension(row: dict, rec) -> dict:
    """A worker the suspension took down is not alive, whatever its files last said:
    a quit kills the tmux server before any seat can record its own end."""
    return {**row, "alive": False, "motion": UNKNOWN, "condition": UNKNOWN,
            "reason": "suspended", "since": rec["at"]}


def _session(path):
    """The tmux session a beat or supervisor file names, or None."""
    value, _ = _read_json(path) if path else (None, None)
    session = value.get("session") if isinstance(value, dict) else None
    return session if isinstance(session, str) and session else None


def snapshot(workspace=None, *, agent: str = "all", view: str = "summary", now=None) -> dict:
    """The health snapshot. `agent`: all | core | workers | <worker id>. `view`: summary | full."""
    if view not in VIEWS:
        raise ValueError(f"view must be one of {', '.join(VIEWS)}")
    ws = Path(workspace) if workspace is not None else Path(resolve_workspace())
    now = time.time() if now is None else now
    workers = _workers(ws)
    suspension = _suspension(ws)
    stopped = set(suspension["stopped"]) if suspension else set()
    activity = _activity_by_agent(ws, now, [w[0] for w in workers])
    agents = []

    if agent in ("all", "core"):
        supervisor = _supervisor_source(Path(status_read_path("core-supervisor.json", ws)), ws, now)
        heartbeat = _heartbeat_source(ws, now)
        if _core_seen_since(ws, supervisor, heartbeat):
            supervisor = {**supervisor, "value": {**(supervisor["value"] or {}), "superseded": True},
                          "opinion": None}
        session = (_session(ws / "state" / "cores" / f"{_host_label()}.alive")
                   or _session(Path(status_read_path("core-supervisor.json", ws))))
        observation, obs_rec = _observation(ws, now, "core", session)
        sources = _supersede({
            "supervisor": supervisor,
            "observation": observation,
            "cli_wedge": _wedge_source(ws, now),
            "heartbeat": heartbeat,
            "activity": _activity_source("core", activity, now),
            "self_report": _status_source(ws, now),
        }, obs_rec, now)
        agents.append((_verdict({"id": "core", "role": "core", "label": None, "session": session,
                                 "alive": _alive(sources["heartbeat"], sources["supervisor"])}, sources), sources))

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
            observation, obs_rec = _observation(ws, now, wid, _session(seat), started)
            sources = _supersede({
                "supervisor": supervisor,
                "observation": observation,
                "watcher_beat": _heartbeat_source(ws, now, ws / "state" / "watchers" / f"{wid}.alive"),
                "pool": _pool_source((pool_rows or {}).get(wid), sampled, now),
                "roster": {"path": "state/roster.json", "age_s": None, "value": {"state": state},
                           "opinion": (None if state in (None, "live") else
                                       _opinion(None, ABNORMAL, str(state)))},
                "activity": _activity_source(wid, activity, now),
            }, obs_rec, now)
            # Only a verdict known to be this incarnation's may override the beat's alive.
            current = sources["supervisor"] if started is not None else None
            row = _verdict({"id": wid, "role": "worker", "label": label, "session": _session(seat),
                            "alive": _alive(sources["watcher_beat"], current)}, sources)
            agents.append((_stopped_by_suspension(row, suspension) if wid in stopped else row, sources))

    conditions = {a["condition"] for a, _ in agents}
    overall = ("attention" if ABNORMAL in conditions else
               "ok" if agents and conditions == {HEALTHY} else "unknown")
    out = {"checked_at": round(now, 1), "instance": _instance(ws, now), "overall": overall,
           "suspended": _suspended(suspension),
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
