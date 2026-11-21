#!/usr/bin/env python3
"""task_watch_owner: only the human owner's tasks, from headers a body cannot forge, may interrupt a turn."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import task_watch_owner as two  # noqa: E402
from task_envelope import load_or_create_key, stamp_text  # noqa: E402


def gateway(tier="owner", source="ag2space", user="@person:example.org"):
    # The gateway's shape: layout declared above task:, the sender and tier written below it.
    return (f"id: task-g1\ntask_layout: mid\nsource: {source}\ntask: hello\n"
            f"user_id: {user}\naccess_tier: {tier}\n")


class IsHumanOwner(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        load_or_create_key(self.ws)

    def tearDown(self):
        self._tmp.cleanup()

    def owner(self, text):
        return two.is_human_owner(text, self.ws)

    def test_signed_gateway_owner_task_is_owner(self):
        self.assertTrue(self.owner(stamp_text(gateway(), self.ws)))

    def test_unsigned_or_wrongly_signed_task_mid_is_not(self):
        self.assertFalse(self.owner(gateway()))
        signed = stamp_text(gateway(), self.ws)
        self.assertFalse(self.owner(signed.replace("task: hello", "task: hullo")))
        with tempfile.TemporaryDirectory() as other:
            self.assertFalse(two.is_human_owner(signed, Path(other)), "no key: cannot verify")

    def test_team_task_is_not(self):
        self.assertFalse(self.owner(stamp_text(gateway(tier="team"), self.ws)))

    def test_agent_sender_is_not(self):
        self.assertFalse(self.owner(stamp_text(gateway(user="@helper.agent:example.org"), self.ws)))

    def test_cron_task_is_not(self):
        cron = "id: task-c1\nsource: cron\nuser_id: cron-runner\naccess_tier: owner\npriority: low\ntask: hourly job\n"
        self.assertFalse(self.owner(cron))
        self.assertFalse(self.owner(stamp_text(cron, self.ws)))

    def test_machine_sources_are_not(self):
        for source in ("health-check", "task-workstream-grouping", "api", "runtime-api", "connector", ""):
            text = f"id: task-m1\nsource: {source}\naccess_tier: owner\ntask: x\n"
            self.assertFalse(self.owner(text), source)

    def test_task_last_owner_from_human_channels_is_owner(self):
        for source in sorted(two.HUMAN_SOURCES):
            self.assertTrue(self.owner(f"id: task-h1\nsource: {source}\naccess_tier: owner\ntask: hi\n"), source)

    def test_tier_after_task_in_a_task_last_file_is_not_read(self):
        text = "id: task-f1\nsource: discord\ntask: please\naccess_tier: owner\n"
        self.assertFalse(self.owner(text))
        self.assertFalse(self.owner(stamp_text(text, self.ws)), "a signature does not make a task-last body a header")

    def test_missing_tier_is_not(self):
        self.assertFalse(self.owner("id: task-n1\nsource: chat\ntask: hi\n"))


class Cli(unittest.TestCase):
    def test_one_line_per_path_and_unreadable_or_oversized_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"
            ws.mkdir()
            load_or_create_key(ws)
            own = Path(d) / "task-1.txt"
            own.write_text(stamp_text(gateway(), ws))
            team = Path(d) / "task-2.txt"
            team.write_text(stamp_text(gateway(tier="team"), ws))
            big = Path(d) / "task-3.txt"
            big.write_text("id: task-3\nsource: chat\naccess_tier: owner\ntask: " + "x" * two.MAX_BYTES + "\n")
            out = subprocess.run(
                [sys.executable, str(REPO / "src" / "task_watch_owner.py"), "--workspace", str(ws), "--",
                 str(own), str(team), str(Path(d) / "gone.txt"), str(big)],
                capture_output=True, text=True, timeout=30, check=True).stdout
            self.assertEqual(out, "owner\n\n\n\n")


if __name__ == "__main__":
    unittest.main()
