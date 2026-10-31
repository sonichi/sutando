"""Per-invocation turn context and the 3-outcome turn gate for relay tasks.

A turn is one attempt at one task: `turn_id = <task_id>_g<execution_generation>_<ulid>`.
Its context lives in an immutable `<state>/turns/<turn_id>.json` (mode 0400) that the
MCP proxy reads to stamp `operation_id = op_<turn_id>_<seq>` on every mutating call;
the proxy journals each call to `<state>/room-actions.jsonl` as a `dispatch_intent`
before the HTTP request and a `dispatch_outcome` after it.

At turn exit `classify_turn` reads those records and decides:
  terminal_confirmed   — a succeeded operation applied a terminal task state;
  durably_suspended    — the latest applied state is waiting_input or input_ready, or
                         has an open blocker, or a terminal-intent operation is still
                         running (202);
  incomplete_or_error  — anything else. The plain-text answer may be sent as the
                         reply ONLY when the journal is readable, holds no intent for
                         the turn nor any unbound intent since it began, and core-api
                         confirms zero operations for the turn.

`result_file_decision` applies the gate to Sutando's result file, which the agent writes
on purpose: it is posted unless the task is already closed or its text was already sent.

Stdlib only, so the ag2-sparrow package can vendor it byte-identical.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = 1
TURN_CONTEXT_ENV = "AG2_TURN_CONTEXT_FILE"
TURNS_DIRNAME = "turns"
ROOM_ACTIONS_NAME = "room-actions.jsonl"

TERMINAL_CONFIRMED = "terminal_confirmed"
DURABLY_SUSPENDED = "durably_suspended"
INCOMPLETE_OR_ERROR = "incomplete_or_error"

INTENT = "dispatch_intent"
OUTCOME = "dispatch_outcome"

TERMINAL_PHASES = frozenset({"responded", "suppressed", "consolidated", "completed",
                             "no_action_needed", "failed", "cancelled", "dead"})
TERMINAL_STATUSES = frozenset({"done", "no_action", "consolidated", "failed", "cancelled"})
TERMINAL_INTENT_STATUSES = frozenset({"done", "no_action"})
ASYNC_STATUSES = frozenset({"accepted", "running"})
SUSPENDED_PHASES = frozenset({"waiting_input", "input_ready"})

_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_TURN_RE = re.compile(r"[A-Za-z0-9._-]{1,160}")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid(now_ms: int | None = None) -> str:
    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    n = (ms << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def new_turn_id(task_id: str, execution_generation: int, now_ms: int | None = None) -> str:
    if not _ID_RE.fullmatch(str(task_id)):
        raise ValueError(f"invalid task_id: {task_id!r}")
    gen = int(execution_generation)
    if gen < 1:
        raise ValueError(f"execution_generation must be >= 1, got {gen}")
    return f"{task_id}_g{gen}_{_ulid(now_ms)}"


def turns_dir(state_dir: Path) -> Path:
    return Path(state_dir) / TURNS_DIRNAME


def room_actions_path(state_dir: Path) -> Path:
    return Path(state_dir) / ROOM_ACTIONS_NAME


def _turn_path(state_dir: Path, turn_id: str, suffix: str) -> Path:
    if not _TURN_RE.fullmatch(str(turn_id)):
        raise ValueError(f"invalid turn_id: {turn_id!r}")
    return turns_dir(state_dir) / f"{turn_id}{suffix}"


def build_turn_context(*, task_id: str, execution_generation: int, room_id: str | None = None,
                       thread_root_id: str | None = None, source_event_id: str | None = None,
                       agent_mxid: str | None = None, worker_id: str | None = None,
                       source_kind: str | None = None, state_dir: Path,
                       now_ms: int | None = None) -> dict:
    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return {
        "schema_version": SCHEMA_VERSION,
        "turn_id": new_turn_id(task_id, execution_generation, ms),
        "task_id": str(task_id),
        "execution_generation": int(execution_generation),
        "worker_id": worker_id,
        "room_id": room_id,
        "thread_root_id": thread_root_id,
        "source_event_id": source_event_id,
        "agent_mxid": agent_mxid,
        "source_kind": source_kind,
        "created_at_ms": ms,
        "issued_at": ms / 1000,
        "room_actions_file": str(room_actions_path(state_dir).resolve()),
    }


def write_turn_context(state_dir: Path, context: dict) -> Path:
    """Publish the context read-only; refuses to replace an existing turn file."""
    path = _turn_path(state_dir, context["turn_id"], ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(context, fh, sort_keys=True)
            fh.flush()
            os.fchmod(fh.fileno(), 0o400)
            os.fsync(fh.fileno())
        # link() fails on an existing name where rename() would silently replace it
        os.link(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    _fsync_dir(path.parent)
    return path


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def child_env(context_path: Path, base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    env[TURN_CONTEXT_ENV] = str(Path(context_path).resolve())
    return env


def read_turn_context(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("turn_id") else None


def is_closed(state_dir: Path, turn_id: str) -> bool:
    return _turn_path(state_dir, turn_id, ".closed").exists()


def open_turn_for_task(state_dir: Path, task_id: str) -> Path | None:
    """The unclosed turn file for a task with the largest `created_at_ms`, as the proxy picks it."""
    if not _ID_RE.fullmatch(str(task_id)):
        return None
    best: tuple[int, str, Path] | None = None
    for p in turns_dir(state_dir).glob(f"{task_id}_g*_*.json"):
        if not re.fullmatch(re.escape(task_id) + r"_g\d+_[0-9A-Z]{26}", p.stem) \
                or is_closed(state_dir, p.stem):
            continue
        ctx = read_turn_context(p)
        if not ctx or ctx.get("task_id") != task_id or not isinstance(ctx.get("created_at_ms"), int):
            continue
        key = (ctx["created_at_ms"], p.stem, p)
        if best is None or key[:2] > best[:2]:
            best = key
    return best[2] if best else None


def open_turns(state_dir: Path) -> list:
    """Every turn file without a `.closed` sibling."""
    return [p for p in turns_dir(state_dir).glob("*.json")
            if _TURN_RE.fullmatch(p.stem) and not is_closed(state_dir, p.stem)]


JOURNAL_LOCK_WAIT_S = 2.0
_LOCK_POLL_S = 0.02


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _take_journal_lock(state_dir: Path, wait_s: "float | None" = None) -> "tuple[Path, str] | None":
    """The proxy's journal lock: an O_EXCL file holding "<pid> <token> <start_ms>". A holder whose
    pid is dead is broken; a live or half-written one is waited on. None when the wait runs out."""
    lock = room_actions_path(state_dir).with_name(ROOM_ACTIONS_NAME + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    body = f"{os.getpid()} {secrets.token_hex(8)} {int(time.time() * 1000)}"
    deadline = time.monotonic() + (JOURNAL_LOCK_WAIT_S if wait_s is None else wait_s)
    while True:
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                held = lock.read_text().split()
            except FileNotFoundError:
                continue
            except OSError:
                held = []
            if len(held) == 3 and held[0].isdigit() and not _pid_alive(int(held[0])):
                _unlink_if_holding(lock, " ".join(held))
                continue
            if time.monotonic() >= deadline:
                return None
            time.sleep(_LOCK_POLL_S)
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        return lock, body


def _unlink_if_holding(lock: Path, body: str) -> None:
    """Remove the lock only while it still holds `body`: another holder's lock is never removed."""
    try:
        if lock.read_text().split() == body.split():
            lock.unlink()
    except FileNotFoundError:
        pass


