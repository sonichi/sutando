#!/usr/bin/env python3
"""Tests for issue #1120 fix — join_trigger passes reply-channel/reply-user
to the voice server so startup refusals go to the originating channel, not
the owner's DMs via proactive-*.txt.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).parent.parent
_SCRIPT = REPO_ROOT / "skills" / "discord-voice" / "scripts" / "join_trigger.py"

# Load join_trigger without executing top-level subprocess / file-read calls.
_spec = importlib.util.spec_from_file_location("join_trigger", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["join_trigger"] = _mod
_spec.loader.exec_module(_mod)

_spawn_voice_server = _mod._spawn_voice_server
handle_join_trigger = _mod.handle_join_trigger


def _make_message(channel_id="111", user_id="222", voice_channel=None):
    """Construct a minimal mock discord.Message."""
    msg = MagicMock()
    msg.channel.id = channel_id
    msg.author.id = user_id
    # guild member's voice.channel (the voice room the owner is already in)
    member = MagicMock()
    if voice_channel is None:
        voice_channel = MagicMock()
        voice_channel.id = "vc-999"
        voice_channel.name = "General"
        voice_channel.guild.id = "guild-1"
    member.voice.channel = voice_channel
    msg.guild.get_member.return_value = member
    msg.author.id = user_id
    return msg


class TestSpawnVoiceServerArgs(unittest.TestCase):
    """_spawn_voice_server must pass --reply-channel and --reply-user when given."""

    def _captured_argv(self, guild_id, channel_id, reply_ch=None, reply_user=None):
        captured = {}

        def fake_popen(argv, **kwargs):
            captured["argv"] = argv
            raise OSError("fake — don't actually spawn")

        with patch("subprocess.Popen", side_effect=fake_popen):
            _spawn_voice_server(guild_id, channel_id, reply_ch, reply_user)
        return captured.get("argv", [])

    def test_no_reply_ids_no_extra_flags(self):
        argv = self._captured_argv("g1", "ch1")
        self.assertNotIn("--reply-channel", argv)
        self.assertNotIn("--reply-user", argv)

    def test_reply_channel_added(self):
        argv = self._captured_argv("g1", "ch1", reply_ch="TEXT_CH")
        idx = argv.index("--reply-channel")
        self.assertEqual(argv[idx + 1], "TEXT_CH")

    def test_reply_user_added(self):
        argv = self._captured_argv("g1", "ch1", reply_user="USER_ID")
        idx = argv.index("--reply-user")
        self.assertEqual(argv[idx + 1], "USER_ID")

    def test_both_ids_added(self):
        argv = self._captured_argv("g1", "ch1", reply_ch="TC", reply_user="UID")
        self.assertIn("--reply-channel", argv)
        self.assertIn("--reply-user", argv)

    def test_guild_and_channel_still_present(self):
        argv = self._captured_argv("g1", "ch1", reply_ch="TC")
        self.assertIn("--guild", argv)
        self.assertIn("--channel", argv)
        self.assertEqual(argv[argv.index("--guild") + 1], "g1")
        self.assertEqual(argv[argv.index("--channel") + 1], "ch1")

    def test_none_ids_not_added(self):
        argv = self._captured_argv("g1", "ch1", reply_ch=None, reply_user=None)
        self.assertNotIn("--reply-channel", argv)
        self.assertNotIn("--reply-user", argv)


class TestHandleJoinTriggerThreadsReplyContext(unittest.TestCase):
    """handle_join_trigger() must extract reply_channel_id/reply_user_id from
    the message and pass them to _spawn_voice_server."""

    def test_reply_channel_forwarded_to_spawn(self):
        msg = _make_message(channel_id="CH123", user_id="USR456")
        spawn_calls = []

        def fake_spawn(guild_id, channel_id, reply_channel_id=None, reply_user_id=None):
            spawn_calls.append({
                "guild_id": guild_id,
                "channel_id": channel_id,
                "reply_channel_id": reply_channel_id,
                "reply_user_id": reply_user_id,
            })
            return True

        with patch.object(_mod, "_spawn_voice_server", fake_spawn), \
             patch.object(_mod, "_server_already_running", return_value=False), \
             patch.object(_mod, "_enqueue_context_prep_task"):
            handle_join_trigger(msg)

        self.assertEqual(len(spawn_calls), 1)
        self.assertEqual(spawn_calls[0]["reply_channel_id"], "CH123")
        self.assertEqual(spawn_calls[0]["reply_user_id"], "USR456")

    def test_reply_channel_not_forwarded_when_server_already_running(self):
        """When the server is already running, spawn is never called — no leak."""
        msg = _make_message(channel_id="CH123", user_id="USR456")
        spawn_calls = []

        def fake_spawn(*args, **kwargs):
            spawn_calls.append(kwargs)
            return True

        with patch.object(_mod, "_spawn_voice_server", fake_spawn), \
             patch.object(_mod, "_server_already_running", return_value=True):
            result = handle_join_trigger(msg)

        self.assertEqual(spawn_calls, [])
        self.assertIn("already in", result)

    def test_no_channel_attr_does_not_raise(self):
        """If message.channel has no id, reply_channel_id defaults to None."""
        msg = MagicMock()
        del msg.channel.id  # channel exists but has no .id
        msg.author.id = "USR"
        vc = MagicMock()
        vc.id = "vc"
        vc.name = "General"
        vc.guild.id = "g"
        msg.guild.get_member.return_value.voice.channel = vc

        spawn_calls = []

        def fake_spawn(guild_id, channel_id, reply_channel_id=None, reply_user_id=None):
            spawn_calls.append(reply_channel_id)
            return True

        with patch.object(_mod, "_spawn_voice_server", fake_spawn), \
             patch.object(_mod, "_server_already_running", return_value=False), \
             patch.object(_mod, "_enqueue_context_prep_task"):
            handle_join_trigger(msg)

        self.assertEqual(spawn_calls, [None])


if __name__ == "__main__":
    unittest.main()
