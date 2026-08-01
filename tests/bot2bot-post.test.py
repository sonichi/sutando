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
import re
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
finally:
    _restore()

# --- kind vocabulary: every tag the docs tell agents to use must be accepted ---
# Regression for the 2026-08-01 drift: proactive-loop/SKILL.md documents `nack:`
# ("vetoing another bot's pending claim") but VALID_KINDS omitted it, so post.py
# exited 2 on a documented primitive. That is not a cosmetic mismatch — a rejected
# coordination tag degrades to "the retraction lands AFTER the claim it retracts",
# which is exactly how it was found: a nack post was refused, went unnoticed, and
# the correction arrived after the message it corrected.
check("nack is a valid kind", "nack" in b2b.VALID_KINDS)

_DOC = Path(__file__).resolve().parents[1] / "skills" / "proactive-loop" / "SKILL.md"
_doc_kinds = set(re.findall(r"`([a-z-]+):`", _DOC.read_text())) if _DOC.exists() else set()
# FLOOR, and it is the load-bearing half. Without it this check is disableable by
# the very event it exists to catch: if SKILL.md is moved/renamed, or the tags stop
# matching the backtick form, `_doc_kinds` degrades to set(), the gap below is empty,
# and the drift assertion PASSES — reporting green on an unmonitored vocabulary.
# An assertion that a mechanism exists is only meaningful if it cannot pass in the
# broken state, and "source of truth unreadable" is one of the broken states.
check(f"documented-kind extraction is non-degenerate ({len(_doc_kinds)} found, floor 5)",
      len(_doc_kinds) >= 5)
# `opinion-requested:` is the prose name for the `opinion` kind; map it.
_doc_kinds = {"opinion" if k == "opinion-requested" else k for k in _doc_kinds}
_undocumented_gap = _doc_kinds - b2b.VALID_KINDS
check(f"every kind documented in proactive-loop SKILL.md is accepted (gap: {sorted(_undocumented_gap)})",
      not _undocumented_gap)

# main() accepts nack end-to-end, and an unknown kind is still refused (guard not disabled)
_install_mocks()
try:
    _posted.clear()
    sys.argv = ["post.py", "--to", MEMBER_B, "nack", "vetoing that claim"]
    b2b.main()
    check("main: nack posts to bot2bot channel", _posted.get("channel") == BOT2BOT)
    check("main: nack body carries the tag", "nack:" in _posted.get("text", ""))

    _posted.clear()
    sys.argv = ["post.py", "--to", MEMBER_B, "definitely-not-a-kind", "x"]
    refused = False
    try:
        b2b.main()
    except SystemExit:
        refused = True
    check("main: unknown kind STILL refused (positive control — guard not disabled)",
          refused and not _posted)
finally:
    _restore()

print()
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("all tests passed — bot2bot-channel scope guard + kind vocabulary")