def _release_journal_lock(held: "tuple[Path, str] | None") -> None:
    if held is not None:
        _unlink_if_holding(*held)


def close_turn(state_dir: Path, turn_id: str) -> bool:
    """Fence the turn: later calls carrying it are refused, and only its own file goes. Only under
    the proxy's journal lock, so a close never lands between the proxy's check and its intent
    append. False, with nothing changed, when the lock was not had in time: the turn stays open
    (unfenced) until a later close_turn, retried from its still-open file, lands."""
    closed = _turn_path(state_dir, turn_id, ".closed")
    closed.parent.mkdir(parents=True, exist_ok=True)
    held = _take_journal_lock(state_dir)
    if held is None:
        return False
    try:
        fd = os.open(closed, os.O_WRONLY | os.O_CREAT, 0o400)
        os.close(fd)
        try:
            _turn_path(state_dir, turn_id, ".json").unlink()
        except FileNotFoundError:
            pass
    finally:
        _release_journal_lock(held)
    return True


@dataclass(frozen=True)
class TurnRecords:
    intents: list
    outcomes: list
    readable: bool
    unbound_intents: tuple = ()
    partial: bool = False
    # room.message.send intents of the same task's other turns (an earlier generation's reply).
    sibling_sends: tuple = ()


# An unterminated last line younger than this is an append in progress, not corruption.
PARTIAL_LINE_GRACE_S = 30.0


