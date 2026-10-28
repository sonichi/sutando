#!/usr/bin/env python3
"""Every gateway client outside room_ops acts through `connections.room_credential`.

One install, three connections (tests/_helpers/three_connections.py): ag2.space (the legacy row,
whose token is also the process's REMOTE_TASK_TOKEN), s.space (the primary) and dev.s.space. Each
client is driven with the real resolver: a room on each server reaches that server's gateway
with that server's secret, no room reaches the primary, and a room no connection serves is
refused, never answered with another server's token. The resolver's own contract is
tests/connections-room-routing.test.py; room_ops' and notify's delegation is
tests/room-ops-room-connection.test.py.

Run: python3 tests/gateway-clients-room-routing.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import asyncio
import contextlib
import importlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "skills" / "agent-room-ops"),
                str(ROOT / "skills" / "schedule-crons" / "scripts"),
                str(ROOT / "skills" / "pending-questions" / "scripts")]

from _helpers.three_connections import (PRIMARY, ROOMS, SERVERS, UNSERVED_ROOM, ThreeConnections,  # noqa: E402
                                        gateway, origin, secret, token)

_FAKE_VI = types.ModuleType("vault_intercept")
_FAKE_VI.get_vault_key = lambda var: (_ for _ in ()).throw(KeyError(var))
sys.modules["vault_intercept"] = _FAKE_VI

import channel_credentials
import channel_env_resolve
import connections
import owner_room_access as access
import result_markers  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # a dataclass resolves its own module
    spec.loader.exec_module(mod)
    return mod


# The room capability's credential surface, as room_collab and room_commons read it.
FAKE_CAP = textwrap.dedent('''
    import os, re
    URL_VARS = ("AG2_ROOM_COLLAB_URL", "REMOTE_TASK_URL")
    TOKEN_VARS = ("ROOM_COLLAB_TOKEN", "REMOTE_TASK_TOKEN")

    def _split(value):
        head, sep, tail = value.partition("|")
        if sep and tail and re.match(r"^https?://", head):
            return re.match(r"^(https?://[^/]+)", head).group(1), tail
        return None, value

    def resolve_url(explicit):
        if explicit:
            return explicit
        for var in URL_VARS:
            if os.environ.get(var):
                return os.environ[var]
        for var in TOKEN_VARS:
            found = _split(os.environ.get(var, ""))[0]
            if found:
                return found
        raise RuntimeError("no service URL")

    def resolve_token(explicit):
        raw = explicit or next((os.environ[v] for v in TOKEN_VARS if os.environ.get(v)), None)
        if not raw:
            raise RuntimeError("no access token")
        return _split(raw)[1]
''')
FAKE_CLIENT = textwrap.dedent('''
    from contextlib import asynccontextmanager
    OPENED = []

    class Doc:
        database = {}

        async def settle(self, sec):
            return None

    @asynccontextmanager
    async def open_room_collab(url, room, token, kind="doc"):
        OPENED.append((url, room, token))
        yield Doc()
''')


class Base(unittest.TestCase):
    def setUp(self):
        self.three = ThreeConnections(self)
        saved = {k: os.environ.get(k) for k in ("AG2_ROOM_COLLAB_URL", "REMOTE_TASK_URL", "ROOM_COLLAB_TOKEN")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        self.scripts = self.three.root / "cap"
        self.scripts.mkdir()
        (self.scripts / "room_collab.py").write_text(FAKE_CAP)
        (self.scripts / "room_collab_client.py").write_text(FAKE_CLIENT)
        self.cap = _load("fake_room_collab", self.scripts / "room_collab.py")

    def no_network(self):
        return mock.patch("urllib.request.urlopen", side_effect=AssertionError("no request may be sent"))


class TestRoomEnv(Base):
    """channel_credentials.room_env, the shared answer for env-variable room clients."""

    def test_each_room_names_its_own_servers_token_and_blanks_the_rest(self):
        for server, room in ROOMS.items():
            got = channel_credentials.room_env(("U1", "U2"), ("T1", "T2"), room)
            self.assertEqual(got, {"U1": "", "U2": "", "T1": token(server), "T2": ""}, server)

    def test_no_room_is_the_primary(self):
        self.assertEqual(channel_credentials.room_env((), ("T",), None)["T"], token(PRIMARY))

    def test_a_bare_secret_gets_its_gateway(self):
        with mock.patch.dict(os.environ, {connections.token_ref_for("dev-s-space"): "bare"}):
            got = channel_credentials.room_env((), ("T",), ROOMS["dev.s.space"])
        self.assertEqual(got["T"], f"{gateway('dev.s.space')}|bare")

    def test_an_unserved_room_is_refused(self):
        with self.assertRaises(connections.NoConnectionForRoom):
            channel_credentials.room_env((), ("T",), UNSERVED_ROOM)

    def test_primary_row_is_none_for_an_unreadable_record(self):
        self.assertEqual(connections.primary_row(self.three.workspace)["server"], PRIMARY)
        connections.record_path(self.three.workspace).write_text("{not json")
        self.assertIsNone(connections.primary_row(self.three.workspace))

    def test_single_connection_and_no_token_vars_keep_the_callers_chain(self):
        self.assertIsNone(channel_credentials.room_env(("U",), (), ROOMS["s.space"]))
        connections.record_path(self.three.workspace).unlink()
        self.assertIsNone(channel_credentials.room_env(("U",), ("T",), ROOMS["s.space"]))


class TestOwnerRoomAccess(Base):
    """Caller 1: the pending-questions and Crons room databases."""

    def routing(self, name, owner_dm):
        path = self.three.workspace / "state" / name
        path.write_text(json.dumps({"owner_dm": owner_dm, "identity": "@a:x"}))
        return path

    def test_owner_routing_is_the_primary_rows_own_file(self):
        self.routing("owner-routing.json", ROOMS["ag2.space"])
        own = self.routing("owner-routing.s-space.json", ROOMS["s.space"])
        self.assertEqual(access.owner_routing_path(self.three.workspace), own)
        self.assertEqual(access.owner_dm(access.owner_routing(self.three.workspace)), ROOMS["s.space"])

    def test_owner_routing_falls_back_to_the_unsuffixed_file(self):
        legacy = self.routing("owner-routing.json", ROOMS["ag2.space"])
        self.assertEqual(access.owner_routing_path(self.three.workspace), legacy)
        connections.record_path(self.three.workspace).unlink()
        self.assertEqual(access.owner_routing_path(self.three.workspace), legacy)

    def test_a_legacy_primary_reads_the_unsuffixed_file(self):
        rec = json.loads(connections.record_path(self.three.workspace).read_text())
        for r in rec["connections"]:
            r["primary"] = r["instance"] == ""
        connections.record_path(self.three.workspace).write_text(json.dumps(rec))
        self.routing("owner-routing.s-space.json", ROOMS["s.space"])
        self.assertEqual(access.owner_routing_path(self.three.workspace).name, "owner-routing.json")

    def test_credentials_are_the_rooms_own_server(self):
        for server, room in ROOMS.items():
            self.assertEqual(access.resolve_credentials(self.cap, None, room), (origin(server), secret(server)))
        self.assertEqual(access.resolve_credentials(self.cap, None), (origin(PRIMARY), secret(PRIMARY)))

    def test_an_unserved_room_is_refused(self):
        with self.assertRaises(connections.NoConnectionForRoom):
            access.resolve_credentials(self.cap, None, UNSERVED_ROOM)

    def test_an_explicit_collab_url_keeps_the_capabilitys_own_order(self):
        url, tok = access.resolve_credentials(self.cap, "https://named.test.invalid", ROOMS["dev.s.space"])
        self.assertEqual((url, tok), ("https://named.test.invalid", secret("ag2.space")))

    def test_crons_table_opens_the_room_with_its_servers_credentials(self):
        ct = importlib.import_module("crons_table")
        opened = []

        @contextlib.asynccontextmanager
        async def open_room(url, room, tok, kind="doc"):
            opened.append((url, room, tok))
            yield types.SimpleNamespace(database={}, put_database=None, settle=None)

        plan = types.SimpleNamespace(writes=[])
        with mock.patch.object(ct, "load_capability", return_value=(self.cap, open_room, None)):
            asyncio.run(ct.run_plan(self.scripts, ROOMS["dev.s.space"], "@a:x", lambda *a: plan))
        self.assertEqual(opened, [(origin("dev.s.space"), ROOMS["dev.s.space"], secret("dev.s.space"))])

    def test_pending_questions_serve_opens_the_room_with_its_servers_credentials(self):
        pq = importlib.import_module("pending_questions_room_db")
        args = types.SimpleNamespace(skill_scripts=str(self.scripts), collab_url=None,
                                     room=ROOMS["dev.s.space"], user_id="@a:x")
        with mock.patch.object(pq, "apply", mock.AsyncMock(return_value=[])):
            asyncio.run(pq._serve(args, {"op": "rows", "schema": {"id": "pendingq"}}))
        import room_collab_client
        self.assertEqual(room_collab_client.OPENED[-1],
                         (origin("dev.s.space"), ROOMS["dev.s.space"], secret("dev.s.space")))


class TestCronRoom(Base):
    """Caller 2: ensure-cron-room.py and cron-notify.py."""

    def setUp(self):
        super().setUp()
        self.ensure = _load("ensure_cron_room_t", ROOT / "skills" / "schedule-crons" / "ensure-cron-room.py")
        self.notify = _load("cron_notify_t", ROOT / "skills" / "schedule-crons" / "cron-notify.py")

    def test_ensure_cron_room_creates_on_the_primary(self):
        self.assertEqual(self.ensure.resolve_token(str(ROOT)), (gateway(PRIMARY), secret(PRIMARY)))

    def test_ensure_cron_room_main_skips_when_the_primary_has_no_token(self):
        crons = self.three.root / "crons.json"
        crons.write_text(json.dumps([{"name": "c", "room": "auto"}]))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {connections.token_ref_for("s-space"): ""}), self.no_network(), \
                mock.patch.object(sys, "argv", ["x", "--crons-file", str(crons), "--repo", str(ROOT)]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(self.ensure.main(), 0)
        self.assertIn("s.space", out.getvalue())
        self.assertEqual(json.loads(crons.read_text())[0]["room"], "auto")

    def test_without_core_src_or_with_one_connection_the_env_chain_is_kept(self):
        empty = self.three.root / "no-core"
        empty.mkdir()
        self.assertEqual(self.ensure.resolve_token(str(empty), ROOMS["dev.s.space"]),
                         (gateway("ag2.space"), secret("ag2.space")))
        connections.record_path(self.three.workspace).unlink()
        self.assertEqual(self.ensure.resolve_token(str(ROOT), ROOMS["dev.s.space"]),
                         (gateway("ag2.space"), secret("ag2.space")))

    def test_cron_notify_posts_through_the_rooms_server(self):
        for server, room in ROOMS.items():
            self.assertEqual(self.notify._load_gateway(str(ROOT), room), (gateway(server), secret(server)))
        sent = []

        def urlopen(req, timeout=None):
            sent.append((req.full_url, req.get_header("Authorization")))
            return io.BytesIO(b'{"event_id": "$e"}')
        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(self.notify._post_to_room(ROOMS["dev.s.space"], "hi", str(ROOT)), "$e")
        self.assertEqual(sent, [(gateway("dev.s.space") + "/v1/room", "Bearer " + secret("dev.s.space"))])

    def test_cron_notify_refuses_an_unserved_room(self):
        err = io.StringIO()
        with self.no_network(), contextlib.redirect_stderr(err):
            self.assertIsNone(self.notify._post_to_room(UNSERVED_ROOM, "hi", str(ROOT)))
        self.assertIn("other.example.org", err.getvalue())


class TestBeeActions(Base):
    """Caller 3: bee_actions.py's room verbs."""

    def setUp(self):
        super().setUp()
        self.bee = _load("bee_actions_t", ROOT / "skills" / "bee-actions" / "scripts" / "bee_actions.py")

    def test_each_room_and_the_primary(self):
        for server, room in ROOMS.items():
            base, headers = self.bee._gateway_creds(room)
            self.assertEqual((base, headers["Authorization"]), (gateway(server), "Bearer " + secret(server)))
        self.assertEqual(self.bee._gateway_creds()[0], gateway(PRIMARY))

    def test_post_room_to_an_unserved_room_fails_without_sending(self):
        err = io.StringIO()
        with self.no_network(), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "argv", ["bee", "post-room", "hi", "--room", UNSERVED_ROOM]):
            self.assertEqual(self.bee.main(), 2)
        self.assertIn("other.example.org", err.getvalue())


