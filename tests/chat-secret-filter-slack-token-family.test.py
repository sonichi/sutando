#!/usr/bin/env python3
"""The Slack token filter covers the whole family, from one shared definition.

User feedback (P1-43): a pasted Slack app-level token (``xapp-1-…``) reached a
task file in plaintext while the bot token beside it was redacted, because the
curated rule was a private ``xox[abps]`` copy. Every token below is built from
obvious placeholders in Slack's published shapes; none is a real credential.

Run: python3 tests/chat-secret-filter-slack-token-family.test.py
"""
import importlib.util
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import chat_secret_filter
import secret_scanner
from chat_secret_filter import filter_chat_secrets

HEX = "0" * 32
# Kinds the old xox[abps] rule missed (each of these fails on the old rule).
NEW_KINDS = {
    "app-level (xapp-)": "xapp-1-A00000000000-0000000000000-" + "a0" * 32,
    "refresh (xoxe-)": "xoxe-1-" + "A0" * 60,
    "config refresh (xoxr-)": "xoxr-" + "0" * 12 + "-" + "a0" * 16,
    "legacy (xoxo-)": "xoxo-0000000000-0000000000-0000000000-" + HEX,
    "rotated user (xoxe.xoxp-)": "xoxe.xoxp-1-" + "A0" * 80,
    "rotated bot (xoxe.xoxb-)": "xoxe.xoxb-1-" + "A0" * 80,
    "browser session (xoxc-)": "xoxc-0000000000-0000000000-0000000000000-" + "a0" * 32,
    "browser cookie (xoxd-)": "xoxd-1" + "A0" * 40,
}
# Kinds the old rule already covered; kept so the family can never shrink.
OLD_KINDS = {
    "bot (xoxb-)": "xoxb-0000000000-0000000000000-" + "a0" * 12,
    "user (xoxp-)": "xoxp-0000000000-0000000000-0000000000-" + HEX,
    "workspace (xoxa-)": "xoxa-2-" + "a0" * 20,
    "session (xoxs-)": "xoxs-0000000000-0000000000-0000000000-" + HEX,
}
PRIVATE_SLACK_RULE = re.compile(r"xox[\[(]|\(xapp|xapp[\[|]|xapp-\\d|xapp-\[")


