"""Cross-lane duplicate guard: one room message becomes one task, answered by its owner lane when known.

An install may run several gateway bridges ("lanes"), one per homeserver connection, all
writing into the same workspace inbox. A Matrix event keeps its `event_id` on every
homeserver that sees it, so the same message can arrive once per lane. Before a lane queues
a task, `claim_key` records `(room, source_message_id)` with the broker-attested tier in one
SQLite store shared by every lane's bridge process; the same event at the same tier, already
held by ANOTHER lane, is a duplicate. Lanes that disagree on the tier both deliver.

On ONE lane, `content_copy_of` records which broker task first carried the message with a given
content: a second broker task for the same message, tier and content is a duplicate, while a
redelivery or a later run of the first task, and an edit (new content), are not. A card click,
picker press or AgentTrigger task is never folded by content.

Which lane answers is first-come unless the caller names the event's owner lanes (the lane whose
agent the message addresses, else the primary): another lane then waits up to `OWNER_WAIT_S` for an
owner's claim and settles as a duplicate once it appears; past the wait it claims as before, so a
message the owner never receives is still answered once.

Claims expire after `TTL_S` and expired rows are pruned on each claim. A task without a
`source_message_id` is never deduped; a hook or Summon task dedupes on its own fire or Summon,
never on a message id, and is delivered when that identity is missing. Any store failure (unwritable
directory, malformed or locked database) fails OPEN: the task is delivered and one warning
line is logged per distinct failure, because a missed duplicate is better than a dropped
message.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Callable, Mapping, Optional

from local_task_protocol import RUN_SEPARATOR

STORE_NAME = "gateway-seen.sqlite3"
# One agent's store, shared by its lanes only: keys leave the agent out, so two agents must never share one.
TTL_S = 24 * 3600
BUSY_TIMEOUT_S = 2.0
# Outlasts the owner lane's own poll of the same event; past it the message is answered, never dropped.
OWNER_WAIT_S = 6.0
OWNER_POLL_S = 0.25

_WARNED: "set[str]" = set()


def lane_label(lane: str) -> str:
    return lane or "primary"


_SUMMON_FIELDS = ("room_id", "database", "row_id", "update_id")


def _context_identity(task: Mapping[str, object]) -> "tuple[bool, Optional[str]]":
    """(is a Commons context task, its lane-independent identity or None).

    A hook fire is its fire_id; a Summon is the backend's summon key (room, database, row, update).
    Neither includes the agent, unlike the broker task id, so each lane's copy of one fire shares it."""
    hook, summon = task.get("hook"), task.get("summon")
    if not hook and not summon:
        return False, None
    kind, fields = ("hook", ("fire_id",)) if isinstance(hook, Mapping) else ("summon", _SUMMON_FIELDS)
    source = hook if kind == "hook" else summon
    if not isinstance(source, Mapping):
        return True, None
    parts = [str(source.get(k) if source.get(k) is not None else "").strip() for k in fields]
    # A kind-tagged JSON array: one field cannot spill into the next, and it has no raw newline,
    # so it never equals a message key's "room\nmessage_id".
    return True, json.dumps([kind, *parts], ensure_ascii=True, separators=(",", ":")) if all(parts) else None


def key_of(task: Mapping[str, object]) -> Optional[str]:
    """The event a broker task answers, or None when the task must never be deduped."""
    context, identity = _context_identity(task)
    if context:
        # One task per fire or Summon across lanes; a message id it carries never folds it into another task.
        return hashlib.sha256(identity.encode("utf-8")).hexdigest() if identity else None
    message_id = str(task.get("source_message_id") or "").strip()
    if not message_id:
        return None
    room = str(task.get("source_room_id") or task.get("channel_id") or "").strip()
    return hashlib.sha256(f"{room}\n{message_id}".encode("utf-8")).hexdigest()


def tier_of(task: Mapping[str, object]) -> str:
    """The broker's attestation of the sender, as the bridge will read it."""
    tier = str(task.get("access_tier") or "").strip()
    return f"{tier}+collaborator" if task.get("collaborator") is True else tier


