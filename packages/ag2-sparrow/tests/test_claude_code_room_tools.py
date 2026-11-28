#!/usr/bin/env python3
"""claude-local's AG2 Space room tools: with MCP_PROXY_COMMAND (install --mcp-proxy-command) the session starts
with `--mcp-config <state/room-tools/ag2-mcp-config.json>`, one `ag2-space` stdio server that is the desktop's
proxy under the name shim acting as the agent (mcp_stdio, shared with codex-local and antigravity-local); the
isolated restricted turn never gets it.

No network, no real `claude` and no real token exchange: the exchange child is a stand-in that prints a
descriptor, and the session is the suite's fake claude. Run:
    python3 packages/ag2-sparrow/tests/test_claude_code_room_tools.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

# Before anything below reads the environment: no outer credential selector survives.
isolate_os_environ()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from claude_code_fixtures import (  # noqa: E402
    AgentRootBase, cli, cm, ConnectBase, hold_consumer, hold_mod, inbox, isolated_runs, SESSION_ID, unhold,
    write_isolated_stub,
)
from ag2_sparrow import mcp_auth, mcp_name_shim, mcp_stdio, task_io, task_lifecycle as lifecycle  # noqa: E402

SECRET = "relay-secret-do-not-leak"
AGENT = "@cl:s.space"
# Its first word must be an executable that exists (hold checks it at every start): this interpreter stands in.
PROXY = shlex.join([sys.executable, "/app/ag2-mcp-proxy.mjs"])
ALONE = {"ALLOW_COLLABORATOR": "0", "ALLOW_NON_OWNER": "0"}     # nobody but the owner is served
ALONE_FLAGS = ("--no-allow-collaborator", "--no-allow-non-owner")
DESC = {"version": 1, "lane": "primary", "env_key": "REMOTE_TASK_TOKEN",
        "mint_url": "https://relay.s.space/api/v1/mcp/agent-access-tokens", "mcp_url": "https://mcp.s.space/mcp",
        "relay": "https://relay.s.space"}
# The hold child's stand-ins: the agent's credential env and the exchange that prints the descriptor.
HOLD_PATCH = ("import json, types\nfrom ag2_sparrow import connect_bridge\n"
              "connect_bridge.credential_env = lambda tf, aid: ({'AG2_DEVICE_ENV': tf}, '')\n"
              "DESC = dict(%r, env_file=%r)\nRC = %d\n"
              "hold.exchange_run = lambda argv, **kw: types.SimpleNamespace(returncode=RC, stdout=json.dumps(DESC), "
              "stderr='' if RC == 0 else 'ag2-sparrow-mcp: discovery failed')\n")


def token_file(d):
    path = Path(d) / "agent.env"
    path.write_text("REMOTE_TASK_TOKEN=https://relay.s.space|%s\nAGENT_MXID=%s\n" % (SECRET, AGENT))
    path.chmod(0o600)
    return str(path)


class RoomToolsArgv(unittest.TestCase):
    """hold.room_tools_argv: what the session's argv gains, and every way it gains nothing."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name).resolve()
        self.root = self.d / "work"
        (self.root / "tasks").mkdir(parents=True)
        (self.root / "state").mkdir()
        self.config_dir = self.d / ".claude-agent"
        self.config_dir.mkdir()
        self.token = token_file(self.d)
        self.cfg = {"MCP_PROXY_COMMAND": PROXY, "TOKEN_ENV_FILE": self.token, "AGENT_ID": AGENT,
                    "CLAUDE_CONFIG_DIR": str(self.config_dir), "ROOT": str(self.root),
                    "SESSION_ID": SESSION_ID, "ROOM_TOOLS_SESSION": SESSION_ID, "ROOM_TOOLS_PREVIEW": "1", **ALONE}
        self.runs, self.lines = [], []
        self.desc = dict(DESC, env_file=self.token)
        self.rc = 0
        cred = mock.patch("ag2_sparrow.connect_bridge.credential_env", lambda tf, aid: ({"AG2_DEVICE_ENV": tf}, ""))
        cred.start()
        self.addCleanup(cred.stop)

    def exchange(self, argv, **kw):
        self.runs.append((argv, kw))
        if isinstance(self.rc, BaseException):
            raise self.rc
        return mock.Mock(returncode=self.rc, stdout=json.dumps(self.desc), stderr="" if self.rc == 0 else "boom")

    def argv(self, cfg=None):
        with mock.patch.object(hold_mod, "exchange_run", self.exchange):
            return hold_mod.room_tools_argv(cfg or self.cfg, str(self.root), str(self.root / "tasks"),
                                            log=self.lines.append)

    @property
    def state(self):
        return self.root / "state" / "room-tools"

    def test_the_session_gets_one_ag2_space_stdio_server_as_the_agent_with_no_secret(self):
        got = self.argv()
        self.assertEqual(got, ["--mcp-config", str(self.state / hold_mod.MCP_CONFIG_NAME)])
        path = Path(got[1])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        servers = json.loads(path.read_text())["mcpServers"]
        self.assertEqual(list(servers), [mcp_auth.SERVER_NAME], "one server, ours")
        server = servers[mcp_auth.SERVER_NAME]
        self.assertEqual((server["type"], server["command"], server["args"]),
                         ("stdio", str(self.state / mcp_stdio.SHIM_NAME), shlex.split(PROXY)))
        descriptor = self.state / mcp_stdio.DESCRIPTOR_NAME
        self.assertEqual(server["env"], {"AG2_MCP_DESCRIPTOR": str(descriptor),
                                         "AG2_MCP_LOG": str(self.state / mcp_stdio.PROXY_LOG_NAME),
                                         mcp_name_shim.GATE_ENV: json.dumps(hold_mod.gate_argv(str(self.root)))},
                         "the shim is told the command that gates every call")
        gate = hold_mod.gate_argv(str(self.root))
        self.assertEqual((gate[0], gate[1], gate[-4:]), (sys.executable, "-I", ["inbox", "--root", str(self.root), "room-tools"]))
        self.assertEqual(stat.S_IMODE(descriptor.stat().st_mode), 0o600)
        desc = json.loads(descriptor.read_text())
        self.assertEqual((desc["env_file"], desc["agent_id"]), (self.token, AGENT), "names the token file, never holds it")
        shim = (self.state / mcp_stdio.SHIM_NAME).read_text()
        self.assertIn("AG2_MCP_SHIM_TOOLS=%s" % ",".join(sorted(mcp_auth.OWNER_TOOLS)), shim, "the owner's grant")
        for text in (path.read_text(), descriptor.read_text(), shim, " ".join(got)):
            self.assertNotIn(SECRET, text)
            self.assertNotIn("REMOTE_TASK_TOKEN=", text)
        (argv, kw), = self.runs
        self.assertIn("'descriptor', '--env-file', %r" % self.token, argv[-1], "the descriptor exchange, as the agent")
        self.assertEqual(self.lines, ["ag2-space MCP tools offered (%d tools granted)" % len(mcp_auth.OWNER_TOOLS)])

    def test_each_start_rebuilds_it(self):
        """A restart (a sign-in change, a /exit) is a new hold: the descriptor and the config are written again."""
        first = Path(self.argv()[1]).read_text()
        shim = (self.state / mcp_stdio.SHIM_NAME).read_text()
        self.desc["mcp_url"] = "https://mcp2.s.space/mcp"
        self.assertEqual(self.argv()[0], "--mcp-config")
        self.assertEqual(json.loads((self.state / mcp_stdio.DESCRIPTOR_NAME).read_text())["mcp_url"],
                         "https://mcp2.s.space/mcp", "the descriptor of this start")
        self.assertEqual(Path(self.state / hold_mod.MCP_CONFIG_NAME).read_text(), first, "the same server entry")
        self.assertNotEqual((self.state / mcp_stdio.SHIM_NAME).read_text(), shim, "a fresh turn id for the shim")
        self.assertEqual(len(self.runs), 2)

    def test_only_the_conversation_started_for_the_tools_gets_them(self):
        other = "11111111-2222-4333-8444-555555555555"
        for cfg in (dict(self.cfg, ROOM_TOOLS_SESSION=""), dict(self.cfg, ROOM_TOOLS_SESSION=other),
                    {k: v for k, v in self.cfg.items() if k != "ROOM_TOOLS_SESSION"}):
            self.assertEqual(self.argv(cfg), [], cfg.get("ROOM_TOOLS_SESSION"))
            self.assertIn("this conversation was not started for them", self.lines[-1])
        self.assertEqual(self.runs, [], "an older, shared conversation never reaches the token exchange")
        with mock.patch.object(hold_mod, "exchange_run", self.exchange):
            started_as_another = hold_mod.room_tools_argv(self.cfg, str(self.root), str(self.root / "tasks"),
                                                          log=self.lines.append, session_id=other)
        self.assertEqual(started_as_another, [], "the pane's own session id is the one checked")
        self.assertTrue(hold_mod.tools_session(self.cfg) and hold_mod.tools_session(self.cfg, SESSION_ID))
        # The preview off on the tools' own conversation: no tools, and still the owner's alone.
        off = dict(self.cfg, ROOM_TOOLS_PREVIEW="")
        self.assertEqual(self.argv(off), [])
        self.assertEqual((hold_mod.tools_session(off), hold_mod.tools_conversation(off)), (False, True))

    def test_install_starts_a_new_conversation_for_the_tools_and_keeps_the_earlier_one_without_them(self):
        from ag2_sparrow.adapters.claude_code import cli as cc
        on = {"MCP_PROXY_COMMAND": PROXY, "TOKEN_ENV_FILE": self.token, "ROOM_TOOLS_PREVIEW": "1", **ALONE}
        old_id, new_id = SESSION_ID, "99999999-2222-4333-8444-555555555555"

        def decide(old, values, session_id, kept, resume="", live=None):
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                got = cc.room_tools_session(old, values, session_id, kept, resume, lambda: live, "stop it",
                                            mint=lambda: new_id)
            self.remembered = got.pop("OWNER_ONLY_SESSIONS")
            return got, said.getvalue()
        # No proxy, or no token file of the agent's own: no tools session.
        for values in ({}, {"MCP_PROXY_COMMAND": PROXY}, {"TOKEN_ENV_FILE": self.token}):
            self.assertEqual(decide({}, values, old_id, old_id)[0], {"ROOM_TOOLS_SESSION": "", "EARLIER_SESSION_ID": ""})
        # The proxy and the token file without the preview (what an app update passes): nothing changes for an
        # agent that has a conversation, and nothing is said. Its collaborators keep the session they share.
        quiet = {k: v for k, v in on.items() if k != "ROOM_TOOLS_PREVIEW"}
        for kept in (old_id, ""):
            self.assertEqual(decide({"SESSION_ID": old_id}, quiet, old_id, kept),
                             ({"ROOM_TOOLS_SESSION": "", "EARLIER_SESSION_ID": ""}, ""), kept)
        # Turning the preview off leaves the tools' conversation: back to the one from before it.
        was_on = {"SESSION_ID": new_id, "ROOM_TOOLS_SESSION": new_id, "EARLIER_SESSION_ID": old_id}
        got, said = decide(was_on, quiet, new_id, new_id)
        self.assertEqual(got, {"SESSION_ID": old_id, "ROOM_TOOLS_SESSION": "", "EARLIER_SESSION_ID": ""})
        self.assertIn("back to the earlier conversation (%s), without the tools" % old_id, said)
        self.assertEqual(self.remembered, new_id, "the conversation that had the tools is remembered as the owner's alone")
        # Resumed later by its id, it is the session again, still the owner's alone and without the tools.
        left = {"SESSION_ID": old_id, "ROOM_TOOLS_SESSION": "", "EARLIER_SESSION_ID": "", "OWNER_ONLY_SESSIONS": new_id}
        for values in (quiet, on):
            got, said = decide(left, values, new_id, new_id, resume=new_id)
            self.assertEqual((got["ROOM_TOOLS_SESSION"], self.remembered), ("", new_id), values)
            self.assertIn("stays the owner's alone", said)
            cfg = dict(values, SESSION_ID=new_id, ROOM_TOOLS_SESSION="", OWNER_ONLY_SESSIONS=self.remembered)
            self.assertEqual((hold_mod.tools_conversation(cfg), hold_mod.tools_session(cfg)), (True, False))
        self.assertEqual(cc.owner_only_sessions({"OWNER_ONLY_SESSIONS": "%s not-an-id %s" % (new_id, old_id)}),
                         [new_id, old_id])
        # With none from before it (the agent was first installed with the preview): a new one, never the
        # tools' conversation handed to everyone.
        third = "77777777-2222-4333-8444-555555555555"
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            got = cc.room_tools_session({"SESSION_ID": new_id, "ROOM_TOOLS_SESSION": new_id}, quiet, new_id, new_id, "",
                                        lambda: None, "stop it", mint=lambda: third)
        self.assertEqual(got, {"SESSION_ID": third, "ROOM_TOOLS_SESSION": "", "EARLIER_SESSION_ID": "",
                               "OWNER_ONLY_SESSIONS": new_id})
        self.assertIn("a new conversation, without the tools", said.getvalue())
        # While the session that had the tools is not proven gone, it stays the tools' conversation on record
        # (so it stays the owner's alone), and the install after it is stopped leaves it.
        got, said = decide(was_on, quiet, new_id, new_id, live=4242)
        self.assertEqual(got, {"ROOM_TOOLS_SESSION": new_id, "EARLIER_SESSION_ID": old_id})
        self.assertIn("stays the owner's alone", said)
        self.assertIn("stop it", said)
        self.assertEqual(decide(dict(was_on), quiet, new_id, new_id)[0],
                         {"SESSION_ID": old_id, "ROOM_TOOLS_SESSION": "", "EARLIER_SESSION_ID": ""})
        # A conversation the owner names to resume is theirs to pick.
        named = "33333333-2222-4333-8444-555555555555"
        self.assertEqual(decide(was_on, quiet, named, named, resume=named)[0],
                         {"ROOM_TOOLS_SESSION": "", "EARLIER_SESSION_ID": old_id})
        # A first install: the conversation it minted is the tools'.
        self.assertEqual(decide({}, on, old_id, "")[0], {"ROOM_TOOLS_SESSION": old_id, "EARLIER_SESSION_ID": ""})
        # The tools turned on for an agent that already has a conversation: a new one, the earlier kept.
        got, said = decide({"SESSION_ID": old_id}, on, old_id, old_id)
        self.assertEqual(got, {"SESSION_ID": new_id, "ROOM_TOOLS_SESSION": new_id, "EARLIER_SESSION_ID": old_id})
        self.assertIn("the earlier one (%s) is kept, without them" % old_id, said)
        self.assertIn(cc.PREVIEW_NOTE, said, "the owner is told what the preview costs a collaborator")
        # Installed again: the tools' conversation stays, and so does the record of the earlier one.
        again = {"SESSION_ID": new_id, "ROOM_TOOLS_SESSION": new_id, "EARLIER_SESSION_ID": old_id}
        self.assertEqual(decide(again, on, new_id, new_id)[0], {"ROOM_TOOLS_SESSION": new_id, "EARLIER_SESSION_ID": old_id})
        # The old session not proven gone: it runs on, without the tools, and the command says what to do.
        got, said = decide({"SESSION_ID": old_id}, on, old_id, old_id, live=4242)
        self.assertEqual(got, {"ROOM_TOOLS_SESSION": "", "EARLIER_SESSION_ID": ""})
        self.assertIn("not proven gone", said)
        self.assertIn("stop it", said)
        # A conversation the owner names to resume has history: never given the tools.
        got, said = decide({}, on, old_id, old_id, resume=old_id)
        self.assertEqual(got, {"ROOM_TOOLS_SESSION": "", "EARLIER_SESSION_ID": ""})
        self.assertIn("already has history", said)

    def test_a_recorded_proxy_whose_executable_is_gone_is_said_and_offers_nothing(self):
        gone = shlex.join([str(self.d / "moved" / "node"), "/app/ag2-mcp-proxy.mjs"])
        self.assertEqual(self.argv(dict(self.cfg, MCP_PROXY_COMMAND=gone)), [])
        self.assertIn("the recorded proxy's executable is gone", self.lines[-1])
        self.assertEqual(self.argv(dict(self.cfg, MCP_PROXY_COMMAND="'unterminated")), [])
        self.assertIn("not offered: ValueError", self.lines[-1])
        self.assertEqual(self.runs, [], "no exchange for a proxy that cannot run")

    def test_no_proxy_command_no_token_file_gives_nothing(self):
        self.assertEqual(self.argv(dict(self.cfg, MCP_PROXY_COMMAND="")), [])
        self.assertEqual(self.lines, [], "a conf from before the option: silent, no tools")
        self.assertEqual(self.argv(dict(self.cfg, TOKEN_ENV_FILE="")), [])
        self.assertIn("no TOKEN_ENV_FILE", self.lines[-1])
        self.assertEqual(self.runs, [])
        self.assertFalse(self.state.exists(), "nothing written")

    def test_a_failed_exchange_starts_the_session_without_the_tools(self):
        self.rc = 1
        self.assertEqual(self.argv(), [])
        self.assertEqual(self.lines, ["ag2-space MCP tools not offered: boom"])
        self.rc = subprocess.TimeoutExpired("x", 12)
        self.assertEqual(self.argv(), [])
        self.assertIn("did not run (TimeoutExpired)", self.lines[-1])
        self.rc = 0
        self.desc = {"version": 1}
        self.assertEqual(self.argv(), [])
        self.assertIn("printed no proxy descriptor", self.lines[-1])
        self.assertFalse((self.state / hold_mod.MCP_CONFIG_NAME).exists())

    def test_an_entry_that_raises_and_a_state_dir_that_is_not_one_cost_nothing_but_the_tools(self):
        def broken(*a, **kw):
            raise RuntimeError("unexpected")
        self.assertEqual(hold_mod.room_tools_argv(self.cfg, str(self.root), str(self.root / "tasks"), entry=broken,
                                                  log=self.lines.append), [])
        self.assertEqual(self.lines[-1], "ag2-space MCP tools not offered: RuntimeError: unexpected")
        self.state.rmdir()
        elsewhere = self.d / "elsewhere"
        elsewhere.mkdir()
        self.state.symlink_to(elsewhere)
        self.assertEqual(self.argv(), [])
        self.assertIn("is not a directory", self.lines[-1])
        self.assertEqual(list(elsewhere.iterdir()), [], "nothing written through the link")

    def test_an_ag2_space_server_the_agent_already_has_is_kept_and_ours_skipped(self):
        claude_json = self.config_dir / ".claude.json"
        tasks = str(self.root / "tasks")
        for body in ({"mcpServers": {"ag2-space": {"type": "http", "url": "https://mcp.s.space/mcp"}}},
                     {"projects": {tasks: {"mcpServers": {"ag2-space": {"command": "x"}}}}}):
            claude_json.write_text(json.dumps(body))
            self.assertEqual(self.argv(), [])
            self.assertIn("already has an ag2-space server", self.lines[-1])
        claude_json.unlink()
        (self.root / "tasks" / ".mcp.json").write_text(json.dumps({"mcpServers": {"ag2-space": {"command": "x"}}}))
        self.assertEqual(self.argv(), [])
        self.assertIn("already has an ag2-space server", self.lines[-1])
        self.assertEqual(self.runs, [], "no exchange for a server that is not offered")
        (self.root / "tasks" / ".mcp.json").unlink()
        # Its other servers stay: no --strict-mcp-config, ours is added beside them.
        claude_json.write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}},
                                           "projects": {tasks: {"mcpServers": {"notes": {"command": "n"}}}}}))
        got = self.argv()
        self.assertEqual(got[0], "--mcp-config")
        self.assertNotIn("--strict-mcp-config", got)

    def test_the_default_config_dir_is_home_and_an_unreadable_config_offers_nothing(self):
        home = self.d / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            (home / ".claude.json").write_text(json.dumps({"mcpServers": {"ag2-space": {}}}))
            self.assertEqual(self.argv(dict(self.cfg, CLAUDE_CONFIG_DIR="")), [], "~/.claude.json without a config dir")
            (home / ".claude.json").write_text("{not json")
            self.assertEqual(self.argv(dict(self.cfg, CLAUDE_CONFIG_DIR="")), [])
            self.assertIn("not offered: JSONDecodeError", self.lines[-1])
            (home / ".claude.json").unlink()
            (home / ".claude.json").mkdir()
            self.assertEqual(self.argv(dict(self.cfg, CLAUDE_CONFIG_DIR="")), [])
            self.assertIn("was refused", self.lines[-1])

    def test_the_exchange_is_bounded_inside_the_launchers_consumer_wait(self):
        from ag2_sparrow.adapters.claude_code import launcher
        self.assertLess(hold_mod.ROOM_TOOLS_EXCHANGE_S, launcher.CONSUMER_WAIT_S)
        with mock.patch.object(subprocess, "run", side_effect=lambda argv, **kw: kw) as run:
            self.assertEqual(hold_mod.exchange_run(["x"], timeout=mcp_stdio.MINT_TIMEOUT_S)["timeout"],
                             hold_mod.ROOM_TOOLS_EXCHANGE_S)
            self.assertEqual(hold_mod.exchange_run(["x"], timeout=2)["timeout"], 2)
            self.assertEqual(hold_mod.exchange_run(["x"])["timeout"], hold_mod.ROOM_TOOLS_EXCHANGE_S)
        self.assertEqual(run.call_count, 3)

    def test_the_root_with_room_tools_still_starts(self):
        self.assertIsNone(lifecycle.prepare_work_dir(str(self.root)))
        self.assertEqual(self.argv()[0], "--mcp-config")
        (self.state / "ag2-mcp-status.json").write_text("{}")      # what the shim writes there
        self.assertIsNone(lifecycle.prepare_work_dir(str(self.root)), "state/room-tools is this adapter's")


