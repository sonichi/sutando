#!/usr/bin/env python3
"""Tests for the runtime-API identity surface (identity_view.py + dispatch).

Contract: sutando.info/status/owner/allowlist report ONLY what the workspace
records say — daemon actor id, core-status.json, own heartbeat, and channel
access.json files. Ownership is never inferred from an allowlist entry.

Run: python3 tests/runtime-api-identity-view.test.py
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

from identity_view import IdentityView  # noqa: E402
from dispatcher import RuntimeDispatcher  # noqa: E402
from protocol import ProtocolError  # noqa: E402


def _mk(state: Path, channels: Path | None = None, **kw) -> IdentityView:
    return IdentityView(state, "@me:example.org", channels_dir=channels,
                        host_label=kw.get("host_label"))


class IdentityViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.channels = Path(self.tmp.name) / "channels"

    def tearDown(self):
        self.tmp.cleanup()

    def _channel(self, name: str, payload: dict):
        d = self.channels / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "access.json").write_text(json.dumps(payload))

    def test_info_is_daemon_actor_plus_own_beat(self):
        cores = self.state / "cores"
        cores.mkdir()
        (cores / "my-host.alive").write_text(json.dumps(
            {"host": "my-host", "pid": 7, "socket": "/tmp/t.sock"}))
        v = _mk(self.state, host_label="my-host")
        info = v.info()
        self.assertEqual(info["agentId"], "@me:example.org")
        self.assertEqual(info["hostLabel"], "my-host")
        self.assertEqual(info["pid"], 7)
        self.assertEqual(info["socket"], "/tmp/t.sock")  # tmux sock passthrough

    def test_info_includes_injected_runtime_socket(self):
        v = IdentityView(self.state, "@me:example.org",
                         runtime_socket="/run/rt.sock")
        self.assertEqual(v.info()["runtimeSocket"], "/run/rt.sock")
        # not injected → key absent, never invented
        self.assertNotIn("runtimeSocket", _mk(self.state).info())

    def test_status_reads_core_status_and_liveness(self):
        (self.state / "core-status.json").write_text(json.dumps(
            {"status": "running", "step": "doing a thing", "ts": 123}))
        cores = self.state / "cores"
        cores.mkdir()
        (cores / "h.alive").write_text("{}")
        v = _mk(self.state, host_label="h")
        st = v.status()
        self.assertEqual(st["status"], "running")
        self.assertEqual(st["step"], "doing a thing")
        self.assertTrue(st["alive"])

    def test_status_missing_file_is_unknown_not_crash(self):
        self.assertEqual(_mk(self.state).status()["status"], "unknown")

    def test_stale_own_beat_reports_not_alive(self):
        cores = self.state / "cores"
        cores.mkdir()
        f = cores / "h.alive"
        f.write_text("{}")
        old = time.time() - 300
        os.utime(f, (old, old))
        st = _mk(self.state, host_label="h").status()
        self.assertFalse(st["alive"])

    def test_owner_uses_explicit_fields_never_allowfrom(self):
        # telegram: explicit tofuOwner. slack: tierMap owner. discord: ONLY an
        # allowFrom list — no ownership metadata → must NOT appear as owner.
        self._channel("telegram", {"allowFrom": ["111"], "tofuOwner": "111"})
        self._channel("slack", {"allowFrom": ["U1", "U2"],
                                "tierMap": {"U1": "owner", "U2": "team"}})
        self._channel("discord", {"allowFrom": ["999"]})
        owners = _mk(self.state, self.channels).owner()["owners"]
        self.assertEqual(owners["telegram"]["tofuOwner"], "111")
        self.assertEqual(owners["slack"]["tierOwners"], ["U1"])
        self.assertNotIn("discord", owners)

    def test_allowlist_is_verbatim_per_channel(self):
        self._channel("ag2space", {"allowFrom": ["@a:hs", "@b:hs"]})
        self._channel("discord", {"allowFrom": ["1", "2"]})
        ch = _mk(self.state, self.channels).allowlist()["channels"]
        self.assertEqual(ch["ag2space"], ["@a:hs", "@b:hs"])
        self.assertEqual(ch["discord"], ["1", "2"])

    def test_unreadable_channel_is_skipped_not_fatal(self):
        self._channel("good", {"allowFrom": ["x"]})
        bad = self.channels / "bad"
        bad.mkdir(parents=True)
        (bad / "access.json").write_text("{nope")
        ch = _mk(self.state, self.channels).allowlist()["channels"]
        self.assertEqual(list(ch), ["good"])

    def test_no_channels_dir_yields_empty_surfaces(self):
        v = _mk(self.state, None)
        self.assertEqual(v.owner()["owners"], {})
        self.assertEqual(v.allowlist()["channels"], {})


class DispatchTests(unittest.TestCase):
    class _No:
        def __getattr__(self, name):
            raise AssertionError(f"sutando.* reached {name}")

    def test_all_four_methods_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / "core-status.json").write_text('{"status":"idle","ts":1}')
            d = RuntimeDispatcher(self._No(), self._No(), "@me:x",
                                  executors={},
                                  identity_view=IdentityView(state, "@me:x"))
            for method in ("sutando.info", "sutando.status",
                           "sutando.owner", "sutando.allowlist"):
                out = asyncio.run(d.handle(method, {}))
                self.assertIsInstance(out, dict, method)
            self.assertEqual(
                asyncio.run(d.handle("sutando.info", {}))["agentId"], "@me:x")

    def test_unconfigured_identity_fails_loudly(self):
        d = RuntimeDispatcher(self._No(), self._No(), "@me:x",
                              executors={}, identity_view=None)
        with self.assertRaises(ProtocolError):
            asyncio.run(d.handle("sutando.info", {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
