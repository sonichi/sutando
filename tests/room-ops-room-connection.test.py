#!/usr/bin/env python3
"""room_ops and notify pick their gateway through `connections.room_credential`.

An install connected to several homeservers must act in a room through the
connection serving that room's server, whatever token the process environment or
the AG2 Space channel file holds. These cases pin the delegation: the room ID
reaches the resolver, its answer outranks the environment, its refusal fails the
command, and a single-connection install (resolver answers None) keeps today's
chain. The resolver's own contract is tests/connections-room-routing.test.py.

Run: python3 tests/room-ops-room-connection.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

_FAKE_VI = types.ModuleType("vault_intercept")
_FAKE_VI.get_vault_key = lambda var: (_ for _ in ()).throw(KeyError(var))
sys.modules["vault_intercept"] = _FAKE_VI

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "agent-room-ops"))
sys.path.insert(0, str(ROOT / "src"))
import _gateway
import connections
import media as md
import say as sy
import room_ops

_spec = importlib.util.spec_from_file_location("notify_under_test", ROOT / "skills" / "task-progress" / "scripts" / "notify.py")
notify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify)

DEV_ROOM = "!r:dev.s.space"
DEV_GW = "https://chat.dev.s.space/relay"
ENVK = ("GATEWAY_URL", "GATEWAY_TOKEN", "RELAY_URL", "RELAY_TOKEN", "REMOTE_TASK_URL", "REMOTE_TASK_TOKEN",
        "ROOM_MEDIA_ALLOW", "ROOM_OPS_GATE")


class Routed:
    """A stand-in for connections.room_credential that records the rooms it was asked about."""

    def __init__(self, answer=None, refuse=None):
        self.answer, self.refuse, self.rooms = answer, refuse, []

    def __call__(self, room_id, **_kw):
        self.rooms.append(room_id)
        if self.refuse:
            raise connections.NoConnectionForRoom(self.refuse)
        return self.answer


class Base(unittest.TestCase):
    def setUp(self):
        saved = {k: os.environ.get(k) for k in ENVK}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        for k in ENVK:
            os.environ.pop(k, None)
        os.environ["REMOTE_TASK_TOKEN"] = "https://chat.ag2.space/relay|legacy-secret"
        p = mock.patch.object(_gateway, "_channel_env_file", lambda: None)
        p.start()
        self.addCleanup(p.stop)

    def route(self, routed):
        p = mock.patch.object(connections, "room_credential", routed)
        p.start()
        self.addCleanup(p.stop)
        return routed


class TestGatewayDelegation(Base):
    def test_room_credential_outranks_the_environment(self):
        r = self.route(Routed((DEV_GW, DEV_GW + "|dev-secret")))
        base, headers = _gateway.gateway(DEV_ROOM)
        self.assertEqual(r.rooms, [DEV_ROOM])
        self.assertEqual(base, DEV_GW)
        self.assertEqual(headers["Authorization"], "Bearer dev-secret")

    def test_no_room_asks_for_the_primary(self):
        r = self.route(Routed((DEV_GW, "bare")))
        base, headers = _gateway.gateway()
        self.assertEqual(r.rooms, [None])
        self.assertEqual((base, headers["Authorization"]), (DEV_GW, "Bearer bare"))

    def test_single_connection_keeps_todays_chain(self):
        self.route(Routed(None))
        base, headers = _gateway.gateway(DEV_ROOM)
        self.assertEqual((base, headers["Authorization"]), ("https://chat.ag2.space/relay", "Bearer legacy-secret"))

    def test_refusal_is_raised_never_answered_with_another_token(self):
        self.route(Routed(refuse="no connection for server dev.s.space"))
        with self.assertRaises(_gateway.RoomConnectionError) as cm:
            _gateway.gateway(DEV_ROOM)
        self.assertIn("dev.s.space", str(cm.exception))


class TestWithoutCore(Base):
    """A skill copy with no core `src/` keeps today's chain rather than failing."""

    def test_gateway_without_core_src(self):
        with mock.patch.object(_gateway, "_core_src_on_path", lambda: False):
            self.assertEqual(_gateway.gateway(DEV_ROOM)[0], "https://chat.ag2.space/relay")

    def test_gateway_and_notify_with_an_unimportable_resolver(self):
        with mock.patch.dict(sys.modules, {"connections": None}):
            self.assertEqual(_gateway.gateway(DEV_ROOM)[0], "https://chat.ag2.space/relay")
            self.assertEqual(notify._gateway_config("ag2space", DEV_ROOM),
                             ("https://chat.ag2.space/relay", "legacy-secret"))


