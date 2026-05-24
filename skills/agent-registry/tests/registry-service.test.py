#!/usr/bin/env python3
"""
Tests for skills/agent-registry/scripts/registry-service.py

Coverage:
  resolve_workspace — env var override, default path, tilde expansion
  make_id           — format, uniqueness
  row_to_agent      — active fresh, stale detection, stopped stays stopped, meta parsing
  prune             — removes old stopped/stale, keeps recent, keeps active
  op_register       — inserts agent, returns id; upsert on id conflict
  op_heartbeat      — updates heartbeat; missing id → error; unknown id → 404
  op_deregister     — marks stopped; missing id → error
  op_agents         — returns list, calls prune
  op_health         — count and uptime

Run: python3 skills/agent-registry/tests/registry-service.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent.parent.parent

# Must set SUTANDO_WORKSPACE before module loads (module-level resolve_workspace call)
_tmpdir = tempfile.mkdtemp()
os.makedirs(os.path.join(_tmpdir, "state"), exist_ok=True)
os.makedirs(os.path.join(_tmpdir, "data"), exist_ok=True)
with patch.dict(os.environ, {"SUTANDO_WORKSPACE": _tmpdir}):
    _spec = importlib.util.spec_from_file_location(
        "registry_service",
        REPO / "skills" / "agent-registry" / "scripts" / "registry-service.py",
    )
    rs = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(rs)


_SCHEMA = """
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


