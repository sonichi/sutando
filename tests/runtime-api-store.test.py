#!/usr/bin/env python3
"""In-process unit suite for the runtime-api RequestStore.

The E2E suite (tests/runtime-api-e2e.test.py) exercises the store through the
REAL daemon — a subprocess the coverage recorder cannot see. This suite drives
RequestStore directly in-process so the durable-lifecycle contract is both
unit-pinned and coverage-visible: CAS transitions, lazy expiry, one-time
consumption, idempotency-key lookup with fingerprint, atomic
create_consuming (record + approval consumption in one transaction),
pending recovery, and the pre-idempotency schema migration.

Run: python3 tests/runtime-api-store.test.py   (stdlib only)
"""
from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "request_store", REPO / "src" / "runtime-api" / "request_store.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

FAILS: list = []


def check(cond, msg):
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rt-store-"))
    store = rs.RequestStore(str(tmp / "s.sqlite"))

    # create → get roundtrip
    rec = store.create("approval", "approval.request", "@a:hs",
                       {"action": "x.y"}, task_id="t1")
    got = store.get(rec["requestId"])
    check(got["status"] == "pending" and got["taskId"] == "t1"
          and got["params"]["action"] == "x.y" and got["result"] is None,
          "create → get roundtrip (pending, params, no result)")
    check(store.get("nope-123") is None, "get unknown id → None")

    # pending() lists it; transition is CAS-once
    check(any(r["requestId"] == rec["requestId"] for r in store.pending()),
          "pending() lists the open request")
    check(store.transition(rec["requestId"], "approved",
                           resolved_by="@o:hs") is True,
          "pending → approved transition succeeds")
    check(store.transition(rec["requestId"], "denied") is False,
          "second transition loses the CAS (terminal immutable)")
    got = store.get(rec["requestId"])
    check(got["status"] == "approved" and got["resolvedBy"] == "@o:hs",
          "terminal state + resolver recorded")

    # one-time consumption
    check(store.consume(rec["requestId"]) is True, "approved consumes once")
    check(store.consume(rec["requestId"]) is False, "second consume refused")
    check(store.consume("nope-1") is False, "consume unknown id refused")

    # consume only applies to approved
    rec2 = store.create("approval", "approval.request", "@a:hs", {})
    check(store.consume(rec2["requestId"]) is False,
          "pending request cannot be consumed")

    # lazy expiry: past-deadline pending reads (and persists) expired
    rec3 = store.create("elicitation", "elicitation.request", "@a:hs",
                        {}, expires_in_s=0.05)
    time.sleep(0.1)
    check(store.get(rec3["requestId"])["status"] == "expired",
          "past-deadline pending reads as expired (lazy, durable)")
    check(store.get(rec3["requestId"])["status"] == "expired",
          "expiry is persisted (second read agrees)")

    # transition with a result payload
    rec4 = store.create("capability", "capability.execute", "@a:hs", {})
    store.transition(rec4["requestId"], "completed",
                     result={"eventId": "$e"}, resolved_by="executor")
    check(store.get(rec4["requestId"])["result"]["eventId"] == "$e",
          "result payload persists on transition")

    # idempotency key + fingerprint
    rec5 = store.create("capability", "capability.execute", "@a:hs",
                        {"action": "m.s"}, idempotency_key="k1",
                        fingerprint="fp-abc")
    hit = store.by_idempotency_key("k1")
    check(hit is not None and hit["requestId"] == rec5["requestId"]
          and hit["fingerprint"] == "fp-abc",
          "by_idempotency_key returns the request with its fingerprint")
    check(store.by_idempotency_key("k-none") is None,
          "unknown idempotency key → None")

    # ── create_consuming: record insert + approval consume are ONE txn ──────
    # (review P1: consume-then-create left a window where a crash or a
    # same-key race spent an approval without leaving a replayable record)
    ap = store.create("approval", "approval.request", "@a:hs",
                      {"action": "message.send", "resource": {"roomId": "!r"}})
    store.transition(ap["requestId"], "approved", resolved_by="@o:hs")
    cap = store.create_consuming(ap["requestId"], "capability",
                                 "capability.execute", "@a:hs", {},
                                 idempotency_key="atomic-k1", fingerprint="fpA")
    check(cap is not None and cap["status"] == "pending"
          and cap["requestId"].startswith("capability-"),
          "create_consuming: record created")
    check(store.get(ap["requestId"])["consumedAt"] is not None,
          "create_consuming: approval consumed in the same commit")

    n_before = store._db.execute(
        "SELECT COUNT(*) FROM runtime_requests").fetchone()[0]
    check(store.create_consuming(ap["requestId"], "capability",
                                 "capability.execute", "@a:hs", {},
                                 idempotency_key="atomic-k2") is None,
          "already-consumed approval → create_consuming returns None")
    n_after = store._db.execute(
        "SELECT COUNT(*) FROM runtime_requests").fetchone()[0]
    check(n_after == n_before and store.by_idempotency_key("atomic-k2") is None,
          "lost consume race rolls back the insert (no orphan row, key unspent)")

    ap2 = store.create("approval", "approval.request", "@a:hs",
                       {"action": "message.send"})
    store.transition(ap2["requestId"], "approved", resolved_by="@o:hs")
    try:
        store.create_consuming(ap2["requestId"], "capability",
                               "capability.execute", "@a:hs", {},
                               idempotency_key="atomic-k1", fingerprint="fpB")
        dup_raised = False
    except sqlite3.IntegrityError:
        dup_raised = True
    check(dup_raised, "duplicate idempotency key raises IntegrityError")
    a2 = store.get(ap2["requestId"])
    check(a2["status"] == "approved" and a2["consumedAt"] is None,
          "same-key/different-approval race leaves the 2nd approval unconsumed")
    check(store.consume(ap2["requestId"]) is True,
          "raced-but-unspent approval remains consumable afterwards")

    store.close()

    # migration: pre-idempotency schema gains the new columns on boot
    old_db = str(tmp / "old.sqlite")
    con = sqlite3.connect(old_db)
    con.executescript("""
CREATE TABLE runtime_requests (
  request_id TEXT PRIMARY KEY, request_type TEXT NOT NULL, task_id TEXT,
  execution_id TEXT, actor_id TEXT NOT NULL, method TEXT NOT NULL,
  params_json TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT,
  created_at REAL NOT NULL, expires_at REAL, resolved_at REAL,
  resolved_by TEXT, consumed_at REAL);
INSERT INTO runtime_requests VALUES ('approval-m1','approval','t',NULL,
  '@a:hs','approval.request','{}','pending',NULL,1,NULL,NULL,NULL,NULL);
""")
    # note: column order above deliberately mirrors the old schema; the values
    # place created_at=1 (10th) and leave result_json NULL (9th).
    con.commit()
    con.close()
    migrated = rs.RequestStore(old_db)
    old_rec = migrated.get("approval-m1")
    check(old_rec is not None and old_rec["status"] == "pending",
          "pre-idempotency DB migrates: old rows readable")
    check(migrated.by_idempotency_key("x") is None,
          "migrated DB: idempotency index usable")
    rec6 = migrated.create("capability", "capability.execute", "@a:hs",
                           {}, idempotency_key="k2", fingerprint="fp2")
    check(migrated.by_idempotency_key("k2")["requestId"] == rec6["requestId"],
          "migrated DB: new columns writable")
    # second boot over the migrated DB is a no-op (ALTERs skipped)
    migrated.close()
    again = rs.RequestStore(old_db)
    check(again.get("approval-m1") is not None, "re-boot over migrated DB is clean")
    again.close()

    print(f"\n{'PASS — request store unit suite green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
