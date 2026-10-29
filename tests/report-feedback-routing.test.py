#!/usr/bin/env python3
"""A request to report a bug or a feature reaches the report-feedback skill, the only path to the AG2 team.

2026-09-29, a shared product room: a teammate asked their Sutando to report an issue and nothing reached
Agent Universe (no feedback row, nothing in Slack #product-feedback). The agent asked another Sutando to
"add a row to your master DB" and posted the report as a chat message; shown the exact command, it filed
at once. The capability existed; nothing pointed at it. The skill's description, the only text the runtime
shows before a skill is invoked, fired on "report a bug" / "something's broken, file it" / "I have a
feature request" and never said it was the only path, and the one always-loaded rule about a reported
Sutando problem (CLAUDE.md, Community support routing) said to recommend the Discord.

Run: python3 tests/report-feedback-routing.test.py
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "report-feedback" / "SKILL.md"
TRIGGERS = ("report this bug", "report this issue", "report an issue", "report a bug", "file a bug",
            "log this bug", "submit feedback", "feature request", "tell the team this is broken")
# In a dev room these mean a PR comment or a message, not a feedback row.
NOT_TRIGGERS = ("report this to teammate-a", "report this on the PR")
RULE = ("Asked to report or file a bug or feature about Sutando, AG2 Space or the desktop app, in a DM "
        "or a room, use the `report-feedback` skill, never a chat post or another agent; reply with the "
        "reference id it returns.")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _description() -> str:
    m = re.search(r"^description: (.*)$", SKILL.read_text(encoding="utf-8"), re.M)
    assert m, "SKILL.md has no description line"
    return m.group(1)


def _listed_triggers() -> list[str]:
    m = re.search(r"Use when the owner says (.*?), in a DM, a room or by voice", _description())
    assert m, "the description has no trigger list"
    return re.findall(r'"([^"]+)"', m.group(1))


class TheDescriptionFiresOnPlainAsks(unittest.TestCase):
    def test_every_plain_phrasing_is_a_trigger(self):
        listed = [t.lower() for t in _listed_triggers()]
        for phrase in TRIGGERS:
            self.assertIn(phrase, listed, f"trigger missing: {phrase!r}")

    def test_a_pr_comment_or_a_message_is_not_a_trigger(self):
        listed = [t.lower() for t in _listed_triggers()]
        self.assertNotIn("report this", listed)
        for ask in NOT_TRIGGERS:
            for trigger in listed:
                self.assertFalse(ask.lower().startswith(trigger), f"{ask!r} would fire on {trigger!r}")

    def test_it_says_it_is_the_only_path_and_what_is_not_reporting(self):
        desc = _description()
        self.assertIn("THE way a bug, feature request or feedback about Sutando, AG2 Space or the desktop "
                      "app reaches the AG2 team", desc)
        self.assertIn("the only path to the tracker", desc)
        self.assertIn("Posting it in chat, a room or a DM, or asking another agent to log it, is not "
                      "reporting it.", desc)

    def test_an_ask_in_a_room_counts_and_a_non_owner_is_answered(self):
        desc = _description()
        self.assertIn("in a DM, a room or by voice", desc)
        self.assertIn("A non-owner asking is told to file it through their own report-feedback skill or "
                      "the app's Report a bug button.", desc)
        self.assertNotIn("their own agent", desc)

    def test_it_fits_the_runtime_limit(self):
        self.assertLessEqual(len(_description()), 1024)


class TheSkillBody(unittest.TestCase):
    def setUp(self):
        self.text = _norm(SKILL.read_text(encoding="utf-8"))

    def test_the_only_path_is_stated_with_what_does_not_reach_it(self):
        self.assertIn("**This is the only path that reaches the AG2 team.**", self.text)
        self.assertIn("asking another agent (another Sutando included) to \"add a row\" or pass it on never "
                      "gets there", self.text)
        self.assertIn("reply with the reference id the script prints", self.text)

    def test_a_non_owner_ask_is_answered_never_dropped_or_delegated(self):
        m = re.search(r"### When a non-owner asks (.*?)(?= ## | ### |\Z)", self.text)
        self.assertIsNotNone(m, "the non-owner section is missing")
        section = m.group(1)
        for words in ("This applies to every task whose `access_tier` is not `owner`, a collaborator's "
                      "included.", "Never stay silent and never hand it to another agent.",
                      "file it through your own `report-feedback` skill",
                      "**Report a bug** button in the AG2 Space app (the bug icon in the composer)"):
            self.assertIn(words, section)
        self.assertNotIn("their own agent", section)

    def test_the_access_tier_does_not_claim_collaborators_are_sandboxed(self):
        self.assertNotIn("Non-owner tasks never reach this skill", self.text)
        self.assertIn("AG2 Space Team tasks, broker-attested collaborators included, and a Discord channel's "
                      "listed collaborators run in the owner's core with its normal tools", self.text)
        self.assertIn("Nothing structural stops them from running this script", self.text)

    def test_the_success_line_documents_the_reference(self):
        self.assertIn("`OK: filed <kind> report (<status>). Reference: <id>.`", self.text)


class TheAlwaysLoadedRule(unittest.TestCase):
    def _check(self, name):
        text = _norm((REPO / name).read_text(encoding="utf-8"))
        self.assertIn(_norm(RULE), text, f"{name}: the report-feedback rule is missing")
        section = text[text.index("## Community support routing"):text.index("## Pending decisions")]
        self.assertIn("report-feedback", section, f"{name}: the rule lives in Community support routing")
        self.assertIn("https://discord.gg/uZHWXXmrCS", section, f"{name}: the Discord advice is kept")

    def test_claude_md(self):
        self._check("CLAUDE.md")

    def test_agents_md(self):
        self._check("AGENTS.md")


class TheAccessPolicy(unittest.TestCase):
    def test_the_non_owner_answer_is_in_the_policy_the_core_loads(self):
        text = _norm((REPO / "docs" / "access-control.md").read_text(encoding="utf-8"))
        self.assertIn("## A non-owner asking to report a bug", text)
        self.assertIn("never hand it to another agent", text)
        self.assertIn("file it through your own `report-feedback` skill", text)
        self.assertIn("**Report a bug** button", text)
        self.assertNotIn("their own agent", text)



class TheRoomConventions(unittest.TestCase):
    def test_room_guidance_names_the_skill_and_the_non_owner_answer(self):
        text = _norm((REPO / "skills" / "agent-room-ops" / "SKILL.md").read_text(encoding="utf-8"))
        section = text[text.index("**Bug and feature reports**"):text.index("**Errors & retries**")]
        self.assertIn("never reaches the AG2 team", section)
        self.assertIn("the `report-feedback` skill, the only path", section)
        self.assertIn("file it through their own `report-feedback` skill or the app's **Report a bug** "
                      "button", section)

if __name__ == "__main__":
    unittest.main(verbosity=2)
