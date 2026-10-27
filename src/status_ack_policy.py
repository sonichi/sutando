"""Whether to send one task's initial status acknowledgement, what it says, and where it goes.

Scope: only a task no session has picked up and that shows no activity card or progress notice.
A picked-up task is the card's and the progress notifier's to report, so this layer stays silent.

Pure and dependency-light: the caller observes the task (headers, activity phase, result state,
inbox holders, ack-claim state) and this module only decides. It never reads the clock, a file
or the process table, so every requirement is checkable from a fixture. The deadline is fixed
from the task's intake time; no later observation can extend it. A destination that cannot be
verified from the task's own headers suppresses the ack: there is no fallback room or thread.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

try:
    from . import task_priority as _tp
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import task_priority as _tp  # noqa: E402

#: Seconds from intake to the first ack, per task priority. A skill's manifest config overrides these.
DEFAULT_FIRST_ACK_DEADLINE_S: Mapping[str, float] = {"urgent": 30.0, "normal": 120.0, "low": 600.0}

SPEAK, WAIT, SUPPRESS = "speak", "wait", "suppress"

#: The only lines this policy can emit. Each states an observed fact; none estimates time or progress.
TEMPLATES: Mapping[str, str] = {
    "queued": "Received. Not picked up yet; it is in the queue.",
    "inbox_down": "Received. Not picked up yet: the core is not reading its inbox right now.",
    "inbox_down_owner_told": ("Received. Not picked up yet: the core is not reading its inbox right now. "
                              "The owner has been told."),
    "unclaimed": "Received. Not picked up yet.",
}

#: Result states the caller may report. Anything but "none" means the task already has its outcome.
RESULT_STATES = ("none", "answered", "no-send", "sending", "archived")
#: Ack-claim states. Only "none" may speak; "unknown" is a parked send and is never retried here.
ACK_STATES = ("none", "claimed", "confirmed", "unknown")

PHASES = ("RECEIVED", "QUEUED", "RUNNING", "WAITING", "COMPLETED", "FAILED", "CANCELLED")
_UNCLAIMED_PHASES = frozenset({"RECEIVED", "QUEUED"})
_TERMINAL_PHASES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

_ROOM_ID = re.compile(r"^![^\s:]+:\S+\Z")
_EVENT_ID = re.compile(r"^\$\S+\Z")
# Minted ids are task-<ms>, task-chat-<ms>, task-<slug>-<ms>; a digit run inside a hex id is not a stamp.
_ID_EPOCH = re.compile(r"\Atask-(?:[a-z]+-)*([0-9]{13}|[0-9]{10})(?=[-.]|\Z)")
_EPOCH_MIN, _EPOCH_MAX = 1_500_000_000.0, 4_100_000_000.0


def ack_key(task_id: str) -> str:
    """The one arbitration key for a task's initial ack: whoever claims it first is the only sender."""
    return f"ack:{task_id}:initial"


def _plausible(ts: Optional[float]) -> Optional[float]:
    return ts if ts is not None and _EPOCH_MIN <= ts <= _EPOCH_MAX else None


