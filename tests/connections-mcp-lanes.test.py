#!/usr/bin/env python3
"""Contract tests for the MCP lane descriptors src/connections.py writes for the s-space proxy.

Each enabled core connection the main descriptor does not serve gets `ag2-mcp.d/conn-<instance>.json`
(the proxy's lane descriptor shape) and a 0600 `conn-<instance>.env` holding its bearer under the
row's `token_ref`; anything else under `conn-*` is removed. A disabled, parked or removed connection
loses its lane. Without a readable main descriptor nothing is touched. No secret reaches a result,
stdout, stderr or the log. The record, the vault, the resolver and the descriptor are fixtures.

Run: python3 tests/connections-mcp-lanes.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import hashlib
import io
import json
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import connections as C  # noqa: E402
import dotspace  # noqa: E402

MINT_PATH = "/api/v1/mcp/agent-access-tokens"
REGISTRY = {"instances": [
    {"server": "ag2.space", "gateway_host": "chat.ag2.space", "mcp_url": "https://mcp.ag2.space/mcp"},
    {"server": "s.space", "gateway_host": "chat.s.space", "mcp_url": "https://mcp.s.space/mcp"},
    {"server": "dev.s.space", "gateway_host": "dev-chat.s.space", "mcp_url": "https://dev-mcp.s.space/mcp"},
    {"server": "nomcp-fixture.net", "gateway_host": "chat.nomcp-fixture.net"},
]}
SECRETS = {"REMOTE_TASK_TOKEN": "https://chat.ag2.space/relay|secret-legacy",
           "AG2_REMOTE_TOKEN_S_SPACE": "https://chat.s.space/relay|secret-s",
           "AG2_REMOTE_TOKEN_DEV_S_SPACE": "https://dev-chat.s.space/relay|secret-dev",
           "AG2_REMOTE_TOKEN_NOMCP_FIXTURE_NET": "https://chat.nomcp-fixture.net/relay|secret-nomcp"}


def space_of(name):
    return dotspace.dotspace_for(name, fetch=dotspace.NO_FETCH, config=REGISTRY)


def row(server, instance, gateway_url, *, primary=False, **extra):
    r = {"server": server, "instance": instance, "gateway_url": gateway_url,
         "token_ref": C.token_ref_for(instance), "primary": primary, "added_by": "env",
         "added_at": "2026-10-01T00:00:00Z", "verified": True, "server_source": "identity", "disabled": False}
    r.update(extra)
    return r


LEGACY = row("ag2.space", "", "https://chat.ag2.space/relay", agent_mxid="@bot:ag2.space")
S_SPACE = row("s.space", "s-space", "https://chat.s.space/relay", primary=True, agent_mxid="@bot:s.space")
DEV_S = row("dev.s.space", "dev-s-space", "https://dev-chat.s.space/relay", agent_mxid="@bot:dev.s.space")


class FakeVault:
    def __init__(self, store=None):
        self.store = dict(SECRETS if store is None else store)

    def get(self, key):
        if key not in self.store:
            raise KeyError(key)
        return self.store[key]

    def delete(self, key):
        self.store.pop(key, None)


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.lanes = self.ws / "state" / C.MCP_LANES_DIR_NAME
        self.vault = FakeVault()
        self.main = self.write_main()

    def record(self, rows):
        path = C.record_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": C.SCHEMA_VERSION, "connections": rows}))

    def write_main(self, **fields):
        d = {"version": 1, "lane": "s_dspace", "env_file": "/elsewhere/.env", "env_key": "AG2_REMOTE_TOKEN_S_DSPACE",
             "mint_url": "https://chat.s.space" + MINT_PATH, "mcp_url": "https://mcp.s.space/mcp", "ca_bundle": None,
             "agent_id": "@bot:s.space", "relay": "https://chat.s.space/relay", "bearer_digest": "ab" * 32, **fields}
        path = self.ws / "outside" / "ag2-mcp.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(d))
        return path

    def sync(self, **kw):
        kw.setdefault("environ", {})
        kw.setdefault("vault", self.vault)
        kw.setdefault("descriptor", self.main)
        kw.setdefault("mcp_config", self.ws / "no-config.json")
        kw.setdefault("space_of", space_of)
        return C.sync_mcp_lanes(self.ws, **kw)

    def lane(self, slug):
        return json.loads((self.lanes / f"conn-{slug}.json").read_text())

    def names(self):
        return sorted(p.name for p in self.lanes.iterdir()) if self.lanes.is_dir() else []


class Writes(Base):
    def test_each_enabled_connection_but_the_main_ones_gets_a_lane_with_its_own_endpoints(self):
        self.record([LEGACY, S_SPACE, DEV_S])
        out = self.sync()
        self.assertEqual((out["ok"], sorted(out["written"]), out["removed"], out["skipped"]),
                         (True, ["dev-s-space", "legacy"], [], {}))
        self.assertEqual(self.names(), ["conn-dev-s-space.env", "conn-dev-s-space.json",
                                        "conn-legacy.env", "conn-legacy.json"])
        env_file = self.lanes / "conn-dev-s-space.env"
        self.assertEqual(self.lane("dev-s-space"), {
            "version": 1, "lane": "dev-s-space", "server": "dev.s.space", "env_file": str(env_file),
            "env_key": "AG2_REMOTE_TOKEN_DEV_S_SPACE", "mint_url": "https://dev-chat.s.space" + MINT_PATH,
            "mcp_url": "https://dev-mcp.s.space/mcp", "ca_bundle": None, "agent_id": "@bot:dev.s.space",
            "relay": "https://dev-chat.s.space/relay",
            "bearer_digest": hashlib.sha256(b"secret-dev").hexdigest()})
        legacy = self.lane("legacy")
        self.assertEqual((legacy["lane"], legacy["env_key"], legacy["mint_url"], legacy["mcp_url"]),
                         ("legacy", "REMOTE_TASK_TOKEN", "https://chat.ag2.space" + MINT_PATH, "https://mcp.ag2.space/mcp"))

    def test_the_env_file_is_0600_and_holds_the_token_under_the_descriptors_key(self):
        self.record([S_SPACE, DEV_S])
        self.sync()
        env_file = self.lanes / "conn-dev-s-space.env"
        self.assertEqual(stat.S_IMODE(env_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.lanes / "conn-dev-s-space.json").stat().st_mode), 0o600)
        self.assertEqual(env_file.read_text(), "AG2_REMOTE_TOKEN_DEV_S_SPACE='https://dev-chat.s.space/relay|secret-dev'\n")

    def test_a_bare_secret_is_written_behind_the_rows_relay(self):
        self.record([S_SPACE, DEV_S])
        self.vault.store["AG2_REMOTE_TOKEN_DEV_S_SPACE"] = "secret-dev"
        self.sync()
        self.assertEqual((self.lanes / "conn-dev-s-space.env").read_text(),
                         "AG2_REMOTE_TOKEN_DEV_S_SPACE='https://dev-chat.s.space/relay|secret-dev'\n")
        self.assertEqual(self.lane("dev-s-space")["bearer_digest"], hashlib.sha256(b"secret-dev").hexdigest())

    def test_a_second_sync_rewrites_nothing(self):
        self.record([S_SPACE, DEV_S])
        self.sync()
        before = (self.lanes / "conn-dev-s-space.json").stat().st_mtime_ns
        out = self.sync()
        self.assertEqual((out["written"], out["unchanged"]), ([], ["dev-s-space"]))
        self.assertEqual((self.lanes / "conn-dev-s-space.json").stat().st_mtime_ns, before)

    def test_a_rotated_secret_rewrites_the_env_and_the_digest(self):
        self.record([S_SPACE, DEV_S])
        self.sync()
        self.vault.store["AG2_REMOTE_TOKEN_DEV_S_SPACE"] = "https://dev-chat.s.space/relay|secret-dev-2"
        self.assertEqual(self.sync()["written"], ["dev-s-space"])
        self.assertEqual(self.lane("dev-s-space")["bearer_digest"], hashlib.sha256(b"secret-dev-2").hexdigest())

    def test_an_unlearned_agent_is_null_and_the_server_comes_from_the_row(self):
        self.record([S_SPACE, row("dev.s.space", "dev-s-space", "https://dev-chat.s.space/relay")])
        self.sync()
        self.assertEqual((self.lane("dev-s-space")["agent_id"], self.lane("dev-s-space")["server"]),
                         (None, "dev.s.space"))

    def test_the_main_descriptors_ca_bundle_is_carried(self):
        self.main = self.write_main(ca_bundle="/certs/ca.pem")
        self.record([S_SPACE, DEV_S])
        self.sync()
        self.assertEqual(self.lane("dev-s-space")["ca_bundle"], "/certs/ca.pem")


class Skips(Base):
    def test_the_row_the_main_descriptor_serves_gets_no_lane_by_server(self):
        self.record([S_SPACE, DEV_S])
        self.assertNotIn("s-space", self.sync()["written"])
        self.assertFalse((self.lanes / "conn-s-space.json").exists())

    def test_the_main_lane_is_matched_by_relay_when_it_names_no_agent(self):
        self.main = self.write_main(agent_id=None)
        self.record([S_SPACE, DEV_S])
        self.assertEqual(self.sync()["written"], ["dev-s-space"])

    def test_the_primary_flag_is_not_what_decides_it(self):
        self.record([row("s.space", "s-space", "https://chat.s.space/relay", agent_mxid="@bot:s.space"),
                     row("dev.s.space", "dev-s-space", "https://dev-chat.s.space/relay", primary=True)])
        self.assertEqual(self.sync()["written"], ["dev-s-space"])

    def test_a_worker_identity_is_never_a_lane(self):
        self.record([S_SPACE, row("dev.s.space", "dev-s-space-w1", "https://dev-chat.s.space/relay", worker="w1")])
        self.assertEqual((self.sync()["written"], self.names()), ([], []))

    def test_a_row_without_a_token_or_endpoints_is_skipped_by_reason_and_its_old_lane_removed(self):
        nomcp = row("nomcp-fixture.net", "nomcp-fixture-net", "https://chat.nomcp-fixture.net/relay")
        self.record([S_SPACE, DEV_S, nomcp])
        self.lanes.mkdir(parents=True)
        (self.lanes / "conn-dev-s-space.json").write_text("{}")
        del self.vault.store["AG2_REMOTE_TOKEN_DEV_S_SPACE"]
        out = self.sync()
        self.assertEqual(out["skipped"], {"dev-s-space": "no_token", "nomcp-fixture-net": "no_endpoints"})
        self.assertEqual((out["removed"], self.names()), (["dev-s-space"], []))

    def test_a_plaintext_relay_off_loopback_is_refused(self):
        self.record([S_SPACE, row("dev.s.space", "dev-s-space", "http://dev-chat.s.space/relay")])
        self.vault.store["AG2_REMOTE_TOKEN_DEV_S_SPACE"] = "http://dev-chat.s.space/relay|secret-dev"
        self.assertEqual(self.sync()["skipped"], {"dev-s-space": "no_endpoints"})

    def test_no_main_descriptor_touches_nothing(self):
        self.record([S_SPACE, DEV_S])
        self.lanes.mkdir(parents=True)
        (self.lanes / "conn-gone.json").write_text("{}")
        out = self.sync(descriptor=self.ws / "missing.json")
        self.assertEqual((out["ok"], out["reason"], self.names()), (False, "no_main_descriptor", ["conn-gone.json"]))


class Removes(Base):
    def test_a_connection_that_is_gone_loses_its_lane_and_other_files_stay(self):
        self.record([S_SPACE, DEV_S])
        self.sync()
        (self.lanes / "desktop-own.json").write_text("{}")
        (self.lanes / "conn-old.env").write_text("X='y'\n")
        self.record([S_SPACE])
        out = self.sync()
        self.assertEqual(sorted(out["removed"]), ["dev-s-space", "old"])
        self.assertEqual(self.names(), ["desktop-own.json"])

    def test_a_disabled_or_parked_connection_loses_its_lane_on_sync(self):
        for flag in ("disabled", "parked"):
            self.record([S_SPACE, DEV_S])
            self.sync()
            self.record([S_SPACE, dict(DEV_S, **{flag: True})])
            self.assertEqual(self.sync()["removed"], ["dev-s-space"], flag)
            self.assertEqual(self.names(), [], flag)

    def test_disconnect_park_and_remove_drop_the_lane_at_once(self):
        cases = {"disconnect": lambda: C.disconnect("dev-s-space", workspace=self.ws, kill=self.no_kill),
                 "park": lambda: C.park("dev-s-space", workspace=self.ws, kill=self.no_kill),
                 "park_legacy": lambda: C.park_legacy(workspace=self.ws, kill=self.no_kill),
                 "remove": lambda: C.remove_connection("dev-s-space", workspace=self.ws, vault=self.vault,
                                                       kill=self.no_kill)}
        for name, act in cases.items():
            self.record([S_SPACE, DEV_S])
            self.sync()
            self.assertIn("conn-dev-s-space.json", self.names(), name)
            with unittest.mock.patch.object(C, "_stop_row", lambda *a, **k: 0):
                act()
            self.assertEqual(self.names(), [], name)

    def no_kill(self, *a):
        self.fail("no bridge runs")

    def test_dropping_honours_the_lanes_dir_override_and_a_missing_lane_is_false(self):
        other = self.ws / "elsewhere"
        self.record([S_SPACE, DEV_S])
        self.sync(environ={C.MCP_LANES_DIR_ENV: str(other)})
        self.assertTrue((other / "conn-dev-s-space.json").is_file())
        self.assertTrue(C.drop_mcp_lane("dev-s-space", self.ws, {C.MCP_LANES_DIR_ENV: str(other)}))
        self.assertFalse(C.drop_mcp_lane("dev-s-space", self.ws, {C.MCP_LANES_DIR_ENV: str(other)}))

    def test_a_drop_that_cannot_unlink_is_logged_and_false(self):
        with unittest.mock.patch.object(C, "_unlink_lane", side_effect=PermissionError()), \
                self.assertLogs("connections", "WARNING") as logs:
            self.assertFalse(C.drop_mcp_lane("dev-s-space", self.ws, {}))
        self.assertIn("PermissionError", "".join(logs.output))


class DropSyncRace(Base):
    def test_a_sync_that_read_the_row_as_enabled_cannot_write_the_lane_back_after_a_drop(self):
        self.record([S_SPACE, DEV_S])
        reading, go = threading.Event(), threading.Event()

        def slow_space_of(name):
            if name == "dev.s.space":
                reading.set()
                go.wait(5)
            return space_of(name)

        syncer = threading.Thread(target=lambda: self.sync(space_of=slow_space_of))
        syncer.start()
        self.assertTrue(reading.wait(5))
        # The row turns disabled after the sync read it, then its lane is dropped.
        self.record([S_SPACE, dict(DEV_S, disabled=True)])
        dropper = threading.Thread(target=lambda: C.drop_mcp_lane("dev-s-space", self.ws, {}))
        dropper.start()
        dropper.join(0.3)
        self.assertTrue(dropper.is_alive(), "the drop waits for the sync's lanes lock")
        go.set()
        syncer.join(5)
        dropper.join(5)
        self.assertEqual(self.names(), [])


class Quoting(Base):
    def test_a_token_holding_a_quote_gets_no_lane_and_its_old_lane_is_removed(self):
        self.record([S_SPACE, DEV_S])
        self.sync()
        self.vault.store["AG2_REMOTE_TOKEN_DEV_S_SPACE"] = "https://dev-chat.s.space/relay|sec'ret"
        with self.assertLogs("connections", "WARNING") as logs:
            out = self.sync()
        self.assertEqual((out["skipped"], out["removed"]), ({"dev-s-space": "unquotable_token"}, ["dev-s-space"]))
        self.assertEqual(self.names(), [])
        self.assertIn("AG2_REMOTE_TOKEN_DEV_S_SPACE", "".join(logs.output))
        self.assertNotIn("sec'ret", "".join(logs.output))

    def test_a_line_break_is_refused_too_and_a_plain_value_is_single_quoted(self):
        path = self.ws / "x.env"
        for bad in ("a\nB=c", "a\rb"):
            with self.assertRaises(C.UnquotableValue), self.assertLogs("connections", "WARNING"):
                C._env_file_with(path, "K", bad)
        self.assertEqual(C._env_file_with(path, "K", "u|s-_.~"), "K='u|s-_.~'\n")


class TempSweep(Base):
    def test_a_temp_file_a_crash_left_is_swept_and_other_dotfiles_stay(self):
        self.record([S_SPACE, DEV_S])
        self.lanes.mkdir(parents=True)
        for name in (".conn-dev-s-space.env.k3j9.tmp", ".conn-gone.json.a1.tmp", ".other.tmp", ".conn-keep"):
            (self.lanes / name).write_text("x")
        self.sync()
        self.assertEqual(self.names(), [".conn-keep", ".other.tmp", "conn-dev-s-space.env", "conn-dev-s-space.json"])


class MainDescriptor(Base):
    def test_found_through_the_environment_then_the_mcp_config_then_the_workspace(self):
        self.record([S_SPACE, DEV_S])
        self.assertTrue(self.sync(descriptor=None, environ={"AG2_MCP_DESCRIPTOR": str(self.main)})["ok"])
        config = self.ws / "claude.json"
        config.write_text(json.dumps({"mcpServers": {"x": "junk", "s-space": {"env": {"AG2_MCP_DESCRIPTOR": str(self.main)}}}}))
        self.assertTrue(self.sync(descriptor=None, mcp_config=config)["ok"])
        self.assertFalse(self.sync(descriptor=None)["ok"])
        (self.ws / "state" / C.MCP_DESCRIPTOR_NAME).write_text(self.main.read_text())
        self.assertTrue(self.sync(descriptor=None)["ok"])

    def test_the_default_mcp_config_is_the_claude_homes(self):
        self.record([S_SPACE, DEV_S])
        home = self.ws / "claude-home"
        home.mkdir()
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {"s-space": {"env": {"AG2_MCP_DESCRIPTOR": str(self.main)}}}}))
        with unittest.mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(home)}):
            self.assertTrue(self.sync(descriptor=None, mcp_config=None)["ok"])

    def test_an_unsupported_descriptor_is_none(self):
        self.record([S_SPACE, DEV_S])
        self.main = self.write_main(version=2)
        self.assertFalse(self.sync()["ok"])


class Lifecycle(Base):
    def test_a_successful_launch_syncs_and_a_failed_sync_never_fails_the_launch(self):
        calls = []
        with unittest.mock.patch.object(C, "_launch", return_value={"ok": True}), \
                unittest.mock.patch.object(C, "sync_mcp_lanes", lambda ws, **kw: calls.append(ws)):
            self.assertEqual(C.launch("dev-s-space", repo=self.ws, logs_dir=self.ws, workspace=self.ws), {"ok": True})
        self.assertEqual(calls, [self.ws])
        with unittest.mock.patch.object(C, "_launch", return_value={"ok": False}), \
                unittest.mock.patch.object(C, "sync_mcp_lanes", lambda ws, **kw: calls.append(ws)):
            C.launch("dev-s-space", repo=self.ws, logs_dir=self.ws, workspace=self.ws)
        self.assertEqual(calls, [self.ws], "a failed launch does not sync")
        with unittest.mock.patch.object(C, "_launch", return_value={"ok": True}), \
                unittest.mock.patch.object(C, "sync_mcp_lanes", side_effect=OSError("disk")), \
                self.assertLogs("connections", "WARNING") as logs:
            self.assertEqual(C.launch("dev-s-space", repo=self.ws, logs_dir=self.ws, workspace=self.ws), {"ok": True})
        self.assertIn("MCP lane sync failed (OSError)", "".join(logs.output))


class LifecycleReconcile(Base):
    """The lanes follow the committed record after every lifecycle change, launch or not: a row
    promote parks loses its lane, and the legacy row unpark brings back regains it."""

    def fixture_sync(self):
        # The refresh the lifecycle runs is the real sync, aimed at this test's descriptor.
        real = C.sync_mcp_lanes
        return unittest.mock.patch.object(
            C, "sync_mcp_lanes",
            lambda ws, **kw: real(ws, environ={}, vault=self.vault, descriptor=self.main,
                                  mcp_config=self.ws / "no-config.json", space_of=space_of))

    def test_promote_with_park_drops_the_parked_owners_lane(self):
        self.record([dict(DEV_S, primary=True), dict(S_SPACE, primary=False), LEGACY])
        self.sync()
        self.assertIn("conn-dev-s-space.json", self.names())
        # The new primary's bridge starts and connects; the launch hook does not run here, so only
        # the reconcile after the committed write can drop the parked row's lane.
        with self.fixture_sync(), \
                unittest.mock.patch.object(C, "start", return_value={"ok": True, "pid": 7, "lease": "held"}), \
                unittest.mock.patch.object(C, "_await_connected", lambda *a, **k: None), \
                unittest.mock.patch.object(C, "_connected_bridge", lambda *a, **k: 0), \
                unittest.mock.patch.object(C, "_stop_row", lambda *a, **k: 0), \
                unittest.mock.patch.object(C, "bridge_pid", lambda *a, **k: 0):
            out = C.promote("s-space", repo=self.ws, logs_dir=self.ws, environ={}, workspace=self.ws,
                            vault=self.vault, park=True)
        self.assertEqual(out["previous"], "dev-s-space")
        self.assertNotIn("conn-dev-s-space.json", self.names())
        self.assertNotIn("conn-dev-s-space.env", self.names())
        self.assertIn("conn-legacy.json", self.names(), "an unparked row keeps its lane")

    def test_promote_of_the_legacy_row_with_park_drops_the_parked_owners_lane(self):
        self.record([dict(DEV_S, primary=True), LEGACY])
        self.sync()
        self.assertIn("conn-dev-s-space.json", self.names())
        with self.fixture_sync(), \
                unittest.mock.patch.object(C, "_stop_row", lambda *a, **k: 0), \
                unittest.mock.patch.object(C, "bridge_pid", lambda inst, *a, **k: 9 if inst == "" else 0):
            out = C.promote("", repo=self.ws, logs_dir=self.ws, environ={}, workspace=self.ws,
                            vault=self.vault, park=True)
        self.assertEqual(out["previous"], "dev-s-space")
        self.assertEqual([n for n in self.names() if n.startswith("conn-dev-s-space")], [])
        self.assertIn("conn-legacy.json", self.names())

    def test_park_then_unpark_of_the_legacy_row_brings_its_lane_back(self):
        self.record([S_SPACE, LEGACY])
        self.sync()
        self.assertEqual(self.names(), ["conn-legacy.env", "conn-legacy.json"])
        with unittest.mock.patch.object(C, "_stop_row", lambda *a, **k: 0):
            C.park("", workspace=self.ws, kill=lambda *a: None)
        self.assertEqual(self.names(), [], "parking drops it at once")
        with self.fixture_sync():
            out = C.unpark("", repo=self.ws, logs_dir=self.ws, environ={}, workspace=self.ws, vault=self.vault)
        self.assertIsNone(out["launch"], "the legacy row gets no launch")
        self.assertEqual(self.names(), ["conn-legacy.env", "conn-legacy.json"])

    def promote_parked_dev(self, raises=None, **patches):
        # s.space is primary and dev.s.space parked; the real start/launch hook writes dev's lane.
        self.record([S_SPACE, dict(DEV_S, parked=True), LEGACY])
        self.sync()
        self.assertNotIn("conn-dev-s-space.json", self.names())
        base = {"_launch": lambda *a, **k: {"ok": True, "pid": 7, "lease": "held"},
                "_proven_holder": lambda *a, **k: 0, "_connected_bridge": lambda *a, **k: 0,
                "_stop_row": lambda *a, **k: 0, "bridge_pid": lambda *a, **k: 0,
                "stop_bridge": lambda *a, **k: 0}
        base.update(patches)
        with contextlib.ExitStack() as stack:
            stack.enter_context(self.fixture_sync())
            for name, fn in base.items():
                stack.enter_context(unittest.mock.patch.object(C, name, fn))
            with self.assertRaises(raises or C.ConnectionsError) as cm:
                C.promote("dev-s-space", repo=self.ws, logs_dir=self.ws, environ={}, workspace=self.ws,
                          vault=self.vault, park=True, connect_timeout_s=0)
        rows = {r["instance"]: r for r in C._read_record(C.record_path(self.ws))["rows"]}
        self.assertTrue(rows["dev-s-space"].get("parked"))
        self.assertFalse(rows["dev-s-space"]["primary"])
        return cm.exception

    def test_a_promotion_that_never_connects_rolls_the_parked_rows_lane_back(self):
        def timeout(instance, *a, **k):
            raise C.NewPrimaryNotConnected(instance, "timeout")
        self.assertIsInstance(self.promote_parked_dev(_await_connected=timeout), C.NewPrimaryNotConnected)
        self.assertEqual([n for n in self.names() if n.startswith("conn-dev-s-space")], [])

    def test_a_refused_old_owner_stop_rolls_the_parked_rows_lane_back(self):
        def refuse(row, *a, **k):
            if row is not None and row.get("instance") == "s-space":
                raise C.BridgeStillRunning("s-space", 9)
            return 0
        self.promote_parked_dev(_await_connected=lambda *a, **k: None, _stop_row=refuse)
        self.assertEqual([n for n in self.names() if n.startswith("conn-dev-s-space")], [])

    def test_a_restart_that_raises_after_a_never_connected_promotion_still_rolls_the_lane_back(self):
        def timeout(instance, *a, **k):
            raise C.NewPrimaryNotConnected(instance, "timeout")
        err = self.promote_parked_dev(raises=RuntimeError, _await_connected=timeout,
                                      _restart_secondary=unittest.mock.Mock(side_effect=RuntimeError("restart")))
        self.assertIsInstance(err.__context__, C.NewPrimaryNotConnected, "the rollback's cause is kept")
        self.assertEqual([n for n in self.names() if n.startswith("conn-dev-s-space")], [])

    def test_a_restart_that_raises_after_a_refused_stop_still_rolls_the_lane_back(self):
        def refuse(row, *a, **k):
            if row is not None and row.get("instance") == "s-space":
                raise C.BridgeStillRunning("s-space", 9)
            return 0
        err = self.promote_parked_dev(raises=RuntimeError, _await_connected=lambda *a, **k: None, _stop_row=refuse,
                                      _restart_secondary=unittest.mock.Mock(side_effect=RuntimeError("restart")))
        self.assertIsInstance(err.__cause__, C.BridgeStillRunning, "the rollback's cause is kept")
        self.assertEqual([n for n in self.names() if n.startswith("conn-dev-s-space")], [])

    def test_a_failed_reconcile_never_fails_the_change(self):
        self.record([S_SPACE, dict(LEGACY, parked=True)])
        with unittest.mock.patch.object(C, "sync_mcp_lanes", side_effect=OSError("disk")), \
                self.assertLogs("connections", "WARNING") as logs:
            out = C.unpark("", repo=self.ws, logs_dir=self.ws, environ={}, workspace=self.ws, vault=self.vault)
        self.assertNotIn("parked", out)
        self.assertIn("MCP lane sync failed (OSError)", "".join(logs.output))


class NoSecretLeaks(Base):
    def run_cli(self, *argv):
        import importlib.util
        spec = importlib.util.spec_from_file_location("connections_cli", ROOT / "scripts" / "connections.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        out, err = io.StringIO(), io.StringIO()
        real = C.sync_mcp_lanes

        def sync(ws, **kw):
            return real(ws, environ={}, vault=self.vault, mcp_config=self.ws / "no-config.json", space_of=space_of,
                        descriptor=kw.get("descriptor"))
        with unittest.mock.patch.object(cli.connections, "sync_mcp_lanes", sync), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--workspace", str(self.ws), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_no_secret_in_results_cli_output_logs_or_descriptors(self):
        nomcp = row("nomcp-fixture.net", "nomcp-fixture-net", "https://chat.nomcp-fixture.net/relay")
        self.record([LEGACY, S_SPACE, DEV_S, nomcp])
        with self.assertLogs("connections", "DEBUG") as logs:
            C.logging.getLogger("connections").debug("start")
            result = self.sync()
            text = self.run_cli("sync-mcp-lanes", "--descriptor", str(self.main))
            js = self.run_cli("sync-mcp-lanes", "--json", "--descriptor", str(self.main))
            missing = self.run_cli("sync-mcp-lanes", "--descriptor", str(self.ws / "missing.json"))
        self.assertEqual((text[0], js[0], missing[0]), (0, 0, 1))
        self.assertIn("unchanged\tdev-s-space", text[1])
        self.assertIn("skipped\tnomcp-fixture-net\tno_endpoints", text[1])
        self.assertEqual(sorted(json.loads(js[1])["unchanged"]), ["dev-s-space", "legacy"])
        self.assertIn("no_main_descriptor", missing[2])
        blobs = [json.dumps(result), *text[1:], *js[1:], *missing[1:], "\n".join(logs.output)]
        blobs += [p.read_text() for p in self.lanes.glob("conn-*.json")]
        for secret in ("secret-legacy", "secret-s", "secret-dev", "secret-nomcp"):
            for blob in blobs:
                self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main(verbosity=1)
