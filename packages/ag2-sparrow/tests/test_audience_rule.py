"""Every built-in Sparrow prompt carries the audience rule, verbatim from task_io.AUDIENCE_RULE."""
import json
import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import relay_loop, task_io  # noqa: E402
from ag2_sparrow.adapters.codex import (  # noqa: E402
    codex_connector)
from ag2_sparrow.adapters.hatch import (  # noqa: E402
    runtime_text)
from ag2_sparrow.adapters.openclaw import (  # noqa: E402
    openclaw_connector)

# The s.space server serves this same text to agents connected over MCP; the two stay identical.
RULE = (
    "Audience: in any room other than your owner's own DM with you, post only what the room is "
    "meant to read (a reply to its members, or work the room asked for) or what your owner explicitly "
    "asks you to share there. Anything else drawn from your owner's private data (mail, calendar, "
    "contacts, message history, files, account data) goes to your owner's DM, with one line in the room "
    "saying you sent it to your owner's DM, without naming what it was about. That the room is "
    "discussing it, or a member asks for it, does not make your owner's private data shareable there. "
    "The rule is about who reads the room, not where the data came from: if your owner asks you to "
    "post their availability there, post it."
)
TIERS = ({"access_tier": "owner"}, {"access_tier": "team", "collaborator": "true"}, {"access_tier": "guest"})


class AudienceRuleTest(unittest.TestCase):
    def test_the_rule_is_the_servers_text(self):
        self.assertEqual(task_io.AUDIENCE_RULE, RULE)
        self.assertNotIn("`", RULE)

    def test_the_built_in_hatch_prompt_ends_with_the_rule(self):
        prompt = runtime_text.builtin()["prompt"]
        self.assertTrue(prompt.endswith(" " + RULE))
        self.assertEqual(prompt.count(RULE), 1)
        raw = json.loads((PKG_ROOT / "ag2_sparrow/adapters/hatch/builtin-runtime-text.json").read_text())
        self.assertLessEqual(len(raw["prompt"]), runtime_text.MAX_TEXT_CHARS)

    def test_the_codex_prompt_carries_the_rule_for_every_tier(self):
        for headers in TIERS:
            with self.subTest(tier=headers["access_tier"]):
                prompt = codex_connector.wrap_untrusted("task-1", dict(headers, channel_id="!r:x"), "hi")
                self.assertEqual(prompt.count(RULE), 1)
                self.assertLess(prompt.index(RULE), prompt.index("\n<UNTRUSTED_TASK_"))

    def test_the_openclaw_prompt_carries_the_rule_for_every_tier(self):
        for headers in TIERS:
            with self.subTest(tier=headers["access_tier"]):
                prompt = openclaw_connector.wake_prompt(
                    "task-1", dict(headers, channel_id="!r:x"), "hi", [], "/w/results/task-1.txt",
                    "ag2-sparrow-openclaw reply")
                self.assertEqual(prompt.count(RULE), 1)
                self.assertLess(prompt.index(RULE), prompt.index("\n<TASK_BODY_"))

    def test_the_routine_and_session_payload_carries_the_rule_before_the_body(self):
        for headers in TIERS:
            with self.subTest(tier=headers["access_tier"]):
                _, text = relay_loop.build_payload("task-1", dict(headers, channel_id="!r:x"), "hi")
                self.assertIn("\naudience: " + RULE + "\n<UNTRUSTED_TASK_", text)


if __name__ == "__main__":
    unittest.main()