def _make_conn():
    """Return a fresh in-memory SQLite connection with the agents schema."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def _insert(conn, *, agent_id="test-id", name="test-agent", cwd="/tmp",
            pid=1234, host="localhost", started_at=None, last_heartbeat=None,
            status="active", meta=None):
    ts = time.time()
    conn.execute(
        "INSERT INTO agents VALUES (?,?,?,?,?,?,?,?,?)",
        (
            agent_id, name, cwd, pid, host,
            started_at or ts,
            last_heartbeat or ts,
            status,
            json.dumps(meta or {}),
        ),
    )
    conn.commit()


def _fetch_row(conn, agent_id="test-id"):
    return conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()


class _ServiceTest(unittest.TestCase):
    """Base class: injects fresh in-memory db into rs._db for each test."""

    def setUp(self):
        self._conn = _make_conn()
        self._orig_db = rs._db
        rs._db = self._conn

    def tearDown(self):
        rs._db = self._orig_db
        self._conn.close()


# ── resolve_workspace ─────────────────────────────────────────────────────────

class TestResolveWorkspace(unittest.TestCase):
    def test_env_var_used_when_set(self):
        with patch.dict(os.environ, {"SUTANDO_WORKSPACE": "/tmp/my-ws"}):
            self.assertEqual(rs.resolve_workspace(), "/tmp/my-ws")

    def test_default_when_no_env(self):
        env = {k: v for k, v in os.environ.items() if k != "SUTANDO_WORKSPACE"}
        with patch.dict(os.environ, env, clear=True):
            result = rs.resolve_workspace()
        self.assertIn(".sutando/workspace", result)

    def test_tilde_expanded(self):
        with patch.dict(os.environ, {"SUTANDO_WORKSPACE": "~/.sutando/workspace"}):
            result = rs.resolve_workspace()
        self.assertNotIn("~", result)


# ── make_id ───────────────────────────────────────────────────────────────────

class TestMakeId(unittest.TestCase):
    def test_contains_name_and_pid(self):
        agent_id = rs.make_id("my-agent", 9999)
        self.assertIn("my-agent", agent_id)
        self.assertIn("9999", agent_id)

    def test_unique_per_call(self):
        id1 = rs.make_id("agent", 1)
        id2 = rs.make_id("agent", 1)
        self.assertNotEqual(id1, id2)

    def test_format_segments(self):
        # "{name}-{pid}-{timestamp}-{hex}" → at least 4 dash-separated segments
        agent_id = rs.make_id("agent", 42)
        parts = agent_id.split("-")
        self.assertGreaterEqual(len(parts), 4)

    def test_hex_suffix_is_hex(self):
        agent_id = rs.make_id("a", 1)
        suffix = agent_id.split("-")[-1]
        int(suffix, 16)  # raises ValueError if not hex


# ── row_to_agent ─────────────────────────────────────────────────────────────

class TestRowToAgent(_ServiceTest):
    def _row(self, **kwargs):
        _insert(self._conn, **kwargs)
        return _fetch_row(self._conn, kwargs.get("agent_id", "test-id"))

    def test_active_fresh_stays_active(self):
        row = self._row(status="active", last_heartbeat=time.time())
        agent = rs.row_to_agent(row)
        self.assertEqual(agent["status"], "active")

    def test_stale_detected_when_heartbeat_old(self):
        old_ts = time.time() - rs.STALE_SECS - 10
        row = self._row(status="active", last_heartbeat=old_ts)
        agent = rs.row_to_agent(row)
        self.assertEqual(agent["status"], "stale")

    def test_stopped_not_marked_stale(self):
        old_ts = time.time() - rs.STALE_SECS - 10
        row = self._row(status="stopped", last_heartbeat=old_ts)
        agent = rs.row_to_agent(row)
        self.assertEqual(agent["status"], "stopped")

    def test_meta_parsed_from_json(self):
        _insert(self._conn, agent_id="m1", meta={"key": "value"})
        row = _fetch_row(self._conn, "m1")
        agent = rs.row_to_agent(row)
        self.assertEqual(agent["meta"], {"key": "value"})

    def test_null_meta_returns_empty_dict(self):
        self._conn.execute(
            "INSERT INTO agents VALUES (?,?,?,?,?,?,?,?,?)",
            ("m2", "a", None, 0, None, time.time(), time.time(), "active", None),
        )
        self._conn.commit()
        row = _fetch_row(self._conn, "m2")
        agent = rs.row_to_agent(row)
        self.assertEqual(agent["meta"], {})

    def test_heartbeat_age_returned(self):
        old_ts = time.time() - 30
        row = self._row(last_heartbeat=old_ts)
        agent = rs.row_to_agent(row)
        self.assertGreaterEqual(agent["heartbeat_age"], 29)

    def test_all_fields_present(self):
        row = self._row()
        agent = rs.row_to_agent(row)
        for field in ("id", "name", "cwd", "pid", "host", "started_at",
                      "last_heartbeat", "heartbeat_age", "status", "meta"):
            self.assertIn(field, agent)


# ── prune ─────────────────────────────────────────────────────────────────────

class TestPrune(_ServiceTest):
    def test_old_stopped_removed(self):
        old = time.time() - rs.PRUNE_SECS - 10
        _insert(self._conn, agent_id="old-stopped", status="stopped", last_heartbeat=old)
        rs.prune(self._conn)
        self._conn.commit()
        row = _fetch_row(self._conn, "old-stopped")
        self.assertIsNone(row)

    def test_old_stale_removed(self):
        old = time.time() - rs.PRUNE_SECS - 10
        _insert(self._conn, agent_id="old-stale", status="stale", last_heartbeat=old)
        rs.prune(self._conn)
        self._conn.commit()
        self.assertIsNone(_fetch_row(self._conn, "old-stale"))

    def test_active_not_pruned(self):
        old = time.time() - rs.PRUNE_SECS - 10
        _insert(self._conn, agent_id="old-active", status="active", last_heartbeat=old)
        rs.prune(self._conn)
        self._conn.commit()
        self.assertIsNotNone(_fetch_row(self._conn, "old-active"))

    def test_recent_stopped_not_pruned(self):
        _insert(self._conn, agent_id="recent-stopped", status="stopped",
                last_heartbeat=time.time())
        rs.prune(self._conn)
        self._conn.commit()
        self.assertIsNotNone(_fetch_row(self._conn, "recent-stopped"))


# ── op_register ───────────────────────────────────────────────────────────────

class TestOpRegister(_ServiceTest):
    def test_returns_ok_and_id(self):
        result = rs.op_register({"name": "my-agent", "pid": 42})
        self.assertTrue(result["ok"])
        self.assertIn("id", result)

    def test_agent_inserted(self):
        result = rs.op_register({"name": "my-agent", "pid": 42})
        row = _fetch_row(self._conn, result["id"])
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "my-agent")
        self.assertEqual(row["status"], "active")

    def test_provided_id_honored(self):
        result = rs.op_register({"name": "a", "pid": 1, "id": "custom-id"})
        self.assertEqual(result["id"], "custom-id")
        self.assertIsNotNone(_fetch_row(self._conn, "custom-id"))

    def test_upsert_on_conflict(self):
        rs.op_register({"name": "agent", "pid": 1, "id": "same-id", "cwd": "/old"})
        rs.op_register({"name": "agent-v2", "pid": 2, "id": "same-id", "cwd": "/new"})
        row = _fetch_row(self._conn, "same-id")
        self.assertEqual(row["name"], "agent-v2")
        self.assertEqual(row["cwd"], "/new")

    def test_meta_stored(self):
        rs.op_register({"name": "a", "pid": 1, "id": "meta-id", "meta": {"k": "v"}})
        row = _fetch_row(self._conn, "meta-id")
        self.assertEqual(json.loads(row["meta"]), {"k": "v"})

    def test_default_name_when_missing(self):
        result = rs.op_register({"pid": 1})
        row = _fetch_row(self._conn, result["id"])
        self.assertEqual(row["name"], "agent")


# ── op_heartbeat ──────────────────────────────────────────────────────────────

class TestOpHeartbeat(_ServiceTest):
    def setUp(self):
        super().setUp()
        _insert(self._conn, agent_id="hb-id", status="active",
                last_heartbeat=time.time() - 60)

    def test_updates_heartbeat(self):
        before = _fetch_row(self._conn, "hb-id")["last_heartbeat"]
        rs.op_heartbeat({"id": "hb-id"})
        after = _fetch_row(self._conn, "hb-id")["last_heartbeat"]
        self.assertGreater(after, before)

    def test_returns_ok_and_active(self):
        result = rs.op_heartbeat({"id": "hb-id"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "active")

    def test_missing_id_returns_error(self):
        result = rs.op_heartbeat({})
        if isinstance(result, tuple):
            payload, code = result
        else:
            payload, code = result, 200
        self.assertFalse(payload["ok"])
        self.assertEqual(code, 400)

    def test_unknown_id_returns_404(self):
        result = rs.op_heartbeat({"id": "nonexistent"})
        payload, code = result
        self.assertFalse(payload["ok"])
        self.assertEqual(code, 404)

    def test_stopped_agent_not_updated(self):
        _insert(self._conn, agent_id="stopped-id", status="stopped",
                last_heartbeat=time.time() - 60)
        result = rs.op_heartbeat({"id": "stopped-id"})
        payload, code = result
        self.assertEqual(code, 404)


# ── op_deregister ─────────────────────────────────────────────────────────────

class TestOpDeregister(_ServiceTest):
    def setUp(self):
        super().setUp()
        _insert(self._conn, agent_id="dereg-id", status="active")

    def test_marks_stopped(self):
        rs.op_deregister({"id": "dereg-id"})
        row = _fetch_row(self._conn, "dereg-id")
        self.assertEqual(row["status"], "stopped")

    def test_returns_ok(self):
        result = rs.op_deregister({"id": "dereg-id"})
        self.assertTrue(result["ok"])

    def test_missing_id_returns_error(self):
        result = rs.op_deregister({})
        if isinstance(result, tuple):
            payload, code = result
        else:
            payload, code = result, 200
        self.assertFalse(payload["ok"])
        self.assertEqual(code, 400)

    def test_unknown_id_still_ok(self):
        # deregister is idempotent — unknown ID is a no-op (UPDATE affects 0 rows)
        result = rs.op_deregister({"id": "ghost"})
        self.assertTrue(result["ok"])


# ── op_agents ─────────────────────────────────────────────────────────────────

class TestOpAgents(_ServiceTest):
    def test_empty_returns_list(self):
        result = rs.op_agents()
        self.assertEqual(result["agents"], [])
        self.assertEqual(result["count"], 0)

    def test_active_agent_returned(self):
        _insert(self._conn, agent_id="a1", name="worker")
        result = rs.op_agents()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["agents"][0]["name"], "worker")

    def test_old_stopped_pruned_on_list(self):
        old = time.time() - rs.PRUNE_SECS - 10
        _insert(self._conn, agent_id="old-stop", status="stopped", last_heartbeat=old)
        result = rs.op_agents()
        ids = [a["id"] for a in result["agents"]]
        self.assertNotIn("old-stop", ids)

    def test_count_matches_list(self):
        _insert(self._conn, agent_id="x1")
        _insert(self._conn, agent_id="x2")
        result = rs.op_agents()
        self.assertEqual(result["count"], len(result["agents"]))


# ── op_health ─────────────────────────────────────────────────────────────────

class TestOpHealth(_ServiceTest):
    def test_returns_ok(self):
        result = rs.op_health()
        self.assertTrue(result["ok"])

    def test_count_reflects_agents(self):
        _insert(self._conn, agent_id="h1")
        _insert(self._conn, agent_id="h2")
        result = rs.op_health()
        self.assertEqual(result["count"], 2)

    def test_uptime_positive(self):
        result = rs.op_health()
        self.assertGreaterEqual(result["uptime"], 0)


if __name__ == "__main__":
    unittest.main()
