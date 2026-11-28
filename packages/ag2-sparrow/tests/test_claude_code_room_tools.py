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
    cli, cm, ConnectBase, hold_consumer, hold_mod, inbox, isolated_runs, SESSION_ID, unhold, write_isolated_stub,
)
from ag2_sparrow import mcp_auth, mcp_stdio, task_lifecycle as lifecycle  # noqa: E402

SECRET = "relay-secret-do-not-leak"
AGENT = "@cl:s.space"
PROXY = shlex.join(["/app/node", "/app/ag2-mcp-proxy.mjs"])
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
                    "CLAUDE_CONFIG_DIR": str(self.config_dir), "ROOT": str(self.root)}
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
                                         "AG2_MCP_LOG": str(self.state / mcp_stdio.PROXY_LOG_NAME)})
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
        self.assertIn("ag2-space MCP tools offered", err)
        server = json.loads((state / hold_mod.MCP_CONFIG_NAME).read_text())["mcpServers"]["ag2-space"]
        self.assertEqual(server["env"]["AG2_MCP_DESCRIPTOR"], str(state / mcp_stdio.DESCRIPTOR_NAME))
        env = json.dumps(session["env"])
        self.assertNotIn(SECRET, env, "the agent's token file is not read into the session")
        self.assertFalse({k for k in session["env"] if k.startswith("AG2_MCP")}, "the proxy's env is the shim's only")
        self.assertEqual(inbox.prepare_root(str(self.work)), "", "the root starts again with state/room-tools")

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


if __name__ == "__main__":
    unittest.main()
