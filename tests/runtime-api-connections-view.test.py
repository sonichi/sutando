#!/usr/bin/env python3
"""Tests for the runtime-API connections surface (connections_view.py + dispatcher wiring).

Contract under test: `connection.list` / `connection.add` (preview, then confirm) /
`connection.remove` / `connection.set_primary` / `connection.reconnect` dispatch to
src/connections.py, return rows with a vault key name and never a secret, answer a
moved identity with CONFLICT, and refuse malformed params with ProtocolError.
The vault, the gateway probe and the spawn are fakes.

Run: python3 tests/runtime-api-connections-view.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

from connections_view import CONFLICT, ConnectionsView  # noqa: E402
from dispatcher import RuntimeDispatcher  # noqa: E402

from protocol import ProtocolError  # noqa: E402

TOKEN_A = "https://chat.alpha.example|secret-alpha"
TOKEN_B = "https://relay.beta.example|secret-beta"

import connections  # noqa: E402

connections.DEFAULT_RESOLVER = lambda host: None  # offline: no well-known fetch from a test


class FakeVault:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store[key]

    def set(self, key, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


class FakeGateway:
    """GET /v1/agents by gateway URL: which agent each token's gateway names."""

    def __init__(self):
        self.agents = {"https://chat.alpha.example": ["@sutando:alpha.example"],
                       "https://relay.beta.example": ["@sutando:beta.example"]}

    def __call__(self, url, token):
        return {"agents": [{"id": i} for i in self.agents.get(url, [])]}


