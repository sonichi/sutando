#!/usr/bin/env python3
"""src/chat_bridges.py: which chat-app bridges are set up, skipped on this host, and running.

- A bridge is listed only when its channel directory holds a `.env` or an `access.json`.
- `SKIP_<CHANNEL>=1` in the environment or in the main `.env` leaves it out.
- `running` is the probe's answer, and None when the probe itself failed.

Run: python3 tests/chat-bridges.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import chat_bridges  # noqa: E402


class States(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.channels = self.root / "channels"
        self.env = self.root / ".env"
        clean = {k: v for k, v in os.environ.items() if not k.startswith("SKIP_")}
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_up(self, channel, name=".env"):
        (self.channels / channel).mkdir(parents=True, exist_ok=True)
        (self.channels / channel / name).write_text("")

    def test_only_set_up_bridges_are_listed_with_their_probe_answer(self):
        self.set_up("telegram")
        self.set_up("slack", "access.json")
        answers = {"telegram-bridge": ([12], True), "slack-bridge": ([], False)}
        got = chat_bridges.states(self.channels, self.env, probe_fn=answers.__getitem__)
        self.assertEqual(got, [{"channel": "telegram", "name": "Telegram", "running": True},
                               {"channel": "slack", "name": "Slack", "running": None}])

    def test_a_skip_in_the_environment_or_the_main_env_leaves_a_bridge_out(self):
        for channel in ("telegram", "discord", "slack"):
            self.set_up(channel)
        self.env.write_text("# a comment\n\nSKIP_DISCORD='1'\nSKIP_SLACK=0\nnot a pair\n")
        with mock.patch.dict(os.environ, {"SKIP_TELEGRAM": "1"}):
            got = chat_bridges.states(self.channels, self.env, probe_fn=lambda p: ([], True))
        self.assertEqual(got, [{"channel": "slack", "name": "Slack", "running": False}])

    def test_an_unreadable_main_env_skips_nothing(self):
        self.env.mkdir()
        self.assertFalse(chat_bridges.skipped("telegram", self.env))

    def test_the_default_probe_matches_the_bridge_script(self):
        seen = []
        fake = mock.Mock(probe_pids=lambda pattern: seen.append(pattern) or ([7], True))
        with mock.patch.dict(sys.modules, {"sutando_platform": fake}):
            self.assertEqual(chat_bridges.probe("discord-bridge"), ([7], True))
        self.assertEqual(seen, ["discord-bridge\\.py$"])


if __name__ == "__main__":
    unittest.main()