class TestRoomOpsCli(Base):
    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = room_ops._main(argv)
        return rc, json.loads(out.getvalue())

    def test_send_uploads_through_the_rooms_connection(self):
        r = self.route(Routed((DEV_GW, DEV_GW + "|dev-secret")))
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.environ["ROOM_MEDIA_ALLOW"] = tmp
        f = os.path.join(tmp, "a.png")
        pathlib.Path(f).write_bytes(b"IMG")
        sent = {}

        def fake(method, url, headers=None, data=None):
            sent.update(url=url, auth=headers.get("Authorization"))
            return 200, b"{}", {}
        with mock.patch.object(md, "http_request", side_effect=fake):
            rc, res = self.run_cli(["send", DEV_ROOM, f])
        self.assertEqual(rc, 0, res)
        self.assertEqual(r.rooms, [DEV_ROOM])
        self.assertTrue(sent["url"].startswith(DEV_GW + "/v1/rooms/"), sent)
        self.assertEqual(sent["auth"], "Bearer dev-secret")

    def test_say_posts_through_the_rooms_connection(self):
        r = self.route(Routed((DEV_GW, "dev-secret")))
        seen = {}

        def fake_http_json(method, url, headers, payload):
            seen.update(url=url, auth=headers.get("Authorization"))
            return 200, {"event_id": "$e"}
        with mock.patch.object(sy, "http_json", side_effect=fake_http_json), \
                mock.patch.object(sy, "load_gate", lambda: None):
            rc, res = self.run_cli(["say", DEV_ROOM, "hello"])
        self.assertEqual(rc, 0, res)
        self.assertEqual(r.rooms, [DEV_ROOM])
        self.assertTrue(seen["url"].startswith(DEV_GW), seen)
        self.assertEqual(seen["auth"], "Bearer dev-secret")

    def test_refusal_fails_the_command_by_name(self):
        self.route(Routed(refuse="no connection for server dev.s.space (connected: a, b)"))
        with mock.patch.object(sy, "load_gate", lambda: None):
            rc, res = self.run_cli(["say", DEV_ROOM, "hello"])
        self.assertEqual(rc, 1)
        self.assertFalse(res["ok"])
        self.assertIn("dev.s.space", res["reason"])


class TestNotifyDelegation(Base):
    def test_routed_room_outranks_the_source_chain(self):
        r = self.route(Routed((DEV_GW + "/", DEV_GW + "|dev-secret")))
        self.assertEqual(notify._gateway_config("ag2space", DEV_ROOM), (DEV_GW, "dev-secret"))
        self.assertEqual(r.rooms, [DEV_ROOM])

    def test_bare_routed_token_is_kept(self):
        self.route(Routed((DEV_GW, "bare")))
        self.assertEqual(notify._gateway_config("ag2space", DEV_ROOM), (DEV_GW, "bare"))

    def test_refusal_sends_nothing(self):
        self.route(Routed(refuse="no connection for server dev.s.space"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(notify._gateway_config("ag2space", DEV_ROOM))
        self.assertIn("dev.s.space", err.getvalue())

    def test_single_connection_keeps_the_source_chain(self):
        self.route(Routed(None))
        self.assertEqual(notify._gateway_config("ag2space", DEV_ROOM),
                         ("https://chat.ag2.space/relay", "legacy-secret"))


if __name__ == "__main__":
    unittest.main()
