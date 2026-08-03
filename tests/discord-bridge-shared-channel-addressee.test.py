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

Assertion-based so every line executes on a passing run (no failure-only
branches) — keeps the test itself at full diff-coverage.

Run: python3 tests/discord-bridge-shared-channel-addressee.test.py
Exit: 0 pass / non-zero on assertion failure.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from discord_addressee import is_addressed_in_shared_channel, reference_is_reply  # noqa: E402

ME = 1512984771305799792          # this bot
PRO = 1504316176686120980         # another agent
MINI = 1490412828065267872        # another agent (posts "⏳ working…")
OWNER = 1022910063620390932       # the human owner


def addressed(**kw):
    kw.setdefault("self_id", ME)
    return is_addressed_in_shared_channel(**kw)


def main() -> int:
    # --- addressed to us → process (True) ---
    assert addressed(author_is_bot=False, bot_mentioned=True, role_mentioned=False,
                     is_reply=False, reply_author_id=None), "bot @-mentions us"
    print("  ok  we are @-mentioned")

    assert addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=True,
                     is_reply=False, reply_author_id=None), "role-mention of us"
    print("  ok  role-mention of us")

    assert addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=False,
                     is_reply=True, reply_author_id=ME), "owner replies to OUR message"
    print("  ok  owner replies to OUR message")

    assert addressed(author_is_bot=True, bot_mentioned=True, role_mentioned=False,
                     is_reply=False, reply_author_id=None), "another bot @-mentions us"
    print("  ok  another bot @-mentions us (still reachable)")

    assert addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=False,
                     is_reply=False, reply_author_id=None), "fresh human message"
    print("  ok  fresh human message, no addressee (owner posting directly)")

    # --- addressed elsewhere → skip (False) — the bug being fixed ---
    assert not addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=False,
                         is_reply=True, reply_author_id=PRO), "owner replies to ANOTHER agent"
    print("  ok  owner replies to ANOTHER agent (the reported bug)")

    assert not addressed(author_is_bot=True, bot_mentioned=False, role_mentioned=False,
                         is_reply=False, reply_author_id=None), "another agent's status post"
    print("  ok  another agent's own status post (Mini '⏳ working…')")

    assert not addressed(author_is_bot=True, bot_mentioned=False, role_mentioned=False,
                         is_reply=True, reply_author_id=PRO), "bot replying to a third bot"
    print("  ok  another bot replying to a third bot")

    # --- structural: the bridge wires the gate in + carves out bot2bot ---
    bridge = (REPO / "src" / "discord-bridge.py").read_text()
    assert "is_addressed_in_shared_channel(" in bridge, \
        "discord-bridge.py does not call is_addressed_in_shared_channel"
    print("  ok  bridge calls the addressee gate")
    assert '_channel_role(str(message.channel.id)) != "bot2bot"' in bridge, \
        "discord-bridge.py does not carve out role:'bot2bot' channels"
    print("  ok  bridge carves out bot2bot channels")


    # --- reference_is_reply: a FORWARD is not a REPLY (owner-reported 2026-07-27) ---
    class _T:                      # mimics discord.MessageReferenceType members
        def __init__(self, name): self.name = name

    assert reference_is_reply(True, _T("default")) is True, "reply -> is a reply"
    print("  ok  reference.type=default is a reply")

    assert reference_is_reply(True, _T("forward")) is False, "forward -> NOT a reply"
    print("  ok  reference.type=forward is NOT a reply")

    assert reference_is_reply(False, None) is False, "no reference -> not a reply"
    print("  ok  no reference at all is not a reply")

    assert reference_is_reply(True, None) is True, "missing type -> pre-forward default"
    print("  ok  missing reference.type keeps the pre-forward default")

    assert reference_is_reply(True, "forward") is False, "raw string name also handled"
    print("  ok  raw string type name handled (not just enum members)")

    # --- the actual bug: an owner FORWARD in a shared channel must not be skipped ---
    # Before the fix the bridge passed is_reply=True for a forward; a forward has no
    # reply.resolved.author, so reply_author_id is None and the gate skipped it.
    _fwd_is_reply = reference_is_reply(True, _T("forward"))
    assert _fwd_is_reply is False, (
        "an owner's forward must reach the gate as is_reply=False, else it is "
        "classified as a reply-not-addressed-to-us and the forward handler never runs")
    print("  ok  owner FORWARD is not classified as a reply-to-someone-else")

    print("\nAll addressee-gate cases pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
