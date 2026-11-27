"""codex-local's AG2 Space room tools: the owner's and an attested collaborator's threads get
`mcp_servers.ag2-space`, the desktop's stdio proxy under the name shim acting as the agent (mcp_stdio, shared
with the Antigravity connector); a restricted turn (plain team, a guest) never does.

No network, no real `codex` and no real token exchange: the connector runs against `stub_app_server.py`, whose
`[[config]]` echo shows the config a thread was started or resumed with, and the exchange child is a stand-in
that prints a descriptor. Run:
    python3 packages/ag2-sparrow/tests/test_codex_room_tools.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before the gateway-reaching imports below: no outer credential selector survives.
isolate_os_environ()

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ag2_sparrow import mcp_auth, mcp_stdio, task_io  # noqa: E402
from ag2_sparrow.adapters.codex import codex_connector as cc  # noqa: E402

from test_codex_connector import FakeRpc, gateway_mid_task, result, run_connector, write_task, write_text  # noqa: E402

SECRET = "relay-secret-do-not-leak"
AGENT = "@cx:s.space"
PROXY = shlex.join(["/app/node", "/app/ag2-mcp-proxy.mjs"])
REAL_ENTRY = mcp_stdio.stdio_entry
REAL_RUN_TURN = cc.Connector.run_turn


def config_of(text):
    return json.loads(text.split(" config=", 1)[1])


class World:
    """A work root, the agent's own token file beside it, and a token exchange that prints a descriptor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name) / "work"
        self.token_file = str(Path(self.tmp.name) / "agent.env")
        with open(self.token_file, "w") as f:
            f.write("REMOTE_TASK_TOKEN=https://relay.s.space|%s\nAGENT_MXID=%s\n" % (SECRET, AGENT))
        os.chmod(self.token_file, 0o600)
        self.desc = {"version": 1, "lane": "primary", "env_file": self.token_file, "env_key": "REMOTE_TASK_TOKEN",
                     "mint_url": "https://relay.s.space/api/v1/mcp/agent-access-tokens",
                     "mcp_url": "https://mcp.s.space/mcp", "relay": "https://relay.s.space"}
        self.runs, self.prompts = [], []

    def run_turn(self, conn, prompt, *a, **kw):
        self.prompts.append(prompt)
        return REAL_RUN_TURN(conn, prompt, *a, **kw)

    def exchange(self, argv, **kw):
        self.runs.append((argv, kw["env"]))
        return mock.Mock(returncode=0, stdout=json.dumps(self.desc), stderr="")

    def serve(self, once, *extra, proxy=PROXY, token=True):
        flags = (["--token-env-file", self.token_file, "--agent-id", AGENT] if token else []) + \
            (["--mcp-proxy-command", proxy] if proxy else []) + list(extra)
        entry = lambda *a, **kw: REAL_ENTRY(*a, run=self.exchange, **kw)  # noqa: E731
        spy = lambda conn, prompt, *a, **kw: self.run_turn(conn, prompt, *a, **kw)  # noqa: E731
        with mock.patch.object(mcp_stdio, "stdio_entry", entry), mock.patch.object(cc.Connector, "run_turn", spy), \
                mock.patch("ag2_sparrow.connect_bridge.credential_env", lambda tf, aid: ({"AG2_DEVICE_ENV": tf}, "")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(run_connector(self.work, once, flags), 0)
        return err.getvalue()

    def state(self):
        return os.path.abspath(str(self.work / "state"))

    def expected_server(self):
        state = self.state()
        return {"command": os.path.join(state, mcp_stdio.SHIM_NAME), "args": ["/app/node", "/app/ag2-mcp-proxy.mjs"],
                "env": {"AG2_MCP_DESCRIPTOR": os.path.join(state, mcp_stdio.DESCRIPTOR_NAME),
                        "AG2_MCP_LOG": os.path.join(state, mcp_stdio.PROXY_LOG_NAME)},
                "startup_timeout_sec": cc.ROOM_TOOLS_STARTUP_S}

    def assert_no_secret(self, *texts):
        for text in texts:
            self.assertNotIn(SECRET, text)
            self.assertNotIn("REMOTE_TASK_TOKEN=", text)


class RoomToolsTest(World, unittest.TestCase):
    def test_an_owner_thread_gets_the_agents_own_ag2_space_server_with_the_granted_tools(self):
        write_task(self.work, "task-chat-o", "[[config]] owner")
        err = self.serve(1)
        text = result(self.work, "task-chat-o")
        cfg = config_of(text)
        self.assertEqual(cfg, {"mcp_servers": {"ag2-space": self.expected_server()}})
        launcher = Path(self.state()) / mcp_stdio.SHIM_NAME
        self.assertEqual(os.stat(launcher).st_mode & 0o777, 0o700)
        body = launcher.read_text()
        self.assertIn("mcp_name_shim", body)
        self.assertIn("AG2_MCP_SHIM_TOOLS=%s " % shlex.quote(",".join(sorted(mcp_auth.OWNER_TOOLS))), body)
        desc_path = Path(self.state()) / mcp_stdio.DESCRIPTOR_NAME
        self.assertEqual(os.stat(desc_path).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(desc_path.read_text()), dict(self.desc, agent_id=AGENT))
        self.assert_no_secret(text, body, desc_path.read_text(), json.dumps(cfg), err)
        [(argv, env)] = self.runs
        self.assertIn("main(['descriptor', '--env-file', %r])" % self.token_file, argv[-1])
        self.assertEqual(set(env) - set(mcp_stdio.MINT_ENV_KEEP), {"AG2_DEVICE_ENV"},
                         "the exchange gets the token file's path, never the token")
        self.assertIn("ag2-space MCP tools offered on this thread", err)
        self.assertIn(mcp_stdio.ROOM_TOOLS_HINT, self.prompts[0], "the turn is told what the tools are for")

    def test_a_collaborator_thread_gets_the_same_grant_and_team_and_guest_get_none(self):
        tid = "task-1792100000009"
        now = time.time()
        write_text(self.work, tid, gateway_mid_task(tid, "[[config]] collab", tier="team", collaborator=True),
                   mtime=now - 30)
        write_task(self.work, "task-chat-t", "[[config]] plain team", tier="team", channel="!t:s.space", mtime=now - 20)
        write_task(self.work, "task-chat-g", "[[config]] guest", tier="guest", channel="!g:s.space", mtime=now - 10)
        self.serve(3)
        self.assertEqual(config_of(result(self.work, tid)), {"mcp_servers": {"ag2-space": self.expected_server()}})
        for t in ("task-chat-t", "task-chat-g"):
            cfg = config_of(result(self.work, t))
            self.assertEqual(cfg["mcp_servers"], {"owner-mcp": {"enabled": False}}, t)
            self.assertNotIn("ag2-space", json.dumps(cfg), t)
        self.assertEqual(len(self.runs), 1, "no exchange for a restricted turn")

    def test_a_restricted_turn_after_an_owner_turn_in_the_same_room_runs_on_its_own_thread_without_it(self):
        now = time.time()
        write_task(self.work, "task-chat-o", "[[config]] owner", channel="!r:s.space", mtime=now - 20)
        write_task(self.work, "task-chat-g", "[[config]] guest", tier="guest", channel="!r:s.space", mtime=now - 10)
        self.serve(2)
        self.assertIn("ag2-space", config_of(result(self.work, "task-chat-o"))["mcp_servers"])
        self.assertNotIn("ag2-space", config_of(result(self.work, "task-chat-g"))["mcp_servers"])
        self.assertIn("ephemeral=True", result(self.work, "task-chat-g"))
        self.assertNotIn(mcp_stdio.ROOM_TOOLS_HINT, self.prompts[1], "no tools hint either")

    def test_a_resumed_thread_is_resumed_with_the_server(self):
        write_task(self.work, "task-chat-1", "[[config]] one")
        self.serve(1)
        thread = (self.work / "state" / "threads" / cc.channel_key("local-chat")).read_text().split()[0]
        write_task(self.work, "task-chat-2", "[[config]] two")
        self.serve(1)
        self.assertEqual(config_of(result(self.work, "task-chat-2")),
                         {"mcp_servers": {"ag2-space": self.expected_server()}})
        saved = self.work / "state" / "threads" / cc.channel_key("local-chat")
        self.assertEqual(saved.read_text().split()[0], thread)
        self.assertEqual(len(self.runs), 2, "one exchange per thread opened on a server, none per turn on a live one")

    def test_without_the_proxy_command_or_the_token_file_an_owner_thread_gets_nothing(self):
        write_task(self.work, "task-chat-1", "[[config]] no proxy")
        self.serve(1, proxy=None)
        self.assertTrue(result(self.work, "task-chat-1").rstrip().endswith(" config=null"))
        write_task(self.work, "task-chat-2", "[[config]] no token", channel="!b:s.space")
        err = self.serve(1, token=False)
        self.assertTrue(result(self.work, "task-chat-2").rstrip().endswith(" config=null"))
        self.assertIn("no --token-env-file", err)
        self.assertEqual(self.runs, [])

    def test_a_failed_exchange_runs_the_turn_without_the_tools(self):
        self.desc = dict(self.desc, env_file="/other/agent.env")    # not the agent's own token file
        write_task(self.work, "task-chat-1", "[[config]] owner")
        err = self.serve(1)
        self.assertTrue(result(self.work, "task-chat-1").rstrip().endswith(" config=null"))
        self.assertIn("ag2-space MCP tools not offered: the token exchange printed no proxy descriptor", err)


class RoomToolsUnitTest(World, unittest.TestCase):
    """room_tools and start_thread against a scripted server."""

    def connector(self, answers, *flags):
        calls = []

        class Rpc(FakeRpc):
            def request(self, method, params=None, timeout=120):
                calls.append((method, params))
                got = answers.get(method)
                got = got(params) if callable(got) else got
                if isinstance(got, Exception):
                    raise got
                return got
        args = cc.build_parser().parse_args(["--work", str(self.work), "--token-env-file", self.token_file,
                                             "--agent-id", AGENT, "--mcp-proxy-command", PROXY, *flags])
        args.state = str(self.work / "state")
        os.makedirs(args.state, exist_ok=True)
        c = cc.Connector(FakeRpc, args, note="n",
                         proxy_entry=lambda *a, **kw: (self.entry(*a, **kw)))
        c.rpc = Rpc()
        return c, calls

    def entry(self, *a, **kw):
        self.runs.append((a, kw))
        return {"name": "ag2-space", "command": "/s/shim", "args": ["/app/node"],
                "env": [{"name": "AG2_MCP_DESCRIPTOR", "value": "/s/d.json"}]}, ""

    def test_an_existing_ag2_space_entry_or_an_unreadable_config_offers_nothing(self):
        owner = task_io.OWNER_CAPABILITIES
        for answer in ({"config": {"mcp_servers": {"ag2-space": {"url": "https://mcp.s.space/mcp"}}}},
                       RuntimeError("config/read: refused"), {"config": {"mcp_servers": None}}, None):
            c, _ = self.connector({"config/read": answer})
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(c.room_tools(owner, "/w"), {}, answer)
            self.assertIn("ag2-space MCP tools not offered", err.getvalue())
        self.assertEqual(self.runs, [], "no exchange when the server would merge into another entry")
        c, _ = self.connector({"config/read": RuntimeError("config/read: server pipe closed (x)")})
        with self.assertRaises(RuntimeError):
            c.room_tools(owner, "/w")
        c, _ = self.connector({"config/read": {"config": {"mcp_servers": {}}}})
        restricted = task_io.turn_policy({"access_tier": "guest"}, True).capabilities
        self.assertEqual(c.room_tools(restricted, "/w"), {})
        with contextlib.redirect_stderr(io.StringIO()):
            got = c.room_tools(owner, "/w")
        self.assertEqual(got, {"mcp_servers": {"ag2-space": {"command": "/s/shim", "args": ["/app/node"],
                                                             "env": {"AG2_MCP_DESCRIPTOR": "/s/d.json"},
                                                             "startup_timeout_sec": cc.ROOM_TOOLS_STARTUP_S}}})
        [(_a, kw)] = self.runs
        self.assertEqual(kw["tools"], mcp_auth.OWNER_TOOLS)

    def test_a_thread_start_refused_with_the_tools_starts_without_them(self):
        def start(params):
            if "config" in params:
                return RuntimeError("thread/start: invalid mcp_servers.ag2-space")
            return {"thread": {"id": "th-plain"}}
        c, calls = self.connector({"config/read": {"config": {"mcp_servers": {}}}, "thread/start": start})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            c.start_thread(task_io.OWNER_CAPABILITIES, False)
        self.assertEqual(c.thread_id, "th-plain")
        self.assertEqual(["config" in p for m, p in calls if m == "thread/start"], [True, False])
        self.assertIn("starting it without them", err.getvalue())
        c, _ = self.connector({"config/read": {"config": {"mcp_servers": {}}},
                               "thread/start": RuntimeError("thread/start: server pipe closed")})
        with self.assertRaises(RuntimeError), contextlib.redirect_stderr(io.StringIO()):
            c.start_thread(task_io.OWNER_CAPABILITIES, False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