class TestHealthCheck(Base):
    """Caller 4: health-check.py --notify-gateway."""

    def setUp(self):
        super().setUp()
        self.hc = _load("hc_routing_t", ROOT / "src" / "health-check.py")
        self.hc.WORKSPACE_DIR = self.three.workspace

    def test_creds_follow_the_room_and_default_to_the_primary(self):
        for server, room in ROOMS.items():
            self.assertEqual(self.hc._gateway_creds(room=room), (gateway(server), secret(server)))
        self.assertEqual(self.hc._gateway_creds(), (gateway(PRIMARY), secret(PRIMARY)))
        self.assertIsNone(self.hc._gateway_creds(room=UNSERVED_ROOM))

    def test_alert_room_is_the_primarys_and_is_posted_through_its_server(self):
        self.three.channel_file("ag2.space", f"REMOTE_ALERT_ROOM={ROOMS['ag2.space']}\n")
        self.three.channel_file(PRIMARY, f"REMOTE_ALERT_ROOM={ROOMS[PRIMARY]}\n")
        self.assertEqual(self.hc._gateway_owner_room(), ROOMS[PRIMARY])
        sent = []

        class Resp(io.BytesIO):
            status = 200

        def urlopen(req, timeout=None):
            sent.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)["room_id"]))
            return Resp(b"{}")
        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertTrue(self.hc._default_gateway_sender("alert"))
        self.assertEqual(sent, [(gateway(PRIMARY) + "/v1/room", "Bearer " + secret(PRIMARY), ROOMS[PRIMARY])])

    def test_alert_room_falls_back_to_the_legacy_channel(self):
        self.three.channel_file("ag2.space", f"REMOTE_ALERT_ROOM={ROOMS['ag2.space']}\n")
        self.assertEqual(self.hc._gateway_owner_room(), ROOMS["ag2.space"])
        self.assertEqual(self.hc._gateway_creds(room=ROOMS["ag2.space"]), (gateway("ag2.space"), secret("ag2.space")))