class _Store:
    def pending(self):
        return []


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.vault = FakeVault()
        self.gateway = FakeGateway()
        self.spawned = []
        self.view = ConnectionsView(self.ws, self.ws / "repo", self.ws / "logs", vault=self.vault,
                                    fetch=self.gateway, spawn=self._spawn, kill=lambda *a: None)
        self.disp = RuntimeDispatcher(_Store(), None, "actor", connections_view=self.view)

    def tearDown(self):
        self.tmp.cleanup()

    def _spawn(self, argv, env, log_path):
        self.spawned.append(env["GATEWAY_INSTANCE"])
        return 7

    def call(self, method, **params):
        return asyncio.run(self.disp.handle(method, params))

    def test_list_is_empty_without_a_record(self):
        self.assertEqual(self.call("connection.list"), {"connections": []})

    def test_add_is_preview_then_confirm(self):
        preview = self.call("connection.add", token=TOKEN_A)["preview"]
        self.assertEqual((preview["server"], preview["agent_mxid"], preview["verified"], preview["authenticated"]),
                         ("alpha.example", "@sutando:alpha.example", True, True))
        self.assertEqual(preview["gateway_url"], "https://chat.alpha.example")
        self.assertEqual(self.call("connection.list"), {"connections": []}, "a preview saves nothing")
        self.assertEqual(self.vault.store, {})

        out = self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha.example",
                        added_by="ui")
        self.assertTrue(out["created"])
        row = out["connection"]
        self.assertEqual((row["server"], row["instance"], row["token_ref"], row["primary"], row["state"],
                          row["server_source"]),
                         ("alpha.example", "alpha-example", "AG2_REMOTE_TOKEN_ALPHA_EXAMPLE", True, "pending",
                          "identity"))
        self.assertNotIn("secret-alpha", json.dumps(out))
        self.assertEqual(self.vault.store[row["token_ref"]], TOKEN_A)

        again = self.call("connection.add", token="https://chat.alpha.example|rotated", confirm=True,
                          expected_server="alpha.example")
        self.assertFalse(again["created"])
        self.assertEqual(again["connection"]["token_ref"], row["token_ref"])
        self.assertEqual(self.vault.store[row["token_ref"]], TOKEN_A, "an existing server is left as is")
        self.assertEqual(len(self.call("connection.list")["connections"]), 1)

    def test_confirm_against_a_moved_identity_is_a_conflict(self):
        preview = self.call("connection.add", token=TOKEN_A)["preview"]
        self.gateway.agents["https://chat.alpha.example"] = ["@sutando:other.example"]
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.add", token=TOKEN_A, confirm=True, expected_server=preview["server"])
        self.assertEqual(cm.exception.code, CONFLICT)
        self.assertTrue(str(cm.exception).startswith("mismatch"))
        self.assertIn("other.example", str(cm.exception))
        self.assertNotIn("secret-alpha", str(cm.exception))
        self.assertEqual(self.call("connection.list"), {"connections": []})
        self.assertEqual(self.vault.store, {})

    def test_status_state_and_set_primary(self):
        self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha.example")
        self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta.example")
        (self.ws / "state" / "gateway-status.alpha-example.json").write_text(
            json.dumps({"connected": True, "last_ok_ts": 42}))
        listed = self.call("connection.list")["connections"]
        self.assertEqual([(r["instance"], r["primary"], r["state"]) for r in listed],
                         [("alpha-example", True, "connected"), ("beta-example", False, "pending")])
        self.assertEqual(listed[0]["status"], {"connected": True, "last_ok_ts": 42})
        self.assertNotIn("secret-", json.dumps(listed))

        out = self.call("connection.set_primary", instance="beta-example")
        self.assertTrue(out["connection"]["primary"])
        listed = self.call("connection.list")["connections"]
        self.assertEqual([r["primary"] for r in listed], [False, True])

    def test_remove_leaves_the_other_connection_alone(self):
        self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha.example")
        self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta.example")
        self.call("connection.reconnect", instance="alpha-example")
        self.call("connection.reconnect", instance="beta-example")
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.remove", instance="alpha-example")
        self.assertIn("without force", str(cm.exception))
        gone = self.call("connection.remove", instance="beta-example")
        self.assertEqual(gone["removed"]["instance"], "beta-example")
        self.assertEqual(set(self.vault.store), {"AG2_REMOTE_TOKEN_ALPHA_EXAMPLE"})
        listed = self.call("connection.list")["connections"]
        self.assertEqual([(r["instance"], r["primary"], r["launched_at"] is not None) for r in listed],
                         [("alpha-example", True, True)])
        self.assertFalse((self.ws / "state" / "connections-launch" / "beta-example.json").exists())

    def test_reconnect_launches_exactly_that_bridge(self):
        self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha.example")
        self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta.example")
        out = self.call("connection.reconnect", instance="beta-example")
        self.assertEqual((out["launch"]["ok"], out["launch"]["pid"], out["launch"]["stopped_pid"]), (True, 7, 0))
        self.assertEqual(self.spawned, ["beta-example"])
        with self.assertRaises(ProtocolError):
            self.call("connection.reconnect", instance="nope")

    def test_param_validation(self):
        for params in ({}, {"token": ""}, {"token": 3}, {"token": TOKEN_A, "confirm": "yes"},
                       {"token": TOKEN_A, "confirm": True}, {"token": TOKEN_A, "confirm": True, "expected_server": ""}):
            with self.assertRaises(ProtocolError, msg=params):
                self.call("connection.add", **params)
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.add", token="opaque-bearer")
        self.assertIn("no gateway URL", str(cm.exception))
        for method in ("connection.remove", "connection.set_primary", "connection.reconnect"):
            with self.assertRaises(ProtocolError):
                self.call(method)
            with self.assertRaises(ProtocolError) as cm:
                self.call(method, instance="nope")
            self.assertIn("nope", str(cm.exception))
        self.assertEqual(self.vault.store, {})

    def test_unconfigured_surface_is_a_method_not_found(self):
        bare = RuntimeDispatcher(_Store(), None, "actor")
        with self.assertRaises(ProtocolError) as cm:
            asyncio.run(bare.handle("connection.list", {}))
        self.assertEqual(cm.exception.code, -32601)

    def test_broken_record_is_an_internal_error_not_a_crash(self):
        (self.ws / "state").mkdir()
        (self.ws / "state" / "connections.json").write_text("{nope")
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.list")
        self.assertEqual(cm.exception.code, -32603)


if __name__ == "__main__":
    unittest.main(verbosity=1)
