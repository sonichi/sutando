#!/usr/bin/env python3
"""Adoption golden for the codex scheduler (consumer #2 of the write side).

The expected strings below are the EXACT bytes the pre-adoption f-string
producer emitted for these inputs, captured at the parent commit. The
adopted serialize_task_last path must reproduce them byte-for-byte.
"""
import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

SLOT = datetime(2026, 8, 7, 6, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 7, 6, 1, 30, tzinfo=timezone.utc)


def _load():
    spec = importlib.util.spec_from_file_location(
        "cs", REPO / "skills/schedule-crons/scripts/codex-scheduler.py")
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    return cs


class TestSchedulerWriterGolden(unittest.TestCase):
    def test_proactive_delivery_bytes_unchanged(self):
        cs = _load()
        tid, body = cs._task_body(
            Path("/ws"),
            {"name": "morning-briefing", "prompt_skill": "morning-briefing",
             "delivery": "proactive"},
            SLOT, NOW)
        self.assertEqual(tid, "task-cron-morning-briefing-1786082400")
        self.assertEqual(body, (
            "id: task-cron-morning-briefing-1786082400\n"
            "timestamp: 2026-08-07T06:01:30Z\n"
            "source: cron\n"
            "interaction_type: system_event\n"
            "access_tier: owner\n"
            "priority: low\n"
            "schedule_name: morning-briefing\n"
            "schedule_slot: 2026-08-07T06:00:00Z\n"
            "task: /morning-briefing Write the concise owner-facing result to "
            "/ws/results/proactive-morning-briefing-1786082400.txt, then write "
            "[no-send] to /ws/results/task-cron-morning-briefing-1786082400.txt "
            "so this scheduled task is archived without a duplicate reply.\n"))

    def test_silent_retry_bytes_unchanged(self):
        cs = _load()
        tid, body = cs._task_body(
            Path("/ws"),
            {"name": "sync", "prompt": "Run sync now", "_silent_result": True},
            SLOT, NOW, attempt=2)
        self.assertEqual(tid, "task-cron-sync-1786082400-a2")
        self.assertEqual(body, (
            "id: task-cron-sync-1786082400-a2\n"
            "timestamp: 2026-08-07T06:01:30Z\n"
            "source: cron\n"
            "interaction_type: system_event\n"
            "access_tier: owner\n"
            "priority: low\n"
            "schedule_name: sync\n"
            "schedule_slot: 2026-08-07T06:00:00Z\n"
            "task: Run sync now When the pass is complete, write [no-send] to "
            "/ws/results/task-cron-sync-1786082400-a2.txt so the scheduler "
            "records completion without messaging the owner.\n"))

    def test_scheduler_headers_parse_as_headers(self):
        # The vocabulary addition means the scheduler's own stamps are now
        # promoted (previously they fell into the body on task-last parses).
        from local_task_protocol import parse_task_headers
        cs = _load()
        _, body = cs._task_body(
            Path("/ws"), {"name": "sync", "prompt": "x"}, SLOT, NOW)
        parsed = parse_task_headers(body)
        self.assertEqual(parsed.headers["schedule_name"], "sync")
        self.assertEqual(parsed.headers["schedule_slot"], "2026-08-07T06:00:00Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
