#!/usr/bin/env python3
"""ActivityCard is the UI projection of a durable TaskRun state machine; runtime instrumentation
only enriches that state machine with observations.

Two vocabularies, never one enum: TaskPhase is the state machine the scheduler owns
(RECEIVED → QUEUED → RUNNING ↔ WAITING → COMPLETED | FAILED | CANCELLED, terminal phases sticky);
RuntimeEvent is an observation a provider made (a hook, a pane observer, a heartbeat, an app
server). Only `reduce()` turns an observation into a phase change. State is the source of truth;
the rows the client renders, the snapshot file and any later server stream are projections of it.

Dedup is by key, not by "does a row of this phase exist": a lifecycle transition by
(task_id, generation, from_phase, to_phase), a runtime event by (activity_session_id, seq).
A task legitimately runs RUNNING → WAITING → RUNNING many times; each is its own generation.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from activity_rows import append as append_row
from activity_rows import task_from_file
from workspace_default import resolve_workspace

PHASES = ("RECEIVED", "QUEUED", "RUNNING", "WAITING", "COMPLETED", "FAILED", "CANCELLED")
TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "RECEIVED": frozenset({"QUEUED", "RUNNING", "FAILED", "CANCELLED"}),
    "QUEUED": frozenset({"RUNNING", "FAILED", "CANCELLED"}),
    "RUNNING": frozenset({"WAITING", "COMPLETED", "FAILED", "CANCELLED"}),
    "WAITING": frozenset({"RUNNING", "COMPLETED", "FAILED", "CANCELLED"}),
}
EVENT_KINDS = ("Status", "Thinking", "Working", "ToolStarted", "ToolFinished",
               "InteractionRequired", "Heartbeat", "RuntimeStarted", "RuntimeStopped")
# Audience is a property of the projection, decided here, enforced by the transport that carries it:
# the lifecycle is what a room may know (someone is on it), telemetry is the owner's alone.
SCOPES = ("owner", "room", "participants")
LIFECYCLE_SCOPE = "room"
TELEMETRY_SCOPE = "owner"


@dataclass
class TaskActivityState:
    """The durable TaskRun. Everything the card shows derives from this, never the reverse."""

    task_id: str
    phase: str = "RECEIVED"
    generation: int = 0  # bumps each time the task enters RUNNING: one per run
    seq: int = 0  # last runtime-event sequence applied, across sessions
    emitted: int = 0  # rows projected so far: the stable projection id of the next row
    message_event_id: str | None = None
    room: str | None = None
    sender: str | None = None
    text: str | None = None
    activity_session_id: str | None = None
    worker: str | None = None
    started_at: float | None = None
    last_activity_at: float | None = None
    summary: str = ""
    into: str | None = None  # a consolidated reply: the holder message's event id
    pending: list[dict] = field(default_factory=list)  # rows reduced but not yet projected
    visibility: dict = field(default_factory=lambda: {"lifecycle": LIFECYCLE_SCOPE, "telemetry": TELEMETRY_SCOPE})
    applied: list[str] = field(default_factory=list)
    sessions: dict[str, int] = field(default_factory=dict)
    telemetry: int = 0


@dataclass(frozen=True)
class LifecycleTransition:
    task_id: str
    to_phase: str
    from_phase: str | None = None  # None = whatever the state is in now
    generation: int | None = None  # None = the state's current generation
    ts: float | None = None
    reason: str = ""
    message_event_id: str | None = None
    room: str | None = None
    sender: str | None = None
    text: str | None = None
    worker: str | None = None
    into: str | None = None

    def key(self, state: TaskActivityState) -> str:
        gen = self.generation if self.generation is not None else state.generation
        frm = self.from_phase or state.phase
        return f"{self.task_id}:{gen}:{frm}:{self.to_phase}"


@dataclass(frozen=True)
class RuntimeEvent:
    task_id: str
    activity_session_id: str
    seq: int
    kind: str
    ts: float | None = None
    text: str = ""
    tool: str | None = None
    requirement_id: str | None = None


def _task_dict(state: TaskActivityState) -> dict:
    t: dict = {"id": state.task_id}
    if state.sender:
        t["from"] = state.sender
    if state.text:
        t["text"] = state.text
    if state.message_event_id:
        t["event"] = state.message_event_id
    if state.into:
        t["into"] = state.into
    return t


def _row(state: TaskActivityState, kind: str, line: str, ts: float, done: bool = False,
         scope: str | None = None) -> dict:
    # The pid rides in the snapshot's pending list, so a replay projects the same identity again
    # and the row writer applies it once.
    state.emitted += 1
    return {"kind": kind, "line": line, "ts": ts, "room": state.room, "task": _task_dict(state), "done": done, "pid": f"{state.task_id}:{state.generation}:{state.emitted}",
            "scope": scope or state.visibility.get("telemetry", TELEMETRY_SCOPE)}


def shared_projection(state: TaskActivityState) -> dict:
    """What the room may see: who is on it and where it stands. No summary text, no steps."""
    return {"task_id": state.task_id, "message_event_id": state.message_event_id, "worker": state.worker,
            "phase": state.phase, "generation": state.generation, "started_at": state.started_at,
            "last_activity_at": state.last_activity_at, "scope": state.visibility.get("lifecycle", LIFECYCLE_SCOPE)}


def private_projection(state: TaskActivityState) -> dict:
    """What only the owner's own clients receive: the shared fields plus the summary and the sequence."""
    return dict(shared_projection(state), summary=state.summary, seq=state.seq,
                activity_session_id=state.activity_session_id, scope=state.visibility.get("telemetry", TELEMETRY_SCOPE))


