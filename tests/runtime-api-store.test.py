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
import threading
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

    filtered_db = str(tmp / "filtered.sqlite")
    filtered = rs.RequestStore(filtered_db)
    mirrored_types = ("approval", "elicitation", "human_action")
    expected = set()
    for kind in ("approval", "elicitation", "human_action", "capability", "other"):
        for status in ("pending", *sorted(rs.TERMINAL)):
            row = filtered.create(kind, kind + ".request", "synthetic", {})
            if status != "pending":
                filtered.transition(row["requestId"], status)
            if status == "expired" and kind in mirrored_types:
                expected.add(row["requestId"])
    before = filtered._db.execute("SELECT * FROM runtime_requests").fetchall()
    check({r["requestId"] for r in filtered.expired(mirrored_types)} == expected,
          "expiry retry query includes only expired human requests")
    check(filtered.expired(()) == [], "no request types selects no expired rows")
    check(filtered.expired(("approval') OR 1=1 --",)) == [],
          "expiry query binds request types as data")
    capability_rows = filtered.expired(("capability",))
    check(len(capability_rows) == 1 and capability_rows[0]["requestType"] == "capability",
          "the caller owns request-type selection")
    check(all(r["status"] == "pending" for r in filtered.pending()),
          "pending() stays restricted to pending rows")
    check(filtered._db.execute("SELECT * FROM runtime_requests").fetchall() == before,
          "expiry retry query preserves all stored fields")
    filtered.close()
    filtered = rs.RequestStore(filtered_db)
    check({r["requestId"] for r in filtered.expired(mirrored_types)} == expected,
          "expiry retry candidates survive reopen")
    filtered.close()

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

    # The connection is opened check_same_thread=False: a poller on another thread
    # interleaving with writes used to corrupt reads (InterfaceError, None rows).
    shared = rs.RequestStore(str(tmp / "threads.sqlite"))
    errors: list = []
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            try:
                shared.pending()
            except Exception as exc:  # noqa: BLE001 - every failure is the finding
                errors.append(repr(exc))

    poller = threading.Thread(target=poll)
    poller.start()
    try:
        for i in range(2000):
            rec7 = shared.create("capability", "m", "@a:hs", {"i": i},
                                 idempotency_key=f"t{i}")
            if rec7 is None or not shared.transition(rec7["requestId"], "completed",
                                                     result={"i": i}):
                errors.append(f"write {i} lost")
    except Exception as exc:  # noqa: BLE001
        errors.append(repr(exc))
    finally:
        stop.set()
        poller.join()
    check(errors == [], f"one store shared by two threads stays consistent: {errors[:3]}")
    shared.close()

    # create_consuming's insert and consume are one transaction: another thread's
    # commit landing between them must wait, or it commits the insert alone.
    txn_path = str(tmp / "txn.sqlite")
    txn = rs.RequestStore(txn_path)
    other = txn.create("capability", "m", "@a:hs", {})
    unapproved = txn.create("approval", "approval.request", "@a:hs", {})
    real_db, real_lock = txn._db, getattr(txn, "_lock", None)
    seen = threading.Condition()
    evidence: list = []  # first entry decides: only "blocked" passes
    worker_errors: list = []

    def note(what):
        with seen:
            evidence.append(what)
            seen.notify_all()

    class WatchedLock:
        # A failed non-blocking try from the rival is proof the transaction holds it.
        def __enter__(self):
            if not real_lock.acquire(blocking=False):
                note("blocked")
                real_lock.acquire()
            return self

        def __exit__(self, *exc):
            real_lock.release()

    def rival():
        try:
            won = txn.transition(other["requestId"], "completed")
        except BaseException as exc:  # noqa: BLE001 - any worker failure fails the case
            worker_errors.append(repr(exc))
            note("error")
        else:
            note("committed" if won else "lost")

    rival_thread = threading.Thread(target=rival, daemon=True)

    class Paused:
        def __getattr__(self, name):
            return getattr(real_db, name)

        def execute(self, sql, *args):
            if sql.startswith("UPDATE runtime_requests SET consumed_at"):
                rival_thread.start()
                with seen:
                    if not seen.wait_for(lambda: evidence, timeout=30):
                        evidence.append("timeout")
            return real_db.execute(sql, *args)

    txn._db = Paused()
    if real_lock is not None:
        txn._lock = WatchedLock()
    lost = txn.create_consuming(unapproved["requestId"], "capability", "m", "@a:hs", {},
                                idempotency_key="interleaved")
    first = evidence[0] if evidence else "none"
    rival_thread.join(30)
    stuck = rival_thread.is_alive()
    txn._db = real_db
    if real_lock is not None:
        txn._lock = real_lock
    check(first == "blocked",
          f"a commit from another thread mid-create_consuming waits on the store lock (saw {first})")
    check(not stuck and worker_errors == [] and evidence[-1:] == ["committed"],
          f"the waiting thread finishes and commits (stuck={stuck}, errors={worker_errors[:2]})")
    durable = sqlite3.connect(txn_path)
    try:
        orphans = durable.execute("SELECT COUNT(*) FROM runtime_requests"
                                  " WHERE idempotency_key = 'interleaved'").fetchone()[0]
        landed = durable.execute("SELECT status FROM runtime_requests WHERE request_id = ?",
                                 (other["requestId"],)).fetchone()
    finally:
        durable.close()
    check(lost is None and orphans == 0, "the lost consume leaves no committed row")
    check(landed == ("completed",), f"the waiting thread's write is durable ({landed})")
    if not stuck:  # a rival still holding the lock would hang close()
        txn.close()

    print(f"\n{'PASS — request store unit suite green' if not FAILS else f'FAILED ({len(FAILS)})'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
