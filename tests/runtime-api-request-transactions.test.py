#!/usr/bin/env python3
"""Core RequestStore transactions across independent processes (stdlib only).

Run: python3 tests/runtime-api-request-transactions.test.py

SQL trace callbacks pause before the next statement, after inspecting the
preceding write through the production store. These tests cover process death
with the host running, not power loss or capability/backend delivery.
"""
from __future__ import annotations

import importlib.util
import multiprocessing
from multiprocessing.connection import wait
import os
from pathlib import Path
import sqlite3
import tempfile
import traceback
import unittest
from contextlib import contextmanager

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()


REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "request_store", REPO / "src" / "runtime-api" / "request_store.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

WAIT_SECONDS = 15
JOIN_SECONDS = 5


def _create_consuming(store, spec):
    return store.create_consuming(
        spec["approval"], "capability", "capability.execute", "synthetic-actor",
        {"action": "synthetic.action", "label": spec["key"]},
        task_id="transaction-test", idempotency_key=spec["key"],
        fingerprint=spec["fingerprint"])


def _command(connection, expected):
    if not connection.poll(WAIT_SECONDS):
        raise TimeoutError("parent did not send " + expected)
    actual = connection.recv()
    if actual != expected:
        raise AssertionError("expected command %r, got %r" % (expected, actual))


def _boundary(connection, store, spec):
    message = {
        "event": "boundary", "phase": spec["phase"],
        "inTransaction": store._db.in_transaction,
    }
    # Reading before the first write would create a WAL snapshot and change lock behavior.
    if spec["phase"] != "enter_insert":
        message["request"] = store.by_idempotency_key(spec["key"])
        message["approval"] = store.get(spec["approval"])
    connection.send(message)
    _command(connection, "resume")


def _worker(connection, db_path, spec):
    store = None
    try:
        store = rs.RequestStore(db_path)
        connection.send({
            "event": "ready", "pid": os.getpid(),
            "journalMode": store._db.execute("PRAGMA journal_mode").fetchone()[0],
        })
        _command(connection, "run")
        if spec.get("lockProbe"):
            store._db.execute("PRAGMA busy_timeout = 0")
            try:
                _create_consuming(store, spec)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                connection.send({
                    "event": "locked", "inTransaction": store._db.in_transaction,
                })
            else:
                raise AssertionError("rival write did not encounter the held write lock")
            store._db.execute("PRAGMA busy_timeout = 5000")
            _command(connection, "retry")

        reached = False

        def trace(sql):
            nonlocal reached
            statement = " ".join(sql.upper().split())
            matches = (
                spec["phase"] == "after_insert"
                and statement.startswith("UPDATE RUNTIME_REQUESTS SET CONSUMED_AT")
            ) or (
                spec["phase"] == "after_update" and statement == "COMMIT"
            ) or (
                spec["phase"] == "enter_insert"
                and statement.startswith("INSERT INTO RUNTIME_REQUESTS")
            )
            if reached or not matches:
                return
            reached = True
            try:
                _boundary(connection, store, spec)
            except BaseException:
                # sqlite3 swallows trace exceptions; fail the child rather than continue.
                connection.send({"event": "error", "error": traceback.format_exc()})
                os._exit(71)

        store._db.set_trace_callback(trace)
        try:
            record = _create_consuming(store, spec)
        except sqlite3.IntegrityError:
            record, outcome = None, "duplicate"
        else:
            outcome = "created" if record is not None else "not_consumed"
        finally:
            store._db.set_trace_callback(None)
        if spec["phase"] == "after_return":
            _boundary(connection, store, spec)
        connection.send({
            "event": "result", "outcome": outcome, "request": record,
            "replay": store.by_idempotency_key(spec["key"]),
            "approval": store.get(spec["approval"]),
        })
    except BaseException:
        connection.send({"event": "error", "error": traceback.format_exc()})
        raise
    finally:
        if store is not None:
            store.close()
        connection.close()


class _Child:
    def __init__(self, db_path, spec):
        context = multiprocessing.get_context("spawn")
        self.connection, child_connection = context.Pipe()
        self.process = context.Process(
            target=_worker, args=(child_connection, db_path, spec))
        self.started = False
        try:
            self.process.start()
            self.started = True
        except BaseException:
            self.connection.close()
            raise
        finally:
            child_connection.close()

    def expect(self, case, event):
        ready = wait([self.connection, self.process.sentinel], WAIT_SECONDS)
        if self.connection in ready:
            try:
                message = self.connection.recv()
            except EOFError:
                self.process.join(JOIN_SECONDS)
                case.fail("worker exited before %s (exit=%r)" % (
                    event, self.process.exitcode))
            case.assertEqual(message.get("event"), event, message)
            return message
        if self.process.sentinel in ready:
            self.process.join(JOIN_SECONDS)
            case.fail("worker exited before %s (exit=%r)" % (
                event, self.process.exitcode))
        case.fail("worker did not reach %s within %s seconds" % (
            event, WAIT_SECONDS))

    def send(self, command):
        self.connection.send(command)

    def close(self):
        try:
            if self.started:
                if self.process.is_alive():
                    self.process.kill()
                self.process.join(JOIN_SECONDS)
                if self.process.is_alive():
                    raise AssertionError("owned worker survived kill and bounded join")
                self.process.close()
        finally:
            self.connection.close()


class RequestTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runtime-request-transactions-")
        self.addCleanup(temporary.cleanup)
        self.db_path = str(Path(temporary.name) / "requests.sqlite")

    @contextmanager
    def store(self):
        store = rs.RequestStore(self.db_path)
        try:
            self.assertEqual(
                store._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            yield store
        finally:
            store.close()

    def approval(self):
        with self.store() as store:
            record = store.create("approval", "approval.request", "synthetic-actor", {})
            self.assertTrue(store.transition(record["requestId"], "approved"))
            return record["requestId"]

    def spec(self, approval, key, phase, fingerprint="fp-original", **extra):
        return dict(approval=approval, key=key, phase=phase,
                    fingerprint=fingerprint, **extra)

    def child(self, spec):
        child = _Child(self.db_path, spec)
        self.addCleanup(child.close)
        ready = child.expect(self, "ready")
        self.assertEqual(ready["journalMode"], "wal")
        self.assertNotEqual(ready["pid"], os.getpid())
        return child

    def finish(self, child):
        result = child.expect(self, "result")
        child.process.join(JOIN_SECONDS)
        self.assertFalse(child.process.is_alive(), "worker did not exit after its result")
        self.assertEqual(child.process.exitcode, 0)
        return result

    def kill(self, child):
        self.assertTrue(child.process.is_alive(), "worker exited before the requested kill")
        child.process.kill()
        child.process.join(JOIN_SECONDS)
        self.assertFalse(child.process.is_alive(), "worker survived kill")
        self.assertIsNotNone(child.process.exitcode)
        self.assertNotEqual(child.process.exitcode, 0)

    def assert_unconsumed(self, store, approval):
        record = store.get(approval)
        self.assertEqual(record["status"], "approved")
        self.assertIsNone(record["consumedAt"])

    def assert_replay(self, store, spec, request_id):
        replay = store.by_idempotency_key(spec["key"])
        self.assertEqual(replay["requestId"], request_id)
        self.assertEqual(replay["fingerprint"], spec["fingerprint"])
        self.assertEqual(replay["params"], {
            "action": "synthetic.action", "label": spec["key"],
        })
        self.assertEqual(replay["taskId"], "transaction-test")
        self.assertEqual(replay["status"], "pending")
        self.assertIsNone(replay["result"])
        self.assertIsNone(replay["resolvedAt"])
        self.assertEqual({r["requestId"] for r in store.pending()}, {request_id})
        return replay

    def assert_capability_ids(self, store, expected):
        rows = store._db.execute(
            "SELECT request_id FROM runtime_requests WHERE request_type = 'capability'")
        self.assertEqual({row[0] for row in rows}, set(expected))

    def crash_before_commit(self, phase):
        approval = self.approval()
        spec = self.spec(approval, "crash-key", phase)
        child = self.child(spec)
        child.send("run")
        boundary = child.expect(self, "boundary")
        self.assertEqual(boundary["phase"], phase)
        self.assertTrue(boundary["inTransaction"])
        self.assertIsNotNone(boundary["request"])
        self.assertEqual(boundary["request"]["fingerprint"], spec["fingerprint"])
        if phase == "after_insert":
            self.assertIsNone(boundary["approval"]["consumedAt"])
        else:
            self.assertIsNotNone(boundary["approval"]["consumedAt"])
        with self.store() as reader:
            self.assertIsNone(reader.by_idempotency_key(spec["key"]))
            self.assert_unconsumed(reader, approval)
        self.kill(child)

        with self.store() as reopened:
            self.assertIsNone(reopened.by_idempotency_key(spec["key"]))
            self.assert_unconsumed(reopened, approval)
            self.assert_capability_ids(reopened, ())
            retried = _create_consuming(reopened, spec)
            self.assertIsNotNone(retried)
            self.assertNotEqual(retried["requestId"], boundary["request"]["requestId"])
        with self.store() as reopened:
            self.assert_replay(reopened, spec, retried["requestId"])
            self.assertIsNotNone(reopened.get(approval)["consumedAt"])
            self.assert_capability_ids(reopened, (retried["requestId"],))

    def test_kill_after_insert_rolls_back_request_and_approval(self):
        self.crash_before_commit("after_insert")

    def test_kill_after_approval_update_rolls_back_request_and_approval(self):
        self.crash_before_commit("after_update")

    def test_kill_after_commit_preserves_pending_replay_and_fingerprint(self):
        approval = self.approval()
        spec = self.spec(approval, "committed-key", "after_return")
        child = self.child(spec)
        child.send("run")
        boundary = child.expect(self, "boundary")
        self.assertEqual(boundary["phase"], "after_return")
        self.assertFalse(boundary["inTransaction"])
        self.assertIsNotNone(boundary["approval"]["consumedAt"])
        request_id = boundary["request"]["requestId"]
        with self.store() as reader:
            before = self.assert_replay(reader, spec, request_id)
            self.assertIsNotNone(reader.get(approval)["consumedAt"])
        self.kill(child)

        for _ in range(2):
            with self.store() as reopened:
                self.assertEqual(self.assert_replay(reopened, spec, request_id), before)
                self.assertFalse(reopened.consume(approval))
                with self.assertRaises(sqlite3.IntegrityError):
                    _create_consuming(reopened, spec)
                spent = dict(spec, key="cannot-spend-again")
                self.assertIsNone(_create_consuming(reopened, spent))
                self.assertIsNone(reopened.by_idempotency_key(spent["key"]))
                self.assert_capability_ids(reopened, (request_id,))

    def compete(self, winner_spec, rival_spec):
        winner = self.child(winner_spec)
        rival = self.child(dict(rival_spec, lockProbe=True))
        self.assertNotEqual(winner.process.pid, rival.process.pid)
        winner.send("run")
        boundary = winner.expect(self, "boundary")
        self.assertEqual(boundary["phase"], "after_insert")
        self.assertTrue(boundary["inTransaction"])
        self.assertIsNotNone(boundary["request"])
        rival.send("run")
        locked = rival.expect(self, "locked")
        self.assertFalse(locked["inTransaction"], "failed write did not roll back")
        rival.send("retry")
        entered = rival.expect(self, "boundary")
        self.assertEqual(entered["phase"], "enter_insert")
        rival.send("resume")
        winner.send("resume")
        won, lost = self.finish(winner), self.finish(rival)
        self.assertEqual(won["outcome"], "created")
        self.assertIsNotNone(won["approval"]["consumedAt"])
        self.assertIsNone(lost["request"])
        return won, lost

    def test_processes_with_one_approval_leave_no_losing_request(self):
        approval = self.approval()
        winner_spec = self.spec(approval, "winning-key", "after_insert", "fp-winner")
        rival_spec = self.spec(approval, "losing-key", "enter_insert", "fp-loser")
        won, lost = self.compete(winner_spec, rival_spec)
        self.assertEqual(lost["outcome"], "not_consumed")
        self.assertIsNone(lost["replay"])
        self.assertIsNotNone(lost["approval"]["consumedAt"])
        with self.store() as reopened:
            self.assert_replay(reopened, winner_spec, won["request"]["requestId"])
            self.assertIsNone(reopened.by_idempotency_key(rival_spec["key"]))
            self.assert_capability_ids(reopened, (won["request"]["requestId"],))
        reusable = dict(rival_spec, approval=self.approval())
        with self.store() as reopened:
            record = _create_consuming(reopened, reusable)
            self.assertIsNotNone(record, "rolled-back losing key should remain usable")
            self.assertEqual(reopened.by_idempotency_key(reusable["key"])["fingerprint"],
                             reusable["fingerprint"])

    def test_processes_with_one_key_keep_losing_approval_usable(self):
        winner_approval, rival_approval = self.approval(), self.approval()
        winner_spec = self.spec(winner_approval, "shared-key", "after_insert", "fp-winner")
        rival_spec = self.spec(rival_approval, "shared-key", "enter_insert", "fp-loser")
        won, lost = self.compete(winner_spec, rival_spec)
        self.assertEqual(lost["outcome"], "duplicate")
        self.assertEqual(lost["replay"]["requestId"], won["request"]["requestId"])
        self.assertEqual(lost["replay"]["fingerprint"], "fp-winner")
        self.assertIsNone(lost["approval"]["consumedAt"])
        with self.store() as reopened:
            self.assert_replay(reopened, winner_spec, won["request"]["requestId"])
            self.assert_unconsumed(reopened, rival_approval)
            self.assert_capability_ids(reopened, (won["request"]["requestId"],))
            reused = _create_consuming(reopened, dict(rival_spec, key="fresh-key"))
            self.assertIsNotNone(reused, "duplicate key should leave its approval usable")
        with self.store() as reopened:
            self.assertIsNotNone(reopened.get(rival_approval)["consumedAt"])
            self.assertEqual(reopened.by_idempotency_key("fresh-key")["requestId"],
                             reused["requestId"])
            self.assertEqual(reopened.by_idempotency_key("shared-key")["fingerprint"],
                             "fp-winner")
            self.assert_capability_ids(
                reopened, (won["request"]["requestId"], reused["requestId"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
