#!/usr/bin/env python3
"""A Claude Code MCP entry that connects straight to a server that moved is named, never edited.

`connections.moved_mcp_servers` lists the config's `url` entries on the MCP endpoint of a server
with a successor; `scripts/connections.py moved-mcp-servers` prints one line per entry with the
successor and the remove command. A proxy entry, an entry on a live server and an unreadable
config list nothing.

Run: python3 tests/connections-moved-mcp-servers.test.py
"""
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
        import os
        from unittest import mock
        self.write({"Old Space": {"type": "http", "url": OLD_MCP}})
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)}):
            self.assertEqual([e["name"] for e in C.moved_mcp_servers()], ["Old Space"])


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
            'Remove it with: claude mcp remove --scope user "Old Space"'])
        self.assertNotIn("hush", r.stdout)
        self.assertEqual(self.config.read_bytes(), before, "the config is only read")

    def test_nothing_is_printed_when_no_entry_moved_or_there_is_no_config(self):
        r = self.cli()
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.write({"live": {"type": "http", "url": NEW_MCP}})
        r = self.cli()
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
