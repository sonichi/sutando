#!/usr/bin/env python3
"""Tests for skills/bot2bot-post/post.py — the bot2bot-channel scope guard.

Regression for the 2026-07-27 dead-letter: `bot2bot-post --to <X>` silently
posted to the bot2bot channel even when X wasn't a member there, so the ping
went nowhere. bot2bot-post only posts to that one channel; the guard makes it
REFUSE a recipient who isn't a member (and thus the bot-vs-human-owner id
mix-up), instead of dead-lettering. Where to route a non-bot2bot message is the
caller's judgment — the guard doesn't prescribe a destination.

(Ids/names below are generic placeholders — this is shared-repo code.)

Run: python3 tests/bot2bot-post.test.py   (exit 0 pass / non-zero on failure)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_POST = Path(__file__).resolve().parents[1] / "skills" / "bot2bot-post" / "post.py"
_spec = importlib.util.spec_from_file_location("b2b_post", _POST)
b2b = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(b2b)

_fails = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _fails.append(name)


# Numeric ids so main()'s resolve_to_target accepts a raw --to value.
# Generic placeholders — MEMBER_* are in the bot2bot channel, OUTSIDER_* are not.
MEMBER_A, MEMBER_B, MEMBER_SELF = "100", "200", "300"
OUTSIDER_A, OUTSIDER_B = "900", "901"  # only in another channel, not bot2bot
ACCESS = {
    "allowFrom": ["1"],
    "groups": {
        "chan_bot2bot": {"role": "bot2bot", "allowFrom": [MEMBER_A, MEMBER_B, MEMBER_SELF]},
        "chan_other": {"requireMention": True, "allowFrom": ["1", OUTSIDER_A, OUTSIDER_B]},
    },
}
BOT2BOT = "chan_bot2bot"

# --- _recipient_in_channel: the scope check ---
check("channel member → in channel", b2b._recipient_in_channel(ACCESS, BOT2BOT, MEMBER_B) is True)
check("non-member (other channel) → NOT in channel", b2b._recipient_in_channel(ACCESS, BOT2BOT, OUTSIDER_A) is False)
check("unknown id → NOT in channel", b2b._recipient_in_channel(ACCESS, BOT2BOT, "nobody") is False)
check("missing channel → False", b2b._recipient_in_channel(ACCESS, "no_such", MEMBER_B) is False)
# int allowFrom entry still matches a str recipient
check("int allowFrom matches str recipient",
      b2b._recipient_in_channel({"groups": {"c": {"allowFrom": [42]}}}, "c", "42") is True)

# --- main(): the guard refuses a non-member recipient, allows a member ---
_orig = {k: getattr(b2b, k) for k in ("load_token", "load_access", "get_self_id", "post")}
_posted = {}


def _install_mocks():
    b2b.load_token = lambda: "tok"
    b2b.load_access = lambda: ACCESS
    b2b.get_self_id = lambda token: MEMBER_SELF  # this bot is a channel member
    b2b.post = lambda ch, txt, tok: _posted.update(channel=ch, text=txt) or {"id": "1"}


def _restore():
    for k, v in _orig.items():
        setattr(b2b, k, v)


_install_mocks()
try:
    # guard REFUSES a recipient who isn't in the bot2bot channel
    sys.argv = ["post.py", "--to", OUTSIDER_A, "ping", "hi"]
    raised = False
    try:
        b2b.main()
    except SystemExit as e:
        raised = True
        msg = str(e)
    check("main: --to non-member → SystemExit (refused, not posted)", raised and "posted" not in _posted)
    check("main: refusal states non-membership + points at access.json",
          raised and "not a member of the bot2bot" in msg and "access.json" in msg)

    # guard ALLOWS a channel member
    _posted.clear()
    sys.argv = ["post.py", "--to", MEMBER_B, "done", "shipped"]
    b2b.main()
    check("main: --to member → posts to bot2bot channel", _posted.get("channel") == BOT2BOT)
    check("main: member post carries the mention", _posted.get("text", "").startswith(f"<@{MEMBER_B}> "))

    # --- multi-peer NO-GUESS (2026-07-29 double misfire regression) ---
    # With 2+ peer bots allowlisted and no --to, the old code picked
    # bot_candidates[0] arbitrarily (Pro pinged Air meaning Mini; Mini pinged
    # Air meaning Pro; each stray ping triggered the target's team-tier
    # auto-refusal). New contract: post WITHOUT any mention.
    _posted.clear()
    sys.argv = ["post.py", "ping", "who is around"]
    b2b.main()
    check("main: no --to with 2 peers → posts WITHOUT a mention",
          _posted.get("text", "").startswith("ping: ") and "<@" not in _posted.get("text", ""))

    # single-peer fleets keep the convenient auto-mention
    single_access = {
        "allowFrom": ["1"],
        "groups": {"chan_bot2bot": {"role": "bot2bot",
                                    "allowFrom": [MEMBER_A, MEMBER_SELF]}},
    }
    b2b.load_access = lambda: single_access
    _posted.clear()
    sys.argv = ["post.py", "ping", "you there?"]
    b2b.main()
    check("main: no --to with exactly 1 peer → auto-mentions that peer",
          _posted.get("text", "").startswith(f"<@{MEMBER_A}> "))
    b2b.load_access = lambda: ACCESS

    # resolve_other_bot unit view: multi-peer → None, single-peer → the peer
    check("resolve_other_bot: 2 peers → None (no guess)",
          b2b.resolve_other_bot(ACCESS, MEMBER_SELF, BOT2BOT) is None)
    check("resolve_other_bot: 1 peer → that peer",
          b2b.resolve_other_bot(single_access, MEMBER_SELF, BOT2BOT) == MEMBER_A)

    # legacy configs: owner+bot share the top-level allowFrom, so the
    # not-in-global heuristic yields no bot_candidates. Same no-guess rule.
    legacy_single = {
        "allowFrom": [MEMBER_A, MEMBER_SELF],
        "groups": {"chan_bot2bot": {"role": "bot2bot",
                                    "allowFrom": [MEMBER_A, MEMBER_SELF]}},
    }
    check("resolve_other_bot: legacy 1 non-self id → that id",
          b2b.resolve_other_bot(legacy_single, MEMBER_SELF, BOT2BOT) == MEMBER_A)
    legacy_multi = {
        "allowFrom": [MEMBER_A, MEMBER_B, MEMBER_SELF],
        "groups": {"chan_bot2bot": {"role": "bot2bot",
                                    "allowFrom": [MEMBER_A, MEMBER_B, MEMBER_SELF]}},
    }
    check("resolve_other_bot: legacy 2 non-self ids → None (no guess)",
          b2b.resolve_other_bot(legacy_multi, MEMBER_SELF, BOT2BOT) is None)
finally:
    _restore()

print()
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("all tests passed — bot2bot-channel scope guard")
