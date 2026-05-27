#!/usr/bin/env python3
"""Tests for skills/agent-registry — HTTP service + client utility functions.

Run: python3 tests/agent-registry.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "skills" / "agent-registry" / "scripts"
sys.path.insert(0, str(SKILLS))

# Patch SUTANDO_WORKSPACE to a temp dir before importing the service module.
_tmpdir = tempfile.mkdtemp(prefix="agent-registry-test-")
os.environ.setdefault("SUTANDO_WORKSPACE", _tmpdir)

import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location("registry_service", SKILLS / "registry-service.py")
svc = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(svc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _start_service_thread(host="127.0.0.1"):
    """Bind a free port and start the registry service in a daemon thread.
    Returns the bound port."""
    import socket
    with socket.socket() as s:
        s.bind((host, 0))
        port = s.getsockname()[1]

    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer((host, port), svc.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Brief settle time for the server to accept connections.
    time.sleep(0.05)
    return port, server


def _http(method, url, payload=None, timeout=4):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


# ---------------------------------------------------------------------------
# Unit tests (pure function calls — no HTTP)
# ---------------------------------------------------------------------------

class TestRegistryPureFunctions(unittest.TestCase):
    """Tests for op_register, op_heartbeat, op_deregister, op_agents."""

    def setUp(self):
        # Give each test a fresh in-memory database.
        svc._db = None
        svc.DB_PATH = os.path.join(_tmpdir, "data", f"test-{id(self)}.db")
        os.makedirs(os.path.dirname(svc.DB_PATH), exist_ok=True)

    def test_register_creates_active_agent(self):
        result = svc.op_register({"name": "claude", "pid": 1234, "cwd": "/tmp"})
        self.assertTrue(result.get("ok"))
        agent_id = result["id"]
        agents = svc.op_agents()["agents"]
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["status"], "active")
        self.assertEqual(agents[0]["id"], agent_id)

    def test_register_idempotent_with_same_id(self):
        svc.op_register({"name": "claude", "pid": 1, "id": "static-id"})
        svc.op_register({"name": "claude-v2", "pid": 2, "id": "static-id"})
        agents = svc.op_agents()["agents"]
        self.assertEqual(len(agents), 1, "Re-register with same id should upsert, not duplicate")
        self.assertEqual(agents[0]["name"], "claude-v2")

    def test_heartbeat_updates_timestamp(self):
        res = svc.op_register({"name": "hb-test", "pid": 99})
        agent_id = res["id"]
        t0 = svc.db().execute("SELECT last_heartbeat FROM agents WHERE id=?", (agent_id,)).fetchone()[0]
        time.sleep(0.05)
        hb = svc.op_heartbeat({"id": agent_id})
        self.assertTrue(hb.get("ok"))
        t1 = svc.db().execute("SELECT last_heartbeat FROM agents WHERE id=?", (agent_id,)).fetchone()[0]
        self.assertGreater(t1, t0, "last_heartbeat must advance after heartbeat call")

    def test_heartbeat_missing_id_returns_error(self):
        result, code = svc.op_heartbeat({})
        self.assertFalse(result.get("ok"))
        self.assertEqual(code, 400)

    def test_deregister_marks_stopped(self):
        res = svc.op_register({"name": "dereg-test", "pid": 77})
        agent_id = res["id"]
        svc.op_deregister({"id": agent_id})
        row = svc.db().execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone()
        self.assertEqual(row["status"], "stopped")

    def test_agents_list_returns_all_active(self):
        svc.op_register({"name": "a1", "pid": 1})
        svc.op_register({"name": "a2", "pid": 2})
        agents = svc.op_agents()["agents"]
        names = {a["name"] for a in agents}
        self.assertIn("a1", names)
        self.assertIn("a2", names)

    def test_stale_detection_via_row_to_agent(self):
        import sqlite3
        conn = svc.db()
        stale_ts = svc.now() - svc.STALE_SECS - 10
        conn.execute(
            "INSERT INTO agents (id, name, cwd, pid, host, started_at, last_heartbeat, status, meta)"
            " VALUES ('stale-id','stale',NULL,0,NULL,?,?,'active','{}')",
            (stale_ts, stale_ts),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM agents WHERE id='stale-id'").fetchone()
        agent = svc.row_to_agent(row)
        self.assertEqual(agent["status"], "stale")

    def test_prune_removes_old_stopped_rows(self):
        conn = svc.db()
        old_ts = svc.now() - svc.PRUNE_SECS - 10
        conn.execute(
            "INSERT INTO agents (id, name, cwd, pid, host, started_at, last_heartbeat, status, meta)"
            " VALUES ('old-stopped','old',NULL,0,NULL,?,?,'stopped','{}')",
            (old_ts, old_ts),
        )
        conn.commit()
        svc.prune(conn)
        conn.commit()
        row = conn.execute("SELECT id FROM agents WHERE id='old-stopped'").fetchone()
        self.assertIsNone(row, "prune() must delete old stopped agents")


# ---------------------------------------------------------------------------
# Integration tests (live HTTP server)
# ---------------------------------------------------------------------------

class TestRegistryHTTP(unittest.TestCase):
    """End-to-end tests via the real HTTP handler."""

    @classmethod
    def setUpClass(cls):
        # Fresh DB for the HTTP tests.
        svc._db = None
        svc.DB_PATH = os.path.join(_tmpdir, "data", "http-test.db")
        os.makedirs(os.path.dirname(svc.DB_PATH), exist_ok=True)
        cls.port, cls.server = _start_service_thread()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_health_endpoint(self):
        resp = _http("GET", f"{self.base}/health")
        self.assertTrue(resp.get("ok"))
        self.assertIn("uptime", resp)
        self.assertIn("count", resp)

    def test_register_via_http(self):
        resp = _http("POST", f"{self.base}/register",
                     {"name": "http-agent", "pid": 555, "cwd": "/tmp"})
        self.assertTrue(resp.get("ok"))
        self.assertIn("id", resp)

    def test_agents_via_http(self):
        _http("POST", f"{self.base}/register", {"name": "list-agent", "pid": 666})
        resp = _http("GET", f"{self.base}/agents")
        self.assertIn("agents", resp)
        names = [a["name"] for a in resp["agents"]]
        self.assertIn("list-agent", names)

    def test_heartbeat_via_http(self):
        r = _http("POST", f"{self.base}/register", {"name": "hb-http", "pid": 777})
        agent_id = r["id"]
        hb = _http("POST", f"{self.base}/heartbeat", {"id": agent_id})
        self.assertTrue(hb.get("ok"))

    def test_deregister_via_http(self):
        r = _http("POST", f"{self.base}/register", {"name": "dereg-http", "pid": 888})
        agent_id = r["id"]
        dr = _http("POST", f"{self.base}/deregister", {"id": agent_id})
        self.assertTrue(dr.get("ok"))

    def test_unknown_route_returns_404(self):
        try:
            _http("GET", f"{self.base}/nonexistent")
            self.fail("Expected HTTP error")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
