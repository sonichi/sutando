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

import ast
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

    # The self-reply case requires author_id; without it the exemption is off.
    assert addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=False,
                     is_reply=True, reply_author_id=OWNER, author_id=OWNER), \
        "owner replies to their OWN message"
    print("  ok  owner replies to their OWN message (self-reply fix)")

    # COMPOSITION: the self-reply exemption meets the other_agent_mentioned gate;
    # neither side's own tests exercise the interaction.
    assert not addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=False,
                         is_reply=True, reply_author_id=OWNER, author_id=OWNER,
                         other_agent_mentioned=True), \
        "owner self-reply that @-mentions ANOTHER agent is for that agent, not us"
    print("  ok  self-reply + other-agent mention -> NOT ours (other_agent_mentioned wins)")

    assert addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=False,
                     is_reply=True, reply_author_id=OWNER, author_id=OWNER,
                     other_agent_mentioned=False), \
        "owner self-reply with no other-agent mention is still ours"
    print("  ok  self-reply + no other-agent mention -> still ours (exemption survives)")

    assert addressed(author_is_bot=False, bot_mentioned=True, role_mentioned=False,
                     is_reply=True, reply_author_id=OWNER, author_id=OWNER,
                     other_agent_mentioned=True), \
        "an explicit @-mention of US outranks the other-agent gate"
    print("  ok  explicit mention of us outranks other_agent_mentioned")

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

    # A forward carries message.reference too, so reference_is_reply must return
    # False for it or the gate skips the owner's forwarded messages.
    class _RefType:
        def __init__(self, name):
            self.name = name

    assert reference_is_reply(True, _RefType("default")) is True, "reply reference is a reply"
    print("  ok  reference type=default → reply")
    assert reference_is_reply(True, _RefType("forward")) is False, "forward reference is not a reply"
    print("  ok  reference type=forward → NOT a reply")
    assert reference_is_reply(False, None) is False, "no reference → not a reply"
    print("  ok  no reference → not a reply")
    assert reference_is_reply(True, None) is True, "reference with missing type → treated as reply (pre-forward default)"
    print("  ok  reference with None type → reply (back-compat)")
    # accepts a bare string type name too (robust to how the caller passes it)
    assert reference_is_reply(True, "forward") is False, "string 'forward' → not a reply"
    assert reference_is_reply(True, "default") is True, "string 'default' → reply"
    print("  ok  string type names handled")

    # end-to-end through the addressee gate: owner forward (is_reply=False, human,
    # not mentioned) is ADDRESSED → processed (so the forward-handler runs).
    fwd_is_reply = reference_is_reply(True, _RefType("forward"))
    assert addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=False,
                     is_reply=fwd_is_reply, reply_author_id=None), \
        "owner's forwarded message must be treated as addressed (not skipped)"
    print("  ok  owner forward flows through as addressed")
    # a BOT replying to its OWN post is still its own chatter → skip (the
    # author_is_bot check runs before the self-reply exemption).
    assert not addressed(author_is_bot=True, bot_mentioned=False, role_mentioned=False,
                         is_reply=True, reply_author_id=MINI, author_id=MINI), \
        "another agent replying to its OWN post"
    print("  ok  another agent replying to its own post (still skipped)")

    # legacy caller (no author_id): the self-reply exemption is OFF → a human
    # self-reply is treated as before (skipped), so existing callers are unchanged.
    assert not addressed(author_is_bot=False, bot_mentioned=False, role_mentioned=False,
                         is_reply=True, reply_author_id=OWNER), \
        "legacy caller w/o author_id: owner self-reply not exempt"
    print("  ok  legacy caller w/o author_id keeps prior behavior")

    # --- structural: the bridge wires the gate in + carves out bot2bot ---
    bridge = (REPO / "src" / "discord-bridge.py").read_text()
    assert "is_addressed_in_shared_channel(" in bridge, \
        "discord-bridge.py does not call is_addressed_in_shared_channel"
    print("  ok  bridge calls the addressee gate")
    assert '_channel_role(str(message.channel.id)) != "bot2bot"' in bridge, \
        "discord-bridge.py does not carve out role:'bot2bot' channels"
    print("  ok  bridge carves out bot2bot channels")
    assert "reference_is_reply(" in bridge, \
        "discord-bridge.py does not use reference_is_reply (forward vs reply fix)"
    print("  ok  bridge uses reference_is_reply for forward/reply disambiguation")

    # A call site proves nothing if the name is never bound: dropping the import
    # leaves every regex above green and the bridge NameError-ing at runtime.
    _bound = set()
    for _node in ast.walk(ast.parse(bridge)):
        if isinstance(_node, (ast.Import, ast.ImportFrom)):
            for _a in _node.names:
                _bound.add(_a.asname or _a.name.split(".")[0])
    for _name in ("is_addressed_in_shared_channel", "reference_is_reply"):
        assert _name in _bound, \
            f"discord-bridge.py calls {_name}() but never imports it"
    print("  ok  bridge imports every addressee symbol it calls")

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
