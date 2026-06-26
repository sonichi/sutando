#!/usr/bin/env python3
"""Structural regression test for the telegram-bridge [channel:] redirect guard (2026-06-07).

Guards against the race condition where telegram-bridge claims a proactive file
starting with `[channel: <snowflake>]` before discord-bridge can process it.
The fix: peek at file content BEFORE rename-claim and skip Discord-targeted
proactive files. (#1401)

Mirrors slack-bridge-channel-redirect-guard.test.py — same bug class, same fix.

Run: python3 tests/telegram-bridge-channel-redirect-guard.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "src" / "telegram-bridge.py").read_text()


class TestTelegramBridgeChannelRedirectGuard(unittest.TestCase):

    def test_re_imported_at_module_level(self):
        """re must be imported at module level (not lazily inside a function).

        The channel-redirect guard uses re.match() in the proactive polling loop
        which is module-level code — a lazy import inside a function won't cover it.
        """
        # Module-level import: appears before any `def ` line that uses it
        re_import_pos = SRC.find("import re\n")
        self.assertGreater(re_import_pos, 0, "module-level 'import re' not found in telegram-bridge.py")

    def test_channel_redirect_guard_present(self):
        """The source must contain a peek-before-claim guard for [channel:] redirects.

        Without this guard, telegram-bridge wins the proactive-file rename race and
        sends the literal '[channel: <id>] <body>' text to the owner's Telegram DM
        instead of the intended Discord channel (#1401).
        """
        self.assertIn(
            "[channel:",
            SRC,
            "telegram-bridge.py must contain a [channel:] guard — see #1401",
        )

    def test_snowflake_pattern_present(self):
        """The guard must use a Discord snowflake pattern (17-20 digits) to avoid
        false-positives on Slack channel IDs (which use letter prefixes like C, D, G).
        """
        self.assertIn(
            r"\d{17,20}",
            SRC,
            r"telegram-bridge.py must include snowflake regex \d{17,20} in the [channel:] guard",
        )

    def test_peek_occurs_before_rename(self):
        """The peek (read_text) must appear before the rename call in the
        proactive-file processing block.

        This is the structural invariant: you can't claim a file before you know
        whether to skip it.
        """
        proactive_block_start = SRC.find("PROACTIVE_PREFIXES")
        self.assertGreater(proactive_block_start, 0, "PROACTIVE_PREFIXES loop not found")

        peek_pos = SRC.find("peek", proactive_block_start)
        self.assertGreater(peek_pos, 0, "peek variable not found after PROACTIVE_PREFIXES block")

        rename_pos = SRC.find(".rename(claim)", proactive_block_start)
        self.assertGreater(rename_pos, 0, "rename(claim) not found after PROACTIVE_PREFIXES block")

        self.assertLess(
            peek_pos, rename_pos,
            "peek must appear BEFORE the rename(claim) call — claim-before-peek is the bug",
        )

    def test_guard_skips_before_claim(self):
        """The guard must `continue` before the rename — not after claiming."""
        proactive_block_start = SRC.find("PROACTIVE_PREFIXES")
        self.assertGreater(proactive_block_start, 0)

        # The guard should appear before rename(claim) in source order
        guard_continue_pos = SRC.find("continue", SRC.find("[channel:", proactive_block_start))
        rename_pos = SRC.find(".rename(claim)", proactive_block_start)
        self.assertGreater(guard_continue_pos, 0, "continue not found in guard block")
        self.assertLess(
            guard_continue_pos, rename_pos,
            "guard continue must come before rename(claim) — otherwise file is already claimed",
        )

    def test_no_lazy_re_import_in_send_reply(self):
        """The lazy `import re` that was inside send_reply() must be removed
        now that re is imported at module level."""
        # Find send_reply function
        func_pos = SRC.find("def send_reply(")
        self.assertGreater(func_pos, 0, "send_reply function not found")
        # Check no lazy import inside it
        next_def_pos = SRC.find("\ndef ", func_pos + 1)
        func_body = SRC[func_pos:next_def_pos] if next_def_pos > 0 else SRC[func_pos:]
        self.assertNotIn(
            "import re",
            func_body,
            "Lazy 'import re' inside send_reply should be removed — re is now top-level",
        )


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestTelegramBridgeChannelRedirectGuard))
    sys.exit(0 if result.wasSuccessful() else 1)