class TestGettingStarted(Base):
    """Caller 5: the tutorial's room_commons.py environment."""

    def setUp(self):
        super().setUp()
        self.gs = _load("getting_started_t", ROOT / "skills" / "tutorial" / "scripts" / "getting_started.py")

    def test_commons_env_carries_the_rooms_servers_token(self):
        for server, room in ROOMS.items():
            with mock.patch.dict(os.environ, {"REMOTE_TASK_URL": "https://x.invalid"}):
                env = self.gs.commons_env(self.scripts / "room_collab.py", room=room)
            self.assertEqual((env["ROOM_COLLAB_TOKEN"], env["REMOTE_TASK_TOKEN"], env["REMOTE_TASK_URL"]),
                             (token(server), "", ""), server)

    def test_publish_refuses_an_unserved_room(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.gs.main(["publish", "--room", UNSERVED_ROOM, "--name", "Blue", "--core", "codex",
                               "--room-commons", str(self.scripts / "room_collab.py")])
        self.assertEqual(rc, 4)
        self.assertIn("other.example.org", json.loads(out.getvalue())["reason"])

    def test_connect_actions_applies_the_blanked_variables(self):
        script = self.three.root / "rc" / "room_commons.py"
        script.parent.mkdir()
        script.write_text("import os\ndef mcp_door(args):\n"
                          "    return lambda: (os.environ.get('REMOTE_TASK_URL'), os.environ.get('ROOM_COLLAB_TOKEN'))\n")
        with mock.patch.dict(os.environ, {"REMOTE_TASK_URL": "https://other.invalid"}):
            got = self.gs.connect_actions(script, {"REMOTE_TASK_URL": "", "ROOM_COLLAB_TOKEN": token("dev.s.space")})
            self.assertEqual(os.environ["REMOTE_TASK_URL"], "https://other.invalid")
        self.assertEqual(got, ("", token("dev.s.space")))


class TestContextFirstPrompt(Base):
    """Caller 6: the bridge-injected CONTEXT-FIRST step names a channel file for the room's server."""

    def command(self, room, channel_dir="ag2space"):
        text = "\n".join(result_markers.render_skill_prelude(room, channel_dir, "task-1"))
        m = re.search(r'channel-env\.sh (.+?)\)"', text)
        return shlex.split(m.group(1))

    def test_the_prompt_passes_the_room(self):
        self.assertEqual(self.command(ROOMS["dev.s.space"]), ["ag2space", "--room", ROOMS["dev.s.space"]])

    def test_its_command_resolves_each_rooms_own_file(self):
        files = {s: self.three.channel_file(s, f"REMOTE_TASK_TOKEN={token(s)}\n") for s in SERVERS}
        for server, room in ROOMS.items():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = channel_env_resolve.main(["x", str(self.three.config / "channels"), *self.command(room)])
            self.assertEqual((rc, out.getvalue().strip()), (0, str(files[server])), server)

    def test_the_vendored_copy_renders_the_same_step(self):
        pkg = _load("result_markers_pkg_t", ROOT / "packages" / "ag2-sparrow" / "ag2_sparrow" / "result_markers.py")
        args = (ROOMS["s.space"], "s-space-ag2space", "task-1")
        self.assertEqual(pkg.render_skill_prelude(*args), result_markers.render_skill_prelude(*args))


class TestRoomOpsWithoutARoom(Base):
    """Caller 7: room_ops verbs that name no room act through the primary."""

    def test_rooms_and_agents_reach_the_primary(self):
        import resolve
        import rooms
        seen = []

        def http_json(method, url, headers, body=None):
            seen.append((url, headers["Authorization"]))
            return 200, {"ok": True, "rooms": []}

        def http_request(method, url, headers, body=None):
            seen.append((url, headers["Authorization"]))
            return 200, b'{"agents": []}', {}
        with mock.patch.object(rooms, "http_json", http_json), \
                mock.patch.object(resolve, "http_request", http_request):
            rooms.joined_rooms()
            resolve.list_agents()
        bearer = "Bearer " + secret(PRIMARY)
        self.assertEqual(seen, [(gateway(PRIMARY) + "/v1/room", bearer), (gateway(PRIMARY) + "/v1/agents", bearer)])


if __name__ == "__main__":
    unittest.main()