def _warn(log: Callable[[str], None], message: str) -> None:
    if message not in _WARNED:
        _WARNED.add(message)
        log(message)


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _ensure_schema(con: sqlite3.Connection) -> None:
    # Created only when absent: an IF NOT EXISTS create takes the write lock even as a no-op.
    if _has_table(con, "event_claims"):
        return
    con.execute("CREATE TABLE event_claims (key TEXT PRIMARY KEY, event TEXT NOT NULL, "
                "lane TEXT NOT NULL, tier TEXT NOT NULL, ts REAL NOT NULL)")
    con.execute("CREATE INDEX event_claims_ts ON event_claims (ts)")
    con.execute("CREATE INDEX event_claims_event ON event_claims (event)")


def _ensure_content_schema(con: sqlite3.Connection) -> None:
    if _has_table(con, "content_claims"):
        return
    con.execute("CREATE TABLE content_claims (key TEXT PRIMARY KEY, task TEXT NOT NULL, ts REAL NOT NULL)")
    con.execute("CREATE INDEX content_claims_ts ON content_claims (ts)")


# Each makes the task an action on the message rather than a reading of it.
_ACTION_FIELDS = ("hitl_click", "picker_command", "picker_args", "event_type", "agent_trigger_id")


def content_of(task: Mapping[str, object]) -> Optional[str]:
    """Digest of what the task asks, or None when the task must never be folded by content."""
    if _context_identity(task)[0] or any(f in task and task.get(f) is not None for f in _ACTION_FIELDS):
        return None
    shape = [str(task.get("interaction_type") or ""), str(task.get("task") or "")]
    return hashlib.sha256(json.dumps(shape, ensure_ascii=True).encode("utf-8")).hexdigest()


def task_identity(task: Mapping[str, object]) -> str:
    """The broker task a local id names, whichever run of it this is."""
    return str(task.get("id") or "").strip().split(RUN_SEPARATOR, 1)[0]


def content_copy_of(state_dir: Path, event: str, lane: str, tier: str, content: str, task_id: str, *,
                    now: Optional[float] = None, ttl: float = TTL_S,
                    log: Callable[[str], None] = print) -> Optional[str]:
    """The other task this lane already queued for (`event`, `tier`, `content`), else None after
    recording `task_id` as its holder. Fails open like `claim_key`."""
    now = time.time() if now is None else now
    path = Path(state_dir) / STORE_NAME
    key = hashlib.sha256(f"{event}\n{tier}\n{lane}\n{content}".encode("utf-8")).hexdigest()
    con = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S, isolation_level=None)
        con.execute("BEGIN IMMEDIATE")
        try:
            _ensure_content_schema(con)
            con.execute("DELETE FROM content_claims WHERE ts <= ?", (now - ttl,))
            row = con.execute("SELECT task FROM content_claims WHERE key = ?", (key,)).fetchone()
            if row is None:
                con.execute("INSERT INTO content_claims (key, task, ts) VALUES (?, ?, ?)", (key, task_id, now))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        return None if row is None or str(row[0]) == task_id else str(row[0])
    except (sqlite3.Error, OSError) as exc:
        _warn(log, f"lane dedupe: store {path} unusable ({type(exc).__name__}: {exc}); delivering without the guard")
        return None
    finally:
        if con is not None:
            con.close()


def claim_key(state_dir: Path, event: str, lane: str, tier: str = "", *, now: Optional[float] = None,
              ttl: float = TTL_S, log: Callable[[str], None] = print) -> "tuple[Optional[str], list]":
    """Claim (`event`, `tier`) for `lane`. Returns (the other lane holding that claim or None,
    [(lane, tier)] of other lanes that hold the same event under a different tier)."""
    now = time.time() if now is None else now
    path = Path(state_dir) / STORE_NAME
    key = hashlib.sha256(f"{event}\n{tier}".encode("utf-8")).hexdigest()
    con = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S, isolation_level=None)
        # IMMEDIATE takes the write lock before the read, so two lanes cannot both see "free".
        con.execute("BEGIN IMMEDIATE")
        try:
            _ensure_schema(con)
            con.execute("DELETE FROM event_claims WHERE ts <= ?", (now - ttl,))
            row = con.execute("SELECT lane FROM event_claims WHERE key = ?", (key,)).fetchone()
            if row is None:
                con.execute("INSERT INTO event_claims (key, event, lane, tier, ts) VALUES (?, ?, ?, ?, ?)",
                            (key, event, lane, tier, now))
                holder = None
            else:
                holder = None if str(row[0]) == lane else str(row[0])
            others = con.execute("SELECT lane, tier FROM event_claims WHERE event = ? AND key != ? AND lane != ?",
                                 (event, key, lane)).fetchall()
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        return holder, [(str(a), str(b)) for a, b in others]
    except (sqlite3.Error, OSError) as exc:
        _warn(log, f"lane dedupe: store {path} unusable ({type(exc).__name__}: {exc}); delivering without the guard")
        return None, []
    finally:
        if con is not None:
            con.close()


