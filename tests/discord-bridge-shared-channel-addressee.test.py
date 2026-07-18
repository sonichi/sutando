#!/usr/bin/env python3
"""
Regression test for the shared-channel addressee gate (owner-reported 2026-07-18:
"whenever I reply to someone else, you answer as if I addressed to you").

In a shared channel configured `requireMention:false` (non-bot2bot), the bridge
was processing messages NOT addressed to this bot:
  * the owner replying to another agent (Discord auto-adds the reply-target to
    mentions; the old gate *excluded* it from the addressee check — backwards);
  * another agent's own posts (e.g. a sibling bot's "⏳ working…" status).

The fix moves the decision into the pure `src/discord_addressee.py`
(`is_addressed_in_shared_channel`) so the truth table is tested directly, and
wires the bridge to gate on `requireMention:false` + `role != "bot2bot"`.

Run: python3 tests/discord-bridge-shared-channel-addressee.test.py
Exit: 0 pass / 1 fail.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from discord_addressee import is_addressed_in_shared_channel  # noqa: E402

ME = 1512984771305799792          # this bot
PRO = 1504316176686120980         # another agent
MINI = 1490412828065267872        # another agent (posts "⏳ working…")
OWNER = 1022910063620390932       # the human owner

FAILURES = []


def check(name, got, want):
    if got is not want:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")
    else:
        print(f"  ok  {name}")


def main() -> int:
    # --- addressed to us → process (True) ---
    check("bot @-mentions us",
          is_addressed_in_shared_channel(author_is_bot=False, bot_mentioned=True,
              role_mentioned=False, is_reply=False, reply_author_id=None, self_id=ME),
          True)
    check("role-mention of us",
          is_addressed_in_shared_channel(author_is_bot=False, bot_mentioned=False,
              role_mentioned=True, is_reply=False, reply_author_id=None, self_id=ME),
          True)
    check("owner replies to OUR message",
          is_addressed_in_shared_channel(author_is_bot=False, bot_mentioned=False,
              role_mentioned=False, is_reply=True, reply_author_id=ME, self_id=ME),
          True)
    check("another bot @-mentions us (still reachable)",
          is_addressed_in_shared_channel(author_is_bot=True, bot_mentioned=True,
              role_mentioned=False, is_reply=False, reply_author_id=None, self_id=ME),
          True)
    check("fresh human message, no addressee (owner posting directly)",
          is_addressed_in_shared_channel(author_is_bot=False, bot_mentioned=False,
              role_mentioned=False, is_reply=False, reply_author_id=None, self_id=ME),
          True)

    # --- addressed elsewhere → skip (False) — the bug being fixed ---
    check("owner replies to ANOTHER agent (the reported bug)",
          is_addressed_in_shared_channel(author_is_bot=False, bot_mentioned=False,
              role_mentioned=False, is_reply=True, reply_author_id=PRO, self_id=ME),
          False)
    check("another agent's own status post (Mini '⏳ working…')",
          is_addressed_in_shared_channel(author_is_bot=True, bot_mentioned=False,
              role_mentioned=False, is_reply=False, reply_author_id=None, self_id=ME),
          False)
    check("another bot replying to a third bot",
          is_addressed_in_shared_channel(author_is_bot=True, bot_mentioned=False,
              role_mentioned=False, is_reply=True, reply_author_id=PRO, self_id=ME),
          False)

    # --- structural: the bridge wires the gate in + carves out bot2bot ---
    bridge = (REPO / "src" / "discord-bridge.py").read_text()
    if "is_addressed_in_shared_channel(" not in bridge:
        FAILURES.append("discord-bridge.py does not call is_addressed_in_shared_channel")
    else:
        print("  ok  bridge calls the addressee gate")
    if '_channel_role(str(message.channel.id)) != "bot2bot"' not in bridge:
        FAILURES.append("discord-bridge.py does not carve out role:'bot2bot' channels")
    else:
        print("  ok  bridge carves out bot2bot channels")

    if FAILURES:
        print("\nFAIL:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nAll addressee-gate cases pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
