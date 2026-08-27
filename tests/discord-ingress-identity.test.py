#!/usr/bin/env python3
"""Slice 3 ingress: injective provider-derived task ids + durable replay skip.

Run: python3 tests/discord-ingress-identity.test.py   (stdlib only)
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ingress_identity import already_admitted, provider_task_id  # noqa: E402


class ProviderTaskId(unittest.TestCase):
    def test_same_event_same_id_across_calls(self):
        a = provider_task_id("dc1504316176686120980", "1541010944446963782")
        b = provider_task_id("dc1504316176686120980", "1541010944446963782")
        self.assertEqual(a, b)
        self.assertEqual(a, "task-dc1504316176686120980~1541010944446963782")

    def test_id_stays_in_the_pool_claim_charset(self):
        tid = provider_task_id("dc123", "456")
        self.assertRegex(tid, r"^task-[A-Za-z0-9._~-]+$")

    def test_distinct_events_distinct_ids(self):
        self.assertNotEqual(provider_task_id("dc1", "2"),
                            provider_task_id("dc1", "3"))
        self.assertNotEqual(provider_task_id("dc1~2", "3"),
                            provider_task_id("dc1", "2~3"))


class AlreadyAdmitted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tasks = root / "tasks"
        self.results = root / "results"
        self.tasks.mkdir()
        self.results.mkdir()
        self.tid = provider_task_id("dc1", "42")

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_event_is_not_admitted(self):
        self.assertFalse(already_admitted(self.tid, self.tasks, self.results))

    def test_pending_claimed_resulted_and_archived_all_count(self):
        (self.tasks / f"{self.tid}.txt").write_text("x")
        self.assertTrue(already_admitted(self.tid, self.tasks, self.results))
        (self.tasks / f"{self.tid}.txt").rename(
            self.tasks / f"{self.tid}.claimed-core-2.txt")
        self.assertTrue(already_admitted(self.tid, self.tasks, self.results))
        (self.tasks / f"{self.tid}.claimed-core-2.txt").unlink()
        (self.results / f"{self.tid}.txt").write_text("done")
        self.assertTrue(already_admitted(self.tid, self.tasks, self.results))
        (self.results / f"{self.tid}.txt").unlink()
        self.assertTrue(already_admitted(
            self.tid, self.tasks, self.results, lambda tid: True))

    def test_a_different_event_is_not_shadowed(self):
        (self.tasks / f"{self.tid}.txt").write_text("x")
        other = provider_task_id("dc1", "43")
        self.assertFalse(already_admitted(other, self.tasks, self.results))

    def test_a_longer_id_sharing_the_prefix_is_not_a_replay(self):
        # A pending file for a LONGER id must not admit the shorter one: the
        # glob is anchored to the id's "." delimiter, not a bare prefix (john #3316).
        longer = self.tid + "extra"
        (self.tasks / f"{longer}.txt").write_text("x")
        self.assertFalse(already_admitted(self.tid, self.tasks, self.results))
        # positive control: the exact-id file still admits
        (self.tasks / f"{self.tid}.txt").write_text("x")
        self.assertTrue(already_admitted(self.tid, self.tasks, self.results))


class BridgeWiring(unittest.TestCase):
    """One wiring pin per bridge: the mint site delegates to the policy."""

    def test_discord_mint_site_uses_provider_task_id_with_replay_skip(self):
        src = (REPO / "src" / "discord-bridge.py").read_text()
        self.assertIn("from ingress_identity import provider_task_id", src)
        site = re.search(
            r"task_id = provider_task_id\(f\"dc\{_inst\}\", str\(message\.id\)\)"
            r"[\s\S]{0,400}?already_admitted\(task_id, TASKS_DIR, RESULTS_DIR",
            src)
        self.assertIsNotNone(
            site, "discord-bridge DM ingress must derive the id from the "
                  "provider event and consult already_admitted before writing")

    def test_slack_mint_site_uses_provider_task_id_with_replay_skip(self):
        src = (REPO / "src" / "slack-bridge.py").read_text()
        self.assertIn("from ingress_identity import provider_task_id", src)
        site = re.search(
            r"task_id = provider_task_id\(f\"sl\{event\.get\('team'\) or '0'\}\","
            r"[\s\S]{0,400}?already_admitted\(task_id, TASKS_DIR, RESULTS_DIR",
            src)
        self.assertIsNotNone(
            site, "slack-bridge ingress must derive the id from channel+ts "
                  "and consult already_admitted before writing")

    def test_slack_event_id_shape_needs_no_escaping(self):
        tid = provider_task_id("slT08ABC", "D0B4N6DSY90-1787477641.984")
        self.assertNotIn("%", tid)
        self.assertRegex(tid, r"^task-[A-Za-z0-9._~-]+$")


if __name__ == "__main__":
    unittest.main()