def _header_epoch(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _plausible(dt.timestamp())


def _id_epoch(task_id: str) -> Optional[float]:
    m = _ID_EPOCH.match(task_id or "")
    if not m:
        return None
    raw = m.group(1)
    return _plausible(int(raw) / 1000.0 if len(raw) == 13 else float(raw))


def intake_at(task_id: str, headers: Mapping[str, str], first_seen: Optional[float] = None) -> Optional[float]:
    """The earliest immutable intake anchor: the `timestamp` header, the id's epoch, or a first-seen
    stamp the caller has persisted. None when there is none; a file mtime is never an anchor."""
    stamps = [_header_epoch(headers.get("timestamp")), _id_epoch(task_id)]
    if isinstance(first_seen, (int, float)) and not isinstance(first_seen, bool):
        stamps.append(_plausible(float(first_seen)))
    stamps = [s for s in stamps if s is not None]
    return min(stamps) if stamps else None


def priority_of(headers: Mapping[str, str]) -> str:
    """The task's own `priority:` header when valid, else the source's default."""
    declared = (headers.get("priority") or "").strip().lower()
    if _tp.is_valid_priority(declared):
        return declared
    return _tp.default_priority_for_source(headers.get("source") or "", headers.get("access_tier"))


def deadline_for(headers: Mapping[str, str], deadlines: Optional[Mapping[str, float]] = None) -> float:
    table = dict(DEFAULT_FIRST_ACK_DEADLINE_S)
    table.update(deadlines or {})
    return float(table[priority_of(headers)])


def inbox_state(observed: bool, roles: Sequence[str], undecided: int = 0) -> str:
    """`session` when a session watcher holds the inbox; `standby_only` when only non-session
    holders do; `empty` when none does; `unknown` when the table was unread or a line undecided."""
    if not observed:
        return "unknown"
    if "session" in roles:
        return "session"
    if undecided:
        return "unknown"
    return "standby_only" if roles else "empty"


@dataclass(frozen=True)
class Destination:
    room: str
    thread_root: str
    reply_to: str


def destination(headers: Mapping[str, str]) -> Tuple[Optional[Destination], str]:
    """The task's own room and thread, or (None, why). An in-thread request keeps its thread root;
    only a top-level request is rooted at its source message. A malformed field never falls back."""
    rooms = {r.strip() for r in (headers.get("channel_id"), headers.get("source_room_id")) if r and r.strip()}
    if len(rooms) != 1:
        return None, "no room" if not rooms else "room headers disagree"
    room = rooms.pop()
    if not _ROOM_ID.match(room):
        return None, "room id is not verifiable"
    source_msg = (headers.get("source_message_id") or "").strip()
    if not _EVENT_ID.match(source_msg):
        return None, "source message id is not verifiable"
    raw_root = headers.get("thread_root")
    if raw_root is not None and raw_root.strip():
        root = raw_root.strip()
        if not _EVENT_ID.match(root):
            return None, "thread root is not verifiable"
        return Destination(room, root, source_msg), ""
    if raw_root is not None:
        return None, "thread root header is empty"
    return Destination(room, source_msg, source_msg), ""


@dataclass(frozen=True)
class TaskObservation:
    """What the caller saw for one task. `started_at` / `last_activity_at` mirror the activity
    snapshot and are deliberately not used for the deadline or the wording."""
    task_id: str
    headers: Mapping[str, str]
    intake_at: Optional[float]
    phase: Optional[str] = None
    result: str = "none"
    ack: str = "none"
    worker_holder: Optional[str] = None
    inbox: str = "unknown"
    cancel_instruction: bool = False
    cancel_requested: bool = False
    owner_alerted: bool = False
    card: bool = False
    progress_notice: bool = False
    started_at: Optional[float] = None
    last_activity_at: Optional[float] = None


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    key: str
    due_at: Optional[float] = None
    template: Optional[str] = None
    text: Optional[str] = None
    destination: Optional[Destination] = None
    watcher_down: bool = False
    facts: Mapping[str, str] = field(default_factory=dict)


def decide(obs: TaskObservation, now: float, deadlines: Optional[Mapping[str, float]] = None) -> Decision:
    """One task's initial-ack decision at `now`. Fails closed: any state it does not recognise
    suppresses, because a wrong status line is worse than a late one."""
    key = ack_key(obs.task_id)

    def no(reason: str, due: Optional[float] = None) -> Decision:
        return Decision(SUPPRESS, reason, key, due_at=due)

    if obs.cancel_instruction:
        return no("a cancel instruction is not a request")
    if obs.cancel_requested:
        return no("cancelled")
    if obs.result not in RESULT_STATES:
        return no(f"unrecognised result state {obs.result!r}")
    if obs.result != "none":
        return no(f"terminal: {obs.result}")
    if obs.phase is not None and obs.phase not in PHASES:
        return no(f"unrecognised phase {obs.phase!r}")
    if obs.phase in _TERMINAL_PHASES:
        return no(f"terminal: {obs.phase}")
    if obs.phase == "WAITING":
        return no("waiting on a person")
    if obs.ack not in ACK_STATES:
        return no(f"unrecognised ack state {obs.ack!r}")
    if obs.ack != "none":
        return no(f"initial ack already {obs.ack}")
    if obs.phase == "RUNNING" or obs.worker_holder is not None:
        return no("picked up: the card and the progress notifier report it")
    if obs.card:
        return no("an activity card already shows this task")
    if obs.progress_notice:
        return no("a progress notice already went out")
    dest, why = destination(obs.headers)
    if dest is None:
        return no(f"unverified destination: {why}")
    if obs.intake_at is None:
        return no("intake time unknown")
    due = obs.intake_at + deadline_for(obs.headers, deadlines)
    if now < due:
        return Decision(WAIT, "before the first-ack deadline", key, due_at=due, destination=dest)

    watcher_down = obs.inbox == "standby_only"
    if watcher_down:
        template = "inbox_down_owner_told" if obs.owner_alerted else "inbox_down"
    elif obs.inbox == "session" and obs.phase in _UNCLAIMED_PHASES:
        template = "queued"
    else:
        template = "unclaimed"
    facts = {"phase": obs.phase or "none", "inbox": obs.inbox, "priority": priority_of(obs.headers)}
    return Decision(SPEAK, "past the first-ack deadline with no answer", key, due_at=due, template=template,
                    text=TEMPLATES[template], destination=dest, watcher_down=watcher_down, facts=facts)
