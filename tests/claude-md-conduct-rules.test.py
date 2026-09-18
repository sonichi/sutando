#!/usr/bin/env python3
"""The conduct rules that fix the 2026-09-17 onboarding misses must stay in the always-loaded files
(CLAUDE.md and its generated twin AGENTS.md), with their operative sentences intact: where a reply
goes (closed DM list, web research in the room, the one-line notice), the owner's language,
blockers stated plainly, checking the catalog and the skills before refusing, and the queue line.
Doc-pin pattern of tests/claude-md-tier-summary.test.py."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _norm(text: str) -> str:
    """Whitespace-insensitive: the rule is the sentence, not the line wrap."""
    return re.sub(r"\s+", " ", text)

ROUTING = (
    "Reply where you were asked: a task with `channel_id`/`source_room_id` is answered in that room, "
    "threaded to `source_message_id`.",
    "Web research, listings, shopping, summaries, code: always in the room, however personal the topic.",
    "The DM exception is a closed list: data read from the owner's connected accounts or device "
    "(mail, calendar events, contacts, message history, files from Drive/Dropbox/Notion, credentials, "
    "health or financial records).",
    "post it in the DM and exactly one line in the room: 'I sent it to you in our DM.' Never move silently.",
)
LANGUAGE = (
    "Reply in the language your owner wrote in.",
    "When you deliberately answer in English (a quoted error, a code identifier, a term with no good translation), say so in one clause.",
    "A stated preference is written to `user_profile.md` and kept.",
)
BLOCKERS = (
    "When you cannot finish because of a quota, a missing capability, a site that blocks you or a "
    "permission you lack, say exactly that in one line, name what unblocks it, and stop.",
    "No silent retries, no wording that implies success.",
    "If several requests are pending, say how many are ahead of this one.",
)
SKILLS_FIRST = (
    "Before saying a capability does not exist, check `docs/built-in-tools.md`, the skills directory and the last tool response.",
    "Cloud tools are activated by the `marketplace` skill; a newly activated tool is usable at once through "
    "station_find/station_call unless the script printed RESTART REQUIRED.",
)
QUEUE = (
    "When the `QUEUE:` line (or `activity.py queue`) says more than one task is pending, the first line to "
    "that task's conversation names the position: \"Got it. 2 ahead of this one, working in order.\"",
    "One line per task, in its own conversation, voice included; never narrate the queue anywhere else.",
    "`QUEUE: <n> pending after this`",
    "--source ag2space --channel-id <room>",
)


class ConductRulesArePinned(unittest.TestCase):
    def _check(self, name):
        s = _norm((REPO / name).read_text(encoding="utf-8"))
        for group, sentences in (("routing", ROUTING), ("language", LANGUAGE), ("blockers", BLOCKERS),
                                 ("skills-before-refusing", SKILLS_FIRST), ("queue", QUEUE)):
            for sentence in sentences:
                self.assertIn(_norm(sentence), s, f"{name}: the {group} rule lost: {sentence[:60]!r}")
        self.assertIn("### Where replies go", s, f"{name}: the routing rule is a Task bridge subsection")
        self.assertLess(s.index("## Operating Style"), s.index("Reply in the language your owner wrote in."),
                        f"{name}: language and blockers live under Operating Style")
        self.assertLess(s.index("Reply in the language"), s.index("## Architecture rules"))
        self.assertLess(s.index("## Built-in tools"), s.index("Before saying a capability does not exist"))
        self.assertLess(s.index("Before saying a capability does not exist"), s.index("## Learn from demonstration"))

    def test_claude_md(self):
        self._check("CLAUDE.md")

    def test_agents_md(self):
        self._check("AGENTS.md")

    def test_connect_apps_uses_the_same_closed_list_with_the_web_research_carve_out(self):
        d = (REPO / "skills" / "connect-apps" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("The DM exception is a closed list: data read from the owner's connected accounts or device", d)
        self.assertIn("web research, listings, shopping, summaries, code", d)
        self.assertIn("however personal the topic", d)
        self.assertIn('"I sent it to you in our DM." Never move silently.', d)
        self.assertIn("reply_to=room|dm", d)
        rules = d.split("## Rules")[1]
        self.assertIn("that list is closed", rules)
        self.assertNotIn("Private content (mail, calendar, files, messages, contacts) only", rules,
                         "the old category label must not survive as a rule")

    def test_the_precheck_hook_says_where_the_reply_goes(self):
        h = (REPO / "skills" / "connect-apps" / "hooks" / "connect-precheck.py").read_text(encoding="utf-8")
        self.assertIn('parts.append(f"reply_to={reply_to(fields)}")', h)

    def test_marketplace_activation_is_usable_at_once_and_comes_before_the_report(self):
        m = (REPO / "skills" / "marketplace" / "SKILL.md").read_text(encoding="utf-8")
        use = m.index("**Use it now; restart only when told.**")
        self.assertLess(use, m.index("**Report.**"), "the use-it-now step precedes the report step")
        self.assertIn("**usable at once through `station_find` / `station_call`**", m)
        self.assertIn("never send the owner to a dashboard or Station page to activate", m)


if __name__ == "__main__":
    unittest.main(verbosity=2)
