"""runtime-api request store — durable request lifecycle in SQLite.

The Unix socket is transport, never storage: every request is durably
recorded the moment it is issued, survives daemon restarts, and resolves
exactly once. SQLite (WAL) over loose JSON files because the lifecycle needs
atomic state transitions (a resolution and an expiry racing must produce ONE
terminal state), status queries, and concurrent `request.wait` pollers.

States: pending → approved|denied|resolved|completed|failed|cancelled|expired
(approved may later be stamped consumed — one-time-use approvals). Terminal
states are immutable: `transition()` is compare-and-swap on status, so a late
answer can never overwrite a resolution — the same contract the human-action
store enforces with flock.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid

TERMINAL = frozenset(
    {"approved", "denied", "resolved", "completed", "failed", "cancelled", "expired"}
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_requests (
  request_id   TEXT PRIMARY KEY,
  request_type TEXT NOT NULL,
  task_id      TEXT,
  execution_id TEXT,
  actor_id     TEXT NOT NULL,
  method       TEXT NOT NULL,
  params_json  TEXT NOT NULL,
  status       TEXT NOT NULL,
  result_json  TEXT,
  created_at   REAL NOT NULL,
  expires_at   REAL,
  resolved_at  REAL,
  resolved_by  TEXT,
  consumed_at  REAL,
  idempotency_key TEXT,
  fingerprint  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_requests_idem
  ON runtime_requests (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_runtime_requests_status
  ON runtime_requests (status);
CREATE TABLE IF NOT EXISTS attribution_outbox (
  request_id    TEXT PRIMARY KEY,
  actor_id      TEXT NOT NULL,
  receipts_json TEXT NOT NULL,
  status        TEXT NOT NULL,
  attempts      INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  error         TEXT,
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL,
  FOREIGN KEY (request_id) REFERENCES runtime_requests(request_id)
);
CREATE INDEX IF NOT EXISTS idx_attribution_outbox_status
  ON attribution_outbox (status);
"""


class RequestStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        # Idempotent migration BEFORE the schema script: a pre-idempotency v0
        # DB (the live acceptance created one) has the table without the two
        # newer columns, and CREATE TABLE IF NOT EXISTS won't add them — the
        # index creation would then fail on boot (review P1: rolling-update
        # path). ALTER is a no-op error when the column already exists.
        have = {r[1] for r in self._db.execute(
            "PRAGMA table_info(runtime_requests)").fetchall()}
        if have:  # table exists — migrate missing columns
            for col, decl in (("idempotency_key", "TEXT"),
                              ("fingerprint", "TEXT")):
                if col not in have:
                    self._db.execute(
                        f"ALTER TABLE runtime_requests ADD COLUMN {col} {decl}")
        self._db.executescript(_SCHEMA)
        outbox_have = {r[1] for r in self._db.execute(
            "PRAGMA table_info(attribution_outbox)").fetchall()}
        for col, decl in (("attempts", "INTEGER NOT NULL DEFAULT 0"),
                          ("next_attempt_at", "REAL NOT NULL DEFAULT 0")):
            if col not in outbox_have:
                self._db.execute(
                    f"ALTER TABLE attribution_outbox ADD COLUMN {col} {decl}")
        self._db.commit()

    # ── lifecycle ───────────────────────────────────────────────────────────
    def create(self, request_type: str, method: str, actor_id: str,
               params: dict, task_id=None, execution_id=None,
               expires_in_s=None, idempotency_key=None,
               fingerprint=None) -> dict:
        rid = f"{request_type}-{uuid.uuid4().hex[:12]}"
        now = time.time()
        expires_at = (now + float(expires_in_s)) if expires_in_s else None
        self._db.execute(
            "INSERT INTO runtime_requests (request_id, request_type, task_id,"
            " execution_id, actor_id, method, params_json, status, created_at,"
            " expires_at, idempotency_key, fingerprint)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, request_type, task_id, execution_id, actor_id, method,
             json.dumps(params, ensure_ascii=False), "pending", now, expires_at,
             idempotency_key, fingerprint))
        self._db.commit()
        return self.get(rid)

    def create_consuming(self, approval_id: str, request_type: str, method: str,
                         actor_id: str, params: dict, task_id=None,
                         idempotency_key=None, fingerprint=None):
        """create() + consume(approval_id) as ONE transaction. The record
        insert (carrying its idempotency key) and the approval's consumption
        commit together or not at all — a crash or duplicate-key race between
        the two steps can never spend an approval without leaving a replayable
        record (review P1: consume-then-create left exactly that window).
        Returns the new record; None when the approval lost the consume race
        (nothing inserted). A duplicate idempotency_key raises
        sqlite3.IntegrityError with the approval untouched."""
        rid = f"{request_type}-{uuid.uuid4().hex[:12]}"
        now = time.time()
        try:
            self._db.execute(
                "INSERT INTO runtime_requests (request_id, request_type, task_id,"
                " execution_id, actor_id, method, params_json, status, created_at,"
                " expires_at, idempotency_key, fingerprint)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, request_type, task_id, None, actor_id, method,
                 json.dumps(params, ensure_ascii=False), "pending", now, None,
                 idempotency_key, fingerprint))
            cur = self._db.execute(
                "UPDATE runtime_requests SET consumed_at = ? WHERE request_id = ?"
                " AND status = 'approved' AND consumed_at IS NULL",
                (now, approval_id))
            if cur.rowcount != 1:
                self._db.rollback()
                return None
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.get(rid)

    def by_idempotency_key(self, key: str):
        """The existing request created under this key, or None. The unique
        index makes create() with a duplicate key raise — callers must look
        up FIRST and return the recorded request instead of re-executing."""
        row = self._db.execute(
            "SELECT request_id, fingerprint FROM runtime_requests"
            " WHERE idempotency_key = ?", (key,)).fetchone()
        if not row:
            return None
        rec = self.get(row[0])
        if rec is not None:
            rec["fingerprint"] = row[1]
        return rec

    def get(self, request_id: str):
        cur = self._db.execute(
            "SELECT request_id, request_type, task_id, execution_id, actor_id,"
            " method, params_json, status, result_json, created_at, expires_at,"
            " resolved_at, resolved_by, consumed_at FROM runtime_requests"
            " WHERE request_id = ?", (request_id,))
        row = cur.fetchone()
        if row is None:
            return None
        rec = {
            "requestId": row[0], "requestType": row[1], "taskId": row[2],
            "executionId": row[3], "actorId": row[4], "method": row[5],
            "params": json.loads(row[6]), "status": row[7],
            "result": json.loads(row[8]) if row[8] else None,
            "createdAt": row[9], "expiresAt": row[10], "resolvedAt": row[11],
            "resolvedBy": row[12], "consumedAt": row[13],
        }
        # Lazy expiry: a pending request past its deadline reads as expired —
        # and is transitioned durably so every later reader agrees.
        if (rec["status"] == "pending" and rec["expiresAt"]
                and time.time() > rec["expiresAt"]):
            if self.transition(request_id, "expired"):
                rec["status"] = "expired"
        return rec

    def transition(self, request_id: str, new_status: str, result=None,
                   resolved_by=None) -> bool:
        """pending → terminal, exactly once (CAS on status). False = lost the
        race (already terminal) or unknown id — the caller must re-read."""
        cur = self._db.execute(
            "UPDATE runtime_requests SET status = ?, result_json = ?,"
            " resolved_at = ?, resolved_by = ? WHERE request_id = ? AND"
            " status = 'pending'",
            (new_status, json.dumps(result, ensure_ascii=False) if result is not None else None,
             time.time(), resolved_by, request_id))
        self._db.commit()
        return cur.rowcount == 1

    def complete_with_receipts(self, request_id: str, result: dict,
                               actor_id: str, receipts: list[dict]) -> bool:
        """Complete provider execution and enqueue its receipts atomically."""
        now = time.time()
        try:
            cur = self._db.execute(
                "UPDATE runtime_requests SET status = 'completed', result_json = ?,"
                " resolved_at = ?, resolved_by = 'executor' WHERE request_id = ?"
                " AND status = 'pending'",
                (json.dumps(result, ensure_ascii=False), now, request_id))
            if cur.rowcount != 1:
                self._db.rollback()
                return False
            self._db.execute(
                "INSERT INTO attribution_outbox"
                " (request_id, actor_id, receipts_json, status, created_at, updated_at)"
                " VALUES (?, ?, ?, 'pending', ?, ?)",
                (request_id, actor_id, json.dumps(receipts, ensure_ascii=False), now, now))
            self._db.commit()
            return True
        except Exception:
            self._db.rollback()
            raise

    def attribution_outbox(self, status: str = "pending", *,
                           due_before=None, limit: int = 8) -> list[dict]:
        due = time.time() if due_before is None else float(due_before)
        cur = self._db.execute(
            "SELECT request_id, actor_id, receipts_json, status, error, created_at,"
            " attempts, next_attempt_at FROM attribution_outbox"
            " WHERE status = ? AND next_attempt_at <= ?"
            " ORDER BY created_at, request_id LIMIT ?",
            (status, due, int(limit)))
        return [{
            "requestId": row[0], "actorId": row[1], "receipts": json.loads(row[2]),
            "status": row[3], "error": row[4], "createdAt": row[5],
            "attempts": row[6], "nextAttemptAt": row[7],
        } for row in cur.fetchall()]

    def attribution_status(self, request_id: str):
        row = self._db.execute(
            "SELECT status, error FROM attribution_outbox WHERE request_id = ?",
            (request_id,)).fetchone()
        return {"status": row[0], "error": row[1]} if row else None

    def settle_attribution(self, request_id: str, status: str,
                           error=None) -> bool:
        if status not in {"recorded", "unavailable"}:
            raise ValueError("attribution status must be recorded or unavailable")
        cur = self._db.execute(
            "UPDATE attribution_outbox SET status = ?, error = ?, updated_at = ?"
            " WHERE request_id = ? AND status = 'pending'",
            (status, error, time.time(), request_id))
        self._db.commit()
        return cur.rowcount == 1

    def defer_attribution(self, request_id: str, error: str, *,
                          max_attempts: int = 5, now=None) -> str:
        current = time.time() if now is None else float(now)
        row = self._db.execute(
            "SELECT attempts FROM attribution_outbox"
            " WHERE request_id = ? AND status = 'pending'", (request_id,),
        ).fetchone()
        if row is None:
            return "missing"
        attempts = row[0] + 1
        status = "unavailable" if attempts >= max_attempts else "pending"
        delay = 0 if status == "unavailable" else min(60.0, 2.0 ** (attempts - 1))
        self._db.execute(
            "UPDATE attribution_outbox SET attempts = ?, status = ?, error = ?,"
            " next_attempt_at = ?, updated_at = ? WHERE request_id = ?"
            " AND status = 'pending'",
            (attempts, status, str(error)[:1000], current + delay, current, request_id),
        )
        self._db.commit()
        return status

    def consume(self, request_id: str) -> bool:
        """Stamp a one-time-use approval as consumed (approved → consumed_at
        set, exactly once). An approval for one action must not replay."""
        cur = self._db.execute(
            "UPDATE runtime_requests SET consumed_at = ? WHERE request_id = ?"
            " AND status = 'approved' AND consumed_at IS NULL",
            (time.time(), request_id))
        self._db.commit()
        return cur.rowcount == 1

    def pending(self) -> list:
        cur = self._db.execute(
            "SELECT request_id FROM runtime_requests WHERE status = 'pending'")
        return [self.get(r[0]) for r in cur.fetchall()]

    def close(self) -> None:
        self._db.close()
