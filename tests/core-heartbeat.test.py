#!/usr/bin/env python3
"""Tests for src/core_heartbeat.py — per-host liveness signal.

Run: python3 tests/core-heartbeat.test.py
Exit: 0 on pass, 1 on fail.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _short_host() -> str:
    return socket.gethostname().split(".")[0]


class TestHeartbeatWrite(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("SUTANDO_WORKSPACE")
        self.tmp = Path(tempfile.mkdtemp(prefix="core-heartbeat-"))
        os.environ["SUTANDO_WORKSPACE"] = str(self.tmp)
        # Force re-import so module picks up the new env.
        sys.modules.pop("core_heartbeat", None)

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["SUTANDO_WORKSPACE"] = self._saved_env
        elif "SUTANDO_WORKSPACE" in os.environ:
            del os.environ["SUTANDO_WORKSPACE"]
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        sys.modules.pop("core_heartbeat", None)

    def test_write_beat_creates_per_host_file(self):
        import core_heartbeat
        core_heartbeat.write_beat()
        alive_path = self.tmp / "state" / "cores" / f"{_short_host()}.alive"
        self.assertTrue(alive_path.is_file(), f"expected {alive_path} to exist")

    def test_write_beat_payload_schema(self):
        import core_heartbeat
        core_heartbeat.write_beat(status="custom-status")
        data = json.loads((self.tmp / "state" / "cores" / f"{_short_host()}.alive").read_text())
        # Required fields
        self.assertEqual(data["host"], _short_host())
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["status"], "custom-status")
        self.assertEqual(data["schema_version"], 1)
        self.assertIsInstance(data["started_at"], float)
        self.assertIsInstance(data["last_beat_at"], float)
        # last_beat_at advances after a sleep; just sanity-check it's recent.
        self.assertLess(abs(time.time() - data["last_beat_at"]), 5)

    def test_write_beat_is_atomic_via_tmp(self):
        """The .alive write goes through .alive.tmp then renames into place —
        a concurrent reader at the destination path never sees a half-file."""
        import core_heartbeat
        core_heartbeat.write_beat()
        alive = self.tmp / "state" / "cores" / f"{_short_host()}.alive"
        tmp = self.tmp / "state" / "cores" / f"{_short_host()}.alive.tmp"
        self.assertTrue(alive.exists())
        self.assertFalse(tmp.exists(), "tmp file should have been renamed away")

    def test_write_beat_overwrites_on_second_call(self):
        import core_heartbeat
        core_heartbeat.write_beat(status="first")
        path = self.tmp / "state" / "cores" / f"{_short_host()}.alive"
        first_data = json.loads(path.read_text())
        time.sleep(0.01)
        core_heartbeat.write_beat(status="second")
        second_data = json.loads(path.read_text())
        self.assertEqual(second_data["status"], "second")
        # started_at should NOT change — it's set at module import.
        self.assertEqual(first_data["started_at"], second_data["started_at"])
        # last_beat_at should advance.
        self.assertGreater(second_data["last_beat_at"], first_data["last_beat_at"])

    def test_write_beat_creates_cores_dir(self):
        """The cores/ dir must be created if it doesn't yet exist — fresh
        install case."""
        import core_heartbeat
        cores_dir = self.tmp / "state" / "cores"
        self.assertFalse(cores_dir.exists())
        core_heartbeat.write_beat()
        self.assertTrue(cores_dir.is_dir())


class TestHeartbeatCli(unittest.TestCase):
    """End-to-end tests that exercise the script via subprocess so the CLI
    parsing, signal handling, and cleanup paths are covered."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="core-heartbeat-cli-"))
        self.env = {**os.environ, "SUTANDO_WORKSPACE": str(self.tmp)}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_once_flag_writes_single_beat_and_exits(self):
        script = ROOT / "src" / "core_heartbeat.py"
        result = subprocess.run(
            [sys.executable, str(script), "--once", "--status", "smoke"],
            env=self.env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        alive = self.tmp / "state" / "cores" / f"{_short_host()}.alive"
        self.assertTrue(alive.is_file())
        data = json.loads(alive.read_text())
        self.assertEqual(data["status"], "smoke")

    def test_sigterm_cleans_up_alive_file(self):
        """Graceful shutdown removes the .alive file so peers see the core
        leave immediately rather than wait for mtime staleness."""
        import signal as _signal
        script = ROOT / "src" / "core_heartbeat.py"
        proc = subprocess.Popen(
            [sys.executable, str(script), "--interval", "0.5"],
            env=self.env,
        )
        # Wait for first beat to land.
        alive = self.tmp / "state" / "cores" / f"{_short_host()}.alive"
        for _ in range(40):
            if alive.exists():
                break
            time.sleep(0.1)
        self.assertTrue(alive.exists(), "first beat should have landed within 4s")
        # Signal graceful shutdown.
        proc.send_signal(_signal.SIGTERM)
        proc.wait(timeout=5)
        self.assertFalse(alive.exists(), ".alive should have been unlinked on SIGTERM")


if __name__ == "__main__":
    unittest.main(verbosity=2)
