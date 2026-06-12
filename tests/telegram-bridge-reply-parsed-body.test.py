#!/usr/bin/env python3
"""Structural regression test: telegram-bridge pending_replies uses parsed.body (#1381).

Bug: the pending_replies loop called send_reply(chat_id, reply_text, ...) — the
raw result text — instead of parsed.body. parse_markers() had already been called
to extract skip/redirect/attach actions, but the stripped body was never used.
Effect: a result starting with `[channel: 123456789012345678] some text` would
send the literal `[channel:...]` string to the Telegram DM instead of dropping it.

Fix (PR that closes this test): pass parsed.body to send_reply(), then iterate
parsed.actions for attach entries to send any extracted files.

Run: python3 tests/telegram-bridge-reply-parsed-body.test.py
Exit 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "src" / "telegram-bridge.py").read_text()


class TestTelegramBridgeReplyParsedBody(unittest.TestCase):

    # ------------------------------------------------------------------
    # Locate the pending_replies block to scope all assertions tightly.
    # ------------------------------------------------------------------

    def _pending_replies_block(self) -> str:
        """Return the source of the pending_replies result-polling loop."""
        start = SRC.find("pending_replies.keys()")
        self.assertGreater(start, 0, "pending_replies loop not found in telegram-bridge.py")
        # Grab a generous window: loop body is well under 80 lines.
        return SRC[start : start + 3000]

    def test_send_reply_uses_parsed_body_not_reply_text(self):
        """send_reply() must receive parsed.body, not the raw reply_text.

        Passing reply_text leaks unstripped markers (e.g. [channel: <id>]) as
        literal text in the Telegram DM.
        """
        block = self._pending_replies_block()
        # Should contain: send_reply(chat_id, parsed.body, ...)
        self.assertIn(
            "send_reply(chat_id, parsed.body",
            block,
            "pending_replies block must call send_reply(chat_id, parsed.body, ...) not reply_text",
        )

    def test_reply_text_not_passed_to_send_reply(self):
        """reply_text must NOT be the second argument to send_reply() in the reply loop."""
        block = self._pending_replies_block()
        self.assertNotIn(
            "send_reply(chat_id, reply_text",
            block,
            "send_reply must not be called with raw reply_text — use parsed.body instead",
        )

    def test_attach_actions_handled_after_send_reply(self):
        """After send_reply(), the loop must iterate parsed.actions for attach entries.

        parse_markers() strips [file:|send:|attach:] from body but collects them
        as attach actions. They must be sent separately via send_file().
        """
        block = self._pending_replies_block()
        # Both the action-kind check and the send_file() call must be present.
        self.assertIn(
            'action.kind == "attach"',
            block,
            "pending_replies block must check action.kind == 'attach' after send_reply()",
        )
        self.assertIn(
            "send_file(chat_id, fpath)",
            block,
            "pending_replies block must call send_file(chat_id, fpath) for attach actions",
        )

    def test_attach_action_loop_follows_send_reply(self):
        """The attach-action loop must appear AFTER the send_reply() call (source order)."""
        block = self._pending_replies_block()
        send_reply_pos = block.find("send_reply(chat_id, parsed.body")
        attach_pos = block.find('action.kind == "attach"')
        self.assertGreater(send_reply_pos, 0, "send_reply(parsed.body) not found")
        self.assertGreater(attach_pos, 0, "attach action check not found")
        self.assertGreater(
            attach_pos,
            send_reply_pos,
            "attach-action loop must come AFTER send_reply() in source order",
        )

    def test_print_uses_parsed_body_not_reply_text(self):
        """The reply confirmation print must show parsed.body, not reply_text."""
        block = self._pending_replies_block()
        self.assertIn(
            "parsed.body[:80]",
            block,
            "Reply confirmation print should use parsed.body[:80] (not reply_text[:80])",
        )
        self.assertNotIn(
            "reply_text[:80]",
            block,
            "reply_text[:80] in reply print would log unstripped markers",
        )

    def test_channel_redirect_not_forwarded_to_telegram(self):
        """Telegram must not forward [channel:] redirects to an alternate channel.

        Telegram has no concept of channel routing — redirect actions are silently
        dropped. There should be no code that calls send_reply with a redirect channel_id.
        """
        block = self._pending_replies_block()
        # The redirect action kind must NOT appear in a send_reply() call context.
        # Ensure no branch applies redirect to a send_reply for Telegram.
        redirect_redirect = re.search(
            r'action\.kind\s*==\s*["\']redirect["\'].*send_reply',
            block,
            re.DOTALL,
        )
        self.assertIsNone(
            redirect_redirect,
            "Telegram bridge must not route redirect actions to send_reply() — "
            "Telegram has no channel concept; redirect is silently dropped",
        )


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(
        unittest.TestLoader().loadTestsFromTestCase(TestTelegramBridgeReplyParsedBody)
    )
    sys.exit(0 if result.wasSuccessful() else 1)