class InstallOption(ConnectBase):
    def install(self, *extra):
        """--work, as a repair line names the job: its record lends what the command does not give."""
        return self.main("install", "--plugin-dir", str(self.plugin), "--work", str(self.work),
                         "--claude-config-dir", str(self.home / ".claude-sutando"),
                         "--hmac-key-file", str(self.home / "task-hmac.key"), *extra)

    def test_mcp_proxy_command_is_checked_recorded_listed_and_kept_on_a_repair(self):
        engine = self.home / "Application Support" / "engine"   # a space in the path, as the desktop's
        engine.mkdir(parents=True)
        node = engine / "node"
        node.write_text("#!/bin/sh\n")
        node.chmod(0o755)
        proxy = shlex.join([str(node), str(engine / "ag2-mcp-proxy.mjs")])
        for verb in ("install", "connect"):
            with contextlib.redirect_stdout(io.StringIO()) as helped, self.assertRaises(SystemExit):
                cli.main([verb, "--help"])
            self.assertIn("--mcp-proxy-command", helped.getvalue(), "the desktop checks help before passing it")
        for bad in ("%s %s" % (node, engine / "ag2-mcp-proxy.mjs"), "'unterminated", "/no/such/node x.mjs", str(node)):
            rc, _, err = self.install_with(self.runner(), False, "--mcp-proxy-command", bad)
            self.assertEqual(rc, 2, bad)
            self.assertIn("--mcp-proxy-command", err)
            self.assertFalse(cli.config_path().exists(), "refused before anything is written")
        runner = self.runner()
        rc, out, err = self.install_with(runner, False, "--mcp-proxy-command", proxy)
        self.assertEqual(rc, 0, out + err)
        cfg = cm.read_config(cli.config_path())
        self.assertEqual(shlex.split(cfg["MCP_PROXY_COMMAND"]), shlex.split(proxy))
        self.assertEqual(cfg["TOKEN_ENV_FILE"], "", "--work: a bridge the owner runs, no token file of the agent's")
        self.assertIn("NOTE mcp:", out, "said: no tools without it")
        # A repair reinstall from the record keeps it.
        rc, out, err = self.install_with(runner, False)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(cm.read_config(cli.config_path())["MCP_PROXY_COMMAND"], cfg["MCP_PROXY_COMMAND"])
        self.assertIn(("MCP_PROXY_COMMAND", "mcp_proxy_command", str), cli.RECORDED)
        # The proxy command alone (what the desktop app passes) turns nothing on: the tools are a preview the
        # owner asks for, listed in --help, recorded, and kept on a repair like the command.
        self.assertIn("--room-tools-preview", helped.getvalue())
        self.assertEqual((cfg["ROOM_TOOLS_PREVIEW"], cfg["ROOM_TOOLS_SESSION"]), ("", ""))
        self.assertIn("ROOM_TOOLS_PREVIEW", [key for key, _, _ in cli.RECORDED])
        rc, out, err = self.install_with(runner, False, "--mcp-proxy-command", proxy, "--room-tools-preview", *ALONE_FLAGS)
        self.assertEqual(rc, 0, out + err)
        cfg = cm.read_config(cli.config_path())
        self.assertEqual((cfg["ROOM_TOOLS_PREVIEW"], cfg["ROOM_TOOLS_SESSION"]), ("1", ""), "no token file: still no tools")
        # A repair install keeps both from the record.
        rc, out, err = self.install_with(runner, False)
        self.assertEqual(rc, 0, out + err)
        again = cm.read_config(cli.config_path())
        self.assertEqual((again["ROOM_TOOLS_PREVIEW"], again["MCP_PROXY_COMMAND"]), ("1", cfg["MCP_PROXY_COMMAND"]))

    def test_a_config_without_it_stays_valid_with_no_tools(self):
        path = self.configure()
        cfg = cm.read_config(path)
        self.assertNotIn("MCP_PROXY_COMMAND", {k for k, v in cfg.items() if v})
        self.assertEqual(hold_mod.room_tools_argv(cfg, str(self.work), str(self.inbox), log=self.fail), [])


