#!/usr/bin/env python3
"""Structural regression test for the slack-bridge [channel:] redirect guard (2026-06-07).

Guards against the race condition where slack-bridge claims a proactive file
starting with `[channel: <snowflake>]` before discord-bridge can process it.
The fix: peek at file content BEFORE rename-claim and skip Discord-targeted
proactive files. (#1401, incident 2026-06-01)

Run: python3 tests/slack-bridge-channel-redirect-guard.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "src" / "slack-bridge.py").read_text()


class TestSlackBridgeChannelRedirectGuard(unittest.TestCase):

    def test_peek_before_claim_guard_present(self):
        """The source must contain a peek-before-claim guard for [channel:] redirects.

        Without this guard, slack-bridge wins the proactive-file rename race and sends
        the literal '[channel: <id>] <body>' text to the owner's Slack DM instead of
        the intended Discord channel (#1401, incident 2026-06-01).
        """
        self.assertIn(
            "[channel:",
            SRC,
            "slack-bridge.py must contain a [channel:] guard — see #1401",
        )

    def test_peek_occurs_before_rename(self):
        """The peek (read_text / read) must appear before the rename call in the
        proactive-file processing block.

        This is the structural invariant: you can't claim a file before you know
        whether to skip it.
        """
        # Find the proactive-file processing block (bounded by the 'proactive-' name check).
        proactive_block_start = SRC.find("proactive-")
        self.assertGreater(proactive_block_start, 0, "proactive-file loop not found")

        # Find peek in the code after the proactive-file check
        peek_pos = SRC.find("peek", proactive_block_start)
        self.assertGreater(peek_pos, 0, "peek variable not found after proactive- block")

        # Find rename after the proactive-file check
        rename_pos = SRC.find(".rename(", proactive_block_start)
        self.assertGreater(rename_pos, 0, "rename call not found after proactive- block")

        # Peek must come BEFORE rename
        self.assertLess(
            peek_pos,
            rename_pos,
            "peek must appear before .rename() in the proactive-file processing block. "
            "Without this ordering, the guard doesn't work — slack-bridge would claim "
            "the file before checking its content.",
        )

    def test_snowflake_regex_present(self):
        """The guard must use a Discord snowflake regex (17-20 digits) to avoid
        false-positive matches on Slack channel IDs (which start with C/D prefix,
        not pure digits).
        """
        self.assertIn(
            r"\d{17,20}",
            SRC,
            "Snowflake regex (\\d{17,20}) must be present to distinguish Discord "
            "channel IDs from Slack channel IDs",
        )


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestSlackBridgeChannelRedirectGuard)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
