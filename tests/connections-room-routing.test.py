#!/usr/bin/env python3
"""Contract tests for the room -> connection resolver in src/connections.py.

An operation on a room acts through the connection whose server is exactly the room
ID's domain; an operation with no room acts through the primary. A domain no
connection serves, or a disconnected one, is refused by name and never answered
with another server's token. Fewer than two rows keeps the caller's own resolution.
The record, the environment and the vault are fixtures.

Run: python3 tests/connections-room-routing.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import connections as C

LEGACY = "https://chat.ag2.space/relay"
S_SPACE = "https://chat.s.space/relay"
DEV_S_SPACE = "https://chat.dev.s.space/relay"
EXAMPLE = "https://gw.example.org/relay"


def row(server, instance, gateway_url, *, primary=False, **extra):
    r = {"server": server, "instance": instance, "gateway_url": gateway_url,
         "token_ref": C.token_ref_for(instance), "primary": primary, "added_by": "env",
         "added_at": "2026-10-01T00:00:00Z", "verified": True, "server_source": "identity"}
    r.update(extra)
    return r


THREE = [row("ag2.space", "", LEGACY, parked=True),
         row("s.space", "s-space", S_SPACE, primary=True),
         row("dev.s.space", "dev-s-space", DEV_S_SPACE)]


class FakeVault:
    def __init__(self, store=None):
        self.store = dict(store or {})
        self.asked = []

    def get(self, key):
        self.asked.append(key)
        return self.store.get(key, "")


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def record(self, rows):
        path = C.record_path(self.ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": C.SCHEMA_VERSION, "connections": rows}))

    def instance(self, room):
        r = C.connection_for_room(room, self.ws)
        return None if r is None else r["instance"]


class TestSelection(Base):
    def test_each_room_resolves_to_the_row_of_its_exact_domain(self):
        self.record(THREE)
        self.assertEqual(self.instance("!a:s.space"), "s-space")
        self.assertEqual(self.instance("!a:dev.s.space"), "dev-s-space")
        self.assertEqual(self.instance("!a:ag2.space"), "")

    def test_a_subdomain_never_matches_its_parent(self):
        self.record(THREE)
        with self.assertRaises(C.NoConnectionForRoom) as cm:
            C.connection_for_room("!a:x.s.space", self.ws)
        self.assertIn("x.s.space", str(cm.exception))

    def test_an_arbitrary_third_domain_routes_by_its_row(self):
        self.record(THREE + [row("example.org", "example-org", EXAMPLE)])
        self.assertEqual(self.instance("!a:example.org"), "example-org")

    def test_domain_is_case_insensitive(self):
        self.record(THREE)
        self.assertEqual(self.instance("!a:DEV.S.Space"), "dev-s-space")

    def test_agent_mxid_server_matches_too(self):
        self.record([row("gw-host.example.org", "custom", EXAMPLE, primary=True,
                         agent_mxid="@bot:example.org"), THREE[2]])
        self.assertEqual(self.instance("!a:example.org"), "custom")

    def test_no_room_is_the_primary(self):
        self.record(THREE)
        self.assertEqual(self.instance(None), "s-space")
        self.assertEqual(self.instance(""), "s-space")

    def test_a_room_id_naming_no_domain_is_the_primary(self):
        self.record(THREE)
        self.assertEqual(self.instance("!opaqueid"), "s-space")

    def test_a_domainless_room_follows_the_lane_with_a_task_open_in_it(self):
        self.record(THREE)
        state = C.record_path(self.ws).parent
        (state / "remote-task-rooms.dev-s-space.json").write_text(json.dumps({"task-dev-s-space~1": "!opaqueid"}))
        (state / "remote-task-rooms.json").write_text("{broken")
        self.assertEqual(self.instance("!opaqueid"), "dev-s-space")
        self.assertEqual(self.instance("!otherid"), "s-space")

    def test_a_domainless_room_open_on_two_lanes_is_the_primary(self):
        self.record(THREE)
        state = C.record_path(self.ws).parent
        for name in ("remote-task-rooms.json", "remote-task-rooms.dev-s-space.json"):
            (state / name).write_text(json.dumps({"t": "!opaqueid"}))
        self.assertEqual(self.instance("!opaqueid"), "s-space")

    def test_unknown_server_is_refused_by_name(self):
        self.record(THREE)
        with self.assertRaises(C.NoConnectionForRoom) as cm:
            C.connection_for_room("!a:other.net", self.ws)
        msg = str(cm.exception)
        self.assertIn("other.net", msg)
        self.assertIn("dev.s.space", msg)

    def test_disconnected_row_is_refused(self):
        rows = [dict(r) for r in THREE]
        rows[2]["disabled"] = True
        self.record(rows)
        with self.assertRaises(C.NoConnectionForRoom) as cm:
            C.connection_for_room("!a:dev.s.space", self.ws)
        self.assertIn("disconnected", str(cm.exception))

    def test_single_connection_and_no_record_keep_the_callers_resolution(self):
        self.assertIsNone(C.connection_for_room("!a:anywhere.net", self.ws))
        self.record([THREE[1]])
        self.assertIsNone(C.connection_for_room("!a:anywhere.net", self.ws))

    def test_no_primary_and_no_room_keeps_the_callers_resolution(self):
        self.record([dict(r, primary=False) for r in THREE])
        self.assertIsNone(C.connection_for_room(None, self.ws))

    def test_unreadable_record_keeps_the_callers_resolution(self):
        path = C.record_path(self.ws)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        self.assertIsNone(C.connection_for_room("!a:s.space", self.ws))


class TestCredential(Base):
    def cred(self, room, environ=None, vault=None):
        return C.room_credential(room, workspace=self.ws, environ=environ or {}, vault=vault or FakeVault())

    def test_named_row_uses_its_own_env_var_never_the_legacy_token(self):
        self.record(THREE)
        env = {"REMOTE_TASK_TOKEN": LEGACY + "|legacy-secret",
               "AG2_REMOTE_TOKEN_S_SPACE": S_SPACE + "|s-secret",
               "AG2_REMOTE_TOKEN_DEV_S_SPACE": DEV_S_SPACE + "|dev-secret"}
        self.assertEqual(self.cred("!a:s.space", env), (S_SPACE, S_SPACE + "|s-secret"))
        self.assertEqual(self.cred("!a:dev.s.space", env), (DEV_S_SPACE, DEV_S_SPACE + "|dev-secret"))
        self.assertEqual(self.cred("!a:ag2.space", env), (LEGACY, LEGACY + "|legacy-secret"))

    def test_no_room_uses_the_primary_token(self):
        self.record(THREE)
        env = {"REMOTE_TASK_TOKEN": LEGACY + "|legacy-secret", "AG2_REMOTE_TOKEN_S_SPACE": "bare-s"}
        self.assertEqual(self.cred(None, env), (S_SPACE, "bare-s"))

    def test_a_token_for_another_gateway_is_skipped(self):
        self.record(THREE)
        env = {"REMOTE_TASK_TOKEN": S_SPACE + "|wrong-lane"}
        vault = FakeVault({"REMOTE_TASK_TOKEN": LEGACY + "|from-vault"})
        self.assertEqual(self.cred("!a:ag2.space", env, vault), (LEGACY, LEGACY + "|from-vault"))

    def test_vault_holds_the_named_token(self):
        self.record(THREE)
        vault = FakeVault({"AG2_REMOTE_TOKEN_DEV_S_SPACE": DEV_S_SPACE + "|v"})
        self.assertEqual(self.cred("!a:dev.s.space", {}, vault), (DEV_S_SPACE, DEV_S_SPACE + "|v"))
        self.assertEqual(vault.asked, ["AG2_REMOTE_TOKEN_DEV_S_SPACE"])

    def test_launch_key_alias_counts(self):
        rows = [dict(r) for r in THREE]
        rows[2]["launch_keys"] = ["AG2_REMOTE_TOKEN_DEV"]
        self.record(rows)
        self.assertEqual(self.cred("!a:dev.s.space", {"AG2_REMOTE_TOKEN_DEV": "alias"}), (DEV_S_SPACE, "alias"))

    def test_no_token_is_refused_by_connection_name(self):
        self.record(THREE)
        with self.assertRaises(C.NoConnectionForRoom) as cm:
            self.cred("!a:dev.s.space", {"AG2_REMOTE_TOKEN_S_SPACE": "s"})
        self.assertIn("dev-s-space", str(cm.exception))
        self.assertIn("AG2_REMOTE_TOKEN_DEV_S_SPACE", str(cm.exception))

    def test_unavailable_vault_is_a_miss(self):
        self.record(THREE)

        class Broken:
            def get(self, key):
                raise RuntimeError("locked")
        with self.assertRaises(C.NoConnectionForRoom):
            self.cred("!a:s.space", {}, Broken())

    def channels(self):
        cfg = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, cfg, ignore_errors=True)
        p = unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(cfg)})
        p.start()
        self.addCleanup(p.stop)
        return cfg / "channels"

    def test_channel_files_serve_their_own_rows(self):
        self.record(THREE)
        channels = self.channels()
        (channels / "dev-s-space-ag2space").mkdir(parents=True)
        (channels / "dev-s-space-ag2space" / ".env").write_text(f'REMOTE_TASK_TOKEN="{DEV_S_SPACE}|dev-file"\n')
        (channels / "ag2space").mkdir()
        (channels / "ag2space" / ".env").write_text(f'REMOTE_TASK_TOKEN="{LEGACY}|legacy-file"\n'
                                                     f'AG2_REMOTE_TOKEN_S_SPACE="{S_SPACE}|s-launch-key"\n')
        self.assertEqual(self.cred("!a:dev.s.space"), (DEV_S_SPACE, DEV_S_SPACE + "|dev-file"))
        self.assertEqual(self.cred("!a:ag2.space"), (LEGACY, LEGACY + "|legacy-file"))
        self.assertEqual(self.cred("!a:s.space"), (S_SPACE, S_SPACE + "|s-launch-key"))

    def test_an_unimportable_vault_is_a_miss(self):
        self.record(THREE)
        with unittest.mock.patch.dict(sys.modules, {"vault_intercept": None}):
            with self.assertRaises(C.NoConnectionForRoom):
                C.room_credential("!a:s.space", workspace=self.ws, environ={})

    def test_single_connection_returns_none(self):
        self.record([THREE[1]])
        self.assertIsNone(self.cred("!a:s.space", {"AG2_REMOTE_TOKEN_S_SPACE": "s"}))


class TestServerOfRoom(unittest.TestCase):
    def test_grammar(self):
        self.assertEqual(C.server_of_room("!a:Example.ORG"), "example.org")
        self.assertEqual(C.server_of_room("!a:host.example.org:8448"), "host.example.org:8448")
        self.assertEqual(C.server_of_room("!a"), "")
        self.assertEqual(C.server_of_room("!a:"), "")
        self.assertEqual(C.server_of_room(""), "")


if __name__ == "__main__":
    unittest.main()