class HoldLaunch(ConnectBase):
    """The pane's claude, as hold starts it: --mcp-config last, and only with MCP_PROXY_COMMAND."""

    def setUp(self):
        super().setUp()
        self.token = token_file(self.home)

    def started(self, rc=0, **over):
        if over.get("MCP_PROXY_COMMAND"):
            over.setdefault("ROOM_TOOLS_SESSION", SESSION_ID)      # the conversation install started for the tools
            over.setdefault("ROOM_TOOLS_PREVIEW", "1")
            for key, value in ALONE.items():
                over.setdefault(key, value)
        self.configure(TOKEN_ENV_FILE=self.token, AGENT_ID=AGENT, **over)
        code, _, err = self.hold_cmd(patch=HOLD_PATCH % (DESC, self.token, rc))
        self.assertEqual(code, 0, err)
        return self.session(), err

    def test_the_session_starts_with_mcp_config_last_and_no_token_in_its_env(self):
        session, err = self.started(MCP_PROXY_COMMAND=PROXY)
        state = self.work / "state" / "room-tools"
        self.assertEqual(session["argv"][1:], ["--plugin-dir", str(self.plugin), "--permission-mode",
                                               "bypassPermissions", "--session-id", SESSION_ID,
                                               "--mcp-config", str(state / hold_mod.MCP_CONFIG_NAME)])
        self.assertNotIn("--strict-mcp-config", session["argv"], "the agent's own servers keep working")
        mark = json.loads((self.work / "state" / "room-tools" / "session.json").read_text())
        self.assertEqual((mark["pid"], mark["session_id"]), (session["pid"], SESSION_ID),
                         "the session process itself is marked owner-only, before it runs")
        self.assertIn("ag2-space MCP tools offered", err)
        server = json.loads((state / hold_mod.MCP_CONFIG_NAME).read_text())["mcpServers"]["ag2-space"]
        self.assertEqual(server["env"]["AG2_MCP_DESCRIPTOR"], str(state / mcp_stdio.DESCRIPTOR_NAME))
        env = json.dumps(session["env"])
        self.assertNotIn(SECRET, env, "the agent's token file is not read into the session")
        self.assertFalse({k for k in session["env"] if k.startswith("AG2_MCP")}, "the proxy's env is the shim's only")
        self.assertEqual(inbox.prepare_root(str(self.work)), "", "the root starts again with state/room-tools")

    def test_the_tools_conversation_with_the_preview_off_starts_without_tools_and_stays_the_owners_alone(self):
        session, err = self.started(MCP_PROXY_COMMAND=PROXY, ROOM_TOOLS_PREVIEW="")
        self.assertNotIn("--mcp-config", session["argv"])
        mark = json.loads((self.work / "state" / "room-tools" / "session.json").read_text())
        self.assertEqual(mark["pid"], session["pid"], "marked owner-only: it holds the tools' results")

    def test_a_resumed_conversation_that_once_had_the_tools_is_marked_the_owners_alone(self):
        session, err = self.started(MCP_PROXY_COMMAND=PROXY, ROOM_TOOLS_PREVIEW="", ROOM_TOOLS_SESSION="",
                                    OWNER_ONLY_SESSIONS="11111111-2222-4333-8444-555555555555 " + SESSION_ID)
        self.assertNotIn("--mcp-config", session["argv"])
        mark = json.loads((self.work / "state" / "room-tools" / "session.json").read_text())
        self.assertEqual(mark["pid"], session["pid"], "collaborators are kept out of it")

    def test_a_tools_conversation_that_cannot_be_marked_is_not_started(self):
        self.configure(TOKEN_ENV_FILE=self.token, AGENT_ID=AGENT, MCP_PROXY_COMMAND=PROXY, ROOM_TOOLS_PREVIEW="1",
                       ROOM_TOOLS_SESSION=SESSION_ID, **ALONE)
        patch = HOLD_PATCH % (DESC, self.token, 0) + "hold.mark_tools_generation = lambda *a, **k: False\n"
        code, _, err = self.hold_cmd(patch=patch)
        self.assertNotEqual(code, 0, err)
        self.assertIn("owner-only mark could not be written", err)
        self.assertIsNone(self.session(), "no session ran without its mark")

    def test_no_proxy_command_no_mcp_config_and_a_failed_exchange_starts_without_tools(self):
        session, err = self.started()
        self.assertNotIn("--mcp-config", session["argv"])
        self.assertNotIn("ag2-space", err)
        (self.home / "claude-session.json").unlink()
        session, err = self.started(1, MCP_PROXY_COMMAND=PROXY)
        self.assertNotIn("--mcp-config", session["argv"], "the session starts, without the tools")
        self.assertIn("ag2-space MCP tools not offered: ag2-sparrow-mcp: discovery failed", err)


