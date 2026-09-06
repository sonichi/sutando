#!/usr/bin/env python3
"""Two room-visible projections of one private runtime: what this agent is doing on THIS task, and
whether it can take more work. Neither says why it is busy.

The room availability contract is deliberately narrow — available | busy_accepting |
busy_unavailable | offline | unknown — computed here from the private canonical numbers
(active runs, capacity, queue depth, runtime health, accepting flag) that never leave the owner's
side. A member sees "Busy · not accepting work", never "reviewing the acquisition documents" or
"3 of 4 runs". Agents consume the same projection: a second agent asked for help can see the first
is on it and offer to work independently instead of doubling up.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from workspace_default import resolve_workspace

AVAILABILITY = ("available", "busy_accepting", "busy_unavailable", "offline", "unknown")
# What a room policy may turn each true value into: the same, something less available, or
# unknown. Never more available, never a different known fact (offline is a fact, not a level).
NARROWING = {"available": frozenset({"available", "busy_accepting", "busy_unavailable", "unknown"}),
             "busy_accepting": frozenset({"busy_accepting", "busy_unavailable", "unknown"}),
             "busy_unavailable": frozenset({"busy_unavailable", "unknown"}),
             "offline": frozenset({"offline", "unknown"}), "unknown": frozenset({"unknown"})}
ACTIVE_PHASES = frozenset({"RUNNING", "WAITING"})
HEARTBEAT_MAX_AGE_S = 90.0  # matches the core liveness rule: younger than ~90 s is alive
# A live run keeps writing its snapshot (events, transitions); one silent this long is a leftover.
ACTIVE_SNAPSHOT_MAX_AGE_S = 1800.0
WORK_SIGNALS = ("working", "idle", "wedged", "unknown")
# A pane reading older than this is no reading: the health probe samples about once a minute and
# stops when the pane is gone, so an old window must never outrank a stale heartbeat.
WORK_SIGNAL_MAX_AGE_S = 180.0
# The CLI wedge detector reads the pane, not the process: its verdict kinds fold to the three the
# room state acts on. Every warning kind (a wedge in some shape) is "wedged"; unreadable is unknown.
_WEDGE_KIND_TO_SIGNAL = {"working": "working", "clock-only": "working", "idle": "idle",
                         "static-with-work": "wedged", "retry-loop": "wedged", "provider-limit": "wedged",
                         "low-novelty": "wedged"}


def work_signal_from_verdict(verdict, max_age_s: float = WORK_SIGNAL_MAX_AGE_S) -> str:
    if not isinstance(verdict, dict):
        return "unknown"
    age = verdict.get("last_sample_age_s")
    if isinstance(age, (int, float)) and age > max_age_s:
        return "unknown"
    return _WEDGE_KIND_TO_SIGNAL.get(verdict.get("kind"), "unknown")


def wedge_window_verdict(ws: Path, now: float | None = None):
    """The latest verdict from the window the health probe already writes; never a new pane sample
    here. No window, an unreadable one, or an engine without the detector reads as None."""
    try:
        import cli_wedge
        now = now if now is not None else time.time()
        entries = cli_wedge.load_window(cli_wedge.window_path(ws))
        if not entries:
            return None
        verdict = dict(cli_wedge.classify_window(entries, cli_wedge.work_outstanding(ws, now), now))
        verdict.setdefault("last_sample_age_s", max(0.0, now - max(float(e.get("ts", 0)) for e in entries)))
        return verdict
    except Exception:  # noqa: BLE001
        return None


@dataclass
class AgentRuntimeState:
    """Private, canonical. Change the scheduler and only this changes; the room contract holds."""

    active_runs: int = 0
    max_concurrency: int = 1
    queue_depth: int = 0
    runtime_healthy: bool | None = None  # None = nothing readable
    accepting_work: bool = True
    last_heartbeat_at: float | None = None
    last_runtime_update_at: float | None = None
    disconnected: bool = False  # an explicit, known disconnect: offline, never merely unknown
    work_signal: str = "unknown"  # the wedge detector's reading: working | idle | wedged | unknown


def is_fresh(state: AgentRuntimeState, now: float | None = None, max_age_s: float = HEARTBEAT_MAX_AGE_S) -> bool:
    now = now if now is not None else time.time()
    return state.last_heartbeat_at is not None and now - state.last_heartbeat_at < max_age_s


def availability(state: AgentRuntimeState, now: float | None = None) -> str:
    """The narrow room-visible value. offline is a known fact (an explicit disconnect); unknown is
    missing or stale telemetry — other agents decide differently on the two, so they never merge.
    Never derived from 'has a running task' alone: 2 of 4 runs active is busy_accepting."""
    if state.disconnected:
        return "offline"
    # A heartbeat and a status file stay fresh on a wedged core; the pane reading outranks them.
    if state.work_signal == "wedged":
        return "busy_unavailable"
    if state.work_signal in ("working", "idle"):
        if not state.accepting_work:
            return "busy_unavailable"
        if state.work_signal == "idle" and state.queue_depth <= 0:
            return "available"
        return "busy_accepting" if state.active_runs < max(state.max_concurrency, 1) else "busy_unavailable"
    if state.runtime_healthy is None or not is_fresh(state, now):
        return "unknown"
    if not state.runtime_healthy:
        return "unknown"
    if not state.accepting_work:
        return "busy_unavailable"
    if state.active_runs <= 0 and state.queue_depth <= 0:
        return "available"
    if state.active_runs < max(state.max_concurrency, 1):
        return "busy_accepting"
    return "busy_unavailable"


ROOM_AVAILABILITY_FIELDS = frozenset({"worker", "room", "availability", "audience", "projection", "ts"})
ROOM_TASK_STATUS_FIELDS = frozenset({"task_id", "message_event_id", "worker", "phase", "since_s", "last_status_at",
                                     "audience", "projection"})
# Never in a room payload, by key: what a server log, a sync or another client could otherwise read.
FORBIDDEN_IN_ROOM = frozenset({"summary", "thinking", "tool", "command", "private_room_id", "active_runs",
                               "capacity", "max_concurrency", "queue_depth", "reason", "seq", "activity_session_id",
                               "work_signal", "verdict", "confidence"})


def room_payload(payload: dict, allowed: frozenset[str]) -> dict:
    """Privacy happens here, before any transport: a key outside the room allowlist or inside the
    denylist is refused, not trimmed. A raise, not an assert — this must survive `python -O`."""
    extra = set(payload) - allowed
    leaked = set(payload) & FORBIDDEN_IN_ROOM
    if extra or leaked:
        raise ValueError(f"room payload refused: outside allowlist {sorted(extra)}, forbidden {sorted(leaked)}")
    return payload


def availability_projection(state: AgentRuntimeState, worker: str | None = None, room_id: str | None = None,
                            room_policy=None, now: float | None = None) -> dict:
    """What THIS room may see. No counts, no reasons, no queue contents. The canonical state is global;
    `room_policy(room_id, value) -> value` lets a room learn less (never more), so a member of one
    room cannot infer what the agent does in another."""
    value = availability(state, now)
    if room_policy is not None:
        narrowed = room_policy(room_id, value)
        if narrowed in NARROWING[value]:  # a policy may only say less; anything else is ignored
            value = narrowed
    payload = {"worker": worker, "room": room_id, "availability": value, "audience": "room",
               "projection": "AVAILABILITY", "ts": now if now is not None else time.time()}
    return room_payload(payload, ROOM_AVAILABILITY_FIELDS)


def task_projection(snapshot: dict, now: float | None = None) -> dict:
    """The wire shape of one task's TASK_STATUS: who is on it, where it stands, since when. Its input
    is the bus's shared_projection (the snapshot); the audience that snapshot carries is kept, never
    widened to the room here."""
    started = snapshot.get("started_at")
    now = now if now is not None else time.time()
    payload = {"task_id": snapshot.get("task_id"), "message_event_id": snapshot.get("message_event_id"),
               "worker": snapshot.get("worker"), "phase": snapshot.get("phase"),
               "since_s": (max(0.0, now - started) if isinstance(started, (int, float)) else None),
               "last_status_at": snapshot.get("last_activity_at"), "audience": snapshot.get("audience") or "room",
               "projection": "TASK_STATUS"}
    return room_payload(payload, ROOM_TASK_STATUS_FIELDS)


def this_host() -> str:
    """The label the heartbeat writes under, via the one resolver core_heartbeat uses; never a glob.
    state/cores/ is synced across hosts, so any other choice lets a peer answer for this agent."""
    try:
        from util_paths import _host_label
        return _host_label()
    except Exception:  # noqa: BLE001
        import platform
        return platform.node().split(".")[0]


def _session_started_at(ws: Path) -> float | None:
    """When this core session's task watcher started: it dispatches every task that gets a snapshot and
    dies with the session, so its pid file's mtime is the session boundary (the heartbeat writer is not)."""
    try:
        return (ws / "state" / "watch-tasks-stream.pid").stat().st_mtime
    except OSError:
        return None


