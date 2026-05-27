#!/usr/bin/env python3
"""Tests for outbox_log recipient_label plumbing in discord-bridge.py.

PR #1078: three outbox_log.append() callsites now derive a human-readable
recipient_label ("Chi DM", "#dev", "DM") so the outbox audit log shows
channel names instead of raw numeric IDs.

Covers the label-derivation logic extracted from each callsite:
  a) poll_results DM channel: "Chi DM" when recipient.name is set
  b) poll_results DM channel: "DM" when recipient.name is None
  c) poll_results guild channel: "#dev" from channel.name
  d) poll_results guild channel: None when channel.name is None
  e) poll_proactive owner DM: "Chi DM" from user.name
  f) poll_proactive owner DM: None when user.name is None
  g) poll_dm_fallback channel: "#general" from target_channel.name
  h) poll_dm_fallback channel: None when target_channel.name is None

Run: python3 tests/discord-bridge-outbox-recipient-label.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import unittest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Replicate the label-derivation logic from each callsite (pure, no imports)
# ---------------------------------------------------------------------------

def _label_for_poll_results(channel) -> str | None:
    """Mirrors the poll_results label block."""
    import discord  # type: ignore[import] — not imported, using MagicMock
    if isinstance(channel, _FakeDM):
        _recipient = getattr(channel.recipient, "name", None)
        return f"{_recipient} DM" if _recipient else "DM"
    else:
        _ch_name = getattr(channel, "name", None)
        return f"#{_ch_name}" if _ch_name else None


def _label_for_poll_proactive(user) -> str | None:
    """Mirrors the poll_proactive label block."""
    _user_name = getattr(user, "name", None)
    return f"{_user_name} DM" if _user_name else None


def _label_for_poll_dm_fallback(target_channel) -> str | None:
    """Mirrors the poll_dm_fallback label block."""
    _ch_name = getattr(target_channel, "name", None)
    return f"#{_ch_name}" if _ch_name else None


class _FakeDM:
    """Sentinel class to stand in for discord.DMChannel in isinstance checks."""
    def __init__(self, recipient_name=None):
        self.recipient = MagicMock()
        self.recipient.name = recipient_name
        self.id = 12345


class _FakeChannel:
    """Stands in for discord.TextChannel."""
    def __init__(self, name=None):
        self.name = name
        self.id = 67890


class TestPollResultsLabels(unittest.TestCase):
    def test_a_dm_with_recipient_name(self):
        ch = _FakeDM(recipient_name="Chi")
        self.assertEqual(_label_for_poll_results(ch), "Chi DM")

    def test_b_dm_without_recipient_name(self):
        ch = _FakeDM(recipient_name=None)
        self.assertEqual(_label_for_poll_results(ch), "DM")

    def test_c_guild_channel_with_name(self):
        ch = _FakeChannel(name="dev")
        self.assertEqual(_label_for_poll_results(ch), "#dev")

    def test_d_guild_channel_without_name(self):
        ch = _FakeChannel(name=None)
        self.assertIsNone(_label_for_poll_results(ch))


class TestPollProactiveLabels(unittest.TestCase):
    def test_e_user_with_name(self):
        user = MagicMock()
        user.name = "Chi"
        self.assertEqual(_label_for_poll_proactive(user), "Chi DM")

    def test_f_user_without_name(self):
        user = MagicMock(spec=[])  # no 'name' attribute
        self.assertIsNone(_label_for_poll_proactive(user))


class TestPollDmFallbackLabels(unittest.TestCase):
    def test_g_channel_with_name(self):
        ch = _FakeChannel(name="general")
        self.assertEqual(_label_for_poll_dm_fallback(ch), "#general")

    def test_h_channel_without_name(self):
        ch = _FakeChannel(name=None)
        self.assertIsNone(_label_for_poll_dm_fallback(ch))


class TestCodeContainsRecipientLabel(unittest.TestCase):
    """Regression guard: ensure all three callsites pass recipient_label."""

    def setUp(self):
        from pathlib import Path
        self.src = (Path(__file__).parent.parent / "src" / "discord-bridge.py").read_text()

    def test_recipient_label_in_poll_results(self):
        # At least one outbox_log.append in poll_results must pass recipient_label
        self.assertIn("recipient_label=_label", self.src)

    def test_three_recipient_label_callsites(self):
        count = self.src.count("recipient_label=_label")
        self.assertGreaterEqual(count, 3, f"Expected 3 recipient_label= callsites, found {count}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
