#!/usr/bin/env python3
"""A Claude Code MCP entry that connects straight to a server that moved is named, never edited.

`connections.moved_mcp_servers` lists the config's `url` entries on the MCP endpoint of a server
with a successor; `scripts/connections.py moved-mcp-servers` prints one line per entry with the
successor and the remove command (`connections.mcp_remove_command`: the name shell-quoted as one
argument after `--`, and pinned to the file that was read). A proxy entry, an entry on a live server and an unreadable
config list nothing.

Run: python3 tests/connections-moved-mcp-servers.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import connections as C  # noqa: E402
import dotspace  # noqa: E402

OLD, NEW = dotspace.LEGACY_SERVER, dotspace.SUCCESSOR_SERVER
OLD_MCP = dotspace.dotspace_for(OLD, fetch=dotspace.NO_FETCH).mcp_url
NEW_MCP = dotspace.dotspace_for(NEW, fetch=dotspace.NO_FETCH).mcp_url


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="moved-mcp-servers-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config = self.root / ".claude.json"

    def write(self, servers):
        self.config.write_text(json.dumps({"mcpServers": servers}))
        return self.config.read_bytes()


class TheDetector(Base):
    def test_an_entry_on_the_moved_servers_endpoint_is_listed_with_its_successor(self):
        self.write({"Old Space": {"type": "http", "url": OLD_MCP}})
        self.assertEqual(C.moved_mcp_servers(self.config),
                         [{"name": "Old Space", "server": OLD, "successor": NEW, "mcp_url": NEW_MCP}])

    def test_entries_on_a_live_server_a_proxy_and_an_unrelated_host_are_not_listed(self):
        self.write({"live": {"type": "http", "url": NEW_MCP},
                    "proxy": {"command": "node", "args": ["proxy.mjs"], "env": {"AG2_MCP_DESCRIPTOR": "/x/ag2-mcp.json"}},
                    "other": {"type": "http", "url": "https://mcp.example-fixture.net/mcp"},
                    "sibling": {"type": "http", "url": f"https://other-host.{OLD}/mcp"},
                    "odd": "not a table", "no-url": {"type": "http", "url": 7}})
        self.assertEqual(C.moved_mcp_servers(self.config), [])

    def test_every_moved_entry_is_listed_in_entry_order(self):
        self.write({"b": {"url": OLD_MCP}, "live": {"url": NEW_MCP}, "a": {"type": "sse", "url": OLD_MCP + "?x=1"}})
        self.assertEqual([e["name"] for e in C.moved_mcp_servers(self.config)], ["b", "a"])

    def test_a_missing_or_unreadable_config_lists_nothing(self):
        self.assertEqual(C.moved_mcp_servers(self.config), [])
        self.config.write_text("{not json")
        self.assertEqual(C.moved_mcp_servers(self.config), [])
        self.config.write_text(json.dumps({"mcpServers": ["a list"]}))
        self.assertEqual(C.moved_mcp_servers(self.config), [])

    def test_the_default_config_is_the_claude_homes(self):
        self.write({"Old Space": {"type": "http", "url": OLD_MCP}})
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)}):
            self.assertEqual([e["name"] for e in C.moved_mcp_servers()], ["Old Space"])


class TheRemoveCommand(Base):
    NAMES = ("plain", "Old Space", "$HOME", "`id`", "$(id)", "it's", 'say "hi"', "-x", "-rf", "--scope", "--", "a;b|c&d",
             "tab\there", "back\\slash", "*", "~", "!!", "")
    TAIL = ["claude", "mcp", "remove", "--scope", "user", "--"]

    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def test_the_name_parses_back_as_one_literal_argument_after_the_option_terminator(self):
        for name in self.NAMES:
            for config in (self.home / ".claude.json", self.root / "seat" / ".claude.json"):
                argv = shlex.split(C.mcp_remove_command(name, config))
                self.assertEqual(argv[-7:], self.TAIL + [name], name)

    def test_the_home_folders_own_file_gets_no_folder_assignment(self):
        self.assertEqual(C.mcp_remove_command("x", self.home / ".claude.json"),
                         "env -u CLAUDE_CONFIG_DIR claude mcp remove --scope user -- x")
        self.assertEqual(C.mcp_remove_command("x"), "env -u CLAUDE_CONFIG_DIR claude mcp remove --scope user -- x")

    def test_any_config_folder_is_named_as_one_env_assignment(self):
        stock = self.home / ".claude"
        with mock.patch.dict(os.environ, {"SOURCE_CLAUDE_CONFIG_DIR": str(self.root / "seat")}):
            for folder in (self.root / "seat", stock, self.root / "seat's $dir with `ticks`"):
                argv = shlex.split(C.mcp_remove_command("x", folder / ".claude.json"))
                self.assertEqual(argv, [f"CLAUDE_CONFIG_DIR={folder}"] + self.TAIL + ["x"])
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "seat")}):
            self.assertEqual(shlex.split(C.mcp_remove_command("x"))[0], f"CLAUDE_CONFIG_DIR={self.root / 'seat'}")

    def test_a_file_claude_would_not_target_gets_no_command(self):
        for config in (self.root / "seat" / "mcp.json", self.home / "claude.json", self.root / ".claude.json.bak"):
            self.assertEqual(C.mcp_remove_command("x", config), "", config)

    def test_a_relative_config_is_named_by_its_absolute_folder(self):
        first = shlex.split(C.mcp_remove_command("x", Path("rel") / ".claude.json"))[0]
        self.assertEqual(first, f"CLAUDE_CONFIG_DIR={Path.cwd() / 'rel'}")

    def test_a_shell_with_another_folder_inherited_still_reaches_the_file_that_was_read(self):
        fake = self.root / "bin" / "claude"
        fake.parent.mkdir()
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$#" "${CLAUDE_CONFIG_DIR-unset}"; for a in "$@"; do printf "%s\\0" "$a"; done\n')
        fake.chmod(0o755)
        own = self.root / "seat's $dir"
        for inherited in ({}, {"CLAUDE_CONFIG_DIR": str(self.root / "some other folder")}):
            env = {"PATH": f"{fake.parent}:/usr/bin:/bin", "HOME": str(self.home), **inherited}
            for name in self.NAMES:
                for config, folder in ((self.home / ".claude.json", "unset"), (own / ".claude.json", str(own))):
                    r = subprocess.run(["/bin/sh", "-c", C.mcp_remove_command(name, config)], env=env,
                                       capture_output=True, text=True, timeout=30, cwd=self.root)
                    count, seen, rest = r.stdout.split("\n", 2)
                    self.assertEqual((r.returncode, count, seen, rest.split("\0")[:-1]),
                                     (0, "6", folder, self.TAIL[1:] + [name]), (name, inherited, r.stderr))


class TheDefaultConfig(Base):
    def test_without_a_config_folder_it_is_the_home_folders_own_file(self):
        home = self.root / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / ".claude.json").write_text(json.dumps({"mcpServers": {"nested": {"url": OLD_MCP}}}))
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {"root": {"url": OLD_MCP}}}))
        with mock.patch.dict(os.environ, {"HOME": str(home), "CLAUDE_HOME": str(home / ".claude")}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(C.claude_config_file(), home / ".claude.json")
            self.assertEqual([e["name"] for e in C.moved_mcp_servers()], ["root"])

    def two_configs(self):
        """A home with a descriptor named in each candidate file; returns (home, workspace, root's, nested's)."""
        home, ws = self.root / "home", self.root / "ws"
        (home / ".claude").mkdir(parents=True)
        (ws / "state").mkdir(parents=True)
        named = {}
        for where, folder in (("root", home), ("nested", home / ".claude")):
            named[where] = ws / "state" / f"{where}-mcp.json"
            named[where].write_text("{}")
            env = {"AG2_MCP_DESCRIPTOR": str(named[where])}
            if where == "root":
                env[C.MCP_LANE_CREDENTIAL_ENV] = json.dumps(["python3", "resolver.py"])
            (folder / ".claude.json").write_text(json.dumps({"mcpServers": {"s-space": {"command": "node", "env": env}}}))
        return home, ws, named["root"], named["nested"]

    def test_the_proxy_descriptor_lookup_reads_the_home_folders_own_file(self):
        home, ws, at_root, _ = self.two_configs()
        with mock.patch.dict(os.environ, {"HOME": str(home), "CLAUDE_HOME": str(home / ".claude")}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(C.mcp_descriptor_path(ws), at_root)

    def test_the_lane_lookup_reads_the_home_folders_own_file(self):
        home, _, at_root, nested = self.two_configs()
        missing = self.root / "no-codex" / "config.toml"
        with mock.patch.dict(os.environ, {"HOME": str(home), "CLAUDE_HOME": str(home / ".claude")}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertTrue(C._proxy_reads_vault_lanes(at_root, {}, None, missing))
            self.assertFalse(C._proxy_reads_vault_lanes(nested, {}, None, missing))

    def test_with_a_config_folder_it_is_that_folders_file(self):
        self.assertEqual(C.claude_config_file({"CLAUDE_CONFIG_DIR": str(self.root)}), self.root / ".claude.json")


class TheCli(Base):
    def cli(self):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "connections.py"), "moved-mcp-servers",
                               "--mcp-config", str(self.config)], capture_output=True, text=True, timeout=60)

    def test_one_line_names_the_entry_the_successor_and_the_remove_command(self):
        before = self.write({"Old Space": {"type": "http", "url": OLD_MCP, "headers": {"Authorization": "Bearer hush"}}})
        r = self.cli()
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            f'MCP server "Old Space" connects to {OLD}, which moved to {NEW} ({NEW_MCP}); it will not connect. '
            f"Remove it with: CLAUDE_CONFIG_DIR={self.root} claude mcp remove --scope user -- 'Old Space'"])
        self.assertNotIn("hush", r.stdout)
        self.assertEqual(self.config.read_bytes(), before, "the config is only read")

    def test_a_config_under_another_file_name_is_warned_about_without_a_command(self):
        self.config = self.root / "custom mcp.json"
        self.write({"Old Space": {"type": "http", "url": OLD_MCP}})
        r = self.cli()
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            f'MCP server "Old Space" connects to {OLD}, which moved to {NEW} ({NEW_MCP}); it will not connect. '
            f'It is in {json.dumps(str(self.config))}; edit that file to remove it.'])
        self.assertNotIn("claude mcp remove", r.stdout)

    def test_the_home_folders_own_file_gets_the_unset_form(self):
        home = self.root / "home"
        home.mkdir()
        self.config = home / ".claude.json"
        self.write({"-x": {"type": "http", "url": OLD_MCP}})
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "connections.py"), "moved-mcp-servers"],
                           capture_output=True, text=True, timeout=60,
                           env={k: v for k, v in {**os.environ, "HOME": str(home)}.items() if k != "CLAUDE_CONFIG_DIR"})
        self.assertEqual((r.returncode, r.stderr), (0, ""), r.stderr)
        self.assertTrue(r.stdout.rstrip("\n").endswith(
            "Remove it with: env -u CLAUDE_CONFIG_DIR claude mcp remove --scope user -- -x"), r.stdout)

    def test_nothing_is_printed_when_no_entry_moved_or_there_is_no_config(self):
        r = self.cli()
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.write({"live": {"type": "http", "url": NEW_MCP}})
        r = self.cli()
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