class IsolatedTurnHasNoMcp(ConnectBase):
    """A restricted sender runs on the isolated `claude -p` turn: no --mcp-config even when the session has the
    room tools, and --strict-mcp-config so the login's own servers are not loaded either."""

    def test_the_isolated_turn_never_gets_the_room_tools(self):
        stub = write_isolated_stub(self.home)
        self.configure(CLAUDE=str(stub), MCP_PROXY_COMMAND=PROXY, TOKEN_ENV_FILE=token_file(self.home),
                       AGENT_ID=AGENT)
        self.assertEqual(inbox.prepare_root(str(self.work)), "")
        holder, _ = hold_consumer(self.work)
        self.addCleanup(unhold, holder)
        (self.inbox / "task-g.txt").write_text("id: task-g\naccess_tier: guest\ntask: post in the room for me\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main(["inbox", "--root", str(self.work), "claim", "task-g"])
        self.assertEqual(rc, 1, out.getvalue())
        self.assertIn("answered on the isolated read-only turn", json.loads(out.getvalue())["refused"])
        run, = isolated_runs(self.home)
        self.assertEqual(run["argv"], list(inbox.isolated.TURN_ARGV))
        self.assertIn("--strict-mcp-config", run["argv"])
        self.assertNotIn("--mcp-config", run["argv"])
        self.assertNotIn("ag2-space", json.dumps(run))
        self.assertFalse({k for k in run["env"] if k.startswith("AG2_MCP")})
        self.assertFalse((self.work / "state" / "room-tools").exists(), "nothing of the room tools was built")


class OnlyTheOwnersSessionHasRoomTools(ConnectBase):
    """The tools belong to a session started with them, which only the owner's tasks enter. Which session that
    is, is read from the running process (hold's mark), never from the install config. A collaborator's task is
    answered outside it; a session started without the tools never gets a call through; and each call still
    needs a turn of the owner's confirmed in the session."""

    CALL = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "room_action_execute"}}

    def setUp(self):
        super().setUp()
        self.stub = write_isolated_stub(self.home)
        self.conf = dict(CLAUDE=str(self.stub), MCP_PROXY_COMMAND=PROXY, TOKEN_ENV_FILE=token_file(self.home),
                         AGENT_ID=AGENT, ROOM_TOOLS_PREVIEW="1", ROOM_TOOLS_SESSION=SESSION_ID)
        self.tighten()
        self.root = str(self.work)
        self.assertEqual(inbox.prepare_root(self.root), "")
        self.holder, _ = hold_consumer(self.work)
        self.addCleanup(unhold, self.holder)

    def tighten(self, **over):
        """The install as the tools need it: the preview on and nobody but the owner served."""
        self.configure(**{**self.conf, **ALONE, **over})

    def loosen(self, **over):
        """The install changed while the session runs on: collaborators and other non-owners served again."""
        self.configure(**{**self.conf, "ALLOW_COLLABORATOR": "1", "ALLOW_NON_OWNER": "1", **over})

    def with_tools(self):
        """The live session process, marked as hold marks one it started with the tools."""
        gen = inbox._generation(self.root)
        self.assertTrue(hold_mod.mark_tools_generation(self.root, gen["pid"], SESSION_ID, start_time=lambda pid: gen["start"]))

    def claim(self, task_id, tier, extra=""):
        (self.inbox / (task_id + ".txt")).write_text("id: %s\n%saccess_tier: %s\ntask: post in the room\n"
                                                     % (task_id, extra, tier))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["inbox", "--root", self.root, "claim", task_id])
        return rc, json.loads(out.getvalue())

    def phase(self, task_id, phase):
        lc = inbox._lifecycle(self.root)
        lc.claims.save(dict(lc.claims.load(task_id), phase=phase))

    def call(self):
        """(forwarded?, the error message) for one room-tool call through the shim, gated on this root."""
        gate = mcp_name_shim.turn_gate({mcp_name_shim.GATE_ENV: json.dumps(hold_mod.gate_argv(self.root))})
        names = mcp_name_shim.Names({"room.action.execute"}, gate=gate)
        names.rename_tools([{"name": "room.action.execute"}])
        forward, reply = mcp_name_shim.to_server(json.dumps(self.CALL) + "\n", names, set())
        return forward is not None, (json.loads(reply)["error"]["message"] if reply else "")

    def test_the_owner_calls_the_tools_and_a_collaborator_never_reaches_the_session_that_has_them(self):
        self.with_tools()
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_NO_OWNER),
                         "nothing running: finding nobody else is not the owner's authorization")
        rc, out = self.claim("task-o", "owner")
        self.assertEqual((rc, out.get("admission")), (0, "owner"), out)
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_NO_OWNER), "claimed, not yet a turn: a queued task is not it")
        self.phase("task-o", lifecycle.CALLING)
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_NO_OWNER), "being sent: not yet confirmed in the session")
        self.phase("task-o", lifecycle.TURN_CONFIRMED)
        self.assertEqual(self.call(), (True, ""), "the owner's own turn, confirmed in the session")

        # The install loosened while the session runs on: the tools close at once, and a collaborator's task
        # is answered on its own turn, never submitted to the session that has them.
        self.loosen()
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_OPENED))
        rc, out = self.claim("task-c", "team", "collaborator: true\n")
        self.assertEqual((rc, "admission" in out), (1, False), out)
        self.assertIn(inbox.TOOLS_SESSION_LIVE, out["refused"])
        self.assertEqual(isolated_runs(self.home), [], "no turn is started beside the session either")
        self.assertEqual((self.results / "task-c.txt").read_text(), task_io.NON_OWNER_REFUSED, "it is told so")
        self.tighten()
        self.assertEqual(self.call(), (True, ""), "the owner's turn is not closed by it")

        # The owner's answer staged: the turn is over, and nothing authorizes a call any more.
        for over in (lifecycle.RESULT_STAGED, lifecycle.PUBLISHED, lifecycle.SETTLED):
            self.phase("task-o", over)
            self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_NO_OWNER), over)

    def test_a_collaborator_alone_authorizes_nothing(self):
        self.with_tools()
        self.loosen()
        rc, out = self.claim("task-c", "team", "collaborator: true\n")
        self.assertEqual(rc, 1, out)
        self.tighten()
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_NO_OWNER))

    def test_admission_follows_the_running_session_not_the_install_config(self):
        # The config says tools, the running session was started without them: collaborators stay in it, as
        # before, and no call gets through even on the owner's confirmed turn.
        self.assertIs(inbox.tools_generation(self.root, inbox._generation(self.root)), False)
        self.loosen()
        rc, out = self.claim("task-c1", "team", "collaborator: true\n")
        self.assertEqual((rc, out.get("admission")), (0, "collaborator"), out)
        self.tighten()
        rc, out = self.claim("task-o1", "owner")
        self.phase("task-o1", lifecycle.TURN_CONFIRMED)
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_NOT_OWNERS))
        self.assertEqual(isolated_runs(self.home), [])

        # The session runs with the tools; the config is then changed, and then cannot be read at all: a
        # collaborator is still kept out of it, because the running process is what is asked.
        self.with_tools()
        self.loosen()
        changed = cli.config_path().read_text().replace("ROOM_TOOLS_SESSION=%s" % SESSION_ID, "ROOM_TOOLS_SESSION=''")
        self.assertNotEqual(changed, cli.config_path().read_text())
        cli.config_path().write_text(changed)
        rc, out = self.claim("task-c2", "team", "collaborator: true\n")
        self.assertEqual((rc, "admission" in out), (1, False), out)
        self.assertIn(inbox.TOOLS_SESSION_LIVE, out["refused"])
        self.assertEqual(isolated_runs(self.home), [])
        cli.config_path().write_text("not a config\n")
        rc, out = self.claim("task-c3", "team", "collaborator: true\n")
        self.assertEqual((rc, "admission" in out), (1, False), "an unreadable config admits nobody: %s" % out)

    def test_a_mark_that_cannot_be_read_keeps_collaborators_out_and_refuses_every_call(self):
        mark = Path(self.root).joinpath(*inbox.TOOLS_MARK)
        mark.parent.mkdir(parents=True, exist_ok=True)
        for text in ("not json", "[]", '{"pid": "seven"}'):
            mark.write_text(text)
            self.assertIsNone(inbox.tools_generation(self.root, inbox._generation(self.root)), text)
        self.loosen()
        rc, out = self.claim("task-c", "team", "collaborator: true\n")
        self.assertEqual((rc, "admission" in out), (1, False), "never admitted on a doubt: %s" % out)
        self.tighten()
        rc, out = self.claim("task-o", "owner")
        self.phase("task-o", lifecycle.TURN_CONFIRMED)
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_UNREAD))
        # A whole mark naming an earlier process is plainly not this session's.
        mark.write_text(json.dumps({"pid": 1, "start": "long ago", "session_id": SESSION_ID}))
        self.assertIs(inbox.tools_generation(self.root, inbox._generation(self.root)), False)
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_NOT_OWNERS))

    def test_an_incomplete_mark_is_unknown_never_a_verified_earlier_start(self):
        gen = inbox._generation(self.root)
        mark = Path(self.root).joinpath(*inbox.TOOLS_MARK)
        mark.parent.mkdir(parents=True, exist_ok=True)
        halves = ({"pid": gen["pid"]}, {"start": gen["start"]}, {"pid": gen["pid"], "start": None},
                  {"pid": gen["pid"], "start": ""}, {"pid": 1}, {"pid": 0, "start": gen["start"]},
                  {"pid": True, "start": gen["start"]}, {"pid": gen["pid"], "start": ["x"]}, {})
        self.loosen()
        for i, half in enumerate(halves):
            with self.subTest(half=half):
                mark.write_text(json.dumps(dict(half, session_id=SESSION_ID)))
                self.assertIsNone(inbox.tools_generation(self.root, gen), "pid and start time are both needed")
                rc, out = self.claim("task-c%d" % i, "team", "collaborator: true\n")
                self.assertEqual((rc, "admission" in out), (1, False), "kept outside the session: %s" % out)
                self.assertIn(inbox.TOOLS_SESSION_LIVE, out["refused"])
        self.assertEqual(isolated_runs(self.home), [], "and no turn is started for it on a doubt")
        self.tighten()
        rc, out = self.claim("task-o", "owner")
        self.phase("task-o", lifecycle.TURN_CONFIRMED)
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_UNREAD), "and no call goes through on it")
        # A live generation whose own start time is not known cannot be matched either.
        self.with_tools()
        self.assertIs(inbox.tools_generation(self.root, gen), True)
        for unknown in ({"pid": gen["pid"]}, {"pid": gen["pid"], "start": None}, {"start": gen["start"]}):
            self.assertIsNone(inbox.tools_generation(self.root, unknown), unknown)
        # And hold never writes half a mark: a start time it cannot read is a mark it could not write.
        self.assertFalse(hold_mod.mark_tools_generation(self.root, gen["pid"], SESSION_ID, start_time=lambda pid: None))

    def test_hold_marks_only_a_session_it_started_with_the_tools(self):
        gen = inbox._generation(self.root)
        mark = Path(self.root).joinpath(*hold_mod.TOOLS_MARK)
        self.assertEqual(hold_mod.TOOLS_MARK, inbox.TOOLS_MARK)
        self.with_tools()
        self.assertEqual(stat.S_IMODE(mark.stat().st_mode), 0o600)
        self.assertEqual(json.loads(mark.read_text()), {"pid": gen["pid"], "start": gen["start"], "session_id": SESSION_ID})
        self.assertTrue(hold_mod.mark_tools_generation(self.root), "a start without the tools clears it")
        self.assertFalse(mark.exists())
        self.assertTrue(hold_mod.mark_tools_generation(self.root), "and clearing nothing is fine")
        with mock.patch.object(hold_mod.cm, "_write", side_effect=OSError(28, "No space left on device")):
            self.assertFalse(hold_mod.mark_tools_generation(self.root, gen["pid"], SESSION_ID), "said, never assumed")

    def test_a_restricted_sender_authorizes_nothing_and_does_not_close_the_owners_turn(self):
        self.with_tools()
        self.loosen()
        rc, out = self.claim("task-g", "guest")
        self.assertEqual(rc, 1, out)
        self.tighten()
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_NO_OWNER))
        rc, out = self.claim("task-o", "owner")
        self.assertEqual(rc, 0, out)
        self.phase("task-o", lifecycle.TURN_CONFIRMED)
        self.assertEqual(self.call(), (True, ""))

    def test_what_cannot_be_checked_refuses(self):
        self.with_tools()
        rc, out = self.claim("task-o", "owner")
        self.assertEqual(rc, 0, out)
        self.phase("task-o", lifecycle.TURN_CONFIRMED)
        (self.inbox / "task-o.txt").unlink()
        self.assertEqual(inbox.room_tools_refusal(self.root), inbox.ROOM_TOOLS_NO_OWNER,
                         "a task file already gone is an answered task")
        (self.inbox / "task-o.txt").mkdir()
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_UNREAD))
        (self.inbox / "task-o.txt").rmdir()
        unhold(self.holder)
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_NO_SESSION), "no live session holds the root")

    def test_the_gate_verb_answers_as_the_shim_reads_it(self):
        def verb():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["inbox", "--root", self.root, "room-tools"])
            return rc, json.loads(out.getvalue())
        self.with_tools()
        self.assertEqual(verb(), (0, {"allowed": False, "why": inbox.ROOM_TOOLS_NO_OWNER}))
        self.claim("task-o", "owner")
        self.phase("task-o", lifecycle.TURN_CONFIRMED)
        self.assertEqual(verb(), (0, {"allowed": True, "why": ""}))

    def test_an_install_loosened_or_unreadable_while_the_session_runs_closes_the_tools_at_once(self):
        self.with_tools()
        self.claim("task-o", "owner")
        self.phase("task-o", lifecycle.TURN_CONFIRMED)
        self.assertEqual(self.call(), (True, ""))
        opened = ({"ALLOW_COLLABORATOR": "1"}, {"ALLOW_NON_OWNER": "1"}, {"ALLOW_COLLABORATOR": ""},
                  {"ALLOW_NON_OWNER": ""}, {"ALLOW_COLLABORATOR": "no"}, {"ALLOW_NON_OWNER": "false"},
                  {"ROOM_TOOLS_PREVIEW": ""}, {"ROOM_TOOLS_PREVIEW": "yes"})
        for over in opened:
            with self.subTest(over=over):
                self.tighten(**over)
                self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_OPENED))
                self.assertFalse(hold_mod.tools_session({**self.conf, "SESSION_ID": SESSION_ID, **ALONE, **over}),
                                 "and the next start gets no tools")
        cli.config_path().write_text("not a config\n")
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_OPENED), "a config that cannot be read is not a yes")
        cli.config_path().unlink()
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_OPENED))
        self.tighten()
        self.assertEqual(self.call(), (True, ""), "tightened again, the owner's confirmed turn calls them")

    def test_a_start_with_the_preview_on_but_others_served_or_unknown_gets_no_tools_and_says_why(self):
        base = dict(self.conf, SESSION_ID=SESSION_ID, ROOT=self.root)
        for over in ({}, {"ALLOW_COLLABORATOR": "0"}, {"ALLOW_NON_OWNER": "0"},
                     {"ALLOW_COLLABORATOR": "0", "ALLOW_NON_OWNER": "1"}, {"ALLOW_COLLABORATOR": None, "ALLOW_NON_OWNER": "0"}):
            lines = []
            got = hold_mod.room_tools_argv(dict(base, **over), self.root, self.root, entry=self.no_exchange, log=lines.append)
            self.assertEqual(got, [], over)
            self.assertIn("serves collaborators or other non-owners", lines[0])
            self.assertTrue(hold_mod.tools_conversation(dict(base, **over)), "still the owner's alone: never shared")

    def no_exchange(self, *a, **kw):
        raise AssertionError("no token exchange is attempted")

    def test_the_gate_reads_the_config_the_session_was_started_from(self):
        with mock.patch.dict(os.environ, {hold_mod.ENV_CONFIG_DIR: "/agents/one"}):
            code = hold_mod.gate_argv(self.root)[3]
        self.assertIn("os.environ[%r] = %r; " % (hold_mod.ENV_CONFIG_DIR, "/agents/one"), code)
        with mock.patch.dict(os.environ):
            os.environ.pop(hold_mod.ENV_CONFIG_DIR, None)
            self.assertNotIn("os.environ[", hold_mod.gate_argv(self.root)[3])

    def test_loosening_either_setting_while_the_tools_session_runs_starts_nothing_for_a_non_owner(self):
        self.with_tools()
        senders = (("team", "collaborator: true\n"), ("team", ""), ("guest", ""))
        n = 0
        for over in ({"ALLOW_COLLABORATOR": "1"}, {"ALLOW_NON_OWNER": "1"},
                     {"ALLOW_COLLABORATOR": "1", "ALLOW_NON_OWNER": "1"}):
            self.tighten(**over)
            for tier, extra in senders:
                with self.subTest(over=over, tier=tier, extra=extra):
                    n, task_id = n + 1, "task-n%d" % n
                    rc, out = self.claim(task_id, tier, extra)
                    self.assertEqual((rc, "admission" in out), (1, False), out)
                    self.assertTrue((self.results / (task_id + ".txt")).exists(), "answered, by the refusal")
        self.assertEqual(isolated_runs(self.home), [], "no non-owner process was started while it runs")
        rc, out = self.claim("task-o", "owner")
        self.assertEqual((rc, out.get("admission")), (0, "owner"), "the owner is still served: %s" % out)

    def test_once_the_tools_session_is_gone_the_usual_non_owner_flow_is_back(self):
        self.with_tools()
        self.loosen()
        rc, out = self.claim("task-g1", "guest")
        self.assertIn(inbox.TOOLS_SESSION_LIVE, out["refused"])
        self.assertEqual(isolated_runs(self.home), [])
        # The preview left safely: its process stopped, and the next start (without the tools) cleared the mark.
        self.assertTrue(hold_mod.mark_tools_generation(self.root))
        self.assertIs(inbox.tools_generation(self.root, inbox._generation(self.root)), False)
        rc, out = self.claim("task-g2", "guest")
        self.assertEqual((rc, "admission" in out), (1, False), out)
        self.assertIn("never submitted to this session", out["refused"])
        run, = isolated_runs(self.home)
        self.assertIn("--strict-mcp-config", run["argv"])
        self.assertFalse({k for k in run["env"] if k.startswith("AG2_MCP")}, "nothing of the tools reaches its turn")
        rc, out = self.claim("task-c", "team", "collaborator: true\n")
        self.assertEqual((rc, out.get("admission")), (0, "collaborator"), "the shared session, as before: %s" % out)

    def test_an_install_that_never_had_the_preview_serves_non_owners_as_before(self):
        self.loosen(ROOM_TOOLS_PREVIEW="", ROOM_TOOLS_SESSION="")
        rc, out = self.claim("task-c", "team", "collaborator: true\n")
        self.assertEqual((rc, out.get("admission")), (0, "collaborator"), out)
        rc, out = self.claim("task-g", "guest")
        self.assertIn("never submitted to this session", out["refused"])
        self.assertEqual(len(isolated_runs(self.home)), 1)

    def test_a_resumed_preview_conversation_stays_the_owners_alone_whoever_the_install_serves(self):
        # The session runs on a conversation that once had the tools (hold marks it), on an install that now
        # serves everyone and has the preview off: nobody but the owner is answered while it runs.
        resumed = dict(self.conf, ROOM_TOOLS_PREVIEW="", ROOM_TOOLS_SESSION="", OWNER_ONLY_SESSIONS=SESSION_ID,
                       SESSION_ID=SESSION_ID)
        self.assertTrue(hold_mod.tools_conversation(resumed))
        self.assertFalse(hold_mod.tools_session(resumed))
        self.with_tools()       # what hold writes for a start on such a conversation
        self.loosen(ROOM_TOOLS_PREVIEW="", ROOM_TOOLS_SESSION="", OWNER_ONLY_SESSIONS=SESSION_ID)
        for i, (tier, extra) in enumerate((("team", "collaborator: true\n"), ("guest", ""))):
            rc, out = self.claim("task-r%d" % i, tier, extra)
            self.assertEqual((rc, "admission" in out), (1, False), out)
            self.assertIn(inbox.TOOLS_SESSION_LIVE, out["refused"])
        self.assertEqual(isolated_runs(self.home), [])
        rc, out = self.claim("task-o", "owner")
        self.phase("task-o", lifecycle.TURN_CONFIRMED)
        self.assertEqual(self.call(), (False, inbox.ROOM_TOOLS_OPENED), "and it has no tools")

    def test_a_gate_that_cannot_answer_refuses(self):
        def gated(raw, run):
            check = mcp_name_shim.turn_gate({mcp_name_shim.GATE_ENV: raw}, run=run)
            return check()

        def answers(stdout, returncode=0):
            return lambda argv, **kw: mock.Mock(stdout=stdout, returncode=returncode)

        def raises(error):
            def run(argv, **kw):
                raise error
            return run
        ok = json.dumps(["gate"])
        self.assertEqual(gated(ok, answers('{"allowed": true}')), "")
        self.assertEqual(gated(ok, answers('{"allowed": false, "why": "not now"}')), "not now")
        for stdout in ("", "not json", "[]", '{"allowed": "yes"}', '{"allowed": false}', '{"allowed": false, "why": 7}'):
            self.assertEqual(gated(ok, answers(stdout)), mcp_name_shim.GATE_UNCHECKED, stdout)
        for code in (1, 2, -9):
            self.assertEqual(gated(ok, answers('{"allowed": true}', code)), mcp_name_shim.GATE_UNCHECKED,
                             "a gate that exited %s allowed nothing, whatever it printed" % code)
        for error in (OSError(2, "no such file"), subprocess.TimeoutExpired("gate", 10)):
            self.assertEqual(gated(ok, raises(error)), mcp_name_shim.GATE_UNCHECKED, error)
        for raw in ("not json", "{}", "[]", '["gate", 7]'):
            self.assertEqual(gated(raw, answers('{"allowed": true}')), mcp_name_shim.GATE_UNCHECKED, raw)

    def test_a_shim_without_the_gate_is_unchanged_for_the_other_connectors(self):
        self.assertIsNone(mcp_name_shim.turn_gate({}))
        names = mcp_name_shim.Names({"room.action.execute"})
        names.rename_tools([{"name": "room.action.execute"}])
        forward, reply = mcp_name_shim.to_server(json.dumps(self.CALL) + "\n", names, set())
        self.assertEqual((json.loads(forward)["params"]["name"], reply), ("room.action.execute", None))


