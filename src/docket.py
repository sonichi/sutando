"""The docket: matters awaiting action, and the conditions that say when.

A docket entry is one durable TODO an agent can pick up later.

The owner asked for a one-shot reminder that survives a restart. Session crons
do not: they live in the running session and die with it. The per-host crons
file is durable but recurring, so neither fits "do this once, when the moment
is right".

Two decisions the shape encodes, both hers:

* The TODO's *content* lives in the room document, not here. A record carries where
  to find it and how to open it, and the text stays somewhere she can edit.
  `how_to_open` is written into the record on purpose — the agent that picks
  the item up weeks later is not the one that filed it and will not remember
  which skill opens a room document.
* Filing requires enough to pick the work up cold: a one-line brief, whether it
  must happen or would merely be nice, how urgent it is, and a size. A record
  missing any of those is refused rather than stored, because a TODO nobody can
  act on is worse than no TODO.

A record always names the ROOM, and names a WORKER only when the filer means a
particular one. `state/bindings.json` maps a room to one addressee today, so an
unaddressed TODO resolves through it — but the owner's point stands that a room
may host several workers later, and then "who should pick this up" is a fact
only the filer knows. So `assignee` is optional: absent means whoever the room
resolves to, present means that seat, and the two can never silently disagree
because an absent field makes no claim.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402

IMPORTANCE = ("must", "should", "nice")
URGENCY = ("now", "soon", "whenever")
SIZES = ("S", "M", "L", "XL")
STATES = ("open", "done", "cancelled")
# The owner's lifecycle. `state` stays the coarse open/done/cancelled a reader
# filters on; `status` is where an open item actually stands.
STATUSES = ("proposed", "approved", "blocked", "in_progress", "completion_declared", "confirmed")
# Only an approved item may be picked up — the others are waiting on someone.
PICKUP_STATUSES = ("approved",)
# The two that need the owner herself, so nothing rots unnoticed at "proposed".
AWAITING_OWNER = ("proposed", "completion_declared")
# Conditions the proactive loop can evaluate from signals it already computes.
CONDITIONS = ("idle", "owner_away", "credit_full", "credit_medium_or_better", "credit_for_size")

# What each tier will start: this is what makes "enough credit" mean something.
TIER_SIZES = {
    "FULL": ("S", "M", "L", "XL"),
    "MEDIUM": ("S", "M", "L"),
    "LIGHT": ("S", "M"),
    "MINIMAL": ("S",),
}


def fits_tier(size: str, tier: str | None) -> bool:
    """Whether an item of this size is worth starting on this much credit.
    An unknown tier is treated as FULL: refusing everything because a signal is
    missing would be a quiet stop, and a quiet stop is worse than a big item."""
    allowed = TIER_SIZES.get((tier or "FULL").upper(), TIER_SIZES["FULL"])
    return size in allowed


def canonical_how_to_open(room_id: str, surface: str = "markdown", mode: str = "once") -> str:
    """The note the pickup follows, in the two shapes the room-collab skill has:
    a one-shot subcommand, or the client held open while collaborating. Built
    here so a filer does not have to remember either spelling."""
    if mode == "hold":
        return (
            "room-collab skill: import room_collab_client and hold the connection open "
            f"(surface {surface}, room {room_id}) so presence and the caret stay visible"
        )
    return f"room-collab skill: scripts/room_collab.py --kind {surface} read '{room_id}'"

REQUIRED = ("room_id", "note", "how_to_open", "brief", "importance", "urgency", "size")
# Optional, and free-form on purpose: a seat id, an mxid, or a name the roster
# knows. Validated as non-empty if given, never invented.
OPTIONAL_TEXT = ("assignee", "context", "surface")


def store_path(workspace: Path | None = None) -> Path:
    return (workspace or resolve_workspace()) / "state" / "docket.json"


def _read(path: Path) -> list[dict]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = d.get("todos") if isinstance(d, dict) else d
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _write_atomic(path: Path, rows: list[dict]) -> None:
    """Temp + os.replace, like every other state writer here: a reader polling
    mid-write must see the old file whole, never a truncated one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"v": 1, "todos": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def validate(record: dict) -> list[str]:
    """Everything wrong with a record, in one pass — a filer fixes all of it at
    once instead of discovering the next missing field on the next attempt."""
    problems: list[str] = []
    for field in REQUIRED:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{field} is required and must be a non-empty string")
    if record.get("importance") not in IMPORTANCE and isinstance(record.get("importance"), str):
        problems.append(f"importance must be one of {', '.join(IMPORTANCE)}")
    if record.get("urgency") not in URGENCY and isinstance(record.get("urgency"), str):
        problems.append(f"urgency must be one of {', '.join(URGENCY)}")
    if record.get("size") not in SIZES and isinstance(record.get("size"), str):
        problems.append(f"size must be one of {', '.join(SIZES)}")
    brief = record.get("brief")
    if isinstance(brief, str) and "\n" in brief:
        problems.append("brief is one line; put the detail in the room document")
    when = record.get("when", [])
    if not isinstance(when, list) or any(w not in CONDITIONS for w in when):
        problems.append(f"when must be a list drawn from {', '.join(CONDITIONS)}")
    for stamp in ("due_at", "not_before"):
        value = record.get(stamp)
        if value is not None and not isinstance(value, (int, float)):
            problems.append(f"{stamp} is a unix timestamp or null")
    state = record.get("state", "open")
    if state not in STATES:
        problems.append(f"state must be one of {', '.join(STATES)}")
    status = record.get("status", "proposed")
    if status not in STATUSES:
        problems.append(f"status must be one of {', '.join(STATUSES)}")
    note = record.get("blocked_note")
    if status == "blocked" and not (isinstance(note, str) and note.strip()):
        problems.append("a blocked item needs blocked_note saying what it is waiting on")
    if note is not None and not isinstance(note, str):
        problems.append("blocked_note is free text")
    for field in OPTIONAL_TEXT:
        value = record.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            problems.append(f"{field} is optional, but not blank — leave it out instead")
    return problems