def read_turn_records(state_dir: Path, turn_id: str, issued_at: float | None = None,
                      task_id: str | None = None) -> TurnRecords:
    """Both record types for one turn across the rotated and live journal. Records the proxy
    could not bind, written since `issued_at` (None: all), count as this turn's when they name
    `task_id`; other unbound intents are kept apart. A fresh unterminated last line marks the
    journal `partial`. Any other line that does not parse makes it unreadable for this turn only
    when the line names this turn or task; the journal is shared, so another task's torn line is skipped,
    and so is one torn before its ids (nothing was dispatched for it). `sibling_sends` are the same
    task's other turns' room.message.send intents whose outcome is not a failure."""
    path = room_actions_path(state_dir)
    intents, outcomes, unbound, readable, partial = [], [], [], True, False
    siblings: list = []
    sibling_failed: set = set()
    sibling_prefix = f"{task_id}_g" if task_id else None
    for p in (path.with_name(path.name + ".1"), path):
        try:
            text = p.read_text()
            age = time.time() - p.stat().st_mtime
        except FileNotFoundError:
            continue
        except OSError:
            readable = False
            continue
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                rec = None
            if not isinstance(rec, dict):
                if p == path and i == len(lines) - 1 and age < PARTIAL_LINE_GRACE_S:
                    partial = True
                elif (turn_id and turn_id in line) or (task_id and task_id in line):
                    readable = False
                continue
            if rec.get("turn_id") is None:
                ts = rec.get("ts")
                if issued_at is not None and isinstance(ts, (int, float)) and ts < issued_at:
                    continue
                if task_id is None or rec.get("task_id") != task_id:
                    if rec.get("record_type") == INTENT:
                        unbound.append(rec)
                    continue
            elif rec.get("turn_id") != turn_id:
                if sibling_prefix and str(rec.get("turn_id")).startswith(sibling_prefix):
                    if rec.get("record_type") == INTENT and rec.get("action") == "room.message.send":
                        siblings.append(rec)
                    elif rec.get("record_type") == OUTCOME and rec.get("operation_status") not in (
                            None, "succeeded", "dispatched_unknown"):
                        sibling_failed.add(rec.get("operation_id"))
                continue
            if rec.get("record_type") == INTENT:
                intents.append(rec)
            elif rec.get("record_type") == OUTCOME:
                outcomes.append(rec)
    # A send whose outcome failed delivered nothing; one with no outcome yet may have been sent.
    sent = tuple(s for s in siblings if s.get("operation_id") not in sibling_failed)
    return TurnRecords(intents, outcomes, readable, tuple(unbound), partial, sent)


def unmatched_intents(records: TurnRecords) -> list:
    """Intents whose server outcome is unknown and must be reconciled with core-api."""
    known = {o.get("operation_id") for o in records.outcomes
             if o.get("operation_status") not in (None, "dispatched_unknown")}
    return [i for i in records.intents if i.get("operation_id") not in known]


@dataclass(frozen=True)
class TurnVerdict:
    outcome: str
    may_send_stdout: bool
    reason: str
    intent_count: int


def _applied_terminal(o: dict) -> bool:
    if "applied_terminal" in o:
        return o.get("applied_terminal") is True
    if o.get("applied_execution_phase") is not None:
        return o.get("applied_execution_phase") in TERMINAL_PHASES
    return o.get("effective_task_status") in TERMINAL_STATUSES


