"""event_inbox — a durable, crash-safe local inbox for Workspace Events (#AWP P0).

Events (unlike tasks) can be high-frequency and continuous, so they are NOT one
file each — they land in a SQLite queue (stdlib `sqlite3`, no deps). The inbox is
the "durable" in "Sparrow guarantees events reach the local durable inbox at
least once; the Core dedups/reprocesses by event_id."

Three cursors are tracked separately (the friend's design — the key to not
losing an event in the recv→write window):
  - received_cursor : highest cursor Sparrow has seen off the wire (in-memory,
                      the EventChannel's concern — NOT persisted here).
  - durable_cursor  : highest cursor written to THIS inbox (persisted; the SSE
                      resume anchor — reconnect with Last-Event-ID = durable).
  - consumed_cursor : highest cursor the Core has processed (persisted).

Idempotent insert (event_id UNIQUE) makes at-least-once safe: a replayed event
after reconnect is silently ignored. WAL mode keeps a reader (the Core consumer)
from blocking the writer (the channel).
"""
from __future__ import annotations

import json
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_inbox (
    event_id    TEXT PRIMARY KEY,
    cursor      INTEGER NOT NULL,
    type        TEXT,
    room_id     TEXT,
    payload     TEXT NOT NULL,
    received_at REAL NOT NULL,
    consumed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_inbox_cursor   ON event_inbox(cursor);
CREATE INDEX IF NOT EXISTS idx_inbox_unconsumed ON event_inbox(consumed_at) WHERE consumed_at IS NULL;
"""


class EventInbox:
    def __init__(self, path: str):
        # check_same_thread=False: the channel thread writes, the consumer reads.
        # WAL + a short busy timeout make that concurrency safe without app locks.
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # -- write side (EventChannel) ------------------------------------------- #
    def insert(self, event: dict) -> bool:
        """Idempotently persist one event. Returns True if newly inserted, False
        if it was a duplicate (replayed after reconnect). The COMMIT is the
        durable point — once it returns True, durable_cursor covers this event."""
        eid = str(event.get("event_id") or "")
        cur = event.get("cursor")
        if not eid or not isinstance(cur, int):
            return False  # unusable envelope — never advance the cursor past it
        try:
            self._db.execute(
                "INSERT INTO event_inbox(event_id, cursor, type, room_id, payload, received_at)"
                " VALUES (?,?,?,?,?,?)",
                (eid, cur, event.get("type"), event.get("room_id"),
                 json.dumps(event, ensure_ascii=False), time.time()),
            )
            self._db.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # event_id UNIQUE violation = duplicate → at-least-once dedup

    def durable_cursor(self) -> "int | None":
        """The SSE resume anchor: the highest cursor durably written. Reconnect
        with Last-Event-ID = this so a crash never advances past unwritten events."""
        row = self._db.execute("SELECT MAX(cursor) FROM event_inbox").fetchone()
        return row[0] if row and row[0] is not None else None

    # -- read side (Core attention consumer) --------------------------------- #
    def unconsumed(self, limit: int = 100) -> "list[dict]":
        """Oldest-first batch the Core hasn't processed yet."""
        rows = self._db.execute(
            "SELECT payload FROM event_inbox WHERE consumed_at IS NULL"
            " ORDER BY cursor ASC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def mark_consumed(self, event_ids: "list[str]") -> int:
        """Mark events processed by the Core. Idempotent."""
        if not event_ids:
            return 0
        now = time.time()
        cur = self._db.executemany(
            "UPDATE event_inbox SET consumed_at=? WHERE event_id=? AND consumed_at IS NULL",
            [(now, e) for e in event_ids])
        self._db.commit()
        return cur.rowcount

    def consumed_cursor(self) -> "int | None":
        row = self._db.execute(
            "SELECT MAX(cursor) FROM event_inbox WHERE consumed_at IS NOT NULL").fetchone()
        return row[0] if row and row[0] is not None else None

    # -- retention ----------------------------------------------------------- #
    def prune(self, max_age_s: float = 24 * 3600, keep_last: int = 10000) -> int:
        """Drop CONSUMED events older than max_age_s, but always keep the most
        recent `keep_last` rows regardless (mirrors the server EventLog policy).
        Never prunes unconsumed events — the Core must see them first."""
        cutoff = time.time() - max_age_s
        cur = self._db.execute(
            "DELETE FROM event_inbox WHERE consumed_at IS NOT NULL AND consumed_at < ?"
            " AND cursor NOT IN (SELECT cursor FROM event_inbox ORDER BY cursor DESC LIMIT ?)",
            (cutoff, keep_last))
        self._db.commit()
        return cur.rowcount

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass
