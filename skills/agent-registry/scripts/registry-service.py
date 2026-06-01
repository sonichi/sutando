#!/usr/bin/env python3
"""Local Agent Registry service.

A standalone, dependency-free local service that tracks running Claude Code
agents. Claude Code instances register themselves on startup (via the
SessionStart hook) and heartbeat while alive; consumers — the Electron overlay,
the dashboard — read the live list over a localhost HTTP API.

This is intentionally NOT the AG2 Workforce Hub app. It is a thin local
service. Hub mirroring (announcing registry state to an AG2 hub via
``ag2_workforce.hub.client``) is an optional, separable extension — see
SKILL.md — and is deliberately left out of this core service.

Design notes:
  * stdlib only (http.server + sqlite3) — no pip install, ever.
  * binds 127.0.0.1 only; never exposed off-host.
  * writes a discovery file so clients find the port without hardcoding it.

API:
  POST /register    {name, cwd, pid, host?, meta?}  -> {id}
  POST /heartbeat   {id}                            -> {ok, status}
  POST /deregister  {id}                            -> {ok}
  GET  /agents                                      -> {agents: [...]}
  GET  /health                                      -> {ok, count, uptime}
"""

import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
DEFAULT_PORT = 7847
PORT_RANGE = 20  # try DEFAULT_PORT .. DEFAULT_PORT+PORT_RANGE

STALE_SECS = 90      # no heartbeat for this long -> "stale"
PRUNE_SECS = 3600    # stopped/stale rows older than this are deleted

STARTED_AT = time.time()

# Shared workspace resolver. See _workspace_resolve.py for the SessionStart-
# hook env-miss case that motivates the .env-grep fallback. Both this script
# and registry-client.py import from the same module so they stay in lock-step.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _workspace_resolve import resolve_workspace  # noqa: E402

WORKSPACE = resolve_workspace()
DB_PATH = os.path.join(WORKSPACE, "data", "agent-registry.db")
DISCOVERY_PATH = os.path.join(WORKSPACE, "state", "agent-registry.json")

_db_lock = threading.Lock()
_db = None


def db():
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id             TEXT PRIMARY KEY,
                name           TEXT NOT NULL,
                cwd            TEXT,
                pid            INTEGER,
                host           TEXT,
                started_at     REAL NOT NULL,
                last_heartbeat REAL NOT NULL,
                status         TEXT NOT NULL,
                meta           TEXT
            )
            """
        )
        _db.commit()
    return _db


def now():
    return time.time()


def make_id(name, pid):
    return f"{name}-{pid}-{int(now())}-{os.urandom(2).hex()}"


def row_to_agent(row):
    age = now() - row["last_heartbeat"]
    status = row["status"]
    if status != "stopped" and age > STALE_SECS:
        status = "stale"
    return {
        "id": row["id"],
        "name": row["name"],
        "cwd": row["cwd"],
        "pid": row["pid"],
        "host": row["host"],
        "started_at": row["started_at"],
        "last_heartbeat": row["last_heartbeat"],
        "heartbeat_age": round(age, 1),
        "status": status,
        "meta": json.loads(row["meta"]) if row["meta"] else {},
    }


def prune(conn):
    cutoff = now() - PRUNE_SECS
    conn.execute(
        "DELETE FROM agents WHERE last_heartbeat < ? "
        "AND status IN ('stopped','stale')",
        (cutoff,),
    )


# --- API operations (all guarded by _db_lock) ----------------------------

def op_register(body):
    name = (body.get("name") or "agent").strip()
    pid = int(body.get("pid") or 0)
    agent_id = body.get("id") or make_id(name, pid)
    ts = now()
    with _db_lock:
        conn = db()
        conn.execute(
            """
            INSERT INTO agents
                (id, name, cwd, pid, host, started_at, last_heartbeat, status, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, cwd=excluded.cwd, pid=excluded.pid,
                host=excluded.host, last_heartbeat=excluded.last_heartbeat,
                status='active', meta=excluded.meta
            """,
            (
                agent_id, name, body.get("cwd"), pid, body.get("host"),
                ts, ts, json.dumps(body.get("meta") or {}),
            ),
        )
        conn.commit()
    return {"ok": True, "id": agent_id}


def op_heartbeat(body):
    agent_id = body.get("id")
    if not agent_id:
        return {"ok": False, "error": "missing id"}, 400
    with _db_lock:
        conn = db()
        cur = conn.execute(
            "UPDATE agents SET last_heartbeat=?, status='active' "
            "WHERE id=? AND status!='stopped'",
            (now(), agent_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "unknown id"}, 404
    return {"ok": True, "status": "active"}


def op_deregister(body):
    agent_id = body.get("id")
    if not agent_id:
        return {"ok": False, "error": "missing id"}, 400
    with _db_lock:
        conn = db()
        conn.execute(
            "UPDATE agents SET status='stopped', last_heartbeat=? WHERE id=?",
            (now(), agent_id),
        )
        conn.commit()
    return {"ok": True}


def op_agents():
    with _db_lock:
        conn = db()
        prune(conn)
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM agents ORDER BY started_at DESC"
        ).fetchall()
    agents = [row_to_agent(r) for r in rows]
    return {"agents": agents, "count": len(agents)}


def op_health():
    with _db_lock:
        conn = db()
        n = conn.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"]
    return {"ok": True, "count": n, "uptime": round(now() - STARTED_AT, 1)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # quiet — this is a background service

    def _send(self, payload, code=200):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path == "/agents":
            self._send(op_agents())
        elif self.path == "/health":
            self._send(op_health())
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        try:
            body = self._body()
            if self.path == "/register":
                self._send(op_register(body))
            elif self.path == "/heartbeat":
                result = op_heartbeat(body)
                self._send(*result) if isinstance(result, tuple) else self._send(result)
            elif self.path == "/deregister":
                result = op_deregister(body)
                self._send(*result) if isinstance(result, tuple) else self._send(result)
            else:
                self._send({"error": "not found"}, 404)
        except Exception as exc:  # never let a bad request kill the thread
            self._send({"error": str(exc)}, 500)


def write_discovery(port):
    os.makedirs(os.path.dirname(DISCOVERY_PATH), exist_ok=True)
    tmp = DISCOVERY_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(
            {
                "host": HOST,
                "port": port,
                "pid": os.getpid(),
                "url": f"http://{HOST}:{port}",
                "started_at": STARTED_AT,
            },
            fh,
        )
    os.replace(tmp, DISCOVERY_PATH)


def remove_discovery():
    try:
        os.unlink(DISCOVERY_PATH)
    except FileNotFoundError:
        pass


def main():
    server = None
    for port in range(DEFAULT_PORT, DEFAULT_PORT + PORT_RANGE):
        try:
            server = ThreadingHTTPServer((HOST, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print("agent-registry: no free port found", file=sys.stderr)
        sys.exit(1)

    bound_port = server.server_address[1]
    write_discovery(bound_port)
    print(f"agent-registry listening on http://{HOST}:{bound_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        remove_discovery()
        server.server_close()


if __name__ == "__main__":
    main()
