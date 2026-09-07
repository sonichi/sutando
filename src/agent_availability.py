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


@dataclass
class AgentRuntimeState:
    """Private, canonical. Change the scheduler and only this changes; the room contract holds."""

    active_runs: int = 0
    max_concurrency: int = 1
    queue_depth: int = 0
    runtime_healthy: bool | None = None  # None = nothing readable
    accepting_work: bool = True


def availability(state: AgentRuntimeState) -> str:
    """The narrow room-visible value. Never derived from 'has a running task' alone: a concurrent
    agent with 2 of 4 runs active is busy_accepting, not busy."""
    if state.runtime_healthy is None:
        return "unknown"
    if not state.runtime_healthy:
        return "offline"
    if not state.accepting_work:
        return "busy_unavailable"
    if state.active_runs <= 0 and state.queue_depth <= 0:
        return "available"
    if state.active_runs < max(state.max_concurrency, 1):
        return "busy_accepting"
    return "busy_unavailable"


def availability_projection(state: AgentRuntimeState, worker: str | None = None, room_id: str | None = None,
                            room_policy=None) -> dict:
    """What THIS room may see. No counts, no reasons, no queue contents. The canonical state is global;
    `room_policy(room_id, value) -> value` lets a room learn less (never more), so a member of one
    room cannot infer what the agent does in another."""
    value = availability(state)
    if room_policy is not None:
        narrowed = room_policy(room_id, value)
        if narrowed in NARROWING[value]:  # a policy may only say less; anything else is ignored
            value = narrowed
    return {"worker": worker, "room": room_id, "availability": value, "audience": "room",
            "projection": "AVAILABILITY", "ts": time.time()}


def task_projection(snapshot: dict, now: float | None = None) -> dict:
    """What the room may see about one task: who is on it, where it stands, since when. Built from
    the bus's shared projection; carries no summary and no steps."""
    started = snapshot.get("started_at")
    now = now if now is not None else time.time()
    return {"task_id": snapshot.get("task_id"), "message_event_id": snapshot.get("message_event_id"),
            "worker": snapshot.get("worker"), "phase": snapshot.get("phase"),
            "since_s": (max(0.0, now - started) if isinstance(started, (int, float)) else None),
            "audience": snapshot.get("audience") or "room", "projection": "TASK_STATUS"}


def this_host() -> str:
    """The label the heartbeat writes under, via the one resolver core_heartbeat uses; never a glob.
    state/cores/ is synced across hosts, so any other choice lets a peer answer for this agent."""
    try:
        from util_paths import _host_label
        return _host_label()
    except Exception:  # noqa: BLE001
        import platform
        return platform.node().split(".")[0]


def _heartbeat_started_at(path: Path) -> float | None:
    """When this host's heartbeat writer started: a snapshot written before it belongs to an earlier life."""
    try:
        v = json.loads(path.read_text(encoding="utf-8")).get("started_at")
        return float(v) if isinstance(v, (int, float)) else None
    except (OSError, ValueError, TypeError):
        return None


def snapshot_is_live(written_at: float, now: float, core_started_at: float | None) -> bool:
    """A RUNNING snapshot counts as work only while a live core could still be writing it."""
    if core_started_at is not None and written_at < core_started_at:
        return False
    return now - written_at <= ACTIVE_SNAPSHOT_MAX_AGE_S


def read_runtime_state(workspace: Path | None = None, host: str | None = None,
                       max_concurrency: int = 1, now: float | None = None) -> AgentRuntimeState:
    """The private numbers from what the engine already writes: task snapshots under state/activity
    (active runs), the tasks/ directory (queue depth), and THIS host's core heartbeat (health)."""
    ws = workspace or resolve_workspace()
    host = host or this_host()
    now = now if now is not None else time.time()
    beat = ws / "state" / "cores" / f"{host}.alive"
    core_started_at = _heartbeat_started_at(beat) if beat.exists() else None
    active = 0
    for p in (ws / "state" / "activity").glob("task-*.json"):
        try:
            phase = json.loads(p.read_text(encoding="utf-8")).get("phase")
            if phase in ACTIVE_PHASES and snapshot_is_live(p.stat().st_mtime, now, core_started_at):
                active += 1
        except (OSError, ValueError):
            continue
    queue = sum(1 for p in (ws / "tasks").glob("task-*.txt") if not p.name.startswith("task-cron-")) if (ws / "tasks").exists() else 0
    healthy: bool | None = None
    cores = ws / "state" / "cores"
    beats = [cores / f"{host}.alive"] if (cores / f"{host}.alive").exists() else []
    if beats:
        healthy = any(now - b.stat().st_mtime < HEARTBEAT_MAX_AGE_S for b in beats)
    # accepting_work: the scheduler's pause control sets it later; this reader never does.
    return AgentRuntimeState(active_runs=active, max_concurrency=max_concurrency, queue_depth=queue,
                             runtime_healthy=healthy, accepting_work=True)