def holder_of(state_dir: Path, event: str, tier: str = "", *, now: Optional[float] = None,
              ttl: float = TTL_S) -> Optional[str]:
    """The lane holding an unexpired claim on (`event`, `tier`), read only; None when unclaimed or unreadable."""
    now = time.time() if now is None else now
    path = Path(state_dir) / STORE_NAME
    if not path.exists():
        return None
    key = hashlib.sha256(f"{event}\n{tier}".encode("utf-8")).hexdigest()
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_S)
        try:
            row = con.execute("SELECT lane FROM event_claims WHERE key = ? AND ts > ?", (key, now - ttl)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return None if row is None else str(row[0])


def _await_owner(state_dir: Path, event: str, tier: str, owners, wait_s: float, sleep) -> Optional[str]:
    deadline = time.monotonic() + wait_s
    while True:
        holder = holder_of(state_dir, event, tier)
        if holder is not None and holder in owners:
            return holder
        if time.monotonic() >= deadline:
            return None
        sleep(OWNER_POLL_S)


class OwnerPending(Exception):
    """The owner lane has not claimed yet and the caller asked not to wait."""


def duplicate_reason(state_dir: Path, task: Mapping[str, object], lane: str, *,
                     now: Optional[float] = None, log: Callable[[str], None] = print,
                     owner_lanes=(), wait_s: Optional[float] = None, sleep=None,
                     defer: bool = False) -> Optional[str]:
    """One log line naming both lanes when another lane already took this message at the same
    tier, else None; lanes that disagree on the tier both deliver, with one WARNING. With
    `owner_lanes` not naming `lane`, an owner's claim within `wait_s` (`OWNER_WAIT_S`) makes this one the duplicate; `defer` raises
    OwnerPending instead of waiting. On this lane, another broker task with the same content is a duplicate too."""
    event = key_of(task)
    if event is None:
        return None
    tier = tier_of(task)
    if owner_lanes and lane not in owner_lanes:
        if defer:
            owner = holder_of(state_dir, event, tier)
            if owner is None or owner not in owner_lanes:
                raise OwnerPending()
        else:
            owner = _await_owner(state_dir, event, tier, list(owner_lanes),
                                 OWNER_WAIT_S if wait_s is None else wait_s, sleep or time.sleep)
        if owner is not None:
            return (f"lane dedupe: message {task.get('source_message_id')} on lane {lane_label(lane)} "
                    f"is lane {lane_label(owner)}'s to answer; settled as a redelivery")
    holder, disagreeing = claim_key(state_dir, event, lane, tier, now=now, log=log)
    if holder is not None:
        return (f"lane dedupe: message {task.get('source_message_id')} on lane {lane_label(lane)} "
                f"was already taken by lane {lane_label(holder)}; settled as a redelivery")
    content, task_id = content_of(task), task_identity(task)
    if content is not None and task_id:
        earlier = content_copy_of(state_dir, event, lane, tier, content, task_id, now=now, log=log)
        if earlier is not None:
            return (f"lane dedupe: message {task.get('source_message_id')} on lane {lane_label(lane)} "
                    f"was already queued as {earlier} with the same content; settled as a redelivery")
    if disagreeing:
        seen = ", ".join(f"lane {lane_label(a)} as {b or 'none'}" for a, b in disagreeing)
        log(f"WARNING lane dedupe: message {task.get('source_message_id')} attested as "
            f"{tier or 'none'} on lane {lane_label(lane)} but {seen}; delivering both")
    return None
