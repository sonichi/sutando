#!/usr/bin/env python3
"""Tests for the runtime surface (runtime_view.py + dispatch).

Contract (owner taxonomy 2026-08-08): runtime.health is the COARSE end-user
readout — online/offline/degraded + current activity; runtime.details is the
diagnostic surface where pid/sockets/heartbeat internals live. Identity
(sutando.info) no longer carries those internals.

Run: python3 tests/runtime-api-runtime-view.test.py
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

from runtime_view import RuntimeView, DEGRADED_BEAT_AGE_S  # noqa: E402
from identity_view import IdentityView  # noqa: E402

from dispatcher import RuntimeDispatcher  # noqa: E402
from protocol import ProtocolError  # noqa: E402


class RuntimeViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        (self.state / "cores").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _beat(self, payload: dict, age_s: float = 0):
        f = self.state / "cores" / "h.alive"
        f.write_text(json.dumps(payload))
        if age_s:
            old = time.time() - age_s
            os.utime(f, (old, old))
        return f

    def test_health_online_with_current_activity(self):
        self._beat({"pid": 7})
        (self.state / "core-status.json").write_text(
            json.dumps({"status": "running", "step": "building a thing"}))
        h = RuntimeView(self.state, host_label="h").health()
        self.assertEqual(h["state"], "online")
        self.assertEqual(h["currentActivity"], "building a thing")

    def test_health_degraded_on_lagging_beat(self):
        self._beat({}, age_s=DEGRADED_BEAT_AGE_S + 5)
        self.assertEqual(RuntimeView(self.state, host_label="h").health()["state"],
                         "degraded")

    def test_health_offline_on_stale_or_missing_beat(self):
        self.assertEqual(RuntimeView(self.state, host_label="h").health()["state"],
                         "offline")
        self._beat({}, age_s=300)
        self.assertEqual(RuntimeView(self.state, host_label="h").health()["state"],
                         "offline")

    def test_details_carries_diagnostics(self):
        self._beat({"pid": 7, "socket": "/tmp/t.sock", "started_at": 1})
        d = RuntimeView(self.state, host_label="h",
                        runtime_socket="/run/rt.sock").details()
        self.assertEqual(d["pid"], 7)
        self.assertEqual(d["socket"], "/tmp/t.sock")
        self.assertEqual(d["runtimeSocket"], "/run/rt.sock")

    def test_identity_info_no_longer_leaks_runtime_internals(self):
        self._beat({"pid": 7, "socket": "/tmp/t.sock",
                    "locality": {"kind": "local", "host": "h"}})
        info = IdentityView(self.state, "@me:x", host_label="h").info()
        self.assertNotIn("pid", info)
        self.assertNotIn("socket", info)
        self.assertNotIn("runtimeSocket", info)
        self.assertEqual(info["locality"], {"kind": "local", "host": "h"})


class DispatchTests(unittest.TestCase):
    class _No:
        def __getattr__(self, name):
            raise AssertionError(f"runtime.* reached {name}")

    def test_dispatch_and_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "cores").mkdir()
            d = RuntimeDispatcher(self._No(), self._No(), "@me:x", executors={},
                                  runtime_view=RuntimeView(state, host_label="h"))
            h = asyncio.run(d.handle("runtime.health", {}))
            self.assertEqual(h["state"], "offline")
        d2 = RuntimeDispatcher(self._No(), self._No(), "@me:x",
                               executors={}, runtime_view=None)
        with self.assertRaises(ProtocolError):
            asyncio.run(d2.handle("runtime.health", {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