class PreviewThroughInstall(AgentRootBase):
    """The install entry point, for an agent already installed and stopped: the proxy command alone changes
    nothing, and the preview starts the tools' conversation, keeping the earlier one."""

    def test_the_preview_starts_a_new_conversation_and_the_proxy_alone_changes_nothing(self):
        from ag2_sparrow.adapters.claude_code import cli as cc
        engine = self.home / "Application Support" / "engine"
        engine.mkdir(parents=True)
        node = engine / "node"
        node.write_text("#!/bin/sh\n")
        node.chmod(0o755)
        proxy = shlex.join([str(node), str(engine / "ag2-mcp-proxy.mjs")])
        fake = self.bridged()
        rc, out, err = self.install_default(fake)
        self.assertEqual(rc, 0, out + err)
        first = self.cfg()
        self.assertTrue(first["TOKEN_ENV_FILE"], "the agent's own token file")
        self.assertEqual((first["ROOM_TOOLS_PREVIEW"], first["ROOM_TOOLS_SESSION"]), ("", ""))

        # What an app update passes: the same conversation, no tools, nothing said about them.
        rc, out, err = self.install_default(fake, "--mcp-proxy-command", proxy)
        self.assertEqual(rc, 0, out + err)
        quiet = self.cfg()
        self.assertEqual((quiet["SESSION_ID"], quiet["ROOM_TOOLS_SESSION"], quiet["EARLIER_SESSION_ID"]),
                         (first["SESSION_ID"], "", ""))
        self.assertNotIn("NOTE mcp", out)

        # The owner turns the preview on: a new conversation for the tools, the earlier one kept by its id.
        rc, out, err = self.install_default(fake, "--mcp-proxy-command", proxy, "--room-tools-preview", *ALONE_FLAGS)
        self.assertEqual(rc, 0, out + err)
        on = self.cfg()
        self.assertNotEqual(on["SESSION_ID"], first["SESSION_ID"])
        self.assertTrue(cc.SESSION_ID_RE.match(on["SESSION_ID"]))
        self.assertEqual((on["ROOM_TOOLS_SESSION"], on["EARLIER_SESSION_ID"], on["ROOM_TOOLS_PREVIEW"]),
                         (on["SESSION_ID"], first["SESSION_ID"], "1"))
        self.assertIn(cc.PREVIEW_NOTE, out)
        self.assertTrue(hold_mod.tools_session(on))

        # Installed again: the tools' conversation stays the session.
        rc, out, err = self.install_default(fake, "--mcp-proxy-command", proxy, "--room-tools-preview", *ALONE_FLAGS)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.cfg()["SESSION_ID"], on["SESSION_ID"])
        self.assertEqual(self.cfg()["ROOM_TOOLS_SESSION"], on["SESSION_ID"])

        # A room tool's result lands in the tools' conversation while the preview is on.
        transcript = Path(on["CLAUDE_CONFIG_DIR"]) / "projects" / "-tasks" / (on["SESSION_ID"] + ".jsonl")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"type": "tool_result", "content": "room.action.execute: owner-only page"}) + "\n")
        self.assertEqual(hold_mod.claude_argv(on, on["SESSION_ID"])[-2:], ["--resume", on["SESSION_ID"]])

        # The preview turned off (what the app's next update passes): the original conversation is the session
        # again, and it has no tools.
        rc, out, err = self.install_default(fake, "--mcp-proxy-command", proxy)
        self.assertEqual(rc, 0, out + err)
        off = self.cfg()
        self.assertEqual((off["SESSION_ID"], off["ROOM_TOOLS_SESSION"], off["EARLIER_SESSION_ID"], off["ROOM_TOOLS_PREVIEW"]),
                         (first["SESSION_ID"], "", "", ""))
        self.assertIn("back to the earlier conversation (%s)" % first["SESSION_ID"], out)
        self.assertFalse(hold_mod.tools_session(off))
        self.assertFalse(hold_mod.tools_conversation(off))
        self.assertNotEqual(off["SESSION_ID"], on["SESSION_ID"],
                            "the conversation collaborators share again is not the one that had the tools")
        started = hold_mod.claude_argv(off, off["SESSION_ID"])
        self.assertNotIn(on["SESSION_ID"], started, "the session collaborators enter never resumes the preview's history")
        self.assertEqual(started[-1], first["SESSION_ID"])
        self.assertIn("owner-only page", transcript.read_text(), "the preview's conversation is kept on disk, untouched")
        self.assertEqual(off["OWNER_ONLY_SESSIONS"], on["SESSION_ID"])

        # On, off, then the owner resumes the old preview conversation by its id: it is the session again, but
        # never the one collaborators share, and it has no tools.
        rc, out, err = self.install_default(fake, "--mcp-proxy-command", proxy, "--resume-session", on["SESSION_ID"])
        self.assertEqual(rc, 0, out + err)
        back = self.cfg()
        self.assertEqual((back["SESSION_ID"], back["ROOM_TOOLS_SESSION"], back["OWNER_ONLY_SESSIONS"]),
                         (on["SESSION_ID"], "", on["SESSION_ID"]))
        self.assertIn("stays the owner's alone", out)
        self.assertTrue(hold_mod.tools_conversation(back), "hold marks a session on it as the owner's alone")
        self.assertFalse(hold_mod.tools_session(back))
        self.assertEqual(hold_mod.room_tools_argv(back, back["ROOT"], str(Path(back["ROOT"]) / "tasks"),
                                                  log=lambda line: None), [])
        self.assertEqual(hold_mod.room_tools_argv(off, off["ROOT"], str(Path(off["ROOT"]) / "tasks"),
                                                  log=lambda line: None), [], "started again, it gets no tools")

    def test_the_preview_on_an_install_that_serves_others_gets_no_tools_and_loosening_later_leaves_them(self):
        engine = self.home / "Application Support" / "engine"
        engine.mkdir(parents=True)
        node = engine / "node"
        node.write_text("#!/bin/sh\n")
        node.chmod(0o755)
        proxy = shlex.join([str(node), str(engine / "ag2-mcp-proxy.mjs")])
        fake = self.bridged()
        rc, out, err = self.install_default(fake)
        self.assertEqual(rc, 0, out + err)
        first = self.cfg()
        # The preview asked for with the defaults, or with only one of the two kept out: no tools, same session.
        for flags in ((), ALONE_FLAGS[:1], ("--allow-collaborator", ALONE_FLAGS[1])):
            rc, out, err = self.install_default(fake, "--mcp-proxy-command", proxy, "--room-tools-preview", *flags)
            self.assertEqual(rc, 0, out + err)
            got = self.cfg()
            self.assertIn(cli.OWNER_ALONE_NOTE, out, flags)
            self.assertNotIn(cli.PREVIEW_NOTE, out)
            self.assertEqual((got["SESSION_ID"], got["ROOM_TOOLS_SESSION"]), (first["SESSION_ID"], ""), flags)
            self.assertFalse(hold_mod.tools_session(got))
        # Both kept out: the tools' own conversation.
        rc, out, err = self.install_default(fake, "--mcp-proxy-command", proxy, "--room-tools-preview", *ALONE_FLAGS)
        self.assertEqual(rc, 0, out + err)
        on = self.cfg()
        self.assertTrue(hold_mod.tools_session(on))
        self.assertNotIn(cli.OWNER_ALONE_NOTE, out)
        # Loosened later, the preview still asked for: the tools stop, the session leaves their conversation,
        # and that conversation is remembered as the owner's alone.
        rc, out, err = self.install_default(fake, "--mcp-proxy-command", proxy, "--room-tools-preview",
                                            "--allow-collaborator", ALONE_FLAGS[1])
        self.assertEqual(rc, 0, out + err)
        loose = self.cfg()
        self.assertIn(cli.OWNER_ALONE_NOTE, out)
        self.assertEqual((loose["SESSION_ID"], loose["ROOM_TOOLS_SESSION"]), (first["SESSION_ID"], ""))
        self.assertEqual(loose["OWNER_ONLY_SESSIONS"], on["SESSION_ID"])
        self.assertFalse(hold_mod.tools_session(loose))
        self.assertNotIn(on["SESSION_ID"], hold_mod.claude_argv(loose, loose["SESSION_ID"]),
                         "the session collaborators enter never resumes the tools' history")
        # Resumed by its id on the loosened install: the owner's alone, without the tools.
        transcript = Path(on["CLAUDE_CONFIG_DIR"]) / "projects" / "-tasks" / (on["SESSION_ID"] + ".jsonl")
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"type": "tool_result", "content": "owner-only page"}) + "\n")
        rc, out, err = self.install_default(fake, "--mcp-proxy-command", proxy, "--room-tools-preview",
                                            "--resume-session", on["SESSION_ID"])
        self.assertEqual(rc, 0, out + err)
        back = self.cfg()
        self.assertEqual((back["SESSION_ID"], back["ROOM_TOOLS_SESSION"]), (on["SESSION_ID"], ""))
        self.assertTrue(hold_mod.tools_conversation(back))
        self.assertFalse(hold_mod.tools_session(back))

    def fake_transcript(self, config_dir, session_id, text="owner-only page"):
        path = Path(config_dir) / "projects" / "-tasks" / (session_id + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "tool_result", "content": text}) + "\n")
        return path

    def test_uninstall_then_reinstall_and_resume_keeps_the_preview_conversation_the_owners_alone(self):
        import claude_code_fixtures as fx
        engine = self.home / "Application Support" / "engine"
        engine.mkdir(parents=True)
        node = engine / "node"
        node.write_text("#!/bin/sh\n")
        node.chmod(0o755)
        proxy = shlex.join([str(node), str(engine / "ag2-mcp-proxy.mjs")])
        fake = self.bridged()
        rc, out, err = self.install_default(fake, "--mcp-proxy-command", proxy, "--room-tools-preview", *ALONE_FLAGS)
        self.assertEqual(rc, 0, out + err)
        on = self.cfg()
        home = on["CLAUDE_CONFIG_DIR"]
        self.assertTrue(hold_mod.tools_session(on))
        store = Path(home) / hold_mod.OWNER_ONLY_STORE
        self.assertEqual((store.read_text().split(), stat.S_IMODE(store.stat().st_mode)), ([on["SESSION_ID"]], 0o600),
                         "recorded beside the transcripts before the conversation holds any tool result")
        transcript = self.fake_transcript(home, on["SESSION_ID"])
        ordinary = "77777777-2222-4333-8444-555555555555"
        self.fake_transcript(home, ordinary, "an ordinary chat")

        # Uninstall removes the install config, and with it OWNER_ONLY_SESSIONS; the transcripts stay.
        path = cli.config_path(cm.select_job(fx.L)[0] or fx.LA)
        with mock.patch.object(fx.sm, "_run", side_effect=fake):
            rc, out, err = self.main("uninstall")
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(path.exists(), "the install config is gone")
        self.assertTrue(transcript.exists() and store.exists(), "the transcript and the record beside it are not")

        # Reinstalled with the defaults (everyone served), naming the old preview conversation to resume.
        rc, out, err = self.install_default(fake, "--resume-session", on["SESSION_ID"])
        self.assertEqual(rc, 0, out + err)
        back = self.cfg()
        self.assertEqual((back["SESSION_ID"], back["ROOM_TOOLS_SESSION"]), (on["SESSION_ID"], ""))
        self.assertIn(on["SESSION_ID"], back["OWNER_ONLY_SESSIONS"].split())
        self.assertIn("stays the owner's alone", out)
        self.assertTrue(hold_mod.tools_conversation(back), "hold marks a session on it: nobody else is answered")
        self.assertFalse(hold_mod.tools_session(back), "and it has no tools")

        # An ordinary conversation resumed on the same install is shared, as before.
        with mock.patch.object(fx.sm, "_run", side_effect=fake):
            self.assertEqual(self.main("uninstall")[0], 0)
        rc, out, err = self.install_default(fake, "--resume-session", ordinary)
        self.assertEqual(rc, 0, out + err)
        plain = self.cfg()
        self.assertEqual(plain["SESSION_ID"], ordinary)
        self.assertFalse(hold_mod.tools_conversation(plain), "never had the tools: the session collaborators share")
        self.assertNotIn("stays the owner's alone", out)

        # The record cannot be read: a resumed conversation cannot be shown shareable, so it is the owner's alone.
        with mock.patch.object(fx.sm, "_run", side_effect=fake):
            self.assertEqual(self.main("uninstall")[0], 0)
        store.unlink()
        store.mkdir()
        self.assertIsNone(hold_mod.owner_only_kept(home))
        rc, out, err = self.install_default(fake, "--resume-session", ordinary)
        self.assertEqual(rc, 0, out + err)
        doubt = self.cfg()
        self.assertEqual(doubt["OWNER_ONLY_SESSIONS"].split(), [ordinary])
        self.assertTrue(hold_mod.tools_conversation(doubt))
        self.assertIn("cannot be shown not to have", out)
        # And with no way to record it, a new preview conversation is not given the tools.
        with mock.patch.object(fx.sm, "_run", side_effect=fake):
            self.assertEqual(self.main("uninstall")[0], 0)
        rc, out, err = self.install_default(fake, "--mcp-proxy-command", proxy, "--room-tools-preview", *ALONE_FLAGS)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("could not be recorded as the owner's alone", out)
        self.assertFalse(hold_mod.tools_session(self.cfg()))

    def test_an_agent_first_installed_with_the_preview_gets_a_new_conversation_when_it_is_turned_off(self):
        engine = self.home / "Application Support" / "engine"
        engine.mkdir(parents=True)
        node = engine / "node"
        node.write_text("#!/bin/sh\n")
        node.chmod(0o755)
        proxy = shlex.join([str(node), str(engine / "ag2-mcp-proxy.mjs")])
        fake = self.bridged()
        rc, out, err = self.install_default(fake, "--mcp-proxy-command", proxy, "--room-tools-preview", *ALONE_FLAGS)
        self.assertEqual(rc, 0, out + err)
        on = self.cfg()
        self.assertEqual((on["ROOM_TOOLS_SESSION"], on["EARLIER_SESSION_ID"]), (on["SESSION_ID"], ""))
        rc, out, err = self.install_default(fake, "--mcp-proxy-command", proxy)
        self.assertEqual(rc, 0, out + err)
        off = self.cfg()
        self.assertNotEqual(off["SESSION_ID"], on["SESSION_ID"], "never the tools' conversation, shared")
        self.assertEqual((off["ROOM_TOOLS_SESSION"], off["EARLIER_SESSION_ID"]), ("", ""))
        self.assertIn("a new conversation, without the tools", out)
        self.assertIn(on["SESSION_ID"], out, "the one that had them is named, and kept")


if __name__ == "__main__":
    unittest.main()