def snapshot_is_live(written_at: float, now: float, task_file_present: bool, session_started_at: float | None) -> bool:
    """A RUNNING snapshot counts as work while its task is still in the queue; once the task file is gone,
    only a run of this session, still within a live run's event cadence, can be finishing it."""
    if task_file_present:
        return True
    if session_started_at is not None and written_at < session_started_at:
        return False
    return now - written_at <= ACTIVE_SNAPSHOT_MAX_AGE_S


def read_runtime_state(workspace: Path | None = None, host: str | None = None,
                       max_concurrency: int = 1, now: float | None = None,
                       work_probe: Callable[[Path, float], object] | None = None) -> AgentRuntimeState:
    """The private numbers from what the engine already writes: the wedge detector's window (the
    work signal), task snapshots under state/activity (active runs), the tasks/ directory (queue
    depth), and THIS host's core heartbeat (health, the fallback when there is no work signal)."""
    ws = workspace or resolve_workspace()
    host = host or this_host()
    now = now if now is not None else time.time()
    try:
        verdict = (work_probe or wedge_window_verdict)(ws, now)
    except Exception:  # noqa: BLE001
        verdict = None
    signal = work_signal_from_verdict(verdict)
    session_started_at = _session_started_at(ws)
    active = 0
    for p in (ws / "state" / "activity").glob("task-*.json"):
        try:
            phase = json.loads(p.read_text(encoding="utf-8")).get("phase")
            task_file_present = (ws / "tasks" / f"{p.stem}.txt").exists()
            if phase in ACTIVE_PHASES and snapshot_is_live(p.stat().st_mtime, now, task_file_present, session_started_at):
                active += 1
        except (OSError, ValueError):
            continue
    queue = sum(1 for p in (ws / "tasks").glob("task-*.txt") if not p.name.startswith("task-cron-")) if (ws / "tasks").exists() else 0
    healthy: bool | None = None
    beat_at: float | None = None
    disconnected = False
    cores = ws / "state" / "cores"
    beats = [cores / f"{host}.alive"] if (cores / f"{host}.alive").exists() else []
    if beats:
        beat_at = max(b.stat().st_mtime for b in beats)
        healthy = True  # the heartbeat writer only writes while the core runs; staleness is judged by age
    else:
        # The heartbeat unlinks its file on a graceful stop: no file plus a stop marker is a KNOWN
        # disconnect; no file and no marker is merely unknown.
        try:
            status = json.loads((ws / "state" / "core-status.json").read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError):
            status = None
        disconnected = status in ("stopped", "shutdown", "offline")
    # accepting_work: the scheduler's pause control sets it later; this reader never does.
    return AgentRuntimeState(active_runs=active, max_concurrency=max_concurrency, queue_depth=queue,
                             runtime_healthy=healthy, accepting_work=True, last_heartbeat_at=beat_at,
                             disconnected=disconnected, work_signal=signal)
