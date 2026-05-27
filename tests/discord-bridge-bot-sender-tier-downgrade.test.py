#!/usr/bin/env python3
"""
Regression test for the #ep013 2026-05-27 bot-flood post-mortem.

Background: Sutando-Mini's bot user_id had been added to MacBook's Discord
global `allowFrom`. When Mini ran `/task-orphan-check` and his per-orphan
recovery sentinels fanned out across shared channels, every one of them
landed in MacBook's bridge as `access_tier: owner` (full capabilities)
instead of `team` (sandboxed). 21 of 22 orphans got promoted that way.

Fix: bridge-side defensive downgrade. If the Discord author has
`message.author.bot == True` AND lands in `access_tier: owner` via global
allowFrom membership, downgrade to `team`. Owner identities should be
human; cross-fleet bot mentions are peers, never owners.

Guards (structural — does not exercise the live bridge):
  1. `is_bot_sender = bool(getattr(message.author, "bot", False))` is
     extracted near the access-tier determination.
  2. A defensive downgrade block exists that flips owner→team when
     `is_bot_sender` is true.
  3. `write_owner_activity("discord", text)` is gated on the FINAL
     access_tier value (after downgrade), so bot pings don't pollute
     owner-activity tracking.

Run: python3 tests/discord-bridge-bot-sender-tier-downgrade.test.py
Exit code: 0 on pass, 1 on fail.
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"


def main() -> int:
    if not BRIDGE.exists():
        print(f"FAIL: {BRIDGE} not found", file=sys.stderr)
        return 1

    src = BRIDGE.read_text()

    # 1. Bot-author detection extracted.
    if not re.search(
        r'is_bot_sender\s*=\s*bool\s*\(\s*getattr\s*\(\s*message\.author\s*,\s*[\'"]bot[\'"]\s*,\s*False\s*\)\s*\)',
        src,
    ):
        print(
            "FAIL: missing is_bot_sender = bool(getattr(message.author, 'bot', False))",
            file=sys.stderr,
        )
        return 1

    # 2. Defensive downgrade block: when is_bot_sender AND access_tier == "owner",
    #    set access_tier = "team". Match a few lines so a stray reorder is caught.
    if not re.search(
        r'if\s+is_bot_sender\s+and\s+access_tier\s*==\s*[\'"]owner[\'"]\s*:[\s\S]{0,400}?access_tier\s*=\s*[\'"]team[\'"]',
        src,
    ):
        print(
            "FAIL: missing defensive downgrade `if is_bot_sender and access_tier == \"owner\": ... access_tier = \"team\"`",
            file=sys.stderr,
        )
        return 1

    # 3. write_owner_activity is gated on access_tier == "owner" AFTER the
    #    downgrade — not unconditionally inside the `if sender_id in allowed:`
    #    block. Search for the gated form.
    if not re.search(
        r'if\s+access_tier\s*==\s*[\'"]owner[\'"]\s*:\s*\n\s+write_owner_activity\s*\(\s*[\'"]discord[\'"]\s*,\s*text\s*\)',
        src,
    ):
        print(
            "FAIL: write_owner_activity should be gated on final access_tier == 'owner' (after downgrade)",
            file=sys.stderr,
        )
        return 1

    # 4. write_owner_activity is NOT called inside the
    #    `if sender_id in allowed:` block anymore (would re-introduce the leak
    #    for bot senders that get downgraded later).
    leak_pattern = re.search(
        r'if\s+sender_id\s+in\s+allowed\s*:\s*\n\s+access_tier\s*=\s*[\'"]owner[\'"]\s*\n\s+(?:#[^\n]*\n\s+)?write_owner_activity',
        src,
    )
    if leak_pattern:
        print(
            "FAIL: write_owner_activity should NOT be called inside the `if sender_id in allowed:` block — "
            "it would fire before the bot-downgrade and pollute owner-activity tracking.",
            file=sys.stderr,
        )
        return 1

    print("PASS: discord-bridge.py cross-fleet bot-sender downgrade looks correct.")
    print("  - is_bot_sender extracted from message.author.bot")
    print("  - owner→team downgrade fires when is_bot_sender")
    print("  - write_owner_activity gated on final access_tier (post-downgrade)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
