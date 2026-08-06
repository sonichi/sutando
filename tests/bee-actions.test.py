#!/usr/bin/env python3
"""bee-actions tool contract: verbs hit the right route+method+body, destructive
verbs refuse without --yes, base resolution prefers cloud bearer over proxy,
and the bearer falls back to the vault. Runs against a stub HTTP server."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

_SCRIPT = (Path(__file__).resolve().parent.parent
           / "skills" / "bee-actions" / "scripts" / "bee_actions.py")


def _load():
    spec = importlib.util.spec_from_file_location("bee_actions_mod", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bee_actions_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Server(BaseHTTPRequestHandler):
    calls: list = []

    def _record(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        _Server.calls.append({
            "method": self.command, "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(body) if body else None,
        })
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true, "todos": [{"id": 1, "completed": false},'
                         b' {"id": 2, "completed": true}]}')

    do_GET = do_POST = do_PUT = do_DELETE = _record

    def log_message(self, *a):
        pass


class TestBeeActions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), _Server)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        _Server.calls = []
        self.mod = _load()

    def _run(self, *argv):
        with patch.object(sys, "argv", ["bee_actions.py", *argv]), \
             patch.dict(os.environ, {"BEE_PROXY_URL": self.base,
                                     "BEE_API_BASE": "", "BEE_API_TOKEN": ""}):
            return self.mod.main()

    def test_complete_todo_puts_completed_true(self):
        self.assertEqual(self._run("complete-todo", "42"), 0)
        c = _Server.calls[-1]
        self.assertEqual((c["method"], c["path"]), ("PUT", "/v1/todos/42"))
        self.assertEqual(c["body"], {"completed": True})

    def test_create_and_edit_todo_bodies(self):
        self.assertEqual(self._run("create-todo", "buy milk"), 0)
        self.assertEqual(_Server.calls[-1]["body"], {"text": "buy milk"})
        self.assertEqual(self._run("edit-todo", "7", "buy oat milk"), 0)
        c = _Server.calls[-1]
        self.assertEqual((c["method"], c["path"]), ("PUT", "/v1/todos/7"))
        self.assertEqual(c["body"], {"text": "buy oat milk"})

    def test_reads_hit_expected_routes(self):
        for argv, path in (
            (("list-todos",), "/v1/todos?limit=20"),
            (("list-conversations",), "/v1/conversations?limit=10"),
            (("get-conversation", "9"), "/v1/conversations/9"),
            (("list-facts",), "/v1/facts?limit=50"),
        ):
            self.assertEqual(self._run(*argv), 0)
            c = _Server.calls[-1]
            self.assertEqual((c["method"], c["path"]), ("GET", path))

    def test_list_todos_filters_completed_unless_all(self):
        import io
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            self.assertEqual(self._run("list-todos"), 0)
        out = json.loads(buf.getvalue())
        self.assertEqual([t["id"] for t in out["todos"]], [1])   # completed filtered
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            self.assertEqual(self._run("list-todos", "--all"), 0)
        out = json.loads(buf.getvalue())
        self.assertEqual([t["id"] for t in out["todos"]], [1, 2])

    def test_destructive_verbs_refuse_without_yes(self):
        # The confirm gate: no HTTP call may fire without --yes.
        self.assertEqual(self._run("delete-todo", "5"), 3)
        self.assertEqual(self._run("delete-fact", "6"), 3)
        self.assertEqual(_Server.calls, [])
        self.assertEqual(self._run("delete-todo", "5", "--yes"), 0)
        c = _Server.calls[-1]
        self.assertEqual((c["method"], c["path"]), ("DELETE", "/v1/todos/5"))

    def test_cloud_bearer_wins_over_proxy(self):
        with patch.object(sys, "argv", ["bee_actions.py", "list-todos"]), \
             patch.dict(os.environ, {"BEE_PROXY_URL": "http://127.0.0.1:1",
                                     "BEE_API_BASE": self.base,
                                     "BEE_API_TOKEN": "tok-cloud"}):
            self.assertEqual(self.mod.main(), 0)
        self.assertEqual(_Server.calls[-1]["auth"], "Bearer tok-cloud")

    def test_vault_fallback_for_bearer(self):
        import types
        stub = types.ModuleType("vault_intercept")
        stub.get_vault_key = lambda k: {"BEE_API_TOKEN": "tok-vault"}[k]
        with patch.dict(sys.modules, {"vault_intercept": stub}), \
             patch.object(sys, "argv", ["bee_actions.py", "list-todos"]), \
             patch.dict(os.environ, {"BEE_PROXY_URL": "",
                                     "BEE_API_BASE": self.base,
                                     "BEE_API_TOKEN": ""}):
            self.assertEqual(self.mod.main(), 0)
        self.assertEqual(_Server.calls[-1]["auth"], "Bearer tok-vault")

    def test_unconfigured_exits_2(self):
        with patch.object(sys, "argv", ["bee_actions.py", "list-todos"]), \
             patch.dict(os.environ, {"BEE_PROXY_URL": "", "BEE_API_BASE": "",
                                     "BEE_API_TOKEN": ""}):
            self.assertEqual(self.mod.main(), 2)
        self.assertEqual(_Server.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
