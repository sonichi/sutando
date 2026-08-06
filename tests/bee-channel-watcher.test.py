#!/usr/bin/env python3
"""Bee watcher (skills/bee-channel): SSE → /v1/ingest forwarding contract.

SHIPPED-PATH discipline: tests run the module's real `run()` loop against a
REAL local HTTP server that serves an SSE stream and a stub /v1/ingest broker
endpoint — the exact wire path production uses (urllib request, SSE parse,
bearer header, cursor file). Only the cursor location is sandboxed.

Run: python3 tests/bee-channel-watcher.test.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

_WATCHER = (Path(__file__).resolve().parent.parent
            / "skills" / "bee-channel" / "scripts" / "bee_watcher.py")

SSE_BODY = (
    b"event: todo-created\n"
    b"id: e1\n"
    b'data: {"id": "t1", "text": "buy milk", "conversation_id": "c9"}\n'
    b"\n"
    b"event: new-utterance\n"
    b"id: e2\n"
    b'data: {"text": "chatty noise that must be filtered"}\n'
    b"\n"
    b"event: todo-updated\n"
    b"id: f:9/x\n"
    b'data: {"id": "t1", "text": "buy oat milk", "conversation_id": "c9"}\n'
    b"\n"
)


def _load():
    spec = importlib.util.spec_from_file_location("bee_watcher_test_mod", _WATCHER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bee_watcher_test_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Server(BaseHTTPRequestHandler):
    ingested: list = []
    auth_seen: list = []
    last_event_id_seen: list = []

    def do_GET(self):
        if self.path == "/v1/events":
            _Server.last_event_id_seen.append(self.headers.get("Last-Event-ID"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(SSE_BODY)
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/v1/ingest":
            n = int(self.headers.get("Content-Length") or 0)
            _Server.ingested.append(json.loads(self.rfile.read(n)))
            _Server.auth_seen.append(self.headers.get("Authorization"))
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"queued": true}')
            return
        self.send_response(404); self.end_headers()

    def log_message(self, *a):  # keep test output clean
        pass


class TestBeeWatcher(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), _Server)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        _Server.ingested, _Server.auth_seen, _Server.last_event_id_seen = [], [], []
        self.mod = _load()
        self.tmp = tempfile.TemporaryDirectory()
        self._cursor = Path(self.tmp.name) / "bee-watcher-cursor.json"
        self._patch = patch.object(self.mod, "_cursor_path", lambda: self._cursor)
        self._patch.start()
        self.cfg = {
            "BEE_PROXY_URL": self.base,
            "BEE_EVENTS_PATH": "/v1/events",
            "BEE_EVENT_TYPES": "todo-created,todo-updated",
            "BEE_BROKER_URL": self.base,
            "BEE_BROKER_TOKEN": "tok-abc",
            "BEE_AGENT_ID": "bee-lane",
        }

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_forwards_selected_events_with_bearer_and_safe_ids(self):
        rc = self.mod.run(self.cfg, once=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(_Server.ingested), 2)          # utterance filtered out
        tid_re = re.compile(r"[A-Za-z0-9._-]{1,64}")        # sparrow id contract
        for post in _Server.ingested:
            self.assertEqual(post["agent_id"], "bee-lane")
            self.assertEqual(post["task"]["source"], "bee")
            self.assertTrue(tid_re.fullmatch(post["task"]["id"]), post["task"]["id"])
        self.assertEqual(_Server.ingested[0]["task"]["id"], "task-bee-e1")
        self.assertEqual(_Server.ingested[0]["task"]["task"], "[Bee todo-created] buy milk")
        self.assertEqual(_Server.ingested[0]["task"]["channel_id"], "c9")
        self.assertNotIn("/", _Server.ingested[1]["task"]["id"])   # "f:9/x" hashed
        self.assertEqual(set(_Server.auth_seen), {"Bearer tok-abc"})

    def test_cursor_persists_and_replays_as_last_event_id(self):
        self.mod.run(self.cfg, once=True)
        cursor = json.loads(self._cursor.read_text())["last_event_id"]
        self.assertEqual(cursor, "f:9/x")                   # raw id in cursor, safe id on wire
        self.mod.run(self.cfg, once=True)
        self.assertEqual(_Server.last_event_id_seen[0], None)
        self.assertEqual(_Server.last_event_id_seen[1], "f:9/x")

    def test_unconfigured_exits_2_not_crash(self):
        with patch.object(sys, "argv", ["bee_watcher.py"]):
            rc = self.mod.main()
        self.assertEqual(rc, 2)

    def test_event_normalizer_falls_back_to_compact_json(self):
        t = self.mod.event_to_task("todo-created", "e9", {"weird": {"nested": 1}})
        self.assertEqual(t["id"], "task-bee-e9")
        self.assertIn('{"weird":{"nested":1}}', t["task"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
