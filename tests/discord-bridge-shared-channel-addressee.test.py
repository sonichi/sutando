#!/usr/bin/env python3
"""
Regression test for the shared-channel addressee gate (owner-reported 2026-07-18:
"whenever I reply to someone else, you answer as if I addressed to you").

In a shared channel configured `requireMention:false` (non-bot2bot), the bridge
was processing messages NOT addressed to this bot:
  * the owner replying to another agent (Discord auto-adds the reply-target to
    mentions; the old gate *excluded* it from the addressee check — backwards);
  * another agent's own posts (e.g. a sibling bot's "⏳ working…" status);
  * a human @-mentioning a DIFFERENT agent — observed 2026-08-06, when the owner
    posted "<@PRO> keep improving the arcade" twice in five minutes in a channel
    named "arcade — Pro iterating, Chi reviews". Every listening bot ingested it:
    this one queued tasks it could only discard, and its progress-streamer posted
    "⏳ working…" into the lane the owner had pointed at one agent.

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

from discord_addressee import is_addressed_in_shared_channel  # noqa: E402

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

    # --- a human addressing a DIFFERENT agent ---
    assert not addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=False,
                         is_reply=False, reply_author_id=None,
                         other_agent_mentioned=True), "owner @-mentions ANOTHER agent"
    print("  ok  owner @-mentions another agent -> not ours")

    # Reachability must survive: naming us AND a peer is still ours.
    assert addressed(author_is_bot=False, bot_mentioned=True, role_mentioned=False,
                     is_reply=False, reply_author_id=None,
                     other_agent_mentioned=True), "owner @-mentions BOTH us and a peer"
    print("  ok  owner @-mentions us AND a peer -> still ours")

    assert addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=True,
                     is_reply=False, reply_author_id=None,
                     other_agent_mentioned=True), "role-mention of us alongside a peer"
    print("  ok  role-mention of us alongside a peer -> still ours")

    assert addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=False,
                     is_reply=True, reply_author_id=ME,
                     other_agent_mentioned=True), "reply to US that also names a peer"
    print("  ok  reply to us that also names a peer -> still ours")

    # Unaddressed owner messages are unchanged (out of scope).
    assert addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=False,
                     is_reply=False, reply_author_id=None,
                     other_agent_mentioned=False), "owner addresses nobody"
    print("  ok  owner addresses nobody -> still ours (unchanged)")

    # Omitting the kwarg entirely must behave as before this change.
    assert addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=False,
                     is_reply=False, reply_author_id=None), "default keeps old behavior"
    print("  ok  parameter defaults to False (back-compatible)")

    assert "other_agent_mentioned=_other_agent_mentioned" in bridge, \
        "discord-bridge.py does not pass other_agent_mentioned to the gate"
    print("  ok  bridge resolves and passes other_agent_mentioned")
    assert 'getattr(u, "bot", False)' in bridge and "message, \"mentions\"" in bridge, \
        "discord-bridge.py does not derive peer mentions from message.mentions"
    print("  ok  bridge derives peer mentions from message.mentions")

    print("\nAll addressee-gate cases pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
