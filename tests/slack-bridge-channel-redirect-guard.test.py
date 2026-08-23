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

        # The claim is inline (`.rename`) or delegated (`claim_for_delivery`); both are
        # the moment the file leaves the `*.txt` glob, so take whichever comes first.
        claim_sites = [p for p in (SRC.find(".rename(", proactive_block_start),
                                   SRC.find("claim_for_delivery(", proactive_block_start))
                       if p > 0]
        self.assertTrue(claim_sites, "no claim site (.rename or claim_for_delivery) found "
                                     "after proactive- block")
        rename_pos = min(claim_sites)

        # Peek must come BEFORE rename
        self.assertLess(
            peek_pos,
            rename_pos,
            "peek must appear before .rename() in the proactive-file processing block. "
            "Without this ordering, the guard doesn't work — slack-bridge would claim "
            "the file before checking its content.",
        )

    def test_guard_delegates_to_the_shared_classifier(self):
        """The distinction this test has always been about — a Discord id is not a
        Slack id — now lives in proactive_routing instead of a literal here.

        The `\\d{17,20}` spelling recognised Discord and NOTHING else, so a Matrix
        room id read as unaddressed and the file was claimable. Asserting the
        literal pinned the implementation; asserting the delegate plus its
        behaviour pins what the literal was for.
        """
        self.assertIn(
            'proactive_body_guard(f.name, peek, "slack")', SRC,
            "the peek must delegate to proactive_routing, not spell its own grammar")
        self.assertNotRegex(
            SRC, r"\\d\{17,20\}",
            "no private copy of the id grammar may survive in this adapter")

        sys.path.insert(0, str(REPO / "src"))
        from proactive_routing import body_claimable_by

        # A grep is satisfied by a no-op; these are the discriminations the
        # docstring above has always named, now actually exercised.
        self.assertTrue(body_claimable_by("[channel: C0123ABCD]\nx", "slack"),
                        "a Slack channel id is this bridge's own address")
        self.assertFalse(body_claimable_by("[channel: 1530802402603700415]\nx", "slack"),
                         "a Discord snowflake belongs to discord-bridge")
        self.assertFalse(body_claimable_by("[channel: !Room:ag2.space]\nx", "slack"),
                         "and a Matrix room to the gateway — the case the literal missed")


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestSlackBridgeChannelRedirectGuard)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
