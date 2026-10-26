"""Cross-lane duplicate guard: one room message becomes one task, whichever lane saw it first.

An install may run several gateway bridges ("lanes"), one per homeserver connection, all
writing into the same workspace inbox. A Matrix event keeps its `event_id` on every
homeserver that sees it, so the same message can arrive once per lane. Before a lane queues
a task, `claim` records `(room, source_message_id)` in one SQLite store shared by every
lane's bridge process; a key already held by ANOTHER lane is a duplicate. The same lane
seeing its own key again is not, so a lane's own redelivery and retry semantics are kept.

Claims expire after `TTL_S` and expired rows are pruned on each claim. A task without a
`source_message_id`, or a hook task, is never deduped. Any store failure (unwritable
directory, malformed or locked database) fails OPEN: the task is delivered and one warning
line is logged per distinct failure, because a missed duplicate is better than a dropped
message.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Callable, Mapping, Optional

STORE_NAME = "gateway-seen.sqlite3"
TTL_S = 24 * 3600
BUSY_TIMEOUT_S = 2.0

_WARNED: "set[str]" = set()


def lane_label(lane: str) -> str:
    return lane or "primary"


def key_of(task: Mapping[str, object]) -> Optional[str]:
    """The dedupe key for a broker task, or None when the task must never be deduped."""
    if task.get("hook"):
        return None
    message_id = str(task.get("source_message_id") or "").strip()
    if not message_id:
        return None
    room = str(task.get("source_room_id") or task.get("channel_id") or "").strip()
    return hashlib.sha256(f"{room}\n{message_id}".encode("utf-8")).hexdigest()


def _warn(log: Callable[[str], None], message: str) -> None:
    if message not in _WARNED:
        _WARNED.add(message)
        log(message)


def claim(state_dir: Path, key: str, lane: str, *, now: Optional[float] = None,
          ttl: float = TTL_S, log: Callable[[str], None] = print) -> Optional[str]:
    """Claim `key` for `lane`. Returns the other lane holding a live claim, else None (deliver)."""
    now = time.time() if now is None else now
    path = Path(state_dir) / STORE_NAME
    con = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S, isolation_level=None)
        con.execute("CREATE TABLE IF NOT EXISTS claims "
                    "(key TEXT PRIMARY KEY, lane TEXT NOT NULL, ts REAL NOT NULL)")
        con.execute("CREATE INDEX IF NOT EXISTS claims_ts ON claims (ts)")
        # IMMEDIATE takes the write lock before the read, so two lanes cannot both see "free".
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("DELETE FROM claims WHERE ts <= ?", (now - ttl,))
            row = con.execute("SELECT lane FROM claims WHERE key = ?", (key,)).fetchone()
            if row is None:
                con.execute("INSERT INTO claims (key, lane, ts) VALUES (?, ?, ?)", (key, lane, now))
                holder = None
            else:
                holder = None if str(row[0]) == lane else str(row[0])
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        return holder
    except (sqlite3.Error, OSError) as exc:
        _warn(log, f"lane dedupe: store {path} unusable ({type(exc).__name__}: {exc}); delivering without the guard")
        return None
    finally:
        if con is not None:
            con.close()


def duplicate_reason(state_dir: Path, task: Mapping[str, object], lane: str, *,
                     now: Optional[float] = None, log: Callable[[str], None] = print) -> Optional[str]:
    """One log line naming both lanes when another lane already took this message, else None."""
    key = key_of(task)
    if key is None:
        return None
    holder = claim(state_dir, key, lane, now=now, log=log)
    if holder is None:
        return None
    return (f"lane dedupe: message {task.get('source_message_id')} on lane {lane_label(lane)} "
            f"was already taken by lane {lane_label(holder)}; settled as a redelivery")
