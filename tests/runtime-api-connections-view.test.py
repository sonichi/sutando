#!/usr/bin/env python3
"""Tests for the runtime-API connections surface (connections_view.py + dispatcher wiring).

Contract under test: `connection.list` / `connection.known_servers` / `connection.add`
(preview, then confirm) / `connection.remove` / `connection.set_primary` /
`connection.disconnect` / `connection.reconnect` dispatch to src/connections.py, return
rows with a vault key name and never a secret, answer a moved identity and a disconnect
of the primary with CONFLICT, and refuse malformed params with ProtocolError.
The vault, the gateway probe and the spawn are fakes.

Run: python3 tests/runtime-api-connections-view.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
# The spawn fake takes no bridge lease, as a bridge run with the lock off does.
isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1", SUTANDO_BRIDGE_LOCK="0")

import asyncio
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "runtime-api"))

from connections_view import CONFLICT, ConnectionsView  # noqa: E402
from dispatcher import RuntimeDispatcher  # noqa: E402

from protocol import ProtocolError  # noqa: E402

TOKEN_A = "https://chat.alpha-fixture.net|secret-alpha"
TOKEN_B = "https://relay.beta-fixture.net|secret-beta"

import connections  # noqa: E402
import dotspace  # noqa: E402

# Offline: the real resolver with no well-known fetch and a registry naming both test servers.
REGISTRY = {"instances": [{"server": "alpha-fixture.net", "gateway_host": "chat.alpha-fixture.net"},
                          {"server": "beta-fixture.net", "gateway_host": "relay.beta-fixture.net"}]}
connections.DEFAULT_RESOLVER = lambda host: dotspace.dotspace_for(host, fetch=dotspace.NO_FETCH, config=REGISTRY)


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
        self.agents = {"https://chat.alpha-fixture.net": ["@sutando:alpha-fixture.net"],
                       "https://relay.beta-fixture.net": ["@sutando:beta-fixture.net"]}

    def __call__(self, url, token, address=None):
        if self.agents.get(url) == "reject":
            import urllib.error
            raise urllib.error.HTTPError(url, 401, "nope", {}, None)
        return {"agents": [{"id": i} for i in self.agents.get(url, [])]}


def public_getaddrinfo(host, port, **kw):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


class _Store:
    def pending(self):
        return []


# Above any pid_max: the spawn fake's pid never runs, so launch sees it exit at once.
DEAD_PID = 4_194_404


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.vault = FakeVault()
        self.gateway = FakeGateway()
        self.spawned = []
        self.view = ConnectionsView(self.ws, self.ws / "repo", self.ws / "logs", vault=self.vault,
                                    fetch=self.gateway, spawn=self._spawn, kill=lambda *a: None,
                                    getaddrinfo=public_getaddrinfo)
        self.disp = RuntimeDispatcher(_Store(), None, "actor", connections_view=self.view)

    def tearDown(self):
        self.tmp.cleanup()

    def _spawn(self, argv, env, log_path):
        self.spawned.append(env["GATEWAY_INSTANCE"])
        return DEAD_PID

    def call(self, method, **params):
        return asyncio.run(self.disp.handle(method, params))

    def test_list_is_empty_without_a_record(self):
        self.assertEqual(self.call("connection.list"), {"connections": [], "capabilities": ["legacy_readopt", "promote_keeps_lanes"]})

    def test_list_on_an_empty_record_imports_the_channel_file_token_through_the_view_vault(self):
        home = self.ws / "claude-home"
        (home / "channels" / "ag2space").mkdir(parents=True)
        (home / "channels" / "ag2space" / "relay-client.env").write_text(f"REMOTE_TASK_TOKEN={TOKEN_A}\n")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}):
            rows = self.call("connection.list")["connections"]
        self.assertEqual([(r["instance"], r["token_ref"], r["primary"], r["added_by"]) for r in rows],
                         [("", "REMOTE_TASK_TOKEN", True, "env")])
        self.assertEqual(self.vault.store, {"REMOTE_TASK_TOKEN": TOKEN_A}, "the view's vault, not the host's")
        self.assertTrue(connections.record_path(self.ws).is_file())

    def test_add_is_preview_then_confirm(self):
        preview = self.call("connection.add", token=TOKEN_A)["preview"]
        self.assertEqual((preview["server"], preview["agent_mxid"], preview["verified"], preview["authenticated"]),
                         ("alpha-fixture.net", "@sutando:alpha-fixture.net", True, True))
        self.assertEqual(preview["gateway_url"], "https://chat.alpha-fixture.net")
        self.assertEqual(self.call("connection.list"), {"connections": [], "capabilities": ["legacy_readopt", "promote_keeps_lanes"]}, "a preview saves nothing")
        self.assertEqual(self.vault.store, {})

        out = self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha-fixture.net",
                        added_by="ui")
        self.assertTrue(out["created"])
        row = out["connection"]
        self.assertEqual((row["server"], row["instance"], row["token_ref"], row["primary"], row["state"],
                          row["server_source"]),
                         ("alpha-fixture.net", "alpha-fixture-net", "AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET", True, "pending",
                          "identity"))
        self.assertNotIn("secret-alpha", json.dumps(out))
        self.assertEqual(self.vault.store[row["token_ref"]], TOKEN_A)

        again = self.call("connection.add", token="https://chat.alpha-fixture.net|rotated", confirm=True,
                          expected_server="alpha-fixture.net")
        self.assertFalse(again["created"])
        self.assertEqual(again["connection"]["token_ref"], row["token_ref"])
        self.assertEqual(self.vault.store[row["token_ref"]], TOKEN_A, "an existing server is left as is")
        self.assertEqual(len(self.call("connection.list")["connections"]), 1)

    def test_add_starts_the_new_bridge_and_leaves_the_primary_routing(self):
        first = self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha-fixture.net")
        self.assertEqual((first["launch"]["ok"], self.spawned), (True, ["alpha-fixture-net"]))
        envs = []
        self.view.spawn = lambda argv, env, log: envs.append(env) or DEAD_PID
        with mock.patch.dict(os.environ, {"REMOTE_PROACTIVE_ROOM": "!room"}):
            second = self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta-fixture.net")
        self.assertEqual(second["launch"]["instance"], "beta-fixture-net")
        self.assertEqual([e["GATEWAY_INSTANCE"] for e in envs], ["beta-fixture-net"], "the second lane starts beside the first")
        self.assertEqual(envs[0]["REMOTE_PROACTIVE_ROOM"], "", "only the primary carries the proactive room")
        self.assertEqual(connections.primary_instance(self.ws), "alpha-fixture-net")
        self.assertEqual(second["connection"]["running"], False)

    def test_add_reports_a_failed_start_with_its_reason(self):
        def broken(argv, env, log):
            raise OSError("no python")
        self.view.spawn = broken
        out = self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha-fixture.net")
        self.assertEqual((out["created"], out["launch"]["ok"], out["launch"]["reason"]), (True, False, "launch_failed"))
        self.assertEqual((out["connection"]["state"], out["connection"]["reason"]), ("failed", "launch_failed"))

    def test_add_does_not_start_a_bridge_that_already_holds_the_lease(self):
        from _helpers import bridge_lease
        holder = bridge_lease.start(self.ws, "alpha-fixture-net")
        try:
            out = self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha-fixture.net")
            self.assertEqual((out["launch"]["lease"], self.spawned), ("already_running", []))
            self.assertTrue(out["connection"]["running"])
        finally:
            bridge_lease.stop(holder)

    def test_disconnect_stays_down_and_reconnect_starts_now(self):
        self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha-fixture.net")
        self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta-fixture.net")
        self.call("connection.disconnect", instance="beta-fixture-net")
        self.assertEqual([e["instance"] for e in connections.launch_plan({}, workspace=self.ws)], ["alpha-fixture-net"],
                         "a relaunch of every lane leaves the disconnected one down")
        again = self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta-fixture.net")
        self.assertIsNone(again["launch"], "re-adding a parked row does not start it")
        self.spawned.clear()
        out = self.call("connection.reconnect", instance="beta-fixture-net")
        self.assertEqual((out["launch"]["ok"], self.spawned), (True, ["beta-fixture-net"]))

    def test_promoting_off_a_running_legacy_primary_is_a_conflict_naming_the_restart(self):
        from _helpers import bridge_lease
        connections.import_from_env({"REMOTE_TASK_TOKEN": TOKEN_A}, workspace=self.ws, vault=self.vault)
        self.call("connection.add", token="https://relay.beta-fixture.net|secret-beta", confirm=True,
                  expected_server="beta-fixture.net")
        legacy = bridge_lease.start(self.ws, "")
        try:
            with self.assertRaises(ProtocolError) as cm:
                self.call("connection.set_primary", instance="beta-fixture-net")
        finally:
            bridge_lease.stop(legacy)
        self.assertEqual(cm.exception.code, CONFLICT)
        self.assertTrue(str(cm.exception).startswith("legacy_primary_running"))
        self.assertIn("restart.sh --stop-only", str(cm.exception))
        self.assertEqual(connections.primary_instance(self.ws), "", "nothing moved")

    def test_promote_moves_the_primary_off_a_running_legacy_bridge_and_unpark_brings_it_back(self):
        from _helpers import bridge_lease
        started = []
        inner = bridge_lease.spawner(self.ws, started=started)

        def spawn(argv, env, log_path):
            proc = inner(argv, env, log_path)
            status = self.ws / "state" / f"gateway-status.{env['GATEWAY_INSTANCE']}.json"
            status.parent.mkdir(parents=True, exist_ok=True)
            status.write_text(json.dumps({"pid": proc.pid, "connected": True}))
            return proc
        self.view.spawn, self.view.kill = spawn, None
        connections.import_from_env({"REMOTE_TASK_TOKEN": TOKEN_A}, workspace=self.ws, vault=self.vault)
        connections.add_connection(TOKEN_B, added_by="owner", expected_server="beta-fixture.net", workspace=self.ws,
                                   vault=self.vault, preview=connections.preview_connection(TOKEN_B, fetch=self.gateway,
                                                                                            getaddrinfo=public_getaddrinfo))
        legacy = bridge_lease.start(self.ws, "")
        try:
            out = self.call("connection.promote", instance="beta-fixture-net")
            self.assertEqual((out["connection"]["primary"], out["previous"], out["kept"], out["stopped_pid"],
                              out["changed"], out["relaunch_needed"]), (True, None, "", 0, True, False))
            self.assertEqual(out["launch"]["pid"], started[0].pid)
            self.assertEqual(connections.bridge_pid("", self.ws), legacy.pid, "the legacy lane keeps running")
            with self.assertRaises(ProtocolError) as cm:
                self.call("connection.promote", instance="beta-fixture-net", park="yes")
            self.assertEqual(cm.exception.code, -32602)
            with self.assertRaises(ProtocolError) as cm:
                self.call("connection.park", instance="beta-fixture-net")
            self.assertTrue(str(cm.exception).startswith("primary_cannot_park"))
            out = self.call("connection.park_legacy")
            legacy.wait(5)
            self.assertEqual((out["primary"], out["parked"], out["stopped"]), ("beta-fixture-net", [""], {"": legacy.pid}))
            listed = {r["instance"]: r for r in self.call("connection.list")["connections"]}
            self.assertEqual((listed[""]["state"], listed[""]["reason"], listed[""]["parked"]), ("parked", "parked", True))
            out = self.call("connection.unpark", instance="")
            self.assertEqual((out["connection"]["parked"], out["launch"]), (False, None))
            with self.assertRaises(ProtocolError) as cm:
                self.call("connection.promote", instance="")
            self.assertEqual(cm.exception.code, CONFLICT)
            self.assertTrue(str(cm.exception).startswith("legacy_bridge_not_running"))
        finally:
            bridge_lease.stop(legacy)
            for proc in started:
                bridge_lease.stop(proc)

    def test_park_legacy_without_a_primary_is_a_conflict(self):
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.park_legacy")
        self.assertEqual(cm.exception.code, CONFLICT)

    def test_promote_and_unpark_of_an_unknown_instance_are_invalid_params(self):
        for method in ("connection.promote", "connection.park", "connection.unpark"):
            with self.assertRaises(ProtocolError) as cm:
                self.call(method, instance="nope")
            self.assertEqual(cm.exception.code, -32602, method)

    def test_confirm_against_a_moved_identity_is_a_conflict(self):
        preview = self.call("connection.add", token=TOKEN_A)["preview"]
        self.assertEqual((preview["server"], preview["verified"]), ("alpha-fixture.net", True))
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="chat.alpha-fixture.net")
        self.assertEqual(cm.exception.code, CONFLICT)
        self.assertTrue(str(cm.exception).startswith("mismatch"))
        self.assertIn("alpha-fixture.net", str(cm.exception))
        self.assertNotIn("secret-alpha", str(cm.exception))
        self.assertEqual(self.call("connection.list"), {"connections": [], "capabilities": ["legacy_readopt", "promote_keeps_lanes"]})
        self.assertEqual(self.vault.store, {})

    def test_status_state_and_set_primary(self):
        self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha-fixture.net")
        self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta-fixture.net")
        self.spawned.clear()
        (self.ws / "state" / "gateway-status.alpha-fixture-net.json").write_text(
            json.dumps({"connected": True, "last_ok_ts": 42}))
        listed = self.call("connection.list")["connections"]
        self.assertEqual([(r["instance"], r["primary"], r["state"]) for r in listed],
                         [("alpha-fixture-net", True, "connected"), ("beta-fixture-net", False, "pending")])
        self.assertEqual(listed[0]["status"], {"connected": True, "last_ok_ts": 42})
        self.assertNotIn("secret-", json.dumps(listed))

        out = self.call("connection.set_primary", instance="beta-fixture-net")
        self.assertTrue(out["connection"]["primary"])
        listed = self.call("connection.list")["connections"]
        self.assertEqual([r["primary"] for r in listed], [False, True])

    def test_remove_leaves_the_other_connection_alone(self):
        self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha-fixture.net")
        self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta-fixture.net")
        self.spawned.clear()
        self.call("connection.reconnect", instance="alpha-fixture-net")
        self.call("connection.reconnect", instance="beta-fixture-net")
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.remove", instance="alpha-fixture-net")
        self.assertIn("without force", str(cm.exception))
        gone = self.call("connection.remove", instance="beta-fixture-net")
        self.assertEqual(gone["removed"]["instance"], "beta-fixture-net")
        self.assertEqual(set(self.vault.store), {"AG2_REMOTE_TOKEN_ALPHA_FIXTURE_NET"})
        listed = self.call("connection.list")["connections"]
        self.assertEqual([(r["instance"], r["primary"], r["launched_at"] is not None) for r in listed],
                         [("alpha-fixture-net", True, True)])
        self.assertFalse((self.ws / "state" / "connections-launch" / "beta-fixture-net.json").exists())

    def test_reconnect_launches_exactly_that_bridge(self):
        self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha-fixture.net")
        self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta-fixture.net")
        self.spawned.clear()
        out = self.call("connection.reconnect", instance="beta-fixture-net")
        self.assertEqual((out["launch"]["ok"], out["launch"]["pid"], out["launch"]["stopped_pid"]), (True, DEAD_PID, 0))
        self.assertEqual(self.spawned, ["beta-fixture-net"])
        with self.assertRaises(ProtocolError):
            self.call("connection.reconnect", instance="nope")

    def test_disconnect_keeps_the_row_and_reconnect_resumes_it(self):
        self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha-fixture.net")
        self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta-fixture.net")
        self.spawned.clear()
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.disconnect", instance="alpha-fixture-net")
        self.assertEqual(cm.exception.code, CONFLICT)
        self.assertTrue(str(cm.exception).startswith("primary_cannot_disconnect"))
        out = self.call("connection.disconnect", instance="beta-fixture-net")
        row = out["connection"]
        self.assertEqual((row["instance"], row["disabled"], row["state"], row["reason"]),
                         ("beta-fixture-net", True, "parked", "disabled"))
        self.assertNotIn("secret-", json.dumps(out))
        self.assertIn("AG2_REMOTE_TOKEN_BETA_FIXTURE_NET", self.vault.store, "the token stays")
        listed = self.call("connection.list")["connections"]
        self.assertEqual([(r["instance"], r["disabled"], r["state"]) for r in listed],
                         [("alpha-fixture-net", False, "pending"), ("beta-fixture-net", True, "parked")])
        self.assertEqual([r.get("agent_display_name") for r in listed], [None, None], "identity fields are spelled out")
        out = self.call("connection.reconnect", instance="beta-fixture-net")
        self.assertEqual((out["launch"]["ok"], self.spawned), (True, ["beta-fixture-net"]))
        listed = self.call("connection.list")["connections"]
        self.assertEqual([(r["instance"], r["disabled"], r["state"]) for r in listed],
                         [("alpha-fixture-net", False, "pending"), ("beta-fixture-net", False, "pending")])
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.disconnect", instance="nope")
        self.assertEqual(cm.exception.code, -32602)

    def test_update_token_replaces_the_secret_or_answers_conflict(self):
        self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha-fixture.net")
        self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta-fixture.net")
        self.spawned.clear()
        self.call("connection.disconnect", instance="beta-fixture-net")
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.update_token", instance="beta-fixture-net", token=TOKEN_A)
        self.assertEqual(cm.exception.code, CONFLICT)
        self.assertTrue(str(cm.exception).startswith("token_server_mismatch"))
        self.gateway.agents["https://relay.beta-fixture.net"] = ["@other:beta-fixture.net"]
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.update_token", instance="beta-fixture-net", token="https://relay.beta-fixture.net|rotated")
        self.assertEqual(cm.exception.code, CONFLICT)
        self.assertTrue(str(cm.exception).startswith("token_agent_mismatch"))
        self.assertNotIn("rotated", str(cm.exception))
        self.assertEqual((self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], self.spawned), (TOKEN_B, []),
                         "a refused code touches nothing")
        self.gateway.agents["https://relay.beta-fixture.net"] = ["@sutando:beta-fixture.net"]
        out = self.call("connection.update_token", instance="beta-fixture-net", token="https://relay.beta-fixture.net|rotated")
        row = out["connection"]
        self.assertEqual((row["instance"], row["token_ref"], row["disabled"], row["state"], row["primary"]),
                         ("beta-fixture-net", "AG2_REMOTE_TOKEN_BETA_FIXTURE_NET", False, "pending", False))
        self.assertEqual(self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], "https://relay.beta-fixture.net|rotated")
        self.assertEqual(self.spawned, ["beta-fixture-net"], "relaunched once")
        self.assertNotIn("rotated", json.dumps(out))
        for params in ({"instance": "beta-fixture-net"}, {"instance": "beta-fixture-net", "token": ""}, {"token": TOKEN_B}):
            with self.assertRaises(ProtocolError, msg=params) as cm:
                self.call("connection.update_token", **params)
            self.assertEqual(cm.exception.code, -32602)
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.update_token", instance="nope", token=TOKEN_B)
        self.assertEqual(cm.exception.code, -32602)
        self.assertIn("nope", str(cm.exception))

    def test_token_state_unknown_is_a_conflict_with_the_same_code(self):
        self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha-fixture.net")
        self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta-fixture.net")
        self.spawned.clear()
        refusal = connections.TokenStateUnknown("token_state_unknown: the update of dev failed after the vault write began (OSError: disk full) and reading AG2_REMOTE_TOKEN_DEV back failed, so it is not known which secret it holds")
        with mock.patch.object(connections, "update_token", side_effect=refusal):
            with self.assertRaises(ProtocolError) as cm:
                self.call("connection.update_token", instance="beta-fixture-net", token="https://relay.beta-fixture.net|rotated")
        self.assertEqual(cm.exception.code, CONFLICT)
        self.assertTrue(str(cm.exception).startswith("token_state_unknown"))
        self.assertNotIn("rotated", str(cm.exception))

    def test_every_coded_refusal_is_a_conflict_on_this_surface_too(self):
        """The same class answers CONFLICT here and 409 over HTTP: a rejected code (was -32602), a
        disabled row made primary, an unconfirmed agent, a shadowed token source, the legacy row."""
        import os
        self.call("connection.add", token=TOKEN_A, confirm=True, expected_server="alpha-fixture.net")
        self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta-fixture.net")
        self.spawned.clear()
        self.gateway.agents["https://rejecting-fixture.net"] = "reject"
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.add", token="https://rejecting-fixture.net|bad", confirm=True,
                      expected_server="rejecting-fixture.net")
        self.assertEqual((cm.exception.code, str(cm.exception)[:14]), (CONFLICT, "token_rejected"))
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.update_token", instance="beta-fixture-net", token="https://rejecting-fixture.net|bad")
        self.assertEqual((cm.exception.code, str(cm.exception)[:14]), (CONFLICT, "token_rejected"),
                         "a rejected code is refused before the server check")
        self.gateway.agents["https://relay.beta-fixture.net"] = "reject"
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.update_token", instance="beta-fixture-net", token="https://relay.beta-fixture.net|rotated")
        self.assertEqual((cm.exception.code, str(cm.exception)[:14]), (CONFLICT, "token_rejected"))
        self.gateway.agents["https://relay.beta-fixture.net"] = []
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.update_token", instance="beta-fixture-net", token="https://relay.beta-fixture.net|rotated")
        self.assertEqual(cm.exception.code, CONFLICT)
        self.assertTrue(str(cm.exception).startswith("token_unverified"))
        self.gateway.agents["https://relay.beta-fixture.net"] = ["@sutando:beta-fixture.net"]
        os.environ["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"] = TOKEN_B
        try:
            with self.assertRaises(ProtocolError) as cm:
                self.call("connection.update_token", instance="beta-fixture-net", token="https://relay.beta-fixture.net|rotated")
        finally:
            del os.environ["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"]
        self.assertEqual(cm.exception.code, CONFLICT)
        self.assertTrue(str(cm.exception).startswith("token_source_shadowed: AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"))
        self.assertEqual((self.vault.store["AG2_REMOTE_TOKEN_BETA_FIXTURE_NET"], self.spawned), (TOKEN_B, []),
                         "every refusal left the vault and the bridge alone")
        self.call("connection.disconnect", instance="beta-fixture-net")
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.set_primary", instance="beta-fixture-net")
        self.assertEqual(cm.exception.code, CONFLICT)
        self.assertTrue(str(cm.exception).startswith("disabled_cannot_be_primary"))
        self.assertEqual([r["primary"] for r in self.call("connection.list")["connections"]], [True, False])
        connections.import_from_env({"REMOTE_TASK_TOKEN": "https://chat.legacy-fixture.net|legacy"}, workspace=self.ws,
                                    vault=self.vault)
        for method in ("connection.disconnect", "connection.update_token"):
            with self.assertRaises(ProtocolError, msg=method) as cm:
                self.call(method, instance="", token="https://chat.legacy-fixture.net|rotated")
            self.assertEqual(cm.exception.code, CONFLICT)
            self.assertTrue(str(cm.exception).startswith("legacy_row_unmanaged"))
        self.assertEqual(self.vault.store["REMOTE_TASK_TOKEN"], "https://chat.legacy-fixture.net|legacy")
        out = self.call("connection.set_primary", instance="alpha-fixture-net")
        self.assertFalse(out["connection"]["relaunch_needed"], "the hint rides on the row")

    def test_known_servers_joins_the_registry_with_the_rows(self):
        fixtures = [dotspace.DotSpace(server="alpha-fixture.net"), dotspace.DotSpace(server="beta-fixture.net")]
        with unittest.mock.patch.object(dotspace, "registered_instances", lambda: fixtures):
            self.assertEqual(self.call("connection.known_servers"), {"servers": [
                {"server": "alpha-fixture.net", "connected": False, "instance": None, "primary": False, "disabled": False},
                {"server": "beta-fixture.net", "connected": False, "instance": None, "primary": False, "disabled": False}]})
            self.call("connection.add", token=TOKEN_B, confirm=True, expected_server="beta-fixture.net")
            out = self.call("connection.known_servers")["servers"]
        self.assertEqual([(s["server"], s["connected"], s["instance"], s["primary"]) for s in out],
                         [("alpha-fixture.net", False, None, False), ("beta-fixture.net", True, "beta-fixture-net", True)])
        self.assertNotIn("token_ref", json.dumps(out))
        (self.ws / "state" / "connections.json").write_text("{nope")
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.known_servers")
        self.assertEqual(cm.exception.code, -32603)

    def test_param_validation(self):
        for params in ({}, {"token": ""}, {"token": 3}, {"token": TOKEN_A, "confirm": "yes"},
                       {"token": TOKEN_A, "confirm": True}, {"token": TOKEN_A, "confirm": True, "expected_server": ""}):
            with self.assertRaises(ProtocolError, msg=params):
                self.call("connection.add", **params)
        with self.assertRaises(ProtocolError) as cm:
            self.call("connection.add", token="opaque-bearer")
        self.assertIn("no gateway URL", str(cm.exception))
        for method in ("connection.remove", "connection.set_primary", "connection.disconnect", "connection.reconnect"):
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