_PHASE_ROW = {
    "QUEUED": ("notice", "queued"),
    "RUNNING": ("processing", "picked up"),
    "WAITING": ("notice", "waiting for you"),
    "COMPLETED": ("done", "replied"),
    "FAILED": ("done", "failed"),
    "CANCELLED": ("done", "cancelled"),
}


def reduce(state: TaskActivityState, item: LifecycleTransition | RuntimeEvent) -> tuple[TaskActivityState, list[dict]]:
    """Pure: returns the next state and the rows that project this step. Duplicates and
    out-of-order items change nothing; a terminal phase never regresses."""
    now = item.ts if item.ts is not None else time.time()
    rows: list[dict] = []
    if isinstance(item, LifecycleTransition):
        key = item.key(state)
        if key in state.applied:
            return state, rows
        frm = item.from_phase or state.phase
        if (item.to_phase == "QUEUED" and state.phase in ("RUNNING", "WAITING") and item.from_phase is None
                and state.started_at is not None and now <= state.started_at and "queued" not in state.applied):
            # QUEUED and RUNNING are emitted by independent processes and can land out of order: an
            # earlier-stamped QUEUED is history — row written, phase kept, a replay of it a no-op.
            state.applied += ["queued", key]
            rows.append(_row(state, "notice", "queued", now))
            return state, rows
        if frm != state.phase or state.phase in TERMINAL or item.to_phase not in TRANSITIONS.get(state.phase, frozenset()):
            # Not a valid move from where the task is: a stale or replayed transition. Telemetry only.
            state.telemetry += 1
            return state, rows
        if item.to_phase == "QUEUED":
            state.applied.append("queued")
        for f in ("message_event_id", "room", "sender", "text", "worker", "into"):
            v = getattr(item, f)
            if v:
                setattr(state, f, v)
        if item.to_phase == "RUNNING":
            state.generation += 1
        state.applied.append(key)
        state.phase = item.to_phase
        state.last_activity_at = now
        if state.started_at is None and item.to_phase in ("RUNNING", "QUEUED"):
            state.started_at = now
        if item.reason:
            state.summary = item.reason
        kind, line = _PHASE_ROW[item.to_phase]
        if item.to_phase == "COMPLETED" and state.into:
            line = "consolidated"
        elif item.reason and item.to_phase in TERMINAL:
            line = f"{line}: {item.reason}" if item.to_phase != "COMPLETED" else item.reason
        rows.append(_row(state, kind, line, now, done=item.to_phase in TERMINAL,
                         scope=state.visibility.get("lifecycle", LIFECYCLE_SCOPE)))
        return state, rows
    # a runtime observation
    last = state.sessions.get(item.activity_session_id, 0)
    if item.seq <= last:
        return state, rows  # duplicate or out of order: the later seq already won
    state.sessions[item.activity_session_id] = item.seq
    state.seq = max(state.seq, item.seq)
    if item.kind not in EVENT_KINDS:
        return state, rows
    if state.phase in TERMINAL:
        state.telemetry += 1  # a late hook event after the end is kept, never reopens the task
        return state, rows
    state.last_activity_at = now
    state.activity_session_id = state.activity_session_id or item.activity_session_id
    if item.kind == "RuntimeStarted" and state.phase in ("RECEIVED", "QUEUED", "WAITING"):
        state, more = reduce(state, LifecycleTransition(state.task_id, "RUNNING", ts=now, reason=item.text))
        rows += more
    elif item.kind == "InteractionRequired" and state.phase == "RUNNING":
        state, more = reduce(state, LifecycleTransition(state.task_id, "WAITING", ts=now, reason=item.text))
        rows += more
    elif item.kind in ("Working", "ToolStarted") and state.phase == "WAITING":
        state, more = reduce(state, LifecycleTransition(state.task_id, "RUNNING", ts=now))
        rows += more
    if item.kind in ("Working", "ToolStarted") and item.text:
        state.summary = item.text
        rows.append(_row(state, "working", item.text, now))
    elif item.kind == "Thinking" and item.text:
        rows.append(_row(state, "thinking", item.text, now))
    elif item.kind == "Status" and item.text:
        rows.append(_row(state, "notice", item.text, now))
    # Heartbeat, ToolFinished, RuntimeStopped: liveness only. A provider that stops is not a
    # failure; the scheduler decides FAILED, never an observation's absence.
    return state, rows


