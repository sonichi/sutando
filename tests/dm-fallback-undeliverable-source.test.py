#!/usr/bin/env python3
"""A source in neither source set has no consumer, so skipping its result loses
the reply permanently. Rationale and evidence live in the PR."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The bridge resolves channel config at MODULE level, so isolation has to happen
# before exec_module or the import reads the operator's real allowlist.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-dmfallback-")
_CFG = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_CFG.mkdir(parents=True, exist_ok=True)
(_CFG / "access.json").write_text('{"allowFrom": []}')
# The module `exit(1)`s at import when no token resolves, so CI (which has none)
# cannot import it without this. Config isolation alone is not sufficient.
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")


def _load():
    spec = importlib.util.spec_from_file_location("db", ROOT / "src" / "discord-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["db"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestUndeliverableSource(unittest.TestCase):
    def setUp(self):
        self.db = _load()
        self.tmp = Path(tempfile.mkdtemp())
        self.tasks = self.tmp / "tasks"
        self.tasks.mkdir()
        self.db.TASKS_DIR = self.tasks

    def _task(self, tid, source, channel_id="1509379092116672602"):
        body = f"id: {tid}\nsource: {source}\n"
        if channel_id is not None:
            body += f"channel_id: {channel_id}\n"
        (self.tasks / f"{tid}.txt").write_text(body + "task: b\n")
        return tid

    def test_consumerless_source_with_channel_is_flagged(self):
        tid = self._task("task-newsradar", "news-radar")
        self.assertFalse(self.db._dm_fallback_eligible(tid))
        self.assertEqual(self.db._orphan_channel_target(tid), 1509379092116672602)

    def test_delivery_owning_source_is_NOT_flagged(self):
        # Control: discord owns its own consumer, so it must not be claimed here.
        tid = self._task("task-discordsrc", "discord")
        self.assertIsNone(self.db._orphan_channel_target(tid))

    def test_dm_eligible_source_is_NOT_flagged(self):
        # Control: voice is handled by the DM path, not this one.
        tid = self._task("task-voicesrc", "voice")
        self.assertIsNone(self.db._orphan_channel_target(tid))

    def test_consumerless_source_without_channel_fails_closed(self):
        tid = self._task("task-nochan", "news-radar", channel_id=None)
        self.assertIsNone(self.db._orphan_channel_target(tid))

    def test_missing_task_file_fails_closed(self):
        self.assertIsNone(self.db._orphan_channel_target("task-absent"))

    def test_warning_is_one_shot_across_repeated_polls(self):
        # poll_dm_fallback rescans every 30s; an unguarded print would emit
        # ~2880 identical lines/day per orphan. Two iterations, one signal.
        tid = self._task("task-repeat", "news-radar")
        self.assertTrue(self.db._should_warn_undeliverable(tid))
        self.assertFalse(self.db._should_warn_undeliverable(tid))
        self.assertFalse(self.db._should_warn_undeliverable(tid))

    def test_distinct_orphans_each_warn_once(self):
        # Control: one-shot must be PER TASK, not a global latch that silences
        # every later orphan after the first.
        a = self._task("task-orphan-a", "news-radar")
        b = self._task("task-orphan-b", "news-radar")
        self.assertTrue(self.db._should_warn_undeliverable(a))
        self.assertTrue(self.db._should_warn_undeliverable(b))
        self.assertFalse(self.db._should_warn_undeliverable(a))
        self.assertFalse(self.db._should_warn_undeliverable(b))

    def test_channel_id_resolves_from_processed_dir(self):
        # Artifact named this branch uncovered: a task already moved to
        # processed/ must still resolve its channel.
        tid = "task-inprocessed"
        proc = self.tasks / "processed"
        proc.mkdir(parents=True, exist_ok=True)
        (proc / f"{tid}.txt").write_text(f"id: {tid}\nsource: news-radar\nchannel_id: 4242\n")
        self.assertEqual(self.db._task_channel_id(tid), 4242)
        self.assertEqual(self.db._orphan_channel_target(tid), 4242)

    def test_channel_id_resolves_from_archive_dir(self):
        tid = "task-inarchive"
        arch = self.tasks / "archive" / "2026-08-07"
        arch.mkdir(parents=True, exist_ok=True)
        (arch / f"{tid}.txt").write_text(f"id: {tid}\nsource: news-radar\nchannel_id: 7171\n")
        self.assertEqual(self.db._task_channel_id(tid), 7171)

    def test_unreadable_task_file_returns_none(self):
        # The OSError arm: a directory where a file is expected raises on read.
        tid = "task-unreadable"
        (self.tasks / f"{tid}.txt").mkdir()
        self.assertIsNone(self.db._task_channel_id(tid))

    def test_non_numeric_channel_id_returns_none(self):
        tid = self._task("task-badchan", "news-radar", channel_id="not-a-number")
        self.assertIsNone(self.db._task_channel_id(tid))

    # The COMPOSITION poll_dm_fallback calls. Testing the helpers alone never
    # exercises how they combine, which is where ordering bugs live.

    def test_composition_emits_once_then_none(self):
        tid = self._task("task-compose", "news-radar")
        first = self.db._undeliverable_warning_for(tid, "task-compose.txt")
        self.assertIsNotNone(first)
        self.assertIn("1509379092116672602", first)
        self.assertIn("UNDELIVERABLE", first)
        self.assertIn("news-radar", first)
        self.assertIsNone(self.db._undeliverable_warning_for(tid, "task-compose.txt"))

    def test_composition_returns_none_for_delivery_owning_source(self):
        tid = self._task("task-compose-discord", "discord")
        self.assertIsNone(self.db._undeliverable_warning_for(tid, "x.txt"))

    def test_composition_returns_none_without_channel(self):
        tid = self._task("task-compose-nochan", "news-radar", channel_id=None)
        self.assertIsNone(self.db._undeliverable_warning_for(tid, "x.txt"))

    def test_composition_does_not_consume_the_one_shot_when_not_eligible(self):
        # Order matters: an ineligible task must NOT burn its one-shot slot, or a
        # source later added to neither set would be silently pre-silenced.
        tid = self._task("task-order", "discord")
        self.assertIsNone(self.db._undeliverable_warning_for(tid, "x.txt"))
        self.assertTrue(self.db._should_warn_undeliverable(tid))

    def test_non_task_prefix_is_ignored(self):
        self._task("question-x", "news-radar")
        self.assertIsNone(self.db._orphan_channel_target("question-x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
