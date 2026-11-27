#!/usr/bin/env python3
"""Evaluate the shared RPC client's lifecycle through real, fictional stdio children.

Children control response order and EOF with protocol handshakes, never scheduling sleeps.
These are local transport checks, not authenticated Codex or backend-delivery evidence.
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from _helpers.hermetic_env import hermetic_env

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from codex_rpc import Rpc  # noqa: E402

CHILD = r'''
import json
import sys
import threading


def receive():
    return json.loads(sys.stdin.readline())


def emit(message):
    print(json.dumps(message), flush=True)


mode, label, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
if mode in ("batch", "eof"):
    requests = [receive() for _ in range(count)]
    emit({"method": "accepted", "params": {"count": len(requests)}})
    if mode == "eof":
        sys.exit(0)
    print('not json\n[]\nnull\n42', flush=True)
    emit({"id": min(request["id"] for request in requests), "method": "server/question",
          "params": {"label": label}})
    for request in reversed(requests):
        emit({"jsonrpc": "2.0", "id": request["id"], "result": {
            "label": label, "token": request["params"]["token"]}})
    emit({"method": "batch/done"})
elif mode == "late":
    old = receive()
    emit({"method": "held", "params": {"id": old["id"]}})
    current = receive()
    emit({"id": old["id"], "result": {"token": "expired"}})
    emit({"id": 99999, "result": {"token": "unsolicited"}})
    emit({"id": current["id"], "result": {"token": current["params"]["token"]}})
    emit({"method": "late/drained"})
elif mode == "stall":
    receive()
    emit({"method": "parked"})
    threading.Event().wait()

for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "error":
        emit({"jsonrpc": "2.0", "id": request["id"], "error": {
            "code": -32001, "message": "fictional refusal", "data": {"retry": False}}})
    else:
        emit({"id": request["id"], "result": request["params"]})
emit({"method": "stdin/closed"})
'''


class Lifecycle(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="rpc-lifecycle-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    @contextlib.contextmanager
    def child(self, mode="echo", label="a", count=1):
        rpc = Rpc([sys.executable, "-u", "-c", CHILD, mode, label, str(count)],
                  env=hermetic_env(home=self.root), cwd=str(self.root))
        try:
            yield rpc
        finally:
            self.close_checked(rpc)

    def close_checked(self, rpc):
        errors = []

        def close():
            try:
                rpc.close()
            except Exception as error:
                errors.append(error)

        closer = threading.Thread(target=close, daemon=True)
        closer.start()
        closer.join(timeout=12)
        stuck = closer.is_alive()
        if rpc.p.poll() is None:
            rpc.p.kill()
            rpc.p.wait(timeout=3)
        closer.join(timeout=3)
        self.assertFalse(stuck, "close did not finish; test watchdog killed the owned child")
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(rpc.alive())
        self.assertFalse(rpc.reader.is_alive())
        self.assertTrue(rpc.p.stdin.closed)
        self.assertTrue(rpc.p.stdout.closed)
        self.assertEqual(rpc.pending, {})

    def event(self, rpc):
        return rpc.events.get(timeout=8)

    def test_concurrent_calls_match_reversed_responses_and_isolate_clients(self):
        count = 6
        with self.child("batch", "a", count) as a, self.child("batch", "b", count) as b:
            with ThreadPoolExecutor(max_workers=count * 2) as pool:
                calls = [(label, token, pool.submit(rpc.call, "echo", {"token": token}, 8))
                         for label, rpc in (("a", a), ("b", b)) for token in range(count)]
                ids = {"a": [], "b": []}
                for label, token, future in calls:
                    response = future.result(timeout=12)
                    self.assertEqual(response["result"], {"label": label, "token": token})
                    self.assertEqual(response["jsonrpc"], "2.0")
                    ids[label].append(response["id"])
            for label, rpc in (("a", a), ("b", b)):
                self.assertEqual(sorted(ids[label]), list(range(1, count + 1)))
                self.assertEqual(self.event(rpc), {"method": "accepted", "params": {"count": count}})
                self.assertEqual(self.event(rpc), {"id": 1, "method": "server/question",
                                                  "params": {"label": label}})
                self.assertEqual(self.event(rpc), {"method": "batch/done"})
                self.assertTrue(rpc.events.empty())
                self.assertEqual(rpc.pending, {})
                self.assertEqual(rpc.request("echo", {"still": label}, timeout=8), {"still": label})

    def test_timeout_retires_waiter_and_late_response_cannot_answer_the_next_call(self):
        with self.child("late") as rpc:
            with self.assertRaisesRegex(RuntimeError, "hold: no response in 0s"):
                rpc.call("hold", {"token": "old"}, timeout=0)
            self.assertEqual(self.event(rpc), {"method": "held", "params": {"id": 1}})
            self.assertEqual(rpc.pending, {})
            self.assertEqual(rpc.request("next", {"token": "new"}, timeout=8), {"token": "new"})
            self.assertEqual(self.event(rpc), {"method": "late/drained"})
            self.assertTrue(rpc.events.empty(), "orphan responses must not become server events")
            self.assertEqual(rpc.pending, {})

    def test_child_eof_fans_out_to_every_pending_call(self):
        count = 4
        with self.child("eof", count=count) as rpc:
            with ThreadPoolExecutor(max_workers=count) as pool:
                calls = [pool.submit(rpc.call, "unanswered", {"token": token}, 3)
                         for token in range(count)]
                for future in calls:
                    self.assertEqual(future.result(timeout=8), {"error": {"message": "app-server exited"}})
            self.assertEqual(self.event(rpc), {"method": "accepted", "params": {"count": count}})
            self.assertIsNone(self.event(rpc))
            rpc.reader.join(timeout=3)
            self.assertFalse(rpc.reader.is_alive())
            self.assertEqual(rpc.p.wait(timeout=3), 0)
            self.assertEqual(rpc.pending, {})

    def test_error_envelope_and_request_exception_leave_the_client_usable(self):
        with self.child() as rpc:
            response = rpc.call("error", timeout=8)
            self.assertEqual(response, {"jsonrpc": "2.0", "id": 1, "error": {
                "code": -32001, "message": "fictional refusal", "data": {"retry": False}}})
            with self.assertRaisesRegex(RuntimeError, "error: .*fictional refusal"):
                rpc.request("error", timeout=8)
            self.assertEqual(rpc.request("echo", {"token": "after-error"}, timeout=8),
                             {"token": "after-error"})
            self.assertEqual(rpc.pending, {})
            self.assertTrue(rpc.events.empty())

    def test_orderly_close_reaps_child_and_failed_writes_release_waiters(self):
        with self.child() as rpc:
            self.assertEqual(rpc.request("echo", {"token": "before-close"}, timeout=8),
                             {"token": "before-close"})
            self.close_checked(rpc)
            self.assertEqual(rpc.p.returncode, 0)
            self.assertEqual(self.event(rpc), {"method": "stdin/closed"})
            self.assertIsNone(self.event(rpc))
            self.assertFalse(rpc.send({"method": "too-late"}))
            with self.assertRaisesRegex(RuntimeError, "too-late: app-server pipe closed"):
                rpc.call("too-late", timeout=8)
            self.assertEqual(rpc.pending, {})

    def test_close_kills_a_child_that_does_not_read_stdin_eof(self):
        with self.child("stall") as rpc:
            self.assertTrue(rpc.send({"method": "park"}))
            self.assertEqual(self.event(rpc), {"method": "parked"})
            self.assertTrue(rpc.alive())
            self.close_checked(rpc)
            self.assertNotEqual(rpc.p.returncode, 0)
            self.assertIsNone(self.event(rpc))


if __name__ == "__main__":
    unittest.main(verbosity=2)