def add(record: dict, workspace: Path | None = None, now: float | None = None) -> dict:
    """Store a validated record. Raises ValueError listing every problem, so an
    agent filing a sloppy TODO is told what a usable one needs."""
    problems = validate(record)
    if problems:
        raise ValueError("; ".join(problems))
    path = store_path(workspace)
    rows = _read(path)
    row = dict(record)
    row.setdefault("id", f"todo-{uuid.uuid4().hex[:12]}")
    row.setdefault("created_at", now if now is not None else time.time())
    row.setdefault("state", "open")
    # Proposed, not approved: an agent may file anything, but the owner decides
    # what gets worked on. awaiting_owner() is what stops that being a silent hold.
    row.setdefault("status", "proposed")
    row.setdefault("when", [])
    row.setdefault("due_at", None)
    row.setdefault("not_before", None)
    # Absent, not guessed: an unaddressed TODO is for whoever the room resolves to.
    row.setdefault("assignee", None)
    rows = [r for r in rows if r.get("id") != row["id"]] + [row]
    _write_atomic(path, rows)
    return row


def close(todo_id: str, state: str = "done", workspace: Path | None = None) -> bool:
    """Mark one item done or cancelled; returns whether it was there to mark."""
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}")
    path = store_path(workspace)
    rows = _read(path)
    hit = False
    for row in rows:
        if row.get("id") == todo_id:
            row["state"], row["closed_at"], hit = state, time.time(), True
    if hit:
        _write_atomic(path, rows)
    return hit


