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
    {"approved", "denied", "resolved", "completed", "declined", "failed",
     "cancelled", "expired"}
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
"""


class RequestStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
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