def _seq(rec: dict) -> int:
    try:
        return int(rec.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def classify_turn(records: TurnRecords, *, stdout: str = "", reconciled: Iterable[dict] = (),
                  server_state: dict | None = None,
                  server_operation_count: int | None = None) -> TurnVerdict:
    """`reconciled`: core-api outcomes for unmatched intents. `server_state`: the task row.
    `server_operation_count`: core-api operations for (task_id, turn_id); None is unknown."""
    by_op: dict = {}
    for o in sorted(records.outcomes, key=_seq):
        by_op[o.get("operation_id")] = o
    intent_by_op = {i.get("operation_id"): i for i in records.intents}
    for r in reconciled:
        op = r.get("operation_id")
        merged = {**intent_by_op.get(op, {}), **by_op.get(op, {}), **r}
        merged["record_type"] = OUTCOME
        by_op[op] = merged
    outcomes = sorted(by_op.values(), key=_seq)
    n = len(records.intents)

    if any(o.get("operation_status") == "succeeded" and _applied_terminal(o) for o in outcomes):
        return TurnVerdict(TERMINAL_CONFIRMED, False, "terminal operation succeeded", n)

    latest = outcomes[-1] if outcomes else {}
    if server_state is not None:
        terminal, phase = server_state.get("terminal") is True, server_state.get("execution_phase")
        blockers = server_state.get("active_blocker_count")
    else:
        terminal, phase = _applied_terminal(latest), latest.get("applied_execution_phase")
        blockers = latest.get("active_blocker_count")
    if not terminal and (phase in SUSPENDED_PHASES or int(blockers or 0) > 0):
        return TurnVerdict(DURABLY_SUSPENDED, False, "open HITL blocker", n)

    terminal_intents = [o for o in outcomes
                        if o.get("requested_task_status") in TERMINAL_INTENT_STATUSES]
    if terminal_intents and terminal_intents[-1].get("operation_status") in ASYNC_STATUSES:
        return TurnVerdict(DURABLY_SUSPENDED, False, "terminal operation still running", n)

    if terminal and server_state is not None:
        return TurnVerdict(INCOMPLETE_OR_ERROR, False, "task already closed on server", n)
    if not records.readable or records.partial:
        return TurnVerdict(INCOMPLETE_OR_ERROR, False, "journal unreadable", n)
    if n:
        return TurnVerdict(INCOMPLETE_OR_ERROR, False, "room actions attempted without a terminal", n)
    if records.unbound_intents:
        return TurnVerdict(INCOMPLETE_OR_ERROR, False, "unbound room actions during the turn", n)
    if server_operation_count != 0:
        return TurnVerdict(INCOMPLETE_OR_ERROR, False, "server operations not confirmed absent", n)
    if not stdout.strip():
        return TurnVerdict(INCOMPLETE_OR_ERROR, False, "no output", n)
    return TurnVerdict(INCOMPLETE_OR_ERROR, True, "text-only turn", n)


SETTLE = "settle"
POST = "post"
DEFER = "defer"


@dataclass(frozen=True)
class ResultDecision:
    action: str
    fields: dict
    reason: str


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.strip().encode("utf-8")).hexdigest()


def result_file_decision(records: TurnRecords, body: str, *, no_send: bool = False,
                         reconciled: Iterable[dict] = (),
                         server_state: dict | None = None) -> ResultDecision:
    """What to do with a result file the agent wrote on purpose (not inferred stdout):
    post it unless the task is already closed or the same text was sent as a room message, in this
    turn or an earlier one of the same task (a re-serve under a new generation)."""
    verdict = classify_turn(records, stdout=body, reconciled=reconciled, server_state=server_state)
    if verdict.outcome == TERMINAL_CONFIRMED or (server_state or {}).get("terminal") is True:
        return ResultDecision(SETTLE, {}, verdict.reason if verdict.outcome == TERMINAL_CONFIRMED
                              else "task already closed on server")
    if records.partial:
        return ResultDecision(DEFER, {}, "a journal line is still being written")
    if verdict.outcome == DURABLY_SUSPENDED:
        return ResultDecision(POST, {"status": "waiting_input", "no_send": True}, verdict.reason)
    asks = {i.get("operation_id") for i in records.intents if i.get("action") == "room.question.ask"}
    status = {o.get("operation_id"): o.get("operation_status") for o in (*records.outcomes, *reconciled)}
    if any(status.get(op) not in (None, "dispatched_unknown", "succeeded", *ASYNC_STATUSES) for op in asks):
        # The note says "I asked X", but no question exists: never post it as the reply.
        return ResultDecision(POST, {"status": "failed", "no_send": True}, "the question could not be asked")
    if any(status.get(op) in (None, "dispatched_unknown") for op in asks):
        # Its outcome is unknown: never the final reply. A broker with no open question refuses it.
        return ResultDecision(POST, {"status": "waiting_input", "no_send": True},
                              "a question was asked and its outcome is unknown")
    if no_send:
        return ResultDecision(POST, {"status": "no_action_needed"}, "result marked no-send")
    if not records.readable:
        return ResultDecision(POST, {"status": "failed", "no_send": True}, "journal unreadable")
    digest = body_sha256(body) if body.strip() else None
    if digest and any(i.get("action") == "room.message.send" and i.get("body_sha256") == digest
                      for i in (*records.intents, *records.unbound_intents, *records.sibling_sends)):
        return ResultDecision(POST, {"status": "completed", "no_send": True},
                              "the same text was already sent as a room message")
    return ResultDecision(POST, {"status": "completed"}, "deliberate result file")
