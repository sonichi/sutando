#!/usr/bin/env python3
"""A source owning no consumer must be classified, not silently skipped.

`poll_dm_fallback` skips a `task-` result whose source is not DM-eligible, on
the stated grounds that "its own consumer" will drain it. A source in neither
DM_FALLBACK_SOURCES nor DELIVERY_OWNING_SOURCES has no consumer, so that skip
loses the reply permanently.
"""
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

    def test_non_task_prefix_is_ignored(self):
        self._task("question-x", "news-radar")
        self.assertIsNone(self.db._orphan_channel_target("question-x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