def set_status(todo_id: str, status: str, note: str | None = None,
               workspace: Path | None = None) -> bool:
    """Move one item along the lifecycle; returns whether it was there to move.
    Confirming it also closes it, so `status` and `state` cannot disagree."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if status == "blocked" and not (note or "").strip():
        raise ValueError("a blocked item needs a note saying what it is waiting on")
    path = store_path(workspace)
    rows = _read(path)
    hit = False
    for row in rows:
        if row.get("id") != todo_id:
            continue
        row["status"], row["status_at"], hit = status, time.time(), True
        if note is not None:
            row["blocked_note"] = note
        if status == "confirmed":
            row["state"], row["closed_at"] = "done", time.time()
    if hit:
        _write_atomic(path, rows)
    return hit


def awaiting_owner(workspace: Path | None = None) -> list[dict]:
    """The open items that need her: newly proposed, or declared complete and
    waiting to be confirmed. Without this, `proposed` is where TODOs go to die."""
    return [r for r in load(workspace) if r.get("status", "proposed") in AWAITING_OWNER]


def load(workspace: Path | None = None, state: str | None = "open") -> list[dict]:
    rows = _read(store_path(workspace))
    return [r for r in rows if state is None or r.get("state", "open") == state]


def conditions_met(record: dict, signals: dict) -> bool:
    """Every condition must hold — she said they stack, and a TODO that fires on
    ANY of them would wake the agent at the wrong moment as often as the right.

    `credit_for_size` is the one condition read from the record as well as the
    signals: how much credit is enough depends on how big the item is."""
    for name in record.get("when", []):
        if name == "credit_for_size":
            if not fits_tier(str(record.get("size", "")), signals.get("tier")):
                return False
        elif not signals.get(name):
            return False
    return True


def is_ready(record: dict, now: float, signals: dict) -> bool:
    """Ready when nothing holds it back: not before its floor, past its deadline
    OR with its conditions met. A deadline overrides the conditions — that is
    what makes it a deadline."""
    if record.get("state", "open") != "open":
        return False
    if record.get("status", "proposed") not in PICKUP_STATUSES:
        return False
    not_before = record.get("not_before")
    if isinstance(not_before, (int, float)) and now < not_before:
        return False
    due_at = record.get("due_at")
    if isinstance(due_at, (int, float)) and now >= due_at:
        return True
    return conditions_met(record, signals)


# Most pressing first: a deadline that has passed, then importance, then urgency.
_IMPORTANCE_RANK = {name: i for i, name in enumerate(IMPORTANCE)}
_URGENCY_RANK = {name: i for i, name in enumerate(URGENCY)}


def ready(workspace: Path | None = None, now: float | None = None, signals: dict | None = None) -> list[dict]:
    at = now if now is not None else time.time()
    sig = signals or {}
    rows = [r for r in load(workspace) if is_ready(r, at, sig)]
    return sorted(
        rows,
        key=lambda r: (
            0 if isinstance(r.get("due_at"), (int, float)) and at >= r["due_at"] else 1,
            _IMPORTANCE_RANK.get(r.get("importance"), len(IMPORTANCE)),
            _URGENCY_RANK.get(r.get("urgency"), len(URGENCY)),
            r.get("created_at", 0),
        ),
    )


def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="docket", description=__doc__)
    # Explicit, because $SUTANDO_WORKSPACE is not honoured any more (#1440): a
    # test that could not name its own workspace wrote into the real one.
    parser.add_argument("--workspace", default=None, help="workspace root; default is the resolved one")
    sub = parser.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="file a TODO (every discipline field required)")
    a.add_argument("--room", required=True, help="room id the TODO note lives in")
    a.add_argument("--note", required=True, help="where in the doc: a heading or the line's text")
    a.add_argument(
        "--how-to-open",
        default=None,
        help="how the pickup opens the doc; omitted means the canonical room-collab note",
    )
    a.add_argument(
        "--pickup-mode",
        choices=("once", "hold"),
        default="once",
        help="once: one subcommand and close. hold: keep the client open while collaborating",
    )
    a.add_argument("--brief", required=True, help="one line: what this is")
    a.add_argument("--importance", required=True, choices=IMPORTANCE)
    a.add_argument("--urgency", required=True, choices=URGENCY)
    a.add_argument("--size", required=True, choices=SIZES)
    a.add_argument("--surface", default="markdown")
    a.add_argument("--due-at", type=float, default=None, help="unix ts; omit when there is no deadline")
    a.add_argument("--not-before", type=float, default=None)
    a.add_argument("--when", action="append", default=[], choices=CONDITIONS)
    a.add_argument("--context", default=None, help="anything the pickup needs that the doc lacks")
    a.add_argument("--status", default="proposed", choices=STATUSES,
                   help="lifecycle state; an owner filing her own item means --status approved")
    a.add_argument("--blocked-note", default=None, help="what a blocked item is waiting on (free text)")
    a.add_argument(
        "--assignee",
        default=None,
        help="the worker meant to pick this up (seat id, mxid or roster name); omit to let the room decide",
    )

    lister = sub.add_parser("list", help="the open TODOs")
    lister.add_argument("--status", default=None, choices=STATUSES, help="only this lifecycle state")

    st = sub.add_parser("status", help="move one along the lifecycle")
    st.add_argument("id")
    st.add_argument("status", choices=STATUSES)
    st.add_argument("--note", default=None, help="required when blocking: what it waits on")

    sub.add_parser("awaiting", help="the ones needing the owner: proposed, or completion declared")
    r = sub.add_parser("ready", help="the ones whose moment has come")
    r.add_argument("--idle", action="store_true")
    r.add_argument("--owner-away", action="store_true")
    r.add_argument("--tier", default=None, help="FULL|MEDIUM|LIGHT|MINIMAL")
    r.add_argument(
        "--for",
        dest="for_worker",
        default=None,
        help="only items addressed to this worker, plus the unaddressed ones",
    )

    d = sub.add_parser("done", help="close one")
    d.add_argument("id")
    c = sub.add_parser("cancel", help="cancel one")
    c.add_argument("id")

    args = parser.parse_args(argv)
    box = Path(args.workspace) if args.workspace else None
    if args.cmd == "add":
        record = {
            "room_id": args.room,
            "surface": args.surface,
            "note": args.note,
            "how_to_open": args.how_to_open
            or canonical_how_to_open(args.room, args.surface, args.pickup_mode),
            "brief": args.brief,
            "importance": args.importance,
            "urgency": args.urgency,
            "size": args.size,
            "due_at": args.due_at,
            "not_before": args.not_before,
            "when": args.when,
            "status": args.status,
        }
        if args.blocked_note:
            record["blocked_note"] = args.blocked_note
        if args.context:
            record["context"] = args.context
        if args.assignee:
            record["assignee"] = args.assignee
        try:
            row = add(record, workspace=box)
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(row, ensure_ascii=False))
        return 0
    if args.cmd == "list":
        rows = load(box)
        if args.status:
            rows = [r for r in rows if r.get("status", "proposed") == args.status]
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "awaiting":
        print(json.dumps(awaiting_owner(box), ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "status":
        try:
            moved = set_status(args.id, args.status, args.note, workspace=box)
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        print("moved" if moved else "no such id", file=sys.stderr)
        return 0 if moved else 1
    if args.cmd == "ready":
        tier = (args.tier or "").upper()
        signals = {
            "idle": args.idle,
            "owner_away": args.owner_away,
            "credit_full": tier == "FULL",
            "credit_medium_or_better": tier in ("FULL", "MEDIUM"),
            "tier": tier or None,
        }
        rows = ready(workspace=box, signals=signals)
        if args.for_worker:
            rows = [r for r in rows if r.get("assignee") in (None, args.for_worker)]
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if args.cmd in ("done", "cancel"):
        state = "done" if args.cmd == "done" else "cancelled"
        closed = close(args.id, state, workspace=box)
        print("closed" if closed else "no such id", file=sys.stderr)
        return 0 if closed else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
