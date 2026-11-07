#!/usr/bin/env python3
"""Recovering a refused `move` to an s.space row, end to end over agent-api's connections routes.

An install whose every row is on ag2.space (the sspace-migration warning's case) and whose
`move` is refused: replacing the ag2.space row's token with an s.space one recovers nothing
(legacy_row_unmanaged / token_server_mismatch), while adding the s.space token as a new
connection and promoting it leaves an s.space row that is the primary. A REAL HTTP server on an
ephemeral port; the vault, the gateway probe, the resolver and the bridge spawn are fakes.

Run: python3 tests/connections-move-recovery.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

import http.client
import http.server
import importlib.util
import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path

# Before agent-api.py is imported: its WORKSPACE_DIR is read at import.
_WS = tempfile.mkdtemp(prefix="connections-move-recovery-")
isolate_os_environ(SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=_WS, SUTANDO_TELEMETRY="0",
                   SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1", SUTANDO_BRIDGE_LOCK="0")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


api = _load("agent_api", REPO / "src" / "agent-api.py")
import connections as C  # noqa: E402
import dotspace  # noqa: E402

WS = Path(_WS)
assert api.WORKSPACE_DIR == WS.resolve(), api.WORKSPACE_DIR
api.API_TOKEN = ""

REGISTRY = {"instances": [
    {"server": "s.space", "gateway_host": "chat.s.space", "mcp_url": "https://mcp.s.space/mcp"},
    {"server": "ag2.space", "gateway_host": "chat.ag2.space", "mcp_url": "https://mcp.ag2.space/mcp"}]}
C.DEFAULT_RESOLVER = lambda host: dotspace.dotspace_for(host, fetch=dotspace.NO_FETCH, config=REGISTRY)

OLD_SECRET = "secret-old-ag2"
AG2_TOKEN = "https://chat.ag2.space/relay|" + OLD_SECRET
NEW_SECRET = "secret-new-sspace"
SSPACE_TOKEN = "https://chat.s.space/relay|" + NEW_SECRET
DEAD_PID = 4_194_404  # above any pid_max: the fake bridge never runs


class FakeVault:
    store = {}

    def get(self, key):
        return self.store[key]

    def set(self, key, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


def fetch(url, token, address=None):
    """s.space knows the agent only by the token it issued; the copied ag2.space secret is rejected."""
    host, secret = C.host_of_url(url), C.secret_of(token)
    if host == "chat.s.space":
        if secret != NEW_SECRET:
            raise urllib.error.HTTPError(url, 401, "nope", {}, None)
        return {"agents": [{"id": "@sutando-x:s.space"}]}
    return {"agents": [{"id": "@sutando-x:ag2.space"}]}


def spawn(argv, env, log_path):
    status = WS / "state" / f"gateway-status.{env['GATEWAY_INSTANCE']}.json"
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps({"pid": DEAD_PID, "connected": True}))
    return DEAD_PID


def public_getaddrinfo(host, port, **kw):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


_real = api.connections_http
api.connections_http = lambda method, path, body, query=None: _real(
    method, path, body, query, vault=FakeVault(), fetch=fetch, spawn=spawn, kill=lambda *a: None,
    getaddrinfo=public_getaddrinfo, whoami=lambda *a, **k: None)

server = http.server.HTTPServer(("127.0.0.1", 0), api.Handler)
server.timeout = 0.5
PORT = server.server_address[1]
BEARER = api.connections_bearer()


def req(method, path, body=None):
    out = {}

    def go():
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={
            "Host": f"127.0.0.1:{PORT}", "Authorization": f"Bearer {BEARER}", "Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        out["v"] = (resp.status, json.loads(raw) if raw else None)
    t = threading.Thread(target=go)
    t.start()
    while t.is_alive():
        server.handle_request()
    t.join()
    return out["v"]


class RecoverARefusedMove(unittest.TestCase):
    def setUp(self):
        for child in WS.iterdir():
            if child.name != "state":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        for child in (WS / "state").iterdir():
            if child.name != api.local_api_gate.LOCAL_API_TOKEN_NAME:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        FakeVault.store = {}

    def rows(self):
        status, body = req("GET", "/api/v1/connections")
        self.assertEqual(status, 200, body)
        return {r["instance"]: r for r in body["connections"]}

    def refused_move(self):
        self.assertEqual({r["server"] for r in self.rows().values()}, {"ag2.space"}, "every row is on ag2.space")
        status, out = req("POST", "/api/v1/connections/move", {})
        self.assertEqual((status, out["ok"]), (200, False), out)

    def recover(self, old):
        status, out = req("POST", "/api/v1/connections", {"token": SSPACE_TOKEN})
        self.assertEqual((status, out["preview"]["server"]), (200, "s.space"), out)
        status, out = req("POST", "/api/v1/connections", {"token": SSPACE_TOKEN, "confirm": True,
                                                          "expected_server": "s.space", "added_by": "ui"})
        self.assertEqual((status, out["created"], out["connection"]["instance"]), (201, True, "s-space"), out)
        status, out = req("POST", "/api/v1/connections/s-space/promote", {"park": True})
        self.assertEqual((status, out["previous"]), (200, old), out)
        rows = self.rows()
        self.assertEqual((rows["s-space"]["server"], rows["s-space"]["primary"]), ("s.space", True))
        self.assertEqual(rows["s-space"]["agent_mxid"], "@sutando-x:s.space")
        self.assertEqual([i for i, r in rows.items() if r["primary"]], ["s-space"])
        self.assertTrue(rows[old]["parked"], "the ag2.space row is parked")
        self.assertEqual(FakeVault.store["AG2_REMOTE_TOKEN_S_SPACE"], SSPACE_TOKEN)

    def test_legacy_row(self):
        C.import_from_env({"REMOTE_TASK_TOKEN": AG2_TOKEN}, workspace=WS, vault=FakeVault(),
                          resolver=C.DEFAULT_RESOLVER)
        self.refused_move()
        status, out = req("POST", "/api/v1/connections/legacy/token", {"token": SSPACE_TOKEN})
        self.assertEqual((status, out["error"]), (409, "legacy_row_unmanaged"), "replacing its token recovers nothing")
        self.recover("")

    def test_named_row(self):
        status, out = req("POST", "/api/v1/connections", {"token": AG2_TOKEN, "confirm": True,
                                                          "expected_server": "ag2.space", "added_by": "ui"})
        self.assertEqual(status, 201, out)
        old = out["connection"]["instance"]
        self.refused_move()
        status, out = req("POST", f"/api/v1/connections/{old}/token", {"token": SSPACE_TOKEN})
        self.assertEqual((status, out["error"]), (409, "token_server_mismatch"), "replacing its token recovers nothing")
        self.recover(old)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        server.server_close()
        shutil.rmtree(_WS, ignore_errors=True)