CANCEL_RE = re.compile(r"CANCEL_INSTRUCTION:\s*stop processing\s+(task-[A-Za-z0-9._-]+)")


def cancel_target(task_text: str | None) -> str | None:
    """The task a CANCEL_INSTRUCTION names, else None: the scheduler marks it CANCELLED on arrival."""
    m = CANCEL_RE.search(task_text or "")
    return m.group(1) if m else None


def transition_from_file(to_phase: str, task_file: Path, *, reason: str = "", into_task: str | None = None,
                         worker: str | None = None, ws: Path | None = None, ts: float | None = None) -> LifecycleTransition:
    task, room = task_from_file(task_file)
    into = None
    if into_task:
        for d in ("tasks", os.path.join("tasks", "archive")):
            p = (ws or resolve_workspace()) / d / f"{into_task}.txt"
            if p.exists():
                into = task_from_file(p)[0].get("event")
                break
    return LifecycleTransition(task["id"], to_phase, reason=reason, message_event_id=task.get("event"),
                               room=room, sender=task.get("from"), text=task.get("text"), worker=worker, into=into, ts=ts)


def main(argv: list[str] | None = None) -> int:
    """`transition <PHASE> (--task-file P | --task-id ID) [--reason R] [--into-task ID] [--worker W]`
    and `event <task_id> <kind> --session S --seq N [--text T]`. Exits 0 on every path: a caller in
    the delivery path must never fail because the card could not be updated."""
    import argparse
    ap = argparse.ArgumentParser(description="activity bus CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("transition"); t.add_argument("to_phase", choices=PHASES)
    t.add_argument("--task-file"); t.add_argument("--task-id"); t.add_argument("--reason", default="")
    t.add_argument("--into-task"); t.add_argument("--worker"); t.add_argument("--workspace")
    t.add_argument("--ts", type=float, default=None, help="when this transition happened (epoch); default now")
    e = sub.add_parser("event"); e.add_argument("task_id"); e.add_argument("kind", choices=EVENT_KINDS)
    e.add_argument("--session", required=True); e.add_argument("--seq", type=int, required=True)
    e.add_argument("--text", default=""); e.add_argument("--workspace")
    try:
        a = ap.parse_args(argv)
        ws = Path(a.workspace) if a.workspace else None
        store = ActivityStore(ws)
        if a.cmd == "transition":
            if a.task_file:
                item = transition_from_file(a.to_phase, Path(a.task_file), reason=a.reason, into_task=a.into_task,
                                            worker=a.worker, ws=ws, ts=a.ts)
            elif a.task_id:
                item = LifecycleTransition(a.task_id, a.to_phase, reason=a.reason, worker=a.worker, ts=a.ts)
            else:
                return 0
            state = store.apply(item)
        else:
            state = store.apply(RuntimeEvent(a.task_id, a.session, a.seq, a.kind, text=a.text))
        print(json.dumps({"task_id": state.task_id, "phase": state.phase, "generation": state.generation, "seq": state.seq}))
    except SystemExit:
        return 0
    except Exception as exc:  # noqa: BLE001 - never fail the delivery path
        print(f"activity_bus: {exc}", file=sys.stderr)
    return 0


class ActivityStore:
    """Snapshots under <workspace>/state/activity/<task_id>.json (atomic replace) and the row
    projection through the one row writer. Restart-safe: the snapshot IS the task."""

    def __init__(self, workspace: Path | None = None, project: Callable[[dict], None] | None = None):
        self.ws = workspace or resolve_workspace()
        self.dir = self.ws / "state" / "activity"
        self.project = project if project is not None else self._default_project

    def _default_project(self, row: dict) -> None:
        append_row(row["line"], kind=row["kind"], room=row["room"], task=row["task"], done=row["done"],
                   workspace=self.ws, scope=row.get("scope"), pid=row.get("pid"), ts=row.get("ts"), replay=int(row.get("attempts", 0)) > 1)

    def path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def load(self, task_id: str) -> TaskActivityState:
        try:
            d = json.loads(self.path(task_id).read_text(encoding="utf-8"))
            return TaskActivityState(**{k: v for k, v in d.items() if k in TaskActivityState.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return TaskActivityState(task_id=task_id)

    def save(self, state: TaskActivityState) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(state.task_id)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(asdict(state), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)

    def apply(self, item: LifecycleTransition | RuntimeEvent) -> TaskActivityState:
        """Load → drain → reduce → save (rows pending) → project → save (drained), all under one lock
        per task, so two emitters serialize their rows as well as their transitions. A projection
        that fails leaves its rows pending in the snapshot; the next apply on that task drains them
        first, so a row is projected exactly once, later, never lost and never twice."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / f".{item.task_id}.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            state = self.load(item.task_id)
            self._drain(state)
            state, rows = reduce(state, item)
            state.pending = list(state.pending) + rows
            self.save(state)  # the transition is committed with its rows still owed
            self._drain(state)
        return state

    def _drain(self, state: TaskActivityState) -> None:
        """Project every owed row in order; stop at the first failure and persist what is still owed."""
        while state.pending:
            row = state.pending[0]
            # The attempt is recorded and saved BEFORE projecting, so a retry knows it is a replay.
            row["attempts"] = int(row.get("attempts", 0)) + 1
            self.save(state)
            try:
                self.project(row)
            except Exception:  # noqa: BLE001 - the rows stay owed; the caller must not fail
                self.save(state)
                return
            state.pending = state.pending[1:]
            self.save(state)


if __name__ == "__main__":
    sys.exit(main())
