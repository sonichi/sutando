#!/usr/bin/env python3
"""Tests for proactive-notify channel router + matcher + dedup.

Run: python3 tests/proactive-notify-router.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = ROOT / "skills" / "proactive-notify" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

from channel_router import Ping, route  # noqa: E402
from runner import _matches  # noqa: E402


@dataclass
class FakePresence:
    presenter_mode_active: bool = False
    voice_client_connected: bool = False
    owner_active_in_discord_within_min_5: bool = False
    in_quiet_hours: bool = False


POLICY = {
    "default_channel": {"critical": "call", "important": "sms", "fyi": "queue"},
    "overrides": [
        {"name": "presenter_mode_mutes_call", "if": "presenter_mode_active",
         "behavior": {"critical": "sms", "important": "queue", "fyi": "queue"}},
        {"name": "voice_session_prefers_voice", "if": "voice_client_connected",
         "behavior": {"critical": "voice", "important": "voice", "fyi": "voice"}},
        {"name": "discord_active_meets_owner_there", "if": "owner_active_in_discord_within_min_5",
         "behavior": {"important": "discord_dm", "fyi": "discord_dm"}},
        {"name": "in_quiet_hours_downgrades", "if": "in_quiet_hours",
         "behavior": {"critical": "sms", "important": "queue", "fyi": "queue"}},
    ],
}


def _ping(urgency="important", prefer=None, qh_override=False) -> Ping:
    return Ping(name="t", urgency=urgency, voice_natural=True, body="b", dedup_key="t:1",
                prefer_channel=prefer, quiet_hours_override=qh_override)


class TestRouter(unittest.TestCase):
    def test_defaults_apply_when_no_override_matches(self):
        p = FakePresence()
        self.assertEqual(route(_ping("critical"), p, POLICY), "call")
        self.assertEqual(route(_ping("important"), p, POLICY), "sms")
        self.assertEqual(route(_ping("fyi"), p, POLICY), "queue")

    def test_presenter_mode_mutes_call(self):
        p = FakePresence(presenter_mode_active=True)
        self.assertEqual(route(_ping("critical"), p, POLICY), "sms")
        self.assertEqual(route(_ping("important"), p, POLICY), "queue")

    def test_voice_client_prefers_voice(self):
        p = FakePresence(voice_client_connected=True)
        self.assertEqual(route(_ping("critical"), p, POLICY), "voice")
        self.assertEqual(route(_ping("important"), p, POLICY), "voice")
        self.assertEqual(route(_ping("fyi"), p, POLICY), "voice")

    def test_discord_active_routes_dm(self):
        p = FakePresence(owner_active_in_discord_within_min_5=True)
        self.assertEqual(route(_ping("important"), p, POLICY), "discord_dm")
        self.assertEqual(route(_ping("fyi"), p, POLICY), "discord_dm")
        # critical not in this override's behavior — falls through to default.
        self.assertEqual(route(_ping("critical"), p, POLICY), "call")

    def test_quiet_hours_downgrades_non_critical(self):
        p = FakePresence(in_quiet_hours=True)
        self.assertEqual(route(_ping("important"), p, POLICY), "queue")
        self.assertEqual(route(_ping("fyi"), p, POLICY), "queue")

    def test_quiet_hours_override_lets_critical_call(self):
        p = FakePresence(in_quiet_hours=True)
        self.assertEqual(route(_ping("critical"), p, POLICY), "sms")
        self.assertEqual(route(_ping("critical", qh_override=True), p, POLICY), "call")

    def test_prefer_channel_short_circuits(self):
        p = FakePresence(presenter_mode_active=True)
        self.assertEqual(route(_ping("important", prefer="voice"), p, POLICY), "voice")

    def test_first_override_wins(self):
        p = FakePresence(presenter_mode_active=True, voice_client_connected=True)
        # presenter_mode comes before voice in the policy → presenter wins.
        self.assertEqual(route(_ping("important"), p, POLICY), "queue")


class TestMatcher(unittest.TestCase):
    def test_minutes_until_range(self):
        crit = {"starts_in_min_min": 8, "starts_in_min_max": 12}
        self.assertTrue(_matches({"title": "x", "minutes_until": 10}, crit))
        self.assertFalse(_matches({"title": "x", "minutes_until": 5}, crit))
        self.assertFalse(_matches({"title": "x", "minutes_until": 15}, crit))

    def test_exclude_titles_regex(self):
        crit = {"exclude_titles_matching": "(?i)^(rest|focus|ooo)$"}
        self.assertFalse(_matches({"title": "Rest"}, crit))
        self.assertFalse(_matches({"title": "Focus"}, crit))
        self.assertTrue(_matches({"title": "Standup with Mark"}, crit))

    def test_exclude_description_tag(self):
        crit = {"exclude_description_tag": "[no-reminder]"}
        item = {"title": "X", "description": "Skip this one [no-reminder]"}
        self.assertFalse(_matches(item, crit))


if __name__ == "__main__":
    unittest.main()
