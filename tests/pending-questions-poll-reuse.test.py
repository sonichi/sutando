#!/usr/bin/env python3
"""Pollers of the owner's pending questions reuse one room read instead of opening a socket
per poll. Each fresh gather runs the adapter's `serve`, which opens the room's `db` socket,
so the web client's 3 s /tasks/active poll and the dashboard's 15 s reload must go through
`gather_recent` / `count_recent`: one read per POLL_REUSE_SEC, one in-flight read shared by
concurrent callers, and a write through the reader drops what it held.

Run: python3 tests/pending-questions-poll-reuse.test.py
"""
import importlib.util
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
import sys  # noqa: E402
sys.path.insert(0, str(SRC))
import pending_questions_reader as reader  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class CountingStore(types.ModuleType):
    """An adapter whose every gather stands for one socket opened to the room."""

    def __init__(self, delay=0.0):
        super().__init__("counting_store")
        self.opens = 0
        self.delay = delay
        self.items = [{"id": "q", "ask_id": "ask-1", "title": "Pick one?", "snippet": "", "body": "",
                       "asked_at": None, "priority": "medium", "in_room": True}]
        self._lock = threading.Lock()

    def gather(self, workspace):
        with self._lock:
            self.opens += 1
        if self.delay:
            time.sleep(self.delay)
        return {"waiting": [dict(i) for i in self.items], "done": 0, "pending_close": [],
                "unavailable": False, "reason": None, "link": None, "notes": [], "store": "test"}

    def resolve(self, workspace, ask_id, status):
        self.items = [i for i in self.items if i["ask_id"] != ask_id]
        return True, "closed"

    def reconcile_pass(self, workspace):
        return {"flushed": [], "moved": [], "closed": [], "errors": []}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Reader(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="pq-poll-"))
        self.store = CountingStore()
        self.clock = Clock()
        p = mock.patch.object(reader, "_now", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def test_a_3s_poll_for_a_minute_opens_the_room_once(self):
        for _ in range(20):
            g = reader.gather_recent(self.ws, self.store)
            self.clock.t += 3
        self.assertEqual(g["waiting"][0]["ask_id"], "ask-1")
        self.assertEqual(self.store.opens, 1)

    def test_a_read_older_than_the_window_is_taken_again(self):
        reader.gather_recent(self.ws, self.store)
        self.clock.t += reader.POLL_REUSE_SEC
        reader.gather_recent(self.ws, self.store)
        self.assertEqual(self.store.opens, 2)

    def test_count_recent_is_count_over_the_reused_read(self):
        for _ in range(5):
            c = reader.count_recent(self.ws, self.store)
        self.assertEqual(c, reader.count(self.ws, self.store))
        self.assertEqual(self.store.opens, 2)  # five polls, plus the one fresh `count`

    def test_one_shot_gather_is_never_reused(self):
        for _ in range(3):
            reader.gather(self.ws, self.store)
        self.assertEqual(self.store.opens, 3)

    def test_a_resolve_through_the_reader_drops_the_reused_read(self):
        self.assertEqual(len(reader.gather_recent(self.ws, self.store)["waiting"]), 1)
        reader.resolve(self.ws, "ask-1", "Answered", self.store)
        self.assertEqual(reader.gather_recent(self.ws, self.store)["waiting"], [])
        self.assertEqual(self.store.opens, 2)

    def test_a_reconcile_drops_the_reused_read(self):
        reader.gather_recent(self.ws, self.store)
        reader.reconcile_pass(self.ws, self.store)
        reader.gather_recent(self.ws, self.store)
        self.assertEqual(self.store.opens, 2)

    def test_a_write_during_a_read_keeps_that_read_from_being_reused(self):
        real = self.store.gather

        def gather_then_resolve(ws):
            g = real(ws)
            reader.forget_recent(ws)  # a resolve landed while this read was in flight
            return g
        self.store.gather = gather_then_resolve
        reader.gather_recent(self.ws, self.store)
        self.store.gather = real
        reader.gather_recent(self.ws, self.store)
        self.assertEqual(self.store.opens, 2)

    def test_concurrent_pollers_share_one_read(self):
        self.store.delay = 0.2
        threads = [threading.Thread(target=reader.gather_recent, args=(self.ws, self.store)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.store.opens, 1)

    def test_a_caller_cannot_change_what_the_next_one_gets(self):
        reader.gather_recent(self.ws, self.store)["waiting"].clear()
        self.assertEqual(len(reader.gather_recent(self.ws, self.store)["waiting"]), 1)

    def test_an_unreachable_room_is_reused_too_and_stays_unknown(self):
        self.store.gather = lambda ws: (_ for _ in ()).throw(OSError("room down"))
        for _ in range(5):
            g = reader.gather_recent(self.ws, self.store)
        self.assertTrue(g["unavailable"])
        self.assertIsNone(reader.count_recent(self.ws, self.store)["open"])


class Edges(unittest.TestCase):
    """The polled routes take the reused read; the human-paced ones stay fresh."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="pq-poll-edge-"))
        (self.ws / "tasks").mkdir()
        (self.ws / "results").mkdir()
        self.store = CountingStore()

    def test_the_tasks_active_poll_opens_the_room_once_for_twenty_polls(self):
        api = _load("agent_api_poll", SRC / "agent-api.py")
        saved = (api.WORKSPACE_DIR, api.TASK_DIR, api.RESULT_DIR)
        api.WORKSPACE_DIR, api.TASK_DIR, api.RESULT_DIR = self.ws, self.ws / "tasks", self.ws / "results"
        self.addCleanup(lambda: setattr(api, "WORKSPACE_DIR", saved[0]))
        with mock.patch.object(api, "_pending_store", return_value=self.store):
            for _ in range(20):
                payload = api._active_tasks_payload(True, True)
            self.assertEqual(payload["questions"][0]["id"], "ask-1")
            self.assertEqual(self.store.opens, 1)
            api._questions_queue_payload()
            api._questions_queue_payload()
            self.assertEqual(self.store.opens, 3, "the triage queue is shown by a person: read fresh")

    def test_the_dashboard_reload_opens_the_room_once(self):
        dash = _load("dash_poll", SRC / "dashboard.py")
        with mock.patch.object(dash, "WORKSPACE_DIR", self.ws), \
                mock.patch.object(dash.skill_roots, "declared", return_value=self.store):
            for _ in range(10):
                c = dash.get_pending_count()
        self.assertEqual(c["open"], 1)
        self.assertEqual(self.store.opens, 1)


if __name__ == "__main__":
    unittest.main()
