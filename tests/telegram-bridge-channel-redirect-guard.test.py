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


def _claim_pos(src: str, start: int) -> int:
    """First claim site after `start` — inline `.rename(claim)` or the delegated
    `claim_for_delivery(`. Both are the moment the file leaves the `*.txt` glob."""
    hits = [p for p in (src.find(".rename(claim)", start),
                        src.find("claim_for_delivery(", start)) if p > 0]
    return min(hits) if hits else -1


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

    def test_guard_delegates_to_the_shared_classifier(self):
        """The distinction this test is about — a Discord id is not a Slack id —
        now lives in proactive_routing rather than as a literal here.

        `\\d{17,20}` recognised Discord and NOTHING else, so a Matrix room id read
        as unaddressed and the file was claimable by this bridge. The literal
        pinned an implementation; the delegate plus its behaviour pins the point.
        """
        self.assertIn(
            'body_claimable_by(peek, "telegram")', SRC,
            "the peek must delegate to proactive_routing, not spell its own grammar")
        self.assertNotRegex(
            SRC, r"\\d\{17,20\}",
            "no private copy of the id grammar may survive in this adapter")

        sys.path.insert(0, str(REPO / "src"))
        from proactive_routing import body_claimable_by

        self.assertFalse(body_claimable_by("[channel: 1530802402603700415]\nx", "telegram"),
                         "a Discord snowflake belongs to discord-bridge")
        self.assertFalse(body_claimable_by("[channel: C0123ABCD]\nx", "telegram"),
                         "a Slack channel id belongs to slack-bridge")
        self.assertFalse(body_claimable_by("[channel: !Room:ag2.space]\nx", "telegram"),
                         "and a Matrix room to the gateway — the case the literal missed")
        self.assertTrue(body_claimable_by("an ordinary proactive body", "telegram"),
                        "an unaddressed body is still this bridge's to claim")

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

        rename_pos = _claim_pos(SRC, proactive_block_start)
        self.assertGreater(rename_pos, 0, "rename(claim) not found after PROACTIVE_PREFIXES block")

        self.assertLess(
            peek_pos, rename_pos,
            "peek must appear BEFORE the rename(claim) call — claim-before-peek is the bug",
        )

    def test_guard_skips_before_claim(self):
        """The guard must `continue` before the rename — not after claiming."""
        proactive_block_start = SRC.find("PROACTIVE_PREFIXES")
        self.assertGreater(proactive_block_start, 0)

        # Anchor on the gate CALL, not on the string "[channel:" — that literal
        # appears elsewhere in a 3k-line file and silently moved this assertion.
        gate_pos = SRC.find('body_claimable_by(peek, "telegram")', proactive_block_start)
        self.assertGreater(gate_pos, 0, "delegated peek gate not found in the proactive block")
        guard_continue_pos = SRC.find("continue", gate_pos)
        rename_pos = _claim_pos(SRC, proactive_block_start)
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

    def _send_reply_body(self):
        func_pos = SRC.find("def send_reply(")
        self.assertGreater(func_pos, 0, "send_reply function not found")
        next_def_pos = SRC.find("\ndef ", func_pos + 1)
        return SRC[func_pos:next_def_pos] if next_def_pos > 0 else SRC[func_pos:]

    def test_send_reply_derives_attachments_from_parse_markers(self):
        """send_reply() must get marker grammar from parse_markers(), not a local regex.

        Regression: send_reply() used to compile its own
        `\\[(?:file|send|attach): ...\\]` regex. That stripped attachment markers
        but left every OTHER marker in the body — and poll_proactive() passes RAW
        result text to send_reply(), so `[dm-only]` and `[channel:]` were
        delivered verbatim to the owner. The morning briefing is emitted as a
        proactive result carrying `[dm-only]`, so it rendered with the marker
        visible in the message.
        """
        body = self._send_reply_body()
        self.assertIn(
            "parse_markers(", body,
            "send_reply must call parse_markers() — it is the sole owner of marker grammar",
        )
        self.assertIn(
            'kind == "attach"', body,
            'send_reply must derive attachments from parse_markers actions (kind == "attach")',
        )
        # Target the drift itself — a compiled local regex — not any mention of
        # the grammar. The docstring deliberately names the old pattern to
        # explain why it was removed; that is documentation, not a parser.
        self.assertNotIn(
            "re.compile(", body,
            "send_reply must not compile a local marker regex — the attachment-"
            "marker grammar belongs solely to src/result_markers.py",
        )

    def test_proactive_path_cannot_leak_non_attachment_markers(self):
        """poll_proactive() hands raw text to send_reply(); that is only safe
        because send_reply() now strips ALL markers via parse_markers().

        Asserted at the parser level (importing the bridge has side effects), so
        this pins the property the proactive path depends on: a body carrying
        [dm-only]/[channel:] comes back with no marker text remaining.
        """
        sys.path.insert(0, str(REPO / "src"))
        from result_markers import parse_markers

        for raw in (
            "[dm-only]\nYour calendar for today: 3 meetings.",
            "[channel: 1153072414184452241]\nStatus update.",
            "[dm-only]\n[file: /tmp/private.pdf]\nPrivate report.",
        ):
            parsed = parse_markers(raw)
            self.assertNotRegex(
                parsed.body, r"\[(dm-only|channel:|file:|send:|attach:)",
                f"marker leaked into delivered body for {raw!r}",
            )
            # idempotent: the task path passes parsed.body back through
            # send_reply(), which parses again — that must yield no attachments
            # or the file would be sent twice.
            self.assertEqual(
                parse_markers(parsed.body).actions, [],
                "re-parsing an already-stripped body must yield no actions "
                "(otherwise send_reply double-sends the task path's attachments)",
            )


if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(unittest.TestLoader().loadTestsFromTestCase(TestTelegramBridgeChannelRedirectGuard))
    sys.exit(0 if result.wasSuccessful() else 1)