def _load_report_feedback():
    script = REPO / "skills" / "report-feedback" / "report-feedback.py"
    spec = importlib.util.spec_from_file_location("report_feedback_p1_43", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class TestEveryTokenKindIsRedacted(unittest.TestCase):
    def _assert_redacted_whole(self, kind, token):
        result = filter_chat_secrets(f"here is the {kind} token {token} for socket mode")
        self.assertEqual(
            result.text, f"here is the {kind} token [REDACTED-Slack Token] for socket mode",
            f"{kind}: token not redacted as one span",
        )
        self.assertIn("Slack Token", result.secret_types, kind)

    def test_app_level_token_is_redacted(self):
        self._assert_redacted_whole("app-level (xapp-)", NEW_KINDS["app-level (xapp-)"])

    def test_refresh_token_is_redacted(self):
        self._assert_redacted_whole("refresh (xoxe-)", NEW_KINDS["refresh (xoxe-)"])

    def test_config_refresh_token_is_redacted(self):
        self._assert_redacted_whole("config refresh (xoxr-)", NEW_KINDS["config refresh (xoxr-)"])

    def test_legacy_token_is_redacted(self):
        self._assert_redacted_whole("legacy (xoxo-)", NEW_KINDS["legacy (xoxo-)"])

    def test_rotated_user_token_is_redacted_including_its_xoxe_prefix(self):
        self._assert_redacted_whole("rotated user (xoxe.xoxp-)", NEW_KINDS["rotated user (xoxe.xoxp-)"])

    def test_rotated_bot_token_is_redacted_including_its_xoxe_prefix(self):
        self._assert_redacted_whole("rotated bot (xoxe.xoxb-)", NEW_KINDS["rotated bot (xoxe.xoxb-)"])

    def test_browser_session_token_is_redacted(self):
        self._assert_redacted_whole("browser session (xoxc-)", NEW_KINDS["browser session (xoxc-)"])

    def test_browser_cookie_token_is_redacted(self):
        self._assert_redacted_whole("browser cookie (xoxd-)", NEW_KINDS["browser cookie (xoxd-)"])

    def test_previously_covered_kinds_stay_covered(self):
        for kind, token in OLD_KINDS.items():
            with self.subTest(kind=kind):
                self._assert_redacted_whole(kind, token)

    def test_bot_and_app_token_pasted_together_are_both_redacted(self):
        bot = OLD_KINDS["bot (xoxb-)"]
        app = NEW_KINDS["app-level (xapp-)"]
        result = filter_chat_secrets(f"SLACK_BOT_TOKEN={bot}\nSLACK_APP_TOKEN={app}\nplease wire it up")
        self.assertEqual(
            result.text,
            "SLACK_BOT_TOKEN=[REDACTED-Slack Token]\nSLACK_APP_TOKEN=[REDACTED-Slack Token]\nplease wire it up",
        )
        self.assertNotIn("xapp-", result.text)

    def test_prose_naming_the_prefixes_is_left_alone(self):
        prose = "Socket mode needs both the bot token (xoxb-…) and the app token (xapp-…)."
        result = filter_chat_secrets(prose)
        self.assertEqual(result.text, prose)
        self.assertNotIn("Slack Token", result.secret_types)

    def test_a_sign_off_in_prose_is_not_a_token(self):
        # Review of #4892: a tail of [A-Za-z0-9-]+ swallowed "xoxo-Sam" and
        # reported it as a Slack token; every real kind has a digit after the dash.
        prose = "thanks for the fix, hugs xoxo-Sam (cc xapp-Sam)"
        result = filter_chat_secrets(prose)
        self.assertEqual(result.text, prose)
        self.assertNotIn("Slack Token", result.secret_types)
        for kind in ("bot (xoxb-)", "rotated user (xoxe.xoxp-)", "app-level (xapp-)"):
            with self.subTest(kind=kind):
                self._assert_redacted_whole(kind, {**OLD_KINDS, **NEW_KINDS}[kind])


class TestSecretScannerUsesTheSharedFamily(unittest.TestCase):
    def test_a_sign_off_line_is_not_a_scanner_hit(self):
        hits = secret_scanner.scan_secrets("xoxo-Sam")
        self.assertNotIn("Slack Token", [h.secret_type for h in hits])
        self.assertIsNone(secret_scanner._WHOLE_LINE_PATTERNS["Slack Token"].match("xoxo-Sam"))
        self.assertEqual(secret_scanner.redact_secrets("hugs xoxo-Sam", hits), "hugs xoxo-Sam")

    def test_redacts_the_full_span_of_a_refresh_token(self):
        token = NEW_KINDS["refresh (xoxe-)"]
        hit = secret_scanner.SecretHit(secret_type="Slack Token", line_number=1)
        self.assertEqual(
            secret_scanner.redact_secrets(f"vault set SLACK_REFRESH {token}", [hit]),
            "vault set SLACK_REFRESH [STORED-IN-KEYCHAIN-Slack Token]",
        )

    def test_a_bare_app_token_is_recognised_as_a_secret(self):
        hits = secret_scanner.scan_secrets(NEW_KINDS["app-level (xapp-)"])
        self.assertIn("Slack Token", [h.secret_type for h in hits])

    def test_a_token_inside_prose_is_not_a_whole_line_hit(self):
        hits = secret_scanner.scan_secrets("prose around " + NEW_KINDS["app-level (xapp-)"] + " here")
        whole_line = secret_scanner._WHOLE_LINE_PATTERNS["Slack Token"]
        self.assertIsNone(whole_line.match("prose around xapp-1-A0-0-a here"))
        self.assertTrue(all(h.secret_type != "Bare Hex Token" for h in hits))


class TestOneSharedDefinition(unittest.TestCase):
    def test_secret_scanner_patterns_are_the_shared_object(self):
        self.assertIs(secret_scanner._FULL_PATTERNS["Slack Token"], chat_secret_filter.SLACK_TOKEN_PATTERN)
        self.assertIn(
            chat_secret_filter.SLACK_TOKEN_PATTERN.pattern,
            secret_scanner._WHOLE_LINE_PATTERNS["Slack Token"].pattern,
        )

    def test_report_feedback_scrub_is_the_shared_object_and_scrubs_a_rotated_token_whole(self):
        report_feedback = _load_report_feedback()
        self.assertIs(report_feedback.SLACK_TOKEN_PATTERN, chat_secret_filter.SLACK_TOKEN_PATTERN)
        token = NEW_KINDS["rotated user (xoxe.xoxp-)"]
        self.assertEqual(
            report_feedback._redact(f"config token {token} rejected"),
            "config token <redacted-token> rejected",
        )
        self.assertEqual(
            report_feedback._redact("app token " + NEW_KINDS["app-level (xapp-)"]),
            "app token <redacted-token>",
        )

    def test_report_feedback_stays_broad_beside_the_shared_family(self):
        # The excerpt leaves the machine: report-feedback keeps its pre-#4892 broad
        # rule beside the shared object and scrubs values the narrow family skips.
        report_feedback = _load_report_feedback()
        self.assertEqual(report_feedback._redact("hugs xoxo-Samantha"), "hugs <redacted-token>")
        self.assertEqual(report_feedback._redact("cookie xoxd-AbCdEfGhIjKl"), "cookie <redacted-token>")

    def test_no_other_reader_keeps_a_private_slack_rule(self):
        # Structural pin (REVIEW.md rule 17, second exception): two copies that
        # agree pass every behavioural test; the defect IS the duplicate.
        offenders = []
        for top in ("src", "skills", "hooks"):
            for path in (REPO / top).rglob("*.py"):
                rel = path.relative_to(REPO).as_posix()
                if rel == "src/chat_secret_filter.py" or "/tests/" in f"/{rel}":
                    continue
                # report-feedback applies the shared object first (pinned above) and
                # keeps its broad backstop beside it: that excerpt leaves the machine.
                if rel == "skills/report-feedback/report-feedback.py":
                    continue
                for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                    if PRIVATE_SLACK_RULE.search(line):
                        offenders.append(f"{rel}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "private Slack token rules outside chat_secret_filter")

    def test_the_bundled_sparrow_copy_carries_the_same_definition(self):
        bundled = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "chat_secret_filter.py"
        self.assertEqual(bundled.read_text(), (REPO / "src" / "chat_secret_filter.py").read_text())


if __name__ == "__main__":
    unittest.main()
