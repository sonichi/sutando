#!/usr/bin/env python3
"""Tests for the dumb reference-client TUI (architecture probe).

Contract (owner spec, taxonomy part 9): the client composes an instance view
from ONLY registry + manifest + a protocol probe over the instance's own
endpoint. The five states stay separate; a stopped instance shows
Registered/Stopped without any socket; a stale-status manifest is never
trusted as running; identity is verified over the socket.

This drives instance_view()/render_view() against a REAL daemon booted on a
tmp socket + registry (the same shape the E2E harness uses) so the probe path
is exercised end to end, not mocked.

Run: python3 tests/runtime-tui-reference-client.test.py
Exit: 0 on pass, 1 on fail.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "runtime-cli"))
sys.path.insert(0, str(REPO / "src" / "runtime-api"))

import tui  # noqa: E402


def _wait_socket(path: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists() and tui._socket_reachable(path):
            return True
        time.sleep(0.1)
    return False


class TuiReferenceClientTests(unittest.TestCase):
    def test_stopped_instance_view_needs_no_socket(self):
        m = {"identity": {"agent_id": "research-001"},
             "endpoint": {"type": "unix", "path": "/nonexistent/x.sock"},
             "status": "stopped"}
        v = tui.instance_view(m)
        self.assertEqual(v["existence"], "registered")
        self.assertEqual(v["server"], "stopped")
        self.assertEqual(v["core"], "unknown")
        self.assertIsNone(v["identityVerified"])
        self.assertEqual(v["desiredState"], "stopped")
        # render is string-only and shows the separate states
        r = tui.render_view(v)
        self.assertIn("Server:     stopped", r)
        self.assertIn("research-001", r)

    def test_stale_status_manifest_not_trusted_as_running(self):
        # manifest claims running but the socket is dead → view says stopped
        m = {"identity": {"agent_id": "ghost-001"},
             "endpoint": {"type": "unix", "path": "/nonexistent/y.sock"},
             "status": "running"}
        self.assertEqual(tui.instance_view(m)["server"], "stopped")

    def test_live_instance_view_verifies_identity_over_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            state = Path(tmp) / "state"
            state.mkdir(parents=True)
            sock = run / "rt.sock"
            env = {**os.environ,
                   "SUTANDO_RUNTIME_SOCKET": str(sock),
                   "SUTANDO_RUNTIME_DB": str(Path(tmp) / "rt.sqlite"),
                   "SUTANDO_HA_DIR": str(Path(tmp) / "ha"),
                   "SUTANDO_RUNTIME_STATE": str(state),
                   "SUTANDO_HOST_LABEL": "tui-host",
                   "SUTANDO_INSTANCE_REGISTRY": str(Path(tmp) / "instances"),
                   "SUTANDO_AGENT_ID": "@tui-agent:example.org"}
            (state / "cores").mkdir()
            (state / "cores" / "tui-host.alive").write_text(
                json.dumps({"host": "tui-host", "pid": 1}))
            (state / "core-status.json").write_text(
                json.dumps({"status": "running", "step": "tui-e2e"}))
            daemon = subprocess.Popen(
                [sys.executable, str(REPO / "src" / "runtime-api" / "server.py")],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                self.assertTrue(_wait_socket(str(sock)), "daemon socket")
                # discover through the registry the daemon wrote at boot
                os.environ["SUTANDO_INSTANCE_REGISTRY"] = str(Path(tmp) / "instances")
                import instance_registry
                mans = [m for m in instance_registry.list_instances()
                        if m.get("identity", {}).get("agent_id") == "@tui-agent:example.org"]
                self.assertEqual(len(mans), 1)
                v = tui.instance_view(mans[0])
                self.assertEqual(v["server"], "running")
                self.assertTrue(v["identityVerified"])
                self.assertEqual(v["core"], "running")
                self.assertEqual(v["health"], "healthy")
                self.assertEqual(v.get("activity"), "tui-e2e")
                self.assertIn("Identity:   verified", tui.render_view(v))
            finally:
                daemon.terminate()
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                os.environ.pop("SUTANDO_INSTANCE_REGISTRY", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
