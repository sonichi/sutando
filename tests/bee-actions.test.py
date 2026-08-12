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
        parsed = json.loads(body) if body else None
        _Server.calls.append({
            "method": self.command, "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": parsed,
        })
        if self.path.startswith("/gwfail") or self.path.endswith("/boom"):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "stub failure"}')
            return
        self.send_response(200)
        self.end_headers()
        if self.path == "/v1/conversations/rawbody":
            self.wfile.write(b"plain text, not json")
        elif self.command == "GET" and self.path.startswith("/v1/todos/"):
            self.wfile.write(b'{"id": 42, "text": "stub todo", "completed": true}')
        elif self.path == "/v1/room" and isinstance(parsed, dict) \
                and parsed.get("op") == "create":
            if parsed.get("invite") == ["@noroom:stub"]:
                self.wfile.write(b'{}')          # create "succeeds" without an id
            else:
                self.wfile.write(b'{"room_id": "!created:stub"}')
        else:
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
        import tempfile
        _Server.calls = []
        self.mod = _load()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

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

    def test_complete_todo_how_appends_done_note(self):
        # --how: complete first, then read the text, then rewrite it with the
        # " — done:" record (Bee has no notes field; the text IS the record).
        self.assertEqual(self._run("complete-todo", "42", "--how", "sent via Discord"), 0)
        methods = [(c["method"], c["path"]) for c in _Server.calls]
        self.assertEqual(methods, [("PUT", "/v1/todos/42"),
                                   ("GET", "/v1/todos/42"),
                                   ("PUT", "/v1/todos/42")])
        self.assertEqual(_Server.calls[0]["body"], {"completed": True})
        self.assertEqual(_Server.calls[2]["body"],
                         {"text": "stub todo — done: sent via Discord"})

    def _run_room(self, *argv, env=None):
        import io
        e = {"REMOTE_TASK_TOKEN": f"{self.base}|tok-gw",
             "BEE_ROOM_ID": "", "BEE_ROOM_OWNER": ""}
        e.update(env or {})
        buf = io.StringIO()
        with patch.object(sys, "argv", ["bee_actions.py", *argv]), \
             patch.dict(os.environ, e), \
             patch.object(self.mod, "_room_state_path",
                          lambda: Path(self.tmpdir.name) / "bee-room.json"), \
             patch.object(sys, "stdout", buf):
            rc = self.mod.main()
        return rc, buf.getvalue()

    def test_post_room_posts_op_message_with_bearer(self):
        rc, _out = self._run_room("post-room", "hello from sutando",
                                  env={"BEE_ROOM_ID": "!bee:ag2.space"})
        self.assertEqual(rc, 0)
        c = _Server.calls[-1]
        self.assertEqual((c["method"], c["path"]), ("POST", "/v1/room"))
        self.assertEqual(c["auth"], "Bearer tok-gw")
        self.assertEqual(c["body"], {"op": "message", "room_id": "!bee:ag2.space",
                                     "body": "hello from sutando"})

    def test_post_room_auto_registers_dm_room_when_owner_known(self):
        # No room anywhere + BEE_ROOM_OWNER set -> op:create with the owner
        # invited (the DM-by-default behavior), id persisted, then the message.
        rc, _out = self._run_room("post-room", "first post",
                                  env={"BEE_ROOM_OWNER": "@owner:ag2.space"})
        self.assertEqual(rc, 0)
        create, msg = _Server.calls[-2], _Server.calls[-1]
        self.assertEqual(create["body"], {"op": "create", "name": "Sutando · Bee",
                                          "invite": ["@owner:ag2.space"]})
        self.assertEqual(msg["body"]["op"], "message")
        self.assertEqual(msg["body"]["room_id"], "!created:stub")
        persisted = json.loads((Path(self.tmpdir.name) / "bee-room.json").read_text())
        self.assertEqual(persisted["room_id"], "!created:stub")

    def test_post_room_without_room_or_owner_exits_2(self):
        rc, _out = self._run_room("post-room", "orphan message")
        self.assertEqual(rc, 2)
        self.assertEqual(_Server.calls, [])

    def test_register_room_records_user_picked_room(self):
        rc, out = self._run_room("register-room", "--room", "!mine:ag2.space")
        self.assertEqual(rc, 0)
        self.assertEqual(_Server.calls, [])          # no create for existing
        persisted = json.loads((Path(self.tmpdir.name) / "bee-room.json").read_text())
        self.assertEqual(persisted["room_id"], "!mine:ag2.space")
        self.assertIn("existing", out)

    def test_delete_fact_route_and_http_error_exit(self):
        # delete-fact --yes hits its route; a server 5xx exits 1 via the
        # HTTPError handler instead of crashing.
        self.assertEqual(self._run("delete-fact", "6", "--yes"), 0)
        c = _Server.calls[-1]
        self.assertEqual((c["method"], c["path"]), ("DELETE", "/v1/facts/6"))
        self.assertEqual(self._run("delete-fact", "boom", "--yes"), 1)
        self.assertEqual(self._run("delete-todo", "boom", "--yes"), 1)

    def test_non_json_response_wrapped_as_raw(self):
        import io
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            self.assertEqual(self._run("get-conversation", "rawbody"), 0)
        self.assertEqual(json.loads(buf.getvalue()), {"raw": "plain text, not json"})

    def test_vault_import_failure_falls_back_to_proxy(self):
        import types
        stub = types.ModuleType("vault_intercept")
        def _boom(k):
            raise RuntimeError("keychain unavailable")
        stub.get_vault_key = _boom
        with patch.dict(sys.modules, {"vault_intercept": stub}), \
             patch.object(sys, "argv", ["bee_actions.py", "list-todos"]), \
             patch.dict(os.environ, {"BEE_PROXY_URL": self.base,
                                     "BEE_API_BASE": "https://cloud.example",
                                     "BEE_API_TOKEN": ""}):
            self.assertEqual(self.mod.main(), 0)
        self.assertIsNone(_Server.calls[-1]["auth"])   # proxy path, no bearer

    def test_room_verbs_without_gateway_creds_exit_2(self):
        rc, _ = self._run_room("post-room", "msg",
                               env={"REMOTE_TASK_TOKEN": "", "GATEWAY_TOKEN": "",
                                    "RELAY_TOKEN": "", "AG2_REMOTE_TOKEN": ""})
        self.assertEqual(rc, 2)
        self.assertEqual(_Server.calls, [])

    def test_register_room_create_without_id_exits_1(self):
        rc, _ = self._run_room("register-room", "--invite", "@noroom:stub")
        self.assertEqual(rc, 1)

    def test_post_room_gateway_http_and_network_errors_exit_1(self):
        rc, _ = self._run_room("post-room", "msg",
                               env={"REMOTE_TASK_TOKEN": f"{self.base}/gwfail|tok",
                                    "BEE_ROOM_ID": "!bee:ag2.space"})
        self.assertEqual(rc, 1)                        # HTTPError branch
        rc, _ = self._run_room("post-room", "msg",
                               env={"REMOTE_TASK_TOKEN": "http://127.0.0.1:1|tok",
                                    "BEE_ROOM_ID": "!bee:ag2.space"})
        self.assertEqual(rc, 1)                        # URLError branch

    def test_register_room_zero_args_refuses_ownerless_create(self):
        # The review repro: no --room, no --invite, no BEE_ROOM_OWNER must
        # fail BEFORE any HTTP call — an uninvited room is owner-invisible.
        rc, _ = self._run_room("register-room")
        self.assertEqual(rc, 2)
        self.assertEqual(_Server.calls, [])

    def test_register_room_create_success_prints_created(self):
        rc, out = self._run_room("register-room", "--invite", "@owner:ag2.space")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"room_id": "!created:stub",
                                           "registered": "created"})

    def test_bee_api_network_error_exits_1(self):
        with patch.object(sys, "argv", ["bee_actions.py", "list-todos"]), \
             patch.dict(os.environ, {"BEE_PROXY_URL": "http://127.0.0.1:1",
                                     "BEE_API_BASE": "", "BEE_API_TOKEN": ""}):
            self.assertEqual(self.mod.main(), 1)

    def _with_manifest(self, config):
        mf = Path(self.tmpdir.name) / "manifest.json"
        mf.write_text(json.dumps({"name": "bee-actions", "config": config}))
        return patch.object(self.mod, "_MANIFEST", mf)

    def test_manifest_config_reaches_the_script_runner(self):
        # SKILL.md sends users to the manifest config block — a manifest-only
        # install must work with no env and no CLI flags (the Codex P1).
        with self._with_manifest({"BEE_PROXY_URL": self.base}), \
             patch.object(sys, "argv", ["bee_actions.py", "list-todos"]), \
             patch.dict(os.environ, {"BEE_PROXY_URL": "", "BEE_API_BASE": "",
                                     "BEE_API_TOKEN": ""}):
            self.assertEqual(self.mod.main(), 0)
        self.assertEqual(_Server.calls[-1]["path"], "/v1/todos?limit=20")

    def test_manifest_supplies_room_id_for_post_room(self):
        with self._with_manifest({"BEE_ROOM_ID": "!frommanifest:ag2.space"}):
            rc, _ = self._run_room("post-room", "hi")
        self.assertEqual(rc, 0)
        self.assertEqual(_Server.calls[-1]["body"]["room_id"], "!frommanifest:ag2.space")

    def test_env_overrides_manifest(self):
        # Precedence is CLI > env > manifest: a live env value must win over
        # a conflicting manifest value.
        with self._with_manifest({"BEE_PROXY_URL": "http://127.0.0.1:1"}), \
             patch.object(sys, "argv", ["bee_actions.py", "list-todos"]), \
             patch.dict(os.environ, {"BEE_PROXY_URL": self.base, "BEE_API_BASE": "",
                                     "BEE_API_TOKEN": ""}):
            self.assertEqual(self.mod.main(), 0)   # stub answered -> env won

    def test_room_state_path_resolves_under_workspace(self):
        # The real (unpatched) state-path helper must resolve to
        # <workspace>/state/bee-room.json via workspace_default.
        p = self.mod._room_state_path()
        self.assertTrue(str(p).endswith("state/bee-room.json"), p)

    def test_unconfigured_exits_2(self):
        with patch.object(sys, "argv", ["bee_actions.py", "list-todos"]), \
             patch.dict(os.environ, {"BEE_PROXY_URL": "", "BEE_API_BASE": "",
                                     "BEE_API_TOKEN": ""}):
            self.assertEqual(self.mod.main(), 2)
        self.assertEqual(_Server.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
