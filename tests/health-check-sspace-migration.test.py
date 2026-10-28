#!/usr/bin/env python3
"""health-check's sspace-migration probe: warns while every connection row is on ag2.space and
none on s.space, with the real add/promote commands; quiet once an s.space row exists."""

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

import importlib.util
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(hc)
except SystemExit:
    pass

import connections  # noqa: E402

cli_spec = importlib.util.spec_from_file_location("connections_cli", REPO / "scripts" / "connections.py")
cli = importlib.util.module_from_spec(cli_spec)
cli_spec.loader.exec_module(cli)


def _row(server, instance, primary=False):
    return connections._row(server=server, instance=instance, gateway_url=f"https://chat.{server}",
                            token_ref=connections.token_ref_for(instance) if instance else "REMOTE_TASK_TOKEN",
                            primary=primary, added_by="env", verified=False, server_source="resolver")


class SspaceMigrationProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "state").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rows):
        connections._write(connections.record_path(self.ws),
                           {"rows": rows, connections.REMOVED_KEY: [], connections.LEGACY_PARKED_KEY: False})

    def test_warns_when_only_ag2_space(self):
        self.write([_row("ag2.space", "", primary=True)])
        before = connections.record_path(self.ws).read_bytes()
        out = hc.check_sspace_migration(self.ws)
        self.assertEqual(out["name"], "sspace-migration")
        self.assertEqual(out["status"], "warn")
        self.assertIn("python3 scripts/connections.py add '<code>'", out["detail"])
        self.assertIn("python3 scripts/connections.py promote s-space --repo . --logs-dir", out["detail"])
        self.assertEqual(connections.record_path(self.ws).read_bytes(), before, "the probe never writes")

    def test_commands_parse_with_the_real_cli(self):
        self.write([_row("ag2.space", "", primary=True)])
        detail = hc.check_sspace_migration(self.ws)["detail"]
        for line in re.findall(r"python3 scripts/connections\.py ([^&(]+?)(?: &&| \(|$)", detail):
            argv = shlex.split(line.replace('"$(bash scripts/sutando-config.sh workspace)/logs"', "/w/logs"))
            args = cli.build_parser().parse_args(argv)
            self.assertIn(args.verb, ("add", "promote"))
            if args.verb == "promote":
                self.assertEqual(args.instance, connections.instance_for("s.space"))
                self.assertIsInstance(args.logs_dir, Path)

    def test_warns_for_provisional_gateway_host_row(self):
        self.write([_row("chat.ag2.space", "ag2-space", primary=True)])
        self.assertEqual(hc.check_sspace_migration(self.ws)["status"], "warn")

    def test_ok_when_sspace_row_exists(self):
        self.write([_row("ag2.space", "", primary=True), _row("s.space", "s-space")])
        self.assertIsNone(hc.check_sspace_migration(self.ws))

    def test_ok_when_another_server_is_connected(self):
        self.write([_row("ag2.space", "", primary=True), _row("dev.ag2.space", "dev-ag2-space")])
        self.assertIsNone(hc.check_sspace_migration(self.ws))

    def test_ok_with_no_record_or_unreadable_record(self):
        self.assertIsNone(hc.check_sspace_migration(self.ws))
        self.assertFalse(connections.record_path(self.ws).exists(), "no import on an absent record")
        connections.record_path(self.ws).write_text("{not json")
        self.assertIsNone(hc.check_sspace_migration(self.ws))

    def test_doc_section_names_the_same_commands(self):
        doc = (REPO / "docs" / "connections.md").read_text()
        section = doc.split("### Moving a terminal-run core to s.space", 1)[1].split("\n## ", 1)[0]
        self.assertIn("python3 scripts/connections.py add '<code>'", section)
        self.assertIn("python3 scripts/connections.py promote s-space --repo . --logs-dir", section)


if __name__ == "__main__":
    unittest.main()
