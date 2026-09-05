#!/usr/bin/env python3
"""Tests for runtime-API agent discovery (agents_view.py + dispatcher wiring).

Contract under test: `agent.list` / `agent.status` expose identity + liveness
from `state/cores/*.alive` heartbeats — mtime is the only trust signal, the
payload is passthrough metadata, and no process/tmux/socket detail leaks.

Run: python3 tests/runtime-api-agents-view.test.py
Exit: 0 on pass, 1 on fail.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

from agents_view import AgentsView, ALIVE_MAX_AGE_S  # noqa: E402
from dispatcher import RuntimeDispatcher  # noqa: E402
from protocol import ProtocolError  # noqa: E402


def _write_beat(cores: Path, name: str, age_s: float = 0.0, **payload) -> Path:
    cores.mkdir(parents=True, exist_ok=True)
    f = cores / f"{name}.alive"
    f.write_text(json.dumps({"host": name, "pid": 123, "status": "running",
                             **payload}))
    if age_s:
        t = time.time() - age_s
        os.utime(f, (t, t))
    return f


class AgentsViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.cores = self.state / "cores"
        self.view = AgentsView(self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_beat_is_alive(self):
        _write_beat(self.cores, "mac-pro")
        agents = self.view.list_agents()["agents"]
        self.assertEqual(len(agents), 1)
        self.assertTrue(agents[0]["alive"])
        self.assertEqual(agents[0]["agentId"], "mac-pro")

    def test_stale_beat_is_not_alive(self):
        # Just past the documented ~90s cutoff → present but not alive.
        _write_beat(self.cores, "mac-mini", age_s=ALIVE_MAX_AGE_S + 5)
        agents = self.view.list_agents()["agents"]
        self.assertEqual(len(agents), 1)
        self.assertFalse(agents[0]["alive"])

    def test_unlinked_heartbeat_means_absent(self):
        # Graceful shutdown unlinks the file → the agent is not listed at all.
        f = _write_beat(self.cores, "gone")
        f.unlink()
        self.assertEqual(self.view.list_agents()["agents"], [])

    def test_no_cores_dir_is_empty_not_error(self):
        self.assertEqual(self.view.list_agents()["agents"], [])

    def test_payload_metadata_passthrough_without_backend_detail(self):
        _write_beat(self.cores, "mac-pro",
                    socket="/tmp/sutando-tmux.sock",
                    locality={"kind": "local", "host": "mac-pro"})
        entry = self.view.agent_status("mac-pro")
        self.assertEqual(entry["socket"], "/tmp/sutando-tmux.sock")
        self.assertEqual(entry["locality"]["kind"], "local")
        # No invented fields: nothing about tmux sessions, PTYs, or processes
        # beyond the heartbeat's own self-report.
        self.assertNotIn("tmuxSession", entry)

    def test_status_matches_by_basename_or_payload_host(self):
        _write_beat(self.cores, "label-differs", host="real-hostname")
        self.assertIsNotNone(self.view.agent_status("label-differs"))
        self.assertIsNotNone(self.view.agent_status("real-hostname"))
        self.assertIsNone(self.view.agent_status("nobody"))

    def test_corrupt_payload_still_reports_liveness(self):
        self.cores.mkdir(parents=True, exist_ok=True)
        (self.cores / "broken.alive").write_text("{not json")
        entry = self.view.agent_status("broken")
        self.assertTrue(entry["alive"])  # mtime is the trust signal, not JSON


class DispatcherWiringTests(unittest.TestCase):
    """agent.* dispatch through RuntimeDispatcher.handle() — the injected-view
    contract, no store/ha/socket involvement."""

    class _NoStore:  # agent.* must never touch the request store
        def __getattr__(self, name):
            raise AssertionError(f"agent.* reached the store ({name})")

    class _NoHA:
        def __getattr__(self, name):
            raise AssertionError(f"agent.* reached human-actions ({name})")

    def _dispatcher(self, view):
        return RuntimeDispatcher(self._NoStore(), self._NoHA(), "test-actor",
                                 executors={}, agents_view=view)

    def test_agent_list_and_status_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            _write_beat(state / "cores", "mac-pro")
            d = self._dispatcher(AgentsView(state))
            out = asyncio.run(d.handle("agent.list", {}))
            self.assertEqual(out["agents"][0]["agentId"], "mac-pro")
            st = asyncio.run(d.handle("agent.status", {"agentId": "mac-pro"}))
            self.assertTrue(st["alive"])

    def test_unknown_agent_is_protocol_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._dispatcher(AgentsView(Path(tmp)))
            with self.assertRaises(ProtocolError):
                asyncio.run(d.handle("agent.status", {"agentId": "nope"}))

    def test_missing_agent_id_is_protocol_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._dispatcher(AgentsView(Path(tmp)))
            with self.assertRaises(ProtocolError):
                asyncio.run(d.handle("agent.status", {}))

    def test_unconfigured_view_fails_loudly_not_silently(self):
        d = RuntimeDispatcher(self._NoStore(), self._NoHA(), "test-actor",
                              executors={}, agents_view=None)
        with self.assertRaises(ProtocolError):
            asyncio.run(d.handle("agent.list", {}))


class ASupersededGhostIsAnnotatedNotHidden(unittest.TestCase):
    """A host-label change leaves stale .alive files describing a process that is
    still running under a new label; agent.list must collapse them without lying."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cores = Path(self.tmp) / "cores"
        self.cores.mkdir()

    def _beat(self, label, pid, started, age_s):
        f = self.cores / f"{label}.alive"
        payload = {"pid": pid}
        if started is not None:
            payload["started_at"] = started
        f.write_text(json.dumps(payload))
        t = time.time() - age_s
        os.utime(f, (t, t))
        return f

    def _by_id(self):
        return {a["agentId"]: a for a in AgentsView(self.tmp).list_agents()["agents"]}

    def test_dead_label_sharing_pid_and_start_with_a_live_one_is_superseded(self):
        self._beat("new-label", 8654, 1783220522.875356, 1)
        self._beat("old-label", 8654, 1783220522.875356, 300000)
        got = self._by_id()
        self.assertEqual(got["old-label"].get("supersededBy"), "new-label")
        self.assertFalse(got["old-label"]["alive"], "annotation must not revive a dead entry")
        self.assertNotIn("supersededBy", got["new-label"], "the live holder is not superseded")

    def test_same_pid_but_different_start_is_a_genuinely_distinct_host(self):
        # pid recycles across reboots; started_at is what discriminates.
        self._beat("live-host", 7449, 1783220522.875356, 1)
        self._beat("old-real-host", 7449, 1781143903.3270772, 300000)
        self.assertNotIn("supersededBy", self._by_id()["old-real-host"])

    def test_absent_started_at_is_never_treated_as_evidence(self):
        self._beat("live-nostart", 555, None, 1)
        self._beat("dead-nostart", 555, None, 300000)
        self.assertNotIn("supersededBy", self._by_id()["dead-nostart"])

    def test_a_dead_entry_with_no_live_twin_is_left_alone(self):
        self._beat("only-dead", 4242, 1783220522.5, 300000)
        self.assertNotIn("supersededBy", self._by_id()["only-dead"])

    def test_agent_status_reports_the_same_annotation_as_agent_list(self):
        self._beat("new-label", 8654, 1783220522.875356, 1)
        self._beat("old-label", 8654, 1783220522.875356, 300000)
        st = AgentsView(self.tmp).agent_status("old-label")
        self.assertEqual(st.get("supersededBy"), "new-label")


if __name__ == "__main__":
    unittest.main(verbosity=2)
